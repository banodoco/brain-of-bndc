# Live Update Editor Legacy Boundaries

The daily-summary sender has been removed. Current overview publishing is owned
by the hourly topic-editor loop and the `!summarynow` owner command, both of
which run `TopicEditor`.

## Removed Runtime Surface

- `src/features/summarising/summariser.py` and its daily-summary subfeatures
  were deleted.
- `main.py` no longer imports or constructs the daily summarizer.
- `--legacy-summary-now`, `--combine-only`, and `--clear-today-summaries` were
  removed.
- `SummarizerCog` no longer has a legacy startup branch or
  `!legacysummarynow` command.
- `src/common/db_handler.py` and `src/common/storage_handler.py` no longer
  expose write/update helpers for `daily_summaries`.

## Remaining Historical Surface

- `daily_summaries` stays queryable as historical archive data.
- `DatabaseHandler.get_summary_for_date()` and
  `StorageHandler.get_summary_for_date()` remain read-only compatibility
  helpers for scripts that inspect old records.
- Admin/debug tools may read `daily_summaries`, but active overview questions
  should use topic-editor state: `topic_editor_runs`, `topics`, and
  `topic_transitions`.

## Active Overview Runtime

- Scheduled publishing: `SummarizerCog.run_live_pass()` -> `TopicEditor`.
- Startup publishing: `main.py --summary-now` -> archive if requested ->
  `TopicEditor.run_once(trigger="startup_summary_now")`.
- Manual publishing: `!summarynow` -> `TopicEditor.run_once()`.

No active runtime path should generate, post, write, or mutate daily summaries.
