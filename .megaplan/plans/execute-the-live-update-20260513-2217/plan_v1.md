# Implementation Plan: Live-Update Topic Editor Cutover

## Overview
Build the v6 topic-centered live-update editor as the primary production path while keeping the legacy `live_update_*` tables as rollback state. The current runtime enters through `src/features/summarising/summariser_cog.py`, instantiates `LiveUpdateEditor` from `src/features/summarising/live_update_editor.py`, and persists legacy runs/checkpoints/feed/watchlist state through `src/common/storage_handler.py`. The redesign should add a new `src/features/summarising/topic_editor.py` path, new topic storage helpers, migrations/backfill scripts, and focused tests, while avoiding changes to legacy `live_update_editor.py` and `live_update_prompts.py` except invocation suppression or explicit legacy marking.

The sprint brief intentionally removes shadow mode and decision gates. That means the implementation still needs cheap pre-flip validation, a publishing-off replay, strong rollback checkpoint mirroring, and observable failure signals, but should not build a parallel long-running validation framework.

## Phase 1: Database And Migration Foundation

### Step 1: Complete Supabase migration readiness (`.migrations_staging/`, workspace Supabase repo)
**Scope:** Medium
1. Verify local Supabase CLI auth/project linkage and apply the existing backlog migration `.migrations_staging/20260512185328_live_update_watchlist_lifecycle.sql` to dev first, then prod.
2. Update `.migrations_staging/README.md` if the migration handoff process has changed, because it currently says direct writes to the workspace-level Supabase repo were blocked.
3. Confirm `live_update_watchlist` has `expires_at`, `next_revisit_at`, `revisit_count`, `origin_reason`, and `evidence` before backfill work starts.

### Step 2: Add Phase-1 topic schema migration (`.migrations_staging/20260513*_live_update_topic_editor_phase1.sql`)
**Scope:** Large
1. Create `topics`, `topic_sources`, `topic_aliases`, `topic_transitions`, `editorial_observations`, and the checkpoint/lease support chosen for `topic_editor_runs` / `topic_editor_checkpoints`.
2. Include required indexes: topic state/revisit/headline trigram, source message lookup, transition topic/run/action indexes, and any lease uniqueness on `(environment, guild_id, live_channel_id)`.
3. Enable `pg_trgm` in the migration if not already enabled, because collision detection depends on trigram similarity.
4. Add check constraints only for locked states/actions where they will not block the agreed rejected-call shape: `rejected_post_simple`, `rejected_post_sectioned`, and `rejected_watch` need nullable `topic_id`.

### Step 3: Backfill legacy prod state into topics (`scripts/backfill_live_update_topics.py`)
**Scope:** Large
1. Read existing `live_update_feed_items` into `topics WHERE state='posted'` with publication fields populated and `topic_sources` rows derived from `source_message_ids` or ordered Discord/source IDs where available.
2. Read existing `live_update_watchlist` into `topics WHERE state='watching'`, preserving headline/revisit metadata and source message IDs.
3. Generate canonical keys with the same canonicalizer used by runtime code so migration/backfill and live writes cannot diverge.
4. Make the backfill idempotent with upserts on `(environment, guild_id, canonical_key)` and `(topic_id, message_id)`.
5. Print parity counts for legacy posted/watching rows vs new topic rows.

## Phase 2: Storage API And Core Domain Logic

### Step 4: Add topic storage helpers (`src/common/storage_handler.py`)
**Scope:** Large
1. Add CRUD/search helpers for `topics`, `topic_sources`, `topic_aliases`, `topic_transitions`, `editorial_observations`, `topic_editor_runs`, and checkpoint reads/writes.
2. Add a lease acquisition/release helper keyed by `(environment, guild_id, live_channel_id)` that fails closed if a run is already active.
3. Add a one-time checkpoint mirror helper from `live_update_checkpoints.last_message_id` into the new checkpoint owner and a rollback mirror helper back to `live_update_checkpoints`.
4. Keep legacy helper methods intact for existing tests and rollback.

