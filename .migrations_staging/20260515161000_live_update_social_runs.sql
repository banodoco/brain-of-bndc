-- Live-update social persistence schema.
-- Idempotent: safe to replay in production.

create extension if not exists pgcrypto;

create or replace function public.set_live_update_social_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := timezone('utc', now());
    return new;
end;
$$;

create table if not exists public.live_update_social_runs (
    run_id uuid primary key default gen_random_uuid(),
    topic_id text not null,
    platform text not null,
    action text not null default 'post',
    mode text not null default 'draft'
        check (mode in ('draft', 'publish')),
    terminal_status text
        check (terminal_status is null or terminal_status in ('draft', 'queued', 'published', 'skip', 'needs_review')),
    guild_id bigint,
    channel_id bigint,
    chain_vendor text not null default 'codex',
    chain_depth text not null default 'high',
    chain_with_feedback boolean not null default true,
    chain_deepseek_provider text not null default 'direct',
    source_metadata jsonb not null default '{}'::jsonb,
    publish_units jsonb not null default '{}'::jsonb,
    draft_text text,
    media_decisions jsonb not null default '{}'::jsonb,
    publication_outcome jsonb,
    trace_entries jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

alter table public.live_update_social_runs
    add column if not exists publication_outcome jsonb,
    add column if not exists publish_units jsonb not null default '{}'::jsonb;

alter table public.social_publications
    add column if not exists media_attached jsonb,
    add column if not exists media_missing jsonb;

create unique index if not exists uq_live_update_social_runs_topic_platform_action
    on public.live_update_social_runs (topic_id, platform, action);

create index if not exists idx_live_update_social_runs_guild_status_created
    on public.live_update_social_runs (guild_id, terminal_status, created_at desc);

create index if not exists idx_live_update_social_runs_topic
    on public.live_update_social_runs (topic_id);

create index if not exists idx_social_publications_live_update_source
    on public.social_publications (source_kind, platform, action, created_at desc)
    where source_kind = 'live_update_social';

drop trigger if exists live_update_social_runs_set_updated_at on public.live_update_social_runs;
create trigger live_update_social_runs_set_updated_at
before update on public.live_update_social_runs
for each row
execute function public.set_live_update_social_updated_at();

alter table public.live_update_social_runs enable row level security;

revoke all on public.live_update_social_runs from anon, authenticated;
