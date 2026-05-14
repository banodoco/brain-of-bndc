# Execution Checklist

- [x] **T1:** Replace topic-editor tool schema, registry, and system prompt for unified search + reply chain.

Files: src/features/summarising/topic_editor.py

Exact changes:
1. READ_TOOL_NAMES (line 26-33): add "get_reply_chain" to the set.
2. TOPIC_EDITOR_TOOLS (lines 91-105): replace the existing `search_messages` dict with the unified schema (all params optional): query (string), from_author_id (integer), in_channel_id (integer), mentions_author_id (integer), has (array of enum: image/video/audio/link/embed/file), after (string), before (string), is_reply (boolean), limit (integer, default 20, max 50), scope (string, enum: window/archive, default "window").
3. TOPIC_EDITOR_TOOLS: after the `get_message_context` tool closing brace (after line ~123), insert the `get_reply_chain` tool dict: message_id (string, required), max_depth (integer, default 5, max 15).
4. _format_tool_input_hint (line ~1810-1813): remove search_messages from the {"search_topics", "search_messages"} set. Add a separate branch for search_messages showing query + scope, and a branch for get_reply_chain showing a message_id snippet.
5. TOPIC_EDITOR_SYSTEM_PROMPT (lines 46-74): replace the read-tool list on line 49 to include get_reply_chain. Add usage examples paragraph after the existing read-tool sentence (3-5 lines showing search_messages with from_author_id/has/scope and get_reply_chain invocation pattern). Keep existing descriptions for search_topics, get_author_profile, get_message_context unchanged.

Do NOT touch live_update_editor.py, live_update_prompts.py, finalize_run, the agent loop, override-collision, or vision tools.
  Executor notes: All 5 changes applied. READ_TOOL_NAMES updated, search_messages schema replaced (old params removed), get_reply_chain tool inserted at correct position, _format_tool_input_hint refactored, system prompt updated with examples. Verified: live_update_prompts test green (25/25), media_understanding + core green (10/10). One expected test_topic_editor_runtime failure (tool count 13→14) deferred to T5.
  Files changed:
    - .megaplan/debt.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/.plan.lock
    - .megaplan/plans/add-image-video-understanding-20260514-0055/phase_result.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.md
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.meta.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/state.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/.plan.lock
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_output.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/faults.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/gate.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/phase_result.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/state.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_revise_v2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/.plan.lock
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_audit.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_3.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_4.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_5.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_6.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_7.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_8.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_9.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/faults.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/final.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_snapshot.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate_signals_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/phase_result.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.meta.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/review.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/state.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_execute_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_finalize_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_gate_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_plan_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_review_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/user_actions.md
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/.plan.lock
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/.plan.lock
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/faults.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/final.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_snapshot.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/gate.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/phase_result.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_finalize_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_plan_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_revise_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/user_actions.md
    - requirements.txt
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py

- [x] **T2:** Add time-parser helper, replace window search, and wire window-scope dispatch.

Files: src/features/summarising/topic_editor.py

Exact changes:
1. Add `_parse_time_bound(value: Optional[str], default: datetime) -> datetime` near `_message_is_since` (around line 1078):
   - If value is None, return default.
   - If value matches ISO timestamp, parse with datetime.fromisoformat(), ensure UTC.
   - If value matches relative pattern `r'^(\d+)\s*([hd])$'`, subtract `int(hours)` from datetime.now(timezone.utc).
   - Otherwise raise ValueError("Invalid time format: {value}") — this will surface as tool_error.
