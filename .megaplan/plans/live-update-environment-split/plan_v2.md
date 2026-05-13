# Implementation Plan: Live-update / top-creations dev↔prod environment split + dedupe + same-channel routing

## Overview

Replace the `dry_run` short-circuit in `LiveUpdateEditor` and `LiveTopCreations` with a full-fidelity rehearsal mode driven by an `environment` column ('prod' default, 'dev' for dev rehearsals). Every read and write touching the live-update / top-creations tables must be scoped by environment, including the unique-constraint keys that drive upserts and the pre-insert lookups that gate update-vs-insert. Top-creations collapses onto the live-update summary channel and gains a row-level pre-publish dedupe via `(environment, duplicate_key)`.

Repository specifics confirmed from inspection:
- Migrations live at `.migrations_staging/` (verified — 4 existing SQL files use `YYYYMMDDhhmmss_description.sql`).
- All live-update / top-creation storage methods live in `src/common/storage_handler.py` (~lines 652–1862); mirrored wrappers in `src/common/db_handler.py` (~lines 254–624).
- `self.dry_run` is referenced in **four** places beyond the `run_once` short-circuits: `live_top_creations.py:326,404,434` and `live_update_editor.py:1347`. All must be replaced with `self.environment == "dev"` checks (or the surrounding logic refactored) — not just the `run_once` short-circuits.
- `scripts/send_social_picks.py:116` calls `get_recent_live_update_feed_items(...)` with **keyword** args, so adding a trailing `environment=...` kwarg with default `"prod"` is safe — but `environment` must be appended at the end of each method signature, never inserted in the middle.
- Upserts use single-column `on_conflict` keys today (`checkpoint_key`, `duplicate_key`). Adding `environment` to the payload without changing the DB unique constraint will cause dev rows to overwrite prod rows. The unique constraints themselves must change to be `(environment, <key>)`.
- Several writes do manual pre-insert lookups by `(natural_key, guild_id)` to decide update-vs-insert (`store_live_top_creation_post` ~1810, `upsert_live_update_editorial_memory` ~1673, `upsert_live_update_watchlist` ~1726). These lookups must also filter by environment, or a dev run will mutate a prod row.

Note on FLAG-001: a fresh repo scan **does** find `.migrations_staging/` with existing SQL migrations (`20260411220000_backfill_payments.sql`, etc.). The earlier reviewer's claim that the directory is missing was incorrect; the migration convention stated in the original plan is the right one.

## Phase 1: Schema + storage layer

### Step 1: Add environment column + corrected unique constraints (`.migrations_staging/`)
**Scope:** Small
1. **Create** `.migrations_staging/<ts>_live_update_environment_split.sql`:
   - `alter table … add column environment text not null default 'prod'` on: `live_update_runs`, `live_update_candidates`, `live_update_decisions`, `live_update_feed_items`, `live_update_checkpoints`, `live_update_editorial_memory`, `live_update_watchlist`, `live_update_duplicate_state`, `live_top_creation_runs`, `live_top_creation_posts`, `live_top_creation_checkpoints`.
2. **Drop + recreate composite unique constraints** so upserts compose with environment (FLAG-004):
   - `live_update_checkpoints`: replace `unique(checkpoint_key)` with `unique(environment, checkpoint_key)`.
   - `live_update_duplicate_state`: replace `unique(duplicate_key)` with `unique(environment, duplicate_key)`.
   - `live_top_creation_checkpoints`: replace `unique(checkpoint_key)` with `unique(environment, checkpoint_key)`.
   - `live_top_creation_posts`: add `create unique index live_top_creation_posts_env_dupkey_uniq on live_top_creation_posts (environment, duplicate_key) where status = 'posted'`.
3. **Inspect** the live schema first to capture the exact existing constraint names; the migration must `alter table … drop constraint <name>` before adding the new ones (write the migration after grepping the current schema or an existing migration).

### Step 2: Stamp/filter `environment` everywhere in `src/common/storage_handler.py`
**Scope:** Large
1. **Append `environment: str = "prod"` at the end of every signature** (never insert mid-list) for the methods listed below; pass through to inserts (`payload["environment"] = environment`), update filters (`.eq("environment", environment)`), and selects (`.eq("environment", environment)`):
   - Writes: `create_live_update_run` (652), `update_live_update_run` (674), `store_live_update_candidate(s)` (682/704), `update_live_update_candidate_status` (712), `store_live_update_decision` (724), `store_live_update_feed_item` (738), `update_live_update_feed_item_messages` (758), `upsert_live_update_duplicate_state` (825), `upsert_live_update_checkpoint` (858), `upsert_live_update_editorial_memory` (1653), `upsert_live_update_watchlist` (1709), `create_live_top_creation_run` (1763), `update_live_top_creation_run` (1779), `store_live_top_creation_post` (1786), `upsert_live_top_creation_checkpoint` (1848).
   - Reads: `get_recent_live_update_feed_items` (773), `find_live_update_duplicate` (799), `get_live_update_checkpoint` (840), `get_live_update_context_for_messages` (988), `_get_author_live_update_stats` (1088), `search_live_update_messages` (1145), `get_live_update_context_for_message_ids` (1183), `get_live_update_author_profile` (1220), `get_live_update_message_engagement_context` (1237), `get_live_update_recent_reaction_events` (1317), `_live_update_participant_profiles` (1555), `search_live_update_feed_items` (1571), `search_live_update_editorial_memory` (1616), `get_live_update_editorial_memory` (1694), `get_live_update_watchlist` (1748), `get_live_top_creation_checkpoint` (1831).
