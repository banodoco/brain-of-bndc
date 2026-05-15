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

from src.features.sharing.live_update_social.contracts import LiveUpdateHandoffPayload
from src.features.sharing.live_update_social.models import (
    MediaRefIdentity,
    RunState,
    ToolSpec,
    ToolBinding,
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
    build_tool_bindings,
    get_tool_by_name,
    _make_draft_handler,
    _make_skip_handler,
    _make_request_review_handler,
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
    """Exactly 3 tools advertised, each with exactly 1 handler binding."""

    def test_exactly_eight_tools_advertised(self):
        """ALL_TOOL_SPECS contains exactly 3 terminal + 5 read + 1 queue tool."""
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
        }
        assert names == expected, f"Expected {expected}, got {names}"
        assert len(ALL_TOOL_SPECS) == 9

    def test_each_tool_has_exactly_one_binding(self):
        """build_tool_bindings returns 9 bindings with distinct names."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)
        bindings = build_tool_bindings(db)

        assert len(bindings) == 9

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
        """get_tool_by_name returns the correct binding for each tool."""
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
        ):
            binding = get_tool_by_name(bindings, name)
            assert binding is not None, f"No binding found for {name}"
            assert binding.name == name

    def test_get_tool_by_name_returns_none_for_unknown(self):
        """get_tool_by_name returns None for unknown tool names."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)
        bindings = build_tool_bindings(db)

        assert get_tool_by_name(bindings, "publish_social_post") is None
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
        """Agent structurally rejects reply/retweet/quote at entry."""
        fake = FakeSupabase({"live_update_social_runs": []})
        db = build_db_handler(fake)

        agent = LiveUpdateSocialAgent(db_handler=db, bot=MagicMock())

        for forbidden in ("reply", "retweet", "quote"):
            payload = _make_payload(action=forbidden, status="sent")
            result = await agent.run(payload)
            assert result is None, f"Expected None for forbidden action {forbidden}"

        # No runs created for any forbidden action
        assert len(fake.tables["live_update_social_runs"]) == 0

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