2. Rename `_search_source_messages` (line 1034) to `_search_window_messages`. Rewrite signature to accept keyword-only args: messages, query, from_author_id, in_channel_id, mentions_author_id, has, after, before, is_reply, limit.
3. Filter logic:
   - query: case-insensitive substring match on content.
   - from_author_id: str(m.get("author_id")) == str(from_author_id).
   - in_channel_id: str(m.get("channel_id")) == str(in_channel_id).
   - mentions_author_id: re.search(rf'<@!?{mentions_author_id}>', str(content)).
   - has: check attachments/embeds/content URL patterns (image/video/audio by content_type or filename extension, link by http://|https:// in content, embed by non-empty embeds jsonb array via _normalize_attachment_list + _as_json_array pattern, file by non-empty attachments array).
   - after/before: apply _parse_time_bound, then _message_is_since-style datetime comparison.
   - is_reply: check message.get("reply_to_message_id") or message.get("reference_id") is/is-not None/None.
   - limit: clamp 1-50, default 20.
4. Return compact rows: {message_id, channel_id, channel_name, author_id, author_name, content_preview (capped 200 chars via _cap_text), created_at, reply_to_message_id, has_attachments, has_links, has_image, has_video, has_audio, has_embed}.
5. In _dispatch_read_tool (line 811-819): for scope='window', call self._search_window_messages(context.get("messages") or [], ...). Extract all new params from args. Default scope='window'. Unknown scope → ValueError → tool_error.
  Depends on: T1
  Executor notes: All 5 changes applied. _parse_time_bound added, _search_source_messages renamed to _search_window_messages with unified AND-combined filter logic, dispatch wired for window scope with scope default. Archive scope placeholder returns None (T4 will wire). Verified: live_update_prompts 25/25, media_understanding+core 10/10, topic_editor_runtime 16/18 (2 expected failures deferred to T5). No remaining references to old _search_source_messages. Import check passes.
  Files changed:
    - src/features/summarising/topic_editor.py

- [x] **T3:** Add storage methods for archive search and reply chain in storage_handler.py.

Files: src/common/storage_handler.py

Exact changes:
1. After get_topic_editor_message_context (ends ~line 1309), add async `search_messages_unified(self, *, scope, guild_id, environment, query, from_author_id, in_channel_id, mentions_author_id, has, after, before, is_reply, limit=20)` returning Dict with {"messages": [...], "truncated": bool}.
2. Build Supabase query on discord_messages selecting: message_id, channel_id, author_id, content, created_at, reference_id, attachments, embeds.
3. Always filter .eq('is_deleted', False). Add .eq('guild_id', guild_id) if guild_id is not None.
4. query: .ilike('content', f'%{query}%') if query is not None (parameterized — safe).
5. from_author_id: .eq('author_id', from_author_id).
6. in_channel_id: .eq('channel_id', in_channel_id).
7. mentions_author_id: use Supabase .or_() with parameterized token patterns: `.or_(f"content.ilike.%<@{mentions_author_id}>%,content.ilike.%<@!{mentions_author_id}>%")` — integer IDs formatted into fixed token patterns, NOT user-controlled free text.
8. after/before: parse via duplicated time-parser (or import from topic_editor if feasible without circular import; prefer local copy). Clamp range to max 30 days: if (before - after) > 30 days, clamp the earlier bound so effective window ≤ 30 days.
9. is_reply: .not_.is_('reference_id', 'null') if True, .is_('reference_id', 'null') if False.
10. has filters: fetch candidate set with indexed filters (author, channel, time, reply-status), then Python post-filter using _as_json_array on attachments/embeds. Check: image/video/audio by content_type prefix or filename extension, link by regex in content, embed by non-empty embeds array, file by non-empty attachments array.
11. Attach channel context via _attach_channel_context_and_filter_nsfw and author names via get_author_context_snapshots (same pattern as get_topic_editor_message_context at lines 1271-1305).
12. Produce compact rows identical in shape to _search_window_messages: {message_id, channel_id, channel_name, author_id, author_name, content_preview (200 chars), created_at, reply_to_message_id, has_attachments, has_links, has_image, has_video, has_audio, has_embed}.
13. Result capping: if serialized JSON exceeds ~2KB (2048 bytes), truncate list and set truncated: true.
14. .limit(safe_limit) (1-50, default 20). .order('created_at', desc=True).

15. After search_messages_unified, add async `get_reply_chain(self, message_id, guild_id=None, environment='prod', max_depth=5)` returning List[Dict]:
16. Clamp max_depth 1-15, default 5.
17. Iterative loop: query discord_messages for one row by message_id (select message_id, channel_id, author_id, content, created_at, reference_id), .eq('is_deleted', False). Stop on null result, null reference_id, max_depth reached, or cycle (seen set of message_ids).
18. Collect ancestors, reverse for root-first order.
19. Attach channel names and author names via same helpers.
20. Return compact rows: {message_id, channel_id, channel_name, author_id, author_name, content_preview, created_at}.
  Depends on: T2
  Executor notes: Added module-level helpers (_media_check, _parse_single_bound, _parse_time_bounds) with frozenset extensions for image/video/audio detection. Added async search_messages_unified with two-pass filtering: indexed SQL filters (author, channel, time, reply-status, query, mentions via .or_/.ilike) first, Python post-filter for jsonb attachment/embed has checks. Uses _attach_channel_context_and_filter_nsfw and get_author_context_snapshots helpers. Result capped at ~2KB with truncated flag. Added async get_reply_chain with iterative reference_id walking, cycle detection (seen set), max_depth clamp 1-15, and root-first ordering via compact.reverse(). Both methods include proper error handling. Syntax check passes. All 35 non-deferred tests green (10 media+core, 25 live_update_prompts). Only 2 expected T5-deferred failures remain in test_topic_editor_runtime.
  Files changed:
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_audit.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_3.json

- [x] **T4:** Add DB handler wrappers and wire archive-scope + reply-chain dispatch.

Files: src/common/db_handler.py, src/features/summarising/topic_editor.py

Exact changes in db_handler.py:
1. After get_topic_editor_message_context wrapper (ends ~line 532), add sync `search_messages_unified(self, ...)` passing through to self.storage_handler.search_messages_unified(...) via _run_async_in_thread. Return empty dict {"messages": [], "truncated": false} if no storage_handler.
2. After that, add sync `get_reply_chain(self, message_id, guild_id=None, environment='prod', max_depth=5)` wrapper passing through to self.storage_handler.get_reply_chain(...) via _run_async_in_thread. Return [] if no storage_handler.
3. Do NOT modify the older DBHandler.search_messages (line 121) — it serves a different (non-topic-editor) purpose.

Exact changes in topic_editor.py (_dispatch_read_tool, lines 811-839):
4. In the search_messages branch: extract all new params from args (query, from_author_id, in_channel_id, mentions_author_id, has, after, before, is_reply, limit, scope). Default scope='window'. For scope='archive', call self.db.search_messages_unified(guild_id=context.get("guild_id"), environment=self.environment, ...). For scope='window', call self._search_window_messages (already wired in T2). Unknown scope → ValueError.
5. After the elif chain but before the final else (before line 837), add elif for get_reply_chain: extract message_id (required), max_depth (default 5, clamp 1-15). Call self.db.get_reply_chain(message_id=..., guild_id=context.get("guild_id"), environment=self.environment, max_depth=...).
  Depends on: T3
  Executor notes: Added sync search_messages_unified and get_reply_chain wrappers to db_handler.py via _run_async_in_thread, inserted after get_topic_editor_message_context (~line 532). Did NOT modify DBHandler.search_messages at line 121. Wired archive-scope dispatch in _dispatch_read_tool: scope='archive' calls self.db.search_messages_unified(guild_id=context.get('guild_id'), environment=self.environment, ...). Added get_reply_chain elif before the else, with max_depth clamped 1-15 (default 5). Unknown scope → ValueError → tool_error. All watch items: _run_async_in_thread pattern, get_reply_chain before else, guild_id+environment passed through, no older DBHandler.search_messages touched. 16/18 runtime tests pass (2 expected T5-deferred failures). 35/35 guard tests green.
  Files changed:
    - src/common/db_handler.py
    - src/features/summarising/topic_editor.py

- [x] **T5:** Update tests and run full validation suite.

Files: tests/test_topic_editor_runtime.py

Exact test changes:
1. FakeDB (line 57): add search_messages_unified_calls = [] list and search_messages_unified(...) recording method returning {"messages": [], "truncated": false}. Add get_reply_chain_calls = [] list and get_reply_chain(...) method returning self.reply_chain_rows (default []).
2. Update test_topic_editor_read_tool_dispatch_via_fake_db (line 912): rename search_messages inputs from channel_id→in_channel_id, author_id→from_author_id. Update expected result assertion (line 922-930) to match new row shape (which now includes reply_to_message_id, has_attachments, has_links, etc.).
3. Add window-scope filter tests:
   - AND-combined: query + from_author_id + in_channel_id.
   - has=["image"], has=["video"], has=["link"].
   - mentions_author_id matching <@42> / <@!42> in content.
   - is_reply=True / is_reply=False.
   - after="24h" / before="7d" relative time.
   - Malformed time → tool_error.
   - Limit clamp (0→1, 100→50).
4. Add archive-scope dispatch test: prove unified args flow to FakeDB.search_messages_unified(...) with correct guild_id and environment.
5. Add reply-chain dispatch test: prove get_reply_chain calls FakeDB.get_reply_chain(...) and returns root-first rows.
6. Add cycle-detection test: seed FakeDB.reply_chain_rows with A→B→A, verify helper stops and returns [A, B] without infinite loop.
7. Keep semantic coverage from old search_messages tests — just update param names and row shape assertions.

Validation (run in order):
1. pytest tests/test_topic_editor_runtime.py -x -v
2. pytest tests/test_topic_editor_media_understanding.py tests/test_topic_editor_core.py -x
3. pytest tests/test_live_update_prompts.py -x  (guard — must remain green)
4. Full suite: pytest tests/test_topic_editor_runtime.py tests/test_topic_editor_media_understanding.py tests/test_topic_editor_core.py tests/test_live_update_prompts.py -v

Do NOT create new test files. Only modify tests/test_topic_editor_runtime.py.
  Depends on: T4
  Executor notes: All 70 tests pass across all four test suites. FakeDB extended with search_messages_unified, get_reply_chain, reply_chain_rows. Tool count updated 13→14. Old param names replaced. 13 new tests covering all filter combinations, archive dispatch, reply-chain dispatch, cycle detection. Two bugs discovered and fixed: author_context_snapshot extraction and limit=0 falsy handling. All guard tests green (live_update_prompts 25/25, media_understanding+core 10/10).
  Files changed:
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_4.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_5.json

## Watch Items

- Hard scope guards: Do NOT touch src/features/summarising/live_update_editor.py or src/features/summarising/live_update_prompts.py.
- Hard scope guards: Do NOT change finalize_run, the agent loop, override-collision, idempotency, lease-expiry, force-close, or vision tools.
- Do NOT modify the older DBHandler.search_messages at line 121 of db_handler.py — it serves a non-topic-editor purpose.
- mentions_author_id filtering MUST use Discord mention-token regex matching (<@ID> / <@!ID>) against content. There is NO mentions column in discord_messages.
- Archive has filtering uses two-pass approach: indexed SQL filters first, Python post-filter for jsonb attachment/embed checks.
- reference_id IS the reply-parent column and is mapped to reply_to_message_id in all tool output.
- scope defaults to 'window', preserving existing source-window search behavior.
- Archive 30-day range cap applies silently (clamp the interval). Malformed time syntax is still a tool_error.
- content_preview capped at 200 chars per message via _cap_text. Result JSON capped at ~2KB.
- Live_update_prompts.py test at line 265/321 references search_messages as a tool name — that test path is separate and must remain green.
- All existing tests outside test_topic_editor_runtime.py must remain green after changes.

## Sense Checks

- **SC1** (T1): Does TOPIC_EDITOR_TOOLS expose exactly one search_messages (unified schema) and a separate get_reply_chain? Are the old params channel_id, author_id, hours_back fully removed from the schema?
  Executor note: TOPIC_EDITOR_TOOLS has one unified search_messages (query, from_author_id, in_channel_id, mentions_author_id, has, after, before, is_reply, limit, scope) and separate get_reply_chain (message_id, max_depth). Old params channel_id, author_id, hours_back fully removed.

- **SC2** (T1): Does the system prompt include usage examples with the correct new param names (from_author_id, in_channel_id, has, scope, after) and get_reply_chain invocation?
  Executor note: System prompt lines 53-56: examples use from_author_id, has, scope, after, in_channel_id (correct unified schema names) and get_reply_chain(message_id=...) pattern.

- **SC3** (T2): Does _parse_time_bound raise ValueError for malformed input (not ISO, not relative Nd/Nh), and does that surface as tool_error in dispatch?
  Executor note: _parse_time_bound raises ValueError for malformed input (not ISO, not relative Nd/Nh). The ValueError propagates to _dispatch_read_tool's except Exception clause, which returns {outcome: 'tool_error', error: str(exc)}. Verified with 'garbage' input.

- **SC4** (T2): Does the window search AND-combine all filter params correctly — especially mentions_author_id via <@ID>/<@!ID> regex and has via attachment content_type/extension checks?
  Executor note: Window search AND-combines all filters correctly. mentions_author_id uses re.search(rf'<@!?{id}>', content) for <@ID>/<@!ID> tokens. has filter checks content_type prefix and filename extension via _normalize_attachment_list. All filters sequential with continue-on-mismatch for AND semantics. Verified with unit smoke tests for each combination.

- **SC5** (T3): Does archive mentions_author_id use .ilike()/.or_() on content with <@ID> and <@!ID> patterns — NO reference to a non-existent mentions column?
  Executor note: mentions_author_id filtering uses supabase .or_() with .ilike() on the content column checking for '<@ID>' and '<@!ID>' token patterns. The integer ID is cast to int before formatting into fixed token patterns — no reference to any non-existent mentions column.

- **SC6** (T3): Does the reply chain walk reference_id iteratively, stop on max_depth/cycle/null, and return root-first?
  Executor note: get_reply_chain walks reference_id iteratively with: (1) for-loop bounded by safe_depth (max 15), (2) breaks on null reference_id, (3) cycle detection via seen set of message_ids initialized with starting message_id, (4) compact.reverse() for root-first ordering. One SQL query per depth step via .maybe_single().execute().

- **SC7** (T4): Do archive-scope dispatch and get_reply_chain dispatch pass guild_id and environment correctly?
  Executor note: archive scope: self.db.search_messages_unified(guild_id=context.get('guild_id'), environment=self.environment, ...). get_reply_chain: self.db.get_reply_chain(message_id=..., guild_id=context.get('guild_id'), environment=self.environment, max_depth=...). Both follow the same pattern as get_topic_editor_message_context and get_topic_editor_author_profile, ensuring proper Supabase query scoping.

- **SC8** (T5): Do all topic-editor tests pass? Are the old param names (channel_id, author_id) absent from test inputs? Does the live_update_prompts test remain green?
  Executor note: (not provided)

## Meta

Execution guidance:

1. Work in strict dependency order: T1 → T2 → T3 → T4 → T5. Each task depends on the prior.

2. For T2 (time parser + window search): The old _search_source_messages method is only called from _dispatch_read_tool at line 812. After renaming to _search_window_messages and changing the signature, update the dispatch call site. The _message_is_since helper (line 1078) is used elsewhere too — do NOT change it, just call it from the new _search_window_messages.

3. For T3 (storage methods): The time-parser must be duplicated locally OR shared via a utility. Check if topic_editor.py imports are feasible; if circular import risk exists, copy the ~15-line _parse_time_bound into storage_handler.py as a module-level helper. The Supabase Python client's .or_() accepts filter strings — the mention-token pattern MUST use f-strings with integer IDs, which is safe since the ID is cast to int before formatting into fixed '<@ID>' or '<@!ID>' tokens.

4. For the has filter in archive scope: Since Supabase Python client has limited jsonb operators and no guarantee of containment operators in this project's version, use the two-pass approach: (a) apply indexed filters (author, channel_id, time, reply-status, query) in SQL, (b) in Python loop over results to check attachment/embed jsonb for image/video/audio/embed/file/link presence. Check content_type starts-with and filename ends-with patterns for media type detection.

5. Reply chain: One SQL query per step. Use .maybe_single().execute pattern (same as get_topic_editor_message_context's approach). Track seen message_ids in a set for cycle detection.

6. Result capping: For _search_window_messages, apply limit during iteration (break when len(rows) >= safe_limit). For archive search, use .limit(safe_limit) on the Supabase query. For the 2KB JSON cap, after building the compact rows list, json.dumps and check len(); if over 2048, progressively pop from the end and retry with a truncated: true flag.

7. For T5 (tests): The existing test at line 912-930 expects specific row shapes. After the change, the row dicts will have additional keys (reply_to_message_id, has_attachments, has_links, has_image, has_video, has_audio, has_embed). Update the expected assertion to include these keys or use a partial-match approach. Make sure the search_messages input changes from {query, channel_id, author_id} to {query, in_channel_id, from_author_id}.

8. The live_update_prompts test at line 265/321 references search_messages as a tool string name — that test should not break because the name hasn't changed. But verify.

9. Do NOT modify the old _message_is_since helper — it's used by other code paths beyond search.

10. The question in the plan about 30-day cap (silent clamp vs tool_error): the plan says clamp silently. Do that.
