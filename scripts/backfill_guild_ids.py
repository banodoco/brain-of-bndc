#!/usr/bin/env python3
"""
Backfill guild_id on existing tables and seed server_config / guild_members / server_content.

Safe to run multiple times (idempotent). All updates use WHERE guild_id IS NULL
so already-tagged rows are skipped.

Note: daily_summaries is legacy daily-summary history/backfill input. Active
live-update editor state lives in live_update_* tables and is intentionally not
blanket-retagged by this historical guild-id backfill script.

Usage:
    python scripts/backfill_guild_ids.py [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('BackfillGuildIds')

PROJECT_ROOT = Path(__file__).parent.parent
DISCORD_API_BASE = "https://discord.com/api/v10"


def get_client():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(url, key)


def get_discord_token() -> str:
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN must be set for Discord API backfill steps")
    return token


def _channel_type_name(channel_type: int) -> str:
    mapping = {
        0: 'text',
        2: 'voice',
        4: 'category',
        5: 'news',
        10: 'thread',
        11: 'thread',
        12: 'thread',
        13: 'stage',
        15: 'forum',
    }
    return mapping.get(channel_type, 'unknown')


async def _discord_get_json(session: aiohttp.ClientSession, path: str, params: dict | None = None):
    async with session.get(f"{DISCORD_API_BASE}{path}", params=params) as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_guild_channels(token: str, guild_id: int):
    headers = {'Authorization': f'Bot {token}'}
    async with aiohttp.ClientSession(headers=headers) as session:
        channels = await _discord_get_json(session, f"/guilds/{guild_id}/channels")
        try:
            active_threads = await _discord_get_json(session, f"/guilds/{guild_id}/threads/active")
        except aiohttp.ClientResponseError:
            active_threads = {}
    thread_rows = active_threads.get('threads', []) if isinstance(active_threads, dict) else []
    seen_ids = {int(channel['id']) for channel in channels}
    for thread in thread_rows:
        if int(thread['id']) not in seen_ids:
            channels.append(thread)
    return channels


async def _fetch_guild_members(token: str, guild_id: int):
    headers = {'Authorization': f'Bot {token}'}
    members = []
    after = 0
    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            batch = await _discord_get_json(
                session,
                f"/guilds/{guild_id}/members",
                params={'limit': 1000, 'after': after},
            )
            if not batch:
                break
            members.extend(batch)
            after = int(batch[-1]['user']['id'])
            if len(batch) < 1000:
                break
    return members


# ==========================================================================
# Step 0: Safety verification
# ==========================================================================

def verify_no_foreign_guild_data(sb, guild_id: int):
    """Check that no rows already have a guild_id OTHER than the BNDC one.
    If they do, abort — the backfill would incorrectly re-tag them."""
    legacy_summary_tables = ['daily_summaries']
    tables = ['discord_messages', 'discord_channels', *legacy_summary_tables,
              'shared_posts', 'pending_intros', 'discord_reactions', 'discord_reaction_log']
    for table in tables:
        try:
            result = (
                sb.table(table)
                .select('guild_id', count='exact')
                .not_.is_('guild_id', 'null')
                .neq('guild_id', guild_id)
                .limit(1)
                .execute()
            )
            count = result.count or 0
            if count > 0:
                logger.error(f"ABORT: {table} has {count} rows with guild_id != {guild_id}. "
                             f"Cannot safely blanket-assign BNDC guild_id.")
                sys.exit(1)
        except Exception as e:
            logger.warning(f"Could not verify {table}: {e}")
    logger.info("Safety check passed: no foreign guild data found")


# ==========================================================================
# Step 1: Tag existing rows with BNDC guild_id
# ==========================================================================

def backfill_table(sb, table: str, guild_id: int, dry_run: bool, batch_size: int = 5000):
    """Set guild_id on all rows where it's NULL."""
    total = 0
    pk_col = _get_pk_column(table)

    while True:
        result = (
            sb.table(table)
            .select('*', count='exact')
            .is_('guild_id', 'null')
            .limit(1)
            .execute()
        )
        remaining = result.count or 0
        if remaining == 0:
            break

        if dry_run:
            logger.info(f"  [DRY RUN] {table}: {remaining} rows would be updated")
            return remaining

        ids_result = (
            sb.table(table)
            .select(pk_col)
            .is_('guild_id', 'null')
            .limit(batch_size)
            .execute()
        )
        ids = [row[pk_col] for row in (ids_result.data or [])]
        if not ids:
            break

        sb.table(table).update({'guild_id': guild_id}).in_(pk_col, ids).execute()
        total += len(ids)
        logger.info(f"  {table}: updated {total} rows so far ({remaining} were remaining)")

    if not dry_run:
        logger.info(f"  {table}: {total} rows updated total")
    return total


