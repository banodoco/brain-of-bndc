"""Shared media/social helpers for the live-update social review loop.

Provides admin-equivalent media inspection, download, and social route
resolution without duplicating Sharer or admin-chat internals.

Sprint 1: read-only helpers — no publishing, no mutation.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import aiohttp

if TYPE_CHECKING:
    import discord
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")


# ── Discord message inspection ────────────────────────────────────────


async def inspect_discord_message(
    bot: "discord.Client",
    channel_id: int,
    message_id: int,
) -> Dict[str, Any]:
    """Fetch a live Discord message and return structured data.

    Returns a dict with::

        message_id, channel_id, guild_id, content, author_name,
        created_at, attachments (list of {filename, url, content_type}),
        embeds_media (list of {slot, url} from image, thumbnail, video,
        author icon, etc.), reactions (list of {emoji, count}).

    Does NOT persist any data.  CDN URLs are fresh (ephemeral) — callers
    must NOT treat them as durable identities.
    """
    result: Dict[str, Any] = {
        "message_id": message_id,
        "channel_id": channel_id,
        "guild_id": None,
        "content": "",
        "author_name": "",
        "created_at": None,
        "attachments": [],
        "embeds_media": [],
        "reactions": [],
    }

    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            channel = await bot.fetch_channel(channel_id)

        # Handle ForumChannel — the "message" might be a thread
        import discord as _discord
        if isinstance(channel, _discord.ForumChannel):
            try:
                thread = await bot.fetch_channel(int(message_id))
                if isinstance(thread, _discord.Thread):
                    channel = thread
            except Exception:
                pass

        if not hasattr(channel, "fetch_message"):
            return result

        msg = await channel.fetch_message(int(message_id))
        if not msg:
            return result

        result["content"] = msg.content or ""
        result["author_name"] = str(msg.author) if msg.author else ""
        result["created_at"] = msg.created_at.isoformat() if msg.created_at else None
        result["guild_id"] = getattr(msg.guild, "id", None) if msg.guild else None

        # Attachments (fresh CDN URLs — ephemeral)
        for att in msg.attachments:
            result["attachments"].append({
                "filename": att.filename,
                "url": att.url,
                "content_type": att.content_type,
                "size": att.size,
            })

        # Embed-derived media (fresh CDN URLs — ephemeral)
        for emb in msg.embeds:
            emb_data = emb.to_dict() if hasattr(emb, "to_dict") else {}
            # Image
            if emb_data.get("image") and emb_data["image"].get("url"):
                result["embeds_media"].append({
                    "slot": "image",
                    "url": emb_data["image"]["url"],
                })
            # Thumbnail
            if emb_data.get("thumbnail") and emb_data["thumbnail"].get("url"):
                result["embeds_media"].append({
                    "slot": "thumbnail",
                    "url": emb_data["thumbnail"]["url"],
                })
            # Video
            if emb_data.get("video") and emb_data["video"].get("url"):
                result["embeds_media"].append({
                    "slot": "video",
                    "url": emb_data["video"]["url"],
                })
            # Author icon
            if emb_data.get("author") and emb_data["author"].get("icon_url"):
                result["embeds_media"].append({
                    "slot": "author_icon",
                    "url": emb_data["author"]["icon_url"],
                })
            # Footer icon
            if emb_data.get("footer") and emb_data["footer"].get("icon_url"):
                result["embeds_media"].append({
                    "slot": "footer_icon",
                    "url": emb_data["footer"]["icon_url"],
                })

        # Reactions
        for r in msg.reactions:
            result["reactions"].append({
                "emoji": str(r.emoji),
                "count": r.count,
            })

    except Exception as e:
        logger.debug(
            "inspect_discord_message(%d, %d) failed: %s",
            channel_id,
            message_id,
            e,
        )
        result["error"] = str(e)

    return result


# ── Discord media URL refresh ─────────────────────────────────────────


async def refresh_discord_media_urls(
    bot: "discord.Client",
    channel_id: int,
    message_id: int,
) -> Dict[str, Any]:
    """Return fresh attachment and embed-derived media URLs.

    Returns a dict with::

        attachments: list of {filename, url, content_type}
        embeds_media: list of {slot, url}

    All URLs are freshly fetched from Discord and are ephemeral CDN URLs.
    Callers must NOT persist them as durable identity.
    """
    inspected = await inspect_discord_message(bot, channel_id, message_id)
    return {
        "attachments": inspected.get("attachments", []),
        "embeds_media": inspected.get("embeds_media", []),
    }


# ── URL download ──────────────────────────────────────────────────────


async def download_media_url(
    url: str,
    dest_dir: str,
    filename_prefix: str = "media",
) -> Optional[Dict[str, Any]]:
    """Download media from a URL to a local directory.

    Returns a dict with ``local_path`` (str) and ``content_type`` (str),
    or ``None`` on failure.

    Extracted from Sharer._download_media_from_url so both the live-update
    social loop and Sharer can share the same implementation.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        # Basic filename extraction from URL
        filename_from_url = Path(url.split("?")[0]).name
        safe_filename = "".join(
            c if c.isalnum() or c in (".", "_", "-") else "_"
            for c in filename_from_url
        )
        if not safe_filename:
            safe_filename = f"{filename_prefix}_download"

        save_path = dest / f"{filename_prefix}_{safe_filename}"
        original_suffix = Path(filename_from_url).suffix
        if original_suffix:
            save_path = save_path.with_suffix(original_suffix)

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(
                        "download_media_url: HTTP %d for %s",
                        resp.status,
                        url,
                    )
                    return None

                content = await resp.read()
                save_path.write_bytes(content)

                content_type = resp.headers.get("Content-Type")
                if not content_type:
                    content_type, _ = mimetypes.guess_type(url)
                if not content_type:
                    content_type = "application/octet-stream"

                logger.info("Downloaded media from %s to %s", url, save_path)
                return {
                    "url": url,
                    "filename": filename_from_url,
                    "content_type": content_type,
                    "local_path": str(save_path),
                }
    except Exception as e:
        logger.error("Error downloading media from %s: %s", url, e, exc_info=True)
        return None


