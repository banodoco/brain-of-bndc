# Live Update Editor Legacy Boundaries

This audit classifies the current daily-summary runtime surface before the
agentic live-update editor replaces it. Until the live editor is wired in, these
paths remain operationally unchanged; later migration work should treat them as
legacy/backfill entry points, not as active live-flow storage or publishing.

## Runtime Touch Points

- `src/features/summarising/summariser_cog.py`
  - `SummarizerCog.run_daily_summary()` starts the current 07:00 UTC daily
    summary loop and directly calls `ChannelSummarizer.generate_summary()`.
    This is the default scheduled runtime boundary to replace with the live
    editor scheduler.
  - `SummarizerCog.on_ready()` handles startup `--summary-now` and optional
    archive-first behavior, then calls `generate_summary()`. This is the
    startup/manual legacy summary path to replace with one live-editor pass.
  - `SummarizerCog.summary_now_command()` exposes owner command `!summarynow`
    and directly calls `generate_summary()`. This command should trigger one
    live-editor pass after replacement, while legacy/backfill access should be
    explicit.

- `main.py`
  - CLI flags `--summary-now`, `--summary-with-archive`, `--archive-days`,
    `--combine-only`, and `--clear-today-summaries` are the startup gates for
    the current daily-summary flow. `--archive-days` is also consumed by
    `ArchiveCog` when used standalone.
  - `bot.summary_completed` currently blocks hourly archive fetch until
    startup `--summary-now` completes. The live editor must preserve archive
    ingestion ordering without treating daily summaries as active storage.
  - `--combine-only` and `--clear-today-summaries` are legacy/backfill controls
    coupled to `daily_summaries`.

- `src/features/archive/archive_cog.py`
  - Standalone `--archive-days` handling runs without summary generation.
    Archive-first behavior with `--summary-now` is currently handled by
    `SummarizerCog.on_ready()` and should move with the live-editor startup
    pass.

- `src/features/summarising/summariser.py`
  - `schedule_daily_summary()` is an older helper that calls
    `bot.generate_summary()` and is not loaded by `main.py`; keep it legacy only
    or remove it when runtime wiring is replaced.
  - `ChannelSummarizer.generate_summary()` is the monolithic legacy/backfill
    daily batch. It creates per-channel summaries, combines a main summary,
    posts summary threads/messages, writes `daily_summaries`, and currently
    runs top-creations behavior at the tail. New live-editor work should reuse
    only focused helpers and should not copy this batch pipeline.

## Legacy Storage And Helpers

- `src/common/storage_handler.py` and `src/common/db_handler.py` contain
  `daily_summaries` helpers:
  `get_summary_for_date()`, `summary_exists_for_date()`,
  `store_daily_summary_to_supabase()` / `store_daily_summary()`,
  `mark_summaries_included_in_main()`, and
  `update_channel_summary_full_summary()`.
- These helpers are legacy/backfill storage surfaces for historical daily
  summaries. Active live-editor runs, candidates, decisions, feed items,
  duplicate state, editorial memory, watchlist state, and checkpoints must use
  new live-editor tables.
- Reusable non-summary helpers remain valid for future batches, especially
  Discord channel lookup/send patterns, media download/upload helpers, and
  archive/persisted-message reads.

## Direct Callers Found

- `SummarizerCog.run_daily_summary()` -> `ChannelSummarizer.generate_summary()`
- `SummarizerCog.on_ready()` for `--summary-now` -> `generate_summary()`
- `SummarizerCog.summary_now_command()` -> `generate_summary()`
- `schedule_daily_summary()` -> `bot.generate_summary()` legacy unused helper
- Internal daily batch writes through `DatabaseHandler.store_daily_summary()`

No active live-update path should write to `daily_summaries`; it is legacy
history and migration/backfill input only.
