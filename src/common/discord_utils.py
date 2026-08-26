import asyncio
from dataclasses import dataclass
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Union

import discord
from discord.ext import commands

from src.common.rate_limiter import RateLimiter



def split_message(text: str, limit: int = 2000) -> List[str]:
    """Split text into chunks under Discord's message length cap.

    Prefers paragraph breaks (blank lines), then single line breaks,
    then hard slices — so replies break at natural boundaries instead
    of mid-sentence. Fence-aware: tracks ``` blocks (lines starting
    with ```), avoids splitting inside a fence; if a fence block alone
    exceeds the limit it is force-split with closing ``` and reopening
    ``` so each chunk remains valid markdown. Guard covers code block +
    long text co-occurrence.
    """
    if len(text) <= limit:
        return [text] if text.strip() else []

    def _is_fence(line: str) -> bool:
        return line.lstrip().startswith("```")

    # Decompose into fence blocks vs plain-text blocks preserving order.
    # A fence block is from an opening ``` line through its matching closing
    # ``` line (or EOF if unclosed, treated as fenced).
    lines = text.split("\n")
    blocks: List[tuple] = []  # (kind, block_text) kind in ("text", "fence")
    cur_lines: List[str] = []
    in_fence = False
    for line in lines:
        is_fence = _is_fence(line)
        if is_fence:
            if not in_fence:
                if cur_lines:
                    blocks.append(("text", "\n".join(cur_lines)))
                    cur_lines = []
                cur_lines.append(line)
                in_fence = True
            else:
                cur_lines.append(line)
                blocks.append(("fence", "\n".join(cur_lines)))
                cur_lines = []
                in_fence = False
        else:
            cur_lines.append(line)
    if cur_lines:
        blocks.append(("fence" if in_fence else "text", "\n".join(cur_lines)))

    # Turn each block into packable units; fence blocks stay atomic unless
    # they alone exceed the limit (then force-split with fence wrappers).
    all_units: List[str] = []
    for kind, block in blocks:
        if kind == "text":
            if not block.strip():
                continue
            paragraphs = block.split("\n\n")
            for para in paragraphs:
                if len(para) <= limit:
                    all_units.append(para)
                    continue
                for line in para.split("\n"):
                    if len(line) <= limit:
                        all_units.append(line)
                        continue
                    for i in range(0, len(line), limit):
                        all_units.append(line[i:i + limit])
        else:
            # fence block
            if len(block) <= limit:
                all_units.append(block)
                continue
            # Force-split: inner content sliced with wrappers so each
            # chunk has balanced fences. Prefer \n\n / \n boundaries
            # inside the fence when possible, else hard slice.
            blk_lines = block.split("\n")
            opening = blk_lines[0]
            # Detect closing line
            if blk_lines[-1].lstrip().startswith("```"):
                closing = blk_lines[-1]
                inner = "\n".join(blk_lines[1:-1])
            else:
                closing = "```"
                inner = "\n".join(blk_lines[1:])

            # If inner is empty (empty fence), just slice the block
            if not inner:
                for i in range(0, len(block), max(1, limit - 8)):
                    all_units.append(block[i:i+limit])
                continue

            # Slice inner into slices that fit when wrapped
            def _avail(is_first: bool) -> int:
                op = opening if is_first else "```"
                # op + "\n" + slice + "\n" + closing <= limit
                return limit - len(op) - 1 - 1 - len(closing)

            # Use a simple boundary-preferring splitter for inner
            def _split_inner(s: str, avail: int) -> List[str]:
                if len(s) <= avail:
                    return [s]
                parts: List[str] = []
                rem = s
                while rem:
                    if len(rem) <= avail:
                        parts.append(rem)
                        break
                    window = rem[:avail]
                    # prefer \n\n then \n
                    cut = -1
                    idx = window.rfind("\n\n")
                    if idx > avail // 3:
                        cut = idx
                    else:
                        idx2 = window.rfind("\n")
                        if idx2 > avail // 3:
                            cut = idx2
                    if cut == -1:
                        cut = avail
                    # avoid empty slice due to leading newlines
                    slice_text = rem[:cut].rstrip("\n")
                    if not slice_text.strip():
                        cut = avail
                        slice_text = rem[:cut]
                    parts.append(slice_text)
                    rem = rem[cut:].lstrip("\n")
                    # avail may differ for subsequent slices (use avail_rest)
                    # caller handles avail change; for inner helper we keep same avail
                return parts

            # Iteratively carve inner so first slice uses opening-sized avail
            remaining = inner
            is_first = True
            slices: List[str] = []
            while remaining:
                avail = _avail(is_first)
                if avail < 10:
                    avail = 10
                if len(remaining) <= avail:
                    slices.append(remaining)
                    break
                # Find preferred cut within avail
                window = remaining[:avail]
                cut = -1
                idx = window.rfind("\n\n")
                if idx > avail // 3:
                    cut = idx
                else:
                    idx2 = window.rfind("\n")
                    if idx2 > avail // 3:
                        cut = idx2
                if cut == -1:
                    cut = avail
                slice_text = remaining[:cut].rstrip("\n")
                if not slice_text.strip():
                    cut = avail
                    slice_text = remaining[:cut]
                slices.append(slice_text)
                remaining = remaining[cut:].lstrip("\n")
                is_first = False

            for idx, sl in enumerate(slices):
                op = opening if idx == 0 else "```"
                all_units.append(f"{op}\n{sl}\n{closing}")

    # Pack units into final chunks, never exceeding limit. A unit is
    # already <= limit (fence slices were wrapped to fit), so packing
    # just needs candidate length check. We avoid joining with \n\n
    # across fence boundaries that would break balance — but since each
    # fence unit already has balanced fences, concatenation stays balanced.
    chunks: List[str] = []
    current = ""
    for unit in all_units:
        if not unit.strip():
            continue
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # unit already <= limit by construction; if still > limit (defensive)
        if len(unit) > limit:
            for i in range(0, len(unit), limit):
                chunks.append(unit[i:i+limit])
            current = ""
        else:
            current = unit
    if current:
        chunks.append(current)

    # Defensive: ensure each chunk is balanced (even fence count); if
    # somehow we still produced an odd trailing fence (e.g. unclosed
    # input), close it. This is the co-occurrence guard for code block
    # + long text where an earlier heuristic might leave a chunk open.
    balanced: List[str] = []
    for chunk in chunks:
        fence_lines = sum(1 for l in chunk.split("\n") if l.lstrip().startswith("```"))
        if fence_lines % 2 == 1:
            # unbalanced — close at end of chunk
            if len(chunk) + 4 <= limit:
                chunk = chunk.rstrip() + "\n```"
            else:
                # need to make room; trim a little then close
                chunk = chunk[: limit - 4].rstrip() + "\n```"
        balanced.append(chunk)

    # If still balanced but original ended inside fence and we sliced
    # without reopening (should not happen after block logic), fix by
    # ensuring fence count parity across the sequence: if a chunk was
    # closed artificially, next chunk should already have been reopened
    # via the forced-split path. No further fix needed.

    return [c for c in (chunk.strip() for chunk in balanced) if c]



