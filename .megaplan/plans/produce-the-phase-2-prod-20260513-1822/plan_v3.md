# Implementation Plan: Phase 2 Prod Cutover Runbook for Live-Update Editor (revised v3)

## Overview
Author a single new markdown file, `docs/live-update-editor-phase-2-runbook.md`, that serves as the operator's source of truth during the Phase 2 cutover of the BNDC live-update editor. Phase 1 (per `docs/live-update-editor-redesign.md:609-644`) ships the redesigned editor in dev with publishing OFF; Phase 2 enables publishing, runs new + old editors side-by-side in prod for 24-48h, watches `topics.publication_status='partial'`, then renames legacy tables to `_legacy_*` and schedules deletion 2-4 weeks later.

The deliverable is a doc, not code. Mirror `docs/runbook-payments.md` in tone (terse, imperative, copy-paste SQL), reference but do not re-derive Phase 1 design, and default every deviation to ABORT-to-rollback + consult `docs/live-update-editor-redesign.md`. Length budget ~400 lines. Revision v3 changes are sentence-level additions; no new sections.

Five invariants survive review:

1. **Rename happens AFTER the 24-48h watch.** Pre-rename rollback = single toggle flip; post-rename rollback = reverse-rename SQL.
2. **Both editors keep writing their own tables during the watch** (per `docs/live-update-editor-redesign.md:651`). "Flip took" verification targets *Discord-send activity*, not legacy-table inserts.
3. **The partial-publish trip-wire is cross-window** per `docs/live-update-editor-redesign.md:600`. **Phase 1 shadow partials are sourced from Phase-1 shadow-mode telemetry** (trace embed in the Phase-1 monitoring channel + shadow logs per `:611-644`), **not from `topics`** — Phase 1 runs with publishing OFF (`:611`, `:627`, `:633`), so `topics.publication_status='partial'` cannot exist in dev by design. The trip-wire is: `(Phase-1 shadow-attempted-publish partials from telemetry) + (Phase-2 prod partials from topics) ≥ 2 → ABORT + restore topic_publications`.
4. **The runbook is provisional** until Phase 1 fills five `TODO(phase-1)` placeholders: toggle name + location, suppression mechanism (Branch A scheduler-tick vs. Branch B publish-call), Branch-B run-row suppression marker (if any), new-editor bot identity in the live Discord channel, prod trace-embed channel. **If Phase 1 ships any mechanism other than A or B** (hybrid, per-guild config branch, per-channel rollout, partial guild rollout), **ABORT to the consult-the-doc escape hatch** and do not improvise.
5. **Migration is authored in `.migrations_staging/` and applied from `supabase/migrations/`** — staging is a hand-off folder, not the run path. The runbook spells out the copy + apply step.

## Main Phase

### Step 1: Confirm output path, reference anchors, and ground facts (`docs/live-update-editor-phase-2-runbook.md`, `docs/live-update-editor-redesign.md`, `docs/runbook-payments.md`, `src/common/storage_handler.py`, `.migrations_staging/README.md`)
**Scope:** Small
1. **Verify** `docs/live-update-editor-phase-2-runbook.md` does not already exist.
2. **Re-confirm** redesign anchors: data model (`docs/live-update-editor-redesign.md:117-123`, `:223`), legacy-to-new mapping (`:212-223`, `:562`), Phase 2 outline (`:646-653`) — specifically `:651` "Both write to their own tables. Only new editor publishes to Discord; old editor's posts are suppressed" — trip-wires (`:599-604`), rollback intent (`:596`), Phase 1 publishing-OFF statements (`:611`, `:627`, `:633`), Phase 1 trace embed + gate (`:611-644`), lease scope (`:684`).
3. **Confirm** authoritative legacy-table set at `src/common/storage_handler.py:652-1968` (the six `live_update_*` tables + `live_update_editor_runs`); `docs/live-update-editor-legacy-boundaries.md`'s "Legacy Storage And Helpers" is about `daily_summaries` and is NOT the cross-reference.
4. **Confirm** migration hand-off: `.migrations_staging/README.md` says the folder is a HAND-OFF area whose contents must be copied to `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` and applied via `supabase db push` (matching the existing live_update_* migrations: `20260511110349_live_update_environment_split.sql`, `20260512185328_live_update_watchlist_lifecycle.sql`).
5. **Use** `docs/runbook-payments.md:1-92` and `:286-407` as the structural template.

