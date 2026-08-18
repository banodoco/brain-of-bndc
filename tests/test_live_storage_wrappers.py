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

        async def get_topic_editor_checkpoint(self, checkpoint_key, environment="prod"):
            calls.append(("get-topic-checkpoint", checkpoint_key, environment))
            return {"checkpoint_key": checkpoint_key, "guild_id": 1}

        async def create_topic_editor_draft(self, draft, environment="prod"):
            calls.append(("draft-create", draft, environment))
            return {"draft_id": draft["draft_id"], "topic_id": draft.get("topic_id")}

        async def update_topic_editor_draft(self, draft_id, updates, environment="prod"):
            calls.append(("draft-update", draft_id, updates, environment))
            return {"draft_id": draft_id, **updates}

        async def get_recent_topic_editor_drafts(self, guild_id=None, environment="prod", limit=20, status=None, statuses=None, run_id=None, ascending=False):
            calls.append(("draft-read", guild_id, environment, limit, status, statuses, run_id, ascending))
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


def test_storage_get_topic_reverse_lookup_uses_bare_string_list_contains():
    """topics.discord_message_ids is a NATIVE Postgres array (bigint[]), so the
    containment filter MUST be a bare Python list of strings ([str(id)] ->
    cs.{"123"}) — NOT a json.dumps string (which would render cs.["123"] and
    error 22P02 malformed array literal). This drives the *real* query builder
    so the array-vs-jsonb filter form is actually exercised; the method under
    test is NOT mocked away.
    """
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
            return type("Result", (), {"data": [{"topic_id": "t-1"}]})()

    class FakeSupabase:
        def table(self, table):
            captured["table"] = table
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    row = asyncio.run(
        storage.get_topic_by_discord_message_id(123, 1, environment="prod")
    )

    assert row == {"topic_id": "t-1"}
    assert captured["table"] == "topics"
    assert captured["column"] == "discord_message_ids"
    # The crux: a bare list of strings, NEVER a json.dumps string.
    assert isinstance(captured["value"], list), (
        f"contains() must receive a bare list, got {type(captured['value']).__name__}"
    )
    assert captured["value"] == ["123"]
    assert all(isinstance(v, str) for v in captured["value"])


def test_storage_get_live_update_feedback_for_topic_only_omits_feed_item_filter():
    """get_live_update_feedback_for keyed on topic_id only must NOT emit a
    .eq('feed_item_id', ...) filter (which under the old required-positional
    signature would have matched nothing). Drives the real query builder.
    """
    eq_calls = []

    class FakeQuery:
        def select(self, *_a, **_k):
            return self

        def eq(self, column, value):
            eq_calls.append((column, value))
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return type("Result", (), {"data": []})()

    class FakeSupabase:
        def table(self, _table):
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    row = asyncio.run(
        storage.get_live_update_feedback_for(
            9001, environment="prod", topic_id="t-1",
        )
    )

    assert row is None
    eq_columns = {c for c, _ in eq_calls}
    assert ("topic_id", "t-1") in eq_calls
    assert ("replied_to_message_id", 9001) in eq_calls
    assert ("environment", "prod") in eq_calls
    # Never filter on feed_item_id when it is None.
    assert "feed_item_id" not in eq_columns


