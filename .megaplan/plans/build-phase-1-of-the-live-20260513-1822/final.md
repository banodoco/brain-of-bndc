# Execution Checklist

- [ ] **T16:** Read user_actions.md. For each before_execute action, programmatically verify completion using bash tools — grep .env for required keys, query the migrations table, curl the dev server, etc. Reading the file does NOT count as verification; you must run a command. For actions that genuinely cannot be verified mechanically (manual UI checks), explicitly ask the user. If anything is incomplete or unverifiable, mark this task blocked with reason and STOP.
  Executor notes: Both before_execute user_actions verified incomplete via shell commands (not just reading user_actions.md). U1: defer-audit number not delivered — no artifact in plan dir, docs/live-update-editor-phase1-locked-decisions.md does not exist. Blocks T1, T2, T7 per user_actions.md. U2: tests/fixtures/ directory does not exist; similarity_pairs.json and historical_headlines.json absent everywhere in repo. Blocks T3 per user_actions.md. Both require human-only inputs (live Supabase + Postgres-with-pg_trgm). Execution stopped per T16 instructions; no work done on T1–T15.

- [ ] **T1:** Phase 0 — Write docs/live-update-editor-phase1-locked-decisions.md resolving all 5 locked decisions: (1) canonicalizer rule, (2) editorial_observations retention (~3/run agent-discretion, 30d retention) + sampling, (3) collision similarity threshold (canonical-key-prefix OR pg_trgm ≥ 0.55 AND author_overlap ≥ 1) + override semantics, (4) confirm single-table rejected logging on topic_transitions, (5) record defer-audit outcome and ship-Day-1-or-v2 decision for pending_review. Also document the implementer judgment calls from the gate warnings: lean accessor missing message_id → tool-error; duplicate dedupe; null author counts as phantom distinct author; create_topic_editor_run failure aborts run_once loudly; borderline 3-5/week defer volume defaults to v2 unless POM intervenes. Depends on U1 (defer-audit SQL) and U2 (pg_trgm fixture). After U1, build tests/fixtures/historical_headlines.json (50 headlines from live_update_feed_items + recent live_update_candidates.raw_output + recent watchlist titles).
  Depends on: T16

- [ ] **T2:** Phase 1 Step 2 — Write the schema migration at .migrations_staging/20260513XXXXXX_topic_editor_phase1.sql. DDL for 6 tables (5 spec + topic_editor_runs): topics (with publication_status, publication_error, discord_message_ids, publication_attempts, last_published_at folded in), topic_sources (UNIQUE (topic_id, message_id) only), topic_aliases, topic_transitions (environment + guild_id columns, topic_id NULLABLE, run_id NOT NULL REFERENCES topic_editor_runs(run_id) ON DELETE RESTRICT), editorial_observations, topic_editor_runs (run_id uuid PK, environment, guild_id, live_channel_id NULL, model, started_at, finished_at, tokens_in, tokens_out, latency_ms, status, notes jsonb). Include CREATE EXTENSION IF NOT EXISTS pg_trgm + topics_headline_trgm GIN index + all spec indexes. NO backfill SQL. If T1's defer-audit shows ≥5/week acted-upon defers, document state='pending_review' as a valid value in migration comment (no DB-side enum). MUST NOT touch any live_update_* table.
  Depends on: T16, T1

- [ ] **T3:** Phase 1 Step 3 — Implement src/features/summarising/topic_canonicalizer.py with two pure functions: canonicalize(proposed_key, headline, source_messages=None) -> str and similarity_score(headline_a, headline_b, authors_a, authors_b, trigram_similarity) -> dict (author_overlap computed; trigram passed in from caller). Write tests/test_topic_canonicalizer.py exercising determinism/idempotence on tests/fixtures/historical_headlines.json (parent/child collide on canonical_key prefix, whitespace/case/article variance idempotent). Write tests/test_topic_similarity_oracle.py asserting dispatcher match/no-match decision at threshold 0.55 equals expected_match_at_0_55 for every row in tests/fixtures/similarity_pairs.json. Add a conftest guardrail asserting every pair used in threshold-tuning tests appears in similarity_pairs.json (so difflib fallback in the fake store cannot leak back into threshold tests).
  Depends on: T16, T1

