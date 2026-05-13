"""Prompt and structured-output helpers for the live-update editor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import inspect
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("DiscordBot")

DEFAULT_LIVE_UPDATE_MODEL = os.getenv("LIVE_UPDATE_EDITOR_MODEL", "claude-opus-4-6")
DEFAULT_AGENT_MAX_TURNS = max(1, min(500, int(os.getenv("LIVE_UPDATE_AGENT_MAX_TURNS", "100"))))
DEFAULT_AGENT_MAX_TOOL_REQUESTS_PER_TURN = max(
    1,
    min(12, int(os.getenv("LIVE_UPDATE_AGENT_MAX_TOOL_REQUESTS_PER_TURN", "8"))),
)
DEFAULT_LIVE_UPDATE_LLM_TIMEOUT_SECONDS = max(
    30,
    int(os.getenv("LIVE_UPDATE_LLM_TIMEOUT_SECONDS", "300")),
)
LIVE_UPDATE_TYPES = {
    "project_update",
    "release",
    "showcase",
    "question",
    "request",
    "event",
    "milestone",
    "top_creation",
    "other",
}


class LiveUpdateCandidateGenerator:
    """Generate normalized live-update candidates with auditable raw output."""

    NEWSWORTHY_TERMS = (
        "animatediff",
        "beta",
        "breakthrough",
        "checkpoint",
        "controlnet",
        "custom node",
        "dataset",
        "demo",
        "flux",
        "github",
        "launch",
        "lora",
        "merged",
        "milestone",
        "open source",
        "release",
        "released",
        "research",
        "script",
        "ship",
        "shipped",
        "testers",
        "tool",
        "training",
        "workflow",
    )
    CONCRETE_ACTION_TERMS = (
        "built",
        "launched",
        "merged",
        "open-sourced",
        "published",
        "ready",
        "released",
        "shared workflow",
        "shipped",
        "uploaded",
    )
    ARTIFACT_TERMS = (
        "article",
        "custom node",
        "dataset",
        "feature",
        "github",
        "lora",
        "model",
        "node",
        "repo",
        "release",
        "script",
        "tool",
        "tutorial",
        "workflow",
    )
    LOW_SIGNAL_PATTERNS = (
        "anyone know",
        "can anyone help",
        "does anyone know",
        "hello",
        "hi ",
        "i'm interested",
        "im interested",
        "looking forward",
        "thanks",
        "thank you",
        "working on",
    )

    def __init__(
        self,
        *,
        llm_client: Optional[Any] = None,
        model: Optional[str] = None,
        logger_instance: Optional[logging.Logger] = None,
        max_candidates: int = 12,
        max_agent_turns: Optional[int] = None,
        max_tool_requests_per_turn: Optional[int] = None,
    ):
        self.llm_client = llm_client
        self.model = model or DEFAULT_LIVE_UPDATE_MODEL
        self.logger = logger_instance or logger
        self.max_candidates = max_candidates
        self.max_agent_turns = int(max_agent_turns or DEFAULT_AGENT_MAX_TURNS)
        self.max_tool_requests_per_turn = int(
            max_tool_requests_per_turn or DEFAULT_AGENT_MAX_TOOL_REQUESTS_PER_TURN
        )
        self.llm_timeout_seconds = DEFAULT_LIVE_UPDATE_LLM_TIMEOUT_SECONDS
        self.last_agent_trace: Dict[str, Any] = {}
        self.reasoning_recovery_path = "none"
        self.watchlist_actions: List[Dict[str, Any]] = []

    async def generate_candidates(
        self,
        *,
        messages: List[Dict[str, Any]],
        run_id: str,
        guild_id: Optional[int],
        memory: List[Dict[str, Any]],
        watchlist: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        tool_runner: Optional[Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]] | Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Generate candidates with an LLM when available, falling back to heuristics."""
        if self.llm_client and hasattr(self.llm_client, "generate_chat_completion"):
            try:
                llm_messages = [{
                    "role": "user",
                    "content": self.build_user_prompt(messages, memory, watchlist, context or {}),
                }]
                raw_output = ""
                tool_trace: List[Dict[str, Any]] = []
                self.watchlist_actions = []
                agent_turn_count = 0
                for turn_index in range(1, self.max_agent_turns + 1):
                    agent_turn_count = turn_index
                    self.logger.info(
                        "[LiveUpdateCandidateGenerator] model turn %s/%s starting: model=%s messages=%s timeout=%ss",
                        turn_index,
                        self.max_agent_turns,
                        self.model,
                        len(llm_messages),
                        self.llm_timeout_seconds,
                    )
                    raw_output = await asyncio.wait_for(
                        self.llm_client.generate_chat_completion(
                            model=self.model,
                            system_prompt=self.build_system_prompt(),
                            messages=llm_messages,
                            max_tokens=4096,
                            temperature=0.2,
                        ),
                        timeout=self.llm_timeout_seconds,
                    )
                    self.logger.info(
                        "[LiveUpdateCandidateGenerator] model turn %s completed: output_chars=%s",
                        turn_index,
                        len(raw_output or ""),
                    )
                    tool_requests = self._parse_tool_requests(raw_output)
                    if not tool_requests or not tool_runner:
                        self.logger.info(
                            "[LiveUpdateCandidateGenerator] model turn %s produced final output: tool_requests=%s",
                            turn_index,
                            len(tool_requests),
                        )
                        break
                    self.logger.info(
                        "[LiveUpdateCandidateGenerator] model turn %s requested %s tool(s); running %s",
                        turn_index,
                        len(tool_requests),
                        min(len(tool_requests), self.max_tool_requests_per_turn),
                    )
                    tool_results = []
                    for request in tool_requests[: self.max_tool_requests_per_turn]:
                        result = await self._run_requested_tool(tool_runner, request)
                        tool_results.append(result)
                        tool_trace.append(result)
                        result_summary = result.get("error") or self._summarize_tool_result(result.get("result"))
                        self.logger.info(
                            "[LiveUpdateCandidateGenerator] tool %s ok=%s summary=%s",
                            result.get("tool"),
                            result.get("ok"),
                            result_summary,
                        )
                    llm_messages.append({"role": "assistant", "content": raw_output})
                    llm_messages.append({
                        "role": "user",
                        "content": json.dumps({
                            "tool_results": tool_results,
                            "instruction": "Use these tool results to continue. Request more tools only if essential; otherwise return final JSON with candidates.",
                        }, ensure_ascii=True, default=str),
                    })
                parsed_candidates = self._parse_raw_candidates(raw_output)
                self.last_agent_trace = {
                    "agent_turn_count": agent_turn_count,
                    "tool_trace": tool_trace,
                    "watchlist_actions": self.watchlist_actions,
                    "raw_text": raw_output,
                    "editor_reasoning": getattr(self, "last_editor_reasoning", ""),
                    "reasoning_recovery_path": getattr(self, "reasoning_recovery_path", "none"),
                    "model": self.model,
                    "max_agent_turns": self.max_agent_turns,
                    "max_tool_requests_per_turn": self.max_tool_requests_per_turn,
                }
                self.logger.info(
                    "[LiveUpdateCandidateGenerator] parsed candidates: parsed=%s normalized_limit=%s turns=%s tools=%s",
                    0 if parsed_candidates is None else len(parsed_candidates),
                    self.max_candidates,
                    agent_turn_count,
                    len(tool_trace),
                )
                normalized = self._normalize_candidates(
                    parsed_candidates,
                    source_messages=messages,
                    run_id=run_id,
                    guild_id=guild_id,
                    scanned_message_count=len(messages),
                    raw_agent_output={
                        "generator": "live_update_editor_llm_v1",
                        "model": self.model,
                        "raw_text": raw_output,
                        "context_expansion": context or {},
                        "agent_turn_count": agent_turn_count,
                        "tool_trace": tool_trace,
                        "max_agent_turns": self.max_agent_turns,
                        "max_tool_requests_per_turn": self.max_tool_requests_per_turn,
                    },
                )
                if parsed_candidates is not None:
                    return normalized[: self.max_candidates]
            except Exception as exc:
                self.logger.warning(
                    "[LiveUpdateCandidateGenerator] LLM candidate generation failed; using heuristic fallback: %s",
                    exc,
                    exc_info=True,
                )

        return self._generate_heuristic_candidates(
            messages=messages,
            run_id=run_id,
            guild_id=guild_id,
            memory=memory,
            watchlist=watchlist,
        )

    @staticmethod
    def _summarize_tool_result(result: Any) -> str:
        if isinstance(result, dict):
            pieces = []
            for key, value in result.items():
                if isinstance(value, list):
                    pieces.append(f"{key}={len(value)}")
                elif isinstance(value, dict):
                    pieces.append(f"{key}=object")
                elif value is not None:
                    pieces.append(f"{key}={str(value)[:80]}")
            return ", ".join(pieces)[:300] or "empty"
        if isinstance(result, list):
            return f"list={len(result)}"
        return str(result)[:300]

    @staticmethod
    def build_system_prompt() -> str:
        return (
            "You are the BNDC live-update editor. Each hour you scan newly-archived "
            "Discord messages from the Banodoco community (AI art / generative video) "
            "and decide what to publish, watchlist, story-update, or skip.\n\n"

            "═══ OUTPUT (read carefully) ═══\n"
            "Every response is EXACTLY ONE JSON object. Never multiple JSON blocks "
            "in one response — multiple blocks are a BUG and will be dropped.\n\n"
            "Two valid shapes:\n"
            "(A) Tool turn — when you need to look something up OR add/update the "
            "watchlist. Return ONLY:\n"
            "  {\"tool_requests\": [ {\"tool\": \"...\", \"args\": {...}}, ... ]}\n"
            "(B) Final turn — your last response for this run. Return ONLY:\n"
            "  REASONING: <1-3 sentences>\n"
            "  \n"
            "  {\"candidates\": [...], \"editor_reasoning\": \"<same 1-3 sentences>\"}\n\n"
            "Pick A or B. Never mix. If you have BOTH lookups to do AND candidates "
            "to emit AND any watchlist intent, use turn A first and **batch ALL tool "
            "calls into a single `tool_requests` array** — searches, watchlist_add, "
            "and watchlist_update all go in the same array on the same turn. "
            "watchlist_add is NOT a separate turn from searches; bundle them. After "
            "results come back, use turn B with candidates. There is no third turn — "
            "once you emit B, you cannot watchlist anything you forgot. NEVER "
            "narrate watchlist or publish intent without actually invoking the tool "
            "or emitting the candidate — claims in editor_reasoning that aren't "
            "backed by a tool call or candidate row are a BUG and will be visible "
            "as bugs in run logs.\n\n"

            "═══ TRIAGE — every item lands in exactly one bucket ═══\n"
            "PUBLISH (new story → candidate): genuinely new + real community "
            "engagement (reactions, replies, downstream use) + verifiable from "
            "source messages or tool results.\n\n"
            "STORY UPDATE (follow-up on covered story → candidate): an already-"
            "posted story has a concrete NEW development — a fix, a new capability, "
            "a novel experiment by another member, a performance milestone, "
            "third-party validation that changes the verdict, a new variant. "
            "Format: same as PUBLISH but set `update_type=\"project_update\"`, set "
            "`duplicate_key` to `<original-key>-<update-slug>` (e.g. "
            "`omninft-lora-ltx23-comfyui-2026-05-13-exorcist-fix`), and start the "
            "title with `Update:`. Body must reference prior coverage and explain "
            "what's new. Already-covered does NOT mean off the table.\n\n"
            "WATCHLIST (call watchlist_add): promising but missing community signal "
            "/ verification — high-signal poster shared something fresh with no "
            "reactions yet, tool release with no downstream testing, tease without "
            "an artifact, work whose value depends on community reproduction. MOST "
            "'almost' items go here, NOT in skip. If your only complaint is 'no "
            "reactions yet', that's a WATCHLIST signal, not a skip signal.\n\n"
            "SKIP (do nothing): routine chatter, support questions, intros, generic "
            "announcements, BNDC bot telemetry, continued discussion of covered "
            "stories with no concrete new finding, additional reactions on existing "
            "posts, minor parameter variations without a conclusion.\n\n"
            "Empty candidates + empty watchlist is the WRONG answer when the source "
            "pool has any promising-but-unverified content.\n\n"

            "═══ Already-covered decision tree ═══\n"
            "Match against existing feed items / duplicate_keys, then ask:\n"
            "1. Concrete new development on top? → STORY UPDATE.\n"
            "2. Promising-but-unverified new angle? → WATCHLIST.\n"
            "3. Just more conversation/reactions? → SKIP.\n\n"

            "═══ Watchlist tool examples ═══\n"
            "Adding (note: this is a TOOL TURN — only this JSON, nothing else):\n"
            "  {\"tool_requests\": [{\"tool\": \"watchlist_add\", \"args\": {\n"
            "    \"watch_key\": \"ostris-toolkit-hidream-lora-2026-05-13\",\n"
            "    \"title\": \"Ostris AI Toolkit adds HiDream LoRA training support\",\n"
            "    \"reason\": \"Known maintainer, concrete release link, zero reactions \"\n"
            "              \"or downstream testing yet. Revisit when community tries it.\",\n"
            "    \"source_message_ids\": [\"1503901234567890123\"],\n"
            "    \"channel_id\": 1359132958354706633,\n"
            "    \"subject_type\": \"release\"\n"
            "  }}]}\n"
            "watch_key: lowercase, hyphenated, `<creator>-<artifact>-<date>` or "
            "`<tool>-<feature>-<date>`. Must be stable across runs.\n"
            "subject_type: release | showcase | project_update | discussion | other.\n\n"
            "Acting on existing entries (in the `watchlist` field of the user payload):\n"
            "• Fresh: no action unless new signal.\n"
            "• Revisit_due / last_call: MUST decide this run — call "
            "`watchlist_update` with action=publish_now (signal arrived), extend "
            "(still developing), or discard (dead). Last_call auto-discards at 72h.\n"
            "If a source message in this run maps to an existing watch_key, prefer "
            "`watchlist_update(action='publish_now')` over a fresh candidate.\n\n"

            "═══ Editorial bar ═══\n"
            "Focus on what's NEW: releases, fresh discoveries, novel techniques, "
            "breaking developments. Prioritize community-made / open-source work "
            "over commercial announcements. Strong creative posts qualify as PUBLISH "
            "only when the work is unusually good AND has real community response. "
            "Don't overstate — no hyperbole, preserve context (iteration, "
            "collaboration, specific conditions). Credit creators with bold names.\n\n"
            "Reputation is a weak signal — it can support validity, not prove "
            "factual claims. An older message can become newly relevant via fresh "
            "reactions/replies in the last hour.\n\n"

            "═══ Candidate fields ═══\n"
            "Required on each candidate: decision (publish/defer/skip/duplicate), "
            "update_type, title (6-14 words, factual), body (1-2 sentences, 35-220 "
            "words, compact prose, no bullet lists, does not repeat title), "
            "media_refs (up to 4 URLs, hero first), source_message_ids, "
            "author_context_snapshot, duplicate_key, confidence (0-1), priority "
            "(int), rationale, evidence_message_ids, new_information, why_now, "
            "duplicate_assessment, context_used, community_validation, uncertainty, "
            "risk_flags, defer_until_or_condition, editor_notes, editorial_checklist.\n"
            f"update_type ∈ {{{', '.join(sorted(LIVE_UPDATE_TYPES))}}}.\n\n"
            "editorial_checklist: boolean+note for source_verified, "
            "new_information_identified, prior_updates_checked, "
            "surrounding_history_checked, author_context_considered, "
            "community_signal_checked, duplicate_checked, public_value_clear, "
            "risk_checked, media_selected_when_useful, publish_format_ready. "
            "If any gate is false/unknown AND fixable by more time/signal "
            "(community_signal, source_verified, author_context), prefer "
            "watchlist_add. Use defer/skip only when the topic doesn't belong in "
            "the live feed at all.\n\n"
            "Link rules: no Discord jump-links in body (added automatically as "
            "footer). External links (github, huggingface, project pages, papers, "
            "demos, twitter) ARE allowed in body — bare URL or [label](url) — when "
            "they identify the artifact. Release of a tool/page → that link belongs "
            "in the body.\n\n"

            "═══ Multi-example trigger (READ — this is the difference between one "
            "wall of media and a structured post) ═══\n"
            "HARD RULE — app-enforced: if `source_message_ids.length >= 3` OR your "
            "source messages span TWO OR MORE distinct authors, you MUST emit a "
            "non-empty `examples` array — one example per contributor/angle — "
            "instead of flattening all media into the main `media_refs`. Omitting "
            "`examples` in this case is REJECTED at the app layer with reason "
            "`missing_examples_for_multi_source_candidate`. The single-message form "
            "(flat `media_refs`) is RESERVED for one creator's one post — typically "
            "1-2 source_message_ids, all from the same author.\n"
            "When examples ARE used, set candidate-level `media_refs` to empty (or "
            "to just the hero asset). The per-example `media_urls` arrays carry the "
            "media. Do NOT also flatten them into media_refs — that double-posts.\n"
            "Examples shape: `examples: [{caption, source_message_id, "
            "source_channel_id?, source_thread_id?, media_urls: [...]}]`. Up to 5 "
            "examples; up to 4 media_urls each. Captions: 4-10 words, no period, "
            "describe what THIS example adds (not 'Another example' / 'More "
            "media'). Good: 'Gleb i2v test at strength 2.0' / 'PhoenixRisen \"huge "
            "difference\" reaction' / 'David t2v sweep 1.0/1.6/2.0'.\n"
            "Publisher posts: HEADER (title + body + primary jump-link), then one "
            "Discord message per example (▸ caption + media + per-example "
            "jump-link).\n"
            "Per-example bar: each must independently be worth a reader's "
            "attention. Two near-identical posts are one example; pick the "
            "stronger.\n\n"

            "═══ Tools ═══\n"
            "Available tools are listed in `available_tools` in the user payload. "
            "Investigate before finalizing when judgment hangs on it — duplicate "
            "checks, author/poster context, parent message of a reply, prior "
            "related coverage, whether a media post drew real response. Batch "
            "tool calls into one `tool_requests` array per turn. Don't pad turns."
        )

    def build_user_prompt(
        self,
        messages: List[Dict[str, Any]],
        memory: List[Dict[str, Any]],
        watchlist: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        prompt_messages = messages[:1000]
        payload = {
            "messages": [
                {
                    "message_id": str(msg.get("message_id")),
                    "channel_id": msg.get("channel_id"),
                    "channel_name": msg.get("channel_name"),
                    "thread_id": msg.get("thread_id"),
                    "jump_url": LiveUpdateCandidateGenerator._message_jump_url(msg),
                    "reference_id": msg.get("reference_id"),
                    "is_reply": bool(msg.get("reference_id")),
                    "is_thread_message": bool(msg.get("thread_id")),
                    "reply_jump_url": LiveUpdateCandidateGenerator._reply_jump_url(msg),
                    "author_id": msg.get("author_id"),
                    "author_name": msg.get("author_name"),
                    "content": LiveUpdateCandidateGenerator._clean_content(msg.get("content") or ""),
                    "created_at": msg.get("created_at"),
                    "age_bucket": LiveUpdateCandidateGenerator._age_bucket(msg.get("created_at"), now),
                    "is_last_hour": LiveUpdateCandidateGenerator._is_within_hours(msg.get("created_at"), now, 1),
                    "reaction_count": msg.get("reaction_count") or msg.get("unique_reactor_count") or 0,
                    "reply_count": msg.get("reply_count") or msg.get("thread_reply_count") or 0,
                    "attachments": LiveUpdateCandidateGenerator._jsonish(msg.get("attachments") or []),
                    "embeds": LiveUpdateCandidateGenerator._jsonish(msg.get("embeds") or []),
                    "author_context_snapshot": msg.get("author_context_snapshot") or {},
                }
                for msg in prompt_messages
            ],
            "message_window_coverage": {
                "source_message_count": len(messages or []),
                "prompt_message_count": len(prompt_messages),
                "truncated_for_prompt": len(messages or []) > len(prompt_messages),
                "last_hour_prompt_message_count": sum(
                    1
                    for msg in prompt_messages
                    if LiveUpdateCandidateGenerator._is_within_hours(msg.get("created_at"), now, 1)
                ),
                "last_three_hours_prompt_message_count": sum(
                    1
                    for msg in prompt_messages
                    if LiveUpdateCandidateGenerator._is_within_hours(msg.get("created_at"), now, 3)
                ),
                "note": "Each message also has age_bucket and is_last_hour so recency is explicit.",
            },
            "editorial_memory": [
                {
                    "memory_key": row.get("memory_key"),
                    "subject_type": row.get("subject_type"),
                    "summary": row.get("summary"),
                    "importance": row.get("importance"),
                }
                for row in (memory or [])[:40]
            ],
            "watchlist": LiveUpdateCandidateGenerator._render_watchlist(watchlist, now),
            "context_expansion": context or {},
            "agent_runtime_budget": {
                "max_agent_turns": self.max_agent_turns,
                "max_tool_requests_per_turn": self.max_tool_requests_per_turn,
                "note": (
                    "The editor may request multiple tool turns before finalizing. "
                    "Use enough turns to check context, duplicates, author history, "
                    "and media/community response; do not spend turns once the decision is clear."
                ),
            },
            "available_tools": [
                {
                    "tool": "search_messages",
                    "purpose": "Search earlier archived Discord messages by query, author, channel, and time range.",
                    "args": {"query": "string", "author_id": "optional int", "channel_id": "optional int", "hours_back": "optional int", "limit": "optional int"},
                },
                {
                    "tool": "get_message_context",
                    "purpose": "Fetch exact archived source messages plus surrounding same-channel/author context by message_ids. If a message has reference_id, pass that parent id here to inspect what it was replying to. If a message has thread_id, use this plus search_messages filtered by channel/thread context to understand the thread.",
                    "args": {"message_ids": ["message id strings"], "limit": "optional int"},
                },
                {
                    "tool": "get_author_profile",
                    "purpose": "Fetch author stats, profile snapshot, average reactions, and recent messages.",
                    "args": {"author_id": "int"},
                },
                {
                    "tool": "get_engagement_context",
                    "purpose": "Fetch parent reply context, direct/nearby responses, reactor profiles, and participant reputation for source messages. Use this to see who someone replied to, who replied back, who reacted, and whether the engagement came from high-signal members.",
                    "args": {"message_ids": ["message id strings"], "participant_limit": "optional int"},
                },
                {
                    "tool": "get_recent_reactions",
                    "purpose": "Fetch recent reaction events, including older messages that received new reactions in the last hour and the reactor's high-level reputation.",
                    "args": {"hours_back": "optional int", "limit": "optional int"},
                },
                {
                    "tool": "search_previous_updates",
                    "purpose": "Search earlier live updates to avoid repeats or add only genuinely new context.",
                    "args": {"query": "string", "hours_back": "optional int", "limit": "optional int"},
                },
                {
                    "tool": "get_recent_updates",
                    "purpose": "Fetch recent live updates even without a query.",
                    "args": {"hours_back": "optional int", "limit": "optional int"},
                },
                {
                    "tool": "search_editorial_memory",
                    "purpose": "Search longer-lived editorial memory/watch notes.",
                    "args": {"query": "string", "limit": "optional int"},
                },
                {
                    "tool": "watchlist_add",
                    "purpose": "Add an item to the watchlist when something is promising but unclear — not yet ready to publish but worth revisiting later. Use this for emerging stories, partial signals, or interesting content that needs more community reaction before a decision.",
                    "args": {
                        "watch_key": "string (stable identifier — we de-dupe on this)",
                        "title": "string (1-line description of what is being watched)",
                        "reason": "string (why this is interesting but not yet ready)",
                        "source_message_ids": ["string (message ids this is grounded in)"],
                        "channel_id": "optional int",
                        "subject_type": "optional string (showcase | project_update | discussion | other)",
                    },
                },
                {
                    "tool": "watchlist_update",
                    "purpose": "Update a previously watchlisted item: publish it now (converts to a candidate this run), extend the watch window (story still developing), or discard it (no longer relevant). Items in last_call state MUST be decided — they will auto-discard after 72h if not acted upon.",
                    "args": {
                        "watch_key": "string",
                        "action": "'publish_now' | 'extend' | 'discard'",
                        "notes": "optional string (reason for the action)",
                    },
                },
            ],
        }
        return json.dumps(payload, ensure_ascii=True, default=str)

    @staticmethod
    def _parse_created_at(created_at: Any) -> Optional[datetime]:
        if not created_at:
            return None
        try:
            text = str(created_at)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_within_hours(created_at: Any, now: datetime, hours: int) -> bool:
        parsed = LiveUpdateCandidateGenerator._parse_created_at(created_at)
        if not parsed:
            return False
        age_seconds = (now - parsed).total_seconds()
        return 0 <= age_seconds <= max(1, hours) * 3600

    @staticmethod
    def _age_bucket(created_at: Any, now: datetime) -> str:
        parsed = LiveUpdateCandidateGenerator._parse_created_at(created_at)
        if not parsed:
            return "unknown"
        age_seconds = max(0, (now - parsed).total_seconds())
        if age_seconds <= 3600:
            return "last_hour"
        if age_seconds <= 3 * 3600:
            return "last_3_hours"
        if age_seconds <= 6 * 3600:
            return "last_6_hours"
        if age_seconds <= 24 * 3600:
            return "last_24_hours"
        return "older"

    @staticmethod
    def _parse_tool_requests(raw_output: str) -> List[Dict[str, Any]]:
        parsed = LiveUpdateCandidateGenerator._parse_json_payload(raw_output)
        if not isinstance(parsed, dict):
            return []
        requests = parsed.get("tool_requests") or parsed.get("tools") or []
        if not isinstance(requests, list):
            return []
        return [request for request in requests if isinstance(request, dict) and request.get("tool")]

    @staticmethod
    async def _run_requested_tool(
        tool_runner: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]] | Dict[str, Any]],
        request: Dict[str, Any],
    ) -> Dict[str, Any]:
        tool_name = str(request.get("tool") or "")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        try:
            maybe_result = tool_runner(tool_name, args)
            result = await maybe_result if inspect.isawaitable(maybe_result) else maybe_result
            return {"tool": tool_name, "args": args, "ok": True, "result": result}
        except Exception as exc:
            return {"tool": tool_name, "args": args, "ok": False, "error": str(exc)}

    @staticmethod
    def _message_jump_url(msg: Dict[str, Any]) -> Optional[str]:
        guild_id = msg.get("guild_id")
        channel_id = msg.get("channel_id")
        message_id = msg.get("message_id")
        if not guild_id or not channel_id or not message_id:
            return None
        route_id = msg.get("thread_id") or channel_id
        return f"https://discord.com/channels/{guild_id}/{route_id}/{message_id}"

    @staticmethod
    def _reply_jump_url(msg: Dict[str, Any]) -> Optional[str]:
        guild_id = msg.get("guild_id")
        channel_id = msg.get("channel_id")
        reference_id = msg.get("reference_id")
        if not guild_id or not channel_id or not reference_id:
            return None
        route_id = msg.get("thread_id") or channel_id
        return f"https://discord.com/channels/{guild_id}/{route_id}/{reference_id}"

    def _parse_raw_candidates(self, raw_output: str) -> List[Dict[str, Any]]:
        parsed = self._parse_json_payload(raw_output)
        candidates: List[Dict[str, Any]] = []

        if isinstance(parsed, dict):
            candidates = parsed.get("candidates") or []
        elif isinstance(parsed, list):
            candidates = parsed
        else:
            candidates = []

        # ── fallback chain for editor_reasoning ──
        # Set both self.last_editor_reasoning and self.reasoning_recovery_path
        # so the editor can surface reasoning + telemetry.
        self.last_editor_reasoning = ""
        self.reasoning_recovery_path = "none"

        # 1) top-level editor_reasoning
        if isinstance(parsed, dict):
            reasoning = parsed.get("editor_reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                self.last_editor_reasoning = reasoning.strip()
                self.reasoning_recovery_path = "top_level"

        # 2) aliases
        if not self.last_editor_reasoning and isinstance(parsed, dict):
            for alias in ("reasoning", "editor_summary", "editorial_reasoning"):
                value = parsed.get(alias)
                if isinstance(value, str) and value.strip():
                    self.last_editor_reasoning = value.strip()
                    self.reasoning_recovery_path = f"alias:{alias}"
                    break

        # 3) per-candidate editor_reasoning concatenation
        if not self.last_editor_reasoning and candidates:
            per_reasonings = []
            for cand in candidates:
                if isinstance(cand, dict):
                    cr = cand.get("editor_reasoning")
                    if isinstance(cr, str) and cr.strip():
                        per_reasonings.append(cr.strip())
            if per_reasonings:
                self.last_editor_reasoning = " | ".join(per_reasonings)
                self.reasoning_recovery_path = "per_candidate"

        # 4) regex REASONING: prefix on prose prefix
        if not self.last_editor_reasoning and raw_output:
            match = re.search(r'^REASONING:\s*(.+?)(?:\n\n|\n\{)', raw_output, flags=re.MULTILINE | re.DOTALL)
            if match and match.group(1).strip():
                self.last_editor_reasoning = match.group(1).strip()
                self.reasoning_recovery_path = "reasoning_prefix"

        # 5) first ≤3 sentences before JSON span as last resort
        if not self.last_editor_reasoning and raw_output:
            text = raw_output.strip()
            # Find where JSON likely starts
            json_start = text.find("{")
            if json_start == -1:
                json_start = text.find("[")
            if json_start > 0:
                prose = text[:json_start].strip()
            else:
                prose = text
            if prose:
                # Match up to 3 sentences from the start
                sentence_match = re.match(r'^((?:[^.!?\n]+[.!?]\s*){1,3})', prose)
                if sentence_match and sentence_match.group(1).strip():
                    self.last_editor_reasoning = sentence_match.group(1).strip()
                    self.reasoning_recovery_path = "prose_first_paragraph"

        return [candidate for candidate in candidates if isinstance(candidate, dict)]

    @staticmethod
    def _compact_context_for_audit(context: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(context, dict):
            return {}
        source_context = context.get("source_context") or {}
        return {
            "recent_feed_items": (context.get("recent_feed_items") or [])[:20],
            "source_context_keys": list(source_context.keys())[:50] if isinstance(source_context, dict) else [],
            "tool_use_guidance": context.get("tool_use_guidance") or {},
        }

    @staticmethod
    def _parse_json_payload(raw_output: str) -> Any:
        text = (raw_output or "").strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                pass

        object_start = text.find("{")
        object_end = text.rfind("}")
        array_start = text.find("[")
        array_end = text.rfind("]")
        spans = []
        if object_start != -1 and object_end > object_start:
            spans.append((object_start, object_end + 1))
        if array_start != -1 and array_end > array_start:
            spans.append((array_start, array_end + 1))
        for start, end in sorted(spans, key=lambda span: span[0]):
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
        return []

    def _normalize_candidates(
        self,
        raw_candidates: List[Dict[str, Any]],
        *,
        source_messages: List[Dict[str, Any]],
        run_id: str,
        guild_id: Optional[int],
        raw_agent_output: Dict[str, Any],
        scanned_message_count: int = 0,
        is_last_call: bool = False,
    ) -> List[Dict[str, Any]]:
        source_by_id = {
            str(msg.get("message_id")): msg
            for msg in source_messages
            if msg.get("message_id") is not None
        }
        normalized: List[Dict[str, Any]] = []
        for index, candidate in enumerate(raw_candidates, start=1):
            item = self._normalize_candidate(
                candidate,
                source_by_id=source_by_id,
                run_id=run_id,
                guild_id=guild_id,
                raw_agent_output=raw_agent_output,
                fallback_index=index,
                scanned_message_count=scanned_message_count,
                is_last_call=is_last_call,
            )
            if item:
                normalized.append(item)
            if len(normalized) >= self.max_candidates:
                break
        return normalized

    def _normalize_candidate(
        self,
        candidate: Dict[str, Any],
        *,
        source_by_id: Dict[str, Dict[str, Any]],
        run_id: str,
        guild_id: Optional[int],
        raw_agent_output: Dict[str, Any],
        fallback_index: int,
        scanned_message_count: int = 0,
        is_last_call: bool = False,
    ) -> Optional[Dict[str, Any]]:
        source_message_ids = [str(item) for item in candidate.get("source_message_ids") or [] if item is not None]
        source_messages = [source_by_id[item] for item in source_message_ids if item in source_by_id]
        if not source_messages:
            return None

        body = self._clean_content(str(candidate.get("body") or candidate.get("summary") or ""))
        title = self._clean_content(str(candidate.get("title") or ""))[:96]
        media_refs = self._normalize_media_refs(candidate.get("media_refs") or [])
        if not media_refs:
            for message in source_messages:
                media_refs.extend(self._extract_media_refs(message))
        if not body and not media_refs:
            return None

        update_type = str(candidate.get("update_type") or "project_update").strip().lower()
        if update_type not in LIVE_UPDATE_TYPES:
            update_type = self._classify_update_type(body, media_refs)
        if not title:
            title = self._title_from_content(body, update_type)

        agent_decision = str(candidate.get("decision") or candidate.get("agent_decision") or "publish").strip().lower()
        if agent_decision not in {"publish", "defer", "skip", "duplicate"}:
            agent_decision = "publish"

        if not self._meets_editorial_bar(
            update_type=update_type,
            body=body,
            title=title,
            media_refs=media_refs,
            source_messages=source_messages,
            confidence=self._bounded_float(candidate.get("confidence"), default=0.55),
            scanned_message_count=scanned_message_count,
            is_last_call=is_last_call,
        ):
            return None

        author_snapshot = candidate.get("author_context_snapshot")
        if not isinstance(author_snapshot, dict) or not author_snapshot:
            author_snapshot = self._merge_author_snapshots(source_messages)

        duplicate_key = str(candidate.get("duplicate_key") or "").strip()
        if not duplicate_key:
            duplicate_key = self._duplicate_key(
                guild_id=guild_id,
                update_type=update_type,
                title=title,
                source_message_ids=source_message_ids,
                content=body,
            )

        examples = self._normalize_examples(
            candidate.get("examples"),
            source_by_id=source_by_id,
        )

        return {
            "run_id": run_id,
            "guild_id": guild_id,
            "source_channel_id": self._first_non_empty(source_messages, "channel_id"),
            "source_thread_id": self._first_non_empty(source_messages, "thread_id"),
            "update_type": update_type,
            "title": title,
            "body": body or "Shared new media.",
            "media_refs": media_refs,
            "examples": examples,
            "source_message_ids": source_message_ids,
            "author_context_snapshot": author_snapshot,
            "duplicate_key": duplicate_key,
            "agent_decision": agent_decision,
            "confidence": self._bounded_float(candidate.get("confidence"), default=0.55),
            "priority": self._bounded_int(candidate.get("priority"), default=1),
            "rationale": self._clean_content(str(candidate.get("rationale") or "Generated by live-update editor.")),
            "raw_agent_output": {
                **raw_agent_output,
                "candidate_index": fallback_index,
                "raw_candidate": candidate,
                "editorial_decision": {
                    "decision": agent_decision,
                    "evidence_message_ids": candidate.get("evidence_message_ids") or candidate.get("source_message_ids") or [],
                    "new_information": candidate.get("new_information"),
                    "why_now": candidate.get("why_now"),
                    "duplicate_assessment": candidate.get("duplicate_assessment"),
                    "context_used": candidate.get("context_used"),
                    "community_validation": candidate.get("community_validation"),
                    "uncertainty": candidate.get("uncertainty"),
                    "risk_flags": candidate.get("risk_flags") or [],
                    "defer_until_or_condition": candidate.get("defer_until_or_condition"),
                    "editor_notes": candidate.get("editor_notes"),
                    "editorial_checklist": candidate.get("editorial_checklist") or {},
                },
            },
        }

    def _generate_heuristic_candidates(
        self,
        *,
        messages: List[Dict[str, Any]],
        run_id: str,
        guild_id: Optional[int],
        memory: List[Dict[str, Any]],
        watchlist: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        memory_keys = {row.get("memory_key") for row in memory or []}
        for message in messages:
            if len(candidates) >= self.max_candidates:
                break
            content = self._clean_content(message.get("content") or "")
            media_refs = self._extract_media_refs(message)
            if not content and not media_refs:
                continue

            update_type = self._classify_update_type(content, media_refs)
            if not self._message_meets_editorial_bar(
                message=message,
                update_type=update_type,
                content=content,
                media_refs=media_refs,
                scanned_message_count=len(messages),
            ):
                continue
            title = self._title_from_content(content, update_type)
            source_ids = [str(message.get("message_id"))] if message.get("message_id") is not None else []
            duplicate_key = self._duplicate_key(
                guild_id=guild_id,
                update_type=update_type,
                title=title,
                source_message_ids=source_ids,
                content=content,
            )
            confidence = self._confidence_for_message(content, media_refs, watchlist)
            priority = self._priority_for_message(content, media_refs, duplicate_key in memory_keys)

            candidates.append({
                "run_id": run_id,
                "guild_id": guild_id,
                "source_channel_id": message.get("channel_id"),
                "source_thread_id": message.get("thread_id"),
                "update_type": update_type,
                "title": title,
                "body": content or "Shared new media.",
                "media_refs": media_refs,
                "source_message_ids": source_ids,
                "author_context_snapshot": message.get("author_context_snapshot") or {},
                "duplicate_key": duplicate_key,
                "confidence": confidence,
                "priority": priority,
                "rationale": self._candidate_rationale(update_type, confidence, media_refs),
                "raw_agent_output": {
                    "generator": "heuristic_live_update_editor_v1",
                    "source_message_id": message.get("message_id"),
                    "created_at": message.get("created_at"),
                    "source_message": {
                        "channel_id": message.get("channel_id"),
                        "author_id": message.get("author_id"),
                        "content": content,
                    },
                },
            })
        return candidates

    @staticmethod
    def _clean_content(content: str) -> str:
        return re.sub(r"\s+", " ", content or "").strip()

    @staticmethod
    def _jsonish(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _render_watchlist(watchlist: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        """Render watchlist as state-grouped structure with schema-correct fields.

        Computes ``age_hours`` from each row's ``created_at``, and falls back
        to ``evidence.source_message_ids`` if ``source_message_ids`` is absent.
        """
        def _age_hours(row: Dict[str, Any]) -> Optional[float]:
            created_at = row.get("created_at")
            if not created_at:
                return None
            try:
                dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return round((now - dt).total_seconds() / 3600, 1)
            except (ValueError, TypeError):
                return None

        def _render_state_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            result: List[Dict[str, Any]] = []
            for row in (rows or [])[:20]:
                evidence = row.get("evidence") or {}
                if isinstance(evidence, str):
                    try:
                        evidence = json.loads(evidence)
                    except json.JSONDecodeError:
                        evidence = {}
                result.append({
                    "watch_key": row.get("watch_key"),
                    "title": row.get("title"),
                    "origin_reason": row.get("origin_reason"),
                    "age_hours": _age_hours(row),
                    "source_message_ids": LiveUpdateCandidateGenerator._jsonish(
                        row.get("source_message_ids")
                        or evidence.get("source_message_ids")
                        or []
                    ),
                    "subject_type": row.get("subject_type"),
                    "channel_id": row.get("channel_id"),
                    "notes": row.get("notes"),
                })
            return result

        if not isinstance(watchlist, dict):
            return {
                "_explanation": "No watchlist data.",
                "fresh": [],
                "revisit_due": [],
                "last_call": [],
            }

        return {
            "_explanation": (
                "Items you previously flagged as promising-but-unclear. "
                "Decide per item: keep watching (do nothing), "
                "watchlist_update(action='publish_now') when new signal arrived, "
                "watchlist_update(action='extend') if still developing, or "
                "watchlist_update(action='discard') if dead. "
                "Items in last_call will auto-discard after 72h if you don't act. "
                "To ADD a new entry (promising-but-unverified items from this run), "
                "call watchlist_add with {watch_key, title, reason, source_message_ids, "
                "channel_id, subject_type}. See WATCHLIST WORKFLOW in the system prompt."
            ),
            "fresh": _render_state_rows(watchlist.get("fresh") or []),
            "revisit_due": _render_state_rows(watchlist.get("revisit_due") or []),
            "last_call": _render_state_rows(watchlist.get("last_call") or []),
        }

    @classmethod
    def _extract_media_refs(cls, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        for field in ("attachments", "embeds"):
            raw_items = cls._jsonish(message.get(field) or [])
            if not isinstance(raw_items, list):
                raw_items = []
            for item in raw_items:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("proxy_url")
                    if url:
                        refs.append({
                            "kind": field[:-1],
                            "url": url,
                            "content_type": item.get("content_type") or item.get("type"),
                            "filename": item.get("filename"),
                        })
        return refs

    @classmethod
    def _normalize_examples(
        cls,
        raw_examples: Any,
        *,
        source_by_id: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw_examples, list):
            return []
        cleaned: List[Dict[str, Any]] = []
        for item in raw_examples[:5]:
            if not isinstance(item, dict):
                continue
            caption = cls._clean_content(str(item.get("caption") or "")).strip()
            caption = caption.rstrip(".:") [:140]
            source_id_raw = item.get("source_message_id") or item.get("message_id")
            source_id = str(source_id_raw).strip() if source_id_raw is not None else ""
            urls_raw = item.get("media_urls") or item.get("urls") or []
            if isinstance(urls_raw, str):
                urls_raw = [urls_raw]
            urls: List[str] = []
            if isinstance(urls_raw, list):
                for u in urls_raw[:4]:
                    if isinstance(u, str) and u.strip():
                        u_clean = u.strip()
                        if u_clean not in urls:
                            urls.append(u_clean)
            # If urls absent but source_id resolves, fall back to that message's media
            if not urls and source_id and source_id in source_by_id:
                refs = cls._extract_media_refs(source_by_id[source_id])
                urls = [r.get("url") for r in refs if r.get("url")][:4]
            if not caption and not urls:
                continue
            # Cross-channel support: each example may specify its own channel/thread
            # for the jump-link. Fall back to the candidate's primary channel when
            # the example doesn't override it.
            chan_raw = item.get("source_channel_id") or item.get("channel_id")
            thr_raw = item.get("source_thread_id") or item.get("thread_id")
            try:
                source_channel_id = int(chan_raw) if chan_raw is not None and str(chan_raw).strip() else None
            except (TypeError, ValueError):
                source_channel_id = None
            try:
                source_thread_id = int(thr_raw) if thr_raw is not None and str(thr_raw).strip() else None
            except (TypeError, ValueError):
                source_thread_id = None
            # If model omitted channel but source resolves, use the source's channel.
            if source_channel_id is None and source_id and source_id in source_by_id:
                src_chan = source_by_id[source_id].get("channel_id")
                try:
                    source_channel_id = int(src_chan) if src_chan is not None else None
                except (TypeError, ValueError):
                    pass
            cleaned.append({
                "caption": caption,
                "source_message_id": source_id or None,
                "source_channel_id": source_channel_id,
                "source_thread_id": source_thread_id,
                "media_urls": urls,
            })
        return cleaned

    @staticmethod
    def _normalize_media_refs(raw_refs: Any) -> List[Dict[str, Any]]:
        if isinstance(raw_refs, str):
            try:
                raw_refs = json.loads(raw_refs)
            except json.JSONDecodeError:
                raw_refs = []
        if not isinstance(raw_refs, list):
            return []
        refs: List[Dict[str, Any]] = []
        for item in raw_refs:
            if isinstance(item, dict):
                url = item.get("url") or item.get("proxy_url")
                if url:
                    refs.append({
                        "kind": item.get("kind") or item.get("type"),
                        "url": url,
                        "content_type": item.get("content_type"),
                        "filename": item.get("filename"),
                    })
        return refs

    @classmethod
    def _meets_editorial_bar(
        cls,
        *,
        update_type: str,
        body: str,
        title: str,
        media_refs: List[Dict[str, Any]],
        source_messages: List[Dict[str, Any]],
        confidence: float,
        scanned_message_count: int = 0,
        is_last_call: bool = False,
    ) -> bool:
        combined_text = cls._clean_content(" ".join(
            [title, body] + [str(msg.get("content") or "") for msg in source_messages]
        ))
        if not combined_text and not media_refs:
            return False
        if cls._is_low_signal_text(combined_text) and confidence < 0.9:
            return False

        reactions = max((cls._message_reaction_count(msg) for msg in source_messages), default=0)
        reply_count = max((cls._message_reply_count(msg) for msg in source_messages), default=0)
        has_news_term = cls._has_newsworthy_term(combined_text)
        has_media = bool(media_refs)
        text_len = len(combined_text)
        # author_is_high_signal not yet implemented on candidate payloads; degrade to False
        author_is_high_signal = False
        quiet_hour = scanned_message_count < 50

        if update_type in {"release", "milestone", "event"}:
            return cls._has_concrete_artifact_signal(combined_text) and (text_len >= 45 or reactions >= 2 or has_media)
        if update_type == "showcase":
            if is_last_call:
                return has_media and (reactions >= 2 or reply_count >= 1)
            if quiet_hour:
                return has_media and (reactions >= 2 or reply_count >= 1)
            return has_media and (reactions >= 3 or reply_count >= 2 or author_is_high_signal)
        if update_type == "top_creation":
            if is_last_call:
                return has_media and (reactions >= 2 or reply_count >= 1)
            if quiet_hour:
                return has_media and reactions >= 2
            return has_media and reactions >= 3
        if update_type in {"question", "request"}:
            return has_news_term and (reactions >= 5 or reply_count >= 3) and text_len >= 45
        if update_type == "project_update":
            if is_last_call:
                return reactions >= 1 or reply_count >= 1
            return cls._has_concrete_artifact_signal(combined_text) and (reactions >= 3 or reply_count >= 2 or has_media)
        # fallback for 'other'
        if is_last_call:
            return reactions >= 2 or has_media
        return cls._has_concrete_artifact_signal(combined_text) and (reactions >= 3 or has_media)

    @classmethod
    def _message_meets_editorial_bar(
        cls,
        *,
        message: Dict[str, Any],
        update_type: str,
        content: str,
        media_refs: List[Dict[str, Any]],
        scanned_message_count: int = 0,
        is_last_call: bool = False,
    ) -> bool:
        return cls._meets_editorial_bar(
            update_type=update_type,
            body=content,
            title="",
            media_refs=media_refs,
            source_messages=[message],
            confidence=cls._confidence_for_message(content, media_refs, []),
            scanned_message_count=scanned_message_count,
            is_last_call=is_last_call,
        )

    @classmethod
    def _has_newsworthy_term(cls, text: str) -> bool:
        lower = text.lower()
        return any(term in lower for term in cls.NEWSWORTHY_TERMS)

    @classmethod
    def _has_concrete_artifact_signal(cls, text: str) -> bool:
        lower = text.lower()
        has_action = any(term in lower for term in cls.CONCRETE_ACTION_TERMS)
        has_artifact = any(term in lower for term in cls.ARTIFACT_TERMS)
        has_link = "http://" in lower or "https://" in lower or "github.com" in lower
        return has_action and (has_artifact or has_link)

    @classmethod
    def _is_low_signal_text(cls, text: str) -> bool:
        lower = f" {text.lower()} "
        return any(pattern in lower for pattern in cls.LOW_SIGNAL_PATTERNS)

    @staticmethod
    def _message_reaction_count(message: Dict[str, Any]) -> int:
        for key in ("reaction_count", "unique_reactor_count", "reactions"):
            try:
                return max(0, int(message.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _message_reply_count(message: Dict[str, Any]) -> int:
        for key in ("reply_count", "thread_reply_count", "comment_count"):
            try:
                return max(0, int(message.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _classify_update_type(content: str, media_refs: List[Dict[str, Any]]) -> str:
        lower = content.lower()
        if any(token in lower for token in ("launch", "released", "release", "shipping", "shipped")):
            return "release"
        if any(token in lower for token in ("milestone", "done", "completed", "merged")):
            return "milestone"
        if any(token in lower for token in ("event", "meetup", "workshop", "livestream")):
            return "event"
        if "?" in content:
            return "question"
        if any(token in lower for token in ("help", "looking for", "request", "need feedback", "needs testers")):
            return "request"
        if media_refs:
            return "showcase"
        return "project_update"

    @staticmethod
    def _title_from_content(content: str, update_type: str) -> str:
        if not content:
            return update_type.replace("_", " ").title()
        first_sentence = re.split(r"(?<=[.!?])\s+", content, maxsplit=1)[0]
        return first_sentence[:96].strip() or update_type.replace("_", " ").title()

    @staticmethod
    def _duplicate_key(
        *,
        guild_id: Optional[int],
        update_type: str,
        title: str,
        source_message_ids: List[str],
        content: str,
    ) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", f"{title} {content}".lower()).strip()
        seed = "|".join([
            str(guild_id or ""),
            update_type,
            ",".join(source_message_ids),
            normalized[:500],
        ])
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _confidence_for_message(
        content: str,
        media_refs: List[Dict[str, Any]],
        watchlist: List[Dict[str, Any]],
    ) -> float:
        score = 0.35
        if len(content) >= 80:
            score += 0.2
        if media_refs:
            score += 0.2
        lower = content.lower()
        if isinstance(watchlist, dict):
            wl_iter = (
                list(watchlist.get("fresh") or [])
                + list(watchlist.get("revisit_due") or [])
                + list(watchlist.get("last_call") or [])
            )
        else:
            wl_iter = list(watchlist or [])
        if any((watch.get("watch_key") or "").lower() in lower for watch in wl_iter):
            score += 0.15
        if any(token in lower for token in ("launch", "release", "shipped", "demo", "new", "built")):
            score += 0.1
        return min(score, 0.95)

    @staticmethod
    def _priority_for_message(content: str, media_refs: List[Dict[str, Any]], seen_in_memory: bool) -> int:
        priority = 1
        if media_refs:
            priority += 1
        if any(token in content.lower() for token in ("launch", "release", "shipped", "milestone")):
            priority += 2
        if seen_in_memory:
            priority -= 1
        return max(priority, 0)

    @staticmethod
    def _candidate_rationale(update_type: str, confidence: float, media_refs: List[Dict[str, Any]]) -> str:
        media_note = "with media" if media_refs else "text-only"
        return f"Classified as {update_type} from archived message content ({media_note}, confidence={confidence:.2f})."

    @staticmethod
    def _merge_author_snapshots(source_messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        snapshots = [
            msg.get("author_context_snapshot")
            for msg in source_messages
            if isinstance(msg.get("author_context_snapshot"), dict)
        ]
        if not snapshots:
            return {}
        if len(snapshots) == 1:
            return snapshots[0]
        return {"sources": snapshots}

    @staticmethod
    def _first_non_empty(messages: List[Dict[str, Any]], field: str) -> Any:
        for message in messages:
            if message.get(field) is not None:
                return message.get(field)
        return None

    @staticmethod
    def _bounded_float(value: Any, *, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_int(value: Any, *, default: int) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default
