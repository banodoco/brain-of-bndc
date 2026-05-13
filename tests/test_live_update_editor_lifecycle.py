import asyncio
from types import SimpleNamespace

from src.features.summarising.live_update_editor import LiveUpdateEditor


def raw_agent_output():
    return {
        "generator": "lifecycle-test",
        "editorial_decision": {
            "new_information": "A concrete demo milestone shipped.",
            "duplicate_assessment": "Checked recent updates; no duplicate found.",
            "context_used": "Reviewed source and recent feed context.",
            "editorial_checklist": {
                "source_verified": True,
                "new_information_identified": True,
                "prior_updates_checked": True,
                "surrounding_history_checked": True,
                "author_context_considered": True,
                "community_signal_checked": True,
                "duplicate_checked": True,
                "public_value_clear": True,
                "risk_checked": True,
                "media_selected_when_useful": True,
                "publish_format_ready": True,
            },
        },
    }


class LifecycleCandidateGenerator:
    async def generate_candidates(self, **kwargs):
        run_id = kwargs["run_id"]
        guild_id = kwargs["guild_id"]
        return [
            {
                "run_id": run_id,
                "guild_id": guild_id,
                "source_channel_id": 10,
                "update_type": "project_update",
                "title": "Demo milestone",
                "body": "The demo milestone shipped with a new editor and export controls.",
                "media_refs": [{"url": "https://cdn.example.test/demo.png"}],
                "source_message_ids": ["100"],
                "author_context_snapshot": {"member_id": 42, "username": "maker"},
                "duplicate_key": "demo-milestone",
                "confidence": 0.88,
                "priority": 5,
                "rationale": "substantive project update",
                "raw_agent_output": raw_agent_output(),
            },
            {
                "run_id": run_id,
                "guild_id": guild_id,
                "source_channel_id": 10,
                "update_type": "watchlist",
                "title": "Low confidence",
                "body": "This may matter later but needs more corroboration.",
                "media_refs": [],
                "source_message_ids": ["101"],
                "author_context_snapshot": {"member_id": 43},
                "duplicate_key": "low-confidence",
                "confidence": 0.2,
                "priority": 1,
                "rationale": "uncertain signal",
                "raw_agent_output": raw_agent_output(),
            },
            {
                "run_id": run_id,
                "guild_id": guild_id,
                "source_channel_id": 10,
                "update_type": "other",
                "title": "Too short",
                "body": "Tiny",
                "media_refs": [],
                "source_message_ids": ["102"],
                "author_context_snapshot": {"member_id": 44},
                "duplicate_key": "too-short",
                "confidence": 0.9,
                "priority": 1,
                "rationale": "not enough content",
                "raw_agent_output": raw_agent_output(),
            },
        ]


class FakeConfig:
    def resolve_guild_id(self, require_write=False):
        return 1

    def get_server(self, guild_id):
        return {"summary_channel_id": 2}


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.send_kwargs = []
        self.next_id = 9100

    async def send(self, content, **kwargs):
        self.next_id += 1
        self.sent.append(content)
        self.send_kwargs.append(kwargs)
        return SimpleNamespace(id=self.next_id)


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 2 else None


