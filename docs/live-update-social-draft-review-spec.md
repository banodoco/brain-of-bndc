# Live-Update → Social Draft Review (DM + agent-tool attested approval)

**Status:** spec / not yet built — **revised after 3-perspective sense-check (UX / agent-tooling / technical). See §9.**
**Author:** drafted with Claude, 2026-05-30
**Goal:** When a live-update topic is published to Discord, the bot drafts a tweet,
DMs it to the admin (ADMIN_USER_ID), and lets the admin converse with the admin-chat
agent to edit it. The agent edits and posts **only via its own tools**. Posting is
gated by a **dedicated attested-approval tool**: the agent itself judges that the
admin approved, but must record that approval through a specific tool and attest
(with the admin's verbatim approving words) before it can publish.

---

## 1. Target flow

```
1. Topic published to Discord    →  summarising/topic_editor.py (ACTIVE path)   [EXISTS]
2. Draft a tweet from the topic  →  live_update_social agent (draft mode)        [EXISTS, not wired]
3. DM the draft to the admin     →  new delivery helper + review_message_id      [BUILD]
4. Admin converses in DM to edit →  admin_chat agent (DMs route already)         [EXISTS]
5. Agent edits + re-shows        →  update_social_draft tool                     [BUILD]
6. Admin approves                →  agent records it via approve_social_draft (attested) [BUILD]
7. Agent posts                   →  publish_social_draft (requires recorded approval) → SocialPublishService [EXISTS path]
```

**Nothing auto-posts.** `LIVE_UPDATE_SOCIAL_MODE` stays unset (draft mode). The only
path a tweet leaves the building is the agent calling `publish_social_draft` for a
run whose **current revision** has a recorded, attested approval.

---

## 2. Current state (what already exists)

### Drafting — EXISTS, just not triggered
- `src/features/sharing/live_update_social/agent.py` — `LiveUpdateSocialAgent.run()`
  reconstructs publish units, resolves media, calls an LLM, and in **draft mode**
  records a `draft`-status row in `live_update_social_runs` with `draft_text`.
- Draft handler: `live_update_social/tools.py:517` (`_make_draft_handler`).
- Entrypoint: `LiveUpdateSocialService.handle_live_update_publish_results(payload)`
  (`live_update_social/service.py:41`).
- **Gap:** this entrypoint is only ever called from `tests/test_live_update_social_agent.py`.
  Nothing in `summarising/` invokes it. The service is instantiated and attached to
  the bot (`sharing/sharing_cog.py:46,52`) but never called.

### Conversational admin agent — EXISTS
- `src/features/admin_chat/admin_chat_cog.py` + `agent.py` — DeepSeek `AdminChatAgent`.
- Routes to the agent for: **DMs always** (`admin_chat_cog.py:1340`), guild messages
  when **@mentioned**, and — critically — **replies to a live-update message** via a
  reverse-lookup (`get_topic_by_discord_message_id`) that bypasses the @mention gate
  (`admin_chat_cog.py:1353-1371`). This is the pattern to imitate for "reply to a draft".
- Per-user **multi-turn conversation history** (`agent.py:48`, `_conversations`), so
  "punchier" → "add the link" → "post it" works without new plumbing.
- Channel-guidance injection pattern: `LIVE_UPDATE_FEEDBACK_GUIDANCE`
  (`admin_chat_cog.py:99`) — a per-channel guidance block fed into the system prompt.
- Already has a `share_to_social` tool through the unified publish service
  (`admin_chat/tools.py:1944`, `execute_share_to_social`), plus `edit_message`,
  `delete_message`, `log_live_update_feedback`. Existing deterministic-classifier
  prior art: `_classify_confirmation` (`admin_chat_cog.py:295`).
- DM-the-admin helper pattern: `_notify_intent_admin` (`admin_chat_cog.py:356`).

### Publishing — EXISTS
- `SocialPublishService` (`sharing/social_publish_service.py`) → `XProvider`
  (`sharing/providers/x_provider.py`). HMAC integrity signature gives replay/dup
  protection (`social_publish_service.py:402,453`).
- `social_review_cog.py`'s `!social approve <run_id>` already does
  "load stored draft_text → publish via `_make_publish_handler(force_publish=True)`"
  including media resolution (`social_review_cog.py:188-266`, esp. `:223`). This is
  exactly the "post it" action — it just isn't conversational.

---

## 3. Work items (the ~20% to build)

Touches `summarising`, `sharing/live_update_social`, `admin_chat`, plus one DB
migration. Revised effort: **~3–4 days** (see §7).

### Step 1 — Wire the handoff into the ACTIVE publish path  (`summarising/topic_editor.py`)
> ⚠️ **Corrected after sense-check.** The legacy `live_update_editor.py`
> (imported as `LegacyLiveUpdateEditor`) has since been removed; the **active**
> publisher is `TopicEditor` (`topic_editor.py`), which writes the `topics`
> rows that the admin reply reverse-lookup already keys on
> (`get_topic_by_discord_message_id`). Hooking the legacy editor would never fire.

When `TopicEditor` finishes publishing a topic to Discord, build a
`LiveUpdateHandoffPayload` (`live_update_social/contracts.py`) and call
`bot.live_update_social_service.handle_live_update_publish_results(payload)`.
Best-effort + **fire-and-forget** (background task) so it never blocks/slows the topic
publish. Fire **once per topic**; the run upsert dedups on `topic_id+platform+action`
(`service.py:72`).

> ⚠️ **VERIFY-BEFORE-BUILD (round-2 finding).** Do not assume the field mapping. The
> payload requires `topic_id, guild_id, channel_id, platform, status, source_metadata,
> topic_summary_data` + chain fields (`contracts.py:65-78`), and downstream
> `publish_units` expects `message_id` / `mainMediaMessageId` **inside
> `topic_summary_data`** (not a top-level `discord_message_ids`). First confirm exactly
> which of these the `topics` row / TopicEditor expose at publish time, and write the
> mapping explicitly. For v1: `platform="twitter"`, `status="sent"`, `action="post"`.

### Step 2 — DM the draft + store mapping  (new helper + migration)
When a `draft` run is created, DM `ADMIN_USER_ID` with:
- the draft text,
- a media summary line + Discord embed thumbnail (raw URLs don't render well in DMs),
- the **topic title** and a link to the source topic,
- the `run_id`.

Reuse the `_notify_intent_admin` DM pattern. **Store the DM'd message ID as
`review_message_id` on the run** so a reply can be bound back. Set `expires_at`
(default 24h — also bounds Discord attachment durability, see §9 risk T-MEDIA).

### Step 3 — Bind admin DM replies to the draft  (`admin_chat_cog.py` + db_handler)
Add `get_social_run_by_review_message_id(message_id)` to the DB handler. Extend the
existing reverse-lookup block (`admin_chat_cog.py:1353`, after the live-update topic
check): if an admin's DM reply resolves to a `review_message_id`, load that run and:
- inject a "Social Draft Review" guidance block (see §4),
- inject **on every turn** (not just the first reply) the active
  `current_draft_run_id`, `current_draft_text`, `current_draft_revision`,
  `approval_state`, and topic title — as **per-turn context, not persisted** into
  `_conversations` (avoids bleed into unrelated payment/feedback turns; see §9 A-HIST).

### Step 4 — New agent tools  (`admin_chat/tools.py`)
- `update_social_draft(run_id, new_text)` — rewrite + store `draft_text` (mirror
  `_make_draft_handler`); **bump `revision`** and **reset approval** for the run
  (`approval_state → 'pending'`); agent re-shows the new text.
- `approve_social_draft(run_id, admin_approval_quote)` — **the attested-approval gate
  (see §4).** The agent calls this when it judges the admin has explicitly approved
  the current text. `admin_approval_quote` is the admin's verbatim approving words.
  Records `approval_state='approved'`, `approved_text`=current draft, `approved_revision`=current revision, `approved_quote`, `approved_at`. Returns confirmation.
- `publish_social_draft(run_id)` — publishes via `_make_publish_handler(force_publish=True)`
  (the `!social approve` path) **only if** the run has a recorded approval whose
  `approved_revision == current revision` and `approved_text == current draft_text`,
  and `terminal_status != 'published'`. Otherwise refuses with a clear error telling
  the agent to obtain/record approval first. Posts, then records publication outcome.
- `preview_social_draft(run_id)` — returns current text, revision, approval_state,
  media summary (lets the agent show "what will post").
- `list_pending_social_drafts()` — lists the admin's open drafts (run_id, topic title,
  revision, approval_state, age). **Required**, not optional (multi-draft visibility).
- `discard_social_draft(run_id, reason?)` — soft-delete (`terminal_status='discarded'`).

### Step 5 — Gating config  (trivial)
Leave `LIVE_UPDATE_SOCIAL_MODE` unset (draft mode). Publishing happens only through
`publish_social_draft`. (No scheduled-worker filtering needed — draft runs don't
create `social_publications` rows; see §9 T-RACE correction.)

---

## 4. Approval gating — agent-attested, deliberate, audited

**Design decision (owner):** We do **not** move approval to a deterministic
server-side keyword matcher, and we do **not** rely on a passive `approved: true`
boolean. **The agent itself decides whether the admin approved — we trust it — but it
must record that approval through a dedicated tool and attest to it.** The friction of
a separate, attested action (not a flag tacked onto the publish call) is the
safeguard against eager/accidental posting, and it leaves an audit trail.

### The mechanism
1. **Guidance block ("Social Draft Review")**, injected like `LIVE_UPDATE_FEEDBACK_GUIDANCE`:
   - May freely call `update_social_draft` to revise and re-show the text.
   - When the admin clearly approves the **current** text, call `approve_social_draft`
     with `admin_approval_quote` set to the admin's actual approving words. Do **not**
     fabricate or paraphrase approval — quote what they said. Vague positivity
     ("looks good", "nice") is not approval; if unsure, ask.
   - Only after approval is recorded, call `publish_social_draft`.
   - After **any** edit, approval is reset — you must obtain and record fresh approval.
   - Never post on your own initiative.
2. **`approve_social_draft(run_id, admin_approval_quote)`** — the attestation step.
   Records approval bound to the current revision + exact draft text. The quote is
   stored for audit.
3. **`publish_social_draft(run_id)`** — server-side checks (these are *integrity*
   checks on the recorded approval, not a re-judgement of intent):
   - a recorded approval exists for this run,
   - `approved_revision == current revision` **and** `approved_text == current draft_text`
     (so an edit after approval blocks the post),
   - `terminal_status != 'published'` (no re-post).
   Refuses with an actionable error otherwise.

### Integrity details (round-2 findings)
- **TOCTOU.** Re-verify approval and stamp `publish_revision` **immediately before**
  the post, in a single transaction with the guard read — not after. Prevents a
  concurrent edit between check and post from publishing unapproved text.
- **Comparison basis.** Compare a **canonicalized** form of `approved_text` vs current
  `draft_text` (strip + collapse whitespace), not raw `==` — avoids false "stale"
  blocks from reflow/whitespace. On mismatch, return a diff and ask the agent to
  re-show + re-approve.
- **Quote integrity (non-blocking).** When recording approval, do a lightweight search
  of the recent admin DM messages for `admin_approval_quote`; if not found, still
  record but return a `warning` so the agent can self-correct in-conversation. This
  respects "trust but attest" — it surfaces drift without server-side gating intent.
- **Actionable errors.** `publish_social_draft` refusals must tell the agent the next
  step: no approval → "call approve_social_draft first"; stale revision → "text changed
  since approval (rev N vs M), re-approve"; already published → "use list/discard".

### Why this shape (vs. the reviewers' "server-side keyword gate")
The reviewers recommended removing the model from the approval decision entirely.
Owner's call: keep the agent in the loop (it reads nuance the keyword matcher can't),
but make approval a **deliberate, named, attested act with an audit trail**, and make
*publishing* mechanically conditional on that recorded act matching the current text.
We trust the agent's judgement; we do not trust a stray boolean.

