# Implementation Plan: Phase 2 Prod Cutover Runbook for Live-Update Editor

## Overview
Author a single new markdown file, `docs/live-update-editor-phase-2-runbook.md`, that serves as the operator's source of truth during the Phase 2 cutover of the BNDC live-update editor. Phase 1 (per `docs/live-update-editor-redesign.md:609-644`) ships the redesigned editor in dev with publishing OFF; Phase 2 enables publishing, runs new + old editors side-by-side in prod for 24-48h, watches `topics.publication_status='partial'`, then renames legacy tables to `_legacy_*` and schedules deletion 2-4 weeks later.

The deliverable is a doc, not code. It must mirror `docs/runbook-payments.md` in tone (terse, imperative, copy-paste SQL), reference but not re-derive Phase 1 design, and default every deviation to ABORT-to-rollback + consult `docs/live-update-editor-redesign.md`. Length budget ~400 lines, top-to-bottom readable while a cutover is in flight.

Two structural decisions worth flagging up front, because they shape every section:

1. **Rename happens AFTER the 24-48h watch**, not before. This keeps rollback trivial during the watch window (just flip the runner pointer; both table families still live). The runbook must enforce that ordering explicitly so an operator doesn't pre-rename and discover rollback now requires reverse SQL.
2. **The publish toggle's exact name is a Phase-1 deliverable.** The runbook uses a placeholder `<LIVE_EDITOR_PUBLISH_TOGGLE>` with a leading `TODO(phase-1)` note. The toggle's name, where it lives (env var vs. config row vs. feature flag), and the exact suppression mechanism for the legacy editor's Discord send are filled in at execute time if known, or left as `TODO(phase-1)` if not.

## Main Phase

### Step 1: Confirm output path and reference anchors (`docs/live-update-editor-phase-2-runbook.md`, `docs/live-update-editor-redesign.md`, `docs/runbook-payments.md`)
**Scope:** Small
1. **Verify** `docs/live-update-editor-phase-2-runbook.md` does not already exist. The executor writes it fresh.
2. **Re-confirm** the redesign anchors the runbook will link to: data model + publication columns (`docs/live-update-editor-redesign.md:117-123`, `:223`), legacy-to-new table mapping (`:212-223`, `:562`), Phase 2 outline (`:646-653`), trip-wires (`:599-604`), rollback intent (`:596`), Phase 1 trace embed + gate criteria (`:611-644`), lease scope (`:684`).
3. **Use** `docs/runbook-payments.md:1-92` and `:286-407` as the structural template.

### Step 2: Draft section 1 — Header, audience, scope (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** an H1 plus 3-5 line preamble: who reads this (oncall — POM or delegate during cutover), when (after Phase 1 gate passes), what it covers, what it does NOT cover (Phase 1 design — pointer to redesign doc).
2. **Add** a Primary tools bullet list: `<LIVE_EDITOR_PUBLISH_TOGGLE>` placeholder, the trace embed channel (`#main_test` in dev, prod equivalent `TODO(phase-1)`), the SQL one-liners in this doc, the `live_update_editor.py` legacy path (cite `src/features/summarising/live_update_editor.py:60` and `:84-107`/`:261-346`).
3. **Add** Important guardrails: rename only AFTER 24-48h watch; two partial-publish incidents = ABORT + restore `topic_publications` per `docs/live-update-editor-redesign.md:600-601`; default to ABORT-to-rollback over improvisation.

### Step 3: Draft section 2 — Fast Triage SQL (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** a single SQL block scoped `environment='prod'` with `-- swap to 'dev' for pre-flip verification` inline:
   ```sql
   select publication_status, count(*) from topics
   where environment='prod' and last_published_at > now() - interval '1 hour'
   group by publication_status order by publication_status nulls last;
   ```
2. **Add** a Then classify bullet list per status (`pending`, `sent`, `partial`, `failed`, `null`) with one-line meaning + which section to jump to.
3. **Add** a state-summary one-liner: `select state, count(*) from topics where environment='prod' group by state;`.

### Step 4: Draft section 3 — Pre-flip preconditions checklist (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** GATE — verification — pass criterion lines for: Phase 1 soak passed (per `docs/live-update-editor-redesign.md:642-644`); schema migration applied (5-table count check via `information_schema.tables`); named indexes present (`topics_state_idx`, `topics_revisit_idx`, `topics_headline_trgm`, `transitions_*` per `:188-191`); backfill row-count parity (`live_update_feed_items` vs `topics WHERE state='posted'`, watchlist vs `state='watching'`); runner lease key in place per `:684`; trace embed wired in prod monitoring channel per `:611-644`; legacy editor still running (recent `live_update_editor_runs` rows under legacy run-source); operator paged-in window booked.
2. **End** the section: "If any gate is RED, do not flip. Consult `docs/live-update-editor-redesign.md` and reschedule."

