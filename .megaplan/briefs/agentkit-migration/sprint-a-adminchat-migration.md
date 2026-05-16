# bndc agentkit migration A: AdminChatAgent on the kernel

Profile intent: `thoughtful//medium @codex +feedback`.

This milestone migrates `AdminChatAgent` off its hand-rolled "Arnold pattern" loop and onto `agentkit.loop.run_step`. It's the smallest blast-radius migration in the workspace (in-memory state, single user at a time) and serves as the first real production validation of the kernel.

## Prerequisites

- `agentkit v0.1.0` published and installable (pinned via `requirements.txt`).
- `agentkit-bootstrap-chain.yaml` `sprint-1-kernel-and-tools` milestone merged.

## Source plan

- `agentkit` repo: `docs/agentkit-design.md`, `docs/agentkit-sprint-plan.md` §Sprint 1 D11–D14.
- This repo: existing `src/features/admin_chat/agent.py` (loop at lines 246–513) and `src/features/admin_chat/tools.py` (~60 tool dicts at lines 93–500+).

## Goal

`AdminChatAgent` runs on `agentkit.loop.run_step` in prod behind `BNDC_USE_AGENTKIT=true`, with 24h of shadow-mode parity, then flag-flipped to 100% and the old loop deleted.

## Required scope

- Add `agentkit>=0.1.0,<0.2.0` to `requirements.txt`. Pin exact patch for now.
- Convert tool dicts in `src/features/admin_chat/tools.py` to `agentkit.tools.Toolkit` registrations. Use the `agentkit/scripts/convert_anthropic_schema.py` converter as a starting point; expect manual touch-ups for the long-tail tools.
- Each tool becomes: `InputModel` (Pydantic v2), `OutputModel`, async handler. Handler bodies are lifted verbatim from the current `execute_tool()` if-elif chain at `agent.py:4555+`.
- Replace `AdminChatAgent`'s loop body (`agent.py:328-484`) with a single `await run_step(...)` call. Conversation state moves from the in-memory `_conversations` dict into `agentkit.state.InMemoryConversationStore` keyed by `user_id`. Trim logic (`agent.py:219-244`) becomes a `Budget` cap on conversation bytes.
- Wire feature flag: `BNDC_USE_AGENTKIT` env var. When `true`, route through agentkit; when `false`, route through legacy. Default `false`.
- **Shadow mode**: when flag is `false` but `BNDC_SHADOW_AGENTKIT=true`, run *both* paths sequentially per turn. Legacy reply is sent to Discord; agentkit reply is stored in a new `admin_chat_shadow_diffs` Supabase table with `turn_id, legacy_reply, agentkit_reply, diff_summary, latency_legacy_ms, latency_agentkit_ms, cost_legacy_usd, cost_agentkit_usd`.
- A `scripts/diff_admin_shadow.py` script computes a daily report from `admin_chat_shadow_diffs`: % identical, % semantically-equivalent (LLM judge), % divergent, cost delta, latency delta.
- Migration: SQL for `admin_chat_shadow_diffs` table. Idempotent.
- Update `src/features/admin_chat/README.md` (or create one) with the new architecture and the cutover runbook.

## Cutover protocol

1. Deploy with `BNDC_USE_AGENTKIT=false` and `BNDC_SHADOW_AGENTKIT=true` for 24h minimum.
2. Run `scripts/diff_admin_shadow.py --since 24h`. Require ≥95% semantically-equivalent rate before flipping.
3. Investigate every divergent case. Common expected sources: prompt-cache breakpoint differences, conversation-trim boundary, tool result formatting whitespace. Fix in agentkit or in the migration shim, not in legacy.
4. Flip `BNDC_USE_AGENTKIT=true` (keep shadow off). Watch for 24h.
5. Delete legacy `execute_tool()` chain (`agent.py:4555-4750+`) and the in-memory `_conversations` dict. Delete the migration shim. Delete the shadow diff table migration's *reads* (table itself can stay for forensics).

## Explicit non-goals

- Do not migrate `TopicEditor` (separate milestone `sprint-b-topiceditor-and-yaml`).
- Do not add new admin-chat tools.
- Do not change the system prompt.
- Do not migrate the Discord event listener or the hourly archive loop.
- Do not introduce Supabase-backed conversation persistence (in-memory is fine; persistence is for v0.2+).
- Do not couple this milestone to `agentkit v0.2.0` features — must work against v0.1.0.

## Acceptance criteria

- All converted tool registrations import cleanly, schemas pass Pydantic validation against ≥5 real recorded admin-chat tool-call payloads.
- Unit tests in `tests/admin_chat/test_agentkit_runner.py` cover ≥3 multi-tool turns using recorded LLM responses.
- Staging deploy passes shadow mode at ≥95% parity for ≥24h.
- Prod cutover with flag at 100% sustained for ≥24h without regression (latency, cost, escalations).
- Legacy code deleted. `agent.py` shrinks by ≥40%. The remaining file is mostly Discord glue + tool handlers.
- Updated README/runbook explains how to rollback (set flag back to `false`) and how to debug a turn (link to shadow diff table + `obs/audit.py` audit events).

## Testing notes

- Use `pytest-recording` against Anthropic; never live-call in CI.
- Shadow diff script does not require LLM-judge in CI — that's a manual sanity tool, not a gate.
- Cost comparison in shadow mode is critical: agentkit's `usage_pricing.py` vs bndc's `ClaudeClient` accounting can drift; reconcile if delta > 5%.

## Risks and mitigations

- **Tool schema conversion long-tail.** ~60 tools, some with non-obvious JSON Schema features (`oneOf`, `anyOf`, nested objects). Budget 2 days, not 1.
- **Conversation trim semantics differ.** Legacy trims by JSON byte size; agentkit may trim by message count or token estimate. Add explicit `Budget(conversation_bytes=80_000)` to match.
- **Reply vs end_turn termination.** Legacy exits when `reply` or `end_turn` tool fires (line 483). In agentkit, these become "final" tools that produce a `StepOutcome.final` — verify both paths emit identical Discord output.
