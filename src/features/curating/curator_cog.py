# src/features/curating/curator_cog.py

from discord.ext import commands
from src.features.curating.curator import ArtCurator

class CuratorCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        self.logger.info("Initializing CuratorCog...")
        self.art_curator = ArtCurator(logger=logger, dev_mode=dev_mode, bot_ref=bot)
        self.art_channel_id = self.art_curator.art_channel_id
        self.curator_ids = self.art_curator.curator_ids
        self.logger.info(f"CuratorCog resolved art channel: {self.art_channel_id}")
        self.logger.info(f"CuratorCog resolved curator IDs: {self.curator_ids}")

    @commands.Cog.listener()
    async def on_ready(self):
        """If you need extra logic once the bot is connected."""
        self.logger.info("CuratorCog is ready.")

    # If your curator had special tasks or commands, define them here.
    # For example:
    @commands.command(name='curate')
    async def manual_curate(self, ctx):
        """Forces a curation cycle manually, if appropriate."""
        if not await self.bot.is_owner(ctx.author):
            return
        if not hasattr(self.art_curator, 'manual_curate'):
            await ctx.send("Manual curation is not implemented for the current curator.")
            return
        self.logger.info("Running manual curation using ArtCurator...")
        await self.art_curator.manual_curate()
        self.logger.info("Finished ArtCurator's manual curation.")
        await ctx.send("Manual curation cycle completed.")

async def setup(bot: commands.Bot):
    """Sets up the CuratorCog."""
    # Ensure logger and dev_mode are available on the bot instance
    if not hasattr(bot, 'logger'):
        print("ERROR: Logger not found on bot object. Cannot load CuratorCog.")
        return
    if not hasattr(bot, 'dev_mode'):
         print("ERROR: dev_mode attribute not found on bot object. Cannot load CuratorCog.")
         return

    # Retrieve logger and dev_mode from the bot instance
    logger = bot.logger
    dev_mode = bot.dev_mode
    
    await bot.add_cog(CuratorCog(bot, logger, dev_mode=dev_mode))
    logger.info("CuratorCog added to bot.")
