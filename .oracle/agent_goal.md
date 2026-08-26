# Agent Goal — #support auto-agent turn (VibeComfy + Hivemind)

Run: megado, 2026-08-26 · [North Star](./northstar.md) — this run delivers the first
end-to-end member-facing capability of that North Star: an agent that answers support
posts with cited community evidence and concrete workflow help.

## Objective
When a member creates a post in the Discord forum channel `1163250319107555388`
(#support, guild 1076117621407223829), run an automatic agent turn and reply in-thread.
Every follow-up message in the thread continues the same session. The agent's tool
surface includes:
1. **Hivemind discovery** — search the public Banodoco hivemind corpus so answers cite
   real community messages/workflows by jump URL wherever possible.
2. **VibeComfy workflow tools** — send specific workflows to members; when a member
   shares their workflow JSON/attachment, load it into the VibeWorkflow IR, edit per the
   request, validate, and post the edited JSON back into the thread; onboard members to
   VibeComfy usage.
3. **Archive research** — read-only search over archived Discord messages for citation.

## In scope
- New `src/features/support/` feature: cog (triggers) + agent glue (thread-keyed sessions).
- Reuse AdminChatAgent loop machinery with a scoped executor allowlist (reply/end_turn +
  research + the two new tool families). No admin-power tools on this surface.
- Stateless rebuild path: unknown thread → seed history from live Discord thread history.
- Startup catch-up scan for posts missed while bot was down.
- Config via env (`SUPPORT_CHANNEL_ID`, model override) following existing conventions.
- Tests defending observable behavior (trigger routing, session continuity, allowlist
  scoping, tool contracts), matching repo test conventions.

## Non-goals
- Executing workflows (GPU/RunPod runs) — edit/validate only.
- Persisting sessions to Supabase (in-memory + stateless rebuild is sufficient).
- Changes to grants/admin_chat/live-update behavior beyond minimal shared-code hooks.
- Any deployment/Railway config change beyond dependency addition if required.

## Settled decisions (from user)
- Support channel = 1163250319107555388.
- Agent gets both VibeComfy (workflow edits) and Hivemind (discovery).
- Emphasis: cite/reference Discord messages wherever possible, send specific workflows,
  update members' own workflows, output edited JSON back into the thread.

## Authorization boundaries
- Mutation: worktree only. No push, no merge to main, no deploy without explicit user
  approval at completion.
- Model policy: Ox Alpha for all classes (user-declared). No external dispatches.

## Done criteria
1. Bot posts an agent reply in a new #support forum-post thread from the trigger path
   (verified via tests exercising the real listener→agent→reply chain; live Discord
   verification not available in CI).
2. A second message in the same thread continues the conversation (session keyed by
   thread id; history includes prior turn).
3. After a simulated restart (empty session store), next message rebuilds context from
   thread history instead of starting cold.
4. Allowlist blocks admin-power tools on support turns (executor-level test).
5. Hivemind search tool returns corpus results (contract test against stubbed endpoint)
   and formats citations as jump URLs.
6. Vibecomfy edit tool takes workflow JSON + instruction, returns edited validated JSON
   (contract test with stubbed vibecompy calls where the package is unavailable in CI;
   real import path exercised when installed).
7. Full existing test suite passes; new tests pass deterministically offline.

## Validation commands
- `python -m pytest tests/test_support_hivemind.py tests/test_support_workflow.py tests/test_support_cog.py -q`
- `python -m pytest tests/ -q -x` (full suite, once, host-owned)

## Sync/promotion policy
Commit reviewed batches on `oracle-run`. Push/merge only after user reviews evidence.
