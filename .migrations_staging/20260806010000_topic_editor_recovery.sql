-- Cross-run recovery for topic_editor_drafts.
--
-- Purpose: drafts are the durable artifact of the topic editor. When a run is
-- force-closed (cost cap / max turns / lease steal), in-flight drafts were
-- previously orphaned because (a) the source checkpoint advanced past their
-- source messages and (b) no later run ever re-exposed them. This migration:
--
--   1. Changes topic_editor_drafts.run_id FK from ON DELETE CASCADE to
--      ON DELETE SET NULL, so pruning a run no longer deletes its drafts.
--   2. Adds recovery columns used by the guarded claim in
--      _recover_stale_drafts: recovery_claimed_by_run_id, recovery_claimed_at,
--      recovery_count, and needs_review_reason (durable human-review backlog).
--
-- The (environment, status, updated_at) index already exists and serves the
-- recovery query.

-- 1. Recreate the FK as SET NULL. Postgres cannot ALTER a constraint's
--    action in place; drop and re-add.
ALTER TABLE public.topic_editor_drafts
    DROP CONSTRAINT IF EXISTS topic_editor_drafts_run_id_fkey;

ALTER TABLE public.topic_editor_drafts
    ADD CONSTRAINT topic_editor_drafts_run_id_fkey
    FOREIGN KEY (run_id) REFERENCES public.topic_editor_runs(run_id)
    ON DELETE SET NULL;

-- 2. Recovery columns.
ALTER TABLE public.topic_editor_drafts
    ADD COLUMN IF NOT EXISTS recovery_claimed_by_run_id uuid
        REFERENCES public.topic_editor_runs(run_id) ON DELETE SET NULL;
ALTER TABLE public.topic_editor_drafts
    ADD COLUMN IF NOT EXISTS recovery_claimed_at timestamptz;
ALTER TABLE public.topic_editor_drafts
    ADD COLUMN IF NOT EXISTS recovery_count integer NOT NULL DEFAULT 0;
ALTER TABLE public.topic_editor_drafts
    ADD COLUMN IF NOT EXISTS needs_review_reason text;

-- Keep the existing status index; it covers (environment, status, updated_at).

-- 3. Atomic claim RPC for cross-run recovery. The conditional UPDATE both claims
--    the draft (status must still be recoverable) AND increments recovery_count in
--    one statement, returning the updated row. This makes the claim exclusive
--    across runs and makes the exhaustion guard durable (no separate best-effort
--    bump that could silently fail and loop forever).
CREATE OR REPLACE FUNCTION public.claim_topic_editor_draft(
    p_draft_id text,
    p_claimant_run_id uuid,
    p_statuses text[],
    p_environment text
)
RETURNS SETOF public.topic_editor_drafts
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    UPDATE public.topic_editor_drafts
    SET run_id = p_claimant_run_id,
        recovery_claimed_by_run_id = p_claimant_run_id,
        recovery_claimed_at = timezone('utc', now()),
        recovery_count = recovery_count + 1,
        updated_at = timezone('utc', now())
    WHERE draft_id = p_draft_id
      AND environment = p_environment
      AND status = ANY(p_statuses)
    RETURNING *;
END;
$$;
