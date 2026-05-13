# Live update editor — watchlist, publish cap, lower bar, reliable reasoning

Profile: `thoughtful` (megaplan tier 3, `--robustness standard`, default depth).

## Goals

1. Make **watchlist** a first-class concept the model curates via tool calls. Items the model judges "promising but unclear" are watch-listed; they're reviewed at scheduled checkpoints and force a publish/discard decision before TTL.
2. **Remove the per-run publish cap** in both code and prompt.
3. **Lower the publish bar** so genuine engagement counts even when reactions are modest.
4. Make **`editor_reasoning` reliable**: persist on every run, recover from multiple shapes, never silently empty.

The four goals are coupled (watchlist absorbs the marginal items the lowered bar surfaces; removing the cap is what lets a busy hour actually post multiple items; reliable reasoning is how we observe whether the changes worked). Plan them as one sprint.

---

## Context — what exists today

### Current code paths

- `src/features/summarising/live_update_editor.py:60` sets `DEFAULT_MAX_PUBLISH_PER_RUN = 1`. Hard slice at `:453`.
- `src/features/summarising/live_update_prompts.py:349-353` tells the model "at most one normal public feed candidate per run".
- `live_update_prompts.py:319-324` tells the model "be much more willing to return zero candidates than to fill the feed".
- `_meets_editorial_bar` at `live_update_prompts.py:904-938`: showcase needs `reactions >= 5`; top_creation `reactions >= 5`; project_update `(reactions >= 3 OR reply_count >= 2 OR has_media)`.

### Current memory/watchlist mechanism

- `live_update_editorial_memory` table — written only on **publish**. Has `expires_at` column that is never read, never written.
- `live_update_watchlist` table already exists with `watch_key`, `subject_type`, `criteria` (jsonb), `status`, `priority`, `notes`, `last_matched_candidate_id`, `last_matched_at`, `created_at`, `updated_at`.
- **Nothing ever creates a watchlist row today.** The only writes are updates to `last_matched_at` when an accepted candidate matches an existing row.
- The prompt renders watchlist rows with field names **`watch_type` and `description`** (`live_update_prompts.py:450-457`) — neither column exists in the schema. The model has been seeing `null`/`null` for those two fields on every row. Dead content in the prompt.

### Current `editor_reasoning` reliability

- Of 30 recent dev runs: 18 have non-empty reasoning, 12 are empty. All 12 empties have `candidate_count=0`.
- When `candidate_count=0`, the LLM's raw output (`raw_text`) is **never persisted to the DB** — only into a gzipped Discord debug attachment. We can't tell from DB alone whether the model emitted reasoning or not on the empty runs.
- The model is emitting **prose + JSON**, not JSON-only. The salvage path at `_parse_json_payload:625-655` does real work via `text.find("{") / rfind("}")` span extraction.
- Parser at `_parse_raw_candidates:595-611` has multiple silent paths to empty: bare-array output, missing top-level key, reasoning nested per-candidate, wrong key spelling.

---

## Plan

### Phase 1 — Watchlist as a model-driven tool

**Watchlist is curated by the model.** No auto-population on defer/skip. The model decides, via tool call, when something is "promising but unclear" and worth carrying forward.

#### New tool exposed to the model

```
watchlist_add(
  watch_key:     string,        # stable identifier (model proposes; we de-dupe)
  title:         string,        # 1-line description of what's being watched
  reason:        string,        # why this is interesting but not yet ready
  source_message_ids: [string], # the messages this is grounded in
  channel_id:    optional int,
  subject_type:  optional string  # showcase | project_update | discussion | other
)
```

```
watchlist_update(
  watch_key: string,
  action:    "publish_now" | "extend" | "discard",
  notes:     optional string
)
```

