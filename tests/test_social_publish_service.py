import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

import pytest

from src.common.db_handler import WalletUpdateBlockedError
from src.features.payments.payment_service import PaymentActor, PaymentActorKind
from src.features.payments.solana_provider import SolanaProvider
from src.features.sharing.models import PublicationSourceContext, SocialPublishRequest
from src.features.sharing.social_publish_service import SocialPublishService
from src.features.sharing.providers.youtube_zapier_provider import YouTubeZapierProvider
from src.features.payments.payment_service import PaymentService
from src.features.payments.provider import SendResult


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def social_signing_secret(monkeypatch):
    monkeypatch.setenv("SOCIAL_PUBLISH_SIGNING_SECRET", "test-social-signing-secret")


class FakeProvider:
    def __init__(self, media_ids=None):
        self.delete_calls = []
        self.publish_calls = []
        self._media_ids = media_ids  # None -> omit, list -> include in result

    async def publish(self, request):
        self.publish_calls.append(request)
        base = {}
        if self._media_ids is not None:
            base["media_ids"] = list(self._media_ids)
        if request.action == "retweet":
            return {
                **base,
                "provider_ref": request.target_post_ref,
                "provider_url": "https://x.com/example/status/{0}".format(request.target_post_ref),
                "tweet_id": request.target_post_ref,
                "tweet_url": "https://x.com/example/status/{0}".format(request.target_post_ref),
                "delete_supported": False,
            }
        return {
            **base,
            "provider_ref": "tweet-123",
            "provider_url": "https://x.com/example/status/tweet-123",
            "tweet_id": "tweet-123",
            "tweet_url": "https://x.com/example/status/tweet-123",
            "delete_supported": True,
        }

    async def delete(self, publication):
        self.delete_calls.append(publication["publication_id"])
        return publication.get("delete_supported", False)

    def normalize_target_ref(self, target_ref):
        return target_ref


class FailingProvider(FakeProvider):
    def __init__(self, media_ids=None):
        super().__init__(media_ids=media_ids)

    async def publish(self, request):
        self.publish_calls.append(request)
        raise RuntimeError("provider boom")


class FakeZapierResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return '{"youtube_video_id": "yt-123"}'

    async def json(self, content_type=None):
        return {"youtube_video_id": "yt-123"}


