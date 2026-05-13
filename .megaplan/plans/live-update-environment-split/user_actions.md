# User Actions

## Before Execute

- **U2**: Set DEV_SUMMARY_CHANNEL_ID and DEV_LIVE_UPDATE_CHANNEL_ID env vars if not already present. DEV_TOP_GENS_CHANNEL_ID is no longer needed after this change.
  Rationale: The dev runner script reads these env vars for channel resolution. They should exist before running the dev script.

## After Execute

- **U1**: Run the migration SQL (from T1) against the Supabase database. This is a schema change requiring human execution against the production database.
  Rationale: The migration must be applied to the live DB for tests and runtime to work. However, T2-T9 can proceed against a local Supabase instance or test environment. The final validation (criterion 16 in plan: `scripts/run_live_update_dev.py --once` against real Supabase) requires the migration to be live.
- **U3**: After all changes deployed: run `scripts/run_live_update_dev.py --once` against the real Supabase to confirm dev rows are tagged `environment='dev'` and a subsequent prod-mode pass does not surface them.
  Rationale: This is the final acceptance criterion for the features. Requires the migration to be live on the real DB.
