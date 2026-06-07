from types import SimpleNamespace

import pytest

import src.features.payments.payment_ui_cog as payment_ui_cog_module
import src.features.payments.payment_worker_cog as payment_worker_cog_module
import src.features.sharing.sharing_cog as sharing_cog_module
from src.features.grants.grants_cog import GrantsCog
from src.features.payments.payment_service import PaymentActor, PaymentActorKind
from src.features.sharing.models import SocialPublishResult


pytestmark = pytest.mark.anyio


class FakeSharer:
    def __init__(self, bot, db_handler, logger_instance):
        self.bot = bot
        self.db_handler = db_handler
        self.logger_instance = logger_instance


class FakeSupabaseResult:
    def __init__(self, data):
        self.data = data


class FakeSupabaseUpdate:
    def __init__(self, recorder, payload):
        self.recorder = recorder
        self.payload = payload
        self.filters = []

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def execute(self):
        self.recorder.append({"payload": self.payload, "filters": self.filters})
        return FakeSupabaseResult([])


class FakeSupabaseTable:
    def __init__(self, recorder):
        self.recorder = recorder

    def update(self, payload):
        return FakeSupabaseUpdate(self.recorder, payload)


class FakeSupabase:
    def __init__(self):
        self.updates = []

    def table(self, _name):
        return FakeSupabaseTable(self.updates)


class FakeDB:
    def __init__(self, claimed=None):
        self.claimed = claimed or []
        self.claim_limits = []
        self.supabase = FakeSupabase()

    def claim_due_social_publications(self, limit):
        self.claim_limits.append(limit)
        return list(self.claimed)


class FakeService:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    async def execute_publication(self, publication_id):
        self.executed.append(publication_id)
        return self.results.pop(0)


class FakeBot:
    def __init__(self, service):
        self.social_publish_service = service
        self.ready_waits = 0
        self._is_ready = False

    async def wait_until_ready(self):
        self.ready_waits += 1

    def is_ready(self):
        return self._is_ready


class FakePaymentChannel:
    def __init__(self):
        self.messages = []

    async def send(self, content, view=None):
        self.messages.append({"content": content, "view": view})
        return SimpleNamespace(content=content, view=view)


class FakePaymentDB:
    def __init__(self, claimed=None):
        self.claimed = claimed or []
        self.claim_limits = []
        self.payments = {}
        self.intents = {}
        self.cancel_payment_calls = []
        self.updated_intents = []

    def claim_due_payment_requests(self, limit):
        self.claim_limits.append(limit)
        return list(self.claimed)

    def get_payment_request(self, payment_id, guild_id=None):
        payment = self.payments.get(payment_id)
        if payment and guild_id is not None and payment.get("guild_id") != guild_id:
            return None
        return payment

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        payment = self.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return False
        payment["status"] = "manual_hold"
        payment["last_error"] = reason
        return True

    def get_admin_payment_intent(self, intent_id, guild_id):
        intent = self.intents.get(intent_id)
        if intent and guild_id is not None and intent.get("guild_id") != guild_id:
            return None
        return intent

    def list_active_intents(self, guild_id):
        return [
            intent
            for intent in self.intents.values()
            if (guild_id is None or intent.get("guild_id") == guild_id)
            and intent.get("status") not in {"completed", "failed", "cancelled"}
        ]

    def update_admin_payment_intent(self, intent_id, payload, guild_id):
        intent = self.get_admin_payment_intent(intent_id, guild_id=guild_id)
        if not intent:
            return None
        intent.update(payload)
        self.updated_intents.append((intent_id, dict(payload), guild_id))
        return intent

    def cancel_payment(self, payment_id, guild_id=None, reason=None):
        payment = self.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return False
        payment["status"] = "cancelled"
        payment["cancel_reason"] = reason
        self.cancel_payment_calls.append((payment_id, guild_id, reason))
        return True


