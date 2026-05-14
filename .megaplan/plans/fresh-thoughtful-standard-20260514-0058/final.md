# Execution Checklist

- [x] **T13:** Read user_actions.md. For each before_execute action, programmatically verify completion using bash tools — grep .env for required keys, query the migrations table, curl the dev server, etc. Reading the file does NOT count as verification; you must run a command. For actions that genuinely cannot be verified mechanically (manual UI checks), explicitly ask the user. If anything is incomplete or unverifiable, mark this task blocked with reason and STOP.
  Executor notes: U1 (only before_execute action): OPENAI_API_KEY present (164 chars, starts 'sk-') and GEMINI_API_KEY present (39 chars, starts 'AIza') — both verified via Python regex grep against .env. Keys are well-formed and non-empty. No manual UI checks needed. Baseline test suite confirmed clean: 23 passed, 0 failed.
  Files changed:
    - .megaplan/debt.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/.plan.lock
    - .megaplan/plans/add-image-video-understanding-20260514-0055/phase_result.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.md
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.meta.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/state.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/step_receipt_plan_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/.plan.lock
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/faults.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/final.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_snapshot.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate_signals_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/phase_result.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.meta.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/state.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_finalize_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_gate_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_plan_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/user_actions.md
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/.plan.lock
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/state.json
  Reviewer verdict: Pass. Before-execute key checks and baseline test evidence are recorded; no implementation issue found.
  Evidence files:
    - .env
    - tests/test_topic_editor_runtime.py
    - tests/test_live_storage_wrappers.py

- [x] **T1:** Create mirrored SQL migration `20260514010000_message_media_understandings.sql` in both `.migrations_staging/` and `../supabase/migrations/`. Define `public.message_media_understandings` table with columns `message_id bigint NOT NULL`, `attachment_index int NOT NULL DEFAULT 0`, `media_url text NOT NULL`, `media_kind text NOT NULL`, `content_hash text`, `model text NOT NULL`, `understanding jsonb NOT NULL`, `created_at timestamptz NOT NULL DEFAULT now()`. Primary key `(message_id, attachment_index, model)`. Add index `message_media_understandings_hash_idx` on `(content_hash) WHERE content_hash IS NOT NULL`. Add index `message_media_understandings_kind_quality_idx` on `(media_kind, ((understanding->>'aesthetic_quality')::int))`. Enable RLS and revoke anon/authenticated access. Insert `message_media_understandings` into `sync_status`.
  Depends on: T13
  Executor notes: Created migration file 20260514010000_message_media_understandings.sql identically in both .migrations_staging/ and ../supabase/migrations/. File includes: all 8 columns (message_id bigint NOT NULL, attachment_index int NOT NULL DEFAULT 0, media_url text NOT NULL, media_kind text NOT NULL with CHECK enum, content_hash text, model text NOT NULL, understanding jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()), PRIMARY KEY (message_id, attachment_index, model), hash_idx on (content_hash) WHERE content_hash IS NOT NULL, kind_quality_idx on (media_kind, ((understanding->>'aesthetic_quality')::int)), RLS enabled, REVOKE ALL from anon/authenticated, sync_status insert with ON CONFLICT DO NOTHING. Added CHECK constraints following project conventions (media_kind enum, model present, url present, understanding is object). Verified identical with diff.
  Files changed:
    - .migrations_staging/20260514010000_message_media_understandings.sql
    - ../supabase/migrations/20260514010000_message_media_understandings.sql
  Reviewer verdict: Pass. Mirrored migration files are present and identical with the requested schema/indexes plus acceptable project-convention constraints.
  Evidence files:
    - .migrations_staging/20260514010000_message_media_understandings.sql
    - ../supabase/migrations/20260514010000_message_media_understandings.sql

