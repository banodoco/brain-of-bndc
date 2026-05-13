# Live-Update Topic Editor Cutover Runbook

This runbook covers the single-sprint production cutover for the live-update
topic editor described in `docs/live-update-editor-sprint-plan.md` and
`docs/live-update-editor-redesign.md` v6.

The sprint plan supersedes the older Phase-1 soak, shadow gate, and 20-window
replay requirements. For this sprint there is one publishing-off sanity replay,
then the production flip. Problems found after the flip are fixed or rolled back
from production state; there is no formal validation window or shadow-mode gate.

## Operator Values

These values are operator-owned and must be confirmed before publishing is
enabled in production:

| Value | Current repo default | Production action |
|---|---:|---|
| Publishing toggle | `TOPIC_EDITOR_PUBLISHING_ENABLED=false` | Set to `true` only for the final production publishing flip. |
| Trace channel | `LIVE_UPDATE_TRACE_CHANNEL_ID=1316024582041243668` | Replace the dev placeholder with the production `#daily-updates` / operator trace target before the flip. |
| Rollback selector | `LIVE_UPDATE_EDITOR_BACKEND=legacy` in local env files | Use `topic` or unset for the new editor; set `legacy` to roll back runtime selection. |
| Bot identity | BNDC bot id `1316765722738688030` | Confirm the deployed bot identity posts both traces and live messages. |
| Supabase project | Banodoco `ujlwuvkrxlvoswwkerdf` | Apply the staged migrations to the target environment before backfill/replay. |

`TOPIC_EDITOR_PUBLISHING_ENABLED=false` is the safe default. Never enable it
until schema, backfill, checkpoint mirroring, replay, and deployment checks are
green.

## Sprint Gates

All gates below must pass before setting `TOPIC_EDITOR_PUBLISHING_ENABLED=true`.

| Gate | Verification | Pass criterion |
|---|---|---|
| Backlog migration applied | `supabase migration list --linked \| rg 20260512185328` | Migration is present remotely. |
| Topic schema applied | `supabase migration list --linked \| rg 20260513230500` plus table checks below | Topic editor tables exist remotely. |
| Topic tables present | `select table_name from information_schema.tables where table_schema='public' and table_name in ('topics','topic_sources','topic_aliases','topic_transitions','editorial_observations','topic_editor_runs','topic_editor_checkpoints');` | All 7 returned. |
| Lease index present | `select indexname from pg_indexes where indexname = 'topic_editor_runs_active_lease_idx';` | Active-only lease index exists. |
| Transition vocabulary locked | `select distinct action from topic_transitions where action in ('rejected_update_sources','rejected_discard');` | Zero rows; invalid update/discard audits use base actions with `payload->>'outcome'='tool_error'`. |
| Backfill parity, posted | `select (select count(*) from live_update_feed_items where environment='prod') legacy, (select count(*) from topics where environment='prod' and state='posted') topic;` | `topic >= legacy`, with any delta explained by post-backfill writes. |
| Backfill parity, watching | `select (select count(*) from live_update_watchlist where environment='prod') legacy, (select count(*) from topics where environment='prod' and state='watching') topic;` | `topic >= legacy`, with any delta explained by post-backfill writes. |
| Checkpoint mirrored into topic editor | `select checkpoint_key,last_message_id,last_message_created_at from topic_editor_checkpoints where environment='prod' order by updated_at desc limit 5;` | Target live channel checkpoint exists and starts from the legacy checkpoint. |
| Publishing-off replay completed | `TOPIC_EDITOR_PUBLISHING_ENABLED=false python scripts/run_live_update_dev.py --topic-editor-replay-prod` | Command prints transitions, trace text, and would-publish Discord messages without posting. |
| Runtime selector ready | Deployed env has `LIVE_UPDATE_EDITOR_BACKEND=topic` or omits the var. | Scheduled, startup `--summary-now`, and owner `summarynow` use `TopicEditor`. |
| Trace target ready | `LIVE_UPDATE_TRACE_CHANNEL_ID` points to the production trace target. | Operator can see a trace from the publishing-off replay or first scheduled pass. |
| Health/admin visibility ready | Admin/health status reads `topic_editor_runs`, `topics`, and `topic_transitions`. | Legacy `live_update_*` data is labeled rollback state only. |

## Fast Triage

Use `topic_editor_runs` as the new-editor run lifecycle table:

```sql
select run_id, trigger, status, started_at, completed_at,
       source_message_count, tool_call_count, accepted_count, rejected_count,
       override_count, observation_count, published_count, failed_publish_count,
       publishing_enabled, error_message
from topic_editor_runs
where environment = 'prod'
order by started_at desc
limit 10;
```

Publication state:

```sql
select publication_status, count(*) as n,
       min(last_published_at) as first_seen,
       max(last_published_at) as last_seen
from topics
where environment = 'prod'
group by publication_status
order by publication_status;
```

Recent rejections and overrides:

```sql
select action, count(*) as n
from topic_transitions
where environment = 'prod'
  and created_at >= now() - interval '24 hours'
group by action
order by n desc, action;
```

Failed or partial publications:

```sql
select topic_id, headline, publication_status, publication_attempts,
       publication_error, discord_message_ids, updated_at
from topics
where environment = 'prod'
  and publication_status in ('failed','partial')
order by updated_at desc
limit 20;
```

## Cutover Procedure

1. Confirm all sprint gates are green.
2. Confirm the production deploy has the topic editor code and the final
   operator values.
