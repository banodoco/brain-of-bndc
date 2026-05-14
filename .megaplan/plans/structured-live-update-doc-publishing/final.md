# Execution Checklist

- [x] **T1:** Add pure normalization and media-ref helpers to topic_editor.py: normalize_media_ref, normalize_document_blocks, normalize_topic_document, block_source_ids, block_media_refs, collect_document_source_ids. Define canonical media_ref shape {message_id, kind: 'attachment'|'embed', index}. Accept shorthand {message_id, attachment_index} normalizing to {kind: 'attachment', index}. Wire _normalize_attachment_list and embed URL extraction reuse.
  Executor notes: Added normalize_media_ref, normalize_document_blocks, normalize_topic_document, block_source_ids, block_media_refs, collect_document_source_ids as module-level functions in topic_editor.py (before render_topic). normalize_media_ref validates kind (attachment|embed), accepts shorthand {message_id, attachment_index}, converts to canonical {message_id, kind, index}. normalize_document_blocks handles both new-style blocks and legacy body/sections/source_message_ids. collect_document_source_ids returns distinct union. All unit checks pass.
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
    - .megaplan/plans/structured-live-update-doc-publishing/.plan.lock
    - .megaplan/plans/structured-live-update-doc-publishing/critique_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_1.json
    - .megaplan/plans/structured-live-update-doc-publishing/faults.json
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
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py

- [x] **T2:** Add core tests for normalization helpers in test_topic_editor_core.py: canonical media-ref validation (both shorthand and full forms), embed refs, legacy summary normalization into ordered blocks, collect_document_source_ids union.
  Depends on: T1
  Executor notes: Added 16 tests in 5 test classes to test_topic_editor_core.py. TestNormalizeMediaRef: 10 tests covering shorthand→canonical, canonical pass-through, embed refs, invalid kind, missing message_id, non-dict, non-integer indices. TestNormalizeDocumentBlocks: 8 tests covering legacy body→intro, legacy sections→section blocks, topic-level source fallback, new-style blocks preservation, invalid type skipping, body→text fallback, empty summary. TestNormalizeTopicDocument, TestBlockHelpers, TestCollectDocumentSourceIds: coverage for wrapper, distinct union, order preservation. All pass.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - tests/test_topic_editor_core.py

- [x] **T3:** Add get_topic_editor_source_messages resolver in storage_handler.py and db_handler.py. New method selects message_id, guild_id, channel_id, author_id, author_context_snapshot, content, created_at, reference_id, thread_id, attachments, embeds. Limit=50 (not 10). Used for internal validation, source-author derivation, citation hydration, and media URL hydration. Add synchronous wrapper in DBHandler.
  Executor notes: Added async get_topic_editor_source_messages to StorageHandler (storage_handler.py, after get_topic_editor_message_context) with limit=50. Selects message_id, guild_id, channel_id, author_id, author_context_snapshot, content, created_at, reference_id, thread_id, attachments, embeds. Returns rows in requested ID order. Added synchronous wrapper in DatabaseHandler (db_handler.py) with same limit=50 default.
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
    - .megaplan/plans/structured-live-update-doc-publishing/.plan.lock
    - .megaplan/plans/structured-live-update-doc-publishing/critique_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_1.json
    - .megaplan/plans/structured-live-update-doc-publishing/faults.json
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
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py

- [x] **T4:** Enrich get_topic_editor_message_context in storage_handler.py: add guild_id, attachments, embeds to select; add compact media_refs_available list [{kind, index, url_present, content_type, filename}] to each result. Keep the 10-message cap for agent-facing read tool. Reuse existing _normalize_attachment_list and embed URL extraction.
  Executor notes: Enriched get_topic_editor_message_context in storage_handler.py: added guild_id, attachments, embeds to the select; added compact media_refs_available list [{kind, index, url_present, content_type, filename}] to each result. Attachments indexed with kind='attachment'; embeds indexed with kind='embed'. 10-message cap preserved via safe_limit = max(1, min(int(limit or 10), 10)). url_present computed as bool(url or proxy_url).
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
    - .megaplan/plans/structured-live-update-doc-publishing/.plan.lock
    - .megaplan/plans/structured-live-update-doc-publishing/critique_output.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v1.json
    - .megaplan/plans/structured-live-update-doc-publishing/critique_v2.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_1.json
    - .megaplan/plans/structured-live-update-doc-publishing/faults.json
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
    - src/common/db_handler.py
    - src/common/storage_handler.py
    - src/common/vision_clients.py
    - src/features/summarising/topic_editor.py
    - tests/test_topic_editor_media_understanding.py
    - tests/test_topic_editor_runtime.py
    - tests/test_vision_clients.py