### Step 2: Draft section 1 — Header, audience, scope, provisional banner (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** H1 + 3-5 line preamble: who reads this (oncall — POM or delegate during cutover), when (after Phase 1 gate passes), what it covers, what it does NOT cover (Phase 1 design — pointer to redesign doc).
2. **Add** a "Provisional until Phase 1 lands" banner enumerating the five `TODO(phase-1)` markers: (a) toggle name + location, (b) suppression mechanism (Branch A scheduler-tick OR Branch B publish-call), (c) Branch-B run-row suppression marker (column or metadata key, if any), (d) new-editor bot identity in the live Discord channel, (e) prod trace-embed channel. **Explicit out-of-scope clause: if Phase 1 ships a hybrid, per-guild config, per-channel rollout, or any third mechanism, ABORT to rollback and consult `docs/live-update-editor-redesign.md` — do not improvise.**
3. **Add** Primary tools bullet list: `<LIVE_EDITOR_PUBLISH_TOGGLE>` placeholder, trace embed channel (`TODO(phase-1)`), the SQL one-liners in this doc, the legacy editor entrypoint (`src/features/summarising/live_update_editor.py:60`, publish wiring `:84-107` / `:261-346`), the migration hand-off path (`.migrations_staging/` author → `supabase/migrations/` apply).
4. **Add** Important guardrails: rename only after 24-48h watch; partial-publish trip-wire is cross-window (Phase-1 telemetry + Phase-2 `topics`); both editors keep writing their own tables during the watch; default to ABORT.

### Step 3: Draft section 2 — Fast Triage SQL (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** the single SQL block scoped `environment='prod'` with `-- swap to 'dev' for pre-flip verification (note: dev rows are diagnostic only; no 'partial' can appear in dev during Phase 1 per :611/:627/:633)`:
   ```sql
   select publication_status, count(*) from topics
   where environment='prod' and last_published_at > now() - interval '1 hour'
   group by publication_status order by publication_status nulls last;
   ```
2. **Add** "Then classify" bullet list per status, with one-line meaning + jump-to section.
3. **Add** state-summary one-liner: `select state, count(*) from topics where environment='prod' group by state;`.
4. **Add** "Recent runs" one-liner: `select run_id, environment, trigger, status, accepted_count, rejected_count, completed_at from live_update_editor_runs where environment='prod' order by created_at desc limit 5;` with note that Branch-B suppression marker (if Phase 1 ships one) appears here — `TODO(phase-1)` for the exact column/metadata key.

### Step 4: Draft section 3 — Pre-flip preconditions checklist (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** GATE — verification — pass-criterion lines for: Phase 1 soak passed (`:642-644`); 5-table schema migration applied (`information_schema.tables` check); named indexes present (`topics_state_idx`, `topics_revisit_idx`, `topics_headline_trgm`, `transitions_*` per `:188-191`); backfill row-count parity (`live_update_feed_items` vs `topics WHERE state='posted'`; `live_update_watchlist` vs `state='watching'`); runner lease key in place (`:684`); trace embed wired in prod monitoring channel (`:611-644`); legacy editor still running (latest `live_update_editor_runs` row under legacy run-source within last hour); operator paged-in window booked; **all five Phase-1 TODOs resolved**; **Phase 1 actually shipped Branch A or Branch B (not a third mechanism)** — if a third mechanism, abort cutover.
2. **End**: "If any gate is RED, do not flip. Consult `docs/live-update-editor-redesign.md` and reschedule."

### Step 5: Draft section 4 — Shadow-mode → cutover comparison rubric (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** GO / TUNE / ABORT table keyed on Phase-1 signals from `:611-644` and `:599-604`: replay-divergence explainability; collision-override rate (<10% GO / 10-25% TUNE / >25% ABORT per `:603`); `record_observation` rate (1-3/run GO / <1/run TUNE per `:602`); **Phase-1 shadow-attempted-publish partials** from shadow-mode telemetry — 0 GO / 1 TUNE (counts toward cross-window trip-wire); schema-rejection rate from `topic_transitions WHERE action LIKE 'rejected_%'` (<10% / 10-50% / >50%); cost/run; latency/run.
2. **Annotate** partial-publish row: "Sourced from Phase-1 shadow telemetry (trace embed in the Phase-1 monitoring channel + shadow logs per `:611-644`), NOT from `topics WHERE environment='dev'` — Phase 1 runs with publishing OFF per `:611`/`:627`/`:633`, so `topics` cannot record a dev partial-publish."
3. **Define** TUNE = delay flip + consult redesign; ABORT = walk away from Phase 2 this week.

