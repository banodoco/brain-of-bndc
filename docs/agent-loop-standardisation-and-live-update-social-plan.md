# Agent Loop Standardisation and Live Update Social Publishing Plan

## Purpose

Build one larger project that does two related things:

1. Standardise the shared mechanics used by the current editor loop and admin chat loop.
2. Add an agentic social publishing loop that runs after a live update has successfully posted to Discord and decides whether, how, and what to post to social media.

The goal is not to merge all agents into one generic agent. The goal is to share the runtime machinery, tool contracts, logging, media handling, and publishing primitives while keeping each loop's domain policy separate.

## Current Shape

### Topic Editor Loop

The topic editor is an autonomous editorial loop. It fetches recent Discord source messages, known topics, aliases, and shortlisted media, then asks the LLM to use tools to create, update, watch, discard, or finalize topics.

Relevant code:

- `src/features/summarising/topic_editor.py`
- `TOPIC_EDITOR_TOOLS`
- `TopicEditor.run_once`
- `TopicEditor._dispatch_tool_call`
- `TopicEditor._dispatch_read_tool`
- `TopicEditor._publish_topic`

Strengths:

- Has a real run lifecycle.
- Tracks run IDs, checkpoints, accepted/rejected outcomes, tool counts, token/cost estimates, trace embeds, and publish results.
- Has explicit finalization requirements.
- Has idempotency protection for write tools.
- Has image/video understanding and structured publishing.

Weak spots:

- Tool schemas, dispatcher paths, and storage backend methods can drift.
- Media understanding is coupled to `TopicEditor`.
- Tool result formatting is local to this loop.
- DeepSeek/OpenAI-compatible response conversion is being adapted to the editor rather than handled by a general agent runtime.

### Admin Chat Loop

The admin chat loop is a conversational operator loop. It receives user/admin instructions, injects channel context, calls tools, and replies or performs actions.

Relevant code:

- `src/features/admin_chat/agent.py`
- `src/features/admin_chat/tools.py`
- `execute_tool`
- `execute_share_to_social`

Strengths:

- Has a broad practical tool surface.
- Already has social posting tools.
- Handles conversational context, recent channel messages, replied-to messages, DMs, and aborts.
- Can suppress redundant chat replies when a posting tool already produced a visible side effect.

Weak spots:

- Tool dispatcher is a large `if/elif` chain.
- Logging is much lighter than the editor loop.
- It does not have the same structured run record, cost caps, or side-effect audit as the editor.
- Some useful tools are embedded in admin-chat-specific assumptions.

### Existing Social Publishing Stack

The repo already has a canonical social publish service.

Relevant code:

- `src/features/sharing/social_publish_service.py`
- `src/features/sharing/models.py`
- `src/features/sharing/providers/x_provider.py`
- `src/features/sharing/providers/youtube_zapier_provider.py`
- `src/features/sharing/sharing_cog.py`
- `src/features/admin_chat/tools.py::execute_share_to_social`

Strengths:

- Supports immediate and queued publishing.
- Records rows in `social_publications`.
- Has route resolution through `social_channel_routes`.
- Supports Twitter/X post, reply, quote, retweet, and YouTube via Zapier.
- Has scheduler/retry handling for queued publications.

Weak spots:

- `SocialSourceKind` currently does not include a dedicated live-update social source.
- Direct media publishing and Discord-message publishing have different call shapes.
- The current admin-chat social tool is useful but too operator-oriented to be the only abstraction for autonomous social publishing.

## Target Architecture

### Keep Three Product Loops

There should be three separate product loops:

1. `TopicEditor`
   Decides what Discord live updates should exist.

2. `AdminChatAgent`
   Responds to direct user/admin requests.

3. `LiveUpdateSocialAgent`
   Runs after a live update is posted and decides whether/how to share it externally.

They should not share prompts or high-level policy. They should share the runtime, tool model, logging shape, message/media utilities, and social publishing transport.

### Add Shared Runtime Primitives

Create a small shared package, likely:

```text
src/common/agent_loop/
  __init__.py
  runner.py
  models.py
  tools.py
  llm.py
  recorder.py
  policies.py
```

Suggested core types:

```python
AgentLoopRunner
AgentLoopPolicy
AgentTool
AgentToolRegistry
AgentToolCall
AgentToolResult
AgentLoopResult
AgentRunRecorder
LLMToolResponse
```

