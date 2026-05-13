"""Audit the most recent live-update editor runs.

Pulls the last ~20 rows from `live_update_editor_runs` (and the linked
candidates / decisions / feed items) directly from Supabase and prints a
forensic-style summary for each run:

  - timestamp / trigger / status
  - candidate_count / accepted_count / rejected_count / deferred_count
  - parsed editor_reasoning length (from metadata.agent_trace.editor_reasoning
    when available, otherwise from raw_agent_output of the first candidate)
  - tool_trace turn count + model name
  - skipped_reason / error_message

Run with the bot's environment loaded:

    python scripts/debug_live_editor_audit.py [--env prod|dev] [--limit 20]

Requires SUPABASE_URL and SUPABASE_KEY (or the service role key) in env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

try:
    from supabase import create_client  # type: ignore
except Exception as exc:  # pragma: no cover - script-level import diag
    sys.stderr.write(f"supabase-py not importable: {exc}\n")
    sys.exit(2)


def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _reasoning_from_run(run: Dict[str, Any], cand_rows: List[Dict[str, Any]]) -> str:
    # 1) metadata.agent_trace.editor_reasoning if the editor stashed it there
    meta = run.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    reasoning = _dig(meta, "agent_trace", "editor_reasoning", default="") or ""
    if reasoning:
        return reasoning
    # 2) Fall back to the first candidate's raw_agent_output.editor_reasoning if present
    for cand in cand_rows:
        raw = cand.get("raw_agent_output") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if isinstance(raw, dict):
            er = raw.get("editor_reasoning") or _dig(raw, "agent_trace", "editor_reasoning")
            if er:
                return str(er)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="prod", choices=["prod", "dev"])
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
    if not (url and key):
        sys.stderr.write("Missing SUPABASE_URL or SUPABASE_KEY/SUPABASE_SERVICE_KEY in env.\n")
        return 2
    sb = create_client(url, key)

    runs = (
        sb.table("live_update_editor_runs")
        .select("*")
        .eq("environment", args.env)
        .order("created_at", desc=True)
        .limit(args.limit)
        .execute()
        .data
        or []
    )
    if not runs:
        print(f"(no runs in env={args.env})")
        return 0

    print(f"# live_update_editor_runs (env={args.env}, limit={args.limit})\n")

    # Accumulators for aggregate telemetry
    recovery_path_counter: Counter[str] = Counter()
    watchlist_add_count: int = 0
    watchlist_update_count: int = 0

    for run in runs:
        run_id = run.get("run_id")
        cands = (
            sb.table("live_update_candidates")
            .select("candidate_id,status,raw_agent_output,author_context_snapshot")
            .eq("run_id", run_id)
            .execute()
            .data
            or []
        )
        decisions = (
            sb.table("live_update_decisions")
            .select("decision,reason")
            .eq("run_id", run_id)
            .execute()
            .data
            or []
        )
        feed = (
            sb.table("live_update_feed_items")
            .select("feed_item_id,status")
            .eq("run_id", run_id)
            .execute()
            .data
            or []
        )

        reasoning = _reasoning_from_run(run, cands)
        meta = run.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        model = _dig(meta, "agent_trace", "model", default="?")
        tool_turns = _dig(meta, "agent_trace", "agent_turn_count", default=0)
        tool_calls = len(_dig(meta, "agent_trace", "tool_trace", default=[]) or [])
        prompt_messages = _dig(meta, "prompt_messages", default="?")
        recovery_path = _dig(meta, "agent_trace", "reasoning_recovery_path", default="none") or "none"
        recovery_path_counter[recovery_path] += 1
        wl_actions = _dig(meta, "agent_trace", "watchlist_actions", default=[]) or []
        for a in wl_actions:
            if isinstance(a, dict):
                act = a.get("action") or a.get("tool")
                if act == "watchlist_add":
                    watchlist_add_count += 1
                elif act == "watchlist_update":
                    watchlist_update_count += 1

        # Per-author distribution among generated candidates
        authors = []
        for c in cands:
            snap = c.get("author_context_snapshot") or {}
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap)
                except Exception:
                    snap = {}
            authors.append(snap.get("author_id") or snap.get("author_name") or "?")
        author_counts = {a: authors.count(a) for a in set(authors)}

        decision_counts: Dict[str, int] = {}
        for d in decisions:
            decision_counts[d.get("decision") or "?"] = decision_counts.get(d.get("decision") or "?", 0) + 1

        print(
            f"- run_id={run_id}\n"
            f"  created_at={run.get('created_at')}  trigger={run.get('trigger')}  status={run.get('status')}\n"
            f"  candidate_count={run.get('candidate_count')}  accepted={run.get('accepted_count')}  "
            f"rejected={run.get('rejected_count')}  deferred={run.get('deferred_count')}  duplicate={run.get('duplicate_count')}\n"
            f"  feed_items_posted={sum(1 for f in feed if f.get('status') == 'posted')}/{len(feed)}\n"
            f"  prompt_messages={prompt_messages}  model={model}  agent_turns={tool_turns}  tool_calls={tool_calls}\n"
            f"  decision_breakdown={decision_counts}\n"
            f"  author_distribution={author_counts}\n"
            f"  editor_reasoning_len={len(reasoning)}  empty={'YES' if not reasoning else 'no'}\n"
            f"  reasoning_recovery_path: {recovery_path}\n"
            f"  skipped_reason={run.get('skipped_reason')}  error_message={run.get('error_message')}\n"
            f"  reasoning_preview={(reasoning[:160] + '...') if len(reasoning) > 160 else reasoning!r}\n"
        )

    # -- aggregate telemetry --
    print(f"\n{'=' * 48}")
    print("## aggregate telemetry across queried runs\n")

    print("### reasoning_recovery_path distribution")
    if recovery_path_counter:
        for branch, count in recovery_path_counter.most_common():
            print(f"  {branch}: {count}")
    else:
        print("  (no recovery path data)")

    print("\n### watchlist-actions summary")
    print(f"  watchlist_add calls: {watchlist_add_count}")
    print(f"  watchlist_update calls: {watchlist_update_count}")
    print(f"  total watchlist actions: {watchlist_add_count + watchlist_update_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
