#!/usr/bin/env python3
"""Export archived Discord messages as a Hugging Face-compatible JSONL dataset.

The export excludes authors who explicitly opted out of content sharing via
members.allow_content_sharing = false. Unset values are treated as allowed,
matching the bot's existing sharing behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

DEFAULT_COLUMNS = (
    "message_id,guild_id,channel_id,thread_id,author_id,content,created_at,"
    "edited_at,attachments,reaction_count,reactors,reference_id,is_deleted"
)
MEMBER_COLUMNS = "member_id,bot,allow_content_sharing"
CHANNEL_COLUMNS = "channel_id,channel_name,category_id,nsfw,guild_id"


def _env_int(*names: str) -> Optional[int]:
    for name in names:
        raw = os.getenv(name)
        if raw:
            try:
                return int(raw)
            except ValueError:
                raise SystemExit(f"{name} must be an integer")
    return None


def get_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(url, key)


def paged_select(query, *, batch_size: int) -> Iterable[Dict[str, Any]]:
    offset = 0
    while True:
        result = query.range(offset, offset + batch_size - 1).execute()
        rows = result.data or []
        if not rows:
            break
        yield from rows
        if len(rows) < batch_size:
            break
        offset += batch_size


def fetch_opted_out_author_ids(client, *, batch_size: int) -> Set[int]:
    query = (
        client.table("members")
        .select("member_id")
        .eq("allow_content_sharing", False)
        .order("member_id")
    )
    return {
        int(row["member_id"])
        for row in paged_select(query, batch_size=batch_size)
        if row.get("member_id") is not None
    }


def fetch_bot_author_ids(client, *, batch_size: int) -> Set[int]:
    query = client.table("members").select("member_id").eq("bot", True).order("member_id")
    return {
        int(row["member_id"])
        for row in paged_select(query, batch_size=batch_size)
        if row.get("member_id") is not None
    }


def fetch_channel_map(client, *, guild_id: Optional[int], batch_size: int) -> Dict[int, Dict[str, Any]]:
    query = client.table("discord_channels").select(CHANNEL_COLUMNS).order("channel_id")
    if guild_id is not None:
        query = query.eq("guild_id", guild_id)
    rows = paged_select(query, batch_size=batch_size)
    return {int(row["channel_id"]): row for row in rows if row.get("channel_id") is not None}


def iter_messages(
    client,
    *,
    guild_id: Optional[int],
    start_date: Optional[str],
    end_date: Optional[str],
    include_deleted: bool,
    batch_size: int,
) -> Iterable[Dict[str, Any]]:
    last_message_id: Optional[int] = None
    while True:
        query = client.table("discord_messages").select(DEFAULT_COLUMNS).order("message_id", desc=True)
        if guild_id is not None:
            query = query.eq("guild_id", guild_id)
        if start_date:
            query = query.gte("created_at", start_date)
        if end_date:
            query = query.lt("created_at", end_date)
        if not include_deleted:
            query = query.neq("is_deleted", True)
        if last_message_id is not None:
            query = query.lt("message_id", last_message_id)

        result = query.limit(batch_size).execute()
        rows = result.data or []
        if not rows:
            break
        yield from rows

        last_row_id = as_int(rows[-1].get("message_id"))
        if last_row_id is None or last_row_id == last_message_id:
            break
        last_message_id = last_row_id
        if len(rows) < batch_size:
            break


def parse_jsonish(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        if not value.strip():
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stable_hash(value: Any, *, salt: str, prefix: str) -> Optional[str]:
    if value is None:
        return None
    digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def message_jump_url(row: Dict[str, Any]) -> Optional[str]:
    guild_id = row.get("guild_id")
    channel_id = row.get("thread_id") or row.get("channel_id")
    message_id = row.get("message_id")
    if not guild_id or not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def build_dataset_record(
    row: Dict[str, Any],
    *,
    channel: Optional[Dict[str, Any]],
    salt: str,
    include_raw_ids: bool,
    include_attachment_urls: bool,
    include_jump_urls: bool,
) -> Dict[str, Any]:
    attachments = parse_jsonish(row.get("attachments"), [])
    if not isinstance(attachments, list):
        attachments = []

    if include_attachment_urls:
        attachment_records = attachments
    else:
        attachment_records = [
            {
                "filename": string_or_empty(item.get("filename")),
                "content_type": string_or_empty(item.get("content_type")),
                "size": int(item.get("size") or 0),
            }
            for item in attachments
            if isinstance(item, dict)
        ]

    reactors = parse_jsonish(row.get("reactors"), [])
    if not isinstance(reactors, list):
        reactors = []

    record = {
        "id": string_or_empty(stable_hash(row.get("message_id"), salt=salt, prefix="msg")),
        "text": row.get("content") or "",
        "created_at": string_or_empty(row.get("created_at")),
        "edited_at": string_or_empty(row.get("edited_at")),
        "channel": {
            "id": string_or_empty(stable_hash(row.get("channel_id"), salt=salt, prefix="chan")),
            "name": string_or_empty((channel or {}).get("channel_name")),
            "category_id": string_or_empty(stable_hash((channel or {}).get("category_id"), salt=salt, prefix="cat")),
            "nsfw": bool((channel or {}).get("nsfw", False)),
        },
        "author": {
            "id": string_or_empty(stable_hash(row.get("author_id"), salt=salt, prefix="user")),
        },
        "thread_id": string_or_empty(stable_hash(row.get("thread_id"), salt=salt, prefix="thread")),
        "reference_id": string_or_empty(stable_hash(row.get("reference_id"), salt=salt, prefix="msg")),
        "reaction_count": int(row.get("reaction_count") or 0),
        "reactor_count": len(reactors),
        "attachment_count": len(attachments),
        "attachments": attachment_records,
    }

    if include_raw_ids:
        record["raw"] = {
            "message_id": row.get("message_id"),
            "guild_id": row.get("guild_id"),
            "channel_id": row.get("channel_id"),
            "thread_id": row.get("thread_id"),
            "author_id": row.get("author_id"),
            "reference_id": row.get("reference_id"),
        }

    if include_jump_urls:
        record["discord_url"] = message_jump_url(row)

    return record


def skip_reason(
    row: Dict[str, Any],
    *,
    opted_out_author_ids: Set[int],
    bot_author_ids: Set[int],
    include_empty: bool,
) -> Optional[str]:
    author_id = as_int(row.get("author_id"))
    if author_id in opted_out_author_ids:
        return "opted_out"
    if author_id in bot_author_ids:
        return "bot"
    if not include_empty and not (row.get("content") or "").strip():
        return "empty"
    return None


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def write_dataset_card(path: Path, *, stats: Dict[str, Any], args: argparse.Namespace) -> None:
    card = f"""---
