"""
In-process Discord message archive runner (ArchiveTask).

WHY IN-PROCESS (DC-6):
    Before this refactor, the hourly archive spawned `scripts/archive_discord.py`
    as a subprocess that opened a second Discord gateway connection with the same bot
    token. Discord allows only one live gateway per bot, so the duplicate login caused
    session churn on the main bot — every hour the main bot's gateway was invalidated,
    triggering spurious "Bot restarted" DMs from `on_ready` and `heartbeat blocked`
    warnings visible in system_logs.

    ArchiveTask solves this by running the archive logic inside the main bot process,
    using the existing `discord.Client` connection. No second gateway. No session
    churn. A developer re-introducing a separate gateway login would re-create the
    duplicate-session problem and cause the main bot to disconnect/reconnect every
    hourly cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import discord
from dotenv import load_dotenv

from src.common.discord_utils import emoji_to_str
from src.common.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Module-level helpers (same logic as scripts/archive_discord.py)
# ---------------------------------------------------------------------------

DISCORD_EPOCH_MS = 1420070400000
_thread_local = threading.local()


def snowflake_to_datetime(snowflake_id: int) -> datetime:
    """Convert a Discord snowflake ID to a timezone-aware UTC datetime."""
    timestamp_ms = (snowflake_id >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def to_aware_utc(dt_str: str) -> datetime:
    """Convert an ISO format string to a timezone-aware datetime object."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _get_db():
    """Get thread-local database connection (used by _db_worker thread)."""
    if not hasattr(_thread_local, "db"):
        from src.common.db_handler import DatabaseHandler

        _thread_local.db = DatabaseHandler()
    return _thread_local.db


# ---------------------------------------------------------------------------
# ArchiveResult
# ---------------------------------------------------------------------------


@dataclass
class ArchiveResult:
    """Outcome of an ArchiveTask run.

    ``success`` is True when at least one channel was archived without a fatal
    error.  It is False *only* when ALL channels failed or the run encountered
    a fatal error (e.g. guild not found) that prevented any channel from being
    processed.
    """

    success: bool = False
    messages_archived: int = 0
    duration_seconds: float = 0.0
    per_channel_errors: Dict[int, str] = field(default_factory=dict)
    fatal_error: Optional[str] = None


# ---------------------------------------------------------------------------
# ArchiveTask
# ---------------------------------------------------------------------------