### Step 5: Implement canonicalization and collision logic (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Implement the locked canonicalizer: lowercase headline, replace non-`[a-z0-9]` runs with `-`, strip edges, prepend creator Discord name when extractable, append `YYYY-MM-DD`.
2. Resolve proposed keys through `topic_aliases` before creating topics.
3. Implement collision detection: canonical-key prefix match OR trigram headline similarity `>= 0.55` with author overlap `>= 1`.
4. Implement `override_collisions` acceptance and log every override to `topic_transitions` for threshold tuning.

## Phase 3: New Topic Editor Agent

### Step 6: Build the new editor module (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Create `TopicEditor` with a `run_once(trigger)` API compatible with `SummarizerCog` expectations.
2. Fetch new archived messages from the new checkpoint, active watching topics from `topics`, and recent topic context from the new storage helpers.
3. Use native Anthropic tool use through the existing `ClaudeClient.client.messages.create(...)` surface rather than the legacy text-JSON `LiveUpdateCandidateGenerator`.
4. Define the 10 tools from v6: `search_topics`, `search_messages`, `get_author_profile`, `get_message_context`, `post_simple_topic`, `post_sectioned_topic`, `watch_topic`, `update_topic_source_messages`, `discard_topic`, and `record_observation`.
5. Keep the prompt short and structural; do not port the legacy imperative `live_update_prompts.py` rules.

### Step 7: Implement dispatcher invariants (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Reject `post_simple_topic` when source messages include `>= 2` distinct authors or `>= 3` source messages.
2. Run canonical/alias/similarity collision scans for every `post_*` and `watch_topic` write, returning a tool error unless matching `override_collisions` are provided.
3. Enforce idempotency on `(run_id, tool_call_id)` before applying write side effects.
4. Rely on the database unique constraint for `(topic_id, message_id)` and handle duplicate inserts as idempotent source attachment, not run failure.
5. Write `topic_transitions` rows for all accepted actions and for all dispatcher rejections using the locked rejected-call action names.
6. Cap `record_observation` by prompt guidance to roughly 3/run and store observations for later 30-day retention cleanup.

### Step 8: Add publishing renderer (`src/features/summarising/topic_editor.py` or `src/features/summarising/topic_publisher.py`)
**Scope:** Medium
1. Implement pure `render_topic(topic) -> list[DiscordMessage]` with no DB or Discord side effects.
2. Render simple topics as one message with headline, body, up to 4 media URLs, and original-post jump link.
3. Render sectioned topics as one header plus one section message per section, each with caption/media/source jump link.
4. Render story updates with `Update:` and a parent header link from `parent_topic_id` / parent `discord_message_ids[0]`.
5. Add a publisher wrapper that sends rendered messages only when publishing is enabled and updates `topics.publication_status`, `discord_message_ids`, attempts, errors, and `last_published_at`.

### Step 9: Add trace embed and replay mode (`src/features/summarising/topic_editor.py`, `scripts/run_live_update_dev.py`)
**Scope:** Medium
1. Emit a trace embed/file to the configured daily-updates/debug channel with run ID, source count, tool calls, topic outcomes, rejections, overrides, observations, token/cost metadata, latency, and publishing state.
2. Support a publishing-off sanity replay over the most recent prod source-message window without writing Discord posts.
3. Make replay output comparable by printing/rendering the would-publish Discord messages and topic transitions.

## Phase 4: Runtime Cutover

### Step 10: Wire the new editor as primary (`src/features/summarising/summariser_cog.py`)
**Scope:** Medium
1. Import and instantiate `TopicEditor` instead of `LiveUpdateEditor` for scheduled runs, `--summary-now`, and owner `summarynow`.
2. Preserve explicit legacy entrypoints only where they are clearly named legacy/backfill/rollback.
3. Add the run-source discriminator required by the runbook, preferably `metadata.editor = 'topic_editor' | 'legacy_live_update_editor'` if schema churn is lower than a new column.
4. Disable legacy editor invocation without editing `live_update_editor.py` or `live_update_prompts.py` except for minimal compatibility/legacy marking if tests require it.

