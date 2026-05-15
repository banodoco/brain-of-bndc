"""Durable media validation and upload helpers for queue-safe social URLs.

Uploads resolved media to durable Supabase storage so queued
publications survive process restarts and deploys.

Strategy: download media from its source URL (e.g. Discord CDN),
upload the raw bytes to a dedicated Supabase storage bucket, and
return the durable public URL.  Queue mode stores *durable* URLs,
never ephemeral CDN URLs.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")

# ── Constants ──────────────────────────────────────────────────────────

SOCIAL_MEDIA_BUCKET_DEFAULT = "social-media"

# Maximum file sizes (bytes)
MAX_IMAGE_SIZE_BYTES = 15 * 1024 * 1024   # 15 MB
MAX_VIDEO_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_TOTAL_MEDIA_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB total per post

# Allowed content types (image and video)
ALLOWED_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp",
})
ALLOWED_VIDEO_TYPES = frozenset({
    "video/mp4", "video/mpeg", "video/quicktime", "video/webm",
})

# Provider-specific max media counts
PROVIDER_MAX_MEDIA = {
    "twitter": 4,
    "x": 4,
}


def _social_media_bucket() -> str:
    """Return the Supabase storage bucket name for social media."""
    return os.getenv("SOCIAL_MEDIA_BUCKET", SOCIAL_MEDIA_BUCKET_DEFAULT)


def _validate_media_item(
    item: Dict[str, Any],
    platform: Optional[str] = None,
) -> Optional[str]:
    """Validate a single media item; return error message or None.

    Checks:
      - source_url is present and looks like a URL
      - content_type is present and allowed (image or video)
      - size (if known) does not exceed provider limits
    """
    source_url = item.get("source_url") or item.get("url")
    if not source_url or not isinstance(source_url, str):
        return "media item missing source_url"
    if not (source_url.startswith("http://") or source_url.startswith("https://")):
        return f"invalid source_url: {source_url[:100]}"

    content_type = item.get("content_type", "")
    if not content_type:
        # Try to guess from URL
        content_type, _ = mimetypes.guess_type(source_url)
    if not content_type:
        return f"media item missing content_type for {source_url[:80]}"

    is_image = content_type in ALLOWED_IMAGE_TYPES or content_type.startswith("image/")
    is_video = content_type in ALLOWED_VIDEO_TYPES or content_type.startswith("video/")
    if not is_image and not is_video:
        return (
            f"unsupported media content_type {content_type} "
            f"for {source_url[:80]}"
        )

    size = item.get("size")
    if size is not None:
        max_size = MAX_VIDEO_SIZE_BYTES if is_video else MAX_IMAGE_SIZE_BYTES
        if size > max_size:
            return (
                f"media size {size} bytes exceeds max {max_size} "
                f"for {source_url[:80]}"
            )

    # Provider-specific checks
    if platform:
        platform_lower = platform.lower()
        if platform_lower in PROVIDER_MAX_MEDIA:
            # Per-item checks could be added here
            pass

    return None  # valid


def validate_media_batch(
    items: List[Dict[str, Any]],
    platform: Optional[str] = None,
) -> List[str]:
    """Validate a batch of media items; return list of error messages.

    Returns an empty list when all items are valid.
    Also checks total count against provider limits.
    """
    errors: List[str] = []

    if not items:
        return errors

    # Provider max count check
    if platform:
        platform_lower = platform.lower()
        max_count = PROVIDER_MAX_MEDIA.get(platform_lower)
        if max_count is not None and len(items) > max_count:
            errors.append(
                f"platform {platform} supports at most {max_count} "
                f"media items, got {len(items)}"
            )

    # Per-item validation
    for i, item in enumerate(items):
        err = _validate_media_item(item, platform)
        if err:
            errors.append(f"media item {i}: {err}")

    return errors


def _build_storage_path(
    content_type: str,
    source_url: str,
) -> str:
    """Build a stable storage path from a source URL and content type."""
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    unique_id = str(uuid.uuid4())[:12]

    # Determine extension from content_type or URL
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
    if not ext:
        # Fall back to URL extension
        url_path = source_url.split("?")[0]
        ext = os.path.splitext(url_path)[1]
    if not ext:
        if content_type.startswith("image/"):
            ext = ".png"
        elif content_type.startswith("video/"):
            ext = ".mp4"
        else:
            ext = ".bin"

    return f"social/{date_prefix}/{unique_id}{ext}"


async def upload_to_durable_storage(
    db_handler: "DatabaseHandler",
    source_url: str,
    content_type: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Download media from source_url and upload to durable Supabase storage.

    Returns:
        (durable_url, error) — durable_url is the Supabase public URL,
        error is a human-readable error string when durable_url is None.
        Both are never None simultaneously; one is always populated.

    The durable URL is suitable for persisting in queued publications
    and will survive process restarts / deploys.
    """
    if not source_url:
        return None, "source_url is empty"

    bucket = _social_media_bucket()
    storage_path = _build_storage_path(content_type or "application/octet-stream", source_url)

    try:
        # Use the db_handler's existing download-and-upload helper
        durable_url = await db_handler.download_and_upload_media(source_url, storage_path)
        if not durable_url:
            return None, (
                f"Failed to upload {source_url[:120]} to durable storage "
                f"(bucket={bucket}, path={storage_path})"
            )

        logger.info(
            "Durable upload succeeded: %s → %s",
            source_url[:100],
            durable_url[:100],
        )
        return durable_url, None

    except Exception as e:
        logger.error(
            "Durable upload failed for %s: %s",
            source_url[:120],
            e,
            exc_info=True,
        )
        return None, f"Durable upload error: {e}"


