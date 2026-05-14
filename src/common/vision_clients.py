"""
Vision clients for image and video understanding.

Image:  OpenAI Responses API (/v1/responses), models gpt-4o-mini / gpt-5.4.
Video:  Gemini SDK (google-genai), models gemini-2.5-flash / gemini-2.5-pro.

Lazy SDK imports — nothing fails at import-time when the optional packages
are missing; the error surfaces at call-time with a clear message.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _is_transient_error(exc: Exception) -> bool:
    """True when *exc* looks like a transient / retryable failure."""
    message = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "timeout", "tempor", "connection", "rate limit",
        "overloaded", "unavailable", "429", "500", "502", "503", "504",
    )
    return any(marker in message for marker in markers)


def _sanitize_gemini_schema(node: Any) -> Any:
    """Recursively drop ``additionalProperties``, which Gemini rejects."""
    if isinstance(node, dict):
        return {
            k: _sanitize_gemini_schema(v)
            for k, v in node.items()
            if k != "additionalProperties"
        }
    if isinstance(node, list):
        return [_sanitize_gemini_schema(v) for v in node]
    return node


# ---------------------------------------------------------------------------
# Image schema
# ---------------------------------------------------------------------------

IMAGE_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "generation",
                "screenshot",
                "meme",
                "workflow_graph",
                "chart",
                "other",
            ],
        },
        "subject": {"type": "string"},
        "technical_signal": {"type": "string"},
        "aesthetic_quality": {"type": "integer", "minimum": 0, "maximum": 10},
        "discriminator_notes": {"type": "string"},
    },
    "required": [
        "kind",
        "subject",
        "technical_signal",
        "aesthetic_quality",
        "discriminator_notes",
    ],
}

DEFAULT_IMAGE_QUERY = (
    "Describe this image for editorial triage. Return compact JSON with: "
    "kind (generation|screenshot|meme|workflow_graph|chart|other), "
    "subject (what it depicts), technical_signal (quality/clarity cues), "
    "aesthetic_quality (integer 0-10), and discriminator_notes (what "
    "makes it compelling or not for sharing)."
)

# ---------------------------------------------------------------------------
# Video schema  (Astrid fields + kind enum)
# ---------------------------------------------------------------------------

VIDEO_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "generation",
                "screenshot",
                "meme",
                "workflow_graph",
                "chart",
                "other",
            ],
        },
        "summary": {"type": "string"},
        "visual_read": {"type": "string"},
        "audio_read": {"type": "string"},
        "edit_value": {"type": "string"},
        "highlight_score": {"type": "number"},
        "energy": {"type": "number"},
        "pacing": {"type": "string"},
        "production_quality": {"type": "string"},
        "boundary_notes": {"type": "string"},
        "cautions": {"type": "string"},
    },
    "required": [
        "kind",
        "summary",
        "visual_read",
        "audio_read",
        "edit_value",
        "highlight_score",
        "energy",
        "pacing",
        "production_quality",
        "boundary_notes",
        "cautions",
    ],
}

DEFAULT_VIDEO_QUERY = (
    "Watch this video as editorial evidence, using both picture and sound.\n\n"
    "Return compact JSON with:\n"
    "- kind: generation|screenshot|meme|workflow_graph|chart|other\n"
    "- summary: what happens in the clip\n"
    "- visual_read: people, setting, framing, action, text, graphics, cuts, camera motion\n"
    "- audio_read: speech delivery, music/SFX, applause/laughter, room tone, noise, sync issues\n"
    "- edit_value: why this moment is or is not useful in a cut\n"
    "- highlight_score: 0-10\n"
    "- energy: 0-10\n"
    "- pacing: slow/steady/fast/chaotic\n"
    "- production_quality: visual/audio quality problems, bad cuts, focus/exposure, clipping, echo\n"
    "- boundary_notes: suggested clean in/out points relative to this window\n"
    "- cautions: uncertainty or details that need transcript/frame/audio follow-up"
)


# ---------------------------------------------------------------------------
# OpenAI Responses API helpers (image)
# ---------------------------------------------------------------------------

def _resolve_image_data(image_bytes_or_url: Any) -> str:
    """Accept bytes, Path, or a URL string — always return a data URL."""
    if isinstance(image_bytes_or_url, bytes):
        data = base64.b64encode(image_bytes_or_url).decode("ascii")
        return f"data:image/jpeg;base64,{data}"

    if isinstance(image_bytes_or_url, Path):
        raw = image_bytes_or_url.read_bytes()
        media_type = (
            mimetypes.guess_type(image_bytes_or_url.name)[0]
            or "image/jpeg"
        )
        data = base64.b64encode(raw).decode("ascii")
        return f"data:{media_type};base64,{data}"

    text = str(image_bytes_or_url)
    if text.startswith(("http://", "https://", "data:")):
        return text

    # Last resort: treat as a local path
    path = Path(text).expanduser()
    raw = path.read_bytes()
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(raw).decode("ascii")
    return f"data:{media_type};base64,{data}"


def _call_openai_responses(
    *,
    api_key: str,
    model: str,
    image_data_url: str,
    query: str,
    timeout: int = 120,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": query},
                    {
                        "type": "input_image",
                        "image_url": image_data_url,
                        "detail": "low",
                    },
                ],
            }
        ],
        "max_output_tokens": 700,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "image_understanding",
                "schema": IMAGE_RESPONSE_SCHEMA,
                "strict": True,
            }
        },
    }

    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenAI API error {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def _parse_openai_response(response: dict[str, Any]) -> dict[str, Any]:
    """Extract the JSON payload from a Responses API response."""
    # Direct output_text
    text = response.get("output_text")
    if isinstance(text, str) and text.strip():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Walk output blocks
    for item in response.get("output") or []:
        for content in item.get("content") or []:
            raw = content.get("text")
            if isinstance(raw, str) and raw.strip():
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass

    raise RuntimeError(
        "OpenAI response did not contain parseable JSON"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def describe_image(
    image_bytes_or_url: bytes | Path | str,
    model: str,
    query: str | None = None,
) -> dict[str, Any]:
    """Describe an image via the OpenAI Responses API.

    Parameters
    ----------
    image_bytes_or_url:
        Raw bytes, a ``pathlib.Path``, or a http/data URL string.
    model:
        OpenAI model name, e.g. ``"gpt-4o-mini"`` or ``"gpt-5.4"``.
    query:
        Instruction text.  Falls back to a default editorial-triage prompt.

    Returns
    -------
    dict
        Keys: ``kind``, ``subject``, ``technical_signal``,
        ``aesthetic_quality`` (0-10), ``discriminator_notes``.
    """
    # Lazy import — fail at call-time if openai is not installed.
    global _openai
    if "_openai" not in globals():
        try:
            import openai as _openai
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "openai package is required for describe_image(). "
                "Install it with: pip install openai"
            )

    import os

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    image_data_url = _resolve_image_data(image_bytes_or_url)
    final_query = query or DEFAULT_IMAGE_QUERY

    for attempt in range(2):
        try:
            response = _call_openai_responses(
                api_key=api_key,
                model=model,
                image_data_url=image_data_url,
                query=final_query,
            )
            return _parse_openai_response(response)
        except Exception as exc:
            if attempt == 1 or not _is_transient_error(exc):
                raise
            time.sleep(1.0)

    raise RuntimeError("describe_image exhausted retries")


def describe_video(
    video_path_or_bytes: bytes | Path | str,
    model: str,
) -> dict[str, Any]:
    """Describe a video via the Gemini SDK (upload → poll → generate).

    Parameters
    ----------
    video_path_or_bytes:
        Raw bytes or a ``pathlib.Path`` (or string path) to an ``.mp4`` file.
    model:
        Gemini model, e.g. ``"gemini-2.5-flash"`` or ``"gemini-2.5-pro"``.

    Returns
    -------
    dict
        Astrid video fields (``summary``, ``visual_read``, …) plus ``kind``.
    """
    # Lazy imports — fail at call-time.
    global _genai, _types
    if "_genai" not in globals():
        try:
            from google import genai as _genai
            from google.genai import types as _types
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "google-genai package is required for describe_video(). "
                "Install it with: pip install google-genai"
            )

    import os

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    # Materialize bytes to a temp file if needed.
    if isinstance(video_path_or_bytes, bytes):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        try:
            tmp.write(video_path_or_bytes)
            tmp.flush()
            video_path = Path(tmp.name)
        finally:
            tmp.close()
        _cleanup = True
    else:
        video_path = Path(video_path_or_bytes)
        _cleanup = False

    try:
        sanitized_schema = _sanitize_gemini_schema(VIDEO_RESPONSE_SCHEMA)
        client = _genai.Client(api_key=api_key)

        for attempt in range(2):
            upload_name: str | None = None
            try:
                uploaded = client.files.upload(file=str(video_path))
                upload_name = getattr(uploaded, "name", None)

                # Poll until ACTIVE (max 180 s).
                deadline = time.monotonic() + 180.0
                while True:
                    state = str(getattr(uploaded, "state", "")).upper()
                    if state.endswith("ACTIVE"):
                        break
                    if state.endswith("FAILED"):
                        raise RuntimeError(
                            f"Gemini upload entered FAILED state: {upload_name}"
                        )
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            f"Gemini upload {upload_name} did not become ACTIVE "
                            f"within 180 s (state={state!r})"
                        )
                    time.sleep(2.0)
                    uploaded = client.files.get(name=upload_name)

                response = client.models.generate_content(
                    model=model,
                    contents=[uploaded, DEFAULT_VIDEO_QUERY],
                    config=_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=sanitized_schema,
                    ),
                )
                return json.loads(response.text)

            except Exception as exc:
                if attempt == 1 or not _is_transient_error(exc):
                    raise
                time.sleep(1.0)
            finally:
                if upload_name:
                    try:
                        client.files.delete(name=upload_name)
                    except Exception:
                        pass

        raise RuntimeError("describe_video exhausted retries")
    finally:
        if _cleanup and video_path.exists():
            try:
                video_path.unlink()
            except Exception:
                pass
