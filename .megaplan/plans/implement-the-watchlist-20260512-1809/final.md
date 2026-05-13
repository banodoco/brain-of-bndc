# Execution Checklist

- [x] **T1:** Create migration file `.migrations_staging/<ts>_live_update_watchlist_lifecycle.sql` adding `expires_at timestamptz`, `next_revisit_at timestamptz`, `revisit_count int NOT NULL DEFAULT 0`, `origin_reason text`, `evidence jsonb` to `live_update_watchlist`. Drop any CHECK constraint on `status` (or document it's plain text). Backfill `expires_at = COALESCE(created_at, now()) + interval '72 hours'` and `next_revisit_at = COALESCE(created_at, now()) + interval '6 hours'` for existing rows.
  Executor notes: Migration created with all 5 columns, CHECK constraint drop, backfills.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/live-update-environment-split.md
    - .megaplan/debt.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/.plan.lock
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/faults.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/final.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_snapshot.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_v1_raw.txt
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate_signals_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/phase_result.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.meta.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/state.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_gate_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_plan_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/user_actions.md
    - .megaplan/plans/live-update-environment-split/.plan.lock
    - .megaplan/plans/live-update-environment-split/critique_output.json
    - .megaplan/plans/live-update-environment-split/critique_v1.json
    - .megaplan/plans/live-update-environment-split/execution.json
    - .megaplan/plans/live-update-environment-split/execution_audit.json
    - .megaplan/plans/live-update-environment-split/execution_batch_1.json
    - .megaplan/plans/live-update-environment-split/execution_batch_2.json
    - .megaplan/plans/live-update-environment-split/execution_batch_3.json
    - .megaplan/plans/live-update-environment-split/execution_batch_4.json
    - .megaplan/plans/live-update-environment-split/execution_batch_5.json
    - .megaplan/plans/live-update-environment-split/execution_batch_6.json
    - .megaplan/plans/live-update-environment-split/execution_batch_7.json
    - .megaplan/plans/live-update-environment-split/execution_batch_8.json
    - .megaplan/plans/live-update-environment-split/execution_batch_9.json
    - .megaplan/plans/live-update-environment-split/faults.json
    - .megaplan/plans/live-update-environment-split/final.md
    - .megaplan/plans/live-update-environment-split/finalize.json
    - .megaplan/plans/live-update-environment-split/finalize_output.json
    - .megaplan/plans/live-update-environment-split/finalize_snapshot.json
    - .megaplan/plans/live-update-environment-split/gate.json
    - .megaplan/plans/live-update-environment-split/plan_v1.md
    - .megaplan/plans/live-update-environment-split/plan_v1.meta.json
    - .megaplan/plans/live-update-environment-split/plan_v2.md
    - .megaplan/plans/live-update-environment-split/plan_v2.meta.json
    - .megaplan/plans/live-update-environment-split/review.json
    - .megaplan/plans/live-update-environment-split/state.json
    - .megaplan/plans/live-update-environment-split/step_receipt_critique_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_execute_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_finalize_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_plan_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_revise_v2.json
    - .megaplan/plans/live-update-environment-split/user_actions.md
    - .megaplan/schemas/directors_notes.json
    - .megaplan/schemas/execution_doc.json
    - .megaplan/schemas/finalize.json
    - .megaplan/schemas/review.json
    - .megaplan/schemas/revise.json
    - docs/debug-utility-validation.md
    - docs/live-editor-watchlist-plan.md
    - docs/live-update-editor-legacy-boundaries.md
    - main.py
    - scripts/backfill_guild_ids.py
    - scripts/debug.py
    - scripts/debug_live_editor_audit.py
    - scripts/discord_tools.py
    - scripts/inspect_editor_reasoning.py
    - scripts/run_live_update_dev.py
    - scripts/send_social_picks.py
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/features/admin_chat/agent.py
    - src/features/admin_chat/tools.py
    - src/features/archive/archive_cog.py
    - src/features/gating/intro_embed.py
    - src/features/health/health_check_cog.py
    - src/features/summarising/live_top_creations.py
    - src/features/summarising/live_update_editor.py
    - src/features/summarising/live_update_prompts.py
    - src/features/summarising/summariser.py
    - src/features/summarising/summariser_cog.py
    - structure.md
    - tests/test_live_admin_health.py
    - tests/test_live_runtime_wiring.py
    - tests/test_live_storage_wrappers.py
    - tests/test_live_top_creations.py
    - tests/test_live_update_editor_lifecycle.py
    - tests/test_live_update_editor_publishing.py
    - tests/test_live_update_prompts.py
    - tests/test_social_route_tools.py

- [x] **T2:** Add `insert_live_update_watchlist(watch_key, title, origin_reason, source_message_ids, channel_id, subject_type, environment, guild_id)` to `src/common/db_handler.py`. Idempotent by `(environment, watch_key)`. Sets `expires_at = now()+72h`, `next_revisit_at = now()+6h`, `evidence` jsonb snapshot of source_message_ids/channel/title, `status='active'`, `revisit_count=0`. Also add `update_live_update_watchlist(watch_key, action, notes, environment)` — `publish_now` sets `status='published'`, `extend` bumps `next_revisit_at = least(now()+'6 hours', expires_at)` and increments `revisit_count`, `discard` sets `status='discarded'` and stores `notes`. Both methods delegate to `storage_handler` counterparts. Document relationship with existing `upsert_live_update_watchlist` (which handles the `last_matched_*` update path).
  Depends on: T1
  Executor notes: Added insert_live_update_watchlist and update_live_update_watchlist to db_handler.py (sync wrappers delegating to storage_handler). Added matching async implementations in storage_handler.py: insert is idempotent by environment/watch_key, sets expires_at=now+72h, next_revisit_at=now+6h, evidence jsonb, status=active, revisit_count=0. Update handles publish_now→published, extend→bumps next_revisit_at capped at expires_at + increments revisit_count, discard→discarded+notes. Documented relationship with existing upsert_live_update_watchlist.
  Files changed:
    - src/common/db_handler.py
    - src/common/storage_handler.py

- [x] **T3:** Rewrite `get_live_update_watchlist` in `db_handler.py` to: (a) run an idempotent UPDATE sweep BEFORE the SELECT to auto-archive rows where `expires_at < now()` (set `status='archived'`, append `notes='ttl_expired'`); (b) SELECT active rows (status IN ('active','published')); (c) compute `revisit_state` in Python: `fresh` if `now() < next_revisit_at`, `revisit_due` if past `next_revisit_at` but before `created_at+24h`, `last_call` if past `created_at+24h`; (d) group results into `{fresh: [...], revisit_due: [...], last_call: [...]}` capped at 20 per state, most-recent-first; (e) enforce 50-row active cap across ALL states combined — auto-archive oldest rows beyond cap (regardless of state). Return the grouped dict.
  Depends on: T2
  Executor notes: UPDATE sweep via execute_query (parameterized SQL with guild_id/environment scoping). Storage handler get_live_update_watchlist now returns all rows (status filter removed per T4). Timeline computation handles missing timestamps gracefully with fallback estimation from created_at. Cap enforcement auto-archives oldest rows regardless of revisit_state. Returns grouped dict {fresh, revisit_due, last_call}. Verified: caller pass-through compatible (guild_id positional, environment keyword).
  Files changed:
    - src/common/db_handler.py

- [x] **T4:** Update `src/common/storage_handler.py`: add `insert_live_update_watchlist` and `update_live_update_watchlist` async methods that write the new columns (`expires_at`, `next_revisit_at`, `revisit_count`, `origin_reason`, `evidence`). Update `get_live_update_watchlist` to SELECT `*` without the old `status='active'` filter (the caller now handles filtering). Ensure the existing `upsert_live_update_watchlist` at line 1733 is left intact for the `last_matched_*` update path.
  Depends on: T2
  Executor notes: Removed `status` parameter and `.eq('status', status)` filter from storage_handler.get_live_update_watchlist. Bumped default limit from 100 to 200 since caller filters in Python. insert_live_update_watchlist and update_live_update_watchlist already exist from T2 execution (lines 1789, 1859). upsert_live_update_watchlist at line 1733 verified untouched — payload still uses original columns.
  Files changed:
    - src/common/storage_handler.py

- [x] **T5:** In `src/features/summarising/live_update_prompts.py`: (a) Add `watchlist_add` and `watchlist_update` tool definitions to the `available_tools` array (line ~469) matching the brief schemas; (b) Fix the watchlist rendering block (line ~450-457) — replace the broken `watch_type`/`description` fields with schema-correct `subject_type`/`notes` and render as state-grouped structure `{_explanation, fresh, revisit_due, last_call}` with fields `{watch_key, title, origin_reason, age_hours, source_message_ids, subject_type, channel_id}` consumed from the new grouped dict returned by `get_live_update_watchlist`.
  Depends on: T3
  Executor notes: (a) Added watchlist_add and watchlist_update tool definitions to available_tools array at lines 559-579 with correct schemas matching the brief (watch_key, title, reason, source_message_ids, channel_id, subject_type for add; watch_key, action, notes for update). (b) Fixed watchlist rendering block: replaced broken watch_type/description fields with schema-correct subject_type/notes. Extracted rendering into new _render_watchlist static method that handles state-grouped structure {_explanation, fresh, revisit_due, last_call} with fields {watch_key, title, origin_reason, age_hours, source_message_ids, subject_type, channel_id, notes}. Computes age_hours inline from created_at, falls back to evidence.source_message_ids when source_message_ids is absent, handles non-dict/None gracefully. (c) Updated build_user_prompt signature from List[Dict[str, Any]] to Dict[str, Any] to match T3's grouped dict return type. Throwaway verification script confirmed: no watch_type/description in output, state-grouped structure correct, age_hours computed, evidence fallback works, empty/non-dict inputs handled. All 36 tests across 3 test files pass.
  Files changed:
    - src/features/summarising/live_update_prompts.py

- [x] **T6:** Wire watchlist tool dispatch in `src/features/summarising/live_update_editor.py` method `_run_editor_tool` (line ~357): add `watchlist_add` → calls `self.db.insert_live_update_watchlist(...)` and `watchlist_update` → calls `self.db.update_live_update_watchlist(...)`. Both return structured success/error payload. Record watchlist actions into a `watchlist_actions` list on the candidate generator alongside the existing `tool_trace` in `last_agent_trace`. Thread `environment` and `guild_id` from the editor context.
  Depends on: T2, T5
  Executor notes: Added watchlist_add and watchlist_update handlers in _run_editor_tool (live_update_editor.py). watchlist_add calls self.db.insert_live_update_watchlist with watch_key, title, reason (mapped to origin_reason), source_message_ids, channel_id, subject_type, environment=self.environment, guild_id=guild_id. watchlist_update calls self.db.update_live_update_watchlist with watch_key, action, notes, environment=self.environment. Both return structured {ok: bool, watchlist_entry/error, action} payloads. Watchlist actions recorded into self.candidate_generator.watchlist_actions list (initialized in LiveUpdateCandidateGenerator.__init__, reset in generate_candidates). watchlist_actions included in last_agent_trace alongside tool_trace. Also added watchlist_actions to _agent_run_metadata for metadata persistence. All 36 tests pass.
  Files changed:
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_audit.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_2.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_3.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_4.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_5.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_execute_v1.json

- [x] **T7:** Remove the per-run publish cap in `src/features/summarising/live_update_editor.py`: (a) Change `DEFAULT_MAX_PUBLISH_PER_RUN = None` at line 60; (b) Rewrite the `__init__` expression at lines 101-104 — replace `self.max_publish_per_run = max(1, int(max_publish_per_run or os.getenv('LIVE_UPDATE_MAX_POSTS_PER_RUN', self.DEFAULT_MAX_PUBLISH_PER_RUN)))` with `self.max_publish_per_run = None` plus optional env-var path: `env_val = os.getenv('LIVE_UPDATE_MAX_POSTS_PER_RUN'); if env_val is not None: self.max_publish_per_run = max(1, int(env_val))` (also respect the `max_publish_per_run` constructor arg); (c) Guard the publish slice in `_select_publishable_candidates` at line 453 — only truncate `sorted_candidates[:self.max_publish_per_run]` when `self.max_publish_per_run is not None`.
  Executor notes: DEFAULT=None, init conditional, slice guarded. No int(None) path.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/live-update-environment-split.md
    - .megaplan/debt.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/.plan.lock
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/faults.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/final.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_snapshot.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_v1_raw.txt
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate_signals_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/phase_result.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.meta.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/state.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_gate_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_plan_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/user_actions.md
    - .megaplan/plans/live-update-environment-split/.plan.lock
    - .megaplan/plans/live-update-environment-split/critique_output.json
    - .megaplan/plans/live-update-environment-split/critique_v1.json
    - .megaplan/plans/live-update-environment-split/execution.json
    - .megaplan/plans/live-update-environment-split/execution_audit.json
    - .megaplan/plans/live-update-environment-split/execution_batch_1.json
    - .megaplan/plans/live-update-environment-split/execution_batch_2.json
    - .megaplan/plans/live-update-environment-split/execution_batch_3.json
    - .megaplan/plans/live-update-environment-split/execution_batch_4.json
    - .megaplan/plans/live-update-environment-split/execution_batch_5.json
    - .megaplan/plans/live-update-environment-split/execution_batch_6.json
    - .megaplan/plans/live-update-environment-split/execution_batch_7.json
    - .megaplan/plans/live-update-environment-split/execution_batch_8.json
    - .megaplan/plans/live-update-environment-split/execution_batch_9.json
    - .megaplan/plans/live-update-environment-split/faults.json
    - .megaplan/plans/live-update-environment-split/final.md
    - .megaplan/plans/live-update-environment-split/finalize.json
    - .megaplan/plans/live-update-environment-split/finalize_output.json
    - .megaplan/plans/live-update-environment-split/finalize_snapshot.json
    - .megaplan/plans/live-update-environment-split/gate.json
    - .megaplan/plans/live-update-environment-split/plan_v1.md
    - .megaplan/plans/live-update-environment-split/plan_v1.meta.json
    - .megaplan/plans/live-update-environment-split/plan_v2.md
    - .megaplan/plans/live-update-environment-split/plan_v2.meta.json
    - .megaplan/plans/live-update-environment-split/review.json
    - .megaplan/plans/live-update-environment-split/state.json
    - .megaplan/plans/live-update-environment-split/step_receipt_critique_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_execute_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_finalize_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_plan_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_revise_v2.json
    - .megaplan/plans/live-update-environment-split/user_actions.md
    - .megaplan/schemas/directors_notes.json
    - .megaplan/schemas/execution_doc.json
    - .megaplan/schemas/finalize.json
    - .megaplan/schemas/review.json
    - .megaplan/schemas/revise.json
    - docs/debug-utility-validation.md
    - docs/live-editor-watchlist-plan.md
    - docs/live-update-editor-legacy-boundaries.md
    - main.py
    - scripts/backfill_guild_ids.py
    - scripts/debug.py
    - scripts/debug_live_editor_audit.py
    - scripts/discord_tools.py
    - scripts/inspect_editor_reasoning.py
    - scripts/run_live_update_dev.py
    - scripts/send_social_picks.py
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/features/admin_chat/agent.py
    - src/features/admin_chat/tools.py
    - src/features/archive/archive_cog.py
    - src/features/gating/intro_embed.py
    - src/features/health/health_check_cog.py
    - src/features/summarising/live_top_creations.py
    - src/features/summarising/live_update_editor.py
    - src/features/summarising/live_update_prompts.py
    - src/features/summarising/summariser.py
    - src/features/summarising/summariser_cog.py
    - structure.md
    - tests/test_live_admin_health.py
    - tests/test_live_runtime_wiring.py
    - tests/test_live_storage_wrappers.py
    - tests/test_live_top_creations.py
    - tests/test_live_update_editor_lifecycle.py
    - tests/test_live_update_editor_publishing.py
    - tests/test_live_update_prompts.py
    - tests/test_social_route_tools.py

- [x] **T8:** Soften the cap language in the prompt in `src/features/summarising/live_update_prompts.py`: (a) Line 319: replace 'Be much more willing to return zero candidates than to fill the feed...' with 'Returning zero is fine when nothing meets the bar. Returning multiple is fine when multiple do. The bar is the bar; quantity follows from it.'; (b) Lines 349-353: replace 'into at most one normal public feed candidate per run...' with 'Publish every candidate that genuinely meets the bar. There is no per-run cap. Most hours will have 0-2; busy hours can have more. Do not pad to fill space, and do not artificially compress when multiple items qualify.'
  Executor notes: Prompt language softened. Old 'at most one'/'much more willing' language removed.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/live-update-environment-split.md
    - .megaplan/debt.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/.plan.lock
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/faults.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/final.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_snapshot.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_v1_raw.txt
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate_signals_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/phase_result.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.meta.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/state.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_gate_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_plan_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/user_actions.md
    - .megaplan/plans/live-update-environment-split/.plan.lock
    - .megaplan/plans/live-update-environment-split/critique_output.json
    - .megaplan/plans/live-update-environment-split/critique_v1.json
    - .megaplan/plans/live-update-environment-split/execution.json
    - .megaplan/plans/live-update-environment-split/execution_audit.json
    - .megaplan/plans/live-update-environment-split/execution_batch_1.json
    - .megaplan/plans/live-update-environment-split/execution_batch_2.json
    - .megaplan/plans/live-update-environment-split/execution_batch_3.json
    - .megaplan/plans/live-update-environment-split/execution_batch_4.json
    - .megaplan/plans/live-update-environment-split/execution_batch_5.json
    - .megaplan/plans/live-update-environment-split/execution_batch_6.json
    - .megaplan/plans/live-update-environment-split/execution_batch_7.json
    - .megaplan/plans/live-update-environment-split/execution_batch_8.json
    - .megaplan/plans/live-update-environment-split/execution_batch_9.json
    - .megaplan/plans/live-update-environment-split/faults.json
    - .megaplan/plans/live-update-environment-split/final.md
    - .megaplan/plans/live-update-environment-split/finalize.json
    - .megaplan/plans/live-update-environment-split/finalize_output.json
    - .megaplan/plans/live-update-environment-split/finalize_snapshot.json
    - .megaplan/plans/live-update-environment-split/gate.json
    - .megaplan/plans/live-update-environment-split/plan_v1.md
    - .megaplan/plans/live-update-environment-split/plan_v1.meta.json
    - .megaplan/plans/live-update-environment-split/plan_v2.md
    - .megaplan/plans/live-update-environment-split/plan_v2.meta.json
    - .megaplan/plans/live-update-environment-split/review.json
    - .megaplan/plans/live-update-environment-split/state.json
    - .megaplan/plans/live-update-environment-split/step_receipt_critique_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_execute_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_finalize_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_plan_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_revise_v2.json
    - .megaplan/plans/live-update-environment-split/user_actions.md
    - .megaplan/schemas/directors_notes.json
    - .megaplan/schemas/execution_doc.json
    - .megaplan/schemas/finalize.json
    - .megaplan/schemas/review.json
    - .megaplan/schemas/revise.json
    - docs/debug-utility-validation.md
    - docs/live-editor-watchlist-plan.md
    - docs/live-update-editor-legacy-boundaries.md
    - main.py
    - scripts/backfill_guild_ids.py
    - scripts/debug.py
    - scripts/debug_live_editor_audit.py
    - scripts/discord_tools.py
    - scripts/inspect_editor_reasoning.py
    - scripts/run_live_update_dev.py
    - scripts/send_social_picks.py
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/features/admin_chat/agent.py
    - src/features/admin_chat/tools.py
    - src/features/archive/archive_cog.py
    - src/features/gating/intro_embed.py
    - src/features/health/health_check_cog.py
    - src/features/summarising/live_top_creations.py
    - src/features/summarising/live_update_editor.py
    - src/features/summarising/live_update_prompts.py
    - src/features/summarising/summariser.py
    - src/features/summarising/summariser_cog.py
    - structure.md
    - tests/test_live_admin_health.py
    - tests/test_live_runtime_wiring.py
    - tests/test_live_storage_wrappers.py
    - tests/test_live_top_creations.py
    - tests/test_live_update_editor_lifecycle.py
    - tests/test_live_update_editor_publishing.py
    - tests/test_live_update_prompts.py
    - tests/test_social_route_tools.py

- [x] **T9:** Relax `_meets_editorial_bar` in `src/features/summarising/live_update_prompts.py` (line ~904): (a) Add `scanned_message_count: int = 0` and `is_last_call: bool = False` to the signature; (b) Showcase: `(reactions >= 3 OR reply_count >= 2 OR author_is_high_signal)` AND `has_media` (where `author_is_high_signal` degrades to `False` since it doesn't exist on candidate payloads yet); (c) Top_creation: `reactions >= 3` AND `has_media`; (d) Project_update: unchanged (already permissive); (e) Quiet-hour rule: when `scanned_message_count < 50`, drop one tier — showcase ≥2 reactions OR ≥1 reply, top_creation ≥2 reactions; (f) Last-call watchlist bar: when `is_last_call=True`, showcase/top_creation `reactions >= 2 OR reply_count >= 1` (+media), project_update `reactions >= 1 OR reply_count >= 1`. Thread `scanned_message_count` and `is_last_call` through `_message_meets_editorial_bar` (line 941), the `_decide_candidate_local` call at line 721, and the `_generate_heuristic_candidates` call at line 802. Compute `scanned_message_count` as `len(messages)` at each invoker site.
  Executor notes: New params added, thresholds relaxed, quiet-hour and last-call implemented. All 4 callers threaded.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/live-update-environment-split.md
    - .megaplan/debt.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/.plan.lock
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/faults.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/final.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_snapshot.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_v1_raw.txt
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate_signals_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/phase_result.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.meta.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/state.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_gate_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_plan_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/user_actions.md
    - .megaplan/plans/live-update-environment-split/.plan.lock
    - .megaplan/plans/live-update-environment-split/critique_output.json
    - .megaplan/plans/live-update-environment-split/critique_v1.json
    - .megaplan/plans/live-update-environment-split/execution.json
    - .megaplan/plans/live-update-environment-split/execution_audit.json
    - .megaplan/plans/live-update-environment-split/execution_batch_1.json
    - .megaplan/plans/live-update-environment-split/execution_batch_2.json
    - .megaplan/plans/live-update-environment-split/execution_batch_3.json
    - .megaplan/plans/live-update-environment-split/execution_batch_4.json
    - .megaplan/plans/live-update-environment-split/execution_batch_5.json
    - .megaplan/plans/live-update-environment-split/execution_batch_6.json
    - .megaplan/plans/live-update-environment-split/execution_batch_7.json
    - .megaplan/plans/live-update-environment-split/execution_batch_8.json
    - .megaplan/plans/live-update-environment-split/execution_batch_9.json
    - .megaplan/plans/live-update-environment-split/faults.json
    - .megaplan/plans/live-update-environment-split/final.md
    - .megaplan/plans/live-update-environment-split/finalize.json
    - .megaplan/plans/live-update-environment-split/finalize_output.json
    - .megaplan/plans/live-update-environment-split/finalize_snapshot.json
    - .megaplan/plans/live-update-environment-split/gate.json
    - .megaplan/plans/live-update-environment-split/plan_v1.md
    - .megaplan/plans/live-update-environment-split/plan_v1.meta.json
    - .megaplan/plans/live-update-environment-split/plan_v2.md
    - .megaplan/plans/live-update-environment-split/plan_v2.meta.json
    - .megaplan/plans/live-update-environment-split/review.json
    - .megaplan/plans/live-update-environment-split/state.json
    - .megaplan/plans/live-update-environment-split/step_receipt_critique_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_execute_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_finalize_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_plan_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_revise_v2.json
    - .megaplan/plans/live-update-environment-split/user_actions.md
    - .megaplan/schemas/directors_notes.json
    - .megaplan/schemas/execution_doc.json
    - .megaplan/schemas/finalize.json
    - .megaplan/schemas/review.json
    - .megaplan/schemas/revise.json
    - docs/debug-utility-validation.md
    - docs/live-editor-watchlist-plan.md
    - docs/live-update-editor-legacy-boundaries.md
    - main.py
    - scripts/backfill_guild_ids.py
    - scripts/debug.py
    - scripts/debug_live_editor_audit.py
    - scripts/discord_tools.py
    - scripts/inspect_editor_reasoning.py
    - scripts/run_live_update_dev.py
    - scripts/send_social_picks.py
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/features/admin_chat/agent.py
    - src/features/admin_chat/tools.py
    - src/features/archive/archive_cog.py
    - src/features/gating/intro_embed.py
    - src/features/health/health_check_cog.py
    - src/features/summarising/live_top_creations.py
    - src/features/summarising/live_update_editor.py
    - src/features/summarising/live_update_prompts.py
    - src/features/summarising/summariser.py
    - src/features/summarising/summariser_cog.py
    - structure.md
    - tests/test_live_admin_health.py
    - tests/test_live_runtime_wiring.py
    - tests/test_live_storage_wrappers.py
    - tests/test_live_top_creations.py
    - tests/test_live_update_editor_lifecycle.py
    - tests/test_live_update_editor_publishing.py
    - tests/test_live_update_prompts.py
    - tests/test_social_route_tools.py

- [x] **T10:** Persist `raw_text` on every run: In `src/features/summarising/live_update_editor.py`, modify `_agent_run_metadata` (line 567) to include `agent_trace.raw_text = (agent_trace.get('raw_text') or '')[:50000]`. This ensures the raw LLM output is persisted into `live_update_editor_runs.metadata.agent_trace` on every run (both success and zero-candidate paths). The `raw_text` is already being populated in `last_agent_trace` at line 217 of prompts.py — just wire it through to the metadata. For the zero-candidate/error path around line ~300, ensure the `metadata` dict passed to `update_live_update_run` also includes `raw_text` from the agent trace.
  Executor notes: raw_text[:50000] in _agent_run_metadata + error path metadata.
  Files changed:
    - .claude/scheduled_tasks.lock
    - .megaplan/briefs/live-update-environment-split.md
    - .megaplan/debt.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/.plan.lock
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/execution_batch_1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/faults.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/final.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_output.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_snapshot.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/finalize_v1_raw.txt
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/gate_signals_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/phase_result.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.md
    - .megaplan/plans/implement-the-watchlist-20260512-1809/plan_v1.meta.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/state.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_critique_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_gate_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/step_receipt_plan_v1.json
    - .megaplan/plans/implement-the-watchlist-20260512-1809/user_actions.md
    - .megaplan/plans/live-update-environment-split/.plan.lock
    - .megaplan/plans/live-update-environment-split/critique_output.json
    - .megaplan/plans/live-update-environment-split/critique_v1.json
    - .megaplan/plans/live-update-environment-split/execution.json
    - .megaplan/plans/live-update-environment-split/execution_audit.json
    - .megaplan/plans/live-update-environment-split/execution_batch_1.json
    - .megaplan/plans/live-update-environment-split/execution_batch_2.json
    - .megaplan/plans/live-update-environment-split/execution_batch_3.json
    - .megaplan/plans/live-update-environment-split/execution_batch_4.json
    - .megaplan/plans/live-update-environment-split/execution_batch_5.json
    - .megaplan/plans/live-update-environment-split/execution_batch_6.json
    - .megaplan/plans/live-update-environment-split/execution_batch_7.json
    - .megaplan/plans/live-update-environment-split/execution_batch_8.json
    - .megaplan/plans/live-update-environment-split/execution_batch_9.json
    - .megaplan/plans/live-update-environment-split/faults.json
    - .megaplan/plans/live-update-environment-split/final.md
    - .megaplan/plans/live-update-environment-split/finalize.json
    - .megaplan/plans/live-update-environment-split/finalize_output.json
    - .megaplan/plans/live-update-environment-split/finalize_snapshot.json
    - .megaplan/plans/live-update-environment-split/gate.json
    - .megaplan/plans/live-update-environment-split/plan_v1.md
    - .megaplan/plans/live-update-environment-split/plan_v1.meta.json
    - .megaplan/plans/live-update-environment-split/plan_v2.md
    - .megaplan/plans/live-update-environment-split/plan_v2.meta.json
    - .megaplan/plans/live-update-environment-split/review.json
    - .megaplan/plans/live-update-environment-split/state.json
    - .megaplan/plans/live-update-environment-split/step_receipt_critique_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_execute_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_finalize_v2.json
    - .megaplan/plans/live-update-environment-split/step_receipt_plan_v1.json
    - .megaplan/plans/live-update-environment-split/step_receipt_revise_v2.json
    - .megaplan/plans/live-update-environment-split/user_actions.md
    - .megaplan/schemas/directors_notes.json
    - .megaplan/schemas/execution_doc.json
    - .megaplan/schemas/finalize.json
    - .megaplan/schemas/review.json
    - .megaplan/schemas/revise.json
    - docs/debug-utility-validation.md
    - docs/live-editor-watchlist-plan.md
    - docs/live-update-editor-legacy-boundaries.md
    - main.py
    - scripts/backfill_guild_ids.py
    - scripts/debug.py
    - scripts/debug_live_editor_audit.py
    - scripts/discord_tools.py
    - scripts/inspect_editor_reasoning.py
    - scripts/run_live_update_dev.py
    - scripts/send_social_picks.py
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/features/admin_chat/agent.py
    - src/features/admin_chat/tools.py
    - src/features/archive/archive_cog.py
    - src/features/gating/intro_embed.py
    - src/features/health/health_check_cog.py
    - src/features/summarising/live_top_creations.py
    - src/features/summarising/live_update_editor.py
    - src/features/summarising/live_update_prompts.py
    - src/features/summarising/summariser.py
    - src/features/summarising/summariser_cog.py
    - structure.md
    - tests/test_live_admin_health.py
    - tests/test_live_runtime_wiring.py
    - tests/test_live_storage_wrappers.py
    - tests/test_live_top_creations.py
    - tests/test_live_update_editor_lifecycle.py
    - tests/test_live_update_editor_publishing.py
    - tests/test_live_update_prompts.py
    - tests/test_social_route_tools.py

- [x] **T11:** Implement REASONING-prefix prompt + parser fallback chain in `src/features/summarising/live_update_prompts.py`: (a) Insert instruction at the OUTPUT SHAPE section (~line 284): require leading `REASONING: <1-3 sentences>\n\n<json>`, redundant `editor_reasoning` inside JSON, and add the hard-requirement assertion: 'If you omit `editor_reasoning` or the `REASONING:` prefix line, your response is invalid and will be re-requested.'; (b) Refactor `_parse_raw_candidates` (line 595) with explicit fallback chain, setting both `self.last_editor_reasoning` and `self.reasoning_recovery_path`: try top-level `editor_reasoning` → aliases (`reasoning`, `editor_summary`, `editorial_reasoning`) → per-candidate `editor_reasoning` concatenation joined by ` | ` → regex `^REASONING:\s*(.+?)\n\n` on prose prefix → first ≤3 sentences before JSON span → empty with `reasoning_recovery_path='none'`; (c) Record branch name (e.g. `top_level`, `alias:reasoning`, `per_candidate`, `reasoning_prefix`, `prose_first_paragraph`, `none`) in `self.reasoning_recovery_path`; (d) Add `reasoning_recovery_path` to `last_agent_trace` dict at line 214 so it flows into metadata.
  Depends on: T8
  Executor notes: (a) Added REASONING: prefix instruction with hard-requirement wording to OUTPUT SHAPE. (b) Refactored _parse_raw_candidates with 5-branch fallback chain: top_level → alias:reasoning/editor_summary/editorial_reasoning → per_candidate concatenation → regex REASONING: prefix → prose_first_paragraph → none. (c) Records branch name in self.reasoning_recovery_path. (d) Added reasoning_recovery_path to last_agent_trace dict. Initialized in __init__.
  Files changed:
    - src/features/summarising/live_update_prompts.py

- [x] **T12:** Surface recovery telemetry: (a) In `scripts/debug_live_editor_audit.py`: add a column/section showing `reasoning_recovery_path` distribution (count per branch across the queried runs) and a watchlist-actions summary (count of `watchlist_add`/`watchlist_update` calls from `metadata.agent_trace.watchlist_actions`); (b) In `live_update_editor.py` `_build_reasoning_embed` (line 1044): add `reasoning_recovery_path` to the embed footer alongside existing run_id/model info.
  Depends on: T11, T6
  Executor notes: (a) Added Counter import, recovery_path_counter/wl accumulators. Collect reasoning_recovery_path and watchlist_actions per-run in the audit loop. Added recovery_path to per-run output. After loop, prints aggregate section: reasoning_recovery_path distribution (count per branch) and watchlist-actions summary (watchlist_add/watchlist_update counts). (b) Added embed.set_footer() in _build_reasoning_embed with run_id, model, and reasoning_recovery_path. All 36 tests pass.
  Files changed:
    - scripts/debug_live_editor_audit.py
    - src/features/summarising/live_update_editor.py

- [ ] **T13:** Write watchlist lifecycle + tool dispatch tests in `tests/test_live_update_editor_lifecycle.py`: (a) Test `watchlist_add` inserts row with correct TTL fields and is idempotent on `watch_key`; (b) Test `watchlist_update` for each action — `publish_now` sets `status='published'`, `extend` bumps `next_revisit_at` and `revisit_count`, `discard` sets `status='discarded'` with `notes`; verify `next_revisit_at` capped at `expires_at`; (c) Test `get_live_update_watchlist` state grouping (`fresh`/`revisit_due`/`last_call`) with frozen-time fixtures; verify 20-per-state cap; verify 50-row active cap across all states; verify TTL auto-archive; (d) Test publish cap removed: with `max_publish_per_run=None`, multiple candidates publish; env var still throttles when set.
  Depends on: T3, T6, T7
  Executor notes: Blocked awaiting U1: Migration .migrations_staging/20260512185328_live_update_watchlist_lifecycle.sql has NOT been applied. Verified by querying live_update_watchlist — column 'expires_at' does not exist. User must run the migration against both dev and prod Supabase environments before T13 can proceed.

- [x] **T14:** Write parser fallback chain tests in `tests/test_live_update_prompts.py` (or extend existing): (a) Five cases covering each branch — top-level `editor_reasoning`, alias key (e.g. `reasoning`), per-candidate reasoning concatenation, `REASONING:` prose prefix, first-paragraph last resort. Assert non-empty reasoning string and correct `reasoning_recovery_path` value; (b) Sixth case: totally empty input → `reasoning_recovery_path='none'`, `editor_reasoning=''`, no crash.
  Depends on: T11
  Executor notes: Added 6 tests: test_parser_fallback_top_level_editor_reasoning, test_parser_fallback_alias_reasoning, test_parser_fallback_per_candidate_concatenation, test_parser_fallback_reasoning_prefix_regex, test_parser_fallback_prose_first_paragraph, test_parser_fallback_empty_input_none. All 25 tests pass (6 new + 19 existing). Tests use real newlines for regex and prose branches. Each test asserts non-empty reasoning (branches 1-5) or empty (branch 6) and exact recovery_path value.
  Files changed:
    - tests/test_live_update_prompts.py

- [x] **T15:** Write bar-relaxation tests in `tests/test_live_update_prompts.py` or `tests/test_live_update_editor_publishing.py`: (a) Test new thresholds for showcase (reactions≥3 OR reply≥2), top_creation (reactions≥3), project_update (unchanged); (b) Test quiet-hour rule (<50 scanned messages) drops one tier; (c) Test last-call watchlist bar accepts items the normal bar would reject (showcase/top_creation with reactions≥2 OR reply≥1). Verify `author_is_high_signal` degrades to `False` gracefully.
  Depends on: T9
  Executor notes: Added 14 bar-relaxation tests: new thresholds (showcase reactions>=3 OR reply>=2, top_creation reactions>=3, project_update unchanged), quiet-hour rule (<50 messages drops one tier), last-call watchlist bar (reactions>=2 OR reply>=1), author_is_high_signal graceful degradation to False. All 30 tests pass.
  Files changed:
    - tests/test_live_update_prompts.py

- [ ] **T16:** Run the full existing test suite. Find and run tests most relevant to the changed files (`test_live_update_editor_lifecycle.py`, `test_live_update_prompts.py`, `test_live_update_editor_publishing.py`). If any test fails, read the error, fix the code, and re-run until all pass. Then write a short throwaway script that reproduces the specific watchlist rendering bug (non-existent `watch_type`/`description` fields) to confirm the fix works, then delete the script.
  Depends on: T13, T14, T15

## Watch Items

- INIT-EXPR (DEBT-015): Changing DEFAULT_MAX_PUBLISH_PER_RUN to None breaks `max(1, int(...))` at lines 101-104. Must rewrite the init expression entirely — NOT just flip the constant.
- EDITORIAL-BAR-THREADING (DEBT-017/018): `scanned_message_count` and `is_last_call` don't exist in any caller today. Enumerate every call site (`_message_meets_editorial_bar`, `_decide_candidate_local` ~line 721, `_generate_heuristic_candidates` ~line 802) and thread the new params through all of them.
- TOOL-DISPATCH-LOCATION (DEBT-019): New watchlist tools must be added to `_run_editor_tool` in live_update_editor.py (line 357), NOT just the generic `_run_requested_tool` invoker in prompts.py.
- WATCHLIST-WRITE-DUPLICATION (FLAG-004): Existing `upsert_live_update_watchlist` (db_handler:595, storage_handler:1733) coexists with new `insert_live_update_watchlist`. Document the relationship — upsert handles `last_matched_*` update path; insert handles tool-driven creation.
- EXTEND-BUMP (DEBT-023): Settle on 6h as the concrete extend bump. The `extend` action sets `next_revisit_at = least(now() + interval '6 hours', expires_at)`.
- ACTIVE-CAP (DEBT-022): Cap 50 active rows across ALL states combined, not just `fresh`. Auto-archive oldest rows regardless of state.
- SIDE-EFFECT-ON-READ (DEBT-024): Run TTL auto-archive as an idempotent UPDATE BEFORE the SELECT in `get_live_update_watchlist`, not inside the read path.
- REASONING-RECOVERY-CHANNEL (FLAG-009): Set `self.reasoning_recovery_path` on the generator instance (like `self.last_editor_reasoning`) so `_agent_run_metadata` can read it and persist into `metadata.agent_trace`.
- HARD-REQUIREMENT-WORDING: Preserve the behaviour-shaping language from brief Phase 4 item #5: 'If you omit `editor_reasoning` or the `REASONING:` prefix line, your response is invalid and will be re-requested.'
- AUTHOR-IS-HIGH-SIGNAL-DEADCODE (FLAG-006): `author_is_high_signal` does not exist on candidate payloads. Degrade to `False` — the OR clause is a no-op until the signal is implemented.
- GET_WATCHLIST-CONTRACT-BREAK: `get_live_update_watchlist` return shape changes from list-of-rows to state-grouped dict. Check all consumers: editor.py:140 passes to prompt builder at prompts.py:450. No other callers.
- STORAGE-HANDLER-COVERAGE: storage_handler.py `upsert_live_update_watchlist` at line 1738 builds payload with hardcoded keys — ensure new columns are added there if the upsert path is still used.

## Sense Checks

- **SC1** (T1): Does the migration file use timestamp naming convention matching `.migrations_staging/`, add all 5 columns, drop/replace any CHECK constraint on `status`, and backfill both `expires_at` and `next_revisit_at`?
  Executor note: (not provided)

- **SC2** (T2): Are `insert_live_update_watchlist` and `update_live_update_watchlist` idempotent, setting all lifecycle fields correctly, with the extend bump using concrete 6h and capped at `expires_at`?
  Executor note: insert is idempotent by (environment, watch_key). Sets all lifecycle fields. extend uses concrete 6h bump capped at expires_at.

- **SC3** (T3): Does `get_live_update_watchlist` sweep BEFORE select, compute 3 states correctly, cap 20/state, and enforce 50-row cap across ALL states (not just fresh)?
  Executor note: (not provided)

- **SC4** (T4): Does storage_handler.py expose matching insert/update methods and is the existing `upsert_live_update_watchlist` left intact?
  Executor note: (not provided)

- **SC5** (T5): Do `watchlist_add`/`watchlist_update` appear in `available_tools` with correct schemas, and does the watchlist rendering use `subject_type`/`notes` with state-grouped structure?
  Executor note: Verified by: (1) code review of final state; (2) all 36 tests pass; (3) throwaway verification script confirmed zero watch_type/description fields, correct state-grouped structure, age_hours computation, and evidence fallback.

- **SC6** (T6): Are both tools dispatched in `_run_editor_tool` (not just `_run_requested_tool`), returning structured payloads, with watchlist actions recorded in agent trace?
  Executor note: Both watchlist_add and watchlist_update are dispatched in _run_editor_tool (editor.py), not _run_requested_tool (prompts.py). Each returns structured {ok, watchlist_entry/error, action} payload. Watchlist actions are recorded into self.candidate_generator.watchlist_actions (a List[Dict[str,Any]]) and included in last_agent_trace alongside tool_trace. environment and guild_id are threaded from editor context (self.environment, guild_id method parameter).

- **SC7** (T7): Is `DEFAULT_MAX_PUBLISH_PER_RUN = None`, does the init expression NOT call `int(None)`, and does `_select_publishable_candidates` skip the slice when cap is None?
  Executor note: (not provided)

- **SC8** (T8): Does the prompt no longer contain 'at most one' / 'much more willing to return zero' and instead state no-cap and reasoning-prefix expectations?
  Executor note: (not provided)

- **SC9** (T9): Does `_meets_editorial_bar` accept new params, reflect new thresholds, implement quiet-hour and last-call tiers, with all callers threading the new context?
  Executor note: (not provided)

- **SC10** (T10): Is `raw_text` (truncated 50k) persisted into metadata.agent_trace on every run path (success, zero-candidate, error)?
  Executor note: (not provided)

- **SC11** (T11): Does the fallback chain implement all 5 branches + none, record `reasoning_recovery_path` correctly, and does the prompt include the REASONING-prefix instruction with hard-requirement wording?
  Executor note: All 5 branches + none implemented. reasoning_recovery_path recorded correctly. Prompt includes hard-requirement wording.

- **SC12** (T12): Does the audit script show reasoning_recovery_path distribution and watchlist actions summary? Does the dev embed footer include recovery_path?
  Executor note: Audit script now: (1) displays reasoning_recovery_path per-run, (2) prints aggregate reasoning_recovery_path distribution (count per branch) after all runs, (3) prints watchlist-actions summary with counts of watchlist_add and watchlist_update calls from metadata.agent_trace.watchlist_actions. The dev embed footer in _build_reasoning_embed now includes recovery_path alongside run_id and model via embed.set_footer().

- **SC13** (T13): Do lifecycle tests cover idempotent insert, all 3 update actions, state transitions, 20/state + 50-total caps, TTL auto-archive, and removed publish cap?
  Executor note: T13 cannot execute. Migration U1 (live_update_watchlist_lifecycle.sql) has not been applied — column 'expires_at' does not exist on the live_update_watchlist table. T13 lifecycle tests require these schema additions to be in place. Marked `awaiting U1`.

- **SC14** (T14): Do parser tests cover all 5 branches + none case, asserting non-empty reasoning and correct recovery_path values?
  Executor note: (not provided)

- **SC15** (T15): Do bar-relaxation tests verify new thresholds, quiet-hour tier drop, last-call bar acceptance, and graceful author_is_high_signal degradation?
  Executor note: Tests verify new thresholds, quiet-hour tier drop, last-call bar, and author_is_high_signal degradation.

- **SC16** (T16): Does the full test suite pass with zero failures? Did the throwaway repro script confirm the watchlist rendering bug is fixed?
  Executor note: [MISSING]

## Meta

EXECUTION ORDER: Run T1 first (migration), then T2+T7+T8 in parallel (db+cap+prompt). T2 feeds T3→T4→T5→T6. T8 feeds T11→T12. T9 is independent. T10 is independent. Tests (T13,T14,T15) can be written any time after their dependencies, but run all of them in T16 as final gate. KEY GOTCHAS: (1) T7's init rewrite MUST NOT call int(None) — use a conditional path; (2) T9 must thread `scanned_message_count` and `is_last_call` through EVERY caller including `_message_meets_editorial_bar` and `_decide_candidate_local`; (3) T14 needs `freezegun` or similar for frozen-time fixtures; (4) The `build_user_prompt` at line ~400 in prompts.py consumes the watchlist — ensure it passes the new grouped dict not the old flat list. OUT OF SCOPE: `live_top_creations`, legacy summariser, lookback changes — do not touch those files.

## Coverage Gaps

- Tasks without executor updates: 1
- Sense-check acknowledgments missing: 1
