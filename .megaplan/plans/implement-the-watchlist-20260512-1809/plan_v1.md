
# Implementation Plan: Watchlist-Driven Live Update Editor Improvements

## Overview

Implement the four coupled changes from `docs/live-editor-watchlist-plan.md`:
1. Model-driven watchlist (tool calls + lifecycle + schema additions + fixed prompt rendering).
2. Remove the per-run publish cap (code + prompt).
3. Lower the publish bar (hard thresholds, quiet-hour rule, last-call watchlist bar).
4. Bulletproof `editor_reasoning` (raw_text always persisted, REASONING-prefix convention, parser fallback chain with recovery telemetry).

Touch points verified:
- `src/features/summarising/live_update_editor.py:60` (`DEFAULT_MAX_PUBLISH_PER_RUN = 1`), used at `:103`.
- `src/features/summarising/live_update_prompts.py`: watchlist render `:450-457` (uses non-existent `watch_type`/`description`), `available_tools` `:469`, `_run_requested_tool` `:562`, `_parse_raw_candidates` `:595`, `_parse_json_payload` `:625`, `_meets_editorial_bar` `:904`, prompt strings at `:319`, `:349`.
- `src/common/db_handler.py`: `upsert_live_update_watchlist:595`, `get_live_update_watchlist:602`, `live_update_editorial_memory` `:556/575/582`.
- `src/common/storage_handler.py`: editor run insert `:654`, update `:682`.
- Migrations live in `.migrations_staging/` with `YYYYMMDDHHMMSS_<slug>.sql` naming.
- Tests: `tests/test_live_update_editor_lifecycle.py`, `tests/test_live_update_prompts.py`, `tests/test_live_update_editor_publishing.py` all exist.

Out of scope (per brief): `live_top_creations`, legacy summariser, lookback changes.

## Phase 1 — Watchlist schema + DB layer

### Step 1: Add migration for watchlist lifecycle columns (`.migrations_staging/<ts>_live_update_watchlist_lifecycle.sql`)
**Scope:** Small
1. **Create** new migration file with timestamp matching repo convention.
2. **Add** columns to `live_update_watchlist`: `expires_at timestamptz`, `next_revisit_at timestamptz`, `revisit_count int NOT NULL DEFAULT 0`, `origin_reason text`, `evidence jsonb`.
3. **Backfill** `expires_at = COALESCE(created_at, now()) + interval '72 hours'` and `next_revisit_at = COALESCE(created_at, now()) + interval '6 hours'` for existing rows.
4. **Broaden** `status` (drop any CHECK constraint if present; otherwise documentation only) — accepted values: `active | discarded | archived | published`. If schema currently uses `fresh`/etc, also migrate existing values to `active`.

### Step 2: DB handler — insert/update/get watchlist (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `insert_live_update_watchlist(watch_key, title, origin_reason, source_message_ids, channel_id, subject_type, environment, guild_id)` — idempotent by `(environment, watch_key)`; sets `expires_at = now()+72h`, `next_revisit_at = now()+6h`, `evidence = jsonb` snapshot, `status='active'`.
2. **Add** `update_live_update_watchlist(watch_key, action, notes, environment)` — `publish_now` sets `status='published'`, `extend` bumps `next_revisit_at = least(now()+6-12h, expires_at)` and `revisit_count += 1`, `discard` sets `status='discarded'` and stores `notes`.
3. **Modify** `get_live_update_watchlist` (`:602`) to: (a) auto-archive rows whose `expires_at < now()` (`status='archived'`, append `notes='ttl_expired'`); (b) return active rows grouped by computed `revisit_state` in Python (`fresh` if `now()<next_revisit_at`, `revisit_due` if past `next_revisit_at`, `last_call` if past `created_at+24h`); (c) cap 20 per state, most-recent first.
4. **Enforce** active-row cap (50): oldest `fresh` rows beyond cap → auto-archive.

## Phase 2 — Tool definitions, dispatcher, prompt rendering

### Step 3: Register watchlist tools and dispatch (`src/features/summarising/live_update_prompts.py`, `src/features/summarising/live_update_editor.py`)
**Scope:** Medium
1. **Add** two entries in the `available_tools` array (`live_update_prompts.py:469`) — `watchlist_add` and `watchlist_update` schemas per brief.
2. **Wire** dispatch in `_run_requested_tool` (`:562`) calling new DB methods; return structured success/error payload; record into `watchlist_actions` field on the agent trace alongside `tool_trace`.
3. **Pass** `environment` / `guild_id` from the editor through context so the tool can attribute rows correctly.

### Step 4: Fix watchlist rendering + state grouping (`src/features/summarising/live_update_prompts.py:450-457`)
**Scope:** Small
1. **Replace** `watch_type`/`description` with schema-correct `subject_type`/`notes`.
2. **Render** `watchlist` block as `{_explanation, fresh, revisit_due, last_call}` with fields `{watch_key, title, origin_reason, age_hours, source_message_ids, subject_type, channel_id}` from grouped DB output.

## Phase 3 — Remove publish cap

