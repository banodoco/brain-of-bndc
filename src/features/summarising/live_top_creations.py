"""Independent top-creations service for the live-update system."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any, Dict, List, Optional

import discord

from src.common.db_handler import DatabaseHandler
from src.common.urls import message_jump_url

logger = logging.getLogger("DiscordBot")


def _discord_safe_content(content: str) -> str:
    text = re.sub(r"<@!?\d+>", "a member", content or "")
    text = re.sub(r"<@&\d+>", "a role", text)
    text = re.sub(r"@(?=(everyone|here)\b)", "at ", text, flags=re.IGNORECASE)
    text = re.sub(r"@(?=[A-Za-z0-9_])", "at ", text)
    return text


async def _send_without_mentions(channel: Any, content: str) -> Any:
    safe_content = _discord_safe_content(content)
    try:
        return await channel.send(safe_content, allowed_mentions=discord.AllowedMentions.none())
    except TypeError:
        return await channel.send(safe_content)


class LiveTopCreations:
    """Find, post, checkpoint, and audit top creations without daily summaries."""

    DEFAULT_MAX_MESSAGES = 300
    MAX_POSTS_SAFETY_BELT = 50
    DISCORD_MESSAGE_LIMIT = 2000
    VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")
    IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def __init__(
        self,
        db_handler: DatabaseHandler,
        *,
        bot: Optional[Any] = None,
        guild_id: Optional[int] = None,
        art_channel_id: Optional[int] = None,
        logger_instance: Optional[logging.Logger] = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        min_reactions: int = 5,
        dry_run_lookback_hours: int = 6,
        environment: str = "prod",
    ):
        self.db = db_handler
        self.bot = bot
        self.guild_id = guild_id
        self.art_channel_id = art_channel_id
        self.logger = logger_instance or logger
        self.max_messages = max_messages
        self.min_reactions = min_reactions
        self.environment = environment
        self.dry_run_lookback_hours = dry_run_lookback_hours

    async def run_once(self, trigger: str = "scheduled") -> Dict[str, Any]:
        guild_id = self._resolve_guild_id()
        live_channel_id = self._resolve_top_channel_id(guild_id)
        art_channel_id = self._resolve_art_channel_id(guild_id)
        checkpoint_key = self._checkpoint_key(guild_id, live_channel_id)

        checkpoint = await self._call_db(
            self.db.get_live_top_creation_checkpoint, checkpoint_key,
            environment=self.environment,
        )

        # The DB schema for live_top_creation_checkpoints stores `last_source_*`
        # columns, but storage_handler.get_archived_messages_after_checkpoint
        # filters on `last_message_id` / `last_message_created_at`. Mirror the
        # keys so the query actually advances. Without this, the filter falls
        # through to "no filter at all" and we silently re-fetch the oldest 300
        # messages every run.
        if isinstance(checkpoint, dict):
            if checkpoint.get("last_message_id") is None and checkpoint.get("last_source_message_id") is not None:
                checkpoint["last_message_id"] = checkpoint["last_source_message_id"]
            if not checkpoint.get("last_message_created_at") and checkpoint.get("last_source_created_at"):
                checkpoint["last_message_created_at"] = checkpoint["last_source_created_at"]

        # Dev: synthesize an initial checkpoint if none exists
        if checkpoint is None and self.environment == "dev":
            lookback = max(1, int(self.dry_run_lookback_hours or 6))
            checkpoint = {
                "last_message_created_at": (
                    datetime.now(timezone.utc) - timedelta(hours=lookback)
                ).isoformat()
            }
            self.logger.info(
                "[LiveTopCreations] dev: synthesized initial checkpoint with %sh lookback",
                lookback,
            )

        run = await self._call_db(
            self.db.create_live_top_creation_run,
            {
                "guild_id": guild_id,
                "trigger": trigger,
                "status": "running",
                "checkpoint_before": checkpoint or {},
                "metadata": {
                    "live_channel_id": live_channel_id,
                    "art_channel_id": art_channel_id,
                    "max_messages": self.max_messages,
                    "min_reactions": self.min_reactions,
                },
            },
            environment=self.environment,
        )
        if not run:
            return {"status": "failed", "error": "failed_to_create_top_creation_run"}

        run_id = run["top_creation_run_id"]
        try:
            messages = await self._call_db(
                self.db.get_archived_messages_after_checkpoint,
                checkpoint,
                guild_id,
                None,
                self.max_messages,
            )
            candidates = self._select_candidates(messages, guild_id, art_channel_id)
            posted_keys = set(((checkpoint or {}).get("state") or {}).get("posted_duplicate_keys") or [])
            pending = [candidate for candidate in candidates if candidate["duplicate_key"] not in posted_keys]
            skipped_count = len(candidates) - len(pending)

            if not pending:
                checkpoint_after = await self._write_checkpoint(
                    checkpoint_key,
                    guild_id,
                    live_channel_id,
                    run_id,
                    messages,
                    posted_keys,
                    "skipped",
                    skipped_count=skipped_count,
                )
                await self._call_db(
                    self.db.update_live_top_creation_run,
                    run_id,
                    {
                        "status": "skipped",
                        "candidate_count": len(candidates),
                        "posted_count": 0,
                        "skipped_count": skipped_count,
                        "checkpoint_after": checkpoint_after or checkpoint or {},
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    guild_id,
                    environment=self.environment,
                )
                await self._publish_dev_debug_report(
                    trigger=trigger,
                    run_id=run_id,
                    live_channel_id=live_channel_id,
                    messages=messages,
                    candidates=candidates,
                    posted_count=0,
                    checkpoint_skip_count=skipped_count,
                    db_dedupe_skip_count=0,
                    status="skipped",
                    checkpoint_before=checkpoint,
                    checkpoint_after=checkpoint_after,
                )
                return {
                    "run_id": run_id,
                    "status": "skipped",
                    "candidate_count": len(candidates),
                    "posted_count": 0,
                    "skipped_count": skipped_count,
                }

            channel = await self._resolve_channel(live_channel_id)
            posts: List[Dict[str, Any]] = []
            db_skip_count = 0
            for candidate in pending[: LiveTopCreations.MAX_POSTS_SAFETY_BELT]:
                post = await self._publish_candidate(channel, candidate, run_id, guild_id, live_channel_id)
                if post.get("status") == "skipped_duplicate":
                    db_skip_count += 1
                    continue
                posts.append(post)
                posted_keys.add(candidate["duplicate_key"])

            checkpoint_after = await self._write_checkpoint(
                checkpoint_key,
                guild_id,
                live_channel_id,
                run_id,
                messages,
                posted_keys,
                "completed",
                skipped_count=skipped_count,
            )
            await self._call_db(
                self.db.update_live_top_creation_run,
                run_id,
                {
                    "status": "completed",
                    "candidate_count": len(candidates),
                    "posted_count": len(posts),
                    "skipped_count": skipped_count + db_skip_count,
                    "checkpoint_after": checkpoint_after or {},
                    "metadata": {
                        "live_channel_id": live_channel_id,
                        "art_channel_id": art_channel_id,
                        "db_skip_count": db_skip_count,
                        "posted_duplicate_keys": [post.get("duplicate_key") for post in posts],
                    },
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
                guild_id,
                environment=self.environment,
            )
            if not posts:
                await self._publish_dev_debug_report(
                    trigger=trigger,
                    run_id=run_id,
                    live_channel_id=live_channel_id,
                    messages=messages,
                    candidates=candidates,
                    posted_count=0,
                    checkpoint_skip_count=skipped_count,
                    db_dedupe_skip_count=db_skip_count,
                    status="completed",
                    checkpoint_before=checkpoint,
                    checkpoint_after=checkpoint_after,
                )

            return {
                "run_id": run_id,
                "status": "completed",
                "candidate_count": len(candidates),
                "posted_count": len(posts),
                "skipped_count": skipped_count + db_skip_count,
                "post_ids": [post.get("top_creation_post_id") for post in posts],
            }

        except Exception as exc:
            self.logger.error("[LiveTopCreations] run_once failed: %s", exc, exc_info=True)
            await self._call_db(
                self.db.update_live_top_creation_run,
                run_id,
                {
                    "status": "failed",
                    "error_message": str(exc),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
                guild_id,
                environment=self.environment,
            )
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

    def _select_candidates(
        self,
        messages: List[Dict[str, Any]],
        guild_id: Optional[int],
        art_channel_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for message in messages or []:
            attachments = self._attachments(message)
            if not attachments:
                continue
            channel_name = str(message.get("channel_name") or "").lower()
            if "nsfw" in channel_name:
                continue
            reaction_count = self._reaction_count(message)
            if reaction_count < self.min_reactions:
                continue
            source_kind = self._source_kind(message, attachments, art_channel_id)
            if not source_kind:
                continue
            media_refs = self._media_refs(attachments, source_kind)
            message_id = str(message.get("message_id"))
            duplicate_key = f"{source_kind}:{guild_id or 'unknown'}:{message_id}"
            candidates.append({
                "source_kind": source_kind,
                "source_id": duplicate_key,
                "source_message_id": message.get("message_id"),
                "source_channel_id": message.get("channel_id"),
                "duplicate_key": duplicate_key,
                "title": "Top Art Share" if source_kind == "top_art_share" else "Top Generation",
                "body": self._candidate_body(message, source_kind, reaction_count),
                "media_refs": media_refs,
                "reaction_count": reaction_count,
                "created_at": message.get("created_at"),
                "thread_id": message.get("thread_id"),
                "content": message.get("content") or "",
                "channel_name": message.get("channel_name"),
                "author_name": self._author_name(message),
            })
        return sorted(
            candidates,
            key=lambda item: (int(item.get("reaction_count") or 0), str(item.get("created_at") or "")),
            reverse=True,
        )

    async def _publish_candidate(
        self,
        channel: Any,
        candidate: Dict[str, Any],
        run_id: str,
        guild_id: Optional[int],
        live_channel_id: Optional[int],
    ) -> Dict[str, Any]:
        # Pre-publish dedupe via DB lookup: "send only once" guarantee
        existing = await self._call_db(
            self.db.get_live_top_creation_post_by_duplicate_key,
            candidate["duplicate_key"],
            environment=self.environment,
        )
        if existing:
            self.logger.info(
                "[LiveTopCreations] skipping duplicate_key=%s — already posted in DB",
                candidate["duplicate_key"],
            )
            return {
                "top_creation_post_id": existing.get("top_creation_post_id"),
                "duplicate_key": candidate.get("duplicate_key"),
                "discord_message_ids": [],
                "status": "skipped_duplicate",
            }

        content = self._format_post(candidate, guild_id)
        discord_message_ids: List[str] = []
        for chunk in self._split_discord_content(content):
            sent = await _send_without_mentions(channel, chunk)
            message_id = self._extract_discord_message_id(sent)
            if message_id is None:
                raise RuntimeError("top-creations Discord send returned no message id")
            discord_message_ids.append(message_id)

        post = await self._call_db(
            self.db.store_live_top_creation_post,
            {
                "top_creation_run_id": run_id,
                "guild_id": guild_id,
                "source_kind": candidate.get("source_kind"),
                "source_id": candidate.get("source_id"),
                "source_message_id": candidate.get("source_message_id"),
                "source_channel_id": candidate.get("source_channel_id"),
                "live_channel_id": live_channel_id,
                "title": candidate.get("title"),
                "body": candidate.get("body"),
                "media_refs": candidate.get("media_refs") or [],
                "duplicate_key": candidate.get("duplicate_key"),
                "discord_message_ids": discord_message_ids,
                "status": "posted",
                "posted_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {
                    "reaction_count": candidate.get("reaction_count"),
                    "created_at": candidate.get("created_at"),
                },
            },
            environment=self.environment,
        )
        if not post:
            raise RuntimeError("failed to persist top-creations post")
        return post

    async def _write_checkpoint(
        self,
        checkpoint_key: str,
        guild_id: Optional[int],
        live_channel_id: Optional[int],
        run_id: str,
        messages: List[Dict[str, Any]],
        posted_keys: set,
        status: str,
        *,
        skipped_count: int,
    ) -> Optional[Dict[str, Any]]:
        newest = self._newest_message(messages)
        return await self._call_db(
            self.db.upsert_live_top_creation_checkpoint,
            {
                "checkpoint_key": checkpoint_key,
                "guild_id": guild_id,
                "channel_id": live_channel_id,
                "last_source_id": str(newest.get("message_id")) if newest else None,
                "last_source_message_id": newest.get("message_id") if newest else None,
                "last_source_created_at": newest.get("created_at") if newest else None,
                "last_run_id": run_id,
                "state": {
                    "last_status": status,
                    "posted_duplicate_keys": sorted(posted_keys)[-500:],
                    "skipped_count": skipped_count,
                },
            },
            environment=self.environment,
        )

    def _resolve_guild_id(self) -> Optional[int]:
        if self.guild_id is not None:
            return self.guild_id
        server_config = getattr(self.db, "server_config", None)
        if server_config and hasattr(server_config, "resolve_guild_id"):
            return server_config.resolve_guild_id(require_write=True)
        return None

    def _resolve_top_channel_id(self, guild_id: Optional[int]) -> Optional[int]:
        """Resolve the channel ID for top-creations posts (summary channel only)."""
        if self.environment == "dev":
            env_value = os.getenv("DEV_SUMMARY_CHANNEL_ID") or os.getenv("DEV_LIVE_UPDATE_CHANNEL_ID")
            if env_value:
                return int(env_value)
        server_config = getattr(self.db, "server_config", None)
        if server_config and guild_id is not None:
            if hasattr(server_config, "get_server_field"):
                summary_channel = server_config.get_server_field(guild_id, "summary_channel_id", cast=int)
                if summary_channel:
                    return summary_channel
            if hasattr(server_config, "get_server"):
                server = server_config.get_server(guild_id)
                if server and server.get("summary_channel_id"):
                    return int(server["summary_channel_id"])
            if hasattr(server_config, "get_first_server_with_field"):
                server = server_config.get_first_server_with_field("summary_channel_id", require_write=True)
                if server and server.get("summary_channel_id"):
                    return int(server["summary_channel_id"])
        return None

    def _resolve_art_channel_id(self, guild_id: Optional[int]) -> Optional[int]:
        if self.art_channel_id is not None:
            return self.art_channel_id
        if self.environment == "dev":
            env_value = os.getenv("DEV_ART_CHANNEL_ID")
            if env_value:
                return int(env_value)
        server_config = getattr(self.db, "server_config", None)
        if server_config and guild_id is not None and hasattr(server_config, "get_server_field"):
            art_channel = server_config.get_server_field(guild_id, "art_channel_id", cast=int)
            if art_channel:
                return art_channel
        env_value = os.getenv("ART_CHANNEL_ID")
        return int(env_value) if env_value else None

    async def _publish_dev_debug_report(
        self,
        *,
        trigger: str,
        run_id: Optional[str],
        live_channel_id: Optional[int],
        messages: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        posted_count: int,
        checkpoint_skip_count: int,
        db_dedupe_skip_count: int,
        status: str,
        checkpoint_before: Optional[Dict[str, Any]] = None,
        checkpoint_after: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Dev-only Discord post explaining a run that produced zero new top-creation posts."""
        if self.environment != "dev":
            return
        try:
            channel = await self._resolve_channel(live_channel_id)
        except Exception as exc:
            self.logger.warning(
                "[LiveTopCreations] dev debug report skipped — could not resolve channel: %s",
                exc,
            )
            return

        try:
            messages = messages or []
            candidates = candidates or []

            description_bits = [f"Run ended `{status}` with `{posted_count}` new posts."]
            if checkpoint_skip_count or db_dedupe_skip_count:
                description_bits.append(
                    f"Skipped by checkpoint: `{checkpoint_skip_count}` · by DB dedupe: `{db_dedupe_skip_count}`"
                )

            embed = discord.Embed(
                title=f"[dev] top-creations: no new posts ({status})",
                description="\n".join(description_bits),
                color=0x808080,
            )

            # --- field: summary ---
            embed.add_field(
                name="summary",
                value="\n".join([
                    f"trigger: `{trigger}`",
                    f"environment: `{self.environment}`",
                    f"run_id: `{run_id}`",
                    f"min_reactions: `{self.min_reactions}`",
                    f"source messages: `{len(messages)}`",
                    f"candidates: `{len(candidates)}`",
                    f"posted: `{posted_count}`",
                    f"checkpoint skips: `{checkpoint_skip_count}`",
                    f"db dedupe skips: `{db_dedupe_skip_count}`",
                ])[:1024],
                inline=False,
            )

            # --- field: channel coverage (from source messages) ---
            channel_counter: Counter[str] = Counter(
                (m.get("channel_name") or "?") for m in messages
            )
            if channel_counter:
                top_channels = channel_counter.most_common(5)
                lines = [f"`#{name}` · {count}" for name, count in top_channels]
                more = max(0, len(channel_counter) - len(top_channels))
                if more:
                    lines.append(f"… (+{more} more)")
                embed.add_field(
                    name=f"channels ({len(channel_counter)} total)",
                    value="\n".join(lines)[:1024],
                    inline=True,
                )

            # --- field: authors ---
            author_counter: Counter[str] = Counter(
                self._author_name(m) for m in messages
            )
            if author_counter:
                top_authors = author_counter.most_common(5)
                lines = [f"{name} · {count}" for name, count in top_authors]
                more = max(0, len(author_counter) - len(top_authors))
                if more:
                    lines.append(f"… (+{more} more)")
                embed.add_field(
                    name=f"authors ({len(author_counter)} total)",
                    value="\n".join(lines)[:1024],
                    inline=True,
                )

            # --- field: reaction distribution histogram ---
            reaction_counter: Counter[int] = Counter(
                int(c.get("reaction_count") or 0) for c in candidates
            )
            if reaction_counter:
                ordered = sorted(reaction_counter.items())
                histogram = ", ".join(
                    f"{rc}: {count} cand{'s' if count != 1 else ''}"
                    for rc, count in ordered
                )
                embed.add_field(
                    name="reaction distribution",
                    value=f"`{histogram}`"[:1024],
                    inline=False,
                )

            # --- field: near-miss ---
            if candidates and posted_count == 0:
                top_miss = max(
                    candidates,
                    key=lambda c: int(c.get("reaction_count") or 0),
                    default=None,
                )
                if top_miss:
                    miss_value = (
                        f"kind: `{top_miss.get('source_kind') or '?'}`\n"
                        f"reactions: `{top_miss.get('reaction_count') or 0}`\n"
                        f"author: `{top_miss.get('author_name') or '?'}`\n"
                        f"channel: `#{top_miss.get('channel_name') or '?'}`\n"
                        f"dup_key: `{str(top_miss.get('duplicate_key') or '')[:60]}`\n"
                        f"title: `{(top_miss.get('title') or '?')[:80]}`"
                    )
                    embed.add_field(
                        name="near-miss (top by reactions)",
                        value=miss_value[:1024],
                        inline=False,
                    )

            # --- field: per-candidate listing ---
            if candidates:
                lines: List[str] = []
                for candidate in candidates[:8]:
                    kind = candidate.get("source_kind") or "?"
                    reactions = candidate.get("reaction_count") or 0
                    author = candidate.get("author_name") or "?"
                    channel_name = candidate.get("channel_name") or "?"
                    skip_reason = candidate.get("skip_reason") or "pending"
                    lines.append(
                        f"• `{kind}` · {reactions} react · {author} · `#{channel_name}` · {skip_reason}"
                    )
                if len(candidates) > 8:
                    lines.append(f"… (+{len(candidates) - 8} more)")
                self._add_chunked_field(
                    embed,
                    name=f"candidates ({len(candidates)})",
                    lines=lines,
                )

            # Top-creations is heuristic (no LLM agent reasoning to attach);
            # the embed has all the relevant detail inline. No file attachment.
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.logger.info(
                "[LiveTopCreations] dev debug report posted for run_id=%s status=%s",
                run_id,
                status,
            )
        except Exception as exc:
            self.logger.warning(
                "[LiveTopCreations] dev debug report failed: %s",
                exc,
                exc_info=True,
            )

    @staticmethod
    def _add_chunked_field(embed: discord.Embed, *, name: str, lines: List[str]) -> None:
        chunks: List[str] = []
        current = ""
        for line in lines:
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) > 1024:
                if current:
                    chunks.append(current)
                if len(line) > 1024:
                    chunks.append(line[:1020] + "…")
                    current = ""
                else:
                    current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        for idx, chunk in enumerate(chunks):
            embed.add_field(
                name=name if idx == 0 else f"{name} (cont.)",
                value=chunk,
                inline=False,
            )

    def _build_trace_attachment(
        self,
        *,
        run_id: Optional[str],
        trigger: str,
        status: str,
        messages: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        posted_count: int,
        checkpoint_skip_count: int,
        db_dedupe_skip_count: int,
        checkpoint_before: Optional[Dict[str, Any]],
        checkpoint_after: Optional[Dict[str, Any]],
    ) -> Optional[discord.File]:
        try:
            channel_counter: Counter[str] = Counter(
                (m.get("channel_name") or "?") for m in messages
            )
            author_counter: Counter[str] = Counter(
                self._author_name(m) for m in messages
            )

            def _sample_message(m: Dict[str, Any]) -> Dict[str, Any]:
                return {
                    "message_id": m.get("message_id"),
                    "channel_name": m.get("channel_name"),
                    "author_name": self._author_name(m),
                    "created_at": m.get("created_at"),
                    "reaction_count": self._reaction_count(m),
                    "content": (m.get("content") or "")[:240],
                    "attachments_count": len(self._attachments(m)),
                }

            payload: Dict[str, Any] = {
                "run_id": run_id,
                "environment": "dev",
                "trigger": trigger,
                "status": status,
                "min_reactions": self.min_reactions,
                "counts": {
                    "source_messages": len(messages),
                    "candidates": len(candidates),
                    "posted": posted_count,
                    "checkpoint_skip": checkpoint_skip_count,
                    "db_dedupe_skip": db_dedupe_skip_count,
                },
                "messages_summary": {
                    "count": len(messages),
                    "channels": dict(channel_counter),
                    "authors_top_15": dict(author_counter.most_common(15)),
                    "sample": [_sample_message(m) for m in messages[:15]],
                },
                "candidates": [
                    {
                        "source_kind": c.get("source_kind"),
                        "source_id": c.get("source_id"),
                        "source_message_id": c.get("source_message_id"),
                        "source_channel_id": c.get("source_channel_id"),
                        "channel_name": c.get("channel_name"),
                        "author_name": c.get("author_name"),
                        "title": c.get("title"),
                        "body": c.get("body"),
                        "duplicate_key": c.get("duplicate_key"),
                        "reaction_count": c.get("reaction_count"),
                        "created_at": c.get("created_at"),
                        "media_refs": c.get("media_refs"),
                        "content": (c.get("content") or "")[:500],
                    }
                    for c in candidates
                ],
                "checkpoint_before": checkpoint_before,
                "checkpoint_after": checkpoint_after,
            }

            data = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
            if len(data) > 7 * 1024 * 1024:
                payload["messages_summary"]["sample"] = payload["messages_summary"]["sample"][:5]
                data = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
            gzipped = gzip.compress(data)
            return discord.File(io.BytesIO(gzipped), filename=f"top_creations_trace_{run_id}.json.gz")
        except Exception as exc:
            self.logger.warning(
                "[LiveTopCreations] failed to build trace attachment: %s", exc, exc_info=True,
            )
            return None

    async def _resolve_channel(self, channel_id: Optional[int]) -> Any:
        if self.bot is None:
            raise RuntimeError("bot is required for top-creations publishing")
        if channel_id is None:
            raise RuntimeError("top-creations channel id is required")
        channel = self.bot.get_channel(int(channel_id)) if hasattr(self.bot, "get_channel") else None
        if channel is None and hasattr(self.bot, "fetch_channel"):
            channel = await self.bot.fetch_channel(int(channel_id))
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError(f"could not resolve top-creations channel {channel_id}")
        return channel

    @staticmethod
    def _checkpoint_key(guild_id: Optional[int], channel_id: Optional[int]) -> str:
        return f"live_top_creations:{guild_id or 'unknown'}:{channel_id or 'unknown'}"

    @staticmethod
    async def _call_db(method: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    @classmethod
    def _source_kind(
        cls,
        message: Dict[str, Any],
        attachments: List[Dict[str, Any]],
        art_channel_id: Optional[int],
    ) -> Optional[str]:
        if art_channel_id and int(message.get("channel_id") or 0) == int(art_channel_id):
            if any(
                cls._attachment_has_extension(attachment, cls.IMAGE_EXTENSIONS + cls.VIDEO_EXTENSIONS)
                for attachment in attachments
            ):
                return "top_art_share"
        if any(cls._attachment_has_extension(attachment, cls.VIDEO_EXTENSIONS) for attachment in attachments):
            return "top_generation"
        return None

    @staticmethod
    def _attachments(message: Dict[str, Any]) -> List[Dict[str, Any]]:
        attachments = message.get("attachments") or []
        if isinstance(attachments, str):
            try:
                attachments = json.loads(attachments)
            except json.JSONDecodeError:
                attachments = []
        return [attachment for attachment in attachments if isinstance(attachment, dict)]

    @classmethod
    def _media_refs(cls, attachments: List[Dict[str, Any]], source_kind: str = "top_generation") -> List[Dict[str, Any]]:
        extensions = cls.IMAGE_EXTENSIONS + cls.VIDEO_EXTENSIONS if source_kind == "top_art_share" else cls.VIDEO_EXTENSIONS
        refs: List[Dict[str, Any]] = []
        for attachment in attachments:
            if not cls._attachment_has_extension(attachment, extensions):
                continue
            if attachment.get("url") or attachment.get("proxy_url"):
                refs.append({
                    "kind": "attachment",
                    "url": attachment.get("url") or attachment.get("proxy_url"),
                    "content_type": attachment.get("content_type"),
                    "filename": attachment.get("filename"),
                })
        return refs

    @staticmethod
    def _reaction_count(message: Dict[str, Any]) -> int:
        if message.get("reaction_count") is not None:
            try:
                return int(message.get("reaction_count") or 0)
            except (TypeError, ValueError):
                pass
        reactors = message.get("reactors") or []
        if isinstance(reactors, str):
            try:
                reactors = json.loads(reactors)
            except json.JSONDecodeError:
                reactors = []
        return len(reactors) if isinstance(reactors, list) else 0

    @staticmethod
    def _attachment_has_extension(attachment: Dict[str, Any], extensions: tuple) -> bool:
        filename = (attachment.get("filename") or attachment.get("url") or "").lower()
        return any(filename.endswith(extension) for extension in extensions)

    @staticmethod
    def _candidate_body(message: Dict[str, Any], source_kind: str, reaction_count: int) -> str:
        label = "Top art share" if source_kind == "top_art_share" else "Top generation"
        content = (message.get("content") or "").strip()
        if content:
            content = f"\n> {content[:240]}"
        return f"{label} with {reaction_count} reactions.{content}"

    @classmethod
    def _format_post(cls, candidate: Dict[str, Any], guild_id: Optional[int]) -> str:
        author = candidate.get("author_name") or "Unknown"
        channel_name = candidate.get("channel_name")
        lines = [f"By **{author}**" + (f" in #{channel_name}" if channel_name else "")]
        reaction_count = int(candidate.get("reaction_count") or 0)
        lines.append(f"🔥 {reaction_count} unique reactions")
        content = cls._clean_quote(candidate.get("content") or "")
        if content:
            lines.append(f'> "{content}"')
        for ref in candidate.get("media_refs") or []:
            if ref.get("url"):
                lines.append(str(ref["url"]))
                break
        if guild_id and candidate.get("source_channel_id") and candidate.get("source_message_id"):
            lines.append(
                f"🔗 Original post: {message_jump_url(guild_id, candidate['source_channel_id'], candidate['source_message_id'], thread_id=candidate.get('thread_id'))}"
            )
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _clean_quote(content: str, limit: int = 150) -> str:
        text = re.sub(r"\s+", " ", content or "").strip()
        return text[:limit].strip()

    @staticmethod
    def _author_name(message: Dict[str, Any]) -> str:
        snapshot = message.get("author_context_snapshot") or {}
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except json.JSONDecodeError:
                snapshot = {}
        if isinstance(snapshot, dict):
            for key in ("server_nick", "global_name", "display_name", "username"):
                value = snapshot.get(key)
                if value:
                    return str(value)
        return str(message.get("author_name") or message.get("author_id") or "Unknown")

    @classmethod
    def _split_discord_content(cls, content: str, limit: int = DISCORD_MESSAGE_LIMIT) -> List[str]:
        clean = (content or "").strip()
        if not clean:
            return ["(empty top-creation post)"]
        if len(clean) <= limit:
            return [clean]
        chunks: List[str] = []
        while clean:
            if len(clean) <= limit:
                chunks.append(clean)
                break
            split_at = clean.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = clean.rfind(" ", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(clean[:split_at].strip())
            clean = clean[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _extract_discord_message_id(sent_message: Any) -> Optional[str]:
        if sent_message is None:
            return None
        if isinstance(sent_message, dict):
            message_id = sent_message.get("id") or sent_message.get("message_id")
        else:
            message_id = getattr(sent_message, "id", None)
        return str(message_id) if message_id is not None else None

    @staticmethod
    def _newest_message(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not messages:
            return None
        return max(
            messages,
            key=lambda msg: (
                str(msg.get("created_at") or ""),
                int(msg.get("message_id") or 0),
            ),
        )
