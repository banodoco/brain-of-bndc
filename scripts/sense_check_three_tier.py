"""Pre-deploy sense-check for the three-tier member model.

Read-only audit of Discord + DB state against the new model. Exits 0 (pass) or
1 (fail) and prints a counts summary. Run as the FINAL gate AFTER the Supabase
SQL migration is applied, the new bot code is deployed, and the
backfill/channel-mode scripts have run.

Checks:
  1. Every non-bot member holds exactly one tier role (Newbie/Speaker/Moderated).
  2. Every channel resolves to a valid mode (bot/newbie/community/appeal).
  3. @everyone denies SEND_PERMS and pin_messages in every channel (no channel open to everyone).
     Tier roles match the mode table including forum-only pin_messages grants.
  4. Moderated role is positioned above Newbie & Speaker.
  5. Every moderated/muted member holds Moderated and lacks Newbie/Speaker.
  6. DB member_status matches the member's actual tier role.
  7. can_message_bot matches snapshots for moderated members.
  8. Expected special-channel modes present (gate=bot, intro/grants/help=newbie, moderation=appeal).

Usage:
    python scripts/sense_check_three_tier.py
"""
import asyncio
import os
import logging
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.db_handler import (
    DatabaseHandler, MEMBER_STATUS_NEWBIE, MEMBER_STATUS_SPEAKER, MEMBER_STATUS_MODERATED,
    CHANNEL_MODES,
)
from src.common.speaker_perms import PIN_PERMS, SEND_PERMS, pin_allowed

load_dotenv()

logger = logging.getLogger("SenseCheck")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

DEFAULT_HELP_CHANNEL_ID = 1163250319107555388
DEFAULT_MODERATION_CHANNEL_ID = 1475121919484366962

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

failures: list[str] = []
counts = {MEMBER_STATUS_NEWBIE: 0, MEMBER_STATUS_SPEAKER: 0, MEMBER_STATUS_MODERATED: 0}


def check(ok: bool, message: str):
    if ok:
        logger.info(f"  ✓ {message}")
    else:
        failures.append(message)
        logger.error(f"  ✗ {message}")


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


