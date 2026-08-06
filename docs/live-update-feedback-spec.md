# Live Update Feedback — Spec

## Outcome

Admins can give feedback on a live update simply by **replying to it in the live channel**. The bot runs an admin-agent turn driven by **per-channel guidance**, which:

1. Resolves the replied-to Discord message back to its logical update (`live_update_feed_items` row).
2. Researches the relevant context as needed.
3. **Logs the feedback** against the correct update in the database.
4. **Optionally acts on the update itself** — edits or deletes the bot's posted update message(s) when the feedback calls for it.
5. **Reacts ✅** to the admin's reply, then **deletes the reply** so the live channel stays clean.

The live channel is normally empty of human messages, so an admin reply there is, by definition, feedback worth a turn.

## Background — how live updates work today

- **Generation/posting:** `LiveUpdateEditor` (`src/features/summarising/live_update_editor.py`) runs on a loop, generates update candidates, and posts them to the **live channel** (resolved from `server_config.summary_channel_id` per guild via `_resolve_live_channel_id`, `live_update_editor.py:1971`).
- **The update record:** each posted update is a row in **`live_update_feed_items`** (Supabase). Key fields: `feed_item_id` (UUID PK), `title`, `body`, `guild_id`, `live_channel_id`, `environment`, `status`, and **`discord_message_ids`** — the ordered array of message IDs the bot actually posted for that update (`storage_handler.py:592`). **This array is the link from a Discord message back to the logical update.**
- **Admin messages:** `AdminChatCog.on_message` (`admin_chat_cog.py:1482`) routes admins to `_handle_admin_message`, which today only acts on DMs or guild messages that **@mention the bot** (`:1316`). A plain reply in the live channel currently does nothing.
- **The agent:** `AdminChatAgent` (`src/features/admin_chat/agent.py`) is a tool-calling loop (DeepSeek v4-pro). Tools are declared in `admin_chat/tools.py` with `execute_<name>` handlers. It already has `edit_message` (`tools.py:4304`) and `delete_message` (`tools.py:4329`) tools.
- **DB layer:** `db_handler` → `storage_handler` → Supabase. Schema changes are staged as SQL in `.migrations_staging/`, then handed off to the workspace Supabase migrations repo.

There is **no existing feedback table** for live updates — this is net-new.

## Design

### The reverse-lookup is the keystone

When an admin replies, take the replied-to message ID and find the feed item whose `discord_message_ids` array contains it (PostgREST `cs` / contains operator). A hit means it's live-update feedback — no separate channel check required for detection, though guidance is still keyed off the channel.

New query: `get_feed_item_by_discord_message_id(message_id, guild_id, environment)` in `storage_handler` + `db_handler`.

### Per-channel agent guidance (general mechanism)

A registry mapping a channel → a guidance string injected **prominently** into the agent turn for any admin message in that channel. Home: `server_config` (already per-guild, already holds `summary_channel_id`, auto-refreshes every 60s). Add a `channel_agent_guidance` map (`channel_id` → text), seeded with built-in live-update-feedback guidance for the live/summary channel, generic enough for other channels later.

The live-channel guidance instructs the agent, in effect:

> This channel is for feedback on live updates. The replied-to message is part of update X. Research the relevant context, then **log** the admin's feedback against that update using `log_live_update_feedback`. If the feedback calls for it, **edit** the update message(s) with `edit_message` or **delete** them with `delete_message` (which soft-deletes the update in the DB). Then the reply will be acknowledged and removed.

### Routing change

In `on_message` / `_handle_admin_message`: admin messages in a guidance-configured channel run an agent turn **without** requiring an @mention. Existing @mention behavior everywhere else is preserved, and payment/recipient routing in `on_message` is untouched.

### Enrich the agent turn

Extend `channel_context` (built around `admin_chat_cog.py:1391`) with:

- `channel_guidance` — surfaced prominently in the agent prompt.
- `live_update_feed_item` — the resolved update `{feed_item_id, title, body, discord_message_ids, …}`.