def emoji_to_str(emoji) -> str:
    """Convert a discord emoji to a string representation.

    Unicode emoji → char string, custom emoji → 'name:id'.
    """
    if hasattr(emoji, 'id') and emoji.id:
        return f"{emoji.name}:{emoji.id}"
    return str(emoji)
from src.common.error_handler import handle_errors

@handle_errors("safe_send_message")
async def safe_send_message(
    bot: Union[discord.Client, commands.Bot],
    channel: discord.abc.Messageable,
    rate_limiter: RateLimiter,
    logger: logging.Logger,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    file: Optional[discord.File] = None,
    files: Optional[List[discord.File]] = None,
    reference: Optional[Union[discord.Message, discord.MessageReference, discord.PartialMessage]] = None,
    view: Optional[discord.ui.View] = None
) -> Optional[discord.Message]:
    """
    Safely sends a message to a given channel, handling rate limits and common errors.

    Args:
        bot: The bot instance (for admin error notification via @handle_errors).
        channel: The Discord channel, thread, or user to send the message to.
        rate_limiter: An instance of the RateLimiter.
        logger: A logger instance for logging specific operational details.
        content: The content of the message.
        embed: The embed to send with the message.
        file: A file to send with the message.
        files: A list of files to send with the message.
        reference: A message to reply to.
        view: A discord.ui.View to send with the message.

    Returns:
        The sent discord.Message object if successful, None otherwise.
        Raises exceptions on failure after retries or for critical errors.
    """
    try:
        if rate_limiter:
            # Key for rate limiting can be channel.id
            # The rate_limiter now expects a factory
            coroutine_factory = lambda: channel.send(
                content=content, embed=embed, file=file, files=files, reference=reference, view=view
            )
            return await rate_limiter.execute(channel.id, coroutine_factory)
        else:
            # Fallback if no rate limiter is provided (though generally expected)
            logger.warning(f"Sending message to {getattr(channel, 'name', channel.id)} without a rate limiter.")
            send_task = channel.send(
                content=content, embed=embed, file=file, files=files, reference=reference, view=view
            )
            return await asyncio.wait_for(send_task, timeout=30)

    except discord.HTTPException as e:
        # Logged by @handle_errors, but specific logging here can be useful too.
        logger.error(f"HTTP error during safe_send_message to {getattr(channel, 'name', channel.id)}: {e.status} {e.text}")
        raise # Re-raise for @handle_errors and further up the call stack
    except asyncio.TimeoutError:
        # Only for the direct asyncio.wait_for in the else block
        logger.error(f"Timeout during direct safe_send_message to {getattr(channel, 'name', channel.id)} (no rate_limiter).")
        raise # Re-raise
    except (OSError, ConnectionError, TimeoutError) as e:
        # Handle network connectivity issues
        logger.error(f"Network connectivity error during safe_send_message to {getattr(channel, 'name', channel.id)}: {e}")
        raise
    except Exception as e:
        # Catch any other unexpected error during the send logic itself.
        # @handle_errors will catch this too if it's not caught here.
        logger.error(f"Unexpected error in safe_send_message logic for {getattr(channel, 'name', channel.id)}: {e}", exc_info=True)
        raise # Re-raise


