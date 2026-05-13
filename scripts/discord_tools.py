#!/usr/bin/env python3
"""
Discord Tools — unified interface for searching, browsing, and interacting with Discord.

Wraps Supabase DB queries, Discord REST API, and media operations into clean
functions that an agent (or CLI) can call directly.

== DB-backed (indexed messages) ==
    python scripts/discord_tools.py search "query" --days 7
    python scripts/discord_tools.py top --days 7 --min-reactions 3
    python scripts/discord_tools.py top CHANNEL_ID --days 7
    python scripts/discord_tools.py messages CHANNEL_ID --days 7
    python scripts/discord_tools.py context MESSAGE_ID
    python scripts/discord_tools.py thread MESSAGE_ID
    python scripts/discord_tools.py user "username" --days 7
    python scripts/discord_tools.py channels
    python scripts/discord_tools.py summaries --days 7

== Discord API (live, not limited to indexed data) ==
    python scripts/discord_tools.py api-get CHANNEL_ID                    # fetch recent messages
    python scripts/discord_tools.py api-get CHANNEL_ID MESSAGE_ID         # fetch single message
    python scripts/discord_tools.py api-post CHANNEL_ID "message text"    # send a message
    python scripts/discord_tools.py api-edit CHANNEL_ID MESSAGE_ID "new"  # edit a bot message
    python scripts/discord_tools.py api-delete CHANNEL_ID MESSAGE_ID      # delete a bot message
    python scripts/discord_tools.py api-reply CHANNEL_ID MESSAGE_ID "txt" # reply to a message
    python scripts/discord_tools.py api-react CHANNEL_ID MESSAGE_ID 👍    # add reaction
    python scripts/discord_tools.py api-threads CHANNEL_ID                # list threads
    python scripts/discord_tools.py api-upload CHANNEL_ID FILE "caption"  # upload file

== Media ==
    python scripts/discord_tools.py refresh MESSAGE_ID [MESSAGE_ID ...]   # refresh expired CDN URLs
    python scripts/discord_tools.py get-urls MESSAGE_ID [MESSAGE_ID ...]  # get attachment URLs from DB
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import calendar

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.common.urls import message_jump_url

_DEFAULT_GUILD_ID = (
    int(os.getenv('TARGET_GUILD_ID', os.getenv('GUILD_ID', os.getenv('DEV_GUILD_ID', '0'))))
    or None
)
_active_guild_id = _DEFAULT_GUILD_ID
MSG_SELECT = (
    'message_id, guild_id, channel_id, thread_id, author_id, content, created_at, '
    'attachments, reaction_count, reactors, reference_id'
)


def _month_to_date_range(month: str):
    """Convert 'YYYY-MM' to (start_iso, end_iso) covering the full month."""
    dt = datetime.strptime(month, '%Y-%m')
    _, last_day = calendar.monthrange(dt.year, dt.month)
    start = dt.replace(day=1).isoformat()
    end = dt.replace(day=last_day, hour=23, minute=59, second=59).isoformat()
    return start, end


def _resolve_date_range(days: Optional[int] = None,
                        month: Optional[str] = None) -> str:
    """Return a cutoff ISO string from --days, or a (start, end) tuple from --month.

    Returns either a single cutoff string (for gte queries) or a tuple of
    (start, end) strings when month is specified.
    """
    if month:
        return _month_to_date_range(month)
    return (datetime.utcnow() - timedelta(days=days or 7)).isoformat()


# ============================================================
# Clients
# ============================================================

def _supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        sys.exit(1)
    return create_client(url, key)


def _discord_headers():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN must be set")
        sys.exit(1)
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _discord_headers_multipart():
    """Headers for file upload (no Content-Type — requests sets it)."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN must be set")
        sys.exit(1)
    return {"Authorization": f"Bot {token}"}


# ============================================================
# Helpers
# ============================================================

def _member_name(m: Dict) -> str:
    if m.get('include_in_updates') is False:
        return 'A community member'
    return m.get('server_nick') or m.get('global_name') or m.get('username') or 'Unknown'


def _set_active_guild_id(guild_id: Optional[int]):
    global _active_guild_id
    _active_guild_id = guild_id


def _get_active_guild_id() -> Optional[int]:
    return _active_guild_id


def _apply_guild_filter(query):
    guild_id = _get_active_guild_id()
    if guild_id:
        query = query.eq('guild_id', guild_id)
    return query


def _member_map(db, member_ids: List[int]) -> Dict[int, str]:
    if not member_ids:
        return {}

    member_map: Dict[int, str] = {}
    active_guild_id = _get_active_guild_id()
    if active_guild_id:
        guild_rows = (
            db.table('guild_members')
            .select('member_id, server_nick')
            .eq('guild_id', active_guild_id)
            .in_('member_id', member_ids)
            .execute()
        )
        for row in (guild_rows.data or []):
            if row.get('server_nick'):
                member_map[row['member_id']] = row['server_nick']

    missing_ids = [member_id for member_id in member_ids if member_id not in member_map]
    if missing_ids:
        members = db.table('members').select(
            'member_id, username, global_name, server_nick, include_in_updates'
        ).in_('member_id', missing_ids).execute()
        for member in (members.data or []):
            member_map[member['member_id']] = _member_name(member)

    return member_map


def _enrich(messages: List[Dict], channel_names: Dict = None,
            resolve_reactors: bool = False) -> List[Dict]:
    """Add author_name (and optionally channel_name / reactor_names) to messages."""
    if not messages:
        return messages
    db = _supabase()
    ids = list({m['author_id'] for m in messages})

    # Collect all reactor IDs if we need to resolve them
    all_reactor_ids = set()
    for msg in messages:
        reactors = msg.get('reactors', [])
        if isinstance(reactors, str):
            try:
                reactors = json.loads(reactors)
            except Exception:
                reactors = []
        msg['_reactor_ids'] = reactors if isinstance(reactors, list) else []
        msg['unique_reactor_count'] = len(msg['_reactor_ids'])
        if resolve_reactors:
            all_reactor_ids.update(msg['_reactor_ids'])

    # Build one combined member map for authors + reactors
    all_ids = list(set(ids) | all_reactor_ids)
    mmap = _member_map(db, all_ids)

    for msg in messages:
        msg['author_name'] = mmap.get(msg['author_id'], 'Unknown')
        if channel_names:
            msg['channel_name'] = channel_names.get(msg['channel_id'], 'Unknown')
        if resolve_reactors:
            msg['reactor_names'] = [mmap.get(rid, str(rid)) for rid in msg['_reactor_ids']]
        del msg['_reactor_ids']
    return messages


def _channel_map(exclude_nsfw: bool = True) -> Dict:
    db = _supabase()
    q = db.table('discord_channels').select('channel_id, channel_name, nsfw')
    q = _apply_guild_filter(q)
    rows = q.execute()
    if exclude_nsfw:
        return {r['channel_id']: r['channel_name'] for r in rows.data if not r.get('nsfw')}
    return {r['channel_id']: r['channel_name'] for r in rows.data}


