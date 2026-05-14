"""
Pure shared helpers for external-linked media detection and safety classification.

This module MUST NOT import from src.features.* — common cannot depend on features.
All functions are pure/stateless: no subprocess, no network, no filesystem.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Domain safelist — mirrors curator.py:133 plus canonical domains
# ---------------------------------------------------------------------------

# Short-form / social domains: eligible for lazy yt-dlp resolution
_SHORT_FORM_DOMAINS: Tuple[str, ...] = (
    "reddit.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "tiktok.com",
    "streamable.com",
)

# Long-form / video platform domains: fallback-link-only (no yt-dlp download)
_LONG_FORM_DOMAINS: Tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "twitch.tv",
)

# Mirror/embed-fix domains (from curator.py:139-144)
_MIRROR_DOMAINS: Tuple[str, ...] = (
    "fixupx.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "twittpr.com",
    "ddinstagram.com",
    "rxddit.com",
)

# Combined safelist: all domains allowed as external link refs
SAFELISTED_DOMAINS: Tuple[str, ...] = (
    _SHORT_FORM_DOMAINS + _LONG_FORM_DOMAINS + _MIRROR_DOMAINS
)

# ---------------------------------------------------------------------------
# Platform policy
# ---------------------------------------------------------------------------

PLATFORM_POLICY_SHORT_FORM = "lazy_resolve"   # eligible for yt-dlp download
PLATFORM_POLICY_LONG_FORM = "fallback_link_only"  # never yt-dlp download


def get_platform_policy(domain: str) -> str:
    """Return the platform policy for a given domain.

    Returns 'lazy_resolve' for short-form/social domains,
    'fallback_link_only' for long-form/video domains,
    and 'unknown' for anything not in the safelist.
    """
    domain = _normalise_domain(domain)
    if domain in _SHORT_FORM_DOMAINS:
        return PLATFORM_POLICY_SHORT_FORM
    if domain in _LONG_FORM_DOMAINS:
        return PLATFORM_POLICY_LONG_FORM
    if domain in _MIRROR_DOMAINS:
        # Mirror domains resolve to canonical short-form sources
        return PLATFORM_POLICY_SHORT_FORM
    return "unknown"


def is_safelisted(domain: str) -> bool:
    """Check whether a domain is in the safelist (any tier)."""
    return _normalise_domain(domain) in SAFELISTED_DOMAINS


def is_short_form(domain: str) -> bool:
    """Check whether the domain is short-form (eligible for lazy resolution)."""
    return get_platform_policy(domain) == PLATFORM_POLICY_SHORT_FORM


def is_long_form(domain: str) -> bool:
    """Check whether the domain is long-form (fallback-link-only)."""
    return get_platform_policy(domain) == PLATFORM_POLICY_LONG_FORM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"https?://[^\s<>\"'{}|\\^`\[\]]+",
    re.IGNORECASE,
)


def _normalise_domain(domain: str) -> str:
    """Lowercase and strip 'www.' prefix from a domain."""
    domain = (domain or "").strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def extract_domain(url: str) -> str:
    """Extract the domain from a URL, lowercased and without www."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except Exception:
        return ""


def extract_urls_from_text(text: str) -> List[str]:
    """Extract all http/https URLs from a text string, preserving order."""
    if not text:
        return []
    return _URL_RE.findall(text)


def normalise_url(url: str) -> str:
    """Normalize a URL: strip fragments, lowercase host, remove tracking params.

    Does NOT strip query tokens used for auth — those are handled by sanitise
    separately. This is for cache-key stability, not security.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        # Strip fragment
        cleaned = parsed._replace(fragment="")
        # Lowercase netloc
        cleaned = cleaned._replace(netloc=cleaned.netloc.lower())
        return urllib.parse.urlunparse(cleaned)
    except Exception:
        return url


def sanitise_url_for_logs(url: str) -> str:
    """Return a log-safe version of a URL with query tokens redacted.

    Keeps scheme + domain + path. Replaces query params containing tokens
    with '[REDACTED]'.
    """
    if not url:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "unknown").lower()
        path = parsed.path or "/"
        query = parsed.query
        if query:
            # If any query param looks like a token (long, random-looking),
            # redact the entire query string.
            sensitive_params = {"token", "key", "sig", "signature", "expires",
                                "access_token", "api_key", "auth", "authorization",
                                "secret", "password", "s", "t", "e"}
            params = urllib.parse.parse_qs(query, keep_blank_values=True)
            has_sensitive = any(
                p.lower() in sensitive_params or (len(v[0]) > 30 if v else False)
                for p, v in params.items()
            )
            if has_sensitive:
                query_str = "[REDACTED]"
            else:
                query_str = f"?{query}"
        else:
            query_str = ""

        url_str = f"https://{host}{path}{query_str}"
        return url_str
    except Exception:
        # If we can't parse, return a fully redacted placeholder
        return f"[unparseable-url:{hash(url) & 0xFFFF:04x}]"


def make_cache_key(url: str) -> str:
    """Create a stable cache key from a URL using SHA-256 of the normalised form."""
    normalised = normalise_url(url)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def content_hash(data: bytes) -> str:
    """Compute SHA-256 hex digest of binary content."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Content-type classification
# ---------------------------------------------------------------------------

# Content types eligible for Discord file upload as media
MEDIA_CONTENT_TYPES: Tuple[str, ...] = (
    # Images
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/apng",
    "image/avif",
    "image/bmp",
    "image/tiff",
    "image/svg+xml",
    "image/heic",
    "image/heif",
    # Videos
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/x-flv",
    "video/x-ms-wmv",
    "video/x-m4v",
)

