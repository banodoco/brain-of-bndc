"""Happy-path tests for the 6 social-draft review executors in tools.py.

Uses in-memory fakes for db_handler and a minimal fake bot exposing
social_publish_service so every branch can be exercised without real
Discord or Supabase calls.

Covers T16:
  1. update_social_draft: revision bump + clear_approval + media preservation.
  2. approve_social_draft: revision-bind + canonicalise + success shape.
  3. publish_social_draft: current-revision approval → {success: True,
     tweet_url (from provider_url), provider_ref, final_text}.
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── imports under test ──────────────────────────────────────────────────
from src.features.admin_chat.tools import (
    execute_update_social_draft,
    execute_approve_social_draft,
    execute_publish_social_draft,
    _canon,
)


# ======================================================================
#  In-memory fake db_handler
# ======================================================================

class _FakeDB:
    """In-memory fake that records every update_live_update_social_run call
    and returns the latest mutated state from get_live_update_social_run."""

    def __init__(self, row: dict | None = None):
        self._row = dict(row) if row else None
        self.update_calls: list[dict] = []

    def get_live_update_social_run(self, run_id: str) -> dict | None:
        if self._row is None:
            return None
        # Always return a fresh copy so callers see latest state.
        return copy.deepcopy(self._row)

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
#  Minimal fake bot
# ======================================================================

class _FakePublishResult:
    """Simulates the return value of social_publish_service.publish_now or
    equivalent handler result."""

    def __init__(self, ok: bool = True, provider_url: str = "", provider_ref: str = "", provider_refs: list | None = None):
        self._ok = ok
        self._provider_url = provider_url
        self._provider_ref = provider_ref
        self._provider_refs = provider_refs or []

    def get(self, key, default=None):
        if key == "ok":
            return self._ok
        if key == "provider_url":
            return self._provider_url
        if key == "provider_ref":
            return self._provider_ref
        if key == "provider_refs":
            return self._provider_refs
        if key == "error":
            return None
        return default


def _make_bot(*, social_publish_service=None, fetch_user_returns=None):
    bot = MagicMock()
    bot.social_publish_service = social_publish_service
    if fetch_user_returns is not None:
        bot.fetch_user = AsyncMock(return_value=fetch_user_returns)
    else:
        bot.fetch_user = AsyncMock()
    return bot


async def _stub_publish_handler(run_state, params):
    """A stub publish handler that returns synthetic success without real Twitter API."""
    return {
        "tool": "publish_social_post",
        "terminal_status": "published",
        "ok": True,
        "provider_url": "https://x.com/example/status/42",
        "provider_ref": "tweet-ref-42",
        "provider_refs": ["tweet-ref-42"],
    }


def _make_dm_user(msg_id=12345, dm_history=None):
    """Returns a mock user whose DM channel supports .history()."""
    user = MagicMock()
    user.id = msg_id
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


# ======================================================================
#  Base row factory
# ======================================================================

def _base_row(**overrides) -> dict:
    row: dict = {
        "run_id": "run-happy-1",
        "topic_id": "topic-1",
        "platform": "twitter",
        "action": "post",
        "mode": "draft",
        "terminal_status": "draft",
        "guild_id": 1,
        "channel_id": 10,
        "draft_text": "Hello, this is a draft tweet.",
        "media_decisions": {
            "selected": [{"url": "https://example.com/img1.png", "title": "first image"}],
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
        "expires_at": None,
        "publish_revision": None,
        "topic_summary_data": {"title": "My Happy Topic"},
        "source_metadata": {},
        "trace_entries": [],
        "created_at": "2026-05-30T00:00:00+00:00",
        "updated_at": "2026-05-30T00:00:00+00:00",
    }
    row.update(overrides)
    return row


# ======================================================================
#  T16 / test 1 — update_social_draft
# ======================================================================

class TestUpdateSocialDraftHappy:
    """Happy path: revision bump, clear_approval, media preservation."""

    @pytest.mark.asyncio
    async def test_bumps_revision_clears_approval(self):
        row = _base_row(
            revision=2,
            approval_state="approved",
            approved_revision=2,
            approved_text="Hello canonical",
            approved_quote="yes approve",
            approved_at="2026-05-30T01:00:00+00:00",
        )
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_update_social_draft(
            bot, db,
            {"run_id": "run-happy-1", "new_text": "Updated draft"},
        )

        assert result["success"] is True
        assert result["run_id"] == "run-happy-1"
        assert result["revision"] == 3
        assert result["draft_text"] == "Updated draft"
        assert result["approval_state"] == "pending"

        # Verify clear_approval=True was passed
        clear_calls = [c for c in db.update_calls if c["clear_approval"]]
        assert len(clear_calls) == 1, "expected one clear_approval update"
        assert clear_calls[0]["fields"]["approval_state"] == "pending"
        assert clear_calls[0]["fields"]["revision"] == 3

        # Verify the in-memory row now has cleared approval fields
        assert db._row["approved_revision"] is None
        assert db._row["approved_text"] is None
        assert db._row["approved_quote"] is None
        assert db._row["approved_at"] is None

    @pytest.mark.asyncio
    async def test_preserves_existing_media_when_omitted(self):
        row = _base_row(
            draft_text="old text",
            media_decisions={"selected": [{"url": "http://x.com/img.png"}], "considered": [], "skipped": [], "unresolved": []},
        )
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_update_social_draft(
            bot, db,
            {"run_id": "run-happy-1", "new_text": "new text"},
        )
        assert result["success"] is True

        # The draft handler should have been called with preserved media
        # (The handler is _make_draft_handler which is a real call — but our
        # fake DB just records. We verify the row still has its media.)
        # The existing media was preserved because selected_media was omitted.
        assert db._row["media_decisions"]["selected"] == [{"url": "http://x.com/img.png"}]

    @pytest.mark.asyncio
    async def test_missing_row_returns_error_code(self):
        db = _FakeDB(None)
        bot = _make_bot()
        result = await execute_update_social_draft(
            bot, db,
            {"run_id": "run-happy-1", "new_text": "boo"},
        )
        assert result["success"] is False
        assert result["code"] == "missing_or_published"


# ======================================================================
#  T16 / test 2 — approve_social_draft
# ======================================================================

class TestApproveSocialDraftHappy:
    """Happy path: canonicalise + bind to revision + success shape."""

    @pytest.mark.asyncio
    async def test_binds_to_revision_and_canonicalises(self):
        row = _base_row(
            revision=3,
            draft_text="  Hello   world  !  ",
            approval_state="pending",
        )
        db = _FakeDB(row)
        user = _make_dm_user(dm_history=[_make_dm_message("yes hello world confirm please")])
        bot = _make_bot(fetch_user_returns=user)

        with patch.dict("os.environ", {"ADMIN_USER_ID": "999"}):
            result = await execute_approve_social_draft(
                bot, db,
                {"run_id": "run-happy-1", "admin_approval_quote": "hello world confirm"},
            )

        assert result["success"] is True
        assert result["approval_state"] == "approved"
        assert result["approved_revision"] == 3

        # Check DB wrote canonicalised text
        approve_calls = [c for c in db.update_calls
                         if c["fields"].get("approval_state") == "approved"]
        assert len(approve_calls) >= 1
        stored_approved = approve_calls[-1]["fields"].get("approved_text")
        assert stored_approved == "Hello world !"  # _canon collapses whitespace

    @pytest.mark.asyncio
    async def test_returns_success_shape(self):
        row = _base_row(revision=1, draft_text="Simple draft.", approval_state="pending")
        db = _FakeDB(row)
        user = _make_dm_user(dm_history=[_make_dm_message("simple confirm")])
        bot = _make_bot(fetch_user_returns=user)

        with patch.dict("os.environ", {"ADMIN_USER_ID": "999"}):
            result = await execute_approve_social_draft(
                bot, db,
                {"run_id": "run-happy-1", "admin_approval_quote": "simple confirm"},
            )

        assert result["success"] is True
        assert result["run_id"] == "run-happy-1"
        assert "approved_revision" in result
        assert result["approval_state"] == "approved"


# ======================================================================
#  T16 / test 3 — publish_social_draft
# ======================================================================

class TestPublishSocialDraftHappy:
    """Happy path: publish a current-revision approved draft."""

    @pytest.mark.asyncio
    async def test_publishes_and_returns_tweet_url(self):
        row = _base_row(
            revision=3,
            approval_state="approved",
            approved_revision=3,
            approved_text=_canon("Hello from draft"),
            draft_text="Hello from draft",
        )
        db = _FakeDB(row)
        # social_publish_service must be non-None for the executor to proceed
        svc = MagicMock()
        bot = _make_bot(social_publish_service=svc)

        with patch(
            "src.features.admin_chat.tools._make_publish_handler",
            return_value=_stub_publish_handler,
        ):
            result = await execute_publish_social_draft(
                bot, db,
                {"run_id": "run-happy-1"},
            )

        assert result["success"] is True
        assert result["run_id"] == "run-happy-1"
        assert result["tweet_url"] == "https://x.com/example/status/42"
        assert result["provider_ref"] == "tweet-ref-42"
        assert result["final_text"] == "Hello from draft"
