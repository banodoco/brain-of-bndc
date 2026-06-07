from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from src.common.db_handler import DatabaseHandler, WalletUpdateBlockedError
from src.features.admin_chat import agent as admin_agent
from src.features.admin_chat.admin_chat_cog import AdminChatCog
from src.features.admin_chat import tools as admin_tools
from src.features.admin_chat.tools import execute_initiate_batch_payment, execute_initiate_payment
from src.features.grants.grants_cog import GrantsCog
from src.features.payments.payment_service import PaymentActor, PaymentActorKind, PaymentService
from src.features.payments.payment_ui_cog import AdminApprovalView, PaymentConfirmView, PaymentUICog
from src.features.payments.payment_worker_cog import PaymentWorkerCog


VALID_SOL_ADDRESS = "11111111111111111111111111111111"


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "reason"
        self.headers = {}


class FakeClassifierResponse:
    def __init__(self, text):
        self.content = [SimpleNamespace(type="text", text=text)]


class FakeClassifierMessages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeClassifierResponse(self.text)


class FakeClassifierClient:
    def __init__(self, text):
        self.messages = FakeClassifierMessages(text)


class FakeAuthor:
    def __init__(self, user_id, *, bot=False):
        self.id = user_id
        self.bot = bot
        self.display_name = f"user-{user_id}"

    async def send(self, content, **kwargs):
        if not hasattr(self, "sent_messages"):
            self.sent_messages = []
        message = SimpleNamespace(content=content, kwargs=kwargs)
        self.sent_messages.append(message)
        return message


class FakeMessage:
    def __init__(self, message_id, author, channel, guild, content):
        self.id = message_id
        self.author = author
        self.channel = channel
        self.guild = guild
        self.content = content


class FakeChannelMessage:
    def __init__(self, channel, message_id, content, kwargs=None, *, reference=None):
        self.id = message_id
        self.channel = channel
        self.guild = channel.guild
        self.content = content
        self.kwargs = kwargs or {}
        self.reference = reference
        self.edits = []

    async def edit(self, *, content=None, suppress=None):
        self.edits.append({"content": content, "suppress": suppress})
        if content is not None:
            self.content = content
        return self

    async def reply(self, content, **kwargs):
        return self.channel._record_sent_message(content, kwargs, reference=self.id)


class FakeChannel:
    def __init__(self, channel_id=55, guild=None, messages=None, *, parent_id=None):
        self.id = channel_id
        self.guild = guild or SimpleNamespace(id=1)
        self.parent_id = parent_id
        self._messages = list(messages or [])
        self._sent_messages_by_id = {}
        self._missing_message_ids = set()
        self._next_message_id = 900
        self.sent_messages = []

    def _record_sent_message(self, content, kwargs=None, *, reference=None):
        message = FakeChannelMessage(
            self,
            self._next_message_id,
            content,
            kwargs or {},
            reference=reference,
        )
        self._next_message_id += 1
        self.sent_messages.append(message)
        self._sent_messages_by_id[message.id] = message
        return message

    async def send(self, content, **kwargs):
        return self._record_sent_message(content, kwargs)

    async def fetch_message(self, message_id):
        if message_id in self._missing_message_ids or message_id not in self._sent_messages_by_id:
            raise discord.NotFound(FakeResponse(404), "missing")
        return self._sent_messages_by_id[message_id]

    def history(self, limit=100, after=None, oldest_first=False):
        after_id = getattr(after, "id", None)
        messages = [msg for msg in self._messages if after_id is None or msg.id > after_id]
        messages.sort(key=lambda msg: msg.id, reverse=not oldest_first)
        messages = messages[:limit]

        async def iterator():
            for message in messages:
                yield message

        return iterator()


class FakePaymentUICog:
    def __init__(self):
        self.confirmation_requests = []
        self.admin_approval_requests = []

    async def send_confirmation_request(self, payment_id):
        self.confirmation_requests.append(payment_id)
        return SimpleNamespace(id=f"confirm-{payment_id}")

    async def _send_admin_approval_dm(self, payment):
        self.admin_approval_requests.append(dict(payment))
        return SimpleNamespace(id=f"admin-{payment['payment_id']}")


class FakeFlowCog:
    def __init__(self):
        self.started = []
        self.fresh = []
        self.existing = []

    async def _start_admin_payment_flow(self, channel, intent):
        self.started.append((channel, dict(intent)))

    async def _gate_fresh_intent_atomic(
        self,
        channel,
        guild_id,
        recipient_user_id,
        amount_sol,
        source_channel_id,
        wallet_record,
        admin_user_id,
        reason,
        producer_ref,
    ):
        self.fresh.append(
            {
                "channel": channel,
                "guild_id": guild_id,
                "recipient_user_id": recipient_user_id,
                "amount_sol": amount_sol,
                "source_channel_id": source_channel_id,
                "wallet_record": dict(wallet_record),
                "admin_user_id": admin_user_id,
                "reason": reason,
                "producer_ref": producer_ref,
            }
        )
        return {
            "intent": {
                "intent_id": f"fresh-intent-{recipient_user_id}",
                "guild_id": guild_id,
                "channel_id": source_channel_id,
                "recipient_user_id": recipient_user_id,
                "wallet_id": wallet_record.get("wallet_id"),
                "requested_amount_sol": amount_sol,
                "producer_ref": producer_ref,
                "status": "awaiting_admin_approval",
            },
            "payment": {"payment_id": f"payment-{recipient_user_id}", "status": "pending_confirmation"},
        }

    async def _gate_existing_intent(self, channel, intent, wallet_record, amount_sol):
        self.existing.append(
            {
                "channel": channel,
                "intent": dict(intent),
                "wallet_record": dict(wallet_record),
                "amount_sol": amount_sol,
            }
        )
        return {
            "intent": dict(intent),
            "payment": {"payment_id": f"existing-{intent['intent_id']}", "status": "pending_confirmation"},
        }


class FakeBot:
    def __init__(
        self,
        channel,
        *,
        payment_service=None,
        payment_ui_cog=None,
        payment_worker_cog=None,
        admin_chat_cog=None,
        guilds=None,
        db_handler=None,
    ):
        self.payment_service = payment_service
        if isinstance(channel, dict):
            self._channels = dict(channel)
            self._channel = next(iter(self._channels.values()))
        else:
            self._channel = channel
            self._channels = {channel.id: channel}
        self._payment_ui_cog = payment_ui_cog
        self._payment_worker_cog = payment_worker_cog
        self._admin_chat_cog = admin_chat_cog
        default_guilds = []
        for value in self._channels.values():
            if all(existing.id != value.guild.id for existing in default_guilds):
                default_guilds.append(value.guild)
        self.guilds = guilds or default_guilds
        self.user = SimpleNamespace(id=999)
        self.ready_waits = 0
        self.fetched_users = {}
        self.added_views = []
        self._is_ready = False
        self.db_handler = db_handler
        self.claude_client = object()

    async def wait_until_ready(self):
        self.ready_waits += 1

    def is_ready(self):
        return self._is_ready

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id in self._channels:
            return self._channels[channel_id]
        raise RuntimeError("unknown channel")

    async def fetch_user(self, user_id):
        user = self.fetched_users.get(user_id)
        if user is None:
            user = FakeAuthor(user_id)
            user.sent_messages = []
            self.fetched_users[user_id] = user
        return user

    def get_cog(self, name):
        if name == "PaymentUICog":
            return self._payment_ui_cog
        if name == "PaymentWorkerCog":
            return self._payment_worker_cog
        if name == "AdminChatCog":
            return self._admin_chat_cog
        return None

    def get_guild(self, guild_id):
        for guild in self.guilds:
            if guild.id == guild_id:
                return guild
        return None

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))


class FakeInteractionResponse:
    def __init__(self):
        self.calls = []

    async def defer(self, *, ephemeral=False):
        self.calls.append({"ephemeral": ephemeral})


class FakeInteractionFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append({"content": content, "ephemeral": ephemeral})


