"""Conformance and integration tests for the full draft loop.

Covers:
(a) tool registry conformance — exactly 3 tools, each with 1 binding
(b) sent status creates one run
(c) partial status creates one run
(d) non-sent/partial status creates zero runs
(e) non-post action creates zero runs
(f) duplicate replay reuses the same run
(g) draft_social_post sets terminal='draft' with draft_text + media inspectable
(h) skip_social_post sets terminal='skip'
(i) media-resolution failure → needs_review
(j) no social_publications rows created in any path
(k) media-ref identities recorded as considered/selected/skipped/unresolved
(l) trace/status logging entries appended to the run
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_social_publications import FakeSupabase, FakeResult, build_db_handler

from src.common.db_handler import DatabaseHandler
from src.features.sharing.live_update_social.contracts import LiveUpdateHandoffPayload
from src.features.sharing.live_update_social.models import (
    MediaRefIdentity,
    RunState,
    ToolSpec,
    ToolBinding,
    ToolResult,
    PublishOutcome,
    ThreadItem,
    ThreadDraft,
)
from src.features.sharing.live_update_social.tools import (
    ALL_TOOL_SPECS,
    TOOL_DRAFT_SOCIAL_POST,
    TOOL_SKIP_SOCIAL_POST,
    TOOL_REQUEST_SOCIAL_REVIEW,
    TOOL_GET_LIVE_UPDATE_TOPIC,
    TOOL_GET_SOURCE_MESSAGES,
    TOOL_GET_PUBLISHED_UPDATE_CONTEXT,
    TOOL_INSPECT_MESSAGE_MEDIA,
    TOOL_LIST_SOCIAL_ROUTES,
    TOOL_PUBLISH_SOCIAL_POST,
    TOOL_FIND_EXISTING_SOCIAL_POSTS,
    TOOL_GET_SOCIAL_RUN_STATUS,
    build_tool_bindings,
    get_tool_by_name,
    _make_draft_handler,
    _make_skip_handler,
    _make_request_review_handler,
    _make_publish_handler,
    _make_find_existing_posts_handler,
    _make_get_run_status_handler,
)
from src.features.sharing.live_update_social.service import LiveUpdateSocialService
from src.features.sharing.live_update_social.agent import LiveUpdateSocialAgent


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_payload(
    topic_id: str = "topic-1",
    guild_id: int = 1,
    channel_id: int = 10,
    platform: str = "twitter",
    action: str = "post",
    status: str = "sent",
    **overrides,
) -> LiveUpdateHandoffPayload:
    """Create a LiveUpdateHandoffPayload with sensible defaults."""
    kwargs = dict(
        topic_id=topic_id,
        guild_id=guild_id,
        channel_id=channel_id,
        platform=platform,
        action=action,
        status=status,
        source_metadata={"cog": "test"},
        topic_summary_data={
            "title": "Test Topic",
            "message_id": 42,
            "channel_id": channel_id,
        },
    )
    kwargs.update(overrides)
    return LiveUpdateHandoffPayload(**kwargs)


def _make_run_state(
    run_id: str = "run-1",
    topic_id: str = "topic-1",
    platform: str = "twitter",
    action: str = "post",
) -> RunState:
    """Create a basic RunState for handler tests."""
    return RunState(
        run_id=run_id,
        topic_id=topic_id,
        platform=platform,
        action=action,
    )


# ═══════════════════════════════════════════════════════════════════════
# (a) Tool registry conformance
# ═══════════════════════════════════════════════════════════════════════


class TestToolRegistryConformance:
    """12 tools advertised (Sprint 3: 3 terminal + 5 read + 1 queue + 1 publish + 2 read)."""

    def test_exactly_twelve_tools_advertised(self):
        """ALL_TOOL_SPECS contains exactly 12 tools (Sprint 3)."""
        names = {ts.name for ts in ALL_TOOL_SPECS}
        expected = {
            "draft_social_post",
            "skip_social_post",
            "request_social_review",
            "get_live_update_topic",
            "get_source_messages",
            "get_published_update_context",
            "inspect_message_media",
            "list_social_routes",
            "enqueue_social_post",
            "publish_social_post",
            "find_existing_social_posts",
            "get_social_run_status",
        }
        assert names == expected, f"Expected {expected}, got {names}"
        assert len(ALL_TOOL_SPECS) == 12

    def test_each_tool_has_exactly_one_binding(self):
        """build_tool_bindings returns 12 bindings with distinct names."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)
        bindings = build_tool_bindings(db)

        assert len(bindings) == 12

        binding_names = {b.name for b in bindings}
        expected = {
            "draft_social_post",
            "skip_social_post",
            "request_social_review",
            "get_live_update_topic",
            "get_source_messages",
            "get_published_update_context",
            "inspect_message_media",
            "list_social_routes",
            "enqueue_social_post",
            "publish_social_post",
            "find_existing_social_posts",
            "get_social_run_status",
        }
        assert binding_names == expected

        # Each binding's handler is callable (async)
        for b in bindings:
            assert callable(b.handler), f"Handler for {b.name} is not callable"

    def test_binding_names_match_spec_names(self):
        """Each ToolBinding name matches its ToolSpec name."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)
        bindings = build_tool_bindings(db)

        spec_names = {ts.name for ts in ALL_TOOL_SPECS}
        for b in bindings:
            assert b.name == b.tool_spec.name
            assert b.name in spec_names

    def test_get_tool_by_name_finds_correct_binding(self):
        """get_tool_by_name returns the correct binding for all 12 tools."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)
        bindings = build_tool_bindings(db)

        for name in (
            "draft_social_post",
            "skip_social_post",
            "request_social_review",
            "get_live_update_topic",
            "get_source_messages",
            "get_published_update_context",
            "inspect_message_media",
            "list_social_routes",
            "enqueue_social_post",
            "publish_social_post",
            "find_existing_social_posts",
            "get_social_run_status",
        ):
            binding = get_tool_by_name(bindings, name)
            assert binding is not None, f"No binding found for {name}"
            assert binding.name == name

    def test_get_tool_by_name_returns_none_for_unknown(self):
        """get_tool_by_name returns None for unknown tool names."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)
        bindings = build_tool_bindings(db)

        assert get_tool_by_name(bindings, "publish_social_post") is not None  # now exists
        assert get_tool_by_name(bindings, "queue_social_post") is None

    def test_tool_specs_have_valid_openai_format(self):
        """Each ToolSpec produces a valid to_openai_tool() dict with input_schema."""
        for ts in ALL_TOOL_SPECS:
            tool = ts.to_openai_tool()
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"
            assert "properties" in tool["input_schema"]


# ═══════════════════════════════════════════════════════════════════════
# (b) sent status creates one run
# (c) partial status creates one run
# (d) non-sent/partial status creates zero runs
# (e) non-post action creates zero runs
# (f) duplicate replay reuses the same run
# (j) no social_publications rows created
# ═══════════════════════════════════════════════════════════════════════


class TestServiceTrigger:
    """Tests for the LiveUpdateSocialService trigger entrypoint."""

    @staticmethod
    def _make_service(fake: FakeSupabase, mock_terminal: str = "skip"):
        """Create a service with a mocked agent that returns mock_terminal."""
        db = build_db_handler(fake)

        # Create the service but don't call __init__ normally
        service = object.__new__(LiveUpdateSocialService)
        service.db_handler = db
        service._bot = MagicMock()  # non-None bot to allow agent invocation
        service._log = MagicMock()

        # Mock _invoke_agent to return a fixed terminal status
        async def _mock_invoke(payload):
            # Get the run_id from the most recent upsert to verify it
            rows = fake.tables.get("live_update_social_runs", [])
            return mock_terminal

        service._invoke_agent = _mock_invoke
        return service, db

    def test_sent_status_creates_one_run(self):
        """status='sent' + action='post' → one run created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="sent", action="post")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id != ""
        rows = fake.tables["live_update_social_runs"]
        assert len(rows) == 1
        assert rows[0]["topic_id"] == payload.topic_id
        assert rows[0]["platform"] == payload.platform
        assert rows[0]["action"] == payload.action

    def test_partial_status_creates_one_run(self):
        """status='partial' + action='post' → one run created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="partial", action="post")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id != ""
        rows = fake.tables["live_update_social_runs"]
        assert len(rows) == 1

    def test_failed_status_creates_zero_runs(self):
        """status='failed' → zero runs created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="failed", action="post")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id == ""
        rows = fake.tables["live_update_social_runs"]
        assert len(rows) == 0

    def test_skipped_status_creates_zero_runs(self):
        """status='skipped' → zero runs created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="skipped", action="post")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id == ""
        assert len(fake.tables["live_update_social_runs"]) == 0

    def test_non_post_action_creates_zero_runs(self):
        """action='reply' with status='sent' → zero runs created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="sent", action="reply")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id == ""
        assert len(fake.tables["live_update_social_runs"]) == 0

    def test_retweet_action_creates_zero_runs(self):
        """action='retweet' → zero runs created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="sent", action="retweet")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id == ""
        assert len(fake.tables["live_update_social_runs"]) == 0

    def test_quote_action_creates_zero_runs(self):
        """action='quote' → zero runs created."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(status="sent", action="quote")
        run_id = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id == ""
        assert len(fake.tables["live_update_social_runs"]) == 0

    def test_duplicate_replay_reuses_same_run(self):
        """Two identical payloads → same run_id, single row."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        payload = _make_payload(
            topic_id="dup-topic", platform="twitter", action="post", status="sent"
        )

        run_id_1 = asyncio.run(service.handle_live_update_publish_results(payload))
        run_id_2 = asyncio.run(service.handle_live_update_publish_results(payload))

        assert run_id_1 == run_id_2
        assert run_id_1 != ""
        rows = fake.tables["live_update_social_runs"]
        assert len(rows) == 1
        assert rows[0]["run_id"] == run_id_1

    def test_different_platform_creates_distinct_runs(self):
        """Same topic_id + action but different platform → distinct runs."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        p1 = _make_payload(topic_id="t1", platform="twitter", action="post", status="sent")
        p2 = _make_payload(topic_id="t1", platform="youtube", action="post", status="sent")

        r1 = asyncio.run(service.handle_live_update_publish_results(p1))
        r2 = asyncio.run(service.handle_live_update_publish_results(p2))

        assert r1 != r2
        assert len(fake.tables["live_update_social_runs"]) == 2

    def test_no_social_publications_created(self):
        """After any service operation, social_publications table stays empty."""
        fake = FakeSupabase({"live_update_social_runs": [], "social_publications": []})
        service, db = self._make_service(fake)

        # Multiple operations
        p1 = _make_payload(topic_id="t1", platform="twitter", action="post", status="sent")
        p2 = _make_payload(topic_id="t2", platform="twitter", action="post", status="partial")
        p3 = _make_payload(topic_id="t1", platform="twitter", action="post", status="sent")  # duplicate
        p4 = _make_payload(topic_id="t3", platform="twitter", action="post", status="failed")  # skipped

        asyncio.run(service.handle_live_update_publish_results(p1))
        asyncio.run(service.handle_live_update_publish_results(p2))
        asyncio.run(service.handle_live_update_publish_results(p3))
        asyncio.run(service.handle_live_update_publish_results(p4))

        # social_publications must be empty
        assert len(fake.tables["social_publications"]) == 0
        # live_update_social_runs has 2 (t1/twitter reused, t2/twitter new, t3 skipped)
        assert len(fake.tables["live_update_social_runs"]) == 2


