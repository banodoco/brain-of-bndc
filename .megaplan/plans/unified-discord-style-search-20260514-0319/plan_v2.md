# Implementation Plan: Unified Discord-Style Message Search

## Overview
The topic editor currently defines a source-window-only `search_messages` read tool in `src/features/summarising/topic_editor.py` (lines 91–105), implemented by `_search_source_messages(...)` (lines 1034–1076) against `dispatcher_context["messages"]`. Archive-backed topic-editor read helpers already live in `src/common/storage_handler.py` (e.g., `get_topic_editor_message_context` at line 1247), with sync wrappers in `src/common/db_handler.py` (e.g., line 515).

The `discord_messages` table has columns: `message_id`, `guild_id`, `channel_id`, `author_id`, `content`, `created_at`, `attachments` (jsonb), `embeds` (jsonb), `reaction_count`, `reactors`, `reference_id`, `thread_id`, `is_deleted` — but NO dedicated `mentions` column. Replies use `reference_id` (mapped to `reply_to_message_id` in tool output).

**FLAG-001 resolution**: Since there is no `mentions` column, `mentions_author_id` filtering MUST be done by matching Discord mention tokens (`<@ID>` / `<@!ID>`) against the `content` column. For archive scope, use `.ilike('content', '%<@ID>%')` OR `.ilike('content', '%<@!ID>%')` (Supabase parameterized queries — safe against SQL injection). For window scope, regex-match mention tokens in the in-memory content string. This is the simplest correct fallback given the schema.

The implementation replaces only the topic-editor `search_messages` surface, adds `get_reply_chain`, and avoids the guarded live-update editor files (`live_update_editor.py`, `live_update_prompts.py`).

## Main Phase

### Step 1: Replace the `search_messages` Tool Schema and Add `get_reply_chain` (`topic_editor.py:91–105, 26–33`)
**Scope: Small**

1. **Replace** the existing `search_messages` dict in `TOPIC_EDITOR_TOOLS` (line 91) with the unified schema:
   - Properties: `query` (string), `from_author_id` (integer), `in_channel_id` (integer), `mentions_author_id` (integer), `has` (array of enum: `["image", "video", "audio", "link", "embed", "file"]`), `after` (string), `before` (string), `is_reply` (boolean), `limit` (integer, default 20, max 50), `scope` (string, enum: `["window", "archive"]`, default `"window"`).
   - `required`: `[]` (all params optional).
2. **Add** `get_reply_chain` dict after the existing `get_message_context` tool:
   - Properties: `message_id` (string, required), `max_depth` (integer, default 5, max 15).
   - `required`: `["message_id"]`.
3. **Update** `READ_TOOL_NAMES` (line 26) to include `"get_reply_chain"`.
4. **Remove** `search_messages` from the old `{"search_topics", "search_messages"}` set in `_format_tool_input_hint` (line 1813) so `search_messages` gets its own branch showing `query` plus optionally `scope`.

### Step 2: Add Time-Parser Helper and Unified Window Filter (`topic_editor.py:1034–1088`)
**Scope: Medium**

1. **Add** `_parse_time_bound(value: Optional[str], default: datetime) -> datetime` method near `_message_is_since` (line 1078):
   - If `value` is `None`, return `default`.
   - If `value` matches ISO timestamp, parse with `datetime.fromisoformat()`, ensure UTC.
   - If `value` matches relative pattern `r'^(\d+)\s*([hd])$'`, subtract `int(hours)` from `datetime.now(timezone.utc)`.
   - Otherwise raise `ValueError("Invalid time format: {value}")` — this will surface as `tool_error` in dispatch.
2. **Replace** `_search_source_messages` (line 1034) with a unified version `_search_window_messages(messages, *, query, from_author_id, in_channel_id, mentions_author_id, has, after, before, is_reply, limit)`:
   - **`query`**: case-insensitive substring match on `content`.
   - **`from_author_id`**: `str(message.get("author_id")) == str(from_author_id)`.
   - **`in_channel_id`**: `str(message.get("channel_id")) == str(in_channel_id)`.
   - **`mentions_author_id`**: match `<@ID>` or `<@!ID>` tokens in `content` using `re.search(rf'<@!?{mentions_author_id}>', str(content))`.
   - **`has`**: check `attachments` / `embeds` / content URL patterns:
     - `image`: any attachment with content_type starting `image/` or filename ending in `.png|.jpg|.jpeg|.gif|.webp`.
     - `video`: any attachment with content_type starting `video/` or filename ending in `.mp4|.mov|.webm|.mkv`.
     - `audio`: similar for audio content types / extensions.
     - `link`: content contains `http://` or `https://`.
     - `embed`: `embeds` jsonb array is non-empty (evaluated via `_as_json_array`).
     - `file`: any attachment exists (non-empty `attachments` array).
   - **`after` / `before`**: apply `_parse_time_bound`, then filter via `_message_is_since`-style datetime comparison.
   - **`is_reply`**: check `message.get("reply_to_message_id")` or `message.get("reference_id")` is/is-not None.
   - **`limit`**: clamp 1–50, default 20.
