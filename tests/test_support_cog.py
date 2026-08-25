"""Tests for the #support auto-agent: SupportCog + agent plumbing.

Covers:
- support_turn tool allowlist scoping (public-surface safety)
- session continuation across turns keyed by thread id
- post-restart history rebuild (build_seed_history)
- SupportCog listener guards and fallback error path
- execute_tool dispatcher registration for search_hivemind / comfy_workflow
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


import src.features.support.support_cog as support_cog_module
import pytest

from src.features.admin_chat.agent import AdminChatAgent, AdminChatResult, _conversations
from src.features.support.support_cog import SupportCog, build_seed_history

pytestmark = pytest.mark.anyio


# ========== Shared mock helpers ==========


def _make_text_block(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(*blocks):
    resp = MagicMock()
    resp.content = list(blocks)
    resp.stop_reason = "end_turn"
    return resp


def _make_support_context(**overrides):
    ctx = {
        "source": "support",
        "guild_id": "789",
        "channel_id": "456",
        "channel_name": "how do i upscale",
        "is_thread": True,
        "parent_channel_id": "123",
        "environment": "prod",
        "support_turn": True,
        "channel_guidance": "support guidance",
    }
    ctx.update(overrides)
    return ctx


class FakeAuthor(SimpleNamespace):
    pass


class FakeHistoryMsg(SimpleNamespace):
    pass


class FakeDiscordShim:
    """Minimal stand-in so isinstance(channel, discord.Thread) passes."""

    class Thread:
        pass

    class ForumChannel:
        pass

    utils = SimpleNamespace(utcnow=__import__("datetime").datetime.utcnow)


class FakeSupportThread(FakeDiscordShim.Thread):
    def __init__(self, *, tid=456, parent_id=123, history_msgs=None):
        self.id = tid
        self.name = "how do i upscale"
        self.parent_id = parent_id
        self.guild = SimpleNamespace(id=789)
        self.archived = False
        self.joined = False
        self.sent = []
        self._history = list(history_msgs or [])

    async def join(self):
        self.joined = True

    async def send(self, content=None, **kwargs):
        self.sent.append(content)

    def history(self, limit=None, oldest_first=False):
        msgs = reversed(self._history) if not oldest_first else iter(self._history)
        return _AsyncIter(msgs)

    def add_history(self, msg):
        self._history.append(msg)


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def make_message(*, content="help me", author_bot=False, author_id=42,
                 thread_parent=123, thread_id=456):
    thread = FakeSupportThread(tid=thread_id, parent_id=thread_parent)
    msg = SimpleNamespace(
        author=FakeAuthor(bot=author_bot, uid=author_id),
        content=content,
        guild=SimpleNamespace(id=789),
        channel=thread,
    )
    return msg, thread


def make_cog(monkeypatch, *, configured=True, support_channel="123"):
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", support_channel)
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    bot = SimpleNamespace(
        db_handler=MagicMock(),
        dev_mode=False,
        user=SimpleNamespace(id=999),
        get_cog=lambda name: None,
    )
    cog = SupportCog(bot)
    cog.configured = configured
    cog.agent = MagicMock()
    cog.agent.chat = AsyncMock(return_value=AdminChatResult(replies=["ok"], actions=[]))
    return cog


# ========== Agent-level: support_turn allowlist ==========


class TestSupportTurnAllowlist:
    """Executor-level barrier: support turns expose read/research + support
    tools only — never payment/moderation/message-mutation/social tools."""

    async def test_support_turn_restricts_tool_surface(self, monkeypatch):
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        agent = AdminChatAgent(bot, MagicMock(), MagicMock())

        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            return_value=_make_response(_make_text_block("here you go"))
        )
        agent.client = fake_client
        _conversations.clear()

        await agent.chat(
            user_id=456,
            user_message="my workflow errors out",
            channel_context=_make_support_context(),
        )

        names = {t["name"] for t in fake_client.generate_chat_completion.call_args.kwargs["tools"]}
        # Support surface present.
        assert {"search_hivemind", "comfy_workflow", "reply", "end_turn"} <= names
        # Read/research surface present.
        assert {"find_messages", "inspect_message", "query_table", "search_logs",
                "resolve_user", "get_active_channels"} <= names
        # Escalation surface absent.
        assert names.isdisjoint({
            "mute_speaker", "unmute_speaker", "send_message", "edit_message",
            "delete_message", "upload_file", "initiate_payment",
            "publish_social_draft", "approve_social_draft", "share_to_social",
        })
        _conversations.clear()


# ========== Agent-level: session continuation ==========


class TestSessionContinuation:
    """Follow-up messages in the same thread continue one conversation."""

    async def test_same_thread_key_shares_history(self, monkeypatch):
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        agent = AdminChatAgent(bot, MagicMock(), MagicMock())

        captured_lengths = []

        async def _capture(**kwargs):
            captured_lengths.append(len(kwargs["messages"]))
            return _make_response(_make_text_block("answer"))

        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(side_effect=_capture)
        agent.client = fake_client
        _conversations.clear()

        ctx = _make_support_context()
        await agent.chat(user_id=456, user_message="first question", channel_context=ctx)
        await agent.chat(user_id=456, user_message="follow up question", channel_context=ctx)

        assert captured_lengths[0] == 1
        assert captured_lengths[1] > captured_lengths[0]
        roles = [m["role"] for m in _conversations[456]]
        assert roles[0] == "user" and "assistant" in roles
        _conversations.clear()


# ========== Post-restart rebuild ==========


class TestBuildSeedHistory:
    def test_member_messages_become_user_turns(self):
        msgs = [
            FakeHistoryMsg(author=SimpleNamespace(bot=False), content="help"),
            FakeHistoryMsg(author=SimpleNamespace(bot=False), content="more detail"),
        ]
        history = build_seed_history(msgs)
        assert history == [
            {"role": "user", "content": "help"},
            {"role": "user", "content": "more detail"},
        ]

    def test_consecutive_bot_messages_merge_into_one_assistant_turn(self):
        msgs = [
            FakeHistoryMsg(author=SimpleNamespace(bot=False), content="question"),
            FakeHistoryMsg(author=SimpleNamespace(bot=True), content="part one"),
            FakeHistoryMsg(author=SimpleNamespace(bot=True), content="part two"),
        ]
        history = build_seed_history(msgs)
        assert history == [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "part one\n\npart two"},
        ]

    def test_attachment_only_messages_skipped(self):
        msgs = [
            FakeHistoryMsg(author=SimpleNamespace(bot=False), content=""),
            FakeHistoryMsg(author=SimpleNamespace(bot=False), content=None),
            FakeHistoryMsg(author=SimpleNamespace(bot=False), content="real question"),
        ]
        history = build_seed_history(msgs)
        assert history == [{"role": "user", "content": "real question"}]


def make_cog(monkeypatch, *, configured=True, support_channel="123"):
    monkeypatch.setattr(support_cog_module, "discord", FakeDiscordShim)
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", support_channel)
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    bot = SimpleNamespace(
        db_handler=MagicMock(),
        dev_mode=False,
        user=SimpleNamespace(id=999),
        get_cog=lambda name: None,
        guilds=[],
    )
    cog = SupportCog(bot)
    cog.configured = configured
    cog.agent = MagicMock()
    cog.agent.chat = AsyncMock(return_value=AdminChatResult(replies=["ok"], actions=[]))
    return cog

class TestCogGuards:
    async def test_not_configured_noops(self, monkeypatch):
        cog = make_cog(monkeypatch, configured=False)
        msg, thread = make_message()
        await cog.on_message(msg)
        cog.agent.chat.assert_not_awaited()

    async def test_foreign_channel_noops(self, monkeypatch):
        cog = make_cog(monkeypatch)
        msg, thread = make_message(thread_parent=999)
        await cog.on_message(msg)
        cog.agent.chat.assert_not_awaited()

    async def test_bot_author_noops(self, monkeypatch):
        cog = make_cog(monkeypatch)
        msg, thread = make_message(author_bot=True)
        await cog.on_message(msg)
        cog.agent.chat.assert_not_awaited()

    async def test_non_thread_channel_noops(self, monkeypatch):
        cog = make_cog(monkeypatch)
        msg, _ = make_message()
        msg.channel = SimpleNamespace(id=1, parent_id=123)
        await cog.on_message(msg)
        cog.agent.chat.assert_not_awaited()



async def test_member_reply_runs_thread_keyed_turn(monkeypatch):
    cog = make_cog(monkeypatch)
    msg, thread = make_message(content="still failing after seed change")

    await cog.on_message(msg)

    kwargs = cog.agent.chat.call_args.kwargs
    assert kwargs["user_id"] == thread.id
    assert kwargs["user_message"] == "still failing after seed change"
    assert kwargs["channel_context"]["support_turn"] is True
    assert kwargs["channel_context"]["channel_id"] == str(thread.id)
    assert thread.sent == ["ok"]
    # Concurrency guard released.
    assert thread.id not in cog._processing_threads


async def test_error_posts_visible_fallback_with_admin_mention(monkeypatch):
    cog = make_cog(monkeypatch)
    monkeypatch.setenv("ADMIN_USER_ID", "42")
    cog.agent.chat = AsyncMock(side_effect=RuntimeError("llm exploded"))
    msg, thread = make_message()

    await cog.on_message(msg)

    assert len(thread.sent) == 1
    assert "<@42>" in thread.sent[0]
    assert thread.id not in cog._processing_threads


async def test_on_thread_create_joins_and_runs_initial_turn(monkeypatch):
    cog = make_cog(monkeypatch)

    starter = FakeHistoryMsg(
        author=SimpleNamespace(bot=False, id=7),
        content="my wan animate workflow throws an error",
    )
    thread = FakeSupportThread(history_msgs=[starter])

    await cog.on_thread_create(thread)

    assert thread.joined
    kwargs = cog.agent.chat.call_args.kwargs
    assert kwargs["user_id"] == thread.id
    assert kwargs["user_message"] == "my wan animate workflow throws an error"


# ========== Catch-up scan ==========


class FakeForum(FakeDiscordShim.ForumChannel):
    def __init__(self, threads):
        self.threads = threads

    def archived_threads(self, limit=None):
        return _AsyncIter([])


async def test_catch_up_answers_missed_thread(monkeypatch):
    cog = make_cog(monkeypatch)

    member_msg = FakeHistoryMsg(author=SimpleNamespace(bot=False, id=7), content="unanswered question")
    thread = FakeSupportThread(history_msgs=[member_msg])
    forum = FakeForum([thread])
    guild = SimpleNamespace(get_channel=lambda cid: forum if cid == 123 else None)
    cog.bot.guilds = [guild]
    # No conversation known for this thread; last message isn't ours.
    cog.agent.get_conversation.return_value = []

    await cog.on_ready()

    cog.agent.chat.assert_awaited_once()
    assert cog.agent.chat.call_args.kwargs["user_id"] == thread.id


async def test_catch_up_skips_thread_bot_already_answered(monkeypatch):
    cog = make_cog(monkeypatch)

    bot_msg = FakeHistoryMsg(author=SimpleNamespace(bot=True, id=999), content="the answer")
    thread = FakeSupportThread(history_msgs=[bot_msg])
    forum = FakeForum([thread])
    guild = SimpleNamespace(get_channel=lambda cid: forum if cid == 123 else None)
    cog.bot.guilds = [guild]
    cog.agent.get_conversation.return_value = []

    await cog.on_ready()

    cog.agent.chat.assert_not_awaited()


# ========== Dispatcher registration ==========


class TestDispatcherRegistration:
    async def _execute(self, tool_name, tool_input, **extra):
        from src.features.admin_chat.tools import execute_tool
        return await execute_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            bot=MagicMock(),
            db_handler=None,
            sharer=None,
            **extra,
        )

    async def test_search_hivemind_routes_to_executor(self, monkeypatch):
        import src.features.support.tools_support as tools_support
        calls = []

        async def fake_exec(params):
            calls.append(params)
            return {"success": True}

        monkeypatch.setattr(tools_support, "execute_search_hivemind", fake_exec)
        result = await self._execute("search_hivemind", {"query": "wan animate"})
        assert result == {"success": True}
        assert calls == [{"query": "wan animate"}]

    async def test_comfy_workflow_routes_to_executor_with_bot(self, monkeypatch):
        import src.features.support.comfy_tools as comfy_tools
        calls = []
        bot = MagicMock()

        async def fake_exec(tool_input, bot=None):
            calls.append((tool_input, bot))
            return {"success": True}

        monkeypatch.setattr(comfy_tools, "execute_comfy_workflow", fake_exec)
        result = await self._execute("comfy_workflow", {"source": "{}"})
        assert result == {"success": True}
        assert calls[0][0] == {"source": "{}"}
        # The live bot object flows through for file posting.
        assert calls[0][1] is not None

    async def test_denied_when_not_in_allowed_tools(self, monkeypatch):
        import src.features.support.tools_support as tools_support
        import src.features.support.comfy_tools as comfy_tools

        async def fail(*a, **k):
            raise AssertionError("executor must not run when denied")

        monkeypatch.setattr(tools_support, "execute_search_hivemind", fail)
        monkeypatch.setattr(comfy_tools, "execute_comfy_workflow", fail)

        for name in ("search_hivemind", "comfy_workflow"):
            result = await self._execute(name, {}, allowed_tools={"something_else"})
            assert result == {"success": False, "error": "Permission denied"}
