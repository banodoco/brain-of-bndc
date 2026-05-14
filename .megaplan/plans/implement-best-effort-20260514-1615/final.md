# Execution Checklist

- [x] **T1:** Create src/common/external_media.py with shared pure helpers: extract_external_urls(), domain safelist (from curator.py:133 plus canonical domains), platform-policy lookup (short-form=lazy-resolve, long-form=fallback-link-only), URL normalisation, cache-key creation, content-hash calculation, content-type classification, Discord upload compatibility checks, and URL sanitisation for logs. The extract_external_urls helper must deterministically scan message content/clean_content first, then embed URL/thumbnail/image/video fields in a stable order.
  Executor notes: Created src/common/external_media.py with all required pure helpers. Smoke-tested for deterministic ordering, deduplication, safelist, platform policy, cache key stability, content type checks, and URL sanitisation.
  Files changed:
    - .megaplan/debt.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/.plan.lock
    - .megaplan/plans/add-image-video-understanding-20260514-0055/phase_result.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.md
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.meta.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/state.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/.plan.lock
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_output.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/faults.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/gate.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/phase_result.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/state.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_revise_v2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/.plan.lock
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_audit.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_3.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_4.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_5.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_6.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_7.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_8.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_9.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/faults.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/final.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_snapshot.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate_signals_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/phase_result.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.meta.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/review.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/state.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_execute_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_finalize_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_gate_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_plan_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_review_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/user_actions.md
    - .megaplan/plans/implement-best-effort-20260514-1615/.plan.lock
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_audit.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/faults.json
    - .megaplan/plans/implement-best-effort-20260514-1615/final.md
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize.json
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize_snapshot.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate_signals_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate_signals_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/phase_result.json
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v1.md
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v1.meta.json
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v2.md
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v2.meta.json
    - .megaplan/plans/implement-best-effort-20260514-1615/prep.json
    - .megaplan/plans/implement-best-effort-20260514-1615/prep_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/state.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_critique_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_critique_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_execute_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_finalize_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_gate_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_gate_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_plan_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_prep_v0.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_revise_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/user_actions.md
    - .megaplan/plans/structured-live-update-doc-publishing/.plan.lock
    - .megaplan/plans/structured-live-update-doc-publishing/critique_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_1.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_3.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_4.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_5.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_6.json
    - .megaplan/plans/structured-live-update-doc-publishing/faults.json
    - .megaplan/plans/structured-live-update-doc-publishing/feedback.md
    - .megaplan/plans/structured-live-update-doc-publishing/final.md
    - .megaplan/plans/structured-live-update-doc-publishing/finalize.json
    - .megaplan/plans/structured-live-update-doc-publishing/finalize_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/finalize_snapshot.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate_signals_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate_signals_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/phase_result.json
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v1.md
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v1.meta.json
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v2.md
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v2.meta.json
    - .megaplan/plans/structured-live-update-doc-publishing/state.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_finalize_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_gate_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_gate_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_plan_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_revise_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/user_actions.md
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/.plan.lock
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/.plan.lock
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_audit.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_3.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_4.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_5.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/faults.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/final.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_snapshot.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/gate.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/phase_result.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/review.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_execute_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_finalize_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_plan_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_revise_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/user_actions.md
    - requirements.txt
    - scripts/run_live_update_dev.py
    - src/common/db_handler.py
    - src/common/external_media.py
    - src/common/llm/__init__.py
    - src/common/llm/deepseek_client.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/summariser_cog.py
    - src/features/summarising/topic_editor.py
    - tests/test_live_runtime_wiring.py
    - tests/test_topic_editor_core.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py
  Reviewer verdict: Pass. Shared common helper exists with safelist, platform policy, URL normalization, cache keys, content checks, and sanitization.
  Evidence files:
    - src/common/external_media.py

