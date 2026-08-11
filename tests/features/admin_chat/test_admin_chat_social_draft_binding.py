"""Focused tests for the social-draft DM binding block in AdminChatCog._handle_admin_message.

Covers the per-turn channel_context injection added in T7:
  * matched draft run → social_draft_run dict + SOCIAL_DRAFT_REVIEW_GUIDANCE.
  * DM without message.reference → no social_draft_run key.
  * Orphan reply (parent does not resolve to a stored run) → no social_draft_run key.
"""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.features.admin_chat.admin_chat_cog import (
    AdminChatCog,
    SOCIAL_DRAFT_REVIEW_GUIDANCE,
)
from src.features.admin_chat.agent import AdminChatResult


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


# ── factory helpers ──────────────────────────────────────────────────


def _make_db(review_lookup_return=None):
    db = MagicMock()
    db.dev_mode = False
    db.server_config = None
    db.try_claim_bot_event.return_value = True
    # Feedback path stays off — these DM tests don't go through the live-update branch.
    db.get_topic_by_discord_message_id.return_value = None
    db.get_social_run_by_review_message_id.return_value = review_lookup_return
    db.update_live_update_social_run.return_value = True
    return db


def _make_bot():
    bot = MagicMock()
    bot.user.id = 99
    bot.payment_service = None
    bot.guilds = []
    bot.get_guild.return_value = None
    return bot


def _make_dm_message(parent_id=555, author_id=7, content="hi"):
    """Build a DM-like discord.Message: isinstance(channel, DMChannel) is True."""
    channel = MagicMock(spec=discord.DMChannel)
    channel.id = 4242
    channel.history.return_value = _AsyncIter([])
    channel.send = AsyncMock()

    msg = MagicMock()
    msg.id = 9000
    msg.author.id = author_id
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


def _make_cog(db, admin_user_id=7):
    bot = _make_bot()
    cog = AdminChatCog(bot, db, sharer=MagicMock())
    cog._allowed_admin_chat_user_ids = {admin_user_id}
    cog.agent = MagicMock()
    cog.agent.chat = AsyncMock(
        return_value=AdminChatResult(replies=None, actions=[])
    )
    return cog


def _captured_channel_context(cog):
    """Return the channel_context kwarg passed to agent.chat (must have been called once)."""
    cog.agent.chat.assert_called_once()
    _args, kwargs = cog.agent.chat.call_args
    return kwargs["channel_context"]


# ── tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_reply_to_review_message_binds_social_draft_run():
    """Reply parent resolves to a pending draft → social_draft_run + guidance injected."""
    row = {
        "run_id": "run-XYZ",
        "draft_text": "Hello world",
        "revision": 2,
        "approval_state": "pending",
        "approved_revision": None,
        "terminal_status": None,
        "expires_at": "2030-01-01T00:00:00+00:00",
        "topic_summary_data": {"title": "My Topic"},
    }
    db = _make_db(review_lookup_return=row)
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=555)

    await cog._handle_admin_message(msg)

    db.get_social_run_by_review_message_id.assert_called_once()
    args, kwargs = db.get_social_run_by_review_message_id.call_args
    assert args[0] == 555  # parent_id coerced to int

    ctx = _captured_channel_context(cog)
    assert "social_draft_run" in ctx
    sdr = ctx["social_draft_run"]
    assert sdr["run_id"] == "run-XYZ"
    assert sdr["topic_title"] == "My Topic"
    assert sdr["revision"] == 2
    assert sdr["approval_state"] == "pending"
    assert ctx["channel_guidance"] == SOCIAL_DRAFT_REVIEW_GUIDANCE


@pytest.mark.asyncio
async def test_dm_without_reference_skips_binding():
    """A DM with no message.reference at all → no social_draft_run, no review lookup."""
    db = _make_db(review_lookup_return=None)
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=None)

    await cog._handle_admin_message(msg)

    db.get_social_run_by_review_message_id.assert_not_called()

    ctx = _captured_channel_context(cog)
    assert "social_draft_run" not in ctx


