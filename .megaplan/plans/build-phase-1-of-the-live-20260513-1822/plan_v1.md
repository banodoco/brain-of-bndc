
# Implementation Plan: Live-Update Editor Redesign — Phase 1 Shadow-Mode Vertical Slice

## Overview
Build the v6 redesign (docs/live-update-editor-redesign.md) as a shadow-mode dev vertical slice with publishing OFF: a 5-table schema, a native Anthropic tool-use editor (4 read + 6 decision tools) with 4 dispatcher invariants including the v6 collision-override path, a pure renderer, a comparison-trace embed posted to a dev `#main_test` channel, and a runner that replays 20+ historical source-message windows. The existing `live_update_*` pipeline (LiveUpdateEditor, LiveUpdateCandidateGenerator, ClaudeClient.generate_chat_completion, live_update_* tables) stays bit-for-bit untouched so the replay can diff old vs new decisions on the same windows. Stop at the Week-1 decision gate — Phase 2 (prod cutover) is out of scope.

Resolve five locked decisions in writing (`docs/live-update-editor-phase1-locked-decisions.md`) before any code lands. Land code as new modules (`topic_canonicalizer.py`, `topic_editor.py`, `topic_editor_client.py`, `topic_publisher.py`, `topic_dispatcher.py`, `scripts/run_live_update_shadow.py`) and a new migration in `.migrations_staging/`. LOC budget ~800–1000; alarm above 1500.

## Phase 0: Lock decisions (no code)

### Step 1: Defer audit + locked-decisions doc (`docs/live-update-editor-phase1-locked-decisions.md`)
**Scope:** Small
1. **Query** live `live_update_candidates` for the last 30 days counting `decision='defer'` rows by (env, guild) and inspect a sample to see whether POM acted on them. Use a one-off script via `DatabaseHandler` (no new accessor needed).
2. **Pull** 50 historical headlines from `live_update_feed_items` (~18 prod) + recent `live_update_candidates.raw_output` headlines + recent watchlist titles. Save to `tests/fixtures/historical_headlines.json` for the canonicalizer fuzz test.
3. **Write** the locked-decisions doc resolving:
   - (1) canonicalizer algorithm: lowercase → NFKD strip diacritics → strip articles/stopwords → hyphenate → anchor on creator/artifact/date tokens when extractable. Fuzz target: 50 headlines, idempotence under whitespace/case/article variation, parent/child story headlines share a canonical_key prefix or trigger an alias hit.
   - (2) `editorial_observations` retention: agent discretion, prompt-capped ~3/run, 30d retention, no sampling layer in Phase 1.
   - (3) collision threshold: `(canonical_key_prefix match)` OR `(trigram_similarity(headline) >= 0.55 AND author_overlap >= 1)`. Override path: `override_collisions=[{topic_id, reason}]` accepted on three write tools, every override logs `action='override'` in `topic_transitions`.
   - (4) rejected-call logging: single-table on `topic_transitions` with `topic_id=NULL` for failed creates, `action='rejected_*'`, details in `payload`. Revisit if rejection rate >50% of writes.
   - (5) `pending_review` / `defer`: from audit result, decide ship-in-Day-1 vs defer-to-v2. Default to **defer-to-v2** unless audit shows ≥5/week acted-upon defers — in which case add `state='pending_review'` + `request_review_topic` tool in the Day-1 migration.
4. **Commit** the doc as the gate artifact; reference it from the migration and the editor module docstrings.

## Phase 1: Schema + canonicalizer

