#!/usr/bin/env python3
"""
Backfill missing channel_type on discord_channels.

Channels created after the last authoritative crawl can have channel_type NULL
(observed 2026-08-05: `minimax_h3_resources` — a forum, 552/552 threaded
messages — plus several text channels). The archive's classification never
typed them, so the field can't be used to detect forums for those channels.

This script classifies only the NULL-type channels that actually have messages,
offline and idempotently, using a structural discriminator:

  * a FORUM files every post as a thread  -> ~100% of messages have thread_id
  * a TEXT channel is ~0% threaded        -> thread_id rarely populated

Rules (per channel with >=1 message and channel_type IS NULL):
  - thread_ratio >= 0.9  AND  messages >= 2   -> 'forum'
  - thread_ratio <= 0.05                      -> 'text'
  - otherwise (low message count / ambiguous) -> SKIPPED + reported for
    confirmation (use the Discord API: a forum channel's API `type` is 15).

Dry run by default; pass --execute to apply. Safe to re-run (idempotent).

Usage:
    python scripts/backfill_channel_types.py                # dry run
    python scripts/backfill_channel_types.py --execute      # apply
    python scripts/backfill_channel_types.py --guild 1076117621407223829
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from supabase import create_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('BackfillChannelTypes')

FORUM_RATIO = 0.9
TEXT_RATIO = 0.05
MIN_MSGS_FOR_FORUM = 2


def get_client():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(url, key)


def find_null_type_channels(sb, guild_id: int | None) -> list[dict]:
    """Return channels with channel_type IS NULL that have >=1 non-deleted message."""
    q = (
        sb.from_('discord_channels')
        .select('channel_id, channel_name, channel_type, guild_id')
        .is_('channel_type', 'null')
    )
    if guild_id is not None:
        q = q.eq('guild_id', guild_id)
    rows = q.execute().data
    out = []
    for ch in rows:
        mid = ch['channel_id']
        msg_count = (
            sb.from_('discord_messages')
            .select('message_id', count='exact')
            .eq('channel_id', mid)
            .eq('is_deleted', False)
            .execute()
        ).count or 0
        threaded = (
            sb.from_('discord_messages')
            .select('message_id', count='exact')
            .eq('channel_id', mid)
            .eq('is_deleted', False)
            .not_.is_('thread_id', 'null')
            .execute()
        ).count or 0
        out.append({**ch, 'msgs': int(msg_count), 'threaded': int(threaded)})
    return [c for c in out if c['msgs'] > 0]


def classify(msgs: int, threaded: int) -> tuple[str | None, str]:
    ratio = threaded / msgs if msgs else 0.0
    if msgs >= MIN_MSGS_FOR_FORUM and ratio >= FORUM_RATIO:
        return 'forum', f'ratio={ratio:.2f} ({threaded}/{msgs} threaded)'
    if ratio <= TEXT_RATIO:
        return 'text', f'ratio={ratio:.2f} ({threaded}/{msgs} threaded)'
    return None, f'ambiguous (ratio={ratio:.2f}, msgs={msgs}) — needs confirmation'


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backfill missing channel_type on discord_channels.")
    p.add_argument('--execute', action='store_true', help='Apply updates (default: dry run)')
    p.add_argument('--guild', type=int, default=None, help='Restrict to one guild_id')
    args = p.parse_args(argv)

    sb = get_client()
    channels = find_null_type_channels(sb, args.guild)
    if not channels:
        logger.info("no NULL-type channels with messages found")
        return 0

    to_apply: list[dict] = []
    skipped: list[dict] = []
    for ch in sorted(channels, key=lambda c: -c['msgs']):
        typ, why = classify(ch['msgs'], ch['threaded'])
        if typ is None:
            skipped.append({**ch, 'why': why})
            logger.warning("SKIP %-30s %s", ch['channel_name'], why)
        else:
            to_apply.append({**ch, 'new_type': typ, 'why': why})
            logger.info("-> %-30s %-6s %s", ch['channel_name'], typ, why)

    logger.info("classified %d channel(s); skipped %d ambiguous; dry_run=%s",
                len(to_apply), len(skipped), not args.execute)
    if not args.execute:
        logger.info("pass --execute to apply")
        return 0

    for ch in to_apply:
        r = (
            sb.from_('discord_channels')
            .update({'channel_type': ch['new_type']})
            .eq('channel_id', ch['channel_id'])
            .is_('channel_type', 'null')   # only touch still-null rows (idempotent)
            .execute()
        )
        logger.info("UPDATED %-30s -> %-6s (%s)", ch['channel_name'], ch['new_type'], r.data)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
