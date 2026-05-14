"""
Unit tests for src/common/vision_clients.py.

All external API calls are stubbed — zero real HTTP requests.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.common import vision_clients


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

VALID_IMAGE_JSON = json.dumps(
    {
        "kind": "generation",
        "subject": "A cyberpunk alleyway at night",
        "technical_signal": "sharp focus, good lighting, coherent composition",
        "aesthetic_quality": 7,
        "discriminator_notes": "Compelling for a generative-art roundup.",
    }
)

VALID_VIDEO_JSON = json.dumps(
    {
        "kind": "generation",
        "summary": "A 30-second clip of a dancer performing in a studio.",
        "visual_read": "single subject, medium shot, soft lighting",
        "audio_read": "music track with clear beat, no dialogue",
        "edit_value": "good candidate for a highlight reel",
        "highlight_score": 7.5,
        "energy": 6.0,
        "pacing": "steady",
        "production_quality": "clean footage, no artifacts",
        "boundary_notes": "trim 2s from start, 1s from end",
        "cautions": "verify music license",
    }
)

FAKE_OPENAI_RESPONSE = {
    "output_text": VALID_IMAGE_JSON,
}


def _fake_urlopen_bytes_response(data: dict):
    """Return a MagicMock that mimics urlopen for a successful JSON response."""

    class _FakeResponse:
        def read(self):
            return json.dumps(data).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return _FakeResponse()


def _fake_gemini_client():
    """Build a fake google.genai.Client with upload → poll → generate stubs."""

    uploaded = MagicMock()
    uploaded.name = "files/fake-upload-id"
    uploaded.state = "PROCESSING"

    active = MagicMock()
    active.name = "files/fake-upload-id"
    active.state = "ACTIVE"

    generated = MagicMock()
    generated.text = VALID_VIDEO_JSON

    client = MagicMock()
    # files.upload returns a processing file; files.get returns active on second call.
    client.files.upload.return_value = uploaded
    client.files.get.return_value = active
    client.models.generate_content.return_value = generated
    return client


def _fake_genai_module():
    """Return a module-like namespace that provides our fake Client + types."""
    fake = SimpleNamespace()
    fake.Client = lambda api_key: _fake_gemini_client()
    fake.types = SimpleNamespace()
    fake.types.GenerateContentConfig = MagicMock()
    return fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDescribeImage:
    def test_returns_structured_json(self, monkeypatch):
        """describe_image returns a dict with all expected keys."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")
        with patch.object(
            vision_clients, "urlopen", return_value=_fake_urlopen_bytes_response(FAKE_OPENAI_RESPONSE)
        ):
            result = vision_clients.describe_image(
                b"\xff\xd8\xff\xe0\x00\x10JFIF",  # minimal JPEG header
                model="gpt-4o-mini",
            )
        assert isinstance(result, dict)
        assert result["kind"] == "generation"
        assert result["subject"] == "A cyberpunk alleyway at night"
        assert result["technical_signal"] == "sharp focus, good lighting, coherent composition"
        assert result["aesthetic_quality"] == 7
        assert result["discriminator_notes"] == "Compelling for a generative-art roundup."

    def test_accepts_bytes_and_url(self, monkeypatch):
        """describe_image handles both raw bytes and URL strings."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")
        with patch.object(
            vision_clients, "urlopen", return_value=_fake_urlopen_bytes_response(FAKE_OPENAI_RESPONSE)
        ):
            # bytes input
            result_bytes = vision_clients.describe_image(
                b"\xff\xd8\xff\xe0\x00\x10JFIF",
                model="gpt-4o-mini",
            )
            assert result_bytes["kind"] == "generation"

            # URL string input
            result_url = vision_clients.describe_image(
                "https://example.com/photo.jpg",
                model="gpt-4o-mini",
            )
            assert result_url["kind"] == "generation"


class TestDescribeVideo:
    def test_uploads_polls_and_returns_json(self, monkeypatch):
        """describe_video simulates the upload → poll → generate flow."""
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-test-key")
        fake_genai = _fake_genai_module()

        # Inject the fake google module so `from google import genai` works.
        google_mod = SimpleNamespace(genai=fake_genai)
        monkeypatch.setitem(sys.modules, "google", google_mod)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_genai.types)

        # Clear the lazy-import cache so the injection takes effect.
        monkeypatch.delitem(vision_clients.__dict__, "_genai", raising=False)
        monkeypatch.delitem(vision_clients.__dict__, "_types", raising=False)

        # Use a path to a tiny mp4 file — create a temp one.
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(b"\x00\x00\x00\x18ftypmp42")  # minimal MP4 header
            mp4_path = Path(tmp.name)

        try:
            result = vision_clients.describe_video(str(mp4_path), model="gemini-2.5-flash")
        finally:
            mp4_path.unlink(missing_ok=True)

        # Clean up lazy-import state
        vision_clients.__dict__.pop("_genai", None)
        vision_clients.__dict__.pop("_types", None)

        assert isinstance(result, dict)
        assert result["kind"] == "generation"
        assert result["summary"] == "A 30-second clip of a dancer performing in a studio."
        assert result["visual_read"] == "single subject, medium shot, soft lighting"
        assert result["highlight_score"] == 7.5
        assert result["energy"] == 6.0
        assert result["pacing"] == "steady"


class TestSanitizeGeminiSchema:
    def test_drops_additional_properties(self):
        """_sanitize_gemini_schema recursively removes additionalProperties."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "nested": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "deep": {"type": "string", "additionalProperties": False}
                    },
                }
            },
        }
        result = vision_clients._sanitize_gemini_schema(schema)
        assert "additionalProperties" not in result
        assert "additionalProperties" not in result["properties"]["nested"]
        assert "additionalProperties" not in result["properties"]["nested"]["properties"]["deep"]
        assert result["properties"]["nested"]["properties"]["deep"]["type"] == "string"