class FakeInteraction:
    def __init__(self, user_id, *, guild_id=1, message=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.message = message
        self.response = FakeInteractionResponse()
        self.followup = FakeInteractionFollowup()


class FakeAdminPaymentService:
    def __init__(self, *, request_result=None, confirm_result=None):
        self.request_calls = []
        self.confirm_calls = []
        self.request_result = request_result
        self.confirm_result = confirm_result or {"payment_id": "pay-1", "status": "queued"}

    async def request_payment(self, **kwargs):
        self.request_calls.append(dict(kwargs))
        if self.request_result is None:
            return {
                "payment_id": "payment-final",
                "status": "pending_confirmation",
                **kwargs,
            }
        return self.request_result

    def confirm_payment(self, payment_id, *, actor, guild_id=None):
        self.confirm_calls.append((payment_id, {"guild_id": guild_id, "actor": actor}))
        return self.confirm_result


class FakeIntentSupabaseQuery:
    def __init__(self, db):
        self.db = db
        self.payload = {}
        self.filters = {}

    def update(self, payload):
        self.payload = dict(payload)
        self.db.supabase_calls.append(dict(payload))
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def execute(self):
        if self.db.supabase_failures:
            raise Exception(self.db.supabase_failures.pop(0))
        if self.db.reject_status_message_id and "status_message_id" in self.payload:
            self.db.reject_status_message_id = False
            raise Exception("column status_message_id does not exist")

        intent_id = self.filters.get("intent_id")
        guild_id = self.filters.get("guild_id")
        intent = self.db.intents.get(intent_id)
        if not intent or intent.get("guild_id") != guild_id:
            return SimpleNamespace(data=[])

        intent.update(dict(self.payload))
        self.db.updated_intents.append((intent_id, dict(self.payload), guild_id))
        self.db._sync_active_index(intent)
        return SimpleNamespace(data=[dict(intent)])


class FakeIntentSupabase:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        assert name == "admin_payment_intents"
        return FakeIntentSupabaseQuery(self.db)


class FakeIntentDB:
    TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

    def __init__(self):
        self.wallets = {}
        self.intents = {}
        self.active_by_key = {}
        self.payments = {}
        self.storage_handler = None
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}
        self.created_intents = []
        self.batch_create_calls = []
        self.updated_intents = []
        self.upsert_wallet_calls = []
        self.cancel_payment_calls = []
        self.wallet_verified_calls = []
        self.reject_status_message_id = False
        self.supabase_failures = []
        self.supabase_calls = []
        self.supabase = FakeIntentSupabase(self)
        self.server_config = SimpleNamespace(
            get_enabled_servers=lambda require_write=False: [{"guild_id": 1, "enabled": True, "write_enabled": True}],
            resolve_payment_destinations=lambda guild_id, channel_id, producer: None,
            is_guild_enabled=lambda guild_id: True,
            get_first_server_with_field=lambda field, require_write=False: {"guild_id": 1, "grants_channel_id": 55},
        )

    def _gate_check(self, guild_id):
        return True

    def _serialize_supabase_value(self, payload):
        return dict(payload)

    def _sync_active_index(self, intent):
        key = (int(intent["guild_id"]), int(intent["channel_id"]), int(intent["recipient_user_id"]))
        if intent.get("status") in self.TERMINAL_STATUSES:
            self.active_by_key.pop(key, None)
        else:
            self.active_by_key[key] = intent

    def get_wallet(self, guild_id, discord_user_id, chain):
        wallet = self.wallets.get((guild_id, discord_user_id, chain))
        return dict(wallet) if wallet else None

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        for wallet in self.wallets.values():
            if wallet["wallet_id"] == wallet_id and (guild_id is None or wallet["guild_id"] == guild_id):
                return dict(wallet)
        return None

    def get_payment_requests_by_producer(self, guild_id, producer, producer_ref, is_test=None):
        rows = [
            dict(row)
            for row in self.payments.values()
            if row.get("guild_id") == guild_id
            and row.get("producer") == producer
            and row.get("producer_ref") == producer_ref
            and (is_test is None or row.get("is_test") == is_test)
        ]
        return rows

    def create_payment_request(self, record, guild_id=None):
        payment_id = f"payment-{len(self.payments) + 1}"
        row = {"payment_id": payment_id, **dict(record), "guild_id": guild_id or record["guild_id"]}
        self.payments[payment_id] = row
        return dict(row)

    def get_payment_request(self, payment_id, guild_id=None):
        payment = self.payments.get(payment_id)
        if payment and guild_id is not None and payment.get("guild_id") != guild_id:
            return None
        return dict(payment) if payment else None

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        return float(self.rolling_24h_usd.get((int(guild_id), str(provider).strip().lower()), 0.0))

    def create_admin_payment_intent(self, record, guild_id):
        intent_id = record.get("intent_id") or f"intent-{len(self.intents) + 1}"
        intent = {"intent_id": intent_id, **dict(record), "guild_id": guild_id}
        self.intents[intent_id] = intent
        self.created_intents.append(dict(intent))
        self._sync_active_index(intent)
        return dict(intent)

    def create_admin_payment_intents_batch(self, records, guild_id):
        self.batch_create_calls.append((guild_id, [dict(record) for record in records]))
        return [self.create_admin_payment_intent(record, guild_id) for record in records]

    def get_active_intent_for_recipient(self, guild_id, channel_id, recipient_user_id):
        intent = self.active_by_key.get((guild_id, channel_id, recipient_user_id))
        return dict(intent) if intent else None

    def update_admin_payment_intent(self, intent_id, payload, guild_id):
        intent = self.intents.get(intent_id)
        if not intent or intent["guild_id"] != guild_id:
            return None
        intent.update(dict(payload))
        self.updated_intents.append((intent_id, dict(payload), guild_id))
        self._sync_active_index(intent)
        return dict(intent)

    def get_admin_payment_intent(self, intent_id, guild_id):
        intent = self.intents.get(intent_id)
        if not intent or intent["guild_id"] != guild_id:
            return None
        return dict(intent)

    def find_admin_chat_intent_by_payment_id(self, payment_id):
        for intent in self.intents.values():
            if intent.get("final_payment_id") == payment_id or intent.get("test_payment_id") == payment_id:
                return dict(intent)
        return None

    def list_active_intents(self, guild_id):
        return [
            dict(intent)
            for intent in self.intents.values()
            if intent["guild_id"] == guild_id and intent.get("status") not in self.TERMINAL_STATUSES
        ]

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        key = (guild_id, discord_user_id, chain)
        existing = self.wallets.get(key)
        if (
            existing
            and existing["wallet_address"] != address
            and self.has_active_payment_or_intent(guild_id, discord_user_id)
        ):
            raise WalletUpdateBlockedError("active payment in flight")
        # Match real db_handler.upsert_wallet semantics:
        #  - new wallet → verified_at = None
        #  - existing wallet, same address → preserve existing verified_at
        #  - existing wallet, different address → verified_at cleared to None
        if existing is None:
            verified_at = None
        elif existing.get("wallet_address") == address:
            verified_at = existing.get("verified_at")
        else:
            verified_at = None
        wallet = {
            "wallet_id": existing["wallet_id"] if existing else f"wallet-{len(self.upsert_wallet_calls) + 1}",
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": verified_at,
            "metadata": metadata,
        }
        self.upsert_wallet_calls.append((guild_id, discord_user_id, chain, address, metadata))
        self.wallets[key] = wallet
        return dict(wallet)

    def get_awaiting_wallet_intent_for_user(self, guild_id, recipient_user_id):
        for intent in self.intents.values():
            if (
                intent.get("guild_id") == guild_id
                and intent.get("recipient_user_id") == recipient_user_id
                and intent.get("status") == "awaiting_wallet"
            ):
                return dict(intent)
        return None

    def get_payment_request(self, payment_id, guild_id=None):
        payment = self.payments.get(payment_id)
        if payment and guild_id is not None and payment.get("guild_id") != guild_id:
            return None
        return dict(payment) if payment else None

    def cancel_payment(self, payment_id, guild_id=None, reason=None):
        self.cancel_payment_calls.append((payment_id, guild_id, reason))
        payment = self.payments.get(payment_id)
        if payment:
            payment["status"] = "cancelled"
            payment["last_error"] = reason
        return True

    def mark_wallet_verified(self, wallet_id, guild_id=None):
        self.wallet_verified_calls.append((wallet_id, guild_id))
        for wallet in self.wallets.values():
            if wallet["wallet_id"] == wallet_id and (guild_id is None or wallet["guild_id"] == guild_id):
                wallet["verified_at"] = "verified-now"
                return True
        return False

    def increment_intent_ambiguous_reply_count(self, intent_id, guild_id):
        intent = self.intents.get(intent_id)
        if not intent or intent["guild_id"] != guild_id:
            return None
        intent["ambiguous_reply_count"] = int(intent.get("ambiguous_reply_count") or 0) + 1
        self._sync_active_index(intent)
        return dict(intent)


class FakeProvider:
    def __init__(self):
        self.price_calls = 0

    async def get_token_price_usd(self):
        self.price_calls += 1
        return 200.0

    def token_name(self):
        return "SOL"


class FakePaymentRequestDB:
    def __init__(self):
        self.created = []
        self.wallets = {"wallet-1": {"wallet_id": "wallet-1", "wallet_address": VALID_SOL_ADDRESS, "guild_id": 1}}
        self.wallet_registry = {}
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}

    def get_payment_requests_by_producer(self, guild_id, producer, producer_ref, is_test):
        return [
            dict(row)
            for row in self.created
            if row["guild_id"] == guild_id
            and row["producer"] == producer
            and row["producer_ref"] == producer_ref
            and row["is_test"] == is_test
        ]

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        wallet = self.wallets.get(wallet_id)
        if wallet and guild_id is not None and wallet["guild_id"] != guild_id:
            return None
        return dict(wallet) if wallet else None

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        return float(self.rolling_24h_usd.get((int(guild_id), str(provider).strip().lower()), 0.0))

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        key = (guild_id, discord_user_id, chain)
        existing = self.wallet_registry.get(key)
        if (
            existing
            and existing["wallet_address"] != address
            and self.has_active_payment_or_intent(guild_id, discord_user_id)
        ):
            raise WalletUpdateBlockedError("active payment in flight")
        wallet = {
            "wallet_id": existing["wallet_id"] if existing else f"wallet-user-{len(self.wallet_registry) + 1}",
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "now",
            "metadata": metadata,
        }
        self.wallet_registry[key] = wallet
        self.wallets[wallet["wallet_id"]] = wallet
        return dict(wallet)

    def create_payment_request(self, record, guild_id=None):
        row = {"payment_id": f"payment-{len(self.created) + 1}", **dict(record), "guild_id": guild_id or record["guild_id"]}
        self.created.append(row)
        return dict(row)


class FakeGrantDB:
    def __init__(self):
        self.wallets = {}
        self.upsert_wallet_calls = []
        self.status_updates = []
        self.storage_handler = None
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}
        self.server_config = SimpleNamespace(
            get_first_server_with_field=lambda field, require_write=False: {"guild_id": 1, "grants_channel_id": 55},
            resolve_payment_destinations=lambda guild_id, channel_id, producer: None,
        )

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        return float(self.rolling_24h_usd.get((int(guild_id), str(provider).strip().lower()), 0.0))

    def upsert_wallet(self, guild_id, discord_user_id, chain, address, metadata=None):
        key = (guild_id, discord_user_id, chain)
        existing = self.wallets.get(key)
        if (
            existing
            and existing["wallet_address"] != address
            and self.has_active_payment_or_intent(guild_id, discord_user_id)
        ):
            raise WalletUpdateBlockedError("active payment in flight")
        wallet = {
            "wallet_id": existing["wallet_id"] if existing else f"wallet-{len(self.upsert_wallet_calls) + 1}",
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "now",
            "metadata": metadata,
        }
        self.upsert_wallet_calls.append((guild_id, discord_user_id, chain, address, metadata))
        self.wallets[key] = wallet
        return dict(wallet)

    def update_grant_status(self, thread_id, status, guild_id=None, **kwargs):
        self.status_updates.append((thread_id, status, guild_id, kwargs))
        return True


