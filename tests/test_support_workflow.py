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
    if isinstance(sys.modules.get("vibecompy"), types.ModuleType) \
            and getattr(sys.modules["vibecompy"], "__fake__", False):
        return
    vibecompy = types.ModuleType("vibecompy")
    vibecompy.__fake__ = True
    ingest_pkg = types.ModuleType("vibecompy.ingest")
    ingest_pkg.__path__ = []
    normalize = types.ModuleType("vibecompy.ingest.normalize")
    normalize._named_import = _fake_named_import
    sys.modules["vibecompy"] = vibecompy
    sys.modules["vibecompy.ingest"] = ingest_pkg
    sys.modules["vibecompy.ingest.normalize"] = normalize


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
    assert set(schema["required"]) == {"source", "mode"}
    assert schema["properties"]["mode"]["enum"] == ["describe", "validate", "edit"]


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
    result = await comfy_tools.execute_comfy_workflow({"source": API_JSON, "mode": "edit"})
    assert result["success"] is False
    assert "edit_ops" in result["error"]


@pytest.mark.anyio
async def test_vibecompy_not_installed_degrades_cleanly(monkeypatch):
    monkeypatch.setitem(sys.modules, "vibecompy", None)  # None -> ImportError on import
    for name in list(sys.modules):
        if name.startswith("vibecompy.") :
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
        sys.modules["vibecompy.ingest.normalize"], "_named_import", lambda raw: ReportingWorkflow(raw)
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
        {"source": "https://example.com/wf.json", "mode": "describe"}
    )
    assert fetched["url"] == "https://example.com/wf.json"
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
        {"source": API_JSON, "mode": "edit", "edit_ops": ops}
    )
    assert result["success"] is True
    edited = json.loads(result["workflow_json"])
    assert set(edited.keys()) == {"7"}
    assert edited["7"]["inputs"]["text"] == "updated prompt"
    assert "set text on CLIPTextEncode (id=7)" in result["summary"]
    assert "removed KSampler (id=6)" in result["summary"]
    assert "no issues" in result["summary"]


@pytest.mark.anyio
async def test_edit_set_widget_by_index_and_class_type_match():
    ops = [{"op": "set_widget", "node": "CLIPTextEncode", "widget": 0, "value": "by index"}]
    result = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit", "edit_ops": ops}
    )
    assert result["success"] is True
    edited = json.loads(result["workflow_json"])
    # widget index 0 resolves into the first scalar param of the matched node
    assert any(v == "by index" for node in edited.values() for v in node["inputs"].values())


@pytest.mark.anyio
async def test_edit_unknown_widget_lists_available():
    result = await comfy_tools.execute_comfy_workflow({
        "source": API_JSON,
        "mode": "edit",
        "edit_ops": [{"op": "set_widget", "node": "7", "widget": "nonesuch", "value": 1}],
    })
    assert result["success"] is False
    assert "nonesuch" in result["error"]
    assert "Available: ['text']" in result["error"]


@pytest.mark.anyio
async def test_edit_unresolvable_node_errors():
    result = await comfy_tools.execute_comfy_workflow({
        "source": API_JSON,
        "mode": "edit",
        "edit_ops": [{"op": "remove_node", "node": "NopeNet"}],
    })
    assert result["success"] is False
    assert "No node matches" in result["error"]
    assert "KSampler" in result["error"]  # error lists known nodes


@pytest.mark.anyio
async def test_edit_unsupported_op_errors():
    result = await comfy_tools.execute_comfy_workflow({
        "source": API_JSON,
        "mode": "edit",
        "edit_ops": [{"op": "rebalance_chakras"}],
    })
    assert result["success"] is False
    assert "rebalance_chakras" in result["error"]


@pytest.mark.anyio
async def test_vibecompy_exception_caught_as_failure(monkeypatch):
    def boom(_raw):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(sys.modules["vibecompy.ingest.normalize"], "_named_import", boom)
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
async def test_large_output_posted_as_file_to_thread():
    thread = FakeThread()
    bot = FakeBot(thread)
    result = await comfy_tools.execute_comfy_workflow(
        {"source": _big_api_json(), "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": 0, "widget": "text", "value": "edited!"}],
         "thread_id": 12345},
        bot=bot,
    )
    assert result["success"] is True
    assert result["posted_as_file"] is True
    assert "1 edit(s) applied" in result["summary"]
    assert len(thread.sent) == 1
    file = thread.sent[0]["file"]
    payload = file.fp.read().decode("utf-8")
    assert json.loads(payload)["100"]["inputs"]["text"] == "edited!"
    assert file.filename.startswith("edited_workflow_")
    assert file.filename.endswith(".json")


@pytest.mark.anyio
async def test_large_output_without_thread_id_truncates():
    big = _big_api_json()
    result = await comfy_tools.execute_comfy_workflow(
        {"source": big, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": 0, "widget": "text", "value": "edited!"}]}
    )
    assert result["success"] is True
    assert result.get("posted_as_file") is None
    assert result["truncated"] is True
    assert len(result["preview"]) == comfy_tools.TRUNCATED_PREVIEW_CHARS
    assert "truncated preview" in result["note"]
    assert result["preview"] != ""


@pytest.mark.anyio
async def test_large_output_falls_back_to_truncation_when_posting_fails():
    result = await comfy_tools.execute_comfy_workflow(
        {"source": _big_api_json(), "mode": "edit",
         "edit_ops": [{"op": "remove_node", "node": "100"}],
         "thread_id": 999},
        bot=FakeBot(channel=None),  # no resolvable channel -> post fails
    )
    assert result["success"] is True
    assert result.get("posted_as_file") is None
    assert result["truncated"] is True
    assert "could not be posted as a file" in result["note"]


@pytest.mark.anyio
async def test_small_output_returned_in_band():
    result = await comfy_tools.execute_comfy_workflow(
        {"source": API_JSON, "mode": "edit",
         "edit_ops": [{"op": "set_widget", "node": "6", "widget": "seed", "value": 7}],
         "thread_id": 555},
        bot=FakeBot(FakeThread()),
    )
    assert result["success"] is True
    assert "workflow_json" in result
    assert json.loads(result["workflow_json"])["6"]["inputs"]["seed"] == 7
