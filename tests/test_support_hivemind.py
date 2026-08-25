"""Offline tests for the support-agent hivemind search tool."""
from typing import Any, Dict, List, Optional

import aiohttp
import pytest
import asyncio
import src.features.support.tools_support as tools_support

pytestmark = pytest.mark.anyio


class FakeFetch:
    """Stands in for tools_support._fetch_json; records calls, returns canned rows."""

    def __init__(self, rows=None, exc=None, status_error=None):
        self.calls: List[Dict[str, Any]] = []
        self.rows = rows if rows is not None else []
        self.exc = exc
        self.status_error = status_error

    async def __call__(self, session, url):
        self.calls.append({
            "url": url,
            "headers": getattr(session, "headers", None),
            "session": session,
        })
        if self.exc is not None:
            raise self.exc
        if self.status_error is not None:
            raise RuntimeError(f"hivemind returned HTTP {self.status_error}: boom")
        return self.rows


class FakeSession:
    """Opaque session marker; _fetch_json is stubbed so nothing real happens."""

    headers = {"apikey": tools_support.HIVEMIND_API_KEY}


@pytest.fixture
def capture(monkeypatch):
    def _install(fetch):
        monkeypatch.setattr(tools_support, "_fetch_json", fetch)
        return fetch
    return _install


def _decode_query(url: str) -> Dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit
    return dict(parse_qsl(urlsplit(url).query))


# ========== URL / query construction ==========

async def test_messages_builds_or_over_tokenized_terms(capture):
    fetch = capture(FakeFetch())
    result = await tools_support.execute_search_hivemind(
        {"query": "wan animate vace"}, session=FakeSession())

    assert result["success"] is True
    url = fetch.calls[0]["url"]
    assert url.startswith(f"{tools_support.HIVEMIND_BASE_URL}/message_feed?")
    params = _decode_query(url)
    assert params["or"] == (
        "(content.ilike.*wan*,content.ilike.*animate*,content.ilike.*vace*)"
    )
    assert params["order"] == "created_at.desc"
    assert params["limit"] == "15"


async def test_select_projection_per_kind(capture):
    fetch = capture(FakeFetch())
    await tools_support.execute_search_hivemind(
        {"query": "lora", "kind": "distillations"}, session=FakeSession())
    params = _decode_query(fetch.calls[0]["url"])
    assert params["select"] == "question,answer,cites"
    assert params["status"] == "in.(pending,approved)"
    assert params["or"] == "(question.ilike.*lora*,answer.ilike.*lora*)"

    await tools_support.execute_search_hivemind(
        {"query": "comfy", "kind": "resources"}, session=FakeSession())
    params = _decode_query(fetch.calls[1]["url"])
    assert params["select"] == "kind,title,body,url,author"
    assert params["or"] == "(title.ilike.*comfy*,body.ilike.*comfy*)"
    assert "status" not in params


async def test_channel_filter_eq_and_in(capture):
    fetch = capture(FakeFetch())
    await tools_support.execute_search_hivemind(
        {"query": "vae", "channel": "wan_chatter"}, session=FakeSession())
    assert _decode_query(fetch.calls[0]["url"])["channel_name"] == "eq.wan_chatter"

    await tools_support.execute_search_hivemind(
        {"query": "vae", "channel": " wan_chatter , wan_comfyui "}, session=FakeSession())


async def test_channel_filter_ignored_for_non_message_kinds(capture):
    fetch = capture(FakeFetch())
    await tools_support.execute_search_hivemind(
        {"query": "vae", "kind": "resources", "channel": "wan_chatter"},
        session=FakeSession())
    assert "channel_name" not in _decode_query(fetch.calls[0]["url"])

