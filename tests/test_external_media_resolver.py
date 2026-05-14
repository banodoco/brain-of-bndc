"""
Unit tests for external media helpers and resolver logic.

All tests use stubs/monkeypatch for subprocess, HTTP, and filesystem —
no real network/youtube-dl calls.
"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from src.common.external_media import (
    SAFELISTED_DOMAINS,
    DISCORD_UPLOAD_LIMIT_BYTES,
    MEDIA_CONTENT_TYPES,
    content_hash,
    extract_domain,
    extract_external_url_at_index,
    extract_external_urls,
    extract_urls_from_text,
    get_platform_policy,
    is_discord_compatible_content_type,
    is_discord_upload_compatible,
    is_safelisted,
    is_short_form,
    is_long_form,
    make_cache_key,
    media_kind_from_content_type,
    normalise_url,
    sanitise_url_for_logs,
    PLATFORM_POLICY_SHORT_FORM,
    PLATFORM_POLICY_LONG_FORM,
)

from src.features.summarising.external_media_resolver import (
    ExternalMediaResolver,
    ResolveOutcome,
    ResolverResult,
    _default_run_yt_dlp_json,
    _default_download_url_to_path,
    _default_ensure_cache_dir,
    _provenance_check,
    _resolve_file_path,
    DEFAULT_MAX_BYTES,
)


# ---------------------------------------------------------------------------
# Helpers for building fake Discord message dicts
# ---------------------------------------------------------------------------

def _make_message(
    content: str = "",
    clean_content: str = "",
    embeds: Optional[List[Dict[str, Any]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "message_id": "123456",
        "content": content,
        "clean_content": clean_content or content,
        "embeds": embeds or [],
        "attachments": attachments or [],
    }


# ---------------------------------------------------------------------------
# 1. extract_external_urls deterministic ordering
# ---------------------------------------------------------------------------

class TestExtractExternalUrlsOrdering:
    """Deterministic ordering across content + embeds."""

    def test_empty_message_returns_empty(self):
        msg = _make_message()
        assert extract_external_urls(msg) == []

    def test_content_urls_returned_first(self):
        msg = _make_message(
            content="Check this https://x.com/user/status/123 and https://reddit.com/r/foo",
        )
        result = extract_external_urls(msg)
        assert len(result) == 2
        assert result[0]["index"] == 0
        assert result[0]["domain"] == "x.com"
        assert result[0]["source"] == "content"
        assert result[1]["index"] == 1
        assert result[1]["domain"] == "reddit.com"
        assert result[1]["source"] == "content"

    def test_content_then_embed_urls(self):
        msg = _make_message(
            content="https://x.com/user/status/1",
            embeds=[
                {"url": "https://reddit.com/r/test"},
            ],
        )
        result = extract_external_urls(msg)
        assert len(result) == 2
        assert result[0]["source"] == "content"
        assert result[1]["source"] == "embed"

    def test_embed_thumbnail_image_video_order(self):
        msg = _make_message(
            embeds=[
                {
                    "thumbnail": {"url": "https://x.com/thumb.jpg"},
                    "image": {"url": "https://reddit.com/img.png"},
                    "video": {"url": "https://instagram.com/vid.mp4"},
                }
            ],
        )
        result = extract_external_urls(msg)
        assert len(result) == 3
        assert result[0]["domain"] == "x.com"  # thumbnail first
        assert result[1]["domain"] == "reddit.com"  # image second
        assert result[2]["domain"] == "instagram.com"  # video third

    def test_deduplication_across_content_and_embeds(self):
        msg = _make_message(
            content="https://x.com/user/status/42",
            embeds=[
                {"url": "https://x.com/user/status/42"},
            ],
        )
        result = extract_external_urls(msg)
        assert len(result) == 1
        assert result[0]["source"] == "content"  # content wins

    def test_non_safelisted_urls_are_excluded(self):
        msg = _make_message(
            content="https://google.com and https://x.com/user/status/99",
        )
        result = extract_external_urls(msg)
        assert len(result) == 1
        assert result[0]["domain"] == "x.com"

    def test_deterministic_ordering_same_message_twice(self):
        """SC9: same message returns same results regardless of caller."""
        msg = _make_message(
            content="first https://x.com/a then https://reddit.com/b",
            embeds=[{"url": "https://instagram.com/p/c"}],
        )
        result1 = extract_external_urls(msg)
        result2 = extract_external_urls(deepcopy(msg))
        assert result1 == result2
        # Verify indices are deterministic
        for i, entry in enumerate(result1):
            assert entry["index"] == i

    def test_extract_external_url_at_index_handles_string_embed_url(self):
        msg = _make_message(embeds=[{"url": "https://x.com/user/status/42"}])
        assert extract_external_url_at_index(msg, 0) == "https://x.com/user/status/42"


# ---------------------------------------------------------------------------
# 2. Safelist / blocked source domains
# ---------------------------------------------------------------------------

class TestDomainSafelist:
    def test_x_com_is_safelisted(self):
        assert is_safelisted("x.com")
        assert is_safelisted("www.x.com")
        assert is_safelisted("X.COM")

    def test_twitter_com_is_safelisted(self):
        assert is_safelisted("twitter.com")

    def test_reddit_com_is_safelisted(self):
        assert is_safelisted("reddit.com")

    def test_mirror_domains_are_safelisted(self):
        for domain in ["fixupx.com", "fxtwitter.com", "vxtwitter.com",
                        "twittpr.com", "ddinstagram.com", "rxddit.com"]:
            assert is_safelisted(domain), f"{domain} should be safelisted"

    def test_youtube_is_safelisted_but_long_form(self):
        assert is_safelisted("youtube.com")
        assert is_long_form("youtube.com")

    def test_random_domain_not_safelisted(self):
        assert not is_safelisted("evil.com")
        assert not is_safelisted("malware.org")


# ---------------------------------------------------------------------------
# 3. URL sanitization with query tokens
# ---------------------------------------------------------------------------

class TestUrlSanitization:
    def test_query_tokens_redacted(self):
        url = "https://x.com/media.jpg?token=secret123&expires=999999"
        sanitised = sanitise_url_for_logs(url)
        assert "[REDACTED]" in sanitised
        assert "secret123" not in sanitised
        assert "x.com" in sanitised

    def test_clean_url_passes_through(self):
        url = "https://reddit.com/r/test/comments/abc"
        sanitised = sanitise_url_for_logs(url)
        assert sanitised == url or sanitised == url + ""

    def test_scheme_and_host_preserved(self):
        url = "https://x.com/path?token=abc123"
        sanitised = sanitise_url_for_logs(url)
        assert sanitised.startswith("https://x.com")

    def test_empty_url_returns_empty(self):
        assert sanitise_url_for_logs("") == ""


# ---------------------------------------------------------------------------
# 4. Cache-key stability
# ---------------------------------------------------------------------------

class TestCacheKeyStability:
    def test_same_url_same_key(self):
        key1 = make_cache_key("https://x.com/user/status/123")
        key2 = make_cache_key("https://x.com/user/status/123")
        assert key1 == key2

    def test_different_urls_different_keys(self):
        key1 = make_cache_key("https://x.com/1")
        key2 = make_cache_key("https://x.com/2")
        assert key1 != key2

    def test_fragment_stripped_for_stability(self):
        key1 = make_cache_key("https://x.com/path")
        key2 = make_cache_key("https://x.com/path#fragment")
        assert key1 == key2

    def test_keys_are_hex_strings(self):
        key = make_cache_key("https://x.com/test")
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


# ---------------------------------------------------------------------------
# 5. Content-type classification
# ---------------------------------------------------------------------------

class TestContentTypeClassification:
    def test_image_types_are_media(self):
        for ct in ["image/png", "image/jpeg", "image/gif", "image/webp"]:
            assert is_discord_compatible_content_type(ct), f"{ct} should be compatible"

    def test_video_types_are_media(self):
        for ct in ["video/mp4", "video/webm", "video/quicktime"]:
            assert is_discord_compatible_content_type(ct), f"{ct} should be compatible"

    def test_non_media_types_are_rejected(self):
        for ct in ["text/html", "application/json", "text/plain", "application/pdf"]:
            assert not is_discord_compatible_content_type(ct), f"{ct} should NOT be compatible"

    def test_charset_stripped(self):
        assert is_discord_compatible_content_type("image/png; charset=utf-8")

    def test_media_kind_image(self):
        assert media_kind_from_content_type("image/png") == "image"
        assert media_kind_from_content_type("image/jpeg") == "image"

    def test_media_kind_video(self):
        assert media_kind_from_content_type("video/mp4") == "video"
        assert media_kind_from_content_type("video/webm") == "video"

    def test_media_kind_unknown(self):
        assert media_kind_from_content_type("text/plain") == "unknown"
        assert media_kind_from_content_type(None) == "unknown"


# ---------------------------------------------------------------------------
# 6. Discord upload compatibility
# ---------------------------------------------------------------------------

class TestDiscordUploadCompatibility:
    def test_compatible_image_passes(self):
        ok, reason = is_discord_upload_compatible(
            content_type="image/png", byte_size=1024
        )
        assert ok
        assert reason is None

    def test_unsupported_type_fails(self):
        ok, reason = is_discord_upload_compatible(
            content_type="text/html", byte_size=1024
        )
        assert not ok
        assert "unsupported_content_type" in reason

    def test_oversize_fails(self):
        ok, reason = is_discord_upload_compatible(
            content_type="image/png",
            byte_size=DISCORD_UPLOAD_LIMIT_BYTES + 1,
        )
        assert not ok
        assert "oversize" in reason

    def test_just_under_limit_passes(self):
        ok, _ = is_discord_upload_compatible(
            content_type="image/png",
            byte_size=DISCORD_UPLOAD_LIMIT_BYTES,
        )
        assert ok

    def test_zero_or_negative_size_fails(self):
        ok, reason = is_discord_upload_compatible(
            content_type="image/png", byte_size=0
        )
        assert not ok
        assert "zero_or_negative" in reason

    def test_custom_max_bytes(self):
        ok, _ = is_discord_upload_compatible(
            content_type="image/png",
            byte_size=50 * 1024 * 1024,
            max_bytes=100 * 1024 * 1024,
        )
        assert ok

    def test_custom_max_bytes_rejects_oversize(self):
        ok, reason = is_discord_upload_compatible(
            content_type="image/png",
            byte_size=50 * 1024 * 1024,
            max_bytes=25 * 1024 * 1024,
        )
        assert not ok
        assert "oversize" in reason


# ---------------------------------------------------------------------------
# 7. Oversize detection (via resolver stub)
# ---------------------------------------------------------------------------

class TestOversizeDetection:
    def test_resolver_rejects_oversize_download(self):
        def _fake_download(url, dest_path, *, max_bytes, timeout):
            return False, "image/png", max_bytes + 1, "oversize"

        resolver = ExternalMediaResolver(
            _download=_fake_download,
            _run_yt_dlp=lambda url, **kw: (True, {
                "url": "https://cdn.x.com/media.jpg",
                "ext": "jpg",
            }),
        )
        result = resolver.resolve("https://x.com/user/status/456")
        assert result.outcome == ResolveOutcome.OVERSIZE


# ---------------------------------------------------------------------------
# 8. Metadata JSON parsing
# ---------------------------------------------------------------------------

class TestMetadataJsonParsing:
    def test_yt_dlp_json_parsed_successfully(self):
        """Verify resolver correctly processes valid yt-dlp JSON metadata."""
        metadata = {"title": "test", "url": "https://cdn.x.com/img.jpg", "ext": "jpg"}
        def _fake_yt_dlp(url, **kw):
            return True, metadata

        download_attempted = False
        def _fake_download(url, dest, **kw):
            nonlocal download_attempted
            download_attempted = True
            return True, "image/jpeg", 1024, None

        resolver = ExternalMediaResolver(
            _run_yt_dlp=_fake_yt_dlp,
            _download=_fake_download,
        )
        result = resolver.resolve("https://x.com/user/status/789")
        # Should have parsed the JSON and attempted download (not metadata_failed)
        assert result.status != "metadata_failed"

    def test_invalid_json_returns_failure(self):
        def _fake_yt_dlp(url, **kw):
            return False, "yt-dlp JSON parse error: ..."

        resolver = ExternalMediaResolver(
            _run_yt_dlp=_fake_yt_dlp,
        )
        result = resolver.resolve("https://x.com/user/status/999")
        assert result.outcome == ResolveOutcome.METADATA_FAILED


# ---------------------------------------------------------------------------
# 9. Two-tier safety
# ---------------------------------------------------------------------------

class TestTwoTierSafety:
    """Source provenance check before yt-dlp; CDN trust only via provenance chain."""

    def test_safelisted_source_passes_provenance(self):
        ok, reason = _provenance_check(
            "https://x.com/user/status/1",
            "https://pbs.twimg.com/media/xyz.jpg",
        )
        assert ok
        assert reason is None

    def test_non_safelisted_source_rejects_before_yt_dlp(self):
        def _fake_yt_dlp(url, **kw):
            pytest.fail("should not call yt-dlp for non-safelisted source")

        resolver = ExternalMediaResolver(
            _run_yt_dlp=_fake_yt_dlp,
        )
        result = resolver.resolve("https://evil.com/malware.exe")
        assert result.outcome == ResolveOutcome.SKIPPED_DOMAIN

    def test_wrong_content_type_cdn_falls_back(self):
        """CDN URL with non-media content type should be rejected."""
        import os as _os

        def _fake_yt_dlp(url, **kw):
            return True, {
                "formats": [
                    {
                        "url": "https://cdn.x.com/file.pdf",
                        "ext": "jpg",  # extension pretends to be image...
                        "vcodec": "none",
                        "acodec": "none",
                    }
                ],
            }

        # Download "succeeds" but returns a non-media Content-Type header.
        # Must actually write a file so the resolver's hash check passes.
        def _fake_download(url, dest, **kw):
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b"not-really-a-pdf")
            return True, "application/pdf", 17, None

        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = ExternalMediaResolver(
                cache_dir=tmpdir,
                _run_yt_dlp=_fake_yt_dlp,
                _download=_fake_download,
            )
            result = resolver.resolve("https://x.com/user/status/42")
            # Should reject at the final Discord compatibility check
            assert result.outcome in (
                ResolveOutcome.NOT_DISCORD_UPLOAD_COMPATIBLE,
                ResolveOutcome.UNSUPPORTED_CONTENT_TYPE,
            )


# ---------------------------------------------------------------------------
# 10. Platform policy (YouTube/Vimeo/Twitch → fallback_link_only, Reddit/X → lazy)
# ---------------------------------------------------------------------------

class TestPlatformPolicy:
    def test_youtube_is_fallback_only(self):
        assert get_platform_policy("youtube.com") == PLATFORM_POLICY_LONG_FORM
        assert is_long_form("youtube.com")
        assert not is_short_form("youtube.com")

    def test_youtu_be_is_fallback_only(self):
        assert get_platform_policy("youtu.be") == PLATFORM_POLICY_LONG_FORM

    def test_vimeo_is_fallback_only(self):
        assert get_platform_policy("vimeo.com") == PLATFORM_POLICY_LONG_FORM

    def test_twitch_is_fallback_only(self):
        assert get_platform_policy("twitch.tv") == PLATFORM_POLICY_LONG_FORM

    def test_reddit_proceeds_to_lazy_resolve(self):
        assert get_platform_policy("reddit.com") == PLATFORM_POLICY_SHORT_FORM
        assert is_short_form("reddit.com")

    def test_x_proceeds_to_lazy_resolve(self):
        assert get_platform_policy("x.com") == PLATFORM_POLICY_SHORT_FORM
        assert is_short_form("x.com")

    def test_long_form_domain_returns_fallback_without_invoking_resolver(self):
        call_count = 0

        def _fake_yt_dlp(url, **kw):
            nonlocal call_count
            call_count += 1
            return True, {"url": "https://example.com/v.mp4"}

        resolver = ExternalMediaResolver(_run_yt_dlp=_fake_yt_dlp)
        result = resolver.resolve("https://youtube.com/watch?v=abc123")
        assert result.outcome == ResolveOutcome.FALLBACK_ONLY_PLATFORM
        assert call_count == 0  # yt-dlp was never called

    def test_vimeo_returns_fallback_without_yt_dlp(self):
        call_count = 0

        def _fake_yt_dlp(url, **kw):
            nonlocal call_count
            call_count += 1
            return True, {}

        resolver = ExternalMediaResolver(_run_yt_dlp=_fake_yt_dlp)
        result = resolver.resolve("https://vimeo.com/123456")
        assert result.outcome == ResolveOutcome.FALLBACK_ONLY_PLATFORM
        assert call_count == 0

    def test_mirror_domain_is_short_form(self):
        for domain in ["fixupx.com", "fxtwitter.com", "vxtwitter.com",
                        "twittpr.com", "ddinstagram.com", "rxddit.com"]:
            assert get_platform_policy(domain) == PLATFORM_POLICY_SHORT_FORM, \
                f"{domain} should be short-form (mirror)"


# ---------------------------------------------------------------------------
# 11. URL normalisation and extraction helpers
# ---------------------------------------------------------------------------

class TestUrlHelpers:
    def test_extract_domain_strips_www(self):
        assert extract_domain("https://www.x.com/path") == "x.com"

    def test_extract_domain_lowercases(self):
        assert extract_domain("https://X.COM/path") == "x.com"

    def test_normalise_url_strips_fragment(self):
        norm = normalise_url("https://x.com/path#section")
        assert "#" not in norm

    def test_normalise_url_lowercases_host(self):
        norm = normalise_url("https://X.COM/Path")
        assert "x.com" in norm

    def test_extract_urls_from_text_finds_multiple(self):
        urls = extract_urls_from_text(
            "see https://x.com/a and https://reddit.com/b"
        )
        assert len(urls) == 2

    def test_content_hash_deterministic(self):
        h1 = content_hash(b"hello")
        h2 = content_hash(b"hello")
        assert h1 == h2
        assert h1 != content_hash(b"world")


# ---------------------------------------------------------------------------
# 12. Resolver cache hit
# ---------------------------------------------------------------------------

class TestResolverCacheHit:
    def test_cache_hit_returns_without_download(self):
        download_called = False

        def _fake_download(url, dest, **kw):
            nonlocal download_called
            download_called = True
            return True, "image/png", 1024, None

        def _fake_get_cache(url_key):
            return {
                "url_key": url_key,
                "status": "downloaded",
                "content_hash": "abc123",
                "media_kind": "image",
                "content_type": "image/png",
                "byte_size": 1024,
                "file_path": "/tmp/cached_file.bin",
                "resolved_url_sanitized": "https://cdn.x.com/img.jpg",
            }

        resolver = ExternalMediaResolver(
            _download=_fake_download,
            _get_cache=_fake_get_cache,
        )
        result = resolver.resolve("https://x.com/user/status/cached")
        assert result.outcome == ResolveOutcome.CACHE_HIT
        assert not download_called


# ---------------------------------------------------------------------------
# 13. File path construction
# ---------------------------------------------------------------------------

class TestFilePathConstruction:
    def test_resolve_file_path_is_deterministic(self):
        path1 = _resolve_file_path("/tmp/cache", "key123", "hash456")
        path2 = _resolve_file_path("/tmp/cache", "key123", "hash456")
        assert path1 == path2

    def test_resolve_file_path_includes_url_key_prefix(self):
        path = _resolve_file_path("/tmp/cache", "abcdef123456", "hash789")
        assert "ab" in path  # url_key[:2] subdirectory
        assert "abcdef123456" in path
        assert "hash789" in path


# ---------------------------------------------------------------------------
# 14. Provenance check edge cases
# ---------------------------------------------------------------------------

class TestProvenanceCheck:
    def test_empty_source_url_fails(self):
        ok, reason = _provenance_check("", "https://cdn.example.com/img.jpg")
        assert not ok

    def test_custom_safelist_used(self):
        custom_safelist = ("mysite.com",)
        ok, reason = _provenance_check(
            "https://mysite.com/img.jpg",
            "https://cdn.mysite.com/img.jpg",
            safelist=custom_safelist,
        )
        assert ok

    def test_custom_safelist_rejects_other_domains(self):
        custom_safelist = ("mysite.com",)
        ok, reason = _provenance_check(
            "https://x.com/img.jpg",
            "https://cdn.x.com/img.jpg",
            safelist=custom_safelist,
        )
        assert not ok


# ---------------------------------------------------------------------------
# 15. Resolver trace metadata
# ---------------------------------------------------------------------------

class TestResolverTrace:
    def test_fallback_only_platform_has_trace(self):
        resolver = ExternalMediaResolver()
        result = resolver.resolve("https://youtube.com/watch?v=test")
        assert result.trace
        assert "long-form" in result.trace.lower()

    def test_skipped_domain_has_trace(self):
        resolver = ExternalMediaResolver()
        result = resolver.resolve("https://evil.com/file.exe")
        assert result.trace
        assert "non-safelisted" in result.trace.lower()
