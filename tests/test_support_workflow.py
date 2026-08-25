"""Tests for the support agent's comfy_workflow tool.

Stubs a fake vibecompy package in sys.modules BEFORE importing the executor
module (same approach tests/conftest.py uses for src.common.llm), so every
path — describe, validate, edit, not-installed degradation, large-output file
posting, truncation — runs deterministic and offline.
"""
import json
import sys
import types

import pytest

from tests.conftest import load_module_from_repo


# ========== Fake vibecompy ==========

class FakeValidationIssue:
    def __init__(self, code, message, severity="error"):
        self.code = code
        self.message = message
        self.severity = severity


class FakeValidationReport:
    def __init__(self, ok=True, issues=None):
        self.ok = ok
        self.issues = issues or []


class FakeNode:
    def __init__(self, id, class_type, widgets=None, inputs=None, metadata=None):
        self.id = id
        self.class_type = class_type
        self.widgets = widgets if widgets is not None else {}
        self.inputs = inputs if inputs is not None else {}
        self.metadata = metadata or {}


class FakeWorkflow:
    """Minimal VibeWorkflow stand-in over an API-format dict."""

    def __init__(self, raw):
        self.id = "workflow"
        self.nodes = {}
        for node_id, entry in raw.items():
            params = dict(entry.get("inputs", {}))
            widgets = {k: v for k, v in params.items() if isinstance(v, (str, int, float, bool))}
            inputs = {k: v for k, v in params.items() if k not in widgets}
            self.nodes[node_id] = FakeNode(str(node_id), entry["class_type"], widgets, inputs)
        self.export_format_calls = 0

    def validate(self):
        return FakeValidationReport(ok=True, issues=[])

    def remove_node(self, node_id):
        del self.nodes[node_id]

    def export_to_json(self, format="api"):
        assert format == "api"
        self.export_format_calls += 1
        return {
            node.id: {
                "class_type": node.class_type,
                "inputs": {**node.widgets, **node.inputs},
            }
            for node in self.nodes.values()
        }


def _fake_named_import(raw):
    return FakeWorkflow(raw)


def _install_fake_vibecompy():
    """Install a fake vibecompy package tree into sys.modules."""
    if isinstance(sys.modules.get("vibecomfy"), types.ModuleType) \
            and getattr(sys.modules["vibecomfy"], "__fake__", False):
        return
    vibecompy = types.ModuleType("vibecomfy")
    vibecompy.__fake__ = True
    ingest_pkg = types.ModuleType("vibecomfy.ingest")
    ingest_pkg.__path__ = []
    normalize = types.ModuleType("vibecomfy.ingest.normalize")
    normalize._named_import = _fake_named_import
    sys.modules["vibecomfy"] = vibecompy
    sys.modules["vibecomfy.ingest"] = ingest_pkg
    sys.modules["vibecomfy.ingest.normalize"] = normalize


_install_fake_vibecompy()

comfy_tools = load_module_from_repo(
    "src/features/support/comfy_tools.py", "src.features.support.comfy_tools"
)

API_JSON = json.dumps({
    "6": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 20}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cozy cabin"}},
})


# ========== Tool definition shape ==========

def test_tool_definition_is_anthropic_shaped():
    (tool,) = comfy_tools.TOOLS
    assert tool["name"] == "comfy_workflow"
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"mode"}
    assert schema["properties"]["mode"]["enum"] == ["describe", "validate", "edit", "deliver"]


# ========== Input validation / degradation ==========

@pytest.mark.anyio
async def test_missing_source_errors():
    result = await comfy_tools.execute_comfy_workflow({"mode": "describe"})
    assert result == {"success": False, "error": "source is required: workflow JSON string or http(s) URL"}


@pytest.mark.anyio
async def test_bad_mode_errors():
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "run"})
    assert result["success"] is False
    assert "mode" in result["error"]


@pytest.mark.anyio
async def test_edit_without_ops_errors():
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "edit", "thread_id": 3})
    assert result["success"] is False
    assert "edit_ops" in result["error"]


@pytest.mark.anyio
async def test_vibecompy_not_installed_degrades_cleanly(monkeypatch):
    monkeypatch.setitem(sys.modules, "vibecompy", None)  # None -> ImportError on import
    for name in list(sys.modules):
        if name.startswith("vibecomfy.") :
            monkeypatch.delitem(sys.modules, name, raising=False)
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "describe"})
    assert result == {"success": False, "error": "vibecomfy package not installed on this host"}