@pytest.fixture(autouse=True)
def admin_chat_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.delenv("ADMIN_CHAT_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_parse_wallet_from_text_extracts_wrapped_address():
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())

    assert cog._parse_wallet_from_text(f"wallet is `<{VALID_SOL_ADDRESS}>`") == VALID_SOL_ADDRESS
    assert cog._parse_wallet_from_text(f"not-a-wallet {VALID_SOL_ADDRESS}.") == VALID_SOL_ADDRESS
    assert cog._parse_wallet_from_text("definitely not a wallet") is None


def test_admin_chat_allows_configured_extra_users(monkeypatch):
    monkeypatch.setenv("ADMIN_CHAT_ALLOWED_USER_IDS", "614602980943069214, 123")
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())

    assert cog._is_admin(999)
    assert cog._is_admin(614602980943069214)
    assert cog._is_admin(123)
    assert not cog._is_admin(456)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("confirmed, got it", "positive"),
        ("no", "negative"),
        ("confirmed but not received", "ambiguous"),
        ("👍 thanks", "positive"),
        ("maybe later", "ambiguous"),
    ],
)
def test_classify_confirmation_keywords(content, expected):
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())

    assert cog._classify_confirmation_keyword(content) == expected


def test_classify_confirmation_negative_phrase_beats_embedded_positive_keyword():
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())

    assert cog._classify_confirmation_keyword("not received") == "negative"
    assert cog._classify_confirmation_keyword("not received yet") == "negative"


@pytest.mark.anyio
async def test_classify_confirmation_llm_confirmed_maps_to_positive(monkeypatch):
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="classify_payment_reply",
                    input={"classification": "confirmed"},
                )
            ]
        )
    )
    cog._classifier_client = SimpleNamespace(generate_chat_completion=create_mock)

    result = await cog._classify_confirmation("yes, I see it in my wallet")

    assert result == ("positive", None)
    assert "tool_choice" not in create_mock.await_args.kwargs


@pytest.mark.anyio
async def test_classify_confirmation_llm_not_received_maps_to_negative(monkeypatch):
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="classify_payment_reply",
                    input={"classification": "not_received"},
                )
            ]
        )
    )
    cog._classifier_client = SimpleNamespace(generate_chat_completion=create_mock)

    result = await cog._classify_confirmation("no, I still do not see it")

    assert result == ("negative", None)


@pytest.mark.anyio
async def test_classify_confirmation_llm_unclear_maps_to_ambiguous_with_reply(monkeypatch):
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="classify_payment_reply",
                    input={
                        "classification": "unclear",
                        "reply": "Please reply yes once you see the test SOL, or not received if you do not.",
                    },
                )
            ]
        )
    )
    cog._classifier_client = SimpleNamespace(generate_chat_completion=create_mock)

    result = await cog._classify_confirmation("wait let me check")

    assert result == (
        "ambiguous",
        "Please reply yes once you see the test SOL, or not received if you do not.",
    )


@pytest.mark.anyio
async def test_classify_confirmation_falls_back_to_keywords_when_api_key_missing():
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())

    assert await cog._classify_confirmation("confirmed, got it") == ("positive", None)


@pytest.mark.anyio
async def test_classify_confirmation_falls_back_to_keywords_on_api_error(monkeypatch):
    cog = AdminChatCog(FakeBot(FakeChannel(), payment_service=object()), FakeIntentDB(), sharer=object())
    create_mock = AsyncMock(side_effect=RuntimeError("boom"))
    cog._classifier_client = SimpleNamespace(generate_chat_completion=create_mock)

    result = await cog._classify_confirmation("confirmed, got it")

    assert result == ("positive", None)
    create_mock.assert_awaited_once()


def test_dead_code_is_removed():
    member_system_prompt = "MEMBER_" + "SYSTEM_PROMPT"
    member_max_conversation_length = "MEMBER_MAX_" + "CONVERSATION_LENGTH"
    member_tools = "MEMBER_" + "TOOLS"
    admin_only_tools = "ADMIN_ONLY_" + "TOOLS"
    role_tool_lookup_name = "get_" + "tools_for_role"
    can_user_message_bot = "_can_user_" + "message_bot"
    is_directed_at_bot = "_is_" + "directed_at_bot"

    assert not hasattr(admin_agent, member_system_prompt)
    assert not hasattr(admin_agent, member_max_conversation_length)
    assert not hasattr(admin_tools, member_tools)
    assert not hasattr(admin_tools, admin_only_tools)
    assert not hasattr(admin_tools, role_tool_lookup_name)
    assert not hasattr(AdminChatCog, can_user_message_bot)
    assert not hasattr(AdminChatCog, is_directed_at_bot)


@pytest.mark.anyio
async def test_identity_router_routes_admin_to_agent_without_direction_gate():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    cog = AdminChatCog(bot, db, sharer=object())
    cog._handle_admin_message = AsyncMock()
    cog._handle_pending_recipient_message = AsyncMock()

    await cog.on_message(FakeMessage(2001, FakeAuthor(999), channel, guild, "plain text"))

    cog._handle_admin_message.assert_awaited_once()
    cog._handle_pending_recipient_message.assert_not_awaited()


@pytest.mark.anyio
async def test_identity_router_routes_pending_recipient_without_agent_calls():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    cog = AdminChatCog(bot, db, sharer=object())
    cog.agent = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("agent chat should not be used")))
    cog._ensure_agent = lambda: (_ for _ in ()).throw(RuntimeError("agent init should not be used"))
    cog._handle_wallet_received = AsyncMock()

    await cog.on_message(FakeMessage(2002, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS))

    cog._handle_wallet_received.assert_awaited_once()
    cog.agent.chat.assert_not_awaited()


@pytest.mark.anyio
async def test_identity_router_falls_through_for_non_admin_without_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    cog = AdminChatCog(bot, db, sharer=object())
    cog._handle_admin_message = AsyncMock()
    cog._handle_pending_recipient_message = AsyncMock()

    await cog.on_message(FakeMessage(2003, FakeAuthor(77), channel, guild, "hello bot"))

    cog._handle_admin_message.assert_not_awaited()
    cog._handle_pending_recipient_message.assert_not_awaited()


@pytest.mark.anyio
async def test_state_machine_silently_ignores_awaiting_admin_init():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_admin_init",
        },
        guild_id=1,
    )
    cog = AdminChatCog(bot, db, sharer=object())
    cog._handle_wallet_received = AsyncMock()

    await cog.on_message(FakeMessage(2004, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS))

    cog._handle_wallet_received.assert_not_awaited()
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_admin_init"


@pytest.mark.anyio
async def test_24h_timeout_sweep_escalates_stuck_test_receipt():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
        },
        guild_id=1,
    )
    db.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "recipient_wallet": VALID_SOL_ADDRESS,
        "tx_signature": "sig-stale",
    }
    db.list_stale_test_receipt_intents = lambda cutoff_iso: [db.get_admin_payment_intent(intent["intent_id"], 1)]
    bot = FakeBot(channel, payment_service=object())
    bot._is_ready = True
    cog = AdminChatCog(bot, db, sharer=object())

    await cog._sweep_stale_test_receipts.coro(cog)

    assert db.intents[intent["intent_id"]]["status"] == "manual_review"
    admin_user = bot.fetched_users[999]
    assert "Stale test receipt confirmation requires manual review" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_manual_review_blocks_new_intents():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.0,
            "producer_ref": "1_42_123",
            "status": "manual_review",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1.0},
    )

    assert result["success"] is True
    assert result["duplicate"] is True


@pytest.mark.anyio
async def test_initiate_payment_wallet_on_file():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-verified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    flow_cog = FakeFlowCog()
    bot = FakeBot(channel, payment_service=object(), admin_chat_cog=flow_cog)

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1.5, "reason": "thanks", "admin_user_id": 999},
    )

    assert result["success"] is True
    assert result["wallet_on_file"] is True
    assert db.created_intents == []
    assert len(flow_cog.started) == 0
    assert len(flow_cog.fresh) == 1
    assert flow_cog.fresh[0]["wallet_record"]["wallet_id"] == "wallet-verified"
    assert flow_cog.fresh[0]["admin_user_id"] == 999
    assert flow_cog.fresh[0]["producer_ref"].startswith("1_42_")


@pytest.mark.anyio
async def test_initiate_payment_no_wallet():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 2.0, "admin_user_id": 999},
    )

    assert result["success"] is True
    assert result["wallet_on_file"] is False
    assert db.created_intents[0]["status"] == "awaiting_wallet"
    assert db.created_intents[0]["admin_user_id"] == 999
    assert db.updated_intents[-1][1]["prompt_message_id"] == 900
    assert "<@42>" in channel.sent_messages[0].content


@pytest.mark.anyio
async def test_initiate_payment_validation():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    cases = [
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "nope", "amount_sol": 1},
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 0},
        {"source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1},
        {"guild_id": 1, "recipient_user_id": "42", "amount_sol": 1},
    ]

    for params in cases:
        result = await execute_initiate_payment(bot, db, params)
        assert result["success"] is False


@pytest.mark.anyio
async def test_initiate_payment_tool_rejects_invalid_wallet_address_arg():
    """A malformed inline wallet_address must be rejected with a clear error.

    The identity-routing era rule was 'LLM never sources wallets'. The follow-up
    relaxes that to 'admin may provide wallet inline, but it must be a valid
    Solana address and the admin DM approval is still the money-movement gate'.
    So an obviously-junk string like 'Injected...' must fail validation, not be
    silently stripped."""
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "recipient_user_id": "42",
            "amount_sol": 1.0,
            "wallet_address": "Injected111111111111111111111111111111",
        },
    )

    assert result["success"] is False
    assert "Solana" in result["error"]
    # No intent should be created when the inline wallet is rejected
    assert db.created_intents == []
    # No wallet should be upserted either
    assert db.wallets == {}


