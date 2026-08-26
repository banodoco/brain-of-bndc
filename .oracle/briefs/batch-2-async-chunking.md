# Batch 2 — Async safety + fence-aware chunking (Ox Alpha)

North Star: thread=session, evidence-backed, no silent drops.

Task: In worktree /Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc-megado
- T2.1 Wrap both Supabase writes in `await asyncio.to_thread(lambda: sb(...).execute())`:
  * support_cog.py:364 `sb("support_agent_turns").insert(...).execute()`
  * support_cog.py:466-468 `sb("support_thread_outcomes").upsert(...).execute()`
  Follow codebase norm (storage_handler.py:162-163). Keep best-effort try/except. Add comment documenting _processing_threads atomicity (single-tick, no await between check/add).
- T2.2 Make `src/common/discord_utils.py:14-50` `split_message` fence-aware: track `in_fence` toggling on lines starting with ```, avoid splitting inside fence, if forced split then close with ``` and reopen next chunk with ```. Keep 2000 limit and paragraph preference (\n\n → \n → slice). Add guard for code block + long text co-occurrence.
- T2.3 Tests: Update or add tests for to_thread called (mock), fence preservation (split inside ``` retains open/close fences), 2000 cap, OutcomeView attachment on last chunk. Follow repo anyio/MagicMock conventions.

Acceptance: `pytest -k support` B2 tests pass; writes are to_thread, fences intact.

Do not touch main. Worktree only. User-selected Ox Alpha.