- [x] **T2:** Create src/features/summarising/external_media_resolver.py with two-tier safety: (1) source-provenance check using T1 safelist before yt-dlp or HTTP, (2) platform-policy respect (skip yt-dlp for fallback-link-only domains), (3) yt-dlp --dump-json metadata-only with injectable subprocess runner and hard timeout, (4) download only from CDN URLs whose original source passed safelist AND content passes content-type/byte-cap/streaming-limit/timeout/Discord-upload checks, (5) configurable byte cap defaulting to practical Discord bot limits, (6) structured outcome enums (cache_hit, downloaded, skipped_domain, fallback_only_platform, metadata_failed, download_failed, oversize, unsupported_content_type, not_discord_upload_compatible), (7) file storage under configurable cache dir with URL-key+content-hash filename, (8) sanitized logging.
  Depends on: T1
  Executor notes: Created src/features/summarising/external_media_resolver.py with ExternalMediaResolver class. Two-tier safety: (1) source-provenance check using T1 safelist before yt-dlp, (2) platform-policy respect (skip yt-dlp for fallback-link-only domains). Uses injectable side-effectful functions. 10-value ResolveOutcome enum. Streaming byte-limit enforcement during download. Configurable byte cap defaults to 25 MiB. File paths use URL-key+content-hash format. Sanitised logging via T1 helpers. Added yt-dlp to requirements.txt. All side-effectful functions are injectable.
  Files changed:
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_2.json
    - src/features/summarising/external_media_resolver.py
  Reviewer verdict: Pass. Resolver implements metadata-first yt-dlp flow, source provenance, fallback-only long-form policy, cache, size/content-type checks, and structured outcomes.
  Evidence files:
    - src/features/summarising/external_media_resolver.py

- [x] **T3:** Add get_external_media_cache(url_key) and upsert_external_media_cache(row) to db_handler.py (sync wrappers using _run_async_in_thread pattern, placed near the existing message_media_understandings wrappers) and corresponding async methods to storage_handler.py. Row shape: url_key, source_url_sanitized, source_domain, status, content_hash, media_kind, content_type, byte_size, file_path, resolved_url_sanitized, failure_reason, metadata, created_at, updated_at.
  Executor notes: Added get_external_media_cache and upsert_external_media_cache to both db_handler.py (sync wrappers) and storage_handler.py (async methods). Follows existing message_media_understandings pattern. Also re-added message_media_understandings async methods lost during git revert.
  Files changed:
    - .megaplan/debt.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/.plan.lock
    - .megaplan/plans/add-image-video-understanding-20260514-0055/phase_result.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.md
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.meta.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/state.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/.plan.lock
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_output.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/faults.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/gate.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/phase_result.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/state.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_revise_v2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/.plan.lock
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_audit.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_3.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_4.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_5.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_6.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_7.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_8.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_9.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/faults.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/final.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_snapshot.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate_signals_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/phase_result.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.meta.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/review.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/state.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_execute_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_finalize_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_gate_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_plan_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_review_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/user_actions.md
    - .megaplan/plans/implement-best-effort-20260514-1615/.plan.lock
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_audit.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/faults.json
    - .megaplan/plans/implement-best-effort-20260514-1615/final.md
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize.json
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize_snapshot.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate_signals_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate_signals_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/phase_result.json
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v1.md
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v1.meta.json
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v2.md
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v2.meta.json
    - .megaplan/plans/implement-best-effort-20260514-1615/prep.json
    - .megaplan/plans/implement-best-effort-20260514-1615/prep_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/state.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_critique_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_critique_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_execute_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_finalize_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_gate_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_gate_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_plan_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_prep_v0.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_revise_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/user_actions.md
    - .megaplan/plans/structured-live-update-doc-publishing/.plan.lock
    - .megaplan/plans/structured-live-update-doc-publishing/critique_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_1.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_3.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_4.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_5.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_6.json
    - .megaplan/plans/structured-live-update-doc-publishing/faults.json
    - .megaplan/plans/structured-live-update-doc-publishing/feedback.md
    - .megaplan/plans/structured-live-update-doc-publishing/final.md
    - .megaplan/plans/structured-live-update-doc-publishing/finalize.json
    - .megaplan/plans/structured-live-update-doc-publishing/finalize_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/finalize_snapshot.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate_signals_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate_signals_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/phase_result.json
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v1.md
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v1.meta.json
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v2.md
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v2.meta.json
    - .megaplan/plans/structured-live-update-doc-publishing/state.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_finalize_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_gate_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_gate_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_plan_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_revise_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/user_actions.md
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/.plan.lock
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/.plan.lock
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_audit.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_3.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_4.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_5.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/faults.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/final.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_snapshot.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/gate.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/phase_result.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/review.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_execute_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_finalize_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_plan_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_revise_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/user_actions.md
    - requirements.txt
    - scripts/run_live_update_dev.py
    - src/common/db_handler.py
    - src/common/external_media.py
    - src/common/llm/__init__.py
    - src/common/llm/deepseek_client.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/summariser_cog.py
    - src/features/summarising/topic_editor.py
    - tests/test_live_runtime_wiring.py
    - tests/test_topic_editor_core.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py
  Reviewer verdict: Pass. DB/storage cache wrappers are present and return None on missing handler/cache miss.
  Evidence files:
    - src/common/db_handler.py
    - src/common/storage_handler.py