### Step 2: Schema migration (`.migrations_staging/20260513XXXXXX_topic_editor_phase1.sql`)
**Scope:** Medium
1. **Author** DDL verbatim from docs/live-update-editor-redesign.md:100-205 for the 5 tables.
2. **Confirm** v6 corrections: `topic_transitions.environment` + `guild_id` denormalized columns; `topic_transitions.topic_id` NULLABLE; `topic_sources` UNIQUE is `(topic_id, message_id)` ONLY (no env/guild in the unique); `topics` includes `publication_status`, `publication_error`, `discord_message_ids bigint[]`, `publication_attempts`, `last_published_at`.
3. **Include** `CREATE EXTENSION IF NOT EXISTS pg_trgm;` and the `topics_headline_trgm` GIN index plus `transitions_action_idx`, `transitions_topic_idx`, `transitions_run_idx`, `topic_sources_message_idx`, `topics_state_idx`, `topics_revisit_idx`.
4. **Do NOT** touch any existing `live_update_*` table in this migration.
5. **Backfill block** at the end of the SQL: `INSERT INTO topics (...) SELECT ... FROM live_update_feed_items` (state='posted', publication_status='sent', discord_message_ids populated), plus `topic_sources` from the feed_item's source message ids array. Backfill-only — the new editor never reads from or writes to legacy tables afterwards.

### Step 3: Canonicalizer + fuzz test (`src/features/summarising/topic_canonicalizer.py`, `tests/test_topic_canonicalizer.py`)
**Scope:** Small
1. **Implement** `canonicalize(proposed_key: str, headline: str, source_messages: list[dict]) -> str` per the locked algorithm. Pure function, no DB.
2. **Implement** `similarity_score(headline_a, headline_b, authors_a, authors_b) -> dict` returning trigram similarity + author overlap so the dispatcher can apply the locked threshold.
3. **Add** `tests/test_topic_canonicalizer.py` driven by `tests/fixtures/historical_headlines.json`: idempotence, article/case/whitespace stability, parent/child key-prefix collision, collision-rate metric over the 50-headline set (<10% override expected — assert as warning, not hard failure, since this is the tuning gate).

## Phase 2: Tool-use editor + dispatcher

### Step 4: Native tool-use client (`src/common/llm/topic_editor_client.py`)
**Scope:** Medium
1. **Add** a sibling client to `ClaudeClient` (do NOT modify `claude_client.py:33-92`). Wrap `anthropic.AsyncAnthropic.messages.create` with `tools=`, `tool_choice='auto'`, multi-turn `tool_result` loops, extended-thinking blocks, and surface `tool_use_id` on each call.
2. **Expose** `run_tool_loop(system_prompt, user_msg, tools, dispatcher, max_turns=12) -> RunTrace` returning per-turn tool calls, tool results, tokens-in/out, latency, and final stop_reason. The dispatcher is the caller's callable; this module is transport-only.
3. **Keep** `ClaudeClient.generate_chat_completion` untouched.

### Step 5: Storage accessors for new tables (`src/common/db_handler.py`, `src/common/storage_handler.py`)
**Scope:** Medium
1. **Add** new accessors following the existing async-in-thread pattern at `src/common/db_handler.py`:
   - `create_topic`, `update_topic_state`, `get_topic_by_id`, `get_topic_by_canonical_key`
   - `add_topic_sources` (handles `(topic_id, message_id)` UNIQUE)
   - `add_topic_alias`, `resolve_topic_by_alias`
   - `record_topic_transition` (handles `topic_id=NULL` for rejected creates)
   - `record_editorial_observation`
   - `search_topics_by_headline` (uses trigram GIN + alias join)
   - `list_watching_topics(environment, guild_id)`
2. **Mirror** read-side wrappers in `storage_handler.py` only where the editor's read tools need them. The 4 read tools reuse existing `search_live_update_messages` / `get_live_update_author_profile` / `get_live_update_context_for_message_ids` for message archive access — only `search_topics` is genuinely new.
3. **Do NOT** modify or remove any existing `live_update_*` accessor.

