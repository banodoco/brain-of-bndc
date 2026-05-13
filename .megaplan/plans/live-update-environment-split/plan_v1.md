# Implementation Plan: Live-update / top-creations dev↔prod environment split + dedupe + same-channel routing

## Overview

Replace the `dry_run` short-circuit in `LiveUpdateEditor` and `LiveTopCreations` with a full-fidelity rehearsal mode driven by an `environment` column ('prod' default, 'dev' for dry rehearsals). Every read and write touching the live-update / top-creations tables must be scoped by environment. Top-creations also collapses onto the live-update summary channel and gains a row-level pre-publish dedupe via `(environment, duplicate_key)`.

Repository specifics confirmed from inspection:
- Migrations live at `.migrations_staging/` using `YYYYMMDDhhmmss_description.sql`.
- All live-update / top-creation storage methods live in `src/common/storage_handler.py` (~lines 652–1862) with mirror wrappers in `src/common/db_handler.py` (~lines 254–624).
- Editor has 42 `self.db.*` call sites; top-creations has 8.
- `_run_dry_once` (editor: 289–427, top: similarly) and `_publish_dry_run_debug_report` (editor: 430–463) get deleted; the lookback-window seed is folded into the main path when `environment == 'dev'`.
- Tests under `tests/test_live_*` currently assert "dry-run doesn't persist"; flip to "dev persists with environment='dev'".

Scope discriminator note: the editor also touches `live_update_duplicate_state` via `find_live_update_duplicate` / `upsert_live_update_duplicate_state` (storage_handler.py:799,825). The brief doesn't list this table explicitly, but it's part of the editor's per-run dedupe state and must be tagged too to avoid dev runs colliding with prod duplicates — confirming in Questions.

## Phase 1: Schema + storage layer

### Step 1: Add environment column migration (`.migrations_staging/`)
**Scope:** Small
1. **Create** `.migrations_staging/<ts>_live_update_environment_split.sql` adding `environment text not null default 'prod'` to: `live_update_runs`, `live_update_candidates`, `live_update_decisions`, `live_update_feed_items`, `live_update_checkpoints`, `live_update_editorial_memory`, `live_update_watchlist`, `live_top_creation_runs`, `live_top_creation_posts`, `live_top_creation_checkpoints` (and `live_update_duplicate_state` pending Q1).
2. **Add** unique constraint `create unique index live_top_creation_posts_env_dupkey_uniq on live_top_creation_posts (environment, duplicate_key) where status = 'posted'`.
3. **Add** supporting indexes for the most common scoped lookups: `(environment, checkpoint_key)` on checkpoints; `(environment, created_at desc)` on `live_update_feed_items` if the existing read path orders that way (verify in storage_handler before adding).

### Step 2: Stamp/filter environment in `src/common/storage_handler.py`
**Scope:** Large
1. **Update writes** to accept `environment: str = "prod"` and include it in the payload for: `create_live_update_run` (652), `update_live_update_run` (674 — environment included in `eq` filter), `store_live_update_candidate(s)` (682/704), `update_live_update_candidate_status` (712, filter), `store_live_update_decision` (724), `store_live_update_feed_item` (738), `update_live_update_feed_item_messages` (758, filter), `upsert_live_update_duplicate_state` (825), `upsert_live_update_checkpoint` (858), `upsert_live_update_editorial_memory` (1653), `upsert_live_update_watchlist` (1709), `create_live_top_creation_run` (1763), `update_live_top_creation_run` (1779), `store_live_top_creation_post` (1786), `upsert_live_top_creation_checkpoint` (1848).
2. **Update reads** to accept `environment` and add `.eq("environment", environment)` to the query for: `get_recent_live_update_feed_items` (773), `find_live_update_duplicate` (799), `get_live_update_checkpoint` (840), `get_live_update_context_for_messages` (988), `_get_author_live_update_stats` (1088), `search_live_update_messages` (1145), `get_live_update_context_for_message_ids` (1183), `get_live_update_author_profile` (1220), `get_live_update_message_engagement_context` (1237), `get_live_update_recent_reaction_events` (1317), `_live_update_participant_profiles` (1555), `search_live_update_feed_items` (1571), `search_live_update_editorial_memory` (1616), `get_live_update_editorial_memory` (1694), `get_live_update_watchlist` (1748), `get_live_top_creation_checkpoint` (1831).
3. **Add** new method `get_live_top_creation_post_by_duplicate_key(environment, duplicate_key)` after `store_live_top_creation_post` — returns the most recent posted row (status='posted') or None.
4. **Leave** `get_archived_messages_after_checkpoint` untouched (per brief).

