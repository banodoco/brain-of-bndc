# Batch 1 — Migrations fix + persistence contract (Ox Alpha, Vigorous)

North Star: evidence-backed support answers must persist; thread=session.

Task: In worktree /Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc-megado
- T1.1 Add migration file `.migrations_staging/20260826010000_add_guidance_version.sql` with `ALTER TABLE support_agent_turns ADD COLUMN IF NOT EXISTS guidance_version TEXT;` (idempotent). Follow existing migration header style (DO NOT auto-apply comment).
- T1.2 Verify `src/features/support/support_cog.py` already inserts `guidance_version` (line 125 hash, 364 insert) — ensure try/except is best-effort so missing column does not crash turn.
- T1.3 Tests: Add or verify tests cover idempotency (apply twice), column existence, insert with guidance_version. Use stubbed Supabase (no live DB needed). Follow repo test conventions (tests/test_support_cog.py).

Acceptance: `pytest -k support` B1 tests pass; migration is IF NOT EXISTS; insert does not 42703.

Do not touch main branch. Worktree only. Record model source as user-selected Ox Alpha.