class FakeZapierSession:
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json):
        self.payloads.append({"url": url, "json": json})
        return FakeZapierResponse()


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.shared_posts = []
        self.deleted_shared_posts = []
        self.supabase = None
        self.server_config = None

    def create_social_publication(self, data, guild_id=None):
        publication_id = "pub-{0}".format(len(self.rows) + 1)
        row = dict(data)
        row["publication_id"] = publication_id
        row["guild_id"] = guild_id or row.get("guild_id")
        self.rows[publication_id] = row
        return row

    def get_social_publication_by_id(self, publication_id, guild_id=None):
        row = self.rows.get(publication_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return row

    def mark_social_publication_processing(self, publication_id, guild_id=None, attempt_count=None, retry_after=None):
        row = self.rows[publication_id]
        row["status"] = "processing"
        if attempt_count is not None:
            row["attempt_count"] = attempt_count
        if retry_after is not None:
            row["retry_after"] = retry_after
        return True

    def mark_social_publication_succeeded(self, publication_id, guild_id=None, provider_ref=None, provider_url=None, delete_supported=None):
        row = self.rows[publication_id]
        row["status"] = "succeeded"
        row["provider_ref"] = provider_ref
        row["provider_url"] = provider_url
        row["delete_supported"] = delete_supported
        return True

    def mark_social_publication_failed(self, publication_id, last_error, guild_id=None, retry_after=None):
        row = self.rows[publication_id]
        row["status"] = "failed"
        row["last_error"] = last_error
        row["retry_after"] = retry_after
        return True

    def mark_social_publication_cancelled(self, publication_id, guild_id=None, last_error=None):
        row = self.rows[publication_id]
        row["status"] = "cancelled"
        row["last_error"] = last_error
        return True

    def record_shared_post(self, **kwargs):
        self.shared_posts.append(kwargs)
        return True

    def mark_shared_post_deleted(self, discord_message_id, platform, guild_id=None):
        self.deleted_shared_posts.append((discord_message_id, platform, guild_id))
        return True

    def find_existing_social_posts(self, topic_id, platform, guild_id=None,
                                   draft_text=None, limit=20):
        """Find existing social publications related to a live-update topic."""
        matches = []
        rows = sorted(self.rows.values(),
                      key=lambda r: r.get("created_at", ""), reverse=True)
        for row in rows[:limit]:
            request_payload = row.get("request_payload") or {}
            source_context = request_payload.get("source_context") or {}
            metadata = source_context.get("metadata") or {}
            row_platform = row.get("platform", "")
            if metadata.get("topic_id") == topic_id and row_platform == platform:
                if guild_id is not None and row.get("guild_id") != guild_id:
                    continue
                row_dict = dict(row)
                if draft_text:
                    existing_text = row_dict.get("text") or (
                        request_payload.get("text") or "")
                    # Simple 5-gram containment
                    row_dict["_similarity"] = self._check_content_similarity(
                        draft_text, existing_text)
                matches.append(row_dict)
        return matches

    @staticmethod
    def _check_content_similarity(text_a, text_b):
        """Character 5-gram containment (simplified for tests)."""
        if not text_a or not text_b:
            return 0.0

        def _normalise(t):
            return " ".join(t.lower().split())

        na = _normalise(text_a)
        nb = _normalise(text_b)
        if len(na) < 5 or len(nb) < 5:
            return 1.0 if na in nb or nb in na else 0.0
        b_grams = {nb[i:i + 5] for i in range(len(nb) - 4)}
        a_grams = [na[i:i + 5] for i in range(len(na) - 4)]
        if not a_grams:
            return 0.0
        matches = sum(1 for g in a_grams if g in b_grams)
        return matches / len(a_grams)


def build_request(action="post", scheduled_at=None, target_post_ref=None):
    return SocialPublishRequest(
        message_id=1,
        channel_id=2,
        guild_id=3,
        user_id=4,
        platform="twitter",
        action=action,
        scheduled_at=scheduled_at,
        target_post_ref=target_post_ref,
        text="hello world" if action != "retweet" else None,
        source_kind="admin_chat",
        source_context=PublicationSourceContext(
            source_kind="admin_chat",
            metadata={"user_details": {"username": "poster"}, "original_content": "hello"},
        ),
    )


def build_youtube_request():
    return SocialPublishRequest(
        message_id=10,
        channel_id=20,
        guild_id=30,
        user_id=40,
        platform="youtube",
        action="post",
        route_override={
            "route_key": "youtube-main",
            "route_config": {
                "privacy_status": "unlisted",
                "default_tags": ["ADOS", "Banodoco"],
            },
        },
        text="Yaron Inger - Your Model Now\nFull ADOS Paris talk.",
        media_hints=[
            {
                "url": "https://cdn.example.com/yaron.mp4",
                "local_path": "/tmp/yaron.mp4",
                "content_type": "video/mp4",
            }
        ],
        source_kind="admin_chat",
        source_context=PublicationSourceContext(
            source_kind="admin_chat",
            metadata={"youtube_description": "Custom YouTube description."},
        ),
    )


class FakeServerConfig:
    def __init__(self, enabled=True, route=None):
        self.enabled = enabled
        self.route = route
        self.resolve_calls = []

    def is_feature_enabled(self, guild_id, channel_id, feature):
        assert feature == "sharing"
        return self.enabled

    def resolve_social_route(self, guild_id, channel_id, platform):
        self.resolve_calls.append((guild_id, channel_id, platform))
        if not self.enabled:
            return None
        return self.route


async def test_publish_now_enqueue_execute_and_delete_branching():
    db_handler = FakeDB()
    provider = FakeProvider()
    service = SocialPublishService(db_handler, providers={"twitter": provider}, logger_instance=None)

    post_result = await service.publish_now(build_request())
    assert post_result.success is True
    assert post_result.publication_id == "pub-1"
    assert post_result.delete_supported is True
    assert len(db_handler.shared_posts) == 1

    reply_result = await service.publish_now(build_request(action="reply", target_post_ref="12345"))
    assert reply_result.success is True
    assert reply_result.delete_supported is True
    assert len(db_handler.shared_posts) == 1

    retweet_result = await service.publish_now(build_request(action="retweet", target_post_ref="777"))
    assert retweet_result.success is True
    assert retweet_result.delete_supported is False
    assert len(db_handler.shared_posts) == 1

    scheduled_at = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    queued_result = await service.enqueue(build_request(scheduled_at=scheduled_at))
    assert queued_result.success is True
    assert db_handler.rows[queued_result.publication_id]["status"] == "queued"
    assert db_handler.rows[queued_result.publication_id]["scheduled_at"] == scheduled_at

    db_handler.rows["pub-exec"] = {
        "publication_id": "pub-exec",
        "guild_id": 3,
        "message_id": 99,
        "channel_id": 2,
        "user_id": 4,
        "platform": "twitter",
        "action": "post",
        "source_kind": "admin_chat",
        "request_payload": {
            "message_id": 99,
            "channel_id": 2,
            "guild_id": 3,
            "user_id": 4,
            "platform": "twitter",
            "action": "post",
            "text": "scheduled",
            "source_kind": "admin_chat",
            "source_context": {"source_kind": "admin_chat", "metadata": {"user_details": {"username": "poster"}}},
        },
        "attempt_count": 1,
        "status": "queued",
    }
    db_handler.rows["pub-exec"]["integrity_version"] = service.SIGNATURE_VERSION
    db_handler.rows["pub-exec"]["integrity_signature"] = service._sign_publication_payload(db_handler.rows["pub-exec"])
    execute_result = await service.execute_publication("pub-exec")
    assert execute_result.success is True
    assert db_handler.rows["pub-exec"]["status"] == "succeeded"

    assert await service.delete_publication(post_result.publication_id) is True
    assert provider.delete_calls == ["pub-1"]
    assert db_handler.rows["pub-1"]["status"] == "cancelled"
    assert db_handler.deleted_shared_posts == [(1, "twitter", 3)]

    assert await service.delete_publication(retweet_result.publication_id) is False
    assert provider.delete_calls == ["pub-1"]


async def test_default_social_publish_service_registers_youtube_provider():
    service = SocialPublishService(FakeDB(), logger_instance=None)

    assert "youtube" in service.providers
    assert isinstance(service.providers["youtube"], YouTubeZapierProvider)


async def test_youtube_zapier_provider_posts_reachable_media_url(monkeypatch):
    FakeZapierSession.payloads = []
    monkeypatch.setenv("ZAPIER_YOUTUBE_URL", "https://hooks.zapier.test/youtube")
    monkeypatch.setattr(
        "src.features.sharing.providers.youtube_zapier_provider.aiohttp.ClientSession",
        FakeZapierSession,
    )

    provider = YouTubeZapierProvider()
    result = await provider.publish(build_youtube_request())

    assert result["provider_ref"] == "yt-123"
    assert result["provider_url"] == "https://www.youtube.com/watch?v=yt-123"
    assert FakeZapierSession.payloads == [
        {
            "url": "https://hooks.zapier.test/youtube",
            "json": {
                "platform": "youtube",
                "action": "post",
                "title": "Yaron Inger - Your Model Now",
                "description": "Custom YouTube description.",
                "media_url": "https://cdn.example.com/yaron.mp4",
                "media_urls": ["https://cdn.example.com/yaron.mp4"],
                "privacy_status": "unlisted",
                "tags": ["ADOS", "Banodoco"],
                "playlist_id": None,
                "made_for_kids": False,
                "message_id": 10,
                "channel_id": 20,
                "guild_id": 30,
                "user_id": 40,
                "route_key": "youtube-main",
                "source_kind": "admin_chat",
                "source_metadata": {"youtube_description": "Custom YouTube description."},
            },
        }
    ]


async def test_publish_and_enqueue_resolve_and_persist_route_selection():
    db_handler = FakeDB()
    db_handler.server_config = FakeServerConfig(
        route={
            "id": "route-default",
            "channel_id": None,
            "platform": "twitter",
            "route_config": {"account": "main"},
        }
    )
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    immediate_result = await service.publish_now(build_request())
    assert immediate_result.success is True
    assert db_handler.rows[immediate_result.publication_id]["route_key"] == "route-default"
    assert db_handler.rows[immediate_result.publication_id]["request_payload"]["route_override"] == {
        "id": "route-default",
        "channel_id": None,
        "platform": "twitter",
        "route_config": {"account": "main"},
        "route_key": "route-default",
    }

    scheduled_at = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    queued_request = build_request(scheduled_at=scheduled_at)
    queued_request.route_override = "manual-route"
    queued_result = await service.enqueue(queued_request)
    assert queued_result.success is True
    assert db_handler.rows[queued_result.publication_id]["route_key"] == "manual-route"
    assert db_handler.rows[queued_result.publication_id]["request_payload"]["route_override"] == {
        "route_key": "manual-route"
    }
    assert db_handler.server_config.resolve_calls == [(3, 2, "twitter")]


async def test_publish_now_rejects_disabled_or_unrouted_channels():
    disabled_db_handler = FakeDB()
    disabled_db_handler.server_config = FakeServerConfig(enabled=False)
    disabled_service = SocialPublishService(
        disabled_db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    disabled_result = await disabled_service.publish_now(build_request())
    assert disabled_result.success is False
    assert disabled_result.error == "Sharing is not enabled for this channel."
    assert disabled_db_handler.rows == {}

    unrouted_db_handler = FakeDB()
    unrouted_db_handler.server_config = FakeServerConfig(enabled=True, route=None)
    unrouted_service = SocialPublishService(
        unrouted_db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    unrouted_result = await unrouted_service.enqueue(build_request())
    assert unrouted_result.success is False
    assert unrouted_result.error == "No social route is configured for this channel and platform."
    assert unrouted_db_handler.rows == {}


async def test_publish_failures_mark_canonical_rows_failed():
    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FailingProvider()},
        logger_instance=None,
    )

    result = await service.publish_now(build_request())
    assert result.success is False
    assert result.publication_id == "pub-1"
    assert db_handler.rows["pub-1"]["status"] == "failed"
    assert db_handler.rows["pub-1"]["last_error"] == "provider boom"


async def test_execute_publication_rejects_tampered_row():
    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    publication = service._build_publication_record(
        build_request(scheduled_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)),
        status="queued",
    )
    publication["publication_id"] = "pub-bad"
    publication["request_payload"]["text"] = "tampered after signing"
    db_handler.rows["pub-bad"] = publication

    result = await service.execute_publication("pub-bad")

    assert result.success is False
    assert result.error == "Invalid publication signature"
    assert db_handler.rows["pub-bad"]["status"] == "failed"
    assert db_handler.rows["pub-bad"]["last_error"] == "Invalid publication signature"