### Step 6: Draft section 5 — Publishing-flag-flip procedure (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** Branch A / Branch B verification shapes:
   - **Branch A (scheduler-tick gate):** primary signal = no new `live_update_editor_runs` rows under the legacy run-source after the flip timestamp; new editor's run row appears on the next tick.
   - **Branch B (publish-call gate at `_publish_accepted_candidates` `src/features/summarising/live_update_editor.py:84-107` / `_select_publishable_candidates` `:261-346`):** **primary signal = Discord-side count.** Operator scrolls the live Discord channel since the flip timestamp and confirms zero posts attributed to the new-editor bot identity's *legacy counterpart* (i.e. the legacy bot identity — `TODO(phase-1)` to confirm whether it is distinct from the new editor's identity). **Secondary, nice-to-have:** if Phase 1 writes a suppression marker on the legacy `live_update_editor_runs` row, an SQL check on that column/metadata key (`TODO(phase-1)`). If Phase 1 ships no marker, Discord-side count is the only signal — that is acceptable.
2. **Write** procedure body:
   1. Re-run §3 checklist; confirm Branch A vs. Branch B is known.
   2. Set `<LIVE_EDITOR_PUBLISH_TOGGLE>` to the Phase-1-chosen suppression value.
   3. Record the flip timestamp (will be the watch start).
   4. **Anchor verification on the next observed run, not a fixed window** (cadence is hourly per `:633`). Poll: `select run_id, status, completed_at from live_update_editor_runs where environment='prod' and created_at > '<flip timestamp>' order by created_at asc;` until a NEW row appears with `status='completed'`.
   5. Verification SQL one-liner against `topics`:
      ```sql
      select publication_status, count(*) from topics
      where environment='prod' and last_published_at > '<flip timestamp>'
      group by publication_status;
      ```
   6. Confirm ≥1 `sent`, zero `partial`, zero unexpected `failed`.
   7. Confirm the branch-appropriate signal from step 1.
   8. If any check fails, jump to §8.

### Step 7: Draft section 6 — Partial-publish detection SQL (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** four query blocks scoped `environment='prod'` with `-- swap to 'dev' for pre-flip verification (note: dev has no partials during Phase 1)` annotations:
   - (a) windowed counts: last_hour / last_24h / since_cutover for `publication_status='partial'`.
   - (b) per-row deep-dive selecting `id, canonical_key, headline, state, publication_status, publication_error, publication_attempts, last_published_at, discord_message_ids` ordered by `last_published_at desc` limit 50.
   - (c) still-pending-too-long: `publication_status='pending' AND (last_published_at IS NULL OR last_published_at < now() - interval '90 minutes')`.
   - (d) rejection-rate against `topic_transitions WHERE action LIKE 'rejected_%'` last 1h, grouped by action.
2. **Replace** the prior cross-window SQL tally with a **two-source trip-wire tally** that does NOT query `topics WHERE environment='dev'` (vacuous by design):
   ```sql
   -- Phase-2 prod partials since cutover (this query is the only SQL half of the trip-wire).
   select count(*) as prod_partials_since_cutover
   from topics
   where environment='prod'
     and publication_status='partial'
     and last_published_at >= '<cutover-flip-timestamp>';
   ```
   And immediately below: "**Add the Phase-1 shadow-attempted-publish partial count from shadow-mode telemetry (trace embed in the Phase-1 monitoring channel + shadow logs per `docs/live-update-editor-redesign.md:611-644`) — not from `topics`. Record both numbers inline:**
   - Phase-1 shadow partials (from telemetry): ____
   - Phase-2 prod partials (from query above): ____
   - **Trip-wire fires when the sum is ≥ 2** per `docs/live-update-editor-redesign.md:600-601`: ABORT + restore `topic_publications`."
3. **Annotate**: "See `docs/live-update-editor-redesign.md:117-123`, `:223` for column shapes."

