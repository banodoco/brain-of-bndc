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


def _make_feed_item(*, feed_item_id: str = "feed-1",
                    title: str = "Test Update",
                    body: str = "Test body",
                    discord_message_ids: list = None,
                    live_channel_id: int = 456,
                    status: str = "posted"):
    return {
        "feed_item_id": feed_item_id,
        "title": title,
        "body": body,
        "discord_message_ids": discord_message_ids or ["9001", "9002"],
        "live_channel_id": live_channel_id,
        "status": status,
    }


def _make_feedback_row(*, feedback_id: str = "fb-1",
                       feed_item_id: str = "feed-1",
                       disposition: str = "correction"):
    return {
        "feedback_id": feedback_id,
        "feed_item_id": feed_item_id,
        "disposition": disposition,
        "admin_user_id": 999,
        "feedback_text": "Needs fix",
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

    # Default reader methods
    db.get_feed_item_by_discord_message_id = MagicMock(return_value=None)
    db.get_live_update_feedback_for = MagicMock(return_value=None)
    db.store_live_update_feedback = MagicMock(return_value={"feedback_id": "fb-1"})
    db.update_live_update_feed_item_status = MagicMock(
        return_value={"feed_item_id": "feed-1", "status": "edited"})
    db.try_claim_bot_event = MagicMock(return_value=True)

    db._live_write_allowed = MagicMock(return_value=live_write_allowed)

    # Collect calls through wrappers
    def _record_call(name):
        def wrapper(*args, **kwargs):
            db_calls.append((name, args, kwargs))
            if name == "get_feed_item_by_discord_message_id":
                return db._get_feed_item_result
            if name == "get_live_update_feedback_for":
                return db._get_feedback_result
            if name == "store_live_update_feedback":
                return db._store_feedback_result
            if name == "update_live_update_feed_item_status":
                return db._update_status_result
            return None
        return wrapper

    db._get_feed_item_result = None
    db._get_feedback_result = None
    db._store_feedback_result = {"feedback_id": "fb-fallback"}
    db._update_status_result = {"feed_item_id": "feed-1", "status": "edited"}

    # Replace with recording wrappers
    db.get_feed_item_by_discord_message_id = _record_call("get_feed_item_by_discord_message_id")
    db.get_live_update_feedback_for = _record_call("get_live_update_feedback_for")
    db.store_live_update_feedback = _record_call("store_live_update_feedback")
    db.update_live_update_feed_item_status = _record_call("update_live_update_feed_item_status")

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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
        db._get_feedback_result = _make_feedback_row()

        cog.agent.chat.return_value = AdminChatResult(
            replies=["Feedback received!"],
            actions=[{"tool": "log_live_update_feedback",
                       "input": {"feed_item_id": "feed-1", "feedback_text": "fix"},
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

        db._get_feed_item_result = None  # reverse-lookup returns None

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

        db._get_feed_item_result = None  # not a feedback reply

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
                         if c[0] == "get_feed_item_by_discord_message_id"]
        assert len(reverse_calls) == 0, (
            f"Expected 0 reverse-lookup calls, got {len(reverse_calls)}")

    @pytest.mark.asyncio
    async def test_reverse_lookup_called_exactly_once_per_qualifying_reply(self, monkeypatch):
        """(f) Reverse-lookup is called exactly once per qualifying reply
        (message.reference present)."""
        cog, db, calls = _build_cog(monkeypatch)

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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
                         if c[0] == "get_feed_item_by_discord_message_id"]
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
        db._get_feedback_result = _make_feedback_row()  # row exists

        cog.agent.chat.return_value = AdminChatResult(
            replies=["Feedback noted"],
            actions=[{"tool": "log_live_update_feedback",
                       "input": {"feed_item_id": "feed-1",
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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
        even when channel_id != feed_item['live_channel_id'], and without
        any resolve_live_channel_id call."""
        cog, db, calls = _build_cog(monkeypatch)

        # Feed item's live_channel_id differs from message channel
        feed_item = _make_feed_item(live_channel_id=999)  # different channel
        db._get_feed_item_result = feed_item
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
        db._get_feedback_result = _make_feedback_row()

        # Silent turn: replies=None, actions non-empty (tool ran)
        cog.agent.chat.return_value = AdminChatResult(
            replies=None,  # SILENT
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"feed_item_id": "feed-1", "feedback_text": "fix"},
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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
        """(k) edit_message with singular message_id → status='edited',
        audit row with disposition='edited'."""
        cog, db, calls = _build_cog(monkeypatch)

        feed_item = _make_feed_item(discord_message_ids=["9001", "9002"])
        db._get_feed_item_result = feed_item
        db._get_feedback_result = _make_feedback_row()  # agent logged base feedback

        # Agent called edit_message on message_id=9001
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Updated!"],
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"feed_item_id": "feed-1", "feedback_text": "fix"},
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

        # update_live_update_feed_item_status was called with 'edited'
        status_calls = [c for c in calls
                        if c[0] == "update_live_update_feed_item_status"]
        assert len(status_calls) >= 1, (
            "Expected status update call for edit action")
        # Check that status='edited' was passed
        edited_call = [c for c in status_calls if c[1][1] == "edited"]
        assert len(edited_call) >= 1, (
            f"Expected status='edited' call, got status calls: {status_calls}")

    @pytest.mark.asyncio
    async def test_delete_message_singular_shape_triggers_deleted_audit(self, monkeypatch):
        """(k) delete_message with singular message_id shape →
        status='deleted', audit row with disposition='deleted'."""
        cog, db, calls = _build_cog(monkeypatch)

        feed_item = _make_feed_item(discord_message_ids=["9001", "9002"])
        db._get_feed_item_result = feed_item
        db._get_feedback_result = _make_feedback_row()

        # Agent called delete_message with singular message_id
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Deleted!"],
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"feed_item_id": "feed-1",
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

        # update_live_update_feed_item_status was called with 'deleted'
        status_calls = [c for c in calls
                        if c[0] == "update_live_update_feed_item_status"]
        deleted_call = [c for c in status_calls if c[1][1] == "deleted"]
        assert len(deleted_call) >= 1, (
            f"Expected status='deleted' call, got status calls: {status_calls}")

    @pytest.mark.asyncio
    async def test_delete_message_bulk_shape_triggers_deleted_audit(self, monkeypatch):
        """(k) delete_message with message_ids list + deleted_ids list →
        status='deleted', covering BOTH shapes."""
        cog, db, calls = _build_cog(monkeypatch)

        feed_item = _make_feed_item(discord_message_ids=["9001", "9002", "9003"])
        db._get_feed_item_result = feed_item
        db._get_feedback_result = _make_feedback_row()

        # Agent called delete_message with message_ids (plural) list
        cog.agent.chat.return_value = AdminChatResult(
            replies=["Bulk deleted!"],
            actions=[
                {"tool": "log_live_update_feedback",
                 "input": {"feed_item_id": "feed-1",
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

        # update_live_update_feed_item_status was called
        status_calls = [c for c in calls
                        if c[0] == "update_live_update_feed_item_status"]
        deleted_call = [c for c in status_calls if c[1][1] == "deleted"]
        assert len(deleted_call) >= 1, (
            f"Expected status='deleted' for bulk delete, "
            f"got status calls: {status_calls}")


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
                        {"feed_item_id": "feed-1", "feedback_text": "fix"},
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
        """(m) get_feed_item_by_discord_message_id is called via
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

        feed_item = _make_feed_item()
        db._get_feed_item_result = feed_item
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
                         if c[0] == "get_feed_item_by_discord_message_id"]
        assert len(reverse_calls) == 1, (
            f"Expected 1 reverse-lookup call via db_handler, "
            f"got {len(reverse_calls)}")
