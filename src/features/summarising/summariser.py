# Standard library imports
import asyncio

import io
import json
import logging
import os
import re

import traceback
from datetime import datetime, timedelta
from typing import List, Tuple, Set, Dict, Optional, Any, Union
import sqlite3


# Third-party imports
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Local imports
from src.common.db_handler import DatabaseHandler
from src.common.error_handler import handle_errors
from src.common.rate_limiter import RateLimiter
from src.common.log_handler import LogHandler
from src.common import discord_utils
from src.common.urls import message_jump_url, resolve_thread_ids

# Import the new summarizer that handles queries/Claude calls
from src.features.summarising.subfeatures.news_summary import NewsSummarizer
from src.features.summarising.subfeatures.top_generations import TopGenerations
from src.features.summarising.subfeatures.top_art_sharing import TopArtSharing

# Content moderation
from src.common.content_moderator import filter_summary_media

# Speaker welcome blurbs
from src.common.speaker_welcome import get_recommendable_channels, build_speaker_blurb

# --- Import Sharer ---

# Optional imports for media processing
MEDIA_PROCESSING_AVAILABLE = False
POSTER_EXTRACTION_AVAILABLE = False

try:
    from PIL import Image
    MEDIA_PROCESSING_AVAILABLE = True  # PIL is enough for basic image ops
except Exception as e:
    import logging
    logging.getLogger('DiscordBot').warning(f"PIL import failed: {type(e).__name__}: {e}")

# imageio for poster extraction (bundles own ffmpeg - more reliable in containers)
try:
    import imageio.v3 as iio
    import imageio_ffmpeg
    POSTER_EXTRACTION_AVAILABLE = True
except Exception as e:
    import logging
    logging.getLogger('DiscordBot').warning(f"imageio import failed (poster extraction disabled): {type(e).__name__}: {e}")

# moviepy for video concatenation (optional, used by create_media_content)
try:
    import moviepy.editor as mp
except Exception as e:
    mp = None
    import logging
    logging.getLogger('DiscordBot').info(f"moviepy import failed (video concatenation disabled): {type(e).__name__}: {e}")

################################################################################
# You may already have a scheduling function somewhere, but here is a simple stub:
################################################################################
async def schedule_daily_summary(bot):
    """
    Legacy unused helper for daily scheduled runs.

    The production bot uses SummarizerCog today. The live-update editor
    replacement must not call this helper or bot.generate_summary(); keep this
    only as explicit legacy/backfill behavior until it is removed.
    """
    first_run = True
    while not bot._shutdown_flag:
        now_utc = datetime.utcnow()
        # Suppose we run at 10:00 UTC daily
        run_time = now_utc.replace(hour=10, minute=0, second=0, microsecond=0)
        if run_time < now_utc:
            run_time += timedelta(days=1)
        sleep_duration = (run_time - now_utc).total_seconds()
        await asyncio.sleep(sleep_duration)

        if bot._shutdown_flag:
            break

        if first_run:
            if bot.summary_now:
                bot.logger.info("'--summary-now' flag detected. Running initial summary now.")
            else:
                bot.logger.info("Skipping initial summary run because '--summary-now' flag was not provided.")
                first_run = False
                continue
            first_run = False

        try:
            await bot.generate_summary()
        except Exception as e:
            bot.logger.error(f"Scheduled summary run failed: {e}")

        # Sleep 24h until next scheduled run:
        await asyncio.sleep(86400)

################################################################################

class ChannelSummarizerError(Exception):
    """Base exception class for ChannelSummarizer"""
    pass

class APIError(ChannelSummarizerError):
    """Raised when API calls fail"""
    pass

class DiscordError(ChannelSummarizerError):
    """Raised when Discord operations fail"""
    pass

class SummaryError(ChannelSummarizerError):
    """Raised when summary generation fails"""
    pass

class Attachment:
    def __init__(self, filename: str, data: bytes, content_type: str, reaction_count: int, username: str, content: str = ""):
        self.filename = filename
        self.data = data
        self.content_type = content_type
        self.reaction_count = reaction_count
        self.username = username
        self.content = content

class AttachmentHandler:
    def __init__(self, logger: logging.Logger, max_size: int = 25 * 1024 * 1024):
        self.max_size = max_size
        self.attachment_cache: Dict[str, Dict[str, Any]] = {}
        self.logger = logger
        
    def clear_cache(self):
        """Clear the attachment cache"""
        self.attachment_cache.clear()
        
    async def process_attachment(self, attachment: discord.Attachment, message: discord.Message, session: aiohttp.ClientSession) -> Optional[Attachment]:
        """Process a single attachment with size and type validation."""
        try:
            cache_key = f"{message.channel.id}:{message.id}"

            async with session.get(attachment.url, timeout=300) as response:
                if response.status != 200:
                    raise APIError(f"Failed to download attachment: HTTP {response.status}")

                file_data = await response.read()
                if len(file_data) > self.max_size:
                    self.logger.warning(f"Skipping large file {attachment.filename} ({len(file_data)/1024/1024:.2f}MB)")
                    return None

                total_reactions = sum(reaction.count for reaction in message.reactions) if message.reactions else 0
                
                # Get guild display name (nickname) if available, otherwise use display name
                author_name = message.author.display_name
                if hasattr(message.author, 'guild'):
                    member = message.guild.get_member(message.author.id)
                    if member:
                        author_name = member.nick or member.display_name

                processed_attachment = Attachment(
                    filename=attachment.filename,
                    data=file_data,
                    content_type=attachment.content_type,
                    reaction_count=total_reactions,
                    username=author_name,  # Use the determined name
                    content=message.content
                )

                # Ensure the cache key structure is consistent
                if cache_key not in self.attachment_cache:
                    self.attachment_cache[cache_key] = {
                        'attachments': [],
                        'reaction_count': total_reactions,
                        'username': author_name,
                        'channel_id': str(message.channel.id)
                    }
                self.attachment_cache[cache_key]['attachments'].append(processed_attachment)

                return processed_attachment

        except Exception as e:
            self.logger.error(f"Failed to process attachment {attachment.filename}: {e}")
            self.logger.debug(traceback.format_exc())
            return None

    async def prepare_files(self, message_ids: List[str], channel_id: str) -> List[Tuple[discord.File, int, str, str]]:
        """Prepare Discord files from cached attachments."""
        files = []
        for message_id in message_ids:
            # Use composite key to look up attachments
            cache_key = f"{channel_id}:{message_id}"
            if cache_key in self.attachment_cache:
                for attachment in self.attachment_cache[cache_key]['attachments']:
                    try:
                        file = discord.File(
                            io.BytesIO(attachment.data),
                            filename=attachment.filename,
                            description=f"From message ID: {message_id} (🔥 {attachment.reaction_count} reactions)"
                        )
                        files.append((
                            file,
                            attachment.reaction_count,
                            message_id,
                            attachment.username
                        ))
                    except Exception as e:
                        self.logger.error(f"Failed to prepare file {attachment.filename}: {e}")
                        continue

        return sorted(files, key=lambda x: x[1], reverse=True)[:10]

    def get_all_files_sorted(self) -> List[Attachment]:
        """
        Retrieve all attachments sorted by reaction count in descending order.
        """
        all_attachments = []
        for channel_data in self.attachment_cache.values():
            all_attachments.extend(channel_data['attachments'])
        
        # Sort attachments by reaction_count in descending order
        sorted_attachments = sorted(all_attachments, key=lambda x: x.reaction_count, reverse=True)
        return sorted_attachments

class MessageFormatter:
    @staticmethod
    def format_usernames(usernames: List[str]) -> str:
        """Format a list of usernames with proper grammar and bold formatting."""
        unique_usernames = list(dict.fromkeys(usernames))
        if not unique_usernames:
            return ""
        
        formatted_usernames = []
        for username in unique_usernames:
            if not username.startswith('**'):
                username = f"**{username}**"
            formatted_usernames.append(username)
        
        if len(formatted_usernames) == 1:
            return formatted_usernames[0]
        
        return f"{', '.join(formatted_usernames[:-1])} and {formatted_usernames[-1]}"

    @staticmethod
    def chunk_content(content: str, max_length: int = 1900) -> List[Tuple[str, Set[str]]]:
        """Split content into chunks while preserving message links."""
        chunks = []
        current_chunk = ""
        current_chunk_links = set()

        for line in content.split('\n'):
            message_links = set(re.findall(r'https://discord\.com/channels/\d+/\d+/(\d+)', line))
            
            # Start new chunk if we hit an emoji or length limit
            if (any(line.startswith(emoji) for emoji in ['🎥', '💻', '🎬', '🤖', '📱', '🔧', '🎨', '📊']) and 
                current_chunk):
                if current_chunk:
                    chunks.append((current_chunk, current_chunk_links))
                current_chunk = ""
                current_chunk_links = set()
                current_chunk += '\n---\n\n'

            if len(current_chunk) + len(line) + 2 <= max_length:
                current_chunk += line + '\n'
                current_chunk_links.update(message_links)
            else:
                if current_chunk:
                    chunks.append((current_chunk, current_chunk_links))
                current_chunk = line + '\n'
                current_chunk_links = set(message_links)

        if current_chunk:
            chunks.append((current_chunk, current_chunk_links))

        return chunks

    def chunk_long_content(self, content: str, max_length: int = 1900) -> List[str]:
        """Split content into chunks that respect Discord's length limits."""
        chunks = []
        current_chunk = ""
        
        lines = content.split('\n')
        
        for line in lines:
            if len(current_chunk) + len(line) + 1 <= max_length:
                current_chunk += line + '\n'
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = line + '\n'
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks

