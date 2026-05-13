
# Implementation Plan: Live-Update Editor Redesign — Phase 1 Shadow-Mode Vertical Slice (rev 2)

## Overview
Build the v6 redesign (`docs/live-update-editor-redesign.md`) as a shadow-mode dev vertical slice with publishing OFF: 5 new tables + a new `topic_editor_runs` provenance table (so the new editor writes ONLY to its own tables), a native Anthropic tool-use editor (4 read + 6 decision tools) with 4 dispatcher invariants including the v6 collision-override path, a pure renderer, comparison-trace embeds posted to a dev `#main_test` channel, and a runner that replays 20+ historical source-message windows. The existing `LiveUpdateEditor`, `LiveUpdateCandidateGenerator`, `ClaudeClient.generate_chat_completion`, and all `live_update_*` tables/accessors stay bit-for-bit untouched. Stop at the Week-1 decision gate — Phase 2 is out of scope.

Run-provenance, env-scoped reads, and authoritative author resolution are committed up front. Backfill is a Python script run **after** migration apply; SQL DDL does no Python work. Tests use in-memory fake-store fixtures (the existing `tests/conftest.py` stub pattern), not a live Postgres — `.migrations_staging/` remains manual handoff. Concurrent-run lease is explicitly deferred to Phase 1.5 against the doc's Phase-1 recommendation, called out in trace embeds. Locked decisions ship as `docs/live-update-editor-phase1-locked-decisions.md` before any code. LOC budget ~800–1000; alarm above 1500.

## Phase 0: Lock decisions (no code)

### Step 1: Defer audit + locked-decisions doc (`docs/live-update-editor-phase1-locked-decisions.md`, `tests/fixtures/historical_headlines.json`)
**Scope:** Small
1. **Query** live `live_update_candidates` for the last 30 days counting `decision='defer'` rows by (env, guild) via a one-off script using `DatabaseHandler`; inspect samples for POM action.
2. **Build** the 50-headline fuzz fixture: pull from `live_update_feed_items` (~18 prod rows), augment with recent `live_update_candidates.raw_output` headlines + recent watchlist titles. Save to `tests/fixtures/historical_headlines.json`.
3. **Publish** the locked-decisions doc resolving: canonicalizer rule; `editorial_observations` retention; collision threshold + override semantics; single-table rejected logging on `topic_transitions`; defer-audit-driven decision on whether `pending_review` ships Day-1.
4. **Reference** the locked-decisions doc from the migration header and editor module docstrings.

## Phase 1: Schema + canonicalizer

### Step 2: Schema migration (`.migrations_staging/20260513XXXXXX_topic_editor_phase1.sql`)
**Scope:** Medium
1. **Author** DDL verbatim from `docs/live-update-editor-redesign.md:100-205` for the 5 spec tables — `topics` (with publication_* columns folded in), `topic_sources` (UNIQUE `(topic_id, message_id)` only), `topic_aliases`, `topic_transitions` (env + guild_id denormalized columns, `topic_id` NULLABLE), `editorial_observations`.
2. **Add** a 6th NEW table `topic_editor_runs` in the same migration (NOT reusing `live_update_editor_runs`): `run_id uuid PK`, `environment text`, `guild_id bigint`, `live_channel_id bigint NULL`, `model text`, `started_at`, `finished_at`, `tokens_in int`, `tokens_out int`, `latency_ms int`, `status text`, `notes jsonb`. Old editor's `live_update_editor_runs` stays untouched.
3. **Include** `CREATE EXTENSION IF NOT EXISTS pg_trgm;` + `topics_headline_trgm` GIN index, plus `transitions_action_idx`, `transitions_topic_idx`, `transitions_run_idx`, `topic_sources_message_idx`, `topics_state_idx`, `topics_revisit_idx`.
4. **Do NOT** touch any existing `live_update_*` table.
5. **No backfill SQL inside the migration.** Backfill is a separate Python script (Step 4).

### Step 3: Canonicalizer + fuzz test (`src/features/summarising/topic_canonicalizer.py`, `tests/test_topic_canonicalizer.py`)
**Scope:** Small
1. **Implement** `canonicalize(proposed_key: str, headline: str, source_messages: list[dict] | None = None) -> str`. Pure, no DB. `source_messages` optional with headline-only fallback.
2. **Implement** `similarity_score(headline_a, headline_b, authors_a, authors_b) -> dict` returning trigram-similarity + author-overlap; pure Python.
3. **Test** with `tests/fixtures/historical_headlines.json`: idempotence under whitespace/case/article variation, parent/child headlines collide on canonical_key prefix or trigger alias hit, collision-rate metric (<10% expected, asserted as warning).

