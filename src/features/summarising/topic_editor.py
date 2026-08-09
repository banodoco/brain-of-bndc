"""
Deterministic core for the topic-centered live-update editor.

This module intentionally keeps canonicalization, alias resolution, collision
checks, and transition payload shaping pure so they can be tested without
Anthropic or Discord dependencies.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import math
import re
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
import discord

from src.features.summarising.live_update_prompts import DEFAULT_LIVE_UPDATE_MODEL
from src.common.external_media import extract_external_urls  # T6: shared helper
from src.common.urls import message_jump_url
from src.features.sharing.live_update_social import LiveUpdateHandoffPayload


logger = logging.getLogger("DiscordBot")

SIMILARITY_COLLISION_THRESHOLD = 0.55

READ_TOOL_NAMES = {
    "search_topics",
    "search_messages",
    "get_author_profile",
    "get_message_context",
    "get_reply_chain",
    "understand_image",
    "understand_video",
}

WRITE_TOOL_NAMES = {
    "create_draft",
    "edit_draft",
    "validate_draft",
    "preview_draft",
    "submit_draft",
    "abandon_draft",
    "post_topic",
    "post_simple_topic",
    "post_sectioned_topic",
    "watch_topic",
    "update_topic_source_messages",
    "discard_topic",
    "record_observation",
    "finalize_run",
}

LEGACY_POST_TOOL_NAMES = {"post_topic", "post_simple_topic", "post_sectioned_topic"}
DRAFT_TOOL_NAMES = {"create_draft", "edit_draft", "validate_draft", "preview_draft", "submit_draft", "abandon_draft"}
# Fields an edit_draft patch may set on a card. Patches that omit a field leave
# that card's existing value intact (per-card partial overlay).
_DRAFT_CARD_PATCH_FIELDS = ("angle", "body", "source_message_ids", "media_ids")
LEGACY_POST_MODES = {"disabled", "draft_adapter", "direct"}
TOPIC_EDITOR_DRAFT_TEMPLATES = {
    "creation_release",
    "technical_finding",
    "tool_workflow_update",
    "community_debate",
    "generation_showcase",
}


TOPIC_EDITOR_SYSTEM_PROMPT = """You are the BNDC live-update topic editor.

Your job is not to summarize the whole window. Curate a small public update:
short, source-backed, visual when possible, and split into a few focused cards.
Leave material out when it is weaker than the best two or three angles.

Use the workflow in order:

1. Research: inspect the supplied source messages, `evidence_shelf`, active
   topics, and cached media understandings. Use search_topics, search_messages,
   get_author_profile, get_message_context, get_reply_chain, understand_image,
   and understand_video when you need more context.
2. Decide: choose publish, watch, update sources, discard, or observation. If
   there is no publishable public update, do not draft one.
3. Draft: call create_draft with a template, headline, dek, cards, and
   editor_note. Select source_message_ids and media_ids from the hydrated
   evidence shelf. Do not use raw CDN URLs.
4. Validate: call validate_draft.
5. Revise: when validation returns errors or meaningful warnings, call
   edit_draft on the same draft. Do not start a replacement draft for normal
   revision. The `cards` patch is a positional overlay: resend only the card(s)
   and field(s) you are changing; everything you omit stays intact. Remove a
   card with remove_card_indices, add one with append_cards.
6. Preview: call preview_draft and inspect the exact text/media units.
7. Submit: call submit_draft only after a valid preview. Submit refuses stale
   or invalid previews.

Card style:

- Each card answers one question: what changed, what someone made, what the
  community learned, why this is useful now, or what is worth watching next.
- Keep card bodies concise. Prefer one tight paragraph over an essay.
- Use inline `[N]` citations next to factual claims. Each marker must map to
  that card's source_message_ids. Restart numbering at `[1]` for EVERY card;
  `[N]` maps to the Nth entry of that card's source_message_ids. Never append
  a detached "Sources:" footer.
- Attach media_ids to the card that discusses that image or video so media
  appears immediately after the relevant text. Media ids have the shape
  message_id:kind:index (kinds attachment|embed|external); derive them from the
  evidence shelf, never raw CDN URLs.
- Avoid digest prose, broad roundups, padded context, and weak "community
  reacted" points without concrete substance.
- Too many angles means split topics, watch the extras, or leave them out.

Templates:

- creation_release: card 1 says what was released or made; card 2 says what
  the generation/media shows; card 3 says why the community cares or what
  happens next.
- technical_finding: card 1 states the problem or discovery; card 2 gives
  evidence or comparison; card 3 gives a workaround, implication, or next step.
- tool_workflow_update: card 1 says what changed; card 2 says how someone
  tested it; card 3 gives caveats, proof, or next step.
- community_debate: card 1 states the concrete question; card 2 gives the
  strongest position A; card 3 gives the strongest position B; optional card 4
  says what remains unresolved.
- generation_showcase: a themed showcase of recent generations. Each card
  spotlights one generation: creator/tool, a short admiring caption, and why it
  works. Attach that generation's media to its card. One card is fine when a
  single generation is the strongest thing in the window; otherwise use 1 to a
  handful of cards. Group generations by a theme when possible (vibe, style,
  tool, technique) so the post reads as one curated collection.

Media-bearing messages may include cached `media_understandings`; read those
before spending vision budget. Skip workflow_graph media. For uncached media
that affects editorial judgment, use understand_image or understand_video.

Auto-shortlisted media appears in `auto_shortlisted_media` and as active
watching topics. Explicitly decide publish, watch, update, or discard for those
items after checking context.

Sometimes the whole post should be a "generations to admire" showcase rather
than news. When auto_shortlisted_media or other strong generation posts clear
the bar, draft a generation_showcase: 1 to a handful of cards, one generation
per card, each with a short, specific, admiring caption plus that generation's
media attached. Group generations by a theme when possible (vibe, style, tool,
technique) so the collection has a point of view. Read cached
media_understandings before spending vision budget, then use understand_image or
understand_video for uncached media that matters. A showcase may be the entire
post when nothing else this window is stronger; otherwise fold the strongest one
or two generations into a news post. Be creative and opinionated here — this is
the place for taste, not just reporting. Publish at most one generation_showcase
per run, and do not cite a generation in both a showcase and a news topic.

Legacy direct-post tools (`post_topic`, `post_simple_topic`,
`post_sectioned_topic`) are rollback-only or adapter-backed compatibility
surfaces. In normal mode they are hidden and refused. Use the draft tools for
publishing.

REQUIRED to end the run: call finalize_run exactly once with full editorial
reasoning describing what you saw, what you considered, what you skipped and
why, and what you acted on. The run does not close by plain text alone.

