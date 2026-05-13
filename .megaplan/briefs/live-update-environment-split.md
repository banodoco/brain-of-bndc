# Live-update / top-creations: dev-vs-prod environment split + dedupe + same-channel routing

## Goal

Make dev runs of the live-update editor and top-creations service **fully exercise the production codepath** — including DB persistence — without their rows polluting prod reads, and vice versa. At the same time, simplify top-creations to publish into the same channel as live updates with strict once-only dedupe.

## Why

Today's `dry_run=True` path in `LiveUpdateEditor` and `LiveTopCreations` publishes to `DEV_*` Discord channels but **skips all DB writes** (no runs, candidates, decisions, feed items, checkpoints, or top-creation posts persisted). Result: dev gives us no signal about how the system will look in production, because half the production codepath (persistence + checkpoint dedupe + memory/watchlist updates) never runs.

We want dev to be a true rehearsal: same code, same writes, same reads — but tagged so dev and prod never see each other's data.

## Decisions already locked (do not re-debate)

1. **Shared tables with an `environment` discriminator column.** Not separate dev tables.
2. **Top-creations publishes to the same channel as live updates** (the summary channel — `summary_channel_id` in prod, `DEV_SUMMARY_CHANNEL_ID` in dev). The separate `top_gens_channel_id` routing is retired. Drop the per-pass `max_posts` cap; the gate is reactions ≥ 5 + row-level dedupe.
3. **Dev publishes to Discord (to `DEV_*` channels) AND persists with `environment='dev'`.** Real rehearsal, not persist-only.

## Scope

### 1. Schema migrations

Add `environment text not null default 'prod'` to:

- `live_update_runs`
- `live_update_candidates`
- `live_update_decisions`
- `live_update_feed_items`
- `live_update_checkpoints`
- `live_update_editorial_memory`
- `live_update_watchlist`
- `live_top_creation_runs`
- `live_top_creation_posts`
- `live_top_creation_checkpoints`

Plus a uniqueness constraint to enforce "send once" at the DB level:

- Unique index on `live_top_creation_posts (environment, duplicate_key)` where status = 'posted'. (Or just `(environment, duplicate_key)` if status semantics make a partial index awkward — planner to decide.)

The default `'prod'` means existing rows auto-classify correctly; no backfill semantics to debate.

Convention: inspect the repo to identify the migration tooling/location and follow it. Likely Supabase SQL files under a `migrations/` or `supabase/migrations/` directory — confirm before writing.

### 2. Storage layer (`src/common/storage_handler.py`)

Every write method for the tables above: accept and stamp `environment`.
Every read method for the tables above: accept and filter on `environment`.

Methods to touch (non-exhaustive — planner to enumerate from the file):
- `create_live_update_run`, `update_live_update_run`
- `store_live_update_candidate(s)`, `store_live_update_decision`, `store_live_update_feed_item`
- `get_live_update_checkpoint`, `upsert_live_update_checkpoint`
- `get_recent_live_update_feed_items`
- `get_live_update_editorial_memory`, `update_live_update_editorial_memory` (whatever update method exists)
- `get_live_update_watchlist`, `update_live_update_watchlist` (whatever exists)
- `create_live_top_creation_run`, `update_live_top_creation_run`
- `store_live_top_creation_post`
- `get_live_top_creation_checkpoint`, `upsert_live_top_creation_checkpoint`
- **NEW**: `get_live_top_creation_post_by_duplicate_key(environment, duplicate_key)` — for pre-publish dedupe lookup.

`get_archived_messages_after_checkpoint` reads from the archived-messages table (shared infra, not live-update-specific) — it does **not** need an environment column.

### 3. DB handler (`src/common/db_handler.py`)

Mirror every storage_handler change — accept `environment` on the relevant wrappers, default to `'prod'`. Add wrapper for the new `get_live_top_creation_post_by_duplicate_key`.

### 4. `src/features/summarising/live_update_editor.py`

- Add constructor param `environment: str = "prod"`. Store as `self.environment`.
- Remove the `if self.dry_run: return await self._run_dry_once(...)` short-circuit from `run_once`. The dry-run path's lookback-window seeding (when no checkpoint exists yet) should be folded into the normal path: if `get_live_update_checkpoint(checkpoint_key)` returns None and we're in `dev` environment, synthesize an initial checkpoint at `now - dry_run_lookback_hours`. Otherwise behave normally.
- Every call into `self.db.*` that writes one of the listed tables: pass `environment=self.environment`.
- Every call that reads one of the listed tables: pass `environment=self.environment` so the filter applies.
- Channel resolution: keep the current dev-env-vars logic (`DEV_SUMMARY_CHANNEL_ID` / `DEV_LIVE_UPDATE_CHANNEL_ID`) — that part is fine.
- The `dry_run_lookback_hours` param can stay (rename internally if it helps; `initial_lookback_hours` is more honest, but renaming is optional).
- `_run_dry_once` and `_publish_dry_run_debug_report` (lines ~430-547) can be **deleted** — the dev path no longer needs a separate function. The debug-report behavior, if useful, can be kept as a logging side-effect gated on `environment == 'dev'`.

