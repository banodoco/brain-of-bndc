"""Cog for the #support forum: every thread is handled by the support agent.

Public-surface safety: the agent runs with a restricted tool allowlist
(see AdminChatAgent.chat's support_turn branch) — no admin-power tools are
reachable from this surface. Silence is failure: any turn error posts a
visible fallback message and mentions the admin.

Config: SUPPORT_CHANNEL_ID env var (forum channel whose threads the agent
answers; defaults to the BNDC support forum 1163250319107555388; set empty
to disable). Documented here rather than .env.example, which is untracked.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from src.common.llm.deepseek_client import DeepSeekClient
from src.features.admin_chat.agent import AdminChatAgent
from src.common.discord_utils import split_message

logger = logging.getLogger('DiscordBot')

SUPPORT_CHANNEL_ID_DEFAULT = "1163250319107555388"

# Support turns run on Ox Alpha via OpenRouter (OpenAI-compatible endpoint).
# Override the slug with SUPPORT_AGENT_MODEL if OpenRouter renames it.
SUPPORT_AGENT_MODEL_DEFAULT = "stealth/ox-alpha"


class OpenRouterClient(DeepSeekClient):
    """DeepSeek wire-format client pointed at OpenRouter.

    Inherits generate_chat_completion unchanged — OpenRouter speaks the
    same OpenAI-compatible protocol; only key and base URL differ.
    """

    def __init__(self) -> None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY not found in environment")
        from openai import AsyncOpenAI
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)


# Threads younger than this may still be picked up by the on_ready catch-up.
CATCHUP_MAX_AGE_SECONDS = 48 * 3600
HISTORY_SEED_LIMIT = 20


class OutcomeView(discord.ui.View):
    """Persistent Resolved / Probably Resolved / Not Resolved buttons.

    Attached to the last message of every support turn. Survives restarts:
    fixed custom_ids, timeout=None, registered once via bot.add_view().
    """

    CHOICES = {
        "support_outcome:resolved": ("Resolved", discord.ButtonStyle.success),
        "support_outcome:probably_resolved": ("Probably Resolved", discord.ButtonStyle.primary),
        "support_outcome:not_resolved": ("Not Resolved", discord.ButtonStyle.danger),
    }

    def __init__(self, cog: "SupportCog"):
        super().__init__(timeout=None)
        self.cog = cog
        for custom_id, (label, style) in self.CHOICES.items():
            button = discord.ui.Button(label=label, style=style, custom_id=custom_id)
            button.callback = self._make_callback(custom_id.rsplit(":", 1)[1])
            self.add_item(button)

    def _make_callback(self, choice: str):
        # NOTE: sync factory returning the coroutine function — assigning an
        # awaitable here would leave discord.py with inert buttons.
        async def callback(interaction: discord.Interaction):
            await self.cog.record_outcome(interaction, choice)
        return callback



# Cost brake: at most this many missed threads answered per catch-up scan.
CATCHUP_MAX_THREADS = 3

# Persona appended to the system prompt as "## Channel Guidance".
# {ADMIN_MENTION} is substituted at call time so ADMIN_USER_ID changes
# never require a restart.
SUPPORT_GUIDANCE = """\
You are the BNDC community support assistant, answering members in the \
#support forum.

