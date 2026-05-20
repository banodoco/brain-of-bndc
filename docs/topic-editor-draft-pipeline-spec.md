# Topic Editor Draft Pipeline Spec

## Goal

Turn the hourly topic editor from "agent summarizes and publishes" into a small editorial workflow:

1. Research
2. Decide
3. Draft
4. Validate
5. Revise
6. Preview
7. Submit

The output should feel like short BNDC live updates: concise, source-backed, visual when possible, and broken into a few focused cards rather than a long digest.

## Outcomes

- The agent works on an editable draft document before publishing.
- Drafts are validated before submit.
- Bad drafts re-enter the editorial loop for revision.
- Posts are short, card-based, citation-linked, and media-interleaved.
- Publishing failures become rare infrastructure issues, not formatting issues.
- Health/reporting distinguishes no data, draft validation failure, abandoned draft, media failure, and partial publish.

## Existing Context

The current active system uses `topic_editor_runs`, `topics`, `topic_sources`, `topic_transitions`, and `editorial_observations`. The older `live_update_*` tables are rollback/legacy state.

Recent production failures show three concrete problems:

- Structured text blocks can exceed Discord limits and produce `partial` / `failed` publications.
- References often fall back to detached `Sources:` footers rather than inline linked citations.
- The agent tends to write digest-style bodies instead of short visual editorial cards.

The inline citation work in `brief-inline-citation-20260519-1559` is a useful renderer-level building block: draft text can contain `[1]`, `[2]` markers, and the renderer can convert them into Discord masked links.

## Editorial Model

The editor is not summarizing everything it saw. It is curating a small public update.

The agent should leave material out when needed. If there are six interesting angles, it should pick the best two or three, split into separate topics, or keep the rest on the watchlist.

Each card should answer one of:

- What changed?
- What did someone make?
- What did the community learn?
- Why is this useful now?
- What is worth watching next?

If a card does not answer one of these, it probably should not be in the post.

## Draft Document

The publishable artifact is a first-class draft document.

```json
{
  "draft_id": "draft-123",
  "topic_key": "t2-relight-lora-shader-ball-matcap-may19",
  "template": "technical_finding",
  "headline": "T2 Trains Relight LoRA That Follows Shader-Ball References",
  "dek": "A new test shows the relight LoRA responding more directly to reference-ball lighting.",
  "cards": [
    {
      "angle": "What changed",
      "body": "T2 posted a new relight LoRA version trained to respect shader-ball references more closely [1].",
      "source_message_ids": ["1506344740558475356"],
      "media_ids": ["1506344740558475356:attachment:0"]
    }
  ],
  "editor_note": "Concrete community-made model progress with clear visual evidence."
}
```

## Evidence Shelf

Before drafting, source evidence should be normalized into an evidence shelf. The agent should select sources and media from this shelf rather than juggling raw URLs and attachment indexes.

```json
{
  "message_id": "1506344740558475356",
  "author": "T2",
  "content": "almost have this working...",
  "jump_url": "https://discord.com/channels/1076117621407223829/1309520535012638740/1506344740558475356",
  "reaction_count": 7,
  "media": [
    {
      "media_id": "1506344740558475356:attachment:0",
      "kind": "video",
      "source_url": "https://cdn.discordapp.com/...",
      "thumbnail_url": "https://media.discordapp.net/...",
      "description": "Side-by-side relighting comparison using shader-ball references.",
      "aesthetic_quality": 7,
      "editorial_notes": [
        "Best proof for the relight claim",
        "Place immediately after the card describing the shader-ball effect"
      ]
    }
  ]
}
```

## Templates

Start with four templates.

### Creation / Release

- Card 1: what was released or made.
- Card 2: what the generation/media shows.
- Card 3: why the community cares or what happens next.

### Technical Finding

- Card 1: the problem or discovery.
- Card 2: evidence or comparison.
- Card 3: workaround, implication, or next step.

### Tool / Workflow Update

- Card 1: what changed.
- Card 2: how someone tested it.
- Card 3: caveats, proof, or next step.

### Community Debate

- Card 1: the concrete question being debated.
- Card 2: strongest position A.
- Card 3: strongest position B.
- Optional card 4: what remains unresolved.

## Agent Tools

Add explicit draft tools inside the topic-editor loop.

### `create_draft`

Creates an editable draft from researched evidence.

Required fields:

- `topic_key`
- `template`
- `headline`
- `dek`
- `cards`
- `editor_note`

### `edit_draft`

Applies changes to an existing draft. The agent should edit the same draft rather than start over after every validation failure.

Inputs:

- `draft_id`
- `patch`
- `reason`

### `validate_draft`

Runs deterministic validation and returns blocking errors plus warnings.

Inputs:

- `draft_id`

### `preview_draft`

Returns the exact text/media units that Discord would receive, including media descriptions for agent review.

Inputs:

- `draft_id`

### `submit_draft`

Runs validation internally and refuses to publish if there are blocking errors.

Inputs:

- `draft_id`

### `abandon_draft`

Ends a draft attempt when the topic is not publishable after revision.

Inputs:

- `draft_id`
- `reason`
- optional `fallback_action`: `watch_topic`, `update_topic_sources`, or `discard_topic`

## Validation

Validation returns `errors` and `warnings`.

Errors block publishing.

Warnings should push revision, but do not block initially.

### Blocking Errors

