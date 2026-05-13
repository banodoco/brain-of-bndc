from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.features.admin_chat import agent as admin_agent
from src.features.admin_chat import tools as admin_tools


VALID_SOL_ADDRESS = "11111111111111111111111111111111"


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, supabase, table_name):
        self.supabase = supabase
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.limit_value = None

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

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append(lambda row, k=key, v=value: row.get(k) == v)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        table = self.supabase.tables.setdefault(self.table_name, [])

        if self.operation == "insert":
            row = dict(self.payload)
            row.setdefault("id", "route-{0}".format(len(table) + 1))
            table.append(row)
            return FakeResult([dict(row)])

        filtered = [row for row in table if all(check(row) for check in self.filters)]

        if self.operation == "update":
            for row in filtered:
                row.update(dict(self.payload))
            return FakeResult([dict(row) for row in filtered])

        if self.operation == "delete":
            deleted = [dict(row) for row in filtered]
            self.supabase.tables[self.table_name] = [row for row in table if row not in filtered]
            return FakeResult(deleted)

        if self.limit_value is not None:
            filtered = filtered[:self.limit_value]
        return FakeResult([dict(row) for row in filtered])


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def table(self, name):
        return FakeQuery(self, name)


class FakeReconcileService:
    def __init__(self, db_handler, *, decision, reason="decision reason", tx_signature="sig-1234", updated_status=None):
        self.db_handler = db_handler
        self.decision = decision
        self.reason = reason
        self.tx_signature = tx_signature
        self.updated_status = updated_status
        self.calls = []

    async def reconcile_with_chain(self, payment_id, *, guild_id=None):
        self.calls.append((payment_id, guild_id))
        row = self.db_handler.get_payment_request(payment_id, guild_id=guild_id)
        if row and self.updated_status:
            row["status"] = self.updated_status
        return SimpleNamespace(
            decision=self.decision,
            reason=self.reason,
            tx_signature=self.tx_signature,
        )


