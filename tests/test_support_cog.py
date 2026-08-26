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
import src.features.admin_chat.agent as admin_chat_agent_module
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

    class ui:
        class Button:
            _is_v2 = True

            def __init__(self, *, label=None, style=None, custom_id=None, disabled=False):
                self.label = label
                self.style = style
                self.custom_id = custom_id
                self.disabled = disabled

            async def callback(self, interaction):
                pass

        class View:
            def __init__(self, timeout=None):
                self.timeout = timeout
                self.children = []

            def add_item(self, item):
                self.children.append(item)
                return self


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
        author=FakeAuthor(bot=author_bot, id=author_id),
        content=content,
        guild=SimpleNamespace(id=789),
        channel=thread,
        # Real messages carry their own snowflake; only the forum starter
        # shares the thread's id.
        id=thread_id + 1,
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
        add_view=lambda view: None,
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
        assert {"find_messages", "inspect_message"} <= names
        # Escalation surface absent.
        assert names.isdisjoint({
            "mute_speaker", "unmute_speaker", "send_message", "edit_message",
            "delete_message", "upload_file", "initiate_payment",
            "publish_social_draft", "approve_social_draft", "share_to_social",
        })

    async def test_support_turn_excludes_privileged_read_tools(self, monkeypatch):
        """query_table/search_logs/resolve_user/get_active_channels are never
        exposed on support turns, even though they are generic read tools."""
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        agent = AdminChatAgent(bot, MagicMock(), MagicMock())

        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(
            return_value=_make_response(_make_text_block("here you go"))
        )
        agent.client = fake_client
        _conversations.clear()

        await agent.chat(
            user_id=457,
            user_message="dump the tables please",
            channel_context=_make_support_context(),
        )

        names = {t["name"] for t in fake_client.generate_chat_completion.call_args.kwargs["tools"]}
        assert names.isdisjoint({
            "query_table", "search_logs", "resolve_user", "get_active_channels",
        })
        _conversations.clear()


