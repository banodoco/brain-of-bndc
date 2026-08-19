import discord
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.common.soul import BOT_VOICE

# --- Constants for Dispute Resolution Feature ---
DISPUTE_RESOLUTION_INITIATOR_ID = 301463647895683072
DISPUTE_RESOLUTION_LLM_SYSTEM_PROMPT = """Please analyze the provided messages to understand the interactions between the members. If there appears to be a disagreement, comment on how the people were relating such that it came to this point - who seemed to be instigating it, who was responding, and who could have diffused it? More importantly, suggest specific, actionable steps all involved parties could have taken, or could still take, to reach an amicable resolution. Frame your suggestions in a constructive and neutral tone. Explain it but don't be too verbose. Remind them that the community is centered around a shared passion for AI and its creative potential, and encourage them to find common ground and move forward positively.

{bot_voice}"""
DISPUTE_RESOLUTION_LLM_CLIENT = "openai"
DISPUTE_RESOLUTION_LLM_MODEL = "o3-mini"
DISPUTE_RESOLUTION_LLM_MAX_TOKENS = 10024
DISPUTE_RESOLUTION_TIMESPAN_HOURS = 12


def _get_dispute_prompt(db_handler, guild_id: Optional[int]) -> str:
    sc = getattr(db_handler, 'server_config', None) if db_handler else None
    prompt = sc.get_content(guild_id, 'prompt_dispute_resolution') if sc and guild_id else None
    community_name = "the community"
    if sc and guild_id:
        server = sc.get_server(guild_id)
        if server:
            community_name = server.get('community_name') or community_name
    return (prompt or DISPUTE_RESOLUTION_LLM_SYSTEM_PROMPT).replace("{bot_voice}", BOT_VOICE).replace("the community", community_name)

