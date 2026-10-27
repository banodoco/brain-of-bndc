"""Cog for compute micro-grants: forum post → LLM review → SOL payment."""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

import discord
from discord.ext import commands, tasks

from src.common.db_handler import WalletUpdateBlockedError
from src.features.grants.assessor import (
    assess_application,
    interpret_admin_decision,
    is_application_content_sufficient,
)
from src.features.grants.delivery_assessor import assess_delivery
from src.features.grants.delivery_evidence import collect_delivery_evidence
from src.features.grants.delivery_store import GrantDeliveryStore
from src.features.grants.pricing import GPU_RATES, calculate_grant_cost, refresh_grant_prices
from src.features.grants.solana_client import is_valid_solana_address
from src.features.payments.payment_service import PaymentActor, PaymentActorKind

logger = logging.getLogger('DiscordBot')

# Bot-authored public verdicts. Startup recovery must never reassess a row when
# Discord already shows one of these outcomes, even if the DB write was lost.
_POSTED_VERDICT_MARKERS = (
    '**This is not an approval yet**',
    '**Application not approved**',
    '**Grant Approved!**',
)
_ADMIN_REVIEW_FAILURE_MESSAGE = (
    "I couldn't process that moderator decision right now. "
    "The application remains queued for manual review; please try again shortly."
)

# Billing circuit-breaker: once we hit a 402, stop hammering the provider
# and stop spamming the grants channel. A single admin DM is sent; all
# subsequent review attempts until _BILLING_COOLDOWN_S has elapsed just
# post the friendly _BILLING_PAUSED_MESSAGE without touching the LLM.
_BILLING_PAUSED_MESSAGE = (
    "Grants reviews are temporarily paused — LLM credits are exhausted "
    "(402 Insufficient Balance). Your application is saved and will be "
    "reviewed automatically once credits are refilled. No need to repost."
)
_BILLING_COOLDOWN_S = 3600  # 1 hour after a 402 before we retry the provider
_DELIVERY_ACK_MARKER = '**Grant progress update recorded**'
_DELIVERY_ACK_MARKERS = (
    _DELIVERY_ACK_MARKER,  # legacy marker; retain for crash recovery dedupe
    '**Grant delivery marked complete**',
    '**Grant update needs more information**',
    '**Grant update needs review**',
)
_DELIVERY_ACKS = {
    'progress': 'Thanks for sharing this progress update. It has been recorded for future grant reviews.',
    'completed': 'Thanks for sharing this update and the linked work. The evidence has been recorded for future grant reviews.',
    'needs_info': 'Thanks for the update. Could you add a little more detail or link the work for future grant reviews?',
    'needs_review': 'Thanks for the update. It has been recorded for future grant reviews.',
}
_DELIVERY_ACK_HEADINGS = {
    'progress': '**Grant progress update recorded**',
    'completed': '**Grant delivery marked complete**',
    'needs_info': '**Grant update needs more information**',
    'needs_review': '**Grant update needs review**',
}


def _delivery_ack_content(assessment: dict) -> str | None:
    """Build a bounded acknowledgement from an already-validated assessment."""
    assessment = assessment if isinstance(assessment, dict) else {}
    classification = assessment.get('classification')
    if classification not in _DELIVERY_ACK_HEADINGS:
        classification = 'needs_review'
    heading = _DELIVERY_ACK_HEADINGS.get(classification)
    if not heading:
        return None
    response = assessment.get('response')
    if not isinstance(response, str) or not response.strip() or len(response) > 600:
        if classification == 'completed':
            response = 'Your completion report has been recorded for future grant reviews.'
            if assessment.get('verification') == 'recipient_reported':
                response = 'Delivery is marked complete based on your report and the launch you shared.'
            elif assessment.get('verification') == 'evidence_supported':
                response = 'The inspected artifact supports completion of the funded work.'
        else:
            response = _DELIVERY_ACKS.get(classification)
    if not response:
        return None
    response = response.strip()
    if (classification == 'completed'
            and assessment.get('verification') == 'recipient_reported'
            and not any(term in response.casefold() for term in ('report', 'self-report', 'your update'))):
        response = f'Based on your report (not independently verified): {response}'
    return f'{heading}\n{response[:600]}'


def _is_delivery_ack(message: Any, bot_id: int, source_message_id: int) -> bool:
    reference = getattr(message, 'reference', None)
    return (
        getattr(getattr(message, 'author', None), 'id', None) == bot_id
        and getattr(reference, 'message_id', None) == source_message_id
        and any(marker in (getattr(message, 'content', '') or '') for marker in _DELIVERY_ACK_MARKERS)
    )
_billing_paused_until: float = 0  # monotonic-ish via time.time()

def _is_billing_error(exc: Exception) -> bool:
    msg = str(exc)
    return "402" in msg or "Insufficient Balance" in msg or "insufficient" in msg.lower() or "billing exhausted" in msg.lower()

def _billing_paused() -> bool:
    import time
    return time.time() < _billing_paused_until

def _note_billing_paused():
    global _billing_paused_until
    import time
    _billing_paused_until = time.time() + _BILLING_COOLDOWN_S


async def _find_posted_verdict(thread: discord.Thread) -> str | None:
    """Return a stable marker for a recent bot-authored grant verdict, if any."""
    async for message in thread.history(limit=25):
        if not getattr(message.author, 'bot', False):
            continue
        content = message.content or ''
        for marker in _POSTED_VERDICT_MARKERS:
            if marker in content:
                return marker
    return None


