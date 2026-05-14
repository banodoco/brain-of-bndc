"""Tests for media understanding (image/video) cache, budget, and enrichment.

All tests stub ``vision_clients`` via monkeypatch — zero real API calls.
"""

import json
from types import SimpleNamespace

import pytest
import requests as requests_module

import src.common.vision_clients as vision_clients
import src.features.summarising.topic_editor as topic_editor_module
from src.features.summarising.topic_editor import TopicEditor


# ---------------------------------------------------------------------------
# Fake response bytes from a pretend media download
# ---------------------------------------------------------------------------

FAKE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
FAKE_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512


# ---------------------------------------------------------------------------
# Stub vision-client results
# ---------------------------------------------------------------------------

FAKE_IMAGE_UNDERSTANDING: dict = {
    "kind": "generation",
    "subject": "A photorealistic portrait of a cyberpunk cat",
    "technical_signal": "sharp focus, good lighting, rich colour",
    "aesthetic_quality": 8,
    "discriminator_notes": "compelling composition; worth sharing",
}

FAKE_VIDEO_UNDERSTANDING: dict = {
    "kind": "generation",
    "summary": "A 15-second animation of a robot dancing in a city street",
    "visual_read": "bright daylight, wide shot, steady camera, no cuts",
    "audio_read": "electronic beat, no speech, clean sync",
    "edit_value": "the pause at 0:08 makes a natural cut point",
    "highlight_score": 7.5,
    "energy": 8.0,
    "pacing": "steady",
    "production_quality": "good exposure, no clipping",
    "boundary_notes": "clip from 0:02 to 0:14 is the strongest section",
    "cautions": "audio peaks at 0:11 may need compression",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get(url, timeout=30):
    """Return a response whose .content is fake image bytes."""
    return SimpleNamespace(
        content=FAKE_IMAGE_BYTES,
        raise_for_status=lambda: None,
    )


def _mock_requests(monkeypatch):
    """Patch ``requests.get`` so media downloads don't hit the network."""
    monkeypatch.setattr(requests_module, "get", _fake_get)


# ---------------------------------------------------------------------------
# Extended FakeDB that also tracks media-understanding rows
# ---------------------------------------------------------------------------

class MediaUnderstandingFakeDB:
    """A lightweight FakeDB for media-understanding test scenarios."""

    def __init__(self):
        self._understandings: dict[tuple, dict] = {}
        self.pk_lookups: list = []
        self.hash_lookups: list = []
        self.upserts: list = []
        self.source_message_rows: list[dict] = []

    def get_message_media_understanding(self, message_id, attachment_index, model):
        key = (int(message_id), int(attachment_index), str(model))
        self.pk_lookups.append(key)
        row = self._understandings.get(key)
        if row is None:
            return None
        understanding = row.get("understanding")
        if isinstance(understanding, str):
            try:
                understanding = json.loads(understanding)
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "message_id": row["message_id"],
            "attachment_index": row["attachment_index"],
            "media_kind": row["media_kind"],
            "content_hash": row.get("content_hash"),
            "model": row["model"],
            "understanding": understanding,
            "created_at": row.get("created_at"),
        }

    def get_message_media_understanding_by_hash(self, content_hash, model=None, media_kind=None):
        self.hash_lookups.append((content_hash, model, media_kind))
        for key, row in self._understandings.items():
            if row.get("content_hash") != content_hash:
                continue
            if model is not None and row.get("model") != model:
                continue
            if media_kind is not None and row.get("media_kind") != media_kind:
                continue
            understanding = row.get("understanding")
            if isinstance(understanding, str):
                try:
                    understanding = json.loads(understanding)
                except (json.JSONDecodeError, TypeError):
                    pass
            return {
                "message_id": row["message_id"],
                "attachment_index": row["attachment_index"],
                "media_kind": row["media_kind"],
                "content_hash": row.get("content_hash"),
                "model": row["model"],
                "understanding": understanding,
                "created_at": row.get("created_at"),
            }
        return None

    def upsert_message_media_understanding(self, row):
        key = (
            int(row["message_id"]),
            int(row.get("attachment_index", 0)),
            str(row["model"]),
        )
        self._understandings[key] = dict(row)
        self.upserts.append(dict(row))
        return {"status": "ok"}

    def get_topic_editor_source_messages(self, message_ids, guild_id=None, environment="prod", limit=50):
        wanted = {str(message_id) for message_id in message_ids or []}
        rows = [
            row for row in self.source_message_rows
            if str(row.get("message_id")) in wanted
            and (guild_id is None or row.get("guild_id") == guild_id)
        ]
        return rows[:limit]

    def seed(self, *, message_id: int, attachment_index: int = 0,
             media_kind: str = "image", model: str = "gpt-4o-mini",
             content_hash: str | None = None,
             understanding: dict | None = None):
        """Convenience: insert a pre-cached row."""
        row = {
            "message_id": message_id,
            "attachment_index": attachment_index,
            "media_url": "https://cdn.example.test/media.png",
            "media_kind": media_kind,
            "content_hash": content_hash,
            "model": model,
            "understanding": understanding or {},
        }
        self.upsert_message_media_understanding(row)