---

## 5. Data model changes

`live_update_social_runs` (existing table) — add columns:
- `review_message_id` (bigint, nullable) — admin DM message the draft was delivered in.
- `revision` (int, default 0) — bumped on each `update_social_draft`.
- `approval_state` (text, default 'pending') — 'pending' | 'approved'.
- `approved_revision` (int, nullable) — revision that approval was recorded against.
- `approved_text` (text, nullable) — snapshot of draft_text at approval.
- `approved_quote` (text, nullable) — admin's verbatim approving words (audit).
- `approved_at` (timestamptz, nullable).
- `expires_at` (timestamptz, nullable) — draft TTL (default 24h).
- `publish_revision` (int, nullable) — revision actually posted (audit / dup-guard).

DB handler work (verified gaps, `db_handler.py:2762`):
- Extend `update_live_update_social_run()` to accept/persist the new fields (currently
  only handles terminal_status, draft_text, media_decisions, trace_entries, publication_outcome).
- Add `get_social_run_by_review_message_id(message_id)`.
- Add `list_open_social_runs(guild_id)` for `list_pending_social_drafts`.

**Migration convention (verified):** runs live in `.migrations_staging/` (e.g.
`.migrations_staging/20260515161000_live_update_social_runs.sql`). Add an ALTER and
test it against Supabase before shipping.

