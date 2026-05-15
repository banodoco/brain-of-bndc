"""
Deterministic core for the topic-centered live-update editor.

This module intentionally keeps canonicalization, alias resolution, collision
checks, and transition payload shaping pure so they can be tested without
Anthropic or Discord dependencies.
"""

from __future__ import annotations

import json
import logging
import re
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
import discord

from src.features.summarising.live_update_prompts import DEFAULT_LIVE_UPDATE_MODEL
from src.common.external_media import extract_external_urls  # T6: shared helper


logger = logging.getLogger("DiscordBot")

SIMILARITY_COLLISION_THRESHOLD = 0.55

READ_TOOL_NAMES = {
    "search_topics",
    "search_messages",
    "get_author_profile",
    "get_message_context",
    "get_reply_chain",
    "understand_image",
    "understand_video",
}

WRITE_TOOL_NAMES = {
    "post_simple_topic",
    "post_sectioned_topic",
    "watch_topic",
    "update_topic_source_messages",
    "discard_topic",
    "record_observation",
    "finalize_run",
}


TOPIC_EDITOR_SYSTEM_PROMPT = """You are the BNDC live-update topic editor.

Review the supplied archived Discord messages and active topics. Use read tools
(search_topics, search_messages, get_author_profile, get_message_context,
get_reply_chain) when you need more context.

`search_messages` supports Discord-style filter params. Examples:
- `search_messages(query="Wan 2.5", from_author_id=*** has=["video"], scope="archive", after="7d")` to find a user's recent video posts about a tool
- `search_messages(in_channel_id=456, after="24h", has=["image"])` to scan a channel for recent generations
- `get_reply_chain(message_id="789")` when a source message has `reply_to_message_id` set

For every concrete development worth acting on, call a
decision tool (post_simple_topic, post_sectioned_topic, watch_topic,
update_topic_source_messages, discard_topic, record_observation).

You operate in a multi-turn loop. After each batch of tool calls you make, you
will receive tool_result messages and may continue iterating — search, decide,
search more, decide more. Budget: up to 50 turns per run.

REQUIRED to end the run: call the `finalize_run` tool exactly once, with
`overall_reasoning` describing what you saw in the window, what you
considered, what you skipped and why, and what (if anything) you acted on
(minimum 80 characters, full sentences). The run does not close until you
call `finalize_run` — you cannot end the turn by emitting plain text alone.

Media-bearing messages in the source payload may include pre-computed
`media_understandings` (cached from prior runs, keyed by message and
attachment index). These are surfaced in the ``media_understandings``
list on each source message so you can read them for free without calling
a vision tool. For uncached media that could affect editorial judgment,
call `understand_image` or `understand_video`. Use ``kind`` to
discriminate: skip ``workflow_graph`` items entirely; consider
``generation`` items with ``aesthetic_quality >= 6`` for editorial
framing; cite ``technical_signal`` and ``subject`` when writing about
any media-backed topic.

The runtime may auto-shortlist media posts that crossed the reaction threshold.
These appear in `auto_shortlisted_media` and as active topics with
`state="watching"`. For each shortlisted media item, explicitly decide what to
do: use `get_message_context`, `get_reply_chain`, `search_messages`, and
`understand_image`/`understand_video` when the media could be publishable; then
publish it with a compact header/context if there is a real story, keep
watching if signal is still forming, or call `discard_topic` if it is just a
fun throwaway that is not worth the live feed. If it is publishable but mostly
visual, the vision result may support a short, playful caption, but do not make
up details not present in the source or understanding.

## Structured Document Topics (post_sectioned_topic with blocks)

For multi-source / multi-section topics, and for ANY topic that includes an
image, video, embed, or external media link, use the new `blocks` array in
`post_sectioned_topic` over `post_simple_topic` or the legacy `sections` field.
Rules:

1. **Every factual block gets its own sources.**  Each block object in the
   `blocks` array must include a `source_message_ids` list citing the
   Discord messages that support that specific block.  Do NOT rely on a
   global topic-level `source_message_ids` for blocks — the global field is
   a backwards-compat fallback only.

2. **Media is attached to the relevant block.**  If a block discusses a
   specific image or video from a source message, include it in that
   block's `media_refs`.  Do NOT put all media in a global list — each
   media ref lives with the block that owns it.

3. **Canonical media-ref shape:** `{"message_id": "...", "kind": "attachment"|"embed"|"external", "index": N}`.
   Shorthand `{"message_id": "...", "attachment_index": N}` is accepted and
   normalised to `{"kind": "attachment", "index": N}` automatically.
   `kind: "external"` refs point to off-platform media links (Reddit, X, etc.)
   that are resolved best-effort — they are secondary to Discord attachments
   and always bound to a specific block.

4. **No global Sources footer.**  For structured (blocks) topics, source
   citations are rendered inline as bracketed links (e.g. `[[1]](url)`)
   next to the relevant block.  Do NOT append a "Sources: ..." line at the
   end of the topic — the publisher handles citation rendering per block.

5. **Block types:** Use `"type": "intro"` for the opening/intro block and
   `"type": "section"` for each following section.  Each block must have
   `"text"` (the prose content) and may have an optional `"title"`.

6. **Media refs use message_id, not CDN URLs.**  Reference media by the
   stable `{message_id, kind, index}` tuple — do NOT copy raw CDN URLs.
   The publisher resolves media URLs at publish time from stored message
   metadata.

Legacy `sections` field is still accepted for backward compatibility; when
used the global `source_message_ids` list applies to all sections equally,
and no per-section media refs are available.
"""


TOPIC_EDITOR_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_topics",
        "description": "Find existing topics by headline, canonical key, aliases, or state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "state_filter": {"type": "array", "items": {"type": "string"}},
                "hours_back": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_messages",
        "description": (
            "Search Discord messages. Filter parameters are AND-combined; any combination is valid (all are optional). "
            "Use `scope` to choose: 'window' (current source-message window, cheap, default) or 'archive' (full discord_messages, broader context). "
            "Use this when you need 'from:author has:video after:Xd' style queries to ground editorial framing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free-text content match (ILIKE)"},
                "from_author_id": {"type": "integer", "description": "equivalent to Discord 'from:' — restrict to messages this author wrote"},
                "in_channel_id": {"type": "integer", "description": "equivalent to Discord 'in:' — restrict to one channel"},
                "mentions_author_id": {"type": "integer", "description": "equivalent to Discord 'mentions:' — restrict to messages that mention this author"},
                "has": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["image", "video", "audio", "link", "embed", "file"]},
                    "description": "equivalent to Discord 'has:' — filter by attachment/embed kind. Array combines AND.",
                },
                "after": {"type": "string", "description": "lower-bound time. Accepts ISO timestamp OR relative like '24h', '7d', '30d'"},
                "before": {"type": "string", "description": "upper-bound time. Same format as after"},
                "is_reply": {"type": "boolean", "description": "if true, only messages that are replies (reply_to_message_id IS NOT NULL)"},
                "limit": {"type": "integer", "description": "default 20, max 50"},
                "scope": {"type": "string", "enum": ["window", "archive"], "description": "default 'window'"},
            },
            "required": [],
        },
    },
    {
        "name": "get_author_profile",
        "description": "Return author context from the current source window.",
        "input_schema": {
            "type": "object",
            "properties": {"author_id": {"type": "integer"}},
            "required": ["author_id"],
        },
    },
    {
        "name": "get_message_context",
        "description": "Fetch messages by id from the current source window.",
        "input_schema": {
            "type": "object",
            "properties": {"message_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["message_ids"],
        },
    },
    {
        "name": "get_reply_chain",
        "description": "Walk the reply chain backwards from a message. Returns ancestor messages root-first. Use when a generation or post is a reply and you want to understand what it's responding to.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "max_depth": {"type": "integer", "description": "default 5, max 15"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "post_simple_topic",
        "description": (
            "Publish a text-only single-author, one or two source-message topic. "
            "Do not use this for images, videos, embeds, or media refs; use "
            "post_sectioned_topic with blocks[].media_refs instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "body": {"type": "string"},
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "media": {"type": "array", "items": {"type": "string"}},
                "parent_topic_id": {"type": "string"},
                "notes": {"type": "string"},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "body", "source_message_ids"],
        },
    },
    {
        "name": "post_sectioned_topic",
        "description": (
            "Publish a multi-source or multi-contributor topic. Prefer the new "
            "`blocks` array for structured document topics; `sections` is still "
            "accepted for backwards compatibility. Every factual block MUST include "
            "its own `source_message_ids`; media (images, video, embeds) MUST be "
            "attached to the relevant block via `media_refs`, not to a global list. "
            "Media refs use a stable reference shape: "
            "`{\"message_id\": \"...\", \"kind\": \"attachment\"|\"embed\", \"index\": N}` "
            "(shorthand `{\"message_id\": \"...\", \"attachment_index\": N}` is also "
            "accepted). Do NOT include a global Sources footer — citations are "
            "rendered inline per block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "body": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "object"}},
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["intro", "section"]},
                            "title": {"type": "string"},
                            "text": {"type": "string"},
                            "source_message_ids": {"type": "array", "items": {"type": "string"}},
                            "media_refs": {
                                "description": "Media references for this block. Use kind='attachment'/'embed' for Discord-hosted media (preferred, indexed first) and kind='external' for off-platform links (Reddit, X, etc.) that are resolved best-effort. External refs are always block-bound and secondary.",
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "message_id": {"type": "string"},
                                        "kind": {"type": "string", "enum": ["attachment", "embed", "external"]},
                                        "index": {"type": "integer"},
                                        "attachment_index": {"type": "integer"},
                                    },
                                    "required": ["message_id"],
                                },
                            },
                        },
                        "required": ["type", "text"],
                    },
                },
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "parent_topic_id": {"type": "string"},
                "notes": {"type": "string"},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "body", "source_message_ids"],
        },
    },
    {
        "name": "watch_topic",
        "description": "Track a promising topic that is not ready to publish.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "why_interesting": {"type": "string"},
                "revisit_when": {"type": "string"},
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "why_interesting", "revisit_when", "source_message_ids"],
        },
    },
    {
        "name": "update_topic_source_messages",
        "description": "Append source messages to an existing topic without publishing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {"type": "string"},
                "new_source_message_ids": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": ["topic_id", "new_source_message_ids"],
        },
    },
    {
        "name": "discard_topic",
        "description": "Discard a watching topic that is no longer useful.",
        "input_schema": {
            "type": "object",
            "properties": {"topic_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["topic_id", "reason"],
        },
    },
    {
        "name": "record_observation",
        "description": "Record a near-miss or considered-but-not-posted item. Use sparingly; storage is capped at 3 observations per run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "observation_kind": {"type": "string", "enum": ["near_miss", "considered"]},
                "reason": {"type": "string"},
            },
            "required": ["source_message_ids", "observation_kind", "reason"],
        },
    },
    {
        "name": "understand_image",
        "description": (
            "Analyze an image attachment from a source message. Returns structured "
            "JSON with kind, subject, technical_signal, aesthetic_quality (0-10), "
            "and discriminator_notes. Cached results are reused across runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer"},
                "attachment_index": {"type": "integer", "default": 0},
                "mode": {"type": "string", "enum": ["fast", "best"], "default": "fast"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "understand_video",
        "description": (
            "Analyze a video attachment from a source message. Returns structured "
            "JSON with summary, visual_read, audio_read, edit_value, highlight_score, "
            "energy, pacing, production_quality, boundary_notes, cautions, and kind. "
            "Cached results are reused across runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer"},
                "attachment_index": {"type": "integer", "default": 0},
                "mode": {"type": "string", "enum": ["fast", "best"], "default": "fast"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "finalize_run",
        "description": (
            "MUST be called exactly once to end the run. Provide your overall editorial reasoning "
            "describing what you saw in the source window, what you considered, what you skipped "
            "and why, and what (if anything) you acted on. Minimum 100 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "overall_reasoning": {
                    "type": "string",
                    "description": "Editorial summary, ≥100 characters. Full sentences.",
                },
                "topics_considered": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short bullets naming clusters you looked at (acted on or skipped).",
                },
            },
            "required": ["overall_reasoning"],
        },
    },
]


