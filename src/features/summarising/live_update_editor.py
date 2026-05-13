"""Agentic live-update editor service.

This module owns the active editorial loop that replaces the legacy daily
summary batch and appends accepted updates into the configured live feed.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord

from src.common.db_handler import DatabaseHandler
from src.common.urls import message_jump_url
from src.features.summarising.live_update_prompts import LiveUpdateCandidateGenerator

logger = logging.getLogger("DiscordBot")


def _discord_safe_content(content: str) -> str:
    """Prevent archived source text from tagging people, roles, or everyone."""
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


class LiveUpdatePublishError(RuntimeError):
    """Publishing failed after zero or more Discord sends."""

    def __init__(self, message: str, discord_message_ids: Optional[List[str]] = None):
        super().__init__(message)
        self.discord_message_ids = discord_message_ids or []


class LiveUpdateEditor:
    """Scheduled editor that converts newly archived messages into update decisions."""

    DEFAULT_MAX_MESSAGES = 1000
    DEFAULT_MAX_CANDIDATES = 12
    DISCORD_MESSAGE_LIMIT = 2000
    DEFAULT_MAX_PUBLISH_PER_RUN = None
    _CORE_CHECKLIST_KEYS = (
        "source_verified",
        "new_information_identified",
        "prior_updates_checked",
        "duplicate_checked",
        "public_value_clear",
        "risk_checked",
        "publish_format_ready",
    )

    def __init__(
        self,
        db_handler: DatabaseHandler,
        *,
        bot: Optional[Any] = None,
        guild_id: Optional[int] = None,
        live_channel_id: Optional[int] = None,
        logger_instance: Optional[logging.Logger] = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        llm_client: Optional[Any] = None,
        candidate_generator: Optional[LiveUpdateCandidateGenerator] = None,
        dry_run_lookback_hours: int = 6,
        max_publish_per_run: Optional[int] = None,
        environment: str = "prod",
    ):
        self.db = db_handler
        self.bot = bot
        self.logger = logger_instance or logger
        self.guild_id = guild_id
        self.live_channel_id = live_channel_id
        self.max_messages = max_messages
        self.max_candidates = max_candidates
        self.environment = environment
        self.dry_run_lookback_hours = dry_run_lookback_hours
        # Deferred-send slot for the dev reasoning embed. The editor posts its
        # compact summary + trace file first; top_creations posts its embed;
        # then the orchestrator (dev runner) calls flush_pending_reasoning()
        # so the reasoning summary lands at the end of the timeline.
        self._pending_reasoning: Optional[Dict[str, Any]] = None
        self.max_publish_per_run = None
        if max_publish_per_run is not None:
            self.max_publish_per_run = max(1, int(max_publish_per_run))
        else:
            env_val = os.getenv("LIVE_UPDATE_MAX_POSTS_PER_RUN")
            if env_val is not None:
                self.max_publish_per_run = max(1, int(env_val))
        self.candidate_generator = candidate_generator or LiveUpdateCandidateGenerator(
            llm_client=llm_client or getattr(bot, "claude_client", None),
            logger_instance=self.logger,
            max_candidates=max_candidates,
        )

    async def run_once(self, trigger: str) -> Dict[str, Any]:
        """Run one live-editor pass and persist auditable state."""
        started_at = datetime.now(timezone.utc)
        guild_id = self._resolve_guild_id()
        live_channel_id = self._resolve_live_channel_id(guild_id)
        checkpoint_key = self._checkpoint_key(guild_id, live_channel_id)

        checkpoint = await self._call_db(
            self.db.get_live_update_checkpoint, checkpoint_key,
            environment=self.environment,
        )

        # Dev: synthesize an initial checkpoint if none exists
        if checkpoint is None and self.environment == "dev":
            lookback = max(1, int(self.dry_run_lookback_hours or 6))
            checkpoint = {
                "last_message_created_at": (
                    datetime.now(timezone.utc) - timedelta(hours=lookback)
                ).isoformat()
            }
            self.logger.info(
                "[LiveUpdateEditor] dev: synthesized initial checkpoint with %sh lookback",
                lookback,
            )

        memory = await self._call_db(
            self.db.get_live_update_editorial_memory, guild_id,
            environment=self.environment,
        )
        watchlist = await self._call_db(
            self.db.get_live_update_watchlist, guild_id,
            environment=self.environment,
        )
        recent_feed = await self._call_db(
            self.db.get_recent_live_update_feed_items,
            guild_id,
            live_channel_id,
            50,
            24,
            environment=self.environment,
        )

        run = await self._call_db(
            self.db.create_live_update_run,
            {
                "guild_id": guild_id,
                "trigger": trigger,
                "status": "running",
                "live_channel_id": live_channel_id,
                "checkpoint_before": checkpoint or {},
                "memory_snapshot": self._snapshot_memory(memory),
                "duplicate_snapshot": self._snapshot_recent_feed(recent_feed),
                "metadata": {
                    "max_messages": self.max_messages,
                    "max_candidates": self.max_candidates,
                },
            },
            environment=self.environment,
        )
        if not run:
            return {
                "status": "failed",
                "error": "failed_to_create_run",
                "guild_id": guild_id,
                "live_channel_id": live_channel_id,
            }

        run_id = run["run_id"]
        try:
            exclude_author_ids = self._resolve_excluded_author_ids()
            self.logger.info(
                "[LiveUpdateEditor] excluding bot/self author_ids from source scan: %s",
                exclude_author_ids or "[]",
            )
            messages = await self._call_db(
                self.db.get_archived_messages_after_checkpoint,
                checkpoint,
                guild_id,
                None,
                self.max_messages,
                exclude_author_ids,
            )

            if not messages:
                skipped_decision = await self._record_skipped_run(
                    run_id,
                    guild_id,
                    checkpoint_key,
                    checkpoint,
                    reason="no_new_archived_messages",
                )
                await self._publish_dev_debug_report(
                    trigger=trigger,
                    run_id=run_id,
                    live_channel_id=live_channel_id,
                    messages=[],
                    candidates=[],
                    decisions=[],
                    decision_counts=Counter(),
                    publish_results=[],
                    agent_metadata=None,
                    skipped_reason="no_new_archived_messages",
                    status="skipped",
                    memory=memory,
                    watchlist=watchlist,
                    recent_feed=recent_feed,
                )
                return {
                    "run_id": run_id,
                    "status": "skipped",
                    "candidate_count": 0,
                    "decision_count": 1 if skipped_decision else 0,
                    "checkpoint_key": checkpoint_key,
                }

            context = await self._build_editor_context(messages, guild_id, recent_feed)
            candidates = await self.candidate_generator.generate_candidates(
                messages=messages,
                run_id=run_id,
                guild_id=guild_id,
                memory=memory,
                watchlist=watchlist,
                context=context,
                tool_runner=lambda tool, args: self._run_editor_tool(tool, args, guild_id, live_channel_id),
            )
            agent_metadata = self._agent_run_metadata(messages, context)
            await self._call_db(
                self.db.update_live_update_run,
                run_id,
                {"metadata": agent_metadata},
                guild_id,
                environment=self.environment,
            )
            persisted_candidates = await self._call_db(
                self.db.store_live_update_candidates,
                candidates,
                environment=self.environment,
            )

            decision_counts: Counter[str] = Counter()
            decisions: List[Dict[str, Any]] = []
            for candidate in persisted_candidates:
                decision = await self._decide_candidate(candidate, guild_id, recent_feed)
                if decision:
                    decisions.append(decision)
                    decision_counts[decision["decision"]] += 1

            accepted_candidates = self._select_publishable_candidates([
                candidate
                for candidate, decision in zip(persisted_candidates, decisions)
                if decision.get("decision") == "accepted"
            ])
            publish_results = await self._publish_accepted_candidates(
                accepted_candidates,
                guild_id,
                live_channel_id,
            )
            published_candidates = [
                result["candidate"]
                for result in publish_results
                if result.get("status") == "posted"
            ]
            failed_post_count = sum(1 for result in publish_results if result.get("status") == "failed_post")
            post_duplicate_count = sum(1 for result in publish_results if result.get("status") == "duplicate")
            if failed_post_count:
                decision_counts["failed_post"] += failed_post_count
            if post_duplicate_count:
                decision_counts["duplicate"] += post_duplicate_count

            await self._update_editorial_state(published_candidates, watchlist)

            checkpoint_after = await self._write_checkpoint_after_messages(
                checkpoint_key,
                guild_id,
                live_channel_id,
                run_id,
                messages,
            )

            await self._call_db(
                self.db.update_live_update_run,
                run_id,
                {
                    "status": "completed",
                    "candidate_count": len(persisted_candidates),
                    "accepted_count": len(published_candidates),
                    "rejected_count": decision_counts["rejected"],
                    "duplicate_count": decision_counts["duplicate"],
                    "deferred_count": decision_counts["deferred"],
                    "checkpoint_after": checkpoint_after or {},
                    "metadata": {
                        "max_messages": self.max_messages,
                        "max_candidates": self.max_candidates,
                        **agent_metadata,
                        "published_count": len(published_candidates),
                        "failed_post_count": failed_post_count,
                        "post_duplicate_count": post_duplicate_count,
                        "published_feed_item_ids": [
                            result.get("feed_item", {}).get("feed_item_id")
                            for result in publish_results
                            if result.get("feed_item")
                        ],
                    },
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
                guild_id,
                environment=self.environment,
            )

            if not published_candidates:
                await self._publish_dev_debug_report(
                    trigger=trigger,
                    run_id=run_id,
                    live_channel_id=live_channel_id,
                    messages=messages,
                    candidates=persisted_candidates,
                    decisions=decisions,
                    decision_counts=decision_counts,
                    publish_results=publish_results,
                    agent_metadata=agent_metadata,
                    skipped_reason=None,
                    status="completed",
                    memory=memory,
                    watchlist=watchlist,
                    recent_feed=recent_feed,
                )

            return {
                "run_id": run_id,
                "status": "completed",
                "candidate_count": len(persisted_candidates),
                "decision_counts": dict(decision_counts),
                "published_count": len(published_candidates),
                "failed_post_count": failed_post_count,
                "checkpoint_key": checkpoint_key,
            }

        except Exception as exc:
            self.logger.error("[LiveUpdateEditor] run_once failed: %s", exc, exc_info=True)
            agent_trace = getattr(self.candidate_generator, "last_agent_trace", {}) or {}
            await self._call_db(
                self.db.update_live_update_run,
                run_id,
                {
                    "status": "failed",
                    "error_message": str(exc),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "raw_text": (agent_trace.get("raw_text") or "")[:50000],
                    },
                },
                guild_id,
                environment=self.environment,
            )
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

    async def _run_editor_tool(
        self,
        tool_name: str,
        args: Dict[str, Any],
        guild_id: Optional[int],
        live_channel_id: Optional[int],
    ) -> Dict[str, Any]:
        """Run read-only editor tools requested by the LLM."""
        safe_args = args if isinstance(args, dict) else {}
        exclude_author_ids = self._resolve_excluded_author_ids()
        if tool_name == "search_messages":
            return {
                "messages": await self._call_db(
                    self.db.search_live_update_messages,
                    safe_args.get("query") or "",
                    guild_id,
                    safe_args.get("author_id"),
                    safe_args.get("channel_id"),
                    int(safe_args.get("hours_back") or 168),
                    int(safe_args.get("limit") or 20),
                    environment=self.environment,
                    exclude_author_ids=exclude_author_ids,
                )
            }
        if tool_name == "get_author_profile":
            return {
                "profile": await self._call_db(
                    self.db.get_live_update_author_profile,
                    safe_args.get("author_id"),
                    guild_id,
                    environment=self.environment,
                    exclude_author_ids=exclude_author_ids,
                )
            }
        if tool_name == "get_message_context":
            return await self._call_db(
                self.db.get_live_update_context_for_message_ids,
                safe_args.get("message_ids") or [],
                guild_id,
                int(safe_args.get("limit") or 20),
                environment=self.environment,
                exclude_author_ids=exclude_author_ids,
            )
        if tool_name == "get_engagement_context":
            return await self._call_db(
                self.db.get_live_update_message_engagement_context,
                safe_args.get("message_ids") or [],
                guild_id,
                int(safe_args.get("participant_limit") or safe_args.get("limit") or 12),
                environment=self.environment,
            )
        if tool_name == "get_recent_reactions":
            return await self._call_db(
                self.db.get_live_update_recent_reaction_events,
                guild_id,
                int(safe_args.get("hours_back") or 1),
                int(safe_args.get("limit") or 30),
                environment=self.environment,
            )
        if tool_name == "get_recent_updates":
            return {
                "updates": await self._call_db(
                    self.db.get_recent_live_update_feed_items,
                    guild_id,
                    live_channel_id,
                    int(safe_args.get("limit") or 20),
                    int(safe_args.get("hours_back") or 168),
                    environment=self.environment,
                )
            }
        if tool_name == "search_previous_updates":
            return {
                "updates": await self._call_db(
                    self.db.search_live_update_feed_items,
                    safe_args.get("query") or "",
                    guild_id,
                    live_channel_id,
                    int(safe_args.get("hours_back") or 168),
                    int(safe_args.get("limit") or 20),
                    environment=self.environment,
                )
            }
        if tool_name == "search_editorial_memory":
            return {
                "memory": await self._call_db(
                    self.db.search_live_update_editorial_memory,
                    safe_args.get("query") or "",
                    guild_id,
                    int(safe_args.get("limit") or 20),
                    environment=self.environment,
                )
            }
        if tool_name == "watchlist_add":
            try:
                result = self.db.insert_live_update_watchlist(
                    watch_key=str(safe_args.get("watch_key") or ""),
                    title=str(safe_args.get("title") or ""),
                    origin_reason=str(safe_args.get("reason") or ""),
                    source_message_ids=safe_args.get("source_message_ids") or [],
                    channel_id=safe_args.get("channel_id"),
                    subject_type=str(safe_args.get("subject_type") or "general"),
                    environment=self.environment,
                    guild_id=guild_id,
                )
                watchlist_action = {
                    "tool": "watchlist_add",
                    "args": safe_args,
                    "ok": result is not None,
                    "result": result,
                }
                if hasattr(self, "candidate_generator") and self.candidate_generator:
                    self.candidate_generator.watchlist_actions.append(watchlist_action)
                if result is None:
                    return {"ok": False, "error": "watchlist_add_failed", "args": safe_args}
                return {"ok": True, "watchlist_entry": result, "action": "added"}
            except Exception as exc:
                self.logger.error("[LiveUpdateEditor] watchlist_add failed: %s", exc)
                return {"ok": False, "error": f"watchlist_add_error:{exc}", "args": safe_args}
        if tool_name == "watchlist_update":
            try:
                result = self.db.update_live_update_watchlist(
                    watch_key=str(safe_args.get("watch_key") or ""),
                    action=str(safe_args.get("action") or ""),
                    notes=safe_args.get("notes"),
                    environment=self.environment,
                )
                watchlist_action = {
                    "tool": "watchlist_update",
                    "args": safe_args,
                    "ok": result is not None,
                    "result": result,
                }
                if hasattr(self, "candidate_generator") and self.candidate_generator:
                    self.candidate_generator.watchlist_actions.append(watchlist_action)
                if result is None:
                    return {"ok": False, "error": "watchlist_update_failed", "args": safe_args}
                return {"ok": True, "watchlist_entry": result, "action": safe_args.get("action")}
            except Exception as exc:
                self.logger.error("[LiveUpdateEditor] watchlist_update failed: %s", exc)
                return {"ok": False, "error": f"watchlist_update_error:{exc}", "args": safe_args}
        return {"error": f"unknown_tool:{tool_name}"}

    def _select_publishable_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sorted_candidates = sorted(
            candidates or [],
            key=lambda item: (float(item.get("confidence") or 0), int(item.get("priority") or 0)),
            reverse=True,
        )
        if self.max_publish_per_run is not None:
            return sorted_candidates[: self.max_publish_per_run]
        return sorted_candidates

    async def _build_editor_context(
        self,
        messages: List[Dict[str, Any]],
        guild_id: Optional[int],
        recent_feed: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build the retrieval/context bundle the Opus editor should reason over."""
        context: Dict[str, Any] = {
            "source_window": {
                "message_count": len(messages or []),
                "prompt_message_limit": 1000,
                "prompt_message_count": min(len(messages or []), 1000),
                "capped_by_editor_max_messages": len(messages or []) >= self.max_messages,
                "last_hour_message_count": self._count_messages_within_hours(messages, 1),
                "last_three_hours_message_count": self._count_messages_within_hours(messages, 3),
                "oldest_created_at": min((str(msg.get("created_at") or "") for msg in messages), default=None),
                "newest_created_at": max((str(msg.get("created_at") or "") for msg in messages), default=None),
                "selection_note": (
                    "These are the archived non-NSFW source messages fetched for the current live-update window. "
                    "If capped_by_editor_max_messages is true, there may be additional messages in the time period "
                    "that were not included in this run."
                ),
            },
            "tool_use_guidance": {
                "same_channel_history": "Use this to understand whether the source message is a follow-up, repeated chatter, or part of a larger useful development.",
                "author_recent_messages": "Use this to judge whether the poster has been working on a coherent project and whether this message is new or just incremental status.",
                "author_stats": "Use total messages and average/max reactions as weak credibility/visibility signals, not as proof of truth.",
                "recent_feed_items": "Use this to avoid reposting topics that were already sent recently; add new context only if there is a genuinely new development.",
                "reply_and_thread_context": "Messages expose reference_id/is_reply/reply_jump_url and thread_id/is_thread_message. For replies, inspect the referenced parent message before treating the reply as standalone. For thread messages, use thread_id and surrounding context to understand the thread.",
            },
            "recent_feed_items": self._compact_recent_feed(recent_feed),
        }
        if not messages:
            return context
        try:
            context_timeout_seconds = max(
                15,
                int(os.getenv("LIVE_UPDATE_CONTEXT_TIMEOUT_SECONDS", "90")),
            )
            context_source_limit = max(
                5,
                min(50, int(os.getenv("LIVE_UPDATE_CONTEXT_SOURCE_LIMIT", "20"))),
            )
            self.logger.info(
                "[LiveUpdateEditor] expanding source context: source_limit=%s timeout=%ss",
                context_source_limit,
                context_timeout_seconds,
            )
            expanded = await asyncio.wait_for(
                self._call_db(
                    self.db.get_live_update_context_for_messages,
                    messages,
                    guild_id,
                    min(context_source_limit, self.max_messages),
                    environment=self.environment,
                    exclude_author_ids=self._resolve_excluded_author_ids(),
                ),
                timeout=context_timeout_seconds,
            )
            if isinstance(expanded, dict):
                context.update(expanded)
        except AttributeError:
            self.logger.warning("[LiveUpdateEditor] DB does not expose live-update context expansion.")
        except TimeoutError:
            self.logger.warning(
                "[LiveUpdateEditor] Live-update context expansion timed out; continuing with base source window."
            )
        except Exception as exc:
            self.logger.warning("[LiveUpdateEditor] Failed to build live-update context: %s", exc)
        return context

    @staticmethod
    def _parse_created_at(created_at: Any) -> Optional[datetime]:
        if not created_at:
            return None
        try:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _count_messages_within_hours(cls, messages: List[Dict[str, Any]], hours: int) -> int:
        now = datetime.now(timezone.utc)
        cutoff_seconds = max(1, int(hours)) * 3600
        count = 0
        for message in messages or []:
            parsed = cls._parse_created_at(message.get("created_at"))
            if not parsed:
                continue
            age_seconds = (now - parsed).total_seconds()
            if 0 <= age_seconds <= cutoff_seconds:
                count += 1
        return count

    @staticmethod
    def _compact_recent_feed(recent_feed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compacted: List[Dict[str, Any]] = []
        for item in recent_feed or []:
            compacted.append({
                "feed_item_id": item.get("feed_item_id"),
                "update_type": item.get("update_type"),
                "title": item.get("title"),
                "body": (item.get("body") or "")[:500],
                "source_message_ids": item.get("source_message_ids") or [],
                "duplicate_key": item.get("duplicate_key"),
                "posted_at": item.get("posted_at") or item.get("created_at"),
                "status": item.get("status"),
            })
        return compacted[:50]

    def _agent_run_metadata(self, messages: List[Dict[str, Any]], context: Dict[str, Any]) -> Dict[str, Any]:
        agent_trace = getattr(self.candidate_generator, "last_agent_trace", {}) or {}
        tool_trace = agent_trace.get("tool_trace") or []
        return {
            "max_messages": self.max_messages,
            "max_candidates": self.max_candidates,
            "agent_model": agent_trace.get("model") or getattr(self.candidate_generator, "model", None),
            "editor_reasoning": agent_trace.get("editor_reasoning") or "",
            "agent_turn_count": agent_trace.get("agent_turn_count", 0),
            "agent_max_turns": agent_trace.get("max_agent_turns"),
            "agent_tool_call_count": len(tool_trace),
            "agent_tools_used": [item.get("tool") for item in tool_trace[:50]],
            "source_message_count": len(messages or []),
            "source_context_count": len((context or {}).get("source_context") or {}),
            "recent_feed_context_count": len((context or {}).get("recent_feed_items") or []),
            "raw_text": (agent_trace.get("raw_text") or "")[:50000],
            "tool_trace_summary": [
                {
                    "tool": item.get("tool"),
                    "args": item.get("args") or {},
                    "ok": item.get("ok"),
                    "result_count": self._tool_result_count(item.get("result")),
                }
                for item in tool_trace[:50]
            ],
            "watchlist_actions": agent_trace.get("watchlist_actions") or [],
        }

    async def _record_skipped_run(
        self,
        run_id: str,
        guild_id: Optional[int],
        checkpoint_key: str,
        checkpoint: Optional[Dict[str, Any]],
        *,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        decision = await self._call_db(
            self.db.store_live_update_decision,
            {
                "run_id": run_id,
                "decision": "skipped",
                "reason": reason,
                "decision_payload": {"checkpoint": checkpoint or {}},
            },
            guild_id,
            environment=self.environment,
        )
        checkpoint_after = await self._call_db(
            self.db.upsert_live_update_checkpoint,
            {
                "checkpoint_key": checkpoint_key,
                "guild_id": guild_id,
                "last_message_id": (checkpoint or {}).get("last_message_id"),
                "last_message_created_at": (checkpoint or {}).get("last_message_created_at"),
                "last_run_id": run_id,
                "state": {"last_status": "skipped", "reason": reason},
            },
            environment=self.environment,
        )
        await self._call_db(
            self.db.update_live_update_run,
            run_id,
            {
                "status": "skipped",
                "skipped_reason": reason,
                "checkpoint_after": checkpoint_after or checkpoint or {},
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
            guild_id,
            environment=self.environment,
        )
        return decision

    async def _publish_dev_debug_report(
        self,
        *,
        trigger: str,
        run_id: Optional[str],
        live_channel_id: Optional[int],
        messages: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        decision_counts: Counter,
        publish_results: List[Dict[str, Any]],
        agent_metadata: Optional[Dict[str, Any]],
        skipped_reason: Optional[str],
        status: str,
        memory: Optional[List[Dict[str, Any]]] = None,
        watchlist: Optional[List[Dict[str, Any]]] = None,
        recent_feed: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Telemetry post for every run, routed to the dev/debug channel.

        Fires in both dev and prod environments — but always to the dev channel
        (``DEV_SUMMARY_CHANNEL_ID`` / ``DEV_LIVE_UPDATE_CHANNEL_ID``), never to
        the production summary channel. In dev, the reasoning embed + file are
        deferred so the orchestrator can interleave them with top_creations'
        debug post. In prod, they're sent inline immediately (no orchestrator).
        Wrapped in try/except so failures never break the run.
        """
        try:
            channel = await self._resolve_debug_channel()
        except Exception as exc:
            self.logger.warning(
                "[LiveUpdateEditor] dev debug report skipped — could not resolve debug channel: %s",
                exc,
            )
            return

        try:
            messages = messages or []
            candidates = candidates or []
            decisions = decisions or []
            publish_results = publish_results or []
            memory = memory or []
            watchlist = watchlist or []
            recent_feed = recent_feed or []
            agent_metadata = agent_metadata or {}

            decision_by_candidate = {
                (decision or {}).get("candidate_id"): decision
                for decision in decisions
                if isinstance(decision, dict)
            }
            publish_by_candidate = {
                (result.get("candidate") or {}).get("candidate_id"): result
                for result in publish_results
                if isinstance(result, dict)
            }

            agent_trace = getattr(self.candidate_generator, "last_agent_trace", {}) or {}

            title = f"[dev] live-editor: no new feed items ({status})"
            if skipped_reason:
                description = f"Run skipped: `{skipped_reason}`"
            else:
                description = "Run completed but nothing was published."
            embed = discord.Embed(
                title=title,
                description=description,
                color=0x808080,
            )

            # --- field: summary ---
            summary_lines = [
                f"trigger: `{trigger}`",
                f"environment: `{self.environment}`",
                f"run_id: `{run_id}`",
                f"source messages: `{len(messages)}`",
                f"candidates: `{len(candidates)}`",
                f"agent turns: `{agent_metadata.get('agent_turn_count', 'n/a')}`",
                f"agent tool calls: `{agent_metadata.get('agent_tool_call_count', 'n/a')}`",
            ]
            embed.add_field(name="summary", value="\n".join(summary_lines)[:1024], inline=False)

            # --- field: model & budget ---
            model_name = (
                agent_metadata.get("agent_model")
                or agent_trace.get("model")
                or getattr(self.candidate_generator, "model", None)
                or "n/a"
            )
            max_turns = agent_metadata.get("agent_max_turns") or agent_trace.get("max_agent_turns") or "?"
            max_tools_per_turn = (
                agent_trace.get("max_tool_requests_per_turn")
                or getattr(self.candidate_generator, "max_tool_requests_per_turn", None)
                or "?"
            )
            turn_count = agent_metadata.get("agent_turn_count", 0)
            tool_count = agent_metadata.get("agent_tool_call_count", 0)
            tool_budget_total = (
                int(max_tools_per_turn) * int(max_turns)
                if isinstance(max_tools_per_turn, int) and isinstance(max_turns, int)
                else "?"
            )
            embed.add_field(
                name="model & budget",
                value=(
                    f"model: `{model_name}`\n"
                    f"turns: `{turn_count} / {max_turns}`\n"
                    f"tool calls: `{tool_count} / {tool_budget_total}` (cap≈{max_tools_per_turn}/turn)"
                )[:1024],
                inline=False,
            )

            # --- field: time range ---
            time_range_value = self._format_time_range_field(messages)
            if time_range_value:
                embed.add_field(name="time range", value=time_range_value[:1024], inline=False)

            # --- field: channel coverage ---
            channel_counter: Counter[str] = Counter(
                (m.get("channel_name") or "?") for m in messages
            )
            if channel_counter:
                top_channels = channel_counter.most_common(5)
                channel_lines = [f"`#{name}` · {count}" for name, count in top_channels]
                more = max(0, len(channel_counter) - len(top_channels))
                if more:
                    channel_lines.append(f"… (+{more} more)")
                embed.add_field(
                    name=f"channels ({len(channel_counter)} total)",
                    value="\n".join(channel_lines)[:1024],
                    inline=True,
                )

            # --- field: authors ---
            author_counter: Counter[str] = Counter(
                (
                    m.get("author_name")
                    or m.get("author_display_name")
                    or str(m.get("author_id") or "?")
                )
                for m in messages
            )
            if author_counter:
                top_authors = author_counter.most_common(5)
                author_lines = [f"{name} · {count}" for name, count in top_authors]
                more = max(0, len(author_counter) - len(top_authors))
                if more:
                    author_lines.append(f"… (+{more} more)")
                embed.add_field(
                    name=f"authors ({len(author_counter)} total)",
                    value="\n".join(author_lines)[:1024],
                    inline=True,
                )

            # --- field: pre-filter density ---
            density_lines = self._prefilter_density_lines(messages)
            if density_lines:
                embed.add_field(
                    name="pre-filter density",
                    value="\n".join(density_lines)[:1024],
                    inline=False,
                )

            # --- field: memory snapshot ---
            if memory:
                memory_lines = [f"count: `{len(memory)}`"]
                for row in memory[:5]:
                    key = row.get("memory_key") or row.get("subject_id") or "?"
                    memory_lines.append(f"• `{str(key)[:60]}`")
                embed.add_field(
                    name="memory",
                    value="\n".join(memory_lines)[:1024],
                    inline=True,
                )

            # --- field: watchlist snapshot ---
            # watchlist is now a state-grouped dict: {fresh, revisit_due, last_call}.
            if watchlist:
                if isinstance(watchlist, dict):
                    flat_rows = (
                        list(watchlist.get("fresh") or [])
                        + list(watchlist.get("revisit_due") or [])
                        + list(watchlist.get("last_call") or [])
                    )
                else:
                    flat_rows = list(watchlist)
                if flat_rows:
                    counts = (
                        f"fresh={len(watchlist.get('fresh') or [])} "
                        f"due={len(watchlist.get('revisit_due') or [])} "
                        f"last={len(watchlist.get('last_call') or [])}"
                        if isinstance(watchlist, dict) else f"count={len(flat_rows)}"
                    )
                    watch_lines = [f"`{counts}`"]
                    for row in flat_rows[:3]:
                        key = row.get("watch_key") or row.get("subject_id") or row.get("author_id") or "?"
                        watch_lines.append(f"• `{str(key)[:60]}`")
                    embed.add_field(
                        name="watchlist",
                        value="\n".join(watch_lines)[:1024],
                        inline=True,
                    )

            # --- field: recent feed dedupe context ---
            if recent_feed:
                feed_lines = [f"count: `{len(recent_feed)}`"]
                seen_keys: List[str] = []
                for row in recent_feed:
                    key = row.get("duplicate_key")
                    if key and key not in seen_keys:
                        seen_keys.append(str(key))
                    if len(seen_keys) >= 5:
                        break
                for key in seen_keys[:5]:
                    feed_lines.append(f"• `{key[:60]}`")
                embed.add_field(
                    name="recent feed (dedupe)",
                    value="\n".join(feed_lines)[:1024],
                    inline=False,
                )

            # --- field: decisions breakdown ---
            if decision_counts:
                breakdown = ", ".join(
                    f"{name}={count}" for name, count in sorted(decision_counts.items())
                )
                embed.add_field(name="decisions", value=f"`{breakdown}`"[:1024], inline=False)

            # --- field: near-miss ---
            if candidates and not any(
                (p.get("status") == "posted") for p in publish_results
            ):
                top_miss = max(
                    candidates,
                    key=lambda c: (float(c.get("confidence") or 0), int(c.get("priority") or 0)),
                    default=None,
                )
                if top_miss:
                    cand_id = top_miss.get("candidate_id")
                    decision = decision_by_candidate.get(cand_id) or {}
                    publish = publish_by_candidate.get(cand_id) or {}
                    reason = (
                        publish.get("error")
                        or decision.get("reason")
                        or top_miss.get("rationale")
                        or "—"
                    )
                    near_miss_value = (
                        f"title: `{(top_miss.get('title') or '?')[:80]}`\n"
                        f"conf: `{float(top_miss.get('confidence') or 0):.2f}` · "
                        f"pri: `{top_miss.get('priority') or 0}`\n"
                        f"decision: `{decision.get('decision') or publish.get('status') or 'pending'}`\n"
                        f"reason: {str(reason)[:200]}"
                    )
                    embed.add_field(
                        name="near-miss",
                        value=near_miss_value[:1024],
                        inline=False,
                    )

            # --- field: reaction-heavy spotlight ---
            spotlight_lines = self._reaction_spotlight_lines(messages)
            if spotlight_lines:
                embed.add_field(
                    name="reaction-heavy spotlight",
                    value="\n".join(spotlight_lines)[:1024],
                    inline=False,
                )

            # --- field: per-candidate listing ---
            if candidates:
                lines: List[str] = []
                for candidate in candidates[:8]:
                    cand_id = candidate.get("candidate_id")
                    decision = decision_by_candidate.get(cand_id) or {}
                    publish = publish_by_candidate.get(cand_id) or {}
                    decision_name = (
                        publish.get("status")
                        or decision.get("decision")
                        or "pending"
                    )
                    reason = (
                        publish.get("error")
                        or decision.get("reason")
                        or candidate.get("rationale")
                        or "—"
                    )
                    title_text = (candidate.get("title") or "(no title)")[:60]
                    dup_key = str(candidate.get("duplicate_key") or "")[:24]
                    conf = float(candidate.get("confidence") or 0)
                    pri = candidate.get("priority") or 0
                    reason_text = str(reason)[:80]
                    lines.append(
                        f"• `{decision_name}` · {title_text} · conf={conf:.2f} · pri={pri} · dup={dup_key} · {reason_text}"
                    )
                if len(candidates) > 8:
                    lines.append(f"… (+{len(candidates) - 8} more)")
                if lines:
                    # split across fields to stay within 1024 per field
                    self._add_chunked_field(
                        embed,
                        name=f"candidates ({len(candidates)})",
                        lines=lines,
                    )

            # Build the reasoning embed up-front; it will be sent LAST (after
            # top_creations posts) by flush_pending_reasoning().
            reasoning_embed = self._build_reasoning_embed(
                run_id=run_id,
                agent_trace=agent_trace,
                candidates=candidates,
                decisions=decisions,
                publish_results=publish_results,
            )

            # First message: compact summary embed ONLY. Everything else is
            # deferred so the final channel timeline reads:
            #   1) editor compact summary embed
            #   2) top_creations embed
            #   3) editor reasoning embed (editor_reasoning + tool trace + per-cand)
            #   4) trace.json.gz attachment
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

            try:
                trace_file = self._build_trace_attachment(
                    run_id=run_id,
                    trigger=trigger,
                    messages=messages,
                    candidates=candidates,
                    decisions=decisions,
                    publish_results=publish_results,
                    agent_metadata=agent_metadata,
                    agent_trace=agent_trace,
                    memory=memory,
                    watchlist=watchlist,
                    recent_feed=recent_feed,
                )
            except Exception as exc:
                trace_file = None
                self.logger.warning(
                    "[LiveUpdateEditor] failed to build trace attachment: %s",
                    exc,
                    exc_info=True,
                )

            # Defer reasoning + file. The orchestrator (dev runner OR the
            # combined hourly task in summariser_cog) calls
            # flush_pending_reasoning() after top_creations posts so the channel
            # timeline reads compact → top_creations → reasoning → file.
            self._pending_reasoning = {
                "channel": channel,
                "embed": reasoning_embed,
                "file": trace_file,
                "run_id": run_id,
            }
            self.logger.info(
                "[LiveUpdateEditor] dev debug report posted for run_id=%s status=%s",
                run_id,
                status,
            )
        except Exception as exc:
            self.logger.warning(
                "[LiveUpdateEditor] dev debug report failed: %s",
                exc,
                exc_info=True,
            )

    async def flush_pending_reasoning(self) -> None:
        """Send the deferred reasoning embed, then the trace file.

        Called by the dev runner after top_creations has posted, so the final
        timeline is:
          1) editor compact summary embed
          2) top_creations embed
          3) editor reasoning embed
          4) trace.json.gz attachment
        """
        pending = self._pending_reasoning
        self._pending_reasoning = None
        if not pending or self.environment != "dev":
            return
        channel = pending.get("channel")
        run_id = pending.get("run_id")
        reasoning_embed = pending.get("embed")
        trace_file = pending.get("file")
        if reasoning_embed is not None:
            try:
                await channel.send(
                    embed=reasoning_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                self.logger.info(
                    "[LiveUpdateEditor] deferred reasoning embed posted for run_id=%s",
                    run_id,
                )
            except Exception as exc:
                self.logger.warning(
                    "[LiveUpdateEditor] deferred reasoning embed send failed: %s",
                    exc,
                    exc_info=True,
                )
        if trace_file is not None:
            try:
                await channel.send(
                    file=trace_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                self.logger.info(
                    "[LiveUpdateEditor] deferred trace attachment posted for run_id=%s",
                    run_id,
                )
            except Exception as exc:
                self.logger.warning(
                    "[LiveUpdateEditor] deferred trace attachment send failed: %s",
                    exc,
                    exc_info=True,
                )

    def _build_reasoning_embed(
        self,
        *,
        run_id: Optional[str],
        agent_trace: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        publish_results: List[Dict[str, Any]],
    ) -> Optional[discord.Embed]:
        """Second embed: agent's raw reasoning + tool trace + per-candidate raw output.

        Replaces the previous JSON file attachment by inlining the high-value
        bits directly into the message. Returns None if there is nothing
        substantive to show (e.g. skipped-no-messages runs).
        """
        raw_text = (agent_trace or {}).get("raw_text") or ""
        tool_trace = (agent_trace or {}).get("tool_trace") or []
        if not raw_text and not tool_trace and not candidates:
            return None

        try:
            # Top-level editor_reasoning is the agent's own 1-3 sentence
            # distillation of "why this pass turned out the way it did" —
            # surface it prominently in the description.
            editor_reasoning = (agent_trace or {}).get("editor_reasoning") or ""
            if editor_reasoning:
                description = f"**Editor reasoning:** {editor_reasoning.strip()[:3800]}"
            else:
                description = (
                    "_No `editor_reasoning` field in agent output (older prompt "
                    "or agent skipped it). Per-candidate distillation below._"
                )

            embed = discord.Embed(
                title="[dev] agent reasoning + trace",
                description=description[:4096],
                color=0x4a90a4,
            )

            # Tool trace as one-liners (each tool call: tool(args_summary) → result_summary)
            if tool_trace:
                trace_lines: List[str] = []
                for idx, item in enumerate(tool_trace[:50], start=1):
                    tool = str(item.get("tool") or "?")[:32]
                    args = item.get("args") or {}
                    args_summary = ", ".join(
                        f"{k}={str(v)[:30]}" for k, v in list(args.items())[:3]
                    )[:120]
                    ok = item.get("ok")
                    result = item.get("result") or {}
                    if isinstance(result, dict):
                        result_summary = ", ".join(
                            f"{k}={len(v) if isinstance(v, (list, dict)) else str(v)[:20]}"
                            for k, v in list(result.items())[:3]
                        )[:120]
                    else:
                        result_summary = str(result)[:120]
                    status_glyph = "✓" if ok else "✗"
                    trace_lines.append(
                        f"`{idx:>2}` {status_glyph} `{tool}`({args_summary}) → {result_summary}"
                    )
                if len(tool_trace) > 50:
                    trace_lines.append(f"… (+{len(tool_trace) - 50} more)")
                self._add_chunked_field(
                    embed,
                    name=f"tool trace ({len(tool_trace)} calls)",
                    lines=trace_lines,
                )

            # Per-candidate distilled reasoning — prompt asks the agent for these:
            # rationale, new_information, why_now, community_validation,
            # uncertainty, duplicate_assessment, risk_flags, editor_notes.
            # Plus the editorial_checklist of gate booleans.
            if candidates:
                decision_by_candidate = {
                    (d or {}).get("candidate_id"): d for d in (decisions or []) if isinstance(d, dict)
                }
                publish_by_candidate = {
                    ((r.get("candidate") or {}).get("candidate_id")): r
                    for r in (publish_results or []) if isinstance(r, dict)
                }
                for c in candidates[:5]:
                    cid = c.get("candidate_id")
                    title = (c.get("title") or "(no title)")[:80]
                    decision = decision_by_candidate.get(cid) or {}
                    publish = publish_by_candidate.get(cid) or {}
                    verdict = publish.get("status") or decision.get("decision") or "pending"
                    conf = float(c.get("confidence") or 0)
                    pri = c.get("priority") or 0

                    parts: List[str] = [f"verdict: `{verdict}` · conf=`{conf:.2f}` · pri=`{pri}`"]

                    # Pull the distilled fields the prompt asked for.
                    for key, label in [
                        ("rationale", "rationale"),
                        ("new_information", "new info"),
                        ("why_now", "why now"),
                        ("community_validation", "community signal"),
                        ("duplicate_assessment", "duplicate check"),
                        ("uncertainty", "uncertainty"),
                        ("editor_notes", "editor notes"),
                    ]:
                        val = c.get(key)
                        if isinstance(val, str) and val.strip():
                            parts.append(f"**{label}:** {val.strip()[:220]}")

                    # editorial_checklist: list of gates with their boolean answers
                    checklist = c.get("editorial_checklist") or {}
                    if isinstance(checklist, dict) and checklist:
                        passed = [k for k, v in checklist.items() if v is True]
                        failed = [k for k, v in checklist.items() if v is False]
                        unknown = [k for k, v in checklist.items() if v not in (True, False)]
                        cl_bits = []
                        if passed: cl_bits.append(f"✓{len(passed)}")
                        if failed: cl_bits.append(f"✗{len(failed)} ({', '.join(failed[:3])})")
                        if unknown: cl_bits.append(f"?{len(unknown)}")
                        if cl_bits:
                            parts.append(f"**checklist:** {' · '.join(cl_bits)}")

                    # risk_flags: usually a list
                    risks = c.get("risk_flags")
                    if isinstance(risks, list) and risks:
                        parts.append(f"**risks:** {', '.join(str(r)[:40] for r in risks[:5])}")

                    # Reason from the decision layer (if rejected/deferred)
                    decision_reason = decision.get("reason") or publish.get("error")
                    if decision_reason and verdict not in ("accepted", "posted"):
                        parts.append(f"**decision reason:** {str(decision_reason)[:220]}")

                    embed.add_field(
                        name=f"📋 {title}",
                        value="\n".join(parts)[:1024],
                        inline=False,
                    )

                if len(candidates) > 5:
                    embed.add_field(
                        name="…",
                        value=f"+{len(candidates) - 5} more candidates (see attached `.json.gz`)",
                        inline=False,
                    )

            # Footer: run_id, model, and reasoning_recovery_path for telemetry
            model_name = (agent_trace or {}).get("model") or "?"
            recovery_path = (agent_trace or {}).get("reasoning_recovery_path") or "none"
            footer_text = f"run_id={run_id} | model={model_name} | recovery_path={recovery_path}"
            embed.set_footer(text=footer_text[:2048])

            # Suppress only if there's truly nothing to show — no description AND no fields.
            # (editor_reasoning lives in the description, so we must keep the embed when
            # description is non-empty even if no tool calls or candidates fired.)
            if not embed.description and not embed.fields:
                return None
            return embed
        except Exception as exc:
            self.logger.warning(
                "[LiveUpdateEditor] failed to build reasoning embed: %s",
                exc,
                exc_info=True,
            )
            return None

    @staticmethod
    def _add_chunked_field(embed: discord.Embed, *, name: str, lines: List[str]) -> None:
        """Add one or more fields to fit the 1024-char per-field cap."""
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

    @classmethod
    def _format_time_range_field(cls, messages: List[Dict[str, Any]]) -> Optional[str]:
        parsed = [
            dt for dt in (cls._parse_created_at(m.get("created_at")) for m in messages) if dt
        ]
        if not parsed:
            return None
        earliest = min(parsed)
        latest = max(parsed)
        duration = latest - earliest
        hours, remainder = divmod(int(duration.total_seconds()), 3600)
        minutes = remainder // 60
        return (
            f"earliest: `{earliest.strftime('%H:%M')}` UTC\n"
            f"latest: `{latest.strftime('%H:%M')}` UTC\n"
            f"duration: `{hours}h {minutes}m`"
        )

    @staticmethod
    def _prefilter_density_lines(messages: List[Dict[str, Any]]) -> List[str]:
        if not messages:
            return []
        terms = LiveUpdateCandidateGenerator.NEWSWORTHY_TERMS
        n = len(messages)
        keyword_hits = 0
        reaction_heavy = 0
        for m in messages:
            content_lower = (m.get("content") or "").lower()
            if any(term in content_lower for term in terms):
                keyword_hits += 1
            reaction_count = m.get("reaction_count")
            if reaction_count is None:
                reactions = m.get("reactions")
                if isinstance(reactions, list):
                    reaction_count = len(reactions)
            try:
                if int(reaction_count or 0) >= 3:
                    reaction_heavy += 1
            except (TypeError, ValueError):
                pass
        return [
            f"newsworthy keywords: `{keyword_hits} / {n}` msgs",
            f"reactions ≥ 3: `{reaction_heavy} / {n}` msgs",
        ]

    @classmethod
    def _reaction_spotlight_lines(cls, messages: List[Dict[str, Any]]) -> List[str]:
        scored: List[tuple] = []
        for m in messages or []:
            rc = m.get("reaction_count")
            if rc is None:
                reactions = m.get("reactions")
                rc = len(reactions) if isinstance(reactions, list) else 0
            try:
                rc_int = int(rc or 0)
            except (TypeError, ValueError):
                rc_int = 0
            if rc_int <= 0:
                continue
            scored.append((rc_int, m))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        lines: List[str] = []
        for rc_int, m in scored[:3]:
            channel_name = m.get("channel_name") or "?"
            author = (
                m.get("author_name")
                or m.get("author_display_name")
                or str(m.get("author_id") or "?")
            )
            snippet = (m.get("content") or "").replace("\n", " ").strip()[:100]
            lines.append(f"• `#{channel_name}` · {author} · `{rc_int}` react · {snippet or '(no text)'}")
        return lines

    def _build_trace_attachment(
        self,
        *,
        run_id: Optional[str],
        trigger: str,
        messages: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        publish_results: List[Dict[str, Any]],
        agent_metadata: Dict[str, Any],
        agent_trace: Dict[str, Any],
        memory: List[Dict[str, Any]],
        watchlist: List[Dict[str, Any]],
        recent_feed: List[Dict[str, Any]],
    ) -> Optional[discord.File]:
        try:
            decision_by_candidate = {
                (d or {}).get("candidate_id"): d
                for d in decisions
                if isinstance(d, dict)
            }
            publish_by_candidate = {
                (r.get("candidate") or {}).get("candidate_id"): r
                for r in publish_results
                if isinstance(r, dict)
            }

            channel_counter: Counter[str] = Counter(
                (m.get("channel_name") or "?") for m in messages
            )
            author_counter: Counter[str] = Counter(
                (
                    m.get("author_name")
                    or m.get("author_display_name")
                    or str(m.get("author_id") or "?")
                )
                for m in messages
            )
            parsed_times = [
                dt for dt in (self._parse_created_at(m.get("created_at")) for m in messages) if dt
            ]
            terms = LiveUpdateCandidateGenerator.NEWSWORTHY_TERMS
            keyword_hits = sum(
                1 for m in messages
                if any(t in (m.get("content") or "").lower() for t in terms)
            )
            reaction_heavy = 0
            for m in messages:
                rc = m.get("reaction_count")
                if rc is None:
                    reactions = m.get("reactions")
                    rc = len(reactions) if isinstance(reactions, list) else 0
                try:
                    if int(rc or 0) >= 3:
                        reaction_heavy += 1
                except (TypeError, ValueError):
                    pass

            def _sample_message(m: Dict[str, Any]) -> Dict[str, Any]:
                rc = m.get("reaction_count")
                if rc is None:
                    reactions = m.get("reactions")
                    rc = len(reactions) if isinstance(reactions, list) else 0
                return {
                    "message_id": m.get("message_id"),
                    "channel_name": m.get("channel_name"),
                    "author_name": m.get("author_name")
                    or m.get("author_display_name")
                    or m.get("author_id"),
                    "created_at": m.get("created_at"),
                    "reaction_count": rc,
                    "content": (m.get("content") or "")[:240],
                }

            def _candidate_dump(c: Dict[str, Any], *, raw_cap: Optional[int] = None) -> Dict[str, Any]:
                raw = c.get("raw_agent_output") or {}
                raw_text = raw.get("raw_text")
                if raw_cap is not None and isinstance(raw_text, str):
                    raw_text = raw_text[:raw_cap]
                cand_id = c.get("candidate_id")
                return {
                    "candidate_id": cand_id,
                    "title": c.get("title"),
                    "confidence": c.get("confidence"),
                    "priority": c.get("priority"),
                    "duplicate_key": c.get("duplicate_key"),
                    "rationale": c.get("rationale"),
                    "decision": decision_by_candidate.get(cand_id),
                    "publish": publish_by_candidate.get(cand_id),
                    "raw_agent_output_text": raw_text,
                }

            payload: Dict[str, Any] = {
                "run_id": run_id,
                "environment": "dev",
                "trigger": trigger,
                "model": agent_trace.get("model") or agent_metadata.get("agent_model"),
                "agent_trace": {
                    "raw_text": agent_trace.get("raw_text"),
                    "agent_turn_count": agent_trace.get("agent_turn_count"),
                    "max_agent_turns": agent_trace.get("max_agent_turns"),
                    "tool_trace": agent_trace.get("tool_trace") or [],
                },
                "messages_summary": {
                    "count": len(messages),
                    "channels": dict(channel_counter),
                    "authors_top_15": dict(author_counter.most_common(15)),
                    "time_range": [
                        min(parsed_times).isoformat() if parsed_times else None,
                        max(parsed_times).isoformat() if parsed_times else None,
                    ],
                    "newsworthy_keyword_hits": keyword_hits,
                    "reaction_heavy_count": reaction_heavy,
                    "sample": [_sample_message(m) for m in messages[:15]],
                },
                "candidates": [_candidate_dump(c) for c in candidates],
                "memory_snapshot": memory,
                "watchlist_snapshot": watchlist,
                "recent_feed_context": [
                    {
                        "feed_item_id": r.get("feed_item_id"),
                        "duplicate_key": r.get("duplicate_key"),
                        "created_at": r.get("created_at") or r.get("posted_at"),
                        "status": r.get("status"),
                    }
                    for r in (recent_feed or [])[:30]
                ],
                "decisions": decisions,
                "agent_metadata": agent_metadata,
            }

            data = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
            if len(data) > 7 * 1024 * 1024:
                # Trim: drop most of the message sample and cap per-candidate raw text.
                payload["messages_summary"]["sample"] = payload["messages_summary"]["sample"][:5]
                payload["candidates"] = [_candidate_dump(c, raw_cap=2000) for c in candidates]
                data = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
            gzipped = gzip.compress(data)
            return discord.File(io.BytesIO(gzipped), filename=f"agent_trace_{run_id}.json.gz")
        except Exception as exc:
            self.logger.warning(
                "[LiveUpdateEditor] failed to build trace attachment: %s", exc, exc_info=True,
            )
            return None

    async def _publish_accepted_candidates(
        self,
        accepted_candidates: List[Dict[str, Any]],
        guild_id: Optional[int],
        live_channel_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        if not accepted_candidates:
            return []
        results: List[Dict[str, Any]] = []
        try:
            channel = await self._resolve_live_channel(live_channel_id)
        except Exception as exc:
            for candidate in accepted_candidates:
                decision = await self._record_failed_post(
                    candidate,
                    guild_id,
                    live_channel_id,
                    str(exc),
                    discord_message_ids=getattr(exc, "discord_message_ids", []),
                )
                results.append({
                    "status": "failed_post",
                    "candidate": candidate,
                    "decision": decision,
                    "error": str(exc),
                })
            return results

        for candidate in accepted_candidates:
            duplicate = await self._call_db(
                self.db.find_live_update_duplicate,
                candidate.get("duplicate_key"),
                guild_id,
                environment=self.environment,
            )
            if self._is_posted_duplicate(duplicate, candidate):
                decision = await self._record_post_duplicate(candidate, guild_id, duplicate)
                results.append({
                    "status": "duplicate",
                    "candidate": candidate,
                    "decision": decision,
                    "duplicate": duplicate,
                })
                continue
            try:
                feed_item = await self._publish_candidate(channel, candidate, live_channel_id)
                results.append({
                    "status": "posted",
                    "candidate": candidate,
                    "feed_item": feed_item,
                })
            except Exception as exc:
                self.logger.error(
                    "[LiveUpdateEditor] Failed to publish candidate %s: %s",
                    candidate.get("candidate_id"),
                    exc,
                    exc_info=True,
                )
                decision = await self._record_failed_post(
                    candidate,
                    guild_id,
                    live_channel_id,
                    str(exc),
                    discord_message_ids=getattr(exc, "discord_message_ids", []),
                )
                results.append({
                    "status": "failed_post",
                    "candidate": candidate,
                    "decision": decision,
                    "error": str(exc),
                })
        return results

    async def _publish_candidate(
        self,
        channel: Any,
        candidate: Dict[str, Any],
        live_channel_id: Optional[int],
    ) -> Dict[str, Any]:
        messages = self._build_feed_messages(candidate)
        discord_message_ids: List[str] = []
        for message_body in messages:
            for chunk in self._split_discord_content(message_body):
                try:
                    sent_message = await _send_without_mentions(channel, chunk)
                    message_id = self._extract_discord_message_id(sent_message)
                except Exception as exc:
                    raise LiveUpdatePublishError(str(exc), discord_message_ids) from exc
                if message_id is None:
                    raise LiveUpdatePublishError(
                        "published Discord message did not expose an id",
                        discord_message_ids,
                    )
                discord_message_ids.append(message_id)

        feed_item = await self._call_db(
            self.db.store_live_update_feed_item,
            {
                "run_id": candidate.get("run_id"),
                "candidate_id": candidate.get("candidate_id"),
                "guild_id": candidate.get("guild_id"),
                "live_channel_id": live_channel_id,
                "update_type": candidate.get("update_type"),
                "title": candidate.get("title"),
                "body": candidate.get("body"),
                "media_refs": candidate.get("media_refs") or [],
                "examples": candidate.get("examples") or [],
                "source_message_ids": candidate.get("source_message_ids") or [],
                "duplicate_key": candidate.get("duplicate_key"),
                "discord_message_ids": discord_message_ids,
                "status": "posted",
                "posted_at": datetime.now(timezone.utc).isoformat(),
            },
            environment=self.environment,
        )
        if not feed_item:
            raise LiveUpdatePublishError("failed to persist live update feed item", discord_message_ids)

        await self._call_db(
            self.db.upsert_live_update_duplicate_state,
            {
                "guild_id": candidate.get("guild_id"),
                "duplicate_key": candidate.get("duplicate_key"),
                "last_seen_candidate_id": candidate.get("candidate_id"),
                "feed_item_id": feed_item.get("feed_item_id"),
                "status": "posted",
                "metadata": {
                    "discord_message_ids": discord_message_ids,
                    "update_type": candidate.get("update_type"),
                },
            },
            environment=self.environment,
        )
        return feed_item

    async def _resolve_debug_channel(self) -> Any:
        """Always resolve the dev/debug channel (DEV_SUMMARY_CHANNEL_ID).

        Used by ``_publish_dev_debug_report`` so prod runs route their telemetry
        embeds to the dev channel instead of the production summary channel.
        """
        if self.bot is None:
            raise RuntimeError("bot is required for debug-channel publishing")
        env_value = os.getenv("DEV_SUMMARY_CHANNEL_ID") or os.getenv("DEV_LIVE_UPDATE_CHANNEL_ID")
        if not env_value:
            raise RuntimeError(
                "DEV_SUMMARY_CHANNEL_ID / DEV_LIVE_UPDATE_CHANNEL_ID not set; "
                "cannot route debug telemetry"
            )
        channel = None
        if hasattr(self.bot, "get_channel"):
            channel = self.bot.get_channel(int(env_value))
        if channel is None and hasattr(self.bot, "fetch_channel"):
            channel = await self.bot.fetch_channel(int(env_value))
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError(f"could not resolve debug channel {env_value}")
        return channel

    async def _resolve_live_channel(self, live_channel_id: Optional[int]) -> Any:
        if self.bot is None:
            raise RuntimeError("bot is required for live-update publishing")
        if live_channel_id is None:
            raise RuntimeError("live channel id is required for live-update publishing")

        channel = None
        if hasattr(self.bot, "get_channel"):
            channel = self.bot.get_channel(int(live_channel_id))
        if channel is None and hasattr(self.bot, "fetch_channel"):
            channel = await self.bot.fetch_channel(int(live_channel_id))
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError(f"could not resolve live-update channel {live_channel_id}")
        return channel

    async def _record_post_duplicate(
        self,
        candidate: Dict[str, Any],
        guild_id: Optional[int],
        duplicate: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        await self._call_db(
            self.db.update_live_update_candidate_status,
            candidate["candidate_id"],
            "duplicate",
            {"rationale": "Duplicate detected immediately before feed publishing."},
            guild_id,
            environment=self.environment,
        )
        duplicate_feed_item = (duplicate or {}).get("feed_item") or {}
        duplicate_candidate = (duplicate or {}).get("candidate") or {}
        decision = await self._call_db(
            self.db.store_live_update_decision,
            {
                "run_id": candidate.get("run_id"),
                "candidate_id": candidate.get("candidate_id"),
                "decision": "duplicate",
                "reason": "duplicate_key_already_posted_before_publish",
                "duplicate_key": candidate.get("duplicate_key"),
                "duplicate_of_candidate_id": duplicate_candidate.get("candidate_id"),
                "duplicate_of_feed_item_id": duplicate_feed_item.get("feed_item_id"),
                "decision_payload": {
                    "source_message_ids": candidate.get("source_message_ids") or [],
                    "discord_message_ids": duplicate_feed_item.get("discord_message_ids") or [],
                },
            },
            guild_id,
            environment=self.environment,
        )
        await self._call_db(
            self.db.upsert_live_update_duplicate_state,
            {
                "guild_id": guild_id,
                "duplicate_key": candidate.get("duplicate_key"),
                "last_seen_candidate_id": candidate.get("candidate_id"),
                "feed_item_id": duplicate_feed_item.get("feed_item_id"),
                "status": "duplicate",
                "metadata": {"reason": "duplicate_key_already_posted_before_publish"},
            },
            environment=self.environment,
        )
        return decision

    async def _record_failed_post(
        self,
        candidate: Dict[str, Any],
        guild_id: Optional[int],
        live_channel_id: Optional[int],
        error_message: str,
        discord_message_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        posted_ids = [str(message_id) for message_id in (discord_message_ids or [])]
        await self._call_db(
            self.db.update_live_update_candidate_status,
            candidate["candidate_id"],
            "failed_post",
            {"rationale": error_message},
            guild_id,
            environment=self.environment,
        )
        feed_item = await self._call_db(
            self.db.store_live_update_feed_item,
            {
                "run_id": candidate.get("run_id"),
                "candidate_id": candidate.get("candidate_id"),
                "guild_id": candidate.get("guild_id"),
                "live_channel_id": live_channel_id,
                "update_type": candidate.get("update_type"),
                "title": candidate.get("title"),
                "body": candidate.get("body"),
                "media_refs": candidate.get("media_refs") or [],
                "source_message_ids": candidate.get("source_message_ids") or [],
                "duplicate_key": candidate.get("duplicate_key"),
                "discord_message_ids": posted_ids,
                "status": "failed",
                "post_error": error_message,
            },
            environment=self.environment,
        )
        return await self._call_db(
            self.db.store_live_update_decision,
            {
                "run_id": candidate.get("run_id"),
                "candidate_id": candidate.get("candidate_id"),
                "decision": "failed_post",
                "reason": "discord_publish_failed",
                "duplicate_key": candidate.get("duplicate_key"),
                "duplicate_of_feed_item_id": (feed_item or {}).get("feed_item_id"),
                "decision_payload": {
                    "error": error_message,
                    "discord_message_ids": posted_ids,
                    "source_message_ids": candidate.get("source_message_ids") or [],
                },
            },
            guild_id,
            environment=self.environment,
        )

    async def _decide_candidate(
        self,
        candidate: Dict[str, Any],
        guild_id: Optional[int],
        recent_feed: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        duplicate_key = candidate.get("duplicate_key")
        duplicate = await self._call_db(
            self.db.find_live_update_duplicate, duplicate_key, guild_id,
            environment=self.environment,
        )
        recent_duplicate_item = self._recent_feed_duplicate_item(recent_feed, duplicate_key)
        matched_feed_item = (duplicate or {}).get("feed_item") or recent_duplicate_item or {}
        matched_candidate = (duplicate or {}).get("candidate") or {}

        if duplicate and (duplicate.get("feed_item") or duplicate.get("duplicate_state")) or recent_duplicate_item:
            decision_name = "duplicate"
            reason = "duplicate_key_already_seen"
        elif str(candidate.get("agent_decision") or "publish").lower() in {"skip", "duplicate"}:
            decision_name = "rejected"
            reason = f"agent_decision_{str(candidate.get('agent_decision')).lower()}"
        elif str(candidate.get("agent_decision") or "publish").lower() == "defer":
            decision_name = "deferred"
            reason = "agent_requested_defer"
        elif float(candidate.get("confidence") or 0) < 0.75:
            decision_name = "deferred"
            reason = "low_confidence"
        elif self._candidate_checklist_failures(candidate):
            decision_name = "rejected"
            reason = "editorial_checklist_failed"
        elif not self._has_editorial_substance(candidate):
            decision_name = "rejected"
            reason = "insufficient_editorial_substance"
        else:
            decision_name = "accepted"
            reason = "candidate_ready_for_publish_review"

        await self._call_db(
            self.db.update_live_update_candidate_status,
            candidate["candidate_id"],
            decision_name,
            {"rationale": candidate.get("rationale")},
            guild_id,
            environment=self.environment,
        )
        decision = await self._call_db(
            self.db.store_live_update_decision,
            {
                "run_id": candidate["run_id"],
                "candidate_id": candidate["candidate_id"],
                "decision": decision_name,
                "reason": reason,
                "duplicate_key": duplicate_key,
                "duplicate_of_candidate_id": matched_candidate.get("candidate_id") if decision_name == "duplicate" else None,
                "duplicate_of_feed_item_id": matched_feed_item.get("feed_item_id") if decision_name == "duplicate" else None,
                "decision_payload": {
                    "confidence": candidate.get("confidence"),
                    "priority": candidate.get("priority"),
                    "source_message_ids": candidate.get("source_message_ids") or [],
                    "discord_message_ids": matched_feed_item.get("discord_message_ids") or [],
                    "checklist_failures": self._candidate_checklist_failures(candidate),
                },
            },
            guild_id,
            environment=self.environment,
        )
        await self._call_db(
            self.db.upsert_live_update_duplicate_state,
            {
                "guild_id": guild_id,
                "duplicate_key": duplicate_key,
                "last_seen_candidate_id": candidate["candidate_id"],
                "status": "duplicate" if decision_name == "duplicate" else "seen",
                "metadata": {"decision": decision_name, "reason": reason},
            },
            environment=self.environment,
        )
        return decision

    async def _update_editorial_state(
        self,
        accepted_candidates: List[Dict[str, Any]],
        watchlist: List[Dict[str, Any]],
    ) -> None:
        for candidate in accepted_candidates:
            await self._call_db(
                self.db.upsert_live_update_editorial_memory,
                {
                    "guild_id": candidate.get("guild_id"),
                    "memory_key": candidate.get("duplicate_key"),
                    "subject_type": candidate.get("update_type") or "general",
                    "subject_id": self._first_source_id(candidate),
                    "summary": self._memory_summary(candidate),
                    "importance": int(candidate.get("priority") or 0),
                    "state": {
                        "title": candidate.get("title"),
                        "source_message_ids": candidate.get("source_message_ids") or [],
                    },
                    "source_candidate_id": candidate.get("candidate_id"),
                },
                environment=self.environment,
            )
            for watch in self._matching_watchlist_rows(candidate, watchlist):
                await self._call_db(
                    self.db.upsert_live_update_watchlist,
                    {
                        **watch,
                        "last_matched_candidate_id": candidate.get("candidate_id"),
                        "last_matched_at": datetime.now(timezone.utc).isoformat(),
                    },
                    environment=self.environment,
                )

    async def _write_checkpoint_after_messages(
        self,
        checkpoint_key: str,
        guild_id: Optional[int],
        live_channel_id: Optional[int],
        run_id: str,
        messages: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        newest = max(
            messages,
            key=lambda msg: (
                str(msg.get("created_at") or ""),
                int(msg.get("message_id") or 0),
            ),
        )
        return await self._call_db(
            self.db.upsert_live_update_checkpoint,
            {
                "checkpoint_key": checkpoint_key,
                "guild_id": guild_id,
                "channel_id": live_channel_id,
                "last_message_id": newest.get("message_id"),
                "last_message_created_at": newest.get("created_at"),
                "last_run_id": run_id,
                "state": {
                    "last_status": "completed",
                    "message_count": len(messages),
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
        if self.bot is not None:
            bot_config = getattr(self.bot, "server_config", None)
            if bot_config and hasattr(bot_config, "resolve_guild_id"):
                return bot_config.resolve_guild_id(require_write=True)
        return None

    def _resolve_live_channel_id(self, guild_id: Optional[int]) -> Optional[int]:
        if self.live_channel_id is not None:
            return self.live_channel_id
        if self.environment == "dev":
            env_value = os.getenv("DEV_SUMMARY_CHANNEL_ID") or os.getenv("DEV_LIVE_UPDATE_CHANNEL_ID")
            if env_value:
                return int(env_value)
        server_config = getattr(self.db, "server_config", None) or getattr(self.bot, "server_config", None)
        if server_config and guild_id is not None:
            server = server_config.get_server(guild_id) if hasattr(server_config, "get_server") else None
            if server and server.get("summary_channel_id"):
                return int(server["summary_channel_id"])
            if hasattr(server_config, "get_first_server_with_field"):
                server = server_config.get_first_server_with_field("summary_channel_id", require_write=True)
                if server and server.get("summary_channel_id"):
                    return int(server["summary_channel_id"])
        return None

    def _resolve_excluded_author_ids(self) -> List[int]:
        """Exclude the running bot's own messages so the editor never reasons over its own outputs.

        Why: the bot stores its own non-summary posts (top_gens reposts, dev_logs, channel-summary
        follow-ups, live editor's own publishes). Without this, the model wastes context skipping
        its own telemetry and can be biased by it.
        """
        exclusions: set[int] = set()
        bot_user = getattr(getattr(self, "bot", None), "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        if bot_user_id is not None:
            try:
                exclusions.add(int(bot_user_id))
            except (TypeError, ValueError):
                pass
        env_extra = os.getenv("LIVE_UPDATE_EXCLUDED_AUTHOR_IDS", "")
        for chunk in env_extra.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                exclusions.add(int(chunk))
            except ValueError:
                continue
        return sorted(exclusions)

    @staticmethod
    def _checkpoint_key(guild_id: Optional[int], live_channel_id: Optional[int]) -> str:
        return f"live_update_editor:{guild_id or 'unknown'}:{live_channel_id or 'unknown'}"

    @staticmethod
    async def _call_db(method: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    @staticmethod
    def _snapshot_memory(memory: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "count": len(memory or []),
            "keys": [row.get("memory_key") for row in (memory or [])[:20]],
        }

    @staticmethod
    def _snapshot_recent_feed(feed_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "count": len(feed_items or []),
            "duplicate_keys": [row.get("duplicate_key") for row in (feed_items or [])[:20]],
            "discord_message_ids": [
                row.get("discord_message_ids") or []
                for row in (feed_items or [])[:10]
            ],
        }

    @staticmethod
    def _recent_feed_contains_duplicate(feed_items: List[Dict[str, Any]], duplicate_key: Optional[str]) -> bool:
        return LiveUpdateEditor._recent_feed_duplicate_item(feed_items, duplicate_key) is not None

    @staticmethod
    def _recent_feed_duplicate_item(
        feed_items: List[Dict[str, Any]],
        duplicate_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not duplicate_key:
            return None
        return next((item for item in feed_items or [] if item.get("duplicate_key") == duplicate_key), None)

    @staticmethod
    def _is_posted_duplicate(duplicate: Optional[Dict[str, Any]], candidate: Dict[str, Any]) -> bool:
        if not duplicate:
            return False
        feed_item = duplicate.get("feed_item") or {}
        if feed_item.get("feed_item_id"):
            return True
        duplicate_state = duplicate.get("duplicate_state") or {}
        if duplicate_state.get("status") == "posted" and duplicate_state.get("feed_item_id"):
            return True
        existing_candidate = duplicate.get("candidate") or {}
        return (
            bool(existing_candidate.get("candidate_id"))
            and existing_candidate.get("candidate_id") != candidate.get("candidate_id")
            and existing_candidate.get("status") in {"accepted", "failed_post"}
        )

    @classmethod
    def _format_feed_content(cls, candidate: Dict[str, Any]) -> str:
        title = (candidate.get("title") or "Live update").strip()
        body = (candidate.get("body") or "").strip()
        lines = [f"**{title}**"]
        if body:
            body = cls._remove_repeated_title(title, body)
            lines.append(body)
        # Discord renders up to 4 image attachments inline per message. Posting the
        # bare URLs (rather than uploading) lets the auto-embed pick them up.
        media_urls = cls._media_urls(candidate.get("media_refs") or [])
        for url in media_urls[:4]:
            lines.append(url)
        source_link = cls._source_link(candidate)
        if source_link:
            lines.append(f"**Original post:** {source_link}")
        return "\n".join(lines).strip()

    @classmethod
    def _build_feed_messages(cls, candidate: Dict[str, Any]) -> List[str]:
        """Return ordered list of Discord message bodies to publish for the candidate.

        Single-message form (legacy / current default) when `examples` is empty.
        Multi-message form: header message followed by one message per example,
        each example showing its own caption + media + jump-link to its source.
        """
        examples = candidate.get("examples") or []
        if not isinstance(examples, list) or not examples:
            return [cls._format_feed_content(candidate)]

        title = (candidate.get("title") or "Live update").strip()
        body = (candidate.get("body") or "").strip()
        body = cls._remove_repeated_title(title, body)
        # When examples are present, prefer the H2 heading style for the topic
        # header so the topic visually anchors a multi-message thread, mirroring
        # the legacy daily-summary format.
        header_lines = [f"## {title}"]
        if body:
            header_lines.append(body)
        source_link = cls._source_link(candidate)
        if source_link:
            header_lines.append(f"**Original post:** {source_link}")
        messages: List[str] = ["\n".join(header_lines).strip()]

        for example in examples:
            if not isinstance(example, dict):
                continue
            ex_lines: List[str] = []
            caption = (example.get("caption") or "").strip()
            if caption:
                ex_lines.append(f"▸ {caption}")
            for url in (example.get("media_urls") or [])[:4]:
                if isinstance(url, str) and url.strip():
                    ex_lines.append(url.strip())
            ex_link = cls._example_source_link(candidate, example)
            if ex_link:
                ex_lines.append(f"**Original post:** {ex_link}")
            if ex_lines:
                messages.append("\n".join(ex_lines).strip())
        return messages

    @staticmethod
    def _example_source_link(
        candidate: Dict[str, Any], example: Dict[str, Any]
    ) -> Optional[str]:
        # Examples can come from different channels/threads than the primary
        # source (e.g. main release in #wan-chatter, supporting demo in #comfyui).
        # Honor per-example overrides, fall back to candidate-level.
        guild_id = candidate.get("guild_id")
        channel_id = example.get("source_channel_id") or candidate.get("source_channel_id")
        thread_id = example.get("source_thread_id") or candidate.get("source_thread_id")
        message_id = example.get("source_message_id")
        if not guild_id or not channel_id or not message_id:
            return None
        return message_jump_url(guild_id, channel_id, message_id, thread_id=thread_id)

    @staticmethod
    def _remove_repeated_title(title: str, body: str) -> str:
        title_norm = " ".join((title or "").lower().split()).rstrip(".:")
        body_norm = " ".join((body or "").lower().split())
        if title_norm and body_norm.startswith(title_norm):
            trimmed = body[len(title):].lstrip(" .:-\n")
            return trimmed or body
        return body

    @staticmethod
    def _source_link(candidate: Dict[str, Any]) -> Optional[str]:
        guild_id = candidate.get("guild_id")
        channel_id = candidate.get("source_channel_id")
        thread_id = candidate.get("source_thread_id")
        source_ids = candidate.get("source_message_ids") or []
        message_id = source_ids[0] if source_ids else None
        if not guild_id or not channel_id or not message_id:
            return None
        return message_jump_url(guild_id, channel_id, message_id, thread_id=thread_id)

    @staticmethod
    def _media_urls(media_refs: List[Dict[str, Any]]) -> List[str]:
        urls: List[str] = []
        for ref in media_refs or []:
            if not isinstance(ref, dict):
                continue
            url = ref.get("url")
            if url and url not in urls:
                urls.append(str(url))
        return urls

    @classmethod
    def _split_discord_content(cls, content: str, limit: int = DISCORD_MESSAGE_LIMIT) -> List[str]:
        clean = (content or "").strip()
        if not clean:
            return ["(empty live update)"]
        if len(clean) <= limit:
            return [clean]

        chunks: List[str] = []
        current = ""
        paragraphs = clean.split("\n\n")
        for paragraph in paragraphs:
            pending = paragraph if not current else f"{current}\n\n{paragraph}"
            if len(pending) <= limit:
                current = pending
                continue
            if current:
                chunks.append(current)
                current = ""
            while len(paragraph) > limit:
                split_at = paragraph.rfind("\n", 0, limit)
                if split_at <= 0:
                    split_at = paragraph.rfind(" ", 0, limit)
                if split_at <= 0:
                    split_at = limit
                chunks.append(paragraph[:split_at].strip())
                paragraph = paragraph[split_at:].strip()
            current = paragraph
        if current:
            chunks.append(current)
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

    @classmethod
    def _has_editorial_substance(cls, candidate: Dict[str, Any]) -> bool:
        title = candidate.get("title") or ""
        body = candidate.get("body") or ""
        media_refs = candidate.get("media_refs") or []
        if len(body) < 35 and not media_refs:
            return False
        if cls._title_body_overlap(title, body) > 0.7 and cls._body_first_sentence_is_mostly_title(title, body):
            return False
        return True

    @classmethod
    def _candidate_checklist_failures(cls, candidate: Dict[str, Any]) -> List[str]:
        agent_decision = str(candidate.get("agent_decision") or "publish").lower()
        if agent_decision != "publish":
            return []
        raw = candidate.get("raw_agent_output") or {}
        editorial = raw.get("editorial_decision") or {}
        checklist = editorial.get("editorial_checklist") or {}
        if not isinstance(checklist, dict) or not checklist:
            return ["missing_editorial_checklist"]
        failures = [
            key
            for key in cls._CORE_CHECKLIST_KEYS
            if not cls._checklist_value_is_true(checklist.get(key))
        ]
        if not editorial.get("new_information"):
            failures.append("missing_new_information_note")
        if not editorial.get("duplicate_assessment"):
            failures.append("missing_duplicate_assessment")
        if not editorial.get("context_used"):
            failures.append("missing_context_used")
        if cls._candidate_should_have_examples(candidate):
            failures.append("missing_examples_for_multi_source_candidate")
        return failures

    @staticmethod
    def _candidate_should_have_examples(candidate: Dict[str, Any]) -> bool:
        """Enforce multi-example structure when the candidate spans many sources.

        Triggers when source_message_ids has 3+ entries OR the source messages
        come from 2+ distinct authors. In either case, a non-empty `examples`
        array is required so the publisher can render one Discord message per
        contributor/angle instead of flattening everything into one post.
        """
        examples = candidate.get("examples") or []
        if isinstance(examples, list) and len(examples) > 0:
            return False
        source_ids = candidate.get("source_message_ids") or []
        if isinstance(source_ids, list) and len(source_ids) >= 3:
            return True
        evidence = candidate.get("evidence") or {}
        authors = evidence.get("source_authors") if isinstance(evidence, dict) else None
        if isinstance(authors, (list, set)) and len({str(a) for a in authors if a}) >= 2:
            return True
        return False

    @staticmethod
    def _checklist_value_is_true(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, dict):
            return bool(value.get("passed") is True or str(value.get("answer") or "").lower() == "true")
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "passed", "pass"}
        return False

    @staticmethod
    def _title_body_overlap(title: str, body: str) -> float:
        title_tokens = set(
            token
            for token in re.findall(r"[a-z0-9]+", (title or "").lower())
            if len(token) > 2
        )
        first_sentence = (body or "").split(".", 1)[0]
        body_tokens = set(
            token
            for token in re.findall(r"[a-z0-9]+", first_sentence.lower())
            if len(token) > 2
        )
        if not title_tokens or not body_tokens:
            return 0.0
        return len(title_tokens & body_tokens) / len(title_tokens)

    @staticmethod
    def _body_first_sentence_is_mostly_title(title: str, body: str) -> bool:
        title_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", (title or "").lower())
            if len(token) > 2
        ]
        if [token for token in title_tokens if token not in {"live", "update"}] == []:
            return False
        first_sentence_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", (body or "").split(".", 1)[0].lower())
            if len(token) > 2
        ]
        return len(first_sentence_tokens) <= max(len(title_tokens) + 3, 8)

    @staticmethod
    def _first_source_id(candidate: Dict[str, Any]) -> Optional[str]:
        ids = candidate.get("source_message_ids") or []
        return str(ids[0]) if ids else None

    @staticmethod
    def _memory_summary(candidate: Dict[str, Any]) -> str:
        title = candidate.get("title") or candidate.get("update_type") or "Live update"
        body = candidate.get("body") or ""
        return f"{title}: {body[:180]}".strip()

    @staticmethod
    def _matching_watchlist_rows(
        candidate: Dict[str, Any],
        watchlist: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        haystack = " ".join([
            candidate.get("title") or "",
            candidate.get("body") or "",
            candidate.get("update_type") or "",
        ]).lower()
        matches: List[Dict[str, Any]] = []
        if isinstance(watchlist, dict):
            flat = (
                list(watchlist.get("fresh") or [])
                + list(watchlist.get("revisit_due") or [])
                + list(watchlist.get("last_call") or [])
            )
        else:
            flat = list(watchlist or [])
        for watch in flat:
            key = (watch.get("watch_key") or "").lower()
            if key and key in haystack:
                matches.append(watch)
        return matches

    @staticmethod
    def _source_window_coverage_note(
        self: "LiveUpdateEditor",
        messages: List[Dict[str, Any]],
        source_window: Dict[str, Any],
    ) -> str:
        if source_window.get("capped_by_editor_max_messages") or len(messages or []) >= self.max_messages:
            return f"possibly capped at max_messages={self.max_messages}; not guaranteed to include every message in the period"
        if source_window.get("prompt_message_count", len(messages or [])) < len(messages or []):
            return "retrieved window was truncated before sending to Opus"
        return "all fetched non-NSFW messages for this run were sent to Opus"

    @staticmethod
    def _tool_result_count(result: Any) -> Optional[int]:
        if not isinstance(result, dict):
            return None
        for key in ("messages", "updates", "memory"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
        if isinstance(result.get("source_context"), dict):
            return len(result.get("source_context") or {})
        if isinstance(result.get("profile"), dict):
            return 1
        return None