### Step 3: Mirror in `src/common/db_handler.py`
**Scope:** Medium
1. **Add** `environment: str = "prod"` to each wrapper at the matching line (254–624 list above) and forward through to the storage method.
2. **Add** wrapper for `get_live_top_creation_post_by_duplicate_key`.

## Phase 2: Editor + top-creations runtime

### Step 4: Convert `src/features/summarising/live_update_editor.py` to environment-aware
**Scope:** Large
1. **Constructor:** add `environment: str = "prod"`; keep `dry_run_lookback_hours` (rename internally to `initial_lookback_hours` is optional and out of scope here).
2. **`run_once` (line 110):** delete the `if self.dry_run: return await self._run_dry_once(...)` short-circuit. Right after the `get_live_update_checkpoint` call (113), if the checkpoint is None and `self.environment == 'dev'`, synthesize an in-memory checkpoint with `last_processed_at = now - dry_run_lookback_hours`.
3. **All 42 `self.db.*` sites** listed (113–1317): pass `environment=self.environment` to every read/write **except** the two `get_archived_messages_after_checkpoint` calls (151, 313).
4. **Delete** `_run_dry_once` (289–427) and `_publish_dry_run_debug_report` (430–463). The legitimate per-candidate context-fetch helpers used by `_run_dry_once` stay only if they're shared with the prod path; confirm before deletion.
5. **Channel resolution:** leave as-is (dev env vars already drive it).
6. **Optional debug logging:** if the dry-run debug report's content is useful, emit it as a single `logger.info(...)` summary gated on `self.environment == 'dev'` after `run_once` completes — but only if it's trivial; otherwise drop.

### Step 5: Convert `src/features/summarising/live_top_creations.py`
**Scope:** Large
1. **Constructor:** add `environment: str = "prod"`. Retire `top_gens_channel_id` and `live_channel_id` constructor params (no remaining caller paths after channel collapse — verify against cog + dev script).
2. **`run_once` (line 82):** delete the `if self.dry_run` short-circuit. Fold the lookback seeding into the main path after `get_live_top_creation_checkpoint` (90): if checkpoint is None and `environment == 'dev'`, synthesize one at `now - dry_run_lookback_hours`.
3. **All 8 `self.db.*` sites** (90, 93, 137, 175, 204, 336, 376): pass `environment=self.environment` (the two `get_archived_messages_after_checkpoint` reads at 114, 231 stay untouched).
4. **`_resolve_top_channel_id` (401–429):** collapse to summary channel only — dev returns `DEV_SUMMARY_CHANNEL_ID` (fallback `DEV_LIVE_UPDATE_CHANNEL_ID`), prod returns the guild's `summary_channel_id`. Stop reading `DEV_TOP_GENS_CHANNEL_ID` / server `top_gens_channel_id`.
5. **Pre-publish dedupe in `_publish_candidate` (around line 320, before `_send_without_mentions`):**
   ```python
   existing = await self.db.get_live_top_creation_post_by_duplicate_key(
       self.environment, candidate["duplicate_key"]
   )
   if existing:
       # mark skipped/duplicate, bump counter, return without sending
       return _SkipResult(reason="duplicate_existing_post")
   ```
   Wire the skip into the run-summary counters used by the `update_live_top_creation_run` calls at 137/175/204.
6. **Remove `max_posts` cap:** delete `DEFAULT_MAX_POSTS = 5` (42), the `self.max_posts` constructor arg, and the slices at 159 and 238 (or replace with a generous safety cap like 50 — Q2). Loop over the full `pending` list.
7. **Stamp `environment`** on all writes (already covered by Step 4 substep 3 — call sites pass it explicitly).
8. **Keep** the checkpoint's `posted_duplicate_keys` as a soft optimization; the DB unique index + lookup is now source of truth.

