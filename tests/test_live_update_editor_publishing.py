import asyncio
from types import SimpleNamespace

from src.features.summarising.live_update_editor import LiveUpdateEditor


def passing_editorial_decision(**overrides):
    return {
        "new_information": "A concrete update was shared in the source message.",
        "duplicate_assessment": "Checked recent updates; no duplicate found.",
        "context_used": "Reviewed source message, same-channel context, author context, and recent feed context.",
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
        **overrides,
    }


class FixedCandidateGenerator:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []

    async def generate_candidates(self, **kwargs):
        self.calls.append(kwargs)
        run_id = kwargs["run_id"]
        guild_id = kwargs["guild_id"]
        candidates = []
        for candidate in self.candidates:
            raw_agent_output = {
                "generator": "test",
                "editorial_decision": passing_editorial_decision(),
                **(candidate.get("raw_agent_output") or {}),
            }
            candidates.append({
                "run_id": run_id,
                "guild_id": guild_id,
                "source_channel_id": 10,
                "update_type": "project_update",
                "title": "Live update",
                "body": "This is a live update with enough detail to publish.",
                "media_refs": [],
                "source_message_ids": ["100"],
                "author_context_snapshot": {"member_id": 1},
                "duplicate_key": "dup-key",
                "confidence": 0.9,
                "priority": 3,
                "rationale": "fixed candidate",
                "raw_agent_output": raw_agent_output,
                **candidate,
            })
        return candidates


class FakeConfig:
    def resolve_guild_id(self, require_write=False):
        return 1

    def get_server(self, guild_id):
        return {"summary_channel_id": 2}


class FakeChannel:
    def __init__(self, *, fail=False, fail_after=None):
        self.fail = fail
        self.fail_after = fail_after
        self.sent = []
        self.send_kwargs = []
        self.next_id = 9000

    async def send(self, content, **kwargs):
        if self.fail or (self.fail_after is not None and len(self.sent) >= self.fail_after):
            raise RuntimeError("discord send failed")
        self.next_id += 1
        self.sent.append(content)
        self.send_kwargs.append(kwargs)
        return SimpleNamespace(id=self.next_id)


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 2 else None