class FakePaymentDB:
    def __init__(self):
        self.requeue_calls = []
        self.hold_calls = []
        self.release_calls = []
        self.payment_routes = [
            {
                "id": "pay-default",
                "guild_id": 1,
                "channel_id": None,
                "producer": "grants",
                "enabled": True,
                "route_config": {"use_source_thread": True},
            }
        ]
        self.wallets = [
            {
                "wallet_id": "wallet-1",
                "guild_id": 1,
                "discord_user_id": 42,
                "chain": "solana",
                "wallet_address": "ABCDE12345FGHIJ67890",
                "verified_at": None,
            }
        ]
        self.payments = [
            {
                "payment_id": "pay-1",
                "guild_id": 1,
                "producer": "grants",
                "producer_ref": "thread-1",
                "wallet_id": "wallet-1",
                "recipient_discord_id": 42,
                "recipient_wallet": "ABCDE12345FGHIJ67890",
                "chain": "solana",
                "provider": "solana_native",
                "is_test": False,
                "route_key": "pay-default",
                "confirm_channel_id": 11,
                "notify_channel_id": 11,
                "amount_token": 1.25,
                "amount_usd": 200.0,
                "token_price_usd": 160.0,
                "status": "failed",
                "send_phase": "pre_submit",
                "tx_signature": None,
                "attempt_count": 1,
                "last_error": "rpc reset",
            }
        ]

    def list_payment_routes(self, guild_id, producer=None, channel_id=None, enabled=None, limit=100):
        rows = [row for row in self.payment_routes if row["guild_id"] == guild_id]
        if producer:
            rows = [row for row in rows if row["producer"] == producer]
        if channel_id is not None:
            rows = [row for row in rows if row["channel_id"] == channel_id]
        if enabled is not None:
            rows = [row for row in rows if bool(row["enabled"]) is enabled]
        return rows[:limit]

    def create_payment_route(self, data, guild_id=None):
        row = dict(data)
        row.setdefault("id", f"pay-route-{len(self.payment_routes) + 1}")
        row["guild_id"] = guild_id or row["guild_id"]
        self.payment_routes.append(row)
        return dict(row)

    def update_payment_route(self, route_id, data, guild_id=None):
        for row in self.payment_routes:
            if row["id"] == route_id and row["guild_id"] == guild_id:
                row.update(dict(data))
                return dict(row)
        return None

    def delete_payment_route(self, route_id, guild_id=None):
        for idx, row in enumerate(self.payment_routes):
            if row["id"] == route_id and row["guild_id"] == guild_id:
                return self.payment_routes.pop(idx)
        return None

    def list_wallets(self, guild_id, chain=None, discord_user_id=None, verified=None, limit=100):
        rows = [row for row in self.wallets if row["guild_id"] == guild_id]
        if chain:
            rows = [row for row in rows if row["chain"] == chain]
        if discord_user_id is not None:
            rows = [row for row in rows if row["discord_user_id"] == discord_user_id]
        if verified is True:
            rows = [row for row in rows if row.get("verified_at") is not None]
        if verified is False:
            rows = [row for row in rows if row.get("verified_at") is None]
        return rows[:limit]

    def list_payment_requests(
        self,
        guild_id,
        status=None,
        producer=None,
        recipient_discord_id=None,
        wallet_id=None,
        is_test=None,
        route_key=None,
        limit=100,
    ):
        rows = [row for row in self.payments if row["guild_id"] == guild_id]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if producer:
            rows = [row for row in rows if row["producer"] == producer]
        if recipient_discord_id is not None:
            rows = [row for row in rows if row["recipient_discord_id"] == recipient_discord_id]
        if wallet_id is not None:
            rows = [row for row in rows if row["wallet_id"] == wallet_id]
        if is_test is not None:
            rows = [row for row in rows if row["is_test"] == is_test]
        if route_key is not None:
            rows = [row for row in rows if row["route_key"] == route_key]
        return rows[:limit]

    def get_payment_request(self, payment_id, guild_id=None):
        for row in self.payments:
            if row["payment_id"] == payment_id and (guild_id is None or row["guild_id"] == guild_id):
                return row
        return None

    def requeue_payment(self, payment_id, guild_id=None):
        self.requeue_calls.append((payment_id, guild_id))
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row or row["status"] != "failed":
            return False
        row["status"] = "queued"
        row["last_error"] = None
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        self.hold_calls.append((payment_id, reason, guild_id))
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row:
            return False
        row["status"] = "manual_hold"
        row["last_error"] = reason
        return True

    def release_payment_hold(self, payment_id, new_status, guild_id=None, reason=None):
        self.release_calls.append((payment_id, new_status, guild_id, reason))
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row or row["status"] != "manual_hold":
            return False
        if new_status not in {"failed", "manual_hold"}:
            return False
        row["status"] = new_status
        if reason is not None:
            row["last_error"] = reason
        return True

    def cancel_payment(self, payment_id, guild_id=None, reason=None):
        row = self.get_payment_request(payment_id, guild_id=guild_id)
        if not row or row["status"] not in {"pending_confirmation", "queued", "failed"}:
            return False
        row["status"] = "cancelled"
        row["last_error"] = reason
        return True


@pytest.fixture
def fake_supabase(monkeypatch):
    supabase = FakeSupabase(
        {
            "social_channel_routes": [
                {
                    "id": "route-default",
                    "guild_id": 1,
                    "channel_id": None,
                    "platform": "twitter",
                    "enabled": True,
                    "route_config": {"account": "main"},
                }
            ]
        }
    )
    monkeypatch.setattr(admin_tools, "_get_supabase", lambda: supabase)
    monkeypatch.setattr(admin_tools, "_resolve_guild_id", lambda params=None: 1)
    return supabase


