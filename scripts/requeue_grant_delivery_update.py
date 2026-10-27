"""Reassess exactly one acknowledged grant update.

Default mode is a read-only inspection. ``--execute`` requeues only the exact
guild/thread/message row and retains its Discord acknowledgement ID. Add
``--process-now`` to fetch that message and run the normal GrantsCog delivery
handler immediately over Discord REST; this does not start a gateway or load
any cogs. Grant/payment state is never changed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord
from discord.ext import commands

from src.common.db_handler import DatabaseHandler
from src.features.grants.delivery_store import GrantDeliveryStore
from src.features.grants.grants_cog import GrantsCog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guild-id', type=int, required=True)
    parser.add_argument('--thread-id', type=int, required=True)
    parser.add_argument('--message-id', type=int, required=True,
                        help='One applicant delivery-update message to reassess')
    parser.add_argument('--execute', action='store_true',
                        help='Apply the one-row reset; otherwise only inspect the target')
    parser.add_argument('--process-now', action='store_true',
                        help='After reset, immediately invoke the normal handler using REST only')
    return parser


def validate_options(args: argparse.Namespace) -> None:
    if args.process_now and not args.execute:
        raise ValueError('--process-now requires --execute')


def _read_target(db: DatabaseHandler, guild_id: int, thread_id: int, message_id: int) -> dict[str, Any]:
    client = db.storage_handler.supabase_client
    rows = (client.table('grant_delivery_updates').select(
        'guild_id,thread_id,message_id,applicant_id,state,ack_message_id,assessment'
    ).eq('guild_id', guild_id).eq('thread_id', thread_id)
            .eq('message_id', message_id).limit(1).execute().data or [])
    if len(rows) != 1:
        raise ValueError('No unique delivery row matches the supplied guild/thread/message IDs.')
    row = rows[0]
    if row.get('state') != 'acknowledged':
        raise ValueError(f"Target row state is {row.get('state')!r}; expected 'acknowledged'.")
    if not row.get('ack_message_id'):
        raise ValueError('Target has no acknowledgement ID to update in place.')
    return row


def _reset_target(db: DatabaseHandler, row: dict[str, Any]) -> None:
    guild_id = int(row['guild_id'])
    gate = getattr(db, '_gate_check', None)
    if gate is None or not gate(guild_id):
        raise PermissionError(f'Writes are not allowed for guild {guild_id}.')
    updated = (db.storage_handler.supabase_client.table('grant_delivery_updates').update({
        'state': 'pending', 'evidence': [], 'assessment': None,
        'lease_owner': None, 'lease_until': None,
    }).eq('guild_id', guild_id).eq('thread_id', int(row['thread_id']))
      .eq('message_id', int(row['message_id'])).eq('state', 'acknowledged')
      .eq('ack_message_id', int(row['ack_message_id'])).execute().data or [])
    if len(updated) != 1:
        raise RuntimeError('No row reset; it changed concurrently or the update failed.')


async def _validate_discord_target(bot: discord.Client, db: DatabaseHandler,
                                   row: dict[str, Any], guild_id: int,
                                   thread_id: int, message_id: int):
    grant = db.get_grant_by_thread(thread_id, guild_id=guild_id)
    if not grant or grant.get('status') != 'paid' or not grant.get('paid_at'):
        raise ValueError('The exact grant must exist and have paid status before delivery reassessment.')
    applicant_id = int(grant.get('applicant_id', -1))
    if applicant_id != int(row['applicant_id']):
        raise ValueError('Delivery row applicant does not match the grant applicant.')
    thread = await bot.fetch_channel(thread_id)
    if not isinstance(thread, discord.Thread) or int(thread.id) != thread_id:
        raise ValueError('The supplied thread ID did not resolve to a Discord thread.')
    if int(getattr(getattr(thread, 'guild', None), 'id', -1)) != guild_id:
        raise ValueError('Fetched Discord thread belongs to a different guild.')
    message = await thread.fetch_message(message_id)
    if int(getattr(getattr(message, 'channel', None), 'id', -1)) != thread_id:
        raise ValueError('Fetched delivery message belongs to a different thread.')
    if int(getattr(getattr(message, 'author', None), 'id', -1)) != applicant_id:
        raise ValueError('Fetched delivery message is not authored by the grant applicant.')
    if getattr(getattr(message, 'author', None), 'bot', False):
        raise ValueError('Fetched delivery message is bot-authored.')
    return grant, thread, message


async def _process_now(db: DatabaseHandler, row: dict[str, Any], *, guild_id: int,
                       thread_id: int, message_id: int, token: str,
                       client_factory=discord.Client,
                       handler_class=GrantsCog) -> None:
    bot = client_factory(intents=discord.Intents.none())
    try:
        # Client.login performs REST authentication but never opens a gateway.
        await bot.login(token)
        grant, _thread, message = await _validate_discord_target(
            bot, db, row, guild_id, thread_id, message_id,
        )
        _reset_target(db, row)

        handler = handler_class.__new__(handler_class)
        handler.bot = bot
        handler.db = db
        handler.delivery_store = GrantDeliveryStore(db)
        await handler._process_delivery_update(message, grant)
        current = handler.delivery_store.get(guild_id, message_id)
        if current is None or current.get('state') != 'acknowledged':
            raise RuntimeError('The normal delivery handler did not finish this update; it remains retryable.')
    finally:
        await bot.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_options(args)
    except ValueError as exc:
        parser.error(str(exc))
    if args.process_now and not os.getenv('DISCORD_BOT_TOKEN'):
        parser.error('DISCORD_BOT_TOKEN is required for --process-now')

    try:
        db = DatabaseHandler()
        row = _read_target(db, args.guild_id, args.thread_id, args.message_id)
    except Exception as exc:
        print(f'Could not load target: {type(exc).__name__}.', file=sys.stderr)
        return 1

    print('Target:', {
        'guild_id': row['guild_id'], 'thread_id': row['thread_id'],
        'message_id': row['message_id'], 'applicant_id': row['applicant_id'],
        'ack_message_id': row['ack_message_id'],
        'classification': (row.get('assessment') or {}).get('classification'),
    })
    if not args.execute:
        print('Dry run. Re-run with --execute to reset only this row.')
        return 0

    if args.process_now:
        token = os.getenv('DISCORD_BOT_TOKEN')
        try:
            asyncio.run(_process_now(
                db, row, guild_id=args.guild_id, thread_id=args.thread_id,
                message_id=args.message_id, token=token,
            ))
        except Exception as exc:
            # Provider and Discord errors may contain request details; report
            # only the exception type so credentials and response bodies stay private.
            print(f'Immediate reassessment failed: {type(exc).__name__}.', file=sys.stderr)
            return 1
        print('One delivery update was reassessed through the normal handler; its existing acknowledgement was updated.')
        return 0

    try:
        _reset_target(db, row)
    except Exception as exc:
        print(f'Could not requeue target: {type(exc).__name__}.', file=sys.stderr)
        return 1
    print('One delivery row reset to pending; acknowledgement ID retained.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