async def refresh_media_url(
    bot: Union[discord.Client, commands.Bot],
    channel_id: int,
    message_id: int,
    logger: Optional[logging.Logger] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetch a message from Discord API to get fresh attachment URLs.
    
    Discord CDN URLs expire after a period of time. This function fetches the
    message from the Discord API to get current, non-expired URLs for all attachments.
    
    Args:
        bot: The Discord bot client
        channel_id: The channel ID where the message is located
        message_id: The message ID to refresh
        logger: Optional logger instance
        
    Returns:
        Dict containing:
            - 'message_id': The message ID
            - 'channel_id': The channel ID  
            - 'attachments': List of attachment dicts with refreshed URLs
            - 'success': True if URLs were refreshed
        None if the message could not be fetched
    """
    log = logger or logging.getLogger('DiscordBot')
    
    try:
        # Get channel
        channel = bot.get_channel(channel_id)
        if not channel:
            log.debug(f"Channel {channel_id} not in cache, fetching...")
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                log.error(f"Channel {channel_id} not found")
                return None
            except discord.Forbidden:
                log.error(f"No permission to access channel {channel_id}")
                return None
        
        # Handle ForumChannel - need to find the thread containing the message
        if isinstance(channel, discord.ForumChannel):
            log.debug(f"Channel {channel_id} is a ForumChannel, searching for thread...")
            # For forum posts, try fetching the thread directly using message_id as thread_id
            # (forum post threads often have the same ID as their starter message)
            try:
                thread = await bot.fetch_channel(message_id)
                if isinstance(thread, discord.Thread):
                    channel = thread
                    log.debug(f"Found thread {message_id} in forum channel")
                else:
                    log.error(f"Could not find thread for message {message_id} in forum {channel_id}")
                    return None
            except discord.NotFound:
                log.warning(f"Thread/message {message_id} not found in forum {channel_id}")
                return None
            except discord.Forbidden:
                log.error(f"No permission to access thread {message_id}")
                return None
        elif not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.error(f"Channel {channel_id} is not a text channel, thread, or forum")
            return None
        
        # Fetch message
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            log.warning(f"Message {message_id} not found in channel {channel_id}")
            return None
        except discord.Forbidden:
            log.error(f"No permission to fetch message {message_id}")
            return None
        
        # Extract fresh attachment data
        fresh_attachments = []
        for att in message.attachments:
            fresh_attachments.append({
                'id': att.id,
                'filename': att.filename,
                'url': att.url,
                'proxy_url': att.proxy_url,
                'size': att.size,
                'content_type': att.content_type,
                'height': att.height,
                'width': att.width,
            })
        
        log.info(f"Refreshed {len(fresh_attachments)} attachment URL(s) for message {message_id}")
        
        return {
            'message_id': message_id,
            'channel_id': channel_id,
            'attachments': fresh_attachments,
            'success': True
        }
        
    except Exception as e:
        log.error(f"Error refreshing media URLs for message {message_id}: {e}", exc_info=True)
        return None


async def update_no_sharing_role(
    bot: Union[discord.Client, commands.Bot],
    member_id: int,
    allow_sharing: bool,
    logger: Optional[logging.Logger] = None,
    guild_id: Optional[int] = None,
) -> bool:
    """
    Add or remove the "no sharing" role based on user's content sharing preference.
    
    When allow_sharing is False, adds the NO_SHARING_ROLE_ID role to make opt-out visible.
    When allow_sharing is True, removes that role.
    
    Args:
        bot: The Discord bot client
        member_id: Discord member ID to update
        allow_sharing: Whether the user allows content sharing (True = remove role, False = add role)
        logger: Optional logger instance
        
    Returns:
        True if role was successfully updated (or no role configured), False on error
    """
    log = logger or logging.getLogger('DiscordBot')
    
    try:
        sc = getattr(getattr(bot, 'db_handler', None), 'server_config', None)

        def _resolve_role_id(for_guild_id: int) -> Optional[int]:
            if sc:
                role_id = sc.get_server_field(for_guild_id, 'no_sharing_role_id', cast=int)
                if role_id is not None:
                    return role_id
            role_id_str = os.getenv('NO_SHARING_ROLE_ID')
            return int(role_id_str) if role_id_str else None

        guild: Optional[discord.Guild] = None
        role_id: Optional[int] = None
        candidate_guilds = [bot.get_guild(guild_id)] if guild_id else list(getattr(bot, 'guilds', []))
        candidate_guilds = [g for g in candidate_guilds if g is not None]

        for candidate in candidate_guilds:
            candidate_role_id = _resolve_role_id(candidate.id)
            if not candidate_role_id:
                continue
            member = candidate.get_member(member_id)
            if member is None:
                try:
                    member = await candidate.fetch_member(member_id)
                except (discord.NotFound, discord.Forbidden):
                    member = None
            if member is not None:
                guild = candidate
                role_id = candidate_role_id
                break

        if not guild or not member or not role_id:
            log.debug(f"No configured no-sharing role found for member {member_id}")
            return True

        # Get the role
        role = guild.get_role(role_id)
        if not role:
            log.error(f"Role {role_id} not found in guild {guild_id}")
            return False
        
        # Add or remove role based on allow_sharing
        if allow_sharing:
            # Remove the "no sharing" role
            if role in member.roles:
                await member.remove_roles(role, reason="User enabled content sharing")
                log.info(f"Removed 'no sharing' role from member {member_id}")
            else:
                log.debug(f"Member {member_id} doesn't have the 'no sharing' role, nothing to remove")
        else:
            # Add the "no sharing" role
            if role not in member.roles:
                await member.add_roles(role, reason="User disabled content sharing")
                log.info(f"Added 'no sharing' role to member {member_id}")
            else:
                log.debug(f"Member {member_id} already has the 'no sharing' role")
        
        return True
        
    except discord.Forbidden as e:
        log.error(f"No permission to modify roles for member {member_id}: {e}")
        return False
    except Exception as e:
        log.error(f"Error updating 'no sharing' role for member {member_id}: {e}", exc_info=True)
        return False


@dataclass(frozen=True)
class DeleteCounts:
    deleted: int
    skipped: int
    errored: int


async def safe_delete_messages(
    channel,
    message_ids: Iterable[int],
    *,
    logger: logging.Logger,
) -> DeleteCounts:
    ids = list(message_ids)
    if channel is None:
        return DeleteCounts(0, len(ids), 0)

    deleted = 0
    skipped = 0
    errored = 0
    forbidden_logged = False

    async def _delete_once(message_id: int) -> str:
        message = await channel.fetch_message(message_id)
        await message.delete()
        return 'deleted'

    async def _delete_with_retry(message_id: int) -> str:
        try:
            return await _delete_once(message_id)
        except discord.NotFound:
            return 'skipped'
        except discord.Forbidden as exc:
            nonlocal forbidden_logged
            if not forbidden_logged:
                logger.warning(
                    "safe_delete_messages missing permission in channel %s: %s",
                    getattr(channel, 'id', 'unknown'),
                    exc,
                )
                forbidden_logged = True
            return 'errored'
        except discord.HTTPException as exc:
            if getattr(exc, 'status', None) == 429:
                await asyncio.sleep(getattr(exc, 'retry_after', 1.0))
                try:
                    return await _delete_once(message_id)
                except discord.NotFound:
                    return 'skipped'
                except discord.Forbidden as retry_exc:
                    if not forbidden_logged:
                        logger.warning(
                            "safe_delete_messages missing permission in channel %s: %s",
                            getattr(channel, 'id', 'unknown'),
                            retry_exc,
                        )
                        forbidden_logged = True
                    return 'errored'
                except discord.HTTPException as retry_http_exc:
                    logger.warning(
                        "safe_delete_messages HTTP error for message %s in channel %s after retry: %s",
                        message_id,
                        getattr(channel, 'id', 'unknown'),
                        retry_http_exc,
                    )
                    return 'errored'
                except AttributeError as retry_attr_exc:
                    logger.warning(
                        "safe_delete_messages attribute error for message %s in channel %s after retry: %s",
                        message_id,
                        getattr(channel, 'id', 'unknown'),
                        retry_attr_exc,
                    )
                    return 'errored'
            logger.warning(
                "safe_delete_messages HTTP error for message %s in channel %s: %s",
                message_id,
                getattr(channel, 'id', 'unknown'),
                exc,
            )
            return 'errored'
        except AttributeError as exc:
            logger.warning(
                "safe_delete_messages attribute error for message %s in channel %s: %s",
                message_id,
                getattr(channel, 'id', 'unknown'),
                exc,
            )
            return 'errored'

    for message_id in ids:
        try:
            result = await _delete_with_retry(message_id)
        except Exception as exc:
            logger.warning(
                "safe_delete_messages unexpected error for message %s in channel %s: %s",
                message_id,
                getattr(channel, 'id', 'unknown'),
                exc,
            )
            errored += 1
            continue

        if result == 'deleted':
            deleted += 1
        elif result == 'skipped':
            skipped += 1
        else:
            errored += 1

    return DeleteCounts(deleted=deleted, skipped=skipped, errored=errored)