- [x] **T5:** Update post_sectioned_topic tool schema and TOPIC_EDITOR_SYSTEM_PROMPT in topic_editor.py. Add blocks array to input schema with {type, title, text, source_message_ids, media_refs}. Keep body/sections/source_message_ids accepted for backwards compat. Update system prompt: every factual block needs own source_message_ids, media on relevant block, canonical media_refs shape, no global footer, shorthand allowed. Update _summary_for_tool to store blocks alongside body/sections.
  Depends on: T1
  Executor notes: Updated post_sectioned_topic tool schema: added blocks array property with {type, title, text, source_message_ids, media_refs} sub-schema; relaxed required array from [proposed_key, headline, body, sections, source_message_ids] to [proposed_key, headline, body, source_message_ids] (sections no longer required when blocks present). Updated tool description to document structured blocks usage, canonical media-ref shape, shorthand acceptance, no global footer rule. Extended TOPIC_EDITOR_SYSTEM_PROMPT with 'Structured Document Topics' section describing 6 rules: per-block sources, per-block media, canonical shape, no global footer, block types, stable refs. Updated _summary_for_tool to store blocks alongside body/sections when provided.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - tests/test_topic_editor_core.py

- [x] **T6:** Relax post_sectioned_topic guard in _dispatch_create_topic_tool. Change from 'not args.get("sections")' to rejecting only when both sections and blocks are missing or normalize to zero publishable blocks. Add blocks normalization call before source_id derivation.
  Depends on: T1, T3, T5
  Executor notes: Relaxed guard at _dispatch_create_topic_tool. Blocks normalized before source_id derivation. Source IDs now union of args.source_message_ids + collect_document_source_ids(normalized_blocks). Guard checks has_sections and has_blocks, rejecting only when both are false/empty. Reason string: 'post_sectioned_requires_sections_or_blocks'. All tests pass.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_3.json

- [x] **T7:** Add block-level source/validation in _dispatch_create_topic_tool. Normalize blocks → derive topic-level source_ids as distinct union of all block source_ids. Resolve all source IDs against current window messages + archive resolver (get_topic_editor_source_messages). Reject unknown IDs with rejected_post_sectioned reason=unresolved_block_source_message. Validate every media_ref: message_id must resolve, kind must be attachment|embed, index must be in range, URL must be present. Reject invalid refs with unresolved_media_ref or invalid_media_ref.
  Depends on: T1, T3, T6
  Executor notes: Added block-level source/validation in _dispatch_create_topic_tool. Built merged resolved message map from context['messages'] (current window) + self.db.get_topic_editor_source_messages (archive, limit=50). Validated all block source_message_ids resolve against the merged map — rejects unknown IDs with rejected_post_sectioned reason=unresolved_block_source_message. Validated all block media_refs: message_id must resolve, index must be in-range for the resolved message's attachments/embeds, URL must be present. Rejects with unresolved_media_ref (message not found) or invalid_media_ref (index out of range or missing URL). All rejections go through _reject_create_tool → _store_transition producing auditable transition payloads. canonical_key moved earlier in dispatch for use by validation rejections. Rebuilt source_messages/source_authors from merged resolved set. All 129 tests pass (core + runtime + broader + publishing + prompts).
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_4.json

- [x] **T8:** Feed archive-resolved source rows into source_authors and collision detection in _dispatch_create_topic_tool. Replace current _messages_by_id(context['messages'], source_ids) with merged set from current window rows + archive resolver rows. Use merged source rows for source_authors, post_simple_topic single-author/source-count validation, and detect_topic_collisions author-overlap checks. Store topic_sources from the full union.
  Depends on: T3, T6, T7
  Executor notes: Verified T7 already implemented all T8 requirements. Merged resolved_by_id (window + archive) → source_messages → source_authors → collision detection. post_simple_topic validation uses merged source_authors. topic_sources rows iterate over union of args + block source IDs. No code changes needed.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_5.json

