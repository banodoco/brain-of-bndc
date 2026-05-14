# Implementation Plan: BNDC Topic Editor Media Understanding

## Overview
The current topic editor lives in `src/features/summarising/topic_editor.py` and exposes four read tools via `READ_TOOL_NAMES` at line 26, `TOPIC_EDITOR_TOOLS` at line 64, and `_dispatch_read_tool` at line 680. The initial payload is built at line 542 and `_message_payload` at line 1696 currently omits attachments entirely, so media URLs and cached understandings need to be added there.

Persistence is routed through `src/common/db_handler.py` sync wrappers around `src/common/storage_handler.py` async Supabase calls. The migration convention is to mirror the same SQL file into `.migrations_staging/` and `../supabase/migrations/`. Existing topic-editor tests are concentrated in `tests/test_topic_editor_runtime.py`; wrapper tests are in `tests/test_live_storage_wrappers.py`.

The implementation should port Astrid’s OpenAI Responses and Gemini `google-genai` patterns into local code only. It must not import Astrid at runtime and must not touch `src/features/summarising/live_update_editor.py` or `src/features/summarising/live_update_prompts.py`.

## Phase 1: Schema And Storage Foundation

### Step 1: Add mirrored migration (`.migrations_staging/`, `../supabase/migrations/`)
**Scope:** Small
1. Create a new timestamped migration after `20260513230500`, for example `20260514090000_message_media_understandings.sql`, in both migration directories.
2. Add the `public.message_media_understandings` table and the two requested indexes verbatim.
3. Keep the migration independent of topic-editor tables so cache rows can persist across editor implementation changes.

### Step 2: Add storage/db wrappers (`src/common/storage_handler.py`, `src/common/db_handler.py`)
**Scope:** Medium
1. In `StorageHandler`, add async methods near the topic editor helpers:
   - `get_message_media_understanding(message_id, attachment_index, model)` using the primary key lookup.
   - `get_message_media_understanding_by_hash(content_hash, model)` for cross-message dedup, constrained by model so cached JSON is not silently reused across different model rubrics.
   - `upsert_message_media_understanding(row)` using `on_conflict='message_id,attachment_index,model'`.
2. In `DatabaseHandler`, add sync wrappers that delegate through `_run_async_in_thread`, following the existing topic-editor wrapper style around `db_handler.py:388` and `db_handler.py:534`.
3. Update `tests/test_live_storage_wrappers.py` fake storage coverage so the new wrappers are reachable and route to `message_media_understandings`, without touching legacy live-update tables.

## Phase 2: Vision Client Module

### Step 3: Add `src/common/vision_clients.py`
**Scope:** Medium
1. Define constants for model presets and schemas:
   - Image schema: `kind`, `subject`, `technical_signal`, `aesthetic_quality`, `discriminator_notes`.
   - Video schema: Astrid’s `summary`, `visual_read`, `audio_read`, `edit_value`, `highlight_score`, `energy`, `pacing`, `production_quality`, `boundary_notes`, `cautions`, plus `kind`.
2. Implement `describe_image(image_bytes_or_url, model, query=None) -> dict` against OpenAI `/v1/responses` with `text.format=json_schema`, base64 data URLs for bytes, URL input support when supplied, JSON parsing, and one retry for transient errors.
3. Implement `describe_video(video_path_or_bytes, model) -> dict` with lazy `google-genai` imports, upload/poll-until-`ACTIVE`, `models.generate_content`, JSON response schema, schema sanitization that recursively drops `additionalProperties`, and one transient retry.
4. Keep imports lazy for OpenAI/Gemini SDKs or direct HTTP dependencies so importing `src.common.vision_clients` never fails if optional packages are missing; call-time errors should name the missing dependency or API key clearly.
5. Add tests that monkeypatch fake OpenAI/Gemini modules or client factories and verify no real network calls occur.

## Phase 3: Topic Editor Integration

### Step 4: Add media extraction helpers (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Add helpers near `_message_payload` to normalize `attachments` from either JSON strings or lists.
2. Extract attachment URL from common Discord fields such as `url` or `proxy_url`, media kind from `content_type` or filename extension, and produce stable `{attachment_index, media_url, media_kind}` objects.
3. Include `media_urls` in `_message_payload`, and optionally private attachment metadata in the dispatcher context so tool calls can resolve `message_id + attachment_index` back to a URL and kind.

