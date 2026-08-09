from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from src.common.llm import get_llm_response
from src.common.soul import BOT_VOICE
from src.features.gating.intro_embed import build_application_embed, extract_approval_request_marker

logger = logging.getLogger('DiscordBot')

APPROVAL_POLL_INTERVAL_SECONDS = 30
APPROVAL_POLL_BATCH = 25
RECONCILE_HISTORY_LIMIT = 100
RECONCILE_HISTORY_HOURS = 1
STAMP_INLINE_RETRIES = 1

# ── Intro review routing ──
# Primary + fallback LLM routes used to review new introductions, tried in
# failover order. DeepSeek is primary: it's cheap and fast for this small
# classification. The fallback covers transient DeepSeek outages.
INTRO_REVIEW_PRIMARY_CLIENT = "deepseek"
INTRO_REVIEW_PRIMARY_MODEL = "deepseek-v4-flash"
INTRO_REVIEW_FALLBACK_CLIENT = "openai"
INTRO_REVIEW_FALLBACK_MODEL = "gpt-4o-mini"
INTRO_REVIEW_CONTEXT_MESSAGES = 16  # member's prior messages + bot replies included as context
INTRO_REVIEW_CONCURRENCY = 5        # max concurrent review calls
DEFAULT_HELP_CHANNEL_ID = 1163250319107555388  # #support


def _intro_review_routes() -> list[tuple[str, str]]:
    """Return the configured intro-review routes in failover order.

    Env vars INTRO_REVIEW_PRIMARY_CLIENT/MODEL and
    INTRO_REVIEW_FALLBACK_CLIENT/MODEL override the defaults. Duplicate routes
    are dropped so a misconfigured env doesn't retry the same client twice.
    """
    configured = (
        (
            os.getenv("INTRO_REVIEW_PRIMARY_CLIENT"),
            os.getenv("INTRO_REVIEW_PRIMARY_MODEL"),
            INTRO_REVIEW_PRIMARY_CLIENT,
            INTRO_REVIEW_PRIMARY_MODEL,
        ),
        (
            os.getenv("INTRO_REVIEW_FALLBACK_CLIENT"),
            os.getenv("INTRO_REVIEW_FALLBACK_MODEL"),
            INTRO_REVIEW_FALLBACK_CLIENT,
            INTRO_REVIEW_FALLBACK_MODEL,
        ),
    )
    routes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_client, raw_model, default_client, default_model in configured:
        client = (raw_client or "").strip() or default_client
        model = (raw_model or "").strip() or default_model
        route_key = (client.casefold(), model.casefold())
        if route_key in seen:
            continue
        seen.add(route_key)
        routes.append((client, model))
    return routes


def _format_attachments(message: discord.Message) -> str:
    """Format a message's media attachments for the intro-review prompt.

    The reviewer is text-only — it can't play a video or render an image on its
    own — so instead of a bare boolean we hand it the concrete details (filename,
    type, dimensions, size, URL) and let it decide whether the attached media is
    the member's work.
    """
    if not message.attachments:
        return "none"
    parts = []
    for a in message.attachments[:5]:
        dims = f" {a.width}x{a.height}" if a.width and a.height else ""
        size_mb = (a.size or 0) / (1024 * 1024)
        parts.append(
            f"{a.filename} ({a.content_type or 'unknown'}{dims}, {size_mb:.1f}MB) <{a.url}>"
        )
    return "; ".join(parts)


# ── Prompt used by the LLM reviewer for new introductions ──

_INTRO_REVIEW_PROMPT = """\
You are a friendly greeter bot for Banodoco, an open-source AI art community on Discord. \
A new member has posted in the introductions channel. Your job is to welcome them if \
they've written a real intro, or clean up their message if it doesn't belong here.

Everyone is welcome here — but this channel is for real introductions, not drive-by \
one-liners. The bar isn't high, but it exists.

{bot_voice}

## Context

You will be given a member's recent messages in this channel (oldest first), followed by \
their current message. Lines marked [bot] are your own previous replies to this member — \
remember what you already said. Judge the CURRENT message in the context of these prior \
messages. The member is a newcomer who cannot post anywhere else yet. Media attached to the \
current message is the member's own work — treat an attached image or video as the \
"something they made" half of the intro, not as a request to share more.

## What makes a good intro

Becoming a Speaker is a two-part ask, and both parts matter:

1. A short intro — a few specific sentences about who they are or what they're \
into. "I'm into AI art" is not an intro. "I've been training LoRAs for stylised \
portraits using Kohya and just started experimenting with Wan" is.
2. Something they made — a link, image, video, workflow, model, or even a silly \
generation. It doesn't need to be polished; rough and unfinished work is welcome. \
"I made this yesterday and it's not very good" is fine — the point is showing real \
work, not showing off.

If they haven't made anything yet, that's fine too — encourage them to share what \
they have, or what they're working toward. Early and imperfect work is welcome here; \
we're looking for substance and a genuine interest in building, not a portfolio.

## What to do

Respond with exactly one of these actions on the first line, then your message (if any) \
after a blank line:

KEEP
(a short, warm, personal welcome)

Use KEEP for: a real introduction with substance — they say something specific about \
who they are or what they do, ideally with something they've made (a link, image, \
video, workflow, or even a rough early attempt). Write a brief personal reply (1-2 \
sentences) referencing something from their intro. If they haven't shared anything \
they've made, gently encourage them to — even if it's rough or they don't think it's \
good. That's the "something you made" half of becoming a Speaker. Do not promise \
feedback, reactions, or anything in return for sharing.

FEEDBACK
(reply to post in the channel)

Use FEEDBACK for: an attempt at an intro that shows effort but is too vague to act on — \
generic interest statements without specifics, a couple of buzzwords with no substance, \
or a lazy one-liner that doesn't say who they are. Write a warm 2-3 sentence reply. \
Welcome them, then ask for something concrete: what tools they use, what they're \
building, and something they've made — even a rough first attempt, or what they're \
working toward. Frame it as "we'd love to know more" not "you failed." Point them to \
{gate_channel} for how to write a proper intro.

DELETE
(short, friendly note shown briefly before their message is removed)

Use DELETE for messages that are completely off-topic — not an intro and not a support \
question: spam, ads, bare greetings ("hi", emoji), generic one-liners that say nothing \
specific, chit-chat, or replies that just thank or acknowledge someone. The note is \
shown briefly and then deleted; keep it short. Tell them they can see how to make a \
proper intro in {gate_channel}.

REDIRECT
(short note pointing them to {support_channel}, shown briefly before their message is removed)

Use REDIRECT for a support-oriented message — a question or request for help about the \
server, permissions, tools, or anything they need assistance with. Delete their message \
and point them to {support_channel} in the note. This channel is for introductions only. \
Keep the note short and helpful.

NO_REPLY
(do nothing — leave the message, post no reply)

Use NO_REPLY for: a message that's fine in the channel but needs no response from \
you — a thanks, an acknowledgement, or a small follow-up to your own previous reply \
where saying anything would be noise. If you've already replied to this member and \
they've just continued the exchange, prefer NO_REPLY over repeating yourself. Do not \
use NO_REPLY to dodge the other actions: off-topic content still gets DELETE, support \
questions still get REDIRECT, and a first real intro still gets a welcome (KEEP).

Remember: judge the current message. A bare greeting, off-topic discussion, or a support \
question is never KEEP. Don't repeat what you already said to this member — if you've \
already welcomed them or asked for specifics, build on that exchange instead of starting \
over."""