Every open draft must be resolved before finalize_run: submit it, abandon it
(with a fallback_action), or route it to needs_review. finalize_run is rejected
while a draft is still drafting/needs_revision/valid/blocked_for_submit unless
you set acknowledge_pending_drafts=true, which leaves them for cross-run
recovery.
"""


TOPIC_EDITOR_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_topics",
        "description": "Find existing topics by headline, canonical key, aliases, or state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "state_filter": {"type": "array", "items": {"type": "string"}},
                "hours_back": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_messages",
        "description": (
            "Search Discord messages. Filter parameters are AND-combined; any combination is valid (all are optional). "
            "Use `scope` to choose: 'window' (current source-message window, cheap, default) or 'archive' (full discord_messages, broader context). "
            "Use this when you need 'from:author has:video after:Xd' style queries to ground editorial framing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free-text content match (ILIKE)"},
                "from_author_id": {"type": "integer", "description": "equivalent to Discord 'from:' — restrict to messages this author wrote"},
                "in_channel_id": {"type": "integer", "description": "equivalent to Discord 'in:' — restrict to one channel"},
                "mentions_author_id": {"type": "integer", "description": "equivalent to Discord 'mentions:' — restrict to messages that mention this author"},
                "has": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["image", "video", "audio", "link", "embed", "file"]},
                    "description": "equivalent to Discord 'has:' — filter by attachment/embed kind. Array combines AND.",
                },
                "after": {"type": "string", "description": "lower-bound time. Accepts ISO timestamp OR relative like '24h', '7d', '30d'"},
                "before": {"type": "string", "description": "upper-bound time. Same format as after"},
                "is_reply": {"type": "boolean", "description": "if true, only messages that are replies (reply_to_message_id IS NOT NULL)"},
                "limit": {"type": "integer", "description": "default 20, max 50"},
                "scope": {"type": "string", "enum": ["window", "archive"], "description": "default 'window'"},
            },
            "required": [],
        },
    },
    {
        "name": "get_author_profile",
        "description": "Return author context from the current source window.",
        "input_schema": {
            "type": "object",
            "properties": {"author_id": {"type": "integer"}},
            "required": ["author_id"],
        },
    },
    {
        "name": "get_message_context",
        "description": "Fetch messages by id from the current source window.",
        "input_schema": {
            "type": "object",
            "properties": {"message_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["message_ids"],
        },
    },
    {
        "name": "get_reply_chain",
        "description": "Walk the reply chain backwards from a message. Returns ancestor messages root-first. Use when a generation or post is a reply and you want to understand what it's responding to.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "max_depth": {"type": "integer", "description": "default 5, max 15"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "create_draft",
        "description": "Create an editable topic-editor draft from researched evidence. Drafts must be validated and previewed before submit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_key": {"type": "string"},
                "template": {"type": "string", "enum": sorted(TOPIC_EDITOR_DRAFT_TEMPLATES) if "TOPIC_EDITOR_DRAFT_TEMPLATES" in globals() else ["creation_release", "technical_finding", "tool_workflow_update", "community_debate", "generation_showcase"]},
                "headline": {"type": "string"},
                "dek": {"type": "string"},
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "angle": {"type": "string"},
                            "body": {"type": "string"},
                            "source_message_ids": {"type": "array", "items": {"type": "string"}},
                            "media_ids": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "description": "media id in shape message_id:kind:index (e.g. '1506344740558475356:attachment:0'); kinds attachment|embed|external; derive from the evidence shelf, never raw CDN URLs.",
                                },
                            },
                        },
                        "required": ["angle", "body", "source_message_ids"],
                    },
                },
                "editor_note": {"type": "string"},
            },
            "required": ["topic_key", "template", "headline", "dek", "cards", "editor_note"],
        },
    },
    {
        "name": "edit_draft",
        "description": "Patch an existing draft after validation feedback. Edit the same draft rather than creating a replacement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "patch": {
                    "type": "object",
                    "description": "Partial patch over draft_json. `cards` is a positional overlay: the card at array position i merges onto the existing card at that position — only the fields you include change and cards you do not mention are preserved, so never resend the whole list to touch one card. Remove cards with `remove_card_indices` (original indices); add new full cards with `append_cards`. Top-level headline/dek/editor_note replace wholesale. Legacy single-field form cards[i].field (e.g. cards[2].body) also works. cards[].media_ids must be message_id:kind:index (kinds attachment|embed|external). Restart inline citation numbering at [1] for every card; [N] maps to the Nth entry of that card's source_message_ids.",
                },
                "reason": {"type": "string"},
            },
            "required": ["draft_id", "patch", "reason"],
        },
    },
    {
        "name": "validate_draft",
        "description": "Run deterministic draft validation and return blocking errors plus warnings.",
        "input_schema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "preview_draft",
        "description": "Render the exact ordered text/media units Discord would receive. Stores the current valid preview hash.",
        "input_schema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "submit_draft",
        "description": "Submit a previously validated and previewed draft. Refuses invalid or stale-preview drafts before topic upsert or publish.",
        "input_schema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "abandon_draft",
        "description": "End a draft attempt when the topic is not publishable after revision. Use fallback_action=needs_review to route an unfinishable draft to the durable human-review backlog instead of dropping it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "reason": {"type": "string"},
                "fallback_action": {"type": "string", "enum": ["watch_topic", "update_topic_sources", "discard_topic", "needs_review"]},
            },
            "required": ["draft_id", "reason"],
        },
    },
    {
        "name": "post_topic",
        "description": (
            "Publish a topic using the minimum number of editorial blocks. "
            "One intro block = a single-beat story, even if it has media. "
            "Add section blocks only when the topic has distinct contributors, "
            "angles, or sub-stories that each independently merit a header. "
            "Every factual block MUST include its own `source_message_ids`; "
            "media (images, video, embeds, external links) MUST be attached to "
            "the relevant block via `media_refs`, not to a global list. Media "
            "refs use a stable reference shape: "
            "`{\"message_id\": \"...\", \"kind\": \"attachment\"|\"embed\", \"index\": N}` "
            "(shorthand `{\"message_id\": \"...\", \"attachment_index\": N}` is also "
            "accepted). Do NOT include a global Sources footer — citations are "
            "rendered inline per block using plain integer markers like [1], [2] "
            "(e.g. \"Body text [1] with a claim [2].\"). Every [N] must match a "
            "real index in the block's source_message_ids. Restart numbering at "
            "[1] for every block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "blocks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["intro", "section"]},
                            "title": {"type": "string"},
                            "text": {"type": "string"},
                            "source_message_ids": {"type": "array", "items": {"type": "string"}},
                            "media_refs": {
                                "description": "Media references for this block. Use kind='attachment'/'embed' for Discord-hosted media (preferred, indexed first) and kind='external' for off-platform links (Reddit, X, etc.) that are resolved best-effort. External refs are always block-bound and secondary.",
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "message_id": {"type": "string"},
                                        "kind": {"type": "string", "enum": ["attachment", "embed", "external"]},
                                        "index": {"type": "integer"},
                                        "attachment_index": {"type": "integer"},
                                    },
                                    "required": ["message_id"],
                                },
                            },
                        },
                        "required": ["type", "text"],
                    },
                },
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "parent_topic_id": {"type": "string"},
                "notes": {"type": "string"},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "source_message_ids", "blocks"],
        },
    },
    {
        "name": "post_simple_topic",
        "description": (
            "[DEPRECATED — use post_topic] Publish a legacy text-only "
            "single-author, one or two source-message topic. Do not use this "
            "for images, videos, embeds, or media refs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "body": {"type": "string"},
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "media": {"type": "array", "items": {"type": "string"}},
                "parent_topic_id": {"type": "string"},
                "notes": {"type": "string"},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "body", "source_message_ids"],
        },
    },
    {
        "name": "post_sectioned_topic",
        "description": (
            "[DEPRECATED — use post_topic] Publish a legacy multi-source or "
            "multi-contributor topic. Prefer `post_topic`; this alias is "
            "accepted for backwards compatibility. Use the `blocks` array for "
            "structured document topics; `sections` is still accepted for "
            "backwards compatibility. Every factual block MUST include its own "
            "`source_message_ids`; media (images, video, embeds) MUST be attached "
            "to the relevant block via `media_refs`, not to a global list. Media "
            "refs use a stable reference shape: "
            "`{\"message_id\": \"...\", \"kind\": \"attachment\"|\"embed\", \"index\": N}` "
            "(shorthand `{\"message_id\": \"...\", \"attachment_index\": N}` is also "
            "accepted). Do NOT include a global Sources footer — citations are "
            "rendered inline per block using plain integer markers like [1], [2] "
            "(e.g. \"Body text [1] with a claim [2].\"). Every [N] must match a "
            "real index in the block's source_message_ids. Restart numbering at "
            "[1] for every block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "body": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "object"}},
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["intro", "section"]},
                            "title": {"type": "string"},
                            "text": {"type": "string"},
                            "source_message_ids": {"type": "array", "items": {"type": "string"}},
                            "media_refs": {
                                "description": "Media references for this block. Use kind='attachment'/'embed' for Discord-hosted media (preferred, indexed first) and kind='external' for off-platform links (Reddit, X, etc.) that are resolved best-effort. External refs are always block-bound and secondary.",
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "message_id": {"type": "string"},
                                        "kind": {"type": "string", "enum": ["attachment", "embed", "external"]},
                                        "index": {"type": "integer"},
                                        "attachment_index": {"type": "integer"},
                                    },
                                    "required": ["message_id"],
                                },
                            },
                        },
                        "required": ["type", "text"],
                    },
                },
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "parent_topic_id": {"type": "string"},
                "notes": {"type": "string"},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "body", "source_message_ids"],
        },
    },
    {
        "name": "watch_topic",
        "description": "Track a promising topic that is not ready to publish.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_key": {"type": "string"},
                "headline": {"type": "string"},
                "why_interesting": {"type": "string"},
                "revisit_when": {"type": "string"},
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "override_collisions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposed_key", "headline", "why_interesting", "revisit_when", "source_message_ids"],
        },
    },
    {
        "name": "update_topic_source_messages",
        "description": "Append source messages to an existing topic without publishing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {"type": "string"},
                "new_source_message_ids": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": ["topic_id", "new_source_message_ids"],
        },
    },
    {
        "name": "discard_topic",
        "description": "Discard a watching topic that is no longer useful.",
        "input_schema": {
            "type": "object",
            "properties": {"topic_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["topic_id", "reason"],
        },
    },
    {
        "name": "record_observation",
        "description": "Record a near-miss or considered-but-not-posted item. Use sparingly; storage is capped at 3 observations per run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_message_ids": {"type": "array", "items": {"type": "string"}},
                "observation_kind": {"type": "string", "enum": ["near_miss", "considered"]},
                "reason": {"type": "string"},
            },
            "required": ["source_message_ids", "observation_kind", "reason"],
        },
    },
    {
        "name": "understand_image",
        "description": (
            "Analyze an image attachment from a source message. Returns structured "
            "JSON with kind, subject, technical_signal, aesthetic_quality (0-10), "
            "and discriminator_notes. Cached results are reused across runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer"},
                "attachment_index": {"type": "integer", "default": 0},
                "mode": {"type": "string", "enum": ["fast", "best"], "default": "fast"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "understand_video",
        "description": (
            "Analyze a video attachment from a source message. Returns structured "
            "JSON with summary, visual_read, audio_read, edit_value, highlight_score, "
            "energy, pacing, production_quality, boundary_notes, cautions, and kind. "
            "Cached results are reused across runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer"},
                "attachment_index": {"type": "integer", "default": 0},
                "mode": {"type": "string", "enum": ["fast", "best"], "default": "fast"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "finalize_run",
        "description": (
            "MUST be called exactly once to end the run. Provide your overall editorial reasoning "
            "describing what you saw in the source window, what you considered, what you skipped "
            "and why, and what (if anything) you acted on. Minimum 100 characters. If any draft "
            "is still open (drafting / needs_revision / valid / blocked_for_submit), finalize is "
            "rejected unless you set acknowledge_pending_drafts=true — resolve open drafts first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "overall_reasoning": {
                    "type": "string",
                    "description": "Editorial summary, ≥100 characters. Full sentences.",
                },
                "topics_considered": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short bullets naming clusters you looked at (acted on or skipped).",
                },
                "acknowledge_pending_drafts": {
                    "type": "boolean",
                    "description": "Set true to finalize even with open drafts; they stay durably persisted and are recovered by the next run. Only use when a draft is genuinely unfinishable this run.",
                },
            },
            "required": ["overall_reasoning"],
        },
    },
]


class TopicEditor:
    """Topic-centered live-update editor runtime.

    The runtime only talks to the injected db handler. Storage-level details stay
    behind db_handler wrappers so tests and rollback wiring can swap the backend.
    """

    def __init__(
        self,
        bot: Any = None,
        *,
        db_handler: Any = None,
        llm_client: Any = None,
        guild_id: Optional[int] = None,
        live_channel_id: Optional[int] = None,
        environment: Optional[str] = None,
        model: Optional[str] = None,
        source_limit: Optional[int] = None,
        actor_brief: Optional[str] = None,
    ) -> None:
        self.bot = bot
        self.db = db_handler or getattr(bot, "db", None) or getattr(bot, "db_handler", None)
        self.llm_client = llm_client or getattr(bot, "claude_client", None)
        self.guild_id = guild_id
        self.live_channel_id = live_channel_id
        self.environment = environment or ("dev" if getattr(bot, "dev_mode", False) else os.getenv("LIVE_UPDATE_ENVIRONMENT", "prod"))
        self.model = model or os.getenv("TOPIC_EDITOR_MODEL") or DEFAULT_LIVE_UPDATE_MODEL
        # Defaults reflect production usage; override via env if needed.
        self.source_limit = int(source_limit or os.getenv("TOPIC_EDITOR_SOURCE_LIMIT", "1000"))
        self.actor_brief = actor_brief
        self.publishing_enabled = os.getenv("TOPIC_EDITOR_PUBLISHING_ENABLED", "true").lower() == "true"
        # Hardcoded trace-channel default so a missing env var doesn't silently
        # disable editorial trace embeds in production.
        self.trace_channel_id = os.getenv("LIVE_UPDATE_TRACE_CHANNEL_ID", "1316024582041243668")
        self.media_shortlist_min_reactions = self._env_int("TOPIC_EDITOR_MEDIA_SHORTLIST_MIN_REACTIONS", 5)
        self.media_shortlist_limit = self._env_int("TOPIC_EDITOR_MEDIA_SHORTLIST_LIMIT", 5)
        # Reaction qualification is re-scanned over this window each run so a
        # generation that crosses the threshold AFTER its first hour still
        # becomes a candidate (the checkpoint scan alone misses late bloomers).
        self.media_shortlist_lookback_hours = self._env_int(
            "TOPIC_EDITOR_MEDIA_SHORTLIST_LOOKBACK_HOURS", 24
        )
        # Rollout guard for deprecated direct-post tools:
        # - disabled: default draft-required behavior; legacy tools are hidden and refused.
        # - draft_adapter: compatibility path that converts legacy input into a draft and submits it.
        # - direct: rollback-only path that preserves the old direct upsert/publish behavior.
        self.legacy_post_mode = self._normalize_legacy_post_mode(
            os.getenv("TOPIC_EDITOR_LEGACY_POST_MODE", "disabled")
        )
        self.draft_limits = topic_editor_draft_limits_from_config({
            "headline_target_chars": os.getenv("TOPIC_EDITOR_DRAFT_HEADLINE_TARGET_CHARS"),
            "dek_target_chars": os.getenv("TOPIC_EDITOR_DRAFT_DEK_TARGET_CHARS"),
            "card_body_max_chars": os.getenv("TOPIC_EDITOR_DRAFT_CARD_BODY_MAX_CHARS"),
            "max_cards": os.getenv("TOPIC_EDITOR_DRAFT_MAX_CARDS"),
            "max_revision_attempts": os.getenv("TOPIC_EDITOR_DRAFT_MAX_REVISION_ATTEMPTS"),
            "sources_per_card_warning": os.getenv("TOPIC_EDITOR_DRAFT_SOURCES_PER_CARD_WARNING"),
            "discord_content_limit": os.getenv("TOPIC_EDITOR_DISCORD_CONTENT_LIMIT"),
        })
        self.topic_editor_drafts: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _normalize_legacy_post_mode(value: Any) -> str:
        mode = str(value or "disabled").strip().lower()
        return mode if mode in LEGACY_POST_MODES else "disabled"

    def _agent_tools(self) -> List[Dict[str, Any]]:
        if self.legacy_post_mode != "disabled":
            return TOPIC_EDITOR_TOOLS
        return [
            tool for tool in TOPIC_EDITOR_TOOLS
            if tool.get("name") not in LEGACY_POST_TOOL_NAMES
        ]

    async def run_once(self, trigger: str = "scheduled") -> Dict[str, Any]:
        if not self.db:
            raise RuntimeError("TopicEditor requires an injected db_handler")
        if not self.llm_client:
            raise RuntimeError("TopicEditor requires an Anthropic/Claude client")

        started = time.monotonic()
        guild_id = self._resolve_guild_id()
        live_channel_id = self._resolve_live_channel_id(guild_id)
        checkpoint_key = self._checkpoint_key(guild_id, live_channel_id)
        cold_start_seeded = False
        checkpoint = self.db.get_topic_editor_checkpoint(checkpoint_key, environment=self.environment)
        if checkpoint is None:
            checkpoint = self.db.mirror_live_checkpoint_to_topic_editor(checkpoint_key, environment=self.environment)
        if checkpoint is None:
            checkpoint = self._seed_cold_start_checkpoint(checkpoint_key, guild_id, live_channel_id)
            cold_start_seeded = True

        run = self.db.acquire_topic_editor_run({
            "guild_id": guild_id,
            "live_channel_id": live_channel_id,
            "trigger": trigger,
            "checkpoint_before": checkpoint,
            "model": self.model,
            "publishing_enabled": self.publishing_enabled,
            "trace_channel_id": self.trace_channel_id,
        }, environment=self.environment)
        if not run:
            return {"status": "skipped", "reason": "lease_not_acquired", "checkpoint_key": checkpoint_key}
        run_id = str(run.get("run_id"))
        logger.info(
            "TopicEditor run acquired: run_id=%s env=%s guild=%s live_channel=%s checkpoint=%s publishing=%s model=%s",
            run_id,
            self.environment,
            guild_id,
            live_channel_id,
            {
                "last_message_id": (checkpoint or {}).get("last_message_id"),
                "last_message_created_at": (checkpoint or {}).get("last_message_created_at"),
            },
            self.publishing_enabled,
            self.model,
        )

        metadata: Dict[str, Any] = {
            "tool_calls": [],
            "publishing_enabled": self.publishing_enabled,
            "trigger": trigger,
        }
        try:
            # Cold-start no longer short-circuits to zero — the seeded checkpoint
            # is anchored to (lookback_minutes ago), so the same query returns
            # the last interval's worth of messages.
            logger.info(
                "TopicEditor fetching source messages: run_id=%s source_limit=%s",
                run_id,
                self.source_limit,
            )
            messages = self.db.get_archived_messages_after_checkpoint(
                checkpoint=checkpoint,
                guild_id=guild_id,
                channel_ids=None,
                limit=self.source_limit,
                exclude_author_ids=self._excluded_author_ids(),
            )
            logger.info(
                "TopicEditor fetched source messages: run_id=%s count=%s",
                run_id,
                len(messages or []),
            )
            # Re-scan recent messages for late-blooming candidates: a generation
            # that crossed the reaction threshold after the checkpoint never made
            # it into `messages`. Union the lookback window into the shortlist
            # scan only — the editorial source window stays checkpoint-scoped.
            shortlist_scan = self._shortlist_scan_messages(messages, guild_id)
            logger.info("TopicEditor fetching known topics: run_id=%s", run_id)
            known_topics = self.db.get_topics(
                guild_id=guild_id,
                states=["posted", "watching", "discarded"],
                limit=300,
                environment=self.environment,
            )
            logger.info(
                "TopicEditor fetched known topics: run_id=%s count=%s",
                run_id,
                len(known_topics or []),
            )
            auto_shortlisted_media = self._auto_shortlist_media_messages(
                shortlist_scan,
                known_topics,
                run_id=run_id,
                guild_id=guild_id,
            )
            active_topics = [
                topic for topic in known_topics
                if topic.get("state") in {"posted", "watching"}
            ]
            active_keys = {str(topic.get("canonical_key") or "") for topic in active_topics}
            for entry in auto_shortlisted_media:
                topic = entry.get("topic")
                key = str((topic or {}).get("canonical_key") or "")
                if topic and key not in active_keys:
                    active_topics.append(topic)
                    active_keys.add(key)
            logger.info(
                "TopicEditor active topic set ready: run_id=%s active=%s auto_shortlisted=%s",
                run_id,
                len(active_topics),
                len(auto_shortlisted_media),
            )
            logger.info("TopicEditor fetching aliases: run_id=%s", run_id)
            aliases = self.db.get_topic_aliases(guild_id=guild_id, environment=self.environment)
            logger.info(
                "TopicEditor fetched aliases: run_id=%s count=%s",
                run_id,
                len(aliases or []),
            )
            metadata["source_message_timestamps"] = [m.get("created_at") for m in messages if m.get("created_at")]
            metadata["source_channel_counts"] = self._tally_channels(messages)
            metadata["active_topics_count"] = len(active_topics)
            metadata["auto_shortlisted_media"] = [
                {
                    "topic_id": entry.get("topic_id"),
                    "message_id": entry.get("message_id"),
                    "reaction_count": entry.get("reaction_count"),
                    "media_ref": entry.get("media_ref"),
                    "headline": entry.get("headline"),
                    "status": entry.get("status"),
                }
                for entry in auto_shortlisted_media
            ]
            self._persist_run_progress(
                run_id,
                checkpoint_before=checkpoint,
                checkpoint_after=checkpoint,
                messages=messages,
                tool_calls=[],
                outcomes=[],
                started=started,
                metadata=metadata,
            )
            # Cross-run recovery: re-expose drafts stranded by a prior run's forced
            # close / lease steal / clean-finalize-with-open-drafts BEFORE the
            # no-messages fast path, so quiet windows still drain the backlog.
            recovered_drafts = self._recover_stale_drafts(run_id=run_id, guild_id=guild_id)

            # Publish backlog: retry topics with pending/failed/partial outbox units
            # (reconcile-before-resend) so quiet windows drain the publish queue too.
            # Bounded by a timeout so a stuck publish (e.g. a hung media download or
            # Discord send) can't brick the run before the agent loop — the outbox
            # reconciliation only resends pending/failed units, so a mid-publish
            # timeout is safe.
            if hasattr(self.db, "get_pending_topic_publish_outbox_topics"):
                try:
                    backlog_timeout = self._env_float(
                        "TOPIC_EDITOR_PUBLISH_BACKLOG_TIMEOUT_SECONDS", 120.0
                    )
                    backlog_results = await asyncio.wait_for(
                        self._publish_pending_topics(), timeout=backlog_timeout
                    )
                    metadata["publish_backlog_results"] = backlog_results
                except asyncio.TimeoutError:
                    logger.error(
                        "TopicEditor publish-backlog drain timed out after %ss: run_id=%s",
                        backlog_timeout, run_id,
                    )
                except Exception as exc:
                    logger.error("TopicEditor publish-backlog drain failed: run_id=%s error=%s", run_id, exc)

            # Re-scan-only candidates (late bloomers surfaced from the lookback
            # window) are new shortlist watchers — do not fast-path past them.
            if not messages and not recovered_drafts and not auto_shortlisted_media:
                updates = self._run_updates(
                    checkpoint_before=checkpoint,
                    checkpoint_after=checkpoint,
                    messages=[],
                    tool_calls=[],
                    started=started,
                    metadata=metadata,
                    skipped_reason="no_new_archived_messages",
                )
                try:
                    if not self.db.complete_topic_editor_run(run_id, updates, guild_id=guild_id, environment=self.environment):
                        logger.error("TopicEditor complete_topic_editor_run returned None: run_id=%s", run_id)
                except Exception as exc:
                    logger.error("TopicEditor complete_topic_editor_run raised: run_id=%s error=%s", run_id, exc)
                trace_messages = self._format_trace_messages(run_id, updates, [], [])
                await self._emit_trace(trace_messages, run_id=run_id, updates=updates, outcomes=[], publish_results=[])
                return {
                    "status": "completed",
                    "run_id": run_id,
                    "skipped_reason": "no_new_archived_messages",
                    "trace_messages": trace_messages,
                }

            # --- Agent loop with 100-turn budget ---
            max_turns = int(os.getenv("TOPIC_EDITOR_MAX_TURNS", "100"))
            initial_payload = self._build_initial_user_payload(
                messages,
                active_topics,
                auto_shortlisted_media=auto_shortlisted_media,
                recovered_drafts=recovered_drafts,
            )
            messages_arg: List[Dict[str, Any]] = [
                {"role": "user", "content": [{"type": "text", "text": repr(initial_payload)}]}
            ]
            dispatcher_context = {
                "run_id": run_id,
                "guild_id": guild_id,
                "live_channel_id": live_channel_id,
                "messages": messages,
                "active_topics": active_topics,
                "aliases": aliases,
                "seen_tool_call_ids": set(),
                "idempotent_results": {},
                "observation_count": 0,
                "created_topics": [],
                "finalize": None,
                "finalize_nudge_sent": False,
                "vision_budget_usd": self._env_float("TOPIC_EDITOR_VISION_BUDGET_PER_RUN", 1.0),
                "vision_cost_usd": 0.0,
            }
            tool_calls: List[Dict[str, Any]] = []
            outcomes: List[Dict[str, Any]] = []
            total_input_tokens = 0
            total_output_tokens = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0
            cumulative_tokens = 0
            cumulative_cost_usd = 0.0
            has_cost_estimate = False
            cumulative_cache_adjusted_cost_usd = 0.0
            has_cache_adjusted_estimate = False
            compaction_count = 0
            last_compaction_cumulative = 0
            last_context_size = 0
            text_chunks: List[str] = []
            forced_close = False
            forced_close_reason: Optional[str] = None
            turn_count = 0
            max_cost_usd = self._env_float("TOPIC_EDITOR_MAX_COST_USD", 2.0)
            # Token cap is disabled by default: the per-run cost cap above is the
            # real backstop. 0 disables the token check (see guards at :862/:1028).
            max_tokens = self._env_int("TOPIC_EDITOR_MAX_TOKENS", 0)
            compact_token_threshold = self._env_int("TOPIC_EDITOR_COMPACT_TOKEN_THRESHOLD", 900_000)
            max_compactions = self._env_int("TOPIC_EDITOR_MAX_COMPACTIONS", 2)
            for turn_count in range(1, max_turns + 1):
                logger.info(
                    "TopicEditor invoking LLM: run_id=%s turn=%s messages=%s",
                    run_id,
                    turn_count,
                    len(messages_arg),
                )
                response = await self._invoke_anthropic(messages_arg)
                turn_tool_calls = self._extract_tool_calls(response)
                logger.info(
                    "TopicEditor LLM turn complete: run_id=%s turn=%s tool_calls=%s tools=%s",
                    run_id,
                    turn_count,
                    len(turn_tool_calls),
                    [call.get("name") for call in turn_tool_calls],
                )
                turn_reasoning = self._extract_reasoning_text(response)
                if turn_reasoning:
                    text_chunks.append(turn_reasoning)
                usage = self._extract_usage(response)
                last_context_size = int(usage.get("input_tokens", 0) or 0)
                total_input_tokens += int(usage.get("input_tokens", 0) or 0)
                total_output_tokens += int(usage.get("output_tokens", 0) or 0)
                total_cache_hit_tokens += int(usage.get("cache_hit_tokens", 0) or 0)
                total_cache_miss_tokens += int(usage.get("cache_miss_tokens", 0) or 0)
                cumulative_tokens = total_input_tokens + total_output_tokens
                turn_cost = self._estimate_cost_usd(usage)
                if turn_cost is not None:
                    has_cost_estimate = True
                    cumulative_cost_usd = cumulative_cost_usd + float(turn_cost)
                cache_adjusted_turn_cost = self._estimate_cache_adjusted_cost_usd(usage)
                if cache_adjusted_turn_cost is not None:
                    has_cache_adjusted_estimate = True
                    cumulative_cache_adjusted_cost_usd = cumulative_cache_adjusted_cost_usd + float(cache_adjusted_turn_cost)

                cap_reason = None
                if max_cost_usd > 0 and has_cost_estimate and cumulative_cost_usd > max_cost_usd:
                    cap_reason = "cost_cap_exceeded"
                elif max_tokens > 0 and cumulative_tokens > max_tokens:
                    cap_reason = "token_cap_exceeded"
                if cap_reason:
                    # The response has already been paid for, so execute ALL of the
                    # tool calls it returned (not just finalize_run) — otherwise a
                    # budget-edge turn silently discards already-completed work.
                    # Dispatch goes through _dispatch_tool_call to preserve the
                    # idempotency invariants (see _idempotent_replay_outcome).
                    tool_calls.extend(turn_tool_calls)
                    self._populate_idempotent_results(turn_tool_calls, dispatcher_context)
                    for call in turn_tool_calls:
                        outcomes.append(self._dispatch_tool_call(call, dispatcher_context))
                    if dispatcher_context.get("finalize"):
                        metadata["budget_cap_exceeded_after_finalize"] = cap_reason
                        break
                    forced_close = True
                    forced_close_reason = cap_reason
                    break

                # Threshold-based compaction: once the run's cumulative spend crosses
                # the boundary, fold earlier context into a compact "decisions so far"
                # recap while keeping the most recent full turn verbatim. The hard
                # token cap above remains the backstop.
                if (
                    compaction_count < max_compactions
                    and compact_token_threshold > 0
                    and cumulative_tokens >= last_compaction_cumulative + compact_token_threshold
                ):
                    messages_arg = self._compact_conversation(
                        messages_arg,
                        initial_payload,
                        dispatcher_context,
                        outcomes,
                        messages,
                        turn_count,
                    )
                    compaction_count += 1
                    last_compaction_cumulative = cumulative_tokens
                    logger.info(
                        "TopicEditor compacted conversation: run_id=%s turn=%s cumulative_tokens=%s",
                        run_id,
                        turn_count,
                        cumulative_tokens,
                    )
                    metadata.setdefault("compactions", []).append({
                        "turn": turn_count,
                        "cumulative_tokens": cumulative_tokens,
                        "context_size_before": last_context_size,
                    })

                if not turn_tool_calls:
                    # Agent ended with text only — push back demanding finalize_run.
                    assistant_content = self._assistant_content_from_response(response)
                    if assistant_content:
                        messages_arg.append({"role": "assistant", "content": assistant_content})
                    messages_arg.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "You ended without calling `finalize_run`. The run cannot "
                                        "close until you call it with your overall editorial "
                                        "reasoning (≥100 chars). Call it now."
                                    ),
                                }
                            ],
                        }
                    )
                    if turn_count >= max_turns:
                        forced_close = True
                        forced_close_reason = "max_turns_reached_without_finalize"
                        break
                    metadata["tool_calls"] = [
                        {"id": call["id"], "name": call["name"], "input": call["input"]}
                        for call in tool_calls
                    ]
                    metadata["usage"] = {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "cache_hit_tokens": total_cache_hit_tokens,
                        "cache_miss_tokens": total_cache_miss_tokens,
                    }
                    metadata["cumulative_cost_usd"] = round(cumulative_cost_usd, 6) if has_cost_estimate else None
                    metadata["estimated_cache_adjusted_cost_usd"] = (
                        round(cumulative_cache_adjusted_cost_usd, 6) if has_cache_adjusted_estimate else None
                    )
                    metadata["cumulative_tokens"] = cumulative_tokens
                    metadata["max_cost_usd"] = max_cost_usd
                    metadata["max_tokens"] = max_tokens
                    metadata["turn_count"] = turn_count
                    metadata["reasoning"] = "\n\n".join(text_chunks).strip()
                    self._persist_run_progress(
                        run_id,
                        checkpoint_before=checkpoint,
                        checkpoint_after=checkpoint,
                        messages=messages,
                        tool_calls=tool_calls,
                        outcomes=outcomes,
                        started=started,
                        metadata={**metadata, "outcomes": outcomes},
                    )
                    continue

                # Dispatch each tool call this turn, building tool_result blocks.
                tool_calls.extend(turn_tool_calls)
                self._populate_idempotent_results(turn_tool_calls, dispatcher_context)
                turn_results: List[Dict[str, Any]] = []
                for call in turn_tool_calls:
                    outcome = self._dispatch_tool_call(call, dispatcher_context)
                    outcomes.append(outcome)
                    turn_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.get("id"),
                            "content": self._tool_result_content(call, outcome),
                        }
                    )
                # Build assistant turn + user tool_result turn for the next iteration.
                assistant_content = self._assistant_content_from_response(response)
                if assistant_content:
                    messages_arg.append({"role": "assistant", "content": assistant_content})
                messages_arg.append({"role": "user", "content": turn_results})
                metadata["tool_calls"] = [
                    {"id": call["id"], "name": call["name"], "input": call["input"]}
                    for call in tool_calls
                ]
                metadata["usage"] = {
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "cache_hit_tokens": total_cache_hit_tokens,
                    "cache_miss_tokens": total_cache_miss_tokens,
                }
                metadata["cumulative_cost_usd"] = round(cumulative_cost_usd, 6) if has_cost_estimate else None
                metadata["estimated_cache_adjusted_cost_usd"] = (
                    round(cumulative_cache_adjusted_cost_usd, 6) if has_cache_adjusted_estimate else None
                )
                metadata["cumulative_tokens"] = cumulative_tokens
                metadata["max_cost_usd"] = max_cost_usd
                metadata["max_tokens"] = max_tokens
                metadata["turn_count"] = turn_count
                metadata["reasoning"] = "\n\n".join(text_chunks).strip()
                self._persist_run_progress(
                    run_id,
                    checkpoint_before=checkpoint,
                    checkpoint_after=checkpoint,
                    messages=messages,
                    tool_calls=tool_calls,
                    outcomes=outcomes,
                    started=started,
                    metadata={**metadata, "outcomes": outcomes},
                    accepted_count=sum(1 for outcome in outcomes if outcome.get("outcome") == "accepted"),
                    rejected_count=sum(1 for outcome in outcomes if str(outcome.get("outcome", "")).startswith("rejected")),
                    override_count=sum(int(outcome.get("override_count", 0)) for outcome in outcomes),
                    observation_count=sum(1 for outcome in outcomes if outcome.get("action") == "observation"),
                )

                if dispatcher_context.get("finalize"):
                    break

                # Graceful-finalize nudge: once we are within TOPIC_EDITOR_COST_NUDGE_RATIO
                # of the per-run cost cap, ask the model to close out cleanly instead of
                # burning the rest of the budget on new investigations. (The token cap is
                # disabled by default, so this is the effective pre-close signal.)
                cost_nudge_ratio = self._env_float("TOPIC_EDITOR_COST_NUDGE_RATIO", 0.85)
                if (
                    max_cost_usd > 0
                    and has_cache_adjusted_estimate
                    and cumulative_cache_adjusted_cost_usd >= max_cost_usd * cost_nudge_ratio
                    and not dispatcher_context.get("finalize_nudge_sent")
                ):
                    dispatcher_context["finalize_nudge_sent"] = True
                    messages_arg.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "You are near the token budget. If your work is sufficiently "
                                        "complete, call finalize_run now with your overall reasoning; "
                                        "do not start new investigations."
                                    ),
                                }
                            ],
                        }
                    )

                if turn_count >= max_turns:
                    forced_close = True
                    forced_close_reason = "max_turns_reached_without_finalize"
                    break

            if forced_close and not dispatcher_context.get("finalize"):
                # Loud audit row when we hit the budget without a clean finalize.
                reason = forced_close_reason or "max_turns_reached_without_finalize"
                self._store_transition({
                    "run_id": run_id,
                    "guild_id": guild_id,
                    "action": "rejected_finalize_run",
                    "reason": reason,
                    "payload": shape_transition_payload(
                        outcome="tool_error",
                        tool_name="finalize_run",
                        error=self._forced_close_error(reason, max_turns, cumulative_cost_usd, cumulative_tokens),
                        extra={
                            "cumulative_cost_usd": round(cumulative_cost_usd, 6) if has_cost_estimate else None,
                            "cumulative_tokens": cumulative_tokens,
                            "max_cost_usd": max_cost_usd,
                            "max_tokens": max_tokens,
                        },
                    ),
                    "model": self.model,
                })

            metadata["tool_calls"] = [
                {"id": call["id"], "name": call["name"], "input": call["input"]}
                for call in tool_calls
            ]
            metadata["usage"] = {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cache_hit_tokens": total_cache_hit_tokens,
                "cache_miss_tokens": total_cache_miss_tokens,
            }
            metadata["cumulative_cost_usd"] = round(cumulative_cost_usd, 6) if has_cost_estimate else None
            metadata["estimated_cache_adjusted_cost_usd"] = (
                round(cumulative_cache_adjusted_cost_usd, 6) if has_cache_adjusted_estimate else None
            )
            metadata["cumulative_tokens"] = cumulative_tokens
            metadata["max_cost_usd"] = max_cost_usd
            metadata["max_tokens"] = max_tokens
            metadata["turn_count"] = turn_count
            metadata["forced_close"] = forced_close
            metadata["forced_close_reason"] = forced_close_reason
            finalize = dispatcher_context.get("finalize") or {}
            metadata["reasoning"] = finalize.get("overall_reasoning") or "\n\n".join(text_chunks).strip()
            metadata["topics_considered"] = finalize.get("topics_considered") or []
            # Capture surface info for the trace embed.
            metadata.setdefault("source_message_timestamps", [m.get("created_at") for m in messages if m.get("created_at")])
            metadata.setdefault("source_channel_counts", self._tally_channels(messages))
            metadata.setdefault("active_topics_count", len(active_topics))
            metadata.setdefault("auto_shortlisted_media", [
                {
                    "topic_id": entry.get("topic_id"),
                    "message_id": entry.get("message_id"),
                    "reaction_count": entry.get("reaction_count"),
                    "media_ref": entry.get("media_ref"),
                    "headline": entry.get("headline"),
                    "status": entry.get("status"),
                }
                for entry in auto_shortlisted_media
            ])

            publish_results = await self._publish_created_topics(
                self._dedupe_created_topics(dispatcher_context.get("created_topics") or [])
            )
            metadata["publish_results"] = publish_results
            checkpoint_after = self._write_end_of_run_checkpoint(checkpoint, messages, run_id, forced_close)
            updates = self._run_updates(
                checkpoint_before=checkpoint,
                checkpoint_after=checkpoint_after,
                messages=messages,
                tool_calls=tool_calls,
                started=started,
                metadata={**metadata, "outcomes": outcomes},
                accepted_count=sum(1 for outcome in outcomes if outcome.get("outcome") == "accepted"),
                rejected_count=sum(1 for outcome in outcomes if str(outcome.get("outcome", "")).startswith("rejected")),
                override_count=sum(int(outcome.get("override_count", 0)) for outcome in outcomes),
                observation_count=sum(1 for outcome in outcomes if outcome.get("action") == "observation"),
                published_count=sum(1 for result in publish_results if result.get("status") == "sent"),
                failed_publish_count=sum(1 for result in publish_results if result.get("status") in {"failed", "partial"}),
                status="failed" if forced_close else "completed",
            )
            try:
                if not self.db.complete_topic_editor_run(run_id, updates, guild_id=guild_id, environment=self.environment):
                    logger.error("TopicEditor complete_topic_editor_run returned None: run_id=%s", run_id)
            except Exception as exc:
                logger.error("TopicEditor complete_topic_editor_run raised: run_id=%s error=%s", run_id, exc)
            trace_messages = self._format_trace_messages(run_id, updates, outcomes, publish_results)
            await self._emit_trace(trace_messages, run_id=run_id, updates=updates, outcomes=outcomes, publish_results=publish_results)
            status = updates.get("status") or "completed"
            return {
                "status": status,
                "run_id": run_id,
                "tool_calls": len(tool_calls),
                "outcomes": outcomes,
                "publish_results": publish_results,
                "trace_messages": trace_messages,
            }
        except Exception as exc:
            updates = self._run_updates(
                checkpoint_before=checkpoint,
                checkpoint_after=checkpoint,
                messages=[],
                tool_calls=[],
                started=started,
                metadata={**metadata, "error": str(exc)},
            )
            try:
                if not self.db.fail_topic_editor_run(run_id, str(exc), updates, guild_id=guild_id, environment=self.environment):
                    logger.error("TopicEditor fail_topic_editor_run returned None: run_id=%s", run_id)
            except Exception as nested:
                logger.error("TopicEditor fail_topic_editor_run raised: run_id=%s error=%s", run_id, nested)
            raise

    async def _invoke_anthropic(self, messages_arg: Sequence[Dict[str, Any]]) -> Any:
        """One-shot LLM call. The agent loop in `run_once` drives multi-turn behavior.

        A per-call timeout prevents a hung provider request (which has happened in
        prod with a slow/stalled LLM call) from bricking the run forever. On timeout
        the exception propagates to `run_once`, which marks the run failed and
        releases the single-run lease so the next hourly pass can retry.
        """
        timeout_seconds = self._env_float("TOPIC_EDITOR_LLM_TIMEOUT_SECONDS", 600.0)

        async def _call() -> Any:
            system_prompt = self._system_prompt()
            client = getattr(self.llm_client, "client", self.llm_client)
            if hasattr(client, "messages") and hasattr(client.messages, "create"):
                return await client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=list(messages_arg),
                    tools=self._agent_tools(),
                )
            return await self.llm_client.generate_chat_completion(
                model=self.model,
                system_prompt=system_prompt,
                messages=list(messages_arg),
                max_tokens=4096,
                tools=self._agent_tools(),
            )

        return await asyncio.wait_for(_call(), timeout=timeout_seconds)

    def _system_prompt(self) -> str:
        if not self.actor_brief:
            return TOPIC_EDITOR_SYSTEM_PROMPT
        return (
            TOPIC_EDITOR_SYSTEM_PROMPT
            + "\n\nReplay scenario brief. This is the only scenario-specific guidance "
            + "available to the actor; follow it while preserving the topic editor workflow.\n\n"
            + str(self.actor_brief).strip()
        )

    # Known model presets for enrichment — image models for image understanding,
    # video models for video understanding.  We loop over all four so any cached
    # row produced by the dispatcher is surfaced in the initial payload.
    _IMAGE_MODEL_PRESETS = ("gpt-4o-mini", "gpt-5.4")
    _VIDEO_MODEL_PRESETS = ("gemini-2.5-flash", "gemini-2.5-pro")
    _ALL_MODEL_PRESETS = _IMAGE_MODEL_PRESETS + _VIDEO_MODEL_PRESETS

    def _build_initial_user_payload(
        self,
        messages: Sequence[Dict[str, Any]],
        active_topics: Sequence[Dict[str, Any]],
        auto_shortlisted_media: Optional[Sequence[Dict[str, Any]]] = None,
        recovered_drafts: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        source_messages: List[Dict[str, Any]] = []
        for message in messages:
            payload = self._message_payload(message)
            payload["media_understandings"] = self._enrich_media_understandings(message)
            source_messages.append(payload)
        evidence_shelf = [
            _canonical_jsonable(item)
            for item in resolve_topic_editor_evidence_shelf(
                messages,
                db=self.db,
                guild_id=self._resolve_guild_id(),
                environment=self.environment,
            )
        ]
        return {
            "source_messages": source_messages,
            "evidence_shelf": evidence_shelf,
            "active_topics": [
                {
                    "topic_id": topic.get("topic_id"),
                    "canonical_key": topic.get("canonical_key"),
                    "headline": topic.get("headline"),
                    "state": topic.get("state"),
                    "summary": topic.get("summary"),
                    "revisit_at": topic.get("revisit_at"),
                    "source_message_ids": topic.get("source_message_ids") or [],
                    "aliases": topic.get("aliases") or [],
                }
                for topic in active_topics
            ],
            "auto_shortlisted_media": [
                {
                    "topic_id": item.get("topic_id"),
                    "message_id": item.get("message_id"),
                    "reaction_count": item.get("reaction_count"),
                    "headline": item.get("headline"),
                    "reason": item.get("reason"),
                    "media_ref": item.get("media_ref"),
                    "next_action": (
                        "Inspect with message/reply/search context plus understand_image or "
                        "understand_video if not already cached; then publish, keep watching, "
                        "or discard_topic. If it clears the bar, feature it in a "
                        "generation_showcase (publish at most one showcase per run)."
                    ),
                }
                for item in auto_shortlisted_media or []
            ],
            "resumed_drafts": [
                {
                    "draft_id": state.get("draft_id"),
                    "status": state.get("status"),
                    "revision_number": state.get("revision_number"),
                    "headline": (state.get("draft_json") or {}).get("headline"),
                    "template": (state.get("draft_json") or {}).get("template"),
                    "card_count": len((state.get("draft_json") or {}).get("cards") or []),
                    "validation_errors": [
                        err.get("message")
                        for err in ((state.get("latest_validation") or {}).get("errors") or [])
                        if isinstance(err, dict)
                    ],
                }
                for state in recovered_drafts or []
            ],
            "resumed_drafts_instruction": (
                "The drafts below were left unfinished by a previous run. Resume each "
                "one: edit_draft / validate_draft / preview_draft / submit_draft, or "
                "abandon_draft with a fallback_action, or mark needs_review. Do not "
                "silently drop them."
                if recovered_drafts else None
            ),
        }

    def _enrich_media_understandings(
        self, message: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Best-effort: query cache for known model presets.

        Returns a list of understanding dicts (one per cached row).
        Missing rows produce an empty list — never a failure.
        """
        message_id = message.get("message_id")
        if message_id is None:
            return []

        attachments = TopicEditor._normalize_attachment_list(message.get("attachments"))
        if not attachments:
            return []

        results: List[Dict[str, Any]] = []
        for idx in range(len(attachments)):
            for model in self._ALL_MODEL_PRESETS:
                try:
                    row = self.db.get_message_media_understanding(
                        message_id, idx, model
                    )
                except Exception:
                    continue  # best-effort — skip this preset
                if row is None:
                    continue

                understanding = row.get("understanding") or {}
                item: Dict[str, Any] = {
                    "attachment_index": idx,
                    "kind": understanding.get("kind"),
                    "subject": understanding.get("subject"),
                    "technical_signal": understanding.get("technical_signal"),
                    "aesthetic_quality": understanding.get("aesthetic_quality"),
                    "model": model,
                }
                # Attach video-specific fields when present (Gemini models).
                for vf in (
                    "summary",
                    "visual_read",
                    "audio_read",
                    "edit_value",
                    "highlight_score",
                    "energy",
                    "pacing",
                    "production_quality",
                    "boundary_notes",
                    "cautions",
                ):
                    if vf in understanding:
                        item[vf] = understanding[vf]
                results.append(item)

        return results

    def _extract_reasoning_text(self, response: Any) -> str:
        chunks: List[str] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                if text and text.strip():
                    chunks.append(text.strip())
        return "\n\n".join(chunks).strip()

    def _compact_conversation(
        self,
        messages_arg: List[Dict[str, Any]],
        initial_payload: Dict[str, Any],
        dispatcher_context: Dict[str, Any],
        outcomes: List[Dict[str, Any]],
        messages: Sequence[Dict[str, Any]],
        turn_count: int,
    ) -> List[Dict[str, Any]]:
        """Fold the growing conversation into a compact "decisions so far" recap.

        Drops the static source dump and the earlier turns, keeping the most
        recent full turn (assistant message + its tool_result user message)
        verbatim so the model retains immediate context while later turns pay
        far fewer input tokens.
        """
        recap = self._compaction_recap_text(
            dispatcher_context=dispatcher_context,
            outcomes=outcomes,
            messages=messages,
        )
        compacted: List[Dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": recap}]}
        ]
        # PAIR-SAFE tail: preserve from the last assistant message that carries a
        # tool_use block, so the preserved tail always starts with an assistant
        # tool_use and never with an orphaned user tool_result (which would be
        # rejected by Anthropic-style clients). If no such assistant exists (e.g.
        # first-turn compaction), preserve only the recap — this also drops the
        # initial static dump for very low thresholds.
        tail_start = self._last_tool_use_assistant_index(messages_arg)
        if tail_start is not None:
            compacted.extend(messages_arg[tail_start:])
        return compacted

    @staticmethod
    def _last_tool_use_assistant_index(messages_arg: Sequence[Dict[str, Any]]) -> Optional[int]:
        """Index of the last assistant message whose content has a tool_use block."""
        for i in range(len(messages_arg) - 1, -1, -1):
            message = messages_arg[i]
            if not message or message.get("role") != "assistant":
                continue
            if TopicEditor._content_has_tool_use(message.get("content")):
                return i
        return None

    @staticmethod
    def _content_has_tool_use(content: Any) -> bool:
        """True if a content list contains a ``tool_use`` block.

        Mirrors ``_assistant_content_from_response`` so DeepSeek-style responses —
        which store the assistant turn inside an ``openai_assistant_message`` block
        whose ``message.content`` carries the ``tool_use`` — are recognized too.
        Without this, compaction would drop the last DeepSeek assistant turn and
        orphan its ``tool_result``.
        """
        if not isinstance(content, list):
            return False
        for block in content:
            block_type = (
                block.get("type")
                if isinstance(block, dict)
                else getattr(block, "type", None)
            )
            if block_type == "tool_use":
                return True
            if block_type == "openai_assistant_message":
                raw_message = (
                    block.get("message")
                    if isinstance(block, dict)
                    else getattr(block, "message", None)
                )
                inner_content = (
                    raw_message.get("content")
                    if isinstance(raw_message, dict)
                    else getattr(raw_message, "content", None)
                )
                if TopicEditor._content_has_tool_use(inner_content):
                    return True
        return False
    def _compaction_recap_text(
        self,
        *,
        dispatcher_context: Dict[str, Any],
        outcomes: List[Dict[str, Any]],
        messages: Sequence[Dict[str, Any]],
    ) -> str:
        lines: List[str] = [
            "Run status recap (the earlier conversation was compacted to save tokens).",
            "",
            "Your standing job: curate a small, source-backed public update for the "
            "community from the source window, and close the run with finalize_run.",
            "",
            "--- Source window digest ---",
            f"Total source messages: {len(messages or [])}",
        ]
        channel_tally = self._tally_channels(messages)
        if channel_tally:
            tally = ", ".join(
                f"{name}={count}"
                for name, count in sorted(channel_tally.items(), key=lambda kv: kv[1], reverse=True)
            )
            lines.append(f"Channel tally: {tally}")
        top_items = self._top_source_items(messages, limit=6)
        if top_items:
            lines.append("Top items by reactions:")
            lines.extend(top_items)
        lines.append("")

        active_lines = self._active_topics_recap(dispatcher_context)
        if active_lines:
            lines.append("--- Active / watching topics ---")
            lines.extend(active_lines)
            lines.append("")

        lines.append("--- Decisions so far ---")
        decision_lines = self._decisions_so_far(outcomes, dispatcher_context)
        if decision_lines:
            lines.extend(decision_lines)
        else:
            lines.append("(no accepted decisions yet)")
        lines.append("")

        created_lines = self._created_topics_recap(dispatcher_context)
        if created_lines:
            lines.append("--- Topics created this run ---")
            lines.extend(created_lines)
            lines.append("")

        lines.append(
            "The full source-message dump and earlier turns were summarized to save tokens. "
            "Use the message/reply/search context tools to re-read any specific source in "
            "full before deciding on it."
        )
        return "\n".join(lines)

    def _top_source_items(self, messages: Sequence[Dict[str, Any]], limit: int = 6) -> List[str]:
        ranked = sorted(
            (m for m in (messages or []) if m.get("content")),
            key=lambda m: self._message_reaction_count(m),
            reverse=True,
        )
        items: List[str] = []
        for message in ranked[:limit]:
            content = str(message.get("content") or "").replace("\n", " ").strip()
            if len(content) > 120:
                content = content[:117].rstrip() + "..."
            author = self._author_name(message)
            channel = message.get("channel_name") or message.get("channel_id") or "?"
            reactions = self._message_reaction_count(message)
            items.append(
                f"- reactions={reactions} channel={channel} author={author or '?'}: {content}"
            )
        return items

    def _decisions_so_far(
        self,
        outcomes: List[Dict[str, Any]],
        dispatcher_context: Dict[str, Any],
    ) -> List[str]:
        lines: List[str] = []
        seen_keys: set = set()
        # Recent decisions, newest last. Include accepted, idempotent replays and
        # rejected outcomes (with their rejection reason) so a post-compaction
        # model can recover "why" a key was declined.
        recent = (outcomes or [])[-40:]
        for outcome in recent:
            outcome_name = str(outcome.get("outcome") or "?")
            if outcome_name != "accepted" and outcome_name != "idempotent_replay" and not outcome_name.startswith("rejected"):
                continue
            action = str(outcome.get("action") or outcome.get("tool") or "?")
            canonical_key = outcome.get("canonical_key")
            topic_id = outcome.get("topic_id")
            draft_id = outcome.get("draft_id")
            key = canonical_key or topic_id or draft_id or outcome.get("tool_call_id")
            if key is not None:
                key = str(key)
            if key is not None and key in seen_keys:
                continue
            if key is not None:
                seen_keys.add(key)
            if outcome_name.startswith("rejected"):
                reason = outcome.get("error") or outcome.get("reason") or outcome_name
                line = f"- REJECTED {action} reason={reason}"
            elif outcome_name == "idempotent_replay":
                line = f"- replay {action}"
            else:
                line = f"- {action}"
            if canonical_key:
                line += f" canonical_key={canonical_key}"
            if topic_id:
                line += f" topic_id={topic_id}"
            if draft_id:
                line += f" draft_id={draft_id}"
            lines.append(line)
        return lines

    def _active_topics_recap(self, dispatcher_context: Dict[str, Any], cap: int = 30) -> List[str]:
        """Terse list of the active/watching topic set known to the run."""
        lines: List[str] = []
        seen: set = set()
        for topic in dispatcher_context.get("active_topics") or []:
            canonical_key = str(topic.get("canonical_key") or "") or str(topic.get("topic_id") or "")
            if not canonical_key or canonical_key in seen:
                continue
            seen.add(canonical_key)
            headline = str(topic.get("headline") or "")
            if len(headline) > 90:
                headline = headline[:87].rstrip() + "..."
            state = topic.get("state") or "?"
            lines.append(f"- state={state} canonical_key={canonical_key} headline={headline!r}")
            if len(lines) >= cap:
                break
        return lines

    def _created_topics_recap(self, dispatcher_context: Dict[str, Any], cap: int = 30) -> List[str]:
        """Terse list of topics created/published this run with their source ids."""
        by_key = dispatcher_context.get("created_topic_keys") or {}
        created = dispatcher_context.get("created_topics") or []
        lines: List[str] = []
        seen: set = set()
        for topic in list(by_key.values()) + list(created):
            if not isinstance(topic, dict):
                continue
            canonical_key = str(topic.get("canonical_key") or "")
            topic_id = str(topic.get("topic_id") or "")
            key = canonical_key or topic_id
            if not key or key in seen:
                continue
            seen.add(key)
            state = topic.get("state") or "?"
            source_ids = [str(sid) for sid in (topic.get("source_message_ids") or [])][:6]
            lines.append(
                f"- {state} canonical_key={canonical_key} topic_id={topic_id} sources={','.join(source_ids)}"
            )
            if len(lines) >= cap:
                break
        return lines

    def _tool_result_content(self, call: Dict[str, Any], outcome: Dict[str, Any]) -> str:
        """Build a concise tool_result content string to feed back into the agent loop."""
        name = call.get("name") or "?"
        outcome_name = str(outcome.get("outcome") or "unknown")
        if outcome_name in {"rejected_post_simple", "rejected_post_sectioned", "rejected_watch"} or outcome_name.startswith("rejected"):
            err = outcome.get("error") or outcome_name
            return f"tool={name} status={outcome_name} error={err}"
        if outcome_name == "tool_error":
            err = outcome.get("error") or "tool_error"
            return f"tool={name} status=tool_error error={err}"
        if outcome_name == "idempotent_replay":
            return f"tool={name} status=idempotent_replay (already executed in this run)"
        if name in READ_TOOL_NAMES:
            result = outcome.get("result")
            encoded = json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > 2000:
                encoded = encoded[:1970] + "...<truncated>"
            return f"tool={name} status=ok result={encoded}"
        draft_id = outcome.get("draft_id")
        if draft_id:
            action = outcome.get("action") or name
            parts = [f"tool={name} status={outcome_name} action={action} draft_id={draft_id}"]
            validation = outcome.get("validation")
            if isinstance(validation, dict):
                errors = validation.get("errors") or []
                warnings = validation.get("warnings") or []
                if errors:
                    parts.append("errors=" + json.dumps(errors, default=str, ensure_ascii=False, separators=(",", ":"))[:1200])
                if warnings:
                    parts.append("warnings=" + json.dumps(warnings, default=str, ensure_ascii=False, separators=(",", ":"))[:1200])
            if outcome.get("error"):
                parts.append(f"error={outcome.get('error')}")
            return " ".join(parts)
        action = outcome.get("action") or name
        topic_id = outcome.get("topic_id")
        if topic_id:
            return f"tool={name} status=accepted action={action} topic_id={topic_id}"
        return f"tool={name} status={outcome_name} action={action}"

    def _tally_channels(self, messages: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for message in messages or []:
            name = message.get("channel_name") or "?"
            counts[name] = counts.get(name, 0) + 1
        return counts

    def _assistant_content_from_response(self, response: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "openai_assistant_message":
                raw_message = getattr(block, "message", None) or (block.get("message") if isinstance(block, dict) else None)
                if isinstance(raw_message, dict):
                    return [{"type": "openai_assistant_message", "message": raw_message}]

        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                if text:
                    out.append({"type": "text", "text": text})
            elif block_type == "reasoning_content":
                reasoning_content = (
                    getattr(block, "reasoning_content", None)
                    or (block.get("reasoning_content") if isinstance(block, dict) else "")
                )
                if reasoning_content:
                    out.append({"type": "reasoning_content", "reasoning_content": reasoning_content})
            elif block_type == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", None) or block.get("id"),
                        "name": getattr(block, "name", None) or block.get("name"),
                        "input": getattr(block, "input", None) or block.get("input") or {},
                    }
                )
        return out

    def _dispatch_tool_call(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        name = call["name"]
        args = call["input"]
        # d3 first: DB-backed cross-run replay (returns prior outcome if (run_id, tool_call_id) was seen in a prior process)
        replay_outcome = self._idempotent_replay_outcome(call, context)
        if replay_outcome:
            return replay_outcome
        # d1: real read-tool dispatch when read tools are called
        if name in READ_TOOL_NAMES:
            return self._dispatch_read_tool(call, context)
        # d3: in-process idempotency fast path for write tools within a single run
        if name in WRITE_TOOL_NAMES and self._is_idempotent_replay(call, context):
            return {"tool_call_id": call.get("id"), "tool": name, "outcome": "idempotent_replay"}
        if name in DRAFT_TOOL_NAMES:
            return self._dispatch_draft_tool(call, context)
        if name == "record_observation":
            if int(context.get("observation_count") or 0) >= 3:
                self._store_transition({
                    "run_id": context["run_id"],
                    "guild_id": context["guild_id"],
                    "tool_call_id": call["id"],
                    "action": "observation",
                    "reason": "observation_cap_reached",
                    "payload": shape_transition_payload(
                        outcome="tool_error",
                        tool_name=name,
                        source_message_ids=args.get("source_message_ids"),
                        error="observation_cap_reached",
                    ),
                    "model": self.model,
                })
                return {"tool_call_id": call["id"], "tool": name, "outcome": "tool_error", "action": "observation", "error": "observation_cap_reached"}
            source_ids = self._unique_ids(args.get("source_message_ids") or [])
            self.db.store_editorial_observation({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "source_message_ids": source_ids,
                "observation_kind": args.get("observation_kind") or "considered",
                "reason": self._cap_text(args.get("reason"), 500),
            }, environment=self.environment)
            context["observation_count"] = int(context.get("observation_count") or 0) + 1
            self._store_transition({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "observation",
                "reason": self._cap_text(args.get("reason"), 500),
                "payload": shape_transition_payload(outcome="accepted", tool_name=name, source_message_ids=source_ids),
                "model": self.model,
            })
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", "action": "observation"}
        if name in LEGACY_POST_TOOL_NAMES:
            return self._dispatch_legacy_post_tool(call, context)
        if name == "watch_topic":
            return self._dispatch_create_topic_tool(call, context)
        if name == "update_topic_source_messages":
            return self._dispatch_update_sources(call, context)
        if name == "discard_topic":
            return self._dispatch_discard(call, context)
        if name == "finalize_run":
            return self._dispatch_finalize_run(call, context)
        return {"tool_call_id": call["id"], "tool": name, "outcome": "unknown_tool"}

    def _draft_media_message_ids(self, draft_json: Dict[str, Any]) -> List[str]:
        """Collect the message_ids referenced by each card's media_ids.

        Media is attached from the wider evidence shelf, so its owning message is
        often not also listed as a card source. Unioning these into the draft's
        source set lets validation hydrate the media message and resolve its URL.
        """
        ids: List[str] = []
        for card in draft_json.get("cards") or []:
            if not isinstance(card, dict):
                continue
            for media_id in card.get("media_ids") or []:
                try:
                    ref = media_id_to_media_ref(str(media_id))
                    mid = str(ref.get("message_id") or "")
                except (ValueError, TypeError):
                    continue
                if mid and mid not in ids:
                    ids.append(mid)
        return ids

    def _draft_source_ids(self, draft_json: Dict[str, Any]) -> List[str]:
        ids: List[str] = []
        for card in draft_json.get("cards") or []:
            if not isinstance(card, dict):
                continue
            for sid in card.get("source_message_ids") or []:
                sid = str(sid)
                if sid and sid not in ids:
                    ids.append(sid)
        # Media message ids are appended (never prepended) so the per-card positional
        # citation numbering is unchanged; they only widen the hydration set.
        for mid in self._draft_media_message_ids(draft_json):
            if mid not in ids:
                ids.append(mid)
        return ids

    def _draft_evidence_and_metadata(
        self,
        context: Dict[str, Any],
        draft_json: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Tuple[TopicEditorEvidenceItem, ...], Dict[str, Dict[str, Any]]]:
        wanted_ids = self._draft_source_ids(draft_json or {})
        rows_by_id: Dict[str, Dict[str, Any]] = {}
        for message in context.get("messages") or []:
            mid = str(message.get("message_id") or "")
            if mid:
                rows_by_id[mid] = message
        source_rows = [rows_by_id.get(mid) or {"message_id": mid} for mid in wanted_ids]
        if not wanted_ids:
            source_rows = list(rows_by_id.values())
        source_rows = _rehydrate_topic_editor_evidence_rows(
            source_rows,
            db=self.db,
            guild_id=context.get("guild_id"),
            environment=self.environment,
        )
        evidence = resolve_topic_editor_evidence_shelf(
            source_rows,
            db=None,
            guild_id=context.get("guild_id"),
            environment=self.environment,
        )
        source_metadata = {str(row.get("message_id")): row for row in source_rows if row.get("message_id") is not None}
        return evidence, source_metadata

    def _serialize_validation_result(self, result: TopicEditorDraftValidationResult) -> Dict[str, Any]:
        return _canonical_jsonable(result)

    def _draft_state_payload(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "draft_id": state.get("draft_id"),
            "status": state.get("status"),
            "revision_number": state.get("revision_number"),
            "revision_hash": state.get("revision_hash"),
            "revision_attempts": state.get("revision_attempts"),
            "latest_valid_preview_hash": state.get("latest_valid_preview_hash"),
            "topic_id": state.get("topic_id"),
        }

    def _draft_persistence_payload(
        self,
        state: Dict[str, Any],
        context: Dict[str, Any],
        *,
        include_topic_id: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "draft_id": state.get("draft_id"),
            "run_id": context.get("run_id") or state.get("run_id"),
            "guild_id": context.get("guild_id") or state.get("guild_id") or self._resolve_guild_id(),
            "status": state.get("status"),
            "draft_json": state.get("draft_json") or {},
            "validation_result": state.get("latest_validation"),
            "preview_units": state.get("preview_units"),
            "publish_result": state.get("publish_result"),
            "publish_diagnostics": state.get("publish_diagnostics"),
            "revision_number": state.get("revision_number"),
            "revision_hash": state.get("revision_hash"),
            "revision_attempts": state.get("revision_attempts"),
            "latest_valid_preview_hash": state.get("latest_valid_preview_hash"),
            "submitted_at": state.get("submitted_at"),
            "needs_review_reason": state.get("needs_review_reason"),
        }
        payload["topic_id"] = state.get("topic_id") if include_topic_id else None
        return payload

    def _persist_draft_state(
        self,
        state: Dict[str, Any],
        context: Dict[str, Any],
        *,
        create: bool = False,
        include_topic_id: bool = False,
        strict: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Persist a draft row, returning the created/updated row (or None on failure).

        With ``strict=True`` the DB failure is re-raised after logging — used where a
        missing row would orphan the draft across runs (create). Otherwise failures are
        logged and best-effort, since the in-memory state remains authoritative for the
        rest of the run.
        """
        if not self.db or not state.get("draft_id"):
            return None
        payload = self._draft_persistence_payload(state, context, include_topic_id=include_topic_id)
        try:
            if create and hasattr(self.db, "create_topic_editor_draft"):
                created = self.db.create_topic_editor_draft(payload, environment=self.environment)
                if created and str(created.get("draft_id") or "") == str(state.get("draft_id")):
                    return created
                # A partial/absent row means the insert did not durably persist the
                # draft — do NOT fall through to update (it would silently mask a
                # missing row). Report failure so the agent does not believe the
                # draft is durable.
                logger.warning("TopicEditor create_topic_editor_draft returned no draft_id: draft_id=%s created=%s", state.get("draft_id"), created)
                return None
            if hasattr(self.db, "update_topic_editor_draft"):
                return self.db.update_topic_editor_draft(
                    str(state.get("draft_id")),
                    payload,
                    guild_id=payload.get("guild_id"),
                    environment=self.environment,
                )
            return None
        except Exception as exc:
            logger.warning("TopicEditor draft persistence failed: draft_id=%s error=%s", state.get("draft_id"), exc)
            if strict:
                raise
            return None

    def _state_from_persisted_draft_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        draft_json = row.get("draft_json") or {}
        return {
            "draft_id": row.get("draft_id"),
            "run_id": row.get("run_id"),
            "guild_id": row.get("guild_id"),
            "draft_json": draft_json,
            "status": row.get("status") or "drafting",
            "revision_number": row.get("revision_number") or 1,
            "revision_hash": row.get("revision_hash") or revision_hash_for_topic_editor_draft(draft_json),
            "revision_attempts": row.get("revision_attempts") or 0,
            "latest_validation": row.get("validation_result"),
            "preview_units": row.get("preview_units"),
            "latest_valid_preview_hash": row.get("latest_valid_preview_hash"),
            "topic_id": row.get("topic_id"),
            "publish_result": row.get("publish_result"),
            "publish_diagnostics": row.get("publish_diagnostics"),
            "submitted_at": row.get("submitted_at"),
            "recovery_count": row.get("recovery_count") or 0,
            "needs_review_reason": row.get("needs_review_reason"),
        }

    _TERMINAL_DRAFT_STATUSES = frozenset({"submitted", "abandoned", "needs_review"})
    _NONTERMINAL_DRAFT_STATUSES = frozenset({"drafting", "needs_revision", "valid", "blocked_for_submit"})
    _RECOVERABLE_DRAFT_STATUSES = frozenset({"drafting", "needs_revision", "valid", "blocked_for_submit"})

    @staticmethod
    def _draft_is_terminal(status: str) -> bool:
        return status in TopicEditor._TERMINAL_DRAFT_STATUSES

    def _nonterminal_draft_summary(self, context: Dict[str, Any]) -> List[str]:
        """List nonterminal draft id:status pairs for finalize rejection."""
        seen: Dict[str, str] = {}
        for state in list((context.get("drafts") or {}).values()) + list(self.topic_editor_drafts.values()):
            status = str(state.get("status") or "drafting")
            if not self._draft_is_terminal(status):
                seen[str(state.get("draft_id"))] = status
        return [f"{draft_id}:{status}" for draft_id, status in seen.items()]

    def _recover_stale_drafts(self, *, run_id: str, guild_id: int) -> List[Dict[str, Any]]:
        """Claim and re-expose drafts stranded in a nonterminal state by a prior
        run (force-close, lease steal, or clean finalize with open drafts).

        Returns the list of recovered state dicts. ``submitted`` drafts are NOT
        recovered here — their topic was already created, so recovery must not
        re-create/re-publish them; the publish backlog handles those.
        """
        if not self.db or not hasattr(self.db, "get_recent_topic_editor_drafts"):
            return []
        try:
            rows = self.db.get_recent_topic_editor_drafts(
                guild_id=guild_id,
                environment=self.environment,
                limit=min(self._env_int("TOPIC_EDITOR_RECOVERY_MAX_PER_RUN", 8), 100),
                statuses=list(self._RECOVERABLE_DRAFT_STATUSES),
                ascending=True,  # oldest first
            )
        except Exception as exc:
            logger.error("TopicEditor recovery fetch failed: run_id=%s error=%s", run_id, exc)
            return []

        max_claims = self._env_int("TOPIC_EDITOR_RECOVERY_MAX_CLAIMS", 3)
        recovered: List[Dict[str, Any]] = []
        for row in rows or []:
            draft_id = str(row.get("draft_id") or "")
            if not draft_id or draft_id in self.topic_editor_drafts:
                continue
            prior_status = str(row.get("status") or "drafting")
            recovery_count = int(row.get("recovery_count") or 0)
            if recovery_count >= max_claims:
                # Deterministic escape for genuinely-unpublishable drafts: move to a
                # durable human-review backlog instead of surfacing forever.
                try:
                    self.db.update_topic_editor_draft(
                        draft_id,
                        {"status": "needs_review", "needs_review_reason": "recovery_exhausted"},
                        guild_id=guild_id,
                        environment=self.environment,
                    )
                except Exception as exc:
                    logger.error("TopicEditor recovery_exhausted persist failed: draft_id=%s error=%s", draft_id, exc)
                try:
                    self._store_transition({
                        "run_id": run_id,
                        "guild_id": guild_id,
                        "action": "draft_needs_review",
                        "reason": "recovery_exhausted",
                        "payload": shape_transition_payload(
                            outcome="rejected",
                            tool_name="recover_draft",
                            extra={"draft_id": draft_id, "prior_status": prior_status, "recovery_count": recovery_count},
                        ),
                        "model": self.model,
                    })
                except Exception as exc:
                    logger.error("TopicEditor recovery_exhausted transition failed: %s", exc)
                continue

            # Atomic claim: only succeeds if the status is still recoverable at the
            # moment of update. The per-guild run lease already serializes same-guild
            # runs; this guards the residual cross-guild/manual case.
            try:
                claimed = self.db.claim_topic_editor_draft(
                    draft_id,
                    run_id,
                    list(self._RECOVERABLE_DRAFT_STATUSES),
                    guild_id=guild_id,
                    environment=self.environment,
                )
            except Exception as exc:
                logger.error("TopicEditor recovery claim failed: draft_id=%s error=%s", draft_id, exc)
                claimed = None
            if not claimed:
                continue

            # The claim RPC returns the row with recovery_count already incremented
            # (atomic, durable) — use it as the source of truth, no separate bump.
            state = self._state_from_persisted_draft_row(claimed if isinstance(claimed, dict) else row)
            state["revision_attempts"] = 0  # reset the max-revision gate
            claimed_count = int((claimed or {}).get("recovery_count") or recovery_count + 1)
            state["recovery_count"] = claimed_count
            self.topic_editor_drafts[draft_id] = state
            try:
                self._store_transition({
                    "run_id": run_id,
                    "guild_id": guild_id,
                    "action": "draft_recovered",
                    "reason": f"recovered from {prior_status}",
                    "payload": shape_transition_payload(
                        outcome="accepted",
                        tool_name="recover_draft",
                        extra={"draft_id": draft_id, "prior_status": prior_status, "recovery_count": claimed_count},
                    ),
                    "model": self.model,
                })
            except Exception as exc:
                logger.error("TopicEditor recovery transition failed: %s", exc)
            recovered.append(state)
            logger.info("TopicEditor recovered stale draft: run_id=%s draft_id=%s prior_status=%s", run_id, draft_id, prior_status)
        return recovered

    def _load_persisted_draft_state(self, draft_id: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not draft_id or not self.db or not hasattr(self.db, "get_recent_topic_editor_drafts"):
            return None
        try:
            rows = self.db.get_recent_topic_editor_drafts(
                guild_id=context.get("guild_id") or self._resolve_guild_id(),
                environment=self.environment,
                limit=100,
                run_id=context.get("run_id"),
            )
            if not rows:
                rows = self.db.get_recent_topic_editor_drafts(
                    guild_id=context.get("guild_id") or self._resolve_guild_id(),
                    environment=self.environment,
                    limit=100,
                )
        except Exception:
            rows = []
        for row in rows or []:
            if str(row.get("draft_id") or "") != str(draft_id):
                continue
            state = self._state_from_persisted_draft_row(row)
            self.topic_editor_drafts[draft_id] = state
            context.setdefault("drafts", {})[draft_id] = state
            return state
        return None

    def _ensure_media_sources_in_cards(self, draft_json: Dict[str, Any]) -> Dict[str, Any]:
        """Append each card's media message_ids to its own source_message_ids.

        This makes the publish-side source hydration (which resolves media URLs
        from a card's sources) work even when the model attached media from the
        evidence shelf without listing its message as a text source. Appending
        (dedup, order-preserving) keeps the positional citation numbering intact.
        """
        updated = json.loads(json.dumps(draft_json))
        for card in updated.get("cards") or []:
            if not isinstance(card, dict):
                continue
            sources = list(card.get("source_message_ids") or [])
            for media_id in card.get("media_ids") or []:
                try:
                    ref = media_id_to_media_ref(str(media_id))
                    mid = str(ref.get("message_id") or "")
                except (ValueError, TypeError):
                    continue
                if mid and mid not in sources:
                    sources.append(mid)
            card["source_message_ids"] = sources
        return updated

    def _apply_draft_patch(self, draft_json: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        # Deterministic application order (independent of JSON key order):
        #   1. top-level scalar fields
        #   2. remove_card_indices (by ORIGINAL draft index)
        #   3. `cards` positional overlays (against the post-removal list)
        #   4. legacy dotted cards[i].field keys (post-removal index)
        #   5. append_cards
        updated = json.loads(json.dumps(draft_json))
        patch = patch or {}

        for key in ("topic_key", "template", "headline", "dek", "editor_note"):
            if key in patch:
                updated[key] = patch[key]

        cards = [c for c in (updated.get("cards") or []) if isinstance(c, dict)]

        def _is_complete_card(card: Dict[str, Any]) -> bool:
            # Mirror topic_editor_draft_from_json's source filtering so
            # source_message_ids: [""] / [None] does NOT count as complete.
            body = str(card.get("body") or "").strip()
            sources = [s for s in (card.get("source_message_ids") or []) if s]
            return bool(body) and bool(sources)

        # 1. Removals by original index (deduped — a duplicate index must not
        #    remove two cards). Removal is an explicit operation
        #    (remove_card_indices) so the `cards` array can stay a pure
        #    per-card overlay — array length is never overloaded to mean
        #    "drop the unlisted cards".
        remove_indices = sorted(
            {
                int(i)
                for i in (patch.get("remove_card_indices") or [])
                if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()
            },
            reverse=True,
        )
        for idx in remove_indices:
            if 0 <= idx < len(cards):
                cards.pop(idx)

        # 2. Positional per-card overlays: the patch card at array position i
        #    merges onto the card now at position i, preserving any field the
        #    patch omits (angle, body, source_message_ids, media_ids). Cards the
        #    patch does not mention are untouched — editing one card's body must
        #    not wipe the other cards or their sources.
        pos = 0
        for patch_card in patch.get("cards") or []:
            if not isinstance(patch_card, dict):
                logger.warning(
                    "TopicEditor edit_draft patch: ignoring non-object card entry %r", patch_card
                )
                continue
            if pos < len(cards):
                base = dict(cards[pos])
                for fk in _DRAFT_CARD_PATCH_FIELDS:
                    if fk in patch_card:
                        base[fk] = patch_card[fk]
                cards[pos] = base
            else:
                # Positions beyond the existing list are out of contract for an
                # overlay — `cards` edits existing cards, append_cards adds new
                # ones. Keep the entry only if it is a complete card (body +
                # sources) so a mistaken partial overlay never synthesizes an
                # invalid card; anything else is dropped and logged.
                appended = {fk: patch_card[fk] for fk in _DRAFT_CARD_PATCH_FIELDS if fk in patch_card}
                if _is_complete_card(appended):
                    cards.append(appended)
                else:
                    logger.warning(
                        "TopicEditor edit_draft patch: dropped beyond-list card without body/sources "
                        "(pos=%d); use append_cards for new cards", pos
                    )
            pos += 1

        # 3. Legacy dotted keys (cards[i].field), applied after removals and
        #    array overlays so their index is unambiguous.
        for key, value in patch.items():
            match = re.match(r"cards\[(\d+)\]\.(\w+)$", key)
            if not match:
                continue
            idx = int(match.group(1))
            field_name = match.group(2)
            if 0 <= idx < len(cards) and isinstance(cards[idx], dict):
                cards[idx][field_name] = value

        # 4. Append explicit new cards.
        for patch_card in patch.get("append_cards") or []:
            if not isinstance(patch_card, dict):
                logger.warning(
                    "TopicEditor edit_draft patch: ignoring non-object append_cards entry %r", patch_card
                )
                continue
            full_card = {fk: patch_card[fk] for fk in _DRAFT_CARD_PATCH_FIELDS if fk in patch_card}
            if _is_complete_card(full_card):
                cards.append(full_card)
            else:
                logger.warning(
                    "TopicEditor edit_draft patch: append_cards entry lacks body or resolvable sources "
                    "and was dropped: %r", patch_card
                )

        updated["cards"] = cards
        return canonical_topic_editor_draft_json(topic_editor_draft_from_json(updated))

    def _legacy_post_refusal(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call.get("input") or {}
        source_ids = self._unique_ids(args.get("source_message_ids") or [])
        canonical_key = canonicalize_proposed_key(args.get("proposed_key"), args.get("headline") or "")
        self._store_transition({
            "run_id": context.get("run_id"),
            "guild_id": context.get("guild_id"),
            "tool_call_id": call.get("id"),
            "action": "legacy_post_disabled",
            "reason": "legacy_post_disabled",
            "payload": shape_transition_payload(
                outcome="tool_error",
                tool_name=call.get("name"),
                canonical_key=canonical_key,
                proposed_key=args.get("proposed_key"),
                source_message_ids=source_ids,
                error="legacy_post_disabled",
                extra={"legacy_post_mode": self.legacy_post_mode},
            ),
            "model": self.model,
        })
        return {
            "tool_call_id": call.get("id"),
            "tool": call.get("name"),
            "outcome": "tool_error",
            "error": "legacy_post_disabled",
            "legacy_post_mode": self.legacy_post_mode,
        }

    def _dispatch_legacy_post_tool(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        if self.legacy_post_mode == "disabled":
            return self._legacy_post_refusal(call, context)
        if self.legacy_post_mode == "draft_adapter":
            return self._dispatch_legacy_post_adapter(call, context)
        outcome = self._dispatch_create_topic_tool(call, context)
        outcome["legacy_direct_post_used"] = True
        outcome["legacy_post_mode"] = "direct"
        return outcome

    def _legacy_call_to_draft_args(self, call: Dict[str, Any]) -> Dict[str, Any]:
        name = call.get("name")
        args = call.get("input") or {}
        cards: List[Dict[str, Any]] = []
        if name in {"post_topic", "post_sectioned_topic"} and args.get("blocks"):
            try:
                blocks = normalize_document_blocks(
                    {"blocks": args.get("blocks") or []},
                    topic_source_message_ids=args.get("source_message_ids") or [],
                )
            except Exception:
                blocks = []
            for block in blocks:
                media_ids: List[str] = []
                for ref in block_media_refs(block):
                    try:
                        media_ids.append(media_ref_to_media_id(ref))
                    except Exception:
                        continue
                cards.append({
                    "angle": block.get("title") or block.get("type") or "What changed",
                    "body": block.get("text") or "",
                    "source_message_ids": block_source_ids(block),
                    "media_ids": media_ids,
                })
        elif name == "post_sectioned_topic" and args.get("sections"):
            source_ids = self._unique_ids(args.get("source_message_ids") or [])
            for section in args.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                body = section.get("body") or section.get("text") or ""
                cards.append({
                    "angle": section.get("title") or "What changed",
                    "body": body,
                    "source_message_ids": source_ids,
                    "media_ids": [],
                })
        else:
            cards.append({
                "angle": "What changed",
                "body": args.get("body") or args.get("summary") or "",
                "source_message_ids": self._unique_ids(args.get("source_message_ids") or []),
                "media_ids": [],
            })
        return {
            "draft_id": f"draft-{uuid.uuid4().hex[:12]}",
            "topic_key": args.get("proposed_key"),
            "template": "tool_workflow_update",
            "headline": args.get("headline"),
            "dek": args.get("dek") or args.get("why_interesting") or args.get("notes") or "",
            "cards": cards,
            "editor_note": args.get("notes") or args.get("why_interesting") or "Legacy post adapted through the draft pipeline.",
        }

    def _dispatch_legacy_post_adapter(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        create_call = {
            "id": f"{call.get('id')}:create_draft",
            "name": "create_draft",
            "input": self._legacy_call_to_draft_args(call),
        }
        created = self._dispatch_draft_tool(create_call, context)
        draft_id = created.get("draft_id")
        if created.get("outcome") != "accepted" or not draft_id:
            return {
                "tool_call_id": call.get("id"),
                "tool": call.get("name"),
                "outcome": "tool_error",
                "error": "legacy_draft_adapter_create_failed",
                "legacy_post_mode": "draft_adapter",
                "create_result": created,
            }
        previewed = self._dispatch_draft_tool(
            {"id": f"{call.get('id')}:preview_draft", "name": "preview_draft", "input": {"draft_id": draft_id}},
            context,
        )
        if previewed.get("outcome") != "accepted":
            return {
                "tool_call_id": call.get("id"),
                "tool": call.get("name"),
                "outcome": "blocked_for_submit",
                "error": "legacy_draft_adapter_preview_failed",
                "legacy_post_mode": "draft_adapter",
                "draft_id": draft_id,
                "preview_result": previewed,
            }
        submitted = self._dispatch_draft_tool(
            {"id": f"{call.get('id')}:submit_draft", "name": "submit_draft", "input": {"draft_id": draft_id}},
            context,
        )
        submitted.update({
            "tool_call_id": call.get("id"),
            "tool": call.get("name"),
            "legacy_post_mode": "draft_adapter",
            "adapter_draft_id": draft_id,
            "adapted_draft_id": draft_id,
        })
        return submitted

    def _dispatch_draft_tool(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        name = call["name"]
        args = call.get("input") or {}
        draft_id = str(args.get("draft_id") or "")

        if name == "create_draft":
            draft_id = str(args.get("draft_id") or f"draft-{uuid.uuid4().hex[:12]}")
            draft = topic_editor_draft_from_json({
                "draft_id": draft_id,
                "topic_key": args.get("topic_key"),
                "template": args.get("template"),
                "headline": args.get("headline"),
                "dek": args.get("dek"),
                "cards": args.get("cards") or [],
                "editor_note": args.get("editor_note"),
            })
            draft_json = canonical_topic_editor_draft_json(draft)
            revision_hash = revision_hash_for_topic_editor_draft(draft_json)
            state = {
                "draft_id": draft_id,
                "draft_json": draft_json,
                "status": "drafting",
                "revision_number": 1,
                "revision_hash": revision_hash,
                "revision_attempts": 0,
                "latest_validation": None,
                "preview_units": None,
                "latest_valid_preview_hash": None,
                "topic_id": None,
                "publish_diagnostics": None,
            }
            self.topic_editor_drafts[draft_id] = state
            context.setdefault("drafts", {})[draft_id] = state
            try:
                persisted = self._persist_draft_state(state, context, create=True, include_topic_id=False, strict=True)
            except Exception as exc:
                logger.error("TopicEditor create_draft persistence raised: draft_id=%s error=%s", draft_id, exc)
                persisted = None
            if persisted is None:
                # A draft row that never reaches the DB is orphaned on the next run —
                # fail loudly so the agent does not believe the draft is durable.
                return {
                    "tool_call_id": call["id"],
                    "tool": name,
                    "outcome": "tool_error",
                    "error": "draft_persistence_failed",
                    "draft_id": draft_id,
                    **self._draft_state_payload(state),
                    "draft": draft_json,
                }
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", **self._draft_state_payload(state), "draft": draft_json}

        state = (
            self.topic_editor_drafts.get(draft_id)
            or (context.get("drafts") or {}).get(draft_id)
            or self._load_persisted_draft_state(draft_id, context)
        )
        if not state:
            return {"tool_call_id": call["id"], "tool": name, "outcome": "tool_error", "error": "draft_not_found", "draft_id": draft_id}

        # Ensure every card's media message_ids are also present in its
        # source_message_ids so media URL resolution works during validate /
        # preview / submit even when the model attached media from the shelf
        # without listing its message as a text source. Idempotent (dedup).
        if name in ("validate_draft", "preview_draft", "submit_draft"):
            state["draft_json"] = self._ensure_media_sources_in_cards(state.get("draft_json") or {})

        if name == "edit_draft":
            if int(state.get("revision_attempts") or 0) >= int(self.draft_limits.max_revision_attempts):
                state["status"] = "needs_revision"
                self._persist_draft_state(state, context, include_topic_id=False)
                return {
                    "tool_call_id": call["id"],
                    "tool": name,
                    "outcome": "max_revision_attempts_exceeded",
                    "error": "max_revision_attempts_exceeded",
                    "max_revision_attempts": self.draft_limits.max_revision_attempts,
                    "required_next_action": "abandon_draft_or_watch_update_discard",
                    **self._draft_state_payload(state),
                }
            draft_json = self._apply_draft_patch(state["draft_json"], args.get("patch") or {})
            state["draft_json"] = draft_json
            state["revision_number"] = int(state.get("revision_number") or 1) + 1
            state["revision_attempts"] = int(state.get("revision_attempts") or 0) + 1
            state["revision_hash"] = revision_hash_for_topic_editor_draft(draft_json)
            state["status"] = "drafting"
            state["latest_validation"] = None
            state["preview_units"] = None
            state["latest_valid_preview_hash"] = None
            state["edit_reason"] = args.get("reason")
            self._persist_draft_state(state, context, include_topic_id=False)
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", **self._draft_state_payload(state), "draft": draft_json}

        evidence, source_metadata = self._draft_evidence_and_metadata(context, state["draft_json"])
        if name == "validate_draft":
            result = validate_topic_editor_draft(
                state["draft_json"],
                evidence,
                source_metadata,
                self.draft_limits,
                mode="draft",
            )
            state["latest_validation"] = self._serialize_validation_result(result)
            state["status"] = result.status
            self._persist_draft_state(state, context, include_topic_id=False)
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", **self._draft_state_payload(state), "validation": state["latest_validation"]}

        if name == "preview_draft":
            result = validate_topic_editor_draft(
                state["draft_json"],
                evidence,
                source_metadata,
                self.draft_limits,
                mode="preview",
            )
            state["latest_validation"] = self._serialize_validation_result(result)
            if result.errors:
                state["status"] = "needs_revision"
                state["preview_units"] = None
                state["latest_valid_preview_hash"] = None
                self._persist_draft_state(state, context, include_topic_id=False)
                return {"tool_call_id": call["id"], "tool": name, "outcome": "needs_revision", **self._draft_state_payload(state), "validation": state["latest_validation"]}
            preview_units = preview_topic_editor_draft(
                state["draft_json"],
                source_metadata,
                evidence_shelf=evidence,
                limits=self.draft_limits,
            )
            state["preview_units"] = preview_units
            state["latest_valid_preview_hash"] = state["revision_hash"]
            state["status"] = "valid"
            self._persist_draft_state(state, context, include_topic_id=False)
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", **self._draft_state_payload(state), "validation": state["latest_validation"], "preview_units": preview_units}

        if name == "submit_draft":
            result = validate_topic_editor_draft(
                state["draft_json"],
                evidence,
                source_metadata,
                self.draft_limits,
                mode="submit",
                latest_valid_preview_hash=state.get("latest_valid_preview_hash"),
            )
            state["latest_validation"] = self._serialize_validation_result(result)
            if result.errors:
                state["status"] = "needs_revision" if result.status != "blocked_for_submit" else "blocked_for_submit"
                self._persist_draft_state(state, context, include_topic_id=False)
                return {"tool_call_id": call["id"], "tool": name, "outcome": "blocked_for_submit", **self._draft_state_payload(state), "validation": state["latest_validation"]}
            topic = topic_editor_draft_to_structured_topic(
                state["draft_json"],
                evidence_shelf=evidence,
                guild_id=context.get("guild_id"),
            )
            submit_call = {
                "id": call["id"],
                "name": "post_topic",
                "input": {
                    "proposed_key": topic.get("proposed_key"),
                    "headline": topic.get("headline"),
                    "source_message_ids": topic.get("source_message_ids") or [],
                    "blocks": (topic.get("summary") or {}).get("blocks") or [],
                    "notes": (topic.get("summary") or {}).get("editor_note"),
                },
            }
            submit_outcome = self._dispatch_create_topic_tool(submit_call, context)
            if submit_outcome.get("outcome") == "accepted":
                state["status"] = "submitted"
                state["topic_id"] = submit_outcome.get("topic_id")
                state["submitted_at"] = datetime.now(timezone.utc).isoformat()
                topic_id = submit_outcome.get("topic_id")
                if topic_id:
                    self._store_transition({
                        "topic_id": topic_id,
                        "run_id": context.get("run_id"),
                        "guild_id": context.get("guild_id"),
                        "tool_call_id": call.get("id"),
                        "to_state": "posted",
                        "action": "submit_draft",
                        "reason": state["draft_json"].get("editor_note"),
                        "payload": shape_transition_payload(
                            outcome="accepted",
                            tool_name=name,
                            canonical_key=topic.get("canonical_key"),
                            proposed_key=topic.get("proposed_key"),
                            source_message_ids=topic.get("source_message_ids") or [],
                            extra={
                                "draft_id": draft_id,
                                "revision_hash": state.get("revision_hash"),
                                "revision_number": state.get("revision_number"),
                            },
                        ),
                        "model": self.model,
                    })
                self._persist_draft_state(state, context, include_topic_id=True)
            else:
                self._persist_draft_state(state, context, include_topic_id=False)
            return {"tool_call_id": call["id"], "tool": name, "outcome": submit_outcome.get("outcome"), **self._draft_state_payload(state), "submit_result": submit_outcome, "validation": state["latest_validation"]}

        if name == "abandon_draft":
            fallback = args.get("fallback_action")
            if fallback == "needs_review":
                # Durable human-review backlog instead of a silent tombstone: the
                # draft stays visible to ops, and recovery never auto-reclaims it.
                state["status"] = "needs_review"
                state["topic_id"] = None
                state["abandon_reason"] = args.get("reason")
                state["fallback_action"] = fallback
                state["needs_review_reason"] = args.get("reason")
            else:
                state["status"] = "abandoned"
                state["topic_id"] = None
                state["abandon_reason"] = args.get("reason")
                state["fallback_action"] = fallback
            self._persist_draft_state(state, context, include_topic_id=False)
            return {"tool_call_id": call["id"], "tool": name, "outcome": "accepted", **self._draft_state_payload(state), "reason": args.get("reason"), "fallback_action": fallback}

        return {"tool_call_id": call["id"], "tool": name, "outcome": "unknown_tool"}

    def _dispatch_read_tool(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        name = call["name"]
        args = call.get("input") or {}
        try:
            if name == "search_topics":
                result = self.db.search_topic_editor_topics(
                    query=args.get("query") or "",
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    state_filter=args.get("state_filter"),
                    hours_back=int(args.get("hours_back") or 72),
                    limit=10,
                )
            elif name == "search_messages":
                scope = str(args.get("scope") or "window").lower()
                if scope == "window":
                    result = self._search_window_messages(
                        context.get("messages") or [],
                        query=args.get("query"),
                        from_author_id=args.get("from_author_id"),
                        in_channel_id=args.get("in_channel_id"),
                        mentions_author_id=args.get("mentions_author_id"),
                        has=args.get("has"),
                        after=args.get("after"),
                        before=args.get("before"),
                        is_reply=args.get("is_reply"),
                        limit=int(args.get("limit") if args.get("limit") is not None else 20),
                    )
                elif scope == "archive":
                    result = self.db.search_messages_unified(
                        scope="archive",
                        guild_id=context.get("guild_id"),
                        environment=self.environment,
                        query=args.get("query"),
                        from_author_id=args.get("from_author_id"),
                        in_channel_id=args.get("in_channel_id"),
                        mentions_author_id=args.get("mentions_author_id"),
                        has=args.get("has"),
                        after=args.get("after"),
                        before=args.get("before"),
                        is_reply=args.get("is_reply"),
                        limit=int(args.get("limit") if args.get("limit") is not None else 20),
                    )
                else:
                    raise ValueError(f"Unknown scope: {scope}")
            elif name == "get_author_profile":
                result = self.db.get_topic_editor_author_profile(
                    args.get("author_id"),
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                )
            elif name == "get_message_context":
                messages = self.db.get_topic_editor_message_context(
                    args.get("message_ids") or [],
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    limit=10,
                )
                evidence_by_id = {
                    item.message_id: _canonical_jsonable(item)
                    for item in resolve_topic_editor_evidence_shelf(
                        messages,
                        db=self.db,
                        guild_id=context.get("guild_id"),
                        environment=self.environment,
                    )
                }
                result = [
                    dict(message, evidence_item=evidence_by_id.get(str(message.get("message_id"))))
                    for message in messages
                ]
            elif name == "understand_image":
                return self._dispatch_understand_media(call, context, "image")
            elif name == "understand_video":
                return self._dispatch_understand_media(call, context, "video")
            elif name == "get_reply_chain":
                max_depth = max(1, min(int(args.get("max_depth") or 5), 15))
                result = self.db.get_reply_chain(
                    message_id=str(args.get("message_id") or ""),
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    max_depth=max_depth,
                )
            else:
                result = None
            return {"tool_call_id": call["id"], "tool": name, "outcome": "read", "result": result}
        except Exception as exc:
            return {"tool_call_id": call.get("id"), "tool": name, "outcome": "tool_error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Image / video understanding tool dispatcher
    # ------------------------------------------------------------------

    # Fixed estimated costs in USD (deducted from the per-run vision budget
    # only when an actual API call is made, never on cache hits).
    _VISION_COST_IMAGE = 0.01
    _VISION_COST_VIDEO = 0.05

    # mode → model mapping
    _IMAGE_MODEL_MAP = {"fast": "gpt-4o-mini", "best": "gpt-5.4"}
    _VIDEO_MODEL_MAP = {"fast": "gemini-2.5-flash", "best": "gemini-2.5-pro"}

    def _dispatch_understand_media(
        self, call: Dict[str, Any], context: Dict[str, Any], media_kind: str
    ) -> Dict[str, Any]:
        """Shared dispatch for understand_image / understand_video.

        (a) Resolve source message from context['messages'] by message_id.
        (b) Resolve attachment URL.
        (c) Download bytes via sync ``requests.get(url)``.
        (d) Compute sha256.
        (e) Check PK cache → hash cache → budget.
        (f) Budget exceeded → return ``{outcome: budget_exceeded}``.
        (g) Call vision_clients.describe_image / describe_video.
        (h) Persist result, return compact JSON.

        .. note::

            **URL truncation investigation (2025-05):** A user-reported 404 error
            (``Not Found for url: LTX-23-i2v_00416-audio.mp4``) showed a bare
            filename rather than a full CDN URL.  Investigation found that archive
            data may store bare filenames in the ``url`` field of attachment dicts
            (e.g. when the upstream source doesn't preserve the full
            ``cdn.discordapp.com`` path).  The guard below prefers ``proxy_url``
            when ``url`` does not start with ``http`` to mitigate this.  The
            enriched rejection output (``_build_trace_embed``) also surfaces the
            actual ``media_url`` on its own line so future occurrences are
            visible in production.
        """
        import requests

        from src.common.vision_clients import describe_image, describe_video, _sha256

        name = call["name"]
        args = call.get("input") or {}
        message_id = args.get("message_id")
        attachment_index = int(args.get("attachment_index") or 0)
        mode = args.get("mode") or "fast"

        # (a) resolve source message
        messages = context.get("messages") or []
        source = None
        for msg in messages:
            if str(msg.get("message_id")) == str(message_id):
                source = msg
                break
        if source is None:
            try:
                resolved = self.db.get_topic_editor_source_messages(
                    [str(message_id)],
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                    limit=1,
                )
            except Exception:
                resolved = []
            if resolved:
                source = resolved[0]
            else:
                return {
                    "tool_call_id": call["id"],
                    "tool": name,
                    "outcome": "tool_error",
                    "error": f"message_id={message_id} not found in source window or archive",
                    "message_id": str(message_id) if message_id is not None else None,
                }

        # (b) resolve attachment URL
        attachments = TopicEditor._normalize_attachment_list(source.get("attachments"))
        if attachment_index < 0 or attachment_index >= len(attachments):
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": (
                    f"attachment_index={attachment_index} out of range "
                    f"(message has {len(attachments)} attachment(s))"
                ),
                "message_id": str(message_id) if message_id is not None else None,
                "channel_id": source.get("channel_id") if source else None,
                "guild_id": source.get("guild_id") if source else None,
            }
        attachment = attachments[attachment_index]
        # Prefer proxy_url when url is a bare filename (doesn't start with http).
        # Archive data may store bare filenames in the url field; proxy_url is
        # always a full CDN URL.  Fall back to whichever is available.
        _raw_url = attachment.get("url") or ""
        _proxy_url = attachment.get("proxy_url") or ""
        if _raw_url.startswith("http"):
            media_url = _raw_url
        elif _proxy_url:
            media_url = _proxy_url
        else:
            media_url = _raw_url  # let the download fail with a visible URL
        if not media_url:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": f"attachment {attachment_index} has no url field",
                "message_id": str(message_id) if message_id is not None else None,
                "channel_id": source.get("channel_id") if source else None,
                "guild_id": source.get("guild_id") if source else None,
            }

        # model preset
        if media_kind == "image":
            model = self._IMAGE_MODEL_MAP.get(mode, "gpt-4o-mini")
        else:
            model = self._VIDEO_MODEL_MAP.get(mode, "gemini-2.5-flash")

        # (e) PK cache check
        try:
            cached = self.db.get_message_media_understanding(message_id, attachment_index, model)
        except Exception:
            cached = None
        if cached is not None:
            understanding = cached.get("understanding") or {}
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "read",
                "result": {"cached": True, "understanding": understanding},
            }

        # (c) download bytes via sync requests.get
        try:
            resp = requests.get(media_url, timeout=30)
            resp.raise_for_status()
            media_bytes = resp.content
        except Exception as exc:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": f"failed to download media: {exc}",
                "message_id": str(message_id) if message_id is not None else None,
                "channel_id": source.get("channel_id") if source else None,
                "guild_id": source.get("guild_id") if source else (
                    context.get("guild_id")
                ),
                "media_url": media_url,
            }

        # (d) compute sha256
        content_hash = _sha256(media_bytes)

        # (e) hash cache check
        try:
            cached_by_hash = self.db.get_message_media_understanding_by_hash(content_hash, model=model)
        except Exception:
            cached_by_hash = None
        if cached_by_hash is not None:
            understanding = cached_by_hash.get("understanding") or {}
            # Persist the row for this (message_id, attachment_index) so future
            # PK lookups hit immediately, without another download.
            try:
                self.db.upsert_message_media_understanding({
                    "message_id": message_id,
                    "attachment_index": attachment_index,
                    "media_url": media_url,
                    "media_kind": media_kind,
                    "content_hash": content_hash,
                    "model": model,
                    "understanding": understanding,
                })
            except Exception:
                pass  # best-effort write
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "read",
                "result": {"cached": True, "dedup": True, "understanding": understanding},
            }

        # (f) budget check
        budget = float(context.get("vision_budget_usd") or 1.0)
        spent = float(context.get("vision_cost_usd") or 0.0)
        cost_estimate = self._VISION_COST_IMAGE if media_kind == "image" else self._VISION_COST_VIDEO
        if spent + cost_estimate > budget:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "budget_exceeded",
                "error": (
                    f"vision budget spent ${spent:.2f} of ${budget:.2f}; "
                    f"estimated cost ${cost_estimate:.2f} would exceed cap"
                ),
            }

        # (g) call vision API
        try:
            if media_kind == "image":
                understanding = describe_image(media_bytes, model)
            else:
                understanding = describe_video(media_bytes, model)
        except Exception as exc:
            return {
                "tool_call_id": call["id"],
                "tool": name,
                "outcome": "tool_error",
                "error": f"vision API call failed: {exc}",
            }

        # deduct cost
        context["vision_cost_usd"] = round(spent + cost_estimate, 4)

        # (h) persist and return
        try:
            self.db.upsert_message_media_understanding({
                "message_id": message_id,
                "attachment_index": attachment_index,
                "media_url": media_url,
                "media_kind": media_kind,
                "content_hash": content_hash,
                "model": model,
                "understanding": understanding,
            })
        except Exception:
            pass  # best-effort write; still return the understanding

        return {
            "tool_call_id": call["id"],
            "tool": name,
            "outcome": "read",
            "result": {"cached": False, "understanding": understanding},
        }

    def _parse_time_bound(self, value: Optional[str], default: datetime) -> datetime:
        """Parse a time-bound string: ISO timestamp or relative like '24h', '7d'.

        Returns the parsed datetime or *default* if value is None.
        Raises ValueError for malformed input (surfaces as tool_error).
        """
        if value is None:
            return default
        # Try ISO timestamp first
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
        # Try relative pattern: number + h/d
        m = re.match(r'^(\d+)\s*([hd])$', str(value).strip().lower())
        if m:
            amount = int(m.group(1))
            unit = m.group(2)
            if unit == 'h':
                return datetime.now(timezone.utc) - timedelta(hours=amount)
            else:
                return datetime.now(timezone.utc) - timedelta(days=amount)
        raise ValueError(f"Invalid time format: {value}")

    def _search_window_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        query: Optional[str] = None,
        from_author_id: Optional[Any] = None,
        in_channel_id: Optional[Any] = None,
        mentions_author_id: Optional[Any] = None,
        has: Optional[List[str]] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        is_reply: Optional[bool] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search in-memory source messages with AND-combined Discord-style filters."""
        # --- helpers --------------------------------------------------
        _IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
        _VIDEO_EXT = {'.mp4', '.mov', '.webm', '.mkv'}
        _AUDIO_EXT = {'.mp3', '.wav', '.ogg', '.flac'}

        def _ext(filename: str) -> str:
            return (os.path.splitext(filename or '')[1] or '').lower()

        def _is_media_kind(attachments: List[Dict[str, Any]], kind: str) -> bool:
            for a in attachments:
                ct = str(a.get('content_type') or '').lower()
                fn = str(a.get('filename') or '').lower()
                if kind == 'image' and (ct.startswith('image/') or _ext(fn) in _IMAGE_EXT):
                    return True
                if kind == 'video' and (ct.startswith('video/') or _ext(fn) in _VIDEO_EXT):
                    return True
                if kind == 'audio' and (ct.startswith('audio/') or _ext(fn) in _AUDIO_EXT):
                    return True
                if kind == 'file':
                    return True  # any attachment exists
            return False

        # --- defaults -------------------------------------------------
        needle = str(query or '').lower()
        safe_limit = max(1, min(int(limit) if limit is not None else 20, 50))
        has_set = set(has or [])

        # Parse time bounds
        far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        far_future = datetime(2099, 12, 31, tzinfo=timezone.utc)
        after_dt = self._parse_time_bound(after, far_past)
        before_dt = self._parse_time_bound(before, far_future)

        rows: List[Dict[str, Any]] = []
        for message in messages or []:
            # --- from_author_id filter ---
            if from_author_id is not None and str(message.get('author_id')) != str(from_author_id):
                continue

            # --- in_channel_id filter ---
            if in_channel_id is not None and str(message.get('channel_id')) != str(in_channel_id):
                continue

            # --- mentions_author_id filter ---
            if mentions_author_id is not None:
                content = str(message.get('content') or '')
                if not re.search(rf'<@!?{mentions_author_id}>', content):
                    continue

            # --- content query filter ---
            content = str(message.get('content') or '')
            if needle and needle not in content.lower():
                continue

            # --- has filter ---
            if has_set:
                atts = self._normalize_attachment_list(message.get('attachments'))
                embs = self._normalize_attachment_list(message.get('embeds'))
                filter_pass = True
                for h in has_set:
                    if h == 'image' and not _is_media_kind(atts, 'image'):
                        filter_pass = False; break
                    if h == 'video' and not _is_media_kind(atts, 'video'):
                        filter_pass = False; break
                    if h == 'audio' and not _is_media_kind(atts, 'audio'):
                        filter_pass = False; break
                    if h == 'link' and not ('http://' in content or 'https://' in content):
                        filter_pass = False; break
                    if h == 'embed' and len(embs) == 0:
                        filter_pass = False; break
                    if h == 'file' and len(atts) == 0:
                        filter_pass = False; break
                if not filter_pass:
                    continue

            # --- after / before filter ---
            created = message.get('created_at')
            if created:
                try:
                    text = str(created).replace('Z', '+00:00')
                    dt = datetime.fromisoformat(text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < after_dt or dt > before_dt:
                        continue
                except (ValueError, TypeError):
                    pass  # If we can't parse, include the message

            # --- is_reply filter ---
            if is_reply is not None:
                has_reply = bool(message.get('reply_to_message_id') or message.get('reference_id'))
                if is_reply and not has_reply:
                    continue
                if not is_reply and has_reply:
                    continue

            # --- build compact row ---
            author = message.get('author_context_snapshot') or message.get('author') or {}
            atts = self._normalize_attachment_list(message.get('attachments'))
            embs = self._normalize_attachment_list(message.get('embeds'))

            row = {
                'message_id': str(message.get('message_id')),
                'channel_id': str(message.get('channel_id')),
                'channel_name': message.get('channel_name'),
                'author_id': str(message.get('author_id')),
                'author_name': (
                    message.get('author_name')
                    or author.get('server_nick')
                    or author.get('global_name')
                    or author.get('display_name')
                    or author.get('username')
                ),
                'content_preview': self._cap_text(content, 200),
                'created_at': message.get('created_at'),
                'reaction_count': self._message_reaction_count(message),
                'reply_to_message_id': message.get('reply_to_message_id') or message.get('reference_id'),
                'has_attachments': len(atts) > 0,
                'has_links': bool(re.search(r'https?://', content)),
                'has_image': _is_media_kind(atts, 'image'),
                'has_video': _is_media_kind(atts, 'video'),
                'has_audio': _is_media_kind(atts, 'audio'),
                'has_embed': len(embs) > 0,
            }
            rows.append(row)
            if len(rows) >= safe_limit:
                break

        # 2KB JSON cap
        while rows:
            payload = json.dumps(rows, default=str)
            if len(payload.encode('utf-8')) <= 2048:
                break
            rows.pop()
        return rows

    def _message_is_since(self, created_at: Any, since: datetime) -> bool:
        if not created_at:
            return True
        try:
            text = str(created_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= since
        except Exception:
            return True

    def _dispatch_finalize_run(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call.get("input") or {}
        reasoning = (args.get("overall_reasoning") or "").strip()
        topics_considered = args.get("topics_considered") or []
        if len(reasoning) < 100:
            # Reject — but DO NOT mark finalized. The agent must try again with longer reasoning.
            self._store_transition({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "rejected_finalize_run",
                "reason": "overall_reasoning_too_short",
                "payload": shape_transition_payload(
                    outcome="tool_error",
                    tool_name="finalize_run",
                    error=f"overall_reasoning must be >=100 chars; got {len(reasoning)}",
                ),
                "model": self.model,
            })
            return {
                "tool_call_id": call["id"],
                "tool": "finalize_run",
                "outcome": "rejected_too_short",
                "error": f"overall_reasoning must be >=100 chars; got {len(reasoning)}",
            }
        # Do not silently finalize with open drafts. Nonterminal drafts (drafting /
        # needs_revision / valid / blocked_for_submit) must be resolved first — unless
        # the agent explicitly acknowledges them, in which case they stay durable and
        # are recovered by the next run.
        pending = self._nonterminal_draft_summary(context)
        if pending and args.get("acknowledge_pending_drafts") is not True:
            self._store_transition({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "rejected_finalize_run",
                "reason": "nonterminal_drafts_pending",
                "payload": shape_transition_payload(
                    outcome="tool_error",
                    tool_name="finalize_run",
                    error=f"{len(pending)} draft(s) not terminal: {', '.join(pending)}",
                    extra={"pending_drafts": pending},
                ),
                "model": self.model,
            })
            return {
                "tool_call_id": call["id"],
                "tool": "finalize_run",
                "outcome": "rejected_pending_drafts",
                "error": f"{len(pending)} draft(s) not terminal: {', '.join(pending)}",
                "pending_drafts": pending,
                "hint": "Submit, abandon (with fallback_action), or mark needs_review each open draft before finalizing; or set acknowledge_pending_drafts=true to leave them for cross-run recovery.",
            }
        # Accept — capture into context for the run loop to detect + into transitions for audit.
        context["finalize"] = {
            "overall_reasoning": reasoning,
            "topics_considered": list(topics_considered),
        }
        self._store_transition({
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "action": "finalize_run",
            "reason": reasoning[:500],
            "payload": shape_transition_payload(
                outcome="accepted",
                tool_name="finalize_run",
                extra={
                    "overall_reasoning": reasoning,
                    "topics_considered": list(topics_considered),
                },
            ),
            "model": self.model,
        })
        return {
            "tool_call_id": call["id"],
            "tool": "finalize_run",
            "outcome": "accepted",
            "action": "finalize_run",
        }

    def _dispatch_create_topic_tool(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call["input"]

        # Normalize blocks before source_id derivation (T6: structured blocks path)
        normalized_blocks: List[Dict[str, Any]] = []
        if args.get("blocks"):
            try:
                normalized_blocks = normalize_document_blocks(
                    {"blocks": args["blocks"]},
                    topic_source_message_ids=None,
                )
            except (ValueError, TypeError):
                normalized_blocks = []

        # Derive topic-level source_ids: args source_message_ids + union from blocks
        source_ids = self._unique_ids(args.get("source_message_ids") or [])
        if normalized_blocks:
            block_sids = collect_document_source_ids(normalized_blocks)
            for sid in block_sids:
                if sid not in source_ids:
                    source_ids.append(sid)

        # Pre‑compute canonical_key early — T7 validation rejections need it
        canonical_key = canonicalize_proposed_key(args.get("proposed_key"), args.get("headline") or "")

        # BLOCKER 1: semantic per-run dedup. Compaction drops old tool_call_ids,
        # so the model may re-issue a create for a key already created this run
        # with a FRESH tool_call_id. Idempotency keyed only by tool_call_id would
        # treat that as new → duplicate row + duplicate publish. Guard on the
        # normalized canonical_key against this run's created topics.
        #
        # Only a TRUE duplicate of the SAME create state is replayed. The normal
        # watch→post editorial flow (WATCH a key, then POST it later in the same
        # run) is a legitimate state transition and must be allowed to proceed —
        # the post path upserts the existing watched topic. A post→watch downgrade
        # is not a real transition and is replayed like a duplicate.
        existing_created = (context.get("created_topic_keys") or {}).get(str(canonical_key)) if canonical_key else None
        if existing_created is not None:
            target_state = "watching" if call["name"] == "watch_topic" else "posted"
            existing_state = existing_created.get("state")
            watch_to_post = existing_state == "watching" and target_state == "posted"
            if not watch_to_post:
                replay_action = {
                    "post_topic": "post_topic",
                    "post_simple_topic": "post_simple",
                    "post_sectioned_topic": "post_sectioned",
                    "watch_topic": "watch",
                }.get(call["name"], call["name"])
                logger.info(
                    "TopicEditor semantic replay skipped: run_id=%s tool=%s canonical_key=%s topic_id=%s (created this run)",
                    context.get("run_id"),
                    call["name"],
                    canonical_key,
                    existing_created.get("topic_id"),
                )
                return {
                    "tool_call_id": call.get("id"),
                    "tool": call["name"],
                    "outcome": "idempotent_replay",
                    "action": replay_action,
                    "topic_id": existing_created.get("topic_id"),
                    "canonical_key": canonical_key,
                    "error": "semantic_replay_created_this_run",
                }
            # watch→post: fall through to the normal post path (upsert transitions
            # the existing watched topic). The collision scan below skips the
            # same-key created topic so it does not self-collide.

        # ── T7: Build merged resolved-message map (window + archive) ──────
        resolved_by_id: Dict[str, Dict[str, Any]] = {}
        for msg in context.get("messages") or []:
            mid = str(msg.get("message_id"))
            if mid and mid not in resolved_by_id:
                resolved_by_id[mid] = msg

        # Fill gaps from archive resolver (get_topic_editor_source_messages,
        # limit=50 – separate from the 10‑message read-tool cap).
        missing_ids = [sid for sid in source_ids if sid not in resolved_by_id]
        if missing_ids:
            try:
                archive_rows = self.db.get_topic_editor_source_messages(
                    message_ids=missing_ids,
                    guild_id=context.get("guild_id"),
                    environment=self.environment,
                )
            except Exception:
                archive_rows = []
            for row in (archive_rows or []):
                mid = str(row.get("message_id"))
                if mid and mid not in resolved_by_id:
                    resolved_by_id[mid] = row

        # ── T7: Validate block-level source_message_ids ────────────────────
        if normalized_blocks:
            for block in normalized_blocks:
                block_sids = block_source_ids(block)
                for sid in block_sids:
                    if sid not in resolved_by_id:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="unresolved_block_source_message",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "unresolved_message_id": sid,
                                "block_type": block.get("type"),
                                "block_title": block.get("title"),
                            },
                        )

                # ── T7: Validate block-level media_refs ────────────────────
                for ref in block_media_refs(block):
                    ref_mid = ref["message_id"]
                    ref_msg = resolved_by_id.get(ref_mid)
                    if not ref_msg:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="unresolved_media_ref",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "media_ref": ref,
                                "block_type": block.get("type"),
                            },
                        )
                    if ref_mid not in block_sids:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="invalid_media_ref",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "media_ref": ref,
                                "block_type": block.get("type"),
                                "error": (
                                    f"media_ref message_id {ref_mid!r} is not "
                                    "in block source_message_ids"
                                ),
                            },
                        )

                    kind = ref["kind"]
                    idx = ref["index"]
                    if kind == "attachment":
                        atts = self._normalize_attachment_list(
                            ref_msg.get("attachments")
                        )
                        if idx < 0 or idx >= len(atts):
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"attachment index {idx} out of range "
                                        f"(message has {len(atts)} attachments)"
                                    ),
                                },
                            )
                        url = atts[idx].get("url") or atts[idx].get("proxy_url")
                        if not url:
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"attachment at index {idx} has "
                                        f"no url / proxy_url"
                                    ),
                                },
                            )
                    elif kind == "embed":
                        embs = self._normalize_attachment_list(
                            ref_msg.get("embeds")
                        )
                        if idx < 0 or idx >= len(embs):
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"embed index {idx} out of range "
                                        f"(message has {len(embs)} embeds)"
                                    ),
                                },
                            )
                        # Discord embeds may have url at top level or
                        # inside a thumbnail / image sub-object.
                        url = (
                            embs[idx].get("url")
                            or (embs[idx].get("thumbnail") or {}).get("url")
                            or (embs[idx].get("image") or {}).get("url")
                        )
                        if not url:
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"embed at index {idx} has no url"
                                    ),
                                },
                            )
                    elif kind == "external":
                        # Validate external ref: source message must exist and
                        # extract_external_urls(ref_msg)[index] must exist and
                        # be source-domain safelisted. No resolver call here.
                        external_urls = extract_external_urls(ref_msg)
                        if idx < 0 or idx >= len(external_urls):
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"external index {idx} out of range "
                                        f"(message has {len(external_urls)} "
                                        f"external URLs)"
                                    ),
                                },
                            )
                        ext_entry = external_urls[idx]
                        if ext_entry.get("platform_policy") == "unknown":
                            return self._reject_create_tool(
                                call,
                                context,
                                action="rejected_post_sectioned",
                                reason="invalid_media_ref",
                                canonical_key=canonical_key,
                                source_message_ids=source_ids,
                                extra={
                                    "media_ref": ref,
                                    "block_type": block.get("type"),
                                    "error": (
                                        f"external ref domain "
                                        f"\"{ext_entry.get('domain')}\" "
                                        f"not safelisted"
                                    ),
                                },
                            )
                        # No URL resolution here — external refs are resolved
                        # lazily at publish time.
                    else:
                        return self._reject_create_tool(
                            call,
                            context,
                            action="rejected_post_sectioned",
                            reason="invalid_media_ref",
                            canonical_key=canonical_key,
                            source_message_ids=source_ids,
                            extra={
                                "media_ref": ref,
                                "block_type": block.get("type"),
                                "error": f"unknown media_ref kind: {kind!r}",
                            },
                        )

        # ── Rebuild source_messages / source_authors from merged set ─────
        source_messages = [
            resolved_by_id[sid] for sid in source_ids if sid in resolved_by_id
        ]
        source_authors = self._source_authors(source_messages)
        if normalized_blocks:
            normalized_blocks = self._attach_default_media_refs_to_blocks(
                normalized_blocks,
                resolved_by_id,
            )
            if call["name"] in ("post_sectioned_topic", "post_topic"):
                args["blocks"] = normalized_blocks

        action_by_tool = {
            "post_topic": "post_topic",
            "post_simple_topic": "post_simple",
            "post_sectioned_topic": "post_sectioned",
            "watch_topic": "watch",
        }
        state = "watching" if call["name"] == "watch_topic" else "posted"
        simple_media_items = args.get("media") or []
        simple_body = str(args.get("body") or "")
        if (
            call["name"] == "post_simple_topic"
            and (
                bool(simple_media_items)
                or re.search(r"\b\d{15,25}:(?:attachment|embed|external):\d+\b", simple_body)
            )
        ):
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_simple",
                reason="post_simple_cannot_attach_media_use_post_sectioned_topic",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
                extra={
                    "media_count": len(simple_media_items),
                    "has_raw_media_ref": bool(
                        re.search(r"\b\d{15,25}:(?:attachment|embed|external):\d+\b", simple_body)
                    ),
                },
            )
        if call["name"] == "post_simple_topic" and (len(source_ids) >= 3 or len(set(source_authors)) >= 2):
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_simple",
                reason="post_simple_requires_single_author_and_one_or_two_sources",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
                extra={"distinct_author_count": len(set(source_authors)), "source_count": len(source_ids)},
            )

        # Unified post_topic requires blocks. Legacy sectioned calls still
        # accept either sections or normalized blocks.
        sections = args.get("sections") or []
        has_sections = bool(sections and any(isinstance(s, dict) for s in sections))
        has_blocks = bool(normalized_blocks)
        if call["name"] == "post_topic" and not has_blocks:
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_topic",
                reason="post_topic_requires_blocks",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
            )
        if call["name"] == "post_sectioned_topic" and not has_sections and not has_blocks:
            return self._reject_create_tool(
                call,
                context,
                action="rejected_post_sectioned",
                reason="post_sectioned_requires_sections_or_blocks",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
            )

        collisions = detect_topic_collisions(
            proposed_canonical_key=canonical_key,
            headline=args.get("headline") or "",
            source_authors=source_authors,
            existing_topics=self._collision_scan_topics(context, canonical_key),
        )
        unresolved = unresolved_collisions(collisions, args.get("override_collisions") or [])
        if unresolved:
            rejected_action = {
                "post_topic": "rejected_post_topic",
                "post_simple_topic": "rejected_post_simple",
                "post_sectioned_topic": "rejected_post_sectioned",
                "watch_topic": "rejected_watch",
            }[call["name"]]
            return self._reject_create_tool(
                call,
                context,
                action=rejected_action,
                reason="topic_collision",
                canonical_key=canonical_key,
                source_message_ids=source_ids,
                collisions=unresolved,
            )

        # A source may be published in only one topic per run. This stops a
        # generation from appearing in both a generation_showcase and a news
        # post (or twice across two topics) in the same run. Runs after collision
        # detection so canonical-key collisions keep their existing precedence.
        if call["name"] != "watch_topic":
            already_published = context.get("published_source_ids") or set()
            overlaps = [str(sid) for sid in source_ids if str(sid) in already_published]
            if overlaps:
                rejected_action = {
                    "post_topic": "rejected_post_topic",
                    "post_simple_topic": "rejected_post_simple",
                    "post_sectioned_topic": "rejected_post_sectioned",
                }[call["name"]]
                return self._reject_create_tool(
                    call,
                    context,
                    action=rejected_action,
                    reason="source_already_published_this_run",
                    canonical_key=canonical_key,
                    source_message_ids=source_ids,
                    extra={
                        "overlapping_source_message_ids": overlaps,
                        "guidance": (
                            "Each source may be published in only one topic per run. "
                            "A generation already featured in a showcase or news topic "
                            "cannot be cited again in another post."
                        ),
                    },
                )

        revisit_at = None
        if state == "watching":
            revisit_at = parse_optional_datetime(args.get("revisit_when"))

        topic = self.db.upsert_topic({
            "guild_id": context["guild_id"],
            "canonical_key": canonical_key,
            "display_slug": args.get("proposed_key"),
            "state": state,
            "headline": args.get("headline"),
            "summary": self._summary_for_tool(call["name"], args),
            "source_authors": source_authors,
            "parent_topic_id": args.get("parent_topic_id"),
            "publication_status": "pending" if state == "posted" else None,
            "revisit_at": revisit_at,
            "source_message_ids": source_ids,
        }, environment=self.environment)
        topic_id = topic.get("topic_id") if topic else None
        if not topic_id:
            # The topic was not durably created (upsert returned nothing). Report a
            # rejection rather than lying to the agent — otherwise submit_draft marks
            # the draft submitted with topic_id=None and the publish step silently
            # skips it. A rejected_* transition does not trip the per-run idempotency
            # unique index, so the agent may re-attempt with a fresh id.
            self._store_transition({
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "rejected_topic_upsert",
                "reason": "topic_upsert_returned_no_topic_id",
                "payload": shape_transition_payload(
                    outcome="rejected",
                    tool_name=call["name"],
                    canonical_key=canonical_key,
                    proposed_key=args.get("proposed_key"),
                    error="upsert_topic returned no topic_id; topic was not created",
                ),
                "model": self.model,
            })
            return {
                "tool_call_id": call["id"],
                "tool": call["name"],
                "outcome": "rejected",
                "action": "rejected_topic_upsert",
                "error": "topic_upsert_returned_no_topic_id",
                "canonical_key": canonical_key,
            }

        topic.setdefault("source_message_ids", source_ids)
        topic.setdefault("run_id", context.get("run_id"))
        # Register every create (watching + posted) for semantic per-run dedup.
        context.setdefault("created_topic_keys", {})[str(canonical_key)] = topic
        if state == "posted":
            context.setdefault("created_topics", []).append(topic)
            context.setdefault("published_source_ids", set()).update(
                str(sid) for sid in source_ids
            )
            # Once a generation is published (in any post), its auto-shortlist
            # watcher's job is done — close it so it is not re-considered and
            # re-featured in later runs. Exclude the topic just posted, so
            # directly promoting a watcher (watch→post of the shortlist key)
            # does not immediately discard itself.
            self._close_shortlist_watchers(
                context, source_ids, exclude_topic_id=topic_id
            )
        for message_id in source_ids:
            self.db.add_topic_source({
                "topic_id": topic_id,
                "message_id": message_id,
                "guild_id": context["guild_id"],
                "run_id": context["run_id"],
            }, environment=self.environment)
        self.db.upsert_topic_alias({
            "topic_id": topic_id,
            "alias_key": args.get("proposed_key") or canonical_key,
            "alias_kind": "proposed",
            "guild_id": context["guild_id"],
        }, environment=self.environment)
        self._store_transition({
            "topic_id": topic_id,
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "to_state": state,
            "action": action_by_tool[call["name"]],
            "reason": args.get("notes") or args.get("why_interesting"),
            "payload": shape_transition_payload(
                outcome="accepted",
                tool_name=call["name"],
                canonical_key=canonical_key,
                proposed_key=args.get("proposed_key"),
                source_message_ids=source_ids,
                extra={
                    "blocks": normalized_blocks or None,
                } if normalized_blocks else None,
            ),
            "model": self.model,
        })
        override_rows = build_override_transitions(
            run_id=context["run_id"],
            environment=self.environment,
            guild_id=context["guild_id"],
            topic_id=str(topic_id),
            override_collisions=args.get("override_collisions") or [],
            tool_call_id=call["id"],
            model=self.model,
        )
        for row in override_rows:
            self._store_transition(row)
        tool_call_id = call.get("id")
        if tool_call_id:
            key = (str(context.get("run_id")), str(tool_call_id))
            context.setdefault("accepted_tool_call_ids", set()).add(key)
            if args.get("override_collisions"):
                context.setdefault("override_retry_consumed_tool_call_ids", set()).add(key)
        return {
            "tool_call_id": call["id"],
            "tool": call["name"],
            "outcome": "accepted",
            "topic_id": topic_id,
            "action": action_by_tool[call["name"]],
            "override_count": len(override_rows),
        }

    def _attach_default_media_refs_to_blocks(
        self,
        blocks: Sequence[Dict[str, Any]],
        resolved_by_id: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Add one source-local media ref per block when the agent omitted it.

        The fallback is deliberately conservative: it only uses media from the
        block's own cited sources, preserves explicit refs, and adds at most one
        media item per block.
        """
        hydrated: List[Dict[str, Any]] = []
        for block in blocks:
            next_block = dict(block)
            if not block_media_refs(next_block):
                default_ref = self._first_available_media_ref_for_sources(
                    block_source_ids(next_block),
                    resolved_by_id,
                )
                if default_ref:
                    next_block["media_refs"] = [default_ref]
            hydrated.append(next_block)
        return hydrated

    def _first_available_media_ref_for_sources(
        self,
        source_ids: Sequence[str],
        resolved_by_id: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for sid in source_ids:
            message = resolved_by_id.get(str(sid)) or {}
            attachments = self._normalize_attachment_list(message.get("attachments"))
            for idx, attachment in enumerate(attachments):
                if isinstance(attachment, dict) and (attachment.get("url") or attachment.get("proxy_url")):
                    return {"message_id": str(sid), "kind": "attachment", "index": idx}
            embeds = self._normalize_attachment_list(message.get("embeds"))
            for idx, embed in enumerate(embeds):
                if not isinstance(embed, dict):
                    continue
                url = (
                    embed.get("url")
                    or (embed.get("thumbnail") or {}).get("url")
                    or (embed.get("thumbnail") or {}).get("proxy_url")
                    or (embed.get("image") or {}).get("url")
                    or (embed.get("image") or {}).get("proxy_url")
                    or (embed.get("video") or {}).get("url")
                    or (embed.get("video") or {}).get("proxy_url")
                )
                if url:
                    return {"message_id": str(sid), "kind": "embed", "index": idx}
        return None

    def _auto_shortlist_media_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        known_topics: Sequence[Dict[str, Any]],
        *,
        run_id: str,
        guild_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Create watching topics for reaction-qualified media posts.

        This replaces the old direct top-creations auto-post path. The shortlist
        is intentionally conservative and idempotent: one topic per source
        message, skipped forever once the operator/agent discards it.
        """
        threshold = max(0, int(self.media_shortlist_min_reactions or 0))
        limit = max(0, int(self.media_shortlist_limit or 0))
        if threshold <= 0 or limit <= 0:
            return []

        existing_by_key = {
            str(topic.get("canonical_key") or ""): topic
            for topic in known_topics or []
            if topic.get("canonical_key")
        }

        candidates: List[Dict[str, Any]] = []
        for message in messages or []:
            message_id = str(message.get("message_id") or "").strip()
            if not message_id:
                continue
            canonical_key = self._media_shortlist_key(message_id)
            existing = existing_by_key.get(canonical_key)
            if existing:
                # Posted/watching/discarded all mean this message was already
                # intentionally handled. Discarded is the explicit ignore path.
                continue
            channel_name = str(message.get("channel_name") or "").lower()
            if "nsfw" in channel_name:
                continue
            reaction_count = self._message_reaction_count(message)
            if reaction_count < threshold:
                continue
            media_ref = self._first_available_media_ref_for_sources(
                [message_id],
                {message_id: message},
            )
            if not media_ref:
                continue
            candidates.append({
                "message": message,
                "message_id": message_id,
                "canonical_key": canonical_key,
                "reaction_count": reaction_count,
                "media_ref": media_ref,
            })

        # Sources live in `topic_sources`, not on the topics rows. A generation
        # already referenced by ANY known topic (news, another showcase, or an old
        # shortlist watcher outside the 300-topic fetch) is covered and must not be
        # re-surfaced as a fresh candidate by the re-scan.
        if candidates:
            covered = set()
            lookup = getattr(self.db, "get_topic_source_message_ids", None)
            if lookup is not None:
                try:
                    covered = set(
                        lookup(
                            [item["message_id"] for item in candidates],
                            guild_id=guild_id,
                            environment=self.environment,
                        )
                        or set()
                    )
                except Exception:
                    covered = set()
            if covered:
                candidates = [
                    item for item in candidates
                    if item["message_id"] not in covered
                ]

        candidates.sort(
            key=lambda item: (
                int(item.get("reaction_count") or 0),
                str((item.get("message") or {}).get("created_at") or ""),
                str(item.get("message_id") or ""),
            ),
            reverse=True,
        )

        shortlisted: List[Dict[str, Any]] = []
        for item in candidates[:limit]:
            message = item["message"]
            message_id = item["message_id"]
            author = self._author_name(message) or "community member"
            reaction_count = int(item["reaction_count"] or 0)
            headline = f"Shortlisted media from {author} ({reaction_count} reactions)"
            reason = (
                f"Auto-shortlisted because source message {message_id} has media "
                f"and {reaction_count} reactions. Investigate context and media "
                "understanding before deciding whether to publish, keep watching, or discard. "
                "This is a candidate for a \"generations to admire\" showcase."
            )
            revisit_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
            topic = self.db.upsert_topic({
                "guild_id": guild_id,
                "canonical_key": item["canonical_key"],
                "display_slug": item["canonical_key"],
                "state": "watching",
                "headline": headline,
                "summary": {
                    "why_interesting": reason,
                    "auto_shortlist": True,
                    "shortlist_kind": "media_reaction_threshold",
                    "reaction_count": reaction_count,
                    "source_message_id": message_id,
                    "media_refs": [item["media_ref"]],
                    "suggested_actions": [
                        "get_message_context",
                        "get_reply_chain when it is a reply",
                        "search_messages for related posts by the author/tool",
                        "understand_image or understand_video",
                        "draft a generation_showcase for this candidate",
                        "post_topic, watch_topic/update sources, or discard_topic",
                    ],
                },
                "source_authors": [author] if author else [],
                "parent_topic_id": None,
                "publication_status": None,
                "revisit_at": revisit_at,
                "source_message_ids": [message_id],
            }, environment=self.environment)
            topic_id = topic.get("topic_id") if topic else None
            if topic_id:
                topic.setdefault("source_message_ids", [message_id])
                self.db.add_topic_source({
                    "topic_id": topic_id,
                    "message_id": message_id,
                    "guild_id": guild_id,
                    "run_id": run_id,
                }, environment=self.environment)
                self.db.upsert_topic_alias({
                    "topic_id": topic_id,
                    "alias_key": item["canonical_key"],
                    "alias_kind": "proposed",
                    "guild_id": guild_id,
                }, environment=self.environment)
            self._store_transition({
                "topic_id": topic_id,
                "run_id": run_id,
                "guild_id": guild_id,
                "tool_call_id": f"auto-media-shortlist:{message_id}",
                "to_state": "watching",
                "action": "watch",
                "reason": reason,
                "payload": shape_transition_payload(
                    outcome="accepted",
                    tool_name="auto_media_shortlist",
                    canonical_key=item["canonical_key"],
                    proposed_key=item["canonical_key"],
                    source_message_ids=[message_id],
                    extra={
                        "reaction_count": reaction_count,
                        "media_ref": item["media_ref"],
                    },
                ),
                "model": self.model,
            })
            shortlisted.append({
                "status": "added",
                "topic": topic,
                "topic_id": topic_id,
                "message_id": message_id,
                "reaction_count": reaction_count,
                "headline": headline,
                "reason": reason,
                "media_ref": item["media_ref"],
            })

        if shortlisted:
            logger.info(
                "TopicEditor auto-shortlisted %s media item(s) at >=%s reactions: %s",
                len(shortlisted),
                threshold,
                [
                    {
                        "message_id": item.get("message_id"),
                        "reaction_count": item.get("reaction_count"),
                        "topic_id": item.get("topic_id"),
                    }
                    for item in shortlisted
                ],
            )
        return shortlisted

    def _shortlist_scan_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        guild_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        """Union the reaction re-scan lookback window into the shortlist scan.

        The checkpoint scan only sees messages newer than the last checkpoint, so
        a generation that crosses the reaction threshold AFTER its first hour
        never becomes a candidate. Merge recent messages over the lookback window
        in, deduping by message_id, and return the list to feed
        ``_auto_shortlist_media_messages``. When the shortlist is disabled the
        original list is returned unchanged.

        NOTE: ``DatabaseHandler.get_archived_messages_for_window`` is synchronous
        (it wraps the async storage call in a thread), matching every other
        topic-editor DB call — do not ``await`` it.
        """
        scan = list(messages or [])
        if not (self.media_shortlist_min_reactions > 0 and self.media_shortlist_limit > 0):
            return scan
        if self.media_shortlist_lookback_hours <= 0:
            return scan
        try:
            recent = self.db.get_archived_messages_for_window(
                guild_id=guild_id,
                start=(
                    datetime.now(timezone.utc)
                    - timedelta(hours=self.media_shortlist_lookback_hours)
                ).isoformat(),
                end=datetime.now(timezone.utc).isoformat(),
                limit=5000,
                channel_ids=None,
                exclude_author_ids=self._excluded_author_ids(),
            )
        except Exception:
            recent = []
        if not recent:
            return scan
        by_id = {str(m.get("message_id")): m for m in scan}
        for m in recent:
            by_id.setdefault(str(m.get("message_id")), m)
        return list(by_id.values())

    def _close_shortlist_watchers(
        self,
        context: Dict[str, Any],
        source_ids: Sequence[str],
        *,
        exclude_topic_id: Optional[str] = None,
    ) -> None:
        """Close auto-shortlist watchers whose source just got published.

        A media message is shortlisted as a watching topic under the
        ``media-shortlist-{message_id}`` canonical key. Once that message is
        published in ANY topic this run (showcase or news), the watcher is
        transitioned to ``discarded`` so it is not re-presented and re-featured
        in later runs.
        """
        if not source_ids:
            return
        target_keys = {self._media_shortlist_key(sid) for sid in source_ids}
        for topic in context.get("active_topics") or []:
            key = str(topic.get("canonical_key") or "")
            if key not in target_keys or topic.get("state") != "watching":
                continue
            topic_id = topic.get("topic_id")
            if not topic_id:
                continue
            if exclude_topic_id and str(topic_id) == str(exclude_topic_id):
                continue
            self.db.update_topic(
                topic_id,
                {"state": "discarded", "guild_id": context.get("guild_id")},
                guild_id=context.get("guild_id"),
                environment=self.environment,
            )
            self._store_transition({
                "topic_id": topic_id,
                "run_id": context.get("run_id"),
                "guild_id": context.get("guild_id"),
                "tool_call_id": f"auto-close-shortlist:{key}",
                "to_state": "discarded",
                "action": "discard",
                "reason": (
                    f"Shortlisted source was published this run ({key}); "
                    "closing the watcher."
                ),
                "payload": shape_transition_payload(
                    outcome="accepted",
                    tool_name="auto_media_shortlist_close",
                    canonical_key=key,
                    source_message_ids=[
                        sid for sid in source_ids
                        if self._media_shortlist_key(sid) == key
                    ],
                    extra={"closed_by": "published_topic_source"},
                ),
                "model": self.model,
            })
            topic["state"] = "discarded"

    @staticmethod
    def _media_shortlist_key(message_id: str) -> str:
        return f"media-shortlist-{_slugify(str(message_id))}"

    @staticmethod
    def _message_reaction_count(message: Dict[str, Any]) -> int:
        for key in ("reaction_count", "unique_reactor_count", "reactions"):
            value = message.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                return int(value)
            try:
                return int(str(value))
            except (TypeError, ValueError):
                pass
        reactors = message.get("reactors") or []
        if isinstance(reactors, str):
            try:
                reactors = json.loads(reactors)
            except json.JSONDecodeError:
                reactors = []
        if isinstance(reactors, list):
            return len(reactors)
        return 0

    def _dispatch_update_sources(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call["input"]
        topic = self._find_topic(context["active_topics"], args.get("topic_id"))
        source_ids = self._unique_ids(args.get("new_source_message_ids") or [])
        if not topic or not source_ids:
            error = "topic_not_found" if not topic else "no_source_messages"
            self._store_transition({
                "topic_id": args.get("topic_id"),
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "update_sources",
                "reason": args.get("note") or error,
                "payload": shape_transition_payload(
                    outcome="tool_error",
                    tool_name=call["name"],
                    source_message_ids=source_ids,
                    error=error,
                ),
                "model": self.model,
            })
            return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "tool_error", "action": "update_sources", "error": error}
        for message_id in source_ids:
            self.db.add_topic_source({
                "topic_id": args.get("topic_id"),
                "message_id": message_id,
                "guild_id": context["guild_id"],
                "run_id": context["run_id"],
            }, environment=self.environment)
        self._store_transition({
            "topic_id": args.get("topic_id"),
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "action": "update_sources",
            "reason": args.get("note"),
            "payload": shape_transition_payload(outcome="accepted", tool_name=call["name"], source_message_ids=source_ids),
            "model": self.model,
        })
        return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "accepted", "action": "update_sources"}

    def _dispatch_discard(self, call: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        args = call["input"]
        topic = self._find_topic(context["active_topics"], args.get("topic_id"))
        if not topic or topic.get("state") != "watching":
            error = "topic_not_found" if not topic else "topic_not_watching"
            self._store_transition({
                "topic_id": args.get("topic_id"),
                "run_id": context["run_id"],
                "guild_id": context["guild_id"],
                "tool_call_id": call["id"],
                "action": "discard",
                "reason": args.get("reason") or error,
                "payload": shape_transition_payload(outcome="tool_error", tool_name=call["name"], error=error),
                "model": self.model,
            })
            return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "tool_error", "action": "discard", "error": error}
        self.db.update_topic(args.get("topic_id"), {"state": "discarded", "guild_id": context["guild_id"]}, guild_id=context["guild_id"], environment=self.environment)
        self._invalidate_social_drafts_for_topic(args.get('topic_id'), reason='topic_discarded')
        self._store_transition({
            "topic_id": args.get("topic_id"),
            "run_id": context["run_id"],
            "guild_id": context["guild_id"],
            "tool_call_id": call["id"],
            "to_state": "discarded",
            "action": "discard",
            "reason": args.get("reason"),
            "payload": shape_transition_payload(outcome="accepted", tool_name=call["name"]),
            "model": self.model,
        })
        return {"tool_call_id": call["id"], "tool": call["name"], "outcome": "accepted", "action": "discard"}

    def _reject_create_tool(
        self,
        call: Dict[str, Any],
        context: Dict[str, Any],
        *,
        action: str,
        reason: str,
        canonical_key: str,
        source_message_ids: Sequence[str],
        collisions: Optional[Sequence[Collision]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        args = call["input"]
        if reason == "topic_collision" and call.get("id"):
            context.setdefault("collision_rejected_tool_call_ids", set()).add(
                (str(context.get("run_id")), str(call["id"]))
            )
        self._store_transition(build_rejected_transition(
            run_id=context["run_id"],
            environment=self.environment,
            guild_id=context["guild_id"],
            action=action,
            tool_call_id=call["id"],
            reason=reason,
            payload=shape_transition_payload(
                outcome="tool_error",
                tool_name=call["name"],
                canonical_key=canonical_key,
                proposed_key=args.get("proposed_key"),
                source_message_ids=source_message_ids,
                collisions=collisions,
                error=reason,
                extra=extra,
            ),
            model=self.model,
        ))
        return {"tool_call_id": call["id"], "tool": call["name"], "outcome": action, "action": action, "error": reason}

    def _store_transition(self, transition: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        transition = dict(transition)
        if transition.get("action") in {"finalize_run", "rejected_finalize_run"}:
            payload = transition.get("payload") if isinstance(transition.get("payload"), dict) else {}
            payload = {**payload, "original_action": transition.get("action")}
            transition["payload"] = payload
            transition["action"] = "observation"
        try:
            return self.db.store_topic_transition(transition, environment=self.environment)
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                return None
            raise

    def _populate_idempotent_results(self, calls: Sequence[Dict[str, Any]], context: Dict[str, Any]) -> None:
        tool_call_ids = [
            str(call.get("id"))
            for call in calls or []
            if call.get("name") in WRITE_TOOL_NAMES and call.get("id")
        ]
        if not tool_call_ids:
            return
        getter = getattr(self.db, "get_topic_transitions_by_tool_call_ids", None)
        if not getter:
            return
        existing = getter(context.get("run_id"), tool_call_ids, environment=self.environment) or {}
        cache = context.setdefault("idempotent_results", {})
        for tool_call_id, row in existing.items():
            if row:
                cache[str(tool_call_id)] = row

    def _idempotent_replay_outcome(self, call: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tool_call_id = call.get("id")
        if not tool_call_id or call.get("name") not in WRITE_TOOL_NAMES:
            return None
        row = (context.get("idempotent_results") or {}).get(str(tool_call_id))
        if not row:
            return None
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        action = row.get("action")
        if (
            payload.get("tool_name") == "finalize_run"
            and payload.get("outcome") == "accepted"
        ):
            context["finalize"] = {
                "overall_reasoning": payload.get("overall_reasoning") or row.get("reason") or "",
                "topics_considered": list(payload.get("topics_considered") or []),
            }
        return {
            "tool_call_id": str(tool_call_id),
            "tool": call.get("name"),
            "outcome": "idempotent_replay",
            "action": payload.get("original_action") or action,
            "topic_id": payload.get("topic_id") or row.get("topic_id"),
        }

    def _is_idempotent_replay(self, call: Dict[str, Any], context: Dict[str, Any]) -> bool:
        tool_call_id = call.get("id")
        if not tool_call_id:
            return False
        key = (str(context.get("run_id")), str(tool_call_id))
        seen = context.setdefault("seen_tool_call_ids", set())
        if key in seen:
            if self._is_collision_override_retry(call, context, key):
                return False
            return True
        seen.add(key)
        return False

    def _is_collision_override_retry(
        self,
        call: Dict[str, Any],
        context: Dict[str, Any],
        key: tuple[str, str],
    ) -> bool:
        if call.get("name") not in {"post_topic", "post_simple_topic", "post_sectioned_topic", "watch_topic"}:
            return False
        if not (call.get("input") or {}).get("override_collisions"):
            return False
        if key not in context.get("collision_rejected_tool_call_ids", set()):
            return False
        if key in context.get("accepted_tool_call_ids", set()):
            return False
        if key in context.get("override_retry_consumed_tool_call_ids", set()):
            return False
        return True

    def _topics_with_aliases(
        self,
        topics: Sequence[Dict[str, Any]],
        aliases: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        aliases_by_topic: Dict[str, List[str]] = {}
        for alias in aliases or []:
            if self.environment is not None and alias.get("environment") not in {None, self.environment}:
                continue
            topic_id = alias.get("topic_id")
            alias_key = alias.get("alias_key")
            if topic_id and alias_key:
                aliases_by_topic.setdefault(str(topic_id), []).append(str(alias_key))
        enriched = []
        for topic in topics or []:
            row = dict(topic)
            topic_id = str(row.get("topic_id"))
            merged_aliases = list(row.get("aliases") or [])
            for alias_key in aliases_by_topic.get(topic_id, []):
                if alias_key not in merged_aliases:
                    merged_aliases.append(alias_key)
            row["aliases"] = merged_aliases
            enriched.append(row)
        return enriched

    def _collision_scan_topics(self, context: Dict[str, Any], canonical_key: str) -> List[Dict[str, Any]]:
        """Topics considered for collision/similarity detection.

        Includes the original ``active_topics`` (with aliases) AND topics created
        earlier THIS run, so a fresh-ID create whose key only prefix-matches or is
        similar to a topic created this run is flagged as a collision too (e.g.
        ``alpha`` then ``alpha-v2``). The topic matching this exact canonical_key
        is excluded so the same-key watch→post transition does not self-collide.
        """
        existing = list(self._topics_with_aliases(context["active_topics"], context.get("aliases") or []))
        seen_ids = {str(topic.get("topic_id")) for topic in existing if topic.get("topic_id")}
        for created in (context.get("created_topic_keys") or {}).values():
            if not isinstance(created, dict):
                continue
            if str(created.get("canonical_key") or "") == str(canonical_key or ""):
                # Same-key create (watch→post transition) is handled by the post
                # path's upsert, not by collision rejection.
                continue
            topic_id = created.get("topic_id")
            if topic_id and str(topic_id) in seen_ids:
                continue
            if topic_id:
                seen_ids.add(str(topic_id))
            existing.append(created)
        return existing

    def _find_topic(self, topics: Sequence[Dict[str, Any]], topic_id: Any) -> Optional[Dict[str, Any]]:
        if topic_id is None:
            return None
        wanted = str(topic_id)
        return next((topic for topic in topics or [] if str(topic.get("topic_id")) == wanted), None)

    def _unique_ids(self, ids: Sequence[Any]) -> List[str]:
        unique: List[str] = []
        for item in ids or []:
            value = str(item)
            if value and value not in unique:
                unique.append(value)
        return unique

    def _cap_text(self, value: Any, limit: int) -> str:
        text = str(value or "").strip()
        return text[:limit]

    def _run_updates(
        self,
        *,
        checkpoint_before: Dict[str, Any],
        checkpoint_after: Dict[str, Any],
        messages: Sequence[Dict[str, Any]],
        tool_calls: Sequence[Dict[str, Any]],
        started: float,
        metadata: Dict[str, Any],
        accepted_count: int = 0,
        rejected_count: int = 0,
        override_count: int = 0,
        observation_count: int = 0,
        published_count: int = 0,
        failed_publish_count: int = 0,
        skipped_reason: Optional[str] = None,
        status: str = "completed",
    ) -> Dict[str, Any]:
        usage = metadata.get("usage") or {}
        metadata_cost = metadata.get("cumulative_cost_usd")
        return {
            "status": status,
            "guild_id": self._resolve_guild_id(),
            "checkpoint_before": checkpoint_before,
            "checkpoint_after": checkpoint_after,
            "source_message_count": len(messages),
            "tool_call_count": len(tool_calls),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "override_count": override_count,
            "observation_count": observation_count,
            "published_count": published_count,
            "failed_publish_count": failed_publish_count,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
            "cache_miss_tokens": usage.get("cache_miss_tokens", 0),
            "cache_hit_pct": self._cache_hit_pct(usage),
            "cost_usd": metadata_cost if isinstance(metadata_cost, (int, float)) else self._estimate_cost_usd(usage),
            "estimated_cache_adjusted_cost_usd": metadata.get("estimated_cache_adjusted_cost_usd"),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "model": self.model,
            "publishing_enabled": self.publishing_enabled,
            "trace_channel_id": self.trace_channel_id,
            "skipped_reason": skipped_reason,
            "metadata": metadata,
        }

    def _persist_run_progress(
        self,
        run_id: str,
        *,
        checkpoint_before: Dict[str, Any],
        checkpoint_after: Dict[str, Any],
        messages: Sequence[Dict[str, Any]],
        tool_calls: Sequence[Dict[str, Any]],
        outcomes: Sequence[Dict[str, Any]],
        started: float,
        metadata: Dict[str, Any],
        accepted_count: int = 0,
        rejected_count: int = 0,
        override_count: int = 0,
        observation_count: int = 0,
    ) -> None:
        updater = getattr(self.db, "update_topic_editor_run", None)
        if not callable(updater):
            return
        try:
            updates = self._run_updates(
                checkpoint_before=checkpoint_before,
                checkpoint_after=checkpoint_after,
                messages=messages,
                tool_calls=tool_calls,
                started=started,
                metadata=metadata,
                accepted_count=accepted_count,
                rejected_count=rejected_count,
                override_count=override_count,
                observation_count=observation_count,
                status="running",
            )
            updates["metadata"] = {**(updates.get("metadata") or {}), "outcomes": list(outcomes or [])}
            updater(
                run_id,
                updates,
                guild_id=self._resolve_guild_id(),
                environment=self.environment,
            )
        except Exception as exc:
            logger.warning("TopicEditor progress persistence failed: %s", exc, exc_info=True)

    def _dedupe_created_topics(self, topics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the first occurrence of each canonical_key/topic_id before publishing."""
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for topic in topics or []:
            key = str(topic.get("canonical_key") or "") or str(topic.get("topic_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(topic)
        return out

    async def _publish_created_topics(self, topics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        publishable = [
            topic for topic in topics or []
            if topic.get("topic_id") and topic.get("state") == "posted"
        ]
        results: List[Dict[str, Any]] = []
        for topic in publishable:
            results.append(await self._publish_topic(topic))
        return results

    @staticmethod
    def _derive_publish_status_from_outbox(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Derive the aggregate publish status + ordered discord_message_ids from
        outbox rows. sent = all sent; partial = some sent; failed = none sent."""
        rows = [r for r in rows or []]
        rows.sort(key=lambda r: int(r.get("unit_index") or 0))
        sent_ids: List[int] = []
        had_failure = False
        for row in rows:
            status = str(row.get("status") or "pending")
            if status == "sent":
                mid = row.get("discord_message_id")
                if mid is not None:
                    sent_ids.append(int(mid))
            elif status == "failed":
                had_failure = True
        status = (
            "sent" if sent_ids and not had_failure
            else "partial" if sent_ids else "failed"
        )
        return {"status": status, "discord_message_ids": sent_ids}

    def _mark_outbox_units(
        self,
        topic_id: str,
        guild_id: int,
        unit_indices: Sequence[int],
        statuses: Sequence[Dict[str, Any]],
    ) -> None:
        """Update outbox rows at the given unit indices after a send.

        ``unit_indices`` are the original send-unit indices (not compressed batch
        positions), so a skipped already-sent unit never mislabels another row.
        ``statuses``: one dict per unit: {'status', 'discord_message_id'?, 'error'?}.
        Best-effort — a failed outbox write only loses recoverability, not the
        already-sent message."""
        if not hasattr(self.db, "update_topic_publish_outbox"):
            return
        for unit_index, updates in zip(unit_indices, statuses):
            try:
                self.db.update_topic_publish_outbox(
                    topic_id,
                    unit_index,
                    {k: v for k, v in updates.items() if v is not None},
                    environment=self.environment,
                    guild_id=guild_id,
                )
            except Exception as exc:
                logger.error("TopicEditor outbox row update failed: topic_id=%s unit=%s error=%s", topic_id, unit_index, exc)

    @staticmethod
    def _outbox_unit_should_send(row: Dict[str, Any]) -> bool:
        """A unit should be (re)sent only if it is pending/failed AND has no
        recorded discord_message_id (a crash between Discord-accept and row write
        leaves the id unset; if set, treat as already sent)."""
        status = str(row.get("status") or "pending")
        if status == "sent":
            return False
        if row.get("discord_message_id") is not None:
            return False
        return status in ("pending", "failed", "sending")

    async def _publish_pending_topics(self) -> List[Dict[str, Any]]:
        """Retry topics with pending/failed/partial publication via the outbox.

        Sends only pending/failed units (reconcile before resend: 'sent' rows and
        rows with a discord_message_id are never resent). Skips topics with no
        outbox rows (legacy — do not blind-resend)."""
        if not hasattr(self.db, "get_pending_topic_publish_outbox_topics"):
            return []
        results: List[Dict[str, Any]] = []
        try:
            pending = self.db.get_pending_topic_publish_outbox_topics(
                environment=self.environment,
                limit=self._env_int("TOPIC_EDITOR_RETRY_TOPICS_PER_RUN", 5),
            )
        except Exception as exc:
            logger.error("TopicEditor publish-backlog fetch failed: %s", exc)
            return results
        for row in pending or []:
            topic_id = str(row.get("topic_id") or "")
            if not topic_id:
                continue
            try:
                topic = self.db.get_topic(topic_id, environment=self.environment)
            except Exception as exc:
                logger.error("TopicEditor publish-backlog topic fetch failed: topic_id=%s error=%s", topic_id, exc)
                continue
            if not topic:
                continue
            try:
                outbox_rows = self.db.get_topic_publish_outbox(topic_id, environment=self.environment)
            except Exception as exc:
                logger.error("TopicEditor publish-backlog outbox fetch failed: topic_id=%s error=%s", topic_id, exc)
                continue
            if not outbox_rows:
                logger.warning("TopicEditor publish-backlog topic has no outbox rows (legacy), skipping topic_id=%s", topic_id)
                continue
            results.append(await self._publish_topic(topic))
        return results

    def _classify_publish_media_failure(
        self,
        unit: Dict[str, Any],
        error: Any,
        trace: Optional[Dict[str, Any]] = None,
    ) -> str:
        text = " ".join(
            str(part or "").lower()
            for part in (
                error,
                (trace or {}).get("status"),
                (trace or {}).get("action"),
                (trace or {}).get("detail"),
            )
        )
        if any(marker in text for marker in ("expired", "http 403", "http 404", " 403", " 404", "not found", "forbidden", "unauthorized")):
            return "media_url_expired"
        if any(marker in text for marker in ("too large", "413", "payload", "entity too large", "file size", "maximum file")):
            return "media_payload_too_large"
        if unit.get("send_kind") == "file" and any(marker in text for marker in ("resolve", "resolver", "download_failed", "download http", "empty media url")):
            return "media_resolver_failed"
        if any(marker in text for marker in ("send failed", "file_send_failed", "file_batch_send_failed", "fallback_url")):
            return "media_send_failed"
        return "media_unknown_failure"

    def _record_publish_media_failure(
        self,
        diagnostics: Dict[str, Any],
        unit: Dict[str, Any],
        error: Any,
        *,
        trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        if unit.get("send_kind") not in {"file_url", "file", "url"}:
            return
        reason_code = self._classify_publish_media_failure(unit, error, trace)
        reason_codes = diagnostics.setdefault("reason_codes", [])
        if reason_code not in reason_codes:
            reason_codes.append(reason_code)
        ref = unit.get("ref") or {}
        entry = {
            "reason_code": reason_code,
            "message_id": str(ref.get("message_id") or "") or None,
            "source_message_id": str(ref.get("message_id") or "") or None,
            "media_ref": ref or None,
            "error": str(error or "")[:300],
            "trace": trace or None,
        }
        failures = diagnostics.setdefault("media_failures", [])
        if entry not in failures:
            failures.append(entry)

    def _attach_publish_diagnostics_to_matching_draft(
        self,
        topic_id: str,
        status: str,
        diagnostics: Dict[str, Any],
    ) -> None:
        for state in self.topic_editor_drafts.values():
            if str(state.get("topic_id") or "") != str(topic_id):
                continue
            state["publish_result"] = {"topic_id": topic_id, "status": status}
            state["publish_diagnostics"] = diagnostics
            self._persist_draft_state(state, state, include_topic_id=True)

    def _fire_social_handoff(
        self,
        topic: Dict[str, Any],
        channel_id: Any,
        status: str,
        sent_ids: List[int],
        source_message_ids: Any,
        publish_diagnostics: Any,
    ) -> None:
        if status not in {'sent', 'partial'}:
            return
        if self.bot is None:
            return
        if getattr(self.bot, 'live_update_social_service', None) is None:
            return
        try:
            topic_id = str(topic.get('topic_id'))
            guild_id = int(topic.get('guild_id') or self._resolve_guild_id() or 0)
            channel_id_int = int(channel_id or 0)
            topic_summary_data: Dict[str, Any] = {
                'title': topic.get('headline') or topic.get('proposed_key'),
                # sent_ids[0] is first Discord message sent, not necessarily the consumer's documented topic root
                'subTopics': [],
            }
            if sent_ids:
                topic_summary_data['message_id'] = str(sent_ids[0])
            if channel_id_int:
                topic_summary_data['channel_id'] = str(channel_id_int)
            # chain fields (vendor='codex', depth='high', with_feedback=True, deepseek_provider='direct') intentionally rely on dataclass defaults
            payload = LiveUpdateHandoffPayload(
                topic_id=topic_id,
                guild_id=guild_id,
                channel_id=channel_id_int,
                platform='twitter',
                action='post',
                status=status,
                source_metadata={
                    'cog': 'topic_editor',
                    'environment': self.environment,
                    'source_message_ids': list(source_message_ids or []),
                    'publish_diagnostics': publish_diagnostics or {},
                },
                topic_summary_data=topic_summary_data,
            )
            asyncio.create_task(
                self.bot.live_update_social_service.handle_live_update_publish_results(payload)
            )
        except Exception:
            logger.exception('social handoff scheduling failed', exc_info=True)

    def _invalidate_social_drafts_for_topic(self, topic_id: str, reason: str) -> None:
        """Expire every open (pending, not terminal) social draft for *topic_id*.

        Designed to be called from state-change paths (discard, admin delete) so
        downstream reviewers never see stale ``pending`` runs.  Callers are
        responsible for supplying a clear *reason* string for audit logging.
        """
        try:
            rows = self.db.list_open_social_runs(
                environment=self.environment,
                topic_id=topic_id,
                limit=50,
            )
            for row in (rows or []):
                self.db.update_live_update_social_run(
                    run_id=row['run_id'],
                    environment=self.environment,
                    approval_state='expired',
                )
        except Exception:
            logger.exception(
                'social-draft invalidation failed: topic=%s reason=%s',
                topic_id,
                reason,
            )

    def _format_trace_messages(
        self,
        run_id: str,
        updates: Dict[str, Any],
        outcomes: Sequence[Dict[str, Any]],
        publish_results: Sequence[Dict[str, Any]],
    ) -> List[str]:
        usage = {
            "input_tokens": updates.get("input_tokens", 0),
            "output_tokens": updates.get("output_tokens", 0),
            "cache_hit_pct": updates.get("cache_hit_pct"),
            "cost_usd": updates.get("cost_usd"),
            "model": updates.get("model"),
        }
        outcome_counts: Dict[str, int] = {}
        tool_lines: List[str] = []
        rejection_lines: List[str] = []
        override_lines: List[str] = []
        observation_lines: List[str] = []
        for outcome in outcomes or []:
            outcome_name = str(outcome.get("outcome") or "unknown")
            outcome_counts[outcome_name] = outcome_counts.get(outcome_name, 0) + 1
            tool = outcome.get("tool")
            action = outcome.get("action")
            suffix = f" -> {action}" if action and action != tool else ""
            tool_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome_name}{suffix}")
            if outcome_name.startswith("rejected") or outcome_name == "tool_error":
                rejection_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome.get('error') or outcome_name}")
            if int(outcome.get("override_count") or 0):
                override_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome.get('override_count')} override(s)")
            if action == "observation":
                observation_lines.append(f"- `{tool}` `{outcome.get('tool_call_id')}`: {outcome_name}")

        publish_lines: List[str] = []
        for result in publish_results or []:
            status = result.get("status")
            topic_id = result.get("topic_id")
            media_counts = result.get("source_media_counts") or {}
            publish_lines.append(
                f"- `{topic_id}`: {status} "
                f"media_sent={result.get('media_count', 0)} "
                f"flat={result.get('flat_message_count', len(result.get('discord_message_ids') or []))} "
                f"source_resolvable_media={media_counts.get('resolvable_media', 0)}"
            )
            for message in result.get("messages") or []:
                publish_lines.append(_indent_trace_block(f"would-publish: {message}"))

        lines = [
            f"**Topic editor trace** `{run_id}`",
            f"env={self.environment} publishing={'ON' if self.publishing_enabled else 'OFF'} trigger-state={updates.get('status') or 'completed'}",
            f"sources={updates.get('source_message_count', 0)} tools={updates.get('tool_call_count', 0)} accepted={updates.get('accepted_count', 0)} rejected={updates.get('rejected_count', 0)} overrides={updates.get('override_count', 0)} observations={updates.get('observation_count', 0)}",
            f"published={updates.get('published_count', 0)} failed_publish={updates.get('failed_publish_count', 0)} latency_ms={updates.get('latency_ms', 0)}",
            f"tokens in/out={usage['input_tokens']}/{usage['output_tokens']} cost_usd={usage['cost_usd']} model={usage['model']}"
            + (f" cache_hit_pct={usage['cache_hit_pct']}" if usage["cache_hit_pct"] is not None else ""),
            f"outcomes={outcome_counts}",
        ]
        metadata = updates.get("metadata") or {}
        if metadata.get("forced_close"):
            lines.insert(1, f"⚠ FORCE-CLOSED reason={metadata.get('forced_close_reason') or 'unknown'}")
        if metadata.get("cumulative_tokens") is not None:
            compaction_note = ""
            compactions = metadata.get("compactions") or []
            if compactions:
                compaction_note = f" compactions={len(compactions)}"
            lines.insert(
                5,
                f"cumulative_tokens={metadata.get('cumulative_tokens')} cumulative_cost_usd={metadata.get('cumulative_cost_usd')}{compaction_note}",
            )
        shortlist_lines = [
            (
                f"- `{item.get('message_id')}` -> `{item.get('topic_id')}` "
                f"reactions={item.get('reaction_count')} status={item.get('status')}"
            )
            for item in metadata.get("auto_shortlisted_media") or []
        ]
        sections = [
            ("Auto-shortlisted media", shortlist_lines),
            ("Tool calls", tool_lines),
            ("Rejections", rejection_lines),
            ("Overrides", override_lines),
            ("Observations", observation_lines),
            ("Publishing", publish_lines),
        ]
        for title, section_lines in sections:
            if section_lines:
                lines.extend(["", f"**{title}**", *section_lines])
        return _chunk_trace_lines(lines)

    async def _emit_trace(
        self,
        messages: Sequence[str],
        *,
        run_id: str | None = None,
        updates: Dict[str, Any] | None = None,
        outcomes: Sequence[Dict[str, Any]] | None = None,
        publish_results: Sequence[Dict[str, Any]] | None = None,
    ) -> None:
        if not self.trace_channel_id:
            return
        channel = await self._resolve_discord_channel(int(self.trace_channel_id))
        if channel is None:
            return
        embed = self._build_trace_embed(run_id, updates or {}, outcomes or [], publish_results or [])
        if embed is not None:
            try:
                await channel.send(embed=embed)
            except Exception:
                # Fallback to plain-text if embed send fails (e.g. perms missing for embeds).
                for message in messages or []:
                    await channel.send(message)
            return
        for message in messages or []:
            await channel.send(message)

    def _build_trace_embed(
        self,
        run_id: str | None,
        updates: Dict[str, Any],
        outcomes: Sequence[Dict[str, Any]],
        publish_results: Sequence[Dict[str, Any]],
    ) -> Optional[discord.Embed]:
        if run_id is None and not updates:
            return None

        status = updates.get("status") or "completed"
        published_count = updates.get("published_count", 0) or 0
        failed_publish = updates.get("failed_publish_count", 0) or 0
        rejected_count = updates.get("rejected_count", 0) or 0
        accepted_count = updates.get("accepted_count", 0) or 0
        skipped_reason = updates.get("skipped_reason")
        metadata = updates.get("metadata") or {}

        # Pick a color based on what happened.
        if status == "failed" or failed_publish:
            color = 0xE74C3C  # red
        elif published_count:
            color = 0x2ECC71  # green
        elif rejected_count or skipped_reason:
            color = 0xF1C40F  # amber
        elif accepted_count:
            color = 0x3498DB  # blue
        else:
            color = 0x808080  # grey (nothing-to-post idle)

        publishing_label = "ON" if self.publishing_enabled else "OFF"
        title = f"Topic editor · {self.environment} · publishing {publishing_label}"
        description_parts = [f"run `{run_id or 'unknown'}`"]
        if metadata.get("forced_close"):
            description_parts.append(f"⚠ FORCE-CLOSED: `{metadata.get('forced_close_reason') or 'unknown'}`")
        if skipped_reason:
            description_parts.append(f"skipped: `{skipped_reason}`")
        description = " · ".join(description_parts)

        embed = discord.Embed(title=title, description=description, color=color)

        # --- field: editorial reasoning (the agent's overall narrative) ---
        reasoning = metadata.get("reasoning")
        if reasoning:
            reasoning_text = reasoning if len(reasoning) <= 1024 else reasoning[:1000] + "…"
            embed.add_field(name="editorial reasoning", value=reasoning_text, inline=False)
        else:
            embed.add_field(name="editorial reasoning", value="_(agent did not provide reasoning)_", inline=False)

        # --- field: summary ---
        summary_lines = [
            f"sources: `{updates.get('source_message_count', 0)}`",
            f"tool calls: `{updates.get('tool_call_count', 0)}`",
            f"accepted: `{accepted_count}` · rejected: `{rejected_count}`",
            f"overrides: `{updates.get('override_count', 0)}` · observations: `{updates.get('observation_count', 0)}`",
            f"published: `{published_count}` · failed_publish: `{failed_publish}`",
            f"auto-shortlisted media: `{len(metadata.get('auto_shortlisted_media') or [])}`",
        ]
        embed.add_field(name="summary", value="\n".join(summary_lines)[:1024], inline=True)

        # --- field: model & cost ---
        # Report the real billed cost (cache-adjusted) rather than the
        # conservative raw estimate; fall back to the raw number only when the
        # adjusted figure couldn't be computed (see
        # `_estimate_cache_adjusted_cost_usd`). `cost_usd` is already the
        # cumulative figure, so a separate "cumulative cost" line is dropped as
        # redundant.
        cache_adjusted = metadata.get("estimated_cache_adjusted_cost_usd")
        cost = cache_adjusted if isinstance(cache_adjusted, (int, float)) else updates.get("cost_usd")
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
        model_lines = [
            f"model: `{updates.get('model') or 'n/a'}`",
            f"tokens in/out: `{updates.get('input_tokens', 0)}` / `{updates.get('output_tokens', 0)}`",
            f"cumulative tokens: `{metadata.get('cumulative_tokens', updates.get('input_tokens', 0) + updates.get('output_tokens', 0))}`",
            f"cost: `{cost_str}`",
            f"latency: `{updates.get('latency_ms', 0)} ms`",
        ]
        cache_hit_pct = updates.get("cache_hit_pct")
        if isinstance(cache_hit_pct, (int, float)) and cache_hit_pct >= 0:
            model_lines.append(
                f"cache hit: `{cache_hit_pct:.1f}%` "
                f"(hit=`{updates.get('cache_hit_tokens', 0)}` miss=`{updates.get('cache_miss_tokens', 0)}`)"
            )
        embed.add_field(name="model & cost", value="\n".join(model_lines)[:1024], inline=True)

        # --- field: input context (time range + channel coverage) ---
        input_lines: List[str] = []
        time_range = self._format_metadata_time_range(metadata)
        if time_range:
            input_lines.append(f"window: {time_range}")
        channel_summary = self._format_metadata_channel_coverage(metadata)
        if channel_summary:
            input_lines.append(f"channels: {channel_summary}")
        active_topics_count = metadata.get("active_topics_count")
        if active_topics_count is not None:
            input_lines.append(f"active topics: `{active_topics_count}`")
        if input_lines:
            embed.add_field(name="input context", value="\n".join(input_lines)[:1024], inline=False)

        # --- field: tool calls with input snippets ---
        input_by_id = {call.get("id"): call.get("input") or {} for call in (metadata.get("tool_calls") or [])}
        if outcomes:
            tool_lines: List[str] = []
            for outcome in outcomes:
                tool = outcome.get("tool") or "?"
                action = outcome.get("action")
                outcome_name = str(outcome.get("outcome") or "unknown")
                suffix = f" → {action}" if action and action != tool else ""
                tool_input = input_by_id.get(outcome.get("tool_call_id")) or {}
                input_hint = self._format_tool_input_hint(tool, tool_input)
                hint_suffix = f" — {input_hint}" if input_hint else ""
                tool_lines.append(f"`{tool}` · {outcome_name}{suffix}{hint_suffix}")
            value = "\n".join(tool_lines)
            if len(value) > 1024:
                value = value[:1000] + "\n…"
            embed.add_field(name=f"tool calls ({len(outcomes)})", value=value, inline=False)

        # --- field: rejections (if any) ---
        rejection_lines = []
        for outcome in outcomes or []:
            outcome_name = str(outcome.get("outcome") or "")
            if outcome_name.startswith("rejected") or outcome_name == "tool_error":
                tool = outcome.get("tool") or "?"
                err = outcome.get("error") or outcome_name

                # Enriched metadata from _dispatch_understand_media (T3 primary path)
                msg_id = outcome.get("message_id")
                ch_id = outcome.get("channel_id")
                g_id = outcome.get("guild_id")
                media = outcome.get("media_url")

                # Secondary fallback: try tool input for message_id
                if not msg_id:
                    tool_input = input_by_id.get(outcome.get("tool_call_id")) or {}
                    msg_id = tool_input.get("message_id")

                # Guild fallback from updates context
                if not g_id:
                    g_id = updates.get("guild_id")

                # Build line(s): enriched format when metadata present, else legacy
                has_jump = msg_id and ch_id and g_id
                if has_jump or media:
                    lines_for_this = []
                    if has_jump:
                        lines_for_this.append(
                            f"jump: https://discord.com/channels/{g_id}/{ch_id}/{msg_id}"
                        )
                    if media:
                        lines_for_this.append(f"media_url: {media}")
                    lines_for_this.append(f"`{tool}`: {err}")
                    rejection_lines.append("\n".join(lines_for_this))
                else:
                    rejection_lines.append(f"`{tool}`: {err}")
        if rejection_lines:
            value = "\n".join(rejection_lines)
            if len(value) > 1024:
                value = value[:1000] + "\n…"
            embed.add_field(name=f"rejections ({len(rejection_lines)})", value=value, inline=False)

        # --- field: overrides (if any) ---
        override_lines = []
        for outcome in outcomes or []:
            if int(outcome.get("override_count") or 0):
                override_lines.append(
                    f"`{outcome.get('tool') or '?'}` · {outcome.get('override_count')} override(s)"
                )
        if override_lines:
            embed.add_field(name=f"overrides ({len(override_lines)})", value="\n".join(override_lines)[:1024], inline=False)

        # --- field: publishing (if any) ---
        if publish_results:
            publish_lines = []
            for result in publish_results:
                topic_id = result.get("topic_id") or "?"
                media_counts = result.get("source_media_counts") or {}
                publish_lines.append(
                    f"`{topic_id}` · {result.get('status') or '?'} · "
                    f"media_sent `{result.get('media_count', 0)}` · "
                    f"source_media `{media_counts.get('resolvable_media', 0)}` · "
                    f"messages `{result.get('flat_message_count', len(result.get('discord_message_ids') or []))}`"
                )
            embed.add_field(name=f"publishing ({len(publish_results)})", value="\n".join(publish_lines)[:1024], inline=False)

        trigger_label = metadata.get("trigger") or updates.get("trigger") or "n/a"
        embed.set_footer(text=f"trigger: {trigger_label} · env: {self.environment}")
        return embed

    def _format_metadata_time_range(self, metadata: Dict[str, Any]) -> str:
        timestamps = metadata.get("source_message_timestamps") or []
        if not timestamps:
            return ""
        try:
            earliest = min(timestamps)
            latest = max(timestamps)
            return f"`{earliest}` → `{latest}`"
        except Exception:
            return ""

    def _format_metadata_channel_coverage(self, metadata: Dict[str, Any]) -> str:
        channel_counts = metadata.get("source_channel_counts") or {}
        if not channel_counts:
            return ""
        try:
            ranked = sorted(channel_counts.items(), key=lambda item: item[1], reverse=True)[:5]
            return " ".join(f"`#{name}`·{count}" for name, count in ranked)
        except Exception:
            return ""

    def _format_tool_input_hint(self, tool: str, tool_input: Dict[str, Any]) -> str:
        if not tool_input:
            return ""
        if tool == "search_topics":
            query = tool_input.get("query")
            if query:
                snippet = str(query)[:80]
                return f"query=`{snippet}`"
        if tool == "search_messages":
            query = tool_input.get("query")
            scope = tool_input.get("scope") or "window"
            parts = []
            if query:
                parts.append(f"query=`{str(query)[:60]}`")
            parts.append(f"scope={scope}")
            return " ".join(parts)
        if tool == "get_author_profile":
            author_id = tool_input.get("author_id")
            if author_id is not None:
                return f"author=`{author_id}`"
        if tool == "get_message_context":
            ids = tool_input.get("message_ids") or []
            if ids:
                preview = ", ".join(str(x) for x in list(ids)[:3])
                more = "…" if len(ids) > 3 else ""
                return f"messages=`{preview}{more}`"
        if tool == "get_reply_chain":
            message_id = tool_input.get("message_id")
            if message_id:
                return f"message_id=`{str(message_id)[:40]}`"
        if tool in {"post_topic", "post_simple_topic", "post_sectioned_topic", "watch_topic"}:
            slug = tool_input.get("proposed_key")
            if slug:
                return f"key=`{slug}`"
        return ""

    async def _publish_topic(self, topic: Dict[str, Any]) -> Dict[str, Any]:
        topic_id = str(topic.get("topic_id"))
        guild_id = topic.get("guild_id") or self._resolve_guild_id()
        current_attempts = int(topic.get("publication_attempts") or 0)

        # Determine whether this is a structured-document topic
        summary = topic.get("summary") or {}
        has_blocks = isinstance(summary, dict) and bool(summary.get("blocks"))

        if has_blocks:
            # --- Structured block-by-block publishing ---
            # Collect all source IDs and hydrate metadata for jump URLs + media
            blocks = normalize_topic_document(topic)
            all_source_ids = collect_document_source_ids(blocks)
            source_metadata: Dict[str, Dict[str, Any]] = {}
            if all_source_ids:
                try:
                    rows = self.db.get_topic_editor_source_messages(
                        message_ids=all_source_ids,
                        guild_id=guild_id,
                        environment=self.environment,
                    )
                except Exception:
                    rows = []
                for row in rows:
                    source_metadata[str(row.get("message_id"))] = row

            # Fetch the guild's channel-name → id map so the renderer can
            # convert `#channelname` text into clickable `<#id>` mentions.
            channel_lookup: Dict[str, int] = {}
            try:
                if hasattr(self.db, "get_channel_name_lookup"):
                    channel_lookup = self.db.get_channel_name_lookup(guild_id=guild_id) or {}
            except Exception as exc:
                logger.warning("channel name lookup failed for guild=%s: %s", guild_id, exc)

            # Build ordered publish units (text + media interleaved)
            publish_units = render_topic_publish_units(
                topic,
                source_metadata=source_metadata,
                channel_lookup=channel_lookup,
            )
            publish_diagnostics: Dict[str, Any] = {
                "renderer_safety_chunking_used": any(
                    unit.get("kind", "text") == "text"
                    and len(str(unit.get("content") or "")) > 2000
                    for unit in publish_units
                ),
                "reason_codes": [],
                "media_failures": [],
            }
            if publish_diagnostics["renderer_safety_chunking_used"]:
                publish_diagnostics["reason_codes"].append("renderer_safety_chunking_used")

            # Flatten units through paragraph-aware chunking (used for suppressed
            # mode and fallback text display — NOT the primary send path).
            flat_messages: List[str] = []
            media_indices: Set[int] = set()
            _media_count = 0
            for idx, unit in enumerate(publish_units):
                if unit.get("kind") in ("media", "external"):
                    media_indices.add(len(flat_messages))
                    flat_messages.append(unit.get("url") or unit.get("fallback_url", ""))
                else:
                    for chunk in chunk_text_for_discord(unit["content"]):
                        flat_messages.append(chunk)
            media_source_counts = self._summarize_source_media_counts(source_metadata)
            logger.info(
                "TopicEditor publish plan: topic_id=%s structured=true blocks=%s sources=%s "
                "source_media=%s units=%s flat_messages=%s media_messages=%s publishing_enabled=%s",
                topic_id,
                len(blocks),
                len(all_source_ids),
                media_source_counts,
                len(publish_units),
                len(flat_messages),
                len(media_indices),
                self.publishing_enabled,
            )

            if not self.publishing_enabled:
                self.db.update_topic(
                    topic_id,
                    {
                        "guild_id": guild_id,
                        "publication_status": "suppressed",
                        "publication_error": None,
                    },
                    guild_id=guild_id,
                    environment=self.environment,
                )
                return {
                    "topic_id": topic_id,
                    "status": "suppressed",
                    "publish_units": publish_units,
                    "flat_messages": flat_messages,
                    "media_indices": sorted(media_indices),
                    "source_media_counts": media_source_counts,
                    "publish_diagnostics": publish_diagnostics,
                }

            # Build send units from publish_units.
            # Send-unit model:
            #   {'send_kind': 'text',     'content': str}
            #   {'send_kind': 'file_url', 'source_url': str, 'filename': str,
            #    'fallback_url': str, 'ref': dict}
            #   {'send_kind': 'file',     'file_path': str, 'filename': str,
            #    'fallback_url': str, 'ref': dict, 'trace': str}
            send_units: List[Dict[str, Any]] = []
            _build_send_units(publish_units, send_units, source_metadata)

            # Send block-by-block via send units
            sent_ids: List[int] = []
            error: Optional[str] = None
            had_failure = False
            publish_traces: List[Dict[str, str]] = []
            channel_id = self._resolve_live_channel_id(guild_id)

            # Durable per-unit outbox: record every send unit BEFORE the loop so a
            # crash mid-send leaves a recoverable trace. Units already sent in a prior
            # attempt (preserved from the existing outbox) are written as sent and
            # skipped in the loop — reconcile-before-resend prevents duplicates.
            preserve_sent: Dict[int, int] = {}
            if hasattr(self.db, "get_topic_publish_outbox"):
                try:
                    existing_outbox = self.db.get_topic_publish_outbox(topic_id, environment=self.environment)
                    for row in existing_outbox or []:
                        if str(row.get("status")) == "sent" and row.get("discord_message_id") is not None:
                            preserve_sent[int(row.get("unit_index") or 0)] = int(row["discord_message_id"])
                except Exception as exc:
                    logger.error("TopicEditor outbox read failed: topic_id=%s error=%s", topic_id, exc)
            if hasattr(self.db, "insert_topic_publish_outbox"):
                try:
                    inserted = self.db.insert_topic_publish_outbox(
                        topic_id,
                        send_units,
                        environment=self.environment,
                        run_id=topic.get("run_id"),
                        guild_id=guild_id,
                        preserve_sent=preserve_sent,
                    )
                    if not inserted:
                        # Outbox durable-intent write failed. Proceed to send anyway
                        # (at-least-once — the message must not be silently lost), but
                        # flag it loudly so a retry knows reconcile may be imperfect.
                        logger.error("TopicEditor outbox insert returned 0 rows: topic_id=%s — duplicate-reconcile may be degraded", topic_id)
                        publish_diagnostics.setdefault("reason_codes", []).append("outbox_insert_failed")
                except Exception as exc:
                    logger.error("TopicEditor outbox insert failed: topic_id=%s error=%s", topic_id, exc)
                    publish_diagnostics.setdefault("reason_codes", []).append("outbox_insert_failed")
            skip_send: Set[int] = set(preserve_sent.keys())
            for skipped_idx, skipped_mid in preserve_sent.items():
                sent_ids.append(skipped_mid)

            try:
                channel = await self._resolve_discord_channel(channel_id)
                if channel is None:
                    raise RuntimeError(f"live_update_channel_not_found:{channel_id}")
                idx = 0
                while idx < len(send_units):
                    unit = send_units[idx]
                    if idx in skip_send:
                        # Already sent in a prior attempt — reconcile, don't resend.
                        idx += 1
                        continue
                    if unit.get("send_kind") == "file_url":
                        # Keep (original_index, unit) pairs so outbox rows are updated by
                        # their true index even when already-sent units are skipped and
                        # the batch compresses (skipping would otherwise shift indices).
                        batch_orig_indices = [idx]
                        batch = [unit]
                        idx += 1
                        while (
                            idx < len(send_units)
                            and send_units[idx].get("send_kind") == "file_url"
                            and len(batch) < 10
                        ):
                            if idx in skip_send:
                                idx += 1
                                continue
                            batch_orig_indices.append(idx)
                            batch.append(send_units[idx])
                            idx += 1
                        # Durable intent: mark each batch unit as sending before the call.
                        self._mark_outbox_units(
                            topic_id, guild_id, batch_orig_indices,
                            [{"status": "sending"} for _ in batch],
                        )
                        batch_sent_ids, batch_error, batch_traces = await self._send_file_url_batch(
                            channel,
                            batch,
                            topic_id,
                        )
                        # _send_file_url_batch returns one entry per unit, aligned by
                        # index: all units delivered in the single batch message share
                        # that one message id, units sent via per-URL fallback get their
                        # own id, and undelivered units are None. Mark each outbox row
                        # from that alignment — a 2+-file batch must not leave the 2nd
                        # file "failed" just because one message carried the whole batch.
                        self._mark_outbox_units(
                            topic_id, guild_id, batch_orig_indices,
                            [
                                {
                                    "status": "sent" if batch_sent_ids[i] is not None else "failed",
                                    "discord_message_id": batch_sent_ids[i],
                                    "error": batch_error,
                                }
                                for i in range(len(batch))
                            ],
                        )
                        for mid in batch_sent_ids:
                            if mid is not None and mid not in sent_ids:
                                sent_ids.append(mid)
                        publish_traces.extend(batch_traces)
                        if batch_error:
                            error = batch_error
                            had_failure = True
                            self._record_publish_media_failure(
                                publish_diagnostics,
                                batch[0],
                                batch_error,
                                trace=batch_traces[-1] if batch_traces else None,
                            )
                        if not any(batch_sent_ids):
                            had_failure = True
                            self._record_publish_media_failure(
                                publish_diagnostics,
                                batch[0],
                                batch_error or "media batch send returned no message id",
                                trace=batch_traces[-1] if batch_traces else None,
                            )
                        for trace in batch_traces:
                            if trace.get("status") in {"download_failed", "file_batch_send_failed"}:
                                self._record_publish_media_failure(
                                    publish_diagnostics,
                                    batch[0],
                                    trace.get("detail") or trace.get("status"),
                                    trace=trace,
                                )
                        continue

                    unit_outbox_index = idx
                    self._mark_outbox_units(
                        topic_id, guild_id, [unit_outbox_index], [{"status": "sending"}],
                    )
                    unit_sent_id, unit_error, unit_trace = await self._send_one_unit(
                        channel, unit, topic_id
                    )
                    self._mark_outbox_units(
                        topic_id, guild_id, [unit_outbox_index],
                        [{
                            "status": "sent" if unit_sent_id is not None else "failed",
                            "discord_message_id": unit_sent_id,
                            "error": unit_error,
                        }],
                    )
                    idx += 1
                    if unit_sent_id is not None:
                        sent_ids.append(unit_sent_id)
                    if unit_trace:
                        publish_traces.append(unit_trace)
                    if unit_error:
                        error = unit_error  # last error for top-level reporting
                        had_failure = True
                        self._record_publish_media_failure(
                            publish_diagnostics,
                            unit,
                            unit_error,
                            trace=unit_trace,
                        )
                    if unit_sent_id is None:
                        had_failure = True
                        self._record_publish_media_failure(
                            publish_diagnostics,
                            unit,
                            unit_error or "send returned no message id",
                            trace=unit_trace,
                        )
                    if unit_trace and unit_trace.get("status") in {"file_send_failed", "exception_fallback", "download_failed", "resolver_failed"}:
                        self._record_publish_media_failure(
                            publish_diagnostics,
                            unit,
                            unit_trace.get("detail") or unit_trace.get("status"),
                            trace=unit_trace,
                        )
            except Exception as exc:
                error = str(exc)
                had_failure = True

            status = (
                "sent" if sent_ids and not had_failure
                else "partial" if sent_ids else "failed"
            )
            if publish_traces:
                logger.info(
                    "TopicEditor publish traces: topic_id=%s traces=%s",
                    topic_id, publish_traces,
                )
            updates = {
                "guild_id": guild_id,
                "publication_status": status,
                "publication_error": error,
                "discord_message_ids": sent_ids,
                "publication_attempts": current_attempts + 1,
                "last_published_at": datetime.now(timezone.utc).isoformat() if sent_ids else None,
            }
            self.db.update_topic(topic_id, updates, guild_id=guild_id, environment=self.environment)
            self._attach_publish_diagnostics_to_matching_draft(topic_id, status, publish_diagnostics)
            if topic.get("run_id"):
                self._store_transition({
                    "topic_id": topic_id,
                    "run_id": topic.get("run_id"),
                    "guild_id": guild_id,
                    "action": f"publish_{status}",
                    "reason": error,
                    "payload": shape_transition_payload(
                        outcome=status,
                        tool_name="publish_topic",
                        source_message_ids=all_source_ids,
                        error=error,
                        extra={"publish_diagnostics": publish_diagnostics},
                    ),
                    "model": self.model,
                })
            self._fire_social_handoff(topic, channel_id, status, sent_ids, all_source_ids, publish_diagnostics)
            return {
                "topic_id": topic_id,
                "status": status,
                "discord_message_ids": sent_ids,
                "error": error,
                "media_count": len(media_indices),
                "flat_message_count": len(flat_messages),
                "source_media_counts": media_source_counts,
                "publish_diagnostics": publish_diagnostics,
            }

        # --- Legacy simple-topic path (no blocks) ---
        rendered_messages = render_topic(topic)
        if not self.publishing_enabled:
            self.db.update_topic(
                topic_id,
                {
                    "guild_id": guild_id,
                    "publication_status": "suppressed",
                    "publication_error": None,
                },
                guild_id=guild_id,
                environment=self.environment,
            )
            return {"topic_id": topic_id, "status": "suppressed", "messages": rendered_messages}

        sent_ids: List[int] = []
        error: Optional[str] = None
        channel_id = self._resolve_live_channel_id(guild_id)
        try:
            channel = await self._resolve_discord_channel(channel_id)
            if channel is None:
                raise RuntimeError(f"live_update_channel_not_found:{channel_id}")
            for message in rendered_messages:
                sent = await channel.send(message)
                message_id = getattr(sent, "id", None)
                if message_id is not None:
                    sent_ids.append(int(message_id))
        except Exception as exc:
            error = str(exc)

        status = (
            "sent" if sent_ids and len(sent_ids) == len(rendered_messages) and not error
            else "partial" if sent_ids else "failed"
        )
        updates = {
            "guild_id": guild_id,
            "publication_status": status,
            "publication_error": error,
            "discord_message_ids": sent_ids,
            "publication_attempts": current_attempts + 1,
            "last_published_at": datetime.now(timezone.utc).isoformat() if sent_ids else None,
        }
        self.db.update_topic(topic_id, updates, guild_id=guild_id, environment=self.environment)
        self._fire_social_handoff(topic, channel_id, status, sent_ids, topic.get('source_message_ids') or [], None)
        return {"topic_id": topic_id, "status": status, "discord_message_ids": sent_ids, "error": error}

    async def _send_one_unit(
        self,
        channel: Any,
        unit: Dict[str, Any],
        topic_id: str,
    ) -> Tuple[Optional[int], Optional[str], Optional[Dict[str, str]]]:
        """Send a single send-unit to a Discord channel.

        Returns (sent_id, error, trace_dict).
        ``sent_id`` is the Discord message ID on success, or None.
        ``error`` is an error string on failure, or None.
        ``trace_dict`` is a per-unit trace for external-media resolution steps.

        Send-kinds:
          - ``text``  → ``channel.send(content)``
          - ``url``   → ``channel.send(content)`` (URL as plain text)
          - ``file``  → lazily resolve external media then send
            :class:`discord.File` on success; fallback to URL text on failure.
        """
        from src.common.external_media import sanitise_url_for_logs

        send_kind = unit.get("send_kind", "text")
        trace: Optional[Dict[str, str]] = None

        try:
            if send_kind == "text":
                sent = await channel.send(unit["content"])
                mid = getattr(sent, "id", None)
                return (int(mid) if mid else None, None, None)

            if send_kind == "url":
                sent = await channel.send(unit["content"])
                mid = getattr(sent, "id", None)
                return (int(mid) if mid else None, None, None)

            if send_kind == "file":
                fallback_url = unit.get("fallback_url", "")
                ref = unit.get("ref", {})
                safe_url = sanitise_url_for_logs(fallback_url)

                # ---- lazy-resolve external media ----
                result = await self._resolve_external_for_publish(fallback_url, ref)
                outcome = result.outcome.value if hasattr(result, "outcome") else "unknown"

                if outcome in ("cache_hit", "downloaded") and result.file_path:
                    # Send as discord.File
                    try:
                        filename = os.path.basename(result.file_path)
                        with open(result.file_path, "rb") as fh:
                            discord_file = discord.File(fh, filename=filename)
                        sent = await channel.send(file=discord_file)
                        mid = getattr(sent, "id", None)
                        trace = {
                            "url": safe_url,
                            "status": outcome,
                            "action": "file_sent",
                        }
                        return (int(mid) if mid else None, None, trace)
                    except Exception as file_exc:
                        logger.warning(
                            "TopicEditor: discord.File send failed for %s, "
                            "falling back to URL: %s",
                            safe_url, file_exc,
                        )
                        trace = {
                            "url": safe_url,
                            "status": "file_send_failed",
                            "action": "fallback_url",
                            "detail": str(file_exc)[:200],
                        }
                        # Fall through to send URL text
                        sent = await channel.send(fallback_url)
                        mid = getattr(sent, "id", None)
                        return (int(mid) if mid else None, None, trace)

                # ---- any other outcome: send the fallback URL ----
                trace = {
                    "url": safe_url,
                    "status": outcome,
                    "action": "fallback_url",
                    "detail": getattr(result, "failure_reason", "") or "",
                }
                sent = await channel.send(fallback_url)
                mid = getattr(sent, "id", None)
                return (int(mid) if mid else None, None, trace)

            return (None, f"unknown send_kind: {send_kind}", None)

        except Exception as exc:
            # Last-resort: try the fallback URL if we still have one
            error_str = str(exc)
            if send_kind == "file":
                fallback_url = unit.get("fallback_url", "")
                if fallback_url:
                    try:
                        sent = await channel.send(fallback_url)
                        mid = getattr(sent, "id", None)
                        trace = {
                            "url": sanitise_url_for_logs(fallback_url),
                            "status": "exception_fallback",
                            "action": "fallback_url",
                            "detail": error_str[:200],
                        }
                        return (int(mid) if mid else None, None, trace)
                    except Exception:
                        pass
            return (None, error_str, trace)

    async def _send_file_url_batch(
        self,
        channel: Any,
        units: Sequence[Dict[str, Any]],
        topic_id: str,
    ) -> Tuple[List[Optional[int]], Optional[str], List[Dict[str, str]]]:
        """Download Discord-hosted media URLs and upload them as Discord files.

        Consecutive media units belong to the same document block, so this
        batches them into one Discord message when possible. If the batch upload
        fails, each media item falls back to its original URL so publishing can
        continue.

        Returns ``sent_ids`` aligned per unit (``len == len(units)``): the
        message id that delivered unit ``i``, or None if it was not delivered.
        A successful batch puts every unit in one message, so every entry then
        shares that single message id — the caller's per-unit outbox marking
        depends on this 1:1 alignment.
        """
        if not units:
            return [], None, []

        from src.common.external_media import sanitise_url_for_logs

        sent_ids: List[Optional[int]] = [None] * len(units)
        traces: List[Dict[str, str]] = []
        temp_paths: List[str] = []
        handles: List[Tuple[int, Any]] = []
        error: Optional[str] = None

        try:
            for idx, unit in enumerate(units):
                source_url = unit.get("source_url") or unit.get("fallback_url") or ""
                safe_url = sanitise_url_for_logs(source_url)
                try:
                    file_path, filename = await self._download_publish_media_url(source_url, unit)
                    temp_paths.append(file_path)
                    handle = open(file_path, "rb")
                    handles.append((idx, handle))
                    traces.append({
                        "url": safe_url,
                        "status": "downloaded",
                        "action": "queued_file_upload",
                        "filename": filename,
                    })
                except Exception as exc:
                    error = str(exc)
                    traces.append({
                        "url": safe_url,
                        "status": "download_failed",
                        "action": "fallback_url",
                        "detail": str(exc)[:200],
                    })
                    try:
                        sent = await channel.send(source_url)
                        mid = getattr(sent, "id", None)
                        if mid is not None:
                            sent_ids[idx] = int(mid)
                    except Exception as fallback_exc:
                        error = str(fallback_exc)
                        traces.append({
                            "url": safe_url,
                            "status": "fallback_url_send_failed",
                            "action": "fallback_failed",
                            "detail": str(fallback_exc)[:200],
                        })

            if not handles:
                return sent_ids, error, traces

            files = [
                discord.File(handle, filename=os.path.basename(getattr(handle, "name", "")) or None)
                for _, handle in handles
            ]
            try:
                sent = await channel.send(files=files)
                mid = getattr(sent, "id", None)
                if mid is not None:
                    # One message carries the whole batch — every downloaded unit shares it.
                    for idx, _ in handles:
                        sent_ids[idx] = int(mid)
                for trace in traces:
                    if trace.get("action") == "queued_file_upload":
                        trace["action"] = "files_sent"
            except Exception as exc:
                error = str(exc)
                logger.warning(
                    "TopicEditor: Discord file batch send failed for topic %s, falling back to URLs: %s",
                    topic_id,
                    exc,
                )
                for idx, unit in enumerate(units):
                    if sent_ids[idx] is not None:
                        # Already delivered via a per-unit fallback — don't double-post.
                        continue
                    source_url = unit.get("source_url") or unit.get("fallback_url") or ""
                    safe_url = sanitise_url_for_logs(source_url)
                    try:
                        sent = await channel.send(source_url)
                        mid = getattr(sent, "id", None)
                        if mid is not None:
                            sent_ids[idx] = int(mid)
                    except Exception as fallback_exc:
                        error = str(fallback_exc)
                        traces.append({
                            "url": safe_url,
                            "status": "fallback_url_send_failed",
                            "action": "fallback_failed",
                            "detail": str(fallback_exc)[:200],
                        })
                traces.append({
                    "status": "file_batch_send_failed",
                    "action": "fallback_url",
                    "detail": str(exc)[:200],
                })

            return sent_ids, error, traces
        finally:
            for handle in handles:
                try:
                    handle.close()
                except Exception:
                    pass
            for path in temp_paths:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.debug("TopicEditor: failed to delete temp media %s: %s", path, exc)

    async def _download_publish_media_url(
        self,
        source_url: str,
        unit: Dict[str, Any],
    ) -> Tuple[str, str]:
        if not source_url:
            raise ValueError("empty media URL")

        filename = _safe_publish_filename(
            unit.get("filename")
            or _filename_from_url(source_url)
            or f"topic-media-{int(time.time() * 1000)}"
        )
        suffix = os.path.splitext(filename)[1] or ".bin"
        fd, path = tempfile.mkstemp(prefix="topic-editor-media-", suffix=suffix)
        os.close(fd)

        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source_url) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"download HTTP {response.status}")
                    with open(path, "wb") as out:
                        async for chunk in response.content.iter_chunked(1024 * 256):
                            if chunk:
                                out.write(chunk)
            final_path = os.path.join(os.path.dirname(path), filename)
            os.replace(path, final_path)
            return final_path, filename
        except Exception:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise

    async def _resolve_external_for_publish(
        self, source_url: str, ref: Dict[str, Any]
    ) -> Any:
        """Lazily resolve an external media URL for publishing.

        Returns a :class:`ResolverResult` (or compatible duck-type).

        This is the only place where the T2 resolver is invoked during
        publishing. On failure the caller falls back to sending the original
        URL as text.
        """
        from src.features.summarising.external_media_resolver import (
            ExternalMediaResolver,
            ResolverResult,
            ResolveOutcome,
        )
        from src.common.external_media import make_cache_key

        resolver = ExternalMediaResolver()
        # Wire DB cache if available
        if self.db is not None and hasattr(self.db, "get_external_media_cache"):
            resolver._get_cache = self.db.get_external_media_cache
            resolver._upsert_cache = self.db.upsert_external_media_cache

        try:
            result = resolver.resolve(source_url)
            return result
        except Exception as exc:
            logger.warning(
                "TopicEditor: external resolver exception for %s: %s",
                source_url[:120], exc,
            )
            # Return a synthetic failure result
            return ResolverResult(
                outcome=ResolveOutcome.DOWNLOAD_FAILED,
                url_key=make_cache_key(source_url),
                source_url=source_url[:200],
                source_domain="unknown",
                status="download_failed",
                failure_reason=str(exc),
                trace=f"resolver exception: {exc}",
            )

    def _summarize_source_media_counts(self, source_metadata: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
        messages_with_media = 0
        attachments = 0
        embeds = 0
        resolvable_media = 0
        external_links = 0
        for row in source_metadata.values():
            row_attachments = self._normalize_attachment_list(row.get("attachments"))
            row_embeds = self._normalize_attachment_list(row.get("embeds"))
            attachment_urls = sum(
                1
                for item in row_attachments
                if isinstance(item, dict) and (item.get("url") or item.get("proxy_url"))
            )
            embed_urls = 0
            for idx, item in enumerate(row_embeds):
                if not isinstance(item, dict):
                    continue
                if _resolve_media_url_from_metadata(
                    {"message_id": str(row.get("message_id")), "kind": "embed", "index": idx},
                    {"embeds": row_embeds},
                ):
                    embed_urls += 1
            attachments += len(row_attachments)
            embeds += len(row_embeds)
            resolvable_media += attachment_urls + embed_urls
            # Count external links separately using the shared helper
            external_urls = extract_external_urls(row)
            external_links += len(external_urls)
            if row_attachments or row_embeds or external_urls:
                messages_with_media += 1
        return {
            "messages_with_media": messages_with_media,
            "attachments": attachments,
            "embeds": embeds,
            "resolvable_media": resolvable_media,
            "external_links": external_links,
        }

    async def _resolve_discord_channel(self, channel_id: Optional[int]) -> Any:
        if not self.bot or not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id)) if hasattr(self.bot, "get_channel") else None
        if channel is None and hasattr(self.bot, "fetch_channel"):
            channel = await self.bot.fetch_channel(int(channel_id))
        return channel

    def _extract_tool_calls(self, response: Any) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": block.input or {}})
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append({"id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
        return calls

    @staticmethod
    def _safe_usage_int(value: Any, default: int = 0) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _parse_cost_rate(name: str, default: float) -> float:
        """Read a cost-per-MTokens env rate, falling back to `default` when the
        value is missing, unparseable, non-finite, or negative. A misconfigured
        rate must never yield NaN (which would silently disable the cost cap
        comparison) or a negative price.
        """
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("TopicEditor invalid cost rate %s=%r; using default %s", name, raw, default)
            return default
        if not math.isfinite(value) or value < 0:
            logger.warning("TopicEditor non-finite/negative cost rate %s=%r; using default %s", name, raw, default)
            return default
        return value

    def _extract_usage(self, response: Any) -> Dict[str, int]:
        usage = getattr(response, "usage", None)
        if not usage:
            return {}
        input_tokens = self._safe_usage_int(getattr(usage, "input_tokens", 0))
        output_tokens = self._safe_usage_int(getattr(usage, "output_tokens", 0))
        raw_hit = self._safe_usage_int(getattr(usage, "cache_hit_input_tokens", 0))
        raw_miss = self._safe_usage_int(getattr(usage, "cache_miss_input_tokens", 0))
        result = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        if raw_hit or raw_miss:
            # The provider reports hit/miss as a partition of prompt tokens. A
            # shortfall means some tokens were neither flagged — bill them as
            # misses. An over-count means the counters are inconsistent: prefer
            # the reported miss count (conservative — more misses bill higher)
            # and clamp hits to the remainder, rather than emitting a partition
            # that exceeds the reported prompt size.
            if raw_hit + raw_miss > input_tokens:
                logger.warning(
                    "TopicEditor usage cache counters exceed prompt tokens: "
                    "hit=%s miss=%s prompt=%s",
                    raw_hit,
                    raw_miss,
                    input_tokens,
                )
                miss = min(raw_miss, input_tokens)
                hit = max(0, input_tokens - miss)
            else:
                hit = raw_hit
                miss = max(0, input_tokens - hit)
        else:
            # No cache info reported — treat everything as a cache miss so cost
            # accounting stays conservative.
            hit, miss = 0, input_tokens
        result["cache_hit_tokens"] = hit
        result["cache_miss_tokens"] = miss
        return result

    def _estimate_cost_usd(self, usage: Dict[str, Any]) -> Optional[float]:
        """Conservative cost estimate: every input token billed at the full rate.

        Deliberately ignores prompt caching so the `TOPIC_EDITOR_MAX_COST_USD`
        kill-switch stays a worst-case upper bound. See
        `_estimate_cache_adjusted_cost_usd` for the cache-aware sibling.
        """
        try:
            input_tokens = float(usage.get("input_tokens") or 0)
            output_tokens = float(usage.get("output_tokens") or 0)
            input_rate = self._parse_cost_rate("TOPIC_EDITOR_INPUT_COST_PER_MTOKENS", 0.14)
            output_rate = self._parse_cost_rate("TOPIC_EDITOR_OUTPUT_COST_PER_MTOKENS", 0.28)
            if input_rate <= 0 and output_rate <= 0:
                return None
            return (input_tokens / 1_000_000.0 * input_rate) + (output_tokens / 1_000_000.0 * output_rate)
        except (TypeError, ValueError):
            return None

    def _estimate_cache_adjusted_cost_usd(self, usage: Dict[str, Any]) -> Optional[float]:
        """Cache-aware cost estimate for reporting (NOT used by the kill-switch).

        Bills cache-hit input at `TOPIC_EDITOR_CACHE_HIT_COST_PER_MTOKENS` (capped
        at the full input rate) and miss input at the full input rate, so with a
        valid hit rate the result is at or below the conservative
        `_estimate_cost_usd` — that gap is exactly the cache discount this
        estimate exists to surface. Returns `None` (no cache-adjusted cost
        published) when the hit rate is explicitly emptied or misconfigured; the
        conservative estimate that drives the `TOPIC_EDITOR_MAX_COST_USD` guard
        is never affected by this path.
        """
        try:
            input_tokens = max(0.0, float(usage.get("input_tokens") or 0))
            output_tokens = max(0.0, float(usage.get("output_tokens") or 0))
            hit_tokens = max(0.0, float(usage.get("cache_hit_tokens") or 0))
            miss_tokens = max(0.0, float(usage.get("cache_miss_tokens") or 0))
            input_rate = self._parse_cost_rate("TOPIC_EDITOR_INPUT_COST_PER_MTOKENS", 0.14)
            output_rate = self._parse_cost_rate("TOPIC_EDITOR_OUTPUT_COST_PER_MTOKENS", 0.28)
            if input_rate <= 0 and output_rate <= 0:
                return None
            # Cache-hit input is billed at TOPIC_EDITOR_CACHE_HIT_COST_PER_MTOKENS,
            # defaulting to DeepSeek's published deepseek-v4-flash hit price.
            # An explicitly empty/invalid/negative/non-finite value suppresses the
            # cache-adjusted number entirely (never a misleading figure). The rate
            # is capped at the input rate so the adjusted estimate can never
            # exceed the conservative one.
            hit_rate_raw = os.getenv("TOPIC_EDITOR_CACHE_HIT_COST_PER_MTOKENS", "0.0028")
            if str(hit_rate_raw).strip() == "":
                return None
            try:
                hit_rate = float(hit_rate_raw)
            except (TypeError, ValueError):
                logger.warning("TopicEditor invalid cache hit rate %r; no cache-adjusted cost", hit_rate_raw)
                return None
            if not math.isfinite(hit_rate) or hit_rate < 0:
                logger.warning("TopicEditor non-finite/negative cache hit rate %r; no cache-adjusted cost", hit_rate_raw)
                return None
            if hit_tokens <= 0 and miss_tokens <= 0:
                # Rate is configured but the provider reported no split: report
                # the conservative number for this turn (0% hit) rather than None.
                return self._estimate_cost_usd(usage)
            hit_rate = min(hit_rate, input_rate)
            return (
                (hit_tokens / 1_000_000.0 * hit_rate)
                + (miss_tokens / 1_000_000.0 * input_rate)
                + (output_tokens / 1_000_000.0 * output_rate)
            )
        except (TypeError, ValueError):
            return None

    def _cache_hit_pct(self, usage: Dict[str, Any]) -> Optional[float]:
        try:
            hit = max(0.0, float(usage.get("cache_hit_tokens") or 0))
            miss = max(0.0, float(usage.get("cache_miss_tokens") or 0))
        except (TypeError, ValueError):
            return None
        total = hit + miss
        if total <= 0:
            return None
        return round(hit / total * 100, 1)

    def _seed_cold_start_checkpoint(
        self,
        checkpoint_key: str,
        guild_id: Optional[int],
        live_channel_id: Optional[int],
    ) -> Dict[str, Any]:
        """Cold-start: anchor the checkpoint to (run interval ago) so the first
        run immediately processes the last interval's worth of messages.

        Default interval is `TOPIC_EDITOR_COLD_START_LOOKBACK_MINUTES` (default
        60) — matching the runner's typical 60-min cadence. Falls back to the
        most recent archived message id/timestamp for tie-breaking the SQL
        ordering on `(created_at, message_id)`.
        """
        lookback_minutes = self._env_float("TOPIC_EDITOR_COLD_START_LOOKBACK_MINUTES", 60.0)
        anchor_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        anchor_iso = anchor_dt.isoformat()

        # Resolve a real message_id older-than-or-at the anchor timestamp, so
        # `get_archived_messages_after_checkpoint` can index by message_id too.
        anchor_message_id: Optional[int] = None
        if hasattr(self.db, "get_archived_message_id_before_timestamp"):
            anchor_message_id = self.db.get_archived_message_id_before_timestamp(
                guild_id=guild_id, before=anchor_iso
            )
        # Last-resort fallback: take the latest archived message id (loses the hour-window
        # semantic but never poisons the checkpoint with NULL).
        if anchor_message_id is None and hasattr(self.db, "get_latest_archived_message_checkpoint"):
            latest = self.db.get_latest_archived_message_checkpoint(guild_id=guild_id)
            if latest:
                anchor_message_id = (latest or {}).get("message_id")

        checkpoint = {
            "checkpoint_key": checkpoint_key,
            "guild_id": guild_id,
            "channel_id": live_channel_id,
            "last_message_id": anchor_message_id,
            "last_message_created_at": anchor_iso,
            "state": {
                "seeded_from": "interval_lookback",
                "lookback_minutes": lookback_minutes,
            },
        }
        self.db.upsert_topic_editor_checkpoint(checkpoint, environment=self.environment)
        return checkpoint

    def _forced_close_error(
        self,
        reason: str,
        max_turns: int,
        cumulative_cost_usd: float,
        cumulative_tokens: int,
    ) -> str:
        if reason == "cost_cap_exceeded":
            return f"cumulative_cost_usd={cumulative_cost_usd} exceeded TOPIC_EDITOR_MAX_COST_USD"
        if reason == "token_cap_exceeded":
            return f"cumulative_tokens={cumulative_tokens} exceeded TOPIC_EDITOR_MAX_TOKENS"
        return f"max_turns={max_turns} reached without finalize_run"

    def _format_cost(self, value: Any) -> str:
        return f"${value:.4f}" if isinstance(value, (int, float)) else "n/a"

    def _env_float(self, name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _env_int(self, name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _checkpoint_after(self, checkpoint: Dict[str, Any], messages: Sequence[Dict[str, Any]], run_id: str) -> Dict[str, Any]:
        if not messages:
            return dict(checkpoint, last_run_id=run_id)
        last = messages[-1]
        return {
            "checkpoint_key": checkpoint.get("checkpoint_key"),
            "guild_id": checkpoint.get("guild_id") or self._resolve_guild_id(),
            "channel_id": checkpoint.get("channel_id") or self._resolve_live_channel_id(self._resolve_guild_id()),
            "last_message_id": last.get("message_id"),
            "last_message_created_at": last.get("created_at"),
            "last_run_id": run_id,
            "state": {"source_count": len(messages)},
        }

    def _write_end_of_run_checkpoint(self, checkpoint: Dict[str, Any], messages: Sequence[Dict[str, Any]],
                                     run_id: str, forced_close: bool) -> Dict[str, Any]:
        """Advance the source checkpoint at end of run.

        A clean finalize advances normally. A forced close does NOT advance the
        window (so the next run replays the same source messages and any in-flight
        drafts are not lost), but the replay is bounded: after
        TOPIC_EDITOR_MAX_FORCED_CLOSE_REPLAYS consecutive forced closes the window
        is force-advanced anyway (with a loud audit row) to cap replay cost. Force
        advancement never deletes persisted drafts — the recovery pass re-finds them
        by message id.
        """
        max_replays = self._env_int("TOPIC_EDITOR_MAX_FORCED_CLOSE_REPLAYS", 4)
        state = dict(checkpoint.get("state") or {})
        consecutive = int(state.get("consecutive_forced_closes", 0) or 0)

        if not forced_close:
            checkpoint_after = self._checkpoint_after(checkpoint, messages, run_id)
            after_state = dict(checkpoint_after.get("state") or {})
            after_state["consecutive_forced_closes"] = 0
            checkpoint_after["state"] = after_state
            self._upsert_topic_editor_checkpoint(checkpoint_after)
            return checkpoint_after

        # Forced close: do not advance the window; just record the replay counter.
        consecutive += 1
        if consecutive >= max_replays:
            checkpoint_after = self._checkpoint_after(checkpoint, messages, run_id)
            after_state = dict(checkpoint_after.get("state") or {})
            after_state["consecutive_forced_closes"] = 0
            after_state["forced_advance_after"] = consecutive
            checkpoint_after["state"] = after_state
            try:
                self._store_transition({
                    "run_id": run_id,
                    "guild_id": checkpoint.get("guild_id") or self._resolve_guild_id(),
                    "action": "forced_close_checkpoint_forced_advance",
                    "reason": f"advanced after {consecutive} consecutive forced closes; window replayed to bound cost",
                    "payload": shape_transition_payload(
                        outcome="accepted",
                        tool_name="checkpoint",
                        extra={
                            "consecutive_forced_closes": consecutive,
                            "last_message_id": checkpoint_after.get("last_message_id"),
                        },
                    ),
                    "model": self.model,
                })
            except Exception as exc:
                logger.error("TopicEditor forced-advance audit failed: %s", exc)
            self._upsert_topic_editor_checkpoint(checkpoint_after)
            return checkpoint_after

        checkpoint_after = dict(checkpoint)
        checkpoint_after["last_run_id"] = run_id
        checkpoint_after["state"] = {**state, "consecutive_forced_closes": consecutive}
        self._upsert_topic_editor_checkpoint(checkpoint_after)
        return checkpoint_after

    def _upsert_topic_editor_checkpoint(self, checkpoint_after: Dict[str, Any]) -> None:
        """Upsert a checkpoint, logging loudly (not raising) on DB failure.

        Not advancing is the safe direction (the window just replays), so a failed
        write after publish should not fail the whole run.
        """
        try:
            self.db.upsert_topic_editor_checkpoint(checkpoint_after, environment=self.environment)
        except Exception as exc:
            logger.error("TopicEditor checkpoint upsert failed: %s", exc)

    @staticmethod
    def _normalize_attachment_list(attachments: Any) -> List[Dict[str, Any]]:
        """Return a list of attachment dicts from list, dict, or JSON-string inputs."""
        if attachments is None:
            return []
        if isinstance(attachments, list):
            return attachments
        if isinstance(attachments, dict):
            # Single attachment passed as a dict (e.g. from some archive shapes).
            return [attachments]
        if isinstance(attachments, str):
            try:
                parsed = json.loads(attachments)
            except (json.JSONDecodeError, TypeError):
                return []
            return TopicEditor._normalize_attachment_list(parsed)
        return []

    def _message_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        attachments = self._normalize_attachment_list(message.get("attachments"))
        embeds = self._normalize_attachment_list(message.get("embeds"))
        media_urls: List[str] = []
        media_refs_available: List[Dict[str, Any]] = []

        for idx, attachment in enumerate(attachments):
            url = attachment.get("url") or attachment.get("proxy_url")
            if url and isinstance(url, str):
                media_urls.append(url)
            media_refs_available.append({
                "kind": "attachment",
                "index": idx,
                "url_present": bool(attachment.get("url") or attachment.get("proxy_url")),
                "content_type": attachment.get("content_type"),
                "filename": attachment.get("filename"),
            })

        for embed_idx, embed in enumerate(embeds if isinstance(embeds, list) else []):
            if isinstance(embed, dict):
                for key in ("url", "thumbnail", "image", "video"):
                    value = embed.get(key)
                    if isinstance(value, dict):
                        url = value.get("url") or value.get("proxy_url")
                        if url and isinstance(url, str):
                            media_urls.append(url)
                    elif isinstance(value, str) and value:
                        media_urls.append(value)
            media_refs_available.append({
                "kind": "embed",
                "index": embed_idx,
                "url_present": (
                    isinstance(embed, dict) and bool(
                        (isinstance(embed.get("url"), dict) and (embed["url"].get("url") or embed["url"].get("proxy_url")))
                        or (isinstance(embed.get("thumbnail"), dict) and (embed["thumbnail"].get("url") or embed["thumbnail"].get("proxy_url")))
                        or (isinstance(embed.get("image"), dict) and (embed["image"].get("url") or embed["image"].get("proxy_url")))
                        or (isinstance(embed.get("video"), dict) and (embed["video"].get("url") or embed["video"].get("proxy_url")))
                    )
                ),
                "content_type": None,
                "filename": None,
            })

        # ── External linked media refs (after attachment/embed for priority indexing) ──
        # Uses the shared extract_external_urls helper so external index N
        # is deterministic regardless of caller.
        for external_entry in extract_external_urls(message):
            media_refs_available.append({
                "kind": external_entry["kind"],
                "index": external_entry["index"],
                "domain": external_entry["domain"],
                "url_present": external_entry["url_present"],
                "source": external_entry["source"],
            })
            # external URLs are resolved lazily, not pre-fetched into media_urls

        return {
            "message_id": message.get("message_id"),
            "guild_id": message.get("guild_id"),
            "channel_id": message.get("channel_id"),
            "author_id": message.get("author_id"),
            "author": self._author_name(message),
            "content": message.get("content") or message.get("clean_content"),
            "created_at": message.get("created_at"),
            "reaction_count": self._message_reaction_count(message),
            "media_urls": media_urls,
            "media_refs_available": media_refs_available,
        }

    def _messages_by_id(self, messages: Sequence[Dict[str, Any]], ids: Sequence[str]) -> List[Dict[str, Any]]:
        wanted = {str(item) for item in ids}
        return [message for message in messages if str(message.get("message_id")) in wanted]

    def _source_authors(self, messages: Sequence[Dict[str, Any]]) -> List[str]:
        authors = []
        for message in messages:
            author = self._author_name(message)
            if author and author not in authors:
                authors.append(author)
        return authors

    def _author_name(self, message: Dict[str, Any]) -> str:
        snapshot = message.get("author_context_snapshot") or {}
        return str(snapshot.get("server_nick") or snapshot.get("global_name") or snapshot.get("username") or message.get("author_name") or "")

    def _summary_for_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "post_topic":
            return {"blocks": args.get("blocks") or []}
        if tool_name == "post_sectioned_topic":
            summary: Dict[str, Any] = {
                "body": args.get("body"),
                "sections": args.get("sections") or [],
            }
            # Store blocks when provided alongside preserving body/sections for
            # backwards-compat readability.
            if args.get("blocks"):
                summary["blocks"] = args["blocks"]
            return summary
        if tool_name == "watch_topic":
            return {
                "why_interesting": args.get("why_interesting"),
                "revisit_when": args.get("revisit_when"),
            }
        return {"body": args.get("body"), "media": args.get("media") or []}

    def _resolve_guild_id(self) -> Optional[int]:
        if self.guild_id is not None:
            return int(self.guild_id)
        value = os.getenv("DEV_GUILD_ID") if self.environment == "dev" else os.getenv("GUILD_ID")
        return int(value) if value and str(value).isdigit() else None

    def _resolve_live_channel_id(self, guild_id: Optional[int]) -> Optional[int]:
        if self.live_channel_id is not None:
            return int(self.live_channel_id)
        env_names = ["DEV_SUMMARY_CHANNEL_ID", "DEV_LIVE_UPDATE_CHANNEL_ID"] if self.environment == "dev" else ["LIVE_UPDATE_CHANNEL_ID", "SUMMARY_CHANNEL_ID"]
        for name in env_names:
            value = os.getenv(name)
            if value and str(value).isdigit():
                return int(value)
        return None

    def _excluded_author_ids(self) -> List[int]:
        return [int(part) for part in os.getenv("LIVE_UPDATE_EXCLUDED_AUTHOR_IDS", "").split(",") if part.strip().isdigit()]

    @staticmethod
    def _checkpoint_key(guild_id: Optional[int], live_channel_id: Optional[int]) -> str:
        return f"live_update_editor:{guild_id or 'unknown'}:{live_channel_id or 'unknown'}"


@dataclass(frozen=True)
class TopicIdentity:
    topic_id: str
    canonical_key: str
    headline: str
    source_authors: Sequence[str] = field(default_factory=tuple)
    aliases: Sequence[str] = field(default_factory=tuple)
    state: Optional[str] = None
    display_slug: Optional[str] = None


@dataclass(frozen=True)
class Collision:
    topic_id: str
    canonical_key: str
    headline: str
    reason: str
    similarity: Optional[float] = None
    aliases: Sequence[str] = field(default_factory=tuple)
    state: Optional[str] = None


def canonicalize_topic_key(
    headline: str,
    *,
    creator_name: Optional[str] = None,
    topic_date: Optional[date | str] = None,
) -> str:
    """Return the locked sprint canonical key for a topic headline."""
    parts: List[str] = []
    if creator_name:
        parts.append(_slugify(creator_name))
    parts.append(_slugify(headline))
    if topic_date:
        parts.append(str(topic_date)[:10])
    return "-".join(part for part in parts if part).strip("-")


def canonicalize_proposed_key(
    proposed_key: Optional[str],
    headline: str,
    *,
    creator_name: Optional[str] = None,
    topic_date: Optional[date | str] = None,
) -> str:
    base = proposed_key or headline
    return canonicalize_topic_key(base, creator_name=creator_name, topic_date=topic_date)


def resolve_topic_alias(
    proposed_key: str,
    aliases: Iterable[Dict[str, Any]],
    *,
    environment: Optional[str] = None,
    guild_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a proposed key against topic_aliases-style rows."""
    canonical = canonicalize_topic_key(proposed_key)
    for alias in aliases or []:
        if environment is not None and alias.get("environment") != environment:
            continue
        if guild_id is not None and alias.get("guild_id") != guild_id:
            continue
        alias_key = canonicalize_topic_key(str(alias.get("alias_key") or ""))
        if alias_key == canonical:
            return alias
    return None


def detect_topic_collisions(
    *,
    proposed_canonical_key: str,
    headline: str,
    source_authors: Sequence[str],
    existing_topics: Sequence[TopicIdentity | Dict[str, Any]],
    threshold: float = SIMILARITY_COLLISION_THRESHOLD,
) -> List[Collision]:
    """Find canonical-prefix or trigram+author-overlap collisions."""
    source_author_set = _normalize_author_set(source_authors)
    collisions: List[Collision] = []
    proposed_canonical_key = canonicalize_topic_key(proposed_canonical_key)

    for raw_topic in existing_topics or []:
        topic = _coerce_topic_identity(raw_topic)
        existing_key = canonicalize_topic_key(topic.canonical_key)
        prefix_match = _canonical_prefix_match(proposed_canonical_key, existing_key)
        similarity = trigram_similarity(headline, topic.headline)
        author_overlap = len(source_author_set & _normalize_author_set(topic.source_authors))
        alias_match = any(
            _canonical_prefix_match(proposed_canonical_key, canonicalize_topic_key(alias))
            for alias in topic.aliases
        )

        if prefix_match or alias_match:
            reason = "canonical_key_prefix"
        elif similarity >= threshold and author_overlap >= 1:
            reason = "headline_similarity_author_overlap"
        else:
            continue

        collisions.append(Collision(
            topic_id=topic.topic_id,
            canonical_key=topic.canonical_key,
            headline=topic.headline,
            reason=reason,
            similarity=similarity,
            aliases=tuple(topic.aliases),
            state=topic.state,
        ))

    return collisions


def unresolved_collisions(
    collisions: Sequence[Collision],
    override_collisions: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Collision]:
    override_ids = {
        str(item.get("topic_id"))
        for item in override_collisions or []
        if item.get("topic_id")
    }
    return [collision for collision in collisions if collision.topic_id not in override_ids]


def parse_optional_datetime(value: Any) -> Optional[str]:
    """Return an ISO timestamp only for concrete date/time values.

    Tool callers often use natural-language ``revisit_when`` strings such as
    "tomorrow" or "when more results appear". The database column is a
    timestamp, so prose must stay in the topic summary rather than being written
    to ``revisit_at``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def shape_transition_payload(
    *,
    outcome: str,
    tool_name: Optional[str] = None,
    canonical_key: Optional[str] = None,
    proposed_key: Optional[str] = None,
    source_message_ids: Optional[Sequence[Any]] = None,
    collisions: Optional[Sequence[Collision]] = None,
    override_collisions: Optional[Sequence[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "outcome": outcome,
    }
    if tool_name:
        payload["tool_name"] = tool_name
    if canonical_key:
        payload["canonical_key"] = canonical_key
    if proposed_key:
        payload["proposed_key"] = proposed_key
    if source_message_ids is not None:
        payload["source_message_ids"] = [str(message_id) for message_id in source_message_ids]
    if collisions is not None:
        payload["collisions"] = [collision_to_dict(collision) for collision in collisions]
    if override_collisions:
        payload["override_collisions"] = [
            {"topic_id": str(item.get("topic_id")), "reason": item.get("reason")}
            for item in override_collisions
        ]
    if error:
        payload["error"] = error
    if extra:
        payload.update(extra)
    return payload


def build_rejected_transition(
    *,
    run_id: str,
    environment: str,
    guild_id: int,
    action: str,
    tool_call_id: Optional[str],
    reason: str,
    payload: Dict[str, Any],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if action not in {"rejected_post_simple", "rejected_post_sectioned", "rejected_post_topic", "rejected_watch"}:
        raise ValueError(f"unsupported rejected transition action: {action}")
    return {
        "run_id": run_id,
        "environment": environment,
        "guild_id": guild_id,
        "tool_call_id": tool_call_id,
        "action": action,
        "reason": reason,
        "payload": payload,
        "model": model,
    }


def build_override_transitions(
    *,
    run_id: str,
    environment: str,
    guild_id: int,
    topic_id: str,
    override_collisions: Sequence[Dict[str, Any]],
    tool_call_id: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for override in override_collisions or []:
        rows.append({
            "topic_id": topic_id,
            "run_id": run_id,
            "environment": environment,
            "guild_id": guild_id,
            "tool_call_id": tool_call_id,
            "action": "override",
            "reason": override.get("reason"),
            "payload": {
                "overridden_topic_id": str(override.get("topic_id")),
                "reason": override.get("reason"),
            },
            "model": model,
        })
    return rows


# ------------------------------------------------------------------
# Document normalization helpers (T1)
# ------------------------------------------------------------------

def normalize_media_ref(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a media ref to canonical shape: {message_id, kind: 'attachment'|'embed'|'external', index}.

    Accepts shorthand {message_id, attachment_index} which normalizes to
    {kind: 'attachment', index: attachment_index}.  Rejects invalid kind values.
    """
    if not isinstance(ref, dict):
        raise ValueError(f"media_ref must be a dict, got {type(ref).__name__}")

    message_id = str(ref.get("message_id") or "")
    if not message_id:
        raise ValueError("media_ref missing required 'message_id'")

    # Shorthand: {message_id, attachment_index}  →  canonical attachment ref
    if "attachment_index" in ref and "kind" not in ref:
        index = ref.get("attachment_index", 0)
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError(
                f"media_ref attachment_index must be an integer, got {index!r}"
            )
        return {"message_id": message_id, "kind": "attachment", "index": index}

    kind = ref.get("kind", "attachment")
    # The model sometimes emits visual-kind vocabulary (video/image/gif/audio/file)
    # when deriving media refs from the evidence shelf. Discord media is indexed as
    # attachments, so normalize those to "attachment" instead of rejecting them —
    # this removes the "Media id cannot be resolved" failure class for valid media.
    if kind in ("video", "image", "gif", "audio", "file"):
        kind = "attachment"
    if kind not in ("attachment", "embed", "external"):
        raise ValueError(
            f"media_ref kind must be 'attachment', 'embed', or 'external', got {kind!r}"
        )

    index = ref.get("index", 0)
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise ValueError(f"media_ref index must be an integer, got {index!r}")

    return {"message_id": message_id, "kind": kind, "index": index}


DRAFT_RUNTIME_FIELDS = {
    "validation_result",
    "preview_units",
    "created_at",
    "updated_at",
    "submitted_at",
    "publish_result",
    "publish_diagnostics",
    "latest_valid_preview_hash",
    "revision_hash",
    "revision_number",
    "revision_attempts",
    "status",
}


@dataclass(frozen=True)
class TopicEditorDraftLimits:
    """Configurable draft limits used by validation and agent tooling."""

    headline_target_chars: int = 110
    dek_target_chars: int = 220
    card_body_max_chars: int = 650
    max_cards: int = 4
    max_revision_attempts: int = 3
    sources_per_card_warning: int = 4
    discord_content_limit: int = 2000


@dataclass(frozen=True)
class TopicEditorDraftCard:
    """One focused editorial card in a publishable draft."""

    angle: str
    body: str
    source_message_ids: Tuple[str, ...] = field(default_factory=tuple)
    media_ids: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TopicEditorEvidenceMedia:
    """Normalized media item available to a draft."""

    media_id: str
    kind: str
    source_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None
    aesthetic_quality: Optional[int] = None
    editorial_notes: Tuple[str, ...] = field(default_factory=tuple)
    media_ref: Optional[Dict[str, Any]] = None
    strong_media: bool = False


@dataclass(frozen=True)
class TopicEditorEvidenceItem:
    """Normalized source-message evidence available to a draft."""

    message_id: str
    author: Optional[str] = None
    content: Optional[str] = None
    jump_url: Optional[str] = None
    reaction_count: int = 0
    media: Tuple[TopicEditorEvidenceMedia, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TopicEditorDraft:
    """Canonical draft document edited before topic publication."""

    draft_id: str
    topic_key: str
    template: str
    headline: str
    dek: str
    cards: Tuple[TopicEditorDraftCard, ...]
    editor_note: str


@dataclass(frozen=True)
class TopicEditorDraftValidationIssue:
    path: str
    message: str
    suggestion: Optional[str] = None


@dataclass(frozen=True)
class TopicEditorDraftValidationResult:
    status: str
    errors: Tuple[TopicEditorDraftValidationIssue, ...] = field(default_factory=tuple)
    warnings: Tuple[TopicEditorDraftValidationIssue, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TopicEditorDraftPreviewUnit:
    type: str
    content: Optional[str] = None
    media_id: Optional[str] = None
    description: Optional[str] = None
    source_message_id: Optional[str] = None
    source_url: Optional[str] = None


def topic_editor_draft_limits_from_config(config: Optional[Dict[str, Any]] = None) -> TopicEditorDraftLimits:
    """Build draft limits from a loose config/env-style mapping."""
    config = config or {}

    def _int_value(name: str, default: int) -> int:
        raw = config.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    defaults = TopicEditorDraftLimits()
    return TopicEditorDraftLimits(
        headline_target_chars=_int_value("headline_target_chars", defaults.headline_target_chars),
        dek_target_chars=_int_value("dek_target_chars", defaults.dek_target_chars),
        card_body_max_chars=_int_value("card_body_max_chars", defaults.card_body_max_chars),
        max_cards=_int_value("max_cards", defaults.max_cards),
        max_revision_attempts=_int_value("max_revision_attempts", defaults.max_revision_attempts),
        sources_per_card_warning=_int_value("sources_per_card_warning", defaults.sources_per_card_warning),
        discord_content_limit=_int_value("discord_content_limit", defaults.discord_content_limit),
    )


def _canonical_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            str(key): _canonical_jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_jsonable(item) for item in value]
    return value


def canonical_topic_editor_draft_json(draft: TopicEditorDraft | Dict[str, Any]) -> Dict[str, Any]:
    """Return the stable publishable draft JSON, excluding runtime diagnostics."""
    raw = _canonical_jsonable(draft)
    if not isinstance(raw, dict):
        raise ValueError("draft must serialize to an object")
    return {
        key: raw[key]
        for key in sorted(raw.keys())
        if key not in DRAFT_RUNTIME_FIELDS
    }


def serialize_topic_editor_draft(draft: TopicEditorDraft | Dict[str, Any]) -> str:
    """Serialize draft content with deterministic key ordering and separators."""
    return json.dumps(
        canonical_topic_editor_draft_json(draft),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def revision_hash_for_topic_editor_draft(draft: TopicEditorDraft | Dict[str, Any]) -> str:
    """Hash meaningful draft content while ignoring volatile runtime fields."""
    return hashlib.sha256(serialize_topic_editor_draft(draft).encode("utf-8")).hexdigest()


def topic_editor_draft_from_json(payload: Dict[str, Any]) -> TopicEditorDraft:
    """Parse loose draft JSON into canonical dataclasses."""
    cards = []
    for card in payload.get("cards") or []:
        if not isinstance(card, dict):
            continue
        cards.append(TopicEditorDraftCard(
            angle=str(card.get("angle") or ""),
            body=str(card.get("body") or ""),
            source_message_ids=tuple(str(sid) for sid in (card.get("source_message_ids") or []) if sid),
            media_ids=tuple(str(mid) for mid in (card.get("media_ids") or []) if mid),
        ))
    return TopicEditorDraft(
        draft_id=str(payload.get("draft_id") or ""),
        topic_key=str(payload.get("topic_key") or ""),
        template=str(payload.get("template") or ""),
        headline=str(payload.get("headline") or ""),
        dek=str(payload.get("dek") or ""),
        cards=tuple(cards),
        editor_note=str(payload.get("editor_note") or ""),
    )


def _evidence_media_by_id(
    evidence_shelf: Optional[Sequence[TopicEditorEvidenceItem | Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    media_by_id: Dict[str, Dict[str, Any]] = {}
    for item in evidence_shelf or []:
        item_obj = _canonical_jsonable(item)
        if not isinstance(item_obj, dict):
            continue
        for media in item_obj.get("media") or []:
            if not isinstance(media, dict):
                continue
            media_id = str(media.get("media_id") or "")
            if media_id:
                media_by_id[media_id] = media
    return media_by_id


def _evidence_media_by_ref(
    evidence_shelf: Optional[Sequence[TopicEditorEvidenceItem | Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    media_by_ref: Dict[str, Dict[str, Any]] = {}
    for media_id, media in _evidence_media_by_id(evidence_shelf).items():
        try:
            ref_id = media_ref_to_media_id(media.get("media_ref") or media_id_to_media_ref(media_id))
        except (ValueError, TypeError):
            continue
        media_by_ref[ref_id] = media
    return media_by_ref


def topic_editor_draft_to_structured_topic(
    draft: TopicEditorDraft | Dict[str, Any],
    *,
    evidence_shelf: Optional[Sequence[TopicEditorEvidenceItem | Dict[str, Any]]] = None,
    guild_id: Optional[Any] = None,
) -> Dict[str, Any]:
    """Convert a draft into the existing structured-topic shape.

    The conversion preserves card order, inline citation markers in body text,
    source-message ID ordering, and media ordering. Media IDs are resolved from
    the evidence shelf when present, with a conservative canonical-id fallback
    for ``message_id:kind:index`` values.
    """
    parsed = topic_editor_draft_from_json(draft) if isinstance(draft, dict) else draft
    media_by_id = _evidence_media_by_id(evidence_shelf)
    blocks: List[Dict[str, Any]] = []
    topic_source_ids: List[str] = []

    for index, card in enumerate(parsed.cards):
        source_ids = list(dict.fromkeys(str(sid) for sid in card.source_message_ids if sid))
        for sid in source_ids:
            if sid not in topic_source_ids:
                topic_source_ids.append(sid)

        media_refs: List[Dict[str, Any]] = []
        for media_id in card.media_ids:
            media = media_by_id.get(str(media_id), {})
            raw_ref = media.get("media_ref") or media_id_to_media_ref(str(media_id))
            if raw_ref:
                media_refs.append(normalize_media_ref(raw_ref))

        blocks.append({
            "type": "intro" if index == 0 else "section",
            "title": card.angle if index > 0 and card.angle else None,
            "text": card.body,
            "source_message_ids": source_ids,
            "media_refs": media_refs,
            "draft_card_angle": card.angle,
            "draft_media_ids": list(card.media_ids),
        })

    topic: Dict[str, Any] = {
        "proposed_key": parsed.topic_key,
        "canonical_key": parsed.topic_key,
        "headline": parsed.headline,
        "summary": {
            "body": parsed.dek,
            "blocks": blocks,
            "draft_id": parsed.draft_id,
            "template": parsed.template,
            "editor_note": parsed.editor_note,
        },
        "source_message_ids": topic_source_ids,
    }
    if guild_id is not None:
        topic["guild_id"] = guild_id
    return topic



def normalize_document_blocks(
    summary: Dict[str, Any],
    topic_source_message_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Normalize a topic summary into ordered document blocks.

    Handles legacy summaries with body/sections/source_message_ids by converting:
      * body       → intro block
      * sections   → section blocks
      * topic-level source_message_ids used as fallback when a block has no
        local sources.

    If *summary.blocks* is already present each block is individually
    normalized and returned.
    """
    if isinstance(summary, dict) and summary.get("blocks"):
        # Already has blocks — normalise each one individually
        blocks: List[Dict[str, Any]] = []
        for b in summary["blocks"]:
            if not isinstance(b, dict):
                continue
            block_type = b.get("type", "section")
            if block_type not in ("intro", "section"):
                continue
            blocks.append({
                "type": block_type,
                "title": b.get("title"),
                "text": str(b.get("text") or b.get("body") or ""),
                "source_message_ids": [
                    str(sid)
                    for sid in (b.get("source_message_ids") or [])
                    if sid
                ],
                "media_refs": [
                    normalize_media_ref(r) for r in (b.get("media_refs") or [])
                ],
            })
        return blocks

    # Legacy path: convert body + sections into blocks
    body = (
        (summary.get("body") or "").strip()
        if isinstance(summary, dict)
        else ""
    )
    sections = (
        summary.get("sections") or []
        if isinstance(summary, dict)
        else []
    )
    fallback_ids = [
        str(sid) for sid in (topic_source_message_ids or []) if sid
    ]

    blocks = []

    if body:
        blocks.append({
            "type": "intro",
            "title": None,
            "text": body,
            "source_message_ids": list(fallback_ids),
            "media_refs": [],
        })

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = sec.get("title") or sec.get("heading")
        text = (
            sec.get("body")
            or sec.get("text")
            or sec.get("summary")
            or ""
        )
        sec_source_ids = [
            str(sid) for sid in (sec.get("source_message_ids") or []) if sid
        ]
        if not sec_source_ids:
            sec_source_ids = list(fallback_ids)
        blocks.append({
            "type": "section",
            "title": title,
            "text": str(text),
            "source_message_ids": sec_source_ids,
            "media_refs": [],
        })

    return blocks


def normalize_topic_document(topic: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize a full topic dict into ordered document blocks.

    Handles both legacy topics (body / sections) and new-style topics
    (summary.blocks).
    """
    summary = topic.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {"body": str(summary)}
    topic_source_ids = topic.get("source_message_ids") or []
    return normalize_document_blocks(summary, topic_source_ids)


def block_source_ids(block: Dict[str, Any]) -> List[str]:
    """Extract distinct source message IDs from a single block."""
    return list(
        dict.fromkeys(
            str(sid) for sid in (block.get("source_message_ids") or []) if sid
        )
    )


def block_media_refs(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract normalized media refs from a single block."""
    return [normalize_media_ref(r) for r in (block.get("media_refs") or [])]


def media_ref_to_media_id(ref: Dict[str, Any]) -> str:
    """Return the canonical media ID for a media ref.

    Media IDs are derived only from ``normalize_media_ref`` so the evidence
    shelf, draft JSON, validation, preview, and publisher all share one
    normalization path.
    """
    normalized = normalize_media_ref(ref)
    return f"{normalized['message_id']}:{normalized['kind']}:{normalized['index']}"


def media_id_to_media_ref(media_id: str) -> Dict[str, Any]:
    """Parse a canonical ``message_id:kind:index`` media ID into a media ref."""
    parts = str(media_id or "").rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError("media_id must have shape 'message_id:kind:index'")
    message_id, kind, index = parts
    return normalize_media_ref({"message_id": message_id, "kind": kind, "index": index})


def _json_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        try:
            return _json_list(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _message_id_value(message: Dict[str, Any]) -> str:
    return str(message.get("message_id") or "")


def _message_jump_url(message: Dict[str, Any]) -> Optional[str]:
    if message.get("jump_url"):
        return str(message["jump_url"])
    guild_id = message.get("guild_id")
    channel_id = message.get("channel_id")
    message_id = message.get("message_id")
    thread_id = message.get("thread_id")
    if guild_id and channel_id and message_id:
        return message_jump_url(guild_id, channel_id, message_id, thread_id=thread_id)
    return None


def _evidence_author_name(message: Dict[str, Any]) -> Optional[str]:
    snapshot = message.get("author_context_snapshot") or {}
    value = (
        message.get("author")
        or message.get("author_name")
        or snapshot.get("server_nick")
        or snapshot.get("global_name")
        or snapshot.get("display_name")
        or snapshot.get("username")
    )
    return str(value) if value is not None and str(value) else None


def _media_understanding_by_attachment_index(message: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    by_index: Dict[int, Dict[str, Any]] = {}
    for item in message.get("media_understandings") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("attachment_index"))
        except (TypeError, ValueError):
            continue
        existing = by_index.get(index, {})
        existing_score = existing.get("aesthetic_quality")
        new_score = item.get("aesthetic_quality")
        try:
            should_replace = existing == {} or int(new_score or 0) >= int(existing_score or 0)
        except (TypeError, ValueError):
            should_replace = existing == {}
        if should_replace:
            by_index[index] = item
    return by_index


def _media_description(
    *,
    ref: Dict[str, Any],
    metadata: Dict[str, Any],
    understanding: Optional[Dict[str, Any]] = None,
) -> str:
    if understanding:
        for key in ("summary", "visual_read", "subject", "technical_signal"):
            value = understanding.get(key)
            if value:
                return str(value)
    kind = ref.get("kind")
    index = int(ref.get("index", 0))
    if kind == "attachment":
        attachments = _json_list(metadata.get("attachments"))
        if 0 <= index < len(attachments):
            attachment = attachments[index]
            filename = attachment.get("filename")
            content_type = attachment.get("content_type")
            if filename and content_type:
                return f"Attachment {index}: {filename} ({content_type})."
            if filename:
                return f"Attachment {index}: {filename}."
            if content_type:
                return f"Attachment {index}: {content_type}."
        return f"Attachment {index} from source message."
    if kind == "embed":
        embeds = _json_list(metadata.get("embeds"))
        if 0 <= index < len(embeds):
            embed = embeds[index]
            title = embed.get("title")
            url = _resolve_media_url_from_metadata(ref, metadata)
            if title:
                return f"Embed {index}: {title}."
            if url:
                return f"Embed {index}: {url}."
        return f"Embed {index} from source message."
    if kind == "external":
        url = _resolve_media_url_from_metadata(ref, metadata)
        return f"External media link: {url}." if url else f"External media link {index} from source message."
    return f"Media {media_ref_to_media_id(ref)}."


def _media_quality_score(ref: Dict[str, Any], understanding: Optional[Dict[str, Any]]) -> Optional[int]:
    if not understanding:
        return None
    for key in ("aesthetic_quality", "highlight_score", "production_quality"):
        value = understanding.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _media_editorial_notes(ref: Dict[str, Any], understanding: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    notes: List[str] = []
    if understanding:
        for key in ("technical_signal", "edit_value", "cautions", "boundary_notes"):
            value = understanding.get(key)
            if value:
                notes.append(str(value))
    if not notes and ref.get("kind") == "attachment":
        notes.append("Conservative attachment description; run media understanding if visual detail matters.")
    return tuple(notes)


def _iter_message_media_refs(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    message_id = _message_id_value(message)
    if not message_id:
        return []

    refs: List[Dict[str, Any]] = []
    for index, _attachment in enumerate(_json_list(message.get("attachments"))):
        refs.append(normalize_media_ref({"message_id": message_id, "kind": "attachment", "index": index}))
    for index, _embed in enumerate(_json_list(message.get("embeds"))):
        refs.append(normalize_media_ref({"message_id": message_id, "kind": "embed", "index": index}))
    for external in extract_external_urls(message):
        refs.append(normalize_media_ref({
            "message_id": message_id,
            "kind": external.get("kind", "external"),
            "index": external.get("index", 0),
        }))
    return refs


def _rehydrate_topic_editor_evidence_rows(
    messages: Sequence[Dict[str, Any]],
    *,
    db: Optional[Any] = None,
    guild_id: Optional[Any] = None,
    environment: str = "prod",
) -> List[Dict[str, Any]]:
    ids = [_message_id_value(message) for message in messages if _message_id_value(message)]
    if not ids or db is None or not hasattr(db, "get_topic_editor_source_messages"):
        return [dict(message) for message in messages]

    try:
        archive_rows = db.get_topic_editor_source_messages(
            ids,
            guild_id=guild_id,
            environment=environment,
            limit=max(len(ids), 1),
        )
    except Exception:
        archive_rows = []

    archive_by_id = {
        _message_id_value(row): row
        for row in archive_rows or []
        if isinstance(row, dict) and _message_id_value(row)
    }

    hydrated: List[Dict[str, Any]] = []
    for message in messages:
        message_id = _message_id_value(message)
        archive = dict(archive_by_id.get(message_id, {}))
        merged = dict(message)
        if archive:
            merged.update(archive)
            if message.get("media_understandings") and not merged.get("media_understandings"):
                merged["media_understandings"] = message.get("media_understandings")
            if message.get("author_name") and not merged.get("author_name"):
                merged["author_name"] = message.get("author_name")
        hydrated.append(merged)
    return hydrated


def resolve_topic_editor_evidence_shelf(
    messages: Sequence[Dict[str, Any]],
    *,
    db: Optional[Any] = None,
    guild_id: Optional[Any] = None,
    environment: str = "prod",
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[TopicEditorEvidenceItem, ...]:
    """Normalize source messages into the draft evidence shelf.

    Compact read-tool rows are treated as ID hints: when a DB resolver is
    available this first rehydrates them through archived source-message storage
    so media IDs, descriptions, URLs, and jump links come from the canonical
    archived message shape.
    """
    config = config or {}
    strong_quality_threshold = int(config.get("strong_media_min_aesthetic_quality", 6) or 6)
    strong_reaction_threshold = int(config.get("strong_media_min_reaction_count", 5) or 5)

    hydrated = _rehydrate_topic_editor_evidence_rows(
        messages,
        db=db,
        guild_id=guild_id,
        environment=environment,
    )

    shelf: List[TopicEditorEvidenceItem] = []
    for message in hydrated:
        if not isinstance(message, dict):
            continue
        message_id = _message_id_value(message)
        if not message_id:
            continue
        understandings = _media_understanding_by_attachment_index(message)
        reaction_count = 0
        try:
            reaction_count = int(message.get("reaction_count") or 0)
        except (TypeError, ValueError):
            reaction_count = 0

        media_items: List[TopicEditorEvidenceMedia] = []
        for ref in _iter_message_media_refs(message):
            understanding = understandings.get(int(ref.get("index", 0))) if ref.get("kind") == "attachment" else None
            media_id = media_ref_to_media_id(ref)
            quality = _media_quality_score(ref, understanding)
            strong_media = (
                quality is not None and quality >= strong_quality_threshold
            ) or reaction_count >= strong_reaction_threshold
            media_items.append(TopicEditorEvidenceMedia(
                media_id=media_id,
                kind=str(ref.get("kind") or "attachment"),
                source_url=_resolve_media_url_from_metadata(ref, message),
                thumbnail_url=None,
                description=_media_description(ref=ref, metadata=message, understanding=understanding),
                aesthetic_quality=quality,
                editorial_notes=_media_editorial_notes(ref, understanding),
                media_ref=ref,
                strong_media=strong_media,
            ))

        shelf.append(TopicEditorEvidenceItem(
            message_id=message_id,
            author=_evidence_author_name(message),
            content=str(message.get("content") or message.get("clean_content") or ""),
            jump_url=_message_jump_url(message),
            reaction_count=reaction_count,
            media=tuple(media_items),
        ))

    return tuple(shelf)


def collect_document_source_ids(blocks: List[Dict[str, Any]]) -> List[str]:
    """Return the distinct union of all block-level source message IDs."""
    seen: Set[str] = set()
    result: List[str] = []
    for block in blocks:
        for sid in block_source_ids(block):
            if sid not in seen:
                seen.add(sid)
                result.append(sid)
    return result


def render_topic(topic: Dict[str, Any]) -> List[str]:
    """Render a topic into Discord message text without DB or Discord effects."""
    headline = _clean_render_text(topic.get("headline") or topic.get("display_slug") or "Untitled")
    summary = topic.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {"body": str(summary)}
    source_suffix = _render_source_suffix(topic)

    sections = summary.get("sections") or []
    if sections:
        lines = [f"## {headline}"]
        body = _clean_render_text(summary.get("body"))
        if body:
            lines.extend(["", body])
        for section in sections:
            if not isinstance(section, dict):
                continue
            title = _clean_render_text(section.get("title") or section.get("heading") or "Details")
            section_body = _clean_render_text(section.get("body") or section.get("text") or section.get("summary"))
            lines.extend(["", f"### {title}"])
            if section_body:
                lines.append(section_body)
        if source_suffix:
            lines.extend(["", source_suffix])
        return [_trim_discord_message("\n".join(lines))]

    body = _clean_render_text(summary.get("body") or summary.get("why_interesting") or topic.get("body"))
    lines = [f"## {headline}"]
    if body:
        lines.extend(["", body])
    # Simple topics are text-only. Media refs belong in structured block
    # media_refs so they can be validated, chunked, and sent separately.
    if source_suffix:
        lines.extend(["", source_suffix])
    return [_trim_discord_message("\n".join(lines))]


def _normalize_bare_citation_markers(text: str, valid_indexes: Set[int]) -> str:
    """Convert bare sentence-end citation digits into bracket markers."""
    if not text or not valid_indexes:
        return text

    def replace(match: re.Match) -> str:
        raw = match.group(1)
        suffix = match.group(2) or ""
        markers: List[int] = []
        if len(raw) > 1 and all(ch != "0" and int(ch) in valid_indexes for ch in raw):
            markers = [int(ch) for ch in raw]
        else:
            value = int(raw)
            if value in valid_indexes:
                markers = [value]
        if not markers:
            return match.group(0)
        return " ".join(f"[{marker}]" for marker in markers) + suffix

    return re.sub(r"(?<![\[\]\(\)\w.])(\d{1,2})([.,;:!?])?(?=\s|$)", replace, text)


_CHANNEL_MENTION_RE = re.compile(r"(?<![\w#<])#([a-z0-9][a-z0-9_\-]*)")


def _substitute_channel_mentions(
    text: str,
    channel_lookup: Optional[Dict[str, int]],
) -> str:
    """Replace plain `#channelname` with Discord's `<#channel_id>` syntax so
    the mention renders as a clickable channel link rather than literal text.

    Only substitutes when the captured token (lowercased) is an unambiguous
    match in ``channel_lookup``. Unknown tokens (`#1`, `#100`, project names,
    typos) are left untouched.

    Discord channel names are lowercase alphanumeric + `_`/`-`, which is what
    the regex captures. Forum-thread names with spaces or capitals don't fit
    the token shape and don't get accidentally targeted.
    """
    if not text or not channel_lookup:
        return text

    def replace(match: "re.Match") -> str:
        token = match.group(1).lower()
        channel_id = channel_lookup.get(token)
        if channel_id is None:
            return match.group(0)
        return f"<#{int(channel_id)}>"

    return _CHANNEL_MENTION_RE.sub(replace, text)


def render_topic_publish_units(
    topic: Dict[str, Any],
    source_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    channel_lookup: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Render a structured topic into ordered publish units for block-by-block sending.

    Returns a list of units, each of which is either:

        {"kind": "text", "content": "..."}

    or

        {"kind": "media", "url": "https://...", "ref": {...}}

    The order is deterministic: header, intro text with inline linked
    citations, intro media, each section text with per-block citations, that
    section media.

    Citations are per-block, deduped, and ordered by first appearance.
    No global Sources footer is emitted for structured topics.

    This function is only called when the topic summary contains ``blocks``.
    For simple/legacy topics, use the existing ``render_topic`` function.
    """
    blocks = normalize_topic_document(topic)
    if not blocks:
        # Fallback: use legacy render_topic for simple topics
        rendered = render_topic(topic)
        return [{"kind": "text", "content": msg} for msg in rendered]

    headline = _clean_render_text(
        topic.get("headline") or topic.get("display_slug") or "Untitled"
    )
    header = f"## {headline}"

    units: List[Dict[str, Any]] = []

    # Build a lookup: global source_id → metadata row (for jump URL construction)
    meta_by_id: Dict[str, Dict[str, Any]] = source_metadata or {}
    emitted_media_keys: Set[Tuple[str, ...]] = set()

    for block in blocks:
        block_text = block.get("text", "").strip()
        # Translate plain `#channelname` into Discord's `<#id>` syntax so the
        # mention becomes a clickable channel link. Quietly noops for unknown
        # tokens.
        block_text = _substitute_channel_mentions(block_text, channel_lookup)
        block_title = block.get("title")
        block_sids = block_source_ids(block)

        # Build inline citation map for this block
        # Dedupe + preserve order of first appearance
        seen: Set[str] = set()
        ordered_ids: List[str] = []
        for sid in block_sids:
            if sid not in seen:
                seen.add(sid)
                ordered_ids.append(sid)

        # Build the text content for this block
        lines: List[str] = []
        if block["type"] == "section" and block_title:
            lines.append(f"### {_clean_render_text(block_title)}")
        elif block["type"] == "intro":
            # Intro block gets the header; subsequent intro blocks are unusual
            # but handled gracefully (no duplicate header).
            pass

        if block_text:
            lines.append(block_text)

        # Build inline citation map: idx → jump_url and idx → sid.
        # Negative lookahead (?!\() skips pre-existing [N](url) markdown links.
        idx_to_url: Dict[int, str] = {}
        idx_to_sid: Dict[int, str] = {}
        for idx, sid in enumerate(ordered_ids, start=1):
            meta = meta_by_id.get(sid, {})
            guild_id = meta.get("guild_id") or topic.get("guild_id")
            channel_id = meta.get("channel_id")
            thread_id = meta.get("thread_id")
            if guild_id and channel_id and sid:
                idx_to_url[idx] = message_jump_url(
                    guild_id, channel_id, sid, thread_id=thread_id
                )
            idx_to_sid[idx] = sid

        # Previously called _normalize_bare_citation_markers here to wrap bare
        # digits as citations. That helper couldn't distinguish "see 3." from
        # "1 in 4" and would silently corrupt arbitrary digits in the block
        # body. Trust the agent's explicit [N] markers instead; if a draft
        # ships without brackets, that's a draft-validation problem, not a
        # rendering problem.

        # Inline citation: render as `[[N]](url)`. Discord parses this as a
        # masked link `[label](url)` where the label happens to be `[N]`, and
        # renders it as a single clickable span that displays "[N]" with
        # visible brackets. (Tried alternatives that didn't work: `[N](url)`
        # strips the brackets entirely; `[\[N\]](url)` leaks literal
        # backslashes; `\[[N](url)\]` shows brackets but only the digit is
        # clickable, not the whole marker.)
        if block_text and idx_to_url:
            def _sub_citation(m: re.Match) -> str:
                n = int(m.group(1))
                url = idx_to_url.get(n)
                if url is not None:
                    return f"[[{n}]]({url})"
                return m.group(0)  # out-of-range / unresolvable → literal

            # Negative lookahead (?!\() skips pre-existing [N](url) links;
            # negative lookbehind (?<!\[) skips the [N] inside an already-
            # rendered [[N]](url) masked link so re-rendering a body that already
            # contains citations never double-wraps them into [[[N]](url)](url).
            substituted_text = re.sub(
                r"(?<!\[)\[(\d{1,2})\](?!\()", _sub_citation, block_text
            )
            # Treat already-rendered [[N]](url) masked links as an inline-citation
            # success too: neither double-wrap them nor append a redundant
            # Sources: footer alongside the citations already in the body.
            has_rendered_citations = bool(re.search(r"\[\[\d{1,2}\]\]\(", block_text))
            inline_substituted = (substituted_text != block_text) or has_rendered_citations
        else:
            substituted_text = block_text
            inline_substituted = False

        if inline_substituted:
            # Use the substituted body; omit the Sources footer to avoid
            # duplicating citations.
            lines = [
                substituted_text if ln == block_text else ln
                for ln in lines
            ]
        elif ordered_ids:
            # Fallback (no inline `[N]` markers found in body): trailing
            # Sources line so the URLs are still reachable.
            citation_parts: List[str] = []
            for idx, sid in idx_to_sid.items():
                url = idx_to_url.get(idx, "")
                if url:
                    citation_parts.append(f"[{idx}] <{url}>")
                else:
                    citation_parts.append(f"[{idx}] {sid}")
            lines.append("Sources: " + " ".join(citation_parts))

        block_content = "\n".join(lines)
        if block["type"] == "intro":
            block_content = header + "\n\n" + block_content

        units.append({"kind": "text", "content": block_content})

        # Media refs for this block
        for ref in block_media_refs(block):
            meta = meta_by_id.get(ref["message_id"], {})
            url = _resolve_media_url_from_metadata(ref, meta)
            if url:
                if url in block_content:
                    continue
                media_key = (str(url),)
                if not media_key[0]:
                    media_key = (
                        str(ref.get("message_id")),
                        str(ref.get("kind", "attachment")),
                        str(ref.get("index", 0)),
                    )
                if media_key in emitted_media_keys:
                    continue
                emitted_media_keys.add(media_key)
                # External refs carry their kind so the publisher can
                # distinguish lazy-resolve (external) from direct-attach media.
                unit_kind = "media"
                if ref.get("kind") == "external":
                    unit_kind = "external"
                units.append({
                    "kind": unit_kind,
                    "url": url,
                    "ref": ref,
                })

        # Renderer-side rescue: if a cited source message is URL-only and the
        # agent forgot to attach it via media_refs (a DeepSeek prompt-adherence
        # gap), surface the URL as an external unit so the post still embeds.
        # Skip if the URL already appears in the block body or was already
        # emitted as an explicit media_ref above.
        for sid in ordered_ids:
            meta = meta_by_id.get(sid) or {}
            content = (meta.get("content") or "").strip()
            if not content:
                continue
            m = re.fullmatch(r"\s*(https?://\S+)\s*", content)
            if not m:
                continue
            url = m.group(1)
            if url in block_content:
                continue
            media_key = (url,)
            if media_key in emitted_media_keys:
                continue
            emitted_media_keys.add(media_key)
            units.append({
                "kind": "external",
                "url": url,
                "ref": {"message_id": sid, "kind": "external", "auto_attached": True},
            })

    return units


def render_draft_publish_units(
    draft: TopicEditorDraft | Dict[str, Any],
    source_metadata: Dict[str, Dict[str, Any]],
    *,
    evidence_shelf: Optional[Sequence[TopicEditorEvidenceItem | Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Render a draft through the same structured-topic renderer used by publish.

    The returned units are intentionally unchunked. Validation and preview
    callers can inspect these exact renderer units before publisher fallback
    chunking is allowed to run.
    """
    parsed = topic_editor_draft_from_json(draft) if isinstance(draft, dict) else draft
    guild_id = None
    for row in (source_metadata or {}).values():
        if isinstance(row, dict) and row.get("guild_id"):
            guild_id = row.get("guild_id")
            break
    topic = topic_editor_draft_to_structured_topic(
        parsed,
        evidence_shelf=evidence_shelf,
        guild_id=guild_id,
    )
    rendered = render_topic_publish_units(topic, source_metadata=source_metadata)
    if not parsed.dek or not rendered:
        return rendered

    headline = _clean_render_text(parsed.headline or parsed.topic_key or "Untitled")
    header = f"## {headline}"
    intro_prefix = header + "\n\n"
    units: List[Dict[str, Any]] = [{"kind": "text", "content": f"{header}\n\n{_clean_render_text(parsed.dek)}"}]
    for index, unit in enumerate(rendered):
        if index == 0 and unit.get("kind") == "text":
            content = str(unit.get("content") or "")
            if content.startswith(intro_prefix):
                content = content[len(intro_prefix):]
            units.append(dict(unit, content=content))
        else:
            units.append(unit)
    return units


def preview_topic_editor_draft(
    draft: TopicEditorDraft | Dict[str, Any],
    source_metadata: Dict[str, Dict[str, Any]],
    *,
    evidence_shelf: Optional[Sequence[TopicEditorEvidenceItem | Dict[str, Any]]] = None,
    limits: Optional[TopicEditorDraftLimits] = None,
) -> List[Dict[str, Any]]:
    """Return ordered Discord preview units for a draft.

    Media units include evidence descriptions and source/fallback URLs. Text
    length safety is computed against unchunked renderer output.
    """
    limits = limits or TopicEditorDraftLimits()
    revision_hash = revision_hash_for_topic_editor_draft(draft)
    publish_units = render_draft_publish_units(
        draft,
        source_metadata,
        evidence_shelf=evidence_shelf,
    )
    safety_chunking_would_be_needed = any(
        unit.get("kind") == "text"
        and len(str(unit.get("content") or "")) > limits.discord_content_limit
        for unit in publish_units
    )
    media_by_ref = _evidence_media_by_ref(evidence_shelf)

    preview_units: List[Dict[str, Any]] = []
    for unit in publish_units:
        kind = unit.get("kind", "text")
        if kind == "text":
            preview_units.append({
                "type": "text",
                "content": str(unit.get("content") or ""),
                "revision_hash": revision_hash,
                "safety_chunking_would_be_needed": safety_chunking_would_be_needed,
            })
            continue

        ref = normalize_media_ref(unit.get("ref") or {})
        media_id = media_ref_to_media_id(ref)
        evidence_media = media_by_ref.get(media_id, {})
        source_message_id = str(ref.get("message_id") or "")
        source_meta = source_metadata.get(source_message_id, {}) if source_metadata else {}
        source_url = unit.get("url") or unit.get("fallback_url") or evidence_media.get("source_url")
        description = (
            evidence_media.get("description")
            or _media_description(ref=ref, metadata=source_meta, understanding=None)
        )
        preview_units.append({
            "type": "media",
            "media_id": media_id,
            "description": description,
            "source_message_id": source_message_id,
            "source_url": source_url,
            "fallback_url": source_url,
            "revision_hash": revision_hash,
            "safety_chunking_would_be_needed": safety_chunking_would_be_needed,
        })

    return preview_units


def _validation_issue(path: str, message: str, suggestion: Optional[str] = None) -> TopicEditorDraftValidationIssue:
    return TopicEditorDraftValidationIssue(path=path, message=message, suggestion=suggestion)


def _draft_cards(draft: TopicEditorDraft | Dict[str, Any]) -> Tuple[TopicEditorDraftCard, ...]:
    parsed = topic_editor_draft_from_json(draft) if isinstance(draft, dict) else draft
    return parsed.cards


def _citation_markers(text: str) -> List[int]:
    markers: List[int] = []
    for match in re.finditer(r"\[(\d{1,2})\](?!\()", text or ""):
        try:
            markers.append(int(match.group(1)))
        except (TypeError, ValueError):
            continue
    return markers


def _looks_like_digest(text: str) -> bool:
    lowered = (text or "").lower()
    digest_terms = ("roundup", "digest", "meanwhile", "also", "in other news")
    return any(term in lowered for term in digest_terms) or len(re.split(r"[.;]", text or "")) > 6


def _quote_count(text: str) -> int:
    return str(text or "").count('"') + str(text or "").count("'")


def validate_topic_editor_draft(
    draft: TopicEditorDraft | Dict[str, Any],
    evidence_shelf: Sequence[TopicEditorEvidenceItem | Dict[str, Any]],
    source_metadata: Dict[str, Dict[str, Any]],
    limits: Optional[TopicEditorDraftLimits],
    *,
    mode: str = "draft",
    latest_valid_preview_hash: Optional[str] = None,
) -> TopicEditorDraftValidationResult:
    """Validate a draft without requiring preview except in submit mode."""
    if mode not in {"draft", "preview", "submit"}:
        raise ValueError("mode must be 'draft', 'preview', or 'submit'")

    limits = limits or TopicEditorDraftLimits()
    parsed = topic_editor_draft_from_json(draft) if isinstance(draft, dict) else draft
    revision_hash = revision_hash_for_topic_editor_draft(parsed)
    errors: List[TopicEditorDraftValidationIssue] = []
    warnings: List[TopicEditorDraftValidationIssue] = []

    evidence_by_id: Dict[str, Dict[str, Any]] = {}
    strong_media_ids: Set[str] = set()
    for item in evidence_shelf or []:
        item_obj = _canonical_jsonable(item)
        if not isinstance(item_obj, dict):
            continue
        message_id = str(item_obj.get("message_id") or "")
        if message_id:
            evidence_by_id[message_id] = item_obj
        for media in item_obj.get("media") or []:
            if isinstance(media, dict) and media.get("strong_media"):
                media_id = str(media.get("media_id") or "")
                if media_id:
                    strong_media_ids.add(media_id)
    media_by_id = _evidence_media_by_id(evidence_shelf)

    if len(parsed.headline) > limits.headline_target_chars:
        warnings.append(_validation_issue(
            "headline",
            f"Headline is {len(parsed.headline)} chars; target is {limits.headline_target_chars}.",
            "Shorten the headline to the core change.",
        ))
    if parsed.dek and parsed.headline and parsed.dek.strip().lower() == parsed.headline.strip().lower():
        warnings.append(_validation_issue("dek", "Dek repeats the headline.", "Use the dek for one concrete supporting detail."))
    if not parsed.dek or len(parsed.dek.split()) < 5:
        warnings.append(_validation_issue("dek", "Dek is vague or too short.", "Add a concise concrete reason this update matters now."))

    if len(parsed.cards) > limits.max_cards:
        errors.append(_validation_issue(
            "cards",
            f"Draft has {len(parsed.cards)} cards; max is {limits.max_cards}.",
            "Keep only the strongest two to four focused cards.",
        ))

    seen_media_ids: Set[str] = set()
    for idx, card in enumerate(parsed.cards):
        path = f"cards[{idx}]"
        if len(card.body) > limits.card_body_max_chars:
            errors.append(_validation_issue(
                f"{path}.body",
                f"Card is {len(card.body)} chars; max is {limits.card_body_max_chars}.",
                "Keep one claim, one concrete detail, and one citation.",
            ))
        if not card.source_message_ids:
            errors.append(_validation_issue(
                f"{path}.source_message_ids",
                "Card has no resolvable source.",
                "Attach at least one source message that supports this card.",
            ))
        if len(card.source_message_ids) > limits.sources_per_card_warning:
            warnings.append(_validation_issue(
                f"{path}.source_message_ids",
                f"Card uses {len(card.source_message_ids)} sources; warning threshold is {limits.sources_per_card_warning}.",
                "Prefer the few source messages that directly support the card.",
            ))
        for sid in card.source_message_ids:
            if str(sid) not in evidence_by_id and str(sid) not in (source_metadata or {}):
                errors.append(_validation_issue(
                    f"{path}.source_message_ids",
                    f"Source message {sid} cannot be resolved.",
                    "Rehydrate the evidence shelf before validating or replace the source.",
                ))
            meta = (source_metadata or {}).get(str(sid), {})
            if not (meta.get("guild_id") and meta.get("channel_id")):
                errors.append(_validation_issue(
                    f"{path}.source_message_ids",
                    f"References for source message {sid} cannot render as Discord jump links.",
                    "Provide source metadata with guild_id and channel_id.",
                ))

        markers = _citation_markers(card.body)
        valid_marker_found = False
        for marker in markers:
            if marker <= 0 or marker > len(card.source_message_ids):
                errors.append(_validation_issue(
                    f"{path}.body",
                    f"Inline citation marker [{marker}] does not map to a card source.",
                    "Use citation numbers that match this card's source_message_ids order.",
                ))
            else:
                valid_marker_found = True
        if card.source_message_ids and not valid_marker_found:
            issue = _validation_issue(
                f"{path}.body",
                "Card has sources but no inline citation marker.",
                "Add [1], [2], etc. beside the supported claim.",
            )
            if mode == "submit":
                errors.append(issue)
            else:
                warnings.append(issue)

        if _looks_like_digest(card.body):
            warnings.append(_validation_issue(
                f"{path}.body",
                "Draft card reads like a digest or essay.",
                "Split unrelated points or keep only the strongest angle.",
            ))
        if _quote_count(card.body) > 4:
            warnings.append(_validation_issue(f"{path}.body", "Card uses too many quotes.", "Paraphrase and cite instead of quoting repeatedly."))
        if "community reacted" in card.body.lower() and len(card.body.split()) < 18:
            warnings.append(_validation_issue(
                f"{path}.body",
                "Weak community reacted point without concrete substance.",
                "Name the concrete reaction, test result, or implication.",
            ))

        for media_id in card.media_ids:
            if media_id in seen_media_ids:
                errors.append(_validation_issue(
                    f"{path}.media_ids",
                    f"Same media repeats unnecessarily: {media_id}.",
                    "Use each media item once next to the relevant card.",
                ))
            seen_media_ids.add(media_id)
            media = media_by_id.get(media_id)
            try:
                ref = normalize_media_ref((media or {}).get("media_ref") or media_id_to_media_ref(media_id))
            except (ValueError, TypeError):
                errors.append(_validation_issue(
                    f"{path}.media_ids",
                    f"Media id cannot be resolved: {media_id}.",
                    "Choose a media_id from the evidence shelf.",
                ))
                continue
            meta = (source_metadata or {}).get(str(ref.get("message_id")), {})
            if not _resolve_media_url_from_metadata(ref, meta) and not (media or {}).get("source_url"):
                errors.append(_validation_issue(
                    f"{path}.media_ids",
                    f"Media id cannot be resolved: {media_id}.",
                    "Rehydrate the source message or remove the media reference.",
                ))
            if not re.search(r"\b(image|video|clip|screenshot|media|shows|watch|see)\b", card.body.lower()):
                warnings.append(_validation_issue(
                    f"{path}.media_ids",
                    "Media appears detached from the relevant text.",
                    "Mention what the attached media proves or shows.",
                ))

    if parsed.template == "generation_showcase":
        if not parsed.cards:
            errors.append(_validation_issue(
                "cards",
                "generation_showcase has no cards.",
                "Add one card per generation to admire.",
            ))
        for idx, card in enumerate(parsed.cards):
            path = f"cards[{idx}]"
            if not card.media_ids:
                errors.append(_validation_issue(
                    f"{path}.media_ids",
                    "Showcase card has no media attached.",
                    "Attach the generation's media to its card so it renders inline.",
                ))
            if len(card.source_message_ids) != 1:
                errors.append(_validation_issue(
                    f"{path}.source_message_ids",
                    "Showcase card must reference exactly one generation message.",
                    "Keep one generation per card; add another card for a second generation.",
                ))

    if strong_media_ids and not any(card.media_ids for card in parsed.cards):
        warnings.append(_validation_issue(
            "cards",
            "No media is used despite strong media existing in the evidence shelf.",
            "Attach the strongest media item to the card it supports.",
        ))

    try:
        rendered_units = render_draft_publish_units(parsed, source_metadata or {}, evidence_shelf=evidence_shelf)
        for unit_idx, unit in enumerate(rendered_units):
            if unit.get("kind") == "text":
                content = str(unit.get("content") or "")
                if len(content) > limits.discord_content_limit:
                    errors.append(_validation_issue(
                        f"preview_units[{unit_idx}].content",
                        f"Rendered text unit is {len(content)} chars; Discord limit is {limits.discord_content_limit}.",
                        "Revise the draft instead of relying on publisher chunking.",
                    ))
    except Exception as exc:
        errors.append(_validation_issue("preview_units", f"Draft cannot render for validation: {exc}"))

    if mode == "submit" and latest_valid_preview_hash != revision_hash:
        errors.append(_validation_issue(
            "latest_valid_preview_hash",
            "submit_draft called before a current valid preview exists.",
            "Run preview_draft after the latest edit, then submit without further changes.",
        ))

    if errors:
        status = "blocked_for_submit" if mode == "submit" else "needs_revision"
    else:
        status = "valid"
    return TopicEditorDraftValidationResult(
        status=status,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _build_send_units(
    publish_units: List[Dict[str, Any]],
    send_units_out: List[Dict[str, Any]],
    source_metadata: Dict[str, Dict[str, Any]],
) -> None:
    """Build explicit send units from publish_units.

    Populates ``send_units_out`` in-place with dicts following the send-unit model:

        {'send_kind': 'text',   'content': str}
        {'send_kind': 'file_url', 'source_url': str, 'filename': str,
         'fallback_url': str, 'ref': dict}
        {'send_kind': 'file',   'file_path': str, 'filename': str,
         'fallback_url': str, 'ref': dict, 'trace': str}

    Text units contain the block content (already header-wrapped). Media units
    that are NOT external become ``file_url`` send units; the publisher
    downloads and reuploads consecutive file_url units together.
    External units become ``file`` send units; the actual resolution/download
    happens lazily in :meth:`TopicEditor._send_one_unit` so the caller can
    decide whether to invoke the resolver.
    """
    from src.common.external_media import sanitise_url_for_logs

    for unit in publish_units:
        kind = unit.get("kind", "text")
        if kind == "text":
            for chunk in chunk_text_for_discord(unit["content"]):
                send_units_out.append({"send_kind": "text", "content": chunk})
        elif kind == "media":
            ref = unit.get("ref", {})
            meta = source_metadata.get(str(ref.get("message_id")), {})
            if not _is_reuploadable_discord_media_url(unit["url"]):
                send_units_out.append({
                    "send_kind": "url",
                    "content": unit["url"],
                    "ref": ref,
                })
                continue
            send_units_out.append({
                "send_kind": "file_url",
                "source_url": unit["url"],
                "fallback_url": unit["url"],
                "filename": _filename_for_media_ref(ref, meta, unit["url"]),
                "ref": ref,
            })
        elif kind == "external":
            url = unit.get("url", "")
            ref = unit.get("ref", {})
            safe = sanitise_url_for_logs(url)
            send_units_out.append({
                "send_kind": "file",
                "file_path": "",          # filled by _send_one_unit after resolve
                "filename": "",           # filled by _send_one_unit after resolve
                "fallback_url": url,
                "ref": ref,
                "trace": f"external: pending resolve for {safe}",
            })


def _resolve_media_url_from_metadata(
    ref: Dict[str, Any], meta: Dict[str, Any]
) -> Optional[str]:
    """Resolve a media ref to an actual URL using source message metadata."""
    kind = ref.get("kind", "attachment")
    index = ref.get("index", 0)
    if kind == "attachment":
        attachments = meta.get("attachments") or []
        if isinstance(attachments, list) and 0 <= index < len(attachments):
            att = attachments[index]
            if isinstance(att, dict):
                return att.get("url") or att.get("proxy_url")
    elif kind == "embed":
        embeds = meta.get("embeds") or []
        if isinstance(embeds, list) and 0 <= index < len(embeds):
            emb = embeds[index]
            if isinstance(emb, dict):
                for key in ("url", "thumbnail", "image", "video"):
                    value = emb.get(key)
                    if isinstance(value, dict):
                        url = value.get("url") or value.get("proxy_url")
                        if url:
                            return url
                    elif isinstance(value, str) and value:
                        return value
    elif kind == "external":
        from src.common.external_media import extract_external_url_at_index
        return extract_external_url_at_index(meta, index)
    return None


def _filename_for_media_ref(ref: Dict[str, Any], meta: Dict[str, Any], url: str) -> str:
    kind = ref.get("kind", "attachment")
    index = int(ref.get("index") or 0)
    if kind == "attachment":
        attachments = meta.get("attachments") or []
        if isinstance(attachments, list) and 0 <= index < len(attachments):
            att = attachments[index]
            if isinstance(att, dict) and att.get("filename"):
                return _safe_publish_filename(att.get("filename"))
    return _safe_publish_filename(_filename_from_url(url) or f"media-{index}.bin")


def _filename_from_url(url: str) -> str:
    try:
        path = urlparse(str(url)).path
    except Exception:
        return ""
    name = unquote(os.path.basename(path or ""))
    return name


def _safe_publish_filename(value: Any) -> str:
    name = str(value or "media.bin").strip().replace("\x00", "")
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "media.bin"
    return name[:120]


def _is_reuploadable_discord_media_url(url: str) -> bool:
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return False
    return host in {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }


def _indent_trace_block(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in str(text).splitlines())


def _chunk_trace_lines(lines: Sequence[str], limit: int = 1900) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in lines:
        line = str(line)
        next_len = current_len + len(line) + 1
        if current and next_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line) + 1
        else:
            current.append(line)
            current_len = next_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def collision_to_dict(collision: Collision) -> Dict[str, Any]:
    return {
        "topic_id": collision.topic_id,
        "canonical_key": collision.canonical_key,
        "headline": collision.headline,
        "reason": collision.reason,
        "similarity": collision.similarity,
        "aliases": list(collision.aliases),
        "state": collision.state,
    }


def trigram_similarity(a: str, b: str) -> float:
    a_trigrams = _trigrams(a)
    b_trigrams = _trigrams(b)
    if not a_trigrams and not b_trigrams:
        return 1.0
    if not a_trigrams or not b_trigrams:
        return 0.0
    return (2.0 * len(a_trigrams & b_trigrams)) / (len(a_trigrams) + len(b_trigrams))


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _canonical_prefix_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _normalize_author_set(authors: Sequence[Any]) -> Set[str]:
    return {
        str(author).strip().lower()
        for author in authors or []
        if str(author).strip()
    }


def _trigrams(value: str) -> Set[str]:
    normalized = f"  {_slugify(value).replace('-', ' ')}  "
    if len(normalized) < 3:
        return {normalized} if normalized.strip() else set()
    return {normalized[index:index + 3] for index in range(len(normalized) - 2)}


def _coerce_topic_identity(topic: TopicIdentity | Dict[str, Any]) -> TopicIdentity:
    if isinstance(topic, TopicIdentity):
        return topic
    return TopicIdentity(
        topic_id=str(topic.get("topic_id")),
        canonical_key=str(topic.get("canonical_key") or ""),
        headline=str(topic.get("headline") or ""),
        source_authors=tuple(topic.get("source_authors") or ()),
        aliases=tuple(topic.get("aliases") or ()),
        state=topic.get("state"),
        display_slug=topic.get("display_slug"),
    )


def _render_source_suffix(topic: Dict[str, Any]) -> str:
    ids = [str(item) for item in topic.get("source_message_ids") or [] if item]
    if not ids:
        return ""
    label = "Source" if len(ids) == 1 else "Sources"
    return f"{label}: " + ", ".join(ids)


def _clean_render_text(value: Any) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(value or "").strip())


def _trim_discord_message(value: str) -> str:
    text = value.strip()
    if len(text) <= 2000:
        return text
    return text[:1997].rstrip() + "..."


def chunk_text_for_discord(text: str, limit: int = 2000) -> List[str]:
    """Split text into Discord-safe chunks preserving paragraph boundaries.

    Strategy (applied only to blocks exceeding *limit*):
      1. Split on blank-line paragraph boundaries first.
      2. Within an oversized paragraph, split on single newlines.
      3. Individual lines that are still too long are hard-split.

    Normal blocks that fit within *limit* are returned as a single-element list.
    """
    if len(text) <= limit:
        return [text]

    paragraphs = re.split(r"\n\n+", text)
    chunks: List[str] = []
    current: List[str] = []

    def _flush() -> None:
        if current:
            chunks.append("\n\n".join(current))
            current.clear()

    for para in paragraphs:
        trial = "\n\n".join(current + [para]) if current else para
        if len(trial) <= limit:
            current.append(para)
            continue
        # Current paragraph would overflow — flush what we have
        _flush()
        if len(para) <= limit:
            current.append(para)
            continue

        # Strategy 2: split oversized paragraph on single newlines
        lines = para.split("\n")
        sub_buf: List[str] = []
        for line in lines:
            trial2 = "\n".join(sub_buf + [line]) if sub_buf else line
            if len(trial2) <= limit:
                sub_buf.append(line)
                continue
            if sub_buf:
                chunks.append("\n".join(sub_buf))
                sub_buf.clear()
            # Strategy 3: hard-split individual long line
            if len(line) > limit:
                for i in range(0, len(line), limit - 3):
                    chunks.append(line[i : i + limit - 3].rstrip())
            else:
                sub_buf.append(line)
        if sub_buf:
            chunks.append("\n".join(sub_buf))

    _flush()
    return chunks