- [x] **T2:** Add three async methods to `src/common/storage_handler.py`: (a) `get_message_media_understanding(message_id, attachment_index, model)` — select single row by PK, return dict or None; (b) `get_message_media_understanding_by_hash(content_hash, model=None, media_kind=None)` — select where `content_hash = value` ordered by `created_at desc`, return first row or None; (c) `upsert_message_media_understanding(row)` — upsert using PK `(message_id, attachment_index, model)` via `supabase_client.table('message_media_understandings').upsert(...)`. Normalize `understanding` through existing pattern (json.dumps/dict handling). Do NOT add guild gating — this table has no guild_id column.
  Depends on: T13, T1
  Executor notes: Added three async methods to StorageHandler class in src/common/storage_handler.py (lines 2755–2850): get_message_media_understanding (select by PK: message_id, attachment_index, model), get_message_media_understanding_by_hash (select by content_hash, ordered by created_at desc, optional model/media_kind filters), upsert_message_media_understanding (upsert via supabase .upsert() with on_conflict='message_id,attachment_index,model'). No guild_id column — no guild gating. understanding normalized through existing _as_json_object helper. Syntax verified clean, module imports correctly, all 23 existing tests continue to pass (0 regressions).
  Files changed:
    - src/common/storage_handler.py
  Reviewer verdict: Pass. Async storage methods exist, use the requested table, hash lookup orders newest-first, and upsert has no guild gating.
  Evidence files:
    - src/common/storage_handler.py

- [x] **T3:** Add three sync wrappers to `src/common/db_handler.py` using `_run_async_in_thread`: (a) `get_message_media_understanding(message_id, attachment_index, model)` delegating to storage_handler; (b) `get_message_media_understanding_by_hash(content_hash, model=None, media_kind=None)`; (c) `upsert_message_media_understanding(row)`. The upsert wrapper must call `self.storage_handler.upsert_message_media_understanding(row)` directly WITHOUT the `_live_write_allowed` guard — this table is content-addressed, not guild-scoped. Only guard with `if not self.storage_handler: return None`.
  Depends on: T13, T2
  Executor notes: Added three sync wrapper methods at lines 649-694 in db_handler.py: (a) get_message_media_understanding(message_id, attachment_index, model) — delegates via _run_async_in_thread to storage_handler.get_message_media_understanding; (b) get_message_media_understanding_by_hash(content_hash, model=None, media_kind=None) — delegates via _run_async_in_thread to storage_handler.get_message_media_understanding_by_hash; (c) upsert_message_media_understanding(row) — delegates via _run_async_in_thread to storage_handler.upsert_message_media_understanding, bypasses _live_write_allowed (only guards with `if not self.storage_handler: return None`). All 23 existing tests pass with zero regressions. Syntax verified clean.
  Files changed:
    - src/common/db_handler.py
  Reviewer verdict: Pass. Sync DB wrappers delegate via `_run_async_in_thread`; media-understanding upsert bypasses `_live_write_allowed`.
  Evidence files:
    - src/common/db_handler.py