def _fmt(msg: Dict) -> str:
    """Format a single message for display."""
    lines = []
    author = msg.get('author_name', 'Unknown')
    ts = msg.get('created_at', '')[:16].replace('T', ' ')
    ch = f" #{msg['channel_name']}" if msg.get('channel_name') else ''
    lines.append(f"**{author}** ({ts}){ch}")
    lines.append(f"  ID: {msg['message_id']}")
    if msg.get('content'):
        c = msg['content'][:400]
        if len(msg['content']) > 400:
            c += '...'
        lines.append(f"  {c}")
    rc = msg.get('reaction_count', 0)
    urc = msg.get('unique_reactor_count', 0)
    if rc:
        parts = [f"Reactions: {rc}"]
        if urc and urc != rc:
            parts.append(f"({urc} unique)")
        lines.append(f"  {' '.join(parts)}")
    if msg.get('reactor_names'):
        lines.append(f"  Reacted by: {', '.join(msg['reactor_names'])}")
    atts = msg.get('attachments') or []
    if isinstance(atts, str):
        try:
            atts = json.loads(atts)
        except Exception:
            atts = []
    if atts:
        for a in atts[:3]:
            lines.append(f"  Attachment: {a.get('filename', 'file')}")
    lines.append(f"  {message_jump_url(msg.get('guild_id') or _get_active_guild_id() or 0, msg['channel_id'], msg['message_id'], thread_id=msg.get('thread_id'))}")
    return '\n'.join(lines)


# ============================================================
# DB-backed queries
# ============================================================

def find_messages(query: str = '', days: int = None, month: str = None,
                  channel_id: int = None, author_id: int = None,
                  min_reactions: int = 0, has_media: bool = False,
                  limit: int = 30, sort: str = 'date',
                  exclude_nsfw: bool = True,
                  show_reactors: bool = False,
                  allowed_channel_ids: List[int] = None) -> List[Dict]:
    """Unified message search — the single data-fetching function.

    All other search functions (search, top, user) delegate to this.
    The bot's admin_chat tools can also call this directly.

    Args:
        query: Text search (case-insensitive substring match)
        days: Filter to last N days (mutually exclusive with month)
        month: Filter to YYYY-MM month (overrides days)
        channel_id: Filter to specific channel
        author_id: Filter to specific author
        min_reactions: Minimum reaction count
        has_media: Only posts with attachments
        limit: Max results
        sort: 'date', 'reactions', or 'unique_reactors'
        exclude_nsfw: Exclude NSFW channels (ignored if channel_id or allowed_channel_ids set)
        show_reactors: Resolve reactor member IDs to names
        allowed_channel_ids: Restrict to these channel IDs (for permission filtering)
    """
    db = _supabase()

    q = db.table('discord_messages').select(MSG_SELECT)

    # Date filtering
    if month or days:
        date_range = _resolve_date_range(days, month)
        if isinstance(date_range, tuple):
            q = q.gte('created_at', date_range[0]).lte('created_at', date_range[1])
        else:
            q = q.gte('created_at', date_range)

    # Guild filter
    if _get_active_guild_id():
        q = q.eq('guild_id', _get_active_guild_id())

    # Channel filtering
    if channel_id:
        q = q.eq('channel_id', channel_id)
    elif allowed_channel_ids is not None:
        q = q.in_('channel_id', allowed_channel_ids)
    elif exclude_nsfw:
        cmap = _channel_map(exclude_nsfw=True)
        q = q.in_('channel_id', list(cmap.keys()))

    # Content filters
    if query:
        q = q.ilike('content', f'%{query}%')
    if author_id:
        q = q.eq('author_id', author_id)
    if min_reactions:
        q = q.gte('reaction_count', min_reactions)
    if has_media:
        q = q.neq('attachments', [])

    # Sort and limit
    if sort == 'unique_reactors':
        q = q.order('reaction_count', desc=True).limit(limit * 3)
    elif sort == 'reactions':
        q = q.order('reaction_count', desc=True).limit(limit)
    else:
        q = q.order('created_at', desc=True).limit(limit)

    messages = q.execute().data

    # Enrich
    cmap = _channel_map(exclude_nsfw=False)
    _enrich(messages, channel_names=cmap, resolve_reactors=show_reactors)

    # Client-side sort for unique_reactors
    if sort == 'unique_reactors':
        messages.sort(key=lambda m: m.get('unique_reactor_count', 0), reverse=True)
        messages = messages[:limit]

    return messages


def search(query: str, days: int = 7, month: str = None, channel_id: int = None,
           limit: int = 30, exclude_nsfw: bool = True,
           show_reactors: bool = False) -> List[Dict]:
    """Search messages by content (case-insensitive)."""
    return find_messages(query=query, days=days, month=month,
                         channel_id=channel_id, limit=limit,
                         sort='date', exclude_nsfw=exclude_nsfw,
                         show_reactors=show_reactors)


def top(days: int = 7, month: str = None, min_reactions: int = 3,
        limit: int = 30, channel_id: int = None,
        exclude_nsfw: bool = True, show_reactors: bool = False) -> List[Dict]:
    """Top messages by reaction count, server-wide or in a channel."""
    return find_messages(days=days, month=month, min_reactions=min_reactions,
                         limit=limit, channel_id=channel_id, sort='reactions',
                         exclude_nsfw=exclude_nsfw, show_reactors=show_reactors)


def messages(channel_id: int, days: int = 7, limit: int = 50) -> List[Dict]:
    """Recent messages from a channel."""
    db = _supabase()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    q = db.table('discord_messages').select(MSG_SELECT) \
        .eq('channel_id', channel_id).gte('created_at', cutoff) \
        .order('created_at', desc=True).limit(limit)
    if _get_active_guild_id():
        q = q.eq('guild_id', _get_active_guild_id())
    result = q.execute()
    return _enrich(result.data)


def context(message_id: int, surrounding: int = 5) -> Dict[str, Any]:
    """Get a message with surrounding context and replies."""
    db = _supabase()
    result = _apply_guild_filter(
        db.table('discord_messages').select('*').eq('message_id', message_id)
    ).execute()
    if not result.data:
        return {"error": f"Message {message_id} not found"}
    target = result.data[0]
    ch = target['channel_id']
    ts = target['created_at']

    before = list(reversed(
        _apply_guild_filter(
            db.table('discord_messages').select('*').eq('channel_id', ch)
            .lt('created_at', ts).order('created_at', desc=True)
            .limit(surrounding)
        ).execute().data
    ))
    after = _apply_guild_filter(
        db.table('discord_messages').select('*').eq('channel_id', ch)
        .gt('created_at', ts).order('created_at').limit(surrounding)
    ).execute().data
    replies = _apply_guild_filter(
        db.table('discord_messages').select('*')
        .eq('reference_id', message_id).order('created_at')
    ).execute().data

    all_msgs = [target] + before + after + replies
    ids = list({m['author_id'] for m in all_msgs})
    mmap = _member_map(db, ids)
    for m in all_msgs:
        m['author_name'] = mmap.get(m['author_id'], 'Unknown')

    return {"target": target, "before": before, "after": after,
            "replies": replies, "reply_count": len(replies)}


def thread(message_id: int) -> Dict[str, Any]:
    """Follow a reply chain up to root, then down through all replies."""
    db = _supabase()
    result = _apply_guild_filter(
        db.table('discord_messages').select('*').eq('message_id', message_id)
    ).execute()
    if not result.data:
        return {"error": f"Message {message_id} not found"}

    current = result.data[0]
    chain = [current]
    visited = {message_id}

    # Walk up
    root = current
    while root.get('reference_id'):
        ref = root['reference_id']
        if ref in visited:
            break
        visited.add(ref)
        parent = _apply_guild_filter(
            db.table('discord_messages').select('*').eq('message_id', ref)
        ).execute()
        if not parent.data:
            break
        root = parent.data[0]
        chain.insert(0, root)

    # Walk down (BFS)
    queue = [root['message_id']]
    while queue:
        cid = queue.pop(0)
        replies = _apply_guild_filter(
            db.table('discord_messages').select('*')
            .eq('reference_id', cid).order('created_at')
        ).execute()
        for r in replies.data:
            if r['message_id'] not in visited:
                visited.add(r['message_id'])
                chain.append(r)
                queue.append(r['message_id'])

    chain.sort(key=lambda x: x['created_at'])
    _enrich(chain)
    return {"root": chain[0], "messages": chain, "length": len(chain)}


