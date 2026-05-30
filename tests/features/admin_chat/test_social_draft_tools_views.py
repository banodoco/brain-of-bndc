"""View / read-path tests for social-draft review executors (T18).

Covers:
  1. preview_social_draft: shape includes topic_title from topic_summary_data.title.
  2. list_pending_social_drafts: returns drafted-but-unapproved runs
     (terminal_status='draft', approval_state != 'expired').
  3. discard_social_draft: confirmation_required gate + confirm path.
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.features.admin_chat.tools import (
    execute_preview_social_draft,
    execute_list_pending_social_drafts,
    execute_discard_social_draft,
)


# ======================================================================
#  In-memory fake db_handler (extended with list_pending + discard)
# ======================================================================

class _FakeDB:
    def __init__(self, row: dict | None = None, pending_rows: list[dict] | None = None):
        self._row = dict(row) if row else None
        self._pending_rows = list(pending_rows) if pending_rows else []
        self.update_calls: list[dict] = []

    def get_live_update_social_run(self, run_id: str) -> dict | None:
        if self._row is None:
            return None
        return copy.deepcopy(self._row)

    def update_live_update_social_run(self, run_id: str, *, clear_approval: bool = False, **kwargs) -> bool:
        self.update_calls.append({"run_id": run_id, "clear_approval": clear_approval, "fields": dict(kwargs)})
        if self._row is not None:
            for k, v in kwargs.items():
                self._row[k] = v
            if clear_approval:
                self._row["approved_revision"] = None
                self._row["approved_text"] = None
                self._row["approved_quote"] = None
                self._row["approved_at"] = None
        return True

    def list_pending_review_social_runs(self, environment: str | None = None) -> list[dict]:
        # Simulate the real query's filtering: terminal_status='draft' AND approval_state != 'expired'
        return [
            copy.deepcopy(r) for r in self._pending_rows
            if r.get("terminal_status") == "draft" and r.get("approval_state") != "expired"
        ]

    def get_social_run_by_review_message_id(self, review_message_id: int, environment: str = "prod") -> dict | None:
        return None


def _make_bot():
    bot = MagicMock()
    bot.fetch_user = AsyncMock()
    return bot


def _base_row(**overrides) -> dict:
    row: dict = {
        "run_id": "run-view-1",
        "topic_id": "topic-view",
        "platform": "twitter",
        "action": "post",
        "mode": "draft",
        "terminal_status": "draft",
        "guild_id": 1,
        "channel_id": 10,
        "draft_text": "A viewable draft.",
        "media_decisions": {
            "selected": [{"url": "https://x.com/img.png", "title": "media one"}],
            "considered": [],
            "skipped": [],
            "unresolved": [],
        },
        "revision": 1,
        "approval_state": "pending",
        "approved_revision": None,
        "approved_text": None,
        "approved_quote": None,
        "approved_at": None,
        "expires_at": "2026-06-01T00:00:00+00:00",
        "publish_revision": None,
        "topic_summary_data": {"title": "Preview Topic Title"},
        "source_metadata": {},
        "trace_entries": [],
        "created_at": "2026-05-30T00:00:00+00:00",
        "updated_at": "2026-05-30T00:00:00+00:00",
    }
    row.update(overrides)
    return row


# ======================================================================
#  1. preview_social_draft
# ======================================================================

class TestPreviewSocialDraft:
    @pytest.mark.asyncio
    async def test_returns_expected_shape_with_topic_title(self):
        row = _base_row()
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_preview_social_draft(bot, db, {"run_id": "run-view-1"})

        assert result["success"] is True
        assert result["run_id"] == "run-view-1"
        assert result["draft_text"] == "A viewable draft."
        assert result["revision"] == 1
        assert result["approval_state"] == "pending"
        assert result["approved_revision"] is None
        assert result["expires_at"] == "2026-06-01T00:00:00+00:00"
        assert result["topic_title"] == "Preview Topic Title"
        assert result["media"]["count"] == 1
        assert result["media"]["first_url_or_title"] == "https://x.com/img.png"

    @pytest.mark.asyncio
    async def test_topic_title_from_topic_summary_data(self):
        row = _base_row(topic_summary_data={"title": "Actual Title"})
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_preview_social_draft(bot, db, {"run_id": "run-view-1"})
        assert result["topic_title"] == "Actual Title"

    @pytest.mark.asyncio
    async def test_missing_topic_summary_data_title_is_none(self):
        row = _base_row(topic_summary_data={})
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_preview_social_draft(bot, db, {"run_id": "run-view-1"})
        assert result["topic_title"] is None


# ======================================================================
#  2. list_pending_social_drafts
# ======================================================================

class TestListPendingSocialDrafts:
    @pytest.mark.asyncio
    async def test_returns_drafted_but_unapproved_runs(self):
        # Seed two pending-review rows that would be excluded by
        # list_open_social_runs (which filters terminal_status IS NULL).
        pending = [
            {
                "run_id": "run-a",
                "terminal_status": "draft",
                "approval_state": "pending",
                "draft_text": "draft A",
                "revision": 1,
                "topic_summary_data": {"title": "Topic A"},
                "expires_at": None,
                "created_at": "2026-05-29T00:00:00+00:00",
            },
            {
                "run_id": "run-b",
                "terminal_status": "draft",
                "approval_state": "approved",
                "draft_text": "draft B",
                "revision": 2,
                "topic_summary_data": {"title": "Topic B"},
                "expires_at": None,
                "created_at": "2026-05-28T00:00:00+00:00",
            },
        ]
        db = _FakeDB(pending_rows=pending)
        bot = _make_bot()

        result = await execute_list_pending_social_drafts(bot, db, {})

        assert result["success"] is True
        # Both rows have terminal_status='draft' and approval_state != 'expired'
        assert result["count"] == 2
        assert len(result["drafts"]) == 2
        run_ids = {d["run_id"] for d in result["drafts"]}
        assert run_ids == {"run-a", "run-b"}

    @pytest.mark.asyncio
    async def test_excludes_expired_approval_state(self):
        pending = [
            {
                "run_id": "run-expired",
                "terminal_status": "draft",
                "approval_state": "expired",
                "draft_text": "expired draft",
                "revision": 1,
                "topic_summary_data": {},
                "expires_at": None,
                "created_at": "2026-05-29T00:00:00+00:00",
            },
            {
                "run_id": "run-active",
                "terminal_status": "draft",
                "approval_state": "pending",
                "draft_text": "active",
                "revision": 1,
                "topic_summary_data": {"title": "Active"},
                "expires_at": None,
                "created_at": "2026-05-28T00:00:00+00:00",
            },
        ]
        db = _FakeDB(pending_rows=pending)
        bot = _make_bot()

        result = await execute_list_pending_social_drafts(bot, db, {})
        # The expired row should be excluded
        assert result["count"] == 1
        assert result["drafts"][0]["run_id"] == "run-active"


# ======================================================================
#  3. discard_social_draft
# ======================================================================

class TestDiscardSocialDraft:
    @pytest.mark.asyncio
    async def test_requires_confirmation_by_default(self):
        row = _base_row()
        db = _FakeDB(row)
        bot = _make_bot()

        # No confirm token → confirmation_required
        result = await execute_discard_social_draft(
            bot, db,
            {"run_id": "run-view-1", "reason": "not needed"},
        )
        assert result["success"] is False
        assert result["code"] == "confirmation_required"

        # Row should be unchanged
        current = db.get_live_update_social_run("run-view-1")
        assert current["approval_state"] == "pending"

    @pytest.mark.asyncio
    async def test_with_confirm_token_sets_skip_and_expired(self):
        row = _base_row()
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_discard_social_draft(
            bot, db,
            {"run_id": "run-view-1", "reason": "stale", "confirm": "discard"},
        )
        assert result["success"] is True
        assert result["run_id"] == "run-view-1"

        # Verify the row was updated
        current = db.get_live_update_social_run("run-view-1")
        assert current["approval_state"] == "expired"
        assert current["terminal_status"] == "skip"

        # Reason stored in trace_entries
        discard_entries = [
            e for e in current["trace_entries"]
            if e.get("tool") == "discard_social_draft"
        ]
        assert len(discard_entries) >= 1
        assert discard_entries[0].get("discard_reason") == "stale"

    @pytest.mark.asyncio
    async def test_require_confirmation_false_bypasses_gate(self):
        row = _base_row()
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_discard_social_draft(
            bot, db,
            {"run_id": "run-view-1", "require_confirmation": False, "reason": "force"},
        )
        assert result["success"] is True
        current = db.get_live_update_social_run("run-view-1")
        assert current["approval_state"] == "expired"
        assert current["terminal_status"] == "skip"
