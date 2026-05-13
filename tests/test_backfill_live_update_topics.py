from scripts import backfill_live_update_topics as backfill


class FakeTable:
    def __init__(self, parent, name):
        self.parent = parent
        self.name = name
        self.payload = None
        self.on_conflict = None

    def upsert(self, payload, on_conflict=None):
        self.payload = payload
        self.on_conflict = on_conflict
        self.parent.calls.append((self.name, payload, on_conflict))
        return self

    def execute(self):
        if self.name == "topics":
            return type("Result", (), {"data": [{**self.payload, "topic_id": "topic-1"}]})()
        return type("Result", (), {"data": [self.payload]})()


class FakeSupabase:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return FakeTable(self, name)


def test_backfill_uses_runtime_canonicalizer_and_topic_source_conflicts():
    sb = FakeSupabase()
    rows = [{
        "feed_item_id": "feed-1",
        "guild_id": 1,
        "title": "The OmniNFT LoRA for LTX 2.3!",
        "body": "Demo body",
        "source_message_ids": [100, "101"],
        "duplicate_key": None,
        "discord_message_ids": ["9001"],
        "status": "posted",
        "posted_at": "2026-05-13T10:00:00Z",
        "author_context_snapshot": {"username": "gleb"},
    }]

    result = backfill.backfill_rows(sb, rows, backfill.topic_from_feed_item, "prod", dry_run=False)

    assert result == {"topics": 1, "sources": 2}
    topic_call = sb.calls[0]
    assert topic_call[0] == "topics"
    assert topic_call[2] == "environment,guild_id,canonical_key"
    assert topic_call[1]["canonical_key"] == "the-omninft-lora-for-ltx-2-3"
    assert topic_call[1]["state"] == "posted"
    assert topic_call[1]["publication_status"] == "sent"
    assert [call[2] for call in sb.calls[1:]] == ["topic_id,message_id", "topic_id,message_id"]


def test_backfill_prints_posted_and_watching_parity(monkeypatch, capsys):
    class Query:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args):
            return self

        def range(self, *_args):
            return self

        def execute(self):
            return type("Result", (), {"data": self.rows})()

    class Client(FakeSupabase):
        def table(self, name):
            if name == "live_update_feed_items":
                return Query([{"guild_id": 1, "title": "Posted", "source_message_ids": [1]}])
            if name == "live_update_watchlist":
                return Query([{"guild_id": 1, "title": "Watching", "source_message_ids": [2]}])
            return super().table(name)

    monkeypatch.setattr(backfill, "get_client", lambda: Client())
    exit_code = backfill.main([])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "posted legacy_rows=1 topics_upserted=1 sources_upserted=1" in output
    assert "watching legacy_rows=1 topics_upserted=1 sources_upserted=1" in output