3. **Return** compact rows: `{message_id, channel_id, channel_name, author_id, author_name, content_preview, created_at, reply_to_message_id, has_attachments, has_links, has_image, has_video, has_audio, has_embed}`. `content_preview` capped at 200 chars via `_cap_text`.
4. **Keep** existing `_search_source_messages` → rename to `_search_window_messages`; old call sites updated.

### Step 3: Add Archive Search Storage Method (`storage_handler.py`, after line 1309)
**Scope: Medium**

1. **Implement** async `search_messages_unified(self, *, scope: str, guild_id: Optional[int], environment: str, query: Optional[str], from_author_id: Optional[int], in_channel_id: Optional[int], mentions_author_id: Optional[int], has: Optional[List[str]], after: Optional[str], before: Optional[str], is_reply: Optional[bool], limit: int = 20)` returning `Dict[str, Any]` with shape `{"messages": [...], "truncated": bool}`.
2. **Build** a Supabase query on `discord_messages` selecting: `message_id, channel_id, author_id, content, created_at, reference_id, attachments, embeds`.
3. **Apply filters** using Supabase query builder (NO raw SQL interpolation):
   - `.eq('is_deleted', False)` always.
   - `.eq('guild_id', guild_id)` if guild_id is not None.
   - `.ilike('content', f'%{query}%')` if query is not None.
   - `.eq('author_id', from_author_id)` if not None.
   - `.eq('channel_id', in_channel_id)` if not None.
   - **`mentions_author_id`**: `.or_(f"content.ilike.%<@{mentions_author_id}>%,content.ilike.%<@!{mentions_author_id}>%")` (parameterized via Supabase `.or_()` — this is safe: the integer ID is formatted into a fixed token pattern, not user-controlled free text).
   - **`after` / `before`**: parse with the same time-parser helper (duplicated or extracted to a shared utility); clamp effective range to 30 days (if difference exceeds 30 days, cap the earlier bound).
   - **`is_reply`**: `.not_.is_('reference_id', 'null')` if True else `.is_('reference_id', 'null')` if False.
   - `.limit(safe_limit)` (1–50, default 20).
   - `.order('created_at', desc=True)`.
4. **Handle `has` filters**:
   - Supabase's Python client has limited jsonb operators. Use a two-pass approach: fetch a candidate set with indexed filters (author, channel, time), then apply JSON/media-kind tests in Python. This is reliable and avoids depending on undocumented Supabase jsonb query support.
   - In Python post-filter: use `_as_json_array(row.get('attachments'))` and `_as_json_array(row.get('embeds'))` to check `image/video/audio/embed/file` presence; regex for `link` in `content`.
5. **Attach** channel context via `_attach_channel_context_and_filter_nsfw` and author names via `get_author_context_snapshots`, matching the existing `get_topic_editor_message_context` pattern (lines 1271–1305).
6. **Produce** compact rows identical in shape to the window helper.
7. **Apply** result capping: if serialized result would exceed ~2KB, truncate list and set `truncated: true`. `content_preview` already capped at 200 chars per message.

### Step 4: Add Reply Chain Storage Method (`storage_handler.py`, after new `search_messages_unified`)
**Scope: Small**

1. **Implement** async `get_reply_chain(self, message_id: str, guild_id: Optional[int] = None, environment: str = 'prod', max_depth: int = 5)` returning `List[Dict[str, Any]]`.
2. **Clamp** `max_depth` 1–15, default 5.
3. **Iteratively** query `discord_messages` for one row by `message_id`:
   ```python
   result = await asyncio.to_thread(
       self.supabase_client.table('discord_messages')
           .select('message_id,channel_id,author_id,content,created_at,reference_id')
           .eq('message_id', current_id)
           .eq('is_deleted', False)
           .maybe_single()
           .execute
   )
   ```
4. **Stop** on null result, null `reference_id`, max_depth reached, or cycle (`seen` set of message_ids).
5. **Collect** ancestor rows, then reverse for root-first order.
6. **Attach** channel names and author names via the same helpers used in step 3.
7. **Return** compact rows: `{message_id, channel_id, channel_name, author_id, author_name, content_preview, created_at}`.

### Step 5: Add DB Handler Wrappers (`db_handler.py`, after existing wrappers)
**Scope: Small**

1. **Add** `search_messages_unified(...)` sync wrapper (near line 530, after `get_topic_editor_message_context`): calls `self.storage_handler.search_messages_unified(...)` via `_run_async_in_thread`, returns `[]` if no storage handler.
2. **Add** `get_reply_chain(...)` sync wrapper: same pattern.
3. **Do NOT modify** the older `DBHandler.search_messages(...)` (line 121) — it serves a different (non-topic-editor) purpose.