# ========== Describe / validate happy paths ==========

@pytest.mark.anyio
async def test_describe_lists_nodes_with_widgets_and_validation():
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "describe"})
    assert result["success"] is True
    assert result["node_count"] == 2
    assert result["issues"] == []
    formatted = result["formatted"]
    assert "[0] KSampler (id=6)" in formatted
    assert "seed=42" in formatted and "steps=20" in formatted
    assert "[1] CLIPTextEncode (id=7)" in formatted
    assert "'a cozy cabin'" in formatted
    assert "no issues" in formatted


@pytest.mark.anyio
async def test_validate_reports_digest(monkeypatch):
    report = FakeValidationReport(ok=False, issues=[
        FakeValidationIssue("missing_model", "Model file not found."),
        FakeValidationIssue("legacy_pack", "Old pack.", severity="warning"),
    ])

    class ReportingWorkflow(FakeWorkflow):
        def validate(self):
            return report

    monkeypatch.setattr(
        sys.modules["vibecomfy.ingest.normalize"], "_named_import", lambda raw: ReportingWorkflow(raw)
    )
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "validate"})
    assert result["success"] is True
    assert result["ok"] is False
    codes = [issue["code"] for issue in result["issues"]]
    assert codes == ["missing_model", "legacy_pack"]
    severities = [issue["severity"] for issue in result["issues"]]
    assert severities == ["error", "warning"]
    assert "[error] missing_model" in result["formatted"]
    assert "[warning] legacy_pack" in result["formatted"]


@pytest.mark.anyio
async def test_url_source_fetches_then_describes(monkeypatch):
    fetched = {}

    async def fake_fetch(url):
        fetched["url"] = url
        return json.loads(API_JSON)

    monkeypatch.setattr(comfy_tools, "_fetch_workflow_json", fake_fetch)
    result = await comfy_tools.execute_comfy_workflow(
        {"source": "https://cdn.discordapp.com/attachments/1/2/wf.json", "mode": "describe"}
    )
    assert fetched["url"] == "https://cdn.discordapp.com/attachments/1/2/wf.json"
    assert result["success"] is True
    assert result["node_count"] == 2


# ========== Edit path ==========

@pytest.mark.anyio
async def test_edit_set_widget_by_name_and_remove_by_title():
    ops = [
        {"op": "set_widget", "node": "7", "widget": "text", "value": "updated prompt"},
        {"op": "remove_node", "node": 0},
    ]
    result = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit", "edit_ops": ops, "thread_id": 1}
    )
    assert result["success"] is True
    assert result["staged"] is True
    assert "set text on CLIPTextEncode (id=7)" in result["summary"] or \
           "removed KSampler (id=6)" in result["summary"]
    assert "2 total edit(s) staged" in result["summary"]
    assert "no issues" in result["summary"]
    staged = comfy_tools._STAGED[1]
    exported = staged["workflow"].export_to_json(format="api")
    edited = json.loads(exported) if isinstance(exported, str) else exported
    assert set(edited.keys()) == {"7"}
    assert edited["7"]["inputs"]["text"] == "updated prompt"


@pytest.mark.anyio
async def test_edit_set_widget_by_index_and_class_type_match():
    ops = [{"op": "set_widget", "node": "CLIPTextEncode", "widget": 0, "value": "by index"}]
    result = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit", "edit_ops": ops, "thread_id": 2}
    )
    assert result["success"] is True
    staged = comfy_tools._STAGED[2]
    exported = staged["workflow"].export_to_json(format="api")
    edited = json.loads(exported) if isinstance(exported, str) else exported
    # widget index 0 resolves into the first scalar param of the matched node
    assert any(v == "by index" for node in edited.values() for v in node["inputs"].values())

@pytest.mark.anyio
async def test_edit_unknown_widget_lists_available():
    result = await comfy_tools.execute_comfy_workflow({
        "source": API_JSON,
        "mode": "edit",
        "thread_id": 4,
        "edit_ops": [{"op": "set_widget", "node": "7", "widget": "nonesuch", "value": 1}],
    })
    assert "Available: ['text']" in result["error"]


@pytest.mark.anyio
async def test_edit_unresolvable_node_errors():
    result = await comfy_tools.execute_comfy_workflow({
        "source": API_JSON,
        "mode": "edit",
        "thread_id": 5,
        "edit_ops": [{"op": "remove_node", "node": "NopeNet"}],
    })
    assert "KSampler" in result["error"]  # error lists known nodes


