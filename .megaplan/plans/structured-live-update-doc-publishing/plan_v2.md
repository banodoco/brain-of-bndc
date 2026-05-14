# Implementation Plan: Structured TopicEditor Documents

## Overview
The critique does not point to the wrong root cause. The original target remains correct: `src/features/summarising/topic_editor.py` owns the TopicEditor tool schemas, create-topic dispatch, summary storage, rendering, and Discord publishing. The flags show missing implementation specificity in the same path: block-only `post_sectioned_topic` calls would currently be rejected, archive-resolved source messages would not feed author/collision checks, read tools do not expose enough media metadata for stable refs, and embed media refs need a deterministic schema.

The revised approach keeps the original design but tightens the contract. `topics.summary.blocks` becomes the canonical document shape for structured topics, legacy summaries are normalized at render/publish time, and source/media hydration uses one archive message metadata path shared by validation, author derivation, citation links, and publish-time media URL resolution.

Media refs will use an explicit canonical shape:

```json
{"message_id": "123", "kind": "attachment", "index": 0}
```

`kind` is `attachment` or `embed`. For compatibility with the existing vision/read-tool convention, `{message_id, attachment_index}` is accepted as shorthand and normalized to `{message_id, kind: "attachment", index: attachment_index}`. This preserves current attachment-index workflows while making embeds addressable.

## Phase 1: Document Shape And Prompt Contract

### Step 1: Define the canonical document and media-ref model (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Add pure helpers near `render_topic`: `normalize_topic_document(topic)`, `normalize_document_blocks(summary, topic_source_ids)`, `normalize_media_ref(ref)`, `block_source_ids(block)`, `block_media_refs(block)`, and `collect_document_source_ids(document)`.
2. Use `summary.blocks` as the canonical structured document shape. Each block supports `type`, `title`, `text`, `source_message_ids`, and `media_refs`.
3. Support block types `intro` and `section` for this change. Do not add separate media-only blocks unless tests reveal a needed compatibility case.
4. Define canonical `media_refs` as `{message_id: string, kind: "attachment"|"embed", index: integer}`. Accept and normalize legacy/shorthand `{message_id, attachment_index}` to `{kind: "attachment", index: attachment_index}`.
5. Normalize legacy sectioned summaries into blocks:
   - `summary.body` becomes an `intro` block.
   - `summary.sections[]` become ordered `section` blocks.
   - section-level `source_message_ids` and `media_refs` are preserved if present.
   - topic-level `source_message_ids` are used only as a compatibility fallback when a legacy block has no local sources.
6. Normalize legacy simple topics into a single `intro` block only for structured publishing helpers; keep old simple-topic rendering behavior unless `summary.blocks` is present.

### Step 2: Update tool schemas and agent instructions (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Extend `post_sectioned_topic` input schema to accept `blocks`, with block properties for `type`, `title`, `text`, `source_message_ids`, and canonical `media_refs`.
2. Keep `body`, `sections`, and topic-level `source_message_ids` accepted for backward compatibility with old model output and existing tests.
3. Explicitly relax the current `post_sectioned_topic` guard in `_dispatch_create_topic_tool` from `not args.get("sections")` to rejecting only when both `sections` and `blocks` are missing or normalize to zero publishable blocks.
4. Update `TOPIC_EDITOR_SYSTEM_PROMPT` and the tool description so the agent knows:
   - every factual intro/section block needs its own `source_message_ids`.
   - media belongs on the block it illustrates.
   - attachment media refs use either canonical `{message_id, kind: "attachment", index}` or shorthand `{message_id, attachment_index}`.
   - embed media refs use `{message_id, kind: "embed", index}`.
   - there is no global source footer.
5. Update `_summary_for_tool` so structured `post_sectioned_topic` calls store `{"blocks": [...]}` in `topics.summary`. Legacy calls can be normalized into `blocks` while optionally preserving `body`/`sections` fields for rollback readability.

## Phase 2: Source And Media Metadata

### Step 3: Add one archive message metadata resolver (`src/common/storage_handler.py`, `src/common/db_handler.py`)
**Scope:** Medium
1. Add `get_topic_editor_source_messages(message_ids, guild_id=None, environment="prod", limit=50)` or extend the existing `get_topic_editor_message_context` path to return full source metadata needed by TopicEditor internals.
2. The resolver must return rows ordered by requested ID with `message_id`, `guild_id`, `channel_id`, `author_id`, author display snapshot/name, `content`, `created_at`, `reference_id`, `thread_id`, `attachments`, and `embeds`.
3. Expose the resolver through `DBHandler`, matching existing synchronous wrapper style.
4. Use this same resolver for validation, source-author derivation, source link hydration, and media URL hydration so these behaviors cannot drift.

### Step 4: Enrich read-tool output for media ref discovery (`src/common/storage_handler.py`, `src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Update `storage_handler.get_topic_editor_message_context` to select and return `guild_id`, `attachments`, and `embeds` in addition to its current fields.
2. Add compact agent-facing media metadata to `get_message_context` results, for example `media_refs_available: [{kind, index, url_present, content_type, filename}]`, while keeping payloads bounded.
3. Include the same compact `media_refs_available` list in source-window `_message_payload` so current-run and archive messages use the same ref-addressing convention.
4. Reuse existing `_normalize_attachment_list` and embed URL extraction logic instead of introducing a separate media parser.

## Phase 3: Validation And Topic Creation

