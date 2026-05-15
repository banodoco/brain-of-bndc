"""Live Update Social Review loop — Sprint 3 publish mode, threads, review controls."""

from .contracts import (
    LiveUpdatePublishResult,
    LiveUpdateHandoffPayload,
)
from .service import LiveUpdateSocialService
from .models import (
    MediaRefIdentity,
    ResolvedMedia,
    ToolSpec,
    ToolBinding,
    RunState,
    ToolResult,
    ThreadItem,
    ThreadDraft,
    PublishOutcome,
)
from .tools import (
    TOOL_DRAFT_SOCIAL_POST,
    TOOL_SKIP_SOCIAL_POST,
    TOOL_REQUEST_SOCIAL_REVIEW,
    TOOL_GET_LIVE_UPDATE_TOPIC,
    TOOL_GET_SOURCE_MESSAGES,
    TOOL_GET_PUBLISHED_UPDATE_CONTEXT,
    TOOL_INSPECT_MESSAGE_MEDIA,
    TOOL_LIST_SOCIAL_ROUTES,
    TOOL_ENQUEUE_SOCIAL_POST,
    TOOL_PUBLISH_SOCIAL_POST,
    TOOL_FIND_EXISTING_SOCIAL_POSTS,
    TOOL_GET_SOCIAL_RUN_STATUS,
    ALL_TOOL_SPECS,
    build_tool_bindings,
    get_tool_by_name,
    _run_media_understanding_and_upload,
)
from .failure_reasons import (
    FailureReason,
    classify_failure,
)
from .publish_units import reconstruct_publish_units
from .helpers import (
    inspect_discord_message,
    refresh_discord_media_urls,
    download_media_url,
    list_social_routes,
    resolve_social_route,
)
from .agent import LiveUpdateSocialAgent
from .durable_media import (
    SOCIAL_MEDIA_BUCKET_DEFAULT,
    MAX_IMAGE_SIZE_BYTES,
    MAX_VIDEO_SIZE_BYTES,
    MAX_TOTAL_MEDIA_SIZE_BYTES,
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    validate_media_batch,
    upload_to_durable_storage,
    upload_media_batch_to_durable,
)
from .media_understanding import (
    understand_image,
    understand_video,
)

# Public alias for the shared media-understanding helper (task requirement:
# "run_media_understanding_and_upload" without underscore prefix)
run_media_understanding_and_upload = _run_media_understanding_and_upload

__all__ = [
    # contracts
    "LiveUpdatePublishResult",
    "LiveUpdateHandoffPayload",
    # service
    "LiveUpdateSocialService",
    # agent
    "LiveUpdateSocialAgent",
    # models
    "MediaRefIdentity",
    "ResolvedMedia",
    "ToolSpec",
    "ToolBinding",
    "RunState",
    "ToolResult",
    # models (Sprint 3)
    "ThreadItem",
    "ThreadDraft",
    "PublishOutcome",
    # tools
    "TOOL_DRAFT_SOCIAL_POST",
    "TOOL_SKIP_SOCIAL_POST",
    "TOOL_REQUEST_SOCIAL_REVIEW",
    "TOOL_GET_LIVE_UPDATE_TOPIC",
    "TOOL_GET_SOURCE_MESSAGES",
    "TOOL_GET_PUBLISHED_UPDATE_CONTEXT",
    "TOOL_INSPECT_MESSAGE_MEDIA",
    "TOOL_LIST_SOCIAL_ROUTES",
    "TOOL_ENQUEUE_SOCIAL_POST",
    "TOOL_PUBLISH_SOCIAL_POST",
    "TOOL_FIND_EXISTING_SOCIAL_POSTS",
    "TOOL_GET_SOCIAL_RUN_STATUS",
    "ALL_TOOL_SPECS",
    "build_tool_bindings",
    "get_tool_by_name",
    # tools (Sprint 3 shared helper)
    "run_media_understanding_and_upload",
    # failure reasons (Sprint 3)
    "FailureReason",
    "classify_failure",
    # publish_units
    "reconstruct_publish_units",
    # helpers
    "inspect_discord_message",
    "refresh_discord_media_urls",
    "download_media_url",
    "list_social_routes",
    "resolve_social_route",
    # durable media
    "SOCIAL_MEDIA_BUCKET_DEFAULT",
    "MAX_IMAGE_SIZE_BYTES",
    "MAX_VIDEO_SIZE_BYTES",
    "MAX_TOTAL_MEDIA_SIZE_BYTES",
    "ALLOWED_IMAGE_TYPES",
    "ALLOWED_VIDEO_TYPES",
    "validate_media_batch",
    "upload_to_durable_storage",
    "upload_media_batch_to_durable",
    # media understanding
    "understand_image",
    "understand_video",
]
