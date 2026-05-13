import asyncio
import json
from types import SimpleNamespace

from src.features.summarising.live_top_creations import LiveTopCreations


class FakeConfig:
    def resolve_guild_id(self, require_write=False):
        return 1

    def get_server_field(self, guild_id, field, cast=None):
        values = {
            "summary_channel_id": 2,
            "top_gens_channel_id": 3,
            "art_channel_id": 4,
        }
        value = values.get(field)
        return cast(value) if cast and value is not None else value


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.send_kwargs = []
        self.next_id = 8000

    async def send(self, content, **kwargs):
        self.next_id += 1
        self.sent.append(content)
        self.send_kwargs.append(kwargs)
        return SimpleNamespace(id=self.next_id)


class FakeBot:
    def __init__(self, channel, *, summary_channel_id=None):
        self.channel = channel
        self.summary_channel_id = summary_channel_id

    def get_channel(self, channel_id):
        # Return channel if channel_id matches summary_channel_id (2) or self.channel's next_id
        if channel_id == 2:
            return self.channel
        if self.summary_channel_id and channel_id == self.summary_channel_id:
            return self.channel
        return None


class FakeDB:
    server_config = FakeConfig()

    def __init__(self, *, checkpoint=None, messages=None):
        self.checkpoint = checkpoint
        self.messages = messages or []
        self.runs = []
        self.posts = []
        self.checkpoints = []
        self._posted_by_dup_key = {}  # composite unique: environment -> dup_key -> post

    # -- reads (accept environment kwarg) --

    def get_live_top_creation_checkpoint(self, checkpoint_key, environment="prod"):
        return self.checkpoint

    def get_live_top_creation_post_by_duplicate_key(self, duplicate_key, guild_id=None, environment="prod"):
        # Composite unique constraint: (environment, duplicate_key)
        env_map = self._posted_by_dup_key.get(environment, {})
        return env_map.get(duplicate_key)

    # -- writes (accept environment kwarg) --

    def create_live_top_creation_run(self, run, environment="prod"):
        row = {**run, "top_creation_run_id": "run-1"}
        self.runs.append(row)
        return row

    def get_archived_messages_after_checkpoint(self, checkpoint=None, guild_id=None, channel_ids=None, limit=200, exclude_author_ids=None):
        return self.messages[:limit]

    def store_live_top_creation_post(self, post, environment="prod"):
        row = {**post, "top_creation_post_id": f"post-{len(self.posts) + 1}"}
        self.posts.append(row)
        # Honor composite unique: only one post per (environment, duplicate_key)
        dup_key = post.get("duplicate_key")
        if dup_key:
            self._posted_by_dup_key.setdefault(environment, {})[dup_key] = row
        return row

    def upsert_live_top_creation_checkpoint(self, checkpoint, environment="prod"):
        self.checkpoint = checkpoint
        self.checkpoints.append(checkpoint)
        return checkpoint

    def update_live_top_creation_run(self, run_id, updates, guild_id=None, environment="prod"):
        if self.runs:
            self.runs[-1].update(updates)
        return self.runs[-1] if self.runs else None


def _message(message_id, *, channel_id=10, filename="clip.mp4", reactions=5, content="new work"):
    return {
        "message_id": message_id,
        "channel_id": channel_id,
        "thread_id": None,
        "content": content,
        "created_at": f"2026-05-08T10:{message_id % 60:02d}:00Z",
        "reaction_count": reactions,
        "attachments": json.dumps([{
            "url": f"https://cdn.example.test/{filename}",
            "filename": filename,
            "content_type": "video/mp4" if filename.endswith(".mp4") else "image/png",
        }]),
    }


def test_live_top_creations_posts_and_persists_ordered_message_ids_without_daily_summary():
    channel = FakeChannel()
    db = FakeDB(messages=[
        _message(101, filename="clip.mp4", reactions=7),
        _message(102, channel_id=4, filename="art.png", reactions=6),
    ])
    service = LiveTopCreations(db, bot=FakeBot(channel), min_reactions=3)

    result = asyncio.run(service.run_once("test"))

    assert result["status"] == "completed"
    assert result["posted_count"] == 2
    assert len(channel.sent) == 2
    assert [post["discord_message_ids"] for post in db.posts] == [["8001"], ["8002"]]
    assert {post["source_kind"] for post in db.posts} == {"top_generation", "top_art_share"}
    assert db.checkpoints[-1]["state"]["posted_duplicate_keys"] == [
        "top_art_share:1:102",
        "top_generation:1:101",
    ]
    assert not hasattr(service, "generate_summary")


def test_live_top_creations_disables_mentions_and_sanitizes_tags():
    channel = FakeChannel()
    db = FakeDB(messages=[
        _message(
            501,
            filename="clip.mp4",
            reactions=7,
            content="<@1234567890> @everyone @artist shared a new clip",
        )
    ])
    service = LiveTopCreations(db, bot=FakeBot(channel), min_reactions=3)

    result = asyncio.run(service.run_once("test"))

    assert result["posted_count"] == 1
    assert "<@1234567890>" not in channel.sent[0]
    assert "@everyone" not in channel.sent[0]
    assert "@artist" not in channel.sent[0]
    assert channel.send_kwargs[0].get("allowed_mentions") is not None


