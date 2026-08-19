# src/features/health/health_check_cog.py

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

logger = logging.getLogger('DiscordBot')


class HealthCheckCog(commands.Cog):
    """Periodic health checks that DM the admin when something looks wrong."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = getattr(bot, 'db_handler', None)
        self.health_check_loop.start()

    def cog_unload(self):
        self.health_check_loop.cancel()

    # ------------------------------------------------------------------
    # Scheduled loop – runs every 6 hours
    # ------------------------------------------------------------------
    @tasks.loop(hours=6)
    async def health_check_loop(self):
        alerts: list[str] = []

        try:
            alerts.extend(self._check_recent_messages())
        except Exception as e:
            logger.error(f"[HealthCheck] Error checking recent messages: {e}", exc_info=True)

        try:
            alerts.extend(self._check_reactions_recorded())
        except Exception as e:
            logger.error(f"[HealthCheck] Error checking reactions: {e}", exc_info=True)

        try:
            alerts.extend(self._check_live_update_editor())
        except Exception as e:
            logger.error(f"[HealthCheck] Error checking live-update editor: {e}", exc_info=True)

        if alerts:
            await self._notify_admin(alerts)
        else:
            logger.info("[HealthCheck] All checks passed")

    @health_check_loop.before_loop
    async def before_health_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    def _get_supabase(self):
        """Return the Supabase client, or None."""
        if self.db and self.db.storage_handler and self.db.storage_handler.supabase_client:
            return self.db.storage_handler.supabase_client
        return None

    def _check_recent_messages(self) -> list[str]:
        """Alert if no messages were indexed in the last 6 hours."""
        sb = self._get_supabase()
        if not sb:
            return ["Supabase client unavailable – cannot check recent messages"]

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        result = (
            sb.table('discord_messages')
            .select('message_id', count='exact')
            .gte('indexed_at', cutoff)
            .limit(1)
            .execute()
        )
        count = result.count if result.count is not None else len(result.data)
        if count == 0:
            return ["No messages indexed in the last 6 hours"]
        return []

    def _check_reactions_recorded(self) -> list[str]:
        """Alert if there are recent messages but none have reactions."""
        sb = self._get_supabase()
        if not sb:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        # Check if any recent messages exist at all
        msg_result = (
            sb.table('discord_messages')
            .select('message_id', count='exact')
            .gte('created_at', cutoff)
            .limit(1)
            .execute()
        )
        msg_count = msg_result.count if msg_result.count is not None else len(msg_result.data)
        if msg_count == 0:
            return []  # No messages at all – nothing to check

        # Check if any of those have reactions
        react_result = (
            sb.table('discord_messages')
            .select('message_id', count='exact')
            .gte('created_at', cutoff)
            .gt('reaction_count', 0)
            .limit(1)
            .execute()
        )
        react_count = react_result.count if react_result.count is not None else len(react_result.data)
        if react_count == 0:
            return ["No messages with reaction_count > 0 in the last 24 hours (reaction updates may be broken)"]
        return []

    def _check_live_update_editor(self) -> list[str]:
        """Alert if the active topic editor has stopped or is failing."""
        sb = self._get_supabase()
        if not sb:
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        result = (
            sb.table('topic_editor_runs')
            .select('run_id,status,started_at,completed_at,error_message,source_message_count,tool_call_count,accepted_count,rejected_count,override_count,published_count,failed_publish_count', count='exact')
            .gte('started_at', cutoff)
            .order('started_at', desc=True)
            .limit(1)
            .execute()
        )
        count = result.count if result.count is not None else len(result.data)
        if count == 0:
            return ["No topic-editor runs recorded in the last 2 hours"]

        latest = result.data[0] if result.data else {}
        status = latest.get('status')
        if status == 'failed':
            return [f"Latest topic-editor run failed: {latest.get('error_message') or latest.get('run_id')}"]
        if status == 'running':
            started_at = latest.get('started_at')
            if started_at:
                try:
                    started_dt = datetime.fromisoformat(str(started_at).replace('Z', '+00:00'))
                    age_minutes = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
                    if age_minutes > 45:
                        return [f"Topic-editor run has been running for {age_minutes:.0f} minutes"]
                except ValueError:
                    pass
        if int(latest.get('failed_publish_count') or 0) > 0:
            return [
                "Latest topic-editor run had failed or partial publications: "
                f"{latest.get('failed_publish_count')}"
            ]
        if (
            status != 'running'
            and latest.get('source_message_count') is not None
            and int(latest.get('source_message_count') or 0) == 0
        ):
            return ["Latest topic-editor run had no source data"]

        draft_alert = self._check_topic_editor_drafts(sb, cutoff)
        if draft_alert:
            return [draft_alert]

        publication_result = (
            sb.table('topics')
            .select('topic_id,headline,publication_status,publication_error,updated_at', count='exact')
            .gte('updated_at', cutoff)
            .order('updated_at', desc=True)
            .limit(10)
            .execute()
        )
        publication_problems = [
            row for row in (publication_result.data or [])
            if row.get('publication_status') in {'failed', 'partial'}
        ]
        if publication_problems:
            first = publication_problems[0]
            return [
                "Topic-editor has failed or partial publications: "
                f"{first.get('headline') or first.get('topic_id')} "
                f"({first.get('publication_status')})"
            ]
        transition_alert = self._check_topic_editor_transition_diagnostics(sb, cutoff)
        if transition_alert:
            return [transition_alert]
        return []

    @staticmethod
    def _topic_editor_diagnostic_codes(row: dict) -> set[str]:
        codes: set[str] = set()
        diagnostics = row.get('publish_diagnostics') or {}
        if isinstance(diagnostics, dict):
            for code in diagnostics.get('reason_codes') or []:
                if code:
                    codes.add(str(code))
            for failure in diagnostics.get('media_failures') or []:
                if isinstance(failure, dict) and failure.get('reason_code'):
                    codes.add(str(failure.get('reason_code')))
            if diagnostics.get('renderer_safety_chunking_used'):
                codes.add('renderer_safety_chunking_used')
            if diagnostics.get('legacy_direct_post_used'):
                codes.add('legacy_direct_post_used')
        validation = row.get('validation_result') or {}
        if isinstance(validation, dict):
            if validation.get('errors'):
                codes.add('draft_validation_failed')
            validation_text = str(validation).lower()
            if 'preview' in validation_text and (
                'stale' in validation_text
                or 'missing' in validation_text
                or 'latest_valid_preview_hash' in validation_text
            ):
                codes.add('stale_or_missing_preview')
        publish_result = row.get('publish_result') or {}
        if isinstance(publish_result, dict):
            status = publish_result.get('status')
            if status in {'failed', 'partial'}:
                codes.add(f'publish_{status}')
        if row.get('status') == 'needs_revision':
            codes.add('draft_validation_failed')
        if row.get('status') == 'abandoned':
            codes.add('draft_abandoned')
        return codes

    @staticmethod
    def _topic_editor_validation_detail(validation_result) -> str:
        """Short human summary of the first validation errors, if any.

        The health alert otherwise says only "draft validation failed:
        <draft_id>" with no hint of *what* failed — the errors live in
        ``validation_result.errors`` and are surfaced here.
        """
        if not isinstance(validation_result, dict):
            return ""
        errors = validation_result.get('errors') or []
        if not isinstance(errors, list):
            return ""
        messages: list[str] = []
        seen: set[str] = set()
        for err in errors:
            if not isinstance(err, dict):
                continue
            message = err.get('message')
            if not message:
                continue
            text = str(message).strip()
            if text and text not in seen:
                seen.add(text)
                messages.append(text)
            if len(messages) >= 2:
                break
        detail = '; '.join(messages)
        if len(detail) > 300:
            detail = detail[:300].rstrip() + '…'
        return detail

    @staticmethod
    def _topic_editor_alert_for_codes(
        codes: set[str],
        label: str,
        detail: str = "",
    ) -> str | None:
        if not codes:
            return None
        priority = [
            ('draft_validation_failed', 'draft validation failed'),
            ('draft_abandoned', 'draft abandoned'),
            ('stale_or_missing_preview', 'stale or missing preview submit refusal'),
            ('legacy_post_disabled', 'legacy direct-post refusal'),
            ('legacy_direct_post_used', 'legacy direct-post usage'),
            ('publish_failed', 'publish failed'),
            ('publish_partial', 'publish partially succeeded'),
            ('media_url_expired', 'media URL expired'),
            ('media_payload_too_large', 'media payload too large'),
            ('renderer_safety_chunking_used', 'renderer safety chunking used'),
        ]
        for code, message in priority:
            if code in codes:
                alert = f"Topic-editor {message}: {label}"
                if code == 'draft_validation_failed' and detail:
                    alert = f"{alert} — {detail}"
                return alert
        return f"Topic-editor diagnostics present: {label} ({', '.join(sorted(codes))})"

    def _check_topic_editor_drafts(self, sb, cutoff: str) -> str | None:
        try:
            result = (
                sb.table('topic_editor_drafts')
                .select(
                    'draft_id,run_id,topic_id,status,validation_result,publish_result,'
                    'publish_diagnostics,revision_number,revision_hash,'
                    'latest_valid_preview_hash,updated_at'
                )
                .gte('updated_at', cutoff)
                .order('updated_at', desc=True)
                .limit(10)
                .execute()
            )
        except Exception as e:
            logger.debug(f"[HealthCheck] topic_editor_drafts unavailable: {e}")
            return None
        for draft in result.data or []:
            codes = self._topic_editor_diagnostic_codes(draft)
            alert = self._topic_editor_alert_for_codes(
                codes,
                str(draft.get('draft_id') or draft.get('run_id') or 'draft'),
                detail=self._topic_editor_validation_detail(
                    draft.get('validation_result')
                ),
            )
            if alert:
                return alert
        return None

    def _check_topic_editor_transition_diagnostics(self, sb, cutoff: str) -> str | None:
        try:
            result = (
                sb.table('topic_transitions')
                .select('transition_id,run_id,topic_id,action,reason,extra,metadata,created_at')
                .gte('created_at', cutoff)
                .order('created_at', desc=True)
                .limit(20)
                .execute()
            )
        except Exception as e:
            logger.debug(f"[HealthCheck] topic_transitions unavailable: {e}")
            return None
        for transition in result.data or []:
            text = (
                f"{transition.get('action') or ''} "
                f"{transition.get('reason') or ''} "
                f"{transition.get('extra') or transition.get('metadata') or ''}"
            ).lower()
            codes: set[str] = set()
            if 'legacy_post_disabled' in text:
                codes.add('legacy_post_disabled')
            if 'legacy_direct_post_used' in text:
                codes.add('legacy_direct_post_used')
            extra = transition.get('extra') or transition.get('metadata') or {}
            detail = ""
            if isinstance(extra, dict):
                codes.update(self._topic_editor_diagnostic_codes(extra))
                detail = self._topic_editor_validation_detail(
                    extra.get('validation_result')
                )
            alert = self._topic_editor_alert_for_codes(
                codes,
                str(transition.get('transition_id') or transition.get('run_id') or 'transition'),
                detail=detail,
            )
            if alert:
                return alert
        return None

    # ------------------------------------------------------------------
    # Admin notification
    # ------------------------------------------------------------------
    async def _notify_admin(self, alerts: list[str]):
        admin_id_str = os.getenv('ADMIN_USER_ID')
        if not admin_id_str:
            logger.error("[HealthCheck] ADMIN_USER_ID not set – cannot send alerts")
            return

        try:
            admin_id = int(admin_id_str)
        except ValueError:
            logger.error("[HealthCheck] ADMIN_USER_ID is not a valid integer")
            return

        body = "\n".join(f"- {a}" for a in alerts)
        message = f"**Health Check Alert**\n{body}"
        if len(message) > 1900:
            message = message[:1900] + "..."

        try:
            admin_user = await self.bot.fetch_user(admin_id)
            await admin_user.send(message)
            logger.info(f"[HealthCheck] Sent {len(alerts)} alert(s) to admin")
        except discord.HTTPException as e:
            if e.status == 429:
                logger.warning("[HealthCheck] Rate limited sending admin DM – skipping")
            else:
                logger.error(f"[HealthCheck] Failed to DM admin: {e}")
        except Exception as e:
            logger.error(f"[HealthCheck] Failed to DM admin: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------
    @commands.command(name="healthcheck")
    async def healthcheck_command(self, ctx: commands.Context):
        """Manually trigger a health check."""
        alerts: list[str] = []
        alerts.extend(self._check_recent_messages())
        alerts.extend(self._check_reactions_recorded())
        alerts.extend(self._check_live_update_editor())

        if alerts:
            body = "\n".join(f"- {a}" for a in alerts)
            await ctx.send(f"**Health Check Issues:**\n{body}")
        else:
            await ctx.send("All health checks passed.")
