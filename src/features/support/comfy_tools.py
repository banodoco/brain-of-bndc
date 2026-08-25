"""ComfyUI workflow tool for the support agent.

Lets the LLM inspect, validate, and edit ComfyUI workflows (API or UI format)
by delegating to the vibecompy package when it is available on this host.
Concreteness over advice: the tool hands back specific artifacts — a node
listing, a validation digest, or edited workflow JSON — never generic tips.
"""
import copy
import io
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import discord

logger = logging.getLogger('DiscordBot')

PREVIEW_CHARS = 800
URL_FETCH_TIMEOUT_SECONDS = 20

# SSRF guard for remote workflow sources: https only, fixed host allowlist,
# no redirects followed, hard size cap.
ALLOWED_WORKFLOW_HOSTS = frozenset({
    'cdn.discordapp.com',
    'media.discordapp.net',
    'raw.githubusercontent.com',
    'github.com',
    'comfyworkflows.com',
    'civitai.com',
})
MAX_WORKFLOW_BYTES = 5 * 1024 * 1024

# ========== Tool Definitions (Anthropic format) ==========

COMFY_WORKFLOW_TOOL = {
    "name": "comfy_workflow",
    "description": (
        "Inspect, validate, or iteratively edit a ComfyUI workflow. Pass raw "
        "workflow JSON (API or UI format) or an https URL from an allowed host "
        "(cdn.discordapp.com, media.discordapp.net, raw.githubusercontent.com, "
        "github.com, comfyworkflows.com, civitai.com) as source. "
        "mode=describe returns a node-by-node summary with widget values; "
        "mode=validate returns a validation report digest; mode=edit applies "
        "structured edit_ops (e.g. {\"op\":\"set_widget\",\"node\":<index-or-title>,"
        "\"widget\":<name-or-index>,\"value\":...} or {\"op\":\"remove_node\","
        "\"node\":<index-or-title>}) to a per-thread STAGED working copy — edits "
        "are NOT sent to the member. Repeat mode=edit (omitting source) to stack "
        "more changes on the staged copy. When completely done, call mode=deliver "
        "ONCE: it attaches the finished workflow to the thread as a downloadable "
        ".json file for the member."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Raw workflow JSON string (API or UI format), or an https URL pointing at one (only allowed hosts: cdn.discordapp.com, media.discordapp.net, raw.githubusercontent.com, github.com, comfyworkflows.com, civitai.com). Required for describe/validate and for the first edit of a thread; omit on follow-up edits to keep modifying the staged copy.",
            },
            "mode": {
                "type": "string",
                "enum": ["describe", "validate", "edit", "deliver"],
                "description": "describe = summarize nodes/widgets; validate = run the validator; edit = apply edit_ops to the staged copy (nothing is sent); deliver = attach the staged result to the thread as a downloadable .json file (call once, when done).",
            },
            "edit_ops": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Required for mode=edit. Supported ops: set_widget {node, widget, value}, remove_node {node}.",
            },
            "thread_id": {
                "type": "integer",
                "description": "Discord thread/channel ID used to key the staged working copy and as the delivery target for mode=deliver.",
            },
        },
        "required": ["mode"],
    },
}

TOOLS = [COMFY_WORKFLOW_TOOL]


# ========== Helpers ==========

def _load_vibecompy():
    """Lazy-import the pieces of vibecompy this tool needs.

    Raises ImportError when the package is missing so callers can degrade cleanly.
    """
    from vibecomfy.ingest.normalize import _named_import

    return _named_import


