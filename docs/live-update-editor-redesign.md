# Live-update editor: holistic redesign

**Status:** proposal, v6 (post-Codex-v5-review)
**Author:** drafted via debugging session, 2026-05-13
**Reviewer:** POM
**Independent reviews:** Codex GPT-5.5H × 3 at v4 (technical / agent-behavior / human-operator) confirmed direction and sharpened the multi-table shape; Codex GPT-5.5H ×1 at v5 caught two migration-blocking schema bugs, the missing collision-override path, and a too-tight `topic_sources` constraint. v6 fixes all of those, corrects the tool count, and expands the "lock before Day 1" decision list. v5 trims (`search_result_id` gate dropped, `topic_publications` folded into `topics`) remain in place.

## Summary

The live-update editor scans newly-archived Discord messages each hour and decides which community developments to publish to the BNDC live feed. The current architecture has accumulated overlapping concepts, multiple parallel decision mechanisms, and ~8 KB of imperative prompt rules to keep the model on track. The model still produces malformed output (multi-JSON blocks, narrated-but-not-invoked watchlist intent, missing examples on multi-author stories) often enough that we've stacked app-side enforcement gates underneath the prompt.

This document proposes a redesign that organizes around one noun — **the topic** — with native Anthropic tool-use, **agentic search** for dedupe (no embedding pipeline), and a tool surface that uses tool *choice* to encode structural decisions (so the dispatcher can reject the wrong shape at submission time, not after the model emits a malformed candidate).

The promise: ~1500-char prompt, ~800-1000 lines of code (down from ~3000), no multi-JSON parser bugs, no narrate-without-invoke bugs, the multi-section vs single-message decision is a structural invariant rather than a prompt rule, no external embedding dependency, and a flat conceptual surface that costs ~zero per new editorial feature.

The cost: a few more tables than the v1 single-table sketch (the multi-reviewer review correctly pushed back on one-table purity, though v5 collapses delivery state back onto `topics`), more tool calls per run (slower, more API spend, but acceptable at hourly cadence), and a real shadow-mode trial week before flipping production over.

