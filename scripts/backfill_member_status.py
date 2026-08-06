"""Backfill the three-tier member model (Newbie / Speaker / Moderated).

Assigns the Newbie role to every member who isn't a Speaker and isn't currently
muted, sets the DB member_status for all tiers, and snapshots prior_status for
moderated members. Verifies the Moderated role sits above Newbie and Speaker.

Usage:
    python scripts/backfill_member_status.py            # apply
    python scripts/backfill_member_status.py --dry-run  # report only, no writes
"""
import argparse
import asyncio
import os
import logging
import sys
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.db_handler import DatabaseHandler, MEMBER_STATUS_NEWBIE, MEMBER_STATUS_SPEAKER, MEMBER_STATUS_MODERATED

load_dotenv()

logger = logging.getLogger("BackfillStatus")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dry-run', action='store_true', help='Report what would change without writing anything')
args = parser.parse_args()

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)


def resolve_role_id(db: DatabaseHandler, guild_id: int | None, field: str, env_var: str) -> int | None:
    if guild_id is None:
        return None
    sc = getattr(db, 'server_config', None)
    if sc:
        role_id = sc.get_server_field(guild_id, field, cast=int)
        if role_id:
            return role_id
    env_value = os.getenv(env_var)
    return int(env_value) if env_value else None


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not TARGET_GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        return

    db = DatabaseHandler()
    newbie_id = resolve_role_id(db, TARGET_GUILD_ID, 'newbie_role_id', 'NEWBIE_ROLE_ID')
    speaker_id = resolve_role_id(db, TARGET_GUILD_ID, 'speaker_role_id', 'SPEAKER_ROLE_ID')
    moderated_id = resolve_role_id(db, TARGET_GUILD_ID, 'moderated_role_id', 'MODERATED_ROLE_ID')
    if not newbie_id or not speaker_id or not moderated_id:
        logger.error(
            f"Tier roles not fully configured — need NEWBIE_ROLE_ID, SPEAKER_ROLE_ID, MODERATED_ROLE_ID. "
            f"Got newbie={newbie_id}, speaker={speaker_id}, moderated={moderated_id}"
        )
        await client.close()
        return

    guild = client.get_guild(TARGET_GUILD_ID)
    if not guild:
        logger.error(f"Guild {TARGET_GUILD_ID} not found")
        await client.close()
        return

    newbie_role = guild.get_role(newbie_id)
    speaker_role = guild.get_role(speaker_id)
    moderated_role = guild.get_role(moderated_id)
    if not newbie_role or not speaker_role or not moderated_role:
        logger.error("One or more tier roles not found in the guild — create them first.")
        await client.close()
        return

    # Verify Moderated sits above Newbie & Speaker. NOTE: this ordering does NOT
    # enforce moderation (Discord allows-win over denies across roles); it is
    # hygiene for role-list readability. The block comes from role removal.
    if not (moderated_role.position > newbie_role.position and moderated_role.position > speaker_role.position):
        logger.warning(
            f"Moderated role (pos {moderated_role.position}) is NOT above Newbie (pos {newbie_role.position}) "
            f"and Speaker (pos {speaker_role.position}). Not blocking, but reorder for clarity."
        )

    members = sorted(
        [m for m in guild.members if not m.bot],
        key=lambda m: m.joined_at or m.created_at,
    )

    # Optional: skip members holding any staff role (comma-separated IDs in STAFF_ROLE_IDS).
    staff_ids = set()
    for part in os.getenv('STAFF_ROLE_IDS', '').split(','):
        part = part.strip()
        if part:
            staff_ids.add(int(part))
    staff_skipped = 0

    counts = {MEMBER_STATUS_NEWBIE: 0, MEMBER_STATUS_SPEAKER: 0, MEMBER_STATUS_MODERATED: 0}
    need_newbie = []
    need_moderated = []
    stale_removals = []  # (member, [roles]) — members holding >1 tier role
    already = 0
    errors = 0

    tier_roles = {'newbie': newbie_role, 'speaker': speaker_role, 'moderated': moderated_role}

    # Bulk-fetch member tier state once (avoids one query per member, which times
    # out at ~8k members). Empty map on error -> falls back to role-only logic.
    status_map = db.get_guild_member_statuses(TARGET_GUILD_ID)
    logger.info(f"Loaded {len(status_map)} guild member status rows")

    for member in members:
        if staff_ids and any(r.id in staff_ids for r in member.roles):
            staff_skipped += 1
            continue
        has_speaker = speaker_role in member.roles
        has_moderated = moderated_role in member.roles
        # The SQL migration marks legacy speaker_muted=true rows as 'moderated'.
        # Honour that so pre-existing mutes (Speaker already removed, Moderated not
        # yet assigned) are NOT demoted to Newbie by the role-based classification.
        db_row = status_map.get(member.id) or {}
        db_status = db_row.get('member_status')
        if has_moderated or db_status == MEMBER_STATUS_MODERATED:
            status = MEMBER_STATUS_MODERATED
        elif has_speaker:
            status = MEMBER_STATUS_SPEAKER
        else:
            status = MEMBER_STATUS_NEWBIE
        counts[status] += 1

        if status == MEMBER_STATUS_NEWBIE and newbie_role not in member.roles:
            need_newbie.append(member)
        elif status == MEMBER_STATUS_MODERATED and moderated_role not in member.roles:
            need_moderated.append(member)
        else:
            already += 1

        # Ensure exactly one tier role: collect stale roles to strip.
        stale = [r for key, r in tier_roles.items() if key != status and r in member.roles]
        if stale:
            stale_removals.append((member, stale))

        # Only write the DB when the status changes (or for moderated, when the
        # DM snapshot is missing). Keeps re-runs cheap and idempotent.
        if not args.dry_run:
            try:
                if status == MEMBER_STATUS_MODERATED:
                    needs_status = db_status != MEMBER_STATUS_MODERATED or db_row.get('prior_can_message_bot') is None
                    if needs_status:
                        db.set_member_status(member.id, TARGET_GUILD_ID, status,
                                             prior_status=MEMBER_STATUS_SPEAKER, set_prior=True)
                    db.set_member_can_message_bot(member.id, False, username=member.name)
                elif db_status != status:
                    db.set_member_status(member.id, TARGET_GUILD_ID, status)
            except Exception as e:
                errors += 1
                if errors <= 10:
                    logger.error(f"  DB error for {member.name} ({member.id}): {e}")

    logger.info(
        f"Tiers: newbie={counts[MEMBER_STATUS_NEWBIE]}, "
        f"speaker={counts[MEMBER_STATUS_SPEAKER]}, moderated={counts[MEMBER_STATUS_MODERATED]}"
    )
    logger.info(f"Staff members skipped: {staff_skipped}")
    logger.info(
        f"{len(need_newbie)} need Newbie, {len(need_moderated)} need Moderated, "
        f"{len(stale_removals)} need stale tier roles stripped "
        f"({'dry-run, no writes' if args.dry_run else 'applying'})..."
    )

    async def safe_role_op(op, *args, **kwargs):
        """Run a Discord role op with a hard timeout + one retry.

        A hung socket (seen during the migration) must not stall the whole run.
        """
        for attempt in (1, 2):
            try:
                await asyncio.wait_for(op(*args, **kwargs), timeout=60)
                return True
            except asyncio.TimeoutError:
                if attempt == 2:
                    raise
                logger.warning("  role op timed out — retrying")
                await asyncio.sleep(3)
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(3)

    # --- Newbie assignment (concurrent, rate-limit-safe) ---
    CONCURRENCY = 3
    sem = asyncio.Semaphore(CONCURRENCY)

    async def assign_newbie(member):
        nonlocal errors
        if newbie_role in member.roles:
            return True  # idempotent — already has it
        async with sem:
            try:
                await safe_role_op(member.add_roles, newbie_role,
                                   reason="Backfill Newbie role (three-tier migration)")
                return True
            except Exception as e:
                errors += 1
                if errors <= 10:
                    logger.error(f"  Failed for {member.name} ({member.id}): {e}")
                return False

    assigned = 0
    errors = 0
    start = time.time()
    if args.dry_run:
        assigned = len(need_newbie)
    else:
        results = await asyncio.gather(*[assign_newbie(m) for m in need_newbie])
        assigned = sum(1 for r in results if r)
        if assigned % 200 == 0 and assigned:
            elapsed = time.time() - start
            rate = assigned / elapsed
            remaining = (len(need_newbie) - assigned) / rate if rate > 0 else 0
            logger.info(f"  Progress: {assigned}/{len(need_newbie)} (~{remaining/60:.0f} min remaining)")

    # --- Moderated assignment ---
    mod_assigned = 0
    for member in need_moderated:
        if args.dry_run:
            mod_assigned += 1
            continue
        try:
            if moderated_role not in member.roles:
                await safe_role_op(member.add_roles, moderated_role,
                                   reason="Backfill Moderated role (three-tier migration)")
            mod_assigned += 1
        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.error(f"  Failed assigning Moderated to {member.name} ({member.id}): {e}")

    # --- Stale tier-role stripping ---
    stripped = 0
    for member, stale in stale_removals:
        if args.dry_run:
            stripped += 1
            continue
        try:
            await safe_role_op(member.remove_roles, *stale, reason="Backfill: ensure exactly one tier role")
            stripped += 1
        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.error(f"  Failed stripping roles for {member.name} ({member.id}): {e}")

    elapsed = time.time() - start
    logger.info(
        f"Done! Newbie assigned: {assigned}/{len(need_newbie)}, "
        f"Moderated assigned: {mod_assigned}/{len(need_moderated)}, "
        f"Stale roles stripped: {stripped}/{len(stale_removals)}, "
        f"Already correct: {already}, Errors: {errors}, Time: {elapsed:.0f}s"
    )
    if not args.dry_run:
        logger.info(
            "Runbook: 1) apply the Supabase SQL migration, 2) deploy the new bot code, "
            "3) run scripts/migrate_channel_modes.py, 4) run scripts/sense_check_three_tier.py as the pre-live gate."
        )
    await client.close()


client.run(TOKEN)