### Step 8: Draft section 7 — 24-48h watch decision tree (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** SYMPTOM | THRESHOLD | ACTION | POINTER table:
   - `publication_status='partial'`: 1 in cutover window → INVESTIGATE; **(Phase-1 shadow partials from telemetry + Phase-2 prod partials from §6) cumulative ≥ 2 (per `docs/live-update-editor-redesign.md:600-601`) → ABORT** + restore `topic_publications` + §8.
   - Rejection rate >50%/hr (§6(d)) → TUNE / ABORT — consult redesign.
   - Collision-override rate >10% → TUNE per `:603`.
   - Duplicate posts to live channel: 1 isolated → HOT-FIX (check `_publish_accepted_candidates` idempotency / `topic_sources` uniqueness per `:625`); recurring → ABORT.
   - Legacy editor accidentally publishes to live channel → HOT-FIX: re-verify toggle + branch-A/B signal from §5.
   - Discord 429s / auth errors from new editor → HOT-FIX routing.
   - **Lease symptoms (split per `:684`)**: new-editor self-contention → HOT-FIX release lease for `(environment, guild_id, live_channel_id)` + verify next tick; legacy editor writing to NEW tables (`topics`, `topic_*`) → ABORT (isolation broke); new + legacy run rows overlapping in same lease key → ABORT (lease misconfigured).
   - Shape divergence vs. Phase 1 baseline: explainable → ACCEPT; unexplained → TUNE / ABORT.
   - `record_observation` <1/run sustained → INFO (post-cutover concern per `:602`).
   - **Phase 1 turns out to be a third mechanism (not A or B)** → ABORT immediately; the runbook does not cover it.
2. **End**: "If symptom not listed, ABORT to rollback and consult `docs/live-update-editor-redesign.md`."

### Step 9: Draft section 8 — Rollback procedure (runner-pointer flip) (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Write** pre-rename rollback (common case during watch):
   1. Flip `<LIVE_EDITOR_PUBLISH_TOGGLE>` to its legacy value.
   2. Drain new-editor in-flight run OR force lease release for `(environment, guild_id, live_channel_id)` (`:684`).
   3. Wait for next scheduler tick. Confirm legacy editor publishes via `_publish_accepted_candidates` (`src/features/summarising/live_update_editor.py:84-107`):
      - Branch A: a new legacy `live_update_editor_runs` row appears under the legacy run-source.
      - Branch B: existing legacy run rows now show the suppression marker CLEARED (if Phase 1 writes one), AND a new live-channel post is attributed to the legacy bot identity (primary Discord-side signal).
   4. Verify a new row in `live_update_feed_items` for the prod run AND a corresponding Discord post.
   5. Leave `topics` rows alone — do not truncate or rewrite. Cite `:596`.
2. **Write** post-rename rollback variant (rare): same toggle flip + reverse-rename SQL (mirror of §9, six tables back to original names), then re-run pre-rename verification.
3. **State** explicitly: "Run §9 rename only after the 24-48h watch passes. This keeps rollback to a single toggle flip during the watch window."

