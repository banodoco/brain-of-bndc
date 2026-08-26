# Explore: Hivemind/VibeComfy Tool Contracts — Spot-Check

**Ranked findings (verified, file:line cited)**

1. **Allowlist isolation is correct and executor-enforced.** `support_turn` flag set in `support_cog.py:234`, allowlist in `agent.py:614-627` (`reply, end_turn, find_messages, inspect_message, search_hivemind, comfy_workflow` only). Payment/moderation/social/message-mutation tools excluded. Dispatcher gate `tools.py:5845` returns `Permission denied` when not in `allowed_tools`. **Confused-deputy hardening:** `agent.py:814-821` overwrites `thread_id` and `832-836` overwrites `guild_id` from `channel_context`, so LLM cannot cross-post or pick guilds. Tests in `test_support_cog.py:176-204,642-647` defend this.

2. **Hivemind citation contract meets North Star.** `tools_support.py:51-68` maps `messages→message_feed`, `distillations`, `resources`. `build_hivemind_url:71-98` builds PostgREST `or=(ilike...)` + channel filter. `_format_message_result:114-130` emits `https://discord.com/channels/{guild_id}/{channel_id}/{message_id}` jump URLs (evidence over vibes). `execute_search_hivemind:153-196` handles `TimeoutError`/`ClientError`/non-200 (401) gracefully, never crashes loop. Schema `tools_support.py:200-240` requires `query` only; `DEFAULT_LIMIT 15 / MAX_LIMIT 30` clamped at `25-33`.

3. **ComfyWorkflow staged-edit + deliver contract intact, SSRF-guarded.** `comfy_tools.py:36-77` defines 4 modes (`describe/validate/edit/deliver`) with `thread_id`-keyed staging `316: _STAGED`. SSRF guard `24-31` allowlist + `94-112` `https`-only + `allow_redirects=False` + `115-120` 5 MB cap + JSON validation. Lazy import `84-91` degrades to `vibecomfy package not installed` (`368-369`). `deliver` `331-357` posts `edited_workflow_<ts>.json` via `_post_workflow_file:428-441`. Persistence is intentionally ephemeral (matches agent goal: no Supabase session persistence).

**Unknowns:** Hivemind publishable key hard-coded `tools_support.py:17` (public but rotates?); corpus freshness/latency unmeasured; `ilike` terms not escaped for `%_,()` — malformed query may 400; vibecomfy missing in CI (stubbed tests) — real import path not exercised here.

**Risks (low):** `_STAGED` in-memory accumulates per thread (no eviction); `edit_ops` schema loosely typed (`items: {type:object}`) — invalid ops surface only as runtime `ValueError`; `publishable` key in source.

**Suggested approach:** No change to contracts before implementation — reuse `AdminChatAgent` allowlist pattern. If touching: (a) escape PostgREST terms, (b) add `_STAGED` size/TTL or clear on deliver, (c) keep hard overwrite of `thread_id`/`guild_id` as source of truth. All aligned to concrete workflow delivery + cited evidence.