The runner owns:

- LLM invocation.
- Provider response normalization.
- Tool-call extraction.
- Tool dispatch sequencing.
- Tool result conversion.
- Max-turn handling.
- Token/cost accounting when available.
- Common logging.
- Common exception handling.

Each loop policy owns:

- System prompt.
- Initial user payload.
- Context injection.
- Tool allowlist.
- Tool argument injection.
- Stop condition.
- What to do when the model returns text without tools.
- How to classify side effects.
- Completion/failure persistence.

## Shared Tool Model

Introduce a proper tool registry rather than separate hand-written tool lists and dispatch chains.

Example:

```python
AgentTool(
    name="search_messages",
    description="Search Discord messages.",
    input_schema={...},
    handler=search_messages_handler,
    permissions={"read_messages"},
    result_mode="json",
)
```

The registry should support:

- Tool schema export for Anthropic-style APIs.
- Tool schema export for OpenAI/DeepSeek-style function tools.
- Runtime allowlists per loop.
- Startup validation that each selected tool has a handler.
- Backend validation that required DB/storage methods exist.
- Permission metadata.
- Result-size limits and truncation policy.
- Side-effect metadata, such as `read`, `write`, `publish`, `reply`, `social_publish`.

Initial shared tools worth extracting:

- `search_messages`
- `get_message_context`
- `get_reply_chain`
- `inspect_message`
- `understand_image`
- `understand_video`
- `list_social_routes`
- `share_to_social`
- `draft_social_post`
- `skip_social_post`

Loop-specific tools can remain loop-specific:

- Topic editor: `post_sectioned_topic`, `watch_topic`, `discard_topic`, `finalize_run`.
- Admin chat: `reply`, `end_turn`, payment tools, moderation tools.
- Social loop: `publish_social_thread`, `enqueue_social_thread`, `record_social_decision`.

## LLM Response Normalisation

DeepSeek currently returns an Anthropic-like response object to fit the topic editor. That should move into a shared layer.

Add a provider-neutral shape:

```python
LLMToolResponse(
    assistant_content=[...],
    tool_calls=[...],
    reasoning_text="...",
    usage={...},
    raw_response=...
)
```

Provider adapters should normalize:

- Anthropic Claude content blocks.
- DeepSeek/OpenAI-compatible tool calls.
- Reasoning content.
- Usage fields.
- Assistant messages with tool call IDs.

This prevents each loop from needing its own `_extract_tool_calls`, `_assistant_content_from_response`, and OpenAI compatibility shims.

## Run Logging and Observability

The new runtime should emit a consistent run record for every agent loop.

Minimum fields:

- `run_id`
- `loop_name`
- `guild_id`
- `channel_id`
- `trigger`
- `model`
- `provider`
- `status`
- `started_at`
- `completed_at`
- `turn_count`
- `tool_call_count`
- `tool_error_count`
- `input_tokens`
- `output_tokens`
- `cost_usd`
- `forced_close_reason`
- `final_decision`
- `side_effects`
- `error_message`

There are two implementation options:

1. Add a generic `agent_runs` and `agent_tool_calls` table.
2. Keep existing specific tables and add a shared recorder abstraction that writes into each loop's existing tables.

Recommendation:

Start with a recorder abstraction that can write to existing topic editor tables, while the new social loop gets a dedicated table. Add generic tables only if migration cost is acceptable.

For the social loop, add:

```text
live_update_social_runs
  run_id
  topic_id
  guild_id
  live_update_discord_message_ids
  source_message_ids
  status
  decision
  decision_reason
  selected_media_refs
  selected_publish_units
  social_publication_ids
  draft_text
  error_message
  metadata
  started_at
  completed_at
```

Potentially add:

```text
live_update_social_decisions
  id
  run_id
  topic_id
  decision
  payload
  created_at
```

## Live Update Social Agent

### Trigger

After `_publish_created_topics` returns publish results, enqueue a social review for each topic with `status in {"sent", "partial"}` and at least one Discord message ID.

The trigger should be best-effort. Failure to enqueue social review must not mark the Discord live update as failed.

Possible trigger point:

- After `TopicEditor._publish_created_topics(...)`
- Or from a small worker that scans topics with `publication_status in ("sent", "partial")` and no social decision yet.

Recommendation:

Use a queued worker instead of inline social posting. Inline can still be enabled later for dev mode.

### Input Packet

The social agent should receive a structured packet, not scrape text back out of Discord.

Suggested input:

```json
{
  "topic": {
    "topic_id": "...",
    "headline": "...",
    "summary": {...},
    "canonical_key": "...",
    "state": "posted",
    "publication_status": "sent",
    "discord_message_ids": [...]
  },
  "publish_units": [
    {"kind": "text", "content": "..."},
    {"kind": "media", "url": "...", "ref": {...}},
    {"kind": "external", "url": "...", "ref": {...}}
  ],
  "source_messages": [...],
  "source_metadata": {...},
  "media_understandings": {...},
  "recent_related_context": [...],
  "social_route_context": {...}
}
```

The packet should include the same `publish_units` that Discord publishing used, because they preserve section order and media association.

### Decisions

The social loop should make one explicit terminal decision:

- `skip`
- `draft`
- `approve_post`
- `needs_review`

The model decision should describe editorial intent. Runtime configuration decides whether `approve_post` is stored as a draft, enqueued, or published immediately.

Later, once single-post media publishing is reliable, the decision set can expand to thread, quote, and reply strategies.

### Tools

Suggested social loop tools:

#### Read Tools

- `get_live_update_topic`
- `get_published_update_context`
- `get_source_messages`
- `search_messages`
- `get_reply_chain`
- `understand_image`
- `understand_video`
- `list_social_routes`
- `find_existing_social_posts`

#### Drafting Tools

- `draft_social_post`
- `draft_social_thread`
- `select_social_media`

These tools can be pure write-to-run-state tools, not external side effects.

#### Publishing Tools

- `enqueue_social_post`
- `publish_social_post`
- `skip_social_post`
- `request_social_review`

Publishing should call `SocialPublishService`, not a separate X/Twitter client.

## Media Handling for Social Posts

This is the highest-risk part and should be treated explicitly.

### Media Sources

The social loop can use:

1. Original Discord attachments from source messages.
2. External media resolved during live update publishing.
3. Direct URLs from embeds where compatible.
4. Generated media only if a future generation tool explicitly creates it and records provenance.

It should avoid:

- Scraping just-posted Discord live-update message text as the source of truth.
- Reusing expired Discord CDN URLs without refresh.
- Posting a Discord message link when the actual media file is needed.

### Media Resolution Contract

Create a shared `MediaRef` model:

```python
MediaRef(
    message_id="...",
    kind="attachment" | "embed" | "external",
    index=0,
    url="...",
    content_type="video/mp4",
    filename="...",
    source="discord" | "twitter" | "reddit" | "youtube" | "external",
    understanding_id="...",
)
```

Create a shared resolver:

```python
resolve_media_ref(ref, source_metadata, *, refresh=True, target="discord"|"social")
```

For social publishing, the resolver should return either:

- a downloadable local file in `media_hints`, or
- a stable direct URL that the provider can download.

### Vision Understanding

The social loop should use image/video understanding before deciding how to tweet media-heavy posts.

For example:

- If the topic is a technical release with no compelling media, it can draft a text-only tweet.
- If the topic is a visual generation, it should inspect the media and write copy grounded in what the image/video actually shows.
- If understanding fails, it can still post cautiously using factual source text, or choose `needs_human_review`.

## Social Publishing Contract

Extend `SocialSourceKind`:

```python
SocialSourceKind = Literal[
    "admin_chat",
    "reaction_bridge",
    "summary",
    "reaction_auto",
    "live_update_social",
]
```

Create social loop requests using:

```python
SocialPublishRequest(
    message_id=primary_live_update_discord_message_id,
    channel_id=live_update_channel_id,
    guild_id=guild_id,
    user_id=0,
    platform="twitter",
    action="post",
    text=draft_text,
    media_hints=selected_media,
    source_kind="live_update_social",
    duplicate_policy={...},
    source_context=PublicationSourceContext(
        source_kind="live_update_social",
        metadata={
            "topic_id": topic_id,
            "source_message_ids": source_message_ids,
            "live_update_discord_message_ids": discord_message_ids,
            "decision_run_id": run_id
        }
    )
)
```

For thread posting, add either:

1. A loop-level `publish_social_thread` tool that calls `SocialPublishService` repeatedly with reply chaining.
2. A native thread request model in the service.