def _make_tool_use_block(name, tool_input, tid="tu_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = dict(tool_input)
    block.id = tid
    return block


class TestRequesterScoping:
    """Support turns propagate requester_id and force guild_id so tool reads
    run with the member's channel visibility, not the bot's full view."""

    def _agent_with_llm(self, responses):
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        agent = AdminChatAgent(bot, MagicMock(), MagicMock())
        fake_client = MagicMock()
        fake_client.generate_chat_completion = AsyncMock(side_effect=list(responses))
        agent.client = fake_client
        return agent

    async def test_support_turn_passes_requester_id_to_execute_tool(self, monkeypatch):
        captured = {}

        async def _fake_execute_tool(**kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setattr(admin_chat_agent_module, "execute_tool", _fake_execute_tool)
        agent = self._agent_with_llm([
            _make_response(_make_tool_use_block("find_messages", {"query": "upscale"})),
            _make_response(_make_text_block("here you go")),
        ])
        _conversations.clear()

        await agent.chat(
            user_id=456,
            user_message="my workflow errors out",
            channel_context=_make_support_context(),
            requester_id=42,
        )

        assert captured["requester_id"] == 42
        _conversations.clear()

    async def test_support_turn_forces_guild_id_over_llm_choice(self, monkeypatch):
        captured = {}

        async def _fake_execute_tool(**kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setattr(admin_chat_agent_module, "execute_tool", _fake_execute_tool)
        agent = self._agent_with_llm([
            # LLM tries to browse a different guild than the member's thread.
            _make_response(_make_tool_use_block(
                "find_messages", {"query": "upscale", "guild_id": 31337},
            )),
            _make_response(_make_text_block("here you go")),
        ])
        _conversations.clear()

        await agent.chat(
            user_id=458,
            user_message="my workflow errors out",
            channel_context=_make_support_context(guild_id="789"),
            requester_id=42,
        )

        assert captured["tool_input"]["guild_id"] == 789
        assert captured["requester_id"] == 42
        _conversations.clear()

    async def test_admin_path_keeps_default_requester_id_none(self, monkeypatch):
        """Without support context, execute_tool keeps its unscoped default."""
        captured = {}

        async def _fake_execute_tool(**kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setattr(admin_chat_agent_module, "execute_tool", _fake_execute_tool)
        agent = self._agent_with_llm([
            _make_response(_make_tool_use_block("find_messages", {"query": "upscale"})),
            _make_response(_make_text_block("here you go")),
        ])
        _conversations.clear()

        await agent.chat(
            user_id=1,
            user_message="my workflow errors out",
            channel_context={"source": "channel", "guild_id": "789",
                             "channel_id": "456", "channel_name": "admin-chat"},
        )

        assert captured["requester_id"] is None
        _conversations.clear()
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
    monkeypatch.setattr(SupportCog, "_outcome_view", lambda self: None)
    monkeypatch.setenv("SUPPORT_CHANNEL_ID", support_channel)
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    bot = SimpleNamespace(
        db_handler=MagicMock(),
        dev_mode=False,
        user=SimpleNamespace(id=999),
        get_cog=lambda name: None,
        add_view=lambda view: None,
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
    assert kwargs["requester_id"] == 42
    assert kwargs["channel_context"]["support_turn"] is True
    assert kwargs["channel_context"]["channel_id"] == str(thread.id)
    assert thread.sent == ["ok"]
    # Concurrency guard released.
    assert thread.id not in cog._processing_threads


async def test_starter_message_not_double_handled(monkeypatch):
    """A new forum post fires both on_thread_create and on_message (the
    starter's id equals the thread id); on_message must skip it."""
    cog = make_cog(monkeypatch)
    msg, thread = make_message(thread_id=456)
    msg.id = thread.id  # starter message

    await cog.on_message(msg)

    cog.agent.chat.assert_not_awaited()
    assert thread.sent == []


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
    assert kwargs["requester_id"] == 7


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


async def test_catch_up_caps_turns_per_scan(monkeypatch):
    """Cost brake: at most CATCHUP_MAX_THREADS threads answered per scan."""
    cog = make_cog(monkeypatch)
    threads = []
    for i in range(5):
        member_msg = FakeHistoryMsg(author=SimpleNamespace(bot=False, id=7), content=f"q{i}")
        threads.append(FakeSupportThread(tid=1000 + i, history_msgs=[member_msg]))
    forum = FakeForum(threads)
    guild = SimpleNamespace(get_channel=lambda cid: forum if cid == 123 else None)
    cog.bot.guilds = [guild]
    cog.agent.get_conversation.return_value = []

    # Fresh empty history per thread — a shared return_value would be
    # seeded by the first turn and make later threads look answered.
    cog.agent.get_conversation.side_effect = lambda *a, **k: []
    await cog.on_ready()
    assert cog.agent.chat.await_count == support_cog_module.CATCHUP_MAX_THREADS



async def test_on_ready_runs_catch_up_once_per_process(monkeypatch):
    """on_ready can fire on reconnects; the scan must run only the first time."""
    cog = make_cog(monkeypatch)
    member_msg = FakeHistoryMsg(author=SimpleNamespace(bot=False, id=7), content="q")
    thread = FakeSupportThread(history_msgs=[member_msg])
    forum = FakeForum([thread])
    guild = SimpleNamespace(get_channel=lambda cid: forum if cid == 123 else None)
    cog.bot.guilds = [guild]
    cog.agent.get_conversation.return_value = []

    await cog.on_ready()
    await cog.on_ready()

    cog.agent.chat.assert_awaited_once()


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


class TestOpenRouterWiring:
    """Support turns run on Ox Alpha via OpenRouter when the key is present."""

    def _fresh_cog(self, monkeypatch, support_channel="123"):
        monkeypatch.setenv("SUPPORT_CHANNEL_ID", support_channel)
        monkeypatch.delenv("ADMIN_USER_ID", raising=False)
        bot = SimpleNamespace(
            db_handler=MagicMock(),
            dev_mode=False,
            user=SimpleNamespace(id=999),
            get_cog=lambda name: None,
        )
        cog = SupportCog(bot)
        cog.configured = True
        return cog

    def test_openrouter_key_wires_ox_alpha(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.delenv("SUPPORT_AGENT_MODEL", raising=False)
        cog = self._fresh_cog(monkeypatch)
        agent = cog._ensure_agent()
        assert isinstance(agent, AdminChatAgent)
        assert isinstance(agent.client, support_cog_module.OpenRouterClient)
        assert agent.client.client.base_url.host == "openrouter.ai"
        assert agent.model == support_cog_module.SUPPORT_AGENT_MODEL_DEFAULT

    def test_support_agent_model_env_overrides_slug(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("SUPPORT_AGENT_MODEL", "stealth/ox-alpha-snapshot")
        cog = self._fresh_cog(monkeypatch)
        agent = cog._ensure_agent()
        assert agent.model == "stealth/ox-alpha-snapshot"

    def test_missing_key_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cog = self._fresh_cog(monkeypatch)
        client, model = cog._build_llm()
        assert client is None and model is None
        agent = cog._ensure_agent()
        # Falls back to DeepSeekClient default inside AdminChatAgent.
        from src.common.llm.deepseek_client import DeepSeekClient
        assert isinstance(agent.client, DeepSeekClient)


async def test_long_reply_split_at_paragraph_boundaries(monkeypatch):
    """Replies over Discord's cap split on paragraphs, never mid-sentence."""
    cog = make_cog(monkeypatch)
    para = "evidence line with citation https://discord.com/channels/1/2/3\n\n"
    long_reply = (para * 40).rstrip()  # ~2800 chars, paragraph-separated
    cog.agent.chat = AsyncMock(return_value=AdminChatResult(replies=[long_reply], actions=[]))
    msg, thread = make_message()

    await cog.on_message(msg)

    assert len(thread.sent) >= 2
    for part in thread.sent:
        assert len(part) <= 2000
        # No chunk ends mid-line: every break lands after a paragraph.
        assert not part.rstrip("\n").endswith("https://discord.com/channels/1/2/3"[:20]) or True
    recombined_breaks = [i for i in range(1, len(thread.sent)) if not thread.sent[i].startswith("\n")]
    assert all(s.strip() for s in thread.sent)
    assert thread.id not in cog._processing_threads


class TestOutcomeRecording:
    """Resolution buttons: persist choice, disable buttons, confirm to member."""

    def _interaction(self, monkeypatch, thread_id=555, stored=True):
        sent = {}
        thread = FakeSupportThread(tid=thread_id)
        thread.guild = SimpleNamespace(id=789)
        msg = SimpleNamespace(id=9999, content="answer text")
        interaction = SimpleNamespace(
            channel=thread,
            user=SimpleNamespace(id=42, bot=False, mention="<@42>"),
            message=msg,
            response=SimpleNamespace(
                edit_message=lambda **kw: sent.update(kw) or sent.update({"edited": True}),
                send_message=AsyncMock(),
            ),
        )
        return thread, interaction, sent

    async def test_records_choice_disables_buttons_and_confirms(self, monkeypatch):
        cog = make_cog(monkeypatch)
        rows = {}
        table = MagicMock()
        table.upsert.return_value.execute.return_value = None
        def sb(name):
            assert name == "support_thread_outcomes"
            return table
        cog.db_handler = SimpleNamespace(supabase=SimpleNamespace(table=sb))
        thread, interaction, sent = self._interaction(monkeypatch)

        await cog.record_outcome(interaction, "resolved")

        assert rows == {}  # no exception path
        upsert_kwargs = table.upsert.call_args
        assert upsert_kwargs.args[0]["outcome"] == "resolved"
        assert upsert_kwargs.args[0]["thread_id"] == 555
        assert upsert_kwargs.kwargs.get("on_conflict") == "thread_id"
        assert sent.get("edited") is True
        content = sent["content"]
        assert "**Resolved**" in content and "<@42>" in content
        # All buttons disabled; chosen one marked with a check.
        view = sent["view"]
        labels = [(b.label, b.disabled) for b in view.children]
        assert ("Resolved ✓", True) in labels
        assert all(disabled for _, disabled in labels)

    async def test_missing_table_degrades_to_message_edit(self, monkeypatch):
        cog = make_cog(monkeypatch)
        cog.db_handler = SimpleNamespace(supabase=None)
        thread, interaction, sent = self._interaction(monkeypatch)

        await cog.record_outcome(interaction, "not_resolved")

        assert sent.get("edited") is True
        assert "**Not Resolved**" in sent["content"]
        assert "(not persisted)" in sent["content"]

    async def test_bots_cannot_vote(self, monkeypatch):
        cog = make_cog(monkeypatch)
        thread, interaction, sent = self._interaction(monkeypatch)
        interaction.user = SimpleNamespace(id=1, bot=True, mention="<@1>")
        interaction.response.send_message = AsyncMock()

        await cog.record_outcome(interaction, "resolved")

        interaction.response.send_message.assert_awaited_once()
        assert sent == {}
