#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.db_handler import DatabaseHandler


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.last_hours is not None:
        if args.start or args.end:
            raise SystemExit("--last-hours cannot be combined with --start/--end")
        if args.last_hours <= 0:
            raise SystemExit("--last-hours must be greater than zero")
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=args.last_hours)
        return start, end
    if not args.start or not args.end:
        raise SystemExit("provide either --last-hours or both --start and --end")
    return _parse_time(args.start), _parse_time(args.end)


def _fetch_rows(storage, table: str, guild_id: int, start: str, end: str, limit: int = 500):
    if not getattr(storage, "supabase_client", None):
        return []
    try:
        query = (
            storage.supabase_client.table(table)
            .select("*")
            .eq("guild_id", guild_id)
            .gte("created_at", start)
            .lte("created_at", end)
            .order("created_at")
            .limit(limit)
        )
        return query.execute().data or []
    except Exception:
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze a bounded Supabase message window for topic-editor replay.")
    parser.add_argument("--guild-id", required=True, type=int)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--last-hours", type=float, help="Freeze a rolling UTC window ending now.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--out-root", default="tests/agentic/fixtures/windows")
    parser.add_argument("--env-file", action="append", default=[], help="Optional KEY=VALUE env file to load before DB setup.")
    args = parser.parse_args()

    for env_file in args.env_file:
        _load_env_file(Path(env_file).expanduser())

    start, end = _resolve_window(args)
    if start >= end:
        raise SystemExit("--start must be before --end")
    start_text = start.isoformat().replace("+00:00", "Z")
    end_text = end.isoformat().replace("+00:00", "Z")

    db = DatabaseHandler()
    rows = db.get_archived_messages_for_window(args.guild_id, start_text, end_text, limit=args.limit)
    if len(rows) >= args.limit:
        raise SystemExit(f"window hit limit={args.limit}; rerun with a narrower window or higher explicit limit")
    for row in rows:
        created = _parse_time(str(row.get("created_at")))
        if created < start or created > end:
            raise SystemExit(f"row outside requested window: {row.get('message_id')} {row.get('created_at')}")

    out = Path(args.out_root) / args.name
    _write_json(out / "source_messages.json", rows)
    _write_json(out / "active_topics.json", db.get_topics(guild_id=args.guild_id, states=["posted", "watching", "discarded"], limit=300, environment="prod") or [])
    _write_json(out / "topic_sources.json", _fetch_rows(db.storage_handler, "topic_sources", args.guild_id, start_text, end_text))
    _write_json(out / "recent_transitions.json", _fetch_rows(db.storage_handler, "topic_transitions", args.guild_id, start_text, end_text))
    _write_json(out / "media_metadata.json", [])
    _write_json(out / "server_config_snapshot.json", {
        "guild_id": args.guild_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "start": start_text,
        "end": end_text,
        "last_hours": args.last_hours,
    })
    (out / "notes.md").write_text(
        f"# {args.name}\n\nFrozen guild `{args.guild_id}` from `{start_text}` to `{end_text}`.\nRows: {len(rows)}\n"
    )
    print(json.dumps({
        "fixture_dir": str(out.resolve()),
        "guild_id": args.guild_id,
        "start": start_text,
        "end": end_text,
        "rows": len(rows),
    }, indent=2))


if __name__ == "__main__":
    main()