2. **Fix on-conflict keys** in upserts that previously keyed on a single column (FLAG-004): pass `on_conflict="environment,checkpoint_key"` for both checkpoint upserts and `on_conflict="environment,duplicate_key"` for `upsert_live_update_duplicate_state`.
3. **Fix pre-insert lookup scoping** (FLAG-005): in `store_live_top_creation_post` (~1810), `upsert_live_update_editorial_memory` (~1673), and `upsert_live_update_watchlist` (~1726), add `.eq("environment", environment)` to the existing `(natural_key, guild_id)` lookup so update-vs-insert decisions never cross environments.
4. **Add** `get_live_top_creation_post_by_duplicate_key(environment: str, duplicate_key: str)` after `store_live_top_creation_post` — returns the latest `status='posted'` row or None, scoped by both args.
5. **Leave** `get_archived_messages_after_checkpoint` untouched.

### Step 3: Mirror in `src/common/db_handler.py`
**Scope:** Medium
1. **Append `environment: str = "prod"` as the last kwarg** on each wrapper (254–624 list) and forward through (FLAG-002 — append-only signature change keeps keyword callers like `send_social_picks.py:116` working).
2. **Add** wrapper for `get_live_top_creation_post_by_duplicate_key`.

## Phase 2: Editor + top-creations runtime

### Step 4: Convert `src/features/summarising/live_update_editor.py` to environment-aware
**Scope:** Large
1. **Constructor:** add `environment: str = "prod"`, store as `self.environment`. Keep `dry_run_lookback_hours`. Delete `dry_run` constructor param.
2. **`run_once` (line 110):** delete the `if self.dry_run` short-circuit. After `get_live_update_checkpoint(...)` at 113, if it returns None and `self.environment == "dev"`, synthesize an in-memory checkpoint at `now - dry_run_lookback_hours` for the rest of the run.
3. **All 42 `self.db.*` sites** (113–1317): pass `environment=self.environment` as the final kwarg. Skip the two `get_archived_messages_after_checkpoint` calls (151, 313).
4. **Replace every other `self.dry_run` reference** with `self.environment == "dev"` (FLAG-003) — including `live_update_editor.py:1347`. Audit the file with grep before finishing the step; any remaining `self.dry_run` must be deliberately removed or rewritten.
5. **Delete** `_run_dry_once` (289–427) and `_publish_dry_run_debug_report` (430–463). If any helper they call is only used by them, drop it too; if shared with the prod path, leave it.
6. **Channel resolution:** unchanged.

### Step 5: Convert `src/features/summarising/live_top_creations.py`
**Scope:** Large
1. **Constructor:** add `environment: str = "prod"`; delete `dry_run`, `top_gens_channel_id`, and `live_channel_id` constructor params (verify against cog + dev script — both will be updated in Step 6).
2. **`run_once` (line 82):** delete the `if self.dry_run` short-circuit. After `get_live_top_creation_checkpoint` (90), if None and `environment == "dev"`, synthesize a checkpoint at `now - dry_run_lookback_hours`.
3. **All 8 `self.db.*` sites** (90, 93, 137, 175, 204, 336, 376): pass `environment=self.environment` (skip the two `get_archived_messages_after_checkpoint` reads).
4. **Replace every remaining `self.dry_run` reference** (FLAG-003) — `live_top_creations.py:326, 404, 434` — with `self.environment == "dev"`. Final grep check: `grep -n "self.dry_run" src/features/summarising/live_top_creations.py` must return nothing.
5. **`_resolve_top_channel_id` (401–429):** collapse to summary channel only. Dev → `DEV_SUMMARY_CHANNEL_ID` (fallback `DEV_LIVE_UPDATE_CHANNEL_ID`). Prod → guild `summary_channel_id`. Stop reading `DEV_TOP_GENS_CHANNEL_ID` and server `top_gens_channel_id`.
6. **Pre-publish dedupe in `_publish_candidate` (~line 320, before `_send_without_mentions`):**
   ```python
   existing = await self.db.get_live_top_creation_post_by_duplicate_key(
       candidate["duplicate_key"], environment=self.environment
   )
   if existing:
       return _SkipResult(reason="duplicate_existing_post")
   ```
   Thread the skip into the run-summary counters consumed by `update_live_top_creation_run` at 137/175/204.
