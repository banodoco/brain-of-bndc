"""Fake-Supabase CRUD and duplicate-guard tests for live_update_social_runs.

Uses the FakeSupabase/FakeQuery infrastructure modelled on test_social_publications.py.
"""

import pytest
from typing import Optional

from tests.test_social_publications import FakeSupabase, FakeResult, build_db_handler


# ── helpers ────────────────────────────────────────────────────────────

def _upsert(db, topic_id="t1", platform="twitter", action="post", **overrides):
    kwargs = dict(
        topic_id=topic_id,
        platform=platform,
        action=action,
        guild_id=1,
        channel_id=10,
        source_metadata={"cog": "test"},
        topic_summary_data={"title": "test summary"},
        vendor="codex",
        depth="high",
        with_feedback=True,
        deepseek_provider="direct",
    )
    kwargs.update(overrides)
    return db.upsert_live_update_social_run(**kwargs)


def _update(db, run_id, **fields):
    return db.update_live_update_social_run(run_id, **fields)


# ── test helpers -------------------------------------------------------

def test_upsert_creates_deterministic_row():
    """Upsert creates a row with expected deterministic fields."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="topic-a", platform="twitter", action="post")
    assert run is not None
    assert run["topic_id"] == "topic-a"
    assert run["platform"] == "twitter"
    assert run["action"] == "post"
    assert run["mode"] == "draft"
    assert run["terminal_status"] is None
    assert run["chain_vendor"] == "codex"
    assert run["chain_depth"] == "high"
    assert run["chain_with_feedback"] is True
    assert run["chain_deepseek_provider"] == "direct"
    assert run["draft_text"] is None
    assert run["media_decisions"] == {}
    assert run["trace_entries"] == []
    assert run["run_id"] is not None
    assert run["created_at"] is not None
    assert run["updated_at"] is not None

    rows = fake.tables["live_update_social_runs"]
    assert len(rows) == 1


def test_replay_same_key_reuses_existing_row():
    """Replaying the same topic_id+platform+action reuses the existing row."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run1 = _upsert(db, topic_id="topic-b", platform="twitter", action="post")
    run2 = _upsert(db, topic_id="topic-b", platform="twitter", action="post")

    assert run1["run_id"] == run2["run_id"]
    assert len(fake.tables["live_update_social_runs"]) == 1


def test_different_keys_create_distinct_rows():
    """Different topic_id, platform, or action creates distinct rows."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    r1 = _upsert(db, topic_id="t1", platform="twitter", action="post")
    r2 = _upsert(db, topic_id="t2", platform="twitter", action="post")
    r3 = _upsert(db, topic_id="t1", platform="youtube", action="post")
    r4 = _upsert(db, topic_id="t1", platform="twitter", action="reply")

    ids = {r1["run_id"], r2["run_id"], r3["run_id"], r4["run_id"]}
    assert len(ids) == 4
    assert len(fake.tables["live_update_social_runs"]) == 4


def test_terminal_status_update_is_persisted():
    """Terminal status updates are persisted on the row."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t1", platform="twitter", action="post")
    run_id = run["run_id"]

    assert db.update_live_update_social_run(run_id, terminal_status="draft")
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["terminal_status"] == "draft"

    assert db.update_live_update_social_run(run_id, terminal_status="skip")
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["terminal_status"] == "skip"

    assert db.update_live_update_social_run(run_id, terminal_status="needs_review")
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["terminal_status"] == "needs_review"


def test_chain_fields_round_trip():
    """Chain fields are round-tripped correctly."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(
        db,
        topic_id="t-chain",
        vendor="codex",
        depth="high",
        with_feedback=True,
        deepseek_provider="direct",
    )
    run_id = run["run_id"]

    fetched = db.get_live_update_social_run(run_id)
    assert fetched["chain_vendor"] == "codex"
    assert fetched["chain_depth"] == "high"
    assert fetched["chain_with_feedback"] is True
    assert fetched["chain_deepseek_provider"] == "direct"


def test_draft_text_round_trip():
    """Draft text is round-tripped correctly."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-draft")
    run_id = run["run_id"]

    draft = "This is a test draft"
    assert db.update_live_update_social_run(run_id, draft_text=draft)
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["draft_text"] == draft


def test_media_decisions_round_trip():
    """Media decisions JSONB is round-tripped correctly."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-media")
    run_id = run["run_id"]

    decisions = {
        "considered": [{"source": "discord_attachment", "index": 0}],
        "selected": [{"source": "discord_attachment", "index": 0}],
        "skipped": [],
        "unresolved": [],
    }
    assert db.update_live_update_social_run(run_id, media_decisions=decisions)
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["media_decisions"] == decisions


def test_trace_entries_round_trip():
    """Trace/status entries are round-tripped correctly."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-trace")
    run_id = run["run_id"]

    entries = [
        {"event": "created", "ts": "2026-01-01T00:00:00Z"},
        {"event": "tool_called", "ts": "2026-01-01T00:00:01Z", "tool": "draft_social_post"},
    ]
    assert db.update_live_update_social_run(run_id, trace_entries=entries)
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["trace_entries"] == entries