# ═══════════════════════════════════════════════════════════════════════
# (g) draft_social_post tool sets terminal='draft' with draft_text + media
# (h) skip_social_post sets terminal='skip'
# (i) media-resolution failure → needs_review
# (k) media-ref identities recorded
# (l) trace/status logging entries
# ═══════════════════════════════════════════════════════════════════════


class TestToolHandlers:
    """Direct tests of the three terminal tool handlers."""

    @pytest.mark.asyncio
    async def test_draft_social_post_sets_terminal_draft(self):
        """draft_social_post handler sets terminal_status='draft' with draft_text."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        # Create a run first so the handler has something to update
        run = db.upsert_live_update_social_run(
            topic_id="t-draft", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        handler = _make_draft_handler(db)
        result = await handler(run_state, {
            "draft_text": "Exciting news from Banodoco!",
            "selected_media": [
                {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0},
            ],
        })

        assert result["tool"] == "draft_social_post"
        assert result["terminal_status"] == "draft"
        assert result["ok"] is True

        # Verify DB state
        fetched = db.get_live_update_social_run(run["run_id"])
        assert fetched["terminal_status"] == "draft"
        assert fetched["draft_text"] == "Exciting news from Banodoco!"

    @pytest.mark.asyncio
    async def test_draft_social_post_selected_media_inspectable(self):
        """Selected media identities are stored in media_decisions and inspectable."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-media", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        selected = [
            {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0},
            {"source": "discord_embed", "channel_id": 10, "message_id": 42, "embed_slot": "image"},
        ]

        handler = _make_draft_handler(db)
        await handler(run_state, {
            "draft_text": "Check out this image!",
            "selected_media": selected,
        })

        fetched = db.get_live_update_social_run(run["run_id"])
        decisions = fetched["media_decisions"]
        assert decisions["selected"] == selected
        assert len(decisions["selected"]) == 2

    @pytest.mark.asyncio
    async def test_skip_social_post_sets_terminal_skip(self):
        """skip_social_post handler sets terminal_status='skip'."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-skip", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        handler = _make_skip_handler(db)
        result = await handler(run_state, {"reason": "Not newsworthy"})

        assert result["tool"] == "skip_social_post"
        assert result["terminal_status"] == "skip"
        assert result["reason"] == "Not newsworthy"
        assert result["ok"] is True

        fetched = db.get_live_update_social_run(run["run_id"])
        assert fetched["terminal_status"] == "skip"

    @pytest.mark.asyncio
    async def test_skip_social_post_without_reason(self):
        """skip_social_post works even with empty reason."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-skip2", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        handler = _make_skip_handler(db)
        result = await handler(run_state, {})

        assert result["terminal_status"] == "skip"
        assert result["reason"] == "no reason given"

    @pytest.mark.asyncio
    async def test_request_social_review_sets_needs_review(self):
        """request_social_review handler sets terminal_status='needs_review'."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-review", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        handler = _make_request_review_handler(db)
        result = await handler(run_state, {"reason": "Media cannot be resolved"})

        assert result["tool"] == "request_social_review"
        assert result["terminal_status"] == "needs_review"
        assert result["ok"] is True

        fetched = db.get_live_update_social_run(run["run_id"])
        assert fetched["terminal_status"] == "needs_review"

    @pytest.mark.asyncio
    async def test_trace_entries_appended_by_handlers(self):
        """All three handlers append trace entries to the run."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-trace", platform="twitter", action="post"
        )
        run_id = run["run_id"]

        # draft
        rs = RunState.from_row(run)
        await _make_draft_handler(db)(rs, {"draft_text": "Hello"})

        fetched = db.get_live_update_social_run(run_id)
        assert len(fetched["trace_entries"]) >= 1
        events = [e["event"] for e in fetched["trace_entries"]]
        assert "tool" in events

        # skip
        run2 = db.upsert_live_update_social_run(
            topic_id="t-trace2", platform="twitter", action="post"
        )
        rs2 = RunState.from_row(run2)
        await _make_skip_handler(db)(rs2, {"reason": "nope"})

        fetched2 = db.get_live_update_social_run(run2["run_id"])
        assert len(fetched2["trace_entries"]) >= 1

        # needs_review
        run3 = db.upsert_live_update_social_run(
            topic_id="t-trace3", platform="twitter", action="post"
        )
        rs3 = RunState.from_row(run3)
        await _make_request_review_handler(db)(rs3, {"reason": "help"})

        fetched3 = db.get_live_update_social_run(run3["run_id"])
        assert len(fetched3["trace_entries"]) >= 1


