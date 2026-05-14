#!/usr/bin/env python3
"""Run live-update previews locally without starting the full Discord bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.db_handler import DatabaseHandler
from src.common.llm.claude_client import ClaudeClient
from src.common.llm.deepseek_client import DeepSeekClient
from src.features.summarising.topic_editor import TopicEditor


class MinimalDiscordBot:
    """Small adapter exposing only what the live preview services need."""

    def __init__(
        self,
        client: discord.Client,
        db_handler: DatabaseHandler,
        logger: logging.Logger,
        *,
        llm_client: Any | None = None,
    ):
        self.client = client
        self.db_handler = db_handler
        self.server_config = db_handler.server_config
        self.logger = logger
        self.dev_mode = True
        self.claude_client = llm_client or ClaudeClient()

    @property
    def user(self):
        return self.client.user

    def get_channel(self, channel_id: int):
        return self.client.get_channel(int(channel_id))

    async def fetch_channel(self, channel_id: int):
        return await self.client.fetch_channel(int(channel_id))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_token() -> str:
    token = os.getenv("DEV_DISCORD_BOT_TOKEN")
    if token:
        return token
    if _env_flag("ALLOW_PROD_DISCORD_TOKEN_IN_DEV", False):
        token = os.getenv("DISCORD_BOT_TOKEN")
        if token:
            return token
    raise RuntimeError(
        "DEV_DISCORD_BOT_TOKEN is required. Set ALLOW_PROD_DISCORD_TOKEN_IN_DEV=true "
        "only for this minimal preview runner if you intentionally want to reuse DISCORD_BOT_TOKEN."
    )


async def _run_topic_editor_scheduled_pass(editor: "TopicEditor", *, trigger: str) -> None:
    result = await editor.run_once(trigger)
    logging.info(
        "TopicEditor result: status=%s tools=%s published=%s rejected=%s overrides=%s observations=%s latency_ms=%s",
        result.get("status"),
        result.get("tool_calls"),
        sum(1 for r in (result.get("publish_results") or []) if r),
        result.get("rejected", 0),
        result.get("overrides", 0),
        result.get("observations", 0),
        result.get("latency_ms"),
    )


def _fetch_latest_prod_source_window(db_handler: DatabaseHandler, *, guild_id: int | None, limit: int) -> List[Dict[str, Any]]:
    if not db_handler.supabase:
        raise RuntimeError("Supabase client is required for topic-editor replay")
    query = (
        db_handler.supabase.table("discord_messages")
        .select("*")
        .eq("is_deleted", False)
        .order("created_at", desc=True)
        .order("message_id", desc=True)
        .limit(limit)
    )
    if guild_id is not None:
        query = query.eq("guild_id", guild_id)
    result = query.execute()
    return list(reversed(result.data or []))


def _attach_latest_window_replay(db_handler: DatabaseHandler, messages: List[Dict[str, Any]]) -> None:
    def latest_window(*_args, **_kwargs):
        return messages

    db_handler.get_archived_messages_after_checkpoint = latest_window


async def _run_topic_editor_replay(args: argparse.Namespace) -> None:
    os.environ["TOPIC_EDITOR_PUBLISHING_ENABLED"] = "false"
    guild_id = int(args.guild_id) if args.guild_id else int(os.getenv("GUILD_ID") or os.getenv("DEV_GUILD_ID") or "0") or None
    live_channel_id = int(args.live_channel_id) if args.live_channel_id else int(
        os.getenv("LIVE_UPDATE_CHANNEL_ID") or os.getenv("SUMMARY_CHANNEL_ID") or os.getenv("DEV_SUMMARY_CHANNEL_ID") or "0"
    ) or None
    db_handler = DatabaseHandler(dev_mode=False)
    messages = _fetch_latest_prod_source_window(db_handler, guild_id=guild_id, limit=args.source_limit)
    _attach_latest_window_replay(db_handler, messages)
    llm_client = _build_topic_llm_client(args.llm_client)
    bot = MinimalDiscordBot(
        _new_discord_client(),
        db_handler,
        logging.getLogger("topic-editor-replay"),
        llm_client=llm_client,
    )
    bot.dev_mode = False
    editor = TopicEditor(
        bot=bot,
        db_handler=db_handler,
        llm_client=bot.claude_client,
        guild_id=guild_id,
        live_channel_id=live_channel_id,
        environment="prod",
        source_limit=args.source_limit,
    )
    editor.trace_channel_id = None
    logging.info(
        "Starting topic-editor publishing-off replay: prod source_count=%s guild=%s live_channel=%s llm=%s model=%s",
        len(messages),
        guild_id,
        live_channel_id,
        args.llm_client,
        editor.model,
    )
    result = await editor.run_once("prod_replay_publishing_off")
    print("=== Topic editor replay result ===")
    print(result)
    print("\n=== Transitions ===")
    for outcome in result.get("outcomes") or []:
        print(outcome)
    print("\n=== Would-publish Discord messages ===")
    for publish_result in result.get("publish_results") or []:
        for message in publish_result.get("messages") or []:
            print("---")
            print(message)
    print("\n=== Operator trace ===")
    for message in result.get("trace_messages") or []:
        print("---")
        print(message)


def _new_discord_client() -> discord.Client:
    return discord.Client(intents=discord.Intents.none())


def _build_topic_llm_client(provider: str) -> Any:
    provider = (provider or "claude").strip().lower()
    if provider == "claude":
        return ClaudeClient()
    if provider == "deepseek":
        os.environ.setdefault("TOPIC_EDITOR_MODEL", "deepseek-v4-pro")
        return DeepSeekClient()
    raise ValueError("Unsupported --llm-client value. Use 'claude' or 'deepseek'.")


async def _login_with_backoff(token: str) -> discord.Client:
    """Discord can temporarily rate-limit bot login; keep local dev alive until it clears."""
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
            logging.warning(
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


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run local dry-run live-update previews")
    parser.add_argument("--interval-minutes", type=float, default=60.0)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--art-channel-id", help="Override DEV_ART_CHANNEL_ID for this run")
    parser.add_argument("--summary-channel-id", help="Override DEV_SUMMARY_CHANNEL_ID for this run")
    parser.add_argument("--top-gens-channel-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--topic-editor-replay-prod",
        action="store_true",
        help="Run one publishing-off TopicEditor sanity replay over the latest prod source-message window and print outputs.",
    )
    parser.add_argument("--source-limit", type=int, default=200, help="Source message count for TopicEditor replay")
    parser.add_argument("--guild-id", help="Override prod guild id for TopicEditor replay")
    parser.add_argument("--live-channel-id", help="Override prod live update channel id for TopicEditor replay")
    parser.add_argument(
        "--llm-client",
        choices=["claude", "deepseek"],
        default=None,
        help="LLM backend for TopicEditor. Can also be set with TOPIC_EDITOR_LLM_CLIENT.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=True)
    args.llm_client = args.llm_client or os.getenv("TOPIC_EDITOR_LLM_CLIENT", "claude")
    if args.art_channel_id:
        os.environ["DEV_ART_CHANNEL_ID"] = str(args.art_channel_id)
    if args.summary_channel_id:
        os.environ["DEV_SUMMARY_CHANNEL_ID"] = str(args.summary_channel_id)
    if args.top_gens_channel_id:
        logging.warning("--top-gens-channel-id is ignored; media is now auto-shortlisted by TopicEditor.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy_logger in ("httpx", "httpcore", "supabase", "postgrest"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    if args.topic_editor_replay_prod:
        os.environ["TOPIC_EDITOR_LLM_CLIENT"] = args.llm_client
        await _run_topic_editor_replay(args)
        return

    token = _resolve_token()
    db_handler = DatabaseHandler(dev_mode=True)
    client = await _login_with_backoff(token)

    try:
        llm_client = _build_topic_llm_client(args.llm_client)
        bot = MinimalDiscordBot(client, db_handler, logging.getLogger("live-update-dev"), llm_client=llm_client)

        guild_id = int(os.getenv("DEV_GUILD_ID") or os.getenv("GUILD_ID") or "0") or None
        live_channel_id = int(os.getenv("DEV_LIVE_UPDATE_CHANNEL_ID") or os.getenv("LIVE_UPDATE_TRACE_CHANNEL_ID") or "0") or None
        editor = TopicEditor(
            bot=bot,
            db_handler=db_handler,
            llm_client=bot.claude_client,
            guild_id=guild_id,
            live_channel_id=live_channel_id,
            environment="dev",
        )
        logging.info(
            "Starting local TopicEditor dev runner: guild=%s live_channel=%s trace=%s publishing=%s interval=%sm llm=%s model=%s",
            guild_id,
            live_channel_id,
            editor.trace_channel_id,
            editor.publishing_enabled,
            args.interval_minutes,
            args.llm_client,
            editor.model,
        )
        await _run_topic_editor_scheduled_pass(editor, trigger="local_dev_once" if args.once else "local_dev_scheduled")
        while not args.once:
            await asyncio.sleep(max(1.0, args.interval_minutes * 60.0))
            await _run_topic_editor_scheduled_pass(editor, trigger="local_dev_scheduled")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
