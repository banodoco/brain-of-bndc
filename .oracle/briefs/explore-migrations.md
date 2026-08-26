Explore area: Supabase schema/migration idempotency, RLS, indexes

Report verified facts with file/line evidence, unknowns, risks, and a suggested approach aligned with the North Star and bounded by the agent goal. Ranked findings, <300 words.

Focus: .migrations_staging/*support*.sql, src/common/db_handler.py, src/features/support/support_cog.py persistence calls. Check: migration idempotency (IF NOT EXISTS), RLS policies, indexes on thread_id, guidance_version column addition.
