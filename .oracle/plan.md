# Plan v2 — brain-of-bndc megado delta (revised after Explore, STABLE)

Base: d186dee · Worktree: ../brain-of-bndc-megado · Estimate: **~1 day → NOT huge run** · **STABLE**

## Synthesis of Explore findings (bias: elegance/simplicity, cut non-essential scope)

**Migrations (highest risk):** `guidance_version` replay gap is the only must-fix. `CREATE TABLE IF NOT EXISTS` won't add column on DBs that ran `b2873df` before `1867ebd`. Insert will 42703-fail, logs lost. Fix: single `ALTER TABLE ... ADD COLUMN IF NOT EXISTS guidance_version TEXT` migration. Other findings (RLS no policies, no revoke, no updated_at trigger) are intentional (service-role bypass, observability-only per agent_goal non-goal) — document, don't change. Keep append-only, best-effort.

**Async safety:** Support writes are sync `supabase.table(...).execute()` inside `async def` — blocks event loop per RTT while peers use `await asyncio.to_thread`. Also `_processing_threads` plain `set` is safe today (single-tick atomic) but undocumented. Fix: wrap both writes in `asyncio.to_thread` per codebase norm; keep guard as-is, add comment. No queue/schema change.

**Chunking:** `split_message` fence-unaware → mid-block split garbles rendering and breaks copy-paste. Risk is UX only. Fix: make `split_message` fence-aware (track `in_fence` on ``` lines, avoid splitting inside, close/reopen if forced). Low scope, keeps 2000 limit and paragraph preference.

**Tool contracts:** Allowlist, Hivemind citations (jump URLs), ComfyWorkflow staged-edit + SSRF guards all correct and executor-enforced. No changes needed. Spot-checked, aligned.

**No new areas to explore.** All findings are scoped fixes, not architecture. No new tool surfaces.

## Revised Architecture (elegance bias: 3 fixes, no new abstractions)
- **B1 — Migrations fix + persistence contract**: add `ADD COLUMN IF NOT EXISTS` migration for `guidance_version`; keep RLS/enable as-is; verify `_persist_turn`/`record_outcome` now handle missing column gracefully (best-effort). Tests: idempotency, column existence, insert with guidance_version.
- **B2 — Async + chunking**: `await asyncio.to_thread(lambda: sb(...).execute())` for both writes; fence-aware `split_message` with regression test for ``` block. Tests: async safety (to_thread called), fence preservation, 2000 cap, OutcomeView attachment.
- **B3 — Vigorous suite**: full `pytest` 1260+ tests, structure.md, clean-base comparison (≤8 pre-existing failures, zero new), plus live smoke of #support trigger → persistence → chunked delivery in pty harness (like prior B4). No new features.

## North Star check (explicit)
- **Advances:** evidence-over-vibes (Hivemind citations persist via fixed schema), concreteness (staged edits still deliver file), thread=session (persistence preserves history, now not lost to 42703).
- **Avoids anti-patterns:** No parallel agent framework, no admin-tool exposure, no speculative layers. Fixes are minimal, reuse existing patterns (`asyncio.to_thread`, paragraph splitter).
- **No task-specific conflicts.**

## Batches (frozen after pre-execution review)
Same 3 batches as v1, now with scoped fixes. No [XHARD] — all decomposable, locally validatable.