class FakePaymentProvider:
    def __init__(
        self,
        send_result=None,
        confirm_result="confirmed",
        check_status_result="confirmed",
        price=150.0,
        price_error=None,
    ):
        self.send_result = send_result or SendResult(signature="sig-1", phase="submitted", error=None)
        self.confirm_result = confirm_result
        self.check_status_result = check_status_result
        self.price = price
        self.price_error = price_error
        self.send_calls = []
        self.confirm_calls = []
        self.status_calls = []
        self.price_calls = 0

    async def send(self, recipient, amount_token):
        self.send_calls.append((recipient, amount_token))
        return self.send_result

    async def confirm_tx(self, tx_signature):
        self.confirm_calls.append(tx_signature)
        return self.confirm_result

    async def check_status(self, tx_signature):
        self.status_calls.append(tx_signature)
        return self.check_status_result

    async def get_token_price_usd(self):
        self.price_calls += 1
        if self.price_error is not None:
            raise self.price_error
        return self.price

    def token_name(self):
        return "SOL"


class FakeSolanaRpcClient:
    def __init__(self, *, confirm_side_effect=None, check_side_effect=None, check_result="confirmed"):
        self.confirm_tx = AsyncMock(side_effect=confirm_side_effect)
        self.check_tx_status = AsyncMock(side_effect=check_side_effect, return_value=check_result)


