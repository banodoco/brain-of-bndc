import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.admin_chat.tools import execute_share_to_social, execute_reply_to_tweet
from src.features.sharing.models import PublicationSourceContext, SocialPublishRequest
from src.features.sharing.providers.x_provider import XProvider
from src.features.sharing.sharer import Sharer

from conftest import load_module_from_repo


pytestmark = pytest.mark.anyio


class FakeService:
    def __init__(self):
        self.requests = []
        self.mode = None

    async def publish_now(self, request):
        self.mode = "publish_now"
        self.requests.append(request)
        return SimpleNamespace(
            success=True,
            tweet_url="https://x.com/test/status/1",
            tweet_id="1",
            publication_id="pub-1",
            error=None,
        )

    async def enqueue(self, request):
        self.mode = "enqueue"
        self.requests.append(request)
        return SimpleNamespace(success=True, publication_id="pub-queued", error=None)


class FakeDB:
    def __init__(self):
        self.member_updates = []
        self.first_shared = []

    def get_member(self, member_id):
        return {"member_id": member_id, "username": "poster", "twitter_url": None}

    def mark_member_first_shared(self, member_id, guild_id=None):
        self.first_shared.append((member_id, guild_id))
        return False

    def get_social_publications_for_message(self, **_kwargs):
        return []

    def list_social_publications(self, **kwargs):
        self._last_list_kwargs = kwargs
        return list(getattr(self, "_publications", []))

    def create_or_update_member(self, **kwargs):
        self.member_updates.append(kwargs)
        return True


class FakeSharer:
    def __init__(self):
        self.social_publish_service = FakeService()
        self.db_handler = FakeDB()
        self.cleaned = []
        self.announced = []

    def _find_existing_publication(self, **_kwargs):
        return None

    async def _download_attachment(self, attachment):
        return {"local_path": "/tmp/{0}".format(attachment.id), "media_type": "image"}

    async def _download_media_from_url(self, url, kind, index):
        return {"local_path": f"/tmp/{kind}_{index}.mp4", "url": url, "filename": f"{kind}_{index}.mp4"}

    def _cleanup_files(self, files):
        self.cleaned.extend(files)

    async def _announce_tweet_url(self, *args, **kwargs):
        self.announced.append((args, kwargs))


class FakeAuthor:
    def __init__(self, member_id=123, display_name="Poster", is_bot=False):
        self.id = member_id
        self.display_name = display_name
        self.name = display_name
        self.bot = is_bot
        self.global_name = display_name
        self.nick = None


class FakeAttachment:
    def __init__(self, attachment_id=1, url="https://cdn.example/image.png"):
        self.id = attachment_id
        self.url = url
        self.filename = "image.png"
        self.content_type = "image/png"
        self.size = 12


class FakeGuild:
    def __init__(self, guild_id=456):
        self.id = guild_id

    def get_thread(self, _message_id):
        return None


class FakeMessage:
    def __init__(self):
        self.id = 789
        self.author = FakeAuthor()
        self.guild = FakeGuild()
        self.content = "hello from discord"
        self.jump_url = "https://discord.com/channels/456/222/789"
        self.attachments = [FakeAttachment()]
        self.channel = SimpleNamespace(id=222)


class FakeChannel:
    def __init__(self, message):
        self.id = 222
        self.guild = message.guild
        self._message = message

    async def fetch_message(self, _message_id):
        return self._message


class FakeBot:
    def __init__(self, channel):
        self._channel = channel
        self.social_publish_service = None
        self.rate_limiter = None

    def get_channel(self, _channel_id):
        return self._channel

    async def fetch_channel(self, _channel_id):
        return self._channel


class FakeSummaryChannel:
    def __init__(self):
        self.id = 555
        self.name = "daily-summary"
        self.topic = "summary topic"


class FakeLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class FakeLLMClient:
    async def generate_chat_completion(self, **_kwargs):
        return "yes|looks good"


class FakeReactorDM:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, embed=None):
        self.messages.append({"content": content, "embed": embed})


class FakeInteraction:
    def __init__(self):
        self.followup = SimpleNamespace(messages=[])

        async def send(message, ephemeral=False):
            self.followup.messages.append((message, ephemeral))

        self.followup.send = send


async def test_admin_chat_reply_defaults_text_only_and_reaches_publish_service():
    sharer = FakeSharer()
    message = FakeMessage()
    bot = FakeBot(FakeChannel(message))

    result = await execute_share_to_social(
        bot,
        sharer,
        {
            "message_link": message.jump_url,
            "action": "reply",
            "target_post": "123456",
        },
    )

    assert result["success"] is True
    assert sharer.social_publish_service.mode == "publish_now"
    assert sharer.social_publish_service.requests[0].action == "reply"
    assert sharer.social_publish_service.requests[0].text_only is True


