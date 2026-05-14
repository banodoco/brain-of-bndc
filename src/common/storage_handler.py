"""
Unified Storage Handler - Manages writes to Supabase.
This provides a single interface for storing messages.
"""

import asyncio
import json
import logging
import os
import mimetypes
import uuid
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional
from pathlib import Path
import sys

import aiohttp

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions

logger = logging.getLogger('DiscordBot')

class StorageHandler:
    """
    Handler for storing data to Supabase.
    """
    
    def __init__(self, storage_backend: Optional[str] = None):
        """
        Initialize the storage handler.
        
        Args:
            storage_backend: Ignored - always uses Supabase. Kept for backwards compatibility.
        """
        self.supabase_client: Optional[Client] = None
        self.batch_size = 100  # Batch size for Supabase writes
        
        logger.debug(f"Storage backend: supabase")
        
        # Initialize Supabase
        self._init_supabase()
    
    def _init_supabase(self) -> None:
        """Initialize the Supabase client."""
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
        
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL or SUPABASE_SERVICE_KEY not set. Cannot use Supabase backend.")
            raise ValueError("Supabase credentials required")
        
        try:
            # Try with ClientOptions (newer API)
            try:
                options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
                self.supabase_client = create_client(supabase_url, supabase_key, options=options)
            except (AttributeError, TypeError):
                # Fall back to creating client without options if ClientOptions API has changed
                self.supabase_client = create_client(supabase_url, supabase_key)
            logger.debug("Supabase client initialized successfully for direct writes")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}", exc_info=True)
            raise

    @staticmethod
    def _normalize_attachment_list(attachments: Any) -> List[Dict[str, Any]]:
        """Return attachment/embed rows as a list from list, dict, or JSON string inputs."""
        if attachments is None:
            return []
        if isinstance(attachments, list):
            return attachments
        if isinstance(attachments, dict):
            return [attachments]
        if isinstance(attachments, str):
            try:
                parsed = json.loads(attachments)
            except (json.JSONDecodeError, TypeError):
                return []
            return StorageHandler._normalize_attachment_list(parsed)
        return []
    
    async def store_messages_to_supabase(self, messages: List[Dict]) -> int:
        """
        Store messages directly to Supabase.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Number of messages successfully stored
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return 0
        
        if not messages:
            return 0
        messages = list(messages)

        try:
            # Transform messages to Supabase format
            supabase_messages = []
            for msg in messages:
                # Handle attachments and embeds - ensure they're properly formatted
                attachments = msg.get('attachments', [])
                if isinstance(attachments, str):
                    try:
                        attachments = json.loads(attachments)
                    except json.JSONDecodeError:
                        attachments = []
                
                embeds = msg.get('embeds', [])
                if isinstance(embeds, str):
                    try:
                        embeds = json.loads(embeds)
                    except json.JSONDecodeError:
                        embeds = []
                
                reactors = msg.get('reactors', [])
                if isinstance(reactors, str):
                    try:
                        reactors = json.loads(reactors)
                    except json.JSONDecodeError:
                        reactors = []
                
                supabase_msg = {
                    'message_id': msg.get('message_id') or msg.get('id'),
                    'channel_id': msg.get('channel_id'),
                    'author_id': msg.get('author_id'),
                    'content': msg.get('content'),
                    'created_at': msg.get('created_at'),
                    'attachments': attachments,
                    'embeds': embeds,
                    'reaction_count': msg.get('reaction_count', 0) or 0,
                    'reactors': reactors,
                    'reference_id': msg.get('reference_id'),
                    'edited_at': msg.get('edited_at'),
                    'is_pinned': bool(msg.get('is_pinned', False)),
                    'thread_id': msg.get('thread_id'),
                    'message_type': msg.get('message_type'),
                    'flags': msg.get('flags'),
                    'is_deleted': bool(msg.get('is_deleted', False)),
                    'indexed_at': msg.get('indexed_at') or datetime.utcnow().isoformat(),
                    'synced_at': datetime.utcnow().isoformat()
                }
                # Include guild_id when available
                if msg.get('guild_id') is not None:
                    supabase_msg['guild_id'] = msg['guild_id']
                supabase_messages.append(supabase_msg)
            
            # Write in batches
            stored_count = 0
            for i in range(0, len(supabase_messages), self.batch_size):
                batch = supabase_messages[i:i + self.batch_size]
                
                try:
                    await asyncio.to_thread(
                        self.supabase_client.table('discord_messages').upsert(batch).execute
                    )
                    stored_count += len(batch)
                    logger.debug(f"Stored batch of {len(batch)} messages to Supabase ({stored_count}/{len(supabase_messages)})")
                except Exception as e:
                    logger.error(f"Failed to store message batch to Supabase: {e}", exc_info=True)
                    continue
            
            if stored_count > 0:
                logger.info(f"Successfully stored {stored_count} messages directly to Supabase")
            
            return stored_count
            
        except Exception as e:
            logger.error(f"Error storing messages to Supabase: {e}", exc_info=True)
            return 0
    
    async def store_members_to_supabase(self, members: List[Dict]) -> int:
        """
        Store member profiles directly to Supabase.
        
        Args:
            members: List of member dictionaries
            
        Returns:
            Number of members successfully stored
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return 0
        
        if not members:
            return 0
        
        try:
            supabase_members = []
            for member in members:
                # Parse role_ids if it's a string
                role_ids = member.get('role_ids', [])
                if isinstance(role_ids, str):
                    try:
                        role_ids = json.loads(role_ids)
                    except json.JSONDecodeError:
                        role_ids = []
                
                supabase_member = {
                    'member_id': member.get('member_id'),
                    'username': member.get('username'),
                    'global_name': member.get('global_name'),
                    'server_nick': member.get('server_nick') or member.get('display_name'),
                    'avatar_url': member.get('avatar_url'),
                    'discriminator': member.get('discriminator'),
                    'bot': bool(member.get('bot', False)),
                    'system': bool(member.get('system', False)),
                    'accent_color': member.get('accent_color'),
                    'banner_url': member.get('banner_url'),
                    'discord_created_at': member.get('discord_created_at'),
                    'guild_join_date': member.get('guild_join_date'),
                    'role_ids': role_ids,
                    'twitter_url': member.get('twitter_url'),
                    'reddit_url': member.get('reddit_url'),
                    'created_at': member.get('created_at') or datetime.utcnow().isoformat(),
                    'updated_at': member.get('updated_at') or datetime.utcnow().isoformat(),
                    'synced_at': datetime.utcnow().isoformat()
                }

                # IMPORTANT: Only include these fields when explicitly provided as True/False.
                # If we upsert NULL for these columns, we override the DB defaults (TRUE)
                # and can unintentionally wipe preferences.
                if 'include_in_updates' in member and member.get('include_in_updates') is not None:
                    supabase_member['include_in_updates'] = member.get('include_in_updates')
                if 'allow_content_sharing' in member and member.get('allow_content_sharing') is not None:
                    supabase_member['allow_content_sharing'] = member.get('allow_content_sharing')
                supabase_members.append(supabase_member)
            
            # Write in batches
            stored_count = 0
            for i in range(0, len(supabase_members), self.batch_size):
                batch = supabase_members[i:i + self.batch_size]
                
                try:
                    await asyncio.to_thread(
                        self.supabase_client.table('members').upsert(batch).execute
                    )
                    stored_count += len(batch)
                    logger.debug(f"Stored batch of {len(batch)} members to Supabase")
                except Exception as e:
                    logger.error(f"Failed to store member batch to Supabase: {e}", exc_info=True)
                    continue
            
            if stored_count > 0:
                logger.info(f"Successfully stored {stored_count} members directly to Supabase")
            
            return stored_count
            
        except Exception as e:
            logger.error(f"Error storing members to Supabase: {e}", exc_info=True)
            return 0
    
    async def store_channels_to_supabase(self, channels: List[Dict]) -> int:
        """
        Store channels directly to Supabase.
        
        Args:
            channels: List of channel dictionaries
            
        Returns:
            Number of channels successfully stored
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return 0
        
        if not channels:
            return 0
        
        try:
            supabase_channels = []
            for channel in channels:
                # NOTE: speaker_mode and onboarding_default are intentionally
                # omitted here so that upserts do not overwrite DB-managed
                # values. New rows get DB defaults; existing rows keep theirs.
                supabase_channel = {
                    'channel_id': channel.get('channel_id'),
                    'channel_name': channel.get('channel_name'),
                    'category_id': channel.get('category_id'),
                    'description': channel.get('description'),
                    'suitable_posts': channel.get('suitable_posts'),
                    'unsuitable_posts': channel.get('unsuitable_posts'),
                    'rules': channel.get('rules'),
                    'setup_complete': bool(channel.get('setup_complete', False)),
                    'nsfw': bool(channel.get('nsfw', False)),
                    'enriched': bool(channel.get('enriched', False)),
                    'synced_at': datetime.utcnow().isoformat()
                }
                # Include guild_id when available
                if channel.get('guild_id') is not None:
                    supabase_channel['guild_id'] = channel['guild_id']
                if channel.get('channel_type') is not None:
                    supabase_channel['channel_type'] = channel['channel_type']
                if channel.get('parent_id') is not None:
                    supabase_channel['parent_id'] = channel['parent_id']
                supabase_channels.append(supabase_channel)
            
            # Write in batches
            stored_count = 0
            for i in range(0, len(supabase_channels), self.batch_size):
                batch = supabase_channels[i:i + self.batch_size]
                
                try:
                    await asyncio.to_thread(
                        self.supabase_client.table('discord_channels').upsert(batch).execute
                    )
                    stored_count += len(batch)
                    logger.debug(f"Stored batch of {len(batch)} channels to Supabase")
                except Exception as e:
                    logger.error(f"Failed to store channel batch to Supabase: {e}", exc_info=True)
                    continue
            
            if stored_count > 0:
                logger.info(f"Successfully stored {stored_count} channels directly to Supabase")
            
            return stored_count
            
        except Exception as e:
            logger.error(f"Error storing channels to Supabase: {e}", exc_info=True)
            return 0
    
    async def get_summary_for_date(
        self,
        channel_id: int,
        date: Optional[datetime] = None,
        dev_mode: bool = False,
    ) -> Optional[str]:
        """
        Get the legacy daily summary for a channel on a given date.
        
        Args:
            channel_id: The channel ID
            date: Date to check (defaults to today)
            
        Returns:
            The full_summary text if exists, None otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        
        try:
            summary_date = (date or datetime.now()).strftime('%Y-%m-%d')
            
            result = await asyncio.to_thread(
                lambda: self.supabase_client.table('daily_summaries')
                    .select('full_summary')
                    .eq('date', summary_date)
                    .eq('channel_id', channel_id)
                    .eq('dev_mode', dev_mode)
                    .execute()
            )
            
            if result.data and len(result.data) > 0:
                return result.data[0].get('full_summary')
            return None
            
        except Exception as e:
            logger.error(f"Error getting summary for date: {e}", exc_info=True)
            return None

    async def summary_exists_for_date(
        self,
        channel_id: int,
        date: Optional[datetime] = None,
        dev_mode: bool = False,
    ) -> bool:
        """
        Check if a legacy daily summary already exists for a channel/date.
        
        Args:
            channel_id: The channel ID
            date: Date to check (defaults to today)
            
        Returns:
            True if summary exists, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        try:
            summary_date = (date or datetime.now()).strftime('%Y-%m-%d')
            
            result = await asyncio.to_thread(
                lambda: self.supabase_client.table('daily_summaries')
                    .select('channel_id')
                    .eq('date', summary_date)
                    .eq('channel_id', channel_id)
                    .eq('dev_mode', dev_mode)
                    .execute()
            )
            
            exists = bool(result.data and len(result.data) > 0)
            logger.debug(f"Summary exists check for channel {channel_id}, date {summary_date}: {exists}")
            return exists
            
        except Exception as e:
            logger.error(f"Error checking if summary exists: {e}", exc_info=True)
            return False

    async def store_daily_summary_to_supabase(
        self,
        channel_id: int,
        full_summary: Optional[str],
        short_summary: Optional[str],
        date: Optional[datetime] = None,
        included_in_main_summary: bool = False,
        dev_mode: bool = False,
        guild_id: Optional[int] = None
    ) -> bool:
        """
        Store a legacy daily summary to Supabase.

        Active live-update editor state must use live-editor persistence, not
        the daily_summaries table.
        
        Args:
            channel_id: The channel ID
            full_summary: Full summary text
            short_summary: Short summary text
            date: Date of the summary (defaults to today)
            included_in_main_summary: Whether items from this summary were included in the main summary
            dev_mode: Whether this summary was created in development mode
            
        Returns:
            True if successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        try:
            summary_date = (date or datetime.now()).strftime('%Y-%m-%d')
            
            summary_data = {
                'date': summary_date,
                'channel_id': channel_id,
                'full_summary': full_summary,
                'short_summary': short_summary,
                'created_at': datetime.utcnow().isoformat(),
                'included_in_main_summary': included_in_main_summary,
                'dev_mode': dev_mode
            }
            if guild_id is not None:
                summary_data['guild_id'] = guild_id
            
            await asyncio.to_thread(
                self.supabase_client.table('daily_summaries').upsert(
                    summary_data, 
                    on_conflict='date,channel_id'
                ).execute
            )
            
            logger.debug(f"Stored daily summary to Supabase for channel {channel_id}, date {summary_date} (dev_mode={dev_mode})")
            return True
            
        except Exception as e:
            logger.error(f"Error storing daily summary to Supabase: {e}", exc_info=True)
            return False

    async def mark_summaries_included_in_main(
        self,
        date: datetime,
        channel_message_ids: Dict[int, List[str]],
        dev_mode: bool = False,
    ) -> bool:
        """
        Mark legacy channel summaries as included in the main summary.
        
        Args:
            date: The date of the summaries
            channel_message_ids: Dict mapping channel_id -> list of message_ids that were included
            
        Returns:
            True if all updates successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        summary_date = date.strftime('%Y-%m-%d')
        all_success = True
        
        for channel_id, _message_ids in channel_message_ids.items():
            try:
                await asyncio.to_thread(
                    self.supabase_client.table('daily_summaries')
                    .update({
                        'included_in_main_summary': True
                    })
                    .eq('date', summary_date)
                    .eq('channel_id', channel_id)
                    .eq('dev_mode', dev_mode)
                    .execute
                )
                logger.debug(f"Marked summary for channel {channel_id} as included in main summary")
            except Exception as e:
                logger.error(f"Error marking summary for channel {channel_id} as included: {e}", exc_info=True)
                all_success = False
        
        return all_success

    async def update_channel_summary_full_summary(
        self,
        channel_id: int,
        date: datetime,
        full_summary: str,
        dev_mode: bool = False,
    ) -> bool:
        """
        Update the full_summary field for a legacy channel daily summary.
        Used to enrich channel summaries with inclusion flags and media URLs.
        
        Args:
            channel_id: The channel ID
            date: The date of the summary
            full_summary: The enriched full_summary JSON string
            
        Returns:
            True if successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        summary_date = date.strftime('%Y-%m-%d')
        
        try:
            await asyncio.to_thread(
                self.supabase_client.table('daily_summaries')
                .update({'full_summary': full_summary})
                .eq('date', summary_date)
                .eq('channel_id', channel_id)
                .eq('dev_mode', dev_mode)
                .execute
            )
            logger.debug(f"Updated full_summary for channel {channel_id} on {summary_date}")
            return True
        except Exception as e:
            logger.error(f"Error updating full_summary for channel {channel_id}: {e}", exc_info=True)
            return False
    
    async def update_summary_thread_to_supabase(self, channel_id: int, thread_id: Optional[int]) -> bool:
        """
        Update or delete a summary thread ID in Supabase.
        
        Args:
            channel_id: The channel ID
            thread_id: The thread ID (None to delete)
            
        Returns:
            True if successful, False otherwise
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return False
        
        try:
            if thread_id:
                # Upsert the thread ID
                thread_data = {
                    'channel_id': channel_id,
                    'summary_thread_id': thread_id,
                    'updated_at': datetime.utcnow().isoformat()
                }
                
                await asyncio.to_thread(
                    self.supabase_client.table('channel_summary').upsert(thread_data).execute
                )
                logger.debug(f"Updated summary thread ID to {thread_id} for channel {channel_id} in Supabase")
            else:
                # Delete the entry
                await asyncio.to_thread(
                    self.supabase_client.table('channel_summary').delete().eq('channel_id', channel_id).execute
                )
                logger.debug(f"Deleted summary thread entry for channel {channel_id} in Supabase")
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating summary thread in Supabase: {e}", exc_info=True)
            return False

    # ========== Live-Update Editor Persistence ==========

    @staticmethod
    def _clean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def _as_json_array(value: Optional[Any]) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    @staticmethod
    def _as_json_object(value: Optional[Any]) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _topic_editor_lease_timeout_minutes() -> int:
        raw_timeout = os.getenv('TOPIC_EDITOR_LEASE_TIMEOUT_MINUTES', '30')
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid TOPIC_EDITOR_LEASE_TIMEOUT_MINUTES=%r; using 30",
                raw_timeout,
            )
            return 30
        return max(timeout, 1)

    async def _insert_live_row(self, table: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table(table).insert(payload).execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error inserting {table}: {e}", exc_info=True)
            return None

    async def _update_live_row(
        self,
        table: str,
        id_column: str,
        row_id: Any,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        payload = self._clean_payload(payload)
        if not payload:
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table(table).update(payload).eq(id_column, row_id).execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error updating {table}.{id_column}={row_id}: {e}", exc_info=True)
            return None

    async def _upsert_live_row(
        self,
        table: str,
        payload: Dict[str, Any],
        on_conflict: str,
    ) -> Optional[Dict[str, Any]]:
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table(table).upsert(
                    self._clean_payload(payload),
                    on_conflict=on_conflict,
                ).execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error upserting {table}: {e}", exc_info=True)
            return None

    async def create_live_update_run(self, run: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Create an auditable live-editor run."""
        return await self._insert_live_row('live_update_editor_runs', self._clean_payload({
            'guild_id': run.get('guild_id'),
            'environment': environment,
            'trigger': run.get('trigger') or 'scheduled',
            'status': run.get('status') or 'running',
            'live_channel_id': run.get('live_channel_id'),
            'checkpoint_before': self._as_json_object(run.get('checkpoint_before')),
            'checkpoint_after': self._as_json_object(run.get('checkpoint_after')),
            'memory_snapshot': self._as_json_object(run.get('memory_snapshot')),
            'duplicate_snapshot': self._as_json_object(run.get('duplicate_snapshot')),
            'candidate_count': run.get('candidate_count', 0),
            'accepted_count': run.get('accepted_count', 0),
            'rejected_count': run.get('rejected_count', 0),
            'duplicate_count': run.get('duplicate_count', 0),
            'deferred_count': run.get('deferred_count', 0),
            'skipped_reason': run.get('skipped_reason'),
            'error_message': run.get('error_message'),
            'metadata': self._as_json_object(run.get('metadata')),
            'completed_at': run.get('completed_at'),
        }))

    async def update_live_update_run(self, run_id: str, updates: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Update an auditable live-editor run."""
        payload = dict(updates or {})
        payload['environment'] = environment
        for key in ('checkpoint_before', 'checkpoint_after', 'memory_snapshot', 'duplicate_snapshot', 'metadata'):
            if key in payload:
                payload[key] = self._as_json_object(payload[key])
        return await self._update_live_row('live_update_editor_runs', 'run_id', run_id, payload)

    async def store_live_update_candidate(self, candidate: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Persist one generated candidate with source IDs and author snapshot."""
        return await self._insert_live_row('live_update_candidates', self._clean_payload({
            'run_id': candidate.get('run_id'),
            'environment': environment,
            'guild_id': candidate.get('guild_id'),
            'source_channel_id': candidate.get('source_channel_id'),
            'update_type': candidate.get('update_type') or 'other',
            'title': candidate.get('title'),
            'body': candidate.get('body') or '',
            'media_refs': self._as_json_array(candidate.get('media_refs')),
            'source_message_ids': self._as_json_array(candidate.get('source_message_ids')),
            'author_context_snapshot': self._as_json_object(
                candidate.get('author_context_snapshot') or candidate.get('author_context')
            ),
            'duplicate_key': candidate.get('duplicate_key'),
            'confidence': candidate.get('confidence'),
            'priority': candidate.get('priority', 0),
            'rationale': candidate.get('rationale'),
            'raw_agent_output': self._as_json_object(candidate.get('raw_agent_output')),
            'status': candidate.get('status') or 'generated',
        }))

    async def store_live_update_candidates(self, candidates: List[Dict[str, Any]], environment: str = 'prod') -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for candidate in candidates or []:
            row = await self.store_live_update_candidate(candidate, environment=environment)
            if row:
                rows.append(row)
        return rows

    async def update_live_update_candidate_status(
        self,
        candidate_id: str,
        status: str,
        updates: Optional[Dict[str, Any]] = None,
        environment: str = 'prod',
    ) -> Optional[Dict[str, Any]]:
        payload = {'status': status}
        payload.update(updates or {})
        if 'raw_agent_output' in payload:
            payload['raw_agent_output'] = self._as_json_object(payload['raw_agent_output'])
        return await self._update_live_row('live_update_candidates', 'candidate_id', candidate_id, payload)

    async def store_live_update_decision(self, decision: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Persist one candidate decision."""
        return await self._insert_live_row('live_update_decisions', self._clean_payload({
            'run_id': decision.get('run_id'),
            'environment': environment,
            'candidate_id': decision.get('candidate_id'),
            'decision': decision.get('decision') or 'skipped',
            'reason': decision.get('reason'),
            'duplicate_key': decision.get('duplicate_key'),
            'duplicate_of_candidate_id': decision.get('duplicate_of_candidate_id'),
            'duplicate_of_feed_item_id': decision.get('duplicate_of_feed_item_id'),
            'decided_by': decision.get('decided_by') or 'live_update_editor',
            'decision_payload': self._as_json_object(decision.get('decision_payload') or decision.get('metadata')),
        }))

    async def store_live_update_feed_item(self, feed_item: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Persist one logical feed item with every posted Discord message ID in order."""
        discord_message_ids = [str(mid) for mid in self._as_json_array(feed_item.get('discord_message_ids'))]
        return await self._insert_live_row('live_update_feed_items', self._clean_payload({
            'run_id': feed_item.get('run_id'),
            'environment': environment,
            'candidate_id': feed_item.get('candidate_id'),
            'guild_id': feed_item.get('guild_id'),
            'live_channel_id': feed_item.get('live_channel_id') or feed_item.get('channel_id'),
            'update_type': feed_item.get('update_type') or 'other',
            'title': feed_item.get('title'),
            'body': feed_item.get('body') or '',
            'media_refs': self._as_json_array(feed_item.get('media_refs')),
            'source_message_ids': self._as_json_array(feed_item.get('source_message_ids')),
            'duplicate_key': feed_item.get('duplicate_key'),
            'discord_message_ids': discord_message_ids,
            'status': feed_item.get('status') or 'posted',
            'post_error': feed_item.get('post_error'),
            'posted_at': feed_item.get('posted_at') or (datetime.utcnow().isoformat() if discord_message_ids else None),
        }))

    async def update_live_update_feed_item_messages(
        self,
        feed_item_id: str,
        discord_message_ids: List[Any],
        status: str = 'posted',
        post_error: Optional[str] = None,
        environment: str = 'prod',
    ) -> Optional[Dict[str, Any]]:
        """Replace ordered posted Discord message IDs for a logical feed item."""
        return await self._update_live_row('live_update_feed_items', 'feed_item_id', feed_item_id, {
            'discord_message_ids': [str(mid) for mid in self._as_json_array(discord_message_ids)],
            'status': status,
            'post_error': post_error,
            'posted_at': datetime.utcnow().isoformat() if status == 'posted' else None,
        })

    async def get_recent_live_update_feed_items(
        self,
        guild_id: Optional[int] = None,
        live_channel_id: Optional[int] = None,
        limit: int = 50,
        since_hours: Optional[int] = None,
        environment: str = 'prod',
    ) -> List[Dict[str, Any]]:
        """Fetch recent logical feed items; discord_message_ids remains ordered."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return []
        try:
            query = self.supabase_client.table('live_update_feed_items').select('*').order('created_at', desc=True).limit(limit)
            query = query.eq('environment', environment)
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if live_channel_id is not None:
                query = query.eq('live_channel_id', live_channel_id)
            if since_hours:
                since = datetime.utcnow() - timedelta(hours=since_hours)
                query = query.gte('created_at', since.isoformat())
            result = await asyncio.to_thread(query.execute)
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching live-update feed items: {e}", exc_info=True)
            return []

    async def find_live_update_duplicate(self, duplicate_key: str, guild_id: Optional[int] = None, environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Find duplicate state plus latest candidate/feed item for a key."""
        if not self.supabase_client or not duplicate_key:
            return None
        try:
            state_q = self.supabase_client.table('live_update_duplicate_state').select('*').eq('duplicate_key', duplicate_key).eq('environment', environment).limit(1)
            feed_q = self.supabase_client.table('live_update_feed_items').select('*').eq('duplicate_key', duplicate_key).eq('environment', environment).order('created_at', desc=True).limit(1)
            candidate_q = self.supabase_client.table('live_update_candidates').select('*').eq('duplicate_key', duplicate_key).eq('environment', environment).order('created_at', desc=True).limit(1)
            if guild_id is not None:
                state_q = state_q.eq('guild_id', guild_id)
                feed_q = feed_q.eq('guild_id', guild_id)
                candidate_q = candidate_q.eq('guild_id', guild_id)
            state_result, feed_result, candidate_result = await asyncio.gather(
                asyncio.to_thread(state_q.execute),
                asyncio.to_thread(feed_q.execute),
                asyncio.to_thread(candidate_q.execute),
            )
            return {
                'duplicate_state': state_result.data[0] if state_result.data else None,
                'feed_item': feed_result.data[0] if feed_result.data else None,
                'candidate': candidate_result.data[0] if candidate_result.data else None,
            }
        except Exception as e:
            logger.error(f"Error finding duplicate key {duplicate_key}: {e}", exc_info=True)
            return None

    async def upsert_live_update_duplicate_state(self, state: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Upsert duplicate suppression state."""
        return await self._upsert_live_row('live_update_duplicate_state', {
            'duplicate_key': state.get('duplicate_key'),
            'environment': environment,
            'guild_id': state.get('guild_id'),
            'first_seen_candidate_id': state.get('first_seen_candidate_id'),
            'last_seen_candidate_id': state.get('last_seen_candidate_id'),
            'feed_item_id': state.get('feed_item_id'),
            'status': state.get('status') or 'seen',
            'seen_count': state.get('seen_count', 1),
            'metadata': self._as_json_object(state.get('metadata')),
            'first_seen_at': state.get('first_seen_at'),
            'last_seen_at': state.get('last_seen_at') or datetime.utcnow().isoformat(),
        }, on_conflict='environment,duplicate_key')

    async def get_live_update_checkpoint(self, checkpoint_key: str, environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Read a live-editor checkpoint."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table('live_update_checkpoints')
                .select('*')
                .eq('checkpoint_key', checkpoint_key)
                .eq('environment', environment)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error reading checkpoint {checkpoint_key}: {e}", exc_info=True)
            return None

    async def upsert_live_update_checkpoint(self, checkpoint: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Upsert a live-editor checkpoint over archived messages."""
        return await self._upsert_live_row('live_update_checkpoints', {
            'checkpoint_key': checkpoint.get('checkpoint_key'),
            'environment': environment,
            'guild_id': checkpoint.get('guild_id'),
            'channel_id': checkpoint.get('channel_id'),
            'last_message_id': checkpoint.get('last_message_id'),
            'last_message_created_at': checkpoint.get('last_message_created_at'),
            'last_run_id': checkpoint.get('last_run_id'),
            'state': self._as_json_object(checkpoint.get('state')),
        }, on_conflict='environment,checkpoint_key')

    async def acquire_topic_editor_run(self, run: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Create a running topic-editor lease row."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None

        run_id = str(run.get('run_id') or uuid.uuid4())
        now = datetime.utcnow().isoformat()
        timeout_minutes = self._topic_editor_lease_timeout_minutes()
        cutoff = (datetime.utcnow() - timedelta(minutes=timeout_minutes)).isoformat()

        try:
            stale_result = await asyncio.to_thread(
                self.supabase_client.table('topic_editor_runs')
                .select('run_id,metadata')
                .eq('environment', environment)
                .eq('guild_id', run.get('guild_id'))
                .eq('live_channel_id', run.get('live_channel_id'))
                .eq('status', 'running')
                .lt('started_at', cutoff)
                .limit(1)
                .execute
            )
            stale_rows = stale_result.data or []
            if stale_rows:
                stale = stale_rows[0]
                metadata = self._as_json_object(stale.get('metadata'))
                metadata.update({
                    'expired_by': run_id,
                    'expired_at': now,
                })
                # Supabase REST cannot wrap this stale-lease update and the insert below
                # in one transaction. The insert still relies on the partial unique index,
                # so any competing runner that wins the post-expiry race blocks this insert.
                await asyncio.to_thread(
                    self.supabase_client.table('topic_editor_runs')
                    .update({
                        'status': 'failed',
                        'error_message': 'stale running lease expired by a later run',
                        'completed_at': now,
                        'metadata': metadata,
                    })
                    .eq('run_id', stale.get('run_id'))
                    .eq('environment', environment)
                    .eq('status', 'running')
                    .lt('started_at', cutoff)
                    .execute
                )
        except Exception as e:
            logger.error(f"Error failing stale topic_editor_runs lease: {e}", exc_info=True)
            return None

        return await self._insert_live_row('topic_editor_runs', self._clean_payload({
            'run_id': run_id,
            'guild_id': run.get('guild_id'),
            'environment': environment,
            'live_channel_id': run.get('live_channel_id'),
            'trigger': run.get('trigger') or 'scheduled',
            'status': 'running',
            'checkpoint_before': self._as_json_object(run.get('checkpoint_before')),
            'checkpoint_after': self._as_json_object(run.get('checkpoint_after')),
            'source_message_count': run.get('source_message_count', 0),
            'tool_call_count': run.get('tool_call_count', 0),
            'accepted_count': run.get('accepted_count', 0),
            'rejected_count': run.get('rejected_count', 0),
            'override_count': run.get('override_count', 0),
            'observation_count': run.get('observation_count', 0),
            'published_count': run.get('published_count', 0),
            'failed_publish_count': run.get('failed_publish_count', 0),
            'input_tokens': run.get('input_tokens', 0),
            'output_tokens': run.get('output_tokens', 0),
            'cost_usd': run.get('cost_usd'),
            'latency_ms': run.get('latency_ms'),
            'model': run.get('model'),
            'publishing_enabled': bool(run.get('publishing_enabled', False)),
            'trace_channel_id': run.get('trace_channel_id'),
            'skipped_reason': run.get('skipped_reason'),
            'error_message': run.get('error_message'),
            'metadata': self._as_json_object(run.get('metadata')),
        }))

    async def complete_topic_editor_run(
        self,
        run_id: str,
        updates: Optional[Dict[str, Any]] = None,
        environment: str = 'prod',
    ) -> Optional[Dict[str, Any]]:
        payload = dict(updates or {})
        payload['status'] = payload.get('status') or 'completed'
        payload['environment'] = environment
        payload['completed_at'] = payload.get('completed_at') or datetime.utcnow().isoformat()
        for key in ('checkpoint_before', 'checkpoint_after', 'metadata'):
            if key in payload:
                payload[key] = self._as_json_object(payload[key])
        return await self._update_live_row('topic_editor_runs', 'run_id', run_id, payload)

    async def fail_topic_editor_run(
        self,
        run_id: str,
        error_message: str,
        updates: Optional[Dict[str, Any]] = None,
        environment: str = 'prod',
    ) -> Optional[Dict[str, Any]]:
        payload = dict(updates or {})
        payload.update({
            'status': 'failed',
            'environment': environment,
            'error_message': error_message,
            'completed_at': payload.get('completed_at') or datetime.utcnow().isoformat(),
        })
        for key in ('checkpoint_before', 'checkpoint_after', 'metadata'):
            if key in payload:
                payload[key] = self._as_json_object(payload[key])
        return await self._update_live_row('topic_editor_runs', 'run_id', run_id, payload)

    async def upsert_topic(self, topic: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._upsert_live_row('topics', {
            'topic_id': topic.get('topic_id'),
            'canonical_key': topic.get('canonical_key'),
            'display_slug': topic.get('display_slug'),
            'guild_id': topic.get('guild_id'),
            'environment': environment,
            'state': topic.get('state'),
            'headline': topic.get('headline'),
            'summary': self._as_json_object(topic.get('summary')),
            'source_authors': self._as_json_array(topic.get('source_authors')),
            'parent_topic_id': topic.get('parent_topic_id'),
            'revisit_at': topic.get('revisit_at'),
            'publication_status': topic.get('publication_status'),
            'publication_error': topic.get('publication_error'),
            'discord_message_ids': [int(mid) for mid in self._as_json_array(topic.get('discord_message_ids')) if str(mid).isdigit()],
            'publication_attempts': topic.get('publication_attempts', 0),
            'last_published_at': topic.get('last_published_at'),
        }, on_conflict='environment,guild_id,canonical_key')

    async def update_topic(self, topic_id: str, updates: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        payload = dict(updates or {})
        payload['environment'] = environment
        if 'summary' in payload:
            payload['summary'] = self._as_json_object(payload['summary'])
        if 'source_authors' in payload:
            payload['source_authors'] = self._as_json_array(payload['source_authors'])
        if 'discord_message_ids' in payload:
            payload['discord_message_ids'] = [int(mid) for mid in self._as_json_array(payload['discord_message_ids']) if str(mid).isdigit()]
        return await self._update_live_row('topics', 'topic_id', topic_id, payload)

    async def get_topics(
        self,
        guild_id: Optional[int] = None,
        states: Optional[List[str]] = None,
        limit: int = 100,
        environment: str = 'prod',
    ) -> List[Dict[str, Any]]:
        if not self.supabase_client:
            return []
        try:
            query = self.supabase_client.table('topics').select('*').eq('environment', environment).order('updated_at', desc=True).limit(limit)
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if states:
                query = query.in_('state', states)
            result = await asyncio.to_thread(query.execute)
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching topics: {e}", exc_info=True)
            return []

    async def add_topic_source(self, source: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._upsert_live_row('topic_sources', {
            'topic_id': source.get('topic_id'),
            'message_id': source.get('message_id'),
            'guild_id': source.get('guild_id'),
            'environment': environment,
            'added_in_run_id': source.get('added_in_run_id') or source.get('run_id'),
        }, on_conflict='topic_id,message_id')

    async def add_topic_sources(self, sources: List[Dict[str, Any]], environment: str = 'prod') -> List[Dict[str, Any]]:
        rows = []
        for source in sources or []:
            row = await self.add_topic_source(source, environment=environment)
            if row:
                rows.append(row)
        return rows

    async def upsert_topic_alias(self, alias: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._upsert_live_row('topic_aliases', {
            'topic_id': alias.get('topic_id'),
            'alias_key': alias.get('alias_key'),
            'alias_kind': alias.get('alias_kind') or 'proposed',
            'guild_id': alias.get('guild_id'),
            'environment': environment,
        }, on_conflict='environment,guild_id,alias_key')

    async def get_topic_aliases(self, guild_id: Optional[int] = None, environment: str = 'prod') -> List[Dict[str, Any]]:
        if not self.supabase_client:
            return []
        try:
            query = self.supabase_client.table('topic_aliases').select('*').eq('environment', environment)
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching topic aliases: {e}", exc_info=True)
            return []

    async def search_topic_editor_topics(
        self,
        query: str,
        guild_id: Optional[int] = None,
        environment: str = 'prod',
        state_filter: Optional[List[str]] = None,
        hours_back: int = 72,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search compact topic rows by headline, canonical key, and alias."""
        if not self.supabase_client:
            return []
        safe_query = str(query or "").strip()[:120]
        safe_limit = max(1, min(int(limit or 10), 10))
        since = datetime.utcnow() - timedelta(hours=max(1, int(hours_back or 72)))
        states = self._as_json_array(state_filter)

        def topic_query():
            q = (
                self.supabase_client.table('topics')
                .select('topic_id,canonical_key,headline,state,created_at')
                .eq('environment', environment)
                .gte('created_at', since.isoformat())
                .order('created_at', desc=True)
                .limit(safe_limit)
            )
            if guild_id is not None:
                q = q.eq('guild_id', guild_id)
            if states:
                q = q.in_('state', states)
            return q

        try:
            topic_tasks = []
            if safe_query:
                topic_tasks = [
                    asyncio.to_thread(topic_query().ilike('headline', f"%{safe_query}%").execute),
                    asyncio.to_thread(topic_query().ilike('canonical_key', f"%{safe_query}%").execute),
                ]
            else:
                topic_tasks = [asyncio.to_thread(topic_query().execute)]

            alias_query = (
                self.supabase_client.table('topic_aliases')
                .select('topic_id,alias_key')
                .eq('environment', environment)
                .limit(safe_limit)
            )
            if guild_id is not None:
                alias_query = alias_query.eq('guild_id', guild_id)
            if safe_query:
                alias_query = alias_query.ilike('alias_key', f"%{safe_query}%")
            query_results = await asyncio.gather(*topic_tasks, asyncio.to_thread(alias_query.execute))
            alias_rows = query_results[-1].data or []
            alias_topic_ids = [row.get('topic_id') for row in alias_rows if row.get('topic_id')]

            topic_rows: List[Dict[str, Any]] = []
            for result in query_results[:-1]:
                topic_rows.extend(result.data or [])

            if alias_topic_ids:
                alias_topic_query = topic_query().in_('topic_id', alias_topic_ids).limit(safe_limit)
                alias_topic_result = await asyncio.to_thread(alias_topic_query.execute)
                topic_rows.extend(alias_topic_result.data or [])

            merged: Dict[str, Dict[str, Any]] = {}
            for row in topic_rows:
                topic_id = str(row.get('topic_id') or '')
                if topic_id and topic_id not in merged:
                    merged[topic_id] = row

            topic_ids = list(merged.keys())[:safe_limit]
            aliases_by_topic: Dict[str, List[str]] = {topic_id: [] for topic_id in topic_ids}
            if topic_ids:
                all_alias_query = (
                    self.supabase_client.table('topic_aliases')
                    .select('topic_id,alias_key')
                    .eq('environment', environment)
                    .in_('topic_id', topic_ids)
                )
                if guild_id is not None:
                    all_alias_query = all_alias_query.eq('guild_id', guild_id)
                all_alias_result = await asyncio.to_thread(all_alias_query.execute)
                for alias in all_alias_result.data or []:
                    topic_id = str(alias.get('topic_id') or '')
                    alias_key = alias.get('alias_key')
                    if topic_id in aliases_by_topic and alias_key:
                        aliases_by_topic[topic_id].append(str(alias_key)[:200])

            compacted: List[Dict[str, Any]] = []
            for topic_id in topic_ids:
                row = merged[topic_id]
                compacted.append({
                    'topic_id': topic_id,
                    'canonical_key': str(row.get('canonical_key') or '')[:200],
                    'headline': str(row.get('headline') or '')[:200],
                    'state': row.get('state'),
                    'aliases': aliases_by_topic.get(topic_id, [])[:10],
                    'created_at': row.get('created_at'),
                })
            return compacted[:safe_limit]
        except Exception as e:
            logger.warning("Could not search topic-editor topics: %s", e, exc_info=True)
            return []

    async def get_topic_editor_author_profile(
        self,
        author_id: Optional[int],
        guild_id: Optional[int] = None,
        environment: str = 'prod',
    ) -> Dict[str, Any]:
        """Fetch compact last-30-days author stats from archived Discord messages."""
        if not self.supabase_client or not author_id:
            return {}
        try:
            since = datetime.utcnow() - timedelta(days=30)
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,content,created_at,reaction_count', count='exact')
                .eq('author_id', author_id)
                .eq('is_deleted', False)
                .gte('created_at', since.isoformat())
                .order('created_at', desc=True)
                .limit(30)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            rows = result.data or []
            reaction_counts = [int(row.get('reaction_count') or 0) for row in rows]
            recent = rows[:3]
            return {
                'author_id': author_id,
                'message_count_30d': result.count if result.count is not None else len(rows),
                'recent_message_ids': [str(row.get('message_id')) for row in recent if row.get('message_id') is not None],
                'recent_message_dates': [row.get('created_at') for row in recent if row.get('created_at')],
                'average_reaction_count': round(sum(reaction_counts) / len(reaction_counts), 2) if reaction_counts else 0,
                'sample_messages': [
                    {
                        'message_id': str(row.get('message_id')),
                        'content_preview': str(row.get('content') or '')[:200],
                        'created_at': row.get('created_at'),
                    }
                    for row in recent
                ],
            }
        except Exception as e:
            logger.warning("Could not fetch topic-editor author profile: %s", e, exc_info=True)
            return {}

    async def get_topic_editor_message_context(
        self,
        message_ids: List[str],
        guild_id: Optional[int] = None,
        environment: str = 'prod',
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fetch exact archived messages in a compact shape for topic-editor tools."""
        if not self.supabase_client or not message_ids:
            return []
        safe_limit = max(1, min(int(limit or 10), 10))
        ids = [str(item) for item in message_ids if item is not None][:safe_limit]
        if not ids:
            return []
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,channel_id,author_id,content,attachments,embeds,created_at,reference_id,thread_id')
                .in_('message_id', ids)
                .eq('is_deleted', False)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            rows = await self._attach_channel_context_and_filter_nsfw(result.data or [])
            author_ids = []
            for row in rows:
                try:
                    author_id = int(row.get('author_id'))
                except (TypeError, ValueError):
                    continue
                if author_id not in author_ids:
                    author_ids.append(author_id)
            snapshots = await self.get_author_context_snapshots(sorted(author_ids), guild_id=guild_id)
            rows_by_id = {str(row.get('message_id')): row for row in rows}
            compacted: List[Dict[str, Any]] = []
            for message_id in ids:
                row = rows_by_id.get(str(message_id))
                if not row:
                    continue
                try:
                    author_id = int(row.get('author_id'))
                except (TypeError, ValueError):
                    author_id = None
                snapshot = snapshots.get(author_id, {})

                # ── Build media_refs_available with attachment, embed, external entries ──
                media_refs_available: List[Dict[str, Any]] = []
                attachments = self._normalize_attachment_list(row.get('attachments'))
                for idx, att in enumerate(attachments):
                    media_refs_available.append({
                        'kind': 'attachment',
                        'index': idx,
                        'url_present': bool(
                            (isinstance(att, dict) and (att.get('url') or att.get('proxy_url')))
                            if isinstance(att, dict) else False
                        ),
                        'content_type': att.get('content_type') if isinstance(att, dict) else None,
                        'filename': att.get('filename') if isinstance(att, dict) else None,
                    })

                embeds = self._normalize_attachment_list(row.get('embeds'))
                for embed_idx, embed in enumerate(embeds if isinstance(embeds, list) else []):
                    media_refs_available.append({
                        'kind': 'embed',
                        'index': embed_idx,
                        'url_present': (
                            isinstance(embed, dict) and bool(
                                (isinstance(embed.get('url'), dict) and (embed['url'].get('url') or embed['url'].get('proxy_url')))
                                or (isinstance(embed.get('thumbnail'), dict) and (embed['thumbnail'].get('url') or embed['thumbnail'].get('proxy_url')))
                                or (isinstance(embed.get('image'), dict) and (embed['image'].get('url') or embed['image'].get('proxy_url')))
                                or (isinstance(embed.get('video'), dict) and (embed['video'].get('url') or embed['video'].get('proxy_url')))
                            )
                        ),
                        'content_type': None,
                        'filename': None,
                    })

                # External linked media refs (after attachment/embed for priority indexing)
                # Uses the shared extract_external_urls helper from src.common.external_media
                from src.common.external_media import extract_external_urls
                for external_entry in extract_external_urls(row):
                    media_refs_available.append({
                        'kind': external_entry['kind'],
                        'index': external_entry['index'],
                        'domain': external_entry['domain'],
                        'url_present': external_entry['url_present'],
                        'source': external_entry['source'],
                    })

                compacted.append({
                    'message_id': str(row.get('message_id')),
                    'channel_name': row.get('channel_name'),
                    'author_name': (
                        snapshot.get('server_nick')
                        or snapshot.get('global_name')
                        or snapshot.get('display_name')
                        or snapshot.get('username')
                    ),
                    'content': str(row.get('content') or '')[:200],
                    'created_at': row.get('created_at'),
                    'reply_to_message_id': row.get('reference_id'),
                    'thread_id': row.get('thread_id'),
                    'media_refs_available': media_refs_available,
                })
            return compacted
        except Exception as e:
            logger.warning("Could not fetch topic-editor message context: %s", e, exc_info=True)
            return []

    async def store_topic_transition(self, transition: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._insert_live_row('topic_transitions', self._clean_payload({
            'topic_id': transition.get('topic_id'),
            'run_id': transition.get('run_id'),
            'environment': environment,
            'guild_id': transition.get('guild_id'),
            'tool_call_id': transition.get('tool_call_id'),
            'from_state': transition.get('from_state'),
            'to_state': transition.get('to_state'),
            'action': transition.get('action'),
            'reason': transition.get('reason'),
            'model': transition.get('model'),
            'payload': self._as_json_object(transition.get('payload')),
        }))

    async def get_topic_transitions_by_tool_call_ids(
        self,
        run_id: str,
        tool_call_ids: List[str],
        environment: str = 'prod',
    ) -> Dict[str, Dict[str, Any]]:
        if not self.supabase_client or not run_id or not tool_call_ids:
            return {}
        ids = []
        seen = set()
        for tool_call_id in tool_call_ids:
            if tool_call_id is None:
                continue
            normalized = str(tool_call_id)
            if normalized and normalized not in seen:
                seen.add(normalized)
                ids.append(normalized)
        if not ids:
            return {}
        try:
            query = (
                self.supabase_client
                .table('topic_transitions')
                .select('*')
                .eq('environment', environment)
                .eq('run_id', str(run_id))
                .in_('tool_call_id', ids)
            )
            result = await asyncio.to_thread(query.execute)
            rows = result.data or []
            by_tool_call_id: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                tool_call_id = row.get('tool_call_id')
                if not tool_call_id:
                    continue
                key = str(tool_call_id)
                existing = by_tool_call_id.get(key)
                if not existing or (existing.get('action') == 'override' and row.get('action') != 'override'):
                    by_tool_call_id[key] = row
            return by_tool_call_id
        except Exception as e:
            logger.error(f"Error fetching topic transitions by tool_call_id: {e}", exc_info=True)
            return {}

    async def store_editorial_observation(self, observation: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._insert_live_row('editorial_observations', self._clean_payload({
            'run_id': observation.get('run_id'),
            'guild_id': observation.get('guild_id'),
            'environment': environment,
            'source_message_ids': [int(mid) for mid in self._as_json_array(observation.get('source_message_ids')) if str(mid).isdigit()],
            'source_authors': self._as_json_array(observation.get('source_authors')),
            'observation_kind': observation.get('observation_kind') or 'considered',
            'reason': observation.get('reason'),
        }))

    async def get_topic_editor_checkpoint(self, checkpoint_key: str, environment: str = 'prod') -> Optional[Dict[str, Any]]:
        if not self.supabase_client:
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table('topic_editor_checkpoints')
                .select('*')
                .eq('checkpoint_key', checkpoint_key)
                .eq('environment', environment)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error reading topic-editor checkpoint {checkpoint_key}: {e}", exc_info=True)
            return None

    async def upsert_topic_editor_checkpoint(self, checkpoint: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._upsert_live_row('topic_editor_checkpoints', {
            'checkpoint_key': checkpoint.get('checkpoint_key'),
            'environment': environment,
            'guild_id': checkpoint.get('guild_id'),
            'channel_id': checkpoint.get('channel_id'),
            'last_message_id': checkpoint.get('last_message_id'),
            'last_message_created_at': checkpoint.get('last_message_created_at'),
            'last_run_id': checkpoint.get('last_run_id'),
            'state': self._as_json_object(checkpoint.get('state')),
        }, on_conflict='environment,checkpoint_key')

    async def mirror_live_checkpoint_to_topic_editor(self, checkpoint_key: str, environment: str = 'prod') -> Optional[Dict[str, Any]]:
        legacy = await self.get_live_update_checkpoint(checkpoint_key, environment=environment)
        if not legacy:
            return None
        return await self.upsert_topic_editor_checkpoint(legacy, environment=environment)

    async def mirror_topic_editor_checkpoint_to_live(self, checkpoint_key: str, environment: str = 'prod') -> Optional[Dict[str, Any]]:
        topic_checkpoint = await self.get_topic_editor_checkpoint(checkpoint_key, environment=environment)
        if not topic_checkpoint:
            return None
        return await self.upsert_live_update_checkpoint(topic_checkpoint, environment=environment)

    async def get_author_context_snapshots(self, author_ids: List[int], guild_id: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
        """Build immutable author-context snapshots from persisted profile rows."""
        if not self.supabase_client or not author_ids:
            return {}
        try:
            members_result = await asyncio.to_thread(
                self.supabase_client.table('members').select('*').in_('member_id', author_ids).execute
            )
            guild_rows: List[Dict[str, Any]] = []
            if guild_id is not None:
                guild_result = await asyncio.to_thread(
                    self.supabase_client.table('guild_members')
                    .select('*')
                    .eq('guild_id', guild_id)
                    .in_('member_id', author_ids)
                    .execute
                )
                guild_rows = guild_result.data or []
            guild_by_member = {row.get('member_id'): row for row in guild_rows}
            snapshots: Dict[int, Dict[str, Any]] = {}
            for member in members_result.data or []:
                member_id = member.get('member_id')
                guild_member = guild_by_member.get(member_id, {})
                snapshots[member_id] = {
                    'member_id': member_id,
                    'guild_id': guild_id,
                    'username': member.get('username'),
                    'global_name': member.get('global_name'),
                    'server_nick': guild_member.get('server_nick') or member.get('server_nick'),
                    'avatar_url': member.get('avatar_url'),
                    'bot': member.get('bot'),
                    'role_ids': guild_member.get('role_ids') or member.get('role_ids') or [],
                    'twitter_url': member.get('twitter_url') or member.get('twitter_handle'),
                    'reddit_url': member.get('reddit_url'),
                    'website': member.get('website'),
                    'snapshot_at': datetime.utcnow().isoformat(),
                }
            return snapshots
        except Exception as e:
            logger.error(f"Error building author snapshots: {e}", exc_info=True)
            return {}

    async def get_archived_messages_after_checkpoint(
        self,
        checkpoint: Optional[Dict[str, Any]] = None,
        guild_id: Optional[int] = None,
        channel_ids: Optional[List[int]] = None,
        limit: int = 200,
        exclude_author_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch persisted archived messages after the stored checkpoint."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return []
        checkpoint = checkpoint or {}
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('*')
                .eq('is_deleted', False)
                .order('created_at')
                .order('message_id')
                .limit(limit)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if channel_ids:
                query = query.in_('channel_id', channel_ids)
            if exclude_author_ids:
                cleaned_exclusions = [int(author_id) for author_id in exclude_author_ids if author_id is not None]
                if cleaned_exclusions:
                    query = query.not_.in_('author_id', cleaned_exclusions)
            if checkpoint.get('last_message_id') is not None:
                query = query.gt('message_id', checkpoint['last_message_id'])
            elif checkpoint.get('last_message_created_at'):
                query = query.gt('created_at', checkpoint['last_message_created_at'])
            result = await asyncio.to_thread(query.execute)
            messages = result.data or []
            if messages:
                messages = await self._attach_channel_context_and_filter_nsfw(messages)
            author_ids = sorted({msg.get('author_id') for msg in messages if msg.get('author_id') is not None})
            author_snapshots = await self.get_author_context_snapshots(author_ids, guild_id=guild_id)
            for msg in messages:
                msg['author_context_snapshot'] = author_snapshots.get(msg.get('author_id'), {})
            return messages
        except Exception as e:
            logger.error(f"Error fetching archived messages after checkpoint: {e}", exc_info=True)
            return []

    async def get_latest_archived_message_checkpoint(self, guild_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return the newest non-deleted archived message id/timestamp for checkpoint seeding."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('guild_id,message_id,created_at')
                .eq('is_deleted', False)
                .order('created_at', desc=True)
                .order('message_id', desc=True)
                .limit(1)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching latest archived message checkpoint: {e}", exc_info=True)
            return None

    async def get_archived_message_id_before_timestamp(
        self, guild_id: Optional[int], before: str
    ) -> Optional[int]:
        """Return the message_id of the most recent non-deleted archived message
        whose created_at is at or before `before` (ISO timestamp). Used to anchor
        cold-start checkpoints to a time window so `get_archived_messages_after_checkpoint`
        returns the post-anchor traffic.

        Returns None if no archived message is older than the anchor.
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,created_at')
                .eq('is_deleted', False)
                .lte('created_at', before)
                .order('created_at', desc=True)
                .order('message_id', desc=True)
                .limit(1)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            if not result.data:
                return None
            return int(result.data[0]['message_id'])
        except Exception as e:
            logger.error(f"Error fetching archived message id before timestamp: {e}", exc_info=True)
            return None

    async def _attach_channel_context_and_filter_nsfw(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach channel names and remove NSFW channels from live-editor source messages."""
        channel_ids = sorted({msg.get('channel_id') for msg in messages if msg.get('channel_id') is not None})
        if not channel_ids or not self.supabase_client:
            return messages

        try:
            channel_result = await asyncio.to_thread(
                self.supabase_client.table('discord_channels')
                .select('channel_id,channel_name,nsfw')
                .in_('channel_id', [str(channel_id) for channel_id in channel_ids])
                .execute
            )
            channels = {
                str(row.get('channel_id')): row
                for row in (channel_result.data or [])
                if row.get('channel_id') is not None
            }
        except Exception as e:
            logger.warning("Could not attach channel context for live-editor messages: %s", e, exc_info=True)
            return messages

        filtered: List[Dict[str, Any]] = []
        for msg in messages:
            channel = channels.get(str(msg.get('channel_id'))) or {}
            channel_name = str(channel.get('channel_name') or msg.get('channel_name') or '')
            is_nsfw = bool(channel.get('nsfw')) or 'nsfw' in channel_name.lower()
            if is_nsfw:
                continue
            msg['channel_name'] = channel_name
            msg['channel_is_nsfw'] = False
            filtered.append(msg)
        return filtered

    async def get_live_update_context_for_messages(
        self,
        messages: List[Dict[str, Any]],
        guild_id: Optional[int] = None,
        limit: int = 24,
        environment: str = 'prod',
        exclude_author_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Fetch DB-backed context packets for likely live-update source messages."""
        if not self.supabase_client or not messages:
            return {"source_context": {}}

        selected = self._select_live_context_sources(messages, limit=limit)
        source_context: Dict[str, Dict[str, Any]] = {}
        for message in selected:
            message_id = str(message.get('message_id'))
            source_context[message_id] = {
                "source_message_id": message_id,
                "same_channel_history": await self._get_channel_history_before_message(
                    message, guild_id=guild_id, exclude_author_ids=exclude_author_ids
                ),
                "author_recent_messages": await self._get_author_recent_messages(
                    message, guild_id=guild_id, exclude_author_ids=exclude_author_ids
                ),
                "author_stats": await self._get_author_live_update_stats(message.get('author_id'), guild_id=guild_id),
                "engagement_context": await self.get_live_update_message_engagement_context(
                    [message_id],
                    guild_id=guild_id,
                    participant_limit=8,
                ),
            }
        return {"source_context": source_context}

    def _select_live_context_sources(self, messages: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        def score(message: Dict[str, Any]) -> tuple:
            reaction_count = int(message.get('reaction_count') or 0)
            has_media = bool(self._as_json_array(message.get('attachments')) or self._as_json_array(message.get('embeds')))
            content_len = len(str(message.get('content') or ''))
            return (reaction_count, 1 if has_media else 0, content_len, str(message.get('created_at') or ''))

        sorted_messages = sorted(messages, key=score, reverse=True)
        return sorted_messages[:max(1, limit)]

    async def _get_channel_history_before_message(
        self,
        message: Dict[str, Any],
        guild_id: Optional[int] = None,
        limit: int = 18,
        exclude_author_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        channel_id = message.get('channel_id')
        created_at = message.get('created_at')
        if not channel_id or not created_at:
            return []
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                .eq('is_deleted', False)
                .eq('channel_id', channel_id)
                .lt('created_at', created_at)
                .order('created_at', desc=True)
                .limit(limit)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if exclude_author_ids:
                cleaned = [int(a) for a in exclude_author_ids if a is not None]
                if cleaned:
                    query = query.not_.in_('author_id', cleaned)
            result = await asyncio.to_thread(query.execute)
            rows = await self._attach_channel_context_and_filter_nsfw(result.data or [])
            return self._compact_live_context_messages(rows)
        except Exception as e:
            logger.warning("Could not fetch live-update channel history: %s", e)
            return []

    async def _get_author_recent_messages(
        self,
        message: Dict[str, Any],
        guild_id: Optional[int] = None,
        limit: int = 18,
        exclude_author_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        author_id = message.get('author_id')
        created_at = message.get('created_at')
        if not author_id:
            return []
        if exclude_author_ids:
            try:
                if int(author_id) in {int(a) for a in exclude_author_ids if a is not None}:
                    return []
            except (TypeError, ValueError):
                pass
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                .eq('is_deleted', False)
                .eq('author_id', author_id)
                .order('created_at', desc=True)
                .limit(limit + 1)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if created_at:
                query = query.lte('created_at', created_at)
            result = await asyncio.to_thread(query.execute)
            rows = [
                row for row in (result.data or [])
                if str(row.get('message_id')) != str(message.get('message_id'))
            ][:limit]
            rows = await self._attach_channel_context_and_filter_nsfw(rows)
            return self._compact_live_context_messages(rows)
        except Exception as e:
            logger.warning("Could not fetch live-update author history: %s", e)
            return []

    async def _get_author_live_update_stats(
        self,
        author_id: Optional[int],
        guild_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not author_id:
            return {}
        try:
            count_query = self.supabase_client.table('discord_messages').select('message_id', count='exact').eq('author_id', author_id)
            sample_query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,reaction_count,created_at')
                .eq('author_id', author_id)
                .order('created_at', desc=True)
                .limit(200)
            )
            if guild_id is not None:
                count_query = count_query.eq('guild_id', guild_id)
                sample_query = sample_query.eq('guild_id', guild_id)
            count_result, sample_result = await asyncio.gather(
                asyncio.to_thread(count_query.execute),
                asyncio.to_thread(sample_query.execute),
            )
            samples = sample_result.data or []
            reaction_counts = [int(row.get('reaction_count') or 0) for row in samples]
            average_reactions = sum(reaction_counts) / len(reaction_counts) if reaction_counts else 0
            return {
                "total_messages": count_result.count or 0,
                "sample_size": len(samples),
                "average_reactions_per_recent_message": round(average_reactions, 2),
                "max_reactions_recent": max(reaction_counts) if reaction_counts else 0,
            }
        except Exception as e:
            logger.warning("Could not fetch live-update author stats: %s", e)
            return {}

    def _compact_live_context_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compacted: List[Dict[str, Any]] = []
        for row in messages:
            compacted.append({
                "message_id": str(row.get('message_id')),
                "guild_id": row.get('guild_id'),
                "channel_id": row.get('channel_id'),
                "channel_name": row.get('channel_name'),
                "author_id": row.get('author_id'),
                "content": str(row.get('content') or '')[:500],
                "created_at": row.get('created_at'),
                "reaction_count": row.get('reaction_count') or 0,
                "attachments": self._as_json_array(row.get('attachments')),
                "embeds": self._as_json_array(row.get('embeds')),
                "thread_id": row.get('thread_id'),
                "reference_id": row.get('reference_id'),
                "is_reply": bool(row.get('reference_id')),
                "is_thread_message": bool(row.get('thread_id')),
            })
        return compacted

    async def search_live_update_messages(
        self,
        query: str,
        guild_id: Optional[int] = None,
        author_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        hours_back: int = 168,
        limit: int = 20,
        environment: str = 'prod',
        exclude_author_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Search archived messages for the live-update editor agent."""
        if not self.supabase_client:
            return []
        try:
            since = datetime.utcnow() - timedelta(hours=max(1, int(hours_back or 168)))
            safe_limit = max(1, min(int(limit or 20), 50))
            query_builder = (
                self.supabase_client.table('discord_messages')
                .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                .eq('is_deleted', False)
                .gte('created_at', since.isoformat())
                .order('created_at', desc=True)
                .limit(safe_limit)
            )
            if guild_id is not None:
                query_builder = query_builder.eq('guild_id', guild_id)
            if author_id:
                query_builder = query_builder.eq('author_id', author_id)
            if channel_id:
                query_builder = query_builder.eq('channel_id', channel_id)
            if exclude_author_ids:
                cleaned = [int(a) for a in exclude_author_ids if a is not None]
                if cleaned:
                    query_builder = query_builder.not_.in_('author_id', cleaned)
            if query:
                query_builder = query_builder.ilike('content', f"%{str(query)[:120]}%")
            result = await asyncio.to_thread(query_builder.execute)
            rows = await self._attach_channel_context_and_filter_nsfw(result.data or [])
            return self._compact_live_context_messages(rows)
        except Exception as e:
            logger.warning("Could not search live-update messages: %s", e, exc_info=True)
            return []

    async def search_messages_unified(
        self,
        *,
        scope: str = "archive",
        guild_id: Optional[int] = None,
        environment: str = "prod",
        query: Optional[str] = None,
        from_author_id: Optional[int] = None,
        in_channel_id: Optional[int] = None,
        mentions_author_id: Optional[int] = None,
        has: Optional[List[str]] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        is_reply: Optional[bool] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Search archived Discord messages with Discord-style filters."""
        if not self.supabase_client:
            return {"messages": [], "truncated": False}

        safe_limit = max(1, min(int(limit or 20), 50))
        fetch_limit = min(max(safe_limit * 5, 50), 250)
        select_columns = (
            'message_id,guild_id,channel_id,author_id,content,created_at,'
            'attachments,embeds,reaction_count,thread_id,reference_id'
        )

        def parse_time_bound(value: Optional[str]) -> Optional[str]:
            if not value:
                return None
            raw = str(value).strip()
            if not raw:
                return None
            unit = raw[-1].lower()
            number = raw[:-1]
            try:
                amount = float(number)
            except ValueError:
                return raw
            if unit == "h":
                return (datetime.utcnow() - timedelta(hours=amount)).isoformat()
            if unit == "d":
                return (datetime.utcnow() - timedelta(days=amount)).isoformat()
            if unit == "m":
                return (datetime.utcnow() - timedelta(minutes=amount)).isoformat()
            return raw

        def attachment_kind(row: Dict[str, Any], wanted: str) -> bool:
            content = str(row.get('content') or '').lower()
            attachments = self._as_json_array(row.get('attachments'))
            embeds = self._as_json_array(row.get('embeds'))
            if wanted == "embed":
                return bool(embeds)
            if wanted == "link":
                return "http://" in content or "https://" in content or bool(embeds)
            if wanted == "file":
                return bool(attachments)
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                content_type = str(attachment.get('content_type') or '').lower()
                filename = str(attachment.get('filename') or '').lower()
                if wanted == "image" and (content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
                    return True
                if wanted == "video" and (content_type.startswith("video/") or filename.endswith((".mp4", ".mov", ".webm", ".mkv"))):
                    return True
                if wanted == "audio" and (content_type.startswith("audio/") or filename.endswith((".mp3", ".wav", ".m4a", ".ogg", ".flac"))):
                    return True
            return False

        try:
            builder = (
                self.supabase_client.table('discord_messages')
                .select(select_columns)
                .eq('is_deleted', False)
                .order('created_at', desc=True)
                .limit(fetch_limit)
            )
            if guild_id is not None:
                builder = builder.eq('guild_id', guild_id)
            if from_author_id is not None:
                builder = builder.eq('author_id', from_author_id)
            if in_channel_id is not None:
                builder = builder.eq('channel_id', in_channel_id)
            parsed_after = parse_time_bound(after)
            if parsed_after:
                builder = builder.gte('created_at', parsed_after)
            parsed_before = parse_time_bound(before)
            if parsed_before:
                builder = builder.lte('created_at', parsed_before)
            if query:
                builder = builder.ilike('content', f"%{str(query)[:120]}%")
            result = await asyncio.to_thread(builder.execute)
            rows = result.data or []
            if is_reply is True:
                rows = [row for row in rows if row.get('reference_id')]
            elif is_reply is False:
                rows = [row for row in rows if not row.get('reference_id')]
            wanted_kinds = [str(item).lower() for item in (has or []) if item]
            if mentions_author_id is not None:
                mention_variants = [
                    f"<@{mentions_author_id}>",
                    f"<@!{mentions_author_id}>",
                    str(mentions_author_id),
                ]
                rows = [
                    row for row in rows
                    if any(variant in str(row.get('content') or '') for variant in mention_variants)
                ]
            for wanted in wanted_kinds:
                rows = [row for row in rows if attachment_kind(row, wanted)]

            truncated = len(rows) > safe_limit
            rows = rows[:safe_limit]
            rows = await self._attach_channel_context_and_filter_nsfw(rows)
            return {"messages": self._compact_live_context_messages(rows), "truncated": truncated}
        except Exception as e:
            logger.warning("Could not search archived messages: %s", e, exc_info=True)
            return {"messages": [], "truncated": False}

    async def get_live_update_context_for_message_ids(
        self,
        message_ids: List[str],
        guild_id: Optional[int] = None,
        limit: int = 20,
        environment: str = 'prod',
        exclude_author_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Fetch exact source messages plus context by message id."""
        if not self.supabase_client or not message_ids:
            return {"source_context": {}}
        ids = [str(item) for item in message_ids if item is not None][: max(1, min(int(limit or 20), 50))]
        if not ids:
            return {"source_context": {}}
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('*')
                .in_('message_id', ids)
                .eq('is_deleted', False)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if exclude_author_ids:
                cleaned = [int(a) for a in exclude_author_ids if a is not None]
                if cleaned:
                    query = query.not_.in_('author_id', cleaned)
            result = await asyncio.to_thread(query.execute)
            messages = await self._attach_channel_context_and_filter_nsfw(result.data or [])
            context = await self.get_live_update_context_for_messages(
                messages=messages,
                guild_id=guild_id,
                limit=len(messages),
                exclude_author_ids=exclude_author_ids,
            )
            compact = self._compact_live_context_messages(messages)
            for msg in compact:
                key = str(msg.get('message_id'))
                context.setdefault("source_context", {}).setdefault(key, {})["source_message"] = msg
            return context
        except Exception as e:
            logger.warning("Could not fetch live-update message context by ids: %s", e, exc_info=True)
            return {"source_context": {}}

    async def get_live_update_author_profile(
        self,
        author_id: Optional[int],
        guild_id: Optional[int] = None,
        environment: str = 'prod',
        exclude_author_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Fetch author stats and recent messages for the live-update editor agent."""
        if not author_id:
            return {}
        if exclude_author_ids:
            try:
                if int(author_id) in {int(a) for a in exclude_author_ids if a is not None}:
                    return {}
            except (TypeError, ValueError):
                pass
        stats = await self._get_author_live_update_stats(author_id, guild_id=guild_id)
        recent = await self._get_author_recent_messages(
            {"author_id": author_id},
            guild_id=guild_id,
            limit=20,
        )
        snapshot = (await self.get_author_context_snapshots([int(author_id)], guild_id=guild_id)).get(int(author_id), {})
        return {"author_id": author_id, "snapshot": snapshot, "stats": stats, "recent_messages": recent}

    async def get_live_update_message_engagement_context(
        self,
        message_ids: List[str],
        guild_id: Optional[int] = None,
        participant_limit: int = 12,
        environment: str = 'prod',
    ) -> Dict[str, Any]:
        """Fetch reactor/responder profiles so the editor can evaluate community validation."""
        if not self.supabase_client or not message_ids:
            return {"messages": []}
        safe_ids = [str(item) for item in message_ids if item is not None][:20]
        if not safe_ids:
            return {"messages": []}
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,reactors,thread_id,reference_id')
                .in_('message_id', safe_ids)
                .eq('is_deleted', False)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            messages = await self._attach_channel_context_and_filter_nsfw(result.data or [])
        except Exception as e:
            logger.warning("Could not fetch live-update engagement source messages: %s", e, exc_info=True)
            return {"messages": []}

        participant_limit = max(1, min(int(participant_limit or 12), 25))
        rows: List[Dict[str, Any]] = []
        for message in messages:
            message_id = str(message.get('message_id'))
            reactor_rows = await self._get_active_reactor_rows(message_id, guild_id=guild_id)
            reactor_ids = self._extract_reactor_ids(message, reactor_rows)
            responder_messages = await self._get_response_messages_for_source(message, guild_id=guild_id)
            parent_message = await self._get_parent_message_for_reply(message, guild_id=guild_id)
            responder_ids = [
                int(row.get('author_id'))
                for row in responder_messages
                if row.get('author_id') is not None
            ]
            participant_ids = []
            for item in [message.get('author_id'), *reactor_ids, *responder_ids]:
                if item is None:
                    continue
                try:
                    int_id = int(item)
                except (TypeError, ValueError):
                    continue
                if int_id not in participant_ids:
                    participant_ids.append(int_id)
                if len(participant_ids) >= participant_limit:
                    break
            profiles = await self._live_update_participant_profiles(participant_ids, guild_id=guild_id)
            rows.append({
                "message_id": message_id,
                "source_author_id": message.get('author_id'),
                "is_reply": bool(message.get('reference_id')),
                "reference_id": message.get('reference_id'),
                "parent_message": parent_message,
                "is_thread_message": bool(message.get('thread_id')),
                "thread_id": message.get('thread_id'),
                "reaction_count": message.get('reaction_count') or len(reactor_ids),
                "reactors": [
                    {
                        "user_id": row.get('user_id'),
                        "emoji": row.get('emoji'),
                        "profile": profiles.get(int(row.get('user_id'))) if row.get('user_id') is not None else None,
                    }
                    for row in reactor_rows[:participant_limit]
                ],
                "legacy_reactor_ids": reactor_ids[:participant_limit],
                "responses": responder_messages[:participant_limit],
                "participant_profiles": [
                    profiles[item]
                    for item in participant_ids
                    if item in profiles
                ],
            })
        return {"messages": rows}

    async def get_live_update_recent_reaction_events(
        self,
        guild_id: Optional[int] = None,
        hours_back: int = 1,
        limit: int = 30,
        environment: str = 'prod',
    ) -> Dict[str, Any]:
        """Fetch recent reaction activity, including the message and reactor reputation."""
        if not self.supabase_client:
            return {"reaction_events": []}
        try:
            since = datetime.utcnow() - timedelta(hours=max(1, int(hours_back or 1)))
            safe_limit = max(1, min(int(limit or 30), 100))
            query = (
                self.supabase_client.table('discord_reaction_log')
                .select('message_id,user_id,emoji,action,guild_id,created_at')
                .gte('created_at', since.isoformat())
                .order('created_at', desc=True)
                .limit(safe_limit)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            events = result.data or []
        except Exception as e:
            logger.warning("Could not fetch recent live-update reaction events: %s", e, exc_info=True)
            return {"reaction_events": []}

        message_ids = [str(row.get('message_id')) for row in events if row.get('message_id') is not None]
        user_ids = []
        for row in events:
            if row.get('user_id') is None:
                continue
            try:
                user_id = int(row.get('user_id'))
            except (TypeError, ValueError):
                continue
            if user_id not in user_ids:
                user_ids.append(user_id)

        message_by_id: Dict[str, Dict[str, Any]] = {}
        if message_ids:
            try:
                query = (
                    self.supabase_client.table('discord_messages')
                    .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                    .in_('message_id', message_ids[:100])
                    .eq('is_deleted', False)
                )
                if guild_id is not None:
                    query = query.eq('guild_id', guild_id)
                result = await asyncio.to_thread(query.execute)
                messages = await self._attach_channel_context_and_filter_nsfw(result.data or [])
                message_by_id = {
                    str(row.get('message_id')): row
                    for row in self._compact_live_context_messages(messages)
                    if row.get('message_id') is not None
                }
            except Exception as e:
                logger.warning("Could not hydrate messages for recent reaction events: %s", e)

        profiles = await self._live_update_participant_profiles(user_ids[:25], guild_id=guild_id)
        compact_events: List[Dict[str, Any]] = []
        for row in events:
            message_id = str(row.get('message_id'))
            message = message_by_id.get(message_id)
            if not message:
                continue
            user_id = None
            try:
                user_id = int(row.get('user_id')) if row.get('user_id') is not None else None
            except (TypeError, ValueError):
                user_id = None
            compact_events.append({
                "message_id": message_id,
                "reaction_created_at": row.get('created_at'),
                "action": row.get('action'),
                "emoji": row.get('emoji'),
                "reactor_user_id": user_id,
                "reactor_profile": profiles.get(user_id) if user_id is not None else None,
                "message": message,
                "message_age_bucket": self._live_update_age_bucket(message.get('created_at')),
            })
        return {
            "reaction_events": compact_events,
            "hours_back": max(1, int(hours_back or 1)),
            "note": "Use this to detect older messages that received fresh reactions during the current editorial window.",
        }

    async def _get_active_reactor_rows(
        self,
        message_id: str,
        guild_id: Optional[int] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        try:
            query = (
                self.supabase_client.table('discord_reactions')
                .select('user_id,emoji,guild_id')
                .eq('message_id', message_id)
                .is_('removed_at', 'null')
                .limit(max(1, min(int(limit or 25), 50)))
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            return result.data or []
        except Exception as e:
            logger.warning("Could not fetch active reactors for live-update message %s: %s", message_id, e)
            return []

    def _extract_reactor_ids(
        self,
        message: Dict[str, Any],
        reactor_rows: List[Dict[str, Any]],
    ) -> List[int]:
        ids: List[int] = []
        for row in reactor_rows or []:
            user_id = row.get('user_id')
            if user_id is None:
                continue
            try:
                int_id = int(user_id)
            except (TypeError, ValueError):
                continue
            if int_id not in ids:
                ids.append(int_id)
        legacy_reactors = self._as_json_array(message.get('reactors'))
        for item in legacy_reactors:
            raw_id = item.get('id') if isinstance(item, dict) else item
            try:
                int_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if int_id not in ids:
                ids.append(int_id)
        return ids

    async def get_reply_chain(
        self,
        message_id: str,
        guild_id: Optional[int] = None,
        environment: str = 'prod',
        max_depth: int = 5,
    ) -> List[Dict[str, Any]]:
        """Walk ancestor replies for a Discord message and return them root-first."""
        if not self.supabase_client or not message_id:
            return []

        try:
            depth_limit = max(1, min(int(max_depth or 5), 15))
        except (TypeError, ValueError):
            depth_limit = 5

        select_columns = (
            'message_id,guild_id,channel_id,author_id,content,created_at,'
            'attachments,embeds,reaction_count,thread_id,reference_id'
        )

        async def fetch_message(target_message_id: str) -> Optional[Dict[str, Any]]:
            query = (
                self.supabase_client.table('discord_messages')
                .select(select_columns)
                .eq('message_id', str(target_message_id))
                .eq('is_deleted', False)
                .limit(1)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            rows = result.data or []
            return rows[0] if rows else None

        try:
            ancestors: List[Dict[str, Any]] = []
            seen = {str(message_id)}
            current = await fetch_message(str(message_id))
            for _ in range(depth_limit):
                if not current:
                    break
                parent_id = current.get('reference_id')
                if not parent_id:
                    break
                parent_id = str(parent_id)
                if parent_id in seen:
                    break
                seen.add(parent_id)
                parent = await fetch_message(parent_id)
                if not parent:
                    break
                ancestors.append(parent)
                current = parent

            rows = await self._attach_channel_context_and_filter_nsfw(ancestors)
            return list(reversed(self._compact_live_context_messages(rows)))
        except Exception as e:
            logger.warning("Could not fetch reply chain for live-update message %s: %s", message_id, e)
            return []

    async def _get_response_messages_for_source(
        self,
        message: Dict[str, Any],
        guild_id: Optional[int] = None,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        message_id = message.get('message_id')
        channel_id = message.get('channel_id')
        created_at = message.get('created_at')
        responses: List[Dict[str, Any]] = []
        if message_id:
            try:
                query = (
                    self.supabase_client.table('discord_messages')
                    .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                    .eq('is_deleted', False)
                    .eq('reference_id', message_id)
                    .order('created_at', desc=False)
                    .limit(limit)
                )
                if guild_id is not None:
                    query = query.eq('guild_id', guild_id)
                result = await asyncio.to_thread(query.execute)
                responses.extend(result.data or [])
            except Exception:
                # Older deployments may not expose reference_id; nearby channel replies below are still useful.
                responses = []
        if channel_id and created_at and len(responses) < limit:
            try:
                query = (
                    self.supabase_client.table('discord_messages')
                    .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                    .eq('is_deleted', False)
                    .eq('channel_id', channel_id)
                    .gt('created_at', created_at)
                    .order('created_at', desc=False)
                    .limit(limit)
                )
                if guild_id is not None:
                    query = query.eq('guild_id', guild_id)
                result = await asyncio.to_thread(query.execute)
                seen = {str(row.get('message_id')) for row in responses}
                for row in result.data or []:
                    if str(row.get('message_id')) == str(message_id) or str(row.get('message_id')) in seen:
                        continue
                    responses.append(row)
                    seen.add(str(row.get('message_id')))
                    if len(responses) >= limit:
                        break
            except Exception as e:
                logger.warning("Could not fetch response messages for live-update source %s: %s", message_id, e)
        rows = await self._attach_channel_context_and_filter_nsfw(responses)
        return self._compact_live_context_messages(rows)

    async def _get_parent_message_for_reply(
        self,
        message: Dict[str, Any],
        guild_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        reference_id = message.get('reference_id')
        if not reference_id:
            return None
        try:
            query = (
                self.supabase_client.table('discord_messages')
                .select('message_id,guild_id,channel_id,author_id,content,created_at,attachments,embeds,reaction_count,thread_id,reference_id')
                .eq('message_id', reference_id)
                .eq('is_deleted', False)
                .limit(1)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            rows = await self._attach_channel_context_and_filter_nsfw(result.data or [])
            compact = self._compact_live_context_messages(rows)
            return compact[0] if compact else None
        except Exception as e:
            logger.warning("Could not fetch parent reply message %s: %s", reference_id, e)
            return None

    @staticmethod
    def _live_update_age_bucket(created_at: Any) -> str:
        if not created_at:
            return "unknown"
        try:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return "unknown"
        if age_seconds <= 3600:
            return "last_hour"
        if age_seconds <= 3 * 3600:
            return "last_3_hours"
        if age_seconds <= 6 * 3600:
            return "last_6_hours"
        if age_seconds <= 24 * 3600:
            return "last_24_hours"
        return "older"

    async def _live_update_participant_profiles(
        self,
        participant_ids: List[int],
        guild_id: Optional[int] = None,
    ) -> Dict[int, Dict[str, Any]]:
        snapshots = await self.get_author_context_snapshots(participant_ids, guild_id=guild_id)
        profiles: Dict[int, Dict[str, Any]] = {}
        for participant_id in participant_ids:
            stats = await self._get_author_live_update_stats(participant_id, guild_id=guild_id)
            profiles[participant_id] = {
                "author_id": participant_id,
                "snapshot": snapshots.get(participant_id, {}),
                "stats": stats,
            }
        return profiles

    async def search_live_update_feed_items(
        self,
        query: str,
        guild_id: Optional[int] = None,
        live_channel_id: Optional[int] = None,
        hours_back: int = 168,
        limit: int = 20,
        environment: str = 'prod',
    ) -> List[Dict[str, Any]]:
        """Search previous live-update feed items for the editor agent."""
        if not self.supabase_client:
            return []
        try:
            since = datetime.utcnow() - timedelta(hours=max(1, int(hours_back or 168)))
            safe_limit = max(1, min(int(limit or 20), 50))
            query_builder = (
                self.supabase_client.table('live_update_feed_items')
                .select('*')
                .eq('environment', environment)
                .gte('created_at', since.isoformat())
                .order('created_at', desc=True)
                .limit(safe_limit)
            )
            if guild_id is not None:
                query_builder = query_builder.eq('guild_id', guild_id)
            if live_channel_id is not None:
                query_builder = query_builder.eq('live_channel_id', live_channel_id)
            if query:
                query_builder = query_builder.or_(f"title.ilike.%{str(query)[:120]}%,body.ilike.%{str(query)[:120]}%")
            result = await asyncio.to_thread(query_builder.execute)
            return [
                {
                    "feed_item_id": row.get('feed_item_id'),
                    "title": row.get('title'),
                    "body": str(row.get('body') or '')[:700],
                    "update_type": row.get('update_type'),
                    "source_message_ids": self._as_json_array(row.get('source_message_ids')),
                    "duplicate_key": row.get('duplicate_key'),
                    "posted_at": row.get('posted_at') or row.get('created_at'),
                    "status": row.get('status'),
                }
                for row in (result.data or [])
            ]
        except Exception as e:
            logger.warning("Could not search live-update feed items: %s", e, exc_info=True)
            return []

    async def search_live_update_editorial_memory(
        self,
        query: str,
        guild_id: Optional[int] = None,
        limit: int = 20,
        environment: str = 'prod',
    ) -> List[Dict[str, Any]]:
        """Search editorial memory rows for the live-update editor agent."""
        if not self.supabase_client:
            return []
        try:
            safe_limit = max(1, min(int(limit or 20), 50))
            query_builder = (
                self.supabase_client.table('live_update_editorial_memory')
                .select('*')
                .eq('environment', environment)
                .order('last_seen_at', desc=True)
                .limit(safe_limit)
            )
            if guild_id is not None:
                query_builder = query_builder.eq('guild_id', guild_id)
            if query:
                query_builder = query_builder.or_(f"summary.ilike.%{str(query)[:120]}%,memory_key.ilike.%{str(query)[:120]}%")
            result = await asyncio.to_thread(query_builder.execute)
            return [
                {
                    "memory_key": row.get('memory_key'),
                    "subject_type": row.get('subject_type'),
                    "summary": row.get('summary'),
                    "importance": row.get('importance'),
                    "last_seen_at": row.get('last_seen_at'),
                    "state": self._as_json_object(row.get('state')),
                }
                for row in (result.data or [])
            ]
        except Exception as e:
            logger.warning("Could not search live-update editorial memory: %s", e, exc_info=True)
            return []

    async def upsert_live_update_editorial_memory(self, memory: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Upsert an editorial memory row."""
        payload = {
            'guild_id': memory.get('guild_id'),
            'environment': environment,
            'memory_key': memory.get('memory_key'),
            'subject_type': memory.get('subject_type') or 'general',
            'subject_id': memory.get('subject_id'),
            'summary': memory.get('summary') or '',
            'importance': memory.get('importance', 0),
            'state': self._as_json_object(memory.get('state')),
            'source_candidate_id': memory.get('source_candidate_id'),
            'source_feed_item_id': memory.get('source_feed_item_id'),
            'first_seen_at': memory.get('first_seen_at'),
            'last_seen_at': memory.get('last_seen_at') or datetime.utcnow().isoformat(),
            'expires_at': memory.get('expires_at'),
        }
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            query = (
                self.supabase_client.table('live_update_editorial_memory')
                .select('memory_id')
                .eq('memory_key', memory.get('memory_key'))
                .eq('environment', environment)
                .limit(1)
            )
            if memory.get('guild_id') is not None:
                query = query.eq('guild_id', memory.get('guild_id'))
            result = await asyncio.to_thread(query.execute)
            if result.data:
                return await self._update_live_row(
                    'live_update_editorial_memory',
                    'memory_id',
                    result.data[0]['memory_id'],
                    payload,
                )
            return await self._insert_live_row('live_update_editorial_memory', self._clean_payload(payload))
        except Exception as e:
            logger.error(f"Error upserting editorial memory: {e}", exc_info=True)
            return None

    async def get_live_update_editorial_memory(self, guild_id: Optional[int] = None, limit: int = 100, environment: str = 'prod') -> List[Dict[str, Any]]:
        """Fetch current editorial memory."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return []
        try:
            query = self.supabase_client.table('live_update_editorial_memory').select('*').eq('environment', environment).order('importance', desc=True).order('last_seen_at', desc=True).limit(limit)
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching editorial memory: {e}", exc_info=True)
            return []

    async def upsert_live_update_watchlist(self, watch: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Upsert a watchlist row."""
        payload = {
            'guild_id': watch.get('guild_id'),
            'environment': environment,
            'watch_key': watch.get('watch_key'),
            'subject_type': watch.get('subject_type') or 'general',
            'subject_id': watch.get('subject_id'),
            'criteria': self._as_json_object(watch.get('criteria')),
            'status': watch.get('status') or 'active',
            'priority': watch.get('priority', 0),
            'notes': watch.get('notes'),
            'last_matched_candidate_id': watch.get('last_matched_candidate_id'),
            'last_matched_at': watch.get('last_matched_at'),
        }
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            query = (
                self.supabase_client.table('live_update_watchlist')
                .select('watch_id')
                .eq('watch_key', watch.get('watch_key'))
                .eq('environment', environment)
                .limit(1)
            )
            if watch.get('guild_id') is not None:
                query = query.eq('guild_id', watch.get('guild_id'))
            result = await asyncio.to_thread(query.execute)
            if result.data:
                return await self._update_live_row(
                    'live_update_watchlist',
                    'watch_id',
                    result.data[0]['watch_id'],
                    payload,
                )
            return await self._insert_live_row('live_update_watchlist', self._clean_payload(payload))
        except Exception as e:
            logger.error(f"Error upserting watchlist row: {e}", exc_info=True)
            return None

    async def get_live_update_watchlist(self, guild_id: Optional[int] = None, limit: int = 200, environment: str = 'prod') -> List[Dict[str, Any]]:
        """Fetch watchlist rows for live-editor attention.

        Returns all rows without a status filter — the caller (db_handler)
        now handles filtering for status IN ('active','published').
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return []
        try:
            query = self.supabase_client.table('live_update_watchlist').select('*').eq('environment', environment).order('priority', desc=True).order('updated_at', desc=True).limit(limit)
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = await asyncio.to_thread(query.execute)
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching watchlist: {e}", exc_info=True)
            return []

    async def insert_live_update_watchlist(
        self,
        *,
        watch_key: str,
        title: str,
        origin_reason: str,
        source_message_ids: List[str],
        channel_id: Optional[int] = None,
        subject_type: str = 'general',
        environment: str = 'prod',
        guild_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Insert a watchlist row idempotently by (environment, watch_key).

        Sets expires_at = now()+72h, next_revisit_at = now()+6h, status='active',
        revisit_count=0, evidence jsonb snapshot of source_message_ids/channel/title.
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            # Idempotent: check if row already exists by (environment, watch_key)
            check_query = (
                self.supabase_client.table('live_update_watchlist')
                .select('watch_id')
                .eq('watch_key', watch_key)
                .eq('environment', environment)
                .limit(1)
            )
            check_result = await asyncio.to_thread(check_query.execute)
            if check_result.data:
                # Already exists — return existing row (idempotent)
                full = (
                    self.supabase_client.table('live_update_watchlist')
                    .select('*')
                    .eq('watch_id', check_result.data[0]['watch_id'])
                    .limit(1)
                )
                full_result = await asyncio.to_thread(full.execute)
                return full_result.data[0] if full_result.data else None

            now = datetime.utcnow().isoformat()
            expires_at = (datetime.utcnow() + timedelta(hours=72)).isoformat()
            next_revisit_at = (datetime.utcnow() + timedelta(hours=6)).isoformat()

            payload = self._clean_payload({
                'guild_id': guild_id,
                'environment': environment,
                'watch_key': watch_key,
                'subject_type': subject_type,
                'status': 'active',
                'priority': 0,
                'notes': title,
                'origin_reason': origin_reason,
                'evidence': json.dumps({
                    'source_message_ids': source_message_ids,
                    'channel_id': channel_id,
                    'title': title,
                    'origin_reason': origin_reason,
                    'created_at': now,
                }),
                'expires_at': expires_at,
                'next_revisit_at': next_revisit_at,
                'revisit_count': 0,
            })
            return await self._insert_live_row('live_update_watchlist', payload)
        except Exception as e:
            logger.error(f"Error inserting watchlist row: {e}", exc_info=True)
            return None

    async def update_live_update_watchlist(
        self,
        *,
        watch_key: str,
        action: str,
        notes: Optional[str] = None,
        environment: str = 'prod',
    ) -> Optional[Dict[str, Any]]:
        """Update a watchlist row with publish_now/extend/discard action.

        publish_now: sets status='published'.
        extend: bumps next_revisit_at = least(now()+6h, expires_at), increments revisit_count.
        discard: sets status='discarded', stores notes.
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            # Find the row by (environment, watch_key)
            find_query = (
                self.supabase_client.table('live_update_watchlist')
                .select('*')
                .eq('watch_key', watch_key)
                .eq('environment', environment)
                .limit(1)
            )
            find_result = await asyncio.to_thread(find_query.execute)
            if not find_result.data:
                logger.warning(f"Watchlist row not found for watch_key={watch_key} env={environment}")
                return None

            row = find_result.data[0]
            watch_id = row['watch_id']

            if action == 'publish_now':
                update_payload = self._clean_payload({
                    'status': 'published',
                    'notes': notes,
                })
            elif action == 'extend':
                now = datetime.utcnow()
                expires_at_str = row.get('expires_at')
                if expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                    if expires_at.tzinfo is not None:
                        expires_at = expires_at.replace(tzinfo=None)
                else:
                    expires_at = now + timedelta(hours=72)

                candidate_revisit = now + timedelta(hours=6)
                next_revisit = candidate_revisit if candidate_revisit < expires_at else expires_at
                update_payload = self._clean_payload({
                    'next_revisit_at': next_revisit.isoformat(),
                    'revisit_count': (row.get('revisit_count') or 0) + 1,
                    'notes': notes,
                })
            elif action == 'discard':
                update_payload = self._clean_payload({
                    'status': 'discarded',
                    'notes': notes,
                })
            else:
                logger.warning(f"Unknown watchlist action: {action}")
                return None

            return await self._update_live_row('live_update_watchlist', 'watch_id', watch_id, update_payload)
        except Exception as e:
            logger.error(f"Error updating watchlist row: {e}", exc_info=True)
            return None

    async def create_live_top_creation_run(self, run: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Create an independent top-creations audit run."""
        return await self._insert_live_row('live_top_creation_runs', self._clean_payload({
            'guild_id': run.get('guild_id'),
            'environment': environment,
            'trigger': run.get('trigger') or 'scheduled',
            'status': run.get('status') or 'running',
            'checkpoint_before': self._as_json_object(run.get('checkpoint_before')),
            'checkpoint_after': self._as_json_object(run.get('checkpoint_after')),
            'candidate_count': run.get('candidate_count', 0),
            'posted_count': run.get('posted_count', 0),
            'skipped_count': run.get('skipped_count', 0),
            'error_message': run.get('error_message'),
            'metadata': self._as_json_object(run.get('metadata')),
            'completed_at': run.get('completed_at'),
        }))

    async def update_live_top_creation_run(self, run_id: str, updates: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        payload = dict(updates or {})
        payload['environment'] = environment
        for key in ('checkpoint_before', 'checkpoint_after', 'metadata'):
            if key in payload:
                payload[key] = self._as_json_object(payload[key])
        return await self._update_live_row('live_top_creation_runs', 'top_creation_run_id', run_id, payload)

    async def store_live_top_creation_post(self, post: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        """Persist a top-creations post with ordered Discord message IDs."""
        payload = {
            'top_creation_run_id': post.get('top_creation_run_id'),
            'environment': environment,
            'guild_id': post.get('guild_id'),
            'source_kind': post.get('source_kind') or 'unknown',
            'source_id': str(post.get('source_id') or 'unknown'),
            'source_message_id': post.get('source_message_id'),
            'source_channel_id': post.get('source_channel_id'),
            'live_channel_id': post.get('live_channel_id'),
            'title': post.get('title'),
            'body': post.get('body'),
            'media_refs': self._as_json_array(post.get('media_refs')),
            'duplicate_key': post.get('duplicate_key') or str(post.get('source_id') or 'unknown'),
            'discord_message_ids': [str(mid) for mid in self._as_json_array(post.get('discord_message_ids'))],
            'status': post.get('status') or 'posted',
            'post_error': post.get('post_error'),
            'posted_at': post.get('posted_at') or datetime.utcnow().isoformat(),
            'metadata': self._as_json_object(post.get('metadata')),
        }
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            query = (
                self.supabase_client.table('live_top_creation_posts')
                .select('top_creation_post_id')
                .eq('duplicate_key', payload['duplicate_key'])
                .eq('environment', environment)
                .limit(1)
            )
            if post.get('guild_id') is not None:
                query = query.eq('guild_id', post.get('guild_id'))
            result = await asyncio.to_thread(query.execute)
            if result.data:
                return await self._update_live_row(
                    'live_top_creation_posts',
                    'top_creation_post_id',
                    result.data[0]['top_creation_post_id'],
                    payload,
                )
            return await self._insert_live_row('live_top_creation_posts', self._clean_payload(payload))
        except Exception as e:
            logger.error(f"Error storing top-creations post: {e}", exc_info=True)
            return None

    async def get_live_top_creation_post_by_duplicate_key(self, environment: str, duplicate_key: str) -> Optional[Dict[str, Any]]:
        """Check if a top-creation post with this duplicate_key already exists (for pre-publish dedupe)."""
        if not self.supabase_client or not duplicate_key:
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table('live_top_creation_posts')
                .select('*')
                .eq('environment', environment)
                .eq('duplicate_key', duplicate_key)
                .eq('status', 'posted')
                .order('created_at', desc=True)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error looking up top-creation post by duplicate key {duplicate_key}: {e}", exc_info=True)
            return None

    async def get_live_top_creation_checkpoint(self, checkpoint_key: str, environment: str = 'prod') -> Optional[Dict[str, Any]]:
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table('live_top_creation_checkpoints')
                .select('*')
                .eq('checkpoint_key', checkpoint_key)
                .eq('environment', environment)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error reading top-creations checkpoint {checkpoint_key}: {e}", exc_info=True)
            return None

    async def upsert_live_top_creation_checkpoint(self, checkpoint: Dict[str, Any], environment: str = 'prod') -> Optional[Dict[str, Any]]:
        return await self._upsert_live_row('live_top_creation_checkpoints', {
            'checkpoint_key': checkpoint.get('checkpoint_key'),
            'environment': environment,
            'guild_id': checkpoint.get('guild_id'),
            'channel_id': checkpoint.get('channel_id'),
            'last_source_id': checkpoint.get('last_source_id'),
            'last_source_message_id': checkpoint.get('last_source_message_id'),
            'last_source_created_at': checkpoint.get('last_source_created_at'),
            'last_run_id': checkpoint.get('last_run_id'),
            'state': self._as_json_object(checkpoint.get('state')),
        }, on_conflict='environment,checkpoint_key')

    # ========== Media Storage Methods ==========
    
    SUMMARY_MEDIA_BUCKET = "summary-media"
    MAX_UPLOAD_ATTEMPTS = 3
    BASE_RETRY_DELAY = 1.0

    async def upload_bytes_to_storage(
        self,
        file_bytes: bytes,
        storage_path: str,
        content_type: str,
        bucket_name: Optional[str] = None
    ) -> Optional[str]:
        """
        Upload raw bytes to Supabase Storage with retry logic.
        
        Args:
            file_bytes: The raw bytes to upload
            storage_path: Path within the bucket (e.g., "2025-12-27/1234567890_0.mp4")
            content_type: MIME type of the file
            bucket_name: Target bucket (defaults to SUMMARY_MEDIA_BUCKET)
            
        Returns:
            Public URL of the uploaded file, or None on failure
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized for storage upload")
            return None
        
        bucket = bucket_name or self.SUMMARY_MEDIA_BUCKET
        
        for attempt in range(self.MAX_UPLOAD_ATTEMPTS):
            try:
                await asyncio.to_thread(
                    self.supabase_client.storage.from_(bucket).upload,
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": content_type, "upsert": "true"}
                )
                logger.debug(f"Uploaded {len(file_bytes)} bytes to {bucket}/{storage_path}")
                
                # Get public URL
                public_url = await asyncio.to_thread(
                    self.supabase_client.storage.from_(bucket).get_public_url,
                    storage_path
                )
                
                if public_url and isinstance(public_url, str):
                    return public_url.strip()
                    
                logger.warning(f"Got invalid URL after upload: {public_url}")
                return None
                
            except Exception as e:
                logger.warning(f"Upload attempt {attempt + 1}/{self.MAX_UPLOAD_ATTEMPTS} failed: {e}")
                if attempt + 1 < self.MAX_UPLOAD_ATTEMPTS:
                    await asyncio.sleep(self.BASE_RETRY_DELAY * (2 ** attempt))
                else:
                    logger.error(f"Upload to {bucket}/{storage_path} failed after {self.MAX_UPLOAD_ATTEMPTS} attempts")
        
        return None

    async def download_file(self, source_url: str) -> Optional[Dict[str, any]]:
        """
        Download a file from a URL.
        
        Args:
            source_url: URL to download from (e.g., Discord CDN URL)
            
        Returns:
            Dict with 'bytes', 'content_type', 'filename' or None on failure
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(source_url, timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status != 200:
                        logger.warning(f"Failed to download {source_url}: HTTP {response.status}")
                        return None
                    
                    file_bytes = await response.read()
                    
                    # Determine content type from response or URL
                    content_type = response.content_type
                    if not content_type or content_type == 'application/octet-stream':
                        guessed_type, _ = mimetypes.guess_type(source_url.split('?')[0])
                        content_type = guessed_type or 'application/octet-stream'
                    
                    # Extract filename from URL
                    url_path = source_url.split('?')[0]
                    filename = url_path.split('/')[-1] if '/' in url_path else 'file'
                    
                    logger.debug(f"Downloaded {len(file_bytes)} bytes ({content_type}) from {source_url[:80]}...")
                    
                    return {
                        'bytes': file_bytes,
                        'content_type': content_type,
                        'filename': filename
                    }
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout downloading {source_url}")
            return None
        except Exception as e:
            logger.error(f"Error downloading {source_url}: {e}", exc_info=True)
            return None

    async def download_and_upload_url(
        self,
        source_url: str,
        storage_path: str,
        bucket_name: Optional[str] = None
    ) -> Optional[str]:
        """
        Download a file from a URL and upload to Supabase Storage.
        
        Args:
            source_url: URL to download from (e.g., Discord CDN URL)
            storage_path: Path within the bucket
            bucket_name: Target bucket (defaults to SUMMARY_MEDIA_BUCKET)
            
        Returns:
            Public URL of the uploaded file, or None on failure
        """
        file_data = await self.download_file(source_url)
        if not file_data:
            return None

        return await self.upload_bytes_to_storage(
            file_data['bytes'], storage_path, file_data['content_type'], bucket_name
        )

    # ---------- message_media_understandings ----------------------

    async def get_message_media_understanding(
        self,
        message_id: int,
        attachment_index: int = 0,
        model: str = '',
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single media understanding row by primary key."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table('message_media_understandings')
                .select('*')
                .eq('message_id', message_id)
                .eq('attachment_index', attachment_index)
                .eq('model', model)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error fetching media understanding for message_id=%s "
                "attachment_index=%s model=%s: %s",
                message_id, attachment_index, model, e,
                exc_info=True,
            )
            return None

    async def get_message_media_understanding_by_hash(
        self,
        content_hash: str,
        model: Optional[str] = None,
        media_kind: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a media understanding by content_hash, newest first."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            query = (
                self.supabase_client.table('message_media_understandings')
                .select('*')
                .eq('content_hash', content_hash)
                .order('created_at', desc=True)
                .limit(1)
            )
            if model is not None:
                query = query.eq('model', model)
            if media_kind is not None:
                query = query.eq('media_kind', media_kind)
            result = await asyncio.to_thread(query.execute)
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error fetching media understanding by hash %s: %s",
                content_hash, e,
                exc_info=True,
            )
            return None

    async def upsert_message_media_understanding(
        self,
        row: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Upsert a media understanding row by PK (message_id, attachment_index, model)."""
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            payload = {
                'message_id': row.get('message_id'),
                'attachment_index': row.get('attachment_index', 0),
                'media_url': row.get('media_url'),
                'media_kind': row.get('media_kind'),
                'content_hash': row.get('content_hash'),
                'model': row.get('model'),
                'understanding': self._as_json_object(row.get('understanding')),
            }
            result = await asyncio.to_thread(
                self.supabase_client.table('message_media_understandings')
                .upsert(
                    self._clean_payload(payload),
                    on_conflict='message_id,attachment_index,model',
                )
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error upserting media understanding for message_id=%s: %s",
                row.get('message_id'), e,
                exc_info=True,
            )
            return None

    # ---------- external_media_cache -----------------------------------

    async def get_external_media_cache(
        self,
        url_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a cached external-media resolution row by url_key.

        Returns None on cache miss (not an exception).
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            result = await asyncio.to_thread(
                self.supabase_client.table('external_media_cache')
                .select('*')
                .eq('url_key', url_key)
                .limit(1)
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error fetching external_media_cache for url_key=%s: %s",
                url_key, e,
                exc_info=True,
            )
            return None

    async def upsert_external_media_cache(
        self,
        row: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Upsert an external-media cache row by PK (url_key).

        Uses ON CONFLICT (url_key) DO UPDATE so repeated resolver runs
        update the same row rather than inserting duplicates.
        """
        if not self.supabase_client:
            logger.error("Supabase client not initialized")
            return None
        try:
            payload = {
                'url_key': row.get('url_key'),
                'source_url_sanitized': row.get('source_url_sanitized'),
                'source_domain': row.get('source_domain'),
                'status': row.get('status'),
                'content_hash': row.get('content_hash'),
                'media_kind': row.get('media_kind'),
                'content_type': row.get('content_type'),
                'byte_size': row.get('byte_size'),
                'file_path': row.get('file_path'),
                'resolved_url_sanitized': row.get('resolved_url_sanitized'),
                'failure_reason': row.get('failure_reason'),
                'metadata': self._as_json_object(row.get('metadata')),
                'created_at': row.get('created_at'),
                'updated_at': row.get('updated_at'),
            }
            result = await asyncio.to_thread(
                self.supabase_client.table('external_media_cache')
                .upsert(
                    self._clean_payload(payload),
                    on_conflict='url_key',
                )
                .execute
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error upserting external_media_cache for url_key=%s: %s",
                row.get('url_key'), e,
                exc_info=True,
            )
            return None