class FakePaymentDB:
    def __init__(self):
        self.rows = {}
        self.wallets = {}
        self.wallet_registry = {}
        self.transitions = []
        self.active_payment_or_intent_users = set()
        self.rolling_24h_usd = {}
        self.rolling_24h_calls = []

    def get_payment_requests_by_producer(self, guild_id, producer, producer_ref, is_test=None):
        rows = [
            row for row in self.rows.values()
            if row.get("guild_id") == guild_id
            and row.get("producer") == producer
            and row.get("producer_ref") == producer_ref
            and (is_test is None or row.get("is_test") == is_test)
        ]
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def create_payment_request(self, record, guild_id=None):
        payment_id = "pay-{0}".format(len(self.rows) + 1)
        row = dict(record)
        row["payment_id"] = payment_id
        row["guild_id"] = guild_id or row.get("guild_id")
        self.rows[payment_id] = row
        return row

    def get_payment_request(self, payment_id, guild_id=None):
        row = self.rows.get(payment_id)
        if row and guild_id is not None and row.get("guild_id") != guild_id:
            return None
        return row

    def mark_payment_confirmed_by_user(self, payment_id, guild_id=None, confirmed_by_user_id=None, confirmed_by="user", scheduled_at=None):
        row = self.rows[payment_id]
        row.update(
            {
                "status": "queued",
                "confirmed_by": confirmed_by,
                "confirmed_by_user_id": confirmed_by_user_id,
                "scheduled_at": scheduled_at,
            }
        )
        self.transitions.append(("confirmed_by_user", payment_id))
        return True

    def mark_payment_submitted(self, payment_id, tx_signature, amount_token=None, token_price_usd=None, send_phase="submitted", guild_id=None):
        row = self.rows[payment_id]
        row.update(
            {
                "status": "submitted",
                "tx_signature": tx_signature,
                "amount_token": amount_token,
                "token_price_usd": token_price_usd,
                "send_phase": send_phase,
            }
        )
        self.transitions.append(("submitted", payment_id, tx_signature))
        return True

    def mark_payment_confirmed(self, payment_id, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "confirmed"
        self.transitions.append(("confirmed", payment_id))
        return True

    def mark_payment_failed(self, payment_id, error, send_phase=None, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "failed"
        row["last_error"] = error
        row["send_phase"] = send_phase
        self.transitions.append(("failed", payment_id, send_phase))
        return True

    def mark_payment_manual_hold(self, payment_id, reason, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "manual_hold"
        row["last_error"] = reason
        self.transitions.append(("manual_hold", payment_id, reason))
        return True

    def requeue_payment(self, payment_id, retry_after=None, guild_id=None):
        row = self.rows[payment_id]
        row["status"] = "queued"
        row["retry_after"] = retry_after
        self.transitions.append(("requeue", payment_id))
        return True

    def get_inflight_payments_for_recovery(self, guild_ids=None):
        rows = [
            row for row in self.rows.values()
            if row.get("status") in {"processing", "submitted"}
            and (guild_ids is None or row.get("guild_id") in guild_ids)
        ]
        return list(rows)

    def get_pending_confirmation_payments(self, guild_ids=None):
        return [
            row for row in self.rows.values()
            if row.get("status") == "pending_confirmation"
            and (guild_ids is None or row.get("guild_id") in guild_ids)
        ]

    def get_wallet_by_id(self, wallet_id, guild_id=None):
        wallet = self.wallets.get(wallet_id)
        if wallet and guild_id is not None and wallet.get("guild_id") != guild_id:
            return None
        return wallet

    def has_active_payment_or_intent(self, guild_id, user_id):
        return (int(guild_id), int(user_id)) in self.active_payment_or_intent_users

    def get_rolling_24h_payout_usd(self, guild_id, provider):
        key = (int(guild_id), str(provider).strip().lower())
        self.rolling_24h_calls.append(key)
        return float(self.rolling_24h_usd.get(key, 0.0))

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
            "wallet_id": existing["wallet_id"] if existing else "wallet-user-{0}".format(len(self.wallet_registry) + 1),
            "guild_id": guild_id,
            "discord_user_id": discord_user_id,
            "chain": chain,
            "wallet_address": address,
            "verified_at": "verified",
            "metadata": metadata,
        }
        self.wallet_registry[key] = wallet
        self.wallets[wallet["wallet_id"]] = wallet
        return wallet

    def mark_wallet_verified(self, wallet_id, guild_id=None):
        wallet = self.wallets[wallet_id]
        wallet["verified_at"] = "verified"
        self.transitions.append(("wallet_verified", wallet_id))
        return True


async def test_payment_service_request_is_idempotent_and_test_amount_is_fixed():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
    }
    provider = FakePaymentProvider(price=200.0)
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    created = await service.request_payment(
        producer="grants",
        producer_ref="thread-1",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_native",
        is_test=True,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
        recipient_discord_id=99,
    )
    assert created["status"] == "pending_confirmation"
    assert created["amount_token"] == 0.002
    assert created["amount_usd"] is None
    assert created["token_price_usd"] is None

    duplicate = await service.request_payment(
        producer="grants",
        producer_ref="thread-1",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_native",
        is_test=True,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
    )
    assert duplicate["payment_id"] == created["payment_id"]
    assert len(db_handler.rows) == 1

    confirmed = service.confirm_payment(
        created["payment_id"],
        actor=PaymentActor(PaymentActorKind.AUTO, 99),
    )
    assert confirmed["status"] == "queued"
    assert confirmed["confirmed_by"] == "auto"


async def test_payment_service_confirm_payment_requires_expected_recipient():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-secure"] = {
        "payment_id": "pay-secure",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-secure",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": 123,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-secure",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 999),
    )
    accepted = service.confirm_payment(
        "pay-secure",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )

    assert rejected is None
    assert accepted["status"] == "queued"