class FakePaymentService:
    def __init__(self, pending=None, recovered=None, execute_results=None, reconcile_results=None, db_handler=None):
        self.pending = pending or []
        self.recovered = recovered or []
        self.execute_results = list(execute_results or [])
        self.reconcile_results = list(reconcile_results or [])
        self.db_handler = db_handler
        self.execute_calls = []
        self.recover_calls = []
        self.migrate_calls = []
        self.reconcile_calls = []

    def get_pending_confirmation_payments(self, guild_ids=None):
        return list(self.pending)

    async def recover_inflight(self, guild_ids=None):
        self.recover_calls.append(guild_ids)
        return list(self.recovered)

    async def execute_payment(self, payment_id, guild_id=None):
        self.execute_calls.append((payment_id, guild_id))
        return self.execute_results.pop(0)

    async def reconcile_with_chain(self, payment_id, guild_id=None):
        self.reconcile_calls.append((payment_id, guild_id))
        result = self.reconcile_results.pop(0)
        row = self.db_handler.get_payment_request(payment_id, guild_id=guild_id) if self.db_handler else None
        if row and hasattr(result, "updated_status") and result.updated_status:
            row["status"] = result.updated_status
        return result

    def migrate_legacy_provider_rows(self, guild_ids=None):
        self.migrate_calls.append(guild_ids)
        return 0


class FakeProducerCog:
    def __init__(self):
        self.handled = []

    async def handle_payment_result(self, payment):
        self.handled.append(payment["payment_id"])


class FakePaymentBot:
    def __init__(self, payment_service, channel=None, producer_cog=None, payment_ui_cog=None):
        self.payment_service = payment_service
        self.ready_waits = 0
        self.added_views = []
        self.channel = channel or FakePaymentChannel()
        self.producer_cog = producer_cog
        self.payment_ui_cog = payment_ui_cog
        self.db_handler = None
        self.claude_client = object()
        self.guilds = []
        self._is_ready = False

    async def wait_until_ready(self):
        self.ready_waits += 1

    def is_ready(self):
        return self._is_ready

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))

    def get_cog(self, name):
        if name == "PaymentUICog":
            return self.payment_ui_cog
        if name == "GrantsCog":
            return self.producer_cog
        return None

    def get_channel(self, channel_id):
        if channel_id == getattr(self.channel, "id", 999):
            return self.channel
        return None

    async def fetch_channel(self, channel_id):
        if channel_id == getattr(self.channel, "id", 999):
            return self.channel
        raise RuntimeError("unknown channel")


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    async def defer(self, ephemeral=False):
        self.deferred = ephemeral

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeInteractionFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeInteractionMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, user_id, guild_id=1):
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeInteractionResponse()
        self.followup = FakeInteractionFollowup()
        self.message = FakeInteractionMessage()


class FakeGrantsDB:
    def __init__(self):
        self.server_config = SimpleNamespace(
            get_first_server_with_field=lambda field, require_write=False: {"guild_id": 1, "grants_channel_id": 10},
            resolve_payment_destinations=lambda guild_id, channel_id, producer: None,
        )
        self.storage_handler = None
        self.wallet_calls = []
        self.status_updates = []
        self.recorded_payments = []
        self.grant = {
            "thread_id": 1001,
            "guild_id": 1,
            "applicant_id": 222,
            "total_cost_usd": 42.5,
            "gpu_type": "a10",
            "recommended_hours": 5,
            "status": "payment_requested",
        }

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        self.wallet_calls.append((guild_id, discord_user_id, chain, address, metadata))
        return {"wallet_id": "wallet-1", "wallet_address": address}

    def update_grant_status(self, thread_id, status, guild_id=None, **kwargs):
        self.grant["status"] = status
        self.grant.update(kwargs)
        self.status_updates.append((thread_id, status, guild_id, kwargs))
        return True

    def get_grant_by_thread(self, thread_id, guild_id=None):
        if thread_id == self.grant["thread_id"] and (guild_id is None or guild_id == self.grant["guild_id"]):
            return dict(self.grant)
        return None

    def record_grant_payment(self, thread_id, tx_signature, sol_amount, sol_price_usd, guild_id=None):
        self.recorded_payments.append((thread_id, tx_signature, sol_amount, sol_price_usd, guild_id))
        self.grant["status"] = "paid"
        return True