### Step 4: Python backfill script (`scripts/backfill_topics_from_feed_items.py`)
**Scope:** Small
1. **Run** after migration apply (one-shot, dev). Pulls `live_update_feed_items` via `DatabaseHandler.search_live_update_feed_items`.
2. **For each row**: parse JSONB `source_message_ids`, resolve source messages + authors via `get_live_update_context_for_message_ids` (the authoritative source for `source_authors` — feed_items has no such column), call `topic_canonicalizer.canonicalize(...)`, insert `topics` (`state='posted'`, `publication_status='sent'`, `discord_message_ids` populated), then `topic_sources` rows (one per `(topic_id, bigint(message_id))`). Idempotent on `(environment, guild_id, canonical_key)`.
3. **Document** as one-shot; never invoked at runtime.

## Phase 2: Tool-use editor + dispatcher

### Step 5: Native tool-use client (`src/common/llm/topic_editor_client.py`)
**Scope:** Medium
1. **Add** a sibling client to `ClaudeClient` (do NOT modify `src/common/llm/claude_client.py:33-92`). Wrap `anthropic.AsyncAnthropic.messages.create` with `tools=`, `tool_choice='auto'`, multi-turn tool-result loops, extended-thinking blocks, surfacing `tool_use_id`.
2. **Expose** `run_tool_loop(system_prompt, user_msg, tools, dispatcher, max_turns=12) -> RunTrace`. Transport-only.

### Step 6: Storage + DB accessors for new tables (`src/common/db_handler.py`, `src/common/storage_handler.py`)
**Scope:** Medium
1. **Add** in `db_handler.py` (async-in-thread), each backed by a matching `storage_handler.py` method (DEBT-028 two-layer pattern at `storage_handler.py:1733-1780`):
   - `create_topic`, `update_topic_state`, `get_topic_by_id`, `get_topic_by_canonical_key`
   - `add_topic_sources` (handles `(topic_id, message_id)` UNIQUE)
   - `add_topic_alias`, `resolve_topic_by_alias`
   - `record_topic_transition` (handles `topic_id=NULL` for rejected creates)
   - `record_editorial_observation`
   - `search_topics_by_headline(headline, environment, guild_id, ...)` — trigram + alias join
   - `list_watching_topics(environment, guild_id)`
   - `create_topic_editor_run`, `finish_topic_editor_run`
   - `get_topic_transition_rates(environment, guild_id, window_hours=168)` — trip-wire rolling aggregate
2. **Mirror** each in `storage_handler.py` as the lower-layer payload builder. Enumerated.
3. **Env-scoped read invariant (hard)**: every new read accessor filters by `(environment, guild_id)` mandatorily. `cross_env: bool = False` is the only escape hatch (debug scripts only).
4. **Do NOT** modify or remove any existing `live_update_*` accessor.

### Step 7: 10 tool schemas + dispatcher (`src/features/summarising/topic_tools.py`, `src/features/summarising/topic_dispatcher.py`)
**Scope:** Large
1. **Define** the 10 Anthropic tool schemas verbatim from `docs/live-update-editor-redesign.md:264-391`. Write tools accept `override_collisions: list[{topic_id, reason}]`. `search_topics` is dispatcher-scoped to `(environment, guild_id)`; no cross-env access on the agent surface.
2. **Implement** `TopicDispatcher.dispatch(tool_name, tool_use_id, args, run_ctx)`. Author resolution is **authoritative**: `_resolve_authors(source_message_ids)` calls `get_live_update_context_for_message_ids` and uses archive `author_id` set, NOT agent tool args.
   - **Inv 1 (structural)**: `post_simple_topic` returns tool-error when authoritative `distinct_authors >= 2` OR `len(source_message_ids) >= 3`; writes `action='rejected_post_simple'`, `topic_id=NULL`.
   - **Inv 2 (collision + override)**: canonicalize → call `search_topics_by_headline` (env-scoped); for each candidate compute `similarity_score(...)` with `author_overlap` derived from `topic_sources` join + authoritative archive authors of incoming `source_message_ids`. Match AND `topic_id` not in `override_collisions` → tool-error + log `action='rejected_*'`. Override present → accept the write + log `action='override'` per matched topic_id with `reason` in `payload`.
   - **Inv 3 (idempotency)**: `(run_id, tool_call_id)` check first; replay returns prior result no-op.
   - **Inv 4 (source uniqueness)**: rely on `topic_sources` UNIQUE; on conflict log `action='rejected_*'`.
3. **Every** rejection writes one `topic_transitions` row (`topic_id=NULL` for failed creates, structured details in `payload`).
4. **Tests** (`tests/test_topic_dispatcher_invariants.py`, `tests/test_topic_aliases_resolution.py`) use the in-memory fake-store fixture (Step 11).

### Step 8: Editor agent + prompt (`src/features/summarising/topic_editor.py`, `src/features/summarising/topic_editor_prompt.py`)
**Scope:** Medium
1. **Build** `TopicEditor.run_once(window, environment, guild_id)`:
   - Insert a `topic_editor_runs` row via `create_topic_editor_run` (NEVER `live_update_editor_runs`).
   - Assemble context: new source messages, watching topics (`list_watching_topics`, env-scoped), recently-posted topics for `search_topics` priming.
   - Call `topic_editor_client.run_tool_loop` with `TopicDispatcher` as executor.
   - Close via `finish_topic_editor_run` (tokens, latency, status).
   - Return a `RunTrace`.
