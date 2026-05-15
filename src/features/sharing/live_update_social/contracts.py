"""Typed handoff contract for live-update → social review.

First-class chain fields (vendor, depth, with_feedback, deepseek_provider)
are required on every payload so the social runtime can select models without
guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

# ── publish-result wrapper ───────────────────────────────────────────

@dataclass
class LiveUpdatePublishResult:
    """One platform result from a live-update publish run.

    ``status`` is the canonical publish status as returned by the live-update
    subsystem.  Only ``"sent"`` and ``"partial"`` are eligible for social
    review; everything else is silently dropped.
    """

    platform: str
    status: str  # "sent", "partial", "failed", "skipped", …
    action: str = "post"  # Sprint 1: only "post" is supported
    topic_id: str = ""
    publish_metadata: Dict[str, Any] = field(default_factory=dict)


# ── handoff payload ──────────────────────────────────────────────────

@dataclass
class LiveUpdateHandoffPayload:
    """The structured payload that triggers a social review run.

    Required fields
    ---------------
    topic_id : str
        Stable identity for the topic that was published.
    guild_id : int
        Discord guild responsible for the publish.
    channel_id : int
        Discord channel where the live-update message lives.
    platform : str
        Target social platform (e.g. ``"twitter"``, ``"youtube"``).
    action : str
        Must be ``"post"`` in Sprint 1.
    status : str
        Canonical publish result status.  Only ``"sent"`` and ``"partial"``
        are processed; everything else is rejected *before* run creation.
    source_metadata : dict
        Caller-supplied metadata snapshot (e.g. which cog triggered this).
    topic_summary_data : dict
        The reconstructed topic summary used for publish-unit building.

    Chain fields (first-class)
    --------------------------
    vendor : str = "codex"
    depth : str = "high"
    with_feedback : bool = True
    deepseek_provider : str = "direct"
    """

    topic_id: str
    guild_id: int
    channel_id: int
    platform: str
    action: str = "post"
    status: str = ""
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    topic_summary_data: Dict[str, Any] = field(default_factory=dict)

    # ── chain fields ──
    vendor: str = "codex"
    depth: str = "high"
    with_feedback: bool = True
    deepseek_provider: str = "direct"

    # Sprint-1 guard: explicitly reject non-post actions before run creation.
    ALLOWED_ACTIONS: frozenset = field(default=frozenset({"post"}), init=False, repr=False)
    ALLOWED_ACTIONS_PUBLISH: frozenset = frozenset({"post", "reply", "quote"})
    ALLOWED_STATUSES: frozenset = field(
        default=frozenset({"sent", "partial"}), init=False, repr=False
    )

    def is_eligible(self) -> bool:
        """Return True when this payload should create a social review run."""
        return self.action in self.ALLOWED_ACTIONS and self.status in self.ALLOWED_STATUSES

    @staticmethod
    def is_eligible_for_publish_mode() -> bool:
        """Return True when the runtime is in publish mode."""
        import os
        return os.getenv("LIVE_UPDATE_SOCIAL_MODE", "") == "publish"
