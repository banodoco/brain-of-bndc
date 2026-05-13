import os
import sys
import argparse
import logging
import asyncio
from datetime import datetime
import traceback

from dotenv import load_dotenv
from discord.ext import tasks

# Load environment variables BEFORE importing modules that might need them
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, '.env')
load_dotenv(dotenv_path=env_path, override=True)

from discord.ext import commands
import discord

from src.common.log_handler import LogHandler, setup_supabase_logging
from src.common.base_bot import BaseDiscordBot
from src.common.db_handler import DatabaseHandler
from src.common.openmuse_interactor import OpenMuseInteractor
from src.common.llm.claude_client import ClaudeClient
from src.common.health_server import HealthServer
from src.features.curating.curator_cog import CuratorCog
from src.features.summarising.summariser_cog import SummarizerCog
from src.features.summarising.summariser import ChannelSummarizer
from src.features.logging.logger_cog import LoggerCog
from src.features.sharing.sharing_cog import SharingCog
from src.features.sharing.providers.x_provider import XProvider
from src.features.sharing.providers.youtube_zapier_provider import YouTubeZapierProvider
from src.features.sharing.social_publish_service import SocialPublishService
from src.features.reacting.reactor import Reactor
from src.features.reacting.reactor_cog import ReactorCog
from src.features.archive.archive_cog import ArchiveCog
from src.features.health.health_check_cog import HealthCheckCog
from src.features.payments.payment_service import PaymentService
from src.features.payments.payment_ui_cog import PaymentUICog
from src.features.payments.payment_worker_cog import PaymentWorkerCog
from src.features.grants.solana_client import SolanaClient
from src.features.payments.solana_provider import SolanaProvider


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(dev_mode=False):
    """Setup shared logging configuration for all bots"""
    log_handler = LogHandler(
        logger_name='DiscordBot',
        prod_log_file='discord_bot.log',
        dev_log_file='discord_bot_dev.log'
    )
    logger = log_handler.setup_logging(dev_mode)
    if not logger:
        print("ERROR: Failed to create logger")
        sys.exit(1)
    
    # Log all INFO and above to Supabase (for full visibility)
    setup_supabase_logging(
        logger,
        min_level=logging.INFO,  # Capture all INFO, WARNING, ERROR, CRITICAL
        batch_size=25,  # Smaller batches for faster visibility
        flush_interval=10.0  # Flush every 10 seconds
    )
    
    return logger


def _bind_cap_breach_dm(bot, logger):
    async def _notify(payment):
        payment_worker_cog = bot.get_cog('PaymentWorkerCog')
        if not payment_worker_cog or not hasattr(payment_worker_cog, '_dm_admin_payment_failure'):
            logger.warning(
                "PaymentWorkerCog is not available to DM admin about manual-review payment %s",
                payment.get('payment_id'),
            )
            return
        try:
            await payment_worker_cog._dm_admin_payment_failure(payment)
        except Exception as e:
            logger.error("Failed to DM admin about manual-review payment %s: %s", payment.get('payment_id'), e)

    return _notify

async def run_archive_script(days, dev_mode=False, logger=None, in_depth=False,
                             guild_id=None, channels=None):
    """Run the archive_discord.py script with the specified number of days"""
    if logger is None:
        logger = logging.getLogger(__name__)

    from src.common.archive_runner import ArchiveRunner

    logger.info(f"Starting archive process for {days} days (in_depth={in_depth})"
                + (f" guild={guild_id}" if guild_id else ""))

    archive_runner = ArchiveRunner()
    success = await archive_runner.run_archive(days, dev_mode, in_depth=in_depth,
                                                guild_id=guild_id, channels=channels)

    if not success:
        raise RuntimeError("Archive script failed")

