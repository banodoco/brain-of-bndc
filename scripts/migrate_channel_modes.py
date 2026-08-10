"""Migrate channel speaker_modes to the three-tier four-mode set and apply perms.

Modes: 'bot' (nobody), 'newbie' (Newbie+Speaker), 'community' (Speaker only),
'appeal' (Speaker+Moderated), 'admin' (admin role only). Applies per-channel
overwrites for @everyone +
Newbie + Speaker + Moderated via `apply_perms_to_channel`.

Channel mapping (server_config first, then env fallback):
    gate        -> bot
    introductions -> newbie
    grants forum  -> newbie
    help/support  -> newbie   (HELP_CHANNEL_ID, else default 1163250319107555388)
    moderation    -> appeal   (MODERATION_CHANNEL_ID, else default 1475121919484366962)
    everything else -> community

Usage:
    python scripts/migrate_channel_modes.py            # apply
    python scripts/migrate_channel_modes.py --dry-run  # report only, no writes
"""
import argparse
import asyncio
import os
import logging
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.db_handler import DatabaseHandler
from src.common.speaker_perms import apply_perms_to_channel

load_dotenv()

logger = logging.getLogger("MigrateModes")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

# Defaults from the current production server (see docs / .env).
DEFAULT_HELP_CHANNEL_ID = 1163250319107555388
DEFAULT_MODERATION_CHANNEL_ID = 1475121919484366962

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dry-run', action='store_true', help='Report what would change without writing anything')
args = parser.parse_args()

intents = discord.Intents.default()
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
        return

    db = DatabaseHandler()
    newbie_id = resolve_role_id(db, TARGET_GUILD_ID, 'newbie_role_id', 'NEWBIE_ROLE_ID')
    speaker_id = resolve_role_id(db, TARGET_GUILD_ID, 'speaker_role_id', 'SPEAKER_ROLE_ID')
    moderated_id = resolve_role_id(db, TARGET_GUILD_ID, 'moderated_role_id', 'MODERATED_ROLE_ID')
    if not newbie_id or not speaker_id or not moderated_id:
        logger.error("Need NEWBIE_ROLE_ID, SPEAKER_ROLE_ID, MODERATED_ROLE_ID before migrating modes.")
        await client.close()
        return

    guild = client.get_guild(TARGET_GUILD_ID)
    if not guild:
        logger.error(f"Guild {TARGET_GUILD_ID} not found")
        await client.close()
        return

    roles = {
        'everyone': guild.default_role,
        'newbie': guild.get_role(newbie_id),
        'speaker': guild.get_role(speaker_id),
        'moderated': guild.get_role(moderated_id),
    }
    if not all(roles.values()):
        logger.error("One or more tier roles not found in the guild.")
        await client.close()
        return

    special = {
        resolve_channel_id(db, TARGET_GUILD_ID, 'gate_channel_id', 'GATE_CHANNEL_ID'): 'bot',
        resolve_channel_id(db, TARGET_GUILD_ID, 'intro_channel_id', 'INTRO_CHANNEL_ID'): 'newbie',
        resolve_channel_id(db, TARGET_GUILD_ID, 'grants_channel_id', 'GRANTS_CHANNEL_ID'): 'newbie',
        resolve_channel_id(db, TARGET_GUILD_ID, 'help_channel_id', 'HELP_CHANNEL_ID', DEFAULT_HELP_CHANNEL_ID): 'newbie',
        resolve_channel_id(db, TARGET_GUILD_ID, 'moderation_channel_id', 'MODERATION_CHANNEL_ID', DEFAULT_MODERATION_CHANNEL_ID): 'appeal',
    }
    special = {cid: mode for cid, mode in special.items() if cid}

    channels = [c for c in guild.channels
                if isinstance(c, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel))]

    updated = 0
    skipped = 0
    errors = 0
    for channel in channels:
        mode = special.get(channel.id, 'community')
        if args.dry_run:
            # Dry-run must NOT touch Discord permissions or the DB.
            logger.info(f"  [dry-run] would set mode={mode} on #{channel.name} ({channel.id})")
            updated += 1
            continue
        try:
            db.set_channel_speaker_mode(channel.id, mode, guild_id=TARGET_GUILD_ID)
        except Exception as e:
            errors += 1
            logger.error(f"  DB error for #{channel.name} ({channel.id}): {e}")
            continue
        try:
            changed, api_calls = await apply_perms_to_channel(channel, roles, mode)
            if changed:
                updated += 1
                logger.info(f"  set mode={mode} on #{channel.name} ({channel.id}), api_calls={api_calls}")
            else:
                skipped += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            errors += 1
            logger.error(f"  Failed for #{channel.name} ({channel.id}): {e}")

    logger.info(f"Done! {'Would update' if args.dry_run else 'Updated'}: {updated}, Skipped: {skipped}, Errors: {errors}")
    if not args.dry_run:
        logger.info(
            "Runbook: 1) apply the Supabase SQL migration, 2) deploy the new bot code, "
            "3) this channel-mode migration should run AFTER deploy (so the old bot's loop "
            "doesn't fight it), 4) run scripts/sense_check_three_tier.py as the pre-live gate."
        )
    await client.close()


client.run(TOKEN)
