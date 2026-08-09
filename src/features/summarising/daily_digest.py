"""
Daily live-update digest — converts posted TopicEditor topics into
legacy-shaped daily_summary items and coordinates persistence + posting.
"""

from __future__ import annotations

import asyncio
import io
import json as _json
import logging
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Default number of curated stories the daily digest condenses the day into.
DEFAULT_DIGEST_MAX_STORIES = int(os.getenv("DAILY_DIGEST_MAX_STORIES", "5") or "5")

# Token budget for the editorial call. Generous because the editor emits 5
# multi-block stories with many inline citations AND a reasoning model spends
# part of the budget thinking — too low truncates the JSON and forces the
# (bounded) uncurated fallback.
DEFAULT_DIGEST_MAX_TOKENS = int(os.getenv("DAILY_DIGEST_MAX_TOKENS", "32000") or "32000")

# Max media files a single digest story may attach (across all its blocks), so
# one busy story can't dump a dozen images/clips.
DEFAULT_DIGEST_MAX_MEDIA_PER_STORY = int(os.getenv("DAILY_DIGEST_MAX_MEDIA_PER_STORY", "3") or "3")


def _resolve_digest_model(model: Optional[str] = None) -> str:
    """Resolve the editorial LLM model for the daily digest.

    Falls back through the digest-specific env var, then the topic editor's
    model, then the shared live-update default — so the digest tracks whatever
    model the hourly live-update editor is already using in this environment.
    """
    if model:
        return model
    from src.features.summarising.live_update_prompts import DEFAULT_LIVE_UPDATE_MODEL  # noqa: PLC0415

    return (
        os.getenv("DAILY_DIGEST_MODEL")
        or os.getenv("TOPIC_EDITOR_MODEL")
        or DEFAULT_LIVE_UPDATE_MODEL
    )