def test_expected_admin_tools_are_registered():
    registered = {tool["name"] for tool in admin_tools.TOOLS}

    assert {
        "list_social_routes",
        "create_social_route",
        "update_social_route",
        "delete_social_route",
        "list_payment_routes",
        "create_payment_route",
        "update_payment_route",
        "delete_payment_route",
        "list_wallets",
        "list_payments",
        "get_payment_status",
        "retry_payment",
        "hold_payment",
        "release_payment",
        "cancel_payment",
        "initiate_payment",
    }.issubset(registered)
    assert "payment_requests" not in admin_tools.QUERYABLE_TABLES
    assert "payment_channel_routes" not in admin_tools.QUERYABLE_TABLES
    assert "wallet_registry" not in admin_tools.QUERYABLE_TABLES


@pytest.mark.anyio
async def test_social_route_tool_crud_flow(fake_supabase):
    listed = await admin_tools.execute_list_social_routes({"platform": "x"})
    assert listed["success"] is True
    assert listed["count"] == 1
    assert listed["data"][0]["id"] == "route-default"

    created = await admin_tools.execute_create_social_route(
        {
            "platform": "twitter",
            "channel_id": "123",
            "enabled": True,
            "route_config": {"account": "alt"},
        }
    )
    assert created["success"] is True
    assert created["route"]["channel_id"] == 123
    assert created["route"]["route_config"] == {"account": "alt"}

    updated = await admin_tools.execute_update_social_route(
        {
            "route_id": created["route"]["id"],
            "channel_id": "",
            "enabled": False,
            "route_config": {"account": "archive"},
        }
    )
    assert updated["success"] is True
    assert updated["route"]["channel_id"] is None
    assert updated["route"]["enabled"] is False
    assert updated["route"]["route_config"] == {"account": "archive"}

    deleted = await admin_tools.execute_delete_social_route(
        {"route_id": created["route"]["id"]}
    )
    assert deleted["success"] is True
    assert deleted["route"]["id"] == created["route"]["id"]

    remaining_ids = {row["id"] for row in fake_supabase.tables["social_channel_routes"]}
    assert remaining_ids == {"route-default"}


@pytest.mark.anyio
async def test_create_social_route_validates_route_config(fake_supabase):
    result = await admin_tools.execute_create_social_route(
        {
            "platform": "twitter",
            "route_config": "not-a-dict",
        }
    )

    assert result["success"] is False
    assert "route_config must be a JSON object" in result["error"]


@pytest.mark.anyio
async def test_create_social_route_requires_account_for_twitter(fake_supabase):
    result = await admin_tools.execute_create_social_route(
        {
            "platform": "twitter",
            "route_config": {},
        }
    )

    assert result["success"] is False
    assert "route_config.account" in result["error"]
    assert "create_payment_route" in result["error"]


@pytest.mark.anyio
async def test_update_social_route_requires_account_for_twitter(fake_supabase):
    result = await admin_tools.execute_update_social_route(
        {
            "route_id": "route-default",
            "route_config": {},
        }
    )

    assert result["success"] is False
    assert "route_config.account" in result["error"]


def test_admin_prompt_steers_payment_requests_to_payment_tools():
    assert "Payment routing, payout confirmations, test payments, wallet collection" in admin_agent.SYSTEM_PROMPT


def test_prompt_renderer_preserves_literal_json_braces():
    rendered = admin_agent._render_prompt_template(
        admin_agent.SYSTEM_PROMPT,
        bot_user_id=123,
        guild_id=456,
        community_name="Banodoco",
        bot_voice="VOICE",
    )

    assert '{"account": "main"}' in rendered
    assert '{"use_source_thread": true}' in rendered
    assert "Banodoco" in rendered


