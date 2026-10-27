"""Integration-level checks for paid-grant delivery follow-up routing."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from src.features.grants import grants_cog


class FakeDeliveryStore:
    def __init__(self):
        self.rows = {}
        self.claimed = set()
        self.fail_create = False
        self.fail_ack_persist_once = False

    def create(self, guild_id, message_id, thread_id, applicant_id, content, source_url=None, evidence=None):
        if self.fail_create:
            raise RuntimeError('db unavailable')
        key = (guild_id, message_id)
        return self.rows.setdefault(key, {
            'guild_id': guild_id, 'message_id': message_id, 'thread_id': thread_id,
            'applicant_id': applicant_id, 'content': content, 'source_url': source_url,
            'evidence': evidence or [], 'assessment': None, 'state': 'pending',
        })

    def claim(self, guild_id, message_id, worker_id):
        row = self.rows[(guild_id, message_id)]
        key = (guild_id, message_id)
        if row['state'] in ('acknowledged', 'ignored'):
            return None
        if key in self.claimed:
            expiry = row.get('lease_until')
            if not expiry or datetime.fromisoformat(expiry.replace('Z', '+00:00')) > datetime.now(timezone.utc):
                return None
        self.claimed.add(key)
        row['state'] = 'processing'
        row['lease_owner'] = worker_id
        return dict(row)

    def update(self, guild_id, message_id, fields, *, worker_id=None):
        row = self.rows[(guild_id, message_id)]
        if row.get('lease_owner') != worker_id:
            raise PermissionError('stale lease owner')
        if fields.get('state') == 'acknowledged' and self.fail_ack_persist_once:
            self.fail_ack_persist_once = False
            raise RuntimeError('database write failed after Discord reply')
        row.update(fields)
        if fields.get('state') in ('acknowledged', 'ignored'):
            self.claimed.discard((guild_id, message_id))
        return dict(row)

    def history(self, guild_id, thread_id, limit=100):
        return [r for r in self.rows.values() if r['guild_id'] == guild_id and r['thread_id'] == thread_id]

    def pending(self, guild_id, limit=100):
        return []


class FakeThread:
    parent_id = 900

    def __init__(self, thread_id=800):
        self.id = thread_id
        self.messages = []
        self.last_history = {}

    def history(self, limit=50, **kwargs):
        self.last_history = {'limit': limit, **kwargs}
        async def iterator():
            messages = list(self.messages)
            after = kwargs.get('after')
            if isinstance(after, datetime):
                messages = [m for m in messages if getattr(m, 'created_at', None) and m.created_at > after]
            messages.sort(key=lambda m: (getattr(m, 'created_at', None) or datetime.min.replace(tzinfo=timezone.utc), m.id))
            if not kwargs.get('oldest_first', False):
                messages.reverse()
            for msg in messages[:limit]:
                yield msg
        return iterator()


def make_message(thread, *, message_id=700, author_id=50, content='I finished a milestone'):
    author = SimpleNamespace(id=author_id, bot=False)
    msg = SimpleNamespace(
        id=message_id, author=author, guild=SimpleNamespace(id=1), channel=thread,
        content=content, jump_url=f'https://discord.com/channels/1/800/{message_id}',
        attachments=[], embeds=[], created_at=datetime(2026, 10, 27, 12, tzinfo=timezone.utc),
    )
    async def reply(text, **kwargs):
        ack = SimpleNamespace(
            id=message_id + 1, author=SimpleNamespace(id=999, bot=True), content=text,
            reference=SimpleNamespace(message_id=message_id),
        )
        async def edit(**edit_kwargs):
            ack.content = edit_kwargs['content']
            ack.allowed_mentions = edit_kwargs.get('allowed_mentions')
            return ack
        ack.edit = edit
        ack.allowed_mentions = kwargs.get('allowed_mentions')
        thread.messages.append(ack)
        return ack
    msg.reply = reply
    return msg


def make_cog(store=None, *, applicant_id=50, admin_id=50):
    cog = grants_cog.GrantsCog.__new__(grants_cog.GrantsCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog.delivery_store = store or FakeDeliveryStore()
    cog.admin_id = admin_id
    cog.configured = True
    cog.grants_channel_id = 900
    cog.db = SimpleNamespace(get_grant_by_thread=lambda *_a, **_k: {
        'thread_id': 800, 'guild_id': 1, 'applicant_id': applicant_id,
        'status': 'paid', 'paid_at': '2026-10-26T00:00:00+00:00',
        'thread_content': 'Build a released open source tool',
    })
    cog._processing_threads = set()
    return cog


def test_paid_applicant_who_is_also_admin_routes_to_delivery_before_admin_review(monkeypatch):
    cog = make_cog()
    thread = FakeThread()
    msg = make_message(thread)
    called = []
    async def process(message, grant):
        called.append((message.id, grant['status']))
    async def admin(*_):
        pytest.fail('paid applicant must not enter admin review')
    cog._process_delivery_update = process
    cog._handle_admin_review = admin
    monkeypatch.setattr(grants_cog.discord, 'Thread', FakeThread)

    asyncio.run(cog.on_message(msg))
    assert called == [(700, 'paid')]


def test_duplicate_event_and_reply_then_crash_recovers_without_second_assessment(monkeypatch):
    store = FakeDeliveryStore()
    store.fail_ack_persist_once = True
    cog = make_cog(store)
    thread = FakeThread()
    msg = make_message(thread)
    assessed = []
    async def evidence(*_):
        return []
    async def assess(*_args, **_kwargs):
        assessed.append(1)
        return {'classification': 'progress', 'summary': 'reported milestone', 'response': 'ok', 'evidence_refs': []}
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)

    with pytest.raises(RuntimeError, match='after Discord reply'):
        asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))
    for index in range(60):
        thread.messages.append(SimpleNamespace(
            id=9000 + index, author=SimpleNamespace(id=999, bot=True),
            content='other bot message', reference=None,
        ))
    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))
    assert len(assessed) == 1
    assert len(thread.messages) == 61
    assert thread.last_history['limit'] == 1000
    assert store.rows[(1, 700)]['state'] == 'acknowledged'
    assert thread.messages[0].content == '**Grant progress update recorded**\nok'


def test_concurrent_duplicate_events_only_assess_and_reply_once(monkeypatch):
    cog = make_cog()
    thread = FakeThread()
    msg = make_message(thread)
    assessed = []
    async def evidence(*_):
        return []
    async def assess(*_args, **_kwargs):
        await asyncio.sleep(0.01)
        assessed.append(1)
        return {'classification': 'progress', 'summary': 'reported milestone', 'response': 'ok', 'evidence_refs': []}
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)

    async def run_both():
        await asyncio.gather(
            cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)),
            cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)),
        )
    asyncio.run(run_both())
    assert len(assessed) == 1
    assert len(thread.messages) == 1


@pytest.mark.parametrize(('classification', 'heading', 'response'), [
    ('completed', '**Grant delivery marked complete**', 'Marked complete based on your report.'),
    ('progress', '**Grant progress update recorded**', 'This is reported progress.'),
    ('needs_info', '**Grant update needs more information**', 'Please add the test results.'),
    ('needs_review', '**Grant update needs review**', 'A reviewer will check this.'),
])
def test_assessment_response_and_classification_heading_are_sent(monkeypatch, classification, heading, response):
    cog = make_cog()
    thread = FakeThread()
    msg = make_message(thread)
    async def evidence(*_):
        return []
    async def assess(*_args, **_kwargs):
        return {'classification': classification, 'summary': 'assessment', 'response': response,
                'evidence_refs': [], 'verification': 'recipient_reported' if classification == 'completed' else 'unverified'}
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)

    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))

    assert len(thread.messages) == 1
    ack = thread.messages[0]
    assert ack.content == f'{heading}\n{response}'
    assert ack.allowed_mentions.everyone is False
    assert ack.allowed_mentions.users is False
    assert ack.allowed_mentions.roles is False
    assert cog.db.get_grant_by_thread(800)['status'] == 'paid'


def test_malformed_assessment_response_uses_bounded_static_fallback(monkeypatch):
    cog = make_cog()
    thread = FakeThread()
    msg = make_message(thread)
    async def evidence(*_):
        return []
    async def assess(*_args, **_kwargs):
        return {'classification': 'needs_info', 'summary': 'assessment', 'response': 'x' * 601}
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)

    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))

    assert thread.messages[0].content == (
        '**Grant update needs more information**\n'
        'Thanks for the update. Could you add a little more detail or link the work for future grant reviews?'
    )


def test_malformed_classification_falls_back_to_human_review_ack(monkeypatch):
    cog = make_cog()
    thread = FakeThread()
    msg = make_message(thread)
    async def evidence(*_):
        return []
    async def assess(*_args, **_kwargs):
        return {'classification': 'unexpected', 'response': None}
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)

    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))

    assert thread.messages[0].content == (
        '**Grant update needs review**\n'
        'Thanks for the update. It has been recorded for future grant reviews.'
    )


def test_legacy_ack_is_edited_during_explicit_reassessment_not_duplicated(monkeypatch):
    store = FakeDeliveryStore()
    cog = make_cog(store)
    thread = FakeThread()
    msg = make_message(thread)
    legacy = SimpleNamespace(
        id=701, author=SimpleNamespace(id=999, bot=True),
        content=f'{grants_cog._DELIVERY_ACK_MARKER}\nGeneric old response',
        reference=SimpleNamespace(message_id=700), edits=0,
    )
    async def edit(**kwargs):
        legacy.content = kwargs['content']
        legacy.allowed_mentions = kwargs.get('allowed_mentions')
        legacy.edits += 1
        return legacy
    legacy.edit = edit
    thread.messages.append(legacy)
    async def evidence(*_):
        return []
    async def assess(*_args, **_kwargs):
        return {'classification': 'needs_info', 'summary': 'assessment',
                'response': 'Please link the beta and list GPUs tested.', 'evidence_refs': []}
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)
    # Simulate the narrowly-targeted operator requeue path: only this row is reset.
    store.rows[(1, 700)] = {
        'guild_id': 1, 'message_id': 700, 'thread_id': 800, 'applicant_id': 50,
        'content': msg.content, 'source_url': msg.jump_url, 'evidence': [], 'assessment': None,
        'state': 'pending', 'ack_message_id': 701,
    }

    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))

    assert legacy.edits == 1
    assert legacy.content == '**Grant update needs more information**\nPlease link the beta and list GPUs tested.'
    assert legacy.allowed_mentions.everyone is False
    assert legacy.allowed_mentions.users is False
    assert legacy.allowed_mentions.roles is False
    assert len(thread.messages) == 1
    assert store.rows[(1, 700)]['ack_message_id'] == 701
    assert store.rows[(1, 700)]['state'] == 'acknowledged'


def test_reassessed_ack_edits_in_place_and_marks_reported_completion_without_verifying_payment(monkeypatch):
    store = FakeDeliveryStore()
    cog = make_cog(store)
    thread = FakeThread()
    msg = make_message(thread)
    legacy = SimpleNamespace(
        id=701, author=SimpleNamespace(id=999, bot=True),
        content=f'{grants_cog._DELIVERY_ACK_MARKER}\nGeneric old response',
        reference=SimpleNamespace(message_id=700), edits=0,
    )
    async def edit(**kwargs):
        legacy.content = kwargs['content']
        legacy.allowed_mentions = kwargs.get('allowed_mentions')
        legacy.edits += 1
        return legacy
    legacy.edit = edit
    thread.messages.append(legacy)
    prior_evidence = {
        'id': 'discord:1:22:333', 'url': 'https://discord.com/channels/1/22/333',
        'status': 'available', 'content': 'Astrid beta launch announcement',
        'reference_type': 'discord_message',
    }
    store.rows[(1, 600)] = {
        'guild_id': 1, 'message_id': 600, 'thread_id': 800, 'applicant_id': 50,
        'content': 'I did this!', 'evidence': [prior_evidence],
        'assessment': {'classification': 'progress', 'verification': 'unverified'},
        'state': 'acknowledged',
    }
    # This is the one-row reset performed by the explicit requeue script.
    store.rows[(1, 700)] = {
        'guild_id': 1, 'message_id': 700, 'thread_id': 800, 'applicant_id': 50,
        'content': msg.content, 'source_url': msg.jump_url, 'evidence': [], 'assessment': None,
        'state': 'pending', 'ack_message_id': 701,
    }
    received_prior = []
    async def evidence(*_):
        return []
    async def assess(_grant, _update, _evidence, *, prior_updates=None):
        received_prior.extend(prior_updates or [])
        return {
            'classification': 'completed', 'verification': 'recipient_reported',
            'summary': 'Applicant reports beta shipment; prior announcement available.',
            'response': 'Marked complete based on your update and the launch you shared.',
            'evidence_refs': ['discord:1:22:333'],
        }
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', assess)

    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))

    assert any(row['message_id'] == 600 for row in received_prior)
    assert legacy.edits == 1
    assert legacy.content.startswith('**Grant delivery marked complete**\n')
    assert 'based on your update' in legacy.content.casefold()
    assert legacy.allowed_mentions.everyone is False
    assert legacy.allowed_mentions.users is False
    assert legacy.allowed_mentions.roles is False
    assert len(thread.messages) == 1
    current = store.rows[(1, 700)]
    assert current['state'] == 'acknowledged'
    assert current['ack_message_id'] == 701
    assert current['assessment']['verification'] == 'recipient_reported'
    assert cog.db.get_grant_by_thread(800)['status'] == 'paid'


def test_store_failure_and_unavailable_assessor_do_not_ack_then_retry(monkeypatch):
    store = FakeDeliveryStore()
    cog = make_cog(store)
    thread = FakeThread()
    msg = make_message(thread)
    store.fail_create = True
    with pytest.raises(RuntimeError, match='db unavailable'):
        asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))
    assert not thread.messages

    store.fail_create = False
    async def evidence(*_):
        return []
    attempts = []
    async def unavailable(*_args, **_kwargs):
        attempts.append(1)
        raise RuntimeError('provider unavailable')
    monkeypatch.setattr(grants_cog, 'collect_delivery_evidence', evidence)
    monkeypatch.setattr(grants_cog, 'assess_delivery', unavailable)
    with pytest.raises(RuntimeError, match='provider unavailable'):
        asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))
    assert not thread.messages
    assert store.rows[(1, 700)]['assessment'] is None


def test_unrelated_author_in_paid_thread_is_ignored(monkeypatch):
    cog = make_cog(applicant_id=50, admin_id=77)
    thread = FakeThread()
    msg = make_message(thread, author_id=77)
    monkeypatch.setattr(grants_cog.discord, 'Thread', FakeThread)
    asyncio.run(cog.on_message(msg))
    assert not cog.delivery_store.rows
    assert not thread.messages


def test_update_before_paid_at_is_not_recorded():
    cog = make_cog()
    thread = FakeThread()
    msg = make_message(thread)
    msg.created_at = datetime(2026, 10, 25, tzinfo=timezone.utc)
    asyncio.run(cog._process_delivery_update(msg, cog.db.get_grant_by_thread(800)))
    assert not cog.delivery_store.rows


def test_startup_recovery_catches_recent_paid_update(monkeypatch):
    class FakeForum:
        def __init__(self, thread):
            self.threads = [thread]

        async def archived_threads(self, limit=100):
            if False:
                yield None

    thread = FakeThread()
    message = make_message(thread, message_id=1564628170676637799)
    thread.messages.append(message)
    forum = FakeForum(thread)
    guild = SimpleNamespace(id=1, get_channel=lambda _channel_id: forum)
    cog = make_cog()
    cog.bot.guilds = [guild]
    seen = []
    async def process(msg, _grant):
        seen.append(msg.id)
    cog._process_delivery_update = process
    monkeypatch.setattr(grants_cog.discord, 'ForumChannel', FakeForum)
    monkeypatch.setattr(grants_cog.discord, 'Thread', FakeThread)

    asyncio.run(cog._recover_paid_delivery_updates())
    assert seen == [1564628170676637799]


def test_backlog_beyond_one_hundred_messages_is_paginated_across_sweeps(monkeypatch):
    class FakeForum:
        def __init__(self, thread):
            self.threads = [thread]

        async def archived_threads(self, limit=100):
            if False:
                yield None

    thread = FakeThread()
    base = datetime.now(timezone.utc) - timedelta(days=1)
    for index in range(101):
        msg = make_message(thread, message_id=2000 + index)
        msg.created_at = base + timedelta(minutes=index)
        thread.messages.append(msg)
    forum = FakeForum(thread)
    guild = SimpleNamespace(id=1, get_channel=lambda _channel_id: forum)
    cog = make_cog()
    cog.db.get_grant_by_thread = lambda *_a, **_k: {
        'thread_id': 800, 'guild_id': 1, 'applicant_id': 50,
        'status': 'paid', 'paid_at': (base - timedelta(minutes=1)).isoformat(),
    }
    cog.bot.guilds = [guild]
    seen = []
    async def process(msg, _grant):
        seen.append(msg.id)
    cog._process_delivery_update = process
    monkeypatch.setattr(grants_cog.discord, 'ForumChannel', FakeForum)
    monkeypatch.setattr(grants_cog.discord, 'Thread', FakeThread)

    asyncio.run(cog._recover_paid_delivery_updates())
    assert seen == list(range(2000, 2100))
    asyncio.run(cog._recover_paid_delivery_updates())
    assert seen[-1] == 2100
