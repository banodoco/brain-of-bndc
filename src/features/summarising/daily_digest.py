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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


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


async def _send_media_files(
    channel: Any,
    storage_handler: Any,
    media_entries: List[Dict[str, str]],
) -> List[int]:
    """Send media as Discord files, falling back to URL text per item."""
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


async def post_digest(
    bot: Any,
    items: List[Dict[str, Any]],
    channel_id: int,
    storage_handler: Any = None,
) -> Dict[int, List[int]]:
    """Post one message/contiguous-chunk-group per item to channel_id.

    Each item's text is rendered as plain Discord text and chunked to
    Discord's 2000-char limit via the existing ``chunk_text_for_discord``
    helper from ``topic_editor``.

    Returns
    -------
    dict[int, list[int]]
        ``{item_index: [sent_msg.id, ...]}``.  Items that produce no text
        are omitted from the mapping.
    """
    from src.features.summarising.topic_editor import chunk_text_for_discord  # noqa: PLC0415

    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    result: Dict[int, List[int]] = {}
    for idx, item in enumerate(items):
        text = _format_item_for_discord(item)
        chunks = chunk_text_for_discord(text)
        sent_ids: List[int] = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            msg = await channel.send(chunk)
            sent_ids.append(msg.id)
        sent_ids.extend(
            await _send_media_files(channel, storage_handler, _iter_media_entries(item))
        )
        if sent_ids:
            result[idx] = sent_ids

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
) -> Dict[str, Any]:
    """Orchestrate the daily digest: query → convert → enrich → post → upsert.

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

    # 4. Convert to legacy items
    items = topics_to_legacy_daily_summary_items(in_window)

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

    # 6. Post to Discord — one chunk-group per item
    post_mapping = await post_digest(bot, items, channel_id, storage_handler)

    # 7. Assign posted_message_ids from the post_digest mapping
    #    These are IDs of the messages WE just posted, NOT topics.discord_message_ids
    for idx, item in enumerate(items):
        item["posted_message_ids"] = post_mapping.get(idx, [])

    # 8. Cheap title-based short_summary
    titles = [item.get("title") or "" for item in items if item.get("title")]
    short_summary = "; ".join(titles[:5])
    if len(items) > 5:
        short_summary += f" (+{len(items) - 5} more)"

    # 9. Upsert one daily_summaries row
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
