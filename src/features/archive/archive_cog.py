# src/features/archive/archive_cog.py

"""
ArchiveCog handles standalone archive operations.

Responsibilities:
- Handles --archive-days when used WITHOUT --summary-now
- Provides manual !archive command for admins
- Automatically shuts down bot after standalone archive completion

Note: When --archive-days is used WITH --summary-now, SummarizerCog owns the
archive-first startup pass before running the live-update editor.
"""

import logging
from discord.ext import commands

logger = logging.getLogger('DiscordBot')

class ArchiveCog(commands.Cog):
    """Handles standalone archive operations and manual archive commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Handles the --archive-days flag on bot startup when used standalone."""
        if not hasattr(self, '_ran_archive_check'):
            self._ran_archive_check = True
            archive_days = getattr(self.bot, 'archive_days', None)
            summary_now = getattr(self.bot, 'summary_now', False)
            
            # Only run archive if archive_days is specified and summary_now is NOT set
            # (when summary_now is set, SummarizerCog handles archive before the live editor)
            if archive_days and not summary_now:
                logger.info(f"Detected standalone --archive-days {archive_days} flag on startup.")
                try:
                    from src.common.archive_runner import ArchiveRunner
                    
                    dev_mode = getattr(self.bot, 'dev_mode', False)
                    archive_runner = ArchiveRunner()
                    sc = getattr(self.bot, 'server_config', None)
                    guilds_to_archive = sc.get_guilds_to_archive() if sc else []
                    success = True
                    for guild_cfg in guilds_to_archive:
                        guild_success = await archive_runner.run_archive(
                            archive_days, dev_mode, in_depth=True, guild_id=guild_cfg['guild_id']
                        )
                        success = success and guild_success

                    if not guilds_to_archive:
                        logger.warning("No writable guilds with archiving enabled in server_config")
                    elif success:
                        logger.info("Standalone archive process completed successfully")
                    else:
                        logger.error("Standalone archive process failed")
                    
                    logger.info("Standalone archive process finished. Shutting down bot.")
                    # Close the bot after archive is complete
                    await self.bot.close()
                    
                except Exception as e:
                    logger.error(f"Error during standalone archive process: {e}", exc_info=True)
                    await self.bot.close()

    @commands.command(name="archive")
    @commands.is_owner()
    async def archive_command(self, ctx, days: int):
        """Manually triggers the archive process for the specified number of days."""
        logger.info(f"Manual archive triggered by {ctx.author.name} for {days} days")
        await ctx.send(f"Starting archive process for {days} days...")
        
        try:
            from src.common.archive_runner import ArchiveRunner
            
            dev_mode = getattr(self.bot, 'dev_mode', False)
            archive_runner = ArchiveRunner()
            sc = getattr(self.bot, 'server_config', None)
            _guild_id = getattr(getattr(ctx, 'guild', None), 'id', None) or (sc.bndc_guild_id if sc else None)
            success = await archive_runner.run_archive(days, dev_mode, in_depth=True, guild_id=_guild_id)
            
            if success:
                await ctx.send("Archive process completed successfully.")
            else:
                await ctx.send("Archive process failed. Check logs for details.")
        except Exception as e:
            logger.error(f"Error during manual archive run: {e}", exc_info=True)
            await ctx.send(f"An error occurred during archive process: {e}")

async def setup(bot: commands.Bot):
    logger.info("Setting up ArchiveCog...")
    await bot.add_cog(ArchiveCog(bot))
    logger.info("ArchiveCog added to bot.")
