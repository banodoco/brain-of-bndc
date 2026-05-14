"""
Best-effort external linked media resolver for TopicEditor.

Two-tier safety:
  1. Source-provenance check using T1 safelist before yt-dlp or HTTP.
  2. Platform-policy respect (skip yt-dlp for fallback-link-only domains).

All side-effectful functions (subprocess, HTTP, filesystem) are injectable
so the resolver can be tested without real network calls.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.common.external_media import (
    SAFELISTED_DOMAINS,
    MEDIA_CONTENT_TYPES,
    DISCORD_UPLOAD_LIMIT_BYTES,
    content_hash as _content_hash,
    extract_domain,
    get_platform_policy,
    is_discord_compatible_content_type,
    is_discord_upload_compatible,
    is_safelisted,
    make_cache_key,
    media_kind_from_content_type,
    sanitise_url_for_logs,
    PLATFORM_POLICY_LONG_FORM,
    PLATFORM_POLICY_SHORT_FORM,
)

logger = logging.getLogger("DiscordBot")

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

# Default byte cap aligned with Discord bot upload limits (standard tier)
DEFAULT_MAX_BYTES = DISCORD_UPLOAD_LIMIT_BYTES  # 25 MiB

# Hard timeout for yt-dlp metadata extraction
DEFAULT_YT_DLP_TIMEOUT = 30  # seconds

# Hard timeout for HTTP download
DEFAULT_DOWNLOAD_TIMEOUT = 60  # seconds

# Streaming read chunk size for byte-limit enforcement
STREAM_CHUNK_SIZE = 64 * 1024  # 64 KiB

# Default cache directory (set via env var in production)
_CACHE_DIR = os.environ.get("EXTERNAL_MEDIA_CACHE_DIR", os.path.join(tempfile.gettempdir(), "bndc_external_media"))

# Max bytes configurable via env var
_MAX_BYTES = int(os.environ.get("EXTERNAL_MEDIA_MAX_BYTES", str(DEFAULT_MAX_BYTES)))


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------

class ResolveOutcome(str, Enum):
    """Structured outcome for external media resolution."""
    CACHE_HIT = "cache_hit"
    DOWNLOADED = "downloaded"
    SKIPPED_DOMAIN = "skipped_domain"  # domain not in safelist
    FALLBACK_ONLY_PLATFORM = "fallback_only_platform"  # long-form domain
    METADATA_FAILED = "metadata_failed"  # yt-dlp --dump-json failed
    DOWNLOAD_FAILED = "download_failed"  # HTTP download failed
    OVERSIZE = "oversize"  # content exceeds byte cap
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    NOT_DISCORD_UPLOAD_COMPATIBLE = "not_discord_upload_compatible"
    TIMED_OUT = "timed_out"  # subprocess or download timed out


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResolverResult:
    """Result of resolving an external media URL."""
    outcome: ResolveOutcome
    url_key: str  # cache key (content-hash of the original URL)
    source_url: str  # original URL (sanitised in logging)
    source_domain: str
    status: str  # snapshot of outcome for DB cache
    content_hash_val: Optional[str] = None
    media_kind: Optional[str] = None  # "image" | "video" | "unknown"
    content_type: Optional[str] = None
    byte_size: Optional[int] = None
    file_path: Optional[str] = None  # local path of downloaded file
    resolved_url: Optional[str] = None  # sanitised CDN URL (for logs)
    failure_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    trace: str = ""  # human-readable trace for logging


# ---------------------------------------------------------------------------
# Injectable side-effectful functions
# ---------------------------------------------------------------------------

# Default implementations — can be replaced in tests via monkeypatch or DI.

def _default_run_yt_dlp_json(
    url: str,
    *,
    timeout: int = DEFAULT_YT_DLP_TIMEOUT,
) -> Tuple[bool, Any]:
    """Run yt-dlp --dump-json to extract metadata without downloading.

    Returns (success, result) where result is either the parsed JSON dict
    (on success) or an error string (on failure).
    """
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--no-playlist",
                "--no-check-certificates",
                "--socket-timeout", str(timeout),
                "--retries", "1",
                "--flat-playlist",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return False, f"yt-dlp timed out after {timeout}s"
    except FileNotFoundError:
        return False, "yt-dlp binary not found"
    except Exception as exc:
        return False, f"yt-dlp subprocess error: {exc}"

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if stderr:
            return False, f"yt-dlp exit {proc.returncode}: {stderr[:500]}"
        return False, f"yt-dlp exit {proc.returncode}"

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return False, "yt-dlp produced empty output"

    try:
        import json
        data = json.loads(stdout)
        return True, data
    except json.JSONDecodeError as exc:
        return False, f"yt-dlp JSON parse error: {exc}"


def _default_download_url_to_path(
    url: str,
    dest_path: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Download a URL to a local file path with streaming byte-limit enforcement.

    Returns (success, content_type, byte_size, error_message).
    Uses streaming reads to enforce the byte cap during download, not after.
    """
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; BNDC-Bot/1.0; "
                    "+https://github.com/banodoco/brain-of-bndc)"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "").strip()

            total_read = 0
            chunks: List[bytes] = []
            while True:
                chunk = resp.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > max_bytes:
                    return False, content_type, total_read, (
                        f"oversize: downloaded {total_read} bytes, "
                        f"cap is {max_bytes}"
                    )
                chunks.append(chunk)

            data = b"".join(chunks)

            # Write to file
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(data)

            return True, content_type, len(data), None

    except urllib.error.HTTPError as exc:
        return False, None, 0, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, None, 0, f"URL error: {exc.reason}"
    except TimeoutError:
        return False, None, 0, f"download timed out after {timeout}s"
    except Exception as exc:
        return False, None, 0, f"download error: {exc}"