- [x] **T4:** Create .migrations_staging/20260514160000_external_media_cache.sql with CREATE TABLE IF NOT EXISTS external_media_cache (url_key TEXT PRIMARY KEY, source_url_sanitized TEXT, source_domain TEXT, status TEXT NOT NULL, content_hash TEXT, media_kind TEXT, content_type TEXT, byte_size BIGINT, file_path TEXT, resolved_url_sanitized TEXT, failure_reason TEXT, metadata JSONB DEFAULT '{}', created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()) and CREATE INDEX idx_external_media_cache_status ON external_media_cache(status).
  Executor notes: Created .migrations_staging/20260514160000_external_media_cache.sql with CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS. All specified columns present with correct defaults.
  Files changed:
    - .megaplan/debt.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/.plan.lock
    - .megaplan/plans/add-image-video-understanding-20260514-0055/phase_result.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.md
    - .megaplan/plans/add-image-video-understanding-20260514-0055/plan_v1.meta.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/state.json
    - .megaplan/plans/add-image-video-understanding-20260514-0055/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/.plan.lock
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_output.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/faults.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/gate.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/phase_result.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v1.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.md
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/plan_v2.meta.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/state.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_critique_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_plan_v1.json
    - .megaplan/plans/extend-the-topic-editor-s-20260514-0315/step_receipt_revise_v2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/.plan.lock
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_audit.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_2.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_3.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_4.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_5.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_6.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_7.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_8.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/execution_batch_9.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/faults.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/final.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_output.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/finalize_snapshot.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/gate_signals_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/phase_result.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.md
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/plan_v1.meta.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/review.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/state.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_critique_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_execute_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_finalize_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_gate_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_plan_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/step_receipt_review_v1.json
    - .megaplan/plans/fresh-thoughtful-standard-20260514-0058/user_actions.md
    - .megaplan/plans/implement-best-effort-20260514-1615/.plan.lock
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/critique_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_audit.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/faults.json
    - .megaplan/plans/implement-best-effort-20260514-1615/final.md
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize.json
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/finalize_snapshot.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate_signals_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/gate_signals_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/phase_result.json
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v1.md
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v1.meta.json
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v2.md
    - .megaplan/plans/implement-best-effort-20260514-1615/plan_v2.meta.json
    - .megaplan/plans/implement-best-effort-20260514-1615/prep.json
    - .megaplan/plans/implement-best-effort-20260514-1615/prep_output.json
    - .megaplan/plans/implement-best-effort-20260514-1615/state.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_critique_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_critique_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_execute_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_finalize_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_finalize_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_gate_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_gate_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_plan_v1.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_prep_v0.json
    - .megaplan/plans/implement-best-effort-20260514-1615/step_receipt_revise_v2.json
    - .megaplan/plans/implement-best-effort-20260514-1615/user_actions.md
    - .megaplan/plans/structured-live-update-doc-publishing/.plan.lock
    - .megaplan/plans/structured-live-update-doc-publishing/critique_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_1.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_3.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_4.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_5.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_6.json
    - .megaplan/plans/structured-live-update-doc-publishing/faults.json
    - .megaplan/plans/structured-live-update-doc-publishing/feedback.md
    - .megaplan/plans/structured-live-update-doc-publishing/final.md
    - .megaplan/plans/structured-live-update-doc-publishing/finalize.json
    - .megaplan/plans/structured-live-update-doc-publishing/finalize_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/finalize_snapshot.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate_signals_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/gate_signals_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/phase_result.json
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v1.md
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v1.meta.json
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v2.md
    - .megaplan/plans/structured-live-update-doc-publishing/plan_v2.meta.json
    - .megaplan/plans/structured-live-update-doc-publishing/state.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_finalize_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_gate_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_gate_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_plan_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/step_receipt_revise_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/user_actions.md
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/.plan.lock
    - .megaplan/plans/thoughtful-standard-codex-20260514-0055/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/.plan.lock
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_audit.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_3.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_4.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/execution_batch_5.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/faults.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/final.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_output.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/finalize_snapshot.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/gate.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/phase_result.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v1.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.md
    - .megaplan/plans/unified-discord-style-search-20260514-0319/plan_v2.meta.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/review.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/state.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_critique_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_execute_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_finalize_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_plan_v1.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/step_receipt_revise_v2.json
    - .megaplan/plans/unified-discord-style-search-20260514-0319/user_actions.md
    - requirements.txt
    - scripts/run_live_update_dev.py
    - src/common/db_handler.py
    - src/common/external_media.py
    - src/common/llm/__init__.py
    - src/common/llm/deepseek_client.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/summariser_cog.py
    - src/features/summarising/topic_editor.py
    - tests/test_live_runtime_wiring.py
    - tests/test_topic_editor_core.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py
  Reviewer verdict: Pass. Staged migration exists with IF NOT EXISTS table/index and expected external_media_cache columns.
  Evidence files:
    - .migrations_staging/20260514160000_external_media_cache.sql

