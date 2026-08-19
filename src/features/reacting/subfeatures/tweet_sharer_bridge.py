import discord
import logging
from discord.ui import View, Button, button # Added for UI elements
from discord.ext import commands # For Bot instance to use wait_for
from typing import Optional, Any # Added Any
import asyncio # Added for asyncio.TimeoutError
import os # For saving and removing temporary files
import re # For regular expressions
from src.common.llm.claude_client import ClaudeClient # Corrected LLM Client import
from src.common import discord_utils # Added import
from src.features.sharing.models import PublicationSourceContext, SocialPublishRequest

# Assuming Sharer class will be passed or imported appropriately
# from src.features.sharing.sharer import Sharer
# Assuming DatabaseHandler will be passed
# from src.common.db_handler import DatabaseHandler

# Timeout for waiting for user responses in DMs
USER_RESPONSE_TIMEOUT = 21600.0  # 6 hours (6 * 60 * 60)

ADMIN_USER_ID_STR = os.getenv('ADMIN_USER_ID')
ADMIN_USER_ID = None
if ADMIN_USER_ID_STR:
    try:
        ADMIN_USER_ID = int(ADMIN_USER_ID_STR)
    except ValueError:
        # This will be logged where ADMIN_USER_ID is first used if it remains None
        pass 

async def _send_admin_flag_dm(
    bot_instance: commands.Bot, 
    logger: logging.Logger, 
    message_to_share: discord.Message, 
    original_poster: discord.User, 
    reactor: discord.User, 
    reactor_comment: str, 
    moderation_decision: str, 
    path_type: str,
    moderation_reason: Optional[str] = None
):
    """Sends a DM to the admin if content is flagged."""
    if not ADMIN_USER_ID:
        logger.warning(f"[TweetSharerBridge] ADMIN_USER_ID not configured or invalid. Cannot send admin DM for flagged content in {path_type}.")
        return

    try:
        admin_user = await bot_instance.fetch_user(ADMIN_USER_ID)
        if not admin_user:
            logger.warning(f"[TweetSharerBridge] Could not fetch admin user {ADMIN_USER_ID} for flagging DM in {path_type}.")
            return

        dm_channel = await admin_user.create_dm()
        
        original_content_snippet = message_to_share.content[:200] + "..." if message_to_share.content and len(message_to_share.content) > 200 else message_to_share.content
        reactor_comment_snippet = reactor_comment[:200] + "..." if len(reactor_comment) > 200 else reactor_comment

        embed = discord.Embed(
            title=f"Content Flagged by LLM",
            description=f"A piece of content was flagged as unsuitable by the LLM during a tweet sharing attempt.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Original Message ID", value=f"[{message_to_share.id}]({message_to_share.jump_url})", inline=False)
        embed.add_field(name="Original Poster", value=f"{original_poster.name} (`{original_poster.id}`)", inline=True)
        embed.add_field(name="Reactor (Initiator)", value=f"{reactor.name} (`{reactor.id}`)", inline=True)
        embed.add_field(name="LLM Decision", value=f"`{moderation_decision}`", inline=True)
        embed.add_field(name="Reactor's Comment", value=f"```\n{reactor_comment_snippet}\n```", inline=False)
        if original_content_snippet:
            embed.add_field(name="Original Message Content Snippet", value=f"```\n{original_content_snippet}\n```", inline=False)
        else:
            embed.add_field(name="Original Message Content", value="[No text content or media only]", inline=False)
        
        if moderation_reason:
            embed.add_field(name="LLM Rejection Reason", value=f"```\n{moderation_reason[:1000]}\n```", inline=False) # Cap reason length for embed

        embed.set_footer(text="TweetSharerBridge Moderation Alert")

        if not hasattr(bot_instance, 'rate_limiter'):
            logger.error(f"[TweetSharerBridge] Rate limiter not found on bot_instance for _send_admin_flag_dm. Sending directly.")
            await dm_channel.send(embed=embed)
        else:
            await discord_utils.safe_send_message(
                bot_instance,
                dm_channel,
                bot_instance.rate_limiter,
                logger,
                embed=embed
            )
        logger.info(f"[TweetSharerBridge] Sent admin DM to {ADMIN_USER_ID} for flagged content in {path_type} (message {message_to_share.id}).")

    except discord.Forbidden:
        logger.warning(f"[TweetSharerBridge] Could not DM admin {ADMIN_USER_ID} (Forbidden) for flagged content in {path_type}.")
    except discord.NotFound:
        logger.warning(f"[TweetSharerBridge] Admin user {ADMIN_USER_ID} not found for flagging DM in {path_type}.")
    except Exception as e:
        logger.error(f"[TweetSharerBridge] Error sending admin DM for flagged content in {path_type} (message {message_to_share.id}): {e}", exc_info=True)

