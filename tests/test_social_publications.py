from typing import Optional

from src.common.db_handler import DatabaseHandler
from src.common.server_config import ServerConfig
from src.features.sharing.sharer import Sharer


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeRPC:
    def __init__(self, supabase, name, params):
        self.supabase = supabase
        self.name = name
        self.params = params

    def execute(self):
        self.supabase.rpc_calls.append((self.name, self.params))
        return FakeResult(self.supabase.rpc_results.get(self.name, []))


class FakeQuery:
    def __init__(self, supabase, table_name):
        self.supabase = supabase
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.limit_value = None
        self.range_start = None
        self.range_end = None
        self.order_key = None
        self.order_desc = False

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

    def eq(self, key, value):
        self.filters.append(lambda row, k=key, v=value: row.get(k) == v)
        return self

    def is_(self, key, value):
        if value == "null":
            self.filters.append(lambda row, k=key: row.get(k) is None)
        return self

    def order(self, key, desc=False):
        self.order_key = key
        self.order_desc = desc
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def range(self, start, end):
        self.range_start = start
        self.range_end = end
        return self

    def execute(self):
        table = self.supabase.tables.setdefault(self.table_name, [])

        if self.operation == "insert":
            row = dict(self.payload)
            if self.table_name == "social_publications" and "publication_id" not in row:
                row["publication_id"] = f"pub-{len(table) + 1}"
            table.append(row)
            return FakeResult([dict(row)])

        filtered = [row for row in table if all(check(row) for check in self.filters)]

        if self.operation == "update":
            for row in filtered:
                row.update(dict(self.payload))
            return FakeResult([dict(row) for row in filtered])

        if self.order_key:
            filtered = sorted(
                filtered,
                key=lambda row: row.get(self.order_key) or "",
                reverse=self.order_desc,
            )
        if self.range_start is not None and self.range_end is not None:
            filtered = filtered[self.range_start:self.range_end + 1]
        if self.limit_value is not None:
            filtered = filtered[:self.limit_value]
        return FakeResult([dict(row) for row in filtered])


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.rpc_calls = []
        self.rpc_results = {}

    def table(self, name):
        return FakeQuery(self, name)

    def rpc(self, name, params):
        return FakeRPC(self, name, params)


def build_db_handler(fake_supabase: FakeSupabase) -> DatabaseHandler:
    db_handler = object.__new__(DatabaseHandler)
    db_handler.supabase = fake_supabase
    db_handler.storage_handler = type("Storage", (), {"supabase_client": fake_supabase})()
    class ServerConfigStub:
        def get_enabled_servers(self, require_write=False):
            return [{"guild_id": 1}]

        def resolve_social_route(self, guild_id, channel_id, platform):
            if platform != "twitter":
                return None
            return {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "platform": platform,
                "target_type": "account",
                "target_id": "test_bot",
                "enabled": True,
            }

    db_handler.server_config = ServerConfigStub()
    db_handler._gate_check = lambda guild_id: guild_id is not None
    db_handler._serialize_supabase_value = lambda value: value

    def resolve_guild_id(publication_id: str) -> Optional[int]:
        for row in fake_supabase.tables.get("social_publications", []):
            if row.get("publication_id") == publication_id:
                return row.get("guild_id")
        return None

    db_handler._resolve_social_publication_guild_id = resolve_guild_id
    return db_handler


def test_database_handler_social_publication_crud_and_claim_flow():
    fake_supabase = FakeSupabase({"social_publications": []})
    fake_supabase.rpc_results["claim_due_social_publications"] = [
        {"publication_id": "pub-1", "status": "processing"}
    ]
    db_handler = build_db_handler(fake_supabase)

    created = db_handler.create_social_publication(
        {
            "guild_id": 1,
            "channel_id": 10,
            "message_id": 99,
            "user_id": 123,
            "platform": "twitter",
            "action": "post",
            "status": "queued",
            "created_at": "2026-04-09T12:00:00+00:00",
        },
        guild_id=1,
    )
    assert created["publication_id"] == "pub-1"

    fetched = db_handler.get_social_publication_by_id("pub-1", guild_id=1)
    assert fetched["message_id"] == 99

    by_message = db_handler.get_social_publications_for_message(
        99,
        guild_id=1,
        platform="twitter",
        action="post",
        status="queued",
    )
    assert [row["publication_id"] for row in by_message] == ["pub-1"]

    listed = db_handler.list_social_publications(guild_id=1, status="queued")
    assert [row["publication_id"] for row in listed] == ["pub-1"]

    assert db_handler.mark_social_publication_processing("pub-1", guild_id=1, attempt_count=2)
    fetched = db_handler.get_social_publication_by_id("pub-1", guild_id=1)
    assert fetched["status"] == "processing"
    assert fetched["attempt_count"] == 2

    assert db_handler.mark_social_publication_succeeded(
        "pub-1",
        guild_id=1,
        provider_ref="tweet-1",
        provider_url="https://x.com/example/status/1",
        delete_supported=True,
    )
    fetched = db_handler.get_social_publication_by_id("pub-1", guild_id=1)
    assert fetched["status"] == "succeeded"
    assert fetched["provider_ref"] == "tweet-1"
    assert fetched["delete_supported"] is True

    assert db_handler.mark_social_publication_failed("pub-1", "timeout", guild_id=1)
    fetched = db_handler.get_social_publication_by_id("pub-1", guild_id=1)
    assert fetched["status"] == "failed"
    assert fetched["last_error"] == "timeout"

    assert db_handler.mark_social_publication_cancelled("pub-1", guild_id=1)
    fetched = db_handler.get_social_publication_by_id("pub-1", guild_id=1)
    assert fetched["status"] == "cancelled"

    claimed = db_handler.claim_due_social_publications(limit=5)
    assert claimed == [{"publication_id": "pub-1", "status": "processing"}]
    assert fake_supabase.rpc_calls == [
        ("claim_due_social_publications", {"claim_limit": 5, "claim_guild_ids": [1]})
    ]