def test_live_top_creations_suppresses_repeated_posts_from_checkpoint_and_db_dedupe():
    """Checkpoint dedupe + DB-level dedupe survive checkpoint reset.

    Run 1: message 201 gets posted, checkpoint records duplicate_keys.
    Run 2: same message should be suppressed by checkpoint (no new messages).
    Even if checkpoint resets, the DB dedupe lookup blocks re-posting.
    """
    channel = FakeChannel()
    checkpoint = {
        "state": {
            "posted_duplicate_keys": ["top_generation:1:201"],
        },
        "last_source_message_id": 200,
    }
    db = FakeDB(
        checkpoint=checkpoint,
        messages=[_message(201, filename="clip.mp4", reactions=9)],
    )
    # Pre-seed the DB dedupe store to simulate a posted row
    db._posted_by_dup_key.setdefault("prod", {})["top_generation:1:201"] = {
        "top_creation_post_id": "post-existing",
        "duplicate_key": "top_generation:1:201",
        "status": "posted",
    }

    service = LiveTopCreations(db, bot=FakeBot(channel), min_reactions=3)

    result = asyncio.run(service.run_once("test"))

    assert result["status"] == "skipped"
    assert result["posted_count"] == 0
    assert result["skipped_count"] >= 1
    assert channel.sent == []
    assert db.runs[-1]["status"] == "skipped"


def test_live_top_creations_splits_overlong_posts_and_preserves_id_order():
    channel = FakeChannel()
    long_media_message = _message(301, filename="clip.mp4", reactions=8)
    long_media_message["attachments"] = json.dumps([{
        "url": f"https://cdn.example.test/{'x' * 2200}.mp4",
        "filename": "clip.mp4",
        "content_type": "video/mp4",
    }])
    db = FakeDB(messages=[long_media_message])
    service = LiveTopCreations(db, bot=FakeBot(channel), min_reactions=3)

    result = asyncio.run(service.run_once("test"))

    assert result["status"] == "completed"
    assert len(channel.sent) > 1
    assert all(len(content) <= LiveTopCreations.DISCORD_MESSAGE_LIMIT for content in channel.sent)
    assert db.posts[0]["discord_message_ids"] == [
        str(message_id)
        for message_id in range(8001, 8001 + len(channel.sent))
    ]


def test_live_top_creations_dev_env_posts_and_persists_state(monkeypatch):
    """Dev env publishes to Discord AND persists state (was dry_run which skipped DB)."""
    monkeypatch.delenv("DEV_SUMMARY_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DEV_LIVE_UPDATE_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DEV_ART_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ART_CHANNEL_ID", raising=False)
    channel = FakeChannel()
    db = FakeDB(messages=[_message(401, channel_id=4, filename="art.png", reactions=5)])
    service = LiveTopCreations(
        db,
        bot=FakeBot(channel),
        min_reactions=3,
        environment="dev",
        dry_run_lookback_hours=1,
    )

    result = asyncio.run(service.run_once("dev_scheduled"))

    assert result["status"] == "completed"
    assert result["posted_count"] == 1
    assert channel.sent
    # Dev env persists state — key flip from old dry_run
    assert len(db.runs) >= 1
    assert len(db.posts) >= 1
    assert len(db.checkpoints) >= 1


def test_live_top_creations_dev_env_resolves_summary_channel(monkeypatch):
    """Dev env resolves DEV_SUMMARY_CHANNEL_ID (not a separate top-gens channel)."""
    monkeypatch.setenv("DEV_SUMMARY_CHANNEL_ID", "555")
    channel = FakeChannel()
    bot = FakeBot(channel)
    # Make get_channel return channel for 555
    bot.channel_map = {555: channel}
    original_get_channel = bot.get_channel

    def get_channel(channel_id):
        if hasattr(bot, "channel_map") and channel_id in bot.channel_map:
            return bot.channel_map[channel_id]
        return original_get_channel(channel_id)

    bot.get_channel = get_channel

    service = LiveTopCreations(
        FakeDB(),
        bot=bot,
        environment="dev",
    )

    assert service._resolve_top_channel_id(1) == 555


def test_live_top_creations_dedupe_exactly_one_send_and_one_db_row():
    """Run twice over same archived message (reactions ≥ 5) → exactly one DB row and one Discord send.

    Second pass short-circuits via get_live_top_creation_post_by_duplicate_key."""
    # --- Pass 1 ---
    channel = FakeChannel()
    db = FakeDB(messages=[_message(601, filename="clip.mp4", reactions=7)])
    service = LiveTopCreations(db, bot=FakeBot(channel), min_reactions=3)

    result1 = asyncio.run(service.run_once("test"))

    assert result1["status"] == "completed"
    assert result1["posted_count"] == 1
    assert len(channel.sent) == 1
    assert len(db.posts) == 1
    assert db.posts[0]["duplicate_key"] == "top_generation:1:601"

    # --- Pass 2: same message, same environment ---
    # DB dedupe store already has the post from pass 1, so the
    # checkpoint won't have posted_duplicate_keys (checkpoints only carry
    # state from the most recent run). The DB-level dedupe is the safety net.
    db2 = FakeDB(messages=[_message(601, filename="clip.mp4", reactions=7)])
    # Carry forward the dedupe state from db
    db2._posted_by_dup_key = db._posted_by_dup_key
    channel2 = FakeChannel()
    service2 = LiveTopCreations(db2, bot=FakeBot(channel2), min_reactions=3)

    result2 = asyncio.run(service2.run_once("test"))

    assert result2["status"] == "completed"
    assert result2["posted_count"] == 0
    assert result2["skipped_count"] >= 1
    # Zero Discord sends on second pass
    assert len(channel2.sent) == 0
    # Zero new DB rows created on second pass (dedupe blocked store)
    assert len(db2.posts) == 0
    # The dedupe map still has the original post
    env_map = db2._posted_by_dup_key.get("prod", {})
    assert "top_generation:1:601" in env_map
    assert env_map["top_generation:1:601"]["duplicate_key"] == "top_generation:1:601"
