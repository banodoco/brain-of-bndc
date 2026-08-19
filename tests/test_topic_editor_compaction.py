"""Tests for TopicEditor threshold-based conversation compaction.

Covers the `_compact_conversation` helper, boundary-based firing, a multi-turn
run that would previously blow the token cap but completes after compaction,
the graceful-finalize nudge, and the summariser_cog default model.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

import src.features.summarising.summariser_cog as summariser_cog_module
from src.features.summarising.topic_editor import TopicEditor


# --------------------------------------------------------------------------
# Helpers shared across tests
# --------------------------------------------------------------------------

def _tool_block(tool_id, name, **input_):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _watch_block(tool_id, proposed_key):
    return _tool_block(
        tool_id,
        "watch_topic",
        proposed_key=proposed_key,
        headline=proposed_key,
        why_interesting="Worth tracking.",
        source_message_ids=["100"],
    )


def _search_block(tool_id="tool-search"):
    return _tool_block(tool_id, "search_topics", query="anything")


def _finalize_block(tool_id="tool-finalize"):
    return _tool_block(
        tool_id,
        "finalize_run",
        overall_reasoning=(
            "The scripted agent has reviewed the source window, recorded the "
            "accepted decisions, and there is nothing further worth publishing "
            "or watching right now. Closing the run cleanly with the overall "
            "editorial reasoning as required."
        ),
        topics_considered=["alpha"],
    )


def _source_message(message_id, channel_id=10, content="I shipped a new LoRA test."):
    return {
        "message_id": message_id,
        "guild_id": 1,
        "channel_id": channel_id,
        "channel_name": "show-and-tell",
        "author_id": 42,
        "content": content,
        "created_at": "2026-05-13T10:00:00Z",
        "author_context_snapshot": {"username": "alice"},
        "reaction_count": 2,
        "attachments": [],
        "embeds": [],
    }


class ScriptedMessages:
    """Fake Anthropic messages.create with context-sensitive token accounting.

    Input tokens scale with the size of the message list so compaction (which
    drops the huge static source dump) actually reduces the per-turn cost the
    way it does in production.
    """

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        messages = kwargs.get("messages", [])
        input_tokens = self._tokens_for(messages)
        content = self.contents.pop(0)
        if not isinstance(content, list):
            content = [content]
        return SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=0),
        )

    @staticmethod
    def _tokens_for(messages):
        has_dump = any(
            isinstance(message.get("content"), list)
            and message.get("content")
            and isinstance(message["content"][0], dict)
            and str(message["content"][0].get("text", "")).startswith("{'source_messages'")
            for message in messages
        )
        # Static dump re-sent every turn is ~40K; a compacted context is ~2K.
        return (40_000 if has_dump else 2_000) + 1_000 * len(messages)


class ScriptedClaude:
    def __init__(self, contents):
        self.client = SimpleNamespace(messages=ScriptedMessages(contents))


class FakeDB:
    def __init__(self):
        self.completed = []
        self.failed = []
        self.transitions = []
        self.topics = []
        self.sources = []
        self.aliases = []
        self.active_topics = []
        self.source_message_rows = []
        self.checkpoints = []
        self.topic_updates = []

    def get_topic_editor_checkpoint(self, checkpoint_key, environment="prod"):
        return {
            "checkpoint_key": checkpoint_key,
            "guild_id": 1,
            "channel_id": 2,
            "last_message_id": 99,
        }

    def acquire_topic_editor_run(self, run, environment="prod"):
        return {"run_id": "run-1"}

    def get_archived_messages_after_checkpoint(
        self, checkpoint=None, guild_id=None, channel_ids=None, limit=200, exclude_author_ids=None
    ):
        return list(self.source_message_rows)

    def get_topics(self, guild_id=None, states=None, limit=100, environment="prod"):
        return self.active_topics

    def get_topic_aliases(self, guild_id=None, environment="prod"):
        return []

    def search_topic_editor_topics(self, query, guild_id=None, environment="prod", state_filter=None, hours_back=72, limit=10):
        return []

    def get_topic_editor_source_messages(self, message_ids, guild_id=None, environment="prod", limit=50):
        wanted = {str(message_id) for message_id in message_ids or []}
        return [row for row in self.source_message_rows if str(row.get("message_id")) in wanted][:limit]

    def get_topic_transitions_by_tool_call_ids(self, run_id, tool_call_ids, environment="prod"):
        return {}

    def upsert_topic(self, topic, environment="prod"):
        self.topics.append((topic, environment))
        return {"topic_id": f"topic-{len(self.topics)}", **topic}

    def update_topic(self, topic_id, updates, guild_id=None, environment="prod"):
        self.topic_updates.append((topic_id, updates, guild_id, environment))
        return {"topic_id": topic_id, **updates}

    def add_topic_source(self, source, environment="prod"):
        self.sources.append((source, environment))
        return {"topic_source_id": "source-1"}

    def upsert_topic_alias(self, alias, environment="prod"):
        self.aliases.append((alias, environment))
        return {"alias_id": "alias-1"}

    def store_topic_transition(self, transition, environment="prod"):
        self.transitions.append((transition, environment))
        return {"transition_id": "transition-1"}

    def upsert_topic_editor_checkpoint(self, checkpoint, environment="prod"):
        self.checkpoints.append((checkpoint, environment))
        return checkpoint

    def complete_topic_editor_run(self, run_id, updates, guild_id=None, environment="prod"):
        self.completed.append((run_id, updates, guild_id, environment))
        return {"run_id": run_id, "status": "completed"}

    def fail_topic_editor_run(self, run_id, error_message, updates=None, guild_id=None, environment="prod"):
        self.failed.append((run_id, error_message, updates, guild_id, environment))
        return {"run_id": run_id, "status": "failed"}


def _make_editor(db, contents):
    return TopicEditor(
        db_handler=db,
        llm_client=ScriptedClaude(contents),
        guild_id=1,
        live_channel_id=2,
        environment="prod",
    )


# --------------------------------------------------------------------------
# 1. _compact_conversation helper
# --------------------------------------------------------------------------

def test_compact_conversation_preserves_last_two_turns_and_drops_initial_payload():
    db = FakeDB()
    db.source_message_rows = [
        _source_message(100, content="I shipped a new LoRA test."),
        _source_message(101, content="Community benchmark results are in."),
    ]
    editor = _make_editor(db, [])
    initial_payload = {
        "source_messages": [{"message_id": 100, "content": "I shipped a new LoRA test."}],
        "evidence_shelf": [],
        "active_topics": [],
        "auto_shortlisted_media": [],
    }
    messages_arg = [
        {"role": "user", "content": [{"type": "text", "text": repr(initial_payload)}]},
        {"role": "assistant", "content": [{"type": "text", "text": "reasoning one"}, {"type": "tool_use", "id": "t1", "name": "watch_topic", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "tool=watch_topic status=accepted topic_id=topic-1"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "reasoning two"}, {"type": "tool_use", "id": "t2", "name": "search_topics", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "tool=search_topics status=ok result=[]"}]},
    ]
    dispatcher_context = {
        "run_id": "run-1",
        "guild_id": 1,
        "messages": db.source_message_rows,
        "seen_tool_call_ids": set(),
    }
    outcomes = [
        {"tool_call_id": "t1", "tool": "watch_topic", "outcome": "accepted", "action": "watch", "topic_id": "topic-1"},
    ]

    compacted = editor._compact_conversation(
        messages_arg,
        initial_payload,
        dispatcher_context,
        outcomes,
        db.source_message_rows,
        turn_count=5,
    )

    assert len(compacted) == 3
    # Last 2 items of the old conversation preserved verbatim.
    assert compacted[1] == messages_arg[-2]
    assert compacted[2] == messages_arg[-1]
    # Leading recap replaces the initial payload dump.
    recap = compacted[0]["content"][0]["text"]
    assert compacted[0]["role"] == "user"
    assert repr(initial_payload) not in recap
    assert "Total source messages: 2" in recap
    assert "Channel tally: show-and-tell=2" in recap
    assert "Top items by reactions:" in recap
    assert "- reactions=2 channel=show-and-tell author=alice: I shipped a new LoRA test." in recap
    assert "--- Decisions so far ---" in recap
    assert "- watch topic_id=topic-1" in recap
    assert "re-read any specific source in full before deciding on it." in recap


# --------------------------------------------------------------------------
# 2. Boundary-based firing
# --------------------------------------------------------------------------

def test_compaction_fires_at_boundary_but_not_before(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [_search_block(), _watch_block("tool-watch-1", "alpha"), _search_block(), _finalize_block()]
    editor = _make_editor(db, contents)

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    compactions = db.completed[0][1]["metadata"]["compactions"]
    assert len(compactions) == 1
    assert compactions[0]["turn"] == 2
    # T1 = 41K, T2 = 84K; boundary crossed at turn 2.
    assert compactions[0]["cumulative_tokens"] == 84000
    assert compactions[0]["context_size_before"] == 43000


def test_compaction_never_fires_with_high_threshold(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "10000000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "2")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [_search_block(), _watch_block("tool-watch-1", "alpha"), _search_block(), _finalize_block()]
    editor = _make_editor(db, contents)

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    assert db.completed[0][1]["metadata"].get("compactions") is None


# --------------------------------------------------------------------------
# 3. Multi-turn run completes thanks to compaction
# --------------------------------------------------------------------------

def _run_once_with_threshold():
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _watch_block("tool-watch-1", "alpha"),
        _watch_block("tool-watch-2", "beta"),
        _watch_block("tool-watch-1", "alpha"),  # replay after compaction -> idempotent
        _search_block(),
        _search_block(),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))
    return db, result


def test_multi_turn_run_completes_with_compaction_but_fails_without(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_MAX_TOKENS", "150000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")

    # With a low threshold, compaction shrinks later turns so the run survives
    # to its finalize call and closes cleanly.
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    compacting_db, result = _run_once_with_threshold()
    assert result["status"] == "completed"
    # watch-1 (turn 2) and watch-2 (turn 3) each created a topic; the replayed
    # watch-1 after compaction was idempotent, so nothing was double-written.
    assert len(compacting_db.topics) == 2
    assert [outcome["outcome"] for outcome in result["outcomes"]].count("idempotent_replay") == 1
    compactions = compacting_db.completed[0][1]["metadata"]["compactions"]
    assert len(compactions) == 1

    # With a high threshold (no compaction), the same script blows the cap.
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "10000000")
    _, no_compact_result = _run_once_with_threshold()
    assert no_compact_result["status"] == "failed"


# --------------------------------------------------------------------------
# 4. Graceful-finalize nudge near the cap after compaction
# --------------------------------------------------------------------------

def test_graceful_finalize_nudge_appears_near_cap_after_compaction(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_MAX_TOKENS", "0")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COST_USD", "0.0145")
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [_search_block(), _watch_block("tool-watch-1", "alpha"), _search_block(), _finalize_block()]
    editor = _make_editor(db, contents)

    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    nudge_text = (
        "You are near the token budget. If your work is sufficiently complete, "
        "call finalize_run now with your overall reasoning; do not start new investigations."
    )
    llm_calls = editor.llm_client.client.messages.calls
    # The nudge is appended to the context seen by the turn that calls finalize.
    last_messages = llm_calls[-1]["messages"]
    found = any(
        isinstance(message.get("content"), list)
        and any(isinstance(block, dict) and block.get("text") == nudge_text for block in message["content"])
        for message in last_messages
    )
    assert found, f"nudge not found in last call messages: {last_messages}"


# --------------------------------------------------------------------------
# 5. summariser_cog default model
# --------------------------------------------------------------------------

def test_summariser_cog_default_model_is_deepseek_v4_flash(monkeypatch):
    monkeypatch.delenv("TOPIC_EDITOR_MODEL", raising=False)
    monkeypatch.delenv("TOPIC_EDITOR_LLM_CLIENT", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    summariser_cog_module._build_topic_editor_llm_client()

    assert os.environ.get("TOPIC_EDITOR_MODEL") == "deepseek-v4-flash"


# --------------------------------------------------------------------------
# 6. BLOCKER 1 — semantic per-run dedup with a FRESH tool_call_id
# --------------------------------------------------------------------------

def _post_simple_block(tool_id, proposed_key):
    return _tool_block(
        tool_id,
        "post_simple_topic",
        proposed_key=proposed_key,
        headline=proposed_key,
        body="A concise source-backed update worth posting.",
        source_message_ids=["100"],
    )


def test_semantic_dedup_post_fresh_tool_id_no_duplicate(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    monkeypatch.setenv("TOPIC_EDITOR_LEGACY_POST_MODE", "direct")
    monkeypatch.setenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "false")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _post_simple_block("tool-post-1", "alpha"),
        # Compaction drops the first post's tool_call_id; the model re-issues the
        # SAME canonical key with a FRESH tool_call_id — must not create/publish again.
        _post_simple_block("tool-post-2", "alpha"),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    # No duplicate topic row for alpha.
    assert len(db.topics) == 1
    # The fresh-id replay resolved as idempotent, referencing the existing topic.
    replays = [o for o in result["outcomes"] if o.get("outcome") == "idempotent_replay"]
    assert len(replays) == 1
    assert replays[0].get("topic_id")
    # Only one publish result (no duplicate publish).
    publish_results = db.completed[0][1]["metadata"].get("publish_results") or []
    assert len(publish_results) == 1


def test_semantic_dedup_watch_fresh_tool_id_no_duplicate(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _watch_block("tool-watch-1", "alpha"),
        _watch_block("tool-watch-2", "beta"),
        _watch_block("tool-watch-3", "alpha"),  # fresh tool_call_id for an existing key
        _search_block(),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    # alpha + beta, no duplicate alpha.
    assert len(db.topics) == 2
    replays = [o for o in result["outcomes"] if o.get("outcome") == "idempotent_replay"]
    assert len(replays) == 1


def test_semantic_dedup_watch_then_post_publishes(monkeypatch):
    """BLOCKER: WATCH then POST the same key in one run must NOT be replayed.

    The watch→post editorial flow is a legitimate state transition, so the post
    must run the normal post path (upserting the existing watched topic) and
    publish — not be swallowed by the per-run semantic dedup guard."""
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    monkeypatch.setenv("TOPIC_EDITOR_LEGACY_POST_MODE", "direct")
    monkeypatch.setenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "false")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _watch_block("tool-watch-1", "alpha"),
        _post_simple_block("tool-post-1", "alpha"),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    # The post must NOT be replayed as idempotent.
    replays = [o for o in result["outcomes"] if o.get("outcome") == "idempotent_replay"]
    assert replays == []
    # The post is accepted via the normal post path.
    accepted_posts = [
        o for o in result["outcomes"]
        if o.get("tool") == "post_simple_topic" and o.get("outcome") == "accepted"
    ]
    assert len(accepted_posts) == 1
    # The topic is published (exactly one publish result).
    publish_results = db.completed[0][1]["metadata"].get("publish_results") or []
    assert len(publish_results) == 1


def test_semantic_dedup_canonical_key_prefix_alias_collision(monkeypatch):
    """BLOCKER: a fresh-ID post for `alpha-v2` must collide with `alpha` created
    earlier THIS run (canonical-key prefix match) — a collision, not a second
    create+publish."""
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    monkeypatch.setenv("TOPIC_EDITOR_LEGACY_POST_MODE", "direct")
    monkeypatch.setenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "false")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _post_simple_block("tool-post-1", "alpha"),
        _post_simple_block("tool-post-2", "alpha-v2"),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    # Only ONE topic row created (alpha). alpha-v2 collided before any upsert.
    assert len(db.topics) == 1
    # The second post was rejected as a topic_collision, not accepted/published.
    collision_rejects = [
        o for o in result["outcomes"]
        if o.get("tool") == "post_simple_topic" and o.get("error") == "topic_collision"
    ]
    assert len(collision_rejects) == 1
    # Only one publish result (for alpha).
    publish_results = db.completed[0][1]["metadata"].get("publish_results") or []
    assert len(publish_results) == 1
# --------------------------------------------------------------------------
# 7. BLOCKER 2 — pair-safe tail + one-shot nudge
# --------------------------------------------------------------------------

def test_compact_post_nudge_preserves_pair_safe_tail():
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    editor = _make_editor(db, [])
    initial_payload = {
        "source_messages": [{"message_id": 100, "content": "I shipped a new LoRA test."}],
        "evidence_shelf": [],
        "active_topics": [],
        "auto_shortlisted_media": [],
    }
    nudge_text = (
        "You are near the token budget. If your work is sufficiently complete, "
        "call finalize_run now with your overall reasoning; do not start new investigations."
    )
    messages_arg = [
        {"role": "user", "content": [{"type": "text", "text": repr(initial_payload)}]},
        {"role": "assistant", "content": [{"type": "text", "text": "reasoning one"}, {"type": "tool_use", "id": "t1", "name": "watch_topic", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "tool=watch_topic status=accepted topic_id=topic-1"}]},
        # Trailing nudge text user message must NOT orphan the tool_result above.
        {"role": "user", "content": [{"type": "text", "text": nudge_text}]},
    ]
    dispatcher_context = {
        "run_id": "run-1",
        "guild_id": 1,
        "messages": db.source_message_rows,
        "seen_tool_call_ids": set(),
    }

    compacted = editor._compact_conversation(
        messages_arg,
        initial_payload,
        dispatcher_context,
        [],
        db.source_message_rows,
        turn_count=5,
    )

    # The preserved tail must start with the assistant tool_use, never a tool_result.
    assert compacted[0]["role"] == "user"
    assert compacted[1]["role"] == "assistant"
    assert any(isinstance(b, dict) and b.get("type") == "tool_use" for b in compacted[1]["content"])
    assert compacted[2]["role"] == "user"
    assert compacted[2]["content"][0]["type"] == "tool_result"
    # The trailing nudge text user message is preserved after the tool_result.
    assert compacted[3]["role"] == "user"
    assert compacted[3]["content"][0]["text"] == nudge_text
    assert len(compacted) == 4


def test_graceful_finalize_nudge_is_one_shot(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_MAX_TOKENS", "0")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COST_USD", "0.0145")
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "1")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _watch_block("tool-w1", "alpha"),
        _search_block(),
        _search_block(),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    nudge_text = (
        "You are near the token budget. If your work is sufficiently complete, "
        "call finalize_run now with your overall reasoning; do not start new investigations."
    )
    # The nudge may be re-sent inside a single context, but never appended twice.
    max_in_one_call = 0
    for call in editor.llm_client.client.messages.calls:
        count = sum(
            1 for message in call["messages"]
            if isinstance(message.get("content"), list)
            and any(isinstance(b, dict) and b.get("text") == nudge_text for b in message["content"])
        )
        max_in_one_call = max(max_in_one_call, count)
    assert max_in_one_call == 1


def test_last_tool_use_assistant_index_finds_deepseek_openai_assistant_message():
    """SHOULD-FIX: DeepSeek stores the assistant turn inside an
    ``openai_assistant_message`` block. The pair-safe tail must recognize a
    ``tool_use`` nested in that block's ``message.content`` so compaction keeps
    the last DeepSeek assistant turn instead of dropping it (and orphaning its
    ``tool_result``)."""
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    editor = _make_editor(db, [])
    messages_arg = [
        {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "openai_assistant_message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "reasoning"},
                            {"type": "tool_use", "id": "t-deepseek", "name": "watch_topic", "input": {}},
                        ],
                    },
                }
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-deepseek", "content": "ok"}]},
    ]

    index = TopicEditor._last_tool_use_assistant_index(messages_arg)
    assert index == 1

    compacted = editor._compact_conversation(
        messages_arg,
        {},
        {"run_id": "run-1", "guild_id": 1, "messages": db.source_message_rows, "seen_tool_call_ids": set()},
        [],
        db.source_message_rows,
        turn_count=3,
    )
    # Preserved tail starts at the DeepSeek assistant turn, not the orphaned
    # tool_result.
    assert compacted[1]["role"] == "assistant"
    assert compacted[1]["content"][0]["type"] == "openai_assistant_message"
    assert compacted[2]["role"] == "user"
    assert compacted[2]["content"][0]["type"] == "tool_result"
# --------------------------------------------------------------------------
# 8. SHOULD-FIX 3 — compaction is boundary-spaced, not consecutive
# --------------------------------------------------------------------------

def test_compaction_is_boundary_spaced_not_consecutive(monkeypatch):
    monkeypatch.setenv("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", "60000")
    monkeypatch.setenv("TOPIC_EDITOR_MAX_COMPACTIONS", "2")
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    contents = [
        _search_block(),
        _watch_block("tool-w1", "alpha"),
        _search_block(), _search_block(), _search_block(),
        _search_block(), _search_block(), _search_block(), _search_block(),
        _watch_block("tool-w2", "beta"),
        _search_block(),
        _finalize_block(),
    ]
    editor = _make_editor(db, contents)
    result = asyncio.run(editor.run_once("manual"))

    assert result["status"] == "completed"
    compactions = db.completed[0][1]["metadata"]["compactions"]
    assert len(compactions) == 2
    turns = [c["turn"] for c in compactions]
    # First compaction at turn 2; the second must NOT fire on the very next turn —
    # it waits until cumulative advances by another full threshold.
    assert turns == [2, 8]


# --------------------------------------------------------------------------
# 9. First-turn / very-low-threshold compaction drops the initial dump
# --------------------------------------------------------------------------

def test_compact_first_turn_drops_initial_dump():
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    editor = _make_editor(db, [])
    initial_payload = {
        "source_messages": [{"message_id": 100, "content": "I shipped a new LoRA test."}],
        "evidence_shelf": [],
        "active_topics": [],
        "auto_shortlisted_media": [],
    }
    messages_arg = [
        {"role": "user", "content": [{"type": "text", "text": repr(initial_payload)}]},
    ]
    dispatcher_context = {
        "run_id": "run-1",
        "guild_id": 1,
        "messages": db.source_message_rows,
        "seen_tool_call_ids": set(),
    }

    compacted = editor._compact_conversation(
        messages_arg,
        initial_payload,
        dispatcher_context,
        [],
        db.source_message_rows,
        turn_count=1,
    )

    # With no assistant tool_use yet, only the recap survives — the static dump is dropped.
    assert len(compacted) == 1
    assert compacted[0]["role"] == "user"
    recap = compacted[0]["content"][0]["text"]
    assert repr(initial_payload) not in recap
    assert "Total source messages: 1" in recap


# --------------------------------------------------------------------------
# 10. SHOULD-FIX 4 — recap carries active topics, rejections, created sources
# --------------------------------------------------------------------------

def test_compaction_recap_includes_active_topics_rejections_and_created_sources():
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    editor = _make_editor(db, [])
    dispatcher_context = {
        "run_id": "run-1",
        "guild_id": 1,
        "messages": db.source_message_rows,
        "seen_tool_call_ids": set(),
        "active_topics": [
            {"canonical_key": "existing-watch", "state": "watching", "headline": "An existing watch worth tracking"},
        ],
        "created_topic_keys": {
            "alpha": {"canonical_key": "alpha", "topic_id": "topic-9", "state": "posted", "source_message_ids": ["100", "101"]},
        },
        "created_topics": [
            {"canonical_key": "alpha", "topic_id": "topic-9", "state": "posted", "source_message_ids": ["100", "101"]},
        ],
    }
    outcomes = [
        {"tool_call_id": "t1", "tool": "watch_topic", "outcome": "accepted", "action": "watch", "topic_id": "topic-8", "canonical_key": "alpha"},
        {"tool_call_id": "t2", "tool": "post_simple_topic", "outcome": "rejected_post_simple", "action": "rejected_post_simple", "canonical_key": "dup-post", "error": "post_simple_requires_single_author_and_one_or_two_sources"},
    ]

    recap = editor._compaction_recap_text(
        dispatcher_context=dispatcher_context,
        outcomes=outcomes,
        messages=db.source_message_rows,
    )

    assert "--- Active / watching topics ---" in recap
    assert "canonical_key=existing-watch" in recap
    assert "--- Decisions so far ---" in recap
    assert "REJECTED rejected_post_simple reason=post_simple_requires_single_author_and_one_or_two_sources" in recap
    assert "--- Topics created this run ---" in recap
    assert "canonical_key=alpha topic_id=topic-9" in recap
    assert "sources=100,101" in recap


def test_compaction_recap_preserves_open_draft_card_bodies():
    """Regression: post-compaction the model loses the create/edit tool results;
    the recap must reproduce nonterminal drafts' card bodies verbatim so the
    agent can keep editing instead of abandoning drafts it can no longer see."""
    db = FakeDB()
    db.source_message_rows = [_source_message(100)]
    editor = _make_editor(db, [])
    dispatcher_context = {
        "run_id": "run-1",
        "guild_id": 1,
        "messages": db.source_message_rows,
        "seen_tool_call_ids": set(),
        "drafts": {
            "draft-abc": {
                "draft_id": "draft-abc",
                "status": "drafting",
                "draft_json": {
                    "headline": "Sigma shift fixes turbo audio",
                    "topic_key": "sigma-shift-audio-fix",
                    "cards": [
                        {
                            "angle": "What changed",
                            "body": "MrWeaz pinned the fix to shift_video 12 / shift_audio 3 on the 600-EMA build [1][2].",
                            "media_ids": [],
                        }
                    ],
                },
            },
            "draft-done": {
                "draft_id": "draft-done",
                "status": "submitted",
                "draft_json": {
                    "headline": "Already submitted",
                    "cards": [{"angle": "x", "body": "should NOT appear in recap"}],
                },
            },
        },
    }

    recap = editor._compaction_recap_text(
        dispatcher_context=dispatcher_context,
        outcomes=[],
        messages=db.source_message_rows,
    )

    assert "--- Drafts in progress (content preserved verbatim below) ---" in recap
    assert "draft `draft-abc` status=drafting headline='Sigma shift fixes turbo audio'" in recap
    assert "shift_video 12 / shift_audio 3 on the 600-EMA build [1][2]" in recap
    # terminal drafts are excluded — never re-exposed as editable
    assert "draft-done" not in recap
    assert "Already submitted" not in recap


def test_invoke_anthropic_times_out_on_hanging_provider(monkeypatch):
    """A hung provider call must raise TimeoutError so run_once can fail the run."""
    import asyncio as _asyncio
    from types import SimpleNamespace as _NS
    from src.features.summarising.topic_editor import TopicEditor

    class _HangingClient:
        async def generate_chat_completion(self, *a, **k):
            await _asyncio.sleep(60)

    class _FakeDB:
        server_config = _NS(bndc_guild_id=1, get_server_field=lambda *a, **k: None,
                            get_server=lambda *a, **k: None,
                            get_first_server_with_field=lambda *a, **k: None,
                            resolve_guild_id=lambda *a, **k: 1)
        def get_topics(self, *a, **k): return []
        def get_topic_aliases(self, *a, **k): return []
        def get_archived_messages_after_checkpoint(self, *a, **k): return []
        def __getattr__(self, name): return lambda *a, **k: []

    monkeypatch.setenv("TOPIC_EDITOR_LLM_TIMEOUT_SECONDS", "0.2")
    editor = TopicEditor(db_handler=_FakeDB(), llm_client=_HangingClient(), guild_id=1, live_channel_id=2, environment="prod")
    with pytest.raises(_asyncio.TimeoutError):
        _asyncio.run(editor._invoke_anthropic([{"role": "user", "content": "hi"}]))