def test_get_nonexistent_run_returns_none():
    """get_live_update_social_run returns None for nonexistent run_id."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)
    assert db.get_live_update_social_run("nonexistent") is None


def test_update_nonexistent_run_returns_true_no_error():
    """update_live_update_social_run on nonexistent run does not crash."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)
    # Should not raise
    result = db.update_live_update_social_run("nonexistent", terminal_status="skip")
    assert result is True  # query succeeds even if no rows match


# ── Sprint 3: run-level persistence tests ────────────────────────────


def test_publication_outcome_persisted():
    """publication_outcome is round-tripped through update and get."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-outcome")
    run_id = run["run_id"]

    outcome = {
        "publication_id": "pub-test",
        "success": True,
        "provider_ref": "tweet-123",
        "provider_url": "https://x.com/user/status/123",
        "media_ids": ["media-1"],
        "media_attached": [{"identity": {"source": "discord_attachment", "index": 0}}],
        "media_missing": [],
        "error": None,
        "failure_reason": None,
    }
    assert db.update_live_update_social_run(
        run_id, publication_outcome=outcome,
    )
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["publication_outcome"] == outcome

    # Update with failure outcome
    failure_outcome = {
        "success": False,
        "error": "Provider rejected media",
        "failure_reason": "provider_rejected_media",
    }
    assert db.update_live_update_social_run(
        run_id, publication_outcome=failure_outcome,
    )
    fetched = db.get_live_update_social_run(run_id)
    assert fetched["publication_outcome"] == failure_outcome


def test_find_runs_by_status():
    """get_recent_social_runs filters by terminal_status and mode."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    # Create runs with different topic_ids (they all get mode="draft" by default)
    r1 = _upsert(db, topic_id="t-draft")
    db.update_live_update_social_run(r1["run_id"], terminal_status="draft")

    r2 = _upsert(db, topic_id="t-pub")
    db.update_live_update_social_run(r2["run_id"], terminal_status="published")
    # Override mode on the fake row directly (upsert hardcodes mode="draft")
    for row in fake.tables["live_update_social_runs"]:
        if row["run_id"] == r2["run_id"]:
            row["mode"] = "publish"
            break

    r3 = _upsert(db, topic_id="t-review")
    db.update_live_update_social_run(r3["run_id"], terminal_status="needs_review")

    # Filter by needs_review
    review_runs = db.get_recent_social_runs(
        guild_id=1, terminal_status="needs_review",
    )
    assert len(review_runs) == 1
    assert review_runs[0]["run_id"] == r3["run_id"]

    # Filter by published
    pub_runs = db.get_recent_social_runs(
        guild_id=1, terminal_status="published",
    )
    assert len(pub_runs) == 1
    assert pub_runs[0]["run_id"] == r2["run_id"]

    # Filter by mode
    publish_mode_runs = db.get_recent_social_runs(
        guild_id=1, mode="publish",
    )
    assert len(publish_mode_runs) == 1
    assert publish_mode_runs[0]["run_id"] == r2["run_id"]

    # All runs
    all_runs = db.get_recent_social_runs(guild_id=1)
    assert len(all_runs) == 3


# ── T2: review-column kwargs ──────────────────────────────────────────


def test_update_review_columns_each_kwarg_appears_in_payload():
    """Each of the 9 new review kwargs is written into the supabase update payload."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-review-cols")
    run_id = run["run_id"]

    result = db.update_live_update_social_run(
        run_id,
        review_message_id=999000111222,
        revision=3,
        approval_state="approved",
        approved_revision=2,
        approved_text="Approved draft text",
        approved_quote="A quoted snippet",
        approved_at="2026-05-30T12:00:00+00:00",
        expires_at="2026-05-31T12:00:00+00:00",
        publish_revision=1,
    )
    assert result is True

    # Verify each kwarg was applied to the row (FakeQuery.execute applies payload to row)
    row = fake.tables["live_update_social_runs"][0]
    assert row["review_message_id"] == 999000111222
    assert row["revision"] == 3
    assert row["approval_state"] == "approved"
    assert row["approved_revision"] == 2
    assert row["approved_text"] == "Approved draft text"
    assert row["approved_quote"] == "A quoted snippet"
    assert row["approved_at"] == "2026-05-30T12:00:00+00:00"
    assert row["expires_at"] == "2026-05-31T12:00:00+00:00"
    assert row["publish_revision"] == 1


def test_update_review_columns_none_kwargs_excluded_from_payload():
    """Non-None review kwargs are written; None kwargs are not set on the row."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-partial-review")
    run_id = run["run_id"]

    # Only pass two of the nine; the rest should be absent from the row
    result = db.update_live_update_social_run(
        run_id,
        approval_state="pending",
        revision=1,
    )
    assert result is True

    row = fake.tables["live_update_social_runs"][0]
    assert row["approval_state"] == "pending"
    assert row["revision"] == 1
    assert "review_message_id" not in row
    assert "approved_text" not in row
    assert "expires_at" not in row