class GrantsCog(commands.Cog):
    """Micro-grants: applicants post in forum → LLM review → SOL payment."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)
        self.payment_service = getattr(bot, 'payment_service', None)
        self.storage = getattr(self.db, 'storage_handler', None) if self.db else None
        self.delivery_store = GrantDeliveryStore(self.db) if self.db else None

        # Grants channel: resolve from server_config
        sc = getattr(self.db, 'server_config', None) if self.db else None
        _server = sc.get_first_server_with_field('grants_channel_id', require_write=True) if sc else None
        _guild_id = int(_server['guild_id']) if _server else None
        grants_ch = _server.get('grants_channel_id') if _server else None
        self.grants_channel_id = int(grants_ch) if grants_ch else None

        self.guild_id = _guild_id

        admin_id = os.getenv('ADMIN_USER_ID')
        self.admin_id = int(admin_id) if admin_id else None

        self.configured = all([self.grants_channel_id, self.db])
        if not self.configured:
            logger.warning("GrantsCog: missing config, handlers will no-op")

        # Thread names the bot should ignore (guide + questions)
        self._ignored_thread_names = {"How Micro-Grants Work", "Questions & Discussion"}

        # Forum tags — populated on_ready once we can read the forum channel
        self._tags: dict[str, discord.ForumTag] = {}

        # In-memory guard against concurrent processing of the same thread
        self._processing_threads: set[int] = set()
        self._delivery_recovery_thread_cursor = 0
        self._delivery_recovery_message_cursors: dict[int, datetime] = {}

        # Price refresh agent: keeps GPU market rates (and the SOL price cache)
        # current so grants are priced at today's market, not static defaults.
        # First run fires immediately at startup; interval from env (hours).
        try:
            refresh_hours = max(float(os.getenv('GRANTS_PRICE_REFRESH_HOURS', '24')), 1.0)
        except ValueError:
            refresh_hours = 24.0
        self._price_refresh_loop.change_interval(hours=refresh_hours)
        self._price_refresh_hours = refresh_hours

    @property
    def _admin_mention(self) -> str:
        return f"<@{self.admin_id}>" if self.admin_id else "@admin"

    # ========== Startup Scan ==========

    @tasks.loop(hours=24)
    async def _price_refresh_loop(self):
        """Periodically refresh GPU market rates + SOL price for grants."""
        await refresh_grant_prices()

    @_price_refresh_loop.before_loop
    async def _before_price_refresh(self):
        await self.bot.wait_until_ready()

    @_price_refresh_loop.error
    async def _price_refresh_error(self, exc):
        logger.error(f"GrantsCog: price refresh loop error: {exc}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Load forum tags, scan for unprocessed threads, start price refresh."""
        if not self.configured:
            return
        try:
            await self._load_forum_tags()
            await self._scan_missed_threads()
        except Exception as e:
            logger.error(f"GrantsCog: startup failed: {e}", exc_info=True)
        finally:
            if not self._delivery_recovery_loop.is_running():
                self._delivery_recovery_loop.start()
            if not self._price_refresh_loop.is_running():
                self._price_refresh_loop.start()
                logger.info(
                    f"GrantsCog: price refresh loop started (every {self._price_refresh_hours}h)"
                )

    def cog_unload(self):
        if self._delivery_recovery_loop.is_running():
            self._delivery_recovery_loop.cancel()
        if self._price_refresh_loop.is_running():
            self._price_refresh_loop.cancel()

    @tasks.loop(minutes=15)
    async def _delivery_recovery_loop(self):
        """Recover recent paid-thread updates that arrived while the bot was offline."""
        try:
            await self._recover_paid_delivery_updates()
        except Exception:
            logger.error('GrantsCog: delivery recovery sweep failed', exc_info=True)

    @_delivery_recovery_loop.before_loop
    async def _before_delivery_recovery(self):
        await self.bot.wait_until_ready()

    async def _recover_paid_delivery_updates(self):
        if not self.configured or not self.delivery_store:
            return
        now = discord.utils.utcnow()
        cutoff = now - timedelta(days=7)
        for guild in self.bot.guilds:
            # Retry durable work regardless of the recent-message horizon, so a
            # long provider outage cannot strand updates permanently.
            for row in self.delivery_store.pending(guild.id, limit=100):
                try:
                    grant = self.db.get_grant_by_thread(row['thread_id'], guild_id=guild.id)
                    if not grant or grant.get('status') != 'paid':
                        continue
                    thread = self.bot.get_channel(int(row['thread_id']))
                    if thread is None:
                        thread = await self.bot.fetch_channel(int(row['thread_id']))
                    if not isinstance(thread, discord.Thread):
                        continue
                    source = await thread.fetch_message(int(row['message_id']))
                    if (int(source.id) == int(thread.id)
                            or int(source.author.id) != int(grant.get('applicant_id', -1))):
                        continue
                    await self._process_delivery_update(source, grant)
                except Exception:
                    logger.error('GrantsCog: durable delivery retry failed for message %s', row.get('message_id'), exc_info=True)
            forum = guild.get_channel(self.grants_channel_id)
            if not isinstance(forum, discord.ForumChannel):
                continue
            threads = list(forum.threads)
            async for thread in forum.archived_threads(limit=100):
                threads.append(thread)
            threads = sorted({int(t.id): t for t in threads}.values(), key=lambda t: int(t.id))
            if threads:
                start = getattr(self, '_delivery_recovery_thread_cursor', 0) % len(threads)
                threads = threads[start:] + threads[:start]
            else:
                start = 0
            remaining_messages = 100
            visited_threads = 0
            for thread in threads[:100]:
                if remaining_messages <= 0:
                    break
                visited_threads += 1
                try:
                    grant = self.db.get_grant_by_thread(thread.id, guild_id=guild.id)
                    if not grant or grant.get('status') != 'paid':
                        continue
                    paid_at = grant.get('paid_at')
                    if not paid_at:
                        continue
                    if isinstance(paid_at, str):
                        paid_at = datetime.fromisoformat(paid_at.replace('Z', '+00:00'))
                    if paid_at.tzinfo is None:
                        paid_at = paid_at.replace(tzinfo=timezone.utc)
                    cursors = getattr(self, '_delivery_recovery_message_cursors', None)
                    if cursors is None:
                        cursors = self._delivery_recovery_message_cursors = {}
                    cursor = cursors.get(int(thread.id))
                    after = max(cutoff, paid_at, cursor) if cursor else max(cutoff, paid_at)
                    recent = [msg async for msg in thread.history(
                        limit=min(100, remaining_messages), after=after, oldest_first=True
                    )]
                    for msg in recent:
                        remaining_messages -= 1
                        if int(msg.id) == int(thread.id):
                            cursors[int(thread.id)] = msg.created_at
                            continue
                        if msg.author.bot or int(msg.author.id) != int(grant.get('applicant_id', -1)):
                            cursors[int(thread.id)] = msg.created_at
                            continue
                        await self._process_delivery_update(msg, grant)
                        cursors[int(thread.id)] = msg.created_at
                except Exception:
                    logger.error('GrantsCog: delivery recovery failed for thread %s', thread.id, exc_info=True)
                    # Preserve the last successfully examined timestamp so this
                    # message can be retried without blocking other threads.
                    continue
            if threads:
                self._delivery_recovery_thread_cursor = (start + max(1, visited_threads)) % len(threads)

    async def _load_forum_tags(self):
        """Cache forum tags by name for applying to threads."""
        for guild in self.bot.guilds:
            forum = guild.get_channel(self.grants_channel_id)
            if forum and isinstance(forum, discord.ForumChannel):
                for tag in forum.available_tags:
                    self._tags[tag.name.lower()] = tag
                logger.info(f"GrantsCog: loaded {len(self._tags)} forum tags: {list(self._tags.keys())}")
                return

    async def _scan_missed_threads(self):
        """Find forum threads with no DB record and process them."""
        guild = None
        for g in self.bot.guilds:
            channel = g.get_channel(self.grants_channel_id)
            if channel:
                guild = g
                break
        if not guild:
            return

        forum = guild.get_channel(self.grants_channel_id)
        if not forum or not isinstance(forum, discord.ForumChannel):
            return

        # Get all active (non-archived) threads
        threads = forum.threads
        # Also fetch archived threads that might have been missed
        archived = []
        async for thread in forum.archived_threads(limit=50):
            archived.append(thread)

        all_threads = list(threads) + archived
        processed = 0

        for thread in all_threads:
            # Skip guide/questions threads
            if thread.name in self._ignored_thread_names:
                continue

            # If already in DB, backfill missing attachments/avatar then skip
            existing = self.db.get_grant_by_thread(thread.id, guild_id=guild.id)
            if existing:
                await self._backfill_media(thread, existing)
                # A provider outage may have stranded the row after creation but
                # before a verdict was persisted. Re-run it on startup now that
                # the LLM route is available again.
                if existing.get('status') == 'reviewing' and not thread.locked:
                    if thread.id in self._processing_threads:
                        continue
                    self._processing_threads.add(thread.id)
                    try:
                        posted_verdict = await _find_posted_verdict(thread)
                        if posted_verdict:
                            logger.warning(
                                f"GrantsCog: reviewing row {thread.id} already has posted "
                                f"verdict {posted_verdict!r}; skipping startup reassessment"
                            )
                        else:
                            logger.info(
                                f"GrantsCog: recovering reviewing thread {thread.id} "
                                f"({thread.name})"
                            )
                            await self._reassess_application(thread, existing)
                    except Exception:
                        logger.error(
                            f"GrantsCog: failed to recover reviewing thread {thread.id}",
                            exc_info=True,
                        )
                    finally:
                        self._processing_threads.discard(thread.id)
                continue

            # Skip locked threads (already handled manually)
            if thread.locked:
                continue

            logger.info(f"GrantsCog: found missed thread {thread.id} ({thread.name}), processing...")
            if thread.id in self._processing_threads:
                continue
            self._processing_threads.add(thread.id)
            try:
                await self._process_new_application(thread)
                processed += 1
            except Exception as e:
                logger.error(f"GrantsCog: error processing missed thread {thread.id}: {e}", exc_info=True)
            finally:
                self._processing_threads.discard(thread.id)

        if processed:
            logger.info(f"GrantsCog: processed {processed} missed thread(s) on startup")

    # ========== Admin Review ==========

    async def _handle_admin_review(self, thread: discord.Thread, grant: dict, admin_message: str):
        """Route admin's natural-language reply through LLM to interpret their decision."""
        thread_id = thread.id

        # Gather full thread conversation for context
        messages = []
        async for msg in thread.history(limit=100, oldest_first=True):
            prefix = "[Reviewer]" if msg.author.bot else "[Applicant]" if msg.author.id == grant['applicant_id'] else "[Admin]"
            messages.append(f"{prefix}: {msg.content}")
        thread_content = f"**{thread.name}**\n\n" + "\n\n".join(messages)

        # Get original LLM recommendation if available
        llm_recommendation = None
        if grant.get('llm_assessment'):
            try:
                llm_recommendation = json.loads(grant['llm_assessment'])
            except (json.JSONDecodeError, TypeError):
                pass

        try:
            assessment = await interpret_admin_decision(
                thread_content, admin_message,
                llm_recommendation=llm_recommendation,
                guild_id=getattr(thread.guild, 'id', None),
                server_config=getattr(self.db, 'server_config', None),
            )
        except Exception as e:
            if _is_billing_error(e):
                logger.error(f"GrantsCog: billing exhausted during admin review for thread {thread_id}: {e}")
                _note_billing_paused()
                # Update status but stay in needs_review — do not spin
                self.db.update_grant_status(
                    thread_id,
                    guild_id=getattr(thread.guild, 'id', None),
                    status='needs_review',
                )
                await thread.send(_BILLING_PAUSED_MESSAGE + f" {self._admin_mention} will refill shortly.")
                # One DM to admin, throttled by the same cooldown
                try:
                    # _admin_mention is "<@id>" — try DM if we can resolve the user
                    if getattr(self, 'admin_id', None):
                        user = self.bot.get_user(self.admin_id) or await self.bot.fetch_user(self.admin_id)
                        if user:
                            await user.send(f"Grants LLM billing exhausted (402) — paused reviews for {_BILLING_COOLDOWN_S//60} min. Please refill OpenRouter credits at https://openrouter.ai/credits. Thread {thread_id} triggered it.")
                except Exception:
                    pass
                return
            logger.error(
                f"GrantsCog: admin review provider failed for thread {thread_id}",
                exc_info=True,
            )
            self.db.update_grant_status(
                thread_id,
                guild_id=getattr(thread.guild, 'id', None),
                status='needs_review',
            )
            await thread.send(_ADMIN_REVIEW_FAILURE_MESSAGE)
            return

        # interpret_admin_decision returns needs_review if admin intent was unclear —
        # in that case just post the response as a message and keep the thread open
        if assessment['decision'] == 'needs_review':
            await thread.send(assessment['response'])
            return

        await self._handle_assessment(thread, assessment)

    # ========== Listeners ==========

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        """Handle new forum posts in the grants channel."""
        if not self.configured:
            return
        if not thread.parent_id or thread.parent_id != self.grants_channel_id:
            return
        if thread.name in self._ignored_thread_names:
            return

        thread_id = thread.id

        # Race condition guard
        if thread_id in self._processing_threads:
            return
        self._processing_threads.add(thread_id)

        try:
            await self._process_new_application(thread)
        except Exception as e:
            logger.error(f"GrantsCog: error processing thread {thread_id}: {e}", exc_info=True)
            try:
                await thread.send(f"An error occurred while reviewing this application. {self._admin_mention} will follow up.")
            except Exception:
                pass
        finally:
            self._processing_threads.discard(thread_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle replies in grant threads (follow-up info or wallet address)."""
        if not self.configured:
            return
        if message.author.bot or not message.guild:
            return

        # Must be in a thread under the grants channel
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return
        if not channel.parent_id or channel.parent_id != self.grants_channel_id:
            return

        thread_id = channel.id
        grant = self.db.get_grant_by_thread(thread_id, guild_id=message.guild.id)
        if not grant:
            return

        # Paid recipients may report delivery after funding. Route the original
        # applicant first, including when that person is also the configured
        # admin. No other paid-thread author enters moderation or wallet flows.
        if grant.get('status') == 'paid':
            if int(message.author.id) == int(grant.get('applicant_id', -1)):
                await self._process_delivery_update(message, grant)
            return

        # Moderator decisions are only accepted while a grant is awaiting
        # human review. In every other state, an admin who owns the application
        # follows the same applicant path as any other owner.
        if self.admin_id and message.author.id == self.admin_id and grant['status'] == 'needs_review':
            if thread_id in self._processing_threads:
                return
            self._processing_threads.add(thread_id)
            try:
                await self._handle_admin_review(channel, grant, message.content)
            except Exception:
                logger.error(
                    f"GrantsCog: admin review error for thread {thread_id}",
                    exc_info=True,
                )
                self.db.update_grant_status(
                    thread_id,
                    guild_id=message.guild.id,
                    status='needs_review',
                )
                await channel.send(_ADMIN_REVIEW_FAILURE_MESSAGE)
            finally:
                self._processing_threads.discard(thread_id)
            return

        # Only respond to the original applicant
        if message.author.id != grant['applicant_id']:
            return

        if grant['status'] == 'needs_info':
            # Re-assess with full conversation
            if thread_id in self._processing_threads:
                return
            self._processing_threads.add(thread_id)
            try:
                await self._reassess_application(channel, grant)
            except Exception as e:
                logger.error(f"GrantsCog: re-assessment error for thread {thread_id}: {e}", exc_info=True)
                await channel.send(f"An error occurred while re-reviewing. {self._admin_mention} will follow up.")
            finally:
                self._processing_threads.discard(thread_id)

        elif grant['status'] == 'awaiting_wallet':
            # Extract wallet address from message
            wallet = message.content.strip()
            if not is_valid_solana_address(wallet):
                await message.reply("That doesn't look like a valid Solana wallet address. Please send a valid base58-encoded address.")
                return

            # Race condition guard
            if thread_id in self._processing_threads:
                return
            self._processing_threads.add(thread_id)

            try:
                await self._start_payment_flow(channel, grant, wallet)
            except Exception as e:
                logger.error(f"GrantsCog: payment error for thread {thread_id}: {e}", exc_info=True)
                await channel.send(
                    f"I couldn't start the payment flow for that wallet. {self._admin_mention} will follow up.\n\n"
                    f"Error: {e}"
                )
            finally:
                self._processing_threads.discard(thread_id)

        elif grant['status'] != 'awaiting_wallet':
            # Any other stage (reviewing/needs_review/rejected/paid/payment_
            # requested): don't leave the applicant in silence. A valid-looking
            # wallet address means they've run ahead of the flow — e.g. they
            # misread a manual-review notice as an approval. State the actual
            # stage instead of dropping the message.
            if is_valid_solana_address(message.content.strip()):
                await message.reply(
                    "I'm not collecting a wallet address at this stage of your application "
                    f"(current stage: `{grant['status']}`). No payment can be sent yet — "
                    "please wait for instructions in this thread."
                )

    async def _process_delivery_update(self, message: discord.Message, grant: dict) -> None:
        """Persist, assess and acknowledge one applicant message in a paid grant."""
        if not self.delivery_store:
            raise RuntimeError('Grant delivery storage is not configured')
        if grant.get('status') != 'paid' or not grant.get('paid_at'):
            return
        paid_at = grant['paid_at']
        if isinstance(paid_at, str):
            paid_at = datetime.fromisoformat(paid_at.replace('Z', '+00:00'))
        created_at = getattr(message, 'created_at', None)
        if created_at is None:
            return
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at <= paid_at:
            return
        if int(message.id) == int(message.channel.id):
            return
        guild_id = int(message.guild.id)
        message_id = int(message.id)
        worker_id = f'{getattr(self.bot.user, "id", "bot")}:{uuid4()}'
        update = self.delivery_store.create(
            guild_id=guild_id,
            message_id=message_id,
            thread_id=int(message.channel.id),
            applicant_id=int(message.author.id),
            content=message.content or '',
            source_url=getattr(message, 'jump_url', None),
            evidence=[],
        )
        claim = self.delivery_store.claim(guild_id, message_id, worker_id)
        if claim is None:
            return

        try:
            # If assessment was saved before a crash, reuse it and only recover
            # the Discord acknowledgement. Never charge for / repeat the review.
            assessment = claim.get('assessment')
            evidence = claim.get('evidence') or []
            if assessment is None:
                evidence = await asyncio.wait_for(
                    collect_delivery_evidence(self.bot, message), timeout=90
                )
                prior = self.delivery_store.history(guild_id, int(message.channel.id), limit=100)
                prior = [row for row in prior if row.get('message_id') != message_id]
                assessment = await asyncio.wait_for(
                    assess_delivery(grant, update, evidence, prior_updates=prior[-10:]), timeout=120
                )
                self.delivery_store.update(
                    guild_id, message_id,
                    {'evidence': evidence, 'assessment': assessment, 'state': 'assessed'},
                    worker_id=worker_id,
                )

            if assessment.get('classification') == 'unrelated':
                self.delivery_store.update(
                    guild_id, message_id, {'state': 'ignored'}, worker_id=worker_id,
                )
                return
            acknowledgement = _delivery_ack_content(assessment)
            if not acknowledgement:
                logger.warning(
                    'Grant delivery assessment returned malformed acknowledgement for %r',
                    assessment.get('classification'),
                )
                return

            # Discord may have accepted the reply immediately before a process
            # crash. Match both the bot-authored marker and source reference.
            existing_ack = None
            stored_ack_id = claim.get('ack_message_id')
            fetch_message = getattr(message.channel, 'fetch_message', None)
            if stored_ack_id and callable(fetch_message):
                try:
                    candidate = await fetch_message(int(stored_ack_id))
                    if _is_delivery_ack(candidate, int(self.bot.user.id), message_id):
                        existing_ack = candidate
                except Exception:
                    # Fall back to the bounded history scan if the stored ID
                    # is stale or Discord cannot resolve it.
                    logger.debug('Stored grant delivery acknowledgement %s could not be fetched', stored_ack_id)
            inspected = 0
            if existing_ack is None:
                async for candidate in message.channel.history(limit=1000, after=message, oldest_first=True):
                    inspected += 1
                    if _is_delivery_ack(candidate, int(self.bot.user.id), message_id):
                        existing_ack = candidate
                        break
            if existing_ack is None and inspected >= 1000:
                # The source is too old to prove whether an earlier ack exists.
                # Fail closed instead of risking a duplicate public reply.
                raise RuntimeError('Could not conclusively check for an existing delivery acknowledgement')
            if existing_ack is not None:
                # An explicit re-assessment reuses the source message and the
                # existing public reply. Refresh its content rather than
                # leaving stale assessment text or creating a duplicate reply.
                expiry = (discord.utils.utcnow() + timedelta(seconds=240)).isoformat()
                self.delivery_store.update(
                    guild_id, message_id, {'lease_until': expiry}, worker_id=worker_id,
                )
                if (existing_ack.content or '') != acknowledgement:
                    await existing_ack.edit(
                        content=acknowledgement,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                self.delivery_store.update(
                    guild_id, message_id,
                    {'state': 'acknowledged', 'ack_message_id': int(existing_ack.id)},
                    worker_id=worker_id,
                )
                return

            # Renew/verify ownership immediately before the external side effect.
            expiry = (discord.utils.utcnow() + timedelta(seconds=240)).isoformat()
            self.delivery_store.update(
                guild_id, message_id, {'lease_until': expiry}, worker_id=worker_id,
            )
            ack = await message.reply(
                acknowledgement,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.delivery_store.update(
                guild_id, message_id,
                {'state': 'acknowledged', 'ack_message_id': int(ack.id)},
                worker_id=worker_id,
            )
        except Exception:
            # Keep saved assessment/evidence. Expire the lease for prompt retry;
            # persistence errors propagate so the gateway reports failure.
            try:
                self.delivery_store.update(
                    guild_id, message_id,
                    {'lease_until': (discord.utils.utcnow() - timedelta(seconds=1)).isoformat()},
                    worker_id=worker_id,
                )
            except Exception:
                pass
            raise

    # ========== Core Logic ==========
    async def _process_new_application(self, thread: discord.Thread):
        """Assess a new grant application and respond."""
        thread_id = thread.id
        applicant_id = thread.owner_id

        # Join the thread so the bot can post
        await thread.join()

        # Fetch the starter message content (retry — on_thread_create can fire
        # before the starter message is available via the API)
        starter_message = None
        for attempt in range(6):
            try:
                starter_message = await thread.fetch_message(thread_id)
                break
            except discord.NotFound:
                if attempt < 5:
                    await asyncio.sleep(2 ** attempt)  # 1, 2, 4, 8, 16s (~31s total)
                else:
                    raise
        thread_content = f"**{thread.name}**\n\n{starter_message.content}"

        # Transcribe image attachments so the reviewer reads the actual
        # application (e.g. writeups pasted as screenshots), not just the title.
        transcriptions = await self._transcribe_attachments(starter_message.attachments)
        for item in transcriptions:
            thread_content += f"\n\n## Attachment transcription ({item['filename']})\n{item['text']}"

        # Gate: never ask the reviewer to adjudicate a title-only / empty
        # application — it fabricates content when it does. needs_info is the
        # agent-free process answer; the reviewer only sees real applications.
        sufficient, reason = is_application_content_sufficient(thread.name, starter_message.content)
        if not sufficient:
            self.db.create_grant_application(
                thread_id, applicant_id, thread_content,
                attachment_urls=[], guild_id=thread.guild.id,
            )
            self.db.update_grant_status(thread_id, guild_id=thread.guild.id, status='needs_info')
            await thread.send(
                f"<@{applicant_id}> **More information needed**\n\n{reason}\n\n"
                f"Please reply here with your writeup as text and I'll review it."
            )
            return

        # Upload attachments to Supabase storage
        attachment_urls = await self._upload_attachments(thread_id, starter_message)

        # Upload applicant's avatar to permanent storage
        await self._upload_avatar(thread)

        # Record in DB
        self.db.create_grant_application(thread_id, applicant_id, thread_content, attachment_urls=attachment_urls, guild_id=thread.guild.id)

        # Fetch grant history and engagement data for this applicant
        grant_history = self.db.get_grant_history_for_applicant(applicant_id, guild_id=thread.guild.id)
        # Exclude the current application we just created
        grant_history = [g for g in grant_history if g.get('status') != 'reviewing' or g.get('thread_id') != thread_id]
        engagement = self.db.get_member_engagement(applicant_id, guild_id=thread.guild.id)

        # Fast-path: billing is paused — don't even call the LLM (saves the
        # 3 retries and the spam). This also protects cold-start catch-up.
        if _billing_paused():
            logger.warning(f"GrantsCog: skipping assessment for thread {thread_id} — billing paused")
            self.db.update_grant_status(thread_id, guild_id=thread.guild.id, status='needs_review')
            await thread.send(_BILLING_PAUSED_MESSAGE + f" {self._admin_mention} will refill shortly.")
            return
        # Assess with LLM
        try:
            assessment = await assess_application(
                thread_content,
                grant_history=grant_history or None,
                engagement=engagement,
                guild_id=getattr(thread.guild, 'id', None),
                server_config=getattr(self.db, 'server_config', None),
            )
        except Exception as e:
            if _is_billing_error(e):
                logger.error(f"GrantsCog: billing exhausted for thread {thread_id}: {e}")
                _note_billing_paused()
                self.db.update_grant_status(thread_id, guild_id=thread.guild.id, status='needs_review')
                await thread.send(_BILLING_PAUSED_MESSAGE + f" {self._admin_mention} will refill shortly.")
                try:
                    if getattr(self, 'admin_id', None):
                        user = self.bot.get_user(self.admin_id) or await self.bot.fetch_user(self.admin_id)
                        if user:
                            await user.send(f"Grants LLM billing exhausted (402) — paused reviews for {_BILLING_COOLDOWN_S//60} min. Please refill OpenRouter credits at https://openrouter.ai/credits. Thread {thread_id} triggered it.")
                except Exception:
                    pass
                return
            logger.error(
                f"GrantsCog: assessment failed for thread {thread_id}",
                exc_info=True,
            )
            # Don't strand the thread in 'reviewing': flag it needs_review so an
            # admin reply can drive the decision once the LLM path recovers.
            self.db.update_grant_status(thread_id, guild_id=thread.guild.id, status='needs_review')
            await thread.send(f"Unable to process this application right now. {self._admin_mention} will review it manually.")
            return

        await self._handle_assessment(thread, assessment)

    async def _reassess_application(self, thread: discord.Thread, grant: dict):
        """Re-assess after applicant provides more info."""
        thread_id = thread.id
        applicant_id = grant['applicant_id']

        # Gather full conversation: starter message + all follow-up messages
        messages = []
        applicant_text = []
        async for msg in thread.history(limit=100, oldest_first=True):
            if msg.author.bot:
                messages.append(f"[Reviewer]: {msg.content}")
            else:
                messages.append(f"[Applicant]: {msg.content}")
                applicant_text.append(msg.content)

        # Gate: same rule as new applications — never hand the reviewer a
        # thread whose only content is the title plus chatter. Without this,
        # any follow-up (even gibberish) promoted a title-only post to a full
        # LLM adjudication, and the reviewer fabricated a project from the
        # title alone.
        sufficient, reason = is_application_content_sufficient(thread.name, "\n".join(applicant_text))
        if not sufficient:
            self.db.update_grant_status(
                thread_id, guild_id=thread.guild.id, status='needs_info',
                thread_content=f"**{thread.name}**\n\n" + "\n\n".join(messages),
            )
            await thread.send(
                f"<@{applicant_id}> **More information needed**\n\n{reason}\n\n"
                f"Please reply here with your writeup as text and I'll review it."
            )
            return

        thread_content = f"**{thread.name}**\n\n" + "\n\n".join(messages)

        # Update stored content
        self.db.update_grant_status(thread_id, guild_id=thread.guild.id, status='reviewing', thread_content=thread_content)

        # Fetch grant history and engagement
        grant_history = self.db.get_grant_history_for_applicant(applicant_id, guild_id=thread.guild.id)
        # Exclude this application's own row while retaining other active and
        # needs-info applications so concurrent commitments reach the assessor.
        grant_history = [g for g in grant_history if g.get('thread_id') != thread_id]
        engagement = self.db.get_member_engagement(applicant_id, guild_id=thread.guild.id)

        try:
            assessment = await assess_application(
                thread_content,
                grant_history=grant_history or None,
                engagement=engagement,
                guild_id=getattr(thread.guild, 'id', None),
                server_config=getattr(self.db, 'server_config', None),
            )
        except Exception:
            logger.error(
                f"GrantsCog: re-assessment failed for thread {thread_id}",
                exc_info=True,
            )
            self.db.update_grant_status(thread_id, guild_id=thread.guild.id, status='needs_review')
            await thread.send(f"Unable to re-review right now. {self._admin_mention} will follow up.")
            return

        await self._handle_assessment(thread, assessment)

    async def _backfill_media(self, thread: discord.Thread, grant: dict):
        """Backfill attachments and avatar for an existing grant if missing."""
        needs_attachments = not grant.get('attachment_urls')
        member = self.db.get_member(thread.owner_id)
        needs_avatar = not (member and member.get('stored_avatar_url'))

        if not needs_attachments and not needs_avatar:
            return

        try:
            if needs_avatar:
                await self._upload_avatar(thread)

            if needs_attachments:
                starter_message = await thread.fetch_message(thread.id)
                if starter_message.attachments:
                    urls = await self._upload_attachments(thread.id, starter_message)
                    if urls:
                        self.db.update_grant_status(thread.id, grant['status'], guild_id=thread.guild.id, attachment_urls=urls)
                        logger.info(f"GrantsCog: backfilled {len(urls)} attachment(s) for thread {thread.id}")
        except Exception as e:
            logger.warning(f"GrantsCog: backfill failed for thread {thread.id}: {e}")

    async def _transcribe_attachments(self, attachments) -> list:
        """OCR image attachments into text for the reviewer (Gemini vision).

        Grant writeups are often pasted as screenshots; the reviewer must read
        the actual application, not infer it from the thread title. Returns a
        list of {'filename', 'text'} for readable images; failures are skipped
        (the content gate catches applications with no readable text at all).
        """
        if not attachments:
            return []
        try:
            from src.features.sharing.live_update_social.media_understanding import understand_image
        except Exception as e:
            logger.warning(f"GrantsCog: media understanding unavailable, skipping attachment transcription: {e}")
            return []

        transcribed = []
        for att in attachments:
            if not (att.content_type or '').startswith('image/'):
                continue
            try:
                result = await understand_image(
                    att.url,
                    prompt=(
                        "This image is part of a community micro-grant application. "
                        "Transcribe ALL text in it verbatim. Then summarize key "
                        "details about the project, compute needs, links, or prior "
                        "work. If there is no text, say 'No text in image'."
                    ),
                )
            except Exception as e:
                logger.warning(f"GrantsCog: transcription failed for {att.filename}: {e}")
                continue
            if result.ok and result.data and result.data.get('description'):
                transcribed.append({'filename': att.filename, 'text': result.data['description']})
                logger.info(f"GrantsCog: transcribed attachment {att.filename} for thread review")
            else:
                logger.warning(f"GrantsCog: transcription empty for {att.filename}: {getattr(result, 'error', 'unknown')}")
        return transcribed

    async def _upload_attachments(self, thread_id: int, message: discord.Message) -> list:
        """Download message attachments and upload to Supabase storage.

        Returns a list of dicts with 'filename' and 'url' for each uploaded file.
        """
        if not message.attachments or not self.storage:
            return []

        uploaded = []
        for att in message.attachments:
            storage_path = f"grants/{thread_id}/{att.filename}"
            try:
                url = await self.storage.download_and_upload_url(att.url, storage_path)
                if url:
                    uploaded.append({'filename': att.filename, 'url': url})
                    logger.info(f"GrantsCog: uploaded attachment {att.filename} for thread {thread_id}")
            except Exception as e:
                logger.warning(f"GrantsCog: failed to upload attachment {att.filename}: {e}")
        return uploaded

    async def _upload_avatar(self, thread: discord.Thread):
        """Upload the applicant's Discord avatar to permanent storage."""
        if not self.storage:
            return
        try:
            member = thread.guild.get_member(thread.owner_id)
            if not member or not member.display_avatar:
                return
            avatar_url = str(member.display_avatar.with_size(256).with_format('png'))
            storage_path = f"avatars/{member.id}.png"
            stored_url = await self.storage.download_and_upload_url(avatar_url, storage_path)
            if stored_url:
                self.db.update_member_stored_avatar(member.id, stored_url, guild_id=thread.guild.id)
                logger.info(f"GrantsCog: uploaded avatar for member {member.id}")
        except Exception as e:
            logger.warning(f"GrantsCog: failed to upload avatar for thread owner {thread.owner_id}: {e}")

    async def _apply_tag(self, thread: discord.Thread, tag_name: str):
        """Apply a forum tag to a thread if the tag exists."""
        tag = self._tags.get(tag_name.lower())
        if tag:
            try:
                await thread.edit(applied_tags=[tag])
            except Exception as e:
                logger.warning(f"GrantsCog: failed to apply tag '{tag_name}' to thread {thread.id}: {e}")

    async def _handle_assessment(self, thread: discord.Thread, assessment: dict):
        """Handle an LLM assessment result — update DB and reply."""
        thread_id = thread.id
        guild_id = thread.guild.id
        decision = assessment['decision']
        response = assessment['response']
        reasoning = assessment['reasoning']
        # Persist the full validated verdict. Admin review needs the original
        # decision, GPU, and hours as well as its private/public prose.
        llm_assessment = json.dumps(assessment)

        if decision == 'spam':
            self.db.update_grant_status(thread_id, guild_id=guild_id, status='rejected', llm_assessment=llm_assessment,
                                        rejected_at='now()')
            logger.info(f"GrantsCog: deleting spam thread {thread_id}: {reasoning[:100]}")
            try:
                await thread.delete()
            except Exception as e:
                logger.warning(f"GrantsCog: failed to delete spam thread {thread_id}: {e}")
            return

        if decision == 'needs_info':
            self.db.update_grant_status(
                thread_id,
                guild_id=guild_id,
                status='needs_info',
                llm_assessment=llm_assessment,
            )
            await thread.send(
                f"**More information needed**\n\n{response}\n\n"
                f"Please reply here with the requested details and I'll re-review your application."
            )
        elif decision == 'needs_review':
            # Include LLM's recommended GPU/hours if provided — framed so the
            # applicant can't mistake it for acceptance and jump ahead to the
            # wallet step. The reviewer's reasoning stays out of the thread
            # (the assessor prompt treats it as internal).
            details = ""
            if assessment.get('gpu_type') and assessment.get('recommended_hours'):
                gpu_type = assessment['gpu_type']
                hours = assessment['recommended_hours']
                cost = calculate_grant_cost(gpu_type, hours)
                details = (
                    f"\n\n**Reviewer recommendation (pending human approval):** "
                    f"{gpu_type.replace('_', ' ')} / {hours}hrs / ${cost:.2f}"
                )
            await thread.send(
                f"<@{thread.owner_id}> {response}\n\n"
                f"**This is not an approval yet** — a moderator will review your application manually. "
                f"If you have any additional links, examples, or details that would strengthen "
                f"your application, please share them here. "
                f"Please don't send a wallet address until you're asked."
                f"{details}\n"
                f"{self._admin_mention} **Manual review needed**"
            )
            self.db.update_grant_status(thread_id, guild_id=guild_id, status='needs_review', llm_assessment=llm_assessment)

        elif decision == 'rejected':
            await thread.send(f"**Application not approved**\n\n{response}")
            self.db.update_grant_status(thread_id, guild_id=guild_id, status='rejected', llm_assessment=llm_assessment,
                                        rejected_at='now()')
            try:
                await thread.edit(archived=True)
            except Exception as e:
                logger.warning(f"GrantsCog: failed to archive rejected thread {thread_id}: {e}")

        elif decision == 'approved':
            gpu_type = assessment['gpu_type']
            hours = assessment['recommended_hours']
            rate = GPU_RATES[gpu_type]
            total_cost = calculate_grant_cost(gpu_type, hours)

            # Send the approval message BEFORE updating DB status to awaiting_wallet.
            # If the message fails (e.g. Discord 503), the grant stays in reviewing
            # and the applicant won't be incorrectly prompted for a wallet address.
            await self._apply_tag(thread, 'accepted')
            await thread.send(
                f"<@{thread.owner_id}> **Grant Approved!**\n\n"
                f"{response}\n\n"
                f"**Grant Details:**\n"
                f"- GPU: {gpu_type.replace('_', ' ')}\n"
                f"- Hours: {hours}\n"
                f"- Rate: ${rate:.2f}/hr (+ 10% fee buffer)\n"
                f"- Total: ${total_cost:.2f} USD (paid in SOL)\n\n"
                f"Please reply with your **Solana wallet address** to receive the grant."
            )

            self.db.update_grant_status(
                thread_id, 'awaiting_wallet',
                guild_id=guild_id,
                llm_assessment=llm_assessment,
                gpu_type=gpu_type,
                recommended_hours=hours,
                gpu_rate_usd=rate,
                total_cost_usd=total_cost,
                approved_at='now()',
            )

    def _explorer_url(self, tx_sig: str) -> str:
        """Build a Solana Explorer URL for a transaction."""
        rpc_url = os.getenv('SOLANA_RPC_URL', '')
        if 'devnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=devnet"
        elif 'testnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=testnet"
        return f"https://explorer.solana.com/tx/{tx_sig}"

    async def _start_payment_flow(self, thread: discord.Thread, grant: Dict[str, Any], wallet: str):
        """Register the wallet and queue the shared payment flow."""
        if not self.payment_service:
            await thread.send(f"Payment system is not configured. {self._admin_mention} will process this manually.")
            return

        guild_id = thread.guild.id
        try:
            wallet_record = self.db.upsert_wallet(
                guild_id=guild_id,
                discord_user_id=grant['applicant_id'],
                chain='solana',
                address=wallet,
                metadata={'producer': 'grants', 'thread_id': thread.id},
            )
        except WalletUpdateBlockedError:
            await thread.send(
                "I couldn't update that wallet because there is already an active payment in flight for you. "
                "Please wait for the current payment flow to finish or ask an admin for manual review."
            )
            return
        if not wallet_record:
            raise RuntimeError("failed to register wallet for payment verification")

        destinations = self._resolve_payment_destinations(thread)
        test_payment = await self.payment_service.request_payment(
            producer='grants',
            producer_ref=str(thread.id),
            guild_id=guild_id,
            recipient_wallet=wallet,
            chain='solana',
            provider='solana_grants',
            is_test=True,
            confirm_channel_id=destinations['confirm_channel_id'],
            confirm_thread_id=destinations['confirm_thread_id'],
            notify_channel_id=destinations['notify_channel_id'],
            notify_thread_id=destinations['notify_thread_id'],
            recipient_discord_id=grant['applicant_id'],
            wallet_id=wallet_record.get('wallet_id'),
            route_key=destinations.get('route_key'),
            metadata={'grant_thread_id': thread.id},
        )
        if not test_payment:
            raise RuntimeError("failed to create test payment request")

        self.db.update_grant_status(
            thread.id,
            'payment_requested',
            guild_id=guild_id,
            wallet_address=wallet,
            payment_status='test_requested',
        )

        confirmed_test = self.payment_service.confirm_payment(
            test_payment['payment_id'],
            guild_id=guild_id,
            actor=PaymentActor(PaymentActorKind.AUTO, grant['applicant_id']),
        )
        if not confirmed_test:
            raise RuntimeError("failed to queue the test payment")

        await self._apply_tag(thread, 'in progress')
        await thread.send(
            "Thanks. I’ve queued a small test payment to verify that wallet.\n\n"
            "Once that lands, I’ll send the full grant payment confirmation prompt."
        )

    def _resolve_payment_destinations(self, thread: discord.Thread) -> Dict[str, Optional[int]]:
        server_config = getattr(self.db, 'server_config', None) if self.db else None
        resolved = None
        if server_config:
            resolved = server_config.resolve_payment_destinations(thread.guild.id, thread.id, 'grants')
        if resolved:
            return resolved

        return {
            'route_key': None,
            'confirm_channel_id': thread.parent_id or thread.id,
            'confirm_thread_id': thread.id if thread.parent_id else None,
            'notify_channel_id': thread.parent_id or thread.id,
            'notify_thread_id': thread.id if thread.parent_id else None,
        }

    def _get_payment_ui_cog(self):
        return (
            getattr(self.bot, 'payment_ui_cog', None)
            or self.bot.get_cog('PaymentUICog')
        )

    async def _fetch_grant_thread(self, thread_id: int) -> Optional[discord.Thread]:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except Exception:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def handle_payment_result(self, payment: Dict[str, Any]):
        """Receive terminal payment outcomes from the shared payment cog."""
        if str(payment.get('producer') or '').strip().lower() != 'grants':
            return

        try:
            thread_id = int(payment.get('producer_ref'))
        except (TypeError, ValueError):
            logger.warning("[GrantsCog] Ignoring payment result with invalid producer_ref: %s", payment.get('producer_ref'))
            return

        guild_id = payment.get('guild_id')
        grant = self.db.get_grant_by_thread(thread_id, guild_id=guild_id)
        thread = await self._fetch_grant_thread(thread_id)

        if payment.get('is_test'):
            await self._handle_test_payment_result(payment, grant, thread)
            return

        await self._handle_final_payment_result(payment, grant, thread)

    async def _handle_test_payment_result(
        self,
        payment: Dict[str, Any],
        grant: Optional[Dict[str, Any]],
        thread: Optional[discord.Thread],
    ):
        status = payment.get('status')
        if status != 'confirmed':
            if thread:
                await thread.send(
                    f"The wallet verification payment ended in `{status}`. "
                    f"{self._admin_mention} will review it before any final payout is sent."
                )
            return

        if not grant:
            logger.warning("[GrantsCog] Missing grant row for confirmed test payment %s", payment.get('payment_id'))
            return

        final_payment = await self.payment_service.request_payment(
            producer='grants',
            producer_ref=str(grant['thread_id']),
            guild_id=grant['guild_id'],
            recipient_wallet=payment['recipient_wallet'],
            chain='solana',
            provider='solana_grants',
            is_test=False,
            amount_usd=float(grant['total_cost_usd']),
            confirm_channel_id=payment['confirm_channel_id'],
            confirm_thread_id=payment.get('confirm_thread_id'),
            notify_channel_id=payment['notify_channel_id'],
            notify_thread_id=payment.get('notify_thread_id'),
            recipient_discord_id=grant['applicant_id'],
            wallet_id=payment.get('wallet_id'),
            route_key=payment.get('route_key'),
            metadata={'grant_thread_id': grant['thread_id']},
        )
        if not final_payment:
            if thread:
                await thread.send(
                    f"The wallet test payment succeeded, but I couldn't create the final payment request. "
                    f"{self._admin_mention} will follow up."
                )
            return

        payment_ui_cog = self._get_payment_ui_cog()
        if payment_ui_cog and final_payment.get('status') == 'pending_confirmation':
            await payment_ui_cog.send_confirmation_request(final_payment['payment_id'])

        if thread:
            await thread.send(
                "Test payment confirmed.\n\n"
                f"Please confirm the full grant payout using the payment prompt in {self._format_payment_destination(final_payment)}."
            )

    async def _handle_final_payment_result(
        self,
        payment: Dict[str, Any],
        grant: Optional[Dict[str, Any]],
        thread: Optional[discord.Thread],
    ):
        status = payment.get('status')
        if status != 'confirmed':
            if thread:
                await thread.send(
                    f"The final payment ended in `{status}`. "
                    f"{self._admin_mention} will review it before any further action."
                )
            return

        if grant and grant.get('status') != 'paid':
            self.db.record_grant_payment(
                grant['thread_id'],
                payment.get('tx_signature'),
                float(payment.get('amount_token') or 0),
                float(payment.get('token_price_usd') or 0),
                guild_id=grant['guild_id'],
            )

        if thread:
            gpu_type = str((grant or {}).get('gpu_type') or 'compute').replace('_', ' ')
            hours = (grant or {}).get('recommended_hours')
            await thread.send(
                f"**Payment sent!**\n\n"
                f"- Amount: {float(payment.get('amount_token') or 0):.4f} SOL\n"
                f"- Wallet: `{payment.get('recipient_wallet')}`\n"
                f"- Transaction: [View on Explorer]({self._explorer_url(payment.get('tx_signature'))})\n\n"
                f"Your compute grant for {gpu_type} ({hours}hrs) has been funded. Good luck with your project!"
            )
            try:
                await thread.edit(archived=True)
            except Exception as e:
                logger.warning(f"GrantsCog: failed to archive paid thread {thread.id}: {e}")

    def _format_payment_destination(self, payment: Dict[str, Any]) -> str:
        destination_id = payment.get('confirm_thread_id') or payment.get('confirm_channel_id')
        if destination_id:
            return f"<#{destination_id}>"
        return "the configured payment channel"
