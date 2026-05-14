# Implementation Plan: Topic Editor Full-Archive Read Tools

## Overview
The topic editor read tools currently split between DB-backed topic/profile/context reads and an in-memory source-window `search_messages`. The requested change adds three DB-backed archive reads while preserving the existing four read tools and keeping `live_update_editor.py` / `live_update_prompts.py` untouched.

Concrete touch points are:
- `src/features/summarising/topic_editor.py`: `READ_TOOL_NAMES`, `TOPIC_EDITOR_SYSTEM_PROMPT`, `TOPIC_EDITOR_TOOLS`, `_dispatch_read_tool`, and `_format_tool_input_hint` for trace readability.
- `src/common/storage_handler.py`: adjacent to `search_topic_editor_topics`, `get_topic_editor_author_profile`, and `get_topic_editor_message_context`.
- `src/common/db_handler.py`: sync wrappers near existing topic-editor wrappers at lines 475–532.
- `tests/test_topic_editor_runtime.py`: `FakeDB` plus focused dispatch/tool-result tests for the three new reads. **Must update the hardcoded `assert len(llm_call["tools"]) == 13` on line 273 to `16`.**

The main implementation wrinkle is schema naming: this codebase stores Discord reply parents as `reference_id` and exposes them to editor tools as `reply_to_message_id`. We keep that storage mapping rather than inventing a new DB column.

## Main Phase

### Step 1: Add Tool Definitions And Prompt Guidance (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Extend** `READ_TOOL_NAMES` near line 26 with `search_archive_messages`, `get_reply_chain`, and `get_author_recent_messages`.
2. **Append** three tool definitions to `TOPIC_EDITOR_TOOLS` after the existing `finalize_run` definition (which ends at line 269), leaving the existing 13 tool definitions unchanged. Insert the new tools before the closing `]`.
3. **Update** `TOPIC_EDITOR_SYSTEM_PROMPT` near line 46 with a short paragraph after the media-understanding instructions and before the closing triple-quote (around line 73–74):
   - `search_archive_messages` — use when in-window `search_messages` lacks enough context; queries full archive.
   - `get_reply_chain` — use when a source message has `reply_to_message_id` set and the parent conversation matters.
   - `get_author_recent_messages` — use when sharing a generation to ground what the author has been iterating on.
4. **Update** `_format_tool_input_hint` around line 1810 to add hint branches for the three new tools (e.g., show query snippet for `search_archive_messages`, message_id for `get_reply_chain`, author_id for `get_author_recent_messages`).

### Step 2: Implement Archive Storage Reads (`src/common/storage_handler.py`)
**Scope:** Medium
1. **Add** `search_archive_messages(query, *, guild_id, environment, channel_id, author_id, hours_back, limit)` after `get_topic_editor_message_context` (which ends near line 1309).
2. **Query** `discord_messages` directly:
   - `select('message_id,channel_id,author_id,content,created_at,reference_id')`
   - `.eq('is_deleted', False)`
   - optional `.eq('guild_id', guild_id)`, `.eq('channel_id', channel_id)`, `.eq('author_id', author_id)`
   - `.gte('created_at', since.isoformat())` where `since = datetime.utcnow() - timedelta(hours=safe_hours_back)`
   - `.ilike('content', f'%{safe_query}%')`
   - `.order('created_at', desc=True).limit(safe_limit)`
3. **Cap** `hours_back` to `1..720` and `limit` to `1..50`. Trim query text to 200 chars (consistent with existing trimming in `get_topic_editor_author_profile`).
4. **Attach** channel names via `_attach_channel_context_and_filter_nsfw`, then author names via `get_author_context_snapshots`, following the exact pattern in `get_topic_editor_message_context` at lines 1271–1305.
5. **Return** compact rows shaped as `{message_id, channel_id, channel_name, author_id, author_name, content_preview, created_at, reply_to_message_id}` with `reply_to_message_id` populated from `reference_id` and `content_preview` trimmed to 200 chars. Wrap in try/except returning `[]` on error (matching existing patterns).
6. **Add** `get_author_recent_messages(author_id, *, guild_id, environment, hours_back, limit, channel_id)` using the same compaction and ordering by `created_at DESC` (no ILIKE).
7. **Add** `get_reply_chain(message_id, *, guild_id, environment, max_depth)` using an iterative loop:
   - Fetch one row by `message_id` (with optional `guild_id` filter, `is_deleted=False`).
   - Read parent from `reference_id`.
   - Track visited IDs in a set; abort if revisit (cycle detection).
   - Stop at no parent, missing parent, or capped depth `max_depth` (default 5, hard cap 15).
   - Attach channel names and author names per-iteration (each step one query — fine at depth ≤ 15).
   - Return only ancestor messages (not the starting message) ordered root-first as `{message_id, author_id, author_name, channel_id, content_preview, created_at}`.
8. **Factor** a private `_compact_archive_message_row` helper only if it removes duplicated author/channel formatting across the two list-returning methods; keep it local and simple. If factoring creates more indirection than it saves (3 call sites, tiny logic), inline the compaction.