@pytest.mark.asyncio
async def test_dm_reply_orphan_does_not_inject_social_draft_run():
    """Reply whose parent does NOT resolve to a stored run → no social_draft_run key."""
    db = _make_db(review_lookup_return=None)  # lookup miss
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=12345)

    await cog._handle_admin_message(msg)

    db.get_social_run_by_review_message_id.assert_called_once()

    ctx = _captured_channel_context(cog)
    assert "social_draft_run" not in ctx


@pytest.mark.asyncio
async def test_dm_reply_to_published_run_does_not_inject():
    """Defensive: if a published row is returned, binding is skipped (per cog guard)."""
    row = {
        "run_id": "run-published",
        "draft_text": "...",
        "revision": 5,
        "approval_state": "approved",
        "terminal_status": "published",
        "topic_summary_data": {"title": "Already shipped"},
    }
    db = _make_db(review_lookup_return=row)
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=555)

    await cog._handle_admin_message(msg)

    ctx = _captured_channel_context(cog)
    assert "social_draft_run" not in ctx


@pytest.mark.asyncio
async def test_dm_reply_to_failed_run_injects_failed_context():
    """A failed-publish run binds social_failed_run + failed guidance (retryable)."""
    from src.features.admin_chat.admin_chat_cog import SOCIAL_FAILED_REVIEW_GUIDANCE

    row = {
        "run_id": "run-failed-1",
        "draft_text": "Draft that failed to publish",
        "revision": 3,
        "approval_state": "approved",
        "terminal_status": "failed",
        "expires_at": "2030-01-01T00:00:00+00:00",
        "publication_outcome": {"success": False, "error": "No social route configured for twitter"},
        "topic_summary_data": {"title": "Failed Topic"},
    }
    db = _make_db(review_lookup_return=row)
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=555, content="retry the publish")

    await cog._handle_admin_message(msg)

    ctx = _captured_channel_context(cog)
    assert "social_failed_run" in ctx
    fr = ctx["social_failed_run"]
    assert fr["run_id"] == "run-failed-1"
    assert fr["draft_text"] == "Draft that failed to publish"
    assert "No social route configured" in fr["failure"]
    assert ctx["channel_guidance"] == SOCIAL_FAILED_REVIEW_GUIDANCE
    assert "social_draft_run" not in ctx


@pytest.mark.asyncio
async def test_dm_reply_to_proposed_run_injects_proposal_context():
    """A proposed run binds social_proposal_run + proposal guidance."""
    from src.features.admin_chat.admin_chat_cog import SOCIAL_PROPOSAL_REVIEW_GUIDANCE

    row = {
        "run_id": "run-proposed-1",
        "draft_text": None,
        "revision": 0,
        "approval_state": "pending",
        "terminal_status": "proposed",
        "expires_at": "2030-01-01T00:00:00+00:00",
        "proposals": [
            {"theme": "Idea one", "media_strategy": "single clip",
             "source_message_ids": ["42"], "rationale": "why"},
        ],
        "topic_summary_data": {"title": "Proposal Topic"},
    }
    db = _make_db(review_lookup_return=row)
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=555, content="let's do the first one")

    await cog._handle_admin_message(msg)

    ctx = _captured_channel_context(cog)
    assert "social_proposal_run" in ctx
    pr = ctx["social_proposal_run"]
    assert pr["run_id"] == "run-proposed-1"
    assert len(pr["proposals"]) == 1
    assert ctx["channel_guidance"] == SOCIAL_PROPOSAL_REVIEW_GUIDANCE
    assert "social_draft_run" not in ctx


@pytest.mark.asyncio
async def test_dm_reply_to_expired_run_treated_as_orphan():
    """An expired review DM (past expires_at) must NOT inject review context."""
    row = {
        "run_id": "run-expired",
        "draft_text": "Stale draft",
        "revision": 1,
        "approval_state": "pending",
        "terminal_status": "draft",
        "expires_at": "2020-01-01T00:00:00+00:00",  # long past
        "topic_summary_data": {"title": "Old"},
    }
    db = _make_db(review_lookup_return=row)
    cog = _make_cog(db)
    msg = _make_dm_message(parent_id=555, content="what's this draft?")

    await cog._handle_admin_message(msg)

    ctx = _captured_channel_context(cog)
    assert "social_draft_run" not in ctx
    assert "social_proposal_run" not in ctx
    assert "social_failed_run" not in ctx
