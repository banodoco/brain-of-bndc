-- support_agent_turns: structured record of every support-agent turn —
-- the member message, the replies posted, the full tool-call trace
-- (name/input/result per call), model, errors, and duration. One row per
-- turn; append-only.
--
-- NOTE: This migration is staged only.  Do NOT auto-apply to the live database;
-- application is left to the human / orchestrator (the cog degrades to a
-- logged warning when the table is missing).

create table if not exists public.support_agent_turns (
    id bigint generated always as identity primary key,
    thread_id bigint not null,
    guild_id bigint not null default 0,
    member_id bigint,
    trigger text not null check (trigger in ('new_post', 'follow_up', 'catch_up')),
    user_message text,
    replies jsonb,
    tool_calls jsonb,
    model text,
    guidance_version text,
    error text,
    duration_ms integer,
    created_at timestamptz not null default now()
);

create index if not exists support_agent_turns_thread_idx
    on public.support_agent_turns (thread_id, created_at desc);

alter table public.support_agent_turns enable row level security;
