import json

from scripts.export_hf_discord_dataset import (
    build_dataset_record,
    iter_messages,
    parse_jsonish,
    skip_reason,
    stable_hash,
)


def test_skip_reason_excludes_explicit_content_sharing_opt_out():
    row = {"author_id": "123", "content": "please do not export me"}

    reason = skip_reason(
        row,
        opted_out_author_ids={123},
        bot_author_ids=set(),
        include_empty=False,
    )

    assert reason == "opted_out"


def test_skip_reason_allows_unset_sharing_preference():
    row = {"author_id": "456", "content": "allowed by default"}

    reason = skip_reason(
        row,
        opted_out_author_ids={123},
        bot_author_ids=set(),
        include_empty=False,
    )

    assert reason is None


def test_skip_reason_excludes_bots_and_empty_messages_by_default():
    assert (
        skip_reason(
            {"author_id": 999, "content": "system notice"},
            opted_out_author_ids=set(),
            bot_author_ids={999},
            include_empty=False,
        )
        == "bot"
    )
    assert (
        skip_reason(
            {"author_id": 111, "content": "   "},
            opted_out_author_ids=set(),
            bot_author_ids=set(),
            include_empty=False,
        )
        == "empty"
    )


def test_build_dataset_record_hashes_ids_and_strips_attachment_urls_by_default():
    row = {
        "message_id": 10,
        "guild_id": 20,
        "channel_id": 30,
        "thread_id": None,
        "author_id": 40,
        "reference_id": 50,
        "content": "hello",
        "created_at": "2026-05-14T12:00:00+00:00",
        "edited_at": None,
        "reaction_count": 2,
        "reactors": json.dumps([1, 2]),
        "attachments": json.dumps(
            [
                {
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size": 123,
                    "url": "https://cdn.discordapp.com/private.png",
                }
            ]
        ),
    }

    record = build_dataset_record(
        row,
        channel={"channel_id": 30, "channel_name": "general", "category_id": 60, "nsfw": False},
        salt="test-salt",
        include_raw_ids=False,
        include_attachment_urls=False,
        include_jump_urls=False,
    )

    assert record["id"] == stable_hash(10, salt="test-salt", prefix="msg")
    assert record["author"]["id"] == stable_hash(40, salt="test-salt", prefix="user")
    assert record["channel"]["name"] == "general"
    assert record["reactor_count"] == 2
    assert record["attachment_count"] == 1
    assert record["attachments"] == [
        {"filename": "image.png", "content_type": "image/png", "size": 123}
    ]
    assert "raw" not in record
    assert "url" not in record["attachments"][0]


def test_parse_jsonish_returns_fallback_for_bad_json():
    assert parse_jsonish("{bad", []) == []


def test_iter_messages_pages_newest_first_by_message_id():
    class FakeResult:
        def __init__(self, rows):
            self.data = rows

    class FakeQuery:
        def __init__(self, batches, calls):
            self.batches = batches
            self.calls = calls

        def select(self, *_args):
            return self

        def order(self, column, desc=False):
            self.calls.append(("order", column, desc))
            return self

        def eq(self, *_args):
            return self

        def neq(self, *_args):
            return self

        def lt(self, column, value):
            self.calls.append(("lt", column, value))
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            return FakeResult(self.batches.pop(0))

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.batches = [
                [{"message_id": 30}, {"message_id": 20}],
                [{"message_id": 10}],
            ]

        def table(self, name):
            assert name == "discord_messages"
            return FakeQuery(self.batches, self.calls)

    client = FakeClient()

    rows = list(
        iter_messages(
            client,
            guild_id=None,
            start_date=None,
            end_date=None,
            include_deleted=False,
            batch_size=2,
        )
    )

    assert [row["message_id"] for row in rows] == [30, 20, 10]
    assert ("order", "message_id", True) in client.calls
    assert ("lt", "message_id", 20) in client.calls