async def test_admin_chat_schedule_can_pass_explicit_route_override():
    sharer = FakeSharer()
    message = FakeMessage()
    bot = FakeBot(FakeChannel(message))

    result = await execute_share_to_social(
        bot,
        sharer,
        {
            "message_link": message.jump_url,
            "schedule_for": "2026-04-10T12:00:00Z",
            "route_key": "route-manual",
        },
    )

    assert result["success"] is True
    assert result["status"] == "queued"
    assert sharer.social_publish_service.mode == "enqueue"
    assert sharer.social_publish_service.requests[0].route_override == {
        "route_key": "route-manual"
    }


async def test_summary_finalize_sharing_reaches_publish_service(monkeypatch):
    fake_db = FakeDB()
    fake_service = FakeService()
    fake_bot = SimpleNamespace(social_publish_service=fake_service)
    sharer = Sharer(bot=fake_bot, db_handler=fake_db, logger_instance=logging.getLogger("test"))
    sharer.social_publish_service = fake_service

    fake_message = FakeMessage()
    fake_message.attachments = []

    async def fake_fetch_message(channel_id, message_id):
        assert channel_id == 222
        assert message_id == 789
        return fake_message

    async def fake_announce(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sharer, "_fetch_message", fake_fetch_message)
    monkeypatch.setattr(sharer, "_announce_tweet_url", fake_announce)

    result = await sharer.finalize_sharing(
        user_id=fake_message.author.id,
        message_id=fake_message.id,
        channel_id=222,
        summary_channel=FakeSummaryChannel(),
        source_kind="summary",
    )

    assert result["success"] is True
    assert fake_service.requests[0].source_kind == "summary"
    assert fake_service.requests[0].action == "post"


async def test_reaction_bridge_consent_path_preserves_side_effects_and_uses_publish_service():
    module = load_module_from_repo(
        "src/features/reacting/subfeatures/tweet_sharer_bridge.py",
        "tests_tweet_sharer_bridge",
    )

    sharer = FakeSharer()
    logger = FakeLogger()
    interaction = FakeInteraction()
    reactor_dm = FakeReactorDM()
    message = FakeMessage()

    async def fake_download_media(url, message_id, item_index):
        return {"local_path": "/tmp/{0}-{1}.png".format(message_id, item_index), "url": url}

    sharer._download_media_from_url = fake_download_media

    await module._process_moderation_and_sharing(
        bot_instance=SimpleNamespace(rate_limiter=None),
        logger=logger,
        db_handler=sharer.db_handler,
        sharer_instance=sharer,
        llm_client=FakeLLMClient(),
        message_to_share=message,
        original_poster=message.author,
        reactor=FakeAuthor(member_id=999, display_name="Reactor"),
        reactor_comment="Love this one",
        moderation_model_name="claude-test",
        path_type="Consent Path",
        interaction=interaction,
        reactor_dm_channel_override=reactor_dm,
    )

    request = sharer.social_publish_service.requests[0]
    assert isinstance(request, SocialPublishRequest)
    assert request.source_kind == "reaction_bridge"
    assert request.duplicate_policy["check_existing"] is False
    assert sharer.db_handler.member_updates[0]["allow_content_sharing"] is True
    assert interaction.followup.messages == [("Thanks! Your content is being shared.", True)]
    assert "has been tweeted" in reactor_dm.messages[0]["content"]


async def test_admin_chat_quote_tweet_via_message_link():
    sharer = FakeSharer()
    message = FakeMessage()
    bot = FakeBot(FakeChannel(message))

    result = await execute_share_to_social(
        bot,
        sharer,
        {
            "message_link": message.jump_url,
            "action": "quote",
            "target_post": "https://x.com/banodoco/status/2041974635487494386",
            "tweet_text": "Check out this amazing work!",
        },
    )

    assert result["success"] is True
    req = sharer.social_publish_service.requests[0]
    assert req.action == "quote"
    assert req.target_post_ref == "2041974635487494386"
    assert "Quote tweeted" in result["message"]


async def test_admin_chat_quote_tweet_direct_post_with_media_url():
    sharer = FakeSharer()

    async def fake_download_media(url, message_id, item_index):
        return {"local_path": f"/tmp/{message_id}-{item_index}.mp4", "url": url}

    sharer._download_media_from_url = fake_download_media
    bot = FakeBot(FakeChannel(FakeMessage()))

    result = await execute_share_to_social(
        bot,
        sharer,
        {
            "action": "quote",
            "target_post": "https://x.com/banodoco/status/2041974635487494386",
            "tweet_text": "Featured artwork of the day",
            "media_urls": ["https://example.com/video.mp4"],
        },
        guild_id=456,
    )

    assert result["success"] is True
    req = sharer.social_publish_service.requests[0]
    assert req.action == "quote"
    assert req.target_post_ref == "2041974635487494386"
    assert len(req.media_hints) == 1
    assert "Quote tweeted" in result["message"]