# ---------------------------------------------------------------------------
# Standalone helper to build a dispatcher context
# ---------------------------------------------------------------------------

def _make_context(*, messages=None, vision_budget=1.0, vision_cost=0.0):
    return {
        "run_id": "run-media-test",
        "guild_id": 1,
        "live_channel_id": 2,
        "messages": messages or [],
        "active_topics": [],
        "aliases": [],
        "seen_tool_call_ids": set(),
        "idempotent_results": {},
        "observation_count": 0,
        "created_topics": [],
        "finalize": None,
        "vision_budget_usd": vision_budget,
        "vision_cost_usd": vision_cost,
    }


# ---------------------------------------------------------------------------
# Test 1: cache-first — second call returns cached without API
# ---------------------------------------------------------------------------

def test_cache_first_second_call_no_api(monkeypatch):
    """Two ``understand_image`` calls for the same PK: second is a cache hit."""
    db = MediaUnderstandingFakeDB()

    messages = [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "check this render",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [{"url": "https://cdn.test/img.png"}],
        }
    ]
    context = _make_context(messages=messages)

    describe_image_calls: list = []

    def fake_describe_image(image_bytes, model):
        describe_image_calls.append((len(image_bytes), model))
        return dict(FAKE_IMAGE_UNDERSTANDING)

    monkeypatch.setattr(vision_clients, "describe_image", fake_describe_image)
    monkeypatch.setattr(vision_clients, "_sha256", lambda data: "abc123stillimagesha256")
    _mock_requests(monkeypatch)

    editor = TopicEditor(db_handler=db, llm_client=None, guild_id=1, environment="prod")

    # First call — should hit the API.
    call1 = {"id": "vision-1", "name": "understand_image", "input": {"message_id": 100, "attachment_index": 0, "mode": "fast"}}
    outcome1 = editor._dispatch_understand_media(call1, context, "image")

    assert outcome1["outcome"] == "read"
    assert outcome1["result"]["cached"] is False
    assert len(describe_image_calls) == 1
    assert describe_image_calls[0][1] == "gpt-4o-mini"
    assert len(db.upserts) == 1
    assert float(context["vision_cost_usd"]) > 0

    # Second call — cache hit, no new API call.
    call2 = {"id": "vision-2", "name": "understand_image", "input": {"message_id": 100, "attachment_index": 0, "mode": "fast"}}
    outcome2 = editor._dispatch_understand_media(call2, context, "image")

    assert outcome2["outcome"] == "read"
    assert outcome2["result"]["cached"] is True
    assert len(describe_image_calls) == 1
    assert len(db.upserts) == 1


# ---------------------------------------------------------------------------
# Test 2: cross-message dedup via content_hash
# ---------------------------------------------------------------------------

def test_cross_message_dedup_via_content_hash(monkeypatch):
    """Same bytes, different message_ids → hash cache kicks in."""
    db = MediaUnderstandingFakeDB()

    messages = [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "image A",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [{"url": "https://cdn.test/img_a.png"}],
        },
        {
            "message_id": 200,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 43,
            "content": "same image reposted",
            "created_at": "2026-05-13T11:00:00Z",
            "author_context_snapshot": {"username": "bob"},
            "attachments": [{"url": "https://cdn.test/img_a.png"}],
        },
    ]

    describe_image_calls: list = []

    def fake_describe_image(image_bytes, model):
        describe_image_calls.append(True)
        return dict(FAKE_IMAGE_UNDERSTANDING)

    COMPUTED_HASH = "abcdef1234567890image"

    monkeypatch.setattr(vision_clients, "describe_image", fake_describe_image)
    monkeypatch.setattr(vision_clients, "_sha256", lambda data: COMPUTED_HASH)
    _mock_requests(monkeypatch)

    editor = TopicEditor(db_handler=db, llm_client=None, guild_id=1, environment="prod")

    # First message (ID=100) — hits the API.
    context1 = _make_context(messages=messages)
    call1 = {"id": "vision-1", "name": "understand_image", "input": {"message_id": 100, "attachment_index": 0, "mode": "fast"}}
    outcome1 = editor._dispatch_understand_media(call1, context1, "image")

    assert outcome1["outcome"] == "read"
    assert outcome1["result"]["cached"] is False
    assert len(describe_image_calls) == 1
    assert len(db.upserts) == 1

    key100 = (100, 0, "gpt-4o-mini")
    assert key100 in db._understandings
    assert db._understandings[key100]["message_id"] == 100

    # Second message (ID=200) — same bytes → hash cache.
    context2 = _make_context(messages=messages)
    call2 = {"id": "vision-2", "name": "understand_image", "input": {"message_id": 200, "attachment_index": 0, "mode": "fast"}}
    outcome2 = editor._dispatch_understand_media(call2, context2, "image")

    assert outcome2["outcome"] == "read"
    assert outcome2["result"]["cached"] is True
    assert outcome2["result"].get("dedup") is True
    assert len(describe_image_calls) == 1

    assert len(db.upserts) == 2
    key200 = (200, 0, "gpt-4o-mini")
    assert key200 in db._understandings
    assert db._understandings[key200]["message_id"] == 200
    assert db._understandings[key200]["content_hash"] == COMPUTED_HASH