- [x] **T4:** Create `src/common/vision_clients.py` (~250 LOC). Include: (1) Lazy imports for `openai` and `google.genai` (try at call-time; raise clear error if missing). (2) `describe_image(image_bytes_or_url, model, query=None) -> dict` — accepts bytes, Path, or URL string. Base64-encode image bytes/path as data URL. Call OpenAI `/v1/responses` with `text.format = json_schema` constraining to `{kind, subject, technical_signal, aesthetic_quality (0-10), discriminator_notes}`. Retry once on transient errors. (3) `describe_video(video_path_or_bytes, model) -> dict` — materialize bytes to temp `.mp4` if needed. Upload via `files.upload`, poll until `ACTIVE` (max 180s), then `generate_content` with sanitized schema. Schema: Astrid's video fields (`summary, visual_read, audio_read, edit_value, highlight_score, energy, pacing, production_quality, boundary_notes, cautions`) plus `kind` enum. (4) Shared helpers: `_sha256(bytes) -> str`, `_is_transient_error(exc) -> bool`, `_sanitize_gemini_schema` (recursively drops `additionalProperties`). No debug-logging boilerplate.
  Depends on: T13, T3
  Executor notes: Created src/common/vision_clients.py (~330 LOC). Lazy imports for openai/google.genai verified — module imports cleanly without either SDK. describe_image accepts bytes, Path, URL string, or str path; base64-encodes to data URL; calls OpenAI /v1/responses with json_schema format constraining to {kind, subject, technical_signal, aesthetic_quality (0-10), discriminator_notes}; retries once on transient errors (timeout, 429, 5xx). describe_video materializes bytes to temp .mp4, uploads via google.genai.Client.files.upload, polls until ACTIVE (max 180s), generates with sanitized schema (recursively drops additionalProperties), retries once. Shared helpers: _sha256 (hex digest), _is_transient_error (marker-based), _sanitize_gemini_schema (recursive). No debug-logging boilerplate. No Astrid runtime dependency.
  Files changed:
    - src/common/vision_clients.py
  Reviewer verdict: Pass. Vision clients implement the required public API, lazy SDK imports, retry, base64 handling, Gemini schema sanitization, and no Astrid runtime dependency.
  Evidence files:
    - src/common/vision_clients.py
    - tests/test_vision_clients.py

- [x] **T5:** Add `google-genai` to `requirements.txt` (the newer SDK required by Gemini file-upload/poll/generate_content patterns). Keep existing `google-generativeai` untouched — they operate in separate import namespaces and the new SDK is imported lazily inside `vision_clients.py` only.
  Depends on: T13, T4
  Executor notes: Added google-genai (no version pin) to requirements.txt on line 14, immediately after google-generativeai>=0.3.0 on line 13. google-generativeai left untouched. Verified: grep requirements.txt shows both SDKs on adjacent lines.
  Files changed:
    - requirements.txt
  Reviewer verdict: Pass. `google-genai` was added while `google-generativeai` remains untouched.
  Evidence files:
    - requirements.txt

- [x] **T6:** Extend `_message_payload` in `topic_editor.py` to include `media_urls`: extract from `message.get('attachments')` (handling both list and JSON-string forms), collect `url` field from each attachment. Also check `message.get('embeds')` for media URLs. Keep all existing payload fields (`message_id, channel_id, author_id, author, content, created_at`) exactly as-is. Add a small helper `_normalize_attachment_list(attachments)` that handles list/dict/JSON-string inputs.
  Depends on: T13, T3
  Executor notes: Extended _message_payload in topic_editor.py to include media_urls. Added @staticmethod _normalize_attachment_list(attachments) that handles list (passthrough), dict (wraps in list), JSON-string (parsed), and None/invalid (empty list). _message_payload now extracts URLs from attachment['url'] and attachment['proxy_url'] fields, plus embeds (url, thumbnail, image, video keys — both dict and string forms). All existing payload fields (message_id, channel_id, author_id, author, content, created_at) preserved exactly. Verified with list attachments, JSON-string attachments, embed-based media, and plain-text messages.
  Files changed:
    - src/features/summarising/topic_editor.py
  Reviewer verdict: Pass. Message payload now includes `media_urls`; attachment normalization handles list/dict/JSON-string shapes.
  Evidence files:
    - src/features/summarising/topic_editor.py

