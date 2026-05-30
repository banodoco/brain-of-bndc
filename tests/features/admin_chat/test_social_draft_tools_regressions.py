"""Regression tests for social-draft review executors (T17).

Covers:
  1. Edit-after-approve → publish refuses with 'no_approval'.
  2. All 5 publish refusal codes never invoke social_publish_service.
  3. TOCTOU: edit between stamp and re-read → 'text_changed'.
  4. Out-of-context quote → warning attached but approval still recorded.
  5. Empty-draft approve → 'empty_draft' before canonicalisation.
"""

from __future__ import annotations

import copy
import os as _os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.features.admin_chat.tools import (
    execute_update_social_draft,
    execute_approve_social_draft,
    execute_publish_social_draft,
    _canon,
)


# ======================================================================
#  In-memory fake db_handler (same shape as T16)
# ======================================================================

class _FakeDB:
    def __init__(self, row: dict | None = None):
        self._row = dict(row) if row else None
        self.update_calls: list[dict] = []
        # For TOCTOU: if set, .get_live_update_social_run() calls this hook
        # right before returning, letting us mutate the row mid-flight.
        self._get_hook = None

    def get_live_update_social_run(self, run_id: str) -> dict | None:
        if self._row is None:
            return None
        row = copy.deepcopy(self._row)
        # Hook receives the copy so it can mutate what the caller sees.
        if self._get_hook:
            self._get_hook(row)
        # Mirror hook mutations back into _row for consistency.
        if self._get_hook and row is not None:
            self._row.update(row)
        return row

    def update_live_update_social_run(self, run_id: str, *, clear_approval: bool = False, **kwargs) -> bool:
        self.update_calls.append({"run_id": run_id, "clear_approval": clear_approval, "fields": dict(kwargs)})
        if self._row is None:
            self._row = {}
        for k, v in kwargs.items():
            self._row[k] = v
        if clear_approval:
            self._row["approved_revision"] = None
            self._row["approved_text"] = None
            self._row["approved_quote"] = None
            self._row["approved_at"] = None
        return True

    def get_social_run_by_review_message_id(self, review_message_id: int, environment: str = "prod") -> dict | None:
        return None

    def list_pending_review_social_runs(self, environment: str | None = None) -> list[dict]:
        return []


# ======================================================================
#  Fake bot (with optional social_publish_service + fetch_user)
# ======================================================================

def _make_bot(*, social_publish_service=None, fetch_user_returns=None):
    bot = MagicMock()
    bot.social_publish_service = social_publish_service
    if fetch_user_returns is not None:
        bot.fetch_user = AsyncMock(return_value=fetch_user_returns)
    else:
        bot.fetch_user = AsyncMock()
    return bot


def _make_dm_user(dm_history=None):
    user = MagicMock()
    dm_channel = MagicMock()

    async def _history(limit=20):
        for msg in (dm_history or []):
            yield msg

    dm_channel.history = _history
    user.dm_channel = dm_channel
    user.create_dm = AsyncMock(return_value=dm_channel)
    return user


def _make_dm_message(content: str):
    msg = MagicMock()
    msg.content = content
    return msg


def _base_row(**overrides) -> dict:
    row: dict = {
        "run_id": "run-reg-1",
        "topic_id": "topic-1",
        "platform": "twitter",
        "action": "post",
        "mode": "draft",
        "terminal_status": "draft",
        "guild_id": 1,
        "channel_id": 10,
        "draft_text": "Regression draft text here.",
        "media_decisions": {"selected": [], "considered": [], "skipped": [], "unresolved": []},
        "revision": 1,
        "approval_state": "pending",
        "approved_revision": None,
        "approved_text": None,
        "approved_quote": None,
        "approved_at": None,
        "expires_at": None,
        "publish_revision": None,
        "topic_summary_data": {"title": "Reg Topic"},
        "source_metadata": {},
        "trace_entries": [],
        "created_at": "2026-05-30T00:00:00+00:00",
        "updated_at": "2026-05-30T00:00:00+00:00",
    }
    row.update(overrides)
    return row


