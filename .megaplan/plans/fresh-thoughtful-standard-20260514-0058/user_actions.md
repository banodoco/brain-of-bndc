# User Actions

## Before Execute

- **U1**: Verify OPENAI_API_KEY and GEMINI_API_KEY are set in .env (already present per brief).
  Rationale: Tests stub SDKs so keys are not strictly required for tests, but the real runtime needs them.

## After Execute

- **U2**: Run the SQL migration against the Supabase database (or verify it auto-applies if the project has migration automation).
  Rationale: The migration files are created in-repo but must be applied to the live database.