- [ ] **T4:** Phase 1 Step 4 — Write scripts/backfill_topics_from_feed_items.py as one-shot post-migration Python script. Pulls live_update_feed_items filtered to status='posted' AND len(discord_message_ids) > 0. For each row: parse JSONB source_message_ids, resolve authors via the NEW lean get_message_author_ids (NOT the heavy context accessor), call topic_canonicalizer.canonicalize, insert topics (state='posted', publication_status='sent', discord_message_ids populated), then topic_sources rows. Idempotent on (environment, guild_id, canonical_key). Run flag --dry-run for preview. Dev-only. Note: this script depends on T6 for get_message_author_ids — order accordingly.
  Depends on: T16, T2, T3, T6

- [ ] **T5:** Phase 2 Step 5 — Add src/common/llm/topic_editor_client.py as a sibling client to ClaudeClient. DO NOT modify claude_client.py. Wraps anthropic messages.create with tools=, tool_choice='auto', multi-turn tool-result loops, extended-thinking blocks, surfacing tool_use_id. Expose run_tool_loop(system_prompt, user_msg, tools, dispatcher, max_turns=12) -> RunTrace (transport-only; dispatcher is injected). RunTrace dataclass captures tool calls, tokens_in/out, latency_ms, raw turns.
  Depends on: T16

- [ ] **T6:** Phase 2 Step 6 — Add new accessors in src/common/db_handler.py (async-in-thread) each with a matching src/common/storage_handler.py payload-builder (DEBT-028 two-layer pattern): create_topic, update_topic_state, get_topic_by_id, get_topic_by_canonical_key, add_topic_sources (handles (topic_id, message_id) UNIQUE), add_topic_alias, resolve_topic_by_alias, record_topic_transition (validates run_id exists in topic_editor_runs; topic_id NULLABLE for rejected creates), record_editorial_observation, search_topics_by_headline (trigram+alias join returning rows + trigram score), list_watching_topics, create_topic_editor_run, finish_topic_editor_run, get_topic_transition_rates(environment, guild_id, window_hours=168), AND the lean get_message_author_ids(message_ids: list[int]) -> dict[int, int] backed by `SELECT message_id, author_id FROM discord_messages WHERE message_id = ANY($1)`. Lean accessor docstring: missing message_id → caller responsibility to detect and treat as tool-error; duplicates deduped at caller; author_id NULL counts as phantom distinct author. Every NEW topic_* read accessor filters by (environment, guild_id) mandatorily with cross_env: bool = False escape hatch. DO NOT modify any existing live_update_* accessor.
  Depends on: T16, T2

- [ ] **T7:** Phase 2 Step 7 — Implement src/features/summarising/topic_tools.py with the 10 Anthropic tool schemas verbatim from docs/live-update-editor-redesign.md:264-391 (search_topics, search_messages, get_author_profile, get_message_context, post_simple_topic, post_sectioned_topic, watch_topic, update_topic_source_messages, discard_topic, record_observation). Write tools accept override_collisions: list[{topic_id, reason}]. search_topics is env-scoped at the dispatcher layer. If T1's defer-audit forced pending_review, add an 11th tool request_review_topic. Implement src/features/summarising/topic_dispatcher.py with TopicDispatcher.dispatch(tool_name, tool_use_id, args, run_ctx) enforcing all 4 invariants: Inv 1 structural (authoritative distinct_authors via _resolve_authors → get_message_author_ids; ≥2 distinct authors OR ≥3 source_message_ids → reject post_simple_topic, write topic_transitions row with topic_id=NULL action='rejected_post_simple'); Inv 2 collision+override (canonicalize → search_topics_by_headline returns rows+trigram; compute author_overlap; match at canonical-key-prefix OR (trigram≥0.55 AND author_overlap≥1); reject if matched topic_id not in override_collisions, action='rejected_*'; if override present, accept write + log action='override' per matched topic_id with reason in payload); Inv 3 idempotency (check (run_id, tool_call_id) first); Inv 4 source uniqueness (rely on (topic_id, message_id) UNIQUE; on conflict log rejected_*). Missing message_id in author lookup → tool-error.
  Depends on: T16, T6