def topics_to_legacy_daily_summary_items(
    topics: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert a list of posted TopicEditor topics into legacy daily_summary items.

    This is a **pure function** — no I/O.  All source-message lookups
    (channel_id, message metadata) are the caller's responsibility.

    Mapping rules (verified against the legacy contract at 9915d8f^):

    * ``headline`` → ``title``
    * intro block ``text`` → ``mainText``
    * intro block's **first** ``media_refs[].message_id`` →
      ``mainMediaMessageId`` (``str | None``)
    * Each ``section`` block → a subTopic dict with the **verified legacy
      keys**:

        * ``text`` — block body (section title folded in, if present)
        * ``subTopicMediaMessageIds`` — every ``media_refs[].message_id``
        * ``message_id`` — first ``source_message_ids`` entry (or ``None``)
        * ``channel_id`` — always ``None`` (requires source-message lookup
          which is I/O)

      **Do NOT** emit ``subTopicText`` or ``subTopicTitle`` — those keys do
      not exist in the legacy contract.

    * Item-level ``message_id`` → first source id of the intro block
      (or ``None``).
    * Item-level ``channel_id`` → ``None`` (not directly available on the
      topic row).
    * **Do NOT** set ``posted_message_ids`` — those are assigned by the
      orchestrator after the digest is posted to Discord.

    Parameters
    ----------
    topics : list[dict]
        Raw topic rows as returned by ``StorageHandler.get_topics()``.
        Each row must include at least ``headline`` and ``summary``
        (a JSON object with ``blocks``).

    Returns
    -------
    list[dict]
        Legacy-shaped items, one per topic, ready for enrichment + posting.
    """
    items: List[Dict[str, Any]] = []

    for topic in topics:
        if not isinstance(topic, dict):
            continue

        headline = topic.get("headline")
        summary = topic.get("summary") or {}

        # Normalize summary to a dict with blocks
        if isinstance(summary, str):
            import json as _json

            try:
                summary = _json.loads(summary)
            except (_json.JSONDecodeError, TypeError):
                summary = {}

        if not isinstance(summary, dict):
            summary = {}

        blocks: List[Dict[str, Any]] = summary.get("blocks") or []

        # --- Locate intro block ---
        intro_block: Optional[Dict[str, Any]] = None
        section_blocks: List[Dict[str, Any]] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "intro" and intro_block is None:
                intro_block = blk
            elif blk.get("type") == "section":
                section_blocks.append(blk)

        # --- Extract intro data ---
        main_text: Optional[str] = None
        main_media_message_id: Optional[str] = None
        if intro_block is not None:
            main_text = str(intro_block.get("text") or intro_block.get("body") or "")
            media_refs = intro_block.get("media_refs") or []
            if media_refs:
                first_ref = media_refs[0]
                if isinstance(first_ref, dict) and first_ref.get("message_id"):
                    main_media_message_id = str(first_ref["message_id"])

        # --- Item-level message_id (first source of intro block) ---
        item_message_id: Optional[str] = None
        if intro_block is not None:
            src_ids = intro_block.get("source_message_ids") or []
            if src_ids:
                item_message_id = str(src_ids[0])

        # --- Build subTopic dicts from section blocks ---
        sub_topics: List[Dict[str, Any]] = []
        for blk in section_blocks:
            # Body text: fold title in if present
            body_text = str(blk.get("text") or blk.get("body") or "")
            section_title = blk.get("title")
            if section_title:
                title_str = str(section_title).strip()
                if title_str:
                    # Fold title into body: "**Title**\\nBody"
                    body_text = f"**{title_str}**\n{body_text}".strip()

            # Media ref message ids
            media_refs = blk.get("media_refs") or []
            media_ids: List[str] = [
                str(r["message_id"])
                for r in media_refs
                if isinstance(r, dict) and r.get("message_id")
            ]

            # First source_message_id as subTopic-level message_id
            src_ids = blk.get("source_message_ids") or []
            sub_msg_id: Optional[str] = str(src_ids[0]) if src_ids else None

            sub_topics.append(
                {
                    "text": body_text,
                    "subTopicMediaMessageIds": media_ids,
                    "message_id": sub_msg_id,
                    "channel_id": None,
                }
            )

        items.append(
            {
                "title": headline,
                "mainText": main_text,
                "mainMediaMessageId": main_media_message_id,
                "subTopics": sub_topics,
                "message_id": item_message_id,
                "channel_id": None,
                # posted_message_ids intentionally omitted — assigned post-posting
            }
        )

    return items


# ---------------------------------------------------------------------------
# LLM editorial layer — condense the day into <= N curated stories
# ---------------------------------------------------------------------------

# Stage 1 — selection/clustering. Tiny output (indices + a headline each), so it
# never truncates regardless of how busy the day was.
DIGEST_SELECT_PROMPT = (
    "You are the daily editor for the Banodoco community — practitioners building with "
    "generative video and image tooling. You are given every live-update topic the bot "
    "posted in the last 24 hours, each with an `index`. Choose the AT MOST {max_stories} "
    "most meaningful developments and group related topics into stories, most-important "
    "first. Drop the trivial; fewer than {max_stories} is good on a quiet day — never pad.\n\n"
    "Return ONLY valid JSON, no prose or fences:\n"
    '{{"clusters": [{{"headline": "short working title", "candidate_indexes": [0, 3]}}]}}'
)

# Stage 2 — write ONE story from its cluster's topics. Bounded output (a single
# story), so it never truncates either.
DIGEST_WRITE_PROMPT = (
    "You are the daily editor for the Banodoco community. Write ONE SHORT story for the "
    "daily digest from the topics provided (grouped as one development). This is a recap, "
    "not a reproduction — be tight and skimmable. Factual, plain insider voice, **bold** "
    "key names/models/tools, no hype, no filler.\n\n"
    "Return a story as BLOCKS:\n"
    "- `title`: punchy, specific, no '#'.\n"
    "- `blocks`: PREFER A SINGLE block (2-3 sentences total). Add a second block ONLY if a "
    "genuinely distinct point needs its own supporting media. Never more than 2 blocks. Each:\n"
    "    * `text`: at most 2 sentences.\n"
    "    * `source_message_ids`: the REAL source ids this block draws from (from the provided "
    "`source_message_ids`), ordered by relevance — at most 2-3.\n"
    "    * `media_message_ids`: AT MOST ONE genuinely illustrative media for this block (from "
    "the provided `media_message_ids`); usually zero. Pick the single best clip/image; do not "
    "attach several.\n"
    "- Cite inline: write `[1]`, `[2]`, … in `text` where the number is the 1-based position in "
    "THAT block's `source_message_ids`. One citation per claim — don't over-cite.\n\n"
    "Only use ids that appear in the provided topics. Return ONLY valid JSON, no fences:\n"
    '{{"title": "...", "blocks": [{{"text": "... [1]", "source_message_ids": ["..."], '
    '"media_message_ids": ["..."]}}]}}'
)


def _ordered_unique(values: List[Any]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for value in values:
        if value is None:
            continue
        s = str(value)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _topic_block_source_ids(topic: Dict[str, Any]) -> List[str]:
    """Ordered-unique source_message_ids across all blocks of a topic."""
    summary = topic.get("summary") or {}
    if isinstance(summary, str):
        try:
            summary = _json.loads(summary)
        except (ValueError, TypeError):
            summary = {}
    blocks = (summary or {}).get("blocks") or []
    ids: List[str] = []
    for blk in blocks:
        if isinstance(blk, dict):
            ids.extend(blk.get("source_message_ids") or [])
    return _ordered_unique(ids)


def build_digest_candidates(topics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build compact, LLM-ready candidates from posted topics.

    Reuses :func:`topics_to_legacy_daily_summary_items` for the text + media so
    the editor can only reference media that the downstream pipeline can resolve,
    and additionally exposes each topic's real ``source_message_ids`` so the
    editor can cite sources inline (``[N]``) exactly like a live update.
    """
    legacy_items = topics_to_legacy_daily_summary_items(topics)
    candidates: List[Dict[str, Any]] = []
    for index, (topic, item) in enumerate(zip(topics, legacy_items)):
        media_ids: List[str] = []
        if item.get("mainMediaMessageId"):
            media_ids.append(str(item["mainMediaMessageId"]))
        for sub in item.get("subTopics") or []:
            media_ids.extend(str(m) for m in (sub.get("subTopicMediaMessageIds") or []) if m)

        text_parts: List[str] = []
        if item.get("mainText"):
            text_parts.append(str(item["mainText"]))
        for sub in item.get("subTopics") or []:
            if sub.get("text"):
                text_parts.append(str(sub["text"]))

        candidates.append(
            {
                "index": index,
                "headline": item.get("title") or "",
                "text": "\n".join(text_parts),
                "source_message_ids": _topic_block_source_ids(topic),
                "media_message_ids": _ordered_unique(media_ids),
                "authors": topic.get("source_authors") or [],
                "last_published_at": topic.get("last_published_at"),
            }
        )
    return candidates


def _build_select_user_message(candidates: List[Dict[str, Any]]) -> str:
    """Compact candidate list for stage 1 — index, headline, and a text snippet
    (enough to judge importance, small enough to never bloat the call)."""
    compact = [
        {
            "index": c["index"],
            "headline": c["headline"],
            "summary": (c["text"] or "")[:400],
            "authors": c["authors"][:5],
        }
        for c in candidates
    ]
    return _json.dumps({"topics": compact}, ensure_ascii=False)


def _build_write_user_message(cluster_candidates: List[Dict[str, Any]], headline_hint: str) -> str:
    """Full material for stage 2 — only the cluster's topics, with their real
    source/media ids so the model can cite and attach correctly."""
    payload = {
        "suggested_headline": headline_hint,
        "topics": [
            {
                "headline": c["headline"],
                "text": c["text"],
                "source_message_ids": c["source_message_ids"],
                "media_message_ids": c["media_message_ids"],
            }
            for c in cluster_candidates
        ],
    }
    return _json.dumps(payload, ensure_ascii=False)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return -1


def _parse_json_object(text: Any) -> Any:
    """Defensively parse a JSON object/array from model output, tolerating
    markdown fences and surrounding prose. Returns the parsed value or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip().strip("`").strip()
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return _json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return None


def _parse_digest_response(text: Any) -> List[Dict[str, Any]]:
    """Parse a {"stories": [...]} (or bare list) response into a stories list."""
    data = _parse_json_object(text)
    if isinstance(data, dict):
        stories = data.get("stories")
    elif isinstance(data, list):
        stories = data
    else:
        stories = None
    return stories if isinstance(stories, list) else []


def _story_blocks(story: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize a story to a list of blocks, tolerating the old flat shape."""
    blocks = story.get("blocks")
    if isinstance(blocks, list) and blocks:
        return [b for b in blocks if isinstance(b, dict)]
    # backward-compat: a single-body story becomes one block
    body = story.get("body") or story.get("mainText")
    if body:
        return [
            {
                "text": body,
                "source_message_ids": story.get("source_message_ids") or [],
                "media_message_ids": story.get("media_message_ids") or [],
            }
        ]
    return []


def _story_to_item(
    story: Dict[str, Any],
    valid_media_ids: Set[str],
    valid_source_ids: Set[str],
) -> Optional[Dict[str, Any]]:
    """Map one curated story onto the legacy daily_summary item schema.

    Blocks become the intro (``mainText`` + ``mainMediaMessageId``) and the
    ``subTopics`` list, each carrying its source ``message_id``/``channel_id``
    (legacy reference fields). A transient ``_source_ids`` list is attached to
    every block for inline ``[N]`` citation rendering and stripped before the
    row is stored.
    """

    # Story-level media budget + dedup: a given media id is used at most once
    # across the whole story, and total media is capped so one busy story can't
    # dump a dozen files.
    used_media: Set[str] = set()
    media_budget = [DEFAULT_DIGEST_MAX_MEDIA_PER_STORY]

    def _clean(block: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
        text = str(block.get("text") or "")
        srcs = [s for s in _ordered_unique(block.get("source_message_ids") or []) if s in valid_source_ids]
        meds: List[str] = []
        for m in _ordered_unique(block.get("media_message_ids") or []):
            if m in valid_media_ids and m not in used_media and media_budget[0] > 0:
                used_media.add(m)
                media_budget[0] -= 1
                meds.append(m)
        return text, srcs, meds

    blocks = _story_blocks(story)
    if not blocks:
        return None

    main_text, main_srcs, main_meds = _clean(blocks[0])
    item: Dict[str, Any] = {
        "title": story.get("title"),
        "mainText": main_text,
        "mainMediaMessageId": main_meds[0] if main_meds else None,
        "message_id": main_srcs[0] if main_srcs else None,
        "channel_id": None,  # resolved from source metadata before posting
        "_source_ids": main_srcs,
        "subTopics": [],
    }
    for block in blocks[1:]:
        text, srcs, meds = _clean(block)
        if not text and not meds:
            continue
        item["subTopics"].append(
            {
                "text": text,
                "subTopicMediaMessageIds": meds,
                "message_id": srcs[0] if srcs else None,
                "channel_id": None,
                "_source_ids": srcs,
            }
        )
    return item


def _uncurated_fallback(topics: List[Dict[str, Any]], max_stories: int) -> List[Dict[str, Any]]:
    """Degraded path: legacy 1:1 mapping, BOUNDED to max_stories so a curation
    failure never dumps the entire day as a wall of posts."""
    return topics_to_legacy_daily_summary_items(topics)[:max_stories]


async def _call_json(
    llm_client: Any,
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
) -> Any:
    """One LLM call returning parsed JSON (dict/list), with a single retry.

    Returns the parsed object, or None if both attempts fail/parse-fail.
    """
    for attempt in range(2):
        try:
            text = await llm_client.generate_chat_completion(
                model=model,
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                max_tokens=max_tokens,
                temperature=0.4,
            )
        except Exception:
            logger.error("daily digest: LLM call failed (attempt %d)", attempt + 1, exc_info=True)
            continue
        obj = _parse_json_object(text)
        if obj is not None:
            return obj
        logger.warning("daily digest: unparseable JSON (attempt %d)", attempt + 1)
    return None


async def curate_digest_stories(
    topics: List[Dict[str, Any]],
    llm_client: Any,
    *,
    model: Optional[str] = None,
    max_stories: int = DEFAULT_DIGEST_MAX_STORIES,
    max_tokens: int = DEFAULT_DIGEST_MAX_TOKENS,
) -> List[Dict[str, Any]]:
    """Condense the day's topics into <= N curated stories via TWO bounded LLM
    stages, so no single call has to emit everything (the one-shot's failure mode):

      1. SELECT: pick & cluster the topics into <= max_stories groups (tiny output).
      2. WRITE: one bounded call PER cluster produces that story's blocks + inline
         [N] citations (small output each).

    Returns legacy-shaped items for the existing media/enrich/post pipeline. Falls
    back to a BOUNDED uncurated mapping (<= max_stories) — never a wall — if there's
    no client or both stages yield nothing usable.
    """
    candidates = build_digest_candidates(topics)
    if not candidates:
        return []
    if llm_client is None:
        logger.warning("daily digest: no LLM client; bounded uncurated fallback")
        return _uncurated_fallback(topics, max_stories)

    valid_media_ids: Set[str] = {m for c in candidates for m in c["media_message_ids"]}
    valid_source_ids: Set[str] = {s for c in candidates for s in c["source_message_ids"]}
    by_index = {c["index"]: c for c in candidates}
    resolved_model = _resolve_digest_model(model)

    # --- Stage 1: select & cluster ---
    selection = await _call_json(
        llm_client,
        model=resolved_model,
        system_prompt=DIGEST_SELECT_PROMPT.format(max_stories=max_stories),
        user_message=_build_select_user_message(candidates),
        max_tokens=4000,
    )
    clusters = (selection or {}).get("clusters") if isinstance(selection, dict) else None
    if not isinstance(clusters, list) or not clusters:
        logger.warning("daily digest: selection stage produced no clusters; bounded fallback")
        return _uncurated_fallback(topics, max_stories)

    # --- Stage 2: write one story per cluster (concurrently, order preserved) ---
    async def _write(cluster: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        idxs = [i for i in (cluster.get("candidate_indexes") or []) if _to_int(i) in by_index]
        cluster_cands = [by_index[_to_int(i)] for i in idxs]
        if not cluster_cands:
            return None
        story = await _call_json(
            llm_client,
            model=resolved_model,
            system_prompt=DIGEST_WRITE_PROMPT,
            user_message=_build_write_user_message(cluster_cands, cluster.get("headline") or ""),
            max_tokens=max_tokens,
        )
        if not isinstance(story, dict):
            return None
        item = _story_to_item(story, valid_media_ids, valid_source_ids)
        if item and (item.get("title") or item.get("mainText")):
            return item
        return None

    results = await asyncio.gather(
        *[_write(c) for c in clusters[:max_stories] if isinstance(c, dict)],
        return_exceptions=True,
    )
    items: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, dict):
            items.append(r)
        elif isinstance(r, Exception):
            logger.warning("daily digest: a story write failed: %s", r)

    if not items:
        logger.warning("daily digest: no stories written; bounded fallback")
        return _uncurated_fallback(topics, max_stories)

    logger.info(
        "daily digest: %d topics -> %d clusters -> %d stories (model=%s)",
        len(candidates), len(clusters), len(items), resolved_model,
    )
    return items


# ---------------------------------------------------------------------------
# Inline source citations ([N] -> jump link), like live updates
# ---------------------------------------------------------------------------

def _iter_item_blocks_with_sources(item: Dict[str, Any]):
    """Yield (block_dict, source_ids) for the main block and each subTopic."""
    yield item, item.get("_source_ids") or []
    for sub in item.get("subTopics") or []:
        if isinstance(sub, dict):
            yield sub, sub.get("_source_ids") or []


async def resolve_source_metadata(
    items: List[Dict[str, Any]],
    storage_handler: Any,
    *,
    guild_id: int,
    environment: str = "prod",
) -> Dict[str, Dict[str, Any]]:
    """Fetch {source_id: {guild_id, channel_id, thread_id}} for every cited
    source across all blocks, and fill each block's legacy ``channel_id`` field.
    """
    all_ids: List[str] = []
    for _, src_ids in (pair for item in items for pair in _iter_item_blocks_with_sources(item)):
        all_ids.extend(src_ids)
    all_ids = _ordered_unique(all_ids)
    if not all_ids:
        return {}

    meta_by_id: Dict[str, Dict[str, Any]] = {}
    try:
        rows = storage_handler.get_topic_editor_source_messages(
            all_ids, guild_id=guild_id, environment=environment, limit=100
        )
        for row in rows or []:
            mid = str(row.get("message_id"))
            meta_by_id[mid] = {
                "guild_id": row.get("guild_id"),
                "channel_id": row.get("channel_id"),
                "thread_id": row.get("thread_id"),
            }
    except Exception:
        logger.warning("daily digest: source metadata lookup failed", exc_info=True)
        return {}

    # Fill legacy channel_id on each block from its primary source.
    # Stored as a string to match the legacy daily_summaries schema exactly.
    for item in items:
        for block, src_ids in _iter_item_blocks_with_sources(item):
            primary = src_ids[0] if src_ids else None
            if primary and primary in meta_by_id:
                channel_id = meta_by_id[primary].get("channel_id")
                block["channel_id"] = str(channel_id) if channel_id is not None else None
    return meta_by_id


def _substitute_citations(
    text: str,
    ordered_source_ids: List[str],
    meta_by_id: Dict[str, Dict[str, Any]],
    guild_fallback: Optional[int],
) -> str:
    """Replace inline ``[N]`` (or ``[[N]]``) markers with ``[[N]](jump_url)``
    masked links.

    Mirrors the live-update renderer: N is the 1-based index into this block's
    source ids; unresolvable markers are left literal. Both single- and double-
    bracket markers are accepted (the model sometimes emits ``[[N]]`` despite
    the prompt); already-rendered ``[[N]](url)`` links are left untouched.
    """
    if not text or not ordered_source_ids:
        return text
    from src.common.urls import message_jump_url  # noqa: PLC0415

    idx_to_url: Dict[int, str] = {}
    for idx, sid in enumerate(ordered_source_ids, start=1):
        meta = meta_by_id.get(sid, {})
        guild_id = meta.get("guild_id") or guild_fallback
        channel_id = meta.get("channel_id")
        if guild_id and channel_id and sid:
            idx_to_url[idx] = message_jump_url(
                guild_id, channel_id, sid, thread_id=meta.get("thread_id")
            )

    def _sub(m: "re.Match") -> str:
        n = int(m.group(1) or m.group(2))
        url = idx_to_url.get(n)
        return f"[[{n}]]({url})" if url else m.group(0)

    return re.sub(
        r"\[\[(\d{1,2})\]\](?!\()|(?<!\[)\[(\d{1,2})\](?!\()", _sub, text
    )


def apply_citations(
    items: List[Dict[str, Any]],
    meta_by_id: Dict[str, Dict[str, Any]],
    guild_id: Optional[int],
) -> List[Dict[str, Any]]:
    """Rewrite each block's text, turning ``[N]`` markers into jump links."""
    for item in items:
        item["mainText"] = _substitute_citations(
            item.get("mainText") or "", item.get("_source_ids") or [], meta_by_id, guild_id
        )
        for sub in item.get("subTopics") or []:
            if isinstance(sub, dict):
                sub["text"] = _substitute_citations(
                    sub.get("text") or "", sub.get("_source_ids") or [], meta_by_id, guild_id
                )
    return items


def _finalize_for_storage(items: List[Dict[str, Any]]) -> None:
    """Prepare items for the stored row: drop transient ``_source_ids`` and set
    the legacy ``included_in_main`` flag the website filters on (every curated
    digest story is meant to show), so the community section renders them."""
    for item in items:
        item.pop("_source_ids", None)
        item["included_in_main"] = True
        for sub in item.get("subTopics") or []:
            if isinstance(sub, dict):
                sub.pop("_source_ids", None)
                sub["included_in_main"] = True


# ---------------------------------------------------------------------------
# Media resolution helpers
# ---------------------------------------------------------------------------

def _collect_message_ids(items: List[Dict[str, Any]]) -> List[str]:
    """Collect unique message_ids from all items' media references."""
    ids: Set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = item.get("mainMediaMessageId")
        if mid:
            ids.add(str(mid))
        for sub in item.get("subTopics") or []:
            if not isinstance(sub, dict):
                continue
            for smid in sub.get("subTopicMediaMessageIds") or []:
                if smid:
                    ids.add(str(smid))
    return list(ids)


def _classify_media_type(content_type: Optional[str], url: str = "") -> str:
    """Return 'video' or 'image' from content_type, falling back to URL extension."""
    if content_type:
        ct = content_type.lower()
        if ct.startswith("video/"):
            return "video"
        if ct.startswith("image/"):
            return "image"
    # Fallback: guess from the URL via mimetypes
    mime_type, _ = mimetypes.guess_type(url.split("?")[0])
    if mime_type:
        mt = mime_type.lower()
        if mt.startswith("video/"):
            return "video"
        if mt.startswith("image/"):
            return "image"
    return "image"  # default


async def resolve_media_urls(
    items: List[Dict[str, Any]],
    storage_handler: Any,
    guild_id: Optional[int],
    environment: str = "prod",
    date: Optional[str] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """Resolve media message_ids → Supabase storage URLs.

    Collects every ``mainMediaMessageId`` and each subTopic's
    ``subTopicMediaMessageIds``, hydrates source-message metadata via
    the **synchronous** ``storage_handler.get_topic_editor_source_messages``
    (wrapped in ``asyncio.to_thread``), then for each attachment/embed URL:

    1. Downloads the file via ``storage_handler.download_file``.
    2. Re-uploads to the ``summary-media`` bucket via
       ``storage_handler.upload_bytes_to_storage`` with a dated path.
    3. Classifies the media as ``video`` or ``image`` from the content-type
       (falling back to URL extension).

    Media download/upload failures are **non-fatal**: the offending media
    is skipped and the digest proceeds.

    Parameters
    ----------
    items : list[dict]
        Legacy-shaped daily-summary items (post-conversion).
    storage_handler : StorageHandler
        Instance providing ``download_file``, ``upload_bytes_to_storage``,
        and the sync ``get_topic_editor_source_messages``.
    guild_id : int | None
        Guild filter for source-message lookup.
    environment : str
        ``'prod'`` or ``'dev'``; forwarded to source-message lookup.
    date : str | None
        Date string for the storage path prefix (e.g. ``'2026-05-24'``).
        Defaults to today's UTC date.

    Returns
    -------
    dict[str, list[dict]]
        Mapping ``{message_id: [{url, type}, ...]}``.  Message IDs with
        no resolvable media are absent from the dict (not mapped to an
        empty list).
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Collect all unique message_ids
    all_ids = _collect_message_ids(items)
    if not all_ids:
        return {}

    # 2. Hydrate source messages (sync → asyncio.to_thread)
    try:
        source_rows = await asyncio.to_thread(
            storage_handler.get_topic_editor_source_messages,
            all_ids,
            guild_id,
            environment,
            len(all_ids),
        )
    except Exception:
        logger.warning("Failed to fetch topic-editor source messages for media resolution", exc_info=True)
        return {}

    if not source_rows:
        return {}

    # 3. Hydrate channel_id fields and build URL extraction index:
    #    message_id -> [(url, content_type)]
    source_by_id: Dict[str, Dict[str, Any]] = {
        str(row.get("message_id")): row
        for row in source_rows
        if isinstance(row, dict) and row.get("message_id") is not None
    }
    _hydrate_item_channels(items, source_by_id)

    url_index: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("message_id") or "")
        if not mid:
            continue
        urls: List[Tuple[str, Optional[str]]] = []

        # Attachments
        attachments = _normalize_list_field(row.get("attachments"))
        for att in attachments:
            if not isinstance(att, dict):
                continue
            url = att.get("url") or att.get("proxy_url")
            if url:
                content_type = att.get("content_type")
                urls.append((url, content_type))

        # Embeds
        embeds = _normalize_list_field(row.get("embeds"))
        for embed in embeds:
            if not isinstance(embed, dict):
                continue
            # Extract the first available URL from embed sub-objects
            for key in ("url", "thumbnail", "image", "video"):
                sub = embed.get(key)
                if isinstance(sub, dict):
                    embed_url = sub.get("url") or sub.get("proxy_url")
                    if embed_url:
                        urls.append((embed_url, None))  # embeds rarely carry content_type
                        break

        if urls:
            url_index[mid] = urls

    if not url_index:
        return {}

    # 4. Download → re-upload each URL
    result: Dict[str, List[Dict[str, str]]] = {}
    for mid, url_tuples in url_index.items():
        resolved: List[Dict[str, str]] = []
        for i, (source_url, content_type) in enumerate(url_tuples):
            try:
                # Determine extension from URL or content-type
                ext = _extension_from_url(source_url, content_type)
                # Download
                file_data = await storage_handler.download_file(source_url)
                if not file_data or not file_data.get("bytes"):
                    logger.debug("Skipping media %s — download returned no data", source_url[:80])
                    continue

                # Re-upload
                effective_ct = content_type or file_data.get("content_type") or "application/octet-stream"
                media_type = _classify_media_type(effective_ct, source_url)
                if not ext:
                    ext = ".mp4" if media_type == "video" else ".jpg"
                storage_path = f"{date}/{mid}_{i}{ext}"
                public_url = await storage_handler.upload_bytes_to_storage(
                    file_data["bytes"],
                    storage_path,
                    effective_ct,
                    bucket_name=storage_handler.SUMMARY_MEDIA_BUCKET,
                )
                if not public_url:
                    logger.debug("Skipping media %s — upload returned no URL", source_url[:80])
                    continue

                media_entry = {"url": public_url, "type": media_type}
                if media_type == "video":
                    poster_bytes = _extract_video_poster(file_data["bytes"])
                    if poster_bytes:
                        poster_url = await storage_handler.upload_bytes_to_storage(
                            poster_bytes,
                            f"{date}/{mid}_{i}_poster.jpg",
                            "image/jpeg",
                            bucket_name=storage_handler.SUMMARY_MEDIA_BUCKET,
                        )
                        if poster_url:
                            media_entry["poster_url"] = poster_url
                resolved.append(media_entry)
            except Exception:
                logger.debug("Non-fatal media failure for %s", source_url[:80], exc_info=True)
                continue

        if resolved:
            result[mid] = resolved

    return result


def _hydrate_item_channels(
    items: List[Dict[str, Any]],
    source_by_id: Dict[str, Dict[str, Any]],
) -> None:
    """Fill legacy ``channel_id`` fields from archived source message rows."""
    for item in items:
        item_mid = str(item.get("message_id") or "")
        if item_mid in source_by_id:
            item["channel_id"] = str(source_by_id[item_mid].get("channel_id"))
        elif item.get("mainMediaMessageId") and str(item["mainMediaMessageId"]) in source_by_id:
            item["channel_id"] = str(source_by_id[str(item["mainMediaMessageId"])].get("channel_id"))

        for sub in item.get("subTopics") or []:
            if not isinstance(sub, dict):
                continue
            sub_mid = str(sub.get("message_id") or "")
            if sub_mid in source_by_id:
                sub["channel_id"] = str(source_by_id[sub_mid].get("channel_id"))
                continue
            for media_mid in sub.get("subTopicMediaMessageIds") or []:
                if str(media_mid) in source_by_id:
                    sub["channel_id"] = str(source_by_id[str(media_mid)].get("channel_id"))
                    break


def enrich_items(
    items: List[Dict[str, Any]],
    media_urls: Dict[str, List[Dict[str, str]]],
) -> List[Dict[str, Any]]:
    """Enrich daily-summary items with resolved media URLs.

    Mirroring legacy ``_enrich_summary_with_media_urls``:

    * Item-level ``mainMediaUrls`` is set from ``media_urls`` keyed on
      ``mainMediaMessageId`` — a list of ``{url, type}`` dicts, or
      ``None`` when the message id is absent/unresolved.
    * Per-subTopic ``subTopicMediaUrls`` is a **list of lists** with one
      entry per ``subTopicMediaMessageIds`` element: the resolved list
      of ``{url, type}`` dicts, or ``None`` for unresolved ids.

    Parameters
    ----------
    items : list[dict]
        Legacy-shaped daily-summary items (post-conversion).
    media_urls : dict
        Mapping from ``resolve_media_urls``.

    Returns
    -------
    list[dict]
        The same items list, mutated in-place with media URL keys added.
    """
    for item in items:
        if not isinstance(item, dict):
            continue

        # --- mainMediaUrls ---
        main_mid = item.get("mainMediaMessageId")
        if main_mid and main_mid in media_urls:
            item["mainMediaUrls"] = media_urls[main_mid]
        else:
            item["mainMediaUrls"] = None

        for sub in item.get("subTopics") or []:
            if not isinstance(sub, dict):
                continue
            smids = sub.get("subTopicMediaMessageIds") or []
            sub_resolved: List[Optional[List[Dict[str, str]]]] = []
            for smid in smids:
                if smid and smid in media_urls:
                    sub_resolved.append(media_urls[smid])
                else:
                    sub_resolved.append(None)
            sub["subTopicMediaUrls"] = sub_resolved

    return items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_list_field(value: Any) -> List[Dict[str, Any]]:
    """Return a list of dicts from list, dict, or JSON-string inputs."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
        except (_json.JSONDecodeError, TypeError):
            return []
        return _normalize_list_field(parsed)
    return []


def _extension_from_url(url: str, content_type: Optional[str] = None) -> str:
    """Derive a file extension (with leading dot) from a URL or content-type."""
    # Try content-type first (most reliable)
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    # Fall back to URL path via mimetypes
    path_part = url.split("?")[0]
    mime_type, _ = mimetypes.guess_type(path_part)
    if mime_type:
        ext = mimetypes.guess_extension(mime_type)
        if ext:
            return ext
    # Last resort: extract from the last path segment
    filename = path_part.rstrip("/").split("/")[-1]
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].split("?")[0]
    return ""


def _extract_video_poster(video_bytes: bytes) -> Optional[bytes]:
    """Best-effort poster extraction matching legacy summary-media rows."""
    try:
        import imageio.v3 as iio  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return None

    try:
        frame = iio.imread(video_bytes, index=0)
        image = Image.fromarray(frame)
        image.thumbnail((300, 300))
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=70)
        return out.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

def _format_item_for_discord(item: Dict[str, Any]) -> str:
    """Render a legacy daily-summary item as plain Discord text."""
    parts: List[str] = []

    title = item.get("title")
    if title:
        parts.append(f"## {title}")

    main_text = item.get("mainText") or ""
    if main_text:
        parts.append(main_text)

    sub_topics = item.get("subTopics") or []
    for sub in sub_topics:
        if not isinstance(sub, dict):
            continue
        sub_text = sub.get("text") or ""
        if sub_text:
            parts.append(sub_text)

    return "\n\n".join(p for p in parts if p)


def _iter_media_entries(item: Dict[str, Any]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    for media in item.get("mainMediaUrls") or []:
        if isinstance(media, dict) and media.get("url"):
            entries.append(media)
    for sub in item.get("subTopics") or []:
        if not isinstance(sub, dict):
            continue
        for per_msg in sub.get("subTopicMediaUrls") or []:
            if isinstance(per_msg, list):
                for media in per_msg:
                    if isinstance(media, dict) and media.get("url"):
                        entries.append(media)
    return entries


def _item_send_blocks(item: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, str]]]]:
    """Break an item into ordered (text, media_entries) blocks for sending.

    Mirrors the live-update structure: the intro block (title + mainText) and
    its media, then each subTopic's text and its media — so each point's media
    sits beside the point it supports rather than all media being dumped at the
    end.
    """
    blocks: List[Tuple[str, List[Dict[str, str]]]] = []

    intro_parts: List[str] = []
    if item.get("title"):
        intro_parts.append(f"## {item['title']}")
    if item.get("mainText"):
        intro_parts.append(str(item["mainText"]))
    intro_media = [m for m in (item.get("mainMediaUrls") or []) if isinstance(m, dict) and m.get("url")]
    blocks.append(("\n\n".join(intro_parts), intro_media))

    for sub in item.get("subTopics") or []:
        if not isinstance(sub, dict):
            continue
        sub_media: List[Dict[str, str]] = []
        for per_msg in sub.get("subTopicMediaUrls") or []:
            if isinstance(per_msg, list):
                sub_media.extend(m for m in per_msg if isinstance(m, dict) and m.get("url"))
        blocks.append((str(sub.get("text") or ""), sub_media))

    return blocks