class TestMissingSDK:
    def test_imports_at_import_time(self):
        """vision_clients module imports cleanly without any SDK required."""
        # Re-import to confirm no import-time errors (both SDKs are installed,
        # but the point is that the module doesn't eagerly import them).
        import importlib
        import src.common.vision_clients as vc

        importlib.reload(vc)
        # If we got here without ImportError, the module loads lazily.

    def test_describe_image_raises_at_call_time(self, monkeypatch):
        """describe_image raises ModuleNotFoundError when openai is unavailable."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")

        # Remove the cached _openai from module globals if present.
        monkeypatch.delitem(vision_clients.__dict__, "_openai", raising=False)

        # Make 'import openai' fail.
        import builtins

        orig_import = builtins.__import__

        def _fail_openai(name, *args, **kwargs):
            if name == "openai":
                raise ModuleNotFoundError("No module named 'openai'")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_openai)

        with pytest.raises(ModuleNotFoundError, match="openai package is required"):
            vision_clients.describe_image(b"\xff\xd8", model="gpt-4o-mini")

        # Restore module state
        vision_clients.__dict__.pop("_openai", None)

    def test_describe_video_raises_at_call_time(self, monkeypatch):
        """describe_video raises ModuleNotFoundError when google-genai is unavailable."""
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-test-key")

        # Remove cached imports
        monkeypatch.delitem(vision_clients.__dict__, "_genai", raising=False)
        monkeypatch.delitem(vision_clients.__dict__, "_types", raising=False)

        # Make 'google.genai' import fail.
        import builtins

        orig_import = builtins.__import__

        def _fail_genai(name, *args, **kwargs):
            if name == "google.genai":
                raise ModuleNotFoundError("No module named 'google.genai'")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_genai)

        from pathlib import Path

        with pytest.raises(ModuleNotFoundError, match="google-genai package is required"):
            vision_clients.describe_video(Path("/tmp/fake.mp4"), model="gemini-2.5-flash")

        # Restore module state
        vision_clients.__dict__.pop("_genai", None)
        vision_clients.__dict__.pop("_types", None)


class TestRetryOnce:
    def test_retries_once_on_transient_image(self, monkeypatch):
        """describe_image retries once when first call raises a transient error."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")

        call_count = 0

        def _flaky_urlopen(request, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("Connection timed out")
            return _fake_urlopen_bytes_response(FAKE_OPENAI_RESPONSE)

        with patch.object(vision_clients, "urlopen", _flaky_urlopen):
            result = vision_clients.describe_image(
                b"\xff\xd8\xff\xe0\x00\x10JFIF",
                model="gpt-4o-mini",
            )

        assert call_count == 2
        assert result["kind"] == "generation"

    def test_retries_once_on_transient_video(self, monkeypatch):
        """describe_video retries once when first call raises a transient error."""
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-test-key")

        call_count = 0
        fake_genai = _fake_genai_module()

        # The transient error will come from files.upload
        client_instance = _fake_gemini_client()
        generated = MagicMock()
        generated.text = VALID_VIDEO_JSON

        def _flaky_upload(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Temporary network issue")
            uploaded = MagicMock()
            uploaded.name = "files/fake-upload-id"
            uploaded.state = "ACTIVE"
            client_instance.models.generate_content.return_value = generated
            return uploaded

        client_instance.files.upload = _flaky_upload
        fake_genai.Client = lambda api_key: client_instance

        google_mod = SimpleNamespace(genai=fake_genai)
        monkeypatch.setitem(sys.modules, "google", google_mod)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_genai.types)

        monkeypatch.delitem(vision_clients.__dict__, "_genai", raising=False)
        monkeypatch.delitem(vision_clients.__dict__, "_types", raising=False)

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(b"\x00\x00\x00\x18ftypmp42")
            mp4_path = Path(tmp.name)

        try:
            result = vision_clients.describe_video(str(mp4_path), model="gemini-2.5-flash")
        finally:
            mp4_path.unlink(missing_ok=True)

        vision_clients.__dict__.pop("_genai", None)
        vision_clients.__dict__.pop("_types", None)

        assert call_count == 2
        assert result["kind"] == "generation"

    def test_does_not_retry_on_permanent_error(self, monkeypatch):
        """describe_image does NOT retry on permanent (non-transient) errors."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")

        call_count = 0

        def _fatal_urlopen(request, timeout):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Permanent authentication failure")

        with patch.object(vision_clients, "urlopen", _fatal_urlopen):
            with pytest.raises(RuntimeError):
                vision_clients.describe_image(
                    b"\xff\xd8\xff\xe0\x00\x10JFIF",
                    model="gpt-4o-mini",
                )

        assert call_count == 1  # never retried