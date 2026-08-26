# B1 Checkpoint Review — Ox Alpha (independent, 1 pass)

**Verdict: PASS**

**Scope:** Batch 1 — Migrations fix + persistence contract. Worktree `brain-of-bndc-megado` @ `d186dee`, branch `megado-run`.

## Checklist

- **Migration file** `.migrations_staging/20260826010000_add_guidance_version.sql` — EXISTS, 584B, `ALTER TABLE public.support_agent_turns ADD COLUMN IF NOT EXISTS guidance_version TEXT` (idempotent, replay-safe for b2873df→1867ebd gap), header correctly says staged-only / `Do NOT auto-apply` + compensating-ALTER comment. Correctly ignored by `/.migrations_staging/` (prior 8 migrations likewise force-added) — will need `git add -f` at commit, not a defect at this stage.
- **Cog** `support_cog.py:125` `GUIDANCE_VERSION = sha256(SUPPORT_GUIDANCE)[:12]` and `:360` insert include guidance_version; `:343-370` `_persist_turn` is best-effort `try/except Exception` → `logger.warning` (table missing? run staged migration), never raises, so 42703 is a warning not a crash. Verified `thread.sent` still delivers.
- **Tests** `tests/test_support_cog.py` — 7 new `TestGuidanceVersionMigration` tests, `pytest -k support` 96 passed (45 in file + 51 elsewhere, 7/7 B1 green), stubbed Supabase idempotency via double-apply.
- **North Star:** advances `thread=session` — persistence no longer lost to 42703; evidence-backed turns retained. No anti-pattern (no admin tools exposed, no parallel framework).
- **Elegance:** minimal fix, no new abstractions/layers/config surfaces, reuses existing hash + try/except pattern. No overengineering.
- **Custody:** `custody.md`/`plan.md`/`evidence/batch-1.log` all record `Ox Alpha (stealth/ox-alpha via OpenRouter) — user-selected`, worktree-only, no push/merge. No external dispatches (no codex/deepseek/grok calls in Evidence); Grok mentions only in historic `d186dee` commit message.

## Issues (non-blocking, <300w)

- `test_persist_column_existence_via_stubbed_select` is tautological — asserts a fake `set` then repeats `test_insert_includes_guidance_version`; defends no new contract. Keep or remove at B3.
- Duplicate `make_cog` helper (line 161 vs 409) — minor duplication.
- Remember `git add -f .migrations_staging/20260826010000_add_guidance_version.sql` at commit due to `.gitignore`.

No blocking defects. Recommend proceed B1→B2.
