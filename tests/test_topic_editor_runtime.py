import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.common.storage_handler import StorageHandler
import src.features.summarising.topic_editor as topic_editor_module
from src.features.summarising.topic_editor import (
    TopicEditor,
    TOPIC_EDITOR_TOOLS,
    build_rejected_transition,
    render_topic,
)


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClaude:
    def __init__(self, response):
        self.client = SimpleNamespace(messages=FakeMessages(response))


class FakeDB:
    def __init__(self):
        self.completed = []
        self.failed = []
        self.transitions = []
        self.topics = []
        self.sources = []
        self.aliases = []
        self.checkpoints = []
        self.topic_updates = []
        self.active_topics = []
        self.topic_alias_rows = []

    def get_topic_editor_checkpoint(self, checkpoint_key, environment="prod"):
        return {
            "checkpoint_key": checkpoint_key,
            "guild_id": 1,
            "channel_id": 2,
            "last_message_id": 99,
        }

    def mirror_live_checkpoint_to_topic_editor(self, checkpoint_key, environment="prod"):
        raise AssertionError("topic checkpoint exists; legacy mirror should not be called")

    def acquire_topic_editor_run(self, run, environment="prod"):
        self.acquired = (run, environment)
        return {"run_id": "run-1"}

    def get_archived_messages_after_checkpoint(self, checkpoint=None, guild_id=None, channel_ids=None, limit=200, exclude_author_ids=None):
        return [
            {
                "message_id": 100,
                "guild_id": 1,
                "channel_id": 10,
                "author_id": 42,
                "content": "I shipped a new LoRA test.",
                "created_at": "2026-05-13T10:00:00Z",
                "author_context_snapshot": {"username": "alice"},
            }
        ]

    def get_topics(self, guild_id=None, states=None, limit=100, environment="prod"):
        return self.active_topics

    def get_topic_aliases(self, guild_id=None, environment="prod"):
        return self.topic_alias_rows

    def upsert_topic(self, topic, environment="prod"):
        self.topics.append((topic, environment))
        return {"topic_id": "topic-1", **topic}

    def add_topic_source(self, source, environment="prod"):
        self.sources.append((source, environment))
        return {"topic_source_id": "source-1"}

    def upsert_topic_alias(self, alias, environment="prod"):
        self.aliases.append((alias, environment))
        return {"alias_id": "alias-1"}

    def store_topic_transition(self, transition, environment="prod"):
        self.transitions.append((transition, environment))
        return {"transition_id": "transition-1"}

    def store_editorial_observation(self, observation, environment="prod"):
        raise AssertionError("not used in this test")

    def update_topic(self, topic_id, updates, guild_id=None, environment="prod"):
        self.topic_updates.append((topic_id, updates, guild_id, environment))
        return {"topic_id": topic_id, **updates}

    def upsert_topic_editor_checkpoint(self, checkpoint, environment="prod"):
        self.checkpoints.append((checkpoint, environment))
        return checkpoint

    def complete_topic_editor_run(self, run_id, updates=None, guild_id=None, environment="prod"):
        self.completed.append((run_id, updates, guild_id, environment))
        return {"run_id": run_id, "status": "completed"}

    def fail_topic_editor_run(self, run_id, error_message, updates=None, guild_id=None, environment="prod"):
        self.failed.append((run_id, error_message, updates, guild_id, environment))
        return {"run_id": run_id, "status": "failed"}