def test_update_environment_param_accepted_does_not_filter():
    """The environment kwarg is accepted but does not filter the update (no such column)."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-env")
    run_id = run["run_id"]

    # Both 'prod' and 'dev' values must succeed without error
    assert db.update_live_update_social_run(run_id, approval_state="pending", environment="prod")
    assert db.update_live_update_social_run(run_id, approval_state="approved", environment="dev")

    row = fake.tables["live_update_social_runs"][0]
    assert row["approval_state"] == "approved"
    # environment must NOT have been written into the row
    assert "environment" not in row


def test_update_review_columns_bool_return_true_on_success():
    """update_live_update_social_run returns True when supabase succeeds."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-bool-true")
    assert db.update_live_update_social_run(run["run_id"], revision=1) is True


def test_update_review_columns_bool_return_false_on_supabase_error():
    """update_live_update_social_run returns False when supabase raises."""
    class RaisingSupabase:
        def table(self, _name):
            raise RuntimeError("supabase connection failed")

    db = build_db_handler(RaisingSupabase())
    # supabase is set so the early-return guard is bypassed
    db.supabase = RaisingSupabase()
    result = db.update_live_update_social_run("any-run-id", revision=5)
    assert result is False


# ── T3: reader methods ────────────────────────────────────────────────


def test_get_social_run_by_review_message_id_present_row():
    """Returns the row when review_message_id matches."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-rmid")
    _update(db, run["run_id"], review_message_id=123456789)

    result = db.get_social_run_by_review_message_id(123456789)
    assert result is not None
    assert result["run_id"] == run["run_id"]
    assert result["review_message_id"] == 123456789


def test_get_social_run_by_review_message_id_absent_row():
    """Returns None when no row matches the review_message_id."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    result = db.get_social_run_by_review_message_id(999999)
    assert result is None


def test_get_social_run_by_review_message_id_uninitialised_supabase():
    """Returns None immediately when supabase client is not set."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)
    db.supabase = None

    result = db.get_social_run_by_review_message_id(111)
    assert result is None


def test_list_open_social_runs_topic_id_filter():
    """topic_id filter returns only runs for the specified topic."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    r1 = _upsert(db, topic_id="topic-alpha", platform="twitter", action="post")
    r2 = _upsert(db, topic_id="topic-beta", platform="twitter", action="post")
    # Both rows need approval_state='pending' for the query filter to match
    _update(db, r1["run_id"], approval_state="pending")
    _update(db, r2["run_id"], approval_state="pending")

    results = db.list_open_social_runs(topic_id="topic-alpha")
    assert len(results) == 1
    assert results[0]["run_id"] == r1["run_id"]


def test_list_open_social_runs_limit_respected():
    """list_open_social_runs returns at most `limit` rows."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    for i in range(5):
        r = _upsert(db, topic_id=f"topic-lim-{i}", platform="twitter", action="post")
        _update(db, r["run_id"], approval_state="pending")

    results = db.list_open_social_runs(limit=3)
    assert len(results) == 3


def test_list_open_social_runs_uninitialised_supabase():
    """Returns empty list immediately when supabase client is not set."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)
    db.supabase = None

    results = db.list_open_social_runs()
    assert results == []


# ── T15: list_pending_review_social_runs ─────────────────────────────


def test_list_pending_review_social_runs_filters_draft_and_excludes_expired():
    """Returns rows with terminal_status='draft' AND approval_state != 'expired'."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    # Three rows: draft+pending (keep), draft+expired (exclude), open/null-terminal (exclude)
    r_keep = _upsert(db, topic_id="t-pending")
    _update(db, r_keep["run_id"], terminal_status="draft", approval_state="pending")

    r_expired = _upsert(db, topic_id="t-expired")
    _update(db, r_expired["run_id"], terminal_status="draft", approval_state="expired")

    r_open = _upsert(db, topic_id="t-open")
    _update(db, r_open["run_id"], approval_state="pending")  # terminal_status stays null

    r_published = _upsert(db, topic_id="t-pub")
    _update(db, r_published["run_id"], terminal_status="published", approval_state="approved")

    results = db.list_pending_review_social_runs()
    run_ids = [row["run_id"] for row in results]
    assert run_ids == [r_keep["run_id"]]


def test_list_pending_review_social_runs_uninitialised_supabase():
    """Returns empty list immediately when supabase client is not set."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)
    db.supabase = None

    assert db.list_pending_review_social_runs() == []


# ── T3: clear_approval kwarg ───────────────────────────────────────────


