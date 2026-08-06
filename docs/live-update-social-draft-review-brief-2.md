# Brief 2: Live-Update Social Draft Review — the user-facing flow (DM + edit tools + attested approval)

Full design: `docs/live-update-social-draft-review-spec.md`. This is **sprint 2 of 2**.
Sprint 1 (brief `docs/live-update-social-draft-review-brief.md`) landed the plumbing.
This sprint delivers the **actual user-facing feature** on top of it.

## ⚠️ Scope discipline (read first)
This sprint MUST deliver the **complete end-to-end flow**, not foundation only. A
topic publishes → the admin gets a DM → the admin edits it conversationally → the
agent posts it to X after recording an attested approval. **Do not defer any of T1–T6
below to a future sprint. Do not stop at "infrastructure".** Each task has a concrete
done-criterion; all must be met.

## Already landed in sprint 1 — DO NOT rebuild (it exists in this worktree)
- `TopicEditor._fire_social_handoff()` wired into both publish paths → calls
  `LiveUpdateSocialService.handle_live_update_publish_results(payload)` (so `draft`
  runs are already being created on publish). Plus topic-discard invalidation.
- `db_handler`: `update_live_update_social_run()` now accepts `review_message_id`,
  `revision`, `approval_state`, `approved_revision`, `publish_revision`; new readers
  `get_social_run_by_review_message_id(review_message_id, environment)` and
  `list_open_social_runs(...)`.
- `admin_chat_cog`: invalidate open drafts when an admin deletes a topic.
- The `live_update_social_runs` columns (review_message_id, revision, approval_state,
  approved_revision, approved_text, approved_quote, approved_at, expires_at,
  publish_revision) — VERIFY the migration in `.migrations_staging/` actually contains
  all of these; add any missing column before building on them.

## Outcome
The admin receives each draft tweet as a DM, edits it by chatting with the admin-chat
agent, and the agent posts it to X — but only after recording an attested approval that
still matches the current draft text/revision. Nothing auto-posts.

## Tasks (ALL required — each must ship with its test)

**T1 — DM delivery.** When a `draft` run is created (in the handoff/draft path), DM
`ADMIN_USER_ID` the draft text + a media summary (Discord embed thumbnail; raw URLs
render poorly) + topic title + source-topic link + run_id. Store the sent DM message id
via `update_live_update_social_run(review_message_id=...)`. Set `expires_at` (24h).
Reuse the `_notify_intent_admin` DM pattern (`admin_chat_cog.py:356`).
*Done:* publishing a topic results in an admin DM whose message id is persisted on the run.

**T2 — Reply→run binding + guidance.** Extend the admin DM handler so a reply that
resolves (via `get_social_run_by_review_message_id`) to an open run loads that run and
injects, **per-turn and NOT persisted into `_conversations`**: `current_draft_run_id`,
`current_draft_text`, `current_draft_revision`, `approval_state`, topic title — plus a
"Social Draft Review" guidance block (mirror `LIVE_UPDATE_FEEDBACK_GUIDANCE`,
`admin_chat_cog.py:99`). *Done:* replying to a draft DM routes to the agent with the
correct run context; an unrelated DM turn does not see that context.

**T3 — `update_social_draft(run_id, new_text)` tool.** Rewrite + store `draft_text`,
bump `revision`, reset `approval_state→'pending'`; agent re-shows the new text.
*Done:* editing changes the stored text, bumps revision, clears approval.

**T4 — `approve_social_draft(run_id, admin_approval_quote)` tool (attested gate).**
Records `approval_state='approved'`, `approved_revision`=current revision,
`approved_text`=current (canonicalized) draft, `approved_quote`, `approved_at`. Do a
lightweight non-blocking check that the quote appears in recent admin DM messages;
if not, still record but return a `warning`. *Done:* approval is recorded bound to the
current revision + text, with the quote stored.

**T5 — `publish_social_draft(run_id)` tool.** Posts via
`_make_publish_handler(force_publish=True)` (the `social_review_cog.py:223` path),
**only if**: a recorded approval exists, `approved_revision == current revision`,
canonicalized `approved_text == current draft_text`, and `terminal_status != 'published'`.
Re-verify + stamp `publish_revision` immediately before posting (TOCTOU-safe). On
refusal, return an actionable error (no-approval / stale-revision / already-published).
On success return final text + tweet URL + provider_ref. *Done:* posting works only for
a current-revision attested approval; edit-after-approve is blocked; returns the URL.

**T6 — Supporting tools.** `preview_social_draft(run_id)` (returns text/revision/
approval_state/media summary); `list_pending_social_drafts()` (required — run_id, topic
title, revision, approval_state, age for the admin's open drafts); `discard_social_draft(run_id, reason?, require_confirmation)`.
Register all 6 tools in the admin-chat tool surface and dispatch (`admin_chat/tools.py`,
schema list + the tool_name dispatch near line 4863, following the `share_to_social`
pattern at line 1944). *Done:* all tools are advertised, dispatched, and callable.

## Locked decisions (from spec §4/§6 — do not relitigate)
- Approval = agent judges it (trusted) but MUST call `approve_social_draft` with a
  verbatim `admin_approval_quote`; publish is mechanically conditional on the recorded
  approval. NOT a server-side keyword gate, NOT a passive `approved:true` boolean.
- DM delivery (not a review channel). Single `post` action. `LIVE_UPDATE_SOCIAL_MODE`
  stays unset; reuse `_make_publish_handler(force_publish=True)`.
- Guidance: agent approves and publishes in SEPARATE turns; after any edit, re-approval
  is required.

## Open questions (prep)
- Confirm how the existing `live_update_social` draft path currently produces
  `draft_text` + `media_decisions` (so DM + publish reuse them, not reinvent).
- Confirm the admin-chat tool registration + dispatch pattern in `admin_chat/tools.py`
  and how `channel_guidance`/per-turn context reaches the agent (`agent.py`).

## Constraints
- Posting to X is public + irreversible → the approval gate + integrity checks are
  load-bearing; correctness there outweighs everything.
- DM-delivery must never block/fail topic publishing (handoff already best-effort).
- Per-turn draft context must NOT persist into shared `_conversations`.

## Done criteria (sprint)
- End-to-end: publish topic → admin DM → reply-edit (revision bumps, approval resets) →
  approve (attested) → publish (returns tweet URL). Demonstrated by tests.
- Edit-after-approve is blocked from posting (test).
- All 6 tools registered, dispatched, unit-tested.
- New + existing tests pass; `tests/test_live_update_social_agent.py` and
  `tests/test_live_update_social_runs.py` stay green.

## Touchpoints
- `src/features/sharing/live_update_social/{service,agent,tools}.py` (draft path, DM trigger)
- `src/features/admin_chat/{admin_chat_cog,tools}.py` (reply-binding, guidance, 6 tools)
- `src/common/db_handler.py` (reuse the sprint-1 methods; add any missing column/method)
- Reuse: `src/features/sharing/social_review_cog.py:188` (`_make_publish_handler`)

## Anti-scope
- No review-channel delivery, no 👍-reaction approval, no `retract_social_post`, no
  surge batching/quiet-hours, no reply/quote/thread, no second-model validation.
- Do not touch legacy `live_update_editor.py`; do not refactor the publish service / X
  provider; do not flip `LIVE_UPDATE_SOCIAL_MODE`.
