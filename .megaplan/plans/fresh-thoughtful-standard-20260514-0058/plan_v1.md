# Implementation Plan: Topic Editor Media Understanding

## Overview
Add cache-backed image and video understanding to the BNDC topic editor without touching the legacy live-update editor. The existing runtime is concentrated in `src/features/summarising/topic_editor.py`: tools are declared in `TOPIC_EDITOR_TOOLS`, read tools dispatch through `_dispatch_read_tool`, source payloads are built by `_build_initial_user_payload` and `_message_payload`, and run-local state lives in `dispatcher_context` inside `run_once`. Persistence wrappers follow the existing pattern of sync methods in `src/common/db_handler.py` delegating to async Supabase calls in `src/common/storage_handler.py`.

The simplest implementation is to keep the new vision integration as a small isolated service module, add thin persistence wrappers, and make the topic editor call it only from new read tools. Existing finalize/idempotency/collision/lease logic should remain structurally untouched.

## Phase 1: Database And Persistence

### Step 1: Add mirrored migration (`.migrations_staging/`, `../supabase/migrations/`)
**Scope:** Small
1. Create a timestamped SQL migration in `.migrations_staging/` and copy it unchanged into `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/`.
2. Define `public.message_media_understandings` exactly as requested, including the primary key and both indexes.
3. Keep this table independent of live editor state tables so no existing topic run/finalize migrations are affected.

### Step 2: Add Supabase storage methods (`src/common/storage_handler.py`)
**Scope:** Medium
1. Add `get_message_media_understanding(message_id, attachment_index, model)` selecting a single row by primary-key fields.
2. Add `get_message_media_understanding_by_hash(content_hash, model=None, media_kind=None)` for cross-message deduplication. Use `content_hash` only when non-empty, order by `created_at desc`, and return the first row.
3. Add `upsert_message_media_understanding(row)` using Supabase `upsert` against the primary key. Normalize `understanding` through existing JSON helper patterns such as `_as_json_object` / `_clean_payload`.
4. Do not add environment/guild gating to this table unless the schema changes; the requested table has neither column.

### Step 3: Add DB handler wrappers (`src/common/db_handler.py`)
**Scope:** Small
1. Add sync wrappers with the requested names: `get_message_media_understanding(...)` and `upsert_message_media_understanding(...)`.
2. Add a hash lookup wrapper even though it is not explicitly named in deliverable 3, because deliverable 3 requires fallback by `content_hash`.
3. Delegate through `_run_async_in_thread`, matching nearby topic-editor wrapper style.

## Phase 2: Vision Client Module

### Step 4: Implement `src/common/vision_clients.py`
**Scope:** Medium
1. Create a standalone module that does not import Astrid. Distill only the needed patterns from Astrid’s `visual_understand`, `video_understand`, and `llm_clients` files.
2. Implement `describe_image(image_bytes_or_url, model, query=None) -> dict`:
   - Lazy-import `openai` or use HTTP `urllib/request` against `/v1/responses`; fail at call time with a clear dependency/key error.
   - Accept bytes, local `Path`/string paths, or URL strings.
   - For bytes/path input, base64-encode as a data URL with a guessed MIME type.
   - Request JSON constrained to the image schema: `kind`, `subject`, `technical_signal`, `aesthetic_quality`, `discriminator_notes`.
3. Implement `describe_video(video_path_or_bytes, model) -> dict`:
   - Lazy-import `google.genai`; if current dependencies only provide `google-generativeai`, update `requirements.txt` to include `google-genai` because the brief specifically references that SDK.
   - For bytes input, materialize a temporary `.mp4` file before upload.
   - Upload with `files.upload`, poll until `ACTIVE`, then call `generate_content` with the sanitized response schema.
   - Use Astrid’s video schema fields verbatim and add `kind`.
4. Add shared helpers for `sha256` calculation, transient retry once, base64 materialization, JSON extraction, and `_sanitize_gemini_schema` that drops `additionalProperties` recursively.
5. Keep the module compact and free of Astrid debug-log machinery.

## Phase 3: Topic Editor Integration

### Step 5: Include attachments in source payloads (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Extend `_message_payload` to include `media_urls` from `attachments` and, if useful, embed media URLs.
2. Add a small helper to normalize attachment lists whether they arrive as JSON strings or Python lists. Prefer fields already stored by `logger_cog.py`: `url`, `filename`; also tolerate `proxy_url` and common embed media fields if present.
3. Preserve existing payload fields exactly.

### Step 6: Enrich initial payload with cached understandings (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. In `_build_initial_user_payload`, after building each source message payload, attach `media_understandings` for any cached rows.
2. Query by `(message_id, attachment_index, model)` for the fast image/video models, or add a compact DB helper that returns all understandings for a message. If only the requested wrappers are implemented, loop over attachment indexes and known model presets.
3. Shape each item as requested: `attachment_index`, `kind`, `subject`, `technical_signal`, `aesthetic_quality`, `model`, plus video fields when present.
4. Keep enrichment best-effort: missing DB methods or rows should produce an empty list, not fail the run.

