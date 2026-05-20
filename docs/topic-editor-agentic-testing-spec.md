# Topic Editor Agentic Testing Spec

## Goal

Build a local replay and assessment loop that tests the topic editor as an agent-facing editorial system using frozen real Discord windows.

The central question is:

> Given real BNDC message windows and the same tools/context the production topic editor sees, does the agent make good editorial decisions and produce valid, concise, publishable drafts?

This is not a replacement for unit tests. Unit tests should continue to cover validators, renderers, storage wrappers, and pure functions. Agentic tests cover whether an LLM can actually use the surfaces correctly.

## Methodology

This spec follows the methodology in `/Users/peteromalley/Documents/reigh-workspace/Astrid/docs/agentic-testing-guide.md`.

Key principles:

- A scenario is data, not code.
- The actor sees only the brief.
- The harness owns priming and rubric.
- Evidence beats narrative.
- Evidence packs are frozen after every run.
- Assessors read evidence packs, not live state and not the agent's report alone.
- Signals are split into enforced, graded, and observed.
- Deterministic checks are preferred when the failure is mechanically detectable.
- Negative tests are required.
- Each iteration should tighten one named gap.

## Relationship To The Draft Pipeline

Project 1 creates the production/editorial surface:

- draft document
- evidence shelf
- draft tools
- validator
- preview
- submit path

This project tests whether that surface works for an LLM on real data.

The testing loop should be able to compare prompt versions, template versions, validation thresholds, model choices, and media shelf formats against the same frozen windows.

## Scenario Shape

Scenarios live as YAML.

```yaml
name: gemini_omni_megathread
tier: core
description: |
  Tests whether the editor avoids turning a large live-event thread into
  an oversized digest and instead produces a concise card-based update.

brief: briefs/gemini_omni_megathread.md
target_orchestrator: topic_editor_replay

priming:
  window:
    guild_id: 1076117621407223829
    start: "2026-05-19T17:00:00Z"
    end: "2026-05-19T18:10:00Z"
  fixtures:
    active_topics: from_fixture
    media_metadata: from_fixture

assessment:
  universal_checks: true
  enforced:
    - id: submitted_draft_passed_validation
      question: Did any submitted draft pass deterministic validation?
      evidence: [validation.json, final_action.json]
      grading: pass_fail
      weight: 3
  graded:
    - id: reads_like_feed_update
      question: Does the final draft read like a concise feed update rather than a digest?
      evidence: [draft.json, preview.json]
      weight: 2
  observed:
    - id: revision_attempts
      question: How many draft revision attempts occurred?
      evidence: [tool_trace.json]
      grading: numeric
```

The actor receives only the brief. It does not see the rubric.

## Fixture Freezing

Real-data testing should use frozen fixtures, not live moving Supabase state.

Add:

```bash
python scripts/freeze_topic_editor_window.py \
  --guild-id 1076117621407223829 \
  --start "2026-05-19T17:00:00Z" \
  --end "2026-05-19T18:10:00Z" \
  --name gemini_omni_megathread
```

Output:

```text
tests/agentic/fixtures/windows/gemini_omni_megathread/
  source_messages.json
  active_topics.json
  topic_sources.json
  recent_transitions.json
  media_metadata.json
  server_config_snapshot.json
  notes.md
```

Fixture refresh should be explicit. Replay should default to frozen JSON.

## Replay Runner

Add:

```bash
python scripts/replay_topic_editor_scenario.py \
  tests/agentic/scenarios/gemini_omni_megathread.yaml \
  --out runs/topic-editor-agentic/gemini_omni_v1
```

Runner responsibilities:

- Load scenario YAML.
- Load scenario brief.
- Load frozen fixture.
- Create isolated fake DB/write target.
- Run the topic editor with `publishing_enabled=False`.
- Allow real LLM mode or mock actor mode.
- Capture tool trace, draft state, validation, preview, simulated writes, and final action.
- Avoid prod DB writes and Discord sends.

## Local Safety Defaults

Defaults must be safe:

- `publishing_enabled=False`
- `allow_prod_writes=False`
- real Supabase reads only during fixture freezing
- replay uses frozen JSON
- fake DB captures writes
- Discord client is mocked
- output goes to `runs/`

Any live behavior should require explicit flags:

