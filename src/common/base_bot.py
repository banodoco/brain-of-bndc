# src/common/base_bot.py

import asyncio
from datetime import datetime
from typing import Optional, Any, Dict
import traceback
import os
import subprocess
import time
import aiohttp

import discord
from discord.ext import commands

from src.common.rate_limiter import RateLimiter


def _current_commit_sha() -> str:
    for env_name in (
        "RAILWAY_GIT_COMMIT_SHA",
        "GIT_COMMIT_SHA",
        "SOURCE_COMMIT",
        "COMMIT_SHA",
        "VERCEL_GIT_COMMIT_SHA",
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            return value[:7]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip() or "unknown"
    except Exception:
        return "unknown"


def _claim_startup_notification(bot: commands.Bot, admin_id: int, sha: str) -> bool:
    db_handler = getattr(bot, "db_handler", None)
    claim = getattr(db_handler, "try_claim_bot_event", None)
    if not callable(claim):
        return True

    # Railway can briefly overlap old and new containers during deploys. One
    # startup DM per admin per 10-minute window is enough signal without spam.
    bucket = int(time.time() // 600)
    return bool(claim(
        event_key=f"startup_admin_dm:{admin_id}:{bucket}",
        event_type="startup_admin_dm",
        payload={
            "admin_id": str(admin_id),
            "guild_id": getattr(bot.guilds[0], "id", None) if getattr(bot, "guilds", None) else None,
            "sha": sha,
            "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID"),
            "service_id": os.getenv("RAILWAY_SERVICE_ID"),
        },
    ))


class BaseDiscordBot(commands.Bot):
    """
    Base class for all Discord bots, relying on discord.py's built-in
    heartbeat and reconnection logic rather than manual heartbeat checks.
    """

    def __init__(self, command_prefix, logger, dev_mode=False, intents=None, **kwargs):
        # Create HTTP connector/timeouts only if an event loop is already running.
        # This avoids "no running event loop" when scripts instantiate the bot
        # before starting the loop (e.g., archive_discord.py main()).
        try:
            _ = asyncio.get_running_loop()
            connector = aiohttp.TCPConnector(
                limit=100,  # Total connection pool size
                limit_per_host=30,  # Connections per host
                ttl_dns_cache=300,  # DNS cache TTL
                use_dns_cache=True,
            )

            timeout = aiohttp.ClientTimeout(
                total=120,  # Total timeout for request (increased for slower networks)
                connect=60, # Connection timeout (increased from 30s)
                sock_read=60,
            )

            kwargs.update({
                'connector': connector,
                'timeout': timeout
            })
        except RuntimeError:
            # No running loop yet; fall back to discord.py defaults.
            pass
        
        # Block @everyone, @here, and role mentions in all bot messages by default.
        # Individual sends can override with their own allowed_mentions if needed.
        kwargs.setdefault(
            'allowed_mentions',
            discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        super().__init__(command_prefix=command_prefix, intents=intents, **kwargs)
        self.logger = logger
        self.dev_mode = dev_mode
        self.summary_now = False
        self.rate_limiter = RateLimiter()

        # Session management (optional, if you want to track session IDs):
        self._last_session_id: Optional[str] = None
        self._session_start_time: Optional[datetime] = None
        self._failed_session_count: int = 0

        # Summarizer or other cogs might set this if needed
        self._shutdown_flag: bool = False

        # For Summarizer Cog to track if we've run the immediate summary
        self.summarizer_ready = False

    async def setup_hook(self):
        """Called when the bot is starting up."""
        # Add the sync command
        @self.command()
        @commands.is_owner()
        async def sync(ctx):
            """Force sync slash commands."""
            try:
                synced = await self.tree.sync()
                await ctx.send(f"Synced {len(synced)} command(s) globally")
                
                if self.dev_mode:
                    for guild in self.guilds:
                        guild_synced = await self.tree.sync(guild=guild)
                        await ctx.send(f"Synced {len(guild_synced)} command(s) to guild {guild.id}")
            except Exception as e:
                await ctx.send(f"Failed to sync commands: {e}")

    async def start(self, *args, **kwargs):
        """Start the bot."""
        try:
            await super().start(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"Error in bot start: {e}")
            raise

    async def close(self):
        """Clean up resources on shutdown."""
        try:
            # Ensure HTTP session is cleaned up (if it's still open).
            if hasattr(self.http, "_session") and self.http._session:
                await self.http._session.close()

            await super().close()
        except Exception as e:
            self.logger.error(f"Error during bot shutdown: {str(e)}")
            self.logger.debug(traceback.format_exc())
            raise

    # -------------------------------------------------------------------------
    # The following method is optional. It logs certain gateway events
    # (op=9, code=4004, etc.), but *no longer* forces reconnections or modifies
    # your bot's connection state. You can remove this entire method if you
    # don't need these logs.
    # -------------------------------------------------------------------------
    async def on_socket_response(self, msg: Dict[str, Any]) -> None:
        """Handle WebSocket responses for errors/resumptions."""
        if not isinstance(msg, dict):
            return
        try:
            op_code = msg.get("op")
            event_type = msg.get("t")

            # Log invalid session
            if op_code == 9:  # Invalid session
                self.logger.error(f"Invalid session detected - Full message: {msg}")
                self._failed_session_count += 1
                self._last_session_id = None

            # Log auth failure
            elif msg.get("code") == 4004:  # Auth failure
                self.logger.critical(
                    "Authentication failed - bot token may be invalid. "
                    "Please check your token and try again."
                )
                await self.close()

            # Log new session
            elif event_type == "READY":
                session_id = msg.get("session_id")
                self._last_session_id = session_id
                self._session_start_time = datetime.now()
                self._failed_session_count = 0
                self.logger.info(
                    f"New session established - ID: {session_id}, "
                    f"Start time: {self._session_start_time.isoformat()}"
                )

            # Log resumed session
            elif event_type == "RESUMED":
                self.logger.info(
                    f"Session resumed successfully - ID: {self._last_session_id}, "
                    f"Failed attempts: {self._failed_session_count}"
                )
                self._failed_session_count = 0

        except Exception as e:
            self.logger.error(f"Error processing socket response: {str(e)}")
            self.logger.debug(f"Message that caused error: {msg}")
            self.logger.debug(traceback.format_exc())

    async def on_ready(self):
        """Called when the bot is ready."""
        self.logger.info(f"Bot is ready! Logged in as {self.user.name} (ID: {self.user.id})")
        self.logger.info(f"Dev mode: {self.dev_mode}")
        self.logger.info(f"Connected to {len(self.guilds)} guilds")
        # Initialize error handler (if you have a custom one)
        try:
            from src.common.error_handler import ErrorHandler
            self.error_handler = ErrorHandler(self)
        except ImportError:
            self.logger.warning("No custom error_handler found or import failed.")

        # Notify admin of startup via DM
        try:
            admin_id = int(os.getenv("ADMIN_USER_ID", "0"))
            if admin_id != 0:
                admin_user = await self.fetch_user(admin_id)
                self.logger.info(f"Successfully connected and can notify admin: {admin_user.name}")
                if not self.dev_mode:
                    sha = _current_commit_sha()
                    if _claim_startup_notification(self, admin_id, sha):
                        dm = await admin_user.create_dm()
                        await dm.send(f"Bot restarted — `{sha}` on {len(self.guilds)} guild(s)")
                    else:
                        self.logger.info("Startup admin DM suppressed by cross-container cooldown")
        except Exception as e:
            self.logger.error(f"Failed to verify/notify admin: {e}")

    async def cleanup(self) -> None:
        """Perform any necessary cleanup. Default implementation does nothing."""
        pass

    def is_connected(self) -> bool:
        """
        Return True if the bot has an active websocket connection.
        This is a lightweight utility, but note that discord.py
        handles reconnections automatically, so checking this
        is usually only for debug or informational purposes.
        """
        if not hasattr(self, "ws") or self.ws is None:
            return False
        # Try to use the is_closed() method if available
        is_closed_method = getattr(self.ws, "is_closed", None)
        if callable(is_closed_method):
            return not is_closed_method()
        # Fallback: check a 'close_code' attribute if available
        if hasattr(self.ws, "close_code"):
            return self.ws.close_code is None
        # If no reliable attribute, assume connected
        return True
