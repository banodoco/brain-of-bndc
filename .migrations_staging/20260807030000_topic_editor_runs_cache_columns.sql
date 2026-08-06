-- Add cache accounting columns to topic_editor_runs.
--
-- The topic editor writes cache_hit_tokens, cache_miss_tokens, cache_hit_pct,
-- and estimated_cache_adjusted_cost_usd into every run-update payload
-- (topic_editor.py _run_updates), but the phase-1 migration
-- (20260513230500) only defined input_tokens/output_tokens/cost_usd/latency_ms.
-- PostgREST then failed the entire UPDATE with
-- "Could not find the '<col>' column ... in the schema cache", which left every
-- run stuck in the started state and surfaced as false failures. Add the four
-- columns to match the code's contract.

ALTER TABLE public.topic_editor_runs
    ADD COLUMN IF NOT EXISTS cache_hit_tokens bigint NOT NULL DEFAULT 0;
ALTER TABLE public.topic_editor_runs
    ADD COLUMN IF NOT EXISTS cache_miss_tokens bigint NOT NULL DEFAULT 0;
ALTER TABLE public.topic_editor_runs
    ADD COLUMN IF NOT EXISTS cache_hit_pct numeric(5,2);
ALTER TABLE public.topic_editor_runs
    ADD COLUMN IF NOT EXISTS estimated_cache_adjusted_cost_usd numeric(12,6);
