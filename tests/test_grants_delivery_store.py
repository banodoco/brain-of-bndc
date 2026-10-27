"""Tests for durable, idempotent grant delivery update storage."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.features.grants.delivery_store import GrantDeliveryStore


class Result:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table, action, payload=None):
        self.table, self.action, self.payload = table, action, payload
        self.filters, self.membership, self.comparisons, self.null_filters = [], [], [], []
        self.less_equal = []
        self.cap, self.sort = None, None

    def select(self, *_):
        self.action = 'select'
        return self

    def insert(self, payload):
        self.action, self.payload = 'insert', payload
        return self

    def update(self, payload):
        self.action, self.payload = 'update', payload
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def gt(self, key, value):
        self.comparisons.append((key, value))
        return self

    def lte(self, key, value):
        self.less_equal.append((key, value))
        return self

    def is_(self, key, value):
        self.null_filters.append((key, value))
        return self

    def in_(self, key, values):
        self.membership.append((key, values))
        return self

    def limit(self, value):
        self.cap = value
        return self

    def order(self, key, desc=False):
        self.sort = (key, desc)
        return self

    def execute(self):
        rows = self.table.rows
        if self.action == 'insert':
            key = (self.payload['guild_id'], self.payload['message_id'])
            if key in rows:
                raise ValueError('duplicate key')
            row = deepcopy(self.payload)
            row.setdefault('created_at', datetime.now(timezone.utc).isoformat())
            row.setdefault('updated_at', row['created_at'])
            rows[key] = row
            return Result([deepcopy(row)])
        matches = [r for r in rows.values()
                   if all(r.get(k) == v for k, v in self.filters)
                   and all(r.get(k) in values for k, values in self.membership)
                   and all(r.get(k) is None for k, v in self.null_filters if v == 'null')]
        for key, value in self.comparisons:
            matches = [r for r in matches if r.get(key) and r[key] > value]
        for key, value in self.less_equal:
            matches = [r for r in matches if r.get(key) and r[key] <= value]
        if self.action == 'update':
            for row in matches:
                row.update(deepcopy(self.payload))
            return Result([deepcopy(r) for r in matches])
        if self.sort:
            key, desc = self.sort
            matches.sort(key=lambda r: r.get(key) or '', reverse=desc)
        return Result([deepcopy(r) for r in matches[:self.cap] if self.cap is not None] if self.cap is not None else [deepcopy(r) for r in matches])


class FakeClient:
    def __init__(self):
        self.rows = {}

    def table(self, _name):
        return FakeQuery(self, 'select')


class FakeDB:
    def __init__(self, allowed=True):
        self.storage_handler = type('Storage', (), {'supabase_client': FakeClient()})()
        self.allowed = allowed
        self.gate_calls = []

    def _gate_check(self, guild_id):
        self.gate_calls.append(guild_id)
        return self.allowed


def test_create_is_idempotent_and_history_is_per_thread():
    db = FakeDB()
    store = GrantDeliveryStore(db)
    row = store.create(1, 10, 100, 5, 'first', 'https://discord.test/10')
    duplicate = store.create(1, 10, 100, 5, 'changed')
    store.create(1, 11, 100, 5, 'second')

    assert duplicate['content'] == 'first'
    assert [r['message_id'] for r in store.history(1, 100)] == [10, 11]
    assert db.gate_calls == [1, 1, 1]


def test_claim_is_exclusive_and_expired_lease_is_recoverable():
    store = GrantDeliveryStore(FakeDB(), lease_seconds=60)
    store.create(1, 10, 100, 5, 'update')

    claim = store.claim(1, 10, 'worker-a')
    assert claim['state'] == 'processing'
    assert claim['lease_owner'] == 'worker-a'
    assert store.claim(1, 10, 'worker-b') is None

    key = (1, 10)
    store.db.storage_handler.supabase_client.rows[key]['lease_until'] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    recovered = store.claim(1, 10, 'worker-b')
    assert recovered['lease_owner'] == 'worker-b'
    assert store.release(1, 10, 'worker-a') is None
    assert store.release(1, 10, 'worker-b')['state'] == 'pending'


def test_assessed_row_can_be_reclaimed_for_ack_retry_without_losing_assessment():
    store = GrantDeliveryStore(FakeDB(), lease_seconds=60)
    store.create(1, 10, 100, 5, 'update')
    store.claim(1, 10, 'worker-a')
    store.update(1, 10, {'assessment': {'decision': 'ok'}, 'state': 'assessed'}, worker_id='worker-a')
    key = (1, 10)
    store.db.storage_handler.supabase_client.rows[key]['lease_until'] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()

    retry = store.claim(1, 10, 'worker-b')
    assert retry['assessment'] == {'decision': 'ok'}
    assert retry['state'] == 'processing'
    with pytest.raises(PermissionError, match='lease owner'):
        store.update(1, 10, {'ack_message_id': 20}, worker_id='worker-a')
    done = store.update(1, 10, {'state': 'acknowledged', 'ack_message_id': 21}, worker_id='worker-b')
    assert done['assessment'] == {'decision': 'ok'}
    assert store.claim(1, 10, 'worker-c') is None


def test_assessed_row_without_lease_is_recoverable_and_terminal_rows_are_immutable():
    store = GrantDeliveryStore(FakeDB())
    store.create(1, 10, 100, 5, 'update')
    store.claim(1, 10, 'worker-a')
    store.update(1, 10, {'assessment': {'decision': 'ok'}, 'state': 'assessed'}, worker_id='worker-a')
    key = (1, 10)
    store.db.storage_handler.supabase_client.rows[key]['lease_until'] = None
    retry = store.claim(1, 10, 'worker-b')
    assert retry['assessment'] == {'decision': 'ok'}
    store.update(1, 10, {'state': 'acknowledged', 'ack_message_id': 22}, worker_id='worker-b')
    with pytest.raises(PermissionError, match='immutable'):
        store.update(1, 10, {'ack_message_id': 23}, worker_id='worker-b')


def test_writes_fail_closed_and_errors_are_not_reported_as_success():
    store = GrantDeliveryStore(FakeDB(allowed=False))
    with pytest.raises(PermissionError):
        store.create(1, 10, 100, 5, 'update')
    assert store.get(1, 10) is None


def test_pending_queue_excludes_active_claims_so_due_rows_do_not_starve():
    store = GrantDeliveryStore(FakeDB(), lease_seconds=60)
    store.create(1, 10, 100, 5, 'active')
    store.create(1, 11, 100, 5, 'due')
    store.claim(1, 10, 'worker')
    store.db.storage_handler.supabase_client.rows[(1, 10)]['updated_at'] = '2026-01-01T00:00:00+00:00'
    store.db.storage_handler.supabase_client.rows[(1, 11)]['state'] = 'processing'
    store.db.storage_handler.supabase_client.rows[(1, 11)]['lease_until'] = '2026-01-01T00:00:00+00:00'

    assert [row['message_id'] for row in store.pending(1, limit=1)] == [11]