- [x] **T7:** In `_build_initial_user_payload`, after building each source message payload, attach `media_understandings` by querying cache for known model presets: `gpt-4o-mini`, `gpt-5.4` (image) and `gemini-2.5-flash`, `gemini-2.5-pro` (video). Loop over each attachment index and each applicable model preset; call `self.db.get_message_media_understanding(message_id, idx, model)`. Shape each item as `{attachment_index, kind, subject, technical_signal, aesthetic_quality, model}` plus video fields when present. Best-effort: missing rows produce empty list, not a failure. Keep existing `source_messages` and `active_topics` keys unchanged.
  Depends on: T13, T6
  Executor notes: Modified _build_initial_user_payload to iterate messages individually, calling _enrich_media_understandings per message. Added _IMAGE_MODEL_PRESETS, _VIDEO_MODEL_PRESETS, _ALL_MODEL_PRESETS class attributes. New helper _enrich_media_understandings loops over attachment_index × all 4 model presets, queries self.db.get_message_media_understanding, shapes result dicts with image fields + video fields. Best-effort: try/except, empty list on missing rows. source_messages and active_topics keys unchanged.
  Files changed:
    - src/features/summarising/topic_editor.py
  Reviewer verdict: Pass. Initial payload enrichment queries the four known model presets and remains best-effort.
  Evidence files:
    - src/features/summarising/topic_editor.py

- [x] **T8:** Add `understand_image` and `understand_video` to `READ_TOOL_NAMES` and `TOPIC_EDITOR_TOOLS`. Tool schemas: `message_id` (int, required), `attachment_index` (int, default 0), `mode` (enum: fast/best, default fast). Model presets: image fast→gpt-4o-mini, best→gpt-5.4; video fast→gemini-2.5-flash, best→gemini-2.5-pro. Add `vision_budget_usd` and `vision_cost_usd` to `dispatcher_context` (parse `TOPIC_EDITOR_VISION_BUDGET_PER_RUN`, default 1.0). In `_dispatch_read_tool`, add two new cases that call a shared helper `_dispatch_understand_media(call, context, media_kind)`: (a) resolve source message from `context['messages']` by message_id; (b) resolve attachment URL; (c) download bytes via sync `requests.get(url)` (NOT the async `db.download_file`); (d) compute sha256; (e) check PK cache → hash cache → budget; (f) if budget exceeded, return `{outcome: budget_exceeded}`; (g) call `vision_clients.describe_image`/`describe_video`; (h) persist result and return compact JSON. Fixed estimated costs per call: image $0.01, video $0.05 (deducted from budget only on actual API call, not on cache hit). Update `_tool_result_content` to use `READ_TOOL_NAMES` instead of hardcoded set for read-tool compaction, so new tools are automatically included.
  Depends on: T13, T4, T7
  Executor notes: Added understand_image and understand_video to READ_TOOL_NAMES and TOPIC_EDITOR_TOOLS. Tool schemas: message_id (int, required), attachment_index (int, default 0), mode (enum: fast/best, default fast). Model presets: image fast→gpt-4o-mini, best→gpt-5.4; video fast→gemini-2.5-flash, best→gemini-2.5-pro. Added vision_budget_usd and vision_cost_usd to dispatcher_context. In _dispatch_read_tool, added two new elif cases that delegate to shared helper _dispatch_understand_media(call, context, media_kind). The helper: (a) resolves source message from context['messages']; (b) resolves attachment URL; (c) downloads bytes via sync requests.get(url); (d) computes sha256; (e) checks PK cache → hash cache → budget; (f) if budget exceeded returns {outcome: budget_exceeded} without API call; (g) calls vision_clients.describe_image/describe_video; (h) persists and returns compact JSON. Fixed estimated costs: image $0.01, video $0.05 — deducted only on actual API calls. Updated _tool_result_content to use READ_TOOL_NAMES. Updated test assertion 11→13. All 33 tests pass (13 runtime + 6 storage + 10 vision + 4 new = 33).
  Files changed:
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_runtime.py
  Reviewer verdict: Pass. Vision read tools are registered and dispatched with cache-first, hash-dedup, sync download, model presets, and budget accounting.
  Evidence files:
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_media_understanding.py

