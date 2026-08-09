import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src.features.summarising.daily_digest import (
    build_digest_candidates,
    curate_digest_stories,
    daily_digest_run,
    enrich_items,
    post_digest,
    resolve_media_urls,
    topics_to_legacy_daily_summary_items,
    _parse_digest_response,
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


# ---------------------------------------------------------------------------
# Editorial layer (LLM curation)
# ---------------------------------------------------------------------------

class FakeLLM:
    """Two-stage-aware fake: returns the select-stage response when the system
    prompt is the clustering prompt, else the write-stage response. `response`
    is a catch-all used when a stage-specific response isn't set."""

    def __init__(self, response="", select_response=None, write_response=None, raise_exc=None):
        self.response = response
        self.select_response = select_response
        self.write_response = write_response
        self.raise_exc = raise_exc
        self.calls = []

    async def generate_chat_completion(self, model, system_prompt, messages, **kwargs):
        self.calls.append(
            {"model": model, "system_prompt": system_prompt, "messages": messages, "kwargs": kwargs}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        if "clusters" in system_prompt:  # stage 1 (selection)
            return self.select_response if self.select_response is not None else self.response
        return self.write_response if self.write_response is not None else self.response


def _topic(headline, intro_text, *, media_id=None, source_id=None):
    intro = {"type": "intro", "text": intro_text}
    if source_id:
        intro["source_message_ids"] = [source_id]
    if media_id:
        intro["media_refs"] = [{"message_id": media_id, "index": 0}]
    return {
        "headline": headline,
        "state": "posted",
        "last_published_at": "2026-05-23T12:00:00+00:00",
        "summary": {"blocks": [intro]},
    }


def test_build_digest_candidates_exposes_text_sources_and_media():
    topics = [_topic("H1", "first", media_id="222", source_id="111")]
    cands = build_digest_candidates(topics)
    assert len(cands) == 1
    assert cands[0]["index"] == 0
    assert cands[0]["headline"] == "H1"
    assert "first" in cands[0]["text"]
    assert cands[0]["media_message_ids"] == ["222"]
    assert cands[0]["source_message_ids"] == ["111"]


def test_parse_digest_response_handles_markdown_fences():
    fenced = '```json\n{"stories": [{"title": "T", "body": "B"}]}\n```'
    stories = _parse_digest_response(fenced)
    assert stories == [{"title": "T", "body": "B"}]
    # plain JSON and junk-wrapped JSON both work
    assert _parse_digest_response('{"stories": []}') == []
    assert _parse_digest_response('blah {"stories":[{"title":"x"}]} trailing')[0]["title"] == "x"
    assert _parse_digest_response("not json at all") == []


def test_curate_builds_blocks_with_validated_sources_and_media():
    topics = [
        _topic("A happened", "alpha", media_id="222", source_id="111"),
        _topic("B happened", "beta", media_id="333", source_id="444"),
    ]
    llm = FakeLLM(
        select_response=json.dumps(
            {"clusters": [{"headline": "Merged", "candidate_indexes": [0, 1]}]}
        ),
        write_response=json.dumps(
            {
                "title": "Merged Story",
                "blocks": [
                    {
                        "text": "Alpha shipped [1].",
                        "source_message_ids": ["111", "999"],  # 999 invalid -> dropped
                        "media_message_ids": ["222", "888"],   # 888 invalid -> dropped
                    },
                    {
                        "text": "Beta followed [1].",
                        "source_message_ids": ["444"],
                        "media_message_ids": ["333"],
                    },
                ],
            }
        ),
    )
    items = asyncio.run(curate_digest_stories(topics, llm, max_stories=5))
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Merged Story"
    # intro block
    assert item["mainText"] == "Alpha shipped [1]."
    assert item["mainMediaMessageId"] == "222"
    assert item["message_id"] == "111"
    assert item["_source_ids"] == ["111"]  # 999 dropped
    # supporting block -> subTopic
    sub = item["subTopics"][0]
    assert sub["text"] == "Beta followed [1]."
    assert sub["subTopicMediaMessageIds"] == ["333"]
    assert sub["message_id"] == "444"
    assert sub["_source_ids"] == ["444"]


def test_story_to_item_dedups_media_across_blocks():
    from src.features.summarising.daily_digest import _story_to_item
    story = {
        "title": "T",
        "blocks": [
            {"text": "lead", "source_message_ids": [], "media_message_ids": ["m1"]},
            {"text": "b1", "source_message_ids": [], "media_message_ids": ["m1", "m2"]},  # m1 repeats
            {"text": "b2", "source_message_ids": [], "media_message_ids": ["m2", "m3"]},  # m2 repeats
        ],
    }
    item = _story_to_item(story, {"m1", "m2", "m3"}, set())
    used = [item["mainMediaMessageId"]] + [
        m for s in item["subTopics"] for m in s["subTopicMediaMessageIds"]
    ]
    used = [u for u in used if u]
    assert used == ["m1", "m2", "m3"]  # each media used once, first-seen order


def test_story_to_item_caps_total_media_per_story():
    from src.features.summarising.daily_digest import _story_to_item, DEFAULT_DIGEST_MAX_MEDIA_PER_STORY
    story = {
        "title": "T",
        "blocks": [
            {"text": "lead", "source_message_ids": [], "media_message_ids": []},
            {"text": "a", "source_message_ids": [], "media_message_ids": ["m1", "m2"]},
            {"text": "b", "source_message_ids": [], "media_message_ids": ["m3", "m4", "m5"]},
        ],
    }
    item = _story_to_item(story, {f"m{i}" for i in range(1, 6)}, set())
    used = [item["mainMediaMessageId"]] + [
        m for s in item["subTopics"] for m in s["subTopicMediaMessageIds"]
    ]
    used = [u for u in used if u]
    assert len(used) == DEFAULT_DIGEST_MAX_MEDIA_PER_STORY == 3


def test_post_digest_dedups_repeated_media_urls():
    channel = FakeChannel()
    storage = FakeStorage()
    same = [{"url": "https://x/a.png", "type": "image"}]
    items = [
        {"title": "A", "mainText": "a", "mainMediaUrls": same, "subTopics": []},
        {"title": "B", "mainText": "b", "mainMediaUrls": same, "subTopics": []},
    ]
    asyncio.run(post_digest(FakeBot(channel), items, 1, storage))
    media_sends = [s for s in channel.sends if s.get("files")]
    assert len(media_sends) == 1  # the duplicate URL in story B is skipped


def test_resolve_source_metadata_and_inline_citations():
    from src.features.summarising.daily_digest import (
        apply_citations,
        resolve_source_metadata,
    )

    class MetaStorage:
        def get_topic_editor_source_messages(self, ids, guild_id=None, environment="prod", limit=50):
            rows = {
                "111": {"message_id": "111", "guild_id": 7, "channel_id": 70, "thread_id": None},
                "444": {"message_id": "444", "guild_id": 7, "channel_id": 71, "thread_id": 99},
            }
            return [rows[i] for i in ids if i in rows]

    items = [
        {
            "title": "S",
            "mainText": "Alpha shipped [1].",
            "message_id": "111",
            "channel_id": None,
            "_source_ids": ["111"],
            "subTopics": [
                {"text": "Beta in a thread [1].", "message_id": "444", "channel_id": None,
                 "subTopicMediaMessageIds": [], "_source_ids": ["444"]}
            ],
        }
    ]
    meta = asyncio.run(resolve_source_metadata(items, MetaStorage(), guild_id=7))
    assert meta["111"]["channel_id"] == 70
    # legacy channel_id filled from source metadata
    assert items[0]["channel_id"] == "70"  # stored as string to match legacy schema
    assert items[0]["subTopics"][0]["channel_id"] == "71"

    apply_citations(items, meta, 7)
    assert items[0]["mainText"] == "Alpha shipped [[1]](https://discord.com/channels/7/70/111)."
    # thread_id routes the URL to the thread
    assert items[0]["subTopics"][0]["text"] == (
        "Beta in a thread [[1]](https://discord.com/channels/7/99/444)."
    )


def test_inline_citations_normalize_double_bracket_markers():
    from src.features.summarising.daily_digest import _substitute_citations

    meta = {"111": {"guild_id": 7, "channel_id": 70, "thread_id": None}}
    # the model sometimes writes [[N]] despite the prompt — both forms must
    # render as the same masked jump-link
    assert _substitute_citations(
        "Claim [[1]].", ["111"], meta, 7
    ) == "Claim [[1]](https://discord.com/channels/7/70/111)."
    assert _substitute_citations(
        "Claim [1].", ["111"], meta, 7
    ) == "Claim [[1]](https://discord.com/channels/7/70/111)."
    # already-rendered masked links are never double-wrapped
    rendered = "Claim [[1]](https://discord.com/channels/7/70/111)."
    assert _substitute_citations(rendered, ["111"], meta, 7) == rendered


def test_curate_caps_at_max_stories():
    topics = [_topic(f"H{i}", f"body{i}", source_id=str(i)) for i in range(8)]
    # selection returns 8 clusters; curate must cap the WRITE stage at max_stories
    clusters = [{"headline": f"S{i}", "candidate_indexes": [i]} for i in range(8)]
    llm = FakeLLM(
        select_response=json.dumps({"clusters": clusters}),
        write_response=json.dumps({"title": "S", "blocks": [{"text": "b", "source_message_ids": [], "media_message_ids": []}]}),
    )
    items = asyncio.run(curate_digest_stories(topics, llm, max_stories=5))
    assert len(items) == 5
    # only 5 write calls were made (1 select + 5 writes = 6 total)
    assert len(llm.calls) == 6


def test_curate_falls_back_to_uncurated_without_client():
    topics = [_topic("A", "alpha", source_id="111"), _topic("B", "beta", source_id="222")]
    items = asyncio.run(curate_digest_stories(topics, None, max_stories=5))
    # uncurated 1:1 mapping
    assert [it["title"] for it in items] == ["A", "B"]


def test_curate_falls_back_when_llm_raises():
    topics = [_topic("A", "alpha", source_id="111")]
    llm = FakeLLM(raise_exc=RuntimeError("boom"))
    items = asyncio.run(curate_digest_stories(topics, llm, max_stories=5))
    assert [it["title"] for it in items] == ["A"]


def test_curate_falls_back_on_unparseable_output():
    topics = [_topic("A", "alpha", source_id="111")]
    llm = FakeLLM(response="the model rambled but emitted no json")
    items = asyncio.run(curate_digest_stories(topics, llm, max_stories=5))
    assert [it["title"] for it in items] == ["A"]


def test_format_digest_header_is_dated_with_weekday():
    from src.features.summarising.daily_digest import _format_digest_header

    dt = datetime(2026, 5, 7, tzinfo=timezone.utc)
    # no leading zero on the day; weekday + month from the date itself
    assert _format_digest_header(dt) == f"# Daily Update - {dt.strftime('%A, %B')} 7"


def test_post_digest_emits_header_and_footer_jump_link():
    channel = FakeChannel()
    storage = FakeStorage()
    items = [{"title": "A", "mainText": "Body", "subTopics": []}]

    mapping = asyncio.run(
        post_digest(
            FakeBot(channel),
            items,
            555,
            storage,
            header="# Daily Update, Sunday, May 7",
            guild_id=999,
        )
    )

    assert channel.sends[0]["content"] == "# Daily Update, Sunday, May 7"
    assert "header" in mapping and "footer" in mapping
    header_id = mapping["header"][0]
    assert channel.sends[-1]["content"] == (
        "---\n\n**Click here to jump to the beginning of today's summary:** "
        f"https://discord.com/channels/999/555/{header_id}"
    )


def test_post_digest_no_footer_without_guild_or_header():
    channel = FakeChannel()
    items = [{"title": "A", "mainText": "Body", "subTopics": []}]
    mapping = asyncio.run(post_digest(FakeBot(channel), items, 555, FakeStorage()))
    assert "header" not in mapping
    assert "footer" not in mapping


def test_daily_digest_run_uses_curated_items_end_to_end():
    # source ids 222/444 exist in FakeStorage so citations + channel_id resolve
    topics = [
        _topic("Raw One", "one", media_id="222", source_id="222"),
        _topic("Raw Two", "two", media_id="333", source_id="444"),
    ]
    storage = FakeStorage(topics=topics)
    channel = FakeChannel()
    llm = FakeLLM(
        select_response=json.dumps(
            {"clusters": [{"headline": "Curated", "candidate_indexes": [0, 1]}]}
        ),
        write_response=json.dumps(
            {
                "title": "Curated Headline",
                "blocks": [
                    {"text": "Lead point [1].", "source_message_ids": ["222"],
                     "media_message_ids": ["222"]},
                    {"text": "Follow up [1].", "source_message_ids": ["444"],
                     "media_message_ids": ["333"]},
                ],
            }
        ),
    )

    result = asyncio.run(
        daily_digest_run(
            FakeBot(channel),
            storage,
            guild_id=1,
            channel_id=1507878962515148952,
            environment="prod",
            now=datetime(2026, 5, 24, tzinfo=timezone.utc),
            llm_client=llm,
            model="deepseek-v4-pro",
        )
    )

    assert result["status"] == "ok"
    assert result["items_posted"] == 1
    assert llm.calls and llm.calls[0]["model"] == "deepseek-v4-pro"

    # header first (dash format), story headline after, footer last
    assert channel.sends[0]["content"].startswith("# Daily Update - ")
    assert any((s["content"] or "").startswith("## Curated Headline") for s in channel.sends)
    assert channel.sends[-1]["content"].startswith(
        "---\n\n**Click here to jump to the beginning of today's summary:**"
    )
    # inline citation rendered as a masked jump-link to the source message
    assert any(
        "[[1]](https://discord.com/channels/1/12/222)" in (s["content"] or "")
        for s in channel.sends
    )

    # stored row matches the legacy schema: source refs present, transient stripped
    item = storage.rows[0]["full_summary"][0]
    assert item["title"] == "Curated Headline"
    assert item["message_id"] == "222"
    assert item["channel_id"] == "12"  # filled from source metadata (string, legacy parity)
    assert "_source_ids" not in item
    assert all("_source_ids" not in sub for sub in item["subTopics"])
    # website community section filters on these flags — must be set
    assert item["included_in_main"] is True
    assert all(sub["included_in_main"] is True for sub in item["subTopics"])
    assert "[[1]](https://discord.com/channels/1/12/222)" in item["mainText"]
    json.dumps(item)  # legacy row must be JSON-serializable
