"""Media understanding module for the live-update social review loop.

Provides ``understand_image`` and ``understand_video`` handlers that
analyse media using Gemini multimodal capabilities and return typed
``ToolResult`` envelopes with truncation metadata.

When Gemini is not configured, the handlers return ``ToolResult(ok=False)``
with a descriptive error — they never crash the calling loop.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

from .models import ToolResult

logger = logging.getLogger("DiscordBot")

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_MEDIA_UNDERSTANDING_MODEL = "gemini-2.5-pro-preview-03-25"

# Truncation thresholds
MAX_DESCRIPTION_LENGTH = 4000  # characters — truncate beyond this
TRUNCATION_WARNING_LENGTH = 3000  # characters — set truncated=True beyond this

# Gemini prompt templates
IMAGE_UNDERSTANDING_PROMPT = (
    "Describe this image in detail. Include:\n"
    "1. What is depicted (subjects, objects, setting)\n"
    "2. Key visual elements (colors, composition, text if any)\n"
    "3. The overall mood, style, and likely context\n"
    "4. Whether this image would be suitable for sharing on social media\n"
    "   (consider quality, clarity, appropriateness)\n"
    "Keep the description concise but thorough — around 2-4 paragraphs."
)

VIDEO_UNDERSTANDING_PROMPT = (
    "Analyse this video frame-by-frame or holistically. Include:\n"
    "1. What is happening in the video (action, subjects, setting)\n"
    "2. Key visual and audio elements (if discernible)\n"
    "3. The overall mood, style, and likely context\n"
    "4. Whether this video would be suitable for sharing on social media\n"
    "   (consider quality, clarity, appropriateness, duration)\n"
    "Keep the description concise but thorough — around 2-4 paragraphs."
)


def _is_gemini_configured() -> bool:
    """Check whether Gemini is available and configured."""
    return bool(os.getenv("GEMINI_API_KEY"))


def _get_gemini_client():
    """Get a GeminiClient instance, or None if unavailable."""
    try:
        from src.common.llm.gemini_client import GeminiClient
        client = GeminiClient()
        if not client._configured or not client.client:
            logger.warning("GeminiClient is not configured")
            return None
        return client
    except Exception as e:
        logger.warning("Failed to initialise GeminiClient: %s", e)
        return None


def _truncate_description(description: str) -> tuple:
    """Truncate a description and return (text, truncated, note)."""
    if not description:
        return "", False, None

    if len(description) <= TRUNCATION_WARNING_LENGTH:
        return description, False, None

    if len(description) <= MAX_DESCRIPTION_LENGTH:
        return description, True, (
            f"Description is {len(description)} characters (threshold: "
            f"{TRUNCATION_WARNING_LENGTH})"
        )

    truncated = description[:MAX_DESCRIPTION_LENGTH]
    return truncated, True, (
        f"Description truncated from {len(description)} to "
        f"{MAX_DESCRIPTION_LENGTH} characters"
    )


async def _call_gemini_multimodal(
    prompt: str,
    image_data: Optional[bytes] = None,
    image_mime_type: Optional[str] = None,
    video_data: Optional[bytes] = None,
    video_mime_type: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Call Gemini with a prompt and optional image/video data.

    Returns the response text or None on failure.
    """
    client = _get_gemini_client()
    if not client:
        return None

    effective_model = model or os.getenv(
        "MEDIA_UNDERSTANDING_MODEL",
        DEFAULT_MEDIA_UNDERSTANDING_MODEL,
    )

    # Build the content parts for the user message
    user_parts: list = []
    user_parts.append({"type": "text", "text": prompt})

    if image_data and image_mime_type:
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        user_parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_mime_type,
                "data": image_b64,
            },
        })

    if video_data and video_mime_type:
        video_b64 = base64.b64encode(video_data).decode("utf-8")
        user_parts.append({
            "type": "video",
            "source": {
                "type": "base64",
                "media_type": video_mime_type,
                "data": video_b64,
            },
        })

    messages = [
        {"role": "user", "content": user_parts},
    ]

    try:
        response = await client.generate_chat_completion(
            model=effective_model,
            system_prompt="You are a media analysis assistant. "
                          "Describe media clearly and concisely.",
            messages=messages,
            temperature=0.3,
            max_output_tokens=1024,
        )
        return response
    except Exception as e:
        logger.error(
            "Gemini multimodal call failed for %s: %s",
            "video" if video_data else "image",
            e,
            exc_info=True,
        )
        return None