class TopicEditor:
    """Topic-centered live-update editor runtime.

    The runtime only talks to the injected db handler. Storage-level details stay
    behind db_handler wrappers so tests and rollback wiring can swap the backend.
    """

    def __init__(
        self,
        bot: Any = None,
        *,
        db_handler: Any = None,
        llm_client: Any = None,
        guild_id: Optional[int] = None,
        live_channel_id: Optional[int] = None,
        environment: Optional[str] = None,
        model: Optional[str] = None,
        source_limit: Optional[int] = None,
    ) -> None:
        self.bot = bot
        self.db = db_handler or getattr(bot, "db", None) or getattr(bot, "db_handler", None)
        self.llm_client = llm_client or getattr(bot, "claude_client", None)
        self.guild_id = guild_id
        self.live_channel_id = live_channel_id
        self.environment = environment or ("dev" if getattr(bot, "dev_mode", False) else os.getenv("LIVE_UPDATE_ENVIRONMENT", "prod"))
        self.model = model or os.getenv("TOPIC_EDITOR_MODEL") or DEFAULT_LIVE_UPDATE_MODEL
        self.source_limit = int(source_limit or os.getenv("TOPIC_EDITOR_SOURCE_LIMIT", "200"))
        self.publishing_enabled = os.getenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "false").lower() == "true"
        self.trace_channel_id = os.getenv("LIVE_UPDATE_TRACE_CHANNEL_ID")
        self.media_shortlist_min_reactions = self._env_int("TOPIC_EDITOR_MEDIA_SHORTLIST_MIN_REACTIONS", 5)
        self.media_shortlist_limit = self._env_int("TOPIC_EDITOR_MEDIA_SHORTLIST_LIMIT", 5)

    async def run_once(self, trigger: str = "scheduled") -> Dict[str, Any]:
        if not self.db:
            raise RuntimeError("TopicEditor requires an injected db_handler")
        if not self.llm_client:
            raise RuntimeError("TopicEditor requires an Anthropic/Claude client")

        started = time.monotonic()
        guild_id = self._resolve_guild_id()
        live_channel_id = self._resolve_live_channel_id(guild_id)
        checkpoint_key = self._checkpoint_key(guild_id, live_channel_id)
        cold_start_seeded = False
        checkpoint = self.db.get_topic_editor_checkpoint(checkpoint_key, environment=self.environment)
        if checkpoint is None:
            checkpoint = self.db.mirror_live_checkpoint_to_topic_editor(checkpoint_key, environment=self.environment)
        if checkpoint is None:
            checkpoint = self._seed_cold_start_checkpoint(checkpoint_key, guild_id, live_channel_id)
            cold_start_seeded = True

        run = self.db.acquire_topic_editor_run({
            "guild_id": guild_id,
            "live_channel_id": live_channel_id,
            "trigger": trigger,
            "checkpoint_before": checkpoint,
            "model": self.model,
            "publishing_enabled": self.publishing_enabled,
            "trace_channel_id": self.trace_channel_id,
        }, environment=self.environment)
        if not run:
            return {"status": "skipped", "reason": "lease_not_acquired", "checkpoint_key": checkpoint_key}
        run_id = str(run.get("run_id"))
        logger.info(
            "TopicEditor run acquired: run_id=%s env=%s guild=%s live_channel=%s checkpoint=%s publishing=%s model=%s",
            run_id,
            self.environment,
            guild_id,
            live_channel_id,
            {
                "last_message_id": (checkpoint or {}).get("last_message_id"),
                "last_message_created_at": (checkpoint or {}).get("last_message_created_at"),
            },
            self.publishing_enabled,
            self.model,
        )

        metadata: Dict[str, Any] = {
            "tool_calls": [],
            "publishing_enabled": self.publishing_enabled,
            "trigger": trigger,
        }
        try:
            # Cold-start no longer short-circuits to zero — the seeded checkpoint
            # is anchored to (lookback_minutes ago), so the same query returns
            # the last interval's worth of messages.
            logger.info(
                "TopicEditor fetching source messages: run_id=%s source_limit=%s",
                run_id,
                self.source_limit,
            )
            messages = self.db.get_archived_messages_after_checkpoint(
                checkpoint=checkpoint,
                guild_id=guild_id,
                channel_ids=None,
                limit=self.source_limit,
                exclude_author_ids=self._excluded_author_ids(),
            )
            logger.info(
                "TopicEditor fetched source messages: run_id=%s count=%s",
                run_id,
                len(messages or []),
            )
            logger.info("TopicEditor fetching known topics: run_id=%s", run_id)
            known_topics = self.db.get_topics(
                guild_id=guild_id,
                states=["posted", "watching", "discarded"],
                limit=300,
                environment=self.environment,
            )
            logger.info(
                "TopicEditor fetched known topics: run_id=%s count=%s",
                run_id,
                len(known_topics or []),
            )
            auto_shortlisted_media = self._auto_shortlist_media_messages(
                messages,
                known_topics,
                run_id=run_id,
                guild_id=guild_id,
            )
            active_topics = [
                topic for topic in known_topics
                if topic.get("state") in {"posted", "watching"}
            ]
            active_keys = {str(topic.get("canonical_key") or "") for topic in active_topics}
            for entry in auto_shortlisted_media:
                topic = entry.get("topic")
                key = str((topic or {}).get("canonical_key") or "")
                if topic and key not in active_keys:
                    active_topics.append(topic)
                    active_keys.add(key)
            logger.info(
                "TopicEditor active topic set ready: run_id=%s active=%s auto_shortlisted=%s",
                run_id,
                len(active_topics),
                len(auto_shortlisted_media),
            )
            logger.info("TopicEditor fetching aliases: run_id=%s", run_id)
            aliases = self.db.get_topic_aliases(guild_id=guild_id, environment=self.environment)
            logger.info(
                "TopicEditor fetched aliases: run_id=%s count=%s",
                run_id,
                len(aliases or []),
            )
            if not messages:
                updates = self._run_updates(
                    checkpoint_before=checkpoint,
                    checkpoint_after=checkpoint,
                    messages=[],
                    tool_calls=[],
                    started=started,
                    metadata=metadata,
                    skipped_reason="no_new_archived_messages",
                )
                self.db.complete_topic_editor_run(run_id, updates, guild_id=guild_id, environment=self.environment)
                trace_messages = self._format_trace_messages(run_id, updates, [], [])
                await self._emit_trace(trace_messages, run_id=run_id, updates=updates, outcomes=[], publish_results=[])
                return {
                    "status": "completed",
                    "run_id": run_id,
                    "skipped_reason": "no_new_archived_messages",
                    "trace_messages": trace_messages,
                }

            # --- Agent loop with 100-turn budget ---
            max_turns = int(os.getenv("TOPIC_EDITOR_MAX_TURNS", "100"))
            initial_payload = self._build_initial_user_payload(
                messages,
                active_topics,
                auto_shortlisted_media=auto_shortlisted_media,
            )
            messages_arg: List[Dict[str, Any]] = [
                {"role": "user", "content": [{"type": "text", "text": repr(initial_payload)}]}
            ]
            dispatcher_context = {
                "run_id": run_id,
                "guild_id": guild_id,
                "live_channel_id": live_channel_id,
                "messages": messages,
                "active_topics": active_topics,
                "aliases": aliases,
                "seen_tool_call_ids": set(),
                "idempotent_results": {},
                "observation_count": 0,
                "created_topics": [],
                "finalize": None,
                "vision_budget_usd": self._env_float("TOPIC_EDITOR_VISION_BUDGET_PER_RUN", 1.0),
                "vision_cost_usd": 0.0,
            }
            tool_calls: List[Dict[str, Any]] = []
            outcomes: List[Dict[str, Any]] = []
            total_input_tokens = 0
            total_output_tokens = 0
            cumulative_tokens = 0
            cumulative_cost_usd = 0.0
            has_cost_estimate = False
            text_chunks: List[str] = []
            forced_close = False
            forced_close_reason: Optional[str] = None
            turn_count = 0
            max_cost_usd = self._env_float("TOPIC_EDITOR_MAX_COST_USD", 5.0)
            max_tokens = self._env_int("TOPIC_EDITOR_MAX_TOKENS", 500000)
            for turn_count in range(1, max_turns + 1):
                logger.info(
                    "TopicEditor invoking LLM: run_id=%s turn=%s messages=%s",
                    run_id,
                    turn_count,
                    len(messages_arg),
                )
                response = await self._invoke_anthropic(messages_arg)
                turn_tool_calls = self._extract_tool_calls(response)
                logger.info(
                    "TopicEditor LLM turn complete: run_id=%s turn=%s tool_calls=%s tools=%s",
                    run_id,
                    turn_count,
                    len(turn_tool_calls),
                    [call.get("name") for call in turn_tool_calls],
                )
                turn_reasoning = self._extract_reasoning_text(response)
                if turn_reasoning:
                    text_chunks.append(turn_reasoning)
                usage = self._extract_usage(response)
                total_input_tokens += int(usage.get("input_tokens", 0) or 0)
                total_output_tokens += int(usage.get("output_tokens", 0) or 0)
                cumulative_tokens = total_input_tokens + total_output_tokens
                turn_cost = self._estimate_cost_usd(usage)
                if turn_cost is not None:
                    has_cost_estimate = True
                    cumulative_cost_usd = round(cumulative_cost_usd + float(turn_cost), 6)

                cap_reason = None
                if has_cost_estimate and cumulative_cost_usd > max_cost_usd:
                    cap_reason = "cost_cap_exceeded"
                elif cumulative_tokens > max_tokens:
                    cap_reason = "token_cap_exceeded"
                if cap_reason:
                    # The response has already been paid for. If it contains the
                    # required finalizer, accept only that close-out tool so we do
                    # not lose the agent's audit reasoning at the budget edge.
                    finalize_calls = [call for call in turn_tool_calls if call.get("name") == "finalize_run"]
                    if finalize_calls:
                        tool_calls.extend(finalize_calls)
                        self._populate_idempotent_results(finalize_calls, dispatcher_context)
                        for call in finalize_calls:
                            outcomes.append(self._dispatch_tool_call(call, dispatcher_context))
                        if dispatcher_context.get("finalize"):
                            metadata["budget_cap_exceeded_after_finalize"] = cap_reason
                            break
                    forced_close = True
                    forced_close_reason = cap_reason
                    break

                if not turn_tool_calls:
                    # Agent ended with text only — push back demanding finalize_run.
                    assistant_content = self._assistant_content_from_response(response)
                    if assistant_content:
                        messages_arg.append({"role": "assistant", "content": assistant_content})
                    messages_arg.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "You ended without calling `finalize_run`. The run cannot "
                                        "close until you call it with your overall editorial "
                                        "reasoning (≥100 chars). Call it now."
                                    ),
                                }
                            ],
                        }
                    )
                    if turn_count >= max_turns:
                        forced_close = True
                        forced_close_reason = "max_turns_reached_without_finalize"
                        break
                    continue

                # Dispatch each tool call this turn, building tool_result blocks.
                tool_calls.extend(turn_tool_calls)
                self._populate_idempotent_results(turn_tool_calls, dispatcher_context)
                turn_results: List[Dict[str, Any]] = []
                for call in turn_tool_calls:
                    outcome = self._dispatch_tool_call(call, dispatcher_context)
                    outcomes.append(outcome)
                    turn_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.get("id"),
                            "content": self._tool_result_content(call, outcome),
                        }
                    )
                # Build assistant turn + user tool_result turn for the next iteration.
                assistant_content = self._assistant_content_from_response(response)
                if assistant_content:
                    messages_arg.append({"role": "assistant", "content": assistant_content})
                messages_arg.append({"role": "user", "content": turn_results})

                if dispatcher_context.get("finalize"):
                    break

                if turn_count >= max_turns:
                    forced_close = True
                    forced_close_reason = "max_turns_reached_without_finalize"
                    break

            if forced_close and not dispatcher_context.get("finalize"):
                # Loud audit row when we hit the budget without a clean finalize.
                reason = forced_close_reason or "max_turns_reached_without_finalize"
                self._store_transition({
                    "run_id": run_id,
                    "guild_id": guild_id,
                    "action": "rejected_finalize_run",
                    "reason": reason,
                    "payload": shape_transition_payload(
                        outcome="tool_error",
                        tool_name="finalize_run",
                        error=self._forced_close_error(reason, max_turns, cumulative_cost_usd, cumulative_tokens),
                        extra={
                            "cumulative_cost_usd": cumulative_cost_usd if has_cost_estimate else None,
                            "cumulative_tokens": cumulative_tokens,
                            "max_cost_usd": max_cost_usd,
                            "max_tokens": max_tokens,
                        },
                    ),
                    "model": self.model,
                })

            metadata["tool_calls"] = [
                {"id": call["id"], "name": call["name"], "input": call["input"]}
                for call in tool_calls
            ]
            metadata["usage"] = {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}
            metadata["cumulative_cost_usd"] = cumulative_cost_usd if has_cost_estimate else None
            metadata["cumulative_tokens"] = cumulative_tokens
            metadata["max_cost_usd"] = max_cost_usd
            metadata["max_tokens"] = max_tokens
            metadata["turn_count"] = turn_count
            metadata["forced_close"] = forced_close
            metadata["forced_close_reason"] = forced_close_reason
            finalize = dispatcher_context.get("finalize") or {}
            metadata["reasoning"] = finalize.get("overall_reasoning") or "\n\n".join(text_chunks).strip()
            metadata["topics_considered"] = finalize.get("topics_considered") or []
            # Capture surface info for the trace embed.
            metadata["source_message_timestamps"] = [m.get("created_at") for m in messages if m.get("created_at")]
            metadata["source_channel_counts"] = self._tally_channels(messages)
            metadata["active_topics_count"] = len(active_topics)
            metadata["auto_shortlisted_media"] = [
                {
                    "topic_id": entry.get("topic_id"),
                    "message_id": entry.get("message_id"),
                    "reaction_count": entry.get("reaction_count"),
                    "media_ref": entry.get("media_ref"),
                    "headline": entry.get("headline"),
                    "status": entry.get("status"),
                }
                for entry in auto_shortlisted_media
            ]

            publish_results = await self._publish_created_topics(dispatcher_context["created_topics"])
            metadata["publish_results"] = publish_results
            checkpoint_after = self._checkpoint_after(checkpoint, messages, run_id)
            self.db.upsert_topic_editor_checkpoint(checkpoint_after, environment=self.environment)
            updates = self._run_updates(
                checkpoint_before=checkpoint,
                checkpoint_after=checkpoint_after,
                messages=messages,
                tool_calls=tool_calls,
                started=started,
                metadata={**metadata, "outcomes": outcomes},
                accepted_count=sum(1 for outcome in outcomes if outcome.get("outcome") == "accepted"),
                rejected_count=sum(1 for outcome in outcomes if str(outcome.get("outcome", "")).startswith("rejected")),
                override_count=sum(int(outcome.get("override_count", 0)) for outcome in outcomes),
                observation_count=sum(1 for outcome in outcomes if outcome.get("action") == "observation"),
                published_count=sum(1 for result in publish_results if result.get("status") == "sent"),
                failed_publish_count=sum(1 for result in publish_results if result.get("status") in {"failed", "partial"}),
                status="failed" if forced_close else "completed",
            )
            self.db.complete_topic_editor_run(run_id, updates, guild_id=guild_id, environment=self.environment)
            trace_messages = self._format_trace_messages(run_id, updates, outcomes, publish_results)
            await self._emit_trace(trace_messages, run_id=run_id, updates=updates, outcomes=outcomes, publish_results=publish_results)
            status = updates.get("status") or "completed"
            return {
                "status": status,
                "run_id": run_id,
                "tool_calls": len(tool_calls),
                "outcomes": outcomes,
                "publish_results": publish_results,
                "trace_messages": trace_messages,
            }
        except Exception as exc:
            updates = self._run_updates(
                checkpoint_before=checkpoint,
                checkpoint_after=checkpoint,
                messages=[],
                tool_calls=[],
                started=started,
                metadata={**metadata, "error": str(exc)},
            )
            self.db.fail_topic_editor_run(run_id, str(exc), updates, guild_id=guild_id, environment=self.environment)
            raise

    async def _invoke_anthropic(self, messages_arg: Sequence[Dict[str, Any]]) -> Any:
        """One-shot LLM call. The agent loop in `run_once` drives multi-turn behavior."""
        client = getattr(self.llm_client, "client", self.llm_client)
        if hasattr(client, "messages") and hasattr(client.messages, "create"):
            return await client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=TOPIC_EDITOR_SYSTEM_PROMPT,
                messages=list(messages_arg),
                tools=TOPIC_EDITOR_TOOLS,
            )
        return await self.llm_client.generate_chat_completion(
            model=self.model,
            system_prompt=TOPIC_EDITOR_SYSTEM_PROMPT,
            messages=list(messages_arg),
            max_tokens=4096,
            tools=TOPIC_EDITOR_TOOLS,
        )

    # Known model presets for enrichment — image models for image understanding,
    # video models for video understanding.  We loop over all four so any cached
    # row produced by the dispatcher is surfaced in the initial payload.
    _IMAGE_MODEL_PRESETS = ("gpt-4o-mini", "gpt-5.4")
    _VIDEO_MODEL_PRESETS = ("gemini-2.5-flash", "gemini-2.5-pro")
    _ALL_MODEL_PRESETS = _IMAGE_MODEL_PRESETS + _VIDEO_MODEL_PRESETS

    def _build_initial_user_payload(
        self,
        messages: Sequence[Dict[str, Any]],
        active_topics: Sequence[Dict[str, Any]],
        auto_shortlisted_media: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        source_messages: List[Dict[str, Any]] = []
        for message in messages:
            payload = self._message_payload(message)
            payload["media_understandings"] = self._enrich_media_understandings(message)
            source_messages.append(payload)
        return {
            "source_messages": source_messages,
            "active_topics": [
                {
                    "topic_id": topic.get("topic_id"),
                    "canonical_key": topic.get("canonical_key"),
                    "headline": topic.get("headline"),
                    "state": topic.get("state"),
                    "summary": topic.get("summary"),
                    "revisit_at": topic.get("revisit_at"),
                    "source_message_ids": topic.get("source_message_ids") or [],
                    "aliases": topic.get("aliases") or [],
                }
                for topic in active_topics
            ],
            "auto_shortlisted_media": [
                {
                    "topic_id": item.get("topic_id"),
                    "message_id": item.get("message_id"),
                    "reaction_count": item.get("reaction_count"),
                    "headline": item.get("headline"),
                    "reason": item.get("reason"),
                    "media_ref": item.get("media_ref"),
                    "next_action": (
                        "Inspect with message/reply/search context plus understand_image or "
                        "understand_video if not already cached; then publish, keep watching, "
                        "or discard_topic."
                    ),
                }
                for item in auto_shortlisted_media or []
            ],
        }

    def _enrich_media_understandings(
        self, message: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Best-effort: query cache for known model presets.

        Returns a list of understanding dicts (one per cached row).
        Missing rows produce an empty list — never a failure.
        """
        message_id = message.get("message_id")
        if message_id is None:
            return []

        attachments = TopicEditor._normalize_attachment_list(message.get("attachments"))
        if not attachments:
            return []

        results: List[Dict[str, Any]] = []
        for idx in range(len(attachments)):
            for model in self._ALL_MODEL_PRESETS:
                try:
                    row = self.db.get_message_media_understanding(
                        message_id, idx, model
                    )
                except Exception:
                    continue  # best-effort — skip this preset
                if row is None:
                    continue

                understanding = row.get("understanding") or {}
                item: Dict[str, Any] = {
                    "attachment_index": idx,
                    "kind": understanding.get("kind"),
                    "subject": understanding.get("subject"),
                    "technical_signal": understanding.get("technical_signal"),
                    "aesthetic_quality": understanding.get("aesthetic_quality"),
                    "model": model,
                }
                # Attach video-specific fields when present (Gemini models).
                for vf in (
                    "summary",
                    "visual_read",
                    "audio_read",
                    "edit_value",
                    "highlight_score",
                    "energy",
                    "pacing",
                    "production_quality",
                    "boundary_notes",
                    "cautions",
                ):
                    if vf in understanding:
                        item[vf] = understanding[vf]
                results.append(item)

        return results

    def _extract_reasoning_text(self, response: Any) -> str:
        chunks: List[str] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                if text and text.strip():
                    chunks.append(text.strip())
        return "\n\n".join(chunks).strip()

    def _tool_result_content(self, call: Dict[str, Any], outcome: Dict[str, Any]) -> str:
        """Build a concise tool_result content string to feed back into the agent loop."""
        name = call.get("name") or "?"
        outcome_name = str(outcome.get("outcome") or "unknown")
        if outcome_name in {"rejected_post_simple", "rejected_post_sectioned", "rejected_watch"} or outcome_name.startswith("rejected"):
            err = outcome.get("error") or outcome_name
            return f"tool={name} status={outcome_name} error={err}"
        if outcome_name == "tool_error":
            err = outcome.get("error") or "tool_error"
            return f"tool={name} status=tool_error error={err}"
        if outcome_name == "idempotent_replay":
            return f"tool={name} status=idempotent_replay (already executed in this run)"
        if name in READ_TOOL_NAMES:
            result = outcome.get("result")
            encoded = json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > 2000:
                encoded = encoded[:1970] + "...<truncated>"
            return f"tool={name} status=ok result={encoded}"
        action = outcome.get("action") or name
        topic_id = outcome.get("topic_id")
        if topic_id:
            return f"tool={name} status=accepted action={action} topic_id={topic_id}"
        return f"tool={name} status={outcome_name} action={action}"

    def _tally_channels(self, messages: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for message in messages or []:
            name = message.get("channel_name") or "?"
            counts[name] = counts.get(name, 0) + 1
        return counts

    def _assistant_content_from_response(self, response: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "openai_assistant_message":
                raw_message = getattr(block, "message", None) or (block.get("message") if isinstance(block, dict) else None)
                if isinstance(raw_message, dict):
                    return [{"type": "openai_assistant_message", "message": raw_message}]

        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                if text:
                    out.append({"type": "text", "text": text})
            elif block_type == "reasoning_content":
                reasoning_content = (
                    getattr(block, "reasoning_content", None)
                    or (block.get("reasoning_content") if isinstance(block, dict) else "")
                )
                if reasoning_content:
                    out.append({"type": "reasoning_content", "reasoning_content": reasoning_content})
            elif block_type == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", None) or block.get("id"),
                        "name": getattr(block, "name", None) or block.get("name"),
                        "input": getattr(block, "input", None) or block.get("input") or {},
                    }
                )
        return out

    def _dispatch_tool_call(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        name = call["name"]
        args = call["input"]
        # d3 first: DB-backed cross-run replay (returns prior outcome if (run_id, tool_call_id) was seen in a prior process)
        replay_outcome = self._idempotent_replay_outcome(call, context)
        if replay_outcome:
            return replay_outcome
        # d1: real read-tool dispatch when read tools are called
        if name in READ_TOOL_NAMES:
            return self._dispatch_read_tool(call, context)
        # d3: in-process idempotency fast path for write tools within a single run
        if name in WRITE_TOOL_NAMES and self._is_idempotent_replay(call, context):
            return {"tool_call_id": call.get("id"), "tool": name, "outcome": "idempotent_replay"}
        if name == "record_observation":
            if int(context.get("observation_count") or 0) >= 3:
                self._store_transition({
                    "run_id": context["run_id"],
                    "guild_id": context["guild_id"],
                    "tool_call_id": call["id"],
                    "action": "observation",
                    "reason": "observation_cap_reached",
                    "payload": shape_transition_payload(
                        outcome="tool_error",
                        tool_name=name,
                        source_message_ids=args.get("source_message_ids"),
                        error="observation_cap_reached",
                    ),
                    "model": self.model,
                })
                return {"tool_call_id": call["id"], "tool": name, "outcome": "tool_error", "action": "observation", "error": "observation_cap_reached"}
            source_ids = self._unique_ids(args.get("source_message_ids") or [])
            self.db.store_editorial_observation({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "source_message_ids": source_ids,
                "observation_kind": args.get("observation_kind") or "considered",
                "reason": self._cap_text(args.get("reason"), 500),
            }, environment=self.environment)
            context["observation_count"] = int(context.get("observation_count") or 0) + 1
            self._store_transition({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "observation",
                "reason": self._cap_text(args.get("reason"), 500),
                "payload": shape_transition_payload(outcome="accepted", tool_name=name, source_message_ids=source_ids),
                "model": self.model,
            })
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", "action": "observation"}
        if name in {"post_simple_topic", "post_sectioned_topic", "watch_topic"}:
            return self._dispatch_create_topic_tool(call, context)
        if name == "update_topic_source_messages":
            return self._dispatch_update_sources(call, context)
        if name == "discard_topic":
            return self._dispatch_discard(call, context)
        if name == "finalize_run":
            return self._dispatch_finalize_run(call, context)
        return {"tool_call_id": call["id"], "tool": name, "outcome": "unknown_tool"}

    def _dispatch_read_tool(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        name = call["name"]
        args = call.get("input") or {}
        try:
            if name == "search_topics":
                result = self.db.search_topic_editor_topics(
                    query=args.get("query") or "",
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    state_filter=args.get("state_filter"),
                    hours_back=int(args.get("hours_back") or 72),
                    limit=10,
                )
            elif name == "search_messages":
                scope = str(args.get("scope") or "window").lower()
                if scope == "window":
                    result = self._search_window_messages(
                        context.get("messages") or [],
                        query=args.get("query"),
                        from_author_id=args.get("from_author_id"),
                        in_channel_id=args.get("in_channel_id"),
                        mentions_author_id=args.get("mentions_author_id"),
                        has=args.get("has"),
                        after=args.get("after"),
                        before=args.get("before"),
                        is_reply=args.get("is_reply"),
                        limit=int(args.get("limit") if args.get("limit") is not None else 20),
                    )
                elif scope == "archive":
                    result = self.db.search_messages_unified(
                        scope="archive",
                        guild_id=context.get("guild_id"),
                        environment=self.environment,
                        query=args.get("query"),
                        from_author_id=args.get("from_author_id"),
                        in_channel_id=args.get("in_channel_id"),
                        mentions_author_id=args.get("mentions_author_id"),
                        has=args.get("has"),
                        after=args.get("after"),
                        before=args.get("before"),
                        is_reply=args.get("is_reply"),
                        limit=int(args.get("limit") if args.get("limit") is not None else 20),
                    )
                else:
                    raise ValueError(f"Unknown scope: {scope}")
            elif name == "get_author_profile":
                result = self.db.get_topic_editor_author_profile(
                    args.get("author_id"),
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                )
            elif name == "get_message_context":
                result = self.db.get_topic_editor_message_context(
                    args.get("message_ids") or [],
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    limit=10,
                )
            elif name == "understand_image":
                return self._dispatch_understand_media(call, context, "image")
            elif name == "understand_video":
                return self._dispatch_understand_media(call, context, "video")
            elif name == "get_reply_chain":
                max_depth = max(1, min(int(args.get("max_depth") or 5), 15))
                result = self.db.get_reply_chain(
                    message_id=str(args.get("message_id") or ""),
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    max_depth=max_depth,
                )
            else:
                result = None
            return {"tool_call_id": call["id"], "tool": name, "outcome": "read", "result": result}
        except Exception as exc:
            return {"tool_call_id": call.get("id"), "tool": name, "outcome": "tool_error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Image / video understanding tool dispatcher
    # ------------------------------------------------------------------

    # Fixed estimated costs in USD (deducted from the per-run vision budget
    # only when an actual API call is made, never on cache hits).
    _VISION_COST_IMAGE = 0.01
    _VISION_COST_VIDEO = 0.05

    # mode → model mapping
    _IMAGE_MODEL_MAP = {"fast": "gpt-4o-mini", "best": "gpt-5.4"}
    _VIDEO_MODEL_MAP = {"fast": "gemini-2.5-flash", "best": "gemini-2.5-pro"}

    def _dispatch_understand_media(
        self, call: Dict[str, Any], context: Dict[str, Any], media_kind: str
    ) -> Dict[str, Any]:
        """Shared dispatch for understand_image / understand_video.

        (a) Resolve source message from context['messages'] by message_id.
        (b) Resolve attachment URL.
        (c) Download bytes via sync ``requests.get(url)``.
        (d) Compute sha256.
        (e) Check PK cache → hash cache → budget.
        (f) Budget exceeded → return ``{outcome: budget_exceeded}``.
        (g) Call vision_clients.describe_image / describe_video.
        (h) Persist result, return compact JSON.
        """
        import requests

        from src.common.vision_clients import describe_image, describe_video, _sha256

        name = call["name"]
        args = call.get("input") or {}
        message_id = args.get("message_id")
        attachment_index = int(args.get("attachment_index") or 0)
        mode = args.get("mode") or "fast"

        # (a) resolve source message
        messages = context.get("messages") or []
        source = None
        for msg in messages:
            if str(msg.get("message_id")) == str(message_id):
                source = msg
                break
        if source is None:
            try:
                resolved = self.db.get_topic_editor_source_messages(
                    [str(message_id)],
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    limit=1,
                )
            except Exception:
                resolved = []
            if resolved:
                source = resolved[0]
            else:
                return {
                    "tool_call_id": call["id"],
                    "tool": name,
                    "outcome": "tool_error",
                    "error": f"message_id={message_id} not found in source window or archive",
                }

        # (b) resolve attachment URL
        attachments = TopicEditor._normalize_attachment_list(source.get("attachments"))
        if attachment_index < 0 or attachment_index >= len(attachments):
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": (
                    f"attachment_index={attachment_index} out of range "
                    f"(message has {len(attachments)} attachment(s))"
                ),
            }
        attachment = attachments[attachment_index]
        media_url = attachment.get("url") or attachment.get("proxy_url") or ""
        if not media_url:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": f"attachment {attachment_index} has no url field",
            }

        # model preset
        if media_kind == "image":
            model = self._IMAGE_MODEL_MAP.get(mode, "gpt-4o-mini")
        else:
            model = self._VIDEO_MODEL_MAP.get(mode, "gemini-2.5-flash")

        # (e) PK cache check
        try:
            cached = self.db.get_message_media_understanding(message_id, attachment_index, model)
        except Exception:
            cached = None
        if cached is not None:
            understanding = cached.get("understanding") or {}
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "read",
                "result": {"cached": True, "understanding": understanding},
            }

        # (c) download bytes via sync requests.get
        try:
            resp = requests.get(media_url, timeout=30)
            resp.raise_for_status()
            media_bytes = resp.content
        except Exception as exc:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": f"failed to download media: {exc}",
            }

        # (d) compute sha256
        content_hash = _sha256(media_bytes)

        # (e) hash cache check
        try:
            cached_by_hash = self.db.get_message_media_understanding_by_hash(content_hash, model=model)
        except Exception:
            cached_by_hash = None
        if cached_by_hash is not None:
            understanding = cached_by_hash.get("understanding") or {}
            # Persist the row for this (message_id, attachment_index) so future
            # PK lookups hit immediately, without another download.
            try:
                self.db.upsert_message_media_understanding({
                    "message_id": message_id,
                    "attachment_index": attachment_index,
                    "media_url": media_url,
                    "media_kind": media_kind,
                    "content_hash": content_hash,
                    "model": model,
                    "understanding": understanding,
                })
            except Exception:
                pass  # best-effort write
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "read",
                "result": {"cached": True, "dedup": True, "understanding": understanding},
            }

        # (f) budget check
        budget = float(context.get("vision_budget_usd") or 1.0)
        spent = float(context.get("vision_cost_usd") or 0.0)
        cost_estimate = self._VISION_COST_IMAGE if media_kind == "image" else self._VISION_COST_VIDEO
        if spent + cost_estimate > budget:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "budget_exceeded",
                "error": (
                    f"vision budget spent ${spent:.2f} of ${budget:.2f}; "
                    f"estimated cost ${cost_estimate:.2f} would exceed cap"
                ),
            }

        # (g) call vision API
        try:
            if media_kind == "image":
                understanding = describe_image(media_bytes, model)
            else:
                understanding = describe_video(media_bytes, model)
        except Exception as exc:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": f"vision API call failed: {exc}",
            }

        # deduct cost
        context["vision_cost_usd"] = round(spent + cost_estimate, 4)

        # (h) persist and return
        try:
            self.db.upsert_message_media_understanding({
                "message_id": message_id,
                "attachment_index": attachment_index,
                "media_url": media_url,
                "media_kind": media_kind,
                "content_hash": content_hash,
                "model": model,
                "understanding": understanding,
            })
        except Exception:
            pass  # best-effort write; still return the understanding

        return {
            "tool_call_id": call["id"],
            "tool": name,
            "outcome": "read",
            "result": {"cached": False, "understanding": understanding},
        }

    def _parse_time_bound(self, value: Optional[str], default: datetime) -> datetime:
        """Parse a time-bound string: ISO timestamp or relative like '24h', '7d'.

        Returns the parsed datetime or *default* if value is None.
        Raises ValueError for malformed input (surfaces as tool_error).
        """
        if value is None:
            return default
        # Try ISO timestamp first
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
        # Try relative pattern: number + h/d
        m = re.match(r'^(\d+)\s*([hd])$', str(value).strip().lower())
        if m:
            amount = int(m.group(1))
            unit = m.group(2)
            if unit == 'h':
                return datetime.now(timezone.utc) - timedelta(hours=amount)
            else:
                return datetime.now(timezone.utc) - timedelta(days=amount)
        raise ValueError(f"Invalid time format: {value}")

    def _search_window_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        query: Optional[str] = None,
        from_author_id: Optional[Any] = None,
        in_channel_id: Optional[Any] = None,
        mentions_author_id: Optional[Any] = None,
        has: Optional[List[str]] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        is_reply: Optional[bool] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search in-memory source messages with AND-combined Discord-style filters."""
        # --- helpers --------------------------------------------------
        _IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
        _VIDEO_EXT = {'.mp4', '.mov', '.webm', '.mkv'}
        _AUDIO_EXT = {'.mp3', '.wav', '.ogg', '.flac'}

        def _ext(filename: str) -> str:
            return (os.path.splitext(filename or '')[1] or '').lower()

        def _is_media_kind(attachments: List[Dict[str, Any]], kind: str) -> bool:
            for a in attachments:
                ct = str(a.get('content_type') or '').lower()
                fn = str(a.get('filename') or '').lower()
                if kind == 'image' and (ct.startswith('image/') or _ext(fn) in _IMAGE_EXT):
                    return True
                if kind == 'video' and (ct.startswith('video/') or _ext(fn) in _VIDEO_EXT):
                    return True
                if kind == 'audio' and (ct.startswith('audio/') or _ext(fn) in _AUDIO_EXT):
                    return True
                if kind == 'file':
                    return True  # any attachment exists
            return False

        # --- defaults -------------------------------------------------
        needle = str(query or '').lower()
        safe_limit = max(1, min(int(limit) if limit is not None else 20, 50))
        has_set = set(has or [])

        # Parse time bounds
        far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        far_future = datetime(2099, 12, 31, tzinfo=timezone.utc)
        after_dt = self._parse_time_bound(after, far_past)
        before_dt = self._parse_time_bound(before, far_future)

        rows: List[Dict[str, Any]] = []
        for message in messages or []:
            # --- from_author_id filter ---
            if from_author_id is not None and str(message.get('author_id')) != str(from_author_id):
                continue

            # --- in_channel_id filter ---
            if in_channel_id is not None and str(message.get('channel_id')) != str(in_channel_id):
                continue

            # --- mentions_author_id filter ---
            if mentions_author_id is not None:
                content = str(message.get('content') or '')
                if not re.search(rf'<@!?{mentions_author_id}>', content):
                    continue

            # --- content query filter ---
            content = str(message.get('content') or '')
            if needle and needle not in content.lower():
                continue

            # --- has filter ---
            if has_set:
                atts = self._normalize_attachment_list(message.get('attachments'))
                embs = self._normalize_attachment_list(message.get('embeds'))
                filter_pass = True
                for h in has_set:
                    if h == 'image' and not _is_media_kind(atts, 'image'):
                        filter_pass = False; break
                    if h == 'video' and not _is_media_kind(atts, 'video'):
                        filter_pass = False; break
                    if h == 'audio' and not _is_media_kind(atts, 'audio'):
                        filter_pass = False; break
                    if h == 'link' and not ('http://' in content or 'https://' in content):
                        filter_pass = False; break
                    if h == 'embed' and len(embs) == 0:
                        filter_pass = False; break
                    if h == 'file' and len(atts) == 0:
                        filter_pass = False; break
                if not filter_pass:
                    continue

            # --- after / before filter ---
            created = message.get('created_at')
            if created:
                try:
                    text = str(created).replace('Z', '+00:00')
                    dt = datetime.fromisoformat(text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < after_dt or dt > before_dt:
                        continue
                except (ValueError, TypeError):
                    pass  # If we can't parse, include the message

            # --- is_reply filter ---
            if is_reply is not None:
                has_reply = bool(message.get('reply_to_message_id') or message.get('reference_id'))
                if is_reply and not has_reply:
                    continue
                if not is_reply and has_reply:
                    continue

            # --- build compact row ---
            author = message.get('author_context_snapshot') or message.get('author') or {}
            atts = self._normalize_attachment_list(message.get('attachments'))
            embs = self._normalize_attachment_list(message.get('embeds'))

            row = {
                'message_id': str(message.get('message_id')),
                'channel_id': str(message.get('channel_id')),
                'channel_name': message.get('channel_name'),
                'author_id': str(message.get('author_id')),
                'author_name': (
                    message.get('author_name')
                    or author.get('server_nick')
                    or author.get('global_name')
                    or author.get('display_name')
                    or author.get('username')
                ),
                'content_preview': self._cap_text(content, 200),
                'created_at': message.get('created_at'),
                'reaction_count': self._message_reaction_count(message),
                'reply_to_message_id': message.get('reply_to_message_id') or message.get('reference_id'),
                'has_attachments': len(atts) > 0,
                'has_links': bool(re.search(r'https?://', content)),
                'has_image': _is_media_kind(atts, 'image'),
                'has_video': _is_media_kind(atts, 'video'),
                'has_audio': _is_media_kind(atts, 'audio'),
                'has_embed': len(embs) > 0,
            }
            rows.append(row)
            if len(rows) >= safe_limit:
                break

        # 2KB JSON cap
        while rows:
            payload = json.dumps(rows, default=str)
            if len(payload.encode('utf-8')) <= 2048:
                break
            rows.pop()
        return rows

    def _message_is_since(self, created_at: Any, since: datetime) -> bool:
        if not created_at:
            return True
        try:
            text = str(created_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= since
        except Exception:
            return True

    def _dispatch_finalize_run(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call.get("input") or {}
        reasoning = (args.get("overall_reasoning") or "").strip()
        topics_considered = args.get("topics_considered") or []
        if len(reasoning) < 100:
            # Reject — but DO NOT mark finalized. The agent must try again with longer reasoning.
            self._store_transition({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "rejected_finalize_run",
                "reason": "overall_reasoning_too_short",
                "payload": shape_transition_payload(
                    outcome="tool_error",
                    tool_name="finalize_run",
                    error=f"overall_reasoning must be >=100 chars; got {len(reasoning)}",
                ),
                "model": self.model,
            })
            return {
                "tool_call_id": call["id"],
                "tool": "finalize_run",
                "outcome": "rejected_too_short",
                "error": f"overall_reasoning must be >=100 chars; got {len(reasoning)}",
            }
        # Accept — capture into context for the run loop to detect + into transitions for audit.
        context["finalize"] = {
            "overall_reasoning": reasoning,
            "topics_considered": list(topics_considered),
        }
        self._store_transition({
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "action": "finalize_run",
            "reason": reasoning[:500],
            "payload": shape_transition_payload(
                outcome="accepted",
                tool_name="finalize_run",
                extra={
                    "overall_reasoning": reasoning,
                    "topics_considered": list(topics_considered),
                },
            ),
            "model": self.model,
        })
        return {
            "tool_call_id": call["id"],
            "tool": "finalize_run",
            "outcome": "accepted",
            "action": "finalize_run",
        }

    def _dispatch_create_topic_tool(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call["input"]

        # Normalize blocks before source_id derivation (T6: structured blocks path)
        normalized_blocks: List[Dict[str, Any]] = []
        if args.get("blocks"):
            try:
                normalized_blocks = normalize_document_blocks(
                    {"blocks": args["blocks"]},
                    topic_source_message_ids=None,
                )
            except (ValueError, TypeError):
                normalized_blocks = []

        # Derive topic-level source_ids: args source_message_ids + union from blocks
        source_ids = self._unique_ids(args.get("source_message_ids") or [])
        if normalized_blocks:
            block_sids = collect_document_source_ids(normalized_blocks)
            for sid in block_sids:
                if sid not in source_ids:
                    source_ids.append(sid)

        # Pre‑compute canonical_key early — T7 validation rejections need it
        canonical_key = canonicalize_proposed_key(args.get("proposed_key"), args.get("headline") or "")

        # ── T7: Build merged resolved-message map (window + archive) ──────
        resolved_by_id: Dict[str, Dict[str, Any]] = {}
        for msg in context.get("messages") or []:
            mid = str(msg.get("message_id"))
            if mid and mid not in resolved_by_id:
                resolved_by_id[mid] = msg

        # Fill gaps from archive resolver (get_topic_editor_source_messages,
        # limit=50 – separate from the 10‑message read-tool cap).
        missing_ids = [sid for sid in source_ids if sid not in resolved_by_id]
        if missing_ids:
            try:
                archive_rows = self.db.get_topic_editor_source_messages(
                    message_ids=missing_ids,
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                )
            except Exception:
                archive_rows = []
            for row in (archive_rows or []):
                mid = str(row.get("message_id"))
                if mid and mid not in resolved_by_id:
                    resolved_by_id[mid] = row

        # ── T7: Validate block-level source_message_ids ────────────────────
        if normalized_blocks:
            for block in normalized_blocks:
                block_sids = block_source_ids(block)
                for sid in block_sids:
                    if sid not in resolved_by_id:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="unresolved_block_source_message",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "unresolved_message_id": sid,
                                "block_type": block.get("type"),
                                "block_title": block.get("title"),
                            },
                        )

                # ── T7: Validate block-level media_refs ────────────────────
                for ref in block_media_refs(block):
                    ref_mid = ref["message_id"]
                    ref_msg = resolved_by_id.get(ref_mid)
                    if not ref_msg:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="unresolved_media_ref",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "media_ref": ref,
                                "block_type": block.get("type"),
                            },
                        )
                    if ref_mid not in block_sids:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="invalid_media_ref",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "media_ref": ref,
                                "block_type": block.get("type"),
                                "error": (
                                    f"media_ref message_id {ref_mid!r} is not "
                                    "in block source_message_ids"
                                ),
                            },
                        )

                    kind = ref["kind"]
                    idx = ref["index"]
                    if kind == "attachment":
                        atts = self._normalize_attachment_list(
                            ref_msg.get("attachments")
                        )
                        if idx < 0 or idx >= len(atts):
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"attachment index {idx} out of range "
                                        f"(message has {len(atts)} attachments)"
                                    ),
                                },
                            )
                        url = atts[idx].get("url") or atts[idx].get("proxy_url")
                        if not url:
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"attachment at index {idx} has "
                                        f"no url / proxy_url"
                                    ),
                                },
                            )
                    elif kind == "embed":
                        embs = self._normalize_attachment_list(
                            ref_msg.get("embeds")
                        )
                        if idx < 0 or idx >= len(embs):
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"embed index {idx} out of range "
                                        f"(message has {len(embs)} embeds)"
                                    ),
                                },
                            )
                        # Discord embeds may have url at top level or
                        # inside a thumbnail / image sub-object.
                        url = (
                            embs[idx].get("url")
                            or (embs[idx].get("thumbnail") or {}).get("url")
                            or (embs[idx].get("image") or {}).get("url")
                        )
                        if not url:
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"embed at index {idx} has no url"
                                    ),
                                },
                            )
                    elif kind == "external":
                        # Validate external ref: source message must exist and
                        # extract_external_urls(ref_msg)[index] must exist and
                        # be source-domain safelisted. No resolver call here.
                        external_urls = extract_external_urls(ref_msg)
                        if idx < 0 or idx >= len(external_urls):
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"external index {idx} out of range "
                                        f"(message has {len(external_urls)} "
                                        f"external URLs)"
                                    ),
                                },
                            )
                        ext_entry = external_urls[idx]
                        if ext_entry.get("platform_policy") == "unknown":
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"external ref domain "
                                        f"\"{ext_entry.get('domain')}\" "
                                        f"not safelisted"
                                    ),
                                },
                            )
                        # No URL resolution here — external refs are resolved
                        # lazily at publish time.
                    else:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="invalid_media_ref",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "media_ref": ref,
                                "block_type": block.get("type"),
                                "error": f"unknown media_ref kind: {kind!r}",
                            },
                        )

        # ── Rebuild source_messages / source_authors from merged set ─────
        source_messages = [
            resolved_by_id[sid] for sid in source_ids if sid in resolved_by_id
        ]
        source_authors = self._source_authors(source_messages)
        if normalized_blocks:
            normalized_blocks = self._attach_default_media_refs_to_blocks(
                normalized_blocks,
                resolved_by_id,
            )
            if call["name"] == "post_sectioned_topic":
                args["blocks"] = normalized_blocks

        action_by_tool = {
            "post_simple_topic": "post_simple",
            "post_sectioned_topic": "post_sectioned",
            "watch_topic": "watch",
        }
        state = "watching" if call["name"] == "watch_topic" else "posted"
        simple_media_items = args.get("media") or []
        simple_body = str(args.get("body") or "")
        if (
            call["name"] == "post_simple_topic"
            and (
                bool(simple_media_items)
                or re.search(r"\b\d{15,25}:(?:attachment|embed|external):\d+\b", simple_body)
            )
        ):
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_simple",
                reason="post_simple_cannot_attach_media_use_post_sectioned_topic",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
                extra={
                    "media_count": len(simple_media_items),
                    "has_raw_media_ref": bool(
                        re.search(r"\b\d{15,25}:(?:attachment|embed|external):\d+\b", simple_body)
                    ),
                },
            )
        if call["name"] == "post_simple_topic" and (len(source_ids) >= 3 or len(set(source_authors)) >= 2):
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_simple",
                reason="post_simple_requires_single_author_and_one_or_two_sources",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
                extra={"distinct_author_count": len(set(source_authors)), "source_count": len(source_ids)},
            )

        # T6: Relax guard — allow blocks-only calls, reject only when BOTH
        # sections and blocks are missing or normalize to zero publishable blocks.
        sections = args.get("sections") or []
        has_sections = bool(sections and any(isinstance(s, dict) for s in sections))
        has_blocks = bool(normalized_blocks)
        if call["name"] == "post_sectioned_topic" and not has_sections and not has_blocks:
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_sectioned",
                reason="post_sectioned_requires_sections_or_blocks",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
            )

        collisions = detect_topic_collisions(
            proposed_canonical_key=canonical_key,
            headline=args.get("headline") or "",
            source_authors=source_authors,
            existing_topics=self._topics_with_aliases(context["active_topics"], context.get("aliases") or []),
        )
        unresolved = unresolved_collisions(collisions, args.get("override_collisions") or [])
        if unresolved:
            rejected_action = {
                "post_simple_topic": "rejected_post_simple",
                "post_sectioned_topic": "rejected_post_sectioned",
                "watch_topic": "rejected_watch",
            }[call["name"]]
            return self._reject_create_tool(
                call,
                context,
                action=rejected_action,
                reason="topic_collision",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
                collisions=unresolved,
            )

        revisit_at = None
        if state == "watching":
            revisit_at = parse_optional_datetime(args.get("revisit_when"))

        topic = self.db.upsert_topic({
            "guild_id": context["guild_id"],
            "canonical_key": canonical_key,
            "display_slug": args.get("proposed_key"),
            "state": state,
            "headline": args.get("headline"),
            "summary": self._summary_for_tool(call["name"], args),
            "source_authors": source_authors,
            "parent_topic_id": args.get("parent_topic_id"),
            "publication_status": "pending" if state == "posted" else None,
            "revisit_at": revisit_at,
            "source_message_ids": source_ids,
        }, environment=self.environment)
        topic_id = topic.get("topic_id") if topic else None
        if topic_id:
            topic.setdefault("source_message_ids", source_ids)
            if state == "posted":
                context.setdefault("created_topics", []).append(topic)
            for message_id in source_ids:
                self.db.add_topic_source({
                    "topic_id": topic_id,
                    "message_id": message_id,
                    "guild_id": context["guild_id"],
                    "run_id": context["run_id"],
                }, environment=self.environment)
            self.db.upsert_topic_alias({
                "topic_id": topic_id,
                "alias_key": args.get("proposed_key") or canonical_key,
                "alias_kind": "proposed",
                "guild_id": context["guild_id"],
            }, environment=self.environment)
        self._store_transition({
            "topic_id": topic_id,
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "to_state": state,
            "action": action_by_tool[call["name"]],
            "reason": args.get("notes") or args.get("why_interesting"),
            "payload": shape_transition_payload(
                outcome="accepted",
                tool_name=call["name"],
                canonical_key=canonical_key,
                proposed_key=args.get("proposed_key"),
                source_message_ids=source_ids,
                extra={
                    "blocks": normalized_blocks or None,
                } if normalized_blocks else None,
            ),
            "model": self.model,
        })
        override_rows = build_override_transitions(
            run_id=context["run_id"],
            environment=self.environment,
            guild_id=context["guild_id"],
            topic_id=str(topic_id),
            override_collisions=args.get("override_collisions") or [],
            tool_call_id=call["id"],
            model=self.model,
        )
        for row in override_rows:
            self._store_transition(row)
        tool_call_id = call.get("id")
        if tool_call_id:
            key = (str(context.get("run_id")), str(tool_call_id))
            context.setdefault("accepted_tool_call_ids", set()).add(key)
            if args.get("override_collisions"):
                context.setdefault("override_retry_consumed_tool_call_ids", set()).add(key)
        return {
            "tool_call_id": call["id"],
            "tool": call["name"],
            "outcome": "accepted",
            "topic_id": topic_id,
            "action": action_by_tool[call["name"]],
            "override_count": len(override_rows),
        }

    def _attach_default_media_refs_to_blocks(
        self,
        blocks: Sequence[Dict[str, Any]],
        resolved_by_id: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Add one source-local media ref per block when the agent omitted it.

        The fallback is deliberately conservative: it only uses media from the
        block's own cited sources, preserves explicit refs, and adds at most one
        media item per block.
        """
        hydrated: List[Dict[str, Any]] = []
        for block in blocks:
            next_block = dict(block)
            if not block_media_refs(next_block):
                default_ref = self._first_available_media_ref_for_sources(
                    block_source_ids(next_block),
                    resolved_by_id,
                )
                if default_ref:
                    next_block["media_refs"] = [default_ref]
            hydrated.append(next_block)
        return hydrated

    def _first_available_media_ref_for_sources(
        self,
        source_ids: Sequence[str],
        resolved_by_id: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for sid in source_ids:
            message = resolved_by_id.get(str(sid)) or {}
            attachments = self._normalize_attachment_list(message.get("attachments"))
            for idx, attachment in enumerate(attachments):
                if isinstance(attachment, dict) and (attachment.get("url") or attachment.get("proxy_url")):
                    return {"message_id": str(sid), "kind": "attachment", "index": idx}
            embeds = self._normalize_attachment_list(message.get("embeds"))
            for idx, embed in enumerate(embeds):
                if not isinstance(embed, dict):
                    continue
                url = (
                    embed.get("url")
                    or (embed.get("thumbnail") or {}).get("url")
                    or (embed.get("thumbnail") or {}).get("proxy_url")
                    or (embed.get("image") or {}).get("url")
                    or (embed.get("image") or {}).get("proxy_url")
                    or (embed.get("video") or {}).get("url")
                    or (embed.get("video") or {}).get("proxy_url")
                )
                if url:
                    return {"message_id": str(sid), "kind": "embed", "index": idx}
        return None

    def _auto_shortlist_media_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        known_topics: Sequence[Dict[str, Any]],
        *,
        run_id: str,
        guild_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Create watching topics for reaction-qualified media posts.

        This replaces the old direct top-creations auto-post path. The shortlist
        is intentionally conservative and idempotent: one topic per source
        message, skipped forever once the operator/agent discards it.
        """
        threshold = max(0, int(self.media_shortlist_min_reactions or 0))
        limit = max(0, int(self.media_shortlist_limit or 0))
        if threshold <= 0 or limit <= 0:
            return []

        existing_by_key = {
            str(topic.get("canonical_key") or ""): topic
            for topic in known_topics or []
            if topic.get("canonical_key")
        }

        candidates: List[Dict[str, Any]] = []
        for message in messages or []:
            message_id = str(message.get("message_id") or "").strip()
            if not message_id:
                continue
            canonical_key = self._media_shortlist_key(message_id)
            existing = existing_by_key.get(canonical_key)
            if existing:
                # Posted/watching/discarded all mean this message was already
                # intentionally handled. Discarded is the explicit ignore path.
                continue
            channel_name = str(message.get("channel_name") or "").lower()
            if "nsfw" in channel_name:
                continue
            reaction_count = self._message_reaction_count(message)
            if reaction_count < threshold:
                continue
            media_ref = self._first_available_media_ref_for_sources(
                [message_id],
                {message_id: message},
            )
            if not media_ref:
                continue
            candidates.append({
                "message": message,
                "message_id": message_id,
                "canonical_key": canonical_key,
                "reaction_count": reaction_count,
                "media_ref": media_ref,
            })

        candidates.sort(
            key=lambda item: (
                int(item.get("reaction_count") or 0),
                str((item.get("message") or {}).get("created_at") or ""),
                str(item.get("message_id") or ""),
            ),
            reverse=True,
        )

        shortlisted: List[Dict[str, Any]] = []
        for item in candidates[:limit]:
            message = item["message"]
            message_id = item["message_id"]
            author = self._author_name(message) or "community member"
            reaction_count = int(item["reaction_count"] or 0)
            headline = f"Shortlisted media from {author} ({reaction_count} reactions)"
            reason = (
                f"Auto-shortlisted because source message {message_id} has media "
                f"and {reaction_count} reactions. Investigate context and media "
                "understanding before deciding whether to publish, keep watching, or discard."
            )
            revisit_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
            topic = self.db.upsert_topic({
                "guild_id": guild_id,
                "canonical_key": item["canonical_key"],
                "display_slug": item["canonical_key"],
                "state": "watching",
                "headline": headline,
                "summary": {
                    "why_interesting": reason,
                    "auto_shortlist": True,
                    "shortlist_kind": "media_reaction_threshold",
                    "reaction_count": reaction_count,
                    "source_message_id": message_id,
                    "media_refs": [item["media_ref"]],
                    "suggested_actions": [
                        "get_message_context",
                        "get_reply_chain when it is a reply",
                        "search_messages for related posts by the author/tool",
                        "understand_image or understand_video",
                        "post_sectioned_topic, watch_topic/update sources, or discard_topic",
                    ],
                },
                "source_authors": [author] if author else [],
                "parent_topic_id": None,
                "publication_status": None,
                "revisit_at": revisit_at,
                "source_message_ids": [message_id],
            }, environment=self.environment)
            topic_id = topic.get("topic_id") if topic else None
            if topic_id:
                topic.setdefault("source_message_ids", [message_id])
                self.db.add_topic_source({
                    "topic_id": topic_id,
                    "message_id": message_id,
                    "guild_id": guild_id,
                    "run_id": run_id,
                }, environment=self.environment)
                self.db.upsert_topic_alias({
                    "topic_id": topic_id,
                    "alias_key": item["canonical_key"],
                    "alias_kind": "proposed",
                    "guild_id": guild_id,
                }, environment=self.environment)
            self._store_transition({
                "topic_id": topic_id,
                "run_id": run_id,
                "guild_id": guild_id,
                "tool_call_id": f"auto-media-shortlist:{message_id}",
                "to_state": "watching",
                "action": "watch",
                "reason": reason,
                "payload": shape_transition_payload(
                    outcome="accepted",
                    tool_name="auto_media_shortlist",
                    canonical_key=item["canonical_key"],
                    proposed_key=item["canonical_key"],
                    source_message_ids=[message_id],
                    extra={
                        "reaction_count": reaction_count,
                        "media_ref": item["media_ref"],
                    },
                ),
                "model": self.model,
            })
            shortlisted.append({
                "status": "added",
                "topic": topic,
                "topic_id": topic_id,
                "message_id": message_id,
                "reaction_count": reaction_count,
                "headline": headline,
                "reason": reason,
                "media_ref": item["media_ref"],
            })

        if shortlisted:
            logger.info(
                "TopicEditor auto-shortlisted %s media item(s) at >=%s reactions: %s",
                len(shortlisted),
                threshold,
                [
                    {
                        "message_id": item.get("message_id"),
                        "reaction_count": item.get("reaction_count"),
                        "topic_id": item.get("topic_id"),
                    }
                    for item in shortlisted
                ],
            )
        return shortlisted

    @staticmethod
    def _media_shortlist_key(message_id: str) -> str:
        return f"media-shortlist-{_slugify(str(message_id))}"

    @staticmethod
    def _message_reaction_count(message: Dict[str, Any]) -> int:
        for key in ("reaction_count", "unique_reactor_count", "reactions"):
            value = message.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                return int(value)
            try:
                return int(str(value))
            except (TypeError, ValueError):
                pass
        reactors = message.get("reactors") or []
        if isinstance(reactors, str):
            try:
                reactors = json.loads(reactors)
            except json.JSONDecodeError:
                reactors = []
        if isinstance(reactors, list):
            return len(reactors)
        return 0

    def _dispatch_update_sources(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call["input"]
        topic = self._find_topic(context["active_topics"], args.get("topic_id"))
        source_ids = self._unique_ids(args.get("new_source_message_ids") or [])
        if not topic or not source_ids:
            error = "topic_not_found" if not topic else "no_source_messages"
            self._store_transition({
                "topic_id": args.get("topic_id"),
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "update_sources",
                "reason": args.get("note") or error,
                "payload": shape_transition_payload(
                    outcome="tool_error",
                    tool_name=call["name"],
                    source_message_ids=source_ids,
                    error=error,
                ),
                "model": self.model,
            })
            return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "tool_error", "action": "update_sources", "error": error}
        for message_id in source_ids:
            self.db.add_topic_source({
                "topic_id": args.get("topic_id"),
                "message_id": message_id,
                "guild_id": context["guild_id"],
                "run_id": context["run_id"],
            }, environment=self.environment)
        self._store_transition({
            "topic_id": args.get("topic_id"),
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "action": "update_sources",
            "reason": args.get("note"),
            "payload": shape_transition_payload(outcome="accepted", tool_name=call["name"], source_message_ids=source_ids),
            "model": self.model,
        })
        return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "accepted", "action": "update_sources"}

    def _dispatch_discard(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call["input"]
        topic = self._find_topic(context["active_topics"], args.get("topic_id"))
        if not topic or topic.get("state") != "watching":
            error = "topic_not_found" if not topic else "topic_not_watching"
            self._store_transition({
                "topic_id": args.get("topic_id"),
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "discard",
                "reason": args.get("reason") or error,
                "payload": shape_transition_payload(outcome="tool_error", tool_name=call["name"], error=error),
                "model": self.model,
            })
            return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "tool_error", "action": "discard", "error": error}
        self.db.update_topic(args.get("topic_id"), {"state": "discarded", "guild_id": context["guild_id"]}, guild_id=context["guild_id"], environment=self.environment)
        self._store_transition({
            "topic_id": args.get("topic_id"),
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "to_state": "discarded",
            "action": "discard",
            "reason": args.get("reason"),
            "payload": shape_transition_payload(outcome="accepted", tool_name=call["name"]),
            "model": self.model,
        })
        return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "accepted", "action": "discard"}

    def _reject_create_tool(
        self,
        call: Dict[str, Any],
        context: Dict[str, Any],
        *,
        action: str,
        reason: str,
        canonical_key: str,
        source_message_ids: Sequence[str],
        collisions: Optional[Sequence[Collision]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        args = call["input"]
        if reason == "topic_collision" and call.get("id"):
            context.setdefault("collision_rejected_tool_call_ids", set()).add(
                (str(context.get("run_id")), str(call["id"]))
            )
        self._store_transition(build_rejected_transition(
            run_id=context["run_id"],
            environment=self.environment,
            guild_id=context["guild_id"],
            action=action,
            tool_call_id=call["id"],
            reason=reason,
            payload=shape_transition_payload(
                outcome="tool_error",
                tool_name=call["name"],
                canonical_key=canonical_key,
                proposed_key=args.get("proposed_key"),
                source_message_ids=source_message_ids,
                collisions=collisions,
                error=reason,
                extra=extra,
            ),
            model=self.model,
        ))
        return {"tool_call_id": call["id"], "tool": call["name"], "outcome": action, "action": action, "error": reason}

    def _store_transition(self, transition: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        transition = dict(transition)
        if transition.get("action") in {"finalize_run", "rejected_finalize_run"}:
            payload = transition.get("payload") if isinstance(transition.get("payload"), dict) else {}
            payload = {**payload, "original_action": transition.get("action")}
            transition["payload"] = payload
            transition["action"] = "observation"
        try:
            return self.db.store_topic_transition(transition, environment=self.environment)
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                return None
            raise

    def _populate_idempotent_results(self, calls: Sequence[Dict[str, Any]], context: Dict[str, Any]) -> None:
        tool_call_ids = [
            str(call.get("id"))
            for call in calls or []
            if call.get("name") in WRITE_TOOL_NAMES and call.get("id")
        ]
        if not tool_call_ids:
            return
        getter = getattr(self.db, "get_topic_transitions_by_tool_call_ids", None)
        if not getter:
            return
        existing = getter(context.get("run_id"), tool_call_ids, environment=self.environment) or {}
        cache = context.setdefault("idempotent_results", {})
        for tool_call_id, row in existing.items():
            if row:
                cache[str(tool_call_id)] = row

    def _idempotent_replay_outcome(self, call: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tool_call_id = call.get("id")
        if not tool_call_id or call.get("name") not in WRITE_TOOL_NAMES:
            return None
        row = (context.get("idempotent_results") or {}).get(str(tool_call_id))
        if not row:
            return None
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        action = row.get("action")
        if (
            payload.get("tool_name") == "finalize_run"
            and payload.get("outcome") == "accepted"
        ):
            context["finalize"] = {
                "overall_reasoning": payload.get("overall_reasoning") or row.get("reason") or "",
                "topics_considered": list(payload.get("topics_considered") or []),
            }
        return {
            "tool_call_id": str(tool_call_id),
            "tool": call.get("name"),
            "outcome": "idempotent_replay",
            "action": payload.get("original_action") or action,
            "topic_id": payload.get("topic_id") or row.get("topic_id"),
        }

    def _is_idempotent_replay(self, call: Dict[str, Any], context: Dict[str, Any]) -> bool:
        tool_call_id = call.get("id")
        if not tool_call_id:
            return False
        key = (str(context.get("run_id")), str(tool_call_id))
        seen = context.setdefault("seen_tool_call_ids", set())
        if key in seen:
            if self._is_collision_override_retry(call, context, key):
                return False
            return True
        seen.add(key)
        return False

    def _is_collision_override_retry(
        self,
        call: Dict[str, Any],
        context: Dict[str, Any],
        key: tuple[str, str],
    ) -> bool:
        if call.get("name") not in {"post_simple_topic", "post_sectioned_topic", "watch_topic"}:
            return False
        if not (call.get("input") or {}).get("override_collisions"):
            return False
        if key not in context.get("collision_rejected_tool_call_ids", set()):
            return False
        if key in context.get("accepted_tool_call_ids", set()):
            return False
        if key in context.get("override_retry_consumed_tool_call_ids", set()):
            return False
        return True

    def _topics_with_aliases(
        self,
        topics: Sequence[Dict[str, Any]],
        aliases: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        aliases_by_topic: Dict[str, List[str]] = {}
        for alias in aliases or []:
            if self.environment is not None and alias.get("environment") not in {None, self.environment}:
                continue
            topic_id = alias.get("topic_id")
            alias_key = alias.get("alias_key")
            if topic_id and alias_key:
                aliases_by_topic.setdefault(str(topic_id), []).append(str(alias_key))
        enriched = []
        for topic in topics or []:
            row = dict(topic)
            topic_id = str(row.get("topic_id"))
            merged_aliases = list(row.get("aliases") or [])
            for alias_key in aliases_by_topic.get(topic_id, []):
                if alias_key not in merged_aliases:
                    merged_aliases.append(alias_key)
            row["aliases"] = merged_aliases
            enriched.append(row)
        return enriched

    def _find_topic(self, topics: Sequence[Dict[str, Any]], topic_id: Any) -> Optional[Dict[str, Any]]:
        if topic_id is None:
            return None
        wanted = str(topic_id)
        return next((topic for topic in topics or [] if str(topic.get("topic_id")) == wanted), None)

    def _unique_ids(self, ids: Sequence[Any]) -> List[str]:
        unique: List[str] = []
        for item in ids or []:
            value = str(item)
            if value and value not in unique:
                unique.append(value)
        return unique

    def _cap_text(self, value: Any, limit: int) -> str:
        text = str(value or "").strip()
        return text[:limit]

    def _run_updates(
        self,
        *,
        checkpoint_before: Dict[str, Any],
        checkpoint_after: Dict[str, Any],
        messages: Sequence[Dict[str, Any]],
        tool_calls: Sequence[Dict[str, Any]],
        started: float,
        metadata: Dict[str, Any],
        accepted_count: int = 0,
        rejected_count: int = 0,
        override_count: int = 0,
        observation_count: int = 0,
        published_count: int = 0,
        failed_publish_count: int = 0,
        skipped_reason: Optional[str] = None,
        status: str = "completed",
    ) -> Dict[str, Any]:
        usage = metadata.get("usage") or {}
        metadata_cost = metadata.get("cumulative_cost_usd")
        return {
            "status": status,
            "guild_id": self._resolve_guild_id(),
            "checkpoint_before": checkpoint_before,
            "checkpoint_after": checkpoint_after,
            "source_message_count": len(messages),
            "tool_call_count": len(tool_calls),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "override_count": override_count,
            "observation_count": observation_count,
            "published_count": published_count,
            "failed_publish_count": failed_publish_count,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cost_usd": metadata_cost if isinstance(metadata_cost, (int, float)) else self._estimate_cost_usd(usage),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "model": self.model,
            "publishing_enabled": self.publishing_enabled,
            "trace_channel_id": self.trace_channel_id,
            "skipped_reason": skipped_reason,
            "metadata": metadata,
        }

    async def _publish_created_topics(self, topics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        publishable = [
            topic for topic in topics or []
            if topic.get("topic_id") and topic.get("state") == "posted"
        ]
        results: List[Dict[str, Any]] = []
        for topic in publishable:
            results.append(await self._publish_topic(topic))
        return results

    def _format_trace_messages(
        self,
        run_id: str,
        updates: Dict[str, Any],
        outcomes: Sequence[Dict[str, Any]],
        publish_results: Sequence[Dict[str, Any]],
    ) -> List[str]:
        usage = {
            "input_tokens": updates.get("input_tokens", 0),
            "output_tokens": updates.get("output_tokens", 0),
            "cost_usd": updates.get("cost_usd"),
            "model": updates.get("model"),
        }
        outcome_counts: Dict[str, int] = {}
        tool_lines: List[str] = []
        rejection_lines: List[str] = []
        override_lines: List[str] = []
        observation_lines: List[str] = []
        for outcome in outcomes or []:
            outcome_name = str(outcome.get("outcome") or "unknown")
            outcome_counts[outcome_name] = outcome_counts.get(outcome_name, 0) + 1
            tool = outcome.get("tool")
            action = outcome.get("action")
            suffix = f" -> {action}" if action and action != tool else ""
            tool_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome_name}{suffix}")
            if outcome_name.startswith("rejected") or outcome_name == "tool_error":
                rejection_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome.get('error') or outcome_name}")
            if int(outcome.get("override_count") or 0):
                override_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome.get('override_count')} override(s)")
            if action == "observation":
                observation_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome_name}")

        publish_lines: List[str] = []
        for result in publish_results or []:
            status = result.get("status")
            topic_id = result.get("topic_id")
            media_counts = result.get("source_media_counts") or {}
            publish_lines.append(
                f"- `{topic_id}`: {status} "
                f"media_sent={result.get('media_count', 0)} "
                f"flat={result.get('flat_message_count', len(result.get('discord_message_ids') or []))} "
                f"source_resolvable_media={media_counts.get('resolvable_media', 0)}"
            )
            for message in result.get("messages") or []:
                publish_lines.append(_indent_trace_block(f"would-publish: {message}"))

        lines = [
            f"**Topic editor trace** `{run_id}`",
            f"env={self.environment} publishing={'ON' if self.publishing_enabled else 'OFF'} trigger-state={updates.get('status') or 'completed'}",
            f"sources={updates.get('source_message_count', 0)} tools={updates.get('tool_call_count', 0)} accepted={updates.get('accepted_count', 0)} rejected={updates.get('rejected_count', 0)} overrides={updates.get('override_count', 0)} observations={updates.get('observation_count', 0)}",
            f"published={updates.get('published_count', 0)} failed_publish={updates.get('failed_publish_count', 0)} latency_ms={updates.get('latency_ms', 0)}",
            f"tokens in/out={usage['input_tokens']}/{usage['output_tokens']} cost_usd={usage['cost_usd']} model={usage['model']}",
            f"outcomes={outcome_counts}",
        ]
        metadata = updates.get("metadata") or {}
        if metadata.get("forced_close"):
            lines.insert(1, f"⚠ FORCE-CLOSED reason={metadata.get('forced_close_reason') or 'unknown'}")
        if metadata.get("cumulative_tokens") is not None:
            lines.insert(
                5,
                f"cumulative_tokens={metadata.get('cumulative_tokens')} cumulative_cost_usd={metadata.get('cumulative_cost_usd')}",
            )
        shortlist_lines = [
            (
                f"- `{item.get('message_id')}` -> `{item.get('topic_id')}` "
                f"reactions={item.get('reaction_count')} status={item.get('status')}"
            )
            for item in metadata.get("auto_shortlisted_media") or []
        ]
        sections = [
            ("Auto-shortlisted media", shortlist_lines),
            ("Tool calls", tool_lines),
            ("Rejections", rejection_lines),
            ("Overrides", override_lines),
            ("Observations", observation_lines),
            ("Publishing", publish_lines),
        ]
        for title, section_lines in sections:
            if section_lines:
                lines.extend(["", f"**{title}**", *section_lines])
        return _chunk_trace_lines(lines)

    async def _emit_trace(
        self,
        messages: Sequence[str],
        *,
        run_id: str | None = None,
        updates: Dict[str, Any] | None = None,
        outcomes: Sequence[Dict[str, Any]] | None = None,
        publish_results: Sequence[Dict[str, Any]] | None = None,
    ) -> None:
        if not self.trace_channel_id:
            return
        channel = await self._resolve_discord_channel(int(self.trace_channel_id))
        if channel is None:
            return
        embed = self._build_trace_embed(run_id, updates or {}, outcomes or [], publish_results or [])
        if embed is not None:
            try:
                await channel.send(embed=embed)
            except Exception:
                # Fallback to plain-text if embed send fails (e.g. perms missing for embeds).
                for message in messages or []:
                    await channel.send(message)
            return
        for message in messages or []:
            await channel.send(message)

    def _build_trace_embed(
        self,
        run_id: str | None,
        updates: Dict[str, Any],
        outcomes: Sequence[Dict[str, Any]],
        publish_results: Sequence[Dict[str, Any]],
    ) -> Optional[discord.Embed]:
        if run_id is None and not updates:
            return None

        status = updates.get("status") or "completed"
        published_count = updates.get("published_count", 0) or 0
        failed_publish = updates.get("failed_publish_count", 0) or 0
        rejected_count = updates.get("rejected_count", 0) or 0
        accepted_count = updates.get("accepted_count", 0) or 0
        skipped_reason = updates.get("skipped_reason")
        metadata = updates.get("metadata") or {}

        # Pick a color based on what happened.
        if status == "failed" or failed_publish:
            color = 0xE74C3C  # red
        elif published_count:
            color = 0x2ECC71  # green
        elif rejected_count or skipped_reason:
            color = 0xF1C40F  # amber
        elif accepted_count:
            color = 0x3498DB  # blue
        else:
            color = 0x808080  # grey (nothing-to-post idle)

        publishing_label = "ON" if self.publishing_enabled else "OFF"
        title = f"Topic editor · {self.environment} · publishing {publishing_label}"
        description_parts = [f"run `{run_id or 'unknown'}`"]
        if metadata.get("forced_close"):
            description_parts.append(f"⚠ FORCE-CLOSED: `{metadata.get('forced_close_reason') or 'unknown'}`")
        if skipped_reason:
            description_parts.append(f"skipped: `{skipped_reason}`")
        description = " · ".join(description_parts)

        embed = discord.Embed(title=title, description=description, color=color)

        # --- field: editorial reasoning (the agent's overall narrative) ---
        reasoning = metadata.get("reasoning")
        if reasoning:
            reasoning_text = reasoning if len(reasoning) <= 1024 else reasoning[:1000] + "…"
            embed.add_field(name="editorial reasoning", value=reasoning_text, inline=False)
        else:
            embed.add_field(name="editorial reasoning", value="_(agent did not provide reasoning)_", inline=False)

        # --- field: summary ---
        summary_lines = [
            f"sources: `{updates.get('source_message_count', 0)}`",
            f"tool calls: `{updates.get('tool_call_count', 0)}`",
            f"accepted: `{accepted_count}` · rejected: `{rejected_count}`",
            f"overrides: `{updates.get('override_count', 0)}` · observations: `{updates.get('observation_count', 0)}`",
            f"published: `{published_count}` · failed_publish: `{failed_publish}`",
            f"auto-shortlisted media: `{len(metadata.get('auto_shortlisted_media') or [])}`",
        ]
        embed.add_field(name="summary", value="\n".join(summary_lines)[:1024], inline=True)

        # --- field: model & cost ---
        cost = updates.get("cost_usd")
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
        model_lines = [
            f"model: `{updates.get('model') or 'n/a'}`",
            f"tokens in/out: `{updates.get('input_tokens', 0)}` / `{updates.get('output_tokens', 0)}`",
            f"cumulative tokens: `{metadata.get('cumulative_tokens', updates.get('input_tokens', 0) + updates.get('output_tokens', 0))}`",
            f"cost: `{cost_str}`",
            f"cumulative cost: `{self._format_cost(metadata.get('cumulative_cost_usd'))}`",
            f"latency: `{updates.get('latency_ms', 0)} ms`",
        ]
        embed.add_field(name="model & cost", value="\n".join(model_lines)[:1024], inline=True)

        # --- field: input context (time range + channel coverage) ---
        input_lines: List[str] = []
        time_range = self._format_metadata_time_range(metadata)
        if time_range:
            input_lines.append(f"window: {time_range}")
        channel_summary = self._format_metadata_channel_coverage(metadata)
        if channel_summary:
            input_lines.append(f"channels: {channel_summary}")
        active_topics_count = metadata.get("active_topics_count")
        if active_topics_count is not None:
            input_lines.append(f"active topics: `{active_topics_count}`")
        if input_lines:
            embed.add_field(name="input context", value="\n".join(input_lines)[:1024], inline=False)

        # --- field: tool calls with input snippets ---
        if outcomes:
            tool_lines: List[str] = []
            input_by_id = {call.get("id"): call.get("input") or {} for call in (metadata.get("tool_calls") or [])}
            for outcome in outcomes:
                tool = outcome.get("tool") or "?"
                action = outcome.get("action")
                outcome_name = str(outcome.get("outcome") or "unknown")
                suffix = f" → {action}" if action and action != tool else ""
                tool_input = input_by_id.get(outcome.get("tool_call_id")) or {}
                input_hint = self._format_tool_input_hint(tool, tool_input)
                hint_suffix = f" — {input_hint}" if input_hint else ""
                tool_lines.append(f"`{tool}` · {outcome_name}{suffix}{hint_suffix}")
            value = "\n".join(tool_lines)
            if len(value) > 1024:
                value = value[:1000] + "\n…"
            embed.add_field(name=f"tool calls ({len(outcomes)})", value=value, inline=False)

        # --- field: rejections (if any) ---
        rejection_lines = []
        for outcome in outcomes or []:
            outcome_name = str(outcome.get("outcome") or "")
            if outcome_name.startswith("rejected") or outcome_name == "tool_error":
                err = outcome.get("error") or outcome_name
                rejection_lines.append(f"`{outcome.get('tool') or '?'}`: {err}")
        if rejection_lines:
            value = "\n".join(rejection_lines)
            if len(value) > 1024:
                value = value[:1000] + "\n…"
            embed.add_field(name=f"rejections ({len(rejection_lines)})", value=value, inline=False)

        # --- field: overrides (if any) ---
        override_lines = []
        for outcome in outcomes or []:
            if int(outcome.get("override_count") or 0):
                override_lines.append(
                    f"`{outcome.get('tool') or '?'}` · {outcome.get('override_count')} override(s)"
                )
        if override_lines:
            embed.add_field(name=f"overrides ({len(override_lines)})", value="\n".join(override_lines)[:1024], inline=False)

        # --- field: publishing (if any) ---
        if publish_results:
            publish_lines = []
            for result in publish_results:
                topic_id = result.get("topic_id") or "?"
                media_counts = result.get("source_media_counts") or {}
                publish_lines.append(
                    f"`{topic_id}` · {result.get('status') or '?'} · "
                    f"media_sent `{result.get('media_count', 0)}` · "
                    f"source_media `{media_counts.get('resolvable_media', 0)}` · "
                    f"messages `{result.get('flat_message_count', len(result.get('discord_message_ids') or []))}`"
                )
            embed.add_field(name=f"publishing ({len(publish_results)})", value="\n".join(publish_lines)[:1024], inline=False)

        trigger_label = metadata.get("trigger") or updates.get("trigger") or "n/a"
        embed.set_footer(text=f"trigger: {trigger_label} · env: {self.environment}")
        return embed

    def _format_metadata_time_range(self, metadata: Dict[str, Any]) -> str:
        timestamps = metadata.get("source_message_timestamps") or []
        if not timestamps:
            return ""
        try:
            earliest = min(timestamps)
            latest = max(timestamps)
            return f"`{earliest}` → `{latest}`"
        except Exception:
            return ""

    def _format_metadata_channel_coverage(self, metadata: Dict[str, Any]) -> str:
        channel_counts = metadata.get("source_channel_counts") or {}
        if not channel_counts:
            return ""
        try:
            ranked = sorted(channel_counts.items(), key=lambda item: item[1], reverse=True)[:5]
            return " ".join(f"`#{name}`·{count}" for name, count in ranked)
        except Exception:
            return ""

    def _format_tool_input_hint(self, tool: str, tool_input: Dict[str, Any]) -> str:
        if not tool_input:
            return ""
        if tool == "search_topics":
            query = tool_input.get("query")
            if query:
                snippet = str(query)[:80]
                return f"query=`{snippet}`"
        if tool == "search_messages":
            query = tool_input.get("query")
            scope = tool_input.get("scope") or "window"
            parts = []
            if query:
                parts.append(f"query=`{str(query)[:60]}`")
            parts.append(f"scope={scope}")
            return " ".join(parts)
        if tool == "get_author_profile":
            author_id = tool_input.get("author_id")
            if author_id is not None:
                return f"author=`{author_id}`"
        if tool == "get_message_context":
            ids = tool_input.get("message_ids") or []
            if ids:
                preview = ", ".join(str(x) for x in list(ids)[:3])
                more = "…" if len(ids) > 3 else ""
                return f"messages=`{preview}{more}`"
        if tool == "get_reply_chain":
            message_id = tool_input.get("message_id")
            if message_id:
                return f"message_id=`{str(message_id)[:40]}`"
        if tool in {"post_simple_topic", "post_sectioned_topic", "watch_topic"}:
            slug = tool_input.get("proposed_key")
            if slug:
                return f"key=`{slug}`"
        return ""

    async def _publish_topic(self, topic: Dict[str, Any]) -> Dict[str, Any]:
        topic_id = str(topic.get("topic_id"))
        guild_id = topic.get("guild_id") or self._resolve_guild_id()
        current_attempts = int(topic.get("publication_attempts") or 0)

        # Determine whether this is a structured-document topic
        summary = topic.get("summary") or {}
        has_blocks = isinstance(summary, dict) and bool(summary.get("blocks"))

        if has_blocks:
            # --- Structured block-by-block publishing ---
            # Collect all source IDs and hydrate metadata for jump URLs + media
            blocks = normalize_topic_document(topic)
            all_source_ids = collect_document_source_ids(blocks)
            source_metadata: Dict[str, Dict[str, Any]] = {}
            if all_source_ids:
                try:
                    rows = self.db.get_topic_editor_source_messages(
                        message_ids=all_source_ids,
                        guild_id=guild_id,
                        environment=self.environment,
                    )
                except Exception:
                    rows = []
                for row in rows:
                    source_metadata[str(row.get("message_id"))] = row

            # Build ordered publish units (text + media interleaved)
            publish_units = render_topic_publish_units(topic, source_metadata=source_metadata)

            # Flatten units through paragraph-aware chunking (used for suppressed
            # mode and fallback text display — NOT the primary send path).
            flat_messages: List[str] = []
            media_indices: Set[int] = set()
            _media_count = 0
            for idx, unit in enumerate(publish_units):
                if unit.get("kind") in ("media", "external"):
                    media_indices.add(len(flat_messages))
                    flat_messages.append(unit.get("url") or unit.get("fallback_url", ""))
                else:
                    for chunk in chunk_text_for_discord(unit["content"]):
                        flat_messages.append(chunk)
            media_source_counts = self._summarize_source_media_counts(source_metadata)
            logger.info(
                "TopicEditor publish plan: topic_id=%s structured=true blocks=%s sources=%s "
                "source_media=%s units=%s flat_messages=%s media_messages=%s publishing_enabled=%s",
                topic_id,
                len(blocks),
                len(all_source_ids),
                media_source_counts,
                len(publish_units),
                len(flat_messages),
                len(media_indices),
                self.publishing_enabled,
            )

            if not self.publishing_enabled:
                self.db.update_topic(
                    topic_id,
                    {
                        "guild_id": guild_id,
                        "publication_status": "suppressed",
                        "publication_error": None,
                    },
                    guild_id=guild_id,
                    environment=self.environment,
                )
                return {
                    "topic_id": topic_id,
                    "status": "suppressed",
                    "publish_units": publish_units,
                    "flat_messages": flat_messages,
                    "media_indices": sorted(media_indices),
                    "source_media_counts": media_source_counts,
                }

            # Build send units from publish_units.
            # Send-unit model:
            #   {'send_kind': 'text',     'content': str}
            #   {'send_kind': 'file_url', 'source_url': str, 'filename': str,
            #    'fallback_url': str, 'ref': dict}
            #   {'send_kind': 'file',     'file_path': str, 'filename': str,
            #    'fallback_url': str, 'ref': dict, 'trace': str}
            send_units: List[Dict[str, Any]] = []
            _build_send_units(publish_units, send_units, source_metadata)

            # Send block-by-block via send units
            sent_ids: List[int] = []
            error: Optional[str] = None
            had_failure = False
            publish_traces: List[Dict[str, str]] = []
            channel_id = self._resolve_live_channel_id(guild_id)
            try:
                channel = await self._resolve_discord_channel(channel_id)
                if channel is None:
                    raise RuntimeError(f"live_update_channel_not_found:{channel_id}")
                idx = 0
                while idx < len(send_units):
                    unit = send_units[idx]
                    if unit.get("send_kind") == "file_url":
                        batch = [unit]
                        idx += 1
                        while (
                            idx < len(send_units)
                            and send_units[idx].get("send_kind") == "file_url"
                            and len(batch) < 10
                        ):
                            batch.append(send_units[idx])
                            idx += 1
                        batch_sent_ids, batch_error, batch_traces = await self._send_file_url_batch(
                            channel,
                            batch,
                            topic_id,
                        )
                        sent_ids.extend(batch_sent_ids)
                        publish_traces.extend(batch_traces)
                        if batch_error:
                            error = batch_error
                            had_failure = True
                        if not batch_sent_ids:
                            had_failure = True
                        continue

                    unit_sent_id, unit_error, unit_trace = await self._send_one_unit(
                        channel, unit, topic_id
                    )
                    idx += 1
                    if unit_sent_id is not None:
                        sent_ids.append(unit_sent_id)
                    if unit_trace:
                        publish_traces.append(unit_trace)
                    if unit_error:
                        error = unit_error  # last error for top-level reporting
                        had_failure = True
                    if unit_sent_id is None:
                        had_failure = True
            except Exception as exc:
                error = str(exc)
                had_failure = True

            status = (
                "sent" if sent_ids and not had_failure
                else "partial" if sent_ids else "failed"
            )
            if publish_traces:
                logger.info(
                    "TopicEditor publish traces: topic_id=%s traces=%s",
                    topic_id, publish_traces,
                )
            updates = {
                "guild_id": guild_id,
                "publication_status": status,
                "publication_error": error,
                "discord_message_ids": sent_ids,
                "publication_attempts": current_attempts + 1,
                "last_published_at": datetime.now(timezone.utc).isoformat() if sent_ids else None,
            }
            self.db.update_topic(topic_id, updates, guild_id=guild_id, environment=self.environment)
            return {
                "topic_id": topic_id,
                "status": status,
                "discord_message_ids": sent_ids,
                "error": error,
                "media_count": len(media_indices),
                "flat_message_count": len(flat_messages),
                "source_media_counts": media_source_counts,
            }

        # --- Legacy simple-topic path (no blocks) ---
        rendered_messages = render_topic(topic)
        if not self.publishing_enabled:
            self.db.update_topic(
                topic_id,
                {
                    "guild_id": guild_id,
                    "publication_status": "suppressed",
                    "publication_error": None,
                },
                guild_id=guild_id,
                environment=self.environment,
            )
            return {"topic_id": topic_id, "status": "suppressed", "messages": rendered_messages}

        sent_ids: List[int] = []
        error: Optional[str] = None
        channel_id = self._resolve_live_channel_id(guild_id)
        try:
            channel = await self._resolve_discord_channel(channel_id)
            if channel is None:
                raise RuntimeError(f"live_update_channel_not_found:{channel_id}")
            for message in rendered_messages:
                sent = await channel.send(message)
                message_id = getattr(sent, "id", None)
                if message_id is not None:
                    sent_ids.append(int(message_id))
        except Exception as exc:
            error = str(exc)

        status = (
            "sent" if sent_ids and len(sent_ids) == len(rendered_messages) and not error
            else "partial" if sent_ids else "failed"
        )
        updates = {
            "guild_id": guild_id,
            "publication_status": status,
            "publication_error": error,
            "discord_message_ids": sent_ids,
            "publication_attempts": current_attempts + 1,
            "last_published_at": datetime.now(timezone.utc).isoformat() if sent_ids else None,
        }
        self.db.update_topic(topic_id, updates, guild_id=guild_id, environment=self.environment)
        return {"topic_id": topic_id, "status": status, "discord_message_ids": sent_ids, "error": error}

    async def _send_one_unit(
        self,
        channel: Any,
        unit: Dict[str, Any],
        topic_id: str,
    ) -> Tuple[Optional[int], Optional[str], Optional[Dict[str, str]]]:
        """Send a single send-unit to a Discord channel.

        Returns (sent_id, error, trace_dict).
        ``sent_id`` is the Discord message ID on success, or None.
        ``error`` is an error string on failure, or None.
        ``trace_dict`` is a per-unit trace for external-media resolution steps.

        Send-kinds:
          - ``text``  → ``channel.send(content)``
          - ``url``   → ``channel.send(content)`` (URL as plain text)
          - ``file``  → lazily resolve external media then send
            :class:`discord.File` on success; fallback to URL text on failure.
        """
        from src.common.external_media import sanitise_url_for_logs

        send_kind = unit.get("send_kind", "text")
        trace: Optional[Dict[str, str]] = None

        try:
            if send_kind == "text":
                sent = await channel.send(unit["content"])
                mid = getattr(sent, "id", None)
                return (int(mid) if mid else None, None, None)

            if send_kind == "url":
                sent = await channel.send(unit["content"])
                mid = getattr(sent, "id", None)
                return (int(mid) if mid else None, None, None)

            if send_kind == "file":
                fallback_url = unit.get("fallback_url", "")
                ref = unit.get("ref", {})
                safe_url = sanitise_url_for_logs(fallback_url)

                # ---- lazy-resolve external media ----
                result = await self._resolve_external_for_publish(fallback_url, ref)
                outcome = result.outcome.value if hasattr(result, "outcome") else "unknown"

                if outcome in ("cache_hit", "downloaded") and result.file_path:
                    # Send as discord.File
                    try:
                        filename = os.path.basename(result.file_path)
                        with open(result.file_path, "rb") as fh:
                            discord_file = discord.File(fh, filename=filename)
                        sent = await channel.send(file=discord_file)
                        mid = getattr(sent, "id", None)
                        trace = {
                            "url": safe_url,
                            "status": outcome,
                            "action": "file_sent",
                        }
                        return (int(mid) if mid else None, None, trace)
                    except Exception as file_exc:
                        logger.warning(
                            "TopicEditor: discord.File send failed for %s, "
                            "falling back to URL: %s",
                            safe_url, file_exc,
                        )
                        trace = {
                            "url": safe_url,
                            "status": "file_send_failed",
                            "action": "fallback_url",
                            "detail": str(file_exc)[:200],
                        }
                        # Fall through to send URL text
                        sent = await channel.send(fallback_url)
                        mid = getattr(sent, "id", None)
                        return (int(mid) if mid else None, None, trace)

                # ---- any other outcome: send the fallback URL ----
                trace = {
                    "url": safe_url,
                    "status": outcome,
                    "action": "fallback_url",
                    "detail": getattr(result, "failure_reason", "") or "",
                }
                sent = await channel.send(fallback_url)
                mid = getattr(sent, "id", None)
                return (int(mid) if mid else None, None, trace)

            return (None, f"unknown send_kind: {send_kind}", None)

        except Exception as exc:
            # Last-resort: try the fallback URL if we still have one
            error_str = str(exc)
            if send_kind == "file":
                fallback_url = unit.get("fallback_url", "")
                if fallback_url:
                    try:
                        sent = await channel.send(fallback_url)
                        mid = getattr(sent, "id", None)
                        trace = {
                            "url": sanitise_url_for_logs(fallback_url),
                            "status": "exception_fallback",
                            "action": "fallback_url",
                            "detail": error_str[:200],
                        }
                        return (int(mid) if mid else None, None, trace)
                    except Exception:
                        pass
            return (None, error_str, trace)

    async def _send_file_url_batch(
        self,
        channel: Any,
        units: Sequence[Dict[str, Any]],
        topic_id: str,
    ) -> Tuple[List[int], Optional[str], List[Dict[str, str]]]:
        """Download Discord-hosted media URLs and upload them as Discord files.

        Consecutive media units belong to the same document block, so this
        batches them into one Discord message when possible. If the batch upload
        fails, each media item falls back to its original URL so publishing can
        continue.
        """
        if not units:
            return [], None, []

        from src.common.external_media import sanitise_url_for_logs

        sent_ids: List[int] = []
        traces: List[Dict[str, str]] = []
        temp_paths: List[str] = []
        handles: List[Any] = []
        error: Optional[str] = None

        try:
            for unit in units:
                source_url = unit.get("source_url") or unit.get("fallback_url") or ""
                safe_url = sanitise_url_for_logs(source_url)
                try:
                    file_path, filename = await self._download_publish_media_url(source_url, unit)
                    temp_paths.append(file_path)
                    handle = open(file_path, "rb")
                    handles.append(handle)
                    traces.append({
                        "url": safe_url,
                        "status": "downloaded",
                        "action": "queued_file_upload",
                        "filename": filename,
                    })
                except Exception as exc:
                    error = str(exc)
                    traces.append({
                        "url": safe_url,
                        "status": "download_failed",
                        "action": "fallback_url",
                        "detail": str(exc)[:200],
                    })
                    sent = await channel.send(source_url)
                    mid = getattr(sent, "id", None)
                    if mid is not None:
                        sent_ids.append(int(mid))

            if not handles:
                return sent_ids, error, traces

            files = [
                discord.File(handle, filename=os.path.basename(getattr(handle, "name", "")) or None)
                for handle in handles
            ]
            try:
                sent = await channel.send(files=files)
                mid = getattr(sent, "id", None)
                if mid is not None:
                    sent_ids.append(int(mid))
                for trace in traces:
                    if trace.get("action") == "queued_file_upload":
                        trace["action"] = "files_sent"
            except Exception as exc:
                error = str(exc)
                logger.warning(
                    "TopicEditor: Discord file batch send failed for topic %s, falling back to URLs: %s",
                    topic_id,
                    exc,
                )
                for unit in units:
                    source_url = unit.get("source_url") or unit.get("fallback_url") or ""
                    sent = await channel.send(source_url)
                    mid = getattr(sent, "id", None)
                    if mid is not None:
                        sent_ids.append(int(mid))
                traces.append({
                    "status": "file_batch_send_failed",
                    "action": "fallback_url",
                    "detail": str(exc)[:200],
                })

            return sent_ids, error, traces
        finally:
            for handle in handles:
                try:
                    handle.close()
                except Exception:
                    pass
            for path in temp_paths:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.debug("TopicEditor: failed to delete temp media %s: %s", path, exc)

    async def _download_publish_media_url(
        self,
        source_url: str,
        unit: Dict[str, Any],
    ) -> Tuple[str, str]:
        if not source_url:
            raise ValueError("empty media URL")

        filename = _safe_publish_filename(
            unit.get("filename")
            or _filename_from_url(source_url)
            or f"topic-media-{int(time.time() * 1000)}"
        )
        suffix = os.path.splitext(filename)[1] or ".bin"
        fd, path = tempfile.mkstemp(prefix="topic-editor-media-", suffix=suffix)
        os.close(fd)

        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source_url) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"download HTTP {response.status}")
                    with open(path, "wb") as out:
                        async for chunk in response.content.iter_chunked(1024 * 256):
                            if chunk:
                                out.write(chunk)
            final_path = os.path.join(os.path.dirname(path), filename)
            os.replace(path, final_path)
            return final_path, filename
        except Exception:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise

    async def _resolve_external_for_publish(
        self, source_url: str, ref: Dict[str, Any]
    ) -> Any:
        """Lazily resolve an external media URL for publishing.

        Returns a :class:`ResolverResult` (or compatible duck-type).

        This is the only place where the T2 resolver is invoked during
        publishing. On failure the caller falls back to sending the original
        URL as text.
        """
        from src.features.summarising.external_media_resolver import (
            ExternalMediaResolver,
            ResolverResult,
            ResolveOutcome,
        )
        from src.common.external_media import make_cache_key

        resolver = ExternalMediaResolver()
        # Wire DB cache if available
        if self.db is not None and hasattr(self.db, "get_external_media_cache"):
            resolver._get_cache = self.db.get_external_media_cache
            resolver._upsert_cache = self.db.upsert_external_media_cache

        try:
            result = resolver.resolve(source_url)
            return result
        except Exception as exc:
            logger.warning(
                "TopicEditor: external resolver exception for %s: %s",
                source_url[:120], exc,
            )
            # Return a synthetic failure result
            return ResolverResult(
                outcome=ResolveOutcome.DOWNLOAD_FAILED,
                url_key=make_cache_key(source_url),
                source_url=source_url[:200],
                source_domain="unknown",
                status="download_failed",
                failure_reason=str(exc),
                trace=f"resolver exception: {exc}",
            )

    def _summarize_source_media_counts(self, source_metadata: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
        messages_with_media = 0
        attachments = 0
        embeds = 0
        resolvable_media = 0
        external_links = 0
        for row in source_metadata.values():
            row_attachments = self._normalize_attachment_list(row.get("attachments"))
            row_embeds = self._normalize_attachment_list(row.get("embeds"))
            attachment_urls = sum(
                1
                for item in row_attachments
                if isinstance(item, dict) and (item.get("url") or item.get("proxy_url"))
            )
            embed_urls = 0
            for idx, item in enumerate(row_embeds):
                if not isinstance(item, dict):
                    continue
                if _resolve_media_url_from_metadata(
                    {"message_id": str(row.get("message_id")), "kind": "embed", "index": idx},
                    {"embeds": row_embeds},
                ):
                    embed_urls += 1
            attachments += len(row_attachments)
            embeds += len(row_embeds)
            resolvable_media += attachment_urls + embed_urls
            # Count external links separately using the shared helper
            external_urls = extract_external_urls(row)
            external_links += len(external_urls)
            if row_attachments or row_embeds or external_urls:
                messages_with_media += 1
        return {
            "messages_with_media": messages_with_media,
            "attachments": attachments,
            "embeds": embeds,
            "resolvable_media": resolvable_media,
            "external_links": external_links,
        }

    async def _resolve_discord_channel(self, channel_id: Optional[int]) -> Any:
        if not self.bot or not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id)) if hasattr(self.bot, "get_channel") else None
        if channel is None and hasattr(self.bot, "fetch_channel"):
            channel = await self.bot.fetch_channel(int(channel_id))
        return channel

    def _extract_tool_calls(self, response: Any) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": block.input or {}})
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append({"id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
        return calls

    def _extract_usage(self, response: Any) -> Dict[str, int]:
        usage = getattr(response, "usage", None)
        if not usage:
            return {}
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }

    def _estimate_cost_usd(self, usage: Dict[str, Any]) -> Optional[float]:
        try:
            input_tokens = float(usage.get("input_tokens") or 0)
            output_tokens = float(usage.get("output_tokens") or 0)
            input_rate = float(os.getenv("TOPIC_EDITOR_INPUT_COST_PER_MTOKENS", "0") or 0)
            output_rate = float(os.getenv("TOPIC_EDITOR_OUTPUT_COST_PER_MTOKENS", "0") or 0)
            if input_rate <= 0 and output_rate <= 0:
                return None
            return round((input_tokens / 1_000_000.0 * input_rate) + (output_tokens / 1_000_000.0 * output_rate), 6)
        except (TypeError, ValueError):
            return None

    def _seed_cold_start_checkpoint(
        self,
        checkpoint_key: str,
        guild_id: Optional[int],
        live_channel_id: Optional[int],
    ) -> Dict[str, Any]:
        """Cold-start: anchor the checkpoint to (run interval ago) so the first
        run immediately processes the last interval's worth of messages.

        Default interval is `TOPIC_EDITOR_COLD_START_LOOKBACK_MINUTES` (default
        60) — matching the runner's typical 60-min cadence. Falls back to the
        most recent archived message id/timestamp for tie-breaking the SQL
        ordering on `(created_at, message_id)`.
        """
        lookback_minutes = self._env_float("TOPIC_EDITOR_COLD_START_LOOKBACK_MINUTES", 60.0)
        anchor_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        anchor_iso = anchor_dt.isoformat()

        # Resolve a real message_id older-than-or-at the anchor timestamp, so
        # `get_archived_messages_after_checkpoint` can index by message_id too.
        anchor_message_id: Optional[int] = None
        if hasattr(self.db, "get_archived_message_id_before_timestamp"):
            anchor_message_id = self.db.get_archived_message_id_before_timestamp(
                guild_id=guild_id, before=anchor_iso
            )
        # Last-resort fallback: take the latest archived message id (loses the hour-window
        # semantic but never poisons the checkpoint with NULL).
        if anchor_message_id is None and hasattr(self.db, "get_latest_archived_message_checkpoint"):
            latest = self.db.get_latest_archived_message_checkpoint(guild_id=guild_id)
            if latest:
                anchor_message_id = (latest or {}).get("message_id")

        checkpoint = {
            "checkpoint_key": checkpoint_key,
            "guild_id": guild_id,
            "channel_id": live_channel_id,
            "last_message_id": anchor_message_id,
            "last_message_created_at": anchor_iso,
            "state": {
                "seeded_from": "interval_lookback",
                "lookback_minutes": lookback_minutes,
            },
        }
        self.db.upsert_topic_editor_checkpoint(checkpoint, environment=self.environment)
        return checkpoint

    def _forced_close_error(
        self,
        reason: str,
        max_turns: int,
        cumulative_cost_usd: float,
        cumulative_tokens: int,
    ) -> str:
        if reason == "cost_cap_exceeded":
            return f"cumulative_cost_usd={cumulative_cost_usd} exceeded TOPIC_EDITOR_MAX_COST_USD"
        if reason == "token_cap_exceeded":
            return f"cumulative_tokens={cumulative_tokens} exceeded TOPIC_EDITOR_MAX_TOKENS"
        return f"max_turns={max_turns} reached without finalize_run"

    def _format_cost(self, value: Any) -> str:
        return f"${value:.4f}" if isinstance(value, (int, float)) else "n/a"

    def _env_float(self, name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _env_int(self, name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _checkpoint_after(self, checkpoint: Dict[str, Any], messages: Sequence[Dict[str, Any]], run_id: str) -> Dict[str, Any]:
        if not messages:
            return dict(checkpoint, last_run_id=run_id)
        last = messages[-1]
        return {
            "checkpoint_key": checkpoint.get("checkpoint_key"),
            "guild_id": checkpoint.get("guild_id") or self._resolve_guild_id(),
            "channel_id": checkpoint.get("channel_id") or self._resolve_live_channel_id(self._resolve_guild_id()),
            "last_message_id": last.get("message_id"),
            "last_message_created_at": last.get("created_at"),
            "last_run_id": run_id,
            "state": {"source_count": len(messages)},
        }

    @staticmethod
    def _normalize_attachment_list(attachments: Any) -> List[Dict[str, Any]]:
        """Return a list of attachment dicts from list, dict, or JSON-string inputs."""
        if attachments is None:
            return []
        if isinstance(attachments, list):
            return attachments
        if isinstance(attachments, dict):
            # Single attachment passed as a dict (e.g. from some archive shapes).
            return [attachments]
        if isinstance(attachments, str):
            try:
                parsed = json.loads(attachments)
            except (json.JSONDecodeError, TypeError):
                return []
            return TopicEditor._normalize_attachment_list(parsed)
        return []

    def _message_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        attachments = self._normalize_attachment_list(message.get("attachments"))
        embeds = self._normalize_attachment_list(message.get("embeds"))
        media_urls: List[str] = []
        media_refs_available: List[Dict[str, Any]] = []

        for idx, attachment in enumerate(attachments):
            url = attachment.get("url") or attachment.get("proxy_url")
            if url and isinstance(url, str):
                media_urls.append(url)
            media_refs_available.append({
                "kind": "attachment",
                "index": idx,
                "url_present": bool(attachment.get("url") or attachment.get("proxy_url")),
                "content_type": attachment.get("content_type"),
                "filename": attachment.get("filename"),
            })

        for embed_idx, embed in enumerate(embeds if isinstance(embeds, list) else []):
            if isinstance(embed, dict):
                for key in ("url", "thumbnail", "image", "video"):
                    value = embed.get(key)
                    if isinstance(value, dict):
                        url = value.get("url") or value.get("proxy_url")
                        if url and isinstance(url, str):
                            media_urls.append(url)
                    elif isinstance(value, str) and value:
                        media_urls.append(value)
            media_refs_available.append({
                "kind": "embed",
                "index": embed_idx,
                "url_present": (
                    isinstance(embed, dict) and bool(
                        (isinstance(embed.get("url"), dict) and (embed["url"].get("url") or embed["url"].get("proxy_url")))
                        or (isinstance(embed.get("thumbnail"), dict) and (embed["thumbnail"].get("url") or embed["thumbnail"].get("proxy_url")))
                        or (isinstance(embed.get("image"), dict) and (embed["image"].get("url") or embed["image"].get("proxy_url")))
                        or (isinstance(embed.get("video"), dict) and (embed["video"].get("url") or embed["video"].get("proxy_url")))
                    )
                ),
                "content_type": None,
                "filename": None,
            })

        # ── External linked media refs (after attachment/embed for priority indexing) ──
        # Uses the shared extract_external_urls helper so external index N
        # is deterministic regardless of caller.
        for external_entry in extract_external_urls(message):
            media_refs_available.append({
                "kind": external_entry["kind"],
                "index": external_entry["index"],
                "domain": external_entry["domain"],
                "url_present": external_entry["url_present"],
                "source": external_entry["source"],
            })
            # external URLs are resolved lazily, not pre-fetched into media_urls

        return {
            "message_id": message.get("message_id"),
            "guild_id": message.get("guild_id"),
            "channel_id": message.get("channel_id"),
            "author_id": message.get("author_id"),
            "author": self._author_name(message),
            "content": message.get("content") or message.get("clean_content"),
            "created_at": message.get("created_at"),
            "reaction_count": self._message_reaction_count(message),
            "media_urls": media_urls,
            "media_refs_available": media_refs_available,
        }

    def _messages_by_id(self, messages: Sequence[Dict[str, Any]], ids: Sequence[str]) -> List[Dict[str, Any]]:
        wanted = {str(item) for item in ids}
        return [message for message in messages if str(message.get("message_id")) in wanted]

    def _source_authors(self, messages: Sequence[Dict[str, Any]]) -> List[str]:
        authors = []
        for message in messages:
            author = self._author_name(message)
            if author and author not in authors:
                authors.append(author)
        return authors

    def _author_name(self, message: Dict[str, Any]) -> str:
        snapshot = message.get("author_context_snapshot") or {}
        return str(snapshot.get("server_nick") or snapshot.get("global_name") or snapshot.get("username") or message.get("author_name") or message.get("author_id") or "")

    def _summary_for_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "post_sectioned_topic":
            summary: Dict[str, Any] = {
                "body": args.get("body"),
                "sections": args.get("sections") or [],
            }
            # Store blocks when provided alongside preserving body/sections for
            # backwards-compat readability.
            if args.get("blocks"):
                summary["blocks"] = args["blocks"]
            return summary
        if tool_name == "watch_topic":
            return {
                "why_interesting": args.get("why_interesting"),
                "revisit_when": args.get("revisit_when"),
            }
        return {"body": args.get("body"), "media": args.get("media") or []}

    def _resolve_guild_id(self) -> Optional[int]:
        if self.guild_id is not None:
            return int(self.guild_id)
        value = os.getenv("DEV_GUILD_ID") if self.environment == "dev" else os.getenv("GUILD_ID")
        return int(value) if value and str(value).isdigit() else None

    def _resolve_live_channel_id(self, guild_id: Optional[int]) -> Optional[int]:
        if self.live_channel_id is not None:
            return int(self.live_channel_id)
        env_names = ["DEV_SUMMARY_CHANNEL_ID", "DEV_LIVE_UPDATE_CHANNEL_ID"] if self.environment == "dev" else ["LIVE_UPDATE_CHANNEL_ID", "SUMMARY_CHANNEL_ID"]
        for name in env_names:
            value = os.getenv(name)
            if value and str(value).isdigit():
                return int(value)
        return None

    def _excluded_author_ids(self) -> List[int]:
        return [int(part) for part in os.getenv("LIVE_UPDATE_EXCLUDED_AUTHOR_IDS", "").split(",") if part.strip().isdigit()]

    @staticmethod
    def _checkpoint_key(guild_id: Optional[int], live_channel_id: Optional[int]) -> str:
        return f"live_update_editor:{guild_id or 'unknown'}:{live_channel_id or 'unknown'}"


@dataclass(frozen=True)
class TopicIdentity:
    topic_id: str
    canonical_key: str
    headline: str
    source_authors: Sequence[str] = field(default_factory=tuple)
    aliases: Sequence[str] = field(default_factory=tuple)
    state: Optional[str] = None
    display_slug: Optional[str] = None


@dataclass(frozen=True)
class Collision:
    topic_id: str
    canonical_key: str
    headline: str
    reason: str
    similarity: Optional[float] = None
    aliases: Sequence[str] = field(default_factory=tuple)
    state: Optional[str] = None


def canonicalize_topic_key(
    headline: str,
    *,
    creator_name: Optional[str] = None,
    topic_date: Optional[date | str] = None,
) -> str:
    """Return the locked sprint canonical key for a topic headline."""
    parts: List[str] = []
    if creator_name:
        parts.append(_slugify(creator_name))
    parts.append(_slugify(headline))
    if topic_date:
        parts.append(str(topic_date)[:10])
    return "-".join(part for part in parts if part).strip("-")


def canonicalize_proposed_key(
    proposed_key: Optional[str],
    headline: str,
    *,
    creator_name: Optional[str] = None,
    topic_date: Optional[date | str] = None,
) -> str:
    base = proposed_key or headline
    return canonicalize_topic_key(base, creator_name=creator_name, topic_date=topic_date)


def resolve_topic_alias(
    proposed_key: str,
    aliases: Iterable[Dict[str, Any]],
    *,
    environment: Optional[str] = None,
    guild_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a proposed key against topic_aliases-style rows."""
    canonical = canonicalize_topic_key(proposed_key)
    for alias in aliases or []:
        if environment is not None and alias.get("environment") != environment:
            continue
        if guild_id is not None and alias.get("guild_id") != guild_id:
            continue
        alias_key = canonicalize_topic_key(str(alias.get("alias_key") or ""))
        if alias_key == canonical:
            return alias
    return None


def detect_topic_collisions(
    *,
    proposed_canonical_key: str,
    headline: str,
    source_authors: Sequence[str],
    existing_topics: Sequence[TopicIdentity | Dict[str, Any]],
    threshold: float = SIMILARITY_COLLISION_THRESHOLD,
) -> List[Collision]:
    """Find canonical-prefix or trigram+author-overlap collisions."""
    source_author_set = _normalize_author_set(source_authors)
    collisions: List[Collision] = []
    proposed_canonical_key = canonicalize_topic_key(proposed_canonical_key)

    for raw_topic in existing_topics or []:
        topic = _coerce_topic_identity(raw_topic)
        existing_key = canonicalize_topic_key(topic.canonical_key)
        prefix_match = _canonical_prefix_match(proposed_canonical_key, existing_key)
        similarity = trigram_similarity(headline, topic.headline)
        author_overlap = len(source_author_set & _normalize_author_set(topic.source_authors))
        alias_match = any(
            _canonical_prefix_match(proposed_canonical_key, canonicalize_topic_key(alias))
            for alias in topic.aliases
        )

        if prefix_match or alias_match:
            reason = "canonical_key_prefix"
        elif similarity >= threshold and author_overlap >= 1:
            reason = "headline_similarity_author_overlap"
        else:
            continue

        collisions.append(Collision(
            topic_id=topic.topic_id,
            canonical_key=topic.canonical_key,
            headline=topic.headline,
            reason=reason,
            similarity=similarity,
            aliases=tuple(topic.aliases),
            state=topic.state,
        ))

    return collisions


def unresolved_collisions(
    collisions: Sequence[Collision],
    override_collisions: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Collision]:
    override_ids = {
        str(item.get("topic_id"))
        for item in override_collisions or []
        if item.get("topic_id")
    }
    return [collision for collision in collisions if collision.topic_id not in override_ids]


def parse_optional_datetime(value: Any) -> Optional[str]:
    """Return an ISO timestamp only for concrete date/time values.

    Tool callers often use natural-language ``revisit_when`` strings such as
    "tomorrow" or "when more results appear". The database column is a
    timestamp, so prose must stay in the topic summary rather than being written
    to ``revisit_at``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def shape_transition_payload(
    *,
    outcome: str,
    tool_name: Optional[str] = None,
    canonical_key: Optional[str] = None,
    proposed_key: Optional[str] = None,
    source_message_ids: Optional[Sequence[Any]] = None,
    collisions: Optional[Sequence[Collision]] = None,
    override_collisions: Optional[Sequence[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "outcome": outcome,
    }
    if tool_name:
        payload["tool_name"] = tool_name
    if canonical_key:
        payload["canonical_key"] = canonical_key
    if proposed_key:
        payload["proposed_key"] = proposed_key
    if source_message_ids is not None:
        payload["source_message_ids"] = [str(message_id) for message_id in source_message_ids]
    if collisions is not None:
        payload["collisions"] = [collision_to_dict(collision) for collision in collisions]
    if override_collisions:
        payload["override_collisions"] = [
            {"topic_id": str(item.get("topic_id")), "reason": item.get("reason")}
            for item in override_collisions
        ]
    if error:
        payload["error"] = error
    if extra:
        payload.update(extra)
    return payload


def build_rejected_transition(
    *,
    run_id: str,
    environment: str,
    guild_id: int,
    action: str,
    tool_call_id: Optional[str],
    reason: str,
    payload: Dict[str, Any],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if action not in {"rejected_post_simple", "rejected_post_sectioned", "rejected_watch"}:
        raise ValueError(f"unsupported rejected transition action: {action}")
    return {
        "run_id": run_id,
        "environment": environment,
        "guild_id": guild_id,
        "tool_call_id": tool_call_id,
        "action": action,
        "reason": reason,
        "payload": payload,
        "model": model,
    }


def build_override_transitions(
    *,
    run_id: str,
    environment: str,
    guild_id: int,
    topic_id: str,
    override_collisions: Sequence[Dict[str, Any]],
    tool_call_id: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for override in override_collisions or []:
        rows.append({
            "topic_id": topic_id,
            "run_id": run_id,
            "environment": environment,
            "guild_id": guild_id,
            "tool_call_id": tool_call_id,
            "action": "override",
            "reason": override.get("reason"),
            "payload": {
                "overridden_topic_id": str(override.get("topic_id")),
                "reason": override.get("reason"),
            },
            "model": model,
        })
    return rows


# ------------------------------------------------------------------
# Document normalization helpers (T1)
# ------------------------------------------------------------------

def normalize_media_ref(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a media ref to canonical shape: {message_id, kind: 'attachment'|'embed'|'external', index}.

    Accepts shorthand {message_id, attachment_index} which normalizes to
    {kind: 'attachment', index: attachment_index}.  Rejects invalid kind values.
    """
    if not isinstance(ref, dict):
        raise ValueError(f"media_ref must be a dict, got {type(ref).__name__}")

    message_id = str(ref.get("message_id") or "")
    if not message_id:
        raise ValueError("media_ref missing required 'message_id'")

    # Shorthand: {message_id, attachment_index}  →  canonical attachment ref
    if "attachment_index" in ref and "kind" not in ref:
        index = ref.get("attachment_index", 0)
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError(
                f"media_ref attachment_index must be an integer, got {index!r}"
            )
        return {"message_id": message_id, "kind": "attachment", "index": index}

    kind = ref.get("kind", "attachment")
    if kind not in ("attachment", "embed", "external"):
        raise ValueError(
            f"media_ref kind must be 'attachment', 'embed', or 'external', got {kind!r}"
        )

    index = ref.get("index", 0)
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise ValueError(f"media_ref index must be an integer, got {index!r}")

    return {"message_id": message_id, "kind": kind, "index": index}



def normalize_document_blocks(
    summary: Dict[str, Any],
    topic_source_message_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Normalize a topic summary into ordered document blocks.

    Handles legacy summaries with body/sections/source_message_ids by converting:
      * body       → intro block
      * sections   → section blocks
      * topic-level source_message_ids used as fallback when a block has no
        local sources.

    If *summary.blocks* is already present each block is individually
    normalized and returned.
    """
    if isinstance(summary, dict) and summary.get("blocks"):
        # Already has blocks — normalise each one individually
        blocks: List[Dict[str, Any]] = []
        for b in summary["blocks"]:
            if not isinstance(b, dict):
                continue
            block_type = b.get("type", "section")
            if block_type not in ("intro", "section"):
                continue
            blocks.append({
                "type": block_type,
                "title": b.get("title"),
                "text": str(b.get("text") or b.get("body") or ""),
                "source_message_ids": [
                    str(sid)
                    for sid in (b.get("source_message_ids") or [])
                    if sid
                ],
                "media_refs": [
                    normalize_media_ref(r) for r in (b.get("media_refs") or [])
                ],
            })
        return blocks

    # Legacy path: convert body + sections into blocks
    body = (
        (summary.get("body") or "").strip()
        if isinstance(summary, dict)
        else ""
    )
    sections = (
        summary.get("sections") or []
        if isinstance(summary, dict)
        else []
    )
    fallback_ids = [
        str(sid) for sid in (topic_source_message_ids or []) if sid
    ]

    blocks = []

    if body:
        blocks.append({
            "type": "intro",
            "title": None,
            "text": body,
            "source_message_ids": list(fallback_ids),
            "media_refs": [],
        })

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = sec.get("title") or sec.get("heading")
        text = (
            sec.get("body")
            or sec.get("text")
            or sec.get("summary")
            or ""
        )
        sec_source_ids = [
            str(sid) for sid in (sec.get("source_message_ids") or []) if sid
        ]
        if not sec_source_ids:
            sec_source_ids = list(fallback_ids)
        blocks.append({
            "type": "section",
            "title": title,
            "text": str(text),
            "source_message_ids": sec_source_ids,
            "media_refs": [],
        })

    return blocks


def normalize_topic_document(topic: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize a full topic dict into ordered document blocks.

    Handles both legacy topics (body / sections) and new-style topics
    (summary.blocks).
    """
    summary = topic.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {"body": str(summary)}
    topic_source_ids = topic.get("source_message_ids") or []
    return normalize_document_blocks(summary, topic_source_ids)


def block_source_ids(block: Dict[str, Any]) -> List[str]:
    """Extract distinct source message IDs from a single block."""
    return list(
        dict.fromkeys(
            str(sid) for sid in (block.get("source_message_ids") or []) if sid
        )
    )


def block_media_refs(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract normalized media refs from a single block."""
    return [normalize_media_ref(r) for r in (block.get("media_refs") or [])]


def collect_document_source_ids(blocks: List[Dict[str, Any]]) -> List[str]:
    """Return the distinct union of all block-level source message IDs."""
    seen: Set[str] = set()
    result: List[str] = []
    for block in blocks:
        for sid in block_source_ids(block):
            if sid not in seen:
                seen.add(sid)
                result.append(sid)
    return result


def render_topic(topic: Dict[str, Any]) -> List[str]:
    """Render a topic into Discord message text without DB or Discord effects."""
    headline = _clean_render_text(topic.get("headline") or topic.get("display_slug") or "Live update")
    summary = topic.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {"body": str(summary)}
    prefix = "Update" if topic.get("parent_topic_id") else "Live update"
    source_suffix = _render_source_suffix(topic)

    sections = summary.get("sections") or []
    if sections:
        lines = [f"## {prefix}: {headline}"]
        body = _clean_render_text(summary.get("body"))
        if body:
            lines.extend(["", body])
        for section in sections:
            if not isinstance(section, dict):
                continue
            title = _clean_render_text(section.get("title") or section.get("heading") or "Details")
            section_body = _clean_render_text(section.get("body") or section.get("text") or section.get("summary"))
            lines.extend(["", f"**{title}**"])
            if section_body:
                lines.append(section_body)
        if source_suffix:
            lines.extend(["", source_suffix])
        return [_trim_discord_message("\n".join(lines))]

    body = _clean_render_text(summary.get("body") or summary.get("why_interesting") or topic.get("body"))
    lines = [f"## {prefix}: {headline}"]
    if body:
        lines.extend(["", body])
    # Simple topics are text-only. Media refs belong in structured block
    # media_refs so they can be validated, chunked, and sent separately.
    if source_suffix:
        lines.extend(["", source_suffix])
    return [_trim_discord_message("\n".join(lines))]


def render_topic_publish_units(
    topic: Dict[str, Any],
    source_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Render a structured topic into ordered publish units for block-by-block sending.

    Returns a list of units, each of which is either:

        {"kind": "text", "content": "..."}

    or

        {"kind": "media", "url": "https://...", "ref": {...}}

    The order is deterministic: header, intro text with inline linked
    citations, intro media, each section text with per-block citations, that
    section media.

    Citations are per-block, deduped, and ordered by first appearance.
    No global Sources footer is emitted for structured topics.

    This function is only called when the topic summary contains ``blocks``.
    For simple/legacy topics, use the existing ``render_topic`` function.
    """
    blocks = normalize_topic_document(topic)
    if not blocks:
        # Fallback: use legacy render_topic for simple topics
        rendered = render_topic(topic)
        return [{"kind": "text", "content": msg} for msg in rendered]

    headline = _clean_render_text(
        topic.get("headline") or topic.get("display_slug") or "Live update"
    )
    prefix = "Update" if topic.get("parent_topic_id") else "Live update"
    header = f"## {prefix}: {headline}"

    units: List[Dict[str, Any]] = []

    # Build a lookup: global source_id → metadata row (for jump URL construction)
    meta_by_id: Dict[str, Dict[str, Any]] = source_metadata or {}
    emitted_media_keys: Set[Tuple[str, ...]] = set()

    for block in blocks:
        block_text = block.get("text", "").strip()
        block_title = block.get("title")
        block_sids = block_source_ids(block)

        # Build inline citation map for this block
        # Dedupe + preserve order of first appearance
        seen: Set[str] = set()
        ordered_ids: List[str] = []
        for sid in block_sids:
            if sid not in seen:
                seen.add(sid)
                ordered_ids.append(sid)

        # Build the text content for this block
        lines: List[str] = []
        if block["type"] == "section" and block_title:
            lines.append(f"**{_clean_render_text(block_title)}**")
        elif block["type"] == "intro":
            # Intro block gets the header; subsequent intro blocks are unusual
            # but handled gracefully (no duplicate header).
            pass

        if block_text:
            lines.append(block_text)

        # Per-block citations. Discord does not render Markdown link syntax
        # like ``[label](url)``, so include the actual jump URLs next to the
        # relevant block instead of emitting fake inline links.
        if ordered_ids:
            citation_parts: List[str] = []
            for idx, sid in enumerate(ordered_ids, start=1):
                meta = meta_by_id.get(sid, {})
                guild_id = meta.get("guild_id") or topic.get("guild_id")
                channel_id = meta.get("channel_id")
                url = ""
                if guild_id and channel_id and sid:
                    url = (
                        f"https://discord.com/channels/{guild_id}/"
                        f"{channel_id}/{sid}"
                    )
                if url:
                    citation_parts.append(f"[{idx}] {url}")
                else:
                    citation_parts.append(f"[{idx}] {sid}")
            lines.append("Sources: " + " ".join(citation_parts))

        block_content = "\n".join(lines)
        if block["type"] == "intro":
            block_content = header + "\n\n" + block_content

        units.append({"kind": "text", "content": block_content})

        # Media refs for this block
        for ref in block_media_refs(block):
            meta = meta_by_id.get(ref["message_id"], {})
            url = _resolve_media_url_from_metadata(ref, meta)
            if url:
                if url in block_content:
                    continue
                media_key = (str(url),)
                if not media_key[0]:
                    media_key = (
                        str(ref.get("message_id")),
                        str(ref.get("kind", "attachment")),
                        str(ref.get("index", 0)),
                    )
                if media_key in emitted_media_keys:
                    continue
                emitted_media_keys.add(media_key)
                # External refs carry their kind so the publisher can
                # distinguish lazy-resolve (external) from direct-attach media.
                unit_kind = "media"
                if ref.get("kind") == "external":
                    unit_kind = "external"
                units.append({
                    "kind": unit_kind,
                    "url": url,
                    "ref": ref,
                })

    return units


def _build_send_units(
    publish_units: List[Dict[str, Any]],
    send_units_out: List[Dict[str, Any]],
    source_metadata: Dict[str, Dict[str, Any]],
) -> None:
    """Build explicit send units from publish_units.

    Populates ``send_units_out`` in-place with dicts following the send-unit model:

        {'send_kind': 'text',   'content': str}
        {'send_kind': 'file_url', 'source_url': str, 'filename': str,
         'fallback_url': str, 'ref': dict}
        {'send_kind': 'file',   'file_path': str, 'filename': str,
         'fallback_url': str, 'ref': dict, 'trace': str}

    Text units contain the block content (already header-wrapped). Media units
    that are NOT external become ``file_url`` send units; the publisher
    downloads and reuploads consecutive file_url units together.
    External units become ``file`` send units; the actual resolution/download
    happens lazily in :meth:`TopicEditor._send_one_unit` so the caller can
    decide whether to invoke the resolver.
    """
    from src.common.external_media import sanitise_url_for_logs

    for unit in publish_units:
        kind = unit.get("kind", "text")
        if kind == "text":
            send_units_out.append({"send_kind": "text", "content": unit["content"]})
        elif kind == "media":
            ref = unit.get("ref", {})
            meta = source_metadata.get(str(ref.get("message_id")), {})
            if not _is_reuploadable_discord_media_url(unit["url"]):
                send_units_out.append({
                    "send_kind": "url",
                    "content": unit["url"],
                    "ref": ref,
                })
                continue
            send_units_out.append({
                "send_kind": "file_url",
                "source_url": unit["url"],
                "fallback_url": unit["url"],
                "filename": _filename_for_media_ref(ref, meta, unit["url"]),
                "ref": ref,
            })
        elif kind == "external":
            url = unit.get("url", "")
            ref = unit.get("ref", {})
            safe = sanitise_url_for_logs(url)
            send_units_out.append({
                "send_kind": "file",
                "file_path": "",          # filled by _send_one_unit after resolve
                "filename": "",           # filled by _send_one_unit after resolve
                "fallback_url": url,
                "ref": ref,
                "trace": f"external: pending resolve for {safe}",
            })


def _resolve_media_url_from_metadata(
    ref: Dict[str, Any], meta: Dict[str, Any]
) -> Optional[str]:
    """Resolve a media ref to an actual URL using source message metadata."""
    kind = ref.get("kind", "attachment")
    index = ref.get("index", 0)
    if kind == "attachment":
        attachments = meta.get("attachments") or []
        if isinstance(attachments, list) and 0 <= index < len(attachments):
            att = attachments[index]
            if isinstance(att, dict):
                return att.get("url") or att.get("proxy_url")
    elif kind == "embed":
        embeds = meta.get("embeds") or []
        if isinstance(embeds, list) and 0 <= index < len(embeds):
            emb = embeds[index]
            if isinstance(emb, dict):
                for key in ("url", "thumbnail", "image", "video"):
                    value = emb.get(key)
                    if isinstance(value, dict):
                        url = value.get("url") or value.get("proxy_url")
                        if url:
                            return url
                    elif isinstance(value, str) and value:
                        return value
    elif kind == "external":
        from src.common.external_media import extract_external_url_at_index
        return extract_external_url_at_index(meta, index)
    return None


def _filename_for_media_ref(ref: Dict[str, Any], meta: Dict[str, Any], url: str) -> str:
    kind = ref.get("kind", "attachment")
    index = int(ref.get("index") or 0)
    if kind == "attachment":
        attachments = meta.get("attachments") or []
        if isinstance(attachments, list) and 0 <= index < len(attachments):
            att = attachments[index]
            if isinstance(att, dict) and att.get("filename"):
                return _safe_publish_filename(att.get("filename"))
    return _safe_publish_filename(_filename_from_url(url) or f"media-{index}.bin")


def _filename_from_url(url: str) -> str:
    try:
        path = urlparse(str(url)).path
    except Exception:
        return ""
    name = unquote(os.path.basename(path or ""))
    return name


def _safe_publish_filename(value: Any) -> str:
    name = str(value or "media.bin").strip().replace("\x00", "")
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "media.bin"
    return name[:120]


def _is_reuploadable_discord_media_url(url: str) -> bool:
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return False
    return host in {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }


def _indent_trace_block(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in str(text).splitlines())


def _chunk_trace_lines(lines: Sequence[str], limit: int = 1900) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in lines:
        line = str(line)
        next_len = current_len + len(line) + 1
        if current and next_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line) + 1
        else:
            current.append(line)
            current_len = next_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def collision_to_dict(collision: Collision) -> Dict[str, Any]:
    return {
        "topic_id": collision.topic_id,
        "canonical_key": collision.canonical_key,
        "headline": collision.headline,
        "reason": collision.reason,
        "similarity": collision.similarity,
        "aliases": list(collision.aliases),
        "state": collision.state,
    }


def trigram_similarity(a: str, b: str) -> float:
    a_trigrams = _trigrams(a)
    b_trigrams = _trigrams(b)
    if not a_trigrams and not b_trigrams:
        return 1.0
    if not a_trigrams or not b_trigrams:
        return 0.0
    return (2.0 * len(a_trigrams & b_trigrams)) / (len(a_trigrams) + len(b_trigrams))


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _canonical_prefix_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _normalize_author_set(authors: Sequence[Any]) -> Set[str]:
    return {
        str(author).strip().lower()
        for author in authors or []
        if str(author).strip()
    }


def _trigrams(value: str) -> Set[str]:
    normalized = f"  {_slugify(value).replace('-', ' ')}  "
    if len(normalized) < 3:
        return {normalized} if normalized.strip() else set()
    return {normalized[index:index + 3] for index in range(len(normalized) - 2)}


def _coerce_topic_identity(topic: TopicIdentity | Dict[str, Any]) -> TopicIdentity:
    if isinstance(topic, TopicIdentity):
        return topic
    return TopicIdentity(
        topic_id=str(topic.get("topic_id")),
        canonical_key=str(topic.get("canonical_key") or ""),
        headline=str(topic.get("headline") or ""),
        source_authors=tuple(topic.get("source_authors") or ()),
        aliases=tuple(topic.get("aliases") or ()),
        state=topic.get("state"),
        display_slug=topic.get("display_slug"),
    )


def _render_source_suffix(topic: Dict[str, Any]) -> str:
    ids = [str(item) for item in topic.get("source_message_ids") or [] if item]
    if not ids:
        return ""
    label = "Source" if len(ids) == 1 else "Sources"
    return f"{label}: " + ", ".join(ids)


def _clean_render_text(value: Any) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(value or "").strip())


def _trim_discord_message(value: str) -> str:
    text = value.strip()
    if len(text) <= 2000:
        return text
    return text[:1997].rstrip() + "..."


def chunk_text_for_discord(text: str, limit: int = 2000) -> List[str]:
    """Split text into Discord-safe chunks preserving paragraph boundaries.

    Strategy (applied only to blocks exceeding *limit*):
      1. Split on blank-line paragraph boundaries first.
      2. Within an oversized paragraph, split on single newlines.
      3. Individual lines that are still too long are hard-split.

    Normal blocks that fit within *limit* are returned as a single-element list.
    """
    if len(text) <= limit:
        return [text]

    paragraphs = re.split(r"\n\n+", text)
    chunks: List[str] = []
    current: List[str] = []

    def _flush() -> None:
        if current:
            chunks.append("\n\n".join(current))
            current.clear()

    for para in paragraphs:
        trial = "\n\n".join(current + [para]) if current else para
        if len(trial) <= limit:
            current.append(para)
            continue
        # Current paragraph would overflow — flush what we have
        _flush()
        if len(para) <= limit:
            current.append(para)
            continue

        # Strategy 2: split oversized paragraph on single newlines
        lines = para.split("\n")
        sub_buf: List[str] = []
        for line in lines:
            trial2 = "\n".join(sub_buf + [line]) if sub_buf else line
            if len(trial2) <= limit:
                sub_buf.append(line)
                continue
            if sub_buf:
                chunks.append("\n".join(sub_buf))
                sub_buf.clear()
            # Strategy 3: hard-split individual long line
            if len(line) > limit:
                for i in range(0, len(line), limit - 3):
                    chunks.append(line[i : i + limit - 3].rstrip())
            else:
                sub_buf.append(line)
        if sub_buf:
            chunks.append("\n".join(sub_buf))

    _flush()
    return chunks
