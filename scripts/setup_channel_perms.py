"""Set channel permission overwrites for the Speaker role system.

Reads speaker_mode from the database for each channel.  Falls back to the
SPEAKER_EXEMPT_CHANNELS env var for backward compatibility.
"""
import asyncio
import os
import sys
import logging
from pathlib import Path

import discord
from dotenv import load_dotenv

# Add project root so we can import src.common
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.speaker_perms import apply_perms_to_channel
from src.common.db_handler import DatabaseHandler

load_dotenv()

logger = logging.getLogger("ChannelPerms")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not TARGET_GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        return

    db = DatabaseHandler()

    def resolve_role_id(field: str, env_var: str) -> int | None:
        sc = getattr(db, 'server_config', None)
        if sc:
            val = sc.get_server_field(TARGET_GUILD_ID, field, cast=int)
            if val:
                return val
        env_value = os.getenv(env_var)
        return int(env_value) if env_value else None

    speaker_role_id = resolve_role_id('speaker_role_id', 'SPEAKER_ROLE_ID')
    newbie_role_id = resolve_role_id('newbie_role_id', 'NEWBIE_ROLE_ID')
    moderated_role_id = resolve_role_id('moderated_role_id', 'MODERATED_ROLE_ID')
    if not speaker_role_id or not newbie_role_id or not moderated_role_id:
        logger.error("Need SPEAKER_ROLE_ID, NEWBIE_ROLE_ID, MODERATED_ROLE_ID before applying perms.")
        await client.close()
        return

    guild = client.get_guild(TARGET_GUILD_ID)
    if not guild:
        logger.error(f"Guild {TARGET_GUILD_ID} not found")
        await client.close()
        return

    roles = {
        'everyone': guild.default_role,
        'newbie': guild.get_role(newbie_role_id),
        'speaker': guild.get_role(speaker_role_id),
        'moderated': guild.get_role(moderated_role_id),
    }
    if not all(roles.values()):
        logger.error("One or more tier roles not found in the guild.")
        await client.close()
        return

    # Load channel modes from DB (normalized to the four-mode set)
    modes = {}
    try:
        modes = db.get_all_channel_speaker_modes()
        logger.info(f"Loaded {len(modes)} channel modes from DB")
    except Exception as e:
        logger.warning(f"Could not load modes from DB, using defaults only: {e}")

    logger.info(f"Setting permissions on channels in {guild.name}...")

    updated = 0
    skipped = 0
    errors = 0

    for channel in guild.channels:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            skipped += 1
            continue

        mode = modes.get(channel.id) or 'community'

        try:
            changed, api_calls = await apply_perms_to_channel(channel, roles, mode)
            if changed:
                updated += 1
                logger.info(f"  Applied mode={mode} to #{channel.name} ({channel.id}), api_calls={api_calls}")
            else:
                skipped += 1
            if updated % 20 == 0 and updated > 0:
                logger.info(f"  Progress: {updated} channels updated")
            await asyncio.sleep(0.5)
        except Exception as e:
            errors += 1
            logger.error(f"  Failed for #{channel.name} ({channel.id}): {e}")

    logger.info(f"Done! Updated: {updated}, Skipped: {skipped}, Errors: {errors}")
    await client.close()


client.run(TOKEN)