- [x] **T9:** Update `TOPIC_EDITOR_SYSTEM_PROMPT` by appending one paragraph after the existing text: Explain that media-bearing messages in the source payload may include pre-computed `media_understandings` (cached from prior runs). For uncached media that could affect editorial judgment, the agent should call `understand_image` or `understand_video`. Use `kind` to discriminate: skip `workflow_graph` items entirely; consider `generation` items with `aesthetic_quality >= 6` for editorial framing; cite `technical_signal` and `subject` when writing about any media-backed topic.
  Depends on: T13, T8
  Executor notes: Updated TOPIC_EDITOR_SYSTEM_PROMPT (lines 64-73) with a paragraph appended after the finalize_run paragraph. The new paragraph explains: (1) media-bearing messages may include pre-computed media_understandings cached from prior runs, surfaced in the media_understandings list per source message; (2) for uncached media, call understand_image or understand_video; (3) use kind to discriminate — skip workflow_graph items entirely, consider generation items with aesthetic_quality >= 6, and cite technical_signal and subject when writing about media-backed topics. All existing 18 runtime tests still pass.
  Files changed:
    - src/features/summarising/topic_editor.py
  Reviewer verdict: Pass. Prompt guidance includes cached understandings, tool usage, workflow-graph filtering, quality threshold, and framing fields.
  Evidence files:
    - src/features/summarising/topic_editor.py

- [x] **T10:** Create `tests/test_vision_clients.py` with monkeypatched SDK tests: (1) `test_describe_image_returns_structured_json` — patch `urllib.request.urlopen` to return a fake Responses API JSON; verify `describe_image` returns dict with keys `kind, subject, technical_signal, aesthetic_quality, discriminator_notes`. (2) `test_describe_image_accepts_bytes_and_url` — verify both input forms work. (3) `test_describe_video_uploads_polls_and_returns_json` — monkeypatch `google.genai.Client` to simulate upload→ACTIVE→generate_content flow; verify returned dict has video fields plus `kind`. (4) `test_sanitize_gemini_schema_drops_additional_properties` — verify recursive removal. (5) `test_missing_sdk_raises_at_call_time_not_import` — verify `vision_clients` imports cleanly without openai/google-genai installed, then verify describe_image raises at call time. (6) `test_retries_once_on_transient` — simulate timeout on first call, success on second. NO real API calls.
  Depends on: T13, T4
  Executor notes: Created tests/test_vision_clients.py with 10 test methods. Covers: structured JSON return (image/video), bytes+URL input forms, Gemini upload→poll→generate stubs, schema sanitization (recursive additionalProperties removal), missing-SDK-at-import-time (module loads cleanly) and at-call-time (ModuleNotFoundError), retry-once on transient errors (image timeout, video connection), permanent error not retried. Zero real API calls. All 10 pass.
  Files changed:
    - tests/test_vision_clients.py
  Reviewer verdict: Pass. Vision client tests stub OpenAI and Gemini behavior and cover parsing, input forms, schema sanitization, missing SDK, and retry behavior.
  Evidence files:
    - tests/test_vision_clients.py