### 5. `src/features/summarising/live_top_creations.py`

- Add constructor param `environment: str = "prod"`.
- Same persistence cleanup as the editor: remove the `if self.dry_run` short-circuit at `run_once` line 82, fold the lookback-window seed into the normal path for `environment == 'dev'`.
- **Channel resolution** (`_resolve_top_channel_id`, line 401): collapse to the live-update/summary channel. In prod, return `summary_channel_id` for the guild; in dev, return `DEV_SUMMARY_CHANNEL_ID` (or `DEV_LIVE_UPDATE_CHANNEL_ID` as fallback). Stop reading `DEV_TOP_GENS_CHANNEL_ID` / server `top_gens_channel_id`. Constructor param `top_gens_channel_id` and `live_channel_id` can be removed or repurposed — planner's call, but the runtime path must only go to the summary channel.
- **Pre-publish dedupe**: in `_publish_candidate` (line 309), before the `await _send_without_mentions(...)` call, query `db.get_live_top_creation_post_by_duplicate_key(self.environment, candidate["duplicate_key"])`. If a posted row exists, skip publishing and mark the candidate as skipped/duplicate. Update the run summary counters accordingly. This is the "send only once" guarantee, surviving checkpoint resets.
- Remove the `max_posts` per-pass cap (currently `DEFAULT_MAX_POSTS = 5`, applied at `pending[: self.max_posts]` line 159, 238). Allow all eligible candidates through. Optionally keep a high-ceiling safety belt (e.g. 50) — planner's call.
- Stamp `environment` on all writes (run create/update, post store, checkpoint upsert).
- The checkpoint's `posted_duplicate_keys` state can stay as a soft optimization but is no longer the source of truth.

### 6. Cog wiring (`src/features/summarising/summariser_cog.py`)

At construction (lines ~61, ~68): pass `environment="dev" if self.dev_mode else "prod"` into `LiveUpdateEditor` and `LiveTopCreations`. The `dry_run=self.dev_mode` argument can either stay (now meaning only "seed initial lookback") or be removed entirely if the planner folds the seeding logic in cleanly.

### 7. Run script (`scripts/run_live_update_dev.py`)

Should require **no structural changes** — `dev_mode=True` flows through to environment="dev" via the cog path. The script constructs the editor/top-creations directly though, so update those constructor calls (lines 129-140) to pass `environment="dev"` explicitly. The `dry_run=True` and `dry_run_lookback_hours` args either stay or are removed in lockstep with the editor/top-creations API.

### 8. Tests

Existing tests under `tests/test_live_*` (especially `test_live_update_editor_lifecycle.py`, `test_live_update_editor_publishing.py`, `test_live_top_creations.py`, `test_live_runtime_wiring.py`, `test_live_storage_wrappers.py`) currently assert "dry-run doesn't persist." Flip those to assert "dev environment persists with `environment='dev'` and prod reads do not see those rows."

Add a new test (or extend an existing one) covering: **pre-publish dedupe for top-creations** — running twice over the same archived message with reactions ≥ 5 results in exactly one `live_top_creation_posts` row and one Discord send.

## Out of scope

- Any change to the archived-messages ingestion path.
- Any change to the legacy daily-summary code (`summariser.py`, `ChannelSummarizer.generate_summary`, `daily_summaries` table).
- Any change to how reactions are counted or the `min_reactions = 5` threshold itself.
- Renaming `dry_run` everywhere — internal rename is optional; behavior is what matters.
- Backfilling existing rows — the default `'prod'` does the right thing.

## Acceptance criteria

1. **Schema**: all listed tables have an `environment` column; unique constraint exists on `live_top_creation_posts (environment, duplicate_key)`.
2. **Audit pass — every read filters on environment**: a planner-or-reviewer-driven grep through the storage layer confirms there is no `select` against the listed tables that omits the `environment` filter when called from editor or top-creations code. *This is the load-bearing correctness check.*
3. **Dev rehearsal works**: running `scripts/run_live_update_dev.py --once` against a real Supabase produces rows in the relevant tables with `environment='dev'`, and a subsequent prod-mode pass does not surface any of those dev rows in `get_recent_live_update_feed_items`, the watchlist, the editorial memory, or the top-creation dedupe lookup.
4. **Top-creations dedupe**: re-running top-creations against the same archived message twice publishes exactly once.
5. **Top-creations channel**: posts land in the live-update / summary channel; no traffic to a separate top-gens channel.
6. **Tests pass**: all existing live-update tests pass (after being flipped from "no persistence" to "dev persistence + isolation"), plus the new dedupe test.

## Useful starting pointers

- Current dry-run short-circuits: `live_update_editor.py:110`, `live_top_creations.py:82`.
- Top-creations channel resolution to collapse: `live_top_creations.py:401-429`.
- Top-creations publish path to add dedupe check: `live_top_creations.py:309`.
- Cog wiring: `summariser_cog.py:55-72`.
- Dev run script: `scripts/run_live_update_dev.py`.
- Legacy boundaries doc (helpful context, not authoritative): `docs/live-update-editor-legacy-boundaries.md`.