def test_clear_approval_true_nulls_all_four_approved_columns():
    """clear_approval=True writes explicit None for all four approved_* columns."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-clear-approval")
    run_id = run["run_id"]

    # First set some approved values so they exist in the row
    _update(db, run_id,
        approved_revision=1,
        approved_text="some text",
        approved_quote="some quote",
        approved_at="2026-05-30T12:00:00+00:00",
    )

    # clear_approval=True must null all four regardless of per-field guards
    result = db.update_live_update_social_run(run_id, clear_approval=True)
    assert result is True

    row = fake.tables["live_update_social_runs"][0]
    assert "approved_revision" in row
    assert row["approved_revision"] is None
    assert "approved_text" in row
    assert row["approved_text"] is None
    assert "approved_quote" in row
    assert row["approved_quote"] is None
    assert "approved_at" in row
    assert row["approved_at"] is None


def test_clear_approval_false_default_does_not_null_approved_columns():
    """clear_approval=False (default) leaves approved_* columns untouched."""
    fake = FakeSupabase({"live_update_social_runs": []})
    db = build_db_handler(fake)

    run = _upsert(db, topic_id="t-no-clear")
    run_id = run["run_id"]

    _update(db, run_id,
        approved_text="keep me",
        approved_at="2026-05-30T12:00:00+00:00",
    )

    # Update something unrelated — approved columns must be preserved
    _update(db, run_id, revision=2)

    row = fake.tables["live_update_social_runs"][0]
    assert row.get("approved_text") == "keep me"
    assert row.get("approved_at") == "2026-05-30T12:00:00+00:00"


# ── T5: _dm_admin_with_draft unit tests ─────────────────────────────────

import os as _os
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord as _discord
import pytest

from src.features.sharing.live_update_social.service import LiveUpdateSocialService


def _make_mock_bot(*, fetch_user_returns=None, fetch_user_raises=None):
    """Build a mock bot for _dm_admin_with_draft tests."""
    bot = MagicMock(spec=_discord.Client)
    if fetch_user_raises:
        bot.fetch_user = AsyncMock(side_effect=fetch_user_raises)
    elif fetch_user_returns is not None:
        bot.fetch_user = AsyncMock(return_value=fetch_user_returns)
    else:
        mock_user = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = 12345
        mock_user.send = AsyncMock(return_value=mock_msg)
        bot.fetch_user = AsyncMock(return_value=mock_user)
    return bot


def _make_mock_db_handler(*, update_returns=True):
    """Build a mock db_handler with update_live_update_social_run."""
    db = MagicMock()
    db.update_live_update_social_run = MagicMock(return_value=update_returns)
    return db


class _FakeMediaResponse:
    """Minimal async context manager for mocked aiohttp media responses."""

    def __init__(self, *, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self):
        return self._body


class _FakeMediaSession:
    """Minimal fake aiohttp session keyed by (method, url)."""

    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def head(self, url, **_kwargs):
        response = self.responses[("HEAD", url)]
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **_kwargs):
        response = self.responses[("GET", url)]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_dm_admin_with_draft_happy_path():
    """Happy path: DM sent, review_message_id+expires_at persisted, embed correct."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    media_url = "https://example.com/thumb.png"
    fake_session = _FakeMediaSession({
        ("HEAD", media_url): _FakeMediaResponse(
            headers={"Content-Length": "5", "Content-Type": "image/png"},
        ),
        ("GET", media_url): _FakeMediaResponse(
            headers={"Content-Type": "image/png"},
            body=b"image",
        ),
    })

    with (
        patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}),
        patch(
            "src.features.sharing.live_update_social.service.aiohttp.ClientSession",
            return_value=fake_session,
        ),
    ):
        await svc._dm_admin_with_draft(
            run_id="run-1",
            draft_text="Hello world draft text",
            media_decisions={"selected": [{"url": media_url}]},
            topic_title="My Topic Title",
            source_link="https://discord.com/channels/1/2",
        )

    # Assert fetch_user called with admin id
    bot.fetch_user.assert_awaited_once_with(999)

    # Get the user and verify send was called
    mock_user = bot.fetch_user.return_value
    mock_user.send.assert_awaited_once()
    call_args, _call_kwargs = mock_user.send.call_args
    # send is called with embed=...
    assert "embed" in _call_kwargs or len(call_args) == 0
    embed = _call_kwargs.get("embed")
    if embed is None and len(call_args) > 0:
        embed = call_args[0]

    assert embed is not None, "send should be called with an embed"
    assert embed.title == "My Topic Title"
    assert "Hello world draft text" in embed.description
    assert "run_id=run-1" not in embed.footer.text
    assert embed.footer.text == "Source: https://discord.com/channels/1/2"
    assert embed.thumbnail.url == media_url
    assert _call_kwargs["content"].startswith("Reply to this message to edit")
    assert '"post it"' in _call_kwargs["content"]
    assert '"skip"' in _call_kwargs["content"]
    assert '"list drafts"' in _call_kwargs["content"]
    assert media_url in _call_kwargs["content"]
    assert len(_call_kwargs["files"]) == 1

    # Assert DB write with review_message_id and non-null expires_at
    db.update_live_update_social_run.assert_called_once()
    db_call_kwargs = db.update_live_update_social_run.call_args.kwargs
    assert db_call_kwargs["run_id"] == "run-1"
    assert db_call_kwargs["review_message_id"] == 12345
    assert db_call_kwargs["expires_at"] is not None