class ArchiveTask:
    """Plain async class that archives Discord messages using an existing bot client.

    This is NOT a ``discord.Client`` subclass and NOT a ``commands.Cog``
    (LD-2).  It receives the main bot's ``discord.Client`` via its constructor
    and uses ``self.bot.get_guild()`` / ``self.bot.get_channel()`` to interact
    with Discord through the already-authenticated gateway connection.
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        bot: discord.Client,
        *,
        dev_mode: bool = False,
        order: str = "newest",
        days: Optional[int] = None,
        batch_size: int = 100,
        in_depth: bool = False,
        channel_id: Optional[int] = None,
        channel_ids: Optional[List[int]] = None,
        fetch_reactions: bool = False,
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None,
        fast_fill: bool = False,
        guild_id: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
    ):
        # --- bot reference (NOT a second client) ---
        self.bot: discord.Client = bot

        # --- logger ---
        self.logger: logging.Logger = logger or logging.getLogger("DiscordBot")

        # --- database queue + worker thread (preserved from original design, LD-4) ---
        self.db_queue: queue.Queue = queue.Queue()

        # --- rate limiter ---
        self.rate_limiter: RateLimiter = RateLimiter()

        # --- member cache to avoid repeated DB upserts ---
        self.member_update_cache: Dict[str, float] = {}
        self.member_update_cache_timeout: int = 300  # 5 minutes

        # --- summary thread cache ---
        self._summary_thread_ids: set[int] = set()

        # --- archive totals ---
        self.total_messages_archived: int = 0

        # --- per-channel error tracking ---
        self.per_channel_errors: Dict[int, str] = {}

        # ------------------------------------------------------------------
        # Guild ID: constructor arg > env var (LD decision)
        # ------------------------------------------------------------------
        load_dotenv(override=True)

        if guild_id is not None:
            self.guild_id: int = guild_id
        else:
            env_key = "DEV_GUILD_ID" if dev_mode else "GUILD_ID"
            raw = os.getenv(env_key)
            if raw is None:
                raise ValueError(
                    f"guild_id not provided and {env_key} is not set in environment"
                )
            self.guild_id = int(raw)

        # ------------------------------------------------------------------
        # Channel targeting: constructor args take precedence; env var is fallback
        # ------------------------------------------------------------------
        if channel_ids is not None:
            self.target_channel_ids: List[int] = list(channel_ids)
        elif channel_id is not None:
            self.target_channel_ids: List[int] = [channel_id]
        else:
            raw_channels = os.getenv("DISCORD_CHANNEL_IDS", "")
            if raw_channels.strip():
                self.target_channel_ids = [
                    int(c.strip()) for c in raw_channels.split(",") if c.strip()
                ]
            else:
                self.target_channel_ids = []

        # Backward compat alias
        self.target_channel_id: Optional[int] = (
            self.target_channel_ids[0] if self.target_channel_ids else None
        )

        # ------------------------------------------------------------------
        # Channels to skip (empty for non-BNDC guilds — populated per-guild)
        # ------------------------------------------------------------------
        self.skip_channels: set[int] = set()

        # ------------------------------------------------------------------
        # Default config
        # ------------------------------------------------------------------
        self.default_config: Dict[str, Any] = {
            "batch_size": batch_size,
            "delay": 0.25,
        }

        # ------------------------------------------------------------------
        # Message ordering
        # ------------------------------------------------------------------
        self.oldest_first: bool = order.lower() == "oldest"
        self.logger.info(
            "Message ordering: %s",
            "oldest to newest" if self.oldest_first else "newest to oldest",
        )

        # ------------------------------------------------------------------
        # Days limit (mutually exclusive with start/end date)
        # ------------------------------------------------------------------
        if days is not None and not (start_date_str or end_date_str):
            self.days_limit: Optional[int] = days
        elif start_date_str or end_date_str:
            self.days_limit = None
        else:
            self.days_limit = None

        if self.days_limit:
            self.logger.info("Will fetch messages from the last %d days", self.days_limit)
        elif not (start_date_str or end_date_str):
            self.logger.info("Will fetch all available messages (checking DB range)")

        # ------------------------------------------------------------------
        # Start / end dates
        # ------------------------------------------------------------------
        self.start_date: Optional[datetime] = None
        self.end_date: Optional[datetime] = None
        if start_date_str or end_date_str:
            if not start_date_str or not end_date_str:
                raise ValueError(
                    "Both --start-date and --end-date must be provided together."
                )
            self.start_date = datetime.strptime(start_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            self.end_date = (
                datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(days=1)
            ).replace(tzinfo=timezone.utc)
            if self.start_date >= self.end_date:
                raise ValueError("Start date must be before end date.")
            self.logger.info(
                "Fetching messages strictly between %s and %s",
                self.start_date.strftime("%Y-%m-%d %H:%M:%S %Z"),
                self.end_date.strftime("%Y-%m-%d %H:%M:%S %Z"),
            )

        # ------------------------------------------------------------------
        # In-depth mode
        # ------------------------------------------------------------------
        self.in_depth: bool = in_depth
        if in_depth:
            self.logger.info("Running in in-depth mode - will perform thorough message checks")

        # ------------------------------------------------------------------
        # Fetch reactions
        # ------------------------------------------------------------------
        self.fetch_reactions: bool = fetch_reactions
        if fetch_reactions:
            self.logger.info("Will fetch reactions for all messages in range")

        # ------------------------------------------------------------------
        # Fast-fill mode
        # ------------------------------------------------------------------
        self.fast_fill: bool = fast_fill
        if fast_fill:
            self.logger.info(
                "🚀 Running in FAST-FILL mode - batched DB checks, "
                "skipping member updates and reactions"
            )

        # ------------------------------------------------------------------
        # Rate limiting tracking
        # ------------------------------------------------------------------
        self.last_api_call: datetime = datetime.now()
        self.api_call_count: int = 0
        self.rate_limit_reset: datetime = datetime.now()
        self.rate_limit_remaining: int = 50

        # ------------------------------------------------------------------
        # Total days in range (for progress reporting)
        # ------------------------------------------------------------------
        self.total_days_in_range: int = 0
        if self.start_date and self.end_date:
            self.total_days_in_range = (self.end_date - self.start_date).days
            if self.total_days_in_range <= 0:
                self.total_days_in_range = 1

        # ------------------------------------------------------------------
        # Start database worker thread
        # ------------------------------------------------------------------
        self.db_thread: threading.Thread = threading.Thread(
            target=self._db_worker, daemon=True
        )
        self.db_thread.start()

        # ------------------------------------------------------------------
        # Connection history (preserved from original for shutdown safety)
        # ------------------------------------------------------------------
        self._connection_history: list = []

    # ------------------------------------------------------------------
    # DB Worker Thread (LD-4: preserved background thread + queue pattern)
    # ------------------------------------------------------------------

    def _db_worker(self) -> None:
        """Worker thread for database operations.

        Runs on a background daemon thread.  Uses ``self.bot.loop`` (NOT
        ``self.loop``) for ``call_soon_threadsafe`` because this class is NOT a
        ``discord.Client`` subclass — the event loop belongs to the bot.

        Shutdown protocol: caller pushes ``None`` onto ``db_queue``, then calls
        ``thread.join(timeout=30)``.  A logged warning is emitted on timeout.
        """
        db = _get_db()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            try:
                operation = self.db_queue.get()
                if operation is None:  # sentinel — graceful shutdown
                    break

                func, args, kwargs, future = operation
                try:
                    result = func(db, *args, **kwargs)
                    if asyncio.iscoroutine(result):
                        result = loop.run_until_complete(result)

                    if not future.done():
                        def _set_result(r=result):
                            if not future.done():
                                future.set_result(r)

                        self.bot.loop.call_soon_threadsafe(_set_result)
                except Exception as exception:
                    if not future.done():
                        def _set_exception(e=exception):
                            if not future.done():
                                future.set_exception(e)

                        self.bot.loop.call_soon_threadsafe(_set_exception)

                self.db_queue.task_done()
            except Exception:
                self.logger.error("Error in database worker", exc_info=True)
                continue

        loop.close()

    # ------------------------------------------------------------------
    # Shutdown helper
    # ------------------------------------------------------------------

    def _shutdown_db_worker(self) -> None:
        """Signal the DB worker to stop and join with a 30 s timeout."""
        self.db_queue.put(None)  # sentinel
        self.db_thread.join(timeout=30)
        if self.db_thread.is_alive():
            self.logger.warning(
                "DB worker thread did not exit within 30 s — proceeding anyway"
            )

    # ------------------------------------------------------------------
    # DB operation helper (schedules work on the background thread)
    # ------------------------------------------------------------------

    async def _db_operation(self, func, *args, **kwargs) -> Any:
        """Execute a database operation in the worker thread."""
        if not self.bot.loop or self.bot.loop.is_closed():
            self.logger.error("Bot event loop is closed or None — cannot schedule DB op")
            raise RuntimeError("Bot event loop unavailable")

        future = self.bot.loop.create_future()
        self.db_queue.put((func, args, kwargs, future))
        try:
            return await future
        except Exception:
            self.logger.error("Error in database operation", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # Archiving feature flag check
    # ------------------------------------------------------------------

    def _is_archiving_enabled(self, channel_id: int) -> bool:
        """Check if archiving is enabled for this channel via server_config."""
        db = _get_db()
        sc = getattr(db, "server_config", None)
        if sc is None:
            return True
        return sc.is_feature_enabled(self.guild_id, channel_id, "archiving")

    # ------------------------------------------------------------------
    # Bot summary message detection (uses bot.user.id instead of BOT_USER_ID env)
    # ------------------------------------------------------------------

    def _is_bot_summary_message(
        self, message: discord.Message, channel: discord.abc.Messageable
    ) -> bool:
        """Return True only for bot messages posted inside a summary thread."""
        if message.author.id != self.bot.user.id:  # uses bot.user.id, NOT env var
            return False
        # Check cache first
        if channel.id in self._summary_thread_ids:
            return True
        # Only skip bot messages in summary threads
        if isinstance(channel, discord.Thread) and "Summary" in (channel.name or ""):
            self._summary_thread_ids.add(channel.id)
            return True
        return False

    # ------------------------------------------------------------------
    # Rate-limit wait helper
    # ------------------------------------------------------------------

    async def _wait_for_rate_limit(self) -> None:
        """Handles rate limiting for Discord API calls."""
        now = datetime.now()
        self.api_call_count += 1

        time_since_last = (now - self.last_api_call).total_seconds()

        # Basic throttling — ensure at least 0.1 s between calls
        if time_since_last < 0.1:
            await asyncio.sleep(0.1 - time_since_last)

        # Only enforce rate limits if we're approaching them
        if self.api_call_count >= 45:  # conservative buffer before hitting 50
            await asyncio.sleep(1.0)
            self.api_call_count = 0
            self.rate_limit_reset = datetime.now() + timedelta(seconds=60)
            self.rate_limit_remaining = 50

        self.last_api_call = now

    # ------------------------------------------------------------------
    # Fetch archived threads (with retry)
    # ------------------------------------------------------------------

    async def _fetch_archived_threads(
        self, channel: Union[discord.TextChannel, discord.ForumChannel]
    ) -> List[discord.Thread]:
        """Fetch archived threads with retry logic for Discord API errors."""
        max_retries = 3
        delay = 5  # seconds
        channel_name = getattr(channel, "name", str(channel.id))
        for attempt in range(max_retries):
            try:
                return [t async for t in channel.archived_threads()]
            except discord.DiscordServerError:
                if attempt < max_retries - 1:
                    self.logger.warning(
                        "Discord API error (503) fetching archived threads for #%s. "
                        "Retrying in %ds... (Attempt %d/%d)",
                        channel_name,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(
                        "Failed to fetch archived threads for #%s after %d attempts. "
                        "Skipping threads for this channel.",
                        channel_name,
                        max_retries,
                        exc_info=True,
                    )
                    return []
        return []

    # ------------------------------------------------------------------
    # Guild / channel resolution with fetch fallback (OQ-2)
    # ------------------------------------------------------------------

    async def _resolve_guild(self, guild_id: int) -> Optional[discord.Guild]:
        """Return a guild by ID, falling back to fetch on cache miss.

        Uses ``self.bot.get_guild`` first (cache); on cache miss falls
        back to ``self.bot.fetch_guild``.  Returns ``None`` if the guild
        is not found or the bot lacks access.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return guild

        try:
            guild = await self.bot.fetch_guild(guild_id)
            return guild
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning(
                "fetch_guild(%d) failed: %s", guild_id, exc
            )
            return None

    async def _resolve_channel(
        self, channel_id: int
    ) -> Optional[discord.abc.GuildChannel]:
        """Return a channel by ID, falling back to fetch on cache miss.

        Uses ``self.bot.get_channel`` first (cache); on cache miss falls
        back to ``self.bot.fetch_channel``.  Returns ``None`` if the
        channel is not found or the bot lacks access.
        """
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel

        try:
            channel = await self.bot.fetch_channel(channel_id)
            return channel
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning(
                "fetch_channel(%d) failed: %s", channel_id, exc
            )
            return None

    # ------------------------------------------------------------------
    # Message storage helpers
    # ------------------------------------------------------------------

    async def _store_messages_and_reactions(
        self, processed_messages: List[Dict[str, Any]]
    ) -> None:
        """Store messages and their granular reaction data.

        Calls ``db.store_messages`` in a single batch, then iterates over
        each message to extract and upsert per-emoji reaction rows via
        ``db.upsert_reactions_batch``.
        """
        await self._db_operation(
            lambda db: db.store_messages(processed_messages)
        )

        # Yield control between batch store and per-message reaction
        # iteration (starvation insurance).
        await asyncio.sleep(0)

        for msg in processed_messages:
            rows: List[Dict[str, Any]] = msg.pop("_reaction_rows", [])
            if rows:
                msg_id: int = msg["message_id"]
                await self._db_operation(
                    lambda db, mid=msg_id, r=rows, gid=self.guild_id: (
                        db.upsert_reactions_batch(mid, r, guild_id=gid)
                    )
                )

    # ------------------------------------------------------------------
    # Single-message processor (ported from scripts/archive_discord.py)
    # ------------------------------------------------------------------

    async def _process_message(
        self, message: discord.Message, channel_id: int
    ) -> Optional[Dict[str, Any]]:
        """Process a single Discord message into a Supabase-ready dict.

        Handles reaction fetching, member upserts, channel creation,
        and thread-parent resolution.  Returns ``None`` on failure so
        callers can safely skip.
        """
        try:
            # ---- reaction counting -------------------------------------------
            reaction_count: int = (
                sum(r.count for r in message.reactions) if message.reactions else 0
            )
            reactors: List[int] = []
            reaction_rows: List[Dict[str, Any]] = []

            # In fast-fill mode, skip reaction fetching entirely
            # (we already have reactions from the first pass).
            if self.fast_fill:
                pass  # skip reaction + member processing
            elif reaction_count > 0 and message.reactions:
                reactor_ids: set[int] = set()
                try:
                    message_exists: bool = await self._db_operation(
                        lambda db: db.message_exists(message.id)
                    )
                    if self.in_depth or self.fetch_reactions or not message_exists:
                        self.logger.debug(
                            "Processing reactions for message %d: %d types, %d total",
                            message.id,
                            len(message.reactions),
                            reaction_count,
                        )

                        guild = await self._resolve_guild(self.guild_id)

                        for reaction in message.reactions:
                            try:
                                emoji_str_val: str = emoji_to_str(reaction.emoji)

                                async def fetch_users() -> None:
                                    async for user in reaction.users(limit=50):
                                        reactor_ids.add(user.id)
                                        reaction_rows.append({
                                            "message_id": message.id,
                                            "user_id": user.id,
                                            "emoji": emoji_str_val,
                                        })
                                        # Check cache before upserting member
                                        cache_key = f"{user.id}_{user.name}"
                                        cache_time = self.member_update_cache.get(
                                            cache_key, 0
                                        )
                                        current_time = time.time()

                                        if (
                                            current_time - cache_time
                                            > self.member_update_cache_timeout
                                        ):
                                            member = (
                                                guild.get_member(user.id)
                                                if guild
                                                else None
                                            )
                                            role_ids = (
                                                json.dumps(
                                                    [r.id for r in member.roles]
                                                )
                                                if member and member.roles
                                                else None
                                            )
                                            guild_join_date = (
                                                member.joined_at.isoformat()
                                                if member and member.joined_at
                                                else None
                                            )

                                            await self._db_operation(
                                                lambda db,
                                                gid=self.guild_id: (
                                                    db.create_or_update_member(
                                                        user.id,
                                                        user.name,
                                                        getattr(
                                                            user,
                                                            "display_name",
                                                            None,
                                                        ),
                                                        getattr(
                                                            user,
                                                            "global_name",
                                                            None,
                                                        ),
                                                        (
                                                            str(user.avatar.url)
                                                            if user.avatar
                                                            else None
                                                        ),
                                                        getattr(
                                                            user,
                                                            "discriminator",
                                                            None,
                                                        ),
                                                        getattr(
                                                            user, "bot", False
                                                        ),
                                                        getattr(
                                                            user, "system", False
                                                        ),
                                                        getattr(
                                                            user,
                                                            "accent_color",
                                                            None,
                                                        ),
                                                        (
                                                            str(user.banner.url)
                                                            if getattr(
                                                                user,
                                                                "banner",
                                                                None,
                                                            )
                                                            else None
                                                        ),
                                                        (
                                                            user.created_at.isoformat()
                                                            if hasattr(
                                                                user, "created_at"
                                                            )
                                                            else None
                                                        ),
                                                        guild_join_date,
                                                        role_ids,
                                                        guild_id=gid,
                                                    )
                                                )
                                            )
                                            self.member_update_cache[
                                                cache_key
                                            ] = current_time

                                await self.rate_limiter.execute(
                                    f"reaction_{message.id}_{reaction}", fetch_users
                                )

                            except Exception as exc:
                                self.logger.warning(
                                    "Error fetching users for reaction %s on "
                                    "message %d: %s",
                                    reaction,
                                    message.id,
                                    exc,
                                )
                                continue

                        if reactor_ids:
                            reactors = list(reactor_ids)
                except Exception as exc:
                    self.logger.warning(
                        "Could not fetch reactors for message %d: %s",
                        message.id,
                        exc,
                    )

            # ---- message author (skip in fast-fill) --------------------------
            if not self.fast_fill and hasattr(message.author, "id"):
                cache_key = f"{message.author.id}_{message.author.name}"
                cache_time = self.member_update_cache.get(cache_key, 0)
                current_time = time.time()

                if current_time - cache_time > self.member_update_cache_timeout:
                    guild = await self._resolve_guild(self.guild_id)
                    member = (
                        guild.get_member(message.author.id) if guild else None
                    )
                    role_ids = (
                        json.dumps([r.id for r in member.roles])
                        if member and member.roles
                        else None
                    )
                    guild_join_date = (
                        member.joined_at.isoformat()
                        if member and member.joined_at
                        else None
                    )

                    await self._db_operation(
                        lambda db, gid=self.guild_id: db.create_or_update_member(
                            message.author.id,
                            message.author.name,
                            getattr(message.author, "display_name", None),
                            getattr(message.author, "global_name", None),
                            (
                                str(message.author.avatar.url)
                                if message.author.avatar
                                else None
                            ),
                            getattr(message.author, "discriminator", None),
                            getattr(message.author, "bot", False),
                            getattr(message.author, "system", False),
                            getattr(message.author, "accent_color", None),
                            (
                                str(message.author.banner.url)
                                if getattr(message.author, "banner", None)
                                else None
                            ),
                            (
                                message.author.created_at.isoformat()
                                if hasattr(message.author, "created_at")
                                else None
                            ),
                            guild_join_date,
                            role_ids,
                            guild_id=gid,
                        )
                    )
                    self.member_update_cache[cache_key] = current_time

            # ---- thread parent resolution ------------------------------------
            # Threads (regular text threads and forum posts) are stored against
            # their parent channel with thread_id pointing to the thread itself.
            # This keeps per-parent-channel activity queries working while still
            # preserving enough info for jump-URL builders.
            actual_channel = message.channel
            thread_id: Optional[int] = None
            if isinstance(message.channel, discord.Thread):
                actual_channel = message.channel.parent or message.channel
                thread_id = message.channel.id

            # ---- channel upsert (skip in fast-fill) --------------------------
            if not self.fast_fill:
                category_id: Optional[int] = None
                if (
                    hasattr(actual_channel, "category")
                    and actual_channel.category
                ):
                    category_id = actual_channel.category.id

                # Determine channel_type and parent_id
                ch_type: Optional[str] = None
                parent_id: Optional[int] = None
                if isinstance(actual_channel, discord.ForumChannel):
                    ch_type = "forum"
                elif isinstance(actual_channel, discord.TextChannel):
                    # discord.py has no NewsChannel class — news channels ARE
                    # TextChannel instances with is_news() True.
                    ch_type = "news" if actual_channel.is_news() else "text"
                elif isinstance(actual_channel, discord.Thread):
                    # A forum-post thread IS a forum post (label it 'forum' so the
                    # forum is detectable); a reply-thread inside a text channel is
                    # 'thread'. Previously left NULL, which made newer forums
                    # undetectable from channel_type (bug 2026-08-05). Defensive —
                    # normally the code resolves to message.channel.parent first.
                    parent = getattr(actual_channel, "parent", None)
                    ch_type = "forum" if isinstance(parent, discord.ForumChannel) else "thread"
                elif isinstance(actual_channel, discord.VoiceChannel):
                    ch_type = "voice"
                elif isinstance(actual_channel, discord.StageChannel):
                    ch_type = "stage"
                elif isinstance(actual_channel, discord.CategoryChannel):
                    ch_type = "category"
                if (
                    hasattr(actual_channel, "parent")
                    and actual_channel.parent
                ):
                    parent_id = actual_channel.parent.id

                await self._db_operation(
                    lambda db,
                    gid=self.guild_id,
                    ct=ch_type,
                    pid=parent_id: db.create_or_update_channel(
                        channel_id=actual_channel.id,
                        channel_name=actual_channel.name,
                        nsfw=getattr(actual_channel, "nsfw", False),
                        category_id=category_id,
                        guild_id=gid,
                        channel_type=ct,
                        parent_id=pid,
                    )
                )

            # ---- build processed message dict --------------------------------
            processed_message: Dict[str, Any] = {
                "message_id": message.id,
                "channel_id": actual_channel.id,
                "guild_id": self.guild_id,
                "author_id": message.author.id,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "attachments": [
                    {"url": a.url, "filename": a.filename}
                    for a in message.attachments
                ],
                "embeds": [e.to_dict() for e in message.embeds],
                "reaction_count": reaction_count,
                "reactors": reactors,
                "reference_id": (
                    message.reference.message_id
                    if message.reference
                    else None
                ),
                "edited_at": (
                    message.edited_at.isoformat()
                    if message.edited_at
                    else None
                ),
                "is_pinned": message.pinned,
                "thread_id": thread_id,
                "message_type": str(message.type),
                "flags": message.flags.value,
                "_reaction_rows": reaction_rows,
            }

            # Yield to the event loop after each message (starvation insurance).
            await asyncio.sleep(0)

            return processed_message

        except Exception as exc:
            self.logger.error("Error processing message %d: %s", message.id, exc)
            return None

    # ------------------------------------------------------------------
    # Target resolution (ported from on_ready warm-up logic)
    # ------------------------------------------------------------------

    async def _resolve_targets(
        self,
    ) -> List[Tuple[str, Any, Optional[str]]]:
        """Collect text/forum channels and threads to archive.

        Handles CategoryChannel expansion, forum threads, and archived
        threads.  When ``days_limit`` is set and ``in_depth`` is False,
        inactive threads are filtered by snowflake timestamp.
        """
        guild = await self._resolve_guild(self.guild_id)
        if not guild:
            raise ValueError(
                "Could not find guild with ID %d" % self.guild_id
            )

        items_to_process: List[Tuple[str, Any, Optional[str]]] = []

        if self.target_channel_ids:
            for target_cid in self.target_channel_ids:
                channel = await self._resolve_channel(target_cid)
                if channel is None:
                    self.logger.error(
                        "Could not find target channel with ID %d", target_cid
                    )
                    continue

                if isinstance(channel, discord.CategoryChannel):
                    self.logger.info(
                        "Target is a CategoryChannel: #%s. Expanding to child channels...",
                        channel.name,
                    )
                    try:
                        for child in channel.channels:
                            if isinstance(child, discord.TextChannel):
                                items_to_process.append(
                                    ("channel", child, None)
                                )
                                archived_threads = (
                                    await self._fetch_archived_threads(child)
                                )
                                active_threads = child.threads
                                for thread in archived_threads + list(active_threads):
                                    items_to_process.append(
                                        ("thread", thread, child.name)
                                    )
                            elif isinstance(child, discord.ForumChannel):
                                archived_threads = (
                                    await self._fetch_archived_threads(child)
                                )
                                active_threads = child.threads
                                for thread in archived_threads + list(active_threads):
                                    items_to_process.append(
                                        ("forum_thread", thread, child.name)
                                    )
                            else:
                                continue
                    except Exception as exc:
                        self.logger.error(
                            "Failed to expand CategoryChannel %d: %s",
                            channel.id,
                            exc,
                            exc_info=True,
                        )
                elif isinstance(channel, discord.TextChannel):
                    items_to_process.append(("channel", channel, None))
                    archived_threads = await self._fetch_archived_threads(channel)
                    active_threads = channel.threads
                    for thread in archived_threads + list(active_threads):
                        items_to_process.append(
                            ("thread", thread, channel.name)
                        )
                elif isinstance(channel, discord.ForumChannel):
                    archived_threads = await self._fetch_archived_threads(channel)
                    active_threads = channel.threads
                    for thread in archived_threads + list(active_threads):
                        items_to_process.append(
                            ("forum_thread", thread, channel.name)
                        )
                elif isinstance(channel, discord.Thread):
                    items_to_process.append(
                        (
                            "thread",
                            channel,
                            getattr(channel.parent, "name", None),
                        )
                    )
                else:
                    self.logger.warning(
                        "Channel %d is an unsupported type. Skipping.",
                        target_cid,
                    )
        else:
            # Collect all text channels, filtered by archiving feature config
            all_text_channels = [
                c
                for c in guild.text_channels
                if c.id not in self.skip_channels
            ]
            for channel in all_text_channels:
                if not self._is_archiving_enabled(channel.id):
                    continue
                items_to_process.append(("channel", channel, None))
                archived_threads = await self._fetch_archived_threads(channel)
                active_threads = channel.threads
                for thread in archived_threads + list(active_threads):
                    items_to_process.append(
                        ("thread", thread, channel.name)
                    )

            # Collect all forum threads, filtered by archiving feature config
            all_forums = [
                f
                for f in guild.channels
                if isinstance(f, discord.ForumChannel)
                and f.id not in self.skip_channels
            ]
            for forum in all_forums:
                if not self._is_archiving_enabled(forum.id):
                    continue
                archived_threads = await self._fetch_archived_threads(forum)
                active_threads = forum.threads
                for thread in archived_threads + list(active_threads):
                    items_to_process.append(
                        ("forum_thread", thread, forum.name)
                    )

        total_items_before_filter = len(items_to_process)
        self.logger.info(
            "Collected %d total channels/threads to process.",
            total_items_before_filter,
        )

        # Filter inactive threads using snowflake timestamps.
        # When using --days, skip threads whose last_message_id is older
        # than the cutoff.  Skip this filter in --in-depth mode.
        if self.days_limit and not self.in_depth:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(
                days=self.days_limit
            )
            filtered_items: List[Tuple[str, Any, Optional[str]]] = []
            skipped_count = 0
            for item_type, item, parent_name in items_to_process:
                if item_type in ("thread", "forum_thread"):
                    last_msg_id = getattr(item, "last_message_id", None)
                    if last_msg_id:
                        last_activity = snowflake_to_datetime(last_msg_id)
                        if last_activity < cutoff_dt:
                            skipped_count += 1
                            continue
                filtered_items.append((item_type, item, parent_name))

            if skipped_count > 0:
                self.logger.info(
                    "Skipped %d inactive threads (no messages since %s UTC)",
                    skipped_count,
                    cutoff_dt.strftime("%Y-%m-%d %H:%M"),
                )
            items_to_process = filtered_items

        total_items = len(items_to_process)
        if total_items != total_items_before_filter:
            self.logger.info(
                "Processing %d items after filtering (was %d)",
                total_items,
                total_items_before_filter,
            )

        return items_to_process

    # ------------------------------------------------------------------
    # Channel archive dispatch
    # ------------------------------------------------------------------

    async def archive_channel(self, channel_id: int) -> None:
        """Archive all messages from a channel.

        Resolves the channel via ``_resolve_channel`` (with fetch
        fallback), handles thread-parent redirection for forum threads,
        and dispatches to the appropriate archival method.
        """
        channel_start_time = datetime.now(timezone.utc)
        try:
            # Skip welcome channel
            if channel_id in self.skip_channels:
                self.logger.info(
                    "Skipping welcome channel %d", channel_id
                )
                return

            channel = await self._resolve_channel(channel_id)
            if channel is None:
                self.logger.error(
                    "Could not find channel %d", channel_id
                )
                return

            # Get the actual channel (parent forum if this is a thread)
            channel_name = getattr(channel, "name", str(channel_id))
            actual_channel = channel
            if hasattr(channel, "parent") and channel.parent:
                actual_channel = channel.parent
                self.logger.debug(
                    "Using parent forum #%s (ID: %d) for thread #%s",
                    getattr(actual_channel, "name", str(actual_channel.id)),
                    actual_channel.id,
                    channel_name,
                )
                channel_id = actual_channel.id

            self.logger.info(
                "Starting archive of #%s at %s",
                channel_name,
                channel_start_time,
            )

            # Dispatch to the appropriate archival method
            if self.start_date and self.end_date:
                await self._archive_channel_date_range(channel, channel_id)
            else:
                await self._archive_channel_incremental(
                    channel, channel_id, channel_start_time
                )

        except discord.HTTPException as exc:
            if exc.code == 429:  # Rate limit error
                self.logger.warning(
                    "Hit rate limit while processing #%s: %s",
                    getattr(channel, "name", str(channel_id)),
                    exc,
                )
                retry_after = (
                    exc.retry_after
                    if hasattr(exc, "retry_after")
                    else 5
                )
                self.logger.info(
                    "Waiting %ss before continuing", retry_after
                )
                await asyncio.sleep(retry_after)
            else:
                self.logger.error(
                    "HTTP error in channel %s: %s",
                    getattr(channel, "name", str(channel_id)),
                    exc,
                )
        except Exception as exc:
            self.logger.error(
                "Error archiving channel %s: %s",
                getattr(channel, "name", str(channel_id)),
                exc,
            )
        finally:
            pass

    # ------------------------------------------------------------------
    # Date-range archive
    # ------------------------------------------------------------------

    async def _archive_channel_date_range(
        self, channel, channel_id: int
    ) -> None:
        """Archive messages from a channel within a specific date range."""
        channel_name = getattr(channel, "name", str(channel_id))
        start_log_msg = (
            "Starting date-range archive for #%s (%s to %s)"
            % (
                channel_name,
                self.start_date.date() if self.start_date else "?",
                (self.end_date - timedelta(days=1)).date()
                if self.end_date
                else "?",
            )
        )
        if self.total_days_in_range > 0:
            start_log_msg += (
                " - Total duration: %d days" % self.total_days_in_range
            )
        self.logger.info(start_log_msg)

        message_counter = 0
        new_message_count = 0
        current_batch: List[discord.Message] = []
        last_processed_message_date = None
        last_progress_log_time = time.time()

        # Variables for daily timing and ETR
        current_processing_day_date = None
        current_day_start_time = None
        processed_days_count = 0
        total_processing_time_seconds = 0.0
        last_day_duration_str = "N/A"
        messages_processed_this_day = 0

        after = self.start_date
        before = self.end_date
        async for message in channel.history(
            limit=None,
            after=after,
            before=before,
            oldest_first=self.oldest_first,
        ):
            # Skip bot summary messages
            if self._is_bot_summary_message(message, channel):
                continue

            message_counter += 1
            msg_date = message.created_at.date()
            current_time_epoch = time.time()

            # Daily duration calculation
            if msg_date != current_processing_day_date:
                if (
                    current_processing_day_date is not None
                    and current_day_start_time is not None
                ):
                    day_duration_secs = (
                        current_time_epoch - current_day_start_time
                    )
                    processed_days_count += 1
                    total_processing_time_seconds += day_duration_secs
                    last_day_duration_str = "%%.2fs" % day_duration_secs  # will be formatted below
                    last_day_duration_str = "%.2fs" % day_duration_secs
                    self.logger.info(
                        "Completed Day %d/%d (%s) for #%s in %s - %d messages processed",
                        processed_days_count,
                        self.total_days_in_range,
                        current_processing_day_date,
                        channel_name,
                        last_day_duration_str,
                        messages_processed_this_day,
                    )
                    messages_processed_this_day = 0

                current_processing_day_date = msg_date
                current_day_start_time = current_time_epoch
                self.logger.info(
                    "Starting Day %d/%d (%s) for #%s...",
                    processed_days_count + 1,
                    self.total_days_in_range,
                    current_processing_day_date,
                    channel_name,
                )

            messages_processed_this_day += 1
            last_processed_message_date = message.created_at

            # Progress logging & ETR every ~30s
            if current_time_epoch - last_progress_log_time > 30:
                if (
                    last_processed_message_date
                    and self.total_days_in_range > 0
                ):
                    if self.oldest_first:
                        elapsed_total_days = (
                            last_processed_message_date - self.start_date
                        ).days + 1
                    else:
                        elapsed_total_days = (
                            self.end_date - last_processed_message_date
                        ).days
                        if elapsed_total_days == 0:
                            elapsed_total_days = 1

                    elapsed_total_days = max(
                        1,
                        min(elapsed_total_days, self.total_days_in_range),
                    )
                    percentage = (
                        elapsed_total_days / self.total_days_in_range
                    ) * 100

                    if processed_days_count > 0:
                        avg_time_per_day = (
                            total_processing_time_seconds
                            / processed_days_count
                        )
                        remaining_days = (
                            self.total_days_in_range - processed_days_count
                        )
                        etr_seconds = avg_time_per_day * remaining_days
                        etr_str = str(
                            timedelta(seconds=int(etr_seconds))
                        )
                    else:
                        etr_str = "Calculating..."

                    self.logger.info(
                        "Progress #%s: Overall %.1f%% (%d/%d days). "
                        "Last Day (%s) took %s. ETR: %s",
                        channel_name,
                        percentage,
                        elapsed_total_days,
                        self.total_days_in_range,
                        current_processing_day_date,
                        last_day_duration_str,
                        etr_str,
                    )
                    last_progress_log_time = current_time_epoch

            # Message selection
            if self.fast_fill:
                current_batch.append(message)
            else:
                process_this_message = False
                if self.in_depth or self.fetch_reactions:
                    process_this_message = True
                else:
                    message_exists = await self._db_operation(
                        lambda db: db.message_exists(message.id)
                    )
                    if not message_exists:
                        process_this_message = True

                if process_this_message:
                    current_batch.append(message)

            # Store batch when it reaches the threshold
            if len(current_batch) >= 100:
                try:
                    if self.fast_fill:
                        batch_ids = [msg.id for msg in current_batch]
                        existing_in_db = await self._db_operation(
                            lambda db: db.get_messages_by_ids(batch_ids)
                        )
                        existing_ids = {
                            msg["message_id"] for msg in existing_in_db
                        }
                        messages_to_process = [
                            msg
                            for msg in current_batch
                            if msg.id not in existing_ids
                        ]

                        if messages_to_process:
                            processed_messages = []
                            for msg in messages_to_process:
                                processed_msg = (
                                    await self._process_message(
                                        msg, channel_id
                                    )
                                )
                                if processed_msg:
                                    processed_messages.append(processed_msg)
                            if processed_messages:
                                new_message_count += len(processed_messages)
                                await self._store_messages_and_reactions(
                                    processed_messages
                                )
                    else:
                        processed_messages = []
                        for msg in current_batch:
                            processed_msg = await self._process_message(
                                msg, channel_id
                            )
                            if processed_msg:
                                processed_messages.append(processed_msg)
                        if processed_messages:
                            pre_existing_ids: set = set()
                            if not self.in_depth:
                                existing_in_db = await self._db_operation(
                                    lambda db: db.get_messages_by_ids(
                                        [
                                            msg["message_id"]
                                            for msg in processed_messages
                                        ]
                                    )
                                )
                                pre_existing_ids = {
                                    msg["message_id"]
                                    for msg in existing_in_db
                                }
                            new_messages = [
                                msg
                                for msg in processed_messages
                                if msg["message_id"] not in pre_existing_ids
                            ]
                            new_message_count += len(new_messages)
                            await self._store_messages_and_reactions(
                                processed_messages
                            )
                    current_batch = []
                    await asyncio.sleep(0.1)
                except Exception as exc:
                    self.logger.error(
                        "Failed to store batch during date range archive: %s",
                        exc,
                    )

        # Log duration for the final day
        if (
            current_processing_day_date is not None
            and current_day_start_time is not None
        ):
            final_day_duration_secs = time.time() - current_day_start_time
            if processed_days_count < self.total_days_in_range:
                processed_days_count += 1
            self.logger.info(
                "Completed Final Day %d/%d (%s) for #%s in %.2fs - %d messages processed",
                processed_days_count,
                self.total_days_in_range,
                current_processing_day_date,
                channel_name,
                final_day_duration_secs,
                messages_processed_this_day,
            )

        # Process any remaining messages
        if current_batch:
            try:
                if self.fast_fill:
                    batch_ids = [msg.id for msg in current_batch]
                    existing_in_db = await self._db_operation(
                        lambda db: db.get_messages_by_ids(batch_ids)
                    )
                    existing_ids = {
                        msg["message_id"] for msg in existing_in_db
                    }
                    messages_to_process = [
                        msg
                        for msg in current_batch
                        if msg.id not in existing_ids
                    ]

                    if messages_to_process:
                        processed_messages = []
                        for msg in messages_to_process:
                            processed_msg = await self._process_message(
                                msg, channel_id
                            )
                            if processed_msg:
                                processed_messages.append(processed_msg)
                        if processed_messages:
                            new_message_count += len(processed_messages)
                            await self._store_messages_and_reactions(
                                processed_messages
                            )
                else:
                    processed_messages = []
                    for msg in current_batch:
                        processed_msg = await self._process_message(
                            msg, channel_id
                        )
                        if processed_msg:
                            processed_messages.append(processed_msg)
                    if processed_messages:
                        pre_existing_ids = set()
                        if not self.in_depth:
                            existing_in_db = await self._db_operation(
                                lambda db: db.get_messages_by_ids(
                                    [
                                        msg["message_id"]
                                        for msg in processed_messages
                                    ]
                                )
                            )
                            pre_existing_ids = {
                                msg["message_id"]
                                for msg in existing_in_db
                            }
                        new_messages = [
                            msg
                            for msg in processed_messages
                            if msg["message_id"] not in pre_existing_ids
                        ]
                        new_message_count += len(new_messages)
                        await self._store_messages_and_reactions(
                            processed_messages
                        )
            except Exception as exc:
                self.logger.error(
                    "Failed to store final date range batch: %s", exc
                )

        self.logger.info(
            "Date range archive complete for #%s - Processed %d messages, "
            "saved %d new to Supabase",
            channel_name,
            message_counter,
            new_message_count,
        )
        self.total_messages_archived += new_message_count

        if self.total_days_in_range > 0:
            self.logger.info(
                "Progress for #%s: Day %d/%d (100.0%%) - Completed.",
                channel_name,
                self.total_days_in_range,
                self.total_days_in_range,
            )

    # ------------------------------------------------------------------
    # Incremental / full archive (with gap scanning)
    # ------------------------------------------------------------------

    async def _archive_channel_incremental(
        self,
        channel,
        channel_id: int,
        channel_start_time: datetime,
    ) -> None:
        """Archive messages using incremental/full logic.

        Uses ``--days`` cutoff or DB-derived date-range checks, then
        performs gap scanning for any weeks-long holes in coverage.
        """
        channel_name = getattr(channel, "name", str(channel_id))
        self.logger.info(
            "Starting incremental/full archive for #%s at %s",
            channel_name,
            channel_start_time,
        )

        cutoff_date: Optional[datetime] = None
        if self.days_limit:
            cutoff_date = datetime.now(timezone.utc) - timedelta(
                days=self.days_limit
            )
            self.logger.debug(
                "Will only fetch messages after %s", cutoff_date
            )

        earliest_date: Optional[datetime] = None
        latest_date: Optional[datetime] = None

        try:
            earliest_date, latest_date = await self._db_operation(
                lambda db: db.get_message_date_range(channel_id)
            )
            if earliest_date:
                earliest_date = earliest_date.replace(tzinfo=timezone.utc)
                self.logger.info(
                    "Earliest message in DB for #%s: %s",
                    channel_name,
                    earliest_date,
                )
            if latest_date:
                latest_date = latest_date.replace(tzinfo=timezone.utc)
                self.logger.info(
                    "Latest message in DB for #%s: %s",
                    channel_name,
                    latest_date,
                )
        except Exception as exc:
            self.logger.warning(
                "Could not get message date range, will fetch all messages: %s",
                exc,
            )

        message_counter = 0
        new_message_count = 0

        # If no archived messages exist or we're in in-depth mode,
        # get all messages in the time range
        if not earliest_date or not latest_date or self.in_depth:
            if self.in_depth:
                self.logger.debug(
                    "In-depth mode: Re-checking all messages in time range "
                    "for #%s",
                    channel_name,
                )
            else:
                self.logger.info(
                    "No existing archives found for #%s. Getting all messages...",
                    channel_name,
                )
            self.logger.debug(
                "Starting message fetch for #%s from %s...",
                channel_name,
                "oldest to newest" if self.oldest_first else "newest to oldest",
            )
            try:
                last_message = None
                while True:
                    history_kwargs = {
                        "limit": None,
                        "oldest_first": self.oldest_first,
                        "before": (
                            last_message.created_at
                            if last_message
                            else None
                        ),
                        "after": cutoff_date if cutoff_date else None,
                    }

                    self.logger.debug(
                        "Fetching messages for #%s with kwargs: %s",
                        channel_name,
                        history_kwargs,
                    )
                    current_batch: List[discord.Message] = []

                    try:
                        got_messages = False
                        async for message in channel.history(
                            **{
                                k: v
                                for k, v in history_kwargs.items()
                                if v is not None
                            }
                        ):
                            got_messages = True
                            last_message = message

                            message_counter += 1
                            if message_counter % 25 == 0:
                                self.logger.debug(
                                    "Fetched %d messages so far from #%s, "
                                    "last message from %s",
                                    message_counter,
                                    channel_name,
                                    message.created_at,
                                )

                            try:
                                if self._is_bot_summary_message(
                                    message, channel
                                ):
                                    continue

                                message_exists = await self._db_operation(
                                    lambda db: db.message_exists(message.id)
                                )
                                if (
                                    self.in_depth
                                    or self.fetch_reactions
                                    or not message_exists
                                ):
                                    current_batch.append(message)

                                if len(current_batch) >= 100:
                                    try:
                                        processed_messages = []
                                        for msg in current_batch:
                                            processed_msg = (
                                                await self._process_message(
                                                    msg, channel_id
                                                )
                                            )
                                            if processed_msg:
                                                processed_messages.append(
                                                    processed_msg
                                                )

                                        if processed_messages:
                                            pre_existing = set(
                                                msg["message_id"]
                                                for msg in await self._db_operation(
                                                    lambda db: db.get_messages_by_ids(
                                                        [
                                                            msg[
                                                                "message_id"
                                                            ]
                                                            for msg in processed_messages
                                                        ]
                                                    )
                                                )
                                            )
                                            new_messages = [
                                                msg
                                                for msg in processed_messages
                                                if msg["message_id"]
                                                not in pre_existing
                                            ]
                                            new_message_count += len(
                                                new_messages
                                            )

                                            self.logger.info(
                                                "Storing batch of %d messages "
                                                "from #%s (%d new, %d existing)",
                                                len(processed_messages),
                                                channel_name,
                                                len(new_messages),
                                                len(pre_existing),
                                            )
                                            await self._store_messages_and_reactions(
                                                processed_messages
                                            )

                                        current_batch = []
                                        await asyncio.sleep(0.1)
                                    except Exception as exc:
                                        self.logger.error(
                                            "Failed to store batch: %s", exc
                                        )

                            except Exception as exc:
                                self.logger.error(
                                    "Error processing message %d: %s",
                                    message.id,
                                    exc,
                                )
                                continue

                    except discord.Forbidden:
                        self.logger.warning(
                            "Missing permissions to read messages in #%s",
                            channel_name,
                        )
                        break
                    except Exception as exc:
                        self.logger.error(
                            "Error fetching messages: %s", exc
                        )
                        break

                    # Process remaining messages
                    if current_batch:
                        try:
                            processed_messages = []
                            for msg in current_batch:
                                processed_msg = (
                                    await self._process_message(
                                        msg, channel_id
                                    )
                                )
                                if processed_msg:
                                    processed_messages.append(processed_msg)

                            if processed_messages:
                                pre_existing = set(
                                    msg["message_id"]
                                    for msg in await self._db_operation(
                                        lambda db: db.get_messages_by_ids(
                                            [
                                                msg["message_id"]
                                                for msg in processed_messages
                                            ]
                                        )
                                    )
                                )
                                new_messages = [
                                    msg
                                    for msg in processed_messages
                                    if msg["message_id"] not in pre_existing
                                ]
                                new_message_count += len(new_messages)

                                self.logger.debug(
                                    "Storing final batch of %d messages "
                                    "from #%s (%d new)",
                                    len(processed_messages),
                                    channel_name,
                                    len(new_messages),
                                )
                                await self._store_messages_and_reactions(
                                    processed_messages
                                )
                        except Exception as exc:
                            self.logger.error(
                                "Failed to store final batch: %s", exc
                            )

                    if not got_messages:
                        self.logger.info(
                            "No more messages found in #%s for the current "
                            "time range",
                            channel_name,
                        )
                        break

                    await asyncio.sleep(0.1)

                self.logger.info(
                    "Finished initial fetch for #%s: %d messages fetched, "
                    "last message from %s",
                    channel_name,
                    message_counter,
                    (
                        last_message.created_at
                        if last_message
                        else "N/A"
                    ),
                )
            except Exception as exc:
                self.logger.error(
                    "Error fetching message history: %s", exc
                )
                raise

        # Check for newer messages after latest_date
        if latest_date:
            self.logger.info(
                "Searching for newer messages in #%s (after %s)...",
                channel_name,
                latest_date,
            )
            current_batch = []
            messages_found = 0
            async for message in channel.history(
                limit=None,
                after=latest_date,
                oldest_first=self.oldest_first,
            ):
                messages_found += 1
                if messages_found % 100 == 0:
                    self.logger.debug(
                        "Found %d newer messages in #%s",
                        messages_found,
                        channel_name,
                    )
                if cutoff_date and message.created_at < cutoff_date:
                    self.logger.debug(
                        "Reached cutoff date %s, stopping newer message search",
                        cutoff_date,
                    )
                    break

                if self._is_bot_summary_message(message, channel):
                    continue

                current_batch.append(message)
                message_counter += 1

                if len(current_batch) >= 100:
                    try:
                        processed_messages = []
                        for msg in current_batch:
                            processed_msg = await self._process_message(
                                msg, channel_id
                            )
                            if processed_msg:
                                processed_messages.append(processed_msg)

                        if processed_messages:
                            pre_existing = set(
                                msg["message_id"]
                                for msg in await self._db_operation(
                                    lambda db: db.get_messages_by_ids(
                                        [
                                            msg["message_id"]
                                            for msg in processed_messages
                                        ]
                                    )
                                )
                            )
                            new_messages = [
                                msg
                                for msg in processed_messages
                                if msg["message_id"] not in pre_existing
                            ]
                            new_message_count += len(new_messages)

                            self.logger.info(
                                "Storing batch of %d messages from #%s "
                                "(%d new, %d existing)",
                                len(processed_messages),
                                channel_name,
                                len(new_messages),
                                len(pre_existing),
                            )
                            await self._store_messages_and_reactions(
                                processed_messages
                            )
                        current_batch = []
                        await asyncio.sleep(0.1)
                    except Exception as exc:
                        self.logger.error(
                            "Failed to store batch: %s", exc
                        )

            if current_batch:
                try:
                    processed_messages = []
                    for msg in current_batch:
                        processed_msg = await self._process_message(
                            msg, channel_id
                        )
                        if processed_msg:
                            processed_messages.append(processed_msg)

                    if processed_messages:
                        pre_existing = set(
                            msg["message_id"]
                            for msg in await self._db_operation(
                                lambda db: db.get_messages_by_ids(
                                    [
                                        msg["message_id"]
                                        for msg in processed_messages
                                    ]
                                )
                            )
                        )
                        new_messages = [
                            msg
                            for msg in processed_messages
                            if msg["message_id"] not in pre_existing
                        ]
                        new_message_count += len(new_messages)

                        self.logger.info(
                            "Storing batch of %d messages from #%s "
                            "(%d new, %d existing)",
                            len(processed_messages),
                            channel_name,
                            len(new_messages),
                            len(pre_existing),
                        )
                        await self._store_messages_and_reactions(
                            processed_messages
                        )
                except Exception as exc:
                    self.logger.error(
                        "Failed to store batch: %s", exc
                    )

        # Search for older messages (only when NOT using --days)
        if not self.days_limit and earliest_date:
            self.logger.info(
                "Searching for older messages in #%s (before %s)...",
                channel_name,
                earliest_date,
            )
            current_batch = []
            messages_found = 0
            async for message in channel.history(
                limit=None,
                before=earliest_date,
                oldest_first=self.oldest_first,
            ):
                messages_found += 1
                if messages_found % 100 == 0:
                    self.logger.debug(
                        "Found %d older messages in #%s",
                        messages_found,
                        channel_name,
                    )
                if cutoff_date and message.created_at < cutoff_date:
                    continue

                if self._is_bot_summary_message(message, channel):
                    continue

                current_batch.append(message)
                message_counter += 1

                if len(current_batch) >= 100:
                    try:
                        processed_messages = []
                        for msg in current_batch:
                            processed_msg = await self._process_message(
                                msg, channel_id
                            )
                            if processed_msg:
                                processed_messages.append(processed_msg)

                        if processed_messages:
                            pre_existing = set(
                                msg["message_id"]
                                for msg in await self._db_operation(
                                    lambda db: db.get_messages_by_ids(
                                        [
                                            msg["message_id"]
                                            for msg in processed_messages
                                        ]
                                    )
                                )
                            )
                            new_messages = [
                                msg
                                for msg in processed_messages
                                if msg["message_id"] not in pre_existing
                            ]
                            new_message_count += len(new_messages)

                            self.logger.info(
                                "Storing batch of %d messages from #%s "
                                "(%d new, %d existing)",
                                len(processed_messages),
                                channel_name,
                                len(new_messages),
                                len(pre_existing),
                            )
                            await self._store_messages_and_reactions(
                                processed_messages
                            )
                        current_batch = []
                        await asyncio.sleep(0.1)
                    except Exception as exc:
                        self.logger.error(
                            "Failed to store batch: %s", exc
                        )

            if current_batch:
                try:
                    processed_messages = []
                    for msg in current_batch:
                        processed_msg = await self._process_message(
                            msg, channel_id
                        )
                        if processed_msg:
                            processed_messages.append(processed_msg)

                    if processed_messages:
                        pre_existing = set(
                            msg["message_id"]
                            for msg in await self._db_operation(
                                lambda db: db.get_messages_by_ids(
                                    [
                                        msg["message_id"]
                                        for msg in processed_messages
                                    ]
                                )
                            )
                        )
                        new_messages = [
                            msg
                            for msg in processed_messages
                            if msg["message_id"] not in pre_existing
                        ]
                        new_message_count += len(new_messages)

                        self.logger.debug(
                            "Storing batch of %d messages from #%s (%d new)",
                            len(processed_messages),
                            channel_name,
                            len(new_messages),
                        )
                        await self._store_messages_and_reactions(
                            processed_messages
                        )
                except Exception as exc:
                    self.logger.error(
                        "Failed to store batch: %s", exc
                    )

        # Gap scanning (only when NOT using --days)
        message_dates = None
        if not self.days_limit:
            message_dates = await self._db_operation(
                lambda db: db.get_message_dates(channel_id)
            )
        if message_dates:
            if cutoff_date:
                message_dates = [
                    d
                    for d in message_dates
                    if to_aware_utc(d) >= cutoff_date
                ]

            message_dates.sort(reverse=not self.oldest_first)
            gaps: List[Tuple[datetime, datetime]] = []
            for i in range(len(message_dates) - 1):
                current = to_aware_utc(message_dates[i])
                next_date = to_aware_utc(message_dates[i + 1])
                if self.oldest_first:
                    date_diff = (next_date - current).days
                else:
                    date_diff = (current - next_date).days
                if date_diff > 7:
                    if self.oldest_first:
                        gaps.append((current, next_date))
                    else:
                        gaps.append((next_date, current))

            if gaps:
                self.logger.info(
                    "Found %d gaps (>1 week) in message history for #%s",
                    len(gaps),
                    channel_name,
                )
                for start, end in gaps:
                    gap_message_count = 0
                    current_batch = []
                    self.logger.info(
                        "Searching for messages in #%s between %s and %s "
                        "(gap of %d days)",
                        channel_name,
                        start,
                        end,
                        abs((end - start).days),
                    )
                    async for message in channel.history(
                        limit=None,
                        after=start,
                        before=end,
                        oldest_first=self.oldest_first,
                    ):
                        if self._is_bot_summary_message(message, channel):
                            continue

                        current_batch.append(message)
                        gap_message_count += 1

                        if len(current_batch) >= 100:
                            try:
                                processed_messages = []
                                for msg in current_batch:
                                    processed_msg = (
                                        await self._process_message(
                                            msg, channel_id
                                        )
                                    )
                                    if processed_msg:
                                        processed_messages.append(
                                            processed_msg
                                        )

                                if processed_messages:
                                    pre_existing = set(
                                        msg["message_id"]
                                        for msg in await self._db_operation(
                                            lambda db: db.get_messages_by_ids(
                                                [
                                                    msg["message_id"]
                                                    for msg in processed_messages
                                                ]
                                            )
                                        )
                                    )
                                    new_messages = [
                                        msg
                                        for msg in processed_messages
                                        if msg["message_id"]
                                        not in pre_existing
                                    ]
                                    new_message_count += len(new_messages)

                                    self.logger.debug(
                                        "Storing batch of %d messages from "
                                        "gap in #%s (%d new)",
                                        len(processed_messages),
                                        channel_name,
                                        len(new_messages),
                                    )
                                    await self._store_messages_and_reactions(
                                        processed_messages
                                    )
                                    if gap_message_count % 100 == 0:
                                        self.logger.debug(
                                            "Found %d messages in current gap "
                                            "for #%s",
                                            gap_message_count,
                                            channel_name,
                                        )

                                current_batch = []
                                await asyncio.sleep(0.1)
                            except Exception as exc:
                                self.logger.error(
                                    "Failed to store batch: %s", exc
                                )

                    if current_batch:
                        try:
                            processed_messages = []
                            for msg in current_batch:
                                processed_msg = (
                                    await self._process_message(
                                        msg, channel_id
                                    )
                                )
                                if processed_msg:
                                    processed_messages.append(processed_msg)

                            if processed_messages:
                                pre_existing = set(
                                    msg["message_id"]
                                    for msg in await self._db_operation(
                                        lambda db: db.get_messages_by_ids(
                                            [
                                                msg["message_id"]
                                                for msg in processed_messages
                                            ]
                                        )
                                    )
                                )
                                new_messages = [
                                    msg
                                    for msg in processed_messages
                                    if msg["message_id"] not in pre_existing
                                ]
                                new_message_count += len(new_messages)

                                self.logger.debug(
                                    "Storing final gap batch of %d messages "
                                    "from #%s (%d new)",
                                    len(processed_messages),
                                    channel_name,
                                    len(new_messages),
                                )
                                await self._store_messages_and_reactions(
                                    processed_messages
                                )
                        except Exception as exc:
                            self.logger.error(
                                "Failed to store batch: %s", exc
                            )

                    self.logger.info(
                        "Finished gap search in #%s, found %d messages",
                        channel_name,
                        gap_message_count,
                    )

        self.logger.info(
            "Found %d new messages to archive in #%s",
            new_message_count,
            channel_name,
        )
        self.logger.info(
            "Archive complete for #%s - %d new messages saved to Supabase",
            channel_name,
            new_message_count,
        )
        self.total_messages_archived += new_message_count

        channel_duration = (
            datetime.now(timezone.utc) - channel_start_time
        ).total_seconds()
        self.logger.info(
            "Finished archive of #%s in %.2fs",
            channel_name,
            channel_duration,
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> ArchiveResult:
        """Execute the archive and return a structured result.

        Starts the DB worker thread, resolves targets, iterates
        ``archive_channel`` with per-channel error handling, shuts down
        the DB worker, and returns an ``ArchiveResult``.

        ``ArchiveResult.success`` is True when at least one channel was
        archived without a fatal error.  It is False only when ALL channels
        failed or the run encountered a fatal error (e.g. guild not found).
        """
        start_mono = time.monotonic()

        try:
            # Resolve targets — fatal if the guild can't be found
            items_to_process = await self._resolve_targets()
        except Exception as exc:
            self.logger.error(
                "Fatal error resolving targets for guild %d: %s",
                self.guild_id,
                exc,
                exc_info=True,
            )
            self._shutdown_db_worker()
            duration = time.monotonic() - start_mono
            return ArchiveResult(
                success=False,
                messages_archived=0,
                duration_seconds=duration,
                fatal_error=str(exc),
            )

        total_items = len(items_to_process)
        if total_items == 0:
            self.logger.info("No channels/threads to archive — nothing to do")
            self._shutdown_db_worker()
            duration = time.monotonic() - start_mono
            return ArchiveResult(
                success=True,  # nothing to do is not a failure
                messages_archived=0,
                duration_seconds=duration,
            )

        self.logger.info(
            "Processing %d items across guild %d", total_items, self.guild_id
        )

        any_channel_succeeded = False
        per_channel_errors: Dict[int, str] = {}

        for index, (item_type, item, parent_name) in enumerate(
            items_to_process
        ):
            item_index = index + 1

            if item_type == "channel":
                log_prefix = (
                    "Processing Channel %d/%d:" % (item_index, total_items)
                )
                self.logger.info(
                    "%s #%s", log_prefix, getattr(item, "name", str(item.id))
                )
            elif item_type == "thread":
                log_prefix = (
                    "Processing Thread %d/%d in #%s:"
                    % (item_index, total_items, parent_name or "?")
                )
                self.logger.info(
                    "%s #%s", log_prefix, getattr(item, "name", str(item.id))
                )
            elif item_type == "forum_thread":
                log_prefix = (
                    "Processing Forum Thread %d/%d in forum #%s:"
                    % (item_index, total_items, parent_name or "?")
                )
                self.logger.info(
                    "%s #%s", log_prefix, getattr(item, "name", str(item.id))
                )
            else:
                self.logger.warning(
                    "Skipping unknown item type at index %d", item_index
                )
                continue

            try:
                await self.archive_channel(item.id)
                any_channel_succeeded = True
            except Exception as exc:
                ch_name = getattr(item, "name", str(item.id))
                self.logger.error(
                    "Channel %s (%d) failed: %s",
                    ch_name,
                    item.id,
                    exc,
                    exc_info=True,
                )
                per_channel_errors[item.id] = str(exc)

            self.logger.info(
                "Running Total New Messages Archived: %d",
                self.total_messages_archived,
            )

        # Shut down the DB worker
        self._shutdown_db_worker()

        duration = time.monotonic() - start_mono
        self.logger.info(
            "Archive complete for guild %d — %d total new messages archived",
            self.guild_id,
            self.total_messages_archived,
        )
        self.logger.info(
            "FINAL TOTAL: %d new messages archived to Supabase",
            self.total_messages_archived,
        )

        # Merge per-channel errors into the instance dict for inspection
        self.per_channel_errors.update(per_channel_errors)

        success = any_channel_succeeded

        return ArchiveResult(
            success=success,
            messages_archived=self.total_messages_archived,
            duration_seconds=duration,
            per_channel_errors=per_channel_errors,
        )