def test_topic_editor_run_once_uses_native_tools_and_topic_run_lifecycle(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_INPUT_COST_PER_MTOKENS", "3")
    monkeypatch.setenv("TOPIC_EDITOR_OUTPUT_COST_PER_MTOKENS", "15")
    monkeypatch.setenv("LIVE_UPDATE_TRACE_CHANNEL_ID", "999")

    class TraceChannel:
        def __init__(self):
            self.sent = []

        async def send(self, content):
            self.sent.append(content)

    trace_channel = TraceChannel()
    bot = SimpleNamespace(get_channel=lambda channel_id: trace_channel if channel_id == 999 else None)
    block = SimpleNamespace(
        type="tool_use",
        id="tool-1",
        name="post_simple_topic",
        input={
            "proposed_key": "Alice LoRA Test",
            "headline": "Alice ships a new LoRA test",
            "body": "Alice shared a new LoRA test with early outputs.",
            "source_message_ids": ["100"],
        },
    )
    response = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=123, output_tokens=45))
    db = FakeDB()
    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(response),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
        model="claude-test",
    )

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    assert db.acquired[0]["publishing_enabled"] is False
    assert db.completed[0][1]["source_message_count"] == 1
    assert db.completed[0][1]["tool_call_count"] == 1
    assert db.completed[0][1]["input_tokens"] == 123
    assert db.completed[0][1]["cost_usd"] == 0.001044
    assert db.checkpoints[0][0]["last_message_id"] == 100
    assert db.topics[0][0]["canonical_key"] == "alice-lora-test"
    assert db.sources[0][0]["message_id"] == "100"
    assert db.transitions[0][0]["run_id"] == "run-1"
    assert db.transitions[0][0]["tool_call_id"] == "tool-1"
    assert db.transitions[0][0]["action"] == "post_simple"
    assert db.topic_updates[0][1]["publication_status"] == "suppressed"
    assert result["publish_results"][0]["status"] == "suppressed"
    assert result["trace_messages"]
    assert "sources=1 tools=1" in result["trace_messages"][0]
    assert "publishing=OFF" in result["trace_messages"][0]
    assert "cost_usd=0.001044" in result["trace_messages"][0]
    assert "would-publish:" in result["trace_messages"][0]
    assert trace_channel.sent == result["trace_messages"]
    assert db.failed == []

    llm_call = editor.llm_client.client.messages.calls[0]
    assert llm_call["tools"] == TOPIC_EDITOR_TOOLS
    assert len(llm_call["tools"]) == 10


def test_topic_editor_run_once_skips_when_active_lease_is_not_acquired():
    db = FakeDB()
    db.acquire_topic_editor_run = lambda run, environment="prod": None
    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )

    result = asyncio.run(editor.run_once("scheduled"))

    assert result == {
        "status": "skipped",
        "reason": "lease_not_acquired",
        "checkpoint_key": "live_update_editor:1:2",
    }
    assert db.completed == []
    assert db.failed == []
    assert db.transitions == []
    assert db.checkpoints == []


def test_acquire_topic_editor_run_expires_stale_running_lease(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_LEASE_TIMEOUT_MINUTES", "30")

    stale_started_at = (datetime.utcnow() - timedelta(minutes=45)).isoformat()
    rows = [{
        "run_id": "stale-run",
        "environment": "prod",
        "guild_id": 1,
        "live_channel_id": 2,
        "status": "running",
        "started_at": stale_started_at,
        "metadata": {"owner": "crashed-runner"},
    }]

    class Result:
        def __init__(self, data):
            self.data = data

    class Query:
        def __init__(self, table_name, operation=None, payload=None):
            self.table_name = table_name
            self.operation = operation
            self.payload = payload
            self.filters = []
            self.limit_count = None

        def select(self, *_args, **_kwargs):
            self.operation = "select"
            return self

        def insert(self, payload):
            self.operation = "insert"
            self.payload = payload
            return self

        def update(self, payload):
            self.operation = "update"
            self.payload = payload
            return self

        def eq(self, column, value):
            self.filters.append(("eq", column, value))
            return self

        def lt(self, column, value):
            self.filters.append(("lt", column, value))
            return self

        def limit(self, count):
            self.limit_count = count
            return self

        def execute(self):
            if self.operation == "insert":
                inserted = dict(self.payload)
                inserted.setdefault("started_at", datetime.utcnow().isoformat())
                rows.append(inserted)
                return Result([inserted])

            matched = [row for row in rows if self._matches(row)]
            if self.limit_count is not None:
                matched = matched[:self.limit_count]

            if self.operation == "update":
                for row in matched:
                    row.update(self.payload)
                return Result([dict(row) for row in matched])

            return Result([dict(row) for row in matched])

        def _matches(self, row):
            for op, column, value in self.filters:
                if op == "eq" and row.get(column) != value:
                    return False
                if op == "lt" and not (str(row.get(column)) < value):
                    return False
            return True

    class FakeSupabase:
        def table(self, table_name):
            assert table_name == "topic_editor_runs"
            return Query(table_name)

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()

    acquired = asyncio.run(storage.acquire_topic_editor_run({
        "guild_id": 1,
        "live_channel_id": 2,
        "trigger": "scheduled",
    }, environment="prod"))

    assert acquired is not None
    assert acquired["status"] == "running"
    assert acquired["run_id"] != "stale-run"

    stale_row = rows[0]
    assert stale_row["status"] == "expired"
    assert stale_row["metadata"]["owner"] == "crashed-runner"
    assert stale_row["metadata"]["expired_by"] == acquired["run_id"]
    assert stale_row["metadata"]["expired_at"]
    assert rows[1]["run_id"] == acquired["run_id"]


def test_topic_editor_dispatch_rejects_simple_collisions_and_replays_without_side_effects():
    blocks = [
        SimpleNamespace(
            type="tool_use",
            id="tool-simple",
            name="post_simple_topic",
            input={
                "proposed_key": "Multi Author Cluster",
                "headline": "Multi author cluster",
                "body": "Two creators are involved.",
                "source_message_ids": ["100", "101", "101"],
            },
        ),
        SimpleNamespace(
            type="tool_use",
            id="tool-collision",
            name="watch_topic",
            input={
                "proposed_key": "Alice LoRA Test v2",
                "headline": "Alice ships a new LoRA test update",
                "why_interesting": "Still developing.",
                "revisit_when": "tomorrow",
                "source_message_ids": ["100"],
            },
        ),
        SimpleNamespace(
            type="tool_use",
            id="tool-collision",
            name="watch_topic",
            input={
                "proposed_key": "Alice LoRA Test v2",
                "headline": "Alice ships a new LoRA test update",
                "why_interesting": "Replay should do nothing.",
                "revisit_when": "tomorrow",
                "source_message_ids": ["100"],
            },
        ),
    ]
    response = SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=1, output_tokens=2))
    db = FakeDB()
    db.active_topics = [{
        "topic_id": "topic-existing",
        "canonical_key": "alice-lora-test",
        "headline": "Alice ships a new LoRA test",
        "state": "watching",
        "source_authors": ["alice"],
    }]

    def messages_after_checkpoint(*args, **kwargs):
        return [
            {
                "message_id": 100,
                "guild_id": 1,
                "channel_id": 10,
                "author_id": 42,
                "content": "Alice update",
                "created_at": "2026-05-13T10:00:00Z",
                "author_context_snapshot": {"username": "alice"},
            },
            {
                "message_id": 101,
                "guild_id": 1,
                "channel_id": 10,
                "author_id": 43,
                "content": "Bob update",
                "created_at": "2026-05-13T10:01:00Z",
                "author_context_snapshot": {"username": "bob"},
            },
        ]

    db.get_archived_messages_after_checkpoint = messages_after_checkpoint
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert [transition[0]["action"] for transition in db.transitions] == ["rejected_post_simple", "rejected_watch"]
    assert db.transitions[0][0]["payload"]["outcome"] == "tool_error"
    assert db.transitions[0][0]["payload"]["source_message_ids"] == ["100", "101"]
    assert db.transitions[1][0]["payload"]["collisions"][0]["topic_id"] == "topic-existing"
    assert db.topics == []
    assert db.sources == []
    assert result["outcomes"][2]["outcome"] == "idempotent_replay"