async def test_confirm_rejects_null_recipient_discord_id():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-null"] = {
        "payment_id": "pay-null",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-null",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": None,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-null",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 123),
    )

    assert rejected is None
    assert db_handler.rows["pay-null"]["status"] == "pending_confirmation"


async def test_confirm_rejects_auto_actor_for_non_test_payment():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-auto"] = {
        "payment_id": "pay-auto",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-auto",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": 123,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-auto",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.AUTO, 123),
    )

    assert rejected is None
    assert db_handler.rows["pay-auto"]["status"] == "pending_confirmation"


async def test_confirm_rejects_mismatched_user():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-mismatch"] = {
        "payment_id": "pay-mismatch",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-mismatch",
        "recipient_wallet": "recipient-wallet",
        "recipient_discord_id": 123,
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_native": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    rejected = service.confirm_payment(
        "pay-mismatch",
        guild_id=3,
        actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, 999),
    )

    assert rejected is None
    assert db_handler.rows["pay-mismatch"]["status"] == "pending_confirmation"


async def test_request_payment_per_payment_cap_manual_holds():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
    }
    provider = FakePaymentProvider(price=200.0)
    breached = []

    async def on_cap_breach(payment):
        breached.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-cap-usd",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=600.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "per-payment cap exceeded: $600.00 > $500.00"
    assert breached == [created["payment_id"]]


async def test_request_payment_amount_token_path_cap_breach():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=150.0)
    breached = []

    async def on_cap_breach(payment):
        breached.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-cap-token",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=4.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "per-payment cap exceeded: $600.00 > $500.00"
    assert created["amount_usd"] == 600.0
    assert created["token_price_usd"] == 150.0
    assert created["request_payload"]["amount_usd"] == 600.0
    assert created["request_payload"]["token_price_usd"] == 150.0
    assert breached == [created["payment_id"]]


async def test_request_payment_amount_token_path_stamps_amount_usd():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=150.0)
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-stamp",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=1.5,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "pending_confirmation"
    assert created["amount_usd"] == 225.0
    assert created["token_price_usd"] == 150.0
    assert created["request_payload"]["amount_usd"] == 225.0
    assert created["request_payload"]["token_price_usd"] == 150.0
    assert provider.price_calls == 1


async def test_request_payment_amount_token_path_missing_price_holds():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=None)
    breached = []

    async def on_cap_breach(payment):
        breached.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-missing-price",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=1.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "cap check unavailable: token price missing"
    assert breached == [created["payment_id"]]


async def test_request_payment_amount_token_uncapped_provider_preserves_none():
    db_handler = FakePaymentDB()
    provider = FakePaymentProvider(price=150.0)
    service = PaymentService(
        db_handler,
        providers={"solana": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-uncapped",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana",
        is_test=False,
        amount_token=1.5,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["amount_usd"] is None
    assert created["token_price_usd"] is None
    assert provider.price_calls == 0


async def test_request_payment_rolling_daily_cap_manual_holds():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
    }
    db_handler.rolling_24h_usd[(3, "solana_payouts")] = 1900.0
    provider = FakePaymentProvider(price=200.0)
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-daily-usd",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        wallet_id="wallet-1",
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "rolling daily cap exceeded: $2050.00 > $2000.00"


async def test_request_payment_rolling_daily_cap_sees_derived_usd():
    db_handler = FakePaymentDB()
    db_handler.rolling_24h_usd[(3, "solana_payouts")] = 1900.0
    provider = FakePaymentProvider(price=150.0)
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": provider},
        test_payment_amount=0.002,
        logger_instance=None,
        per_payment_usd_cap=500,
        daily_usd_cap=2000,
        capped_providers=("solana_payouts",),
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-daily-token",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_token=1.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "rolling daily cap exceeded: $2050.00 > $2000.00"
    assert created["amount_usd"] == 150.0
    assert created["token_price_usd"] == 150.0


async def test_slot_reuse_collision_detected_after_failure():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-old"] = {
        "payment_id": "pay-old",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-collision",
        "recipient_wallet": "old-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "failed",
    }
    notifications = []

    async def on_cap_breach(payment):
        notifications.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-collision",
        guild_id=3,
        recipient_wallet="new-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["status"] == "manual_hold"
    assert created["last_error"] == "idempotency collision: prior wallet differs"
    assert len(db_handler.rows) == 2
    assert notifications == [created["payment_id"]]