@pytest.mark.anyio
async def test_payment_route_wallet_and_control_tools_are_redacted(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "99999")
    db_handler = FakePaymentDB()
    bot = SimpleNamespace(
        payment_service=FakeReconcileService(db_handler, decision="allow_requeue"),
    )

    listed_routes = await admin_tools.execute_list_payment_routes(db_handler, {"producer": "grants"})
    assert listed_routes["success"] is True
    assert listed_routes["data"][0]["id"] == "pay-default"

    created_route = await admin_tools.execute_create_payment_route(
        db_handler,
        {
            "producer": "grants",
            "channel_id": "123",
            "route_config": {"confirm_channel_id": 500},
        },
    )
    assert created_route["success"] is True
    assert created_route["route"]["channel_id"] == 123

    wallets = await admin_tools.execute_list_wallets(
        db_handler,
        {"chain": "solana", "verified": False},
    )
    assert wallets["success"] is True
    assert wallets["data"][0]["wallet_address"] == "ABCD...7890"

    payments = await admin_tools.execute_list_payments(
        db_handler,
        {"status": "failed", "producer": "grants"},
    )
    assert payments["success"] is True
    assert payments["data"][0]["recipient_wallet"] == "ABCD...7890"

    status = await admin_tools.execute_get_payment_status(db_handler, {"payment_id": "pay-1"})
    assert status["success"] is True
    assert status["payment"]["recipient_wallet"] == "ABCD...7890"

    retried = await admin_tools.execute_retry_payment(bot, db_handler, {"payment_id": "pay-1", "admin_user_id": "99999"})
    assert retried["success"] is True
    assert retried["payment"]["status"] == "queued"

    held = await admin_tools.execute_hold_payment(
        db_handler,
        {"payment_id": "pay-1", "reason": "needs review", "admin_user_id": "99999"},
    )
    assert held["success"] is True
    assert held["payment"]["status"] == "manual_hold"

    released = await admin_tools.execute_release_payment(
        bot,
        db_handler,
        {"payment_id": "pay-1", "new_status": "failed", "reason": "chain rejected", "admin_user_id": "99999"},
    )
    assert released["success"] is True
    assert released["payment"]["status"] == "failed"

    disallowed_release = await admin_tools.execute_release_payment(
        bot,
        db_handler,
        {"payment_id": "pay-1", "new_status": "confirmed", "admin_user_id": "99999"},
    )
    assert disallowed_release["success"] is False

    cancelled = await admin_tools.execute_cancel_payment(
        db_handler,
        {"payment_id": "pay-1", "reason": "operator cancelled", "admin_user_id": "99999"},
    )
    assert cancelled["success"] is True
    assert cancelled["payment"]["status"] == "cancelled"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("decision", "updated_status", "expected_success", "expected_status"),
    [
        ("reconciled_confirmed", "confirmed", True, "confirmed"),
        ("reconciled_failed", "failed", True, "failed"),
        ("allow_requeue", None, True, "queued"),
        ("keep_in_hold", None, False, "manual_hold"),
    ],
)
async def test_retry_payment_reconcile_gate(decision, updated_status, expected_success, expected_status, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "99999")
    db_handler = FakePaymentDB()
    payment_service = FakeReconcileService(
        db_handler,
        decision=decision,
        reason="chain says no",
        updated_status=updated_status,
    )
    bot = SimpleNamespace(payment_service=payment_service)

    result = await admin_tools.execute_retry_payment(bot, db_handler, {"payment_id": "pay-1", "admin_user_id": "99999"})

    assert payment_service.calls == [("pay-1", 1)]
    assert result["success"] is expected_success
    assert result["payment"]["status"] == expected_status
    if decision == "allow_requeue":
        assert db_handler.requeue_calls == [("pay-1", 1)]
        assert db_handler.hold_calls == []
    elif decision == "keep_in_hold":
        assert db_handler.hold_calls == [("pay-1", "chain says no", 1)]
        assert db_handler.requeue_calls == []
    else:
        assert db_handler.requeue_calls == []
        assert db_handler.hold_calls == []