async def _process_moderation_and_sharing(
    bot_instance: commands.Bot,
    logger: logging.Logger,
    db_handler: Any, 
    sharer_instance: Any, 
    llm_client: ClaudeClient,
    message_to_share: discord.Message,
    original_poster: discord.User,
    reactor: discord.User,
    reactor_comment: str,
    moderation_model_name: str,
    path_type: str, # e.g., "Consent Path" or "Pre-Approved Path"
    interaction: Optional[discord.Interaction] = None, 
    reactor_dm_channel_override: Optional[discord.DMChannel] = None
):
    """Handles LLM moderation, tweeting, and notifications."""
    logger.info(f"[TweetSharerBridge] Starting moderation and sharing process for message {message_to_share.id} via {path_type}.")

    system_moderation_prompt = """Can you determine whether this TEXT CONTENT is suitable for sharing on an open source AI art community's Twitter to showcase cool AI creations and celebrate creativity?

IMPORTANT: You are only moderating the text content - you cannot see any images, GIFs, or other media attachments. If there's no text content but the message has media attachments, you should generally approve it since this community is about showcasing visual AI art.

This community loves to highlight:
- Amazing AI-generated art and creative works
- Innovative techniques and experiments  
- Cool discoveries and breakthroughs
- Community members showing off their creations
- Enthusiasm and excitement about AI art

Only reject TEXT content that is:
- Clearly offensive, hateful, or inappropriate language
- Spam or completely unrelated to AI/art
- Contains personal attacks or harassment

Be permissive and err on the side of sharing - we want to celebrate creativity and show off cool things! Enthusiasm and self-promotion of creative work is encouraged. If there's no text content but media is present, approve it.

Your reply should be in this format:

{yes or no}|{reason}

Make sure to use lowercase. For example:

yes|great creative work to showcase

Reply with that and nothing else"""
    user_content_for_moderation = (
        f"Original Post Content: \"\"\"{message_to_share.content if message_to_share.content else '[No text content]'}\"\"\"\n"
        f"Reactor's Comment: \"\"\"{reactor_comment}\"\"\""
    )
    moderation_messages = [{'role': 'user', 'content': user_content_for_moderation}]
    
    actual_moderation_decision = "yes" # Default to yes (fail-open)
    moderation_reason = "Defaulted to approval due to LLM call issue or client unavailability." # Default reason if LLM not called or fails

    # Check if both original content and reactor comment are empty
    original_content_is_empty = not (message_to_share.content and message_to_share.content.strip())
    reactor_comment_is_empty = not (reactor_comment and reactor_comment.strip())

    if original_content_is_empty and reactor_comment_is_empty:
        logger.info(f"[TweetSharerBridge] ({path_type}) Skipping LLM moderation for message {message_to_share.id} as both original content and reactor comment are empty. Proceeding with approval.")
        actual_moderation_decision = "yes"
        moderation_reason = "Skipped LLM review: No text content from original post or reactor comment."
    else:
        # Proceed with LLM moderation only if there is some text content
        try:
            if llm_client and isinstance(llm_client, ClaudeClient):
                llm_response_raw = await llm_client.generate_chat_completion(
                    model=moderation_model_name,
                    system_prompt=system_moderation_prompt,
                    messages=moderation_messages,
                    max_tokens=150 # Increased to accommodate reason
                )
                parsed_llm_response = llm_response_raw.strip().lower()
                parts = parsed_llm_response.split('|', 1)
                if len(parts) == 2:
                    actual_moderation_decision = parts[0]
                    moderation_reason = parts[1]
                    if actual_moderation_decision not in ["yes", "no"]:
                        logger.warning(f"[TweetSharerBridge] LLM moderation ({path_type}) decision part is not 'yes' or 'no': '{actual_moderation_decision}'. Defaulting to 'no'. Full response: '{parsed_llm_response}'")
                        actual_moderation_decision = "no"
                        moderation_reason = moderation_reason if moderation_reason else "Invalid decision value from LLM."
                else:
                    logger.warning(f"[TweetSharerBridge] LLM moderation ({path_type}) response not in expected format 'yes/no|reason': '{parsed_llm_response}'. Defaulting to 'no'.")
                    actual_moderation_decision = "no"
                    moderation_reason = "LLM response format error."
                
                logger.info(f"[TweetSharerBridge] LLM moderation ({path_type}) for message {message_to_share.id} - Decision: '{actual_moderation_decision}', Reason: '{moderation_reason}', Model: '{moderation_model_name}'")
            else:
                logger.warning(f"[TweetSharerBridge] LLM client not available or not a ClaudeClient instance for {path_type} check on message {message_to_share.id}. Skipping moderation. Defaulting to approval based on initial default.")
                # actual_moderation_decision and moderation_reason retain their initial fail-open 'yes' or 'LLM client not available' values
        except Exception as e_llm:
            logger.error(f"[TweetSharerBridge] LLM moderation call failed ({path_type}) for message {message_to_share.id} using model '{moderation_model_name}': {e_llm}. Defaulting to 'yes'.")
            actual_moderation_decision = "yes" # Fail-open on error
            moderation_reason = f"LLM call failed: {str(e_llm)}" # Provide specific error as reason

    reactor_dm_to_use = reactor_dm_channel_override
    if not reactor_dm_to_use:
        try:
            reactor_dm_to_use = await reactor.create_dm()
        except discord.Forbidden:
            logger.warning(f"[TweetSharerBridge] ({path_type}) Cannot create DM with reactor {reactor.id}. Notifications may fail.")
        except Exception as e:
            logger.error(f"[TweetSharerBridge] ({path_type}) Error creating DM with reactor {reactor.id}: {e}. Notifications may fail.")
            
    _guild_id = getattr(message_to_share.guild, 'id', None)
    if actual_moderation_decision == "yes":
        if path_type == "Consent Path":
            db_handler.create_or_update_member(
                member_id=original_poster.id,
                username=original_poster.name,
                global_name=getattr(original_poster, 'global_name', None),
                display_name=getattr(original_poster, 'nick', None),
                allow_content_sharing=True,
                guild_id=_guild_id,
            )
            logger.info(f"[TweetSharerBridge] ({path_type}) Updated allow_content_sharing to True for user {original_poster.id} (LLM approved).")
            if interaction: # Should always be true for Consent Path
                try:
                    await interaction.followup.send("Thanks! Your content is being shared.", ephemeral=True)
                except discord.HTTPException as e:
                     logger.warning(f"[TweetSharerBridge] ({path_type}) Failed to send 'content being shared' followup to OP {original_poster.id}: {e}")
        
        member_data = db_handler.get_member(original_poster.id)
        raw_twitter_handle = member_data.get('twitter_url') if member_data else None
        author_identifier = original_poster.display_name # Default to display name

        if raw_twitter_handle:
            handle_val = raw_twitter_handle.strip()
            extracted_username = None

            is_url_like_structure = '://' in handle_val or \
                                   'x.com/' in handle_val.lower() or \
                                   'twitter.com/' in handle_val.lower()

            if handle_val.startswith('@') and is_url_like_structure:
                # If it's like '@twitter.com/user', remove the initial @ for URL parsing
                handle_val = handle_val[1:]

            if '://' in handle_val:
                path_after_scheme = handle_val.split('://', 1)[-1]
                domain_and_path_lower = path_after_scheme.lower()
                if domain_and_path_lower.startswith('twitter.com/'):
                    extracted_username = path_after_scheme[len('twitter.com/'):].split('/')[0]
                elif domain_and_path_lower.startswith('www.twitter.com/'):
                    extracted_username = path_after_scheme[len('www.twitter.com/'):].split('/')[0]
                elif domain_and_path_lower.startswith('x.com/'):
                    extracted_username = path_after_scheme[len('x.com/'):].split('/')[0]
                elif domain_and_path_lower.startswith('www.x.com/'):
                    extracted_username = path_after_scheme[len('www.x.com/'):].split('/')[0]
            elif 'x.com/' in handle_val.lower():
                match_pattern = 'x.com/'
                start_idx = handle_val.lower().find(match_pattern) + len(match_pattern)
                extracted_username = handle_val[start_idx:].split('/')[0]
            elif 'twitter.com/' in handle_val.lower():
                match_pattern = 'twitter.com/'
                start_idx = handle_val.lower().find(match_pattern) + len(match_pattern)
                extracted_username = handle_val[start_idx:].split('/')[0]
            else:
                extracted_username = handle_val # Assume it's a direct handle or @handle

            if extracted_username:
                # Remove query parameters or fragments, and leading @
                cleaned_username = extracted_username.split('?')[0].split('#')[0]
                if cleaned_username.startswith('@'):
                    cleaned_username = cleaned_username[1:]
                if cleaned_username: # Ensure not empty after stripping
                    author_identifier = f"@{cleaned_username}" # Add a single @

        if reactor_comment and reactor_comment.strip(): # Check if comment is not empty or just whitespace
            tweet_text = f"{reactor_comment.strip()}\n\nGeneration by {author_identifier}"
        else:
            tweet_text = f"Generation by {author_identifier}"
        media_urls = [att.url for att in message_to_share.attachments]

        if sharer_instance:
            downloaded_media = []
            temp_files_to_clean = []
            try:
                if media_urls:
                    for i, url in enumerate(media_urls):
                        downloaded_item = await sharer_instance._download_media_from_url(
                            url,
                            str(message_to_share.id),
                            i,
                        )
                        if downloaded_item and downloaded_item.get('local_path'):
                            downloaded_media.append(downloaded_item)
                            temp_files_to_clean.append(downloaded_item['local_path'])
                        else:
                            logger.warning(
                                f"[TweetSharerBridge] ({path_type}) Failed to download media from URL: {url}"
                            )
                if media_urls and not downloaded_media:
                    success = False
                    tweet_url = None
                else:
                    request = SocialPublishRequest(
                        message_id=message_to_share.id,
                        channel_id=message_to_share.channel.id,
                        guild_id=getattr(message_to_share.guild, 'id', None) or 0,
                        user_id=original_poster.id,
                        platform='twitter',
                        action='post',
                        text=tweet_text,
                        media_hints=downloaded_media,
                        source_kind='reaction_bridge',
                        duplicate_policy={'check_existing': False},
                        text_only=not downloaded_media,
                        announce_policy={
                            'enabled': True,
                            'author_display_name': original_poster.display_name,
                            'original_message_jump_url': message_to_share.jump_url,
                        },
                        first_share_notification_policy={'enabled': False},
                        legacy_shared_post_policy={'enabled': True, 'delete_eligible_hours': 6},
                        moderation_metadata={
                            'decision': actual_moderation_decision,
                            'reason': moderation_reason,
                            'path_type': path_type,
                        },
                        source_context=PublicationSourceContext(
                            source_kind='reaction_bridge',
                            metadata={
                                'user_details': member_data or {},
                                'original_content': message_to_share.content,
                                'author_display_name': original_poster.display_name,
                                'original_message_jump_url': message_to_share.jump_url,
                                'guild_id': _guild_id,
                            },
                        ),
                    )
                    publish_result = await sharer_instance.social_publish_service.publish_now(request)
                    success = publish_result.success
                    tweet_url = publish_result.tweet_url
                    if success:
                        if tweet_url:
                            await sharer_instance._announce_tweet_url(
                                tweet_url=tweet_url,
                                author_display_name=original_poster.display_name,
                                original_message_jump_url=message_to_share.jump_url,
                                context_message_id=str(message_to_share.id),
                                guild_id=_guild_id,
                                is_reply=False,
                            )
                        db_handler.mark_member_first_shared(original_poster.id, guild_id=_guild_id)
            finally:
                sharer_instance._cleanup_files(temp_files_to_clean)

            if success:
                reactor_dm_confirm_msg = f"Your comment on {message_to_share.jump_url} has been tweeted! View it here: {tweet_url}"
                if not tweet_url: reactor_dm_confirm_msg = f"Your comment on {message_to_share.jump_url} has been tweeted!"
                if reactor_dm_to_use:
                    try: await reactor_dm_to_use.send(reactor_dm_confirm_msg)
                    except discord.HTTPException: logger.warning(f"[TweetSharerBridge] ({path_type}) Failed to DM reactor {reactor.id} with tweet success.")
                logger.info(f"[TweetSharerBridge] ({path_type}) Tweet sent for message {message_to_share.id} (LLM approved). URL: {tweet_url}")
            else: # Tweet failed
                if reactor_dm_to_use:
                    try: await reactor_dm_to_use.send(f"Sorry, there was an issue trying to tweet your comment for {message_to_share.jump_url}. Please try again later or contact an admin.")
                    except discord.HTTPException: logger.warning(f"[TweetSharerBridge] ({path_type}) Failed to DM reactor {reactor.id} with tweet failure.")
                logger.error(f"[TweetSharerBridge] ({path_type}) Failed to send tweet for message {message_to_share.id} (LLM approved).")
        else: # Sharer instance not available
            logger.error(f"[TweetSharerBridge] ({path_type}) Sharer instance not available for sending tweet (LLM approved).")
            if reactor_dm_to_use:
                try: await reactor_dm_to_use.send(f"Sorry, the tweeting service is currently unavailable for {message_to_share.jump_url}.")
                except discord.HTTPException: logger.warning(f"[TweetSharerBridge] ({path_type}) Failed to DM reactor {reactor.id} with sharer unavailable.")
    
    else: # LLM Moderation said "no"
        logger.info(f"[TweetSharerBridge] ({path_type}) Content for message {message_to_share.id} flagged by LLM as unsuitable. Decision: '{actual_moderation_decision}', Reason: '{moderation_reason}'. Not tweeting.")
        
        if path_type == "Consent Path":
            # Update sharing preference for OP (they said yes, but content flagged)
            db_handler.create_or_update_member(
                member_id=original_poster.id,
                username=original_poster.name,
                global_name=getattr(original_poster, 'global_name', None),
                display_name=getattr(original_poster, 'nick', None),
                allow_content_sharing=True,  # Still record they consented, even if this instance is blocked
                guild_id=_guild_id,
            )
            logger.info(f"[TweetSharerBridge] ({path_type}) User {original_poster.id} consented, but LLM flagged. allow_content_sharing still set to True.")
            if interaction: # Should always be true for Consent Path
                try:
                    await interaction.followup.send("Thank you for your consent. However, upon further review by our automated system, the content was determined to be unsuitable for tweeting at this time. An admin has been notified. Your general preference to share has been saved.", ephemeral=True)
                except discord.HTTPException:
                    logger.warning(f"[TweetSharerBridge] ({path_type}) Failed to send LLM rejection followup to OP {original_poster.id}.")

        # Notify Reactor with an embed
        if reactor_dm_to_use:
            reactor_embed = discord.Embed(
                title="Content Moderation Update",
                description=f"The content you proposed for sharing from {message_to_share.jump_url} was reviewed by our automated system and determined to be unsuitable for tweeting at this time.",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            reactor_embed.add_field(name="Original Poster", value=f"{original_poster.name} (`{original_poster.id}`)", inline=True)
            reactor_comment_snippet = reactor_comment[:200] + "..." if len(reactor_comment) > 200 else reactor_comment
            reactor_embed.add_field(name="Your Comment", value=f"```\n{reactor_comment_snippet}\n```", inline=False if len(reactor_comment_snippet) > 50 else True) # Basic layout adjustment
            reactor_embed.add_field(name="Moderation Outcome", value="Flagged as unsuitable", inline=True)
            reason_snippet = moderation_reason[:1000] # Max length for embed field value
            reactor_embed.add_field(name="Reason Provided by System", value=f"```\n{reason_snippet}\n```", inline=False)
            reactor_embed.set_footer(text="TweetSharerBridge Moderation")

            await reactor_dm_to_use.send(embed=reactor_embed)
            logger.info(f"[TweetSharerBridge] ({path_type}) Sent moderation block embed to reactor {reactor.id} for message {message_to_share.id}.")
        
        log_prefix = "ADMIN ALERT" if path_type == "Consent Path" else "ADMIN ALERT (Pre-Approved Path)"
        logger.critical(f"[TweetSharerBridge] {log_prefix}: Content for message {message_to_share.id} (OP {original_poster.id}, Reactor {reactor.id}, Comment: \"{reactor_comment[:100]}...\") was FLAGGED by LLM. Decision: '{actual_moderation_decision}', Reason: '{moderation_reason}'. Original Content: \"{message_to_share.content[:100]}...\". Tweet ABORTED.")
        
        # Send DM to Admin
        await _send_admin_flag_dm(
            bot_instance=bot_instance,
            logger=logger,
            message_to_share=message_to_share,
            original_poster=original_poster,
            reactor=reactor,
            reactor_comment=reactor_comment,
            moderation_decision=actual_moderation_decision, 
            path_type=path_type,
            moderation_reason=moderation_reason
        )

class ConsentView(View):
    def __init__(self, original_poster: discord.User, reactor: discord.User, reactor_comment: str, message_to_share: discord.Message, db_handler, sharer_instance, logger, bot_instance, llm_client: ClaudeClient):
        super().__init__(timeout=USER_RESPONSE_TIMEOUT)
        self.original_poster = original_poster
        self.reactor = reactor
        self.reactor_comment = reactor_comment
        self.message_to_share = message_to_share
        self.db_handler = db_handler
        self.sharer_instance = sharer_instance
        self.logger = logger
        self.bot = bot_instance 
        self.llm_client = llm_client
        self.consent_given = None 
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.original_poster.id

    async def on_timeout(self):
        self.logger.info(f"[TweetSharerBridge] Consent view timed out for message {self.message_to_share.id}. User {self.original_poster.id} did not respond within 6 hours.")
        if self.message:
            try:
                await self.message.delete()
                self.logger.info(f"[TweetSharerBridge] Deleted consent DM {self.message.id} sent to {self.original_poster.id} due to timeout.")
            except discord.Forbidden:
                self.logger.warning(f"[TweetSharerBridge] Could not delete consent DM {self.message.id} for {self.original_poster.id} (Forbidden).")
            except discord.NotFound:
                self.logger.warning(f"[TweetSharerBridge] Could not delete consent DM {self.message.id} for {self.original_poster.id} (NotFound - already deleted?).")
            except Exception as e:
                self.logger.error(f"[TweetSharerBridge] Error deleting consent DM {self.message.id} for {self.original_poster.id}: {e}")
        else:
            for item in self.children:
                if isinstance(item, Button):
                    item.disabled = True
        try:
            reactor_dm_channel = await self.reactor.create_dm()
            await discord_utils.safe_send_message(
                self.bot, reactor_dm_channel, self.bot.rate_limiter, self.logger, 
                content=f"The author of the message did not respond to the 6-hour request to share their content. Message: {self.message_to_share.jump_url}"
            )
        except discord.Forbidden:
            self.logger.warning(f"[TweetSharerBridge] Could not DM reactor {self.reactor.id} about timeout.")
        except Exception as e:
            self.logger.error(f"[TweetSharerBridge] Error DMing reactor {self.reactor.id} about timeout: {e}")

    @button(label="I'm happy for my stuff to be shared", style=discord.ButtonStyle.green, custom_id="consent_share_yes")
    async def consent_yes_button(self, interaction: discord.Interaction):
        self.logger.info(f"[TweetSharerBridge] User {self.original_poster.id} GAVE consent to share message {self.message_to_share.id} reacted by {self.reactor.id}. Proceeding with LLM check.")
        self.consent_given = True
        # Disable buttons early, edit message after LLM check and potential notifications
        for item in self.children:
            if isinstance(item, Button): item.disabled = True
        # Acknowledge interaction quickly, will edit full message later
        await interaction.response.defer() 

        try:
            await _process_moderation_and_sharing(
                bot_instance=self.bot,
                logger=self.logger,
                db_handler=self.db_handler,
                sharer_instance=self.sharer_instance,
                llm_client=self.llm_client,
                message_to_share=self.message_to_share,
                original_poster=self.original_poster,
                reactor=self.reactor,
                reactor_comment=self.reactor_comment,
                moderation_model_name="claude-sonnet-4-5-20250929",
                path_type="Consent Path",
                interaction=interaction # Pass the interaction object
            )
            # Original message edit now happens after helper function finishes (to disable buttons)
            await interaction.edit_original_response(view=self)

        except Exception as e:
            self.logger.error(f"[TweetSharerBridge] Error in consent_yes_button after LLM check: {e}", exc_info=True)
            try: 
                if not interaction.is_done(): 
                    # Ephemeral followup, less critical for safe_send_message but can be for consistency
                    await interaction.followup.send("An error occurred while processing your consent after the final review. Please try again.", ephemeral=True)
                else: 
                    await discord_utils.safe_send_message(
                        self.bot, self.original_poster, self.bot.rate_limiter, self.logger,
                        content="An error occurred while processing your consent after the final review. Please try again."
                    )
            except Exception as e_followup:
                self.logger.error(f"[TweetSharerBridge] Failed to send error followup/DM in consent_yes_button: {e_followup}")
            # Ensure view is disabled on the original message even if errors occur post-deferral
            if self.message: # If original DM object is stored
                for item_view in self.children:
                    if isinstance(item_view, Button): item_view.disabled = True
                try: await self.message.edit(view=self)
                except Exception: pass # Suppress edit error here, main error logged
        finally:
            self.stop()

    @button(label="Please don't share", style=discord.ButtonStyle.red, custom_id="consent_share_no")
    async def consent_no_button(self, interaction: discord.Interaction):
        self.logger.info(f"[TweetSharerBridge] User {self.original_poster.id} DENIED consent to share message {self.message_to_share.id} reacted by {self.reactor.id}")
        self.consent_given = False
        for item in self.children:
            if isinstance(item, Button): item.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            # Set allow_content_sharing to False since user explicitly denied
            self.db_handler.create_or_update_member(
                member_id=self.original_poster.id,
                username=self.original_poster.name,
                global_name=getattr(self.original_poster, 'global_name', None),
                display_name=getattr(self.original_poster, 'nick', None),
                allow_content_sharing=False,
                guild_id=getattr(self.message_to_share.guild, 'id', None),
            )
            self.logger.info(f"[TweetSharerBridge] Updated allow_content_sharing to False for user {self.original_poster.id}")
            # Add the "no sharing" role to make opt-out visible
            await discord_utils.update_no_sharing_role(
                self.bot,
                self.original_poster.id,
                False,
                self.logger,
                guild_id=getattr(self.message_to_share.guild, 'id', None),
            )
            await discord_utils.safe_send_message(
                self.bot, interaction.user, self.bot.rate_limiter, self.logger,
                content="Your preference has been updated. This content will not be shared, and we won't ask for this message again. You can manage global preferences by DMing me /update_details."
            )
            reactor_dm_channel = await self.reactor.create_dm()
            await discord_utils.safe_send_message(
                self.bot, reactor_dm_channel, self.bot.rate_limiter, self.logger,
                content=f"The author of the message {self.message_to_share.jump_url} declined to have their content shared at this time."
            )
        except Exception as e:
            self.logger.error(f"[TweetSharerBridge] Error in consent_no_button: {e}", exc_info=True)
            await interaction.followup.send("An error occurred while processing your preference. Please try again.", ephemeral=True)
        self.stop()

async def handle_send_tweet_about_message(
    reaction: discord.Reaction, user: discord.User, sharer_instance, 
    logger: logging.Logger, db_handler, bot_instance: commands.Bot, llm_client: ClaudeClient # Corrected type hint
):
    reactor = user 
    original_poster = reaction.message.author 
    message_to_share = reaction.message
    logger.info(f"[TweetSharerBridge] Initiating 'send_tweet_about_message' for message {message_to_share.id} by {original_poster.id}, reacted by {reactor.id}.")

    if original_poster.bot:
        logger.info(f"[TweetSharerBridge] Original poster ({original_poster.id}) is a bot. Aborting.")
        return

    # Check if the source channel is NSFW
    if hasattr(message_to_share.channel, 'name') and isinstance(message_to_share.channel, discord.TextChannel):
        if "nsfw" in message_to_share.channel.name.lower():
            logger.info(f"[TweetSharerBridge] Message {message_to_share.id} is from an NSFW channel ('{message_to_share.channel.name}'). Aborting share process.")
            try:
                reactor_dm_channel_nsfw = await reactor.create_dm()
                dm_content_nsfw = (
                    f"Sorry, content from channels marked as NSFW (like '{message_to_share.channel.name}') cannot be shared using this feature. "
                    f"The message was: {message_to_share.jump_url}"
                )
                if not hasattr(bot_instance, 'rate_limiter'):
                    logger.error(f"[TweetSharerBridge] Rate limiter not found on bot_instance for NSFW DM. Sending directly.")
                    await reactor_dm_channel_nsfw.send(content=dm_content_nsfw)
                else:
                    await discord_utils.safe_send_message(
                        bot_instance, reactor_dm_channel_nsfw, bot_instance.rate_limiter, logger, content=dm_content_nsfw
                    )
            except discord.Forbidden:
                logger.warning(f"[TweetSharerBridge] Could not DM reactor {reactor.id} about NSFW channel block (Forbidden).")
            except Exception as e_dm_nsfw:
                logger.error(f"[TweetSharerBridge] Error DMing reactor {reactor.id} about NSFW channel block: {e_dm_nsfw}")
            return

    reactor_comment = None # Initialize reactor_comment

    # --- Self-Post Flow or Regular Flow: DM Reactor for comment ---
    try:
        reactor_dm_channel = await reactor.create_dm()
    except discord.Forbidden:
        logger.warning(f"[TweetSharerBridge] Cannot create DM with reactor {reactor.id}. Aborting.")
        return
    except Exception as e:
        logger.error(f"[TweetSharerBridge] Error creating DM with reactor {reactor.id}: {e}. Aborting.")
        return

    # Construct the main text prompt
    prompt_lines = []
    # Always use the general prompt, even if reactor is the original poster
    prompt_lines.append(f"You reacted to {original_poster.mention}'s message to share it: {message_to_share.jump_url}")
    
    prompt_lines.append("") # Extra blank line for separation
    prompt_lines.append("What comment would you like to leave with the tweet? Reply with your comment, or type `n` if you don't want to add one.")
    prompt_lines.append("") # Blank line before original post text details

    cleaned_content = message_to_share.content.strip() if message_to_share.content else ""
    text_limit = 1500 if reactor.id == original_poster.id else 1000

    if cleaned_content:
        # If content itself is a URL, don't put it in backticks, let it flow.
        # Backticks are more for pre-formatted text or code, not for general content display if it might be a link.
        is_content_url = cleaned_content.startswith("http://") or cleaned_content.startswith("https://")
        if is_content_url and len(cleaned_content.split()) == 1: # Content is likely just a single URL
             prompt_lines.append(f"Original Post Text:\n{cleaned_content[:text_limit]}")
        else:
            prompt_lines.append(f"Original Post Text:\n```{cleaned_content[:text_limit]}```")
    else:
        prompt_lines.append("Original Post Text:\n[No text content]")
    
    main_prompt_message_content = "\n".join(prompt_lines)

    try:
        # Send the main text part of the prompt
        await reactor_dm_channel.send(main_prompt_message_content)

        # If there are attachments, send a header and then each attachment URL as a new message
        if message_to_share.attachments:

            
            for att in message_to_share.attachments:
                temp_filename = None # Initialize for cleanup
                try:
                    # Try sending as a file first
                    # Ensure filename is safe and doesn't cause path traversal if used directly from user input
                    # For simplicity, using a fixed prefix and att.id or a random component if filename is risky.
                    # att.filename should be generally safe but let's be cautious.
                    safe_filename_part = re.sub(r'[^a-zA-Z0-9._-]', '_', att.filename) # Basic sanitization
                    temp_filename = f"temp_media_{att.id}_{safe_filename_part}"[:200] # Max filename length considerations
                    
                    await att.save(fp=temp_filename)
                    logger.debug(f"[TweetSharerBridge] Attachment {att.filename} saved to {temp_filename}")
                    with open(temp_filename, 'rb') as fp:
                        discord_file = discord.File(fp, filename=att.filename) # Use original filename for display
                        await reactor_dm_channel.send(file=discord_file)
                    logger.info(f"[TweetSharerBridge] Sent attachment {att.filename} as a file to reactor {reactor.id}.")
                except Exception as e_file_send:
                    logger.error(f"[TweetSharerBridge] Failed to send attachment {att.filename} as file: {e_file_send}. Falling back to URL.")
                    try:
                        await reactor_dm_channel.send(att.url.strip()) 
                    except discord.HTTPException as e_att_url:
                        logger.warning(f"[TweetSharerBridge] Could not send attachment URL {att.url} (fallback) to reactor {reactor.id}: {e_att_url}")
                        await reactor_dm_channel.send(f"(Could not display attachment: {att.filename} - Link: <{att.url.strip()}>)")
                finally:
                    if temp_filename and os.path.exists(temp_filename):
                        try:
                            os.remove(temp_filename)
                            logger.debug(f"[TweetSharerBridge] Cleaned up temporary file: {temp_filename}")
                        except Exception as e_cleanup:
                            logger.error(f"[TweetSharerBridge] Error cleaning up temp file {temp_filename}: {e_cleanup}")
        
        logger.info(f"[TweetSharerBridge] Sent comment prompt elements to reactor {reactor.id} for message {message_to_share.id}.")

    except discord.HTTPException as e:
        logger.error(f"[TweetSharerBridge] Failed to send initial comment prompt parts to reactor {reactor.id}: {e}")
        # Fallback logic for very long main_prompt_message_content
        if len(main_prompt_message_content) > 1900:
            try:
                simplified_prompt = f"You reacted to a message to share it ({message_to_share.jump_url}). What comment would you like to leave? (Original content was too long to display fully)."
                await reactor_dm_channel.send(simplified_prompt)
                # Even in simplified prompt, try to send actual files if possible
                if message_to_share.attachments:
                    media_header_text = "Media from original post:"
                    await reactor_dm_channel.send("-"*20)
                    await reactor_dm_channel.send(media_header_text)
                    for att in message_to_share.attachments:
                        temp_filename_fallback = None
                        try:
                            safe_filename_part_fb = re.sub(r'[^a-zA-Z0-9._-]', '_', att.filename)
                            temp_filename_fallback = f"temp_media_fb_{att.id}_{safe_filename_part_fb}"[:200]
                            await att.save(fp=temp_filename_fallback)
                            with open(temp_filename_fallback, 'rb') as fp:
                                await reactor_dm_channel.send(file=discord.File(fp, filename=att.filename))
                        except Exception:
                            try: await reactor_dm_channel.send(att.url.strip())
                            except discord.HTTPException: 
                                await reactor_dm_channel.send(f"(Could not display attachment: {att.filename} - Link: <{att.url.strip()}>)")
                        finally:
                            if temp_filename_fallback and os.path.exists(temp_filename_fallback):
                                try: os.remove(temp_filename_fallback)
                                except Exception: pass # Suppress cleanup error in this deep fallback
            except discord.HTTPException as e_simple:
                logger.error(f"[TweetSharerBridge] Failed to send even simplified comment prompt DM to reactor {reactor.id}: {e_simple}")
                return
        else: 
            return 
    
    # Wait for reactor's comment
    def check_reactor_reply(m):
        return m.author.id == reactor.id and m.channel.id == reactor_dm_channel.id

    try:
        reactor_reply_message = await bot_instance.wait_for('message', check=check_reactor_reply, timeout=USER_RESPONSE_TIMEOUT)
        reactor_comment = reactor_reply_message.content
        if reactor_comment.strip().lower() == 'n':
            reactor_comment = "" # Set to empty string if user opts out of comment
            logger.info(f"[TweetSharerBridge] Reactor {reactor.id} chose not to add a comment.")
        logger.info(f"[TweetSharerBridge] Received comment from reactor {reactor.id}: '{reactor_comment[:50]}...'")
    except asyncio.TimeoutError:
        # Simplified timeout message as the flow is now always towards asking OP for consent
        timeout_message = "You didn't provide a comment in time, so the process to request sharing from the original author could not proceed."
        logger.info(f"[TweetSharerBridge] Reactor {reactor.id} did not reply with a comment within timeout for message {message_to_share.id}.")
        try:
            await reactor_dm_channel.send(timeout_message)
        except discord.HTTPException: pass
        return
    except Exception as e:
        logger.error(f"[TweetSharerBridge] Error waiting for reactor's comment for message {message_to_share.id}: {e}", exc_info=True)
        try:
            await reactor_dm_channel.send("An unexpected error occurred while waiting for your comment.")
        except discord.HTTPException: pass
        return

    # Check for original poster's content sharing permission
    # allow_content_sharing defaults to TRUE, so:
    # - TRUE or None: can proceed (ask for consent if not explicitly True, or use pre-approved path)
    # - FALSE: explicit opt-out, respect it
    member_data_op = db_handler.get_member(original_poster.id)
    allow_content_sharing_status = member_data_op.get('allow_content_sharing') if member_data_op else None

    # Check for explicit opt-out first
    if allow_content_sharing_status is False:
        logger.info(f"[TweetSharerBridge] User {original_poster.id} has explicitly opted out (allow_content_sharing=False). Aborting for message {message_to_share.id}.")
        try:
            await reactor_dm_channel.send(
                f"The author of the message ({original_poster.mention}) has opted out of content sharing. "
                f"Your request to share {message_to_share.jump_url} cannot be processed."
            )
        except discord.HTTPException:
            logger.warning(f"[TweetSharerBridge] Failed to notify reactor {reactor.id} about OP ({original_poster.id}) opt-out for message {message_to_share.id}.")
        return

    # Check for pre-approved sharing (allow_content_sharing = True)
    if allow_content_sharing_status is True:
        logger.info(f"[TweetSharerBridge] User {original_poster.id} has pre-approved sharing (allow_content_sharing=True). Proceeding with LLM check for message {message_to_share.id} based on reactor {reactor.id}'s comment: '{reactor_comment[:50]}...'.")
        
        await _process_moderation_and_sharing(
            bot_instance=bot_instance,
            logger=logger,
            db_handler=db_handler,
            sharer_instance=sharer_instance,
            llm_client=llm_client,
            message_to_share=message_to_share,
            original_poster=original_poster,
            reactor=reactor,
            reactor_comment=reactor_comment,
            moderation_model_name="claude-sonnet-4-5-20250929",
            path_type="Pre-Approved Path",
            reactor_dm_channel_override=reactor_dm_channel # Pass the already created DM channel
        )
        return # End of pre-approved flow (either tweeted or blocked by LLM)

    # --- Regular Flow: DM Original Poster for Consent ---
    # This section is reached when allow_content_sharing is None (not yet set)
    logger.info(f"[TweetSharerBridge] Proceeding to ask original poster {original_poster.id} for consent for message {message_to_share.id} (allow_content_sharing not yet set).")
    
    original_poster_dm_channel = None
    try:
        original_poster_dm_channel = await original_poster.create_dm()
    except discord.Forbidden:
        logger.warning(f"[TweetSharerBridge] Cannot create DM with original poster {original_poster.id}. Aborting consent process.")
        try: 
            dm_content_op_fail = f"Could not reach {original_poster.mention} to ask for permission. Their DMs might be closed."
            if not hasattr(bot_instance, 'rate_limiter') or not reactor_dm_channel: # Ensure reactor_dm_channel exists
                logger.error(f"[TweetSharerBridge] Rate limiter or reactor_dm_channel not available for OP DM failure notification. Skipping or logging direct send attempt.")
                if reactor_dm_channel: await reactor_dm_channel.send(content=dm_content_op_fail) # Attempt direct if channel exists
            else:
                await discord_utils.safe_send_message(
                    bot_instance, reactor_dm_channel, bot_instance.rate_limiter, logger, content=dm_content_op_fail
                )
        except Exception as e:
            logger.error(f"[TweetSharerBridge] Error creating DM with original poster {original_poster.id}: {e}. Aborting consent process.")
            try: 
                dm_content_op_error = f"An error occurred trying to reach {original_poster.mention} for sharing permission."
                if not hasattr(bot_instance, 'rate_limiter') or not reactor_dm_channel:
                    logger.error(f"[TweetSharerBridge] Rate limiter or reactor_dm_channel not available for OP DM error notification. Skipping or logging direct send attempt.")
                    if reactor_dm_channel: await reactor_dm_channel.send(content=dm_content_op_error)
                else:
                    await discord_utils.safe_send_message(
                        bot_instance, reactor_dm_channel, bot_instance.rate_limiter, logger, content=dm_content_op_error
                    )
            except Exception as e:
                logger.error(f"[TweetSharerBridge] Error creating DM with original poster {original_poster.id}: {e}. Aborting consent process.")
                try: 
                    await reactor_dm_channel.send(f"An unexpected error occurred while trying to send the sharing request to {original_poster.mention}.")
                except discord.HTTPException:
                    logger.warning(f"[TweetSharerBridge] Failed to notify reactor about OP main consent DM general error for {original_poster.id}.")
        return
    except Exception as e:
        logger.error(f"[TweetSharerBridge] Error creating DM with original poster {original_poster.id}: {e}. Aborting consent process.")
        try: 
            await reactor_dm_channel.send(f"An error occurred trying to reach {original_poster.mention} for sharing permission.")
        except discord.HTTPException:
            logger.warning(f"[TweetSharerBridge] Failed to notify reactor about OP DM creation error for {original_poster.id}.")
        return

    # Send attachments to Original Poster first, if any
    attachments_were_sent_to_op = False
    if message_to_share.attachments:
        logger.info(f"[TweetSharerBridge] Attempting to send {len(message_to_share.attachments)} attachment(s) to OP {original_poster.id} as part of consent request for message {message_to_share.id}.")
        
        for att_idx, att in enumerate(message_to_share.attachments):
            temp_filename_op = None
            try:
                # Ensure filename is safe and doesn't cause path traversal
                safe_filename_part_op = re.sub(r'[^a-zA-Z0-9._-]', '_', att.filename)
                temp_filename_op = f"temp_op_media_{att.id}_{att_idx}_{safe_filename_part_op}"[:200] # Max filename length considerations
                
                await att.save(fp=temp_filename_op)
                logger.debug(f"[TweetSharerBridge] Attachment {att.filename} saved to {temp_filename_op} for OP DM (consent).")
                with open(temp_filename_op, 'rb') as fp:
                    discord_file = discord.File(fp, filename=att.filename) # Use original filename for display
                    await original_poster_dm_channel.send(file=discord_file)
                logger.info(f"[TweetSharerBridge] Sent attachment {att.filename} as a file to OP {original_poster.id} for consent.")
                attachments_were_sent_to_op = True 
            except discord.HTTPException as e_file_send_op_http:
                 logger.error(f"[TweetSharerBridge] HTTP error sending attachment {att.filename} as file to OP {original_poster.id} for consent: {e_file_send_op_http}. Halting further media sends for this request.")
                 # If DM channel is broken (e.g., user blocked bot mid-flow), stop trying to send more media.
                 # The main consent text will still be attempted.
                 break 
            except Exception as e_file_send_op:
                logger.error(f"[TweetSharerBridge] Failed to send attachment {att.filename} as file to OP {original_poster.id} for consent: {e_file_send_op}. Falling back to URL.")
                try:
                    await original_poster_dm_channel.send(att.url.strip())
                    attachments_were_sent_to_op = True
                    logger.info(f"[TweetSharerBridge] Sent attachment URL {att.url} (fallback) to OP {original_poster.id} for consent.")
                except discord.HTTPException as e_att_url_op_http:
                    logger.warning(f"[TweetSharerBridge] HTTP error sending attachment URL {att.url} (fallback) to OP {original_poster.id} for consent: {e_att_url_op_http}. Halting further media sends.")
                    break
                except Exception as e_att_url_op_generic:
                     logger.error(f"[TweetSharerBridge] Generic error sending attachment URL {att.url} (fallback) to OP {original_poster.id} for consent: {e_att_url_op_generic}")
                     # Attempt to send a textual placeholder if URL sending also fails catastrophically
                     try:
                        await original_poster_dm_channel.send(f"(Could not display attachment: {att.filename} - Link: <{att.url.strip()}>)")
                     except Exception:
                        logger.error(f"[TweetSharerBridge] Failed to send even textual placeholder for attachment {att.filename} to OP {original_poster.id}.")
            finally:
                if temp_filename_op and os.path.exists(temp_filename_op):
                    try:
                        os.remove(temp_filename_op)
                        logger.debug(f"[TweetSharerBridge] Cleaned up OP temp file for consent: {temp_filename_op}")
                    except Exception as e_cleanup_op:
                        logger.error(f"[TweetSharerBridge] Error cleaning up OP temp file {temp_filename_op} for consent: {e_cleanup_op}")
        
        if attachments_were_sent_to_op:
             logger.info(f"[TweetSharerBridge] Finished attempting to send media to OP {original_poster.id} for consent on message {message_to_share.id}.")
        else:
             logger.warning(f"[TweetSharerBridge] No attachments were successfully sent/displayed to OP {original_poster.id} for consent on message {message_to_share.id}, despite attachments being present.")

    # Construct the consent prompt text, aware of whether attachments were displayed
    prompt_details_line = "The tweet would include your original message content"
    if message_to_share.attachments:
        if attachments_were_sent_to_op:
            prompt_details_line += " and the media shown above (if it displayed correctly)."
        else: # Attachments exist but attempt to send them might have failed
            prompt_details_line += " and any attachments (we attempted to display them to you)."
    else: # No attachments on the message
        prompt_details_line += "."

    consent_prompt_text = (
        f"{reactor.mention} (display name: {reactor.display_name}) would like to share your content from {message_to_share.jump_url} on Twitter.\n"
        f"They've included the following comment:\n\n"
        f"> {reactor_comment}\n\n"
        f"{prompt_details_line}"
    )
    
    view = ConsentView(
        original_poster=original_poster, reactor=reactor, reactor_comment=reactor_comment, 
        message_to_share=message_to_share, db_handler=db_handler, 
        sharer_instance=sharer_instance, logger=logger, bot_instance=bot_instance, llm_client=llm_client # Pass llm_client
    )

    try:
        consent_dm_message = await original_poster_dm_channel.send(content=consent_prompt_text, view=view)
        view.message = consent_dm_message 
        logger.info(f"[TweetSharerBridge] Sent consent text and view to original poster {original_poster.id} for message {message_to_share.id}.")
        await reactor_dm_channel.send(f"A request to share has been sent to {original_poster.mention}. They will be asked for permission.")
    except discord.HTTPException as e:
        logger.error(f"[TweetSharerBridge] Failed to send main consent DM (text/view) to original poster {original_poster.id}: {e}")
        try: 
            await reactor_dm_channel.send(f"Failed to send the sharing request to {original_poster.mention}. They may have DMs disabled or an error occurred.")
        except discord.HTTPException:
             logger.warning(f"[TweetSharerBridge] Failed to notify reactor about OP main consent DM send failure for {original_poster.id}.")
    except Exception as e:
        logger.error(f"[TweetSharerBridge] Unexpected error sending main consent DM (text/view) to OP {original_poster.id}: {e}", exc_info=True)
        try: 
            await reactor_dm_channel.send(f"An unexpected error occurred while trying to send the sharing request to {original_poster.mention}.")
        except discord.HTTPException:
            logger.warning(f"[TweetSharerBridge] Failed to notify reactor about OP main consent DM general error for {original_poster.id}.")

# Notes at the end remain the same regarding Sharer and DatabaseHandler assumptions. 
