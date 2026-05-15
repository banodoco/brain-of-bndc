"""LiveUpdateSocialService — SharingCog-owned runtime skeleton.

This service is the single entrypoint for live-update → social review
handoff.  It is instantiated by SharingCog and exposed on
``bot.live_update_social_service``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

from .contracts import LiveUpdateHandoffPayload

if TYPE_CHECKING:
    import discord
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")


class LiveUpdateSocialService:
    """Best-effort social review trigger owned by SharingCog.

    Sprint 1 only filters eligible payloads (status ∈ {sent, partial},
    action = post) and creates draft runs.  No publishing is performed.
    """

    def __init__(
        self,
        db_handler: "DatabaseHandler",
        bot: Optional["discord.Client"] = None,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.db_handler = db_handler
        self._bot = bot
        self._log = logger_instance or logger

    async def handle_live_update_publish_results(
        self,
        payload: LiveUpdateHandoffPayload,
    ) -> str:
        """Process a single handoff payload.

        Best-effort: filters eligible payloads, upserts a run, and
        invokes the LiveUpdateSocialAgent for a draft decision.
        Logs and continues on errors — the upstream live-update result
        is authoritative; failure here does NOT create a social
        publication.

        Returns the run_id of the created (or re-used) run, or an empty
        string if the payload was rejected.
        """
        # ── eligibility gate ──────────────────────────────────────────
        if not payload.is_eligible():
            self._log.debug(
                "LiveUpdateSocialService: payload %s / %s / %s not eligible "
                "(status=%r action=%r) — skipping.",
                payload.topic_id,
                payload.platform,
                payload.action,
                payload.status,
                payload.action,
            )
            return ""

        run_id = ""
        try:
            # ── upsert run (durable duplicate guard on topic_id+platform+action) ──
            run = self.db_handler.upsert_live_update_social_run(
                topic_id=payload.topic_id,
                platform=payload.platform,
                action=payload.action,
                guild_id=payload.guild_id,
                channel_id=payload.channel_id,
                source_metadata=payload.source_metadata,
                topic_summary_data=payload.topic_summary_data,
                vendor=payload.vendor,
                depth=payload.depth,
                with_feedback=payload.with_feedback,
                deepseek_provider=payload.deepseek_provider,
            )
            if not run:
                self._log.error(
                    "LiveUpdateSocialService: upsert returned None for %s/%s/%s",
                    payload.topic_id,
                    payload.platform,
                    payload.action,
                )
                return ""

            run_id = run.get("run_id") or ""
            self._log.info(
                "LiveUpdateSocialService: run %r recorded for topic %s (%s/%s)",
                run_id,
                payload.topic_id,
                payload.platform,
                payload.action,
            )

            # ── invoke agent (best-effort) ────────────────────────────
            if self._bot is not None:
                terminal = await self._invoke_agent(payload)
                self._log.info(
                    "LiveUpdateSocialService: agent returned terminal_status=%r "
                    "for run %s",
                    terminal,
                    run_id,
                )
            else:
                self._log.warning(
                    "LiveUpdateSocialService: no bot available — cannot invoke "
                    "agent for run %s (upsert completed)",
                    run_id,
                )

        except Exception:
            self._log.exception(
                "LiveUpdateSocialService: error processing run for %s/%s/%s",
                payload.topic_id,
                payload.platform,
                payload.action,
            )
            # Best-effort: failure here does NOT create a social publication.
            # The run was already upserted; the caller can inspect it.

        return run_id

    async def _invoke_agent(
        self,
        payload: LiveUpdateHandoffPayload,
    ) -> Optional[str]:
        """Invoke the LiveUpdateSocialAgent for a draft decision.

        Returns the terminal_status (``"draft"``, ``"skip"``,
        ``"needs_review"``) or ``None`` on failure.
        """
        try:
            from .agent import LiveUpdateSocialAgent

            agent = LiveUpdateSocialAgent(
                db_handler=self.db_handler,
                bot=self._bot,
            )
            terminal = await agent.run(payload)
            return terminal
        except Exception:
            self._log.exception(
                "LiveUpdateSocialService: agent invocation failed for %s/%s/%s",
                payload.topic_id,
                payload.platform,
                payload.action,
            )
            return None