def resolve_user(username: str) -> Optional[Dict]:
    """Resolve a username to a member record (fuzzy match).

    Returns the full member dict or None.
    """
    db = _supabase()
    for field in ('username', 'server_nick', 'global_name'):
        r = db.table('members').select('*').eq(field, username).execute()
        if r.data:
            return r.data[0]
    r = db.table('members').select('*').ilike('username', f'%{username}%').limit(1).execute()
    if r.data:
        return r.data[0]
    r = db.table('members').select('*').ilike('server_nick', f'%{username}%').limit(1).execute()
    if r.data:
        return r.data[0]
    return None


def user(username: str, days: int = 7, month: str = None,
         limit: int = 30, show_reactors: bool = False) -> List[Dict]:
    """Get messages from a user (fuzzy name match)."""
    member = resolve_user(username)
    if not member:
        return []
    return find_messages(days=days, month=month, author_id=member['member_id'],
                         limit=limit, exclude_nsfw=False,
                         show_reactors=show_reactors)


def channels(days: int = 7, month: str = None,
             exclude_nsfw: bool = True) -> List[Dict]:
    """Active channels with message counts."""
    db = _supabase()
    date_range = _resolve_date_range(days, month)
    cmap = _channel_map(exclude_nsfw)
    mq = db.table('discord_messages').select('channel_id')
    if isinstance(date_range, tuple):
        mq = mq.gte('created_at', date_range[0]).lte('created_at', date_range[1])
    else:
        mq = mq.gte('created_at', date_range)
    if _get_active_guild_id():
        mq = mq.eq('guild_id', _get_active_guild_id())
    rows = mq.execute().data
    counts = {}
    for r in rows:
        cid = r['channel_id']
        if cid in cmap:
            counts[cid] = counts.get(cid, 0) + 1
    return sorted([
        {'channel_id': cid, 'channel_name': cmap[cid], 'messages': n}
        for cid, n in counts.items()
    ], key=lambda x: x['messages'], reverse=True)


def summaries(days: int = 7, month: str = None,
              channel_id: int = None) -> List[Dict]:
    """Get legacy daily summary history from the bot.

    Active overview state now lives in live_update_* tables; this helper is
    retained for explicit legacy/backfill inspection.
    """
    db = _supabase()
    q = db.table('daily_summaries').select('date, channel_id, short_summary') \
        .order('date', desc=True)
    if month:
        start, end = _month_to_date_range(month)
        q = q.gte('date', start[:10]).lte('date', end[:10])
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
        q = q.gte('date', cutoff)
    if channel_id:
        q = q.eq('channel_id', str(channel_id))
    if _get_active_guild_id():
        q = q.eq('guild_id', _get_active_guild_id())
    rows = q.execute().data or []
    for row in rows:
        row['system'] = 'legacy_daily_summaries'
        row['legacy_note'] = 'Use live_update_* tables for active overview state.'
    return rows


def get_member_id(username: str) -> Optional[str]:
    """Resolve a username to a Discord member ID."""
    db = _supabase()
    for field in ('username', 'server_nick', 'global_name'):
        r = db.table('members').select('member_id').eq(field, username).execute()
        if r.data:
            return r.data[0]['member_id']
    r = db.table('members').select('member_id') \
        .ilike('username', f'%{username}%').limit(1).execute()
    if r.data:
        return r.data[0]['member_id']
    return None


def get_message(message_id: int) -> Optional[Dict]:
    """Fetch a single message by ID."""
    db = _supabase()
    r = _apply_guild_filter(
        db.table('discord_messages').select('*')
        .eq('message_id', message_id)
    ).execute()
    if not r.data:
        return None
    msg = r.data[0]
    _enrich([msg])
    return msg


def get_author_id(message_id: int) -> Optional[str]:
    """Get the author_id for a message."""
    db = _supabase()
    r = _apply_guild_filter(
        db.table('discord_messages').select('author_id')
        .eq('message_id', message_id)
    ).execute()
    return r.data[0]['author_id'] if r.data else None


# ============================================================
# Contributors — Layer 1 structured signals
# ============================================================

# Channel categories for signal classification
_RESOURCE_CHANNEL_KEYWORDS = ['resource']
_GENS_CHANNEL_KEYWORDS = ['gens', 'gen_sharing', 'art_sharing']
_TRAINING_CHANNEL_KEYWORDS = ['training']
_TECH_CHANNEL_KEYWORDS = ['chatter', 'comfyui', 'help', 'support']


def _categorize_channels() -> Dict[str, List[int]]:
    """Categorize channels into types by name keywords."""
    cmap = _channel_map(exclude_nsfw=True)
    categories: Dict[str, List[int]] = {
        'resources': [],
        'gens': [],
        'training': [],
        'tech': [],
    }
    for cid, name in cmap.items():
        lower = name.lower()
        # Skip summary threads (they have " - Summary - " in the name)
        if 'summary' in lower:
            continue
        if any(kw in lower for kw in _RESOURCE_CHANNEL_KEYWORDS):
            categories['resources'].append(cid)
        if any(kw in lower for kw in _GENS_CHANNEL_KEYWORDS):
            categories['gens'].append(cid)
        if any(kw in lower for kw in _TRAINING_CHANNEL_KEYWORDS):
            categories['training'].append(cid)
        if any(kw in lower for kw in _TECH_CHANNEL_KEYWORDS):
            categories['tech'].append(cid)
    return categories


def _paginated_fetch(db, table: str, select: str, filters: Dict,
                     order_col: str = 'created_at', page_size: int = 1000,
                     max_pages: int = 50) -> List[Dict]:
    """Fetch all rows matching filters with pagination."""
    all_rows = []
    for page in range(max_pages):
        q = db.table(table).select(select)
        for col, val in filters.items():
            if isinstance(val, tuple) and len(val) == 2:
                op, v = val
                if op == 'gte':
                    q = q.gte(col, v)
                elif op == 'lte':
                    q = q.lte(col, v)
                elif op == 'in':
                    q = q.in_(col, v)
            else:
                q = q.eq(col, val)
        q = q.order(order_col).range(page * page_size, (page + 1) * page_size - 1)
        rows = q.execute().data
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
    return all_rows