- [x] **T9:** Add render_topic_publish_units(topic, source_metadata=None) in topic_editor.py for structured document topics. Returns ordered units [{kind: 'text', content: ...}, {kind: 'media', url: ..., ref: ...}]. Deterministic order: header, intro text with inline linked citations [[1]](jump_url), intro media, each section text with inline citations, that section media. Citations per block deduped and ordered by first appearance. No global Sources footer for structured topics. Keep existing render_topic for legacy simple topics.
  Depends on: T1
  Executor notes: Added render_topic_publish_units(topic, source_metadata=None) function. Returns ordered units [{kind: 'text', content}, {kind: 'media', url, ref}]. Deterministic order: header+intro text with inline [[1]](url) citations → intro media → each section text with inline citations → that section media. Citations per block deduped and ordered by first appearance. Jump URLs use https://discord.com/channels/{guild_id}/{channel_id}/{message_id}. No global Sources footer. Falls back to legacy render_topic when no blocks present. Added _resolve_media_url_from_metadata helper for URL resolution from attachment/embed metadata. Existing render_topic unchanged.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - tests/test_topic_editor_core.py

- [x] **T10:** Add rendering tests in test_topic_editor_core.py. Test: structured section-level sources render inline next to correct intro/section with no global footer. Source jump URLs hydrate from metadata, ordered and deduped per block. Paragraph-aware chunking splits only oversized blocks, normal sections stay as one message each.
  Depends on: T9
  Executor notes: Added 24 tests in 4 classes: TestRenderTopicPublishUnits (9 tests for inline citations, no footer, section isolation, dedup, media ordering, legacy fallback), TestResolveMediaUrlFromMetadata (8 tests for attachment/embed URL resolution with edge cases), TestChunkTextForDiscord (7 tests for paragraph-aware chunking at all 3 strategies). All pass alongside existing tests (54 total).
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_3.json

- [x] **T11:** Rewrite _publish_topic in topic_editor.py for block-by-block publishing. When topic has structured blocks or media_refs, hydrate source/media metadata with get_topic_editor_source_messages. Call render_topic_publish_units. Send one normal text block per Discord message, then each media URL immediately after its owning block. Add paragraph-aware chunking for oversized text blocks: split on blank lines first, then single newlines, then hard-split individual paragraphs. Persist discord_message_ids in actual send order including media URL messages. When publishing disabled, return deterministic would-publish units in publish_results.
  Depends on: T3, T9
  Executor notes: Rewrote _publish_topic with structured block-by-block path. When summary.blocks present: hydrates source metadata, calls render_topic_publish_units, flattens through chunk_text_for_discord. Media URLs sent immediately after owning block. Suppressed mode returns publish_units/flat_messages/media_indices. Added chunk_text_for_discord module-level function. Legacy path preserved unchanged. All 106 tests pass.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_3.json

- [x] **T12:** Add runtime creation and publishing tests in test_topic_editor_runtime.py. Tests: (a) post_sectioned_topic with blocks and no sections accepted and stored; (b) structured topic stores summary.blocks, topic-level union source_message_ids, and one topic_sources row per distinct block source ID; (c) archive-resolved source rows feed source_authors, validation, and collision detection; (d) get_message_context read output includes compact media_refs_available; (e) invalid source IDs and media refs reject with auditable transitions; (f) publishing enabled sends header/intro text, intro media, section text, section media in order; (g) publishing disabled returns deterministic would-publish sequence; (h) existing simple-topic behavior still compatible.
  Depends on: T6, T7, T8, T11
  Executor notes: Added 14 runtime creation and publishing tests in test_topic_editor_runtime.py covering all requirements (a)-(h). Extended FakeDB with get_topic_editor_source_messages method (resolves against source_message_rows) and enriched get_topic_editor_message_context with media_refs_available passthrough. Added three helper factories (_make_editor_with_source_rows, _make_source_context, _sample_source_message_rows, _sample_context_messages). All 49 tests pass (35 existing + 14 new). Broader suite (145 tests) passes with no regressions. Existing test_render_topic_is_pure_and_handles_simple_sectioned_and_story_update still passes.
  Files changed:
    - tests/test_topic_editor_runtime.py

