# src/features/summarising/summariser_cog.py

from discord.ext import commands
import logging
import os
from discord.ext import tasks

from .live_update_editor import LiveUpdateEditor as LegacyLiveUpdateEditor
from .topic_editor import TopicEditor
from .live_top_creations import LiveTopCreations

MAX_RETRIES = 3
READY_TIMEOUT = 30
INITIAL_RETRY_DELAY = 5
MAX_RETRY_WAIT = 300  # 5 minutes

logger = logging.getLogger('DiscordBot')

LIVE_UPDATE_EDITOR_BACKEND_TOPIC = "topic"
LIVE_UPDATE_EDITOR_BACKEND_LEGACY = "legacy"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _build_topic_editor_llm_client():
    provider = os.getenv("TOPIC_EDITOR_LLM_CLIENT", "claude").strip().lower()
    if provider in {"", "claude"}:
        return None
    if provider == "deepseek":
        from src.common.llm.deepseek_client import DeepSeekClient

        os.environ.setdefault("TOPIC_EDITOR_MODEL", "deepseek-v4-pro")
        return DeepSeekClient()
    logger.warning(
        "Unknown TOPIC_EDITOR_LLM_CLIENT=%r; using the bot default Claude client",
        provider,
    )
    return None

class SummarizerCog(commands.Cog):
    """Runtime owner for the active live-update editor."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        live_update_editor: object | None = None,
        live_top_creations: LiveTopCreations | None = None,
        start_loops: bool = True,
    ):
        self.bot = bot
        db_handler = getattr(bot, "db_handler", None)
        if db_handler is None:
            raise RuntimeError("SummarizerCog requires bot.db_handler for live-update runtime")
        self.dev_mode = bool(getattr(bot, "dev_mode", False))
        self.live_updates_enabled = self.dev_mode or _env_flag("LIVE_UPDATES_ENABLED", False)
        # The old live-top-creations loop directly auto-posted media once it hit
        # the reaction threshold. TopicEditor now auto-shortlists those media
        # posts as watching topics so the agent can inspect context/vision first.
        self.live_top_creations_enabled = False
        if _env_flag("LIVE_TOP_CREATIONS_ENABLED", False):
            logger.warning(
                "LIVE_TOP_CREATIONS_ENABLED is ignored; reaction-qualified media "
                "is now handled by TopicEditor auto-shortlisting."
            )
        self.live_pass_interval_minutes = _env_int("LIVE_PASS_INTERVAL_MINUTES", 60)
        dry_run_lookback_hours = _env_int("LIVE_UPDATE_DEV_LOOKBACK_HOURS", 6)
        self.live_update_editor = live_update_editor or self._build_live_update_editor(
            db_handler,
            dry_run_lookback_hours=dry_run_lookback_hours,
        )
        self.live_top_creations = live_top_creations
        if start_loops:
            self.run_live_pass.change_interval(minutes=self.live_pass_interval_minutes)
            if self.live_updates_enabled or self.live_top_creations_enabled:
                self.run_live_pass.start()
            else:
                logger.warning(
                    "Live-update pass disabled; set LIVE_UPDATES_ENABLED=true "
                    "to enable in production."
                )

    def cog_unload(self):
        if self.run_live_pass.is_running():
            self.run_live_pass.cancel()

    @tasks.loop(minutes=60)
    async def run_live_pass(self):
        """Hourly TopicEditor pass plus trace flushing."""
        if self.live_updates_enabled:
            logger.info("Scheduled live-update editor pass starting...")
            try:
                result = await self.live_update_editor.run_once(trigger="scheduled")
                logger.info("Scheduled live-update editor pass finished: %s", result)
            except Exception as e:
                logger.error(f"Error during scheduled live-update editor pass: {e}", exc_info=True)
        # Flush the editor's deferred reasoning embed + trace file.
        try:
            flush_pending_reasoning = getattr(self.live_update_editor, "flush_pending_reasoning", None)
            if callable(flush_pending_reasoning):
                await flush_pending_reasoning()
        except Exception as e:
            logger.warning("flush_pending_reasoning failed: %s", e, exc_info=True)

    def _build_live_update_editor(self, db_handler, *, dry_run_lookback_hours: int):
        backend = os.getenv("LIVE_UPDATE_EDITOR_BACKEND", LIVE_UPDATE_EDITOR_BACKEND_TOPIC).strip().lower()
        environment = "dev" if self.dev_mode else "prod"
        if backend == LIVE_UPDATE_EDITOR_BACKEND_LEGACY:
            logger.warning("Using legacy live-update editor backend via LIVE_UPDATE_EDITOR_BACKEND=legacy")
            return LegacyLiveUpdateEditor(
                db_handler,
                bot=self.bot,
                logger_instance=getattr(self.bot, "logger", logger),
                dry_run_lookback_hours=dry_run_lookback_hours,
                environment=environment,
            )
        if backend not in {"", LIVE_UPDATE_EDITOR_BACKEND_TOPIC, "topic_editor"}:
            logger.warning(
                "Unknown LIVE_UPDATE_EDITOR_BACKEND=%r; defaulting to topic editor",
                backend,
            )
        topic_kwargs = {
            "bot": self.bot,
            "db_handler": db_handler,
            "environment": environment,
        }
        topic_llm_client = _build_topic_editor_llm_client()
        if topic_llm_client is not None:
            topic_kwargs["llm_client"] = topic_llm_client
        return TopicEditor(**topic_kwargs)

    @run_live_pass.before_loop
    async def before_run_live_pass(self):
        await self.bot.wait_until_ready()
        await self._wait_for_startup_live_pass()

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Handles the --summary-now live-editor flag on bot startup.
        
        If --archive-days is also specified, runs archive first, then the
        live editor so candidates use the most recent archived messages.
        """
        if not hasattr(self, '_ran_summary_now_check'):
            self._ran_summary_now_check = True
            run_now_flag = getattr(self.bot, 'summary_now', False)
            archive_days = getattr(self.bot, 'archive_days', None)
            
            logger.info(
                "SummarizerCog on_ready: summary_now=%s, archive_days=%s",
                run_now_flag,
                archive_days,
            )
            
            if run_now_flag:
                logger.info("Detected --summary-now flag on startup.")
                
                # If archive_days is specified, run archive first
                if archive_days:
                    await self._run_startup_archive(archive_days)
                
                # Run the live editor after any requested archive work.
                try:
                    result = await self.live_update_editor.run_once(trigger="startup_summary_now")
                    logger.info("Initial --summary-now live-update pass finished: %s", result)
                except Exception as e:
                    logger.error(f"Error during initial --summary-now live-update pass: {e}", exc_info=True)
                finally:
                    self._release_startup_live_gate()
            else:
                logger.debug("No --summary-now flag detected on startup.")
                self._release_startup_live_gate()

    @commands.command(name="summarynow")
    @commands.is_owner() # Or check for specific admin role/ID
    async def summary_now_command(self, ctx):
        """Manually triggers one live-update editor pass."""
        logger.info(f"Manual live-update pass triggered by {ctx.author.name}")
        await ctx.send("Starting live-update editor pass...")
        try:
            result = await self.live_update_editor.run_once(trigger="owner_summarynow")
            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            await ctx.send(f"Live-update editor pass complete: {status}.")
        except Exception as e:
            logger.error(f"Error during manual live-update pass: {e}", exc_info=True)
            await ctx.send(f"An error occurred during live-update editor pass: {e}")

    async def _run_startup_archive(self, archive_days: int) -> None:
        logger.info(f"Archive days specified ({archive_days}). Running archive process before live editor.")
        try:
            from src.common.archive_runner import ArchiveRunner

            dev_mode = getattr(self.bot, 'dev_mode', False)
            archive_runner = ArchiveRunner()
            sc = getattr(self.bot, 'server_config', None)
            guilds_to_archive = sc.get_guilds_to_archive() if sc and hasattr(sc, "get_guilds_to_archive") else []
            if not guilds_to_archive and sc and getattr(sc, "bndc_guild_id", None):
                guilds_to_archive = [{"guild_id": sc.bndc_guild_id}]

            success = True
            for guild_cfg in guilds_to_archive:
                guild_id = guild_cfg.get("guild_id") if isinstance(guild_cfg, dict) else guild_cfg
                guild_success = await archive_runner.run_archive(
                    archive_days,
                    dev_mode,
                    in_depth=True,
                    guild_id=guild_id,
                )
                success = success and guild_success

            if not guilds_to_archive:
                logger.warning("No writable guilds available for startup archive before live editor.")
            elif success:
                logger.info("Startup archive completed successfully. Starting live editor.")
            else:
                logger.warning("Startup archive failed for one or more guilds. Continuing with live editor.")
        except Exception as e:
            logger.error(f"Error during startup archive process: {e}", exc_info=True)
            logger.info("Continuing with live editor despite archive error.")

    async def _wait_for_startup_live_pass(self) -> None:
        startup_gate = getattr(self.bot, "summary_completed", None)
        if startup_gate is not None:
            await startup_gate.wait()

    def _release_startup_live_gate(self) -> None:
        startup_gate = getattr(self.bot, "summary_completed", None)
        if startup_gate is not None and not startup_gate.is_set():
            startup_gate.set()

async def setup(bot: commands.Bot):
    logger.info("Setting up SummarizerCog...")
    try:
        await bot.add_cog(SummarizerCog(bot))
        logger.info("SummarizerCog added to bot.")
    except Exception as e:
        logger.critical(f"Failed to add SummarizerCog to bot: {e}", exc_info=True)
        raise