async def main_async(args):
    logger = setup_logging(dev_mode=args.dev)
    
    # Start health check server immediately
    health_server = HealthServer(port=8080)
    health_server.start()
    
    # Log deployment info for diagnostics
    deployment_id = os.getenv('RAILWAY_DEPLOYMENT_ID', 'local')
    service_id = os.getenv('RAILWAY_SERVICE_ID', 'local')
    replica_id = os.getenv('RAILWAY_REPLICA_ID', 'local')
    logger.info(f"🚀 Starting deployment {deployment_id[:8]}... (service: {service_id[:8]}..., replica: {replica_id})")
    
    logger.info("Starting unified bot initialization")

    try:
        if args.dev:
            token = os.getenv('DEV_DISCORD_BOT_TOKEN')
            if not token and _env_flag('ALLOW_PROD_DISCORD_TOKEN_IN_DEV', False):
                token = os.getenv('DISCORD_BOT_TOKEN')
                logger.warning("Using DISCORD_BOT_TOKEN in dev because ALLOW_PROD_DISCORD_TOKEN_IN_DEV=true")
            if not token:
                raise ValueError(
                    "DEV_DISCORD_BOT_TOKEN is required in dev mode. "
                    "Set ALLOW_PROD_DISCORD_TOKEN_IN_DEV=true only if you explicitly want to reuse the production bot token."
                )
            token_env_name = 'DEV_DISCORD_BOT_TOKEN'
        else:
            token = os.getenv('DISCORD_BOT_TOKEN')
            token_env_name = 'DISCORD_BOT_TOKEN'

        logger.debug(f"Token length: {len(token) if token else 0}")
        logger.debug(f"Token starts with: {token[:6]}..." if token else "No token found")
        logger.debug(f"Environment variable name used: {token_env_name}")

        if not token:
            raise ValueError("Discord bot token not found in environment variables")            

        # Create a single bot instance
        intents = discord.Intents.all()
        bot = BaseDiscordBot(
            command_prefix="!",
            logger=logger,
            dev_mode=args.dev,
            intents=intents
        )
        # Store the health server on the bot so cogs can access it
        bot.health_server = health_server
        
        # Store the command-line flags on the bot instance so cogs can access them
        bot.summary_now = args.summary_now
        bot.legacy_summary_now = args.legacy_summary_now
        bot.summary_with_archive = args.summary_with_archive
        bot.combine_only = args.combine_only
        bot.archive_days = args.archive_days
        bot.run_archive_script = run_archive_script  # Make the function available to cogs

        # Startup live-update gate: --summary-now may run archive ingestion before
        # one live-editor pass, so hourly archive fetch waits until that pass is
        # complete. The name is kept for compatibility with existing cogs/tests.
        bot.summary_completed = asyncio.Event()
        if not args.summary_now and not args.legacy_summary_now:
            bot.summary_completed.set()

        # ---- Initialize Core Components ----
        logger.info("Initializing core components (DB, Sharer, Reactor)...")

        # 1. Database Handler (also creates ServerConfig)
        bot.db_handler = DatabaseHandler(dev_mode=args.dev)
        bot.server_config = bot.db_handler.server_config
        logger.info("DatabaseHandler initialized and attached to bot.")

        # Shared social publish service for immediate callers and scheduled worker.
        x_provider = XProvider()
        youtube_provider = YouTubeZapierProvider()
        bot.social_publish_service = SocialPublishService(
            db_handler=bot.db_handler,
            providers={
                'twitter': x_provider,
                'x': x_provider,
                'youtube': youtube_provider,
            },
            logger_instance=logger,
        )
        logger.info("SocialPublishService initialized and attached to bot.")

        try:
            test_payment_amount = float(os.getenv('PAYMENT_TEST_AMOUNT_SOL', '0.002085'))
            per_payment_usd_cap = float(os.getenv('ADMIN_PAYOUT_PER_PAYMENT_USD_CAP', '10000'))
            daily_usd_cap = float(os.getenv('ADMIN_PAYOUT_DAILY_USD_CAP', '70000'))
            grants_provider = SolanaProvider(
                solana_client=SolanaClient(os.getenv('SOLANA_PRIVATE_KEY_GRANTS')),
            )
            payouts_provider = SolanaProvider(
                solana_client=SolanaClient(os.getenv('SOLANA_PRIVATE_KEY_PAYOUTS')),
            )
            bot.payment_service = PaymentService(
                db_handler=bot.db_handler,
                providers={
                    'solana_grants': grants_provider,
                    'solana_payouts': payouts_provider,
                },
                test_payment_amount=test_payment_amount,
                logger_instance=logger,
                per_payment_usd_cap=per_payment_usd_cap,
                daily_usd_cap=daily_usd_cap,
                capped_providers=('solana_payouts',),
                on_cap_breach=_bind_cap_breach_dm(bot, logger),
            )
            logger.info(
                "PaymentService initialized with fixed test payment amount of %.8f SOL.",
                test_payment_amount,
            )
            logger.info(
                "Admin payout caps enabled: per_payment_usd_cap=%.2f daily_usd_cap=%.2f capped_providers=%s",
                per_payment_usd_cap,
                daily_usd_cap,
                ('solana_payouts',),
            )
        except Exception as e:
            bot.payment_service = None
            logger.error(f"Failed to initialize payment subsystem (payments disabled): {e}")

        # 2. Claude Client
        claude_client_instance = ClaudeClient()
        bot.claude_client = claude_client_instance
        logger.info("ClaudeClient initialized.")

        # 3. Sharing Cog & Sharer Instance
        sharing_cog_instance = SharingCog(
            bot,
            bot.db_handler,
            social_publish_service=bot.social_publish_service,
        )
        await bot.add_cog(sharing_cog_instance)
        sharer_instance = sharing_cog_instance.sharer_instance
        if not sharer_instance:
            logger.error("Failed to retrieve Sharer instance from SharingCog!")
            return
        logger.info("SharingCog loaded.")

        # 4. Reactor Instance
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_KEY')

        openmuse_interactor_instance = OpenMuseInteractor(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            logger=logger
        )
        bot.openmuse_interactor_instance = openmuse_interactor_instance

        reactor_instance = Reactor(
            logger=logger,
            sharer_instance=sharer_instance,
            db_handler=bot.db_handler,
            openmuse_interactor=openmuse_interactor_instance,
            bot_instance=bot,
            llm_client=claude_client_instance,
            dev_mode=args.dev,
        )
        bot.reactor_instance = reactor_instance
        logger.info("Core components initialized.")

        # ---- Add Cogs ----

        channel_summarizer_instance = ChannelSummarizer(
            bot=bot,
            logger=logger,
            dev_mode=args.dev,
            command_prefix=bot.command_prefix,
            sharer_instance=sharer_instance
        )
        await bot.add_cog(SummarizerCog(bot, channel_summarizer_instance))
        logger.info("SummarizerCog loaded.")

        await bot.add_cog(CuratorCog(bot, logger, args.dev))
        await bot.add_cog(LoggerCog(bot, logger, args.dev))
        await bot.add_cog(ReactorCog(bot, logger, args.dev))
        await bot.add_cog(ArchiveCog(bot))
        await bot.add_cog(HealthCheckCog(bot))
        if bot.payment_service is not None:
            await bot.add_cog(PaymentWorkerCog(bot, bot.db_handler, payment_service=bot.payment_service))
            logger.info("PaymentWorkerCog loaded.")
            await bot.add_cog(PaymentUICog(bot, bot.db_handler, payment_service=bot.payment_service))
            logger.info("PaymentUICog loaded.")

        # Optional cogs — don't block startup if they fail
        try:
            from src.features.admin.admin_cog import AdminCog
            await bot.add_cog(AdminCog(bot))
        except Exception as e:
            logger.warning(f"Failed to load AdminCog (skipping): {e}")
        try:
            from src.features.admin_chat.admin_chat_cog import AdminChatCog
            await bot.add_cog(AdminChatCog(bot, bot.db_handler, sharer_instance))
        except Exception as e:
            logger.warning(f"Failed to load AdminChatCog (skipping): {e}")
        try:
            from src.features.gating.gating_cog import GatingCog
            await bot.add_cog(GatingCog(bot))
        except Exception as e:
            logger.warning(f"Failed to load GatingCog (skipping): {e}")
        try:
            from src.features.grants.grants_cog import GrantsCog
            await bot.add_cog(GrantsCog(bot))
        except Exception as e:
            logger.warning(f"Failed to load GrantsCog (skipping): {e}")
        try:
            from src.features.competition.competition_cog import CompetitionCog
            await bot.add_cog(CompetitionCog(bot))
            logger.info("CompetitionCog loaded.")
        except Exception as e:
            logger.error(f"Failed to load CompetitionCog: {e}", exc_info=True)
        try:
            from src.features.content.content_cog import ContentCog
            await bot.add_cog(ContentCog(bot))
        except Exception as e:
            logger.warning(f"Failed to load ContentCog (skipping): {e}")

        logger.info(f"All cogs loaded.")

        # ---- SETUP HOURLY MESSAGE FETCHING ----
        @tasks.loop(hours=1)
        async def hourly_message_fetch():
            """Fetch new messages every hour for all configured guilds."""
            try:
                logger.info("Starting hourly message fetch...")
                sc = getattr(bot, 'server_config', None)
                if sc:
                    sc.refresh()
                    for guild_cfg in sc.get_guilds_to_archive():
                        gid = guild_cfg['guild_id']
                        try:
                            logger.info(f"Archiving guild {gid} ({guild_cfg.get('guild_name', '?')})...")
                            await run_archive_script(days=1, dev_mode=args.dev, logger=logger,
                                                     guild_id=gid)
                        except Exception as e:
                            logger.error(f"Error archiving guild {gid}: {e}", exc_info=True)
                else:
                    logger.warning("Skipping hourly archive: server_config unavailable")

                logger.info("Hourly message fetch completed successfully")
            except Exception as e:
                logger.error(f"Error in hourly message fetch: {e}", exc_info=True)

        @hourly_message_fetch.before_loop
        async def before_hourly_fetch():
            """Wait for readiness and any startup live-editor pass before hourly fetch."""
            await bot.wait_until_ready()
            logger.info("Waiting for startup live-update gate before starting hourly fetch...")
            await bot.summary_completed.wait()
            logger.info("Ready to start hourly message fetch loop")

        # Start the hourly fetch task
        hourly_message_fetch.start()
        logger.info("Hourly message fetch task scheduled")

        # Use a Cog listener instead of @bot.event to avoid overriding other on_ready handlers
        class ReadinessListener(commands.Cog):
            @commands.Cog.listener()
            async def on_ready(self_cog):
                health_server.mark_ready()
                health_server.update_heartbeat()
                logger.info(f"✅ Bot is ready! Logged in as {bot.user} (Deployment: {deployment_id[:8]}...)")

        await bot.add_cog(ReadinessListener(bot))
        
        # ---- RUN ----
        logger.info("Starting bot...")
        await bot.start(token)

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Error running unified bot: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        print(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Unified Discord Bot')
    parser.add_argument('--summary-now', action='store_true', help='Run one live-update editor pass immediately on startup')
    parser.add_argument('--legacy-summary-now', action='store_true',
                      help='Explicit legacy daily-summary/backfill startup run')
    parser.add_argument(
        '--dev',
        action='store_true',
        default=_env_flag('BOT_DEV_MODE', False),
        help='Run in development mode; can also be enabled with BOT_DEV_MODE=true',
    )
    parser.add_argument('--archive-days', type=int, help='Number of days to archive (can be used standalone or with --summary-now)')
    parser.add_argument('--summary-with-archive', action='store_true', help='Archive past 24 hours FIRST, then run one live-update editor pass')
    parser.add_argument('--combine-only', action='store_true',
                      help='Legacy daily-summary backfill: load existing channel summaries from DB and re-run only combine+post')
    parser.add_argument('--clear-today-summaries', action='store_true',
                      help='Legacy daily-summary backfill: delete today\'s summaries from Supabase before running')
    args = parser.parse_args()
    
    # Handle --clear-today-summaries flag
    if args.clear_today_summaries:
        print("🗑️  Clearing today's summaries from Supabase...")
        try:
            from supabase import create_client
            from dotenv import load_dotenv
            load_dotenv()
            url = os.getenv('SUPABASE_URL')
            key = os.getenv('SUPABASE_SERVICE_KEY')
            if url and key:
                supabase = create_client(url, key)
                today = datetime.now().strftime('%Y-%m-%d')
                result = supabase.table('daily_summaries').delete().eq('date', today).execute()
                deleted = len(result.data) if result.data else 0
                print(f"✅ Deleted {deleted} summary records for {today}")
            else:
                print("⚠️  SUPABASE_URL or SUPABASE_SERVICE_KEY not set, skipping clear")
        except Exception as e:
            print(f"⚠️  Error clearing summaries: {e}")
    
    # Check for date-based environment variable triggers. These env names are
    # legacy deployment controls; they now trigger the live-update editor.
    # Priority: SUMMARY_WITH_ARCHIVE_DATE > JUST_SUMMARY_DATE
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # Check SUMMARY_WITH_ARCHIVE_DATE first (archive + live update)
    env_archive_date = os.getenv('SUMMARY_WITH_ARCHIVE_DATE')
    if env_archive_date:
        env_archive_date = env_archive_date.strip()
        try:
            parsed_date = datetime.strptime(env_archive_date, '%Y-%m-%d')
            parsed_date_str = parsed_date.strftime('%Y-%m-%d')
            
            if parsed_date_str == today_str:
                print(f"✓ SUMMARY_WITH_ARCHIVE_DATE={env_archive_date} matches today's date ({today_str}). Triggering archive + live update...")
                args.summary_with_archive = True
            else:
                print(f"ℹ SUMMARY_WITH_ARCHIVE_DATE={env_archive_date} set but doesn't match today ({today_str}). Skipping auto-trigger.")
        except ValueError:
            print(f"⚠ WARNING: SUMMARY_WITH_ARCHIVE_DATE='{env_archive_date}' is not a valid date format (expected YYYY-MM-DD). Ignoring.")
    
    # Check JUST_SUMMARY_DATE only if SUMMARY_WITH_ARCHIVE_DATE didn't trigger
    elif os.getenv('JUST_SUMMARY_DATE'):
        env_summary_date = os.getenv('JUST_SUMMARY_DATE').strip()
        try:
            parsed_date = datetime.strptime(env_summary_date, '%Y-%m-%d')
            parsed_date_str = parsed_date.strftime('%Y-%m-%d')
            
            if parsed_date_str == today_str:
                print(f"✓ JUST_SUMMARY_DATE={env_summary_date} matches today's date ({today_str}). Triggering live update only...")
                args.summary_now = True
            else:
                print(f"ℹ JUST_SUMMARY_DATE={env_summary_date} set but doesn't match today ({today_str}). Skipping auto-trigger.")
        except ValueError:
            print(f"⚠ WARNING: JUST_SUMMARY_DATE='{env_summary_date}' is not a valid date format (expected YYYY-MM-DD). Ignoring.")

    # Handle the combined flags
    if args.summary_with_archive:
        args.summary_now = True
        args.archive_days = 1

    if args.combine_only:
        args.legacy_summary_now = True

    # No validation needed - --archive-days can be used standalone or with --summary-now

    # Environment variables already loaded at module import time
    
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("Received keyboard interrupt, shutting down...")
    except Exception as e:
        print(f"Error running unified bot: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
