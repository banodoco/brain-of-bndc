"""Daily workflow-source scan: discover new ComfyUI workflow links shared in
the archived Discord corpus and contribute them to the public Hivemind
knowledge corpus as ``workflow`` resources.

Runs on the same daily cadence as the digest (see SummarizerCog). It reuses
the bot's own Supabase archive (``discord_messages``, populated by the hourly
message fetch) as the source of truth, applies lightweight quality gates
(channel scope, author whitelist, praise signals, URL patterns), and POSTs
each new workflow to the Hivemind ``contribute-resource`` edge function using
an idempotent ``source + external_id`` key so re-runs are no-ops.

This module is intentionally dependency-light (asyncio + aiohttp + stdlib) so
it can run inside the bot process without pulling in the vibecomfy package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("DiscordBot")

# ---- Channel scope ---------------------------------------------------------

# Channels whose workflow links we consider high-signal. minimax_h3_* are the
# MiniMax H3 (Hailuo 3) release-week channels; the rest are the long-standing
# quality channels.
QUALITY_CHANNELS: Tuple[str, ...] = (
    "minimax_h3_resources",
    "minimax_h3_chatter",
    "minimax_h3_gens",
    "daily_summaries",
    "top_gens",
    "resources",
    "comfyui",
    "wan_comfyui",
)

# Repeated quality contributors (from community observation; extend freely).
AUTHOR_WHITELIST: Tuple[str, ...] = (
    "kijai",
    "ablejones",
    "seitanism",
    "lukeg89",
    "lumifel",
    "crinklypaper",
    "nebsh",
    "avataraim",
    "embedding-shapes",
    "nynxz",
    "manu_le_surikhate_gamer",
    "god_is_a_lie",
    "sftawil",
    "arts bro",
    "tsolful",
    "vrgamedevgirl84",
    "galaxytimemachine",
    "mdkb",
    "zelgo_",
    "runex",
    "lym0",
    "antirez",
)

_PRAISE_RE = re.compile(r"\b(sick|great|amazing|best|insane|quality|love it|wicked|so good)\b", re.IGNORECASE)

# URL patterns that point at a ComfyUI workflow (JSON graph, PNG-embedded
# graph, or a page that hosts one).
_WORKFLOW_URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)
_WORKFLOW_HOST_PATTERN = re.compile(
    r"(raw\.githubusercontent\.com|github\.com|comfyworkflows\.com|"
    r"civitai\.com|huggingface\.co|cdn\.discordapp\.com|"
    r"media\.discordapp\.net)",
    re.IGNORECASE,
)
_JSON_SUFFIX_RE = re.compile(r"\.json(?:$|[?#])", re.IGNORECASE)
_PNG_SUFFIX_RE = re.compile(r"\.(?:png|webp)(?:$|[?#])", re.IGNORECASE)

# ComfyUI workflow graphs are JSON files (or PNG/WebP with an embedded graph).
# Repo homepages, blob browser pages, and generation images are NOT workflows.
_BLOB_PAGE_RE = re.compile(r"/blob/[^/]+/[^/]+\.py(?:$|[?#])", re.IGNORECASE)
_REPO_HOME_RE = re.compile(r"^https?://github\.com/[^/]+/[^/]+/?$", re.IGNORECASE)

# Defaults
DEFAULT_LOOKBACK_HOURS = 26  # a little more than one daily cycle
DEFAULT_MIN_REACTIONS = 2
DEFAULT_MAX_CANDIDATES_PER_RUN = 50

SOURCE = "banodoco-daily-scan"
KIND = "workflow"

# URL of the hivemind anonymous resource endpoint; derived from SUPABASE_URL
# unless HIVEMIND_CONTRIBUTE_URL is set explicitly.
DEFAULT_CONTRIBUTE_PATH = "/functions/v1/contribute-resource"


# ---- Quality gates ---------------------------------------------------------


def _reaction_count(message: Dict[str, Any]) -> int:
    """Best-effort reaction count from the archived message row.

    The ``discord_messages`` archive stores the aggregate in ``reaction_count``
    (see structure.md:180); ``reactions`` is not a column there. Accept both
    shapes so tests and future schemas keep working.
    """
    raw = message.get("reaction_count", message.get("reactions"))
    if isinstance(raw, int):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("count"), int):
        return int(raw["count"])
    if isinstance(raw, list):
        return sum(int(item.get("count") or 0) for item in raw if isinstance(item, dict))
    return 0


def _author_score(author_name: Optional[str]) -> int:
    if not author_name:
        return 0
    lowered = re.sub(r"[^a-z0-9]", "", author_name.strip().lower())
    if lowered in {re.sub(r"[^a-z0-9]", "", name) for name in AUTHOR_WHITELIST}:
        return 3
    return 0


def _praise_score(content: str) -> int:
    return 1 if _PRAISE_RE.search(content or "") else 0


def _channel_score(channel_name: Optional[str]) -> int:
    if not channel_name:
        return 0
    if channel_name in {"minimax_h3_resources", "resources"}:
        return 2  # explicitly for sharing resources
    # daily_summaries / top_gens are curated digests and generation highlights,
    # not workflow-sharing channels — never auto-pass them (their links are
    # generation images and model chatter, not workflow graphs).
    if channel_name in {"minimax_h3_chatter", "comfyui", "wan_comfyui"}:
        return 1
    return 0


def _looks_like_workflow_url(url: str, filename: str = "") -> bool:
    """A URL is a workflow candidate only if it points at a workflow FILE.

    Strict on purpose: the corpus is public, so repo homepages, GitHub blob
    browser pages for .py files, and generation images must not be admitted.
    """
    if _BLOB_PAGE_RE.search(url):
        return False
    if _REPO_HOME_RE.match(url):
        return False
    name = filename or url.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    if _JSON_SUFFIX_RE.search(name):
        return True
    # PNG/WebP can embed a ComfyUI graph, but only from known hosts — a Discord
    # attachment or a raw GitHub file, never an arbitrary image host.
    if _PNG_SUFFIX_RE.search(name) and (
        "cdn.discordapp.com" in url or "media.discordapp.net" in url or "raw.githubusercontent.com" in url
    ):
        return True
    return False


def _extract_workflow_urls(message: Dict[str, Any]) -> List[str]:
    """Pull candidate workflow URLs from message content + attachments."""
    candidates: List[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        url = url.rstrip(".,;:)]}")
        if url in seen:
            return
        seen.add(url)
        candidates.append(url)

    content = message.get("content") or ""
    for match in _WORKFLOW_URL_RE.finditer(content):
        url = match.group(0)
        if _WORKFLOW_HOST_PATTERN.search(url) and _looks_like_workflow_url(url):
            add(url)

    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            url = attachment.get("url") or attachment.get("proxy_url")
            filename = attachment.get("filename") or ""
            if not url:
                continue
            if _looks_like_workflow_url(url, filename):
                add(url)
    return candidates


def _passes_quality_gates(message: Dict[str, Any], *, min_reactions: int) -> bool:
    """A candidate passes if any strong signal is present."""
    channel_name = message.get("channel_name") or ""
    author = message.get("author_name")
    content = message.get("content") or ""
    reactions = _reaction_count(message)
    if _channel_score(channel_name) >= 2:
        return True
    if _author_score(author) >= 2:
        return True
    if reactions >= min_reactions:
        return True
    # Praise words in a resource-capable channel are a weak but useful signal.
    return _channel_score(channel_name) >= 1 and _praise_score(content) >= 1 and len(content.split()) >= 5


# ---- Envelope + Hivemind API -----------------------------------------------


def _contribute_url() -> str:
    explicit = os.getenv("HIVEMIND_CONTRIBUTE_URL")
    if explicit:
        return explicit
    supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    return f"{supabase_url}{DEFAULT_CONTRIBUTE_PATH}"


def _canonical_url(url: str) -> str:
    """Strip signed query params (Discord CDN ``?ex=&hm=``) that change on
    refresh and would otherwise break idempotency and leave dead links."""
    base = url.split("#", 1)[0].split("?", 1)[0]
    return base.rstrip("/")


def _external_id(message: Dict[str, Any], url: str, index: int) -> str:
    """Idempotency key: source message + canonical URL, stable across re-runs."""
    message_id = message.get("message_id")
    suffix = re.sub(r"[^A-Za-z0-9._-]+", "_", _canonical_url(url))[-120:]
    return f"banodoco-discord:{message_id}:{index}:{suffix}"


_GENERIC_LEAF_RE = re.compile(r"^(image|img|main|tree|blob|raw|workflow|workflows|example|examples|json|download|view|index)$", re.IGNORECASE)
_CDN_PATH_RE = re.compile(r"/attachments/\d+/\d+/(?P<filename>[^/?#]+)", re.IGNORECASE)


def _title_from_url(url: str) -> str:
    """Derive a readable title from a workflow URL."""
    path = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    # Discord CDN attachments carry the real filename in the path.
    cdn_match = _CDN_PATH_RE.search(url)
    if cdn_match:
        leaf = cdn_match.group("filename")
    else:
        leaf = path.rsplit("/", 1)[-1] if "/" in path else path
    leaf = re.sub(r"\.(json|png|webp)$", "", leaf, flags=re.IGNORECASE)
    leaf = re.sub(r"[-_]+", " ", leaf).strip()
    if leaf and not _GENERIC_LEAF_RE.match(leaf):
        return leaf[:80].title()
    return "Discord workflow share"


def _build_envelope(message: Dict[str, Any], url: str, index: int) -> Dict[str, Any]:
    """Build the contribute-resource request body for one workflow URL."""
    content = (message.get("content") or "").strip()
    snippet = " ".join(content.split())[:400]
    body_lines = [f"Shared in #{message.get('channel_name') or '?'}."]
    if message.get("author_name"):
        body_lines.append(f"Author: {message['author_name']}")
    if snippet:
        body_lines.append(f"Context: {snippet}")
    body_lines.append(f"Source URL: {url}")
    body = "\n".join(body_lines)
    canonical = _canonical_url(url)

    metadata = {
        "provenance": {
            "source": SOURCE,
            "external_id": _external_id(message, url, index),
            "discord_message_id": str(message.get("message_id")),
            "discord_channel": message.get("channel_name"),
            "discord_author": message.get("author_name"),
            "discord_message_url": (
                f"https://discord.com/channels/{message.get('guild_id')}/"
                f"{message.get('channel_id')}/{message.get('message_id')}"
            ),
            "reactions": _reaction_count(message),
            "discovered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
    }
    title = _title_from_url(url)
    if title == "Discord workflow share" and snippet:
        # Fall back to the message's own words when the URL leaf is generic.
        title = " ".join(snippet.split())[:70].title()
    return {
        "action": "add_resource",
        "data": {
            "kind": KIND,
            "source": SOURCE,
            "external_id": _external_id(message, url, index),
            "title": title,
            "body": body,
            # Canonical URL without signed tokens: stable and long-lived.
            "url": canonical,
            "metadata": metadata,
        },
    }


async def _preflight_existing(session: Any, envelopes: List[Dict[str, Any]], supabase_url: str, service_key: str) -> set[str]:
    """Return the set of external_ids already present in Hivemind."""
    existing: set[str] = set()
    if not envelopes:
        return existing
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    # Batch by chunks of 50; one query per chunk with OR of external_id filters.
    chunk_size = 50
    for start in range(0, len(envelopes), chunk_size):
        chunk = envelopes[start : start + chunk_size]
        or_clause = ",".join(
            f'external_id.eq."{env["data"]["external_id"]}"' for env in chunk
        )
        params = {
            "select": "external_id",
            "source": f"eq.{SOURCE}",
            "or": f"({or_clause})",
            "limit": "100",
        }
        try:
            async with session.get(
                f"{supabase_url}/rest/v1/external_resources",
                params=params,
                headers=headers,
            ) as response:
                if response.status == 200:
                    rows = await response.json()
                    for row in rows:
                        if isinstance(row, dict) and row.get("external_id"):
                            existing.add(row["external_id"])
                else:
                    logger.warning(
                        "workflow_source_scan: preflight returned %s for chunk %d",
                        response.status,
                        start // chunk_size,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("workflow_source_scan: preflight error: %s", exc, exc_info=True)
    return existing


async def _post_envelope(session: Any, envelope: Dict[str, Any], contribute_url: str) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    try:
        async with session.post(contribute_url, json=envelope, headers=headers, timeout=120) as response:
            text = await response.text()
            if response.status in (200, 201):
                return {"status": "uploaded", "response": text[:200]}
            if response.status == 409:
                return {"status": "skipped_duplicate", "response": text[:200]}
            logger.warning(
                "workflow_source_scan: contribute returned %s: %s",
                response.status,
                text[:200],
            )
            return {"status": "error", "error": f"HTTP {response.status}: {text[:200]}"}
    except Exception as exc:  # noqa: BLE001
        logger.error("workflow_source_scan: contribute POST error: %s", exc, exc_info=True)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


# ---- Main entry ------------------------------------------------------------


async def _fetch_recent_messages(query_handler: Any, *, since: datetime, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch archived messages in the lookback window from the bot's archive.

    Scoped to a guild when known so we never scan other guilds' archives.
    """
    if query_handler is None:
        return []
    try:
        return await query_handler.get_messages_in_range(
            since,
            datetime.now(timezone.utc),
            guild_id=guild_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("workflow_source_scan: failed to fetch recent messages: %s", exc, exc_info=True)
        return []


def _resolve_guild_id(bot: Any, *, environment: str) -> Optional[int]:
    """Resolve the guild to scan, mirroring the digest's resolution."""
    server_config = getattr(bot, "server_config", None)
    if server_config is not None:
        try:
            for guild_cfg in server_config.get_guilds_to_archive():
                gid = guild_cfg.get("guild_id")
                if gid is not None:
                    return int(gid)
        except Exception:  # noqa: BLE001
            logger.debug("workflow_source_scan: server_config guild resolution failed")
    dev = bool(getattr(bot, "dev_mode", False))
    if dev:
        return int(os.getenv("DEV_GUILD_ID") or "1076117621407223829")
    return int(os.getenv("GUILD_ID") or "1076117621407223829")


async def run_workflow_source_scan(
    bot: Any,
    storage_handler: Any,
    *,
    environment: str = "prod",
    now: Optional[datetime] = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    min_reactions: int = DEFAULT_MIN_REACTIONS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_RUN,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Scan the last ``lookback_hours`` of archived Discord messages for new
    ComfyUI workflow links and contribute them to Hivemind.

    Returns a summary dict with counts of scanned/uploaded/skipped items.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    db_handler = getattr(bot, "db_handler", None)
    query_handler = getattr(db_handler, "query_handler", None)
    if query_handler is None:
        logger.warning("workflow_source_scan: no query_handler available; skipping")
        return {"status": "skipped", "reason": "no_query_handler", "scanned": 0, "uploaded": 0, "skipped": 0}

    guild_id = _resolve_guild_id(bot, environment=environment)
    messages = await _fetch_recent_messages(query_handler, since=since, guild_id=guild_id)
    logger.info(
        "workflow_source_scan: fetched %d messages in lookback window (guild=%s)",
        len(messages),
        guild_id,
    )

    # Channel name resolution: the archive stores channel_id; map to names via
    # the shared supabase client (discord_channels table).
    channel_names: Dict[int, str] = {}
    supabase_client = getattr(storage_handler, "supabase_client", None)
    if supabase_client is not None:
        try:
            result = await asyncio.to_thread(
                supabase_client.table("discord_channels")
                .select("channel_id,channel_name")
                .limit(10000)
                .execute
            )
            for row in result.data or []:
                cid = row.get("channel_id")
                if cid is not None:
                    channel_names[int(cid)] = str(row.get("channel_name") or cid)
        except Exception:  # noqa: BLE001
            logger.debug("workflow_source_scan: channel map unavailable; using raw ids")

    # Deterministic ordering: most-reacted first, then newest, so the cap does
    # not silently favor whatever the archive returns first (it is unordered).
    messages = sorted(
        messages,
        key=lambda m: (-_reaction_count(m), str(m.get("created_at") or "")),
    )

    candidates: List[Tuple[Dict[str, Any], str, int]] = []
    seen_pairs: set[Tuple[str, str]] = set()
    for message in messages:
        channel_id = message.get("channel_id")
        channel_name = channel_names.get(int(channel_id), str(channel_id)) if channel_id is not None else ""
        message["channel_name"] = channel_name
        if channel_name not in QUALITY_CHANNELS:
            continue
        if not _passes_quality_gates(message, min_reactions=min_reactions):
            continue
        urls = _extract_workflow_urls(message)
        for index, url in enumerate(urls):
            key = (str(message.get("message_id")), url)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            candidates.append((message, url, index))
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    logger.info("workflow_source_scan: %d candidate workflow links", len(candidates))
    if not candidates:
        return {"status": "ok", "scanned": len(messages), "uploaded": 0, "skipped": 0, "candidates": 0}

    envelopes = [_build_envelope(message, url, index) for message, url, index in candidates]
    if dry_run:
        return {
            "status": "dry_run",
            "scanned": len(messages),
            "uploaded": 0,
            "skipped": 0,
            "candidates": len(candidates),
            "envelopes": envelopes,
        }

    import aiohttp

    supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_KEY") or ""
    contribute_url = _contribute_url()
    uploaded = 0
    skipped = 0
    errors = 0
    async with aiohttp.ClientSession() as session:
        existing = await _preflight_existing(session, envelopes, supabase_url, service_key)
        for envelope in envelopes:
            external_id = envelope["data"]["external_id"]
            if external_id in existing:
                skipped += 1
                continue
            result = await _post_envelope(session, envelope, contribute_url)
            if result["status"] == "uploaded":
                uploaded += 1
            elif result["status"] == "skipped_duplicate":
                skipped += 1
            else:
                errors += 1

    return {
        "status": "ok",
        "scanned": len(messages),
        "uploaded": uploaded,
        "skipped": skipped,
        "errors": errors,
        "candidates": len(candidates),
    }
