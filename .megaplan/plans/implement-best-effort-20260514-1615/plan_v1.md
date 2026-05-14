# Implementation Plan: Best-Effort External Media Refs for TopicEditor

## Overview
TopicEditor already has the right shape for this feature: source messages expose compact `media_refs_available`, structured blocks bind stable `media_refs`, and `render_topic_publish_units` interleaves block text with media units. The smallest durable change is to add `kind: "external"` as a third media-ref kind, keep Discord attachment/embed resolution untouched, and resolve/download external media only when a block actually binds that external ref for publishing. This avoids eagerly downloading every Reddit/X link in the source window while still giving the agent a stable ref it can attach to relevant blocks.

External extraction must be best-effort throughout: archived Discord attachments/embeds remain first-class and preferred; external URL detection/resolution/download failures become trace/log metadata plus a fallback URL unit, never a topic rejection or text-publishing failure.

## Phase 1: External Resolver Foundation

### Step 1: Add pure external-media helpers (`src/features/summarising/external_media_resolver.py`)
**Scope:** Medium
1. **Define** a shared safelist seeded from `src/features/curating/curator.py:133`: YouTube, Vimeo, TikTok, Streamable, Twitch, fixupx/fxtwitter/vxtwitter/twittpr, ddinstagram, rxddit; add direct canonical domains needed by the task such as `reddit.com`, `www.reddit.com`, `x.com`, `twitter.com`, `instagram.com`.
2. **Implement pure functions** for URL extraction, URL normalization, domain safelist checks, cache-key creation, content-hash calculation, content-type/media-kind classification, filename extension selection, and URL sanitization for logs.
3. **Keep functions testable** by avoiding hidden global I/O in helpers. Side effects should be behind injectable functions/classes.

### Step 2: Implement resolver side effects with strict safeguards (`src/features/summarising/external_media_resolver.py`)
**Scope:** Large
1. **Use `yt-dlp --dump-json` first** via an injectable subprocess runner with a hard timeout. Parse metadata for a direct media URL, selected format URL, thumbnail, or gallery media URL depending on platform output.
2. **Download only after metadata passes safeguards:** safelisted original URL, allowed content type (`image/*`, `video/*`, limited GIF/WebP), configurable max byte size, streaming byte cap, and total timeout. Do not domain-safelist ephemeral yt-dlp CDN hosts; rely on content-type, byte cap, timeout, and successful provenance from a safelisted original URL. Default the byte cap around practical Discord bot upload limits, not a tiny preview-cache size, because successful media is uploaded to Discord for hosting.
3. **Return structured outcomes** such as `resolved`, `cache_hit`, `downloaded`, `skipped_domain`, `metadata_failed`, `download_failed`, `oversize`, and `unsupported_content_type`, always including sanitized trace fields and never raising for normal extraction failures.
4. **Store downloaded files in a stable cache directory** under a configurable temp/cache root, using URL hash plus content hash in the filename. Do not log raw URLs containing query tokens.

## Phase 2: Cache Persistence

### Step 3: Add DB cache wrappers (`src/common/db_handler.py`, `src/common/storage_handler.py`)
**Scope:** Medium
1. **Add methods** like `get_external_media_cache(url_key)` and `upsert_external_media_cache(row)` in `db_handler.py`, delegating to async storage methods in `storage_handler.py` using the existing `_run_async_in_thread` pattern near `message_media_understandings`.
2. **Use a compact row shape:** `url_key`, sanitized/original URL as appropriate, `source_domain`, `status`, `content_hash`, `media_kind`, `content_type`, `file_path` or storage path, `resolved_url`, `failure_reason`, `metadata`, `created_at`, `updated_at`.
3. **Create the staged migration in this repo** under `.migrations_staging/` for the new `external_media_cache` table. Applying that migration to production and configuring deployment environment variables are operator follow-up actions after implementation, not prerequisites for local code execution.

## Phase 3: TopicEditor Contract and Payloads

### Step 4: Extend media ref normalization (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Update** `normalize_media_ref` at `src/features/summarising/topic_editor.py:3387` to accept canonical external refs: `{"message_id": "...", "kind": "external", "index": N}`.
2. **Preserve** existing shorthand and canonical attachment/embed behavior exactly.
3. **Update tool schema and system prompt** around `src/features/summarising/topic_editor.py:110` and `src/features/summarising/topic_editor.py:257` so the agent knows external refs are secondary and should prefer archived Discord attachment/embed refs when both exist.

### Step 5: Expose detected external refs in source payloads (`src/features/summarising/topic_editor.py`, `src/common/storage_handler.py`)
**Scope:** Medium
1. **Detect URLs** in message `content`/`clean_content` and embed fields in `_message_payload` at `src/features/summarising/topic_editor.py:3056`.
2. **Append** compact entries to `media_refs_available` with `kind: "external"`, `index`, `domain`, `url_present: true`, and a sanitized/display URL or original URL only if that is already acceptable in current source payloads.
3. **Mirror** the compact external-ref exposure in `StorageHandler.get_topic_editor_message_context` around `src/common/storage_handler.py:1350`, because `get_message_context` has a separate compact media list path.
4. **Do not auto-shortlist solely on external links** unless explicitly decided later; the current reaction-qualified media shortlist should keep its Discord media semantics so the old direct auto-post loop is not reintroduced.

### Step 6: Validate external refs without requiring extraction success (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Update** block-level media-ref validation around `src/features/summarising/topic_editor.py:1621` so `kind: "external"` validates only that the source message exists and the external URL index exists in detected safelisted URLs.
2. **Reject malformed/out-of-range refs** like existing attachment/embed refs, but do not reject because yt-dlp metadata/download failed.
3. **Store normalized external refs** in topic summaries through the existing `normalize_document_blocks` path.

## Phase 4: Publishing Integration

### Step 7: Render external media units with fallback URLs (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Extend** `_resolve_media_url_from_metadata` at `src/features/summarising/topic_editor.py:3703` only for pure URL lookup: external refs return the original detected external URL from message content/embed metadata by index.
2. **Keep extraction out of `render_topic_publish_units`** so rendering remains pure and deterministic. Media units can carry `ref`, `url`, and `kind`.

### Step 8: Resolve/download selected external media during publish (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. **In `_publish_topic` structured path** around `src/features/summarising/topic_editor.py:2720`, before flattening/sending media units, call the external resolver only for media units whose ref has `kind: "external"`.
2. **On resolver success**, send the downloaded file via `discord.File` in the same interleaved position as the media URL would have occupied. On failure, send the original external link text at that position.
3. **Do not block text publishing:** wrap each external resolution/send in local best-effort handling. If a file send fails, fall back to URL text for that unit and continue subsequent units.
4. **Record trace metadata** in the publish result and logs: cache hit, downloaded, skipped, fallback reason, sanitized URL key/domain, bytes/content type where available.
5. **Leave legacy/simple topic publishing unchanged** unless the topic has structured blocks, since external media refs are block-bound.

## Phase 5: Tests and Validation

### Step 9: Add unit tests for pure resolver behavior (`tests/test_external_media_resolver.py`)
**Scope:** Medium
1. **Cover** Reddit/X/Twitter/Instagram/TikTok-like URL detection, allowed/blocked domains, URL sanitization with query tokens, cache key stability, content-type classification, oversize detection, and metadata parsing.
2. **Stub** subprocess and download functions; no real network or yt-dlp execution in tests.

### Step 10: Extend TopicEditor core/runtime tests (`tests/test_topic_editor_core.py`, `tests/test_topic_editor_runtime.py`)
**Scope:** Large
1. **Core tests:** `normalize_media_ref` accepts `kind: "external"`; invalid external index still fails; existing attachment/embed tests remain unchanged.
2. **Payload tests:** `_message_payload` and `get_message_context` include external refs from message content and embeds while preserving attachment/embed refs first.
3. **Validation tests:** block refs to valid external URLs are accepted; out-of-range external refs reject with auditable transition; resolver failure does not reject post creation.
4. **Publishing tests:** cache hit avoids download, resolver success sends a `discord.File` in block order, resolver failure falls back to original link, oversize/unsupported content type falls back, and text blocks still publish when external media fails.
5. **Regression tests:** reaction-qualified Discord media auto-shortlisting still creates `watching` topics and does not direct-post at five reactions.

## Execution Order
1. Build and test pure resolver helpers first; this gives cheap feedback without touching TopicEditor behavior.
2. Add DB cache wrappers with fake DB/storage tests before runtime wiring.
3. Extend media-ref normalization and payload exposure next, preserving attachment/embed order.
4. Wire validation and pure rendering after the ref contract is stable.
5. Add publish-time resolver/file-send behavior last, because it has the most integration risk.
6. Finish with targeted TopicEditor tests, then the broader summarising test slice.

## Validation Order
1. `pytest tests/test_external_media_resolver.py`
2. `pytest tests/test_topic_editor_core.py`
3. `pytest tests/test_topic_editor_runtime.py -k "media_ref or publish or auto_shortlist or get_message_context"`
4. `pytest tests/test_topic_editor_media_understanding.py`
5. `pytest tests/test_live_top_creations.py tests/test_live_runtime_wiring.py` as regression coverage for the no-direct-auto-post constraint.

## Deployment Follow-Up
1. After implementation creates `.migrations_staging/20260514160000_external_media_cache.sql`, copy/apply it through the project’s normal Supabase migration path before deploying code that depends on the cache table.
2. Configure `EXTERNAL_MEDIA_CACHE_DIR` in the production environment if the default cache directory is not durable enough for the deployment.
3. Run the manual production smoke test with a Reddit or X external media ref after code and schema are deployed.