def contributors(month: str) -> Dict[str, Any]:
    """Generate structured contributor signals for a month.

    Returns a dict with metadata and a list of contributor signal profiles.
    This is Layer 1 — deterministic, no LLM involved.
    """
    db = _supabase()
    start, end = _month_to_date_range(month)
    guild_id = _get_active_guild_id()
    channel_cats = _categorize_channels()

    # Get all non-NSFW channel IDs
    safe_cmap = _channel_map(exclude_nsfw=True)
    # Filter out summary threads
    safe_channel_ids = [cid for cid, name in safe_cmap.items()
                        if 'summary' not in name.lower()]

    # ---- Fetch all messages for the month (paginated) ----
    filters = {
        'created_at': ('gte', start),
    }
    # Add end date filter separately since we need two filters on same col
    # Build query manually for dual date filter
    all_messages = []
    page = 0
    page_size = 1000
    while True:
        q = db.table('discord_messages').select(
            'message_id, author_id, channel_id, content, created_at, '
            'reaction_count, reactors, reference_id, attachments'
        ).gte('created_at', start).lte('created_at', end)
        if guild_id:
            q = q.eq('guild_id', guild_id)
        q = q.in_('channel_id', safe_channel_ids)
        q = q.order('created_at').range(page * page_size, (page + 1) * page_size - 1)
        rows = q.execute().data
        all_messages.extend(rows)
        if len(rows) < page_size:
            break
        page += 1

    if not all_messages:
        return {"month": month, "total_messages": 0, "contributors": []}

    # ---- Fetch daily summaries and extract mentioned names ----
    summary_rows = summaries(month=month)
    all_summary_text = "\n".join(r.get('short_summary', '') or '' for r in summary_rows)

    # ---- Fetch #updates channel posts and extract mentioned names ----
    updates_channel_id = 1138790534987661363
    updates_messages = []
    upage = 0
    while True:
        uq = db.table('discord_messages').select(
            'message_id, content, created_at, reaction_count'
        ).eq('channel_id', updates_channel_id
        ).gte('created_at', start).lte('created_at', end
        ).order('created_at').range(upage * page_size, (upage + 1) * page_size - 1)
        if guild_id:
            uq = uq.eq('guild_id', guild_id)
        rows = uq.execute().data
        updates_messages.extend(rows)
        if len(rows) < page_size:
            break
        upage += 1
    all_updates_text = "\n".join(m.get('content', '') or '' for m in updates_messages)

    # ---- Build per-author raw data ----
    author_data: Dict[int, Dict] = defaultdict(lambda: {
        'total_messages': 0,
        'total_reactions': 0,
        'max_reactions': 0,
        'top_messages': [],  # (message_id, reaction_count) tuples, kept sorted
        'unique_reactors': set(),
        'resource_posts': 0,
        'github_links': 0,
        'huggingface_links': 0,
        # Contribution signals
        'resource_threads_created': 0,     # threads started in resource channels
        'helpful_replies_reacted': 0,      # replies to others that got reactions
        'distinct_repliers': set(),        # unique people who replied to their messages
        'long_form_posts': 0,             # messages 500+ chars (tutorials, guides, explanations)
        'tutorial_posts': 0,              # messages with tutorial/guide keywords
        'rich_resource_posts': 0,         # resource channel posts with both text AND links/attachments
        # Raw counts for context
        'replies_made': 0,
        'channels_active': set(),
    })

    # Index messages by ID for reply/thread analysis
    msg_by_id = {m['message_id']: m for m in all_messages}
    resource_channel_ids = set(channel_cats.get('resources', []))

    for msg in all_messages:
        aid = msg['author_id']
        d = author_data[aid]
        d['total_messages'] += 1
        rc = msg.get('reaction_count', 0) or 0
        d['total_reactions'] += rc
        if rc > d['max_reactions']:
            d['max_reactions'] = rc
        # Track top 5 messages by reaction count
        if rc >= 1:
            d['top_messages'].append((msg['message_id'], rc, msg['channel_id']))
            d['top_messages'].sort(key=lambda x: x[1], reverse=True)
            d['top_messages'] = d['top_messages'][:5]

        # Parse reactors
        reactors = msg.get('reactors', [])
        if isinstance(reactors, str):
            try:
                reactors = json.loads(reactors)
            except Exception:
                reactors = []
        if isinstance(reactors, list):
            d['unique_reactors'].update(reactors)

        # Channel tracking
        cid = msg['channel_id']
        d['channels_active'].add(cid)

        # Resource channel posts
        if cid in resource_channel_ids:
            d['resource_posts'] += 1

        # Reply tracking
        is_reply = msg.get('reference_id') is not None
        if is_reply:
            d['replies_made'] += 1
            # Helpful reply = reply to someone else that got reacted
            ref = msg['reference_id']
            if ref in msg_by_id and msg_by_id[ref]['author_id'] != aid and rc > 0:
                d['helpful_replies_reacted'] += 1

        # Content signals — links to own work
        content = (msg.get('content') or '')
        content_lower = content.lower()
        if 'github.com' in content_lower:
            d['github_links'] += 1
        if 'huggingface' in content_lower or 'hf.co' in content_lower:
            d['huggingface_links'] += 1

        # Long-form content (500+ chars, excluding pure URLs/logs)
        if len(content) >= 500:
            # Filter out messages that are mostly URLs or error logs
            non_url_content = content
            for word in content.split():
                if word.startswith('http'):
                    non_url_content = non_url_content.replace(word, '')
            if len(non_url_content.strip()) >= 300:
                d['long_form_posts'] += 1

        # Tutorial/guide signals
        tutorial_keywords = ['tutorial', 'guide', 'how to', 'step by step', 'walkthrough',
                             'here\'s how', 'explanation', 'breakdown', 'demo video']
        if any(kw in content_lower for kw in tutorial_keywords):
            d['tutorial_posts'] += 1

        # Rich resource posts (resource channel + has text + has link or attachment)
        if cid in resource_channel_ids and len(content) >= 100:
            has_link = 'http' in content_lower
            atts = msg.get('attachments')
            has_attachment = atts and atts != '[]'
            if has_link or has_attachment:
                d['rich_resource_posts'] += 1

    # Second pass: count distinct repliers and resource thread creation
    for msg in all_messages:
        ref = msg.get('reference_id')
        if ref and ref in msg_by_id:
            parent_author = msg_by_id[ref]['author_id']
            if parent_author != msg['author_id']:
                author_data[parent_author]['distinct_repliers'].add(msg['author_id'])

    # ---- Fetch emoji-level reaction data for the month ----
    # Get appreciation/gratitude reactions and equity holder reactions
    appreciation_emojis = {'❤️', '🙏', '💖', '💯', '👑', '❤️‍🔥'}

    # Load equity holders first (need their IDs)
    equity_holders_names = set()
    equity_file = os.path.join(project_root, 'equity_holders.txt')
    if os.path.exists(equity_file):
        with open(equity_file) as f:
            for line in f:
                if ',' in line and not line.startswith('display_name'):
                    parts = line.strip().split(',')
                    equity_holders_names.add(parts[0].strip().lower())

    # Fetch reactions for the month (paginated)
    all_reactions = []
    page = 0
    while True:
        rq = db.table('discord_reactions').select(
            'message_id, user_id, emoji'
        ).gte('created_at', start).lte('created_at', end)
        if guild_id:
            rq = rq.eq('guild_id', guild_id)
        rq = rq.order('created_at').range(page * page_size, (page + 1) * page_size - 1)
        rows = rq.execute().data
        all_reactions.extend(rows)
        if len(rows) < page_size:
            break
        page += 1

    # Resolve equity holder member IDs
    # First get all member records for equity holders
    equity_holder_ids = set()
    if equity_holders_names:
        for field in ('username', 'server_nick', 'global_name'):
            for name in list(equity_holders_names):
                try:
                    r = db.table('members').select('member_id').ilike(field, name).execute()
                    for row in r.data:
                        equity_holder_ids.add(row['member_id'])
                except Exception:
                    pass

    # Count per-author: appreciation reactions received, equity holder endorsements
    author_appreciation: Dict[int, int] = defaultdict(int)
    author_equity_endorsers: Dict[int, set] = defaultdict(set)

    for reaction in all_reactions:
        mid = reaction['message_id']
        if mid not in msg_by_id:
            continue
        msg_author = msg_by_id[mid]['author_id']
        reactor_id = reaction['user_id']

        # Skip self-reactions
        if reactor_id == msg_author:
            continue

        # Appreciation reactions
        if reaction.get('emoji') in appreciation_emojis:
            author_appreciation[msg_author] += 1

        # Equity holder endorsements
        if reactor_id in equity_holder_ids:
            author_equity_endorsers[msg_author].add(reactor_id)

    # ---- Resolve author names ----
    author_ids = list(author_data.keys())
    mmap = _member_map(db, author_ids)

    # ---- Extract names from summaries via **bold** pattern ----
    import re
    bold_matches = re.findall(r'\*\*([^*]+)\*\*', all_summary_text)

    name_to_aid: Dict[str, int] = {}
    for aid, name in mmap.items():
        if name and name.lower() not in ('unknown', 'a community member'):
            name_to_aid[name.lower()] = aid

    def _count_bold_mentions(text: str) -> Dict[int, int]:
        """Extract **bolded** names from text, match against known members."""
        counts: Dict[int, int] = defaultdict(int)
        for bold_text in re.findall(r'\*\*([^*]+)\*\*', text):
            cleaned = bold_text.strip()
            if cleaned.lower() in name_to_aid:
                counts[name_to_aid[cleaned.lower()]] += 1
                continue
            for known_name, aid in name_to_aid.items():
                if cleaned.lower().startswith(known_name) and len(known_name) >= 3:
                    counts[aid] += 1
                    break
        return counts

    summary_mention_counts = _count_bold_mentions(all_summary_text)
    updates_mention_counts = _count_bold_mentions(all_updates_text)

    # ---- Build output with contribution-focused signals ----
    contributors_list = []
    for aid, d in author_data.items():
        name = mmap.get(aid, str(aid))

        # Skip bots and pom
        if name.lower() in ('bndc', 'general-scheming', 'pom', 'a community member'):
            continue

        is_holder = name.lower() in equity_holders_names
        mentions = summary_mention_counts.get(aid, 0)
        updates_mentions = updates_mention_counts.get(aid, 0)
        appreciation = author_appreciation.get(aid, 0)
        equity_endorser_count = len(author_equity_endorsers.get(aid, set()))
        distinct_replier_count = len(d['distinct_repliers'])

        # ---- Contribution signals (each = evidence of value created) ----
        signals = []

        # Work was noticed: community reacted meaningfully
        if d['max_reactions'] >= 5:
            signals.append('high-impact-post')

        # Shared original work: links to repos/models
        if d['github_links'] > 0 or d['huggingface_links'] > 0:
            signals.append('shares-original-work')

        # Posted in resource channels: sharing tools/workflows for others
        if d['resource_posts'] > 0:
            signals.append('resource-contributor')

        # Sparked discussion: multiple different people replied to them
        if distinct_replier_count >= 5:
            signals.append('sparks-discussion')

        # Helpful replies got recognized: their replies to others got reactions
        if d['helpful_replies_reacted'] >= 3:
            signals.append('recognized-helper')

        # Endorsed by equity holders: known contributors reacted to their work
        if equity_endorser_count >= 3:
            signals.append('equity-holder-endorsed')

        # Received gratitude: appreciation/heart reactions on their posts
        if appreciation >= 5:
            signals.append('appreciated')

        # Editorially notable: bot's summaries called them out
        if mentions >= 2:
            signals.append('summary-featured')

        # Featured in POM's updates: called out in #updates channel
        if updates_mentions >= 1:
            signals.append('updates-featured')

        # Educator: writes long-form explanations, tutorials, guides
        if d['long_form_posts'] >= 5 or d['rich_resource_posts'] >= 3:
            signals.append('educator')

        # Rich resource contributor: substantial posts in resource channels
        if d['rich_resource_posts'] >= 2:
            signals.append('rich-resource-poster')

        contributors_list.append({
            'username': name,
            'author_id': aid,
            'is_equity_holder': is_holder,
            'signal_count': len(signals),
            'signals': signals,
            'metrics': {
                'total_messages': d['total_messages'],
                'total_reactions': d['total_reactions'],
                'max_reactions': d['max_reactions'],
                'top_messages': [
                    {'message_id': mid, 'reactions': rc, 'channel_id': cid}
                    for mid, rc, cid in d['top_messages']
                ],
                'unique_reactor_count': len(d['unique_reactors']),
                'resource_posts': d['resource_posts'],
                'github_links': d['github_links'],
                'huggingface_links': d['huggingface_links'],
                'helpful_replies_reacted': d['helpful_replies_reacted'],
                'distinct_repliers': distinct_replier_count,
                'appreciation_reactions': appreciation,
                'equity_holder_endorsements': equity_endorser_count,
                'long_form_posts': d['long_form_posts'],
                'tutorial_posts': d['tutorial_posts'],
                'rich_resource_posts': d['rich_resource_posts'],
                'summary_mentions': mentions,
                'updates_mentions': updates_mentions,
            },
        })

    # Sort by signal count desc, then total reactions desc
    contributors_list.sort(key=lambda x: (x['signal_count'], x['metrics']['total_reactions']),
                           reverse=True)

    return {
        "month": month,
        "total_messages": len(all_messages),
        "total_authors": len(author_data),
        "channel_categories": {k: len(v) for k, v in channel_cats.items()},
        "contributors": contributors_list,
    }