class FakeResponseCtx:
    """Async context manager mimicking aiohttp's session.get(...) response."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return "boom"

    async def json(self):
        return self._payload


class RecordingSession:
    """Fake ClientSession: records get kwargs, yields a canned response."""

    def __init__(self, payload):
        self.payload = payload
        self.get_kwargs: Optional[Dict[str, Any]] = None

    def get(self_inner, url, **kwargs):
        self_inner.get_kwargs = kwargs
        return FakeResponseCtx(self_inner.payload)


async def test_fetch_json_sends_apikey_header_and_timeout():
    session = RecordingSession(payload=[{"ok": 1}])
    rows = await tools_support._fetch_json(session, "https://example/x")

    assert rows == [{"ok": 1}]
    assert session.get_kwargs["headers"]["apikey"] == tools_support.HIVEMIND_API_KEY
    assert session.get_kwargs["timeout"].total == tools_support.SEARCH_TIMEOUT_SECONDS


async def test_fetch_json_raises_on_http_error_status():
    ctx = FakeResponseCtx(payload=[], status=401)
    class ErrSession:
        def get(self_inner, url, **kwargs):
            return ctx
    with pytest.raises(RuntimeError, match="401"):
        await tools_support._fetch_json(ErrSession(), "https://example/x")


# ========== Formatting ==========

async def test_jump_url_formatting_for_messages(capture):
    rows = [{
        "message_id": 789,
        "content": "use vace for this\n\nit works great",
        "author_name": "Kijai",
        "channel_name": "wan_chatter",
        "channel_id": 456,
        "guild_id": 123,
        "created_at": "2026-04-01T12:00:00+00:00",
        "reactions": "🔥 x3",
    }]
    capture(FakeFetch(rows=rows))
    result = await tools_support.execute_search_hivemind({"query": "vace"}, session=FakeSession())

    formatted = result["formatted"]
    assert "https://discord.com/channels/123/456/789" in formatted
    assert "Kijai" in formatted and "#wan_chatter" in formatted and "2026-04-01" in formatted
    assert "use vace for this it works great" in formatted  # newlines collapsed
    assert result["results"] == rows


async def test_snippet_truncation(capture):
    rows = [{"message_id": 1, "guild_id": 1, "channel_id": 1,
             "content": "x" * 500, "author_name": "a", "channel_name": "c",
             "created_at": None, "reactions": None}]
    capture(FakeFetch(rows=rows))
    result = await tools_support.execute_search_hivemind({"query": "x"}, session=FakeSession())
    snippet_line = [ln for ln in result["formatted"].splitlines()
                    if ln.startswith("xxx")][0]
    assert len(snippet_line) <= tools_support.SNIPPET_MAX_CHARS + len("...") + 3
    assert snippet_line.endswith("...")


async def test_resource_results_cite_url_and_title(capture):
    rows = [{
        "kind": "article", "title": "VACE guide", "body": "how to use vace",
        "url": "https://example.com/vace", "author": "someone",
    }]
    capture(FakeFetch(rows=rows))
    result = await tools_support.execute_search_hivemind(
        {"query": "vace", "kind": "resources"}, session=FakeSession())
    assert "https://example.com/vace" in result["formatted"]
    assert "VACE guide" in result["formatted"]
    assert "discord.com/channels" not in result["formatted"]


async def test_empty_result_set_formats_gracefully(capture):
    capture(FakeFetch(rows=[]))
    result = await tools_support.execute_search_hivemind({"query": "zzz"}, session=FakeSession())
    assert result["success"] is True
    assert result["results"] == []
    assert "No hivemind results found." in result["formatted"]


# ========== Error paths ==========

async def test_empty_query_fails_without_http_call(capture):
    fetch = capture(FakeFetch())
    for bad in ("", "   ", None):
        result = await tools_support.execute_search_hivemind({"query": bad}, session=FakeSession())
        assert result == {"success": False, "error": "query is required"}
    assert fetch.calls == []


async def test_unknown_kind_fails(capture):
    capture(FakeFetch())
    result = await tools_support.execute_search_hivemind(
        {"query": "x", "kind": "unified_feed"}, session=FakeSession())
    assert result["success"] is False
    assert "kind must be one of" in result["error"]


async def test_timeout_returns_failure(capture):
    capture(FakeFetch(exc=asyncio.TimeoutError()))
    result = await tools_support.execute_search_hivemind({"query": "x"}, session=FakeSession())
    assert result == {"success": False, "error": "hivemind search timed out"}


async def test_client_error_returns_failure(capture):
    capture(FakeFetch(exc=aiohttp.ClientConnectionError("dns fail")))
    result = await tools_support.execute_search_hivemind({"query": "x"}, session=FakeSession())
    assert result["success"] is False
    assert "hivemind request failed" in result["error"]


async def test_http_error_status_returns_failure(capture):
    capture(FakeFetch(status_error=401))
    result = await tools_support.execute_search_hivemind({"query": "x"}, session=FakeSession())
    assert result["success"] is False
    assert "401" in result["error"]


async def test_unexpected_exception_never_raises(capture, monkeypatch):
    capture(FakeFetch())
    monkeypatch.setattr(tools_support, "build_hivemind_url",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    result = await tools_support.execute_search_hivemind({"query": "x"}, session=FakeSession())
    assert result["success"] is False
    assert "boom" in result["error"]


# ========== Tool definition shape ==========

def test_tool_definition_matches_anthropic_format():
    (tool,) = tools_support.TOOLS
    assert tool["name"] == "search_hivemind"
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    props = schema["properties"]
    assert set(props) == {"query", "channel", "kind", "limit"}
    assert props["kind"]["enum"] == ["messages", "distillations", "resources"]


def test_build_hivemind_url_rejects_empty_input():
    assert tools_support.build_hivemind_url("   ") is None
    assert tools_support.build_hivemind_url("x", kind="bogus") is None