# ======================================================================
#  1. Edit-after-approve → 'no_approval'
# ======================================================================

class TestEditAfterApprove:
    @pytest.mark.asyncio
    async def test_publish_refuses_no_approval_after_edit(self):
        # Start at rev=2, approved
        row = _base_row(
            revision=2,
            approval_state="approved",
            approved_revision=2,
            approved_text=_canon("Approved text v2"),
            draft_text="Approved text v2",
        )
        db = _FakeDB(row)
        user = _make_dm_user(dm_history=[_make_dm_message("approve plz")])
        bot = _make_bot(fetch_user_returns=user, social_publish_service=MagicMock())

        # First approve at rev=2 (just to set up the state — though row is already approved)
        with patch.dict("os.environ", {"ADMIN_USER_ID": "999"}):
            appr = await execute_approve_social_draft(
                bot, db,
                {"run_id": "run-reg-1", "admin_approval_quote": "approve plz"},
            )
        assert appr["success"] is True

        # Now edit — bumps rev=3, clears approval
        edit = await execute_update_social_draft(
            bot, db,
            {"run_id": "run-reg-1", "new_text": "Edited text v3"},
        )
        assert edit["success"] is True
        assert edit["revision"] == 3

        # Publish should now refuse with 'no_approval'
        pub = await execute_publish_social_draft(
            bot, db,
            {"run_id": "run-reg-1"},
        )
        assert pub["success"] is False
        assert pub["code"] == "no_approval"
        # social_publish_service should never have been invoked
        bot.social_publish_service.publish_now.assert_not_called()


# ======================================================================
#  2. All publish refusal codes — never invoke social_publish_service
# ======================================================================

class TestPublishRefusalCodes:
    @pytest.mark.asyncio
    async def test_service_unavailable(self):
        row = _base_row()
        db = _FakeDB(row)
        bot = _make_bot(social_publish_service=None)  # no service

        result = await execute_publish_social_draft(bot, db, {"run_id": "run-reg-1"})
        assert result["success"] is False
        assert result["code"] == "service_unavailable"

    @pytest.mark.asyncio
    async def test_no_approval(self):
        row = _base_row(approval_state="pending", approved_revision=None)
        db = _FakeDB(row)
        svc = MagicMock()
        bot = _make_bot(social_publish_service=svc)

        result = await execute_publish_social_draft(bot, db, {"run_id": "run-reg-1"})
        assert result["success"] is False
        assert result["code"] == "no_approval"
        svc.publish_now.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_revision(self):
        row = _base_row(revision=5, approval_state="approved", approved_revision=3, approved_text=_canon("OK"), draft_text="OK")
        db = _FakeDB(row)
        svc = MagicMock()
        bot = _make_bot(social_publish_service=svc)

        result = await execute_publish_social_draft(bot, db, {"run_id": "run-reg-1"})
        assert result["success"] is False
        assert result["code"] == "stale_revision"
        svc.publish_now.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_changed(self):
        row = _base_row(revision=2, approval_state="approved", approved_revision=2,
                        approved_text=_canon("Original"), draft_text="Changed")
        db = _FakeDB(row)
        svc = MagicMock()
        bot = _make_bot(social_publish_service=svc)

        result = await execute_publish_social_draft(bot, db, {"run_id": "run-reg-1"})
        assert result["success"] is False
        assert result["code"] == "text_changed"
        svc.publish_now.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_published(self):
        row = _base_row(terminal_status="published")
        db = _FakeDB(row)
        svc = MagicMock()
        bot = _make_bot(social_publish_service=svc)

        result = await execute_publish_social_draft(bot, db, {"run_id": "run-reg-1"})
        assert result["success"] is False
        assert result["code"] == "already_published"
        svc.publish_now.assert_not_called()