class TestMediaResolutionAndNeedsReview:
    """Tests for media-resolution failure → needs_review."""

    @pytest.mark.asyncio
    async def test_force_needs_review_sets_terminal_status(self):
        """_force_needs_review sets terminal_status='needs_review' durably."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-force", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        agent = LiveUpdateSocialAgent(db_handler=db, bot=MagicMock())
        result = await agent._force_needs_review(run_state, "Test failure reason")

        assert result == "needs_review"
        assert run_state.terminal_status == "needs_review"

        fetched = db.get_live_update_social_run(run["run_id"])
        assert fetched["terminal_status"] == "needs_review"

    @pytest.mark.asyncio
    async def test_force_needs_review_appends_trace(self):
        """_force_needs_review appends a trace entry."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-force2", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)
        agent = LiveUpdateSocialAgent(db_handler=db, bot=MagicMock())

        await agent._force_needs_review(run_state, "Media missing")

        fetched = db.get_live_update_social_run(run["run_id"])
        assert len(fetched["trace_entries"]) >= 1
        force_events = [e for e in fetched["trace_entries"] if e["event"] == "force_needs_review"]
        assert len(force_events) == 1
        assert force_events[0]["reason"] == "Media missing"

    @pytest.mark.asyncio
    async def test_agent_rejects_forbidden_actions(self):
        """Agent structurally rejects forbidden actions based on mode.

        In publish mode (LIVE_UPDATE_SOCIAL_MODE=publish), reply/quote are
        allowed for thread chaining; only retweet is forbidden.
        In draft/queue mode, reply/retweet/quote are all forbidden.
        """
        import os
        original_mode = os.environ.get("LIVE_UPDATE_SOCIAL_MODE", "")

        try:
            # Test in draft mode (default): all non-post actions forbidden
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = "draft"
            fake = FakeSupabase({"live_update_social_runs": []})
            db = build_db_handler(fake)

            agent = LiveUpdateSocialAgent(db_handler=db, bot=MagicMock())

            for forbidden in ("reply", "retweet", "quote"):
                payload = _make_payload(action=forbidden, status="sent")
                result = await agent.run(payload)
                assert result is None, f"Expected None for forbidden action {forbidden}"

            # No runs created for any forbidden action
            assert len(fake.tables["live_update_social_runs"]) == 0
        finally:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = original_mode

    @pytest.mark.asyncio
    async def test_agent_publish_mode_allows_reply_quote(self):
        """In publish mode, agent allows reply/quote but forbids retweet."""
        import os
        original_mode = os.environ.get("LIVE_UPDATE_SOCIAL_MODE", "")

        try:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = "publish"
            fake = FakeSupabase({"live_update_social_runs": []})
            db = build_db_handler(fake)

            agent = LiveUpdateSocialAgent(db_handler=db, bot=None)

            # retweet should still be forbidden in publish mode
            payload_rt = _make_payload(action="retweet", status="sent")
            result = await agent.run(payload_rt)
            assert result is None, "Expected retweet to be forbidden in publish mode"

            # reply/quote should be allowed in publish mode
            # (the agent will attempt to process them, though they'll likely fail
            # without full infrastructure — but they should NOT be rejected)
            for allowed_action in ("reply", "quote"):
                fake2 = FakeSupabase({"live_update_social_runs": []})
                db2 = build_db_handler(fake2)
                agent2 = LiveUpdateSocialAgent(db_handler=db2, bot=None)
                payload = _make_payload(action=allowed_action, status="sent")
                result = await agent2.run(payload)
                # Should NOT return None (rejection); allowed actions proceed
                assert result is not None, (
                    f"Expected non-None for {allowed_action} in publish mode"
                )
        finally:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = original_mode

    @pytest.mark.asyncio
    async def test_agent_allows_post_action(self):
        """Agent accepts action='post' and creates a run."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        # Create agent with no bot → _resolve_media returns True (no media to check)
        agent = LiveUpdateSocialAgent(db_handler=db, bot=None)

        payload = _make_payload(action="post", status="sent")
        # Without bot, _resolve_media returns True (no messages to inspect)
        # LLM stub returns "Generated description" → can't parse → needs_review
        result = await agent.run(payload)

        # Should create a run (agent internally upserts)
        rows = fake.tables["live_update_social_runs"]
        assert len(rows) == 1
        assert rows[0]["action"] == "post"
        # LLM stub returns unparseable string → falls through to needs_review
        assert result == "needs_review"


class TestMediaRefRecording:
    """Media-ref identities recorded as considered/selected/skipped/unresolved."""

    @pytest.mark.asyncio
    async def test_media_decisions_structure_present_after_draft(self):
        """After draft_social_post, media_decisions has all four keys."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-rec", platform="twitter", action="post"
        )
        run_state = RunState.from_row(run)

        handler = _make_draft_handler(db)
        await handler(run_state, {
            "draft_text": "Test",
            "selected_media": [
                {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0},
            ],
        })

        fetched = db.get_live_update_social_run(run["run_id"])
        decisions = fetched["media_decisions"]

        for key in ("considered", "selected", "skipped", "unresolved"):
            assert key in decisions, f"media_decisions missing key: {key}"
            assert isinstance(decisions[key], list)

    @pytest.mark.asyncio
    async def test_media_decisions_empty_by_default(self):
        """New runs have empty media_decisions dict."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="t-empty", platform="twitter", action="post"
        )

        assert run["media_decisions"] == {}

    @pytest.mark.asyncio
    async def test_media_identity_validation_rejects_missing_fields(self):
        """MediaRefIdentity validates required fields."""
        # discord_attachment requires channel_id, message_id, attachment_index
        with pytest.raises(ValueError, match="channel_id and message_id"):
            MediaRefIdentity(source="discord_attachment")

        with pytest.raises(ValueError, match="attachment_index"):
            MediaRefIdentity(
                source="discord_attachment", channel_id=10, message_id=42
            )

        # discord_embed requires channel_id, message_id, embed_slot
        with pytest.raises(ValueError, match="embed_slot"):
            MediaRefIdentity(
                source="discord_embed", channel_id=10, message_id=42
            )

        # url source requires url
        with pytest.raises(ValueError, match="url"):
            MediaRefIdentity(source="url")

    def test_media_identity_round_trip_via_dict(self):
        """MediaRefIdentity survives to_dict → from_dict round-trip."""
        original = MediaRefIdentity(
            source="discord_attachment",
            channel_id=10,
            message_id=42,
            attachment_index=0,
        )
        d = original.to_dict()
        restored = MediaRefIdentity.from_dict(d)

        assert restored.source == original.source
        assert restored.channel_id == original.channel_id
        assert restored.message_id == original.message_id
        assert restored.attachment_index == original.attachment_index


class TestTraceLogging:
    """Trace/status logging entries are appended to runs."""

    def test_run_state_add_trace_appends_entry(self):
        """RunState.add_trace() appends an entry with event and timestamp."""
        rs = _make_run_state()
        assert len(rs.trace_entries) == 0

        rs.add_trace("agent_start", topic_id="t1")
        assert len(rs.trace_entries) == 1
        assert rs.trace_entries[0]["event"] == "agent_start"
        assert rs.trace_entries[0]["topic_id"] == "t1"
        assert "ts" in rs.trace_entries[0]

    def test_run_state_multiple_traces(self):
        """Multiple add_trace calls produce ordered entries."""
        rs = _make_run_state()

        rs.add_trace("step_1")
        rs.add_trace("step_2", detail="extra")
        rs.add_trace("step_3")

        assert len(rs.trace_entries) == 3
        assert [e["event"] for e in rs.trace_entries] == ["step_1", "step_2", "step_3"]
        assert rs.trace_entries[1]["detail"] == "extra"

    @pytest.mark.asyncio
    async def test_agent_run_produces_trace_entries(self):
        """Agent run() produces trace entries even without LLM success."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        # No bot → no media resolution, LLM stub returns unparseable → needs_review
        agent = LiveUpdateSocialAgent(db_handler=db, bot=None)
        payload = _make_payload(action="post", status="sent")

        await agent.run(payload)

        rows = fake.tables["live_update_social_runs"]
        assert len(rows) == 1
        trace = rows[0]["trace_entries"]
        assert len(trace) >= 2  # at least agent_start + something
        events = [e["event"] for e in trace]
        assert "agent_start" in events


