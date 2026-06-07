from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands, tasks

from src.common.redaction import redact_wallet as _redact_wallet
from src.common.discord_utils import safe_delete_messages, safe_send_message

from .payment_service import PaymentActor, PaymentActorKind, PaymentService

logger = logging.getLogger('DiscordBot')


class PaymentConfirmView(discord.ui.View):
    """Persistent per-payment confirmation control."""

    def __init__(self, payment_ui_cog: 'PaymentUICog', payment_id: str):
        super().__init__(timeout=None)
        self.payment_ui_cog = payment_ui_cog
        self.payment_id = payment_id

        confirm_button = discord.ui.Button(
            label="Confirm Payment",
            style=discord.ButtonStyle.success,
            custom_id=f"payment_confirm:{payment_id}",
        )
        confirm_button.callback = self._confirm_button_pressed
        self.add_item(confirm_button)

    async def _confirm_button_pressed(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing_payment = self.payment_ui_cog.db_handler.get_payment_request(
            self.payment_id,
            guild_id=interaction.guild_id,
        )
        if not existing_payment:
            await interaction.followup.send(
                "I couldn't find that payment request anymore.",
                ephemeral=True,
            )
            return

        expected_user_id = existing_payment.get('recipient_discord_id')
        if expected_user_id is None:
            await interaction.followup.send(
                "This payment has no designated recipient and cannot be confirmed via button.",
                ephemeral=True,
            )
            return
        if int(expected_user_id) != interaction.user.id:
            await interaction.followup.send(
                "Only the intended recipient can confirm this payment.",
                ephemeral=True,
            )
            return

        payment = self.payment_ui_cog.payment_service.confirm_payment(
            self.payment_id,
            guild_id=interaction.guild_id,
            actor=PaymentActor(PaymentActorKind.RECIPIENT_CLICK, interaction.user.id),
        )
        if not payment:
            await interaction.followup.send("I couldn't queue that payment.", ephemeral=True)
            return

        status = payment.get('status')
        if status == 'queued':
            self._disable_all_items()
            try:
                if interaction.message:
                    await interaction.message.edit(view=self)
            except Exception as exc:
                logger.warning(
                    "[PaymentUICog] Failed to disable confirmation view for %s: %s",
                    self.payment_id,
                    exc,
                )
            await interaction.followup.send(
                f"Queued payment `{self.payment_id}` for processing.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Payment `{self.payment_id}` is already `{status}`.",
            ephemeral=True,
        )

    def _disable_all_items(self):
        for child in self.children:
            if hasattr(child, 'disabled'):
                child.disabled = True


class AdminApprovalView(discord.ui.View):
    """Persistent per-payment admin approval control."""

    def __init__(self, payment_ui_cog: 'PaymentUICog', payment_id: str):
        super().__init__(timeout=None)
        self.payment_ui_cog = payment_ui_cog
        self.payment_id = payment_id

        confirm_button = discord.ui.Button(
            label="Approve Payment",
            style=discord.ButtonStyle.success,
            custom_id=f"payment_admin_approve:{payment_id}",
        )
        confirm_button.callback = self._confirm_button_pressed
        self.add_item(confirm_button)

    async def _confirm_button_pressed(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        admin_user_id = self.payment_ui_cog._get_admin_user_id()
        if admin_user_id is None or interaction.user.id != admin_user_id:
            await interaction.followup.send("admin-only", ephemeral=True)
            return

        payment = self.payment_ui_cog.payment_service.confirm_payment(
            self.payment_id,
            guild_id=interaction.guild_id,
            actor=PaymentActor(PaymentActorKind.ADMIN_DM, interaction.user.id),
        )
        if not payment:
            await interaction.followup.send("I couldn't queue that payment.", ephemeral=True)
            return

        status = payment.get('status')
        if status == 'queued':
            self._disable_all_items()
            try:
                if interaction.message:
                    await interaction.message.edit(content="✅ approved — queued for sending", view=self)
            except Exception as exc:
                logger.warning(
                    "[PaymentUICog] Failed to update admin approval DM for %s: %s",
                    self.payment_id,
                    exc,
                )
            await interaction.followup.send("payment confirmed, sending")
            await self.payment_ui_cog._post_admin_approval_thread_update(payment)
            await self.payment_ui_cog._cleanup_admin_intent_messages(payment)
            return

        await interaction.followup.send(
            f"Payment `{self.payment_id}` is already `{status}`.",
            ephemeral=True,
        )

    def _disable_all_items(self):
        for child in self.children:
            if hasattr(child, 'disabled'):
                child.disabled = True


class PaymentUICog(commands.Cog):
    """Confirmation UI and persistent view registration for payments."""

    def __init__(
        self,
        bot: commands.Bot,
        db_handler,
        payment_service: Optional[PaymentService] = None,
    ):
        self.bot = bot
        self.db_handler = db_handler
        self.payment_service = payment_service or getattr(bot, 'payment_service', None)
        self.bot.payment_service = self.payment_service
        self.bot.payment_ui_cog = self

    async def cog_load(self):
        await self._reconcile_admin_chat_orphans()
        await self._register_pending_confirmation_views()
        await self._register_pending_admin_approval_views()
        if not self._orphan_sweep_loop.is_running():
            self._orphan_sweep_loop.start()

    def cog_unload(self):
        if self._orphan_sweep_loop.is_running():
            self._orphan_sweep_loop.cancel()

    async def _reconcile_admin_chat_orphans(self):
        if not self.payment_service:
            return

        pending_lookup = getattr(self.payment_service, 'get_pending_confirmation_payments', None)
        if not callable(pending_lookup):
            return

        guild_ids = self._get_writable_guild_ids()
        pending = pending_lookup(guild_ids=guild_ids)
        for payment in pending:
            if payment.get('is_test') or str(payment.get('producer') or '').strip().lower() != 'admin_chat':
                continue

            payment_id = str(payment.get('payment_id') or '').strip()
            if not payment_id:
                continue

            intent = None
            if hasattr(self.db_handler, 'find_admin_chat_intent_by_payment_id'):
                try:
                    intent = self.db_handler.find_admin_chat_intent_by_payment_id(payment_id)
                except Exception as exc:
                    logger.error(
                        "[PaymentUICog] Failed to resolve admin intent for orphan sweep payment %s: %s",
                        payment_id,
                        exc,
                        exc_info=True,
                    )
                    continue

            if not intent:
                reason = 'admin_chat pending_confirmation payment missing linked intent'
                logger.error("[PaymentUICog] Cancelling orphan payment %s: %s", payment_id, reason)
                self.db_handler.cancel_payment(payment_id, guild_id=payment.get('guild_id'), reason=reason)
                continue

            status = str(intent.get('status') or '').strip().lower()
            if status in {'awaiting_admin_approval', 'awaiting_confirmation'}:
                continue

            if status in {'awaiting_test_receipt_confirmation', 'awaiting_admin_init'}:
                updated = self.db_handler.update_admin_payment_intent(
                    intent['intent_id'],
                    {
                        'status': 'awaiting_admin_approval',
                        'final_payment_id': payment_id,
                    },
                    int(intent['guild_id']),
                )
                if not updated:
                    logger.error(
                        "[PaymentUICog] Failed to repair admin intent %s for payment %s during orphan sweep",
                        intent.get('intent_id'),
                        payment_id,
                    )
                    continue
                await self._send_admin_approval_dm(payment)
                continue

            reason = f"admin_chat pending_confirmation payment linked to unexpected intent status `{status or 'unknown'}`"
            logger.error("[PaymentUICog] Cancelling anomalous payment %s: %s", payment_id, reason)
            self.db_handler.cancel_payment(payment_id, guild_id=payment.get('guild_id'), reason=reason)

        if not hasattr(self.db_handler, 'list_stale_awaiting_admin_init_intents'):
            return

        cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        try:
            stale_intents = self.db_handler.list_stale_awaiting_admin_init_intents(cutoff_iso)
        except Exception as exc:
            logger.error(
                "[PaymentUICog] Failed to list stale awaiting_admin_init intents before %s: %s",
                cutoff_iso,
                exc,
                exc_info=True,
            )
            return

        for intent in stale_intents:
            if intent.get('final_payment_id') is not None:
                continue
            logger.error(
                "[PaymentUICog] Cancelling stale awaiting_admin_init intent %s with no final payment_id",
                intent.get('intent_id'),
            )
            self.db_handler.update_admin_payment_intent(
                intent['intent_id'],
                {'status': 'cancelled'},
                int(intent['guild_id']),
            )

    @tasks.loop(minutes=5)
    async def _orphan_sweep_loop(self):
        try:
            await self._reconcile_admin_chat_orphans()
        except Exception as exc:
            logger.error("[PaymentUICog] Periodic orphan sweep failed: %s", exc, exc_info=True)

    async def _register_pending_confirmation_views(self):
        if not self.payment_service:
            return

        pending = self.payment_service.get_pending_confirmation_payments(
            guild_ids=self._get_writable_guild_ids(),
        )
        admin_chat_real_payment_ids = [
            str(payment.get('payment_id'))
            for payment in pending
            if payment.get('payment_id')
            and not payment.get('is_test')
            and str(payment.get('producer') or '').strip().lower() == 'admin_chat'
        ]
        admin_chat_intents = {}
        if admin_chat_real_payment_ids and hasattr(self.db_handler, 'get_pending_confirmation_admin_chat_intents_by_payment'):
            admin_chat_intents = self.db_handler.get_pending_confirmation_admin_chat_intents_by_payment(
                admin_chat_real_payment_ids
            )

        for payment in pending:
            payment_id = payment.get('payment_id')
            if not payment_id:
                continue

            producer = str(payment.get('producer') or '').strip().lower()
            if producer == 'admin_chat' and not payment.get('is_test'):
                linked_intent = admin_chat_intents.get(str(payment_id))
                linked_status = str((linked_intent or {}).get('status') or '').strip().lower()
                if linked_status in {'awaiting_admin_approval', 'awaiting_admin_init'}:
                    continue
                if linked_status != 'awaiting_confirmation':
                    continue

            self.bot.add_view(PaymentConfirmView(self, payment_id))

        if pending:
            logger.info(
                "[PaymentUICog] Re-registered %s persistent payment confirmation view(s).",
                len(pending),
            )

    async def send_confirmation_request(self, payment_id: str) -> Optional[discord.Message]:
        """Post one confirmation message with a persistent view."""
        payment = self.db_handler.get_payment_request(payment_id)
        if not payment:
            return None
        if payment.get('status') != 'pending_confirmation':
            return None

        destination = await self._resolve_destination(
            payment.get('confirm_channel_id'),
            payment.get('confirm_thread_id'),
        )
        if destination is None:
            logger.warning(
                "[PaymentUICog] Could not resolve confirmation destination for payment %s",
                payment_id,
            )
            return None

        view = PaymentConfirmView(self, payment_id)
        self.bot.add_view(view)
        return await self._send_message(
            destination,
            self._build_confirmation_message(payment),
            view=view,
        )

    async def _register_pending_admin_approval_views(self):
        if not hasattr(self.db_handler, 'list_intents_by_status'):
            return

        try:
            intents = []
            for guild_id in self._get_writable_guild_ids() or []:
                intents.extend(self.db_handler.list_intents_by_status(guild_id, 'awaiting_admin_approval'))
        except Exception as exc:
            logger.error("[PaymentUICog] Failed to list awaiting_admin_approval intents: %s", exc, exc_info=True)
            return

        registered = 0
        for intent in intents:
            payment_id = intent.get('final_payment_id')
            if not payment_id:
                continue
            self.bot.add_view(AdminApprovalView(self, payment_id))
            registered += 1

        if registered:
            logger.info(
                "[PaymentUICog] Re-registered %s persistent admin approval view(s).",
                registered,
            )

    async def _send_admin_approval_dm(self, payment: Dict[str, Any]) -> Optional[discord.Message]:
        """DM the configured admin a persistent approval control for one real admin_chat payment."""
        if payment.get('is_test') or str(payment.get('producer') or '').strip().lower() != 'admin_chat':
            return None

        admin_user_id = self._get_admin_user_id()
        if admin_user_id is None:
            logger.error("[PaymentUICog] ADMIN_USER_ID is not configured; cannot send admin approval DM for %s", payment.get('payment_id'))
            return None

        intent = self._find_admin_intent_for_payment(payment.get('payment_id'))
        view = AdminApprovalView(self, str(payment.get('payment_id')))
        self.bot.add_view(view)

        try:
            admin_user = await self.bot.fetch_user(admin_user_id)
            return await admin_user.send(
                self._build_admin_approval_message(payment, intent),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "[PaymentUICog] Failed to DM admin approval request for payment %s: %s",
                payment.get('payment_id'),
                exc,
            )
            return None

    @app_commands.command(
        name="payment-resolve",
        description="Reconcile one payment, or cancel a stuck admin intent with intent:<id>.",
    )
    @app_commands.describe(payment_id="Payment request ID, or intent:<admin intent ID> to clear a stuck admin intent")
    async def payment_resolve(self, interaction: discord.Interaction, payment_id: str):
        admin_user_id = os.getenv("ADMIN_USER_ID")
        if str(interaction.user.id) != str(admin_user_id):
            await interaction.response.send_message("admin-only", ephemeral=True)
            return

        if not self.payment_service:
            await interaction.response.send_message("payment_service unavailable", ephemeral=True)
            return

        target = str(payment_id or '').strip()
        if target.lower().startswith('intent:'):
            await self._resolve_admin_intent_target(interaction, target.split(':', 1)[1].strip())
            return

        decision = await self.payment_service.reconcile_with_chain(
            target,
            guild_id=interaction.guild_id,
        )
        payment = self.db_handler.get_payment_request(target, guild_id=interaction.guild_id) or {}
        status = payment.get('status') or 'unknown'
        tx_signature = _redact_wallet(decision.tx_signature or payment.get('tx_signature'))

        await interaction.response.send_message(
            "```text\n"
            f"decision: {decision.decision}\n"
            f"reason: {decision.reason}\n"
            f"status: {status}\n"
            f"tx_signature: {tx_signature}\n"
            "```",
            ephemeral=True,
        )

    async def _resolve_admin_intent_target(self, interaction: discord.Interaction, intent_id: str):
        if not intent_id:
            await interaction.response.send_message(
                "```text\n"
                "decision: not_applicable\n"
                "reason: intent id is required\n"
                "status: unknown\n"
                "tx_signature: unknown\n"
                "```",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        intent, lookup_error = self._find_admin_intent_for_resolve(intent_id, guild_id)
        if lookup_error:
            await interaction.response.send_message(
                "```text\n"
                "decision: not_applicable\n"
                f"reason: {lookup_error}\n"
                "status: unknown\n"
                "tx_signature: unknown\n"
                "```",
                ephemeral=True,
            )
            return
        if not intent:
            await interaction.response.send_message(
                "```text\n"
                "decision: not_applicable\n"
                "reason: admin intent not found\n"
                "status: unknown\n"
                "tx_signature: unknown\n"
                "```",
                ephemeral=True,
            )
            return

        intent_status = str(intent.get('status') or '').strip().lower()
        if intent_status in {'completed', 'failed', 'cancelled'}:
            await interaction.response.send_message(
                "```text\n"
                "decision: not_applicable\n"
                f"reason: intent already terminal ({intent_status})\n"
                f"status: {intent_status or 'unknown'}\n"
                "tx_signature: unknown\n"
                "```",
                ephemeral=True,
            )
            return

        resolved_intent_id = str(intent.get('intent_id') or intent_id)
        linked_statuses: List[str] = []
        for payment_field in ('test_payment_id', 'final_payment_id'):
            linked_payment_id = intent.get(payment_field)
            if not linked_payment_id:
                continue

            payment = self.db_handler.get_payment_request(linked_payment_id, guild_id=guild_id)
            if not payment:
                continue

            payment_status = str(payment.get('status') or '').strip().lower()
            if payment_field == 'test_payment_id' and payment_status == 'confirmed':
                linked_statuses.append(f"{linked_payment_id}:confirmed")
                continue

            if payment_status in {'submitted', 'confirmed', 'manual_hold'}:
                await interaction.response.send_message(
                    "```text\n"
                    "decision: keep_in_hold\n"
                    f"reason: linked payment {linked_payment_id} is {payment_status}; run /payment-resolve {linked_payment_id}\n"
                    f"status: {intent_status or 'unknown'}\n"
                    f"tx_signature: {_redact_wallet(payment.get('tx_signature'))}\n"
                    "```",
                    ephemeral=True,
                )
                return

            if payment_status != 'cancelled':
                cancelled = self.db_handler.cancel_payment(
                    linked_payment_id,
                    guild_id=guild_id,
                    reason=f"Admin cancelled payment intent {resolved_intent_id}",
                )
                if not cancelled:
                    await interaction.response.send_message(
                        "```text\n"
                        "decision: keep_in_hold\n"
                        f"reason: linked payment {linked_payment_id} could not be cancelled from status {payment_status}\n"
                        f"status: {intent_status or 'unknown'}\n"
                        f"tx_signature: {_redact_wallet(payment.get('tx_signature'))}\n"
                        "```",
                        ephemeral=True,
                    )
                    return
                payment_status = 'cancelled'

            linked_statuses.append(f"{linked_payment_id}:{payment_status}")

        updated_intent = self.db_handler.update_admin_payment_intent(
            resolved_intent_id,
            {'status': 'cancelled'},
            guild_id,
        )
        if not updated_intent:
            await interaction.response.send_message(
                "```text\n"
                "decision: keep_in_hold\n"
                "reason: failed to cancel admin intent\n"
                f"status: {intent_status or 'unknown'}\n"
                "tx_signature: unknown\n"
                "```",
                ephemeral=True,
            )
            return

        status_detail = ', '.join(linked_statuses) if linked_statuses else 'no linked payments'
        await interaction.response.send_message(
            "```text\n"
            "decision: intent_cancelled\n"
            f"reason: cleared admin intent; {status_detail}\n"
            "status: cancelled\n"
            "tx_signature: unknown\n"
            "```",
            ephemeral=True,
        )

    def _find_admin_intent_for_resolve(
        self,
        intent_id: str,
        guild_id: Optional[int],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        is_full_uuid = False
        try:
            is_full_uuid = str(UUID(intent_id)) == intent_id.lower()
        except (TypeError, ValueError):
            is_full_uuid = False

        if not is_full_uuid:
            list_active_intents = getattr(self.db_handler, 'list_active_intents', None)
            if not callable(list_active_intents):
                return None, None
            matches = [
                intent
                for intent in (list_active_intents(guild_id) or [])
                if str(intent.get('intent_id') or '').startswith(intent_id)
            ]
            if len(matches) > 1:
                return None, f"intent prefix {intent_id} matched multiple active intents"
            if matches:
                return matches[0], None
            return None, None

        try:
            return self.db_handler.get_admin_payment_intent(intent_id, guild_id), None
        except Exception as exc:
            logger.warning("[PaymentUICog] Failed to fetch admin intent %s: %s", intent_id, exc)
            return None, None

    async def _resolve_destination(
        self,
        channel_id: Optional[int],
        thread_id: Optional[int],
    ) -> Optional[discord.abc.Messageable]:
        target_id = int(thread_id) if thread_id else int(channel_id) if channel_id else None
        if target_id is None:
            return None

        destination = self.bot.get_channel(target_id)
        if destination is not None:
            return destination

        try:
            return await self.bot.fetch_channel(target_id)
        except Exception as exc:
            logger.warning("[PaymentUICog] Failed to fetch destination %s: %s", target_id, exc)
            return None

    async def _send_message(
        self,
        destination: discord.abc.Messageable,
        content: str,
        *,
        view: Optional[discord.ui.View] = None,
    ) -> Optional[discord.Message]:
        rate_limiter = getattr(self.bot, 'rate_limiter', None)
        if rate_limiter is not None:
            return await safe_send_message(
                self.bot,
                destination,
                rate_limiter,
                logger,
                content=content,
                view=view,
            )
        return await destination.send(content, view=view)

    def _build_confirmation_message(self, payment: Dict[str, Any]) -> str:
        amount = float(payment.get('amount_token') or 0)
        payment_type = 'test payment' if payment.get('is_test') else 'final payment'
        return (
            f"**Payment confirmation required**\n"
            f"- Payment ID: `{payment.get('payment_id')}`\n"
            f"- Producer: `{payment.get('producer')}` / `{payment.get('producer_ref')}`\n"
            f"- Type: {payment_type}\n"
            f"- Amount: {amount:.8f} {self._token_label(payment)}\n"
            f"- Wallet: `{_redact_wallet(payment.get('recipient_wallet'))}`\n"
            f"- Route: `{payment.get('route_key') or 'unrouted'}`"
        )

    def _build_admin_approval_message(self, payment: Dict[str, Any], intent: Optional[Dict[str, Any]]) -> str:
        amount = float(payment.get('amount_token') or 0)
        recipient_user_id = payment.get('recipient_discord_id')
        thread_link = self._build_admin_intent_jump_link(payment, intent)
        return (
            f"**Admin approval required**\n"
            f"- Payment ID: `{payment.get('payment_id')}`\n"
            f"- Intent ID: `{(intent or {}).get('intent_id') or 'unknown'}`\n"
            f"- Recipient: <@{recipient_user_id}>\n"
            f"- Amount: {amount:.8f} {self._token_label(payment)}\n"
            f"- Wallet: `{_redact_wallet(payment.get('recipient_wallet'))}`\n"
            f"- Thread: {thread_link}"
        )

    def _token_label(self, payment: Dict[str, Any]) -> str:
        provider = None
        provider_name = payment.get('provider')
        if provider_name and self.payment_service:
            providers = getattr(self.payment_service, 'providers', None) or {}
            provider = providers.get(str(provider_name).strip().lower())
        if provider is not None:
            try:
                return provider.token_name()
            except Exception:
                pass
        chain = str(payment.get('chain') or '').strip().upper()
        return chain or 'TOKEN'

    def _get_writable_guild_ids(self) -> Optional[List[int]]:
        server_config = getattr(self.db_handler, 'server_config', None)
        if not server_config:
            return None
        guild_ids = [int(server['guild_id']) for server in server_config.get_enabled_servers(require_write=True)]
        return guild_ids or None

    def _get_admin_user_id(self) -> Optional[int]:
        raw_admin_user_id = os.getenv("ADMIN_USER_ID")
        if raw_admin_user_id in (None, ""):
            return None
        try:
            return int(raw_admin_user_id)
        except (TypeError, ValueError):
            logger.error("[PaymentUICog] Invalid ADMIN_USER_ID value: %r", raw_admin_user_id)
            return None

    def _find_admin_intent_for_payment(self, payment_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not payment_id or not hasattr(self.db_handler, 'find_admin_chat_intent_by_payment_id'):
            return None
        try:
            return self.db_handler.find_admin_chat_intent_by_payment_id(str(payment_id))
        except Exception as exc:
            logger.warning("[PaymentUICog] Failed to resolve admin intent for payment %s: %s", payment_id, exc)
            return None

    def _build_admin_intent_jump_link(self, payment: Dict[str, Any], intent: Optional[Dict[str, Any]]) -> str:
        guild_id = payment.get('guild_id')
        channel_id = (intent or {}).get('channel_id') or payment.get('notify_thread_id') or payment.get('confirm_thread_id') or payment.get('notify_channel_id') or payment.get('confirm_channel_id')
        if guild_id and channel_id:
            return f"https://discord.com/channels/{guild_id}/{channel_id}"
        return "unavailable"

    async def _post_admin_approval_thread_update(self, payment: Dict[str, Any]):
        intent = self._find_admin_intent_for_payment(payment.get('payment_id'))
        if not intent:
            return
        channel = await self._resolve_destination(intent.get('channel_id'), None)
        if channel is None:
            return
        recipient_user_id = intent.get('recipient_user_id') or payment.get('recipient_discord_id')
        try:
            status_message_id = intent.get('status_message_id')
            if status_message_id is not None:
                fetch_message = getattr(channel, 'fetch_message', None)
                if callable(fetch_message):
                    try:
                        status_message = await fetch_message(int(status_message_id))
                    except (discord.NotFound, discord.HTTPException):
                        status_message = None
                    if status_message is not None:
                        await status_message.edit(
                            content='Payout queued for sending.',
                            suppress=True,
                        )
                        return
            await channel.send(f"Payment to <@{recipient_user_id}> queued for sending.")
        except Exception as exc:
            logger.warning(
                "[PaymentUICog] Failed to post queued-for-sending update for payment %s: %s",
                payment.get('payment_id'),
                exc,
            )

    async def _cleanup_admin_intent_messages(self, payment: Dict[str, Any]):
        intent = self._find_admin_intent_for_payment(payment.get('payment_id'))
        if not intent:
            return
        channel = await self._resolve_destination(intent.get('channel_id'), None)
        if channel is None:
            return
        status_message_id = intent.get('status_message_id')
        protected_status_message_id = int(status_message_id) if status_message_id is not None else None
        message_ids = [
            int(message_id)
            for message_id in (
                intent.get('prompt_message_id'),
                intent.get('receipt_prompt_message_id'),
            )
            if message_id is not None and int(message_id) != protected_status_message_id
        ]
        if not message_ids:
            return
        await safe_delete_messages(channel, message_ids, logger=logger)


async def setup(bot: commands.Bot):
    db_handler = getattr(bot, 'db_handler', None)
    payment_service = getattr(bot, 'payment_service', None)
    if db_handler is None or payment_service is None:
        logger.error("PaymentUICog setup skipped because db_handler or payment_service is missing.")
        return
    await bot.add_cog(PaymentUICog(bot, db_handler, payment_service=payment_service))