@pytest.mark.anyio
async def test_retry_payment_fails_closed_when_payment_service_missing(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "99999")
    db_handler = FakePaymentDB()
    bot = SimpleNamespace()

    result = await admin_tools.execute_retry_payment(bot, db_handler, {"payment_id": "pay-1", "admin_user_id": "99999"})

    assert result == {"success": False, "error": "payment_service unavailable"}
    assert db_handler.requeue_calls == []
    assert db_handler.hold_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("decision", "updated_status", "expected_success", "expected_status"),
    [
        ("reconciled_confirmed", "confirmed", True, "confirmed"),
        ("reconciled_failed", "failed", True, "failed"),
        ("allow_requeue", None, True, "failed"),
        ("keep_in_hold", None, False, "manual_hold"),
    ],
)
async def test_release_payment_reconcile_gate(decision, updated_status, expected_success, expected_status, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "99999")
    db_handler = FakePaymentDB()
    db_handler.payments[0]["status"] = "manual_hold"
    payment_service = FakeReconcileService(
        db_handler,
        decision=decision,
        reason="operator must wait",
        updated_status=updated_status,
    )
    bot = SimpleNamespace(payment_service=payment_service)

    result = await admin_tools.execute_release_payment(
        bot,
        db_handler,
        {"payment_id": "pay-1", "new_status": "failed", "reason": "released", "admin_user_id": "99999"},
    )

    assert payment_service.calls == [("pay-1", 1)]
    assert result["success"] is expected_success
    assert result["payment"]["status"] == expected_status
    if decision == "allow_requeue":
        assert db_handler.release_calls == [("pay-1", "failed", 1, "released")]
        assert db_handler.hold_calls == []
    elif decision == "keep_in_hold":
        assert db_handler.hold_calls == [("pay-1", "operator must wait", 1)]
        assert db_handler.release_calls == []
    else:
        assert db_handler.release_calls == []
        assert db_handler.hold_calls == []


@pytest.mark.anyio
async def test_release_payment_fails_closed_when_payment_service_missing(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "99999")
    db_handler = FakePaymentDB()
    db_handler.payments[0]["status"] = "manual_hold"
    bot = SimpleNamespace()

    result = await admin_tools.execute_release_payment(
        bot,
        db_handler,
        {"payment_id": "pay-1", "new_status": "failed", "reason": "released", "admin_user_id": "99999"},
    )

    assert result == {"success": False, "error": "payment_service unavailable"}
    assert db_handler.release_calls == []
    assert db_handler.hold_calls == []


@pytest.mark.anyio
async def test_execute_tool_dispatches_payment_control_tools_with_bot(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "99999")
    db_handler = FakePaymentDB()
    payment_service = FakeReconcileService(db_handler, decision="allow_requeue")
    bot = SimpleNamespace(payment_service=payment_service)

    retry_result = await admin_tools.execute_tool(
        "retry_payment",
        {"payment_id": "pay-1", "admin_user_id": "99999"},
        bot,
        db_handler,
        sharer=None,
    )

    db_handler.payments[0]["status"] = "manual_hold"
    release_result = await admin_tools.execute_tool(
        "release_payment",
        {"payment_id": "pay-1", "new_status": "failed", "reason": "released", "admin_user_id": "99999"},
        bot,
        db_handler,
        sharer=None,
    )

    assert retry_result["success"] is True
    assert release_result["success"] is True
    assert payment_service.calls == [("pay-1", 1), ("pay-1", 1)]


@pytest.mark.anyio
async def test_query_payment_state_reads_canonical_db_rows():
    db_handler = FakePaymentDB()

    result = await admin_tools.execute_query_payment_state(
        db_handler,
        {
            "guild_id": 1,
            "payment_id": "pay-1",
            "user_id": 42,
            "wallet_address": "Injected111111111111111111111111111111",
        },
    )

    assert result["success"] is True
    assert result["payment"]["payment_id"] == "pay-1"
    assert result["payment"]["recipient_wallet"] == "ABCD...7890"
    assert result["user_id"] == 42
    assert result["count"] == 1
    assert result["payments"][0]["recipient_wallet"] == "ABCD...7890"


