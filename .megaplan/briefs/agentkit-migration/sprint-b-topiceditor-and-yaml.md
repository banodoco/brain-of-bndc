# bndc agentkit migration B: TopicEditor on the kernel + YAML chain via Workflow

Profile intent: `thoughtful//medium @codex +feedback`.

This milestone migrates `TopicEditor` to `agentkit.loop` with a proper `StepPlan` and `Budget` (replacing the hand-rolled forced-close + cost cap), and rewires the existing `live-update-social-megaplan-chain.yaml` to load through `agentkit.plan.Workflow.from_yaml(...)` so YAML is a serialization of `Workflow`, not a parallel concept.

## Prerequisites

- `agentkit v0.3.0` published and installable.
- `agentkit-bootstrap-chain.yaml` sprints 1, 2, and 3 all merged.
- `sprint-a-adminchat-migration` milestone in this chain merged and cutover stable for ≥7 days.

## Source plan

- `agentkit`: `docs/agentkit-design.md` §`plan/`, `docs/workflow.md`.
- This repo: `src/features/summarising/topic_editor.py` (loop at 675–778, forced-close at 779–799, cost/token budget at 704–723), `live-update-social-megaplan-chain.yaml`.

## Goal

`TopicEditor` runs on `agentkit.loop.run_step` with a proper `StepPlan` (final step terminated by `finalize_run` tool, forced-close as a `Gate.pre_step` audit event). The existing chain YAML loads via `Workflow.from_yaml` and runs through `agentkit`'s phase state-machine. Old runner code paths deleted.

## Required scope

- Bump `agentkit` pin to `>=0.3.0,<0.4.0`.
- Convert TopicEditor's ~15 hand-built tool schemas to `agentkit.tools.Toolkit` (Pydantic v2 in/out). Handlers ported from `_dispatch_tool_call` methods.
- Define a `topic_editor_plan: StepPlan` with one or more steps; the terminal step's allowed tools include `finalize_run` and the LLM must call it. Iteration cap matches current `TOPIC_EDITOR_MAX_TURNS`.
- `Budget(max_cost_usd=TOPIC_EDITOR_MAX_COST_USD, max_tokens=TOPIC_EDITOR_MAX_TOKENS, mode='abort')`. On exhaustion before `finalize_run`, an `agentkit.control.Gate.pre_step` callback emits a `ForcedCloseAuditEvent` (Veas-shape audit event) before the kernel raises `BudgetExhausted`.
- TopicEditor's archive RAG query (`topic_editor.py:576-611`) becomes a `HotContext` subclass (`TopicEditorHotContext`) populated at `Checkpoint.resume()` time. Markdown rendering is deterministic.
- Checkpoint cursor (`last_message_id`, `created_at`) moves into `agentkit.state.Checkpoint` with Supabase backend. The `topic_editor_runs` and `topic_editor_checkpoints` tables keep their schemas; the agentkit Checkpoint adapter writes to them via app-supplied SQL.
- Rewire `live-update-social-megaplan-chain.yaml` to load through `Workflow.from_yaml(...)`. Validate round-trip is lossless. The runner script that today calls `megaplan chain` directly becomes `python -m bndc.chain_runner --workflow live-update-social-megaplan-chain.yaml`, which dispatches through `agentkit` while delegating to megaplan's `auto.drive` for the underlying phase execution (megaplan's outer driver is unchanged in this milestone).
- Forced-close audit semantics preserved: every run that exits without `finalize_run` produces a `forced_close_reason` row in the run metadata, plus an `agentkit_audit_events` row with `event_type='topic_editor.forced_close'`.
- Update `src/features/summarising/README.md` (create if absent) with the new architecture.

## Cutover protocol

1. Stage agentkit-pathed runs against a copy of prod data: replay last 7 days of archive messages through the new path with publishing disabled. Diff against historical runs in `topic_editor_runs`.
2. Require ≥95% topic-decision parity (same `topic_id`, same action: `skip` / `update` / `create`) before enabling in prod.
3. Roll out behind `BNDC_TOPIC_EDITOR_USE_AGENTKIT=true`. Run for 24h on hourly cron.
4. Monitor cost / token usage — confirm `Budget` caps fire correctly; verify forced-close events fire correctly on synthetic budget-exhaustion test.
5. Delete legacy loop (`topic_editor.py:675-799`), legacy budget enforcement, and the old chain runner script.

## Explicit non-goals

- Do not change topic schema, alias semantics, or similarity-collision threshold.
- Do not migrate the Discord archive ingestion (`scripts/archive_discord.py`) — that's a separate pipeline.
- Do not change the megaplan `auto.drive` outer driver. This milestone only changes how the inner phase runners are invoked (via agentkit) and how the YAML is loaded (via Workflow).
- Do not introduce new tools, prompts, or models.

## Acceptance criteria

- `Workflow.from_yaml('live-update-social-megaplan-chain.yaml')` round-trips losslessly to the original YAML (modulo whitespace). Asserted as a test.
- Synthetic test: budget-exhausted run without `finalize_run` produces both a `forced_close_reason` in `topic_editor_runs` and a matching `agentkit_audit_events` row.
- 7-day replay parity ≥95% topic-action match vs historical runs.
- 24h prod run on flag with no regressions in cost, latency, or topic quality (manual spot-check).
- Legacy code deleted. `topic_editor.py` shrinks by ≥30%.

## Testing notes

- Replay harness must run against a snapshot of `messages_archive`, not live ingestion. Pin a snapshot date.
- LLM cost must match historical within ±15% per run.
- Forced-close must remain visible to ops in the existing admin views — verify the dashboards still surface it.

## Risks and mitigations

- **`finalize_run` plumbing.** The plan's final step needs the kernel to recognise `finalize_run` as a terminator. If `agentkit` v0.3.0 doesn't surface this as a first-class concept, model it as a `Gate.pre_step` that flips `StepOutcome.final` when the tool was called.
- **Cost-cap timing.** Legacy aborts after the LLM call that pushed over budget; agentkit checks *before* the next call. The first-iteration cost-cap behaviour may differ. Verify in shadow-mode replay.
