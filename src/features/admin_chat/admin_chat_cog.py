"""Discord cog for privileged admin chat and approved member bot access."""
import asyncio
import os
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Dict, Literal, Optional
from uuid import uuid4
import discord
from discord.ext import commands, tasks

from src.common.llm.deepseek_client import DeepSeekClient
from src.common.db_handler import WalletUpdateBlockedError
from src.features.payments.payment_service import PaymentActor, PaymentActorKind
from .agent import AdminChatAgent
from src.features.grants.solana_client import is_valid_solana_address

logger = logging.getLogger('DiscordBot')

_ADMIN_FALLBACK_REPLY_LINES = frozenset({
    "I hit an internal error while trying to do that.",
    "I couldn't complete that right now because the model API failed.",
    "Sorry, something went wrong on my side. Try again in a moment.",
})


def _preview_text(content: str, limit: int) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= limit:
        return text

    clipped = text[:limit].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return f"{clipped}..."


def _parse_user_id_list(raw_user_ids: str | None, *, env_name: str) -> set[int]:
    """Parse a comma/whitespace separated list of Discord user IDs."""
    if not raw_user_ids:
        return set()

    user_ids: set[int] = set()
    for token in re.split(r"[\s,]+", raw_user_ids.strip()):
        if not token:
            continue
        try:
            user_ids.add(int(token))
        except ValueError:
            logger.error("[AdminChat] Invalid %s entry: %r", env_name, token)
    return user_ids


_CLASSIFIER_TOOL = {
    "name": "classify_payment_reply",
    "description": "Classify whether a recipient confirms seeing the test SOL payment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": ["confirmed", "not_received", "unclear"],
            },
            "reply": {
                "type": "string",
                "description": (
                    "Optional recipient-facing clarification to send only when the reply is unclear."
                ),
            },
        },
        "required": ["classification"],
        "additionalProperties": False,
    },
}

_CLASSIFIER_SYSTEM_PROMPT = """
You classify one recipient reply to a bot asking whether a small test SOL payment is visible in their wallet.

Return classification:
- confirmed: only if the recipient clearly says they see or received the test payment in their wallet.
- not_received: if the recipient clearly says they do not see it, did not receive it, or it is missing.
- unclear: if the reply is ambiguous, noncommittal, says they are checking, or does not clearly answer.

Strict rules:
- "ok", "okay", "thanks", or similar acknowledgments alone are NOT confirmed.
- "wait let me check" and similar checking-in-progress replies are unclear.
- Use only the single recipient message. Do not assume memory, prior channel history, or unstated facts.
- If classification is unclear, you may include a brief recipient-facing clarification reply that asks them to say confirmed/yes once they see the test SOL, or not received if they do not.
- Call the provided tool exactly once and do not output anything outside the tool call.
""".strip()


# ── Built-in live-update-feedback agent guidance ──────────────────────────
# Injected as channel_guidance when an admin replies in a live channel.
# The caller may override this with a per-channel DB value via
# ServerConfig.get_channel_agent_guidance().
LIVE_UPDATE_FEEDBACK_GUIDANCE = """\
## Live Update Feedback Channel

This channel is for admin feedback on live updates. When an admin replies to
a bot-posted live-update message, you are handling that feedback turn.

- The replied-to message is part of a specific live update. You are given the
  live update context — research it using your available tools as needed.
- **Log the feedback** by calling `log_live_update_feedback` with the admin's
  feedback text and an optional disposition (e.g. "correction", "approval",
  "deletion-request"). The live update (topic_id) is injected from context.
- If the feedback calls for a change, **act on the update itself**:
  - Edit the update message(s) with `edit_message`.
  - Delete the update message(s) with `delete_message` — the bot will
    soft-delete the live update (state "deleted"; row retained).
- After you log the feedback, the admin's reply will be ✅ acknowledged
  and then **removed** from the channel to keep it clean. This turn is a
  silent action — do not send a separate confirmation reply."""

SOCIAL_DRAFT_REVIEW_GUIDANCE = """\
## Social Draft Review Channel

This channel is for admin review of social-media draft posts. When an admin
replies to a bot-posted draft, you are handling that review turn.

- The draft run is injected into context (run_id, draft_text, media decisions).
  Research it further with your available tools as needed.
- **Available tools:** `update_social_draft`, `approve_social_draft`,
  `publish_social_draft`, `preview_social_draft`, `discard_social_draft`.
- **Approval** requires `admin_approval_quote` — a verbatim phrase from the
  admin's message. Do not infer or paraphrase; copy exactly.
- **Approval and publish are always separate turns.** Approve first, then wait
  for a distinct publish instruction. Never combine them in one action.
- **Any edit resets approval.** After calling `update_social_draft`, the run
  returns to pending; re-approval is required before publishing.
- **Never auto-post.** Only call `publish_social_draft` when the admin has
  explicitly requested it after approving.
- If this reply has no matching draft context (orphaned reply), call
  `list_pending_social_drafts` to surface the correct run for the admin."""

SOCIAL_FAILED_REVIEW_GUIDANCE = """\
## Social Failed-Publish Review

This channel is for admin review of a social post whose publish FAILED. The
run is injected into context (run_id, draft_text, failure reason).

- **The draft is preserved and still approved** — once the cause is fixed you
  may retry with `publish_social_draft`, or edit with `update_social_draft`
  (which resets approval, so re-approve after editing), or discard with
  `discard_social_draft`.
- **Common causes:** no social route configured for the platform (check
  `list_social_routes` / `create_social_route`), provider/API failure, or
  media upload failure. Diagnose before retrying — a blind retry of the same
  broken state wastes a turn.
- **Approval and publish are always separate turns.** If you edit, re-approve
  first, then wait for a distinct publish instruction.
- If this reply has no matching failed-run context (orphaned reply), call
  `list_pending_social_drafts` to surface the correct run for the admin."""

SOCIAL_PROPOSAL_REVIEW_GUIDANCE = """\
## Social Proposal Review Channel

This channel is for admin review of social-media post IDEAS. When an admin
replies to a bot-posted proposal set, you are handling that review turn.
replies to a bot-posted proposal set, you are handling that review turn.

- The proposed run is injected into context (run_id, proposals list). Each
  proposal has a theme, a media strategy, the source messages it is based on,
  and a rationale.
- The admin will usually pick one idea to develop. When they do, YOU draft the
  tweet text from that proposal — apply its theme and media strategy, keep the
  creator credited, no hype inflation — then call `develop_social_proposal`
  with run_id, proposal_index (1-based), and your draft_text. That converts the
  proposal into a normal draft.
- **Development and approval/publish are always separate turns.** After calling
  `develop_social_proposal`, END your turn by showing the resulting draft and
  asking for feedback. Never call `approve_social_draft` or
  `publish_social_draft` in the same turn as development.
- After development the run becomes a draft and the normal draft review loop
  applies: `update_social_draft`, `approve_social_draft`,
  `publish_social_draft`, `preview_social_draft`, `discard_social_draft`.
  Iterate with the admin until the draft is perfect before publishing.
- **Any edit resets approval.** After calling `update_social_draft`, the run
  returns to pending; re-approval is required before publishing.
- **Never auto-post.** Only call `publish_social_draft` when the admin has
  explicitly requested it after approving.
- If the admin says skip or discard, use `discard_social_draft`.
- If this reply has no matching proposal context (orphaned reply), call
  `list_pending_social_drafts` to surface the correct run for the admin."""


