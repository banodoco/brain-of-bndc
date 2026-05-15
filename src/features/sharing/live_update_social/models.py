"""Live Update Social models — Sprint 2 data types.

Defines MediaRefIdentity (durable), ResolvedMedia (transient), ToolSpec,
ToolBinding, RunState, and ToolResult for the social review loop including
queue-mode and media understanding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional


# ── Media identity (durable, no CDN URLs) ─────────────────────────────

@dataclass
class MediaRefIdentity:
    """Stable, durable identity for a piece of media.

    Uses Discord object references (channel_id, message_id, attachment_index
    or embed_slot) — **never** Discord CDN URLs — for durable identity.
    For non-Discord media set ``source='url'`` and provide ``url``.
    """

    source: Literal["discord_attachment", "discord_embed", "url"]
    channel_id: Optional[int] = None
    message_id: Optional[int] = None
    # For attachments: 0-based index in the message's attachment list.
    attachment_index: Optional[int] = None
    # For embeds: descriptive slot like "image", "thumbnail", "video", "author_icon".
    embed_slot: Optional[str] = None
    # For non-Discord media (source='url').
    url: Optional[str] = None

    def __post_init__(self):
        if self.source == "url" and not self.url:
            raise ValueError("MediaRefIdentity with source='url' must provide a url")
        if self.source in ("discord_attachment", "discord_embed"):
            if self.channel_id is None or self.message_id is None:
                raise ValueError(
                    f"MediaRefIdentity with source='{self.source}' requires "
                    f"channel_id and message_id"
                )
            if self.source == "discord_attachment" and self.attachment_index is None:
                raise ValueError(
                    "MediaRefIdentity with source='discord_attachment' requires "
                    "attachment_index"
                )
            if self.source == "discord_embed" and self.embed_slot is None:
                raise ValueError(
                    "MediaRefIdentity with source='discord_embed' requires embed_slot"
                )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "attachment_index": self.attachment_index,
            "embed_slot": self.embed_slot,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MediaRefIdentity":
        return cls(
            source=d["source"],
            channel_id=d.get("channel_id"),
            message_id=d.get("message_id"),
            attachment_index=d.get("attachment_index"),
            embed_slot=d.get("embed_slot"),
            url=d.get("url"),
        )


# ── Resolved media (transient, fresh URLs only) ───────────────────────

@dataclass
class ResolvedMedia:
    """Transient resolved media — holds fresh URLs or local file paths.

    The ``fresh_url`` is a freshly-refreshed Discord CDN URL (ephemeral).
    The ``local_path`` is a local file path if the media was downloaded.
    """

    identity: MediaRefIdentity
    fresh_url: Optional[str] = None
    local_path: Optional[str] = None
    content_type: Optional[str] = None
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "identity": self.identity.to_dict(),
            "fresh_url": self.fresh_url,
            "local_path": self.local_path,
            "content_type": self.content_type,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


# ── Tool spec and binding ─────────────────────────────────────────────

@dataclass
class ToolSpec:
    """Specification for a tool exposed to the LLM."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_openai_tool(self) -> dict:
        """Return an OpenAI-compatible tool definition."""
        props: Dict[str, Any] = {}
        required: List[str] = []
        schema = self.parameters.get("input_schema", self.parameters)
        if isinstance(schema, dict):
            props = schema.get("properties", {})
            required = schema.get("required", [])
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        }


@dataclass
class ToolBinding:
    """Binds a ToolSpec to its async handler callable."""

    tool_spec: ToolSpec
    handler: Callable[..., Any]  # async callable

    @property
    def name(self) -> str:
        return self.tool_spec.name


# ── Run state ─────────────────────────────────────────────────────────

@dataclass
class RunState:
    """In-memory representation of a live_update_social_runs row."""

    run_id: str
    topic_id: str
    platform: str
    action: str = "post"
    mode: str = "draft"
    terminal_status: Optional[str] = None
    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    chain_vendor: str = "codex"
    chain_depth: str = "high"
    chain_with_feedback: bool = True
    chain_deepseek_provider: str = "direct"
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    publish_units: Dict[str, Any] = field(default_factory=dict)
    draft_text: Optional[str] = None
    media_decisions: Dict[str, Any] = field(default_factory=dict)
    trace_entries: List[Dict[str, Any]] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "RunState":
        return cls(
            run_id=str(row.get("run_id", "")),
            topic_id=str(row.get("topic_id", "")),
            platform=str(row.get("platform", "")),
            action=str(row.get("action", "post")),
            mode=str(row.get("mode", "draft")),
            terminal_status=row.get("terminal_status"),
            guild_id=row.get("guild_id"),
            channel_id=row.get("channel_id"),
            chain_vendor=str(row.get("chain_vendor", "codex")),
            chain_depth=str(row.get("chain_depth", "high")),
            chain_with_feedback=bool(row.get("chain_with_feedback", True)),
            chain_deepseek_provider=str(row.get("chain_deepseek_provider", "direct")),
            source_metadata=row.get("source_metadata") or {},
            publish_units=row.get("publish_units") or {},
            draft_text=row.get("draft_text"),
            media_decisions=row.get("media_decisions") or {},
            trace_entries=row.get("trace_entries") or [],
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def add_trace(self, event: str, **kwargs) -> None:
        """Append a trace entry to the run."""
        entry: Dict[str, Any] = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
        entry.update(kwargs)
        self.trace_entries.append(entry)


# ── Tool result envelope ─────────────────────────────────────────────

@dataclass
class ToolResult:
    """Typed envelope for tool handler results with truncation metadata.

    All social-loop tools (read tools, media understanding tools, and
    queue tools) return this envelope so callers can inspect
    ``ok``, surface errors, and honour truncation hints without
    parsing loose dicts.

    Attributes
    ----------
    ok : bool
        Whether the tool executed successfully.
    tool_name : str
        Name of the tool that produced this result.
    data : dict
        Tool-specific result payload.
    truncated : bool
        True when ``data`` was truncated (e.g. long text or image descriptions).
    truncation_note : Optional[str]
        Human-readable note about what was truncated and why.
    error : Optional[str]
        Error message when ``ok`` is False.
    """

    ok: bool
    tool_name: str
    data: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    truncation_note: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "data": self.data,
            "truncated": self.truncated,
            "truncation_note": self.truncation_note,
            "error": self.error,
        }