def _get_pk_column(table: str) -> str:
    pk_map = {
        'discord_messages': 'message_id',
        'discord_channels': 'channel_id',
        'daily_summaries': 'daily_summary_id',  # legacy summary history/backfill input
        'shared_posts': 'id',
        'pending_intros': 'id',
        'discord_reactions': 'message_id',  # composite PK; works for IN filter
        'discord_reaction_log': 'id',
    }
    return pk_map.get(table, 'id')


# ==========================================================================
# Step 2: Backfill reaction guild_id via JOIN on message_id
# ==========================================================================

def backfill_reaction_guild_ids(sb, dry_run: bool, batch_size: int = 5000):
    """Set guild_id on discord_reactions/discord_reaction_log rows by looking up
    the message's guild_id. This catches reactions where the message was tagged
    but the reaction wasn't (e.g. pre-existing reactions)."""
    for table in ('discord_reactions', 'discord_reaction_log'):
        logger.info(f"  Backfilling {table} guild_id from messages...")
        total = 0
        while True:
            # Find reaction rows with NULL guild_id
            result = (
                sb.table(table)
                .select('message_id', count='exact')
                .is_('guild_id', 'null')
                .limit(batch_size)
                .execute()
            )
            remaining = result.count or 0
            if remaining == 0:
                break

            if dry_run:
                logger.info(f"    [DRY RUN] {table}: {remaining} rows would be updated")
                break

            message_ids = list({row['message_id'] for row in (result.data or [])})
            if not message_ids:
                break

            # Look up guild_ids from messages
            msg_result = (
                sb.table('discord_messages')
                .select('message_id, guild_id')
                .in_('message_id', message_ids)
                .not_.is_('guild_id', 'null')
                .execute()
            )
            guild_map = {row['message_id']: row['guild_id'] for row in (msg_result.data or [])}

            # Update reactions by message_id batches (grouped by guild_id)
            from collections import defaultdict
            by_guild = defaultdict(list)
            for mid, gid in guild_map.items():
                by_guild[gid].append(mid)

            for gid, mids in by_guild.items():
                for i in range(0, len(mids), 500):
                    batch = mids[i:i + 500]
                    (
                        sb.table(table)
                        .update({'guild_id': gid})
                        .in_('message_id', batch)
                        .is_('guild_id', 'null')
                        .execute()
                    )
                    total += len(batch)

            logger.info(f"    {table}: updated {total} rows so far")

            # Safety: if no guild_ids found for any messages, break to avoid infinite loop
            if not guild_map:
                logger.warning(f"    {table}: {remaining} rows have no message with guild_id, skipping")
                break

        if not dry_run:
            logger.info(f"    {table}: {total} rows updated total")


# ==========================================================================
# Step 3: guild_members from discord_members
# ==========================================================================

