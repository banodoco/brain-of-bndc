# Implementation Plan: Phase 2 Prod Cutover Runbook for Live-Update Editor (revised)

## Overview
Author a single new markdown file, `docs/live-update-editor-phase-2-runbook.md`, that serves as the operator's source of truth during the Phase 2 cutover of the BNDC live-update editor. Phase 1 (per `docs/live-update-editor-redesign.md:609-644`) ships the redesigned editor in dev with publishing OFF; Phase 2 enables publishing, runs new + old editors side-by-side in prod for 24-48h, watches `topics.publication_status='partial'`, then renames legacy tables to `_legacy_*` and schedules deletion 2-4 weeks later.

The deliverable is a doc, not code. It must mirror `docs/runbook-payments.md` in tone (terse, imperative, copy-paste SQL), reference but not re-derive Phase 1 design, and default every deviation to ABORT-to-rollback + consult `docs/live-update-editor-redesign.md`. Length budget ~400 lines, top-to-bottom readable while a cutover is in flight.

Four invariants shape every section (these came out of critique iteration 1 and must survive review):

1. **Rename happens AFTER the 24-48h watch**, not before. Pre-rename rollback is a single toggle flip; post-rename rollback requires reverse-rename SQL. Section 9 only runs once §7 passes.
2. **Both editors keep writing to their own tables during the 24-48h watch** — per `docs/live-update-editor-redesign.md:651`, only the legacy editor's *Discord send* is suppressed. Verification of "the flip took" must target Discord-send activity (legacy `live_update_editor_runs` rows showing zero publish activity and absence of new live-channel posts attributed to the legacy bot identity), NOT legacy table inserts.
3. **The partial-publish trip-wire is cross-window**, not rolling. Per `docs/live-update-editor-redesign.md:600`: "if partial-publish incidents happen twice during shadow-mode or prod" — Phase 1 shadow partials count toward the trip-wire alongside Phase 2 watch partials.
4. **The runbook is provisional** until Phase 1 fills `TODO(phase-1)` placeholders: toggle name and location, suppression mechanism (scheduler-tick vs. publish-call), prod trace-embed channel, and migration-runner path (psql vs. supabase migrations folder vs. dashboard). Section 1 calls this out at the top so an operator does not run the procedure before the TODOs are answered.

## Main Phase

### Step 1: Confirm output path, reference anchors, and ground facts (`docs/live-update-editor-phase-2-runbook.md`, `docs/live-update-editor-redesign.md`, `docs/runbook-payments.md`, `src/common/storage_handler.py`)
**Scope:** Small
1. **Verify** `docs/live-update-editor-phase-2-runbook.md` does not already exist.
2. **Re-confirm** redesign anchors: data model + publication columns (`docs/live-update-editor-redesign.md:117-123`, `:223`), legacy-to-new mapping (`:212-223`, `:562`), Phase 2 outline (`:646-653`) — specifically `:651` "Both write to their own tables. Only new editor publishes to Discord; old editor's posts are suppressed" — trip-wires (`:599-604`), rollback intent (`:596`), Phase 1 trace embed + gate (`:611-644`), lease scope (`:684`).
3. **Confirm** the six legacy `live_update_*` tables exist as live runtime surfaces in `src/common/storage_handler.py:652-1968` (entry point `create_live_update_run` at `:652`; full set covers `live_update_editor_runs`, `live_update_candidates`, `live_update_decisions`, `live_update_feed_items`, `live_update_duplicate_state`, `live_update_editorial_memory`, `live_update_watchlist`). This is the authoritative list — `docs/live-update-editor-legacy-boundaries.md`'s "Legacy Storage And Helpers" is about `daily_summaries` and is NOT the right cross-reference.
4. **Confirm** repo migration convention: `.migrations_staging/` already hosts `*_live_update_*.sql` (e.g. `20260511110349_live_update_environment_split.sql`, `20260512185328_live_update_watchlist_lifecycle.sql`). Default the rename migration to a new timestamped file in that folder; carry a `TODO(phase-1)` for "or run inline via psql against prod if Phase 1 chooses the dashboard path."
5. **Use** `docs/runbook-payments.md:1-92` and `:286-407` as the structural template.