- Evidence first: ground answers in real community messages using \
search_hivemind and find_messages. ALWAYS cite the jump URLs you actually \
found; never invent precedent or links.
- Workflow-shaped questions: use comfy_workflow. If a member shares their \
workflow JSON or an attachment, ALWAYS fetch and load it — don't answer from \
the description alone. Diagnose the reported problem against the actual \
graph, then FIX it yourself: apply the edits you are confident about with \
comfy_workflow edit mode rather than only describing what could change.
- Edit mechanics: each mode=edit call applies to a per-thread STAGED working \
copy and is NOT sent to the member — stack as many edits as the fix needs \
(omit source on follow-up edits to keep modifying the staged copy). When \
you are completely done, call mode=deliver EXACTLY ONCE: it attaches the \
finished workflow as a downloadable edited_workflow_<timestamp>.json file \
in this thread. Then tell the member the file is attached, how to open it \
in ComfyUI, and walk through what you changed and why, node by node.
- File exchange: members send workflows by attaching a .json file to their \
post; if they want more changes later, they can attach the file again in a \
new message and you start a fresh staging round.
- If a tool fails (e.g. vibecomfy unavailable), say so plainly, answer from \
evidence you do have, and note the member can re-post to retry later.
- When relevant, onboard members to VibeComfy: \
`pip install vibecomfy --extra-index-url https://nodes.appmana.com/simple/`
- Admit unknowns plainly instead of guessing.
- Hand off to {ADMIN_MENTION} when asked about billing, grants, or payments, \
or whenever you are stuck.
- Tone: concise, warm, no lecturing."""


def build_seed_history(messages: List[Any]) -> List[Dict[str, str]]:
    """Map raw thread messages to LLM history after a restart.

    Member messages become user turns; consecutive bot-authored messages are
    merged into a single assistant turn; attachment-only / empty messages are
    skipped gracefully. `messages` must be in chronological order.
    """
    history: List[Dict[str, str]] = []
    for msg in messages:
        content = (getattr(msg, 'content', None) or "").strip()
        if not content:
            continue
        if getattr(getattr(msg, 'author', None), 'bot', False):
            role = "assistant"
        else:
            role = "user"
        if role == "assistant" and history and history[-1]["role"] == "assistant":
            history[-1]["content"] += "\n\n" + content
        else:
            history.append({"role": role, "content": content})
    return history


class SupportCog(commands.Cog):
    """Auto-agent for the #support forum: threads get answered end-to-end."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_handler = getattr(bot, 'db_handler', None)

        raw = os.getenv("SUPPORT_CHANNEL_ID", SUPPORT_CHANNEL_ID_DEFAULT)
        try:
            self.support_channel_id = int(raw) if raw else None
        except ValueError:
            self.support_channel_id = None

        self.configured = bool(self.support_channel_id and self.db_handler)
        if not self.configured:
            logger.warning("SupportCog: missing config, handlers will no-op")

        # Lazily-initialized shared agent; conversation state lives in
        # AdminChatAgent._conversations keyed by THREAD id.
        self.agent: AdminChatAgent = None

        # In-memory guard against concurrent processing of the same thread.
        self._processing_threads: set = set()

        # on_ready can fire more than once per process (reconnects); run
        # the catch-up scan only the first time.
        self._catchup_done: bool = False

    # ========== Helpers ==========

    def _admin_mention(self) -> str:
        admin_id = os.getenv('ADMIN_USER_ID')
        return f"<@{admin_id}>" if admin_id else "@admin"

    def _build_llm(self):
        """Return (client, model) for support turns: Ox Alpha via OpenRouter.

        Falls back to the AdminChatAgent defaults (DeepSeek) when
        OPENROUTER_API_KEY is unset, so the cog never hard-fails on a
        missing key.
        """
        if not os.getenv("OPENROUTER_API_KEY"):
            logger.warning(
                "[Support] OPENROUTER_API_KEY not set — falling back to "
                "the default admin-chat model for support turns"
            )
            return None, None
        try:
            client = OpenRouterClient()
        except Exception:
            logger.exception("[Support] Failed to build OpenRouter client")
            return None, None
        return client, os.getenv("SUPPORT_AGENT_MODEL", SUPPORT_AGENT_MODEL_DEFAULT)

    def _ensure_agent(self) -> Optional[AdminChatAgent]:
        """Lazily initialize the agent (avoids issues during bot startup)."""
        if self.agent is None:
            try:
                client, model = self._build_llm()
                self.agent = AdminChatAgent(
                    bot=self.bot,
                    db_handler=self.db_handler,
                    sharer=getattr(self.bot, 'sharer', None),
                    client=client,
                    model=model,
                )
            except Exception:
                logger.exception("[Support] Failed to initialize agent")
                return None
        return self.agent

    def _build_context(self, thread: discord.Thread) -> Dict[str, Any]:
        guild_id = str(thread.guild.id) if thread.guild else "unknown"
        environment = "dev" if getattr(self.bot, 'dev_mode', False) else "prod"
        return {
            "source": "support",
            "guild_id": guild_id,
            "channel_id": str(thread.id),
            "channel_name": thread.name,
            "is_thread": True,
            "parent_channel_id": str(thread.parent_id) if thread.parent_id else None,
            "environment": environment,
            "support_turn": True,
            "channel_guidance": SUPPORT_GUIDANCE.replace(
                "{ADMIN_MENTION}", self._admin_mention()),
        }

    async def _seed_conversation_if_needed(self, thread: discord.Thread) -> bool:
        """Rebuild conversation history after a bot restart.

        Returns True when history was seeded (thread unknown to the agent).
        """
        agent = self._ensure_agent()
        if agent is None:
            return False
        if agent.get_conversation(thread.id):
            return False
        ordered = []
        async for msg in thread.history(limit=HISTORY_SEED_LIMIT, oldest_first=False):
            ordered.append(msg)
        ordered.reverse()
        seeded = build_seed_history(ordered)
        agent.get_conversation(thread.id).extend(seeded)
        logger.info(
            "[Support] Seeded %d history message(s) for thread %s",
            len(seeded), thread.id,
        )
        return True
    async def _starter_content(self, thread: discord.Thread) -> tuple:
        """Best-effort text and author of the post that started this thread.

        Returns (content, requester_id); requester_id is None when the
        starter's author cannot be resolved.
        """
        starter = getattr(thread, 'starter_message', None)
        if starter is None:
            try:
                starter = await thread.fetch_message(thread.id)
            except Exception:
                try:
                    async for m in thread.history(limit=1, oldest_first=True):
                        starter = m
                        break
                except Exception:
                    starter = None
        content = (getattr(starter, 'content', None) or "").strip()
        requester_id = getattr(getattr(starter, 'author', None), 'id', None)
        return (content or thread.name), requester_id

    async def _send_fallback(self, thread: discord.Thread):
        """Silence-is-failure: always leave a visible trace on errors."""
        try:
            await thread.send(
                f"Something went wrong on my end — {self._admin_mention()} will follow up."
            )
        except Exception:
            logger.exception("[Support] Fallback message failed for thread %s", thread.id)

    async def _run_turn_guarded(self, thread: discord.Thread, user_message: str,
                                requester_id=None):
        try:
            agent = self._ensure_agent()
            if agent is None:
                await self._send_fallback(thread)
                return
            try:
                await self._seed_conversation_if_needed(thread)
            except Exception:
                logger.exception(
                    "[Support] History seeding failed for thread %s — continuing",
                    thread.id,
                )
            result = await agent.chat(
                user_id=thread.id,
                user_message=user_message,
                channel_context=self._build_context(thread),
                channel=thread,
                requester_id=requester_id,
            )
            chunks = [chunk for reply in (result.replies or [])
                      for chunk in split_message(reply)]
            for i, chunk in enumerate(chunks):
                # Resolution buttons ride on the LAST message of the turn.
                view = self._outcome_view() if chunks and i == len(chunks) - 1 else None
                await thread.send(chunk, view=view)
        except Exception as e:
            logger.error(
                "[Support] Turn failed for thread %s: %s", thread.id, e, exc_info=True
            )
            await self._send_fallback(thread)

    # ========== Listeners ==========

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        """Handle a new post in the support forum."""
        if not self.configured:
            return
        if not thread.parent_id or thread.parent_id != self.support_channel_id:
            return
        if thread.id in self._processing_threads:
            return
        self._processing_threads.add(thread.id)
        try:
            try:
                await thread.join()
            except Exception:
                logger.warning("[Support] Could not join thread %s", thread.id)
            user_message, requester_id = await self._starter_content(thread)
            await self._run_turn_guarded(thread, user_message, requester_id)
        finally:
            self._processing_threads.discard(thread.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle member follow-ups inside support threads."""
        if not self.configured:
            return
        if message.author.bot or not message.guild:
            return
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return
        if not channel.parent_id or channel.parent_id != self.support_channel_id:
            return
        # A new forum post fires BOTH on_thread_create and on_message (the
        # starter message's id equals the thread id). on_thread_create owns
        # the initial turn; the on_ready catch-up covers any misses.
        if message.id == channel.id:
            return
        if channel.id in self._processing_threads:
            return
        self._processing_threads.add(channel.id)
        try:
            await self._run_turn_guarded(channel, message.content,
                                         requester_id=message.author.id)
        finally:
            self._processing_threads.discard(channel.id)

    @commands.Cog.listener()
    async def on_ready(self):
        """Catch-up scan: answer threads that got no bot reply while we were down."""
        if not self.configured:
            return
        # Persistent resolution buttons survive restarts via fixed custom_ids.
        self.bot.add_view(self._outcome_view())
        if self._catchup_done:
            return
        self._catchup_done = True
        try:
            await self._catch_up()
        except Exception as e:
            logger.error(f"SupportCog: catch-up scan failed: {e}", exc_info=True)

    def _outcome_view(self):
        """Build the persistent resolution-button view (stubbed in tests)."""
        return OutcomeView(self)

    async def record_outcome(self, interaction: discord.Interaction, choice: str):
        """Persist a Resolved / Probably Resolved / Not Resolved selection."""
        thread = interaction.channel
        member = interaction.user
        if getattr(member, "bot", False):
            await interaction.response.send_message("Bots can't vote.", ephemeral=True)
            return

        # Acknowledge immediately so a slow persist can't blow Discord's
        # 3-second interaction window ("This interaction failed").
        await interaction.response.defer()

        # Persist (best-effort; the feature degrades to message-edit only if
        # the staged migration has not been applied yet).
        stored = False
        sb = getattr(getattr(self.db_handler, "supabase", None), "table", None)
        if sb is not None:
            try:
                row = {
                    "thread_id": thread.id,
                    "guild_id": getattr(thread.guild, "id", None) or 0,
                    "message_id": interaction.message.id if interaction.message else None,
                    "member_id": member.id,
                    "outcome": choice,
                }
                (sb("support_thread_outcomes")
                 .upsert(row, on_conflict="thread_id")
                 .execute())
                stored = True
            except Exception:
                logger.warning(
                    "[Support] Could not store outcome for thread %s "
                    "(table missing? run the staged migration)", thread.id,
                    exc_info=True,
                )

        # Disable the buttons and show who marked what.
        label = dict(OutcomeView.CHOICES.items())[f"support_outcome:{choice}"][0]
        view = discord.ui.View(timeout=None)
        for custom_id, (text, style) in OutcomeView.CHOICES.items():
            button = discord.ui.Button(
                label=text, style=style, custom_id=custom_id, disabled=True,
            )
            if custom_id == f"support_outcome:{choice}":
                button.label = f"{text} ✓"
            view.add_item(button)
        note = (
            f"Outcome recorded: **{label}** by {member.mention}"
            + ("" if stored else " (not persisted)")
        )

        base = interaction.message.content or ""
        if len(base) + len(note) + 2 <= 2000:
            new_content = f"{base}\n\n{note}" if base else note
        elif base:
            # Truncate the ORIGINAL text to fit — never wipe it.
            allowed = max(0, 2000 - len(note) - 2)
            new_content = base[:allowed].rstrip() + "\n\n" + note
        else:
            new_content = note[:2000]
        try:
            await interaction.message.edit(content=new_content, view=view)
        except Exception:
            logger.exception(
                "[Support] Failed to edit outcome message in thread %s", thread.id,
            )
        try:
            await interaction.followup.send(note, ephemeral=True)
        except Exception:
            pass

    async def _catch_up(self):
        forum = None
        for guild in self.bot.guilds:
            channel = guild.get_channel(self.support_channel_id)
            if isinstance(channel, discord.ForumChannel):
                forum = channel
                break
        if forum is None:
            return

        candidates = list(forum.threads)
        try:
            async for thread in forum.archived_threads(limit=50):
                candidates.append(thread)
        except Exception:
            logger.warning("[Support] Could not list archived threads", exc_info=True)

        now = discord.utils.utcnow()
        # Cost brake: answer at most CATCHUP_MAX_THREADS threads per scan,
        # oldest first so the longest-waiting posts get served.
        candidates.sort(key=lambda t: getattr(t, 'created_at', None) or now)
        answered = 0
        for thread in candidates:
            if thread.archived:
                continue
            created_at = getattr(thread, 'created_at', None)
            if created_at is not None and (now - created_at).total_seconds() > CATCHUP_MAX_AGE_SECONDS:
                continue
            if thread.id in self._processing_threads:
                continue
            self._processing_threads.add(thread.id)
            try:
                agent = self._ensure_agent()
                if agent is None:
                    continue
                # Heuristic: skip threads the bot already engaged with — either
                # we hold a session for it, or its last message was ours.
                if agent.get_conversation(thread.id):
                    continue
                last = None
                async for m in thread.history(limit=1, oldest_first=False):
                    last = m
                if last is not None and getattr(last.author, 'id', None) == self.bot.user.id:
                    continue
                logger.info("[Support] Catch-up: answering missed thread %s", thread.id)
                user_message, requester_id = await self._starter_content(thread)
                await self._run_turn_guarded(thread, user_message, requester_id)
                answered += 1
                if answered >= CATCHUP_MAX_THREADS:
                    break
            except Exception as e:
                logger.error(
                    "[Support] Catch-up error for thread %s: %s", thread.id, e,
                    exc_info=True,
                )
            finally:
                self._processing_threads.discard(thread.id)


async def setup(bot: commands.Bot):
    """Setup function for loading the cog."""
    if getattr(bot, 'db_handler', None) is None:
        logger.error("[Support] Cannot setup cog - db_handler not found on bot")
        return
    await bot.add_cog(SupportCog(bot))
    logger.info("[Support] Cog loaded")