Recommendation:

Start with a social-loop-level thread tool that performs repeated service calls. Add native service support later if needed.

## Duplicate Protection

The social loop needs duplicate checks at three levels:

1. Topic-level:
   Has this `topic_id` already had a social decision?

2. Publication-level:
   Does `social_publications` already have a successful or queued `live_update_social` row for this topic?

3. Content-level:
   Is this substantially the same as another recent live update social post?

The first two are mandatory before rollout. The third can come later.

## Human Review Mode

Add a mode flag:

```text
LIVE_UPDATE_SOCIAL_MODE=draft|queue|publish
```

Suggested behavior:

- `draft`: record draft and decision only. No social publication.
- `queue`: enqueue social publication for scheduled worker when the agent chooses `approve_post`.
- `publish`: publish immediately when the agent chooses `approve_post`.

Default should be `draft`.

Also add:

```text
LIVE_UPDATE_SOCIAL_ENABLED=false
LIVE_UPDATE_SOCIAL_REQUIRE_MEDIA_UNDERSTANDING=true
LIVE_UPDATE_SOCIAL_MAX_TURNS=20
LIVE_UPDATE_SOCIAL_MAX_COST_USD=1.00
```

## Multi-Sprint Rollout Plan

This should be planned as multiple two-week sprints, but not a sprawling two-month-plus platform rewrite. The practical target should be four two-week sprints for the real product, with broader agent-runtime migration treated as optional follow-up.

The important correction is that media attachment is not a late polish item. It is a core capability and should be treated as a release gate for any queue/publish mode.

The social poster must also have access to the useful media and social tools currently available through admin chat. The goal is not to fork those capabilities. The goal is to extract or wrap the existing admin capabilities so the social loop can use the same underlying media inspection, media download, media refresh, route lookup, and social publishing behavior with a loop-specific policy.

### Sprint 1: Draft-Only Social Review, Tool Contract, and Admin Media Access

Goal:

Create the social review loop after successful live-update posting, keep it non-publishing by default, and give it access to the admin-equivalent media/social helpers immediately.

Deliverables:

- Add `live_update_social_runs` persistence.
- Add durable duplicate guard for `topic_id + platform + action`.
- Add `LiveUpdateSocialAgent` in `draft` mode.
- Trigger best-effort social review after live-update publish results with `status in {"sent", "partial"}`.
- Reconstruct `publish_units` from topic summary plus source metadata.
- Add minimal `ToolSpec` and `ToolBinding`.
- Add social-loop conformance tests so advertised tools have handlers.
- Add terminal decision tools: `draft_social_post`, `skip_social_post`, `request_social_review`.
- Add trace/status logging for social runs.
- Extract or wrap the admin-chat helpers needed by the social loop:
  - inspect a Discord message and fetch fresh attachment/embed media;
  - download a media URL;
  - refresh Discord CDN URLs;
  - list/resolve social routes;
  - call the canonical social publish service.

Media work in this sprint:

- Store selected media refs as stable identities, not CDN URLs.
- Record which media refs were considered, selected, skipped, or unresolved.
- Add `MediaRefIdentity` and `ResolvedMedia`.
- Resolve media refs to fresh URLs or local files on demand for draft/understanding.
- Do not publish media yet.

Acceptance criteria:

- A successful live update creates one social review run.
- The run records `skip`, `draft`, or `needs_review`.
- Draft text and selected media ref identities are inspectable.
- The social loop can use admin-equivalent media inspection/download/refresh behavior through shared helpers.
- Duplicate reruns do not create a second social run for the same topic/platform/action.
- No social publication is created in default mode.

### Sprint 2: Media Understanding and Durable Queue Mode

Goal:

Make media-aware decisions and enable queue mode only when media attachment is durable across restarts/deploys.

Deliverables:

- Extract/reuse image and video understanding handlers.
- Add social-loop read tools:
  - `get_live_update_topic`;
  - `get_source_messages`;
  - `get_published_update_context`;
  - `inspect_message_media`;
  - `understand_image`;
  - `understand_video`;
  - `list_social_routes`.
- Add typed tool result envelopes with truncation metadata.
- Run image/video understanding on selected or candidate media.
- Require media understanding for media-heavy posts unless explicitly skipped with a recorded reason.
- Add durable media strategy for queued social posts:
  - either upload resolved media to durable object storage and queue durable URLs;
  - or store media ref identities and resolve/download them at execution time.