- `publish_now`: model converts the watch into a published candidate this run (treated like a normal accept; goes through the usual editorial bar with the watchlist's lower threshold; reasoning required).
- `extend`: bumps `next_revisit_at` by 6-12h (capped at `expires_at`). Used when the story is still developing but not ready.
- `discard`: marks the row inactive (`status = 'discarded'`) and records `notes` as the discard reason. Permanent — won't re-enter the prompt.

Both tools idempotent on `watch_key`. Server-side: cap on max active watchlist rows per environment (suggest **50**) to bound prompt growth; oldest `fresh` rows get auto-archived (not auto-discarded) if cap exceeded.

#### Schema additions (additive only)

`live_update_watchlist` adds:

- `expires_at timestamptz` — set to `created_at + 72h`. Hard cutoff.
- `next_revisit_at timestamptz` — defaults to `created_at + 6h`.
- `revisit_count int default 0` — increments on each `extend`.
- `revisit_state text default 'fresh'` — computed in Python at read time (not stored), values: `fresh` | `revisit_due` (past `next_revisit_at`) | `last_call` (past `created_at + 24h`).
- `origin_reason text` — what the model said when it added this.
- `evidence jsonb` — snapshot of source_message_ids, channel, title at watch-creation time.
- `status` values broadened: `active` (default) | `discarded` | `archived` | `published`.

No new tables. No drops. Migration writes `expires_at = created_at + 72h` for any pre-existing rows.

#### Lifecycle

- `t=0`: model calls `watchlist_add`. Row inserted with `expires_at=+72h`, `next_revisit_at=+6h`, `revisit_state=fresh`.
- `t=6h`: row enters `revisit_due` state at next run.
- `t=24h`: row enters `last_call`. Bar drops one tier (see Phase 3). Prompt explicitly says: "decide this run — publish, extend, or discard."
- `t=72h`: row auto-discards (`status='archived'`, `notes='ttl_expired'`). Never reaches the model again.

A simple Python sweep at the start of every run handles the t=72h auto-archive (no cron needed; the run itself triggers cleanup).

#### Prompt rendering

Replace the broken watchlist block at `live_update_prompts.py:450-457` with a state-grouped structure, capped at **20 items per state** (most-recent-first):

```json
"watchlist": {
  "_explanation": "Items you previously flagged as promising-but-unclear. Decide per item: keep watching (do nothing), `watchlist_update(action='publish_now')`, `watchlist_update(action='extend')`, or `watchlist_update(action='discard')`. Items in `last_call` will auto-discard after 72h if you don't act.",
  "fresh":       [{watch_key, title, origin_reason, age_hours, source_message_ids, subject_type, channel_id}, ...],
  "revisit_due": [...],
  "last_call":   [...]
}
```

Field names match the schema (`subject_type`, `notes`) — fixes the long-standing rendering bug.

#### Where the watchlist tool calls live

- New tool definitions added at `live_update_prompts.py:469-510` (the `available_tools` array).
- Tool dispatcher in `live_update_editor.py` wires `watchlist_add` → `db.insert_live_update_watchlist`, `watchlist_update` → `db.update_live_update_watchlist`.
- Both tool calls record into a per-run `watchlist_actions` log alongside `tool_trace` for telemetry.

---

### Phase 2 — Kill the per-run publish cap

#### Code

- Remove `DEFAULT_MAX_PUBLISH_PER_RUN = 1` at `live_update_editor.py:60`. Replace with `DEFAULT_MAX_PUBLISH_PER_RUN = None` (unlimited).
- Keep the `LIVE_UPDATE_MAX_POSTS_PER_RUN` env var as an emergency throttle. Default unset.
- At `live_update_editor.py:453`, only apply the slice if the cap is not None.

#### Prompt

Rewrite `live_update_prompts.py:349-353`:

> **Before:** "into at most one normal public feed candidate per run. A second candidate only makes sense if it is also very high confidence and clearly unrelated."
>
> **After:** "Publish every candidate that genuinely meets the bar. There is no per-run cap. Most hours will have 0-2; busy hours can have more. Do not pad to fill space, and do not artificially compress when multiple items qualify."

Soften `live_update_prompts.py:319`:

> **Before:** "Be much more willing to return zero candidates than to fill the feed."
>
> **After:** "Returning zero is fine when nothing meets the bar. Returning multiple is fine when multiple do. The bar is the bar; quantity follows from it."

---

### Phase 3 — Lower the publish bar

#### Hard thresholds (`_meets_editorial_bar`, `live_update_prompts.py:904-938`)

- `showcase`: relax `reactions >= 5` → `(reactions >= 3 OR reply_count >= 2 OR author_is_high_signal)`. Already requires `has_media`.
- `top_creation`: relax `reactions >= 5` → `reactions >= 3`. The heuristic top-creations layer covers the longer tail.
- `project_update`: leave as-is (already permissive).

#### Quiet-hour rule

When the run scans fewer than 50 messages (computed from `archived_messages` length pre-filter), every category's bar drops one tier:

- `showcase`: `reactions >= 2 OR reply_count >= 1` (still requires media).
- `top_creation`: `reactions >= 2` (still requires media).
- `project_update`: keep current.

This handles "low-volume hour but real signal" without lowering the bar globally.

#### Last-call watchlist bar

A watchlist item in `last_call` publishes on:

- showcase / top_creation: `reactions >= 2 OR reply_count >= 1` (still needs media).
- project_update: `reactions >= 1 OR reply_count >= 1`.

The watchlist is the model's prior judgment that this is interesting; the bar at last-call is whether community signal has materialised at all.

---

### Phase 4 — Reliable `editor_reasoning`

Five concrete changes; all defensive, all additive:

1. **Persist `raw_text` on every run.** Today `live_update_editor.py` only writes `raw_text` into the Discord debug attachment, not into `live_update_editor_runs.metadata.agent_trace`. Add `metadata.agent_trace.raw_text = raw_output` on every run (success or zero-candidate). ~5 lines. Truncate at 50k chars to bound row size.

2. **Move reasoning to the front of the response.** New prompt instruction inserted at `live_update_prompts.py:284`:

   > "Begin your response with a single line `REASONING: <1-3 sentences>` followed by a blank line, then the JSON object. Repeat the same string inside the JSON as `editor_reasoning` for redundancy."

   Parser gets two recovery paths — even totally broken JSON yields reasoning.

3. **Parser fallback chain in `_parse_raw_candidates`:**
   - Try top-level `editor_reasoning` (current behaviour).
   - Else aliases: `reasoning`, `editor_summary`, `editorial_reasoning`.
   - Else walk candidates: concatenate any per-candidate `editor_reasoning` joined by " | ".
   - Else regex-scan prose prefix for `^REASONING:\s*(.+?)\n\n` (from change #2).
   - Else take the first paragraph (up to 3 sentences) before the JSON span as a last resort.
   - Whichever branch fires, record it in `metadata.agent_trace.reasoning_recovery_path`.

4. **Telemetry.** Surface `reasoning_recovery_path` in the audit script and in the dev debug embed. Empty-reasoning runs now tell us which branch fired and why — never silent again.

5. **Hard requirement in prompt.** Add to the OUTPUT SHAPE block: *"If you omit `editor_reasoning` or the `REASONING:` prefix line, your response is invalid and will be re-requested."* (We don't actually re-request — too costly — but the assertion changes model behaviour.)

---

### Phase 5 — Verification

Three gates before calling it done:

1. **24h dev soak.** Run the bot in dev for 24h with the new code. Audit script should show:
   - Zero runs with `editor_reasoning empty=YES`.
   - `reasoning_recovery_path` distribution visible — `top_level` should dominate.
   - At least one watchlist item created via tool call.
   - At least one `revisit_due` item handled (publish_now, extend, or discard).

2. **Publish-rate sanity.** Count candidates published in a 24h window. Expect roughly **3-5×** the prior week's rate. If it's >10×, the bar is too loose somewhere — back off.

3. **Watchlist size bounded.** End of 24h: active watchlist should have 5-30 items. Not 0 (model isn't using the tool) and not 50+ (no decisions being made on `last_call` items).

---

## File-level change inventory

| File | Phase | Changes |
|---|---|---|
| `src/common/db_handler.py` | 1 | Add `insert_live_update_watchlist`, `update_live_update_watchlist`. Adjust `get_live_update_watchlist` to compute `revisit_state`, group by state, auto-archive expired rows. |
| `src/common/storage_handler.py` | 1, 4 | Add `agent_trace.raw_text` to the metadata payload written to `live_update_editor_runs`. Match watchlist column names to schema. |
| `src/features/summarising/live_update_editor.py` | 1, 2, 4 | Remove `DEFAULT_MAX_PUBLISH_PER_RUN`. Wire up two new tool calls in `tool_dispatcher`. Persist `raw_text` in metadata. |
| `src/features/summarising/live_update_prompts.py` | 1, 2, 3, 4 | Rewrite OUTPUT SHAPE block. Update tool list. Replace watchlist rendering. Relax `_meets_editorial_bar`. Implement fallback chain in `_parse_raw_candidates`. |
| migration: `migrations/<timestamp>_live_update_watchlist_lifecycle.sql` | 1 | Add `expires_at`, `next_revisit_at`, `revisit_count`, `origin_reason`, `evidence` columns. Backfill `expires_at = created_at + 72h` for existing rows. Broaden `status` values. |
| `scripts/debug_live_editor_audit.py` | 4, 5 | Surface `reasoning_recovery_path`. Add watchlist-summary section. |
| `tests/test_live_update_editor_lifecycle.py` | 1, 2 | Tests for `watchlist_add` / `watchlist_update` tool dispatch, TTL handling, state transitions, no per-run cap. |
| `tests/test_live_update_prompts.py` | 4 | Tests for parser fallback chain across all 5 input shapes (top-level / alias / per-candidate / prose-prefix / first-paragraph). |

---

## Out of scope

- Re-architecting `live_top_creations` (separate heuristic system; behaves fine).
- Changing the per-author throttle that doesn't exist (the symptom comes from the per-run cap, not a per-author rule).
- The legacy daily summariser (`summariser.py`) — kept as-is for explicit backfill.
- Changing the 6h lookback. The combination of "watchlist carries items across runs" + "lowered bar" should close the coverage gap without expanding lookback. Revisit if 24h soak still shows gaps.
