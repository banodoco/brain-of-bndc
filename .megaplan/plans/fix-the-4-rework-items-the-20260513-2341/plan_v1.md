# Implementation Plan: Topic Editor Rework Items

## Overview
Patch the existing topic-editor implementation without touching `src/features/summarising/live_update_editor.py` or `src/features/summarising/live_update_prompts.py`. The concrete touch points are `src/features/summarising/topic_editor.py`, `src/common/storage_handler.py`, `src/common/db_handler.py`, both copies of `20260513230500_live_update_topic_editor_phase1.sql`, focused tests under `tests/`, and the dry-run backfill report path.

The redesign doc says each state change writes a `topic_transitions` row and every override is logged for audit, while tool calls are idempotent on `(run_id, tool_call_id)`. That points away from updating the rejected row in place and toward allowing multiple transition events per tool call while keeping one canonical replay row for the tool result.

## Main Phase

### Step 1: Fix transition uniqueness while preserving audit rows (`.migrations_staging/20260513230500_live_update_topic_editor_phase1.sql`, `../supabase/migrations/20260513230500_live_update_topic_editor_phase1.sql`)
**Scope:** Medium
1. Add an event sequence column to `topic_transitions`, e.g. `tool_call_sequence integer NOT NULL DEFAULT 0`, with a nonnegative check.
2. Replace `topic_transitions_tool_call_idx` so uniqueness is on `(run_id, tool_call_id, tool_call_sequence)` where `tool_call_id IS NOT NULL`.
3. Keep the main accepted/rejected transition at sequence `0`; write override audit rows for the same tool call at sequence `1..N`.
4. Apply the same SQL changes to both migration copies and verify they stay identical for this migration file.

### Step 2: Store replayable tool results in transition payloads (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Introduce a small helper to construct the returned tool outcome before writing the transition, then store that return object under `payload["tool_result"]` for write tools.
2. Update `_dispatch_create_topic_tool()` around `topic_editor.py:495` so the accepted transition includes `tool_result`, and override transitions from `build_override_transitions()` use sequence numbers rather than colliding with the accepted row.
3. Update `_reject_create_tool()`, `_dispatch_update_sources()`, `_dispatch_discard()`, and `record_observation` paths so every persisted write-tool transition has enough payload to reconstruct the previous return value.
4. Keep read-only tools (`search_topics`, `search_messages`, `get_author_profile`, `get_message_context`) in-memory only unless current code already persists them; the requested DB replay requirement applies to write tools.

### Step 3: Add DB-backed idempotent replay (`src/common/storage_handler.py`, `src/common/db_handler.py`, `src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Add storage/db wrappers such as `get_topic_transition_for_tool_call(run_id, tool_call_id, environment)` that query `topic_transitions` for `tool_call_sequence = 0`, ordered/limited defensively.
2. Replace `_is_idempotent_replay()` at `topic_editor.py:645` with a replay lookup that first checks the DB for `(run_id, tool_call_id)` before executing write-tool side effects.
3. If a stored transition is found, return `payload.tool_result` when present; otherwise reconstruct a conservative result from the transition row (`tool_call_id`, inferred `tool`, `outcome`, `action`, `error`, `topic_id`) so older rows remain readable.
4. Keep the in-memory `seen_tool_call_ids` as a cheap same-process guard, but do not let it be the source of truth for write-tool idempotency.

### Step 4: Add stale lease expiry (`.migrations_staging/`, `../supabase/migrations/`, `src/common/storage_handler.py`)
**Scope:** Medium
1. Add `lease_expires_at timestamptz` to `topic_editor_runs`, with an index useful for finding stale running leases.
2. In `StorageHandler.acquire_topic_editor_run()` at `storage_handler.py:882`, read `TOPIC_EDITOR_LEASE_TIMEOUT_MINUTES` with default `30`, compute a lease expiry timestamp, and include it in the inserted running row.
3. Before inserting a new run, expire matching stale rows for `(environment, guild_id, live_channel_id)` by marking `status='failed'` or `status='skipped'`, setting `completed_at`, and recording a clear `skipped_reason`/`error_message` such as `stale_lease_expired`.
4. Treat only rows whose `status='running'` and `lease_expires_at < now()` as stealable; active non-expired leases should still cause `acquire_topic_editor_run()` to return `None` via the existing unique lease behavior.

### Step 5: Add regression tests (`tests/test_topic_editor_runtime.py`, `tests/test_live_storage_wrappers.py`, migration tests if present)
**Scope:** Medium
1. Extend `FakeDB` in `tests/test_topic_editor_runtime.py` with the new transition lookup behavior and sequence-aware transition storage.
2. Add an end-to-end override test that simulates DB uniqueness and proves accepted + override transitions for the same `tool_call_id` both persist.
3. Add a restart replay test: pre-seed a transition for `(run_id, tool_call_id)`, run the same write tool through a fresh `TopicEditor`/context, and assert no `upsert_topic`, `add_topic_source`, or duplicate transition occurs while the prior result is returned.
4. Add a storage-wrapper test for stale lease expiry behavior, checking the update-before-insert query shape and the default/configurable timeout.
5. Update any migration/schema assertions to expect `tool_call_sequence` and `lease_expires_at` in both migration copies.

### Step 6: Validate backfill parity against prod in dry-run mode (`scripts/backfill_live_update_topics.py`, `reports/`)
**Scope:** Small
1. Run `scripts/backfill_live_update_topics.py --environment prod --dry-run` using the existing Supabase service credentials; do not run without `--dry-run`.
2. Capture a structured report, preferably `reports/live-update-topic-backfill-parity-20260513.json`, containing environment, dry-run flag, legacy feed row count, legacy watchlist row count, generated posted topic count, generated watching topic count, source counts, and any deltas/mismatches.
3. If the current script only prints text, either add a `--report-json PATH` option or collect the dry-run output into a small structured artifact without changing write behavior.
4. Surface mismatches explicitly; do not apply the backfill as part of this sprint.

### Step 7: Run focused validation first, then broader tests
**Scope:** Small
1. Run targeted tests first: `pytest tests/test_topic_editor_core.py tests/test_topic_editor_runtime.py tests/test_live_storage_wrappers.py tests/test_backfill_live_update_topics.py`.
2. Run any relevant migration/schema test if the repo has one.
3. Finish with the existing topic-editor/live-update targeted suite used by the prior sprint, then the broader test suite if runtime is acceptable.
4. Confirm guarded files `src/features/summarising/live_update_editor.py` and `src/features/summarising/live_update_prompts.py` have no diff.

## Execution Order
1. Update the migrations first so runtime/test decisions match the intended schema.
2. Add storage/db helper wrappers before changing dispatcher idempotency.
3. Patch `TopicEditor` transition payloads and sequence handling.
4. Add tests for override, replay, and stale lease behavior.
5. Run tests, then perform the prod dry-run backfill parity check and write the structured report.

## Validation Order
1. Cheap static checks: compare migration copies and grep guarded files for no diff.
2. Focused unit/regression tests for `topic_editor`, storage wrappers, and backfill helpers.
3. Dry-run prod backfill parity command with structured report artifact.
4. Broader pytest run once the targeted checks are green.
