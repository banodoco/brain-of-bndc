import asyncio

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.common.base_bot import _claim_startup_notification, _current_commit_sha
from src.features.admin_chat.agent import AdminChatResult, AdminChatAgent, _conversations
from src.features.admin_chat.admin_chat_cog import AdminChatCog, _preview_text


def test_strip_fallback_reply_lines_keeps_real_answer():
    content = "\n".join([
        "You restarted me twice in four minutes.",
        "I hit an internal error while trying to do that.",
        "Pom. You're back. Looks stable now.",
    ])

    assert AdminChatCog._strip_fallback_reply_lines(content) == "\n".join([
        "You restarted me twice in four minutes.",
        "Pom. You're back. Looks stable now.",
    ])


def test_preview_text_does_not_cut_mid_word():
    preview = _preview_text(
        "Plus you've been restarting me, debugging the multi-message bug, and poking at me in DMs for an hour and a half.",
        76,
    )

    assert preview.endswith("...")
    assert "pokin..." not in preview
    assert preview.endswith("and...")


def test_current_commit_sha_prefers_available_env(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_SHA", "abcdef123456")

    assert _current_commit_sha() == "abcdef1"


def test_admin_chat_claim_uses_shared_event_lock(monkeypatch):
    calls = []

    class FakeDB:
        def try_claim_bot_event(self, **kwargs):
            calls.append(kwargs)
            return False

    bot = SimpleNamespace(payment_service=None)
    cog = AdminChatCog(bot, FakeDB(), sharer=object())
    message = SimpleNamespace(
        id=123,
        channel=SimpleNamespace(id=456),
        author=SimpleNamespace(id=789),
    )

    assert cog._claim_admin_chat_message(message) is False
    assert calls[0]["event_key"] == "admin_chat_message:123"
    assert calls[0]["event_type"] == "admin_chat_message"


def test_startup_notification_claim_uses_cooldown_bucket(monkeypatch):
    calls = []

    class FakeDB:
        def try_claim_bot_event(self, **kwargs):
            calls.append(kwargs)
            return False

    monkeypatch.setattr("src.common.base_bot.time.time", lambda: 1234)
    bot = SimpleNamespace(db_handler=FakeDB())

    assert _claim_startup_notification(bot, 42, "abcdef1") is False
    assert calls[0]["event_key"] == "startup_admin_dm:42:2"
    assert calls[0]["event_type"] == "startup_admin_dm"


# --- AdminChatResult return-contract tests (T3 / Step 5A) ---


def _make_tool_use_block(name: str, input_: dict, block_id: str = "toolu_001"):
    """Return a mock content block shaped like DeepSeek's tool_use."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_
    block.id = block_id
    return block


def _make_text_block(text: str):
    """Return a mock content block shaped like DeepSeek's text."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(*blocks):
    """Return a mock LLM response whose .content is a list of blocks."""
    resp = MagicMock()
    resp.content = list(blocks)
    resp.stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return resp


class TestAdminChatResultContract:
    """Verify AdminChatResult struct and that chat() returns it on every path."""

    def test_dataclass_fields(self):
        result = AdminChatResult(replies=["hello"], actions=[{"tool": "reply", "input": {}, "result": {"success": True}}])
        assert result.replies == ["hello"]
        assert len(result.actions) == 1
        assert result.actions[0]["tool"] == "reply"

    def test_silent_turn_replies_none_actions_nonempty(self):
        """Tool-only (silent) turn: replies is None, actions is non-empty."""
        result = AdminChatResult(replies=None, actions=[{"tool": "send_message", "input": {}, "result": {"success": True}}])
        assert result.replies is None
        assert len(result.actions) == 1

    @pytest.mark.asyncio
    async def test_chat_clear_returns_adminchatresult(self, monkeypatch):
        """The clear/reset command path returns AdminChatResult."""
        monkeypatch.setattr(
            "src.features.admin_chat.tools.TOOLS",
            [{"name": "reply", "description": "Reply to the user", "parameters": {}}],
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        db = MagicMock()
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)

        # Stub DeepSeekClient so it isn't called for clear
        agent.client = MagicMock()

        # Clear any prior conversation
        _conversations.clear()

        result = await agent.chat(user_id=1, user_message="/clear")
        assert isinstance(result, AdminChatResult)
        assert result.replies == ["Conversation cleared!"]
        assert result.actions == []

    @pytest.mark.asyncio
    async def test_chat_tool_call_returns_replies_and_actions(self, monkeypatch):
        """A turn that calls reply tool returns both replies and actions."""
        monkeypatch.setattr(
            "src.features.admin_chat.tools.TOOLS",
            [
                {"name": "reply", "description": "Reply to the user", "parameters": {}},
                {"name": "end_turn", "description": "End turn silently", "parameters": {}},
            ],
        )

        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        db = MagicMock()
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)

        # Stub the client to return a reply tool call
        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            return_value=_make_response(
                _make_tool_use_block("reply", {"message": "Hello, feedback logged!"}, "tu_1"),
            )
        )
        agent.client = fake_client

        # Stub execute_tool to return success
        monkeypatch.setattr(
            "src.features.admin_chat.agent.execute_tool",
            AsyncMock(return_value={"success": True, "messages": ["Hello, feedback logged!"]}),
        )

        _conversations.clear()

        result = await agent.chat(user_id=1, user_message="log feedback")
        assert isinstance(result, AdminChatResult)
        assert result.replies == ["Hello, feedback logged!"]
        assert len(result.actions) == 1
        assert result.actions[0]["tool"] == "reply"

    @pytest.mark.asyncio
    async def test_chat_silent_tool_turn_replies_none_actions_nonempty(self, monkeypatch):
        """A turn with only a channel-posting tool (send_message) returns
        replies=None (suppressed) but actions non-empty."""
        monkeypatch.setattr(
            "src.features.admin_chat.tools.TOOLS",
            [
                {"name": "send_message", "description": "Send a message", "parameters": {}},
                {"name": "end_turn", "description": "End turn silently", "parameters": {}},
            ],
        )

        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        db = MagicMock()
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)

        # First response: send_message tool call
        # Second response: end_turn (to finish the loop)
        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            side_effect=[
                _make_response(
                    _make_tool_use_block("send_message", {"channel_id": "123", "content": "updated!"}, "tu_1"),
                ),
                _make_response(
                    _make_tool_use_block("end_turn", {}, "tu_2"),
                ),
            ]
        )
        agent.client = fake_client

        monkeypatch.setattr(
            "src.features.admin_chat.agent.execute_tool",
            AsyncMock(return_value={"success": True}),
        )

        _conversations.clear()

        result = await agent.chat(user_id=1, user_message="update the post")
        assert isinstance(result, AdminChatResult)
        # replies suppressed because send_message is a channel-posting tool
        assert result.replies is None
        assert len(result.actions) >= 1
        assert any(a["tool"] == "send_message" for a in result.actions)


# ── T12: Cog-level live-update feedback routing/ack/fallback/audit tests ─────


# Shared helpers for building mock Discord objects