async def test_slot_reuse_collision_blocked_when_prior_active():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-active"] = {
        "payment_id": "pay-active",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-active-collision",
        "recipient_wallet": "old-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    notifications = []

    async def on_cap_breach(payment):
        notifications.append(payment["payment_id"])

    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
        on_cap_breach=on_cap_breach,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-active-collision",
        guild_id=3,
        recipient_wallet="new-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created is None
    assert len(db_handler.rows) == 1
    assert notifications == ["pay-active"]


async def test_slot_reuse_same_wallet_creates_fresh_row_after_failure():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-old"] = {
        "payment_id": "pay-old",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-same-wallet",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "failed",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    created = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-same-wallet",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert created["payment_id"] != "pay-old"
    assert created["status"] == "pending_confirmation"
    assert len(db_handler.rows) == 2


async def test_idempotent_return_for_nonterminal():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-existing"] = {
        "payment_id": "pay-existing",
        "guild_id": 3,
        "producer": "admin_chat",
        "producer_ref": "intent-existing",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_payouts",
        "is_test": False,
        "amount_token": 1.0,
        "status": "pending_confirmation",
    }
    service = PaymentService(
        db_handler,
        providers={"solana_payouts": FakePaymentProvider()},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    duplicate = await service.request_payment(
        producer="admin_chat",
        producer_ref="intent-existing",
        guild_id=3,
        recipient_wallet="recipient-wallet",
        chain="solana",
        provider="solana_payouts",
        is_test=False,
        amount_usd=150.0,
        confirm_channel_id=10,
        notify_channel_id=10,
        recipient_discord_id=99,
    )

    assert duplicate["payment_id"] == "pay-existing"
    assert len(db_handler.rows) == 1


async def test_payment_service_execute_persists_submission_and_confirms_test_wallet():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
        "verified_at": None,
    }
    provider = FakePaymentProvider(
        send_result=SendResult(signature="sig-123", phase="submitted", error=None),
        confirm_result="confirmed",
    )
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.002,
        logger_instance=None,
    )
    db_handler.rows["pay-1"] = {
        "payment_id": "pay-1",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-1",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "wallet_id": "wallet-1",
        "is_test": True,
        "amount_token": 0.000001,
        "token_price_usd": None,
        "status": "processing",
    }

    result = await service.execute_payment("pay-1")

    assert result["status"] == "confirmed"
    assert db_handler.rows["pay-1"]["tx_signature"] == "sig-123"
    assert provider.confirm_calls == ["sig-123"]
    assert db_handler.transitions[:2] == [("submitted", "pay-1", "sig-123"), ("confirmed", "pay-1")]
    assert ("wallet_verified", "wallet-1") in db_handler.transitions


async def test_execute_payment_uses_stored_wallet():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "registry-wallet",
        "verified_at": "verified",
    }
    provider = FakePaymentProvider(
        send_result=SendResult(signature="sig-frozen", phase="submitted", error=None),
        confirm_result="confirmed",
    )
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.002,
        logger_instance=None,
    )
    db_handler.rows["pay-frozen"] = {
        "payment_id": "pay-frozen",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-frozen",
        "recipient_wallet": "frozen-wallet",
        "provider": "solana_native",
        "wallet_id": "wallet-1",
        "is_test": False,
        "amount_token": 0.75,
        "token_price_usd": 100.0,
        "status": "processing",
    }

    result = await service.execute_payment("pay-frozen")

    assert result["status"] == "confirmed"
    assert provider.send_calls == [("frozen-wallet", 0.75)]


async def test_payment_service_execute_fail_closed_on_ambiguous_and_timeout():
    ambiguous_db = FakePaymentDB()
    ambiguous_db.rows["pay-ambiguous"] = {
        "payment_id": "pay-ambiguous",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-2",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 1.25,
        "token_price_usd": 100.0,
        "status": "processing",
    }
    ambiguous_service = PaymentService(
        ambiguous_db,
        providers={
            "solana_native": FakePaymentProvider(
                send_result=SendResult(signature=None, phase="ambiguous", error="rpc timeout")
            )
        },
        test_payment_amount=0.002,
        logger_instance=None,
    )

    ambiguous_result = await ambiguous_service.execute_payment("pay-ambiguous")
    assert ambiguous_result["status"] == "manual_hold"
    assert "Ambiguous send error" in ambiguous_result["last_error"]

    timeout_db = FakePaymentDB()
    timeout_db.rows["pay-timeout"] = {
        "payment_id": "pay-timeout",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-3",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 2.0,
        "token_price_usd": 100.0,
        "status": "processing",
    }
    timeout_service = PaymentService(
        timeout_db,
        providers={
            "solana_native": FakePaymentProvider(
                send_result=SendResult(signature="sig-timeout", phase="submitted", error=None),
                confirm_result="timeout",
            )
        },
        test_payment_amount=0.002,
        logger_instance=None,
    )

    timeout_result = await timeout_service.execute_payment("pay-timeout")
    assert timeout_result["status"] == "manual_hold"
    assert timeout_db.rows["pay-timeout"]["tx_signature"] == "sig-timeout"
    assert timeout_result["last_error"] == "Confirmation timed out after submission"


