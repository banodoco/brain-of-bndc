# Implementation Plan: Topic Editor Full-Archive Read Tools

## Overview
The topic editor read tools currently split between DB-backed topic/profile/context reads and an in-memory source-window `search_messages`. The requested change adds three DB-backed archive reads while preserving the existing four read tools and keeping `live_update_editor.py` / `live_update_prompts.py` untouched.

Concrete touch points are:
- `src/features/summarising/topic_editor.py`: `READ_TOOL_NAMES`, `TOPIC_EDITOR_SYSTEM_PROMPT`, `TOPIC_EDITOR_TOOLS`, `_dispatch_read_tool`, and optionally `_format_tool_input_hint` for trace readability.
- `src/common/storage_handler.py`: adjacent to `search_topic_editor_topics`, `get_topic_editor_author_profile`, and `get_topic_editor_message_context`.
- `src/common/db_handler.py`: sync wrappers near existing topic-editor wrappers.
- `tests/test_topic_editor_runtime.py`: `FakeDB` plus focused dispatch/tool-result tests for the three new reads.

The main implementation wrinkle is schema naming: this codebase stores Discord reply parents as `reference_id` and exposes them to editor tools as `reply_to_message_id`. I would keep that storage mapping rather than inventing a new DB column unless the database already has `reply_to_message_id`.

## Main Phase

### Step 1: Add Tool Definitions And Prompt Guidance (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Extend** `READ_TOOL_NAMES` near `src/features/summarising/topic_editor.py:25` with `search_archive_messages`, `get_reply_chain`, and `get_author_recent_messages`.
2. **Append** three tool definitions to `TOPIC_EDITOR_TOOLS` after the existing `get_message_context` read tool at `src/features/summarising/topic_editor.py:116`, leaving the existing four read tool definitions unchanged.
3. **Update** `TOPIC_EDITOR_SYSTEM_PROMPT` near `src/features/summarising/topic_editor.py:46` with a short paragraph explaining:
   - use `search_archive_messages` when in-window search lacks enough context,
   - use `get_reply_chain` when a source message has `reply_to_message_id` / reply context and the parent matters,
   - use `get_author_recent_messages` when sharing a generation to ground what the author has been iterating on.
4. **Optionally update** `_format_tool_input_hint` around `src/features/summarising/topic_editor.py:1813` so traces show useful hints for the new read calls. This is not behavior-critical.

### Step 2: Implement Archive Storage Reads (`src/common/storage_handler.py`)
**Scope:** Medium
1. **Add** `search_archive_messages(query, *, guild_id, environment, channel_id, author_id, hours_back, limit)` near existing topic-editor storage reads at `src/common/storage_handler.py:1097`.
2. **Query** `discord_messages` directly with:
   - `select('message_id,channel_id,author_id,content,created_at,reference_id')`,
   - `.eq('is_deleted', False)`,
   - optional `.eq('guild_id', guild_id)`, `.eq('channel_id', channel_id)`, `.eq('author_id', author_id)`,
   - `.gte('created_at', since.isoformat())`,
   - `.ilike('content', f'%{safe_query}%')`,
   - `.order('created_at', desc=True).limit(safe_limit)`.
3. **Cap** `hours_back` to `1..720` and `limit` to `1..50`; trim query text to a reasonable length consistent with `search_topic_editor_topics`.
4. **Attach** channel names using `_attach_channel_context_and_filter_nsfw`, then attach author display names via `get_author_context_snapshots`, following the pattern in `get_topic_editor_message_context` at `src/common/storage_handler.py:1247`.
5. **Return** compact rows shaped as `{message_id, channel_id, channel_name, author_id, author_name, content_preview, created_at, reply_to_message_id}` with `reply_to_message_id` populated from `reference_id`.
6. **Add** `get_author_recent_messages(author_id, *, guild_id, environment, hours_back, limit, channel_id)` using the same compaction helper and ordering by `created_at DESC`.
7. **Add** `get_reply_chain(message_id, *, guild_id, environment, max_depth)` using an iterative loop:
   - start with the supplied message id,
   - fetch one row by `message_id` and optional `guild_id`,
   - read parent from `reference_id`,
   - track visited ids to avoid cycles,
   - stop at no parent, missing parent, revisit, or capped depth `15`,
   - return only ancestor messages ordered root-first as `{message_id, author_id, author_name, channel_id, content_preview, created_at}`.