def _make_mock_message(*, content: str = "test", author_id: int = 999,
                       channel_id: int = 456, guild_id: int = 789,
                       message_id: int = 111, mentions: list = None,
                       reference_message_id: int = None,
                       reference_resolved = None,
                       is_dm: bool = False):
    """Build a mock discord.Message with configurable reply/reference state."""
    msg = MagicMock()
    msg.id = message_id
    msg.content = content
    msg.author = SimpleNamespace(id=author_id, bot=False,
                                  display_name=f"User{author_id}")
    if is_dm:
        msg.guild = None
        msg.channel = MagicMock()
        msg.channel.id = channel_id
        msg.channel.name = None
        # Make isinstance check work
        import discord
        type(msg.channel).__bases__ = (discord.DMChannel,)
    else:
        # Guild needs get_member for _strip_mention
        _guild = MagicMock()
        _guild.id = guild_id

        def _fake_get_member(user_id):
            m = MagicMock()
            m.top_role = SimpleNamespace(name="Admin")
            return m

        _guild.get_member = _fake_get_member
        msg.guild = _guild
        msg.channel = MagicMock()
        msg.channel.id = channel_id
        msg.channel.name = "test-channel"
    msg.mentions = list(mentions) if mentions else []
    # Build reference
    if reference_message_id is not None:
        ref = SimpleNamespace()
        ref.message_id = reference_message_id
        ref.resolved = reference_resolved
        msg.reference = ref
    else:
        msg.reference = None
    msg.add_reaction = AsyncMock()
    msg.delete = AsyncMock()
    # channel.send for _send_with_retry (cog sends replies)
    msg.channel.send = AsyncMock()
    # channel.fetch_message for best-effort parent hydration
    msg.channel.fetch_message = AsyncMock(
        return_value=SimpleNamespace(
            id=reference_message_id,
            author=SimpleNamespace(display_name="Bot"),
            content="Original update post",
        )
    )
    # channel.history for recent messages context
    async def _empty_history(limit=10):
        return
        yield  # make async generator
    msg.channel.history = _empty_history
    return msg


def _make_topic(*, topic_id: str = "t-1",
                headline: str = "Test Update",
                summary: str = "Test body",
                discord_message_ids: list = None,
                publication_status: str = "sent",
                state: str = "posted"):
    return {
        "topic_id": topic_id,
        "headline": headline,
        "summary": summary,
        "discord_message_ids": discord_message_ids or [9001, 9002],
        "publication_status": publication_status,
        "state": state,
    }


def _make_feedback_row(*, feedback_id: str = "fb-1",
                       topic_id: str = "t-1",
                       disposition: str = "correction",
                       verdict: str = None,
                       feedback_text: str = "Needs fix"):
    return {
        "feedback_id": feedback_id,
        "topic_id": topic_id,
        "disposition": disposition,
        "verdict": verdict,
        "admin_user_id": 999,
        "feedback_text": feedback_text,
        "replied_to_message_id": 9001,
    }


# ── Cog test fixture ──


def _build_cog(monkeypatch, *, dev_mode: bool = False,
               live_write_allowed: bool = True):
    """Build an AdminChatCog with mocked bot/db_handler/agent suitable for
    calling _handle_admin_message directly.

    Returns (cog, db_calls) where db_calls is a list that collects every
    db_handler method call for later assertion.
    """
    db_calls = []

    # Mock asyncio.to_thread to run synchronously in tests
    async def _fake_to_thread(fn, *args, **kwargs):
        db_calls.append(("to_thread", fn.__name__ if hasattr(fn, "__name__") else str(fn)))
        return fn(*args, **kwargs)
    monkeypatch.setattr("asyncio.to_thread", _fake_to_thread)

    bot = SimpleNamespace(
        user=SimpleNamespace(id=777),
        guilds=[SimpleNamespace(id=789)],
    )

    db = MagicMock()
    db.dev_mode = dev_mode
    db.server_config = MagicMock()
    db.server_config.get_channel_agent_guidance = MagicMock(return_value=None)
    db.server_config.get_server_field = MagicMock(return_value=None)

    # Default reader methods
    db.get_topic_by_discord_message_id = MagicMock(return_value=None)
    db.get_topic_ground_truth = MagicMock(return_value=[])
    db.get_author_context_snapshots = MagicMock(return_value={})
    db.get_topic_editor_author_profile = MagicMock(return_value={})
    db.get_live_update_feedback_for = MagicMock(return_value=None)
    db.store_live_update_feedback = MagicMock(return_value={"feedback_id": "fb-1"})
    db.update_topic = MagicMock(
        return_value={"topic_id": "t-1", "state": "deleted"})
    db.try_claim_bot_event = MagicMock(return_value=True)
    db.get_active_intent_for_recipient = MagicMock(return_value=None)

    db._live_write_allowed = MagicMock(return_value=live_write_allowed)

    # Collect calls through wrappers
    def _record_call(name):
        def wrapper(*args, **kwargs):
            db_calls.append((name, args, kwargs))
            if name == "get_topic_by_discord_message_id":
                return db._get_topic_result
            if name == "get_live_update_feedback_for":
                return db._get_feedback_result
            if name == "store_live_update_feedback":
                return db._store_feedback_result
            if name == "update_topic":
                return db._update_topic_result
            return None
        return wrapper

    db._get_topic_result = None
    db._get_feedback_result = None
    db._store_feedback_result = {"feedback_id": "fb-fallback"}
    db._update_topic_result = {"topic_id": "t-1", "state": "deleted"}

    # Replace with recording wrappers
    db.get_topic_by_discord_message_id = _record_call("get_topic_by_discord_message_id")
    db.get_live_update_feedback_for = _record_call("get_live_update_feedback_for")
    db.store_live_update_feedback = _record_call("store_live_update_feedback")
    db.update_topic = _record_call("update_topic")

    sharer = MagicMock()

    cog = AdminChatCog(bot, db, sharer)

    # Mock agent
    cog.agent = MagicMock()
    cog.agent.chat = AsyncMock()

    # Mock _ensure_agent
    cog._ensure_agent = MagicMock()

    # Mock channel history iteration
    async def _fake_history(limit=10):
        # Yield nothing (empty history)
        return
        yield  # make it an async generator

    return cog, db, db_calls


# ── Test (a): Reply resolving to feed item runs turn without @mention ────────


