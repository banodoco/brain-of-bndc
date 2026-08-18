"""Shared speaker-mute core used by /mute, the admin-chat mute_speaker tool, and
the auto-mute guard.

Owns everything a mute needs so no caller has to import admin_cog:
- ``mute_speaker_member`` — the tier swap (Speaker/Newbie -> Moderated),
  prior-status snapshot, DM-access revocation, and timed-mute record.
- ``post_mute_to_moderation`` — the moderation-channel notice.
- ``_parse_duration`` — "5m"/"1h"/"7d"/"2w" parsing.

Each caller keeps its own role resolution, response plumbing, and DM/notice
decisions. ``admin_cog`` re-exports these names so existing imports and test
patches (``admin_cog.post_mute_to_moderation``) keep working.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

import discord

logger = logging.getLogger('DiscordBot')


def _parse_duration(duration_str: str) -> Optional[timedelta]:
    """Parse a duration string like '5m', '7d', '24h', '2w' into a timedelta.

    Returns None if the string is not a valid duration.
    """
    match = re.fullmatch(r'(\d+)(m|h|d|w)', duration_str.strip().lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)
    elif unit == 'w':
        return timedelta(weeks=value)
    return None


# Moderation log channel for mute notices. Override with MODERATION_CHANNEL_ID env var.
_DEFAULT_MODERATION_CHANNEL_ID = 1475121919484366962


def _get_moderation_channel_id() -> Optional[int]:
    raw = os.getenv('MODERATION_CHANNEL_ID')
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(f"Invalid MODERATION_CHANNEL_ID env var: {raw!r}")
    return _DEFAULT_MODERATION_CHANNEL_ID


async def post_mute_to_moderation(
    bot,
    *,
    target_user_id: int,
    target_username: str,
    actor_user_id: Optional[int],
    actor_label: str,
    duration: Optional[str],
    mute_end_at_iso: Optional[str],
    reason: str,
    channel_id: Optional[int] = None,
) -> bool:
    """Post a mute notice to the moderation channel. Returns True on success.

    Never raises — failures are logged and reported via the bool return value so
    the calling mute action is never short-circuited by a logging hiccup.
    """
    try:
        channel_id = channel_id or _get_moderation_channel_id()
        if not channel_id:
            return False
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                logger.error(f"Moderation channel {channel_id} not found")
                return False
            except discord.Forbidden:
                logger.error(f"Bot lacks access to moderation channel {channel_id}")
                return False
            except discord.HTTPException as e:
                logger.error(f"Could not fetch moderation channel {channel_id}: {e}")
                return False

        actor_part = f"<@{actor_user_id}>" if actor_user_id else actor_label
        if duration:
            duration_part = f"for **{duration}**"
            if mute_end_at_iso:
                try:
                    ts = int(datetime.fromisoformat(mute_end_at_iso.replace('Z', '+00:00')).timestamp())
                    duration_part += f" — unmute <t:{ts}:R>"
                except (ValueError, TypeError):
                    pass
        else:
            duration_part = "**permanently**"

        content = (
            f"🔇 **Speaker muted**\n"
            f"User: <@{target_user_id}> ({target_username})\n"
            f"By: {actor_part}\n"
            f"Duration: {duration_part}\n"
            f"Reason: {reason}"
        )

        # Forum channels can't accept plain messages — they require a thread.
        if isinstance(channel, discord.ForumChannel):
            thread_name = f"Mute: {target_username} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            # Forum thread names are capped at 100 chars by Discord.
            thread_name = thread_name[:100]
            try:
                await channel.create_thread(
                    name=thread_name,
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except discord.HTTPException as e:
                logger.error(f"Failed to create forum thread in moderation channel: {e}", exc_info=True)
                return False

        # Regular text channel / thread / news channel — plain send works.
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException as e:
            logger.error(f"Failed to post mute notice to moderation channel: {e}", exc_info=True)
            return False
    except Exception as e:
        logger.error(f"Unexpected error posting mute notice to moderation channel: {e}", exc_info=True)
        return False


async def mute_speaker_member(
    db_handler,
    *,
    guild,
    member,
    tier_roles: dict,
    reason: str,
    actor_label: str,
    actor_user_id: Optional[int] = None,
    duration: Optional[str] = None,
    mute_end_at=None,
    allow_update: bool = False,
    rollback_on_timer_failure: bool = False,
    invalidate_dm_cache: Optional[Callable[[int], None]] = None,
) -> Dict:
    """Move a member to the Moderated tier and record a timed mute.

    Swap whichever tier role the member holds (Speaker/Newbie) -> Moderated,
    snapshot their prior tier + DM-to-bot access, revoke DM access, and upsert a
    ``timed_mutes`` row when ``mute_end_at`` is given.

    Args:
        db_handler: DatabaseHandler, or None (DB writes skipped).
        guild: The guild the member belongs to.
        member: The member to mute.
        tier_roles: Dict with 'newbie', 'speaker', 'moderated' role objects
            (resolved by the caller so each caller keeps its own resolution).
        reason: Mute reason (posted to the moderation channel by the caller).
        actor_label: Human-readable actor ("admin 123", "Auto-mute guard").
        actor_user_id: Discord ID of the actor, if any.
        duration: Human duration string ("5m", "7d") for the audit trail.
        mute_end_at: When the timed mute expires (UTC). None = permanent.
        allow_update: When the member is already Moderated — True updates their
            timed mute (preserving the ORIGINAL prior-status snapshot), False
            leaves everything untouched and reports ``already_muted``.
        rollback_on_timer_failure: When a timed mute (``mute_end_at`` given) is
            requested but the DB record cannot be created, undo the whole mute
            (status, roles, DM access) instead of leaving the member Moderated
            with no restore path. Default False keeps legacy /mute behavior
            (mute stands, admin unmutes manually).
        invalidate_dm_cache: Optional callback to drop per-user DM-access cache
            entries (each cog owns its own cache).

    Returns:
        Dict with ``success``, ``was_already_muted``, ``already_muted``
        (True when no change was made because the member is already muted and
        ``allow_update`` is False), ``mute_end_at`` (ISO string or None),
        ``timed_mute_scheduled``, and the snapshotted ``prior_status`` /
        ``prior_can_message_bot`` (for exact-time restores). Never raises —
        Discord/DB failures are reported via ``success=False`` / ``error``.
    """
    newbie_role = tier_roles['newbie']
    speaker_role = tier_roles['speaker']
    moderated_role = tier_roles['moderated']

    result: Dict = {
        'success': True,
        'was_already_muted': False,
        'already_muted': False,
        'mute_end_at': None,
        'timed_mute_scheduled': False,
        'prior_status': None,
        'prior_can_message_bot': None,
        'error': None,
    }

    member_id = member.id
    guild_id = guild.id

    was_already_muted = moderated_role in member.roles
    if not was_already_muted and db_handler:
        was_already_muted = db_handler.get_member_status(member_id, guild_id=guild_id) == 'moderated'
    result['was_already_muted'] = was_already_muted

    if was_already_muted and not allow_update:
        result.update(already_muted=True)
        return result

    # Snapshot the tier to restore on expiry. When re-muting an already-muted
    # member, preserve the ORIGINAL snapshot so the restore target doesn't
    # drift across repeated mutes.
    prior_status = 'speaker'
    prior_can_message_bot = None
    if db_handler:
        if was_already_muted:
            existing = db_handler.get_guild_member(member_id, guild_id)
            prior_status = (existing or {}).get('prior_status') or 'speaker'
            if prior_status not in ('newbie', 'speaker'):
                prior_status = 'speaker'
            prior_can_message_bot = (existing or {}).get('prior_can_message_bot')
        else:
            prior_status = db_handler.get_member_status(member_id, guild_id=guild_id)
            if prior_status not in ('newbie', 'speaker'):
                prior_status = 'speaker'
            prior_can_message_bot = db_handler.get_member_can_message_bot(member_id)
    result['prior_status'] = prior_status
    result['prior_can_message_bot'] = prior_can_message_bot

    audit_reason = f"Muted by {actor_label}: {reason}" + (f" (for {duration})" if duration else "")
    status_written = False

    async def _rollback(reason_note: str) -> None:
        """Best-effort full undo: DB status, DM access, and tier roles."""
        try:
            if db_handler:
                db_handler.set_member_status(member_id, guild_id, prior_status)
                if prior_can_message_bot is not None:
                    db_handler.set_member_can_message_bot(member_id, prior_can_message_bot, username=member.name)
            if moderated_role in member.roles:
                await member.remove_roles(moderated_role, reason="Mute rolled back")
            restore_role = speaker_role if prior_status == 'speaker' else newbie_role
            if restore_role not in member.roles:
                await member.add_roles(restore_role, reason="Mute rolled back")
            logger.info(f"[speaker_mute] Rolled back mute for {member_id} ({reason_note})")
        except Exception as rollback_err:
            logger.error(f"[speaker_mute] Rollback for {member_id} failed: {rollback_err}", exc_info=True)

    try:
        # Mark status first so on_member_update (fired by the role changes)
        # sees 'moderated' and doesn't re-add a tier role mid-swap.
        if db_handler:
            db_handler.set_member_status(
                member_id, guild_id, 'moderated',
                prior_status=prior_status, set_prior=not was_already_muted,
            )
            status_written = True

        roles_to_remove = [r for r in (newbie_role, speaker_role) if r in member.roles]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=audit_reason[:512])
        if moderated_role not in member.roles:
            await member.add_roles(moderated_role, reason=audit_reason[:512])

        # Revoke DM-to-bot access (appeals go through the moderation channel).
        if db_handler:
            db_handler.set_member_can_message_bot(member_id, False, username=member.name)
        if invalidate_dm_cache is not None:
            invalidate_dm_cache(member_id)

        if mute_end_at is not None and db_handler:
            mute_end_iso = mute_end_at.isoformat()
            result['mute_end_at'] = mute_end_iso
            result['timed_mute_scheduled'] = db_handler.create_timed_mute(
                member_id=member_id,
                guild_id=guild_id,
                mute_end_at=mute_end_iso,
                reason=reason,
                muted_by_id=actor_user_id,
                prior_status=prior_status,
                prior_can_message_bot=prior_can_message_bot,
            )
            if not result['timed_mute_scheduled'] and rollback_on_timer_failure:
                # Roles + status committed but no restore row — without it the
                # member is stuck Moderated forever (exact restore and the
                # 5-min loop both no-op). Undo the whole mute.
                await _rollback("timed-mute record failed")
                result.update(success=False, error="Failed to record the timed mute — mute rolled back.")
                return result
        elif was_already_muted and mute_end_at is None and db_handler:
            # Converting an existing timed mute back to permanent: clear the timer.
            db_handler.delete_timed_mute(member_id, guild_id)

        logger.info(
            f"[speaker_mute] {'updated mute on' if was_already_muted else 'muted'} "
            f"{member_id} ({member.name}) by {actor_label}"
            + (f" for {duration}" if duration else " permanently")
            + f" — reason: {reason}"
        )
        return result
    except Exception as e:
        logger.error(f"[speaker_mute] Error muting {member_id}: {e}", exc_info=True)
        # The role swap failed after the DB status was written — roll the mute
        # back so the member isn't stuck 'moderated' with no timed mute (which
        # would otherwise need a manual /unmute to clear).
        if status_written:
            await _rollback("mute failed after status write")
        if isinstance(e, discord.Forbidden):
            result.update(success=False, error="I don't have permission to change that user's roles.")
        else:
            result.update(success=False, error=str(e))
        return result