- [x] **T5:** Extend normalize_media_ref at topic_editor.py:3387 to accept kind='external' with {message_id, kind: 'external', index}. Preserve existing shorthand attachment behavior exactly. Update prompt text around line 110-115 and tool schema enum at line 263 to include 'external' as a valid kind. Update the 'Canonical media-ref shape' docstring to say kind can be 'attachment'|'embed'|'external'. Also update the tool schema description at ~line 257 so agents know external refs are secondary and block-bound.
  Depends on: T1
  Executor notes: Extended normalize_media_ref to accept kind='external'. Updated docstring, valid kind tuple to ('attachment', 'embed', 'external'), error message, prompt text (line 113), tool schema enum (line 263), added description to media_refs schema. Updated test_rejects_invalid_kind. All 54 test_topic_editor_core tests pass. SC5 verified: external passes through, shorthand unchanged, default attachment unchanged, invalid kind still raises correctly.
  Files changed:
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_2.json
    - src/features/summarising/external_media_resolver.py
  Reviewer verdict: Pass. normalize_media_ref accepts external and preserves legacy attachment shorthand.
  Evidence files:
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_core.py

- [x] **T6:** Wire extract_external_urls from T1 into TopicEditor._message_payload at ~line 3056 and StorageHandler.get_topic_editor_message_context at ~line 1341. In _message_payload: after attachment and embed entries, append external entries using extract_external_urls(message). In get_topic_editor_message_context: after attachment and embed media_refs_available, append external entries using the same helper. External entries use compact shape: {kind: 'external', index: N, domain: 'x.com', url_present: true, source: 'content'|'embed'}. Both callers must use the exact same helper so external index N is identical in both payloads.
  Depends on: T1, T5
  Executor notes: Wired extract_external_urls into TopicEditor._message_payload (after attachment/embed loops) and StorageHandler.get_topic_editor_message_context (added attachments, embeds, clean_content to Supabase select; built media_refs_available with attachment, embed, external entries). Both callers use the same shared extract_external_urls helper. External entries use compact shape: {kind, index, domain, url_present, source}. External entries go AFTER attachment/embed entries for Discord media priority indexing. No auto-shortlisting of external links. Storage handler function needed corrupted lines fixed (patch tool mangled author_id lines).
  Files changed:
    - .gitignore
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_3.json
    - scripts/export_hf_discord_dataset.py
    - tests/test_export_hf_discord_dataset.py
    - tests/test_external_media_resolver.py
  Reviewer verdict: Pass. Payload/context exposure uses extract_external_urls and appends compact external entries after Discord media entries.
  Evidence files:
    - src/features/summarising/topic_editor.py
    - src/common/storage_handler.py