def test_resolve_topic_title_falls_back_to_headline():
    """Topic title prefers cleaned title, then headline/name/subject."""
    svc = LiveUpdateSocialService(db_handler=MagicMock(), bot=None)

    assert svc._resolve_topic_title({"title": "  Explicit title  "}) == "Explicit title"
    assert svc._resolve_topic_title({"title": " ", "headline": "Real headline"}) == "Real headline"
    assert svc._resolve_topic_title({"name": "Named topic"}) == "Named topic"
    assert svc._resolve_topic_title({}) == ""


@pytest.mark.asyncio
async def test_dm_admin_with_draft_resolves_discord_attachment_thumbnail():
    """Discord attachment media refs resolve to image CDN URLs for the embed thumbnail."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    media_url = "https://cdn.discordapp.com/preview.png"

    inspected = {
        "attachments": [
            {
                "filename": "preview.png",
                "url": media_url,
                "content_type": "image/png",
            },
        ],
        "embeds_media": [],
    }
    fake_session = _FakeMediaSession({
        ("HEAD", media_url): _FakeMediaResponse(
            headers={"Content-Length": "5", "Content-Type": "image/png"},
        ),
        ("GET", media_url): _FakeMediaResponse(
            headers={"Content-Type": "image/png"},
            body=b"image",
        ),
    })
    with (
        patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}),
        patch(
            "src.features.sharing.live_update_social.service.inspect_discord_message",
            AsyncMock(return_value=inspected),
        ) as inspect_mock,
        patch(
            "src.features.sharing.live_update_social.service.aiohttp.ClientSession",
            return_value=fake_session,
        ),
    ):
        await svc._dm_admin_with_draft(
            run_id="run-attachment",
            draft_text="Hello world draft text",
            media_decisions={
                "selected": [
                    {
                        "source": "discord_attachment",
                        "channel_id": 111,
                        "message_id": 222,
                        "attachment_index": 0,
                    },
                ],
            },
            topic_title="My Topic Title",
            source_link="",
        )

    inspect_mock.assert_awaited_once_with(bot, 111, 222)
    send_kwargs = bot.fetch_user.return_value.send.call_args.kwargs
    embed = send_kwargs["embed"]
    assert embed.thumbnail.url == media_url
    assert embed.footer.text is None
    assert media_url in send_kwargs["content"]
    assert len(send_kwargs["files"]) == 1


@pytest.mark.asyncio
async def test_dm_admin_with_draft_surfaces_image_and_video_links():
    """Every selected media ref is surfaced as a link; small files are attached."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    image_url = "https://cdn.discordapp.com/preview.png"
    video_url = "https://cdn.discordapp.com/clip.mp4"

    inspected = {
        "attachments": [
            {
                "filename": "preview.png",
                "url": image_url,
                "content_type": "image/png",
            },
        ],
        "embeds_media": [
            {
                "slot": "video",
                "url": video_url,
                "content_type": "video/mp4",
                "filename": "clip.mp4",
            },
        ],
    }
    fake_session = _FakeMediaSession({
        ("HEAD", image_url): _FakeMediaResponse(
            headers={"Content-Length": "5", "Content-Type": "image/png"},
        ),
        ("GET", image_url): _FakeMediaResponse(
            headers={"Content-Type": "image/png"},
            body=b"image",
        ),
        ("HEAD", video_url): _FakeMediaResponse(
            headers={"Content-Length": "5", "Content-Type": "video/mp4"},
        ),
        ("GET", video_url): _FakeMediaResponse(
            headers={"Content-Type": "video/mp4"},
            body=b"video",
        ),
    })

    with (
        patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}),
        patch(
            "src.features.sharing.live_update_social.service.inspect_discord_message",
            AsyncMock(return_value=inspected),
        ),
        patch(
            "src.features.sharing.live_update_social.service.aiohttp.ClientSession",
            return_value=fake_session,
        ),
    ):
        await svc._dm_admin_with_draft(
            run_id="run-media",
            draft_text="Draft with media",
            media_decisions={
                "selected": [
                    {
                        "source": "discord_attachment",
                        "channel_id": 111,
                        "message_id": 222,
                        "attachment_index": 0,
                    },
                    {
                        "source": "discord_embed",
                        "channel_id": 111,
                        "message_id": 222,
                        "embed_slot": "video",
                    },
                ],
            },
            topic_title="My Topic Title",
            source_link="",
        )

    send_kwargs = bot.fetch_user.return_value.send.call_args.kwargs
    assert image_url in send_kwargs["content"]
    assert video_url in send_kwargs["content"]
    assert "image/png" in send_kwargs["content"]
    assert "video/mp4" in send_kwargs["content"]
    assert send_kwargs["embed"].thumbnail.url == image_url
    assert len(send_kwargs["files"]) == 2
    assert [file.filename for file in send_kwargs["files"]] == ["preview.png", "clip.mp4"]