async def _download_media_bytes(
    url: str,
    max_size: int = 100 * 1024 * 1024,
) -> Optional[tuple]:
    """Download media bytes from a URL.

    Returns (bytes, content_type) or None on failure.
    """
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Media download HTTP %d for %s",
                        resp.status,
                        url[:120],
                    )
                    return None

                # Check Content-Length if available
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > max_size:
                    logger.warning(
                        "Media size %s exceeds max %d for %s",
                        content_length,
                        max_size,
                        url[:120],
                    )
                    return None

                content_type = resp.headers.get("Content-Type", "")
                data = await resp.read()

                if len(data) > max_size:
                    logger.warning(
                        "Downloaded media size %d exceeds max %d for %s",
                        len(data),
                        max_size,
                        url[:120],
                    )
                    return None

                return data, content_type

    except Exception as e:
        logger.error(
            "Error downloading media from %s: %s",
            url[:120],
            e,
            exc_info=True,
        )
        return None


async def understand_image(
    source_url: str,
    content_type: Optional[str] = None,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> ToolResult:
    """Analyse an image from a URL and return a structured description.

    Downloads the image bytes, feeds them to Gemini (if configured) along
    with a descriptive prompt, and returns a ``ToolResult``.

    Args:
        source_url: URL of the image to analyse (must be accessible).
        content_type: MIME type (e.g. 'image/png'). Guessed from URL if absent.
        prompt: Custom prompt for analysis. Uses default image prompt if absent.
        model: Gemini model name. Defaults to ``MEDIA_UNDERSTANDING_MODEL``
               or ``gemini-2.5-pro-preview-03-25``.

    Returns:
        ToolResult with ``ok=True, data={'description': ...}`` on success,
        or ``ok=False, error=...`` on failure.  ``truncated`` is True when
        the description was truncated for length.
    """
    tool_name = "understand_image"

    if not source_url:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="source_url is required",
        )

    if not _is_gemini_configured():
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="Gemini API is not configured (GEMINI_API_KEY not set)",
        )

    effective_prompt = prompt or IMAGE_UNDERSTANDING_PROMPT

    # Download image bytes
    downloaded = await _download_media_bytes(source_url)  # default 100MB max
    if not downloaded:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error=f"Failed to download image from {source_url[:120]}",
        )

    image_bytes, inferred_type = downloaded
    effective_content_type = content_type or inferred_type

    # Call Gemini
    description = await _call_gemini_multimodal(
        prompt=effective_prompt,
        image_data=image_bytes,
        image_mime_type=effective_content_type,
        model=model,
    )

    if description is None:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="Gemini API call failed — check logs for details",
        )

    text, truncated, truncation_note = _truncate_description(description)

    return ToolResult(
        ok=True,
        tool_name=tool_name,
        data={
            "description": text,
            "source_url": source_url,
            "content_type": effective_content_type,
            "model": model or DEFAULT_MEDIA_UNDERSTANDING_MODEL,
        },
        truncated=truncated,
        truncation_note=truncation_note,
    )


async def understand_video(
    source_url: str,
    content_type: Optional[str] = None,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_size: int = 100 * 1024 * 1024,
) -> ToolResult:
    """Analyse a video from a URL and return a structured description.

    Downloads the video bytes (up to ``max_size``), feeds a representative
    portion to Gemini (if configured), and returns a ``ToolResult``.

    .. note::

       Gemini video understanding is limited by model capabilities.
       Very large videos may be sampled or may fail.  Consider using
       ``max_size`` to bound downloads.

    Args:
        source_url: URL of the video to analyse (must be accessible).
        content_type: MIME type (e.g. 'video/mp4'). Guessed from URL if absent.
        prompt: Custom prompt for analysis. Uses default video prompt if absent.
        model: Gemini model name.
        max_size: Maximum bytes to download (default 100 MB).

    Returns:
        ToolResult with ``ok=True, data={'description': ...}`` on success,
        or ``ok=False, error=...`` on failure.
    """
    tool_name = "understand_video"

    if not source_url:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="source_url is required",
        )

    if not _is_gemini_configured():
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="Gemini API is not configured (GEMINI_API_KEY not set)",
        )

    effective_prompt = prompt or VIDEO_UNDERSTANDING_PROMPT

    # Download video bytes
    downloaded = await _download_media_bytes(source_url, max_size=max_size)
    if not downloaded:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error=f"Failed to download video from {source_url[:120]}",
        )

    video_bytes, inferred_type = downloaded
    effective_content_type = content_type or inferred_type

    # Call Gemini (video analysis may fail for large videos — Gemini
    # has model-specific limits; the error is surfaced gracefully)
    description = await _call_gemini_multimodal(
        prompt=effective_prompt,
        video_data=video_bytes,
        video_mime_type=effective_content_type,
        model=model,
    )

    if description is None:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error="Gemini API call failed — check logs for details",
        )

    text, truncated, truncation_note = _truncate_description(description)

    return ToolResult(
        ok=True,
        tool_name=tool_name,
        data={
            "description": text,
            "source_url": source_url,
            "content_type": effective_content_type,
            "model": model or DEFAULT_MEDIA_UNDERSTANDING_MODEL,
        },
        truncated=truncated,
        truncation_note=truncation_note,
    )