# ---------------------------------------------------------------------------
# Test 3: payload enrichment surfaces cached understandings
# ---------------------------------------------------------------------------

class EnrichmentFakeDB(MediaUnderstandingFakeDB):
    """FakeDB extended with the getters that _build_initial_user_payload needs."""

    def __init__(self):
        super().__init__()
        self.checkpoints = []
        self.topics = []
        self.sources = []
        self.aliases = []
        self.transitions = []
        self.completed = []
        self.failed = []
        self.topic_alias_rows = []

    def get_topic_editor_checkpoint(self, checkpoint_key, environment="prod"):
        return {
            "checkpoint_key": checkpoint_key,
            "guild_id": 1,
            "channel_id": 2,
            "last_message_id": 99,
        }

    def mirror_live_checkpoint_to_topic_editor(self, checkpoint_key, environment="prod"):
        return None

    def acquire_topic_editor_run(self, run, environment="prod"):
        return {"run_id": "run-1"}

    def get_archived_messages_after_checkpoint(self, *, checkpoint, guild_id, channel_ids, limit, exclude_author_ids):
        return []

    def get_topics(self, guild_id=None, states=None, limit=100, environment="prod"):
        return []

    def get_topic_aliases(self, guild_id=None, environment="prod"):
        return []

    def search_topic_editor_topics(self, query, guild_id=None, environment="prod", state_filter=None, hours_back=72, limit=10):
        return []

    def upsert_topic_editor_checkpoint(self, checkpoint, environment="prod"):
        return checkpoint

    def complete_topic_editor_run(self, run_id, updates=None, guild_id=None, environment="prod"):
        return {"status": "ok"}

    def store_topic_transition(self, transition, environment="prod"):
        self.transitions.append((transition, environment))

    def get_topic_transitions_by_tool_call_ids(self, run_id, tool_call_ids, environment="prod"):
        return {}


def test_payload_enrichment_surfaces_cached_understandings():
    """Seed cache rows, call _build_initial_user_payload, verify media_understandings."""
    db = EnrichmentFakeDB()

    db.seed(
        message_id=100,
        attachment_index=0,
        media_kind="image",
        model="gpt-4o-mini",
        content_hash="hash-img-100",
        understanding=FAKE_IMAGE_UNDERSTANDING,
    )

    messages = [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "check this render",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [{"url": "https://cdn.test/img.png"}],
        }
    ]

    editor = TopicEditor(db_handler=db, llm_client=None, guild_id=1, environment="prod")

    payload = editor._build_initial_user_payload(messages, [])

    assert "source_messages" in payload
    assert len(payload["source_messages"]) == 1
    msg_payload = payload["source_messages"][0]

    assert "media_understandings" in msg_payload
    understandings = msg_payload["media_understandings"]
    assert len(understandings) >= 1

    mini_entry = next(
        (u for u in understandings if u.get("model") == "gpt-4o-mini"),
        None,
    )
    assert mini_entry is not None
    assert mini_entry["attachment_index"] == 0
    assert mini_entry["kind"] == "generation"
    assert mini_entry["subject"] == FAKE_IMAGE_UNDERSTANDING["subject"]
    assert mini_entry["aesthetic_quality"] == 8
    assert mini_entry["technical_signal"] == FAKE_IMAGE_UNDERSTANDING["technical_signal"]


# ---------------------------------------------------------------------------
# Test 4: budget cap returns budget_exceeded
# ---------------------------------------------------------------------------