**Execution shape:** one-week shadow-mode vertical slice in dev (build the new system end-to-end with **publishing turned off**, replay real source-message windows, compare against today's editor), THEN a separate decision about prod cutover. The original "3-day migration" framing in v1 of this doc was unrealistic.

---

## Why we're here

The current architecture grew organically. Each new editorial concept was added by extending the prompt, adding a candidate field, and patching an enforcement layer downstream. The cumulative state:

**Five dedupe surfaces doing overlapping work:**

- `live_update_feed_items` — published posts, unique on `(env, duplicate_key)`
- `live_update_duplicate_state` — separate "we've seen this key before" cache
- `live_update_editorial_memory` — model-readable snapshot of past coverage
- `live_update_candidates` — every emission, including rejected ones
- `recent_feed_items` payload — denormalized prompt slice

**Two parallel decision mechanisms:**

- `candidates.decision` field with values `publish | defer | skip | duplicate`
- `tool_requests` calls to `watchlist_add` / `watchlist_update`

These are conceptually the same thing ("what should we do with this item?") but use completely different mechanisms. The model has to remember to use the right one for the right outlet.

**Eleven-gate editorial_checklist:** `source_verified, new_information_identified, prior_updates_checked, surrounding_history_checked, author_context_considered, community_signal_checked, duplicate_checked, public_value_clear, risk_checked, media_selected_when_useful, publish_format_ready`. Mostly variations of "did you do the work" — enforced as a separate validation pass that can reject the model's emission.

**Text-mode JSON instead of native tool_use:** the editor calls `messages.create` with no tools, parses JSON out of the response text via regex. This is why the multi-JSON-blocks-in-one-response bug existed — the API has no notion of "exactly one JSON object per response", so the prompt has to enforce it imperatively, and a single malformed response cascades into total output loss.

**The receipt is the prompt:** 8 KB of FORCING RULEs, HARD RULEs, TRIAGE buckets, decision trees, and "X is a BUG" disclaimers. The prompt's length is a fair reflection of the conceptual surface area underneath. The model isn't confused because it's dumb — it's confused because the system has too many overlapping ways to express the same intent.

Recent debugging session symptoms that all collapse to this root:

- Model narrates *"I watchlisted X"* in `editor_reasoning` without invoking `watchlist_add` → because watchlist is a tool call but publish is a JSON field; the model conflates the two outlets.
- Model emits a candidate with 11 source messages from 5+ contributors as a flat `media_refs` list → because the `examples` array is an opt-in shape buried in the prompt, not a structural requirement.
- Model produces four separate JSON blocks in one response (searches + watchlist_add + candidates + commentary) → because there's no native message-boundary protocol; everything is parsed out of free text.
- App-side enforcement gates exist to catch the model's structural mistakes → because the prompt asks for structure the schema doesn't enforce.

Every new editorial concept added in this debugging session (STORY UPDATE, multi-example forcing, watchlist HARD RULE) made the next concept harder to add. The prompt is now load-bearing in a way that makes it brittle.

## The job, stripped down

For each topic that appears in the source-message window, decide one of three things:

1. **Post it** — the community wants to see this. Pick a structure (one creator's post vs multi-contributor).
2. **Watch it** — interesting but not ready. Revisit when more signal arrives.
3. **Ignore it** — routine chatter, support questions, intros, already-covered with no new development.

Story updates are a fourth-looking case but they're really "post it, with a pointer to the previous post". Dedupe is a property of `topic_key`, not a separate concept. The 11-gate checklist is "did you decide thoughtfully" — which the tool choice itself encodes.

Three outlets, one structural choice, one identity (`topic_key`). That's the whole job.

## Mental model: topics

The unit of work is a **topic** — a thing the community is discussing or doing, identified by a stable `topic_key`. A topic accumulates source messages over time and transitions through states based on editorial decisions.

```
states: posted | watching | discarded

new content → post_topic → posted (topic row created)
            → watch_topic → watching (topic row created)
            → (no action — chatter not worth covering, no row created)
watching → post_topic with parent_topic_key → posted (story update)
         → discard_topic → discarded (audit-only end-state)
         → post_topic → posted (was watched, now ready)
posted → post_topic with parent_topic_key → posted (story update)
```

Every editorial decision that creates state is a state transition on a topic. Casual chatter the agent decides isn't worth covering produces **no tool call and no topic row** — the absence of action is the decision.

A topic can have:

- One or many source messages, accumulating across runs
- One author or many contributors
- A current state and a full history of transitions
- An optional `parent_topic_key` if it's a follow-up to another topic
- An optional `revisit_at` if it's being watched

## Data model

The v1 sketch put everything on one `topics` table with `transitions jsonb`. The technical reviewer pushed back hard on this — JSON-array audit trails are unindexable, cause write amplification, and turn ordinary operational queries ("show me all failed publish attempts by model version") into JSON-path spelunking. They also conflate editorial state (decide / re-decide) with delivery state (Discord publishing succeeded / failed mid-render).

Final shape: **topic-centered but multi-table**, separating editorial identity from event log from delivery state.

```sql
-- Current editorial state for each topic, plus Discord delivery state.
CREATE TABLE topics (
  topic_id            uuid PRIMARY KEY,
  canonical_key       text NOT NULL,                       -- canonical identity (see below)
  display_slug        text,                                -- human-readable, optional
  guild_id            bigint NOT NULL,
  environment         text NOT NULL,                       -- prod | dev
  state               text NOT NULL,                       -- posted | watching | discarded
  headline            text NOT NULL,
  summary             jsonb,                               -- {body, sections?, media?}
  source_authors      text[] NOT NULL DEFAULT '{}',        -- denormalized for filtering
  parent_topic_id     uuid REFERENCES topics(topic_id),    -- for story updates
  revisit_at          timestamptz,

  -- Discord delivery state (folded in from v4's topic_publications table).
  -- 1:1 with topic; if you ever need full retry history move back to a
  -- separate table.
  publication_status  text,                                -- pending | sent | partial | failed | null
  publication_error   text,
  discord_message_ids bigint[] NOT NULL DEFAULT '{}',
  publication_attempts int NOT NULL DEFAULT 0,
  last_published_at   timestamptz,

  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  UNIQUE (environment, guild_id, canonical_key)
);

CREATE INDEX topics_state_idx ON topics (environment, guild_id, state);
CREATE INDEX topics_revisit_idx ON topics (environment, revisit_at) WHERE state = 'watching';
CREATE INDEX topics_headline_trgm ON topics USING gin (headline gin_trgm_ops);  -- for search_topics

-- Source message → topic relation. A single (topic, message) pair is unique
-- (prevents adding the same message twice to one topic). The same message CAN
-- support multiple topics — that's a real case for story updates where a
-- parent post and a later child topic both reference the announcing message.
CREATE TABLE topic_sources (
  topic_id            uuid NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
  message_id          bigint NOT NULL,
  guild_id            bigint NOT NULL,
  environment         text NOT NULL,
  added_in_run_id     uuid,
  created_at          timestamptz NOT NULL DEFAULT now(),

  UNIQUE (topic_id, message_id)
);

-- Lookup index for "what topics does this message participate in?" — needed
-- by the dispatcher's collision scan.
CREATE INDEX topic_sources_message_idx ON topic_sources (environment, guild_id, message_id);

-- Alternate slugs the agent might invent for the same story. Solves slug
-- drift: when the agent generates "omninft-lora-ltx23" then later
-- "omninft-ltx-2-3-lora", both resolve to one topic via alias lookup.
CREATE TABLE topic_aliases (
  alias_id            uuid PRIMARY KEY,
  topic_id            uuid NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
  alias_key           text NOT NULL,
  alias_kind          text NOT NULL,                       -- canonical | display | proposed
  guild_id            bigint NOT NULL,
  environment         text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),

  UNIQUE (environment, guild_id, alias_key)
);

-- Normalized event log: every state change, every tool call, every decision —
-- including rejected ones (a write that the dispatcher refused at submission).
-- topic_id is NULLABLE because a rejected create has no topic yet.
CREATE TABLE topic_transitions (
  transition_id       uuid PRIMARY KEY,
  topic_id            uuid REFERENCES topics(topic_id) ON DELETE CASCADE,  -- nullable for rejected create attempts
  run_id              uuid NOT NULL,
  environment         text NOT NULL,                       -- prod | dev (denormalized for fast filter)
  guild_id            bigint NOT NULL,                     -- denormalized for fast filter
  tool_call_id        text,                                -- Anthropic tool_use_id (idempotency key)
  from_state          text,
  to_state            text,                                -- target state; NULL for rejected actions
  action              text NOT NULL,                       -- post_simple | post_sectioned | watch | update_sources | discard
                                                            -- | rejected_post_simple | rejected_post_sectioned | rejected_watch
                                                            -- | observation
  reason              text,
  model               text,
  payload             jsonb,                               -- agent's notes, structure choice, dispatcher rejection details
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX transitions_topic_idx ON topic_transitions (topic_id, created_at) WHERE topic_id IS NOT NULL;
CREATE INDEX transitions_run_idx ON topic_transitions (run_id);
CREATE INDEX transitions_action_idx ON topic_transitions (environment, action, created_at);

-- Optional: sampled record of "I considered this and decided not to post."
-- Mitigates the "no row for ignored = no debugging visibility" risk.
CREATE TABLE editorial_observations (
  observation_id      uuid PRIMARY KEY,
  run_id              uuid NOT NULL,
  guild_id            bigint NOT NULL,
  environment         text NOT NULL,
  source_message_ids  bigint[] NOT NULL,
  source_authors      text[] NOT NULL DEFAULT '{}',
  observation_kind    text NOT NULL,                       -- ignored | near_miss | considered
  reason              text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now()
);
```

Five tables, each with one job. Compare to today's five overlapping ones.

**Mapping from old concepts:**

| Old | New |
|---|---|
| `live_update_feed_items` row | `topics WHERE state='posted'` + `topics.publication_*` for delivery state |
| `live_update_watchlist` row | `topics WHERE state='watching'` |
| `live_update_duplicate_state` row | unique constraint on `canonical_key` + `topic_aliases` lookup |
| `live_update_editorial_memory` row | recent-topic context in run payload + `search_topics` tool |
| `live_update_candidates` row | doesn't exist; the topic row IS the decision |
| `live_update_decisions` row | `topic_transitions` row per state change |
| `feed_item.update_type='project_update'` | `topics.parent_topic_id IS NOT NULL` |
| Casual-chatter rejection rows | `editorial_observations` (sampled) — preserves debugging visibility |
| Source-message → feed_item via JSON array | `topic_sources` normalized row + unique constraint |
| Discord post failure mid-render (untracked) | `topics.publication_status='partial'` with `publication_error` |

**Topic identity (canonical key + aliases).** The hardest predictable failure mode of v1 — flagged independently by all three reviewers — was leaving topic identity entirely to the agent. Claude won't reliably produce the same slug across runs for the same story; `omninft-lora-ltx23`, `omninft-ltx-2-3-lora`, and `gleb-omninft-lora-test` are all plausible outputs.

The fix has two halves:

1. **Canonical keys are app-generated**, not agent-authored. When the agent calls a `post_*` or `watch_topic` tool with a *proposed_key* and `source_message_ids`, the dispatcher normalizes the key (lowercase, hyphenate, strip articles, anchor to creator + artifact + date when extractable) and uses that as `canonical_key`. The agent's proposed key becomes a `topic_aliases` row with `alias_kind='proposed'`.

2. **Search returns canonical keys + aliases.** `search_topics(query)` returns existing topics with their canonical_key, display_slug, AND all known aliases. When the agent reads a search result, it sees every name we've ever known a topic by. When it then calls `post_*`, the dispatcher resolves the proposed key through the aliases table — same story under a different slug still hits the same topic_id.

This means the agent can be sloppy about slug consistency without fracturing stories. The slug remains human-readable for log inspection; the alias system absorbs drift.

**Transitions audit trail.** Each state change writes one `topic_transitions` row. To see the lifecycle of a topic: `SELECT * FROM topic_transitions WHERE topic_id = ? ORDER BY created_at`. One indexed query, full history. Operational queries like "show all failed publish attempts in the last 24h" or "compare acceptance rate by model version" become normal SQL, not JSON-path navigation.

## Architecture: agentic, two stages

```
┌──────────────────────────────────┐    ┌────────────────────┐
│ 1. Editorial agent               │ →  │ 2. Topic publisher │
│    LLM + tools, fully agentic    │    │    pure function   │
└──────────────────────────────────┘    └────────────────────┘
       source messages + state →            state transitions →
       editorial decisions                  Discord messages
```

No pre-pass stage. No clustering algorithm. No embedding store. The agent owns the whole decision loop and uses tools when it needs more information.

### Stage 1: editorial agent (LLM tool-use, agentic search)

The agent receives:

- New source messages since the last checkpoint.
- All currently-watching topics from the DB (so the agent can re-evaluate them this run).
- A short editorial prompt and a fixed tool surface.

The agent decides for itself which messages belong to which topic, whether a topic is new or a continuation, whether to publish / watch / ignore, and what structure a published topic should have. When it needs information it doesn't have — "have we seen this story before?", "who is this author?", "did this message get follow-up reactions?" — it calls a search tool.

**Tool surface (all native Anthropic tool-use):**

The agent-behavior reviewer made the strongest single design move here: **split `post_topic` into two structurally-distinct tools so the multi-section decision is a tool-choice invariant, not a soft prompt rule.** The dispatcher rejects `post_simple_topic` for multi-author / multi-message clusters without invoking the model again — the wrong shape is structurally unreachable, not just discouraged.

```python
# === Read tools (agent calls as needed) ===

@tool
def search_topics(query: str, state_filter: list[str] = None, hours_back: int = 72) -> SearchResult:
    """Find existing topics by headline / body / source-message content.
    Returns each topic with its canonical_key, display_slug, current state,
    and all known aliases.

    Call this whenever you suspect a cluster of new messages might be a
    continuation of an existing topic. Not enforced as a gate — but at
    write time the dispatcher also runs a canonical-key + similarity scan
    and surfaces collisions back as a tool error, so the agent gets a
    safety net even when it forgot to search."""

@tool
def search_messages(query: str, channel_id: int = None, author_id: int = None, hours_back: int = 24, limit: int = 20) -> list[Message]:
    """Search archived Discord messages."""

@tool
def get_author_profile(author_id: int) -> AuthorProfile:
    """Author stats, recent messages, avg reactions."""

@tool
def get_message_context(message_ids: list[str]) -> list[MessageWithContext]:
    """Fetch messages with surrounding thread / reply context."""

# === Decision tools — one per outlet, split for structural enforcement ===

@tool
def post_simple_topic(
    proposed_key: str,               # agent-suggested slug; dispatcher canonicalizes
    headline: str,                   # 6-14 words, factual, no hype
    body: str,                       # 1-2 sentences, 35-220 words
    source_message_ids: list[str],   # 1-2 messages, MUST be from 1 distinct author
    media: list[str] = None,         # up to 4 URLs
    parent_topic_id: str = None,     # set → story update, renders as "Update: ..."
    notes: str = None,               # optional free-form introspection
    override_collisions: list[OverrideCollision] = None,   # see below
) -> None:
    """Publish a single-author, single-post topic to the live feed.

    Dispatcher REJECTS this call (returns error to model) when:
      • source_message_ids.length >= 3, OR
      • the source messages span >= 2 distinct authors.
    For those cases, use post_sectioned_topic instead.

    Dispatcher also runs a canonical-key + similarity scan and returns
    matching existing topics as a tool error if it finds one — so you
    can re-decide (story update? update_topic_source_messages?
    different topic after all?).

    Collision override: when the dispatcher rejects with a collision, the
    agent re-submits with `override_collisions=[{topic_id: ..., reason: ...}, ...]`
    asserting each flagged match is a false positive. The dispatcher accepts
    the write and logs an `override` entry in topic_transitions for audit."""

@tool
def post_sectioned_topic(
    proposed_key: str,
    headline: str,
    body: str,
    sections: list[Section],          # REQUIRED, >= 1 section, one per contributor/angle
    source_message_ids: list[str],
    parent_topic_id: str = None,
    notes: str = None,
    override_collisions: list[OverrideCollision] = None,
) -> None:
    """Publish a multi-contributor or multi-source topic to the live feed.

    Renders as one header message + one Discord message per section.
    Use whenever source_message_ids span multiple authors or 3+ messages.
    Empty sections array is rejected. Same canonical-key collision
    handling + override path as post_simple_topic."""

@tool
def watch_topic(
    proposed_key: str,
    headline: str,
    why_interesting: str,
    revisit_when: str,                # ISO timestamp or natural language
    source_message_ids: list[str],
    override_collisions: list[OverrideCollision] = None,
) -> None:
    """Place a topic in watching state. Promising but not ready —
    will be re-evaluated on future runs via the watching-topics payload.
    Same canonical-key collision handling + override path."""

# Shared shape for the collision-override parameter on the three write tools.
class OverrideCollision(TypedDict):
    topic_id: str                    # the existing topic the dispatcher flagged
    reason: str                      # 1-2 sentences, agent's justification
                                     # (logged to topic_transitions.payload)

@tool
def update_topic_source_messages(
    topic_id: str,                    # resolved existing topic_id, not a proposed_key
    new_source_message_ids: list[str],
    note: str = None,
) -> None:
    """Append new source messages to an existing topic without re-posting.
    Use for ongoing discussion of an already-covered story that doesn't
    represent a concrete new development worth a story update."""

@tool
def discard_topic(
    topic_id: str,
    reason: str,
) -> None:
    """End a watching topic that's gone dead. Sets state='discarded' for
    audit. Only valid on topics currently in 'watching' state."""

@tool
def record_observation(
    source_message_ids: list[str],
    observation_kind: str,            # 'near_miss' | 'considered'
    reason: str,
) -> None:
    """OPTIONAL. Record that you considered acting on a cluster of source
    messages but decided not to. Writes to editorial_observations for
    debugging visibility — POM can later ask 'why didn't you post X?'
    Use sparingly: only for items that were close to qualifying, not for
    routine chatter. No upper bound but treat as ~0-3 per run."""

# (No ignore_topic tool: routine chatter the agent decides isn't worth
# covering simply gets no tool call. Near-misses are captured via
# record_observation when worth flagging.)
```

**Dispatcher invariants enforced server-side (not prompt-side):**

1. `post_simple_topic` is rejected when `distinct_author_count(source_message_ids) >= 2` OR `len(source_message_ids) >= 3`. The agent receives the rejection as a tool error and must retry with `post_sectioned_topic`.
2. Every `post_*` / `watch_topic` call runs through server-side canonicalization + a similarity scan against existing topics. If a match is found AND the agent didn't pass `override_collisions` for that match, the call is returned as a tool error with the existing `topic_id`, canonical_key, and aliases — so the agent can re-decide (story update via `parent_topic_id`, `update_topic_source_messages`, override as false positive, or different topic after all). The agent isn't *required* to search first, but it can't accidentally fracture a story either. **The override path is essential**: without it, a false-positive collision (the agent and dispatcher disagreeing on whether two topics are the same) traps the agent in a retry loop with no exit. Every override is logged to `topic_transitions` with the agent's reason for audit and for tuning the similarity threshold.
3. Each write tool's call is idempotent on `(run_id, tool_call_id)` — Anthropic's `tool_use_id` is the natural idempotency key.
4. A `(topic_id, message_id)` pair is unique (enforced by `topic_sources` UNIQUE constraint). One message can support multiple topics (parent + later child for a story update), but a topic can't accidentally add the same message twice.

Every dispatcher rejection — schema check failure (#1), collision without override (#2), idempotency replay (#3), source uniqueness (#4) — also writes a `topic_transitions` row with `action='rejected_*'` and `topic_id=NULL` (for create attempts), so the rejection rate by category is queryable.

These four invariants close the three predicted-bug categories from the agent reviewer's v4 review and the collision-loop bug Codex flagged at v5. v5 swapped invariant #2 from a ritual ("you must pass a search_result_id") to a substantive check; v6 adds the override path so the substantive check doesn't itself become a trap.

**Section schema:**

```python
class Section(TypedDict):
    caption: str                    # 4-10 words, no period
    source_message_id: str
    source_channel_id: int | None
    source_thread_id: int | None
    media_urls: list[str]           # up to 4
```

**Agent loop:**

The model receives the source messages + watching-topics payload + short prompt + tools. It reasons (with extended-thinking blocks if useful), calls search tools when it needs to verify, and ends with one decision tool call per topic it identified. Anthropic's API handles tool_use_id boundaries — no JSON parsing layer, no candidates-vs-tool_requests distinction, no `editor_reasoning` narration that doesn't fire.

A typical run looks like:

1. Agent reads 87 new source messages + 3 watching topics.
2. Agent thinks: "These messages look like they're about OmniNFT for LTX 2.3, plus a separate OpenCS2 release, plus chatter about ComfyUI updates."
3. Agent calls `search_topics(query="OmniNFT LoRA LTX")` → no existing match.
4. Agent calls `search_topics(query="OpenCS2 Counter-Strike dataset")` → no match.
5. Agent calls `get_author_profile(228118453062467585)` → confirms Gleb is a recurring contributor.
6. Agent calls `post_sectioned_topic(proposed_key="omninft-lora-ltx23-2026-05-13", sections=[...])` — multi-section enforced by tool choice.
7. Agent calls `post_simple_topic(proposed_key="opencs2-dataset-2026-05-13", media=[...])` — single author, single message, simple form. Dispatcher's collision scan finds no match; write proceeds.
8. Agent does nothing about the ComfyUI update chatter — no tool call, no row (or records one `record_observation` if it was close to qualifying).

Done. The dispatcher's collision scan runs on every write, so even if the agent had skipped the explicit `search_topics` calls a near-duplicate would surface as a tool error before persisting.

### Stage 2: topic publisher (pure function)

```python
def render_topic(topic: Topic) -> list[DiscordMessage]:
    """Map a posted topic to its Discord representation.

    Pure function. No DB access, no decision logic.
    """
    summary = topic.summary
    if summary["structure"] == "simple":
        return [render_simple(topic, summary)]
    return [render_header(topic, summary)] + [
        render_section(topic, section) for section in summary["sections"]
    ]
```

**Simple** = one Discord message: `**headline**` + body + first 4 media URLs + `**Original post:** <jump-link>`.

**Multi-section** = N+1 Discord messages: a header (`## headline` + body + primary jump-link) followed by one section per contributor (`▸ caption` + media URLs + jump-link to that specific source message).

**Story update** = a single message starting with `Update: ...`, with a header-link to `parent_topic.discord_message_ids[0]`.

This stage has no branching on "are examples set?" or "should we split into multiple messages?" — the decision was made upstream by the agent's tool choice + structure parameter. The publisher just renders.

## The prompt

```
You are the BNDC live-update editor for Banodoco, an AI art / generative
video community on Discord. Each hour you decide what to do with new
messages and ongoing watched topics.

For each topic worth acting on, call one tool:
- post_simple_topic — single-author, 1-2 source messages
- post_sectioned_topic — multiple authors OR 3+ source messages
- watch_topic — interesting but not ready; revisit later
- update_topic_source_messages — append to an existing topic without re-posting
- discard_topic — end a watching topic that's gone dead
- record_observation — OPTIONAL, capture a near-miss for debugging

For routine chatter, support questions, intros, BNDC bot telemetry, or
anything else not worth covering: do nothing. No tool call needed.

Call search_topics whenever a cluster of messages might be a continuation
of something we've covered. Search results include all known aliases for
each topic. The dispatcher also runs a collision check on every write
and will surface matching existing topics as a tool error — so a
near-duplicate can't slip through even if you forgot to search.

If the dispatcher flags a collision you genuinely believe is a false
positive (different story under similar slug), re-submit the same tool
with override_collisions=[{topic_id, reason}, ...]. Use this when you've
read the flagged topic and confirmed it's unrelated — not as a shortcut
when you're unsure. Every override is logged with your reason.

If a search_topics result (or a write-time collision) matches your current cluster of messages:
- Concrete new development on top? → post_simple_topic or post_sectioned_topic
  with parent_topic_id set (story update). Examples: a fix for a known
  issue, a novel experiment by another member, a performance milestone,
  third-party validation, a new variant.
- More conversation but nothing concrete? → update_topic_source_messages
  to keep the source trail current without re-posting.
- Watching topic that's gone dead? → discard_topic.

Bar for post_simple_topic / post_sectioned_topic:
- Genuinely NEW (release, fix, discovery, novel experiment, milestone)
- Concrete artifact (link, repo, file, demonstrable result)
- Community-validated (reactions or replies from members with history here)

Missing one of those? Use watch_topic — that's how we follow stories that
aren't ready yet. Set revisit_when based on what signal you're waiting for
("revisit when community tests it" → ~24h; "waiting for release date" →
specific timestamp).

Watching topics in the input payload should always be re-evaluated this
run. For each: signal arrived → post_simple_topic/post_sectioned_topic
with parent_topic_id, still developing → leave it, gone dead → discard_topic.

Tool choice = structure choice. post_simple_topic produces a single
Discord message with body + media. post_sectioned_topic produces a header
message + one Discord message per section (one section per contributor or
angle). The dispatcher will reject post_simple_topic if your source
messages span multiple authors or include 3+ messages — use
post_sectioned_topic for those.

Editorial taste:
- No hyperbole. Don't inflate. If someone made a nice workflow, don't
  call it groundbreaking.
- Preserve conditions. "X under condition Y", not "X is a breakthrough".
- Credit creators with bold names: "**username**".
- Prioritize community contributions over commercial announcements.
- High-signal members carry more weight, but reputation is a weak signal —
  it can support validity, not prove factual claims.

proposed_key conventions: lowercase, hyphenated, includes creator/artifact
and date. Examples: ostris-toolkit-hidream-lora-2026-05-13,
kijai-comfyui-ltx-weighting-2026-05-13. The dispatcher canonicalizes your
proposed_key and stores variants as aliases, so close-but-not-identical
slugs across runs still resolve to the same topic.
```

About 1500 characters. No FORCING RULEs, no HARD RULEs, no TRIAGE tables, no editorial_checklist, no "exactly one JSON object" instruction.

## What goes away

| Today | Redesign |
|---|---|
| 5 overlapping dedupe surfaces | `topics.canonical_key` unique + `topic_aliases` + `topic_sources` unique + `search_topics` tool |
| 2 parallel decision mechanisms (JSON `decision` field + separate tool calls) | All decisions are tool calls, native tool-use |
| `candidates` + `decisions` + `feed_items` + `duplicate_state` + `editorial_memory` tables | `topics` (with delivery state columns) + `topic_sources` + `topic_aliases` + `topic_transitions` + `editorial_observations` (5 tables, each with one job, no overlap) |
| Per-chatter rejection rows | sampled `editorial_observations` for near-misses; routine chatter writes nothing |
| 8 KB prompt | ~1500-char prompt |
| Text-mode JSON regex parsing | Native tool_use boundaries |
| App-side 11-gate `editorial_checklist` | Dispatcher invariants on a few critical structural points (multi-section, search-first, idempotency, source-message uniqueness) |
| `decision='defer'`/`'skip'`/`'duplicate'` values | Choose a tool or don't call one |
| Multi-JSON parser bugs | Impossible by API design |
| Watchlist narration → noop bugs | Impossible (call the tool or don't) |
| Examples-missing rejections | **Structural**: dispatcher rejects `post_simple_topic` for multi-author/3+ source clusters |
| Story-update duplicate_key suffix conventions | `parent_topic_id` field |
| `LIVE_UPDATE_TYPES` enum | doesn't exist; state + parent linkage covers it |
| Per-candidate `confidence`/`why_now`/`new_information`/`rationale`/etc. | Optional `notes` string + the normalized `topic_transitions` event log |
| Agent-authored slug fragility | Server-side canonicalization + `topic_aliases` |
| Lost telemetry on "model wanted to publish, schema blocked" | Captured in `topic_transitions.payload` on rejected tool calls |
| Discord partial-publish failure (untracked today) | `topics.publication_status` + `publication_error` + `discord_message_ids` |

## What stays

- Discord ingestion path and `discord_messages` archive table.
- Anthropic SDK + Claude Opus 4.7.
- Runner / cron / dev-vs-prod environment split.
- Bot's posting permissions and channel routing.
- Editorial *taste* (what's interesting, who's high-signal, no hyperbole) — moves from prompt rules to a short style guide.
- `live_update_editor_runs` table for run-level stats (count, duration, errors). Per-topic outcomes live on `topics.transitions`.
- The existing `discord_messages` search infrastructure (already used by `search_messages`).

## What we'd lose (honest list)

The v1 draft listed 12 losses. The v4 design (multi-table + split decision tools + alias system + observations table) mitigates most of them. Remaining real losses below.

### Behavioral

1. **The `defer` outlet disappears.** Currently `decision='defer'` means "surface but don't post — needs human review". Redesign has post-simple/post-sectioned/watch/update/discard, no defer. If "needs human review" is a workflow actually used (audit prod), it has to come back as a tool or a state. Likely never used in practice — verify before assuming it's needed.

2. **The `REASONING:` prefix commitment.** Forcing the model to summarize before emitting JSON acts as a soft pre-commitment — readable even when output is malformed. Native tool-use mode replaces this with extended-thinking blocks (different flavor of the same thing) and with the `notes` parameter on each decision tool.

3. **Free-form prose between JSON blocks.** Sometimes that prose surfaces nuance the structured fields don't capture. Tool-use doesn't allow it. *Partly a feature* — that pattern is what caused the multi-JSON parsing bug.

### Flexibility

4. **Harder to bolt on new editorial concepts cheaply.** Today adding a new concept means: small prompt change, maybe a new field on candidates. Redesign means: new tool definition, new schema, new prompt example, possibly a new publisher branch. ~2 places instead of ~1. The current plasticity is what got us into 8K-prompt territory, but it's real flexibility.

5. **Lost optionality on weird future features.** "This topic needs human approval before posting" — today: add a decision value + a Discord button. Redesign: new tool + new state + propagation. Sharper design makes some adjacencies harder; those adjacencies are also rarer.

### Operational

6. **More tool calls per run = higher API cost + latency.** Each editorial pass now involves the agent calling 2-5 search tools + 3-8 decision tools. At hourly cadence: fine. At minute-by-minute: would add up. Not a concern at current volume.

### Mitigated by the v4 design (no longer real losses)

- ~~Per-row SQL audit granularity~~ — `topic_transitions` is a normalized events table; ordinary indexes apply.
- ~~`raw_agent_output` debug fidelity~~ — `notes` parameter + `topic_transitions.payload` carry the same audit data.
- ~~Visibility into "model wanted to publish but schema blocked it"~~ — rejected tool calls log to `topic_transitions` with `action='rejected_*'` and `topic_id=NULL`.
- ~~Agent might invent different topic_key slugs~~ — server canonicalization + `topic_aliases` table.
- ~~Cross-channel story-matching~~ — `search_topics` returns canonical key + aliases.
- ~~"No row for ignored chatter = no debugging visibility"~~ — `editorial_observations` captures near-misses without storing every routine message.
- ~~Migration not reversible~~ — old tables kept as `_legacy_*` for 2+ weeks; runner-pointer flip is the rollback.
- ~~Collision false-positive traps the agent in a retry loop~~ — `override_collisions` parameter on the three write tools (v6); every override logs to `topic_transitions` for audit and threshold tuning.

### Operational trip-wires (revisit decisions if these fire)

- **If partial-publish incidents happen twice during shadow-mode or prod**, restore the separate `topic_publications` table for full per-attempt history. The folded-into-`topics` form keeps only the latest attempt's status/error; that's fine for normal operation, painful during a publishing-incident forensics review.
- **If `record_observation` is invoked <1× per run on average**, drop the tool and the table — the agent has decided the "near-miss debugging visibility" feature isn't earning its prompt surface area.
- **If override rate exceeds ~10% of write calls**, the collision similarity threshold is too tight. Loosen the trigram threshold or add author-overlap weighting before the override path becomes load-bearing.

## Migration plan

Two phases. Phase 1 is a **one-week shadow-mode dev build** that produces a system POM can watch, distrust, tune, and only then promote. Phase 2 is the prod cutover and runs on its own timeline — explicitly **not** committed-to as part of this week. The v1 doc's "3-day prod migration" framing was unrealistic; all three reviewers flagged it.

### Phase 1: shadow-mode dev build (~1 week focused, as a vertical slice)

**Goal:** a runnable system in dev that takes real source-message windows, makes editorial decisions via the new tool-use loop, writes to the new tables, **and does not publish to Discord**. Comparison artifacts (side-by-side trace embeds in `#main_test`) let POM judge whether the new agent reaches equivalent or better decisions than today's.

**Scope discipline — what one week actually buys.** One focused week of POM-time delivers a *vertical slice*: schema + tool-use loop + pure renderer + trace embed format + 20-window replay against historical data. It does NOT deliver a polished drop-in replacement. The Codex v5 review called out specifically what pushes Phase 1 into two-to-three weeks if you let it expand: Anthropic tool-use integration through the existing SDK wrapper, Supabase migration friction (the unmerged migration backlog already in `.migrations_staging/`), canonicalizer threshold tuning, trace-embed UX iteration, run-lease/checkpoint behavior, and the comparison baseline being itself noisy ("today's editor would have done it differently" vs. "today's editor was wrong"). Treat anything in that list as Phase-1.5 — out of scope for the first focused week.

**Day 1 — Schema + dual-write of historical data.**

- Apply the 5 new tables (`topics` with publication columns, `topic_sources`, `topic_aliases`, `topic_transitions`, `editorial_observations`).
- Backfill: existing 18 prod `feed_items` → `topics` rows with `state='posted'` + populated publication columns + corresponding `topic_sources` rows. Existing `watchlist` (once unblocked) → `topics WHERE state='watching'`.
- Leave the existing editor and old tables fully untouched. No dual-write to old tables from the new editor yet.

**Day 2 — Native tool-use editor scaffold.**

- Replace `live_update_editor.py`'s `messages.create`-based agent loop with Anthropic's native `tool_use` API.
- Implement the 4 read tools + 6 decision tools described above.
- Implement the dispatcher invariants (post_simple_topic structural check, write-time canonical-key + similarity collision scan, idempotency on tool_use_id, topic_sources uniqueness).
- Implement key canonicalization + alias resolution.
- New editor writes ONLY to the new tables. Publishing path is disabled (no Discord sends — but render the output and post a *preview* trace embed in `#main_test` so POM can see what it would have posted).

**Day 3 — Pure publisher + trace embed.**

- `render_topic(topic)` pure function with test coverage (simple, sectioned, story-update, partial-publish recovery).
- New trace embed format in `#main_test`: source-message count, tool calls fired, topics posted/watched/updated/discarded, near-miss observations, cost (tokens / $), latency, comparison vs. today's editor output.
- Run hourly in dev with publishing OFF.

**Days 4-5 — Replay + tune.**

- Replay 20+ recent source-message windows from the prod archive through the new editor (publishing still off).
- For each, compare against what today's editor decided.
- Tune prompt + dispatcher behavior until divergences are explainable as "agent made a valid different call" rather than "agent drifted."
- Verify slug-drift handling: deliberately roll a window twice through different runs, confirm aliases resolve cleanly.

**End of Week 1 — Decision gate.**

POM evaluates the shadow-mode run logs and answers: *would I be comfortable letting this system publish to prod?* Pass → Phase 2. Fail → tune, extend, or abandon.

### Phase 2: prod cutover (separate timeline, not committed-to this week)

Roughly 2-3 days of focused work AFTER Phase 1 passes its gate:

- Enable publishing on the new editor.
- Run new + old editor concurrently in prod for 24-48h. Both write to their own tables. Only new editor publishes to Discord; old editor's posts are suppressed.
- Watch for partial-publish failures via `topics.publication_status='partial'`.
- Once stable, rename old tables to `_legacy_*`. Schedule deletion in 2-4 weeks.

### Deferred to v2 (not in scope for either phase)

- `pending_publish` / `publish_failed` states beyond what `topics.publication_status` already encodes; full retry-history table if partial-publish failures turn out to need per-attempt logging
- `pending_review` outlet (human-curator workflow) — only needed if `defer` audit shows real demand
- Manual merge tool for aliased topics that should collapse together
- Sampling strategy tuning for `editorial_observations` (start with "agent's discretion")

### Code scope

Approximately 800-1000 lines replacing the current ~3000 lines of `live_update_editor.py` + `live_update_prompts.py` + their storage methods. Tests stay similar in count, mostly easier to write because the pieces are decoupled.

## Five decisions to lock before starting

The reviewers identified five design choices that, if wrong, would cost real rework mid-build. Decide all five before Day 1:

1. **Canonical key generation algorithm.** Server-side canonicalization needs a concrete rule: lowercase + hyphen-replace + strip articles + anchor on creator/artifact/date when extractable. Worth writing the canonicalizer first, fuzzing it against 50 historical headlines, and verifying it produces stable keys before the new editor runs. If extractor heuristics produce too many collisions or too many near-duplicates, the `topic_aliases` system absorbs it, but better to know upfront.

2. **`editorial_observations` retention + sampling policy.** Storing every near-miss is overkill; storing none defeats the purpose. Recommended starting policy: agent calls `record_observation` at its discretion, capped at ~3 per run by prompt guidance. Sample retention: keep all observations from the last 30 days for debugging. Worth choosing now because it affects the prompt and the table cardinality estimate.

3. **Collision similarity threshold + override semantics.** The dispatcher's collision scan needs a concrete similarity score and threshold (canonical-key prefix match? trigram similarity on headline? source-author overlap?). Too tight → agent constantly overrides false positives and the override path becomes load-bearing. Too loose → fractured topics slip through. Recommended starting policy: dispatcher returns collision if (canonical-key-prefix match) OR (trigram_similarity(headline, candidate) >= 0.55 AND author_overlap >= 1). Phase 1 includes deliberate adversarial pairs (near-duplicates + spurious matches) in the replay set so the threshold gets tuned against real data.

4. **Rejected-call logging shape.** v6 specs rejected dispatcher calls as `topic_transitions` rows with `topic_id=NULL`, `action='rejected_*'`, and rejection details in `payload`. Confirm this is the schema you want before the migration ships — alternative is a separate `tool_call_log` table. The single-table shape is simpler; the separate-table shape keeps `topic_transitions` indexes lean if rejection volume is high. Recommendation: stay single-table; revisit if rejection rate exceeds 50% of total writes.

5. **`defer` audit + `pending_review` schema.** Current production code at `live_update_editor.py:1835` actually emits `decision='defer'` for low-confidence candidates — it's not unused. Before Day 1, query the live `live_update_candidates` table: how many `defer` decisions in the last 30 days, and what did POM do with them? If `>5/week and acted upon`, add `state='pending_review'` to the topic state machine and a `request_review_topic` tool now, not in v2. If `unused`, drop. Don't guess — the answer changes whether Day 1 schema is final or rewritten in Phase 2.

## Other open questions

- **The `defer` outlet.** Audit current prod usage. If never used: drop. If used 1-2 times: log it as a v2 feature. If used often: design a `pending_review` outlet before starting.

- **Concurrent-run protection.** Lease via `live_update_editor_runs` keyed by `(environment, guild_id, live_channel_id)` so a slow run can't be doubled by a retry. Worth implementing in Phase 1.

- **Cross-environment `search_topics`.** Default scoped to current env. Cross-env search is a debug-only opt-in.

- **Backfill of historical topics.** The 18 existing prod feed_items + any dev watchlist entries become the initial topic corpus. `topic_sources` rows generated from `discord_message_ids`. The agent has a small but real search history on day 1.

- **Cost-per-run budget.** Native tool-use with 5-10 tool calls per run costs roughly 2-3× today's per-run token spend. At hourly cadence and Banodoco's volume, still trivial — but worth tracking via a `total_tokens` field on `live_update_editor_runs` so cost regressions are visible.

## Recommendation

Build it. The current architecture's complexity isn't going to decrease on its own — every editorial concept we add from here makes it worse, and the prompt is already past the point where iteration is productive.

Codex's v5 review confirmed v5 is structurally ready for execution; v6 adds the collision-override path it flagged as missing, fixes two migration-blocking schema bugs (`topic_transitions` was missing `environment` / `guild_id`, and `topic_id` couldn't be null for rejected create attempts), loosens `topic_sources` UNIQUE to allow one message to support multiple topics (real for parent + child story updates), and expands the lock-before-Day-1 list from two decisions to five.

Three biggest design wins through v6:

1. **Multi-table topic model with one job per table.** Editorial state + delivery state (`topics`), source ownership (`topic_sources`), identity resilience (`topic_aliases`), event log (`topic_transitions`), observability (`editorial_observations`). Each table indexable normally, no JSON-path queries for operational work.

2. **Tool choice IS the structural decision.** `post_simple_topic` vs. `post_sectioned_topic` makes the wall-of-media bug structurally impossible — dispatcher rejects the wrong shape at submission time. The dispatcher's write-time canonical-key + similarity scan catches slug-drift duplicates substantively instead of via a search_result_id ritual.

3. **Native tool_use end-to-end.** No JSON parsing layer, no candidates-vs-tool_requests distinction, no narrate-without-invoke bugs, no multi-JSON parser bugs. Schema and dispatcher invariants do the structural enforcement; the prompt only handles editorial taste.

The honest summary: the system has too many overlapping concepts for what's fundamentally a three-bucket problem, and the prompt is a fair receipt for that overlap. Reducing the conceptual surface area is where the next 10× of clarity comes from. Phase 1 (shadow-mode in dev with publishing off) is one focused week and produces a system POM can watch before trusting. Phase 2 (prod cutover) is its own decision, made after the shadow-mode logs prove the new agent's calls are equivalent-or-better. Don't commit to both this week.
