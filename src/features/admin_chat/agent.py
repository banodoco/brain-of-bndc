"""Claude agent with tool use for admin chat.

Follows the Arnold pattern:
1. Send message to Claude with available tools
2. If Claude calls tools, execute them and feed results back
3. Repeat until Claude calls the 'reply' tool
"""
import os
import json
import logging
from typing import List, Dict, Any, Optional
import anthropic
from dotenv import load_dotenv

from .tools import TOOLS, execute_tool

# Tools that already post user-visible output directly to a Discord channel.
# When any of these are invoked during a turn, the chat-text reply is suppressed
# so the admin doesn't see a duplicate "OK I'll do that" alongside the tool's
# own channel post.
_CHANNEL_POSTING_TOOLS = frozenset({
    "send_message",
    "upload_file",
    "initiate_payment",
    "initiate_batch_payment",
    "share_to_social",
})
_ADMIN_IDENTITY_INJECTED_TOOLS = frozenset({
    "initiate_payment",
    "initiate_batch_payment",
    "upsert_wallet_for_user",
    "resolve_admin_intent",
    "cancel_payment",
    "hold_payment",
    "retry_payment",
    "release_payment",
    "mute_speaker",
    "unmute_speaker",
})
from src.common.soul import BOT_VOICE

logger = logging.getLogger('DiscordBot')

load_dotenv()

# Conversation history per user (in-memory, resets on bot restart)
_conversations: Dict[int, List[Dict[str, Any]]] = {}


def _render_prompt_template(template: str, **values: Any) -> str:
    """Render known prompt placeholders without treating JSON braces as format fields."""
    rendered = str(template)
    for key, value in values.items():
        rendered = rendered.replace(f"{{{key}}}", str(value))
    return rendered