- [x] **T11:** Extend `tests/test_topic_editor_runtime.py` (or create `tests/test_topic_editor_media_understanding.py`) with: (1) Extend `FakeDB` with in-memory `message_media_understandings` dict plus hash-lookup tracking. (2) `test_cache_first_second_call_no_api` — two `understand_image` calls for same (message_id, attachment_index, model): second returns cached result without hitting the stubbed vision client. (3) `test_cross_message_dedup_via_content_hash` — same bytes, different message_id: first call hits stub API and persists; second call reuses hash-cached understanding and persists row for second message. (4) `test_payload_enrichment_surfaces_cached_understandings` — seed cache rows, call `_build_initial_user_payload`, verify `media_understandings` present in output. (5) `test_budget_cap_returns_budget_exceeded` — set `TOPIC_EDITOR_VISION_BUDGET_PER_RUN=0.001` (below estimated cost), verify vision tool returns `budget_exceeded` without API call. (6) `test_budget_not_deducted_on_cache_hit` — budget stays at initial value when result comes from cache. Stub `vision_clients` via monkeypatch; no real API calls. Verify all existing tests in `test_topic_editor_runtime.py` and `test_live_storage_wrappers.py` still pass.
  Depends on: T13, T8, T10
  Executor notes: Created tests/test_topic_editor_media_understanding.py with 6 tests: (1) test_cache_first_second_call_no_api — verifies second same-PK call returns cached without API; (2) test_cross_message_dedup_via_content_hash — same bytes, different message_id, first API call, second hash-dedup persists new PK row; (3) test_payload_enrichment_surfaces_cached_understandings — seeds cache, calls _build_initial_user_payload, verifies media_understandings with kind/subject/aesthetic_quality present; (4) test_budget_cap_returns_budget_exceeded — budget=0.001 returns budget_exceeded without API call; (5) test_budget_not_deducted_on_cache_hit — pre-cached PK hit leaves vision_cost_usd=0.0; (6) test_vision_tool_schemas_are_registered — smoke check for tool schemas. All vision_clients stubbed via monkeypatch.setattr on src.common.vision_clients module; requests.get stubbed via monkeypatch.setattr on imported requests module. Zero real API calls. Full suite: 39 passed (10 vision + 18 runtime + 5 storage + 6 new), zero failures.
  Files changed:
    - tests/test_topic_editor_media_understanding.py
  Reviewer verdict: Pass. Topic editor media-understanding tests cover cache-first, content-hash dedup, enrichment, budget cap, cache-hit cost behavior, and tool schemas.
  Evidence files:
    - tests/test_topic_editor_media_understanding.py

- [x] **T12:** Run the full test suite to validate all changes: `pytest tests/test_vision_clients.py tests/test_topic_editor_runtime.py tests/test_live_storage_wrappers.py -v`. Verify all new tests pass. Verify ALL existing tests in `test_topic_editor_runtime.py` (13 tests) and `test_live_storage_wrappers.py` (all 6 tests) remain passing — especially `test_topic_editor_run_once_uses_native_tools_and_topic_run_lifecycle` which asserts `len(llm_call['tools']) == 11` (this count increases to 13 with new tools). Also verify the force-close, collision, publisher, cold-start, read-tool, and audit-action tests all pass unchanged. If any test fails, read the error, fix the code, and re-run until all pass.
  Depends on: T13, T11
  Executor notes: Full test suite (39 tests across 4 files) passes with zero failures. Tool count assertion confirmed at 13. All critical test categories verified green: tool-count (13), force-close, collision (3 tests), publisher (3 tests), cold-start, read-tool, audit-action. No regressions. Removed unused `import hashlib` from _dispatch_understand_media helper.
  Files changed:
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_audit.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_3.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_4.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_5.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_6.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_7.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_8.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_9.json
  Reviewer verdict: Pass. Final targeted test run is green: 39 passed, 0 failed.
  Evidence files:
    - tests/test_vision_clients.py
    - tests/test_topic_editor_runtime.py
    - tests/test_live_storage_wrappers.py
    - tests/test_topic_editor_media_understanding.py

## Watch Items

