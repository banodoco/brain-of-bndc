# Implementation Plan: Unified Discord-Style Message Search

## Overview
The topic editor currently defines a source-window-only `search_messages` read tool in `src/features/summarising/topic_editor.py`, implemented by `_search_source_messages(...)` against `dispatcher_context["messages"]`. Archive-backed topic-editor read helpers already live in `src/common/storage_handler.py`, with sync wrappers in `src/common/db_handler.py`. The implementation should replace only the topic-editor `search_messages` surface, add `get_reply_chain`, and avoid the guarded live-update editor files.

The simplest shape is to keep window search in `TopicEditor` because it already has the in-memory source window, and add archive/reply-chain methods to storage/db for Supabase access. Tool results can rely on the existing `_tool_result_content` 2KB cap, with search methods returning compact rows and optionally a `truncated` marker when row trimming is needed.

## Main Phase

### Step 1: Update the Topic Editor Tool Contract (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Replace** the existing `search_messages` definition in `TOPIC_EDITOR_TOOLS` with the unified schema: optional `query`, `from_author_id`, `in_channel_id`, `mentions_author_id`, `has`, `after`, `before`, `is_reply`, `limit`, and `scope`.
2. **Add** a new `get_reply_chain` read tool next to the other read tools, with `message_id` required and `max_depth` optional.
3. **Update** `READ_TOOL_NAMES` to include `get_reply_chain`.
4. **Update** `TOPIC_EDITOR_SYSTEM_PROMPT` to mention the unified search examples and `get_reply_chain(...)`, while leaving existing descriptions for `search_topics`, `get_author_profile`, and `get_message_context` functionally intact.
5. **Extend** `_format_tool_input_hint(...)` so `get_reply_chain` traces show the requested message id, and unified `search_messages` hints can still show `query` plus optionally `scope`.

### Step 2: Add Shared Filter Parsing and Window Search (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Replace** `_search_source_messages(...)` with a unified window helper that accepts the new parameter names and filters `context["messages"]` by content, author, channel, mentions, attachment/embed/link/file kinds, time bounds, and reply status.
2. **Add** a time-bound parser helper near the existing `_message_is_since(...)` logic. It should accept ISO timestamps and relative `Nh`/`Nd` strings, normalize to timezone-aware UTC datetimes, and raise `ValueError` for malformed input so `_dispatch_read_tool(...)` returns `outcome: "tool_error"`.
3. **Clamp** `limit` to `1..50`, defaulting to `20`.
4. **Return** compact message rows with at least `message_id`, `channel_id`, `channel_name`, `author_id`, `author_name`, `content_preview`, `created_at`, `reply_to_message_id`, `has_attachments`, `has_links`, and media-kind booleans where practical.
5. **Preserve** the default behavior by making `scope="window"` when omitted.

### Step 3: Add Archive Search Storage Method (`src/common/storage_handler.py`)
**Scope:** Medium
1. **Implement** async `search_messages_unified(...)` on `StorageHandler` with the requested signature and compact return shape.
2. **Build** a Supabase query on `discord_messages` with parameterized/query-builder filters rather than interpolated SQL. Use `.ilike("content", f"%{query}%")`, `.eq(...)`, `.gte(...)`, `.lte(...)`, `.not_.is_(...)` or the locally supported equivalent for reply filtering.
3. **Use** actual stored column names from the repo: replies appear to be stored as `reference_id`, while existing topic-editor context maps that to `reply_to_message_id` in API output. The archive method should select `reference_id` and output it as `reply_to_message_id`.
4. **Handle** `has` filters against `attachments`, `embeds`, and content links. Prefer Supabase JSON operators where supported by the client; if the client API does not expose a reliable JSON non-empty operator, fetch a bounded candidate set after indexed filters and apply the JSON/media-kind tests in Python before final limiting.
5. **Attach** channel context using existing `_attach_channel_context_and_filter_nsfw(...)` where possible and author display names via `get_author_context_snapshots(...)`, matching the existing topic-editor helper style.
6. **Apply** a 30-day maximum range: reject or clamp broad absolute/relative ranges before querying. The brief says “clamp absolute max range to 30 days”; implement as a clear 30-day window cap rather than silently scanning all history.

