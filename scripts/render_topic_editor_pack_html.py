#!/usr/bin/env python
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _pack_dirs(path: Path) -> List[Path]:
    if (path / "summary.json").exists() and (path / "final_action.json").exists():
        return [path]
    packs = [
        child for child in sorted(path.iterdir())
        if child.is_dir() and (child / "summary.json").exists() and (child / "final_action.json").exists()
    ]
    return packs


def _status_class(status: Any) -> str:
    text = str(status or "").lower()
    if text in {"pass", "completed", "submitted", "valid"}:
        return "ok"
    if text in {"fail", "failed", "blocked_for_submit"}:
        return "bad"
    return "warn"


def _linkify_discord_refs(text: str) -> str:
    escaped = html.escape(str(text or ""))

    def repl_inline(match: re.Match[str]) -> str:
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        return f'<a href="{url}">[{label}]</a>'

    escaped = re.sub(r"\[(\d+)\]\((https://discord\.com/channels/[^)]+)\)", repl_inline, escaped)
    escaped = re.sub(
        r"&lt;(https://discord\.com/channels/[^&]+)&gt;",
        lambda m: f'<a href="{html.escape(m.group(1), quote=True)}">{html.escape(m.group(1))}</a>',
        escaped,
    )
    return escaped.replace("\n", "<br>")


def _render_discord_markdown(text: str) -> str:
    rendered = _linkify_discord_refs(text)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*<][^*]*?)\*(?!\*)", r"<em>\1</em>", rendered)
    blocks = []
    for raw_line in rendered.split("<br>"):
        line = raw_line.strip()
        if not line:
            blocks.append('<div class="md-gap"></div>')
        elif line.startswith("### "):
            blocks.append(f'<div class="md-h3">{line[4:]}</div>')
        elif line.startswith("## "):
            blocks.append(f'<div class="md-h2">{line[3:]}</div>')
        elif line.startswith("# "):
            blocks.append(f'<div class="md-h1">{line[2:]}</div>')
        else:
            blocks.append(f'<div class="md-line">{line}</div>')
    return "\n".join(blocks)


def _render_preview(preview: List[Dict[str, Any]]) -> str:
    if not preview:
        return '<div class="empty">No preview units captured.</div>'
    rows = []
    for index, unit in enumerate(preview, start=1):
        unit_type = unit.get("type") or unit.get("kind") or "text"
        if unit_type == "media":
            src = unit.get("source_url") or unit.get("fallback_url") or ""
            desc = unit.get("description") or unit.get("media_id") or "media"
            media_html = ""
            if src and re.search(r"\.(png|jpe?g|gif|webp)(\?|$)", str(src), re.I):
                media_html = f'<img src="{html.escape(str(src), quote=True)}" alt="{html.escape(str(desc), quote=True)}">'
            elif src and re.search(r"\.(mp4|mov|webm|m4v)(\?|$)", str(src), re.I):
                media_html = (
                    f'<video controls preload="metadata" src="{html.escape(str(src), quote=True)}">'
                    f'<a href="{html.escape(str(src), quote=True)}">{html.escape(str(src))}</a>'
                    '</video>'
                )
            else:
                media_html = f'<a href="{html.escape(str(src), quote=True)}">{html.escape(str(src or desc))}</a>'
            rows.append(
                f'<div class="message media"><div class="avatar">{index}</div><div class="bubble">'
                f'<div class="meta">media · {html.escape(str(unit.get("media_id") or ""))}</div>{media_html}'
                f'<div class="caption">{html.escape(str(desc))}</div></div></div>'
            )
        else:
            content = unit.get("content") or ""
            rows.append(
                f'<div class="message"><div class="avatar">{index}</div><div class="bubble">'
                f'{_render_discord_markdown(content)}</div></div>'
            )
    return "\n".join(rows)