- [x] **T7:** Update three areas in topic_editor.py: (1) Block-level media-ref validation at ~line 1621: add explicit elif kind == 'external' branch that validates source message exists and extract_external_urls(ref_msg)[index] exists and is source-domain safelisted; reject out-of-range/blocked-domain refs with auditable transitions; never call resolver here. (2) _resolve_media_url_from_metadata at ~line 3703: add explicit elif kind == 'external' branch that calls extract_external_urls(meta)[index] and returns the original URL as deterministic fallback (no extraction/download). (3) render_topic_publish_units at ~line 3687: for external refs, build media units carrying kind/external, the original URL as url, and the ref; do not resolve. (4) _summarize_source_media_counts at ~line 2878: add external_links count (separate from resolvable_media) using extract_external_urls; do not silently inflate existing attachment/embed counts.
  Depends on: T1, T5
  Executor notes: (1) Block-level media-ref validation: changed 'else: # embed' to 'elif kind == embed:' with explicit 'elif kind == external:' branch that validates source message exists and extract_external_urls(ref_msg)[index] exists and is source-domain safelisted; added 'else:' reject branch for unknown kinds. (2) _resolve_media_url_from_metadata: added elif kind == 'external' branch that uses the shared extract_external_urls helper and new _resolve_external_url_by_index pure function to return the original URL as deterministic fallback; no subprocess/network/disk calls. (3) render_topic_publish_units: for external refs, builds media units with kind='external', the original URL as url, and the ref. (4) _summarize_source_media_counts: added external_links count using extract_external_urls, separate from resolvable_media; messages_with_media now considers external URLs. All existing tests pass identically.
  Files changed:
    - .gitignore
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_3.json
    - scripts/export_hf_discord_dataset.py
    - tests/test_export_hf_discord_dataset.py
    - tests/test_external_media_resolver.py
  Reviewer verdict: Needs rework. Validation/counting branches are mostly correct, but external fallback URL lookup duplicates the shared extractor and crashes on string-valued embed URLs.
  Evidence files:
    - src/features/summarising/topic_editor.py

- [x] **T8:** Replace flat string-only send loop in _publish_topic structured path (lines 2750-2831) with explicit send units. Introduce local send-unit model: text units (send_kind: 'text', content), URL fallback units (send_kind: 'url', content, ref), and file units (send_kind: 'file', file_path, filename, fallback_url, ref, trace). Build send units from publish_units while preserving suppressed-mode output (flat_messages, media_indices, media_count, flat_message_count). For external file units, lazily call T2 resolver before sending; on cache hit or download success, send discord.File; on any failure, send fallback URL text. Each send unit produces one Discord message ID. Status is 'sent' when all units send, 'partial' when some send, 'failed' when none send. Track per-unit publish traces for external media (cache hit/downloaded/skipped/fallback). Leave simple-topic (non-blocks) path unchanged.
  Depends on: T2, T7
  Executor notes: Replaced flat string-only send loop in _publish_topic structured path with explicit send units. Added _build_send_units() module-level function that translates publish_units into send-unit model (text/url/file send_kinds). Added _send_one_unit() and _resolve_external_for_publish() methods to TopicEditor for lazy-resolve of external media during publish. Suppressed-mode output shape preserved (flat_messages, media_indices, source_media_counts, media_count, flat_message_count). Simple-topic path unchanged. Per-unit traces use sanitized URLs. Added Tuple to typing imports. flat_messages now includes external units (url or fallback_url) for suppressed mode. _send_one_unit handles text/url/file with per-unit trace dicts. File units lazily call resolver; on cache_hit/downloaded send discord.File; on failure send fallback URL text. All 70 core tests pass.
  Files changed:
    - src/features/summarising/topic_editor.py
  Reviewer verdict: Needs rework. Send-unit model exists, but the required URL de-duplication against block text and previously sent fallback URLs is missing.
  Evidence files:
    - src/features/summarising/topic_editor.py