def _default_ensure_cache_dir(base_dir: str = _CACHE_DIR) -> str:
    """Ensure the cache directory exists and return its path."""
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _resolve_file_path(cache_dir: str, url_key: str, content_hash_val: str) -> str:
    """Build a deterministic file path from url_key and content_hash.

    Format: {cache_dir}/{url_key[:2]}/{url_key}_{content_hash_val[:16]}.bin
    """
    subdir = os.path.join(cache_dir, url_key[:2])
    filename = f"{url_key}_{content_hash_val[:16]}.bin"
    return os.path.join(subdir, filename)


def _provenance_check(
    source_url: str,
    resolved_cdn_url: str,
    *,
    safelist: Optional[Tuple[str, ...]] = None,
) -> Tuple[bool, Optional[str]]:
    """Two-tier provenance check.

    Tier 1: Source domain must be in safelist.
    Tier 2: yt-dlp resolved CDN URL must be from a safelisted source —
            but we do NOT trust the CDN host standalone. We only accept it
            because the source provenance chain was verified in Tier 1.
            (The actual content-type/byte-cap/Discord checks happen later.)

    Returns (passed, reason_if_not).
    """
    if safelist is None:
        safelist = SAFELISTED_DOMAINS

    source_domain = extract_domain(source_url)
    if source_domain not in safelist:
        return False, f"source_domain_not_safelisted:{source_domain}"

    # Tier 2: resolved CDN must be different from source (expected),
    # but we don't safelist the CDN host. Provenance is from the source.
    return True, None


# ---------------------------------------------------------------------------
# Main resolver orchestrator
# ---------------------------------------------------------------------------