async def test_payment_service_execute_payment_distinguishes_rpc_unreachable():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-rpc"] = {
        "payment_id": "pay-rpc",
        "guild_id": 3,
        "producer": "grants",
        "producer_ref": "thread-rpc",
        "recipient_wallet": "recipient-wallet",
        "provider": "solana_native",
        "is_test": False,
        "amount_token": 2.0,
        "token_price_usd": 100.0,
        "status": "processing",
    }
    service = PaymentService(
        db_handler,
        providers={
            "solana_native": FakePaymentProvider(
                send_result=SendResult(signature="sig-rpc", phase="submitted", error=None),
                confirm_result="rpc_unreachable",
            )
        },
        test_payment_amount=0.002,
        logger_instance=None,
    )

    result = await service.execute_payment("pay-rpc")

    assert result["status"] == "manual_hold"
    assert result["last_error"] == "rpc_unreachable: confirmation RPC offline"


async def test_payment_service_recover_inflight_requeues_or_holds_safely():
    db_handler = FakePaymentDB()
    db_handler.wallets["wallet-1"] = {
        "wallet_id": "wallet-1",
        "guild_id": 3,
        "wallet_address": "recipient-wallet",
        "verified_at": None,
    }
    db_handler.rows["pay-processing"] = {
        "payment_id": "pay-processing",
        "guild_id": 3,
        "status": "processing",
        "provider": "solana_native",
        "is_test": False,
    }
    db_handler.rows["pay-submitted"] = {
        "payment_id": "pay-submitted",
        "guild_id": 3,
        "status": "submitted",
        "provider": "solana_native",
        "tx_signature": "sig-recovery",
        "wallet_id": "wallet-1",
        "is_test": True,
    }
    db_handler.rows["pay-unknown"] = {
        "payment_id": "pay-unknown",
        "guild_id": 3,
        "status": "submitted",
        "provider": "unknown",
        "tx_signature": "sig-unknown",
        "is_test": False,
    }
    provider = FakePaymentProvider(check_status_result="confirmed")
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    recovered = await service.recover_inflight(guild_ids=[3])

    by_id = {row["payment_id"]: row for row in recovered}
    assert by_id["pay-processing"]["status"] == "queued"
    assert by_id["pay-submitted"]["status"] == "confirmed"
    assert by_id["pay-unknown"]["status"] == "manual_hold"
    assert provider.status_calls == ["sig-recovery"]


async def test_payment_service_recover_inflight_marks_rpc_unreachable_hold():
    db_handler = FakePaymentDB()
    db_handler.rows["pay-rpc"] = {
        "payment_id": "pay-rpc",
        "guild_id": 3,
        "status": "submitted",
        "provider": "solana_native",
        "tx_signature": "sig-rpc",
        "is_test": False,
    }
    provider = FakePaymentProvider(check_status_result="rpc_unreachable")
    service = PaymentService(
        db_handler,
        providers={"solana_native": provider},
        test_payment_amount=0.002,
        logger_instance=None,
    )

    recovered = await service.recover_inflight(guild_ids=[3])

    assert recovered[0]["status"] == "manual_hold"
    assert recovered[0]["last_error"] == "rpc_unreachable: confirmation RPC offline"
    assert provider.status_calls == ["sig-rpc"]


async def test_solana_provider_check_status_returns_rpc_unreachable_on_connection_error():
    provider = SolanaProvider(
        solana_client=FakeSolanaRpcClient(
            check_side_effect=aiohttp.ClientConnectionError("rpc down"),
        )
    )

    status = await provider.check_status("sig-rpc")

    assert status == "rpc_unreachable"


async def test_solana_provider_confirm_tx_preserves_confirmation_timeout():
    provider = SolanaProvider(
        solana_client=FakeSolanaRpcClient(
            confirm_side_effect=asyncio.TimeoutError(),
            check_result="not_found",
        ),
        confirm_timeout_seconds=0.01,
    )

    status = await provider.confirm_tx("sig-timeout")

    assert status == "timeout"


async def test_solana_provider_confirm_tx_returns_rpc_unreachable_on_lookup_outage():
    provider = SolanaProvider(
        solana_client=FakeSolanaRpcClient(
            confirm_side_effect=asyncio.TimeoutError(),
            check_side_effect=aiohttp.ClientConnectionError("rpc down"),
        ),
        confirm_timeout_seconds=0.01,
    )

    status = await provider.confirm_tx("sig-rpc")

    assert status == "rpc_unreachable"


# ═══════════════════════════════════════════════════════════════════
# Sprint 3 — Provider chain & media outcome tracking (T12)
# ═══════════════════════════════════════════════════════════════════


async def test_publish_now_records_media_ids():
    """publish_now writes media_ids from the provider result into SocialPublishResult."""
    db_handler = FakeDB()
    provider = FakeProvider(media_ids=["media-aaa", "media-bbb"])
    service = SocialPublishService(
        db_handler,
        providers={"twitter": provider},
        logger_instance=None,
    )

    result = await service.publish_now(build_request())

    assert result.success is True
    assert result.media_ids == ["media-aaa", "media-bbb"]
    assert len(provider.publish_calls) == 1


