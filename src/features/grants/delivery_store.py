"""Durable storage for individual grant-delivery updates.

These rows track applicant messages and their follow-up assessment lifecycle.
They are intentionally independent from grant funding/payment records.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4


TABLE = 'grant_delivery_updates'
STATES = {'pending', 'processing', 'assessed', 'acknowledged', 'ignored'}
_MUTABLE = {
    'thread_id', 'applicant_id', 'content', 'source_url', 'evidence',
    'assessment', 'state', 'ack_message_id', 'lease_owner', 'lease_until',
}


def _row_data(result: Any) -> list[dict[str, Any]]:
    data = getattr(result, 'data', None)
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return list(data)


class GrantDeliveryStore:
    """Persistence adapter over the application's Supabase client.

    Writes fail closed when the guild is not writable, and database errors are
    allowed to propagate so callers can retry instead of reporting success.
    """

    def __init__(self, db: Any, lease_seconds: int = 300):
        self.db = db
        self.lease_seconds = max(int(lease_seconds), 1)

    @property
    def _client(self):
        client = getattr(getattr(self.db, 'storage_handler', None), 'supabase_client', None)
        if client is None:
            raise RuntimeError('Supabase client is not initialized')
        return client

    def _check_write(self, guild_id: int) -> None:
        gate = getattr(self.db, '_gate_check', None)
        if gate is None or not gate(guild_id):
            raise PermissionError(f'Writes are not allowed for guild {guild_id}')

    def get(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        rows = _row_data(
            self._client.table(TABLE).select('*')
            .eq('guild_id', int(guild_id)).eq('message_id', int(message_id)).limit(1).execute()
        )
        return rows[0] if rows else None

    def create(
        self,
        guild_id: int,
        message_id: int,
        thread_id: int,
        applicant_id: int,
        content: str,
        source_url: str | None = None,
        evidence: Any = None,
    ) -> dict[str, Any]:
        """Insert once per source message; duplicate delivery returns original row."""
        self._check_write(guild_id)
        payload = {
            'guild_id': int(guild_id), 'message_id': int(message_id),
            'thread_id': int(thread_id), 'applicant_id': int(applicant_id),
            'content': content, 'source_url': source_url,
            'evidence': evidence if evidence is not None else [], 'state': 'pending',
        }
        try:
            inserted = _row_data(self._client.table(TABLE).insert(payload).execute())
            if inserted:
                return inserted[0]
        except Exception:
            # A concurrent gateway delivery may have won the unique-key insert.
            existing = self.get(guild_id, message_id)
            if existing is not None:
                return existing
            raise
        existing = self.get(guild_id, message_id)
        if existing is not None:
            return existing
        raise RuntimeError('Delivery insert returned no row')

    def update(self, guild_id: int, message_id: int, fields: Mapping[str, Any], *, worker_id: str | None = None) -> dict[str, Any]:
        self._check_write(guild_id)
        payload = dict(fields)
        unknown = payload.keys() - _MUTABLE
        if unknown:
            raise ValueError(f'Unsupported delivery fields: {sorted(unknown)}')
        if 'state' in payload and payload['state'] not in STATES:
            raise ValueError(f"Invalid delivery state: {payload['state']}")
        payload['updated_at'] = datetime.now(timezone.utc).isoformat()
        query = self._client.table(TABLE).update(payload).eq('guild_id', int(guild_id)).eq('message_id', int(message_id))
        current = self.get(guild_id, message_id)
        if current is None:
            raise KeyError(f'Delivery update {guild_id}/{message_id} does not exist')
        if current.get('state') not in {'processing', 'assessed'}:
            raise PermissionError('Terminal or unclaimed delivery rows are immutable')
        if not worker_id:
            raise PermissionError('Worker lease owner is required for processor updates')
        expiry = current.get('lease_until')
        now = datetime.now(timezone.utc).isoformat()
        if not expiry or datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            raise PermissionError('Delivery lease has expired')
        query = (query.eq('state', current['state']).eq('lease_owner', worker_id)
                 .eq('lease_until', expiry).gt('lease_until', now))
        rows = _row_data(query.execute())
        if rows:
            return rows[0]
        raise PermissionError('Delivery row was not updated; lease owner is stale or mismatched')

    def history(self, guild_id: int, thread_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return _row_data(
            self._client.table(TABLE).select('*').eq('guild_id', int(guild_id))
            .eq('thread_id', int(thread_id)).order('created_at', desc=False)
            .limit(max(1, min(int(limit), 1000))).execute()
        )

    def claim(self, guild_id: int, message_id: int, worker_id: str | None = None) -> dict[str, Any] | None:
        """Claim pending work or reclaim an expired processing lease.

        Each update is a compare-and-set against the observed state/lease. A
        fresh opaque owner token lets completion reject stale workers.
        """
        self._check_write(guild_id)
        existing = self.get(guild_id, message_id)
        if existing is None or existing.get('state') in {'acknowledged', 'ignored'}:
            return None
        now = datetime.now(timezone.utc)
        old_state = existing.get('state')
        if old_state in {'processing', 'assessed'}:
            expiry = existing.get('lease_until')
            if expiry and datetime.fromisoformat(expiry.replace('Z', '+00:00')) > now:
                return None
        elif old_state not in {'pending', 'assessed'}:
            return None
        token = worker_id or str(uuid4())
        until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        query = self._client.table(TABLE).update({
            'state': 'processing', 'lease_owner': token, 'lease_until': until,
            'updated_at': now.isoformat(),
        }).eq('guild_id', int(guild_id)).eq('message_id', int(message_id)).eq('state', old_state)
        if old_state in {'processing', 'assessed'}:
            expiry = existing.get('lease_until')
            if expiry:
                query = query.eq('lease_until', expiry)
            else:
                query = query.is_('lease_until', 'null')
        rows = _row_data(query.execute())
        return rows[0] if rows else None

    def release(self, guild_id: int, message_id: int, worker_id: str, state: str = 'pending') -> dict[str, Any] | None:
        """Release a lease only if still owned by this worker."""
        if state not in {'pending', 'assessed', 'acknowledged', 'ignored'}:
            raise ValueError(f'Invalid release state: {state}')
        self._check_write(guild_id)
        now = datetime.now(timezone.utc).isoformat()
        current = self.get(guild_id, message_id)
        if current is None or current.get('state') not in {'processing', 'assessed'}:
            return None
        query = self._client.table(TABLE).update({
                'state': state, 'lease_owner': None, 'lease_until': None,
                'updated_at': now,
            }).eq('guild_id', int(guild_id)).eq('message_id', int(message_id))
        query = query.eq('state', current['state']).eq('lease_owner', worker_id)
        expiry = current.get('lease_until')
        if not expiry or datetime.fromisoformat(expiry.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            return None
        query = query.eq('lease_until', expiry).gt('lease_until', datetime.now(timezone.utc).isoformat())
        rows = _row_data(query.execute())
        return rows[0] if rows else None

    def pending(self, guild_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Read due durable work, excluding active leases so they cannot starve retries."""
        cap = max(1, min(int(limit), 1000))
        rows = _row_data(
            self._client.table(TABLE).select('*').eq('guild_id', int(guild_id))
            .eq('state', 'pending').order('updated_at', desc=False).limit(cap).execute()
        )
        remaining = cap - len(rows)
        now = datetime.now(timezone.utc).isoformat()
        for state in ('processing', 'assessed'):
            if remaining <= 0:
                break
            expired = _row_data(
                self._client.table(TABLE).select('*').eq('guild_id', int(guild_id))
                .eq('state', state).lte('lease_until', now)
                .order('updated_at', desc=False).limit(remaining).execute()
            )
            rows.extend(expired)
            remaining = cap - len(rows)
            if remaining > 0:
                missing_lease = _row_data(
                    self._client.table(TABLE).select('*').eq('guild_id', int(guild_id))
                    .eq('state', state).is_('lease_until', 'null')
                    .order('updated_at', desc=False).limit(remaining).execute()
                )
                rows.extend(missing_lease)
                remaining = cap - len(rows)
        return rows