---

## 6. Decisions locked

| Decision | Choice |
|---|---|
| Delivery surface | **DM the admin** (`ADMIN_USER_ID`) |
| Editing | Agent edits via `update_social_draft` tool |
| Posting | Agent posts via `publish_social_draft` tool |
| Approval | **Agent judges approval (trusted) but must record it via a dedicated `approve_social_draft` tool with a verbatim attestation quote.** Publish is mechanically conditional on that recorded approval matching the current revision + text. |
| Auto-post | Never; `LIVE_UPDATE_SOCIAL_MODE` stays draft. (No worker race — draft runs create no `social_publications` rows.) |
| Hook point | `TopicEditor` (active), **not** `live_update_editor.py` (legacy) |

---

## 7. Effort summary (revised: ~3–4 days)

| Item | Where | Size |
|---|---|---|
| 1. Handoff call from TopicEditor publish path | `summarising/topic_editor.py` | small–medium |
| 2. DM delivery + `review_message_id` + expiry | new helper | small |
| 3. Reply→run reverse-lookup + per-turn context | `admin_chat_cog.py`, `db_handler.py` | small–medium |
| 4. Tools: update / approve / publish / preview / list / discard | `admin_chat/tools.py` | medium |
| 5. Approval guidance block | `admin_chat_cog.py` | small |
| 6. Migration (9 columns) + handler methods, tested vs Supabase | `.migrations_staging/`, `db_handler.py` | medium |
| 7. Approval-integrity guards + tests (TOCTOU txn, canonical text-match, edit-after-approve) | `admin_chat/tools.py`, tests | medium |
| 8. Gating config | env | trivial |

