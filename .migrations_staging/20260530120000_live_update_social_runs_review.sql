-- Add review/approval columns to live_update_social_runs.
-- Idempotent: safe to replay in production.
--
-- NOTE: Legacy rows where terminal_status IS NOT NULL (draft/queued/published/skip/needs_review)
-- will receive the 'pending' default for approval_state via this migration. Those rows are
-- filtered out by list_open_social_runs's `terminal_status IS NULL` clause and will never
-- participate in the review/approval flow.

alter table public.live_update_social_runs
    add column if not exists review_message_id bigint,
    add column if not exists revision int not null default 0,
    add column if not exists approval_state text not null default 'pending'
        check (approval_state in ('pending', 'approved', 'expired')),
    add column if not exists approved_revision int,
    add column if not exists approved_text text,
    add column if not exists approved_quote text,
    add column if not exists approved_at timestamptz,
    add column if not exists expires_at timestamptz,
    add column if not exists publish_revision int;

create index if not exists idx_live_update_social_runs_review_message_id
    on public.live_update_social_runs (review_message_id)
    where review_message_id is not null;

create index if not exists idx_live_update_social_runs_open
    on public.live_update_social_runs (topic_id)
    where approval_state = 'pending' and terminal_status is null;