def profile(username: str, month: str) -> Dict[str, Any]:
    """Full contributor profile for Layer 2 deep dive.

    Returns everything an evaluator needs about a person in one call:
    their messages, who replied to them, who reacted, what the reactions were,
    and relevant summary/updates excerpts.
    """
    db = _supabase()
    start, end = _month_to_date_range(month)
    guild_id = _get_active_guild_id()

    # Resolve user
    member = resolve_user(username)
    if not member:
        return {"error": f"User '{username}' not found"}

    uid = member['member_id']
    display_name = _member_name(member)

    # ---- Get their messages for the month ----
    msgs = []
    page = 0
    page_size = 1000
    while True:
        q = db.table('discord_messages').select(
            'message_id, channel_id, thread_id, content, created_at, '
            'reaction_count, reactors, reference_id, attachments'
        ).eq('author_id', uid).gte('created_at', start).lte('created_at', end)
        if guild_id:
            q = q.eq('guild_id', guild_id)
        q = q.order('created_at').range(page * page_size, (page + 1) * page_size - 1)
        rows = q.execute().data
        msgs.extend(rows)
        if len(rows) < page_size:
            break
        page += 1

    if not msgs:
        return {"username": display_name, "month": month, "total_messages": 0,
                "messages": [], "replies_to_them": [], "summary_excerpts": []}

    # Channel name map
    cmap = _channel_map(exclude_nsfw=False)
    msg_ids = [m['message_id'] for m in msgs]

    # ---- Get replies to their messages ----
    replies_to_them = []
    # Batch query: messages where reference_id IN their message IDs
    for i in range(0, len(msg_ids), 100):
        batch = msg_ids[i:i+100]
        rq = db.table('discord_messages').select(
            'message_id, author_id, channel_id, content, created_at, '
            'reaction_count, reference_id'
        ).in_('reference_id', batch).gte('created_at', start).lte('created_at', end)
        if guild_id:
            rq = rq.eq('guild_id', guild_id)
        replies_to_them.extend(rq.execute().data)

    # Filter out self-replies
    replies_to_them = [r for r in replies_to_them if r['author_id'] != uid]

    # Resolve replier names
    replier_ids = list({r['author_id'] for r in replies_to_them})
    replier_map = _member_map(db, replier_ids) if replier_ids else {}

    # ---- Get emoji-level reactions on their messages ----
    reactions_on_them = []
    for i in range(0, len(msg_ids), 100):
        batch = msg_ids[i:i+100]
        rq = db.table('discord_reactions').select(
            'message_id, user_id, emoji'
        ).in_('message_id', batch)
        if guild_id:
            rq = rq.eq('guild_id', guild_id)
        reactions_on_them.extend(rq.execute().data)

    # Filter out self-reactions
    reactions_on_them = [r for r in reactions_on_them if r['user_id'] != uid]

    # Resolve reactor names
    reactor_ids = list({r['user_id'] for r in reactions_on_them})
    reactor_map = _member_map(db, reactor_ids) if reactor_ids else {}

    # Load equity holders for flagging
    equity_holders_names = set()
    equity_file = os.path.join(project_root, 'equity_holders.txt')
    if os.path.exists(equity_file):
        with open(equity_file) as f:
            for line in f:
                if ',' in line and not line.startswith('display_name'):
                    equity_holders_names.add(line.strip().split(',')[0].strip().lower())

    # ---- Build reaction summary per message ----
    reaction_details: Dict[int, List] = defaultdict(list)
    for r in reactions_on_them:
        reactor_name = reactor_map.get(r['user_id'], str(r['user_id']))
        is_holder = reactor_name.lower() in equity_holders_names
        reaction_details[r['message_id']].append({
            'reactor': reactor_name,
            'emoji': r['emoji'],
            'is_equity_holder': is_holder,
        })

    # ---- Build reply summary per message ----
    reply_details: Dict[int, List] = defaultdict(list)
    for r in replies_to_them:
        replier_name = replier_map.get(r['author_id'], str(r['author_id']))
        reply_details[r['reference_id']].append({
            'replier': replier_name,
            'content': (r.get('content') or '')[:200],
            'reactions': r.get('reaction_count', 0),
        })

    # ---- Format messages with enriched context ----
    formatted_msgs = []
    for msg in sorted(msgs, key=lambda m: m.get('reaction_count', 0) or 0, reverse=True):
        mid = msg['message_id']
        reactions = reaction_details.get(mid, [])
        replies = reply_details.get(mid, [])

        # Count equity holder reactions for this message
        eh_reactions = [r for r in reactions if r['is_equity_holder']]

        formatted_msgs.append({
            'message_id': mid,
            'channel': cmap.get(msg['channel_id'], str(msg['channel_id'])),
            'content': (msg.get('content') or '')[:500],
            'date': msg.get('created_at', '')[:10],
            'reaction_count': msg.get('reaction_count', 0),
            'is_reply': msg.get('reference_id') is not None,
            'link': message_jump_url(_get_active_guild_id() or 0, msg['channel_id'], mid, thread_id=msg.get('thread_id')),
            'reactions': reactions[:20],  # cap for readability
            'equity_holder_reactions': len(eh_reactions),
            'replies': replies[:10],  # cap for readability
            'reply_count': len(replies),
        })

    # ---- Extract summary excerpts mentioning this person ----
    summary_rows = summaries(month=month)
    summary_excerpts = []
    name_lower = display_name.lower()
    for row in summary_rows:
        text = row.get('short_summary', '') or ''
        if name_lower in text.lower():
            # Extract the relevant bullet point(s)
            for line in text.split('\n'):
                if name_lower in line.lower():
                    summary_excerpts.append({
                        'date': row.get('date'),
                        'excerpt': line.strip()[:300],
                    })

    # ---- Extract updates excerpts mentioning this person ----
    updates_channel_id = 1138790534987661363
    uq = db.table('discord_messages').select(
        'message_id, content, created_at'
    ).eq('channel_id', updates_channel_id
    ).gte('created_at', start).lte('created_at', end
    ).order('created_at')
    if guild_id:
        uq = uq.eq('guild_id', guild_id)
    updates_msgs = uq.execute().data

    updates_excerpts = []
    for umsg in updates_msgs:
        text = umsg.get('content', '') or ''
        if name_lower in text.lower():
            # Extract relevant sentences/lines
            for line in text.split('\n'):
                if name_lower in line.lower():
                    updates_excerpts.append({
                        'date': umsg.get('created_at', '')[:10],
                        'excerpt': line.strip()[:300],
                    })

    # ---- Replies they made to others (who are they helping?) ----
    their_replies = [m for m in msgs if m.get('reference_id')]
    # Look up parent messages to see who they replied to
    parent_ids = [m['reference_id'] for m in their_replies if m['reference_id']]
    parent_msgs = {}
    for i in range(0, len(parent_ids), 100):
        batch = parent_ids[i:i+100]
        pq = db.table('discord_messages').select(
            'message_id, author_id, content, channel_id'
        ).in_('message_id', batch)
        for row in pq.execute().data:
            parent_msgs[row['message_id']] = row

    # Resolve parent author names
    parent_author_ids = list({m['author_id'] for m in parent_msgs.values()})
    parent_name_map = _member_map(db, parent_author_ids) if parent_author_ids else {}

    # Build "who they helped" — their replies to others, sorted by reactions on the reply
    helped_interactions = []
    for msg in sorted(their_replies, key=lambda m: m.get('reaction_count', 0) or 0, reverse=True):
        parent = parent_msgs.get(msg.get('reference_id'))
        if not parent or parent['author_id'] == uid:
            continue  # skip self-replies
        helped_interactions.append({
            'helped_user': parent_name_map.get(parent['author_id'], str(parent['author_id'])),
            'their_question': (parent.get('content') or '')[:200],
            'your_reply': (msg.get('content') or '')[:200],
            'reply_reactions': msg.get('reaction_count', 0),
            'channel': cmap.get(msg['channel_id'], str(msg['channel_id'])),
            'date': msg.get('created_at', '')[:10],
            'link': message_jump_url(_get_active_guild_id() or 0, msg['channel_id'], msg['message_id'], thread_id=msg.get('thread_id')),
        })

    # ---- @mentions of them by others (not in replies) ----
    mention_str = f'<@{uid}>'
    mention_msgs = db.table('discord_messages').select(
        'message_id, author_id, content, channel_id, created_at, reaction_count'
    ).ilike('content', f'%{mention_str}%'
    ).gte('created_at', start).lte('created_at', end
    ).neq('author_id', uid).order('reaction_count', desc=True).limit(20)
    if guild_id:
        mention_msgs = mention_msgs.eq('guild_id', guild_id)
    mention_results = mention_msgs.execute().data

    mention_author_ids = list({m['author_id'] for m in mention_results})
    mention_name_map = _member_map(db, mention_author_ids) if mention_author_ids else {}

    formatted_mentions = [{
        'from': mention_name_map.get(m['author_id'], str(m['author_id'])),
        'content': (m.get('content') or '')[:200],
        'channel': cmap.get(m['channel_id'], str(m['channel_id'])),
        'date': m.get('created_at', '')[:10],
        'reactions': m.get('reaction_count', 0),
    } for m in mention_results]

    # ---- Channel breakdown ----
    from collections import Counter
    channel_counts = Counter(cmap.get(m['channel_id'], str(m['channel_id'])) for m in msgs)
    channel_breakdown = [
        {'channel': ch, 'messages': count, 'pct': round(100 * count / len(msgs))}
        for ch, count in channel_counts.most_common(10)
    ]

    # ---- Emoji summary on their posts ----
    emoji_counts = Counter(r['emoji'] for r in reactions_on_them)
    emoji_summary = [{'emoji': e, 'count': c} for e, c in emoji_counts.most_common(10)]

    return {
        'username': display_name,
        'author_id': uid,
        'month': month,
        'is_equity_holder': display_name.lower() in equity_holders_names,
        'total_messages': len(msgs),
        'total_reactions_received': sum(m.get('reaction_count', 0) or 0 for m in msgs),
        'distinct_reactors': len({r['user_id'] for r in reactions_on_them}),
        'distinct_repliers': len({r['author_id'] for r in replies_to_them}),
        'total_replies_received': len(replies_to_them),
        'total_replies_made': len(their_replies),
        'channel_breakdown': channel_breakdown,
        'emoji_summary': emoji_summary,
        'messages': formatted_msgs[:30],  # top 30 by reactions
        'helped': helped_interactions[:15],  # top 15 help interactions by reactions
        'mentioned_by': formatted_mentions[:10],  # top 10 @mentions
        'summary_excerpts': summary_excerpts,
        'updates_excerpts': updates_excerpts,
    }