### Step 6: Wire Dispatch (`topic_editor.py:811–819, 833–837`)
**Scope: Medium**

1. **Update** `_dispatch_read_tool` for `search_messages`:
   - Extract all new params from `args`.
   - Determine `scope` (default `"window"`).
   - `scope="window"`: call `self._search_window_messages(context.get("messages") or [], ...)`.
   - `scope="archive"`: call `self.db.search_messages_unified(guild_id=..., environment=self.environment, ...)`.
   - Unknown scope: raise `ValueError` → becomes `tool_error`.
2. **Add** `get_reply_chain` branch in `_dispatch_read_tool` (in the `elif` chain, before the `else: result = None` at line 837):
   - Extract `message_id` (required), `max_depth` (default 5, clamp 1–15).
   - Call `self.db.get_reply_chain(message_id=..., guild_id=..., environment=self.environment, max_depth=...)`.
3. **Update** `_format_tool_input_hint` (line 1810):
   - For `search_messages`: show `query` plus `scope` if not default.
   - For `get_reply_chain`: show `message_id` snippet.

### Step 7: Update System Prompt (`topic_editor.py:46–74`)
**Scope: Small**

1. **Update** the `TOPIC_EDITOR_SYSTEM_PROMPT` paragraph at line 49 to mention `get_reply_chain`.
2. **Add** a brief usage examples section (3–5 lines) after the existing read-tool sentence:
   ```
   Use `search_messages(query="Wan 2.5", from_author_id=X, has=["video"], scope="archive", after="7d")`
   to find a user's recent video posts about a tool. Use `search_messages(in_channel_id=X, after="24h", has=["image"])`
   to scan a channel for recent generations. When a source message has `reply_to_message_id` set, call
   `get_reply_chain(message_id="...")` to walk backwards and understand what it's responding to.
   ```
3. **Keep** existing descriptions for `search_topics`, `get_author_profile`, `get_message_context` unchanged.

### Step 8: Update and Expand Tests (`tests/test_topic_editor_runtime.py`)
**Scope: Medium**

1. **Extend** `FakeDB` (line 57) with:
   - `search_messages_unified_calls = []` list.
   - `search_messages_unified(...)` recording method.
   - `get_reply_chain_calls = []` list.
   - `get_reply_chain(...)` method returning `self.reply_chain_rows`.
2. **Update** the existing `search_messages` dispatch test (line 912): change `channel_id` → `in_channel_id`, `author_id` → `from_author_id`. Update expected result assertions to match new row shape (which now includes `reply_to_message_id`, `has_attachments`, `has_links`, etc.).
3. **Add** window-scope filter tests:
   - AND-combined: query + from_author + in_channel.
   - `has=["image"]`, `has=["video"]`, `has=["link"]`.
   - `mentions_author_id` matching `<@42>` / `<@!42>` in content.
   - `is_reply=True` / `is_reply=False`.
   - `after="24h"` / `before="7d"` relative time.
   - Malformed time → `tool_error`.
   - Limit clamp (0 → 1, 100 → 50).
4. **Add** archive-scope dispatch test: prove unified args flow to `FakeDB.search_messages_unified(...)` with correct `guild_id` and `environment`.
5. **Add** reply-chain dispatch test: prove `get_reply_chain` calls `FakeDB.get_reply_chain(...)` and returns root-first rows.
6. **Add** cycle-detection test: seed `FakeDB.reply_chain_rows` with `A → B → A`, verify the helper stops and returns `[A, B]` without infinite loop.
7. **Keep** `tests/test_live_update_prompts.py` untouched; run only as a guard.

## Execution Order
1. Replace tool schema and update `READ_TOOL_NAMES` + system prompt (Step 1 + Step 7) — low risk, defines the contract.
2. Add time-parser helper and rewrite window search (Step 2) — core filter logic, testable without Supabase.
3. Add archive storage method (Step 3) — depends on time-parser and compact row shape from Step 2.
4. Add reply-chain storage method (Step 4) — separate from search, can proceed in parallel after Step 3 patterns are established.
5. Add DB handler wrappers (Step 5) — simple passthrough, depends on Steps 3–4.
6. Wire dispatch (Step 6) — connects everything, depends on Steps 2–5.
7. Update tests (Step 8) — depends on final method names and shapes.

## Validation Order
1. `pytest tests/test_topic_editor_runtime.py -x -v` — focused topic-editor tests first.
2. `pytest tests/test_topic_editor_media_understanding.py tests/test_topic_editor_core.py -x` — adjacent tests.
3. `pytest tests/test_live_update_prompts.py -x` — guard against accidental import/schema breakage.
4. Full suite: `pytest tests/test_topic_editor_runtime.py tests/test_topic_editor_media_understanding.py tests/test_topic_editor_core.py tests/test_live_update_prompts.py`.