class TestFeedbackReplyRouting:
    """Cog-level tests: reply routing, reverse-lookup, turn execution."""

    @pytest.mark.asyncio
    async def test_reply_to_feed_item_runs_turn_without_mention(self, monkeypatch):
        """(a) A reply whose parent resolves to a feed item triggers an agent
        turn even when the bot is not @mentioned."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        cog.agent.chat.return_value = AdminChatResult(
            replies=["Feedback received!"],
            actions=[{"tool": "log_live_update_feedback",
                       "input": {"topic_id": "t-1", "feedback_text": "fix"},
                       "result": {"success": True, "feedback_id": "fb-1"}}],
        )

        msg = _make_mock_message(
            content="This needs a fix",
            reference_message_id=9001,
            # NO @mention of the bot
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Agent turn was invoked
        cog.agent.chat.assert_called_once()
        # Bot DID reply (send loop ran)
        assert msg.delete.called or msg.add_reaction.called, (
            "Expected ack/delete to be attempted for feedback reply")

    @pytest.mark.asyncio
    async def test_reply_to_non_update_message_no_turn_no_delete(self, monkeypatch):
        """(b) A reply whose parent does NOT resolve to a feed item is
        unaffected — no agent turn, no reaction, no deletion (when @mention
        is also absent)."""
        cog, db, calls = _build_cog(monkeypatch)

        db._get_topic_result = None  # reverse-lookup returns None

        msg = _make_mock_message(
            content="some random reply",
            reference_message_id=9999,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Agent turn was NOT invoked (early-return before @mention gate)
        cog.agent.chat.assert_not_called()
        # No reaction / delete on the message
        msg.add_reaction.assert_not_called()
        msg.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_mention_path_still_triggers(self, monkeypatch):
        """(e) Existing @mention path still triggers for non-feedback messages,
        and early-returns on replies is None for non-feedback turns."""
        cog, db, calls = _build_cog(monkeypatch)

        db._get_topic_result = None  # not a feedback reply

        # The bot is @mentioned
        cog.agent.chat.return_value = AdminChatResult(
            replies=None,  # silent turn
            actions=[{"tool": "send_message",
                       "input": {}, "result": {"success": True}}],
        )

        msg = _make_mock_message(
            content="<@777> do something silent",
            mentions=[SimpleNamespace(id=777)],
            reference_message_id=None,  # no reply
        )

        await cog._handle_admin_message(msg)

        # Agent was called (because of @mention)
        cog.agent.chat.assert_called_once()
        # Early-return on replies=None for non-feedback — no send loop,
        # no ack/delete
        msg.add_reaction.assert_not_called()
        msg.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_reverse_lookup_not_called_when_no_reference(self, monkeypatch):
        """(f) Reverse-lookup is NOT called when message.reference is absent."""
        cog, db, calls = _build_cog(monkeypatch)

        cog.agent.chat.return_value = AdminChatResult(
            replies=["Hello!"], actions=[])

        msg = _make_mock_message(
            content="<@777> hello",
            mentions=[SimpleNamespace(id=777)],
            reference_message_id=None,
        )

        await cog._handle_admin_message(msg)

        # get_feed_item_by_discord_message_id was never called
        reverse_calls = [c for c in calls
                         if c[0] == "get_topic_by_discord_message_id"]
        assert len(reverse_calls) == 0, (
            f"Expected 0 reverse-lookup calls, got {len(reverse_calls)}")

    @pytest.mark.asyncio
    async def test_reverse_lookup_called_exactly_once_per_qualifying_reply(self, monkeypatch):
        """(f) Reverse-lookup is called exactly once per qualifying reply
        (message.reference present)."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Got it"], actions=[])

        msg = _make_mock_message(
            content="fix this",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Exactly one reverse-lookup call
        reverse_calls = [c for c in calls
                         if c[0] == "get_topic_by_discord_message_id"]
        assert len(reverse_calls) == 1, (
            f"Expected 1 reverse-lookup call, got {len(reverse_calls)}")
        # The call went through asyncio.to_thread (captured as 'to_thread'
        # with the wrapper name — not the original function name — but the
        # db_calls recording confirms the reverse-lookup executed)
        to_thread_calls = [c for c in calls
                           if c[0] == "to_thread"]
        assert len(to_thread_calls) >= 1, (
            f"Expected at least 1 to_thread call, "
            f"got {len(to_thread_calls)}")


# ── Test (c) / (d): Fallback + ack/delete gate ──────────────────────────────


class TestFeedbackPostTurnProcessing:
    """Cog-level tests: fallback, ack/delete gate, Step 8 processing."""

    @pytest.mark.asyncio
    async def test_fallback_stores_row_when_agent_logs_none(self, monkeypatch):
        """(c) When the agent turn ends without logging a feedback row
        (get_live_update_feedback_for returns None), the cog's fallback
        stores a row with disposition='fallback'."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        # Auth re-query returns None → trigger fallback
        db._get_feedback_result = None  # first read: no row
        # After fallback store, re-read with disposition='fallback' returns row
        db._get_feedback_result_after_fallback = _make_feedback_row(
            disposition="fallback")

        # We need to control the sequence of get_live_update_feedback_for calls
        call_count = [0]

        def _get_feedback_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # first call: no row → fallback triggers
            else:
                return _make_feedback_row(disposition="fallback")

        db.get_live_update_feedback_for = _get_feedback_side_effect

        cog.agent.chat.return_value = AdminChatResult(
            replies=["I'll fix that"],  # agent replied but didn't log feedback
            actions=[{"tool": "reply",
                       "input": {"message": "I'll fix that"},
                       "result": {"success": True}}],
        )

        msg = _make_mock_message(
            content="please fix this update",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # store_live_update_feedback was called (fallback)
        store_calls = [c for c in calls if c[0] == "store_live_update_feedback"]
        assert len(store_calls) >= 1, "Expected fallback store_live_update_feedback call"

    @pytest.mark.asyncio
    async def test_ack_and_delete_only_after_confirmed_row(self, monkeypatch):
        """(d) ✅ reaction + reply deletion happen ONLY after
        get_live_update_feedback_for confirms a feedback row exists."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()  # row exists

        cog.agent.chat.return_value = AdminChatResult(
            replies=["Feedback noted"],
            actions=[{"tool": "log_live_update_feedback",
                       "input": {"topic_id": "t-1",
                                  "feedback_text": "fix"},
                       "result": {"success": True}}],
        )

        msg = _make_mock_message(
            content="please fix this",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # ✅ and delete were called
        msg.add_reaction.assert_called()
        msg.delete.assert_called()

    @pytest.mark.asyncio
    async def test_no_ack_or_delete_when_no_feedback_row(self, monkeypatch):
        """(d) When no feedback row is confirmed (not even fallback works),
        ✅ and delete are NOT called."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = None  # no row from agent

        # Also make fallback store return None (write failure)
        db._store_feedback_result = None

        # get_live_update_feedback_for always returns None
        db.get_live_update_feedback_for = lambda *a, **kw: None

        cog.agent.chat.return_value = AdminChatResult(
            replies=["will do"],
            actions=[],
        )

        msg = _make_mock_message(
            content="fix it",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # No ack/delete because no feedback_row was ever confirmed
        msg.add_reaction.assert_not_called()
        msg.delete.assert_not_called()


# ── Test (h): replied_to_message_id from reference.message_id ────────────────


class TestChannelContextEnrichment:
    """Cog-level tests: channel_context enrichment fields."""

    @pytest.mark.asyncio
    async def test_replied_to_message_id_from_uncached_reference(self, monkeypatch):
        """(h) replied_to_message_id is populated from
        message.reference.message_id even when .resolved is None."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        # Capture channel_context passed to agent.chat()
        captured_ctx = {}

        async def _capture_chat(*, user_id, user_message, channel_context,
                                channel=None, requester_id=None):
            captured_ctx.update(channel_context or {})
            return AdminChatResult(replies=["ok"], actions=[])

        cog.agent.chat = _capture_chat

        # message.reference exists but .resolved is None (uncached)
        msg = _make_mock_message(
            content="fix this",
            reference_message_id=9001,
            reference_resolved=None,  # NOT cached
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # replied_to_message_id is set from message.reference.message_id
        assert captured_ctx.get("replied_to_message_id") == "9001", (
            f"Expected replied_to_message_id='9001', "
            f"got {captured_ctx.get('replied_to_message_id')!r}")

    @pytest.mark.asyncio
    async def test_environment_derived_from_dev_mode(self, monkeypatch):
        """(l) environment is derived from db_handler.dev_mode."""
        cog, db, calls = _build_cog(monkeypatch, dev_mode=True)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        captured_ctx = {}

        async def _capture_chat(*, user_id, user_message, channel_context,
                                channel=None, requester_id=None):
            captured_ctx.update(channel_context or {})
            return AdminChatResult(replies=["ok"], actions=[])

        cog.agent.chat = _capture_chat

        msg = _make_mock_message(
            content="fix this",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        assert captured_ctx.get("environment") == "dev", (
            f"Expected environment='dev' in dev_mode, "
            f"got {captured_ctx.get('environment')!r}")

    @pytest.mark.asyncio
    async def test_guidance_supplied_even_when_channel_not_live_channel_id(self, monkeypatch):
        """(n) Default guidance is supplied for a confirmed feedback reply
        regardless of which channel the reply lands in (guidance is no longer
        gated on a per-feed-item live_channel_id)."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        captured_ctx = {}

        async def _capture_chat(*, user_id, user_message, channel_context,
                                channel=None, requester_id=None):
            captured_ctx.update(channel_context or {})
            return AdminChatResult(replies=["ok"], actions=[])

        cog.agent.chat = _capture_chat

        msg = _make_mock_message(
            content="fix this",
            reference_message_id=9001,
            channel_id=456,  # different from live_channel_id=999
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Guidance IS supplied (not gated on channel equality)
        assert captured_ctx.get("channel_guidance") is not None, (
            "Expected channel_guidance to be supplied even when "
            "channel_id != live_channel_id")
        assert "Live Update Feedback" in captured_ctx["channel_guidance"], (
            "Expected default LIVE_UPDATE_FEEDBACK_GUIDANCE to be present")


# ── Test (j): Silent feedback turn runs full Step 8 ──────────────────────────


class TestSilentFeedbackTurn:
    """Test (j): The keystone — silent feedback turn (result.replies is None)
    must still execute the full Step 8 block: re-query, fallback, ✅, delete."""

    @pytest.mark.asyncio
    async def test_silent_feedback_turn_runs_full_step8(self, monkeypatch):
        """(j) When result.replies is None for a feedback reply, the guard
        `if result.replies is None and not is_live_update_feedback: return`
        does NOT early-return.  Control continues into Step 8 post-processing:
        authoritative re-query, fallback store (if needed), ✅, delete."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        # Silent turn: replies=None, actions non-empty (tool ran)
        cog.agent.chat.return_value = AdminChatResult(
            replies=None,  # SILENT
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"topic_id": "t-1", "feedback_text": "fix"},
                 "result": {"success": True, "feedback_id": "fb-1"}},
            ],
        )

        msg = _make_mock_message(
            content="fix this",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Step 8.2: authoritative re-query happened
        get_feedback_calls = [c for c in calls
                              if c[0] == "get_live_update_feedback_for"]
        assert len(get_feedback_calls) >= 1, (
            "Expected authoritative re-query in Step 8.2")

        # Step 8.5: ✅ reaction + delete happened
        msg.add_reaction.assert_called()
        msg.delete.assert_called()

    @pytest.mark.asyncio
    async def test_silent_feedback_turn_with_fallback(self, monkeypatch):
        """(j) Silent feedback turn where agent didn't log → fallback stores
        row, then ✅ + delete still execute."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = None  # agent didn't log

        # After fallback store succeeds, re-read returns row
        _feedback_sequence = [None, _make_feedback_row(disposition="fallback")]
        _seq_idx = [0]

        def _get_feedback_seq(*args, **kwargs):
            idx = _seq_idx[0]
            _seq_idx[0] += 1
            if idx < len(_feedback_sequence):
                return _feedback_sequence[idx]
            return _make_feedback_row(disposition="fallback")

        db.get_live_update_feedback_for = _get_feedback_seq
        db._store_feedback_result = {"feedback_id": "fb-fallback"}

        cog.agent.chat.return_value = AdminChatResult(
            replies=None,  # SILENT
            actions=[{"tool": "edit_message",
                       "input": {"message_id": "9001", "content": "updated"},
                       "result": {"success": True, "message_id": "9001"}}],
        )

        msg = _make_mock_message(
            content="fix this",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Fallback store was called
        store_calls = [c for c in calls if c[0] == "store_live_update_feedback"]
        assert len(store_calls) >= 1, (
            "Expected fallback store_live_update_feedback call for silent turn")

        # Still acked + deleted
        msg.add_reaction.assert_called()
        msg.delete.assert_called()


# ── Test (k): Editorial audit from result.actions ────────────────────────────


class TestEditorialAudit:
    """Test (k): Agent edits/deletes but logs no editorial row → cog detects
    from result.actions and enforces disposition='edited'/'deleted' row +
    feed-item status update, covering BOTH edit_message (singular message_id)
    and delete_message (message_ids/deleted_ids list) shapes."""

    @pytest.mark.asyncio
    async def test_edit_message_action_triggers_edited_audit(self, monkeypatch):
        """(k) edit_message with singular message_id → 'edited' audit feedback
        row stored; topics.state is NOT mutated (there is no 'edited' state)."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic(discord_message_ids=[9001, 9002])
        db._get_topic_result = topic
        # No pre-existing feedback row → the 'edited' audit store path fires.
        db._get_feedback_result = None

        # Agent called edit_message on message_id=9001
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Updated!"],
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"topic_id": "t-1", "feedback_text": "fix"},
                 "result": {"success": True}},
                {"tool": "edit_message",
                 "input": {"message_id": "9001", "content": "updated text"},
                 "result": {"success": True, "message_id": "9001"}},
            ],
        )

        msg = _make_mock_message(
            content="please fix this update",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # An 'edited' disposition audit row was stored, keyed on topic_id.
        store_calls = [c for c in calls if c[0] == "store_live_update_feedback"]
        edited_stores = [
            c for c in store_calls
            if c[1] and isinstance(c[1][0], dict)
            and c[1][0].get("disposition") == "edited"
            and c[1][0].get("topic_id") == "t-1"
        ]
        assert len(edited_stores) >= 1, (
            f"Expected an 'edited' audit store keyed on topic_id, got: {store_calls}")
        # topics.state must NOT be mutated on edit (no 'edited' state exists).
        assert not [c for c in calls if c[0] == "update_topic"], (
            "edit_message must not call update_topic / mutate topics.state")

    @pytest.mark.asyncio
    async def test_delete_message_singular_shape_triggers_deleted_audit(self, monkeypatch):
        """(k) delete_message with singular message_id shape →
        status='deleted', audit row with disposition='deleted'."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic(discord_message_ids=[9001, 9002])
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        # Agent called delete_message with singular message_id
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Deleted!"],
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"topic_id": "t-1",
                            "feedback_text": "delete this",
                            "disposition": "deletion-request"},
                 "result": {"success": True}},
                {"tool": "delete_message",
                 "input": {"message_id": "9001"},
                 "result": {"success": True, "deleted_ids": ["9001"]}},
            ],
        )

        msg = _make_mock_message(
            content="please delete this update",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Soft-delete: update_topic called with state='deleted' (never hard-delete).
        topic_updates = [c for c in calls if c[0] == "update_topic"]
        assert len(topic_updates) >= 1, (
            f"Expected update_topic soft-delete call, got: {calls}")
        c = topic_updates[0]
        assert c[1][0] == "t-1", "update_topic must target the resolved topic_id"
        assert c[1][1] == {"state": "deleted"}, (
            f"Expected state='deleted' soft-delete, got: {c[1]}")

    @pytest.mark.asyncio
    async def test_delete_message_bulk_shape_triggers_deleted_audit(self, monkeypatch):
        """(k) delete_message with message_ids list + deleted_ids list →
        status='deleted', covering BOTH shapes."""
        cog, db, calls = _build_cog(monkeypatch)

        topic = _make_topic(discord_message_ids=[9001, 9002, 9003])
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()

        # Agent called delete_message with message_ids (plural) list
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Bulk deleted!"],
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"topic_id": "t-1",
                            "feedback_text": "bulk delete"},
                 "result": {"success": True}},
                {"tool": "delete_message",
                 "input": {"message_ids": ["9001", "9002"]},
                 "result": {"success": True, "deleted_ids": ["9001", "9002"]}},
            ],
        )

        msg = _make_mock_message(
            content="delete these updates",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Soft-delete via update_topic(state='deleted') for the bulk shape too.
        topic_updates = [c for c in calls if c[0] == "update_topic"]
        deleted_call = [c for c in topic_updates if c[1][1] == {"state": "deleted"}]
        assert len(deleted_call) >= 1, (
            f"Expected update_topic state='deleted' for bulk delete, got: {topic_updates}")


# ── Test (g): log_live_update_feedback receives injected context ─────────────


class TestAgentContextInjection:
    """Agent-level tests: context threading for log_live_update_feedback."""

    @pytest.mark.asyncio
    async def test_log_live_update_feedback_receives_injected_context(self, monkeypatch):
        """(g) log_live_update_feedback receives injected admin_user_id,
        environment, and replied_to_message_id from channel_context, not
        from LLM-supplied values."""
        monkeypatch.setattr(
            "src.features.admin_chat.tools.TOOLS",
            [
                {"name": "log_live_update_feedback",
                 "description": "Log live update feedback",
                 "parameters": {}},
                {"name": "end_turn",
                 "description": "End turn silently",
                 "parameters": {}},
            ],
        )

        # Capture the tool input that reaches execute_tool
        captured_tool_inputs = []

        async def _fake_execute_tool(*, tool_name, tool_input, **kwargs):
            captured_tool_inputs.append((tool_name, dict(tool_input)))
            if tool_name == "log_live_update_feedback":
                return {"success": True, "feedback_id": "fb-1"}
            if tool_name == "end_turn":
                return {"success": True}
            return {"success": False, "error": "unknown"}

        monkeypatch.setattr(
            "src.features.admin_chat.agent.execute_tool",
            _fake_execute_tool,
        )

        bot = SimpleNamespace(user=SimpleNamespace(id=777))
        db = MagicMock()
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)

        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            side_effect=[
                _make_response(
                    _make_tool_use_block(
                        "log_live_update_feedback",
                        {"topic_id": "t-1", "feedback_text": "fix"},
                        "tu_1",
                    ),
                ),
                _make_response(
                    _make_tool_use_block("end_turn", {}, "tu_2"),
                ),
            ]
        )
        agent.client = fake_client

        _conversations.clear()

        channel_context = {
            "guild_id": "789",
            "channel_id": "456",
            "environment": "dev",
            "replied_to_message_id": "9001",
            "channel_guidance": "Some guidance",
            "live_update_topic": {"topic_id": "t-injected"},
        }

        result = await agent.chat(
            user_id=999,
            user_message="fix this update",
            channel_context=channel_context,
        )

        # Find the log_live_update_feedback call
        luf_calls = [(name, inp) for name, inp in captured_tool_inputs
                     if name == "log_live_update_feedback"]
        assert len(luf_calls) == 1, (
            f"Expected 1 log_live_update_feedback call, got {len(luf_calls)}")

        _, luf_input = luf_calls[0]

        # admin_user_id injected from _ADMIN_IDENTITY_INJECTED_TOOLS
        assert luf_input.get("admin_user_id") == 999, (
            f"Expected admin_user_id=999 injected, got {luf_input.get('admin_user_id')!r}")
        # environment injected from channel_context['environment']
        assert luf_input.get("environment") == "dev", (
            f"Expected environment='dev', got {luf_input.get('environment')!r}")
        # replied_to_message_id injected from channel_context['replied_to_message_id']
        assert luf_input.get("replied_to_message_id") == "9001", (
            f"Expected replied_to_message_id='9001', "
            f"got {luf_input.get('replied_to_message_id')!r}")
        # topic_id injected from channel_context['live_update_topic']
        assert luf_input.get("topic_id") == "t-injected", (
            f"Expected topic_id='t-injected' injected from context, "
            f"got {luf_input.get('topic_id')!r}")


# ── Test (i): channel_guidance appended to system ────────────────────────────


class TestChannelGuidanceAppend:
    """Test (i): channel_guidance is appended to system prompt as a post-render
    append, not via a {channel_guidance} template literal. Works with both base
    SYSTEM_PROMPT and per-guild prompt_admin_chat_system override."""

    @pytest.mark.asyncio
    async def test_guidance_appended_not_templated(self, monkeypatch):
        """(i) channel_guidance is appended as '## Channel Guidance' block,
        not via {channel_guidance} template placeholder — no literal leak."""
        monkeypatch.setattr(
            "src.features.admin_chat.tools.TOOLS",
            [
                {"name": "reply",
                 "description": "Reply to the user",
                 "parameters": {}},
                {"name": "end_turn",
                 "description": "End turn silently",
                 "parameters": {}},
            ],
        )

        # Capture the system prompt
        captured_system = {}

        class FakeClient:
            async def generate_chat_completion(self, *, model, system_prompt,
                                               messages, max_tokens, tools):
                captured_system["system"] = system_prompt
                return _make_response(
                    _make_tool_use_block("reply",
                                          {"message": "ok"}, "tu_1"))

        bot = SimpleNamespace(user=SimpleNamespace(id=777))
        db = MagicMock()
        db.server_config = MagicMock()
        db.server_config.get_server = MagicMock(return_value={})
        db.server_config.get_content = MagicMock(return_value=None)
        db.server_config.get_default_guild_id = MagicMock(return_value="789")
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)
        agent.client = FakeClient()

        monkeypatch.setattr(
            "src.features.admin_chat.agent.execute_tool",
            AsyncMock(return_value={"success": True,
                                     "messages": ["ok"]}),
        )

        _conversations.clear()

        channel_context = {
            "guild_id": "789",
            "channel_id": "456",
            "channel_guidance": "## Live Update Feedback Channel\n\nCustom guidance here.",
        }

        await agent.chat(
            user_id=999,
            user_message="hello",
            channel_context=channel_context,
        )

        system = captured_system["system"]
        # Guidance IS present in the system prompt
        assert "Custom guidance here" in system, (
            "Expected channel_guidance content in system prompt")
        # No {channel_guidance} literal leaked
        assert "{channel_guidance}" not in system, (
            "Found raw {channel_guidance} literal in system prompt — "
            "guidance was not properly appended")
        # The guidance appears as a post-render append after _POM_ADDENDUM
        assert "## Channel Guidance" in system, (
            "Expected '## Channel Guidance' header in system prompt")

    @pytest.mark.asyncio
    async def test_guidance_works_with_per_guild_prompt_override(self, monkeypatch):
        """(i) Guidance append works even when per-guild prompt_admin_chat_system
        replaces the base SYSTEM_PROMPT (no {channel_guidance} placeholder)."""
        monkeypatch.setattr(
            "src.features.admin_chat.tools.TOOLS",
            [
                {"name": "reply",
                 "description": "Reply to the user",
                 "parameters": {}},
                {"name": "end_turn",
                 "description": "End turn silently",
                 "parameters": {}},
            ],
        )

        captured_system = {}

        class FakeClient:
            async def generate_chat_completion(self, *, model, system_prompt,
                                               messages, max_tokens, tools):
                captured_system["system"] = system_prompt
                return _make_response(
                    _make_tool_use_block("reply",
                                          {"message": "ok"}, "tu_1"))

        bot = SimpleNamespace(user=SimpleNamespace(id=777))
        db = MagicMock()
        sc = MagicMock()
        # per-guild override returns a custom prompt WITHOUT {channel_guidance}
        custom_prompt = "You are a custom admin bot for guild {guild_id}.\n{bot_voice}"
        sc.get_server = MagicMock(
            return_value={"community_name": "TestCommunity"})
        sc.get_content = MagicMock(return_value=custom_prompt)
        sc.get_default_guild_id = MagicMock(return_value="789")
        db.server_config = sc
        # Agent accesses server_config via self.bot.db_handler
        bot.db_handler = db
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)
        agent.client = FakeClient()

        monkeypatch.setattr(
            "src.features.admin_chat.agent.execute_tool",
            AsyncMock(return_value={"success": True,
                                     "messages": ["ok"]}),
        )

        _conversations.clear()

        channel_context = {
            "guild_id": "789",
            "channel_id": "456",
            "channel_guidance": "Per-channel guidance text.",
        }

        await agent.chat(
            user_id=999,
            user_message="hello",
            channel_context=channel_context,
        )

        system = captured_system["system"]
        # Per-guild prompt is in effect
        assert "custom admin bot" in system, (
            f"Expected per-guild prompt override, got: {system[:200]}...")
        # Guidance is still appended
        assert "Per-channel guidance text" in system
        assert "## Channel Guidance" in system
        # No literal leak
        assert "{channel_guidance}" not in system


# ── Test (m): Reverse-lookup via asyncio.to_thread ───────────────────────────


class TestReverseLookupViaToThread:
    """Test (m): Reverse-lookup is invoked via asyncio.to_thread."""

    @pytest.mark.asyncio
    async def test_reverse_lookup_offloaded_to_asyncio_to_thread(self, monkeypatch):
        """(m) get_topic_by_discord_message_id is called via
        asyncio.to_thread, not directly."""
        to_thread_calls = []

        async def _fake_to_thread(fn, *args, **kwargs):
            to_thread_calls.append((fn.__name__ if hasattr(fn, "__name__") else str(fn),
                                    args, kwargs))
            return fn(*args, **kwargs)

        monkeypatch.setattr("asyncio.to_thread", _fake_to_thread)

        cog, db, calls = _build_cog(monkeypatch)
        # Re-patch to_thread since _build_cog already patched it
        monkeypatch.setattr("asyncio.to_thread", _fake_to_thread)

        topic = _make_topic()
        db._get_topic_result = topic
        db._get_feedback_result = _make_feedback_row()
        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        msg = _make_mock_message(
            content="fix",
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        # Verify that to_thread was invoked — the function name will be
        # 'wrapper' (from _record_call closure) but it IS the reverse-lookup
        # wrapper.  Also verify via the db_calls capture.
        assert len(to_thread_calls) >= 1, (
            f"Expected asyncio.to_thread to be invoked at least once, "
            f"got {len(to_thread_calls)} calls: {to_thread_calls}")

        # Separately verify the reverse-lookup was called through the
        # db_calls recording (from _record_call in _build_cog).
        reverse_calls = [c for c in calls
                         if c[0] == "get_topic_by_discord_message_id"]
        assert len(reverse_calls) == 1, (
            f"Expected 1 reverse-lookup call via db_handler, "
            f"got {len(reverse_calls)}")


def _run_busy_then_second(
    monkeypatch,
    *,
    chat_results,
    second_content="<@777> second",
):
    """Simulate the prod incident: message 1 starts a busy turn; while it is
    in-flight (blocked inside agent.chat), message 2 arrives and is queued.
    Message 1's turn then completes and its finally-drain must replay message 2
    through on_message so it is claimed and processed (not dropped).

    Returns (cog, db, chat_calls, msg1, msg2, pending_after).
    """
    cog, db, calls = _build_cog(monkeypatch)
    cog._allowed_admin_chat_user_ids.add(999)

    chat_entered = asyncio.Event()
    release = asyncio.Event()
    chat_calls = []

    async def _fake_chat(**kwargs):
        chat_calls.append(kwargs)
        if len(chat_calls) == 1:
            chat_entered.set()
            await release.wait()
        result = chat_results[len(chat_calls) - 1]
        return result

    cog.agent.chat = AsyncMock(side_effect=_fake_chat)

    msg1 = _make_mock_message(
        content="<@777> first",
        author_id=999,
        message_id=1001,
        mentions=[SimpleNamespace(id=777)],
    )
    msg2 = _make_mock_message(
        content=second_content,
        author_id=999,
        message_id=1002,
        mentions=[SimpleNamespace(id=777)],
    )
    return cog, db, chat_calls, msg1, msg2, chat_entered, release


class TestBusyQueueReplayNotDropped:
    """Regression (prod incident 2026-08-06 23:17 UTC): an admin message that
    arrives while the agent is busy was claimed at receipt, queued as pending,
    and then the drain replay hit the already-existing claim and silently
    dropped it ('Skipping already-claimed message'). The claim must be deferred
    until the message is actually processed, so the queued replay can claim it.
    The drain must also run after tool-only turns and exceptions (finally), and
    abort must be terminal on every replica (claim gates only the ack).
    """

    @pytest.mark.asyncio
    async def test_busy_turn_drains_queued_message_via_finally(self, monkeypatch):
        cog, db, chat_calls, msg1, msg2, chat_entered, release = _run_busy_then_second(
            monkeypatch,
            chat_results=[
                AdminChatResult(replies=["first ok"], actions=[]),
                AdminChatResult(replies=["second ok"], actions=[]),
            ],
        )

        # Start msg1's turn; it blocks inside agent.chat (busy=True).
        task = asyncio.create_task(cog._handle_admin_message(msg1))
        await asyncio.wait_for(chat_entered.wait(), timeout=2)

        # msg2 arrives while busy → queued, NOT claimed at receipt.
        await cog._handle_admin_message(msg2)
        assert cog._pending_messages.get(999) is msg2
        claim_keys = [c.kwargs.get('event_key') for c in db.try_claim_bot_event.call_args_list]
        assert "admin_chat_message:1002" not in claim_keys, (
            f"msg2 must not be claimed at receipt; claims so far: {claim_keys}")

        # Release msg1's turn → its finally-drain replays msg2 → processed.
        release.set()
        await asyncio.wait_for(task, timeout=2)

        assert len(chat_calls) == 2, f"msg2 was dropped: {len(chat_calls)} chat calls"
        assert 999 not in cog._pending_messages
        # msg2 was claimed when actually processed.
        claim_keys = [c.kwargs.get('event_key') for c in db.try_claim_bot_event.call_args_list]
        assert "admin_chat_message:1002" in claim_keys
        msg2.channel.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_tool_only_turn_still_drains_pending(self, monkeypatch):
        """A tool-only turn (replies=None) early-returns after agent.chat; the
        pending queue must still be drained via finally."""
        cog, db, chat_calls, msg1, msg2, chat_entered, release = _run_busy_then_second(
            monkeypatch,
            chat_results=[
                AdminChatResult(replies=None, actions=[{"tool": "query_table", "input": {}, "result": {"success": True}}]),
                AdminChatResult(replies=["second ok"], actions=[]),
            ],
        )

        task = asyncio.create_task(cog._handle_admin_message(msg1))
        await asyncio.wait_for(chat_entered.wait(), timeout=2)

        await cog._handle_admin_message(msg2)
        assert cog._pending_messages.get(999) is msg2

        release.set()
        await asyncio.wait_for(task, timeout=2)

        assert len(chat_calls) == 2, f"tool-only turn left msg2 stranded: {len(chat_calls)}"
        assert 999 not in cog._pending_messages
        claim_keys = [c.kwargs.get('event_key') for c in db.try_claim_bot_event.call_args_list]
        assert "admin_chat_message:1002" in claim_keys
        msg2.channel.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_abort_terminal_every_replica_claim_gates_ack(self, monkeypatch):
        """'stop' while busy requests abort locally; an idle replica must NOT
        claim it and run it through agent.chat as a normal turn. Only one
        replica acknowledges."""
        busy_cog, busy_db, _ = _build_cog(monkeypatch)
        busy_cog._allowed_admin_chat_user_ids.add(999)
        busy_cog._busy[999] = True

        idle_cog, idle_db, _ = _build_cog(monkeypatch)
        idle_cog._allowed_admin_chat_user_ids.add(999)

        # Shared claim store across the two "replicas".
        claimed = set()

        def _shared_claim(**kwargs):
            key = kwargs['event_key']
            if key in claimed:
                return False
            claimed.add(key)
            return True

        busy_db.try_claim_bot_event.side_effect = _shared_claim
        idle_db.try_claim_bot_event.side_effect = _shared_claim

        msg = _make_mock_message(
            content="<@777> stop",
            author_id=999,
            mentions=[SimpleNamespace(id=777)],
        )

        await busy_cog._handle_admin_message(msg)
        await idle_cog._handle_admin_message(msg)

        # Busy replica requested the local abort.
        assert busy_cog.agent.request_abort.call_count == 1
        # Idle replica must not run "stop" as a chat turn.
        idle_cog.agent.chat.assert_not_called()
        # Exactly one replica claimed/acked.
        assert len(claimed) == 1

    @pytest.mark.asyncio
    async def test_busy_busy_replicas_only_one_claim_wins(self, monkeypatch):
        """Two replicas both busy with the same message: both queue it without
        claiming; when both drain, only one claim insert succeeds (unique
        message_id), so exactly one processes and the other skips."""
        claimed = set()

        def _shared_claim(**kwargs):
            key = kwargs['event_key']
            if key in claimed:
                return False
            claimed.add(key)
            return True

        cogs = []
        for _ in range(2):
            cog, db, _ = _build_cog(monkeypatch)
            cog._allowed_admin_chat_user_ids.add(999)
            db.try_claim_bot_event.side_effect = _shared_claim
            cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])
            cogs.append((cog, db))

        msg = _make_mock_message(
            content="<@777> hello",
            author_id=999,
            mentions=[SimpleNamespace(id=777)],
        )

        for cog, _ in cogs:
            await cog._handle_admin_message(msg)

        assert len(claimed) == 1
        processed = [cog.agent.chat.call_count > 0 for cog, _ in cogs]
        assert sum(processed) == 1

    @pytest.mark.asyncio
    async def test_not_busy_message_still_claimed_and_processed(self, monkeypatch):
        cog, db, calls = _build_cog(monkeypatch)
        cog._allowed_admin_chat_user_ids.add(999)
        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        msg = _make_mock_message(
            content="<@777> hello",
            author_id=999,
            mentions=[SimpleNamespace(id=777)],
        )

        await cog._handle_admin_message(msg)

        db.try_claim_bot_event.assert_called_once()
        cog.agent.chat.assert_called_once()


# ── Community live-update feedback (any member) + editorial-decisions ───────


def _make_decision_channel():
    """Mock text channel whose send records the decision embed."""
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=888))
    return channel


class TestCommunityFeedbackRouting:
    """Anyone — not just admins — replying to a live-update post triggers the
    feedback sense-check turn; regular non-admin messages stay untouched."""

    @pytest.mark.asyncio
    async def test_non_admin_reply_to_live_update_runs_turn(self, monkeypatch):
        """A non-admin reply whose parent resolves to a topic runs the full
        feedback turn (agent chat + ✅ + delete), no @mention required."""
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row()

        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        msg = _make_mock_message(
            content="this update is wrong",
            author_id=424242,  # NOT an admin
            reference_message_id=9001,
            mentions=[],
        )

        await cog.on_message(msg)

        cog.agent.chat.assert_awaited_once()
        # Feedback row confirmed → ✅ + delete the reply in the live channel.
        msg.add_reaction.assert_awaited()
        msg.delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_non_admin_reply_to_non_update_no_turn(self, monkeypatch):
        """A non-admin reply whose parent does NOT resolve to a topic gets no
        agent turn and no deletion."""
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = None  # not a live-update reply

        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        msg = _make_mock_message(
            content="hello there",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )

        await cog.on_message(msg)

        cog.agent.chat.assert_not_called()
        msg.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_non_reply_message_no_turn(self, monkeypatch):
        """A plain (non-reply) non-admin message is not feedback and is not
        routed to the agent."""
        cog, db, calls = _build_cog(monkeypatch)
        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        msg = _make_mock_message(
            content="hello there",
            author_id=424242,
            mentions=[],
        )

        await cog.on_message(msg)

        cog.agent.chat.assert_not_called()
        msg.delete.assert_not_called()


class TestEditorialDecisionPosting:
    """The sense-check outcome is posted to the editorial-decisions channel
    with the user tagged, a copy of their feedback, and the precise edit."""

    def _run_turn(self, cog, db, msg, actions):
        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=actions)
        return msg

    @pytest.mark.asyncio
    async def test_decision_posted_with_precise_edit_and_mention(self, monkeypatch):
        """Edit action → decision post carries before/after diff, tags the
        author, and the reply is still ✅-reacted + deleted."""
        monkeypatch.setenv("EDITORIAL_DECISIONS_CHANNEL_ID", "777")
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row(
            verdict="Feedback matches the source — fixed the version number.",
            feedback_text="Wan version is wrong",
        )

        decision_channel = _make_decision_channel()
        msg = _make_mock_message(
            content="Wan version is wrong",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )
        msg.guild.get_channel = MagicMock(return_value=decision_channel)

        self._run_turn(cog, db, msg, [
            {"tool": "log_live_update_feedback",
             "input": {"topic_id": "t-1", "feedback_text": "Wan version is wrong"},
             "result": {"success": True, "feedback_id": "fb-1"}},
            {"tool": "edit_message",
             "input": {"message_id": "9001", "content": "Corrected: Wan 2.5"},
             "result": {"success": True, "message_id": "9001"}},
        ])

        await cog._handle_admin_message(msg)

        decision_channel.send.assert_awaited_once()
        call_kwargs = decision_channel.send.call_args.kwargs
        assert call_kwargs["content"] == "<@424242>", "decision post must tag the author"
        embed = call_kwargs["embed"]
        assert "Edited" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert "Their feedback" in fields
        assert "Wan version is wrong" in fields["Their feedback"]
        assert "Sense-check verdict" in fields
        assert "Feedback matches the source" in fields["Sense-check verdict"]
        precise = fields["Precise edit"]
        assert "Original update post" in precise  # before = replied-to content
        assert "Corrected: Wan 2.5" in precise    # after = edit input content
        # Original reply still acked + deleted from the live channel.
        msg.add_reaction.assert_awaited()
        msg.delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_decision_posted_on_delete_and_topic_soft_deleted(self, monkeypatch):
        """Delete action → 'Deleted' decision post + topics.state='deleted'."""
        monkeypatch.setenv("EDITORIAL_DECISIONS_CHANNEL_ID", "777")
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row(disposition="deletion-request")

        decision_channel = _make_decision_channel()
        msg = _make_mock_message(
            content="remove this, it's wrong",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )
        msg.guild.get_channel = MagicMock(return_value=decision_channel)

        self._run_turn(cog, db, msg, [
            {"tool": "log_live_update_feedback",
             "input": {"topic_id": "t-1", "feedback_text": "remove this"},
             "result": {"success": True, "feedback_id": "fb-1"}},
            {"tool": "delete_message",
             "input": {"message_id": "9001"},
             "result": {"success": True, "deleted": 1, "deleted_ids": ["9001"]}},
        ])

        await cog._handle_admin_message(msg)

        decision_channel.send.assert_awaited_once()
        embed = decision_channel.send.call_args.kwargs["embed"]
        assert "Deleted" in embed.title
        # Soft-delete: update_topic called with state='deleted'.
        update_calls = [c for c in calls if c[0] == "update_topic"]
        assert len(update_calls) >= 1
        assert update_calls[0][1][1] == {"state": "deleted"}
        msg.delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_decision_posted_no_change_with_feedback_copy(self, monkeypatch):
        """No edit/delete → 'No change' decision post carrying the feedback
        copy and the author mention; reply still removed."""
        monkeypatch.setenv("EDITORIAL_DECISIONS_CHANNEL_ID", "777")
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row(
            disposition="no_change",
            verdict="Feedback not supported by the source messages; no change.",
        )

        decision_channel = _make_decision_channel()
        msg = _make_mock_message(
            content="this headline is clickbait",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )
        msg.guild.get_channel = MagicMock(return_value=decision_channel)

        self._run_turn(cog, db, msg, [
            {"tool": "log_live_update_feedback",
             "input": {"topic_id": "t-1", "feedback_text": "this headline is clickbait"},
             "result": {"success": True, "feedback_id": "fb-1"}},
        ])

        await cog._handle_admin_message(msg)

        decision_channel.send.assert_awaited_once()
        call_kwargs = decision_channel.send.call_args.kwargs
        assert call_kwargs["content"] == "<@424242>"
        embed = call_kwargs["embed"]
        assert "No change" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert "Feedback not supported by the source messages" in fields["Sense-check verdict"]
        assert "Needs fix" in fields["Their feedback"]
        msg.add_reaction.assert_awaited()
        msg.delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_editorial_decisions_channel_found_by_name(self, monkeypatch):
        """Without env/server_config, the channel is found by name —
        including the literal `editorial_decisions` spelling used in BNDC."""
        cog, db, calls = _build_cog(monkeypatch)
        # No env, no server_config field → name fallback.
        monkeypatch.delenv("EDITORIAL_DECISIONS_CHANNEL_ID", raising=False)

        decision_channel = _make_decision_channel()
        decision_channel.name = "editorial_decisions"
        decision_channel.id = 1316024582041243668
        msg = _make_mock_message(
            content="fix this",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )
        # Fake guild channel list with the underscore-spelled channel; no
        # env/server_config id resolves, so the name fallback must pick it.
        msg.guild.channels = [decision_channel]
        msg.guild.get_channel = MagicMock(return_value=None)

        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row()

        await cog._handle_admin_message(msg)

        # The name fallback resolved the underscore channel and the decision
        # post landed there.
        decision_channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_decision_post_when_channel_unresolvable(self, monkeypatch):
        """No editorial-decisions channel → the post is skipped but the reply
        is still acked/deleted (feedback is never stranded)."""
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row()

        # get_server_field returns None and env is unset → channel unresolvable.
        msg = _make_mock_message(
            content="fix this",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )

        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        await cog._handle_admin_message(msg)

        msg.add_reaction.assert_awaited()
        msg.delete.assert_awaited()


class TestFeedbackTurnAllowlist:
    """Executor-level barrier: feedback turns only expose the feedback tool
    surface, never payment/moderation/upload/social tools."""

    @pytest.mark.asyncio
    async def test_feedback_turn_restricts_tool_surface(self, monkeypatch):
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        db = MagicMock()
        sharer = MagicMock()
        agent = AdminChatAgent(bot, db, sharer)

        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            return_value=_make_response(_make_text_block("checked"))
        )
        agent.client = fake_client
        _conversations.clear()

        channel_context = {
            "guild_id": "789",
            "channel_id": "456",
            "channel_name": "live-updates",
            "environment": "prod",
            "replied_to_message_id": "9001",
            "live_update_topic": {"topic_id": "t-1", "headline": "Test Update"},
            "channel_guidance": "feedback guidance",
        }

        await agent.chat(
            user_id=7,
            user_message="this update is wrong",
            channel_context=channel_context,
        )

        names = {t["name"] for t in fake_client.generate_chat_completion.call_args.kwargs["tools"]}
        # Feedback surface present.
        assert {"log_live_update_feedback", "get_live_update_ground_truth",
                "edit_message", "delete_message"} <= names
        # Escalation surface absent.
        assert names.isdisjoint({
            "mute_speaker", "unmute_speaker", "upload_file", "send_message",
            "publish_social_draft", "approve_social_draft", "update_member_socials",
        })


class TestFeedbackLLMResolver:
    """LIVE_UPDATE_FEEDBACK_CLIENT / LIVE_UPDATE_FEEDBACK_MODEL drive the
    sense-check LLM (e.g. GPT Sol via client=openai); defaults stay on the
    admin-chat client/model."""

    def _make_agent(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        return AdminChatAgent(bot, MagicMock(), MagicMock())

    def test_defaults_to_admin_client_and_model(self):
        agent = self._make_agent()
        client, model = agent._resolve_feedback_llm()
        assert client is agent.client
        assert model == agent.model

    def test_model_override_keeps_admin_client(self, monkeypatch):
        monkeypatch.setenv("LIVE_UPDATE_FEEDBACK_MODEL", "gpt-5.6-sol")
        agent = self._make_agent()
        client, model = agent._resolve_feedback_llm()
        assert client is agent.client
        assert model == "gpt-5.6-sol"

    def test_openai_client_selected_for_feedback(self, monkeypatch):
        class _FakeOpenAI:
            tag = "openai"

        # The tests conftest stubs src.common.llm as a namespace module, so
        # patch the attribute on that module object directly.
        import src.common.llm as _llm_stub
        monkeypatch.setattr(
            _llm_stub, "SUPPORTED_CLIENTS", {"openai": _FakeOpenAI}, raising=False,
        )
        monkeypatch.setenv("LIVE_UPDATE_FEEDBACK_CLIENT", "openai")
        monkeypatch.setenv("LIVE_UPDATE_FEEDBACK_MODEL", "gpt-5.6-sol")
        agent = self._make_agent()
        client, model = agent._resolve_feedback_llm()
        assert isinstance(client, _FakeOpenAI)
        assert client.tag == "openai"
        assert model == "gpt-5.6-sol"

    def test_unknown_client_falls_back_to_admin_client(self, monkeypatch):
        monkeypatch.setenv("LIVE_UPDATE_FEEDBACK_CLIENT", "martian")
        agent = self._make_agent()
        client, model = agent._resolve_feedback_llm()
        assert client is agent.client
        assert model == agent.model


class TestFeedbackAuthorReputation:
    """The sense-check sees the feedback author's editorial-archive reputation
    (identity + roles + last-30d activity), like the topic editor sees for
    source authors."""

    @pytest.mark.asyncio
    async def test_feedback_context_includes_author_reputation(self, monkeypatch):
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row()
        db.get_author_context_snapshots = MagicMock(return_value={
            424242: {
                "member_id": 424242, "username": "alice",
                "role_ids": [1, 2], "twitter_url": "https://x.com/alice",
            },
        })
        db.get_topic_editor_author_profile = MagicMock(return_value={
            "message_count_30d": 87, "average_reaction_count": 3.2,
        })

        captured_ctx = {}

        async def _capture_chat(*, user_id, user_message, channel_context,
                                channel=None, requester_id=None):
            captured_ctx.update(channel_context or {})
            return AdminChatResult(replies=["ok"], actions=[])

        cog.agent.chat = _capture_chat

        msg = _make_mock_message(
            content="fix this",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )

        await cog._handle_admin_message(msg)

        author_ctx = captured_ctx.get("live_update_feedback_author") or {}
        assert author_ctx.get("username") == "alice"
        assert author_ctx.get("message_count_30d") == 87
        assert author_ctx.get("average_reaction_count") == 3.2
        assert author_ctx.get("twitter_url") == "https://x.com/alice"

    @pytest.mark.asyncio
    async def test_agent_prompt_renders_ground_truth_and_reputation(self, monkeypatch):
        """The agent prompt carries the verbatim ground truth AND the feedback
        author's reputation for the sense-check."""
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        agent = AdminChatAgent(bot, MagicMock(), MagicMock())

        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            return_value=_make_response(_make_text_block("checked"))
        )
        agent.client = fake_client
        _conversations.clear()

        channel_context = {
            "guild_id": "789",
            "channel_id": "456",
            "channel_name": "live-updates",
            "environment": "prod",
            "replied_to_message_id": "9001",
            "live_update_topic": {"topic_id": "t-1", "headline": "Test Update"},
            "live_update_ground_truth": [
                {"message_id": "100", "created_at": "2026-08-18T00:00:00Z",
                 "author_id": 42, "content": "source says Wan 2.5"},
            ],
            "live_update_feedback_author": {
                "member_id": 424242, "username": "alice", "role_ids": [1, 2],
                "message_count_30d": 87, "average_reaction_count": 3.2,
            },
            "channel_guidance": "feedback guidance",
        }

        await agent.chat(
            user_id=7,
            user_message="the version is wrong",
            channel_context=channel_context,
        )

        # messages[0] is the rendered user turn (the loop appends to the same
        # list after the call, so the last slot may be the assistant reply).
        prompt = fake_client.generate_chat_completion.call_args.kwargs["messages"][0]["content"]
        assert "Ground truth — verbatim source messages" in prompt
        assert "source says Wan 2.5" in prompt
        assert "Feedback author reputation" in prompt
        assert "messages_30d=87" in prompt
        assert "role_count=2" in prompt

    @pytest.mark.asyncio
    async def test_decision_post_includes_reputation_field(self, monkeypatch):
        """The editorial-decisions post carries a compact reputation reference."""
        monkeypatch.setenv("EDITORIAL_DECISIONS_CHANNEL_ID", "777")
        cog, db, calls = _build_cog(monkeypatch)
        db._get_topic_result = _make_topic()
        db._get_feedback_result = _make_feedback_row()
        db.get_author_context_snapshots = MagicMock(return_value={
            424242: {"member_id": 424242, "username": "alice", "role_ids": [1, 2]},
        })
        db.get_topic_editor_author_profile = MagicMock(return_value={
            "message_count_30d": 12, "average_reaction_count": 1.5,
        })

        decision_channel = _make_decision_channel()
        msg = _make_mock_message(
            content="fix this",
            author_id=424242,
            reference_message_id=9001,
            mentions=[],
        )
        msg.guild.get_channel = MagicMock(return_value=decision_channel)
        cog.agent.chat.return_value = AdminChatResult(replies=["ok"], actions=[])

        await cog._handle_admin_message(msg)

        embed = decision_channel.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        reputation = fields.get("Reputation (editorial archive)") or ""
        assert "alice" in reputation
        assert "Messages (30d): 12" in reputation
        assert "Roles: 2" in reputation