### Step 6: Wire `environment` through callers (`summariser_cog.py`, `scripts/run_live_update_dev.py`)
**Scope:** Small
1. **`summariser_cog.py` lines ~64, ~71:** pass `environment="dev" if self.dev_mode else "prod"` to both `LiveUpdateEditor(...)` and `LiveTopCreations(...)`. Drop the `dry_run=self.dev_mode` argument once the editor/top-creations constructors no longer accept it (or keep `dry_run` as a deprecated alias that just sets `environment` — Q3).
2. **`scripts/run_live_update_dev.py` lines 129–140:** pass `environment="dev"`; drop `dry_run=True` in lockstep with constructor cleanup. Keep `dry_run_lookback_hours=args.lookback_hours`.

## Phase 3: Tests + verification

### Step 7: Update existing live-update tests (`tests/test_live_*`)
**Scope:** Medium
1. **`test_live_update_editor_publishing.py`:** "Dry run posts without persisting editor state" and "Dry run debug report explains skipped candidate" — flip to "Dev environment posts AND persists with environment='dev'"; delete the debug-report test (or convert to log-capture if Step 4.6 is kept).
2. **`test_live_update_editor_publishing.py`:** "Dry run prefers dev channel env" — keep but rename; channel logic unchanged.
3. **`test_live_top_creations.py`:** "Dry run posts without persisting state" → flip to dev persistence; "Dry run prefers dev channel env" → assert it resolves to the summary channel (not top-gens). "Suppresses repeated posts from checkpoint" → augment to also assert behavior survives a checkpoint reset.
4. **`test_live_update_editor_lifecycle.py`, `test_live_storage_wrappers.py`, `test_live_runtime_wiring.py`:** thread the `environment` kwarg through mocks; assert wrapper passes it through.
5. **Update** in-memory fakes in test helpers to filter rows by `environment` so cross-env isolation is asserted, not assumed.

### Step 8: Add cross-environment isolation + dedupe tests
**Scope:** Small
1. **New test in `test_live_top_creations.py`:** run top-creations twice against the same archived message with reactions ≥ 5; assert exactly one `store_live_top_creation_post` call and one Discord send. Second pass must invoke `get_live_top_creation_post_by_duplicate_key` and short-circuit.
2. **New test in `test_live_update_editor_publishing.py` (or `test_live_runtime_wiring.py`):** seed a row with `environment='dev'`, run a prod editor pass, assert it does not surface in `get_recent_live_update_feed_items` / watchlist / editorial memory lookups.

### Step 9: Audit pass + run suite
**Scope:** Small
1. **Grep** `src/common/storage_handler.py` for `.from_("live_update_` / `.from_("live_top_creation_` and confirm every `select(...)` chain on the targeted tables has `.eq("environment", ...)`. This is the load-bearing correctness check called out in the brief.
2. **Run** `pytest tests/test_live_*.py -x` then full `pytest` to confirm no regressions in adjacent paths (admin chat, archive, summariser legacy).

## Execution Order
1. Schema migration first (Step 1) — code changes assume the column exists.
2. Storage + db_handler signatures (Steps 2–3) so the runtime layer has APIs to call.
3. Editor + top-creations runtime conversion (Steps 4–5).
4. Wire callers (Step 6).
5. Tests (Steps 7–8).
6. Audit + full suite (Step 9).

## Validation Order
1. Targeted unit tests: `pytest tests/test_live_storage_wrappers.py tests/test_live_top_creations.py tests/test_live_update_editor_publishing.py tests/test_live_update_editor_lifecycle.py -x`.
2. Runtime wiring + admin: `pytest tests/test_live_runtime_wiring.py tests/test_live_admin_health.py -x`.
3. Full suite: `pytest -x`.
4. Storage grep audit (Step 9.1) — visual confirmation.
5. (Manual / info) `scripts/run_live_update_dev.py --once` against a real Supabase to confirm dev rows are written with `environment='dev'` and a subsequent prod pass doesn't see them.