def _render_sources(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty">No source messages captured.</div>'
    out = []
    for row in rows:
        author = row.get("author_context_snapshot") or {}
        name = row.get("author_name") or author.get("server_nick") or author.get("username") or row.get("author_id") or "unknown"
        out.append(
            '<div class="source">'
            f'<div class="source-head">{html.escape(str(name))} · {html.escape(str(row.get("created_at") or ""))} · #{html.escape(str(row.get("channel_name") or row.get("channel_id") or ""))}</div>'
            f'<div>{html.escape(str(row.get("content") or ""))}</div>'
            '</div>'
        )
    return "\n".join(out)


def _render_issues(validation: Dict[str, Any], assessment: Dict[str, Any]) -> str:
    lines = []
    for kind in ("errors", "warnings"):
        for issue in validation.get(kind) or []:
            lines.append(
                f'<li class="{_status_class("failed" if kind == "errors" else "warn")}">'
                f'<strong>{html.escape(kind[:-1])}</strong> {html.escape(str(issue.get("path") or ""))}: '
                f'{html.escape(str(issue.get("message") or ""))}</li>'
            )
    for check, data in sorted((assessment.get("checks") or {}).items()):
        if data.get("status") != "pass":
            details = "; ".join(str(item) for item in data.get("details") or [])
            lines.append(
                f'<li class="bad"><strong>{html.escape(check)}</strong>: {html.escape(details)}</li>'
            )
    if not lines:
        return '<div class="empty">No blocking issues in captured validation/assessment.</div>'
    return "<ul>" + "\n".join(lines) + "</ul>"


def _render_pack(pack: Path) -> str:
    summary = _read_json(pack / "summary.json", {})
    final_action = _read_json(pack / "final_action.json", {})
    assessment = _read_json(pack / "assessment.json", {})
    validation = _read_json(pack / "validation.json", {})
    preview = _read_json(pack / "preview.json", [])
    draft = _read_json(pack / "draft.json", {})
    sources = _read_json(pack / "source_messages.json", [])
    rendered = (pack / "rendered_messages.txt").read_text() if (pack / "rendered_messages.txt").exists() else ""
    scenario = summary.get("scenario") or pack.name
    return f"""
    <section class="pack" id="{html.escape(pack.name)}">
      <header class="pack-header">
        <div>
          <h2>{html.escape(str(scenario))}</h2>
          <div class="path">{html.escape(str(pack))}</div>
        </div>
        <div class="badges">
          <span class="badge {_status_class(summary.get("status"))}">run {html.escape(str(summary.get("status") or "unknown"))}</span>
          <span class="badge {_status_class(assessment.get("status"))}">assessment {html.escape(str(assessment.get("status") or "missing"))}</span>
          <span class="badge {_status_class(summary.get("draft_status"))}">draft {html.escape(str(summary.get("draft_status") or "none"))}</span>
          <span class="badge">actor {html.escape(str(summary.get("actor_kind") or ("mock" if summary.get("mock_actor") else "unknown")))}</span>
        </div>
      </header>
      <div class="grid">
        <main class="discord">
          <div class="channel-title"># topic-editor-preview</div>
          {_render_preview(preview)}
        </main>
        <aside>
          <div class="panel">
            <h3>Final Action</h3>
            <dl>
              <dt>submitted</dt><dd>{html.escape(str(final_action.get("submitted")))}</dd>
              <dt>forced close</dt><dd>{html.escape(str(final_action.get("forced_close")))}</dd>
              <dt>reason</dt><dd>{html.escape(str(final_action.get("forced_close_reason") or ""))}</dd>
              <dt>tools</dt><dd>{html.escape(str(summary.get("tool_call_count") or 0))}</dd>
            </dl>
          </div>
          <div class="panel">
            <h3>Issues</h3>
            {_render_issues(validation if isinstance(validation, dict) else {}, assessment if isinstance(assessment, dict) else {})}
          </div>
          <div class="panel">
            <h3>Draft</h3>
            <div class="draft-title">{html.escape(str(draft.get("headline") or "No headline"))}</div>
            <div class="draft-dek">{html.escape(str(draft.get("dek") or ""))}</div>
            <dl class="draft-meta">
              <dt>topic</dt><dd>{html.escape(str(draft.get("topic_key") or ""))}</dd>
              <dt>cards</dt><dd>{html.escape(str(len(draft.get("cards") or [])))}</dd>
            </dl>
          </div>
        </aside>
      </div>
      <details>
        <summary>Source Messages</summary>
        {_render_sources(sources if isinstance(sources, list) else [])}
      </details>
      <details>
        <summary>Rendered Text</summary>
        <pre>{html.escape(rendered)}</pre>
      </details>
    </section>
    """


def render_html(input_path: Path, output_path: Path) -> Path:
    packs = _pack_dirs(input_path)
    if not packs:
        raise SystemExit(f"No evidence packs found under {input_path}")
    body = "\n".join(_render_pack(pack) for pack in packs)
    nav = "\n".join(f'<a href="#{html.escape(pack.name)}">{html.escape(pack.name)}</a>' for pack in packs)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Topic Editor Replay Preview</title>
  <style>
    :root {{ color-scheme: dark; --bg:#111214; --panel:#1e1f22; --soft:#2b2d31; --text:#dbdee1; --muted:#949ba4; --line:#313338; --green:#3ba55d; --red:#ed4245; --yellow:#faa61a; --link:#00a8fc; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    nav {{ position:sticky; top:0; z-index:3; background:#0b0c0e; border-bottom:1px solid var(--line); padding:10px 18px; display:flex; gap:10px; flex-wrap:wrap; }}
    nav a {{ color:var(--link); text-decoration:none; padding:4px 8px; background:var(--panel); border-radius:4px; }}
    .pack {{ padding:22px; border-bottom:1px solid var(--line); }}
    .pack-header {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }}
    h1 {{ margin:18px 22px 0; font-size:22px; }}
    h2 {{ margin:0; font-size:20px; }}
    h3 {{ margin:0 0 10px; font-size:14px; color:#fff; }}
    .path,.meta,.caption,.draft-dek,dt {{ color:var(--muted); }}
    .badges {{ display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }}
    .badge {{ display:inline-flex; padding:3px 8px; border-radius:4px; background:var(--soft); color:var(--text); font-size:12px; }}
    .badge.ok,.ok {{ color:#b6f4c3; }}
    .badge.bad,.bad {{ color:#ffd0d0; }}
    .badge.warn,.warn {{ color:#ffe3ad; }}
    .grid {{ display:grid; grid-template-columns:minmax(360px, 1fr) 340px; gap:18px; align-items:start; }}
    .discord,.panel,details {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
    .channel-title {{ padding:12px 16px; border-bottom:1px solid var(--line); font-weight:700; color:#fff; }}
    .message {{ display:grid; grid-template-columns:42px 1fr; gap:12px; padding:14px 16px; }}
    .message + .message {{ border-top:1px solid rgba(255,255,255,.04); }}
    .avatar {{ width:38px; height:38px; border-radius:50%; background:#5865f2; display:flex; align-items:center; justify-content:center; font-weight:700; color:#fff; }}
    .bubble {{ min-width:0; white-space:normal; overflow-wrap:anywhere; }}
    .bubble a {{ color:var(--link); text-decoration:none; }}
    .bubble strong {{ color:#fff; font-weight:700; }}
    .md-h1,.md-h2 {{ color:#fff; font-weight:800; line-height:1.25; margin-bottom:6px; }}
    .md-h1 {{ font-size:20px; }}
    .md-h2 {{ font-size:18px; }}
    .md-h3 {{ color:#fff; font-weight:750; font-size:15px; margin-bottom:4px; }}
    .md-line + .md-line {{ margin-top:4px; }}
    .md-gap {{ height:8px; }}
    .media img,.media video {{ max-width:100%; max-height:420px; border-radius:6px; border:1px solid var(--line); background:#000; display:block; }}
    .media video {{ width:min(100%, 720px); }}
    .caption {{ margin-top:8px; font-size:12px; }}
    aside {{ display:grid; gap:12px; }}
    .panel {{ padding:14px; }}
    dl {{ display:grid; grid-template-columns:95px 1fr; gap:6px 10px; margin:0; }}
    .draft-meta {{ margin-top:12px; }}
    dd {{ margin:0; overflow-wrap:anywhere; }}
    ul {{ margin:0; padding-left:18px; }}
    .draft-title {{ font-weight:700; margin-bottom:6px; }}
    details {{ margin-top:14px; padding:12px 14px; }}
    summary {{ cursor:pointer; font-weight:700; }}
    .source {{ padding:10px 0; border-top:1px solid var(--line); }}
    .source:first-of-type {{ border-top:0; }}
    .source-head {{ color:#fff; font-weight:600; margin-bottom:4px; }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text); }}
    .empty {{ color:var(--muted); font-style:italic; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns:1fr; }} .pack-header {{ flex-direction:column; }} .badges {{ justify-content:flex-start; }} }}
  </style>
</head>
<body>
  <h1>Topic Editor Replay Preview</h1>
  <nav>{nav}</nav>
  {body}
</body>
</html>
""")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render topic-editor evidence packs as a Discord-like HTML preview.")
    parser.add_argument("input", help="Evidence pack directory or run root containing pack directories")
    parser.add_argument("--out", help="Output HTML path. Defaults to <input>/index.html for roots or <pack>/preview.html for a pack.")
    args = parser.parse_args()
    input_path = Path(args.input).resolve()
    default_out = input_path / ("preview.html" if (input_path / "summary.json").exists() else "index.html")
    out = render_html(input_path, Path(args.out).resolve() if args.out else default_out)
    print(out)


if __name__ == "__main__":
    main()