def backfill_guild_members(sb, guild_id: int, dry_run: bool):
    """Copy nick/join/roles from discord_members to guild_members for BNDC."""
    offset = 0
    batch_size = 500
    total = 0

    while True:
        result = (
            sb.table('members')
            .select('member_id, server_nick, guild_join_date, role_ids')
            .range(offset, offset + batch_size - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break

        if dry_run:
            logger.info(f"  [DRY RUN] guild_members: would upsert {len(rows)} rows (batch at offset {offset})")
        else:
            guild_rows = [{
                'guild_id': guild_id,
                'member_id': r['member_id'],
                'server_nick': r.get('server_nick'),
                'guild_join_date': r.get('guild_join_date'),
                'role_ids': r.get('role_ids'),
            } for r in rows]
            sb.table('guild_members').upsert(guild_rows).execute()
            total += len(guild_rows)
            logger.info(f"  guild_members: upserted {total} rows so far")

        if len(rows) < batch_size:
            break
        offset += batch_size

    if not dry_run:
        logger.info(f"  guild_members: {total} rows upserted total")


def seed_guild_members_from_discord(sb, guild_id: int, members: list, dry_run: bool, batch_size: int = 500):
    """Seed guild_members directly from Discord API member payloads."""
    if dry_run:
        logger.info(f"  [DRY RUN] guild_members[{guild_id}]: would upsert {len(members)} rows from Discord API")
        return

    total = 0
    rows = []
    for member in members:
        user = member.get('user') or {}
        member_id = user.get('id')
        if not member_id:
            continue
        rows.append({
            'guild_id': guild_id,
            'member_id': int(member_id),
            'server_nick': member.get('nick'),
            'guild_join_date': member.get('joined_at'),
            'role_ids': [int(role_id) for role_id in (member.get('roles') or [])],
        })

    for index in range(0, len(rows), batch_size):
        batch = rows[index:index + batch_size]
        sb.table('guild_members').upsert(batch).execute()
        total += len(batch)

    logger.info(f"  guild_members[{guild_id}]: seeded {total} rows from Discord API")


# ==========================================================================
# Step 4: Seed server_config
# ==========================================================================

def seed_server_config(sb, guild_id: int, dry_run: bool, force: bool = False):
    """Seed server_config for BNDC from env vars."""
    existing = sb.table('server_config').select('*').eq('guild_id', guild_id).limit(1).execute()
    existing_row = existing.data[0] if existing.data else None

    def _int(key):
        v = os.getenv(key)
        return int(v) if v else None

    def _int_list(key):
        v = os.getenv(key, '')
        return [int(x.strip()) for x in v.split(',') if x.strip()] if v else None

    def _monitor_config():
        raw_value = os.getenv('CHANNELS_TO_MONITOR', '').strip()
        if not raw_value:
            return False, None
        if raw_value.lower() == 'all':
            return True, None
        return False, [int(x.strip()) for x in raw_value.split(',') if x.strip()]

    def _reactor_config():
        watchlist_str = os.getenv('REACTION_WATCHLIST', '[]')
        try:
            raw_rules = json.loads(watchlist_str)
        except json.JSONDecodeError:
            logger.warning("  server_config: REACTION_WATCHLIST is invalid JSON; skipping reactor config seed")
            return None, None

        if not isinstance(raw_rules, list):
            logger.warning("  server_config: REACTION_WATCHLIST is not a list; skipping reactor config seed")
            return None, None

        reaction_rules = []
        linker_channels = set()
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            if rule.get('feature') == 'message_linker' and 'channel_id' in rule:
                raw_channel_id = rule.get('channel_id')
                if raw_channel_id not in (None, '', '*'):
                    try:
                        linker_channels.add(int(raw_channel_id))
                    except (TypeError, ValueError):
                        logger.warning(f"  server_config: invalid message_linker channel ID {raw_channel_id!r}; skipping")
                continue
            reaction_rules.append(rule)

        return reaction_rules, sorted(linker_channels) or None

    monitor_all_channels, monitored_channel_ids = _monitor_config()
    reaction_watchlist, message_linker_channels = _reactor_config()

    config = {
        'guild_id': guild_id,
        'guild_name': 'BNDC',
        'enabled': True,
        'write_enabled': True,
        'default_logging': True,
        'default_archiving': True,
        'default_summarising': True,
        'default_reactions': True,
        'default_sharing': True,
        'community_name': 'BNDC',
        'community_description': 'Brave New Digital Creatives',
        'community_demonym': 'BNDCer',
        'admin_user_id': _int('ADMIN_USER_ID'),
        'summary_channel_id': _int('SUMMARY_CHANNEL_ID'),
        'top_gens_channel_id': _int('TOP_GEN_CHANNEL'),
        'art_channel_id': _int('ART_CHANNEL_ID'),
        'gate_channel_id': _int('GATE_CHANNEL_ID'),
        'intro_channel_id': _int('INTRO_CHANNEL_ID'),
        'welcome_channel_id': _int('WELCOME_CHANNEL_ID'),
        'grants_channel_id': _int('GRANTS_CHANNEL_ID'),
        'moderation_channel_id': _int('MODERATION_CHANNEL_ID'),
        'openmuse_channel_id': _int('OPENMUSE_CHANNEL_ID'),
        'speaker_role_id': _int('SPEAKER_ROLE_ID'),
        'approver_role_id': _int('APPROVER_ROLE_ID'),
        'super_approver_role_id': _int('SUPER_APPROVER_ROLE_ID'),
        'no_sharing_role_id': _int('NO_SHARING_ROLE_ID'),
        'announcement_channel_id': _int('ANNOUNCEMENT_CHANNEL_ID'),
        'twitter_account': os.getenv('TWITTER_ACCOUNT'),
        'solana_wallet': os.getenv('SOLANA_WALLET'),
        'curator_ids': _int_list('CURATOR_IDS'),
        'speaker_management_enabled': True,
        'monitor_all_channels': monitor_all_channels,
        'monitored_channel_ids': monitored_channel_ids,
        'reaction_watchlist': reaction_watchlist,
        'message_linker_channels': message_linker_channels,
    }

    if dry_run:
        action = "update missing BNDC fields on existing row" if existing_row else "insert BNDC config"
        logger.info(f"  [DRY RUN] server_config: would {action}")
        return

    if existing_row:
        if force:
            updates = {
                key: value
                for key, value in config.items()
                if value is not None and existing_row.get(key) != value
            }
        else:
            updates = {
                key: value
                for key, value in config.items()
                if existing_row.get(key) is None and value is not None
            }
        if updates:
            sb.table('server_config').update(updates).eq('guild_id', guild_id).execute()
            logger.info(f"  server_config: filled {len(updates)} missing BNDC field(s) on existing row")
        else:
            logger.info("  server_config: BNDC row already has required fields, skipping")
    else:
        sb.table('server_config').insert(config).execute()
        logger.info(f"  server_config: BNDC config seeded")


def seed_archive_only_server_config(sb, guild_id: int, guild_name: str, dry_run: bool, force: bool = False):
    """Seed a second guild with archive-only defaults."""
    existing = sb.table('server_config').select('*').eq('guild_id', guild_id).limit(1).execute()
    existing_row = existing.data[0] if existing.data else None

    config = {
        'guild_id': guild_id,
        'guild_name': guild_name,
        'enabled': True,
        'write_enabled': True,
        'default_logging': False,
        'default_archiving': True,
        'default_summarising': False,
        'default_reactions': True,
        'default_sharing': False,
        'speaker_management_enabled': False,
        'monitor_all_channels': True,
        'monitored_channel_ids': None,
        'reaction_watchlist': [],
        'message_linker_channels': [],
    }

    if dry_run:
        action = "update missing fields" if existing_row else "insert archive-only config"
        logger.info(f"  [DRY RUN] server_config[{guild_id}]: would {action} for {guild_name}")
        return

    if existing_row:
        if force:
            updates = {
                key: value
                for key, value in config.items()
                if value is not None and existing_row.get(key) != value
            }
        else:
            updates = {
                key: value
                for key, value in config.items()
                if existing_row.get(key) is None and value is not None
            }
        if updates:
            sb.table('server_config').update(updates).eq('guild_id', guild_id).execute()
            logger.info(f"  server_config[{guild_id}]: filled {len(updates)} missing field(s) for {guild_name}")
        else:
            logger.info(f"  server_config[{guild_id}]: existing row already has required fields")
    else:
        sb.table('server_config').insert(config).execute()
        logger.info(f"  server_config[{guild_id}]: seeded archive-only config for {guild_name}")


# ==========================================================================
# Step 5: Seed server_content from posts/*.md
# ==========================================================================

def seed_server_content(sb, guild_id: int, dry_run: bool):
    """Seed server_content from posts/*.md files."""
    existing = sb.table('server_content').select('content_key').eq('guild_id', guild_id).execute()
    existing_keys = {row['content_key'] for row in (existing.data or [])}

    posts_dir = PROJECT_ROOT / 'posts'
    if not posts_dir.exists():
        logger.info(f"  server_content: no posts/ directory found, skipping")
        return

    seeded = 0
    for md_file in sorted(posts_dir.glob('*.md')):
        content_key = f"post_{md_file.stem}"
        if content_key in existing_keys:
            continue

        content = md_file.read_text(encoding='utf-8')
        if dry_run:
            logger.info(f"  [DRY RUN] server_content: would insert {content_key} ({len(content)} chars)")
        else:
            sb.table('server_content').upsert({
                'guild_id': guild_id,
                'content_key': content_key,
                'content': content,
                'content_type': 'post',
            }).execute()
            seeded += 1

    if not dry_run:
        logger.info(f"  server_content: seeded {seeded} posts")


# ==========================================================================
# Step 6: Discord API backfill for guild-specific channels/messages/metadata
# ==========================================================================

def upsert_channel_metadata_from_discord(sb, guild_id: int, channels: list, dry_run: bool, batch_size: int = 500):
    rows = []
    for channel in channels:
        rows.append({
            'channel_id': int(channel['id']),
            'channel_name': channel.get('name') or f"channel-{channel['id']}",
            'guild_id': guild_id,
            'channel_type': _channel_type_name(channel.get('type')),
            'parent_id': int(channel['parent_id']) if channel.get('parent_id') else None,
            'category_id': int(channel['parent_id']) if channel.get('type') not in (4, 10, 11, 12) and channel.get('parent_id') else None,
            'nsfw': bool(channel.get('nsfw', False)),
        })

    if dry_run:
        logger.info(f"  [DRY RUN] channel_metadata[{guild_id}]: would upsert {len(rows)} channels from Discord API")
        return

    total = 0
    for index in range(0, len(rows), batch_size):
        batch = rows[index:index + batch_size]
        sb.table('discord_channels').upsert(batch).execute()
        total += len(batch)
    logger.info(f"  channel_metadata[{guild_id}]: upserted {total} channels from Discord API")


def reassign_guild_for_channel_ids(sb, guild_id: int, channel_ids: list[int], dry_run: bool, batch_size: int = 500):
    if not channel_ids:
        logger.info(f"  guild_reassign[{guild_id}]: no channel IDs supplied")
        return

    if dry_run:
        logger.info(f"  [DRY RUN] guild_reassign[{guild_id}]: would retag channels/messages for {len(channel_ids)} channels")
        return

    updated_channels = 0
    updated_messages = 0
    for index in range(0, len(channel_ids), batch_size):
        batch = channel_ids[index:index + batch_size]
        sb.table('discord_channels').update({'guild_id': guild_id}).in_('channel_id', batch).execute()
        sb.table('discord_messages').update({'guild_id': guild_id}).in_('channel_id', batch).execute()
        sb.table('discord_messages').update({'guild_id': guild_id}).in_('thread_id', batch).execute()
        updated_channels += len(batch)
        updated_messages += len(batch)

    logger.info(f"  guild_reassign[{guild_id}]: updated channel/message guild IDs using Discord channel inventory")


# ==========================================================================
# Main
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            'Backfill guild_id on historical/core tables. daily_summaries is '
            'legacy history; active live-update tables are not modified here.'
        )
    )
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--skip-verify', action='store_true', help='Skip safety verification')
    parser.add_argument('--force-server-config', action='store_true',
                        help='Overwrite existing server_config values for seeded guilds')
    parser.add_argument('--arca-guild-id', type=int, help='Optional Arca guild ID to retag and seed from Discord API')
    args = parser.parse_args()

    guild_id = int(os.getenv('GUILD_ID', 0))
    if not guild_id:
        logger.error("GUILD_ID env var not set")
        sys.exit(1)

    logger.info(f"Backfilling guild_id={guild_id} (BNDC)")
    if args.dry_run:
        logger.info("[DRY RUN MODE]")

    sb = get_client()

    # 0. Safety verification
    if not args.skip_verify:
        logger.info("Running safety verification...")
        verify_no_foreign_guild_data(sb, guild_id)

    # 1. Tag existing rows with BNDC guild_id
    tables = [
        'discord_messages',
        'discord_channels',
        'daily_summaries',  # legacy daily-summary history/backfill input
        'shared_posts',
        'pending_intros',
        'discord_reactions',
        'discord_reaction_log',
    ]
    for table in tables:
        logger.info(f"Backfilling {table}...")
        try:
            backfill_table(sb, table, guild_id, args.dry_run)
        except Exception as e:
            logger.error(f"Error backfilling {table}: {e}", exc_info=True)

    # 2. Backfill reaction guild_ids via message JOIN
    logger.info("Backfilling reaction guild_ids from messages...")
    try:
        backfill_reaction_guild_ids(sb, args.dry_run)
    except Exception as e:
        logger.error(f"Error backfilling reaction guild_ids: {e}", exc_info=True)

    # 3. Populate guild_members from discord_members
    logger.info("Backfilling guild_members...")
    try:
        backfill_guild_members(sb, guild_id, args.dry_run)
    except Exception as e:
        logger.error(f"Error backfilling guild_members: {e}", exc_info=True)

    # 4. Seed server_config for BNDC
    logger.info("Seeding server_config...")
    try:
        seed_server_config(sb, guild_id, args.dry_run, force=args.force_server_config)
    except Exception as e:
        logger.error(f"Error seeding server_config: {e}", exc_info=True)

    # 5. Seed server_content from posts/*.md
    logger.info("Seeding server_content...")
    try:
        seed_server_content(sb, guild_id, args.dry_run)
    except Exception as e:
        logger.error(f"Error seeding server_content: {e}", exc_info=True)

    # 6. Populate channel metadata from Discord API (BNDC)
    discord_token = os.getenv('DISCORD_BOT_TOKEN')
    if discord_token:
        try:
            logger.info("Fetching BNDC channels from Discord API...")
            bndc_channels = asyncio.run(_fetch_guild_channels(discord_token, guild_id))
            upsert_channel_metadata_from_discord(sb, guild_id, bndc_channels, args.dry_run)
        except Exception as e:
            logger.error(f"Error backfilling BNDC channel metadata: {e}", exc_info=True)
    else:
        logger.warning("DISCORD_BOT_TOKEN not set; skipping Discord API metadata backfill")

    # 7. Optional Arca retag + seed
    if args.arca_guild_id:
        if not discord_token:
            logger.error("Cannot process --arca-guild-id without DISCORD_BOT_TOKEN")
            sys.exit(1)

        try:
            logger.info(f"Fetching Arca channels from Discord API (guild {args.arca_guild_id})...")
            arca_channels = asyncio.run(_fetch_guild_channels(discord_token, args.arca_guild_id))
            arca_channel_ids = [int(channel['id']) for channel in arca_channels]
            reassign_guild_for_channel_ids(sb, args.arca_guild_id, arca_channel_ids, args.dry_run)
            upsert_channel_metadata_from_discord(sb, args.arca_guild_id, arca_channels, args.dry_run)
        except Exception as e:
            logger.error(f"Error reassigning/backfilling Arca channels: {e}", exc_info=True)

        try:
            logger.info(f"Fetching Arca members from Discord API (guild {args.arca_guild_id})...")
            arca_members = asyncio.run(_fetch_guild_members(discord_token, args.arca_guild_id))
            seed_guild_members_from_discord(sb, args.arca_guild_id, arca_members, args.dry_run)
        except Exception as e:
            logger.error(f"Error seeding Arca guild_members: {e}", exc_info=True)

        try:
            seed_archive_only_server_config(sb, args.arca_guild_id, "Arca Gidan", args.dry_run, force=args.force_server_config)
        except Exception as e:
            logger.error(f"Error seeding Arca server_config: {e}", exc_info=True)

        try:
            logger.info("Re-syncing reaction guild_ids after Arca channel/message reassignment...")
            backfill_reaction_guild_ids(sb, args.dry_run)
        except Exception as e:
            logger.error(f"Error re-syncing reaction guild_ids after Arca reassignment: {e}", exc_info=True)

    logger.info("Backfill complete!")


if __name__ == '__main__':
    main()