class LifecycleDB:
    server_config = FakeConfig()

    def __init__(self, messages):
        self.messages = messages
        self.runs = []
        self.candidates = []
        self.candidate_updates = []
        self.decisions = []
        self.duplicates = {}
        self.feed_items = []
        self.memory = []
        self.watchlist_updates = []
        self.checkpoints = []

    # -- reads (accept environment kwarg) --

    def get_live_update_checkpoint(self, key, environment="prod"):
        return {"last_message_id": 99}

    def get_live_update_editorial_memory(self, guild_id, limit=100, environment="prod"):
        return [{"memory_key": "previous-demo", "summary": "Prior demo note"}]

    def get_live_update_watchlist(self, guild_id, status="active", limit=100, environment="prod"):
        return [{"guild_id": guild_id, "watch_key": "demo", "status": status, "priority": 3}]

    def get_recent_live_update_feed_items(self, guild_id=None, live_channel_id=None, limit=50, since_hours=None, environment="prod"):
        return []

    # -- writes (accept environment kwarg) --

    def create_live_update_run(self, run, environment="prod"):
        row = {**run, "run_id": "run-1"}
        self.runs.append(row)
        return row

    def update_live_update_run(self, run_id, updates, guild_id=None, environment="prod"):
        self.runs[-1].update(updates)
        return self.runs[-1]

    def get_archived_messages_after_checkpoint(self, checkpoint=None, guild_id=None, channel_ids=None, limit=200, exclude_author_ids=None):
        return self.messages[:limit]

    def store_live_update_candidates(self, candidates, environment="prod"):
        self.candidates = [
            {**candidate, "candidate_id": f"candidate-{index}"}
            for index, candidate in enumerate(candidates, start=1)
        ]
        return self.candidates

    def find_live_update_duplicate(self, duplicate_key, guild_id=None, environment="prod"):
        return {
            "duplicate_state": self.duplicates.get(duplicate_key),
            "feed_item": None,
            "candidate": next(
                (candidate for candidate in self.candidates if candidate.get("duplicate_key") == duplicate_key),
                None,
            ),
        }

    def update_live_update_candidate_status(self, candidate_id, status, updates=None, guild_id=None, environment="prod"):
        self.candidate_updates.append({"candidate_id": candidate_id, "status": status, "updates": updates or {}})
        for candidate in self.candidates:
            if candidate["candidate_id"] == candidate_id:
                candidate["status"] = status
        return {"candidate_id": candidate_id, "status": status}

    def store_live_update_decision(self, decision, guild_id=None, environment="prod"):
        row = {**decision, "decision_id": f"decision-{len(self.decisions) + 1}"}
        self.decisions.append(row)
        return row

    def upsert_live_update_duplicate_state(self, state, environment="prod"):
        self.duplicates[state["duplicate_key"]] = state
        return state

    def store_live_update_feed_item(self, feed_item, environment="prod"):
        row = {**feed_item, "feed_item_id": f"feed-{len(self.feed_items) + 1}"}
        self.feed_items.append(row)
        return row

    def upsert_live_update_editorial_memory(self, memory, environment="prod"):
        self.memory.append(memory)
        return memory

    def upsert_live_update_watchlist(self, watch, environment="prod"):
        self.watchlist_updates.append(watch)
        return watch

    def upsert_live_update_checkpoint(self, checkpoint, environment="prod"):
        self.checkpoints.append(checkpoint)
        return checkpoint


def archived_message(message_id):
    return {
        "message_id": message_id,
        "channel_id": 10,
        "author_id": 42,
        "content": "We shipped a demo milestone with useful screenshots.",
        "created_at": f"2026-05-08T10:{message_id % 60:02d}:00Z",
        "attachments": [],
        "embeds": [],
        "author_context_snapshot": {"member_id": 42, "username": "maker"},
    }


def test_live_update_editor_records_candidate_lifecycle_memory_and_checkpoint():
    channel = FakeChannel()
    db = LifecycleDB([archived_message(100), archived_message(101), archived_message(102)])
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=LifecycleCandidateGenerator(),
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["status"] == "completed"
    assert result["candidate_count"] == 3
    assert result["published_count"] == 1
    assert [candidate["author_context_snapshot"]["member_id"] for candidate in db.candidates] == [42, 43, 44]
    assert [update["status"] for update in db.candidate_updates[:3]] == ["accepted", "deferred", "rejected"]
    assert [decision["decision"] for decision in db.decisions[:3]] == ["accepted", "deferred", "rejected"]
    assert channel.sent and db.feed_items[0]["discord_message_ids"] == ["9101"]
    assert db.feed_items[0]["status"] == "posted"
    assert db.duplicates["demo-milestone"]["status"] == "posted"
    assert db.memory[0]["memory_key"] == "demo-milestone"
    assert db.memory[0]["state"]["source_message_ids"] == ["100"]
    assert db.watchlist_updates[0]["last_matched_candidate_id"] == "candidate-1"
    assert db.checkpoints[-1]["last_message_id"] == 102
    assert db.checkpoints[-1]["state"]["last_status"] == "completed"
    assert db.runs[-1]["status"] == "completed"
    assert db.runs[-1]["metadata"]["published_count"] == 1


def test_live_update_editor_records_skipped_run_when_no_new_archived_messages():
    db = LifecycleDB([])
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(FakeChannel()),
        candidate_generator=LifecycleCandidateGenerator(),
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["status"] == "skipped"
    assert result["decision_count"] == 1
    assert db.decisions[0]["decision"] == "skipped"
    assert db.decisions[0]["reason"] == "no_new_archived_messages"
    assert db.checkpoints[-1]["state"] == {
        "last_status": "skipped",
        "reason": "no_new_archived_messages",
    }
    assert db.runs[-1]["status"] == "skipped"
    assert db.runs[-1]["skipped_reason"] == "no_new_archived_messages"
