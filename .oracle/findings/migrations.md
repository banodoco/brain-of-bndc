# Findings — Supabase Schema / Migration Idempotency

## Ranked findings (verified)
1. **Idempotent CREATEs.** Both use `create table if not exists` + `create index if not exists` (`20260826000000_support_agent_turns.sql:10,26`; `20260825230000_support_thread_outcomes.sql:11`), matching `20260515161000:16,49`.

2. **guidance_version replay gap — highest risk.** `b2873df` shipped `support_agent_turns` without `guidance_version`; `1867ebd` edited same `CREATE TABLE` to add it (`20260826000000:20`, `support_cog.py:125,360`). `CREATE TABLE IF NOT EXISTS` won't add column on DBs that already applied `b2873df`. No `ADD COLUMN IF NOT EXISTS` compensating migration; inserts at `support_cog.py:364` would 42703-fail (warned, logs lost).

3. **RLS enabled, no policies, inconsistent REVOKE.** Both `enable row level security` (`20260826000000:29`; `20260825230000:21`) with no `CREATE POLICY`. Service-role bypasses RLS so bot writes succeed. Prior `live_update_social_runs` adds `revoke all … from anon, authenticated` (`20260515161000:70`); support tables omit it.

4. **Indexes correct.** `support_agent_turns_thread_idx on (thread_id, created_at desc)` (`20260826000000:26`) covers per-thread history; `support_thread_outcomes` PK on `thread_id` (`20260825230000:12`) is its index. No index on `guidance_version` intentional.

5. **Persistence bypasses DatabaseHandler.** `_persist_turn` (`support_cog.py:343-370`) and `record_outcome` (`:441-475`) call `supabase.table(...).insert/upsert` directly; `db_handler.py` has zero `support_*` methods. Best-effort try/except, inline `guild_id` default `0`, no `_gate_check`.

6. **Staged-only intentional.** Headers “Do NOT auto-apply” (`20260826000000:6-8`; `20260825230000:6-9`); cog degrades to warning/edit-only (`support_cog.py:365,470`). Aligns with `agent_goal.md:32` non-goal — tables are observability logs, not session store.

## Unknowns
- Whether prod applied pre-`1867ebd` table (column missing?); RLS key posture; retention for append-only `support_agent_turns`.

## Risks
- Silent log loss if column missing; `GUIDANCE_VERSION=sha256(SUPPORT_GUIDANCE)[:12]` (`support_cog.py:125`) churns on guidance edits; `support_thread_outcomes.updated_at` never auto-touched (no trigger vs `live_update_social_runs:62`).

## Suggested approach
Add `alter table … add column if not exists guidance_version text` migration; keep RLS-enable (add `revoke` for consistency optional); keep append-only insert/upsert; add `updated_at` trigger only if freshness matters. Keep as observability, not session persistence.
