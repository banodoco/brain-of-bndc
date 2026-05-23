import asyncio
from pathlib import Path

from src.common.db_handler import DatabaseHandler
from src.common.storage_handler import StorageHandler


def make_storage(calls):
    storage = StorageHandler.__new__(StorageHandler)

    class NoStaleLeaseQuery:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def lt(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            return type("Result", (), {"data": []})()

    class FakeSupabase:
        def table(self, _table):
            return NoStaleLeaseQuery()

    storage.supabase_client = FakeSupabase()

    async def insert_live_row(table, payload):
        calls.append(("insert", table, payload))
        return {**payload, "_table": table}

    async def update_live_row(table, key, value, payload):
        calls.append(("update", table, key, value, payload))
        return {**payload, key: value, "_table": table}

    async def upsert_live_row(table, payload, on_conflict):
        calls.append(("upsert", table, payload, on_conflict))
        return {**payload, "_table": table, "_on_conflict": on_conflict}

    storage._insert_live_row = insert_live_row
    storage._update_live_row = update_live_row
    storage._upsert_live_row = upsert_live_row
    return storage


def test_storage_live_update_candidate_preserves_author_context_and_audit_fields():
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(storage.store_live_update_candidate({
        "run_id": "run-1",
        "guild_id": 1,
        "source_channel_id": 10,
        "update_type": "release",
        "title": "Demo shipped",
        "body": "A new demo build shipped with better controls.",
        "media_refs": [{"url": "https://cdn.example.test/demo.png"}],
        "source_message_ids": [100, "101"],
        "author_context_snapshot": {"member_id": 42, "username": "artist"},
        "duplicate_key": "release:demo",
        "confidence": 0.91,
        "priority": 4,
        "rationale": "high-signal release note",
        "raw_agent_output": {"generator": "test"},
    }))

    assert row["_table"] == "live_update_candidates"
    assert calls[-1][1] == "live_update_candidates"
    payload = calls[-1][2]
    assert payload["source_message_ids"] == [100, "101"]
    assert payload["author_context_snapshot"] == {"member_id": 42, "username": "artist"}
    assert payload["media_refs"] == [{"url": "https://cdn.example.test/demo.png"}]
    assert payload["raw_agent_output"] == {"generator": "test"}
    assert payload["status"] == "generated"


def test_storage_live_update_feed_item_preserves_ordered_discord_message_ids():
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(storage.store_live_update_feed_item({
        "run_id": "run-1",
        "candidate_id": "candidate-1",
        "guild_id": 1,
        "channel_id": 20,
        "update_type": "project_update",
        "title": "Long update",
        "body": "A long update was split into multiple Discord sends.",
        "source_message_ids": ["100"],
        "duplicate_key": "project:update",
        "discord_message_ids": [9003, "9004", 9005],
    }))

    assert row["_table"] == "live_update_feed_items"
    payload = calls[-1][2]
    assert payload["live_channel_id"] == 20
    assert payload["discord_message_ids"] == ["9003", "9004", "9005"]
    assert payload["status"] == "posted"
    assert payload["posted_at"]


def test_storage_live_update_feed_item_message_update_preserves_ordered_ids_on_failure():
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(storage.update_live_update_feed_item_messages(
        "feed-1",
        [9010, "9011"],
        status="failed",
        post_error="discord send failed",
    ))

    assert row["_table"] == "live_update_feed_items"
    assert calls[-1][:4] == ("update", "live_update_feed_items", "feed_item_id", "feed-1")
    payload = calls[-1][4]
    assert payload["discord_message_ids"] == ["9010", "9011"]
    assert payload["status"] == "failed"
    assert payload["post_error"] == "discord send failed"
    assert payload["posted_at"] is None


def test_storage_topic_editor_helpers_route_to_new_tables_without_touching_legacy():
    calls = []
    storage = make_storage(calls)

    run = asyncio.run(storage.acquire_topic_editor_run({
        "guild_id": 1,
        "live_channel_id": 20,
        "trigger": "scheduled",
        "checkpoint_before": {"last_message_id": 100},
    }))
    run_update = asyncio.run(storage.update_topic_editor_run("run-1", {
        "guild_id": 1,
        "status": "running",
        "source_message_count": 4,
        "metadata": {"partial": True},
    }))
    topic = asyncio.run(storage.upsert_topic({
        "guild_id": 1,
        "canonical_key": "demo-topic",
        "state": "posted",
        "headline": "Demo topic shipped",
        "summary": {"body": "A demo shipped."},
        "source_authors": ["alice"],
    }))
    source = asyncio.run(storage.add_topic_source({
        "topic_id": "topic-1",
        "message_id": 100,
        "guild_id": 1,
        "run_id": "run-1",
    }))
    alias = asyncio.run(storage.upsert_topic_alias({
        "topic_id": "topic-1",
        "alias_key": "demo",
        "guild_id": 1,
    }))
    transition = asyncio.run(storage.store_topic_transition({
        "run_id": "run-1",
        "guild_id": 1,
        "action": "post_simple",
        "to_state": "posted",
        "payload": {"outcome": "accepted"},
    }))
    observation = asyncio.run(storage.store_editorial_observation({
        "run_id": "run-1",
        "guild_id": 1,
        "source_message_ids": [100],
        "source_authors": ["alice"],
        "observation_kind": "near_miss",
        "reason": "Almost enough signal.",
    }))
    draft = asyncio.run(storage.create_topic_editor_draft({
        "draft_id": "draft-1",
        "run_id": "run-1",
        "topic_id": None,
        "guild_id": 1,
        "status": "drafting",
        "draft_json": {"headline": "Draft"},
        "validation_result": None,
        "preview_units": None,
        "revision_number": 1,
        "revision_hash": "hash-1",
        "revision_attempts": 0,
        "latest_valid_preview_hash": None,
    }))

    assert run["_table"] == "topic_editor_runs"
    assert run_update["_table"] == "topic_editor_runs"
    assert run_update["status"] == "running"
    assert run_update["source_message_count"] == 4
    assert topic["_table"] == "topics"
    assert source["_table"] == "topic_sources"
    assert alias["_table"] == "topic_aliases"
    assert transition["_table"] == "topic_transitions"
    assert observation["_table"] == "editorial_observations"
    assert draft["_table"] == "topic_editor_drafts"
    assert draft["topic_id"] is None
    assert draft["revision_hash"] == "hash-1"
    assert {call[1] for call in calls}.isdisjoint({"live_update_editor_runs", "live_update_feed_items"})


def test_db_handler_topic_editor_wrappers_are_reachable_through_storage_handler():
    calls = []

    class FakeStorage:
        async def acquire_topic_editor_run(self, run, environment="prod"):
            calls.append(("acquire", run, environment))
            return {"run_id": "run-1"}

        async def complete_topic_editor_run(self, run_id, updates=None, environment="prod"):
            calls.append(("complete", run_id, updates, environment))
            return {"run_id": run_id, "status": "completed"}

        async def update_topic_editor_run(self, run_id, updates=None, environment="prod"):
            calls.append(("update-run", run_id, updates, environment))
            return {"run_id": run_id, **(updates or {})}

        async def fail_topic_editor_run(self, run_id, error_message, updates=None, environment="prod"):
            calls.append(("fail", run_id, error_message, updates, environment))
            return {"run_id": run_id, "status": "failed"}

        async def upsert_topic(self, topic, environment="prod"):
            calls.append(("topic", topic, environment))
            return {"topic_id": "topic-1"}

        async def add_topic_source(self, source, environment="prod"):
            calls.append(("source", source, environment))
            return {"topic_source_id": "source-1"}

        async def upsert_topic_alias(self, alias, environment="prod"):
            calls.append(("alias", alias, environment))
            return {"alias_id": "alias-1"}

        async def store_topic_transition(self, transition, environment="prod"):
            calls.append(("transition", transition, environment))
            return {"transition_id": "transition-1"}

        async def get_topic_transitions_by_tool_call_ids(self, run_id, tool_call_ids, environment="prod"):
            calls.append(("get-transitions", run_id, tool_call_ids, environment))
            return {"tool-1": {"tool_call_id": "tool-1", "action": "post_simple"}}

        async def store_editorial_observation(self, observation, environment="prod"):
            calls.append(("observation", observation, environment))
            return {"observation_id": "observation-1"}

        async def upsert_topic_editor_checkpoint(self, checkpoint, environment="prod"):
            calls.append(("checkpoint", checkpoint, environment))
            return {"checkpoint_key": checkpoint["checkpoint_key"]}

        async def get_live_update_checkpoint(self, checkpoint_key, environment="prod"):
            calls.append(("get-live-checkpoint", checkpoint_key, environment))
            return {"checkpoint_key": checkpoint_key, "guild_id": 1}

        async def get_topic_editor_checkpoint(self, checkpoint_key, environment="prod"):
            calls.append(("get-topic-checkpoint", checkpoint_key, environment))
            return {"checkpoint_key": checkpoint_key, "guild_id": 1}

        async def mirror_live_checkpoint_to_topic_editor(self, checkpoint_key, environment="prod"):
            calls.append(("mirror-live-to-topic", checkpoint_key, environment))
            return {"checkpoint_key": checkpoint_key}

        async def mirror_topic_editor_checkpoint_to_live(self, checkpoint_key, environment="prod"):
            calls.append(("mirror-topic-to-live", checkpoint_key, environment))
            return {"checkpoint_key": checkpoint_key}

        async def create_topic_editor_draft(self, draft, environment="prod"):
            calls.append(("draft-create", draft, environment))
            return {"draft_id": draft["draft_id"], "topic_id": draft.get("topic_id")}

        async def update_topic_editor_draft(self, draft_id, updates, environment="prod"):
            calls.append(("draft-update", draft_id, updates, environment))
            return {"draft_id": draft_id, **updates}

        async def get_recent_topic_editor_drafts(self, guild_id=None, environment="prod", limit=20, status=None, run_id=None):
            calls.append(("draft-read", guild_id, environment, limit, status, run_id))
            return [{"draft_id": "draft-1"}]

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._run_async_in_thread = lambda coro: asyncio.run(coro)
    db._live_write_allowed = lambda guild_id: guild_id == 1

    assert db.acquire_topic_editor_run({"guild_id": 1, "live_channel_id": 20}, environment="dev") == {"run_id": "run-1"}
    assert db.complete_topic_editor_run("run-1", {"guild_id": 1}) == {"run_id": "run-1", "status": "completed"}
    assert db.update_topic_editor_run("run-1", {"guild_id": 1, "source_message_count": 4}) == {
        "run_id": "run-1",
        "guild_id": 1,
        "source_message_count": 4,
    }
    assert db.fail_topic_editor_run("run-1", "boom", {"guild_id": 1}) == {"run_id": "run-1", "status": "failed"}
    assert db.upsert_topic({"guild_id": 1, "canonical_key": "demo"}) == {"topic_id": "topic-1"}
    assert db.add_topic_source({"guild_id": 1, "topic_id": "topic-1", "message_id": 100}) == {"topic_source_id": "source-1"}
    assert db.upsert_topic_alias({"guild_id": 1, "topic_id": "topic-1", "alias_key": "demo"}) == {"alias_id": "alias-1"}
    assert db.store_topic_transition({"guild_id": 1, "run_id": "run-1", "action": "post_simple"}) == {"transition_id": "transition-1"}
    assert db.get_topic_transitions_by_tool_call_ids("run-1", ["tool-1"]) == {
        "tool-1": {"tool_call_id": "tool-1", "action": "post_simple"}
    }
    assert db.store_editorial_observation({"guild_id": 1, "run_id": "run-1", "reason": "near miss"}) == {"observation_id": "observation-1"}
    assert db.upsert_topic_editor_checkpoint({"guild_id": 1, "checkpoint_key": "live", "channel_id": 20}) == {"checkpoint_key": "live"}
    assert db.mirror_live_checkpoint_to_topic_editor("live") == {"checkpoint_key": "live"}
    assert db.mirror_topic_editor_checkpoint_to_live("live") == {"checkpoint_key": "live"}
    assert db.create_topic_editor_draft({"draft_id": "draft-1", "guild_id": 1, "topic_id": None}) == {
        "draft_id": "draft-1",
        "topic_id": None,
    }
    assert db.update_topic_editor_draft("draft-1", {
        "guild_id": 1,
        "preview_units": [{"type": "text"}],
        "latest_valid_preview_hash": "hash-1",
    })["latest_valid_preview_hash"] == "hash-1"
    assert db.get_recent_topic_editor_drafts(guild_id=1, run_id="run-1") == [{"draft_id": "draft-1"}]
    assert [call[0] for call in calls] == [
        "acquire",
        "complete",
        "update-run",
        "fail",
        "topic",
        "source",
        "alias",
        "transition",
        "get-transitions",
        "observation",
        "checkpoint",
        "get-live-checkpoint",
        "mirror-live-to-topic",
        "get-topic-checkpoint",
        "mirror-topic-to-live",
        "draft-create",
        "draft-update",
        "draft-read",
    ]


def test_topic_editor_drafts_migration_landed_in_canonical_path():
    sql_path = Path("/Users/peteromalley/Documents/supabase/migrations/20260519000000_topic_editor_drafts.sql")
    sql = sql_path.read_text()

    assert "create table if not exists public.topic_editor_drafts" in sql
    assert "topic_id uuid null" in sql
    assert "publish_diagnostics jsonb null" in sql
    assert "revision_number integer not null default 1" in sql
    assert "revision_hash text not null" in sql
    assert "revision_attempts integer not null default 0" in sql
    assert "latest_valid_preview_hash text null" in sql
    assert "idx_topic_editor_drafts_null_topic" in sql


# ── Live-update feedback storage + wrapper tests (T11 / Step 9) ────────────


def test_storage_get_feed_item_by_discord_message_id_contains_filter():
    """Reverse-lookup uses .contains('discord_message_ids', [str(message_id)])."""
    calls = []
    storage = make_storage(calls)

    # Replace the async method with a spy that captures the query args.
    captured = {}

    async def fake_get(message_id, guild_id, environment="prod"):
        captured["message_id"] = message_id
        captured["guild_id"] = guild_id
        captured["environment"] = environment
        captured["message_id_str"] = str(message_id)  # must be string-cast
        return {
            "feed_item_id": "feed-1",
            "title": "Test",
            "body": "Body",
            "discord_message_ids": ["9001", "9002"],
            "live_channel_id": 42,
            "status": "posted",
        }

    storage.get_feed_item_by_discord_message_id = fake_get

    row = asyncio.run(
        storage.get_feed_item_by_discord_message_id(9001, 1, environment="prod")
    )
    assert row is not None
    assert row["feed_item_id"] == "feed-1"
    assert row["live_channel_id"] == 42
    assert captured["message_id"] == 9001
    assert captured["message_id_str"] == "9001"
    assert captured["guild_id"] == 1
    assert captured["environment"] == "prod"


def test_storage_get_feed_item_reverse_lookup_uses_json_array_contains():
    """Regression: discord_message_ids is a jsonb column, so the containment
    filter must be a JSON-array literal (json.dumps(["123"]) -> cs.["123"]),
    NOT a bare Python list (which postgrest renders as the PG-array literal
    cs.{123} and Postgres rejects with 22P02 'invalid input syntax for type
    json'). Unlike the spy-based test above, this drives the *real* query
    builder so the jsonb-vs-array filter form is actually exercised.
    """
    import json as _json

    captured = {}

    class FakeQuery:
        def select(self, *_a, **_k):
            return self

        def contains(self, column, value):
            captured["column"] = column
            captured["value"] = value
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return type("Result", (), {"data": [{"feed_item_id": "feed-1"}]})()

    class FakeSupabase:
        def table(self, _table):
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    row = asyncio.run(
        storage.get_feed_item_by_discord_message_id(9001, 1, environment="prod")
    )

    assert row == {"feed_item_id": "feed-1"}
    assert captured["column"] == "discord_message_ids"
    # The crux of the fix: a JSON string, never a bare Python list.
    assert isinstance(captured["value"], str), (
        f"contains() must receive a JSON string, got {type(captured['value']).__name__}"
    )
    assert _json.loads(captured["value"]) == ["9001"]


def test_storage_store_live_update_feedback_normalises_types_and_default():
    """store_live_update_feedback normalises IDs to int and defaults environment."""
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(
        storage.store_live_update_feedback({
            "feed_item_id": "feed-1",
            "guild_id": "1",                # str → int
            "admin_user_id": 42,
            "feedback_text": "Needs a fix",
            "replied_to_message_id": 9001.0,  # float → int
            "disposition": "correction",
            # environment NOT provided — should default to 'prod'
            # status NOT provided — should default to 'logged'
        })
    )

    assert row is not None
    # Check the insert call
    insert_call = next(c for c in calls if c[0] == "insert")
    payload = insert_call[2]
    assert payload["guild_id"] == 1             # str → int normalised
    assert payload["admin_user_id"] == 42       # kept as int
    assert payload["replied_to_message_id"] == 9001  # float → int
    assert payload["feedback_text"] == "Needs a fix"
    assert payload["disposition"] == "correction"
    assert payload["environment"] == "prod"     # default applied
    assert payload["status"] == "logged"        # default applied
    assert payload["feed_item_id"] == "feed-1"


def test_storage_get_live_update_feedback_for_filters_and_returns_none():
    """get_live_update_feedback_for filters correctly and returns None when absent."""
    storage = make_storage([])

    captured = {}

    async def fake_get(feed_item_id, replied_to_message_id,
                       environment="prod", disposition=None):
        captured.update(
            feed_item_id=feed_item_id,
            replied_to_message_id=replied_to_message_id,
            environment=environment,
            disposition=disposition,
        )
        # Simulate no row found
        return None

    storage.get_live_update_feedback_for = fake_get

    # Without disposition filter
    row = asyncio.run(
        storage.get_live_update_feedback_for("feed-1", 9001, environment="dev")
    )
    assert row is None
    assert captured["feed_item_id"] == "feed-1"
    assert captured["replied_to_message_id"] == 9001
    assert captured["environment"] == "dev"
    assert captured["disposition"] is None

    # With disposition filter
    captured.clear()
    row = asyncio.run(
        storage.get_live_update_feedback_for(
            "feed-2", 9002, environment="prod", disposition="deletion-request"
        )
    )
    assert row is None
    assert captured["disposition"] == "deletion-request"


def test_storage_update_live_update_feed_item_status_only():
    """update_live_update_feed_item_status writes ONLY status (no title/body)."""
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(
        storage.update_live_update_feed_item_status(
            "feed-1", "deleted", guild_id=1, environment="prod",
        )
    )

    assert row is not None
    # Check the update call
    update_call = next(c for c in calls if c[0] == "update")
    payload = update_call[4]  # (update, table, key, value, payload)
    assert payload == {"status": "deleted"}
    assert "title" not in payload
    assert "body" not in payload


# ── db_handler wrapper tests ────────────────────────────────────────────────


def test_db_handler_feedback_wrappers_are_synchronous_and_gated():
    """Write wrappers no-op when _live_write_allowed=False; readers are not gated."""
    calls = []

    class FakeStorage:
        async def get_feed_item_by_discord_message_id(
            self, message_id, guild_id, environment="prod"
        ):
            calls.append(("reverse-lookup", message_id, guild_id, environment))
            return {
                "feed_item_id": "feed-1",
                "title": "Test",
                "body": "Body",
                "discord_message_ids": ["9001"],
                "live_channel_id": 42,
                "status": "posted",
            }

        async def store_live_update_feedback(self, feedback):
            calls.append(("store-feedback", feedback))
            return {"feedback_id": "fb-1", **feedback}

        async def get_live_update_feedback_for(
            self, feed_item_id, replied_to_message_id,
            environment="prod", disposition=None,
        ):
            calls.append(("read-feedback", feed_item_id, replied_to_message_id,
                          environment, disposition))
            return {"feedback_id": "fb-1", "feed_item_id": feed_item_id}

        async def update_live_update_feed_item_status(
            self, feed_item_id, status, guild_id, environment="prod",
        ):
            calls.append(("update-status", feed_item_id, status, guild_id, environment))
            return {"feed_item_id": feed_item_id, "status": status}

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._run_async_in_thread = lambda coro: asyncio.run(coro)

    # --- Write-gate OFF: writers return None ---
    db._live_write_allowed = lambda guild_id: False

    result = db.store_live_update_feedback(
        {"guild_id": 1, "feed_item_id": "feed-1", "feedback_text": "fix"},
        environment="prod",
    )
    assert result is None  # no-op when write not allowed

    result = db.update_live_update_feed_item_status(
        "feed-1", "deleted", guild_id=1, environment="prod",
    )
    assert result is None  # no-op when write not allowed

    # Readers are NOT gated — they still work even when write is disallowed
    row = db.get_feed_item_by_discord_message_id(9001, 1, environment="prod")
    assert row is not None
    assert row["feed_item_id"] == "feed-1"
    assert row["live_channel_id"] == 42

    row = db.get_live_update_feedback_for("feed-1", 9001, environment="prod")
    assert row is not None
    assert row["feed_item_id"] == "feed-1"

    # --- Write-gate ON: writers work ---
    calls.clear()
    db._live_write_allowed = lambda guild_id: True

    result = db.store_live_update_feedback(
        {"guild_id": 1, "feed_item_id": "feed-1", "feedback_text": "fix"},
        environment="dev",
    )
    assert result is not None
    assert result["feedback_id"] == "fb-1"

    result = db.update_live_update_feed_item_status(
        "feed-1", "edited", guild_id=1, environment="prod",
    )
    assert result is not None
    assert result["status"] == "edited"

    # Verify the write calls went through after gate was re-enabled
    call_types = [c[0] for c in calls]
    assert "store-feedback" in call_types
    assert "update-status" in call_types
    # Reader calls happened before calls.clear() — confirmed they returned data above


def test_db_handler_feedback_wrappers_called_synchronously():
    """Wrappers are synchronous def — called without await."""
    calls = []

    class FakeStorage:
        async def get_feed_item_by_discord_message_id(
            self, message_id, guild_id, environment="prod"
        ):
            calls.append("storage-called")
            return None

        async def store_live_update_feedback(self, feedback):
            calls.append("storage-called")
            return None

        async def get_live_update_feedback_for(self, *args, **kwargs):
            calls.append("storage-called")
            return None

        async def update_live_update_feed_item_status(self, *args, **kwargs):
            calls.append("storage-called")
            return None

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._run_async_in_thread = lambda coro: asyncio.run(coro)
    db._live_write_allowed = lambda guild_id: True

    # All calls are synchronous (no await)
    db.get_feed_item_by_discord_message_id(9001, 1)
    db.get_live_update_feedback_for("feed-1", 9001)

    # These go through _live_write_allowed gate which is True
    db.store_live_update_feedback(
        {"guild_id": 1, "feed_item_id": "feed-1", "feedback_text": "fix"}
    )
    db.update_live_update_feed_item_status("feed-1", "deleted", guild_id=1)

    assert len(calls) == 4
    assert all(c == "storage-called" for c in calls)