7. **Remove `max_posts` cap:** delete `DEFAULT_MAX_POSTS = 5` (42) and the `pending[: self.max_posts]` slices at 159, 238. Replace with a single `MAX_POSTS_SAFETY_BELT = 50` module constant slice as a runaway guardrail.
8. **Keep** the checkpoint's `posted_duplicate_keys` as a soft optimization — DB lookup + unique index is the source of truth.

### Step 6: Wire `environment` through callers (`summariser_cog.py`, `scripts/run_live_update_dev.py`)
**Scope:** Small
1. **`summariser_cog.py` ~64, ~71:** pass `environment="dev" if self.dev_mode else "prod"` to both constructors. Remove the `dry_run=self.dev_mode` arg. Remove any `top_gens_channel_id` / `live_channel_id` args passed to `LiveTopCreations` (none remain after Step 5 constructor cleanup).
2. **`scripts/run_live_update_dev.py` lines 129–140:** drop `dry_run=True`; pass `environment="dev"`. Keep `dry_run_lookback_hours=args.lookback_hours`.

## Phase 3: Tests + verification

### Step 7: Update existing live-update tests (`tests/test_live_*`)
**Scope:** Medium
1. **`test_live_update_editor_publishing.py`:** flip "Dry run posts without persisting editor state" and "Dry run debug report explains skipped candidate" to "Dev env posts AND persists with environment='dev'"; delete the debug-report test (Step 4.5 removes the helper).
2. **`test_live_update_editor_publishing.py`:** rename "Dry run prefers dev channel env" to "Dev env resolves DEV_LIVE_UPDATE_CHANNEL_ID".
3. **`test_live_top_creations.py`:** flip "Dry run posts without persisting state" to dev-persistence; flip "Dry run prefers dev channel env" to assert resolution to the **summary** channel (DEV_SUMMARY_CHANNEL_ID), not top-gens. Augment "Suppresses repeated posts from checkpoint" to cover checkpoint reset survival via the new DB dedupe lookup.
4. **`test_live_update_editor_lifecycle.py`, `test_live_storage_wrappers.py`, `test_live_runtime_wiring.py`:** thread `environment` through mocks and assert wrapper forwarding.
5. **In-memory DB fakes** used by tests: extend to honor `environment` filtering and unique-constraint scoping so isolation is asserted, not assumed.

### Step 8: Add cross-environment isolation + dedupe tests
**Scope:** Small
1. **New test in `test_live_top_creations.py`:** run twice over the same archived message (reactions ≥ 5) → exactly one `store_live_top_creation_post` call and one Discord send; second pass invokes `get_live_top_creation_post_by_duplicate_key` and short-circuits.
2. **New test in `test_live_update_editor_publishing.py` (or `test_live_runtime_wiring.py`):** seed dev-env rows; run a prod editor pass; assert they don't appear in `get_recent_live_update_feed_items`, watchlist, editorial memory, or top-creation dedupe lookup.

### Step 9: Audit pass + run suite
**Scope:** Small
1. **Storage grep:** in `src/common/storage_handler.py`, every `select` / `update` / `upsert` against `live_update_*` or `live_top_creation_*` (except `live_update_archived_messages`) must include `.eq("environment", environment)` or have `environment` in payload / on_conflict. Confirm zero misses.
2. **Runtime grep:** `grep -n "self.dry_run" src/features/summarising/live_*.py` must return nothing (FLAG-003 finality).
3. **Pytest:** `pytest tests/test_live_*.py -x` then `pytest -x`.

## Execution Order
1. Schema migration (Step 1) — composite unique constraints land with the column add.
2. Storage signatures + on-conflict + pre-insert lookup scoping (Step 2).
3. db_handler wrappers (Step 3).
4. Editor + top-creations runtime (Steps 4–5), including every `self.dry_run` rewrite.
5. Cog + dev script wiring (Step 6).
6. Tests (Steps 7–8).
7. Audit + full suite (Step 9).

## Validation Order
1. `pytest tests/test_live_storage_wrappers.py tests/test_live_top_creations.py tests/test_live_update_editor_publishing.py tests/test_live_update_editor_lifecycle.py -x`.
2. `pytest tests/test_live_runtime_wiring.py tests/test_live_admin_health.py -x`.
3. Full `pytest -x`.
4. Greps in Step 9.1 + 9.2 (storage and runtime audits).
5. (info) `scripts/run_live_update_dev.py --once` against a real Supabase; confirm dev rows tagged `environment='dev'`; subsequent prod pass does not surface them.
