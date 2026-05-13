"""One-shot inspection of LLM raw output for live-update editor runs.

What actually stores the LLM output:
  - `live_update_editor_runs.metadata.editor_reasoning` (top-level) holds the
    parsed reasoning string the editor extracted post-parse.
  - `live_update_editor_runs.metadata.agent_trace` is persisted as an empty
    object today (see live_update_editor.py:_dev_payload), so the raw text is
    NOT recoverable from there.
  - `live_update_candidates.raw_agent_output.raw_text` is where the actual
    LLM output gets persisted (only when an LLM candidate was produced; the
    heuristic fallback writes no raw_text).

This script joins the two: it walks the most recent editor_runs, then pulls
the matching candidate rows' raw_agent_output.raw_text and categorises that
text the same way _parse_raw_candidates would.

Usage:
    python scripts/inspect_editor_reasoning.py --env prod --limit 30
    python scripts/inspect_editor_reasoning.py --env dev  --limit 30

Reads SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_KEY) from .env in cwd.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get_supabase_client():
    try:
        from supabase import create_client  # type: ignore
    except ImportError as exc:  # pragma: no cover
        print(f"ERROR: supabase-py not installed: {exc}", file=sys.stderr)
        sys.exit(2)
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print(
            "ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_KEY) must be set.",
            file=sys.stderr,
        )
        sys.exit(2)
    return create_client(url, key)


# ---- mirror parser logic from live_update_prompts.py ----

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _parse_json_payload(raw_output: str) -> Tuple[Any, str]:
    """Return (parsed_value, parse_path) where parse_path documents which
    branch of _parse_json_payload succeeded ("direct", "fenced", "span", "none")."""
    text = (raw_output or "").strip()
    if not text:
        return [], "none"
    try:
        return json.loads(text), "direct"
    except json.JSONDecodeError:
        pass

    fenced = _FENCE_RE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1)), "fenced"
        except json.JSONDecodeError:
            pass

    object_start = text.find("{")
    object_end = text.rfind("}")
    array_start = text.find("[")
    array_end = text.rfind("]")
    spans: List[Tuple[int, int]] = []
    if object_start != -1 and object_end > object_start:
        spans.append((object_start, object_end + 1))
    if array_start != -1 and array_end > array_start:
        spans.append((array_start, array_end + 1))
    for start, end in sorted(spans, key=lambda s: s[0]):
        try:
            return json.loads(text[start:end]), "span"
        except json.JSONDecodeError:
            continue
    return [], "none"


def _has_per_candidate_reasoning(candidates: Any) -> bool:
    if not isinstance(candidates, list):
        return False
    for item in candidates:
        if isinstance(item, dict) and any(
            isinstance(item.get(k), str) and item.get(k).strip()
            for k in (
                "editor_reasoning",
                "rationale",
                "editor_notes",
                "why_now",
                "new_information",
            )
        ):
            return True
    return False


def categorise(raw_text: str) -> Dict[str, Any]:
    """Replicate _parse_raw_candidates' decision points and explain them."""
    text = (raw_text or "").strip()
    if not text:
        return {"category": "missing entirely", "parse_path": "none"}

    parsed, parse_path = _parse_json_payload(text)

    # Detect prose-wrap: there is text outside the JSON envelope.
    is_prose_wrapped = False
    if parse_path == "fenced":
        is_prose_wrapped = True
    elif parse_path == "span":
        try:
            # If the JSON span isn't (almost) the entire string, treat as prose-wrapped.
            obj_start = text.find("{")
            obj_end = text.rfind("}")
            arr_start = text.find("[")
            arr_end = text.rfind("]")
            envelope_span: Optional[Tuple[int, int]] = None
            for s, e in (
                ((obj_start, obj_end + 1) if obj_start != -1 and obj_end > obj_start else None),
                ((arr_start, arr_end + 1) if arr_start != -1 and arr_end > arr_start else None),
            ):
                if s is None:
                    continue
                if envelope_span is None or (s[1] - s[0]) > (envelope_span[1] - envelope_span[0]):
                    envelope_span = s
            if envelope_span:
                start, end = envelope_span
                outside = (text[:start] + text[end:]).strip()
                if outside:
                    is_prose_wrapped = True
        except Exception:
            pass

    if parse_path == "none":
        return {"category": "malformed JSON", "parse_path": parse_path}

    if isinstance(parsed, list):
        cat = "bare array"
        if _has_per_candidate_reasoning(parsed):
            cat = "bare array (per-candidate reasoning present)"
        return {"category": cat, "parse_path": parse_path, "prose_wrapped": is_prose_wrapped}

    if isinstance(parsed, dict):
        top_reasoning = parsed.get("editor_reasoning")
        has_top = isinstance(top_reasoning, str) and top_reasoning.strip() != ""
        candidates = parsed.get("candidates")
        if has_top:
            base = "top-level present"
        elif _has_per_candidate_reasoning(candidates):
            base = "per-candidate"
        else:
            base = "missing entirely"
        if is_prose_wrapped:
            return {
                "category": f"prose wrapped ({base})",
                "parse_path": parse_path,
                "prose_wrapped": True,
                "top_reasoning_len": len(top_reasoning) if isinstance(top_reasoning, str) else 0,
            }
        return {
            "category": base,
            "parse_path": parse_path,
            "prose_wrapped": False,
            "top_reasoning_len": len(top_reasoning) if isinstance(top_reasoning, str) else 0,
        }

    # parsed is something else (number/string/null) — treat as malformed for our purposes.
    return {"category": "malformed JSON", "parse_path": parse_path}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=("prod", "dev"), default="prod")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    _load_dotenv(Path.cwd() / ".env")
    client = _get_supabase_client()

    # 1. Pull recent runs (for editor_reasoning + agent_turn_count + model).
    print(
        f"Querying last {args.limit} rows of live_update_editor_runs env={args.env} ...",
        file=sys.stderr,
    )
    resp = (
        client.table("live_update_editor_runs")
        .select("run_id, environment, status, metadata, started_at")
        .eq("environment", args.env)
        .order("started_at", desc=True)
        .limit(args.limit)
        .execute()
    )
    rows: List[Dict[str, Any]] = resp.data or []
    if not rows:
        print(f"No rows returned for env={args.env}.")
        return 0

    # 2. Pull candidates for those run_ids so we can recover raw_text.
    run_ids = [r["run_id"] for r in rows if r.get("run_id")]
    cands_by_run: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if run_ids:
        print(
            f"Querying live_update_candidates for {len(run_ids)} run_ids ...",
            file=sys.stderr,
        )
        c_resp = (
            client.table("live_update_candidates")
            .select("candidate_id, run_id, raw_agent_output, created_at, environment")
            .in_("run_id", run_ids)
            .eq("environment", args.env)
            .execute()
        )
        for c in c_resp.data or []:
            cands_by_run[c["run_id"]].append(c)

    category_counts: Counter[str] = Counter()
    category_examples: Dict[str, Dict[str, Any]] = {}
    parse_path_counts: Counter[str] = Counter()
    turn_counts: Counter[int] = Counter()
    model_counts: Counter[str] = Counter()
    parsed_reasoning_empty = 0
    parsed_reasoning_nonempty = 0
    prose_wrapped_count = 0

    runs_with_no_llm_candidate = 0
    for row in rows:
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        agent_trace = meta.get("agent_trace") or {}

        # Prefer raw_text from candidates (where it is actually persisted).
        raw_text = ""
        for cand in cands_by_run.get(row["run_id"], []):
            rao = cand.get("raw_agent_output") or {}
            if isinstance(rao, str):
                try:
                    rao = json.loads(rao)
                except json.JSONDecodeError:
                    rao = {}
            if isinstance(rao, dict):
                rt = rao.get("raw_text") or ""
                if rt:
                    raw_text = rt
                    break

        # Fall back to (currently always-empty) agent_trace path.
        if not raw_text and isinstance(agent_trace, dict):
            raw_text = agent_trace.get("raw_text") or ""

        if not raw_text:
            runs_with_no_llm_candidate += 1

        parsed_reasoning = meta.get("editor_reasoning") or (
            agent_trace.get("editor_reasoning") if isinstance(agent_trace, dict) else ""
        ) or ""
        if isinstance(parsed_reasoning, str) and parsed_reasoning.strip():
            parsed_reasoning_nonempty += 1
        else:
            parsed_reasoning_empty += 1

        verdict = categorise(raw_text)
        cat = verdict["category"]
        category_counts[cat] += 1
        parse_path_counts[verdict.get("parse_path", "?")] += 1
        if verdict.get("prose_wrapped"):
            prose_wrapped_count += 1

        turn_count_value = (
            meta.get("agent_turn_count")
            or (agent_trace.get("agent_turn_count") if isinstance(agent_trace, dict) else 0)
            or 0
        )
        turn_counts[int(turn_count_value)] += 1
        model_name = (
            meta.get("agent_model")
            or (agent_trace.get("model") if isinstance(agent_trace, dict) else None)
            or meta.get("model")
            or "unknown"
        )
        model_counts[str(model_name)] += 1

        if cat not in category_examples:
            category_examples[cat] = {
                "run_id": row.get("run_id"),
                "started_at": row.get("started_at"),
                "parsed_reasoning_empty": not bool(
                    isinstance(parsed_reasoning, str) and parsed_reasoning.strip()
                ),
                "raw_text_chars": len(raw_text),
                "raw_text_preview": raw_text[:500],
                "parse_path": verdict.get("parse_path"),
            }

    total = sum(category_counts.values())
    print("\n=== Category counts ({} runs, env={}) ===".format(total, args.env))
    for cat, count in category_counts.most_common():
        pct = (count / total) * 100 if total else 0.0
        print(f"  {count:>3}  ({pct:5.1f}%)  {cat}")

    print("\n=== Parse path distribution ===")
    for path, count in parse_path_counts.most_common():
        print(f"  {count:>3}  {path}")

    print("\n=== Model distribution ===")
    for model, count in model_counts.most_common():
        print(f"  {count:>3}  {model}")

    print("\n=== Agent turn count distribution ===")
    for turns, count in sorted(turn_counts.items()):
        print(f"  turns={turns:<3}  {count}")

    print("\n=== editor_reasoning persisted on run (post-parse) ===")
    print(f"  empty:     {parsed_reasoning_empty}")
    print(f"  non-empty: {parsed_reasoning_nonempty}")
    print(f"  prose-wrapped raw outputs: {prose_wrapped_count}")
    print(f"  runs with no recoverable raw_text: {runs_with_no_llm_candidate}")

    print("\n=== Example raw_text per category (first 500 chars) ===")
    for cat, example in category_examples.items():
        print("\n--- category: {} ---".format(cat))
        print(
            "run_id={run_id}  started_at={started_at}  "
            "parse_path={parse_path}  raw_chars={raw_text_chars}  "
            "parsed_reasoning_empty={parsed_reasoning_empty}".format(**example)
        )
        print(example["raw_text_preview"] or "<empty>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
