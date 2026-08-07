"""Unit tests for deterministic guild resolution in ServerConfig.

Regression coverage for the #top-gens outage: with two enabled+write guilds in
``server_config``, PostgREST returns unordered rows, and ``resolve_guild_id``
used to pick whichever row came first — which flipped to a near-empty secondary
guild and silently stopped top-creations. Resolution must now prefer the
deployment-bound guild (GUILD_ID/DEV_GUILD_ID) and otherwise be deterministic.
"""

from types import SimpleNamespace

import pytest

from src.common.server_config import ServerConfig

# These mirror the real DB: the secondary server is returned FIRST, the Banodoco
# guild second — the row order that exposed the bug.
BANODOCO_GUILD_ID = 1076117621407223829
SECONDARY_GUILD_ID = 1431366141380395290


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *columns, **kwargs):  # noqa: ARG002
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _FakeSupabase:
    def __init__(self, server_rows, channel_rows=None):
        self._tables = {
            'server_config': _FakeTable(server_rows),
            'channel_effective_config': _FakeTable(channel_rows or []),
        }

    def table(self, name):
        return self._tables.get(name, _FakeTable([]))


def _make_server_config(rows, monkeypatch, configured_guild=None):
    if configured_guild is not None:
        monkeypatch.setenv('GUILD_ID', str(configured_guild))
        monkeypatch.delenv('DEV_GUILD_ID', raising=False)
    else:
        monkeypatch.delenv('GUILD_ID', raising=False)
        monkeypatch.delenv('DEV_GUILD_ID', raising=False)
    return ServerConfig(_FakeSupabase(rows))


def _two_writable_guilds():
    # Wrong (secondary) guild comes first in DB row order — the exact shape that
    # caused the outage when PostgREST chose that order.
    return [
        {'guild_id': SECONDARY_GUILD_ID, 'enabled': True, 'write_enabled': True},
        {'guild_id': BANODOCO_GUILD_ID, 'enabled': True, 'write_enabled': True},
    ]


def test_resolve_guild_id_prefers_configured_guild_over_db_row_order(monkeypatch):
    sc = _make_server_config(_two_writable_guilds(), monkeypatch, BANODOCO_GUILD_ID)
    assert sc.resolve_guild_id(require_write=True) == BANODOCO_GUILD_ID


def test_resolve_guild_id_without_configured_uses_lowest_guild_id(monkeypatch):
    # No GUILD_ID/DEV_GUILD_ID: selection must be deterministic (lowest id),
    # not whatever row PostgREST returned first.
    sc = _make_server_config(_two_writable_guilds(), monkeypatch)
    assert sc.resolve_guild_id(require_write=True) == BANODOCO_GUILD_ID


def test_resolve_guild_id_skips_configured_when_not_writable(monkeypatch):
    rows = [
        {'guild_id': SECONDARY_GUILD_ID, 'enabled': True, 'write_enabled': True},
        {'guild_id': BANODOCO_GUILD_ID, 'enabled': True, 'write_enabled': False},
    ]
    sc = _make_server_config(rows, monkeypatch, BANODOCO_GUILD_ID)
    # Configured guild not writable with require_write=True -> deterministic fallback.
    assert sc.resolve_guild_id(require_write=True) == SECONDARY_GUILD_ID


def test_get_first_server_with_field_prefers_configured_guild(monkeypatch):
    rows = [
        {'guild_id': SECONDARY_GUILD_ID, 'enabled': True, 'write_enabled': True, 'art_channel_id': 111},
        {'guild_id': BANODOCO_GUILD_ID, 'enabled': True, 'write_enabled': True, 'art_channel_id': 222},
    ]
    sc = _make_server_config(rows, monkeypatch, BANODOCO_GUILD_ID)
    server = sc.get_first_server_with_field('art_channel_id', require_write=True)
    assert server['guild_id'] == BANODOCO_GUILD_ID
    assert server['art_channel_id'] == 222


def test_get_first_server_with_field_falls_back_to_field_bearer(monkeypatch):
    # Only the secondary guild carries the field: it must still be found even
    # though the configured guild is preferred when it has the field.
    rows = [
        {'guild_id': BANODOCO_GUILD_ID, 'enabled': True, 'write_enabled': True},
        {'guild_id': SECONDARY_GUILD_ID, 'enabled': True, 'write_enabled': True, 'grants_channel_id': 333},
    ]
    sc = _make_server_config(rows, monkeypatch, BANODOCO_GUILD_ID)
    server = sc.get_first_server_with_field('grants_channel_id', require_write=True)
    assert server['guild_id'] == SECONDARY_GUILD_ID


def test_get_default_guild_id_respects_field_parameter(monkeypatch):
    rows = _two_writable_guilds()
    rows[1]['summary_channel_id'] = 555  # only Banodoco has it
    sc = _make_server_config(rows, monkeypatch, BANODOCO_GUILD_ID)
    assert sc.get_default_guild_id(require_write=True, field='summary_channel_id') == BANODOCO_GUILD_ID