# ============================================================
# Discord REST API
# ============================================================

def api_get_messages(channel_id: int, limit: int = 50,
                     before: int = None, after: int = None) -> List[Dict]:
    """Fetch messages from Discord API (live, not DB)."""
    import requests
    headers = _discord_headers()
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    if before:
        url += f"&before={before}"
    if after:
        url += f"&after={after}"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def api_get_message(channel_id: int, message_id: int) -> Dict:
    """Fetch a single message from Discord API."""
    import requests
    headers = _discord_headers()
    resp = requests.get(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        headers=headers
    )
    resp.raise_for_status()
    return resp.json()


def api_post(channel_id: int, content: str,
             reply_to: int = None) -> Dict:
    """Send a message to a channel. Optionally reply to a message."""
    import requests
    headers = _discord_headers()
    payload = {"content": content}
    if reply_to:
        payload["message_reference"] = {"message_id": str(reply_to)}
    resp = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers, json=payload
    )
    resp.raise_for_status()
    return resp.json()


def api_edit(channel_id: int, message_id: int, content: str) -> Dict:
    """Edit a bot message."""
    import requests
    headers = _discord_headers()
    resp = requests.patch(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        headers=headers, json={"content": content}
    )
    resp.raise_for_status()
    return resp.json()


def api_delete(channel_id: int, message_id: int) -> bool:
    """Delete a bot message."""
    import requests
    headers = _discord_headers()
    resp = requests.delete(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        headers=headers
    )
    return resp.status_code == 204