async def _send_media_files(
    channel: Any,
    storage_handler: Any,
    media_entries: List[Dict[str, str]],
    seen_urls: Optional[Set[str]] = None,
) -> List[int]:
    """Send media as Discord files, falling back to URL text per item.

    ``seen_urls`` (when provided) dedups across the whole digest — a URL already
    posted in an earlier block/story is skipped, so the same media never repeats.
    """
    if seen_urls is None:
        seen_urls = set()
    # drop blanks, already-seen, and within-call duplicates while preserving order
    deduped: List[Dict[str, str]] = []
    for media in media_entries:
        url = media.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(media)
    media_entries = deduped
    if not media_entries:
        return []

    try:
        import discord  # type: ignore
    except Exception:
        discord = None

    sent_ids: List[int] = []
    files = []
    fallback_urls: List[str] = []
    for media in media_entries[:10]:
        url = media.get("url")
        if not url:
            continue
        if discord is None or storage_handler is None:
            fallback_urls.append(url)
            continue
        try:
            file_data = await storage_handler.download_file(url)
            if not file_data or not file_data.get("bytes"):
                fallback_urls.append(url)
                continue
            filename = _filename_for_url(url, media.get("type"))
            files.append(discord.File(io.BytesIO(file_data["bytes"]), filename=filename))
        except Exception:
            logger.debug("Failed to prepare digest media file for %s", url[:80], exc_info=True)
            fallback_urls.append(url)

    if files:
        try:
            msg = await channel.send(files=files)
            sent_ids.append(msg.id)
        except Exception:
            logger.debug("Failed to send digest media files; falling back to URLs", exc_info=True)
            fallback_urls.extend(media.get("url") for media in media_entries[:10] if media.get("url"))

    for url in fallback_urls:
        msg = await channel.send(url)
        sent_ids.append(msg.id)
    return sent_ids