@pytest.mark.anyio
async def test_edit_unsupported_op_errors():
    result = await comfy_tools.execute_comfy_workflow({
        "source": API_JSON,
        "mode": "edit",
        "thread_id": 6,
        "edit_ops": [{"op": "rebalance_chakras"}],
    })
    assert result["success"] is False
    assert "rebalance_chakras" in result["error"]


@pytest.mark.anyio
async def test_vibecompy_exception_caught_as_failure(monkeypatch):
    def boom(_raw):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(sys.modules["vibecomfy.ingest.normalize"], "_named_import", boom)
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "describe"})
    assert result["success"] is False
    assert "RuntimeError" in result["error"]
    assert "ingest exploded" in result["error"]


# ========== Large output handling ==========

class FakeThread:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeBot:
    def __init__(self, channel=None):
        self.channel = channel

    def get_channel(self, _channel_id):
        return self.channel


def _big_api_json(target_chars=2500):
    filler = "x" * 400
    raw = {}
    for i in range(10):
        raw[str(100 + i)] = {"class_type": f"Node_{i}", "inputs": {"text": filler}}
    return json.dumps(raw)


@pytest.mark.anyio
async def test_edit_stages_without_sending_then_deliver_attaches_once():
    """Edits stage silently; only mode=deliver attaches the file."""
    comfy_tools._STAGED.clear()
    thread = FakeThread()
    bot = FakeBot(thread)

    first = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "6", "widget": "seed", "value": 7}],
         "thread_id": 555},
        bot=bot,
    )
    assert first["success"] is True and first["staged"] is True
    assert len(thread.sent) == 0  # nothing sent yet

    # Follow-up edit omits source -> stacks on the staged copy.
    second = await comfy_tools.execute_comfy_workflow(
        {"mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "7", "widget": "text", "value": "fixed!"}],
         "thread_id": 555},
        bot=bot,
    )
    assert second["success"] is True
    assert "2 total edit(s) staged" in second["summary"]
    assert len(thread.sent) == 0

    deliver = await comfy_tools.execute_comfy_workflow(
        {"mode": "deliver", "thread_id": 555}, bot=bot,
    )
    assert deliver["success"] is True and deliver["posted_as_file"] is True
    assert "REMINDER" in deliver["summary"]
    assert len(thread.sent) == 1
    payload = thread.sent[0]["file"].fp.read().decode("utf-8")
    doc = json.loads(payload)
    assert doc["6"]["inputs"]["seed"] == 7  # both edits present
    assert "100" not in doc


@pytest.mark.anyio
async def test_deliver_with_nothing_staged_errors():
    comfy_tools._STAGED.clear()
    result = await comfy_tools.execute_comfy_workflow(
        {"mode": "deliver", "thread_id": 1}, bot=FakeBot(FakeThread()),
    )
    assert result["success"] is False
    assert "nothing staged" in result["error"]


@pytest.mark.anyio
async def test_new_source_starts_fresh_staging():
    comfy_tools._STAGED.clear()
    thread = FakeThread()
    bot = FakeBot(thread)
    await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "6", "widget": "seed", "value": 7}],
         "thread_id": 42}, bot=bot,
    )
    again = await comfy_tools.execute_comfy_workflow(
        {"source": _big_api_json(), "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": 0, "widget": "text", "value": "x"}],
         "thread_id": 42}, bot=bot,
    )
    assert again["success"] is True
    assert "1 total edit(s) staged" in again["summary"]


@pytest.mark.anyio
async def test_deliver_post_failure_is_a_clean_error():
    comfy_tools._STAGED.clear()
    # Stage under thread 999, then deliver with a bot that cannot resolve it.
    await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "6", "widget": "seed", "value": 7}],
         "thread_id": 999}, bot=FakeBot(FakeThread()),
    )
    result = await comfy_tools.execute_comfy_workflow(
        {"mode": "deliver", "thread_id": 999},
        bot=FakeBot(channel=None),
    )
    assert result["success"] is False
    assert "could not post the file attachment" in result["error"]


@pytest.mark.anyio
async def test_describe_and_validate_still_work_without_staging():
    comfy_tools._STAGED.clear()
    r1 = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "describe"})
    assert r1["success"] is True
    r2 = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "validate"})
    assert r2["success"] is True and r2["ok"] is not None


# ========== URL fetch hardening (SSRF) ==========

