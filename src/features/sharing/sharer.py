# Placeholder for Sharer class 

import discord
import logging
import os
import aiohttp
import asyncio # Added
from pathlib import Path
from typing import List, Dict, Optional, Tuple # Added Tuple
import mimetypes # For inferring content type from URL

from src.common.db_handler import DatabaseHandler
# Remove old client import
# from src.common.claude_client import ClaudeClient 
# Import the dispatcher
from src.common.llm import get_llm_response
from .subfeatures.notify_user import send_post_share_notification
from .models import PublicationSourceContext, SocialPublishRequest
from .social_publish_service import SocialPublishService
from .subfeatures.social_poster import generate_media_title
from src.common import discord_utils # Ensure this is imported
from .live_update_social.helpers import download_media_url as _shared_download_media_url

logger = logging.getLogger('DiscordBot')


class Sharer:
    def __init__(self, bot: discord.Client, db_handler: DatabaseHandler, logger_instance: logging.Logger):
        self.bot = bot
        self.db_handler = db_handler
        self.logger = logger_instance
        self.social_publish_service = (
            getattr(bot, 'social_publish_service', None)
            or SocialPublishService(db_handler=self.db_handler, logger_instance=self.logger)
        )
        self.temp_dir = Path("./temp_media_sharing")
        self.temp_dir.mkdir(exist_ok=True)
        self._processing_lock = asyncio.Lock()
        self._currently_processing = set()
        self._posted_to_summary = set()  # Track messages already posted to summary channels

    async def _download_attachment(self, attachment: discord.Attachment) -> Optional[Dict]:
        """Downloads a single discord.Attachment to the temporary directory."""
        # Keep original filename but prefix with ID for uniqueness
        # Sanitize filename slightly to avoid issues, though discord.Attachment.filename should be reasonable
        safe_filename = "".join(c if c.isalnum() or c in ('.', '_', '-') else '_' for c in attachment.filename)
        save_path = self.temp_dir / f"{attachment.id}_{safe_filename}"
        try:
            await attachment.save(save_path) # discord.Attachment has a save method
            self.logger.info(f"Successfully downloaded attachment using discord.Attachment.save: {save_path}")
            return {
                'url': attachment.url,
                'filename': attachment.filename, # Original filename for display/metadata
                'content_type': attachment.content_type,
                'size': attachment.size,
                'id': attachment.id,
                'local_path': str(save_path) # Store local path
            }
        except Exception as e:
            self.logger.error(f"Error downloading attachment {attachment.url} using discord.Attachment.save: {e}", exc_info=True)
            # Fallback to aiohttp if .save() fails (e.g. if it's not available or has issues)
            self.logger.info(f"Falling back to aiohttp download for {attachment.url}")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            with open(save_path, 'wb') as f:
                                f.write(await resp.read())
                            self.logger.info(f"Successfully downloaded attachment via aiohttp fallback: {save_path}")
                            return {
                                'url': attachment.url,
                                'filename': attachment.filename,
                                'content_type': attachment.content_type,
                                'size': attachment.size,
                                'id': attachment.id,
                                'local_path': str(save_path)
                            }
                        else:
                            self.logger.error(f"AIOHTTP fallback failed to download {attachment.url}. Status: {resp.status}")
                            return None
            except Exception as e2:
                self.logger.error(f"Error during aiohttp fallback download for {attachment.url}: {e2}", exc_info=True)
                return None

    async def _download_media_from_url(self, url: str, message_id: str, item_index: int) -> Optional[Dict]:
        """Download media from a direct URL (delegates to shared helper).

        Kept as a backward-compatible wrapper for existing callers
        (tweet_sharer_bridge, admin-chat tools).  New callers should use
        ``live_update_social.helpers.download_media_url`` directly.
        """
        return await _shared_download_media_url(
            url,
            dest_dir=str(self.temp_dir),
            filename_prefix=f"tweet_{message_id}_{item_index}",
        )

    def _resolve_source_kind(
        self,
        summary_channel: Optional[discord.TextChannel] = None,
        tweet_text: Optional[str] = None,
        in_reply_to_tweet_id: Optional[str] = None,
        explicit_source_kind: Optional[str] = None,
    ) -> str:
        if explicit_source_kind:
            return explicit_source_kind
        if summary_channel is not None:
            return 'summary'
        if tweet_text is not None or in_reply_to_tweet_id is not None:
            return 'admin_chat'
        return 'reaction_auto'

    def _resolve_message_channel_id(self, message_id: int) -> Optional[int]:
        try:
            messages = self.db_handler.get_messages_by_ids([message_id])
            if messages:
                return messages[0].get('channel_id')
        except Exception as e:
            self.logger.warning(f"Failed to resolve channel_id for message {message_id}: {e}")
        return None

    def _should_check_duplicate(self, source_kind: str, action: str) -> bool:
        return action == 'post' and source_kind != 'reaction_bridge'

    def _find_existing_publication(
        self,
        message_id: int,
        guild_id: Optional[int],
        platform: str,
        action: str,
        source_kind: str,
    ) -> Optional[Dict]:
        if guild_id is None or not self._should_check_duplicate(source_kind, action):
            return None

        publications = self.db_handler.get_social_publications_for_message(
            message_id=message_id,
            guild_id=guild_id,
            platform=platform,
            action=action,
            status='succeeded',
        )
        for publication in publications:
            if publication.get('deleted_at'):
                continue
            if publication.get('source_kind') == 'reaction_bridge':
                continue
            return publication
        return None

    def _build_publish_request(
        self,
        *,
        message_id: int,
        channel_id: int,
        guild_id: Optional[int],
        user_id: int,
        platform: str,
        action: str,
        text: Optional[str],
        media_hints: List[Dict],
        source_kind: str,
        target_post_ref: Optional[str],
        text_only: bool,
        author_display_name: Optional[str],
        original_message_content: Optional[str],
        original_message_jump_url: Optional[str],
        user_details: Dict,
        duplicate_policy: Optional[Dict] = None,
        announce_policy: Optional[Dict] = None,
        legacy_shared_post_policy: Optional[Dict] = None,
        first_share_notification_policy: Optional[Dict] = None,
        moderation_metadata: Optional[Dict] = None,
        consent_metadata: Optional[Dict] = None,
    ) -> SocialPublishRequest:
        return SocialPublishRequest(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id or 0,
            user_id=user_id,
            platform=platform,
            action=action,
            target_post_ref=target_post_ref,
            text=text,
            media_hints=media_hints,
            source_kind=source_kind,
            duplicate_policy=duplicate_policy or {
                'check_existing': self._should_check_duplicate(source_kind, action),
            },
            text_only=text_only,
            announce_policy=announce_policy or {
                'enabled': True,
                'author_display_name': author_display_name,
                'original_message_jump_url': original_message_jump_url,
            },
            first_share_notification_policy=first_share_notification_policy or {
                'enabled': source_kind != 'reaction_bridge',
            },
            legacy_shared_post_policy=legacy_shared_post_policy or {
                'enabled': action == 'post',
                'delete_eligible_hours': 6,
            },
            moderation_metadata=moderation_metadata or {},
            consent_metadata=consent_metadata or {},
            source_context=PublicationSourceContext(
                source_kind=source_kind,
                metadata={
                    'user_details': user_details,
                    'original_content': original_message_content,
                    'author_display_name': author_display_name,
                    'original_message_jump_url': original_message_jump_url,
                    'guild_id': guild_id,
                },
            ),
        )

    async def send_tweet(
        self,
        content: str, 
        image_urls: Optional[List[str]], 
        message_id: str, # Original Discord message ID for context/logging
        user_id: int,      # Original Discord user ID for fetching details
        author_display_name: str, # NEW parameter for display name
        original_message_content: Optional[str] = None, # Original Discord message text
        original_message_jump_url: Optional[str] = None,
        guild_id: Optional[int] = None,
        in_reply_to_tweet_id: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """Prepares data and publishes through the unified social publish service."""
        self.logger.info(f"Sharer.send_tweet called for message_id {message_id} by user_id {user_id} with content: '{content[:50]}...'")

        user_details = self.db_handler.get_member(user_id)
        if not user_details:
            self.logger.error(f"Sharer.send_tweet: User {user_id} not found in DB. Cannot post tweet.")
            return False, None

        downloaded_media_for_tweet = []
        temp_files_to_clean = []

        if image_urls:
            self.logger.info(f"Sharer.send_tweet: Processing {len(image_urls)} image_urls for message_id {message_id}.")
            for i, url in enumerate(image_urls):
                downloaded_item = await _shared_download_media_url(
                    url,
                    dest_dir=str(self.temp_dir),
                    filename_prefix=f"tweet_{message_id}_{i}",
                )
                if downloaded_item and downloaded_item.get('local_path'):
                    downloaded_media_for_tweet.append(downloaded_item)
                    temp_files_to_clean.append(downloaded_item['local_path'])
                else:
                    self.logger.warning(f"Sharer.send_tweet: Failed to download media from URL: {url} for message_id {message_id}")
        
        if not downloaded_media_for_tweet and image_urls:
            self.logger.error(f"Sharer.send_tweet: All media downloads failed for message_id {message_id}. Cannot post tweet.")
            self._cleanup_files(temp_files_to_clean) # Clean up any partial downloads
            return False, None
        
        if not downloaded_media_for_tweet:
            self.logger.info(
                f"Sharer.send_tweet: No media prepared for message_id {message_id}. Proceeding with a text-only tweet."
            )

        request = self._build_publish_request(
            message_id=int(message_id),
            channel_id=self._resolve_message_channel_id(int(message_id)) or 0,
            guild_id=guild_id,
            user_id=user_id,
            platform='twitter',
            action='reply' if in_reply_to_tweet_id else 'post',
            text=content,
            media_hints=downloaded_media_for_tweet,
            source_kind='reaction_bridge',
            target_post_ref=in_reply_to_tweet_id,
            text_only=not downloaded_media_for_tweet,
            author_display_name=author_display_name,
            original_message_content=original_message_content,
            original_message_jump_url=original_message_jump_url,
            user_details=user_details,
            duplicate_policy={'check_existing': False},
            first_share_notification_policy={'enabled': False},
            legacy_shared_post_policy={'enabled': not bool(in_reply_to_tweet_id), 'delete_eligible_hours': 6},
        )
        self.logger.info(
            f"Sharer.send_tweet: Calling SocialPublishService.publish_now for message_id {message_id} with {len(downloaded_media_for_tweet)} media items."
        )
        publish_result = await self.social_publish_service.publish_now(request)

        self._cleanup_files(temp_files_to_clean)

        if publish_result.success:
            tweet_url = publish_result.tweet_url
            self.logger.info(f"Sharer.send_tweet: Successfully posted tweet for message_id {message_id}. URL: {tweet_url}")
            if tweet_url:
                await self._announce_tweet_url(
                    tweet_url=tweet_url, 
                    author_display_name=author_display_name, 
                    original_message_jump_url=original_message_jump_url, 
                    context_message_id=message_id,
                    guild_id=guild_id,
                    is_reply=bool(in_reply_to_tweet_id)
                )

            # Mark first share (for tracking) but don't send notification here
            # tweet_sharer_bridge has its own consent flow
            self.db_handler.mark_member_first_shared(user_id, guild_id=guild_id)

            return True, tweet_url
        else:
            self.logger.error(
                f"Sharer.send_tweet: Failed to post tweet for message_id {message_id}. Error: {publish_result.error}"
            )
            return False, None

    # Renamed original function for clarity
    async def initiate_sharing_process_from_reaction(self, reaction: discord.Reaction, user: discord.User):
        """Starts the sharing process via reaction. Checks opt-out and proceeds directly (no pre-share DM)."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_reaction called. Triggering User ID: {user.id}, Emoji: {reaction.emoji}, Message ID: {reaction.message.id}, Message Author ID: {reaction.message.author.id}")
        message = reaction.message
        author = message.author

        # Fetch user details to check for opt-out
        user_details = self.db_handler.get_member(author.id)

        # Check for explicit opt-out (allow_content_sharing = False)
        if user_details and user_details.get('allow_content_sharing') is False:
            self.logger.info(f"User {author.id} (message author) has opted out of content sharing (allow_content_sharing=False). Skipping.")
            return

        # Proceed directly to sharing (no pre-share DM - notification comes after)
        self.logger.info(f"Initiating sharing for message {message.id} triggered by {user.id} reacting with {reaction.emoji}.")
        if message.channel:
            asyncio.create_task(
                self.finalize_sharing(
                    author.id,
                    message.id,
                    message.channel.id,
                    summary_channel=None,
                    source_kind='reaction_auto',
                )
            )
        else:
            self.logger.warning(f"Cannot finalize sharing for message {message.id} as message.channel is not available.")

    # Added new function to initiate from summary/message object
    async def initiate_sharing_process_from_summary(self, message: discord.Message, summary_channel: Optional[discord.TextChannel] = None):
        """Starts the sharing process directly from a message object. Checks opt-out and proceeds directly (no pre-share DM)."""
        self.logger.debug(f"[Sharer] initiate_sharing_process_from_summary called. Message ID: {message.id}, Author ID: {message.author.id}, Summary Channel: {summary_channel.id if summary_channel else 'None'}")
        author = message.author
        # Check if author is a bot before proceeding
        if author.bot:
            self.logger.warning(f"Attempted to initiate sharing for a bot message ({message.id}). Skipping.")
            return
            
        # Fetch user details to check for opt-out
        user_details = self.db_handler.get_member(author.id)

        # Check for explicit opt-out (allow_content_sharing = False)
        if user_details and user_details.get('allow_content_sharing') is False:
            self.logger.info(f"User {author.id} has opted out of content sharing (allow_content_sharing=False). Skipping summary share for message {message.id}.")
            return

        # Proceed directly to sharing (no pre-share DM - notification comes after)
        self.logger.info(f"Initiating sharing for message {message.id} requested via summary.")
        if message.channel:
            asyncio.create_task(
                self.finalize_sharing(
                    author.id,
                    message.id,
                    message.channel.id,
                    summary_channel=summary_channel,
                    source_kind='summary',
                )
            )
        else:
            self.logger.warning(f"Cannot finalize sharing for message {message.id} (triggered by summary) as message.channel is not available.")

    async def finalize_sharing(
        self,
        user_id: int,
        message_id: int,
        channel_id: int,
        summary_channel: Optional[discord.TextChannel] = None,
        tweet_text: Optional[str] = None,
        in_reply_to_tweet_id: Optional[str] = None,
        text_only: bool = False,
        source_kind: Optional[str] = None,
    ) -> Dict[str, object]:
        """
        Finalizes the sharing process after receiving consent. 
        This function now acts as the central point for all sharing activities for a given message.
        It is responsible for fetching content, generating descriptions, and posting to all configured platforms.
        """
        # Acquire lock and hold it for the entire operation to prevent concurrent processing
        async with self._processing_lock:
            if message_id in self._currently_processing:
                self.logger.warning(f"Sharing for message {message_id} is already in progress. Aborting.")
                return {'success': False, 'error': 'Sharing already in progress', 'message_id': message_id}
            self._currently_processing.add(message_id)

            try:
                # Step 2: Fetch the original message
                message = await self._fetch_message(channel_id, message_id)
                if not message:
                    self.logger.error(f"Failed to fetch message {message_id} in finalize_sharing. Aborting.")
                    return {'success': False, 'error': 'Failed to fetch message', 'message_id': message_id}

                # Step 3: Download attachments (skip entirely for text-only posts,
                # e.g. follow-up replies in a thread that shouldn't reattach the
                # source message's media).
                downloaded_attachments = []
                if text_only:
                    self.logger.info(f"text_only=True for message {message_id}; skipping attachment download.")
                else:
                    for attachment in message.attachments:
                        downloaded_item = await self._download_attachment(attachment)
                        if downloaded_item:
                            downloaded_attachments.append(downloaded_item)

                    if not downloaded_attachments:
                        self.logger.warning(f"No attachments could be downloaded for message {message_id}. Sharing might fail for platforms requiring media.")
                
                # Step 4: Get author details
                user_details = self.db_handler.get_member(user_id)
                if not user_details:
                    self.logger.error(f"Failed to get user details for user {user_id}. Aborting.")
                    return {'success': False, 'error': 'Failed to get user details', 'message_id': message_id}

                resolved_source_kind = self._resolve_source_kind(
                    summary_channel=summary_channel,
                    tweet_text=tweet_text,
                    in_reply_to_tweet_id=in_reply_to_tweet_id,
                    explicit_source_kind=source_kind,
                )
                action = 'reply' if in_reply_to_tweet_id else 'post'
                existing_publication = self._find_existing_publication(
                    message_id=message_id,
                    guild_id=getattr(message.guild, 'id', None),
                    platform='twitter',
                    action=action,
                    source_kind=resolved_source_kind,
                )
                if existing_publication:
                    self.logger.info(
                        f"Message {message_id} already has a canonical social publication. Returning existing result."
                    )
                    return {
                        'success': True,
                        'tweet_url': existing_publication.get('provider_url'),
                        'tweet_id': existing_publication.get('provider_ref'),
                        'publication_id': existing_publication.get('publication_id'),
                        'message_id': message_id,
                        'already_shared': True,
                    }

                # Step 5: Generate title and descriptions
                is_video = any('video' in (att.get('content_type') or '') for att in downloaded_attachments)
                media_type = 'video' if is_video else 'image'
                
                first_attachment = downloaded_attachments[0] if downloaded_attachments else None
                generated_title = None

                if first_attachment:
                    self.logger.info(f"Generating media title for message {message_id} ({first_attachment.get('content_type')}).")
                    generated_title = await generate_media_title(
                        attachment=first_attachment,
                        original_comment=message.content,
                        post_id=message.id
                    )
                
                self.logger.info(f"Generating LLM description (for non-Twitter use) via dispatcher for message {message_id}...")
                # Build a concise prompt for the LLM. The function `get_llm_response` expects the standard
                # arguments (client_name, model, system_prompt, messages,…). We were previously calling it
                # with a non-existent `prompt` kwarg which caused a TypeError and aborted the sharing flow.
                llm_description = await get_llm_response(
                    client_name="claude",
                    model="claude-sonnet-4-5-20250929",
                    system_prompt=(
                        "You are an expert social-media copywriter. Respond with exactly one engaging yet "
                        "concise sentence (no hashtags) that describes the attached media so it can be used "
                        "as a caption on various platforms."
                    ),
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Author: {message.author.display_name}\n"
                            f"Media type: {media_type}\n"
                            f"Original comment: {message.content}"
                        )
                    }],
                    max_tokens=64,
                    temperature=0.7,
                )

                twitter_content = ""
                if tweet_text:
                    # Custom tweet text provided (e.g. from admin chat)
                    twitter_content = tweet_text
                    self.logger.info(f"Using custom tweet text for message {message_id}: '{twitter_content[:80]}...'")
                elif summary_channel and "top-art-sharing" in summary_channel.name.lower():
                    self.logger.info(f"Using specific Twitter format for message {message_id}. Content: '{summary_channel.topic[:50]}...'")
                    twitter_content = summary_channel.topic
                else:
                     self.logger.info(f"Using Title (for non-Twitter): '{generated_title}', LLM Desc (for non-Twitter): '{llm_description[:50]}...' for message {message_id}")
                     # Check if this is from top art sharing (summary_channel exists) vs regular reaction-based sharing
                     if summary_channel:
                         # This is top art sharing from daily summary
                         twitter_content = f"Top art sharing post of the day by {message.author.display_name}:"
                     else:
                         # This is regular reaction-based sharing
                         twitter_content = f"Check out this post by {message.author.display_name}! {message.jump_url}"

                request = self._build_publish_request(
                    message_id=message_id,
                    channel_id=channel_id,
                    guild_id=getattr(message.guild, 'id', None),
                    user_id=user_id,
                    platform='twitter',
                    action=action,
                    text=twitter_content,
                    media_hints=downloaded_attachments,
                    source_kind=resolved_source_kind,
                    target_post_ref=in_reply_to_tweet_id,
                    text_only=text_only,
                    author_display_name=message.author.display_name,
                    original_message_content=message.content,
                    original_message_jump_url=message.jump_url,
                    user_details=user_details,
                    duplicate_policy={
                        'check_existing': self._should_check_duplicate(resolved_source_kind, action),
                    },
                    legacy_shared_post_policy={
                        'enabled': action == 'post',
                        'delete_eligible_hours': 6,
                    },
                )

                self.logger.info(f"Attempting to publish message {message_id} via SocialPublishService.")
                publish_result = await self.social_publish_service.publish_now(request)
                if publish_result.success:
                    tweet_url = publish_result.tweet_url
                    tweet_id = publish_result.tweet_id
                    publication_id = publish_result.publication_id
                    self.logger.info(f"Successfully posted message {message_id} to Twitter: {tweet_url}")
                    if tweet_url:
                        await self._announce_tweet_url(
                            tweet_url,
                            message.author.display_name,
                            message.jump_url,
                            str(message_id),
                            guild_id=getattr(message.guild, 'id', None),
                            is_reply=bool(in_reply_to_tweet_id)
                        )

                    # Check if this is the user's first share and send notification
                    is_first_share = self.db_handler.mark_member_first_shared(user_id, guild_id=getattr(message.guild, 'id', None))
                    if is_first_share:
                        self.logger.info(f"First share for user {user_id}. Sending post-share notification.")
                        await send_post_share_notification(
                            bot=self.bot,
                            user=message.author,
                            discord_message=message,
                            publication_id=publication_id,
                            tweet_id=tweet_id,
                            tweet_url=tweet_url,
                            db_handler=self.db_handler
                        )

                    return {
                        'success': True,
                        'tweet_url': tweet_url,
                        'tweet_id': tweet_id,
                        'publication_id': publication_id,
                        'message_id': message_id,
                        'already_shared': False
                    }

                self.logger.error(f"Failed to post message {message_id} to Twitter.")
                return {
                    'success': False,
                    'error': publish_result.error or 'Failed to post tweet',
                    'publication_id': publish_result.publication_id,
                    'message_id': message_id,
                }

                # Removed summary channel posting to prevent messages appearing after "jump to beginning"
                # if summary_channel:
                #     # Create a unique key for this message-channel combination
                #     summary_key = f"{message_id}_{summary_channel.id}"
                #     if summary_key not in self._posted_to_summary:
                #         self.logger.info(f"Attempting to post summary to original summary channel {summary_channel.id} for message {message_id}")
                #         summary_content = f"Successfully shared post by <@{user_id}>: {message.jump_url}"
                #         await summary_channel.send(summary_content)
                #         self._posted_to_summary.add(summary_key)
                #         self.logger.info(f"Successfully posted summary for message {message_id} to summary channel {summary_channel.id}")
                #     else:
                #         self.logger.info(f"Summary for message {message_id} already posted to channel {summary_channel.id}, skipping duplicate post")

            except Exception as e:
                self.logger.error(f"An unexpected error occurred during finalize_sharing for message {message_id}: {e}", exc_info=True)
                return {'success': False, 'error': str(e), 'message_id': message_id}
            finally:
                if message_id in self._currently_processing:
                    self._currently_processing.remove(message_id)
                self.logger.info(f"Finished processing sharing for message {message_id}.")
                if 'downloaded_attachments' in locals():
                    self._cleanup_files([att['local_path'] for att in downloaded_attachments if 'local_path' in att])

    def _cleanup_files(self, file_paths: List[str]):
        """Removes temporary files."""
        for file_path in file_paths:
            try:
                os.remove(file_path)
                self.logger.info(f"Removed temporary file: {file_path}")
            except OSError as e:
                self.logger.error(f"Error removing temporary file {file_path}: {e}")

    # Replaced placeholder with actual implementation
    async def _fetch_message(self, channel_id: int, message_id: int) -> Optional[discord.Message]:
        """Fetches a message using channel_id and message_id."""
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                # Fallback: try fetching channel if not in cache
                self.logger.info(f"[Sharer._fetch_message] Channel {channel_id} not in cache, fetching.")
                channel = await self.bot.fetch_channel(channel_id)
                
            if isinstance(channel, (discord.TextChannel, discord.Thread)): # Added discord.Thread
                 message = await channel.fetch_message(message_id)
                 self.logger.info(f"Successfully fetched message {message_id} from channel {channel_id}")
                 return message
            else:
                self.logger.error(f"Channel {channel_id} is not a TextChannel or Thread.")
                return None
        except discord.NotFound:
            self.logger.error(f"Could not find channel {channel_id} or message {message_id}.")
            return None
        except discord.Forbidden:
            self.logger.error(f"Bot lacks permissions to fetch message {message_id} from channel {channel_id}.")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error fetching message {message_id} from channel {channel_id}: {e}", exc_info=True)
            return None 

    def _get_announcement_channel_id(self, guild_id: Optional[int]) -> Optional[int]:
        """Resolve announcement_channel_id from server_config."""
        sc = getattr(self.db_handler, 'server_config', None)
        if sc and guild_id:
            val = sc.get_server_field(guild_id, 'announcement_channel_id', cast=int)
            if val:
                return val
        return None

    async def _announce_tweet_url(
        self,
        tweet_url: str,
        author_display_name: str,
        original_message_jump_url: Optional[str] = None,
        context_message_id: Optional[str] = None,
        guild_id: Optional[int] = None,
        is_reply: bool = False
    ):
        channel_id = self._get_announcement_channel_id(guild_id)
        if not channel_id:
            self.logger.info("[Sharer] Tweet announcement channel not configured. Skipping.")
            return

        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(channel_id)

            if not isinstance(channel, discord.TextChannel):
                self.logger.error(f"[Sharer] Announcement channel {channel_id} is not a text channel.")
                return

            message_content = f"{'Replied in thread' if is_reply else 'Tweet'}: {tweet_url}"
            if original_message_jump_url:
                message_content += f"\n\nBased on this post by {author_display_name}: {original_message_jump_url}"
            elif context_message_id:
                message_content += f"\n\nBased on this post by {author_display_name} (Original Discord Message ID for context: {context_message_id})"
            else:
                message_content += f"\n\n(Shared content by {author_display_name})"

            if hasattr(self.bot, 'rate_limiter') and self.bot.rate_limiter is not None:
                await discord_utils.safe_send_message(
                    self.bot, channel, self.bot.rate_limiter, self.logger,
                    content=message_content
                )
            else:
                await channel.send(content=message_content)
            self.logger.info(f"[Sharer] Announced tweet {tweet_url} to channel {channel_id}.")

        except discord.NotFound:
            self.logger.error(f"[Sharer] Announcement channel {channel_id} not found.")
        except discord.Forbidden:
            self.logger.error(f"[Sharer] No permission to send to announcement channel {channel_id}.")
        except Exception as e:
            self.logger.error(f"[Sharer] Error announcing tweet to channel {channel_id}: {e}", exc_info=True) 