- Any rendered text unit exceeds the Discord content limit.
- Any card body exceeds the configured maximum.
- Draft has more than the configured maximum number of cards.
- Card has no resolvable source.
- Inline citation marker `[N]` does not map to a card source.
- Source message cannot be resolved.
- Media id cannot be resolved and the card depends on it.
- References cannot render as Discord jump links.
- Same media repeats unnecessarily.
- `submit_draft` is called before a valid preview exists.

### Warnings

- Headline is too long.
- Dek is vague or repeats the headline.
- Card uses too many sources.
- No media is used despite strong media existing in the evidence shelf.
- Card combines multiple unrelated points.
- Draft reads like a digest or essay.
- Too many quotes.
- Media appears detached from the relevant text.
- Weak "community reacted" point without concrete substance.

### Validation Response

```json
{
  "status": "needs_revision",
  "errors": [
    {
      "path": "cards[0].body",
      "message": "Card is 1210 chars; max is 650.",
      "suggestion": "Keep the launch fact and one concrete community reaction."
    }
  ],
  "warnings": [
    {
      "path": "cards[1]",
      "message": "No media attached, but source 1506344740558475356 has a strong video asset."
    }
  ]
}
```

## Recommended Limits

Initial defaults:

- `headline`: target under 110 chars.
- `dek`: target under 220 chars.
- `card.body`: max 650 chars.
- `cards`: max 4.
- `revision_attempts`: max 3.
- `sources_per_card`: warning above 4.

These should be configuration values so agentic testing can compare thresholds.

## Revision Loop

For publish-worthy topics:

1. Agent creates draft.
2. System validates.
3. If errors exist, agent edits the same draft.
4. System validates again.
5. Agent previews.
6. Agent submits if valid.
7. If still invalid after the revision limit, agent abandons, watches, updates sources, or discards.

The loop is bounded. A topic that cannot be made into a good post should not thrash forever.

## Preview

Preview should render exactly what Discord would receive.

```json
[
  {
    "type": "text",
    "content": "## T2 Trains Relight LoRA That Follows Shader-Ball References\n\nA new test shows the relight LoRA responding more directly to reference-ball lighting."
  },
  {
    "type": "text",
    "content": "T2 posted a new relight LoRA version trained to respect shader-ball references more closely [1](https://discord.com/channels/...)."
  },
  {
    "type": "media",
    "media_id": "1506344740558475356:attachment:0",
    "description": "Side-by-side relighting comparison using shader-ball references.",
    "source_message_id": "1506344740558475356"
  }
]
```

Preview should include media descriptions so the agent can reason about the editorial presentation before submit.

## Publishing

Publisher responsibilities:

- Convert inline `[1]`, `[2]` markers into Discord masked links.
- Interleave each card's media immediately after that card.
- Chunk text only as a final safety net.
- Refresh or cache media where possible.
- Fall back cleanly for expired or oversized media.
- Record exact status: `sent`, `partial`, `failed`, or `suppressed`.

Chunking should not be the normal content-shaping mechanism. If the draft is too long, validation should send the agent back to edit it.

## Persistence

Add a minimal draft table.

### `topic_editor_drafts`

- `draft_id`
- `run_id`
- `topic_id`
- `guild_id`
- `environment`
- `status`: `drafting`, `needs_revision`, `valid`, `submitted`, `abandoned`
- `draft_json`
- `validation_result`
- `preview_units`
- `created_at`
- `updated_at`
- `submitted_at`

### Optional Later: `topic_editor_draft_versions`

- `draft_version_id`
- `draft_id`
- `version_number`
- `draft_json`
- `validation_result`
- `edit_reason`
- `created_at`

For the first sprint, latest-only draft storage is acceptable if versioning slows the vertical slice.

## Health And Reporting

Update health/reporting to distinguish:

- No recent source data.
- Topic-editor run failed.
- Draft validation failed.
- Draft abandoned after revision attempts.
- Publish failed.
- Publish partially succeeded.
- Media failed because URL expired.
- Media failed because payload too large.
- Renderer safety chunking was used.

The report should include latest drafts, validation failures, previews, and publication status.

## Decisions To Make

- Card body max: recommend 650 chars.
- Max cards: recommend 4.
- Max revision attempts: recommend 3.
- Missing inline citations: warning for one week, then error.
- No-media draft when strong media exists: warning, not error.
- Persist draft versions in sprint one or later: recommend later unless cheap.
- Prod publish immediately or shadow first: recommend shadow first.
- `submit_draft` timing: recommend writing/updating `topics` only after validation succeeds.

## Implementation Order

1. Add draft dataclasses/types and pure validation functions.
2. Add preview/render functions.
3. Add draft tools with run-local in-memory drafts.
4. Add publisher safety chunking.
5. Wire `submit_draft` into existing topic creation/publication path.
6. Add draft persistence.
7. Update prompt/templates.
8. Add evidence shelf media descriptions.
9. Add health/report script.

## Two-Week Sprint Slice

### Week 1

- Draft model.
- Pure validation.
- Preview rendering.
- Draft tools.
- Renderer/publisher safety.
- Initial prompt update.

### Week 2

- Draft persistence.
- Evidence shelf.
- Media descriptions in preview.
- Health/reporting.
- Real-data replay harness integration.
- First prompt/template iteration based on replay results.

## Done Criteria

- Agent can create, validate, revise, preview, and submit a draft.
- Invalid drafts do not publish.
- Long text cannot cause Discord partial publish.
- Cards render with clickable inline citations.
- Media appears next to the relevant card.
- Health report explains draft versus publish failures.
- Watch/update/discard behavior remains available.
- Existing topic-editor tests still pass.

