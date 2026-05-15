"""Live Update Social tool registry — Sprint 2 read + terminal tools.

Terminal decision tools (Sprint 1)::

    draft_social_post     — set terminal_status='draft' with draft_text
    skip_social_post      — set terminal_status='skip'
    request_social_review — set terminal_status='needs_review'

Social-loop read tools (Sprint 2)::

    get_live_update_topic       — returns the full topic summary for the current run
    get_source_messages         — fetches source Discord messages with fresh CDN URLs
    get_published_update_context — returns the published live-update message metadata
    inspect_message_media       — inspects media on a specific Discord message
    list_social_routes          — lists configured social routes for the guild

Each tool has exactly one handler binding.  Handlers return typed
``ToolResult`` envelopes with truncation metadata.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .models import ToolSpec, ToolBinding, RunState, ToolResult

if TYPE_CHECKING:
    import discord
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")

# ── tool definitions ──────────────────────────────────────────────────

TOOL_DRAFT_SOCIAL_POST = ToolSpec(
    name="draft_social_post",
    description=(
        "Record a draft social media post for human review. "
        "Use this when content and media are ready for review. "
        "The draft text and selected media identities will be persisted "
        "for inspection."
    ),
    parameters={
        "type": "object",
        "properties": {
            "draft_text": {
                "type": "string",
                "description": "The draft social media post text.",
            },
            "selected_media": {
                "type": "array",
                "description": (
                    "List of selected media identities "
                    "(channel_id, message_id, attachment_index or embed_slot)."
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["draft_text"],
    },
)

TOOL_SKIP_SOCIAL_POST = ToolSpec(
    name="skip_social_post",
    description=(
        "Skip social posting for this topic. Use this when the content "
        "should not be posted to social media (e.g., not newsworthy, "
        "duplicate, or intentionally excluded)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason for skipping.",
            },
        },
        "required": [],
    },
)

TOOL_REQUEST_SOCIAL_REVIEW = ToolSpec(
    name="request_social_review",
    description=(
        "Request human review for this social post. Use this when you "
        "cannot make a confident decision — e.g., media URLs could not "
        "be resolved, route configuration is missing, or the content "
        "needs human judgment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why human review is needed.",
            },
        },
        "required": ["reason"],
    },
)

# ── Sprint 2 read tools ────────────────────────────────────────────────

TOOL_GET_LIVE_UPDATE_TOPIC = ToolSpec(
    name="get_live_update_topic",
    description=(
        "Retrieve the full live-update topic summary for the current social review run. "
        "Returns title, sub-topics, source metadata, and publish unit configuration. "
        "Use this to understand what content the live update contains before deciding "
        "whether and how to post to social media."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

TOOL_GET_SOURCE_MESSAGES = ToolSpec(
    name="get_source_messages",
    description=(
        "Retrieve the source Discord messages that triggered this live-update topic, "
        "including their content, author, timestamps, and attachment metadata. "
        "Media URLs returned are fresh Discord CDN URLs (ephemeral — do NOT persist "
        "them as durable identities). Use this to review the original messages that "
        "inspired the live update."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of source messages to return (default: 5).",
            },
        },
        "required": [],
    },
)

TOOL_GET_PUBLISHED_UPDATE_CONTEXT = ToolSpec(
    name="get_published_update_context",
    description=(
        "Retrieve metadata for the live-update Discord message that was already "
        "published (the bot's own post), including its message ID, channel ID, "
        "reactions, and publish timestamp. Use this to understand what the bot "
        "already told the community before drafting a social post."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

TOOL_INSPECT_MESSAGE_MEDIA = ToolSpec(
    name="inspect_message_media",
    description=(
        "Inspect all media (attachments and embeds) on a specific Discord message. "
        "Returns structured media metadata with fresh CDN URLs and content types. "
        "Use this before calling understand_image or understand_video to identify "
        "which media items are available for analysis."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message_id": {
                "type": "integer",
                "description": "The Discord message ID to inspect for media.",
            },
            "channel_id": {
                "type": "integer",
                "description": "The Discord channel ID containing the message.",
            },
        },
        "required": ["message_id", "channel_id"],
    },
)

TOOL_LIST_SOCIAL_ROUTES = ToolSpec(
    name="list_social_routes",
    description=(
        "List configured social media routes for this guild, including platform, "
        "account credentials, and enabled/disabled status. Use this to verify that "
        "a social route exists for the target platform before drafting or enqueuing "
        "a social post."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

# ── Sprint 2 queue tool ────────────────────────────────────────────────

TOOL_ENQUEUE_SOCIAL_POST = ToolSpec(
    name="enqueue_social_post",
    description=(
        "Enqueue a social media post for durable publication. "
        "Use this when the draft is approved and media understanding has been "
        "completed. The post will be persisted to the social_publications queue "
        "and published asynchronously. Queue mode must be enabled "
        "(LIVE_UPDATE_SOCIAL_MODE=queue). Media-heavy posts require successful "
        "media understanding unless explicitly skipped with a recorded reason."
    ),
    parameters={
        "type": "object",
        "properties": {
            "draft_text": {
                "type": "string",
                "description": "The approved social media post text to enqueue.",
            },
            "selected_media": {
                "type": "array",
                "description": (
                    "List of selected media identities with understanding "
                    "summaries. Each item must have identity fields and "
                    "optionally understanding_summary and source_url."
                ),
                "items": {"type": "object"},
            },
            "skip_media_understanding": {
                "type": "boolean",
                "description": (
                    "Set to true to skip media understanding for this post. "
                    "Must provide a skip_media_reason when true."
                ),
            },
            "skip_media_reason": {
                "type": "string",
                "description": "Required reason when skip_media_understanding is true.",
            },
        },
        "required": ["draft_text"],
    },
)

# ── aggregated registry ───────────────────────────────────────────────

ALL_TOOL_SPECS: List[ToolSpec] = [
    # Terminal tools (Sprint 1)
    TOOL_DRAFT_SOCIAL_POST,
    TOOL_SKIP_SOCIAL_POST,
    TOOL_REQUEST_SOCIAL_REVIEW,
    # Read tools (Sprint 2)
    TOOL_GET_LIVE_UPDATE_TOPIC,
    TOOL_GET_SOURCE_MESSAGES,
    TOOL_GET_PUBLISHED_UPDATE_CONTEXT,
    TOOL_INSPECT_MESSAGE_MEDIA,
    TOOL_LIST_SOCIAL_ROUTES,
    # Queue tool (Sprint 2)
    TOOL_ENQUEUE_SOCIAL_POST,
]

# ── handler factories ─────────────────────────────────────────────────
# Each returns an async callable that updates the run and returns a result dict.


def _make_draft_handler(db_handler: "DatabaseHandler") -> Any:
    async def handler(run_state: RunState, params: dict) -> dict:
        draft_text = params.get("draft_text", "")
        selected_media = params.get("selected_media", [])

        # Build media_decisions
        decisions = run_state.media_decisions or {}
        decisions["selected"] = selected_media
        decisions.setdefault("considered", [])
        decisions.setdefault("skipped", [])
        decisions.setdefault("unresolved", [])

        run_state.terminal_status = "draft"
        run_state.draft_text = draft_text
        run_state.media_decisions = decisions
        run_state.add_trace("tool", tool="draft_social_post", draft_len=len(draft_text))

        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="draft",
            draft_text=draft_text,
            media_decisions=decisions,
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error("draft_social_post: DB update failed for run %s", run_state.run_id)
        return {"tool": "draft_social_post", "terminal_status": "draft", "ok": ok}

    return handler


def _make_skip_handler(db_handler: "DatabaseHandler") -> Any:
    async def handler(run_state: RunState, params: dict) -> dict:
        reason = params.get("reason", "no reason given")

        run_state.terminal_status = "skip"
        run_state.add_trace("tool", tool="skip_social_post", reason=reason)

        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="skip",
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error("skip_social_post: DB update failed for run %s", run_state.run_id)
        return {"tool": "skip_social_post", "terminal_status": "skip", "reason": reason, "ok": ok}

    return handler


def _make_request_review_handler(db_handler: "DatabaseHandler") -> Any:
    async def handler(run_state: RunState, params: dict) -> dict:
        reason = params.get("reason", "")

        run_state.terminal_status = "needs_review"
        run_state.add_trace("tool", tool="request_social_review", reason=reason)

        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="needs_review",
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error("request_social_review: DB update failed for run %s", run_state.run_id)
        return {"tool": "request_social_review", "terminal_status": "needs_review", "reason": reason, "ok": ok}

    return handler


# ── read-tool handler factories (Sprint 2) ──────────────────────────────
# These are safe for any mode — they only read, never mutate.


def _make_get_topic_handler() -> Any:
    """Return a handler that retrieves the live-update topic summary.

    Reads from run_state.publish_units (the stored topic_summary_data).
    """

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        publish_units = run_state.publish_units or {}
        return ToolResult(
            ok=True,
            tool_name="get_live_update_topic",
            data={
                "topic_id": run_state.topic_id,
                "title": publish_units.get("title", ""),
                "sub_topics": publish_units.get("sub_topics", []),
                "message_id": publish_units.get("message_id"),
                "channel_id": publish_units.get("channel_id"),
                "source_metadata": publish_units.get("source_metadata", {}),
            },
        )

    return handler


def _make_get_source_messages_handler(
    bot: Optional["discord.Client"] = None,
) -> Any:
    """Return a handler that fetches source Discord messages with fresh CDN URLs."""

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        limit = int(params.get("limit", 5))
        publish_units = run_state.publish_units or {}
        source_metadata = publish_units.get("source_metadata", {}) or {}

        # Collect message IDs to inspect
        msg_id = publish_units.get("message_id")
        media_msg_id = publish_units.get("media_message_id") or source_metadata.get(
            "mainMediaMessageId"
        )
        channel_id = publish_units.get("channel_id") or run_state.channel_id

        message_ids = []
        if msg_id:
            message_ids.append(int(msg_id))
        if media_msg_id and media_msg_id != msg_id:
            message_ids.append(int(media_msg_id))

        if not message_ids:
            return ToolResult(
                ok=True,
                tool_name="get_source_messages",
                data={"messages": [], "note": "No source message IDs in topic data."},
            )

        if not bot:
            return ToolResult(
                ok=False,
                tool_name="get_source_messages",
                error="Bot client not available — cannot fetch Discord messages.",
            )

        from .helpers import inspect_discord_message

        messages = []
        errors = []
        for mid in message_ids[:limit]:
            try:
                inspected = await inspect_discord_message(
                    bot=bot,
                    channel_id=int(channel_id),
                    message_id=mid,
                )
                if inspected.get("error"):
                    errors.append({"message_id": mid, "error": inspected["error"]})
                else:
                    messages.append(inspected)
            except Exception as e:
                errors.append({"message_id": mid, "error": str(e)})

        return ToolResult(
            ok=True,
            tool_name="get_source_messages",
            data={"messages": messages, "errors": errors, "total_requested": limit},
        )

    return handler


def _make_get_published_update_context_handler(
    bot: Optional["discord.Client"] = None,
) -> Any:
    """Return a handler that retrieves the published live-update message metadata."""

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        source_metadata = run_state.source_metadata or {}
        publish_units = run_state.publish_units or {}

        # Collect known published message IDs from source metadata
        published_messages = source_metadata.get("published_messages", [])
        if not published_messages:
            # Fallback: use topic message_id from publish_units
            msg_id = publish_units.get("message_id")
            channel_id = publish_units.get("channel_id") or run_state.channel_id
            if msg_id and channel_id:
                published_messages = [{"message_id": msg_id, "channel_id": channel_id}]

        result_data: Dict[str, Any] = {
            "published_messages": [],
            "source_metadata": source_metadata,
        }

        if bot and published_messages:
            from .helpers import inspect_discord_message

            for pm in published_messages[:5]:
                try:
                    inspected = await inspect_discord_message(
                        bot=bot,
                        channel_id=int(pm.get("channel_id", run_state.channel_id or 0)),
                        message_id=int(pm.get("message_id", 0)),
                    )
                    if not inspected.get("error"):
                        result_data["published_messages"].append(inspected)
                except Exception:
                    pass

        return ToolResult(
            ok=True,
            tool_name="get_published_update_context",
            data=result_data,
        )

    return handler


def _make_inspect_message_media_handler(
    bot: Optional["discord.Client"] = None,
) -> Any:
    """Return a handler that inspects media on a specific Discord message."""

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        message_id = params.get("message_id")
        channel_id = params.get("channel_id")

        if not message_id or not channel_id:
            return ToolResult(
                ok=False,
                tool_name="inspect_message_media",
                error="Both message_id and channel_id are required.",
            )

        if not bot:
            return ToolResult(
                ok=False,
                tool_name="inspect_message_media",
                error="Bot client not available — cannot inspect Discord media.",
            )

        from .helpers import refresh_discord_media_urls

        try:
            media_data = await refresh_discord_media_urls(
                bot=bot,
                channel_id=int(channel_id),
                message_id=int(message_id),
            )
            return ToolResult(
                ok=True,
                tool_name="inspect_message_media",
                data={
                    "message_id": int(message_id),
                    "channel_id": int(channel_id),
                    "attachments": media_data.get("attachments", []),
                    "embeds_media": media_data.get("embeds_media", []),
                },
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                tool_name="inspect_message_media",
                error=f"Failed to inspect message media: {e}",
            )

    return handler


def _make_list_social_routes_handler(
    db_handler: "DatabaseHandler",
) -> Any:
    """Return a handler that lists social routes for the current guild."""

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        guild_id = run_state.guild_id
        if not guild_id:
            return ToolResult(
                ok=False,
                tool_name="list_social_routes",
                error="No guild_id in run state — cannot list routes.",
            )

        from .helpers import list_social_routes as _list_routes

        try:
            routes = _list_routes(
                db_handler=db_handler,
                guild_id=int(guild_id),
                channel_id=run_state.channel_id,
            )
            return ToolResult(
                ok=True,
                tool_name="list_social_routes",
                data={"routes": routes, "guild_id": int(guild_id)},
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                tool_name="list_social_routes",
                error=f"Failed to list social routes: {e}",
            )

    return handler


# ── enqueue handler factory (Sprint 2 queue mode) ──────────────────────


def _make_enqueue_handler(
    db_handler: "DatabaseHandler",
    social_publish_service: Any = None,
    bot: Optional["discord.Client"] = None,
) -> Any:
    """Return a handler that enqueues a social post for durable publication.

    Gates:
      - LIVE_UPDATE_SOCIAL_MODE must be ``\"queue\"``.
      - ``social_publish_service`` must be available.

    Behaviour:
      - Validates draft_text presence.
      - Runs media understanding on selected media (unless skipped with
        a recorded reason).
      - Uploads media to durable storage via the durable_media module.
      - Checks for duplicate publications against live_update_social_runs
        and social_publications.
      - Builds a SocialPublishRequest and calls
        social_publish_service.enqueue().
      - Updates run_state with publication_id and trace entries.
    """
    import os

    async def handler(run_state: RunState, params: dict) -> dict:
        tool_name = "enqueue_social_post"
        draft_text = params.get("draft_text", "")
        selected_media = params.get("selected_media", [])
        skip_understanding = params.get("skip_media_understanding", False)
        skip_reason = params.get("skip_media_reason", "")

        # ── gate: queue mode required ──────────────────────────────
        live_mode = os.getenv("LIVE_UPDATE_SOCIAL_MODE", "")
        if live_mode != "queue":
            logger.error(
                "enqueue_social_post: LIVE_UPDATE_SOCIAL_MODE=%r, not 'queue'",
                live_mode,
            )
            run_state.add_trace(
                "tool", tool=tool_name,
                error="Queue mode not enabled (LIVE_UPDATE_SOCIAL_MODE != 'queue')",
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": "Queue mode is not enabled. Set LIVE_UPDATE_SOCIAL_MODE=queue.",
            }

        # ── gate: social_publish_service required ──────────────────
        if social_publish_service is None:
            logger.error(
                "enqueue_social_post: social_publish_service is None"
            )
            run_state.add_trace(
                "tool", tool=tool_name,
                error="SocialPublishService not available",
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": "SocialPublishService is not available.",
            }

        # ── validate draft_text ────────────────────────────────────
        if not draft_text:
            run_state.add_trace(
                "tool", tool=tool_name,
                error="draft_text is empty",
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": "draft_text is required for enqueue.",
            }

        # ── duplicate guard: check existing runs ───────────────────
        existing_row = db_handler.check_live_update_social_duplicate_publication(
            topic_id=run_state.topic_id,
            platform=run_state.platform,
            action=run_state.action,
            guild_id=run_state.guild_id,
        )
        if existing_row:
            logger.warning(
                "enqueue_social_post: duplicate publication found for "
                "topic=%s platform=%s action=%s — publication_id=%s",
                run_state.topic_id,
                run_state.platform,
                run_state.action,
                existing_row.get("publication_id"),
            )
            run_state.add_trace(
                "tool", tool=tool_name,
                error="Duplicate publication exists",
                existing_publication_id=existing_row.get("publication_id"),
            )
            return {
                "tool": tool_name,
                "terminal_status": "queued",
                "ok": True,
                "publication_id": existing_row.get("publication_id"),
                "duplicate": True,
                "note": "Publication already exists for this topic.",
            }

        # ── media understanding ────────────────────────────────────
        media_hints: list = []
        media_understanding_results: list = []
        media_failures: list = []

        if selected_media:
            if skip_understanding:
                if not skip_reason:
                    run_state.add_trace(
                        "tool", tool=tool_name,
                        error="skip_media_reason required when skip_media_understanding=True",
                    )
                    return {
                        "tool": tool_name,
                        "terminal_status": None,
                        "ok": False,
                        "error": (
                            "skip_media_reason is required when "
                            "skip_media_understanding is true."
                        ),
                    }
                run_state.add_trace(
                    "tool", tool=tool_name,
                    media_understanding_skipped=True,
                    skip_reason=skip_reason,
                )
            else:
                from .media_understanding import understand_image, understand_video

                for i, media_item in enumerate(selected_media):
                    source_url = media_item.get("source_url") or media_item.get("url")
                    content_type = media_item.get("content_type", "")
                    identity = media_item.get("identity", {})

                    if not source_url:
                        media_failures.append({
                            "index": i,
                            "identity": identity,
                            "error": "No source_url for media understanding",
                        })
                        continue

                    # Determine image vs video
                    is_video = bool(
                        content_type
                        and (
                            content_type.startswith("video/")
                            or "video" in content_type.lower()
                        )
                    )

                    try:
                        if is_video:
                            result = await understand_video(
                                source_url=source_url,
                                content_type=content_type,
                            )
                        else:
                            result = await understand_image(
                                source_url=source_url,
                                content_type=content_type,
                            )

                        if result.ok:
                            media_understanding_results.append({
                                "index": i,
                                "identity": identity,
                                "source_url": source_url,
                                "content_type": content_type,
                                "understanding": result.data.get("description", ""),
                            })
                        else:
                            media_failures.append({
                                "index": i,
                                "identity": identity,
                                "source_url": source_url,
                                "error": result.error or "Media understanding failed",
                            })
                    except Exception as e:
                        logger.error(
                            "enqueue_social_post: media understanding error for "
                            "item %d: %s", i, e, exc_info=True,
                        )
                        media_failures.append({
                            "index": i,
                            "identity": identity,
                            "source_url": source_url,
                            "error": str(e),
                        })

            # If media understanding was attempted and all failed, refuse
            if not skip_understanding and selected_media and not media_understanding_results:
                run_state.add_trace(
                    "tool", tool=tool_name,
                    media_understanding_failed=True,
                    failures=len(media_failures),
                )
                return {
                    "tool": tool_name,
                    "terminal_status": None,
                    "ok": False,
                    "error": (
                        "Media understanding failed for all selected media items. "
                        "Set skip_media_understanding=true with a reason to proceed "
                        "without understanding, or use request_social_review."
                    ),
                    "media_failures": media_failures,
                }

            # ── durable upload ─────────────────────────────────────
            from .durable_media import upload_to_durable_storage

            for item in media_understanding_results:
                source_url = item.get("source_url", "")
                content_type = item.get("content_type", "")
                identity = item.get("identity", {})

                durable_url, upload_error = await upload_to_durable_storage(
                    db_handler=db_handler,
                    source_url=source_url,
                    content_type=content_type,
                )

                if durable_url:
                    media_hints.append({
                        "durable_url": durable_url,
                        "content_type": content_type,
                        "identity": identity,
                        "understanding_summary": item.get("understanding", ""),
                    })
                else:
                    # Record failure but allow other media to proceed
                    media_failures.append({
                        "identity": identity,
                        "source_url": source_url,
                        "error": upload_error or "Durable upload failed",
                    })
                    fallback_media_hint = {
                        "content_type": content_type,
                        "identity": identity,
                        "understanding_summary": item.get("understanding", ""),
                        "fallback_reason": upload_error or "Durable upload failed",
                    }
                    media_hints.append(fallback_media_hint)

        # ── store media decisions ──────────────────────────────────
        decisions = run_state.media_decisions or {}
        decisions.setdefault("understanding_summaries", [])
        for r in media_understanding_results:
            decisions["understanding_summaries"].append(r)
        decisions.setdefault("media_failures", [])
        decisions["media_failures"].extend(media_failures)
        decisions["selected"] = selected_media
        run_state.media_decisions = decisions

        # ── build SocialPublishRequest ─────────────────────────────
        try:
            from src.features.sharing.models import (
                SocialPublishRequest,
                PublicationSourceContext,
            )
        except ImportError:
            from ....sharing.models import (
                SocialPublishRequest,
                PublicationSourceContext,
            )

        # Determine message_id: prefer publish_units message_id, fallback to
        # source_metadata published_messages first entry
        publish_units = run_state.publish_units or {}
        src_meta = run_state.source_metadata or {}
        message_id = publish_units.get("message_id")
        if not message_id:
            published_msgs = src_meta.get("published_messages", [])
            if published_msgs:
                message_id = published_msgs[0].get("message_id")
        if not message_id:
            message_id = 0

        channel_id = run_state.channel_id or publish_units.get("channel_id") or 0
        guild_id = run_state.guild_id or 0

        source_context = PublicationSourceContext(
            source_kind="live_update_social",
            metadata={
                "topic_id": run_state.topic_id,
                "run_id": run_state.run_id,
                "source_message_ids": src_meta.get("source_message_ids", []),
                "live_update_discord_message_ids": src_meta.get(
                    "published_messages", [],
                ),
            },
        )

        request = SocialPublishRequest(
            message_id=int(message_id) if message_id else 0,
            channel_id=int(channel_id) if channel_id else 0,
            guild_id=int(guild_id) if guild_id else 0,
            user_id=0,  # bot-owned post — user_id will be set by the bot
            platform=run_state.platform,
            action=run_state.action,
            text=draft_text,
            media_hints=media_hints,
            source_kind="live_update_social",
            duplicate_policy={
                "enabled": True,
                "source": "live_update_social_runs+social_publications",
            },
            source_context=source_context,
        )

        # ── enqueue via SocialPublishService ───────────────────────
        try:
            publish_result = await social_publish_service.enqueue(request)
        except Exception as e:
            logger.error(
                "enqueue_social_post: SocialPublishService.enqueue failed: %s",
                e, exc_info=True,
            )
            run_state.add_trace(
                "tool", tool=tool_name,
                error=f"SocialPublishService.enqueue failed: {e}",
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": f"Enqueue failed: {e}",
            }

        if not publish_result.success:
            error_msg = publish_result.error or "Unknown enqueue failure"
            logger.error(
                "enqueue_social_post: enqueue returned failure: %s", error_msg
            )
            run_state.add_trace(
                "tool", tool=tool_name,
                error=error_msg,
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": error_msg,
            }

        publication_id = publish_result.publication_id
        logger.info(
            "enqueue_social_post: enqueued publication_id=%s for topic=%s",
            publication_id, run_state.topic_id,
        )

        # ── update run state ──────────────────────────────────────
        run_state.terminal_status = "queued"
        run_state.draft_text = draft_text
        run_state.media_decisions = decisions
        run_state.add_trace(
            "tool", tool=tool_name,
            terminal_status="queued",
            publication_id=publication_id,
            media_hints_count=len(media_hints),
            media_failures_count=len(media_failures),
        )

        # Persist run state update
        ok = db_handler.update_live_update_social_run(
            run_id=run_state.run_id,
            terminal_status="queued",
            draft_text=draft_text,
            media_decisions=decisions,
            trace_entries=run_state.trace_entries,
        )
        if not ok:
            logger.error(
                "enqueue_social_post: DB update failed for run %s "
                "(publication %s was created)",
                run_state.run_id, publication_id,
            )

        return {
            "tool": tool_name,
            "terminal_status": "queued",
            "ok": True,
            "publication_id": publication_id,
            "media_hints_count": len(media_hints),
            "media_failures_count": len(media_failures),
        }

    return handler


# ── binding builder ────────────────────────────────────────────────────


def build_tool_bindings(
    db_handler: "DatabaseHandler",
    bot: Optional["discord.Client"] = None,
    social_publish_service: Any = None,
) -> List[ToolBinding]:
    """Return all tool bindings (terminal + read + queue tools).

    Each ToolBinding pairs a ToolSpec with its async handler.
    The optional ``bot`` parameter enables read tools that inspect
    Discord messages; without it those tools return ToolResult(ok=False).
    The optional ``social_publish_service`` enables the enqueue tool;
    without it the enqueue tool returns ok=False with an error.
    """
    return [
        # Terminal tools (Sprint 1)
        ToolBinding(
            tool_spec=TOOL_DRAFT_SOCIAL_POST,
            handler=_make_draft_handler(db_handler),
        ),
        ToolBinding(
            tool_spec=TOOL_SKIP_SOCIAL_POST,
            handler=_make_skip_handler(db_handler),
        ),
        ToolBinding(
            tool_spec=TOOL_REQUEST_SOCIAL_REVIEW,
            handler=_make_request_review_handler(db_handler),
        ),
        # Read tools (Sprint 2)
        ToolBinding(
            tool_spec=TOOL_GET_LIVE_UPDATE_TOPIC,
            handler=_make_get_topic_handler(),
        ),
        ToolBinding(
            tool_spec=TOOL_GET_SOURCE_MESSAGES,
            handler=_make_get_source_messages_handler(bot),
        ),
        ToolBinding(
            tool_spec=TOOL_GET_PUBLISHED_UPDATE_CONTEXT,
            handler=_make_get_published_update_context_handler(bot),
        ),
        ToolBinding(
            tool_spec=TOOL_INSPECT_MESSAGE_MEDIA,
            handler=_make_inspect_message_media_handler(bot),
        ),
        ToolBinding(
            tool_spec=TOOL_LIST_SOCIAL_ROUTES,
            handler=_make_list_social_routes_handler(db_handler),
        ),
        # Queue tool (Sprint 2)
        ToolBinding(
            tool_spec=TOOL_ENQUEUE_SOCIAL_POST,
            handler=_make_enqueue_handler(
                db_handler,
                social_publish_service=social_publish_service,
                bot=bot,
            ),
        ),
    ]


def get_tool_by_name(bindings: List[ToolBinding], name: str) -> Optional[ToolBinding]:
    """Find a ToolBinding by name."""
    for b in bindings:
        if b.name == name:
            return b
    return None
