# Live-update editor sprint plan

Single sprint. Build → ship → fix what breaks.

Design reference: `docs/live-update-editor-redesign.md` (v6). Cutover ops reference: `docs/live-update-editor-phase-2-runbook.md`.

## Locked decisions

1. **Canonicalizer.** `slug = re.sub(r'[^a-z0-9]+', '-', headline.lower()).strip('-')`. Prepend creator-discord-name when extractable. Append `YYYY-MM-DD`. Tune via prod override rate.
2. **Similarity threshold.** Collision iff `canonical_key_prefix_match` OR (`pg_trgm(headline_a, headline_b) >= 0.55` AND `author_overlap >= 1`). Tune via prod override rate.
3. **`editorial_observations`.** Agent discretion, ~3/run cap via prompt, 30-day retention.
4. **Rejected-call schema.** Single-table: `topic_transitions` with nullable `topic_id`, `action IN ('rejected_post_simple', 'rejected_post_sectioned', 'rejected_watch')`.
5. **`defer` / `pending_review`.** Drop entirely. No `pending_review` state, no `request_review_topic` tool. If usage emerges from missing posts, re-add later as a tool + state.
6. **Checkpoint ownership.** New editor owns its own checkpoint table (`topic_editor_checkpoints` or column on `topic_editor_runs`). On flip, mirror current `live_update_checkpoints.last_message_id` once as the starting position. On rollback, mirror new checkpoint position back to `live_update_checkpoints` so the legacy editor doesn't reprocess the flip window.

## Work

- Complete the Supabase OAuth handshake that's been blocked since the watchlist migration
- Apply backlog migration `.migrations_staging/20260512185328_live_update_watchlist_lifecycle.sql` to dev + prod
- Write Phase-1 migration with the five tables + `topic_editor_runs` per v6 redesign (`topics` with publication columns folded in; `topic_sources` UNIQUE on `(topic_id, message_id)`; `topic_aliases`; `topic_transitions` with `environment`, `guild_id`, nullable `topic_id`; `editorial_observations`)
- Apply to dev + prod Supabase
- Backfill: existing prod `live_update_feed_items` → `topics WHERE state='posted'` + matching `topic_sources` + populated publication columns. Existing `live_update_watchlist` → `topics WHERE state='watching'`.
- New module `src/features/summarising/topic_editor.py`. Implement the 10 tools (4 read + 6 decision) per v6 redesign §"Tool surface"
- Implement all 4 dispatcher invariants:
  1. `post_simple_topic` rejected on `distinct_author_count >= 2` OR `len(source_message_ids) >= 3`
  2. Every write runs canonical-key + similarity collision scan; returns matching topics as tool error unless `override_collisions` is supplied
  3. Idempotency on `(run_id, tool_call_id)`
  4. `topic_sources` `(topic_id, message_id)` uniqueness
- Implement key canonicalizer + alias resolution
- Every dispatcher rejection writes a `topic_transitions` row with `action='rejected_*'`
- Unit tests for each invariant + rejection path
- `render_topic(topic) -> list[DiscordMessage]` pure function:
  - Simple: one message with headline + body + media (up to 4 URLs) + original-post jump-link
  - Sectioned: header message + one per section, each with caption + media + per-section jump-link
  - Story update: prefix `Update:` + parent header-link
- Trace embed format. Posts to `#daily-updates` alongside each run.
- Concurrent-run lease on `topic_editor_runs` keyed by `(environment, guild_id, live_channel_id)`
- Wire new editor into the runner as primary. Disable legacy `live_update_editor.py` / `live_update_prompts.py` from being invoked.
- Sanity replay against the most recent prod source-message window, publishing OFF. Eyeball output.
- Flip publishing ON in prod.

## After ship

- Override rate visible in `topic_transitions` (`COUNT(action='override') / COUNT(action LIKE 'post_%')`)
- Partial-publish failures visible via `topics WHERE publication_status='partial'`
- Fix whatever shows up. Tune the similarity threshold if override rate >10%.
- After 2 weeks of clean operation: rename `live_update_*` → `_legacy_live_update_*`. Schedule deletion 2 weeks later.

## Rollback

Edit runner config to point back at `live_update_editor.py`. Mirror new checkpoint position back to `live_update_checkpoints.last_message_id` per the checkpoint-ownership decision so the legacy editor doesn't reprocess the flip window. Old data otherwise untouched.
