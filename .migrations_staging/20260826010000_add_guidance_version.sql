-- support_agent_turns: backfill guidance_version hash for prompt-version tracking
-- Compensating ALTER for DBs that applied 20260826000000 before it included
-- guidance_version (b2873df → 1867ebd replay gap). Idempotent via
-- ADD COLUMN IF NOT EXISTS so re-apply is a no-op.
--
-- NOTE: This migration is staged only.  Do NOT auto-apply to the live database;
-- application is left to the human / orchestrator (the cog degrades to a
-- logged warning when the table/column is missing).

alter table public.support_agent_turns
    add column if not exists guidance_version text;