### Step 6: Dispatcher + 10 tool definitions (`src/features/summarising/topic_dispatcher.py`, `src/features/summarising/topic_tools.py`)
**Scope:** Large
1. **Define** the 10 Anthropic tool schemas in `topic_tools.py` verbatim from the v6 spec (docs lines 264–391): `search_topics`, `search_messages`, `get_author_profile`, `get_message_context`, `post_simple_topic`, `post_sectioned_topic`, `watch_topic`, `update_topic_source_messages`, `discard_topic`, `record_observation`. The three write tools include `override_collisions: list[{topic_id, reason}]`.
2. **Implement** `TopicDispatcher.dispatch(tool_name, tool_use_id, args, run_ctx)` in `topic_dispatcher.py` enforcing the 4 invariants:
   - **Inv 1 (structural)**: `post_simple_topic` returns tool-error when `distinct_authors(source_message_ids) >= 2` OR `len(source_message_ids) >= 3`; writes `action='rejected_post_simple'` row with `topic_id=NULL`.
   - **Inv 2 (collision + override)**: canonicalize → run similarity scan via `search_topics_by_headline` + author-overlap → if match and `override_collisions` doesn't list that `topic_id`, return tool-error with matching topic_ids/canonical_keys/aliases AND log `action='rejected_*'`. If override is present, accept the write AND log `action='override'` per matched topic_id with the agent's `reason` in `payload`.
   - **Inv 3 (idempotency)**: every dispatch checks `(run_id, tool_call_id)` against `topic_transitions`; replays return the prior result no-op.
   - **Inv 4 (source uniqueness)**: rely on `topic_sources (topic_id, message_id)` UNIQUE; on conflict, log `action='rejected_*'` with details. Multiple topics per message remain accepted.
3. **Every** dispatcher rejection writes one `topic_transitions` row with `topic_id=NULL` for failed creates, `action='rejected_<tool>'`, structured rejection details in `payload`.
4. **Tests** (`tests/test_topic_dispatcher_invariants.py`): all 4 invariants + the override-accept path + the rejected_* row writes.
5. **Tests** (`tests/test_topic_aliases_resolution.py`): `search_topics` returns canonical_key + all aliases; dispatcher resolves drift-spelled `proposed_key` through aliases to the existing topic_id; new `alias_kind='proposed'` rows written on each call.

