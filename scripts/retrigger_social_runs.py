#!/usr/bin/env python3
"""Retrigger terminal (needs_review) live-update social runs.

Terminal runs are replay-guarded: ``handle_live_update_publish_results`` skips
rows whose ``terminal_status`` is already set (that guard exists so replaying a
handoff can't overwrite a finished draft or spam the admin). This script is the
explicit escape hatch:

1. Loads the target runs (by run_id, or ``--needs-review`` for all of them).
2. Resets each run (``terminal_status → NULL`` and clears the stale
   ``review_message_id`` / ``expires_at`` binding) so the replay guard passes.
3. Reconstructs the ``LiveUpdateHandoffPayload`` from the stored row —
   ``publish_units`` holds the topic summary data the handoff needs.
4. Connects a real Discord client and re-invokes
   ``LiveUpdateSocialService.handle_live_update_publish_results`` so the social
   agent re-runs with the *current* code in this checkout.

The agent's needs-review DMs are batched (60s window by default), so the script
stays alive until the batch flushes, then prints each run's post-run state.

⚠️ Connecting with a bot token replaces that bot's gateway session if it is
already running elsewhere (e.g. prod on Railway); the other process reconnects
shortly after this script exits.

Usage:
    python scripts/retrigger_social_runs.py <run_id> [<run_id> ...]
    python scripts/retrigger_social_runs.py --needs-review
    python scripts/retrigger_social_runs.py --dry-run --needs-review
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.db_handler import DatabaseHandler
from src.features.sharing.live_update_social.contracts import LiveUpdateHandoffPayload
from src.features.sharing.live_update_social.service import (
    LiveUpdateSocialService,
    TERMINAL_DM_BATCH_WINDOW_SECONDS,
)

logger = logging.getLogger("retrigger-social-runs")

MAX_RETRIGGER_BATCH = 25


def _new_discord_client() -> discord.Client:
    return discord.Client(intents=discord.Intents.none())


async def _login_with_backoff(token: str) -> discord.Client:
    delay = 30.0
    while True:
        client = _new_discord_client()
        try:
            await client.login(token)
            return client
        except discord.HTTPException as exc:
            status = getattr(exc, "status", None)
            if status != 429 and not (status and 500 <= int(status) < 600):
                await client.close()
                raise
            logger.warning(
                "Discord login failed with HTTP %s; retrying in %.0fs",
                status,
                delay,
            )
            await client.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)
        except Exception:
            await client.close()
            raise


def _resolve_target_runs(
    db_handler: DatabaseHandler,
    run_ids: List[str],
    *,
    needs_review_all: bool,
    since_hours: int,
) -> List[Dict[str, Any]]:
    """Return the run rows to retrigger, deduped and in created order."""
    rows: Dict[str, Dict[str, Any]] = {}

    if run_ids:
        for run_id in run_ids:
            row = db_handler.get_live_update_social_run(run_id.strip())
            if not row:
                logger.warning("run %s not found — skipping", run_id)
                continue
            rows[row["run_id"]] = row

    if needs_review_all:
        if not db_handler.supabase:
            raise RuntimeError("Supabase client unavailable — cannot query --needs-review")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=since_hours)
        ).isoformat()
        # needs_review runs plus STALLED runs (terminal_status IS NULL but the
        # row has trace entries — a read-tool dispatch that never set a
        # terminal). Stalled runs are invisible to every review surface, so
        # --needs-review must sweep them too.
        result = (
            db_handler.supabase.table("live_update_social_runs")
            .select("*")
            .or_("terminal_status.eq.needs_review,terminal_status.is.null")
            .gte("created_at", cutoff)
            .order("created_at", desc=False)
            .limit(MAX_RETRIGGER_BATCH)
            .execute()
        )
        for row in result.data or []:
            rows[row["run_id"]] = row

    ordered = sorted(rows.values(), key=lambda r: r.get("created_at") or "")
    if len(ordered) > MAX_RETRIGGER_BATCH:
        logger.warning(
            "target set exceeds %d runs — truncating to the oldest %d",
            MAX_RETRIGGER_BATCH,
            MAX_RETRIGGER_BATCH,
        )
        ordered = ordered[:MAX_RETRIGGER_BATCH]
    return ordered


def _build_payload(row: Dict[str, Any]) -> Optional[LiveUpdateHandoffPayload]:
    """Rebuild the handoff payload from the stored run row."""
    publish_units = row.get("publish_units") or {}
    guild_id = row.get("guild_id")
    channel_id = publish_units.get("channel_id") or row.get("channel_id")
    try:
        return LiveUpdateHandoffPayload(
            topic_id=row["topic_id"],
            guild_id=int(guild_id) if guild_id else 0,
            channel_id=int(channel_id) if channel_id else 0,
            platform=row.get("platform") or "twitter",
            action=row.get("action") or "post",
            status="sent",  # the run already exists; only sent/partial are eligible
            source_metadata=row.get("source_metadata") or {},
            topic_summary_data=publish_units,
            vendor=row.get("chain_vendor") or "codex",
            depth=row.get("chain_depth") or "high",
            with_feedback=bool(row.get("chain_with_feedback", True)),
            deepseek_provider=row.get("chain_deepseek_provider") or "direct",
        )
    except Exception as e:
        logger.error("cannot build payload for run %s: %s", row.get("run_id"), e)
        return None


def _reset_run(db_handler: DatabaseHandler, row: Dict[str, Any]) -> bool:
    """Clear terminal status + stale review binding so the replay guard passes."""
    if not db_handler.storage_handler or not db_handler.storage_handler.supabase_client:
        logger.error("supabase client unavailable — cannot reset run %s", row.get("run_id"))
        return False
    try:
        (
            db_handler.storage_handler.supabase_client.table("live_update_social_runs")
            .update({
                "terminal_status": None,
                "review_message_id": None,
                "expires_at": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("run_id", row["run_id"])
            .execute()
        )
        return True
    except Exception as e:
        logger.error("reset failed for run %s: %s", row.get("run_id"), e)
        return False


def _topic_title(row: Dict[str, Any]) -> str:
    return str((row.get("publish_units") or {}).get("title") or "(untitled)")


async def _run_retriggers(
    db_handler: DatabaseHandler,
    rows: List[Dict[str, Any]],
    *,
    batch_window: float,
) -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set — cannot connect for media inspection")
    client = await _login_with_backoff(token)
    try:
        svc = LiveUpdateSocialService(db_handler=db_handler, bot=client)
        svc._terminal_batch_window = batch_window

        payloads = []
        for row in rows:
            payload = _build_payload(row)
            if payload is None:
                continue
            payloads.append((row, payload))

        if not payloads:
            logger.warning("no eligible runs to retrigger")
            return

        # Reset all targets first, then invoke — if any reset fails, abort
        # before any agent run so we never retrigger a half-reset batch.
        for row, _ in payloads:
            if not _reset_run(db_handler, row):
                logger.error(
                    "aborting — could not reset run %s; no agent runs started",
                    row["run_id"],
                )
                return

        logger.info("reset %d run(s) — invoking social agent", len(payloads))
        for row, payload in payloads:
            run_id = await svc.handle_live_update_publish_results(payload)
            logger.info(
                "invoked run %s → %s (%s)",
                row["run_id"],
                run_id,
                _topic_title(row),
            )

        # The batch flush is the only async tail (draft/proposal DMs send
        # inline). Wait for it, then give inline sends a beat to land.
        flush_task = getattr(svc, "_terminal_flush_task", None)
        if flush_task is not None and not flush_task.done():
            try:
                await asyncio.wait_for(flush_task, timeout=batch_window + 60)
            except asyncio.TimeoutError:
                logger.warning("batch flush did not finish within timeout")
        await asyncio.sleep(2)

        print("\n=== Post-run state ===")
        for row, _ in payloads:
            final = db_handler.get_live_update_social_run(row["run_id"])
            if not final:
                print(f"- {row['run_id']}: MISSING")
                continue
            print(
                f"- {final['run_id'][:8]}… "
                f"terminal={final.get('terminal_status')} "
                f"review_msg={'yes' if final.get('review_message_id') else 'no'} "
                f"draft={'yes' if (final.get('draft_text') or '').strip() else 'no'} "
                f"proposals={len(final.get('proposals') or [])} "
                f"| {_topic_title(final)}"
            )
    finally:
        await client.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Retrigger needs_review live-update social runs")
    parser.add_argument("run_ids", nargs="*", help="run_id(s) to retrigger")
    parser.add_argument(
        "--needs-review",
        action="store_true",
        help="retrigger all recent needs_review runs (see --since-hours)",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=168,
        help="lookback for --needs-review (default 168 = 7 days)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument(
        "--batch-window",
        type=float,
        default=TERMINAL_DM_BATCH_WINDOW_SECONDS,
        help="seconds to collapse needs-review DMs into one (default 60)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy_logger in ("httpx", "httpcore", "supabase", "postgrest"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    if not args.run_ids and not args.needs_review:
        parser.error("provide run_id(s) or --needs-review")

    db_handler = DatabaseHandler(dev_mode=False)
    rows = _resolve_target_runs(
        db_handler,
        args.run_ids,
        needs_review_all=args.needs_review,
        since_hours=args.since_hours,
    )
    if not rows:
        print("No eligible runs found.")
        return

    print(f"=== Retrigger plan: {len(rows)} run(s) ===")
    for row in rows:
        status = row.get("terminal_status")
        if status not in ("needs_review", None):
            print(
                f"- SKIP {row['run_id']} "
                f"(terminal_status={status!r} — only needs_review or stalled "
                "runs are retriggerable)"
            )
            continue
        print(f"- {row['run_id']} | {_topic_title(row)}")

    eligible = [
        row for row in rows if row.get("terminal_status") in ("needs_review", None)
    ]
    if not eligible:
        print("Nothing eligible to retrigger.")
        return
    if args.dry_run:
        print("\nDry run — no changes made. Re-run without --dry-run to execute.")
        return

    await _run_retriggers(db_handler, eligible, batch_window=args.batch_window)


if __name__ == "__main__":
    asyncio.run(main())