license: other
task_categories:
- text-generation
language:
- en
pretty_name: Discord Archive
tags:
- discord
- ai-art
- open-source
- community
---

# Discord Archive

This is an archive of messages from the Banodoco Discord community, where
technical and artistic practitioners have been discussing open source AI art for
the past three years.

The archive captures a long-running community record of people learning,
training, evaluating, and using open source AI art models in practice. It
contains discussion around model releases, workflows, tooling, troubleshooting,
creative experiments, training details, and the many small technical and
artistic nuances that are hard to recover from model cards or formal
documentation alone.

Banodoco is an open source AI art community with many talented contributors.
This dataset is intended as a research and search resource for understanding how
people have explored and unlocked open source AI art models over the past few
years.

## Use With Agents

If you want to query this archive from a coding agent instead of downloading
the full dataset, use the Hivemind repo:

https://github.com/banodoco/hivemind

Hivemind packages the Banodoco Discord message feed as an agent skill with
query patterns, channel guidance, and examples for finding community knowledge.

## Privacy Filter

Messages are excluded when the author has `members.allow_content_sharing = false`.
Unset sharing preferences are treated as allowed, matching the production bot's
existing sharing behavior. Bot messages and deleted messages are excluded by default.

Discord user, message, channel, thread, category, and reference IDs are hashed by
default. Raw Discord IDs are included only when the exporter is run with
`--include-raw-ids`.

Attachment URLs are not included by default. Attachment records keep only basic
metadata such as filename, content type, and size.

## Export Stats