class ChannelSummarizer:
    # Define constants if they are not already defined
    RATE_LIMIT_CALLS = 10
    RATE_LIMIT_PERIOD = 60  # seconds
    MAX_RETRIES = 3
    INITIAL_RETRY_DELAY = 5  # seconds
    MAX_RETRY_WAIT = 300 # 5 minutes
    MAX_MESSAGE_LENGTH = 1990
    DEFAULT_TIME_DELTA_HOURS = 24
    MAX_ATTACHMENTS_PER_MESSAGE = 10
    MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024 # 25 MB

    # MODIFIED __init__ to accept bot
    def __init__(self, bot: commands.Bot, logger=None, dev_mode=False, command_prefix="!", sharer_instance=None): 
        self.bot = bot # Store the passed bot instance
        
        # Initialize logger (ensure setup_logger exists or handle here)
        self.logger = logger or logging.getLogger('DiscordBot') # Use provided or default
        if logger is None:
             # Minimal logger setup if none provided - adjust as needed
             logging.basicConfig(level=logging.INFO) 
             self.logger.warning("No logger provided to ChannelSummarizer, using basic config.")

        # Set dev_mode - assuming property exists
        self.dev_mode = dev_mode 

        self.command_prefix = command_prefix
        
        # Store Sharer Instance
        if sharer_instance is None:
            self.logger.critical("Sharer instance was not provided to ChannelSummarizer.")
            # Decide whether to raise error or just log
            # raise ValueError("Sharer instance is required") 
        self.sharer = sharer_instance # Use self.sharer consistently

        # Initialize DB Handler
        self.db_handler = DatabaseHandler(dev_mode=self.dev_mode)
        self.logger.info(f"DB Handler initialized in ChannelSummarizer. Dev mode: {self.dev_mode}")

        # Initialize sub-features correctly (pass dependencies, NOT bot=self)
        try:
            self.news_summarizer = NewsSummarizer(
                self.logger,
                self.dev_mode,
                server_config=getattr(self.db_handler, 'server_config', None),
            )
            self.logger.info("NewsSummarizer initialized.")

            self.top_generations = TopGenerations(self)
            self.logger.info("TopGenerations initialized.")

            self.top_art_sharer = TopArtSharing(self, self.sharer)
            self.logger.info("TopArtSharing initialized.")

            self.logger.info("Sub-feature handlers initialized successfully.")

        except Exception as e:
            self.logger.critical(f"Failed to initialize sub-feature handlers: {e}", exc_info=True)
            raise # Re-raise to prevent cog loading if any sub-feature fails

        # Initialize Rate Limiter (takes no arguments)
        self.rate_limiter = RateLimiter()
        self.logger.info("RateLimiter initialized.")

        # Initialize Attachment Handler
        self.attachment_handler = AttachmentHandler(self.logger, max_size=self.MAX_ATTACHMENT_SIZE)
        self.logger.info("Attachment Handler initialized.")

        # Load config AFTER other initializations if it depends on them
        self.load_config() 

        # Other attributes
        self.processed_today = set() 
        self._shutdown_flag = False 
        # Initialize the summary lock
        self.summary_lock = asyncio.Lock()
        self.first_message = None

        self.logger.info(f"ChannelSummarizer initialized successfully.")

    # Keep setup_logger if used by __init__
    def setup_logger(self, dev_mode):
        # ... (ensure this setup logic is appropriate or remove if logger is always passed)
        log_handler = LogHandler(logger_name='ChannelSummarizer', 
                                 prod_log_file='channel_summarizer.log', 
                                 dev_log_file='channel_summarizer_dev.log')
        self.logger = log_handler.setup_logging(dev_mode)
        if self.logger:
            self.logger.info(f"ChannelSummarizer logger setup ({'DEV' if dev_mode else 'PROD'}).")
        return self.logger # Return logger instance

    # Keep dev_mode property and setter
    @property
    def dev_mode(self):
        return self._dev_mode

    @dev_mode.setter
    def dev_mode(self, value):
        if not hasattr(self, '_dev_mode') or self._dev_mode != value:
            self._dev_mode = value
            # Reload config or re-init logger if necessary when mode changes
            # self.setup_logger(value) # Example: Reconfigure logger
            # self.load_config() # Example: Reload config
            self.logger.info(f"ChannelSummarizer dev_mode set to: {value}")

    def load_config(self):
        # ... (load_config implementation as before) ...
        self.logger.debug("Loading configuration...")
        # Simplified logging in load_config for clarity
        try:
             load_dotenv(override=True) # Ensure .env is loaded
             env_prefix = "DEV_" if self.dev_mode else ""

             # server_config is authoritative for guild settings
             sc = getattr(getattr(self.bot, 'db_handler', None), 'server_config', None) if self.bot else None

             self.guild_id = None
             if sc:
                 guilds = [
                     s for s in sc.get_enabled_servers(require_write=True)
                     if s.get('default_summarising')
                 ]
                 if guilds:
                     self.guild_id = guilds[0]['guild_id']
             server = sc.get_server(self.guild_id) if sc and self.guild_id else None

             def _field(name, cast=int):
                 return sc.get_server_field(self.guild_id, name, cast=cast) if sc and self.guild_id else None

             self.summary_channel_id = _field('summary_channel_id')
             server_monitor_all = bool(server.get('monitor_all_channels')) if server and server.get('monitor_all_channels') is not None else False
             server_monitored_ids = server.get('monitored_channel_ids') if server else None
             self.monitor_all_channels = False
             if server_monitor_all:
                 self.monitor_all_channels = True
                 self.channels_to_monitor = []
             elif isinstance(server_monitored_ids, list):
                 self.channels_to_monitor = [int(c) for c in server_monitored_ids]
             else:
                 self.channels_to_monitor = []
             self.art_channel_id = _field('art_channel_id') or int(os.getenv(f'{env_prefix}ART_CHANNEL_ID'))
             self.top_gens_channel_id = _field('top_gens_channel_id') or self.summary_channel_id
             self.welcome_channel_id = _field('welcome_channel_id')

             monitor_desc = 'ALL' if self.monitor_all_channels else self.channels_to_monitor
             self.logger.info(f"Loaded {'DEV' if self.dev_mode else 'PROD'} config: Guild={self.guild_id}, Summary={self.summary_channel_id}, TopGens={self.top_gens_channel_id}, Monitor={monitor_desc}, Art={self.art_channel_id}")

        except (ValueError, TypeError) as e:
             self.logger.error(f"Invalid ID format in environment variables: {e}", exc_info=True)
             raise ConfigurationError(f"Invalid ID format: {e}")
        except ConfigurationError as e:
            self.logger.error(f"Configuration Error: {e}", exc_info=True)
            raise
        except Exception as e:
             self.logger.error(f"Unexpected error loading configuration: {e}", exc_info=True)
             raise ConfigurationError(f"Unexpected error loading config: {e}")

    async def _get_channel_with_retry(self, channel_id: int) -> Optional[Union[discord.TextChannel, discord.Thread, discord.ForumChannel]]:
        if not self.bot or not self.bot.is_ready(): # Also check if bot is ready
             self.logger.error("Bot instance not available or not ready in _get_channel_with_retry.")
             return None
        
        self.logger.debug(f"Attempting to get channel {channel_id}. Bot instance: {self.bot}")
        # Use the stored self.bot instance
        channel = self.bot.get_channel(channel_id)
        if channel:
            self.logger.debug(f"Channel {channel_id} found in cache: {channel}")
            return channel
        
        # Retry logic using self.bot.fetch_channel
        self.logger.warning(f"Channel {channel_id} not in cache. Will attempt to fetch via API.")
        delay = self.INITIAL_RETRY_DELAY
        for attempt in range(self.MAX_RETRIES):
             self.logger.warning(f"Attempt {attempt+1}/{self.MAX_RETRIES} to fetch channel {channel_id} via API after {delay:.1f}s delay...")
             await asyncio.sleep(delay)
             try:
                  # Use fetch_channel which makes an API call if not in cache
                  self.logger.debug(f"Calling self.bot.fetch_channel({channel_id})")
                  channel = await self.bot.fetch_channel(channel_id)
                  if channel:
                       self.logger.info(f"Successfully fetched channel {channel_id} via API on attempt {attempt+1}. Channel: {channel}")
                       return channel
                  else:
                       # This case should ideally not happen if fetch_channel doesn't raise an error
                       self.logger.warning(f"fetch_channel({channel_id}) returned None on attempt {attempt+1} without raising error.")
             except discord.NotFound:
                  self.logger.error(f"discord.NotFound error fetching channel {channel_id} on attempt {attempt+1}. The channel likely does not exist or the bot can't see it.")
                  # Don't retry if definitively not found
                  return None 
             except discord.Forbidden:
                  self.logger.error(f"discord.Forbidden error fetching channel {channel_id} on attempt {attempt+1}. Check bot permissions for this channel.")
                  # Don't retry if forbidden
                  return None 
             except discord.HTTPException as e:
                   self.logger.error(f"discord.HTTPException fetching channel {channel_id} on retry {attempt + 1}: Status={e.status}, Code={e.code}, Text={e.text}")
                   if e.status == 429: # Rate limited
                        retry_after = e.retry_after if hasattr(e, 'retry_after') and e.retry_after else delay * 2 # Use retry_after if available
                        self.logger.warning(f"Rate limited fetching channel. Retrying after {retry_after:.2f}s...")
                        await asyncio.sleep(retry_after)
                        delay = retry_after # Adjust delay based on header
                   else:
                        # Exponential backoff for other HTTP errors
                        delay = min(delay * 2, self.MAX_RETRY_WAIT) 
                        self.logger.warning(f"Applying exponential backoff. Next retry in {delay:.1f}s.")
             except Exception as e:
                  self.logger.error(f"Unexpected {type(e).__name__} error fetching channel {channel_id} on retry {attempt + 1}: {e}", exc_info=True)
                  delay = min(delay * 2, self.MAX_RETRY_WAIT) # Exponential backoff
                  self.logger.warning(f"Applying exponential backoff due to unexpected error. Next retry in {delay:.1f}s.")
        
        self.logger.error(f"Failed to get channel {channel_id} after {self.MAX_RETRIES} retries.")
        return None

    async def _send_media_group(self, target_channel, source_channel, message_ids: list) -> List[Optional[discord.Message]]:
        """
        Send media files from source messages to target channel.
        Downloads attachments and sends as actual files (max 10 per message).
        Falls back to URLs if anything fails.
        
        Returns:
            List of sent discord.Message objects (for tracking posted message IDs)
        """
        DISCORD_MAX_FILES = 10
        sent_messages: List[Optional[discord.Message]] = []
        
        # Collect all attachments from the source messages (rate-limited to avoid 429)
        all_attachments = []
        for msg_id in message_ids:
            try:
                original_message = await self.rate_limiter.execute(
                    f"media_fetch_{source_channel.id}",
                    lambda mid=msg_id: source_channel.fetch_message(int(mid))
                )
                if original_message and original_message.attachments:
                    all_attachments.extend(original_message.attachments)
                await asyncio.sleep(0.5)
            except Exception as e:
                self.logger.warning(f"Could not fetch message {msg_id} for media: {e}")
        
        if not all_attachments:
            return sent_messages
        
        # Send in chunks of 10 (Discord's limit)
        for i in range(0, len(all_attachments), DISCORD_MAX_FILES):
            chunk = all_attachments[i:i + DISCORD_MAX_FILES]
            sent_as_files = False
            
            try:
                files = []
                for attachment in chunk:
                    file_bytes = await attachment.read()
                    files.append(discord.File(io.BytesIO(file_bytes), filename=attachment.filename))
                
                if files:
                    sent_msg = await discord_utils.safe_send_message(
                        self.bot, target_channel, self.rate_limiter, self.logger, files=files
                    )
                    if sent_msg:
                        sent_messages.append(sent_msg)
                    sent_as_files = True
                    await asyncio.sleep(0.5)
            except Exception as e:
                self.logger.warning(f"Failed to send files, falling back to URLs: {e}")
            
            # Fallback: send URLs individually
            if not sent_as_files:
                for attachment in chunk:
                    try:
                        sent_msg = await discord_utils.safe_send_message(
                            self.bot, target_channel, self.rate_limiter, self.logger, content=attachment.url
                        )
                        if sent_msg:
                            sent_messages.append(sent_msg)
                        await asyncio.sleep(0.3)
                    except Exception as e_url:
                        self.logger.error(f"Failed to send URL fallback: {e_url}")
        
        return sent_messages

    async def get_channel_history(self, channel_id: int) -> List[dict]:
        """
        Fetches the message history for a given channel from the database,
        joining with the channels table to include the channel name.
        """
        # Calculate the timestamp for 24 hours ago
        time_24_hours_ago = datetime.utcnow() - timedelta(hours=self.DEFAULT_TIME_DELTA_HOURS)
        time_24_hours_ago_str = time_24_hours_ago.isoformat()
        
        self.logger.info(f"📥 Fetching message history for channel {channel_id}")
        self.logger.info(f"⏰ Time filter: created_at >= {time_24_hours_ago_str}")

        query = """
            SELECT
                m.*,
                c.channel_name,
                COALESCE(mb.server_nick, mb.global_name, mb.username, 'Unknown User') as author_name,
                mb.include_in_updates
            FROM
                messages m
            LEFT JOIN
                channels c ON m.channel_id = c.channel_id
            LEFT JOIN
                members mb ON m.author_id = mb.member_id
            WHERE
                m.channel_id = ? AND
                m.created_at >= ? AND
                (mb.bot IS NULL OR mb.bot = FALSE) AND
                m.is_deleted = FALSE
            ORDER BY
                m.created_at ASC
        """
        
        self.logger.info(f"🔍 Query: {query}")
        self.logger.info(f"📋 Params: channel_id={channel_id}, created_at>={time_24_hours_ago_str}")
        
        try:
            # Reuse the existing db_handler instead of creating a new one
            messages = await self._execute_db_operation(
                self.db_handler.execute_query, 
                query, 
                (channel_id, time_24_hours_ago_str),
                db_handler=self.db_handler
            )
            
            # Anonymize author names for users who have opted out of include_in_updates
            # include_in_updates defaults to TRUE in DB, so only explicit FALSE means opt-out
            opt_out_count = 0
            for msg in messages:
                if msg.get('include_in_updates') is False:
                    msg['author_name'] = 'A community member'
                    opt_out_count += 1
            
            if opt_out_count > 0:
                self.logger.info(f"📝 Anonymized {opt_out_count} message(s) from users who opted out of updates")
            
            # Since execute_query returns a list of dicts, we just return it
            self.logger.info(f"✅ Fetched {len(messages)} messages from the database for channel {channel_id}")
            return messages
        
        except Exception as e:
            self.logger.error(f"Failed to fetch messages for channel {channel_id} from DB: {e}")
            self.logger.debug(traceback.format_exc())
            return []


    async def create_media_content(self, files: List[Tuple[discord.File, int, str, str]], max_media: int = 4) -> Optional[discord.File]:
        """Create a collage of images or a combined video, depending on attachments."""
        try:
            if not MEDIA_PROCESSING_AVAILABLE:
                self.logger.error("Media processing libraries are not available (PIL not installed)")
                return None
            
            self.logger.info(f"Starting media content creation with {len(files)} files")
            
            # Check if moviepy is available for video processing
            if mp is None:
                self.logger.info("moviepy not available - will only process images, skipping videos")
            
            images = []
            videos = []
            has_audio = False
            
            for file_tuple, _, _, _ in files[:max_media]:
                file_tuple.fp.seek(0)
                data = file_tuple.fp.read()
                
                if file_tuple.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    self.logger.debug(f"Processing image: {file_tuple.filename}")
                    img = Image.open(io.BytesIO(data))
                    images.append(img)
                elif file_tuple.filename.lower().endswith(('.mp4', '.mov', '.webm')) and mp is not None:
                    self.logger.debug(f"Processing video: {file_tuple.filename}")
                    temp_path = f'temp_{len(videos)}.mp4'
                    with open(temp_path, 'wb') as f:
                        f.write(data)
                    video = mp.VideoFileClip(temp_path)
                    if video.audio is not None:
                        has_audio = True
                        self.logger.debug(f"Video {file_tuple.filename} has audio")
                    videos.append(video)
            
            self.logger.info(f"Processed {len(images)} images and {len(videos)} videos. Has audio: {has_audio}")
                
            if videos and has_audio:
                self.logger.info("Creating combined video with audio")
                final_video = mp.concatenate_videoclips(videos)
                output_path = 'combined_video.mp4'
                final_video.write_videofile(output_path)
                
                for video in videos:
                    video.close()
                final_video.close()
                
                self.logger.info("Video combination complete")
                
                with open(output_path, 'rb') as f:
                    return discord.File(f, filename='combined_video.mp4')
                
            elif images or (videos and not has_audio):
                self.logger.info("Creating image/GIF collage")
                
                # Convert silent videos to GIF
                for i, video in enumerate(videos):
                    self.logger.debug(f"Converting silent video {i+1} to GIF")
                    gif_path = f'temp_gif_{len(images)}.gif'
                    video.write_gif(gif_path)
                    gif_img = Image.open(gif_path)
                    images.append(gif_img)
                    video.close()
                
                if not images:
                    self.logger.warning("No images available for collage")
                    return None
                
                n = len(images)
                if n == 1:
                    cols, rows = 1, 1
                elif n == 2:
                    cols, rows = 2, 1
                else:
                    cols, rows = 2, 2
                
                self.logger.debug(f"Creating {cols}x{rows} collage for {n} images")
                
                target_size = (800 // cols, 800 // rows)
                resized_images = []
                for i, img in enumerate(images):
                    self.logger.debug(f"Resizing image {i+1}/{len(images)} to {target_size}")
                    img = img.convert('RGB')
                    img.thumbnail(target_size)
                    resized_images.append(img)
                
                collage = Image.new('RGB', (800, 800))
                
                for idx, img in enumerate(resized_images):
                    x = (idx % cols) * (800 // cols)
                    y = (idx // cols) * (800 // rows)
                    collage.paste(img, (x, y))
                
                self.logger.info("Collage creation complete")
                
                buffer = io.BytesIO()
                collage.save(buffer, format='JPEG')
                buffer.seek(0)
                return discord.File(buffer, filename='collage.jpg')
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error creating media content: {e}")
            self.logger.debug(traceback.format_exc())
            return None
        finally:
            # Cleanup
            import os
            self.logger.debug("Cleaning up temporary files")
            for f in os.listdir():
                if f.startswith('temp_'):
                    try:
                        os.remove(f)
                        self.logger.debug(f"Removed temporary file: {f}")
                    except Exception as ex:
                        self.logger.warning(f"Failed to remove temporary file {f}: {ex}")

    async def create_summary_thread(self, message, thread_name, is_top_generations=False):
        try:
            self.logger.info(f"Attempting to create thread '{thread_name}' for message {message.id}")
            # If it's already a Thread object
            if isinstance(message, discord.Thread):
                self.logger.warning(f"Message is already a Thread object with ID {message.id}. Returning it directly.")
                return message

            if not message.guild:
                self.logger.error("Cannot create thread: message is not in a guild")
                return None

            bot_member = message.guild.get_member(self.bot.user.id)
            if not bot_member:
                self.logger.error("Cannot find bot member in guild")
                return None

            self.logger.debug(f"Using channel: {message.channel} (ID: {message.channel.id}) for thread creation")
            required_permissions = ['create_public_threads', 'send_messages_in_threads', 'manage_messages']
            missing_permissions = [perm for perm in required_permissions if not getattr(message.channel.permissions_for(bot_member), perm, False)]
            if missing_permissions:
                self.logger.error(f"Missing required permissions in channel {message.channel.id}: {', '.join(missing_permissions)}")
                return None
            
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=1440  # 24 hours
            )
            
            if thread:
                self.logger.info(f"Successfully created thread: {thread.name} (ID: {thread.id})")
                
                # Only pin/unpin if this is not a top generations thread
                if not is_top_generations:
                    try:
                        pinned_messages = await message.channel.pins()
                        for pinned_msg in pinned_messages:
                            if pinned_msg.author.id == self.bot.user.id:
                                await pinned_msg.unpin()
                                self.logger.info(f"Unpinned previous message: {pinned_msg.id}")
                    except Exception as e:
                        self.logger.error(f"Error unpinning previous messages: {e}")
                    
                    try:
                        await message.pin()
                        self.logger.info(f"Pinned new thread starter message: {message.id}")
                    except Exception as e:
                        self.logger.error(f"Error pinning new message: {e}")
                
                return thread
            else:
                self.logger.error("Thread creation returned None")
                return None
                
        except discord.Forbidden as e:
            self.logger.error(f"Forbidden error creating thread: {e}")
            self.logger.debug(traceback.format_exc())
            return None
        except discord.HTTPException as e:
            self.logger.error(f"HTTP error creating thread: {e}")
            self.logger.debug(traceback.format_exc())
            return None
        except Exception as e:
            self.logger.error(f"Error creating thread: {e}")
            self.logger.debug(traceback.format_exc())
            return None

    @handle_errors("_execute_db_operation")
    async def _execute_db_operation(self, operation, *args, db_handler=None):
        """Execute a blocking DB call in a non-blocking way.

        Parameters
        ----------
        operation: Callable
            The synchronous function/method that will be executed (e.g. `db_handler.execute_query`).
        *args: Any
            Positional arguments that should be forwarded to the operation.
        db_handler: DatabaseHandler | None  # noqa: D401
            Optional database handler instance. Included only so callers do not need to repeat it in *args.

        Returns
        -------
        Any | list
            Whatever the `operation` returns. If the call errors the exception is logged and an
            empty list is returned to keep downstream logic working (most callers expect an
            iterable and will happily handle an empty one).
        """

        # NOTE: `operation` is expected to be **blocking** (SQLite access) therefore we shuttle
        # it off to a threadpool so the asyncio event-loop does not stall.
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: operation(*args))
            return result
        except Exception as exc:
            # We already have the @handle_errors decorator but this explicit handler lets us
            # decide what to return so callers don't break on `None`.
            self.logger.error(f"Database operation failed inside _execute_db_operation: {exc}", exc_info=True)
            return []

    async def _post_summary_with_transaction(self, channel_id: int, summary: str, messages: list, current_date: datetime, db_handler: DatabaseHandler, dev_mode: bool = False) -> bool:
        """Atomic operation for posting summary and updating database"""
        try:
            # Generate a short summary using the NewsSummarizer
            short_summary = await self.news_summarizer.generate_short_summary(summary, len(messages))
            
            # Use the dedicated method in db_handler which handles its own transaction and retries.
            success = await asyncio.to_thread(
                db_handler.store_daily_summary,
                channel_id,
                summary,
                short_summary,
                current_date,
                False,  # included_in_main_summary - will be updated later if needed
                dev_mode,
                guild_id=self.guild_id
            )
            
            if success:
                self.logger.info(f"Successfully saved summary for channel {channel_id} to DB (dev_mode={dev_mode}).")
            else:
                self.logger.warning(f"Failed to save summary for channel {channel_id}, store_daily_summary returned False.")

            return success
            
        except Exception as e:
            # Catch errors from generate_short_summary or the transaction execution
            self.logger.error(f"Error in _post_summary_with_transaction for channel {channel_id}: {e}", exc_info=True)
            return False

    def _extract_message_ids_by_channel(self, summary_json: str) -> Dict[int, List[str]]:
        """
        Extract message IDs grouped by channel from a combined summary JSON.
        
        Args:
            summary_json: JSON string containing the combined summary items
            
        Returns:
            Dict mapping channel_id -> list of message_ids from that channel
        """
        channel_message_ids: Dict[int, List[str]] = {}
        
        try:
            items = json.loads(summary_json)
            if not isinstance(items, list):
                return channel_message_ids
                
            for item in items:
                # Get channel_id and message_id from main item
                channel_id = item.get('channel_id')
                message_id = item.get('message_id')
                
                if channel_id and message_id:
                    channel_id_int = int(channel_id)
                    if channel_id_int not in channel_message_ids:
                        channel_message_ids[channel_id_int] = []
                    channel_message_ids[channel_id_int].append(str(message_id))
                
                # Also check subTopics for additional message_ids
                sub_topics = item.get('subTopics', [])
                for sub in sub_topics:
                    sub_channel_id = sub.get('channel_id')
                    sub_message_id = sub.get('message_id')
                    
                    if sub_channel_id and sub_message_id:
                        sub_channel_id_int = int(sub_channel_id)
                        if sub_channel_id_int not in channel_message_ids:
                            channel_message_ids[sub_channel_id_int] = []
                        if str(sub_message_id) not in channel_message_ids[sub_channel_id_int]:
                            channel_message_ids[sub_channel_id_int].append(str(sub_message_id))
                            
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to parse summary JSON for message_id extraction: {e}")
            
        return channel_message_ids

    def _extract_media_message_ids(self, summary_json: str) -> Dict[int, Set[str]]:
        """
        Extract all media message IDs grouped by channel from a summary JSON.
        These are the messages that have media (images/videos) to persist.
        
        Args:
            summary_json: JSON string containing the summary items
            
        Returns:
            Dict mapping channel_id -> set of media message_ids
        """
        channel_media_ids: Dict[int, Set[str]] = {}
        
        try:
            items = json.loads(summary_json)
            if not isinstance(items, list):
                return channel_media_ids
                
            for item in items:
                channel_id = item.get('channel_id')
                if not channel_id:
                    continue
                channel_id_int = int(channel_id)
                
                if channel_id_int not in channel_media_ids:
                    channel_media_ids[channel_id_int] = set()
                
                # Get mainMediaMessageId
                main_media_id = item.get('mainMediaMessageId')
                if main_media_id:
                    channel_media_ids[channel_id_int].add(str(main_media_id))
                
                # Get subTopicMediaMessageIds
                for sub in item.get('subTopics', []):
                    sub_channel_id = sub.get('channel_id')
                    if sub_channel_id:
                        sub_channel_int = int(sub_channel_id)
                        if sub_channel_int not in channel_media_ids:
                            channel_media_ids[sub_channel_int] = set()
                        
                        for media_id in sub.get('subTopicMediaMessageIds', []):
                            if media_id:
                                channel_media_ids[sub_channel_int].add(str(media_id))
                            
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to parse summary JSON for media message_id extraction: {e}")
        
        return channel_media_ids
    
    async def _fetch_message_for_moderation(self, channel_id: int, message_id: str):
        """
        Fetch a Discord message for content moderation.
        This is a callback passed to filter_summary_media.
        """
        try:
            channel = await self._get_channel_with_retry(channel_id)
            if not channel:
                return None
            return await channel.fetch_message(int(message_id))
        except discord.NotFound:
            self.logger.warning(f"Message {message_id} not found in channel {channel_id}")
            return None
        except discord.Forbidden:
            self.logger.warning(f"No permission to fetch message {message_id}")
            return None
        except Exception as e:
            self.logger.error(f"Error fetching message {message_id}: {e}")
            return None

    def _extract_video_poster(self, video_bytes: bytes, frame_time: float = 1.0) -> Optional[bytes]:
        """
        Extract a poster frame from video bytes using imageio.
        
        Args:
            video_bytes: Raw video file bytes
            frame_time: Time in seconds to extract frame from (default 1.0)
            
        Returns:
            JPEG bytes of the poster frame, or None on failure
        """
        if not POSTER_EXTRACTION_AVAILABLE:
            self.logger.warning("Poster extraction not available (imageio/imageio-ffmpeg not installed)")
            return None
        
        import tempfile
        temp_video = None
        try:
            # Write video to temp file (imageio needs a file path for videos)
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
                f.write(video_bytes)
                temp_video = f.name
            
            # Get video metadata to find duration
            meta = iio.immeta(temp_video, plugin="pyav")
            duration = meta.get('duration', 0)
            fps = meta.get('fps', 30)
            
            # Calculate which frame to extract
            actual_time = min(frame_time, duration - 0.1) if duration > frame_time else 0
            frame_index = int(actual_time * fps)
            
            # Read the specific frame
            frame = iio.imread(temp_video, index=frame_index, plugin="pyav")
            
            # Convert to JPEG bytes (compressed for community - 300x300 max, quality 70)
            img = Image.fromarray(frame)
            
            # Resize to max 300x300 for community feed
            if img.width > 300 or img.height > 300:
                img.thumbnail((300, 300), Image.Resampling.LANCZOS)
            
            img_buffer = io.BytesIO()
            img.save(img_buffer, format='JPEG', quality=70, optimize=True)
            img_buffer.seek(0)
            
            self.logger.debug(f"Extracted poster frame at {actual_time:.1f}s (frame {frame_index})")
            return img_buffer.getvalue()
            
        except Exception as e:
            self.logger.warning(f"Failed to extract video poster: {e}")
            return None
        finally:
            # Clean up temp file
            if temp_video:
                try:
                    os.unlink(temp_video)
                except OSError:
                    pass

    def _is_video_content_type(self, content_type: str) -> bool:
        """Check if content type indicates a video file."""
        return content_type and content_type.startswith('video/')

    def _compress_image(
        self, 
        image_bytes: bytes, 
        max_size: int = 300, 
        quality: int = 70,
        content_type: str = 'image/jpeg'
    ) -> Optional[tuple[bytes, str]]:
        """
        Compress and resize an image for community feed display.
        
        Args:
            image_bytes: Raw image bytes
            max_size: Maximum dimension (width or height), default 300px for community
            quality: JPEG quality (1-100), default 70 for ~15-30KB target
            content_type: Original content type to determine format
            
        Returns:
            Tuple of (compressed_bytes, content_type) or None on failure
        """
        if not MEDIA_PROCESSING_AVAILABLE:
            self.logger.warning("Image compression not available (PIL not installed)")
            return None
        
        try:
            img = Image.open(io.BytesIO(image_bytes))
            original_size = len(image_bytes)
            
            # Convert RGBA/P to RGB for JPEG output
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            # Resize if larger than max_size (maintains aspect ratio)
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                self.logger.debug(f"Resized image to {img.width}x{img.height}")
            
            # Compress to JPEG
            img_buffer = io.BytesIO()
            img.save(img_buffer, format='JPEG', quality=quality, optimize=True)
            img_buffer.seek(0)
            compressed_bytes = img_buffer.getvalue()
            
            compressed_size = len(compressed_bytes)
            savings = ((original_size - compressed_size) / original_size) * 100
            self.logger.debug(
                f"Compressed image: {original_size/1024:.1f}KB -> {compressed_size/1024:.1f}KB "
                f"({savings:.1f}% reduction)"
            )
            
            return (compressed_bytes, 'image/jpeg')
            
        except Exception as e:
            self.logger.warning(f"Failed to compress image: {e}")
            return None

    def _compress_video(self, video_bytes: bytes, target_height: int = 360, crf: int = 28) -> Optional[bytes]:
        """
        Compress video to 360p for community feed display.
        
        Args:
            video_bytes: Raw video bytes
            target_height: Target height in pixels (width scales proportionally), default 360p
            crf: Constant Rate Factor (18-28 typical, higher = smaller file), default 28
            
        Returns:
            Compressed video bytes or None on failure
        """
        if not POSTER_EXTRACTION_AVAILABLE:
            self.logger.warning("Video compression not available (imageio-ffmpeg not installed)")
            return None
        
        import tempfile
        import subprocess
        temp_input = None
        temp_output = None
        
        try:
            # Get ffmpeg path from imageio-ffmpeg
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
            
            # Write input video to temp file
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
                f.write(video_bytes)
                temp_input = f.name
            
            # Create temp output path
            temp_output = temp_input.replace('.mp4', '_compressed.mp4')
            
            original_size = len(video_bytes)
            
            # FFmpeg command: scale to 360p, CRF 28, no audio (for community thumbnails)
            cmd = [
                ffmpeg_path,
                '-i', temp_input,
                '-vf', f'scale=-2:{target_height}',
                '-c:v', 'libx264',
                '-crf', str(crf),
                '-preset', 'medium',
                '-an',  # No audio for community feed
                '-y',  # Overwrite output
                temp_output
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            
            if result.returncode != 0:
                self.logger.warning(f"FFmpeg compression failed: {result.stderr.decode()[:500]}")
                return None
            
            # Read compressed video
            with open(temp_output, 'rb') as f:
                compressed_bytes = f.read()
            
            compressed_size = len(compressed_bytes)
            savings = ((original_size - compressed_size) / original_size) * 100
            self.logger.debug(
                f"Compressed video: {original_size/1024/1024:.1f}MB -> {compressed_size/1024/1024:.1f}MB "
                f"({savings:.1f}% reduction)"
            )
            
            return compressed_bytes
            
        except subprocess.TimeoutExpired:
            self.logger.warning("Video compression timed out (>120s)")
            return None
        except Exception as e:
            self.logger.warning(f"Failed to compress video: {e}")
            return None
        finally:
            # Clean up temp files
            for path in [temp_input, temp_output]:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def _enrich_summary_with_media_urls(
        self, 
        summary_json: str, 
        media_urls: Dict[str, List[Dict[str, str]]]
    ) -> str:
        """
        Enrich the summary JSON by embedding persisted media URLs directly into each news item.
        
        Adds:
        - mainMediaUrls: array of media objects for the mainMediaMessageId
        - subTopicMediaUrls: array of arrays, one per subTopicMediaMessageIds entry
        
        Args:
            summary_json: The combined summary JSON string
            media_urls: Dict mapping message_id -> list of media objects
            
        Returns:
            Enriched summary JSON string with embedded media URLs
        """
        try:
            items = json.loads(summary_json)
            if not isinstance(items, list):
                return summary_json
            
            for item in items:
                # Add mainMediaUrls
                main_media_id = item.get('mainMediaMessageId')
                if main_media_id and str(main_media_id) in media_urls:
                    item['mainMediaUrls'] = media_urls[str(main_media_id)]
                else:
                    item['mainMediaUrls'] = None
                
                # Process subTopics
                for sub in item.get('subTopics', []):
                    sub_media_ids = sub.get('subTopicMediaMessageIds', [])
                    sub_media_urls = []
                    
                    for media_id in sub_media_ids:
                        if media_id and str(media_id) in media_urls:
                            sub_media_urls.append(media_urls[str(media_id)])
                        else:
                            sub_media_urls.append(None)
                    
                    sub['subTopicMediaUrls'] = sub_media_urls
            
            return json.dumps(items, indent=2)
            
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to enrich summary with media URLs: {e}")
            return summary_json

    def _enrich_summary_with_posted_ids(
        self, 
        summary_json: str, 
        posted_by_topic: Dict[int, List[int]]
    ) -> str:
        """
        Enrich the summary JSON by embedding posted Discord message IDs into each topic.
        
        Adds 'posted_message_ids' array to each topic containing the IDs of Discord
        messages that were posted for that topic.
        
        Args:
            summary_json: The summary JSON string
            posted_by_topic: Dict mapping topic_index -> list of posted message IDs
            
        Returns:
            Enriched summary JSON string with embedded posted message IDs
        """
        try:
            items = json.loads(summary_json)
            if not isinstance(items, list):
                return summary_json
            
            for idx, item in enumerate(items):
                if idx in posted_by_topic:
                    item['posted_message_ids'] = posted_by_topic[idx]
                else:
                    item['posted_message_ids'] = []
            
            return json.dumps(items, indent=2)
            
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to enrich summary with posted message IDs: {e}")
            return summary_json

    def _get_included_message_ids(self, main_summary_json: str) -> Set[str]:
        """
        Extract all message_ids (main items + subtopics) from the main summary.
        These are the items that were selected for inclusion.
        
        Returns:
            Set of message_ids that are in the main summary
        """
        included_ids: Set[str] = set()
        
        try:
            items = json.loads(main_summary_json)
            if not isinstance(items, list):
                return included_ids
            
            for item in items:
                # Add main item's message_id
                if item.get('message_id'):
                    included_ids.add(str(item['message_id']))
                
                # Add subtopic message_ids
                for sub in item.get('subTopics', []):
                    if sub.get('message_id'):
                        included_ids.add(str(sub['message_id']))
                        
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to extract included message_ids: {e}")
            
        return included_ids

    def _enrich_channel_summary_with_inclusion(
        self,
        channel_summary_json: str,
        included_message_ids: Set[str],
        media_urls: Dict[str, List[Dict[str, str]]]
    ) -> str:
        """
        Enrich a channel summary by marking which items/subtopics were included in the main summary
        and embedding their persisted media URLs.
        
        Adds to each news item:
        - included_in_main: boolean
        - mainMediaUrls: array of media objects (if included and has media)
        
        Adds to each subtopic:
        - included_in_main: boolean
        - subTopicMediaUrls: array of arrays (if included and has media)
        
        Args:
            channel_summary_json: The channel's full_summary JSON string
            included_message_ids: Set of message_ids that were included in main summary
            media_urls: Dict mapping message_id -> list of media objects
            
        Returns:
            Enriched channel summary JSON with inclusion flags and media URLs
        """
        try:
            items = json.loads(channel_summary_json)
            if not isinstance(items, list):
                return channel_summary_json
            
            for item in items:
                item_msg_id = str(item.get('message_id', ''))
                item_included = item_msg_id in included_message_ids
                item['included_in_main'] = item_included
                
                # Add media URLs if included
                if item_included:
                    main_media_id = item.get('mainMediaMessageId')
                    if main_media_id and str(main_media_id) in media_urls:
                        item['mainMediaUrls'] = media_urls[str(main_media_id)]
                    else:
                        item['mainMediaUrls'] = None
                
                # Process subtopics
                for sub in item.get('subTopics', []):
                    sub_msg_id = str(sub.get('message_id', ''))
                    sub_included = sub_msg_id in included_message_ids
                    sub['included_in_main'] = sub_included
                    
                    # Add media URLs if included
                    if sub_included:
                        sub_media_ids = sub.get('subTopicMediaMessageIds', [])
                        sub_media_urls = []
                        
                        for media_id in sub_media_ids:
                            if media_id and str(media_id) in media_urls:
                                sub_media_urls.append(media_urls[str(media_id)])
                            else:
                                sub_media_urls.append(None)
                        
                        sub['subTopicMediaUrls'] = sub_media_urls
            
            return json.dumps(items, indent=2)
            
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"Failed to enrich channel summary with inclusion flags: {e}")
            return channel_summary_json

    async def _update_channel_summaries_with_inclusion(
        self,
        main_summary_json: str,
        media_urls: Dict[str, List[Dict[str, str]]],
        current_date: datetime,
        db_handler: DatabaseHandler
    ) -> bool:
        """
        Update all channel summaries to mark which items/subtopics were included
        in the main summary and embed their persisted media URLs.
        
        Args:
            main_summary_json: The combined main summary JSON
            media_urls: Dict mapping message_id -> list of media objects
            current_date: Date of the summaries
            db_handler: Database handler
            
        Returns:
            True if all updates successful
        """
        # Get the set of included message_ids from the main summary
        included_message_ids = self._get_included_message_ids(main_summary_json)
        self.logger.info(f"Updating channel summaries with {len(included_message_ids)} included items")
        
        # Get channel_ids that contributed to the main summary
        channel_message_ids = self._extract_message_ids_by_channel(main_summary_json)
        
        all_success = True
        for channel_id in channel_message_ids.keys():
            try:
                # Fetch the channel summary from DB
                channel_summary = db_handler.get_summary_for_date(channel_id, current_date)
                if not channel_summary:
                    self.logger.warning(f"No summary found for channel {channel_id} on {current_date}")
                    continue
                
                # Enrich with inclusion flags and media URLs
                enriched_summary = self._enrich_channel_summary_with_inclusion(
                    channel_summary,
                    included_message_ids,
                    media_urls
                )
                
                # Update the channel summary in DB
                success = await asyncio.to_thread(
                    db_handler.update_channel_summary_full_summary,
                    channel_id,
                    current_date,
                    enriched_summary
                )
                
                if success:
                    self.logger.debug(f"Updated channel {channel_id} summary with inclusion flags")
                else:
                    self.logger.warning(f"Failed to update channel {channel_id} summary")
                    all_success = False
                    
            except Exception as e:
                self.logger.error(f"Error updating channel {channel_id} summary: {e}")
                all_success = False
        
        return all_success

    async def _persist_summary_media(
        self, 
        summary_json: str, 
        current_date: datetime, 
        db_handler: DatabaseHandler
    ) -> Dict[str, List[Dict[str, str]]]:
        """
        Download and upload all media from summary items to Supabase Storage.
        For videos, also extracts and uploads a poster image.
        
        Args:
            summary_json: The combined summary JSON
            current_date: Date for organizing storage paths
            db_handler: Database handler for uploads
            
        Returns:
            Dict mapping message_id -> list of media objects with url, type, and optional poster_url
            Example: {"123": [{"url": "...", "type": "video", "poster_url": "..."}, {"url": "...", "type": "image"}]}
        """
        media_urls: Dict[str, List[Dict[str, str]]] = {}
        
        # Extract all media message IDs grouped by channel
        channel_media_ids = self._extract_media_message_ids(summary_json)
        
        total_messages = sum(len(ids) for ids in channel_media_ids.values())
        self.logger.info(f"Persisting media from {total_messages} messages across {len(channel_media_ids)} channels")
        
        date_str = current_date.strftime('%Y-%m-%d')
        
        for channel_id, message_ids in channel_media_ids.items():
            try:
                channel = await self._get_channel_with_retry(channel_id)
                if not channel:
                    self.logger.warning(f"Could not fetch channel {channel_id} for media persistence")
                    continue
                
                for message_id in message_ids:
                    try:
                        message = await channel.fetch_message(int(message_id))
                        if not message.attachments:
                            continue
                        
                        message_media = []
                        for idx, attachment in enumerate(message.attachments):
                            # Note: Content moderation already done via filter_summary_media()
                            # before this method is called, so we don't need to check here
                            
                            # Download the file to get bytes and content type
                            file_data = await db_handler.download_file(attachment.url)
                            if not file_data:
                                self.logger.warning(f"Failed to download {attachment.filename} from message {message_id}")
                                continue
                            
                            file_bytes = file_data['bytes']
                            content_type = file_data['content_type']
                            ext = attachment.filename.split('.')[-1] if '.' in attachment.filename else 'bin'
                            
                            is_video = self._is_video_content_type(content_type)
                            is_image = content_type and content_type.startswith('image/')
                            
                            # Compress images before upload (300x300 max, quality 70 for community)
                            if is_image and not is_video:
                                compressed = self._compress_image(
                                    file_bytes, 
                                    max_size=300, 
                                    quality=70,
                                    content_type=content_type
                                )
                                if compressed:
                                    file_bytes, content_type = compressed
                                    ext = 'jpg'  # Compressed images are always JPEG
                            
                            # Compress videos to 360p for community feed
                            if is_video:
                                self.logger.debug(f"Compressing video {attachment.filename} to 360p")
                                compressed_video = self._compress_video(file_bytes, target_height=360, crf=28)
                                if compressed_video:
                                    file_bytes = compressed_video
                                    ext = 'mp4'  # Compressed videos are always MP4
                                    content_type = 'video/mp4'
                            
                            # Upload the main file
                            storage_path = f"{date_str}/{message_id}_{idx}.{ext}"
                            storage_url = await db_handler.upload_bytes(
                                file_bytes, storage_path, content_type
                            )
                            
                            if not storage_url:
                                self.logger.warning(f"Failed to upload {attachment.filename} from message {message_id}")
                                continue
                            
                            # Build media entry
                            media_entry: Dict[str, str] = {
                                'url': storage_url,
                                'type': 'video' if is_video else 'image'
                            }
                            
                            # For videos, extract and upload poster (300x300 max, quality 70)
                            if is_video:
                                self.logger.debug(f"Extracting poster for video {attachment.filename}")
                                poster_bytes = self._extract_video_poster(file_bytes)
                                
                                if poster_bytes:
                                    poster_path = f"{date_str}/{message_id}_{idx}_poster.jpg"
                                    poster_url = await db_handler.upload_bytes(
                                        poster_bytes, poster_path, 'image/jpeg'
                                    )
                                    
                                    if poster_url:
                                        media_entry['poster_url'] = poster_url
                                        self.logger.debug(f"Persisted video + poster: {storage_path}")
                                    else:
                                        self.logger.warning(f"Failed to upload poster for {attachment.filename}")
                                else:
                                    self.logger.warning(f"Could not extract poster for {attachment.filename}")
                            else:
                                self.logger.debug(f"Persisted image: {storage_path}")
                            
                            message_media.append(media_entry)
                        
                        if message_media:
                            media_urls[str(message_id)] = message_media
                            
                    except discord.NotFound:
                        self.logger.warning(f"Message {message_id} not found in channel {channel_id}")
                    except discord.Forbidden:
                        self.logger.warning(f"No permission to fetch message {message_id}")
                    except Exception as e:
                        self.logger.error(f"Error fetching message {message_id}: {e}")
                        
            except Exception as e:
                self.logger.error(f"Error processing channel {channel_id} for media persistence: {e}")
        
        total_files = sum(len(m) for m in media_urls.values())
        video_count = sum(1 for msgs in media_urls.values() for m in msgs if m.get('type') == 'video')
        self.logger.info(f"Persisted media from {len(media_urls)} messages ({total_files} files, {video_count} videos with posters)")
        return media_urls

    def is_forum_channel(self, channel_id: int) -> bool:
        """Check if a channel is a ForumChannel by ID"""
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                # Try to fetch the channel if not in cache
                import asyncio
                try:
                    channel = asyncio.run_coroutine_threadsafe(
                        self.bot.fetch_channel(channel_id), 
                        self.bot.loop
                    ).result(timeout=5)
                except Exception:
                    return False
            return isinstance(channel, discord.ForumChannel)
        except Exception as e:
            self.logger.error(f"Error checking if channel {channel_id} is forum channel: {e}")
            return False

    async def _get_dev_mode_channels(self, db_handler):
        """Get active channels for dev mode"""
        try:
            # Get source channel (where to pull messages from)
            test_channel_str = os.getenv('TEST_DATA_CHANNEL', '')
            self.logger.info(f"[DEV MODE] TEST_DATA_CHANNEL env var = '{test_channel_str}'")
            if not test_channel_str:
                self.logger.error("[DEV MODE] ❌ TEST_DATA_CHANNEL not configured!")
                return []
                
            test_channel_ids = [int(cid.strip()) for cid in test_channel_str.split(',') if cid.strip()]
            self.logger.info(f"[DEV MODE] Parsed test channel IDs: {test_channel_ids}")
            if not test_channel_ids:
                self.logger.error("[DEV MODE] ❌ No test channels parsed!")
                return []

            # Get destination channels (where to post summaries)
            dev_channels_str = os.getenv('DEV_CHANNELS_TO_MONITOR', '')
            self.logger.info(f"[DEV MODE] DEV_CHANNELS_TO_MONITOR env var = '{dev_channels_str}'")
            if not dev_channels_str:
                self.logger.error("[DEV MODE] ❌ DEV_CHANNELS_TO_MONITOR not configured!")
                return []

            dev_channel_ids = [int(cid.strip()) for cid in dev_channels_str.split(',') if cid.strip()]
            self.logger.info(f"[DEV MODE] Parsed dev channel IDs: {dev_channel_ids}")
            if not dev_channel_ids:
                self.logger.error("[DEV MODE] ❌ No dev channels parsed!")
                return []
                
            # Calculate 24 hours ago for consistency with get_channel_history
            from datetime import datetime, timedelta
            time_24_hours_ago = datetime.utcnow() - timedelta(hours=24)
            time_24_hours_ago_str = time_24_hours_ago.isoformat()
            
            query = (
                "SELECT DISTINCT channel_id "
                "FROM messages "
                "WHERE channel_id IN ({}) AND created_at >= '{}' "
                "GROUP BY channel_id "
                "HAVING COUNT(*) >= 25"
            ).format(",".join(str(cid) for cid in test_channel_ids), time_24_hours_ago_str)
            
            self.logger.info(f"[DEV MODE] Query: {query}")
            
            loop = asyncio.get_running_loop()
            try:
                def db_operation():
                    try:
                        self.logger.info(f"[DEV MODE] 🔍 Executing query via db_handler.execute_query...")
                        results = db_handler.execute_query(query)
                        self.logger.info(f"[DEV MODE] Query returned {len(results) if results else 0} results")
                        if results:
                            self.logger.info(f"[DEV MODE] Raw results: {results}")
                        else:
                            self.logger.warning(f"[DEV MODE] ❌ Query returned empty results!")
                        # For each source channel that has enough messages,
                        # set its post_channel_id to the first dev channel
                        if results and dev_channel_ids:
                            # Ensure results is a list of dictionaries
                            processed_results = []
                            for row in results:
                                row_dict = dict(row) # Convert row object to dict if needed
                                self.logger.info(f"[DEV MODE] Processing row: {row_dict}")
                                row_dict['post_channel_id'] = dev_channel_ids[0]
                                processed_results.append(row_dict)
                            self.logger.info(f"[DEV MODE] ✅ Returning {len(processed_results)} channels: {processed_results}")
                            return processed_results
                        self.logger.warning("[DEV MODE] ⚠️ No results or no dev channels, returning []")
                        return [] # Return empty list if no results or no dev_channel_ids
                    except Exception as e:
                        self.logger.error(f"[DEV MODE] ❌ Error in db_operation: {e}", exc_info=True)
                        return []
                        
                return await asyncio.wait_for(
                    loop.run_in_executor(None, db_operation),
                    timeout=10 # Adjust timeout as needed
                )
            except asyncio.TimeoutError:
                self.logger.error("[DEV MODE] ❌ Timeout executing query!")
                return []
            except Exception as e:
                self.logger.error(f"[DEV MODE] ❌ Error executing query: {e}", exc_info=True)
                return []
        except Exception as e:
            self.logger.error(f"[DEV MODE] ❌ Error in _get_dev_mode_channels: {e}", exc_info=True)
            return []

    async def _get_production_channels(self, db_handler):
        """Get active channels for production mode"""
        try:
            if self.monitor_all_channels:
                self.logger.info("[PRODUCTION MODE] Querying all channels enabled for summaries in this guild...")
                channel_query = (
                    "SELECT c.channel_id, c.channel_name, COALESCE(c2.channel_name, 'Unknown') as source, "
                    "COUNT(m.message_id) as msg_count "
                    "FROM channels c "
                    "LEFT JOIN channels c2 ON c.category_id = c2.channel_id "
                    "LEFT JOIN messages m ON c.channel_id = m.channel_id "
                    "AND m.created_at > datetime('now', '-24 hours') "
                    "WHERE c.guild_id = ? "
                    "GROUP BY c.channel_id, c.channel_name, source "
                    "HAVING COUNT(m.message_id) >= 25 "
                    "ORDER BY msg_count DESC"
                )
            else:
                self.logger.info(f"[PRODUCTION MODE] self.channels_to_monitor has {len(self.channels_to_monitor)} channels")
                channel_ids = ",".join(str(cid) for cid in self.channels_to_monitor)
                if not channel_ids:
                     self.logger.error("[PRODUCTION MODE] ❌ No production channels configured!")
                     return []

                self.logger.info(f"[PRODUCTION MODE] Querying {len(self.channels_to_monitor)} configured channels...")
                channel_query = (
                    "SELECT c.channel_id, c.channel_name, COALESCE(c2.channel_name, 'Unknown') as source, "
                    "COUNT(m.message_id) as msg_count "
                    "FROM channels c "
                    "LEFT JOIN channels c2 ON c.category_id = c2.channel_id "
                    "LEFT JOIN messages m ON c.channel_id = m.channel_id "
                    "AND m.created_at > datetime('now', '-24 hours') "
                    f"WHERE c.guild_id = ? AND (c.channel_id IN ({channel_ids}) OR c.category_id IN ({channel_ids})) "
                    "GROUP BY c.channel_id, c.channel_name, source "
                    "HAVING COUNT(m.message_id) >= 25 "
                    "ORDER BY msg_count DESC"
                )
            
            self.logger.info(f"Executing query: {channel_query}")
            
            loop = asyncio.get_running_loop()
            def db_operation():
                try:
                    self.logger.info("Starting database query execution...")
                    results = db_handler.execute_query(channel_query, (self.guild_id,))
                    self.logger.info(f"Database query returned {len(results) if results else 0} results: {results}")
                    return results if results else []
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        self.logger.error("Database lock timeout exceeded during production channel query")
                    else:
                        self.logger.error(f"Database operational error during production channel query: {e}", exc_info=True)
                    return []
                except Exception as e:
                    self.logger.error(f"Error getting active production channels in db_operation: {e}", exc_info=True)
                    return []
            
            self.logger.info("About to execute database query with timeout...")
            result = await asyncio.wait_for(
                loop.run_in_executor(None, db_operation),
                timeout=10 # Adjust timeout as needed
            )
            self.logger.info(f"Database query completed, returning {len(result) if result else 0} channels")
            return result
        except asyncio.TimeoutError:
            self.logger.error("Timeout while executing database query for production channels")
            return []
        except Exception as e:
            self.logger.error(f"Error executing production channel database query: {e}", exc_info=True)
            return []

    # --- Main Summary Generation Logic --- 
    async def _check_discord_connectivity(self) -> bool:
        """
        Check if Discord API is reachable before attempting summary generation.
        
        Returns:
            bool: True if Discord is reachable, False otherwise
        """
        try:
            # Try to fetch the summary channel as a connectivity test
            summary_channel = await self._get_channel_with_retry(self.summary_channel_id)
            if summary_channel:
                self.logger.info("Discord connectivity check passed")
                return True
            else:
                self.logger.warning("Discord connectivity check failed: Could not fetch summary channel")
                return False
        except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            self.logger.warning(f"Discord connectivity check failed with network error: {e}")
            return False
        except Exception as e:
            self.logger.warning(f"Discord connectivity check failed with unexpected error: {e}")
            return False

    async def _post_new_speakers(self, summary_channel: discord.TextChannel):
        """Post a new speakers welcome section to the summary channel."""
        try:
            approved = self.db_handler.get_recently_approved_intros(hours=24, guild_id=self.guild_id)
            if not approved:
                return

            # Deduplicate by member_id
            seen: dict[int, dict] = {}
            for intro in approved:
                mid = intro['member_id']
                if mid not in seen:
                    seen[mid] = intro
            unique_intros = list(seen.values())

            guild = summary_channel.guild
            recommendable = get_recommendable_channels(guild)
            blurbs = []
            mentions = []
            for intro in unique_intros:
                member = guild.get_member(intro['member_id'])
                if not member:
                    continue
                mentions.append(member.mention)
                blurb = await build_speaker_blurb(guild, intro, member, recommendable)
                if blurb:
                    blurbs.append(blurb)

            if not mentions:
                return

            header = f"## New Speakers\n\nWelcome {', '.join(mentions)}!\n"
            body = "\n".join(blurbs) if blurbs else ""
            welcome_ref = f"<#{self.welcome_channel_id}>" if self.welcome_channel_id else "the Getting Started channel"
            footer = f"\nCheck out {welcome_ref} for more info on how to get started."
            content = f"{header}\n{body}{footer}" if body else f"{header}{footer}"

            # Split if needed (Discord 2000 char limit)
            if len(content) > 2000:
                while blurbs and len(content) > 2000:
                    blurbs.pop()
                    body = "\n".join(blurbs)
                    content = f"{header}\n{body}{footer}" if body else f"{header}{footer}"

            await discord_utils.safe_send_message(
                self.bot, summary_channel, self.rate_limiter, self.logger, content=content
            )
            self.logger.info(f"Posted new speakers section ({len(mentions)} speakers) to summary channel")
        except Exception as e:
            self.logger.error(f"Failed to post new speakers section: {e}", exc_info=True)

    _SOCIAL_PICKS_PROMPT = """\
You are a social media editor for Banodoco, an open-source AI art community. \
You're reviewing today's daily summary to find content worth tweeting from the @banodoco account.

The summary data includes news items with titles. You MUST reference items by their \
exact title so we can match your picks back to the original posts and their media.

Look for:
- Exciting new tools, models, or techniques people are discussing
- Impressive generations or art that the community loved
- Notable milestones, releases, or breakthroughs
- Interesting experiments or creative uses of AI tools

For each pick, write a short draft tweet (under 280 chars) that:
- Is enthusiastic but not hype-brained — sounds like a real person, not a brand account
- Credits the creator if there is one
- Explains why it's interesting in plain language
- Would make someone want to click through

Respond with exactly 3 picks in this exact format (no extra text):

PICK
Title: <exact title of the news item from the summary>
Draft: <tweet text>
Why: <1 sentence on why this is share-worthy>

PICK
Title: <exact title>
Draft: <tweet text>
Why: <1 sentence>

PICK
Title: <exact title>
Draft: <tweet text>
Why: <1 sentence>

If nothing stands out today, respond with just: NOTHING"""

    def _build_summary_lookup(self, enriched_summary: str) -> dict:
        """Build a lookup from news item titles to their media URLs and Discord links."""
        lookup = {}
        try:
            items = json.loads(enriched_summary) if isinstance(enriched_summary, str) else enriched_summary
            if not isinstance(items, list):
                return lookup

            thread_id_by_msg = resolve_thread_ids(
                self.db_handler,
                (it.get('message_id') for it in items),
            )

            for item in items:
                title = item.get('title', '').strip()
                if not title:
                    continue

                # Build Discord link to original message
                channel_id = item.get('channel_id')
                message_id = item.get('message_id')
                main_media_message_id = item.get('mainMediaMessageId')
                discord_link = None
                if self.guild_id and channel_id and message_id:
                    discord_link = message_jump_url(
                        self.guild_id,
                        int(channel_id),
                        int(message_id),
                        thread_id=thread_id_by_msg.get(int(message_id)),
                    )

                # Collect media URLs (videos first, then images)
                media_urls = []
                main_media = item.get('mainMediaUrls') or []
                for m in main_media:
                    if isinstance(m, dict) and m.get('url'):
                        media_urls.append(m)
                for sub in item.get('subTopics', []):
                    for sub_media_list in (sub.get('subTopicMediaUrls') or []):
                        if isinstance(sub_media_list, list):
                            for m in sub_media_list:
                                if isinstance(m, dict) and m.get('url'):
                                    media_urls.append(m)
                        elif isinstance(sub_media_list, dict) and sub_media_list.get('url'):
                            media_urls.append(sub_media_list)

                # Sort: videos first, then images
                media_urls.sort(key=lambda m: (0 if m.get('type', '').startswith('video') else 1))

                # The author we want to credit is whoever posted the actual
                # creative artifact (mainMediaMessageId), falling back to the
                # primary message_id if no media is attached.
                author_message_id = main_media_message_id or message_id

                lookup[title.lower()] = {
                    'discord_link': discord_link,
                    'all_media': media_urls,
                    'author_message_id': author_message_id,
                }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            self.logger.warning(f"[SocialPicks] Failed to build summary lookup: {e}")
        return lookup

    @staticmethod
    def _extract_twitter_handle(twitter_url_value: Optional[str]) -> Optional[str]:
        """Normalize a twitter_url value (URL, @handle, or plain text) to a bare handle (no @)."""
        if not twitter_url_value:
            return None
        val = twitter_url_value.strip()
        if not val:
            return None
        candidate: Optional[str] = None
        if val.startswith('@'):
            candidate = val[1:]
        elif any(d in val.lower() for d in ('twitter.com/', 'x.com/', '://')):
            path = val.split('://', 1)[-1] if '://' in val else val
            lower = path.lower()
            for marker in ('twitter.com/', 'x.com/'):
                idx = lower.find(marker)
                if idx != -1:
                    candidate = path[idx + len(marker):].split('/')[0]
                    break
        else:
            candidate = val
        if not candidate:
            return None
        candidate = candidate.split('?')[0].split('#')[0].lstrip('@').strip()
        return candidate or None

    async def _resolve_pick_author_handles(self, message_ids: List[int]) -> Dict[int, Dict]:
        """For a list of primary message_ids, return {message_id: {author_id, author_name, twitter_handle, twitter_raw}}.

        Best-effort: any DB error returns an empty dict so social picks still send.
        """
        result: Dict[int, Dict] = {}
        if not message_ids:
            return result
        try:
            messages = await asyncio.to_thread(
                self.db_handler.get_messages_by_ids, message_ids
            )
        except Exception as e:
            self.logger.warning(f"[SocialPicks] Failed to fetch messages for author lookup: {e}")
            return result

        msg_by_id = {int(m['message_id']): m for m in messages if m.get('message_id') is not None}
        unique_author_ids = list({int(m['author_id']) for m in messages if m.get('author_id') is not None})

        members_by_id: Dict[int, Dict] = {}
        for author_id in unique_author_ids:
            try:
                member = await asyncio.to_thread(self.db_handler.get_member, author_id)
                if member:
                    members_by_id[author_id] = member
            except Exception as e:
                self.logger.debug(f"[SocialPicks] get_member({author_id}) failed: {e}")

        for mid in message_ids:
            msg = msg_by_id.get(int(mid))
            if not msg:
                continue
            author_id = int(msg['author_id']) if msg.get('author_id') is not None else None
            member = members_by_id.get(author_id) if author_id else None
            twitter_raw = (member or {}).get('twitter_url')
            result[int(mid)] = {
                'author_id': author_id,
                'author_name': (msg.get('author_name')
                                or (member or {}).get('global_name')
                                or (member or {}).get('username')),
                'twitter_raw': twitter_raw,
                'twitter_handle': self._extract_twitter_handle(twitter_raw),
            }
        return result

    def _chunk_social_pick_assets(self, assets: List[Dict[str, str]], max_length: int = 1900) -> List[str]:
        """Split asset references into Discord-safe follow-up messages."""
        if not assets:
            return []

        asset_lines: List[str] = ["**Assets:**"]
        multiple_assets = len(assets) > 1

        for i, asset in enumerate(assets):
            url = asset.get('url')
            if not url:
                continue
            asset_type = asset.get('type', 'media')
            label = f"Asset {i + 1} ({asset_type})" if multiple_assets else f"Asset ({asset_type})"
            asset_lines.append(label)
            asset_lines.append(url)

        return self._chunk_text_for_discord("\n".join(asset_lines), max_length=max_length)

    @staticmethod
    def _chunk_text_for_discord(content: str, max_length: int = 1900) -> List[str]:
        """Split text into Discord-safe message chunks."""
        chunks: List[str] = []
        current_chunk = ""

        for line in content.split('\n'):
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) <= max_length:
                current_chunk = candidate
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = line

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    async def _send_social_picks_dm(self, enriched_summary=None, short_summary=None):
        """DM the admin with Claude-curated social picks, each as a separate message with media links."""
        self.logger.info("[SocialPicks] Starting social picks generation")
        try:
            admin_id = int(os.getenv('ADMIN_USER_ID', '0'))
            if not admin_id:
                self.logger.warning("[SocialPicks] No ADMIN_USER_ID set, skipping")
                return

            if not enriched_summary and not short_summary:
                self.logger.warning("[SocialPicks] No summary data available, skipping")
                return

            # Build context for Claude from whatever we have
            context = ""
            if short_summary:
                context += f"Short summary:\n{short_summary}\n\n"
            if enriched_summary:
                summary_text = enriched_summary if isinstance(enriched_summary, str) else json.dumps(enriched_summary)
                context += f"Full summary:\n{summary_text[:8000]}"

            self.logger.info("[SocialPicks] Calling Claude with %d chars of context", len(context))
            from src.common.llm import get_llm_response
            response = await get_llm_response(
                client_name="claude",
                model="claude-sonnet-4-5-20250929",
                system_prompt=self._SOCIAL_PICKS_PROMPT,
                messages=[{"role": "user", "content": context}],
                max_tokens=1000,
            )
            response = response.strip()
            self.logger.info("[SocialPicks] Claude responded (%d chars)", len(response))

            if response == "NOTHING":
                self.logger.info("[SocialPicks] Claude found nothing worth sharing today")
                return

            # Build lookup from enriched summary data
            lookup = self._build_summary_lookup(enriched_summary) if enriched_summary else {}
            self.logger.info("[SocialPicks] Built lookup with %d items", len(lookup))

            # Parse picks from Claude's response
            picks = response.split("PICK")
            admin_user = await self.bot.fetch_user(admin_id)
            pick_count = 0

            # First pass: parse picks and resolve their matches so we can
            # batch-fetch all author info in one round trip.
            parsed_picks = []
            for pick_text in picks:
                pick_text = pick_text.strip()
                if not pick_text:
                    continue
                title = ""
                draft = ""
                why = ""
                for line in pick_text.split("\n"):
                    line = line.strip()
                    if line.startswith("Title:"):
                        title = line[len("Title:"):].strip()
                    elif line.startswith("Draft:"):
                        draft = line[len("Draft:"):].strip()
                    elif line.startswith("Why:"):
                        why = line[len("Why:"):].strip()
                if not draft:
                    continue

                matched = {}
                if title:
                    title_key = title.lower()
                    matched = lookup.get(title_key, {})
                    if not matched:
                        title_words = set(title_key.split())
                        best_overlap, best_match = 0, {}
                        for key, val in lookup.items():
                            overlap = len(title_words & set(key.split()))
                            if overlap > best_overlap:
                                best_overlap, best_match = overlap, val
                        if best_overlap >= 3:
                            matched = best_match
                parsed_picks.append({
                    'title': title,
                    'draft': draft,
                    'why': why,
                    'matched': matched,
                })

            # Batch-resolve author + twitter handle for every pick's primary message
            author_msg_ids = [
                int(p['matched']['author_message_id'])
                for p in parsed_picks
                if p['matched'].get('author_message_id')
            ]
            author_info_by_msg = await self._resolve_pick_author_handles(author_msg_ids)
            self.logger.info(
                "[SocialPicks] Resolved author info for %d/%d picks",
                len(author_info_by_msg), len(parsed_picks),
            )

            sent_message_count = 0
            for parsed in parsed_picks:
                draft = parsed['draft']
                why = parsed['why']
                matched = parsed['matched']
                discord_link = matched.get('discord_link')
                all_media = matched.get('all_media', [])
                author_msg_id = matched.get('author_message_id')
                author_info = author_info_by_msg.get(int(author_msg_id)) if author_msg_id else None

                # If we have a known twitter handle for the creator and the
                # draft doesn't already mention it, append it so the admin
                # can post as-is.
                if author_info and author_info.get('twitter_handle'):
                    handle = author_info['twitter_handle']
                    if f"@{handle.lower()}" not in draft.lower():
                        candidate = f"{draft} (@{handle})"
                        if len(candidate) <= 280:
                            draft = candidate

                dm_parts = [f"**Draft:** {draft}"]
                if why:
                    dm_parts.append(f"**Why:** {why}")

                # Surface creator info so admin knows who to update if missing
                if author_info and author_info.get('author_id'):
                    name = author_info.get('author_name') or 'unknown'
                    handle = author_info.get('twitter_handle')
                    handle_str = f"@{handle}" if handle else "(no handle on file — DM the bot to set one)"
                    dm_parts.append(
                        f"**Creator:** {name} `{author_info['author_id']}` — {handle_str}"
                    )

                if discord_link:
                    dm_parts.append(f"**Original post:** {discord_link}")

                content = "\n".join(dm_parts)
                for chunk in self._chunk_text_for_discord(content, max_length=1900):
                    await admin_user.send(chunk)
                    sent_message_count += 1

                for asset_chunk in self._chunk_social_pick_assets(all_media):
                    await admin_user.send(asset_chunk)
                    sent_message_count += 1

                pick_count += 1

            self.logger.info(
                "[SocialPicks] Sent %d picks to admin across %d DM(s)",
                pick_count,
                sent_message_count,
            )

        except Exception as e:
            self.logger.error(f"[SocialPicks] Failed: {e}", exc_info=True)

    @handle_errors("generate_summary")
    async def generate_summary(self):
        """
        Legacy/backfill daily summary batch.

        This monolithic flow is kept available for explicit historical
        regeneration/backfill only. The live-update editor must not use this as
        active runtime storage or publishing.

        Generate and post summaries following these steps:
        1) Generate individual channel summaries and post to their channels (except for forum channels)
        2) Combine channel summaries for overall summary
        3) Post overall summary to summary channel
        4) Post top generations
        5) Post top art sharing
        """
        try:
            async with self.summary_lock:
                # Reset first_message so a stale reference from a previous run can't be used
                self.first_message = None
                self._today_summary = None
                self._today_short_summary = None

                # Check Discord connectivity before starting
                if not await self._check_discord_connectivity():
                    self.logger.error("Discord connectivity check failed. Aborting summary generation.")
                    return
                self.logger.info("Generating requested summary...")
                db_handler = self.db_handler 
                summary_channel = await self._get_channel_with_retry(self.summary_channel_id)
                if not summary_channel:
                    self.logger.error(f"Could not find summary channel {self.summary_channel_id}")
                    return

                current_date = datetime.utcnow()

                # We'll handle channel picking ourselves:
                self.logger.info(f"════════════════════════════════════════")
                self.logger.info(f"Running in {'DEV' if self.dev_mode else 'PRODUCTION'} mode")
                self.logger.info(f"════════════════════════════════════════")
                if self.dev_mode:
                    active_channels = await self._get_dev_mode_channels(db_handler)
                    self.logger.info(f"════════════════════════════════════════")
                    self.logger.info(f"✅ _get_dev_mode_channels returned {len(active_channels) if active_channels else 0} channels")
                    self.logger.info(f"════════════════════════════════════════")
                else:
                    active_channels = await self._get_production_channels(db_handler)
                    self.logger.info(f"✅ _get_production_channels returned {len(active_channels) if active_channels else 0} channels")
                
                if not active_channels:
                    self.logger.info("No active channels found")
                    # Send a message to summary_channel if no active channels found
                    await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content="_No active channels with sufficient messages found to summarize._")
                    return

                channel_summaries = []

                # --combine-only: skip channel processing, load existing summaries from DB
                combine_only = getattr(self.bot, 'combine_only', False)
                if combine_only:
                    self.logger.info(f"--combine-only: Loading existing channel summaries from DB for {len(active_channels)} channels...")
                    for channel_info in active_channels:
                        channel_id = channel_info['channel_id']
                        existing_summary = db_handler.get_summary_for_date(channel_id, current_date)
                        if existing_summary:
                            channel_summaries.append(existing_summary)
                            self.logger.info(f"  Loaded summary for channel {channel_id} ({channel_info.get('channel_name', 'Unknown')})")
                        else:
                            self.logger.info(f"  No existing summary for channel {channel_id} ({channel_info.get('channel_name', 'Unknown')}), skipping")
                    self.logger.info(f"--combine-only: Loaded {len(channel_summaries)} channel summaries from DB")
                else:
                    self.logger.info(f"Processing {len(active_channels)} channel{'s' if len(active_channels) != 1 else ''} with 25+ messages")

                for i, channel_info in enumerate([] if combine_only else active_channels):
                    channel_id = channel_info['channel_id']
                    channel_name = channel_info.get('channel_name', 'Unknown')
                    post_channel_id = channel_info.get('post_channel_id', channel_id)
                    
                    # Add delay between channels to respect Anthropic API rate limits (30k tokens/min)
                    if i > 0:
                        self.logger.info(f"Waiting 60s before processing next channel to respect rate limits...")
                        await asyncio.sleep(60)
                    
                    self.logger.debug(f"[{i+1}/{len(active_channels)}] Processing channel {channel_id} ({channel_name})")
                    
                    # Check if summary already exists for this channel today (skip in dev mode)
                    if not self.dev_mode:
                        existing_summary = db_handler.get_summary_for_date(channel_id, current_date)
                        if existing_summary:
                            self.logger.info(f"⏭️ Using existing summary for channel {channel_id} from {current_date.strftime('%Y-%m-%d')}")
                            channel_summaries.append(existing_summary)
                            continue
                    
                    try:
                        self.logger.info(f"Getting message history for channel {channel_id} from last 24 hours...")
                        messages = await self.get_channel_history(channel_id)
                        self.logger.info(f"Retrieved {len(messages) if messages else 0} messages for channel {channel_id} from last 24 hours")
                        
                        if not messages or len(messages) < 25:
                             self.logger.info(f"⚠️ Skipping channel {channel_id}: Not enough messages from last 24 hours ({len(messages) if messages else 0}/25 required).")
                             continue
                        
                        self.logger.info(f"Generating news summary for channel {channel_id} with {len(messages)} messages...")
                        channel_summary = await self.news_summarizer.generate_news_summary(messages)
                        self.logger.info(f"News summary generated for channel {channel_id}. Length: {len(channel_summary) if channel_summary else 0} chars")
                        if not channel_summary or channel_summary in ["[NOTHING OF NOTE]", "[NO SIGNIFICANT NEWS]", "[NO MESSAGES TO ANALYZE]"]:
                            self.logger.info(f"No significant news for channel {channel_id}.")
                            continue
                        
                        # Verify summary accuracy using GPT-5 high reasoning
                        self.logger.info(f"Verifying summary accuracy for channel {channel_id}...")
                        channel_summary = await self.news_summarizer.verify_summary_accuracy(channel_summary, messages)
                        self.logger.info(f"Summary verification complete for channel {channel_id}. Length: {len(channel_summary) if channel_summary else 0} chars")
                        
                        if self.is_forum_channel(post_channel_id):
                            # Skip forum channels - don't post updates to them
                            self.logger.info(f"Skipping ForumChannel {post_channel_id} for channel {channel_id} - forum channels are not supported for summary posting")
                        else:
                            channel_obj = await self._get_channel_with_retry(post_channel_id)
                            if channel_obj:
                                # Filter out blocked content before formatting
                                channel_summary = await filter_summary_media(channel_summary, self._fetch_message_for_moderation)
                                formatted_summary = self.news_summarizer.format_news_for_discord(channel_summary, db_handler=self.db_handler)
                                thread = None
                                if not thread:
                                    self.logger.info(f"Creating new summary thread for channel {channel_id}...")
                                    thread_title = f"#{channel_obj.name} - Summary - {current_date.strftime('%B %d, %Y')}"
                                    try:
                                        # UPDATED CALL with network error handling
                                        summary_message_starter = await discord_utils.safe_send_message(self.bot, channel_obj, self.rate_limiter, self.logger, content=f"Summary thread for {current_date.strftime('%B %d, %Y')}")
                                        if summary_message_starter: # check if message was sent
                                            thread = await self.create_summary_thread(summary_message_starter, thread_title) # create_summary_thread is a method of self
                                            if thread:
                                                 self.logger.info(f"Successfully created summary thread for channel {channel_id}: {thread.id}")
                                            else:
                                                 self.logger.error(f"Failed to create thread for channel {channel_id}")
                                        else:
                                             self.logger.error(f"Failed to send header message to channel {channel_id} for thread creation")
                                    except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as network_error:
                                        self.logger.warning(f"Network error creating summary thread for channel {channel_id}: {network_error}. Skipping channel summary posting.")
                                        # Still add to channel_summaries for overall summary
                                        channel_summaries.append({
                                            'channel_id': channel_id,
                                            'summary': channel_summary,
                                            'message_count': len(messages)
                                        })
                                        continue
                                
                                if thread:
                                    self.logger.info(f"Using summary thread in channel {post_channel_id}: {thread.id}")
                                    date_headline = f"# {current_date.strftime('%A, %B %d, %Y')}\n"
                                    # UPDATED CALL
                                    header_msg = await discord_utils.safe_send_message(self.bot, thread, self.rate_limiter, self.logger, content=date_headline)
                                    await asyncio.sleep(1)
                                    
                                    # Track posted message IDs by topic index for channel summary
                                    channel_posted_by_topic: Dict[int, List[int]] = {}
                                    
                                    for item in formatted_summary:
                                        topic_index = item.get('topic_index')
                                        
                                        # Initialize topic tracking if needed
                                        if topic_index is not None and topic_index not in channel_posted_by_topic:
                                            channel_posted_by_topic[topic_index] = []
                                        
                                        if item.get('type') in ['media_reference', 'media_reference_group']:
                                            try:
                                                source_channel_id_media = int(item['channel_id'])
                                                source_channel_media = await self._get_channel_with_retry(source_channel_id_media)
                                                if source_channel_media:
                                                    # Unified handler for both single and group references
                                                    msg_ids = [item['message_id']] if item.get('type') == 'media_reference' else item.get('message_ids', [])
                                                    sent_messages = await self._send_media_group(thread, source_channel_media, msg_ids)
                                                    # Track sent media message IDs
                                                    if sent_messages and topic_index is not None:
                                                        for sent_msg in sent_messages:
                                                            if sent_msg:
                                                                channel_posted_by_topic[topic_index].append(sent_msg.id)
                                            except Exception as e_media:
                                                self.logger.error(f"Error processing media reference {item}: {e_media}")
                                        else:
                                            try:
                                                # UPDATED CALL - capture returned message
                                                sent_msg = await discord_utils.safe_send_message(self.bot, thread, self.rate_limiter, self.logger, content=item.get('content', ''))
                                                if sent_msg and topic_index is not None:
                                                    channel_posted_by_topic[topic_index].append(sent_msg.id)
                                                await asyncio.sleep(1)
                                            except Exception as e_send:
                                                self.logger.error(f"Failed to send channel summary item to thread (topic {topic_index}): {e_send}")
                                                continue
                                    
                                    # Enrich channel summary with posted message IDs
                                    if channel_posted_by_topic:
                                        channel_summary = self._enrich_summary_with_posted_ids(channel_summary, channel_posted_by_topic)
                                        self.logger.info(f"Enriched channel {channel_id} summary with posted message IDs for {len(channel_posted_by_topic)} topics")
                                    
                                    await self.top_generations.post_top_gens_for_channel(thread, channel_id)
                                    if header_msg:
                                         short_summary_text = await self.news_summarizer.generate_short_summary(channel_summary, len(messages))
                                         link = f"https://discord.com/channels/{channel_obj.guild.id}/{thread.id}/{header_msg.id}"
                                         # UPDATED CALL
                                         await discord_utils.safe_send_message(self.bot, thread, self.rate_limiter, self.logger, content=f"\n---\n\n***Click here to jump to the beginning of today's summary:*** {link}")
                                         channel_header = f"### Channel summary for {current_date.strftime('%A, %B %d, %Y')}"
                                         # UPDATED CALL
                                         await discord_utils.safe_send_message(self.bot, channel_obj, self.rate_limiter, self.logger, content=f"{channel_header}\n{short_summary_text}\n[Click here to jump to the summary thread]({link})")
                        
                        # Save channel summary to DB (with dev_mode flag if in dev mode)
                        # Note: channel_summary now includes posted_message_ids if posting occurred
                        success = await self._post_summary_with_transaction(
                            channel_id, channel_summary, messages, current_date, db_handler, 
                            dev_mode=self.dev_mode
                        )
                        if success: 
                            channel_summaries.append(channel_summary)
                        else: 
                            self.logger.error(f"Failed to save summary to DB for channel {channel_id}")

                    except Exception as e:
                        self.logger.error(f"Error processing channel {channel_id}: {e}", exc_info=True)
                        continue

                if channel_summaries:
                    # Check if main summary already exists for today (skip in dev mode, skip if --combine-only)
                    if not combine_only and not self.dev_mode and db_handler.summary_exists_for_date(self.summary_channel_id, current_date):
                        self.logger.info(f"⏭️ Skipping main summary: Already exists for {current_date.strftime('%Y-%m-%d')}")
                    else:
                        self.logger.info(f"Combining summaries from {len(channel_summaries)} channels...")
                        overall_summary = await self.news_summarizer.combine_channel_summaries(channel_summaries)
                        
                        # List of non-content responses to skip posting
                        skip_responses = [
                            "[NOTHING OF NOTE]",
                            "[NO SIGNIFICANT NEWS]",
                            "[NO MESSAGES TO ANALYZE]"
                        ]

                        if overall_summary and overall_summary.startswith("[ERROR"):
                            self.logger.error(f"Combined summary returned error: {overall_summary}")
                            # Notify admin via DM instead of posting error to summary channel
                            try:
                                admin_id = int(os.getenv("ADMIN_USER_ID", "0"))
                                if admin_id:
                                    admin_user = await self.bot.fetch_user(admin_id)
                                    await admin_user.send(f"Summary error: `{overall_summary}` — channel summaries were generated but the combine step failed.")
                            except Exception as e_admin:
                                self.logger.warning(f"Failed to DM admin about combine error: {e_admin}")
                        elif overall_summary and overall_summary not in skip_responses:
                            # Filter out blocked content before formatting
                            overall_summary = await filter_summary_media(overall_summary, self._fetch_message_for_moderation)
                            formatted_summary = self.news_summarizer.format_news_for_discord(overall_summary, db_handler=self.db_handler)
                            # UPDATED CALL — wrapped so a header failure doesn't abort the entire summary
                            try:
                                header = await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content=f"\n\n# Daily Summary - {current_date.strftime('%A, %B %d, %Y')}\n\n")
                                if header is not None: self.first_message = header
                                else: self.logger.error("Failed to post header message; first_message remains unset.")
                            except Exception as e_header:
                                self.logger.error(f"Failed to send summary header: {e_header}")
                                header = None
                            
                            self.logger.info("Posting main summary to summary channel")
                            # Track posted message IDs by topic index
                            posted_by_topic: Dict[int, List[int]] = {}  # topic_index -> [message_ids]
                            
                            for item in formatted_summary:
                                topic_index = item.get('topic_index')
                                
                                # Initialize topic tracking if needed
                                if topic_index is not None and topic_index not in posted_by_topic:
                                    posted_by_topic[topic_index] = []
                                
                                if item.get('type') in ['media_reference', 'media_reference_group']:
                                    try:
                                        source_channel_id_media_main = int(item['channel_id'])
                                        source_channel_media_main = await self._get_channel_with_retry(source_channel_id_media_main)
                                        if source_channel_media_main:
                                            # Unified handler for both single and group references
                                            msg_ids = [item['message_id']] if item.get('type') == 'media_reference' else item.get('message_ids', [])
                                            sent_messages = await self._send_media_group(summary_channel, source_channel_media_main, msg_ids)
                                            # Track sent media message IDs
                                            if sent_messages and topic_index is not None:
                                                for sent_msg in sent_messages:
                                                    if sent_msg:
                                                        posted_by_topic[topic_index].append(sent_msg.id)
                                    except Exception as e_media_main:
                                        self.logger.error(f"Error processing media reference in main summary {item}: {e_media_main}")
                                else:
                                    try:
                                        # UPDATED CALL - capture returned message
                                        sent_msg = await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content=item.get('content', ''))
                                        if sent_msg and topic_index is not None:
                                            posted_by_topic[topic_index].append(sent_msg.id)
                                        await asyncio.sleep(1)
                                    except Exception as e_send_main:
                                        self.logger.error(f"Failed to send main summary item (topic {topic_index}): {e_send_main}")
                                        continue
                            
                            self.logger.info(f"Tracked posted message IDs for {len(posted_by_topic)} topics")
                            
                            # Extract message_ids grouped by channel from the combined summary
                            channel_message_ids = self._extract_message_ids_by_channel(overall_summary)
                            # Persist media to Supabase Storage (runs in both dev and prod mode)
                            self.logger.info("Persisting media attachments to Supabase Storage...")
                            media_urls = await self._persist_summary_media(
                                overall_summary, current_date, db_handler
                            )
                            
                            # Enrich the summary JSON with embedded media URLs for each item
                            if media_urls:
                                enriched_summary = self._enrich_summary_with_media_urls(overall_summary, media_urls)
                                self.logger.info(f"Enriched summary with {len(media_urls)} media URL mappings")
                            else:
                                enriched_summary = overall_summary
                            
                            # Enrich the summary JSON with posted Discord message IDs per topic
                            if posted_by_topic:
                                enriched_summary = self._enrich_summary_with_posted_ids(enriched_summary, posted_by_topic)
                                self.logger.info(f"Enriched summary with posted message IDs for {len(posted_by_topic)} topics")
                            
                            # Save main summary to database (with dev_mode flag if in dev mode)
                            short_summary = await self.news_summarizer.generate_short_summary(overall_summary, 0)
                            self._today_summary = overall_summary
                            self._today_enriched_summary = enriched_summary
                            self._today_short_summary = short_summary
                            main_summary_saved = await asyncio.to_thread(
                                db_handler.store_daily_summary,
                                self.summary_channel_id,
                                enriched_summary,  # Save the enriched version with embedded media URLs and posted IDs
                                short_summary,
                                current_date,
                                False,  # included_in_main_summary (N/A for main summary itself)
                                self.dev_mode,  # dev_mode flag
                                guild_id=self.guild_id
                            )
                            
                            if main_summary_saved:
                                self.logger.info(f"✅ Main summary saved to database for {current_date.strftime('%Y-%m-%d')} (dev_mode={self.dev_mode})")
                                
                                # Mark channel summaries as included in main summary
                                if channel_message_ids:
                                    mark_success = await asyncio.to_thread(
                                        db_handler.mark_summaries_included_in_main,
                                        current_date,
                                        channel_message_ids
                                    )
                                    if mark_success:
                                        self.logger.info(f"✅ Marked {len(channel_message_ids)} channel summaries as included in main summary")
                                    else:
                                        self.logger.warning(f"Failed to mark some channel summaries as included")
                                
                                # Update channel summaries with inclusion flags and media URLs
                                self.logger.info("Updating channel summaries with inclusion flags and media URLs...")
                                enrich_success = await self._update_channel_summaries_with_inclusion(
                                    overall_summary,
                                    media_urls if media_urls else {},
                                    current_date,
                                    db_handler
                                )
                                if enrich_success:
                                    self.logger.info(f"✅ Channel summaries enriched with inclusion flags and media URLs")
                                else:
                                    self.logger.warning(f"Failed to enrich some channel summaries")
                            else:
                                self.logger.error(f"Failed to save main summary to database")
                        else:
                            # UPDATED CALL
                            await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content="_No significant activity to summarize in the last 24 hours._")
                else:
                    try:
                        # UPDATED CALL with network error handling
                        await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content="_No messages found in the last 24 hours for overall summary._")
                    except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as network_error:
                        self.logger.error(f"Network error sending fallback message to summary channel: {network_error}")
                        # Don't re-raise - this is just a fallback message

                # Post top generations: thread to summary channel, one-by-one to top_gens channel
                self.logger.info(f"Posting Top Generations thread to summary channel, individual posts to top_gens channel (ID: {self.top_gens_channel_id})")
                await self.top_generations.post_top_x_generations(summary_channel, limit=20, also_post_to_channel_id=self.top_gens_channel_id)
                await self.top_art_sharer.post_top_art_share(summary_channel)

                # Post new speakers welcome section
                await self._post_new_speakers(summary_channel)

                self.logger.info("Attempting to send link back to start...")
                if self.first_message:
                    link_to_start = self.first_message.jump_url
                    # UPDATED CALL
                    await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content=f"\n---\n\n***Click here to jump to the beginning of today's summary:*** {link_to_start}")
                else:
                    self.logger.warning("No first_message found, cannot send link back")

                # Send social picks DM to admin (use enriched summary for media URLs)
                await self._send_social_picks_dm(
                    getattr(self, '_today_enriched_summary', None) or getattr(self, '_today_summary', None),
                    getattr(self, '_today_short_summary', None),
                )

        except Exception as e:
            self.logger.error(f"Critical error in summary generation: {e}", exc_info=True)
            if summary_channel: # Check if summary_channel was successfully fetched
                try:
                    # UPDATED CALL
                    await discord_utils.safe_send_message(self.bot, summary_channel, self.rate_limiter, self.logger, content=f"⚠️ Critical error during summary generation: {str(e)[:500]}") # Truncate error for safety
                except Exception: pass
        finally:
            if self.summary_lock.locked(): self.summary_lock.release()

    # --- Utility and Helper Methods ---
    def _get_today_str(self):
        """Return today's date as a formatted string"""
        from datetime import datetime
        return datetime.utcnow().strftime("%B %d, %Y")

# --- Main Execution / Test Block --- 
if __name__ == "__main__":
    # This block is likely for testing and might not be needed for production Cog use
    def main():
        print("This script is intended to be run as part of a Discord bot Cog.")
        # Example Test Initialization (requires .env)
        # load_dotenv()
        # logger = logging.getLogger('TestSummarizer')
        # logging.basicConfig(level=logging.INFO)
        # test_summarizer = ChannelSummarizer(logger=logger, dev_mode=True)
        # # Mock sharer for testing
        # class MockSharer:
        #     def initiate_sharing_process_from_summary(self, msg): pass
        # test_summarizer.sharer_instance = MockSharer()
        # # Run test logic (e.g., generate summary for dev channels)
        # asyncio.run(test_summarizer.generate_summary())
        pass
    main()