@pytest.mark.asyncio
async def test_dm_admin_with_draft_oversized_download_falls_back_to_link():
    """Oversized or failed media is not attached, but its link remains visible."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    media_url = "https://cdn.discordapp.com/large.mp4"
    failed_url = "https://cdn.discordapp.com/failed.png"
    fake_session = _FakeMediaSession({
        ("HEAD", media_url): _FakeMediaResponse(
            headers={
                "Content-Length": str(9 * 1024 * 1024),
                "Content-Type": "video/mp4",
            },
        ),
        ("HEAD", failed_url): _FakeMediaResponse(
            headers={"Content-Length": "5", "Content-Type": "image/png"},
        ),
        ("GET", failed_url): RuntimeError("download failed"),
    })

    with (
        patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}),
        patch(
            "src.features.sharing.live_update_social.service.aiohttp.ClientSession",
            return_value=fake_session,
        ),
    ):
        await svc._dm_admin_with_draft(
            run_id="run-large-media",
            draft_text="Draft with large media",
            media_decisions={
                "selected": [
                    {
                        "source": "url",
                        "url": media_url,
                        "content_type": "video/mp4",
                        "filename": "large.mp4",
                    },
                    {
                        "source": "url",
                        "url": failed_url,
                        "content_type": "image/png",
                        "filename": "failed.png",
                    },
                ],
            },
            topic_title="My Topic Title",
            source_link="",
        )

    send_kwargs = bot.fetch_user.return_value.send.call_args.kwargs
    assert media_url in send_kwargs["content"]
    assert failed_url in send_kwargs["content"]
    assert "files" not in send_kwargs


@pytest.mark.asyncio
async def test_dm_admin_with_draft_no_media_still_sends_how_to():
    """No media still sends the review embed and text instructions."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_draft(
            run_id="run-no-media",
            draft_text="Draft without media",
            media_decisions={},
            topic_title="No Media Topic",
            source_link="",
        )

    send_kwargs = bot.fetch_user.return_value.send.call_args.kwargs
    assert send_kwargs["embed"].title == "No Media Topic"
    assert "Reply to this message to edit" in send_kwargs["content"]
    assert "Media:" not in send_kwargs["content"]
    assert "files" not in send_kwargs
    db.update_live_update_social_run.assert_called_once()


@pytest.mark.asyncio
async def test_dm_admin_with_draft_dm_raise_no_propagate():
    """When DM send raises, no exception propagates and no review_message_id write."""
    bot = _make_mock_bot(fetch_user_raises=Exception("DM failed"))
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        # Must not raise
        await svc._dm_admin_with_draft(
            run_id="run-2",
            draft_text="Some draft",
            media_decisions={},
            topic_title="Test",
            source_link="",
        )

    # Verify that update_live_update_social_run was NOT called with review_message_id
    # (It may have been called for other reasons, but not with review_message_id)
    calls_with_review = [
        c for c in db.update_live_update_social_run.call_args_list
        if c.kwargs.get("review_message_id") is not None
    ]
    assert len(calls_with_review) == 0, (
        "update_live_update_social_run must NOT be called with review_message_id "
        "when DM fails"
    )


@pytest.mark.asyncio
async def test_dm_admin_with_draft_whitespace_only():
    """Whitespace-only draft_text: no DM send and no review_message_id write."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_draft(
            run_id="run-3",
            draft_text="   ",
            media_decisions={},
            topic_title="Test",
            source_link="",
        )

    # fetch_user must NOT have been called
    bot.fetch_user.assert_not_awaited()

    # update_live_update_social_run must NOT have been called with review_message_id
    calls_with_review = [
        c for c in db.update_live_update_social_run.call_args_list
        if c.kwargs.get("review_message_id") is not None
    ]
    assert len(calls_with_review) == 0


@pytest.mark.asyncio
async def test_dm_admin_with_draft_none_text():
    """None draft_text: no DM send and no review_message_id write."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_draft(
            run_id="run-4",
            draft_text=None,
            media_decisions={},
            topic_title="Test",
            source_link="",
        )

    bot.fetch_user.assert_not_awaited()
    calls_with_review = [
        c for c in db.update_live_update_social_run.call_args_list
        if c.kwargs.get("review_message_id") is not None
    ]
    assert len(calls_with_review) == 0


