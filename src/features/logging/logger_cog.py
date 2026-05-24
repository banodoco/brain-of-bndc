# src/features/logging/logger_cog.py

from discord.ext import commands
from src.common.db_handler import DatabaseHandler
from src.common.discord_utils import emoji_to_str
import discord
import json
import os


class LoggerCog(commands.Cog):
    def __init__(self, bot, logger, dev_mode=False):
        self.bot = bot
        self.logger = logger
        self.dev_mode = dev_mode
        if dev_mode:
            self.logger.info(f"Initializing LoggerCog in development mode")
            self.logger.debug(f"Bot intents enabled: {bot.intents}")
        # Use bot's shared db_handler if available, else create own
        self.db = getattr(bot, 'db_handler', None) or DatabaseHandler(dev_mode=dev_mode)
        if dev_mode:
            self.logger.debug("Database handler initialized")
        try:
            self.bot_user_id = int(os.getenv('BOT_USER_ID'))
            self.logger.debug(f"Retrieved BOT_USER_ID: {self.bot_user_id}")
        except Exception as e:
            self.logger.error(f"Error retrieving BOT_USER_ID: {e}")
            self.bot_user_id = None
        # Cache of thread IDs known to be summary threads
        self._summary_thread_ids: set[int] = set()

    @property
    def server_config(self):
        return getattr(self.db, 'server_config', None)

    def _is_feature_enabled(self, guild_id, channel_id, feature):
        """Check if a feature is enabled via server_config, defaulting to True."""
        sc = self.server_config
        if sc is None:
            return True
        return sc.is_feature_enabled(guild_id, channel_id, feature)

    async def cog_load(self):
        if self.dev_mode:
            self.logger.debug("Logger cog loaded")
        pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self.dev_mode:
            self.logger.info("LoggerCog is ready")
            self.logger.debug(f"Message events enabled: {self.bot.intents.message_content}")
            self.logger.debug(f"Reaction events enabled: {self.bot.intents.reactions}")
            self.logger.debug(f"Guild reaction events enabled: {self.bot.intents.guild_reactions}")

    async def _update_reaction(self, reaction, user, action: str):
        """Write reaction add/remove to discord_reactions table.

        A Postgres trigger (sync_message_reactors) automatically keeps
        discord_messages.reactors and reaction_count in sync.
        """
        if user.bot:
            return

        try:
            guild_id = getattr(reaction.message.guild, 'id', None)
            channel_id = reaction.message.channel.id

            # Feature guard: reactions_enabled
            if not self._is_feature_enabled(guild_id, channel_id, 'reactions'):
                return

            emoji_str = emoji_to_str(reaction.emoji)
            if action == 'add':
                self.db.add_reaction(reaction.message.id, user.id, emoji_str, guild_id=guild_id)
            elif action == 'remove':
                self.db.remove_reaction(reaction.message.id, user.id, emoji_str, guild_id=guild_id)

            # Append to reaction log (tracks every add/remove event)
            self.db.log_reaction_event(reaction.message.id, user.id, emoji_str, action, guild_id=guild_id)

            if self.dev_mode:
                self.logger.debug(f"[LoggerCog] Reaction {action}: msg={reaction.message.id} user={user.id} emoji={emoji_str}")

        except Exception as e:
            self.logger.error(f"[LoggerCog] Error in _update_reaction (action: {action}): {e}", exc_info=True)

    # --- Public methods for ReactorCog to call ---
    async def log_reaction_add(self, reaction, user):
        """Public method to be called by ReactorCog to log reaction additions."""
        await self._update_reaction(reaction, user, 'add')

    async def log_reaction_remove(self, reaction, user):
        """Public method to be called by ReactorCog to log reaction removals."""
        await self._update_reaction(reaction, user, 'remove')

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        """Track message edits by snapshotting the previous content into edit_history."""
        try:
            data = payload.data

            # Discord fires this event for embed resolution and other non-content
            # changes.  Skip if the payload doesn't include a content field at all.
            if 'content' not in data:
                return

            # Skip DMs (no guild_id)
            if not payload.guild_id:
                return

            # Feature guard: logging_enabled
            if not self._is_feature_enabled(payload.guild_id, payload.channel_id, 'logging'):
                return

            new_content = data.get('content')
            new_edited_at = data.get('edited_timestamp')  # ISO string or None

            updated = self.db.update_message_content(
                message_id=payload.message_id,
                new_content=new_content,
                new_edited_at=new_edited_at,
                guild_id=payload.guild_id,
            )

            if updated and self.dev_mode:
                self.logger.debug(
                    f"[LoggerCog] Recorded edit for message {payload.message_id} "
                    f"in channel {payload.channel_id}"
                )

            # If the message wasn't in the DB at all, the on_message handler
            # or the next hourly scrape will pick it up. We intentionally do NOT
            # store it here, because doing so advances latest_date and can cause
            # the hourly scrape to skip unedited messages in the same window.

        except Exception as e:
            self.logger.error(f"[LoggerCog] Error in on_raw_message_edit: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Soft-delete a message when it's deleted in Discord."""
        try:
            if not payload.guild_id:
                return

            # Feature guard: logging_enabled
            if not self._is_feature_enabled(payload.guild_id, payload.channel_id, 'logging'):
                return

            deleted = self.db.soft_delete_message(payload.message_id, guild_id=payload.guild_id)

            if deleted and self.dev_mode:
                self.logger.debug(
                    f"[LoggerCog] Soft-deleted message {payload.message_id} "
                    f"in channel {payload.channel_id}"
                )
        except Exception as e:
            self.logger.error(f"[LoggerCog] Error in on_raw_message_delete: {e}", exc_info=True)

    def _is_bot_summary_message(self, message) -> bool:
        """Return True for bot messages posted inside a summary thread or the summary channel."""
        if message.author.id != self.bot_user_id:
            return False
        channel = message.channel
        # Check cache first
        if channel.id in self._summary_thread_ids:
            return True
        # Bot messages in summary threads (thread name contains "Summary")
        if isinstance(channel, discord.Thread) and "Summary" in (channel.name or ""):
            self._summary_thread_ids.add(channel.id)
            return True
        # Bot messages that are summary headers or thread starters (posted to parent channel)
        content = message.content or ""
        if (content.startswith("### Channel summary for ")
                or content.startswith("# Daily Summary - ")
                or content.startswith("Summary thread for ")):
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message):
        """Store every new message to Supabase in real-time."""
        try:
            if not message.guild:
                return
            # Skip other bots, but store our own messages (so admin can find/delete them)
            if message.author.bot and message.author.id != self.bot_user_id:
                return
            # Skip our own summary messages to prevent feedback loops
            if message.author.id == self.bot_user_id and self._is_bot_summary_message(message):
                return

            # Feature guard: logging_enabled
            if not self._is_feature_enabled(message.guild.id, message.channel.id, 'logging'):
                return

            message_data = await self._prepare_message_data(message)
            reaction_rows = message_data.pop('_reaction_rows', [])
            await self.db.store_messages([message_data])
            self._store_message_author(message)
            if reaction_rows:
                self.db.upsert_reactions_batch(message.id, reaction_rows,
                                                guild_id=message_data.get('guild_id'))

        except Exception as e:
            self.logger.error(f"[LoggerCog] Error storing message {message.id}: {e}", exc_info=True)

    def _store_message_author(self, message: discord.Message) -> None:
        """Keep member/profile rows in step with real-time message logging."""
        try:
            if not hasattr(self.db, "create_or_update_member"):
                return
            author = message.author
            member = None
            if hasattr(message, "guild") and message.guild:
                member = message.guild.get_member(author.id)
            role_ids = (
                json.dumps([role.id for role in member.roles])
                if member and getattr(member, "roles", None)
                else None
            )
            guild_join_date = (
                member.joined_at.isoformat()
                if member and getattr(member, "joined_at", None)
                else None
            )
            display_name = (
                getattr(member, "display_name", None)
                or getattr(author, "display_name", None)
            )
            self.db.create_or_update_member(
                author.id,
                author.name,
                display_name,
                getattr(author, "global_name", None),
                str(author.avatar.url) if getattr(author, "avatar", None) else None,
                getattr(author, "discriminator", None),
                getattr(author, "bot", False),
                getattr(author, "system", False),
                getattr(author, "accent_color", None),
                str(author.banner.url) if getattr(author, "banner", None) else None,
                author.created_at.isoformat() if hasattr(author, "created_at") else None,
                guild_join_date,
                role_ids,
                guild_id=message.guild.id if getattr(message, "guild", None) else None,
            )
        except Exception as e:
            self.logger.debug(f"[LoggerCog] Error storing author profile for message {message.id}: {e}")

    async def _prepare_message_data(self, message: discord.Message) -> dict:
        """Convert a discord message into a format suitable for database storage."""
        try:
            # Calculate total reaction count
            reaction_count = sum(reaction.count for reaction in message.reactions) if message.reactions else 0

            # Get list of unique reactors and per-emoji reaction data
            reactors = []
            reaction_rows = []
            if message.reactions:
                for reaction in message.reactions:
                    emoji_str = emoji_to_str(reaction.emoji)
                    async for user in reaction.users():
                        if user.id != self.bot_user_id:
                            if user.id not in reactors:
                                reactors.append(user.id)
                            reaction_rows.append({
                                'message_id': message.id,
                                'user_id': user.id,
                                'emoji': emoji_str,
                            })

            # For threads (both regular text-channel threads and forum posts),
            # store channel_id = parent so summariser queries that count activity
            # per parent channel keep working, and store thread_id = thread so
            # jump-URL builders can route to the specific thread. Non-thread
            # channels have thread_id = None.
            actual_channel = message.channel
            thread_id = None
            if isinstance(message.channel, discord.Thread):
                actual_channel = message.channel.parent or message.channel
                thread_id = message.channel.id

            # Get guild display name (nickname) if available
            display_name = None
            global_name = message.author.global_name
            try:
                if hasattr(message, 'guild') and message.guild:
                    member = message.guild.get_member(message.author.id)
                    if member:
                        display_name = member.nick
            except Exception as e:
                self.logger.debug(f"Error getting display name for user {message.author.id}: {e}")

            # Get category ID if available
            category_id = None
            if hasattr(message.channel, 'category') and message.channel.category:
                category_id = message.channel.category.id

            return {
                'id': message.id,
                'message_id': message.id,
                'channel_id': actual_channel.id,
                'channel_name': actual_channel.name,
                'guild_id': message.guild.id if message.guild else None,
                'author_id': message.author.id,
                'author_name': message.author.name,
                'author_discriminator': message.author.discriminator,
                'author_avatar_url': str(message.author.avatar.url) if message.author.avatar else None,
                'content': message.content,
                'created_at': message.created_at.isoformat(),
                'attachments': [
                    {
                        'url': attachment.url,
                        'filename': attachment.filename
                    } for attachment in message.attachments
                ],
                'embeds': [embed.to_dict() for embed in message.embeds],
                'reaction_count': reaction_count,
                'reactors': reactors,
                'reference_id': message.reference.message_id if message.reference else None,
                'edited_at': message.edited_at.isoformat() if message.edited_at else None,
                'is_pinned': message.pinned,
                'thread_id': thread_id,
                'message_type': str(message.type),
                'flags': message.flags.value,
                'is_deleted': False,
                'display_name': display_name,
                'global_name': global_name,
                'category_id': category_id,
                '_reaction_rows': reaction_rows,
            }
        except Exception as e:
            self.logger.error(f"Error preparing message data: {e}")
            raise

async def setup(bot: commands.Bot):
    """Sets up the LoggerCog."""
    # Ensure logger and dev_mode are available on the bot instance
    if not hasattr(bot, 'logger'):
        print("ERROR: Logger not found on bot object. Cannot load LoggerCog.")
        return
    if not hasattr(bot, 'dev_mode'):
         print("ERROR: dev_mode attribute not found on bot object. Cannot load LoggerCog.")
         return

    # Retrieve logger and dev_mode from the bot instance
    logger = bot.logger
    dev_mode = bot.dev_mode

    await bot.add_cog(LoggerCog(bot, logger, dev_mode=dev_mode))
    logger.info("LoggerCog added to bot.")
