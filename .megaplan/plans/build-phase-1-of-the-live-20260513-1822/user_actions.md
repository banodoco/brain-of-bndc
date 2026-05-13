# User Actions

## Before Execute

- **U1**: Run the defer-audit SQL against the live live_update_candidates table for the last 30 days (count of `decision='defer'` rows grouped by environment + guild_id, plus a sample of POM's post-hoc treatment of those rows). Provide the numerical result + qualitative read to the executor BEFORE T1 finalizes. The executor needs the number to decide the pending_review conditional branch: ≥5/week acted-upon defers → ship state='pending_review' + request_review_topic in Day-1; 3-5/week borderline → default to v2 unless you explicitly say otherwise; <3/week → defer to v2.
  Rationale: Phase 0 locked decision #5 cannot be resolved without a live-DB query, and the migration DDL + tool surface depend on the outcome.
- **U2**: Generate tests/fixtures/similarity_pairs.json by running `SELECT similarity(a, b)` on a Postgres instance with the pg_trgm extension (dev Supabase or local Postgres) for ~30 (headline_a, headline_b, expected_pg_trgm_similarity, expected_match_at_0_55) rows drawn from tests/fixtures/historical_headlines.json. Include both 'should collide' and 'should NOT collide' pairs. Hand the file to the executor before T3 runs. This is a one-time external dependency; once committed, CI is hermetic.
  Rationale: T3's pinned-oracle similarity test asserts against real pg_trgm output. Without this fixture the threshold-tuning test cannot run.

## After Execute

- **U3**: Set DEV_SHADOW_TRACE_CHANNEL_ID in the dev environment (.env or equivalent) to the Discord channel ID for #main_test. Unset is acceptable for unit tests (trace embeds become log-only) but T13's manual replay run will not post any embeds without it.
  Rationale: Required for the manual 20-window replay against #main_test; unit tests do not require it.
- **U4**: Apply the new migration .migrations_staging/20260513XXXXXX_topic_editor_phase1.sql to dev Supabase (manual handoff to the workspace Supabase repo, per existing .migrations_staging/ convention). Then run scripts/backfill_topics_from_feed_items.py --dry-run to preview, then without --dry-run to populate. Tests use the in-memory fake store and do NOT require this, but the manual 20-window replay run does.
  Rationale: Migration application is a manual handoff to a separate Supabase repo per current workflow.
- **U5**: Run `python scripts/run_live_update_shadow.py --archive-env prod --topic-env dev --windows 20`. Review the 20 per-window trace embeds + aggregate summary in #main_test. Apply Week-1 decision-gate judgment: are divergences vs today's editor explainable as 'valid different call' rather than drift? Override rate within bounds (<~10%)? POM answers 'would I be comfortable letting this publish to prod?' STOP HERE — Phase 2 is a separate decision.
  Rationale: Subjective Week-1 decision-gate review is human-only by design.