@dataclass
class ExternalMediaResolver:
    """Resolves external media URLs for TopicEditor use.

    All side-effectful dependencies are injectable so tests can stub them.
    """

    cache_dir: str = _CACHE_DIR
    max_bytes: int = _MAX_BYTES
    yt_dlp_timeout: int = DEFAULT_YT_DLP_TIMEOUT
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    safelist: Tuple[str, ...] = SAFELISTED_DOMAINS

    # Injectable side-effectful functions
    _run_yt_dlp: Callable[..., Tuple[bool, Any]] = _default_run_yt_dlp_json
    _download: Callable[..., Tuple[bool, Optional[str], int, Optional[str]]] = _default_download_url_to_path
    _ensure_cache: Callable[..., str] = _default_ensure_cache_dir
    _get_cache: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None  # DB cache lookup
    _upsert_cache: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None  # DB cache write

    def resolve(
        self,
        source_url: str,
        *,
        source_message_id: Optional[str] = None,
    ) -> ResolverResult:
        """Resolve a single external media URL.

        Flow:
          1. Check platform policy → skip long-form domains immediately.
          2. Check source domain against safelist → skip non-safelisted.
          3. Compute cache key → check DB cache → return cache hit if found.
          4. Run yt-dlp --dump-json for metadata.
          5. Extract best CDN URL from metadata.
          6. Download with streaming byte-limit.
          7. Validate content-type, byte-size, Discord compatibility.
          8. Persist to cache and return result.
        """
        source_url_sanitised = sanitise_url_for_logs(source_url)
        source_domain = extract_domain(source_url)

        # ── Step 0: Platform policy ────────────────────────────────────
        policy = get_platform_policy(source_domain)
        if policy == PLATFORM_POLICY_LONG_FORM:
            reason = f"long_form_platform:{source_domain}"
            logger.debug(
                "ExternalMediaResolver: skipping long-form domain %s (%s)",
                source_domain, source_url_sanitised,
            )
            return ResolverResult(
                outcome=ResolveOutcome.FALLBACK_ONLY_PLATFORM,
                url_key=make_cache_key(source_url),
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="fallback_only_platform",
                failure_reason=reason,
                trace=f"skipped: long-form domain {source_domain}",
            )

        if policy == "unknown":
            reason = f"domain_not_safelisted:{source_domain}"
            logger.debug(
                "ExternalMediaResolver: skipping non-safelisted domain %s (%s)",
                source_domain, source_url_sanitised,
            )
            return ResolverResult(
                outcome=ResolveOutcome.SKIPPED_DOMAIN,
                url_key=make_cache_key(source_url),
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="skipped_domain",
                failure_reason=reason,
                trace=f"skipped: non-safelisted domain {source_domain}",
            )

        # ── Step 1: Source-provenance check ────────────────────────────
        passed, reason = _provenance_check(source_url, source_url, safelist=self.safelist)
        if not passed:
            return ResolverResult(
                outcome=ResolveOutcome.SKIPPED_DOMAIN,
                url_key=make_cache_key(source_url),
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="skipped_domain",
                failure_reason=reason,
                trace=f"skipped: {reason}",
            )

        url_key = make_cache_key(source_url)

        # ── Step 2: Check DB cache ─────────────────────────────────────
        if self._get_cache is not None:
            try:
                cached = self._get_cache(url_key)
                if cached is not None and cached.get("status") in ("downloaded", "cache_hit"):
                    logger.debug(
                        "ExternalMediaResolver: cache hit for %s",
                        source_url_sanitised,
                    )
                    return ResolverResult(
                        outcome=ResolveOutcome.CACHE_HIT,
                        url_key=url_key,
                        source_url=source_url_sanitised,
                        source_domain=source_domain,
                        status="cache_hit",
                        content_hash_val=cached.get("content_hash"),
                        media_kind=cached.get("media_kind"),
                        content_type=cached.get("content_type"),
                        byte_size=cached.get("byte_size"),
                        file_path=cached.get("file_path"),
                        resolved_url=cached.get("resolved_url_sanitized"),
                        metadata=cached.get("metadata") or {},
                        trace=f"cache_hit: {source_url_sanitised}",
                    )
            except Exception as exc:
                logger.warning(
                    "ExternalMediaResolver: cache lookup failed for %s: %s",
                    source_url_sanitised, exc,
                )

        # ── Step 3: yt-dlp metadata extraction ────────────────────────
        start_time = time.monotonic()
        success, metadata_or_error = self._run_yt_dlp(
            source_url,
            timeout=self.yt_dlp_timeout,
        )
        elapsed = time.monotonic() - start_time

        if not success:
            error_str = str(metadata_or_error)
            logger.info(
                "ExternalMediaResolver: yt-dlp metadata failed for %s in %.1fs: %s",
                source_url_sanitised, elapsed, error_str,
            )
            result = ResolverResult(
                outcome=(
                    ResolveOutcome.TIMED_OUT if "timed out" in error_str.lower()
                    else ResolveOutcome.METADATA_FAILED
                ),
                url_key=url_key,
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="metadata_failed",
                failure_reason=error_str,
                metadata={"yt_dlp_elapsed": elapsed},
                trace=f"metadata_failed: {error_str}",
            )
            self._persist_to_cache(result)
            return result

        metadata: Dict[str, Any] = metadata_or_error if isinstance(metadata_or_error, dict) else {}

        # ── Step 4: Extract best candidate URL from metadata ──────────
        candidate_url, candidate_content_type = self._extract_best_candidate(metadata, source_url)
        if not candidate_url:
            logger.info(
                "ExternalMediaResolver: no suitable media URL in yt-dlp metadata for %s",
                source_url_sanitised,
            )
            result = ResolverResult(
                outcome=ResolveOutcome.METADATA_FAILED,
                url_key=url_key,
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="metadata_failed",
                failure_reason="no_downloadable_url_in_metadata",
                metadata={
                    **metadata,
                    "yt_dlp_elapsed": elapsed,
                },
                trace="metadata_failed: no downloadable URL in metadata",
            )
            self._persist_to_cache(result)
            return result

        resolved_url_sanitised = sanitise_url_for_logs(candidate_url)

        # ── Step 5: Second-tier provenance check on resolved URL ─────
        # The resolved CDN URL host is NOT trusted standalone.
        # We only proceed because source provenance was verified in Step 1.
        # But we still verify that the content-type is media-compatible.
        logger.debug(
            "ExternalMediaResolver: resolved %s -> %s (type=%s)",
            source_url_sanitised, resolved_url_sanitised, candidate_content_type,
        )

        # ── Step 6: Content-type validation ────────────────────────────
        if candidate_content_type and not is_discord_compatible_content_type(candidate_content_type):
            result = ResolverResult(
                outcome=ResolveOutcome.UNSUPPORTED_CONTENT_TYPE,
                url_key=url_key,
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="unsupported_content_type",
                content_type=candidate_content_type,
                resolved_url=resolved_url_sanitised,
                failure_reason=f"unsupported_content_type:{candidate_content_type}",
                metadata={
                    **metadata,
                    "yt_dlp_elapsed": elapsed,
                },
                trace=f"rejected: unsupported content type {candidate_content_type}",
            )
            self._persist_to_cache(result)
            return result

        # ── Step 7: Download with streaming byte-limit ─────────────────
        _ensure_cache = self._ensure_cache if self._ensure_cache else _default_ensure_cache_dir
        cache_dir = _ensure_cache(self.cache_dir)

        # Compute preliminary content hash from URL (placeholder until download)
        temp_content_hash = _content_hash(candidate_url.encode("utf-8"))
        dest_path = _resolve_file_path(cache_dir, url_key, temp_content_hash)

        d_start = time.monotonic()
        d_success, d_content_type, d_byte_size, d_error = self._download(
            candidate_url,
            dest_path,
            max_bytes=self.max_bytes,
            timeout=self.download_timeout,
        )
        d_elapsed = time.monotonic() - d_start

        if not d_success:
            outcome = (
                ResolveOutcome.TIMED_OUT if "timed out" in str(d_error or "").lower()
                else ResolveOutcome.OVERSIZE if "oversize" in str(d_error or "").lower()
                else ResolveOutcome.DOWNLOAD_FAILED
            )
            logger.info(
                "ExternalMediaResolver: download failed for %s in %.1fs: %s",
                resolved_url_sanitised, d_elapsed, d_error,
            )
            result = ResolverResult(
                outcome=outcome,
                url_key=url_key,
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status=str(outcome.value),
                content_type=d_content_type,
                byte_size=d_byte_size,
                resolved_url=resolved_url_sanitised,
                failure_reason=d_error,
                metadata={
                    **metadata,
                    "yt_dlp_elapsed": elapsed,
                    "download_elapsed": d_elapsed,
                },
                trace=f"download_failed: {d_error}",
            )
            self._persist_to_cache(result)
            # Clean up partial file
            try:
                if os.path.exists(dest_path):
                    os.unlink(dest_path)
            except Exception:
                pass
            return result

        # ── Step 8: Recompute content_hash from actual content ────────
        try:
            with open(dest_path, "rb") as f:
                actual_data = f.read()
            actual_hash = _content_hash(actual_data)
        except Exception as exc:
            result = ResolverResult(
                outcome=ResolveOutcome.DOWNLOAD_FAILED,
                url_key=url_key,
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status="download_failed",
                content_type=d_content_type,
                byte_size=d_byte_size,
                resolved_url=resolved_url_sanitised,
                failure_reason=f"hash computation failed: {exc}",
                trace=f"download_failed: hash error: {exc}",
            )
            self._persist_to_cache(result)
            return result

        # Rename file to include actual content hash
        final_path = _resolve_file_path(cache_dir, url_key, actual_hash)
        if final_path != dest_path:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            try:
                os.rename(dest_path, final_path)
            except OSError:
                # Cross-device rename; fallback to copy+delete
                import shutil
                shutil.move(dest_path, final_path)

        # ── Step 9: Final Discord upload compatibility check ──────────
        effective_content_type = d_content_type or candidate_content_type
        compatible, compat_reason = is_discord_upload_compatible(
            content_type=effective_content_type,
            byte_size=d_byte_size,
            max_bytes=self.max_bytes,
        )
        if not compatible:
            logger.info(
                "ExternalMediaResolver: downloaded media not Discord-compatible for %s: %s",
                resolved_url_sanitised, compat_reason,
            )
            result = ResolverResult(
                outcome=(
                    ResolveOutcome.OVERSIZE if "oversize" in str(compat_reason or "").lower()
                    else ResolveOutcome.NOT_DISCORD_UPLOAD_COMPATIBLE
                ),
                url_key=url_key,
                source_url=source_url_sanitised,
                source_domain=source_domain,
                status=str(ResolveOutcome.NOT_DISCORD_UPLOAD_COMPATIBLE.value)
                    if "oversize" not in str(compat_reason or "").lower()
                    else str(ResolveOutcome.OVERSIZE.value),
                content_hash_val=actual_hash,
                media_kind=media_kind_from_content_type(effective_content_type),
                content_type=effective_content_type,
                byte_size=d_byte_size,
                file_path=final_path,
                resolved_url=resolved_url_sanitised,
                failure_reason=compat_reason,
                metadata={
                    **metadata,
                    "yt_dlp_elapsed": elapsed,
                    "download_elapsed": d_elapsed,
                },
                trace=f"rejected: {compat_reason}",
            )
            self._persist_to_cache(result)
            return result

        # ── Step 10: Success ───────────────────────────────────────────
        media_kind = media_kind_from_content_type(effective_content_type)
        logger.info(
            "ExternalMediaResolver: downloaded %s (%s, %s, %d bytes) in %.1fs",
            resolved_url_sanitised, media_kind, effective_content_type,
            d_byte_size, d_elapsed,
        )

        result = ResolverResult(
            outcome=ResolveOutcome.DOWNLOADED,
            url_key=url_key,
            source_url=source_url_sanitised,
            source_domain=source_domain,
            status="downloaded",
            content_hash_val=actual_hash,
            media_kind=media_kind,
            content_type=effective_content_type,
            byte_size=d_byte_size,
            file_path=final_path,
            resolved_url=resolved_url_sanitised,
            metadata={
                **metadata,
                "yt_dlp_elapsed": elapsed,
                "download_elapsed": d_elapsed,
            },
            trace=f"downloaded: {media_kind} {effective_content_type} {d_byte_size} bytes",
        )
        self._persist_to_cache(result)
        return result

    def _extract_best_candidate(
        self,
        metadata: Dict[str, Any],
        source_url: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Extract the best downloadable media URL from yt-dlp metadata.

        Returns (url, content_type) or (None, None) if nothing workable.

        Priority order:
          1. Direct media URL from formats (best quality image/video)
          2. thumbnail URL
          3. direct 'url' field in metadata
        """
        # Check formats for media candidates
        formats = metadata.get("formats") or []
        if isinstance(formats, list):
            # Prefer formats with direct URLs and known content types
            best_image: Optional[Dict[str, Any]] = None
            best_video: Optional[Dict[str, Any]] = None

            for fmt in formats:
                if not isinstance(fmt, dict):
                    continue
                fmt_url = fmt.get("url") or ""
                if not fmt_url:
                    continue

                fmt_type = (fmt.get("ext") or "").lower()
                vcodec = fmt.get("vcodec") or "none"
                acodec = fmt.get("acodec") or "none"

                # Video format
                if vcodec != "none" and acodec != "none":
                    # This is a combined video+audio format — too large, skip
                    continue

                if vcodec != "none":
                    # Video-only format
                    if best_video is None:
                        best_video = fmt
                    continue

                # Image or still format
                if fmt_type in ("jpg", "jpeg", "png", "gif", "webp", "bmp", "avif", "heic", "heif"):
                    if best_image is None:
                        best_image = fmt
                        continue
                    # Prefer larger images (more likely to be original quality)
                    curr_filesize = best_image.get("filesize") or 0
                    new_filesize = fmt.get("filesize") or 0
                    if new_filesize > curr_filesize:
                        best_image = fmt

            # Prefer image over video for Discord compatibility
            candidate = best_image or best_video
            if candidate:
                url = candidate.get("url")
                content_type_from_fmt = (
                    candidate.get("format_note") or ""
                )
                # Try to infer content type from extension
                ext = (candidate.get("ext") or "").lower()
                ct_map = {
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "png": "image/png",
                    "gif": "image/gif",
                    "webp": "image/webp",
                    "bmp": "image/bmp",
                    "avif": "image/avif",
                    "heic": "image/heic",
                    "heif": "image/heif",
                    "mp4": "video/mp4",
                    "webm": "video/webm",
                }
                inferred_ct = ct_map.get(ext)
                return url, inferred_ct

        # Fallback: thumbnail
        thumbnail = metadata.get("thumbnail") or ""
        if isinstance(thumbnail, str) and thumbnail.startswith("http"):
            return thumbnail, None

        # Fallback: direct url
        direct_url = metadata.get("url") or metadata.get("webpage_url") or ""
        if isinstance(direct_url, str) and direct_url.startswith("http") and direct_url != source_url:
            return direct_url, None

        return None, None

    def _persist_to_cache(self, result: ResolverResult) -> None:
        """Write the result to DB cache if cache callbacks are configured."""
        if self._upsert_cache is None:
            return
        try:
            row: Dict[str, Any] = {
                "url_key": result.url_key,
                "source_url_sanitized": result.source_url,
                "source_domain": result.source_domain,
                "status": result.status,
                "content_hash": result.content_hash_val,
                "media_kind": result.media_kind,
                "content_type": result.content_type,
                "byte_size": result.byte_size,
                "file_path": result.file_path,
                "resolved_url_sanitized": result.resolved_url,
                "failure_reason": result.failure_reason,
                "metadata": result.metadata,
            }
            self._upsert_cache(row)
        except Exception as exc:
            logger.warning(
                "ExternalMediaResolver: cache upsert failed for %s: %s",
                result.source_url, exc,
            )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def create_resolver(
    *,
    cache_dir: Optional[str] = None,
    max_bytes: Optional[int] = None,
    get_cache: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    upsert_cache: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    run_yt_dlp: Optional[Callable[..., Tuple[bool, Any]]] = None,
    download: Optional[Callable[..., Tuple[bool, Optional[str], int, Optional[str]]]] = None,
    ensure_cache: Optional[Callable[..., str]] = None,
) -> ExternalMediaResolver:
    """Create an ExternalMediaResolver with optional overrides.

    All parameters are optional; sensible defaults are used for any not provided.
    """
    kwargs: Dict[str, Any] = {}

    if cache_dir is not None:
        kwargs["cache_dir"] = cache_dir
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    if get_cache is not None:
        kwargs["_get_cache"] = get_cache
    if upsert_cache is not None:
        kwargs["_upsert_cache"] = upsert_cache
    if run_yt_dlp is not None:
        kwargs["_run_yt_dlp"] = run_yt_dlp
    if download is not None:
        kwargs["_download"] = download
    if ensure_cache is not None:
        kwargs["_ensure_cache"] = ensure_cache

    return ExternalMediaResolver(**kwargs)
