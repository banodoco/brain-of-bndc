from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union


SocialAction = Literal['post', 'reply', 'retweet', 'quote']
SocialSourceKind = Literal['admin_chat', 'reaction_bridge', 'summary', 'reaction_auto', 'live_update_social']
RouteOverride = Union[str, Dict[str, Any]]


@dataclass
class PublicationSourceContext:
    """Caller-specific source context that survives past the initial request."""

    source_kind: SocialSourceKind
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SocialPublishRequest:
    """Normalized outbound publish request shared by all caller paths."""

    message_id: int
    channel_id: int
    guild_id: int
    user_id: int
    platform: str
    action: SocialAction = 'post'
    scheduled_at: Optional[datetime] = None
    target_post_ref: Optional[str] = None
    route_override: Optional[RouteOverride] = None
    text: Optional[str] = None
    media_hints: List[Dict[str, Any]] = field(default_factory=list)
    source_kind: SocialSourceKind = 'admin_chat'
    duplicate_policy: Dict[str, Any] = field(default_factory=dict)
    text_only: bool = False
    announce_policy: Dict[str, Any] = field(default_factory=dict)
    first_share_notification_policy: Dict[str, Any] = field(default_factory=dict)
    legacy_shared_post_policy: Dict[str, Any] = field(default_factory=dict)
    moderation_metadata: Dict[str, Any] = field(default_factory=dict)
    consent_metadata: Dict[str, Any] = field(default_factory=dict)
    source_context: Optional[PublicationSourceContext] = None

    def __post_init__(self) -> None:
        if self.source_context is None:
            self.source_context = PublicationSourceContext(source_kind=self.source_kind)
        elif self.source_context.source_kind != self.source_kind:
            self.source_context = PublicationSourceContext(
                source_kind=self.source_kind,
                metadata=dict(self.source_context.metadata),
            )


@dataclass
class SocialPublishResult:
    """Canonical publish result returned to callers and notification flows."""

    publication_id: Optional[str]
    success: bool
    tweet_id: Optional[str] = None
    tweet_url: Optional[str] = None
    provider_ref: Optional[str] = None
    provider_url: Optional[str] = None
    delete_supported: bool = False
    already_shared: bool = False
    error: Optional[str] = None