@pytest.mark.anyio
async def test_initiate_payment_tool_accepts_valid_wallet_address_inline():
    """A valid inline wallet_address is upserted and the flow proceeds through
    branch 2 (unverified wallet on file) — test payment fires immediately,
    recipient is not re-asked to post their wallet."""
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    flow_cog = FakeFlowCog()  # records _start_admin_payment_flow calls
    bot = FakeBot(channel, payment_service=object(), admin_chat_cog=flow_cog)

    result = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "recipient_user_id": "42",
            "amount_sol": 1.5,
            "wallet_address": VALID_SOL_ADDRESS,
        },
    )

    assert result["success"] is True, result
    assert result["wallet_on_file"] is True
    assert result["verified"] is False
    # The wallet was upserted (unverified) before the flow branch ran
    assert (1, 42, "solana") in db.wallets
    assert db.wallets[(1, 42, "solana")]["wallet_address"] == VALID_SOL_ADDRESS
    assert db.wallets[(1, 42, "solana")].get("verified_at") in (None, "")
    # The intent was created directly in awaiting_test (skipping awaiting_wallet)
    assert len(db.created_intents) == 1
    assert db.created_intents[0]["status"] == "awaiting_test"
    assert db.created_intents[0]["wallet_id"] is not None
    # And _start_admin_payment_flow was called to fire the test payment
    assert len(flow_cog.started) == 1
    started_channel, started_intent = flow_cog.started[0]
    assert started_intent["status"] == "awaiting_test"


@pytest.mark.anyio
async def test_initiate_payment_uses_existing_unverified_wallet_on_file():
    """When an unverified wallet is already on file from a prior attempt,
    calling initiate_payment without wallet_address still skips awaiting_wallet
    and goes straight to the test-payment phase using that wallet."""
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    # Pre-seed an UNVERIFIED wallet
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-existing-unverified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": None,
    }
    flow_cog = FakeFlowCog()
    bot = FakeBot(channel, payment_service=object(), admin_chat_cog=flow_cog)

    result = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "recipient_user_id": "42",
            "amount_sol": 1.0,
        },
    )

    assert result["success"] is True, result
    assert result["wallet_on_file"] is True
    assert result["verified"] is False
    # Intent created directly in awaiting_test — no awaiting_wallet detour
    assert len(db.created_intents) == 1
    assert db.created_intents[0]["status"] == "awaiting_test"
    assert db.created_intents[0]["wallet_id"] == "wallet-existing-unverified"
    # And the test-payment flow was kicked off
    assert len(flow_cog.started) == 1
    # Crucially, no wallet-prompt message was posted to the channel
    assert len(channel.sent_messages) == 0


@pytest.mark.anyio
async def test_initiate_payment_duplicate_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    existing = {
        "intent_id": "intent-existing",
        "guild_id": 1,
        "channel_id": 55,
        "recipient_user_id": 42,
        "status": "awaiting_wallet",
    }
    db.intents[existing["intent_id"]] = dict(existing)
    db.active_by_key[(1, 55, 42)] = db.intents[existing["intent_id"]]
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_payment(
        bot,
        db,
        {"guild_id": 1, "source_channel_id": 55, "recipient_user_id": "42", "amount_sol": 1},
    )

    assert result["success"] is True
    assert result["duplicate"] is True
    assert db.created_intents == []


@pytest.mark.anyio
async def test_initiate_batch_payment_atomic_all_or_none():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())

    result = await execute_initiate_batch_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "payments": [
                {"recipient_user_id": "42", "amount_sol": 1.5},
                {"recipient_user_id": "43", "amount_sol": 0},
            ],
        },
    )

    assert result["success"] is False
    assert db.batch_create_calls == []
    assert db.created_intents == []


@pytest.mark.anyio
async def test_initiate_batch_payment_verified_fan_out_uses_gate_existing_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-verified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    db.wallets[(1, 43, "solana")] = {
        "wallet_id": "wallet-unverified",
        "guild_id": 1,
        "discord_user_id": 43,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": None,
    }
    flow_cog = FakeFlowCog()
    bot = FakeBot(channel, payment_service=object(), admin_chat_cog=flow_cog)

    result = await execute_initiate_batch_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "admin_user_id": 999,
            "payments": [
                {"recipient_user_id": "42", "amount_sol": 1.5, "reason": "verified"},
                {"recipient_user_id": "43", "amount_sol": 2.25, "reason": "needs wallet"},
            ],
        },
    )

    assert result["success"] is True
    assert result["count"] == 2
    assert len(db.batch_create_calls) == 1
    batch_guild_id, batch_records = db.batch_create_calls[0]
    assert batch_guild_id == 1
    assert [record["status"] for record in batch_records] == ["awaiting_admin_init", "awaiting_wallet"]
    assert batch_records[0]["wallet_id"] == "wallet-verified"
    assert batch_records[1]["wallet_id"] is None
    assert len(flow_cog.existing) == 1
    assert flow_cog.existing[0]["intent"]["recipient_user_id"] == 42
    assert flow_cog.existing[0]["wallet_record"]["wallet_id"] == "wallet-verified"
    assert flow_cog.fresh == []
    assert db.updated_intents[-1][1]["prompt_message_id"] == 900
    assert "<@43>" in channel.sent_messages[0].content


# VERDICT 2026-04-11: collision reproduced at the downstream admin payment flow
# because same-second `producer_ref` values collapsed distinct intents onto the same
# active payment row. Fixed by switching `producer_ref` to millisecond precision.
@pytest.mark.anyio
async def test_concurrent_admin_payment_producer_ref_collision(monkeypatch):
    guild = SimpleNamespace(id=1)
    channel_one = FakeChannel(channel_id=55, guild=guild)
    channel_two = FakeChannel(channel_id=56, guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-verified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    payment_service = PaymentService(
        db_handler=db,
        providers={"solana_payouts": FakeProvider()},
        test_payment_amount=0.01,
    )
    payment_service.confirm_payment = lambda payment_id, *, actor, guild_id=None: {  # type: ignore[method-assign]
        "payment_id": payment_id,
        "status": "queued",
    }
    bot = FakeBot(
        {55: channel_one, 56: channel_two},
        payment_service=payment_service,
    )
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    bot._admin_chat_cog = cog

    time_values = iter([1_700_000_000.001, 1_700_000_000.002, 1_700_000_000.003, 1_700_000_000.004])
    monkeypatch.setattr("src.features.admin_chat.tools.time.time", lambda: next(time_values))

    result_one = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 55,
            "recipient_user_id": "42",
            "amount_sol": 1.0,
            "admin_user_id": 999,
        },
    )
    result_two = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 56,
            "recipient_user_id": "42",
            "amount_sol": 2.0,
            "admin_user_id": 999,
        },
    )

    assert result_one["success"] is True
    assert result_two["success"] is True
    assert len(db.created_intents) == 2
    assert db.created_intents[0]["producer_ref"] != db.created_intents[1]["producer_ref"]
    assert len(db.payments) == 2
    assert {row["producer_ref"] for row in db.payments.values()} == {
        db.created_intents[0]["producer_ref"],
        db.created_intents[1]["producer_ref"],
    }


@pytest.mark.anyio
async def test_admin_chat_microgrants_thread_uses_grants_provider(monkeypatch):
    guild = SimpleNamespace(id=1)
    grant_thread = FakeChannel(channel_id=1001, guild=guild, parent_id=55)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-verified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    payment_service = PaymentService(
        db_handler=db,
        providers={"solana_grants": FakeProvider(), "solana_payouts": FakeProvider()},
        test_payment_amount=0.01,
    )
    bot = FakeBot(grant_thread, payment_service=payment_service)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    bot._admin_chat_cog = cog
    monkeypatch.setattr("src.features.admin_chat.tools.time.time", lambda: 1_700_000_000.001)

    result = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 1001,
            "recipient_user_id": "42",
            "amount_sol": 2.4,
            "admin_user_id": 999,
        },
    )

    assert result["success"] is True
    payment = next(iter(db.payments.values()))
    assert payment["provider"] == "solana_grants"
    assert payment["notify_thread_id"] == 1001


@pytest.mark.anyio
async def test_admin_chat_non_microgrants_thread_uses_payouts_provider(monkeypatch):
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(channel_id=77, guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-verified",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    payment_service = PaymentService(
        db_handler=db,
        providers={"solana_grants": FakeProvider(), "solana_payouts": FakeProvider()},
        test_payment_amount=0.01,
    )
    bot = FakeBot(channel, payment_service=payment_service)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    bot._admin_chat_cog = cog
    monkeypatch.setattr("src.features.admin_chat.tools.time.time", lambda: 1_700_000_000.001)

    result = await execute_initiate_payment(
        bot,
        db,
        {
            "guild_id": 1,
            "source_channel_id": 77,
            "recipient_user_id": "42",
            "amount_sol": 2.4,
            "admin_user_id": 999,
        },
    )

    assert result["success"] is True
    payment = next(iter(db.payments.values()))
    assert payment["provider"] == "solana_payouts"


@pytest.mark.anyio
async def test_wallet_reply_valid():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    cog._start_admin_payment_flow = AsyncMock()
    message = FakeMessage(1001, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS)

    await cog.on_message(message)

    assert db.upsert_wallet_calls[0][3] == VALID_SOL_ADDRESS
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_test"
    assert db.intents[intent["intent_id"]]["resolved_by_message_id"] == 1001
    cog._start_admin_payment_flow.assert_awaited_once()


