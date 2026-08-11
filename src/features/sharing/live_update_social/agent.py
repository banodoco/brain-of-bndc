"""LiveUpdateSocialAgent — Sprint 1 draft-only terminal runtime.

Single-step: reconstructs publish_units, resolves media identities,
builds a prompt, calls the LLM with exactly three terminal tools, and
dispatches the single tool call through ToolBinding to update the run.

Sprint-1 constraints (structurally enforced):
  • No queue mode
  • No publish mode
  • No reply / thread / quote strategy generation
  • No access to SocialPublishService.publish_now or enqueue

When media is expected but cannot be resolved, the outcome is forced to
request_social_review (needs_review) rather than text-only success.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .contracts import LiveUpdateHandoffPayload
from .models import (
    MediaRefIdentity,
    RunState,
    ToolBinding,
)
from .publish_units import reconstruct_publish_units
from .tools import ALL_TOOL_SPECS, build_tool_bindings, get_tool_by_name
from .helpers import inspect_discord_message

if TYPE_CHECKING:
    import discord
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")

# Mode-dependent forbidden actions are now instance-level (set in __init__).
# See LiveUpdateSocialAgent.__init__ for the per-mode configuration.


class LiveUpdateSocialAgent:
    """Social review agent supporting draft, queue, and publish modes (Sprint 3).

    Supports queue mode when social_publish_service is provided and
    LIVE_UPDATE_SOCIAL_MODE=queue. Supports publish mode when
    LIVE_UPDATE_SOCIAL_MODE=publish and social_publish_service is provided.
    """

    def __init__(
        self,
        db_handler: "DatabaseHandler",
        bot: "discord.Client",
        social_publish_service: Any = None,
    ):
        self.db_handler = db_handler
        self.bot = bot
        self.social_publish_service = social_publish_service

        # ── instance-level forbidden actions (per-mode) ────────────────
        if self._is_publish_mode():
            # Publish mode: allow reply/quote for thread chaining, forbid retweet
            self._forbidden_actions: frozenset = frozenset({"retweet"})
        else:
            # Queue/draft mode: forbid all non-post actions
            self._forbidden_actions: frozenset = frozenset({"reply", "retweet", "quote"})

        self._bindings: List[ToolBinding] = build_tool_bindings(
            db_handler, bot, social_publish_service=social_publish_service,
        )

    # ── public entry point ────────────────────────────────────────────

    async def run(self, payload: LiveUpdateHandoffPayload) -> Optional[str]:
        """Execute one social-review run and return the terminal status.

        Returns one of ``"draft"``, ``"skip"``, ``"needs_review"``, or
        ``None`` if the run could not be completed.
        """
        # ── structural rejection of non-sprint actions ────────────────
        if payload.action in self._forbidden_actions:
            logger.warning(
                "LiveUpdateSocialAgent: rejected forbidden action %r",
                payload.action,
            )
            return None

        # ── upsert the run (duplicate guard) ──────────────────────────
        row = self.db_handler.upsert_live_update_social_run(
            topic_id=payload.topic_id,
            platform=payload.platform,
            action=payload.action,
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            source_metadata=payload.source_metadata,
            topic_summary_data=payload.topic_summary_data,
            vendor=payload.vendor,
            depth=payload.depth,
            with_feedback=payload.with_feedback,
            deepseek_provider=payload.deepseek_provider,
        )
        if not row:
            logger.error(
                "LiveUpdateSocialAgent: upsert returned None for %s/%s",
                payload.topic_id,
                payload.platform,
            )
            return None

        run_state = RunState.from_row(row)
        run_id = run_state.run_id
        run_state.add_trace("agent_start", topic_id=payload.topic_id,
                            platform=payload.platform, action=payload.action)

        try:
            # ── reconstruct publish_units ─────────────────────────────
            run_state.publish_units = reconstruct_publish_units(
                topic_summary_data=payload.topic_summary_data,
                source_metadata=payload.source_metadata,
                mode="publish" if self._is_publish_mode() else "draft",
            )
            run_state.add_trace("publish_units_reconstructed")

            # ── resolve media identities ──────────────────────────────
            media_ok = await self._resolve_media(payload, run_state)
            if not media_ok:
                run_state.add_trace("media_resolution_failed")
                # Force needs_review — no text-only success when media expected
                return await self._force_needs_review(
                    run_state,
                    reason="Media resolution failed — one or more media items "
                           "could not be resolved.",
                )

            # ── propose mode: pre-run media understanding ─────────────
            # Single-step agent — understanding must happen BEFORE the LLM
            # call so the proposals are grounded in what the media actually is.
            if self._is_propose_mode():
                understood = await self._understand_media_for_propose(run_state)
                run_state.add_trace(
                    "media_understanding",
                    items=len(understood),
                    failures=sum(1 for u in understood if u.get("error")),
                )
                # Strict invariant: ANY selected media without usable cached
                # understanding forces review. The agent must never guess
                # about a clip it could not ground (a mixed hit/miss leaves
                # the door open for a fabricated "compile 60+ clips" strategy
                # over clips it never saw). A cache row with an empty
                # understanding dict also counts as unresolved. Text-only
                # source messages (media_bearing=false) are NOT misses.
                selected_refs = (run_state.media_decisions or {}).get("selected", []) or []
                if selected_refs:
                    unresolved = [
                        u for u in understood
                        if u.get("error") and u.get("media_bearing", True)
                        or (not u.get("error") and not (u.get("summary") or u.get("subject")))
                    ]
                    if len(unresolved) == len(understood):
                        run_state.add_trace("media_understanding_all_missed")
                        return await self._force_needs_review(
                            run_state,
                            reason="Propose mode: media is expected but no cached "
                                   "understanding is available — cannot ground a "
                                   "media strategy without inventing one.",
                        )
                    if unresolved:
                        run_state.add_trace(
                            "media_understanding_partial",
                            unresolved=len(unresolved),
                        )
                        return await self._force_needs_review(
                            run_state,
                            reason=f"Propose mode: {len(unresolved)} of "
                                   f"{len(understood)} media item(s) have no "
                                   "cached understanding — the agent would be "
                                   "guessing about them. Human review required.",
                        )

            # ── build and send prompt ─────────────────────────────────
            system_prompt = self._build_system_prompt(payload, run_state)
            user_message = self._build_user_message(payload, run_state)
            tool_specs = self._build_tool_specs()

            llm_response = await self._call_llm(
                payload=payload,
                system_prompt=system_prompt,
                user_message=user_message,
                tools=tool_specs,
            )
            run_state.add_trace("llm_called")

            # ── dispatch tool call ────────────────────────────────────
            tool_name, tool_params = self._parse_tool_call(llm_response)
            if not tool_name:
                run_state.add_trace("no_tool_call_parsed",
                                    raw_response=llm_response[:500])
                return await self._force_needs_review(
                    run_state,
                    reason="LLM did not produce a valid tool call.",
                )

            return await self._dispatch_tool(run_state, tool_name, tool_params)

        except Exception:
            logger.exception(
                "LiveUpdateSocialAgent: error processing run %s", run_id
            )
            run_state.add_trace("agent_error", run_id=run_id)
            try:
                return await self._force_needs_review(
                    run_state,
                    reason="Agent encountered an unexpected error.",
                )
            except Exception:
                logger.exception("Failed to force needs_review for run %s", run_id)
                return None

    # ── media resolution ──────────────────────────────────────────────

    async def _resolve_media(
        self,
        payload: LiveUpdateHandoffPayload,
        run_state: RunState,
    ) -> bool:
        """Resolve media identities from the source Discord message.

        Populates run_state.media_decisions with considered / selected /
        skipped / unresolved lists.  Returns True if all expected media
        was resolved (or no media was expected), False if any required
        media could not be resolved.
        """
        decisions: Dict[str, Any] = {
            "considered": [],
            "selected": [],
            "skipped": [],
            "unresolved": [],
        }

        channel_id = payload.channel_id
        topic_data = payload.topic_summary_data or {}

        # Determine the message(s) to inspect
        msg_id = topic_data.get("message_id")
        media_msg_id = topic_data.get("mainMediaMessageId")

        message_ids = []
        if msg_id:
            message_ids.append(int(msg_id))
        if media_msg_id and media_msg_id != msg_id:
            message_ids.append(int(media_msg_id))

        if not message_ids or not self.bot:
            # No messages to inspect — nothing to resolve, not a failure
            run_state.media_decisions = decisions
            run_state.add_trace("media_resolution", message_count=0)
            return True  # no media expected

        all_resolved = True
        unresolved_any = False

        for mid in message_ids:
            try:
                inspected = await inspect_discord_message(
                    bot=self.bot,
                    channel_id=channel_id,
                    message_id=mid,
                )
            except Exception as e:
                logger.warning(
                    "LiveUpdateSocialAgent: inspect_discord_message failed "
                    "for channel=%d msg=%d: %s", channel_id, mid, e,
                )
                # Record the expected message as unresolved
                decisions["unresolved"].append(
                    MediaRefIdentity(
                        source="discord_attachment",
                        channel_id=channel_id,
                        message_id=mid,
                        attachment_index=0,
                    ).to_dict()
                )
                unresolved_any = True
                all_resolved = False
                continue

            if inspected.get("error"):
                decisions["unresolved"].append({
                    "source": "discord_attachment",
                    "channel_id": channel_id,
                    "message_id": mid,
                    "error": inspected["error"],
                })
                unresolved_any = True
                all_resolved = False
                continue

            # Consider attachments
            for idx, att in enumerate(inspected.get("attachments", [])):
                identity = MediaRefIdentity(
                    source="discord_attachment",
                    channel_id=channel_id,
                    message_id=mid,
                    attachment_index=idx,
                )
                decisions["considered"].append(identity.to_dict())
                # For now, select all found attachments
                decisions["selected"].append(identity.to_dict())

            # Consider embed media
            for emb in inspected.get("embeds_media", []):
                slot = emb.get("slot", "unknown")
                identity = MediaRefIdentity(
                    source="discord_embed",
                    channel_id=channel_id,
                    message_id=mid,
                    embed_slot=slot,
                )
                decisions["considered"].append(identity.to_dict())
                decisions["selected"].append(identity.to_dict())

        run_state.media_decisions = decisions
        run_state.add_trace(
            "media_resolution",
            message_count=len(message_ids),
            attachments_found=len(decisions["selected"]),
            unresolved=len(decisions["unresolved"]),
        )

        # If nothing was found and we expected media, that's a failure
        if (not decisions["selected"]
                and not decisions["unresolved"]
                and len(message_ids) > 0):
            # We inspected messages but found nothing — not necessarily a failure
            # (maybe the message just had no attachments/embeds)
            pass

        return not unresolved_any

    # ── propose-mode media understanding (cache read) ────────────────

    # Models the topic editor uses when it understands media during the
    # editorial pass. We read whatever is already cached — never re-run
    # Gemini in the social loop.
    _CACHED_VIDEO_MODELS = ("gemini-2.5-flash", "gemini-2.5-pro")
    _CACHED_IMAGE_MODELS = ("gpt-4o-mini", "gpt-5.4")

    async def _understand_media_for_propose(
        self,
        run_state: RunState,
    ) -> List[Dict[str, Any]]:
        """Load the media understanding the topic editor already cached.

        The editorial pass runs ``understand_image`` / ``understand_video``
        (Gemini/OpenAI) on source media and persists results to
        ``message_media_understandings`` keyed by (message_id,
        attachment_index, model). Propose mode reads that cache — a
        Supabase lookup, NO new Gemini call and no re-download — so the
        proposals are grounded in the same understanding the editor used.

        Populates ``run_state.media_understanding`` (transient) and returns
        it. Never raises: cache misses are recorded with ``error`` so the
        agent knows the media is not understood and must not invent a
        media strategy for it.
        """
        decisions = run_state.media_decisions or {}
        selected = decisions.get("selected", []) or []

        # CRITICAL: the understanding cache is keyed by the ORIGINAL source
        # message IDs — the topic editor's understand_* tools run on the
        # source window and persist under those IDs. The handoff's
        # topic_summary_data.message_id is the BOT's published message ID
        # (sent_ids[0]), which has no cache rows. Always key the lookup off
        # source_metadata.source_message_ids.
        source_ids: List[str] = []
        src_meta = run_state.source_metadata or {}
        for sid in src_meta.get("source_message_ids") or []:
            if str(sid).strip():
                source_ids.append(str(sid))
        # Fallback: only when the handoff carried no source ids — the selected
        # media refs' message_ids are at least more likely to be source
        # messages than the bot's output message.
        if not source_ids:
            for media in selected:
                if isinstance(media, dict) and media.get("message_id") is not None:
                    source_ids.append(str(media["message_id"]))
        if not source_ids:
            run_state.media_understanding = []
            return run_state.media_understanding

        results: List[Dict[str, Any]] = []
        for sid in source_ids[:6]:
            # Try attachment indices 0..3 — the editor usually understands the
            # first attachment of a source message.
            found = None
            used_index = 0
            for attachment_index in range(4):
                for model in self._CACHED_VIDEO_MODELS + self._CACHED_IMAGE_MODELS:
                    try:
                        found = self.db_handler.get_message_media_understanding(
                            int(sid),
                            attachment_index,
                            model=model,
                        )
                    except Exception as e:
                        logger.warning(
                            "LiveUpdateSocialAgent: cache lookup failed "
                            "msg=%s idx=%s model=%s: %s",
                            sid, attachment_index, model, e,
                        )
                        found = None
                    if found is not None:
                        used_index = attachment_index
                        break
                if found is not None:
                    break

            if found is None:
                # Not every source message carries media — a text-only source
                # has no cache row and that is EXPECTED, not a miss. Only
                # record a miss when this source actually appears in the run's
                # resolved media decisions (i.e. it is media-bearing).
                media_bearing = any(
                    isinstance(m, dict)
                    and str(m.get("message_id") or "") == sid
                    for m in selected
                )
                results.append({
                    "source": "discord_attachment",
                    "message_id": sid,
                    "kind": "media",
                    "error": (
                        "no cached understanding (editor pass did not analyse "
                        "this media)" if media_bearing
                        else "text-only source — no media expected"
                    ),
                    "media_bearing": media_bearing,
                })
                continue

            understanding = found.get("understanding") or {}
            kind = understanding.get("kind") or "media"
            summary = str(understanding.get("summary") or "").strip()
            visual = str(understanding.get("visual_read") or "").strip()
            highlight = understanding.get("highlight_score")
            energy = understanding.get("energy")
            pacing = understanding.get("pacing")

            entry: Dict[str, Any] = {
                "source": "discord_attachment",
                "message_id": sid,
                "attachment_index": used_index,
                "kind": kind,
                "summary": summary,
                "visual_read": visual[:600],
                "model": found.get("model", ""),
            }
            # Image-schema fields (describe_image) — include when the editor
            # cached an image understanding.
            subject = understanding.get("subject")
            if subject:
                entry["subject"] = str(subject)[:400]
            aesthetic = understanding.get("aesthetic_quality")
            if aesthetic is not None:
                entry["aesthetic_quality"] = aesthetic
            technical = understanding.get("technical_signal")
            if technical:
                entry["technical_signal"] = str(technical)[:400]
            if highlight is not None:
                entry["highlight_score"] = highlight
            if energy is not None:
                entry["energy"] = energy
            if pacing:
                entry["pacing"] = pacing
            results.append(entry)

        run_state.media_understanding = results
        return results

    # ── prompt construction ─────────────────────────────────────────

    def _build_system_prompt(
        self,
        payload: LiveUpdateHandoffPayload,
        run_state: RunState,
    ) -> str:
        """Build the system prompt for the LLM.

        Includes chain settings, the available terminal tools, and
        instructions to make exactly one tool call.  When queue mode is
        enabled, ``enqueue_social_post`` is listed as an additional
        terminal option; when disabled only the Sprint-1 draft/skip/review
        tools are presented.  When publish mode is enabled, the prompt
        includes thread-building guidance and publish instructions.
        """
        queue_mode = self._is_queue_mode()
        publish_mode = self._is_publish_mode()
        propose_mode = self._is_propose_mode()

        if publish_mode:
            tools_text = (
                "## Available terminal tool (call exactly ONE)\n\n"
                "1. **publish_social_post** — Publish a social media post "
                "IMMEDIATELY. Use this when content and media are ready. "
                "Provide draft_text and optionally selected_media identities. "
                "For multi-section updates, provide thread_items "
                "(list of {{index, draft_text, media_refs}} where index 0 is "
                "the root post and subsequent items are reply chain items).\n\n"
                "## Thread-building guidance\n"
                "- If the topic has multiple sub-topics (shown below as thread "
                "items with indices), you may publish as a thread: set "
                "thread_items with one entry per sub-topic, each with its own "
                "draft_text. The root post (index 0) should introduce the "
                "overall topic; sub-topic units follow as reply chain items.\n"
                "- For a single-post publish, only provide draft_text and "
                "optionally selected_media (do NOT provide thread_items).\n"
                "- If you cannot make a confident decision (missing media, "
                "unclear content, route issues), return "
                "'request_social_review' as the tool name with a reason.\n"
                "- If the content should not be posted (not newsworthy, "
                "duplicate), return 'skip_social_post' as the tool name "
                "with a reason.\n"
                "- Use read tools (find_existing_social_posts, "
                "get_social_run_status, get_live_update_topic, "
                "get_source_messages, get_published_update_context, "
                "inspect_message_media, list_social_routes) to gather "
                "context BEFORE making your terminal decision.\n"
            )
        elif propose_mode:
            tools_text = (
                "## Available terminal tool (call exactly ONE)\n\n"
                "1. **propose_social_ideas** — Propose 2-4 social post IDEAS "
                "for this topic. This is NOT a finished post — each idea pairs "
                "a THEME (the angle/headline) with a MEDIA STRATEGY (what the "
                "media should be, and at what scale). Ideas are stored for "
                "human review; the human picks which to develop.\n"
                "2. **skip_social_post** — Skip social posting. Use when "
                "nothing here is worth proposing (not newsworthy, duplicate, "
                "routine chatter). Provide a reason.\n"
                "3. **request_social_review** — Request human review. Use "
                "when you cannot make a confident proposal (media unresolved, "
                "unclear content, route issues). Provide a reason.\n"
                "\n"
                "## What a good idea looks like\n"
                "- The theme is ONE short sentence with the creator credited.\n"
                "- The media strategy is SPECIFIC about source and scale — "
                "base it on the media understanding summaries in the user "
                "message. If a thread has many short clips (dozens), propose "
                "a COMPILATION (e.g. 'compile 60+ clips from this thread into "
                "one montage'). If there are a handful of strong examples "
                "(3-10), propose a MONTAGE of ~6+ of them. If there is one "
                "standout clip, propose a single hero clip. If the story is "
                "the observation, not the media, propose text-only or one "
                "clip as evidence.\n"
                "- Ground every media strategy in a specific "
                "media_understanding_basis (clip count, style range, quality, "
                "energy) — do not invent media the source does not have.\n"
            )
        else:
            tools_text = (
                "## Available terminal tools (call exactly ONE)\n\n"
                "1. **draft_social_post** — Record a draft social post. Use when "
                "content and media are ready for review. Provide the draft_text "
                "and optionally selected_media identities.\n"
                "2. **skip_social_post** — Skip social posting. Use when content "
                "should not be posted (not newsworthy, duplicate, etc.). Provide "
                "a reason.\n"
                "3. **request_social_review** — Request human review. Use when "
                "you cannot make a confident decision (missing media, unclear "
                "content, route issues). Provide a reason.\n"
            )
            if queue_mode:
                tools_text += (
                    "4. **enqueue_social_post** — Enqueue an approved social post "
                    "for durable publication. Use when the draft is approved and "
                    "media understanding has been completed. Provide draft_text "
                    "and optionally selected_media with understanding summaries.\n"
                )

        if publish_mode:
            rules_text = (
                "## Rules\n"
                "- Call exactly ONE terminal tool.\n"
                "- You may use read tools to gather context BEFORE making "
                "your terminal decision.\n"
                "- Do NOT provide text outside the tool call.\n"
                "- If media is expected but unresolved, return "
                "'request_social_review' with a reason.\n"
                "- If the content is newsworthy and media is available, use "
                "publish_social_post with a concise draft.\n"
                "- If the content is not newsworthy, return 'skip_social_post' "
                "with a reason.\n"
                "- For threads: each thread item gets its own draft_text. "
                "Media refs are assigned per-item via media_refs.\n"
            )
        elif propose_mode:
            rules_text = (
                "## Rules\n"
                "- Call exactly ONE terminal tool.\n"
                "- Do NOT provide text outside the tool call.\n"
                "- Propose IDEAS, never finished posts. No draft_social_post, "
                "no publish_social_post, no enqueue_social_post.\n"
                "- Every idea MUST have a theme AND a media strategy AND a "
                "pattern AND a media_understanding_basis AND a rationale.\n"
                "- The media strategy must fit what the media understanding "
                "actually found — count the clips, read the style range, "
                "respect the quality. Never propose a 60+ clip compilation "
                "for a thread with 3 clips, and never propose a single clip "
                "when the source is a 76-clip style range.\n"
                "- Credit the creator on every idea. No hype inflation — "
                "preserve conditions (local, model, creator).\n"
                "- If media is expected but unresolved, use "
                "request_social_review.\n"
                "- If the content should not be posted at all, use "
                "skip_social_post.\n"
            )
        else:
            rules_text = (
                "## Rules\n"
                "- Call exactly ONE terminal tool.\n"
                "- You may use read tools (get_live_update_topic, "
                "get_source_messages, get_published_update_context, "
                "inspect_message_media, list_social_routes) to gather context "
                "BEFORE making your terminal decision, but ONLY the final "
                "terminal tool call will be executed.\n"
                "- Do NOT provide text outside the tool call.\n"
                "- If media is expected but unresolved, use "
                "request_social_review.\n"
                "- If the content is newsworthy and media is available, use "
                "draft_social_post with a concise draft.\n"
                "- If the content is not newsworthy, use skip_social_post.\n"
            )
            if queue_mode:
                rules_text += (
                    "- If the content is approved and queue mode is active, "
                    "use enqueue_social_post instead of draft_social_post.\n"
                )

        role = (
            "You are a social media publisher for the Banodoco Discord bot. "
            "Your job is to review a live-update topic and publish it to "
            "social media immediately.\n\n"
            if publish_mode else
            "You are a social media idea proposer for the Banodoco Discord bot. "
            "Your job is to review a live-update topic and propose 2-4 post "
            "ideas — each a theme plus a media strategy — grounded in the "
            "media understanding summaries, so a human can pick which to "
            "develop.\n\n"
            if propose_mode else
            "You are a social media draft reviewer for the Banodoco Discord bot. "
            "Your job is to review a live-update topic and decide whether it "
            "should be drafted for social media, skipped, or flagged for human "
            "review.\n\n"
        )

        exemplars_text = ""
        if propose_mode:
            from .exemplars import (
                SOCIAL_IDEA_EXEMPLARS,
                SOCIAL_IDEA_NEGATIVES,
            )

            parts = ["## Idea patterns we like\n"]
            parts.append(
                "These are REAL Banodoco posts. Learn the pattern: the "
                "CONTEXT tells you when to reach for it, the THEME is the "
                "angle, and the MEDIA STRATEGY says what the media should "
                "be — including scale. Match the pattern to the source "
                "material, then propose your own ideas in the same spirit.\n"
            )
            for ex in SOCIAL_IDEA_EXEMPLARS:
                parts.append(
                    f"\n### Pattern: {ex['pattern']} ({ex['archetype']})\n"
                    f"- Context: {ex['context']}\n"
                    f"- Theme: {ex['theme']}\n"
                    f"- Media strategy: {ex['media_strategy']}\n"
                    f"- Why it lands: {ex['why']}"
                )
            parts.append("\n## When NOT to post\n")
            for neg in SOCIAL_IDEA_NEGATIVES:
                parts.append(
                    f"- {neg['pattern']}: {neg['context']} → {neg['why']}"
                )
            exemplars_text = "\n".join(parts) + "\n\n"

        return (
            role
            + tools_text
            + "\n"
            + rules_text
            + "\n"
            + exemplars_text
            + f"Chain: vendor={payload.vendor}, depth={payload.depth}, "
            f"with_feedback={payload.with_feedback}, "
            f"deepseek_provider={payload.deepseek_provider}"
        )

    def _build_user_message(
        self,
        payload: LiveUpdateHandoffPayload,
        run_state: RunState,
    ) -> str:
        """Build the user message containing topic data and media info.

        In publish mode with multi-unit publish_units, suppresses raw
        subTopics and instead presents units as thread items with indices.
        """
        parts: List[str] = []
        publish_mode = self._is_publish_mode()

        # Topic summary
        topic = payload.topic_summary_data or {}
        parts.append("## Topic Summary")
        parts.append(f"Title: {topic.get('title', 'Untitled')}")
        parts.append(f"Topic ID: {payload.topic_id}")
        parts.append(f"Platform: {payload.platform}")

        # ── publish-mode: present units as thread items ────────────────
        units = run_state.publish_units or {}
        unit_list = units.get("units", [])

        if publish_mode and len(unit_list) > 1:
            # Multi-unit publish mode: suppress raw subTopics, present as
            # thread items with indices so the LLM can build a thread.
            parts.append(f"\n## Thread Items ({len(unit_list)} units)")
            for i, unit in enumerate(unit_list):
                parts.append(f"\n### Item {i}" + (" (root)" if i == 0 else " (reply)"))
                parts.append(f"  title: {unit.get('title', '')}")
                if unit.get("sub_topics"):
                    parts.append(f"  sub_topics: {json.dumps(unit['sub_topics'], default=str)}")
                if unit.get("media_message_id"):
                    parts.append(f"  media_message_id: {unit['media_message_id']}")
                if unit.get("_is_subtopic"):
                    parts.append(f"  _is_subtopic: true")
        else:
            # Sub-topics (legacy presentation for draft/queue/single-unit modes)
            sub_topics = topic.get("subTopics", [])
            if sub_topics:
                parts.append(f"Sub-topics: {len(sub_topics)}")
                for st in sub_topics[:10]:
                    if isinstance(st, dict):
                        parts.append(f"  - {st.get('title', st.get('name', str(st)))}")
                    else:
                        parts.append(f"  - {st}")

        # Source metadata
        src = payload.source_metadata or {}
        if src:
            parts.append("\n## Source Context")
            parts.append(json.dumps(src, default=str, indent=2))

        # Media info
        decisions = run_state.media_decisions or {}
        selected = decisions.get("selected", [])
        unresolved = decisions.get("unresolved", [])

        if selected:
            parts.append(f"\n## Resolved Media ({len(selected)} items)")
            for i, m in enumerate(selected[:10]):
                parts.append(
                    f"  {i + 1}. source={m.get('source')}, "
                    f"channel_id={m.get('channel_id')}, "
                    f"message_id={m.get('message_id')}"
                )

        if unresolved:
            parts.append(f"\n## Unresolved Media ({len(unresolved)} items)")
            for i, m in enumerate(unresolved[:5]):
                parts.append(f"  {i + 1}. {json.dumps(m, default=str)}")

        # ── propose mode: media understanding summaries ───────────────
        if self._is_propose_mode():
            understood = run_state.media_understanding or []
            if understood:
                parts.append(
                    f"\n## Media Understanding ({len(understood)} items)"
                )
                parts.append(
                    "These are the topic editor's cached analyses of the "
                    "source media (Gemini/OpenAI). Ground every media "
                    "strategy in them — clip count, style range, quality, "
                    "energy. Do not invent media the source does not have."
                )
                for i, u in enumerate(understood, start=1):
                    if u.get("error"):
                        parts.append(
                            f"  {i}. msg {u.get('message_id')} "
                            f"({u.get('kind')}): NOT UNDERSTOOD — {u['error']}"
                        )
                        continue
                    bits = [f"kind={u.get('kind')}"]
                    if u.get("highlight_score") is not None:
                        bits.append(f"highlight={u['highlight_score']}")
                    if u.get("energy") is not None:
                        bits.append(f"energy={u['energy']}")
                    if u.get("pacing"):
                        bits.append(f"pacing={u['pacing']}")
                    if u.get("aesthetic_quality") is not None:
                        bits.append(f"aesthetic={u['aesthetic_quality']}")
                    head = u.get("summary") or ""
                    if u.get("subject"):
                        head = f"{head} | subject: {u['subject']}" if head else f"subject: {u['subject']}"
                    if u.get("technical_signal"):
                        head = f"{head} | tech: {u['technical_signal']}" if head else f"tech: {u['technical_signal']}"
                    if u.get("visual_read"):
                        head = f"{head} | visual: {u['visual_read']}" if head else f"visual: {u['visual_read']}"
                    parts.append(
                        f"  {i}. msg {u.get('message_id')} [{', '.join(bits)}]: {head}"
                    )
            else:
                parts.append("\n## Media Understanding")
                parts.append(
                    "No cached media understanding is available for the "
                    "source media. If the topic needs media, do not invent "
                    "a media strategy — either propose text-only ideas or "
                    "request_social_review."
                )

        # Publish units (always include for debugging)
        parts.append("\n## Publish Units")
        parts.append(json.dumps(units, default=str, indent=2))

        return "\n".join(parts)

    # ── tool specs ───────────────────────────────────────────────────

    @staticmethod
    def _is_queue_mode() -> bool:
        """Return True if queue mode is enabled via env var."""
        import os
        return os.getenv("LIVE_UPDATE_SOCIAL_MODE", "") == "queue"

    @staticmethod
    def _is_publish_mode() -> bool:
        """Return True if publish mode is enabled via env var."""
        import os
        return os.getenv("LIVE_UPDATE_SOCIAL_MODE", "") == "publish"

    @staticmethod
    def _is_propose_mode() -> bool:
        """Return True if propose mode is enabled via env var.

        Propose mode makes the agent propose post IDEAS (theme + media
        strategy) grounded in media understanding, instead of drafting a
        finished post. Set ``LIVE_UPDATE_SOCIAL_MODE=propose``.
        """
        import os
        return os.getenv("LIVE_UPDATE_SOCIAL_MODE", "") == "propose"

    def _build_tool_specs(self) -> List[Dict[str, Any]]:
        """Return tool definitions for the LLM.

        Publish mode: includes publish_social_post, excludes enqueue_social_post
        and draft/queue terminal tools. Always includes read tools.
        Queue mode: includes enqueue_social_post, excludes publish_social_post.
        Draft mode (default): only Sprint-1 terminal tools + read tools.

        Always includes read tools (get_live_update_topic, get_source_messages,
        get_published_update_context, inspect_message_media, list_social_routes,
        find_existing_social_posts, get_social_run_status).
        """
        specs: List[Any] = list(ALL_TOOL_SPECS)

        publish_mode = self._is_publish_mode()
        queue_mode = self._is_queue_mode()
        propose_mode = self._is_propose_mode()

        if publish_mode:
            # Exclude draft/queue terminal tools; keep publish_social_post
            specs = [ts for ts in specs if ts.name not in (
                "draft_social_post", "propose_social_ideas", "skip_social_post",
                "request_social_review", "enqueue_social_post",
            )]
        elif propose_mode:
            # Propose mode: keep propose_social_ideas, skip, review; exclude
            # draft/enqueue/publish terminals.
            specs = [ts for ts in specs if ts.name not in (
                "draft_social_post", "enqueue_social_post", "publish_social_post",
            )]
        elif queue_mode:
            # Exclude publish tool; keep enqueue_social_post
            specs = [ts for ts in specs if ts.name not in (
                "propose_social_ideas", "publish_social_post",
            )]
        else:
            # Draft mode: exclude both enqueue and publish
            specs = [ts for ts in specs if ts.name not in (
                "propose_social_ideas", "enqueue_social_post", "publish_social_post",
            )]

        return [ts.to_openai_tool() for ts in specs]
        # Note: to_openai_tool() produces Anthropic-compatible format because
        # Anthropic also uses the "input_schema" key (same structure).

    def _allowed_tool_names(self) -> frozenset:
        """Return the tool names allowed in the CURRENT mode.

        The parser accepts any name in ``ALL_TOOL_SPECS`` and every binding
        is registered, so prompt-only gating is not enough — a model in
        propose mode could emit ``draft_social_post`` and have it persist.
        This allowlist is enforced at parse AND dispatch time.
        """
        return frozenset(ts["name"] for ts in self._build_tool_specs())

    # ── LLM call ─────────────────────────────────────────────────────

    async def _call_llm(
        self,
        payload: LiveUpdateHandoffPayload,
        system_prompt: str,
        user_message: str,
        tools: List[Dict[str, Any]],
    ) -> str:
        """Call the LLM with chain settings and return the raw response text."""
        import os

        from src.common.llm import get_llm_response

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": user_message},
        ]

        # Live-update social drafting runs on DeepSeek by default, decoupled from
        # the (Anthropic-billed) chain vendor. Overridable via env if needed.
        client_name = (os.getenv("LIVE_UPDATE_SOCIAL_LLM_CLIENT") or "deepseek").strip().lower()

        # Determine model based on client + depth
        depth = payload.depth or "high"
        model = (
            (os.getenv("LIVE_UPDATE_SOCIAL_LLM_MODEL") or "").strip()
            or self._select_model(client_name, depth, payload)
        )

        logger.info(
            "LiveUpdateSocialAgent: calling %s model=%s depth=%s",
            client_name, model, depth,
        )

        try:
            response = await get_llm_response(
                client_name=client_name,
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=4096,
            )
            return response or ""
        except Exception as e:
            logger.error(
                "LiveUpdateSocialAgent: LLM call failed: %s", e, exc_info=True
            )
            raise

    @staticmethod
    def _select_model(client_name: str, depth: str, payload: LiveUpdateHandoffPayload) -> str:
        """Select the appropriate model based on chain fields."""
        if client_name == "deepseek":
            # Valid models on this DeepSeek endpoint: deepseek-v4-pro / -flash.
            return "deepseek-v4-pro"
        if client_name == "claude":
            if depth == "high":
                return "claude-opus-4-6"
            return "claude-sonnet-4-20250514"
        elif client_name == "openai":
            return "gpt-4o"
        elif client_name == "gemini":
            return "gemini-2.5-pro"
        # Default fallback
        return "deepseek-v4-pro"

    # ── tool call parsing ────────────────────────────────────────────

    def _parse_tool_call(self, llm_response: str) -> tuple[Optional[str], Dict[str, Any]]:
        """Parse the LLM response to extract tool name and parameters.

        The LLM is instructed to call exactly one tool.  We try to parse
        the response as JSON first (for structured tool-call responses),
        then fall back to heuristics.
        """
        if not llm_response:
            return None, {}

        # Mode allowlist — the model may only call tools advertised in the
        # current mode's tool specs. ALL_TOOL_SPECS is the full registry,
        # but e.g. propose mode must never accept a draft_social_post call.
        valid_names = self._allowed_tool_names()

        # Try to parse as JSON tool-call wrapper
        try:
            data = json.loads(llm_response)
            if isinstance(data, dict):
                tool_name = data.get("tool") or data.get("name") or data.get("tool_name")
                params = data.get("params") or data.get("parameters") or data.get("input") or {}
                if tool_name:
                    # Validate tool name
                    if tool_name in valid_names:
                        return tool_name, params
                    logger.warning(
                        "LiveUpdateSocialAgent: tool %r not allowed in current mode",
                        tool_name,
                    )
                    return None, {}
        except (json.JSONDecodeError, TypeError):
            pass

        import re

        # Strip markdown code fences the model may wrap the call in.
        cleaned = llm_response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

        # Function-call form, e.g. DeepSeek: `draft_social_post({"draft_text": "..."})`.
        # Extract the JSON argument object and use ITS fields — never the raw wrapper.
        m = re.search(
            r"(" + "|".join(re.escape(n) for n in valid_names) + r")\s*\(\s*(\{.*\})\s*\)",
            cleaned,
            re.DOTALL,
        )
        if m:
            try:
                parsed = json.loads(m.group(2))
                if isinstance(parsed, dict):
                    return m.group(1), parsed
            except json.JSONDecodeError:
                pass

        # Bare JSON object embedded in prose.
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            try:
                obj = json.loads(brace.group(0))
                if isinstance(obj, dict):
                    name = obj.get("tool") or obj.get("name") or obj.get("tool_name")
                    inner = obj.get("params") or obj.get("parameters") or obj.get("input")
                    if name in valid_names:
                        return name, inner if isinstance(inner, dict) else {
                            k: v for k, v in obj.items()
                            if k not in ("tool", "name", "tool_name")
                        }
                    if (
                        isinstance(obj.get("draft_text"), str)
                        and "draft_social_post" in valid_names
                    ):
                        return "draft_social_post", obj
            except json.JSONDecodeError:
                pass

        # Last resort: a tool name is mentioned but we could not extract a clean
        # argument. Pick a safe terminal — but NEVER store the raw response as a
        # draft (that leaks the tool-call wrapper into the tweet/DM). Respect the
        # mode allowlist: a disallowed terminal degrades to request_social_review.
        response_lower = cleaned.lower()
        if "skip_social_post" in response_lower and "skip_social_post" in valid_names:
            return "skip_social_post", {"reason": "Content not suitable for social posting"}
        if "get_social_run_status" in response_lower and "get_social_run_status" in valid_names:
            return "get_social_run_status", {}
        if any(n in response_lower for n in ("draft_social_post", "publish_social_post",
                                             "request_social_review", "find_existing_social_posts")):
            return "request_social_review", {
                "reason": "Could not parse a clean draft from the model response.",
            }

        return None, {}

    # ── tool dispatch ────────────────────────────────────────────────

    async def _dispatch_tool(
        self,
        run_state: RunState,
        tool_name: str,
        tool_params: Dict[str, Any],
    ) -> Optional[str]:
        """Dispatch the tool call through its ToolBinding handler."""
        if tool_name not in self._allowed_tool_names():
            logger.error(
                "LiveUpdateSocialAgent: tool %r not allowed in current mode "
                "(allowlist=%s)", tool_name, sorted(self._allowed_tool_names()),
            )
            return await self._force_needs_review(
                run_state,
                reason=f"Tool {tool_name} is not allowed in the current mode.",
            )

        binding = get_tool_by_name(self._bindings, tool_name)
        if not binding:
            logger.error(
                "LiveUpdateSocialAgent: no binding for tool %r", tool_name
            )
            return await self._force_needs_review(
                run_state,
                reason=f"No handler bound for tool: {tool_name}",
            )

        try:
            result = await binding.handler(run_state, tool_params)
            logger.info(
                "LiveUpdateSocialAgent: tool %r returned terminal_status=%r",
                tool_name,
                run_state.terminal_status,
            )
            run_state.add_trace(
                "tool_dispatched",
                tool=tool_name,
                terminal_status=run_state.terminal_status,
            )
            return run_state.terminal_status
        except Exception as e:
            logger.exception(
                "LiveUpdateSocialAgent: tool handler %r failed: %s",
                tool_name, e,
            )
            return await self._force_needs_review(
                run_state,
                reason=f"Tool handler {tool_name} failed: {e}",
            )

    async def _force_needs_review(
        self,
        run_state: RunState,
        reason: str,
    ) -> str:
        """Force the run into needs_review status."""
        run_state.terminal_status = "needs_review"
        run_state.add_trace("force_needs_review", reason=reason)

        self.db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="needs_review",
            trace_entries=run_state.trace_entries,
            media_decisions=run_state.media_decisions,
        )
        return "needs_review"
