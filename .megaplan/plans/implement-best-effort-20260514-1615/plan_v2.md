# Implementation Plan: Best-Effort External Media Refs for TopicEditor

## Overview
The critique does not indicate that the plan targets the wrong subsystem. The root path is still TopicEditor structured publishing: source payloads expose media refs, `normalize_media_ref` and `block_media_refs` validate/bind refs, `render_topic_publish_units` interleaves block content with media units, and `_publish_topic` sends the final Discord messages. The gaps are integration details: external URL indexes must be deterministic across all source-message paths, every media-kind branch must explicitly handle `attachment` / `embed` / `external`, resolved media downloads need a two-tier safety model, and the structured publisher must stop assuming every send unit is a plain text string.

The implementation remains lazy: detect and expose safelisted external URLs in source context, but only run yt-dlp/download for external refs selected in structured blocks at publish time. Discord attachment/embed refs remain preferred and must keep existing behavior. External extraction failure is always a fallback-to-link outcome, never a topic rejection or a reason to block text publishing.

## Phase 1: External Resolver Foundation

### Step 1: Add shared deterministic external URL helpers (`src/common/external_media.py`)
**Scope:** Medium
1. **Create** one shared pure helper, `extract_external_urls(message_or_metadata)`, that deterministically scans the same fields in the same order for all callers: message content/clean content first, then embed top-level URL fields, thumbnail/image/video URLs, and any other existing embed URL containers already handled by TopicEditor.
2. **Use this helper everywhere external indexes matter:** `TopicEditor._message_payload`, `StorageHandler.get_topic_editor_message_context`, validation, `_resolve_media_url_from_metadata`, `render_topic_publish_units` fallback lookup, and publish-time resolver lookup. Do not duplicate URL extraction logic in those call sites.
3. **Define** a safelist seeded from `src/features/curating/curator.py:133`: YouTube, Vimeo, TikTok, Streamable, Twitch, fixupx/fxtwitter/vxtwitter/twittpr, ddinstagram, rxddit; add canonical domains required by the task such as `reddit.com`, `www.reddit.com`, `x.com`, `twitter.com`, `instagram.com`.
4. **Define an explicit platform policy** in the same common module: short-form/social domains such as Reddit, X/Twitter mirrors, Instagram, TikTok, Streamable, ddinstagram, and rxddit may attempt lazy media resolution; long-form domains such as YouTube, Vimeo, and Twitch are exposed as external link refs but are fallback-link-only by default for TopicEditor Discord publishing.
5. **Implement pure helpers** for URL normalization, source-domain safelist checks, platform-policy lookup, cache-key creation, content-hash calculation, content-type/media-kind classification, Discord upload compatibility checks, filename extension selection, and URL sanitization for logs.

### Step 2: Implement resolver side effects with two-tier safety (`src/features/summarising/external_media_resolver.py`)
**Scope:** Large
1. **Require source provenance first:** only process original URLs whose source domain passes the safelist. Block non-safelisted source URLs before yt-dlp or HTTP download.
2. **Respect the platform policy before side effects:** fallback-link-only domains return a structured fallback outcome without running yt-dlp or HTTP download. This avoids inconsistent behavior for YouTube, Vimeo, Twitch, and other long-form sources that commonly exceed Discord upload limits.
3. **Run yt-dlp in metadata-only mode first** for resolution-eligible domains with `--dump-json`, an injectable subprocess runner, and a hard timeout. Parse metadata for direct media URLs, selected format URLs, thumbnails, or gallery items.
4. **Apply two-tier download safety:** resolved yt-dlp CDN URLs may be downloaded only when they came from a safelisted original URL and pass content-type, byte-cap, streaming byte-limit, timeout, and Discord upload compatibility checks. The resolved CDN host itself is logged as sanitized metadata but is not trusted without the safelisted-source provenance and media checks.
5. **Default the max byte cap** around practical Discord bot upload limits and make it configurable. Keep a hard cap even when configuration is larger.
6. **Return structured outcomes** such as `cache_hit`, `downloaded`, `skipped_domain`, `fallback_only_platform`, `metadata_failed`, `download_failed`, `oversize`, `unsupported_content_type`, and `not_discord_upload_compatible`. These are normal results, not exceptions.
7. **Store files** under a configurable cache directory using URL key plus content hash in the filename. Never log raw URLs containing query tokens; use sanitized display URLs and URL keys.

## Phase 2: Cache Persistence

### Step 3: Add cache wrappers and staged migration (`src/common/db_handler.py`, `src/common/storage_handler.py`, `.migrations_staging/`)
**Scope:** Medium
1. **Add** `get_external_media_cache(url_key)` and `upsert_external_media_cache(row)` in `db_handler.py`, delegating to async methods in `storage_handler.py` with the existing `_run_async_in_thread` pattern near `message_media_understandings`.
2. **Use a compact row shape:** `url_key`, `source_url_sanitized`, `source_domain`, `status`, `content_hash`, `media_kind`, `content_type`, `byte_size`, `file_path`, `resolved_url_sanitized`, `failure_reason`, `metadata`, `created_at`, and `updated_at`.
3. **Create** `.migrations_staging/20260514160000_external_media_cache.sql` for the new table and indexes. Applying this migration to production is a deployment follow-up, not a blocker for local implementation.

## Phase 3: TopicEditor Contract and Payloads

### Step 4: Extend media ref normalization and schemas (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Update** `normalize_media_ref` at `src/features/summarising/topic_editor.py:3387` to accept `{"message_id": "...", "kind": "external", "index": N}`.
2. **Preserve** existing shorthand and canonical attachment/embed behavior exactly.
3. **Update** prompt and tool schema text around `src/features/summarising/topic_editor.py:110` and `src/features/summarising/topic_editor.py:257` so agents know external refs are secondary, block-bound, and should be used only when relevant to the block.
4. **Make downstream kind handling explicit** anywhere normalized refs are consumed: attachment, embed, and external must each have their own branch. Do not keep any `else: embed` behavior.

### Step 5: Expose external refs in source payloads with stable indexes (`src/features/summarising/topic_editor.py`, `src/common/storage_handler.py`)
**Scope:** Medium
1. **Update** `TopicEditor._message_payload` at `src/features/summarising/topic_editor.py:3056` to append external entries from `extract_external_urls` after existing attachment/embed entries, preserving Discord media priority.
2. **Mirror** the same helper in `StorageHandler.get_topic_editor_message_context` around `src/common/storage_handler.py:1350` so read-tool payloads use identical external indexes.
3. **Expose compact entries** shaped like `{"kind": "external", "index": N, "domain": "x.com", "url_present": true, "source": "content"|"embed"}` plus only sanitized/display URL fields that are safe for prompts/logs.
4. **Do not auto-shortlist solely on external links.** Reaction-qualified media auto-shortlisting remains based on archived Discord media behavior and continues creating `watching` topics, not direct posts.

### Step 6: Validate external refs without requiring extraction success (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Update** block-level media-ref validation around `src/features/summarising/topic_editor.py:1621` to use explicit branches for `attachment`, `embed`, and `external`.
2. **For external refs**, validate only that the source message exists and `extract_external_urls(ref_msg)[index]` exists and is source-domain safelisted.
3. **Reject malformed or out-of-range refs** with auditable transitions like existing invalid attachment/embed refs.
4. **Never reject a post** because yt-dlp metadata resolution, download, cache lookup, or file upload fails; those failures happen later as publish fallback metadata.

## Phase 4: Pure Rendering and Structured Publish Units

### Step 7: Keep rendering pure and make external fallback lookup deterministic (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Extend** `_resolve_media_url_from_metadata` at `src/features/summarising/topic_editor.py:3703` with explicit `attachment`, `embed`, and `external` branches. The external branch returns the original URL at `extract_external_urls(meta)[index]` for deterministic fallback only.
2. **Keep extraction/download out of** `_resolve_media_url_from_metadata` and `render_topic_publish_units`; they remain pure and deterministic.
3. **Update** `render_topic_publish_units` so media units carry `kind`, `url`, and `ref`, with external units using the original URL as fallback content.
4. **Update** `_summarize_source_media_counts` so existing attachment/embed counts remain unchanged. Either exclude external refs from legacy `resolvable_media` or report them in a separate `external_links` count; do not silently inflate existing media counts.