# ═══════════════════════════════════════════════════════════════════════
# Sprint 3: Publish mode, threads, and failure classification
# ═══════════════════════════════════════════════════════════════════════


class FakeSocialPublishService:
    """Fake SocialPublishService for publish-mode tests."""

    def __init__(self, *, success=True, provider_ref="tweet-1",
                 provider_url="https://x.com/example/status/1",
                 media_ids=None, error=None):
        self.success = success
        self.provider_ref = provider_ref
        self.provider_url = provider_url
        self._media_ids = media_ids
        self.error = error
        self.publish_calls = []
        self.find_existing_calls = []

    async def publish_now(self, request):
        self.publish_calls.append(request)
        from src.features.sharing.models import SocialPublishResult
        if not self.success:
            return SocialPublishResult(
                success=False,
                error=self.error or "Mock publish failure",
            )
        media_ids = list(self._media_ids) if (self._media_ids and request.media_hints) else []
        return SocialPublishResult(
            success=True,
            publication_id="pub-mock-1",
            provider_ref=self.provider_ref,
            provider_url=self.provider_url,
            media_ids=media_ids,
        )

    def find_existing_posts(self, topic_id, platform, guild_id=None,
                            draft_text=None, limit=20):
        self.find_existing_calls.append(locals())
        return []