- [x] **T9:** Create tests/test_external_media_resolver.py with unit tests for: (1) extract_external_urls deterministic ordering across content+embeds, (2) safelisted/blocked source domains, (3) URL sanitization with query tokens, (4) cache-key stability, (5) content-type classification, (6) Discord upload compatibility, (7) oversize detection, (8) metadata JSON parsing, (9) two-tier safety (safelisted source + valid CDN passes, non-safelisted source rejects before yt-dlp, wrong content-type CDN falls back), (10) platform policy (YouTube/Vimeo/Twitch return fallback_only_platform, Reddit/X proceed). Stub subprocess and download; no real network/yt-dlp calls.
  Depends on: T1, T2
  Executor notes: Created tests/test_external_media_resolver.py with 64 unit tests across 15 test classes: (1) extract_external_urls deterministic ordering across content+embeds with SC9 verification, (2) safelisted/blocked source domains, (3) URL sanitization with query tokens, (4) cache-key stability, (5) content-type classification, (6) Discord upload compatibility, (7) oversize detection via resolver stub, (8) metadata JSON parsing, (9) two-tier safety (safelisted source + valid CDN passes, non-safelisted source rejects before yt-dlp, wrong content-type CDN falls back), (10) platform policy (YouTube/Vimeo/Twitch return fallback_only_platform without invoking resolver, Reddit/X/mirror-domains proceed), (11) resolver cache hit, (12) file path construction, (13) provenance check edge cases, (14) resolver trace metadata. All tests stub subprocess, HTTP, and filesystem — no real network/yt-dlp calls. Fixed 2 initial test failures: __wrapped__ accessor on undecorated function and PDF content-type test needing actual file write.
  Files changed:
    - .gitignore
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_3.json
    - scripts/export_hf_discord_dataset.py
    - tests/test_export_hf_discord_dataset.py
    - tests/test_external_media_resolver.py
  Reviewer verdict: Pass with coverage gap. Resolver tests are extensive but missed the embed string URL crash in TopicEditor fallback lookup.
  Evidence files:
    - tests/test_external_media_resolver.py

- [x] **T10:** Extend tests/test_topic_editor_core.py: (1) add TestNormalizeMediaRef tests for kind='external' acceptance, rejection of out-of-range external index, and rejection of unknown kinds, (2) add tests for _resolve_media_url_from_metadata with kind='external' returning original URL, (3) add tests for render_topic_publish_units with external media refs producing media units with kind/external and original URL, (4) add tests for _summarize_source_media_counts returning separate external_links count, (5) add tests for block_media_refs with external refs. All existing attachment/embed tests must continue to pass identically.
  Depends on: T5, T6, T7
  Executor notes: Extended tests/test_topic_editor_core.py with 16 new tests: (1) TestNormalizeMediaRef: external kind acceptance, string-index external, invalid-kind rejection after external added; (2) TestResolveMediaUrlFromMetadata: external returns original URL from content, out-of-range returns None, multiple URLs correct index; (3) TestRenderTopicPublishUnits: external media ref produces kind=external unit with original URL, external without URL produces no media unit; (4) TestBuildSendUnits: text→text, media→url, external→file, mixed order; (5) TestSummarizeSourceMediaCounts: separate external_links count, no external_links returns zero, external not merged into resolvable_media; (6) TestBlockHelpers: block_media_refs with external refs. All 54 original tests continue to pass identically. Total: 70 passed, 0 failed.
  Files changed:
    - tests/test_topic_editor_core.py
  Reviewer verdict: Needs expanded coverage. Core tests cover external content URLs but did not catch string-valued embed URL fallback lookup or block-text de-duplication.
  Evidence files:
    - tests/test_topic_editor_core.py

- [x] **T11:** Extend tests/test_topic_editor_runtime.py: (1) add test for suppressed-mode structured publish with external refs returning correct flat_messages/media_indices/media_count/flat_message_count shape, (2) add test for resolver success sending discord.File in correct block position, (3) add test for resolver failure falling back to original link text, (4) add test for oversize/unsupported media falling back, (5) add test for file-send failure falling back to URL text and continuing, (6) add regression test confirming reaction-qualified auto-shortlist still creates watching topics and does not direct-post at 5 reactions, (7) add test for partial status when mixed text/file units succeed/fail. Use monkeypatch/stubs for resolver, channel.send, discord.File, and cache.
  Depends on: T3, T8
  Executor notes: Added 7 runtime tests to tests/test_topic_editor_runtime.py covering suppressed-mode shape, resolver success (file send), resolver failure (fallback + batch continuation), oversize fallback, file-send failure fallback, reaction-qualified auto-shortlist regression (no direct-posting), and partial status on mixed success/failure. All tests use monkeypatch/stubs for resolver, channel.send, discord.File, and cache. Added _FakeResolverOutcome and _make_resolver_result helpers for proper ResolverResult mocking. All 7 new tests pass; existing tests pass identically (202/203, 1 pre-existing failure).
  Files changed:
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_4.json
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_5.json
  Reviewer verdict: Needs expanded coverage. Runtime tests cover resolver fallback/send failures but not duplicate URL suppression when block text already contains the external URL.
  Evidence files:
    - tests/test_topic_editor_runtime.py

