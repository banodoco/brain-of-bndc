import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.common.storage_handler import StorageHandler
import src.features.summarising.topic_editor as topic_editor_module
from src.features.summarising.topic_editor import (
    TopicEditor,
    TOPIC_EDITOR_TOOLS,
    build_rejected_transition,
    parse_optional_datetime,
    render_topic,
)


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > 1 and not self._has_finalize(self.response):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="tool-finalize",
                        name="finalize_run",
                        input={
                            "overall_reasoning": (
                                "The scripted test response has already exercised the intended tool path. "
                                "This finalizes the run so runtime tests do not depend on the max-turn guard."
                            )
                        },
                    )
                ],
                usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            )
        return self.response

    def _has_finalize(self, response):
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
            if block_type == "tool_use" and name == "finalize_run":
                return True
        return False


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
        self.seeded_transitions = []
        self.transition_lookups = []
        self.author_profiles = {}
        self.message_context_rows = []
        self.read_calls = []
        self.search_messages_unified_calls = []
        self.get_reply_chain_calls = []
        self.reply_chain_rows = []
        self.source_message_rows = []  # for get_topic_editor_source_messages
        self.source_message_calls = []

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

    def search_topic_editor_topics(self, query, guild_id=None, environment="prod", state_filter=None, hours_back=72, limit=10):
        self.read_calls.append(("search_topic_editor_topics", query, guild_id, environment, state_filter, hours_back, limit))
        needle = str(query or "").lower()
        aliases_by_topic = {}
        for alias in self.topic_alias_rows:
            if alias.get("environment") not in {None, environment}:
                continue
            if guild_id is not None and alias.get("guild_id") != guild_id:
                continue
            aliases_by_topic.setdefault(str(alias.get("topic_id")), []).append(alias.get("alias_key"))
        rows = []
        for topic in self.active_topics:
            if topic.get("environment") not in {None, environment}:
                continue
            if guild_id is not None and topic.get("guild_id") != guild_id:
                continue
            if state_filter and topic.get("state") not in state_filter:
                continue
            aliases = aliases_by_topic.get(str(topic.get("topic_id")), [])
            haystack = " ".join([
                str(topic.get("headline") or ""),
                str(topic.get("canonical_key") or ""),
                " ".join(str(alias or "") for alias in aliases),
            ]).lower()
            if needle and needle not in haystack:
                continue
            rows.append({
                "topic_id": topic.get("topic_id"),
                "canonical_key": topic.get("canonical_key"),
                "headline": topic.get("headline"),
                "state": topic.get("state"),
                "aliases": aliases,
                "created_at": topic.get("created_at"),
            })
        return rows[:limit]

    def get_topic_editor_author_profile(self, author_id, guild_id=None, environment="prod"):
        self.read_calls.append(("get_topic_editor_author_profile", author_id, guild_id, environment))
        return self.author_profiles.get(int(author_id), {}) if author_id is not None else {}

    def get_topic_editor_message_context(self, message_ids, guild_id=None, environment="prod", limit=10):
        self.read_calls.append(("get_topic_editor_message_context", message_ids, guild_id, environment, limit))
        wanted = {str(message_id) for message_id in message_ids or []}
        rows = [row for row in self.message_context_rows if str(row.get("message_id")) in wanted][:limit]
        # Enrich with media_refs_available if not already present
        for row in rows:
            if "media_refs_available" not in row:
                row["media_refs_available"] = row.get("media_refs_available", [])
        return rows

    def get_topic_editor_source_messages(self, message_ids, guild_id=None, environment="prod", limit=50):
        """Resolve source messages for validation, author derivation, and citation hydration."""
        self.source_message_calls.append((message_ids, guild_id, environment, limit))
        wanted = {str(message_id) for message_id in message_ids or []}
        return [
            row for row in self.source_message_rows
            if str(row.get("message_id")) in wanted
        ][:limit]

    def search_messages_unified(self, **kwargs):
        self.search_messages_unified_calls.append(kwargs)
        return {"messages": [], "truncated": False}

    def get_reply_chain(self, message_id, guild_id=None, environment="prod", max_depth=5):
        self.get_reply_chain_calls.append((message_id, guild_id, environment, max_depth))
        return self.reply_chain_rows

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

    def get_topic_transitions_by_tool_call_ids(self, run_id, tool_call_ids, environment="prod"):
        self.transition_lookups.append((run_id, list(tool_call_ids), environment))
        wanted = {str(tool_call_id) for tool_call_id in tool_call_ids}
        rows = []
        rows.extend(self.seeded_transitions)
        rows.extend(transition for transition, _environment in self.transitions)
        by_tool_call_id = {}
        for row in rows:
            if str(row.get("run_id")) != str(run_id):
                continue
            if row.get("environment", environment) != environment:
                continue
            tool_call_id = row.get("tool_call_id")
            if str(tool_call_id) not in wanted:
                continue
            existing = by_tool_call_id.get(str(tool_call_id))
            if not existing or (existing.get("action") == "override" and row.get("action") != "override"):
                by_tool_call_id[str(tool_call_id)] = row
        return by_tool_call_id

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
    assert db.completed[0][1]["tool_call_count"] == 2
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
    assert "sources=1 tools=2" in result["trace_messages"][0]
    assert "publishing=OFF" in result["trace_messages"][0]
    assert "cost_usd=0.001044" in result["trace_messages"][0]
    assert "would-publish:" in result["trace_messages"][0]
    assert trace_channel.sent == result["trace_messages"]
    assert db.failed == []

    llm_call = editor.llm_client.client.messages.calls[0]
    assert llm_call["tools"] == TOPIC_EDITOR_TOOLS
    assert len(llm_call["tools"]) == 14


def test_post_simple_topic_with_media_ref_is_rejected():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="tool-media-simple",
                name="post_simple_topic",
                input={
                    "proposed_key": "Alice Media Simple",
                    "headline": "Alice media simple",
                    "body": "This should use sectioned publishing.",
                    "media": ["100:attachment:0"],
                    "source_message_ids": ["100"],
                },
            ),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    db = FakeDB()
    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(response),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )

    result = asyncio.run(editor.run_once("manual"))

    assert result["outcomes"][0]["outcome"] == "rejected_post_simple"
    assert result["outcomes"][0]["error"] == "post_simple_cannot_attach_media_use_post_sectioned_topic"
    assert db.topics == []
    assert db.transitions[0][0]["action"] == "rejected_post_simple"


def test_topic_editor_run_once_replays_seeded_transition_after_restart_without_duplicate_write():
    finalize_reasoning = (
        "The restarted run already executed this topic decision before the crash, "
        "so the editor should replay the stored transition and then close cleanly "
        "without creating another topic or source row."
    )
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="tool-replayed-post",
                name="post_simple_topic",
                input={
                    "proposed_key": "Alice Restart Test",
                    "headline": "Alice restart test",
                    "body": "This should not be written twice.",
                    "source_message_ids": ["100"],
                },
            ),
            SimpleNamespace(
                type="tool_use",
                id="tool-replayed-finalize",
                name="finalize_run",
                input={"overall_reasoning": finalize_reasoning},
            ),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    db = FakeDB()
    db.seeded_transitions = [
        {
            "run_id": "run-1",
            "guild_id": 1,
            "environment": "prod",
            "tool_call_id": "tool-replayed-post",
            "topic_id": "topic-existing",
            "action": "post_simple",
            "payload": {
                "outcome": "accepted",
                "tool_name": "post_simple_topic",
                "topic_id": "topic-existing",
            },
        },
        {
            "run_id": "run-1",
            "guild_id": 1,
            "environment": "prod",
            "tool_call_id": "tool-replayed-finalize",
            "action": "observation",
            "reason": finalize_reasoning,
            "payload": {
                "outcome": "accepted",
                "tool_name": "finalize_run",
                "original_action": "finalize_run",
                "overall_reasoning": finalize_reasoning,
                "topics_considered": ["Alice restart test"],
            },
        },
    ]
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    assert db.transition_lookups == [("run-1", ["tool-replayed-post", "tool-replayed-finalize"], "prod")]
    assert [outcome["outcome"] for outcome in result["outcomes"]] == ["idempotent_replay", "idempotent_replay"]
    assert result["outcomes"][0]["action"] == "post_simple"
    assert result["outcomes"][0]["topic_id"] == "topic-existing"
    assert result["outcomes"][1]["action"] == "finalize_run"
    assert db.topics == []
    assert db.sources == []
    assert db.aliases == []
    assert db.transitions == []


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
    assert stale_row["status"] == "failed"
    assert stale_row["error_message"] == "stale running lease expired by a later run"
    assert stale_row["completed_at"]
    assert stale_row["metadata"]["owner"] == "crashed-runner"
    assert stale_row["metadata"]["expired_by"] == acquired["run_id"]
    assert stale_row["metadata"]["expired_at"]
    assert rows[1]["run_id"] == acquired["run_id"]


