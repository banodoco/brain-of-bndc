"""
Deterministic core for the topic-centered live-update editor.

This module intentionally keeps canonicalization, alias resolution, collision
checks, and transition payload shaping pure so they can be tested without
Anthropic or Discord dependencies.
"""

from __future__ import annotations

import re
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import discord

from src.features.summarising.live_update_prompts import DEFAULT_LIVE_UPDATE_MODEL


SIMILARITY_COLLISION_THRESHOLD = 0.55

READ_TOOL_NAMES = {
    "search_topics",
    "search_messages",
    "get_author_profile",
    "get_message_context",
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
(search_topics, search_messages, get_author_profile, get_message_context) when
you need more context. For every concrete development worth acting on, call a
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
        "description": "Search archived Discord messages in the current source window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "channel_id": {"type": "integer"},
                "author_id": {"type": "integer"},
                "hours_back": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
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
        "name": "post_simple_topic",
        "description": "Publish a single-author, one or two source-message topic.",
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
        "description": "Publish a multi-source or multi-contributor topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "body": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "object"}},
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "parent_topic_id": {"type": "string"},
                "notes": {"type": "string"},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "body", "sections", "source_message_ids"],
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

    async def run_once(self, trigger: str = "scheduled") -> Dict[str, Any]:
        if not self.db:
            raise RuntimeError("TopicEditor requires an injected db_handler")
        if not self.llm_client:
            raise RuntimeError("TopicEditor requires an Anthropic/Claude client")

        started = time.monotonic()
        guild_id = self._resolve_guild_id()
        live_channel_id = self._resolve_live_channel_id(guild_id)
        checkpoint_key = self._checkpoint_key(guild_id, live_channel_id)
        checkpoint = self.db.get_topic_editor_checkpoint(checkpoint_key, environment=self.environment)
        if checkpoint is None:
            checkpoint = self.db.mirror_live_checkpoint_to_topic_editor(checkpoint_key, environment=self.environment)
        checkpoint = checkpoint or {"checkpoint_key": checkpoint_key, "guild_id": guild_id, "channel_id": live_channel_id}

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

        metadata: Dict[str, Any] = {
            "tool_calls": [],
            "publishing_enabled": self.publishing_enabled,
            "trigger": trigger,
        }
        try:
            messages = self.db.get_archived_messages_after_checkpoint(
                checkpoint=checkpoint,
                guild_id=guild_id,
                channel_ids=None,
                limit=self.source_limit,
                exclude_author_ids=self._excluded_author_ids(),
            )
            active_topics = self.db.get_topics(
                guild_id=guild_id,
                states=["posted", "watching"],
                limit=100,
                environment=self.environment,
            )
            aliases = self.db.get_topic_aliases(guild_id=guild_id, environment=self.environment)
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
            initial_payload = self._build_initial_user_payload(messages, active_topics)
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
            }
            tool_calls: List[Dict[str, Any]] = []
            outcomes: List[Dict[str, Any]] = []
            total_input_tokens = 0
            total_output_tokens = 0
            text_chunks: List[str] = []
            forced_close = False
            turn_count = 0
            for turn_count in range(1, max_turns + 1):
                response = await self._invoke_anthropic(messages_arg)
                turn_tool_calls = self._extract_tool_calls(response)
                turn_reasoning = self._extract_reasoning_text(response)
                if turn_reasoning:
                    text_chunks.append(turn_reasoning)
                usage = self._extract_usage(response)
                total_input_tokens += int(usage.get("input_tokens", 0) or 0)
                total_output_tokens += int(usage.get("output_tokens", 0) or 0)

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
                    break

            if forced_close and not dispatcher_context.get("finalize"):
                # Loud audit row when we hit the budget without a clean finalize.
                self._store_transition({
                    "run_id": run_id,
                    "guild_id": guild_id,
                    "action": "rejected_finalize_run",
                    "reason": "max_turns_reached_without_finalize",
                    "payload": shape_transition_payload(
                        outcome="tool_error",
                        tool_name="finalize_run",
                        error=f"max_turns={max_turns} reached without finalize_run",
                    ),
                    "model": self.model,
                })

            metadata["tool_calls"] = [
                {"id": call["id"], "name": call["name"], "input": call["input"]}
                for call in tool_calls
            ]
            metadata["usage"] = {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}
            metadata["turn_count"] = turn_count
            metadata["forced_close"] = forced_close
            finalize = dispatcher_context.get("finalize") or {}
            metadata["reasoning"] = finalize.get("overall_reasoning") or "\n\n".join(text_chunks).strip()
            metadata["topics_considered"] = finalize.get("topics_considered") or []
            # Capture surface info for the trace embed.
            metadata["source_message_timestamps"] = [m.get("created_at") for m in messages if m.get("created_at")]
            metadata["source_channel_counts"] = self._tally_channels(messages)
            metadata["active_topics_count"] = len(active_topics)

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
            )
            self.db.complete_topic_editor_run(run_id, updates, guild_id=guild_id, environment=self.environment)
            trace_messages = self._format_trace_messages(run_id, updates, outcomes, publish_results)
            await self._emit_trace(trace_messages, run_id=run_id, updates=updates, outcomes=outcomes, publish_results=publish_results)
            return {
                "status": "completed",
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

    def _build_initial_user_payload(
        self,
        messages: Sequence[Dict[str, Any]],
        active_topics: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "source_messages": [self._message_payload(message) for message in messages],
            "active_topics": [
                {
                    "topic_id": topic.get("topic_id"),
                    "canonical_key": topic.get("canonical_key"),
                    "headline": topic.get("headline"),
                    "state": topic.get("state"),
                    "aliases": topic.get("aliases") or [],
                }
                for topic in active_topics
            ],
        }

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
        if name in {"search_topics", "search_messages", "get_author_profile", "get_message_context"}:
            return f"tool={name} status=ok (read tools currently return stub results — relevant data is already in the initial source payload)"
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
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                if text:
                    out.append({"type": "text", "text": text})
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
        replay_outcome = self._idempotent_replay_outcome(call, context)
        if replay_outcome:
            return replay_outcome
        if name in READ_TOOL_NAMES:
            return {"tool_call_id": call["id"], "tool": name, "outcome": "read"}
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
        source_ids = self._unique_ids(args.get("source_message_ids") or [])
        source_messages = self._messages_by_id(context["messages"], source_ids)
        source_authors = self._source_authors(source_messages)
        canonical_key = canonicalize_proposed_key(args.get("proposed_key"), args.get("headline") or "")
        action_by_tool = {
            "post_simple_topic": "post_simple",
            "post_sectioned_topic": "post_sectioned",
            "watch_topic": "watch",
        }
        state = "watching" if call["name"] == "watch_topic" else "posted"
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
        if call["name"] == "post_sectioned_topic" and not args.get("sections"):
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_sectioned",
                reason="post_sectioned_requires_sections",
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
            "revisit_at": args.get("revisit_when") if state == "watching" else None,
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
        if action == "finalize_run" and payload.get("outcome") == "accepted":
            context["finalize"] = {
                "overall_reasoning": payload.get("overall_reasoning") or row.get("reason") or "",
                "topics_considered": list(payload.get("topics_considered") or []),
            }
        return {
            "tool_call_id": str(tool_call_id),
            "tool": call.get("name"),
            "outcome": "idempotent_replay",
            "action": action,
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
    ) -> Dict[str, Any]:
        usage = metadata.get("usage") or {}
        return {
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
            "cost_usd": self._estimate_cost_usd(usage),
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
            publish_lines.append(f"- `{topic_id}`: {status}")
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
        sections = [
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
        ]
        embed.add_field(name="summary", value="\n".join(summary_lines)[:1024], inline=True)

        # --- field: model & cost ---
        cost = updates.get("cost_usd")
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
        model_lines = [
            f"model: `{updates.get('model') or 'n/a'}`",
            f"tokens in/out: `{updates.get('input_tokens', 0)}` / `{updates.get('output_tokens', 0)}`",
            f"cost: `{cost_str}`",
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
                publish_lines.append(f"`{topic_id}` · {result.get('status') or '?'}")
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
        if tool in {"search_topics", "search_messages"}:
            query = tool_input.get("query")
            if query:
                snippet = str(query)[:80]
                return f"query=`{snippet}`"
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
        if tool in {"post_simple_topic", "post_sectioned_topic", "watch_topic"}:
            slug = tool_input.get("proposed_key")
            if slug:
                return f"key=`{slug}`"
        return ""

    async def _publish_topic(self, topic: Dict[str, Any]) -> Dict[str, Any]:
        topic_id = str(topic.get("topic_id"))
        rendered_messages = render_topic(topic)
        current_attempts = int(topic.get("publication_attempts") or 0)
        if not self.publishing_enabled:
            self.db.update_topic(
                topic_id,
                {
                    "guild_id": topic.get("guild_id") or self._resolve_guild_id(),
                    "publication_status": "suppressed",
                    "publication_error": None,
                },
                guild_id=topic.get("guild_id") or self._resolve_guild_id(),
                environment=self.environment,
            )
            return {"topic_id": topic_id, "status": "suppressed", "messages": rendered_messages}

        sent_ids: List[int] = []
        error: Optional[str] = None
        channel_id = self._resolve_live_channel_id(topic.get("guild_id") or self._resolve_guild_id())
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

        status = "sent" if sent_ids and len(sent_ids) == len(rendered_messages) and not error else "partial" if sent_ids else "failed"
        updates = {
            "guild_id": topic.get("guild_id") or self._resolve_guild_id(),
            "publication_status": status,
            "publication_error": error,
            "discord_message_ids": sent_ids,
            "publication_attempts": current_attempts + 1,
            "last_published_at": datetime.now(timezone.utc).isoformat() if sent_ids else None,
        }
        self.db.update_topic(
            topic_id,
            updates,
            guild_id=topic.get("guild_id") or self._resolve_guild_id(),
            environment=self.environment,
        )
        return {"topic_id": topic_id, "status": status, "discord_message_ids": sent_ids, "error": error}

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

    def _message_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "message_id": message.get("message_id"),
            "channel_id": message.get("channel_id"),
            "author_id": message.get("author_id"),
            "author": self._author_name(message),
            "content": message.get("content") or message.get("clean_content"),
            "created_at": message.get("created_at"),
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
            return {"body": args.get("body"), "sections": args.get("sections") or []}
        if tool_name == "watch_topic":
            return {"why_interesting": args.get("why_interesting")}
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
        lines = [f"**{prefix}: {headline}**"]
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
    lines = [f"**{prefix}: {headline}**"]
    if body:
        lines.extend(["", body])
    media = summary.get("media") or topic.get("media") or []
    for item in media:
        text = _clean_render_text(item)
        if text:
            lines.append(text)
    if source_suffix:
        lines.extend(["", source_suffix])
    return [_trim_discord_message("\n".join(lines))]


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