2. **Author** the editor prompt covering taste from `_meets_editorial_bar` without JSON-mode scaffolding. Cap `record_observation` at ~3/run.
3. **Note** in docstring: concurrent-run lease (doc line 684 Phase-1 recommendation) is deferred to Phase 1.5; trace embed surfaces `lease: DEFERRED`.

## Phase 3: Pure publisher + trace embed

### Step 9: Pure renderer (`src/features/summarising/topic_publisher.py`, `tests/test_topic_publisher_render.py`)
**Scope:** Small
1. **Implement** `render_topic(topic) -> list[RenderedMessage]` — no DB, no Discord. Handles simple, sectioned, story-update (`Update:` prefix + parent link), partial-publish recovery.
2. **Tests** cover all four forms without a live send.

### Step 10: Trace-embed builder + dev channel wiring (`src/features/summarising/topic_trace_embed.py`)
**Scope:** Small
1. **Build** `build_trace_embed(run_trace, divergence, rolling_rates)`: source-message count, tool calls by name, posted/watched/updated/discarded, observations, cost, latency, override-rate (per-run + 7-day rolling via `get_topic_transition_rates`), record_observation rate, partial-publish incidents, `lease: DEFERRED` indicator, per-window divergence-vs-today.
2. **Resolve** trace channel via new env var `DEV_SHADOW_TRACE_CHANNEL_ID` (default unset → log-only). Do NOT reuse `DEV_LIVE_UPDATE_CHANNEL_ID`.

## Phase 4: Replay runner + tests

### Step 11: Test fixtures + in-memory fake-store (`tests/conftest.py`, `tests/fakes/topic_store.py`)
**Scope:** Small
1. **Add** a `topic_store_fake` pytest fixture (Python dicts/sets) satisfying the same interface as the new `storage_handler.py` topic accessors. Mirrors the `_install_llm_stub` / `_install_tweepy_stub` pattern in `tests/conftest.py` — no live Postgres, no `.migrations_staging/` apply.
2. **Wire** the fake into dispatcher / alias / shadow-replay tests via dependency injection on `TopicDispatcher` and `TopicEditor`. Include `difflib.SequenceMatcher.ratio` as a Python-side trigram stand-in.
3. **Document**: live-DB runs are reserved for the manual `scripts/backfill_*` + `scripts/run_live_update_shadow.py` flow after migration handoff.

### Step 12: Shadow-mode replay runner (`scripts/run_live_update_shadow.py`)
**Scope:** Medium
1. **Model on** `scripts/run_live_update_dev.py` but do NOT route through `LiveUpdateEditor`. Wire `TopicEditor` + `TopicDispatcher` + `topic_editor_client` directly.
2. **Load** ≥20 historical source-message windows from the prod archive. Flags: `--windows N`, `--lookback HOURS`, `--once`.
3. **Per window**: run new editor with publishing OFF, environment='dev'; fetch today's editor's recorded decisions from `live_update_candidates` / `live_update_feed_items` (read-only); post one trace embed per window to `DEV_SHADOW_TRACE_CHANNEL_ID`.
4. **Aggregate** summary embed at end.
5. **Deliberately** roll one window twice; trace embed asserts the second resolves to the same `topic_id` via aliases.

### Step 13: End-to-end shadow smoke test (`tests/test_shadow_mode_replay.py`)
**Scope:** Small
1. **Smoke test** using the in-memory fake store: fixture window → new editor with publishing OFF → assert `topic_transitions` rows recorded, no Discord `channel.send` invoked (mocked), trace-embed payload contains all required fields including override rate, observation rate, and `lease: DEFERRED`.

## Execution Order
1. Phase 0 (locked decisions + defer audit + 50-headline fixture) before any code.
2. Phase 1: migration DDL + canonicalizer + Python backfill (Steps 2–4) — Day 1.
3. Phase 2: client + storage/DB accessors with env-scoped reads + dispatcher + editor (Steps 5–8) — Day 2.
4. Phase 3: renderer + trace embed (Steps 9, 10) — Day 3.
5. Phase 4: test fakes + replay runner + e2e test (Steps 11–13) — Days 4–5.
6. Decision gate (no code) — Day 5/6.

## Validation Order
1. `pytest tests/test_topic_canonicalizer.py` — no fixture needed.
2. `pytest tests/test_topic_publisher_render.py` — pure function.
3. `pytest tests/test_topic_dispatcher_invariants.py tests/test_topic_aliases_resolution.py` — uses fake store.
4. `pytest tests/test_shadow_mode_replay.py` — e2e against the fake with mocked Discord.
5. `pytest tests/test_live_update_editor_lifecycle.py tests/test_live_update_editor_publishing.py tests/test_live_update_prompts.py` — existing tests stay green.
6. Manual (live DB): apply migration to dev Supabase → run `scripts/backfill_topics_from_feed_items.py` → run `scripts/run_live_update_shadow.py --windows 20`; review 20 trace embeds in `#main_test`.
