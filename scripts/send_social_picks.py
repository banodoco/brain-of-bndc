"""Fetch recent live updates, generate social picks, and DM them to admin via Discord API."""
import os
import sys
import json
import asyncio
import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
load_dotenv()

from src.common.db_handler import DatabaseHandler
from src.common.llm import get_llm_response
from src.common.urls import message_jump_url, resolve_thread_ids


GUILD_ID = os.getenv('GUILD_ID', '1076117621407223829')
SUMMARY_CHANNEL_ID = int(os.getenv('SUMMARY_CHANNEL_ID', '1138790297355174039'))
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

SOCIAL_PICKS_PROMPT = """\
You are a social media editor for Banodoco, an open-source AI art community. \
You're reviewing recent live-update feed items to find content worth tweeting from the @banodoco account.

The live-update data includes feed items with titles. You MUST reference items by \
their exact title so we can match your picks back to the original posts and their media.

Look for:
- Exciting new tools, models, or techniques people are discussing
- Impressive generations or art that the community loved
- Notable milestones, releases, or breakthroughs
- Interesting experiments or creative uses of AI tools

For each pick, write a short draft tweet (under 280 chars) that:
- Is enthusiastic but not hype-brained — sounds like a real person, not a brand account
- Credits the creator if there is one
- Explains why it's interesting in plain language
- Would make someone want to click through

Respond with exactly 3 picks in this exact format (no extra text):

PICK
Title: <exact title of the live-update item>
Draft: <tweet text>
Why: <1 sentence on why this is share-worthy>

PICK
Title: <exact title>
Draft: <tweet text>
Why: <1 sentence>

PICK
Title: <exact title>
Draft: <tweet text>
Why: <1 sentence>

If nothing stands out today, respond with just: NOTHING"""


def build_summary_lookup(enriched_summary_str, guild_id, db_handler=None):
    """Legacy daily_summaries lookup retained for explicit backfill workflows."""
    lookup = {}
    try:
        items = json.loads(enriched_summary_str) if isinstance(enriched_summary_str, str) else enriched_summary_str
        if not isinstance(items, list):
            return lookup

        thread_id_by_msg = resolve_thread_ids(
            db_handler,
            (it.get('message_id') for it in items),
        )

        for item in items:
            title = item.get('title', '').strip()
            if not title:
                continue

            channel_id = item.get('channel_id')
            message_id = item.get('message_id')
            discord_link = None
            if guild_id and channel_id and message_id:
                discord_link = message_jump_url(
                    guild_id,
                    channel_id,
                    message_id,
                    thread_id=thread_id_by_msg.get(int(message_id)),
                )

            media_urls = []
            for m in (item.get('mainMediaUrls') or []):
                if isinstance(m, dict) and m.get('url'):
                    media_urls.append(m)
            for sub in item.get('subTopics', []):
                for sub_media_list in (sub.get('subTopicMediaUrls') or []):
                    if isinstance(sub_media_list, list):
                        for m in sub_media_list:
                            if isinstance(m, dict) and m.get('url'):
                                media_urls.append(m)
                    elif isinstance(sub_media_list, dict) and sub_media_list.get('url'):
                        media_urls.append(sub_media_list)

            media_urls.sort(key=lambda m: (0 if m.get('type', '').startswith('video') else 1))

            lookup[title.lower()] = {
                'discord_link': discord_link,
                'all_media': media_urls,
            }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"Failed to build lookup: {e}")
    return lookup


def fetch_live_update_feed_items(db_handler, guild_id, live_channel_id, limit=50):
    rows = db_handler.get_recent_live_update_feed_items(
        guild_id=int(guild_id) if guild_id else None,
        live_channel_id=int(live_channel_id) if live_channel_id else None,
        limit=limit,
    )
    return [row for row in rows if row.get('status') == 'posted']


def build_live_update_lookup(feed_items, guild_id, live_channel_id):
    lookup = {}
    for item in feed_items:
        title = (item.get('title') or '').strip()
        if not title:
            continue
        discord_message_ids = item.get('discord_message_ids') or []
        discord_link = None
        if guild_id and live_channel_id and discord_message_ids:
            discord_link = message_jump_url(guild_id, live_channel_id, discord_message_ids[0])
        lookup[title.lower()] = {
            'discord_link': discord_link,
            'all_media': item.get('media_refs') or [],
        }
    return lookup


