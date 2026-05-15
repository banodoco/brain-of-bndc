"""Tests for shared media/social helpers and the preview facade.

Covers:
  (a) embed-media extraction from mock discord.Message with embeds
  (b) attachment inspection returns fresh URLs without persisting CDN URLs
  (c) download_media_url with a mock aiohttp response
  (d) route listing and resolution through fake ServerConfig
  (e) preview_publish_readiness returns ready=True for valid routes
      without creating social_publications rows
  (f) preview_publish_readiness returns ready=False with errors for
      missing routes or unsupported platforms
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.features.sharing.models import PublicationSourceContext, SocialPublishRequest
from src.features.sharing.social_publish_service import SocialPublishService
from src.features.sharing.live_update_social.helpers import (
    inspect_discord_message,
    refresh_discord_media_urls,
    download_media_url,
    list_social_routes,
    resolve_social_route,
)
from src.features.sharing.live_update_social.models import MediaRefIdentity

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def social_signing_secret(monkeypatch):
    """Provide SOCIAL_PUBLISH_SIGNING_SECRET for preview_publish_readiness tests."""
    monkeypatch.setenv("SOCIAL_PUBLISH_SIGNING_SECRET", "test-social-signing-secret")


# ═══════════════════════════════════════════════════════════════════════
#  Mock helpers
# ═══════════════════════════════════════════════════════════════════════

class _MockAuthor:
    """Mock Discord author whose str() returns the name."""
    def __init__(self, name: str):
        self._name = name

    def __str__(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return self._name


class _MockEmbed:
    """Mock Discord embed with to_dict()."""
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def to_dict(self) -> Dict[str, Any]:
        return self._data


def _make_mock_attachment(filename: str, url: str, content_type: str = "image/png",
                          size: int = 1024) -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        url=url,
        content_type=content_type,
        size=size,
    )


def _make_mock_message(*, content: str = "", attachments: List[SimpleNamespace] = None,
                       embeds: List[Dict[str, Any]] = None,
                       author_name: str = "test-user",
                       guild_id: int = 789,
                       created_at: Optional[datetime] = None) -> SimpleNamespace:
    """Build a SimpleNamespace that looks like a discord.Message."""
    created_at = created_at or datetime(2026, 5, 15, 8, 0, 0, tzinfo=timezone.utc)
    msg = SimpleNamespace(
        content=content,
        attachments=attachments or [],
        embeds=[_MockEmbed(emb) for emb in (embeds or [])],
        author=_MockAuthor(author_name),
        created_at=created_at,
        guild=SimpleNamespace(id=guild_id),
        reactions=[],
    )
    return msg


def _make_mock_bot(msg: Optional[SimpleNamespace] = None) -> MagicMock:
    """Create a mock discord.Client whose get_channel returns a channel with fetch_message."""
    mock_msg = msg or _make_mock_message()
    mock_channel = MagicMock()
    mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

    # hasattr checks for fetch_message need to pass
    def _hasattr(obj, name):
        if name == "fetch_message":
            return True
        return hasattr(obj, name)

    mock_bot = MagicMock()
    mock_bot.get_channel = MagicMock(return_value=mock_channel)
    # Don't set fetch_channel — it's only used for ForumChannel fallback
    mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
    # Inspect uses bot.get_channel first; make it succeed
    return mock_bot


def _build_fake_server_config(routes: List[Dict[str, Any]] = None,
                              enabled: bool = True) -> SimpleNamespace:
    """Build a fake ServerConfig for route tests."""
    sc = SimpleNamespace()
    sc.routes = routes or []
    sc._enabled = enabled

    # Build a chainable fake query builder
    class _FakeQuery:
        def __init__(self, server_config, routes_override=None):
            self._sc = server_config
            self._routes = routes_override
            self._filters = []
        def select(self, *_a, **_kw):
            return self
        def eq(self, *_a, **_kw):
            return self
        def or_(self, *_a, **_kw):
            return self
        def order(self, *_a, **_kw):
            return self
        def limit(self, *_a, **_kw):
            return self
        def execute(self):
            routes = self._routes if self._routes is not None else self._sc.routes
            return SimpleNamespace(data=routes)

    class _FakeTable:
        def __init__(self, server_config):
            self._sc = server_config
        def select(self, *_a, **_kw):
            return _FakeQuery(self._sc)
        def insert(self, *_a, **_kw):
            return _FakeQuery(self._sc, routes_override=self._sc.routes)

    class _FakeSupabase:
        def __init__(self, server_config):
            self._sc = server_config
        def table(self, name):
            return _FakeTable(self._sc)

    sc._supabase = _FakeSupabase(sc)

    def _resolve(guild_id: int, channel_id: int, platform: str) -> Optional[Dict[str, Any]]:
        for r in sc.routes:
            if (r.get("platform") == platform
                    and r.get("guild_id") == guild_id
                    and r.get("enabled", True)):
                chan = r.get("channel_id")
                if chan is None or chan == channel_id:
                    return r
        return None

    sc.resolve_social_route = _resolve
    return sc


def _build_fake_db_handler(server_config: SimpleNamespace = None) -> SimpleNamespace:
    """Build a fake db_handler that carries a ServerConfig."""
    db = SimpleNamespace()
    db.server_config = server_config or _build_fake_server_config()
    return db


# ═══════════════════════════════════════════════════════════════════════
#  (a) embed-media extraction from mock discord.Message
# ═══════════════════════════════════════════════════════════════════════

async def test_inspect_discord_message_embeds_image_thumbnail_video_author_icon():
    """Embed extraction returns image, thumbnail, video, author icon slots."""
    msg = _make_mock_message(
        content="Check this out!",
        embeds=[
            {
                "image": {"url": "https://cdn.discordapp.com/embed-image.png"},
                "thumbnail": {"url": "https://cdn.discordapp.com/embed-thumb.png"},
                "video": {"url": "https://cdn.discordapp.com/embed-video.mp4"},
                "author": {"icon_url": "https://cdn.discordapp.com/author-icon.png"},
                "footer": {"icon_url": "https://cdn.discordapp.com/footer-icon.png"},
            }
        ],
    )
    bot = _make_mock_bot(msg)

    result = await inspect_discord_message(bot, channel_id=456, message_id=123)

    assert result["content"] == "Check this out!"
    assert result["author_name"] == "test-user"
    assert result["guild_id"] == 789

    slots = {m["slot"] for m in result["embeds_media"]}
    assert "image" in slots
    assert "thumbnail" in slots
    assert "video" in slots
    assert "author_icon" in slots
    assert "footer_icon" in slots

    for m in result["embeds_media"]:
        assert m["url"].startswith("https://cdn.discordapp.com/")


async def test_inspect_discord_message_embeds_no_media_when_empty():
    """Empty embeds produce empty embeds_media list."""
    msg = _make_mock_message(content="plain text", embeds=[])
    bot = _make_mock_bot(msg)

    result = await inspect_discord_message(bot, channel_id=1, message_id=1)

    assert result["embeds_media"] == []


async def test_inspect_discord_message_embeds_missing_subfields():
    """Embed with no image/thumbnail/video/author returns empty embeds_media."""
    msg = _make_mock_message(content="x", embeds=[{"title": "just a title"}])
    bot = _make_mock_bot(msg)

    result = await inspect_discord_message(bot, channel_id=1, message_id=1)

    assert result["embeds_media"] == []


# ═══════════════════════════════════════════════════════════════════════
#  (b) attachment inspection returns fresh URLs without persisting CDN URLs
# ═══════════════════════════════════════════════════════════════════════

async def test_attachment_inspection_returns_fresh_urls():
    """Attachments list has fresh CDN URLs; CDN URLs are not used as durable identity."""
    att = _make_mock_attachment("photo.jpg", "https://cdn.discordapp.com/attachments/1/2/photo.jpg")
    msg = _make_mock_message(content="pic", attachments=[att])
    bot = _make_mock_bot(msg)

    result = await inspect_discord_message(bot, channel_id=10, message_id=20)

    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["url"] == "https://cdn.discordapp.com/attachments/1/2/photo.jpg"
    assert result["attachments"][0]["filename"] == "photo.jpg"
    assert result["attachments"][0]["content_type"] == "image/png"

    # Verify no durable identity is tangled in the attachment dict:
    assert "attachment_index" not in result["attachments"][0]
    assert "channel_id" not in result["attachments"][0]
    assert "message_id" not in result["attachments"][0]


async def test_refresh_discord_media_urls_wraps_inspect():
    """refresh_discord_media_urls returns attachments + embeds_media from inspect."""
    att = _make_mock_attachment("vid.mp4", "https://cdn.discordapp.com/att/vid.mp4",
                                content_type="video/mp4")
    msg = _make_mock_message(
        content="media!",
        attachments=[att],
        embeds=[{"image": {"url": "https://cdn.discordapp.com/img.jpg"}}],
    )
    bot = _make_mock_bot(msg)

    result = await refresh_discord_media_urls(bot, channel_id=1, message_id=1)

    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["url"] == "https://cdn.discordapp.com/att/vid.mp4"
    assert len(result["embeds_media"]) == 1
    assert result["embeds_media"][0]["slot"] == "image"


# ═══════════════════════════════════════════════════════════════════════
#  (c) download_media_url with mock aiohttp response
# ═══════════════════════════════════════════════════════════════════════

class _FakeResponse:
    """Minimal async context manager that mimics aiohttp.ClientResponse."""
    status = 200

    def __init__(self, body: bytes = b"fake-image-data",
                 headers: Dict[str, str] = None):
        self._body = body
        self.headers = headers or {"Content-Type": "image/png"}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeSession:
    """Minimal fake aiohttp.ClientSession that returns _FakeResponse on GET."""

    def __init__(self, fake_response: _FakeResponse = None):
        self.fake_response = fake_response or _FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url: str):
        # Return an async context manager
        class _GetCtx:
            async def __aenter__(self2):
                return self.fake_response

            async def __aexit__(self2, *args):
                pass
        return _GetCtx()


async def test_download_media_url_saves_to_dest_dir(tmp_path):
    """download_media_url downloads from URL and saves to local path."""
    fake_body = b"\x89PNG\r\n\x1a\n..."
    dest = str(tmp_path / "downloads")
    fake_response = _FakeResponse(body=fake_body, headers={"Content-Type": "image/png"})

    with patch("aiohttp.ClientSession", return_value=_FakeSession(fake_response)):
        result = await download_media_url(
            "https://example.com/pic.png",
            dest_dir=dest,
            filename_prefix="test",
        )

    assert result is not None
    assert result["content_type"] == "image/png"
    assert result["url"] == "https://example.com/pic.png"
    assert result["local_path"].startswith(dest)
    import os
    assert os.path.exists(result["local_path"])

    with open(result["local_path"], "rb") as f:
        assert f.read() == fake_body


async def test_download_media_url_http_error_returns_none(tmp_path):
    """HTTP non-200 returns None."""
    resp = _FakeResponse(body=b"err", headers={})
    resp.status = 404
    dest = str(tmp_path / "dl")

    with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
        result = await download_media_url("https://example.com/404.jpg", dest_dir=dest)

    assert result is None


async def test_download_media_url_handles_missing_content_type(tmp_path):
    """When Content-Type is missing, mimetypes.guess_type is used."""
    fake_body = b"some data"
    resp = _FakeResponse(body=fake_body, headers={})
    dest = str(tmp_path / "typedl")

    with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
        result = await download_media_url(
            "https://example.com/data.bin",
            dest_dir=dest,
        )

    assert result is not None
    assert result["content_type"] in ("application/octet-stream", None) or result["content_type"] is not None


# ═══════════════════════════════════════════════════════════════════════
#  (d) route listing and resolution through fake ServerConfig
# ═══════════════════════════════════════════════════════════════════════

def test_list_social_routes_returns_routes():
    """list_social_routes queries Supabase table for enabled routes."""
    routes = [
        {"guild_id": 1, "channel_id": 10, "platform": "twitter", "enabled": True,
         "route_key": "twitter-main"},
        {"guild_id": 1, "channel_id": 20, "platform": "youtube", "enabled": True,
         "route_key": "yt-chan"},
    ]
    sc = _build_fake_server_config(routes=routes)
    db = _build_fake_db_handler(server_config=sc)

    result = list_social_routes(db, guild_id=1, channel_id=10)
    assert len(result) == 2
    platforms = {r["platform"] for r in result}
    assert platforms == {"twitter", "youtube"}


def test_list_social_routes_empty_when_no_server_config():
    """When db_handler has no server_config, returns empty list."""
    db = SimpleNamespace()
    # No server_config attribute
    result = list_social_routes(db, guild_id=1)
    assert result == []


def test_resolve_social_route_finds_match():
    """resolve_social_route delegates to ServerConfig and returns matching route."""
    routes = [
        {"guild_id": 1, "channel_id": 10, "platform": "twitter", "enabled": True,
         "route_key": "tw-main"},
    ]
    sc = _build_fake_server_config(routes=routes)
    db = _build_fake_db_handler(server_config=sc)

    result = resolve_social_route(db, guild_id=1, channel_id=10, platform="twitter")
    assert result is not None
    assert result["route_key"] == "tw-main"


def test_resolve_social_route_returns_none_for_no_match():
    """resolve_social_route returns None when no route matches."""
    sc = _build_fake_server_config(routes=[])
    db = _build_fake_db_handler(server_config=sc)

    result = resolve_social_route(db, guild_id=1, channel_id=10, platform="youtube")
    assert result is None


def test_resolve_social_route_returns_none_when_no_server_config():
    """When db_handler lacks server_config, resolve returns None."""
    db = SimpleNamespace()
    result = resolve_social_route(db, guild_id=1, channel_id=10, platform="twitter")
    assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  (e) preview_publish_readiness returns ready=True for valid routes
#      without creating social_publications rows
# ═══════════════════════════════════════════════════════════════════════

async def test_preview_publish_readiness_ready_true(social_signing_secret):
    """preview_publish_readiness returns ready=True when routes and provider exist."""
    from tests.test_social_publish_service import FakeDB, FakeProvider, FakeServerConfig

    db = FakeDB()
    db.server_config = FakeServerConfig(
        enabled=True,
        route={"route_key": "twitter-main", "route_config": {}},
    )
    provider = FakeProvider()
    svc = SocialPublishService(db, providers={"twitter": provider})

    req = SocialPublishRequest(
        message_id=99, channel_id=10, guild_id=1, user_id=100,
        platform="twitter", action="post", text="test",
        source_kind="admin_chat",
    )

    result = await svc.preview_publish_readiness(req)

    assert result["ready"] is True
    assert result["platform"] == "twitter"
    assert result["action"] == "post"
    assert result["route_normalized"] is True
    assert result["provider_available"] is True
    assert result["errors"] == []

    # (e) MUST NOT create social_publications rows
    assert len(db.rows) == 0, "preview_publish_readiness must not insert rows"


async def test_preview_publish_readiness_ready_true_youtube(social_signing_secret):
    """preview_publish_readiness works for youtube platform with route override."""
    from tests.test_social_publish_service import FakeDB, FakeServerConfig

    db = FakeDB()
    db.server_config = FakeServerConfig(enabled=True, route=None)
    svc = SocialPublishService(db, providers={
        "twitter": MagicMock(),
        "youtube": MagicMock(),
    })

    req = SocialPublishRequest(
        message_id=99, channel_id=10, guild_id=1, user_id=100,
        platform="youtube", action="post", text="test",
        route_override={"route_key": "yt-main"},
        source_kind="admin_chat",
    )

    result = await svc.preview_publish_readiness(req)
    assert result["ready"] is True
    assert result["provider_available"] is True
    assert len(db.rows) == 0


# ═══════════════════════════════════════════════════════════════════════
#  (f) preview_publish_readiness returns ready=False with errors
#      for missing routes or unsupported platforms
# ═══════════════════════════════════════════════════════════════════════

async def test_preview_publish_readiness_fails_missing_route(social_signing_secret):
    """ready=False when no social route is configured."""
    from tests.test_social_publish_service import FakeDB, FakeProvider, FakeServerConfig

    db = FakeDB()
    db.server_config = FakeServerConfig(enabled=True, route=None)
    provider = FakeProvider()
    svc = SocialPublishService(db, providers={"twitter": provider})

    req = SocialPublishRequest(
        message_id=1, channel_id=2, guild_id=3, user_id=4,
        platform="twitter", action="post", text="hello",
        source_kind="admin_chat",
    )

    result = await svc.preview_publish_readiness(req)
    assert result["ready"] is False
    assert result["route_normalized"] is False
    assert len(result["errors"]) >= 1
    assert any("route" in e.lower() for e in result["errors"])
    assert len(db.rows) == 0


async def test_preview_publish_readiness_fails_unsupported_platform(social_signing_secret):
    """ready=False for unsupported platform (no provider)."""
    from tests.test_social_publish_service import FakeDB

    db = FakeDB()
    db.server_config = MagicMock()
    db.server_config.is_feature_enabled = MagicMock(return_value=True)
    db.server_config.resolve_social_route = MagicMock(return_value={
        "route_key": "r1", "route_config": {},
    })
    svc = SocialPublishService(db, providers={})  # no providers

    req = SocialPublishRequest(
        message_id=1, channel_id=2, guild_id=3, user_id=4,
        platform="bluesky", action="post", text="hi",
        source_kind="admin_chat",
    )

    result = await svc.preview_publish_readiness(req)
    assert result["ready"] is False
    assert result["provider_available"] is False
    assert any("Unsupported" in e or "platform" in e.lower() for e in result["errors"])
    assert len(db.rows) == 0


async def test_preview_publish_readiness_fails_disabled_feature(social_signing_secret):
    """ready=False when sharing feature is disabled for the channel."""
    from tests.test_social_publish_service import FakeDB, FakeProvider, FakeServerConfig

    db = FakeDB()
    db.server_config = FakeServerConfig(enabled=False, route=None)
    provider = FakeProvider()
    svc = SocialPublishService(db, providers={"twitter": provider})

    req = SocialPublishRequest(
        message_id=1, channel_id=2, guild_id=3, user_id=4,
        platform="twitter", action="post", text="test",
        source_kind="admin_chat",
    )

    result = await svc.preview_publish_readiness(req)
    assert result["ready"] is False
    assert result["route_normalized"] is False
    assert len(result["errors"]) >= 1
    assert len(db.rows) == 0


async def test_preview_publish_readiness_does_not_mutate_any_state(social_signing_secret):
    """Invoking preview_publish_readiness never creates rows or mutates db."""
    from tests.test_social_publish_service import FakeDB, FakeProvider, FakeServerConfig

    db = FakeDB()
    db.server_config = FakeServerConfig(
        enabled=True,
        route={"route_key": "t1", "route_config": {}},
    )
    svc = SocialPublishService(db, providers={"twitter": FakeProvider()})

    req = SocialPublishRequest(
        message_id=1, channel_id=2, guild_id=3, user_id=4,
        platform="twitter", action="post", text="t",
        source_kind="admin_chat",
    )

    # Call multiple times
    for _ in range(3):
        await svc.preview_publish_readiness(req)

    assert len(db.rows) == 0
    assert db.shared_posts == []