# ── Social route helpers ──────────────────────────────────────────────


def list_social_routes(
    db_handler: "DatabaseHandler",
    guild_id: int,
    channel_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """List configured social routes for a guild.

    When ``channel_id`` is provided, both channel-specific and guild-default
    (null channel_id) routes are returned.  Uses ServerConfig's Supabase
    client for the query.
    """
    server_config = getattr(db_handler, "server_config", None)
    if not server_config or not getattr(server_config, "_supabase", None):
        logger.warning("list_social_routes: ServerConfig or Supabase client not available")
        return []

    try:
        supabase = server_config._supabase
        query = (
            supabase.table("social_channel_routes")
            .select("*")
            .eq("guild_id", guild_id)
            .eq("enabled", True)
        )
        if channel_id is not None:
            # Return channel-specific + guild default
            query = query.or_(
                f"channel_id.eq.{channel_id},channel_id.is.null"
            )
        result = query.order("channel_id", desc=False).limit(50).execute()
        return result.data or []
    except Exception as e:
        logger.error("list_social_routes(%d, %s): %s", guild_id, channel_id, e, exc_info=True)
        return []


def resolve_social_route(
    db_handler: "DatabaseHandler",
    guild_id: int,
    channel_id: int,
    platform: str,
) -> Optional[Dict[str, Any]]:
    """Resolve the best-matching social route.

    Delegates to ServerConfig.resolve_social_route which uses:
    1. Exact channel route
    2. Parent channel route
    3. Guild default route (channel_id is null)
    """
    server_config = getattr(db_handler, "server_config", None)
    if not server_config:
        logger.warning("resolve_social_route: ServerConfig not available")
        return None

    return server_config.resolve_social_route(guild_id, channel_id, platform)