def test_topic_editor_dispatch_allows_collision_override_and_dedupes_sources():
    block = SimpleNamespace(
        type="tool_use",
        id="tool-override",
        name="watch_topic",
        input={
            "proposed_key": "Alice LoRA Test v2",
            "headline": "Alice ships a new LoRA test update",
            "why_interesting": "Different enough to track separately.",
            "revisit_when": "tomorrow",
            "source_message_ids": ["100", "100"],
            "override_collisions": [{"topic_id": "topic-existing", "reason": "new artifact"}],
        },
    )
    response = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1, output_tokens=2))
    db = FakeDB()
    db.active_topics = [{
        "topic_id": "topic-existing",
        "canonical_key": "alice-lora-test",
        "headline": "Alice ships a new LoRA test",
        "state": "watching",
        "source_authors": ["alice"],
    }]
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert result["outcomes"][0]["outcome"] == "accepted"
    assert [source[0]["message_id"] for source in db.sources] == ["100"]
    assert [transition[0]["action"] for transition in db.transitions] == ["watch", "override"]
    assert db.transitions[1][0]["payload"]["overridden_topic_id"] == "topic-existing"


def test_topic_editor_dispatch_allows_same_tool_id_collision_override_retry():
    db = FakeDB()
    db.active_topics = [{
        "topic_id": "topic-existing",
        "canonical_key": "alice-lora-test",
        "headline": "Alice ships a new LoRA test",
        "state": "watching",
        "source_authors": ["alice"],
    }]
    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    context = {
        "run_id": "run-1",
        "guild_id": 1,
        "live_channel_id": 2,
        "messages": [{
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "Alice update",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
        }],
        "active_topics": db.active_topics,
        "aliases": [],
        "seen_tool_call_ids": set(),
        "observation_count": 0,
        "created_topics": [],
        "finalize": None,
    }
    first_call = {
        "id": "tool-collision",
        "name": "watch_topic",
        "input": {
            "proposed_key": "Alice LoRA Test v2",
            "headline": "Alice ships a new LoRA test update",
            "why_interesting": "Still developing.",
            "revisit_when": "tomorrow",
            "source_message_ids": ["100"],
        },
    }
    retry_call = {
        "id": "tool-collision",
        "name": "watch_topic",
        "input": {
            **first_call["input"],
            "why_interesting": "Different enough to track separately.",
            "override_collisions": [{"topic_id": "topic-existing", "reason": "new artifact"}],
        },
    }

    rejected = editor._dispatch_tool_call(first_call, context)
    accepted = editor._dispatch_tool_call(retry_call, context)

    assert rejected["outcome"] == "rejected_watch"
    assert accepted["outcome"] == "accepted"
    assert [transition[0]["action"] for transition in db.transitions] == ["rejected_watch", "watch", "override"]
    assert {transition[0]["tool_call_id"] for transition in db.transitions} == {"tool-collision"}
    assert db.transitions[0][0]["payload"]["collisions"][0]["topic_id"] == "topic-existing"
    assert db.transitions[2][0]["payload"]["overridden_topic_id"] == "topic-existing"