def _install_fake_aiohttp(monkeypatch, status=200, body=b"{}"):
    """Swap aiohttp for an offline fake; returns the recorded get() kwargs."""
    class FakeContent:
        def __init__(self, payload):
            self._payload = payload

        async def read(self, n):
            return self._payload[:n]

    class FakeSessionGet:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *exc):
            return False

    calls = {}

    class FakeSession:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, allow_redirects=True):
            calls["url"] = url
            calls["allow_redirects"] = allow_redirects
            response = types.SimpleNamespace(status=status)
            response.content = FakeContent(body)
            return FakeSessionGet(response)

    fake = types.ModuleType("aiohttp")
    fake.ClientTimeout = lambda **kwargs: kwargs
    fake.ClientSession = FakeSession
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return calls


@pytest.mark.anyio
async def test_fetch_rejects_non_https(monkeypatch):
    with pytest.raises(ValueError, match="https"):
        await comfy_tools._fetch_workflow_json("http://cdn.discordapp.com/wf.json")


@pytest.mark.anyio
async def test_fetch_rejects_disallowed_host(monkeypatch):
    with pytest.raises(ValueError, match="Allowed hosts"):
        await comfy_tools._fetch_workflow_json("https://internal.service.local/wf.json")
    with pytest.raises(ValueError, match="Allowed hosts"):
        await comfy_tools._fetch_workflow_json("https://cdn.discordapp.com.evil.io/wf.json")


@pytest.mark.anyio
async def test_fetch_does_not_follow_redirects(monkeypatch):
    calls = _install_fake_aiohttp(monkeypatch, status=302, body=b"")
    with pytest.raises(ValueError, match="HTTP 302"):
        await comfy_tools._fetch_workflow_json("https://comfyworkflows.com/wf.json")
    assert calls["allow_redirects"] is False


@pytest.mark.anyio
async def test_fetch_rejects_oversized_documents(monkeypatch):
    _install_fake_aiohttp(monkeypatch, status=200, body=b"x" * (comfy_tools.MAX_WORKFLOW_BYTES + 10))
    with pytest.raises(ValueError, match="size limit"):
        await comfy_tools._fetch_workflow_json("https://civitai.com/wf.json")


@pytest.mark.anyio
async def test_fetch_parses_allowlisted_https_document(monkeypatch):
    _install_fake_aiohttp(monkeypatch, status=200, body=API_JSON.encode("utf-8"))
    doc = await comfy_tools._fetch_workflow_json(
        "https://raw.githubusercontent.com/some/repo/main/wf.json"
    )
    assert doc["6"]["class_type"] == "KSampler"


# ========== thread_id requirements ==========

@pytest.mark.anyio
async def test_edit_without_thread_id_errors():
    comfy_tools._STAGED.clear()
    result = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "6", "widget": "seed", "value": 7}]},
    )
    assert result == {"success": False, "error": "thread_id is required for edit/deliver"}
    assert not comfy_tools._STAGED  # nothing staged without a thread key


@pytest.mark.anyio
async def test_deliver_without_thread_id_errors():
    comfy_tools._STAGED.clear()
    result = await comfy_tools.execute_comfy_workflow({"mode": "deliver"})
    assert result == {"success": False, "error": "thread_id is required for edit/deliver"}


@pytest.mark.anyio
async def test_edit_accepts_string_thread_id():
    comfy_tools._STAGED.clear()
    result = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "6", "widget": "seed", "value": 7}],
         "thread_id": "31337"},
    )
    assert result["success"] is True and result["staged"] is True
    assert 31337 in comfy_tools._STAGED


# ========== Partial-failure rollback (copy-on-write staging) ==========

@pytest.mark.anyio
async def test_failed_stacked_edit_rolls_back_to_prior_staged_state():
    comfy_tools._STAGED.clear()
    first = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "7", "widget": "text", "value": "good"}],
         "thread_id": 777},
    )
    assert first["success"] is True
    prior = comfy_tools._STAGED[777]

    # Second op in the stacked list raises -> the whole call must be a no-op.
    result = await comfy_tools.execute_comfy_workflow(
        {"mode": "edit",
         "edit_ops": [
             {"op": "set_widget", "node": "7", "widget": "text", "value": "bad"},
             {"op": "rebalance_chakras"},
         ],
         "thread_id": 777},
    )
    assert result["success"] is False
    assert "rebalance_chakras" in result["error"]
    assert comfy_tools._STAGED[777] is prior
    exported = prior["workflow"].export_to_json(format="api")
    assert exported["7"]["inputs"]["text"] == "good"
