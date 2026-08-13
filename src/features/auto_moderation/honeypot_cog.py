"""Honeypot: posting in the rules channel earns a 1-hour timed mute.

The rules channel is bait — the pinned rules text itself warns that posting
there gets an hour-long timeout ("honeypot for spammers"). Anyone holding a
tier role (Newbie or Speaker) who posts in the honeypot channel gets
Speaker/Newbie -> Moderated for 1 hour: a DM, a moderation-channel notice,
and an exact-time in-process restore (the 5-min check_expired_mutes loop
stays as a crash backstop). Staff (manage_messages / administrator /
moderate_members) are never trapped.

Disabled unless a guild opts in (server_config ``honeypot_enabled`` or env
``HONEYPOT_ENABLED``) AND speaker management is enabled — without speaker
management the restore backstop would not run.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

from src.common.speaker_mute import _parse_duration, mute_speaker_member, post_mute_to_moderation

logger = logging.getLogger('DiscordBot')

DEFAULT_HONEYPOT_DURATION = '1h'
HONEYPOT_DM_MESSAGE = (
    "You posted in the rules channel — that's a honeypot: any post there gets "
    "an hour-long timeout. Your speaking role has been removed for 1 hour and "
    "comes back automatically. If you have a question, ask in a support channel "
    "instead.\nYour message: {url}"
)


def is_honeypot_post(message, honeypot_channel_id: Optional[int]) -> bool:
    """Pure detector: a non-bot, non-staff Member posting in the honeypot
    channel (or a thread whose parent is it). Any content counts — the trap
    fires on the act of posting there.

    Testable without discord: relies only on attribute access.
    """
    if not honeypot_channel_id:
        return False
    if not getattr(message, 'guild', None):
        return False
    author = message.author
    if getattr(author, 'bot', False):
        return False
    if not hasattr(author, 'roles'):
        return False
    perms = getattr(author, 'guild_permissions', None)
    if perms is not None and (perms.manage_messages or perms.administrator or perms.moderate_members):
        return False
    channel = message.channel
    ids = {getattr(channel, 'id', None), getattr(channel, 'parent_id', None)}
    return honeypot_channel_id in ids


class HoneypotCog(commands.Cog):
    """Timeouts anyone who posts in the honeypot (rules) channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_handler = getattr(bot, 'db_handler', None)
        self.server_config = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        # (guild_id, member_id) with a trap in flight — guards the pre-await
        # window so two rapid posts can't both pass the "not already muted"
        # check.
        self._in_flight: set = set()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _is_speaker_management_enabled(self, guild_id: Optional[int]) -> bool:
        """Mirror AdminCog: speaker mute/permission enforcement is per-guild opt-in."""
        if guild_id is None:
            return False
        if self.server_config:
            server = self.server_config.get_server(guild_id)
            if server and server.get('speaker_management_enabled') is not None:
                return bool(server.get('speaker_management_enabled'))
            if guild_id == self.server_config.bndc_guild_id:
                return True
        return False

    def _is_enabled(self, guild_id: Optional[int]) -> bool:
        """Opt-in: honeypot_enabled AND speaker management (or restores can't run)."""
        if guild_id is None:
            return False
        if not self._is_speaker_management_enabled(guild_id):
            return False
        if self.server_config:
            server = self.server_config.get_server(guild_id)
            if server and server.get('honeypot_enabled') is not None:
                return bool(server.get('honeypot_enabled'))
        env = os.getenv('HONEYPOT_ENABLED')
        return bool(env and env.strip().lower() in ('1', 'true', 'yes', 'on'))

    def _get_channel_id(self, guild_id: int) -> Optional[int]:
        value = None
        if self.server_config:
            value = self.server_config.get_server_field(guild_id, 'honeypot_channel_id', cast=int)
        if value is None:
            env_value = os.getenv('HONEYPOT_CHANNEL_ID') or os.getenv('RULES_CHANNEL_ID')
            if env_value:
                try:
                    value = int(env_value)
                except ValueError:
                    value = None
        return value

    def _get_duration(self, guild_id: int) -> str:
        value = None
        if self.server_config:
            server = self.server_config.get_server(guild_id)
            if server:
                value = server.get('honeypot_duration')
        if not value:
            value = os.getenv('HONEYPOT_DURATION')
        return str(value or DEFAULT_HONEYPOT_DURATION)

    # ------------------------------------------------------------------
    # Role resolution (mirrors AdminCog._resolve_tier_roles)
    # ------------------------------------------------------------------

    def _resolve_tier_roles(self, guild) -> Optional[dict]:
        newbie_id = speaker_id = moderated_id = None
        if self.server_config:
            newbie_id = self.server_config.get_server_field(guild.id, 'newbie_role_id', cast=int)
            speaker_id = self.server_config.get_server_field(guild.id, 'speaker_role_id', cast=int)
            moderated_id = self.server_config.get_server_field(guild.id, 'moderated_role_id', cast=int)
        newbie_id = newbie_id or self._env_int('NEWBIE_ROLE_ID')
        speaker_id = speaker_id or self._env_int('SPEAKER_ROLE_ID')
        moderated_id = moderated_id or self._env_int('MODERATED_ROLE_ID')
        if not (newbie_id and speaker_id and moderated_id):
            return None
        newbie = guild.get_role(newbie_id)
        speaker = guild.get_role(speaker_id)
        moderated = guild.get_role(moderated_id)
        if not (newbie and speaker and moderated):
            return None
        return {'newbie': newbie, 'speaker': speaker, 'moderated': moderated}

    @staticmethod
    def _env_int(name: str) -> Optional[int]:
        value = os.getenv(name)
        return int(value) if value else None

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Never let the trap break the live message pipeline.
        try:
            await self._maybe_trap(message)
        except Exception as e:
            logger.error(f"HoneypotCog: on_message error for msg {getattr(message, 'id', '?')}: {e}", exc_info=True)

    async def _maybe_trap(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        member = message.author
        if (message.guild.id, member.id) in self._in_flight:
            return
        guild_id = message.guild.id
        if not self._is_enabled(guild_id):
            return

        honeypot_channel_id = self._get_channel_id(guild_id)
        if not is_honeypot_post(message, honeypot_channel_id):
            return

        roles = self._resolve_tier_roles(message.guild)
        if not roles:
            return
        # Duck-typed: a guild message author is always a Member (has .roles).
        member_roles = getattr(member, 'roles', [])
        if roles['moderated'] in member_roles:
            return  # already muted — can't post in the rules channel anyway
        if roles['speaker'] not in member_roles and roles['newbie'] not in member_roles:
            return  # no tier role — nothing to remove (and can't post there)

        self._in_flight.add((message.guild.id, member.id))
        try:
            await self._trap(message, roles)
        finally:
            self._in_flight.discard((message.guild.id, member.id))

    async def _trap(self, message: discord.Message, tier_roles: dict) -> None:
        """Timeout the author for the configured duration + DM + exact-time restore."""
        if message.guild is None:
            return
        duration = self._get_duration(message.guild.id)
        td = _parse_duration(duration)
        if td is None:
            logger.warning(f"HoneypotCog: invalid honeypot_duration {duration!r} — skipping")
            return
        mute_end_at = datetime.now(timezone.utc) + td
        reason = f"Posted in the rules channel (honeypot): {message.jump_url}"

        result = await mute_speaker_member(
            self.db_handler,
            guild=message.guild,
            member=message.author,
            tier_roles=tier_roles,
            reason=reason,
            actor_label='Honeypot guard',
            duration=duration,
            mute_end_at=mute_end_at,
            allow_update=False,
            invalidate_dm_cache=lambda mid: self._invalidate_dm_cache(mid),
        )
        if not result['success']:
            logger.warning(f"HoneypotCog: mute failed for {message.author.id}: {result.get('error')}")
            return
        if result['already_muted']:
            return

        await post_mute_to_moderation(
            self.bot,
            target_user_id=message.author.id,
            target_username=message.author.name,
            actor_user_id=None,
            actor_label='Honeypot guard',
            duration=duration,
            mute_end_at_iso=result.get('mute_end_at'),
            reason=reason,
        )
        await self._dm_notice(message.author, message.jump_url)

        # Exact-time restore; the check_expired_mutes loop covers bot restarts.
        asyncio.create_task(self._restore_after_mute(
            message.guild.id, message.author.id,
            mute_end_at, result.get('prior_status') or 'speaker',
            result.get('prior_can_message_bot'), tier_roles,
        ))
        logger.info(
            f"HoneypotCog: trapped {message.author.id} ({message.author.name}) "
            f"for {duration} — posted in rules channel (msg {message.id})"
        )

    async def _dm_notice(self, member, jump_url: str) -> None:
        try:
            await member.send(HONEYPOT_DM_MESSAGE.format(url=jump_url))
        except Exception as e:
            # Never break the mute flow over a DM failure (DMs closed, blocked).
            logger.info(f"HoneypotCog: DM to {member.id} failed: {e}")

    # ------------------------------------------------------------------
    # Exact-time restore
    # ------------------------------------------------------------------

    async def _restore_after_mute(
        self,
        guild_id: int,
        member_id: int,
        mute_end_at,
        prior_status: str,
        prior_can_message_bot: Optional[bool],
        tier_roles: dict,
    ) -> None:
        """Sleep until the mute expires, then restore IF the mute is still the
        active one (not superseded by a manual unmute, a longer re-mute, or a
        permanent mute). The 5-minute loop is the crash backstop for this task.
        """
        delay = (mute_end_at - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._restore_if_still_muted(
                guild_id, member_id, mute_end_at.isoformat(), prior_status,
                prior_can_message_bot, tier_roles,
            )
        except Exception as e:
            logger.error(f"HoneypotCog: restore task failed for {member_id}: {e}", exc_info=True)

    async def _restore_if_still_muted(
        self,
        guild_id: int,
        member_id: int,
        mute_end_at_iso: str,
        prior_status: str,
        prior_can_message_bot: Optional[bool],
        tier_roles: dict,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        member = guild.get_member(member_id)
        if not member:
            return
        if tier_roles['moderated'] not in member.roles:
            return  # already restored

        if self.db_handler:
            row = self.db_handler.get_timed_mute(member_id, guild_id)
            if row is None:
                return  # manual unmute or permanent conversion happened
            row_end = row.get('mute_end_at')
            if row_end:
                try:
                    drift = abs(
                        (datetime.fromisoformat(str(row_end).replace('Z', '+00:00'))
                         - datetime.fromisoformat(mute_end_at_iso.replace('Z', '+00:00'))).total_seconds()
                    )
                except (ValueError, TypeError):
                    drift = float('inf')
                if drift > 60:
                    return  # a different (re-)mute is active — leave it alone
            if self.db_handler.get_member_status(member_id, guild_id=guild_id) != 'moderated':
                return

        if self.db_handler:
            self.db_handler.set_member_status(member_id, guild_id, prior_status)
        await member.remove_roles(tier_roles['moderated'], reason="Honeypot timeout expired")
        restore_role = tier_roles['speaker'] if prior_status == 'speaker' else tier_roles['newbie']
        if restore_role not in member.roles:
            await member.add_roles(restore_role, reason="Honeypot timeout expired")
        if self.db_handler:
            if prior_can_message_bot is not None:
                self.db_handler.set_member_can_message_bot(member_id, prior_can_message_bot, username=member.name)
            self.db_handler.delete_timed_mute(member_id, guild_id)
        self._invalidate_dm_cache(member_id)
        logger.info(f"HoneypotCog: auto-restored {member_id} to {prior_status} after honeypot timeout")

    def _invalidate_dm_cache(self, member_id: int) -> None:
        """Best-effort: drop the AdminCog's 60s DM-access cache for this member."""
        for name in ('AdminCog', 'Admin'):
            cog = self.bot.get_cog(name)
            if cog is None:
                continue
            cache = getattr(cog, '_dm_access_cache', None)
            if cache is not None:
                cache.pop(member_id, None)
                return
