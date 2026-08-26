# Finding: Support DB Handler Async Safety

## Ranked findings (verified)
1. **Blocking Supabase writes on event loop.** `support_cog.py:364` `sb("support_agent_turns").insert(row).execute()` and `466-468` `upsert(...).execute()` are sync `httpx` inside `async def _persist_turn`/`record_outcome` — no `asyncio.to_thread`. All other writers use `await asyncio.to_thread(...execute)` — `storage_handler.py:162-163,244-245,354-355`, `supabase_query_handler.py:73-74,150-151`, `openmuse_interactor.py:180-184`. Blocks loop per RTT.
2. **`_processing_threads` plain `set`, no `asyncio.Lock`.** `support_cog.py:173` `set()`, guards `381-383`, `412-414`, `540-542` do `if id in set: return` → `add(id)` with no `await` between — atomic within one tick, so safe under single-threaded `asyncio` today. Same as `grants_cog.py:58,222-224`. Undocumented invariant; `discord.py` dispatches listeners as separate Tasks, so any future `await` between check/add breaks dedup.
3. **Shared sync Supabase client.** `db_handler.py:63-64` single `supabase_client` (`supabase.create_client`, `storage_handler.py:60-64` sync `httpx`) shared across cogs. Not documented thread/async-safe; blocking direct calls contend with `to_thread` users. Peers use explicit `asyncio.Lock` per scope (`honeypot_cog.py:80,325`, `workflow_uploader.py:36`).
4. **Catch-up sequential but same block.** `support_cog.py:542-569` iterates candidates serially, `add`/`discard` in `finally:569` — no parallelism, but each turn still blocks on `_persist_turn`.

## Unknowns
- Does `discord.py` dispatch `on_message` + `on_thread_create` for the starter concurrently or serialized?
- `supabase-py` Client thread-safety under shared `to_thread` use.

## Risks
- Loop stall → missed triggers, interaction timeout after `defer` at `451`, heartbeat drops. Violates North Star "silence is failure" (delay, not loss — persist is `finally`).
- Future `await` between check/add → duplicate turns/replies/rows.

## Suggested approach (bounded)
- **Fix:** wrap both writes in `await asyncio.to_thread(lambda: sb(...).execute())` per codebase norm; keep best-effort `try/except`. No schema/queue change — `agent_goal` says DB is telemetry, not critical path.
- **Harden guard (optional):** document invariant or swap to `dict[int, asyncio.Lock]` like `honeypot_cog.py:80` to make atomicity explicit.