### Step 3: Add DB Handler Wrappers (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** sync wrappers after `get_topic_editor_message_context` (which ends near line 532):
   - `search_archive_messages(query, *, guild_id, environment, channel_id, author_id, hours_back, limit) -> List[Dict[str, Any]]`
   - `get_reply_chain(message_id, *, guild_id, environment, max_depth) -> List[Dict[str, Any]]`
   - `get_author_recent_messages(author_id, *, guild_id, environment, hours_back, limit, channel_id) -> List[Dict[str, Any]]`
2. **Call** the corresponding `StorageHandler` async methods through `_run_async_in_thread`, matching existing wrappers (e.g., `get_topic_editor_message_context` at line 515–532).
3. **Return** empty lists if `storage_handler` is missing, matching the current read-wrapper style.

### Step 4: Dispatch The New Read Tools (`src/features/summarising/topic_editor.py`)
**Scope:** Small
1. **Add** cases in `_dispatch_read_tool` at line 798, after the `understand_video` branch (line 836) and before the `else: result = None` fallback (line 837):
   - `search_archive_messages`: pass query, guild_id, environment, optional channel/author, `hours_back=int(args.get('hours_back') or 168)`, `limit=int(args.get('limit') or 20)`.
   - `get_reply_chain`: pass message id, guild_id, environment, `max_depth=int(args.get('max_depth') or 5)`.
   - `get_author_recent_messages`: pass author id, guild_id, environment, `hours_back=int(args.get('hours_back') or 168)`, `limit=int(args.get('limit') or 20)`, optional channel id.
2. **Rely** on the existing `_tool_result_content` read-tool JSON cap at line 701–706; once the new names are in `READ_TOOL_NAMES`, results are JSON-encoded and truncated at ~2KB automatically.
3. **Do not alter** `search_topics`, `search_messages`, `get_author_profile`, or `get_message_context` behavior.

### Step 5: Extend Runtime Tests (`tests/test_topic_editor_runtime.py`)
**Scope:** Medium
1. **Update** the hardcoded assertion at line 273: change `assert len(llm_call["tools"]) == 13` to `assert len(llm_call["tools"]) == 16` (or remove the redundant length assertion since line 272 already asserts `llm_call["tools"] == TOPIC_EDITOR_TOOLS`). This is required for existing tests to pass after adding 3 new tools.
2. **Extend** `FakeDB` near line 57 with fixture fields and methods:
   - `search_archive_messages(query, *, guild_id, environment, channel_id, author_id, hours_back, limit)` — record call in `self.read_calls`, return stubbed archive rows.
   - `get_reply_chain(message_id, *, guild_id, environment, max_depth)` — record call, return stubbed ancestor chain.
   - `get_author_recent_messages(author_id, *, guild_id, environment, hours_back, limit, channel_id)` — record call, return stubbed author messages.
3. **Add** a new test (e.g., `test_topic_editor_archive_read_tools_dispatch`) that:
   - Sets up a `FakeDB` with archive fixtures.
   - Dispatches each of the three new tools through `TopicEditor._dispatch_tool_call`.
   - Verifies outcome `read`.
   - Checks returned shapes have expected keys (`message_id`, `channel_name`, `author_name`, `content_preview`, `created_at`, `reply_to_message_id` for search/author; root-first ordering for reply chain).
   - Asserts `guild_id` / `environment` / default caps are passed through.
   - Asserts that large results get JSON-truncated at the existing cap.
4. **Keep** the existing read-tool test (`test_topic_editor_run_once_uses_native_tools_and_topic_run_lifecycle`) intact so it continues proving `search_messages` remains source-window scoped — only the line-273 count assertion changes.

### Step 6: Validate
**Scope:** Small
1. **Run** the targeted runtime test file first:
   ```bash
   pytest tests/test_topic_editor_runtime.py -v
   ```
2. **Run** a broader relevant suite if available:
   ```bash
   pytest tests/test_topic_editor_runtime.py tests/test_live_update_editor.py -v
   ```
   If `test_live_update_editor.py` is absent or slow, fall back to the repo's established targeted test command.
3. **Confirm** no files in the guarded paths (`src/features/summarising/live_update_editor.py`, `src/features/summarising/live_update_prompts.py`) were modified.

## Execution Order
1. Add tests/FakeDB expectations first or in lockstep with dispatch so the API contract is concrete.
2. Add storage methods before DB wrappers.
3. Add DB wrappers before topic-editor dispatcher cases.
4. Update prompt/tool schemas after the backing runtime path exists.
5. Update the line-273 test assertion early (or alongside Step 5) so the existing test doesn't break during development.
6. Validate with `pytest tests/test_topic_editor_runtime.py -v` before any broader test run.

## Guardrails
- Do not modify the existing four read tools' schemas or implementations.
- Do not touch `src/features/summarising/live_update_editor.py` or `src/features/summarising/live_update_prompts.py`.
- Do not change the agent loop, finalize behavior, idempotency, lease handling, force-close behavior, collision handling, or vision tools.
- Keep DB reads compact and bounded: storage methods enforce the hard caps even if dispatch passes larger values.