def test_server_config_route_fallback_exact_parent_and_default():
    supabase = FakeSupabase(
        {
            "server_config": [
                {"guild_id": 1, "enabled": True, "write_enabled": True, "default_sharing": True}
            ],
            "channel_effective_config": [
                {"channel_id": 10, "parent_id": None, "sharing_enabled": True},
                {"channel_id": 100, "parent_id": 10, "sharing_enabled": True},
            ],
            "discord_channels": [
                {"channel_id": 101, "parent_id": 10},
            ],
            "social_channel_routes": [
                {"id": "route-channel", "guild_id": 1, "channel_id": 100, "platform": "twitter", "enabled": True},
                {"id": "route-parent", "guild_id": 1, "channel_id": 10, "platform": "twitter", "enabled": True},
                {"id": "route-default", "guild_id": 1, "channel_id": None, "platform": "twitter", "enabled": True},
            ],
        }
    )

    server_config = ServerConfig(supabase)
    assert server_config.resolve_social_route(1, 100, "twitter")["id"] == "route-channel"
    assert server_config.resolve_social_route(1, 101, "x")["id"] == "route-parent"
    assert server_config.resolve_social_route(1, 999, "twitter")["id"] == "route-default"


def test_server_config_payment_route_resolution_returns_destinations_and_route_key():
    supabase = FakeSupabase(
        {
            "server_config": [
                {"guild_id": 1, "enabled": True, "write_enabled": True}
            ],
            "channel_effective_config": [
                {"channel_id": 10, "parent_id": None},
                {"channel_id": 100, "parent_id": 10},
            ],
            "discord_channels": [
                {"channel_id": 101, "parent_id": 10},
            ],
            "payment_channel_routes": [
                {
                    "id": "pay-parent",
                    "guild_id": 1,
                    "channel_id": 10,
                    "producer": "grants",
                    "enabled": True,
                    "route_config": {"use_source_thread": True},
                },
                {
                    "id": "pay-default",
                    "guild_id": 1,
                    "channel_id": None,
                    "producer": "grants",
                    "enabled": True,
                    "route_config": {
                        "confirm_channel_id": 500,
                        "confirm_thread_id": 501,
                        "notify_channel_id": 600,
                    },
                },
            ],
        }
    )

    server_config = ServerConfig(supabase)
    resolved_parent = server_config.resolve_payment_destinations(1, 101, "grants")
    assert resolved_parent == {
        "route_key": "pay-parent",
        "confirm_channel_id": 10,
        "confirm_thread_id": 101,
        "notify_channel_id": 10,
        "notify_thread_id": 101,
    }

    resolved_default = server_config.resolve_payment_destinations(1, 999, "grants")
    assert resolved_default == {
        "route_key": "pay-default",
        "confirm_channel_id": 500,
        "confirm_thread_id": 501,
        "notify_channel_id": 600,
        "notify_thread_id": None,
    }


def test_duplicate_lookup_is_action_aware_and_skips_deleted_and_reaction_bridge_rows():
    sharer = object.__new__(Sharer)
    sharer.db_handler = type(
        "DBStub",
        (),
        {
            "get_social_publications_for_message": lambda self, **kwargs: [
                {"publication_id": "pub-deleted", "deleted_at": "2026-04-09T12:00:00+00:00", "source_kind": "admin_chat"},
                {"publication_id": "pub-reaction", "deleted_at": None, "source_kind": "reaction_bridge"},
                {"publication_id": "pub-good", "deleted_at": None, "source_kind": "admin_chat"},
            ]
        },
    )()

    assert sharer._find_existing_publication(99, 1, "twitter", "reply", "admin_chat") is None
    assert sharer._find_existing_publication(99, 1, "twitter", "post", "reaction_bridge") is None
    assert sharer._find_existing_publication(99, 1, "twitter", "post", "admin_chat")["publication_id"] == "pub-good"