- [ ] **T8:** Phase 2 Step 8 — Implement src/features/summarising/topic_editor.py with TopicEditor.run_once(window, environment, guild_id): insert a topic_editor_runs row via create_topic_editor_run FIRST (if that insert fails, abort run_once loudly with raised exception — do NOT swallow); assemble context (new source messages, list_watching_topics env-scoped, recently-posted topics); call topic_editor_client.run_tool_loop with TopicDispatcher; close via finish_topic_editor_run; return a RunTrace. Implement src/features/summarising/topic_editor_prompt.py — system prompt covering editorial taste from _meets_editorial_bar (live_update_prompts.py:903), cap record_observation ~3/run, mention request_review_topic only if T1 fired the pending_review branch. Note: concurrent-run lease deferred to Phase 1.5; trace embed surfaces 'lease: DEFERRED'.
  Depends on: T16, T5, T7

- [ ] **T9:** Phase 3 Step 9 — Implement src/features/summarising/topic_publisher.py with pure render_topic(topic) -> list[RenderedMessage]. No DB, no Discord network. Forms: simple (single message with headline/body/media/jump-link), sectioned (header + N section messages with per-section media/jump-links), story-update (prefix 'Update:' + parent.discord_message_ids[0] link), partial-publish recovery path (exercised but not flipped on — publishing remains OFF in Phase 1). Write tests/test_topic_publisher_render.py covering all four forms; asserts no DB/Discord calls.
  Depends on: T16

- [ ] **T10:** Phase 3 Step 10 — Implement src/features/summarising/topic_trace_embed.py with build_trace_embed(run_trace, divergence, rolling_rates) returning a Discord embed payload containing: archive-env / topic-env header, source-message count, tool calls fired by name, topics posted/watched/updated/discarded counts, near-miss observations count, cost (tokens_in/out + estimated $), latency_ms, per-run override-rate, 7-day rolling override-rate (via get_topic_transition_rates), record_observation rate (per-run + rolling), partial-publish incidents, lease: DEFERRED indicator, per-window divergence-vs-today summary. Resolve trace channel via NEW env var DEV_SHADOW_TRACE_CHANNEL_ID (unset → log-only, no send).
  Depends on: T16, T6, T8

- [ ] **T11:** Phase 4 Step 11 — Build tests/fakes/topic_store.py as a stateful in-memory fake intercepting at the DatabaseHandler seam (NOT StorageHandler). Mandatory module docstring with a one-table caller-map: every fake method ↔ production DatabaseHandler accessor it satisfies, with exact signature + return shape. Backed by Python dicts/sets modeling: env-scoped indexes by (environment, guild_id); (topic_id, message_id) UNIQUE enforcement; (environment, guild_id, alias_key) UNIQUE alias resolution; idempotency on (run_id, tool_call_id); per-run transition log; run_id FK validation against fake topic_editor_runs. Similarity: use similarity_pairs.json oracle for any pair present; difflib.SequenceMatcher.ratio ONLY as retrieval placeholder for unknown pairs. Wire into tests/conftest.py as a pytest fixture for dependency injection on TopicDispatcher and TopicEditor.
  Depends on: T16, T3, T6, T7

- [ ] **T12:** Phase 4 — Write tests/test_topic_dispatcher_invariants.py and tests/test_topic_aliases_resolution.py against the fake store. Invariants test exercises all 4: (1) post_simple_topic rejected when authors≥2 OR len(source_message_ids)≥3 with rejected_* row in topic_transitions (topic_id=NULL); (2) write-time collision returns existing topic_id+aliases as tool error AND override_collisions=[{topic_id, reason}] accepts the write while logging action='override'; (3) idempotency on Anthropic tool_use_id replays no-ops; (4) (topic_id, message_id) UNIQUE rejects duplicate add, multiple topics for one message accepted. Aliases test: search_topics returns canonical_key + all topic_aliases rows; dispatcher resolves a drift-spelled proposed_key through aliases to existing topic_id; new aliases of alias_kind='proposed' written each call. Includes the same-window-rolled-twice test: same fixture window dispatched in two different runs resolves to same topic_id via alias.
  Depends on: T16, T11

