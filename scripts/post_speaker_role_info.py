"""
Create/update the pinned "Want to get a Speaker role?" thread in the support forum.

The bot's automatic KEEP reply in #introductions can say things like "now you're
a Speaker" — that's just the welcome copy, not the role grant. Only a human
reviewing the intro grants Speaker. This pinned thread tells members that
plainly, so they don't sit waiting for a role the bot implied they already have.

Idempotent: finds the bot-owned thread by name and edits the starter message in
place, or creates it fresh. Pinned, not locked — members can reply asking for
help, and the pin keeps it at the top of the forum.

Usage:
    python scripts/post_speaker_role_info.py          # dry run
    python scripts/post_speaker_role_info.py --send   # create/update for real
"""

import asyncio
import argparse
import os

from dotenv import load_dotenv
load_dotenv()

import discord

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SUPPORT_CHANNEL_ID = 1163250319107555388  # #support forum

THREAD_NAME = "Want to get a Speaker role?"

THREAD_CONTENT = """## Want to get a Speaker role?

It's a simple two-part ask. Post **both** in <#1138861011206688829>:

• **A short intro.** Who are you? What are you interested in? Which models, tools, or workflows are you working with?
• **Something you've made.** A link, image, video, workflow, or rough experiment — it doesn't need to be polished.

**A human will review your intro as soon as possible after you share both** — once it's approved you'll get the Speaker role. You don't need to do anything else — just wait for the human review.

More info in <#1478500886283030710>."""


async def find_bot_thread(forum, bot_id):
    """Find the bot-owned thread with THREAD_NAME (active or archived)."""
    for thread in forum.threads:
        if thread.name == THREAD_NAME:
            return thread
    async for thread in forum.archived_threads(limit=50):
        if thread.name == THREAD_NAME:
            return thread
    return None


async def main(send: bool):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(SUPPORT_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(SUPPORT_CHANNEL_ID)

            if not isinstance(channel, discord.ForumChannel):
                print(f"Error: #{channel.name} is not a forum channel")
                return

            print(f"Found support forum: #{channel.name}")

            existing = await find_bot_thread(channel, client.user.id)
            if existing:
                # Edit the starter message in place (idempotent re-runs).
                starter = await existing.fetch_message(existing.id)
                if starter.content == THREAD_CONTENT:
                    print(f"Thread '{THREAD_NAME}' unchanged, skipping")
                else:
                    print(f"Updating thread '{THREAD_NAME}' ({existing.id})")
                    if send:
                        await starter.edit(content=THREAD_CONTENT)
                        await existing.edit(pinned=True)
                        print("  Edited and pinned.")
                return

            print(f"{'Creating' if send else 'Would create'} forum thread: \"{THREAD_NAME}\"")
            if send:
                result = await channel.create_thread(
                    name=THREAD_NAME,
                    content=THREAD_CONTENT,
                )
                thread = result.thread if hasattr(result, 'thread') else result
                await thread.edit(pinned=True)
                print(f"  Created pinned thread: {thread.id}")
            else:
                print(f"  Starter: {THREAD_CONTENT[:80]}...")

            print(f"\n{'Done!' if send else 'Dry run complete. Use --send to execute.'}")
        finally:
            await client.close()

    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create/update the pinned speaker-role info thread in #support")
    parser.add_argument("--send", action="store_true", help="Actually create/update (default is dry run)")
    args = parser.parse_args()

    asyncio.run(main(send=args.send))