@pytest.mark.asyncio
async def test_dm_admin_with_draft_no_admin_user_id():
    """Missing ADMIN_USER_ID: no DM send, no review_message_id write."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {}, clear=True):
        if "ADMIN_USER_ID" in _os.environ:
            del _os.environ["ADMIN_USER_ID"]
        await svc._dm_admin_with_draft(
            run_id="run-5",
            draft_text="Real text",
            media_decisions={},
            topic_title="Test",
            source_link="",
        )

    bot.fetch_user.assert_not_awaited()
    calls_with_review = [
        c for c in db.update_live_update_social_run.call_args_list
        if c.kwargs.get("review_message_id") is not None
    ]
    assert len(calls_with_review) == 0


# ── T7: terminal DM tests (needs_review / failed) ───────────────────────

def _make_terminal_run_row(**overrides):
    """A run row shaped like the real table row the service re-fetches."""
    row = {
        "run_id": "run-1",
        "topic_id": "topic-1",
        "guild_id": 1,
        "terminal_status": "needs_review",
        "draft_text": None,
        "trace_entries": [],
        "source_metadata": {"cog": "topic_editor", "environment": "prod"},
        "publish_units": {
            "title": "Test Topic",
            "channel_id": 10,
            "message_id": 42,
        },
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_extract_terminal_reason_reads_reason_on_tool_event():
    """A tool trace storing its message under ``reason`` is surfaced (bug fix)."""
    svc = LiveUpdateSocialService(db_handler=MagicMock(), bot=None)
    reason = svc._extract_terminal_reason(
        "needs_review",
        [
            {"event": "llm_called", "ts": "t"},
            {
                "event": "tool",
                "tool": "request_social_review",
                "reason": "Could not parse a clean draft from the model response.",
            },
        ],
        None,
    )
    assert reason == "Could not parse a clean draft from the model response."


@pytest.mark.asyncio
async def test_extract_terminal_reason_prefers_force_needs_review():
    """force_needs_review carries the authoritative reason."""
    svc = LiveUpdateSocialService(db_handler=MagicMock(), bot=None)
    reason = svc._extract_terminal_reason(
        "needs_review",
        [
            {"event": "no_tool_call_parsed", "raw_response": "I'll think about it."},
            {"event": "force_needs_review",
             "reason": "LLM did not produce a valid tool call."},
        ],
        None,
    )
    assert reason == "LLM did not produce a valid tool call."


def test_extract_raw_response_returns_snippet():
    """no_tool_call_parsed raw_response is surfaced, trimmed of whitespace."""
    svc = LiveUpdateSocialService(db_handler=MagicMock(), bot=None)
    raw = svc._extract_raw_response([
        {"event": "no_tool_call_parsed",
         "raw_response": "  <read_tools>…</read_tools>  "},
    ])
    assert raw == "<read_tools>…</read_tools>"
    assert svc._extract_raw_response([]) == ""


@pytest.mark.asyncio
async def test_dm_admin_with_terminal_needs_review_enriched():
    """needs_review DM carries reason + source link + model raw response."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    row = _make_terminal_run_row(trace_entries=[
        {"event": "no_tool_call_parsed",
         "raw_response": "I'll gather context about this topic before making a decision."},
        {"event": "force_needs_review",
         "reason": "LLM did not produce a valid tool call."},
    ])

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_terminal(
            run_id="run-1",
            terminal_status="needs_review",
            reason="LLM did not produce a valid tool call.",
            topic_title="Test Topic",
            run_row=row,
        )

    mock_user = bot.fetch_user.return_value
    mock_user.send.assert_awaited_once()
    _args, kwargs = mock_user.send.call_args
    embed = kwargs.get("embed")
    assert embed.title == "Needs review — Test Topic"
    assert "LLM did not produce a valid tool call." in embed.description
    assert "https://discord.com/channels/1/10/42" in embed.description
    assert "Model said:" in embed.description
    assert "I'll gather context" in embed.description
    assert embed.footer.text == "run run-1"
    assert db.update_live_update_social_run.call_count == 1
    db_kwargs = db.update_live_update_social_run.call_args.kwargs
    assert db_kwargs["review_message_id"] == 12345
    assert db_kwargs["expires_at"] is not None


