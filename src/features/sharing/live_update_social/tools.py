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

from .failure_reasons import FailureReason
from .models import PublishOutcome, ToolSpec, ToolBinding, RunState, ToolResult

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

# ── Sprint 3 publish tool ──────────────────────────────────────────────

TOOL_PUBLISH_SOCIAL_POST = ToolSpec(
    name="publish_social_post",
    description=(
        "Publish a social media post immediately (publish mode). "
        "Use this when LIVE_UPDATE_SOCIAL_MODE=publish. "
        "For a single post provide draft_text and optionally selected_media. "
        "For a thread provide thread_items (list of {index, draft_text, media_refs}) "
        "where index 0 is the root post and subsequent items are replies. "
        "Media understanding runs unless skip_media_understanding=true "
        "with a recorded skip_media_reason."
    ),
    parameters={
        "type": "object",
        "properties": {
            "draft_text": {
                "type": "string",
                "description": (
                    "The social media post text. Required for single-post publish; "
                    "optional when thread_items is provided."
                ),
            },
            "selected_media": {
                "type": "array",
                "description": (
                    "List of selected media identities "
                    "(channel_id, message_id, attachment_index or embed_slot)."
                ),
                "items": {"type": "object"},
            },
            "thread_items": {
                "type": "array",
                "description": (
                    "For thread publishing: list of thread item objects. "
                    "Each item must have index (int, 0=root), draft_text (str), "
                    "and optionally media_refs (list of media identities) and "
                    "target_post_ref (str, for reply chaining — LLM-set for items "
                    "after index 0)."
                ),
                "items": {"type": "object"},
            },
            "skip_media_understanding": {
                "type": "boolean",
                "description": (
                    "Set to true to skip media understanding. "
                    "Must provide skip_media_reason when true."
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

# ── Sprint 3 read tools ─────────────────────────────────────────────────

TOOL_FIND_EXISTING_SOCIAL_POSTS = ToolSpec(
    name="find_existing_social_posts",
    description=(
        "Find existing social publications related to this live-update topic. "
        "Returns matching social_publications rows with optional content "
        "similarity scores. Use this to check for duplicates or related posts "
        "before publishing or deciding on a quote/reply strategy."
    ),
    parameters={
        "type": "object",
        "properties": {
            "draft_text": {
                "type": "string",
                "description": "Optional draft text for content similarity checking.",
            },
        },
        "required": [],
    },
)

TOOL_GET_SOCIAL_RUN_STATUS = ToolSpec(
    name="get_social_run_status",
    description=(
        "Return the current social run's status, including terminal_status, "
        "draft_text, media_decisions, publication_outcome, trace_entries, "
        "and linked social_publications rows. Use this to inspect the outcome "
        "of a previous publish or review decision."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
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
    # Publish tool (Sprint 3)
    TOOL_PUBLISH_SOCIAL_POST,
    # Read tools (Sprint 3)
    TOOL_FIND_EXISTING_SOCIAL_POSTS,
    TOOL_GET_SOCIAL_RUN_STATUS,
]

# ── shared media understanding helper (Sprint 3) ──────────────────────


async def _run_media_understanding_and_upload(
    db_handler: "DatabaseHandler",
    selected_media: list,
    skip_understanding: bool = False,
    skip_reason: str = "",
) -> tuple:
    """Run media understanding and durable upload for selected media items.

    Extracted from ``_make_enqueue_handler`` so the publish handler (T6)
    can reuse the same logic without duplication.

    Args:
        db_handler: Database handler for durable storage uploads.
        selected_media: List of media item dicts with ``source_url``,
            ``content_type``, and ``identity`` keys.
        skip_understanding: If True, skip media understanding entirely.
        skip_reason: Required reason when skip_understanding is True.

    Returns:
        Tuple of ``(media_hints, understanding_results, media_failures)``.
        Each is a list of dicts.

        ``media_hints`` items have ``durable_url``, ``content_type``,
        ``identity``, and ``understanding_summary`` (or ``fallback_reason``
        when upload failed).
    """
    media_hints: list = []
    media_understanding_results: list = []
    media_failures: list = []

    if not selected_media:
        return media_hints, media_understanding_results, media_failures

    if skip_understanding:
        # Nothing to understand — caller is responsible for skip_reason
        # validation.  Still upload if items have source_urls.
        for i, media_item in enumerate(selected_media):
            source_url = media_item.get("source_url") or media_item.get("url")
            if not source_url:
                continue
            identity = media_item.get("identity", {})
            content_type = media_item.get("content_type", "")
            media_understanding_results.append({
                "index": i,
                "identity": identity,
                "source_url": source_url,
                "content_type": content_type,
                "understanding": "",
                "skipped": True,
            })
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
                    "_run_media_understanding_and_upload: media understanding "
                    "error for item %d: %s", i, e, exc_info=True,
                )
                media_failures.append({
                    "index": i,
                    "identity": identity,
                    "source_url": source_url,
                    "error": str(e),
                })

    # ── durable upload ─────────────────────────────────────────────
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

    return media_hints, media_understanding_results, media_failures


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

        # ── media understanding + durable upload (shared helper) ────
        media_hints, media_understanding_results, media_failures = (
            await _run_media_understanding_and_upload(
                db_handler=db_handler,
                selected_media=selected_media,
                skip_understanding=skip_understanding,
                skip_reason=skip_reason,
            )
        )

        # ── validate skip_reason when skipping ─────────────────────
        if selected_media and skip_understanding and not skip_reason:
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

        if skip_understanding and skip_reason:
            run_state.add_trace(
                "tool", tool=tool_name,
                media_understanding_skipped=True,
                skip_reason=skip_reason,
            )

        # ── all-failed guard ────────────────────────────────────────
        if (
            not skip_understanding
            and selected_media
            and not media_understanding_results
        ):
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


# ── publish handler factory (Sprint 3) ──────────────────────────────────


def _make_publish_handler(
    db_handler: "DatabaseHandler",
    social_publish_service: Any = None,
    bot: Optional["discord.Client"] = None,
    force_publish: bool = False,
) -> Any:
    """Return a handler that publishes a social post immediately.

    Gates:
      - LIVE_UPDATE_SOCIAL_MODE must be ``\"publish\"`` (unless ``force_publish``).
      - ``social_publish_service`` must be available.

    Behaviour:
      - Validates draft_text (or thread_items with text).
      - Resolves bot user_details (screen_name + user_id from Twitter API).
      - Runs duplicate/similarity guard via find_existing_social_posts.
      - Validates social route is configured.
      - Runs shared media understanding via ``_run_media_understanding_and_upload``.
      - For single post: calls ``social_publish_service.publish_now()``.
      - For thread: publishes item 0 as top-level post, subsequent items as
        replies with target_post_ref chaining; aborts on root failure.
      - Verifies media attached in provider result.
      - Classifies failures via ``classify_failure``.
      - Persists ``publication_outcome`` on the run.

    Thread path: publish item 0 as top-level post, subsequent items as
    reply with ``target_post_ref`` chaining; abort on root-post failure;
    record per-item outcomes.

    Args:
        force_publish: When True, skip the LIVE_UPDATE_SOCIAL_MODE check.
            Used by SocialReviewCog.approve to force-publish without
            mutating the environment.
    """
    import os

    async def handler(run_state: RunState, params: dict) -> dict:
        tool_name = "publish_social_post"
        draft_text = params.get("draft_text", "")
        selected_media = params.get("selected_media", [])
        thread_items_raw = params.get("thread_items", [])
        skip_understanding = params.get("skip_media_understanding", False)
        skip_reason = params.get("skip_media_reason", "")

        # ── gate: publish mode required (unless force_publish) ─────────
        live_mode = os.getenv("LIVE_UPDATE_SOCIAL_MODE", "")
        if not force_publish and live_mode != "publish":
            logger.error(
                "publish_social_post: LIVE_UPDATE_SOCIAL_MODE=%r, not 'publish'",
                live_mode,
            )
            run_state.add_trace(
                "tool", tool=tool_name,
                error="Publish mode not enabled (LIVE_UPDATE_SOCIAL_MODE != 'publish')",
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": "Publish mode is not enabled. Set LIVE_UPDATE_SOCIAL_MODE=publish.",
            }

        # ── gate: social_publish_service required ────────────────────
        if social_publish_service is None:
            logger.error("publish_social_post: social_publish_service is None")
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

        # ── resolve thread vs single post ───────────────────────────
        is_thread = bool(thread_items_raw)
        if is_thread:
            # Validate thread structure: item 0 must have text
            if not thread_items_raw:
                run_state.add_trace(
                    "tool", tool=tool_name,
                    error="thread_items is empty",
                )
                return {
                    "tool": tool_name,
                    "terminal_status": None,
                    "ok": False,
                    "error": "thread_items is empty.",
                }
            root = thread_items_raw[0]
            if not isinstance(root, dict):
                root = {}
            root_text = root.get("draft_text", "")
            if not root_text:
                run_state.add_trace(
                    "tool", tool=tool_name,
                    error="Root thread item missing draft_text",
                )
                return {
                    "tool": tool_name,
                    "terminal_status": None,
                    "ok": False,
                    "error": "Root thread item (index 0) requires draft_text.",
                }
        else:
            # Single post: validate draft_text
            if not draft_text:
                run_state.add_trace(
                    "tool", tool=tool_name,
                    error="draft_text is empty",
                )
                return {
                    "tool": tool_name,
                    "terminal_status": None,
                    "ok": False,
                    "error": "draft_text is required for publish.",
                }

        # ── resolve bot user_details ─────────────────────────────────
        user_details = await _resolve_bot_user_details()
        if not user_details or not user_details.get("screen_name"):
            logger.error("publish_social_post: could not resolve bot user_details")
            run_state.add_trace(
                "tool", tool=tool_name,
                error="Could not resolve bot Twitter user_details",
            )
            return {
                "tool": tool_name,
                "terminal_status": None,
                "ok": False,
                "error": "Could not resolve bot Twitter user_details.",
            }

        # ── duplicate/similarity guard ───────────────────────────────
        existing_posts = db_handler.find_existing_social_posts(
            topic_id=run_state.topic_id,
            platform=run_state.platform,
            guild_id=run_state.guild_id,
            draft_text=draft_text or "",
        )
        import os as _os
        sim_threshold = float(_os.getenv("SOCIAL_DUPLICATE_SIMILARITY_THRESHOLD", "0.7"))
        for ep in existing_posts:
            sim = ep.get("_similarity")
            if sim is not None and sim >= sim_threshold:
                logger.warning(
                    "publish_social_post: duplicate similarity %.2f >= %.2f "
                    "for topic=%s publication_id=%s",
                    sim, sim_threshold,
                    run_state.topic_id,
                    ep.get("publication_id"),
                )
                error_msg = (
                    f"Content similarity {sim:.2f} exceeds threshold "
                    f"{sim_threshold} with existing publication "
                    f"{ep.get('publication_id')}"
                )
                run_state.add_trace(
                    "tool", tool=tool_name,
                    duplicate_similarity=True,
                    error=error_msg,
                )
                outcome = PublishOutcome(
                    success=False,
                    error=error_msg,
                    failure_reason=FailureReason.DUPLICATE_PREVENTED.value,
                )
                run_state.publication_outcome = outcome
                db_handler.update_live_update_social_run(
                    run_id=run_state.run_id,
                    terminal_status="published",
                    publication_outcome=outcome.to_dict(),
                    trace_entries=run_state.trace_entries,
                )
                return {
                    "tool": tool_name,
                    "terminal_status": "published",
                    "ok": True,
                    "duplicate_prevented": True,
                    "note": error_msg,
                }

        # ── validate social route ────────────────────────────────────
        from .helpers import resolve_social_route as _resolve_route
        route = _resolve_route(
            db_handler=db_handler,
            guild_id=run_state.guild_id or 0,
            channel_id=run_state.channel_id or 0,
            platform=run_state.platform,
        )
        if not route:
            error_msg = f"No social route configured for {run_state.platform}"
            logger.error("publish_social_post: %s", error_msg)
            run_state.add_trace(
                "tool", tool=tool_name,
                route_missing=True,
                error=error_msg,
            )
            outcome = PublishOutcome(
                success=False,
                error=error_msg,
                failure_reason=FailureReason.ROUTE_MISSING.value,
            )
            run_state.publication_outcome = outcome
            db_handler.update_live_update_social_run(
                run_id=run_state.run_id,
                terminal_status="published",
                publication_outcome=outcome.to_dict(),
                trace_entries=run_state.trace_entries,
            )
            return {
                "tool": tool_name,
                "terminal_status": "published",
                "ok": True,
                "route_missing": True,
                "error": error_msg,
            }

        # ── media understanding + durable upload (shared helper) ─────
        media_hints, media_understanding_results, media_failures = (
            await _run_media_understanding_and_upload(
                db_handler=db_handler,
                selected_media=selected_media,
                skip_understanding=skip_understanding,
                skip_reason=skip_reason,
            )
        )

        # Validate skip_reason when skipping
        if selected_media and skip_understanding and not skip_reason:
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

        # ── store media decisions ────────────────────────────────────
        decisions = run_state.media_decisions or {}
        decisions.setdefault("understanding_summaries", [])
        for r in media_understanding_results:
            decisions["understanding_summaries"].append(r)
        decisions.setdefault("media_failures", [])
        decisions["media_failures"].extend(media_failures)
        decisions["selected"] = selected_media
        run_state.media_decisions = decisions

        # ── build source context ─────────────────────────────────────
        try:
            from src.features.sharing.models import PublicationSourceContext
        except ImportError:
            from ....sharing.models import PublicationSourceContext

        src_meta = run_state.source_metadata or {}
        source_context = PublicationSourceContext(
            source_kind="live_update_social",
            metadata={
                "topic_id": run_state.topic_id,
                "run_id": run_state.run_id,
                "source_message_ids": src_meta.get("source_message_ids", []),
                "user_details": user_details,
            },
        )

        # ── publish ──────────────────────────────────────────────────
        if is_thread:
            return await _publish_thread(
                run_state=run_state,
                thread_items_raw=thread_items_raw,
                media_hints=media_hints,
                media_failures=media_failures,
                source_context=source_context,
                db_handler=db_handler,
                social_publish_service=social_publish_service,
                user_details=user_details,
            )
        else:
            return await _publish_single_post(
                run_state=run_state,
                draft_text=draft_text,
                media_hints=media_hints,
                media_failures=media_failures,
                source_context=source_context,
                db_handler=db_handler,
                social_publish_service=social_publish_service,
                user_details=user_details,
            )

    return handler


async def _publish_single_post(
    *,
    run_state: RunState,
    draft_text: str,
    media_hints: list,
    media_failures: list,
    source_context: Any,
    db_handler: "DatabaseHandler",
    social_publish_service: Any,
    user_details: dict,
) -> dict:
    """Publish a single social post via publish_now."""
    tool_name = "publish_social_post"
    from src.features.sharing.models import SocialPublishRequest

    request = SocialPublishRequest(
        message_id=0,
        channel_id=run_state.channel_id or 0,
        guild_id=run_state.guild_id or 0,
        user_id=0,
        platform=run_state.platform,
        action="post",
        text=draft_text,
        media_hints=media_hints,
        source_kind="live_update_social",
        duplicate_policy={
            "enabled": True,
            "source": "live_update_social_runs+social_publications",
        },
        source_context=source_context,
    )

    try:
        result = await social_publish_service.publish_now(request)
    except Exception as e:
        logger.error(
            "publish_social_post: SocialPublishService.publish_now failed: %s",
            e, exc_info=True,
        )
        return _publish_failure_response(
            run_state, db_handler, tool_name, draft_text, media_hints,
            media_failures, str(e),
            context={"error": "provider_publish_failed"},
        )

    return await _process_publish_result(
        run_state=run_state,
        db_handler=db_handler,
        tool_name=tool_name,
        publish_result=result,
        media_hints=media_hints,
        media_failures=media_failures,
        draft_text=draft_text,
    )


async def _publish_thread(
    *,
    run_state: RunState,
    thread_items_raw: list,
    media_hints: list,
    media_failures: list,
    source_context: Any,
    db_handler: "DatabaseHandler",
    social_publish_service: Any,
    user_details: dict,
) -> dict:
    """Publish a thread via sequential publish_now calls."""
    tool_name = "publish_social_post"
    from src.features.sharing.models import SocialPublishRequest

    per_item_outcomes: list = []
    last_provider_ref: Optional[str] = None

    for i, item_raw in enumerate(thread_items_raw):
        if not isinstance(item_raw, dict):
            item_raw = {}
        item_text = item_raw.get("draft_text", "")
        item_media_refs = item_raw.get("media_refs", [])
        item_target_ref = item_raw.get("target_post_ref") or last_provider_ref

        # Build media hints for this item from items' media_refs
        item_media_hints = []
        if item_media_refs:
            for mr in item_media_refs:
                if isinstance(mr, dict):
                    for mh in media_hints:
                        mh_id = mh.get("identity") or {}
                        if mh_id == mr:
                            item_media_hints.append(mh)
                            break

        # For root post (index 0), use action='post'
        action = "reply" if i > 0 else "post"

        # Platform gating: reply only on X/Twitter
        if action == "reply" and run_state.platform.lower() not in ("twitter", "x"):
            logger.error(
                "publish_social_post: reply action not supported on %s",
                run_state.platform,
            )
            per_item_outcomes.append({
                "index": i,
                "success": False,
                "error": f"Reply not supported on {run_state.platform}",
            })
            # Abort remaining items on unsupported platform for replies
            break

        request = SocialPublishRequest(
            message_id=0,
            channel_id=run_state.channel_id or 0,
            guild_id=run_state.guild_id or 0,
            user_id=0,
            platform=run_state.platform,
            action=action,
            text=item_text,
            media_hints=item_media_hints if item_media_hints else [],
            target_post_ref=item_target_ref if action == "reply" else None,
            source_kind="live_update_social",
            duplicate_policy={
                "enabled": True,
                "source": "live_update_social_runs+social_publications",
            },
            source_context=source_context,
        )

        try:
            result = await social_publish_service.publish_now(request)
        except Exception as e:
            logger.error(
                "publish_social_post: thread item %d publish failed: %s",
                i, e, exc_info=True,
            )
            per_item_outcomes.append({
                "index": i,
                "success": False,
                "error": str(e),
            })
            if i == 0:
                # Root-post failure — abort thread
                run_state.add_trace(
                    "tool", tool=tool_name,
                    thread_root_failed=True,
                    error=str(e),
                )
                outcome = PublishOutcome(
                    success=False,
                    error=str(e),
                    failure_reason=FailureReason.THREAD_ROOT_FAILED.value,
                    per_item_outcomes=per_item_outcomes,
                )
                run_state.publication_outcome = outcome
                run_state.terminal_status = "published"
                db_handler.update_live_update_social_run(
                    run_id=run_state.run_id,
                    terminal_status="published",
                    publication_outcome=outcome.to_dict(),
                    trace_entries=run_state.trace_entries,
                )
                return {
                    "tool": tool_name,
                    "terminal_status": "published",
                    "ok": True,
                    "thread_root_failed": True,
                    "error": f"Root post failed: {e}",
                }
            # Non-root failure — continue with next item
            continue

        if not result.success:
            per_item_outcomes.append({
                "index": i,
                "success": False,
                "error": result.error or "Unknown publish failure",
            })
            if i == 0:
                run_state.add_trace(
                    "tool", tool=tool_name,
                    thread_root_failed=True,
                    error=result.error,
                )
                outcome = PublishOutcome(
                    success=False,
                    error=result.error,
                    failure_reason=FailureReason.THREAD_ROOT_FAILED.value,
                    per_item_outcomes=per_item_outcomes,
                )
                run_state.publication_outcome = outcome
                run_state.terminal_status = "published"
                db_handler.update_live_update_social_run(
                    run_id=run_state.run_id,
                    terminal_status="published",
                    publication_outcome=outcome.to_dict(),
                    trace_entries=run_state.trace_entries,
                )
                return {
                    "tool": tool_name,
                    "terminal_status": "published",
                    "ok": True,
                    "thread_root_failed": True,
                    "error": f"Root post failed: {result.error}",
                }
            continue

        # Success — record outcome and update chain
        outcome_entry = {
            "index": i,
            "success": True,
            "publication_id": result.publication_id,
            "provider_ref": result.provider_ref,
            "provider_url": result.provider_url,
            "media_ids": result.media_ids,
        }
        per_item_outcomes.append(outcome_entry)
        last_provider_ref = result.provider_ref

        # Verify media attached
        item_media_attached = []
        item_media_missing = []
        item_expected_media_count = len(item_media_hints) if item_media_refs else len(media_hints) if i == 0 else 0
        if item_expected_media_count > 0:
            if not result.media_ids:
                for mh in (item_media_hints if item_media_hints else media_hints):
                    item_media_missing.append(mh.get("identity", {}))
            else:
                for mh in (item_media_hints if item_media_hints else media_hints):
                    item_media_attached.append(mh.get("identity", {}))

        if item_media_missing and result.publication_id:
            db_handler.update_social_publication_media_outcome(
                publication_id=result.publication_id,
                media_attached=item_media_attached,
                media_missing=item_media_missing,
                guild_id=run_state.guild_id,
            )

    # Build aggregate outcome
    all_success = all(o.get("success") for o in per_item_outcomes)
    first_ref = per_item_outcomes[0].get("provider_ref") if per_item_outcomes else None
    first_url = per_item_outcomes[0].get("provider_url") if per_item_outcomes else None
    all_media_ids: list = []
    for o in per_item_outcomes:
        all_media_ids.extend(o.get("media_ids") or [])

    outcome = PublishOutcome(
        success=all_success,
        provider_ref=first_ref,
        provider_url=first_url,
        media_ids=all_media_ids,
        per_item_outcomes=per_item_outcomes,
    )
    run_state.publication_outcome = outcome
    run_state.terminal_status = "published"
    run_state.draft_text = draft_text = thread_items_raw[0].get("draft_text", "") if thread_items_raw else ""
    run_state.media_decisions = run_state.media_decisions or {}
    run_state.add_trace(
        "tool", tool=tool_name,
        terminal_status="published",
        thread_items=len(per_item_outcomes),
        thread_success=all_success,
    )

    db_handler.update_live_update_social_run(
        run_id=run_state.run_id,
        terminal_status="published",
        draft_text=run_state.draft_text,
        media_decisions=run_state.media_decisions,
        publication_outcome=outcome.to_dict(),
        trace_entries=run_state.trace_entries,
    )

    return {
        "tool": tool_name,
        "terminal_status": "published",
        "ok": True,
        "publication_ids": [o.get("publication_id") for o in per_item_outcomes],
        "provider_refs": [o.get("provider_ref") for o in per_item_outcomes],
        "thread_success": all_success,
        "per_item_outcomes": per_item_outcomes,
    }


async def _process_publish_result(
    *,
    run_state: RunState,
    db_handler: "DatabaseHandler",
    tool_name: str,
    publish_result: Any,
    media_hints: list,
    media_failures: list,
    draft_text: str,
) -> dict:
    """Process a single-post publish result, verifying media and persisting outcome."""
    if not publish_result.success:
        return _publish_failure_response(
            run_state, db_handler, tool_name, draft_text, media_hints,
            media_failures, publish_result.error or "Unknown publish failure",
            context={"error": "provider_publish_failed"},
        )

    # ── verify media attached ─────────────────────────────────────────
    media_attached: list = []
    media_missing: list = []
    if media_hints:
        if not publish_result.media_ids:
            # Media was requested but not present in result
            for mh in media_hints:
                media_missing.append(mh.get("identity", {}))
        else:
            for mh in media_hints:
                media_attached.append(mh.get("identity", {}))

    # Persist media outcome on the publication row
    if media_missing and publish_result.publication_id:
        db_handler.update_social_publication_media_outcome(
            publication_id=publish_result.publication_id,
            media_attached=media_attached,
            media_missing=media_missing,
            guild_id=run_state.guild_id,
        )

    # ── build publish outcome ──────────────────────────────────────────
    outcome = PublishOutcome(
        publication_id=publish_result.publication_id,
        success=True,
        provider_ref=publish_result.provider_ref,
        provider_url=publish_result.provider_url,
        media_ids=publish_result.media_ids,
        media_attached=media_attached,
        media_missing=media_missing,
    )
    run_state.publication_outcome = outcome
    run_state.terminal_status = "published"
    run_state.draft_text = draft_text
    run_state.media_decisions = run_state.media_decisions or {}
    run_state.add_trace(
        "tool", tool=tool_name,
        terminal_status="published",
        publication_id=publish_result.publication_id,
        provider_ref=publish_result.provider_ref,
        media_attached_count=len(media_attached),
        media_missing_count=len(media_missing),
    )

    db_handler.update_live_update_social_run(
        run_id=run_state.run_id,
        terminal_status="published",
        draft_text=draft_text,
        media_decisions=run_state.media_decisions,
        publication_outcome=outcome.to_dict(),
        trace_entries=run_state.trace_entries,
    )

    return {
        "tool": tool_name,
        "terminal_status": "published",
        "ok": True,
        "publication_id": publish_result.publication_id,
        "provider_ref": publish_result.provider_ref,
        "provider_url": publish_result.provider_url,
        "media_ids": publish_result.media_ids,
        "media_attached_count": len(media_attached),
        "media_missing_count": len(media_missing),
    }


def _publish_failure_response(
    run_state: RunState,
    db_handler: "DatabaseHandler",
    tool_name: str,
    draft_text: str,
    media_hints: list,
    media_failures: list,
    error_msg: str,
    context: Optional[dict] = None,
) -> dict:
    """Build a failure response for publish, persisting the outcome."""
    from .failure_reasons import classify_failure
    failure_reason = classify_failure(error_message=error_msg, context=context)

    outcome = PublishOutcome(
        success=False,
        error=error_msg,
        failure_reason=failure_reason.value,
    )
    run_state.publication_outcome = outcome
    run_state.terminal_status = "published"
    run_state.draft_text = draft_text
    run_state.media_decisions = run_state.media_decisions or {}
    run_state.add_trace(
        "tool", tool=tool_name,
        terminal_status="published",
        error=error_msg,
        failure_reason=failure_reason.value,
    )

    db_handler.update_live_update_social_run(
        run_id=run_state.run_id,
        terminal_status="published",
        draft_text=draft_text,
        media_decisions=run_state.media_decisions,
        publication_outcome=outcome.to_dict(),
        trace_entries=run_state.trace_entries,
    )

    return {
        "tool": tool_name,
        "terminal_status": "published",
        "ok": True,
        "publication_id": None,
        "error": error_msg,
        "failure_reason": failure_reason.value,
    }


# ── bot user details resolver ──────────────────────────────────────────

_cached_bot_user_details: Optional[dict] = None


async def _resolve_bot_user_details() -> Optional[dict]:
    """Resolve bot Twitter user_details (screen_name + user_id).

    Uses Tweepy verify_credentials to get the authenticated bot user's
    screen name and numeric ID.  Results are cached for the lifetime
    of the process.
    """
    global _cached_bot_user_details
    if _cached_bot_user_details:
        return _cached_bot_user_details

    import asyncio
    import tweepy

    from src.features.sharing.subfeatures.social_poster import (
        CONSUMER_KEY,
        CONSUMER_SECRET,
        ACCESS_TOKEN,
        ACCESS_TOKEN_SECRET,
    )

    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        logger.error(
            "_resolve_bot_user_details: Twitter API credentials missing"
        )
        return None

    try:
        auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
        auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
        api_v1 = tweepy.API(auth)
        credentials = await asyncio.get_event_loop().run_in_executor(
            None, api_v1.verify_credentials,
        )
        screen_name = getattr(credentials, 'screen_name', 'user')
        user_id = getattr(credentials, 'id_str', None)
        if not user_id:
            user_id = str(getattr(credentials, 'id', ''))
        _cached_bot_user_details = {
            "screen_name": screen_name,
            "user_id": user_id,
        }
        return _cached_bot_user_details
    except Exception as e:
        logger.error(
            "_resolve_bot_user_details: verify_credentials failed: %s",
            e, exc_info=True,
        )
        return None


# ── find-existing-posts handler (Sprint 3) ────────────────────────────


def _make_find_existing_posts_handler(
    db_handler: "DatabaseHandler",
) -> Any:
    """Return a handler for the find_existing_social_posts read tool."""

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        draft_text = params.get("draft_text") or run_state.draft_text
        posts = db_handler.find_existing_social_posts(
            topic_id=run_state.topic_id,
            platform=run_state.platform,
            guild_id=run_state.guild_id,
            draft_text=draft_text,
        )

        # Summarise similarity for readability
        summary = []
        for p in posts:
            entry = {
                "publication_id": p.get("publication_id"),
                "status": p.get("status"),
                "created_at": p.get("created_at"),
                "provider_ref": p.get("provider_ref"),
                "provider_url": p.get("provider_url"),
            }
            sim = p.get("_similarity")
            if sim is not None:
                entry["_similarity"] = round(sim, 4)
            summary.append(entry)

        return ToolResult(
            ok=True,
            tool_name="find_existing_social_posts",
            data={
                "topic_id": run_state.topic_id,
                "platform": run_state.platform,
                "count": len(posts),
                "posts": summary,
            },
        )

    return handler


# ── get-social-run-status handler (Sprint 3) ──────────────────────────


def _make_get_run_status_handler(
    db_handler: "DatabaseHandler",
) -> Any:
    """Return a handler for the get_social_run_status read tool."""

    async def handler(run_state: RunState, params: dict) -> ToolResult:
        # Gather linked publications
        publications = db_handler.get_social_publications_by_run_id(
            run_id=run_state.run_id,
        )
        pub_summary = []
        for p in publications:
            pub_summary.append({
                "publication_id": p.get("publication_id"),
                "status": p.get("status"),
                "action": p.get("action"),
                "platform": p.get("platform"),
                "provider_ref": p.get("provider_ref"),
                "provider_url": p.get("provider_url"),
                "media_attached": p.get("media_attached"),
                "media_missing": p.get("media_missing"),
                "created_at": p.get("created_at"),
            })

        outcome_dict = None
        if run_state.publication_outcome:
            outcome_dict = run_state.publication_outcome.to_dict()

        return ToolResult(
            ok=True,
            tool_name="get_social_run_status",
            data={
                "run_id": run_state.run_id,
                "topic_id": run_state.topic_id,
                "platform": run_state.platform,
                "action": run_state.action,
                "mode": run_state.mode,
                "terminal_status": run_state.terminal_status,
                "draft_text": run_state.draft_text,
                "media_decisions": run_state.media_decisions,
                "publication_outcome": outcome_dict,
                "trace_entries": run_state.trace_entries,
                "linked_publications": pub_summary,
            },
        )

    return handler


# ── binding builder ────────────────────────────────────────────────────


def build_tool_bindings(
    db_handler: "DatabaseHandler",
    bot: Optional["discord.Client"] = None,
    social_publish_service: Any = None,
) -> List[ToolBinding]:
    """Return all tool bindings (terminal + read + queue + publish tools).

    Each ToolBinding pairs a ToolSpec with its async handler.
    The optional ``bot`` parameter enables read tools that inspect
    Discord messages; without it those tools return ToolResult(ok=False).
    The optional ``social_publish_service`` enables the enqueue/publish
    tools; without it those tools return ok=False with an error.
    """
    bindings: List[ToolBinding] = [
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
        # Publish tool (Sprint 3) — gated by social_publish_service
        ToolBinding(
            tool_spec=TOOL_PUBLISH_SOCIAL_POST,
            handler=_make_publish_handler(
                db_handler,
                social_publish_service=social_publish_service,
                bot=bot,
            ),
        ),
        # Read tools (Sprint 3)
        ToolBinding(
            tool_spec=TOOL_FIND_EXISTING_SOCIAL_POSTS,
            handler=_make_find_existing_posts_handler(db_handler),
        ),
        ToolBinding(
            tool_spec=TOOL_GET_SOCIAL_RUN_STATUS,
            handler=_make_get_run_status_handler(db_handler),
        ),
    ]
    return bindings


def get_tool_by_name(bindings: List[ToolBinding], name: str) -> Optional[ToolBinding]:
    """Find a ToolBinding by name."""
    for b in bindings:
        if b.name == name:
            return b
    return None
