from types import SimpleNamespace

from src.common.db_handler import DatabaseHandler
from src.features.grants.delivery_store import GrantDeliveryStore


class Query:
    def __init__(self, rows):
        self.rows = rows
        self.selected = None
        self.filters = []

    def select(self, columns):
        self.selected = columns
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class Client:
    def __init__(self, rows):
        self.rows = rows
        self.query = None

    def table(self, _name):
        self.query = Query(self.rows)
        return self.query


def make_db(rows):
    db = DatabaseHandler.__new__(DatabaseHandler)
    client = Client(rows)
    db.storage_handler = SimpleNamespace(supabase_client=client)
    return db, client


def test_history_includes_thread_content_and_delivery_updates_scoped_to_grant_guild(monkeypatch):
    grants = [
        {'thread_id': 100, 'guild_id': 1, 'thread_content': 'Original application', 'status': 'paid'},
    ]
    db, client = make_db(grants)
    calls = []

    def history(self, guild_id, thread_id):
        calls.append((guild_id, thread_id))
        return [{'message_id': 201, 'content': 'Progress', 'evidence': []}]

    monkeypatch.setattr(GrantDeliveryStore, 'history', history)
    result = db.get_grant_history_for_applicant(7, guild_id=1)

    assert 'thread_content' in client.query.selected
    assert result[0]['thread_content'] == 'Original application'
    assert result[0]['delivery_updates'][0]['message_id'] == 201
    assert result[0]['delivery_history_status'] == 'available'
    assert calls == [(1, 100)]


def test_delivery_history_failure_is_unknown_and_preserves_grant(monkeypatch):
    grant = {'thread_id': 100, 'guild_id': 1, 'thread_content': 'Original', 'status': 'paid'}
    db, _ = make_db([grant])

    def fail(_self, _guild_id, _thread_id):
        raise RuntimeError('delivery table unavailable')

    monkeypatch.setattr(GrantDeliveryStore, 'history', fail)
    result = db.get_grant_history_for_applicant(7, guild_id=1)

    assert len(result) == 1
    assert result[0]['status'] == 'paid'
    assert result[0]['thread_content'] == 'Original'
    assert result[0]['delivery_updates'] == []
    assert result[0]['delivery_history_status'] == 'unknown'


def test_mismatched_guild_row_is_not_enriched(monkeypatch):
    db, _ = make_db([{'thread_id': 100, 'guild_id': 2, 'status': 'paid'}])
    calls = []
    monkeypatch.setattr(GrantDeliveryStore, 'history', lambda self, g, t: calls.append((g, t)) or [])

    result = db.get_grant_history_for_applicant(7, guild_id=1)

    assert calls == []
    assert result[0]['delivery_history_status'] == 'unknown'


def test_recipient_reported_completion_is_exposed_and_survives_later_needs_info(monkeypatch):
    grant = {'thread_id': 100, 'guild_id': 1, 'thread_content': 'Original', 'status': 'paid'}
    db, _ = make_db([grant])
    rows = [
        {'message_id': 201, 'assessment': {
            'classification': 'completed', 'verification': 'recipient_reported',
        }},
        {'message_id': 202, 'assessment': {
            'classification': 'needs_info', 'verification': 'unverified',
        }},
    ]
    monkeypatch.setattr(GrantDeliveryStore, 'history', lambda *_args: rows)

    result = db.get_grant_history_for_applicant(7, guild_id=1)[0]

    assert result['status'] == 'paid'
    assert result['delivery_status'] == 'completed'
    assert result['delivery_verification'] == 'recipient_reported'
    assert result['delivery_status_message_id'] == 201


def test_latest_needs_info_is_exposed_when_no_completion_exists(monkeypatch):
    grant = {'thread_id': 100, 'guild_id': 1, 'status': 'paid'}
    db, _ = make_db([grant])
    rows = [
        {'message_id': 201, 'assessment': {
            'classification': 'progress', 'verification': 'unverified',
        }},
        {'message_id': 202, 'assessment': {
            'classification': 'needs_info', 'verification': 'unverified',
        }},
        {'message_id': 203, 'assessment': {'classification': 'unrelated'}},
    ]
    monkeypatch.setattr(GrantDeliveryStore, 'history', lambda *_args: rows)

    result = db.get_grant_history_for_applicant(7, guild_id=1)[0]

    assert result['delivery_status'] == 'needs_info'
    assert result['delivery_verification'] == 'unverified'
    assert result['delivery_status_message_id'] == 202