@pytest.mark.anyio
async def test_upsert_wallet_raises_during_active_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    db.active_payment_or_intent_users.add((1, 42))
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object(), db_handler=db)
    cog = AdminChatCog(bot, db, sharer=object())
    cog._start_admin_payment_flow = AsyncMock()

    await cog._handle_wallet_received(
        FakeMessage(1007, FakeAuthor(42), channel, guild, "Wallet22222222222222222222222222222222"),
        intent,
        "Wallet22222222222222222222222222222222",
    )

    assert "active payment is already in flight" in channel.sent_messages[-1].content
    assert db.intents[intent["intent_id"]]["status"] == "failed"
    cog._start_admin_payment_flow.assert_not_awaited()

    grant_thread = FakeChannel(channel_id=1001, guild=guild, parent_id=10)
    grant_db = FakeGrantDB()
    grant_db.wallets[(1, 222, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 222,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    grant_db.active_payment_or_intent_users.add((1, 222))
    payment_service = FakeAdminPaymentService()
    grant_bot = FakeBot(
        grant_thread,
        payment_service=payment_service,
        guilds=[guild],
        db_handler=grant_db,
    )
    grant_cog = GrantsCog(grant_bot)

    await grant_cog._start_payment_flow(
        grant_thread,
        {"applicant_id": 222, "thread_id": 1001, "total_cost_usd": 42.5},
        "Wallet33333333333333333333333333333333",
    )

    assert "active payment in flight" in grant_thread.sent_messages[-1].content
    assert payment_service.request_calls == []


@pytest.mark.anyio
async def test_upsert_wallet_unblocked_after_terminal():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    db.active_payment_or_intent_users.add((1, 42))
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object(), db_handler=db)
    cog = AdminChatCog(bot, db, sharer=object())
    cog._start_admin_payment_flow = AsyncMock()

    await cog._handle_wallet_received(
        FakeMessage(1008, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS),
        intent,
        VALID_SOL_ADDRESS,
    )

    assert db.intents[intent["intent_id"]]["status"] == "awaiting_test"
    cog._start_admin_payment_flow.assert_awaited_once()

    grant_thread = FakeChannel(channel_id=1001, guild=guild, parent_id=10)
    grant_db = FakeGrantDB()
    grant_db.wallets[(1, 222, "solana")] = {
        "wallet_id": "wallet-existing",
        "guild_id": 1,
        "discord_user_id": 222,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    payment_service = FakeAdminPaymentService(
        request_result={"payment_id": "pay-test", "status": "pending_confirmation"},
        confirm_result={"payment_id": "pay-test", "status": "queued"},
    )
    grant_bot = FakeBot(
        grant_thread,
        payment_service=payment_service,
        guilds=[guild],
        db_handler=grant_db,
    )
    grant_cog = GrantsCog(grant_bot)
    grant_cog._apply_tag = AsyncMock()

    await grant_cog._start_payment_flow(
        grant_thread,
        {"applicant_id": 222, "thread_id": 1001, "total_cost_usd": 42.5},
        "Wallet44444444444444444444444444444444",
    )

    assert grant_db.upsert_wallet_calls[0][3] == "Wallet44444444444444444444444444444444"
    assert payment_service.request_calls[0]["wallet_id"] == "wallet-existing"
    assert payment_service.confirm_calls == [
        ("pay-test", {"guild_id": 1, "actor": PaymentActor(PaymentActorKind.AUTO, 222)})
    ]
    assert grant_db.status_updates[-1][1] == "payment_requested"


@pytest.mark.anyio
async def test_wallet_reply_invalid_then_classified():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.25,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    message = FakeMessage(1002, FakeAuthor(42), channel, guild, "not a wallet")

    await cog.on_message(message)

    assert db.intents[intent["intent_id"]]["status"] == "awaiting_wallet"
    assert channel.sent_messages == []


@pytest.mark.anyio
async def test_wallet_reply_no_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    message = FakeMessage(1003, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS)

    await cog.on_message(message)

    assert db.updated_intents == []


@pytest.mark.anyio
async def test_handle_payment_result_test_confirmed():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    status_message = await channel.send("Verifying wallet with a small test payment…", suppress_embeds=True)
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "wallet_id": "wallet-1",
            "status_message_id": status_message.id,
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    payment_ui_cog = FakePaymentUICog()
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=payment_ui_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog.handle_payment_result(
        {
            "payment_id": "payment-test",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": True,
            "status": "confirmed",
            "recipient_wallet": VALID_SOL_ADDRESS,
            "chain": "solana",
            "provider": "solana",
            "confirm_channel_id": 55,
            "confirm_thread_id": None,
            "notify_channel_id": 55,
            "notify_thread_id": None,
            "route_key": "admin_chat",
            "wallet_id": "wallet-1",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    assert payment_service.request_calls == []
    assert payment_ui_cog.confirmation_requests == []
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_test_receipt_confirmation"
    assert db.intents[intent["intent_id"]].get("final_payment_id") is None
    assert db.intents[intent["intent_id"]]["status_message_id"] == status_message.id
    assert db.intents[intent["intent_id"]]["receipt_prompt_message_id"] == status_message.id
    assert len(channel.sent_messages) == 1
    assert len(status_message.edits) == 1
    assert "<@42>" in status_message.content
    assert "Please reply here" in status_message.content
    admin_user = bot.fetched_users[999]
    assert "Test payment confirmed" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_gate_existing_intent_updates_before_admin_dm():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "wallet_id": "wallet-1",
            "status": "awaiting_test_receipt_confirmation",
        },
        guild_id=1,
    )
    wallet_record = {
        "wallet_id": "wallet-1",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": None,
    }
    db.wallets[(1, 42, "solana")] = dict(wallet_record)
    payment_service = FakeAdminPaymentService()
    payment_ui_cog = FakePaymentUICog()
    snapshots = []

    async def capture_admin_dm(payment):
        snapshots.append(
            {
                "payment_id": payment["payment_id"],
                "status": db.intents[intent["intent_id"]]["status"],
                "final_payment_id": db.intents[intent["intent_id"]].get("final_payment_id"),
            }
        )
        return SimpleNamespace(id=f"admin-{payment['payment_id']}")

    payment_ui_cog._send_admin_approval_dm = capture_admin_dm
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=payment_ui_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    result = await cog._gate_existing_intent(channel, intent, wallet_record, 1.75)

    assert result["payment"]["payment_id"] == "payment-final"
    assert payment_service.request_calls[0]["is_test"] is False
    assert db.updated_intents[-1][1] == {
        "status": "awaiting_admin_approval",
        "final_payment_id": "payment-final",
    }
    assert snapshots == [
        {
            "payment_id": "payment-final",
            "status": "awaiting_admin_approval",
            "final_payment_id": "payment-final",
        }
    ]


@pytest.mark.anyio
async def test_handle_test_receipt_positive_marks_wallet_verified_and_queues_admin_approval():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    status_message = await channel.send(
        "<@42> Test payment confirmed. Please reply here with `confirmed` once you see it in your wallet.",
        suppress_embeds=True,
    )
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-1",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": None,
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "wallet_id": "wallet-1",
            "test_payment_id": "payment-test",
            "status_message_id": status_message.id,
            "status": "awaiting_test_receipt_confirmation",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    payment_ui_cog = FakePaymentUICog()
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=payment_ui_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog._handle_test_receipt_positive(
        FakeMessage(1004, FakeAuthor(42), channel, guild, "confirmed"),
        intent,
    )

    assert db.wallet_verified_calls == [("wallet-1", 1)]
    assert payment_service.request_calls[0]["is_test"] is False
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_admin_approval"
    assert db.intents[intent["intent_id"]]["final_payment_id"] == "payment-final"
    assert payment_ui_cog.admin_approval_requests[0]["payment_id"] == "payment-final"
    assert len(channel.sent_messages) == 1
    assert len(status_message.edits) == 1
    assert status_message.content == "Confirmation received. Awaiting admin approval for the final payout."


@pytest.mark.anyio
async def test_resolve_admin_intent_allows_confirmed_test_payment_and_cancels_pending_final(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "status": "confirmed",
        "producer": "admin_chat",
        "is_test": True,
    }
    db.payments["payment-final"] = {
        "payment_id": "payment-final",
        "guild_id": 1,
        "status": "pending_confirmation",
        "producer": "admin_chat",
        "is_test": False,
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.2,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "final_payment_id": "payment-final",
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )
    bot = FakeBot(channel)

    result = await admin_tools.execute_resolve_admin_intent(
        bot,
        db,
        {
            "guild_id": 1,
            "admin_user_id": 999,
            "intent_id": intent["intent_id"],
            "note": "Replacing with corrected amount",
        },
    )

    assert result["success"] is True
    assert db.intents[intent["intent_id"]]["status"] == "cancelled"
    assert db.payments["payment-test"]["status"] == "confirmed"
    assert db.payments["payment-final"]["status"] == "cancelled"
    assert db.cancel_payment_calls == [
        ("payment-final", 1, "Replacing with corrected amount"),
    ]
    assert len(result["payments"]) == 2
    assert bot.fetched_users[999].sent_messages[-1].content.startswith("admin payment intent")


@pytest.mark.anyio
async def test_resolve_admin_intent_still_blocks_confirmed_final_payment(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.payments["payment-final"] = {
        "payment_id": "payment-final",
        "guild_id": 1,
        "status": "confirmed",
        "producer": "admin_chat",
        "is_test": False,
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.2,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )
    bot = FakeBot(channel)

    result = await admin_tools.execute_resolve_admin_intent(
        bot,
        db,
        {
            "guild_id": 1,
            "admin_user_id": 999,
            "intent_id": intent["intent_id"],
            "note": "Replacing with corrected amount",
        },
    )

    assert result["success"] is False
    assert "confirmed" in result["error"]
    assert db.intents[intent["intent_id"]]["status"] == "awaiting_admin_approval"
    assert db.cancel_payment_calls == []


@pytest.mark.anyio
async def test_cancel_payment_cancels_linked_admin_chat_intent(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    db = FakeIntentDB()
    db.payments["payment-final"] = {
        "payment_id": "payment-final",
        "guild_id": 1,
        "status": "pending_confirmation",
        "producer": "admin_chat",
        "is_test": False,
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.2,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )

    result = await admin_tools.execute_cancel_payment(
        db,
        {
            "guild_id": 1,
            "admin_user_id": 999,
            "payment_id": "payment-final",
            "reason": "Recipient requested higher amount",
        },
    )

    assert result["success"] is True
    assert db.payments["payment-final"]["status"] == "cancelled"
    assert db.intents[intent["intent_id"]]["status"] == "cancelled"
    assert result["intent"]["intent_id"] == intent["intent_id"]
    assert result["intent"]["status"] == "cancelled"


@pytest.mark.anyio
async def test_handle_test_receipt_negative_moves_to_manual_review():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "recipient_wallet": VALID_SOL_ADDRESS,
        "tx_signature": "sig-123",
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog._handle_test_receipt_negative(
        FakeMessage(1005, FakeAuthor(42), channel, guild, "no"),
        intent,
    )

    assert db.intents[intent["intent_id"]]["status"] == "manual_review"
    assert len(channel.sent_messages) == 2
    assert "<@42>" in channel.sent_messages[0].content
    assert "admin review" in channel.sent_messages[0].content
    assert cog._admin_mention in channel.sent_messages[0].content
    assert "sig-123" in channel.sent_messages[1].content
    assert VALID_SOL_ADDRESS in channel.sent_messages[1].content
    admin_user = bot.fetched_users[999]
    assert "Manual review needed" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_handle_test_receipt_negative_sends_recipient_facing_notice():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "recipient_wallet": VALID_SOL_ADDRESS,
        "tx_signature": "sig-123",
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog._handle_test_receipt_negative(
        FakeMessage(1015, FakeAuthor(42), channel, guild, "no"),
        intent,
    )

    assert len(channel.sent_messages) == 2
    assert "<@42>" in channel.sent_messages[0].content
    assert "admin review" in channel.sent_messages[0].content
    assert cog._admin_mention in channel.sent_messages[0].content
    assert "sig-123" in channel.sent_messages[1].content
    assert VALID_SOL_ADDRESS in channel.sent_messages[1].content
    admin_user = bot.fetched_users[999]
    assert "Manual review needed" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_pending_recipient_not_received_escalates_immediately():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "recipient_wallet": VALID_SOL_ADDRESS,
        "tx_signature": "sig-789",
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
            "ambiguous_reply_count": 0,
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    handled = await cog._handle_pending_recipient_message(
        FakeMessage(1008, FakeAuthor(42), channel, guild, "not received"),
        intent,
    )

    assert handled is True
    assert db.intents[intent["intent_id"]]["status"] == "manual_review"
    assert db.intents[intent["intent_id"]]["ambiguous_reply_count"] == 0
    assert "Recipient reported the test payment was not received." in channel.sent_messages[-1].content


@pytest.mark.anyio
async def test_handle_test_receipt_ambiguous_escalates_on_second_reply():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.payments["payment-test"] = {
        "payment_id": "payment-test",
        "guild_id": 1,
        "recipient_wallet": VALID_SOL_ADDRESS,
        "tx_signature": "sig-456",
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
            "ambiguous_reply_count": 0,
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog._handle_test_receipt_ambiguous(
        FakeMessage(1006, FakeAuthor(42), channel, guild, "maybe"),
        intent,
    )

    assert db.intents[intent["intent_id"]]["ambiguous_reply_count"] == 1
    assert len(channel.sent_messages) == 1
    assert "<@42>" in channel.sent_messages[0].content
    assert "confirmed" in channel.sent_messages[0].content

    await cog._handle_test_receipt_ambiguous(
        FakeMessage(1007, FakeAuthor(42), channel, guild, "not sure"),
        db.get_admin_payment_intent(intent["intent_id"], 1),
    )

    assert db.intents[intent["intent_id"]]["ambiguous_reply_count"] == 2
    assert db.intents[intent["intent_id"]]["status"] == "manual_review"
    assert "multiple ambiguous receipt replies" in channel.sent_messages[-1].content


@pytest.mark.anyio
async def test_handle_test_receipt_ambiguous_first_reply_posts_default_clarification():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
            "ambiguous_reply_count": 0,
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog._handle_test_receipt_ambiguous(
        FakeMessage(1016, FakeAuthor(42), channel, guild, "maybe"),
        intent,
    )

    assert db.intents[intent["intent_id"]]["ambiguous_reply_count"] == 1
    assert "<@42>" in channel.sent_messages[-1].content
    assert "confirmed" in channel.sent_messages[-1].content
    assert "not received" in channel.sent_messages[-1].content


@pytest.mark.anyio
async def test_handle_test_receipt_ambiguous_first_reply_uses_llm_reply_when_provided():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
            "ambiguous_reply_count": 0,
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog._handle_test_receipt_ambiguous(
        FakeMessage(1017, FakeAuthor(42), channel, guild, "wait let me check"),
        intent,
        clarification_reply="Please say yes once you see 0.01 SOL",
    )

    assert db.intents[intent["intent_id"]]["ambiguous_reply_count"] == 1
    assert "<@42>" in channel.sent_messages[-1].content
    assert "Please say yes once you see 0.01 SOL" in channel.sent_messages[-1].content


@pytest.mark.anyio
async def test_gate_fresh_intent_atomic_creates_awaiting_admin_approval_intent():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    wallet_record = {
        "wallet_id": "wallet-1",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": "2026-04-10T00:00:00Z",
    }
    db.wallets[(1, 42, "solana")] = dict(wallet_record)
    payment_service = FakeAdminPaymentService()
    payment_ui_cog = FakePaymentUICog()
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=payment_ui_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    result = await cog._gate_fresh_intent_atomic(
        channel=channel,
        guild_id=1,
        recipient_user_id=42,
        amount_sol=2.25,
        source_channel_id=55,
        wallet_record=wallet_record,
        admin_user_id=999,
        reason="verified fast path",
        producer_ref="1_42_999999",
    )

    assert result["payment"]["payment_id"] == "payment-final"
    assert payment_service.request_calls[0]["metadata"]["intent_id"] == result["intent"]["intent_id"]
    assert result["intent"]["status"] == "awaiting_admin_approval"
    assert result["intent"]["final_payment_id"] == "payment-final"
    assert payment_ui_cog.admin_approval_requests[0]["payment_id"] == "payment-final"


@pytest.mark.anyio
async def test_admin_approval_view_re_registers_pending_admin_approval_intents():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )
    db.list_intents_by_status = lambda guild_id, status: [dict(intent)] if status == "awaiting_admin_approval" else []
    ui_cog = PaymentUICog(bot, db, payment_service=object())

    await ui_cog._register_pending_admin_approval_views()

    assert len(bot.added_views) == 1
    view, message_id = bot.added_views[0]
    assert message_id is None
    assert isinstance(view, AdminApprovalView)
    assert view.payment_id == "payment-final"
    assert view.timeout is None
    assert view.children[0].custom_id == "payment_admin_approve:payment-final"


@pytest.mark.anyio
async def test_admin_approval_view_only_admin_can_click():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    payment_service = FakeAdminPaymentService(confirm_result={"payment_id": "payment-final", "status": "queued"})
    bot = FakeBot(channel, payment_service=payment_service)
    ui_cog = PaymentUICog(bot, FakeIntentDB(), payment_service=payment_service)
    view = AdminApprovalView(ui_cog, "payment-final")
    interaction = FakeInteraction(123, message=SimpleNamespace(edit=AsyncMock()))

    await view._confirm_button_pressed(interaction)

    assert interaction.response.calls == [{"ephemeral": True}]
    assert interaction.followup.messages == [{"content": "admin-only", "ephemeral": True}]
    assert payment_service.confirm_calls == []


@pytest.mark.anyio
async def test_admin_approval_view_click_queues_payment_and_cleans_both_prompts():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    payment_service = FakeAdminPaymentService(confirm_result={"payment_id": "payment-final", "status": "queued"})
    bot = FakeBot(channel, payment_service=payment_service)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "prompt_message_id": 900,
            "receipt_prompt_message_id": 901,
            "final_payment_id": "payment-final",
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )
    db.find_admin_chat_intent_by_payment_id = (
        lambda payment_id: db.get_admin_payment_intent(intent["intent_id"], 1)
        if payment_id == "payment-final"
        else None
    )
    ui_cog = PaymentUICog(bot, db, payment_service=payment_service)
    view = AdminApprovalView(ui_cog, "payment-final")
    interaction_message = SimpleNamespace(edit=AsyncMock())
    interaction = FakeInteraction(999, message=interaction_message)

    with patch(
        "src.features.payments.payment_ui_cog.safe_delete_messages",
        new=AsyncMock(return_value=None),
    ) as mock_safe_delete:
        await view._confirm_button_pressed(interaction)

    assert interaction.response.calls == [{"ephemeral": True}]
    assert interaction.followup.messages == [{"content": "payment confirmed, sending", "ephemeral": False}]
    assert payment_service.confirm_calls == [
        (
            "payment-final",
            {
                "guild_id": 1,
                "actor": PaymentActor(PaymentActorKind.ADMIN_DM, 999),
            },
        )
    ]
    interaction_message.edit.assert_awaited_once_with(
        content="✅ approved — queued for sending",
        view=view,
    )
    assert view.children[0].disabled is True
    assert channel.sent_messages[-1].content == "Payment to <@42> queued for sending."
    mock_safe_delete.assert_awaited_once()
    assert mock_safe_delete.await_args.args[0] is channel
    assert mock_safe_delete.await_args.args[1] == [900, 901]


@pytest.mark.anyio
async def test_post_admin_approval_thread_update_edits_status_message():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    status_message = await channel.send(
        "Confirmation received. Awaiting admin approval for the final payout.",
        suppress_embeds=True,
    )
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "status_message_id": status_message.id,
            "final_payment_id": "payment-final",
            "producer_ref": "1_42_123",
            "requested_amount_sol": 1.75,
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )
    db.find_admin_chat_intent_by_payment_id = (
        lambda payment_id: db.get_admin_payment_intent(intent["intent_id"], 1)
        if payment_id == "payment-final"
        else None
    )
    ui_cog = PaymentUICog(bot, db, payment_service=object())

    await ui_cog._post_admin_approval_thread_update({"payment_id": "payment-final", "recipient_discord_id": 42})

    assert len(channel.sent_messages) == 1
    assert status_message.edits == [{"content": "Payout queued for sending.", "suppress": True}]


@pytest.mark.anyio
async def test_cleanup_admin_intent_messages_preserves_status_message_id():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    bot = FakeBot(channel, payment_service=object())
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "prompt_message_id": 901,
            "receipt_prompt_message_id": 900,
            "status_message_id": 900,
            "final_payment_id": "payment-final",
            "producer_ref": "1_42_123",
            "requested_amount_sol": 1.75,
            "status": "awaiting_admin_approval",
        },
        guild_id=1,
    )
    db.find_admin_chat_intent_by_payment_id = (
        lambda payment_id: db.get_admin_payment_intent(intent["intent_id"], 1)
        if payment_id == "payment-final"
        else None
    )
    ui_cog = PaymentUICog(bot, db, payment_service=object())

    with patch(
        "src.features.payments.payment_ui_cog.safe_delete_messages",
        new=AsyncMock(return_value=None),
    ) as mock_safe_delete:
        await ui_cog._cleanup_admin_intent_messages({"payment_id": "payment-final"})

    mock_safe_delete.assert_awaited_once()
    assert mock_safe_delete.await_args.args[0] is channel
    assert mock_safe_delete.await_args.args[1] == [901]


