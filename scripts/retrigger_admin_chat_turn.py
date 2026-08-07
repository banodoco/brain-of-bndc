"""Retrigger a dropped admin-chat turn for a stored Discord message.

Background: `AdminChatCog._handle_admin_message` silently dropped a message
when it arrived while the agent was busy (prod incident 2026-08-06 23:17 UTC).
The message was claimed at receipt, queued as pending, and the drain replay hit
the already-existing claim and skipped it. This script re-runs the agent against
the stored message so the admin gets their reply.

Safety posture (adversarially reviewed):
- The agent's tool surface is RESTRICTED to read-only DB tools + reply/end_turn.
  No write/payment/publish/send_message/edit/delete/media tools are exposed, so a
  user-authored message cannot trigger side effects via the model.
- Guild scope is NOT force-injected (model may search all guilds), but the stored
  author must be the configured admin, else the script fails closed.
- The reply is posted as a REPLY to the original message (message_reference) with
  allowed_mentions fully disabled, rate-limit/5xx retries, and a timeout.
- `--dry-run` prints the reply without posting. No reply / silent turn is treated
  as failure (non-zero exit).

Usage:
    python -m scripts.retrigger_admin_chat_turn <message_id> [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from types import SimpleNamespace

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import aiohttp

from src.common.db_handler import DatabaseHandler
from src.features.admin_chat.agent import AdminChatAgent
from src.features.admin_chat.tools import TOOLS as _FULL_TOOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("retrigger_admin_chat")

DISCORD_API = "https://discord.com/api/v10"

# Read-only DB tools + terminal reply/end_turn. Anything that writes to Discord,
# edits, deletes, publishes, processes payments, or executes media commands is
# deliberately EXCLUDED so a prompt-injected message cannot cause side effects.
_SAFE_TOOL_NAMES = frozenset({
    "reply",
    "end_turn",
    "find_messages",
    "inspect_message",
    "get_active_channels",
    "get_daily_summaries",
    "get_live_update_status",
    "query_table",
    "get_member_info",
    "get_bot_status",
    "search_logs",
    "resolve_user",
    "inspect_social_runs",
    "inspect_social_publication",
    "list_pending_social_drafts",
})

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Canonical failure replies the cog already recognises — imported so the set
# cannot drift (admin_chat_cog.py:22).
from src.features.admin_chat.admin_chat_cog import _ADMIN_FALLBACK_REPLY_LINES as _FALLBACK_REPLY_LINES


def _contains_fallback(reply: str) -> bool:
    """True if a reply is exactly a canonical fallback line or contains one."""
    return any(line in reply for line in _FALLBACK_REPLY_LINES)


def _validate_grounding(actions, target_channel_id: int, *, logger) -> None:
    """Fail-closed, target-specific grounding gate for a "deep DB check" replay.

    To guarantee the reply is grounded in real evidence FROM THE TARGET CHANNEL
    (not a trivial success and not evidence from an unrelated channel), require:
      - >=1 find_messages on the target channel with nonempty returned messages
        whose channel_id matches the target;
      - >=1 successful inspect_message whose inspected message_id is one of the
        messages returned by those target-channel searches, and whose result
        reports the same target channel.
    Forbid find_messages(live=true) and any reply-only turn. A single unrelated
    exploratory failure (e.g. a wrong query_table column) is tolerated; zero real
    target-channel evidence is not.
    """
    read_actions = [
        a for a in (actions or [])
        if a.get("tool") not in {"reply", "end_turn"}
    ]
    if not read_actions:
        logger.error("Turn performed NO read actions; refusing to post a reply-only turn")
        raise SystemExit(1)

    live_uses = [
        a for a in read_actions
        if a.get("tool") == "find_messages" and a.get("input", {}).get("live")
    ]
    if live_uses:
        logger.error("Turn used find_messages(live=true); refusing to post")
        raise SystemExit(1)

    target_str = str(target_channel_id)

    def _msg_in_target_channel(msg) -> bool:
        return str(msg.get("channel_id")) == target_str

    # Target-channel find_messages with nonempty, ALL-target-channel results.
    # A result that mixes channels (or any non-dict message) is REJECTED, not
    # filtered to the target subset — a mixed result cannot be trusted to ground
    # a target-channel reply.
    find_results = []
    for a in read_actions:
        if a.get("tool") != "find_messages":
            continue
        inp = a.get("input", {})
        res = a.get("result", {})
        if str(inp.get("channel_id", "")) != target_str:
            continue
        if res.get("success") is not True:
            continue
        msgs = res.get("messages") or []
        if not msgs:
            continue
        if not all(isinstance(m, dict) and _msg_in_target_channel(m) for m in msgs):
            logger.error(
                "find_messages on channel %s returned a mixed/wrong-channel or "
                "malformed result (%d messages); refusing to post",
                target_channel_id, len(msgs),
            )
            raise SystemExit(1)
        find_results.append((a, msgs))

    if not find_results:
        logger.error(
            "Turn had no find_messages on channel %s with nonempty target-channel "
            "results; refusing to post (reply would be ungrounded). find_messages "
            "calls=%d",
            target_channel_id,
            sum(1 for a in read_actions if a.get("tool") == "find_messages"),
        )
        raise SystemExit(1)

    # The union of target-channel message ids the model could have inspected.
    known_message_ids = {
        str(m.get("message_id"))
        for _, msgs in find_results
        for m in msgs
        if m.get("message_id")
    }

    # A successful inspect_message whose input id is a known target-channel
    # message and whose result confirms the same channel.
    inspected_ok = []
    for a in read_actions:
        if a.get("tool") != "inspect_message":
            continue
        res = a.get("result", {})
        if res.get("success") is not True:
            continue
        inspected_id = str(a.get("input", {}).get("message_id", ""))
        res_msg = res.get("message") or {}
        if not isinstance(res_msg, dict):
            continue
        if (
            inspected_id in known_message_ids
            and str(res_msg.get("channel_id")) == target_str
            and str(res_msg.get("message_id", "")) == inspected_id
        ):
            inspected_ok.append((a, inspected_id))

    if not inspected_ok:
        logger.error(
            "Turn had no successful inspect_message on a target-channel message; "
            "refusing to post"
        )
        raise SystemExit(1)


def _restricted_tools():
    return [t for t in _FULL_TOOLS if t["name"] in _SAFE_TOOL_NAMES]


def _load_message(message_id: int) -> dict:
    """Read the stored message row from discord_messages."""
    from scripts.discord_tools import _supabase
    sb = _supabase()
    rows = (
        sb.table("discord_messages")
        .select("message_id, channel_id, guild_id, author_id, content, created_at, reference_id")
        .eq("message_id", message_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise SystemExit(f"Message {message_id} not found in discord_messages")
    return rows[0]


def _load_parent(parent_id: int) -> dict:
    """Best-effort read of the parent message a reply references."""
    from scripts.discord_tools import _supabase
    sb = _supabase()
    rows = (
        sb.table("discord_messages")
        .select("message_id, author_id, content")
        .eq("message_id", parent_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _configured_admin_ids() -> set:
    ids = set()
    for raw in (os.getenv("ADMIN_USER_ID"), os.getenv("ADMIN_CHAT_ALLOWED_USER_IDS")):
        if not raw:
            continue
        for token in raw.replace(",", " ").split():
            try:
                ids.add(int(token.strip()))
            except (TypeError, ValueError):
                continue
    return ids


def _make_stub_bot(db_handler, bot_user_id: int) -> SimpleNamespace:
    """Minimal bot surface the admin-chat agent touches outside DB tools."""

    async def _fetch_user(user_id):
        return SimpleNamespace(
            id=user_id,
            display_name=f"User{user_id}",
            name=f"User{user_id}",
            bot=False,
        )

    async def _fetch_channel(channel_id):
        ch = SimpleNamespace(
            id=channel_id,
            name="unknown",
            guild=SimpleNamespace(id=1076117621407223829),
            jump_url="",
        )
        # Deliberately a raising stub: no allowed tool reaches channel.send, and
        # if the model somehow bypasses the allowlist we want a loud failure, not
        # an unguarded REST write during the agent phase.
        async def _raise_send(*args, **kwargs):
            raise RuntimeError("channel.send is not available during replay; reply via the reply tool")

        ch.send = _raise_send

        async def _empty_history(limit=100, **kwargs):
            return
            yield

        ch.history = _empty_history
        return ch

    async def _fetch_guild(guild_id):
        return SimpleNamespace(id=guild_id, name="Banodoco")

    bot = SimpleNamespace(
        user=SimpleNamespace(id=bot_user_id),
        db_handler=db_handler,
        guilds=[SimpleNamespace(id=1076117621407223829)],
        start_time=0.0,
        latency=0.0,
        get_guild=lambda gid: SimpleNamespace(id=gid, name="Banodoco"),
        fetch_guild=_fetch_guild,
        get_channel=lambda cid: None,
        fetch_channel=_fetch_channel,
        get_user=lambda uid: None,
        fetch_user=_fetch_user,
        get_cog=lambda name: None,
        is_ready=lambda: True,
    )
    return bot


def _make_rest_send(channel_id: int, *, nonce_seed: str):
    """channel.send() that posts to Discord REST API with retries + safety.

    - Idempotent: each flattened reply chunk gets a stable nonce derived from the
      triggering message + chunk index + content (via sha256, truncated to 25
      chars) with `enforce_nonce: true`, so a retry after an ambiguous 5xx or a
      transport error cannot duplicate, and rerunning the script against the same
      message cannot collide with a prior run.
    - Honors the FULL Retry-After (body or header), never caps it early.
    - Retries transport failures (ClientError/timeout) with the same nonce.
    - fail-closed message_reference: `fail_if_not_exists: true` so we never
      silently post a standalone message if the original was deleted.
    - Fully disables mentions, single session, 30s timeout, Discord-format User-Agent.
    Returns an object with jump_url so tools that inspect the send result do not crash.
    """
    import hashlib

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")

    client_name = os.getenv("BOT_NAME", "BNDC")
    user_agent = (
        f"DiscordBot (https://github.com/banodoco/brain-of-bndc, {client_name}) "
        f"Python/{sys.version_info.major}.{sys.version_info.minor}"
    )
    chunk_counter = 0

    async def _send(content, **kwargs):
        nonlocal chunk_counter
        chunk_counter += 1
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }
        payload = {
            "content": content,
            "allowed_mentions": {"parse": [], "roles": [], "users": [], "replied_user": False},
        }
        message_reference = kwargs.get("message_reference")
        if message_reference:
            payload["message_reference"] = message_reference
        nonce_seed_inner = kwargs.get("nonce") or f"{nonce_seed}:{chunk_counter}:{content}"
        nonce = hashlib.sha256(nonce_seed_inner.encode("utf-8")).hexdigest()[:25]
        payload["nonce"] = nonce
        payload["enforce_nonce"] = True

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(5):
                try:
                    async with session.post(url, headers=headers, json=payload) as resp:
                        if resp.status in (200, 201):
                            data = await resp.json()
                            jump_url = data.get("jump_url") or ""
                            return SimpleNamespace(
                                id=data.get("id", 0),
                                channel_id=channel_id,
                                jump_url=jump_url,
                            )
                        if resp.status == 429:
                            retry_after = 1.0
                            try:
                                body = await resp.json()
                                retry_after = float(body.get("retry_after", retry_after))
                            except Exception:
                                pass
                            header_delay = resp.headers.get("Retry-After")
                            if header_delay:
                                try:
                                    retry_after = max(
                                        retry_after, float(header_delay)
                                    )
                                except (TypeError, ValueError):
                                    pass
                            logger.warning("Discord 429, waiting full %.1fs", retry_after)
                            await asyncio.sleep(max(0.0, retry_after))
                            continue
                        if resp.status in _RETRYABLE_STATUS:
                            logger.warning("Discord %s, retry %d (nonce=%s)", resp.status, attempt + 1, nonce)
                            await asyncio.sleep(2 ** attempt)
                            continue
                        body = await resp.text()
                        raise RuntimeError(f"Discord send failed: {resp.status} {body[:300]}")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning("Discord transport error (nonce=%s): %s; retry %d", nonce, exc, attempt + 1)
                    await asyncio.sleep(2 ** attempt)
                    continue
            raise RuntimeError(f"Discord send failed after retries for channel {channel_id} (nonce={nonce})")
    return _send


async def _post_reply(send, message_id: int, reply: str) -> None:
    """Post one reply string as a reply to the triggering message, splitting on
    '---SPLIT---' and chunking long parts — mirroring the cog's send loop."""
    # Fail-closed: if the original message no longer exists or the stored channel
    # is wrong, Discord refuses instead of silently posting a standalone message.
    message_reference = {"message_id": str(message_id), "fail_if_not_exists": True}
    for part in reply.split("\n---SPLIT---\n"):
        part = part.strip()
        if not part:
            continue
        if len(part) <= 2000:
            if part.strip():
                await send(part, message_reference=message_reference)
                logger.info("Posted reply (len %d)", len(part))
            continue
        for chunk in [part[j:j + 1990] for j in range(0, len(part), 1990)]:
            if chunk.strip():
                await send(chunk, message_reference=message_reference)
                logger.info("Posted reply chunk (len %d)", len(chunk))


