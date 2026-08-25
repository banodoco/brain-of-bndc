"""ComfyUI workflow tool for the support agent.

Lets the LLM inspect, validate, and edit ComfyUI workflows (API or UI format)
by delegating to the vibecompy package when it is available on this host.
Concreteness over advice: the tool hands back specific artifacts — a node
listing, a validation digest, or edited workflow JSON — never generic tips.
"""
import io
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import discord

logger = logging.getLogger('DiscordBot')

LARGE_OUTPUT_CHARS = 1800
TRUNCATED_PREVIEW_CHARS = 1500
URL_FETCH_TIMEOUT_SECONDS = 20

# ========== Tool Definitions (Anthropic format) ==========

COMFY_WORKFLOW_TOOL = {
    "name": "comfy_workflow",
    "description": (
        "Inspect, validate, or edit a ComfyUI workflow. Pass raw workflow JSON "
        "(API or UI format) or an http(s) URL to a workflow JSON as source. "
        "mode=describe returns a node-by-node summary with widget values; "
        "mode=validate returns a validation report digest; mode=edit applies "
        "structured edit_ops (e.g. {\"op\":\"set_widget\",\"node\":<index-or-title>,"
        "\"widget\":<name-or-index>,\"value\":...} or {\"op\":\"remove_node\","
        "\"node\":<index-or-title>}), validates the result, and returns the edited "
        "API-format JSON. Large edited JSON is posted as a .json file attachment "
        "to thread_id when provided, otherwise returned as a truncated preview."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Raw workflow JSON string (API or UI format), or an http(s) URL pointing at one.",
            },
            "mode": {
                "type": "string",
                "enum": ["describe", "validate", "edit"],
                "description": "describe = summarize nodes/widgets; validate = run the validator; edit = apply edit_ops and return edited JSON.",
            },
            "edit_ops": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Required for mode=edit. Supported ops: set_widget {node, widget, value}, remove_node {node}.",
            },
            "thread_id": {
                "type": "integer",
                "description": "Optional Discord thread/channel ID. When the edited JSON exceeds chat limits it is posted there as a file attachment instead.",
            },
        },
        "required": ["source", "mode"],
    },
}

TOOLS = [COMFY_WORKFLOW_TOOL]


# ========== Helpers ==========

def _load_vibecompy():
    """Lazy-import the pieces of vibecompy this tool needs.

    Raises ImportError when the package is missing so callers can degrade cleanly.
    """
    from vibecompy.ingest.normalize import _named_import

    return _named_import