@pytest.mark.anyio
async def test_restart_filter_state_aware():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    pending = [
        {"payment_id": "pay-skip-approval", "guild_id": 1, "producer": "admin_chat", "is_test": False, "status": "pending_confirmation"},
        {"payment_id": "pay-skip-init", "guild_id": 1, "producer": "admin_chat", "is_test": False, "status": "pending_confirmation"},
        {"payment_id": "pay-legacy", "guild_id": 1, "producer": "admin_chat", "is_test": False, "status": "pending_confirmation"},
        {"payment_id": "pay-grants", "guild_id": 1, "producer": "grants", "is_test": False, "status": "pending_confirmation"},
    ]

    class LocalPaymentService:
        def get_pending_confirmation_payments(self, guild_ids=None):
            if guild_ids is None:
                return list(pending)
            return [row for row in pending if row["guild_id"] in guild_ids]

    bot = FakeBot(channel, payment_service=LocalPaymentService())
    db = FakeIntentDB()
    db.get_pending_confirmation_admin_chat_intents_by_payment = lambda payment_ids: {
        "pay-skip-approval": {"intent_id": "intent-1", "status": "awaiting_admin_approval"},
        "pay-skip-init": {"intent_id": "intent-2", "status": "awaiting_admin_init"},
        "pay-legacy": {"intent_id": "intent-3", "status": "awaiting_confirmation"},
    }
    ui_cog = PaymentUICog(bot, db, payment_service=bot.payment_service)

    await ui_cog._register_pending_confirmation_views()

    registered_payment_ids = [view.payment_id for view, _message_id in bot.added_views]
    assert registered_payment_ids == ["pay-legacy", "pay-grants"]
    assert all(isinstance(view, PaymentConfirmView) for view, _message_id in bot.added_views)


