"""Tool definitions and executors for the support agent.


Currently exposes `search_hivemind`, a read-only search over the public
Banodoco hivemind PostgREST endpoint (messages, distillations, resources).
"""
import logging
from typing import Any, Dict, List, Optional, Tuple
import asyncio
from urllib.parse import quote, urlencode

import aiohttp

logger = logging.getLogger('DiscordBot')

HIVEMIND_BASE_URL = 'https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1'
HIVEMIND_API_KEY = 'sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx'

SEARCH_TIMEOUT_SECONDS = 8
DEFAULT_LIMIT = 15
MAX_LIMIT = 30
SNIPPET_MAX_CHARS = 300


def _clamp_limit(raw_limit: Any) -> int:
    """Coerce limit to a sane int: default 15, capped at 30."""
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _tokenize(query: str) -> List[str]:
    """Split a query into non-empty search terms."""
    return [term for term in (part.strip() for part in str(query).split()) if term]


def _ilike(field: str, term: str) -> str:
    return f"{field}.ilike.*{term}*"


def _or_param(pairs: List[Tuple[str, str]]) -> str:
    """Build a PostgREST or=(field.op.*term*,...) expression from field/term pairs."""
    predicates = ','.join(_ilike(field, term) for field, term in pairs)
    return f"or=({predicates})"


# kind -> (view name, select projection, ilike fields)
_HIVEMIND_KINDS = {
    "messages": (
        "message_feed",
        "message_id,content,author_name,channel_name,channel_id,guild_id,created_at,reactions",
        ("content",),
    ),
    "distillations": (
        "distillations",
        "question,answer,cites",
        ("question", "answer"),
    ),
    "resources": (
        "external_resources",
        "kind,title,body,url,author",
        ("title", "body"),
    ),
}


def build_hivemind_url(
    query: str,
    kind: str = "messages",
    channel: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> Optional[str]:
    """Build the PostgREST search URL. Returns None for an empty query/kind."""
    spec = _HIVEMIND_KINDS.get(kind)
    terms = _tokenize(query)
    if not spec or not terms:
        return None
    view, select, fields = spec

    params: List[Tuple[str, str]] = [("select", select)]
    pairs = [(field, term) for term in terms for field in fields]
    params.append(("or", _or_param(pairs)[3:]))
    if kind == "distillations":
        params.append(("status", "in.(pending,approved)"))
    if channel:
        channels = [c.strip() for c in str(channel).split(',') if c.strip()]
        if len(channels) == 1:
            params.append(("channel_name", f"eq.{channels[0]}"))
        elif channels:
            params.append(("channel_name", f"in.({','.join(channels)})"))
    params.append(("order", "created_at.desc"))
    params.append(("limit", str(limit)))

    return f"{HIVEMIND_BASE_URL}/{view}?{urlencode(params, quote_via=quote)}"


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    """GET the URL with the hivemind apikey header and return parsed JSON."""
    async with session.get(
        url,
        headers={"apikey": HIVEMIND_API_KEY},
        timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_SECONDS),
    ) as response:
        if response.status != 200:
            body = await response.text()
            raise RuntimeError(f"hivemind returned HTTP {response.status}: {body[:200]}")
        return await response.json()


def _format_message_result(row: Dict[str, Any]) -> str:
    snippet = ' '.join(str(row.get('content') or '').split())
    if len(snippet) > SNIPPET_MAX_CHARS:
        snippet = snippet[:SNIPPET_MAX_CHARS] + '...'
    date = str(row.get('created_at') or '')[:10]
    jump_url = (
        f"https://discord.com/channels/{row.get('guild_id')}/"
        f"{row.get('channel_id')}/{row.get('message_id')}"
    )
    reactions = row.get('reactions')
    reaction_note = f" | reactions: {reactions}" if reactions else ""
    return (
        f"[{row.get('author_name') or 'unknown'} | #{row.get('channel_name') or '?'}"
        f"{reaction_note} | {date}]\n"
        f"{snippet}\n"
        f"{jump_url}"
    )


def _format_titled_result(row: Dict[str, Any]) -> str:
    snippet = ' '.join(str(row.get('body') or '').split())
    if len(snippet) > SNIPPET_MAX_CHARS:
        snippet = snippet[:SNIPPET_MAX_CHARS] + '...'
    title = row.get('title') or row.get('url') or '(untitled)'
    url_line = f"\n{row.get('url')}" if row.get('url') else ""
    kind_note = f" [{row['kind']}]" if row.get('kind') else ""
    author_note = f" by {row['author']}" if row.get('author') else ""
    return f"{title}{kind_note}{author_note}\n{snippet}{url_line}"


def format_hivemind_results(kind: str, rows: List[Dict[str, Any]]) -> str:
    """Pre-format results into text blocks the LLM can pass straight to reply()."""
    if not rows:
        return "No hivemind results found."
    formatter = _format_message_result if kind == "messages" else _format_titled_result
    blocks = [f"[{i}] {formatter(row)}" for i, row in enumerate(rows, start=1)]
    return '\n\n'.join(blocks)


async def execute_search_hivemind(
    params: Dict[str, Any],
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Any]:
    """Execute the search_hivemind tool: read-only search over the public corpus."""
    try:
        query = str((params or {}).get('query') or '').strip()
        if not query:
            return {"success": False, "error": "query is required"}

        kind = str((params or {}).get('kind') or 'messages').strip().lower()
        if kind not in _HIVEMIND_KINDS:
            valid = ', '.join(sorted(_HIVEMIND_KINDS))
            return {"success": False, "error": f"kind must be one of: {valid}"}

        # Only message_feed has channel_name; ignore the filter for other kinds.
        channel = (params or {}).get('channel') if kind == 'messages' else None
        limit = _clamp_limit((params or {}).get('limit'))
        url = build_hivemind_url(query, kind=kind, channel=channel, limit=limit)
        if url is None:
            return {"success": False, "error": "query is required"}

        owned_session = session is None
        try:
            if owned_session:
                async with aiohttp.ClientSession() as session:
                    rows = await _fetch_json(session, url)
            else:
                rows = await _fetch_json(session, url)
        except asyncio.TimeoutError:
            return {"success": False, "error": "hivemind search timed out"}
        except aiohttp.ClientError as exc:
            return {"success": False, "error": f"hivemind request failed: {exc}"}

        if not isinstance(rows, list):
            rows = []
        return {
            "success": True,
            "results": rows,
            "formatted": format_hivemind_results(kind, rows),
        }
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop
        logger.exception("search_hivemind failed")
        return {"success": False, "error": str(exc)}

# ========== Tool Definitions (Anthropic format) ==========

TOOLS = [
    {
        "name": "search_hivemind",
        "description": (
            "Search the Banodoco community knowledge base (hivemind): Discord chat "
            "history, curated Q&A distillations, and shared external resources "
            "(articles, transcripts, workflows). Use to find real precedent, "
            "recommendations, and links before answering workflow/model questions. "
            "Returns pre-formatted result blocks with citations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms, e.g. 'wan animate workflow'"
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "Optional channel_name filter (messages only). Comma-separated "
                        "list searches multiple channels, e.g. 'wan_chatter,wan_comfyui'"
                    )
                },
                "kind": {
                    "type": "string",
                    "enum": ["messages", "distillations", "resources"],
                    "description": (
                        "What to search: messages (raw Discord chat, default), "
                        "distillations (curated Q&A pairs), resources (shared links/articles)"
                    )
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 15, max 30)"
                }
            },
            "required": ["query"]
        }
    },
]