8. **Factor** a tiny private compaction helper only if it removes duplicated author/channel formatting between the two list-returning methods and reply-chain rows; keep it local and simple.

### Step 3: Add DB Handler Wrappers (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** sync wrappers near `src/common/db_handler.py:475`:
   - `search_archive_messages(...) -> List[Dict[str, Any]]`,
   - `get_reply_chain(...) -> List[Dict[str, Any]]`,
   - `get_author_recent_messages(...) -> List[Dict[str, Any]]`.
2. **Call** the corresponding `StorageHandler` async methods through `_run_async_in_thread`, matching existing wrappers like `get_topic_editor_message_context` at `src/common/db_handler.py:515`.
3. **Return** empty lists if `storage_handler` is missing, matching the current read-wrapper style.

### Step 4: Dispatch The New Read Tools (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Add** cases in `_dispatch_read_tool` around `src/features/summarising/topic_editor.py:798`:
   - `search_archive_messages`: pass query, guild_id, environment, optional channel/author, `hours_back=int(args.get('hours_back') or 168)`, `limit=int(args.get('limit') or 20)`.
   - `get_reply_chain`: pass message id, guild_id, environment, `max_depth=int(args.get('max_depth') or 5)`.
   - `get_author_recent_messages`: pass author id, guild_id, environment, `hours_back=int(args.get('hours_back') or 168)`, `limit=int(args.get('limit') or 20)`, optional channel id.
2. **Rely** on the existing `_tool_result_content` read-tool JSON cap at `src/features/summarising/topic_editor.py:689`; once the new names are in `READ_TOOL_NAMES`, results are JSON-encoded and truncated at roughly 2KB automatically.
3. **Do not alter** `search_topics`, `search_messages`, `get_author_profile`, or `get_message_context` behavior.

### Step 5: Extend Runtime Tests (`tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. **Extend** `FakeDB` near `tests/test_topic_editor_runtime.py:57` with fixture fields and methods for:
   - `search_archive_messages`,
   - `get_reply_chain`,
   - `get_author_recent_messages`.
2. **Add** a focused test that dispatches all three tools through `TopicEditor._dispatch_tool_call`, verifies outcome `read`, checks returned shapes, and asserts `guild_id` / `environment` / default caps are passed through.
3. **Add** or extend tool-result assertions to confirm the returned content is JSON and does not exceed the existing truncation budget for oversized archive results.
4. **Keep** the existing read-tool test intact so it continues proving `search_messages` remains source-window scoped.

### Step 6: Validate Cheaply, Then Broadly
**Scope:** Small
1. **Run** the targeted runtime test file first:
   ```bash
   pytest tests/test_topic_editor_runtime.py
   ```
2. **Run** a broader relevant suite if available/cheap, for example summarising tests:
   ```bash
   pytest tests/test_topic_editor_runtime.py tests/test_live_update_editor.py
   ```
   If `test_live_update_editor.py` is absent or slow, fall back to the repo’s established targeted test command from project docs/config.
3. **Run** formatting/lint only if the repo already has a clear command in config; otherwise skip rather than introducing new tooling.

## Execution Order
1. Add tests/FakeDB expectations first or in lockstep with dispatch so the API contract is concrete.
2. Add storage methods before DB wrappers.
3. Add DB wrappers before topic-editor dispatcher cases.
4. Update prompt/tool schemas after the backing runtime path exists.
5. Validate with `tests/test_topic_editor_runtime.py` before any broader test run.

## Guardrails
- Do not modify the existing four read tools’ schemas or implementations.
- Do not touch `src/features/summarising/live_update_editor.py` or `src/features/summarising/live_update_prompts.py`.
- Do not change the agent loop, finalize behavior, idempotency, lease handling, force-close behavior, collision handling, or vision tools.
- Keep DB reads compact and bounded: storage methods enforce the hard caps even if dispatch passes larger values.