- [ ] **T13:** Phase 4 Step 12 — Write scripts/run_live_update_shadow.py modeled on scripts/run_live_update_dev.py but NOT routing through LiveUpdateEditor. CLI flags: --archive-env (default 'prod', source-message + telemetry-only legacy reads), --topic-env (default 'dev', new topic_* writes + topic_editor_runs), --windows N (default 20), --lookback HOURS, --once. Surface both env knobs in the trace-embed header to prevent cross-wiring. Per window: run TopicEditor with publishing OFF (no channel.send to live feed), post one trace embed per window to DEV_SHADOW_TRACE_CHANNEL_ID. After all windows: post an aggregate summary embed. Deliberately roll one window twice and assert alias resolution to the same topic_id. live_update_candidates / live_update_feed_items reads are TELEMETRY-ONLY for divergence field — never feed back into dispatcher state. Add a code-level comment marker on those calls.
  Depends on: T16, T8, T10

- [ ] **T14:** Phase 4 Step 13 — Write tests/test_shadow_mode_replay.py smoke test using the fake store. Fixture window → TopicEditor.run_once → assert topic_transitions rows recorded with expected actions, no channel.send invoked (mocked), trace-embed payload contains: archive-env/topic-env header, source-message count, tool calls by name, posted/watched/updated/discarded tallies, near-miss observations, cost (tokens), latency, per-run override-rate, 7-day rolling override-rate, record_observation rate, partial-publish incidents, lease: DEFERRED. Verify no live DB required (fake store at DatabaseHandler seam).
  Depends on: T16, T13

- [ ] **T15:** Run the validation suite per the plan's Validation Order: (1) pytest tests/test_topic_canonicalizer.py tests/test_topic_similarity_oracle.py; (2) pytest tests/test_topic_publisher_render.py; (3) pytest tests/test_topic_dispatcher_invariants.py tests/test_topic_aliases_resolution.py; (4) pytest tests/test_shadow_mode_replay.py; (5) pytest tests/test_live_update_editor_lifecycle.py tests/test_live_update_editor_publishing.py tests/test_live_update_prompts.py (existing tests MUST remain green untouched). If any test fails, read the error, fix the code, and re-run until green. Then write a throwaway script that imports topic_editor + dispatcher + fake store, dispatches a fixture window, prints the resulting topic_transitions log + trace embed dict to stdout — confirm the dispatcher invariants fire (e.g. force a 2-author post_simple_topic call and observe rejected_post_simple row), then delete the script. Verify the diff is contained: `git diff --stat` should show ZERO modifications to src/features/summarising/live_update_editor.py, src/features/summarising/live_update_prompts.py, src/common/llm/claude_client.py. Verify net new LOC is in the 800-1500 range (alarm above 1500).
  Depends on: T16, T4, T9, T12, T14

## Watch Items

