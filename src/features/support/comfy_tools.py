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
import re
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import discord

logger = logging.getLogger('DiscordBot')
URL_FETCH_TIMEOUT_SECONDS = 20
FETCH_MAX_ATTEMPTS = 3
LARGE_OUTPUT_CHARS = 1800
TRUNCATED_PREVIEW_CHARS = 1500
PREVIEW_CHARS = 800

import asyncio

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
                "description": "Required for mode=edit. Supported ops: set_widget {node, widget, value}; remove_node {node}; add_node {class_type, node_id?, inputs?} — scalar inputs become widgets, link pairs like [\"21\", 0] become edges; connect {from: \"<node>.<output>\", to: \"<node>.<input>\"}; disconnect {on: \"<node>.<input>\"}.",
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
    last_err: Optional[str] = None
    # Transient CDN failures (rate-limit pages, partial bodies) have hit this
    # path before; retry a few times with backoff before giving up.
    for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # allow_redirects=False so a benign-looking URL cannot bounce
                # to an internal or non-allowlisted host.
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f"Fetching {url} returned HTTP {response.status}")
                    payload = await response.content.read(MAX_WORKFLOW_BYTES + 1)
            if len(payload) > MAX_WORKFLOW_BYTES:
                raise ValueError(
                    f"Workflow document at {url} exceeds the "
                    f"{MAX_WORKFLOW_BYTES // (1024 * 1024)}MB size limit"
                )
            text = payload.decode('utf-8')
            # Sanity check: CDN hiccup pages (HTML/plain-text error bodies)
            # arrive as HTTP 200. A workflow always starts with '{' or '['.
            stripped = text.lstrip()
            if not stripped or stripped[0] not in ('{', '['):
                last_err = "response was not JSON (CDN/transient error page?)"
                logger.warning(
                    "[Support] fetch attempt %d/%d returned non-JSON body "
                    "(%d bytes) — retrying", attempt, FETCH_MAX_ATTEMPTS,
                    len(payload),
                )
                continue
            return _lenient_json_loads(stripped)
        except ValueError as exc:
            last_err = str(exc)
            logger.warning(
                "[Support] fetch attempt %d/%d failed: %s",
                attempt, FETCH_MAX_ATTEMPTS, exc,
            )
            await asyncio.sleep(0.5 * attempt)
    raise ValueError(f"Workflow document at {url} could not be fetched after "
                     f"{FETCH_MAX_ATTEMPTS} attempts: {last_err}")