async def _fetch_workflow_json(url: str) -> Dict[str, Any]:
    """Fetch a workflow JSON document over http(s)."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=URL_FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise ValueError(f"Fetching {url} returned HTTP {response.status}")
            return await response.json()


def _load_raw_source(source: str):
    """Parse the source parameter into a raw workflow dict."""
    stripped = str(source).strip()
    lowered = stripped.lower()
    if lowered.startswith('http://') or lowered.startswith('https://'):
        return None, stripped  # caller fetches asynchronously
    raw = json.loads(stripped)
    if not isinstance(raw, dict):
        raise ValueError("Workflow JSON must decode to an object")
    return raw, None


def _scalar_params(node: Any) -> List[Tuple[str, Any]]:
    """Ordered (name, value) pairs of non-link parameters on a VibeNode."""
    pairs: List[Tuple[str, Any]] = []
    for source in (getattr(node, 'widgets', None) or {}, getattr(node, 'inputs', None) or {}):
        for key, value in source.items():
            if isinstance(value, (list, tuple)):
                continue  # link-shaped value, not a widget
            pairs.append((key, value))
    return pairs


def _short_value(value: Any, max_chars: int = 80) -> str:
    text = repr(value)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + '...'
    return text


def _node_titles(node: Any) -> List[str]:
    """Candidate human titles for a node (metadata-carried UI title variants)."""
    metadata = getattr(node, 'metadata', None) or {}
    titles: List[str] = []
    for candidate in (
        metadata.get('title'),
        metadata.get('_meta', {}).get('title') if isinstance(metadata.get('_meta'), dict) else None,
        metadata.get('_ui', {}).get('title') if isinstance(metadata.get('_ui'), dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            titles.append(candidate.strip())
    return titles


def _resolve_node(workflow: Any, ref: Any) -> Tuple[str, Any]:
    """Resolve an index-or-title/id/class_type reference to (node_id, node)."""
    nodes = list(getattr(workflow, 'nodes', {}).values())
    if not nodes:
        raise ValueError("Workflow has no nodes")

    if isinstance(ref, bool):
        raise ValueError(f"Cannot resolve node reference: {ref!r}")
    if isinstance(ref, int):
        try:
            node = nodes[ref]
        except IndexError:
            raise ValueError(f"Node index {ref} out of range (workflow has {len(nodes)} nodes)")
        return node.id, node
    if isinstance(ref, float) and ref.is_integer():
        return _resolve_node(workflow, int(ref))

    text = str(ref).strip()
    lowered = text.casefold()
    for node in nodes:
        if node.id == text or node.class_type == text:
            return node.id, node
        if any(title.casefold() == lowered for title in _node_titles(node)):
            return node.id, node
    # Numeric strings are treated as indexes only when no id/class_type/title matched.
    if text.isdigit():
        return _resolve_node(workflow, int(text))

    for node in nodes:
        if lowered and (lowered in node.class_type.casefold()):
            return node.id, node
        for title in _node_titles(node):
            if lowered in title.casefold():
                return node.id, node
    raise ValueError(
        f"No node matches {text!r}. Known nodes: "
        + ', '.join(f"{i}:{n.class_type}" for i, n in enumerate(nodes))
    )


def _apply_set_widget(node: Any, widget_ref: Any, value: Any) -> str:
    """Set a widget value on a node by name or position. Returns the key written."""
    widgets = getattr(node, 'widgets', None) or {}
    inputs = getattr(node, 'inputs', None) or {}

    if isinstance(widget_ref, bool):
        raise ValueError(f"Cannot resolve widget reference on {node.class_type}: {widget_ref!r}")
    if isinstance(widget_ref, int):
        keys = list(widgets.keys()) + list(inputs.keys())
        try:
            key = keys[widget_ref]
        except IndexError:
            raise ValueError(
                f"Widget index {widget_ref} out of range on {node.class_type} "
                f"(has {len(keys)} widgets/inputs)"
            )
    elif isinstance(widget_ref, float) and widget_ref.is_integer():
        return _apply_set_widget(node, int(widget_ref), value)
    else:
        key = str(widget_ref)

    if key in widgets:
        widgets[key] = value
    elif key in inputs:
        inputs[key] = value
    else:
        available = [k for k, _ in _scalar_params(node)]
        raise ValueError(
            f"{node.class_type} (id={node.id}) has no widget named {key!r}. Available: {available}"
        )
    return key


def _apply_edit_op(workflow: Any, op: Dict[str, Any]) -> str:
    """Apply one structured edit op; returns a human-readable confirmation line."""
    if not isinstance(op, dict):
        raise ValueError(f"edit_ops entries must be objects, got: {op!r}")
    kind = op.get('op')
    if kind == 'set_widget':
        for field in ('node', 'widget'):
            if op.get(field) is None:
                raise ValueError(f"set_widget requires 'node' and 'widget' fields, got: {op!r}")
        node_id, node = _resolve_node(workflow, op['node'])
        key = _apply_set_widget(node, op['widget'], op.get('value'))
        return f"set {key} on {node.class_type} (id={node_id}) to {_short_value(op.get('value'))}"
    if kind == 'remove_node':
        if op.get('node') is None:
            raise ValueError(f"remove_node requires a 'node' field, got: {op!r}")
        node_id, node = _resolve_node(workflow, op['node'])
        workflow.remove_node(node_id)
        return f"removed {node.class_type} (id={node_id})"
    raise ValueError(
        f"Unsupported edit op {kind!r}. Supported ops: set_widget, remove_node"
    )


def _issues_digest(report: Any) -> List[Dict[str, Any]]:
    issues = getattr(report, 'issues', None) or []
    digest: List[Dict[str, Any]] = []
    for issue in issues:
        digest.append({
            'code': getattr(issue, 'code', 'unknown'),
            'severity': getattr(issue, 'severity', 'error'),
            'message': getattr(issue, 'message', str(issue)),
        })
    return digest


def _validation_lines(report: Any) -> List[str]:
    digest = _issues_digest(report)
    if not digest:
        return ["Validation: OK, no issues."]
    errors = [i for i in digest if i['severity'] == 'error']
    warnings = [i for i in digest if i['severity'] != 'error']
    lines = [f"Validation: {'FAILED' if not getattr(report, 'ok', False) else 'OK'} "
             f"({len(errors)} errors, {len(warnings)} warnings)"]
    for issue in digest:
        prefix = 'error' if issue['severity'] == 'error' else 'warning'
        lines.append(f"- [{prefix}] {issue['code']}: {issue['message']}")
    return lines


def _describe_summary(workflow: Any, report: Any) -> str:
    lines = [f"Workflow '{getattr(workflow, 'id', 'workflow')}' — {len(workflow.nodes)} nodes:"]
    for index, node in enumerate(workflow.nodes.values()):
        params = ', '.join(f"{key}={_short_value(value)}" for key, value in _scalar_params(node))
        suffix = f": {params}" if params else ""
        lines.append(f"- [{index}] {node.class_type} (id={node.id}){suffix}")
    lines.extend(_validation_lines(report))
    return '\n'.join(lines)


async def execute_comfy_workflow(tool_input: Dict[str, Any], bot: Optional[Any] = None) -> Dict[str, Any]:
    """Execute the comfy_workflow tool: describe / validate / edit a workflow."""
    source = tool_input.get('source')
    if not isinstance(source, str) or not source.strip():
        return {"success": False, "error": "source is required: workflow JSON string or http(s) URL"}

    mode = tool_input.get('mode')
    if mode not in ('describe', 'validate', 'edit'):
        return {"success": False, "error": "mode must be one of: describe, validate, edit"}

    edit_ops = tool_input.get('edit_ops')
    if mode == 'edit':
        if not isinstance(edit_ops, list) or not edit_ops:
            return {"success": False, "error": "edit mode requires a non-empty edit_ops list"}

    try:
        named_import = _load_vibecompy()
    except ImportError:
        return {"success": False, "error": "vibecomfy package not installed on this host"}

    try:
        raw, url = _load_raw_source(source)
        if url is not None:
            raw = await _fetch_workflow_json(url)
        workflow = named_import(raw)

        applied: List[str] = []
        if mode == 'edit':
            for op in edit_ops:
                applied.append(_apply_edit_op(workflow, op))

        report = workflow.validate()

        if mode == 'describe':
            return {
                "success": True,
                "formatted": _describe_summary(workflow, report),
                "node_count": len(workflow.nodes),
                "issues": _issues_digest(report),
            }

        if mode == 'validate':
            return {
                "success": True,
                "ok": bool(getattr(report, 'ok', False)),
                "formatted": '\n'.join(_validation_lines(report)),
                "issues": _issues_digest(report),
            }

        # mode == 'edit'
        workflow_json = json.dumps(workflow.export_to_json(format="api"))
        summary = (
            f"{len(applied)} edit(s) applied: " + '; '.join(applied)
            + '. ' + '\n'.join(_validation_lines(report))
        )
        if len(workflow_json) <= LARGE_OUTPUT_CHARS:
            return {"success": True, "workflow_json": workflow_json, "summary": summary}

        posted = False
        thread_id = tool_input.get('thread_id')
        if thread_id is not None and bot is not None:
            posted = await _post_workflow_file(bot, thread_id, workflow_json)
        if posted:
            return {"success": True, "posted_as_file": True, "summary": summary}

        preview = workflow_json[:TRUNCATED_PREVIEW_CHARS]
        note = (
            "Edited JSON is too large for chat"
            + (" and could not be posted as a file" if thread_id is not None else "")
            + "; showing truncated preview."
        )
        return {
            "success": True,
            "truncated": True,
            "preview": preview,
            "note": note,
            "summary": summary,
        }
    except Exception as exc:
        logger.exception("comfy_workflow failed (mode=%s)", mode)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


async def _post_workflow_file(bot: Any, thread_id: Any, workflow_json: str) -> bool:
    """Post the edited workflow as a .json attachment. Returns True on success."""
    try:
        channel = bot.get_channel(int(thread_id))
        if channel is None:
            logger.warning("comfy_workflow: no channel for thread_id %s", thread_id)
            return False
        filename = f"edited_workflow_{int(time.time())}.json"
        file = discord.File(io.BytesIO(workflow_json.encode('utf-8')), filename=filename)
        await channel.send(file=file)
        return True
    except Exception:
        logger.exception("comfy_workflow: failed to post edited workflow file")
        return False