class FakeDB:
    server_config = FakeConfig()

    def __init__(self, *, recent_feed=None):
        self.runs = []
        self.candidates = []
        self.decisions = []
        self.feed_items = list(recent_feed or [])
        self.duplicate_states = {}
        self.candidate_updates = []
        self.memory = []
        self.checkpoints = []

    # -- reads (accept and ignore environment to match db_handler signature) --

    def get_live_update_checkpoint(self, key, environment="prod"):
        return None

    def get_live_update_editorial_memory(self, guild_id, limit=100, environment="prod"):
        return []

    def get_live_update_watchlist(self, guild_id, status="active", limit=100, environment="prod"):
        return []

    def get_recent_live_update_feed_items(self, guild_id=None, live_channel_id=None, limit=50, since_hours=None, environment="prod"):
        return self.feed_items[:limit]

    def find_live_update_duplicate(self, duplicate_key, guild_id=None, environment="prod"):
        feed_item = next(
            (item for item in reversed(self.feed_items) if item.get("duplicate_key") == duplicate_key and item.get("status") == "posted"),
            None,
        )
        candidate = next(
            (item for item in reversed(self.candidates) if item.get("duplicate_key") == duplicate_key),
            None,
        )
        return {
            "duplicate_state": self.duplicate_states.get(duplicate_key),
            "feed_item": feed_item,
            "candidate": candidate,
        }

    # -- writes (accept environment kwarg) --

    def create_live_update_run(self, run, environment="prod"):
        row = {**run, "run_id": "run-1"}
        self.runs.append(row)
        return row

    def update_live_update_run(self, run_id, updates, guild_id=None, environment="prod"):
        self.runs[-1].update(updates)
        return self.runs[-1]

    def get_archived_messages_after_checkpoint(self, checkpoint=None, guild_id=None, channel_ids=None, limit=200, exclude_author_ids=None):
        return [{
            "message_id": 100,
            "channel_id": 10,
            "author_id": 1,
            "content": "archived source",
            "created_at": "2026-05-08T10:00:00Z",
            "attachments": [],
            "embeds": [],
            "author_context_snapshot": {"member_id": 1},
        }]

    def get_live_update_context_for_messages(self, messages, guild_id=None, limit=24, environment="prod", exclude_author_ids=None):
        return {
            "source_context": {
                "100": {
                    "same_channel_history": [{"message_id": "90", "content": "Earlier related workflow context."}],
                    "author_recent_messages": [{"message_id": "91", "content": "Previous work from this author."}],
                    "author_stats": {"total_messages": 42, "average_reactions_per_recent_message": 2.5},
                }
            }
        }

    def store_live_update_candidates(self, candidates, environment="prod"):
        self.candidates = [
            {**candidate, "candidate_id": f"candidate-{index}"}
            for index, candidate in enumerate(candidates, start=1)
        ]
        return self.candidates

    def update_live_update_candidate_status(self, candidate_id, status, updates=None, guild_id=None, environment="prod"):
        self.candidate_updates.append({
            "candidate_id": candidate_id,
            "status": status,
            "updates": updates or {},
        })
        for candidate in self.candidates:
            if candidate["candidate_id"] == candidate_id:
                candidate["status"] = status
        return {"candidate_id": candidate_id, "status": status}

    def store_live_update_decision(self, decision, guild_id=None, environment="prod"):
        row = {**decision, "decision_id": f"decision-{len(self.decisions) + 1}"}
        self.decisions.append(row)
        return row

    def upsert_live_update_duplicate_state(self, state, environment="prod"):
        self.duplicate_states[state["duplicate_key"]] = state
        return state

    def store_live_update_feed_item(self, feed_item, environment="prod"):
        row = {**feed_item, "feed_item_id": f"feed-{len(self.feed_items) + 1}"}
        self.feed_items.append(row)
        return row

    def upsert_live_update_editorial_memory(self, memory, environment="prod"):
        self.memory.append(memory)
        return memory

    def upsert_live_update_watchlist(self, watch, environment="prod"):
        return watch

    def upsert_live_update_checkpoint(self, checkpoint, environment="prod"):
        self.checkpoints.append(checkpoint)
        return checkpoint


def test_live_update_publishing_splits_and_persists_ordered_message_ids():
    long_body = " ".join(["segment"] * 620)
    assert len(long_body) > LiveUpdateEditor.DISCORD_MESSAGE_LIMIT
    channel = FakeChannel()
    db = FakeDB()
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([{
            "title": "Long update",
            "body": long_body,
            "duplicate_key": "long-update",
        }]),
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["status"] == "completed"
    assert result["published_count"] == 1
    assert len(channel.sent) > 1
    assert all(len(content) <= LiveUpdateEditor.DISCORD_MESSAGE_LIMIT for content in channel.sent)
    posted_feed_item = db.feed_items[-1]
    assert posted_feed_item["status"] == "posted"
    assert posted_feed_item["discord_message_ids"] == [
        str(message_id)
        for message_id in range(9001, 9001 + len(channel.sent))
    ]
    assert db.duplicate_states["long-update"]["status"] == "posted"
    assert db.duplicate_states["long-update"]["feed_item_id"] == posted_feed_item["feed_item_id"]
    assert db.memory and db.memory[0]["source_candidate_id"] == "candidate-1"


def test_live_update_publishing_disables_mentions_and_sanitizes_tags():
    channel = FakeChannel()
    db = FakeDB()
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([{
            "title": "Mention safety",
            "body": "<@1234567890> @everyone @artist shared a useful update with enough detail to publish.",
            "duplicate_key": "mention-safety",
        }]),
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["published_count"] == 1
    assert "<@1234567890>" not in channel.sent[0]
    assert "@everyone" not in channel.sent[0]
    assert "@artist" not in channel.sent[0]
    assert channel.send_kwargs[0].get("allowed_mentions") is not None


