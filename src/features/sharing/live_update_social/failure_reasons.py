"""Failure reason classification for social publishing.

Defines FailureReason enum and classify_failure() mapper used by both
enqueue and publish handlers to categorise why a social run failed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class FailureReason(str, Enum):
    """Classification of social publishing failures."""

    MEDIA_RESOLUTION_FAILED = "media_resolution_failed"
    PROVIDER_REJECTED_MEDIA = "provider_rejected_media"
    ROUTE_MISSING = "route_missing"
    DUPLICATE_PREVENTED = "duplicate_prevented"
    MODEL_SKIPPED = "model_skipped"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    PROVIDER_PUBLISH_FAILED = "provider_publish_failed"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    SIGNING_SECRET_MISSING = "signing_secret_missing"
    PUBLICATION_RECORD_FAILED = "publication_record_failed"
    MEDIA_UPLOAD_FAILED = "media_upload_failed"
    TEXT_ONLY_FALLBACK = "text_only_fallback"
    THREAD_ROOT_FAILED = "thread_root_failed"
    UNKNOWN = "unknown"


def classify_failure(
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> FailureReason:
    """Map an error to a FailureReason using message and context hints.

    Args:
        error_message: Human-readable error string.
        error_type: Machine-readable error category (e.g. provider key).
        context: Additional context dict with keys like
            ``duplicate``, ``route_error``, ``media_failed``.

    Returns:
        A FailureReason enum value.
    """
    ctx = context or {}
    msg = (error_message or "").lower()

    # Explicit context hints take priority
    if ctx.get("duplicate"):
        return FailureReason.DUPLICATE_PREVENTED
    if ctx.get("route_error"):
        return FailureReason.ROUTE_MISSING
    if ctx.get("media_failed"):
        return FailureReason.MEDIA_RESOLUTION_FAILED
    if ctx.get("provider_rejected_media"):
        return FailureReason.PROVIDER_REJECTED_MEDIA
    if ctx.get("model_skipped"):
        return FailureReason.MODEL_SKIPPED
    if ctx.get("human_review"):
        return FailureReason.HUMAN_REVIEW_REQUIRED
    if ctx.get("text_only_fallback"):
        return FailureReason.TEXT_ONLY_FALLBACK
    if ctx.get("thread_root_failed"):
        return FailureReason.THREAD_ROOT_FAILED

    # Message-based heuristics
    if "duplicate" in msg:
        return FailureReason.DUPLICATE_PREVENTED
    if "media" in msg and ("fail" in msg or "upload" in msg or "resolution" in msg):
        return FailureReason.MEDIA_RESOLUTION_FAILED
    if "route" in msg and ("missing" in msg or "not configured" in msg):
        return FailureReason.ROUTE_MISSING
    if "provider" in msg and ("reject" in msg or "fail" in msg):
        return FailureReason.PROVIDER_PUBLISH_FAILED
    if "signing secret" in msg:
        return FailureReason.SIGNING_SECRET_MISSING
    if "unsupported platform" in msg:
        return FailureReason.UNSUPPORTED_PLATFORM
    if "review" in msg and ("human" in msg or "required" in msg):
        return FailureReason.HUMAN_REVIEW_REQUIRED
    if "skip" in msg and ("model" in msg or "llm" in msg):
        return FailureReason.MODEL_SKIPPED
    if "root" in msg and "fail" in msg:
        return FailureReason.THREAD_ROOT_FAILED
    if "text.only" in msg.replace("-", "").replace(" ", ""):
        return FailureReason.TEXT_ONLY_FALLBACK

    # error_type heuristics
    if error_type:
        et = error_type.lower()
        if "duplicate" in et:
            return FailureReason.DUPLICATE_PREVENTED
        if "media" in et:
            return FailureReason.MEDIA_RESOLUTION_FAILED
        if "route" in et:
            return FailureReason.ROUTE_MISSING
        if "provider" in et:
            return FailureReason.PROVIDER_PUBLISH_FAILED

    return FailureReason.UNKNOWN