Originally estimated 1–2 days; sense-check showed migration + approval state machine +
concurrency tests push it to **~3–4 days**. Architecture is sound; drafting, the
conversational loop, the publish service/provider, media handling, and the reply
routing all already exist — this is wiring + tools + migration + the attested-approval
gate.

---

## 8. Key file references

- Handoff entrypoint: `src/features/sharing/live_update_social/service.py:41`
- Draft handler: `src/features/sharing/live_update_social/tools.py:517`
- Agent: `src/features/sharing/live_update_social/agent.py`
- Handoff payload: `src/features/sharing/live_update_social/contracts.py`
- Service instantiation: `src/features/sharing/sharing_cog.py:46`
- Publish-from-stored-draft (the "post it" path): `src/features/sharing/social_review_cog.py:188`
- Admin agent routing / reply reverse-lookup: `src/features/admin_chat/admin_chat_cog.py:1336,1353`
- Guidance-injection pattern: `src/features/admin_chat/admin_chat_cog.py:99`
- Deterministic-classifier prior art: `src/features/admin_chat/admin_chat_cog.py:295`
- DM-admin helper: `src/features/admin_chat/admin_chat_cog.py:356`
- Existing social tool: `src/features/admin_chat/tools.py:1944`
- Publish service / provider: `src/features/sharing/social_publish_service.py`, `src/features/sharing/providers/x_provider.py`
- Scheduled publish worker (concurrency): `src/features/sharing/sharing_cog.py:78`
- **Active** publish path (hook here): `src/features/summarising/topic_editor.py` (selected at `summariser_cog.py:140`)
- Legacy editor (do NOT hook): `src/features/summarising/live_update_editor.py` (`summariser_cog.py:8`)
- DB handler social-run methods: `src/common/db_handler.py:2762`
- Migration dir: `.migrations_staging/`

---

## 9. Sense-check findings & resolutions (3-perspective review)

Reviewed from UX, agent-tooling, and technical-execution lenses. Resolutions folded
into the body above.

### Accepted & incorporated
- **[Technical, verified] Wrong hook point.** `live_update_editor.py` is legacy;
  active path is `TopicEditor`. → §3 Step 1 corrected.
- **[Technical, verified] DB schema/handler under-scoped.** Migration lacks the
  columns; `update_live_update_social_run()` doesn't accept them; no reverse-lookup
  reader. → §5 expanded; §7 effort raised.
