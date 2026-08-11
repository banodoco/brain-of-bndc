"""LiveUpdateSocialService — SharingCog-owned runtime skeleton.

This service is the single entrypoint for live-update → social review
handoff.  It is instantiated by SharingCog and exposed on
``bot.live_update_social_service``.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiohttp
import discord

from .contracts import LiveUpdateHandoffPayload
from .helpers import inspect_discord_message

if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger("DiscordBot")

DM_MEDIA_LINK_LIMIT = 10
DM_MEDIA_FILE_LIMIT = 3
DM_MEDIA_FILE_MAX_BYTES = 8 * 1024 * 1024
DM_MEDIA_TOTAL_FILE_MAX_BYTES = 20 * 1024 * 1024
DM_MEDIA_HTTP_TIMEOUT_SECONDS = 10
DM_REVIEW_HOW_TO = (
    'Reply to this message to edit · say **"post it"** to publish · '
    '**"skip"** to discard · **"list drafts"** to see all pending.'
)


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

            # ── replay guard ─────────────────────────────────────────
            # The upsert returns the EXISTING row on a duplicate handoff
            # (topic_id+platform+action). A run that already reached a
            # terminal status must not re-invoke the agent — that would
            # overwrite proposals/drafts and spam the admin with a second
            # DM. Only fresh runs (terminal_status is NULL) get an agent turn.
            existing_status = run.get("terminal_status")
            if existing_status:
                self._log.info(
                    "LiveUpdateSocialService: run %s already terminal "
                    "(status=%r) — skipping agent invocation on replay",
                    run_id,
                    existing_status,
                )
                return run_id

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
                        # topic_summary_data is stored under publish_units
                        # (see upsert_live_update_social_run); fall back there.
                        topic_summary_data = (
                            row.get("topic_summary_data")
                            or row.get("publish_units")
                            or {}
                        )
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

                # ── DM the admin when agent produced proposals ────────
                if terminal == "proposed" and run_id:
                    row = self.db_handler.get_live_update_social_run(run_id)
                    if row:
                        proposals = row.get("proposals") or []
                        media_decisions = row.get("media_decisions") or {}
                        # topic_summary_data is stored under publish_units.
                        topic_summary_data = (
                            row.get("topic_summary_data")
                            or row.get("publish_units")
                            or {}
                        )
                        source_metadata = row.get("source_metadata") or {}
                        topic_title = self._resolve_topic_title(topic_summary_data)
                        source_link = (
                            source_metadata.get("source_link")
                            or source_metadata.get("message_link")
                            or ""
                        )
                        await self._dm_admin_with_proposals(
                            run_id=run_id,
                            proposals=proposals,
                            media_decisions=media_decisions,
                            topic_title=topic_title,
                            source_link=source_link,
                        )
                    else:
                        self._log.warning(
                            "LiveUpdateSocialService: could not re-fetch run %s "
                            "after agent returned proposals",
                            run_id,
                        )

                # ── DM the admin when the run needs review (agent could
                # not produce a safe proposal/draft) or publish failed ──
                if terminal in ("needs_review", "failed") and run_id:
                    row = self.db_handler.get_live_update_social_run(run_id)
                    if row:
                        trace_entries = row.get("trace_entries") or []
                        reason = self._extract_terminal_reason(
                            terminal, trace_entries, row.get("publication_outcome")
                        )
                        topic_summary_data = (
                            row.get("topic_summary_data")
                            or row.get("publish_units")
                            or {}
                        )
                        topic_title = self._resolve_topic_title(topic_summary_data)
                        await self._dm_admin_with_terminal(
                            run_id=run_id,
                            terminal_status=terminal,
                            reason=reason,
                            topic_title=topic_title,
                        )
                    else:
                        self._log.warning(
                            "LiveUpdateSocialService: could not re-fetch run %s "
                            "after agent returned %s",
                            run_id, terminal,
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
    def _extract_terminal_reason(
        terminal_status: str,
        trace_entries: list,
        publication_outcome: Any,
    ) -> str:
        """Extract a human-readable reason for a needs_review / failed run."""
        if not isinstance(trace_entries, list):
            trace_entries = []

        if terminal_status == "failed":
            # Publish failure — publication_outcome carries the error.
            if isinstance(publication_outcome, dict):
                err = publication_outcome.get("error")
                if err:
                    return str(err)
                reason = publication_outcome.get("failure_reason")
                if reason:
                    return f"publish failed: {reason}"

        # needs_review — walk trace entries for force_needs_review reason.
        for entry in reversed(trace_entries):
            if not isinstance(entry, dict):
                continue
            if entry.get("event") in ("force_needs_review", "media_resolution_failed"):
                reason = entry.get("reason") or entry.get("error")
                if reason:
                    return str(reason)
            if entry.get("event") == "tool" and entry.get("error"):
                return str(entry["error"])

        return "No reason recorded — see run logs for details."

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

    async def _resolve_all_media(self, media_decisions: dict) -> list[dict]:
        """Resolve every selected media ref to a fresh URL when possible."""
        if not isinstance(media_decisions, dict):
            return []

        selected = media_decisions.get("selected", [])
        if not isinstance(selected, list):
            return []

        resolved: list[dict] = []
        for media in selected:
            if not isinstance(media, dict):
                continue

            source = media.get("source")
            if source == "url" or (source is None and media.get("url")):
                url = media.get("url")
                if url:
                    content_type = media.get("content_type")
                    resolved.append({
                        "url": str(url),
                        "content_type": str(content_type) if content_type else None,
                        "filename": self._filename_from_media(media, str(url)),
                        "is_image": self._is_image_media(str(url), content_type),
                    })
                continue

            if source not in {"discord_attachment", "discord_embed"}:
                url = media.get("url") or media.get("proxy_url") or media.get("cdn_url")
                if url:
                    content_type = media.get("content_type")
                    resolved.append({
                        "url": str(url),
                        "content_type": str(content_type) if content_type else None,
                        "filename": self._filename_from_media(media, str(url)),
                        "is_image": self._is_image_media(str(url), content_type),
                    })
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
                    if not url:
                        continue
                    content_type = attachment.get("content_type")
                    resolved.append({
                        "url": str(url),
                        "content_type": str(content_type) if content_type else None,
                        "filename": self._filename_from_media(attachment, str(url)),
                        "is_image": self._is_image_media(str(url), content_type),
                    })

                if source == "discord_embed":
                    embed_slot = media.get("embed_slot")
                    if not embed_slot:
                        continue

                    for embed_media in inspected.get("embeds_media", []):
                        if embed_media.get("slot") != embed_slot:
                            continue

                        url = embed_media.get("url")
                        if not url:
                            continue
                        content_type = embed_media.get("content_type")
                        resolved.append({
                            "url": str(url),
                            "content_type": str(content_type) if content_type else None,
                            "filename": self._filename_from_media(embed_media, str(url)),
                            "is_image": self._is_image_media(str(url), content_type),
                        })
                        break
            except Exception:
                continue

        return resolved

    @staticmethod
    def _filename_from_media(media: dict, url: str) -> Optional[str]:
        """Return a readable filename from media metadata or URL."""
        filename = media.get("filename") if isinstance(media, dict) else None
        if filename:
            return str(filename)

        path_name = Path(url.split("?", 1)[0]).name
        return path_name or None

    @staticmethod
    def _safe_media_filename(
        filename: Optional[str],
        content_type: Optional[str],
        index: int,
    ) -> str:
        """Build a Discord-safe attachment filename."""
        raw_name = filename or f"media-{index}"
        safe_name = "".join(
            c if c.isalnum() or c in (".", "_", "-") else "_"
            for c in raw_name
        ).strip("._")
        if not safe_name:
            safe_name = f"media-{index}"

        if "." not in safe_name:
            extension = mimetypes.guess_extension(
                (content_type or "").split(";", 1)[0].strip()
            )
            if extension:
                safe_name = f"{safe_name}{extension}"

        return safe_name

    async def _build_media_files(self, media_items: list[dict]) -> list["discord.File"]:
        """Download small media items and return Discord files, best-effort."""
        if not media_items:
            return []

        files: list[discord.File] = []
        total_bytes = 0
        timeout = aiohttp.ClientTimeout(total=DM_MEDIA_HTTP_TIMEOUT_SECONDS)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for index, media in enumerate(media_items, start=1):
                    if len(files) >= DM_MEDIA_FILE_LIMIT:
                        break

                    url = media.get("url")
                    if not url:
                        continue

                    try:
                        async with session.head(str(url), allow_redirects=True) as resp:
                            if resp.status >= 400:
                                continue
                            content_length = resp.headers.get("Content-Length")
                            content_type = (
                                resp.headers.get("Content-Type")
                                or media.get("content_type")
                            )

                        if not content_length:
                            continue

                        size = int(content_length)
                        if size <= 0 or size > DM_MEDIA_FILE_MAX_BYTES:
                            continue
                        if total_bytes + size > DM_MEDIA_TOTAL_FILE_MAX_BYTES:
                            continue

                        async with session.get(str(url), allow_redirects=True) as resp:
                            if resp.status >= 400:
                                continue
                            data = await resp.read()
                            content_type = (
                                resp.headers.get("Content-Type")
                                or content_type
                                or media.get("content_type")
                            )

                        actual_size = len(data)
                        if (
                            actual_size <= 0
                            or actual_size > DM_MEDIA_FILE_MAX_BYTES
                            or total_bytes + actual_size > DM_MEDIA_TOTAL_FILE_MAX_BYTES
                        ):
                            continue

                        filename = self._safe_media_filename(
                            media.get("filename"),
                            content_type,
                            index,
                        )
                        files.append(discord.File(io.BytesIO(data), filename=filename))
                        total_bytes += actual_size
                    except Exception:
                        continue
        except Exception:
            return []

        return files

    @staticmethod
    def _build_dm_content(media_items: list[dict]) -> str:
        """Build the DM content containing how-to text and media links."""
        lines = [DM_REVIEW_HOW_TO]
        if not media_items:
            return "\n".join(lines)

        lines.append("")
        lines.append("Media:")
        shown_media = media_items[:DM_MEDIA_LINK_LIMIT]
        for index, media in enumerate(shown_media, start=1):
            url = media.get("url")
            if not url:
                continue
            label = "image" if media.get("is_image") else "media"
            content_type = media.get("content_type")
            if content_type:
                label = str(content_type).split(";", 1)[0].strip() or label
            lines.append(f"{index}. {label}: {url}")

        remaining = len(media_items) - len(shown_media)
        if remaining > 0:
            lines.append(f"... {remaining} more media item(s) not shown.")

        return "\n".join(lines)

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
            media_items = await self._resolve_all_media(media_decisions)
            thumb_url = next(
                (
                    str(media["url"])
                    for media in media_items
                    if media.get("is_image") and media.get("url")
                ),
                None,
            )
            if thumb_url:
                embed.set_thumbnail(url=str(thumb_url))

            if source_link:
                embed.set_footer(text=f"Source: {source_link}")

            content = self._build_dm_content(media_items)
            files = await self._build_media_files(media_items)

            # ── send DM ───────────────────────────────────────────────
            send_kwargs = {"content": content, "embed": embed}
            if files:
                send_kwargs["files"] = files
            try:
                msg = await user.send(**send_kwargs)
            except Exception:
                if not files:
                    raise
                self._log.debug(
                    "LiveUpdateSocialService: sending draft DM with media files "
                    "failed for run %s; retrying with links only",
                    run_id,
                    exc_info=True,
                )
                msg = await user.send(content=content, embed=embed)

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

    async def _dm_admin_with_proposals(
        self,
        run_id: str,
        proposals: list,
        media_decisions: dict,
        topic_title: str,
        source_link: str,
    ) -> None:
        """DM the admin with a proposals embed and persist the review_message_id.

        Proposals are idea-level (theme + media strategy), not finished
        drafts — the admin picks which to develop. Persisting
        ``review_message_id`` lets the admin-chat review loop resolve the
        run when the admin replies.
        """
        try:
            if not proposals:
                self._log.warning(
                    "LiveUpdateSocialService: _dm_admin_with_proposals skipped — "
                    "proposals empty for run %s",
                    run_id,
                )
                return

            admin_id = os.getenv("ADMIN_USER_ID")
            if not admin_id:
                self._log.warning(
                    "LiveUpdateSocialService: ADMIN_USER_ID not set — "
                    "cannot DM admin for run %s",
                    run_id,
                )
                return

            user = await self._bot.fetch_user(int(admin_id))

            lines = []
            for idx, idea in enumerate(proposals, start=1):
                if not isinstance(idea, dict):
                    continue
                lines.append(f"**{idx}. {idea.get('theme', '(untitled)')}**")
                strategy = idea.get("media_strategy")
                if strategy:
                    lines.append(f"Media: {strategy}")
                basis = idea.get("media_understanding_basis")
                if basis:
                    lines.append(f"Based on: {basis}")
                rationale = idea.get("rationale")
                if rationale:
                    lines.append(f"Why: {rationale}")
                pattern = idea.get("pattern")
                if pattern and pattern != "custom":
                    lines.append(f"Pattern: {pattern}")
                source_ids = idea.get("source_message_ids") or []
                if source_ids:
                    lines.append(
                        "Source messages: "
                        + ", ".join(f"`{sid}`" for sid in source_ids[:6])
                    )
                lines.append("")

            embed = discord.Embed(
                title=topic_title or "(untitled)",
                description="\n".join(lines).strip(),
                color=0x9B59B6,
            )
            if len(embed.description or "") > 4000:
                embed.description = embed.description[:4000].rstrip() + "\n…(truncated)"
            if source_link:
                embed.set_footer(text=f"Source: {source_link}")

            content = (
                "Post ideas for your review — theme + media strategy each. "
                'Reply with a number (e.g. **"2"**) to develop that idea '
                'into a draft, or **"skip"** to discard.'
            )

            try:
                msg = await user.send(content=content, embed=embed)
            except Exception:
                self._log.exception(
                    "LiveUpdateSocialService: proposal DM send failed for run %s",
                    run_id,
                )
                return

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
                    "LiveUpdateSocialService: proposal DM sent (msg.id=%s) "
                    "but DB write of review_message_id FAILED for run %s",
                    msg.id,
                    run_id,
                )

        except Exception:
            self._log.exception(
                "LiveUpdateSocialService: _dm_admin_with_proposals failed for run %s",
                run_id,
            )

    async def _dm_admin_with_terminal(
        self,
        run_id: str,
        terminal_status: str,
        reason: str,
        topic_title: str,
    ) -> None:
        """DM the admin when a run needs review or failed.

        Persists ``review_message_id`` so the admin-chat loop can resolve
        the run and repair it (retry publish on ``failed``, investigate on
        ``needs_review``).
        """
        try:
            admin_id = os.getenv("ADMIN_USER_ID")
            if not admin_id:
                self._log.warning(
                    "LiveUpdateSocialService: ADMIN_USER_ID not set — "
                    "cannot DM admin for run %s",
                    run_id,
                )
                return

            user = await self._bot.fetch_user(int(admin_id))

            title = "Publish failed" if terminal_status == "failed" else "Needs review"
            color = 0xE74C3C if terminal_status == "failed" else 0xF1C40F
            embed = discord.Embed(
                title=f"{title} — {topic_title or '(untitled)'}",
                description=reason[:3500],
                color=color,
            )
            embed.set_footer(text=f"run {run_id}")

            repair_hint = (
                "The draft is preserved. Reply here to retry the publish, "
                "edit the draft, or discard."
                if terminal_status == "failed"
                else "Reply here to investigate or discard this run."
            )
            try:
                msg = await user.send(
                    content=f"{repair_hint}\n`run_id: {run_id}`",
                    embed=embed,
                )
            except Exception:
                self._log.exception(
                    "LiveUpdateSocialService: terminal DM send failed for run %s",
                    run_id,
                )
                return

            env = os.getenv("ENVIRONMENT", "prod")
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=72)
            ).isoformat()
            ok = self.db_handler.update_live_update_social_run(
                run_id=run_id,
                review_message_id=msg.id,
                expires_at=expires_at,
                environment=env,
            )
            if not ok:
                self._log.error(
                    "LiveUpdateSocialService: terminal DM sent (msg.id=%s) "
                    "but DB write of review_message_id FAILED for run %s",
                    msg.id,
                    run_id,
                )

        except Exception:
            self._log.exception(
                "LiveUpdateSocialService: _dm_admin_with_terminal failed for run %s",
                run_id,
            )