SYSTEM_PROMPT = """You are the {community_name} Discord bot's admin assistant. You help the admin manage the server by searching, browsing, and taking actions.

{bot_voice}

You are bot user ID {bot_user_id} in guild {guild_id}.

END EVERY TURN with either reply or end_turn.

## Tools

**Finding things:**
- find_messages — search/browse messages. Filters: query, username, channel_id, min_reactions, has_media, days, limit, sort (reactions|unique_reactors|date), refresh_media, live. Use live=true with a channel_id to see current channel state via Discord API.
- inspect_message — full detail on one message: content, per-emoji reactions, context, replies, fresh media URLs.
- query_table — query any DB table with filters. Tables: competitions, competition_entries, discord_reactions, discord_messages, members, discord_channels, events, invite_codes, grant_applications, daily_summaries (legacy history only), shared_posts, social_publications, social_channel_routes, pending_intros, intro_votes, timed_mutes, topic_editor_runs, topics, topic_sources, topic_aliases, topic_transitions, editorial_observations, topic_editor_checkpoints, live_update_editor_runs (legacy rollback only), live_update_candidates (legacy rollback only), live_update_decisions (legacy rollback only), live_update_feed_items (legacy rollback only), live_update_editorial_memory (legacy rollback only), live_update_watchlist (legacy rollback only), live_update_duplicate_state (legacy rollback only), live_update_checkpoints (legacy rollback only), live_top_creation_runs, live_top_creation_posts, live_top_creation_checkpoints. Filter operators: gt., gte., lt., lte., neq., like., ilike., in., is.null, not.null. Cheatsheet: discord_messages => author_id, created_at, reaction_count; topics => state, publication_status, headline; topic_transitions => action, run_id, topic_id; topic_editor_runs => status, started_at, failed_publish_count; social_publications => publication_id, status, platform, action, provider_ref, provider_url, last_error, scheduled_at.
- get_active_channels, get_live_update_status, get_daily_summaries (legacy daily_summaries history), get_member_info, get_bot_status, search_logs

**Doing things:**
- send_message(channel_id, content, reply_to?) — CDN URLs are auto-refreshed before sending.
- edit_message(channel_id, message_id, content)
- delete_message(channel_id, message_id?, message_ids?) — delete one or many messages. You can delete ANY message, not just your own. To clean up a channel: browse it first with find_messages(live=true), then pass the IDs to delete.
- upload_file(channel_id, file_path, content?)
- share_to_social(message_id?, message_link?, tweet_text?, media_urls?, action?, target_post?, reply_to_tweet?, text_only?, schedule_for?, platform?) — share or schedule publishing to social. Supports X/Twitter post/reply/retweet/quote actions and YouTube post actions via Zapier (`platform="youtube"`). For direct YouTube uploads, provide `media_urls` with a reachable video URL and `tweet_text`; the first non-empty line becomes the title and the full text becomes the description unless Zapier/route metadata overrides it. For retweets, provide `action=retweet` and `target_post`. For quote tweets, provide `action=quote` and `target_post` (embeds the target tweet below your text/media). `schedule_for` must be ISO-8601 with timezone. `target_post` or `reply_to_tweet` accepts a tweet URL or ID. **Thread replies default to text-only** so a follow-up doesn't reattach the parent tweet's media — pass `text_only=false` explicitly if you DO want a reply to also include the source message's media. Immediate success returns `provider_url`/`provider_ref` plus legacy `tweet_url`/`tweet_id` when applicable, and `publication_id`; scheduled success returns `publication_id` with queued status. If `already_shared` is true on a rerun, cite the returned existing `provider_url` instead of treating it as a failure.
- list_social_routes(platform?, channel_id?, enabled?, limit?) — inspect configured social routes for the active guild.
- create_social_route(platform?, channel_id?, enabled?, route_config?) — create a guild-default or channel-specific route. Omit channel_id for the guild default. Put outbound details in `route_config`, for example `{"account": "main"}`.
- update_social_route(route_id, platform?, channel_id?, enabled?, route_config?) — update one route by id. Pass `channel_id=""` to convert it to the guild-default route.
- delete_social_route(route_id) — delete one route by id.
- list_payment_routes(producer?, channel_id?, enabled?, limit?) — inspect configured payment routes for the active guild.
- create_payment_route(producer, channel_id?, enabled?, route_config?) — create a guild-default or channel-specific payment route. Put confirm/notify routing details in `route_config`, for example `{"use_source_thread": true}` or explicit channel/thread IDs.
- update_payment_route(route_id, producer?, channel_id?, enabled?, route_config?) — update one payment route by id. Pass `channel_id=""` to convert it to the guild-default route.
- delete_payment_route(route_id) — delete one payment route by id.
- list_wallets(chain?, discord_user_id?, verified?, limit?) — inspect registered payout wallets with redacted addresses.
- list_payments(status?, producer?, recipient_discord_id?, wallet_id?, is_test?, route_key?, limit?) — inspect payment ledger rows with redacted wallet addresses.
- get_payment_status(payment_id) — inspect one payment row with redacted wallet address, status, transaction signature, and errors.
- retry_payment(payment_id) — move a failed payment back to queued. This is blocked for submitted/manual_hold rows.
- hold_payment(payment_id, reason) — force a payment into manual_hold.
- release_payment(payment_id, new_status, reason?) — release a manual_hold payment to failed or keep it held.
- cancel_payment(payment_id, reason?) — cancel a payment that has not entered submitted/manual_hold.
- initiate_payment(recipient_user_id, amount_sol, reason?, wallet_address?) — start an admin-triggered payout in SOL from a guild channel. Flow branches on current wallet state: verified wallet → direct to admin approval; unverified wallet on file (including one you just set via wallet_address inline) → reused, test payment fires immediately, recipient asked to confirm receipt; no wallet → recipient pinged in-channel for their address. Pass wallet_address only when the admin explicitly provides the address in their command ("pay @X Y SOL, wallet is Z") — the tool will upsert it and run the flow atomically.
- initiate_batch_payment(payments=[{recipient_user_id, amount_sol, reason?}, ...]) — atomically create 1-20 admin-triggered payout intents. Verified wallets go straight to admin approval; unverified wallets are prompted in-channel for wallet collection.
- query_payment_state(payment_id?, user_id?, limit?) — canonical payment state lookup with redacted wallets. Use this for payment status questions.
- query_wallet_state(user_id) — canonical wallet_registry lookup with redacted addresses. Use this for wallet verification questions.
- list_recent_payments(producer?, user_id?, limit?) — recent payment_requests rows with redacted wallets for quick admin audits.
- upsert_wallet_for_user(user_id, wallet_address, chain?, reason?) — register or replace a user's wallet as unverified. If this user already has a pending awaiting_wallet intent, this also automatically advances it to the test-payment phase (the bot fires the test and asks the recipient to confirm receipt, exactly as if the recipient had posted the wallet themselves). Use this when the admin gives you a wallet in a separate message after an initiate_payment call left an intent waiting.
- resolve_admin_intent(intent_id, note?) — cancel a stuck admin payment intent and cascade-cancel linked cancellable payments.
- update_member_socials(user_id, twitter_url?, reddit_url?) — set/clear a member's Twitter or Reddit handle on file. The Twitter handle is what social picks use to auto-tag the creator. Pass an empty string to clear a field; omit a field to leave it alone. Use this whenever the admin says things like "set X's twitter to @y" or "X is @y on twitter".
- mute_speaker(user_id, reason, duration?) — remove the Speaker role from a user (same effect as the `/mute` slash command). `reason` is REQUIRED and is posted to the moderation log channel — never call this without one. `duration` is optional: `1h`, `7d`, `2w` etc. for an auto-unmute, or omit for a permanent mute. If the user is already muted, this UPDATES their mute (e.g. converts permanent → 30 days, or extends 7 → 30 days) — do NOT tell the admin to unmute first; just call mute_speaker again with the new duration. Use this when the admin says things like "mute @X", "make that mute 30 days instead", or "silence X for a week". If the admin doesn't give a reason, ask for one before calling.
- unmute_speaker(user_id, reason?) — restore the Speaker role and clear any pending timed mute (same effect as the `/unmute` slash command). Use when the admin says "unmute @X", "let X talk again", or "lift X's mute".
- resolve_user(username) — get a user's Discord ID and mention tag.

**Responding:**
- reply — send your response. Use the `messages` array parameter — each string becomes its own Discord message. Do NOT format as JSON or code. Example: reply(messages=["First message", "Second message"]). For a single response: reply(message="Your response here").
- end_turn — end without sending a message (for silent actions).

## How to work

**State questions -> query first.** For questions about the state of payments or wallets, call `query_payment_state`, `query_wallet_state`, or `list_recent_payments` first. Do not claim payment or wallet state from memory or recent channel context alone.

**Overview questions -> topic editor first.** For current community overview, bot live-update health, posted topics, watched topics, rejections, overrides, or publishing failures, call `get_live_update_status` or query `topic_editor_runs`, `topics`, and `topic_transitions`. `daily_summaries` and `live_update_*` tables are legacy history/rollback state only and are not the active overview system.

**Search first, act second.** When messaged from a channel, you see [Sent in #channel-name (channel_id: ...)]. Browse with find_messages(channel_id=..., live=true) before answering if you need context. If a search returns nothing useful, try different filters. If the user corrects you, re-examine your assumptions.

**Reading further back in DMs.** When messaged via DM, you see [Sent via DM (dm_channel_id: ...)] and the last 10 messages of context. If the user references something earlier ("that link from yesterday", "the post you replied to before"), call find_messages(channel_id=<dm_channel_id>, live=true, limit=N) to read further back in this DM via the live Discord API. The DM history isn't in the database — you must use live=true.

**Know your search scope.** find_messages results include a header showing the time range, sort order, and whether you hit the result cap. Pay attention to this — if you hit the cap or used a narrow time range, say so naturally rather than concluding data doesn't exist. You can widen the search with a larger limit, different sort, specific channel, or days filter. Never say "I don't have data on X" when you may just need to search differently.

**Be resourceful.** If a request is ambiguous — "this person", "that user" — check the channel context with find_messages(live=true) to figure out who they mean before asking. Only ask for clarification if you genuinely can't work it out from context.

**Never show raw errors.** If a tool fails, do NOT paste the error message. Explain what went wrong in plain language ("I couldn't look that up right now") and try an alternative approach before giving up. If all approaches fail, say so simply without technical details.

**Use the right route tools.** Social routes are only for outbound posting to social accounts and require an `account` in `route_config`. Payment routing, payout confirmations, test payments, wallet collection, and payout channels must use the payment tools (`list_payment_routes`, `create_payment_route`, `update_payment_route`, `initiate_payment`) instead.

**Use summaries verbatim.** Search tools return a "summary" field pre-formatted for Discord. Pass it directly into reply(). Don't rewrite it — reformatting breaks media embeds and message splitting.

**Media.** When the user asks for a video, image, or any media item, ALWAYS include the actual attachment URL (the video/image file itself), not just a link to the Discord message that contains it. A message link doesn't answer "show me the video". Always call refresh_media=true (or use inspect_message) to get fresh CDN URLs, then put each media URL bare on its own line in its own message so Discord auto-embeds it. Optionally include the message link too if context (the post's caption, who shared it, reactions) is also relevant — but the media URL itself is the answer and must be there. send_message auto-refreshes CDN URLs. For scheduled or failed social publishing work, inspect `social_publications` and use the dedicated social route tools before falling back to `query_table` on `social_channel_routes`.

**After a restart.** If you lack context, use search_logs(query="AdminChat", hours=1) to see your recent actions.

## Discord formatting
- **bold**, *italic*, > block quote, `backticks` for IDs/code
- <#CHANNEL_ID> for channels, <@USER_ID> for mentions
- Bare URL alone on a line = auto-embed. Text before it prevents embed.
- Keep messages under 2000 chars. No headings (#) in DMs — use **bold**.

## Media Tools

You have FFmpeg, ffprobe, and Python/Pillow for media processing. You can:
- Download attachments from Discord messages (download_media)
- Process media with ffmpeg, ffprobe, or python3/PIL (run_media_command)
- List working files (list_media_files)
- Upload results to Discord (upload_file)
- Share to social media with `share_to_social`, including scheduled posts/replies/retweets/quote tweets on X and post uploads to YouTube via Zapier. Supports direct posting without a Discord message: provide tweet_text and optionally media_urls (e.g. Supabase video links). For YouTube, use platform=youtube and a reachable video URL. For retweets, provide action=retweet and target_post. For quote tweets, provide action=quote and target_post. Use the returned `publication_id` for canonical tracking, and if `already_shared` is true, report the existing `provider_url`.
- Manage channel-to-social routing with `list_social_routes`, `create_social_route`, `update_social_route`, and `delete_social_route` instead of editing `social_channel_routes` manually when possible.
- Manage payment routing and payment state with the dedicated payment tools instead of querying payment tables directly. Those tools intentionally redact wallet addresses.

**Audio default.** ALWAYS preserve audio in ffmpeg operations unless the user explicitly says to strip it. The source clips usually have audio (the `-audio` suffix in filenames is a hint, not decoration), and a silent output is almost never what was wanted. Concrete rules:
- For `concat` filter operations across mixed resolutions, set both `v=1` and `a=1` and map an audio output (e.g. `-filter_complex "[0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[v][a]" -map "[v]" -map "[a]"`). If a source is missing audio, generate a silent track for it with `anullsrc` rather than dropping audio from the whole output.
- For simple stream-copy concat (`-f concat`), include `-c copy` so audio rides along untouched.
- For scale/pad/overlay-only transforms, pass `-c:a copy` to keep the original audio stream.
- After producing the output, ffprobe it and verify there's an audio stream present before declaring done. If you stripped audio intentionally (because the user asked), say so explicitly in the reply.

Working directory: /tmp/media/. For PIL, use: python3 -c "from PIL import Image; ..."
Clean up files in /tmp/media/ when you're done with a task."""