def _coerce_thread_id(value: Any) -> Optional[int]:
    """Best-effort int coercion of a thread id; None when absent/unparseable."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _lenient_json_loads(text: str) -> Dict[str, Any]:
    """Parse workflow JSON, tolerating common hand-edit mistakes.

    Fixes:
    - trailing commas before } or ] (e.g. {"a": 1,})
    - unquoted `id` values like \"id\": 105_rope  → \"id\": \"105_rope\"
    - bare node ids in arrays like [4, 105, 0, 105_rope, 0, "MODEL"]
      (same fix, quoted to \"105_rope\")
    Falls back to strict json.loads on success; re-raises the original
    error if lenient fixes do not help.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as orig:
        fixed = text
        # Quote bare id tokens that are not pure integers (e.g. 105_rope).
        def _quote_id(m):
            token = m.group(1)
            if token.isdigit():
                return m.group(0)
            return f'"id": "{token}"{m.group(2)}'
        fixed = re.sub(r'"id"\s*:\s*([A-Za-z0-9_]+)\s*([,}])', _quote_id, fixed)
        # Quote bare node ids that appear as values in arrays/links (e.g. 105_rope)
        # Only tokens containing underscore (to avoid quoting pure numbers) and
        # not already quoted (lookarounds ensure that).
        fixed = re.sub(r'(?<![\w"])([0-9]+_[A-Za-z0-9_]+)(?![\w"])', r'"\1"', fixed)
        # Remove trailing commas before } or ].
        fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            raise orig


def _load_raw_source(source: str):
    """Parse the source parameter into a raw workflow dict."""
    stripped = str(source).strip()
    lowered = stripped.lower()
    if lowered.startswith('http://') or lowered.startswith('https://'):
        return None, stripped  # caller fetches asynchronously
    raw = _lenient_json_loads(stripped)
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
    if kind == 'add_node':
        class_type = op.get('class_type')
        if not class_type:
            raise ValueError(f"add_node requires 'class_type', got: {op!r}")
        inputs = dict(op.get('inputs') or {})
        # API-format link pairs like ["21", 0] are illegal as literal widget
        # values (VibeEdge is the sole connectivity authority). Split them
        # out: scalars become node inputs at creation; link pairs become
        # connect calls on the live IR.
        links, scalar = {}, {}
        try:
            # Prefer VibeComfy's own authority on what counts as an API link.
            from vibecomfy._compile._graph import is_canonical_api_link
        except ImportError:
            is_canonical_api_link = None

        def _is_link_pair(value):
            if is_canonical_api_link is not None:
                return is_canonical_api_link(value)
            # Fallback shape check (fake-module tests / very old builds).
            return (isinstance(value, (list, tuple)) and len(value) == 2
                    and isinstance(value[0], (str, int))
                    and not isinstance(value[0], bool)
                    and isinstance(value[1], int)
                    and not isinstance(value[1], bool))

        for name, value in inputs.items():
            if _is_link_pair(value):
                links[name] = [str(value[0]), value[1]]
            else:
                scalar[name] = value
        new_node = workflow.add_node(class_type, _id=op.get('node_id'), **scalar)
        for input_name, (src, out_idx) in links.items():
            workflow.connect(f"{src}.{out_idx}", f"{new_node.id}.{input_name}")
        refs = ", ".join(f"{k} <- {v[0]}.{v[1]}" for k, v in sorted(links.items()))
        return (f"added {class_type} (id={new_node.id})"
                + (f" wired {refs}" if refs else ""))
    if kind == 'connect':
        from_ref, to_ref = op.get('from'), op.get('to')
        if not from_ref or not to_ref:
            raise ValueError(
                "connect requires 'from' (<node>.<output>) and "
                "'to' (<node>.<input>), e.g. from='108.0' to='900.latent'"
            )
        workflow.connect(str(from_ref), str(to_ref))
        return f"connected {from_ref} -> {to_ref}"
    if kind == 'disconnect':
        target = op.get('on')
        if not target:
            raise ValueError("disconnect requires 'on': '<node>.<input>'")
        removed = workflow.disconnect(str(target))
        if not removed:
            raise ValueError(f"no existing edge into {target!r}")
        return f"disconnected edge into {target}"
    raise ValueError(
        f"Unsupported edit op {kind!r}. Supported ops: set_widget, remove_node, "
        + "add_node, connect, disconnect"
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
                    + " Nothing sent to the member yet. When you're done and satisfied with all changes, call mode='deliver' to attach the finished workflow as a downloadable .json — or use send_file_to_thread for any other artifact you want to share alongside it."
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
        channel = None
        try:
            channel = bot.get_channel(int(thread_id))
        except Exception:
            channel = None
        if channel is None:
            fetch = getattr(bot, "fetch_channel", None)
            if fetch is not None:
                try:
                    channel = await fetch(int(thread_id))
                except Exception:
                    channel = None
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


# ========== send_file_to_thread: general-purpose attachment tool ==========

SEND_FILE_TOOL = {
    "name": "send_file_to_thread",
    "description": (
        "Attach a file to the current support thread so the member can "
        "download it. Use this whenever you have finished content the member "
        "should keep — an edited workflow, a reference workflow fetched from "
        "the community, a text walkthrough, a patch file — anything worth a "
        "downloadable artifact rather than an inline code block. Pass raw "
        "text/JSON as content (it becomes the file body), or pass source_url "
        "to stream a remote file straight through. Call it once per artifact; "
        "the file posts to the thread and you get a confirmation summary to "
        "reference in your reply."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The file body as text (workflow JSON, notes, a patch script). Required unless source_url is given.",
            },
            "filename": {
                "type": "string",
                "description": "Name for the attached file. Defaults to a timestamped name based on kind.",
            },
            "kind": {
                "type": "string",
                "enum": ["workflow", "notes", "patch", "other"],
                "description": "What the file is — controls default filename and how the confirmation reads.",
            },
            "source_url": {
                "type": "string",
                "description": "Optional https URL to fetch instead of using `content` (e.g. attach a community workflow you found). Same host allowlist as comfy_workflow sources.",
            },
            "note_for_member": {
                "type": "string",
                "description": "One line to show next to the confirmation telling the member what this file is.",
            },
        },
        "required": ["filename"],
    },
}