def _filename_for_url(url: str, media_type: Optional[str]) -> str:
    ext = _extension_from_url(url)
    if not ext:
        ext = ".mp4" if media_type == "video" else ".jpg"
    stem = url.split("?")[0].rstrip("/").split("/")[-1].split(".")[0] or "media"
    return f"{stem}{ext}"


def _format_digest_header(now: datetime) -> str:
    """Render the dated digest header, e.g. '# Daily Update - Sunday, May 7'."""
    return f"# Daily Update - {now.strftime('%A, %B')} {now.day}"


async def post_digest(
    bot: Any,
    items: List[Dict[str, Any]],
    channel_id: int,
    storage_handler: Any = None,
    *,
    header: Optional[str] = None,
    guild_id: Optional[int] = None,
    footer_label: str = "**Click here to jump to the beginning of today's summary:**",
) -> Dict[Any, List[int]]:
    """Post the digest to channel_id.

    Posts an optional dated ``header`` first, then one message/contiguous-chunk
    -group per item, then (when the header was posted and ``guild_id`` is known)
    a footer with a masked jump-link back to that header message.

    Each item's text is rendered as plain Discord text and chunked to
    Discord's 2000-char limit via the existing ``chunk_text_for_discord``
    helper from ``topic_editor``.

    Returns
    -------
    dict
        ``{item_index: [sent_msg.id, ...]}`` for each item, plus the special
        keys ``"header"`` and ``"footer"`` carrying their message ids (when
        posted). Entries that produce no text are omitted.
    """
    from src.features.summarising.topic_editor import chunk_text_for_discord  # noqa: PLC0415

    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    result: Dict[Any, List[int]] = {}

    # --- Header (first message; becomes the jump-link anchor) ---
    first_message_id: Optional[int] = None
    if header and header.strip():
        header_msg = await channel.send(header.strip())
        first_message_id = header_msg.id
        result["header"] = [header_msg.id]

    # --- Stories: one message per block (intro + each subTopic), media beside it ---
    seen_media_urls: Set[str] = set()  # digest-wide dedup so no media repeats
    for idx, item in enumerate(items):
        sent_ids: List[int] = []
        for block_text, block_media in _item_send_blocks(item):
            for chunk in chunk_text_for_discord(block_text):
                if not chunk.strip():
                    continue
                msg = await channel.send(chunk)
                sent_ids.append(msg.id)
            sent_ids.extend(
                await _send_media_files(channel, storage_handler, block_media, seen_media_urls)
            )
        if sent_ids:
            result[idx] = sent_ids

    # --- Footer jump-link back to the header ---
    if first_message_id is not None and guild_id is not None:
        from src.common.urls import message_jump_url  # noqa: PLC0415

        jump_url = message_jump_url(guild_id, channel_id, first_message_id)
        footer_msg = await channel.send(f"---\n\n{footer_label} {jump_url}")
        result["footer"] = [footer_msg.id]

    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def daily_digest_run(
    bot: Any,
    storage_handler: Any,
    *,
    guild_id: int,
    channel_id: int,
    environment: str = "prod",
    now: Optional[datetime] = None,
    llm_client: Any = None,
    model: Optional[str] = None,
    max_stories: int = DEFAULT_DIGEST_MAX_STORIES,
) -> Dict[str, Any]:
    """Orchestrate the daily digest: query → curate (LLM) → enrich → post → upsert.

    Parameters
    ----------
    bot : discord.Client
        Discord bot instance used to resolve and send to ``channel_id``.
    storage_handler : StorageHandler
        Provides ``get_topics``, media helpers, and ``store_daily_digest``.
    guild_id : int
        Discord guild id to scope the topic query.
    channel_id : int
        Discord channel id to post the digest to.
    environment : str
        ``'prod'`` or ``'dev'``.
    now : datetime | None
        Override for "now"; defaults to current UTC time.  Inject in tests.

    Returns
    -------
    dict
        ``{status:'ok', upserts:1, items_posted:<n>, date:<str>}`` on success,
        or ``{status:'skipped', reason:'no_topics_in_window', upserts:0}`` when
        no posted topics fall within the trailing 24 h window.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(hours=24)
    date_str = now.strftime("%Y-%m-%d")

    # 1. Fetch posted topics — raise limit to avoid silent window drops
    all_topics = await storage_handler.get_topics(
        guild_id=guild_id,
        states=["posted"],
        environment=environment,
        limit=500,
    )

    # 2. Apply 24h window on last_published_at in Python
    in_window: List[Dict[str, Any]] = []
    for topic in all_topics:
        lpa = topic.get("last_published_at")
        if not lpa:
            continue
        try:
            if isinstance(lpa, str):
                if lpa.endswith("Z"):
                    lpa = lpa[:-1] + "+00:00"
                dt = datetime.fromisoformat(lpa)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            elif isinstance(lpa, datetime):
                dt = lpa if lpa.tzinfo else lpa.replace(tzinfo=timezone.utc)
            else:
                continue
            if dt >= cutoff:
                in_window.append(topic)
        except (ValueError, TypeError):
            continue

    # 3. Empty-window short-circuit — skip BOTH post and upsert
    if not in_window:
        logger.info(
            "daily_digest_run: no posted topics in trailing 24h for guild=%s env=%s — skipping",
            guild_id,
            environment,
        )
        return {"status": "skipped", "reason": "no_topics_in_window", "upserts": 0}

    # 4. Editorial pass: condense the day into <= max_stories curated stories.
    #    Falls back to the uncurated 1:1 mapping internally if the LLM is
    #    unavailable / errors / returns unparseable output, so the digest
    #    always posts something.
    items = await curate_digest_stories(
        in_window,
        llm_client,
        model=model,
        max_stories=max_stories,
    )

    # 5. Resolve and enrich media (non-fatal)
    try:
        media_urls = await resolve_media_urls(
            items,
            storage_handler,
            guild_id=guild_id,
            environment=environment,
            date=date_str,
        )
        items = enrich_items(items, media_urls)
    except Exception:
        logger.warning(
            "daily_digest_run: media resolution raised unexpectedly, proceeding with no media",
            exc_info=True,
        )
        items = enrich_items(items, {})

    # 5b. Resolve source-message metadata and render inline [N] citations as
    #     jump-links (like live updates). Non-fatal — citations are best-effort.
    try:
        meta_by_id = await resolve_source_metadata(
            items, storage_handler, guild_id=guild_id, environment=environment
        )
        apply_citations(items, meta_by_id, guild_id)
    except Exception:
        logger.warning(
            "daily_digest_run: citation rendering failed; posting without inline links",
            exc_info=True,
        )

    # 6. Post to Discord — dated header, one chunk-group per item, footer jump-link
    post_mapping = await post_digest(
        bot,
        items,
        channel_id,
        storage_handler,
        header=_format_digest_header(now),
        guild_id=guild_id,
    )

    # 7. Assign posted_message_ids from the post_digest mapping
    #    These are IDs of the messages WE just posted, NOT topics.discord_message_ids.
    #    Header/footer ids ride on the first/last item so the stored row records
    #    every message we created (clean deletion / regeneration later).
    for idx, item in enumerate(items):
        item["posted_message_ids"] = list(post_mapping.get(idx, []))
    if items:
        items[0]["posted_message_ids"] = (
            list(post_mapping.get("header", [])) + items[0]["posted_message_ids"]
        )
        items[-1]["posted_message_ids"] = (
            items[-1]["posted_message_ids"] + list(post_mapping.get("footer", []))
        )

    # 8. Cheap title-based short_summary
    titles = [item.get("title") or "" for item in items if item.get("title")]
    short_summary = "; ".join(titles[:5])
    if len(items) > 5:
        short_summary += f" (+{len(items) - 5} more)"

    # 9. Upsert one daily_summaries row (legacy schema: drop transient fields,
    #    set the included_in_main flag the website's community section filters on)
    _finalize_for_storage(items)
    dev_mode = environment == "dev"
    stored = await storage_handler.store_daily_digest(
        channel_id=channel_id,
        full_summary=items,
        short_summary=short_summary,
        date=date_str,
        dev_mode=dev_mode,
        guild_id=guild_id,
    )
    if not stored:
        return {
            "status": "failed",
            "reason": "store_daily_digest_failed",
            "upserts": 0,
            "items_posted": len(items),
            "date": date_str,
        }

    return {
        "status": "ok",
        "upserts": 1,
        "items_posted": len(items),
        "date": date_str,
    }
