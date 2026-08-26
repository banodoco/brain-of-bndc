# Tasklist — brain-of-bndc delta (frozen after pre-execution review) — STABLE

Base: d186dee · Worktree: ../brain-of-bndc-megado · Model: Ox Alpha every role · No [XHARD]

## Batch 1 — Migrations fix + persistence contract
**Objective:** Fix `guidance_version` replay gap so every turn persists; keep observability-only contract.
- **T1.1** Add migration `..._add_guidance_version.sql` with `ALTER TABLE support_agent_turns ADD COLUMN IF NOT EXISTS guidance_version TEXT`.
- **T1.2** Verify `support_cog.py` `_persist_turn` and `record_outcome` handle missing column (best-effort try/except, no crash) and now include `guidance_version` hash (sha256(SUPPORT_GUIDANCE)[:12] per code).
- **T1.3** Tests: migration idempotency (apply twice), insert with guidance_version, stubbed Supabase contract.

**Checkpoint B1:** `pytest -k support` migration + persistence tests pass; `guidance_version` column exists after migrations; insert does not 42703.

**North Star:** advances thread=session persistence, not lost to 42703.

## Batch 2 — Async safety + fence-aware chunking
**Objective:** Eliminate event-loop block and garbled code fences.
- **T2.1** Wrap both Supabase writes (`support_cog.py:364` insert and `466-468` upsert) in `await asyncio.to_thread(lambda: sb(...).execute())` per codebase norm (`storage_handler.py:162`).
- **T2.2** Make `src/common/discord_utils.py:14-50` `split_message` fence-aware: track `in_fence` on ``` lines, avoid splitting inside, close with ``` and reopen next chunk if forced.
- **T2.3** Tests: async safety (assert to_thread called), fence preservation (split inside ``` retains fences), 2000 cap, paragraph alignment, OutcomeView attachment.

**Checkpoint B2:** `pytest -k support` async + chunking tests pass; no loop block, fences intact.

## Batch 3 — Vigorous integration + full suite
**Objective:** Prove zero regressions and end-to-end #support flow.
- **T3.1** End-to-end: `support_cog` trigger (on_thread_create/on_message) → `AdminChatAgent` turn → persistence → paragraph-chunked delivery with OutcomeView, via pty harness + stubbed Hivemind/VibeComfy.
- **T3.2** Full suite: `pytest` 1260+ tests, compare to clean-base (expect ≤8 pre-existing failures, zero new). Run from host (single owner, no duplication).
- **T3.3** Structure.md row for support feature (if missing), plus evidence matrix.

**Checkpoint B3 (final):** Full suite passes with zero new failures; B1+B2 fixes verified via B3 integration; evidence matrix complete.

**Sync:** No push/merge without user approval. Worktree only. Vigorous testing at completion per user request.
# Tasklist — FROZEN after pre-execution review PASS (2026-08-26)
