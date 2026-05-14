# Implementation Plan: Structured TopicEditor Documents

## Overview
The TopicEditor path is concentrated in `src/features/summarising/topic_editor.py`: tool schemas and system prompt are defined near the top, create-topic dispatch stores `topics.summary`, `_publish_topic` calls pure `render_topic(topic)`, and `render_topic` currently emits one trimmed Discord message with a `Source(s): raw ids` footer. Storage wrappers in `src/common/db_handler.py` and `src/common/storage_handler.py` already expose compact source-message lookup, but they do not yet return enough metadata for jump URL and media hydration. Focused coverage belongs in `tests/test_topic_editor_core.py` and `tests/test_topic_editor_runtime.py`.

The simplest durable fix is to make `topics.summary` the canonical document payload while preserving legacy summary shapes. Add a normalization layer that accepts both old `{body, sections, source_message_ids}` and new `{blocks: [...]}` shapes, then use one publishing/rendering path that hydrates source links and media from archived Discord message rows at publish time.

## Phase 1: Document Shape And Prompt Contract

### Step 1: Define the topic document model (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Add small pure helpers near `render_topic`: `normalize_topic_document(topic)`, `block_source_ids(block)`, `block_media_refs(block)`, and `collect_document_source_ids(document)`.
2. Support these block types initially: `intro`, `section`, and optionally `media` only if a separate media-only block is needed later. Prefer text blocks with attached `media_refs` for this request.
3. Normalize legacy sectioned summaries into ordered blocks:
   - header remains topic metadata, not a block.
   - legacy `summary.body` becomes an `intro` block using topic-level `source_message_ids` only as a compatibility fallback.
   - legacy `summary.sections[]` become `section` blocks, using section-level `source_message_ids` if present, else topic-level IDs as fallback.
   - legacy simple topics become a single `intro` block with existing `summary.media` converted only for rendering compatibility.
4. Keep block fields intentionally minimal: `type`, `title`, `text`, `source_message_ids`, `media_refs`.

### Step 2: Update tool schemas and agent instructions (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Extend `post_sectioned_topic` input schema to accept `blocks` and section-level `source_message_ids` / `media_refs`; keep `body`, `sections`, and topic-level `source_message_ids` accepted for old callers/tests.
2. Change the tool description and `TOPIC_EDITOR_SYSTEM_PROMPT` to require every factual intro/section block to carry its own source IDs, media to be attached only to the relevant block, media refs to use `{message_id, attachment_index}` instead of CDN URLs, and no global source footer.
3. Update `_summary_for_tool` so new `post_sectioned_topic` calls store `{"blocks": [...]}` in `topics.summary`, while legacy args still store a normalized equivalent or a backward-compatible `{body, sections}` shape plus derived blocks.
4. Preserve `source_message_ids` on the topic row as the stable union of all block-level source IDs for audit/search and existing duplicate/collision behavior.

## Phase 2: Validation And Hydration Data

### Step 3: Validate block sources and media refs before storing (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. In `_dispatch_create_topic_tool`, derive `source_ids` from block-level IDs for `post_sectioned_topic`, falling back to legacy topic-level IDs only when blocks/sections do not provide them.
2. Add validation that every block `source_message_ids` item exists in the current source window or can be resolved by the existing message-context DB method if the agent found it via archive search.
3. Add validation that every `media_ref` has a source `message_id`, integer `attachment_index`, and resolves to an attachment/embed on that message.
4. Reject invalid topic creates with existing transition machinery (`rejected_post_sectioned`) and a precise reason like `unresolved_block_source_message` or `unresolved_media_ref`, instead of silently dropping refs.

### Step 4: Add source/media metadata lookup for publishing (`src/common/storage_handler.py`, `src/common/db_handler.py`)
**Scope:** Small
1. Add or extend a compact lookup method that returns archived rows by message IDs with `message_id`, `guild_id`, `channel_id`, `thread_id`, `created_at`, `attachments`, and `embeds`.
2. Expose it through `DBHandler`, mirroring the existing `get_topic_editor_message_context` wrapper style.
3. Use this lookup from TopicEditor publishing only when a topic has source IDs or media refs to hydrate.