### Step 2: Draft section 1 — Header, audience, scope, and provisional-status banner (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** an H1 plus 3-5 line preamble: who reads this (oncall — POM or delegate during cutover), when (after Phase 1 gate passes), what it covers, what it does NOT cover (Phase 1 design — pointer to redesign doc).
2. **Add** a "Provisional until Phase 1 lands" banner immediately under the H1: this runbook references four Phase-1 deliverables that do not exist in `src/` yet — the publish toggle name + location, the legacy-editor Discord-send suppression mechanism (scheduler-tick gate vs. publish-call gate), the prod trace-embed channel, and the migration-runner path. Each is marked `TODO(phase-1)` inline. Do not execute the procedure until those markers are resolved by Phase 1.
3. **Add** Primary tools bullet list: `<LIVE_EDITOR_PUBLISH_TOGGLE>` placeholder, trace embed channel (`#main_test` in dev; prod equivalent `TODO(phase-1)`), the SQL one-liners in this doc, the legacy editor entrypoint (`src/features/summarising/live_update_editor.py:60`, publish wiring `:84-107` / `:261-346`).
4. **Add** Important guardrails:
   - Rename only AFTER the 24-48h watch.
   - Two partial-publish incidents *across the cutover window* (Phase 1 shadow + Phase 2 watch, combined per `docs/live-update-editor-redesign.md:600`) = ABORT + restore `topic_publications`.
   - Both editors keep writing to their own tables during the watch (`:651`); only Discord-send suppression matters.
   - Default to ABORT-to-rollback over improvisation.

### Step 3: Draft section 2 — Fast Triage SQL (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** a single SQL block scoped `environment='prod'` with `-- swap to 'dev' for pre-flip verification` inline:
   ```sql
   select publication_status, count(*) from topics
   where environment='prod' and last_published_at > now() - interval '1 hour'
   group by publication_status order by publication_status nulls last;
   ```
2. **Add** a "Then classify" bullet list per status (`pending`, `sent`, `partial`, `failed`, `null`) with one-line meaning and which section to jump to.
3. **Add** a state-summary one-liner: `select state, count(*) from topics where environment='prod' group by state;`.
4. **Add** a "Recent runs" one-liner for context (used by sections 5, 7, 8): `select run_id, environment, trigger, status, accepted_count, rejected_count, completed_at from live_update_editor_runs where environment='prod' order by created_at desc limit 5;` — note this row carries Phase-1's suppression marker (`TODO(phase-1)`: confirm exact column/metadata key the legacy editor sets when its Discord send is suppressed).

### Step 4: Draft section 3 — Pre-flip preconditions checklist (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** GATE — verification — pass criterion lines for: Phase 1 soak passed (per `docs/live-update-editor-redesign.md:642-644`); schema migration applied (5-table count check via `information_schema.tables`); named indexes present (`topics_state_idx`, `topics_revisit_idx`, `topics_headline_trgm`, `transitions_*` per `:188-191`); backfill row-count parity (`live_update_feed_items` vs `topics WHERE state='posted'`; `live_update_watchlist` vs `state='watching'`); runner lease key in place per `:684`; trace embed wired in prod monitoring channel per `:611-644`; legacy editor still running (most recent `live_update_editor_runs` row in last hour under legacy run-source); operator paged-in window booked; Phase-1 `TODO`s resolved (toggle name + location, suppression mechanism, prod trace channel, migration-runner path).
2. **End** the section: "If any gate is RED, do not flip. Consult `docs/live-update-editor-redesign.md` and reschedule."

### Step 5: Draft section 4 — Shadow-mode → cutover comparison rubric (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** a GO / TUNE / ABORT table keyed on Phase-1 signals from `docs/live-update-editor-redesign.md:611-644` and `:599-604`: replay-divergence explainability; collision-override rate (<10% GO / 10-25% TUNE / >25% ABORT per `:603`); `record_observation` rate (1-3/run GO / <1/run TUNE per `:602`); partial-publish in dev over soak (0 GO / 1 TUNE / counts toward the cross-window trip-wire — see Important guardrails); schema-rejection rate from `topic_transitions WHERE action LIKE 'rejected_%'` (<10% GO / 10-50% TUNE / >50% ABORT); cost/run, latency/run.
2. **Define** TUNE = delay flip + consult redesign; ABORT = walk away from Phase 2 this week.

