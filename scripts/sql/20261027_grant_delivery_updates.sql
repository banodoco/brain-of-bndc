-- Durable applicant delivery updates, independent of grant funding records.
create table if not exists public.grant_delivery_updates (
    guild_id bigint not null,
    message_id bigint not null,
    thread_id bigint not null,
    applicant_id bigint not null,
    content text not null default '',
    source_url text,
    evidence jsonb not null default '[]'::jsonb,
    assessment jsonb,
    state text not null default 'pending'
        check (state in ('processing', 'pending', 'assessed', 'acknowledged', 'ignored')),
    ack_message_id bigint,
    lease_owner text,
    lease_until timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (guild_id, message_id)
);

create index if not exists grant_delivery_updates_thread_history_idx
    on public.grant_delivery_updates (guild_id, thread_id, created_at);
create index if not exists grant_delivery_updates_work_idx
    on public.grant_delivery_updates (state, lease_until)
    where state in ('pending', 'processing', 'assessed');

alter table public.grant_delivery_updates enable row level security;
revoke all on public.grant_delivery_updates from anon, authenticated;
grant all on public.grant_delivery_updates to service_role;

drop policy if exists grant_delivery_updates_service_role on public.grant_delivery_updates;
create policy grant_delivery_updates_service_role
    on public.grant_delivery_updates
    for all
    to service_role
    using (true)
    with check (true);