def test_budget_cap_returns_budget_exceeded(monkeypatch):
    """With budget=0.001 (< estimated $0.01 cost), vision tool returns budget_exceeded."""
    db = MediaUnderstandingFakeDB()

    messages = [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "image",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [{"url": "https://cdn.test/img.png"}],
        }
    ]
    context = _make_context(messages=messages, vision_budget=0.001, vision_cost=0.0)

    def fail_describe_image(*_args, **_kwargs):
        raise AssertionError("vision API should not be called when budget exceeded")

    monkeypatch.setattr(vision_clients, "describe_image", fail_describe_image)
    monkeypatch.setattr(vision_clients, "_sha256", lambda data: "budget-test-hash")
    _mock_requests(monkeypatch)

    editor = TopicEditor(db_handler=db, llm_client=None, guild_id=1, environment="prod")

    call = {"id": "vision-budget", "name": "understand_image", "input": {"message_id": 100, "attachment_index": 0, "mode": "fast"}}
    outcome = editor._dispatch_understand_media(call, context, "image")

    assert outcome["outcome"] == "budget_exceeded"
    assert "budget" in outcome.get("error", "").lower() or "exceed" in outcome.get("error", "").lower()
    assert len(db.upserts) == 0


# ---------------------------------------------------------------------------
# Test 5: budget not deducted on cache hit
# ---------------------------------------------------------------------------

def test_budget_not_deducted_on_cache_hit(monkeypatch):
    """When a result comes from cache, the vision budget is untouched."""
    db = MediaUnderstandingFakeDB()

    FAKE_HASH = "cached-img-hash-001"
    db.seed(
        message_id=100,
        attachment_index=0,
        media_kind="image",
        model="gpt-4o-mini",
        content_hash=FAKE_HASH,
        understanding=FAKE_IMAGE_UNDERSTANDING,
    )

    messages = [
        {
            "message_id": 100,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "image",
            "created_at": "2026-05-13T10:00:00Z",
            "author_context_snapshot": {"username": "alice"},
            "attachments": [{"url": "https://cdn.test/img.png"}],
        }
    ]
    context = _make_context(messages=messages, vision_budget=1.0, vision_cost=0.0)

    def fail_describe_image(*_args, **_kwargs):
        raise AssertionError("describe_image should not be called on cache hit")

    monkeypatch.setattr(vision_clients, "describe_image", fail_describe_image)

    editor = TopicEditor(db_handler=db, llm_client=None, guild_id=1, environment="prod")

    call = {"id": "vision-cache", "name": "understand_image", "input": {"message_id": 100, "attachment_index": 0, "mode": "fast"}}
    outcome = editor._dispatch_understand_media(call, context, "image")

    assert outcome["outcome"] == "read"
    assert outcome["result"]["cached"] is True
    assert float(context["vision_cost_usd"]) == 0.0


def test_understand_media_resolves_archive_message_outside_source_window(monkeypatch):
    """Archive-search-discovered message ids can still be understood."""
    db = MediaUnderstandingFakeDB()
    db.source_message_rows = [
        {
            "message_id": 1504502824657227816,
            "guild_id": 1,
            "channel_id": 10,
            "author_id": 42,
            "content": "older video",
            "created_at": "2026-05-13T10:00:00Z",
            "attachments": [{"url": "https://cdn.test/video.mp4", "content_type": "video/mp4"}],
        }
    ]
    db.seed(
        message_id=1504502824657227816,
        attachment_index=0,
        media_kind="video",
        model="gemini-2.5-flash",
        content_hash="older-video-hash",
        understanding=FAKE_VIDEO_UNDERSTANDING,
    )

    context = _make_context(messages=[], vision_budget=1.0, vision_cost=0.0)
    editor = TopicEditor(db_handler=db, llm_client=None, guild_id=1, environment="prod")

    call = {
        "id": "vision-archive",
        "name": "understand_video",
        "input": {"message_id": 1504502824657227816, "attachment_index": 0, "mode": "fast"},
    }
    outcome = editor._dispatch_understand_media(call, context, "video")

    assert outcome["outcome"] == "read"
    assert outcome["result"]["cached"] is True
    assert outcome["result"]["understanding"]["summary"] == FAKE_VIDEO_UNDERSTANDING["summary"]


# ---------------------------------------------------------------------------
# Test 6: verify tool schemas
# ---------------------------------------------------------------------------

def test_vision_tool_schemas_are_registered():
    """Smoke check: understand_image and understand_video are in the tool list."""
    tool_names = {tool["name"] for tool in topic_editor_module.TOPIC_EDITOR_TOOLS}
    assert "understand_image" in tool_names
    assert "understand_video" in tool_names

    img_tool = next(t for t in topic_editor_module.TOPIC_EDITOR_TOOLS if t["name"] == "understand_image")
    assert img_tool["input_schema"]["properties"]["mode"]["enum"] == ["fast", "best"]
    assert img_tool["input_schema"]["properties"]["mode"]["default"] == "fast"
    assert img_tool["input_schema"]["required"] == ["message_id"]