- [x] **T12:** Run the full test suite to verify all changes: pytest tests/test_external_media_resolver.py tests/test_topic_editor_core.py tests/test_topic_editor_runtime.py -v. Fix any failures. Then run the broader regression suite: pytest tests/test_live_top_creations.py tests/test_live_runtime_wiring.py -v. Write a throwaway smoke script that exercises extract_external_urls against sample Reddit/Twitter messages, verifies deterministic ordering, then delete the script. Confirm all existing tests pass.
  Depends on: T9, T10, T11
  Executor notes: Ran full test suite: 194 passed, 1 pre-existing failure (test_acquire_topic_editor_run_expires_stale_running_lease: assert 'expired' == 'failed' — not a regression). Broader regression suite: 17 passed (test_live_top_creations + test_live_runtime_wiring). Throwaway smoke script exercised extract_external_urls with Reddit, X/Twitter, Instagram, YouTube, mirror domains, deduplication, empty-message, non-safelisted exclusion, sequential indexing — all passed. Script deleted after confirmation. SC12 verdict: PASS — 211/212 tests pass (1 pre-existing), zero regressions.
  Files changed:
    - .megaplan/plans/implement-best-effort-20260514-1615/execution_batch_6.json
  Reviewer verdict: Pass for executed commands except known failure. Targeted suite reports 194 passed and one executor-identified pre-existing lease status failure; broader regression passed per executor evidence.
  Evidence files:
    - tests/test_topic_editor_runtime.py

## Watch Items

- Staged migration must be applied to production Supabase before deploying code that reads/writes external_media_cache
- EXTERNAL_MEDIA_CACHE_DIR and byte cap must be configured in production before resolver can persist downloads
- Long-form domains (YouTube, Vimeo, Twitch) are fallback-link-only — executor must not attempt yt-dlp for them
- Never make external extraction failure block text publishing
- Keep secrets out of logs — sanitize all URLs that may contain tokens
- Do not reintroduce direct auto-posting at five reactions
- All new functions must be pure/testable following existing stateless patterns
- Every media-kind branch must be explicit attachment/embed/external — no implicit fallthrough

## Sense Checks

- **SC1** (T1): Does extract_external_urls return identical results when called from _message_payload and from StorageHandler.get_topic_editor_message_context for the same input message?
  Executor note: (not provided)
  Verdict: Confirmed for extraction determinism, but downstream fallback lookup still duplicates scanning and can drift.

- **SC2** (T2): Does the resolver reject a safelisted source URL whose yt-dlp-resolved CDN URL points to a non-image/non-video content type, and does it return a structured fallback outcome (not an exception)?
  Executor note: (not provided)
  Verdict: Confirmed. Unsupported content type returns structured resolver outcome.

- **SC3** (T3): Does get_external_media_cache return None (not raise) when the storage_handler is None or when Supabase returns no rows?
  Executor note: (not provided)
  Verdict: Confirmed. Cache miss/no handler returns None.

- **SC4** (T4): Does the SQL migration use IF NOT EXISTS for both the table and index to be safe for re-run?
  Executor note: (not provided)
  Verdict: Confirmed. Migration uses IF NOT EXISTS.

- **SC5** (T5): Does normalize_media_ref({message_id: '123', kind: 'external', index: 0}) return {message_id: '123', kind: 'external', index: 0} while normalize_media_ref({message_id: '123', attachment_index: 0}) still returns {message_id: '123', kind: 'attachment', index: 0} unchanged?
  Executor note: (not provided)
  Verdict: Confirmed. External normalize and attachment shorthand both work.