@pytest.mark.anyio
async def test_query_wallet_state_reads_wallet_registry_rows():
    db_handler = FakePaymentDB()
    db_handler.wallets[0]["verified_at"] = "2026-04-10T00:00:00Z"
    db_handler.wallets[0]["created_at"] = "2026-04-01T00:00:00Z"

    result = await admin_tools.execute_query_wallet_state(
        db_handler,
        {
            "guild_id": 1,
            "user_id": 42,
            "recipient_wallet": "Injected222222222222222222222222222222",
        },
    )

    assert result["success"] is True
    assert result["user_id"] == 42
    assert result["count"] == 1
    assert result["wallets"][0]["wallet_address"] == "ABCD...7890"
    assert result["wallets"][0]["verified_at"] == "2026-04-10T00:00:00Z"
    assert result["wallets"][0]["created_at"] == "2026-04-01T00:00:00Z"


@pytest.mark.anyio
async def test_list_recent_payments_reads_recent_rows_with_redaction():
    db_handler = FakePaymentDB()

    result = await admin_tools.execute_list_recent_payments(
        db_handler,
        {
            "guild_id": 1,
            "producer": "grants",
            "user_id": 42,
            "wallet_address": "Injected333333333333333333333333333333",
        },
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["payments"][0]["payment_id"] == "pay-1"
    assert result["payments"][0]["recipient_wallet"] == "ABCD...7890"


@pytest.mark.anyio
async def test_upsert_wallet_for_user_requires_admin_identity(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")

    class LocalWalletDB:
        def upsert_wallet(self, **kwargs):
            raise AssertionError("upsert should not run without admin identity")

    bot = SimpleNamespace(fetch_user=AsyncMock())

    result = await admin_tools.execute_upsert_wallet_for_user(
        bot,
        LocalWalletDB(),
        {
            "guild_id": 1,
            "admin_user_id": 123,
            "user_id": 42,
            "wallet_address": VALID_SOL_ADDRESS,
        },
    )

    assert result == {"success": False, "error": "Permission denied"}
    bot.fetch_user.assert_not_awaited()


@pytest.mark.anyio
async def test_upsert_wallet_for_user_dms_admin_and_leaves_wallet_unverified(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")

    class LocalWalletDB:
        def __init__(self):
            self.calls = []

        def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
            self.calls.append((guild_id, discord_user_id, chain, address, metadata))
            return {
                "wallet_id": "wallet-1",
                "guild_id": guild_id,
                "discord_user_id": discord_user_id,
                "chain": chain,
                "wallet_address": address,
                "verified_at": "2026-04-10T00:00:00Z",
                "created_at": "2026-04-01T00:00:00Z",
            }

    admin_user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(fetch_user=AsyncMock(return_value=admin_user))
    db_handler = LocalWalletDB()

    result = await admin_tools.execute_upsert_wallet_for_user(
        bot,
        db_handler,
        {
            "guild_id": 1,
            "admin_user_id": 999,
            "user_id": 42,
            "wallet_address": VALID_SOL_ADDRESS,
            "reason": "seed wallet",
        },
    )

    assert result["success"] is True
    assert db_handler.calls == [
        (
            1,
            42,
            "solana",
            VALID_SOL_ADDRESS,
            {"producer": "admin_chat", "source": "upsert_wallet_for_user", "reason": "seed wallet"},
        )
    ]
    assert result["wallet"]["verified_at"] is None
    bot.fetch_user.assert_awaited_once_with(999)
    admin_user.send.assert_awaited_once()
    assert "will be verified on next payment" in admin_user.send.await_args.args[0]


@pytest.mark.anyio
async def test_resolve_admin_intent_cascade_cancels_linked_payments(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")

    class LocalIntentDB:
        def __init__(self):
            self.intent = {
                "intent_id": "intent-1",
                "guild_id": 1,
                "recipient_user_id": 42,
                "status": "awaiting_admin_approval",
                "test_payment_id": "pay-test",
                "final_payment_id": "pay-final",
            }
            self.payments = {
                "pay-test": {"payment_id": "pay-test", "guild_id": 1, "status": "pending_confirmation"},
                "pay-final": {"payment_id": "pay-final", "guild_id": 1, "status": "queued"},
            }
            self.cancel_calls = []
            self.updated = []

        def get_admin_payment_intent(self, intent_id, guild_id):
            if intent_id == self.intent["intent_id"] and guild_id == self.intent["guild_id"]:
                return dict(self.intent)
            return None

        def get_payment_request(self, payment_id, guild_id=None):
            row = self.payments.get(payment_id)
            if row and guild_id is not None and row["guild_id"] != guild_id:
                return None
            return dict(row) if row else None

        def cancel_payment(self, payment_id, guild_id=None, reason=None):
            self.cancel_calls.append((payment_id, guild_id, reason))
            self.payments[payment_id]["status"] = "cancelled"
            self.payments[payment_id]["last_error"] = reason
            return True

        def update_admin_payment_intent(self, intent_id, payload, guild_id):
            self.intent.update(dict(payload))
            self.updated.append((intent_id, dict(payload), guild_id))
            return dict(self.intent)

    admin_user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(fetch_user=AsyncMock(return_value=admin_user))
    db_handler = LocalIntentDB()

    result = await admin_tools.execute_resolve_admin_intent(
        bot,
        db_handler,
        {
            "guild_id": 1,
            "admin_user_id": 999,
            "intent_id": "intent-1",
            "note": "operator cancel",
        },
    )

    assert result["success"] is True
    assert db_handler.cancel_calls == [
        ("pay-test", 1, "operator cancel"),
        ("pay-final", 1, "operator cancel"),
    ]
    assert db_handler.updated == [("intent-1", {"status": "cancelled"}, 1)]
    assert [payment["status"] for payment in result["payments"]] == ["cancelled", "cancelled"]
    bot.fetch_user.assert_awaited_once_with(999)
    admin_user.send.assert_awaited_once()
    assert "intent `intent-1` cancelled" in admin_user.send.await_args.args[0]


def test_agent_prompt_mentions_payment_tools():
    assert "initiate_payment" in admin_agent.SYSTEM_PROMPT
    assert "initiate_batch_payment" in admin_agent.SYSTEM_PROMPT
    assert "list_payment_routes" in admin_agent.SYSTEM_PROMPT
    assert "list_payments" in admin_agent.SYSTEM_PROMPT
    assert "release_payment" in admin_agent.SYSTEM_PROMPT
    assert "query_payment_state" in admin_agent.SYSTEM_PROMPT
    assert "query_wallet_state" in admin_agent.SYSTEM_PROMPT
    assert "list_recent_payments" in admin_agent.SYSTEM_PROMPT
    assert "upsert_wallet_for_user" in admin_agent.SYSTEM_PROMPT
    assert "resolve_admin_intent" in admin_agent.SYSTEM_PROMPT
    assert "State questions -> query first" in admin_agent.SYSTEM_PROMPT


def test_admin_tool_registration_and_identity_injection_sets():
    expected_admin_tools = {
        "query_payment_state",
        "query_wallet_state",
        "list_recent_payments",
        "initiate_batch_payment",
        "upsert_wallet_for_user",
        "resolve_admin_intent",
    }
    assert expected_admin_tools.issubset({tool["name"] for tool in admin_tools.TOOLS})
    assert admin_agent._ADMIN_IDENTITY_INJECTED_TOOLS == {
        "initiate_payment",
        "initiate_batch_payment",
        "upsert_wallet_for_user",
        "resolve_admin_intent",
        "cancel_payment",
        "hold_payment",
        "retry_payment",
        "release_payment",
        "mute_speaker",
        "unmute_speaker",
    }
    assert "initiate_batch_payment" in admin_agent._CHANNEL_POSTING_TOOLS