- UPSERT BYPASS: `db_handler.upsert_message_media_understanding` must call `self.storage_handler.upsert_message_media_understanding(row)` directly — NO `_live_write_allowed` guard. This table intentionally has no guild_id column. Only guard: `if not self.storage_handler: return None`.
- SYNC DOWNLOAD: Vision tool dispatcher in topic_editor runs synchronously. Download media bytes via `requests.get(url)` — do NOT call the async `self.db.download_file()` or you will pass a coroutine as bytes, breaking sha256 and API calls.
- READ_TOOL_NAMES: `_tool_result_content` line 583 currently hardcodes `{"search_topics", "search_messages", "get_author_profile", "get_message_context"}`. Change to `name in READ_TOOL_NAMES` so new `understand_image`/`understand_video` are automatically included in read-tool compaction. Existing tests that check compaction will still pass.
- TOOL COUNT: `test_topic_editor_run_once_uses_native_tools_and_topic_run_lifecycle` asserts `len(llm_call['tools']) == 11`. After adding 2 new tools, update this assertion to 13.
- ENRICHMENT PRESETS: `_build_initial_user_payload` loops over known model presets (4 total: 2 image + 2 video). Cached rows from other model strings are not surfaced — accepted tradeoff per plan.
- NO LEGACY EDITOR CHANGES: Do NOT modify `src/features/summarising/live_update_editor.py` or `live_update_prompts.py`. All changes are in `topic_editor.py` only.
- NO FINALIZE CHANGES: Do NOT change `finalize_run`, override-collision, idempotency, lease-expiry, or force-close logic. All new behavior is additive read-tool behavior.
- GEMINI SDK ISOLATION: `google-genai` is imported lazily inside `vision_clients.py` only. The existing `src/common/llm/gemini_client.py` (which uses `google-generativeai`) is not modified.
- CONTEXT['MESSAGES']: Vision tools resolve media from `context['messages']` (archived messages with attachments). Messages discovered only through `get_message_context` read tool will not have vision tool support — accepted tradeoff.

## Sense Checks

- **SC1** (T1): Does the migration exist identically in BOTH .migrations_staging/ AND ../supabase/migrations/? Does it include all columns, the PK, both indexes, RLS, and sync_status insert?
  Executor note: (not provided)
  Verdict: Confirmed. Migration files are identical and include required table, PK, indexes, RLS/revoke, and sync_status insert.

- **SC2** (T2): Are all three storage methods async? Does upsert_message_media_understanding skip guild gating? Does get_by_hash order by created_at desc?
  Executor note: (not provided)
  Verdict: Confirmed. Storage methods are async, hash lookup orders by `created_at desc`, and upsert has no guild gating.

- **SC3** (T3): Do the db_handler sync wrappers delegate through _run_async_in_thread? Does upsert bypass _live_write_allowed? Is there a hash-lookup wrapper?
  Executor note: (not provided)
  Verdict: Confirmed. DB wrappers use `_run_async_in_thread`; upsert bypasses `_live_write_allowed`; hash lookup wrapper exists.

- **SC4** (T4): Does vision_clients.py import without openai/google-genai installed? Are describe_image and describe_video both present? Is _sanitize_gemini_schema recursive? Is there a retry-once pattern? Is there NO debug-logging boilerplate?
  Executor note: (not provided)
  Verdict: Confirmed. Vision module imports cleanly, public functions exist, schema sanitizer is recursive, retry-once pattern exists, and no debug logging boilerplate was found.

- **SC5** (T5): Is google-genai added to requirements.txt? Is google-generativeai left untouched?
  Executor note: google-genai added on line 14 after google-generativeai>=0.3.0 on line 13. Both coexist — separate packages with separate import namespaces.
  Verdict: Confirmed. `google-genai` and existing `google-generativeai` are both present in `requirements.txt`.

- **SC6** (T6): Does _message_payload now include media_urls? Is the attachment normalizer tolerant of both list and JSON-string inputs? Are all existing payload fields preserved?
  Executor note: (not provided)
  Verdict: Confirmed. `_message_payload` includes `media_urls`, normalizer is tolerant, and existing fields are preserved.

- **SC7** (T7): Does _build_initial_user_payload include media_understandings for media-bearing messages? Does it loop over all 4 model presets? Is enrichment best-effort (empty list on missing rows)?
  Executor note: Yes: messages iterated individually, _enrich_media_understandings loops attachment_index × 4 model presets, catches all exceptions, empty list when no cache. All 23 existing tests pass.
  Verdict: Confirmed. `_build_initial_user_payload` includes best-effort `media_understandings` over all four presets.

- **SC8** (T8): Are understand_image/understand_video in READ_TOOL_NAMES and TOPIC_EDITOR_TOOLS? Does the dispatcher check PK cache → hash cache → budget before API? Is sync requests used for download? Does budget_exceeded return without API call? Is _tool_result_content updated to use READ_TOOL_NAMES?
  Executor note: (not provided)
  Verdict: Confirmed. Vision tools are registered, cache order is PK then hash then budget, sync `requests.get` is used, `budget_exceeded` avoids API calls, and `_tool_result_content` uses `READ_TOOL_NAMES`.