def resolve_channel_id(db: DatabaseHandler, guild_id: int, field: str, env_var: str, default: int | None = None) -> int | None:
    sc = getattr(db, 'server_config', None)
    if sc:
        val = sc.get_server_field(guild_id, field, cast=int)
        if val:
            return val
    env_value = os.getenv(env_var)
    if env_value:
        return int(env_value)
    return default


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not TARGET_GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        sys.exit(1)

    db = DatabaseHandler()
    newbie_id = resolve_role_id(db, TARGET_GUILD_ID, 'newbie_role_id', 'NEWBIE_ROLE_ID')
    speaker_id = resolve_role_id(db, TARGET_GUILD_ID, 'speaker_role_id', 'SPEAKER_ROLE_ID')
    moderated_id = resolve_role_id(db, TARGET_GUILD_ID, 'moderated_role_id', 'MODERATED_ROLE_ID')

    guild = client.get_guild(TARGET_GUILD_ID)
    if not guild:
        logger.error(f"Guild {TARGET_GUILD_ID} not found")
        await client.close()
        sys.exit(1)

    newbie_role = guild.get_role(newbie_id) if newbie_id else None
    speaker_role = guild.get_role(speaker_id) if speaker_id else None
    moderated_role = guild.get_role(moderated_id) if moderated_id else None
    tier_roles = {'newbie': newbie_role, 'speaker': speaker_role, 'moderated': moderated_role}

    if not all(tier_roles.values()):
        check(False, "All three tier roles (Newbie/Speaker/Moderated) must resolve in the guild.")
        await client.close()
        sys.exit(1)

    # ── Check 1: exactly one tier role per member ──
    members = [m for m in guild.members if not m.bot]
    bad_tier = 0
    for m in members:
        held = [k for k, r in tier_roles.items() if r in m.roles]
        if len(held) != 1:
            bad_tier += 1
            if bad_tier <= 10:
                check(False, f"Member {m.name} ({m.id}) holds tier roles {held or 'none'}")
        else:
            counts[held[0]] += 1
    check(bad_tier == 0, f"Every member holds exactly one tier role ({len(members) - bad_tier}/{len(members)} clean)")

    # ── Check 4: Moderated positioned above Newbie & Speaker ──
    check(
        moderated_role.position > newbie_role.position and moderated_role.position > speaker_role.position,
        f"Moderated (pos {moderated_role.position}) above Newbie (pos {newbie_role.position}) & Speaker (pos {speaker_role.position})",
    )

    # ── Channels ──
    modes = db.get_all_channel_speaker_modes(guild_id=TARGET_GUILD_ID)
    channels = [c for c in guild.channels
                if isinstance(c, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel))]

    # ── Check 2: valid modes ──
    invalid = [cid for cid, m in modes.items() if m not in CHANNEL_MODES]
    check(not invalid, f"All DB channel modes valid (bot/newbie/community/appeal) — {len(modes)} modes, {len(invalid)} invalid")

    # ── Check 3: @everyone explicitly DENIES SEND_PERMS (and pin) everywhere ──
    # An unset (None) overwrite is a leak — @everyone's base permissions default
    # to allowing send in text channels, so only an explicit deny is safe.
    from src.common.speaker_perms import _expected_values
    everyone_leaks = []
    tier_mismatches = []
    for c in channels:
        is_forum = c.type is discord.ChannelType.forum
        ow = c.overwrites_for(guild.default_role)
        for perm in SEND_PERMS + list(PIN_PERMS):
            if getattr(ow, perm) is not False:
                everyone_leaks.append(f"#{c.name} ({perm})")
                break
        # Validate each tier role's overwrite matches the expected mode table,
        # including the forum-only pin_messages grant.
        mode = modes.get(c.id) or 'community'
        for role_key, role in (('newbie', newbie_role), ('speaker', speaker_role), ('moderated', moderated_role)):
            expected = _expected_values(mode, role_key)
            expected['pin_messages'] = pin_allowed(mode, role_key, is_forum=is_forum)
            row_ow = c.overwrites_for(role)
            for perm in SEND_PERMS + list(PIN_PERMS):
                if getattr(row_ow, perm) != expected[perm]:
                    tier_mismatches.append(
                        f"#{c.name} mode={mode} {role_key}.{perm}: "
                        f"expected {expected[perm]}, got {getattr(row_ow, perm)}"
                    )
                    break
    check(not everyone_leaks, f"@everyone explicitly denied SEND_PERMS in every channel ({len(channels)} channels, {len(everyone_leaks)} leaks)")
    check(not tier_mismatches, f"Tier-role overwrites match the expected mode table ({len(channels)} channels, {len(tier_mismatches)} mismatches)" + (f" — e.g. {tier_mismatches[0]}" if tier_mismatches else ""))

    # ── Check 8: special-channel modes ──
    special = {
        resolve_channel_id(db, TARGET_GUILD_ID, 'gate_channel_id', 'GATE_CHANNEL_ID'): 'bot',
        resolve_channel_id(db, TARGET_GUILD_ID, 'intro_channel_id', 'INTRO_CHANNEL_ID'): 'newbie',
        resolve_channel_id(db, TARGET_GUILD_ID, 'grants_channel_id', 'GRANTS_CHANNEL_ID'): 'newbie',
        resolve_channel_id(db, TARGET_GUILD_ID, 'help_channel_id', 'HELP_CHANNEL_ID', DEFAULT_HELP_CHANNEL_ID): 'newbie',
        resolve_channel_id(db, TARGET_GUILD_ID, 'moderation_channel_id', 'MODERATION_CHANNEL_ID', DEFAULT_MODERATION_CHANNEL_ID): 'appeal',
    }
    special_bad = []
    for cid, expected in special.items():
        if cid is None:
            continue
        actual = modes.get(cid)
        if actual != expected:
            special_bad.append(f"channel {cid}: expected {expected}, got {actual}")
    check(not special_bad, "Special-channel modes correct (gate=bot, intro/grants/help=newbie, moderation=appeal)" + (f" — {special_bad}" if special_bad else ""))

    # ── Check 9: gate channel readable by Newbie + Speaker ──
    # The gate channel pins the onboarding / welcome message. Nobody may post
    # there (bot mode), but Newbie and Speaker must be able to read it.
    gate_cid = resolve_channel_id(db, TARGET_GUILD_ID, 'gate_channel_id', 'GATE_CHANNEL_ID')
    view_bad = []
    if gate_cid is not None:
        gate_ch = next((c for c in channels if c.id == gate_cid), None)
        if gate_ch is None:
            view_bad.append("gate channel not found in guild")
        else:
            for role_key, role in (('newbie', newbie_role), ('speaker', speaker_role)):
                ow = gate_ch.overwrites_for(role)
                if ow.view_channel is not True:
                    view_bad.append(f"#{gate_ch.name} {role_key}.view_channel expected True, got {ow.view_channel}")
    check(not view_bad, "Gate channel readable by Newbie + Speaker (view_channel=True)" + (f" — {view_bad}" if view_bad else ""))

    # ── Check 5 & 6: moderated role state + DB status match ──
    # Bulk-fetch member statuses once (paged) — per-member queries hang at ~22k members.
    status_map = db.get_guild_member_statuses(TARGET_GUILD_ID)
    logger.info(f"Loaded {len(status_map)} DB status rows for checks 5-6")
    muted_ids = set(db.get_muted_member_ids(guild_id=TARGET_GUILD_ID))
    stale_moderated = 0
    db_mismatch = 0
    for m in members:
        db_status = (status_map.get(m.id) or {}).get('member_status')
        held = [k for k, r in tier_roles.items() if r in m.roles]
        role_status = held[0] if len(held) == 1 else None
        if role_status is not None and db_status != role_status:
            db_mismatch += 1
            if db_mismatch <= 10:
                check(False, f"DB/role mismatch: {m.name} ({m.id}) DB={db_status}, role={role_status}")
        if db_status == MEMBER_STATUS_MODERATED or m.id in muted_ids:
            if moderated_role not in m.roles or newbie_role in m.roles or speaker_role in m.roles:
                stale_moderated += 1
                if stale_moderated <= 10:
                    check(False, f"Moderated member {m.name} ({m.id}) has wrong tier roles")
    check(db_mismatch == 0, f"DB member_status matches actual roles ({len(members) - db_mismatch}/{len(members)})")
    check(stale_moderated == 0, f"All moderated members hold Moderated and lack Newbie/Speaker")

    # ── Check 7: every moderated member must have can_message_bot revoked ──
    cmb_bad = 0
    for m in members:
        if moderated_role in m.roles:
            actual = db.get_member_can_message_bot(m.id)
            if actual:
                cmb_bad += 1
                if cmb_bad <= 10:
                    check(False, f"Moderated member {m.name} ({m.id}) still has can_message_bot=True")
    check(cmb_bad == 0, f"All moderated members have can_message_bot revoked")

    # ── Summary ──
    logger.info(
        f"Tier counts: newbie={counts[MEMBER_STATUS_NEWBIE]}, "
        f"speaker={counts[MEMBER_STATUS_SPEAKER]}, moderated={counts[MEMBER_STATUS_MODERATED]}"
    )
    if failures:
        logger.error(f"SENSE-CHECK FAILED: {len(failures)} problem(s) — do NOT go live until resolved.")
        await client.close()
        sys.exit(1)
    logger.info("SENSE-CHECK PASSED — migration is complete and consistent; safe to go live.")
    await client.close()


client.run(TOKEN)