# Default Discord bot upload limit (used as fallback cap)
DISCORD_UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024  # 25 MiB (standard bot limit)

# Boosted server / higher-tier limit
DISCORD_UPLOAD_LIMIT_BYTES_BOOSTED = 100 * 1024 * 1024  # 100 MiB


def is_discord_compatible_content_type(content_type: Optional[str]) -> bool:
    """Check if a content type is compatible with Discord file upload."""
    if not content_type:
        return False
    ct = content_type.strip().lower()
    # Remove charset suffix (e.g. "image/png; charset=utf-8")
    if ";" in ct:
        ct = ct.split(";")[0].strip()
    return ct in MEDIA_CONTENT_TYPES


def media_kind_from_content_type(content_type: Optional[str]) -> str:
    """Return 'image', 'video', or 'unknown' based on content type."""
    if not content_type:
        return "unknown"
    ct = content_type.strip().lower()
    if ";" in ct:
        ct = ct.split(";")[0].strip()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    return "unknown"


def is_discord_upload_compatible(
    *,
    content_type: Optional[str] = None,
    byte_size: Optional[int] = None,
    max_bytes: int = DISCORD_UPLOAD_LIMIT_BYTES,
) -> Tuple[bool, Optional[str]]:
    """Check whether a downloaded file can be uploaded to Discord.

    Returns (compatible, reason_if_not).
    """
    if not is_discord_compatible_content_type(content_type):
        return False, f"unsupported_content_type:{content_type}"

    if byte_size is not None and byte_size <= 0:
        return False, "zero_or_negative_byte_size"

    if byte_size is not None and byte_size > max_bytes:
        return False, f"oversize:{byte_size}>{max_bytes}"

    return True, None


# ---------------------------------------------------------------------------
# Deterministic external URL extraction
# ---------------------------------------------------------------------------


def extract_external_urls(
    message: Dict[str, Any],
    *,
    safelist: Optional[Tuple[str, ...]] = None,
    include_url: bool = False,
) -> List[Dict[str, Any]]:
    """Scan a Discord message for external URLs that match the domain safelist.

    Deterministic ordering (same result regardless of caller):
      1. URLs from message content (before clean_content fallback).
      2. URLs from message clean_content.
      3. URLs from embeds: url → thumbnail → image → video fields,
         in that order, deduplicating URLs already seen.

    Returns a list of compact dicts:
        {kind: 'external', index: N, domain: str, url_present: true,
         source: 'content'|'embed', platform_policy: str}

    Note: index N is the position in this list (0-based). When called from
    different contexts with the same message data, this function produces
    identical output, ensuring external index agreement.
    """
    if safelist is None:
        safelist = SAFELISTED_DOMAINS

    results: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    def _add(url: str, source: str) -> None:
        domain = extract_domain(url)
        if domain not in safelist:
            return
        normalised = normalise_url(url)
        if normalised in seen_urls:
            return
        seen_urls.add(normalised)
        item = {
            "kind": "external",
            "index": len(results),
            "domain": domain,
            "url_present": True,
            "source": source,
            "platform_policy": get_platform_policy(domain),
        }
        if include_url:
            item["url"] = url
        results.append(item)

    # Phase 1: content URLs (primary)
    content = message.get("content") or ""
    if isinstance(content, str):
        for url in extract_urls_from_text(content):
            _add(url, "content")

    # Phase 2: clean_content URLs (fallback, may duplicate content)
    clean_content = message.get("clean_content") or ""
    if isinstance(clean_content, str) and clean_content != content:
        for url in extract_urls_from_text(clean_content):
            _add(url, "content")

    # Phase 3: embed URLs (stable field order)
    embeds = message.get("embeds")
    if isinstance(embeds, list):
        for emb in embeds:
            if not isinstance(emb, dict):
                continue
            # url field (direct string or dict with url/proxy_url)
            for field_name in ("url", "thumbnail", "image", "video"):
                value = emb.get(field_name)
                if isinstance(value, dict):
                    url = value.get("url") or value.get("proxy_url")
                    if url and isinstance(url, str):
                        _add(url, "embed")
                elif isinstance(value, str) and value:
                    _add(value, "embed")

    return results


def extract_external_url_at_index(
    message: Dict[str, Any],
    index: int,
    *,
    safelist: Optional[Tuple[str, ...]] = None,
) -> Optional[str]:
    """Return the original URL for the N-th safelisted external URL.

    This uses the same extraction order and dedupe rules as
    ``extract_external_urls`` so prompt payloads, validation, rendering, and
    fallback publishing cannot drift.
    """
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        return None
    if wanted < 0:
        return None
    items = extract_external_urls(message, safelist=safelist, include_url=True)
    if wanted >= len(items):
        return None
    url = items[wanted].get("url")
    return url if isinstance(url, str) and url else None


def extract_external_urls_compact(
    message: Dict[str, Any],
    *,
    safelist: Optional[Tuple[str, ...]] = None,
) -> List[Dict[str, Any]]:
    """Same as extract_external_urls but without domain/policy metadata.

    Returns compact entries safe for agent-facing payloads:
        {kind: 'external', index: N, url_present: true, source: 'content'|'embed'}
    """
    full = extract_external_urls(message, safelist=safelist)
    return [
        {
            "kind": item["kind"],
            "index": item["index"],
            "url_present": item["url_present"],
            "source": item["source"],
        }
        for item in full
    ]