### Step 8: Replace flat string-only publishing with explicit send units (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. **Introduce** a local structured send-unit model in `_publish_topic` for block publishing, for example: text units (`send_kind: "text"`, `content`), URL fallback units (`send_kind: "url"`, `content`, `ref`), and file units (`send_kind: "file"`, `file_path`, `filename`, `fallback_url`, `ref`, `trace`).
2. **Build send units from publish units** while preserving the current suppressed-mode observable output: `flat_messages` remains the list of text chunks and fallback URL strings that would appear in Discord, `media_indices` remains indexes for media positions in that flattened view, `media_count` stays the count of media units, and `flat_message_count` remains comparable to the prior string-flattened count.
3. **Resolve external file units lazily** before sending only for publish units whose ref has `kind: "external"`. Attachment/embed media units continue as URL text sends unless existing behavior already uploads them.
4. **Send with explicit branches:** `channel.send(content)` for text and fallback URL units; `channel.send(file=discord.File(...))` for resolved external file units. If creating or sending the file fails, send the fallback URL text instead and continue.
5. **Preserve sent-id accounting and partial status:** append one Discord message ID per successful send unit, whether text, fallback URL, or file. Status is `sent` only when every send unit succeeds without error, `partial` when at least one unit sends, and `failed` only when no units send.
6. **Track per-unit publish traces** for external media: cache hit, downloaded, skipped, fallback reason, sanitized source URL, source domain, content type, bytes, and whether fallback text was sent.
7. **Leave simple-topic publishing unchanged** unless the topic has structured blocks.

## Phase 5: Tests and Validation

### Step 9: Add resolver unit tests (`tests/test_external_media_resolver.py`)
**Scope:** Medium
1. **Cover** Reddit/X/Twitter/Instagram/TikTok-like URL extraction, deterministic ordering across content and embeds, allowed/blocked source domains, URL sanitization with query tokens, cache-key stability, content-type classification, Discord compatibility, oversize detection, and metadata parsing.
2. **Cover** two-tier safety: safelisted original URL plus resolved CDN URL passes only with valid media type/size/timeout; non-safelisted original URL blocks before yt-dlp/download.
3. **Stub** subprocess and download functions. Tests must not run real yt-dlp or real network calls.

### Step 10: Extend TopicEditor core/runtime tests (`tests/test_topic_editor_core.py`, `tests/test_topic_editor_runtime.py`)
**Scope:** Large
1. **Core tests:** `normalize_media_ref` accepts `kind: "external"`; invalid kind still rejects; existing attachment/embed tests remain unchanged.
2. **Payload/index tests:** `_message_payload`, `StorageHandler.get_topic_editor_message_context`, validation, and `_resolve_media_url_from_metadata` all use the same `extract_external_urls` ordering for external index `N`.
3. **Validation tests:** valid external refs are accepted without resolver calls; out-of-range or blocked-domain refs reject with auditable transitions; no non-attachment branch falls through to embed behavior.
4. **Publishing tests:** cache hit avoids download, resolver success sends a fake `discord.File` in the correct block position, resolver failure falls back to original link, oversize/unsupported media falls back, file-send failure falls back, and text blocks still publish.
5. **Suppressed-mode tests:** suppressed structured publishing still returns `publish_units`, `flat_messages`, `media_indices`, source media counts, `media_count`, and `flat_message_count` in the expected shapes when external refs are present.
6. **Regression tests:** reaction-qualified Discord media auto-shortlisting still creates `watching` topics and does not direct-post at five reactions.

## Execution Order
1. Build and test `extract_external_urls` plus pure resolver helpers first so every later path shares the same external index contract.
2. Add the staged migration and DB/storage cache wrappers with fake storage tests.
3. Extend `normalize_media_ref`, prompt/tool schema text, and explicit media-kind branching.
4. Wire payload exposure and validation using the shared helper.
5. Update pure rendering and source media counts.
6. Replace the structured publisher flatten/send loop with explicit send units and fallback-aware file sends.
7. Finish with targeted tests, then broader TopicEditor/live-update regression tests.

## Validation Order
1. `pytest tests/test_external_media_resolver.py`
2. `pytest tests/test_topic_editor_core.py`
3. `pytest tests/test_topic_editor_runtime.py -k "media_ref or publish or auto_shortlist or get_message_context"`
4. `pytest tests/test_topic_editor_media_understanding.py`
5. `pytest tests/test_live_top_creations.py tests/test_live_runtime_wiring.py`
6. Optionally run the broader relevant summarising suite if the targeted tests expose shared fixtures or helper changes.

## Deployment Follow-Up
1. After implementation creates `.migrations_staging/20260514160000_external_media_cache.sql`, apply it through the project’s normal Supabase migration path before deploying code that depends on persistent cache reads/writes.
2. Configure `EXTERNAL_MEDIA_CACHE_DIR` in production if the default cache directory is not durable enough for the deployment.
3. Set the external media byte cap to a value compatible with the deployed Discord bot’s upload limit.
4. Run a manual production smoke test with a Reddit or X external media ref after code and schema are deployed.