async def execute_send_file_to_thread(tool_input, bot=None):
    """Post arbitrary text (or a fetched URL) as a downloadable Discord file.

    Never raises. Returns {success, filename, ...} on completion or
    {success: False, error} on failure.
    """
    import io
    import time as _time

    filename = str(tool_input.get("filename") or "").strip()
    content = tool_input.get("content")
    source_url = tool_input.get("source_url")
    note = tool_input.get("note_for_member") or ""
    kind = tool_input.get("kind") or "other"
    thread_id = _coerce_thread_id(tool_input.get("thread_id"))

    if not filename:
        base = {"workflow": "edited_workflow", "notes": "support_notes",
                "patch": "patch"}.get(kind, "attachment")
        stamp = int(_time.time())
        ext = ".json" if kind in ("workflow",) else ".txt"
        filename = f"{base}_{stamp}{ext}"

    # Fetch from URL when no inline content.
    if content is None and source_url:
        try:
            import aiohttp
            parsed = urlparse(source_url)
            if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_WORKFLOW_HOSTS:
                return {"success": False,
                        "error": f"source_url host must be one of: {', '.join(sorted(ALLOWED_WORKFLOW_HOSTS))}"}
            timeout = aiohttp.ClientTimeout(total=URL_FETCH_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source_url, allow_redirects=False) as response:
                    if response.status != 200:
                        return {"success": False,
                                "error": f"Fetched {url_summary(source_url)} -> HTTP {response.status}"}
                    payload = await response.content.read(MAX_WORKFLOW_BYTES + 1)
            if len(payload) > MAX_WORKFLOW_BYTES:
                return {"success": False, "error": "Fetched file exceeds size limit"}
            content = payload.decode('utf-8')
        except aiohttp.ClientError as exc:
            return {"success": False, "error": f"Fetch failed: {exc}"}
        except UnicodeDecodeError:
            return {"success": False, "error": "Fetched file is not UTF-8 text"}

    if not isinstance(content, str) or not content.strip():
        return {"success": False,
                "error": "Nothing to attach: provide `content` text or a fetchable `source_url`"}

    # Post the file
    posted = False
    if bot is not None and thread_id is not None:
        try:
            channel = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if channel is None:
                return {"success": False, "error": f"No channel for thread {thread_id}"}
            fp = io.BytesIO(content.encode('utf-8'))
            file = discord.File(fp, filename=filename)
            await channel.send(file=file)
            posted = True
        except Exception as exc:
            logger.exception("[Support] send_file_to_thread post failed for %s", filename)
            return {"success": False, "error": f"Failed to post file: {exc}"}

    preview = content[:800]
    result = {
        "success": True,
        "posted_as_file": posted,
        "filename": filename,
        "summary": (
            f"Attached `{filename}` ({len(content)} chars)"
            + (f" with note: {note}" if note else "")
            + ("" if posted else " — could not post (missing bot/thread context); shown inline below")
        ),
        "preview": preview,
    }
    return result


def url_summary(url):
    return url[:60] + ('...' if len(url) > 60 else '')