### Step 5: Draft section 4 — Shadow-mode → cutover comparison rubric (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** a GO / TUNE / ABORT table keyed on Phase-1 signals from `docs/live-update-editor-redesign.md:611-644` and `:599-604`:
   - Replay-divergence explainability.
   - Collision-override rate (<10% GO, 10-25% TUNE, >25% ABORT — per `:603`).
   - `record_observation` rate (1-3/run GO, <1/run TUNE — per `:602`).
   - Partial-publish in dev over soak (0 GO, 1 TUNE, ≥2 ABORT — per `:600-601`).
   - Schema rejection rate from `topic_transitions WHERE action LIKE 'rejected_%'` (<10% GO, 10-50% TUNE, >50% ABORT).
   - Cost/run, latency/run.
2. **Define** TUNE = delay flip + consult redesign; ABORT = walk away from Phase 2 this week.

### Step 6: Draft section 5 — Publishing-flag-flip procedure (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** ordered steps using `<LIVE_EDITOR_PUBLISH_TOGGLE>` placeholder with leading `TODO(phase-1)`:
   1. Re-run section 3 checklist.
   2. Confirm legacy publish path will be suppressed when toggle flips. Cite `src/features/summarising/live_update_editor.py:84-107` and `:261-346` as the gated legacy code path. `TODO(phase-1)`: name suppression mechanism.
   3. Flip the toggle to its "new editor publishes / legacy suppressed" value.
   4. Wait for next hourly tick. Verify trace embed shows ≥1 `publication_status='sent'` row in `topics` AND zero new rows in legacy `live_update_feed_items` for the same window.
   5. Verification SQL one-liner (also lives in section 2):
      ```sql
      select publication_status, count(*) from topics
      where environment='prod' and last_published_at > now() - interval '15 minutes'
      group by publication_status;
      ```
   6. Confirm ≥1 `sent`, zero `partial`, zero unexpected `failed`. If any check fails, jump to section 8.

### Step 7: Draft section 6 — Partial-publish detection SQL (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** four query blocks scoped `environment='prod'` with `-- swap to 'dev' for pre-flip` comments:
   - (a) windowed counts: last_hour / last_24h / since_cutover for `publication_status='partial'`.
   - (b) per-row deep-dive selecting `id, canonical_key, headline, state, publication_status, publication_error, publication_attempts, last_published_at, discord_message_ids` ordered by `last_published_at desc` limit 50.
   - (c) still-pending-too-long: `publication_status='pending' AND (last_published_at IS NULL OR last_published_at < now() - interval '90 minutes')`.
   - (d) rejection-rate against `topic_transitions WHERE action LIKE 'rejected_%'` last 1h, grouped by action.
2. **Annotate**: "See `docs/live-update-editor-redesign.md:117-123`, `:223` for column shapes."

### Step 8: Draft section 7 — 24-48h watch decision tree (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** a markdown table SYMPTOM | THRESHOLD | ACTION | POINTER covering:
   - `publication_status='partial'`: 1 → INVESTIGATE; ≥2 in 24h → **ABORT** + restore `topic_publications` per `docs/live-update-editor-redesign.md:600-601` + section 8.
   - Rejection rate >50%/hr → TUNE / ABORT.
   - Collision-override >10% → TUNE per `:603`.
   - Duplicate live-channel posts: isolated → HOT-FIX (`_publish_accepted_candidates` idempotency / `topic_sources` uniqueness per `:625`); recurring → ABORT.
   - Legacy editor accidentally publishes → HOT-FIX: re-verify toggle + `src/features/summarising/live_update_editor.py:84-107` suppression.
   - Discord 429s / auth errors → HOT-FIX routing.
   - Lease contention (both editors writing topics) → ABORT (`:684`).
   - Shape divergence vs. baseline: explainable → ACCEPT; unexplained → TUNE / ABORT.
   - `record_observation` <1/run sustained → INFO (post-cutover, per `:602`).
2. **End**: "If symptom not listed, ABORT to rollback and consult `docs/live-update-editor-redesign.md`."

