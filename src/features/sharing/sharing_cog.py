import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from discord.ext import commands, tasks

from src.common.db_handler import DatabaseHandler

from .providers.x_provider import XProvider
from .providers.youtube_zapier_provider import YouTubeZapierProvider
from .sharer import Sharer
from .social_publish_service import SocialPublishService
from .live_update_social import LiveUpdateSocialService

logger = logging.getLogger('DiscordBot')


class SharingCog(commands.Cog):
    """Sharing entrypoints plus the scheduled social publication worker."""

    def __init__(
        self,
        bot: commands.Bot,
        db_handler: DatabaseHandler,
        social_publish_service: Optional[SocialPublishService] = None,
    ):
        self.bot = bot
        self.db_handler = db_handler
        self.social_publish_service = (
            social_publish_service
            or getattr(bot, 'social_publish_service', None)
            or self._build_social_publish_service()
        )
        self.bot.social_publish_service = self.social_publish_service
        self.claim_batch_size = max(int(os.getenv('SOCIAL_PUBLISH_CLAIM_LIMIT', '10')), 1)
        self.max_attempts = max(int(os.getenv('SOCIAL_PUBLISH_MAX_ATTEMPTS', '3')), 1)
        self.retry_delay_seconds = max(int(os.getenv('SOCIAL_PUBLISH_RETRY_SECONDS', '300')), 1)

        self.sharer_instance = Sharer(
            bot=self.bot,
            db_handler=self.db_handler,
            logger_instance=logger,
        )

        self.live_update_social_service = LiveUpdateSocialService(
            db_handler=self.db_handler,
            bot=self.bot,
            logger_instance=logger,
            social_publish_service=self.social_publish_service,
        )
        self.bot.live_update_social_service = self.live_update_social_service
        logger.info("SharingCog initialized.")

    def _build_social_publish_service(self) -> SocialPublishService:
        x_provider = XProvider()
        youtube_provider = YouTubeZapierProvider()
        return SocialPublishService(
            db_handler=self.db_handler,
            providers={
                'twitter': x_provider,
                'x': x_provider,
                'youtube': youtube_provider,
            },
            logger_instance=logger,
        )

    async def cog_load(self):
        if not self.scheduled_publication_worker.is_running():
            self.scheduled_publication_worker.start()
            logger.info("[SharingCog] Scheduled publication worker started.")

    def cog_unload(self):
        if self.scheduled_publication_worker.is_running():
            self.scheduled_publication_worker.cancel()
            logger.info("[SharingCog] Scheduled publication worker stopped.")

    @tasks.loop(seconds=30)
    async def scheduled_publication_worker(self):
        """Claim and execute due queued social publications."""
        claimed_publications = self.db_handler.claim_due_social_publications(
            limit=self.claim_batch_size,
        )
        if not claimed_publications:
            return

        logger.info(
            f"[SharingCog] Claimed {len(claimed_publications)} due social publication(s)."
        )
        for publication in claimed_publications:
            await self._process_claimed_publication(publication)

    @scheduled_publication_worker.before_loop
    async def _before_scheduled_publication_worker(self):
        await self.bot.wait_until_ready()

    async def _process_claimed_publication(self, publication: dict):
        publication_id = publication.get('publication_id')
        platform = publication.get('platform')
        action = publication.get('action')
        attempt_count = int(publication.get('attempt_count') or 0)

        if not publication_id:
            logger.warning("[SharingCog] Claimed publication without publication_id; skipping.")
            return

        try:
            result = await self.social_publish_service.execute_publication(publication_id)
            if result.success:
                logger.info(
                    f"[SharingCog] Publication {publication_id} succeeded "
                    f"(platform={platform}, action={action})."
                )
                return

            error_message = result.error or "Unknown publish failure"
            if self._should_retry_publication(error_message, attempt_count):
                retry_after = datetime.now(timezone.utc) + timedelta(
                    seconds=self.retry_delay_seconds * max(attempt_count, 1)
                )
                if self._requeue_publication(publication, error_message, retry_after):
                    logger.warning(
                        f"[SharingCog] Requeued publication {publication_id} after transient failure "
                        f"(platform={platform}, action={action}, attempt={attempt_count}, "
                        f"retry_after={retry_after.isoformat()}, error={error_message})."
                    )
                    return

            logger.error(
                f"[SharingCog] Publication {publication_id} failed "
                f"(platform={platform}, action={action}, attempt={attempt_count}, error={error_message})."
            )
        except Exception as e:
            logger.error(
                f"[SharingCog] Unexpected scheduler error for publication {publication_id} "
                f"(platform={platform}, action={action}): {e}",
                exc_info=True,
            )

    def _should_retry_publication(self, error_message: str, attempt_count: int) -> bool:
        """Return True when a publish failure looks transient and retry budget remains."""
        if attempt_count >= self.max_attempts:
            return False

        normalized_error = (error_message or '').lower()
        transient_markers = (
            'provider publish failed',
            'timeout',
            'tempor',
            'try again',
            'rate limit',
            '429',
            '500',
            '502',
            '503',
            '504',
            'connection reset',
            'service unavailable',
        )
        return any(marker in normalized_error for marker in transient_markers)

    def _requeue_publication(
        self,
        publication: dict,
        error_message: str,
        retry_after: datetime,
    ) -> bool:
        """Move a failed processing row back to queued for another attempt."""
        if not self.db_handler.supabase:
            return False

        publication_id = publication.get('publication_id')
        guild_id = publication.get('guild_id')
        if not publication_id or guild_id is None:
            return False

        try:
            self.db_handler.supabase.table('social_publications').update({
                'status': 'queued',
                'retry_after': retry_after.isoformat(),
                'last_error': error_message,
                'completed_at': None,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('publication_id', publication_id).eq('guild_id', guild_id).execute()
            return True
        except Exception as e:
            logger.error(
                f"[SharingCog] Failed to requeue publication {publication_id}: {e}",
                exc_info=True,
            )
            return False


async def setup(bot: commands.Bot):
    if not hasattr(bot, 'db_handler'):
        logger.error("Database handler not found on bot object. Cannot load SharingCog.")
        return

    try:
        logger.info("About to create SharingCog instance...")
        cog_instance = SharingCog(
            bot,
            bot.db_handler,
            social_publish_service=getattr(bot, 'social_publish_service', None),
        )
        logger.info("SharingCog instance created, adding to bot...")
        await bot.add_cog(cog_instance)
        logger.info("SharingCog added to bot.")
    except Exception as e:
        logger.error(f"Error in SharingCog setup: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
