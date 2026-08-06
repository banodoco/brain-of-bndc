"""Tests for soft-deleting a user's messages from the public hivemind corpus.

Covers DatabaseHandler.soft_delete_user_messages, which flips
discord_messages.is_deleted=true to hide rows from the public
message_feed/unified_feed views without touching Discord.
"""
import types

import pytest

from src.common.db_handler import DatabaseHandler


class FakeQueryBuilder:
    """Records the chained supabase calls issued by the method under test."""

    def __init__(self, count: int = 0):
        self.filters = []
        self.update_payload = None
        self.count = count
        self.table_name = None

    def table(self, name):
        self.table_name = name
        return self

    def select(self, *cols, **kwargs):
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def execute(self):
        return types.SimpleNamespace(count=self.count, data=[{}] * self.count)


class FakeClient:
    def __init__(self, count: int = 0):
        self.builder = FakeQueryBuilder(count)

    def table(self, name):
        return self.builder.table(name)


def _make_db(fake_client: FakeClient) -> DatabaseHandler:
    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = types.SimpleNamespace(supabase_client=fake_client)
    db._gate_check = lambda guild_id: True
    return db


def test_dry_run_counts_without_updating():
    client = FakeClient(count=5)
    db = _make_db(client)

    result = db.soft_delete_user_messages(author_id=123, guild_id=456, dry_run=True)

    assert result == 5
    assert client.builder.update_payload is None
    # count query filters on the caller's messages that aren't already deleted
    assert ("eq", "author_id", 123) in client.builder.filters
    assert ("eq", "guild_id", 456) in client.builder.filters
    assert ("eq", "is_deleted", False) in client.builder.filters


def test_real_run_flags_messages_and_returns_count():
    client = FakeClient(count=3)
    db = _make_db(client)

    result = db.soft_delete_user_messages(author_id=123, guild_id=456, dry_run=False)

    assert result == 3
    assert client.builder.update_payload is not None
    assert client.builder.update_payload["is_deleted"] is True
    assert ("eq", "author_id", 123) in client.builder.filters
    assert ("eq", "guild_id", 456) in client.builder.filters
    assert ("eq", "is_deleted", False) in client.builder.filters


def test_zero_messages_skips_update():
    client = FakeClient(count=0)
    db = _make_db(client)

    result = db.soft_delete_user_messages(author_id=123, guild_id=456, dry_run=False)

    assert result == 0
    assert client.builder.update_payload is None


def test_missing_supabase_client_returns_none():
    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = types.SimpleNamespace(supabase_client=None)
    db._gate_check = lambda guild_id: True

    assert db.soft_delete_user_messages(123, 456, dry_run=False) is None