### Step 5: Add cache-first vision dispatch (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Extend `READ_TOOL_NAMES` at `topic_editor.py:26` with `understand_image` and `understand_video`.
2. Add both tool schemas to `TOPIC_EDITOR_TOOLS` at `topic_editor.py:64`; `mode` should be enum `fast|best` and default to `fast` in dispatcher logic.
3. Initialize vision budget state in `dispatcher_context` at `topic_editor.py:334`:
   - `vision_budget_usd` from `TOPIC_EDITOR_VISION_BUDGET_PER_RUN`, default `1.0`.
   - `vision_cost_usd` starting at `0.0`.
4. Add `_dispatch_understand_media` used by `_dispatch_read_tool` at `topic_editor.py:680`:
   - Resolve message and attachment.
   - Pick model by media kind and mode: image fast/best = `gpt-4o-mini`/`gpt-5.4`; video fast/best = `gemini-2.5-flash`/`gemini-2.5-pro`.
   - Check primary cache by `(message_id, attachment_index, model)`.
   - Download/materialize bytes for hashing if needed, compute SHA-256, then check `(content_hash, model)` dedup.
   - If dedup hits, upsert a new row for the current message/attachment/model and return cached understanding.
   - If budget is exceeded before an API call, return `{"outcome": "budget_exceeded"}` without calling vision clients.
   - Otherwise call `describe_image` or `describe_video`, persist, increment estimated vision spend, and return structured JSON.
5. Update `_tool_result_content` at `topic_editor.py:571` so vision read results are returned like other read tools and remain capped/truncated.
6. Keep write-tool idempotency paths unchanged; these new tools are read tools only and should not create topic transitions.

### Step 6: Enrich initial payload with cached understandings (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. In `_build_initial_user_payload` at `topic_editor.py:542`, pass cached media understanding data into each source message payload.
2. For each media attachment, query the cache for the default fast image/video model based on media kind and add `media_understandings` entries with `attachment_index`, `model`, and the flattened understanding fields.
3. Keep payload enrichment cache-only. Do not download media or call vision APIs during initial payload construction.

### Step 7: Update system prompt (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. Add a concise paragraph to `TOPIC_EDITOR_SYSTEM_PROMPT` at `topic_editor.py:44` explaining:
   - Source messages may already include `media_understandings`.
   - For uncached media, call `understand_image` or `understand_video` only when visual evidence matters.
   - Skip `workflow_graph` and other low-value media unless technically relevant.
   - Prefer `generation` with `aesthetic_quality >= 6` for sharing.
   - Cite `subject` and `technical_signal` in editorial framing.

## Phase 4: Tests And Verification

### Step 8: Add focused tests (`tests/test_topic_editor_runtime.py`, new optional `tests/test_vision_clients.py`)
**Scope:** Large
1. Extend `FakeDB` in `tests/test_topic_editor_runtime.py:57` with media-understanding cache methods and counters.
2. Add a test for cache-first image understanding: first call populates through a stubbed `describe_image`, second call for the same `(message_id, attachment_index, model)` returns cached and does not call the stub again.
3. Add a cross-message dedup test: two messages with identical bytes/hash should result in one API call and two persisted rows.
4. Add a payload enrichment test that cached rows appear in `_build_initial_user_payload` under `media_understandings`.
5. Add a budget-cap test where `TOPIC_EDITOR_VISION_BUDGET_PER_RUN` is already exhausted or lower than the per-call estimate and the tool returns `budget_exceeded` without invoking the vision client.
6. Add client-level tests for JSON/schema behavior and lazy import failure messages using monkeypatched fake modules.
7. Update the existing assertion in `test_topic_editor_run_once_uses_native_tools_and_topic_run_lifecycle` at `tests/test_topic_editor_runtime.py:210` from `len(...) == 11` to include the two new tools.

## Execution Order
1. Land migration and storage/db wrappers first; these are cheap and unlock isolated wrapper tests.
2. Add `vision_clients.py` with direct unit tests before wiring it into the topic editor.
3. Add media extraction and payload enrichment next because they can be tested without API calls.
4. Add read-tool dispatch, cache-first behavior, and budget cap after the helper surfaces are stable.
5. Update prompt/tool schemas last, once the runtime behavior and tests match the new contract.

## Validation Order
1. Run targeted wrapper tests: `pytest tests/test_live_storage_wrappers.py`.
2. Run focused topic editor tests: `pytest tests/test_topic_editor_runtime.py`.
3. Run any new vision client tests: `pytest tests/test_vision_clients.py` if split into a separate file.
4. Finish with the requested regression set: `pytest tests/test_topic_editor_runtime.py tests/test_live_storage_wrappers.py`.
5. Optionally run the broader summarising tests if time permits, but do not expand scope into legacy live-update editor files.