### Logging — the feedback table

New append-only table **`live_update_feedback`**:

| column | type | notes |
|---|---|---|
| `feedback_id` | uuid PK | |
| `feed_item_id` | uuid | FK-ish to `live_update_feed_items` |
| `guild_id` | bigint | |
| `environment` | text | `'prod'` / `'dev'`, default `'prod'` |
| `admin_user_id` | bigint | who gave the feedback |
| `feedback_text` | text | raw text of the admin reply |
| `replied_to_message_id` | bigint | the update message replied to |
| `disposition` | text | optional: agent's classification / action taken |
| `status` | text | e.g. `'logged'` |
| `created_at` | timestamptz | default `now()` |

Append-only — multiple notes per update are fine, and the editor / editorial memory can read it later.

New tool `log_live_update_feedback(feed_item_id, feedback_text, disposition?)` + `execute_log_live_update_feedback` handler → `db_handler.store_live_update_feedback(...)`.

### Acting on the update — edit & soft-delete

Based on the feedback, the agent may also change the update itself, reusing the existing tools:

- **Edit:** `edit_message` rewrites the Discord update message(s). Reflect the change on the feed item — set `status = 'edited'` (and update stored `title`/`body` if appropriate).
- **Delete:** `delete_message` removes the Discord update message(s). **Mark the feed item `status = 'deleted'` — do NOT delete the row.** The row is retained for audit/history; only the Discord-side message is removed and the DB status reflects the soft-delete.

A feed-item status updater is needed (extend `update_live_update_feed_item_messages`, which already takes a `status` param, or add `update_live_update_feed_item_status(feed_item_id, status, …)`).

### Acknowledgement — react then delete

The cog reacts ✅ to the admin's reply, then deletes the reply — **only after** a `live_update_feedback` row is confirmed for that feed item.

### Reliability — belt-and-suspenders fallback

Agent-driven logging is the primary path. **If the turn ends without a logged row, the cog stores the raw feedback text itself**, so feedback is never lost even if the model skips the tool or the LLM backend is down. Deletion of the admin reply is gated on a confirmed row (agent-logged or fallback).

## Done criteria

- An admin reply to a posted live update produces a `live_update_feedback` row tied to the correct `feed_item_id`, a ✅ reaction, and the reply deleted.
- When feedback calls for it, the agent edits the update message and sets the feed item `status = 'edited'`, or deletes the update message and sets `status = 'deleted'` **without removing the row**.
- A reply to a non-update message is unaffected.
- Existing @mention admin chat and payment/recipient routing still work.
- `environment` (prod/dev) isolation is respected throughout; live writes gated by existing `_live_write_allowed`.

## Touchpoints

- `src/features/admin_chat/admin_chat_cog.py` — routing + `channel_context` enrichment + ack/delete + fallback.
- `src/features/admin_chat/tools.py` — `log_live_update_feedback` tool + handler; reuse `edit_message` / `delete_message`.
- `src/common/storage_handler.py` — reverse-lookup, `store_live_update_feedback`, feed-item status update.
- `src/common/db_handler.py` — public wrappers for the above.
- `src/common/server_config.py` — `channel_agent_guidance` map.
- `.migrations_staging/` — new SQL migration for `live_update_feedback` (+ any `status` value notes).

## Anti-scope

- No UI for browsing feedback.
- No auto-reposting or regenerating updates — edit/delete of the existing message and logging only.
- Don't refactor the live update editor or unrelated parts of `on_message`.
- Never hard-delete the `live_update_feed_items` row.

## Open questions for the planner

- Exactly where to inject `channel_guidance` in the agent system prompt for maximum prominence.
- How to confirm the tool was called for delete-gating (in-turn flag vs. re-query the feedback table).
- Whether edits should also append a `live_update_feedback` row capturing the action taken (recommended: yes, via `disposition`).
