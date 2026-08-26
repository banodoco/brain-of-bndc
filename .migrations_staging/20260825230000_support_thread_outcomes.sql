-- support_thread_outcomes: member-selected resolution state for #support
-- threads. After each support-agent turn the bot attaches three buttons
-- (Resolved | Probably Resolved | Not Resolved); the selecting member's
-- choice is upserted here, one row per thread (latest selection wins).
--
-- NOTE: This migration is staged only.  Do NOT auto-apply to the live database;
-- application is left to the human / orchestrator (deploying the bot code
-- before applying it would make outcome writes fail with 42P01 — the cog
-- degrades gracefully to message-edit-only in that case).

create table if not exists public.support_thread_outcomes (
    thread_id bigint primary key,
    guild_id bigint not null default 0,
    message_id bigint,
    member_id bigint not null,
    outcome text not null check (outcome in ('resolved', 'probably_resolved', 'not_resolved')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.support_thread_outcomes enable row level security;