async def send_discord_dm(bot_token, user_id, content):
    """Send a DM to a Discord user via the REST API."""
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        # Create/open DM channel
        async with session.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers,
            json={"recipient_id": user_id},
        ) as resp:
            if resp.status != 200:
                print(f"Failed to open DM channel: {resp.status} {await resp.text()}")
                return False
            dm_channel = await resp.json()

        # Send message
        async with session.post(
            f"https://discord.com/api/v10/channels/{dm_channel['id']}/messages",
            headers=headers,
            json={"content": content},
        ) as resp:
            if resp.status != 200:
                print(f"Failed to send message: {resp.status} {await resp.text()}")
                return False
            print(f"Sent DM ({len(content)} chars)")
            return True


async def main():
    print("Fetching recent live-update feed items from Supabase...")
    db = DatabaseHandler(dev_mode=False)
    feed_items = fetch_live_update_feed_items(db, GUILD_ID, SUMMARY_CHANNEL_ID)

    if not feed_items:
        print("No recent posted live-update feed items found.")
        return

    print(f"Got {len(feed_items)} live-update feed items")

    lookup = build_live_update_lookup(feed_items, GUILD_ID, SUMMARY_CHANNEL_ID)
    print(f"Built lookup with {len(lookup)} items")

    print("Calling Claude for social picks...")
    context_items = [
        {
            "title": item.get("title"),
            "body": item.get("body"),
            "update_type": item.get("update_type"),
            "media_refs": item.get("media_refs") or [],
            "discord_message_ids": item.get("discord_message_ids") or [],
            "posted_at": item.get("posted_at") or item.get("created_at"),
        }
        for item in feed_items[:25]
    ]
    context = "Recent live updates:\n" + json.dumps(context_items, ensure_ascii=False)[:8000]
    response = await get_llm_response(
        client_name="claude",
        model="claude-sonnet-4-5-20250929",
        system_prompt=SOCIAL_PICKS_PROMPT,
        messages=[{"role": "user", "content": context}],
        max_tokens=1000,
    )
    response = response.strip()

    if response == "NOTHING":
        print("Claude found nothing worth sharing today.")
        return

    picks = response.split("PICK")
    pick_count = 0

    for pick_text in picks:
        pick_text = pick_text.strip()
        if not pick_text:
            continue

        title = draft = why = ""
        for line in pick_text.split("\n"):
            line = line.strip()
            if line.startswith("Title:"):
                title = line[len("Title:"):].strip()
            elif line.startswith("Draft:"):
                draft = line[len("Draft:"):].strip()
            elif line.startswith("Why:"):
                why = line[len("Why:"):].strip()

        if not draft:
            continue

        # Fuzzy title matching
        matched = {}
        if title:
            title_key = title.lower()
            matched = lookup.get(title_key, {})
            if not matched:
                title_words = set(title_key.split())
                best_overlap, best_match = 0, {}
                for key, val in lookup.items():
                    overlap = len(title_words & set(key.split()))
                    if overlap > best_overlap:
                        best_overlap, best_match = overlap, val
                if best_overlap >= 3:
                    matched = best_match

        discord_link = matched.get('discord_link')
        all_media = matched.get('all_media', [])

        # One pick = one message. Pick the best (video-preferred) media item
        # and put its URL bare on its own line so Discord auto-embeds it. Same
        # for the Discord link. Any text on the same line as a URL kills the
        # embed, so the URLs go on their own lines below the text body.
        dm_parts = [f"**Draft:** {draft}"]
        if why:
            dm_parts.append(f"**Why:** {why}")

        primary_media = all_media[0] if all_media else None
        if discord_link:
            dm_parts.append("")
            dm_parts.append(discord_link)
        if primary_media and primary_media.get('url'):
            dm_parts.append("")
            dm_parts.append(primary_media['url'])

        content = "\n".join(dm_parts)
        print(f"\n--- Pick {pick_count + 1} ---")
        print(content)

        await send_discord_dm(BOT_TOKEN, ADMIN_USER_ID, content)
        pick_count += 1
        await asyncio.sleep(0.5)  # rate limit courtesy

    print(f"\nDone! Sent {pick_count} picks.")


if __name__ == "__main__":
    asyncio.run(main())
