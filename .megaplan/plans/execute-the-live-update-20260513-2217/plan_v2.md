# Implementation Plan: Live-Update Topic Editor Cutover

## Overview
Build the v6 topic-centered live-update editor as the primary production path while keeping the legacy `live_update_*` tables as rollback state. The critique does not show that the plan targeted the wrong root cause: the core problem is still the legacy editor's prompt/JSON/candidate architecture, and the requested fix is still a new topic editor. The critique does show that the first plan left operational seams ambiguous: run lifecycle, lease semantics, DB wrapper access, admin/health visibility, and contradictory runbook gates. This revision keeps the same implementation direction but makes those seams explicit.

The current runtime enters through `src/features/summarising/summariser_cog.py`, which passes `bot.db_handler` into the active editor. Legacy editor persistence is split across `src/common/db_handler.py` wrapper methods and async methods in `src/common/storage_handler.py`. The new implementation must therefore add both storage helpers and `db_handler` wrappers, not bypass the existing dependency shape.

The sprint plan supersedes the older runbook's Phase-1 soak, 20-window replay, and formal decision gate. The runbook still matters for operational checks and rollback, but it must be edited so operators are not asked to satisfy stale cutover gates. The cutover validation for this sprint is one publishing-off sanity replay, then publishing ON in prod with observable rollback.

## Phase 1: Database And Migration Foundation

### Step 1: Complete Supabase migration readiness (`.migrations_staging/`, workspace Supabase repo)
**Scope:** Medium
1. Verify local Supabase CLI auth/project linkage and apply the existing backlog migration `.migrations_staging/20260512185328_live_update_watchlist_lifecycle.sql` to dev first, then prod.
2. Update `.migrations_staging/README.md` if the migration handoff process has changed, because it currently says direct writes to the workspace-level Supabase repo were blocked.
3. Confirm `live_update_watchlist` has `expires_at`, `next_revisit_at`, `revisit_count`, `origin_reason`, and `evidence` before backfill work starts.

### Step 2: Add Phase-1 topic schema migration (`.migrations_staging/20260513*_live_update_topic_editor_phase1.sql`)
**Scope:** Large
1. Create the five topic tables from v6: `topics`, `topic_sources`, `topic_aliases`, `topic_transitions`, and `editorial_observations`.
2. Create a dedicated `topic_editor_runs` table as the new editor's run lifecycle table. Do not write new editor runs to `live_update_editor_runs`; that table remains legacy rollback history.
3. Create `topic_editor_checkpoints` as the new checkpoint owner per locked decision #6.
4. Add `topic_editor_runs` columns needed for operations: `run_id`, `environment`, `guild_id`, `live_channel_id`, `trigger`, `status`, `started_at`, `completed_at`, `lease_expires_at`, `checkpoint_before`, `checkpoint_after`, counts, error fields, and `metadata` for cost/latency/tool-call summaries.
5. Implement concurrent-run protection as a partial unique index on active rows only, for example `(environment, guild_id, live_channel_id) WHERE status = 'running'`. Do not use a plain unique constraint over all historical runs.
6. In lease acquisition code, expire stale running rows first when `lease_expires_at < now()` by marking them failed/stale, then insert the new `running` row.
7. Include required indexes: topic state/revisit/headline trigram, source message lookup, transition topic/run/action indexes, topic-editor run status/created indexes, and checkpoint uniqueness.
8. Enable `pg_trgm` in the migration if not already enabled, because collision detection depends on trigram similarity.
9. Add check constraints only where they will not block the agreed rejected-call shape: `rejected_post_simple`, `rejected_post_sectioned`, and `rejected_watch` need nullable `topic_id`.