def test_live_update_publishing_suppresses_existing_feed_duplicate_without_send():
    channel = FakeChannel()
    db = FakeDB(recent_feed=[{
        "feed_item_id": "existing-feed",
        "duplicate_key": "dup-key",
        "discord_message_ids": ["7001", "7002"],
        "status": "posted",
    }])
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([{"duplicate_key": "dup-key"}]),
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["status"] == "completed"
    assert result["published_count"] == 0
    assert channel.sent == []
    duplicate_decision = next(decision for decision in db.decisions if decision["decision"] == "duplicate")
    assert duplicate_decision["duplicate_of_feed_item_id"] == "existing-feed"
    assert duplicate_decision["decision_payload"]["discord_message_ids"] == ["7001", "7002"]
    assert db.candidate_updates[-1]["status"] == "duplicate"


def test_live_update_publishing_records_failed_post_audit_state():
    channel = FakeChannel(fail_after=1)
    db = FakeDB()
    long_body = " ".join(["partial"] * 400)
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([{
            "body": long_body,
            "duplicate_key": "failing-update",
        }]),
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["status"] == "completed"
    assert result["failed_post_count"] == 1
    failed_feed_item = db.feed_items[-1]
    assert failed_feed_item["status"] == "failed"
    assert failed_feed_item["discord_message_ids"] == ["9001"]
    assert "discord send failed" in failed_feed_item["post_error"]
    assert db.decisions[-1]["decision"] == "failed_post"
    assert db.decisions[-1]["decision_payload"]["discord_message_ids"] == ["9001"]
    assert db.candidate_updates[-1]["status"] == "failed_post"


def test_live_update_dev_env_posts_and_persists_with_environment_dev(monkeypatch):
    """Dev env posts to Discord AND persists state (unlike old dry_run which skipped DB)."""
    monkeypatch.delenv("DEV_SUMMARY_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DEV_LIVE_UPDATE_CHANNEL_ID", raising=False)
    channel = FakeChannel()
    db = FakeDB()
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([{"duplicate_key": "dev-update"}]),
        environment="dev",
        dry_run_lookback_hours=1,
    )

    result = asyncio.run(editor.run_once("dev_scheduled"))

    assert result["status"] == "completed"
    assert result["published_count"] == 1
    assert channel.sent
    # Dev env STILL persists — this is the key flip from old dry_run behavior
    assert len(db.runs) >= 1
    assert len(db.candidates) >= 1
    assert len(db.decisions) >= 1
    assert len(db.feed_items) >= 1
    assert len(db.checkpoints) >= 1


def test_live_update_dev_env_resolves_dev_live_update_channel_id(monkeypatch):
    monkeypatch.delenv("DEV_SUMMARY_CHANNEL_ID", raising=False)
    monkeypatch.setenv("DEV_LIVE_UPDATE_CHANNEL_ID", "12345")
    editor = LiveUpdateEditor(
        FakeDB(),
        bot=FakeBot(FakeChannel()),
        candidate_generator=FixedCandidateGenerator([]),
        environment="dev",
    )

    assert editor._resolve_live_channel_id(1) == 12345


def test_live_update_editor_passes_expanded_context_to_candidate_generator():
    channel = FakeChannel()
    db = FakeDB(recent_feed=[{
        "feed_item_id": "feed-old",
        "title": "Already posted",
        "body": "Prior update body",
        "duplicate_key": "old",
        "status": "posted",
    }])
    generator = FixedCandidateGenerator([{"duplicate_key": "context-update"}])
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=generator,
    )

    asyncio.run(editor.run_once("test"))

    context = generator.calls[0]["context"]
    assert context["recent_feed_items"][0]["feed_item_id"] == "feed-old"
    assert context["source_context"]["100"]["same_channel_history"][0]["message_id"] == "90"
    assert "tool_use_guidance" in context


def test_live_update_editor_caps_published_candidates_per_run():
    channel = FakeChannel()
    db = FakeDB()
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([
            {"duplicate_key": "first-update", "priority": 5, "confidence": 0.9},
            {"duplicate_key": "second-update", "priority": 4, "confidence": 0.89},
        ]),
        max_publish_per_run=1,
    )

    result = asyncio.run(editor.run_once("test"))

    assert result["published_count"] == 1
    assert len(channel.sent) == 1


