"""Publish-unit reconstruction from topic summary JSON.

Sprint 1: reconstruct publish_units from topic_summary_data (title,
message_id, channel_id, mainMediaMessageId, subTopics) plus handoff
source_metadata.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def reconstruct_publish_units(
    topic_summary_data: Dict[str, Any],
    source_metadata: Optional[Dict[str, Any]] = None,
    mode: str = "draft",
) -> Dict[str, Any]:
    """Build a publish_units dict from topic summary JSON.

    Expected topic_summary_data keys::

        title            — the topic title / headline
        message_id       — Discord message ID of the topic root
        channel_id       — Discord channel ID
        mainMediaMessageId — optional, message with primary media
        subTopics        — list of sub-topic dicts

    Args:
        topic_summary_data: The topic summary JSON from the handoff.
        source_metadata: Caller-supplied metadata snapshot.
        mode: ``\"draft\"`` (queue mode) or ``\"publish\"``.
            In publish mode with subTopics present, produces multi-unit
            output with a root unit plus one unit per sub-topic.

    Returns a dict suitable for storing as publish_units JSONB::

        {
            "units": [
                {
                    "title": "...",
                    "message_id": 123,
                    "channel_id": 456,
                    "media_message_id": 789 or None,
                    "sub_topics": [...],
                }
            ],
            "source_metadata": {...},
        }
    """
    source_metadata = source_metadata or {}

    unit: Dict[str, Any] = {
        "title": topic_summary_data.get("title", ""),
        "message_id": topic_summary_data.get("message_id"),
        "channel_id": topic_summary_data.get("channel_id"),
        "media_message_id": topic_summary_data.get("mainMediaMessageId"),
        "sub_topics": topic_summary_data.get("subTopics", []),
    }

    # Include any extra keys from the summary that might be useful
    for key in ("guild_id", "platform", "author_name", "created_at"):
        if key in topic_summary_data and key not in unit:
            unit[key] = topic_summary_data[key]

    units: List[Dict[str, Any]] = [unit]

    # In publish mode with subTopics present, produce multi-unit output
    # so the LLM can decide between a single post or a thread.
    if mode == "publish":
        sub_topics = topic_summary_data.get("subTopics", []) or []
        if sub_topics:
            for st in sub_topics:
                if not isinstance(st, dict):
                    continue
                sub_unit: Dict[str, Any] = {
                    "title": st.get("title", st.get("name", "")),
                    "message_id": topic_summary_data.get("message_id"),
                    "channel_id": topic_summary_data.get("channel_id"),
                    "media_message_id": st.get("mediaMessageId"),
                    "sub_topics": [],
                    "_is_subtopic": True,
                }
                units.append(sub_unit)

    return {
        "units": units,
        "source_metadata": source_metadata,
    }