### Step 3: Backfill legacy prod state into topics (`scripts/backfill_live_update_topics.py`)
**Scope:** Large
1. Read existing `live_update_feed_items` into `topics WHERE state='posted'` with publication fields populated and `topic_sources` rows derived from `source_message_ids` where available.
2. Read existing `live_update_watchlist` into `topics WHERE state='watching'`, preserving headline/revisit metadata and source message IDs.
3. Generate canonical keys with the same canonicalizer used by runtime code so migration/backfill and live writes cannot diverge.
4. Make the backfill idempotent with upserts on `(environment, guild_id, canonical_key)` and `(topic_id, message_id)`.
5. Print parity counts for legacy posted/watching rows vs new topic rows.

## Phase 2: Storage API And Core Domain Logic

### Step 4: Add topic storage helpers (`src/common/storage_handler.py`, `src/common/db_handler.py`)
**Scope:** Large
1. Add async storage methods in `src/common/storage_handler.py` for `topics`, `topic_sources`, `topic_aliases`, `topic_transitions`, `editorial_observations`, `topic_editor_runs`, and `topic_editor_checkpoints`.
2. Add matching wrapper methods in `src/common/db_handler.py` for every helper the new editor calls, preserving the dependency shape used by `SummarizerCog` and legacy tests.
3. Add `create_topic_editor_run`, `complete_topic_editor_run`, `fail_topic_editor_run`, and `acquire_topic_editor_run_lease` helpers that use the active-row partial unique index and stale lease expiry semantics from Step 2.
4. Add checkpoint mirror helpers: one-time legacy-to-topic mirror from `live_update_checkpoints.last_message_id` into `topic_editor_checkpoints`, and rollback mirror from `topic_editor_checkpoints` back to `live_update_checkpoints`.
5. Keep all legacy live-update helper methods intact for existing tests and rollback.

### Step 5: Implement canonicalization and collision logic (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Implement the locked canonicalizer: lowercase headline, replace non-`[a-z0-9]` runs with `-`, strip edges, prepend creator Discord name when extractable, append `YYYY-MM-DD`.
2. Resolve proposed keys through `topic_aliases` before creating topics.
3. Implement collision detection: canonical-key prefix match OR trigram headline similarity `>= 0.55` with author overlap `>= 1`.
4. Implement `override_collisions` acceptance and log every override to `topic_transitions` for threshold tuning.
5. Keep this logic deterministic and unit-testable outside the Anthropic loop.

## Phase 3: New Topic Editor Agent

### Step 6: Build the new editor module (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Create `TopicEditor` with a `run_once(trigger)` API compatible with `SummarizerCog` expectations.
2. Accept the existing injected `db_handler` object and call the new `src/common/db_handler.py` wrappers, not `storage_handler.py` directly.
3. Fetch new archived messages from `topic_editor_checkpoints`, active watching topics from `topics`, and recent topic context from new topic storage helpers.
4. Use native Anthropic tool use through the existing `ClaudeClient.client.messages.create(...)` surface rather than the legacy text-JSON `LiveUpdateCandidateGenerator`.
5. Define the 10 tools from v6: `search_topics`, `search_messages`, `get_author_profile`, `get_message_context`, `post_simple_topic`, `post_sectioned_topic`, `watch_topic`, `update_topic_source_messages`, `discard_topic`, and `record_observation`.
6. Keep the prompt short and structural; do not port the legacy imperative `live_update_prompts.py` rules.
7. Create `topic_editor_runs` rows for each run and link all `topic_transitions.run_id` values to that table's `run_id`.

### Step 7: Implement dispatcher invariants (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Reject `post_simple_topic` when source messages include `>= 2` distinct authors or `>= 3` source messages.
2. Run canonical/alias/similarity collision scans for every `post_*` and `watch_topic` write, returning a tool error unless matching `override_collisions` are provided.
3. Enforce idempotency on `(run_id, tool_call_id)` before applying write side effects.
4. Rely on the database unique constraint for `(topic_id, message_id)` and handle duplicate source inserts as idempotent source attachment, not run failure.
5. Write `topic_transitions` rows for all accepted actions and for all dispatcher rejections using the locked rejected-call action names.
6. Cap `record_observation` by prompt guidance to roughly 3/run and store observations for later 30-day retention cleanup.