class EnvIsolationFakeDB:
    """FakeDB that separates dev and prod data for cross-environment testing."""
    server_config = FakeConfig()

    def __init__(self):
        self.runs = {"prod": [], "dev": []}
        self.candidates = {"prod": [], "dev": []}
        self.candidate_updates = {"prod": [], "dev": []}
        self.decisions = {"prod": [], "dev": []}
        self.duplicate_states = {"prod": {}, "dev": {}}
        self.feed_items = {"prod": [], "dev": []}
        self.checkpoints = {"prod": [], "dev": []}
        self.memory = {"prod": [], "dev": []}
        self.watchlist_items = {"prod": [], "dev": []}

    def _env(self, environment):
        return "dev" if environment == "dev" else "prod"

    # -- reads (filter by environment) --
    def get_live_update_checkpoint(self, key, environment="prod"):
        env = self._env(environment)
        items = self.checkpoints.get(env, [])
        return items[-1] if items else None

    def get_live_update_editorial_memory(self, guild_id, limit=100, environment="prod"):
        env = self._env(environment)
        return self.memory.get(env, [])[-limit:]

    def get_live_update_watchlist(self, guild_id, status="active", limit=100, environment="prod"):
        env = self._env(environment)
        return self.watchlist_items.get(env, [])[-limit:]

    def get_recent_live_update_feed_items(self, guild_id=None, live_channel_id=None, limit=50, since_hours=None, environment="prod"):
        env = self._env(environment)
        return self.feed_items.get(env, [])[-limit:]

    def find_live_update_duplicate(self, duplicate_key, guild_id=None, environment="prod"):
        env = self._env(environment)
        feed_item = next(
            (item for item in reversed(self.feed_items.get(env, [])) if item.get("duplicate_key") == duplicate_key and item.get("status") == "posted"),
            None,
        )
        candidate = next(
            (item for item in reversed(self.candidates.get(env, [])) if item.get("duplicate_key") == duplicate_key),
            None,
        )
        return {
            "duplicate_state": self.duplicate_states.get(env, {}).get(duplicate_key),
            "feed_item": feed_item,
            "candidate": candidate,
        }

    def get_live_top_creation_post_by_duplicate_key(self, duplicate_key, guild_id=None, environment="prod"):
        env = self._env(environment)
        # Not stored in this fake, but the editor could call it for cross-checks
        return None

    # -- writes (route by environment) --
    def create_live_update_run(self, run, environment="prod"):
        env = self._env(environment)
        row = {**run, "run_id": f"run-{env}-{len(self.runs[env]) + 1}"}
        self.runs[env].append(row)
        return row

    def update_live_update_run(self, run_id, updates, guild_id=None, environment="prod"):
        env = self._env(environment)
        if self.runs[env]:
            self.runs[env][-1].update(updates)
            return self.runs[env][-1]
        return None

    def get_archived_messages_after_checkpoint(self, checkpoint=None, guild_id=None, channel_ids=None, limit=200, exclude_author_ids=None):
        return [{
            "message_id": 100,
            "channel_id": 10,
            "author_id": 1,
            "content": "archived source",
            "created_at": "2026-05-08T10:00:00Z",
            "attachments": [],
            "embeds": [],
            "author_context_snapshot": {"member_id": 1},
        }]

    def get_live_update_context_for_messages(self, messages, guild_id=None, limit=24, environment="prod", exclude_author_ids=None):
        return {
            "source_context": {
                "100": {
                    "same_channel_history": [{"message_id": "90", "content": "Earlier context."}],
                    "author_recent_messages": [],
                    "author_stats": {"total_messages": 10, "average_reactions_per_recent_message": 1.0},
                }
            }
        }

    def store_live_update_candidates(self, candidates, environment="prod"):
        env = self._env(environment)
        stored = [
            {**candidate, "candidate_id": f"candidate-{env}-{index}"}
            for index, candidate in enumerate(candidates, start=1)
        ]
        self.candidates[env] = stored
        return stored

    def update_live_update_candidate_status(self, candidate_id, status, updates=None, guild_id=None, environment="prod"):
        env = self._env(environment)
        self.candidate_updates[env].append({"candidate_id": candidate_id, "status": status, "updates": updates or {}})
        for candidate in self.candidates[env]:
            if candidate["candidate_id"] == candidate_id:
                candidate["status"] = status
        return {"candidate_id": candidate_id, "status": status}

    def store_live_update_decision(self, decision, guild_id=None, environment="prod"):
        env = self._env(environment)
        row = {**decision, "decision_id": f"decision-{env}-{len(self.decisions[env]) + 1}"}
        self.decisions[env].append(row)
        return row

    def upsert_live_update_duplicate_state(self, state, environment="prod"):
        env = self._env(environment)
        self.duplicate_states[env][state["duplicate_key"]] = state
        return state

    def store_live_update_feed_item(self, feed_item, environment="prod"):
        env = self._env(environment)
        row = {**feed_item, "feed_item_id": f"feed-{env}-{len(self.feed_items[env]) + 1}"}
        self.feed_items[env].append(row)
        return row

    def upsert_live_update_editorial_memory(self, memory, environment="prod"):
        env = self._env(environment)
        self.memory[env].append(memory)
        return memory

    def upsert_live_update_watchlist(self, watch, environment="prod"):
        env = self._env(environment)
        self.watchlist_items[env].append(watch)
        return watch

    def upsert_live_update_checkpoint(self, checkpoint, environment="prod"):
        env = self._env(environment)
        self.checkpoints[env].append(checkpoint)
        return checkpoint


