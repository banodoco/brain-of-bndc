"""Tests for execute_develop_social_proposal — the pick→develop bridge.

Covers:
  1. Happy path: proposed run + chosen proposal → terminal_status='draft',
     draft_text persisted, revision bump, approval pending.
  2. Guards: not-proposed run rejected; missing draft_text; missing run;
     no proposals; proposal index out of range.
  3. Media preservation when selected_media omitted.
"""

from __future__ import annotations

import copy
import pytest
from unittest.mock import MagicMock

from src.features.admin_chat.tools import execute_develop_social_proposal


class _FakeDB:
    """In-memory fake db_handler mirroring the other social-tool tests."""

    def __init__(self, row: dict | None = None):
        self._row = dict(row) if row else None
        self.update_calls: list[dict] = []

    def get_live_update_social_run(self, run_id: str) -> dict | None:
        if self._row is None:
            return None
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


def _make_bot():
    return MagicMock()


def _proposed_row(**overrides) -> dict:
    row: dict = {
        "run_id": "run-proposed-1",
        "topic_id": "topic-1",
        "platform": "twitter",
        "action": "post",
        "mode": "draft",
        "terminal_status": "proposed",
        "guild_id": 1,
        "channel_id": 10,
        "draft_text": None,
        "proposals": [
            {
                "theme": "76 styles, one GPU",
                "media_strategy": "compile 60+ clips from this thread into one montage",
                "pattern": "thread_to_compilation",
                "media_understanding_basis": "60+ clips, distinct styles",
                "rationale": "volume + range is the story",
                "source_message_ids": ["42", "43"],
            },
            {
                "theme": "Wan Animate 2 motion transfer",
                "media_strategy": "montage of 6+ example clips",
                "pattern": "showcase_montage",
                "media_understanding_basis": "split-screen style range",
                "rationale": "claim + proof by variety",
                "source_message_ids": ["44"],
            },
        ],
        "media_decisions": {
            "selected": [{"source": "discord_attachment", "message_id": 42, "attachment_index": 0}],
            "considered": [],
            "skipped": [],
            "unresolved": [],
        },
        "revision": 0,
        "approval_state": "pending",
        "approved_revision": None,
        "approved_text": None,
        "approved_quote": None,
        "approved_at": None,
        "expires_at": None,
        "publish_revision": None,
        "topic_summary_data": {"title": "My Topic"},
        "source_metadata": {},
        "trace_entries": [],
        "created_at": "2026-08-11T00:00:00+00:00",
        "updated_at": "2026-08-11T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class TestDevelopSocialProposal:

    @pytest.mark.asyncio
    async def test_happy_path_flips_proposed_to_draft(self):
        db = _FakeDB(_proposed_row())
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {
                "run_id": "run-proposed-1",
                "proposal_index": 2,
                "draft_text": "Wan Animate 2 allows for versatile motion transfer. Examples by Kijai:",
            },
        )

        assert result["success"] is True
        assert result["run_id"] == "run-proposed-1"
        assert result["proposal_index"] == 2
        assert result["proposal_theme"] == "Wan Animate 2 motion transfer"
        assert result["proposal_media_strategy"] == "montage of 6+ example clips"
        assert result["terminal_status"] == "draft"
        assert result["revision"] == 1
        assert result["approval_state"] == "pending"

        # DB state: terminal draft, draft_text persisted, media preserved.
        assert db._row is not None
        assert db._row["terminal_status"] == "draft"
        assert db._row["draft_text"].startswith("Wan Animate 2")
        assert db._row["media_decisions"]["selected"] == [
            {"source": "discord_attachment", "message_id": 42, "attachment_index": 0}
        ]

    @pytest.mark.asyncio
    async def test_media_override(self):
        db = _FakeDB(_proposed_row())
        bot = _make_bot()
        new_media = [{"source": "discord_attachment", "message_id": 44, "attachment_index": 0}]

        result = await execute_develop_social_proposal(
            bot, db,
            {
                "run_id": "run-proposed-1",
                "proposal_index": 1,
                "draft_text": "76 styles on one GPU.",
                "selected_media": new_media,
            },
        )

        assert result["success"] is True
        assert db._row is not None
        assert db._row["media_decisions"]["selected"] == new_media

    @pytest.mark.asyncio
    async def test_rejects_non_proposed_run(self):
        row = _proposed_row(terminal_status="draft", draft_text="already a draft")
        db = _FakeDB(row)
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "run-proposed-1", "proposal_index": 1, "draft_text": "x"},
        )

        assert result["success"] is False
        assert result["code"] == "not_proposed"

    @pytest.mark.asyncio
    async def test_rejects_missing_draft_text(self):
        db = _FakeDB(_proposed_row())
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "run-proposed-1", "proposal_index": 1},
        )

        assert result["success"] is False
        assert result["code"] == "missing_draft_text"

    @pytest.mark.asyncio
    async def test_rejects_missing_run(self):
        db = _FakeDB(None)
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "nope", "proposal_index": 1, "draft_text": "x"},
        )

        assert result["success"] is False
        assert result["code"] == "missing_run"

    @pytest.mark.asyncio
    async def test_rejects_no_proposals(self):
        db = _FakeDB(_proposed_row(proposals=[]))
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "run-proposed-1", "proposal_index": 1, "draft_text": "x"},
        )

        assert result["success"] is False
        assert result["code"] == "no_proposals"

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_index(self):
        db = _FakeDB(_proposed_row())
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "run-proposed-1", "proposal_index": 5, "draft_text": "x"},
        )

        assert result["success"] is False
        assert result["code"] == "proposal_out_of_range"

    @pytest.mark.asyncio
    async def test_rejects_expired_run(self):
        """An approval_state='expired' (discarded-topic) run cannot be developed."""
        db = _FakeDB(_proposed_row(approval_state="expired"))
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "run-proposed-1", "proposal_index": 1, "draft_text": "x"},
        )

        assert result["success"] is False
        assert result["code"] == "expired"

    @pytest.mark.asyncio
    async def test_rejects_expired_run_when_drafting(self):
        """update_social_draft refuses approval_state='expired' runs."""
        from src.features.admin_chat.tools import execute_update_social_draft
        db = _FakeDB(_proposed_row(terminal_status="draft", draft_text="d",
                                   approval_state="expired"))
        bot = _make_bot()

        result = await execute_update_social_draft(
            bot, db,
            {"run_id": "run-proposed-1", "new_text": "edited"},
        )

        assert result["success"] is False
        assert result["code"] == "expired"

    @pytest.mark.asyncio
    async def test_rejects_zero_index(self):
        db = _FakeDB(_proposed_row())
        bot = _make_bot()

        result = await execute_develop_social_proposal(
            bot, db,
            {"run_id": "run-proposed-1", "proposal_index": 0, "draft_text": "x"},
        )

        assert result["success"] is False
        assert result["code"] == "proposal_out_of_range"