### Step 8: Add publishing renderer (`src/features/summarising/topic_editor.py` or `src/features/summarising/topic_publisher.py`)
**Scope:** Medium
1. Implement pure `render_topic(topic) -> list[DiscordMessage]` with no DB or Discord side effects.
2. Render simple topics as one message with headline, body, up to 4 media URLs, and original-post jump link.
3. Render sectioned topics as one header plus one section message per section, each with caption/media/source jump link.
4. Render story updates with `Update:` and a parent header link from `parent_topic_id` / parent `discord_message_ids[0]`.
5. Add a publisher wrapper that sends rendered messages only when publishing is enabled and updates `topics.publication_status`, `discord_message_ids`, attempts, errors, and `last_published_at`.
6. Use `TOPIC_EDITOR_PUBLISHING_ENABLED=false` as the default suppression value until the explicit prod flip.

### Step 9: Add trace embed and one-shot replay (`src/features/summarising/topic_editor.py`, `scripts/run_live_update_dev.py`)
**Scope:** Medium
1. Emit a trace embed/file to the configured daily-updates/operator channel with run ID, source count, tool calls, topic outcomes, rejections, overrides, observations, token/cost metadata, latency, and publishing state.
2. Support one publishing-off sanity replay over the most recent prod source-message window.
3. Make replay output comparable by printing/rendering the would-publish Discord messages and topic transitions.
4. Do not build the older 20-window replay soak or Phase-1 decision gate; that contradicts the sprint's hard scope.

## Phase 4: Runtime Cutover And Operational Surfaces

### Step 10: Wire the new editor as primary (`src/features/summarising/summariser_cog.py`)
**Scope:** Medium
1. Import and instantiate `TopicEditor` as the default active editor for scheduled runs, `--summary-now`, and owner `summarynow`.
2. Preserve the existing constructor injection seam for tests by keeping the `live_update_editor` keyword as the injectable active editor, or deliberately rename it while updating all callers and tests in the same change.
3. Add a rollback selector such as `LIVE_UPDATE_EDITOR_BACKEND=legacy` that instantiates the legacy `LiveUpdateEditor` only when explicitly requested.
4. Preserve explicit legacy/backfill entrypoints and legacy daily summary behavior.
5. Disable default legacy live-update invocation without modifying `live_update_editor.py` or `live_update_prompts.py` except for minimal invocation suppression or legacy labels if required.

### Step 11: Update admin chat and health visibility (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`, `src/features/health/health_check_cog.py`, `tests/test_live_admin_health.py`)
**Scope:** Medium
1. Add `topics`, `topic_sources`, `topic_aliases`, `topic_transitions`, `editorial_observations`, `topic_editor_runs`, and `topic_editor_checkpoints` to admin-query allowlists where appropriate.
2. Update live-update status tools to report topic-editor runs, topic counts by state, recent transitions/rejections, publication partial/failed rows, and override rate from `topic_transitions`.
3. Keep legacy table counts visible under a clearly labeled rollback/legacy section rather than presenting them as primary status.
4. Update health checks to consider `topic_editor_runs` as primary after cutover and retain legacy run checks only for rollback mode.
5. Add/adjust tests in `tests/test_live_admin_health.py` and admin-chat tests so operator status cannot silently report stale legacy-only state.

### Step 12: Reconcile cutover runbook (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. Edit the runbook to state that `docs/live-update-editor-sprint-plan.md` supersedes the old Phase-1 soak/gate and 20-window replay requirements for this sprint.
2. Replace the stale pre-flip gates with the sprint gates: schema applied, backfill parity, active-run lease verified, checkpoint mirrored, admin/health status updated, trace embed configured, one publishing-off sanity replay completed, and publishing toggle ready.
3. Update all run queries to use `topic_editor_runs` for the new editor and legacy `live_update_editor_runs` only for rollback/legacy checks.
4. Document the rollback procedure: set backend to legacy, mirror `topic_editor_checkpoints` back to `live_update_checkpoints`, then re-enable legacy invocation.
5. Fill or explicitly list remaining operator-provided values: publishing toggle, trace channel, bot identity, and rollback backend selector.