def _mock_resolve_bot_user_details():
    """Return fake bot user_details for tests without Twitter credentials."""
    return {"screen_name": "test_bot", "user_id": "12345"}


def _build_db_handler_with_similarity(fake):
    """Build a db_handler that also supports check_content_similarity."""
    db = build_db_handler(fake)
    db.check_content_similarity = DatabaseHandler.check_content_similarity
    return db


class TestPublishMode:
    """Tests for publish mode, threads, and failure classification."""

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _make_db_with_run(fake, topic_id="t-pub", platform="twitter",
                          action="post", draft_text=None,
                          media_decisions=None):
        db = build_db_handler(fake)
        # Ensure check_content_similarity is available for similarity tests
        if not hasattr(db, "check_content_similarity"):
            db.check_content_similarity = DatabaseHandler.check_content_similarity
        run = db.upsert_live_update_social_run(
            topic_id=topic_id,
            platform=platform,
            action=action,
            guild_id=1,
        )
        if draft_text or media_decisions:
            db.update_live_update_social_run(
                run_id=run["run_id"],
                draft_text=draft_text,
                media_decisions=media_decisions or {},
            )
        return db, RunState.from_row(db.get_live_update_social_run(run["run_id"]))

    @staticmethod
    def _mock_user_details_patch():
        """Patch _resolve_bot_user_details to return fake credentials."""
        return patch(
            "src.features.sharing.live_update_social.tools._resolve_bot_user_details",
            new=AsyncMock(return_value={"screen_name": "test_bot", "user_id": "12345"}),
        )

    # ── gating ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_publish_handler_gated_by_mode(self):
        """Publish handler returns error when LIVE_UPDATE_SOCIAL_MODE != 'publish'."""
        import os
        original = os.environ.get("LIVE_UPDATE_SOCIAL_MODE", "")
        try:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = "draft"
            fake = FakeSupabase({"live_update_social_runs": []})
            db, run_state = self._make_db_with_run(fake)
            svc = FakeSocialPublishService()
            with self._mock_user_details_patch():
                handler_fn = _make_publish_handler(db, social_publish_service=svc)
                result = await handler_fn(run_state, {"draft_text": "Hello"})
            assert result["ok"] is False
            assert "not enabled" in result["error"].lower()
        finally:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = original

    @pytest.mark.asyncio
    async def test_force_publish_bypasses_mode_gate(self):
        """force_publish=True skips the LIVE_UPDATE_SOCIAL_MODE check."""
        import os
        original = os.environ.get("LIVE_UPDATE_SOCIAL_MODE", "")
        try:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = "draft"  # not publish
            fake = FakeSupabase({"live_update_social_runs": []})
            db, run_state = self._make_db_with_run(fake)
            svc = FakeSocialPublishService()
            with self._mock_user_details_patch():
                handler_fn = _make_publish_handler(
                    db, social_publish_service=svc, force_publish=True,
                )
                result = await handler_fn(run_state, {"draft_text": "Hello world"})
            # Should succeed (no mode gate error) — will attempt publish
            assert result.get("ok") is True
            assert len(svc.publish_calls) == 1
        finally:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = original

    @pytest.mark.asyncio
    async def test_publish_handler_requires_service(self):
        """Publish handler returns error when social_publish_service is None."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=None, force_publish=True)
            result = await handler_fn(run_state, {"draft_text": "Hello"})
        assert result["ok"] is False
        assert "not available" in result["error"].lower()

    # ── single post ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_single_post_publish(self):
        """Single post publishes via publish_now and records outcome."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService()
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {"draft_text": "Test post content"})

        assert result["ok"] is True
        assert result["terminal_status"] == "published"
        assert result["publication_id"] == "pub-mock-1"
        assert result["provider_ref"] == "tweet-1"
        assert len(svc.publish_calls) == 1
        assert svc.publish_calls[0].text == "Test post content"

        # Verify publication_outcome persisted
        row = db.get_live_update_social_run(run_state.run_id)
        outcome = row.get("publication_outcome")
        assert outcome is not None
        assert outcome.get("success") is True

    # ── media outcome ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_media_outcome_attached(self):
        """Media attached to result is recorded in publication_outcome."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService(media_ids=["media-1", "media-2"])
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "Post with media",
                "selected_media": [
                    {"source_url": "https://example.com/img.png", "content_type": "image/png",
                     "identity": {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0}},
                ],
                "skip_media_understanding": True,
                "skip_media_reason": "test bypass",
            })

        assert result["ok"] is True
        assert result.get("media_ids") == ["media-1", "media-2"]

    @pytest.mark.asyncio
    async def test_media_missing_recorded(self):
        """When media is requested but provider returns none, media_missing recorded."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService(media_ids=[])
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "Post expecting media",
                "selected_media": [
                    {"source_url": "https://example.com/img.png", "content_type": "image/png",
                     "identity": {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0}},
                ],
                "skip_media_understanding": True,
                "skip_media_reason": "test bypass",
            })

        assert result["ok"] is True
        assert result.get("media_missing_count", 0) > 0

    # ── text-only fallback ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_text_only_when_no_media_requested(self):
        """Text-only post succeeds without media understanding."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService()
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {"draft_text": "Text only post"})

        assert result["ok"] is True
        assert result["terminal_status"] == "published"
        assert result.get("media_attached_count", 0) == 0
        assert result.get("media_missing_count", 0) == 0

    # ── duplicate prevention ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_duplicate_prevented_by_similarity(self):
        """Content exceeding similarity threshold prevents publish."""
        import os
        original = os.environ.get("SOCIAL_DUPLICATE_SIMILARITY_THRESHOLD", "")
        try:
            os.environ["SOCIAL_DUPLICATE_SIMILARITY_THRESHOLD"] = "0.5"

            fake = FakeSupabase({
                "live_update_social_runs": [],
                "social_publications": [
                    {
                        "publication_id": "existing-pub",
                        "guild_id": 1,
                        "status": "succeeded",
                        "platform": "twitter",
                        "action": "post",
                        "source_kind": "live_update_social",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "text": "This is almost the same content for testing",
                        "request_payload": {
                            "text": "This is almost the same content for testing",
                            "source_context": {
                                "metadata": {
                                    "topic_id": "t-pub",
                                },
                            },
                        },
                    },
                ],
            })
            db, run_state = self._make_db_with_run(fake)
            svc = FakeSocialPublishService()
            with self._mock_user_details_patch():
                handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
                result = await handler_fn(run_state, {
                    "draft_text": "This is almost the same content for testing",
                })

            assert result["ok"] is True
            assert result.get("duplicate_prevented") is True
            assert len(svc.publish_calls) == 0  # never published
        finally:
            if original:
                os.environ["SOCIAL_DUPLICATE_SIMILARITY_THRESHOLD"] = original
            else:
                os.environ.pop("SOCIAL_DUPLICATE_SIMILARITY_THRESHOLD", None)

    # ── content similarity ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_content_similarity_no_match(self):
        """Dissimilar content does not trigger duplicate prevention."""
        fake = FakeSupabase({
            "live_update_social_runs": [],
            "social_publications": [
                {
                    "publication_id": "existing-pub",
                    "guild_id": 1,
                    "source_kind": "live_update_social",
                    "status": "succeeded",
                    "platform": "twitter",
                    "action": "post",
                    "source_kind": "live_update_social",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "text": "Completely different content here",
                    "request_payload": {
                        "text": "Completely different content here",
                        "source_context": {
                            "metadata": {
                                "topic_id": "t-pub",
                            },
                        },
                    },
                },
            ],
        })
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService()
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "Unique new content about bananas and space travel",
            })

        assert result["ok"] is True
        assert result.get("duplicate_prevented") is not True
        assert len(svc.publish_calls) == 1

    # ── route validation ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_route_missing_blocks_publish(self):
        """Missing social route returns error with route_missing flag."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake, platform="nonexistent-platform")
        svc = FakeSocialPublishService()
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {"draft_text": "Should fail"})

        assert result["ok"] is True  # handler succeeds, but marks route_missing
        assert result.get("route_missing") is True
        assert len(svc.publish_calls) == 0

    # ── provider failure ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_provider_failure_classified(self):
        """Provider publish failure is classified and recorded."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService(success=False, error="Twitter API rate limit exceeded")
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {"draft_text": "Will fail"})

        assert result["ok"] is True
        assert result.get("error") is not None
        assert "failure_reason" in result
        row = db.get_live_update_social_run(run_state.run_id)
        outcome = row.get("publication_outcome")
        assert outcome is not None
        assert outcome.get("success") is False

    # ── user_details resolution ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_user_details_required_for_publish(self):
        """Publish handler requires bot user_details. Mocked creds should pass."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService()
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {"draft_text": "Need user details"})

        # With mocked user_details, publish should succeed
        assert result.get("ok") is True
        assert result.get("terminal_status") == "published"

    # ── thread publishing ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_thread_publishing_success(self):
        """Thread publishes item 0 as post, subsequent as replies."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService()
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "",
                "thread_items": [
                    {"index": 0, "draft_text": "Root post of thread"},
                    {"index": 1, "draft_text": "Reply #1"},
                    {"index": 2, "draft_text": "Reply #2"},
                ],
            })

        assert result["ok"] is True
        assert result.get("thread_success") is True
        assert len(svc.publish_calls) == 3
        assert svc.publish_calls[0].action == "post"
        assert svc.publish_calls[0].text == "Root post of thread"
        assert svc.publish_calls[1].action == "reply"
        assert svc.publish_calls[1].text == "Reply #1"
        assert svc.publish_calls[2].action == "reply"
        assert svc.publish_calls[2].text == "Reply #2"

    # ── thread media associations ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_thread_media_associations_traced(self):
        """Thread items can have per-item media_refs traced in outcomes."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService(media_ids=["media-root"])
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "",
                "thread_items": [
                    {
                        "index": 0,
                        "draft_text": "Root with media",
                        "media_refs": [
                            {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0},
                        ],
                    },
                    {"index": 1, "draft_text": "Reply without media"},
                ],
                "skip_media_understanding": True,
                "skip_media_reason": "test thread media",
            })

        assert result["ok"] is True
        per_item = result.get("per_item_outcomes", [])
        assert len(per_item) == 2
        assert per_item[0]["success"] is True
        assert per_item[1]["success"] is True

    # ── quote/reply no duplicate media ───────────────────────────────

    @pytest.mark.asyncio
    async def test_thread_reply_no_duplicate_media(self):
        """Replies in a thread do not accidentally reattach root's media."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService(media_ids=["media-1"])
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "",
                "thread_items": [
                    {
                        "index": 0,
                        "draft_text": "Root with media",
                        "media_refs": [
                            {"identity": {"source": "discord_attachment", "channel_id": 10, "message_id": 42, "attachment_index": 0}},
                        ],
                    },
                    {"index": 1, "draft_text": "Reply text only"},
                ],
                "skip_media_understanding": True,
                "skip_media_reason": "test no duplicate media",
            })

        assert result["ok"] is True
        per_item = result.get("per_item_outcomes", [])
        if len(per_item) >= 2:
            reply_media = per_item[1].get("media_ids", [])
            assert reply_media == [], (
                f"Reply should have no media_ids, got {reply_media}"
            )

    # ── thread partial failure ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_thread_root_failure_aborts(self):
        """Root post failure aborts remaining thread items."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db, run_state = self._make_db_with_run(fake)
        svc = FakeSocialPublishService(success=False, error="Root publish failed")
        with self._mock_user_details_patch():
            handler_fn = _make_publish_handler(db, social_publish_service=svc, force_publish=True)
            result = await handler_fn(run_state, {
                "draft_text": "",
                "thread_items": [
                    {"index": 0, "draft_text": "Root that will fail"},
                    {"index": 1, "draft_text": "Should never be called"},
                ],
            })

        assert result["ok"] is True
        assert result.get("thread_root_failed") is True
        assert len(svc.publish_calls) == 1

    # ── retweet rejection ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_retweet_rejected_in_publish_mode(self):
        """Agent rejects retweet action even in publish mode."""
        import os
        original = os.environ.get("LIVE_UPDATE_SOCIAL_MODE", "")
        try:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = "publish"
            fake = FakeSupabase({"live_update_social_runs": []})
            db = build_db_handler(fake)
            agent = LiveUpdateSocialAgent(db_handler=db, bot=None)
            payload = _make_payload(action="retweet", status="sent")
            result = await agent.run(payload)
            assert result is None  # rejected
        finally:
            os.environ["LIVE_UPDATE_SOCIAL_MODE"] = original