### Step 7: Add read tools and dispatcher cases (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Add `understand_image` and `understand_video` to `READ_TOOL_NAMES` and `TOPIC_EDITOR_TOOLS` with schemas for `message_id`, `attachment_index`, and `mode` (`fast`/`best`, default `fast`).
2. Add model presets in the module:
   - image: `fast -> gpt-4o-mini`, `best -> gpt-5.4`
   - video: `fast -> gemini-2.5-flash`, `best -> gemini-2.5-pro`
3. Add `vision_budget_usd` and `vision_cost_usd` to `dispatcher_context`. Parse `TOPIC_EDITOR_VISION_BUDGET_PER_RUN`, defaulting to `1.0`.
4. Add a helper such as `_dispatch_understand_media_tool(call, context, media_kind)`:
   - Resolve the source message from `context["messages"]`.
   - Resolve the requested attachment and URL.
   - Check primary-key cache first.
   - Download bytes through existing `db.download_file(source_url)` or, if that is unsuitable in sync context, a small `requests`/`aiohttp` helper in the topic editor. Compute `sha256` from bytes.
   - Check hash cache second.
   - Before API call, check the run budget. If exceeded, return `{"outcome":"budget_exceeded"}`.
   - Call `vision_clients.describe_image` or `describe_video`.
   - Persist under the requested `(message_id, attachment_index, model)`; when hash cache was reused from another message, persist a new row for this message without calling the API.
5. Add a small fixed estimated cost per vision call to support deterministic budget tests. The exact estimate can be conservative and env-overridable later, but the budget counter must increment only when a real API call is attempted, not on cache hits.
6. Update `_tool_result_content` so these read tools return compact JSON results like the existing read tools.

### Step 8: Update the system prompt (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. Add one paragraph to `TOPIC_EDITOR_SYSTEM_PROMPT` explaining that media-bearing messages may include cached `media_understandings`.
2. Tell the agent to call `understand_image` / `understand_video` for uncached media when the media could affect editorial judgment.
3. Include the requested editorial guidance: skip `workflow_graph`, consider `generation` with `aesthetic_quality >= 6`, and cite `technical_signal` / `subject` in framing.

## Phase 4: Tests

### Step 9: Add focused unit tests for vision clients (`tests/`)
**Scope:** Medium
1. Stub OpenAI Responses and Gemini SDK modules with monkeypatching so no real API calls occur.
2. Test that `describe_image` returns parsed structured JSON and accepts bytes/URL input.
3. Test that `describe_video` uploads, polls to active, sanitizes schemas by removing `additionalProperties`, and returns parsed JSON.
4. Test missing SDK/API key errors at call time, not import time.

### Step 10: Add topic editor cache and budget tests (`tests/test_topic_editor_runtime.py` or a new focused file)
**Scope:** Medium
1. Extend `FakeDB` with in-memory `message_media_understandings` storage and hash lookup tracking.
2. Test cache-first behavior: two `understand_image` calls for the same message/index/model hit the stub API once.
3. Test cross-message dedup: same downloaded bytes on a different message reuse the hash-cached understanding and persist a row for the second message.
4. Test payload enrichment: `_build_initial_user_payload` includes cached `media_understandings` for media-bearing source messages.
5. Test budget cap: set `TOPIC_EDITOR_VISION_BUDGET_PER_RUN` below the estimated call cost and assert the dispatcher returns `budget_exceeded` without invoking the stub API.
6. Keep existing tests in `tests/test_topic_editor_runtime.py` and `tests/test_live_storage_wrappers.py` passing by making all new behavior additive.

## Execution Order
1. Land the migration and persistence wrappers first; they are additive and low risk.
2. Build and unit-test `vision_clients.py` independently with stubbed SDKs.
3. Add topic editor attachment normalization and payload enrichment before adding callable tools.
4. Add read tools, cache-first dispatch, and budget accounting.
5. Update the system prompt last, once the tool names and payload shape are final.

## Validation Order
1. Run focused tests while developing: `pytest tests/test_topic_editor_runtime.py -q` or a new media-specific test file.
2. Run client-only tests with monkeypatched SDKs to verify no real network/API calls occur.
3. Run required regression tests: `pytest tests/test_topic_editor_runtime.py tests/test_live_storage_wrappers.py -q`.
4. Optionally run a wider summarising/common test subset if import changes touch shared modules.

## Risk Notes
1. Current `requirements.txt` includes `google-generativeai`, but the brief specifies the newer `google-genai` SDK used by Astrid. I would add `google-genai` unless the repo already vendors it elsewhere.
2. `get_topic_editor_message_context` currently does not select attachments, but the run source messages from `get_archived_messages_after_checkpoint` likely include them. The tool should use `context["messages"]` first and not rely on the message-context read tool.
3. The requested primary key includes `model`, so a fast and best understanding for the same attachment can coexist. Hash dedup should respect model to avoid reusing fast-model output as best-model output unless explicitly allowed later.