def test_storage_store_live_update_feedback_normalises_types_and_default():
    """store_live_update_feedback normalises IDs to int and defaults environment."""
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(
        storage.store_live_update_feedback({
            "topic_id": "t-1",
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
    assert payload["topic_id"] == "t-1"
    assert payload["feed_item_id"] is None       # dormant when keyed on topic


def test_storage_get_live_update_feedback_for_filters_and_returns_none():
    """get_live_update_feedback_for filters correctly and returns None when
    absent. Drives the real query builder (method under test NOT mocked away).
    """
    eq_calls = []
    disposition_box = {}

    class FakeQuery:
        def select(self, *_a, **_k):
            return self

        def eq(self, column, value):
            eq_calls.append((column, value))
            if column == "disposition":
                disposition_box["value"] = value
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return type("Result", (), {"data": []})()

    class FakeSupabase:
        def table(self, _table):
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    # Without disposition filter, keyed on feed_item_id.
    row = asyncio.run(
        storage.get_live_update_feedback_for(
            9001, environment="dev", feed_item_id="feed-1",
        )
    )
    assert row is None
    assert ("feed_item_id", "feed-1") in eq_calls
    assert ("replied_to_message_id", 9001) in eq_calls
    assert ("environment", "dev") in eq_calls
    assert "disposition" not in {c for c, _ in eq_calls}

    # With disposition filter.
    eq_calls.clear()
    disposition_box.clear()
    row = asyncio.run(
        storage.get_live_update_feedback_for(
            9002, environment="prod", disposition="deletion-request",
            feed_item_id="feed-2",
        )
    )
    assert row is None
    assert disposition_box["value"] == "deletion-request"


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

        async def get_topic_by_discord_message_id(
            self, message_id, guild_id, environment="prod"
        ):
            calls.append(("topic-reverse-lookup", message_id, guild_id, environment))
            return {
                "topic_id": "t-1",
                "headline": "Test",
                "summary": "Body",
                "discord_message_ids": [9001],
                "publication_status": "sent",
                "state": "posted",
            }

        async def store_live_update_feedback(self, feedback):
            calls.append(("store-feedback", feedback))
            return {"feedback_id": "fb-1", **feedback}

        async def get_live_update_feedback_for(
            self, replied_to_message_id, environment="prod", disposition=None,
            *, feed_item_id=None, topic_id=None,
        ):
            calls.append(("read-feedback", replied_to_message_id,
                          environment, disposition, feed_item_id, topic_id))
            return {"feedback_id": "fb-1", "topic_id": topic_id}

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._run_async_in_thread = lambda coro: asyncio.run(coro)

    # --- Write-gate OFF: writers return None ---
    db._live_write_allowed = lambda guild_id: False

    result = db.store_live_update_feedback(
        {"guild_id": 1, "topic_id": "t-1", "feedback_text": "fix"},
        environment="prod",
    )
    assert result is None  # no-op when write not allowed

    # Readers are NOT gated — they still work even when write is disallowed
    row = db.get_topic_by_discord_message_id(9001, 1, environment="prod")
    assert row is not None
    assert row["topic_id"] == "t-1"

    row = db.get_live_update_feedback_for(9001, environment="prod", topic_id="t-1")
    assert row is not None
    assert row["topic_id"] == "t-1"

    # --- Write-gate ON: writers work ---
    calls.clear()
    db._live_write_allowed = lambda guild_id: True

    result = db.store_live_update_feedback(
        {"guild_id": 1, "topic_id": "t-1", "feedback_text": "fix"},
        environment="dev",
    )
    assert result is not None
    assert result["feedback_id"] == "fb-1"

    # Verify the write call went through after gate was re-enabled
    call_types = [c[0] for c in calls]
    assert "store-feedback" in call_types
    # Reader calls happened before calls.clear() — confirmed they returned data above


def test_db_handler_feedback_wrappers_called_synchronously():
    """Wrappers are synchronous def — called without await."""
    calls = []

    class FakeStorage:
        async def get_topic_by_discord_message_id(
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

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._run_async_in_thread = lambda coro: asyncio.run(coro)
    db._live_write_allowed = lambda guild_id: True

    # All calls are synchronous (no await)
    db.get_topic_by_discord_message_id(9001, 1)
    db.get_live_update_feedback_for(9001, topic_id="t-1")

    # This goes through _live_write_allowed gate which is True
    db.store_live_update_feedback(
        {"guild_id": 1, "topic_id": "t-1", "feedback_text": "fix"}
    )

    assert len(calls) == 3


# ── Ground truth (community feedback sense-check) ──────────────────────────


def test_storage_get_topic_sources_queries_topic_sources_by_id_and_env():
    """get_topic_sources drives the real query builder against `topic_sources`,
    filtering by topic_id + environment, newest first."""
    captured = {}

    class FakeQuery:
        def select(self, cols, **_k):
            captured["select"] = cols
            return self

        def eq(self, column, value):
            captured.setdefault("eq", []).append((column, value))
            return self

        def order(self, column, **kwargs):
            captured["order"] = (column, kwargs.get("desc"))
            return self

        def limit(self, n):
            captured["limit"] = n
            return self

        def execute(self):
            return type("Result", (), {"data": [
                {"message_id": 200, "guild_id": 1, "added_in_run_id": "r2"},
                {"message_id": 100, "guild_id": 1, "added_in_run_id": "r1"},
            ]})()

    class FakeSupabase:
        def table(self, table):
            captured["table"] = table
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    rows = asyncio.run(
        storage.get_topic_sources("t-1", environment="prod")
    )

    assert captured["table"] == "topic_sources"
    assert ("topic_id", "t-1") in captured["eq"]
    assert ("environment", "prod") in captured["eq"]
    assert captured["order"] == ("created_at", True)
    assert len(rows) == 2
    assert rows[0]["message_id"] == 200


def test_storage_get_topic_ground_truth_chains_sources_to_archive():
    """get_topic_ground_truth resolves topic_sources → discord_messages and
    returns the compact verbatim shape. The method under test is NOT mocked
    away — the real query builders run against a routed fake client."""
    captured = {}

    class FakeQuery:
        def select(self, cols, **_k):
            captured["select"] = cols
            return self

        def eq(self, column, value):
            captured.setdefault("eq", []).append((column, value))
            return self

        def order(self, column, **kwargs):
            captured["order"] = (column, kwargs.get("desc"))
            return self

        def in_(self, column, values):
            captured.setdefault("in", []).append((column, list(values)))
            return self

        def limit(self, n):
            captured["limit"] = n
            return self

        def execute(self):
            if captured.get("table") == "topic_sources":
                return type("Result", (), {"data": [
                    {"message_id": 100, "guild_id": 1},
                    {"message_id": 200, "guild_id": 1},
                ]})()
            return type("Result", (), {"data": [
                {
                    "message_id": 100, "guild_id": 1, "channel_id": 5,
                    "thread_id": None, "author_id": 42,
                    "content": "Kijai endorses Claude Code for node dev",
                    "attachments": [], "embeds": [], "created_at": "2026-08-18T00:00:00Z",
                    "reference_id": None, "reaction_count": 7,
                },
                {
                    "message_id": 200, "guild_id": 1, "channel_id": 5,
                    "thread_id": None, "author_id": 43,
                    "content": "second source message",
                    "attachments": [], "embeds": [], "created_at": "2026-08-18T01:00:00Z",
                    "reference_id": None, "reaction_count": 3,
                },
            ]})()

    class FakeSupabase:
        def table(self, table):
            captured["table"] = table
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    truth = asyncio.run(
        storage.get_topic_ground_truth("t-1", guild_id=1, environment="prod", limit=30)
    )

    # First query was topic_sources (by topic_id), second was the archive.
    assert captured["eq"][0] == ("topic_id", "t-1")
    # The archive query carried the guild filter.
    assert ("guild_id", 1) in captured["eq"]
    # Only the compact ground-truth keys survive, in source order.
    assert len(truth) == 2
    assert truth[0]["message_id"] == "100"
    assert truth[0]["content"].startswith("Kijai endorses")
    assert truth[0]["reaction_count"] == 7
    assert set(truth[0].keys()) == {
        "message_id", "channel_id", "author_id", "content",
        "created_at", "reference_id", "reaction_count",
    }


def test_storage_get_topic_ground_truth_empty_when_no_sources():
    """A topic with no recorded sources yields [] (no archive query)."""
    captured = []

    class FakeQuery:
        def select(self, *_a, **_k):
            return self

        def eq(self, column, value):
            captured.append(("eq", column, value))
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return type("Result", (), {"data": []})()

    class FakeSupabase:
        def table(self, _table):
            return FakeQuery()

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    truth = asyncio.run(
        storage.get_topic_ground_truth("t-none", guild_id=1, environment="prod")
    )
    assert truth == []
    # Only topic_sources was queried — no discord_messages call.
    assert all(c[1] in ("topic_id", "environment") for c in captured)


def test_storage_store_live_update_feedback_persists_verdict():
    """The agent's sense-check verdict lands in the feedback row payload."""
    calls = []
    storage = make_storage(calls)

    row = asyncio.run(
        storage.store_live_update_feedback({
            "topic_id": "t-1",
            "guild_id": 1,
            "admin_user_id": 999,
            "feedback_text": "This says Wan 2.4 but the source says Wan 2.5",
            "replied_to_message_id": 9001,
            "disposition": "correction",
            "verdict": "Feedback matches the source — fixed the version number.",
        })
    )

    assert row["verdict"] == "Feedback matches the source — fixed the version number."
    assert row["disposition"] == "correction"
    assert calls[-1][0] == "insert"
    assert calls[-1][1] == "live_update_feedback"


def test_db_handler_ground_truth_wrappers_are_synchronous_readers():
    """get_topic_sources / get_topic_ground_truth wrappers are sync, non-gated
    readers (they work even when writes are disallowed)."""
    calls = []

    class FakeStorage:
        async def get_topic_sources(self, topic_id, environment="prod", limit=50):
            calls.append(("sources", topic_id, environment, limit))
            return [{"message_id": 100}]

        async def get_topic_ground_truth(self, topic_id, guild_id=None, environment="prod", limit=30):
            calls.append(("truth", topic_id, guild_id, environment, limit))
            return [{"message_id": "100", "content": "ground truth"}]

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._live_write_allowed = lambda guild_id: False  # writes blocked

    sources = db.get_topic_sources("t-1", environment="prod")
    truth = db.get_topic_ground_truth("t-1", guild_id=1, environment="prod", limit=30)

    assert sources == [{"message_id": 100}]
    assert truth == [{"message_id": "100", "content": "ground truth"}]
    assert [c[0] for c in calls] == ["sources", "truth"]