async def test_quote_tweet_requires_target_post():
    sharer = FakeSharer()
    message = FakeMessage()
    bot = FakeBot(FakeChannel(message))

    result = await execute_share_to_social(
        bot,
        sharer,
        {
            "message_link": message.jump_url,
            "action": "quote",
            "tweet_text": "Great work!",
        },
    )

    assert result["success"] is False
    assert "requires target_post" in result["error"]


async def test_x_provider_passes_quote_tweet_id_to_post_tweet(monkeypatch):
    captured_kwargs = {}

    async def fake_post_tweet(**kwargs):
        captured_kwargs.update(kwargs)
        return {"url": "https://x.com/test/status/99", "id": "99"}

    monkeypatch.setattr(
        "src.features.sharing.providers.x_provider.post_tweet",
        fake_post_tweet,
    )

    provider = XProvider()
    request = SocialPublishRequest(
        message_id=1,
        channel_id=2,
        guild_id=3,
        user_id=4,
        platform="twitter",
        action="quote",
        target_post_ref="https://x.com/banodoco/status/2041974635487494386",
        text="Amazing work!",
        source_context=PublicationSourceContext(
            source_kind="admin_chat",
            metadata={"user_details": {"direct_post": True}},
        ),
    )

    result = await provider.publish(request)

    assert result is not None
    assert result["tweet_id"] == "99"
    assert captured_kwargs["quote_tweet_id"] == "2041974635487494386"
    assert captured_kwargs["in_reply_to_tweet_id"] is None


# ── reply_to_tweet: intuitive same-thread follow-ups ──────────────────


async def test_reply_to_tweet_explicit_url_reaches_publish_service_as_reply():
    sharer = FakeSharer()

    result = await execute_reply_to_tweet(
        bot=None,
        sharer=sharer,
        params={
            "text": "Join the community: https://discord.gg/example",
            "tweet": "https://x.com/banodoco/status/2089691650935140492",
        },
        guild_id=456,
    )

    assert result["success"] is True
    assert sharer.social_publish_service.mode == "publish_now"
    req = sharer.social_publish_service.requests[0]
    assert req.action == "reply"
    assert req.target_post_ref == "2089691650935140492"
    assert req.text == "Join the community: https://discord.gg/example"
    # replies default to text-only so the follow-up doesn't reattach parent media
    assert req.text_only is True
    assert "Replied to tweet 2089691650935140492" in result["message"]


async def test_reply_to_tweet_omitted_target_resolves_most_recent_publication():
    sharer = FakeSharer()
    sharer.db_handler._publications = [
        {"provider_ref": "2089691650935140492", "status": "succeeded", "platform": "twitter"},
    ]

    result = await execute_reply_to_tweet(
        bot=None,
        sharer=sharer,
        params={"text": "Workflow: https://discord.com/channels/1/2/3"},
        guild_id=456,
    )

    assert result["success"] is True
    req = sharer.social_publish_service.requests[0]
    assert req.action == "reply"
    assert req.target_post_ref == "2089691650935140492"
    assert sharer.db_handler._last_list_kwargs["platform"] == "twitter"
    assert sharer.db_handler._last_list_kwargs["status"] == "succeeded"
    assert sharer.db_handler._last_list_kwargs["guild_id"] == 456


async def test_reply_to_tweet_no_recent_publication_errors_with_guidance():
    sharer = FakeSharer()
    sharer.db_handler._publications = []

    result = await execute_reply_to_tweet(
        bot=None,
        sharer=sharer,
        params={"text": "hello"},
        guild_id=456,
    )

    assert result["success"] is False
    assert "No recent X post found" in result["error"]


async def test_reply_to_tweet_invalid_target_errors():
    sharer = FakeSharer()

    result = await execute_reply_to_tweet(
        bot=None,
        sharer=sharer,
        params={"text": "hello", "tweet": "not-a-tweet"},
        guild_id=456,
    )

    assert result["success"] is False
    assert "Tweet ID or a tweet URL" in result["error"]


async def test_reply_to_tweet_with_media_downloads_and_posts_not_text_only():
    sharer = FakeSharer()

    result = await execute_reply_to_tweet(
        bot=None,
        sharer=sharer,
        params={
            "text": "see the montage",
            "tweet": "123456",
            "media_urls": ["https://cdn.discordapp.com/attachments/1/2/montage.mp4"],
        },
        guild_id=456,
    )

    assert result["success"] is True
    req = sharer.social_publish_service.requests[0]
    assert req.action == "reply"
    assert req.target_post_ref == "123456"
    assert req.text_only is False
    assert len(req.media_hints) == 1
    # downloaded temp files are cleaned up after publishing
    assert sharer.cleaned == [req.media_hints[0]["local_path"]]