async def test_publish_now_media_ids_none_for_text_only():
    """When text_only is True, the provider returns no media_ids and
    SocialPublishResult.media_ids is an empty list."""
    db_handler = FakeDB()
    # Provider that does NOT set media_ids (the default FakeProvider omits the key)
    provider = FakeProvider()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": provider},
        logger_instance=None,
    )

    req = build_request()
    req.text_only = True
    result = await service.publish_now(req)

    assert result.success is True
    # Default FakeProvider omits media_ids → provider_result.get('media_ids') returns None
    # → media_ids=provider_result.get('media_ids') or [] yields []
    assert result.media_ids == []


async def test_publish_now_media_ids_none_for_youtube(monkeypatch):
    """YouTube provider does not return media_ids keys, so media_ids is empty."""
    FakeZapierSession.payloads = []
    monkeypatch.setenv("ZAPIER_YOUTUBE_URL", "https://hooks.zapier.test/youtube")
    monkeypatch.setattr(
        "src.features.sharing.providers.youtube_zapier_provider.aiohttp.ClientSession",
        FakeZapierSession,
    )

    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"youtube": YouTubeZapierProvider()},
        logger_instance=None,
    )

    result = await service.publish_now(build_youtube_request())

    assert result.success is True
    assert result.provider_ref == "yt-123"
    # YouTube provider doesn't return media_ids → empty
    assert result.media_ids == []


async def test_reply_thread_sequential_publish():
    """Publishing a root post followed by a reply chains through target_post_ref."""
    db_handler = FakeDB()

    # Use a tracking provider that records returned provider_refs
    class TrackingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self._counter = 0

        async def publish(self, request):
            result = await super().publish(request)
            self._counter += 1
            result["provider_ref"] = f"tweet-{self._counter}"
            result["tweet_id"] = f"tweet-{self._counter}"
            result["provider_url"] = f"https://x.com/example/status/tweet-{self._counter}"
            return result

    provider = TrackingProvider()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": provider},
        logger_instance=None,
    )

    # 1) publish root post
    root = await service.publish_now(build_request(action="post"))
    assert root.success is True
    root_ref = root.provider_ref
    assert root_ref == "tweet-1"

    # 2) publish reply chained to root
    reply = await service.publish_now(
        build_request(action="reply", target_post_ref=root_ref)
    )
    assert reply.success is True
    assert reply.provider_ref == "tweet-2"
    assert provider.publish_calls[1].target_post_ref == root_ref

    # 3) another reply chained to the first reply
    reply2 = await service.publish_now(
        build_request(action="reply", target_post_ref=reply.provider_ref)
    )
    assert reply2.success is True
    assert reply2.provider_ref == "tweet-3"
    assert provider.publish_calls[2].target_post_ref == reply.provider_ref


async def test_reply_not_supported_on_youtube():
    """publish_now rejects reply action on non-X/Twitter platforms."""
    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    result = await service.publish_now(
        SocialPublishRequest(
            message_id=99,
            channel_id=2,
            guild_id=3,
            user_id=4,
            platform="youtube",
            action="reply",
            target_post_ref="some-video-id",
            text="Replying to a YouTube video",
            source_kind="admin_chat",
            source_context=PublicationSourceContext(
                source_kind="admin_chat",
                metadata={"user_details": {"username": "poster"}},
            ),
        )
    )

    assert result.success is False
    assert "Reply/quote not supported on this platform" in result.error
    assert result.publication_id is None


async def test_find_existing_posts():
    """find_existing_posts returns matches filtered by topic_id and platform,
    with optional content-similarity annotations."""
    db_handler = FakeDB()
    service = SocialPublishService(
        db_handler,
        providers={"twitter": FakeProvider()},
        logger_instance=None,
    )

    # Seed some publications
    for i in range(3):
        req = build_request()
        req.source_context = PublicationSourceContext(
            source_kind="live_update_social",
            metadata={
                "topic_id": f"topic-{i}",
                "user_details": {"username": "poster"},
            },
        )
        req.text = f"Content for topic {i}"
        await service.publish_now(req)

    # Also publish a non-live_update_social one (should not match find_existing_posts
    # since the real DB filters on source_kind, but FakeDB does not — we only care
    # about topic_id filtering)
    req_extra = build_request()
    req_extra.source_context = PublicationSourceContext(
        source_kind="admin_chat",
        metadata={"topic_id": "topic-0"},
    )
    req_extra.text = "Admin chat content"
    await service.publish_now(req_extra)

    # Search for topic-0
    matches = await service.find_existing_posts(
        topic_id="topic-0",
        platform="twitter",
    )
    # Both the live_update_social row and admin_chat row have topic_id=topic-0
    assert len(matches) >= 1
    # At least one match has topic-0 in its metadata
    topic_texts = [
        m.get("request_payload", {}).get("text", "")
        for m in matches
    ]
    assert any("topic 0" in t for t in topic_texts)

    # With draft_text, similarity score should be present
    matches_with_sim = await service.find_existing_posts(
        topic_id="topic-0",
        platform="twitter",
        draft_text="Content for topic 0",
    )
    assert len(matches_with_sim) >= 1
    for m in matches_with_sim:
        assert "_similarity" in m
        assert isinstance(m["_similarity"], float)
        if "Content for topic 0" in (m.get("text") or ""):
            assert m["_similarity"] == 1.0  # exact match

    # Non-existent topic returns empty
    matches_none = await service.find_existing_posts(
        topic_id="no-such-topic",
        platform="twitter",
    )
    assert matches_none == []