def api_react(channel_id: int, message_id: int, emoji: str) -> bool:
    """Add a reaction to a message."""
    import requests
    headers = _discord_headers()
    import urllib.parse
    encoded = urllib.parse.quote(emoji)
    resp = requests.put(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded}/@me",
        headers=headers
    )
    return resp.status_code == 204


def api_threads(channel_id: int) -> List[Dict]:
    """List active threads in a channel."""
    import requests
    headers = _discord_headers()
    active_guild_id = _get_active_guild_id()
    if not active_guild_id:
        raise ValueError("A guild ID is required for api-threads. Use --guild-id or TARGET_GUILD_ID.")
    # Active threads from guild
    resp = requests.get(
        f"https://discord.com/api/v10/guilds/{active_guild_id}/threads/active",
        headers=headers
    )
    resp.raise_for_status()
    return [t for t in resp.json().get('threads', [])
            if t.get('parent_id') == str(channel_id)]


def api_upload(channel_id: int, file_path: str, content: str = "") -> Dict:
    """Upload a file to a channel."""
    import requests
    headers = _discord_headers_multipart()
    fname = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            data={"content": content},
            files={"file": (fname, f)}
        )
    resp.raise_for_status()
    return resp.json()


# ============================================================
# Media
# ============================================================

def get_urls(message_ids: List[int]) -> Dict[int, List[Dict]]:
    """Get attachment URLs from DB for multiple messages."""
    db = _supabase()
    result = {}
    for mid in message_ids:
        r = _apply_guild_filter(
            db.table('discord_messages').select('attachments')
            .eq('message_id', mid)
        ).execute()
        if r.data:
            atts = r.data[0].get('attachments') or []
            if isinstance(atts, str):
                try:
                    atts = json.loads(atts)
                except Exception:
                    atts = []
            result[mid] = atts
        else:
            result[mid] = []
    return result


def refresh(message_ids: List[int]) -> Dict[str, Any]:
    """Refresh expired Discord CDN URLs by re-fetching from API."""
    # Import the weekly_digest refresh which handles the bot login
    from scripts.weekly_digest import batch_refresh_media, set_active_guild_id
    set_active_guild_id(_get_active_guild_id())
    return batch_refresh_media(message_ids)


# ============================================================
# CLI
# ============================================================