- Extend `SocialSourceKind` with `live_update_social`.
- Add provider/account metadata for bot-owned live-update social posts.
- Add `enqueue_social_post`.
- Add publication duplicate checks against both `live_update_social_runs` and `social_publications`.
- Update `SocialPublishService` or provider execution path so queued media does not depend on temporary files.
- Add queue-mode status and failure logging.

Media work in this sprint:

- Media attachment is a release gate. Queue mode should not be considered complete until image/video media can be attached reliably after a restart.
- Validate file size, content type, and provider compatibility before enqueueing.
- Store fallback reason when media cannot be attached.

Acceptance criteria:

- The social loop can inspect and understand media from source messages.
- A media-heavy live update draft includes selected media refs plus understanding summaries.
- Tool drift tests fail if a social-loop media tool is advertised without a handler/backend dependency.
- In `LIVE_UPDATE_SOCIAL_MODE=queue`, an approved post creates a queued `social_publications` row.
- Queued text posts work.
- Queued media posts work after process restart or deploy.
- Publication rows include topic ID, source message IDs, live-update Discord message IDs, selected media refs, and social run ID.
- Duplicate reruns do not enqueue another post.

### Sprint 3: Publish Mode, Review Controls, Threads, and Richer Social Strategy

Goal:

Move from queue-only safety to controlled publishing, then add the social strategy that matters once single-post media is reliable.

Deliverables:

- Add `publish_social_post` gated by `LIVE_UPDATE_SOCIAL_MODE=publish`.
- Add human-review surface for `needs_review` and draft decisions.
- Add admin/status tools to inspect recent social runs and publication outcomes.
- Add failure classification:
  - media resolution failed;
  - provider rejected media;
  - route missing;
  - duplicate prevented;
  - model skipped;
  - human review required.
- Add social route/account validation before publish.
- Add safe retry behavior for failed social runs.
- Add thread draft decisions.
- Add immediate thread publishing by reply-chaining through `SocialPublishService`.
- Add quote/reply decisions for cases with an existing relevant social post.
- Add `find_existing_social_posts`.
- Add content-level duplicate similarity checks.
- Decide whether queued threads need a native grouped/thread publication model.

Media work in this sprint:

- Confirm attached media appears on the provider result, not just in the queued request.
- Record final media attachment outcome per publication.
- Add explicit text-only fallback only when media was genuinely unavailable or intentionally skipped.
- Support attaching media to the right post in a thread.
- Ensure replies default to text-only only when that is the intended strategy, not because media was lost.
- Add trace output showing which thread item owns which media refs.

Acceptance criteria:

- Publish mode can post a single live-update social post with media attached.
- Operators can inspect exactly what was posted, which media attached, and why.
- Failed media attachment is visible and does not silently degrade into an unexplained text-only post.
- A multi-section live update can become either one post or a short thread.
- Thread media associations are explicit and test-covered.
- Quote/reply actions do not accidentally reattach duplicate media.

### Sprint 4: Hardening, Metrics, Production Rollout, and Selective Runtime Standardisation

Goal:

Make the system operable in production and only standardise broader agent runtime pieces that are clearly paying rent.

Deliverables:

- Add dashboards or status commands for social runs.
- Add structured run metrics:
  - decisions by type;
  - media attached vs failed;
  - provider errors;
  - duplicate prevents;
  - human review queue size;
  - cost/tokens.
- Add runbook for toggling `draft`, `queue`, and `publish`.
- Add backfill/retry tooling for social runs.
- Add production rollout checklist.
- Move provider-neutral response normalization out of loop-specific code if it is still duplicated.
- Add shared idempotency hooks for write/publish tools.
- Optionally migrate narrow TopicEditor/AdminChat internals only where this removes real drift.
- Preserve loop-specific prompts and policies.

Media work in this sprint:

- Track media attachment success rate.
- Alert or surface cases where media-heavy posts repeatedly fail to attach media.
- Add regression fixtures for representative Discord attachment, external video, image, and embed cases.
- Keep media tools shared across admin chat, topic editor, and social loop.
- Ensure admin chat and social loop use the same underlying media refresh/download/understanding primitives.

Acceptance criteria:

