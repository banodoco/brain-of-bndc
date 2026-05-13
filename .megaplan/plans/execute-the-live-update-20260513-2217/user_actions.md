# User Actions

## Before Execute

- **U1**: Provide or confirm Supabase CLI authentication, project link details, and authorization to apply migrations to dev and production if the executor's environment is not already linked.
  Rationale: The executor can write migration SQL, but applying it to real Supabase projects may require human-owned credentials or explicit environment access.
- **U2**: Provide the production Discord trace target channel, bot identity expectations, and final environment variable names/values for publishing control and rollback backend selection.
  Rationale: The code can support defaults, but production channel and deploy-time toggle values are operator-owned decisions.

## After Execute

- **U3**: Approve and perform the production deploy plus final publishing flip after repo validation, schema application, backfill parity, checkpoint mirror, admin/health visibility, and publishing-off replay are complete.
  Rationale: Production deployment and enabling live Discord publishing are operational actions outside normal repo editing.
- **U4**: Manually inspect the first production trace and Discord-side post attribution after publishing is enabled, then decide whether to continue or roll back using the documented legacy backend and checkpoint mirror procedure.
  Rationale: This is an out-of-band production smoke check that requires access to Discord and operational judgment.