### Step 5: Validate blocks before storing (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. In `_dispatch_create_topic_tool`, normalize blocks before deriving `source_ids`, validating, collision checking, or storing the topic.
2. Derive topic-level `source_ids` as the distinct union of all block-level source IDs for structured sectioned topics. Fall back to legacy topic-level IDs only when normalized legacy blocks have no local sources.
3. Resolve all source IDs against the current source window plus the archive metadata resolver. Reject missing IDs with `rejected_post_sectioned` and a clear reason such as `unresolved_block_source_message`.
4. Validate every media ref after normalization:
   - `message_id` must resolve to a source message.
   - `kind` must be `attachment` or `embed`.
   - `index` must be an integer in range for that message’s attachment/embed list.
   - the referenced attachment/embed must expose a usable URL or proxy URL.
5. Reject invalid refs with `rejected_post_sectioned` and reason `unresolved_media_ref` or `invalid_media_ref` rather than silently dropping them.

### Step 6: Feed resolved sources into author and collision logic (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Replace the current source-author derivation that only uses `self._messages_by_id(context["messages"], source_ids)` with a merged resolved-source set from current window rows plus archive resolver rows.
2. Use the merged source rows for `source_authors`, `post_simple_topic` single-author/source-count validation, and `detect_topic_collisions` author-overlap checks.
3. Keep storing `topic_sources` from the final union source IDs, not from only current-window IDs.
4. Add a regression test where a block source comes from archive lookup outside `context["messages"]` and still contributes its author to collision detection.

## Phase 4: Deterministic Rendering And Publishing

### Step 7: Add structured publish units (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Keep `render_topic(topic)` pure for legacy simple tests, but add `render_topic_publish_units(topic, source_metadata=None)` for structured document topics.
2. Return ordered units like `{kind: "text", content: ...}` and `{kind: "media", url: ..., ref: ...}`.
3. Render structured topics in deterministic order: header, intro text with inline citations, intro media, each section text with inline citations, that section media.
4. Render citations inline at the relevant block only, deduped per block and ordered by first appearance: `[[1]](https://discord.com/channels/{guild_id}/{channel_id}/{message_id})`.
5. Do not append a global `Source` or `Sources` footer for structured document topics.
6. Preserve existing simple-topic rendering compatibility unless `summary.blocks` is present.

### Step 8: Make `_publish_topic` publish block-by-block (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Before rendering, hydrate all source/media metadata with the resolver from Step 3 when the topic has structured blocks or media refs.
2. When publishing is disabled, return the same deterministic would-publish units/messages in `publish_results` for trace/debug output.
3. When publishing is enabled, send one normal text block per Discord message and each media URL immediately after the block that owns it.
4. Add paragraph-aware chunking for oversized text blocks only: split on blank lines first, then single newlines, then hard-split only an individual paragraph that still exceeds the Discord limit.
5. Persist `discord_message_ids` in actual send order, including media URL messages.

## Phase 5: Tests

### Step 9: Add pure helper and rendering tests (`tests/test_topic_editor_core.py`, `tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. Canonical media refs validate both `{message_id, kind, index}` and shorthand `{message_id, attachment_index}`.
2. Embed refs validate and resolve deterministically with `{kind: "embed", index}`.
3. Legacy summaries normalize into ordered blocks without breaking old `render_topic` expectations.
4. Structured section-level sources render inline next to the correct intro/section and no global footer appears.
5. Source jump URLs hydrate from metadata and remain ordered/deduped per block.
6. Paragraph-aware chunking splits only oversized blocks and leaves normal sections as one message each.

### Step 10: Add runtime creation and publishing tests (`tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. A `post_sectioned_topic` call with `blocks` and no `sections` is accepted and stored, proving the old missing-`sections` guard was relaxed.
2. A structured topic stores `topics.summary.blocks`, stores topic-level union `source_message_ids`, and writes one `topic_sources` row per distinct block source ID.
3. Archive-resolved source rows feed `source_authors`, `post_simple_topic` validation, and collision detection.
4. `get_message_context` read-tool output includes compact `media_refs_available` for attachments and embeds.
5. Invalid source IDs and invalid media refs reject the create tool with auditable transition payloads.
6. Publishing enabled sends header/intro text, intro media, section text, and section media in order.
7. Publishing disabled returns the same deterministic would-publish sequence without sending.
8. Existing simple-topic behavior remains covered and compatible.

## Execution Order
1. Implement pure normalization/media-ref helpers first and cover them with focused tests.
2. Update tool schema, prompt text, and the `post_sectioned_topic` missing-`sections` guard before enforcing new validation.
3. Add the source metadata resolver and enrich `get_message_context` so both validation and the agent have the data they need.
4. Wire validation and merged source-author derivation into `_dispatch_create_topic_tool`.
5. Add structured publish units and publish-time hydration.
6. Update publisher behavior and final runtime tests once the render units are stable.

## Validation Order
1. Run the cheapest focused checks first: `pytest tests/test_topic_editor_core.py -q`.
2. Run TopicEditor runtime coverage: `pytest tests/test_topic_editor_runtime.py -q`.
3. Run publisher-adjacent existing tests: `pytest tests/test_live_update_editor_publishing.py tests/test_live_update_prompts.py -q`.
4. Run the broader relevant set before handoff: `pytest tests/test_topic_editor_*.py tests/test_live_update_editor_*.py -q`.
