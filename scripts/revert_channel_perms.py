"""Remove the Speaker role channel overwrites and restore @everyone send permissions.

WARNING: this is now an INCOMPLETE rollback for the three-tier model. It only
touches the @everyone and Speaker overwrites; it leaves the Newbie/Moderated
overwrites (and their pin_messages fields, added with forum-only pinning) in
place, so channels end up half-reverted. The bot's 30-minute
`enforce_channel_permissions` reconciliation will also re-apply the managed
tier perms on its next pass. Prefer updating a channel's speaker_mode in the DB
(or /migrate_channel_modes.py) over this script.
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

from src.common.db_handler import DatabaseHandler

load_dotenv()

logger = logging.getLogger("RevertPerms")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None
EXEMPT_CHANNELS = {int(x.strip()) for x in os.getenv("SPEAKER_EXEMPT_CHANNELS", "").split(",") if x.strip()}

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


def get_speaker_role_id(db: DatabaseHandler, guild_id: int | None) -> int | None:
    if guild_id is None:
        return None
    sc = getattr(db, 'server_config', None)
    if sc:
        role_id = sc.get_server_field(guild_id, 'speaker_role_id', cast=int)
        if role_id:
            return role_id
    env_value = os.getenv("SPEAKER_ROLE_ID")
    return int(env_value) if env_value else None


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not TARGET_GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        return

    db = DatabaseHandler()
    speaker_role_id = get_speaker_role_id(db, TARGET_GUILD_ID)
    guild = client.get_guild(TARGET_GUILD_ID)
    if not guild:
        logger.error(f"Guild {TARGET_GUILD_ID} not found")
        await client.close()
        return

    role = guild.get_role(speaker_role_id) if speaker_role_id else None
    if not role:
        logger.error(f"Speaker role {speaker_role_id} not found")
        await client.close()
        return

    logger.info(f"Reverting channel permissions in {guild.name}...")

    reverted = 0
    skipped = 0

    for channel in guild.channels:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            skipped += 1
            continue

        if channel.id in EXEMPT_CHANNELS:
            logger.info(f"  Skipping exempt channel #{channel.name} ({channel.id})")
            skipped += 1
            continue

        try:
            # Reset @everyone send perms back to None (inherit)
            everyone_overwrite = channel.overwrites_for(guild.default_role)
            everyone_overwrite.send_messages = None
            everyone_overwrite.send_messages_in_threads = None
            everyone_overwrite.create_public_threads = None
            everyone_overwrite.create_private_threads = None
            everyone_overwrite.add_reactions = None

            await channel.set_permissions(
                guild.default_role, overwrite=everyone_overwrite,
                reason="Revert Speaker role — restore @everyone send perms",
            )

            # Remove Speaker role overwrite entirely
            if role in channel.overwrites:
                await channel.set_permissions(
                    role, overwrite=None,
                    reason="Revert Speaker role — remove Speaker overwrite",
                )

            reverted += 1
            if reverted % 20 == 0:
                logger.info(f"  Progress: {reverted} channels reverted")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"  Failed for #{channel.name} ({channel.id}): {e}")

    logger.info(f"Done! Reverted: {reverted}, Skipped: {skipped}")
    await client.close()


client.run(TOKEN)