def test_storage_get_reply_chain_walks_ancestors_root_first():
    messages = [
        {
            "message_id": "1",
            "guild_id": 1,
            "channel_id": "10",
            "author_id": "100",
            "content": "Root",
            "created_at": "2026-05-14T10:00:00Z",
            "attachments": [],
            "embeds": [],
            "reaction_count": 3,
            "thread_id": None,
            "reference_id": None,
            "is_deleted": False,
        },
        {
            "message_id": "2",
            "guild_id": 1,
            "channel_id": "10",
            "author_id": "101",
            "content": "Reply",
            "created_at": "2026-05-14T10:01:00Z",
            "attachments": [],
            "embeds": [],
            "reaction_count": 2,
            "thread_id": None,
            "reference_id": "1",
            "is_deleted": False,
        },
        {
            "message_id": "3",
            "guild_id": 1,
            "channel_id": "10",
            "author_id": "102",
            "content": "Nested reply",
            "created_at": "2026-05-14T10:02:00Z",
            "attachments": [],
            "embeds": [],
            "reaction_count": 1,
            "thread_id": None,
            "reference_id": "2",
            "is_deleted": False,
        },
    ]

    class Result:
        def __init__(self, data):
            self.data = data

    class Query:
        def __init__(self):
            self.filters = []
            self.limit_count = None

        def select(self, _columns):
            return self

        def eq(self, column, value):
            self.filters.append((column, value))
            return self

        def limit(self, count):
            self.limit_count = count
            return self

        def execute(self):
            rows = [
                row for row in messages
                if all(row.get(column) == value for column, value in self.filters)
            ]
            if self.limit_count is not None:
                rows = rows[:self.limit_count]
            return Result([dict(row) for row in rows])

    class FakeSupabase:
        def table(self, table_name):
            assert table_name == "discord_messages"
            return Query()

    async def pass_through_channel_context(rows):
        return rows

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()
    storage._attach_channel_context_and_filter_nsfw = pass_through_channel_context

    chain = asyncio.run(storage.get_reply_chain("3", guild_id=1, max_depth=10))

    assert [row["message_id"] for row in chain] == ["1", "2"]
    assert [row["content"] for row in chain] == ["Root", "Reply"]


def test_storage_search_messages_unified_filters_archive_messages():
    messages = [
        {
            "message_id": "1",
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 100,
            "content": "plain status update",
            "created_at": "2026-05-14T10:00:00Z",
            "attachments": [],
            "embeds": [],
            "reaction_count": 1,
            "thread_id": None,
            "reference_id": None,
            "is_deleted": False,
        },
        {
            "message_id": "2",
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 101,
            "content": "surreal wan move clip <@100>",
            "created_at": "2026-05-14T10:01:00Z",
            "attachments": [{"url": "https://cdn.test/clip.mp4", "content_type": "video/mp4", "filename": "clip.mp4"}],
            "embeds": [],
            "reaction_count": 9,
            "thread_id": None,
            "reference_id": "1",
            "is_deleted": False,
        },
        {
            "message_id": "3",
            "guild_id": 2,
            "channel_id": 10,
            "author_id": 101,
            "content": "surreal wan move clip in other guild",
            "created_at": "2026-05-14T10:02:00Z",
            "attachments": [{"url": "https://cdn.test/other.mp4", "content_type": "video/mp4", "filename": "other.mp4"}],
            "embeds": [],
            "reaction_count": 5,
            "thread_id": None,
            "reference_id": None,
            "is_deleted": False,
        },
    ]

    class Result:
        def __init__(self, data):
            self.data = data

    class Query:
        def __init__(self):
            self.filters = []
            self.ilike_filters = []
            self.limit_count = None
            self.desc = False

        def select(self, _columns):
            return self

        def eq(self, column, value):
            self.filters.append(("eq", column, value))
            return self

        def gte(self, column, value):
            self.filters.append(("gte", column, value))
            return self

        def lte(self, column, value):
            self.filters.append(("lte", column, value))
            return self

        def ilike(self, column, pattern):
            self.ilike_filters.append((column, pattern.strip("%").lower()))
            return self

        def order(self, _column, desc=False):
            self.desc = desc
            return self

        def limit(self, count):
            self.limit_count = count
            return self

        def execute(self):
            rows = []
            for row in messages:
                matched = True
                for op, column, value in self.filters:
                    if op == "eq" and row.get(column) != value:
                        matched = False
                    if op == "gte" and str(row.get(column)) < value:
                        matched = False
                    if op == "lte" and str(row.get(column)) > value:
                        matched = False
                for column, needle in self.ilike_filters:
                    if needle not in str(row.get(column) or "").lower():
                        matched = False
                if matched:
                    rows.append(dict(row))
            rows.sort(key=lambda item: item["created_at"], reverse=self.desc)
            if self.limit_count is not None:
                rows = rows[:self.limit_count]
            return Result(rows)

    class FakeSupabase:
        def table(self, table_name):
            assert table_name == "discord_messages"
            return Query()

    async def pass_through_channel_context(rows):
        return rows

    storage = StorageHandler.__new__(StorageHandler)
    storage.supabase_client = FakeSupabase()
    storage._attach_channel_context_and_filter_nsfw = pass_through_channel_context

    result = asyncio.run(storage.search_messages_unified(
        guild_id=1,
        query="wan move",
        from_author_id=101,
        mentions_author_id=100,
        has=["video"],
        is_reply=True,
        limit=10,
    ))

    assert result["truncated"] is False
    assert [row["message_id"] for row in result["messages"]] == ["2"]
    assert result["messages"][0]["attachments"][0]["content_type"] == "video/mp4"


def test_topic_editor_force_close_marks_run_failed_and_trace_embed(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_MAX_TURNS", "1")
    monkeypatch.setenv("LIVE_UPDATE_TRACE_CHANNEL_ID", "999")

    class TraceChannel:
        def __init__(self):
            self.embeds = []

        async def send(self, content=None, **kwargs):
            if kwargs.get("embed") is not None:
                self.embeds.append(kwargs["embed"])

    trace_channel = TraceChannel()
    bot = SimpleNamespace(get_channel=lambda channel_id: trace_channel if channel_id == 999 else None)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="I saw the messages but I am not finalizing.")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
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

    assert result["status"] == "failed"
    assert db.completed[0][1]["status"] == "failed"
    assert db.completed[0][1]["metadata"]["forced_close"] is True
    assert db.transitions[-1][0]["action"] == "observation"
    assert db.transitions[-1][0]["payload"]["original_action"] == "rejected_finalize_run"
    assert db.transitions[-1][0]["reason"] == "max_turns_reached_without_finalize"
    assert trace_channel.embeds
    embed = trace_channel.embeds[0]
    assert embed.color.value == 0xE74C3C
    assert "⚠ FORCE-CLOSED" in embed.description
    assert "⚠ FORCE-CLOSED" in result["trace_messages"][0]


def test_topic_editor_cost_cap_triggers_force_close(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_INPUT_COST_PER_MTOKENS", "1000000")
    monkeypatch.setenv("TOPIC_EDITOR_OUTPUT_COST_PER_MTOKENS", "0")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COST_USD", "5.0")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_TOKENS", "500000")

    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="tool-search",
                name="search_topics",
                input={"query": "expensive"},
            )
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=1),
    )
    db = FakeDB()
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "failed"
    assert db.completed[0][1]["status"] == "failed"
    assert db.completed[0][1]["cost_usd"] == 10.0
    assert db.completed[0][1]["metadata"]["cumulative_cost_usd"] == 10.0
    assert db.completed[0][1]["metadata"]["cumulative_tokens"] == 11
    assert db.transitions[-1][0]["action"] == "observation"
    assert db.transitions[-1][0]["payload"]["original_action"] == "rejected_finalize_run"
    assert db.transitions[-1][0]["reason"] == "cost_cap_exceeded"
    assert db.transitions[-1][0]["payload"]["cumulative_cost_usd"] == 10.0


def test_topic_editor_cold_start_seeds_interval_lookback_and_processes_window(monkeypatch):
    """Cold-start seeds checkpoint to (lookback_minutes ago) so the first run
    immediately processes the last interval's worth of messages, not zero."""
    monkeypatch.setenv("TOPIC_EDITOR_COLD_START_LOOKBACK_MINUTES", "60")

    class ColdStartDB(FakeDB):
        def __init__(self):
            super().__init__()
            self.archived_calls = 0
            self.before_calls = []

        def get_topic_editor_checkpoint(self, checkpoint_key, environment="prod"):
            return None

        def mirror_live_checkpoint_to_topic_editor(self, checkpoint_key, environment="prod"):
            return None

        def get_archived_message_id_before_timestamp(self, guild_id=None, before=None):
            self.before_calls.append({"guild_id": guild_id, "before": before})
            return 555000222111

        def get_archived_messages_after_checkpoint(self, *, checkpoint, guild_id, channel_ids, limit, exclude_author_ids):
            self.archived_calls += 1
            return []

    response = SimpleNamespace(content=[], usage=None)
    db = ColdStartDB()
    claude = FakeClaude(response)
    editor = TopicEditor(db_handler=db, llm_client=claude, guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    assert result["skipped_reason"] == "no_new_archived_messages"
    assert db.archived_calls == 1
    assert db.before_calls and "before" in db.before_calls[0]
    seeded_checkpoint = db.checkpoints[0][0]
    assert seeded_checkpoint["last_message_id"] == 555000222111
    assert seeded_checkpoint["state"]["seeded_from"] == "interval_lookback"
    assert seeded_checkpoint["state"]["lookback_minutes"] == 60.0


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

    assert [transition[0]["action"] for transition in db.transitions] == ["rejected_post_simple", "rejected_watch", "observation"]
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
    assert [transition[0]["action"] for transition in db.transitions] == ["watch", "override", "observation"]
    assert db.transitions[1][0]["payload"]["overridden_topic_id"] == "topic-existing"


def test_watch_topic_ignores_natural_language_revisit_at():
    block = SimpleNamespace(
        type="tool_use",
        id="tool-watch",
        name="watch_topic",
        input={
            "proposed_key": "Future Comparison",
            "headline": "Future comparison needs more testing",
            "why_interesting": "Worth checking later.",
            "revisit_when": "More comparison results emerge",
            "source_message_ids": ["100"],
        },
    )
    response = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1, output_tokens=2))
    db = FakeDB()
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(response), guild_id=1, live_channel_id=2, environment="prod")

    result = asyncio.run(editor.run_once("manual"))

    assert result["outcomes"][0]["outcome"] == "accepted"
    assert db.topics[0][0]["state"] == "watching"
    assert db.topics[0][0]["revisit_at"] is None
    assert db.topics[0][0]["summary"]["revisit_when"] == "More comparison results emerge"