- PUBLISHING MUST STAY OFF — no channel.send against the live feed channel from the new editor. Trace/preview embeds go ONLY to DEV_SHADOW_TRACE_CHANNEL_ID (#main_test).
- DO NOT MODIFY src/features/summarising/live_update_editor.py, live_update_prompts.py, or claude_client.py:generate_chat_completion. Bit-for-bit identical so divergence diff is grounded.
- DO NOT touch any existing live_update_* table or DB accessor. New editor writes ONLY to the 6 new tables.
- Migration stays in .migrations_staging/ for Phase 1. Do not promote until Week-1 decision gate passes.
- Env-scoping invariant applies ONLY to NEW topic_* read accessors. Do not 'fix' env-handling on existing live_update_* accessors — that violates the no-modify constraint.
- Shadow runner's reads of live_update_candidates / live_update_feed_items are TELEMETRY-ONLY for the divergence field. Never feed back into dispatcher state. Mark the call sites with a code comment.
- topic_transitions.run_id NOT NULL + FK ON DELETE RESTRICT means create_topic_editor_run failure must abort run_once loudly — do not swallow or use synthetic IDs.
- Lean get_message_author_ids contract: missing message_id → caller treats as tool-error (fails dispatch); duplicates deduped; author_id NULL counts as phantom distinct author (multi-author guardrail fails closed).
- Dispatcher author resolution uses lean get_message_author_ids on hot path. The heavy get_live_update_context_for_message_ids stays untouched (used only by existing prompt assembly).
- Every dispatcher rejection writes a topic_transitions row with action='rejected_*' and topic_id=NULL for failed creates. Every override writes action='override' with reason in payload.
- Write tools (post_simple_topic, post_sectioned_topic, watch_topic) MUST accept override_collisions: list[{topic_id, reason}]. Without it the agent traps in retry loops on false positives.
- Anthropic tool_use_id is THE idempotency key. Do not invent a parallel UUID for replay protection.
- Similarity threshold tuning asserts against tests/fixtures/similarity_pairs.json oracle (pinned pg_trgm output), NOT difflib. Conftest guardrail must fail-fast if a threshold test references a pair not in the oracle.
- Fake store intercepts at DatabaseHandler seam (production parity), NOT StorageHandler. Caller-map docstring is mandatory and signature-aligned with production accessors.
- LOC budget: 800-1000 target, 1500 alarm. If implementation grows past 1500 LOC raise scope alarm — sign of Phase-1.5 creep (run-lease, partial-publish retry, polishing).
- Concurrent-run lease is DEFERRED to Phase 1.5. Trace embed shows 'lease: DEFERRED' as a known deviation, not a bug.
- Pending_review conditional branch: only ships in Day-1 migration if T1 defer-audit shows ≥5/week acted-upon defers. Borderline 3-5/week → defer to v2 unless POM explicitly intervenes.
- Every new db_handler.py method needs a matching storage_handler.py payload-builder counterpart (DEBT-028 two-layer pattern).
- Phase 2 prod cutover, _legacy_* renames, run-lease/checkpoint, full partial-publish retry history are OUT OF SCOPE. Stop at Week-1 shadow decision gate.
- Existing tests tests/test_live_update_editor_lifecycle.py / _publishing.py / _prompts.py must remain green untouched.

## Sense Checks

- **SC1** (T1): Does docs/live-update-editor-phase1-locked-decisions.md exist, resolve all 5 locked decisions, include the defer-audit numerical outcome with a clear ship-Day-1-or-v2 verdict for pending_review, document the borderline 3-5/week rule, and record the implementer judgment-call contracts (lean accessor missing/duplicate/null behavior, run-insert failure aborts loudly)?
  Executor note: [MISSING]

- **SC2** (T2): Does the migration file create exactly 6 tables (5 spec + topic_editor_runs), declare topic_transitions.run_id REFERENCES topic_editor_runs(run_id) ON DELETE RESTRICT, allow topic_transitions.topic_id NULL, have topic_sources UNIQUE on (topic_id, message_id) only, include CREATE EXTENSION pg_trgm + topics_headline_trgm GIN index, and avoid touching any live_update_* table?
  Executor note: [MISSING]

- **SC3** (T3): Do canonicalizer fuzz tests pass on the 50-headline fixture, and does the pinned-oracle similarity test pass on every row at threshold 0.55? Is the conftest guardrail asserting threshold-test pairs are oracle-covered in place?
  Executor note: [MISSING]

- **SC4** (T4): Does the backfill script filter source rows to status='posted' AND len(discord_message_ids) > 0, use the lean get_message_author_ids (NOT the heavy context accessor), call topic_canonicalizer.canonicalize, and write topics + topic_sources idempotently on (environment, guild_id, canonical_key)?
  Executor note: [MISSING]

- **SC5** (T5): Is topic_editor_client.py a sibling to ClaudeClient (claude_client.py:generate_chat_completion untouched), supporting tools=, tool_choice, tool_use_id round-trips, extended-thinking blocks, and multi-turn tool-result loops?
  Executor note: [MISSING]

- **SC6** (T6): Are all new db_handler methods backed by matching storage_handler payload-builders (DEBT-028)? Does every NEW topic_* read accessor mandatorily filter by (environment, guild_id) with cross_env=False as the only escape? Is get_message_author_ids implemented as a lean SELECT against discord_messages with documented missing/duplicate/null contract? Are existing live_update_* accessors unchanged?
  Executor note: [MISSING]

- **SC7** (T7): Do all 10 (or 11) Anthropic tool schemas match the v6 spec verbatim with override_collisions on the three write tools? Does the dispatcher enforce all 4 invariants with authoritative author resolution via get_message_author_ids? Does every rejection write a topic_transitions row with action='rejected_*' (topic_id=NULL for failed creates) and every override write action='override' with reason in payload?
  Executor note: [MISSING]

- **SC8** (T8): Does TopicEditor.run_once insert the topic_editor_runs row first and abort loudly on failure (no synthetic run_id fallback)? Does it close via finish_topic_editor_run and return a RunTrace? Is the prompt covering the editorial taste from _meets_editorial_bar and capping observations ~3/run?
  Executor note: [MISSING]

- **SC9** (T9): Does render_topic produce correct output for simple, sectioned, story-update (with 'Update:' prefix + parent link), and partial-publish recovery forms, with zero DB or Discord network calls?
  Executor note: [MISSING]

- **SC10** (T10): Does the trace embed surface all required fields including archive-env/topic-env header, per-run + 7-day rolling override-rate via get_topic_transition_rates, record_observation rate, partial-publish incidents, lease: DEFERRED, and per-window divergence? Is DEV_SHADOW_TRACE_CHANNEL_ID the channel resolver?
  Executor note: [MISSING]

- **SC11** (T11): Does tests/fakes/topic_store.py intercept at the DatabaseHandler seam (not StorageHandler), carry a mandatory caller-map docstring (fake method ↔ production accessor with signature + return shape), enforce (topic_id, message_id) UNIQUE / env-scoped reads / alias resolution / idempotency on (run_id, tool_call_id), and use the similarity_pairs.json oracle for known pairs?
  Executor note: [MISSING]

- **SC12** (T12): Do dispatcher and alias tests cover all 4 invariants (incl. override path and (topic_id, message_id) duplicate behavior) and the same-window-rolled-twice alias resolution? Do they run entirely against the fake store with no live DB dependency?
  Executor note: [MISSING]

- **SC13** (T13): Does scripts/run_live_update_shadow.py take --archive-env and --topic-env flags, NOT route through LiveUpdateEditor, load ≥20 historical source-message windows, post per-window + aggregate trace embeds to DEV_SHADOW_TRACE_CHANNEL_ID, deliberately roll one window twice for alias-resolution check, and mark the live_update_* reads as telemetry-only with a code comment?
  Executor note: [MISSING]

- **SC14** (T14): Does the shadow smoke test exercise the full editor with publishing OFF using the fake store, assert topic_transitions rows recorded, assert no channel.send invoked (mock), and verify the trace-embed payload contains every required field including the env header and lease: DEFERRED?
  Executor note: [MISSING]

- **SC15** (T15): Do all five validation-order steps pass green? Does `git diff --stat` confirm zero modifications to live_update_editor.py / live_update_prompts.py / claude_client.py? Is net new LOC in the 800-1500 range? Did the throwaway reproduction script exercise dispatcher rejection invariants and get deleted afterward?
  Executor note: [MISSING]

- **SC16** (T16): Were all before_execute user_actions programmatically verified before execution proceeded?
  Executor note: Verified programmatically (file/directory existence checks), not by reading user_actions.md alone. U1: no defer-audit artifact, no locked-decisions doc — number not delivered. U2: tests/fixtures/ directory absent; required fixture files do not exist anywhere in repo. Both are before_execute prerequisites; T16 marked blocked and execution stopped without touching T1–T15.

## Meta

Execution gotchas and judgment calls:

1) **T1 is gated on U1 (defer-audit SQL).** Do not start T2's migration DDL until you have the audit number — it determines whether the migration includes `state='pending_review'` as a documented topics.state value and whether T7's tool list grows to 11 tools. If U1 returns a borderline (3-5/week) volume, default to v2 unless POM has explicitly told you otherwise in U1's response.