### Step 4: Add Reply Chain Storage Method (`src/common/storage_handler.py`)
**Scope:** Small
1. **Implement** async `get_reply_chain(message_id, guild_id=None, environment="prod", max_depth=5)`.
2. **Clamp** `max_depth` to `1..15`, defaulting to `5`.
3. **Iteratively** select one `discord_messages` row by `message_id`, append a compact row, then follow `reference_id` until null, max depth, missing row, or cycle.
4. **Detect** cycles with a `seen` set and stop on revisit without throwing.
5. **Return** ancestor messages root-first by reversing the collected list.

### Step 5: Add DB Handler Wrappers (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** sync `search_messages_unified(...)` wrapper that calls `storage_handler.search_messages_unified(...)` via `_run_async_in_thread(...)` and returns `[]` if no storage handler is configured.
2. **Add** sync `get_reply_chain(...)` wrapper with the same thread bridge and fallback behavior.
3. **Do not** change the older generic `DBHandler.search_messages(...)` unless tests reveal a direct conflict; the topic editor should use the new wrapper names.

### Step 6: Wire Dispatch (`src/features/summarising/topic_editor.py`)
**Scope:** Medium
1. **Update** `_dispatch_read_tool(...)` for `search_messages`:
   - `scope="window"`: call the new window helper with `context.get("messages") or []`.
   - `scope="archive"`: call `self.db.search_messages_unified(...)` with `guild_id=context.get("guild_id")`, `environment=self.environment`, and all filter args.
   - unknown scope: raise `ValueError` for a `tool_error` result.
2. **Add** a `get_reply_chain` branch that calls `self.db.get_reply_chain(...)` with `guild_id`, `environment`, `message_id`, and clamped `max_depth`.
3. **Keep** `_tool_result_content(...)` as the final JSON cap. If the storage/window helper returns a dict such as `{"messages": [...], "truncated": true}`, it will still be encoded by the existing path.

### Step 7: Update and Expand Tests (`tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. **Extend** `FakeDB` with `search_messages_unified(...)` and `get_reply_chain(...)`, recording calls so dispatch wiring is observable.
2. **Update** the existing read-tool dispatch test to use the new param names: `from_author_id` and `in_channel_id` instead of `author_id` and `channel_id`.
3. **Add** focused window-scope tests for AND-combined filters: query, author, channel, reply status, `has=["image"]`, `has=["video"]`, `has=["link"]`, `after`, `before`, limit clamp, and malformed time producing `tool_error`.
4. **Add** archive-scope dispatch test proving the unified args are passed to `DBHandler.search_messages_unified(...)` with `guild_id` and `environment`.
5. **Add** reply-chain dispatch test proving `get_reply_chain` calls the DB wrapper and returns root-first rows.
6. **Add** storage-level tests where practical using a fake Supabase query object if existing test utilities make that cheap; otherwise keep storage edge cases covered by isolated helper tests and dispatch tests.
7. **Add** a cycle-detection test for reply-chain walking, ideally at the storage method level with a fake `discord_messages` table returning `A -> B -> A`.
8. **Keep** `tests/test_live_update_prompts.py` untouched unless only import/schema fallout requires updates, because the hard scope guard forbids changing `live_update_prompts.py` and the requested behavior is topic-editor-specific.

## Execution Order
1. Change the tool schema and dispatch first, preserving `scope="window"` default so current source-window behavior has a direct replacement.
2. Add the window helper and time parser before archive search so most filter semantics can be tested without Supabase fakes.
3. Add storage methods and DB wrappers after the local semantics are settled.
4. Add reply-chain wiring after archive method patterns are in place.
5. Update tests last where they depend on final method names and compact row shape.

## Validation Order
1. Run the focused topic-editor runtime tests first: `pytest tests/test_topic_editor_runtime.py`.
2. Run adjacent media-understanding/topic-editor tests: `pytest tests/test_topic_editor_media_understanding.py tests/test_topic_editor_core.py`.
3. Run the existing live prompt tests only as a guard against accidental schema/import breakage: `pytest tests/test_live_update_prompts.py`.
4. Finish with the broader relevant suite if time allows: `pytest tests/test_topic_editor_runtime.py tests/test_topic_editor_media_understanding.py tests/test_topic_editor_core.py tests/test_live_update_prompts.py`.

## Notes on Simplicity
Do not introduce a parser for raw Discord query strings like `from:... after:...`; the requested API is structured filter params modeled on Discord filters. Do not alter the agent loop, finalize flow, vision tools, live-update editor, or live-update prompts.