### Step 9: Draft section 8 — Rollback procedure (runner-pointer flip) (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** pre-rename rollback (common case during watch):
   1. Flip `<LIVE_EDITOR_PUBLISH_TOGGLE>` to legacy value.
   2. Drain new-editor in-flight run OR force lease release (`:684`).
   3. Confirm legacy scheduler tick fires and `_publish_accepted_candidates` writes to Discord (`src/features/summarising/live_update_editor.py:84-107`).
   4. Verify next prod run publishes via legacy path: row in `live_update_feed_items` AND a Discord post.
   5. Leave `topics` rows alone — do not truncate or rewrite. Cite `docs/live-update-editor-redesign.md:596`.
2. **Write** post-rename rollback variant (rare during watch): same toggle flip + reverse-rename SQL (mirror of section 9 in reverse), then verify legacy editor finds its tables.
3. **State** explicitly: "Run section 9 rename only after the 24-48h watch passes. This keeps rollback to a single toggle flip."

### Step 10: Draft section 9 — Table-rename migration SQL (post-watch) (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** the ordered `ALTER TABLE ... RENAME TO` block in a `begin; ... commit;` block for the six legacy tables per `docs/live-update-editor-redesign.md:212-223`: `live_update_feed_items`, `live_update_watchlist`, `live_update_duplicate_state`, `live_update_editorial_memory`, `live_update_candidates`, `live_update_decisions` → `_legacy_<name>`. Include inline comment that `live_update_editor_runs` stays (`:223`, `:562`) and that swapping `commit;` for `rollback;` gives a dry-run preview.
2. **Add** constraint-preservation note: Postgres preserves indexes, sequences, FK constraints, and RLS across `RENAME TO`; do not recreate. Dependent views are NOT auto-updated — pre-check `select schemaname, viewname from pg_views where definition ilike '%live_update_%';` and recreate or drop matching views explicitly.
3. **Add** row-count parity SELECT before AND after using `union all`. Counts must match exactly.
4. **Add** "Record `_legacy_*` deletion date inline: __________ (operator picks 2-4 weeks from today, per `:653`)." Do not hardcode a date.
5. **Cross-check** the six tables here against the current set in `docs/live-update-editor-legacy-boundaries.md`'s "Legacy Storage And Helpers". If mismatched, ABORT and consult redesign doc.

### Step 11: Draft section 10 — Deletion schedule + close-out (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** "Before deletion date fires" checklist: no recent reads from `_legacy_*`, no open incidents referencing legacy data, POM explicit sign-off.
2. **Write** the `DROP TABLE _legacy_<name>;` block for all six tables in a single transaction.
3. **Add** Bring-forward circumstances (storage pressure, schema-drift complications, audit done early) and Push-out circumstances (active forensics, regulator request, post-cutover partial-publish investigation).
4. **End** with close-out checklist: legacy dropped, runbook archived, redesign doc updated to note Phase 2 done.

### Step 12: Draft section 11 — Consult-the-doc escape hatch footer (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** short footer: every section that could fork on Phase-1 deviation defaults to ABORT-to-rollback. Re-list the named redesign anchors from Step 1.
2. **Re-list** the three trip-wire signals from `docs/live-update-editor-redesign.md:599-604`.

### Step 13: Self-review pass against test expectations (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Walk** the eight `pass_to_pass` expectations and confirm each maps to at least one section (pre-flip → §3; rubric → §4; flip procedure → §5; partial-publish SQL → §6; rollback → §8; rename SQL → §9; watch decision tree → §7; cleanup/deletion → §9–§10; escape hatch → §11 + inline pointers).
2. **Verify** the two-partial-publish trip-wire is cited in §7 and §11.
3. **Verify** schema shape is referenced (not duplicated) via redesign anchors.
4. **Verify** SQL is `environment='prod'` with `-- swap to 'dev'` comments where pre-flip applies.
5. **Verify** length ~400 lines (close to `docs/runbook-payments.md`).
6. **Verify** §9 step 4 leaves the deletion date blank.
7. **Verify** every `TODO(phase-1)` placeholder is visible and labeled, not silently guessed.

## Execution Order
1. §1-2 (header, fast triage).
2. §3-5 (preconditions, rubric, flip).
3. §6-7 (detection SQL, decision tree).
4. §8 (rollback) — before §9 so rename never appears without an adjacent rollback story.
5. §9-10 (rename, deletion).
6. §11 + self-review pass last.

## Validation Order
1. Self-review pass (Step 13) against the eight `pass_to_pass` criteria.
2. Anchor verification — every `docs/live-update-editor-redesign.md:LINE-LINE` reference resolves to the cited content.
3. No code edits, no tests. Doc-only change.