def test_topic_editor_dispatch_logs_invalid_update_and_discard_under_base_actions():
    blocks = [
        SimpleNamespace(
            type="tool_use",
            id="tool-update",
            name="update_topic_source_messages",
            input={"topic_id": "missing", "new_source_message_ids": ["100"], "note": "more context"},
        ),
        SimpleNamespace(
            type="tool_use",
            id="tool-discard",
            name="discard_topic",
            input={"topic_id": "posted-topic", "reason": "stale"},
        ),
    ]
    response = SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=1, output_tokens=2))
    db = FakeDB()
    db.active_topics = [{"topic_id": "posted-topic", "state": "posted", "canonical_key": "posted", "headline": "Posted"}]
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert [transition[0]["action"] for transition in db.transitions] == ["update_sources", "discard"]
    assert [transition[0]["payload"]["outcome"] for transition in db.transitions] == ["tool_error", "tool_error"]
    assert {transition[0]["action"] for transition in db.transitions}.isdisjoint({"rejected_update_sources", "rejected_discard"})
    assert db.sources == []
    assert result["outcomes"][0]["error"] == "topic_not_found"
    assert result["outcomes"][1]["error"] == "topic_not_watching"


def test_topic_editor_dispatch_caps_record_observation_storage():
    blocks = [
        SimpleNamespace(
            type="tool_use",
            id=f"tool-observation-{idx}",
            name="record_observation",
            input={"source_message_ids": ["100"], "observation_kind": "considered", "reason": f"near miss {idx}"},
        )
        for idx in range(4)
    ]
    response = SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=1, output_tokens=2))
    db = FakeDB()
    observations = []

    def store_observation(observation, environment="prod"):
        observations.append((observation, environment))
        return {"observation_id": f"obs-{len(observations)}"}

    db.store_editorial_observation = store_observation
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert len(observations) == 3
    assert [transition[0]["action"] for transition in db.transitions] == ["observation", "observation", "observation", "observation"]
    assert db.transitions[-1][0]["payload"]["outcome"] == "tool_error"
    assert db.transitions[-1][0]["payload"]["error"] == "observation_cap_reached"
    assert result["outcomes"][-1]["outcome"] == "tool_error"


def test_topic_editor_audit_action_vocabulary_excludes_invalid_rejected_actions():
    allowed_actions = {
        "post_simple",
        "post_sectioned",
        "watch",
        "update_sources",
        "discard",
        "observation",
        "override",
        "rejected_post_simple",
        "rejected_post_sectioned",
        "rejected_watch",
    }
    configured_actions = set()
    for tool in TOPIC_EDITOR_TOOLS:
        name = tool["name"]
        if name == "post_simple_topic":
            configured_actions.add("post_simple")
        elif name == "post_sectioned_topic":
            configured_actions.add("post_sectioned")
        elif name == "watch_topic":
            configured_actions.add("watch")
        elif name == "update_topic_source_messages":
            configured_actions.add("update_sources")
        elif name == "discard_topic":
            configured_actions.add("discard")
        elif name == "record_observation":
            configured_actions.add("observation")

    assert {"rejected_update_sources", "rejected_discard"}.isdisjoint(allowed_actions)
    assert configured_actions == {
        "post_simple",
        "post_sectioned",
        "watch",
        "update_sources",
        "discard",
        "observation",
    }
    assert build_rejected_transition(
        run_id="run-1",
        environment="prod",
        guild_id=1,
        action="rejected_watch",
        tool_call_id="tool-1",
        reason="collision",
        payload={"outcome": "tool_error"},
    )["action"] == "rejected_watch"
    with pytest.raises(ValueError):
        build_rejected_transition(
            run_id="run-1",
            environment="prod",
            guild_id=1,
            action="rejected_update_sources",
            tool_call_id="tool-2",
            reason="invalid",
            payload={"outcome": "tool_error"},
        )