- [x] **T13:** Update _message_payload in topic_editor.py to include compact media_refs_available (matching get_message_context output shape) so current-run source-window messages use the same ref-addressing convention as archive messages. Also include guild_id in the payload for citation link construction.
  Depends on: T4
  Executor notes: Updated _message_payload to include compact media_refs_available (matching get_topic_editor_message_context output shape) with {kind, index, url_present, content_type, filename} for both attachments and embeds. Added guild_id field to payload. Payload remains bounded (no raw URLs in media_refs_available). Embeds indexed with kind='embed'; attachments with kind='attachment'. Existing media_urls field preserved for backwards compatibility.
  Files changed:
    - .megaplan/plans/structured-live-update-doc-publishing/execution_audit.json
    - .megaplan/plans/structured-live-update-doc-publishing/execution_batch_2.json
    - tests/test_topic_editor_core.py

- [ ] **T14:** Run all tests and verify changes work. Commands: pytest tests/test_topic_editor_core.py -q, pytest tests/test_topic_editor_runtime.py -q, pytest tests/test_live_update_editor_publishing.py tests/test_live_update_prompts.py -q, then broader: pytest tests/test_topic_editor_*.py tests/test_live_update_editor_*.py -q. Fix any failures. Write throwaway script exercising blocks-only post_sectioned_topic through FakeDB to confirm end-to-end, then delete it.
  Depends on: T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13

## Watch Items

- 10-message cap in storage_handler.get_topic_editor_message_context (line 1325): must NOT be inherited by the new get_topic_editor_source_messages resolver. The new method has its own limit=50.
- post_sectioned_topic guard at topic_editor.py:1362 currently rejects when sections missing. Must relax to allow blocks-only calls while still rejecting truly empty input.
- topic-level source_message_ids currently stored from args['source_message_ids']. For structured topics, these must be derived as the union of block-level source IDs.
- render_topic simplicity: must not break existing simple-topic rendering used by existing publisher tests at line 1039-1044.
- media_refs canonical shape {message_id, kind, index} must be validated. Shorthand {message_id, attachment_index} must normalize correctly.
- guild_id must be present in source message metadata for Discord jump URL construction. Currently get_message_context does NOT return guild_id.
- topic_sources rows must be written for every distinct block source ID, not just topic-level union. Existing code at line 1413-1419 already writes one per source_id but source_ids currently come from args, not blocks.
- The _summary_for_tool for post_sectioned_topic currently returns {body, sections}. Must also store blocks when provided, preserving body/sections for rollback readability.
- media_refs_available in get_message_context output must be compact (no full URLs). Only {kind, index, url_present, content_type, filename}.
- paragraph-aware chunking: split on blank lines first, then single newlines, then hard-split individual paragraph. Only for blocks exceeding 2000 chars. Normal sections stay one message each.
- publish_results in suppressed mode must still return the deterministic would-publish sequence for trace/debug output.
- Discord jump URL format: https://discord.com/channels/{guild_id}/{channel_id}/{message_id}. Requires guild_id and channel_id in source metadata.
- Citation format per block: [[1]](url), [[2]](url), ..., deduped per block, ordered by first appearance in that block's text.
- Embed refs use {kind: 'embed', index: N} where N is the 0-based index into the message's embeds array. Attachments use {kind: 'attachment', index: N}.
- Legacy simple topics with body/media but no blocks must render exactly as before (no inline citations, global Source footer OK).
- The plan's unresolved correctness flag about the 10-message cap: resolved by creating a separate get_topic_editor_source_messages with limit=50.
- If a structured document exceeds 50 distinct source IDs, explicitly reject with a clear error rather than silently omitting sources.
- Debt watch items from the registry must not be made worse — do not touch live_update_editor, admin-payments, social routes, etc.

## Sense Checks

- **SC1** (T1): Does normalize_media_ref correctly convert shorthand {message_id: '123', attachment_index: 0} to {message_id: '123', kind: 'attachment', index: 0}?
  Executor note: (not provided)

- **SC2** (T1): Does normalize_document_blocks handle legacy summary with body, sections, and topic-level source_message_ids into ordered intro+section blocks?
  Executor note: (not provided)

- **SC3** (T2): Do tests cover both shorthand {message_id, attachment_index} and canonical {message_id, kind, index} media refs?
  Executor note: (not provided)