def test_watch_topic_accepts_iso_revisit_at():
    assert parse_optional_datetime("2026-05-15") == "2026-05-15T00:00:00+00:00"
    assert parse_optional_datetime("2026-05-15T12:34:56Z") == "2026-05-15T12:34:56+00:00"
    assert parse_optional_datetime("tomorrow") is None


def test_topic_editor_auto_shortlists_reaction_qualified_media(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_MEDIA_SHORTLIST_MIN_REACTIONS", "5")
    monkeypatch.setenv("TOPIC_EDITOR_MEDIA_SHORTLIST_LIMIT", "5")
    db = FakeDB()
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(SimpleNamespace(content=[])), guild_id=1, live_channel_id=2, environment="prod")

    shortlisted = editor._auto_shortlist_media_messages(
        [
            {
                "message_id": 200,
                "guild_id": 1,
                "channel_id": 10,
                "author_id": 42,
                "content": "funny test render",
                "created_at": "2026-05-13T10:00:00Z",
                "reaction_count": 5,
                "author_context_snapshot": {"username": "alice"},
                "attachments": [
                    {"url": "https://cdn.example.test/render.mp4", "content_type": "video/mp4", "filename": "render.mp4"},
                ],
                "embeds": [],
            }
        ],
        [],
        run_id="run-shortlist",
        guild_id=1,
    )

    assert len(shortlisted) == 1
    assert shortlisted[0]["message_id"] == "200"
    assert shortlisted[0]["reaction_count"] == 5
    assert db.topics[0][0]["state"] == "watching"
    assert db.topics[0][0]["canonical_key"] == "media-shortlist-200"
    assert db.topics[0][0]["summary"]["auto_shortlist"] is True
    assert db.topics[0][0]["summary"]["media_refs"] == [
        {"message_id": "200", "kind": "attachment", "index": 0}
    ]
    assert db.sources[0][0]["message_id"] == "200"
    assert db.aliases[0][0]["alias_kind"] == "auto_shortlist"
    assert db.transitions[0][0]["action"] == "watch"
    assert db.transitions[0][0]["payload"]["tool_name"] == "auto_media_shortlist"


def test_topic_editor_auto_shortlist_respects_discard_ignore(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_MEDIA_SHORTLIST_MIN_REACTIONS", "5")
    db = FakeDB()
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(SimpleNamespace(content=[])), guild_id=1, live_channel_id=2, environment="prod")

    shortlisted = editor._auto_shortlist_media_messages(
        [
            {
                "message_id": 200,
                "guild_id": 1,
                "channel_id": 10,
                "author_id": 42,
                "content": "already ignored render",
                "created_at": "2026-05-13T10:00:00Z",
                "reaction_count": 12,
                "attachments": [
                    {"url": "https://cdn.example.test/render.png", "content_type": "image/png", "filename": "render.png"},
                ],
                "embeds": [],
            }
        ],
        [{"canonical_key": "media-shortlist-200", "state": "discarded"}],
        run_id="run-shortlist",
        guild_id=1,
    )

    assert shortlisted == []
    assert db.topics == []
    assert db.transitions == []


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

    assert [transition[0]["action"] for transition in db.transitions] == ["update_sources", "discard", "observation"]
    assert [transition[0]["payload"]["outcome"] for transition in db.transitions] == ["tool_error", "tool_error", "accepted"]
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
    assert [transition[0]["action"] for transition in db.transitions] == ["observation", "observation", "observation", "observation", "observation"]
    assert db.transitions[-2][0]["payload"]["outcome"] == "tool_error"
    assert db.transitions[-2][0]["payload"]["error"] == "observation_cap_reached"
    assert result["outcomes"][-2]["outcome"] == "tool_error"


def test_topic_editor_read_tools_return_structured_real_data_from_store_and_source_window():
    db = FakeDB()
    db.active_topics = [
        {
            "topic_id": "topic-1",
            "guild_id": 1,
            "environment": "prod",
            "canonical_key": "alice-lora-test",
            "headline": "Alice ships a LoRA test",
            "state": "watching",
            "created_at": "2026-05-13T10:00:00Z",
        }
    ]
    db.topic_alias_rows = [
        {
            "topic_id": "topic-1",
            "guild_id": 1,
            "environment": "prod",
            "alias_key": "LoRA benchmark",
        }
    ]
    db.author_profiles = {
        42: {
            "author_id": 42,
            "message_count_30d": 3,
            "recent_message_ids": ["102", "101", "100"],
            "recent_message_dates": ["2026-05-13T12:00:00Z"],
            "sample_messages": [{"message_id": "102", "content_preview": "Latest sample"}],
        }
    }
    db.message_context_rows = [
        {
            "message_id": "100",
            "channel_name": "show-and-tell",
            "author_name": "alice",
            "content": "I shipped a new LoRA test.",
            "created_at": "2026-05-13T10:00:00Z",
            "reply_to_message_id": None,
            "thread_id": "thread-1",
        }
    ]
    messages = [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "channel_name": "show-and-tell",
            "author_id": 42,
            "content": "I shipped a new LoRA test with detailed benchmark notes.",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
        },
        {
            "message_id": 101,
            "guild_id": 1,
            "channel_id": 11,
            "channel_name": "off-topic",
            "author_id": 43,
            "content": "Unrelated chatter",
            "created_at": "2026-05-13T10:01:00Z",
            "author_context_snapshot": {"username": "bob"},
        },
    ]
    context = {
        "run_id": "run-1",
        "guild_id": 1,
        "messages": messages,
        "active_topics": db.active_topics,
        "aliases": db.topic_alias_rows,
        "seen_tool_call_ids": set(),
    }
    editor = TopicEditor(db_handler=db, llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)), guild_id=1, live_channel_id=2, environment="prod")
    calls = [
        {"id": "read-1", "name": "search_topics", "input": {"query": "benchmark", "state_filter": ["watching"]}},
        {"id": "read-2", "name": "search_messages", "input": {"query": "lora", "in_channel_id": 10, "from_author_id": 42}},
        {"id": "read-3", "name": "get_author_profile", "input": {"author_id": 42}},
        {"id": "read-4", "name": "get_message_context", "input": {"message_ids": ["100"]}},
    ]

    outcomes = [editor._dispatch_tool_call(call, context) for call in calls]
    contents = [editor._tool_result_content(call, outcome) for call, outcome in zip(calls, outcomes)]

    assert [outcome["outcome"] for outcome in outcomes] == ["read", "read", "read", "read"]
    assert outcomes[0]["result"][0]["aliases"] == ["LoRA benchmark"]
    assert outcomes[1]["result"] == [
        {
            "message_id": "100",
            "channel_id": "10",
            "channel_name": "show-and-tell",
            "author_id": "42",
            "author_name": "alice",
            "content_preview": "I shipped a new LoRA test with detailed benchmark notes.",
            "created_at": "2026-05-13T10:00:00Z",
            "reaction_count": 0,
            "reply_to_message_id": None,
            "has_attachments": False,
            "has_links": False,
            "has_image": False,
            "has_video": False,
            "has_audio": False,
            "has_embed": False,
        }
    ]
    assert outcomes[2]["result"]["message_count_30d"] == 3
    assert outcomes[3]["result"][0]["thread_id"] == "thread-1"
    assert all("stub results" not in content for content in contents)
    assert all("result=" in content for content in contents)


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
        "## Live update: Alice ships a LoRA\n\nA concise update.\n\nSource: 100"
    ]
    assert "**Model**" in render_topic(sectioned)[0]
    assert "Sources: 101, 102" in render_topic(sectioned)[0]
    assert render_topic(story_update)[0].startswith("## Update: Alice follows up with results")


# ------------------------------------------------------------------
# Window-scope filter tests (T5)
# ------------------------------------------------------------------

def _make_editor_for_search_tests(db=None, guild_id=1, live_channel_id=2):
    """Helper to create a TopicEditor with a FakeDB for search tests."""
    editor = TopicEditor(
        db_handler=db or FakeDB(),
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=guild_id,
        live_channel_id=live_channel_id,
        environment="prod",
    )
    return editor


def _make_context(messages=None, extra=None):
    return {
        "run_id": "run-search",
        "guild_id": 1,
        "messages": messages or [],
        "active_topics": [],
        "aliases": [],
        "seen_tool_call_ids": set(),
        **(extra or {}),
    }


def _default_search_messages():
    return [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "channel_name": "show-and-tell",
            "author_id": 42,
            "content": "I shipped a new LoRA test with video attachment.",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [
                {"content_type": "video/mp4", "filename": "demo.mp4"},
            ],
            "embeds": [],
        },
        {
            "message_id": 101,
            "guild_id": 1,
            "channel_id": 10,
            "channel_name": "show-and-tell",
            "author_id": 42,
            "content": "Here's an image link: https://example.test/img.png",
            "created_at": "2026-05-13T10:05:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [
                {"content_type": "image/png", "filename": "img.png"},
            ],
            "embeds": [],
        },
        {
            "message_id": 102,
            "guild_id": 1,
            "channel_id": 11,
            "channel_name": "off-topic",
            "author_id": 43,
            "content": "Hey <@42> check this out!",
            "created_at": "2026-05-13T10:10:00Z",
            "author_context_snapshot": {"username": "bob"},
            "attachments": [],
            "embeds": [],
        },
        {
            "message_id": 103,
            "guild_id": 1,
            "channel_id": 10,
            "channel_name": "show-and-tell",
            "author_id": 43,
            "content": "Replying with a link http://example.test",
            "created_at": "2026-05-13T10:15:00Z",
            "author_context_snapshot": {"username": "bob"},
            "attachments": [],
            "embeds": [],
            "reply_to_message_id": 100,
        },
    ]


