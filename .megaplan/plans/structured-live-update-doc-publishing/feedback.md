# Feedback for plan: structured-live-update-doc-publishing

Fill in any fields you want — leave the rest blank. `rating:` is
an integer 0–10 (or blank). `comment:` is free text and may span
multiple lines (everything until the next `##` heading is the
comment body).

> Implement structured editable live-update documents for the TopicEditor. Current behavior: post_sectioned_topic stores a body plus loose sections and topic-level source_message_ids; render_topic emits one big Discord message with a Sources footer of raw message IDs; section media is dropped or model-copied into simple-topic media; publisher sends a single rendered string and trims at 2000 chars. Desired behavior: the agent creates a document-like draft in topics.summary with ordered blocks/sections. Each intro/section block carries its own source_message_ids and media_refs. Source citations render inline in brackets at the relevant block, not as an overall footer. Media refs bind by stable reference such as message_id plus attachment_index, not copied CDN URLs. Publisher resolves source IDs to Discord jump URLs using message metadata and resolves media refs to attachment/embed URLs at publish time. Publish order should be deterministic: header, intro text with inline linked sources, intro media, each section text with inline linked sources, that section media, one block at a time. Text chunking should be paragraph-aware only when a block exceeds Discord limits; normal sectioned topics should send one section per message. Add validation so media_refs must resolve to source messages/attachments and unknown refs do not silently disappear. Preserve existing topic_sources storage as the union of all block-level source IDs for audit/search. Update prompts/tool schemas so the agent is conscientious: every factual block gets its own sources; media is attached only to the relevant block; no global source footer. Keep backwards compatibility for old summaries with body/sections/source_message_ids as much as reasonable. Add focused tests covering section-level sources, media refs sent after relevant sections, no footer, source link hydration, chunking, and existing simple topic behavior.

## Overall

ai_rating: 
ai_comment: 
rating: 
comment: 

## prep  <!-- Pre-plan research / scoping -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## plan  <!-- Initial plan generation -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## critique  <!-- Parallel critique passes -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## revise  <!-- Plan revisions in response to critique -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## gate  <!-- Quality gate decision -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## tiebreaker  <!-- Tiebreaker orchestration when gates disagreed -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## finalize  <!-- Final plan consolidation -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## execute  <!-- Implementation by the executor -->

ai_rating: 
ai_comment: 
rating: 
comment: 

## review  <!-- Post-execution review -->

ai_rating: 
ai_comment: 
rating: 
comment:
