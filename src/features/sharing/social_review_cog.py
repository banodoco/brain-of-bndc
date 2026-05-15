"""SocialReviewCog — human-review surface for live-update social runs.

Provides ``!social`` commands so operators can inspect, approve, and
audit live-update social runs and publications without accessing the
database directly.

Commands
--------
!social runs [limit]
    List recent ``needs_review`` / ``draft`` runs.

!social inspect <run_id>
    Show full run details (draft_text, media_decisions, publication_outcome,
    trace_entries, linked publications).

!social approve <run_id>
    Force-publish a run by calling the publish handler directly with the
    stored ``draft_text``, bypassing the LLM agent entirely.  Uses a
    constructor-level ``force_publish`` parameter — no ``os.environ`` mutation.

!social publication <publication_id>
    Look up a ``social_publications`` row with ``media_attached`` and
    ``media_missing`` columns.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from src.common.db_handler import DatabaseHandler

from .live_update_social.models import RunState
from .live_update_social.tools import _make_publish_handler, _resolve_bot_user_details

logger = logging.getLogger("DiscordBot")


class SocialReviewCog(commands.Cog):
    """Human-review commands for the live-update social loop."""

    def __init__(
        self,
        bot: commands.Bot,
        db_handler: DatabaseHandler,
    ):
        self.bot = bot
        self.db_handler = db_handler

    # ── !social runs ──────────────────────────────────────────────────

    @commands.hybrid_command(name="social", description="Live-update social review commands")
    async def social_root(self, ctx: commands.Context):
        """Fallback when no subcommand is provided."""
        await ctx.send(
            "Available subcommands:\n"
            "  `!social runs [limit]` — list recent needs_review/draft runs\n"
            "  `!social inspect <run_id>` — full run details\n"
            "  `!social approve <run_id>` — force-publish with stored draft_text\n"
            "  `!social publication <publication_id>` — lookup publication with media outcome\n"
        )

    @commands.hybrid_command(name="social_runs", description="List recent needs_review/draft social runs")
    async def social_runs(
        self,
        ctx: commands.Context,
        limit: Optional[int] = 10,
    ):
        """List recent ``needs_review`` or ``draft`` live-update social runs."""
        guild_id = ctx.guild.id if ctx.guild else None
        limit = max(1, min(limit or 10, 50))

        # Fetch both statuses
        needs_review = self.db_handler.get_recent_social_runs(
            guild_id=guild_id,
            terminal_status="needs_review",
            limit=limit,
        )
        drafts = self.db_handler.get_recent_social_runs(
            guild_id=guild_id,
            terminal_status="draft",
            limit=limit,
        )

        # Merge and sort, deduplicating by run_id
        seen: set = set()
        merged: list = []
        for row in sorted(
            needs_review + drafts,
            key=lambda r: r.get("created_at") or "",
            reverse=True,
        ):
            rid = row.get("run_id")
            if rid and rid not in seen:
                seen.add(rid)
                merged.append(row)

        merged = merged[:limit]

        if not merged:
            await ctx.send("No `needs_review` or `draft` runs found.")
            return

        lines = ["**Recent needs_review / draft runs:**"]
        for row in merged:
            rid = row.get("run_id", "?")
            status = row.get("terminal_status", "?")
            topic = row.get("topic_id", "?")
            platform = row.get("platform", "?")
            created = row.get("created_at", "?")
            draft_preview = (row.get("draft_text") or "")[:80]
            lines.append(
                f"• `{rid}` — **{status}** — `{platform}` — topic `{topic}`\n"
                f"  _{created}_ — \"{draft_preview}...\""
                if len(draft_preview) >= 80
                else f"  _{created}_ — \"{draft_preview}\""
            )

        await ctx.send("\n".join(lines))

    # ── !social inspect ────────────────────────────────────────────────

    @commands.hybrid_command(name="social_inspect", description="Show full details for a social run")
    async def social_inspect(self, ctx: commands.Context, run_id: str):
        """Inspect a live-update social run by run_id."""
        row = self.db_handler.get_live_update_social_run(run_id)
        if not row:
            await ctx.send(f"No run found with ID `{run_id}`.")
            return

        run_state = RunState.from_row(row)

        # Linked publications
        publications = self.db_handler.get_social_publications_by_run_id(run_id)
        pub_lines = []
        for p in publications:
            pub_lines.append(
                f"  • `{p.get('publication_id')}` — status={p.get('status')} "
                f"platform={p.get('platform')} action={p.get('action')}\n"
                f"    provider_ref={p.get('provider_ref')} "
                f"media_attached={p.get('media_attached')} "
                f"media_missing={p.get('media_missing')}"
            )

        outcome = run_state.publication_outcome
        outcome_str = (
            f"success={outcome.success}, provider_ref={outcome.provider_ref}, "
            f"failure_reason={outcome.failure_reason}, "
            f"media_ids={outcome.media_ids}, "
            f"media_attached={outcome.media_attached}, "
            f"media_missing={outcome.media_missing}, "
            f"per_item_outcomes={outcome.per_item_outcomes}"
            if outcome
            else "None"
        )

        trace_count = len(run_state.trace_entries)
        last_traces = run_state.trace_entries[-5:] if run_state.trace_entries else []

        msg = (
            f"**Run `{run_id}`**\n"
            f"• topic_id: `{run_state.topic_id}`\n"
            f"• platform: `{run_state.platform}` — action: `{run_state.action}` — mode: `{run_state.mode}`\n"
            f"• terminal_status: `{run_state.terminal_status}`\n"
            f"• created_at: `{run_state.created_at}`\n"
            f"• draft_text: ```{run_state.draft_text or '(none)'}```\n"
            f"• media_decisions: ```{str(run_state.media_decisions)[:500]}```\n"
            f"• publication_outcome: ```{outcome_str[:500]}```\n"
            f"• trace_entries ({trace_count} total, last 5 shown):\n"
        )
        for t in last_traces:
            msg += f"  - `{t.get('event')}` @ {t.get('ts')}: {str({k: v for k, v in t.items() if k not in ('event', 'ts')})[:200]}\n"

        if pub_lines:
            msg += "\n**Linked publications:**\n" + "\n".join(pub_lines[:10])

        # Discord message limit safety
        if len(msg) > 1900:
            msg = msg[:1900] + "\n... (truncated)"

        await ctx.send(msg)

    # ── !social approve ────────────────────────────────────────────────

    @commands.hybrid_command(name="social_approve", description="Force-publish a social run by run_id")
    async def social_approve(self, ctx: commands.Context, run_id: str):
        """Approve and force-publish a run.

        Bypasses the LLM agent entirely — loads the stored ``draft_text``
        and calls the publish handler directly with ``force_publish=True``.
        Does **not** mutate ``os.environ``.
        """
        row = self.db_handler.get_live_update_social_run(run_id)
        if not row:
            await ctx.send(f"No run found with ID `{run_id}`.")
            return

        run_state = RunState.from_row(row)
        draft_text = row.get("draft_text") or ""
        if not draft_text:
            await ctx.send(f"Run `{run_id}` has no draft_text — cannot publish.")
            return

        # Check if already published
        if run_state.terminal_status == "published":
            await ctx.send(
                f"Run `{run_id}` is already **published**.\n"
                f"Use `!social inspect {run_id}` for details."
            )
            return

        social_publish_service = getattr(self.bot, "social_publish_service", None)
        if social_publish_service is None:
            await ctx.send("SocialPublishService is not available on the bot. Cannot publish.")
            return

        await ctx.send(f"⏳ Force-publishing run `{run_id}`... (bypassing LLM agent)")

        # Build a publish handler with force_publish=True (no os.environ mutation)
        publish_handler_fn = _make_publish_handler(
            db_handler=self.db_handler,
            social_publish_service=social_publish_service,
            bot=self.bot,
            force_publish=True,
        )

        # Build params from the stored run data
        params = {
            "draft_text": draft_text,
            "selected_media": run_state.media_decisions.get("selected", []),
            "skip_media_understanding": False,
        }

        try:
            result = await publish_handler_fn(run_state, params)
        except Exception as e:
            logger.error(
                "social_approve: publish handler failed for run %s: %s",
                run_id, e, exc_info=True,
            )
            await ctx.send(f"❌ Publish handler failed: {e}")
            return

        if result.get("ok"):
            pub_id = result.get("publication_id") or result.get("publication_ids")
            provider_ref = result.get("provider_ref") or result.get("provider_refs")
            duplicate = result.get("duplicate_prevented")
            thread_success = result.get("thread_success")

            lines = [f"✅ Run `{run_id}` published successfully."]
            if duplicate:
                lines.append(f"⚠️ Duplicate prevented: {result.get('note', '')}")
            if pub_id:
                lines.append(f"• publication_id: `{pub_id}`")
            if provider_ref:
                lines.append(f"• provider_ref: `{provider_ref}`")
            if thread_success is not None:
                lines.append(f"• thread_success: {thread_success}")
            lines.append(f"Use `!social inspect {run_id}` for full details.")
            await ctx.send("\n".join(lines))
        else:
            error = result.get("error", "Unknown error")
            await ctx.send(f"❌ Publish failed for run `{run_id}`: {error}")

    # ── !social publication ────────────────────────────────────────────

    @commands.hybrid_command(name="social_publication", description="Look up a social publication by ID")
    async def social_publication(self, ctx: commands.Context, publication_id: str):
        """Look up a ``social_publications`` row with media outcome columns."""
        guild_id = ctx.guild.id if ctx.guild else None
        pub = self.db_handler.get_social_publication_by_id(publication_id, guild_id=guild_id)
        if not pub:
            await ctx.send(f"No publication found with ID `{publication_id}`.")
            return

        media_attached = pub.get("media_attached")
        media_missing = pub.get("media_missing")
        request_payload = pub.get("request_payload") or {}

        msg = (
            f"**Publication `{publication_id}`**\n"
            f"• status: `{pub.get('status')}`\n"
            f"• platform: `{pub.get('platform')}` — action: `{pub.get('action')}`\n"
            f"• provider_ref: `{pub.get('provider_ref')}`\n"
            f"• provider_url: `{pub.get('provider_url')}`\n"
            f"• created_at: `{pub.get('created_at')}`\n"
            f"• media_attached: ```{str(media_attached)[:400]}```\n"
            f"• media_missing: ```{str(media_missing)[:400]}```\n"
            f"• request_payload.text: ```{str(request_payload.get('text', ''))[:300]}```\n"
        )

        if len(msg) > 1900:
            msg = msg[:1900] + "\n... (truncated)"

        await ctx.send(msg)


async def setup(bot: commands.Bot):
    """Discord.py auto-loading entry point."""
    if not hasattr(bot, "db_handler"):
        logger.error("Database handler not found on bot object. Cannot load SocialReviewCog.")
        return

    try:
        cog_instance = SocialReviewCog(bot, bot.db_handler)
        await bot.add_cog(cog_instance)
        logger.info("SocialReviewCog loaded.")
    except Exception as e:
        logger.error("Failed to load SocialReviewCog: %s", e, exc_info=True)