def test_render_topic_is_pure_and_handles_simple_sectioned_and_story_update():
    simple = {
        "headline": "Alice ships a LoRA",
        "summary": {"body": "A concise update.", "media": ["https://example.test/image.png"]},
        "source_message_ids": ["100"],
    }
    sectioned = {
        "headline": "Three creators publish a workflow",
        "summary": {
            "body": "The release has several parts.",
            "sections": [
                {"title": "Model", "body": "New LoRA weights."},
                {"heading": "Workflow", "text": "Comfy graph included."},
            ],
        },
        "source_message_ids": ["101", "102"],
    }
    story_update = {
        "headline": "Alice follows up with results",
        "parent_topic_id": "topic-parent",
        "summary": {"body": "The first benchmark landed."},
    }

    assert render_topic(simple) == [
        "**Live update: Alice ships a LoRA**\n\nA concise update.\nhttps://example.test/image.png\n\nSource: 100"
    ]
    assert "**Model**" in render_topic(sectioned)[0]
    assert "Sources: 101, 102" in render_topic(sectioned)[0]
    assert render_topic(story_update)[0].startswith("**Update: Alice follows up with results**")


def test_topic_editor_publisher_sends_only_when_enabled_and_records_status(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "true")

    class SentMessage:
        def __init__(self, message_id):
            self.id = message_id

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content):
            self.sent.append(content)
            return SentMessage(9101)

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 2 else None)
    response = SimpleNamespace(
        content=[SimpleNamespace(
            type="tool_use",
            id="tool-publish",
            name="post_simple_topic",
            input={
                "proposed_key": "Alice Publish Test",
                "headline": "Alice publish test",
                "body": "This should publish.",
                "source_message_ids": ["100"],
            },
        )],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    db = FakeDB()
    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(response),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )

    result = asyncio.run(editor.run_once("manual"))

    assert channel.sent == ["**Live update: Alice publish test**\n\nThis should publish.\n\nSource: 100"]
    assert result["publish_results"][0]["status"] == "sent"
    assert db.topic_updates[0][1]["publication_status"] == "sent"
    assert db.topic_updates[0][1]["discord_message_ids"] == [9101]
    assert db.topic_updates[0][1]["publication_attempts"] == 1
    assert db.topic_updates[0][1]["publication_error"] is None
    assert db.topic_updates[0][1]["last_published_at"]


def test_topic_editor_publisher_records_failed_send_after_enabled_attempt(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "true")

    class Channel:
        async def send(self, content):
            raise RuntimeError("discord unavailable")

    bot = SimpleNamespace(get_channel=lambda channel_id: Channel())
    db = FakeDB()
    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    topic = {
        "topic_id": "topic-1",
        "guild_id": 1,
        "state": "posted",
        "headline": "Failure",
        "summary": {"body": "Will fail."},
        "publication_attempts": 2,
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "failed"
    assert db.topic_updates[0][1]["publication_status"] == "failed"
    assert db.topic_updates[0][1]["publication_attempts"] == 3
    assert "discord unavailable" in db.topic_updates[0][1]["publication_error"]


def test_topic_editor_publisher_records_partial_after_mid_batch_failure(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "true")
    monkeypatch.setattr(
        topic_editor_module,
        "render_topic",
        lambda _topic: ["first message", "second message"],
    )

    class SentMessage:
        def __init__(self, message_id):
            self.id = message_id

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content):
            self.sent.append(content)
            if len(self.sent) == 2:
                raise RuntimeError("second send failed")
            return SentMessage(9101)

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    db = FakeDB()
    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    topic = {
        "topic_id": "topic-1",
        "guild_id": 1,
        "state": "posted",
        "headline": "Partial",
        "summary": {"body": "Two messages."},
        "publication_attempts": 0,
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert channel.sent == ["first message", "second message"]
    assert result["status"] == "partial"
    assert result["discord_message_ids"] == [9101]
    assert db.topic_updates[0][1]["publication_status"] == "partial"
    assert db.topic_updates[0][1]["discord_message_ids"] == [9101]
    assert db.topic_updates[0][1]["publication_attempts"] == 1
    assert "second send failed" in db.topic_updates[0][1]["publication_error"]