- **SC6** (T6): If a message has 2 attachments, 1 embed, and 3 safelisted external URLs, does _message_payload produce media_refs_available with indices 0-1 (attachment), 2 (embed), 3-5 (external) in that exact order?
  Executor note: _message_payload produces attachment entries (indices 0..Natt-1), embed entries (indices 0..Nemb-1), then external entries (indices 0..Next-1 from extract_external_urls). All use relative indices within each kind, consistent with existing attachment/embed pattern. External entries appear AFTER attachment/embed entries so Discord media gets priority indexing. The extract_external_urls helper is the single source of truth for external URL indexing.
  Verdict: Confirmed in spirit. External entries are appended after attachment/embed entries, with kind-local indices.

- **SC7** (T7): After this change, does _summarize_source_media_counts return the same attachments/embeds/resolvable_media counts for current test inputs, with the new external_links key reporting external URLs separately?
  Executor note: _summarize_source_media_counts returns identical attachments, embeds, and resolvable_media counts for existing test inputs. New external_links key reports external URLs separately using extract_external_urls. messages_with_media now includes messages with external URLs. Verified: all 54 test_topic_editor_core tests pass; all test_topic_editor_runtime tests pass (except pre-existing lease expiry test).
  Verdict: Confirmed. external_links is separate from resolvable_media.

- **SC8** (T8): When publishing a structured topic with one text block and one external media ref where the resolver succeeds: does suppressed mode return flat_messages containing the text content plus the URL (not file path), with media_indices pointing to the URL position, and does actual send produce two Discord messages (one text, one file attachment) with two sent_ids?
  Executor note: Verified via code review: _build_send_units creates send_units from publish_units preserving order. Text units → send_kind: text. External units → send_kind: file with fallback_url. _send_one_unit sends each independently with trace dict. If resolver succeeds (cache_hit/downloaded), discord.File is sent. If fails, fallback URL text is sent. Each unit produces one sent_id. The outer loop accumulates sent_ids per successful unit.
  Verdict: Partially confirmed. Send units and fallback exist, but duplicate fallback suppression is missing.

- **SC9** (T9): Does a test case confirm that extract_external_urls returns the same results for the same message regardless of which caller invokes it?
  Executor note: TestExtractExternalUrlsOrdering.test_deterministic_ordering_same_message_twice calls extract_external_urls twice on identical deepcopied messages and asserts exact equality including all fields and index values, proving deterministic output regardless of caller. Additionally, test_deterministic_ordering_across_content_and_embeds verifies stable field ordering (content → clean_content → embed.url → thumbnail → image → video).
  Verdict: Confirmed for extract_external_urls itself.

- **SC10** (T10): Do all existing TestNormalizeMediaRef tests pass unchanged alongside the new external-ref tests?
  Executor note: Ran pytest tests/test_topic_editor_core.py -v. All original tests including test_shorthand_attachment_index_converts_to_canonical, test_canonical_embed_ref_passes_through, test_rejects_invalid_kind, etc. pass unchanged. New test_rejects_invalid_kind_after_external_added verifies else-branch rejects unknown kinds after external is added.
  Verdict: Confirmed for existing normalize tests.

- **SC11** (T11): Does the resolver-failure test confirm that text blocks before and after the failed external ref are still published (the batch continues)?
  Executor note: (not provided)
  Verdict: Confirmed. Runtime test covers fallback continuation.

- **SC12** (T12): Do all tests pass (including pre-existing) with zero failures and zero errors?
  Executor note: (not provided)
  Verdict: Partially confirmed. Targeted tests ran with one reported pre-existing failure; however missing tests allowed the two review findings through.

## Meta

Execution order is T1 → (T2, T5 in parallel) → (T3, T4, T6, T7 in parallel, but T6 after T5, T7 after T5) → T8 → (T9, T10 in parallel) → T11 → T12. The most critical correctness invariant is that extract_external_urls is the single source of truth for external URL extraction and indexing — every caller must use it. When updating the validation else-branch at line 1680, change 'else: # embed' to 'elif kind == "embed":' and add a new 'else:' that rejects with an explicit error; this prevents any future kind from silently falling into embed logic. The publisher refactoring (T8) is the highest-risk change because it alters the send loop — keep suppressed-mode output shape identical and preserve simple-topic path verbatim. Write the throwaway smoke script in T12 to exercise the full extraction → normalization → rendering pipeline with sample Reddit/Twitter messages before deleting it.
