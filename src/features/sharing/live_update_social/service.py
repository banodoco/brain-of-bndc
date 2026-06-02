"""LiveUpdateSocialService — SharingCog-owned runtime skeleton.

This service is the single entrypoint for live-update → social review
handoff.  It is instantiated by SharingCog and exposed on
``bot.live_update_social_service``.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

import discord

from .contracts import LiveUpdateHandoffPayload
from .helpers import inspect_discord_message

if TYPE_CHECKING:
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
        social_publish_service: Any = None,
    ):
        self.db_handler = db_handler
        self._bot = bot
        self._log = logger_instance or logger
        self.social_publish_service = social_publish_service

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
                # ── DM the admin when agent produced a draft ───────────
                if terminal == "draft" and run_id:
                    # Re-fetch the run row to read the persisted draft_text
                    row = self.db_handler.get_live_update_social_run(run_id)
                    if row:
                        draft_text = row.get("draft_text")
                        media_decisions = row.get("media_decisions") or {}
                        topic_summary_data = row.get("topic_summary_data") or {}
                        source_metadata = row.get("source_metadata") or {}
                        topic_title = self._resolve_topic_title(topic_summary_data)
                        source_link = (
                            source_metadata.get("source_link")
                            or source_metadata.get("message_link")
                            or ""
                        )
                        await self._dm_admin_with_draft(
                            run_id=run_id,
                            draft_text=draft_text,
                            media_decisions=media_decisions,
                            topic_title=topic_title,
                            source_link=source_link,
                        )
                    else:
                        self._log.warning(
                            "LiveUpdateSocialService: could not re-fetch run %s "
                            "after agent returned draft",
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
        """Invoke the LiveUpdateSocialAgent for a draft/queue decision.

        Returns the terminal_status (``\"draft\"``, ``\"skip\"``,
        ``\"needs_review\"``, ``\"queued\"``) or ``None`` on failure.
        """
        try:
            from .agent import LiveUpdateSocialAgent

            # Resolve social_publish_service from the bot if available
            social_publish_service = getattr(
                self, "social_publish_service", None,
            )
            if social_publish_service is None and self._bot is not None:
                social_publish_service = getattr(
                    self._bot, "social_publish_service", None,
                )

            agent = LiveUpdateSocialAgent(
                db_handler=self.db_handler,
                bot=self._bot,
                social_publish_service=social_publish_service,
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

    def _resolve_topic_title(self, topic_summary_data: dict) -> str:
        """Return a cleaned human-facing topic title, if one is available."""
        if not isinstance(topic_summary_data, dict):
            return ""

        for key in ("title", "headline", "name", "subject"):
            value = topic_summary_data.get(key)
            if value is None:
                continue
            title = str(value).strip()
            if title:
                return title
        return ""

    @staticmethod
    def _is_image_media(url: Optional[str], content_type: Optional[str] = None) -> bool:
        """Best-effort image detection from content-type or filename."""
        if content_type:
            return content_type.lower().split(";", 1)[0].strip().startswith("image/")

        if not url:
            return False

        guessed_type, _ = mimetypes.guess_type(url.split("?", 1)[0])
        return bool(guessed_type and guessed_type.startswith("image/"))

    async def _resolve_thumbnail_url(self, media_decisions: dict) -> Optional[str]:
        """Resolve the first selected image media ref to a thumbnail URL."""
        if not isinstance(media_decisions, dict):
            return None

        selected = media_decisions.get("selected", [])
        if not isinstance(selected, list):
            return None

        for media in selected:
            if not isinstance(media, dict):
                continue

            source = media.get("source")
            if source == "url":
                thumb_url = media.get("url")
                if thumb_url:
                    return str(thumb_url)
                continue

            if source not in {"discord_attachment", "discord_embed"}:
                thumb_url = (
                    media.get("url")
                    or media.get("proxy_url")
                    or media.get("cdn_url")
                )
                if thumb_url:
                    return str(thumb_url)
                continue

            if self._bot is None:
                continue

            try:
                channel_id = media.get("channel_id")
                message_id = media.get("message_id")
                if channel_id is None or message_id is None:
                    continue

                inspected = await inspect_discord_message(
                    self._bot,
                    int(channel_id),
                    int(message_id),
                )
                if inspected.get("error"):
                    continue

                if source == "discord_attachment":
                    raw_index = media.get("attachment_index")
                    if raw_index is None:
                        raw_index = media.get("index")
                    if raw_index is None:
                        continue
                    attachments = inspected.get("attachments", [])
                    try:
                        attachment = attachments[int(raw_index)]
                    except (IndexError, TypeError, ValueError):
                        continue

                    url = attachment.get("url")
                    if self._is_image_media(url, attachment.get("content_type")):
                        return str(url)

                if source == "discord_embed":
                    embed_slot = media.get("embed_slot")
                    if not embed_slot or embed_slot == "video":
                        continue
                    for embed_media in inspected.get("embeds_media", []):
                        url = embed_media.get("url")
                        slot = embed_media.get("slot")
                        if slot != embed_slot:
                            continue
                        if slot in {"image", "thumbnail", "author_icon", "footer_icon"}:
                            return str(url)
                        if self._is_image_media(url):
                            return str(url)
            except Exception:
                continue

        return None

    async def _dm_admin_with_draft(
        self,
        run_id: str,
        draft_text: Optional[str],
        media_decisions: dict,
        topic_title: str,
        source_link: str,
    ) -> None:
        """DM the admin with a draft embed and persist the review_message_id.

        If *draft_text* is ``None`` or whitespace-only, logs a warning and
        skips both the DM send and the DB write entirely.  The entire body
        is wrapped in try/except so neither DM nor DB failure ever raises.
        """
        try:
            if not draft_text or not draft_text.strip():
                self._log.warning(
                    "LiveUpdateSocialService: _dm_admin_with_draft skipped — "
                    "draft_text is empty for run %s",
                    run_id,
                )
                return

            # ── resolve admin user ────────────────────────────────────
            admin_id = os.getenv("ADMIN_USER_ID")
            if not admin_id:
                self._log.warning(
                    "LiveUpdateSocialService: ADMIN_USER_ID not set — "
                    "cannot DM admin for run %s",
                    run_id,
                )
                return

            user = await self._bot.fetch_user(int(admin_id))

            # ── build embed ───────────────────────────────────────────
            embed = discord.Embed(
                title=topic_title or "(untitled)",
                description=draft_text,
                color=0x3498DB,
            )

            # Thumbnail: first resolvable image in media_decisions.selected.
            thumb_url = await self._resolve_thumbnail_url(media_decisions)
            if thumb_url:
                embed.set_thumbnail(url=str(thumb_url))

            if source_link:
                embed.set_footer(text=f"Source: {source_link}")

            # ── send DM ───────────────────────────────────────────────
            msg = await user.send(embed=embed)

            # ── persist review_message_id ─────────────────────────────
            env = os.getenv("ENVIRONMENT", "prod")
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat()
            ok = self.db_handler.update_live_update_social_run(
                run_id=run_id,
                review_message_id=msg.id,
                expires_at=expires_at,
                environment=env,
            )
            if not ok:
                self._log.error(
                    "LiveUpdateSocialService: DM sent successfully (msg.id=%s) "
                    "but DB write of review_message_id FAILED for run %s",
                    msg.id,
                    run_id,
                )

        except Exception:
            self._log.exception(
                "LiveUpdateSocialService: _dm_admin_with_draft failed for run %s",
                run_id,
            )