class AdminChatCog(commands.Cog):
    """Cog that handles admin chat plus approved member requests."""

    _ACCESS_CACHE_TTL_SECONDS = 60
    _RATE_LIMIT_WINDOW_SECONDS = 300
    _RATE_LIMIT_MAX_MESSAGES = 10
    _WALLET_DELIMITER_RE = re.compile(r"[\s`<>\"'(),\[\]{}*_~|]+")
    _WALLET_TRAILING_PUNCTUATION = ".,!?;:"
    _CONFIRMATION_POSITIVE_KEYWORDS = (
        'confirmed',
        'received',
        'got it',
        'yes',
        'yep',
        'confirm',
    )
    _CONFIRMATION_NEGATIVE_KEYWORDS = (
        'no',
        'didnt',
        'not received',
        'missing',
        'nothing',
    )
    _CONFIRMATION_POSITIVE_EMOJI = '👍'

    def __init__(self, bot: commands.Bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        self.agent: AdminChatAgent = None
        self.payment_service = getattr(bot, 'payment_service', None)

        # Track whether the agent is busy processing a request per user
        self._busy: dict[int, bool] = {}
        # Queue follow-up messages that arrive while agent is busy
        self._pending_messages: dict[int, discord.Message] = {}
        self._guild_context_cache: dict[int, tuple[float, int | None]] = {}
        self._rate_limits: dict[int, deque[float]] = {}
        self._processing_intents: set[str] = set()
        self._classifier_model = os.getenv(
            "ADMIN_CHAT_CLASSIFIER_MODEL",
            os.getenv("ADMIN_CHAT_MODEL", "deepseek-v4-pro"),
        )
        try:
            self._classifier_client = DeepSeekClient()
        except Exception:
            self._classifier_client = None
            logger.warning(
                "[AdminChat] DEEPSEEK_API_KEY not set - payment reply classification will use keyword fallback"
            )

        # Get admin user ID
        admin_id_str = os.getenv('ADMIN_USER_ID')
        if admin_id_str:
            try:
                self.admin_user_id = int(admin_id_str)
                logger.info(f"[AdminChat] Configured for admin user ID: {self.admin_user_id}")
            except ValueError:
                logger.error(f"[AdminChat] Invalid ADMIN_USER_ID: {admin_id_str}")
                self.admin_user_id = None
        else:
            logger.warning("[AdminChat] ADMIN_USER_ID not set - admin chat disabled")
            self.admin_user_id = None
        self._allowed_admin_chat_user_ids = _parse_user_id_list(
            os.getenv('ADMIN_CHAT_ALLOWED_USER_IDS'),
            env_name='ADMIN_CHAT_ALLOWED_USER_IDS',
        )
        if self.admin_user_id is not None:
            self._allowed_admin_chat_user_ids.add(self.admin_user_id)
        if self._allowed_admin_chat_user_ids:
            logger.info(
                "[AdminChat] Admin chat enabled for user IDs: %s",
                sorted(self._allowed_admin_chat_user_ids),
            )
        self._admin_mention = f"<@{self.admin_user_id}>" if self.admin_user_id else "the admin"
        self._startup_reconciled = False

    def _live_environment(self) -> str:
        """Return the current environment string for live-update lookups (SD5)."""
        return "dev" if self.db_handler.dev_mode else "prod"

    @classmethod
    def _parse_wallet_from_text(cls, content: str) -> Optional[str]:
        """Extract the first valid Solana wallet-looking token from freeform text."""
        if not content:
            return None

        for token in cls._WALLET_DELIMITER_RE.split(content):
            candidate = token.rstrip(cls._WALLET_TRAILING_PUNCTUATION).strip()
            if candidate and is_valid_solana_address(candidate):
                return candidate
        return None

    @classmethod
    def _classify_confirmation_keyword(cls, content: str) -> Literal['positive', 'negative', 'ambiguous']:
        """Classify a recipient confirmation reply using deterministic keywords only."""
        if not content:
            return 'ambiguous'

        lowered = content.casefold()

        def keyword_spans(keyword: str) -> list[tuple[int, int]]:
            pattern = rf'(?<!\w){re.escape(keyword.casefold())}(?!\w)'
            return [
                (match.start(), match.end())
                for match in re.finditer(pattern, lowered, flags=re.IGNORECASE)
            ]

        negative_spans = [
            span
            for keyword in cls._CONFIRMATION_NEGATIVE_KEYWORDS
            for span in keyword_spans(keyword)
        ]
        positive_keyword_spans = [
            span
            for keyword in cls._CONFIRMATION_POSITIVE_KEYWORDS
            for span in keyword_spans(keyword)
        ]

        def is_shadowed_by_negative_phrase(span: tuple[int, int]) -> bool:
            start, end = span
            return any(
                max(start, negative_start) < min(end, negative_end)
                for negative_start, negative_end in negative_spans
            )

        # Negative phrases such as "not received" should win over the positive
        # keyword they contain. Only standalone positives outside a negative
        # phrase span should upgrade the classification to ambiguous.
        positive_outside_negative_phrase = any(
            not is_shadowed_by_negative_phrase(span)
            for span in positive_keyword_spans
        )
        positive = cls._CONFIRMATION_POSITIVE_EMOJI in content or positive_outside_negative_phrase
        negative = bool(negative_spans)

        if positive and not negative:
            return 'positive'
        if negative and not positive:
            return 'negative'
        return 'ambiguous'

    @staticmethod
    def _strip_fallback_reply_lines(content: str) -> str:
        """Avoid echoing stale generic failure messages from recent DM/log context."""
        lines = str(content or "").splitlines()
        kept = [line for line in lines if line.strip() not in _ADMIN_FALLBACK_REPLY_LINES]
        return "\n".join(kept).strip()

    def _claim_admin_chat_message(self, message: discord.Message) -> bool:
        claim = getattr(self.db_handler, "try_claim_bot_event", None)
        if not callable(claim):
            return True

        deployment_id = os.getenv("RAILWAY_DEPLOYMENT_ID") or os.getenv("RAILWAY_SERVICE_ID")
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        if guild_id is None and getattr(self.bot, "guilds", None):
            guild_id = self.bot.guilds[0].id
        return bool(claim(
            event_key=f"admin_chat_message:{message.id}",
            event_type="admin_chat_message",
            payload={
                "guild_id": guild_id,
                "message_id": str(message.id),
                "channel_id": str(getattr(message.channel, "id", "")),
                "author_id": str(getattr(message.author, "id", "")),
                "deployment_id": deployment_id,
                "commit_sha": (
                    os.getenv("RAILWAY_GIT_COMMIT_SHA")
                    or os.getenv("GIT_COMMIT_SHA")
                    or os.getenv("SOURCE_COMMIT")
                    or os.getenv("COMMIT_SHA")
                ),
            },
        ))

    async def _classify_confirmation(self, content: str) -> tuple[Literal['positive', 'negative', 'ambiguous'], Optional[str]]:
        """Classify a recipient confirmation reply via LLM tool use with keyword fallback."""
        keyword_result = self._classify_confirmation_keyword(content)
        if self._classifier_client is None:
            return keyword_result, None

        try:
            response = await self._classifier_client.generate_chat_completion(
                model=self._classifier_model,
                system_prompt=_CLASSIFIER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content or ""}],
                max_tokens=256,
                tools=[_CLASSIFIER_TOOL],
            )
        except Exception:
            logger.warning(
                "[AdminChat] LLM confirmation classifier failed; falling back to keywords",
                exc_info=True,
            )
            return keyword_result, None

        blocks = getattr(response, "content", None) or []
        tool_input: Dict[str, object] | None = None
        for block in blocks:
            block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            block_name = block.get("name") if isinstance(block, dict) else getattr(block, "name", None)
            if block_type == "tool_use" and block_name == "classify_payment_reply":
                tool_input = block.get("input") if isinstance(block, dict) else getattr(block, "input", None)
                break

        if not isinstance(tool_input, dict):
            return keyword_result, None

        classification = tool_input.get("classification")
        mapped = {
            "confirmed": "positive",
            "not_received": "negative",
            "unclear": "ambiguous",
        }.get(classification)
        if mapped is None:
            return keyword_result, None

        reply_value = tool_input.get("reply")
        reply_text = reply_value.strip() if isinstance(reply_value, str) and reply_value.strip() else None
        return mapped, reply_text

    async def _notify_admin_review(self, channel, intent: Dict, detail: str, *, fail_intent: bool = True, resolved_by_message_id: int | None = None):
        """Fail closed and tag the admin for manual review."""
        if fail_intent:
            payload: Dict[str, object] = {'status': 'failed'}
            if resolved_by_message_id is not None:
                payload['resolved_by_message_id'] = resolved_by_message_id
            self.db_handler.update_admin_payment_intent(intent['intent_id'], payload, intent['guild_id'])
        try:
            await channel.send(
                f"{self._admin_mention} review needed for payment intent `{intent.get('intent_id')}` "
                f"for <@{intent.get('recipient_user_id')}>. {detail}"
            )
        except Exception as e:
            logger.error(f"[AdminChat] Failed to notify admin for intent {intent.get('intent_id')}: {e}", exc_info=True)

    async def _notify_intent_admin(self, intent: Dict, detail: str):
        """DM the initiating admin about one payment milestone when possible."""
        admin_user_id = intent.get('admin_user_id') or self.admin_user_id
        try:
            admin_user_id = int(admin_user_id) if admin_user_id is not None else None
        except (TypeError, ValueError):
            admin_user_id = None
        if not admin_user_id:
            return
        try:
            admin_user = await self.bot.fetch_user(admin_user_id)
            await admin_user.send(detail)
        except Exception as e:
            logger.warning(
                "[AdminChat] Failed to DM admin %s for intent %s: %s",
                admin_user_id,
                intent.get('intent_id'),
                e,
            )

    def _resolve_payment_destinations(self, channel) -> Dict[str, int | None]:
        """Resolve persisted payment destinations with a safe in-channel fallback."""
        server_config = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        if server_config:
            resolved = server_config.resolve_payment_destinations(channel.guild.id, channel.id, 'admin_chat')
            if resolved:
                return resolved
        parent_id = getattr(channel, 'parent_id', None)
        return {
            'route_key': None,
            'confirm_channel_id': parent_id or channel.id,
            'confirm_thread_id': channel.id if parent_id else None,
            'notify_channel_id': parent_id or channel.id,
            'notify_thread_id': channel.id if parent_id else None,
        }

    def _resolve_admin_payment_provider(self, channel) -> str:
        """Choose the funding provider for admin-triggered payments from channel context."""
        server_config = getattr(self.db_handler, 'server_config', None) if self.db_handler else None
        if not server_config:
            return 'solana_payouts'

        try:
            server = server_config.get_first_server_with_field('grants_channel_id', require_write=True)
        except Exception as exc:
            logger.warning("[AdminChat] Failed to resolve grants channel for payment provider: %s", exc)
            return 'solana_payouts'

        grants_channel_id = server.get('grants_channel_id') if server else None
        if not grants_channel_id:
            return 'solana_payouts'

        try:
            grants_channel_id = int(grants_channel_id)
        except (TypeError, ValueError):
            logger.warning("[AdminChat] Invalid grants_channel_id in server config: %r", grants_channel_id)
            return 'solana_payouts'

        parent_id = getattr(channel, 'parent_id', None)
        if parent_id == grants_channel_id:
            return 'solana_grants'
        return 'solana_payouts'

    def _get_payment_ui_cog(self):
        return (
            getattr(self.bot, 'payment_ui_cog', None)
            or self.bot.get_cog('PaymentUICog')
        )

    async def _fetch_intent_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def _set_intent_status_message(self, intent: Dict, channel, content: str, *, _retry_on_missing: bool = True):
        status_message_id = intent.get('status_message_id')
        if status_message_id:
            fetch_message = getattr(channel, 'fetch_message', None)
            if callable(fetch_message):
                try:
                    message = await fetch_message(int(status_message_id))
                except discord.NotFound:
                    intent['status_message_id'] = None
                    if not _retry_on_missing:
                        raise
                    return await self._set_intent_status_message(
                        intent,
                        channel,
                        content,
                        _retry_on_missing=False,
                    )
            else:
                message = None

            if message is not None:
                edit = getattr(message, 'edit', None)
                if callable(edit):
                    await edit(content=content, suppress=True)
                else:
                    message.content = content
                return message

        message = await channel.send(content, suppress_embeds=True)
        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'status_message_id': message.id},
            intent['guild_id'],
        )
        if updated_intent:
            intent.update(updated_intent)
        else:
            intent['status_message_id'] = message.id
        return message

    def _format_payment_destination(self, payment: Dict) -> str:
        destination_id = payment.get('confirm_thread_id') or payment.get('confirm_channel_id')
        if destination_id:
            return f"<#{destination_id}>"
        return "the configured payment channel"

    def _explorer_url(self, tx_sig: str) -> str:
        rpc_url = os.getenv('SOLANA_RPC_URL', '')
        if 'devnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=devnet"
        if 'testnet' in rpc_url:
            return f"https://explorer.solana.com/tx/{tx_sig}?cluster=testnet"
        return f"https://explorer.solana.com/tx/{tx_sig}"

    async def _advance_intent_to_test_phase(
        self,
        channel,
        intent: Dict,
        wallet_record: Dict,
        *,
        resolved_by_message_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """Move one existing intent from wallet-collection into the test-payment phase.

        Shared by every path that has a wallet ready for an existing intent:
          - `_handle_wallet_received`: recipient posts their wallet in the thread.
          - `execute_upsert_wallet_for_user`: admin upserts a wallet on behalf of
            a user who has a pending awaiting_wallet intent.

        Updates the intent row to status='awaiting_test' with the wallet_id, then
        kicks off `_start_admin_payment_flow` which creates the verification
        test payment and auto-confirms it so the worker can pick it up.
        Returns the updated intent on success or None on failure.
        """
        updates: Dict[str, Any] = {
            'status': 'awaiting_test',
            'wallet_id': wallet_record.get('wallet_id'),
        }
        if resolved_by_message_id is not None:
            updates['resolved_by_message_id'] = resolved_by_message_id
        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            updates,
            intent['guild_id'],
        )
        if not updated_intent:
            await self._notify_admin_review(
                channel,
                intent,
                "I could not update the payment intent after receiving the wallet.",
                resolved_by_message_id=resolved_by_message_id,
            )
            return None
        await self._start_admin_payment_flow(channel, updated_intent)
        return updated_intent

    async def _handle_wallet_received(self, message: discord.Message, intent: Dict, wallet_address: str):
        """Persist a wallet reply and kick off the test-payment flow."""
        try:
            wallet_record = self.db_handler.upsert_wallet(
                guild_id=int(intent['guild_id']),
                discord_user_id=int(intent['recipient_user_id']),
                chain='solana',
                address=wallet_address,
                metadata={'producer': 'admin_chat', 'intent_id': intent['intent_id'], 'channel_id': message.channel.id},
            )
        except WalletUpdateBlockedError:
            await self._notify_admin_review(
                message.channel,
                intent,
                "I could not update the wallet because an active payment is already in flight for this user.",
                resolved_by_message_id=message.id,
            )
            return
        if not wallet_record:
            await self._notify_admin_review(message.channel, intent, "I could not store the recipient wallet.", resolved_by_message_id=message.id)
            return

        await self._advance_intent_to_test_phase(
            message.channel,
            intent,
            wallet_record,
            resolved_by_message_id=message.id,
        )

    async def _start_admin_payment_flow(self, channel, intent: Dict):
        """Create and auto-confirm the verification payment for one admin payment intent."""
        if not self.payment_service:
            await self._notify_admin_review(channel, intent, "The payment service is not configured.")
            return
        wallet_id = intent.get('wallet_id')
        wallet_record = self.db_handler.get_wallet_by_id(wallet_id, guild_id=intent['guild_id']) if wallet_id else None
        if not wallet_record:
            await self._notify_admin_review(channel, intent, "No verified wallet record is available for the recipient.")
            return

        destinations = self._resolve_payment_destinations(channel)
        provider = self._resolve_admin_payment_provider(channel)
        test_payment = await self.payment_service.request_payment(
            producer='admin_chat',
            producer_ref=str(intent['producer_ref']),
            guild_id=int(intent['guild_id']),
            recipient_wallet=wallet_record['wallet_address'],
            chain='solana',
            provider=provider,
            is_test=True,
            confirm_channel_id=destinations['confirm_channel_id'],
            confirm_thread_id=destinations['confirm_thread_id'],
            notify_channel_id=destinations['notify_channel_id'],
            notify_thread_id=destinations['notify_thread_id'],
            recipient_discord_id=int(intent['recipient_user_id']),
            wallet_id=wallet_record.get('wallet_id'),
            route_key=destinations.get('route_key'),
            metadata={'intent_id': intent['intent_id']},
        )
        if not test_payment:
            await self._notify_admin_review(channel, intent, "I could not create the wallet verification payment.")
            return

        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'test_payment_id': test_payment.get('payment_id')},
            intent['guild_id'],
        ) or intent
        confirmed = self.payment_service.confirm_payment(
            test_payment['payment_id'],
            guild_id=int(intent['guild_id']),
            actor=PaymentActor(PaymentActorKind.AUTO, int(intent['recipient_user_id'])),
        )
        if not confirmed:
            await self._notify_admin_review(channel, updated_intent, "I could not queue the wallet verification payment.")
            return

        await self._set_intent_status_message(
            updated_intent,
            channel,
            "Verifying wallet with a small test payment…",
        )

    async def _gate_existing_intent(self, channel, intent: Dict, wallet_record: Dict, amount_sol: float):
        """Create the real payment for an existing intent, then move it behind admin DM approval."""
        if not self.payment_service:
            await self._notify_admin_review(channel, intent, "The payment service is not configured for the final payout.")
            return None
        if not wallet_record or not wallet_record.get('wallet_address'):
            await self._notify_admin_review(channel, intent, "No wallet record is available for the final payout.")
            return None

        destinations = self._resolve_payment_destinations(channel)
        provider = self._resolve_admin_payment_provider(channel)
        payment = await self.payment_service.request_payment(
            producer='admin_chat',
            producer_ref=str(intent['producer_ref']),
            guild_id=int(intent['guild_id']),
            recipient_wallet=wallet_record['wallet_address'],
            chain='solana',
            provider=provider,
            is_test=False,
            amount_token=float(amount_sol),
            confirm_channel_id=destinations['confirm_channel_id'],
            confirm_thread_id=destinations['confirm_thread_id'],
            notify_channel_id=destinations['notify_channel_id'],
            notify_thread_id=destinations['notify_thread_id'],
            recipient_discord_id=int(intent['recipient_user_id']),
            wallet_id=wallet_record.get('wallet_id'),
            route_key=destinations.get('route_key'),
            metadata={'intent_id': intent['intent_id']},
        )
        if not payment:
            await self._notify_admin_review(channel, intent, "I could not create the final payout request.")
            return None

        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {
                'status': 'awaiting_admin_approval',
                'final_payment_id': payment.get('payment_id'),
            },
            int(intent['guild_id']),
        )
        if not updated_intent:
            await self._notify_admin_review(
                channel,
                intent,
                "The final payout was created, but I could not update the intent row.",
                fail_intent=False,
            )
            return None

        payment_ui_cog = self._get_payment_ui_cog()
        if payment_ui_cog and hasattr(payment_ui_cog, '_send_admin_approval_dm'):
            await payment_ui_cog._send_admin_approval_dm(payment)
        else:
            logger.warning(
                "[AdminChat] PaymentUICog admin approval DM handler unavailable for intent %s",
                intent.get('intent_id'),
            )

        return {'intent': updated_intent, 'payment': payment}

    async def _gate_fresh_intent_atomic(
        self,
        channel,
        guild_id: int,
        recipient_user_id: int,
        amount_sol: float,
        source_channel_id: int,
        wallet_record: Dict,
        admin_user_id: int | None,
        reason: str | None,
        producer_ref: str,
    ):
        """Create a real-payment intent atomically for the verified-wallet fast path."""
        existing = self.db_handler.get_active_intent_for_recipient(guild_id, source_channel_id, recipient_user_id)
        if existing:
            return {'duplicate': True, 'intent': existing}
        if not self.payment_service:
            logger.error("[AdminChat] Payment service unavailable for verified fast-path initiate")
            return None
        if not wallet_record or not wallet_record.get('wallet_address'):
            logger.error("[AdminChat] Wallet record missing for verified fast-path initiate")
            return None

        intent_id = str(uuid4())
        destinations = self._resolve_payment_destinations(channel)
        provider = self._resolve_admin_payment_provider(channel)
        payment = await self.payment_service.request_payment(
            producer='admin_chat',
            producer_ref=str(producer_ref),
            guild_id=int(guild_id),
            recipient_wallet=wallet_record['wallet_address'],
            chain='solana',
            provider=provider,
            is_test=False,
            amount_token=float(amount_sol),
            confirm_channel_id=destinations['confirm_channel_id'],
            confirm_thread_id=destinations['confirm_thread_id'],
            notify_channel_id=destinations['notify_channel_id'],
            notify_thread_id=destinations['notify_thread_id'],
            recipient_discord_id=int(recipient_user_id),
            wallet_id=wallet_record.get('wallet_id'),
            route_key=destinations.get('route_key'),
            metadata={'intent_id': intent_id},
        )
        if not payment:
            return None

        intent = self.db_handler.create_admin_payment_intent(
            {
                'intent_id': intent_id,
                'channel_id': source_channel_id,
                'admin_user_id': admin_user_id,
                'recipient_user_id': recipient_user_id,
                'wallet_id': wallet_record.get('wallet_id'),
                'requested_amount_sol': amount_sol,
                'producer_ref': producer_ref,
                'reason': reason,
                'status': 'awaiting_admin_approval',
                'final_payment_id': payment.get('payment_id'),
            },
            guild_id=int(guild_id),
        )
        if not intent:
            logger.error(
                "[AdminChat] Failed to create verified fast-path intent for payment %s; orphan sweep will recover",
                payment.get('payment_id'),
            )
            try:
                await channel.send(
                    f"{self._admin_mention} review needed: I created payment `{payment.get('payment_id')}` "
                    "but could not persist the linked intent row."
                )
            except Exception:
                logger.warning(
                    "[AdminChat] Failed to send fast-path intent creation failure notice for payment %s",
                    payment.get('payment_id'),
                )
            return None

        payment_ui_cog = self._get_payment_ui_cog()
        if payment_ui_cog and hasattr(payment_ui_cog, '_send_admin_approval_dm'):
            await payment_ui_cog._send_admin_approval_dm(payment)
        else:
            logger.warning(
                "[AdminChat] PaymentUICog admin approval DM handler unavailable for new intent %s",
                intent_id,
            )

        return {'intent': intent, 'payment': payment}

    async def _handle_test_receipt_positive(self, message: discord.Message, intent: Dict):
        wallet_id = intent.get('wallet_id')
        wallet_record = self.db_handler.get_wallet_by_id(wallet_id, guild_id=int(intent['guild_id'])) if wallet_id else None
        if not wallet_record:
            await self._notify_admin_review(
                message.channel,
                intent,
                "The wallet record is missing, so I could not finish verification.",
            )
            return
        if not self.db_handler.mark_wallet_verified(wallet_record['wallet_id'], guild_id=int(intent['guild_id'])):
            await self._notify_admin_review(
                message.channel,
                intent,
                "The wallet test was acknowledged, but I could not mark the wallet as verified.",
            )
            return
        gated = await self._gate_existing_intent(
            message.channel,
            intent,
            wallet_record,
            float(intent.get('requested_amount_sol') or 0),
        )
        if not gated:
            return
        await self._set_intent_status_message(
            gated['intent'],
            message.channel,
            "Confirmation received. Awaiting admin approval for the final payout.",
        )

    async def _handle_test_receipt_negative(self, message: discord.Message, intent: Dict):
        test_payment = None
        if intent.get('test_payment_id'):
            test_payment = self.db_handler.get_payment_request(
                intent['test_payment_id'],
                guild_id=int(intent['guild_id']),
            )

        wallet_text = (
            str((test_payment or {}).get('recipient_wallet') or 'unknown')
        )
        tx_signature = str((test_payment or {}).get('tx_signature') or 'unknown')
        escalation_reason = (
            "Recipient sent multiple ambiguous receipt replies."
            if int(intent.get('ambiguous_reply_count') or 0) >= 2
            else "Recipient reported the test payment was not received."
        )
        detail = (
            f"{self._admin_mention} manual review needed for intent `{intent.get('intent_id')}` "
            f"for <@{intent.get('recipient_user_id')}>. {escalation_reason} "
            f"Test payment: `{intent.get('test_payment_id') or 'unknown'}`. "
            f"Tx signature: `{tx_signature}`. Wallet: `{wallet_text}`."
        )
        await message.channel.send(
            f"Hey <@{intent.get('recipient_user_id')}> - I've flagged this for admin review. "
            f"Please wait for {self._admin_mention} to follow up."
        )
        await message.channel.send(detail)
        self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'status': 'manual_review'},
            int(intent['guild_id']),
        )
        await self._notify_intent_admin(
            intent,
            (
                f"Manual review needed for intent `{intent.get('intent_id')}`.\n"
                f"Recipient: <@{intent.get('recipient_user_id')}>\n"
                f"Reason: {escalation_reason}\n"
                f"Test payment: `{intent.get('test_payment_id') or 'unknown'}`\n"
                f"Tx signature: `{tx_signature}`\n"
                f"Wallet: `{wallet_text}`"
            ),
        )

    async def _handle_test_receipt_ambiguous(
        self,
        message: discord.Message,
        intent: Dict,
        *,
        clarification_reply: Optional[str] = None,
    ):
        updated_intent = self.db_handler.increment_intent_ambiguous_reply_count(
            intent['intent_id'],
            int(intent['guild_id']),
        )
        if not updated_intent:
            logger.warning(
                "[AdminChat] Failed to increment ambiguous reply count for intent %s",
                intent.get('intent_id'),
            )
            return
        reply_count = int(updated_intent.get('ambiguous_reply_count') or 0)
        if reply_count < 2:
            recipient_user_id = updated_intent.get('recipient_user_id') or intent.get('recipient_user_id')
            if clarification_reply:
                text = f"<@{recipient_user_id}> {clarification_reply}"
            else:
                text = (
                    f"<@{recipient_user_id}> I didn't quite understand - please reply with "
                    "**confirmed** or **yes** once you see the test SOL in your wallet, or "
                    "**not received** if you don't see it."
                )
            await message.channel.send(text)
            return
        if reply_count >= 2:
            await self._handle_test_receipt_negative(message, updated_intent)

    async def _handle_confirmation_received(self, message: discord.Message, intent: Dict):
        """Persist a free-text final payout confirmation."""
        if not self.payment_service or not intent.get('final_payment_id'):
            await self._notify_admin_review(message.channel, intent, "The final payment record is unavailable, so I can't accept this confirmation.", resolved_by_message_id=message.id)
            return

        confirmed = self.payment_service.confirm_payment(
            intent['final_payment_id'],
            guild_id=int(intent['guild_id']),
            actor=PaymentActor(PaymentActorKind.RECIPIENT_MESSAGE, message.author.id),
        )
        if not confirmed:
            await self._notify_admin_review(message.channel, intent, "The final payout confirmation could not be applied.", resolved_by_message_id=message.id)
            return

        updated_intent = self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'status': 'confirmed', 'resolved_by_message_id': message.id},
            intent['guild_id'],
        )
        if not updated_intent:
            await self._notify_admin_review(message.channel, intent, "The intent row could not be updated after confirmation.", resolved_by_message_id=message.id)
            return
        await message.channel.send(f"<@{intent['recipient_user_id']}> confirmation received. The payout has been queued.")

    async def handle_payment_result(self, payment: Dict):
        """Receive terminal payment outcomes from the shared payment cog."""
        if str(payment.get('producer') or '').strip().lower() != 'admin_chat':
            return

        metadata = payment.get('metadata') or {}
        intent_id = str(metadata.get('intent_id') or '').strip()
        guild_id = payment.get('guild_id')
        if not intent_id or guild_id is None:
            logger.warning("[AdminChat] Ignoring payment result with missing intent_id/guild_id: %s", payment.get('payment_id'))
            return
        guild_id = int(guild_id)

        intent = self.db_handler.get_admin_payment_intent(intent_id, guild_id)
        if not intent:
            logger.warning("[AdminChat] Missing intent %s for payment %s", intent_id, payment.get('payment_id'))
            return

        channel = await self._fetch_intent_channel(int(intent['channel_id']))
        status = str(payment.get('status') or '').strip().lower()

        if payment.get('is_test'):
            if status != 'confirmed':
                updated = self.db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)
                if channel:
                    await self._set_intent_status_message(
                        intent,
                        channel,
                        f"Wallet verification ended in `{status}`. An admin will review it manually.",
                    )
                    if not updated:
                        await self._notify_admin_review(
                            channel,
                            intent,
                            "The wallet verification payment failed, and I could not update the intent row.",
                            fail_intent=False,
                        )
                return

            tx_signature = payment.get('tx_signature')
            explorer_link = self._explorer_url(tx_signature) if tx_signature else None
            explorer_text = f"\nExplorer: {explorer_link}" if explorer_link else ""
            await self._notify_intent_admin(
                intent,
                (
                    f"Test payment confirmed for <@{intent['recipient_user_id']}>.\n"
                    f"Intent: `{intent_id}`\n"
                    f"Amount: {float(payment.get('amount_token') or 0):.4f} SOL"
                    f"{explorer_text}"
                ),
            )

            status_message = None
            if channel:
                status_message = await self._set_intent_status_message(
                    intent,
                    channel,
                    (
                        f"<@{intent['recipient_user_id']}> Test payment confirmed. "
                        "Please reply here with `confirmed` once you see it in your wallet."
                    ),
                )

            payload = {
                'status': 'awaiting_test_receipt_confirmation',
                'last_scanned_message_id': None,
            }
            if intent.get('status_message_id') is not None:
                payload['receipt_prompt_message_id'] = intent['status_message_id']
            updated = self.db_handler.update_admin_payment_intent(intent_id, payload, guild_id)
            if updated:
                intent.update(updated)
            elif status_message is not None:
                intent['receipt_prompt_message_id'] = intent.get('status_message_id')
            if not updated and channel:
                await self._notify_admin_review(channel, intent, "The wallet test was confirmed, but I could not update the receipt-confirmation state.")
            return

        if status == 'confirmed':
            status_message = None
            if channel:
                status_message = await self._set_intent_status_message(
                    intent,
                    channel,
                    "Payout sending…",
                )
            updated = self.db_handler.update_admin_payment_intent(intent_id, {'status': 'completed'}, guild_id)
            if updated:
                intent.update(updated)
            amount = float(payment.get('amount_token') or intent.get('requested_amount_sol') or 0)
            tx_signature = payment.get('tx_signature')
            explorer_link = self._explorer_url(tx_signature) if tx_signature else None
            if channel and status_message is not None:
                reply_content = (
                    f"<@{intent['recipient_user_id']}> Payment sent — {amount:.4f} SOL. "
                    f"{explorer_link or 'Transaction signature unavailable'}"
                )
                reply = getattr(status_message, 'reply', None)
                if callable(reply):
                    await reply(
                        reply_content,
                        mention_author=True,
                        suppress_embeds=True,
                    )
                else:
                    await channel.send(reply_content, suppress_embeds=True)
            await self._notify_intent_admin(
                intent,
                (
                    f"Final payment confirmed for <@{intent['recipient_user_id']}>.\n"
                    f"Intent: `{intent_id}`\n"
                    f"Amount: {amount:.4f} SOL\n"
                    f"Explorer: {explorer_link or 'unavailable'}"
                ),
            )
            if not updated and channel:
                await self._notify_admin_review(channel, intent, "The payout completed, but I could not mark the intent as completed.", fail_intent=False)
            return

        updated = self.db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)
        if channel:
            await self._set_intent_status_message(
                intent,
                channel,
                f"Payout ended in `{status}`. An admin will review it manually.",
            )
            if not updated:
                await self._notify_admin_review(
                    channel,
                    intent,
                    "The payout failed, and I could not update the intent row.",
                    fail_intent=False,
                )

    async def cog_load(self):
        if not self._sweep_stale_test_receipts.is_running():
            self._sweep_stale_test_receipts.start()
        if self._bot_is_ready():
            await self._ensure_startup_reconciled()

    def cog_unload(self):
        if self._sweep_stale_test_receipts.is_running():
            self._sweep_stale_test_receipts.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_startup_reconciled()

    @tasks.loop(minutes=15)
    async def _sweep_stale_test_receipts(self):
        try:
            if not self._bot_is_ready():
                return

            cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            intents = self.db_handler.list_stale_test_receipt_intents(cutoff_iso)
            for intent in intents:
                try:
                    updated = self.db_handler.update_admin_payment_intent(
                        intent['intent_id'],
                        {'status': 'manual_review'},
                        int(intent['guild_id']),
                    )
                    if not updated:
                        continue

                    test_payment = None
                    if intent.get('test_payment_id'):
                        test_payment = self.db_handler.get_payment_request(
                            intent['test_payment_id'],
                            guild_id=int(intent['guild_id']),
                        )

                    await self._notify_intent_admin(
                        intent,
                        (
                            "Stale test receipt confirmation requires manual review.\n"
                            f"Intent: `{intent.get('intent_id')}`\n"
                            f"Recipient: <@{intent.get('recipient_user_id')}>\n"
                            f"Wallet: {test_payment.get('recipient_wallet') if test_payment else 'unavailable'}\n"
                            f"Tx signature: {test_payment.get('tx_signature') if test_payment else 'unavailable'}"
                        ),
                    )
                except Exception as intent_exc:
                    logger.error(
                        "[AdminChat] Error processing stale test receipt intent %s: %s",
                        intent.get('intent_id'),
                        intent_exc,
                        exc_info=True,
                    )
        except Exception as e:
            logger.error(f"[AdminChat] Stale test receipt sweep failed: {e}", exc_info=True)

    def _bot_is_ready(self) -> bool:
        checker = getattr(self.bot, 'is_ready', None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    async def _ensure_startup_reconciled(self):
        if self._startup_reconciled:
            return
        self._startup_reconciled = True
        try:
            await self._reconcile_active_intents()
        except Exception as e:
            logger.error(f"[AdminChat] Startup reconciliation failed: {e}", exc_info=True)

    def _get_reconciliation_guild_ids(self) -> list[int]:
        server_config = getattr(self.db_handler, 'server_config', None)
        if server_config:
            return [
                int(server['guild_id'])
                for server in server_config.get_enabled_servers(require_write=True)
                if server.get('guild_id') is not None
            ]
        return [guild.id for guild in getattr(self.bot, 'guilds', [])]

    def _is_terminal_payment(self, payment: Dict | None) -> bool:
        return bool(payment and payment.get('status') in {'confirmed', 'failed', 'manual_hold', 'cancelled'})

    async def _reconcile_payment_status(self, intent: Dict, payment_key: str):
        payment_id = intent.get(payment_key)
        if not payment_id:
            return
        payment = self.db_handler.get_payment_request(payment_id, guild_id=int(intent['guild_id']))
        if self._is_terminal_payment(payment):
            await self.handle_payment_result(payment)

    async def _reconcile_intent_history(self, intent: Dict):
        channel = await self._fetch_intent_channel(int(intent['channel_id']))
        if channel is None:
            logger.warning("[AdminChat] Reconciliation skipped missing channel %s for intent %s", intent.get('channel_id'), intent.get('intent_id'))
            return

        cursor_id = (
            intent.get('last_scanned_message_id')
            or intent.get('receipt_prompt_message_id')
            or intent.get('status_message_id')
            or intent.get('prompt_message_id')
        )
        messages = []
        try:
            if cursor_id:
                async for message in channel.history(
                    limit=200,
                    after=discord.Object(id=int(cursor_id)),
                    oldest_first=True,
                ):
                    messages.append(message)
            else:
                async for message in channel.history(limit=200):
                    messages.append(message)
                messages.reverse()
        except Exception as e:
            logger.warning("[AdminChat] Reconciliation history scan failed for intent %s: %s", intent.get('intent_id'), e)
            return

        if not messages:
            return

        last_seen_message_id = None
        original_status = str(intent.get('status') or '').strip().lower()
        recipient_user_id = int(intent['recipient_user_id'])
        for message in messages:
            last_seen_message_id = message.id
            if getattr(message.author, 'id', None) != recipient_user_id:
                continue
            refreshed_intent = self.db_handler.get_admin_payment_intent(intent['intent_id'], int(intent['guild_id']))
            if refreshed_intent is None:
                break
            await self._handle_pending_recipient_message(message, refreshed_intent)
            refreshed = self.db_handler.get_admin_payment_intent(intent['intent_id'], int(intent['guild_id']))
            refreshed_status = str((refreshed or {}).get('status') or '').strip().lower()
            if refreshed is None or refreshed_status != original_status:
                break

        self.db_handler.update_admin_payment_intent(
            intent['intent_id'],
            {'last_scanned_message_id': last_seen_message_id},
            int(intent['guild_id']),
        )
        if len(messages) == 200:
            logger.warning("[AdminChat] Reconciliation capped at 200 messages for intent %s", intent.get('intent_id'))

    async def _reconcile_active_intents(self):
        for guild_id in self._get_reconciliation_guild_ids():
            intents = self.db_handler.list_active_intents(guild_id)
            deferred_counts = {
                'awaiting_admin_approval': 0,
                'awaiting_admin_init': 0,
                'manual_review': 0,
            }
            for intent in intents:
                status = str(intent.get('status') or '').strip().lower()
                if status in {'awaiting_wallet', 'awaiting_test_receipt_confirmation'}:
                    await self._reconcile_intent_history(intent)
                    continue
                if status == 'awaiting_confirmation':
                    await self._reconcile_payment_status(intent, 'final_payment_id')
                    continue
                if status == 'awaiting_test':
                    await self._reconcile_payment_status(intent, 'test_payment_id')
                    continue
                if status == 'confirmed':
                    await self._reconcile_payment_status(intent, 'final_payment_id')
                    continue
                if status in deferred_counts:
                    deferred_counts[status] += 1
            if any(deferred_counts.values()):
                logger.info(
                    "[AdminChat] Active intent backlog for guild %s: awaiting_admin_approval=%s awaiting_admin_init=%s manual_review=%s",
                    guild_id,
                    deferred_counts['awaiting_admin_approval'],
                    deferred_counts['awaiting_admin_init'],
                    deferred_counts['manual_review'],
                )

    def _get_supabase(self):
        """Get the shared Supabase client if available."""
        storage_handler = getattr(self.db_handler, 'storage_handler', None)
        return getattr(storage_handler, 'supabase_client', None)
    
    def _ensure_agent(self):
        """Lazily initialize the agent (to avoid issues during bot startup)."""
        if self.agent is None:
            try:
                self.agent = AdminChatAgent(
                    bot=self.bot,
                    db_handler=self.db_handler,
                    sharer=self.sharer
                )
                logger.info("[AdminChat] Agent initialized")
            except Exception as e:
                logger.error(f"[AdminChat] Failed to initialize agent: {e}", exc_info=True)
                raise
    
    def _is_admin(self, user_id: int) -> bool:
        """Check if a user may use the privileged admin chat route."""
        return user_id in self._allowed_admin_chat_user_ids

    async def _resolve_context_guild_id(self, user_id: int, guild_hint: int | None = None) -> int | None:
        """Resolve a trusted guild context for the requester."""
        if guild_hint is not None:
            server_config = getattr(self.db_handler, 'server_config', None)
            if self.bot.get_guild(guild_hint) is None:
                return None
            if server_config and not server_config.is_guild_enabled(guild_hint):
                return None
            return guild_hint

        now = time.monotonic()
        cached = self._guild_context_cache.get(user_id)
        if cached and now - cached[0] < self._ACCESS_CACHE_TTL_SECONDS:
            return cached[1]

        client = self._get_supabase()
        if client is None:
            return None

        result = await asyncio.to_thread(
            client.table('guild_members')
            .select('guild_id')
            .eq('member_id', user_id)
            .execute
        )
        server_config = getattr(self.db_handler, 'server_config', None)
        guild_ids = sorted({
            int(row['guild_id'])
            for row in (result.data or [])
            if row.get('guild_id') is not None
            and self.bot.get_guild(int(row['guild_id'])) is not None
            and (server_config is None or server_config.is_guild_enabled(int(row['guild_id'])))
        })
        if not guild_ids:
            resolved = None
        else:
            default_guild_id = server_config.get_default_guild_id(require_write=False) if server_config else None
            resolved = default_guild_id if default_guild_id in guild_ids else guild_ids[0]
            if len(guild_ids) > 1:
                logger.info(f"[AdminChat] Resolved DM guild for {user_id} to {resolved} from {guild_ids}")

        self._guild_context_cache[user_id] = (now, resolved)
        return resolved

    def _is_rate_limited(self, user_id: int) -> bool:
        """Apply a simple sliding-window limit for non-admin users."""
        now = time.monotonic()
        bucket = self._rate_limits.setdefault(user_id, deque())
        while bucket and now - bucket[0] > self._RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= self._RATE_LIMIT_MAX_MESSAGES:
            return True
        bucket.append(now)
        return False

    def _strip_mention(self, content: str, guild: discord.Guild = None) -> str:
        """Remove the bot @mention and bot role @mention from message content."""
        if self.bot.user:
            content = content.replace(f'<@{self.bot.user.id}>', '').replace(f'<@!{self.bot.user.id}>', '')
            if guild:
                bot_member = guild.get_member(self.bot.user.id)
                if bot_member:
                    for role in bot_member.roles:
                        if role.is_bot_managed():
                            content = content.replace(f'<@&{role.id}>', '')
        return content.strip()

    _ABORT_PHRASES = {'stop', 'abort', 'cancel', 'halt', 'nevermind', 'never mind', 'quit', 'enough'}

    def _is_abort(self, content: str) -> bool:
        """Check if the message is an abort request."""
        normalised = content.strip().lower().rstrip('!.')
        return normalised in self._ABORT_PHRASES

    async def _handle_pending_recipient_message(self, message: discord.Message, intent: Dict) -> bool:
        """Deterministic state-machine router for pending admin payment recipients."""
        if message.author.bot or message.guild is None:
            return False

        intent_id = str(intent.get('intent_id') or '')
        if not intent_id:
            return False
        if intent_id in self._processing_intents:
            return True

        self._processing_intents.add(intent_id)
        try:
            content = (message.content or '').strip()
            status = str(intent.get('status') or '').strip().lower()

            if status == 'awaiting_wallet':
                wallet_address = self._parse_wallet_from_text(content)
                if wallet_address:
                    await self._handle_wallet_received(message, intent, wallet_address)
                return True

            if status == 'awaiting_test_receipt_confirmation':
                classification, clarification_reply = await self._classify_confirmation(content)
                if classification == 'positive':
                    await self._handle_test_receipt_positive(message, intent)
                elif classification == 'negative':
                    await self._handle_test_receipt_negative(message, intent)
                else:
                    await self._handle_test_receipt_ambiguous(
                        message,
                        intent,
                        clarification_reply=clarification_reply,
                    )
                return True

            if status in {
                'awaiting_test',
                'awaiting_admin_init',
                'awaiting_admin_approval',
                'manual_review',
                'awaiting_confirmation',
                'confirmed',
            }:
                return True

            return False
        finally:
            self._processing_intents.discard(intent_id)

    async def _handle_admin_message(self, message: discord.Message):
        """Process admin messages — DMs always, guild channels only when the bot is @mentioned."""
        if message.author.bot:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        if not is_dm and not self.bot.user:
            return

        # Step 6 (T8): Reverse-lookup for live-update feedback replies BEFORE the
        # @mention gate.  A reply whose parent resolves to a `topics` row (the
        # table backing the live-updates channel) is feedback and bypasses the
        # @mention requirement (SD1).
        is_live_update_feedback = False
        resolved_topic = None
        parent_id = None

        if not is_dm and message.reference:
            parent_id = message.reference.message_id
            if parent_id and message.guild:
                try:
                    resolved_topic = await asyncio.to_thread(
                        self.db_handler.get_topic_by_discord_message_id,
                        parent_id,
                        message.guild.id,
                        self._live_environment(),
                    )
                    is_live_update_feedback = resolved_topic is not None
                except Exception:
                    logger.exception(
                        "[AdminChat] live-update topic reverse-lookup failed for "
                        "parent_id=%s",
                        parent_id,
                    )

        if not is_dm and not is_live_update_feedback and self.bot.user.id not in [m.id for m in message.mentions]:
            return
        content = message.content if is_dm else self._strip_mention(message.content, message.guild)
        if not content:
            return

        user_id = message.author.id
        resolved_guild_id = message.guild.id if message.guild else None
        source = "DM" if is_dm else f"#{getattr(message.channel, 'name', 'unknown')}"
        logger.info(f"[AdminChat] Received from admin in {source}: {content[:50]}...")

        # Abort is terminal on every replica: request the local abort (if the
        # agent is busy in this process) and use the claim only to gate the
        # acknowledgement. An idle replica must never claim an abort message and
        # run it through agent.chat as a normal turn.
        if self._is_abort(content):
            if self._busy.get(user_id) and self.agent:
                self.agent.request_abort(user_id)
                logger.info(f"[AdminChat] Abort requested by user {user_id}")
            if self._claim_admin_chat_message(message):
                await message.add_reaction("\u23f9\ufe0f")
            return

        if self._busy.get(user_id):
            # Defer the claim: the message is queued and will be re-entered via
            # on_message when the busy turn drains. If we claimed here, the replay
            # would fail the claim check ("already-claimed") and drop the message.
            self._pending_messages[user_id] = message
            return

        if not self._claim_admin_chat_message(message):
            logger.info(f"[AdminChat] Skipping already-claimed message {message.id}")
            return

        try:
            self._ensure_agent()

            channel_context = None
            if is_dm:
                ch = message.channel
                guild = self.bot.get_guild(resolved_guild_id) if resolved_guild_id else None
                channel_context = {
                    "source": "dm",
                    "guild_id": str(resolved_guild_id) if resolved_guild_id else None,
                    "guild_name": guild.name if guild else None,
                    "channel_id": str(ch.id),
                    "channel_name": "DM",
                }

                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    if isinstance(ref, discord.Message):
                        channel_context["replied_to"] = {
                            "message_id": str(ref.id),
                            "author": ref.author.display_name,
                            "content": _preview_text(ref.content or '', 500),
                        }
                        channel_context["replied_to_anchor_note"] = "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."

                try:
                    recent = []
                    async for msg in ch.history(limit=10):
                        if msg.id == message.id:
                            continue
                        content_preview = self._strip_fallback_reply_lines(msg.content or '')
                        if not content_preview:
                            continue
                        recent.append(f"[{msg.id}] {msg.author.display_name}: {_preview_text(content_preview, 150)}")
                    recent.reverse()
                    channel_context["recent_messages"] = recent
                except Exception:
                    pass
            else:
                ch = message.channel
                channel_context = {
                    "guild_id": str(resolved_guild_id),
                    "channel_id": str(ch.id),
                    "channel_name": getattr(ch, 'name', 'unknown'),
                }
                if isinstance(ch, discord.Thread) and ch.parent:
                    channel_context["is_thread"] = True
                    channel_context["parent_channel_id"] = str(ch.parent_id)
                    channel_context["parent_channel_name"] = ch.parent.name

                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    if isinstance(ref, discord.Message):
                        channel_context["replied_to"] = {
                            "message_id": str(ref.id),
                            "author": ref.author.display_name,
                            "content": _preview_text(ref.content or '', 500),
                        }
                        channel_context["replied_to_anchor_note"] = "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."

                try:
                    recent = []
                    async for msg in ch.history(limit=10):
                        if msg.id == message.id:
                            continue
                        content_preview = self._strip_fallback_reply_lines(msg.content or '')
                        if not content_preview:
                            continue
                        recent.append(f"[{msg.id}] {msg.author.display_name}: {_preview_text(content_preview, 150)}")
                    recent.reverse()
                    channel_context["recent_messages"] = recent
                except Exception:
                    pass

            # ── Step 7 (T9): Enrich channel_context with live-update-feedback fields ──
            # replied_to_message_id from message.reference.message_id (always present
            # when message.reference exists — independent of the cache-dependent
            # 'replied_to' block).
            if message.reference:
                channel_context["replied_to_message_id"] = str(message.reference.message_id)
                # Best-effort hydrate parent content when resolved is not cached
                if not is_dm and message.reference.resolved is None and parent_id is not None:
                    try:
                        parent_msg = await message.channel.fetch_message(parent_id)
                        if "replied_to" not in channel_context:
                            channel_context["replied_to"] = {
                                "message_id": str(parent_msg.id),
                                "author": parent_msg.author.display_name,
                                "content": _preview_text(parent_msg.content or '', 500),
                            }
                            channel_context["replied_to_anchor_note"] = (
                                "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."
                            )
                    except Exception:
                        pass

            channel_context["environment"] = self._live_environment()

            # Live-update-feedback specific enrichment (SD1/SD3):
            # Guidance is supplied unconditionally when is_live_update_feedback is True,
            # NOT gated on channel_id == live_channel_id (closes issue_hints-2).
            if is_live_update_feedback:
                channel_context["live_update_topic"] = resolved_topic
                sc = self.db_handler.server_config if self.db_handler else None
                override = (
                    sc.get_channel_agent_guidance(resolved_guild_id, message.channel.id)
                    if sc and resolved_guild_id is not None
                    else None
                )
                channel_context["channel_guidance"] = override or LIVE_UPDATE_FEEDBACK_GUIDANCE

            # ── Social-draft DM binding (T7) ──────────────────────────
            # Only runs on DM replies to a bot message that might be a draft
            # embed. Per-turn only — never persisted into _conversations.
            if is_dm and message.reference is not None:
                parent_id = int(message.reference.message_id)
                row = self.db_handler.get_social_run_by_review_message_id(
                    parent_id, environment=self._live_environment()
                )
                # Enforce expiry: an expired run's DM reply is treated as an
                # orphan — no review context injected, so the agent falls back
                # to list_pending_social_drafts and tells the admin the item
                # expired instead of acting on a stale review.
                if row is not None and row.get("expires_at"):
                    try:
                        ts = row["expires_at"]
                        if isinstance(ts, str):
                            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts <= datetime.now(timezone.utc):
                            row = None
                    except (TypeError, ValueError):
                        pass  # malformed expiry — do not drop the binding
                # Discarded runs (approval_state='expired') must never bind —
                # their topic was discarded and the review is void.
                if row is not None and row.get("approval_state") == "expired":
                    row = None
                # topic_summary_data is stored under publish_units.
                topic_title = (
                    row.get("topic_summary_data")
                    or row.get("publish_units")
                    or {}
                ).get("title", "") if row else ""
                if (
                    row is not None
                    and row.get("terminal_status") == "failed"
                ):
                    # Failed-publish context — the admin can retry, edit, or
                    # discard. Retry goes through publish_social_draft once the
                    # cause (e.g. missing route) is fixed.
                    channel_context["social_failed_run"] = {
                        "run_id": row.get("run_id"),
                        "draft_text": row.get("draft_text"),
                        "revision": row.get("revision", 0),
                        "approval_state": row.get("approval_state", "pending"),
                        "topic_title": topic_title,
                        "failure": (
                            (row.get("publication_outcome") or {}).get("error")
                            or "publish failed (see run logs)"
                        ),
                        "expires_at": row.get("expires_at"),
                    }
                    channel_context["channel_guidance"] = SOCIAL_FAILED_REVIEW_GUIDANCE
                elif (
                    row is not None
                    and row.get("terminal_status") == "proposed"
                ):
                    # Proposal-review context — the admin picks an idea to
                    # develop into a draft via develop_social_proposal.
                    channel_context["social_proposal_run"] = {
                        "run_id": row.get("run_id"),
                        "proposals": row.get("proposals") or [],
                        "topic_title": topic_title,
                        "expires_at": row.get("expires_at"),
                    }
                    channel_context["channel_guidance"] = SOCIAL_PROPOSAL_REVIEW_GUIDANCE
                elif (
                    row is not None
                    and row.get("terminal_status") not in (
                        "published", "needs_review",
                    )
                ):
                    channel_context["social_draft_run"] = {
                        "run_id": row.get("run_id"),
                        "draft_text": row.get("draft_text"),
                        "revision": row.get("revision", 0),
                        "approval_state": row.get("approval_state", "pending"),
                        "approved_revision": row.get("approved_revision"),
                        "topic_title": topic_title,
                        "expires_at": row.get("expires_at"),
                    }
                    # Social-draft guidance overrides any other channel guidance
                    # inside DMs (wins over feedback guidance when hypothetically both match).
                    channel_context["channel_guidance"] = SOCIAL_DRAFT_REVIEW_GUIDANCE
                # Orphan-reply fallback: parent does not resolve → do NOT inject
                # social_draft_run / social_proposal_run. The agent will see
                # no draft context.

            self._busy[user_id] = True
            try:
                result = await self.agent.chat(
                    user_id=user_id,
                    user_message=content,
                    channel_context=channel_context,
                    channel=message.channel,
                    requester_id=None,
                )
            finally:
                self._busy[user_id] = False

            if result.replies is None and not is_live_update_feedback:
                logger.info("[AdminChat] Turn ended without reply (silent action)")
                return

            total_chars = 0
            messages_sent = 0
            reply_ref = message if not is_dm else None

            async def _send_with_retry(channel, content_to_send: str, reference=None):
                backoffs = (0.5, 1.5)
                for attempt in range(len(backoffs) + 1):
                    try:
                        return await channel.send(content_to_send, reference=reference)
                    except discord.HTTPException as exc:
                        if exc.status < 500 or attempt == len(backoffs):
                            raise
                        await asyncio.sleep(backoffs[attempt])

            # Live-update-feedback turns are silent actions (log + ✅ + delete the
            # reply). Suppress any conversational reply the agent emits so the live
            # channel stays clean even if the model ignores the silent-action
            # guidance. The raw reply text is still used below for the fallback row.
            if result.replies and is_live_update_feedback:
                logger.info(
                    f"[AdminChat] Suppressing {len(result.replies)} reply(ies) in "
                    f"live-update-feedback mode (silent action)"
                )

            if result.replies and not is_live_update_feedback:
                for response in result.replies:
                    response = self._strip_fallback_reply_lines(response)
                    if not response or not response.strip():
                        continue

                    parts = response.split('\n---SPLIT---\n')
                    for part in parts:
                        part = part.strip()
                        if not part:
                            continue

                        total_chars += len(part)
                        if len(part) <= 2000:
                            await _send_with_retry(message.channel, part, reference=reply_ref)
                            messages_sent += 1
                        else:
                            chunks = [part[i:i + 1990] for i in range(0, len(part), 1990)]
                            for chunk in chunks:
                                if chunk.strip():
                                    await _send_with_retry(message.channel, chunk, reference=reply_ref)
                                    messages_sent += 1
                        reply_ref = None

                logger.info(f"[AdminChat] Sent {messages_sent} message(s) ({total_chars} chars total)")

            # ── Step 8 (T10): Post-turn feedback processing ──
            if is_live_update_feedback and resolved_topic and parent_id:
                topic_id = resolved_topic.get('topic_id')
                replied_to_message_id = int(parent_id)
                environment = self._live_environment()
                guild_id = resolved_guild_id

                # 8.2: Authoritative delete-gate (SD1/SD2) — re-query the table,
                # never trust an in-turn flag.
                feedback_row = await asyncio.to_thread(
                    self.db_handler.get_live_update_feedback_for,
                    replied_to_message_id, environment, None,
                    topic_id=topic_id,
                )

                # 8.3: Fallback — if no row exists, store one with raw reply text
                # and disposition='fallback', then re-read to confirm.
                if not feedback_row:
                    raw_reply = result.replies[0] if result.replies else ""
                    await asyncio.to_thread(
                        self.db_handler.store_live_update_feedback,
                        {
                            'topic_id': topic_id,
                            'guild_id': guild_id,
                            'environment': environment,
                            'admin_user_id': user_id,
                            'feedback_text': raw_reply,
                            'replied_to_message_id': replied_to_message_id,
                            'disposition': 'fallback',
                        },
                        environment,
                    )
                    feedback_row = await asyncio.to_thread(
                        self.db_handler.get_live_update_feedback_for,
                        replied_to_message_id, environment, 'fallback',
                        topic_id=topic_id,
                    )

                # 8.4: Enforced editorial audit (SD2) — inspect result.actions
                # for executed edit_message / delete_message calls.
                discord_message_ids = resolved_topic.get('discord_message_ids') or []
                discord_message_ids_str = {str(mid) for mid in discord_message_ids}

                for action in result.actions:
                    tool_name = action.get('tool', '')
                    if tool_name == 'edit_message':
                        candidate_ids = set()
                        inp = action.get('input', {})
                        res = action.get('result', {})
                        if inp.get('message_id'):
                            candidate_ids.add(str(inp['message_id']))
                        if res.get('message_id'):
                            candidate_ids.add(str(res['message_id']))
                        if candidate_ids & discord_message_ids_str:
                            # NOTE: topics has no 'edited' state (valid states are
                            # posted/watching/discarded/deleted) — do NOT mutate
                            # topics.state on edit; only record the audit row.
                            audit_row = await asyncio.to_thread(
                                self.db_handler.get_live_update_feedback_for,
                                replied_to_message_id, environment, 'edited',
                                topic_id=topic_id,
                            )
                            if not audit_row:
                                await asyncio.to_thread(
                                    self.db_handler.store_live_update_feedback,
                                    {
                                        'topic_id': topic_id,
                                        'guild_id': guild_id,
                                        'environment': environment,
                                        'admin_user_id': user_id,
                                        'feedback_text': '',
                                        'replied_to_message_id': replied_to_message_id,
                                        'disposition': 'edited',
                                    },
                                    environment,
                                )
                            # Also refresh feedback_row so ✅ / delete gate passes
                            if not feedback_row:
                                feedback_row = audit_row

                    elif tool_name == 'delete_message':
                        candidate_ids = set()
                        inp = action.get('input', {})
                        res = action.get('result', {})
                        if inp.get('message_id'):
                            candidate_ids.add(str(inp['message_id']))
                        for mid in (inp.get('message_ids') or []):
                            candidate_ids.add(str(mid))
                        for mid in (res.get('deleted_ids') or []):
                            candidate_ids.add(str(mid))
                        if candidate_ids & discord_message_ids_str:
                            # Soft-delete the topic (NEVER hard-delete): set
                            # topics.state='deleted'.  The feedback migration
                            # extends topics_state_check to permit 'deleted'.
                            await asyncio.to_thread(
                                self.db_handler.update_topic,
                                topic_id, {'state': 'deleted'}, guild_id, environment,
                            )
                            try:
                                open_runs = await asyncio.to_thread(self.db_handler.list_open_social_runs, environment, 50, topic_id)
                                for run_row in (open_runs or []):
                                    await asyncio.to_thread(lambda r=run_row: self.db_handler.update_live_update_social_run(r['run_id'], environment, approval_state='expired'))
                            except Exception:
                                logger.exception('social-draft invalidation failed: topic=%s reason=admin_delete', topic_id)
                            audit_row = await asyncio.to_thread(
                                self.db_handler.get_live_update_feedback_for,
                                replied_to_message_id, environment, 'deleted',
                                topic_id=topic_id,
                            )
                            if not audit_row:
                                await asyncio.to_thread(
                                    self.db_handler.store_live_update_feedback,
                                    {
                                        'topic_id': topic_id,
                                        'guild_id': guild_id,
                                        'environment': environment,
                                        'admin_user_id': user_id,
                                        'feedback_text': '',
                                        'replied_to_message_id': replied_to_message_id,
                                        'disposition': 'deleted',
                                    },
                                    environment,
                                )
                            if not feedback_row:
                                feedback_row = audit_row

                # 8.5: ✅ reaction + reply deletion — ONLY after a confirmed
                # feedback row exists (from 8.2, 8.3, or 8.4).
                if feedback_row:
                    try:
                        await message.add_reaction('\u2705')
                    except Exception:
                        pass
                    try:
                        await message.delete()
                    except Exception:
                        pass
        except Exception:
            logger.exception("[AdminChat] Error processing message")
            try:
                await message.channel.send("Sorry, something went wrong on my side. Try again in a moment.")
            except Exception:
                logger.exception("[AdminChat] Failed to send neutral error message")
        finally:
            # Drain the pending queue on every exit path — successful replies,
            # silent tool-only turns (replies=None early return), and exceptions.
            # Previously a tool-only turn returned before this drain, stranding
            # any message queued while it ran.
            pending = self._pending_messages.pop(user_id, None)
            if pending:
                logger.info(f"[AdminChat] Processing queued message from {user_id}")
                await self.on_message(pending)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle admin chat via identity routing only."""
        if message.author.bot:
            return

        if self._is_admin(message.author.id):
            await self._handle_admin_message(message)
            return

        if message.guild is None:
            return

        intent = self.db_handler.get_active_intent_for_recipient(
            message.guild.id,
            message.channel.id,
            message.author.id,
        )
        if intent:
            await self._handle_pending_recipient_message(message, intent)
            return
    
    @commands.command(name='adminchat_clear')
    @commands.is_owner()
    async def clear_history(self, ctx: commands.Context):
        """Clear the admin chat conversation history."""
        if self.agent:
            self.agent.clear_conversation(ctx.author.id)
            await ctx.send("Conversation history cleared.")
        else:
            await ctx.send("Agent not initialized.")


async def setup(bot: commands.Bot):
    """Setup function for loading the cog."""
    # These will be passed from main.py
    db_handler = getattr(bot, 'db_handler', None)
    sharer = getattr(bot, 'sharer', None)
    
    if db_handler is None or sharer is None:
        logger.error("[AdminChat] Cannot setup cog - db_handler or sharer not found on bot")
        return
    
    await bot.add_cog(AdminChatCog(bot, db_handler, sharer))
    logger.info("[AdminChat] Cog loaded")