- Exported messages: {stats["exported_messages"]}
- Skipped opted-out author messages: {stats["skipped_opted_out_messages"]}
- Skipped bot messages: {stats["skipped_bot_messages"]}
- Skipped empty messages: {stats["skipped_empty_messages"]}
- Opted-out authors: {stats["opted_out_authors"]}
- Bot authors: {stats["bot_authors"]}
- Guild filter: {args.guild_id or "none"}
- Start date: {args.start_date or "none"}
- End date: {args.end_date or "none"}
"""
    path.write_text(card, encoding="utf-8")


def write_metadata(path: Path, *, stats: Dict[str, Any], args: argparse.Namespace) -> None:
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_files": ["train.jsonl"],
        "privacy_filter": {
            "excluded_member_column": "members.allow_content_sharing",
            "excluded_member_value": False,
            "unset_preferences_treated_as_allowed": True,
            "raw_ids_included": bool(args.include_raw_ids),
            "attachment_urls_included": bool(args.include_attachment_urls),
            "jump_urls_included": bool(args.include_jump_urls),
        },
        "filters": {
            "guild_id": args.guild_id,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "include_deleted": args.include_deleted,
            "include_bots": args.include_bots,
            "include_empty": args.include_empty,
        },
        "stats": stats,
    }
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def export_dataset(args: argparse.Namespace) -> Dict[str, Any]:
    client = get_client()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    salt = args.hash_salt or os.getenv("HF_DATASET_HASH_SALT") or os.getenv("SUPABASE_SERVICE_KEY", "")
    if not salt:
        raise SystemExit("Set HF_DATASET_HASH_SALT or use --hash-salt for stable anonymized IDs")

    opted_out_ids = fetch_opted_out_author_ids(client, batch_size=args.batch_size)
    bot_ids = set() if args.include_bots else fetch_bot_author_ids(client, batch_size=args.batch_size)
    channels = fetch_channel_map(client, guild_id=args.guild_id, batch_size=args.batch_size)

    stats = {
        "exported_messages": 0,
        "skipped_opted_out_messages": 0,
        "skipped_bot_messages": 0,
        "skipped_empty_messages": 0,
        "opted_out_authors": len(opted_out_ids),
        "bot_authors": len(bot_ids),
    }

    def records() -> Iterable[Dict[str, Any]]:
        seen = 0
        for row in iter_messages(
            client,
            guild_id=args.guild_id,
            start_date=args.start_date,
            end_date=args.end_date,
            include_deleted=args.include_deleted,
            batch_size=args.batch_size,
        ):
            seen += 1
            reason = skip_reason(
                row,
                opted_out_author_ids=opted_out_ids,
                bot_author_ids=bot_ids,
                include_empty=args.include_empty,
            )
            if reason == "opted_out":
                stats["skipped_opted_out_messages"] += 1
                continue
            if reason == "bot":
                stats["skipped_bot_messages"] += 1
                continue
            if reason == "empty":
                stats["skipped_empty_messages"] += 1
                continue

            stats["exported_messages"] += 1
            yield build_dataset_record(
                row,
                channel=channels.get(as_int(row.get("channel_id"))),
                salt=salt,
                include_raw_ids=args.include_raw_ids,
                include_attachment_urls=args.include_attachment_urls,
                include_jump_urls=args.include_jump_urls,
            )

            if args.limit and stats["exported_messages"] >= args.limit:
                break

        stats["scanned_messages"] = seen

    write_jsonl(out_dir / "train.jsonl", records())
    write_dataset_card(out_dir / "README.md", stats=stats, args=args)
    write_metadata(out_dir / "export_metadata.json", stats=stats, args=args)
    return stats


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Supabase Discord messages to a Hugging Face JSONL dataset."
    )
    parser.add_argument("--output-dir", default="hf_discord_dataset", help="Output dataset directory")
    parser.add_argument("--guild-id", type=int, default=_env_int("TARGET_GUILD_ID", "GUILD_ID"))
    parser.add_argument("--start-date", help="Inclusive ISO timestamp/date filter")
    parser.add_argument("--end-date", help="Exclusive ISO timestamp/date filter")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--limit", type=int, help="Stop after exporting this many messages")
    parser.add_argument("--hash-salt", help="Salt for stable anonymized IDs")
    parser.add_argument("--include-raw-ids", action="store_true", help="Include raw Discord IDs")
    parser.add_argument("--include-attachment-urls", action="store_true", help="Include attachment URL fields")
    parser.add_argument("--include-jump-urls", action="store_true", help="Include Discord message jump URLs")
    parser.add_argument("--include-deleted", action="store_true", help="Include messages marked deleted")
    parser.add_argument("--include-bots", action="store_true", help="Include bot-authored messages")
    parser.add_argument("--include-empty", action="store_true", help="Include messages with empty text")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    stats = export_dataset(args)
    print(json.dumps(stats, indent=2, sort_keys=True))
    print(f"Wrote Hugging Face dataset files to {Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