## Phase 5: Tests And Verification

### Step 13: Add focused unit tests (`tests/test_topic_editor_*.py`)
**Scope:** Large
1. Test canonicalizer stability, creator prefixing, date suffixing, and alias resolution.
2. Test all four dispatcher invariants, including the v6 collision override path and rejected transition logging.
3. Test idempotent replay of the same `(run_id, tool_call_id)`.
4. Test `topic_sources` duplicate message behavior.
5. Test `render_topic` for simple, sectioned, story-update, and partial publish failure handling.
6. Test active-run lease behavior: concurrent acquire is rejected, completed runs do not block future runs, and stale running rows can be expired before a new run starts.

### Step 14: Update runtime wiring tests (`tests/test_live_runtime_wiring.py`)
**Scope:** Medium
1. Update fakes so `summarynow`, startup `--summary-now`, and scheduled live pass call the injected active editor seam by default.
2. Verify the default concrete active editor is `TopicEditor` when no fake is injected.
3. Keep tests that prove legacy daily summary remains explicit-only.
4. Add a rollback-mode test showing the legacy `LiveUpdateEditor` can be selected deliberately.

### Step 15: Preserve or mark legacy tests (`tests/test_live_update_editor_*.py`, `tests/test_live_update_prompts.py`)
**Scope:** Medium
1. Keep existing legacy tests passing if practical because old tables remain rollback state.
2. If any tests assert primary invocation of the legacy editor, update them to assert legacy-only behavior or mark them as legacy compatibility tests.
3. Do not weaken tests that cover legacy publisher safety, checkpointing, or feed item persistence unless equivalent topic-editor coverage exists.

### Step 16: Run validation in increasing cost order
**Scope:** Medium
1. Run SQL syntax validation for new migrations and targeted unit tests first.
2. Run `pytest tests/test_topic_editor_*.py tests/test_live_runtime_wiring.py tests/test_live_admin_health.py`.
3. Run the existing live-update test files to confirm rollback compatibility.
4. Run targeted admin-chat/status tests if present, then the broader suite if targeted tests pass.
5. Run one publishing-off sanity replay against the most recent prod source-message window and inspect rendered output plus trace embed.
6. Flip publishing on in prod only after schema, backfill parity, active-run lease, checkpoint mirror, admin/health status, trace, runbook, and replay checks pass.

## Execution Order
1. Reconcile the run model first: dedicated `topic_editor_runs`, `topic_editor_checkpoints`, and no shared `live_update_editor_runs.metadata.editor` for new-editor runs.
2. Finish migrations and backfill before runtime code depends on new tables.
3. Build `storage_handler.py` helpers and matching `db_handler.py` wrappers before the editor dispatcher.
4. Implement canonicalizer/collision logic before the LLM loop; these are deterministic and cheap to test.
5. Implement tool dispatch, lease lifecycle, and renderer before runner cutover.
6. Update admin/health surfaces before prod flip so operators do not lose status visibility.
7. Wire `SummarizerCog` last, preserving the injection seam used by tests.
8. Edit the runbook before publishing ON so it no longer contains contradictory gate instructions.
9. Run publishing-off replay before enabling prod publishing.
10. Mirror checkpoints before rollback or cutover so neither editor reprocesses the flip window.

## Validation Order
1. SQL lint/dry-run checks for schema files.
2. Focused unit tests for canonicalization, dispatcher invariants, lease behavior, renderer, and storage fakes.
3. `db_handler` wrapper tests or fakes proving the editor can call every required helper through the injected dependency.
4. Runtime wiring tests for scheduled/manual/startup paths and rollback selector.
5. Admin chat and health-check tests for topic-editor status visibility.
6. Legacy live-update tests for rollback confidence.
7. Publishing-off replay and trace inspection.
8. Prod flip verification through `topic_editor_runs`, `topics.publication_status`, `topic_transitions` rejected/override rates, admin/health status, and Discord-side post attribution.