def test_publication_id_lookup_is_unambiguous_for_delete_flows():
    fake_supabase = FakeSupabase(
        {
            "social_publications": [
                {"publication_id": "pub-1", "guild_id": 1, "message_id": 42, "provider_ref": "tweet-1"},
                {"publication_id": "pub-2", "guild_id": 1, "message_id": 42, "provider_ref": "tweet-2"},
            ]
        }
    )
    db_handler = build_db_handler(fake_supabase)

    publication = db_handler.get_social_publication_by_id("pub-2", guild_id=1)
    assert publication["provider_ref"] == "tweet-2"


# ── Sprint 3: DB query method tests ──────────────────────────────────


def test_find_existing_social_posts_by_topic():
    """find_existing_social_posts filters by topic_id in request_payload."""
    fake_supabase = FakeSupabase({
        "social_publications": [
            {
                "publication_id": "pub-a",
                "guild_id": 1,
                "platform": "twitter",
                "source_kind": "live_update_social",
                "request_payload": {
                    "source_context": {
                        "metadata": {"topic_id": "topic-1", "run_id": "run-1"},
                    },
                    "text": "First post about topic 1",
                },
                "status": "succeeded",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "publication_id": "pub-b",
                "guild_id": 1,
                "platform": "twitter",
                "source_kind": "live_update_social",
                "request_payload": {
                    "source_context": {
                        "metadata": {"topic_id": "topic-2", "run_id": "run-2"},
                    },
                    "text": "Post about topic 2",
                },
                "status": "succeeded",
                "created_at": "2026-01-02T00:00:00Z",
            },
        ],
    })
    db_handler = build_db_handler(fake_supabase)

    # Match topic-1
    results = db_handler.find_existing_social_posts(
        topic_id="topic-1",
        platform="twitter",
        guild_id=1,
    )
    assert len(results) == 1
    assert results[0]["publication_id"] == "pub-a"

    # No match for topic-3
    results = db_handler.find_existing_social_posts(
        topic_id="topic-3",
        platform="twitter",
        guild_id=1,
    )
    assert len(results) == 0

    # With draft_text for similarity
    results = db_handler.find_existing_social_posts(
        topic_id="topic-1",
        platform="twitter",
        guild_id=1,
        draft_text="First post about topic 1",
    )
    assert len(results) == 1
    assert "_similarity" in results[0]
    assert results[0]["_similarity"] == 1.0  # exact match


def test_check_content_similarity():
    """Character 5-gram containment similarity is case-insensitive and
    whitespace-normalized."""
    # Exact match
    assert DatabaseHandler.check_content_similarity(
        "Hello world this is a test",
        "Hello world this is a test",
    ) == 1.0

    # Case insensitive
    assert DatabaseHandler.check_content_similarity(
        "HELLO WORLD THIS IS A TEST",
        "hello world this is a test",
    ) == 1.0

    # Whitespace normalization
    assert DatabaseHandler.check_content_similarity(
        "Hello   world\nthis\tis  a   test",
        "Hello world this is a test",
    ) == 1.0

    # Short text in longer text (containment)
    score = DatabaseHandler.check_content_similarity(
        "Hello world",
        "Hello world this is a longer test message",
    )
    assert score == 1.0  # all 5-grams of short text in long text

    # Completely different texts
    score = DatabaseHandler.check_content_similarity(
        "The quick brown fox",
        "Completely unrelated text here",
    )
    assert score < 0.5

    # Empty texts
    assert DatabaseHandler.check_content_similarity("", "text") == 0.0
    assert DatabaseHandler.check_content_similarity("text", "") == 0.0
    assert DatabaseHandler.check_content_similarity("", "") == 0.0

    # Very short texts (< 5 chars) use substring containment
    assert DatabaseHandler.check_content_similarity("abc", "abcdef") == 1.0
    assert DatabaseHandler.check_content_similarity("xyz", "abcdef") == 0.0


def test_update_media_outcome():
    """update_social_publication_media_outcome persists media_attached and
    media_missing on a social_publications row."""
    fake_supabase = FakeSupabase({
        "social_publications": [
            {
                "publication_id": "pub-m",
                "guild_id": 1,
                "status": "succeeded",
                "media_attached": None,
                "media_missing": None,
            },
        ],
    })
    db_handler = build_db_handler(fake_supabase)

    media_attached = [{"identity": {"source": "discord_attachment", "index": 0}}]
    media_missing = [{"identity": {"source": "discord_embed", "slot": "image"}}]

    assert db_handler.update_social_publication_media_outcome(
        publication_id="pub-m",
        media_attached=media_attached,
        media_missing=media_missing,
        guild_id=1,
    )

    # Verify the row was updated
    pub = db_handler.get_social_publication_by_id("pub-m", guild_id=1)
    assert pub["media_attached"] == media_attached
    assert pub["media_missing"] == media_missing
