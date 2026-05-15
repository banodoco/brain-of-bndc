"""Live Update Social tool registry — Sprint 1 terminal decision tools.

Exactly three tools are registered::

    draft_social_post     — set terminal_status='draft' with draft_text
    skip_social_post      — set terminal_status='skip'
    request_social_review — set terminal_status='needs_review'

Each tool has exactly one handler binding.  Handlers update the
live_update_social_runs row durably and record trace entries.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .models import ToolSpec, ToolBinding, RunState

if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")

# ── tool definitions ──────────────────────────────────────────────────

TOOL_DRAFT_SOCIAL_POST = ToolSpec(
    name="draft_social_post",
    description=(
        "Record a draft social media post for human review. "
        "Use this when content and media are ready for review. "
        "The draft text and selected media identities will be persisted "
        "for inspection."
    ),
    parameters={
        "type": "object",
        "properties": {
            "draft_text": {
                "type": "string",
                "description": "The draft social media post text.",
            },
            "selected_media": {
                "type": "array",
                "description": (
                    "List of selected media identities "
                    "(channel_id, message_id, attachment_index or embed_slot)."
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["draft_text"],
    },
)

TOOL_SKIP_SOCIAL_POST = ToolSpec(
    name="skip_social_post",
    description=(
        "Skip social posting for this topic. Use this when the content "
        "should not be posted to social media (e.g., not newsworthy, "
        "duplicate, or intentionally excluded)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason for skipping.",
            },
        },
        "required": [],
    },
)

TOOL_REQUEST_SOCIAL_REVIEW = ToolSpec(
    name="request_social_review",
    description=(
        "Request human review for this social post. Use this when you "
        "cannot make a confident decision — e.g., media URLs could not "
        "be resolved, route configuration is missing, or the content "
        "needs human judgment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why human review is needed.",
            },
        },
        "required": ["reason"],
    },
)

ALL_TOOL_SPECS: List[ToolSpec] = [
    TOOL_DRAFT_SOCIAL_POST,
    TOOL_SKIP_SOCIAL_POST,
    TOOL_REQUEST_SOCIAL_REVIEW,
]

# ── handler factories ─────────────────────────────────────────────────
# Each returns an async callable that updates the run and returns a result dict.


def _make_draft_handler(db_handler: "DatabaseHandler") -> Any:
    async def handler(run_state: RunState, params: dict) -> dict:
        draft_text = params.get("draft_text", "")
        selected_media = params.get("selected_media", [])

        # Build media_decisions
        decisions = run_state.media_decisions or {}
        decisions["selected"] = selected_media
        decisions.setdefault("considered", [])
        decisions.setdefault("skipped", [])
        decisions.setdefault("unresolved", [])

        run_state.terminal_status = "draft"
        run_state.draft_text = draft_text
        run_state.media_decisions = decisions
        run_state.add_trace("tool", tool="draft_social_post", draft_len=len(draft_text))

        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="draft",
            draft_text=draft_text,
            media_decisions=decisions,
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error("draft_social_post: DB update failed for run %s", run_state.run_id)
        return {"tool": "draft_social_post", "terminal_status": "draft", "ok": ok}

    return handler


def _make_skip_handler(db_handler: "DatabaseHandler") -> Any:
    async def handler(run_state: RunState, params: dict) -> dict:
        reason = params.get("reason", "no reason given")

        run_state.terminal_status = "skip"
        run_state.add_trace("tool", tool="skip_social_post", reason=reason)

        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="skip",
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error("skip_social_post: DB update failed for run %s", run_state.run_id)
        return {"tool": "skip_social_post", "terminal_status": "skip", "reason": reason, "ok": ok}

    return handler


def _make_request_review_handler(db_handler: "DatabaseHandler") -> Any:
    async def handler(run_state: RunState, params: dict) -> dict:
        reason = params.get("reason", "")

        run_state.terminal_status = "needs_review"
        run_state.add_trace("tool", tool="request_social_review", reason=reason)

        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="needs_review",
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error("request_social_review: DB update failed for run %s", run_state.run_id)
        return {"tool": "request_social_review", "terminal_status": "needs_review", "reason": reason, "ok": ok}

    return handler


def build_tool_bindings(db_handler: "DatabaseHandler") -> List[ToolBinding]:
    """Return the three terminal tool bindings for Sprint 1.

    Each ToolBinding pairs a ToolSpec with its async handler.
    """
    return [
        ToolBinding(
            tool_spec=TOOL_DRAFT_SOCIAL_POST,
            handler=_make_draft_handler(db_handler),
        ),
        ToolBinding(
            tool_spec=TOOL_SKIP_SOCIAL_POST,
            handler=_make_skip_handler(db_handler),
        ),
        ToolBinding(
            tool_spec=TOOL_REQUEST_SOCIAL_REVIEW,
            handler=_make_request_review_handler(db_handler),
        ),
    ]


def get_tool_by_name(bindings: List[ToolBinding], name: str) -> Optional[ToolBinding]:
    """Find a ToolBinding by name."""
    for b in bindings:
        if b.name == name:
            return b
    return None