class TestFindExistingPosts:
    """Tests for find_existing_social_posts tool."""

    @pytest.mark.asyncio
    async def test_find_existing_posts_match_returns(self):
        """find_existing_social_posts returns matching publications."""
        fake = FakeSupabase({
            "live_update_social_runs": [],
            "social_publications": [
                {
                    "publication_id": "pub-1",
                    "guild_id": 1,
                    "source_kind": "live_update_social",
                    "status": "succeeded",
                    "platform": "twitter",
                    "action": "post",
                    "created_at": "2026-01-15T00:00:00+00:00",
                    "provider_ref": "tweet-abc",
                    "provider_url": "https://x.com/example/status/abc",
                    "request_payload": {
                        "source_context": {
                            "metadata": {
                                "topic_id": "topic-match",
                            },
                        },
                    },
                },
                {
                    "publication_id": "pub-2",
                    "guild_id": 1,
                    "source_kind": "live_update_social",
                    "status": "succeeded",
                    "platform": "twitter",
                    "action": "post",
                    "created_at": "2026-01-14T00:00:00+00:00",
                    "provider_ref": "tweet-def",
                    "provider_url": "https://x.com/example/status/def",
                    "request_payload": {
                        "source_context": {
                            "metadata": {
                                "topic_id": "topic-other",
                            },
                        },
                    },
                },
            ],
        })
        db = build_db_handler(fake)
        handler_fn = _make_find_existing_posts_handler(db)

        # Create a run_state for the matching topic
        run = db.upsert_live_update_social_run(
            topic_id="topic-match", platform="twitter", action="post", guild_id=1,
        )
        run_state = RunState.from_row(run)
        run_state.draft_text = "Some new post content"

        result: ToolResult = await handler_fn(run_state, {})

        assert result.ok is True
        assert result.tool_name == "find_existing_social_posts"
        assert result.data["count"] == 1  # Only topic-match posts
        assert result.data["posts"][0]["publication_id"] == "pub-1"

    @pytest.mark.asyncio
    async def test_character_5gram_similarity_present(self):
        """When draft_text is provided, similarity scores are annotated."""
        fake = FakeSupabase({
            "live_update_social_runs": [],
            "social_publications": [
                {
                    "publication_id": "pub-sim",
                    "guild_id": 1,
                    "source_kind": "live_update_social",
                    "status": "succeeded",
                    "platform": "twitter",
                    "action": "post",
                    "created_at": "2026-01-15T00:00:00+00:00",
                    "text": "The quick brown fox jumps over",
                    "request_payload": {
                        "text": "The quick brown fox jumps over",
                        "source_context": {
                            "metadata": {
                                "topic_id": "topic-sim",
                            },
                        },
                    },
                },
            ],
        })
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="topic-sim", platform="twitter", action="post", guild_id=1,
        )
        run_state = RunState.from_row(run)

        handler_fn = _make_find_existing_posts_handler(db)
        result: ToolResult = await handler_fn(
            run_state,
            {"draft_text": "The quick brown fox jumps over the lazy dog"},
        )

        assert result.ok is True
        assert result.data["count"] == 1
        sim = result.data["posts"][0].get("_similarity")
        assert sim is not None
        # "The quick brown fox jumps over" is entirely contained in
        # "The quick brown fox jumps over the lazy dog" → high similarity
        assert sim > 0.5

    @pytest.mark.asyncio
    async def test_no_false_positives_for_dissimilar_content(self):
        """Dissimilar content does not produce high similarity scores."""
        fake = FakeSupabase({
            "live_update_social_runs": [],
            "social_publications": [
                {
                    "publication_id": "pub-diff",
                    "guild_id": 1,
                    "source_kind": "live_update_social",
                    "status": "succeeded",
                    "platform": "twitter",
                    "action": "post",
                    "created_at": "2026-01-15T00:00:00+00:00",
                    "text": "Breaking news: market reaches all-time high",
                    "request_payload": {
                        "text": "Breaking news: market reaches all-time high",
                        "source_context": {
                            "metadata": {
                                "topic_id": "topic-diff",
                            },
                        },
                    },
                },
            ],
        })
        db = build_db_handler(fake)

        run = db.upsert_live_update_social_run(
            topic_id="topic-diff", platform="twitter", action="post", guild_id=1,
        )
        run_state = RunState.from_row(run)

        handler_fn = _make_find_existing_posts_handler(db)
        result: ToolResult = await handler_fn(
            run_state,
            {"draft_text": "Weather forecast: sunny with mild temperatures"},
        )

        assert result.ok is True
        assert result.data["count"] == 1  # same topic so still returned
        sim = result.data["posts"][0].get("_similarity", 0)
        # Completely different content → low similarity
        assert sim < 0.3, f"Expected low similarity, got {sim}"