class FakeGrantThread:
    def __init__(self, thread_id=1001, parent_id=10):
        self.id = thread_id
        self.parent_id = parent_id
        self.guild = SimpleNamespace(id=1)
        self.messages = []
        self.edits = []

    async def send(self, content):
        self.messages.append(content)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeGrantPaymentService:
    def __init__(self):
        self.request_calls = []
        self.confirm_calls = []

    async def request_payment(self, **kwargs):
        self.request_calls.append(kwargs)
        return {
            "payment_id": "pay-test" if kwargs["is_test"] else "pay-final",
            "status": "pending_confirmation",
            **kwargs,
        }

    def confirm_payment(self, payment_id, *, actor, guild_id=None):
        self.confirm_calls.append((payment_id, {"guild_id": guild_id, "actor": actor}))
        return {"payment_id": payment_id, "status": "queued"}


class FakeGrantPaymentUICog:
    def __init__(self):
        self.sent = []

    async def send_confirmation_request(self, payment_id):
        self.sent.append(payment_id)
        return SimpleNamespace(id=payment_id)


async def test_scheduler_lifecycle_starts_and_stops_via_cog_load(monkeypatch):
    monkeypatch.setattr(sharing_cog_module, "Sharer", FakeSharer)

    db_handler = FakeDB()
    service = FakeService([])
    bot = FakeBot(service)
    cog = sharing_cog_module.SharingCog(bot, db_handler, social_publish_service=service)

    calls = []
    monkeypatch.setattr(cog.scheduled_publication_worker, "is_running", lambda: False)
    monkeypatch.setattr(cog.scheduled_publication_worker, "start", lambda: calls.append("start"))
    await cog.cog_load()

    monkeypatch.setattr(cog.scheduled_publication_worker, "is_running", lambda: True)
    monkeypatch.setattr(cog.scheduled_publication_worker, "cancel", lambda: calls.append("cancel"))
    cog.cog_unload()

    assert calls == ["start", "cancel"]


async def test_scheduler_claims_executes_and_waits_for_bot_ready(monkeypatch):
    monkeypatch.setattr(sharing_cog_module, "Sharer", FakeSharer)

    publication = {"publication_id": "pub-1", "platform": "twitter", "action": "post", "attempt_count": 1}
    db_handler = FakeDB(claimed=[publication])
    service = FakeService([SocialPublishResult(publication_id="pub-1", success=True)])
    bot = FakeBot(service)
    cog = sharing_cog_module.SharingCog(bot, db_handler, social_publish_service=service)

    await cog._before_scheduled_publication_worker()
    await cog.scheduled_publication_worker.coro(cog)

    assert bot.ready_waits == 1
    assert db_handler.claim_limits == [cog.claim_batch_size]
    assert service.executed == ["pub-1"]


async def test_scheduler_retries_transient_failures_and_respects_retry_budget(monkeypatch):
    monkeypatch.setattr(sharing_cog_module, "Sharer", FakeSharer)

    db_handler = FakeDB()
    service = FakeService(
        [
            SocialPublishResult(publication_id="pub-2", success=False, error="timeout from provider"),
            SocialPublishResult(publication_id="pub-3", success=False, error="timeout from provider"),
        ]
    )
    bot = FakeBot(service)
    cog = sharing_cog_module.SharingCog(bot, db_handler, social_publish_service=service)
    cog.max_attempts = 3
    cog.retry_delay_seconds = 60

    await cog._process_claimed_publication(
        {
            "publication_id": "pub-2",
            "guild_id": 1,
            "platform": "twitter",
            "action": "post",
            "attempt_count": 1,
        }
    )
    await cog._process_claimed_publication(
        {
            "publication_id": "pub-3",
            "guild_id": 1,
            "platform": "twitter",
            "action": "post",
            "attempt_count": 3,
        }
    )

    assert service.executed == ["pub-2", "pub-3"]
    assert len(db_handler.supabase.updates) == 1
    update = db_handler.supabase.updates[0]
    assert update["payload"]["status"] == "queued"
    assert update["payload"]["last_error"] == "timeout from provider"
    assert ("publication_id", "pub-2") in update["filters"]
    assert ("guild_id", 1) in update["filters"]