async def _fetch_workflow_json(url: str) -> Dict[str, Any]:
    """Fetch a workflow JSON document over https from an allowlisted host."""
    import aiohttp
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise ValueError("Only https:// URLs are accepted as workflow sources")
    if parsed.hostname not in ALLOWED_WORKFLOW_HOSTS:
        raise ValueError(
            f"Host {parsed.hostname!r} is not allowed for workflow sources. "
            f"Allowed hosts: {', '.join(sorted(ALLOWED_WORKFLOW_HOSTS))}"
        )

    timeout = aiohttp.ClientTimeout(total=URL_FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # allow_redirects=False so a benign-looking URL cannot bounce to an
        # internal or non-allowlisted host.
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Fetching {url} returned HTTP {response.status}")
            payload = await response.content.read(MAX_WORKFLOW_BYTES + 1)
    if len(payload) > MAX_WORKFLOW_BYTES:
        raise ValueError(
            f"Workflow document at {url} exceeds the "
            f"{MAX_WORKFLOW_BYTES // (1024 * 1024)}MB size limit"
        )
    try:
        return json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Workflow document at {url} is not valid JSON: {exc}") from exc


def _coerce_thread_id(value: Any) -> Optional[int]:
    """Best-effort int coercion of a thread id; None when absent/unparseable."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


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


# Per-thread staging for iterative edits: mode=edit stores the working IR
# here; mode=deliver attaches it. Keyed by thread id, overwritten whenever
# a new source is ingested — intentionally in-memory and ephemeral.
_STAGED: Dict[int, Dict[str, Any]] = {}


async def execute_comfy_workflow(tool_input: Dict[str, Any], bot: Optional[Any] = None) -> Dict[str, Any]:
    """Execute the comfy_workflow tool: describe / validate / edit / deliver."""
    mode = tool_input.get('mode')
    if mode not in ('describe', 'validate', 'edit', 'deliver'):
        return {"success": False, "error": "mode must be one of: describe, validate, edit, deliver"}

    thread_id = _coerce_thread_id(tool_input.get('thread_id'))
    if mode in ('edit', 'deliver') and thread_id is None:
        return {"success": False, "error": "thread_id is required for edit/deliver"}

    source = tool_input.get('source')

    if mode == 'deliver':
        staged = _STAGED.get(thread_id)
        if not staged:
            return {"success": False,
                    "error": "nothing staged to deliver: run edit mode first (it stages automatically)"}
        workflow_json = json.dumps(staged['workflow'].export_to_json(format="api"))
        summary = (
            f"Delivered {len(staged['applied'])} staged edit(s): " + '; '.join(staged['applied'])
            + '. ' + '\n'.join(_validation_lines(staged['workflow'].validate()))
        )
        nudge = (
            "REMINDER for your reply: the fixed workflow is attached above as "
            "edited_workflow_<timestamp>.json — tell the member to download it and "
            "open it in ComfyUI (or drag it onto the canvas). If they want more "
            "changes, they can attach the .json again in their next message."
        )
        posted = False
        if bot is not None:
            posted = await _post_workflow_file(bot, thread_id, workflow_json)
        if not posted:
            return {"success": False, "error": "could not post the file attachment to this thread"}
        return {
            "success": True,
            "posted_as_file": True,
            "summary": summary + ' ' + nudge,
            "preview": workflow_json[:PREVIEW_CHARS],
        }

    if mode == 'edit':
        edit_ops = tool_input.get('edit_ops')
        if not isinstance(edit_ops, list) or not edit_ops:
            return {"success": False, "error": "edit mode requires a non-empty edit_ops list"}
    elif not isinstance(source, str) or not source.strip():
        return {"success": False, "error": "source is required: workflow JSON string or http(s) URL"}

    try:
        named_import = _load_vibecompy()
    except ImportError:
        return {"success": False, "error": "vibecomfy package not installed on this host"}

    try:
        staged = _STAGED.get(thread_id)

        # Ingest priority: explicit source > staged working copy.
        if isinstance(source, str) and source.strip():
            raw, url = _load_raw_source(source)
            if url is not None:
                raw = await _fetch_workflow_json(url)
            workflow = named_import(raw)
            applied: List[str] = []
        elif mode == 'edit' and staged:
            workflow = copy.deepcopy(staged['workflow'])
            applied = list(staged['applied'])
        else:
            return {"success": False,
                    "error": "source is required (nothing staged yet on this thread)"}

        if mode == 'edit':
            for op in edit_ops:
                applied.append(_apply_edit_op(workflow, op))

        report = workflow.validate()

        if mode == 'edit':
            _STAGED[thread_id] = {'workflow': workflow, 'applied': applied}
            preview_json = json.dumps(workflow.export_to_json(format="api"))
            return {
                "success": True,
                "staged": True,
                "summary": (
                    f"{len(applied)} total edit(s) staged (latest: {applied[-1]})"
                    + '. ' + '\n'.join(_validation_lines(report))
                    + " Nothing sent to the member yet — call mode='deliver' when done."
                ),
                "preview": preview_json[:PREVIEW_CHARS],
            }

        if mode == 'describe':
            return {
                "success": True,
                "formatted": _describe_summary(workflow, report),
                "node_count": len(workflow.nodes),
                "issues": _issues_digest(report),
            }

        # mode == 'validate'
        return {
            "success": True,
            "ok": bool(getattr(report, 'ok', False)),
            "formatted": '\n'.join(_validation_lines(report)),
            "issues": _issues_digest(report),
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
