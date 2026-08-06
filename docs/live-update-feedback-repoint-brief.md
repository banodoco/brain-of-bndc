# Brief: Re-point live-update feedback from `live_update_feed_items` → `topics`

## Outcome
The live-update feedback feature must work against the table that **actually backs the
live-updates channel**: the `topics` table (written by the Topic Editor). Today it
reverse-looks-up `live_update_feed_items`, which is the wrong table — so every admin
reply in the live channel falls through to normal admin chat and the bot answers
conversationally instead of logging feedback. After this change, when an admin replies
to a live-update (topic) message, the bot resolves the reply to its `topics` row, logs
feedback tied to that topic, optionally edits / soft-deletes the topic, then ✅-reacts
and deletes the admin's reply.

## Root-cause (already diagnosed — do NOT re-investigate, build on this)
- The live-updates channel is populated by the **Topic Editor** (`topic_editor.py`),
  which stores its posted Discord message IDs in **`topics.discord_message_ids`** and
  publishes hourly in `prod`, guild `1076117621407223829`.
- The feedback feature only reverse-looks-up **`live_update_feed_items`**, a different,
  now-dormant table (last prod post 2026-05-08; recent rows are `dev`). So the lookup
  always returns None for real channel posts → `is_live_update_feedback=False` →
  conversational reply.
- `live_update_feedback` table is currently EMPTY — the feature has never logged a row.

## Locked decisions (verified against the live DB — treat as ground truth)
1. **Target table is `topics`.** Reverse-lookup must query `topics`, not
   `live_update_feed_items`. Drop the `live_update_feed_items` path (no dual-table).
2. **`topics.discord_message_ids` is a NATIVE Postgres array (`bigint[]`), NOT jsonb.**
   The correct PostgREST containment filter is a **bare Python list of strings**:
   ```python
   .contains('discord_message_ids', [str(message_id)])   # renders cs.{"123"} — MATCHES
   ```
   VERIFIED: this returns the correct topic row for message_id `1507752690019598519`
   ("Kijai Endorses Claude Code…").
   - Do NOT use `json.dumps(...)` here — that errors `22P02 malformed array literal`.
   - (Contrast: `live_update_feed_items.discord_message_ids` IS jsonb and needs
     `json.dumps([str(id)])`. That table's helper `get_feed_item_by_discord_message_id`
     stays as-is; it's just no longer on the feedback path. Do not delete it.)
3. **`topics` schema columns:** `topic_id` (uuid, PK), `discord_message_ids` (bigint[]),
   `environment` (text), `guild_id` (bigint), `publication_status` (e.g. 'sent'),
   `state` (e.g. 'posted'), `headline`, `summary`, `last_published_at`.
4. **Soft-delete = set `topics.state='deleted'`** (mirror the existing feed-item
   `status='deleted'` convention). NEVER hard-delete the `topics` row. Use `update_topic`.
5. **Calling convention for DatabaseHandler sync wrappers from the async cog:** wrap in
   `await asyncio.to_thread(self.db_handler.<wrapper>, ...)`. The `db_handler.*` methods
   are SYNC wrappers that internally run the async StorageHandler method via
   `_run_async_in_thread`. (Confirmed: see db_handler.py:365-400 calling-convention block.
   Do NOT "fix" these to direct awaits — that's correct as-is.)
6. **Feedback row keys to `topics`:** add a `topic_id uuid` column to `live_update_feedback`
   and make `feed_item_id` nullable. Store feedback against `topic_id`.

## Scope (IN)
1. **storage_handler.py** — add `async get_topic_by_discord_message_id(message_id, guild_id, environment)`
   querying `topics` with the bare-string-list `.contains` form above; select
   `topic_id, headline, summary, discord_message_ids, publication_status, state`.
   Mirror the existing `get_feed_item_by_discord_message_id` structure.
2. **db_handler.py** — add sync wrapper `get_topic_by_discord_message_id` mirroring the
   existing feed-item wrapper (incl. the to_thread calling-convention docstring; reader,
   NOT gated by `_live_write_allowed`).
3. **Migration** (`.migrations_staging/` + mirror to supabase/migrations) — alter
   `live_update_feedback`: add `topic_id uuid` (nullable), make `feed_item_id` nullable,
   add index on `(topic_id, replied_to_message_id)`. Do NOT auto-apply to the live DB —
   leave for the human to apply (the orchestrator/user applies it).
4. **storage_handler `store_live_update_feedback` / `get_live_update_feedback_for`** —
   accept and persist/query `topic_id`.
5. **tools.py** — `log_live_update_feedback` tool schema + `execute_log_live_update_feedback`:
   key on `topic_id` (injected from context) instead of `feed_item_id`. Update the tool
   description to say "live update (topic)".
6. **admin_chat_cog.py** — reverse-lookup block calls the topic lookup; set
   `resolved_topic` + `is_live_update_feedback`; channel_context enrichment passes the
   topic (e.g. `channel_context["live_update_topic"]`); post-turn feedback processing
   (delete-gate re-query, fallback store, ✅+delete) keys on `topic_id`. Keep the
   reply-suppression behaviour. Replace the silent `except Exception: pass` in the
   reverse-lookup block with a `logger` call so future failures are visible.
7. **agent.py (~line 449)** — context injection of `feed_item_id` → `topic_id`.
8. **LIVE_UPDATE_FEEDBACK_GUIDANCE** — reword "feed item" → "live update".
9. **Soft-delete path** — when the agent/cog soft-deletes, set `topics.state='deleted'`
   via `update_topic` (never hard-delete).
10. **Tests** — replace/repair tests that asserted the feed-item path; add a test that
    drives the REAL query builder for the topic lookup and asserts the bare-string-list
    `.contains` form (a `[str(id)]` list, NOT json.dumps). Do not mock away the method
    under test (that's how the original bug shipped green).

## Anti-scope (OUT — do not touch)
- Do not modify the Topic Editor publish flow or `topics` schema beyond reading it.
- Do not delete `get_feed_item_by_discord_message_id` or the `live_update_feed_items`
  table/handlers — just remove them from the feedback path.
- Do not change the generic `edit_message` / `delete_message` Discord tools (they operate
  on any message by ID and already work).
- Do not auto-apply the migration to the live database.

## Constraints
- Respect `environment` (prod/dev) isolation on every query/write (`_live_environment()`).
- Gate live writes via the existing `_live_write_allowed(guild_id)`.
- NEVER hard-delete a `topics` row; soft-delete = `state='deleted'`.
- Production bot auto-deploys from `main`; this change must not break boot.

## Done criteria
- Replying to a live-update (topic) message in the live channel resolves to its `topics`
  row, logs a `live_update_feedback` row keyed by `topic_id`, suppresses the
  conversational reply, ✅-reacts and deletes the admin reply.
- A test drives the real `topics` containment query and asserts the bare-string-list form.
- `pytest` for the live-storage + admin-chat feedback suites passes.

## Touchpoints
`src/common/storage_handler.py`, `src/common/db_handler.py`,
`src/features/admin_chat/admin_chat_cog.py`, `src/features/admin_chat/tools.py`,
`src/features/admin_chat/agent.py`, `.migrations_staging/`,
`~/Documents/banodoco-workspace/supabase/migrations/`,
`tests/test_live_storage_wrappers.py`, `tests/test_admin_chat_reply_hygiene.py`.