### Step 5: Drop per-run cap in code (`src/features/summarising/live_update_editor.py:60`, `:103`, `:~453`)
**Scope:** Small
1. **Change** `DEFAULT_MAX_PUBLISH_PER_RUN = None`; update `__init__` to leave `self.max_publish_per_run = None` unless `LIVE_UPDATE_MAX_POSTS_PER_RUN` env var is set.
2. **Guard** the publish slice — only truncate when `self.max_publish_per_run is not None`.

### Step 6: Soften the "cap" language in the prompt (`live_update_prompts.py:319`, `:349-353`)
**Scope:** Small
1. **Rewrite** the two prompt strings per brief (no per-run cap; "Returning zero is fine when nothing meets the bar…").

## Phase 4 — Lower the publish bar

### Step 7: Relax `_meets_editorial_bar` (`live_update_prompts.py:904-938`)
**Scope:** Small
1. **showcase** bar: `(reactions >= 3 OR reply_count >= 2 OR author_is_high_signal)` AND `has_media`.
2. **top_creation** bar: `reactions >= 3` (+ existing media requirement).
3. **project_update**: unchanged.
4. **Quiet-hour rule:** thread `scanned_message_count` into the bar function; when `< 50`, drop one tier (showcase ≥2 reactions OR 1 reply; top_creation ≥2 reactions; project_update unchanged).
5. **Last-call watchlist bar:** when called for a `last_call` watchlist publish, accept showcase/top_creation with `reactions >= 2 OR reply_count >= 1` (+ media); project_update with `reactions >= 1 OR reply_count >= 1`.

## Phase 5 — Reliable editor_reasoning

### Step 8: Persist raw_text on every run (`src/features/summarising/live_update_editor.py`, `src/common/storage_handler.py`)
**Scope:** Small
1. **Add** `agent_trace.raw_text = raw_output[:50000]` into the metadata payload written through `storage_handler` for both success and zero-candidate paths (`storage_handler.py:654, 682`).

### Step 9: REASONING-prefix prompt + parser fallback chain (`live_update_prompts.py`)
**Scope:** Medium
1. **Insert** instruction at the OUTPUT SHAPE section (~`:284`): require leading `REASONING: <1-3 sentences>\n\n<json>` and a redundant `editor_reasoning` inside JSON; assert that omitting either makes the response invalid.
2. **Refactor** `_parse_raw_candidates` (`:595`) with explicit fallback chain — top-level `editor_reasoning` → aliases (`reasoning`, `editor_summary`, `editorial_reasoning`) → concat per-candidate `editor_reasoning` joined by ` | ` → regex `^REASONING:\s*(.+?)\n\n` on prose prefix → first ≤3 sentences before JSON span.
3. **Record** the firing branch as `metadata.agent_trace.reasoning_recovery_path` (e.g. `top_level | alias:reasoning | per_candidate | reasoning_prefix | prose_first_paragraph | none`).

### Step 10: Surface recovery telemetry (`scripts/debug_live_editor_audit.py`, dev debug embed in `live_update_editor.py`)
**Scope:** Small
1. **Add** a column/section showing `reasoning_recovery_path` distribution and a watchlist actions summary to the audit script.
2. **Include** `reasoning_recovery_path` in the dev reasoning embed footer.

## Phase 6 — Tests

### Step 11: Lifecycle + tool dispatch tests (`tests/test_live_update_editor_lifecycle.py`)
**Scope:** Medium
1. **Test** `watchlist_add` inserts row with correct TTL fields and is idempotent on `watch_key`.
2. **Test** `watchlist_update` for each action (`publish_now`, `extend`, `discard`) — state transitions, `revisit_count`, `notes` recorded, `next_revisit_at` capped at `expires_at`.
3. **Test** `get_live_update_watchlist` grouping (`fresh`/`revisit_due`/`last_call`) with frozen-time fixtures, 20-per-state cap, 50-row active cap, TTL auto-archive.
4. **Test** publish cap removed: with `max_publish_per_run=None`, multiple candidates publish; env var still throttles.

### Step 12: Parser fallback chain tests (`tests/test_live_update_prompts.py`)
**Scope:** Small
1. **Five cases** covering each branch — top-level reasoning, alias key (e.g. `reasoning`), per-candidate reasoning concatenation, `REASONING:` prose prefix, first-paragraph last resort. Assert non-empty reasoning and correct `reasoning_recovery_path`.
2. **Sixth case:** totally empty input → `reasoning_recovery_path='none'`, no crash.

### Step 13: Bar-relaxation tests (`tests/test_live_update_prompts.py` or `test_live_update_editor_publishing.py`)
**Scope:** Small
1. **Test** new thresholds for showcase / top_creation / project_update.
2. **Test** quiet-hour rule (<50 scanned) lowers tier.
3. **Test** last-call watchlist bar accepts items the normal bar would reject.

## Execution Order
1. Migration + DB handler (Phase 1) — foundation.
2. Tool registration + prompt rendering fix (Phase 2).
3. Cap removal (Phase 3) and bar relaxation (Phase 4) — independent, can land together.
4. Reasoning reliability (Phase 5).
5. Tests (Phase 6) alongside each phase where practical, finalize at the end.