@pytest.mark.anyio
async def test_payment_confirm_view_not_sent_for_admin_chat_real_payments():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-1",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": None,
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "wallet_id": "wallet-1",
            "test_payment_id": "payment-test",
            "status": "awaiting_test_receipt_confirmation",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    payment_ui_cog = FakePaymentUICog()
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=payment_ui_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog._handle_test_receipt_positive(
        FakeMessage(1009, FakeAuthor(42), channel, guild, "confirmed"),
        intent,
    )

    assert payment_ui_cog.confirmation_requests == []
    assert payment_ui_cog.admin_approval_requests[0]["payment_id"] == "payment-final"


@pytest.mark.anyio
async def test_handle_payment_result_test_failed():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    status_message = await channel.send("Verifying wallet with a small test payment…", suppress_embeds=True)
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "status_message_id": status_message.id,
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=FakePaymentUICog())
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog.handle_payment_result(
        {
            "payment_id": "payment-test",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": True,
            "status": "failed",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    assert payment_service.request_calls == []
    assert db.intents[intent["intent_id"]]["status"] == "failed"
    assert len(channel.sent_messages) == 1
    assert status_message.edits == [
        {
            "content": "Wallet verification ended in `failed`. An admin will review it manually.",
            "suppress": True,
        }
    ]


@pytest.mark.anyio
async def test_confirmation_reply():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status": "awaiting_confirmation",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService(confirm_result={"payment_id": "payment-final", "status": "queued"})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    message = FakeMessage(1004, FakeAuthor(42), channel, guild, "yes, send it")

    await cog._handle_confirmation_received(message, intent)

    assert payment_service.confirm_calls == [
        ("payment-final", {"guild_id": 1, "actor": PaymentActor(PaymentActorKind.RECIPIENT_MESSAGE, 42)})
    ]
    assert db.intents[intent["intent_id"]]["status"] == "confirmed"


@pytest.mark.anyio
async def test_handle_payment_result_final_confirmed_notifies_admin():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    status_message = await channel.send("Payout queued for sending.", suppress_embeds=True)
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "final_payment_id": "payment-final",
            "status_message_id": status_message.id,
            "status": "confirmed",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog.handle_payment_result(
        {
            "payment_id": "payment-final",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.75,
            "tx_signature": "sig-123",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    assert db.intents[intent["intent_id"]]["status"] == "completed"
    assert len([message for message in channel.sent_messages if message.reference is None]) == 1
    assert len(status_message.edits) >= 1
    assert status_message.edits[-1]["content"] == "Payout sending…"
    reply_messages = [message for message in channel.sent_messages if message.reference == status_message.id]
    assert len(reply_messages) == 1
    assert "<@42>" in reply_messages[0].content
    assert "Payment sent" in reply_messages[0].content
    assert reply_messages[0].kwargs["mention_author"] is True
    assert reply_messages[0].kwargs["suppress_embeds"] is True
    admin_user = bot.fetched_users[999]
    assert "Final payment confirmed" in admin_user.sent_messages[-1].content


@pytest.mark.anyio
async def test_concurrent_intents_same_channel():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    intent_one = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.0,
            "producer_ref": "1_42_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    intent_two = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 43,
            "requested_amount_sol": 2.0,
            "producer_ref": "1_43_123",
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())
    matched = []

    async def record_wallet(message, intent, wallet_address):
        matched.append((message.author.id, intent["intent_id"], wallet_address))

    cog._handle_wallet_received = record_wallet

    await cog.on_message(FakeMessage(1005, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS))
    await cog.on_message(FakeMessage(1006, FakeAuthor(43), channel, guild, VALID_SOL_ADDRESS))

    assert matched == [
        (42, intent_one["intent_id"], VALID_SOL_ADDRESS),
        (43, intent_two["intent_id"], VALID_SOL_ADDRESS),
    ]


@pytest.mark.anyio
async def test_request_payment_amount_token():
    provider = FakeProvider()
    db = FakePaymentRequestDB()
    service = PaymentService(db_handler=db, providers={"solana": provider}, test_payment_amount=0.01)

    result = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-1",
        guild_id=1,
        recipient_wallet=VALID_SOL_ADDRESS,
        chain="solana",
        provider="solana",
        is_test=False,
        amount_token=1.5,
        confirm_channel_id=55,
        notify_channel_id=55,
        recipient_discord_id=42,
        wallet_id="wallet-1",
        metadata={"intent_id": "intent-1"},
    )

    assert result is not None
    assert result["amount_token"] == 1.5
    assert result["amount_usd"] is None
    assert result["token_price_usd"] is None
    assert provider.price_calls == 0


@pytest.mark.anyio
async def test_admin_success_dm_over_threshold(monkeypatch, clear_payment_policy_env):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.setenv("ADMIN_PAYMENT_SUCCESS_DM_THRESHOLD_USD", "100")
    monkeypatch.setenv("ADMIN_PAYMENT_SUCCESS_DM_PROVIDERS", "solana_payouts")
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=payment_service)
    cog._notify_payment_result = AsyncMock()
    cog._handoff_terminal_result = AsyncMock()
    cog._dm_admin_payment_success = AsyncMock()
    cog._dm_admin_payment_failure = AsyncMock()

    await cog._handle_terminal_payment(
        {
            "payment_id": "pay-success",
            "producer": "admin_chat",
            "producer_ref": "intent-1",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.0,
            "amount_usd": 150.0,
            "recipient_wallet": VALID_SOL_ADDRESS,
        }
    )
    await cog._handle_terminal_payment(
        {
            "payment_id": "pay-under",
            "producer": "admin_chat",
            "producer_ref": "intent-2",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.0,
            "amount_usd": 50.0,
            "recipient_wallet": VALID_SOL_ADDRESS,
        }
    )
    await cog._handle_terminal_payment(
        {
            "payment_id": "pay-failure",
            "producer": "admin_chat",
            "producer_ref": "intent-3",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "failed",
            "amount_token": 1.0,
            "amount_usd": 150.0,
            "recipient_wallet": VALID_SOL_ADDRESS,
            "last_error": "manual review",
        }
    )

    assert cog._dm_admin_payment_success.await_count == 1
    assert cog._dm_admin_payment_failure.await_count == 1


@pytest.mark.anyio
async def test_handle_payment_result_suppresses_result_card_for_admin_chat():
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    bot = FakeBot(channel, payment_service=SimpleNamespace(providers={}))
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=bot.payment_service)
    cog._notify_payment_result = AsyncMock()
    cog._handoff_terminal_result = AsyncMock()
    cog._dm_admin_payment_success = AsyncMock()
    cog._dm_admin_payment_failure = AsyncMock()

    with patch(
        "src.features.payments.payment_worker_cog.safe_delete_messages",
        new=AsyncMock(return_value=None),
    ):
        await cog._handle_terminal_payment(
            {
                "payment_id": "pay-admin-chat",
                "producer": "admin_chat",
                "provider": "solana_payouts",
                "chain": "solana",
                "is_test": False,
                "status": "confirmed",
                "amount_token": 1.0,
                "amount_usd": 0.0,
                "notify_channel_id": 55,
                "metadata": {"cleanup_message_ids": []},
            }
        )

    cog._notify_payment_result.assert_not_awaited()
    cog._handoff_terminal_result.assert_awaited_once()