async def handle_initiate_dispute_resolution(
    message: discord.Message,
    db_handler, # Expected: DatabaseHandler instance
    get_llm_response_func, # Expected: get_llm_response function
    logger, # Expected: logging.Logger instance
    dev_mode: bool = False
):
    """Initiates dispute resolution by fetching messages and sending to LLM."""
    logger.info(f"[Reactor][DisputeResolver] Action 'initiate_dispute_resolution' triggered by message {message.id} from user {message.author.id}.")

    mentioned_discord_users = [m for m in message.mentions if not m.bot and m.id != DISPUTE_RESOLUTION_INITIATOR_ID]
    if logger.isEnabledFor(logging.DEBUG):
        log_mentioned_users = [(u.id, u.display_name) for u in mentioned_discord_users]
        logger.debug(f"[Reactor][DisputeResolver] Raw mentioned Discord users (ID, DisplayName) after filtering: {log_mentioned_users}")

    if not mentioned_discord_users:
        logger.info(f"[Reactor][DisputeResolver] No relevant users mentioned in message {message.id} for dispute resolution.")
        try:
            await message.reply("Please mention the users involved in the dispute to analyze their recent messages.")
        except Exception as e:
            logger.warning(f"[Reactor][DisputeResolver] Could not send 'no users mentioned' feedback: {e}")
        return

    author_ids_to_fetch = [user.id for user in mentioned_discord_users]
    logger.debug(f"[Reactor][DisputeResolver] Mentioned user IDs for dispute resolution: {author_ids_to_fetch}")

    thread_target_message = message
    thread_name_participants = " & ".join(sorted([user.display_name for user in mentioned_discord_users]))
    thread_name = f"Dispute Analysis - {thread_name_participants}"
    if len(thread_name) > 100:
        thread_name = thread_name[:97] + "..."
    
    analysis_thread: discord.Thread | None = None
    try:
        logger.info(f"[Reactor][DisputeResolver] Creating thread for dispute resolution: '{thread_name}' from message {thread_target_message.id}")
        if thread_target_message.guild and thread_target_message.id in [t.id for t in thread_target_message.guild.threads]:
            logger.info(f"[Reactor][DisputeResolver] Message {thread_target_message.id} already has a thread. Attempting to fetch it.")
            fetched_channel = await thread_target_message.guild.fetch_channel(thread_target_message.id) # type: ignore
            if isinstance(fetched_channel, discord.Thread):
                analysis_thread = fetched_channel
            else:
                logger.warning(f"[Reactor][DisputeResolver] Fetched channel {thread_target_message.id} is not a Thread. Creating new one.")
        
        if not analysis_thread:
             analysis_thread = await thread_target_message.create_thread(name=thread_name, auto_archive_duration=1440)
        logger.info(f"[Reactor][DisputeResolver] Successfully created/selected thread: {analysis_thread.name} (ID: {analysis_thread.id})")
        await analysis_thread.send(f"Starting dispute analysis for {thread_name_participants}. Fetching messages from the last {DISPUTE_RESOLUTION_TIMESPAN_HOURS} hours...")
    except discord.HTTPException as e:
        logger.error(f"[Reactor][DisputeResolver] Failed to create thread for dispute resolution: {e}", exc_info=True)
        await message.reply(f"Sorry, I couldn't create a thread for the analysis. Error: {e.args[0] if e.args else 'Unknown HTTP Error'}")
        return
    except Exception as e:
        logger.error(f"[Reactor][DisputeResolver] Unexpected error creating thread: {e}", exc_info=True)
        await message.reply("An unexpected error occurred while trying to create a thread for the analysis.")
        return

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=DISPUTE_RESOLUTION_TIMESPAN_HOURS)
    guild_id = getattr(message.guild, 'id', None)
    start_time_iso_for_log = start_time.isoformat()
    end_time_iso_for_log = end_time.isoformat()
    logger.debug(f"[Reactor][DisputeResolver] Database query time window: Start='{start_time_iso_for_log}', End='{end_time_iso_for_log}'")

    try:
        if not db_handler:
            logger.error("[Reactor][DisputeResolver] Database handler not available for dispute resolution action.")
            await analysis_thread.send("Internal error: Database connection is missing.")
            return

        recent_messages = await asyncio.to_thread(
            db_handler.get_messages_by_authors_in_range,
            author_ids_to_fetch,
            start_time,
            end_time,
            guild_id,
        )
        logger.debug(f"[Reactor][DisputeResolver] Database returned {len(recent_messages)} messages for authors {author_ids_to_fetch}.")
    except Exception as e:
        logger.error(f"[Reactor][DisputeResolver] Error fetching messages from database for dispute resolution: {e}", exc_info=True)
        await analysis_thread.send("Sorry, I encountered an error fetching recent messages for analysis.")
        return

    if not recent_messages:
        logger.info(f"[Reactor][DisputeResolver] No recent messages found for mentioned users within the last {DISPUTE_RESOLUTION_TIMESPAN_HOURS} hours.")
        await analysis_thread.send(f"No recent messages found for the mentioned users in the last {DISPUTE_RESOLUTION_TIMESPAN_HOURS} hours to analyze.")
        return

    logger.info(f"[Reactor][DisputeResolver] Found {len(recent_messages)} messages for dispute resolution analysis.")

    author_details_cache = {user.id: user.display_name for user in mentioned_discord_users}
    all_author_ids_in_messages = set(msg['author_id'] for msg in recent_messages)

    for aid in all_author_ids_in_messages:
        if aid not in author_details_cache:
            try:
                if message.guild:
                    member = await message.guild.fetch_member(aid)
                    author_details_cache[aid] = member.display_name
                else:
                    member_data = await asyncio.to_thread(db_handler.get_member, aid)
                    author_details_cache[aid] = member_data.get('username', str(aid)) if member_data else str(aid)
            except discord.NotFound:
                logger.warning(f"[Reactor][DisputeResolver] Could not find member {aid} in guild. Falling back to DB.")
                member_data = await asyncio.to_thread(db_handler.get_member, aid)
                author_details_cache[aid] = member_data.get('username', str(aid)) if member_data else str(aid)
            except Exception as e:
                logger.warning(f"[Reactor][DisputeResolver] Could not fetch member data for author ID {aid}: {e}")
                author_details_cache[aid] = str(aid)

    formatted_message_lines = []
    for msg in recent_messages:
        author_name = author_details_cache.get(msg['author_id'], str(msg['author_id']))
        timestamp_str = msg.get('created_at', '')
        readable_time = timestamp_str
        try:
            dt_obj = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            readable_time = dt_obj.strftime('%Y-%m-%d %H:%M:%S UTC')
        except ValueError:
            logger.debug(f"[Reactor][DisputeResolver] Could not parse timestamp {timestamp_str}, using as is.")
        
        content = msg.get('content', '')
        line = f"{author_name} ({readable_time}): {content}"
        formatted_message_lines.append(line)

    llm_input_content = "\n".join(formatted_message_lines)
    
    if dev_mode or logger.isEnabledFor(logging.DEBUG):
        preview_len = 500
        content_preview = llm_input_content[:preview_len] + ("..." if len(llm_input_content) > preview_len else "")
        logger.debug(f"[Reactor][DisputeResolver] LLM input for dispute resolution (preview):\n{content_preview}")

    llm_messages = [{"role": "user", "content": llm_input_content}]

    try:
        logger.info(f"[Reactor][DisputeResolver] Calling LLM for dispute resolution (model: {DISPUTE_RESOLUTION_LLM_MODEL})...")
        response_text = await get_llm_response_func(
            client_name=DISPUTE_RESOLUTION_LLM_CLIENT,
            model=DISPUTE_RESOLUTION_LLM_MODEL,
            system_prompt=_get_dispute_prompt(db_handler, guild_id),
            messages=llm_messages,
            max_completion_tokens=DISPUTE_RESOLUTION_LLM_MAX_TOKENS,
            reasoning_effort="high"
        )
        logger.info(f"[Reactor][DisputeResolver] Dispute resolution analysis received from LLM for message {message.id}.")
        logger.info(f"[Reactor][DisputeResolver] LLM Dispute Resolution Analysis:\n{response_text[:1000]}...")

        if response_text:
            max_chars = 1900 
            response_chunks = [response_text[i:i+max_chars] for i in range(0, len(response_text), max_chars)]
            for chunk_num, chunk in enumerate(response_chunks):
                reply_content = f"**Dispute Resolution Analysis (Part {chunk_num+1}/{len(response_chunks)}):**\n{chunk}"
                await analysis_thread.send(reply_content)
        else:
            await analysis_thread.send("The analysis returned an empty response.")

    except Exception as e:
        logger.error(f"[Reactor][DisputeResolver] Error during LLM call for dispute resolution: {e}", exc_info=True)
        try:
            await analysis_thread.send(f"Sorry, I encountered an error trying to analyze the dispute. ({type(e).__name__})")
        except Exception as send_error:
            logger.error(f"[Reactor][DisputeResolver] Failed to send error message to thread: {send_error}") 
