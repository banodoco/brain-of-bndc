"""End-to-end tests for _fire_social_handoff called from _publish_topic."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.features.summarising.topic_editor import TopicEditor


# ── factory helpers ────────────────────────────────────────────────────

def _make_db():
    """Build a minimal mocked DatabaseHandler for the publish path."""
    db = MagicMock()
    db.update_topic.return_value = True
    db.get_topic_editor_source_messages.return_value = []
    db.get_channel_name_lookup.return_value = {}
    return db


def _make_bot(*, with_service=True, service_raises=None):
    """Build a mock bot with an optional live_update_social_service."""
    bot = MagicMock()
    if with_service:
        svc = MagicMock()
        svc.handle_live_update_publish_results = AsyncMock(
            side_effect=service_raises
        )
        bot.live_update_social_service = svc
    bot.get_channel.return_value = None
    return bot


def _make_channel_stub(message_ids=None):
    """A mock Discord channel whose .send returns a message with the given ids."""
    ids = iter(message_ids or [9001])
    channel = MagicMock()

    async def _send(content=None, file=None):
        mid = next(ids, 9001)
        msg = MagicMock()
        msg.id = mid
        return msg

    channel.send = AsyncMock(side_effect=_send)
    return channel


def _make_topic_editor(bot, db, **kwargs):
    """Create a TopicEditor with bot/db injected and publishing enabled."""
    overrides = {
        'bot': bot,
        'db_handler': db,
        'llm_client': MagicMock(),
        'guild_id': 42,
        'live_channel_id': 100,
        'environment': 'prod',
        'publishing_enabled': True,
    }
    overrides.update(kwargs)
    editor = TopicEditor(
        bot=overrides.pop('bot'),
        db_handler=overrides.pop('db_handler'),
        llm_client=overrides.pop('llm_client'),
        guild_id=overrides.pop('guild_id'),
        live_channel_id=overrides.pop('live_channel_id'),
        environment=overrides.pop('environment'),
    )
    # Apply remaining overrides directly
    for k, v in overrides.items():
        setattr(editor, k, v)
    return editor


# ── topic fixtures ─────────────────────────────────────────────────────

LEGACY_TOPIC = {
    'topic_id': 'topic-legacy-1',
    'headline': 'Legacy Test Headline',
    'summary': {'body': 'Legacy summary body text.'},
    'source_message_ids': ['111', '222'],
    'guild_id': 42,
}

STRUCTURED_TOPIC = {
    'topic_id': 'topic-structured-1',
    'headline': 'Structured Test Headline',
    'summary': {
        'blocks': [
            {
                'type': 'intro',
                'title': None,
                'text': 'Introductory text for the structured topic.',
                'source_message_ids': ['333'],
                'media_refs': [],
            },
        ],
    },
    'source_message_ids': ['333'],
    'guild_id': 42,
}


# ── tests: legacy path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_publish_sent_fires_handoff_once():
    """Legacy topic with successful publish fires exactly one social handoff."""
    db = _make_db()
    bot = _make_bot()
    channel = _make_channel_stub(message_ids=[9001])
    editor = _make_topic_editor(bot, db)

    # Stub Discord resolution
    editor._resolve_discord_channel = AsyncMock(return_value=channel)

    result = await editor._publish_topic(dict(LEGACY_TOPIC))

    assert result['status'] == 'sent'
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_called_once()

    # Inspect the payload
    call_args, _ = svc.handle_live_update_publish_results.call_args
    payload = call_args[0]
    assert payload.topic_id == 'topic-legacy-1'
    assert payload.guild_id == 42
    assert payload.channel_id == 100
    assert payload.platform == 'twitter'
    assert payload.action == 'post'
    assert payload.status == 'sent'
    assert payload.topic_summary_data['title'] == 'Legacy Test Headline'
    assert payload.topic_summary_data['message_id'] == '9001'
    assert payload.topic_summary_data['subTopics'] == []
    assert 'mainMediaMessageId' not in payload.topic_summary_data
    assert 'channel_id' in payload.topic_summary_data
    assert payload.topic_summary_data['channel_id'] == '100'
    # The topic's actual content rides along so the social agent can draft
    # without a read-tool round trip.
    assert payload.topic_summary_data['summary'] == {'body': 'Legacy summary body text.'}


@pytest.mark.asyncio
async def test_legacy_publish_failed_no_handoff():
    """Legacy topic whose channel raises → status 'failed' → no handoff fires."""
    db = _make_db()
    bot = _make_bot()
    editor = _make_topic_editor(bot, db)

    # Force Discord resolution to raise
    editor._resolve_discord_channel = AsyncMock(side_effect=RuntimeError("channel gone"))

    result = await editor._publish_topic(dict(LEGACY_TOPIC))

    assert result['status'] == 'failed'
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_publish_suppressed_no_handoff():
    """Legacy topic with publishing disabled → status 'suppressed' → no handoff."""
    db = _make_db()
    bot = _make_bot()
    editor = _make_topic_editor(
        bot, db,
        publishing_enabled=False,
    )

    result = await editor._publish_topic(dict(LEGACY_TOPIC))

    assert result['status'] == 'suppressed'
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_not_called()


# ── tests: structured path ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_structured_publish_sent_fires_handoff_once():
    """Structured (block-based) topic fires exactly one social handoff on success."""
    db = _make_db()
    bot = _make_bot()
    channel = _make_channel_stub(message_ids=[7001])
    editor = _make_topic_editor(bot, db)

    editor._resolve_discord_channel = AsyncMock(return_value=channel)

    result = await editor._publish_topic(dict(STRUCTURED_TOPIC))

    assert result['status'] == 'sent'
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_called_once()

    call_args, _ = svc.handle_live_update_publish_results.call_args
    payload = call_args[0]
    assert payload.topic_id == 'topic-structured-1'
    assert payload.platform == 'twitter'
    assert payload.action == 'post'
    assert payload.status == 'sent'
    assert payload.topic_summary_data['title'] == 'Structured Test Headline'
    assert payload.topic_summary_data['message_id'] == '7001'
    assert payload.topic_summary_data['subTopics'] == []
    assert 'mainMediaMessageId' not in payload.topic_summary_data
    # Blocks-shaped summaries ride along verbatim.
    assert payload.topic_summary_data['summary'] == STRUCTURED_TOPIC['summary']

    # Verify source_metadata carries environment and publish_diagnostics
    assert payload.source_metadata['cog'] == 'topic_editor'
    assert payload.source_metadata['environment'] == 'prod'
    assert 'publish_diagnostics' in payload.source_metadata


@pytest.mark.asyncio
async def test_structured_publish_failed_no_handoff():
    """Structured topic whose channel raises → status 'failed' → no handoff."""
    db = _make_db()
    bot = _make_bot()
    editor = _make_topic_editor(bot, db)

    editor._resolve_discord_channel = AsyncMock(side_effect=RuntimeError("channel gone"))

    result = await editor._publish_topic(dict(STRUCTURED_TOPIC))

    assert result['status'] == 'failed'
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_not_called()


@pytest.mark.asyncio
async def test_structured_publish_suppressed_no_handoff():
    """Structured topic with publishing disabled → status 'suppressed' → no handoff."""
    db = _make_db()
    bot = _make_bot()
    editor = _make_topic_editor(
        bot, db,
        publishing_enabled=False,
    )

    result = await editor._publish_topic(dict(STRUCTURED_TOPIC))

    assert result['status'] == 'suppressed'
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_not_called()


# ── tests: fault tolerance ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handoff_service_raises_does_not_propagate(caplog):
    """When the service raises, _publish_topic still returns its dict normally.

    The exception is raised *inside* the fire-and-forget task scheduled by
    asyncio.create_task, so the try/except in _fire_social_handoff does NOT
    catch it — the event-loop exception handler logs it instead.  The
    important contract is that _publish_topic still returns its authoritative
    dict and does not re-raise.
    """
    import asyncio as _asyncio

    db = _make_db()
    bot = _make_bot(service_raises=RuntimeError("service explosion"))
    channel = _make_channel_stub(message_ids=[8001])
    editor = _make_topic_editor(bot, db)
    editor._resolve_discord_channel = AsyncMock(return_value=channel)

    caplog.set_level(logging.ERROR)
    result = await editor._publish_topic(dict(LEGACY_TOPIC))
    # Let the fire-and-forget task settle so the event loop can log the exception
    await _asyncio.sleep(0)

    # Result must be returned normally despite the service blow-up
    assert result['status'] == 'sent'
    assert result['discord_message_ids'] == [8001]

    # The task-level exception is caught by the event loop's exception handler
    # and logged via asyncio's logger, not our own.
    exception_logged = any(
        'service explosion' in r.message
        or 'Task exception was never retrieved' in r.message
        for r in caplog.records
    )
    assert exception_logged, (
        f"Expected service explosion to be logged; got: "
        f"{[r.message for r in caplog.records]}"
    )

    # Service was still called (the error happened inside it)
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_called_once()


# ── tests: sanity (bot / service absent) ───────────────────────────────

def test_handoff_bot_none_noop():
    """_fire_social_handoff returns immediately when self.bot is None."""
    editor = _make_topic_editor(None, _make_db())
    # Should not raise — just return
    editor._fire_social_handoff(
        topic=dict(LEGACY_TOPIC),
        channel_id=100,
        status='sent',
        sent_ids=[1],
        source_message_ids=['111'],
        publish_diagnostics={},
    )


def test_handoff_service_none_noop():
    """_fire_social_handoff returns when live_update_social_service is None."""
    bot = _make_bot(with_service=False)
    editor = _make_topic_editor(bot, _make_db())
    editor._fire_social_handoff(
        topic=dict(LEGACY_TOPIC),
        channel_id=100,
        status='sent',
        sent_ids=[1],
        source_message_ids=['111'],
        publish_diagnostics={},
    )


def test_handoff_unsupported_status_noop():
    """_fire_social_handoff returns immediately for 'failed' and other non-eligible statuses."""
    bot = _make_bot()
    editor = _make_topic_editor(bot, _make_db())
    editor._fire_social_handoff(
        topic=dict(LEGACY_TOPIC),
        channel_id=100,
        status='failed',
        sent_ids=[],
        source_message_ids=['111'],
        publish_diagnostics={},
    )
    svc = bot.live_update_social_service
    svc.handle_live_update_publish_results.assert_not_called()
