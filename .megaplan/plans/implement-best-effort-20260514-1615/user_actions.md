# User Actions

## After Execute

- **U1**: Apply .migrations_staging/20260514160000_external_media_cache.sql to production Supabase before deploying code that reads/writes the external_media_cache table. Run: psql or Supabase dashboard SQL editor to execute the migration.
  Rationale: The migration creates the external_media_cache table. Code in T3 that reads/writes this table will fail at runtime if the table does not exist. This is a deployment follow-up, not a blocker for local implementation.
- **U2**: Configure EXTERNAL_MEDIA_CACHE_DIR in production environment (default is a local temp directory). Set to a durable path with adequate disk space for cached media downloads. Also configure EXTERNAL_MEDIA_MAX_BYTES if the default (aligned with Discord bot upload limit, ~25MB) is not appropriate. Apply via .env or deployment config.
  Rationale: The resolver stores downloaded files under the cache directory. Without a durable path, cached files are lost on restart. The byte cap should be tuned to the specific Discord bot's upload limits. This is a deployment follow-up.
- **U3**: After code and schema are deployed to production, run a manual smoke test: publish a structured topic containing a Reddit or X external media ref and verify either an attached file or fallback link appears in the correct block position on Discord.
  Rationale: This is the only way to validate end-to-end behavior with real yt-dlp and Discord. Requires physical access to the production Discord server.
