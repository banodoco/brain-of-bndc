import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src.features.summarising.daily_digest import (
    daily_digest_run,
    enrich_items,
    post_digest,
    resolve_media_urls,
    topics_to_legacy_daily_summary_items,
)


def test_topics_to_legacy_daily_summary_items_matches_legacy_keys():
    topic = {
        "headline": "Test Headline",
        "summary": {
            "blocks": [
                {
                    "type": "intro",
                    "text": "Intro text.",
                    "source_message_ids": ["111"],
                    "media_refs": [{"message_id": "222", "index": 0}],
                },
                {
                    "type": "section",
                    "title": "Section title",
                    "text": "Section text.",
                    "source_message_ids": ["333"],
                    "media_refs": [{"message_id": "444", "index": 0}],
                },
            ]
        },
        "discord_message_ids": [999],
    }

    items = topics_to_legacy_daily_summary_items([topic])

    assert items == [
        {
            "title": "Test Headline",
            "mainText": "Intro text.",
            "mainMediaMessageId": "222",
            "subTopics": [
                {
                    "text": "**Section title**\nSection text.",
                    "subTopicMediaMessageIds": ["444"],
                    "message_id": "333",
                    "channel_id": None,
                }
            ],
            "message_id": "111",
            "channel_id": None,
        }
    ]
    assert "posted_message_ids" not in items[0]
    assert "subTopicText" not in items[0]["subTopics"][0]


def test_enrich_items_places_media_urls_in_legacy_locations():
    items = [
        {
            "mainMediaMessageId": "222",
            "subTopics": [{"subTopicMediaMessageIds": ["444", "555"]}],
        }
    ]

    enrich_items(
        items,
        {
            "222": [{"url": "https://cdn.test/main.jpg", "type": "image"}],
            "444": [{"url": "https://cdn.test/sub.mp4", "type": "video"}],
        },
    )

    assert items[0]["mainMediaUrls"] == [
        {"url": "https://cdn.test/main.jpg", "type": "image"}
    ]
    assert items[0]["subTopics"][0]["subTopicMediaUrls"] == [
        [{"url": "https://cdn.test/sub.mp4", "type": "video"}],
        None,
    ]
    assert "subTopicMediaUrls" not in items[0]


class FakeStorage:
    SUMMARY_MEDIA_BUCKET = "summary-media"

    def __init__(self, topics=None):
        self.topics = topics or []
        self.uploads = []
        self.rows = []

    async def get_topics(self, **kwargs):
        self.get_topics_kwargs = kwargs
        return list(self.topics)

    def get_topic_editor_source_messages(self, message_ids, guild_id=None, environment="prod", limit=50):
        self.source_lookup = (list(message_ids), guild_id, environment, limit)
        rows = {
            "222": {
                "message_id": "222",
                "channel_id": 12,
                "attachments": [
                    {
                        "url": "https://discord.test/main.png",
                        "filename": "main.png",
                        "content_type": "image/png",
                    }
                ],
                "embeds": [],
            },
            "444": {
                "message_id": "444",
                "channel_id": 34,
                "attachments": [
                    {
                        "url": "https://discord.test/sub.mp4",
                        "filename": "sub.mp4",
                        "content_type": "video/mp4",
                    }
                ],
                "embeds": [],
            },
        }
        return [rows[mid] for mid in message_ids if mid in rows]

    async def download_file(self, url):
        return {
            "bytes": b"fake-bytes",
            "content_type": "video/mp4" if url.endswith(".mp4") else "image/png",
        }

    async def upload_bytes_to_storage(self, data, path, content_type, bucket_name=None):
        self.uploads.append((path, content_type, bucket_name))
        return f"https://supabase.test/{bucket_name}/{path}"

    async def store_daily_digest(self, **kwargs):
        self.rows.append(kwargs)
        return True


def test_resolve_media_urls_uploads_legacy_paths_and_hydrates_channels():
    items = [
        {
            "mainMediaMessageId": "222",
            "message_id": "222",
            "channel_id": None,
            "subTopics": [
                {
                    "message_id": "444",
                    "channel_id": None,
                    "subTopicMediaMessageIds": ["444"],
                }
            ],
        }
    ]
    storage = FakeStorage()

    media = asyncio.run(
        resolve_media_urls(items, storage, guild_id=1, environment="prod", date="2026-05-24")
    )

    assert items[0]["channel_id"] == "12"
    assert items[0]["subTopics"][0]["channel_id"] == "34"
    assert media["222"][0]["url"].endswith("/2026-05-24/222_0.png")
    assert media["444"][0]["url"].endswith("/2026-05-24/444_0.mp4")
    assert ("2026-05-24/222_0.png", "image/png", "summary-media") in storage.uploads
    assert ("2026-05-24/444_0.mp4", "video/mp4", "summary-media") in storage.uploads


class FakeMessage:
    _next_id = 1000

    def __init__(self):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id


class FakeChannel:
    def __init__(self):
        self.sends = []

    async def send(self, content=None, files=None):
        self.sends.append({"content": content, "files": files})
        return FakeMessage()


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


def test_post_digest_records_text_and_media_message_ids():
    channel = FakeChannel()
    storage = FakeStorage()
    items = [
        {
            "title": "A",
            "mainText": "Body",
            "mainMediaUrls": [{"url": "https://supabase.test/main.png", "type": "image"}],
            "subTopics": [],
        }
    ]

    mapping = asyncio.run(post_digest(FakeBot(channel), items, 123, storage))

    assert len(mapping[0]) == 2
    assert channel.sends[0]["content"].startswith("## A")
    assert channel.sends[1]["files"]


def test_daily_digest_run_skips_empty_window():
    storage = FakeStorage(topics=[])
    channel = FakeChannel()

    result = asyncio.run(
        daily_digest_run(FakeBot(channel), storage, guild_id=1, channel_id=123, now=datetime(2026, 5, 24, tzinfo=timezone.utc))
    )

    assert result["status"] == "skipped"
    assert storage.rows == []
    assert channel.sends == []


def test_daily_digest_run_stores_enriched_legacy_row_with_posted_ids():
    topic = {
        "headline": "Daily Topic",
        "state": "posted",
        "last_published_at": "2026-05-23T12:00:00+00:00",
        "summary": {
            "blocks": [
                {
                    "type": "intro",
                    "text": "Intro",
                    "source_message_ids": ["222"],
                    "media_refs": [{"message_id": "222", "index": 0}],
                }
            ]
        },
    }
    storage = FakeStorage(topics=[topic])
    channel = FakeChannel()

    result = asyncio.run(
        daily_digest_run(
            FakeBot(channel),
            storage,
            guild_id=1,
            channel_id=1507878962515148952,
            environment="prod",
            now=datetime(2026, 5, 24, tzinfo=timezone.utc),
        )
    )

    assert result["status"] == "ok"
    assert storage.get_topics_kwargs["limit"] == 500
    row = storage.rows[0]
    assert row["channel_id"] == 1507878962515148952
    assert row["date"] == "2026-05-24"
    assert row["dev_mode"] is False
    assert row["guild_id"] == 1
    item = row["full_summary"][0]
    assert item["mainMediaUrls"][0]["url"].endswith("/2026-05-24/222_0.png")
    assert item["posted_message_ids"]
    json.dumps(row["full_summary"])