- **SC4** (T3): Does get_topic_editor_source_messages select guild_id, attachments, and embeds and accept up to 50 message IDs?
  Executor note: (not provided)

- **SC5** (T4): Does get_message_context output include media_refs_available for both attachments and embeds with correct kind and index fields?
  Executor note: (not provided)

- **SC6** (T5): Does the updated tool schema accept blocks input and does the required array allow blocks as alternative to sections?
  Executor note: (not provided)

- **SC7** (T6): Does a post_sectioned_topic call with valid blocks (no sections) pass the guard and store correctly?
  Executor note: The relaxed guard normalizes blocks before check, derives source_ids from blocks union, and rejects only when both sections and blocks are missing. A blocks-only call with valid blocks passes the guard and proceeds to upsert.

- **SC8** (T7): Are invalid source IDs and invalid media refs rejected with auditable transition payloads rather than silently dropped?
  Executor note: (not provided)

- **SC9** (T8): Does a block source from archive lookup (not in context['messages']) contribute its author to collision detection?
  Executor note: (not provided)

- **SC10** (T9): Does render_topic_publish_units produce ordered units with inline citations per block and no global Sources footer?
  Executor note: (not provided)

- **SC11** (T10): Do rendering tests verify inline citations appear next to correct blocks and no global footer exists?
  Executor note: TestRenderTopicPublishUnits.test_no_global_source_footer verifies no 'Source:'/'Sources:' footer. test_section_sources_rendered_inline_next_to_correct_section verifies [[1]](url) citations appear next to correct blocks.

- **SC12** (T11): Does publisher send media URLs immediately after the block that owns each media ref?
  Executor note: test_media_refs_appear_after_block_text and test_section_with_media_sends_media_after_section verify media units (kind='media') immediately follow their owning text block in the publish units array.

- **SC13** (T11): Does paragraph-aware chunking only activate when a single block exceeds 2000 chars?
  Executor note: TestChunkTextForDiscord tests verify chunking only activates when a block exceeds 2000 chars. Short/at-limit text stays as single chunk. Only oversized blocks get split.

- **SC14** (T12): Does the runtime test verify that publishing enabled sends header → intro text → intro media → section text → section media in order?
  Executor note: (not provided)

- **SC15** (T12): Do tests cover both success and rejection paths for blocks with invalid source IDs and invalid media refs?
  Executor note: (not provided)

- **SC16** (T13): Does _message_payload output include media_refs_available with {kind, index, url_present, content_type, filename} for attachments and embeds?
  Executor note: (not provided)

- **SC17** (T14): Do all existing tests pass alongside the new structured-topic tests?
  Executor note: [MISSING]

## Meta

EXECUTION ORDER: T1 (helpers) → T2 (helper tests) first. Then T3 (resolver) and T4 (read-tool enrichment) and T5 (schema/prompt) in parallel. Then T6 (guard relaxation) depends on T1+T3+T5. Then T7 (validation) and T8 (source-author wiring) depend on T6. Then T9 (rendering) depends on T1. Then T10 (rendering tests) and T11 (publisher rewrite) depend on T9; T11 also depends on T3. Then T12 (runtime tests) depends on T6+T7+T8+T11. T13 (message_payload enrichment) depends on T4. Finally T14 (full test run) depends on everything.

KEY GOTCHAS:
1. The existing render_topic at line 2529 is pure and used by existing publisher tests — do NOT change its signature or behavior for simple topics. Add render_topic_publish_units as a NEW function.
2. _summary_for_tool at line 2282 currently returns {body, sections} for post_sectioned_topic. Add blocks to this dict when provided; preserve body/sections for rollback.
3. The guard at line 1362: 'not args.get("sections")' — change to reject only when both sections and blocks are missing/empty. Normalize blocks before the check.
4. source_ids at line 1342 currently derived from args['source_message_ids'] only. For structured topics, also collect from block source_message_ids union.
5. The FakeDB in tests must be extended with get_topic_editor_source_messages and updated get_topic_editor_message_context.
6. media_refs_available shape must match between _message_payload (source-window) and get_message_context (archive).
7. Publishing with blocks: when blocks present, call render_topic_publish_units instead of render_topic. Fall back to render_topic for simple/legacy topics.

## Coverage Gaps

- Tasks without executor updates: 1
- Sense-check acknowledgments missing: 1