@pytest.mark.anyio
async def test_handle_payment_result_still_notifies_non_admin_chat_producers():
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    bot = FakeBot(channel, payment_service=SimpleNamespace(providers={}))
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=bot.payment_service)
    cog._notify_payment_result = AsyncMock()
    cog._handoff_terminal_result = AsyncMock()
    cog._dm_admin_payment_success = AsyncMock()
    cog._dm_admin_payment_failure = AsyncMock()

    with patch(
        "src.features.payments.payment_worker_cog.safe_delete_messages",
        new=AsyncMock(return_value=None),
    ):
        await cog._handle_terminal_payment(
            {
                "payment_id": "pay-grants",
                "producer": "grants",
                "provider": "solana_payouts",
                "chain": "solana",
                "is_test": False,
                "status": "confirmed",
                "amount_token": 1.0,
                "amount_usd": 0.0,
                "notify_channel_id": 55,
                "metadata": {"cleanup_message_ids": []},
            }
        )

    cog._notify_payment_result.assert_awaited_once()
    cog._handoff_terminal_result.assert_awaited_once()


def test_update_admin_payment_intent_tolerates_missing_status_message_column():
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    db.reject_status_message_id = True

    updated = DatabaseHandler.update_admin_payment_intent(
        db,
        intent["intent_id"],
        {"status": "awaiting_test_receipt_confirmation", "status_message_id": 900},
        1,
    )

    assert updated is not None
    assert updated["status"] == "awaiting_test_receipt_confirmation"
    assert db.supabase_calls[0]["status_message_id"] == 900
    assert "status_message_id" not in db.supabase_calls[1]

    error_db = FakeIntentDB()
    error_intent = error_db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    error_db.supabase_failures.append("column some_other_field does not exist")

    assert DatabaseHandler.update_admin_payment_intent(
        error_db,
        error_intent["intent_id"],
        {"status": "awaiting_test_receipt_confirmation", "status_message_id": 900},
        1,
    ) is None


@pytest.mark.anyio
async def test_admin_chat_status_message_reused_across_transitions():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(guild=guild)
    db = FakeIntentDB()
    db.wallets[(1, 42, "solana")] = {
        "wallet_id": "wallet-1",
        "guild_id": 1,
        "discord_user_id": 42,
        "chain": "solana",
        "wallet_address": VALID_SOL_ADDRESS,
        "verified_at": None,
    }
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "admin_user_id": 999,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "wallet_id": "wallet-1",
            "status": "awaiting_test",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService()
    payment_ui_cog = FakePaymentUICog()
    bot = FakeBot(channel, payment_service=payment_service, payment_ui_cog=payment_ui_cog)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service

    await cog._start_admin_payment_flow(channel, intent)
    await cog.handle_payment_result(
        {
            "payment_id": "payment-test",
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": True,
            "status": "confirmed",
            "amount_token": 0.001,
            "tx_signature": "sig-test",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )
    await cog._handle_test_receipt_positive(
        FakeMessage(1004, FakeAuthor(42), channel, guild, "confirmed"),
        db.get_admin_payment_intent(intent["intent_id"], 1),
    )
    final_intent = db.get_admin_payment_intent(intent["intent_id"], 1)
    await cog.handle_payment_result(
        {
            "payment_id": final_intent["final_payment_id"],
            "producer": "admin_chat",
            "guild_id": 1,
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.75,
            "tx_signature": "sig-final",
            "metadata": {"intent_id": intent["intent_id"]},
        }
    )

    status_messages = [message for message in channel.sent_messages if message.reference is None]
    reply_messages = [message for message in channel.sent_messages if message.reference is not None]

    assert [message.id for message in status_messages] == [900]
    assert len(status_messages[0].edits) == 3
    assert len(reply_messages) == 1
    assert reply_messages[0].reference == status_messages[0].id
    assert db.intents[intent["intent_id"]]["status_message_id"] == status_messages[0].id
    assert db.intents[intent["intent_id"]]["receipt_prompt_message_id"] == status_messages[0].id


@pytest.mark.anyio
async def test_admin_success_dm_sees_derived_usd(monkeypatch, clear_payment_policy_env):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=payment_service)

    await cog._dm_admin_payment_success(
        {
            "payment_id": "pay-success",
            "producer": "admin_chat",
            "producer_ref": "intent-1",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.5,
            "amount_usd": 321.09,
            "recipient_wallet": VALID_SOL_ADDRESS,
            "tx_signature": "sig-123",
        }
    )

    message = bot.fetched_users[999].sent_messages[-1].content
    assert "✅ **Payment Completed**" in message
    assert "- USD: $321.09" in message
    assert "- Wallet: `1111...1111`" in message
    assert "requires manual review" not in message


@pytest.mark.anyio
async def test_admin_payment_dm_paths_route_through_delivery_helper(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    channel = FakeChannel(guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=payment_service)
    cog._deliver_admin_alert = AsyncMock(return_value=True)

    await cog._dm_admin_payment_success(
        {
            "payment_id": "pay-success",
            "producer": "admin_chat",
            "producer_ref": "intent-1",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "confirmed",
            "amount_token": 1.5,
            "amount_usd": 321.09,
            "recipient_wallet": VALID_SOL_ADDRESS,
        }
    )
    await cog._dm_admin_payment_failure(
        {
            "payment_id": "pay-failure",
            "producer": "admin_chat",
            "producer_ref": "intent-2",
            "provider": "solana_payouts",
            "chain": "solana",
            "is_test": False,
            "status": "manual_hold",
            "amount_token": 1.5,
            "recipient_wallet": VALID_SOL_ADDRESS,
            "last_error": "rpc_unreachable",
        }
    )

    assert cog._deliver_admin_alert.await_count == 2


@pytest.mark.anyio
async def test_admin_alert_falls_back_to_channel_on_dm_forbidden(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.setenv("ADMIN_FALLBACK_CHANNEL_ID", "55")
    channel = FakeChannel(channel_id=55, guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    blocked_admin = FakeAuthor(999)
    blocked_admin.send = AsyncMock(
        side_effect=discord.Forbidden(FakeResponse(403), "forbidden")
    )
    bot.fetched_users[999] = blocked_admin
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=payment_service)

    with patch(
        "src.features.payments.payment_worker_cog.safe_send_message",
        new=AsyncMock(return_value=SimpleNamespace(id=123)),
    ) as mock_safe_send:
        await cog._dm_admin_payment_success(
            {
                "payment_id": "pay-success",
                "producer": "admin_chat",
                "producer_ref": "intent-1",
                "provider": "solana_payouts",
                "chain": "solana",
                "is_test": False,
                "status": "confirmed",
                "amount_token": 1.5,
                "amount_usd": 321.09,
                "recipient_wallet": VALID_SOL_ADDRESS,
            }
        )

    mock_safe_send.assert_awaited_once()
    assert mock_safe_send.await_args.kwargs["channel"] is channel


@pytest.mark.anyio
async def test_admin_alert_logs_error_when_dm_and_fallback_fail(monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.setenv("ADMIN_FALLBACK_CHANNEL_ID", "55")
    channel = FakeChannel(channel_id=55, guild=SimpleNamespace(id=1))
    payment_service = SimpleNamespace(providers={"solana_payouts": FakeProvider()})
    bot = FakeBot(channel, payment_service=payment_service)
    blocked_admin = FakeAuthor(999)
    blocked_admin.send = AsyncMock(
        side_effect=discord.HTTPException(FakeResponse(429), "rate limited")
    )
    bot.fetched_users[999] = blocked_admin
    cog = PaymentWorkerCog(bot, FakePaymentRequestDB(), payment_service=payment_service)

    caplog.set_level("ERROR")
    with patch(
        "src.features.payments.payment_worker_cog.safe_send_message",
        new=AsyncMock(side_effect=RuntimeError("fallback down")),
    ):
        delivered = await cog._deliver_admin_alert("hello", admin_user=blocked_admin)

    assert delivered is False
    assert "[PaymentWorkerCog] admin alert undeliverable" in caplog.text


@pytest.mark.anyio
async def test_startup_reconciliation():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(channel_id=55, guild=guild)
    db = FakeIntentDB()
    intent = db.create_admin_payment_intent(
        {
            "channel_id": 55,
            "recipient_user_id": 42,
            "requested_amount_sol": 1.75,
            "producer_ref": "1_42_123",
            "prompt_message_id": 1000,
            "status": "awaiting_wallet",
        },
        guild_id=1,
    )
    payment_service = FakeAdminPaymentService(confirm_result={"payment_id": "payment-final", "status": "queued"})
    bot = FakeBot(channel, payment_service=payment_service)
    cog = AdminChatCog(bot, db, sharer=object())
    cog.payment_service = payment_service
    message = FakeMessage(1001, FakeAuthor(42), channel, guild, "yes")
    channel._messages = [FakeMessage(1001, FakeAuthor(42), channel, guild, VALID_SOL_ADDRESS)]
    cog._handle_wallet_received = AsyncMock()

    await cog.cog_load()
    assert bot.ready_waits == 0
    await cog.on_ready()

    cog._handle_wallet_received.assert_awaited_once()
    assert db.intents[intent["intent_id"]]["last_scanned_message_id"] == 1001


@pytest.mark.anyio
async def test_admin_chat_cog_load_does_not_block_on_wait_until_ready():
    guild = SimpleNamespace(id=1)
    channel = FakeChannel(channel_id=55, guild=guild)
    db = FakeIntentDB()
    bot = FakeBot(channel, payment_service=object())
    cog = AdminChatCog(bot, db, sharer=object())

    await cog.cog_load()

    assert bot.ready_waits == 0