async def _run(message_id: int, dry_run: bool, save_reply: str = "", post_artifact: str = "") -> None:
    # Load the DB row and fail closed on identity BEFORE branching: the post
    # path must also validate the configured-admin author and target channel.
    row = _load_message(message_id)
    channel_id = row["channel_id"]
    author_id = row["author_id"]
    guild_id = row.get("guild_id")
    content = row["content"] or ""

    admin_ids = _configured_admin_ids()
    if author_id not in admin_ids:
        raise SystemExit(
            f"Refusing to replay: author {author_id} is not a configured admin "
            f"(ADMIN_USER_ID/ADMIN_CHAT_ALLOWED_USER_IDS = {sorted(admin_ids)}). "
            f"This script only replays admin chat turns."
        )

    # Post mode: load a previously saved + approved artifact and post EXACTLY
    # those replies without re-running the model. The artifact is re-validated
    # (channel match, admin author, fallback lines, target-specific grounding)
    # so a hand-edited file cannot bypass the fail-closed gates.
    if post_artifact:
        if not os.path.exists(post_artifact):
            raise SystemExit(f"Artifact file not found: {post_artifact}")
        with open(post_artifact, "r", encoding="utf-8") as fh:
            artifact = json.load(fh)
        # Strict shape validation: the artifact must be a dict with list-typed
        # replies/actions and string replies — otherwise a malformed artifact
        # (e.g. replies as a JSON string) could be iterated character-by-character
        # and bypass the fallback/grounding gates.
        if not isinstance(artifact, dict):
            raise SystemExit("Artifact is not a JSON object; refusing to post")
        raw_replies = artifact.get("replies")
        raw_actions = artifact.get("actions")
        if not isinstance(raw_replies, list) or not isinstance(raw_actions, list):
            raise SystemExit("Artifact replies/actions must be JSON arrays; refusing to post")
        if not all(isinstance(r, str) for r in raw_replies):
            raise SystemExit("Artifact replies must all be strings; refusing to post")
        if str(artifact.get("message_id")) != str(message_id):
            raise SystemExit(
                f"Artifact message_id {artifact.get('message_id')} != requested {message_id}"
            )
        if str(artifact.get("channel_id")) != str(channel_id):
            raise SystemExit(
                f"Artifact channel_id {artifact.get('channel_id')} != stored row channel {channel_id}"
            )
        replies = [r for r in raw_replies if r and r.strip()]
        if not replies:
            raise SystemExit("Artifact contains no nonempty replies")
        failure_replies = [r for r in replies if _contains_fallback(r)]
        if failure_replies:
            raise SystemExit(f"Artifact contains a fallback/failure reply; refusing to post: {failure_replies!r}")
        try:
            _validate_grounding(raw_actions, channel_id, logger=logger)
        except SystemExit:
            logger.error("Artifact failed grounding revalidation; refusing to post")
            raise
        if dry_run:
            print("\n".join(f"[dry-run-from-artifact] {r}" for r in replies))
            logger.info("Dry run (artifact): would post %d reply message(s) to channel %s",
                        len(replies), channel_id)
            return
        send = _make_rest_send(channel_id, nonce_seed=str(message_id))
        for reply in replies:
            await _post_reply(send, message_id, reply)
        logger.info("Posted %d reply message(s) from artifact to channel %s", len(replies), channel_id)
        return

    bot_user_id = int(os.getenv("BOT_USER_ID", "0"))
    if not bot_user_id:
        raise SystemExit("BOT_USER_ID not set in .env")

    db_handler = DatabaseHandler()

    bot = _make_stub_bot(db_handler, bot_user_id)
    agent = AdminChatAgent(bot=bot, db_handler=db_handler, sharer=None)

    # Strip the bot mention, mirroring AdminChatCog._strip_mention.
    user_message = content.replace(f"<@{bot_user_id}>", "").replace(f"<@!{bot_user_id}>", "").strip()
    if not user_message:
        raise SystemExit(f"Message {message_id} has no content after stripping mention")

    channel_context = {
        "guild_id": str(guild_id) if guild_id else None,
        "channel_id": str(channel_id),
        "channel_name": "minimax_h3_chatter",
    }

    # Reply context, mirroring AdminChatCog._handle_admin_message.
    parent_id = row.get("reference_id")
    if parent_id:
        channel_context["replied_to_message_id"] = str(parent_id)
        parent = _load_parent(parent_id)
        if parent:
            channel_context["replied_to"] = {
                "message_id": str(parent["message_id"]),
                "author": f"User{parent.get('author_id', '')}",
                "content": (parent.get("content") or "")[:500],
            }
            channel_context["replied_to_anchor_note"] = (
                "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."
            )

    channel_context["channel_guidance"] = (
        "You are replaying a previously dropped turn. Use only the read-only DB "
        "tools available to you (find_messages with live=false, query_table, "
        "get_live_update_status, get_active_channels). Do NOT use live=true. "
        "Answer the admin directly via the reply tool."
    )

    # Restrict the agent's tool surface for this run.
    import src.features.admin_chat.agent as agent_mod
    agent_mod.TOOLS = _restricted_tools()

    timeout_seconds = int(os.getenv("RETRIGGER_TIMEOUT_SECONDS", "300"))

    logger.info("Running agent turn for message %s (channel %s) ...", message_id, channel_id)
    try:
        result = await asyncio.wait_for(
            agent.chat(
                user_id=author_id,
                user_message=user_message,
                channel_context=channel_context,
                channel=None,
                requester_id=None,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.error("Agent turn timed out after %ss for message %s", timeout_seconds, message_id)
        raise SystemExit(1)

    replies = [r for r in (result.replies or []) if r and r.strip()]
    if not replies:
        logger.error("Turn ended WITHOUT a reply (silent action). actions=%d", len(result.actions or []))
        raise SystemExit(1)
    failure_replies = [r for r in replies if _contains_fallback(r)]
    if failure_replies:
        logger.error(
            "Agent returned a failure/fallback reply; refusing to post it: %r",
            failure_replies,
        )
        raise SystemExit(1)

    # Fail-closed, target-specific grounding gate (shared with post-artifact).
    _validate_grounding(result.actions, channel_id, logger=logger)

    # Persist the approved artifact so a later live post uses EXACTLY this reply
    # (the model is not re-run; dry-run and post cannot diverge).
    artifact = {
        "message_id": message_id,
        "channel_id": channel_id,
        "replies": replies,
        "actions": result.actions or [],
    }

    if save_reply:
        with open(save_reply, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, ensure_ascii=False, indent=2)
        logger.info("Saved approved reply artifact to %s", save_reply)

    if dry_run:
        print("\n".join(f"[dry-run] {r}" for r in replies))
        logger.info("Dry run: would post %d reply message(s) to channel %s", len(replies), channel_id)
        return

    send = _make_rest_send(channel_id, nonce_seed=str(message_id))
    for reply in replies:
        await _post_reply(send, message_id, reply)

    logger.info("Done. Posted %d reply message(s) to channel %s", len(replies), channel_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrigger a dropped admin-chat turn")
    parser.add_argument("message_id", type=int, help="Discord message_id to replay")
    parser.add_argument("--dry-run", action="store_true", help="Print the reply without posting")
    parser.add_argument(
        "--save-reply", metavar="PATH",
        help="Also save the approved reply artifact (JSON) to PATH",
    )
    parser.add_argument(
        "--post-artifact", metavar="PATH",
        help="Post a previously saved artifact (JSON) without re-running the model",
    )
    args = parser.parse_args()
    if args.dry_run and args.post_artifact:
        # Honor dry-run in artifact mode (preview the artifact instead of posting).
        pass
    asyncio.run(_run(args.message_id, args.dry_run, args.save_reply, args.post_artifact))


if __name__ == "__main__":
    main()
