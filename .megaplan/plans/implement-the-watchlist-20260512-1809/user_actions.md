# User Actions

## After Execute

- **U1**: Run the migration from `.migrations_staging/<ts>_live_update_watchlist_lifecycle.sql` against the target Supabase database (both dev and prod environments). Verify the new columns exist and backfill completed correctly.
  Rationale: Migration must be applied before watchlist lifecycle tests can run against a real or representative DB schema. This is a DDL change that requires database credentials and direct Supabase access.
- **U2**: If a safety-belt throttle is desired during the first week, set `LIVE_UPDATE_MAX_POSTS_PER_RUN` env var (e.g., to 3) in the deployment environment. Default is unlimited (None).
  Rationale: Optional operational lever; not required for correctness. The code supports this env var but defaults to unlimited.
