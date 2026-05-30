# Brief: Live-Update → Social Draft Review (DM + attested approval)

Full design + rationale: `docs/live-update-social-draft-review-spec.md` (reviewed over
2 adversarial rounds; §9 records findings). This brief is the locked, self-contained
scope for the megaplan.

## 1. Outcome
When a live-update topic is published to Discord, the bot drafts a tweet, DMs it to the
admin, and lets the admin edit it conversationally with the admin-chat agent. The agent
posts to X **only** via a tool, and **only** after recording an attested admin approval
that still matches the current draft text/revision. Nothing auto-posts.

## 2. Scope (IN)
- Wire `LiveUpdateSocialService.handle_live_update_publish_results` into the **active**
  publish path (`TopicEditor`), fire-and-forget, once per topic.
- DM the draft to `ADMIN_USER_ID`; store `review_message_id`; 24h `expires_at`.
- Reverse-lookup admin DM replies → social run; inject per-turn (non-persisted) draft
  context + a "Social Draft Review" guidance block into the admin-chat agent.
- New admin-chat tools: `update_social_draft`, `approve_social_draft` (attested),
  `publish_social_draft`, `preview_social_draft`, `list_pending_social_drafts`,
  `discard_social_draft` (with `require_confirmation`).
- Migration: add 9 columns to `live_update_social_runs` (see spec §5); extend
  `update_live_update_social_run()`; add `get_social_run_by_review_message_id` and
  `list_open_social_runs`.
- Approval integrity: TOCTOU-safe re-verify + `publish_revision` stamp in one
  transaction; canonicalized text comparison; non-blocking quote-vs-recent-messages
  check; actionable refusal errors.
- Tests for the tools, the approval state machine, and edit-after-approve.

## 3. Locked decisions
- Delivery = **DM the admin** (not a review channel).
- Approval = **agent judges it (trusted) but must call `approve_social_draft` with the
  admin's verbatim `admin_approval_quote`**; publishing is mechanically conditional on
  that recorded approval matching the current revision + canonicalized text. NOT a
  server-side keyword gate, NOT a passive `approved:true` boolean. (Owner decision; see
  spec §4.)
- Hook point = `TopicEditor` (active), **NOT** `live_update_editor.py` (legacy).
- `LIVE_UPDATE_SOCIAL_MODE` stays unset (draft mode); reuse
  `_make_publish_handler(force_publish=True)` for the actual post.
- v1 payload: `platform="twitter"`, `action="post"`, `status="sent"`.

## 4. Open questions (prep must resolve before planning)
- **TopicEditor field mapping:** confirm exactly which `LiveUpdateHandoffPayload` fields
  (`contracts.py:65-78`) the `topics` row / TopicEditor expose at publish time, and where
  `message_id` / `mainMediaMessageId` live inside `topic_summary_data` (per
  `publish_units`). Write the mapping explicitly. Confirm where in `TopicEditor` a topic
  is considered "published to Discord."
- **DB handler:** confirm `.migrations_staging/` is the migration convention and that the
  9 columns can be added to `live_update_social_runs`; confirm the current
  `update_live_update_social_run()` signature gap (`db_handler.py:2762`).
- Confirm where topics can be edited/deleted so pending drafts can be invalidated.

## 5. Constraints
- Posting to X is public + irreversible → the approval gate and its integrity checks are
  load-bearing; correctness there outweighs everything else.
- Handoff must never block or fail the topic publish (best-effort, backgrounded).
- Per-turn draft context must NOT be persisted into the shared `_conversations` history
  (avoid bleed into payment/feedback turns).
- Migration is additive only; no backfill of existing rows required.

## 6. Done criteria
- Publishing a topic via TopicEditor creates a `draft` run and DMs the admin (verifiable
  end-to-end or via a focused integration test).
- An admin DM reply routes to the agent, edits bump revision + reset approval, and
  `publish_social_draft` refuses unless a current-revision attested approval exists.
- Edit-after-approve is blocked from posting (test).
- `publish_social_draft` returns final text + tweet URL + provider_ref on success.
- New + existing tests pass (`tests/test_live_update_social_agent.py` stays green).

## 7. Touchpoints
- `src/features/summarising/topic_editor.py` (hook), `summariser_cog.py` (confirm active path)
- `src/features/sharing/live_update_social/{service,agent,contracts,tools}.py`
- `src/features/admin_chat/{admin_chat_cog,tools}.py`
- `src/common/db_handler.py` (~2762) + `.migrations_staging/`
- Reuse: `src/features/sharing/social_review_cog.py:188` (`_make_publish_handler`)

## 8. Anti-scope (do NOT build in v1)
- No review-channel delivery (DM only).
- No reaction-based (👍) approval, no `retract_social_post` undo window.
- No surge batching / per-channel opt-out / quiet-hours / priority-reminder DMs.
- No second-model pre-post validation pass.
- No reply/quote/thread strategies — single `post` action only.
- Do not flip `LIVE_UPDATE_SOCIAL_MODE` to queue/publish; do not touch the legacy
  `live_update_editor.py`; do not refactor the publish service or X provider.
