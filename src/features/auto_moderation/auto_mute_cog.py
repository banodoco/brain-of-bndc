"""Auto-mute guard: Speakers who post many image attachments with no text get a
short timed mute (Speaker -> Moderated) plus an explanatory DM.

Trigger: >= 4 image attachments (by content-type OR file extension) and no
non-whitespace text. Disabled unless a guild opts in (server_config
``auto_mute_enabled`` or env ``AUTO_MUTE_ENABLED``) AND speaker management is
enabled — without speaker management the ``check_expired_mutes`` loop would not
restore the tier, so the mute could be permanent.

The restore is exact-time (in-process task) with the 5-minute loop left as a
crash backstop. The offending message is NOT deleted — the mute only blocks
further posting.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import FrozenSet, Optional

import discord
from discord.ext import commands

from src.common.speaker_mute import _parse_duration, mute_speaker_member, post_mute_to_moderation

logger = logging.getLogger('DiscordBot')

DEFAULT_MIN_IMAGES = 4
DEFAULT_DURATION = '5m'
DEFAULT_DM_MESSAGE = (
    "Heads up: you just posted {count} images with no text, which reads as "
    "low-effort spam. As a nudge your Speaker role has been removed for "
    "{duration} — it comes back automatically. Next time, add a sentence or two "
    "of context (what you made, what you're asking). If you think this was a "
    "mistake, ping a mod in the server.\nYour message: {url}"
)

_IMAGE_EXTENSIONS = frozenset({
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'heic', 'heif', 'avif', 'apng',
})


def image_attachment_count(message) -> int:
    """Count attachments that are images, by content-type OR filename extension.

    Discord's ``content_type`` is often None on older/lazy-loaded attachments,
    so the extension fallback matters.
    """
    count = 0
    for att in (getattr(message, 'attachments', None) or []):
        ctype = (getattr(att, 'content_type', '') or '').lower()
        if ctype.startswith('image/'):
            count += 1
            continue
        filename = (getattr(att, 'filename', '') or '').lower()
        ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
        if ext in _IMAGE_EXTENSIONS:
            count += 1
    return count


def should_auto_mute(
    message,
    *,
    min_images: int = DEFAULT_MIN_IMAGES,
    exempt_channel_ids: FrozenSet[int] = frozenset(),
) -> bool:
    """Pure detector: guild, non-bot, Member author, staff-excluded, >=min
    image attachments, no text content, channel (or thread parent) not exempt.

    Testable without discord: relies only on attribute access. Non-Member
    authors (e.g. a User object in a DM-ish context) have no ``roles`` and are
    skipped defensively.
    """
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

    # Exempt the channel itself and, for threads/forums, the parent channel —
    # help/grant threads would otherwise false-positive.
    channel = message.channel
    channel_ids = {getattr(channel, 'id', None), getattr(channel, 'parent_id', None)}
    channel_ids.discard(None)
    if channel_ids & set(exempt_channel_ids):
        return False

    if (getattr(message, 'content', None) or '').strip():
        return False
    return image_attachment_count(message) >= min_images


class AutoMuteCog(commands.Cog):
    """Mutes Speakers who post many image attachments with no text."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_handler = getattr(bot, 'db_handler', None)
        self.server_config = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        # (guild_id, member_id) with an auto-mute in flight — guards the
        # pre-await window so two rapid posts can't both pass the
        # "not already moderated" check.
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
        """Opt-in: auto_mute_enabled AND speaker management (or restores can't run)."""
        if guild_id is None:
            return False
        if not self._is_speaker_management_enabled(guild_id):
            return False
        if self.server_config:
            server = self.server_config.get_server(guild_id)
            if server and server.get('auto_mute_enabled') is not None:
                return bool(server.get('auto_mute_enabled'))
        env = os.getenv('AUTO_MUTE_ENABLED')
        return bool(env and env.strip().lower() in ('1', 'true', 'yes', 'on'))

    def _get_exempt_channels(self, guild_id: int) -> FrozenSet[int]:
        raw = None
        if self.server_config:
            server = self.server_config.get_server(guild_id)
            if server:
                raw = server.get('auto_mute_exempt_channels')
        if raw is None:
            raw = os.getenv('AUTO_MUTE_EXEMPT_CHANNELS')
        return self._parse_channel_ids(raw) if raw else frozenset()

    @staticmethod
    def _parse_channel_ids(raw) -> FrozenSet[int]:
        """Accept a JSON list (jsonb), a Python list, or comma-separated ids."""
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith('['):
                try:
                    items = json.loads(text)
                except ValueError:
                    items = []
            else:
                items = [part for part in text.replace(',', ' ').split() if part.strip()]
        elif isinstance(raw, (list, tuple, set)):
            items = raw
        else:
            items = []
        out = set()
        for item in items:
            try:
                out.add(int(item))
            except (TypeError, ValueError):
                continue
        return frozenset(out)

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
        # Never let the guard break the live message pipeline — any failure here
        # is logged and swallowed.
        try:
            await self._maybe_auto_mute(message)
        except Exception as e:
            logger.error(f"AutoMuteCog: on_message error for msg {getattr(message, 'id', '?')}: {e}", exc_info=True)

    async def _maybe_auto_mute(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        member = message.author
        if (message.guild.id, member.id) in self._in_flight:
            return
        if not self._is_enabled(message.guild.id):
            return

        roles = self._resolve_tier_roles(message.guild)
        if not roles:
            return
        # Duck-typed: a guild message author is always a Member (has .roles),
        # but be defensive — a User has no roles and must not crash or mute.
        member_roles = getattr(member, 'roles', [])
        if roles['speaker'] not in member_roles:
            return
        if roles['moderated'] in member_roles:
            return

        exempt = self._get_exempt_channels(message.guild.id)
        if not should_auto_mute(message, exempt_channel_ids=exempt):
            return

        self._in_flight.add((message.guild.id, member.id))
        try:
            await self._auto_mute(message, roles)
        finally:
            self._in_flight.discard((message.guild.id, member.id))

    async def _auto_mute(self, message: discord.Message, tier_roles: dict) -> None:
        """Mute the author for the default duration + DM them, then schedule an
        exact-time restore (the 5-min loop stays as a crash backstop)."""
        if message.guild is None:
            return
        duration = DEFAULT_DURATION
        td = _parse_duration(duration)
        if td is None:
            logger.warning(f"AutoMuteCog: invalid default duration {duration!r} — skipping")
            return
        image_count = image_attachment_count(message)
        mute_end_at = datetime.now(timezone.utc) + td
        reason = f"Posted {image_count} images with no text (auto-mute guard): {message.jump_url}"

        result = await mute_speaker_member(
            self.db_handler,
            guild=message.guild,
            member=message.author,
            tier_roles=tier_roles,
            reason=reason,
            actor_label='Auto-mute guard',
            duration=duration,
            mute_end_at=mute_end_at,
            allow_update=False,
            invalidate_dm_cache=lambda mid: self._invalidate_dm_cache(mid),
        )
        if not result['success']:
            logger.warning(f"AutoMuteCog: mute failed for {message.author.id}: {result.get('error')}")
            return
        if result['already_muted']:
            # Already Moderated — nothing to do (guarded earlier, but keep the
            # race-safe path explicit).
            return

        await post_mute_to_moderation(
            self.bot,
            target_user_id=message.author.id,
            target_username=message.author.name,
            actor_user_id=None,
            actor_label='Auto-mute guard',
            duration=duration,
            mute_end_at_iso=result.get('mute_end_at'),
            reason=reason,
        )
        await self._dm_notice(message.author, image_count, message.jump_url)

        # Exact-time restore; the check_expired_mutes loop covers bot restarts.
        asyncio.create_task(self._restore_after_mute(
            message.guild.id, message.author.id,
            mute_end_at, result.get('prior_status') or 'speaker',
            result.get('prior_can_message_bot'), tier_roles,
        ))
        logger.info(
            f"AutoMuteCog: auto-muted {message.author.id} ({message.author.name}) "
            f"for {duration} — {image_count} images, no text (msg {message.id})"
        )

    async def _dm_notice(self, member, image_count: int, jump_url: str) -> None:
        try:
            await member.send(DEFAULT_DM_MESSAGE.format(count=image_count, duration=DEFAULT_DURATION, url=jump_url))
        except Exception as e:
            # Never break the mute flow over a DM failure (DMs closed, blocked).
            logger.info(f"AutoMuteCog: DM to {member.id} failed: {e}")

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
            logger.error(f"AutoMuteCog: restore task failed for {member_id}: {e}", exc_info=True)

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
        await member.remove_roles(tier_roles['moderated'], reason="Auto-mute expired")
        restore_role = tier_roles['speaker'] if prior_status == 'speaker' else tier_roles['newbie']
        if restore_role not in member.roles:
            await member.add_roles(restore_role, reason="Auto-mute expired")
        if self.db_handler:
            if prior_can_message_bot is not None:
                self.db_handler.set_member_can_message_bot(member_id, prior_can_message_bot, username=member.name)
            self.db_handler.delete_timed_mute(member_id, guild_id)
        self._invalidate_dm_cache(member_id)
        logger.info(f"AutoMuteCog: auto-restored {member_id} to {prior_status} after auto-mute")

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
