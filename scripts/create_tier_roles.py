"""Create the Newbie and Moderated tier roles and position Moderated above both.

Idempotent — safe to re-run. If NEWBIE_ROLE_ID / MODERATED_ROLE_ID are already
set, existing roles with those IDs are reused (by name otherwise).

Positioning: Moderated (pos 3) > Speaker (pos 2) > Newbie (pos 1) > @everyone (0).
NOTE: role position is NOT what enforces moderation — Discord OR's all role
allows/denies and allows win, so a member holding BOTH Moderated and a granting
role can still post. The block comes from moderation REMOVING Newbie/Speaker and
the 5-minute reconciliation stripping strays. Positioning Moderated above the
others is defense-in-depth / hygiene, kept for readability of the role list.

Usage:
    python scripts/create_tier_roles.py            # apply
    python scripts/create_tier_roles.py --dry-run  # report only
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

load_dotenv()

logger = logging.getLogger("CreateTierRoles")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dry-run', action='store_true', help='Report what would change without writing anything')
args = parser.parse_args()

intents = discord.Intents.default()
client = discord.Client(intents=intents)


def _int_env(name: str) -> int | None:
    val = os.getenv(name)
    return int(val) if val else None


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        return

    guild = client.get_guild(GUILD_ID)
    if not guild:
        logger.error(f"Guild {GUILD_ID} not found — is the bot in it?")
        await client.close()
        return

    speaker_role = guild.get_role(_int_env('SPEAKER_ROLE_ID')) if _int_env('SPEAKER_ROLE_ID') else None
    if not speaker_role:
        logger.error("SPEAKER_ROLE_ID does not resolve to a role in this guild — aborting position setup.")
        await client.close()
        return

    def find_or_create(name: str, configured_id: int | None):
        if configured_id:
            role = guild.get_role(configured_id)
            if role:
                return role, False
        existing = discord.utils.get(guild.roles, name=name)
        if existing:
            return existing, False
        return None, True

    async def ensure(name: str, configured_id: int | None):
        role, would_create = find_or_create(name, configured_id)
        if args.dry_run:
            if role:
                logger.info(f"  [dry-run] {name}: exists as {role.id}, position would be set")
            else:
                logger.info(f"  [dry-run] {name}: would be created (new role)")
            return role
        if not role:
            role = await guild.create_role(name=name, reason="Three-tier member model — role creation")
            logger.info(f"  Created {name} role ({role.id})")
        return role

    newbie = await ensure("Newbie", _int_env('NEWBIE_ROLE_ID'))
    moderated = await ensure("Moderated", _int_env('MODERATED_ROLE_ID'))

    if args.dry_run:
        logger.info("  [dry-run] would position Moderated > Speaker > Newbie (1/2/3, just above @everyone)")
        await client.close()
        return

    # Position: Moderated > Speaker > Newbie > @everyone, contiguous just above
    # @everyone. Use the bulk reorder API (single atomic call) — per-role
    # edit(position=...) uses swap semantics and scrambles relative order.
    try:
        current = sorted(guild.roles, key=lambda r: r.position)
        everyone = guild.default_role
        tier_ids = {newbie.id, speaker_role.id, moderated.id}
        rest = [r for r in current if r.id not in tier_ids and r.id != everyone.id]
        desired = [newbie, speaker_role, moderated] + rest
        await guild.edit_role_positions(
            positions={r: i + 1 for i, r in enumerate(desired)},
            reason="Three-tier member model — role ordering",
        )
        await guild.fetch_roles()
    except Exception as e:
        logger.error(f"Failed to reorder tier roles: {e}", exc_info=True)
        await client.close()
        sys.exit(1)

    logger.info("Done.")
    logger.info(f"  Newbie role id:    {newbie.id}")
    logger.info(f"  Moderated role id: {moderated.id}")
    logger.info(f"  Speaker role id:   {speaker_role.id}")
    logger.info("Add NEWBIE_ROLE_ID and MODERATED_ROLE_ID to .env with these IDs.")
    logger.info(
        "Verify order: Moderated (pos %d) > Speaker (pos %d) > Newbie (pos %d)",
        moderated.position, speaker_role.position, newbie.position,
    )
    await client.close()


client.run(TOKEN)