### Step 10: Draft section 9 — Table-rename migration SQL (post-watch) (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Medium
1. **Name** the migration-runner path explicitly:
   - **Author** the migration as a new timestamped file in `.migrations_staging/` (e.g. `2026MMDDHHMMSS_live_update_legacy_rename.sql`), matching the convention used by `20260511110349_live_update_environment_split.sql` and `20260512185328_live_update_watchlist_lifecycle.sql`.
   - **Hand off** per `.migrations_staging/README.md`: copy the file VERBATIM (filename unchanged so ordering is preserved) into `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/`.
   - **Apply** via `supabase db push` from the workspace repo (or whichever apply mechanism Phase 1 uses for prod — `TODO(phase-1)` if Phase 1 chose a different applier such as the Supabase dashboard SQL editor or psql-direct against prod; the SQL body is identical regardless).
   - **Verify** the migration recorded in `supabase_migrations.schema_migrations` (Supabase's tracking table) before treating the rename as applied.
2. **Write** the ordered `ALTER TABLE ... RENAME TO` block in a `begin; ... commit;` transaction for the six legacy tables per `docs/live-update-editor-redesign.md:212-223`: `live_update_feed_items`, `live_update_watchlist`, `live_update_duplicate_state`, `live_update_editorial_memory`, `live_update_candidates`, `live_update_decisions` → `_legacy_<name>`. Inline comment that `live_update_editor_runs` stays (`:223`, `:562`) and that swapping `commit;` for `rollback;` gives a dry-run preview.
3. **Add** constraint-preservation note: Postgres preserves indexes, sequences, FK constraints, and RLS across `RENAME TO`; do not recreate. Dependent views are NOT auto-updated — pre-check `select schemaname, viewname from pg_views where definition ilike '%live_update_%';` and recreate or drop matching views explicitly.
4. **Add** row-count parity SELECT before AND after (`union all` over six tables); counts must match exactly.
5. **Add** "Record `_legacy_*` deletion date inline: __________ (operator picks 2-4 weeks from today, per `:653`)." Do not hardcode a date.
6. **Cross-check** the six tables against the authoritative sources: `docs/live-update-editor-redesign.md:212-223` and `src/common/storage_handler.py:652-1968`. NOT `docs/live-update-editor-legacy-boundaries.md` (which is about `daily_summaries`).

### Step 11: Draft section 10 — Deletion schedule + close-out (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** "Before deletion date fires" checklist: no recent reads from `_legacy_*`, no open incidents referencing legacy data, POM explicit sign-off.
2. **Write** the `DROP TABLE _legacy_<name>;` block for all six tables in a single transaction, authored via the same `.migrations_staging/` → `supabase/migrations/` → `supabase db push` hand-off as §9.
3. **Add** Bring-forward circumstances (storage pressure, schema-drift complications, audit done early) and Push-out circumstances (active forensics, regulator request, ongoing post-cutover partial-publish investigation).
4. **End** with close-out checklist: legacy dropped, runbook archived, redesign doc updated to note Phase 2 done.

### Step 12: Draft section 11 — Consult-the-doc escape hatch footer (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Write** short footer: every section that could fork on Phase-1 deviation defaults to ABORT-to-rollback. Re-list the named redesign anchors from Step 1.
2. **Re-list** the three trip-wire signals from `:599-604` — partial-publish ×2 cross-window (Phase-1 telemetry + Phase-2 `topics`) → ABORT + restore `topic_publications`; override rate >10% → TUNE; `record_observation` <1/run → drop tool (post-cutover concern).

### Step 13: Self-review pass against test expectations and v3 critique flags (`docs/live-update-editor-phase-2-runbook.md`)
**Scope:** Small
1. **Walk** the eight `pass_to_pass` expectations; confirm each maps to a section.
2. **Verify** §6 trip-wire tally does NOT query `topics WHERE environment='dev'`; it queries prod-only and instructs the operator to add the Phase-1 shadow count from telemetry. Annotation in §2/§3/§4 reminds the operator that dev `topics` has no `partial` rows by design.
3. **Verify** §5 Branch B names Discord-side count as the PRIMARY signal and the SQL suppression marker as nice-to-have.
4. **Verify** §1 banner lists five `TODO(phase-1)` items (including bot identity) and contains an explicit out-of-scope clause for any third Phase-1 mechanism.
5. **Verify** §3 preconditions includes "Phase 1 shipped Branch A or Branch B (not a third mechanism)" as an explicit gate.
6. **Verify** §7 decision tree includes a "Phase 1 turns out to be a third mechanism" row → ABORT.
7. **Verify** §9 step 1 names the full migration hand-off chain: `.migrations_staging/` author → `supabase/migrations/` apply destination (`/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/`) → `supabase db push` → verify in `supabase_migrations.schema_migrations`.
8. **Verify** §10 step 2 uses the same hand-off chain for the `DROP TABLE` block.
9. **Verify** §5 verification anchors on the next observed `live_update_editor_runs` row, not a fixed time window.
10. **Verify** §7 partial-publish threshold is "(Phase-1 telemetry) + (Phase-2 prod) cumulative ≥ 2" with `:600-601` citation.
11. **Verify** §7 lease symptoms split into three distinct rows.
12. **Verify** §9 cross-checks against `docs/live-update-editor-redesign.md:212-223` and `src/common/storage_handler.py:652-1968` — NOT `docs/live-update-editor-legacy-boundaries.md`.
13. **Verify** SQL is `environment='prod'` with `-- swap to 'dev'` comments where applicable and the "dev has no partials" annotation where it would mislead.
14. **Verify** length ~400 lines, schema referenced (not duplicated), `_legacy_*` deletion date blank, every `TODO(phase-1)` visibly labeled.

## Execution Order
1. §1-2 (header + provisional banner with five TODOs and the third-mechanism abort clause; Fast Triage SQL).
2. §3-5 (preconditions, rubric with Phase-1-telemetry-sourced partial count, flip with Branch A/B verification and Discord-primary Branch B).
3. §6-7 (detection SQL with prod-only trip-wire query + telemetry-sum instruction, decision tree with split lease symptoms + third-mechanism row).
4. §8 (rollback) — before §9.
5. §9-10 (rename with full `.migrations_staging/` → `supabase/migrations/` → `supabase db push` hand-off; deletion with same chain).
6. §11 + self-review pass last.

## Validation Order
1. Self-review pass (Step 13) against the eight `pass_to_pass` criteria and the four v3-residual flags resolved in this revision.
2. Anchor verification — every `docs/live-update-editor-redesign.md:LINE-LINE` and `src/...:LINE` reference resolves.
3. No code edits, no tests. Doc-only change.