async def upload_media_batch_to_durable(
    db_handler: "DatabaseHandler",
    items: List[Dict[str, Any]],
    platform: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate and upload a batch of media items to durable storage.

    Args:
        db_handler: DatabaseHandler with Supabase storage access.
        items: List of media item dicts, each must have at minimum
               ``source_url`` (str) and optionally ``content_type``,
               ``size``, and ``identity``.
        platform: Target social platform for validation (e.g. 'twitter').

    Returns:
        (uploaded, failed) — two lists:
          - uploaded: items with ``durable_url`` set on success.
          - failed: items with ``error`` set on failure.
        The original items are not mutated; new dicts are returned.
    """
    uploaded: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    validation_errors = validate_media_batch(items, platform)
    if validation_errors:
        # All items fail if batch validation fails
        for i, item in enumerate(items):
            err = (
                validation_errors[i] if i < len(validation_errors)
                else "batch validation failed"
            )
            failed.append({**item, "error": err, "durable_url": None})
        return uploaded, failed

    for item in items:
        source_url = item.get("source_url") or item.get("url")
        content_type = item.get("content_type", "")

        # Check size before downloading
        size = item.get("size")
        is_video = content_type and (
            content_type in ALLOWED_VIDEO_TYPES or content_type.startswith("video/")
        )
        max_size = MAX_VIDEO_SIZE_BYTES if is_video else MAX_IMAGE_SIZE_BYTES
        if size is not None and size > max_size:
            failed.append({
                **item,
                "error": f"size {size} exceeds max {max_size}",
                "durable_url": None,
            })
            continue

        # Check total size of already-uploaded items
        total_uploaded = sum(
            (u.get("size") or 0) for u in uploaded
        )
        if size is not None and total_uploaded + size > MAX_TOTAL_MEDIA_SIZE_BYTES:
            failed.append({
                **item,
                "error": "total media size would exceed the 200 MB limit",
                "durable_url": None,
            })
            continue

        durable_url, error = await upload_to_durable_storage(
            db_handler, source_url, content_type,
        )

        if durable_url:
            uploaded.append({
                **item,
                "durable_url": durable_url,
                "bucket": _social_media_bucket(),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
            failed.append({
                **item,
                "error": error or "unknown upload failure",
                "durable_url": None,
            })

    return uploaded, failed
