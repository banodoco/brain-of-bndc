#!/usr/bin/env python3
"""Backfill legacy live-update feed/watchlist rows into topic-editor tables.

Safe to run repeatedly. Topics upsert on (environment, guild_id, canonical_key)
and sources upsert on (topic_id, message_id), matching runtime storage helpers.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

from dotenv import load_dotenv
from supabase import create_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.summarising.topic_editor import canonicalize_topic_key


def get_client():
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(url, key)


def fetch_all(sb, table: str, *, environment: str, guild_id: int | None, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        query = sb.table(table).select("*").eq("environment", environment).range(offset, offset + limit - 1)
        if guild_id is not None:
            query = query.eq("guild_id", guild_id)
        result = query.execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < limit:
            return rows
        offset += limit


def message_ids_from(row: Dict[str, Any]) -> List[str]:
    raw = row.get("source_message_ids") or row.get("discord_message_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item) for item in raw if str(item).strip()]


def authors_from(row: Dict[str, Any]) -> List[str]:
    raw = row.get("author_context_snapshot") or row.get("source_authors") or []
    if isinstance(raw, dict):
        candidates = [raw.get("server_nick"), raw.get("global_name"), raw.get("username"), raw.get("member_id")]
    else:
        candidates = raw if isinstance(raw, list) else [raw]
    authors: List[str] = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text not in authors:
            authors.append(text)
    return authors


def topic_from_feed_item(row: Dict[str, Any], environment: str) -> Dict[str, Any]:
    title = row.get("title") or row.get("headline") or row.get("duplicate_key") or "legacy-live-update"
    canonical_key = canonicalize_topic_key(row.get("duplicate_key") or title)
    return {
        "canonical_key": canonical_key,
        "display_slug": row.get("duplicate_key") or canonical_key,
        "guild_id": row.get("guild_id"),
        "environment": environment,
        "state": "posted",
        "headline": title,
        "summary": {
            "body": row.get("body") or row.get("summary"),
            "legacy_feed_item_id": row.get("feed_item_id"),
            "update_type": row.get("update_type"),
        },
        "source_authors": authors_from(row),
        "publication_status": "sent" if row.get("status") in (None, "posted", "sent") else row.get("status"),
        "publication_error": row.get("post_error"),
        "discord_message_ids": [int(mid) for mid in row.get("discord_message_ids") or [] if str(mid).isdigit()],
        "publication_attempts": row.get("publication_attempts") or 1,
        "last_published_at": row.get("posted_at") or row.get("created_at"),
    }


def topic_from_watch(row: Dict[str, Any], environment: str) -> Dict[str, Any]:
    title = row.get("title") or row.get("topic") or row.get("duplicate_key") or "legacy-watch-topic"
    canonical_key = canonicalize_topic_key(row.get("duplicate_key") or title)
    return {
        "canonical_key": canonical_key,
        "display_slug": row.get("duplicate_key") or canonical_key,
        "guild_id": row.get("guild_id"),
        "environment": environment,
        "state": "watching",
        "headline": title,
        "summary": {
            "reason": row.get("reason") or row.get("notes"),
            "legacy_watch_id": row.get("watch_id") or row.get("watchlist_id"),
            "origin_reason": row.get("origin_reason"),
            "evidence": row.get("evidence"),
            "revisit_count": row.get("revisit_count"),
        },
        "source_authors": authors_from(row),
        "revisit_at": row.get("next_revisit_at") or row.get("expires_at"),
    }


def upsert_topic(sb, topic: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {**topic, "topic_id": f"dry:{topic['canonical_key']}"}
    result = (
        sb.table("topics")
        .upsert(topic, on_conflict="environment,guild_id,canonical_key")
        .execute()
    )
    data = result.data or []
    return data[0] if data else topic


def upsert_sources(sb, topic_id: str, row: Dict[str, Any], environment: str, dry_run: bool) -> int:
    count = 0
    for message_id in message_ids_from(row):
        payload = {
            "topic_id": topic_id,
            "message_id": int(message_id),
            "guild_id": row.get("guild_id"),
            "environment": environment,
        }
        count += 1
        if not dry_run:
            sb.table("topic_sources").upsert(payload, on_conflict="topic_id,message_id").execute()
    return count


def backfill_rows(sb, rows: Iterable[Dict[str, Any]], mapper, environment: str, dry_run: bool) -> Dict[str, int]:
    topics = 0
    sources = 0
    for row in rows:
        topic = mapper(row, environment)
        if not topic.get("guild_id"):
            continue
        stored = upsert_topic(sb, topic, dry_run)
        topics += 1
        topic_id = stored.get("topic_id")
        if topic_id:
            sources += upsert_sources(sb, topic_id, row, environment, dry_run)
    return {"topics": topics, "sources": sources}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", default=os.getenv("LIVE_UPDATE_ENVIRONMENT", "prod"), choices=["prod", "dev"])
    parser.add_argument("--guild-id", type=int)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    sb = get_client()
    feed_rows = fetch_all(sb, "live_update_feed_items", environment=args.environment, guild_id=args.guild_id, limit=args.page_size)
    watch_rows = fetch_all(sb, "live_update_watchlist", environment=args.environment, guild_id=args.guild_id, limit=args.page_size)
    posted = backfill_rows(sb, feed_rows, topic_from_feed_item, args.environment, args.dry_run)
    watching = backfill_rows(sb, watch_rows, topic_from_watch, args.environment, args.dry_run)

    print("live-update topic backfill parity")
    print(f"environment={args.environment} guild_id={args.guild_id or 'all'} dry_run={args.dry_run}")
    print(f"posted legacy_rows={len(feed_rows)} topics_upserted={posted['topics']} sources_upserted={posted['sources']}")
    print(f"watching legacy_rows={len(watch_rows)} topics_upserted={watching['topics']} sources_upserted={watching['sources']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