def _print_messages(msgs: List[Dict], title: str = ""):
    if title:
        print(f"\n{title}\n")
    print(f"Total: {len(msgs)}\n")
    for m in msgs:
        print(_fmt(m))
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Discord Tools — search, browse, and interact with Discord",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--guild-id', type=int, default=None,
                        help='Explicit guild scope for DB queries, jump URLs, and guild-scoped API calls')
    sub = parser.add_subparsers(dest='cmd')

    # -- DB queries --
    p = sub.add_parser('search', help='Search messages by content')
    p.add_argument('query')
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--month', type=str, default=None, help='YYYY-MM (overrides --days)')
    p.add_argument('--channel', type=int, default=None)
    p.add_argument('--limit', type=int, default=30)
    p.add_argument('--show-reactors', action='store_true', help='Show who reacted')

    p = sub.add_parser('top', help='Top messages by reactions')
    p.add_argument('channel', nargs='?', type=int, default=None)
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--month', type=str, default=None, help='YYYY-MM (overrides --days)')
    p.add_argument('--min-reactions', type=int, default=3)
    p.add_argument('--limit', type=int, default=30)
    p.add_argument('--show-reactors', action='store_true', help='Show who reacted')

    p = sub.add_parser('messages', help='Recent messages from a channel')
    p.add_argument('channel', type=int)
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--limit', type=int, default=50)

    p = sub.add_parser('context', help='Message with surrounding context')
    p.add_argument('message_id', type=int)
    p.add_argument('--surrounding', type=int, default=5)

    p = sub.add_parser('thread', help='Follow reply chain')
    p.add_argument('message_id', type=int)

    p = sub.add_parser('user', help='Messages from a user')
    p.add_argument('username')
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--month', type=str, default=None, help='YYYY-MM (overrides --days)')
    p.add_argument('--limit', type=int, default=30)
    p.add_argument('--show-reactors', action='store_true', help='Show who reacted')

    p = sub.add_parser('channels', help='Active channels')
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--month', type=str, default=None, help='YYYY-MM (overrides --days)')

    p = sub.add_parser('summaries', help='Daily summaries')
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--month', type=str, default=None, help='YYYY-MM (overrides --days)')
    p.add_argument('--channel', type=int, default=None)

    p = sub.add_parser('contributors', help='Layer 1: Generate structured contributor signals for a month')
    p.add_argument('--month', type=str, required=True, help='YYYY-MM')
    p.add_argument('--min-signals', type=int, default=2, help='Min signal count to include (default 2)')
    p.add_argument('--output', type=str, default=None, help='Write JSON to file instead of stdout')

    p = sub.add_parser('profile', help='Layer 2: Full contributor dossier — messages, reactions, replies, summary excerpts')
    p.add_argument('username')
    p.add_argument('--month', type=str, required=True, help='YYYY-MM')

    # -- Discord API --
    p = sub.add_parser('api-get', help='Fetch messages from Discord API')
    p.add_argument('channel', type=int)
    p.add_argument('message_id', nargs='?', type=int, default=None)
    p.add_argument('--limit', type=int, default=20)

    p = sub.add_parser('api-post', help='Send a message')
    p.add_argument('channel', type=int)
    p.add_argument('content')
    p.add_argument('--reply-to', type=int, default=None)

    p = sub.add_parser('api-edit', help='Edit a bot message')
    p.add_argument('channel', type=int)
    p.add_argument('message_id', type=int)
    p.add_argument('content')

    p = sub.add_parser('api-delete', help='Delete a bot message')
    p.add_argument('channel', type=int)
    p.add_argument('message_id', type=int)

    p = sub.add_parser('api-reply', help='Reply to a message')
    p.add_argument('channel', type=int)
    p.add_argument('message_id', type=int)
    p.add_argument('content')

    p = sub.add_parser('api-react', help='Add a reaction')
    p.add_argument('channel', type=int)
    p.add_argument('message_id', type=int)
    p.add_argument('emoji')

    p = sub.add_parser('api-threads', help='List threads in a channel')
    p.add_argument('channel', type=int)

    p = sub.add_parser('api-upload', help='Upload a file')
    p.add_argument('channel', type=int)
    p.add_argument('file')
    p.add_argument('--caption', default='')

    # -- Media --
    p = sub.add_parser('refresh', help='Refresh expired CDN URLs')
    p.add_argument('message_ids', nargs='+', type=int)

    p = sub.add_parser('get-urls', help='Get attachment URLs from DB')
    p.add_argument('message_ids', nargs='+', type=int)

    p = sub.add_parser('member-id', help='Resolve username to member ID')
    p.add_argument('username')

    p = sub.add_parser('author-id', help='Get author ID for a message')
    p.add_argument('message_id', type=int)

    args = parser.parse_args()
    _set_active_guild_id(args.guild_id or _DEFAULT_GUILD_ID)

    if args.cmd == 'search':
        period = args.month or f"last {args.days} days"
        _print_messages(search(args.query, args.days, month=args.month,
                               channel_id=args.channel, limit=args.limit,
                               show_reactors=args.show_reactors),
                        f"Search: '{args.query}' ({period})")

    elif args.cmd == 'top':
        period = args.month or f"last {args.days} days"
        _print_messages(top(args.days, month=args.month,
                           min_reactions=args.min_reactions, limit=args.limit,
                           channel_id=args.channel,
                           show_reactors=args.show_reactors),
                        f"Top messages ({period}, min {args.min_reactions} reactions)")

    elif args.cmd == 'messages':
        _print_messages(messages(args.channel, args.days, args.limit),
                        f"Messages from {args.channel}")

    elif args.cmd == 'context':
        r = context(args.message_id, args.surrounding)
        if r.get('error'):
            print(r['error'])
            return
        print("\n=== BEFORE ===")
        for m in r['before']:
            print(_fmt(m)); print()
        print("=== TARGET ===")
        print(_fmt(r['target'])); print()
        print("=== AFTER ===")
        for m in r['after']:
            print(_fmt(m)); print()
        if r['replies']:
            print(f"=== REPLIES ({r['reply_count']}) ===")
            for m in r['replies']:
                print(_fmt(m)); print()

    elif args.cmd == 'thread':
        r = thread(args.message_id)
        if r.get('error'):
            print(r['error'])
            return
        print(f"\nThread ({r['length']} messages):\n")
        for m in r['messages']:
            print(_fmt(m)); print()

    elif args.cmd == 'user':
        period = args.month or f"last {args.days} days"
        _print_messages(user(args.username, args.days, month=args.month,
                            limit=args.limit, show_reactors=args.show_reactors),
                        f"Messages from {args.username} ({period})")

    elif args.cmd == 'channels':
        period = args.month or f"last {args.days} days"
        chs = channels(args.days, month=args.month)
        print(f"\nActive channels ({period}):\n")
        for ch in chs:
            print(f"  {ch['channel_name']}: {ch['messages']} msgs ({ch['channel_id']})")

    elif args.cmd == 'contributors':
        result = contributors(args.month)
        # Filter by min signals
        result['contributors'] = [
            c for c in result['contributors']
            if c['signal_count'] >= args.min_signals
        ]
        output = json.dumps(result, indent=2, default=str)
        if args.output:
            with open(args.output, 'w') as f:
                f.write(output)
            print(f"Wrote {len(result['contributors'])} contributors to {args.output}")
        else:
            print(output)

    elif args.cmd == 'profile':
        result = profile(args.username, args.month)
        print(json.dumps(result, indent=2, default=str))

    elif args.cmd == 'summaries':
        rows = summaries(args.days, month=args.month, channel_id=args.channel)
        by_date = defaultdict(list)
        for r in rows:
            by_date[r['date']].append(r)
        for date in sorted(by_date.keys(), reverse=True):
            items = by_date[date]
            print(f"\n=== {date} ({len(items)} channels) ===")
            for item in items:
                s = (item.get('short_summary') or '')[:400]
                print(f"  [{item['channel_id']}] {s}\n")

    elif args.cmd == 'api-get':
        if args.message_id:
            msg = api_get_message(args.channel, args.message_id)
            print(json.dumps(msg, indent=2))
        else:
            msgs = api_get_messages(args.channel, limit=args.limit)
            for m in reversed(msgs):
                author = m['author']['username']
                content = (m.get('content') or '')[:200]
                atts = [a['filename'] for a in m.get('attachments', [])]
                print(f"{m['id']} | {author} | {content}")
                for a in atts:
                    print(f"  ATT: {a}")

    elif args.cmd == 'api-post':
        r = api_post(args.channel, args.content)
        print(f"Sent: {r['id']}")

    elif args.cmd == 'api-edit':
        r = api_edit(args.channel, args.message_id, args.content)
        print(f"Edited: {r['id']}")

    elif args.cmd == 'api-delete':
        ok = api_delete(args.channel, args.message_id)
        print("Deleted" if ok else "Failed")

    elif args.cmd == 'api-reply':
        r = api_post(args.channel, args.content, reply_to=args.message_id)
        print(f"Replied: {r['id']}")

    elif args.cmd == 'api-react':
        ok = api_react(args.channel, args.message_id, args.emoji)
        print("Reacted" if ok else "Failed")

    elif args.cmd == 'api-threads':
        threads = api_threads(args.channel)
        for t in threads:
            print(f"{t['id']} | {t['name']}")

    elif args.cmd == 'api-upload':
        r = api_upload(args.channel, args.file, args.caption)
        for a in r.get('attachments', []):
            print(f"{a['filename']}: {a['url']}")

    elif args.cmd == 'refresh':
        r = refresh(args.message_ids)
        for s in r.get('success', []):
            print(f"OK {s['message_id']}: {len(s.get('urls', []))} URLs")
        for f in r.get('failed', []):
            print(f"FAIL {f['message_id']}: {f.get('error')}")

    elif args.cmd == 'get-urls':
        r = get_urls(args.message_ids)
        for mid, atts in r.items():
            for a in atts:
                print(f"{mid} | {a.get('filename', '')} | {a.get('url', '')}")

    elif args.cmd == 'member-id':
        mid = get_member_id(args.username)
        print(mid or "Not found")

    elif args.cmd == 'author-id':
        aid = get_author_id(args.message_id)
        print(aid or "Not found")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