def test_live_update_cross_environment_isolation_dev_rows_not_visible_to_prod():
    """Seed dev-env rows; run a prod editor pass; assert they don't appear.

    Dev rows must NOT leak into prod reads for: get_recent_live_update_feed_items,
    get_live_update_watchlist, get_live_update_editorial_memory, and
    get_live_top_creation_post_by_duplicate_key (duplicate detection)."""
    db = EnvIsolationFakeDB()

    # Pre-seed dev rows
    db.feed_items["dev"].append({
        "feed_item_id": "feed-dev-1",
        "title": "Dev update",
        "body": "Dev feed content",
        "duplicate_key": "dev-key",
        "status": "posted",
        "discord_message_ids": ["8999"],
    })
    db.memory["dev"].append({
        "memory_key": "dev-memory",
        "summary": "Dev memory entry",
        "guild_id": 1,
    })
    db.watchlist_items["dev"].append({
        "watch_key": "dev-watch",
        "guild_id": 1,
        "status": "active",
        "priority": 5,
    })

    # Run a prod editor pass
    channel = FakeChannel()
    editor = LiveUpdateEditor(
        db,
        bot=FakeBot(channel),
        candidate_generator=FixedCandidateGenerator([{
            "duplicate_key": "prod-update",
            "body": "A meaningful prod update with enough detail to publish worthily.",
        }]),
        environment="prod",
    )

    result = asyncio.run(editor.run_once("test"))

    # Prod pass succeeded normally
    assert result["status"] == "completed"
    assert result["published_count"] == 1
    assert channel.sent

    # Prod feed items contain only the prod post, NOT the dev feed item
    prod_feed = db.feed_items["prod"]
    assert len(prod_feed) == 1
    assert prod_feed[0]["duplicate_key"] == "prod-update"
    assert not any(item.get("feed_item_id") == "feed-dev-1" for item in prod_feed)

    # Dev feed items are untouched
    dev_feed = db.feed_items["dev"]
    assert len(dev_feed) == 1
    assert dev_feed[0]["feed_item_id"] == "feed-dev-1"

    # Prod memory contains only the prod entry, not the dev entry
    prod_memory = db.memory["prod"]
    assert not any(entry.get("memory_key") == "dev-memory" for entry in prod_memory)

    # Prod watchlist contains only prod entries
    prod_watchlist = db.watchlist_items["prod"]
    assert not any(entry.get("watch_key") == "dev-watch" for entry in prod_watchlist)

    # Dev data is still intact
    assert len(db.memory["dev"]) == 1
    assert db.memory["dev"][0]["memory_key"] == "dev-memory"
    assert len(db.watchlist_items["dev"]) == 1
    assert db.watchlist_items["dev"][0]["watch_key"] == "dev-watch"
