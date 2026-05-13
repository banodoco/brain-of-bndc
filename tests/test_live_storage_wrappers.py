import asyncio

from src.common.db_handler import DatabaseHandler
from src.common.storage_handler import StorageHandler


def make_storage(calls):
    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = object()

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

    assert run["_table"] == "topic_editor_runs"
    assert topic["_table"] == "topics"
    assert source["_table"] == "topic_sources"
    assert alias["_table"] == "topic_aliases"
    assert transition["_table"] == "topic_transitions"
    assert observation["_table"] == "editorial_observations"
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

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = FakeStorage()
    db._run_async_in_thread = lambda coro: asyncio.run(coro)
    db._live_write_allowed = lambda guild_id: guild_id == 1

    assert db.acquire_topic_editor_run({"guild_id": 1, "live_channel_id": 20}, environment="dev") == {"run_id": "run-1"}
    assert db.complete_topic_editor_run("run-1", {"guild_id": 1}) == {"run_id": "run-1", "status": "completed"}
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
    assert [call[0] for call in calls] == [
        "acquire",
        "complete",
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
    ]