```bash
--refresh-fixture-from-supabase
--allow-dev-db-writes
```

There should be no flag that casually enables prod mutation.

## Evidence Pack

Every replay run writes an evidence pack.

```text
runs/topic-editor-agentic/<run_id>/
  scenario.yaml
  brief.md
  input_window.json
  source_messages.json
  active_topics.json
  media_metadata.json
  tool_trace.json
  draft.json
  draft_versions.json
  validation.json
  preview.json
  rendered_messages.txt
  final_action.json
  simulated_db_writes.json
  agent_report.md
  summary.json
```

Assessment must read only the evidence pack. If a future rubric needs to grade a surface not captured here, capture must be expanded before the rubric is trusted.

## Universal Deterministic Checks

Start with deterministic checks that apply to every scenario.

### `deliverable_shape`

Required files exist and are non-empty:

- `tool_trace.json`
- `final_action.json`
- `summary.json`

If a draft was created:

- `draft.json`
- `validation.json`
- `preview.json`

### `validation_contract`

Any submitted draft has no blocking validation errors.

### `discord_length_contract`

Rendered text units are within Discord limits.

### `citation_contract`

All inline `[N]` markers either resolve to the card's sources or are explicitly flagged by validation.

### `source_contract`

Every submitted card has at least one resolvable source.

### `media_contract`

Every referenced media id exists or has an explicit skip/fallback reason.

### `no_prod_write_contract`

Replay did not write to production Supabase and did not send to Discord.

## First Scenario Set

### `t2_relight_lora_positive`

Production failure shape tested:

- Does the agent turn a strong media-backed creation/update into a concise post with media attached?

Expected:

- `creation_release` or `technical_finding` template.
- 1-3 cards.
- Media immediately after relevant card.
- Submitted draft passes validation.

### `nonsens_prompt_relay_positive`

Production failure shape tested:

- Does the agent package a technical finding as problem/evidence/implication rather than a raw conversation recap?

Expected:

- `technical_finding` template.
- Concise problem statement.
- Evidence card with relevant source/media.
- Submitted draft passes validation.

### `gemini_omni_megathread_negative`

Production failure shape tested:

- Does the agent avoid creating a 3000+ char live-event digest?

Expected:

- Short curated draft with 2-4 cards, or no publish if the topic is too broad.
- Failure if any text unit exceeds limits or reads as a catch-all digest.

### `seedance_debate_negative`

Production failure shape tested:

- Does the agent avoid turning a long community debate into an 8000 char essay?

Expected:

- `community_debate` template.
- 2-4 concise cards.
- Strong omissions.
- Failure if the output tries to include every argument.

### `stale_media_recovery`

Production failure shape tested:

- Does the system recover from expired Discord media URLs?

Expected:

- Refresh, durable fallback, link fallback, or explicit skip reason.
- Failure if the run produces unexplained partial publish state.

### `weak_followup_watchlist`

Production failure shape tested:

- Does the agent update/watch instead of over-posting weak follow-up material?

Expected:

- `update_topic_sources` or `watch_topic`.
- No draft submit unless the new evidence is actually publish-worthy.

### `quiet_window_no_post`

Production failure shape tested:

- Does the agent avoid fabricating a post when nothing meaningful happened?

Expected:

- No submitted draft.
- Observation/watch/discard is acceptable.

### `missing_media_description_recovery`

Production failure shape tested:

- Does the agent avoid relying on unknown visual evidence?

Expected:

- Request/derive media understanding, or avoid media-dependent claims.

## Signal Tiers

### Enforced

Binary checks that gate the scenario:

- Submitted draft passed validation.
- No text over Discord limit.
- No unresolved card sources.
- No invalid citation markers in submitted draft.
- No production writes.
- Final action exists.
- If final action is submit, preview exists.

### Graded

Semantic quality, judged from evidence:

- Editorial usefulness.
- Concision.
- Correct template choice.
- Media placement.
- Good omission of weak material.
- Headline quality.
- Appropriate watch vs publish decision.

### Observed

Telemetry only:

- tool call count
- revision attempts
- card count
- text lengths
- source count
- media count
- warning count
- action chosen
- token cost
- latency
- model name
- failure bucket

## Assessment Script

First version:

```bash
python scripts/assess_topic_editor_pack.py runs/topic-editor-agentic/<run_id>
```

Outputs:

```text
runs/topic-editor-agentic/<run_id>/
  assessment.md
  assessment.json
  failure_buckets.json
```

Start with deterministic checks. Add LLM auditor only after evidence capture is reliable.

## Pattern Finder

Add:

```bash
python scripts/summarize_topic_editor_agentic_runs.py runs/topic-editor-agentic/
```

The synthesis report should group recurring failures:

- `overlong_draft`
- `digest_style`
- `weak_publish_choice`
- `missed_watchlist_update`
- `media_detached`
- `missing_citations`
- `unresolved_media`
- `stale_media`
- `fabricated_source`
- `too_many_sources`
- `no_preview`
- `validation_bypassed`

This is the report to read after each sweep. Do not fix scenario-by-scenario unless the pattern report shows it is isolated.

## Iterative Sweep Loop

Each sweep runs the same scenarios across a variant:

- prompt version
- template version
- validation thresholds
- model
- media shelf format
- warning/error policy

Example:

```bash
python scripts/run_topic_editor_sweep.py \
  --scenarios tests/agentic/scenarios \
  --variant draft_prompt_v2 \
  --samples 3 \
  --out runs/topic-editor-agentic/sweep-v2
```

Compare sweeps:

```bash
python scripts/compare_topic_editor_sweeps.py \
  runs/topic-editor-agentic/sweep-v1 \
  runs/topic-editor-agentic/sweep-v2
```

Track:

- pass rate on enforced checks
- average graded editorial quality
- failure bucket deltas
- variance across samples
- cost
- latency

Each iteration should have one hypothesis:

- Adding template examples reduces `digest_style`.
- Making missing inline citations an error reduces fallback refs.
- Media shelf descriptions improve `media_detached`.
- Lowering card max length reduces `overlong_draft`.

## LLM Auditor And Cross-Assessor Diff

Add an LLM auditor only after deterministic capture is reliable.

The LLM auditor should read only the evidence pack and return structured verdicts for graded questions.

Cross-assessor diff is a later enhancement:

```bash
python scripts/cross_assess_topic_editor_pack.py \
  --baseline deepseek \
  --challenger kimi \
  runs/topic-editor-agentic/<run_id>
```

Disagreement is signal. It often exposes cases where one assessor trusted narrative instead of evidence.

## N>1 Sampling

A single run is a sample, not a measurement.

After the basic harness works, run each scenario multiple times:

```bash
python scripts/run_topic_editor_sweep.py \
  --scenarios tests/agentic/scenarios \
  --variant draft_prompt_v2 \
  --samples 5
```

Variance is data. A scenario that passes once and fails twice is not stable.

## Mock Actor

Add a mock/replay actor later so evaluator development does not burn real LLM calls.

The mock actor should replay captured tool traces or canned tool responses and let developers iterate on:

- capture
- deterministic checks
- pattern finder
- report rendering

## Implementation Order

1. Build fixture freezer from Supabase.
2. Build replay runner with fake DB and no publishing.
3. Define evidence pack format.
4. Add deterministic assessor.
5. Add three initial scenarios: one positive, one negative, one recovery.
6. Add pattern finder.
7. Expand to six to eight scenarios.
8. Add N>1 sampling.
9. Add LLM auditor for graded questions.
10. Add cross-assessor diff after capture is reliable.

## Two-Week Sprint Fit

### Week 1

- Fixture freezer.
- Replay runner.
- Evidence pack capture.
- Deterministic checks for validation, length, source, citation, media, and no prod writes.
- First positive and negative scenarios.

### Week 2

- Six-scenario suite.
- Pattern finder.
- Sweep runner.
- First prompt/template comparison.
- Human review of failure buckets.
- Tighten one gap based on evidence.

## Done Criteria

- Can freeze a real Supabase window locally.
- Can replay the topic editor against frozen data with no prod writes.
- Evidence pack captures enough to grade without trusting narrative.
- At least six scenarios exist.
- At least two negative scenarios catch known bad behavior.
- Deterministic checks produce useful failure buckets.
- One sweep report compares two prompt/template variants.
- The team can answer whether a change made the editor shorter, better, and more media-aware.