### Step 7: Editor agent + prompt (`src/features/summarising/topic_editor.py`, `src/features/summarising/topic_editor_prompt.py`)
**Scope:** Medium
1. **Build** `TopicEditor.run_once(window, environment, guild_id)`:
   - Assemble run context: new source messages, currently-watching topics, recently-posted topics (priming `search_topics`), env/guild_id.
   - Insert a `live_update_editor_runs` row (existing table is acceptable for run-id provenance — read only; or add a new `topic_editor_runs` if the existing schema doesn't fit cleanly).
   - Call `topic_editor_client.run_tool_loop` with the dispatcher as the tool executor.
   - Return a `RunTrace` (tool-call counts by name, posted/watched/updated/discarded/observation tallies, tokens, latency, divergence-vs-today payload).
2. **Author** the editor prompt in `topic_editor_prompt.py`. Covers editorial taste from current `live_update_prompts.py` (re-read `_meets_editorial_bar` and the defer/skip/duplicate decisions) without copying the JSON-mode scaffolding. Cap `record_observation` guidance at ~3/run.
3. **Do NOT** modify `live_update_editor.py`, `live_update_prompts.py`, or `claude_client.py`.

## Phase 3: Pure publisher + trace embed

### Step 8: Pure renderer (`src/features/summarising/topic_publisher.py`, `tests/test_topic_publisher_render.py`)
**Scope:** Small
1. **Implement** `render_topic(topic: Topic) -> list[RenderedMessage]` — no DB, no Discord. Handles simple, sectioned, story-update (prefix `Update:`, link to `parent.discord_message_ids[0]`), partial-publish recovery (idempotent re-render from `publication_status='partial'`). Publishing remains OFF in Phase 1.
2. **Tests**: simple form one message with headline/body/media/jump-link; sectioned produces header + N section messages with per-section media/jump-links; story-update prefix + parent link; partial-publish recovery exercised without flipping a live send.

### Step 9: Trace-embed builder + dev channel wiring (`src/features/summarising/topic_trace_embed.py`)
**Scope:** Small
1. **Build** `build_trace_embed(run_trace, divergence) -> discord.Embed` surfacing: source-message count, tool calls fired (per name), topics posted/watched/updated/discarded, near-miss observations, cost (tokens / approx $), latency, override-rate (this run + 7-day rolling), record_observation rate (this run + 7-day rolling), partial-publish incidents counter, and per-window divergence-vs-today (decision_today vs decision_new, topic_id mapping).
2. **Resolve** trace channel via a new env var `DEV_SHADOW_TRACE_CHANNEL_ID` (default semantics: `#main_test`). Do NOT reuse `DEV_LIVE_UPDATE_CHANNEL_ID` to avoid colliding with the existing reasoning-embed pipeline.

## Phase 4: Replay runner + end-to-end tests

### Step 10: Shadow-mode replay runner (`scripts/run_live_update_shadow.py`)
**Scope:** Medium
1. **Model on** `scripts/run_live_update_dev.py` (MinimalDiscordBot + ClaudeClient + DatabaseHandler shape), but do NOT route through `LiveUpdateEditor`. Wire the new `TopicEditor` + `TopicDispatcher` directly.
2. **Load** ≥20 historical source-message windows from the prod archive (filter to recent runs of today's editor, environment='prod' as source — execute in environment='dev' for writes). Provide `--windows N`, `--lookback HOURS`, `--once` flags.
3. **For each window**: run new editor with publishing OFF; capture decisions; fetch today's editor's recorded decisions for the same window from `live_update_candidates` / `live_update_feed_items` for divergence; post one trace embed per window to `DEV_SHADOW_TRACE_CHANNEL_ID`.
4. **Aggregate** summary embed at end: total cost, p50/p95 latency, divergence counts, override rate, observation rate, partial-publish incidents.
5. **Deliberately** roll one window twice to exercise alias resolution; assert via trace embed that the second run hits the same `topic_id`.

### Step 11: End-to-end shadow test (`tests/test_shadow_mode_replay.py`)
**Scope:** Small
1. **Smoke test**: fixture window of source messages → new editor with publishing OFF → assert `topic_transitions` rows exist, no Discord `channel.send` was attempted (mock `discord.Client`), trace-embed payload contains all required fields (source-message count, tool-call counts by name, posted/watched/updated/discarded/observation tallies, tokens, latency, divergence-vs-today summary, override rate, observation rate).

## Execution Order
1. Phase 0 (locked decisions + defer audit + 50-headline fixture) before any code.
2. Phase 1 schema + canonicalizer (Step 2, 3) — Day 1.
3. Phase 2 client + storage + dispatcher + editor (Steps 4–7) — Day 2.
4. Phase 3 renderer + trace embed (Steps 8, 9) — Day 3.
5. Phase 4 runner + e2e test (Steps 10, 11) — Days 4–5.
6. Decision gate (no code) — Day 5/6.

## Validation Order
1. `pytest tests/test_topic_canonicalizer.py` — cheapest, no DB.
2. `pytest tests/test_topic_publisher_render.py` — pure function.
3. `pytest tests/test_topic_dispatcher_invariants.py tests/test_topic_aliases_resolution.py` — requires test DB/migrations applied.
4. `pytest tests/test_shadow_mode_replay.py` — end-to-end with mocks.
5. `pytest tests/test_live_update_editor_lifecycle.py tests/test_live_update_editor_publishing.py tests/test_live_update_prompts.py` — must remain green untouched.
6. Manual: run `scripts/run_live_update_shadow.py --windows 20` against the prod archive in dev env; review 20 trace embeds in `#main_test`; record override-rate and observation-rate against trip-wires.