### Step 11: Add configuration and rollback controls (`src/features/summarising/summariser_cog.py`, deployment env)
**Scope:** Medium
1. Define the publishing toggle name and suppression value used by the runbook, e.g. `TOPIC_EDITOR_PUBLISHING_ENABLED=false` for replay and `true` for prod publishing.
2. Define whether suppression is scheduler-level or publish-call-level; for this sprint, prefer scheduler-level primary routing plus a publisher-level safety flag so replay cannot accidentally send Discord messages.
3. Document bot identities and trace channel IDs in the runbook once implementation chooses exact values.
4. Implement rollback instructions to re-point `SummarizerCog` to `LiveUpdateEditor` and mirror the new checkpoint back to `live_update_checkpoints` first.

## Phase 5: Tests And Verification

### Step 12: Add focused unit tests (`tests/test_topic_editor_*.py`)
**Scope:** Large
1. Test canonicalizer stability, creator prefixing, date suffixing, and alias resolution.
2. Test all four dispatcher invariants, including the v6 collision override path and rejected transition logging.
3. Test idempotent replay of the same `(run_id, tool_call_id)`.
4. Test `topic_sources` duplicate message behavior.
5. Test `render_topic` for simple, sectioned, story-update, and partial publish failure handling.

### Step 13: Update runtime wiring tests (`tests/test_live_runtime_wiring.py`)
**Scope:** Medium
1. Update fakes so `summarynow`, startup `--summary-now`, and scheduled live pass call `TopicEditor` by default.
2. Keep tests that prove legacy daily summary remains explicit-only.
3. Add a rollback/legacy mode test if a config flag is used to select legacy editor.

### Step 14: Preserve or mark legacy tests (`tests/test_live_update_editor_*.py`, `tests/test_live_update_prompts.py`)
**Scope:** Medium
1. Keep existing legacy tests passing if practical because old tables remain rollback state.
2. If any tests assert primary invocation of the legacy editor, update them to assert legacy-only behavior or mark them as legacy compatibility tests.
3. Do not weaken tests that cover legacy publisher safety, checkpointing, or feed item persistence unless equivalent topic-editor coverage exists.

### Step 15: Run validation in increasing cost order
**Scope:** Medium
1. Run SQL syntax validation for new migrations and targeted unit tests first.
2. Run `pytest tests/test_topic_editor_*.py tests/test_live_runtime_wiring.py`.
3. Run the existing live-update test files to confirm rollback compatibility.
4. Run the broader suite if targeted tests pass.
5. Run one publishing-off sanity replay against the most recent prod source-message window and inspect rendered output plus trace embed.
6. Flip publishing on in prod only after the schema, backfill parity, lease, checkpoint mirror, trace, and replay checks pass.

## Execution Order
1. Finish migrations and backfill before runtime code depends on new tables.
2. Build storage helpers before the editor dispatcher so tests can use fake and real-ish storage through one interface.
3. Implement canonicalizer/collision logic before the LLM loop; these are deterministic and cheap to test.
4. Implement tool dispatch and renderer before runner cutover.
5. Wire `SummarizerCog` last, after topic-editor tests prove write and no-publish modes.
6. Run publishing-off replay before enabling prod publishing.
7. Mirror checkpoints before rollback or cutover so neither editor reprocesses the flip window.

## Validation Order
1. `jq`/SQL lint or dry-run migration checks for schema files.
2. Focused unit tests for canonicalization, dispatcher invariants, renderer, and storage fakes.
3. Runtime wiring tests for scheduled/manual/startup paths.
4. Legacy live-update tests for rollback confidence.
5. Publishing-off replay and trace inspection.
6. Prod flip verification through `topics.publication_status`, `topic_transitions` rejected/override rates, run rows, and Discord-side post attribution.