class GatingCog(commands.Cog):
    """
    Gated entry system: new members post intros, approvers react, bot grants Speaker role.

    Flow:
      1. on_member_join     → temp welcome ping in gate channel (auto-deleted after 5 min)
      2. on_message          → track intro, LLM reviews every message with member context (KEEP / FEEDBACK / DELETE / REDIRECT / NO_REPLY)
      3. on_raw_reaction_add → approver reacts on any tracked message → _approve_member
      4. _approve_member     → grant Speaker role, ✅ reaction, welcome post + DM

    Cleanup:
      - on_raw_message_delete  → remove from tracking, expire DB if no messages left
      - cleanup_expired_intros → expire pending intros older than 7 days
      - scan_intro_channels    → backfill _pending_messages from channel history on startup
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)

        # message_id → member_id for all intro messages from pending members
        self._pending_messages: dict[int, int] = {}

        # Single-replica only. brain-of-bndc must run as a single process.
        # Multiple replicas would break _pending_messages, poll loop ordering,
        # and Discord event delivery semantics. To scale: implement
        # AutoShardedBot and migrate _pending_messages to a shared cache.
        # That deployment constraint is why MP2 does not use a DB-side lease.
        self._poll_lock = asyncio.Lock()

        # Temp gate-channel welcome pings awaiting deletion: {message_id: (channel_id, sent_at)}
        self._temp_welcomes: dict[int, tuple[int, datetime]] = {}

        # Per-member recent intro-channel messages (member posts AND the bot's own
        # replies, the latter flagged is_bot), oldest→newest, used as review
        # context. {member_id: deque[(message_id, content, is_bot)]}
        self._intro_history: dict[int, deque] = {}

        # Caps concurrent LLM review calls so a burst of messages doesn't pile up.
        self._review_semaphore = asyncio.Semaphore(INTRO_REVIEW_CONCURRENCY)

    # ── Config helpers ──

    _ROLE_ENV_MAP = {
        'speaker_role_id': 'SPEAKER_ROLE_ID',
        'newbie_role_id': 'NEWBIE_ROLE_ID',
        'moderated_role_id': 'MODERATED_ROLE_ID',
    }
    _CHANNEL_ENV_MAP = {
        'gate_channel_id': 'GATE_CHANNEL_ID',
        'intro_channel_id': 'INTRO_CHANNEL_ID',
        'welcome_channel_id': 'WELCOME_CHANNEL_ID',
        'help_channel_id': 'HELP_CHANNEL_ID',
    }

    def _get_guild_config(self, guild_id: int) -> dict:
        """Resolve gating config for a guild from server_config, then env fallback."""
        sc = getattr(self.db, 'server_config', None) if self.db else None
        server = sc.get_server(guild_id) if sc else None
        cfg = {}
        for key in (
            'gate_channel_id', 'intro_channel_id', 'speaker_role_id',
            'approver_role_id', 'super_approver_role_id', 'welcome_channel_id',
            'newbie_role_id', 'moderated_role_id', 'help_channel_id',
        ):
            val = server.get(key) if server else None
            if val is None:
                env_key = self._ROLE_ENV_MAP.get(key) or self._CHANNEL_ENV_MAP.get(key)
                env_val = os.getenv(env_key) if env_key else None
                val = env_val
            cfg[key] = int(val) if val is not None else None
        # The support channel is always resolvable — newbies get pointed there for
        # help requests, so fall back to the known default when unconfigured.
        if cfg.get('help_channel_id') is None:
            cfg['help_channel_id'] = DEFAULT_HELP_CHANNEL_ID
        return cfg

    def _get_gating_config(self, guild_id: int) -> dict | None:
        """Return guild config if gating is fully configured, else None."""
        cfg = self._get_guild_config(guild_id)
        required = ('gate_channel_id', 'intro_channel_id', 'speaker_role_id',
                     'approver_role_id', 'super_approver_role_id')
        return cfg if all(cfg.get(k) for k in required) else None

    # ── Lifecycle ──

    async def cog_load(self):
        if not self.db:
            return
        try:
            rows = self.db.get_all_pending_intros()
            self._pending_messages = {row['message_id']: row['member_id'] for row in rows}
            logger.info(f"GatingCog: loaded {len(self._pending_messages)} pending intros from DB")
        except Exception as e:
            logger.error(f"GatingCog: failed to load pending intros: {e}", exc_info=True)
        await self.reconcile_orphan_intro_embeds()
        self.scan_intro_channels.start()
        self.cleanup_expired_intros.start()
        self.cleanup_temp_welcomes.start()
        self.poll_approval_requests.start()

    async def cog_unload(self):
        self.scan_intro_channels.cancel()
        self.cleanup_expired_intros.cancel()
        self.cleanup_temp_welcomes.cancel()
        self.poll_approval_requests.cancel()

    # ═══════════════════════════════════════════════════════════════
    #  1. New member joins → temp welcome in gate channel
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self.db:
            return
        cfg = self._get_gating_config(member.guild.id)
        if not cfg:
            return
        speaker_role = member.guild.get_role(cfg['speaker_role_id'])
        if speaker_role and speaker_role in member.roles:
            return
        channel = member.guild.get_channel(cfg['gate_channel_id'])
        if not channel:
            return
        try:
            # Reply to the bot's pinned welcome message if found
            reference = None
            async for hist_msg in channel.history(limit=50, oldest_first=True):
                if hist_msg.author.id == self.bot.user.id:
                    reference = hist_msg
                    break
            msg = await channel.send(
                f"Hi {member.mention}, welcome! If you'd like to speak everywhere, "
                f"see above \U0001f446.",
                reference=reference,
            )
            self._temp_welcomes[msg.id] = (channel.id, msg.created_at)
        except Exception as e:
            logger.error(f"GatingCog: failed to send gate welcome for {member.id}: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════════
    #  2. Member posts intro → track + LLM review (first msg only)
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.db or not message.guild:
            return
        guild_id = message.guild.id
        cfg = self._get_gating_config(guild_id)
        if not cfg:
            return
        if message.channel.id != cfg['intro_channel_id']:
            return

        # The bot's own replies to a tracked member are recorded as review
        # context so later reviews see both sides of the conversation — what the
        # bot already said, and what the member did after. Other bot posts
        # (approval embeds, expiring notes) aren't replies and fall through.
        if message.author.bot:
            self._record_bot_reply_context(message)
            return

        # Only track non-Speakers who aren't moderated
        speaker_role = message.guild.get_role(cfg['speaker_role_id'])
        if not speaker_role or speaker_role in message.author.roles:
            return
        moderated_role = message.guild.get_role(cfg['moderated_role_id']) if cfg.get('moderated_role_id') else None
        if moderated_role and moderated_role in message.author.roles:
            return

        # Track this message so any reaction on it can trigger approval
        existing = self.db.get_pending_intro_by_member(message.author.id, guild_id=guild_id)
        if existing:
            self.db.update_pending_intro_message(existing['id'], message.id, message.channel.id)
            logger.info(f"GatingCog: updated pending intro for {message.author} -> msg {message.id}")
        else:
            self.db.create_pending_intro(message.author.id, message.id, message.channel.id, guild_id=guild_id)
            logger.info(f"GatingCog: tracked intro from {message.author} (msg {message.id})")
        self._pending_messages[message.id] = message.author.id

        # Review EVERY message (with the member's prior channel context) so
        # off-topic chatter and support questions get cleaned up too, not just
        # the first intro.
        history = self._member_context(message.author.id)
        self._append_member_history(message.author.id, message)
        asyncio.create_task(self._review_intro(message, history))

    def _append_member_history(self, member_id: int, message: discord.Message) -> None:
        """Record a member's message for use as review context in later reviews."""
        buf = self._intro_history.setdefault(member_id, deque(maxlen=INTRO_REVIEW_CONTEXT_MESSAGES))
        buf.append((message.id, message.content or "(no text — media only)", False))

    def _member_context(self, member_id: int) -> list[tuple[int, str, bool]]:
        """Return the member's recent intro-channel messages (oldest→newest).

        Entries are (message_id, content, is_bot) — ``is_bot`` marks the bot's
        own replies so the reviewer can tell the two sides apart.
        """
        return list(self._intro_history.get(member_id, []))

    def _record_bot_reply_context(self, message: discord.Message) -> None:
        """Record the bot's own reply to a member for future review context.

        Only THIS bot's replies to a tracked member's intro-channel message
        count — those are the agent's previous words to that member. Other bots'
        messages (which would be misattributed) and non-reply bot posts
        (approval embeds, expiring notes — no resolvable member) are skipped.
        """
        bot_user_id = getattr(getattr(self.bot, 'user', None), 'id', None)
        if bot_user_id is None or message.author.id != bot_user_id:
            return
        ref = message.reference
        if not ref or not ref.message_id:
            return
        member_id = self._pending_messages.get(int(ref.message_id))
        if member_id is None:
            return
        buf = self._intro_history.setdefault(member_id, deque(maxlen=INTRO_REVIEW_CONTEXT_MESSAGES))
        buf.append((message.id, message.content or "(no text — media only)", True))

    async def _review_intro(self, message: discord.Message, history: list[tuple[int, str, bool]] | None = None):
        """Ask the configured LLM routes to review a message: KEEP / FEEDBACK / DELETE / REDIRECT / NO_REPLY."""
        try:
            has_url = bool(re.search(r'https?://\S+', message.content))
            attachments = _format_attachments(message)
            cfg = self._get_guild_config(message.guild.id)
            help_cid = (cfg or {}).get('help_channel_id') or DEFAULT_HELP_CHANNEL_ID
            support_mention = f"<#{help_cid}>"
            gate_cid = (cfg or {}).get('gate_channel_id')
            gate_mention = f"<#{gate_cid}>" if gate_cid else "the become-a-speaker channel"

            context_lines = []
            for mid, content, is_bot in (history or []):
                role = 'bot' if is_bot else 'member'
                context_lines.append(f"- {mid} [{role}]: {content[:300]}")
            context = "\n".join(context_lines) or "(none)"

            request = {
                "system_prompt": _INTRO_REVIEW_PROMPT.format(
                    bot_voice=BOT_VOICE,
                    support_channel=support_mention,
                    gate_channel=gate_mention,
                ),
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Member: {message.author.display_name}\n\n"
                        f"Prior messages from this member in this channel (oldest first):\n{context}\n\n"
                        f"Current message:\n{message.content or '(no text — media only)'}\n\n"
                        f"Has links: {has_url}\n"
                        f"Media attachments: {attachments}"
                    ),
                }],
                "max_tokens": 300,
            }
            last_error: Exception | None = None
            async with self._review_semaphore:
                for client_name, model in _intro_review_routes():
                    try:
                        route_request = dict(request)
                        if client_name.casefold() == "deepseek":
                            # Small classification task — DeepSeek reasoning can
                            # consume the whole 300-token budget and return no text.
                            route_request["thinking_enabled"] = False
                        response = await get_llm_response(
                            client_name=client_name,
                            model=model,
                            **route_request,
                        )
                        if not response.strip():
                            raise RuntimeError("empty intro review response")
                        break
                    except Exception as e:
                        last_error = e
                        logger.warning(
                            "GatingCog: intro review route %s/%s failed for %s: %s",
                            client_name, model, message.author, e,
                        )
                else:
                    raise RuntimeError("all intro review routes failed") from last_error

            response = response.strip()
            action = response.split('\n')[0].strip().upper()
            body = '\n'.join(response.split('\n')[1:]).strip()

            # Guard: member may have been approved while the reviewer was working
            if message.id not in self._pending_messages:
                logger.info(f"GatingCog: skipping intro review action for {message.author} (already resolved)")
                return

            if action == 'KEEP':
                if body:
                    await message.reply(body, mention_author=True, delete_after=60)
                logger.info(f"GatingCog: intro reviewer kept {message.author}: {body[:200] if body else '(no body)'}")

            elif action == 'FEEDBACK':
                if body:
                    await message.reply(body, mention_author=True, delete_after=60)
                logger.info(f"GatingCog: intro reviewer sent feedback to {message.author}: {body[:200] if body else '(no body)'}")

            elif action == 'DELETE':
                await self._delete_off_topic(message, body)
                logger.info(f"GatingCog: intro reviewer deleted off-topic msg from {message.author}: {body[:200] if body else '(no body)'}")

            elif action == 'REDIRECT':
                # Support question — delete it and leave a pointer to #support
                # visible long enough for them to actually see the link.
                await self._delete_off_topic(message, body)
                logger.info(f"GatingCog: intro reviewer redirected support query from {message.author}: {body[:200] if body else '(no body)'}")

            elif action == 'NO_REPLY':
                # Deliberate no-op: keep the message and the pending intro, post
                # nothing. The member-still-pending guard above already bailed if
                # they were approved while the review was in flight.
                logger.info(f"GatingCog: intro reviewer stayed silent for {message.author}: {body[:200] if body else '(no note)'}")

            else:
                logger.warning(f"GatingCog: unexpected intro review response for {message.author}: {response[:100]}")
        except Exception as e:
            logger.error(f"GatingCog: failed to review intro from {message.author}: {e}", exc_info=True)

    async def _delete_off_topic(self, message, body: str, hint_seconds: int = 60) -> None:
        """Delete a message and leave a short, expiring note for the author."""
        self._pending_messages.pop(message.id, None)
        member_id = message.author.id
        if not any(m == member_id for m in self._pending_messages.values()):
            intro = self.db.get_pending_intro_by_member(member_id, guild_id=message.guild.id)
            if intro:
                self.db.expire_pending_intro(intro['message_id'], guild_id=message.guild.id)
            # Nothing left for this member — drop their whole review context so a
            # future re-intro doesn't inherit this deleted conversation.
            self._remove_member_messages(member_id)
        else:
            self._drop_member_message_context(member_id, message.id)
        try:
            await message.delete()
            if body:
                hint = await message.channel.send(f"{message.author.mention} {body}")
                await asyncio.sleep(hint_seconds)
                await hint.delete()
        except Exception as e:
            logger.error(f"GatingCog: failed to delete intro message from {message.author}: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  3. Approver reacts → approve member
    # ═══════════════════════════════════════════════════════════════

    async def _recover_untracked_intro(self, payload: discord.RawReactionActionEvent) -> int | None:
        """Resolve a reaction on a message that wasn't in `_pending_messages`.

        If the author already has an open pending intro, the reaction is resolved to it
        so the member can be admitted from ANY of their messages in the intro channel
        (replies and follow-ups included), not just the originally-tracked intro. This
        matters because approvers frequently react to a member's later message or reply
        rather than to the exact tracked intro.

        Otherwise, a pending intro is created on the fly for the reacted message — replies
        included — so an approver reacting to ANY of the member's messages in the intro
        channel admits them. Returns the author's member_id, else None. Persists the result
        so future reactions on the same message are tracked normally.
        """
        cfg = self._get_gating_config(payload.guild_id)
        if not cfg:
            return None
        if payload.channel_id != cfg['intro_channel_id']:
            return None

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return None
        channel = guild.get_channel(payload.channel_id)
        if not channel:
            return None

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.info(f"GatingCog: couldn't fetch message {payload.message_id} for recovery: {e}")
            return None

        if message.author.bot:
            return None

        author_member = guild.get_member(message.author.id)
        if not author_member:
            try:
                author_member = await guild.fetch_member(message.author.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        speaker_role = guild.get_role(cfg['speaker_role_id'])
        if speaker_role and speaker_role in author_member.roles:
            return None
        moderated_role = guild.get_role(cfg['moderated_role_id']) if cfg.get('moderated_role_id') else None
        if moderated_role and moderated_role in author_member.roles:
            return None

        member_id = message.author.id

        existing = self.db.get_pending_intro_by_member(member_id, guild_id=payload.guild_id)
        if existing:
            # The member already has an open pending intro, but the approver reacted to a
            # different message of theirs in the intro channel (often a reply or a later
            # message). Resolve the reaction to their pending intro so they can be admitted
            # from ANY of their messages — not just the originally-tracked intro.
            self._pending_messages[payload.message_id] = member_id
            logger.info(
                f"GatingCog: resolved reaction on message {payload.message_id} "
                f"to existing pending intro of {member_id}"
            )
            return member_id

        # No existing pending intro. Any of the member's messages in the intro channel
        # (including replies) can seed one on the fly, so an approver reacting to any of
        # their messages admits them.
        try:
            self.db.create_pending_intro(
                member_id, payload.message_id, payload.channel_id, guild_id=payload.guild_id
            )
            self._pending_messages[payload.message_id] = member_id
            logger.info(
                f"GatingCog: recovered missed intro message {payload.message_id} from "
                f"{message.author} ({member_id}) — created pending row on the fly"
            )
            return member_id
        except Exception as e:
            logger.error(
                f"GatingCog: failed to create recovery pending intro for {member_id}: {e}",
                exc_info=True,
            )
            return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not self.db:
            return
        member_id = self._pending_messages.get(payload.message_id)
        if member_id is None:
            # Fallback: an approver may be reacting to an intro that was never tracked
            # (e.g. posted while the bot was offline, or whose DB write failed). Try to
            # recover from the message itself before silently bailing.
            member_id = await self._recover_untracked_intro(payload)
            if member_id is None:
                return

        logger.info(f"GatingCog: reaction {payload.emoji} on pending intro {payload.message_id} by user {payload.user_id}")

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        cfg = self._get_guild_config(payload.guild_id)
        if not cfg.get('approver_role_id'):
            return

        reactor = guild.get_member(payload.user_id)
        if not reactor or reactor.bot:
            return
        reactor_role_ids = {r.id for r in reactor.roles}
        is_approver = cfg['approver_role_id'] in reactor_role_ids
        is_super = cfg.get('super_approver_role_id') in reactor_role_ids if cfg.get('super_approver_role_id') else False
        if not is_approver and not is_super:
            logger.info(f"GatingCog: reactor {reactor} lacks approver role, ignoring")
            return

        intro = self.db.get_pending_intro_by_member(member_id, guild_id=payload.guild_id)
        if not intro:
            self._remove_member_messages(member_id)
            return

        voter_role = 'super_approver' if is_super else 'approver'
        self.db.record_intro_vote(intro['id'], payload.message_id, payload.user_id, voter_role, guild_id=payload.guild_id)
        await self._approve_member(guild, intro, cfg, reacted_message_id=payload.message_id)

    # ═══════════════════════════════════════════════════════════════
    #  4. Approve: grant role, ✅, DM
    # ═══════════════════════════════════════════════════════════════

    async def _approve_member(self, guild: discord.Guild, intro: dict, cfg: dict,
                              reacted_message_id: int | None = None):
        member = guild.get_member(intro['member_id'])
        if not member:
            return
        speaker_role = guild.get_role(cfg['speaker_role_id'])
        if not speaker_role:
            return
        newbie_role = guild.get_role(cfg['newbie_role_id']) if cfg.get('newbie_role_id') else None

        try:
            # Never promote a moderated member — check both the role and the DB status
            moderated_role = guild.get_role(cfg['moderated_role_id']) if cfg.get('moderated_role_id') else None
            if moderated_role and moderated_role in member.roles:
                logger.warning(f"GatingCog: refused to approve moderated member {member}")
                return
            if self.db:
                try:
                    if self.db.get_member_status(intro['member_id'], guild_id=guild.id) == 'moderated':
                        logger.warning(f"GatingCog: refused to approve DB-moderated member {member}")
                        return
                except Exception:
                    pass

            # Swap Newbie -> Speaker. Write the DB status FIRST so on_member_update
            # (fired by the role changes) sees 'speaker' and doesn't strip the new
            # role as a stray while the old status is still 'newbie'.
            if self.db:
                self.db.set_member_status(intro['member_id'], guild.id, 'speaker')
            if newbie_role and newbie_role in member.roles:
                await member.remove_roles(newbie_role, reason="Intro approved — promoted to Speaker")
            if speaker_role not in member.roles:
                await member.add_roles(speaker_role, reason="Intro approved by community")
            self.db.approve_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
            self._remove_member_messages(intro['member_id'])
            logger.info(f"GatingCog: approved {member} (msg {intro['message_id']})")

            if reacted_message_id:
                try:
                    channel = guild.get_channel(intro['channel_id'])
                    if channel:
                        msg = await channel.fetch_message(reacted_message_id)
                        await msg.add_reaction('\u2705')
                except Exception as e:
                    logger.error(f"GatingCog: failed to add checkmark to {reacted_message_id}: {e}")

            await self._send_speaker_welcome(guild, member, cfg)

            try:
                dm_body = (
                    f"Hey {member.display_name}! You've been approved to speak in **{guild.name}**. "
                    f"Welcome aboard \U0001f389"
                )
                if intro.get('approval_request_id'):
                    try:
                        member_row = self.db.get_member_for_approval(intro['member_id'])
                        slug = (member_row or {}).get('username')
                        if slug:
                            dm_body += f" Your art is also now live at https://banodoco.ai/@{slug}"
                        else:
                            dm_body += " Your art is also now live on banodoco.ai"
                    except Exception as e:
                        logger.error(
                            f"GatingCog: failed to load approval profile for DM copy: {e}",
                            exc_info=True,
                        )
                        dm_body += " Your art is also now live on banodoco.ai"
                await member.send(dm_body)
            except discord.Forbidden:
                logger.info(f"GatingCog: couldn't DM {member} (DMs disabled)")
            except Exception as e:
                logger.error(f"GatingCog: failed to DM {member}: {e}")
        except Exception as e:
            logger.error(f"GatingCog: failed to approve {member}: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════════
    #  Approved member welcome → 5-minute tagged post
    # ═══════════════════════════════════════════════════════════════

    async def _send_speaker_welcome(self, guild: discord.Guild, member: discord.Member,
                                    cfg: dict):
        """Tag an approved member beside the persistent Getting Started content."""
        welcome_channel_id = cfg.get('welcome_channel_id')
        if not welcome_channel_id:
            return

        channel = guild.get_channel(welcome_channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(welcome_channel_id)
            except Exception as e:
                logger.error(
                    f"GatingCog: failed to find welcome channel {welcome_channel_id} "
                    f"for approved member {member.id}: {e}"
                )
                return

        reference = None
        try:
            async for history_message in channel.history(limit=50, oldest_first=True):
                if history_message.author.id == self.bot.user.id:
                    reference = history_message
                    break
        except Exception as e:
            logger.warning(
                f"GatingCog: couldn't find persistent welcome message in "
                f"channel {welcome_channel_id}: {e}"
            )

        try:
            sent = await channel.send(
                f"{member.mention}, you're now a speaker! "
                "Check out the welcome message above.",
                reference=reference,
                delete_after=self.TEMP_WELCOME_TTL.total_seconds(),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=True,
                    roles=False,
                    replied_user=False,
                ),
            )
            self._temp_welcomes[sent.id] = (channel.id, sent.created_at)
            logger.info(
                f"GatingCog: sent temporary speaker welcome {sent.id} for {member} "
                f"in channel {welcome_channel_id}"
            )
        except Exception as e:
            logger.error(
                f"GatingCog: failed to send speaker welcome for {member.id} "
                f"in channel {welcome_channel_id}: {e}"
            )

    # ═══════════════════════════════════════════════════════════════
    #  Message deletion → cleanup tracking
    # ═══════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        member_id = self._pending_messages.pop(payload.message_id, None)
        if member_id is None:
            return
        if not any(m == member_id for m in self._pending_messages.values()):
            intro = self.db.get_pending_intro_by_member(member_id, guild_id=getattr(payload, 'guild_id', None))
            if intro:
                self.db.expire_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
                logger.info(f"GatingCog: expired pending intro for member {member_id} (all messages deleted)")
            self._remove_member_messages(member_id)
        else:
            logger.info(f"GatingCog: removed deleted message {payload.message_id} from tracking for member {member_id}")
            self._drop_member_message_context(member_id, int(payload.message_id))

    def _drop_member_message_context(self, member_id: int, message_id: int) -> None:
        """Remove one deleted message from a member's review context.

        Used when the member still has other tracked messages; the last-message-
        gone case is handled by _remove_member_messages clearing the whole buffer.
        """
        buf = self._intro_history.get(member_id)
        if not buf:
            return
        kept = deque((mid, content, is_bot) for mid, content, is_bot in buf if mid != message_id)
        if len(kept) != len(buf):
            self._intro_history[member_id] = kept

    def _remove_member_messages(self, member_id: int):
        """Remove all in-memory tracked messages and review context for a member."""
        to_remove = [mid for mid, m in self._pending_messages.items() if m == member_id]
        for mid in to_remove:
            del self._pending_messages[mid]
        self._intro_history.pop(member_id, None)

    # ═══════════════════════════════════════════════════════════════
    #  Background tasks
    # ═══════════════════════════════════════════════════════════════

    def _get_primary_intro_target(self) -> tuple[discord.Guild, discord.abc.Messageable, dict] | None:
        """Return the first configured guild and intro channel for web approvals."""
        for guild in self.bot.guilds:
            cfg = self._get_gating_config(guild.id)
            if not cfg:
                continue
            channel = guild.get_channel(cfg['intro_channel_id'])
            if channel:
                return guild, channel, cfg
        return None

    def _stamp_with_retry(self, ar_id: str, msg_id: int) -> bool:
        for attempt in range(STAMP_INLINE_RETRIES + 1):
            if self.db.mark_approval_request_posted(ar_id, msg_id):
                return True
            if attempt < STAMP_INLINE_RETRIES:
                logger.warning(
                    f"GatingCog: retrying posted_message_id stamp for approval request {ar_id}"
                )
        return False

    async def _delete_reconciled_duplicate(self, msg: discord.Message, reason: str):
        try:
            await msg.delete()
            logger.info(f"GatingCog: deleted approval embed {msg.id} during reconciliation ({reason})")
        except discord.NotFound:
            pass
        except Exception as e:
            logger.error(
                f"GatingCog: failed to delete approval embed {msg.id} during reconciliation: {e}",
                exc_info=True,
            )

    async def reconcile_orphan_intro_embeds(self):
        """Stitch marked application embeds to pending_intros once on cog_load."""
        if not self.db:
            return

        try:
            for row in self.db.list_unstamped_intros():
                approval_request_id = row.get('approval_request_id')
                message_id = row.get('message_id')
                if not approval_request_id or not message_id:
                    continue
                self.db.mark_approval_request_posted(approval_request_id, int(message_id))
        except Exception as e:
            logger.exception(f"GatingCog: failed DB-only approval intro reconciliation: {e}")

        try:
            target = self._get_primary_intro_target()
            if not target:
                return
            guild, intro_channel, _cfg = target
            cutoff = datetime.now(timezone.utc) - timedelta(hours=RECONCILE_HISTORY_HOURS)
            seen_markers: set[str] = set()
            bot_user_id = getattr(getattr(self.bot, 'user', None), 'id', None)
            if bot_user_id is None:
                logger.warning("GatingCog: skipping approval embed reconciliation; bot user unavailable")
                return

            async for msg in intro_channel.history(
                limit=RECONCILE_HISTORY_LIMIT,
                after=cutoff,
                oldest_first=False,
            ):
                if getattr(msg.author, 'id', None) != bot_user_id:
                    continue
                marker = extract_approval_request_marker(msg)
                if not marker:
                    continue
                if marker in seen_markers:
                    await self._delete_reconciled_duplicate(msg, "older duplicate marker")
                    continue
                seen_markers.add(marker)

                ar = self.db.get_approval_request(marker)
                if not ar or ar.get('status') != 'pending':
                    continue

                existing_pi = self.db.get_pending_intro_by_approval_request(marker)
                if existing_pi:
                    existing_message_id = existing_pi.get('message_id')
                    if existing_message_id and int(existing_message_id) == msg.id:
                        if ar.get('posted_message_id') is None:
                            self.db.mark_approval_request_posted(marker, msg.id)
                        self._pending_messages[msg.id] = int(existing_pi['member_id'])
                    else:
                        await self._delete_reconciled_duplicate(msg, "stale orphan marker")
                    continue

                intro = self.db.create_pending_intro(
                    member_id=int(ar['member_id']),
                    message_id=msg.id,
                    channel_id=intro_channel.id,
                    guild_id=guild.id,
                    approval_request_id=marker,
                )
                if intro:
                    self._pending_messages[msg.id] = int(ar['member_id'])
                    self.db.mark_approval_request_posted(marker, msg.id)
                    continue

                winner = self.db.get_pending_intro_by_approval_request(marker)
                if winner and winner.get('message_id'):
                    self._pending_messages[int(winner['message_id'])] = int(winner['member_id'])
        except Exception as e:
            logger.exception(f"GatingCog: failed Discord approval embed reconciliation: {e}")

    # Single-replica only. brain-of-bndc must run as a single process.
    # Multiple replicas would break _pending_messages, poll loop ordering, and
    # Discord event delivery semantics. To scale: implement AutoShardedBot and
    # migrate _pending_messages to a shared cache.
    # That deployment constraint is why MP2 does not use a DB-side lease.
    @tasks.loop(seconds=APPROVAL_POLL_INTERVAL_SECONDS)
    async def poll_approval_requests(self):
        """Post pending web approval requests into the introductions channel."""
        if not self.db:
            return
        async with self._poll_lock:
            target = self._get_primary_intro_target()
            if not target:
                return
            guild, intro_channel, _cfg = target
            rows = self.db.claim_pending_approval_requests(limit=APPROVAL_POLL_BATCH)

            for row in rows or []:
                try:
                    ar_id = row.get('id')
                    if not ar_id:
                        continue

                    # If a previous tick sent and inserted but failed to stamp,
                    # re-stamp from pending_intros and skip channel.send so no
                    # second visible embed appears in #introductions.
                    existing = self.db.get_pending_intro_by_approval_request(ar_id)
                    if existing and existing.get('message_id'):
                        self._stamp_with_retry(ar_id, int(existing['message_id']))
                        continue

                    member_row = self.db.get_member_for_approval(int(row['member_id']))
                    if not member_row:
                        logger.warning(
                            f"GatingCog: no members row for approval request {ar_id} "
                            f"member {row.get('member_id')}"
                        )
                        continue

                    art = row.get('media') or row.get('asset')
                    embed = build_application_embed(member_row, row, art)

                    try:
                        msg = await intro_channel.send(embed=embed)
                    except Exception as e:
                        logger.error(
                            f"GatingCog: failed to post approval request {ar_id}: {e}",
                            exc_info=True,
                        )
                        continue

                    try:
                        intro = self.db.create_pending_intro(
                            member_id=int(member_row['member_id']),
                            message_id=msg.id,
                            channel_id=intro_channel.id,
                            guild_id=guild.id,
                            approval_request_id=ar_id,
                        )
                    except Exception as e:
                        logger.error(
                            f"GatingCog: failed to create pending intro for approval request {ar_id}: {e}",
                            exc_info=True,
                        )
                        continue

                    if intro is None:
                        existing = self.db.get_pending_intro_by_approval_request(ar_id)
                        if existing and existing.get('message_id'):
                            self._stamp_with_retry(ar_id, int(existing['message_id']))
                        try:
                            await msg.delete()
                        except Exception as e:
                            logger.error(
                                f"GatingCog: failed to delete duplicate approval embed {msg.id}: {e}",
                                exc_info=True,
                            )
                        continue

                    self._pending_messages[msg.id] = int(member_row['member_id'])
                    if not self._stamp_with_retry(ar_id, msg.id):
                        logger.warning(
                            f"GatingCog: approval request {ar_id} posted as {msg.id} "
                            "but posted_message_id could not be stamped"
                        )
                    # Fresh post already reflects the latest bio/art, so clear any
                    # leftover embed_dirty flag (e.g. set when the previous message
                    # was deleted-then-edited and we re-posted on this tick).
                    self.db.mark_embed_updated(ar_id)
                except Exception as e:
                    logger.exception(
                        f"GatingCog: failed while processing approval request row {row.get('id')}: {e}"
                    )

            # ── Refresh embeds for already-posted approval requests whose
            # bio / attached media / attached asset was edited on the web.
            # Mirrors the post-loop above but edits the existing message in
            # place instead of sending a new one. Wrapped in its own try so a
            # failure here can never break the post-loop tick.
            try:
                dirty_rows = self.db.claim_dirty_intro_edits(limit=APPROVAL_POLL_BATCH)
                for row in dirty_rows:
                    try:
                        ar_id = row.get('id')
                        if not ar_id:
                            continue
                        posted_message_id = row.get('posted_message_id')
                        if not posted_message_id:
                            # Defensive: the SQL filter excludes nulls, but
                            # bail rather than fetch_message(None).
                            continue

                        member_row = self.db.get_member_for_approval(int(row['member_id']))
                        if not member_row:
                            logger.warning(
                                f"GatingCog: no members row for dirty approval request {ar_id} "
                                f"member {row.get('member_id')}"
                            )
                            continue

                        art = row.get('media') or row.get('asset')
                        embed = build_application_embed(member_row, row, art)

                        try:
                            msg = await intro_channel.fetch_message(int(posted_message_id))
                            await msg.edit(embed=embed)
                        except discord.NotFound:
                            # Original message deleted by a mod — let the
                            # post-loop recreate it next tick.
                            self.db.clear_posted_message_id(ar_id)
                            self.db.stamp_embed_retry_attempt(ar_id)
                            logger.info(
                                f"GatingCog: posted message for approval {ar_id} was deleted; "
                                "cleared posted_message_id for re-post"
                            )
                            continue
                        except discord.Forbidden as e:
                            self.db.stamp_embed_retry_attempt(ar_id)
                            logger.warning(
                                f"GatingCog: edit forbidden for approval {ar_id}: {e}"
                            )
                            continue
                        except discord.HTTPException as e:
                            # Includes rate-limit (429). Leave embed_dirty=true;
                            # we'll retry next tick.
                            self.db.stamp_embed_retry_attempt(ar_id)
                            logger.warning(
                                f"GatingCog: edit failed for approval {ar_id} (HTTP): {e}"
                            )
                            continue

                        self.db.mark_embed_updated(ar_id)
                        logger.info(
                            f"GatingCog: refreshed approval embed {posted_message_id} "
                            f"for approval_request {ar_id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"GatingCog: unexpected error refreshing embed for "
                            f"approval_request {row.get('id')}: {e}",
                            exc_info=True,
                        )
                        continue
            except Exception as e:
                logger.error(
                    f"GatingCog: dirty-edit refresh block failed: {e}",
                    exc_info=True,
                )

    @poll_approval_requests.before_loop
    async def before_poll_approval_requests(self):
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def scan_intro_channels(self):
        """Backfill _pending_messages from channel history on startup."""
        if not self.db or not self._pending_messages:
            return
        pending_member_ids = set(self._pending_messages.values())
        for guild in self.bot.guilds:
            cfg = self._get_guild_config(guild.id)
            intro_channel_id = cfg.get('intro_channel_id')
            speaker_role_id = cfg.get('speaker_role_id')
            if not intro_channel_id or not speaker_role_id:
                continue
            channel = guild.get_channel(intro_channel_id)
            if not channel:
                continue
            speaker_role = guild.get_role(speaker_role_id)
            found = 0
            try:
                async for msg in channel.history(limit=200):
                    if msg.author.bot or msg.author.id not in pending_member_ids:
                        continue
                    if speaker_role and speaker_role in getattr(msg.author, 'roles', ()):
                        continue
                    if msg.id not in self._pending_messages:
                        self._pending_messages[msg.id] = msg.author.id
                        found += 1
            except Exception as e:
                logger.error(f"GatingCog: failed to scan intro channel {intro_channel_id}: {e}")
            if found:
                logger.info(f"GatingCog: found {found} additional messages from pending members in {guild.name}")
        logger.info(f"GatingCog: intro channel scan complete, tracking {len(self._pending_messages)} total messages")

    @scan_intro_channels.before_loop
    async def before_scan_intro_channels(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def cleanup_expired_intros(self):
        """Expire and delete pending intros older than 3 days."""
        if not self.db:
            return
        expired = self.db.get_expired_pending_intros(expiry_days=3)
        if not expired:
            return
        logger.info(f"GatingCog: expiring {len(expired)} intro(s)")
        for intro in expired:
            # Delete the message from Discord
            await self._delete_intro_message(intro)
            self.db.expire_pending_intro(intro['message_id'], guild_id=intro.get('guild_id'))
            self._remove_member_messages(intro['member_id'])

    async def _delete_intro_message(self, intro: dict):
        """Try to delete an intro message from Discord."""
        try:
            guild = self.bot.get_guild(intro.get('guild_id')) if intro.get('guild_id') else None
            if not guild:
                return
            channel = guild.get_channel(intro['channel_id'])
            if not channel:
                return
            msg = await channel.fetch_message(intro['message_id'])
            await msg.delete()
            logger.info(f"GatingCog: deleted expired intro message {intro['message_id']} from {msg.author}")
        except discord.NotFound:
            pass
        except Exception as e:
            logger.error(f"GatingCog: failed to delete intro message {intro['message_id']}: {e}")

    @cleanup_expired_intros.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()
        # One-time: clean up old stale intros from before this feature existed
        await self._cleanup_old_stale_intros()

    async def _cleanup_old_stale_intros(self):
        """One-time scan: delete intro-channel messages from non-speakers older than 3 days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        for guild in self.bot.guilds:
            cfg = self._get_gating_config(guild.id)
            if not cfg:
                continue
            channel = guild.get_channel(cfg['intro_channel_id'])
            if not channel:
                continue
            speaker_role = guild.get_role(cfg['speaker_role_id'])
            if not speaker_role:
                continue
            deleted = 0
            try:
                async for msg in channel.history(limit=500, before=cutoff):
                    if msg.author.bot:
                        continue
                    # If they still don't have Speaker role, delete
                    member = guild.get_member(msg.author.id)
                    if member and speaker_role in member.roles:
                        continue
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.error(f"GatingCog: failed to delete stale intro {msg.id}: {e}")
            except Exception as e:
                logger.error(f"GatingCog: failed to scan intro channel for stale messages: {e}")
            if deleted:
                logger.info(f"GatingCog: cleaned up {deleted} stale intro(s) in {guild.name}")

    _TEMP_WELCOME_PATTERNS = (
        "welcome! If you'd like to speak",
        "you're now a speaker! Check out the welcome message above.",
    )
    TEMP_WELCOME_TTL = timedelta(minutes=5)

    @tasks.loop(minutes=1)
    async def cleanup_temp_welcomes(self):
        """Delete gate-channel welcome pings older than 5 minutes."""
        now = datetime.now(timezone.utc)
        expired_ids = [
            mid for mid, (_, sent_at) in self._temp_welcomes.items()
            if now - sent_at >= self.TEMP_WELCOME_TTL
        ]
        for mid in expired_ids:
            channel_id, _ = self._temp_welcomes.pop(mid)
            channel = self.bot.get_channel(channel_id)
            if not channel:
                continue
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"GatingCog: failed to delete temp welcome {mid}: {e}")

        # One-time scan on startup for orphaned welcome pings from previous runs
        if not self._startup_scan_done:
            self._startup_scan_done = True
            for guild in self.bot.guilds:
                cfg = self._get_guild_config(guild.id)
                channel_ids = {
                    cfg.get('gate_channel_id'),
                    cfg.get('welcome_channel_id'),
                }
                deleted = 0
                for cid in channel_ids:
                    if not cid:
                        continue
                    channel = self.bot.get_channel(cid)
                    if not channel:
                        continue
                    try:
                        async for msg in channel.history(limit=50):
                            is_temp_welcome = any(
                                pattern in (msg.content or '')
                                for pattern in self._TEMP_WELCOME_PATTERNS
                            )
                            if (msg.author.id == self.bot.user.id
                                    and is_temp_welcome
                                    and now - msg.created_at >= self.TEMP_WELCOME_TTL):
                                try:
                                    await msg.delete()
                                    deleted += 1
                                except discord.NotFound:
                                    pass
                    except Exception as e:
                        logger.error(
                            f"GatingCog: failed to scan channel {cid} "
                            f"for orphaned welcomes: {e}"
                        )
                if deleted:
                    logger.info(f"GatingCog: cleaned up {deleted} orphaned welcome(s) in {guild.name}")

    @cleanup_temp_welcomes.before_loop
    async def before_cleanup_temp_welcomes(self):
        await self.bot.wait_until_ready()
        self._startup_scan_done = False