- Production operators can see what happened without reading raw logs.
- Media attachment failures are measurable and debuggable.
- The system can be safely rolled back to draft mode.
- Existing TopicEditor tests pass.
- Existing AdminChat tests pass.
- Existing social loop tests pass.
- Shared tooling reduces drift without making a global service locator or god abstraction.

## Testing Plan

### Unit Tests

- Tool registry validation.
- Anthropic response normalization.
- DeepSeek/OpenAI response normalization.
- Runner max-turn handling.
- Runner no-tool behavior by policy.
- Tool result truncation.
- Media ref normalization.
- Media resolver fallback paths.
- Social decision duplicate checks.

### Integration Tests

- Topic editor publishes a structured topic and enqueues social review.
- Social loop draft mode records a draft and no publication.
- Social loop queue mode creates a `social_publications` row.
- Social loop duplicate rerun returns existing decision/publication.
- Social loop with media calls understanding before selecting media.
- Social loop handles missing media by recording a non-fatal decision.

### Regression Tests

- Existing topic editor structured publishing tests.
- Existing admin chat social publishing tests.
- Existing social publish service tests.
- Existing external media resolver tests.
- Existing vision client tests.

## Operational Logging

Every social run should make it easy to answer:

- Which live update triggered this?
- Which source messages were used?
- Which media items were considered?
- Did image/video understanding run?
- What did the model decide?
- Why did it skip or post?
- What exact text did it publish or queue?
- Which `social_publications` rows were created?
- Which provider URL resulted?
- Did any tool fail?

Add log markers similar to:

```text
LiveUpdateSocial run started: run_id=... topic_id=...
LiveUpdateSocial tool call: understand_video message_id=...
LiveUpdateSocial decision: enqueue_post topic_id=... media_refs=...
LiveUpdateSocial publication queued: publication_id=...
```

## Risks

### Risk: Over-generalising the Agent Runtime

Mitigation:

Keep the shared runner small. Do not force prompts, storage schemas, or product decisions into the common layer.

### Risk: Posting Low-Quality Tweets Automatically

Mitigation:

Start in `draft` mode. Add `queue` mode before `publish` mode. Require explicit env flags for automatic publishing.

### Risk: Media Attachment Failures

Mitigation:

Use structured media refs from the live update document, not scraped output text. Resolve and validate media before creating a social publication.

### Risk: Duplicate Social Posts

Mitigation:

Require topic-level and publication-level duplicate checks before any enqueue or publish action.

### Risk: Tool Drift

Mitigation:

Add registry/backend conformance tests. Fail fast if advertised tools lack handlers or required backend methods.

## Recommended First Implementation Slice

The best first slice is Sprint 1 from the roadmap above. It is not the full runtime migration. It is:

1. Add minimal `ToolSpec`/`ToolBinding`, not the full shared runner.
2. Build the new `LiveUpdateSocialAgent` in `draft` mode.
3. Trigger social review after successful live-update Discord publishing.
4. Reconstruct `publish_units` and record selected media ref identities.
5. Add durable duplicate guards before any social side effect exists.

This proves the standardisation direction on new functionality without destabilising the already-working TopicEditor and AdminChat paths. The full shared runner should wait until the social loop has validated the tool contracts and media flow.

## Design Review Corrections

Two review passes sharpened the plan:

1. The abstraction path is valid, but the first implementation should be smaller.
2. The social loop has a few concrete operational risks that must be handled before queue/publish mode.

### Revised First Slice

Build the post-live-update social loop first, in draft mode, with only the smallest shared tool/runtime contract needed for that loop.

Do not migrate `TopicEditor` or `AdminChatAgent` onto a new shared runner in the first slice. Their current paths are already complex and working. The new social loop is a safer place to prove shared primitives.

Initial decisions should be:

- `skip`
- `draft`
- `approve_post`
- `needs_review`

The environment mode should decide whether `approve_post` becomes a stored draft, queued publication, or immediate publication:

```text
LIVE_UPDATE_SOCIAL_MODE=draft|queue|publish
```

This avoids mixing editorial intent with transport mechanics. The model decides whether something is worth posting; runtime policy decides whether it is actually posted.

### Minimal Standardisation Contract

Start with a smaller abstraction than the full package sketch above:

```python
ToolSpec(
    name="...",
    description="...",
    input_schema={...},
    side_effect="read" | "write" | "publish",
)

ToolBinding(
    spec=...,
    handler=...,
    required_dependencies=[...],
    inject_args=...,
)
```

