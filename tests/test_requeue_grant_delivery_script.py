import argparse
import asyncio
from types import SimpleNamespace

import pytest

from scripts import requeue_grant_delivery_update as requeue


class Query:
    def __init__(self, row):
        self.row = row
        self.filters = {}
        self.patch = None

    def select(self, *_args):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        if all(self.row.get(key) == value for key, value in self.filters.items()):
            if self.patch is not None:
                self.row.update(self.patch)
            return SimpleNamespace(data=[dict(self.row)])
        return SimpleNamespace(data=[])


class FakeSupabase:
    def __init__(self, row):
        self.row = row

    def table(self, _name):
        return Query(self.row)


class FakeDb:
    def __init__(self, row, *, applicant_id=50, writable=True):
        self.storage_handler = SimpleNamespace(supabase_client=FakeSupabase(row))
        self.applicant_id = applicant_id
        self.writable = writable

    def _gate_check(self, _guild_id):
        return self.writable

    def get_grant_by_thread(self, thread_id, guild_id=None):
        return {
            'thread_id': thread_id, 'guild_id': guild_id,
            'applicant_id': self.applicant_id, 'status': 'paid',
            'paid_at': '2026-10-26T00:00:00+00:00',
        }


class FakeThread:
    id = 800
    guild = SimpleNamespace(id=1)

    def __init__(self, message):
        self.message = message

    async def fetch_message(self, message_id):
        assert message_id == self.message.id
        return self.message


class FakeBot:
    def __init__(self, *, intents):
        self.intents = intents
        self.logged_in = False
        self.closed = False
        self.started = False
        self.thread = None

    async def login(self, token):
        assert token == 'not-a-real-token'
        self.logged_in = True

    async def fetch_channel(self, channel_id):
        assert self.logged_in
        assert channel_id == 800
        return self.thread

    async def close(self):
        self.closed = True

    async def start(self, *_args, **_kwargs):
        self.started = True
        raise AssertionError('REST-only flow must not open the gateway')


def make_target():
    message = SimpleNamespace(
        id=700, channel=SimpleNamespace(id=800),
        author=SimpleNamespace(id=50, bot=False),
    )
    thread = FakeThread(message)
    row = {
        'guild_id': 1, 'thread_id': 800, 'message_id': 700,
        'applicant_id': 50, 'state': 'acknowledged', 'ack_message_id': 701,
        'assessment': {'classification': 'needs_info'},
    }
    return row, message, thread


def test_process_now_requires_execute_flag():
    args = argparse.Namespace(process_now=True, execute=False)
    with pytest.raises(ValueError, match='requires --execute'):
        requeue.validate_options(args)


def test_process_now_validates_target_then_uses_login_without_gateway(monkeypatch):
    row, message, thread = make_target()
    db = FakeDb(row)
    bots = []

    def client_factory(*, intents):
        bot = FakeBot(intents=intents)
        bot.thread = thread
        bots.append(bot)
        return bot

    class FakeStore:
        def __init__(self, _db):
            self.row = row

        def get(self, _guild_id, _message_id):
            return dict(self.row)

    class FakeHandler:
        def __new__(cls):
            return object.__new__(cls)

        async def _process_delivery_update(self, source, grant):
            assert source is message
            assert grant['status'] == 'paid'
            assert row['state'] == 'pending'
            assert row['ack_message_id'] == 701
            row['state'] = 'acknowledged'

    monkeypatch.setattr(requeue.discord, 'Thread', FakeThread)
    monkeypatch.setattr(requeue, 'GrantDeliveryStore', FakeStore)

    asyncio.run(requeue._process_now(
        db, row, guild_id=1, thread_id=800, message_id=700,
        token='not-a-real-token', client_factory=client_factory,
        handler_class=FakeHandler,
    ))

    assert row['state'] == 'acknowledged'
    assert row['ack_message_id'] == 701
    assert len(bots) == 1
    assert bots[0].logged_in and bots[0].closed
    assert not bots[0].started
    assert bots[0].intents.guilds is False


def test_process_now_refuses_wrong_applicant_before_reset_and_closes_client(monkeypatch):
    row, _message, thread = make_target()
    db = FakeDb(row, applicant_id=99)
    bots = []

    def client_factory(*, intents):
        bot = FakeBot(intents=intents)
        bot.thread = thread
        bots.append(bot)
        return bot

    monkeypatch.setattr(requeue.discord, 'Thread', FakeThread)
    with pytest.raises(ValueError, match='applicant'):
        asyncio.run(requeue._process_now(
            db, row, guild_id=1, thread_id=800, message_id=700,
            token='not-a-real-token', client_factory=client_factory,
        ))

    assert row['state'] == 'acknowledged'
    assert bots[0].closed
    assert not bots[0].started


def test_reset_uses_real_write_gate_and_exact_prior_ack_identity():
    row, _, _ = make_target()
    db = FakeDb(row, writable=False)
    with pytest.raises(PermissionError, match='Writes are not allowed'):
        requeue._reset_target(db, row)
    assert row['state'] == 'acknowledged'

    db.writable = True
    requeue._reset_target(db, row)
    assert row['state'] == 'pending'
    assert row['ack_message_id'] == 701
