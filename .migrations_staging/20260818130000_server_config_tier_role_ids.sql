-- Per-guild tier role ids for the three-tier member model.
-- The honeypot and speaker-management resolution read newbie_role_id /
-- moderated_role_id from server_config (with env as fallback); the columns
-- were missing, so every non-BNDC guild fell back to BNDC's env role ids,
-- which never resolve in the other guild (guild.get_role -> None) and the
-- honeypot silently never fired there.
-- Idempotent: safe to replay in production.

alter table public.server_config
    add column if not exists newbie_role_id bigint;

alter table public.server_config
    add column if not exists moderated_role_id bigint;
