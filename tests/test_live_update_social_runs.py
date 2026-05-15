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