2) **T3 is gated on U2 (similarity_pairs.json).** Do not write the pinned-oracle test before the fixture exists. The fixture is generated from real pg_trgm output once and committed; thereafter CI is hermetic.

3) **Task dependency T4 → T6:** The backfill script needs `get_message_author_ids` which lives in T6. T4 is sequenced after T6 even though it's described in Phase 1 of the plan — order tasks by code dependency, not phase number.

4) **Hard scope guards:**
   - `git diff --stat` after T15 must show ZERO modifications to `src/features/summarising/live_update_editor.py`, `src/features/summarising/live_update_prompts.py`, `src/common/llm/claude_client.py`. If you accidentally touched them while reading, `git checkout --` them.
   - The 5 new tables (+ topic_editor_runs = 6) are NEW tables only. Do not migrate, alter, or read-for-decision any `live_update_*` table. The shadow runner DOES read `live_update_candidates` / `live_update_feed_items` but ONLY for the telemetry-only divergence comparison; mark those call sites with a code comment so a future reviewer doesn't refactor them into the dispatcher.

5) **DEBT-028 two-layer pattern is mandatory.** Every new `db_handler.py` method must have a matching `storage_handler.py` payload-builder (hardcoded keys, follows the existing live_update_* shape at storage_handler.py:743-762). Don't shortcut by putting payload-building in db_handler.