3. Keep `TOPIC_EDITOR_PUBLISHING_ENABLED=false`.
4. Run the one publishing-off sanity replay:

   ```bash
   TOPIC_EDITOR_PUBLISHING_ENABLED=false \
   LIVE_UPDATE_EDITOR_BACKEND=topic \
   python scripts/run_live_update_dev.py --topic-editor-replay-prod
   ```

5. Inspect the printed transitions, trace text, and would-publish Discord
   messages. Fix only concrete breakage; do not introduce a shadow validation
   window.
6. Set the deployed runtime selector to the new editor:

   ```bash
   LIVE_UPDATE_EDITOR_BACKEND=topic
   ```

   Leaving the variable unset is also valid because the code defaults to the
   topic editor. Do not use `legacy` for the cutover.

7. Set the trace channel to the production target:

   ```bash
   LIVE_UPDATE_TRACE_CHANNEL_ID=<production trace channel id>
   ```

8. Flip production publishing:

   ```bash
   TOPIC_EDITOR_PUBLISHING_ENABLED=true
   ```

9. Record the UTC flip timestamp.
10. On the next scheduled pass, verify a completed row in `topic_editor_runs`,
    a trace post from the BNDC bot, and either `sent`, `partial`, `failed`, or
    `suppressed` publication statuses on touched topics. After publishing is
    enabled, new successful posts should be `sent`; `suppressed` indicates the
    deployed publishing toggle is still false.

## Checkpoint Ownership

The new editor owns `topic_editor_checkpoints`. At first run it mirrors the
current `live_update_checkpoints` row into `topic_editor_checkpoints` and then
advances only the topic checkpoint.

Before cutover:

```sql
select 'legacy' as source, checkpoint_key, guild_id, channel_id,
       last_message_id, last_message_created_at, last_run_id
from live_update_checkpoints
where environment = 'prod'
union all
select 'topic' as source, checkpoint_key, guild_id, channel_id,
       last_message_id, last_message_created_at, last_run_id
from topic_editor_checkpoints
where environment = 'prod'
order by checkpoint_key, source;
```

During normal topic-editor operation, `topic_editor_checkpoints` is the source
of truth. `live_update_checkpoints` remains untouched so legacy rollback stays
zero-cost until a rollback is requested.

## Rollback

Rollback is runtime selection plus checkpoint mirroring. Old `live_update_*`
tables remain in place.

1. Disable new publishing:

   ```bash
   TOPIC_EDITOR_PUBLISHING_ENABLED=false
   ```

2. Mirror the topic checkpoint back to the legacy checkpoint so the legacy
   editor does not reprocess the flip window. Prefer the application wrapper
   `DatabaseHandler.mirror_topic_editor_checkpoint_to_live(checkpoint_key,
   environment='prod')` when operating from code. The equivalent SQL shape is:

   ```sql
   insert into live_update_checkpoints (
     checkpoint_key, environment, guild_id, channel_id,
     last_message_id, last_message_created_at, last_run_id, state
   )
   select checkpoint_key, environment, guild_id, channel_id,
          last_message_id, last_message_created_at, last_run_id, state
   from topic_editor_checkpoints
   where environment = 'prod'
     and checkpoint_key = '<target checkpoint key>'
   on conflict (environment, checkpoint_key) do update set
     guild_id = excluded.guild_id,
     channel_id = excluded.channel_id,
     last_message_id = excluded.last_message_id,
     last_message_created_at = excluded.last_message_created_at,
     last_run_id = excluded.last_run_id,
     state = excluded.state,
     updated_at = now();
   ```

3. Select the legacy backend:

   ```bash
   LIVE_UPDATE_EDITOR_BACKEND=legacy
   ```

4. Redeploy or restart with the legacy selector.
5. Verify the next live-update pass writes a completed row to
   `live_update_editor_runs` and advances `live_update_checkpoints` from the
   mirrored message id.
6. Leave topic-editor data in place for diagnosis. Do not delete
   `topics`, `topic_transitions`, `topic_editor_runs`, or
   `topic_editor_checkpoints` during rollback.

## Post-Flip Watch

Check these after the first production run and then periodically while the
operator remains paged in:

```sql
select run_id, status, error_message, published_count, failed_publish_count,
       latency_ms, cost_usd
from topic_editor_runs
where environment = 'prod'
order by started_at desc
limit 5;
```

```sql
select topic_id, headline, publication_status, publication_error,
       discord_message_ids, updated_at
from topics
where environment = 'prod'
order by updated_at desc
limit 20;
```

```sql
select action, payload->>'outcome' as outcome, count(*) as n
from topic_transitions
where environment = 'prod'
  and created_at >= now() - interval '2 hours'
group by action, payload->>'outcome'
order by n desc;
```

Investigate immediately if:

- the latest `topic_editor_runs.status` is `failed`;
- no run appears within one scheduler interval plus grace;
- any topic remains `pending` after a completed publishing-enabled run;
- `publication_status` is `failed` or `partial`;
- rejected write calls dominate accepted calls;
- trace messages are missing from the configured trace channel.

## What Not To Use

Do not use these stale requirements for this sprint:

- Phase-1 soak sign-off;
- Week-1 gate approval;
- shadow-mode trace gate;
- 20-window replay or replay-divergence rubric;
- Branch A / Branch B legacy suppression logic;
- `live_update_editor_runs` as the new-editor lifecycle table.

`live_update_editor_runs` is legacy rollback history only. The new editor
records run lifecycle in `topic_editor_runs`.