async def test_payment_scheduler_lifecycle_registers_views_and_starts_worker():
    payment_service = FakePaymentService(
        pending=[
            {
                "payment_id": "pay-pending",
                "guild_id": 1,
                "status": "pending_confirmation",
            }
        ],
        recovered=[],
    )
    db_handler = FakePaymentDB()
    db_handler.server_config = None
    bot = FakePaymentBot(payment_service)
    ui_cog = payment_ui_cog_module.PaymentUICog(bot, db_handler, payment_service=payment_service)
    worker_cog = payment_worker_cog_module.PaymentWorkerCog(bot, db_handler, payment_service=payment_service)

    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_cog.payment_worker, "is_running", lambda: False)
    monkeypatch.setattr(worker_cog.payment_worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(worker_cog.payment_worker, "change_interval", lambda **kwargs: calls.append(kwargs["seconds"]))

    await ui_cog.cog_load()
    assert len(bot.added_views) == 1
    await worker_cog.cog_load()

    monkeypatch.setattr(worker_cog.payment_worker, "is_running", lambda: True)
    monkeypatch.setattr(worker_cog.payment_worker, "cancel", lambda: calls.append("cancel"))
    worker_cog.cog_unload()
    monkeypatch.undo()

    assert calls == [worker_cog.worker_interval_seconds, "start", "cancel"]
    view, message_id = bot.added_views[0]
    assert isinstance(view, payment_ui_cog_module.PaymentConfirmView)
    assert view.payment_id == "pay-pending"
    assert message_id is None


async def test_payment_scheduler_claims_executes_notifies_and_hands_off():
    claimed = {"payment_id": "pay-1", "guild_id": 1}
    terminal_payment = {
        "payment_id": "pay-1",
        "guild_id": 1,
        "producer": "grants",
        "producer_ref": "thread-1",
        "recipient_wallet": "ABCDE12345FGHIJ67890",
        "chain": "solana",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.25,
        "status": "confirmed",
        "notify_channel_id": 999,
        "notify_thread_id": None,
        "tx_signature": "sig-123",
        "last_error": None,
    }
    db_handler = FakePaymentDB(claimed=[claimed])
    payment_service = FakePaymentService(execute_results=[terminal_payment])
    producer_cog = FakeProducerCog()
    bot = FakePaymentBot(payment_service, producer_cog=producer_cog)
    cog = payment_worker_cog_module.PaymentWorkerCog(bot, db_handler, payment_service=payment_service)

    await cog.payment_worker.coro(cog)

    assert db_handler.claim_limits == [cog.claim_batch_size]
    assert payment_service.execute_calls == [("pay-1", 1)]
    assert len(bot.channel.messages) == 1
    assert "Payment Confirmed" in bot.channel.messages[0]["content"]
    assert producer_cog.handled == ["pay-1"]


async def test_payment_scheduler_replays_pending_terminal_handoff_on_ready():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    bot = FakePaymentBot(payment_service, producer_cog=None)
    cog = payment_worker_cog_module.PaymentWorkerCog(bot, db_handler, payment_service=payment_service)
    payment = {
        "payment_id": "pay-recovered",
        "guild_id": 1,
        "producer": "grants",
        "producer_ref": "thread-1",
        "status": "confirmed",
    }

    await cog._handoff_terminal_result(payment)
    assert "pay-recovered" in cog._pending_terminal_handoffs

    producer_cog = FakeProducerCog()
    bot.producer_cog = producer_cog
    await cog.on_ready()

    assert producer_cog.handled == ["pay-recovered"]
    assert cog._pending_terminal_handoffs == {}


async def test_payment_worker_cog_load_does_not_block_on_wait_until_ready():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    bot = FakePaymentBot(payment_service)
    cog = payment_worker_cog_module.PaymentWorkerCog(bot, db_handler, payment_service=payment_service)

    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cog.payment_worker, "is_running", lambda: False)
    monkeypatch.setattr(cog.payment_worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(cog.payment_worker, "change_interval", lambda **kwargs: calls.append(kwargs["seconds"]))

    await cog.cog_load()
    monkeypatch.undo()

    assert bot.ready_waits == 0
    assert calls == [cog.worker_interval_seconds, "start"]


async def test_payment_worker_cog_load_runs_legacy_provider_migration_once():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    db_handler.server_config = SimpleNamespace(
        get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "write_enabled": True}],
    )
    bot = FakePaymentBot(payment_service)
    cog = payment_worker_cog_module.PaymentWorkerCog(bot, db_handler, payment_service=payment_service)

    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cog.payment_worker, "is_running", lambda: False)
    monkeypatch.setattr(cog.payment_worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(cog.payment_worker, "change_interval", lambda **kwargs: calls.append(kwargs["seconds"]))

    await cog.cog_load()
    monkeypatch.undo()

    assert payment_service.migrate_calls == [[1]]
    assert calls == [cog.worker_interval_seconds, "start"]


async def test_payment_confirm_view_rejects_non_recipient():
    payment_service = FakePaymentService()
    db_handler = FakePaymentDB()
    db_handler.payments["pay-1"] = {
        "payment_id": "pay-1",
        "guild_id": 1,
        "status": "pending_confirmation",
        "recipient_discord_id": 123,
    }
    bot = FakePaymentBot(payment_service)
    cog = payment_ui_cog_module.PaymentUICog(bot, db_handler, payment_service=payment_service)
    view = payment_ui_cog_module.PaymentConfirmView(cog, "pay-1")
    interaction = FakeInteraction(user_id=999)

    await view._confirm_button_pressed(interaction)

    assert interaction.followup.messages == [("Only the intended recipient can confirm this payment.", True)]
    assert payment_service.execute_calls == []


async def test_payment_resolve_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    db_handler = FakePaymentDB()
    db_handler.payments["pay-1"] = {
        "payment_id": "pay-1",
        "guild_id": 1,
        "status": "manual_hold",
        "tx_signature": "ABCDEFGH12345678",
    }
    payment_service = FakePaymentService(db_handler=db_handler)
    bot = FakePaymentBot(payment_service)
    cog = payment_ui_cog_module.PaymentUICog(bot, db_handler, payment_service=payment_service)
    interaction = FakeInteraction(user_id=123)

    await cog.payment_resolve.callback(cog, interaction, "pay-1")

    assert interaction.response.messages == [("admin-only", True)]
    assert payment_service.reconcile_calls == []


@pytest.mark.parametrize(
    ("decision", "reason", "updated_status", "initial_status"),
    [
        ("reconciled_confirmed", "chain reported confirmed during reconcile", "confirmed", "manual_hold"),
        ("reconciled_failed", "chain reported failed during reconcile", "failed", "manual_hold"),
        ("allow_requeue", "beyond 150s blockhash safety window", None, "failed"),
        ("keep_in_hold", "RPC unreachable during reconcile", None, "manual_hold"),
        ("not_applicable", "status 'confirmed' does not require chain reconciliation", None, "confirmed"),
    ],
)
async def test_payment_resolve_reports_reconcile_decisions(
    monkeypatch,
    decision,
    reason,
    updated_status,
    initial_status,
):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    db_handler = FakePaymentDB()
    db_handler.payments["pay-1"] = {
        "payment_id": "pay-1",
        "guild_id": 1,
        "status": initial_status,
        "tx_signature": "ABCDEFGH12345678",
    }
    reconcile_result = SimpleNamespace(
        decision=decision,
        reason=reason,
        tx_signature="ABCDEFGH12345678",
        updated_status=updated_status,
    )
    payment_service = FakePaymentService(
        reconcile_results=[reconcile_result],
        db_handler=db_handler,
    )
    bot = FakePaymentBot(payment_service)
    cog = payment_ui_cog_module.PaymentUICog(bot, db_handler, payment_service=payment_service)
    interaction = FakeInteraction(user_id=999)

    await cog.payment_resolve.callback(cog, interaction, "pay-1")

    assert payment_service.reconcile_calls == [("pay-1", 1)]
    assert len(interaction.response.messages) == 1
    content, ephemeral = interaction.response.messages[0]
    assert ephemeral is True
    assert f"decision: {decision}" in content
    assert f"reason: {reason}" in content
    assert f"status: {updated_status or initial_status}" in content
    assert "tx_signature: ABCD...5678" in content


async def test_payment_resolve_intent_target_clears_stuck_admin_intent(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    intent_id = "481ee130-cf28-4cc2-bfc9-f8e5fceedb84"
    db_handler = FakePaymentDB()
    db_handler.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "status": "confirmed",
        "tx_signature": "TESTSIG12345678",
    }
    db_handler.payments["payment-final"] = {
        "payment_id": "payment-final",
        "guild_id": 1,
        "status": "pending_confirmation",
        "tx_signature": None,
    }
    db_handler.intents[intent_id] = {
        "intent_id": intent_id,
        "guild_id": 1,
        "status": "awaiting_admin_approval",
        "test_payment_id": "payment-test",
        "final_payment_id": "payment-final",
    }
    payment_service = FakePaymentService(db_handler=db_handler)
    bot = FakePaymentBot(payment_service)
    cog = payment_ui_cog_module.PaymentUICog(bot, db_handler, payment_service=payment_service)
    interaction = FakeInteraction(user_id=999)

    await cog.payment_resolve.callback(cog, interaction, "intent:481ee130")

    assert payment_service.reconcile_calls == []
    assert db_handler.intents[intent_id]["status"] == "cancelled"
    assert db_handler.payments["payment-test"]["status"] == "confirmed"
    assert db_handler.payments["payment-final"]["status"] == "cancelled"
    assert db_handler.cancel_payment_calls == [
        ("payment-final", 1, f"Admin cancelled payment intent {intent_id}"),
    ]
    content, ephemeral = interaction.response.messages[0]
    assert ephemeral is True
    assert "decision: intent_cancelled" in content
    assert "status: cancelled" in content
    assert "payment-test:confirmed" in content
    assert "payment-final:cancelled" in content


async def test_payment_resolve_intent_target_blocks_submitted_linked_payment(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    intent_id = "481ee130-cf28-4cc2-bfc9-f8e5fceedb84"
    db_handler = FakePaymentDB()
    db_handler.payments["payment-final"] = {
        "payment_id": "payment-final",
        "guild_id": 1,
        "status": "submitted",
        "tx_signature": "FINALSIG12345678",
    }
    db_handler.intents[intent_id] = {
        "intent_id": intent_id,
        "guild_id": 1,
        "status": "awaiting_admin_approval",
        "final_payment_id": "payment-final",
    }
    payment_service = FakePaymentService(db_handler=db_handler)
    bot = FakePaymentBot(payment_service)
    cog = payment_ui_cog_module.PaymentUICog(bot, db_handler, payment_service=payment_service)
    interaction = FakeInteraction(user_id=999)

    await cog.payment_resolve.callback(cog, interaction, "intent:481ee130")

    assert db_handler.intents[intent_id]["status"] == "awaiting_admin_approval"
    assert db_handler.payments["payment-final"]["status"] == "submitted"
    assert db_handler.cancel_payment_calls == []
    content, ephemeral = interaction.response.messages[0]
    assert ephemeral is True
    assert "decision: keep_in_hold" in content
    assert "linked payment payment-final is submitted" in content
    assert "tx_signature: FINA...5678" in content


async def test_grants_wallet_submission_and_test_confirmation_queue_final_payment():
    payment_service = FakeGrantPaymentService()
    payment_ui_cog = FakeGrantPaymentUICog()
    db_handler = FakeGrantsDB()
    thread = FakeGrantThread()

    bot = FakePaymentBot(payment_service, channel=thread, payment_ui_cog=payment_ui_cog)
    bot.db_handler = db_handler
    bot.payment_service = payment_service

    cog = GrantsCog(bot)
    cog._tags["in progress"] = object()
    async def noop_apply_tag(_thread, _tag_name):
        return None
    cog._apply_tag = noop_apply_tag
    async def fake_fetch_grant_thread(_thread_id):
        return thread
    cog._fetch_grant_thread = fake_fetch_grant_thread

    await cog._start_payment_flow(thread, dict(db_handler.grant), "Wallet111111111111111111111111111111111")

    assert db_handler.wallet_calls[0][:4] == (1, 222, "solana", "Wallet111111111111111111111111111111111")
    assert payment_service.request_calls[0]["is_test"] is True
    assert payment_service.confirm_calls == [
        ("pay-test", {"guild_id": 1, "actor": PaymentActor(PaymentActorKind.AUTO, 222)})
    ]
    assert db_handler.status_updates[-1][1] == "payment_requested"

    test_payment = {
        "payment_id": "pay-test",
        "guild_id": 1,
        "producer": "grants",
        "producer_ref": "1001",
        "recipient_wallet": "Wallet111111111111111111111111111111111",
        "wallet_id": "wallet-1",
        "chain": "solana",
        "provider": "solana",
        "is_test": True,
        "status": "confirmed",
        "confirm_channel_id": 10,
        "confirm_thread_id": 1001,
        "notify_channel_id": 10,
        "notify_thread_id": 1001,
        "route_key": None,
    }
    bot.get_channel = lambda channel_id: thread if channel_id == 1001 else None
    async def fetch_thread(_channel_id):
        return thread
    bot.fetch_channel = fetch_thread

    await cog.handle_payment_result(test_payment)

    assert len(payment_service.request_calls) == 2
    final_request = payment_service.request_calls[1]
    assert final_request["is_test"] is False
    assert final_request["amount_usd"] == 42.5
    assert payment_ui_cog.sent == ["pay-final"]
    assert "Test payment confirmed." in thread.messages[-1]


async def test_grants_final_payment_confirmation_marks_grant_paid():
    payment_service = FakeGrantPaymentService()
    db_handler = FakeGrantsDB()
    thread = FakeGrantThread()
    bot = FakePaymentBot(payment_service, channel=thread)
    bot.db_handler = db_handler
    bot.payment_service = payment_service
    bot.get_cog = lambda name: None
    bot.get_channel = lambda channel_id: thread if channel_id == 1001 else None
    async def fetch_thread(_channel_id):
        return thread
    bot.fetch_channel = fetch_thread

    cog = GrantsCog(bot)
    async def fake_fetch_grant_thread(_thread_id):
        return thread
    cog._fetch_grant_thread = fake_fetch_grant_thread

    await cog.handle_payment_result(
        {
            "payment_id": "pay-final",
            "guild_id": 1,
            "producer": "grants",
            "producer_ref": "1001",
            "recipient_wallet": "Wallet111111111111111111111111111111111",
            "chain": "solana",
            "provider": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.5,
            "token_price_usd": 150.0,
            "tx_signature": "sig-123",
        }
    )

    assert db_handler.recorded_payments == [(1001, "sig-123", 1.5, 150.0, 1)]
    assert thread.edits == [{"archived": True}]
    assert "Payment sent!" in thread.messages[-1]