6) **Fake store seam.** It intercepts at the `DatabaseHandler` seam, not the `StorageHandler` lower layer. The lower layer is pure Supabase payload building and not worth faking. The fake must enforce: (topic_id, message_id) UNIQUE, env-scoped reads, idempotency on (run_id, tool_call_id), alias resolution, and FK validation of run_id against fake topic_editor_runs.

7) **Similarity oracle leakage guard.** The fake store uses difflib for unknown pairs as a retrieval placeholder ONLY. Add a conftest assertion that fails fast if any pair used in a threshold-tuning test isn't in `similarity_pairs.json`. This is a maintenance trip-wire so the FLAG-010 fix doesn't decay.

8) **Run-insert failure semantics.** With `topic_transitions.run_id NOT NULL` + FK ON DELETE RESTRICT, the parent run row must exist before any transition write. If `create_topic_editor_run` raises in T8's `run_once`, abort loudly and surface to the runner's stderr — do NOT use a synthetic run_id and do NOT swallow. The trace embed for that window will be missing, which is the correct loud-failure signal.

9) **Lean-accessor contract on edge cases:**
   - Missing `message_id` in `get_message_author_ids` result → caller treats as tool-error (fails the dispatch). For invariant #1's structural check, this means the dispatcher REJECTS the call rather than silently under-counting authors.
   - Duplicate `message_id` in input → dedupe at the caller.
   - `author_id IS NULL` (webhook/system message) → count as a phantom distinct author, so multi-author guardrail fails closed.

10) **Trace embed channel default.** Use a NEW env var `DEV_SHADOW_TRACE_CHANNEL_ID`. Do NOT reuse `DEV_LIVE_UPDATE_CHANNEL_ID` — that would collide with the existing reasoning-embed pipeline.

11) **LOC budget alarm.** Net new code stays in 800-1500 LOC. If you cross 1500, you've probably smuggled in Phase 1.5 work (run-lease, partial-publish retry, polish). Stop and triage.

12) **Validation order at T15.** Run tests in dependency order (pure → fake-store → existing untouched). Existing `tests/test_live_update_editor_*.py` and `test_live_update_prompts.py` MUST remain green — if any flips red, your diff has accidentally touched the old editor path.

13) **The throwaway reproduction script in T15** should import the new dispatcher + fake store, deliberately trigger a 2-author `post_simple_topic` call, and assert the resulting topic_transitions log contains an `action='rejected_post_simple'` row with `topic_id=NULL`. Print the result, then delete the script (do NOT commit it).

14) **Phase 2 is out of scope.** When U5's decision-gate review concludes, STOP. Do not flip the editor over to the new system, do not rename old tables to `_legacy_*`, do not change the production publish path. Phase 2 is a separate plan.

## Coverage Gaps

- Tasks without executor updates: 15
- Sense-check acknowledgments missing: 15