- **[Technical] T-RACE — double-post.** ~~30s scheduled worker vs. manual publish.~~
  **CORRECTED in round 2: this race does not exist.** The scheduled worker
  (`sharing_cog.py:78`) claims from `social_publications`; **draft runs never create
  `social_publications` rows** (only publish does). The only needed guard is
  `publish_social_draft` refusing an already-published run — which the existing handler
  already does. The round-1 "worker never claims draft runs" framing was based on a
  false premise; no worker-filter work is required.
- **[Technical] T-MEDIA — stale media.** Draft made at publish-time, approved hours
  later; Discord attachments expire. → 24h `expires_at`; publish forces `needs_review`
  if expected media can't re-resolve (existing pattern, `agent.py:127`).
- **[Agent] run_id binding.** Inject current run context every turn, per-turn (not
  persisted). → §3 Step 3.
- **[Agent] A-HIST — history contamination** across payment/feedback/social. →
  per-turn context, not persisted into `_conversations`.
- **[Agent] Missing tools** (preview / list / discard). → added to §3 Step 4;
  `list_pending_social_drafts` made required.
- **[UX] Multi-draft confusion / visibility.** → topic title + run_id in DM and in
  per-turn context; `list_pending_social_drafts` required; agent confirms which draft
  when ambiguous.
- **[UX] Post-publish feedback.** Return final text + tweet URL + provider_ref. →
  `publish_social_draft` returns these for the agent to relay.
- **[UX] Draft expiry / topic-edit invalidation.** → `expires_at`; on topic
  edit/delete (existing live-update-feedback flow) invalidate pending drafts.
- **[UX] Media preview in DM** via embeds + text fallback. → §3 Step 2.

### Considered & overridden by owner
- **[Agent + UX] "Move approval fully server-side / deterministic keyword gate;
  never let the model decide."** **Overridden.** Owner's decision: the agent decides
  (trusted), but must record approval via a dedicated tool with a verbatim
  attestation quote, and publishing is mechanically conditional on that recorded
  approval matching the current revision/text. See §4 rationale. (We keep the
  *integrity* checks the reviewers wanted — revision/text match, no re-post — just
  not the "model is never trusted" stance.)

### Deferred (post-v1, optional)
- Reaction-based (👍) approval as a mobile-friendly alternative.
- `retract_social_post` undo window after posting (provider_ref is surfaced so manual
  deletion is possible meanwhile).
- Surge batching / per-channel opt-out / "quiet for 1h" if DM volume becomes a problem.
- Priority/urgency hints + reminder DM for time-sensitive topics.
- Second-model validation pass before posting (latency tradeoff).

### Round-2 sense-check (after the revisions above)
No reviewer re-litigated the approval stance; remaining items are implementation
precision. **Convergence reached — diminishing returns; no round 3 planned.**

Corrected / incorporated:
- **[Technical, verified] T-RACE was a false premise** → corrected (see above); no
  worker-filter work needed.
- **[Technical] Payload field mapping was overstated** → §3 Step 1 now a
  VERIFY-BEFORE-BUILD with the real `contracts.py` / `publish_units` field expectations.
- **[Technical/Agent] TOCTOU on approve→publish** → §4 integrity: re-verify + stamp
  `publish_revision` in one transaction before posting.
- **[Agent] Exact text-match brittleness** → §4 integrity: canonicalized comparison + diff.
- **[Agent] Quote fabrication** → §4 integrity: non-blocking quote-vs-recent-messages
  check that warns (respects "trust but attest").
- **[Agent] Actionable publish errors / edit-reset clarity** → §4 integrity.
- **[Agent] Sequential tool calls** → guidance: approve and publish in separate turns.
- **[Agent] Discard footgun** → `discard_social_draft` gets a `require_confirmation` flag.
- **[UX] Two-step visibility** → agent shows "recorded approval: '<quote>'" before posting.
- **[UX] Edit-reset framing** → agent frames re-approval as "updated to vN, tell me when
  to approve", not a refusal.
- **[UX] Per-turn noise** → show compact 1-line preview each turn; full text only after edits.
- **[UX] Expiry** → warn when <6h TTL remains; on expiry-refusal offer to re-draft.

Confirmed correct by round 2: hook point = TopicEditor; media re-resolved at publish
(`tools.py` `_make_publish_handler` → `_run_media_understanding_and_upload`); publish
reuse of `_make_publish_handler(force_publish=True)`; per-turn-context pattern is sound
(code-review risk, not architectural). DB-handler signature extension remains a
**build-first prerequisite** (not a spec defect).
