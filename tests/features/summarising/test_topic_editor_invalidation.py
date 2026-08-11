"""Focused tests: social-draft invalidation in TopicEditor._dispatch_discard."""

import logging
from unittest.mock import MagicMock

import pytest

from src.features.summarising.topic_editor import TopicEditor


# ── factory helpers ────────────────────────────────────────────────────

def _make_db(list_open_return=None, list_open_side_effect=None):
    """Build a mock DatabaseHandler with methods used by _dispatch_discard."""
    db = MagicMock()
    db.update_topic.return_value = True
    if list_open_side_effect is not None:
        db.list_reviewable_social_runs_for_topic.side_effect = list_open_side_effect
    else:
        db.list_reviewable_social_runs_for_topic.return_value = list_open_return or []
    db.update_live_update_social_run.return_value = True
    return db


def _make_editor(db, **kwargs):
    """Create a TopicEditor with a mock bot and the given db."""
    bot = MagicMock()
    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=MagicMock(),
        guild_id=42,
        live_channel_id=100,
        environment='prod',
    )
    for k, v in kwargs.items():
        setattr(editor, k, v)
    return editor


def _make_call(topic_id='topic-1', reason='no longer relevant'):
    return {
        'id': 'call-001',
        'name': 'discard_topic',
        'input': {
            'topic_id': topic_id,
            'reason': reason,
        },
    }


def _make_context(topic_id='topic-1', guild_id=42, run_id='run-xyz'):
    topic = {
        'topic_id': topic_id,
        'state': 'watching',
        'guild_id': guild_id,
    }
    return {
        'active_topics': [topic],
        'guild_id': guild_id,
        'run_id': run_id,
    }


# ── tests ──────────────────────────────────────────────────────────────

def test_dispatch_discard_invalidates_open_runs():
    """_dispatch_discard calls list_reviewable_social_runs_for_topic + per-row expire."""
    db = _make_db(list_open_return=[
        {'run_id': 'run-a'},
        {'run_id': 'run-b'},
    ])
    editor = _make_editor(db)

    result = editor._dispatch_discard(_make_call(), _make_context())

    assert result['outcome'] == 'accepted'
    assert result['action'] == 'discard'

    db.list_reviewable_social_runs_for_topic.assert_called_once_with(
        topic_id='topic-1',
        environment='prod',
    )
    assert db.update_live_update_social_run.call_count == 2

    # Verify each call used approval_state='expired'
    for call_args in db.update_live_update_social_run.call_args_list:
        _args, _kwargs = call_args
        assert _kwargs.get('approval_state') == 'expired'
        assert _kwargs.get('environment') == 'prod'

    # Verify the right run_ids were processed (helper uses keyword args)
    run_ids = [
        c.kwargs.get('run_id') or (c[0][0] if c[0] else None)
        for c in db.update_live_update_social_run.call_args_list
    ]
    assert set(run_ids) == {'run-a', 'run-b'}


def test_dispatch_discard_no_open_runs_no_update_calls():
    """When no open runs exist, update_live_update_social_run is never called."""
    db = _make_db(list_open_return=[])
    editor = _make_editor(db)

    result = editor._dispatch_discard(_make_call(), _make_context())

    assert result['outcome'] == 'accepted'
    db.list_reviewable_social_runs_for_topic.assert_called_once()
    db.update_live_update_social_run.assert_not_called()


def test_dispatch_discard_topic_not_watching_no_invalidation():
    """When the topic is not in 'watching' state, invalidation is skipped."""
    db = _make_db()
    editor = _make_editor(db)

    ctx = _make_context()
    ctx['active_topics'][0]['state'] = 'discarded'  # not watching → early return

    result = editor._dispatch_discard(_make_call(), ctx)

    # Should return error, not accepted
    assert result['outcome'] == 'tool_error'
    db.list_reviewable_social_runs_for_topic.assert_not_called()
    db.update_live_update_social_run.assert_not_called()


def test_dispatch_discard_invalidation_failure_is_logged(caplog):
    """When list_reviewable_social_runs_for_topic raises, dispatch continues and the error is logged."""
    db = _make_db(list_open_side_effect=RuntimeError("db connection lost"))
    editor = _make_editor(db)

    caplog.set_level(logging.ERROR)
    result = editor._dispatch_discard(_make_call(), _make_context())

    # The discard must still succeed — the invalidation error is swallowed
    assert result['outcome'] == 'accepted'
    assert result['action'] == 'discard'

    # Exception must be logged with identifying details
    assert any(
        'social-draft invalidation failed' in r.message
        and 'topic_discarded' in r.message
        for r in caplog.records
    ), f"Expected invalidation failure log; got: {[r.message for r in caplog.records]}"

    # update_live_update_social_run must NOT have been called (the list call failed)
    db.update_live_update_social_run.assert_not_called()