# ======================================================================
#  3. TOCTOU — edit lands between stamp and re-read
# ======================================================================

class TestTOCTOU:
    @pytest.mark.asyncio
    async def test_text_changed_during_toctou_window(self):
        # Row is fully approved and aligned
        row = _base_row(
            revision=3,
            approval_state="approved",
            approved_revision=3,
            approved_text=_canon("Good text"),
            draft_text="Good text",
        )
        db = _FakeDB(row)
        svc = MagicMock()
        bot = _make_bot(social_publish_service=svc)

        # Install a hook that fires *after* the publish_revision stamp write
        # but *before* the re-read. It mutates draft_text so the re-read sees
        # a changed text. We leave approved_revision intact so that the
        # specific refusal code triggered is 'text_changed' (not 'stale_revision').
        def _toctou_mutate(row):
            row["draft_text"] = "Hacked text!!"

        db._get_hook = _toctou_mutate

        with patch.dict("os.environ", {"LIVE_UPDATE_SOCIAL_MODE": "publish"}):
            result = await execute_publish_social_draft(bot, db, {"run_id": "run-reg-1"})

        assert result["success"] is False
        assert result["code"] == "text_changed"
        # The publish handler was never invoked
        svc.publish_now.assert_not_called()


# ======================================================================
#  4. Out-of-context quote → warning but still approve
# ======================================================================

class TestOutOfContextQuote:
    @pytest.mark.asyncio
    async def test_quote_not_found_produces_warning(self):
        row = _base_row(revision=1, draft_text="Some draft text")
        db = _FakeDB(row)
        # DM history contains nothing matching the quote
        user = _make_dm_user(dm_history=[_make_dm_message("unrelated chat")])
        bot = _make_bot(fetch_user_returns=user)

        with patch.dict("os.environ", {"ADMIN_USER_ID": "999"}):
            result = await execute_approve_social_draft(
                bot, db,
                {"run_id": "run-reg-1", "admin_approval_quote": "totally missing phrase"},
            )

        assert result["success"] is True
        assert result["warning"] == "quote_not_found_in_recent_dm"
        # Approval was still recorded
        assert result["approval_state"] == "approved"
        approve_calls = [c for c in db.update_calls
                         if c["fields"].get("approval_state") == "approved"]
        assert len(approve_calls) >= 1


# ======================================================================
#  5. Empty-draft approve → 'empty_draft'
# ======================================================================

class TestEmptyDraftApprove:
    @pytest.mark.asyncio
    async def test_none_draft_text_returns_empty_draft(self):
        row = _base_row(draft_text=None)
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_approve_social_draft(
            bot, db,
            {"run_id": "run-reg-1", "admin_approval_quote": "ok go"},
        )
        assert result["success"] is False
        assert result["code"] == "empty_draft"

        # No canonicalisation attempt should have reached DB
        approve_calls = [c for c in db.update_calls
                         if c["fields"].get("approval_state") == "approved"]
        assert len(approve_calls) == 0

    @pytest.mark.asyncio
    async def test_empty_string_draft_text_returns_empty_draft(self):
        row = _base_row(draft_text="")
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_approve_social_draft(
            bot, db,
            {"run_id": "run-reg-1", "admin_approval_quote": "ok go"},
        )
        assert result["success"] is False
        assert result["code"] == "empty_draft"

        approve_calls = [c for c in db.update_calls
                         if c["fields"].get("approval_state") == "approved"]
        assert len(approve_calls) == 0

    @pytest.mark.asyncio
    async def test_whitespace_only_draft_text_returns_empty_draft(self):
        row = _base_row(draft_text="   \n  \t  ")
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_approve_social_draft(
            bot, db,
            {"run_id": "run-reg-1", "admin_approval_quote": "ok go"},
        )
        assert result["success"] is False
        assert result["code"] == "empty_draft"