This is better than one large `AgentTool`, because the portable schema and loop-specific handler context are different concerns.

The first conformance tests should verify:

- every advertised social-loop tool has a handler;
- every handler has required DB/storage dependencies;
- write/publish tools have an idempotency key or duplicate guard;
- tool results are JSON envelopes with truncation metadata, not unstructured strings.

### Keep `agent_loop` Narrow

`src/common/agent_loop` should own only:

- turn sequencing;
- LLM response normalization;
- tool call/result protocol;
- max turn/cost/token limits;
- idempotency hooks;
- event emission.

It should not know about:

- Discord topics;
- live update publishing;
- social platforms;
- media semantics;
- storage table names.

Topic-specific, media-specific, and social-specific logic should live outside the runner.

### Media Identity vs Resolution

Do not persist Discord CDN URLs as durable identity.

Use separate concepts:

```python
MediaRefIdentity(
    message_id="...",
    kind="attachment" | "embed" | "external",
    index=0,
)

ResolvedMedia(
    identity=...,
    url="...",
    local_path="...",
    content_type="...",
    filename="...",
    expires_at="...",
)
```

The current topic document media refs already provide a stable identity shape. Resolution should happen close to the use site, with refresh/download behavior appropriate to the target.

### Queued Media Risk

Queued social publishing with media is not safe if it relies on temporary local files.

Current `SocialPublishService.enqueue` serializes `media_hints` into `request_payload`, but X publishing expects usable local files or downloadable media at execution time. Existing admin scheduled direct posts preserve temp downloads, which is not durable across deploys/restarts.

Before enabling `LIVE_UPDATE_SOCIAL_MODE=queue` for media posts, implement one of:

1. Store durable media files in Supabase/object storage and pass durable URLs.
2. Store media ref identities and resolve/download them at publication execution time.

Draft mode can still record intended media refs immediately.

### Provider Identity Risk

The example `SocialPublishRequest` must include provider/account context that satisfies the X provider.

Do not rely on admin-chat `user_details` accidentally being present. For bot-owned live-update social posts, make provider/account metadata explicit in `source_context.metadata`, or adjust the provider contract so bot-owned publishing does not require member-like user details.

### Duplicate Protection Must Be Durable

Do not rely only on model behavior, preflight reads, or `duplicate_policy`.

Add durable duplicate protection before queue/publish mode:

- a unique constraint or equivalent guard for `live_update_social_runs(topic_id, platform, action)`;
- a publication-level check for queued/succeeded `social_publications` tied to the topic;
- idempotency keys for social write/publish tools.

JSON metadata checks alone are too brittle for this.

### Publish Units Availability

The plan says the social loop should receive the same `publish_units` that Discord publishing used. Successful structured publishes currently build those units during `_publish_topic` but do not return them in the successful publish result.

The implementation must either:

1. persist `publish_units` or selected publish metadata during publish; or
2. reconstruct `publish_units` from the stored topic summary plus hydrated source metadata.

Recommendation: reconstruct first, because it avoids another persisted representation. Persist later only if reconstruction becomes expensive or ambiguous.

### Thread Publishing Should Wait

Immediate thread publishing can be implemented by chaining `SocialPublishService` calls, but queued threads are more complex because replies need the first provider post ID.

Defer thread publishing until single-post draft/queue mode is working. If queued threads become important, add a native grouped/thread publication model instead of independent queued rows.

## Definition of Done

This project is done when:

- A successful Discord live update automatically creates a social review run.
- The social review run uses message/search/media-understanding tools before deciding.
- The social poster has access to the same underlying media inspection, refresh, download, route, and social publishing capabilities that admin chat uses.
- The run records a clear terminal decision.
- In draft mode, the draft is inspectable and no post is made.
- In queue/publish mode, social posts go through `SocialPublishService`.
- Media is selected from structured refs and attached reliably when the social post is media-bearing.
- Queued media posts survive process restarts/deploys because media is either durable or resolved at execution time.
- Text-only fallback happens only with an explicit recorded reason.
- Duplicate protection prevents repeated posts for the same topic.
- Logs clearly explain tool failures, skipped decisions, selected media, and created publications.
- Shared runtime primitives are used by the new social loop and are ready for gradual TopicEditor/AdminChat migration.