## Phase 3: Deterministic Publishing

### Step 5: Replace single-message rendering with document publish units (`src/features/summarising/topic_editor.py`)
**Scope:** Large
1. Keep `render_topic(topic)` pure for legacy tests, but add a richer `render_topic_publish_units(topic, source_metadata=None)` helper that returns ordered units such as `{kind: "text", content: ...}` and `{kind: "media", url: ...}`.
2. Render order deterministically: header, intro text with inline linked citations, intro media, each section text with inline linked citations, that section media.
3. Inline citations should render at the relevant block as bracket links, for example `[[1]](https://discord.com/channels/{guild_id}/{channel_id}/{message_id})`, deduped per block and ordered by first appearance.
4. Remove the global `Source(s): ...` footer for structured document topics.
5. Preserve old simple-topic behavior as much as reasonable: simple summaries can still render as one text message with media URLs and existing source suffix unless the summary has document blocks.

### Step 6: Make `_publish_topic` send block-by-block with paragraph-aware chunking (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. Hydrate metadata before rendering when publishing is enabled; when suppressed, still return the would-publish text/media sequence for trace visibility.
2. Send one normal block per Discord message. Only chunk when that block exceeds the Discord limit.
3. Implement paragraph-aware chunking: split on blank lines first, then single newlines, and only hard-split a paragraph if it alone exceeds the limit.
4. Send media refs immediately after the relevant text block. If media is represented as URLs, send as URL messages; if later direct file upload is desired, keep that out of this change unless existing Discord utilities already support it.
5. Persist sent Discord IDs in actual send order, including media messages.

## Phase 4: Tests

### Step 7: Add focused pure rendering tests (`tests/test_topic_editor_core.py` or `tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. Structured section-level sources render inline next to the correct intro/section, not as a footer.
2. Discord jump links hydrate from message metadata.
3. Media refs resolve by `{message_id, attachment_index}` and render after the block that owns them.
4. Unknown source IDs or media refs cause create-topic rejection and a transition payload with the error reason.
5. Paragraph-aware chunking only splits an oversized block and leaves normal sections as one message each.
6. Existing simple-topic rendering behavior remains covered and intentionally unchanged except where compatibility assumptions are revised.

### Step 8: Add runtime/publisher tests (`tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. A `post_sectioned_topic` tool call with blocks stores `topics.summary.blocks`, stores topic-level union `source_message_ids`, and writes one `topic_sources` row per distinct block source ID.
2. Publishing enabled sends messages in the required order: header/intro text, intro media, section text, section media.
3. Publishing disabled returns the same deterministic would-publish units in `publish_results` without sending.
4. Existing run lifecycle tests still pass after updating the expected tool count/schema details if needed.

## Execution Order
1. Add pure normalization and collection helpers first; these are cheap to test and reduce risk in dispatch/publishing changes.
2. Update schema/prompt and `_summary_for_tool` next so new data can be stored without changing publishing yet.
3. Add validation before publisher hydration, because invalid refs should never reach the send path silently.
4. Add metadata lookup wrappers after validation identifies the exact fields required.
5. Replace publishing/rendering last, once fixtures can provide source metadata.
6. Update tests alongside each layer, starting with pure helpers before runtime tests.

## Validation Order
1. Run focused tests first: `pytest tests/test_topic_editor_core.py tests/test_topic_editor_runtime.py -q`.
2. Run publisher-adjacent checks: `pytest tests/test_live_update_editor_publishing.py tests/test_live_update_prompts.py -q` to catch accidental shared prompt/render regressions.
3. Run the broader relevant suite if time allows: `pytest tests/test_topic_editor_*.py tests/test_live_update_editor_*.py -q`.
4. Finish with lint/static checks only if the repo has an established command; otherwise rely on targeted pytest and import-time failures.
