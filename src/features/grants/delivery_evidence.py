"""Collect bounded, permission-checked evidence linked from grant updates.

Discord messages are read only when both the bot and the update's author can
see the source channel. External URLs are retained as references; this module
never fetches arbitrary URLs or follows instructions found in linked content.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit


MAX_LINKS = 12
MAX_CONTENT_CHARS = 4_000
MAX_ATTACHMENTS = 10
MAX_EMBEDS = 5
MAX_METADATA_CHARS = 500

_URL_RE = re.compile(r"https?://[^\s<>\]\[(){}]+", re.IGNORECASE)
_DISCORD_JUMP_RE = re.compile(
    r"^https?://(?:www\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?$",
    re.IGNORECASE,
)
_EXTERNAL_HOSTS = {"github.com", "www.github.com", "youtube.com", "www.youtube.com", "youtu.be", "www.youtu.be"}


def _clip(value: Any, limit: int = MAX_METADATA_CHARS) -> str:
    return str(value or "")[:limit]


def _urls(content: str) -> list[str]:
    found: list[str] = []
    for match in _URL_RE.findall(content or ""):
        # Sentence punctuation commonly trails a URL; keep URL path punctuation
        # otherwise intact.
        url = match.rstrip(".,!?;:")
        if url and url not in found:
            found.append(url)
    return found


def _attachment_metadata(message: Any) -> list[dict[str, Any]]:
    rows = []
    for attachment in list(getattr(message, "attachments", ()) or ())[:MAX_ATTACHMENTS]:
        rows.append({
            "id": _clip(getattr(attachment, "id", "")),
            "filename": _clip(getattr(attachment, "filename", "")),
            "content_type": _clip(getattr(attachment, "content_type", "")),
            "size": getattr(attachment, "size", None),
            "url": _clip(getattr(attachment, "url", ""), 2_000),
            "inspected": False,
        })
    return rows


def _embed_metadata(message: Any) -> list[dict[str, str]]:
    rows = []
    for embed in list(getattr(message, "embeds", ()) or ())[:MAX_EMBEDS]:
        rows.append({
            "title": _clip(getattr(embed, "title", "")),
            "description": _clip(getattr(embed, "description", ""), 1_000),
            "url": _clip(getattr(embed, "url", ""), 2_000),
        })
    return rows


def _entry_id(kind: str, value: str) -> str:
    if kind == "discord":
        return f"discord:{value}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"external:{digest}"


def _record(*, kind: str, value: str, status: str, author_id: str | None = None,
            content: str = "", attachments: list[dict] | None = None,
            embeds: list[dict] | None = None, error: str | None = None) -> dict:
    record = {
        "id": _entry_id(kind, value),
        "url": value,
        "status": status,
        "author_id": author_id,
        "content": content[:MAX_CONTENT_CHARS],
        "attachments": attachments or [],
        "embeds": embeds or [],
        "reference_type": "discord_message" if kind == "discord" else "external",
    }
    if error:
        record["error"] = error
    return record


def _failure(url: str, why: str) -> dict:
    match = _DISCORD_JUMP_RE.match(url)
    record = _record(kind="discord", value=url, status="unavailable", error=why)
    if match:
        guild_id, channel_id, message_id = match.groups()
        record["id"] = f"discord:{guild_id}:{channel_id}:{message_id}"
    return record


def _is_private_thread(channel: Any) -> bool:
    """Detect private threads explicitly; permission checks may reflect parent."""
    is_private = getattr(channel, "is_private", None)
    if is_private is not None:
        try:
            if bool(is_private() if callable(is_private) else is_private):
                return True
        except Exception:
            return True
    channel_type = getattr(channel, "type", None)
    value = getattr(channel_type, "value", channel_type)
    return value == 12 or "private_thread" in str(channel_type).lower()


async def _collect_discord(bot: Any, source_message: Any, url: str, match: re.Match) -> dict:
    guild_id, channel_id, message_id = map(int, match.groups())
    guild = getattr(source_message, "guild", None)
    if guild is None or guild_id != getattr(guild, "id", None):
        return _failure(url, "foreign_guild")

    # Resolve only within the source guild. No arbitrary channel fetches.
    channel = None
    resolver = getattr(guild, "get_channel_or_thread", None)
    if callable(resolver):
        channel = resolver(channel_id)
    if channel is None:
        channel = getattr(guild, "get_channel", lambda _id: None)(channel_id)
    if channel is None:
        return _failure(url, "channel_unavailable")
    if _is_private_thread(channel):
        return _failure(url, "private_thread_not_public")

    author = getattr(source_message, "author", None)
    applicant_id = getattr(author, "id", None)
    applicant = getattr(guild, "get_member", lambda _id: None)(applicant_id)
    bot_member = getattr(guild, "me", None)
    default_role = getattr(guild, "default_role", None)
    if applicant is None or bot_member is None or default_role is None:
        return _failure(url, "permissions_unverifiable")
    try:
        bot_perms = channel.permissions_for(bot_member)
        applicant_perms = channel.permissions_for(applicant)
        public_perms = channel.permissions_for(default_role)
        if not (getattr(bot_perms, "view_channel", False) and getattr(bot_perms, "read_message_history", False)):
            return _failure(url, "bot_cannot_read_channel")
        if not (getattr(applicant_perms, "view_channel", False) and getattr(applicant_perms, "read_message_history", False)):
            return _failure(url, "applicant_cannot_read_channel")
        # Grant updates and their acknowledgements can become public evidence.
        # Do not copy linked content out of private channels, even if the bot
        # and applicant can both read it.
        if not (getattr(public_perms, "view_channel", False) and getattr(public_perms, "read_message_history", False)):
            return _failure(url, "channel_not_publicly_readable")
    except Exception:
        return _failure(url, "permissions_unverifiable")

    try:
        linked = await channel.fetch_message(message_id)
    except Exception as exc:
        # Discord exceptions differ across discord.py forks; keep the public
        # result explicit without exposing exception text.
        name = type(exc).__name__.lower()
        reason = "forbidden" if "forbidden" in name else "not_found" if "notfound" in name else "fetch_failed"
        return _failure(url, reason)

    linked_guild = getattr(linked, "guild", None)
    if linked_guild is not None and getattr(linked_guild, "id", None) != guild_id:
        return _failure(url, "foreign_guild")
    linked_channel = getattr(linked, "channel", None)
    if getattr(linked_channel, "id", channel_id) != channel_id:
        return _failure(url, "channel_mismatch")
    linked_id = str(getattr(linked, "id", message_id))
    record = _record(
        kind="discord", value=f"https://discord.com/channels/{guild_id}/{channel_id}/{linked_id}",
        status="available", author_id=str(getattr(getattr(linked, "author", None), "id", "")) or None,
        content=_clip(getattr(linked, "content", ""), MAX_CONTENT_CHARS),
        attachments=_attachment_metadata(linked), embeds=_embed_metadata(linked),
    )
    record["id"] = f"discord:{guild_id}:{channel_id}:{linked_id}"
    return record


async def collect_delivery_evidence(bot: Any, message: Any) -> list[dict]:
    """Collect evidence linked from one grant progress message.

    ``bot`` is the Discord client; ``message`` is the applicant's update.
    The update itself is handled by the caller and is deliberately not copied
    into this list. Linked Discord content is untrusted input. Attachments on
    the update itself are represented as metadata-only, unverified evidence.
    """
    content = getattr(message, "content", "") or ""
    results: list[dict] = []
    source_attachments = _attachment_metadata(message)
    if source_attachments:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        message_id = getattr(message, "id", None)
        source_url = getattr(message, "jump_url", "") or ""
        if not source_url and guild_id and channel_id and message_id:
            source_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        results.append({
            "id": f"source_attachment:{message_id}" if message_id is not None else f"source_attachment:{hashlib.sha256(source_url.encode()).hexdigest()[:20]}",
            "url": source_url,
            "status": "unverified_reference",
            "author_id": str(getattr(getattr(message, "author", None), "id", "")) or None,
            "content": "",
            "attachments": source_attachments,
            "embeds": [],
            "reference_type": "source_attachment",
        })
    seen: set[str] = set()
    for url in _urls(content)[:MAX_LINKS]:
        if url in seen:
            continue
        seen.add(url)
        jump = _DISCORD_JUMP_RE.match(url)
        if jump:
            results.append(await _collect_discord(bot, message, url, jump))
            continue
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if host in {"discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com"} and parsed.path.startswith("/channels/"):
            results.append(_record(kind="discord", value=url, status="unavailable", error="malformed_link"))
            continue
        if host in _EXTERNAL_HOSTS:
            results.append(_record(kind="external", value=url, status="unverified_reference"))
    return results