@pytest.mark.asyncio
async def test_dm_admin_with_terminal_failed_includes_draft():
    """failed DM shows the preserved draft and the publish error."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    row = _make_terminal_run_row(
        terminal_status="failed",
        draft_text="A draft that failed to publish.",
    )

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_terminal(
            run_id="run-1",
            terminal_status="failed",
            reason="route missing",
            topic_title="Test Topic",
            run_row=row,
        )

    mock_user = bot.fetch_user.return_value
    _args, kwargs = mock_user.send.call_args
    embed = kwargs["embed"]
    assert embed.title == "Publish failed — Test Topic"
    assert "A draft that failed to publish." in embed.description
    assert "The draft is preserved." in kwargs["content"]


@pytest.mark.asyncio
async def test_dm_admin_with_terminal_includes_topic_summary():
    """Topic summary from the topics table is pulled into the DM."""
    db = _make_mock_db_handler()
    db.get_topic = MagicMock(return_value={
        "topic_id": "topic-1",
        "headline": "Test Topic",
        "summary": {"body": "Elvaxorn ships a ComfyUI node for EverAnimate…"},
    })
    bot = _make_mock_bot()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_terminal(
            run_id="run-1",
            terminal_status="needs_review",
            reason="LLM did not produce a valid tool call.",
            topic_title="Test Topic",
            run_row=_make_terminal_run_row(),
        )

    db.get_topic.assert_called_once_with("topic-1", environment="prod")
    embed = bot.fetch_user.return_value.send.call_args.kwargs["embed"]
    assert "Topic:" in embed.description
    assert "Elvaxorn ships a ComfyUI node" in embed.description


@pytest.mark.asyncio
async def test_dm_admin_with_terminal_survives_topic_fetch_failure():
    """A failing topic fetch degrades the DM, never breaks it."""
    db = _make_mock_db_handler()
    db.get_topic = MagicMock(side_effect=Exception("boom"))
    bot = _make_mock_bot()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_terminal(
            run_id="run-1",
            terminal_status="needs_review",
            reason="LLM did not produce a valid tool call.",
            topic_title="Test Topic",
            run_row=_make_terminal_run_row(),
        )

    assert bot.fetch_user.return_value.send.await_count == 1


@pytest.mark.asyncio
async def test_queue_terminal_dm_batches_needs_review_into_one_dm():
    """Multiple needs_review runs in the window → ONE DM listing all runs."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    svc._terminal_batch_window = 0  # flush immediately after queueing

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._queue_terminal_dm(
            run_id="run-1", terminal_status="needs_review",
            reason="LLM did not produce a valid tool call.",
            topic_title="Topic One",
            run_row=_make_terminal_run_row(run_id="run-1"),
        )
        await svc._queue_terminal_dm(
            run_id="run-2", terminal_status="needs_review",
            reason="LLM did not produce a valid tool call.",
            topic_title="Topic Two",
            run_row=_make_terminal_run_row(run_id="run-2"),
        )
        # Await the background flush task (window 0 → immediate)
        await svc._terminal_flush_task

    mock_user = bot.fetch_user.return_value
    assert mock_user.send.await_count == 1
    _args, kwargs = mock_user.send.call_args
    embed = kwargs["embed"]
    assert embed.title == "Needs review — 2 run(s)"
    assert "Topic One" in embed.description
    assert "Topic Two" in embed.description
    assert "run-1" in kwargs["content"]
    assert "run-2" in kwargs["content"]
    # review_message_id is bound to the FIRST run only (single-row resolution)
    assert db.update_live_update_social_run.call_count == 1
    db_kwargs = db.update_live_update_social_run.call_args.kwargs
    assert db_kwargs["run_id"] == "run-1"
    assert db_kwargs["review_message_id"] == 12345


@pytest.mark.asyncio
async def test_queue_terminal_dm_failed_sends_immediately_not_batched():
    """failed runs DM immediately — never queued behind the batch window."""
    bot = _make_mock_bot()
    db = _make_mock_db_handler()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)
    svc._terminal_batch_window = 0

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._queue_terminal_dm(
            run_id="run-1", terminal_status="failed",
            reason="route missing", topic_title="Topic",
            run_row=_make_terminal_run_row(terminal_status="failed"),
        )
        assert getattr(svc, "_terminal_flush_task", None) is None

    mock_user = bot.fetch_user.return_value
    assert mock_user.send.await_count == 1
    embed = mock_user.send.call_args.kwargs["embed"]
    assert embed.title == "Publish failed — Topic"


@pytest.mark.asyncio
async def test_dm_admin_with_terminal_summary_blocks_shape_and_cleanup():
    """Blocks-shaped summaries (the real topics.summary shape) render cleanly."""
    db = _make_mock_db_handler()
    db.get_topic = MagicMock(return_value={
        "topic_id": "topic-1",
        "headline": "Test Topic",
        "summary": {
            "blocks": [
                {"text": "**yi** shared [JoyAI-Echo](https://huggingface.co/x) (LTX) [1][2] — test."},
                {"text": "**MarkDalias** found it consistent."},
            ]
        },
    })
    bot = _make_mock_bot()
    svc = LiveUpdateSocialService(db_handler=db, bot=bot)

    with patch.dict(_os.environ, {"ADMIN_USER_ID": "999"}):
        await svc._dm_admin_with_terminal(
            run_id="run-1",
            terminal_status="needs_review",
            reason="LLM did not produce a valid tool call.",
            topic_title="Test Topic",
            run_row=_make_terminal_run_row(),
        )

    embed = bot.fetch_user.return_value.send.call_args.kwargs["embed"]
    assert "yi shared JoyAI-Echo (LTX) — test. MarkDalias found it consistent." in embed.description
    assert "**yi**" not in embed.description
    assert "[1][2]" not in embed.description