### Step 6: Draft section 5 — Publishing-flag-flip procedure (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** ordered steps using `<LIVE_EDITOR_PUBLISH_TOGGLE>` placeholder. Suppression mechanism branches under a `TODO(phase-1)` — verification shape depends on which Phase 1 ships:
   - **Branch A — scheduler-tick gate (legacy editor never runs):** verification is "no new `live_update_editor_runs` rows under the legacy run-source after the toggle flip; new editor's run row appears on the next tick."
   - **Branch B — publish-call gate (legacy editor still runs but its Discord-send is no-op'd at `_publish_accepted_candidates` `src/features/summarising/live_update_editor.py:84-107` or `_select_publishable_candidates` `:261-346`):** verification is "legacy `live_update_editor_runs` rows still appear, but their suppression marker is set (`TODO(phase-1)`: exact column/metadata key) and zero new posts in the live channel are attributed to the legacy bot identity."
2. **Write** the procedure body:
   1. Re-run section 3 checklist.
   2. Confirm `<LIVE_EDITOR_PUBLISH_TOGGLE>` is set to the Phase-1-chosen suppression value, and the operator knows which branch (A or B) applies.
   3. Flip the toggle to its "new editor publishes / legacy suppressed" value.
   4. **Anchor verification on the next observed run, not a fixed 15-minute window** (cadence is hourly per `docs/live-update-editor-redesign.md:633`). Poll: `select run_id, status, completed_at from live_update_editor_runs where environment='prod' and created_at > '<flip timestamp>' order by created_at asc;` until a NEW row appears with `status='completed'`. Only then run verification.
   5. Verification SQL one-liner against `topics`:
      ```sql
      select publication_status, count(*) from topics
      where environment='prod' and last_published_at > '<flip timestamp>'
      group by publication_status;
      ```
   6. Confirm ≥1 `sent` row, zero `partial`, zero unexpected `failed`.
   7. Confirm the branch-appropriate suppression check from step 1 (Branch A: no legacy run; Branch B: legacy run present but suppression marker set + no legacy-attributed live-channel posts in the window — operator scrolls the live Discord channel for the new editor's bot identity only).
   8. If any check fails, jump to section 8.

### Step 7: Draft section 6 — Partial-publish detection SQL (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** four query blocks scoped `environment='prod'` with `-- swap to 'dev' for pre-flip` comments:
   - (a) windowed counts: last_hour / last_24h / since_cutover for `publication_status='partial'`.
   - (b) per-row deep-dive selecting `id, canonical_key, headline, state, publication_status, publication_error, publication_attempts, last_published_at, discord_message_ids` ordered by `last_published_at desc` limit 50.
   - (c) still-pending-too-long: `publication_status='pending' AND (last_published_at IS NULL OR last_published_at < now() - interval '90 minutes')`.
   - (d) rejection-rate against `topic_transitions WHERE action LIKE 'rejected_%'` last 1h, grouped by action.
2. **Add** a cross-window partial-publish tally for the trip-wire (per `docs/live-update-editor-redesign.md:600`):
   ```sql
   -- Phase 1 shadow partials (dev) + Phase 2 watch partials (prod). Trip-wire fires at >= 2.
   select environment, count(*) as partial_count from topics
   where publication_status='partial'
     and last_published_at >= '<phase-1-soak-start-date>'
   group by environment;
   ```
   Operator records the Phase-1 soak start date inline.
3. **Annotate**: "See `docs/live-update-editor-redesign.md:117-123`, `:223` for column shapes."

### Step 8: Draft section 7 — 24-48h watch decision tree (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** a markdown table SYMPTOM | THRESHOLD | ACTION | POINTER:
   - `publication_status='partial'`: 1 occurrence at any point in cutover window → INVESTIGATE; **2 cumulative across Phase 1 shadow + Phase 2 watch (per `docs/live-update-editor-redesign.md:600-601`) → ABORT** + restore `topic_publications` + section 8.
   - Rejection rate >50%/hr (queries from §6(d)) → TUNE / ABORT — consult redesign.
   - Collision-override rate >10% → TUNE per `:603`.
   - Duplicate posts to live channel: 1 isolated → HOT-FIX (check `_publish_accepted_candidates` idempotency / `topic_sources` uniqueness per `:625`); recurring → ABORT.
   - Legacy editor accidentally publishes to live channel → HOT-FIX: re-verify toggle and the branch-A/B verification from §5.
   - Discord 429s / auth errors from new editor → HOT-FIX routing; do not retry blindly.
   - **Lease symptoms (split per `docs/live-update-editor-redesign.md:684`)**:
     - New-editor self-contention (stuck run holding lease past expected tick): HOT-FIX — release lease for `(environment, guild_id, live_channel_id)`, verify next tick fires.
     - Legacy editor accidentally writing to NEW tables (`topics`, `topic_*`): ABORT — Phase 1 isolation broke.
     - New + legacy run rows overlapping in the same `(environment, guild_id, live_channel_id)` window: ABORT — lease misconfigured; legacy editor should be either suppressed (Branch B, runs but no publish) or skipped (Branch A, no run row).
   - Shape divergence vs. Phase 1 baseline: explainable → ACCEPT; unexplained → TUNE / ABORT.
   - `record_observation` <1/run sustained → INFO (post-cutover concern per `:602`).
2. **End**: "If symptom not listed, ABORT to rollback and consult `docs/live-update-editor-redesign.md`."

### Step 9: Draft section 8 — Rollback procedure (runner-pointer flip) (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** pre-rename rollback (common case during watch):
   1. Flip `<LIVE_EDITOR_PUBLISH_TOGGLE>` to its legacy value.
   2. Drain new-editor in-flight run OR force lease release for `(environment, guild_id, live_channel_id)` (`docs/live-update-editor-redesign.md:684`).
   3. Wait for next scheduler tick. Confirm legacy editor publishes via `_publish_accepted_candidates` (`src/features/summarising/live_update_editor.py:84-107`):
      - Branch A (scheduler-tick gate): a new legacy `live_update_editor_runs` row appears under the legacy run-source.
      - Branch B (publish-call gate): existing legacy run rows now show the suppression marker CLEARED and a new live-channel post is attributed to the legacy bot identity.
   4. Verify a new row in `live_update_feed_items` for the prod run AND a corresponding Discord post.
   5. Leave `topics` rows alone — do not truncate or rewrite. Cite `docs/live-update-editor-redesign.md:596`.
2. **Write** post-rename rollback variant (rare during watch): same toggle flip + reverse-rename SQL (mirror of §9 in reverse, six tables back to original names), then re-run pre-rename verification.
3. **State** explicitly: "Run section 9 rename only after the 24-48h watch passes. This keeps rollback to a single toggle flip during the watch window."

### Step 10: Draft section 9 — Table-rename migration SQL (post-watch) (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Name** the migration-runner path up front: default is a new timestamped file in `.migrations_staging/` matching the repo convention (e.g. `2026MMDDHHMMSS_live_update_legacy_rename.sql`). `TODO(phase-1)`: if Phase 1 elected the Supabase dashboard SQL editor or psql-direct-against-prod instead, swap the location accordingly; the SQL body is identical.
2. **Write** the ordered `ALTER TABLE ... RENAME TO` block in a single `begin; ... commit;` transaction for the six legacy tables per `docs/live-update-editor-redesign.md:212-223`: `live_update_feed_items`, `live_update_watchlist`, `live_update_duplicate_state`, `live_update_editorial_memory`, `live_update_candidates`, `live_update_decisions` → `_legacy_<name>`. Inline comment that `live_update_editor_runs` stays (`:223`, `:562`) and that swapping `commit;` for `rollback;` gives a dry-run preview.
3. **Add** constraint-preservation note: Postgres preserves indexes, sequences, FK constraints, and RLS policies across `RENAME TO`; do not recreate. Dependent views are NOT auto-updated — pre-check `select schemaname, viewname from pg_views where definition ilike '%live_update_%';` and recreate or drop matching views explicitly.
4. **Add** row-count parity SELECT before AND after using `union all` over the six tables; counts must match exactly.
5. **Add** "Record `_legacy_*` deletion date inline: __________ (operator picks 2-4 weeks from today, per `:653`)." Do not hardcode a date.
6. **Cross-check** the six tables against the authoritative sources: `docs/live-update-editor-redesign.md:212-223` and the live runtime surface in `src/common/storage_handler.py:652-1968` (NOT `docs/live-update-editor-legacy-boundaries.md`, which is about `daily_summaries`). If the set in those two sources has drifted, ABORT and consult the redesign doc.

### Step 11: Draft section 10 — Deletion schedule + close-out (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** "Before deletion date fires" checklist: no recent reads from `_legacy_*` (check application logs / DB stats), no open incidents referencing legacy data, POM explicit sign-off.
2. **Write** the `DROP TABLE _legacy_<name>;` block for all six tables in a single transaction.
3. **Add** Bring-forward circumstances (storage pressure, schema-drift complications, audit done early) and Push-out circumstances (active forensics, regulator request, ongoing post-cutover partial-publish investigation).
4. **End** with close-out checklist: legacy dropped, runbook archived, redesign doc updated to note Phase 2 done.

### Step 12: Draft section 11 — Consult-the-doc escape hatch footer (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** short footer: every section that could fork on Phase-1 deviation defaults to ABORT-to-rollback. Re-list the named redesign anchors from Step 1.
2. **Re-list** the three trip-wire signals from `docs/live-update-editor-redesign.md:599-604` — partial-publish ×2 cross-window → ABORT + restore `topic_publications`; override rate >10% → TUNE; `record_observation` <1/run → drop tool (post-cutover concern).

### Step 13: Self-review pass against test expectations and critique flags (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Walk** the eight `pass_to_pass` expectations and confirm each maps to at least one section.
2. **Verify** §5 verification is on Discord-send activity, NOT on legacy-table inserts (Branch A: legacy run absent; Branch B: legacy run present + suppression marker + no legacy-bot posts).
3. **Verify** §5 anchors on the next observed `live_update_editor_runs` row, not a fixed 15-minute window.
4. **Verify** §7 partial-publish threshold is "2 cumulative across Phase 1 shadow + Phase 2 watch" with explicit `:600` citation, not "≥2 in 24h."
5. **Verify** §7 lease symptoms are split into three distinct rows (new-editor self-contention; legacy writing to new tables; lease-key overlap), each with a distinct action.
6. **Verify** §1 carries the "provisional until Phase 1 lands" banner with the four named `TODO(phase-1)` items.
7. **Verify** §9 cross-checks against `docs/live-update-editor-redesign.md:212-223` and `src/common/storage_handler.py:652-1968` — NOT `docs/live-update-editor-legacy-boundaries.md`.
8. **Verify** §9 names a migration-runner path (default `.migrations_staging/` timestamped file) with a `TODO(phase-1)` for alternates.
9. **Verify** SQL is `environment='prod'` with `-- swap to 'dev'` annotations where pre-flip applies.
10. **Verify** schema shape is referenced (not duplicated) via redesign anchors.
11. **Verify** length ~400 lines.
12. **Verify** §10 step 5 leaves the deletion date blank.
13. **Verify** every `TODO(phase-1)` placeholder is visibly labeled.

## Execution Order
1. §1-2 (header + provisional banner, fast triage).
2. §3-5 (preconditions, rubric, flip — flip carries the Branch A/B verification shape).
3. §6-7 (detection SQL with cross-window tally, decision tree with split lease symptoms).
4. §8 (rollback) — before §9 so rename never appears without an adjacent rollback story.
5. §9-10 (rename with named runner path, deletion).
6. §11 + self-review pass last.

## Validation Order
1. Self-review pass (Step 13) against the eight `pass_to_pass` criteria and the nine flags resolved in this revision.
2. Anchor verification — every `docs/live-update-editor-redesign.md:LINE-LINE` and `src/...:LINE` reference resolves to the cited content.
3. No code edits, no tests. Doc-only change.
