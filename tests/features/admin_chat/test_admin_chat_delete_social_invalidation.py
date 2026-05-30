"""Focused tests: social-draft invalidation in admin-chat delete_message path."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.features.admin_chat.admin_chat_cog import AdminChatCog
from src.features.admin_chat.agent import AdminChatResult


# ── async-iterator helper ────────────────────────────────────────────

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


# ── factory helpers ──────────────────────────────────────────────────

def _make_db(list_open_return=None, list_open_side_effect=None):
    db = MagicMock()
    db.dev_mode = False
    db.server_config = None
    db.try_claim_bot_event.return_value = True
    db.get_topic_by_discord_message_id.return_value = {
        'topic_id': 'tid1',
        'discord_message_ids': [111],
        'guild_id': 42,
    }
    db.get_live_update_feedback_for.return_value = {'id': 'fb1'}
    db.update_topic.return_value = True
    db.store_live_update_feedback.return_value = None
    if list_open_side_effect is not None:
        db.list_open_social_runs.side_effect = list_open_side_effect
    else:
        db.list_open_social_runs.return_value = list_open_return if list_open_return is not None else []
    db.update_live_update_social_run.return_value = True
    return db


def _make_bot():
    bot = MagicMock()
    bot.user.id = 99
    bot.payment_service = None
    bot.guilds = []
    bot.get_guild.return_value = None
    return bot


def _make_message(mid=999, author_id=7, guild_id=42, parent_id=200, discord_msg_id=111):
    guild = MagicMock()
    guild.id = guild_id
    guild.get_member.return_value = None

    channel = MagicMock()
    channel.id = 100
    channel.name = "live-updates"
    channel.history.return_value = _AsyncIter([])
    channel.fetch_message = AsyncMock(side_effect=Exception("not cached"))
    channel.send = AsyncMock()

    msg = MagicMock()
    msg.id = mid
    msg.author.id = author_id
    msg.author.bot = False
    msg.author.display_name = "admin"
    msg.guild = guild
    msg.channel = channel
    msg.content = "please delete"
    msg.mentions = []
    msg.reference.message_id = parent_id
    msg.reference.resolved = None
    msg.add_reaction = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_cog(db, admin_user_id=7):
    bot = _make_bot()
    cog = AdminChatCog(bot, db, sharer=MagicMock())
    cog._allowed_admin_chat_user_ids = {admin_user_id}
    cog.agent = MagicMock()
    cog.agent.chat = AsyncMock()
    return cog


def _delete_action(discord_msg_id=111):
    return {
        'tool': 'delete_message',
        'input': {'message_id': str(discord_msg_id)},
        'result': {'deleted_ids': [discord_msg_id]},
    }


# ── tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_invalidates_open_runs():
    """A matching delete_message calls list_open_social_runs + per-row update."""
    db = _make_db(list_open_return=[{'run_id': 'run1'}])
    cog = _make_cog(db)
    msg = _make_message()
    cog.agent.chat = AsyncMock(
        return_value=AdminChatResult(replies=None, actions=[_delete_action()])
    )

    await cog._handle_admin_message(msg)

    db.list_open_social_runs.assert_called_once_with('prod', 50, 'tid1')
    db.update_live_update_social_run.assert_called_once()
    _args, _kwargs = db.update_live_update_social_run.call_args
    assert _args[0] == 'run1'
    assert _kwargs.get('approval_state') == 'expired'


@pytest.mark.asyncio
async def test_delete_multi_action_second_run_empty():
    """Two delete_message actions for the same topic — second has no open runs."""
    db = _make_db()
    db.list_open_social_runs.side_effect = [
        [{'run_id': 'run-A'}],
        [],
    ]
    cog = _make_cog(db)
    msg = _make_message()
    cog.agent.chat = AsyncMock(
        return_value=AdminChatResult(
            replies=None,
            actions=[_delete_action(), _delete_action()],
        )
    )

    await cog._handle_admin_message(msg)

    assert db.list_open_social_runs.call_count == 2
    db.update_live_update_social_run.assert_called_once()


@pytest.mark.asyncio
async def test_delete_invalidation_failure_is_logged_and_flow_continues(caplog):
    """list_open_social_runs raising must be caught; soft-delete still runs."""
    db = _make_db(list_open_side_effect=RuntimeError("db down"))
    cog = _make_cog(db)
    msg = _make_message()
    cog.agent.chat = AsyncMock(
        return_value=AdminChatResult(replies=None, actions=[_delete_action()])
    )

    with caplog.at_level(logging.ERROR):
        await cog._handle_admin_message(msg)

    assert any("social-draft invalidation failed" in r.message for r in caplog.records)
    db.update_topic.assert_called()
    db.update_live_update_social_run.assert_not_called()