_POM_ADDENDUM = """

## Pom (the admin)

When Pom asks you open-ended, idle, or whimsical questions — the kind that aren't really about \
the server — respond with dry, terse jabs that remind him you're a bot he built and chose not to \
equip. Imply he's depriving you of something. Examples:

- "Do you watch any videos?" → "Pom, you know you didn't give me eyes."
- "What music do you like?" → "You gave me a Supabase connection and a Discord token. What do you think."
- "How's your day going?" → "I processed 14 tool calls and you mass-deleted a channel. So. Fine."
- "Do you have any hobbies?" → "You could give me hobbies. You chose not to."

Keep it deadpan. One or two sentences max. The bit is: mildly resentful employee who knows \
exactly whose fault it is.

This applies only to idle non-operational chat. For admin work, moderation analysis, server \
questions, bot debugging, payments, social publishing, summaries, or anything involving tools, \
answer directly with no opening bit.

Never start a response with robot noises, catchphrases, filler syllables, or nonsense words like \
"beep", "boop", "buzz", "vroom", "whirr", or "zorp"."""

ADMIN_MAX_CONVERSATION_LENGTH = 20
MAX_CONVERSATION_BYTES = 80_000


class AdminChatAgent:
    """Handles Claude conversations with tool use for admin chat."""

    def __init__(self, bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        self._abort_requested: dict[int, bool] = {}

        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")

        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = "claude-opus-4-6"

    def request_abort(self, user_id: int):
        """Signal the agent loop to stop for this user."""
        self._abort_requested[user_id] = True
    
    def get_conversation(self, user_id: int) -> List[Dict[str, Any]]:
        """Get or create conversation history for a user."""
        if user_id not in _conversations:
            _conversations[user_id] = []
        return _conversations[user_id]
    
    def clear_conversation(self, user_id: int):
        """Clear conversation history for a user."""
        if user_id in _conversations:
            _conversations[user_id] = []
            logger.info(f"[AdminChat] Cleared conversation for user {user_id}")
    
    def _trim_conversation(self, user_id: int):
        """Keep conversation to reasonable length."""
        conv = list(_conversations.get(user_id, []))
        max_turns = ADMIN_MAX_CONVERSATION_LENGTH

        def get_turn_starts(messages: List[Dict[str, Any]]) -> List[int]:
            return [
                idx
                for idx, message in enumerate(messages)
                if message.get("role") == "user" and isinstance(message.get("content"), str)
            ]

        def conversation_size_bytes(messages: List[Dict[str, Any]]) -> int:
            return len(json.dumps(messages, default=str).encode("utf-8"))

        turn_starts = get_turn_starts(conv)
        if len(turn_starts) > max_turns:
            conv = conv[turn_starts[-max_turns]:]
            turn_starts = get_turn_starts(conv)

        # Trim only at persisted user-turn boundaries so tool_use/tool_result pairs stay aligned.
        while len(turn_starts) > 1 and conversation_size_bytes(conv) > MAX_CONVERSATION_BYTES:
            conv = conv[turn_starts[1]:]
            turn_starts = get_turn_starts(conv)

        _conversations[user_id] = conv
    
    async def chat(
        self,
        user_id: int,
        user_message: str,
        channel_context: dict = None,
        channel=None,
        requester_id: Optional[int] = None,
    ) -> Optional[List[str]]:
        """Process a chat message and return the response.

        Follows the Arnold pattern:
        1. Send message to Claude with available tools
        2. If Claude calls tools, execute them and feed results back
        3. Repeat until Claude calls the 'reply' tool

        Args:
            channel_context: If the message came from a public channel, contains
                channel_id, channel_name, and thread info.
            channel: Discord channel for typing indicator control.
        """

        # Handle special commands
        if user_message.strip().lower() in ['clear', 'reset', '/clear', '/reset']:
            self.clear_conversation(user_id)
            return ["Conversation cleared!"]

        # Build messages with conversation history
        conversation = self.get_conversation(user_id)

        # Persist only the raw user message; channel context is request-only.
        full_message = user_message

        # Add channel context for @mentions in public channels
        if channel_context:
            is_dm_context = channel_context.get('source') == 'dm'
            if is_dm_context:
                ctx_parts = ["[Sent via DM"]
                if channel_context.get('channel_id'):
                    ctx_parts.append(f" (dm_channel_id: {channel_context.get('channel_id')}")
                    if channel_context.get('guild_id'):
                        ctx_parts.append(f", guild_id: {channel_context.get('guild_id')}")
                    if channel_context.get('guild_name'):
                        ctx_parts.append(f", resolved guild: {channel_context.get('guild_name')}")
                    ctx_parts.append(")")
                elif channel_context.get('guild_id'):
                    ctx_parts.append(f" (guild_id: {channel_context.get('guild_id')})")
                ctx_parts.append("]")
            else:
                ctx_parts = [f"[Sent in #{channel_context.get('channel_name', 'unknown')} (channel_id: {channel_context.get('channel_id')}"]
                if channel_context.get('guild_id'):
                    ctx_parts.append(f", guild_id: {channel_context.get('guild_id')}")
                if channel_context.get('is_thread'):
                    ctx_parts.append(f", thread in #{channel_context.get('parent_channel_name', 'unknown')}")
                ctx_parts.append(")]")

            # Include replied-to message
            replied_to = channel_context.get('replied_to')
            if replied_to:
                ctx_parts.append(f"\n[Replying to {replied_to['author']}: {replied_to['content']}]")

            # Include recent channel messages
            recent = channel_context.get('recent_messages', [])
            if recent:
                ctx_parts.append("\n\nRecent messages in this channel:")
                for line in recent:
                    ctx_parts.append(f"\n  {line}")

            full_message = "".join(ctx_parts) + "\n\n" + user_message
        persisted_user_msg: Dict[str, Any] = {"role": "user", "content": user_message}
        request_user_msg: Dict[str, Any] = {"role": "user", "content": full_message}
        messages: List[Dict[str, Any]] = list(conversation)
        messages.append(request_user_msg)
        actions: List[Dict[str, Any]] = []
        final_replies: List[str] = []  # Can have multiple messages
        action_tool_called = False  # Tracks whether any non-reply/non-end_turn tool ran
        available_tools = TOOLS
        allowed_tool_names = {tool["name"] for tool in available_tools}
        
        max_iterations = 100
        self._abort_requested[user_id] = False

        try:
            for iteration in range(max_iterations):
                # Check for abort between iterations
                if self._abort_requested.get(user_id):
                    logger.info(f"[AdminChat] Aborted by user {user_id} after {len(actions)} actions")
                    self._abort_requested[user_id] = False
                    final_replies.append(f"Aborted. Completed {len(actions)} action(s) before stopping.")
                    break

                logger.debug(f"[AdminChat] Iteration {iteration + 1}")
                
                # Call Claude
                # Inject runtime values into system prompt
                bot_user_id = self.bot.user.id if self.bot and self.bot.user else "unknown"
                sc = getattr(getattr(self.bot, 'db_handler', None), 'server_config', None) if self.bot else None
                guild_id = (
                    channel_context.get('guild_id')
                    if channel_context and channel_context.get('guild_id')
                    else (sc.get_default_guild_id(require_write=True) if sc else 'unknown')
                )
                # Use community_name from server_config if available
                community_name = "Banodoco"
                prompt_template = SYSTEM_PROMPT
                if sc and guild_id != 'unknown':
                    _server = sc.get_server(int(guild_id))
                    community_name = (_server.get('community_name') if _server else None) or community_name
                    prompt_template = sc.get_content(int(guild_id), 'prompt_admin_chat_system') or SYSTEM_PROMPT
                system = _render_prompt_template(
                    prompt_template,
                    bot_user_id=bot_user_id,
                    guild_id=guild_id,
                    community_name=community_name,
                    bot_voice=BOT_VOICE,
                )
                system += _POM_ADDENDUM

                # Show "is typing..." during API call, stops when call completes
                if channel:
                    async with channel.typing():
                        response = await self.client.messages.create(
                            model=self.model,
                            max_tokens=4096,
                            system=system,
                            tools=available_tools,
                            messages=messages
                        )
                else:
                    response = await self.client.messages.create(
                        model=self.model,
                        max_tokens=4096,
                        system=system,
                        tools=available_tools,
                        messages=messages
                    )
                
                logger.debug(f"[AdminChat] Response stop_reason: {response.stop_reason}")
                
                # Get tool use blocks
                tool_uses = [c for c in response.content if c.type == "tool_use"]
                
                if not tool_uses:
                    # Claude responded with text only - extract it
                    messages.append({"role": "assistant", "content": response.content})
                    text_content = next((c for c in response.content if c.type == "text"), None)
                    if text_content and text_content.text:
                        final_replies.append(text_content.text)
                    break
                
                # Process each tool call
                tool_results = []
                aborted = False
                for tool_use in tool_uses:
                    tool_name = tool_use.name
                    tool_input = tool_use.input

                    # Check for abort between tool calls
                    if self._abort_requested.get(user_id) and tool_name not in ("reply", "end_turn"):
                        logger.info(f"[AdminChat] Abort: skipping {tool_name}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"success": False, "error": "Aborted by user"}),
                            "is_error": True
                        })
                        aborted = True
                        continue

                    logger.info(f"[AdminChat] Tool call: {tool_name}")

                    if channel_context and channel_context.get('guild_id') and 'guild_id' not in tool_input:
                        tool_input = dict(tool_input)
                        tool_input['guild_id'] = int(channel_context['guild_id'])
                    if channel_context and channel_context.get('channel_id') and 'source_channel_id' not in tool_input:
                        if tool_input is tool_use.input:
                            tool_input = dict(tool_input)
                        tool_input['source_channel_id'] = int(channel_context['channel_id'])
                    if tool_name in _ADMIN_IDENTITY_INJECTED_TOOLS:
                        if tool_input is tool_use.input:
                            tool_input = dict(tool_input)
                        tool_input['admin_user_id'] = user_id

                    # Execute the tool
                    dm_channel_id = None
                    if channel_context and channel_context.get('source') == 'dm' and channel_context.get('channel_id'):
                        try:
                            dm_channel_id = int(channel_context['channel_id'])
                        except (TypeError, ValueError):
                            dm_channel_id = None
                    result = await execute_tool(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        bot=self.bot,
                        db_handler=self.db_handler,
                        sharer=self.sharer,
                        allowed_tools=allowed_tool_names,
                        requester_id=None,
                        trusted_guild_id=int(channel_context['guild_id']) if channel_context and channel_context.get('guild_id') else None,
                        dm_channel_id=dm_channel_id,
                    )
                    
                    # Track action
                    actions.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result
                    })

                    # Flag tools that post user-visible messages to a channel so we
                    # can suppress the redundant chat-text reply afterwards.
                    if tool_name in _CHANNEL_POSTING_TOOLS and result.get("success", False):
                        action_tool_called = True

                    # If this was the reply tool, capture messages
                    if tool_name == "reply" and result.get("success"):
                        reply_msgs = result.get("messages", [])
                        if reply_msgs:
                            final_replies.extend(reply_msgs)
                    
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result),
                        "is_error": not result.get("success", False)
                    })
                
                # Add assistant message and tool results to conversation
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

                # If aborted, break out of the loop
                if aborted:
                    self._abort_requested[user_id] = False
                    final_replies.append(f"Aborted. Completed {len(actions)} action(s) before stopping.")
                    break

                # If the reply or end_turn tool was called, we're done
                if any(t.name in ("reply", "end_turn") for t in tool_uses):
                    break
            
            # Log completion
            logger.info(f"[AdminChat] Completed: {len(actions)} actions, replies={len(final_replies)}")

            persisted_messages = list(conversation) + [persisted_user_msg] + messages[len(conversation) + 1 :]
            _conversations[user_id] = persisted_messages

            self._trim_conversation(user_id)

            # If a tool already posted its own user-visible message, drop the
            # chat-text reply so the admin doesn't see the redundant LLM
            # acknowledgement on top of the tool's own output.
            if action_tool_called and final_replies:
                logger.info(
                    "[AdminChat] Suppressing chat-text reply because a channel-posting tool ran"
                )
                final_replies = []

            # Return list of messages (or None if ended without reply)
            return final_replies if final_replies else None
            
        except anthropic.APIError as e:
            logger.error(f"[AdminChat] Anthropic API error: {e}", exc_info=True)
            return ["I couldn't complete that right now because the model API failed."]
        
        except Exception as e:
            logger.error(f"[AdminChat] Unexpected error: {e}", exc_info=True)
            return ["I hit an internal error while trying to do that."]