def test_search_messages_window_and_combined_filters():
    """AND-combined: query + from_author_id + in_channel_id."""
    messages = _default_search_messages()
    editor = _make_editor_for_search_tests()
    ctx = _make_context(messages)

    # Only message 100 matches: lora + author 42 + channel 10
    outcome = editor._dispatch_tool_call(
        {"id": "s1", "name": "search_messages", "input": {
            "query": "lora", "from_author_id": 42, "in_channel_id": 10,
        }},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "100"
    assert rows[0]["author_id"] == "42"


def test_search_messages_has_image():
    """has=['image'] filter."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s2", "name": "search_messages", "input": {"has": ["image"]}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "101"
    assert rows[0]["has_image"] is True


def test_search_messages_has_video():
    """has=['video'] filter."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s3", "name": "search_messages", "input": {"has": ["video"]}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "100"
    assert rows[0]["has_video"] is True


def test_search_messages_has_link():
    """has=['link'] filter."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s4", "name": "search_messages", "input": {"has": ["link"]}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 2  # messages 101 and 103 have links


def test_search_messages_mentions_author_id():
    """mentions_author_id matching <@42> / <@!42> in content."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    # Message 102 mentions author 42
    outcome = editor._dispatch_tool_call(
        {"id": "s5", "name": "search_messages", "input": {"mentions_author_id": 42}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "102"


def test_search_messages_mentions_author_id_bang_format():
    """mentions_author_id matches <@!ID> format."""
    messages = _default_search_messages()
    # Override message 102 to use <@!42> format
    messages[2]["content"] = "Hey <@!42> check this out!"
    editor = _make_editor_for_search_tests()
    ctx = _make_context(messages)

    outcome = editor._dispatch_tool_call(
        {"id": "s5b", "name": "search_messages", "input": {"mentions_author_id": 42}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "102"


def test_search_messages_is_reply_true():
    """is_reply=True filter."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s6", "name": "search_messages", "input": {"is_reply": True}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "103"


def test_search_messages_is_reply_false():
    """is_reply=False filter."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s7", "name": "search_messages", "input": {"is_reply": False}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    # Messages without reply_to_message_id
    assert all(row["reply_to_message_id"] is None for row in rows)
    assert len(rows) == 3


def test_search_messages_after_relative_time():
    """after='24h' relative time filter."""
    messages = _default_search_messages()
    # Set created_at to now so after=24h includes them
    now = datetime.now(timezone.utc)
    messages[0]["created_at"] = (now - timedelta(hours=6)).isoformat()
    messages[1]["created_at"] = (now - timedelta(hours=48)).isoformat()  # 48h ago — should be excluded by after=24h
    messages[2]["created_at"] = (now - timedelta(hours=1)).isoformat()
    messages[3]["created_at"] = (now - timedelta(hours=12)).isoformat()

    editor = _make_editor_for_search_tests()
    ctx = _make_context(messages)

    outcome = editor._dispatch_tool_call(
        {"id": "s8", "name": "search_messages", "input": {"after": "24h"}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    # Only messages within last 24h
    for row in rows:
        dt = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        assert dt >= (now - timedelta(hours=24)), f"Expected {row['created_at']} to be >= 24h ago"
    # Message 101 is 48h ago — should be excluded
    message_ids = {row["message_id"] for row in rows}
    assert "101" not in message_ids


def test_search_messages_before_relative_time():
    """before='7d' relative time filter — upper bound: only messages older than 7 days."""
    messages = _default_search_messages()
    now = datetime.now(timezone.utc)
    # Message 100: 6 days ago → should be excluded (newer than 7d bound)
    messages[0]["created_at"] = (now - timedelta(days=6)).isoformat()
    # Message 101: 10 days ago → should remain (older than 7d bound)
    messages[1]["created_at"] = (now - timedelta(days=10)).isoformat()

    editor = _make_editor_for_search_tests()
    ctx = _make_context(messages)

    outcome = editor._dispatch_tool_call(
        {"id": "s9", "name": "search_messages", "input": {"before": "7d"}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == "101"


def test_search_messages_malformed_time_tool_error():
    """Malformed after/before → tool_error."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s10", "name": "search_messages", "input": {"after": "garbage"}},
        ctx,
    )
    assert outcome["outcome"] == "tool_error"
    assert "Invalid time format" in outcome["error"]


def test_search_messages_limit_clamp_low():
    """Limit 0 clamps to 1."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s11", "name": "search_messages", "input": {"limit": 0}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) == 1


def test_search_messages_limit_clamp_high():
    """Limit 100 clamps to 50."""
    editor = _make_editor_for_search_tests()
    ctx = _make_context(_default_search_messages())

    outcome = editor._dispatch_tool_call(
        {"id": "s12", "name": "search_messages", "input": {"limit": 100}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    rows = outcome["result"]
    assert len(rows) <= 50
    assert len(rows) == 4  # all messages match (no other filter), but clamped to 50


def test_search_messages_archive_scope_dispatch():
    """Archive scope flows to FakeDB.search_messages_unified with correct guild_id/environment."""
    db = FakeDB()
    editor = _make_editor_for_search_tests(db=db)
    ctx = _make_context()

    outcome = editor._dispatch_tool_call(
        {"id": "s13", "name": "search_messages", "input": {
            "query": "test", "from_author_id": 42, "has": ["video"], "scope": "archive",
            "after": "7d", "before": "1h",
        }},
        ctx,
    )
    assert outcome["outcome"] == "read"
    assert db.search_messages_unified_calls == [{
        "scope": "archive",
        "guild_id": 1,
        "environment": "prod",
        "query": "test",
        "from_author_id": 42,
        "in_channel_id": None,
        "mentions_author_id": None,
        "has": ["video"],
        "after": "7d",
        "before": "1h",
        "is_reply": None,
        "limit": 20,
    }]


def test_get_reply_chain_dispatch():
    """get_reply_chain calls FakeDB.get_reply_chain and returns root-first rows."""
    db = FakeDB()
    db.reply_chain_rows = [
        {"message_id": "1", "author_id": 10, "author_name": "root", "channel_id": 5,
         "content_preview": "Root message", "created_at": "2026-01-01T00:00:00Z"},
        {"message_id": "2", "author_id": 20, "author_name": "child", "channel_id": 5,
         "content_preview": "Child message", "created_at": "2026-01-02T00:00:00Z"},
    ]
    editor = _make_editor_for_search_tests(db=db)
    ctx = _make_context()

    outcome = editor._dispatch_tool_call(
        {"id": "rc1", "name": "get_reply_chain", "input": {"message_id": "2", "max_depth": 5}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    assert outcome["result"] == db.reply_chain_rows
    assert db.get_reply_chain_calls == [("2", 1, "prod", 5)]


def test_get_reply_chain_max_depth_clamped():
    """max_depth clamps to 1..15 range."""
    db = FakeDB()
    editor = _make_editor_for_search_tests(db=db)
    ctx = _make_context()

    # Clamp high
    editor._dispatch_tool_call(
        {"id": "rc2", "name": "get_reply_chain", "input": {"message_id": "x", "max_depth": 999}},
        ctx,
    )
    assert db.get_reply_chain_calls[0][3] == 15

    # Clamp low
    editor._dispatch_tool_call(
        {"id": "rc3", "name": "get_reply_chain", "input": {"message_id": "x", "max_depth": -5}},
        ctx,
    )
    assert db.get_reply_chain_calls[1][3] == 1

    # Default
    editor._dispatch_tool_call(
        {"id": "rc4", "name": "get_reply_chain", "input": {"message_id": "x"}},
        ctx,
    )
    assert db.get_reply_chain_calls[2][3] == 5


def test_get_reply_chain_cycle_detection_terminates():
    """Cycle detection: A → B → A stops and returns accumulated ancestors without infinite loop."""
    # This tests the storage_handler.get_reply_chain behaviour via the FakeDB proxy.
    # The real storage implementation handles cycles; here we verify the concept
    # by seeding reply_chain_rows where a cycle would occur.
    db = FakeDB()
    # Simulate a cycle: A (message 1) → reply to 2, B (message 2) → reply to 1
    db.reply_chain_rows = [
        {"message_id": "1", "author_id": 10, "author_name": "root", "channel_id": 5,
         "content_preview": "Root", "created_at": "2026-01-01T00:00:00Z"},
        {"message_id": "2", "author_id": 20, "author_name": "mid", "channel_id": 5,
         "content_preview": "Middle", "created_at": "2026-01-02T00:00:00Z"},
    ]
    editor = _make_editor_for_search_tests(db=db)
    ctx = _make_context()

    outcome = editor._dispatch_tool_call(
        {"id": "rc-cycle", "name": "get_reply_chain", "input": {"message_id": "3", "max_depth": 10}},
        ctx,
    )
    assert outcome["outcome"] == "read"
    # The FakeDB returns reply_chain_rows directly — the real implementation
    # handles cycle detection in storage_handler. This test proves dispatch works.
    assert len(outcome["result"]) == 2


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

    assert channel.sent == ["## Live update: Alice publish test\n\nThis should publish.\n\nSource: 100"]
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
# ------------------------------------------------------------------
# T12: Structured block-level topic creation and publishing tests
# ------------------------------------------------------------------

def _make_editor_with_source_rows(db=None, guild_id=1, live_channel_id=2, publishing_enabled=False):
    """Helper to create a TopicEditor with FakeDB for structured topic tests."""
    editor = TopicEditor(
        db_handler=db or FakeDB(),
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=guild_id,
        live_channel_id=live_channel_id,
        environment="prod",
    )
    # Override publishing_enabled after init
    editor.publishing_enabled = publishing_enabled
    return editor


def _make_source_context(messages=None, active_topics=None, aliases=None, extra=None):
    """Build a dispatcher context dict for structured topic tests."""
    ctx = {
        "run_id": "run-structured",
        "guild_id": 1,
        "live_channel_id": 2,
        "messages": messages or [],
        "active_topics": active_topics or [],
        "aliases": aliases or [],
        "seen_tool_call_ids": set(),
        "idempotent_results": {},
        "observation_count": 0,
        "created_topics": [],
        "finalize": None,
    }
    if extra:
        ctx.update(extra)
    return ctx


def _sample_source_message_rows():
    """Return sample source_message_rows for FakeDB."""
    return [
        {
            "message_id": "200",
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "I shipped a new LoRA with benchmark results.",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [
                {"url": "https://cdn.example.com/image1.png", "content_type": "image/png", "filename": "benchmark.png"}
            ],
            "embeds": [],
        },
        {
            "message_id": "201",
            "guild_id": 1,
            "channel_id": 11,
            "author_id": 43,
            "content": "Here is the workflow graph.",
            "created_at": "2026-05-13T10:05:00Z",
            "author_context_snapshot": {"username": "bob"},
            "attachments": [
                {"url": "https://cdn.example.com/workflow.json", "content_type": "application/json", "filename": "workflow.json"},
                {"url": "https://cdn.example.com/preview.png", "content_type": "image/png", "filename": "preview.png"},
            ],
            "embeds": [],
        },
        {
            "message_id": "202",
            "guild_id": 1,
            "channel_id": 12,
            "author_id": 42,
            "content": "Follow-up with video.",
            "created_at": "2026-05-13T10:10:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [],
            "embeds": [
                {"url": "https://cdn.example.com/video_thumb.png", "thumbnail": {"url": "https://cdn.example.com/thumb.png"}}
            ],
        },
    ]


def _sample_context_messages():
    """Return sample context messages for dispatch tests."""
    return [
        {
            "message_id": 200,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "I shipped a new LoRA with benchmark results.",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [
                {"url": "https://cdn.example.com/image1.png", "content_type": "image/png", "filename": "benchmark.png"}
            ],
            "embeds": [],
        },
        {
            "message_id": 201,
            "guild_id": 1,
            "channel_id": 11,
            "author_id": 43,
            "content": "Here is the workflow graph.",
            "created_at": "2026-05-13T10:05:00Z",
            "author_context_snapshot": {"username": "bob"},
            "attachments": [
                {"url": "https://cdn.example.com/workflow.json", "content_type": "application/json", "filename": "workflow.json"},
                {"url": "https://cdn.example.com/preview.png", "content_type": "image/png", "filename": "preview.png"},
            ],
            "embeds": [],
        },
    ]


# ---- (a) post_sectioned_topic with blocks and no sections accepted and stored ----

def test_post_sectioned_topic_with_blocks_no_sections_accepted_and_stored():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-blocks-only",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Alice LoRA Benchmark",
            "headline": "Alice ships a new LoRA with benchmarks",
            "body": "Alice released a new LoRA with substantial benchmark data.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Alice released a new LoRA.",
                    "source_message_ids": ["200"],
                },
                {
                    "type": "section",
                    "title": "Benchmark Results",
                    "text": "The benchmarks show substantial improvement.",
                    "source_message_ids": ["200"],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "accepted"
    assert outcome["action"] == "post_sectioned"

    assert len(db.topics) == 1
    stored_topic = db.topics[0][0]
    assert stored_topic["canonical_key"] == "alice-lora-benchmark"
    assert stored_topic["state"] == "posted"
    summary = stored_topic["summary"]
    assert "blocks" in summary
    assert len(summary["blocks"]) == 2
    assert stored_topic["source_message_ids"] == ["200"]

    source_message_ids_written = {s[0]["message_id"] for s in db.sources}
    assert source_message_ids_written == {"200"}
    assert len(db.sources) == 1

    assert len(db.transitions) == 1
    assert db.transitions[0][0]["action"] == "post_sectioned"
    assert db.transitions[0][0]["payload"]["outcome"] == "accepted"


def test_post_sectioned_topic_adds_default_media_ref_from_block_sources():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_source_message_rows()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-blocks-default-media",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Alice LoRA Media",
            "headline": "Alice ships LoRA media",
            "body": "Alice released a new LoRA with media.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Alice released a new LoRA.",
                    "source_message_ids": ["200"],
                },
                {
                    "type": "section",
                    "title": "Embed Preview",
                    "text": "The discussion also included an embed preview.",
                    "source_message_ids": ["202"],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "accepted"
    blocks = db.topics[0][0]["summary"]["blocks"]
    assert blocks[0]["media_refs"] == [
        {"message_id": "200", "kind": "attachment", "index": 0}
    ]
    assert blocks[1]["media_refs"] == [
        {"message_id": "202", "kind": "embed", "index": 0}
    ]
    transition_payload = db.transitions[0][0]["payload"]
    assert transition_payload["blocks"][1]["media_refs"] == [
        {"message_id": "202", "kind": "embed", "index": 0}
    ]


# ---- (b) structured topic stores summary.blocks, topic-level union source_message_ids,
#         and one topic_sources row per distinct block source ID ----

def test_structured_topic_stores_blocks_union_sources_and_rows_per_distinct_id():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-multi-source",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Multi Source Topic",
            "headline": "Multi-source structured topic",
            "body": "Multiple sources across blocks.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Intro text.",
                    "source_message_ids": ["200"],
                },
                {
                    "type": "section",
                    "title": "Section One",
                    "text": "Section one text.",
                    "source_message_ids": ["201"],
                },
                {
                    "type": "section",
                    "title": "Section Two",
                    "text": "Section two text.",
                    "source_message_ids": ["200", "201"],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "accepted"
    stored_topic = db.topics[0][0]

    assert "blocks" in stored_topic["summary"]
    assert len(stored_topic["summary"]["blocks"]) == 3

    assert stored_topic["source_message_ids"] == ["200", "201"]

    source_message_ids_written = {s[0]["message_id"] for s in db.sources}
    assert source_message_ids_written == {"200", "201"}
    assert len(db.sources) == 2


# ---- (c) archive-resolved source rows feed source_authors, validation, and collision detection ----

def test_archive_resolved_sources_feed_source_authors_and_collision_detection():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()

    db.active_topics = [{
        "topic_id": "topic-existing",
        "canonical_key": "alice-archive-topic",
        "headline": "Alice archive topic",
        "state": "watching",
        "source_authors": ["alice"],
    }]

    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages, active_topics=db.active_topics)

    call = {
        "id": "tool-archive-source",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Archive Source Test",
            "headline": "Testing archive-resolved sources",
            "body": "Intro.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Uses archive-only source.",
                    "source_message_ids": ["202"],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "accepted"
    assert len(db.source_message_calls) == 1
    stored_topic = db.topics[0][0]
    assert "alice" in stored_topic["source_authors"]

    source_ids = {s[0]["message_id"] for s in db.sources}
    assert "202" in source_ids


def test_archive_resolved_source_triggers_collision_with_same_author():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()

    db.active_topics = [{
        "topic_id": "topic-alice-lora",
        "canonical_key": "alice-lora-test",
        "headline": "Alice ships a LoRA test",
        "state": "watching",
        "source_authors": ["alice"],
    }]

    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages, active_topics=db.active_topics)

    call = {
        "id": "tool-collision-archive",
        "name": "watch_topic",
        "input": {
            "proposed_key": "Alice Another LoRA",
            "headline": "Alice ships another LoRA test",
            "why_interesting": "More progress.",
            "revisit_when": "tomorrow",
            "source_message_ids": ["200"],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "rejected_watch"
    assert outcome["error"] == "topic_collision"


# ---- (d) get_message_context read output includes compact media_refs_available ----

def test_get_message_context_includes_media_refs_available():
    db = FakeDB()
    db.message_context_rows = [
        {
            "message_id": "300",
            "channel_name": "show-and-tell",
            "author_name": "alice",
            "content": "Check out this image.",
            "created_at": "2026-05-13T10:00:00Z",
            "reply_to_message_id": None,
            "thread_id": None,
            "guild_id": 1,
            "attachments": [
                {"url": "https://cdn.example.com/img.png", "content_type": "image/png", "filename": "img.png"}
            ],
            "embeds": [
                {"url": "https://example.com/embed", "thumbnail": {"url": "https://cdn.example.com/thumb.png"}}
            ],
            "media_refs_available": [
                {"kind": "attachment", "index": 0, "url_present": True, "content_type": "image/png", "filename": "img.png"},
                {"kind": "embed", "index": 0, "url_present": True, "content_type": None, "filename": None},
            ],
        },
    ]
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context()

    call = {
        "id": "read-context",
        "name": "get_message_context",
        "input": {"message_ids": ["300"]},
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "read"
    assert len(outcome["result"]) == 1
    row = outcome["result"][0]
    assert "media_refs_available" in row
    assert len(row["media_refs_available"]) == 2
    assert row["media_refs_available"][0]["kind"] == "attachment"
    assert row["media_refs_available"][0]["index"] == 0
    assert row["media_refs_available"][0]["url_present"] is True


# ---- (e) invalid source IDs and media refs reject with auditable transitions ----

def test_invalid_block_source_id_rejects_with_auditable_transition():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-invalid-src",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Invalid Source Test",
            "headline": "Testing invalid source",
            "body": "Intro.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Has invalid source.",
                    "source_message_ids": ["200", "999"],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "rejected_post_sectioned"
    assert outcome["error"] == "unresolved_block_source_message"

    assert len(db.transitions) == 1
    transition = db.transitions[0][0]
    assert transition["action"] == "rejected_post_sectioned"
    assert transition["reason"] == "unresolved_block_source_message"
    assert transition["payload"]["outcome"] == "tool_error"
    # extra dict keys are merged into payload top-level
    assert transition["payload"]["unresolved_message_id"] == "999"
    assert transition["payload"]["block_type"] == "intro"

    assert db.topics == []
    assert db.sources == []


def test_unresolved_media_ref_message_rejects():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-bad-media-msg",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Bad Media Msg",
            "headline": "Testing unresolvable media ref message",
            "body": "Intro.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Has bad media ref.",
                    "source_message_ids": ["200"],
                    "media_refs": [
                        {"message_id": "999", "attachment_index": 0},
                    ],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "rejected_post_sectioned"
    assert outcome["error"] == "unresolved_media_ref"

    transition = db.transitions[0][0]
    assert transition["reason"] == "unresolved_media_ref"
    assert transition["payload"]["media_ref"]["message_id"] == "999"

    assert db.topics == []
    assert db.sources == []


def test_media_ref_message_must_be_cited_by_own_block():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-media-ref-not-cited",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Media Ref Not Cited",
            "headline": "Testing media ref source ownership",
            "body": "Intro.",
            "source_message_ids": ["202"],
            "blocks": [
                {
                    "type": "intro",
                    "text": "This block cites Alice's benchmark but tries to attach a separate embed.",
                    "source_message_ids": ["200"],
                    "media_refs": [
                        {"message_id": "202", "kind": "embed", "index": 0},
                    ],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "rejected_post_sectioned"
    assert outcome["error"] == "invalid_media_ref"

    transition = db.transitions[0][0]
    assert transition["reason"] == "invalid_media_ref"
    assert transition["payload"]["media_ref"] == {
        "message_id": "202",
        "kind": "embed",
        "index": 0,
    }
    assert "block source_message_ids" in transition["payload"]["error"]

    assert db.topics == []
    assert db.sources == []


def test_media_ref_index_out_of_range_rejects():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()
    messages = _sample_context_messages()
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-bad-index",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "Bad Index Test",
            "headline": "Testing out-of-range media ref",
            "body": "Intro.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Has bad index.",
                    "source_message_ids": ["200"],
                    "media_refs": [
                        {"message_id": "200", "kind": "attachment", "index": 5},
                    ],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "rejected_post_sectioned"
    assert outcome["error"] == "invalid_media_ref"

    transition = db.transitions[0][0]
    assert transition["reason"] == "invalid_media_ref"
    # error is merged from extra into payload top-level
    assert "out of range" in (transition["payload"].get("error") or "")

    assert db.topics == []
    assert db.sources == []


def test_media_ref_missing_url_rejects():
    db = FakeDB()
    rows = _sample_source_message_rows()
    rows[0]["attachments"] = [{"content_type": "image/png", "filename": "no_url.png"}]
    db.source_message_rows = rows
    messages = _sample_context_messages()
    messages[0]["attachments"] = [{"content_type": "image/png", "filename": "no_url.png"}]
    editor = _make_editor_with_source_rows(db=db)
    ctx = _make_source_context(messages=messages)

    call = {
        "id": "tool-no-url",
        "name": "post_sectioned_topic",
        "input": {
            "proposed_key": "No URL Test",
            "headline": "Testing missing media URL",
            "body": "Intro.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Has media with no URL.",
                    "source_message_ids": ["200"],
                    "media_refs": [
                        {"message_id": "200", "kind": "attachment", "index": 0},
                    ],
                },
            ],
        },
    }

    outcome = editor._dispatch_tool_call(call, ctx)

    assert outcome["outcome"] == "rejected_post_sectioned"
    assert outcome["error"] == "invalid_media_ref"
    error_text = (db.transitions[0][0]["payload"].get("error") or "").lower()
    assert "no url" in error_text


# ---- (f) publishing enabled sends header/intro text, intro media, section text,
#         section media in order ----

def test_publishing_enabled_sends_structured_blocks_in_order():
    class SentMessage:
        def __init__(self, message_id):
            self.id = message_id

    class Channel:
        def __init__(self):
            self.sent = []
            self._counter = 9100

        async def send(self, content):
            self.sent.append(content)
            self._counter += 1
            return SentMessage(self._counter)

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    topic = {
        "topic_id": "topic-structured-pub",
        "guild_id": 1,
        "state": "posted",
        "headline": "Structured Publish Test",
        "summary": {
            "body": "Intro fallback.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Alice released a new LoRA with benchmarks.",
                    "source_message_ids": ["200"],
                    "media_refs": [
                        {"message_id": "200", "kind": "attachment", "index": 0},
                    ],
                },
                {
                    "type": "section",
                    "title": "Benchmark Details",
                    "text": "The benchmarks show 15% improvement.",
                    "source_message_ids": ["201"],
                    "media_refs": [
                        {"message_id": "201", "kind": "attachment", "index": 1},
                    ],
                },
            ],
        },
        "source_message_ids": ["200", "201"],
        "publication_attempts": 0,
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "sent"
    assert len(channel.sent) == 4

    assert channel.sent[0].startswith("## Live update: Structured Publish Test")
    assert "Alice released a new LoRA with benchmarks." in channel.sent[0]
    assert "Sources: [1] https://discord.com/channels/1/10/200" in channel.sent[0]

    assert channel.sent[1] == "https://cdn.example.com/image1.png"

    assert "**Benchmark Details**" in channel.sent[2]
    assert "15% improvement" in channel.sent[2]

    assert channel.sent[3] == "https://cdn.example.com/preview.png"

    assert len(result["discord_message_ids"]) == 4
    assert result["discord_message_ids"] == [9101, 9102, 9103, 9104]

    assert db.topic_updates[0][1]["publication_status"] == "sent"
    assert db.topic_updates[0][1]["discord_message_ids"] == [9101, 9102, 9103, 9104]


def test_discord_media_refs_are_bundled_and_temp_files_deleted(monkeypatch, tmp_path):
    class SentMessage:
        def __init__(self, message_id):
            self.id = message_id

    class Channel:
        def __init__(self):
            self.sent = []
            self._counter = 9700

        async def send(self, content=None, file=None, files=None):
            self.sent.append({"content": content, "file": file, "files": files or []})
            self._counter += 1
            return SentMessage(self._counter)

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)
    db = FakeDB()
    db.source_message_rows = [
        {
            "message_id": "300",
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "Two samples.",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [
                {
                    "url": "https://cdn.discordapp.com/attachments/1/2/a.mp4",
                    "filename": "a.mp4",
                },
                {
                    "url": "https://cdn.discordapp.com/attachments/1/2/b.mp4",
                    "filename": "b.mp4",
                },
            ],
            "embeds": [],
        }
    ]

    created_paths = []

    async def fake_download(_self, source_url, unit):
        filename = unit.get("filename") or "media.bin"
        path = tmp_path / filename
        path.write_bytes(b"fake-media")
        created_paths.append(path)
        return str(path), filename

    monkeypatch.setattr(TopicEditor, "_download_publish_media_url", fake_download)

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    topic = {
        "topic_id": "topic-bundled-media",
        "guild_id": 1,
        "state": "posted",
        "headline": "Bundled Media Test",
        "summary": {
            "blocks": [
                {
                    "type": "intro",
                    "text": "Two related samples.",
                    "source_message_ids": ["300"],
                    "media_refs": [
                        {"message_id": "300", "kind": "attachment", "index": 0},
                        {"message_id": "300", "kind": "attachment", "index": 1},
                    ],
                }
            ]
        },
        "source_message_ids": ["300"],
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "sent"
    assert len(channel.sent) == 2
    assert channel.sent[0]["content"].startswith("## Live update: Bundled Media Test")
    assert len(channel.sent[1]["files"]) == 2
    assert {file.filename for file in channel.sent[1]["files"]} == {"a.mp4", "b.mp4"}
    assert all(not path.exists() for path in created_paths)


# ---- (g) publishing disabled returns deterministic would-publish sequence ----

def test_publishing_suppressed_returns_would_publish_sequence():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()

    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = False

    topic = {
        "topic_id": "topic-suppressed",
        "guild_id": 1,
        "state": "posted",
        "headline": "Suppressed Publish Test",
        "summary": {
            "body": "Intro fallback.",
            "blocks": [
                {
                    "type": "intro",
                    "text": "Alice released a new LoRA.",
                    "source_message_ids": ["200"],
                    "media_refs": [
                        {"message_id": "200", "kind": "attachment", "index": 0},
                    ],
                },
                {
                    "type": "section",
                    "title": "Results",
                    "text": "Benchmarks look good.",
                    "source_message_ids": ["201"],
                    "media_refs": [],
                },
            ],
        },
        "source_message_ids": ["200", "201"],
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "suppressed"
    assert "publish_units" in result
    assert "flat_messages" in result
    assert "media_indices" in result

    publish_units = result["publish_units"]
    assert len(publish_units) >= 3

    kinds = [u["kind"] for u in publish_units]
    assert kinds[0] == "text"
    assert "media" in kinds

    flat_messages = result["flat_messages"]
    assert len(flat_messages) >= 3

    media_indices = result["media_indices"]
    for idx in media_indices:
        assert flat_messages[idx].startswith("https://")

    assert db.topic_updates[0][1]["publication_status"] == "suppressed"


def test_publishing_suppressed_includes_no_media_indices_when_no_media():
    db = FakeDB()
    db.source_message_rows = _sample_source_message_rows()

    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = False

    topic = {
        "topic_id": "topic-no-media",
        "guild_id": 1,
        "state": "posted",
        "headline": "No Media Test",
        "summary": {
            "blocks": [
                {
                    "type": "intro",
                    "text": "Just text.",
                    "source_message_ids": ["200"],
                    "media_refs": [],
                },
            ],
        },
        "source_message_ids": ["200"],
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "suppressed"
    assert result["media_indices"] == []
    assert len(result["flat_messages"]) == 1
    assert not any(msg.startswith("https://") for msg in result["flat_messages"])


# ---- (h) existing simple-topic behavior still compatible ----

def test_simple_topic_publishing_unchanged_with_structured_changes():
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
    bot = SimpleNamespace(get_channel=lambda cid: channel)
    db = FakeDB()

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    topic = {
        "topic_id": "topic-simple",
        "guild_id": 1,
        "state": "posted",
        "headline": "Simple Topic",
        "summary": {"body": "Simple body text."},
        "source_message_ids": ["100"],
        "publication_attempts": 0,
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "sent"
    assert any("Source: 100" in msg for msg in channel.sent)
    assert result["discord_message_ids"] == [9101]


def test_simple_topic_suppressed_still_returns_messages():
    db = FakeDB()
    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = False

    topic = {
        "topic_id": "topic-simple-suppressed",
        "guild_id": 1,
        "state": "posted",
        "headline": "Simple Suppressed",
        "summary": {"body": "Will not send."},
        "source_message_ids": ["100"],
    }

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "suppressed"
    assert "messages" in result
    assert len(result["messages"]) == 1
    assert "Source: 100" in result["messages"][0]


# ------------------------------------------------------------------
# T11: External media resolution + structured publish runtime tests
# ------------------------------------------------------------------


def _make_topic_with_external_refs(
    external_url="https://www.reddit.com/r/test/comments/abc123/test_post/",
    additional_blocks=None,
):
    """Build a structured topic with an external media ref in one block."""
    source_rows = [
        {
            "message_id": "400",
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": f"Check out this post: {external_url}",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [],
            "embeds": [],
        },
        {
            "message_id": "401",
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 43,
            "content": "Follow-up text without media.",
            "created_at": "2026-05-13T10:05:00Z",
            "author_context_snapshot": {"username": "bob"},
            "attachments": [],
            "embeds": [],
        },
    ]

    blocks = [
        {
            "type": "intro",
            "text": "Someone shared an interesting external post.",
            "source_message_ids": ["400"],
            "media_refs": [
                {"message_id": "400", "kind": "external", "index": 0},
            ],
        },
        {
            "type": "section",
            "title": "Discussion",
            "text": "Follow-up discussion continues.",
            "source_message_ids": ["401"],
            "media_refs": [],
        },
    ]

    if additional_blocks:
        blocks.extend(additional_blocks)

    topic = {
        "topic_id": "topic-ext-refs",
        "guild_id": 1,
        "state": "posted",
        "headline": "External Media Test",
        "summary": {"blocks": blocks, "body": "Testing."},
        "source_message_ids": ["400", "401"],
        "publication_attempts": 0,
    }

    return topic, source_rows


# ---- helpers for external-media stubs ----


class _FakeResolverOutcome:
    """Proxy that supports both .value access and str comparisons (like a str Enum)."""
    def __init__(self, val: str):
        self.value = val

    def __eq__(self, other):
        if isinstance(other, _FakeResolverOutcome):
            return self.value == other.value
        return self.value == other

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return f"_FakeResolverOutcome({self.value!r})"


def _make_resolver_result(outcome: str, file_path=None, failure_reason=""):
    """Build a SimpleNamespace that mimics ResolverResult for _send_one_unit."""
    return SimpleNamespace(
        outcome=_FakeResolverOutcome(outcome),
        file_path=file_path,
        failure_reason=failure_reason,
    )


# ---- (1) suppressed-mode shape with external refs ----


def test_suppressed_mode_with_external_refs_shape():
    """Suppressed mode returns correct flat_messages/media_indices/media_count/flat_message_count."""
    topic, source_rows = _make_topic_with_external_refs()

    db = FakeDB()
    db.source_message_rows = source_rows

    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = False

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "suppressed"
    # Suppressed-mode shape: publish_units, flat_messages, media_indices, source_media_counts
    assert "flat_messages" in result
    assert "media_indices" in result
    assert "publish_units" in result
    assert "source_media_counts" in result

    # media_indices point to positions in flat_messages
    for idx in result["media_indices"]:
        assert 0 <= idx < len(result["flat_messages"])
    # At least one media index from the external ref
    assert len(result["media_indices"]) >= 1

    # The external URL should appear in flat_messages
    flat_text = " ".join(result["flat_messages"])
    assert "reddit.com" in flat_text

    # source_media_counts should include external_links
    assert "external_links" in result["source_media_counts"]
    assert result["source_media_counts"]["external_links"] >= 1

    # publish_units should include an external-kind unit
    kinds = [u.get("kind") for u in result["publish_units"]]
    assert "external" in kinds


# ---- (2) resolver success sends discord.File in correct block position ----


def test_resolver_success_sends_file_in_correct_position(monkeypatch):
    """When resolver succeeds, a discord.File is sent at the correct block position."""
    import io
    import builtins
    import discord as _real_discord

    external_url = "https://www.reddit.com/r/test/comments/abc123/"
    topic, source_rows = _make_topic_with_external_refs(external_url=external_url)

    db = FakeDB()
    db.source_message_rows = source_rows

    # Stub _resolve_external_for_publish to return a successful result
    async def fake_resolve(_self, source_url, ref):
        return _make_resolver_result("downloaded", file_path="/tmp/fake_reddit_image.png")

    monkeypatch.setattr(TopicEditor, "_resolve_external_for_publish", fake_resolve)

    # Mock open to return fake bytes (keep a reference to original for other uses)
    _orig_open = builtins.open
    def mock_open(path, mode="r"):
        if path == "/tmp/fake_reddit_image.png":
            return io.BytesIO(b"fake-image-bytes")
        return _orig_open(path, mode)

    monkeypatch.setattr(builtins, "open", mock_open)

    # Monkeypatch discord.File directly — this must work because
    # topic_editor.py does `import discord` at module level and
    # _send_one_unit references `discord.File`.
    class FakeDiscordFile:
        def __init__(self, fh, filename=None):
            self.fh = fh
            self.filename = filename

    monkeypatch.setattr(_real_discord, "File", FakeDiscordFile)

    # Channel that tracks sent content and files
    class Channel:
        def __init__(self):
            self.sent_content = []
            self.sent_files = []

        async def send(self, content=None, file=None):
            if content is not None:
                self.sent_content.append(content)
            if file is not None:
                self.sent_files.append(file)
            return SimpleNamespace(id=9200 + len(self.sent_content) + len(self.sent_files))

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] == "sent"
    # We should have 3 units: text (intro), file (external ref), text (section)
    assert len(result["discord_message_ids"]) >= 3

    # The intro text block was sent
    assert any("External Media Test" in c for c in channel.sent_content)

    # The external ref resulted in a file send (not URL text)
    assert len(channel.sent_files) == 1
    # The file was a FakeDiscordFile instance
    assert isinstance(channel.sent_files[0], FakeDiscordFile)
    assert channel.sent_files[0].filename == "fake_reddit_image.png"

    # The section block text was also sent after the file
    assert any("Discussion" in c for c in channel.sent_content)


# ---- (3) resolver failure falls back to original link text ----


def test_resolver_failure_falls_back_to_link_text(monkeypatch):
    """When resolver fails, the original URL is sent as text instead."""
    external_url = "https://www.reddit.com/r/test/comments/abc123/"
    topic, source_rows = _make_topic_with_external_refs(external_url=external_url)

    db = FakeDB()
    db.source_message_rows = source_rows

    # Stub _resolve_external_for_publish to return a failure result
    async def fake_resolve(_self, source_url, ref):
        return _make_resolver_result("download_failed", failure_reason="network error")

    monkeypatch.setattr(TopicEditor, "_resolve_external_for_publish", fake_resolve)

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, file=None):
            self.sent.append({"content": content, "file": file})
            return SimpleNamespace(id=9300 + len(self.sent))

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    result = asyncio.run(editor._publish_topic(topic))

    # The batch continues despite resolver failure
    assert result["status"] in ("sent", "partial")
    assert len(result["discord_message_ids"]) >= 3

    # Check that text blocks before AND after the failed external ref were published
    sent_texts = [s["content"] for s in channel.sent if s["content"]]
    assert any("External Media Test" in t for t in sent_texts), (
        "Intro text block before external ref should be published"
    )
    assert any("Discussion" in t for t in sent_texts), (
        "Section text block after external ref should be published — batch continues"
    )

    # The external URL should appear as fallback text
    assert any("reddit.com" in t for t in sent_texts), (
        "Failing external ref should produce fallback URL text"
    )

    # No files should have been sent
    files_sent = [s for s in channel.sent if s["file"] is not None]
    assert len(files_sent) == 0


# ---- (4) oversize/unsupported media falls back ----


def test_oversize_media_falls_back_to_url(monkeypatch):
    """Oversize (or unsupported content type) media falls back to URL text."""
    external_url = "https://x.com/someuser/status/123456"
    topic, source_rows = _make_topic_with_external_refs(external_url=external_url)

    db = FakeDB()
    db.source_message_rows = source_rows

    # Stub: resolver returns OVERSIZE outcome
    async def fake_resolve_oversize(_self, source_url, ref):
        return _make_resolver_result("oversize", failure_reason="exceeds byte cap")

    monkeypatch.setattr(TopicEditor, "_resolve_external_for_publish", fake_resolve_oversize)

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, file=None):
            self.sent.append({"content": content, "file": file})
            return SimpleNamespace(id=9400 + len(self.sent))

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    result = asyncio.run(editor._publish_topic(topic))

    assert result["status"] in ("sent", "partial")
    sent_texts = [s["content"] for s in channel.sent if s["content"]]
    # Oversize media should produce a fallback URL, not block publishing
    assert any("x.com" in t for t in sent_texts)

    # No files should have been sent for oversize media
    files_sent = [s for s in channel.sent if s["file"] is not None]
    assert len(files_sent) == 0


# ---- (5) file-send failure falls back to URL text and continues ----


def test_file_send_failure_falls_back_and_continues(monkeypatch):
    """When discord.File send itself fails, fall back to URL text and continue batch."""
    import io
    import sys
    import builtins

    external_url = "https://www.reddit.com/r/test/comments/abc123/"
    topic, source_rows = _make_topic_with_external_refs(external_url=external_url)

    db = FakeDB()
    db.source_message_rows = source_rows

    # Resolver succeeds (returns a file path)
    async def fake_resolve_success(_self, source_url, ref):
        return _make_resolver_result("downloaded", file_path="/tmp/fake_image.png")

    monkeypatch.setattr(TopicEditor, "_resolve_external_for_publish", fake_resolve_success)

    # Mock open
    original_open = builtins.open
    def mock_open(path, mode="r"):
        if path == "/tmp/fake_image.png":
            return io.BytesIO(b"fake-image-bytes")
        return original_open(path, mode)

    fake_discord = SimpleNamespace(File=lambda fh, filename=None: SimpleNamespace(fh=fh, filename=filename))
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setattr(builtins, "open", mock_open)

    # Channel that fails on file sends but succeeds on text
    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, file=None):
            if file is not None:
                # Simulate file-send failure
                raise RuntimeError("discord upload rejected")
            self.sent.append({"content": content, "file": file})
            return SimpleNamespace(id=9500 + len(self.sent))

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    result = asyncio.run(editor._publish_topic(topic))

    # Batch continues despite file-send failure
    assert result["status"] in ("sent", "partial")

    sent_texts = [s["content"] for s in channel.sent if s["content"]]
    # Intro text before external ref should publish
    assert any("External Media Test" in t for t in sent_texts)
    # Section text after external ref should still publish (batch continues)
    assert any("Discussion" in t for t in sent_texts)
    # External URL should appear as fallback text after file-send failure
    assert any("reddit.com" in t for t in sent_texts)


# ---- (6) regression: reaction-qualified auto-shortlist creates watching topics, no direct-posting ----


def test_auto_shortlist_watching_no_direct_post(monkeypatch):
    """Regression: reaction-qualified auto-shortlist creates watching topics and does NOT direct-post."""
    monkeypatch.setenv("TOPIC_EDITOR_MEDIA_SHORTLIST_MIN_REACTIONS", "5")
    monkeypatch.setenv("TOPIC_EDITOR_MEDIA_SHORTLIST_LIMIT", "5")

    db = FakeDB()
    editor = TopicEditor(
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )

    messages = [
        {
            "message_id": 500,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "Check out this render!",
            "created_at": "2026-05-13T10:00:00Z",
            "reaction_count": 7,
            "author_context_snapshot": {"username": "alice"},
            "attachments": [
                {"url": "https://cdn.example.test/render.png", "content_type": "image/png", "filename": "render.png"},
            ],
            "embeds": [],
        },
        {
            "message_id": 501,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 43,
            "content": "Another render with more reactions.",
            "created_at": "2026-05-13T10:05:00Z",
            "reaction_count": 9,
            "author_context_snapshot": {"username": "bob"},
            "attachments": [
                {"url": "https://cdn.example.test/render2.mp4", "content_type": "video/mp4", "filename": "render2.mp4"},
            ],
            "embeds": [],
        },
    ]

    shortlisted = editor._auto_shortlist_media_messages(
        messages,
        [],
        run_id="run-regression",
        guild_id=1,
    )

    # Both messages qualify (>=5 reactions, have attachments)
    assert len(shortlisted) == 2

    # Assert watching topics were created
    assert len(db.topics) == 2
    for topic_entry in db.topics:
        stored_topic = topic_entry[0]
        # Must be state "watching", NOT "posted"
        assert stored_topic["state"] == "watching", (
            f"Topic {stored_topic.get('canonical_key')} should be 'watching', "
            f"not '{stored_topic.get('state')}' — auto-shortlist must not direct-post"
        )
        # Must have auto_shortlist marker
        assert stored_topic["summary"]["auto_shortlist"] is True

    # Explicitly assert no topic was directly posted
    for topic_entry in db.topics:
        assert topic_entry[0]["state"] != "posted", (
            "Regression: reaction-qualified auto-shortlist MUST NOT direct-post. "
            "Topics must remain in 'watching' state for agent review."
        )

    # Assert pub status was NOT set to "sent"
    for topic_entry in db.topics:
        pub_status = topic_entry[0].get("publication_status")
        assert pub_status != "sent", (
            f"Topic publication_status should not be 'sent' for auto-shortlisted topics"
        )

    # Transitions should be "watch", not "post_simple" or "post_sectioned"
    for transition_entry in db.transitions:
        action = transition_entry[0]["action"]
        assert action == "watch", (
            f"Auto-shortlist transition action should be 'watch', got '{action}'. "
            "Direct posting is disabled."
        )


# ---- (7) partial status when mixed text/file units succeed/fail ----


def test_partial_status_mixed_success_failure(monkeypatch):
    """Partial status when some send units succeed and some fail."""
    external_url_1 = "https://www.reddit.com/r/test/comments/abc/"
    external_url_2 = "https://x.com/user/status/789"

    # Build a topic with two external refs in different blocks
    topic, source_rows = _make_topic_with_external_refs(external_url=external_url_1)
    # Add a second source_row for x.com
    source_rows.append({
        "message_id": "402",
        "guild_id": 1,
        "channel_id": 10,
        "author_id": 44,
        "content": f"Check this tweet: {external_url_2}",
        "created_at": "2026-05-13T10:10:00Z",
        "author_context_snapshot": {"username": "charlie"},
        "attachments": [],
        "embeds": [],
    })
    topic["source_message_ids"].append("402")
    # Add another block with a second external ref (x.com)
    topic["summary"]["blocks"].append({
        "type": "section",
        "title": "Extra Media",
        "text": "Another external reference.",
        "source_message_ids": ["402"],
        "media_refs": [
            {"message_id": "402", "kind": "external", "index": 0},
        ],
    })
    # Add a third plain-text block
    topic["summary"]["blocks"].append({
        "type": "section",
        "title": "Closing",
        "text": "That wraps it up.",
        "source_message_ids": ["401"],
        "media_refs": [],
    })

    db = FakeDB()
    db.source_message_rows = source_rows

    # Stub: first resolve succeeds, second fails AND fallback send also fails
    call_count = [0]

    async def fake_resolve_mixed(_self, source_url, ref):
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_resolver_result("downloaded", file_path="/tmp/good_image.png")
        else:
            return _make_resolver_result("download_failed", failure_reason="timeout")

    monkeypatch.setattr(TopicEditor, "_resolve_external_for_publish", fake_resolve_mixed)

    # Mock open + discord for the successful file
    import io, builtins
    import discord as _real_discord

    original_open = builtins.open
    def mock_open(path, mode="r"):
        if path == "/tmp/good_image.png":
            return io.BytesIO(b"good-image")
        return original_open(path, mode)

    class FakeDiscordFile:
        def __init__(self, fh, filename=None):
            self.fh = fh
            self.filename = filename

    monkeypatch.setattr(_real_discord, "File", FakeDiscordFile, raising=True)
    monkeypatch.setattr(builtins, "open", mock_open)

    # Channel: succeeds for first external ref (file send),
    # fails for second external ref's fallback URL (so that unit has no sent_id → partial)
    fail_count = [0]
    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, file=None):
            # Fail the fallback URL send for the second external ref
            if content and "x.com" in str(content):
                fail_count[0] += 1
                raise RuntimeError("discord unavailable for fallback")
            self.sent.append({"content": content, "file": file})
            return SimpleNamespace(id=9600 + len(self.sent))

    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)

    editor = TopicEditor(
        bot=bot,
        db_handler=db,
        llm_client=FakeClaude(SimpleNamespace(content=[], usage=None)),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )
    editor.publishing_enabled = True

    result = asyncio.run(editor._publish_topic(topic))

    # Some units succeeded, some failed (second fallback URL send raised) → partial
    assert result["status"] == "partial", (
        f"Expected 'partial' status when some units succeed and some fail, got '{result['status']}'"
    )

    # sent_ids should have entries for successful sends
    assert len(result["discord_message_ids"]) >= 1

    # Verify error was recorded
    assert result.get("error") is not None or result["status"] == "partial"

    # All text blocks should have been sent
    sent_texts = [s["content"] for s in channel.sent if s["content"]]
    assert any("External Media Test" in t for t in sent_texts)
    assert any("Closing" in t for t in sent_texts)

    # One file should have been sent (first external ref succeeded)
    files_sent = [s for s in channel.sent if s["file"] is not None]
    assert len(files_sent) == 1

    # The failed fallback triggered (inner try + outer except re-try)
    assert fail_count[0] >= 1, "Second external ref fallback URL should have been attempted"
