"""E2E / integration-style tests for admin-chat social-draft wiring (T19).

Covers:
  1. DM binding: channel_context['social_draft_run'] populated from
     get_social_run_by_review_message_id, topic_title from
     topic_summary_data.title, channel_guidance = SOCIAL_DRAFT_REVIEW_GUIDANCE.
  2. Tool dispatch: all 6 new tool names present in TOOLS by name, and
     execute_tool routes each to its executor with bot forwarded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from src.features.admin_chat.admin_chat_cog import (
    AdminChatCog,
    SOCIAL_DRAFT_REVIEW_GUIDANCE,
)
from src.features.admin_chat.agent import AdminChatResult
from src.features.admin_chat.tools import (
    TOOLS,
    execute_tool,
)


# ── async-iterator helper ─────────────────────────────────────────────

class _AsyncIter:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


# ── Test 1: DM binding ────────────────────────────────────────────────

class TestSocialDraftDMContextBinding:
    """Verify channel_context injection when an admin replies to a draft DM."""

    def _make_db(self, review_lookup_return=None):
        db = MagicMock()
        db.dev_mode = False
        db.server_config = None
        db.try_claim_bot_event.return_value = True
        db.get_topic_by_discord_message_id.return_value = None
        db.get_social_run_by_review_message_id.return_value = review_lookup_return
        db.update_live_update_social_run.return_value = True
        return db

    def _make_bot(self):
        bot = MagicMock()
        bot.user.id = 99
        bot.payment_service = None
        bot.guilds = []
        bot.get_guild.return_value = None
        return bot

    def _make_dm_message(self, parent_id=555, content="review this"):
        channel = MagicMock(spec=discord.DMChannel)
        channel.id = 4242
        channel.history.return_value = _AsyncIter([])
        channel.send = AsyncMock()

        msg = MagicMock()
        msg.id = 9000
        msg.author.id = 7
        msg.author.bot = False
        msg.author.display_name = "admin"
        msg.guild = None
        msg.channel = channel
        msg.content = content
        msg.mentions = []
        if parent_id is None:
            msg.reference = None
        else:
            msg.reference = MagicMock()
            msg.reference.message_id = parent_id
            msg.reference.resolved = None
        msg.add_reaction = AsyncMock()
        msg.delete = AsyncMock()
        return msg

    def _make_cog(self, db, admin_user_id=7):
        bot = self._make_bot()
        cog = AdminChatCog(bot, db, sharer=MagicMock())
        cog._allowed_admin_chat_user_ids = {admin_user_id}
        cog.agent = MagicMock()
        cog.agent.chat = AsyncMock(
            return_value=AdminChatResult(replies=None, actions=[])
        )
        return cog

    def _captured_channel_context(self, cog):
        """Return channel_context kwarg passed to agent.chat (must have been called once)."""
        cog.agent.chat.assert_awaited_once()
        _, kwargs = cog.agent.chat.call_args
        return kwargs.get("channel_context", {})

    @pytest.mark.asyncio
    async def test_dm_reply_to_draft_injects_social_draft_run(self):
        draft_row = {
            "run_id": "run-e2e-1",
            "draft_text": "E2E draft content",
            "revision": 2,
            "approval_state": "pending",
            "approved_revision": None,
            "expires_at": "2026-09-01T00:00:00+00:00",
            "terminal_status": "draft",
            "topic_summary_data": {"title": "E2E Topic"},
        }
        db = self._make_db(review_lookup_return=draft_row)
        cog = self._make_cog(db)
        msg = self._make_dm_message(parent_id=555, content="please approve this")

        await cog._handle_admin_message(msg)

        ctx = self._captured_channel_context(cog)
        sd = ctx.get("social_draft_run")
        assert sd is not None, "social_draft_run should be present in channel_context"
        assert sd["run_id"] == "run-e2e-1"
        assert sd["draft_text"] == "E2E draft content"
        assert sd["revision"] == 2
        assert sd["approval_state"] == "pending"
        assert sd["approved_revision"] is None
        assert sd["topic_title"] == "E2E Topic"
        assert sd["expires_at"] == "2026-09-01T00:00:00+00:00"
        assert ctx["channel_guidance"] == SOCIAL_DRAFT_REVIEW_GUIDANCE
        db.get_social_run_by_review_message_id.assert_called_once_with(
            555, environment="prod"
        )

    @pytest.mark.asyncio
    async def test_no_reference_no_injection(self):
        db = self._make_db()
        cog = self._make_cog(db)
        msg = self._make_dm_message(parent_id=None, content="hello")

        await cog._handle_admin_message(msg)

        ctx = self._captured_channel_context(cog)
        assert "social_draft_run" not in ctx

    @pytest.mark.asyncio
    async def test_orphan_reply_no_injection(self):
        db = self._make_db(review_lookup_return=None)
        cog = self._make_cog(db)
        msg = self._make_dm_message(parent_id=999, content="orphan")

        await cog._handle_admin_message(msg)

        ctx = self._captured_channel_context(cog)
        assert "social_draft_run" not in ctx


# ── Test 2: Tool dispatch ─────────────────────────────────────────────

_SOCIAL_DRAFT_TOOL_NAMES = {
    "update_social_draft",
    "approve_social_draft",
    "publish_social_draft",
    "preview_social_draft",
    "list_pending_social_drafts",
    "discard_social_draft",
}


class TestSocialDraftToolDispatch:
    """Verify the 6 new tool schemas exist in TOOLS and execute_tool routes
    each to its executor."""

    def test_all_6_schemas_present_in_tools_by_name(self):
        tool_names = {t["name"] for t in TOOLS}
        for name in _SOCIAL_DRAFT_TOOL_NAMES:
            assert name in tool_names, f"Tool '{name}' missing from TOOLS"

    @pytest.mark.asyncio
    async def test_execute_tool_routes_each_executor(self):
        """Dispatch each of the 6 tool names; verify the matching executor is
        called exactly once with bot forwarded."""
        bot = MagicMock()
        bot.social_publish_service = None  # service_unavailable is fine — we only check routing

        db = MagicMock()
        db.update_live_update_social_run.return_value = True
        db.get_live_update_social_run.return_value = None  # causes early-return, but we check routing
        db.get_social_run_by_review_message_id.return_value = None
        db.list_pending_review_social_runs.return_value = []

        sharer = MagicMock()

        # Track which executors were invoked
        executor_names = set()

        # We'll patch each executor with a mock that records its invocation.
        patches = []
        mocks: dict[str, MagicMock] = {}
        for tool_name in _SOCIAL_DRAFT_TOOL_NAMES:
            # The executor name is execute_{tool_name}
            executor_name = f"execute_{tool_name}"
            target = f"src.features.admin_chat.tools.{executor_name}"
            mock = AsyncMock(return_value={"success": True})
            mocks[executor_name] = mock
            patches.append(patch(target, mock))

        with patch.dict("os.environ", {"ADMIN_USER_ID": "999"}), \
             patch.dict("os.environ", {"LIVE_UPDATE_SOCIAL_MODE": "publish"}):
            for p in patches:
                p.start()

            try:
                for tool_name in _SOCIAL_DRAFT_TOOL_NAMES:
                    result = await execute_tool(
                        tool_name=tool_name,
                        tool_input={"run_id": "run-e2e-dispatch", "new_text": "t"},
                        bot=bot,
                        db_handler=db,
                        sharer=sharer,
                    )
                    # Don't assert success — we just want to verify routing happened
                    executor_name = f"execute_{tool_name}"
                    mock = mocks[executor_name]
                    mock.assert_awaited_once()
            finally:
                for p in patches:
                    p.stop()

        # Verify each was called exactly once
        for tool_name in _SOCIAL_DRAFT_TOOL_NAMES:
            executor_name = f"execute_{tool_name}"
            mock = mocks[executor_name]
            mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_each_executor_receives_bot(self):
        """Verify that each executor receives the bot object as its first arg."""
        bot = MagicMock()
        bot.social_publish_service = None
        db = MagicMock()
        db.update_live_update_social_run.return_value = True
        db.get_live_update_social_run.return_value = None
        db.get_social_run_by_review_message_id.return_value = None
        db.list_pending_review_social_runs.return_value = []
        sharer = MagicMock()

        for tool_name in _SOCIAL_DRAFT_TOOL_NAMES:
            executor_name = f"execute_{tool_name}"
            target = f"src.features.admin_chat.tools.{executor_name}"
            mock = AsyncMock(return_value={"success": True})
            with patch(target, mock):
                with patch.dict("os.environ", {"ADMIN_USER_ID": "999", "LIVE_UPDATE_SOCIAL_MODE": "publish"}):
                    await execute_tool(
                        tool_name=tool_name,
                        tool_input={"run_id": "run-bot-check"},
                        bot=bot,
                        db_handler=db,
                        sharer=sharer,
                    )

                # Verify bot was passed as first positional arg
                mock.assert_awaited_once()
                args, _kwargs = mock.call_args
                assert args[0] is bot, f"{executor_name} did not receive bot as first arg"