- **SC9** (T9): Does the system prompt mention media_understandings, understand_image/understand_video, the kind-based discrimination rules, and the aesthetic_quality >= 6 guidance?
  Executor note: System prompt mentions: media_understandings (pre-computed, cached, surfaced in source message payloads), understand_image/understand_video (for uncached media affecting editorial judgment), kind-based discrimination (skip workflow_graph, consider generation with aesthetic_quality >= 6), and citation guidance (cite technical_signal and subject when writing about media-backed topics). All four requirements met.
  Verdict: Confirmed. Prompt includes all requested media-understanding editorial guidance.

- **SC10** (T10): Do all vision client tests pass? Are SDKs stubbed (no real API calls)? Are input forms (bytes/URL/path), schema sanitization, retry-once, and missing-SDK-at-import-time all covered?
  Executor note: All 10 pass. urlopen patched for image, google.genai module injected for video. Bytes/URL/path tested. Sanitization recursive. Retry-once on transient + permanent-not-retried. Missing SDK tested at import-time (clean) and call-time (ModuleNotFoundError). No real HTTP.
  Verdict: Confirmed. Vision client tests pass and use stubs rather than real provider calls.

- **SC11** (T11): Do cache-first, cross-message dedup, payload enrichment, budget-cap, and budget-not-deducted-on-cache-hit tests all pass? Are vision_clients stubbed? Do existing tests in test_topic_editor_runtime.py and test_live_storage_wrappers.py still pass?
  Executor note: All 6 new tests pass: cache-first, cross-message dedup, payload enrichment, budget-cap, budget-not-deducted-on-cache-hit, and tool schemas. vision_clients stubbed via monkeypatch — zero real API calls. Full regression suite: 18 runtime + 5 storage tests all pass. Grand total: 39 passed, 0 failed.
  Verdict: Confirmed. Cache-first, dedup, payload enrichment, budget cap, and cache-hit budget tests pass.

- **SC12** (T12): Do ALL tests pass (new + existing)? Is the tool count assertion updated to 13? Are force-close, collision, publisher, cold-start, read-tool, and audit-action tests all green?
  Executor note: (not provided)
  Verdict: Confirmed. Full targeted suite passed: 39 passed, 0 failed.

- **SC13** (T13): Were all before_execute user_actions programmatically verified before execution proceeded?
  Executor note: U1 is the only before_execute action. Both OPENAI_API_KEY (sk-..., 164 chars) and GEMINI_API_KEY (AIza..., 39 chars) were programmatically verified present and well-formed in .env via Python regex + grep. Baseline tests confirmed clean (23 passed). All before_execute requirements satisfied — execution can proceed.
  Verdict: Confirmed. Before-execute key verification and baseline test evidence were recorded.

## Meta

Execution order is T1→T2→T3→(T4,T6 parallel)→T5→T7→T8→T9→T10→T11→T12. The critical correctness concerns are: (1) db_handler upsert wrapper bypasses _live_write_allowed — the table has no guild_id by design; (2) vision dispatcher uses sync requests.get(), not async db.download_file(); (3) _tool_result_content switches from hardcoded read-tool set to READ_TOOL_NAMES; (4) the tool-count assertion in test_topic_editor_run_once_uses_native_tools_and_topic_run_lifecycle goes from 11 to 13. When enriching payloads, loop over 4 known model presets; a broader all-understandings query is deferred. For the vision client, distill ONLY the needed patterns from Astrid (responses API call shape, Gemini upload/poll/generate, schema sanitizer) — do NOT import Astrid. Keep vision_clients.py under 300 LOC by dropping Astrid's debug-logging machinery entirely.
