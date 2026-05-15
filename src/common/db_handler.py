import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple
import asyncio

from .redaction import redact_wallet as _redact_wallet

logger = logging.getLogger('DiscordBot')


class WalletUpdateBlockedError(Exception):
    """Raised when a wallet change is attempted while a payment flow is still active."""

def to_aware_utc(dt_str: str) -> Optional[datetime]:
    """Convert an ISO format string to a timezone-aware datetime object in UTC."""
    if not dt_str:
        return None
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

class DatabaseHandler:
    def __init__(self, db_path: Optional[str] = None, dev_mode: bool = False, pool_size: int = 5, storage_backend: Optional[str] = None):
        """Initialize database handler with Supabase backend."""
        try:
            self.dev_mode = dev_mode

            # Initialize Supabase handlers
            self.storage_handler = None
            self.supabase = None
            self.query_handler = None
            self.server_config = None
            try:
                from .storage_handler import StorageHandler
                from .supabase_query_handler import SupabaseQueryHandler
                from .server_config import ServerConfig
                self.storage_handler = StorageHandler()
                self.supabase = self.storage_handler.supabase_client
                # Use the same Supabase client for queries
                self.query_handler = SupabaseQueryHandler(self.supabase)
                # ServerConfig shares the same Supabase client
                self.server_config = ServerConfig(self.supabase)
                logger.debug(f"Supabase handlers initialized for read/write operations")
            except Exception as e:
                logger.error(f"Failed to initialize Supabase handlers: {e}", exc_info=True)
                raise

        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def _run_async_in_thread(self, coro):
        """Helper to run async operations from sync context."""
        try:
            # Check if we're already in an async context
            try:
                asyncio.get_running_loop()
                # We're in an async context - need to run in a separate thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, coro)
                    return future.result(timeout=30)
            except RuntimeError:
                # Not in an async context, safe to use asyncio.run
                return asyncio.run(coro)
        except Exception as e:
            logger.error(f"Error running async operation: {e}", exc_info=True)
            raise

    def close(self):
        """Close the database connection (no-op for Supabase)."""
        pass

    def __del__(self):
        """Ensure connection is closed when object is destroyed."""
        self.close()

    def execute_query(self, query: str, params: tuple = ()) -> List[dict]:
        """
        Execute a raw SQL query via Supabase.
        """
        try:
            logger.info(f"🔄 [DB HANDLER] Routing query to SUPABASE")
            logger.info(f"🔄 [DB HANDLER] Query preview: {query[:200]}")
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(query, params if params else None)
            )
            logger.info(f"✅ [DB HANDLER] Supabase returned {len(result)} results")
            return result
        except Exception as e:
            logger.error(f"❌ [DB HANDLER] Supabase query failed: {e}")
            raise

    async def store_messages(self, messages: List[Dict]):
        """Store messages to Supabase.

        Messages must include a 'guild_id' key. Writes are allowed only for
        writable guilds in server_config.
        """
        if self.storage_handler:
            messages = [
                m for m in messages
                if m.get('guild_id') is not None
                and self.server_config
                and self.server_config.is_write_allowed(m.get('guild_id'))
            ]
            if messages:
                await self.storage_handler.store_messages_to_supabase(messages)

    def get_last_message_id(self, channel_id: int) -> Optional[int]:
        """Get the most recent message ID for a channel."""
        try:
            return self._run_async_in_thread(
                self.query_handler.get_last_message_id(channel_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def search_messages(self, query: str, channel_id: Optional[int] = None,
                        guild_id: Optional[int] = None) -> List[Dict]:
        """Search messages by content using the guild-aware query handler."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.search_messages(query, channel_id=channel_id, guild_id=guild_id)
            )
            return result
        except Exception as e:
            logger.error(f"Supabase query failed for search_messages: {e}")
            return []

    def get_summary_for_date(self, channel_id: int, date: Optional[datetime] = None) -> Optional[str]:
        """Get the full summary for a channel on a given date."""
        if self.storage_handler:
            return self._run_async_in_thread(
                self.storage_handler.get_summary_for_date(channel_id, date, self.dev_mode)
            )
        return None

    def summary_exists_for_date(self, channel_id: int, date: Optional[datetime] = None) -> bool:
        """Check if a summary already exists for a channel on a given date."""
        if self.storage_handler:
            return self._run_async_in_thread(
                self.storage_handler.summary_exists_for_date(channel_id, date, self.dev_mode)
            )
        return False

    def store_daily_summary(
        self,
        channel_id: int,
        full_summary: Optional[str],
        short_summary: Optional[str],
        date: Optional[datetime] = None,
        included_in_main_summary: bool = False,
        dev_mode: bool = False,
        guild_id: Optional[int] = None
    ) -> bool:
        """Store a daily summary to Supabase."""
        if not self._gate_check(guild_id):
            return False
        if self.storage_handler:
            logger.info(f"Storing summary to Supabase for channel {channel_id} (dev_mode={dev_mode})")
            supabase_result = self._run_async_in_thread(
                self.storage_handler.store_daily_summary_to_supabase(
                    channel_id, full_summary, short_summary, date,
                    included_in_main_summary, dev_mode, guild_id=guild_id
                )
            )
            if not supabase_result:
                logger.error(f"Failed to store summary to Supabase for channel {channel_id}")
                return False
            return True
        else:
            logger.warning("Storage handler not initialized, cannot store to Supabase")
            return False

    def mark_summaries_included_in_main(self, date: datetime, channel_message_ids: Dict[int, List[str]]) -> bool:
        """Mark channel summaries as having items included in the main summary."""
        if self.storage_handler:
            logger.info(f"Marking {len(channel_message_ids)} channel summaries as included in main summary")
            return self._run_async_in_thread(
                self.storage_handler.mark_summaries_included_in_main(date, channel_message_ids, self.dev_mode)
            )
        else:
            logger.warning("Storage handler not initialized, cannot mark summaries")
            return False

    async def download_and_upload_media(self, source_url: str, storage_path: str) -> Optional[str]:
        """Download media from URL and upload to Supabase Storage."""
        if self.storage_handler:
            return await self.storage_handler.download_and_upload_url(source_url, storage_path)
        else:
            logger.warning("Storage handler not initialized, cannot upload media")
            return None

    async def download_file(self, source_url: str) -> Optional[Dict[str, any]]:
        """Download a file and return bytes + metadata."""
        if self.storage_handler:
            return await self.storage_handler.download_file(source_url)
        else:
            logger.warning("Storage handler not initialized, cannot download file")
            return None

    async def upload_bytes(self, file_bytes: bytes, storage_path: str, content_type: str) -> Optional[str]:
        """Upload raw bytes to Supabase Storage."""
        if self.storage_handler:
            return await self.storage_handler.upload_bytes_to_storage(file_bytes, storage_path, content_type)
        else:
            logger.warning("Storage handler not initialized, cannot upload bytes")
            return None

    def update_channel_summary_full_summary(self, channel_id: int, date: datetime, full_summary: str) -> bool:
        """Update the full_summary for a channel's daily summary (used to add inclusion flags and media URLs)."""
        if self.storage_handler:
            return self._run_async_in_thread(
                self.storage_handler.update_channel_summary_full_summary(channel_id, date, full_summary, self.dev_mode)
            )
        else:
            logger.warning("Storage handler not initialized, cannot update full_summary")
            return False

    def get_summary_thread_id(self, channel_id: int) -> Optional[int]:
        """Get the summary thread ID for a channel."""
        if self.query_handler:
            try:
                logger.debug(f"Fetching summary thread ID from Supabase for channel {channel_id}")
                return self._run_async_in_thread(
                    self.query_handler.get_summary_thread_id(channel_id)
                )
            except Exception as e:
                logger.error(f"Failed to get summary thread ID from Supabase: {e}")
                raise
        return None

    def update_summary_thread(self, channel_id: int, thread_id: Optional[int]):
        """Update the summary thread ID for a channel."""
        if self.storage_handler:
            logger.debug(f"Updating summary thread ID in Supabase for channel {channel_id}: {thread_id}")
            self._run_async_in_thread(
                self.storage_handler.update_summary_thread_to_supabase(channel_id, thread_id)
            )

    def get_all_message_ids(self, channel_id: int) -> List[int]:
        """Get all message IDs for a channel."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    "SELECT message_id FROM discord_messages WHERE channel_id = ?",
                    (channel_id,)
                )
            )
            return [row.get('message_id') for row in result if row.get('message_id')]
        except Exception as e:
            logger.error(f"Supabase query failed for get_all_message_ids: {e}")
            return []

    def get_message_date_range(self, channel_id: int) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Get the date range of messages in a channel."""
        try:
            return self._run_async_in_thread(
                self.query_handler.get_message_date_range(channel_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed for get_message_date_range: {e}")
            raise

    def get_message_dates(self, channel_id: int) -> List[str]:
        """Get distinct message dates for a channel."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    "SELECT DISTINCT DATE(created_at) as date FROM discord_messages WHERE channel_id = ? ORDER BY date",
                    (channel_id,)
                )
            )
            return [row.get('date') for row in result if row.get('date')]
        except Exception as e:
            logger.error(f"Supabase query failed for get_message_dates: {e}")
            return []

    def get_member(self, member_id: int) -> Optional[Dict]:
        """Fetch a member from the database by their ID."""
        try:
            return self._run_async_in_thread(
                self.query_handler.get_member(member_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def message_exists(self, message_id: int) -> bool:
        """Check if a message exists."""
        try:
            return self._run_async_in_thread(
                self.query_handler.message_exists(message_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def update_message(self, message: Dict) -> bool:
        """Update a message in Supabase."""
        guild_id = message.get('guild_id')
        if guild_id is None and message.get('message_id') is not None:
            guild_id = self._resolve_message_guild_id(message['message_id'])
        if guild_id is None and message.get('channel_id') is not None:
            guild_id = self._resolve_channel_guild_id(message['channel_id'])
        if guild_id is None and message.get('thread_id') is not None:
            guild_id = self._resolve_channel_guild_id(message['thread_id'])
        if guild_id is not None:
            message = dict(message)
            message['guild_id'] = guild_id
        if not self._gate_check(guild_id):
            return False
        try:
            stored = self._run_async_in_thread(
                self.storage_handler.store_messages_to_supabase([message])
            )
            return stored > 0
        except Exception as e:
            logger.error(f"Error updating message in Supabase: {e}", exc_info=True)
            return False

    def create_or_update_member(self, member_id: int, username: str, display_name: Optional[str] = None,
                              global_name: Optional[str] = None, avatar_url: Optional[str] = None,
                              discriminator: Optional[str] = None, bot: bool = False,
                              system: bool = False, accent_color: Optional[int] = None,
                              banner_url: Optional[str] = None, discord_created_at: Optional[str] = None,
                              guild_join_date: Optional[str] = None, role_ids: Optional[str] = None,
                              twitter_url: Optional[str] = None, reddit_url: Optional[str] = None,
                              include_in_updates: Optional[bool] = None,
                              allow_content_sharing: Optional[bool] = None,
                              guild_id: Optional[int] = None) -> bool:
        """Create or update a member in Supabase.

        Permission fields (include_in_updates, allow_content_sharing) default to TRUE in the database.
        Only pass explicit values when the user has made a choice.

        If guild_id is provided, also dual-writes nick/join/roles to guild_members.
        """
        if not self._gate_check(guild_id):
            return False

        member_data: Dict[str, Any] = {
            'member_id': member_id,
            'username': username,
            'global_name': global_name,
            'server_nick': display_name,
            'avatar_url': avatar_url,
            'discriminator': discriminator,
            'bot': bot,
            'system': system,
            'accent_color': accent_color,
            'banner_url': banner_url,
            'discord_created_at': discord_created_at,
            'guild_join_date': guild_join_date,
            'role_ids': role_ids,
            'twitter_url': twitter_url,
            'reddit_url': reddit_url,
            'updated_at': datetime.now().isoformat()
        }

        # IMPORTANT: Do not send NULL for these fields during routine member syncs.
        # If we upsert NULL, we override the DB defaults (TRUE) and can also wipe
        # previously-set preferences. Only include these keys when the user has
        # explicitly made a choice (True/False).
        if include_in_updates is not None:
            member_data['include_in_updates'] = include_in_updates
        if allow_content_sharing is not None:
            member_data['allow_content_sharing'] = allow_content_sharing

        if self.storage_handler:
            try:
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                # Dual-write to guild_members if guild_id provided
                if guild_id and stored > 0:
                    self._upsert_guild_member(guild_id, member_id, display_name, guild_join_date, role_ids)
                return stored > 0
            except Exception as e:
                logger.error(f"Error storing member to Supabase: {e}", exc_info=True)
                return False
        return False

    def _upsert_guild_member(self, guild_id: int, member_id: int,
                             server_nick: Optional[str] = None,
                             guild_join_date: Optional[str] = None,
                             role_ids: Optional[str] = None) -> bool:
        """Upsert nick/join/roles into guild_members table."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            import json as _json
            # role_ids may be a JSON string or a list
            parsed_roles = role_ids
            if isinstance(role_ids, str):
                try:
                    parsed_roles = _json.loads(role_ids)
                except (ValueError, TypeError):
                    parsed_roles = None

            data = {
                'guild_id': guild_id,
                'member_id': member_id,
                'server_nick': server_nick,
                'guild_join_date': guild_join_date,
                'role_ids': parsed_roles,
                'updated_at': datetime.now().isoformat(),
            }
            self.storage_handler.supabase_client.table('guild_members').upsert(data).execute()
            return True
        except Exception as e:
            logger.debug(f"Error upserting guild_member ({guild_id}, {member_id}): {e}")
            return False

    def update_member_sharing_permission(self, member_id: int, allow_content_sharing: bool,
                                           guild_id: Optional[int] = None) -> bool:
        """Update member's content sharing permission in Supabase.
        
        Args:
            member_id: Discord member ID
            allow_content_sharing: Whether the user allows their content to be shared
            
        Returns:
            True if update succeeded, False otherwise
        """
        if not self._gate_check(guild_id):
            return False
        if self.storage_handler:
            try:
                member_data = {
                    'member_id': member_id,
                    'allow_content_sharing': allow_content_sharing,
                    'updated_at': datetime.now().isoformat()
                }
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error updating member sharing permission in Supabase: {e}", exc_info=True)
                return False
        return False

    def update_member_updates_permission(self, member_id: int, include_in_updates: bool,
                                            guild_id: Optional[int] = None) -> bool:
        """Update member's include in updates permission in Supabase.
        
        Args:
            member_id: Discord member ID
            include_in_updates: Whether the user allows being mentioned in summaries/digests
            
        Returns:
            True if update succeeded, False otherwise
        """
        if not self._gate_check(guild_id):
            return False
        if self.storage_handler:
            try:
                member_data = {
                    'member_id': member_id,
                    'include_in_updates': include_in_updates,
                    'updated_at': datetime.now().isoformat()
                }
                stored = self._run_async_in_thread(
                    self.storage_handler.store_members_to_supabase([member_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error updating member updates permission in Supabase: {e}", exc_info=True)
                return False
        return False

    def update_member_stored_avatar(self, member_id: int, stored_avatar_url: str,
                                       guild_id: Optional[int] = None) -> bool:
        """Save a permanent avatar URL for a member."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('members').update({
                'stored_avatar_url': stored_avatar_url,
            }).eq('member_id', member_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating stored avatar for member {member_id}: {e}", exc_info=True)
            return False

    def update_member_socials(self, member_id: int,
                              twitter_url: Optional[str] = None,
                              reddit_url: Optional[str] = None) -> bool:
        """Update only the social handles for a member without touching anything else.

        Pass an explicit empty string to clear a field. Pass None (the default)
        to leave the field unchanged. Direct .update().eq() so we never
        accidentally overwrite permission columns or other member data.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        update_data: Dict[str, Any] = {}
        if twitter_url is not None:
            update_data['twitter_url'] = twitter_url or None
        if reddit_url is not None:
            update_data['reddit_url'] = reddit_url or None
        if not update_data:
            return False
        update_data['updated_at'] = datetime.now().isoformat()
        try:
            result = (
                self.storage_handler.supabase_client.table('members')
                .update(update_data)
                .eq('member_id', member_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating socials for member {member_id}: {e}", exc_info=True)
            return False

    # ========== Helpers ==========

    def _resolve_message_guild_id(self, message_id: int) -> Optional[int]:
        """Look up the guild_id for a message. Returns None if not found."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('discord_messages')
                .select('guild_id')
                .eq('message_id', message_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('guild_id')
        except Exception:
            pass
        return None

    def _resolve_channel_guild_id(self, channel_id: int) -> Optional[int]:
        """Look up the guild_id for a channel/thread. Returns None if not found."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('discord_channels')
                .select('guild_id')
                .eq('channel_id', channel_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('guild_id')
        except Exception:
            pass
        return None

    def _gate_check(self, guild_id: Optional[int]) -> bool:
        """Return True if write is allowed for this guild_id.

        Fail-closed: writes are only allowed for writable guilds in server_config.
        """
        if guild_id is None or not self.server_config:
            return False
        return self.server_config.is_write_allowed(guild_id)

    # ========== Reaction Updates ==========

    def soft_delete_message(self, message_id: int, guild_id: Optional[int] = None) -> bool:
        """Soft-delete a message by setting is_deleted and deleted_at."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for soft_delete_message")
            return False

        # Resolve guild_id if not provided, then gate-check
        if guild_id is None:
            guild_id = self._resolve_message_guild_id(message_id)
        if not self._gate_check(guild_id):
            return False

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_messages')
                .update({
                    'is_deleted': True,
                    'deleted_at': datetime.now(timezone.utc).isoformat(),
                })
                .eq('message_id', message_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error soft-deleting message {message_id}: {e}")
            return False

    def update_reactions(self, message_id: int, reaction_count: int, reactors: list,
                         guild_id: Optional[int] = None) -> bool:
        """Update reaction data for a message via Supabase REST API.

        Bypasses execute_raw_sql() which cannot route UPDATE statements.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for update_reactions")
            return False

        if guild_id is None:
            guild_id = self._resolve_message_guild_id(message_id)
        if not self._gate_check(guild_id):
            return False

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_messages')
                .update({'reaction_count': reaction_count, 'reactors': reactors})
                .eq('message_id', message_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating reactions for message {message_id}: {e}")
            return False

    def add_reaction(self, message_id: int, user_id: int, emoji_str: str,
                     guild_id: Optional[int] = None) -> bool:
        """Upsert a single row into discord_reactions."""
        if guild_id is None:
            guild_id = self._resolve_message_guild_id(message_id)
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for add_reaction")
            return False

        try:
            data = {
                'message_id': message_id,
                'user_id': user_id,
                'emoji': emoji_str,
                'removed_at': None,
            }
            if guild_id is not None:
                data['guild_id'] = guild_id
            self.storage_handler.supabase_client.table('discord_reactions').upsert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error adding reaction for message {message_id}: {e}")
            return False

    def remove_reaction(self, message_id: int, user_id: int, emoji_str: str,
                        guild_id: Optional[int] = None) -> bool:
        """Soft-delete a reaction by setting removed_at."""
        if guild_id is None:
            guild_id = self._resolve_message_guild_id(message_id)
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for remove_reaction")
            return False

        try:
            self.storage_handler.supabase_client.table('discord_reactions') \
                .update({'removed_at': datetime.now(timezone.utc).isoformat()}) \
                .eq('message_id', message_id) \
                .eq('user_id', user_id) \
                .eq('emoji', emoji_str) \
                .execute()
            return True
        except Exception as e:
            logger.error(f"Error removing reaction for message {message_id}: {e}")
            return False

    def log_reaction_event(self, message_id: int, user_id: int, emoji_str: str, action: str,
                           guild_id: Optional[int] = None) -> bool:
        """Append an event to the discord_reaction_log table (pure history)."""
        if guild_id is None:
            guild_id = self._resolve_message_guild_id(message_id)
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            data = {
                'message_id': message_id,
                'user_id': user_id,
                'emoji': emoji_str,
                'action': action,
            }
            if guild_id is not None:
                data['guild_id'] = guild_id
            self.storage_handler.supabase_client.table('discord_reaction_log').insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error logging reaction event for message {message_id}: {e}")
            return False

    def record_moderation_decision(
        self,
        *,
        message_id: int,
        channel_id: Optional[int],
        guild_id: Optional[int],
        reactor_user_id: Optional[int],
        reactor_name: Optional[str],
        emoji: str,
        message_author_id: Optional[int],
        message_author_name: Optional[str],
        message_content_snippet: Optional[str],
        classification: str,
        reason: Optional[str] = None,
        is_suspicious: bool = False,
    ) -> bool:
        """Insert a moderation decision row for a reaction removal."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            data = {
                'message_id': message_id,
                'channel_id': channel_id,
                'guild_id': guild_id,
                'reactor_user_id': reactor_user_id,
                'reactor_name': reactor_name,
                'emoji': emoji,
                'message_author_id': message_author_id,
                'message_author_name': message_author_name,
                'message_content_snippet': message_content_snippet,
                'classification': classification,
                'reason': reason,
                'is_suspicious': is_suspicious,
            }
            self.storage_handler.supabase_client.table('moderation_decisions').insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error recording moderation decision for message {message_id}: {e}")
            return False

    def get_active_reactors(self, message_id: int, emoji: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return active reaction rows for a message."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for get_active_reactors")
            return []

        try:
            query = (
                self.storage_handler.supabase_client.table('discord_reactions')
                .select('user_id, emoji, guild_id')
                .eq('message_id', message_id)
                .is_('removed_at', 'null')
            )
            if emoji is not None:
                query = query.eq('emoji', emoji)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching active reactors for message {message_id}: {e}")
            return []

    def get_message_snapshot(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Return a message snapshot for moderation-decision logging."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for get_message_snapshot")
            return None

        try:
            result = (
                self.storage_handler.supabase_client.table('discord_messages')
                .select('author_id, author_name, content, channel_id, guild_id')
                .eq('message_id', message_id)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            return result.data[0]
        except Exception as e:
            logger.error(f"Error fetching message snapshot for message {message_id}: {e}")
            return None

    def upsert_reactions_batch(self, message_id: int, rows: list,
                              guild_id: Optional[int] = None) -> bool:
        """Sync granular reactions for a message.

        Upserts current rows (clearing removed_at), then soft-deletes any
        existing active rows that are no longer present.
        """
        if guild_id is None:
            guild_id = self._resolve_message_guild_id(message_id)
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for upsert_reactions_batch")
            return False

        sb = self.storage_handler.supabase_client
        try:
            # Upsert current reactions (mark as active), include guild_id if available
            for i in range(0, len(rows), 100):
                batch = [dict(r, removed_at=None) for r in rows[i:i + 100]]
                if guild_id is not None:
                    batch = [dict(r, guild_id=guild_id) for r in batch]
                sb.table('discord_reactions').upsert(batch).execute()

            # Soft-delete any active rows for this message not in the current set
            current_keys = {(r['user_id'], r['emoji']) for r in rows}
            existing = sb.table('discord_reactions') \
                .select('user_id, emoji') \
                .eq('message_id', message_id) \
                .is_('removed_at', 'null') \
                .execute()

            now = datetime.now(timezone.utc).isoformat()
            for row in (existing.data or []):
                if (row['user_id'], row['emoji']) not in current_keys:
                    sb.table('discord_reactions') \
                        .update({'removed_at': now}) \
                        .eq('message_id', message_id) \
                        .eq('user_id', row['user_id']) \
                        .eq('emoji', row['emoji']) \
                        .execute()

            return True
        except Exception as e:
            logger.error(f"Error upserting reaction batch for message {message_id}: {e}")
            return False

    def bulk_upsert_reactions(self, message_ids: list, rows: list,
                             guild_id: Optional[int] = None) -> bool:
        """Bulk-sync reactions for multiple messages at once.

        Upserts all current rows (clearing removed_at), then soft-deletes
        any active rows for those messages that are no longer present.
        More efficient than per-message upsert for backfill scenarios.
        """
        if guild_id is None:
            resolved_guild_ids = {
                self._resolve_message_guild_id(message_id)
                for message_id in message_ids
            }
            resolved_guild_ids.discard(None)
            if len(resolved_guild_ids) == 1:
                guild_id = next(iter(resolved_guild_ids))
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for bulk_upsert_reactions")
            return False

        sb = self.storage_handler.supabase_client
        try:
            # Upsert all current reactions (mark as active)
            for i in range(0, len(rows), 500):
                batch = [dict(r, removed_at=None) for r in rows[i:i + 500]]
                if guild_id is not None:
                    batch = [dict(r, guild_id=guild_id) for r in batch]
                sb.table('discord_reactions').upsert(batch).execute()

            # Soft-delete stale rows: fetch active rows for these messages,
            # then mark any not in the current set
            current_keys = {(r['message_id'], r['user_id'], r['emoji']) for r in rows}
            now = datetime.now(timezone.utc).isoformat()

            for i in range(0, len(message_ids), 100):
                batch_ids = message_ids[i:i + 100]
                existing = sb.table('discord_reactions') \
                    .select('message_id, user_id, emoji') \
                    .in_('message_id', batch_ids) \
                    .is_('removed_at', 'null') \
                    .execute()

                for row in (existing.data or []):
                    key = (row['message_id'], row['user_id'], row['emoji'])
                    if key not in current_keys:
                        sb.table('discord_reactions') \
                            .update({'removed_at': now}) \
                            .eq('message_id', row['message_id']) \
                            .eq('user_id', row['user_id']) \
                            .eq('emoji', row['emoji']) \
                            .execute()

            return True
        except Exception as e:
            logger.error(f"Error in bulk_upsert_reactions: {e}")
            return False

    # ========== Message Content / Edit History ==========

    def update_message_content(self, message_id: int, new_content: Optional[str],
                              new_edited_at: Optional[str],
                              guild_id: Optional[int] = None) -> Optional[bool]:
        """Update a message's content and append the previous version to edit_history.

        Reads the current row first to snapshot old content, then writes atomically
        via the REST API.  Skips the update if content has not actually changed.

        Args:
            message_id:    Discord message ID.
            new_content:   The edited content string from Discord.
            new_edited_at: ISO-format timestamp of the edit, or None.
            guild_id:      Guild ID for gate check (resolved from message if None).

        Returns:
            True  – row was updated successfully.
            False – message exists in DB but content is unchanged (no-op).
            None  – message not found in DB at all.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for update_message_content")
            return None

        client = self.storage_handler.supabase_client
        try:
            # 1. Read current state (also resolves guild_id for gate check)
            result = (
                client.table('discord_messages')
                .select('content,edited_at,edit_history,guild_id')
                .eq('message_id', message_id)
                .execute()
            )
            if not result.data:
                logger.warning(f"update_message_content: message {message_id} not found in DB")
                return None

            row = result.data[0]

            # Gate check using guild_id from the message row
            msg_guild_id = guild_id or row.get('guild_id')
            if not self._gate_check(msg_guild_id):
                return False

            old_content = row.get('content')
            old_edited_at = row.get('edited_at')

            # 2. Skip if nothing changed (e.g. embed-resolution triggers)
            if old_content == new_content:
                return False

            # 3. Build the new history array
            existing_history = row.get('edit_history') or []
            if not isinstance(existing_history, list):
                existing_history = []

            history_entry = {
                'content': old_content,
                'edited_at': old_edited_at,
                'recorded_at': datetime.now(timezone.utc).isoformat()
            }
            updated_history = existing_history + [history_entry]

            # 4. Write new content + updated history
            (
                client.table('discord_messages')
                .update({
                    'content': new_content,
                    'edited_at': new_edited_at,
                    'edit_history': updated_history,
                    'synced_at': datetime.now(timezone.utc).isoformat()
                })
                .eq('message_id', message_id)
                .execute()
            )
            logger.debug(f"update_message_content: updated message {message_id} (history depth now {len(updated_history)})")
            return True

        except Exception as e:
            logger.error(f"Error in update_message_content for message {message_id}: {e}", exc_info=True)
            return False

    # ========== Shared Posts Tracking ==========
    
    def record_shared_post(
        self,
        discord_message_id: int,
        discord_user_id: int,
        platform: str,
        platform_post_id: str,
        platform_post_url: Optional[str] = None,
        delete_eligible_hours: int = 6,
        guild_id: Optional[int] = None
    ) -> bool:
        """Record a shared post to enable deletion later.
        
        Args:
            discord_message_id: Original Discord message ID
            discord_user_id: Discord user ID of content author
            platform: Platform name (e.g., 'twitter')
            platform_post_id: ID of the post on the platform (e.g., tweet ID)
            platform_post_url: Full URL to the post
            delete_eligible_hours: Hours during which delete is allowed (default 6)
            
        Returns:
            True if recorded successfully
        """
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for record_shared_post")
            return False

        try:
            from datetime import timedelta
            delete_eligible_until = (datetime.now() + timedelta(hours=delete_eligible_hours)).isoformat()

            data = {
                'discord_message_id': discord_message_id,
                'discord_user_id': discord_user_id,
                'platform': platform,
                'platform_post_id': platform_post_id,
                'platform_post_url': platform_post_url,
                'shared_at': datetime.now().isoformat(),
                'delete_eligible_until': delete_eligible_until
            }
            if guild_id is not None:
                data['guild_id'] = guild_id

            self.storage_handler.supabase_client.table('shared_posts').upsert(data).execute()
            logger.info(f"Recorded shared post: {platform} post {platform_post_id} for message {discord_message_id}")
            return True
        except Exception as e:
            logger.error(f"Error recording shared post: {e}", exc_info=True)
            return False
    
    def get_shared_post(self, discord_message_id: int, platform: str) -> Optional[Dict]:
        """Get a shared post record by Discord message ID and platform.
        
        Returns:
            Dict with post details or None if not found
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        
        try:
            result = (
                self.storage_handler.supabase_client.table('shared_posts')
                .select('*')
                .eq('discord_message_id', discord_message_id)
                .eq('platform', platform)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting shared post: {e}", exc_info=True)
            return None
    
    def mark_shared_post_deleted(self, discord_message_id: int, platform: str,
                                    guild_id: Optional[int] = None) -> bool:
        """Mark a shared post as deleted.

        Returns:
            True if updated successfully
        """
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False

        try:
            (
                self.storage_handler.supabase_client.table('shared_posts')
                .update({'deleted_at': datetime.now().isoformat()})
                .eq('discord_message_id', discord_message_id)
                .eq('platform', platform)
                .execute()
            )
            logger.info(f"Marked {platform} post for message {discord_message_id} as deleted")
            return True
        except Exception as e:
            logger.error(f"Error marking shared post as deleted: {e}", exc_info=True)
            return False

    # ========== Social Publications ==========

    def _serialize_supabase_value(self, value: Any) -> Any:
        """Convert datetime instances to ISO-8601 strings for Supabase writes."""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        if isinstance(value, dict):
            return {key: self._serialize_supabase_value(val) for key, val in value.items()}
        if isinstance(value, list):
            return [self._serialize_supabase_value(item) for item in value]
        return value

    def _resolve_social_publication_guild_id(self, publication_id: str) -> Optional[int]:
        """Resolve guild_id for a publication when callers only have publication_id."""
        if not self.supabase:
            return None
        try:
            result = (
                self.supabase.table('social_publications')
                .select('guild_id')
                .eq('publication_id', publication_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('guild_id')
        except Exception as e:
            logger.error(f"Error resolving guild for social publication {publication_id}: {e}", exc_info=True)
        return None

    def create_social_publication(self, data: Dict, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Insert a canonical social publication row and return the stored record."""
        effective_guild_id = guild_id or data.get('guild_id')
        if effective_guild_id is None:
            logger.error("create_social_publication requires guild_id")
            return None
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            logger.error("Supabase client not initialized for create_social_publication")
            return None

        try:
            payload = self._serialize_supabase_value(dict(data))
            payload['guild_id'] = effective_guild_id
            result = self.supabase.table('social_publications').insert(payload).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error creating social publication: {e}", exc_info=True)
            return None

    def get_social_publication_by_id(self, publication_id: str, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Lookup a canonical social publication row by publication_id."""
        if guild_id is not None and not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            query = (
                self.supabase.table('social_publications')
                .select('*')
                .eq('publication_id', publication_id)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching social publication {publication_id}: {e}", exc_info=True)
            return None

    def get_social_publications_for_message(
        self,
        message_id: int,
        guild_id: Optional[int] = None,
        platform: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict]:
        """Return canonical social publications linked to a Discord message."""
        if guild_id is not None and not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            query = (
                self.supabase.table('social_publications')
                .select('*')
                .eq('message_id', message_id)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if platform:
                query = query.eq('platform', platform)
            if action:
                query = query.eq('action', action)
            if status:
                query = query.eq('status', status)
            result = query.order('created_at', desc=True).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching social publications for message {message_id}: {e}", exc_info=True)
            return []

    def list_social_publications(
        self,
        guild_id: Optional[int] = None,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        action: Optional[str] = None,
        channel_id: Optional[int] = None,
        user_id: Optional[int] = None,
        source_kind: Optional[str] = None,
        route_key: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict]:
        """List canonical social publications with optional filters."""
        if guild_id is not None and not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            query = self.supabase.table('social_publications').select('*')
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            if status:
                query = query.eq('status', status)
            if platform:
                query = query.eq('platform', platform)
            if action:
                query = query.eq('action', action)
            if channel_id is not None:
                query = query.eq('channel_id', channel_id)
            if user_id is not None:
                query = query.eq('user_id', user_id)
            if source_kind:
                query = query.eq('source_kind', source_kind)
            if route_key:
                query = query.eq('route_key', route_key)
            result = query.order('created_at', desc=True).limit(limit).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error listing social publications: {e}", exc_info=True)
            return []

    def mark_social_publication_processing(
        self,
        publication_id: str,
        guild_id: Optional[int] = None,
        attempt_count: Optional[int] = None,
        retry_after: Optional[datetime] = None
    ) -> bool:
        """Mark a publication as processing."""
        effective_guild_id = guild_id or self._resolve_social_publication_guild_id(publication_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        try:
            payload: Dict[str, Any] = {
                'status': 'processing',
                'last_error': None,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            if attempt_count is not None:
                payload['attempt_count'] = attempt_count
            if retry_after is not None:
                payload['retry_after'] = retry_after
            (
                self.supabase.table('social_publications')
                .update(self._serialize_supabase_value(payload))
                .eq('publication_id', publication_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error marking social publication {publication_id} processing: {e}", exc_info=True)
            return False

    def mark_social_publication_succeeded(
        self,
        publication_id: str,
        guild_id: Optional[int] = None,
        provider_ref: Optional[str] = None,
        provider_url: Optional[str] = None,
        delete_supported: Optional[bool] = None
    ) -> bool:
        """Mark a publication as successfully completed."""
        effective_guild_id = guild_id or self._resolve_social_publication_guild_id(publication_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        try:
            payload: Dict[str, Any] = {
                'status': 'succeeded',
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'last_error': None,
                'retry_after': None,
            }
            if provider_ref is not None:
                payload['provider_ref'] = provider_ref
            if provider_url is not None:
                payload['provider_url'] = provider_url
            if delete_supported is not None:
                payload['delete_supported'] = delete_supported
            (
                self.supabase.table('social_publications')
                .update(payload)
                .eq('publication_id', publication_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error marking social publication {publication_id} succeeded: {e}", exc_info=True)
            return False

    def mark_social_publication_failed(
        self,
        publication_id: str,
        last_error: str,
        guild_id: Optional[int] = None,
        retry_after: Optional[datetime] = None
    ) -> bool:
        """Mark a publication as failed."""
        effective_guild_id = guild_id or self._resolve_social_publication_guild_id(publication_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        try:
            payload: Dict[str, Any] = {
                'status': 'failed',
                'last_error': last_error,
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            if retry_after is not None:
                payload['retry_after'] = retry_after
            (
                self.supabase.table('social_publications')
                .update(self._serialize_supabase_value(payload))
                .eq('publication_id', publication_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error marking social publication {publication_id} failed: {e}", exc_info=True)
            return False

    def mark_social_publication_cancelled(
        self,
        publication_id: str,
        guild_id: Optional[int] = None,
        last_error: Optional[str] = None
    ) -> bool:
        """Mark a publication as cancelled."""
        effective_guild_id = guild_id or self._resolve_social_publication_guild_id(publication_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        try:
            payload: Dict[str, Any] = {
                'status': 'cancelled',
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'retry_after': None,
            }
            if last_error is not None:
                payload['last_error'] = last_error
            (
                self.supabase.table('social_publications')
                .update(payload)
                .eq('publication_id', publication_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error marking social publication {publication_id} cancelled: {e}", exc_info=True)
            return False

    def claim_due_social_publications(
        self,
        limit: int = 10,
        guild_ids: Optional[List[int]] = None
    ) -> List[Dict]:
        """Atomically claim due queued publications through the Supabase RPC."""
        if not self.supabase:
            return []

        writable_guild_ids = guild_ids
        if writable_guild_ids is None and self.server_config:
            writable_guild_ids = [
                server['guild_id']
                for server in self.server_config.get_enabled_servers(require_write=True)
                if server.get('guild_id') is not None
            ]
        if writable_guild_ids == []:
            return []

        try:
            params: Dict[str, Any] = {'claim_limit': limit}
            if writable_guild_ids is not None:
                params['claim_guild_ids'] = writable_guild_ids
            result = self.supabase.rpc('claim_due_social_publications', params).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error claiming due social publications: {e}", exc_info=True)
            return []

    # ========== Live Update Social Runs ==========

    def upsert_live_update_social_run(
        self,
        topic_id: str,
        platform: str,
        action: str = "post",
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        source_metadata: Optional[Dict[str, Any]] = None,
        topic_summary_data: Optional[Dict[str, Any]] = None,
        vendor: str = "codex",
        depth: str = "high",
        with_feedback: bool = True,
        deepseek_provider: str = "direct",
    ) -> Optional[Dict]:
        """Create-or-return a live_update_social_runs row.

        Durable unique key: (topic_id, platform, action).
        On conflict → returns the existing row without modifying it.
        """
        if not self.supabase:
            logger.error("Supabase client not initialized for upsert_live_update_social_run")
            return None

        try:
            # First try to find existing run
            existing = (
                self.supabase.table("live_update_social_runs")
                .select("*")
                .eq("topic_id", topic_id)
                .eq("platform", platform)
                .eq("action", action)
                .limit(1)
                .execute()
            )
            if existing.data:
                return existing.data[0]

            # No existing run — insert new one
            from uuid import uuid4

            now = datetime.now(timezone.utc).isoformat()
            payload = {
                "run_id": str(uuid4()),
                "topic_id": topic_id,
                "platform": platform,
                "action": action,
                "mode": "draft",
                "terminal_status": None,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "chain_vendor": vendor,
                "chain_depth": depth,
                "chain_with_feedback": with_feedback,
                "chain_deepseek_provider": deepseek_provider,
                "source_metadata": source_metadata or {},
                "publish_units": topic_summary_data or {},
                "draft_text": None,
                "media_decisions": {},
                "trace_entries": [],
                "created_at": now,
                "updated_at": now,
            }
            serialized = self._serialize_supabase_value(payload)
            result = (
                self.supabase.table("live_update_social_runs")
                .insert(serialized)
                .execute()
            )
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            # If the race condition hits (insert after select fails with conflict),
            # try one more lookup
            logger.debug(
                "upsert_live_update_social_run insert failed (may be race): %s — retrying lookup",
                e,
            )
            try:
                existing = (
                    self.supabase.table("live_update_social_runs")
                    .select("*")
                    .eq("topic_id", topic_id)
                    .eq("platform", platform)
                    .eq("action", action)
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    return existing.data[0]
            except Exception:
                pass
            logger.error(
                "Error upserting live_update_social_run for %s/%s/%s: %s",
                topic_id,
                platform,
                action,
                e,
                exc_info=True,
            )
            return None

    def get_live_update_social_run(self, run_id: str) -> Optional[Dict]:
        """Return a live_update_social_runs row by run_id."""
        if not self.supabase:
            return None
        try:
            result = (
                self.supabase.table("live_update_social_runs")
                .select("*")
                .eq("run_id", run_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error fetching live_update_social_run %s: %s",
                run_id,
                e,
                exc_info=True,
            )
            return None

    # Sprint 3 schema migrations (run these ALTER TABLE statements against Supabase):
    #
    #   ALTER TABLE live_update_social_runs
    #     ADD COLUMN IF NOT EXISTS publication_outcome JSONB DEFAULT NULL;
    #
    #   ALTER TABLE social_publications
    #     ADD COLUMN IF NOT EXISTS media_attached JSONB DEFAULT NULL;
    #
    #   ALTER TABLE social_publications
    #     ADD COLUMN IF NOT EXISTS media_missing JSONB DEFAULT NULL;

    def update_live_update_social_run(
        self,
        run_id: str,
        terminal_status: Optional[str] = None,
        draft_text: Optional[str] = None,
        media_decisions: Optional[Dict[str, Any]] = None,
        trace_entries: Optional[List[Dict[str, Any]]] = None,
        publication_outcome: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update terminal columns on a live_update_social_runs row.

        ``publication_outcome`` is a JSONB dict (see
        :class:`~.live_update_social.models.PublishOutcome.to_dict`).
        """
        if not self.supabase:
            return False
        try:
            payload: Dict[str, Any] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if terminal_status is not None:
                payload["terminal_status"] = terminal_status
            if draft_text is not None:
                payload["draft_text"] = draft_text
            if media_decisions is not None:
                payload["media_decisions"] = media_decisions
            if trace_entries is not None:
                payload["trace_entries"] = trace_entries
            if publication_outcome is not None:
                payload["publication_outcome"] = publication_outcome

            (
                self.supabase.table("live_update_social_runs")
                .update(self._serialize_supabase_value(payload))
                .eq("run_id", run_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(
                "Error updating live_update_social_run %s: %s",
                run_id,
                e,
                exc_info=True,
            )
            return False

    def check_live_update_social_duplicate_publication(
        self,
        topic_id: str,
        platform: str,
        action: str = "post",
        guild_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """Check whether a social_publications row already exists for a
        live-update-social topic.

        Returns the existing publication row if found, or None.

        Duplicate key: (source_kind='live_update_social', topic_id in
        request_payload->source_context->metadata, platform, action).
        """
        if not self.supabase:
            return None
        try:
            # Query by source_kind + platform + action, then filter in Python
            # because request_payload is a JSONB column.
            query = (
                self.supabase.table("social_publications")
                .select("*")
                .eq("source_kind", "live_update_social")
                .eq("platform", platform)
                .eq("action", action)
            )
            if guild_id is not None:
                query = query.eq("guild_id", guild_id)
            result = query.order("created_at", desc=True).limit(20).execute()
            if not result.data:
                return None
            for row in result.data:
                request_payload = row.get("request_payload") or {}
                source_context = request_payload.get("source_context") or {}
                metadata = source_context.get("metadata") or {}
                if metadata.get("topic_id") == topic_id:
                    return row
            return None
        except Exception as e:
            logger.error(
                "Error checking duplicate publication for topic %s: %s",
                topic_id, e, exc_info=True,
            )
            return None

    # ========== Sprint 3: Publish Mode Query Methods ==========

    def find_existing_social_posts(
        self,
        topic_id: str,
        platform: str,
        guild_id: Optional[int] = None,
        draft_text: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """Find existing social publications related to a live-update topic.

        Returns a list of matching ``social_publications`` rows.  When
        ``draft_text`` is supplied each candidate is annotated with a
        ``_similarity`` score computed by :meth:`check_content_similarity`
        so callers can detect near-duplicate content beyond exact match.

        Unifies the query pattern used by
        :meth:`check_live_update_social_duplicate_publication` and adds
        optional content-similarity scoring.
        """
        if not self.supabase:
            return []
        try:
            query = (
                self.supabase.table("social_publications")
                .select("*")
                .eq("source_kind", "live_update_social")
                .eq("platform", platform)
            )
            if guild_id is not None:
                query = query.eq("guild_id", guild_id)
            result = query.order("created_at", desc=True).limit(limit).execute()
            if not result.data:
                return []

            matches: List[Dict] = []
            for row in result.data:
                request_payload = row.get("request_payload") or {}
                source_context = request_payload.get("source_context") or {}
                metadata = source_context.get("metadata") or {}
                if metadata.get("topic_id") == topic_id:
                    row_dict = dict(row)
                    if draft_text:
                        existing_text = row_dict.get("text") or (
                            request_payload.get("text") or ""
                        )
                        row_dict["_similarity"] = self.check_content_similarity(
                            draft_text, existing_text
                        )
                    matches.append(row_dict)
            return matches
        except Exception as e:
            logger.error(
                "Error finding existing social posts for topic %s: %s",
                topic_id, e, exc_info=True,
            )
            return []

    def get_recent_social_runs(
        self,
        guild_id: Optional[int] = None,
        terminal_status: Optional[str] = None,
        mode: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """Return recent ``live_update_social_runs`` rows ordered by
        ``created_at`` descending.

        Args:
            guild_id: Optional guild filter.
            terminal_status: If set, only return runs with this status
                (``draft``, ``queued``, ``published``, ``skip``,
                ``needs_review``).
            mode: Optional mode filter (``draft``, ``publish``).
            limit: Maximum rows to return.
            offset: Pagination offset.
        """
        if not self.supabase:
            return []
        try:
            query = (
                self.supabase.table("live_update_social_runs")
                .select("*")
            )
            if guild_id is not None:
                query = query.eq("guild_id", guild_id)
            if terminal_status is not None:
                query = query.eq("terminal_status", terminal_status)
            if mode is not None:
                query = query.eq("mode", mode)
            result = (
                query
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(
                "Error fetching recent social runs: %s", e, exc_info=True,
            )
            return []

    def get_social_publications_by_run_id(
        self,
        run_id: str,
        limit: int = 50,
    ) -> List[Dict]:
        """Return ``social_publications`` rows whose
        ``request_payload->source_context->metadata->run_id`` matches the
        given *run_id*.

        The run_id is embedded in the publication's JSONB
        ``request_payload`` by :func:`_make_enqueue_handler`.
        """
        if not self.supabase:
            return []
        try:
            # Query all live_update_social publications and filter in Python
            # because run_id is nested in JSONB request_payload.
            result = (
                self.supabase.table("social_publications")
                .select("*")
                .eq("source_kind", "live_update_social")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            if not result.data:
                return []

            matches: List[Dict] = []
            for row in result.data:
                request_payload = row.get("request_payload") or {}
                source_context = request_payload.get("source_context") or {}
                metadata = source_context.get("metadata") or {}
                if metadata.get("run_id") == run_id:
                    matches.append(dict(row))
            return matches
        except Exception as e:
            logger.error(
                "Error fetching social publications for run %s: %s",
                run_id, e, exc_info=True,
            )
            return []

    def update_social_publication_media_outcome(
        self,
        publication_id: str,
        media_attached: Optional[List[Dict[str, Any]]] = None,
        media_missing: Optional[List[Dict[str, Any]]] = None,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Persist media outcome on a ``social_publications`` row.

        Args:
            publication_id: The publication record to update.
            media_attached: List of dicts describing media that was
                successfully attached (each with keys like ``media_ref``,
                ``provider_media_id``).
            media_missing: List of dicts describing media that was
                requested but not present in the provider result.
            guild_id: Optional guild for write gating.
        """
        effective_guild_id = guild_id or self._resolve_social_publication_guild_id(publication_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        try:
            payload: Dict[str, Any] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if media_attached is not None:
                payload["media_attached"] = self._serialize_supabase_value(media_attached)
            if media_missing is not None:
                payload["media_missing"] = self._serialize_supabase_value(media_missing)

            (
                self.supabase.table("social_publications")
                .update(payload)
                .eq("publication_id", publication_id)
                .eq("guild_id", effective_guild_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(
                "Error updating media outcome for publication %s: %s",
                publication_id, e, exc_info=True,
            )
            return False

    @staticmethod
    def check_content_similarity(text_a: str, text_b: str) -> float:
        """Character 5-gram containment similarity.

        Normalises both texts (lowercase, collapse whitespace), extracts
        all length-5 character substrings from *text_a*, and returns the
        fraction found in *text_b*.  A result of 1.0 means every 5-gram
        of *text_a* appears in *text_b*; 0.0 means none do.

        This is a containment measure (not Jaccard), so short *text_a*
        inside long *text_b* can still score 1.0, which is desirable for
        detecting reposts of the same content embedded in longer threads.
        """
        if not text_a or not text_b:
            return 0.0

        def _normalise(t: str) -> str:
            return " ".join(t.lower().split())

        na = _normalise(text_a)
        nb = _normalise(text_b)

        if len(na) < 5 or len(nb) < 5:
            # Fall back to simple substring containment for very short texts
            return 1.0 if na in nb or nb in na else 0.0

        # Build set of 5-grams for text_b (the reference corpus)
        b_grams = {nb[i : i + 5] for i in range(len(nb) - 4)}

        a_grams = [na[i : i + 5] for i in range(len(na) - 4)]
        if not a_grams:
            return 0.0

        matches = sum(1 for g in a_grams if g in b_grams)
        return matches / len(a_grams)

    # ========== Payments ==========

    def _get_writable_guild_ids(self, guild_ids: Optional[List[int]] = None) -> Optional[List[int]]:
        """Resolve writable guild IDs for queue-style payment queries."""
        writable_guild_ids = guild_ids
        if writable_guild_ids is None and self.server_config:
            writable_guild_ids = [
                server['guild_id']
                for server in self.server_config.get_enabled_servers(require_write=True)
                if server.get('guild_id') is not None
            ]
        return writable_guild_ids

    def _resolve_payment_request_guild_id(self, payment_id: str) -> Optional[int]:
        """Resolve guild_id for a payment request when callers only have payment_id."""
        if not self.supabase:
            return None
        try:
            result = (
                self.supabase.table('payment_requests')
                .select('guild_id')
                .eq('payment_id', payment_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('guild_id')
        except Exception as e:
            logger.error(f"Error resolving guild for payment request {payment_id}: {e}", exc_info=True)
        return None

    def _resolve_payment_route_guild_id(self, route_id: str) -> Optional[int]:
        """Resolve guild_id for a payment route when callers only have route_id."""
        if not self.supabase:
            return None
        try:
            result = (
                self.supabase.table('payment_channel_routes')
                .select('guild_id')
                .eq('id', route_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('guild_id')
        except Exception as e:
            logger.error(f"Error resolving guild for payment route {route_id}: {e}", exc_info=True)
        return None

    def _resolve_wallet_guild_id(self, wallet_id: str) -> Optional[int]:
        """Resolve guild_id for a wallet record when callers only have wallet_id."""
        if not self.supabase:
            return None
        try:
            result = (
                self.supabase.table('wallet_registry')
                .select('guild_id')
                .eq('wallet_id', wallet_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('guild_id')
        except Exception as e:
            logger.error(f"Error resolving guild for wallet {wallet_id}: {e}", exc_info=True)
        return None

    def _update_payment_request_record(
        self,
        payment_id: str,
        payload: Dict[str, Any],
        guild_id: Optional[int] = None,
        allowed_statuses: Optional[List[str]] = None,
    ) -> bool:
        """Update a payment request with optional current-status guard."""
        effective_guild_id = guild_id or self._resolve_payment_request_guild_id(payment_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        existing = self.get_payment_request(payment_id, guild_id=effective_guild_id)
        if not existing:
            return False
        if allowed_statuses and existing.get('status') not in allowed_statuses:
            logger.warning(
                "Blocked payment transition for %s in guild %s: status=%s not in %s",
                payment_id,
                effective_guild_id,
                existing.get('status'),
                allowed_statuses,
            )
            return False

        try:
            result = (
                self.supabase.table('payment_requests')
                .update(self._serialize_supabase_value(payload))
                .eq('payment_id', payment_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error updating payment request {payment_id}: {e}", exc_info=True)
            return False

    def _append_tx_signature_history(
        self,
        payment_id: str,
        entry: Dict[str, Any],
        *,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Append one audit entry to tx_signature_history without changing payment behavior."""
        signature = entry.get('signature')
        if not signature:
            return True

        effective_guild_id = guild_id or self._resolve_payment_request_guild_id(payment_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        existing = self.get_payment_request(payment_id, guild_id=effective_guild_id)
        if not existing:
            return False

        current_history = existing.get('tx_signature_history') or []
        if not isinstance(current_history, list):
            current_history = []
        updated_history = list(current_history)
        updated_history.append(dict(entry))

        try:
            result = (
                self.supabase.table('payment_requests')
                .update(
                    self._serialize_supabase_value(
                        {
                            'tx_signature_history': updated_history,
                        }
                    )
                )
                .eq('payment_id', payment_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error appending tx signature history for {payment_id}: {e}", exc_info=True)
            return False

    def upsert_wallet(
        self,
        guild_id: int,
        discord_user_id: int,
        chain: str,
        address: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """Create or update a wallet registry row for one user + chain in a guild."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        existing = self.get_wallet(guild_id, discord_user_id, chain)
        payload: Dict[str, Any] = {
            'guild_id': guild_id,
            'discord_user_id': discord_user_id,
            'chain': chain,
            'wallet_address': address,
        }
        if metadata is not None:
            payload['metadata'] = metadata
        if existing and existing.get('wallet_address') != address:
            if self.has_active_payment_or_intent(guild_id, discord_user_id):
                logger.warning(
                    "Blocked wallet update for guild %s user %s chain %s: existing=%s incoming=%s",
                    guild_id,
                    discord_user_id,
                    chain,
                    _redact_wallet(existing.get('wallet_address')),
                    _redact_wallet(address),
                )
                raise WalletUpdateBlockedError("active payment in flight")
            # Verification is tied to the specific address that received the test payment.
            payload['verified_at'] = None

        try:
            if existing:
                result = (
                    self.supabase.table('wallet_registry')
                    .update(self._serialize_supabase_value(payload))
                    .eq('wallet_id', existing['wallet_id'])
                    .eq('guild_id', guild_id)
                    .execute()
                )
            else:
                result = (
                    self.supabase.table('wallet_registry')
                    .insert(self._serialize_supabase_value(payload))
                    .execute()
                )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                f"Error upserting wallet for guild {guild_id}, user {discord_user_id}, chain {chain}: {e}",
                exc_info=True,
            )
            return None

    def get_wallet(self, guild_id: int, discord_user_id: int, chain: str) -> Optional[Dict]:
        """Fetch a wallet registry record for a guild member and chain."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('wallet_registry')
                .select('*')
                .eq('guild_id', guild_id)
                .eq('discord_user_id', discord_user_id)
                .eq('chain', chain)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                f"Error fetching wallet for guild {guild_id}, user {discord_user_id}, chain {chain}: {e}",
                exc_info=True,
            )
            return None

    def get_wallet_by_id(self, wallet_id: str, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Fetch a wallet registry record by wallet_id."""
        effective_guild_id = guild_id or self._resolve_wallet_guild_id(wallet_id)
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('wallet_registry')
                .select('*')
                .eq('wallet_id', wallet_id)
                .eq('guild_id', effective_guild_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching wallet {wallet_id}: {e}", exc_info=True)
            return None

    def has_active_payment_or_intent(self, guild_id: int, discord_user_id: int) -> bool:
        """Return True when a user has a non-terminal payment request or admin payment intent."""
        if not self._gate_check(guild_id):
            return False
        if not self.supabase:
            return False

        try:
            payment_result = (
                self.supabase.table('payment_requests')
                .select('payment_id')
                .eq('guild_id', guild_id)
                .eq('recipient_discord_id', discord_user_id)
                .not_.in_('status', ['confirmed', 'failed', 'manual_hold', 'cancelled'])
                .limit(1)
                .execute()
            )
            if payment_result.data:
                return True

            intent_result = (
                self.supabase.table('admin_payment_intents')
                .select('intent_id')
                .eq('guild_id', guild_id)
                .eq('recipient_user_id', discord_user_id)
                .not_.in_('status', ['completed', 'failed', 'cancelled'])
                .limit(1)
                .execute()
            )
            return bool(intent_result.data)
        except Exception as e:
            logger.error(
                "Error checking active payment or intent for guild %s user %s: %s",
                guild_id,
                discord_user_id,
                e,
                exc_info=True,
            )
            logger.warning(
                "Failing closed: assuming active payment/intent for guild %s user %s due to error",
                guild_id,
                discord_user_id,
            )
            return True

    def list_wallets(
        self,
        guild_id: int,
        chain: Optional[str] = None,
        verified_only: bool = False,
        discord_user_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """List wallet registry rows for one guild."""
        if not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            query = (
                self.supabase.table('wallet_registry')
                .select('*')
                .eq('guild_id', guild_id)
            )
            if chain:
                query = query.eq('chain', chain)
            if discord_user_id is not None:
                query = query.eq('discord_user_id', discord_user_id)
            result = query.order('created_at', desc=True).limit(limit).execute()
            rows = result.data or []
            if verified_only:
                rows = [row for row in rows if row.get('verified_at')]
            return rows
        except Exception as e:
            logger.error(f"Error listing wallets for guild {guild_id}: {e}", exc_info=True)
            return []

    def mark_wallet_verified(self, wallet_id: str, guild_id: Optional[int] = None) -> bool:
        """Mark a wallet as verified after a confirmed test payment."""
        effective_guild_id = guild_id or self._resolve_wallet_guild_id(wallet_id)
        if not self._gate_check(effective_guild_id):
            return False
        if not self.supabase:
            return False

        try:
            result = (
                self.supabase.table('wallet_registry')
                .update({'verified_at': datetime.now(timezone.utc).isoformat()})
                .eq('wallet_id', wallet_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error marking wallet {wallet_id} verified: {e}", exc_info=True)
            return False

    def create_payment_route(self, data: Dict, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Create a payment route row for one guild."""
        effective_guild_id = guild_id or data.get('guild_id')
        if effective_guild_id is None or not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        try:
            payload = self._serialize_supabase_value(dict(data))
            payload['guild_id'] = effective_guild_id
            result = self.supabase.table('payment_channel_routes').insert(payload).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error creating payment route in guild {effective_guild_id}: {e}", exc_info=True)
            return None

    def get_payment_route_by_id(self, route_id: str, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Fetch one payment route row by route id."""
        effective_guild_id = guild_id or self._resolve_payment_route_guild_id(route_id)
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('payment_channel_routes')
                .select('*')
                .eq('id', route_id)
                .eq('guild_id', effective_guild_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching payment route {route_id}: {e}", exc_info=True)
            return None

    def list_payment_routes(
        self,
        guild_id: int,
        producer: Optional[str] = None,
        channel_id: Optional[int] = None,
        enabled: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """List payment routes for one guild with optional filters."""
        if not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            query = (
                self.supabase.table('payment_channel_routes')
                .select('*')
                .eq('guild_id', guild_id)
            )
            if producer:
                query = query.eq('producer', producer)
            if channel_id is not None:
                query = query.eq('channel_id', channel_id)
            result = query.order('created_at', desc=True).limit(limit).execute()
            rows = result.data or []
            if enabled is not None:
                rows = [row for row in rows if bool(row.get('enabled')) is enabled]
            return rows
        except Exception as e:
            logger.error(f"Error listing payment routes for guild {guild_id}: {e}", exc_info=True)
            return []

    def get_payment_routes(
        self,
        guild_id: int,
        producer: Optional[str] = None,
        channel_id: Optional[int] = None,
        enabled: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Compatibility wrapper for payment-route listing."""
        return self.list_payment_routes(
            guild_id=guild_id,
            producer=producer,
            channel_id=channel_id,
            enabled=enabled,
            limit=limit,
        )

    def update_payment_route(self, route_id: str, data: Dict, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Update one payment route row and return the stored record."""
        effective_guild_id = guild_id or self._resolve_payment_route_guild_id(route_id)
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('payment_channel_routes')
                .update(self._serialize_supabase_value(dict(data)))
                .eq('id', route_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error updating payment route {route_id}: {e}", exc_info=True)
            return None

    def delete_payment_route(self, route_id: str, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Delete one payment route row and return the deleted record if possible."""
        effective_guild_id = guild_id or self._resolve_payment_route_guild_id(route_id)
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        existing = self.get_payment_route_by_id(route_id, guild_id=effective_guild_id)
        if not existing:
            return None

        try:
            (
                self.supabase.table('payment_channel_routes')
                .delete()
                .eq('id', route_id)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return existing
        except Exception as e:
            logger.error(f"Error deleting payment route {route_id}: {e}", exc_info=True)
            return None

    def create_admin_payment_intent(self, record: Dict, guild_id: int) -> Optional[Dict]:
        """Insert one admin payment intent row and return the stored record."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            payload = self._serialize_supabase_value(dict(record))
            payload['guild_id'] = guild_id
            result = self.supabase.table('admin_payment_intents').insert(payload).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error creating admin payment intent in guild {guild_id}: {e}", exc_info=True)
            return None

    def create_admin_payment_intents_batch(self, records: List[Dict], guild_id: int) -> Optional[List[Dict]]:
        """Insert multiple admin payment intents in a single Supabase request."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None
        if not records:
            return []

        try:
            payload = []
            for record in records:
                serialized = self._serialize_supabase_value(dict(record))
                serialized['guild_id'] = guild_id
                payload.append(serialized)
            result = self.supabase.table('admin_payment_intents').insert(payload).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error creating admin payment intent batch in guild {guild_id}: {e}", exc_info=True)
            return None

    def get_admin_payment_intent(self, intent_id: str, guild_id: int) -> Optional[Dict]:
        """Fetch one admin payment intent by intent_id."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('intent_id', intent_id)
                .eq('guild_id', guild_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching admin payment intent {intent_id}: {e}", exc_info=True)
            return None

    def list_intents_by_status(self, guild_id: int, status: str) -> List[Dict]:
        """List admin payment intents for one guild filtered by exact status."""
        if not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            result = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('guild_id', guild_id)
                .eq('status', status)
                .order('created_at')
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(
                "Error listing admin payment intents for guild %s with status %s: %s",
                guild_id,
                status,
                e,
                exc_info=True,
            )
            return []

    def list_stale_test_receipt_intents(self, cutoff_iso: str) -> List[Dict]:
        """List awaiting_test_receipt_confirmation intents older than the cutoff."""
        if not self.supabase:
            return []

        writable_guild_ids = self._get_writable_guild_ids()
        if writable_guild_ids == []:
            return []

        try:
            query = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('status', 'awaiting_test_receipt_confirmation')
                .lt('updated_at', cutoff_iso)
            )
            if writable_guild_ids:
                if len(writable_guild_ids) == 1:
                    query = query.eq('guild_id', writable_guild_ids[0])
                else:
                    query = query.in_('guild_id', writable_guild_ids)
            result = query.order('updated_at').execute()
            return result.data or []
        except Exception as e:
            logger.error("Error listing stale test receipt intents before %s: %s", cutoff_iso, e, exc_info=True)
            return []

    def list_stale_awaiting_admin_init_intents(self, cutoff_iso: str) -> List[Dict]:
        """List awaiting_admin_init intents whose updated_at is older than the cutoff."""
        if not self.supabase:
            return []

        writable_guild_ids = self._get_writable_guild_ids()
        if writable_guild_ids == []:
            return []

        try:
            query = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('status', 'awaiting_admin_init')
                .lt('updated_at', cutoff_iso)
            )
            if writable_guild_ids:
                if len(writable_guild_ids) == 1:
                    query = query.eq('guild_id', writable_guild_ids[0])
                else:
                    query = query.in_('guild_id', writable_guild_ids)
            result = query.order('updated_at').execute()
            return result.data or []
        except Exception as e:
            logger.error("Error listing stale awaiting_admin_init intents before %s: %s", cutoff_iso, e, exc_info=True)
            return []

    def increment_intent_ambiguous_reply_count(self, intent_id: str, guild_id: int) -> Optional[Dict]:
        """Increment ambiguous_reply_count for one admin payment intent."""
        intent = self.get_admin_payment_intent(intent_id, guild_id)
        if not intent:
            return None
        next_count = int(intent.get('ambiguous_reply_count') or 0) + 1
        return self.update_admin_payment_intent(
            intent_id,
            {'ambiguous_reply_count': next_count},
            guild_id,
        )

    def get_pending_confirmation_admin_chat_intents_by_payment(self, payment_ids: List[str]) -> Dict[str, Optional[Dict]]:
        """Map payment_ids to matching admin payment intents, if any."""
        normalized_payment_ids = [str(payment_id).strip() for payment_id in payment_ids if str(payment_id).strip()]
        mapping: Dict[str, Optional[Dict]] = {payment_id: None for payment_id in normalized_payment_ids}
        if not normalized_payment_ids or not self.supabase:
            return mapping

        writable_guild_ids = self._get_writable_guild_ids()
        if writable_guild_ids == []:
            return mapping

        def _apply_guild_scope(query):
            if writable_guild_ids:
                if len(writable_guild_ids) == 1:
                    return query.eq('guild_id', writable_guild_ids[0])
                return query.in_('guild_id', writable_guild_ids)
            return query

        try:
            final_query = _apply_guild_scope(
                self.supabase.table('admin_payment_intents').select('*')
            )
            if len(normalized_payment_ids) == 1:
                final_query = final_query.eq('final_payment_id', normalized_payment_ids[0])
            else:
                final_query = final_query.in_('final_payment_id', normalized_payment_ids)
            final_rows = final_query.execute().data or []

            test_query = _apply_guild_scope(
                self.supabase.table('admin_payment_intents').select('*')
            )
            if len(normalized_payment_ids) == 1:
                test_query = test_query.eq('test_payment_id', normalized_payment_ids[0])
            else:
                test_query = test_query.in_('test_payment_id', normalized_payment_ids)
            test_rows = test_query.execute().data or []

            for row in final_rows:
                payment_id = row.get('final_payment_id')
                if payment_id in mapping:
                    mapping[payment_id] = row
            for row in test_rows:
                payment_id = row.get('test_payment_id')
                if payment_id in mapping and mapping[payment_id] is None:
                    mapping[payment_id] = row
            return mapping
        except Exception as e:
            logger.error(
                "Error fetching pending confirmation admin payment intents for payments %s: %s",
                normalized_payment_ids,
                e,
                exc_info=True,
            )
            return mapping

    def find_admin_chat_intent_by_payment_id(self, payment_id: str) -> Optional[Dict]:
        """Find one admin payment intent linked to the given payment_id."""
        payment_map = self.get_pending_confirmation_admin_chat_intents_by_payment([payment_id])
        found = payment_map.get(str(payment_id).strip())
        if found is not None:
            return found
        if not self.supabase:
            return None

        writable_guild_ids = self._get_writable_guild_ids()
        if writable_guild_ids == []:
            return None

        def _apply_guild_scope(query):
            if writable_guild_ids:
                if len(writable_guild_ids) == 1:
                    return query.eq('guild_id', writable_guild_ids[0])
                return query.in_('guild_id', writable_guild_ids)
            return query

        try:
            final_result = (
                _apply_guild_scope(self.supabase.table('admin_payment_intents').select('*'))
                .eq('final_payment_id', payment_id)
                .limit(1)
                .execute()
            )
            if final_result.data:
                return final_result.data[0]

            test_result = (
                _apply_guild_scope(self.supabase.table('admin_payment_intents').select('*'))
                .eq('test_payment_id', payment_id)
                .limit(1)
                .execute()
            )
            return test_result.data[0] if test_result.data else None
        except Exception as e:
            logger.error("Error finding admin payment intent for payment %s: %s", payment_id, e, exc_info=True)
            return None

    def get_awaiting_wallet_intent_for_user(
        self,
        guild_id: int,
        recipient_user_id: int,
    ) -> Optional[Dict]:
        """Fetch the single active awaiting_wallet admin payment intent for one user.

        Used by admin-initiated wallet-upsert flow: when an admin sets a wallet for
        a user who has a pending intent blocked on wallet collection, we need to find
        that intent across any channel (unique-by-channel invariant already
        prevents duplicates; we just need the lookup to not require the caller to
        know which channel it's in).
        """
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('guild_id', guild_id)
                .eq('recipient_user_id', recipient_user_id)
                .eq('status', 'awaiting_wallet')
                .order('created_at', desc=True)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error fetching awaiting_wallet intent for guild %s recipient %s: %s",
                guild_id,
                recipient_user_id,
                e,
                exc_info=True,
            )
            return None

    def get_active_intent_for_recipient(self, guild_id: int, channel_id: int, recipient_user_id: int) -> Optional[Dict]:
        """Fetch the single active admin payment intent for one guild/channel/recipient."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('guild_id', guild_id)
                .eq('channel_id', channel_id)
                .eq('recipient_user_id', recipient_user_id)
                .not_.in_('status', ['completed', 'failed', 'cancelled'])
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(
                "Error fetching active admin payment intent for guild %s channel %s recipient %s: %s",
                guild_id,
                channel_id,
                recipient_user_id,
                e,
                exc_info=True,
            )
            return None

    def list_active_intents(self, guild_id: int) -> List[Dict]:
        """List active admin payment intents for one guild for reconciliation."""
        if not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            result = (
                self.supabase.table('admin_payment_intents')
                .select('*')
                .eq('guild_id', guild_id)
                .not_.in_('status', ['completed', 'failed', 'cancelled'])
                .order('created_at')
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error listing active admin payment intents for guild {guild_id}: {e}", exc_info=True)
            return []

    def update_admin_payment_intent(self, intent_id: str, payload: Dict, guild_id: int) -> Optional[Dict]:
        """Update one admin payment intent row and return the stored record."""
        if not self._gate_check(guild_id):
            return None
        if not self.supabase:
            return None

        try:
            serialized_payload = self._serialize_supabase_value(dict(payload))
            try:
                result = (
                    self.supabase.table('admin_payment_intents')
                    .update(serialized_payload)
                    .eq('intent_id', intent_id)
                    .eq('guild_id', guild_id)
                    .execute()
                )
            except Exception as exc:
                if 'status_message_id' not in payload or 'status_message_id' not in str(exc):
                    raise
                logger.info(
                    "Retrying admin payment intent update without status_message_id for intent %s",
                    intent_id,
                )
                serialized_payload.pop('status_message_id', None)
                result = (
                    self.supabase.table('admin_payment_intents')
                    .update(serialized_payload)
                    .eq('intent_id', intent_id)
                    .eq('guild_id', guild_id)
                    .execute()
                )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error updating admin payment intent {intent_id}: {e}", exc_info=True)
            return None

    def create_payment_request(self, record: Dict, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Insert one payment request row and return the stored record."""
        effective_guild_id = guild_id or record.get('guild_id')
        if effective_guild_id is None:
            logger.error("create_payment_request requires guild_id")
            return None
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        try:
            payload = self._serialize_supabase_value(dict(record))
            payload['guild_id'] = effective_guild_id
            result = self.supabase.table('payment_requests').insert(payload).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error creating payment request: {e}", exc_info=True)
            return None

    def get_payment_request(self, payment_id: str, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Fetch one payment request by payment_id."""
        effective_guild_id = guild_id or self._resolve_payment_request_guild_id(payment_id)
        if not self._gate_check(effective_guild_id):
            return None
        if not self.supabase:
            return None

        try:
            result = (
                self.supabase.table('payment_requests')
                .select('*')
                .eq('payment_id', payment_id)
                .eq('guild_id', effective_guild_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching payment request {payment_id}: {e}", exc_info=True)
            return None

    def get_legacy_provider_payment_requests(
        self,
        guild_ids: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Fetch payment requests that still use the pre-split provider name."""
        if not self.supabase:
            return []

        writable_guild_ids = self._get_writable_guild_ids(guild_ids)
        if writable_guild_ids == []:
            return []

        try:
            query = (
                self.supabase.table('payment_requests')
                .select('*')
                .eq('provider', 'solana')
            )
            if writable_guild_ids is not None:
                query = query.in_('guild_id', writable_guild_ids)
            result = query.order('created_at').execute()
            return result.data or []
        except Exception as e:
            logger.error("Error fetching legacy provider payment requests: %s", e, exc_info=True)
            return []

    def get_payment_requests_by_producer(
        self,
        guild_id: int,
        producer: str,
        producer_ref: str,
        is_test: Optional[bool] = None,
    ) -> List[Dict]:
        """Fetch payment requests for one producer reference inside a guild."""
        if not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            # Intentionally return every matching row regardless of status; PaymentService's
            # collision/idempotency logic depends on seeing terminal and non-terminal history.
            query = (
                self.supabase.table('payment_requests')
                .select('*')
                .eq('guild_id', guild_id)
                .eq('producer', producer)
                .eq('producer_ref', producer_ref)
            )
            if is_test is not None:
                query = query.eq('is_test', is_test)
            result = query.order('created_at', desc=True).execute()
            return result.data or []
        except Exception as e:
            logger.error(
                f"Error fetching payment requests for {producer}:{producer_ref} in guild {guild_id}: {e}",
                exc_info=True,
            )
            return []

    def get_rolling_24h_payout_usd(self, guild_id: int, provider: str) -> float:
        """Sum stored USD value for recent non-test payouts on one provider."""
        if not self._gate_check(guild_id):
            return 0.0
        if not self.supabase:
            return 0.0

        provider_key = str(provider or '').strip().lower()
        if not provider_key:
            return 0.0

        # Supabase REST has no serializable transaction here, so the residual burst-race window is
        # roughly one RPC round-trip (about 100-500 ms); manual_hold plus admin DM is the fail-closed
        # backstop. This helper also depends on upstream request_payment persisting derived amount_usd
        # for capped amount_token-only callers in T8's cap-enforcement step.
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        try:
            result = (
                self.supabase.table('payment_requests')
                .select('amount_usd')
                .eq('guild_id', guild_id)
                .eq('provider', provider_key)
                .eq('is_test', False)
                .in_('status', ['pending_confirmation', 'queued', 'processing', 'submitted', 'confirmed'])
                .gte('created_at', cutoff)
                .execute()
            )
            rows = result.data or []
            return float(sum(float(row.get('amount_usd') or 0.0) for row in rows))
        except Exception as e:
            logger.error(
                "Error summing rolling 24h payout USD for guild %s provider %s: %s",
                guild_id,
                provider_key,
                e,
                exc_info=True,
            )
            return 0.0

    def list_payment_requests(
        self,
        guild_id: int,
        status: Optional[str] = None,
        producer: Optional[str] = None,
        recipient_discord_id: Optional[int] = None,
        wallet_id: Optional[str] = None,
        is_test: Optional[bool] = None,
        route_key: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """List payment requests for one guild with optional filters."""
        if not self._gate_check(guild_id):
            return []
        if not self.supabase:
            return []

        try:
            query = (
                self.supabase.table('payment_requests')
                .select('*')
                .eq('guild_id', guild_id)
            )
            if status:
                query = query.eq('status', status)
            if producer:
                query = query.eq('producer', producer)
            if recipient_discord_id is not None:
                query = query.eq('recipient_discord_id', recipient_discord_id)
            if wallet_id is not None:
                query = query.eq('wallet_id', wallet_id)
            if is_test is not None:
                query = query.eq('is_test', is_test)
            if route_key:
                query = query.eq('route_key', route_key)
            result = query.order('created_at', desc=True).limit(limit).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error listing payment requests for guild {guild_id}: {e}", exc_info=True)
            return []

    def mark_payment_confirmed_by_user(
        self,
        payment_id: str,
        guild_id: Optional[int] = None,
        confirmed_by_user_id: Optional[int] = None,
        confirmed_by: str = 'user',
        scheduled_at: Optional[datetime] = None,
    ) -> bool:
        """Advance a pending confirmation to the queued state."""
        payload: Dict[str, Any] = {
            'status': 'queued',
            'confirmed_by': confirmed_by,
            'confirmed_by_user_id': confirmed_by_user_id,
            'confirmed_at': datetime.now(timezone.utc),
            'scheduled_at': scheduled_at or datetime.now(timezone.utc),
            'retry_after': None,
            'completed_at': None,
            'last_error': None,
        }
        return self._update_payment_request_record(
            payment_id,
            payload,
            guild_id=guild_id,
            allowed_statuses=['pending_confirmation'],
        )

    def mark_payment_submitted(
        self,
        payment_id: str,
        tx_signature: str,
        amount_token: Optional[float] = None,
        token_price_usd: Optional[float] = None,
        send_phase: str = 'submitted',
        guild_id: Optional[int] = None,
    ) -> bool:
        """Persist submitted tx metadata immediately after broadcast."""
        submitted_at = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            'status': 'submitted',
            'tx_signature': tx_signature,
            'send_phase': send_phase,
            'submitted_at': submitted_at,
            'completed_at': None,
            'last_error': None,
            'retry_after': None,
        }
        if amount_token is not None:
            payload['amount_token'] = amount_token
        if token_price_usd is not None:
            payload['token_price_usd'] = token_price_usd
        updated = self._update_payment_request_record(
            payment_id,
            payload,
            guild_id=guild_id,
            allowed_statuses=['processing'],
        )
        if updated and not self._append_tx_signature_history(
            payment_id,
            {
                'signature': tx_signature,
                'status': 'submitted',
                'timestamp': submitted_at,
                'reason': 'submit',
                'send_phase': send_phase,
            },
            guild_id=guild_id,
        ):
            logger.warning("Failed to append tx signature history after submit for %s", payment_id)
        return updated

    def mark_payment_confirmed(self, payment_id: str, guild_id: Optional[int] = None) -> bool:
        """Mark a submitted payment as confirmed on-chain."""
        completed_at = datetime.now(timezone.utc)
        updated = self._update_payment_request_record(
            payment_id,
            {
                'status': 'confirmed',
                'completed_at': completed_at,
                'last_error': None,
                'retry_after': None,
            },
            guild_id=guild_id,
            allowed_statuses=['submitted'],
        )
        if updated:
            payment = self.get_payment_request(payment_id, guild_id=guild_id)
            if payment and not self._append_tx_signature_history(
                payment_id,
                {
                    'signature': payment.get('tx_signature'),
                    'status': 'confirmed',
                    'timestamp': completed_at,
                    'reason': 'confirm',
                    'send_phase': payment.get('send_phase'),
                },
                guild_id=guild_id,
            ):
                logger.warning("Failed to append tx signature history after confirm for %s", payment_id)
        return updated

    def mark_payment_failed(
        self,
        payment_id: str,
        error: str,
        send_phase: Optional[str] = None,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Mark a processing or submitted payment as definitively failed."""
        payload: Dict[str, Any] = {
            'status': 'failed',
            'last_error': error,
            'completed_at': datetime.now(timezone.utc),
            'retry_after': None,
        }
        if send_phase is not None:
            payload['send_phase'] = send_phase
        return self._update_payment_request_record(
            payment_id,
            payload,
            guild_id=guild_id,
            allowed_statuses=['processing', 'submitted'],
        )

    def _force_reconcile_payment_status(
        self,
        payment_id: str,
        *,
        target_status: str,
        tx_signature: str,
        reason: str,
        history_reason: str,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Override the normal transition guards using authoritative on-chain truth."""
        payment = self.get_payment_request(payment_id, guild_id=guild_id)
        if not payment:
            return False

        previous_status = payment.get('status')
        completed_at = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            'status': target_status,
            'tx_signature': tx_signature,
            'completed_at': completed_at,
            'retry_after': None,
        }
        if target_status == 'confirmed':
            payload['last_error'] = None
        else:
            payload['last_error'] = reason

        updated = self._update_payment_request_record(
            payment_id,
            payload,
            guild_id=guild_id,
            allowed_statuses=['submitted', 'processing', 'failed', 'manual_hold'],
        )
        if not updated:
            return False

        logger.warning(
            "Force-reconciled payment %s in guild %s from %s to %s using chain truth: %s",
            payment_id,
            guild_id or payment.get('guild_id'),
            previous_status,
            target_status,
            reason,
        )
        if not self._append_tx_signature_history(
            payment_id,
            {
                'signature': tx_signature,
                'status': target_status,
                'timestamp': completed_at,
                'reason': history_reason,
                'send_phase': payment.get('send_phase'),
                'detail': reason,
            },
            guild_id=guild_id,
        ):
            logger.warning(
                "Failed to append tx signature history after %s for %s",
                history_reason,
                payment_id,
            )
        return True

    def force_reconcile_payment_to_confirmed(
        self,
        payment_id: str,
        *,
        tx_signature: str,
        reason: str,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Reconcile a previously submitted payment to confirmed from chain truth."""
        return self._force_reconcile_payment_status(
            payment_id,
            target_status='confirmed',
            tx_signature=tx_signature,
            reason=reason,
            history_reason='reconcile_confirmed',
            guild_id=guild_id,
        )

    def force_reconcile_payment_to_failed(
        self,
        payment_id: str,
        *,
        tx_signature: str,
        reason: str,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Reconcile a previously submitted payment to failed from chain truth."""
        return self._force_reconcile_payment_status(
            payment_id,
            target_status='failed',
            tx_signature=tx_signature,
            reason=reason,
            history_reason='reconcile_failed',
            guild_id=guild_id,
        )

    def mark_payment_manual_hold(
        self,
        payment_id: str,
        reason: str,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Freeze a payment in manual_hold for ambiguous or explicitly held states."""
        return self._update_payment_request_record(
            payment_id,
            {
                'status': 'manual_hold',
                'last_error': reason,
                'retry_after': None,
                'completed_at': None,
            },
            guild_id=guild_id,
            allowed_statuses=['pending_confirmation', 'queued', 'processing', 'submitted', 'failed'],
        )

    def requeue_payment(
        self,
        payment_id: str,
        retry_after: Optional[datetime] = None,
        guild_id: Optional[int] = None,
    ) -> bool:
        """Retry only from failed by returning the payment to the queued state."""
        requeue_at = datetime.now(timezone.utc)
        payment = self.get_payment_request(payment_id, guild_id=guild_id)
        updated = self._update_payment_request_record(
            payment_id,
            {
                'status': 'queued',
                'tx_signature': None,
                'send_phase': None,
                'submitted_at': None,
                'completed_at': None,
                'retry_after': retry_after,
                'last_error': None,
            },
            guild_id=guild_id,
            allowed_statuses=['failed'],
        )
        if updated and payment and not self._append_tx_signature_history(
            payment_id,
            {
                'signature': payment.get('tx_signature'),
                'status': 'failed',
                'timestamp': requeue_at,
                'reason': 'requeue',
                'send_phase': payment.get('send_phase'),
            },
            guild_id=guild_id,
        ):
            logger.warning("Failed to append tx signature history before requeue for %s", payment_id)
        return updated

    def release_payment_hold(
        self,
        payment_id: str,
        new_status: str,
        guild_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Release a manual_hold payment only to failed or keep holding."""
        if new_status not in {'failed', 'manual_hold'}:
            logger.warning(f"Unsupported release_payment_hold target status: {new_status}")
            return False

        payload: Dict[str, Any] = {'status': new_status}
        if new_status == 'failed':
            payload['completed_at'] = datetime.now(timezone.utc)
            payload['retry_after'] = None
        else:
            payload['completed_at'] = None
        if reason is not None:
            payload['last_error'] = reason

        return self._update_payment_request_record(
            payment_id,
            payload,
            guild_id=guild_id,
            allowed_statuses=['manual_hold'],
        )

    def cancel_payment(
        self,
        payment_id: str,
        guild_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Cancel a payment only from states that have never entered manual_hold or submitted."""
        payload: Dict[str, Any] = {
            'status': 'cancelled',
            'completed_at': datetime.now(timezone.utc),
            'retry_after': None,
        }
        if reason is not None:
            payload['last_error'] = reason
        return self._update_payment_request_record(
            payment_id,
            payload,
            guild_id=guild_id,
            allowed_statuses=['pending_confirmation', 'queued', 'failed'],
        )

    def claim_due_payment_requests(
        self,
        limit: int = 10,
        guild_ids: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Atomically claim due queued payment requests through the Supabase RPC."""
        if not self.supabase:
            return []

        writable_guild_ids = self._get_writable_guild_ids(guild_ids)
        if writable_guild_ids == []:
            return []

        try:
            params: Dict[str, Any] = {'claim_limit': limit}
            if writable_guild_ids is not None:
                params['claim_guild_ids'] = writable_guild_ids
            result = self.supabase.rpc('claim_due_payment_requests', params).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error claiming due payment requests: {e}", exc_info=True)
            return []

    def get_inflight_payment_requests_for_recovery(
        self,
        guild_ids: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Fetch processing/submitted payments that need restart-time recovery."""
        writable_guild_ids = self._get_writable_guild_ids(guild_ids)
        if writable_guild_ids == [] or not self.supabase:
            return []

        try:
            query = (
                self.supabase.table('payment_requests')
                .select('*')
                .in_('status', ['processing', 'submitted'])
            )
            if writable_guild_ids:
                if len(writable_guild_ids) == 1:
                    query = query.eq('guild_id', writable_guild_ids[0])
                else:
                    query = query.in_('guild_id', writable_guild_ids)
            result = query.order('updated_at').execute()
            return result.data or []
        except Exception as e:
            logger.error("Error fetching inflight payment requests for recovery: %s", e, exc_info=True)
            return []

    def get_inflight_payments_for_recovery(
        self,
        guild_ids: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Compatibility wrapper for restart-time payment recovery fetches."""
        return self.get_inflight_payment_requests_for_recovery(guild_ids=guild_ids)

    def get_pending_confirmation_payments(
        self,
        guild_ids: Optional[List[int]] = None,
    ) -> List[Dict]:
        """Fetch pending confirmations for persistent Discord view re-registration."""
        writable_guild_ids = self._get_writable_guild_ids(guild_ids)
        if writable_guild_ids == [] or not self.supabase:
            return []

        try:
            query = (
                self.supabase.table('payment_requests')
                .select('*')
                .eq('status', 'pending_confirmation')
            )
            if writable_guild_ids:
                if len(writable_guild_ids) == 1:
                    query = query.eq('guild_id', writable_guild_ids[0])
                else:
                    query = query.in_('guild_id', writable_guild_ids)
            result = query.order('created_at').execute()
            return result.data or []
        except Exception as e:
            logger.error("Error fetching pending confirmation payments: %s", e, exc_info=True)
            return []

    def mark_member_first_shared(self, member_id: int, guild_id: Optional[int] = None) -> bool:
        """Set first_shared_at timestamp for a member (only if not already set).

        Returns:
            True if this was their first share (timestamp was set), False otherwise
        """
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        
        try:
            # Check if already shared
            result = (
                self.storage_handler.supabase_client.table('members')
                .select('first_shared_at')
                .eq('member_id', member_id)
                .execute()
            )
            
            if result.data and result.data[0].get('first_shared_at'):
                # Already has first_shared_at set
                return False
            
            # Set first_shared_at
            (
                self.storage_handler.supabase_client.table('members')
                .update({'first_shared_at': datetime.now().isoformat()})
                .eq('member_id', member_id)
                .execute()
            )
            logger.info(f"Marked first share for member {member_id}")
            return True
        except Exception as e:
            logger.error(f"Error marking member first shared: {e}", exc_info=True)
            return False

    def get_channel(self, channel_id: int) -> Optional[Dict]:
        """Get channel info by ID."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.execute_raw_sql(
                    "SELECT * FROM discord_channels WHERE channel_id = ? LIMIT 1",
                    (channel_id,)
                )
            )
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Supabase query failed for get_channel: {e}")
            return None

    def create_or_update_channel(self, channel_id: int, channel_name: str, nsfw: bool = False,
                                category_id: Optional[int] = None, guild_id: Optional[int] = None,
                                channel_type: Optional[str] = None,
                                parent_id: Optional[int] = None) -> bool:
        """Create or update a channel in Supabase."""
        if not self._gate_check(guild_id):
            return False

        channel_data = {
            'channel_id': channel_id,
            'channel_name': channel_name,
            'nsfw': nsfw,
            'category_id': category_id
        }
        if guild_id is not None:
            channel_data['guild_id'] = guild_id
        if channel_type is not None:
            channel_data['channel_type'] = channel_type
        if parent_id is not None:
            channel_data['parent_id'] = parent_id

        if self.storage_handler:
            try:
                stored = self._run_async_in_thread(
                    self.storage_handler.store_channels_to_supabase([channel_data])
                )
                return stored > 0
            except Exception as e:
                logger.error(f"Error storing channel to Supabase: {e}", exc_info=True)
                return False
        return False

    def get_messages_after(self, date: datetime, guild_id: Optional[int] = None) -> List[Dict]:
        """Get messages after a certain date."""
        try:
            result = self._run_async_in_thread(
                self.query_handler.get_messages_after(date, guild_id=guild_id)
            )
            return result
        except Exception as e:
            logger.error(f"Supabase query failed for get_messages_after: {e}")
            return []

    def get_messages_by_ids(self, message_ids: List[int]) -> List[Dict]:
        """Get messages by their IDs."""
        return self._run_async_in_thread(
            self.query_handler.get_messages_by_ids(message_ids)
        )

    # ========== Timed Mutes ==========

    def set_is_speaker(self, member_id: int, is_speaker: bool,
                       guild_id: Optional[int] = None) -> bool:
        """Update guild-scoped speaker state for a member.

        The canonical state lives on guild_members.speaker_muted so muting a
        member in one guild does not affect the same Discord account elsewhere.
        """
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for set_is_speaker")
            return False

        try:
            if guild_id is None:
                logger.error("set_is_speaker requires guild_id")
                return False

            (
                self.storage_handler.supabase_client.table('guild_members')
                .upsert({
                    'guild_id': guild_id,
                    'member_id': member_id,
                    'speaker_muted': not is_speaker,
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                })
                .execute()
            )
            logger.info(f"Set guild speaker state is_speaker={is_speaker} for member {member_id} in guild {guild_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting is_speaker for member {member_id}: {e}", exc_info=True)
            return False

    def get_muted_member_ids(self, guild_id: Optional[int] = None) -> List[int]:
        """Return member IDs muted in a guild via guild_members.speaker_muted."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []

        try:
            query = (
                self.storage_handler.supabase_client.table('guild_members')
                .select('member_id')
                .eq('speaker_muted', True)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return [row['member_id'] for row in (result.data or [])]
        except Exception as e:
            logger.error(f"Error fetching muted member IDs: {e}", exc_info=True)
            return []

    def get_is_speaker(self, member_id: int, guild_id: Optional[int] = None) -> bool:
        """Check if a member should have the Speaker role in a guild.

        Defaults to True unless guild_members.speaker_muted is explicitly True.
        Falls back to the legacy members.is_speaker only when no guild_id
        is supplied.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return True

        try:
            if guild_id is not None:
                result = (
                    self.storage_handler.supabase_client.table('guild_members')
                    .select('speaker_muted')
                    .eq('guild_id', guild_id)
                    .eq('member_id', member_id)
                    .limit(1)
                    .execute()
                )
                if result.data:
                    return result.data[0].get('speaker_muted') is not True
                return True

            result = (
                self.storage_handler.supabase_client.table('members')
                .select('is_speaker')
                .eq('member_id', member_id)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get('is_speaker') is not False
            return True
        except Exception as e:
            logger.error(f"Error getting is_speaker for member {member_id}: {e}", exc_info=True)
            return True

    def create_timed_mute(self, member_id: int, guild_id: int, mute_end_at: str, reason: Optional[str] = None, muted_by_id: Optional[int] = None) -> bool:
        """Upsert a timed mute record."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for create_timed_mute")
            return False

        try:
            data = {
                'member_id': member_id,
                'guild_id': guild_id,
                'mute_end_at': mute_end_at,
                'reason': reason,
                'muted_by_id': muted_by_id,
            }
            self.storage_handler.supabase_client.table('timed_mutes').upsert(data).execute()
            logger.info(f"Created timed mute for member {member_id} in guild {guild_id}, expires {mute_end_at}")
            return True
        except Exception as e:
            logger.error(f"Error creating timed mute for member {member_id}: {e}", exc_info=True)
            return False

    def get_expired_mutes(self) -> List[Dict]:
        """Get all timed mutes that have expired."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for get_expired_mutes")
            return []

        try:
            now = datetime.now(timezone.utc).isoformat()
            result = (
                self.storage_handler.supabase_client.table('timed_mutes')
                .select('*')
                .lte('mute_end_at', now)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching expired mutes: {e}", exc_info=True)
            return []

    def delete_timed_mute(self, member_id: int, guild_id: int) -> bool:
        """Delete a timed mute record."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for delete_timed_mute")
            return False

        try:
            (
                self.storage_handler.supabase_client.table('timed_mutes')
                .delete()
                .eq('member_id', member_id)
                .eq('guild_id', guild_id)
                .execute()
            )
            logger.info(f"Deleted timed mute for member {member_id} in guild {guild_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting timed mute for member {member_id}: {e}", exc_info=True)
            return False

    # ========== Channel Speaker Modes ==========

    def get_all_channel_speaker_modes(self, guild_id: Optional[int] = None) -> Dict[int, str]:
        """Bulk fetch speaker_mode for channels, optionally scoped to a guild.

        Returns:
            Dict mapping channel_id (int) -> speaker_mode string.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for get_all_channel_speaker_modes")
            return {}

        try:
            query = (
                self.storage_handler.supabase_client.table('discord_channels')
                .select('channel_id,speaker_mode')
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return {
                row['channel_id']: row.get('speaker_mode', 'normal')
                for row in (result.data or [])
            }
        except Exception as e:
            logger.error(f"Error fetching channel speaker modes: {e}", exc_info=True)
            return {}

    def set_channel_speaker_mode(self, channel_id: int, mode: str,
                                    guild_id: Optional[int] = None) -> bool:
        """Update the speaker_mode for a single channel.

        Args:
            channel_id: Discord channel ID.
            mode: One of 'normal', 'readonly', 'exempt'.
            guild_id: Guild ID for gate check.

        Returns:
            True if update succeeded.
        """
        if mode not in ('normal', 'readonly', 'exempt'):
            logger.error(f"Invalid speaker_mode '{mode}' for channel {channel_id}")
            return False

        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for set_channel_speaker_mode")
            return False

        try:
            (
                self.storage_handler.supabase_client.table('discord_channels')
                .update({'speaker_mode': mode})
                .eq('channel_id', channel_id)
                .execute()
            )
            logger.info(f"Set speaker_mode='{mode}' for channel {channel_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting speaker_mode for channel {channel_id}: {e}", exc_info=True)
            return False

    def ensure_channel_exists(self, channel_id: int, channel_name: str,
                              category_id: Optional[int] = None, nsfw: bool = False,
                              guild_id: Optional[int] = None,
                              channel_type: Optional[str] = None,
                              parent_id: Optional[int] = None) -> bool:
        """Upsert a channel row without overwriting speaker_mode.

        If the channel already exists, only channel_name/category_id/nsfw are refreshed.
        If it doesn't exist, it's created with speaker_mode defaulting to 'normal' (DB default).

        Returns:
            True if upsert succeeded.
        """
        if not self._gate_check(guild_id):
            return False

        if not self.storage_handler or not self.storage_handler.supabase_client:
            logger.error("Supabase client not initialized for ensure_channel_exists")
            return False

        try:
            data = {
                'channel_id': channel_id,
                'channel_name': channel_name,
                'category_id': category_id,
                'nsfw': nsfw,
            }
            if guild_id is not None:
                data['guild_id'] = guild_id
            if channel_type is not None:
                data['channel_type'] = channel_type
            if parent_id is not None:
                data['parent_id'] = parent_id
            (
                self.storage_handler.supabase_client.table('discord_channels')
                .upsert(data)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error in ensure_channel_exists for channel {channel_id}: {e}", exc_info=True)
            return False

    # ========== Onboarding Defaults ==========

    def get_onboarding_default_ids(self, guild_id: Optional[int] = None) -> List[int]:
        """Return channel IDs where onboarding_default is True."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []

        try:
            query = (
                self.storage_handler.supabase_client.table('discord_channels')
                .select('channel_id')
                .eq('onboarding_default', True)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return [row['channel_id'] for row in (result.data or [])]
        except Exception as e:
            logger.error(f"Error fetching onboarding default IDs: {e}", exc_info=True)
            return []

    # ========== Pending Intros (Gated Entry) ==========

    def create_pending_intro(self, member_id: int, message_id: int, channel_id: int,
                            guild_id: Optional[int] = None,
                            approval_request_id: Optional[str] = None) -> Optional[Dict]:
        """Insert a new pending intro record."""
        if not self._gate_check(guild_id):
            return None
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            data = {
                'member_id': member_id,
                'message_id': message_id,
                'channel_id': channel_id,
            }
            if guild_id is not None:
                data['guild_id'] = guild_id
            if approval_request_id is not None:
                data['approval_request_id'] = approval_request_id
            result = self.storage_handler.supabase_client.table('pending_intros').insert(data).execute()
            return result.data[0] if result.data else data
        except Exception as e:
            if self._is_unique_violation(e):
                logger.info(f"Pending intro already exists for approval request {approval_request_id}")
                return None
            logger.error(f"Error creating pending intro for member {member_id}: {e}", exc_info=True)
            raise

    @staticmethod
    def _is_unique_violation(exc: Exception) -> bool:
        """Return True for Postgres unique-violation errors surfaced by supabase-py."""
        return (
            getattr(exc, 'code', None) == '23505'
            or getattr(exc, 'sqlstate', None) == '23505'
            or '23505' in str(exc)
            or 'duplicate key' in str(exc).lower()
        )

    @staticmethod
    def _media_preview_url(media: Optional[Dict]) -> Optional[str]:
        if not media:
            return None
        return (
            media.get('backup_thumbnail_url')
            or media.get('cloudflare_thumbnail_url')
            or media.get('url')
        )

    def _get_media_for_embed(self, media_id: Optional[str]) -> Optional[Dict]:
        if not media_id or not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('media')
                .select('id,title,url,type,cloudflare_thumbnail_url,backup_thumbnail_url,cloudflare_playback_hls_url,thumbnail_placeholder,admin_status')
                .eq('id', media_id)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            media = result.data[0]
            media['preview_url'] = self._media_preview_url(media)
            media['profile_url'] = f"https://banodoco.ai/art/{media['id']}"
            return media
        except Exception as e:
            logger.error(f"Error hydrating media {media_id} for approval request: {e}", exc_info=True)
            return None

    def _get_asset_for_embed(self, asset_id: Optional[str]) -> Optional[Dict]:
        if not asset_id or not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('assets')
                .select('id,type,name,description,primary_media_id,download_link,slug,status,admin_status')
                .eq('id', asset_id)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            asset = result.data[0]
            primary_media = self._get_media_for_embed(asset.get('primary_media_id'))
            if primary_media:
                asset['primary_media'] = primary_media
                asset['preview_url'] = self._media_preview_url(primary_media)
            if asset.get('slug'):
                asset['profile_url'] = f"https://banodoco.ai/resources/{asset['slug']}"
            elif asset.get('id'):
                asset['profile_url'] = f"https://banodoco.ai/resources/{asset['id']}"
            return asset
        except Exception as e:
            logger.error(f"Error hydrating asset {asset_id} for approval request: {e}", exc_info=True)
            return None

    def claim_pending_approval_requests(self, limit: int = 25) -> List[Dict]:
        """Return pending approval requests that still need intro posts."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client
                .rpc('claim_pending_approval_requests', {'p_limit': limit})
                .execute()
            )
            rows = result.data or []
            for row in rows:
                row['media'] = self._get_media_for_embed(row.get('attached_media_id'))
                row['asset'] = self._get_asset_for_embed(row.get('attached_resource_id'))
            return rows
        except Exception as e:
            logger.error(f"Error claiming pending approval requests: {e}", exc_info=True)
            return []

    def mark_approval_request_posted(self, approval_request_id: str, message_id: int) -> bool:
        """Stamp the Discord intro message id on an approval request."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            result = (
                self.storage_handler.supabase_client.table('approval_requests')
                .update({'posted_message_id': message_id})
                .eq('id', approval_request_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error marking approval request {approval_request_id} posted: {e}", exc_info=True)
            return False

    def get_member_for_approval(self, member_id: int) -> Optional[Dict]:
        """Fetch a members row for an approval request."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('members')
                .select('*')
                .eq('member_id', member_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching member {member_id} for approval request: {e}", exc_info=True)
            return None

    def get_approval_request(self, approval_request_id: str) -> Optional[Dict]:
        """Fetch one approval request by id."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('approval_requests')
                .select('*')
                .eq('id', approval_request_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching approval request {approval_request_id}: {e}", exc_info=True)
            return None

    def get_pending_intro_by_approval_request(self, approval_request_id: str) -> Optional[Dict]:
        """Fetch the pending intro bridged to an approval request."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('approval_request_id', approval_request_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching pending intro for approval request {approval_request_id}: {e}", exc_info=True)
            return None

    def claim_dirty_intro_edits(self, limit: int = 25) -> List[Dict]:
        """Return up to `limit` approval_requests rows whose Discord embed needs re-rendering.

        A row qualifies when ALL of:
          - status = 'pending'
          - embed_dirty = true
          - posted_message_id IS NOT NULL
          - embed_updated_at IS NULL OR embed_updated_at < now() - interval '30 seconds'

        Ordered by embed_updated_at NULLS FIRST, then created_at, so the oldest stale
        rows are refreshed first.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            threshold = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            result = (
                self.storage_handler.supabase_client
                .from_('approval_requests')
                .select(
                    'id,member_id,attached_media_id,attached_resource_id,'
                    'posted_message_id,bio_snapshot,status,embed_dirty,'
                    'embed_updated_at,created_at'
                )
                .eq('status', 'pending')
                .eq('embed_dirty', True)
                .not_.is_('posted_message_id', 'null')
                .or_(f'embed_updated_at.is.null,embed_updated_at.lt.{threshold}')
                .order('embed_updated_at', desc=False, nullsfirst=True)
                .order('created_at', desc=False)
                .limit(limit)
                .execute()
            )
            rows = result.data or []
            for row in rows:
                row['media'] = self._get_media_for_embed(row.get('attached_media_id'))
                row['asset'] = self._get_asset_for_embed(row.get('attached_resource_id'))
            return rows
        except Exception as e:
            logger.error(f"Error claiming dirty intro edits: {e}", exc_info=True)
            return []

    def mark_embed_updated(self, approval_request_id: str) -> bool:
        """Clear embed_dirty and stamp embed_updated_at=now() after a successful re-render."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            result = (
                self.storage_handler.supabase_client.table('approval_requests')
                .update({'embed_dirty': False, 'embed_updated_at': now_iso})
                .eq('id', approval_request_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error marking embed updated for approval request {approval_request_id}: {e}", exc_info=True)
            return False

    def stamp_embed_retry_attempt(self, approval_request_id: str) -> bool:
        """Stamp embed_updated_at=now() without clearing embed_dirty after an edit failure."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            result = (
                self.storage_handler.supabase_client.table('approval_requests')
                .update({'embed_updated_at': now_iso})
                .eq('id', approval_request_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error stamping embed retry for approval request {approval_request_id}: {e}", exc_info=True)
            return False

    def clear_posted_message_id(self, approval_request_id: str) -> bool:
        """Clear posted_message_id so the post-loop re-creates the embed.

        Used when the original Discord message was deleted (e.g. by a mod) and we
        want the next poll tick to send a fresh message.
        """
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            result = (
                self.storage_handler.supabase_client.table('approval_requests')
                .update({'posted_message_id': None})
                .eq('id', approval_request_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error clearing posted_message_id for approval request {approval_request_id}: {e}", exc_info=True)
            return False

    def list_unstamped_intros(self) -> List[Dict]:
        """Return bridged pending intros whose approval request lacks posted_message_id."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*, approval_requests!inner(id,posted_message_id)')
                .not_.is_('approval_request_id', 'null')
                .is_('approval_requests.posted_message_id', 'null')
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error listing unstamped approval intro rows: {e}", exc_info=True)
            return []

    def update_pending_intro_message(self, intro_id: int, message_id: int, channel_id: int) -> bool:
        """Update the message_id on an existing pending intro (member reposted)."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('pending_intros').update({
                'message_id': message_id,
                'channel_id': channel_id,
            }).eq('id', intro_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating pending intro {intro_id} to message {message_id}: {e}", exc_info=True)
            return False

    def get_pending_intro_by_member(self, member_id: int, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Return the latest pending intro for a member, or None."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            query = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('member_id', member_id)
                .eq('status', 'pending')
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.order('created_at', desc=True).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching pending intro for member {member_id}: {e}", exc_info=True)
            return None

    def get_pending_intro_by_message(self, message_id: int) -> Optional[Dict]:
        """Lookup a pending intro by message ID."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            result = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('message_id', message_id)
                .eq('status', 'pending')
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching pending intro for message {message_id}: {e}", exc_info=True)
            return None

    def approve_pending_intro(self, message_id: int, guild_id: Optional[int] = None) -> bool:
        """Mark a pending intro as approved."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            query = self.storage_handler.supabase_client.table('pending_intros').update({
                'status': 'approved',
                'approved_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('status', 'pending')
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            query.execute()
            return True
        except Exception as e:
            logger.error(f"Error approving pending intro for message {message_id}: {e}", exc_info=True)
            return False

    def expire_pending_intro(self, message_id: int, guild_id: Optional[int] = None) -> bool:
        """Mark a pending intro as expired."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            query = self.storage_handler.supabase_client.table('pending_intros').update({
                'status': 'expired',
                'expired_at': datetime.now(timezone.utc).isoformat(),
            }).eq('message_id', message_id).eq('status', 'pending')
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            query.execute()
            return True
        except Exception as e:
            logger.error(f"Error expiring pending intro for message {message_id}: {e}", exc_info=True)
            return False

    def get_expired_pending_intros(self, expiry_days: int = 7, guild_id: Optional[int] = None) -> List[Dict]:
        """Return pending intros older than expiry_days."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=expiry_days)).isoformat()
            query = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('status', 'pending')
                .lt('created_at', cutoff)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching expired pending intros: {e}", exc_info=True)
            return []

    def get_recently_approved_intros(self, hours: int = 24, guild_id: Optional[int] = None) -> List[Dict]:
        """Return intros approved within the last N hours."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            query = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('status', 'approved')
                .gte('approved_at', cutoff)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching recently approved intros: {e}", exc_info=True)
            return []

    def get_all_pending_intros(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Return all pending intros (for bot restart recovery)."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('pending_intros')
                .select('*')
                .eq('status', 'pending')
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching all pending intros: {e}", exc_info=True)
            return []

    def record_intro_vote(self, intro_id: int, message_id: int, voter_id: int, voter_role: str,
                         guild_id: Optional[int] = None) -> bool:
        """Record a vote on an intro. Returns False if already voted."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('intro_votes').insert({
                'intro_id': intro_id,
                'message_id': message_id,
                'voter_id': voter_id,
                'voter_role': voter_role,
            }).execute()
            return True
        except Exception as e:
            # Unique constraint violation means already voted
            if 'duplicate' in str(e).lower() or '23505' in str(e):
                return False
            logger.error(f"Error recording intro vote: {e}", exc_info=True)
            return False

    # ========== Grant Applications ==========

    def create_grant_application(self, thread_id: int, applicant_id: int, thread_content: str,
                                 attachment_urls: Optional[List] = None,
                                 guild_id: Optional[int] = None) -> bool:
        """Insert a new grant application record."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            data = {
                'thread_id': thread_id,
                'applicant_id': applicant_id,
                'thread_content': thread_content,
                'status': 'reviewing',
            }
            if guild_id:
                data['guild_id'] = guild_id
            if attachment_urls:
                data['attachment_urls'] = attachment_urls
            self.storage_handler.supabase_client.table('grant_applications').insert(data).execute()
            return True
        except Exception as e:
            logger.error(f"Error creating grant application for thread {thread_id}: {e}", exc_info=True)
            return False

    def get_grant_by_thread(self, thread_id: int, guild_id: Optional[int] = None) -> Optional[Dict]:
        """Return the grant application for a thread, or None."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            query = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('*')
                .eq('thread_id', thread_id)
            )
            if guild_id:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching grant for thread {thread_id}: {e}", exc_info=True)
            return None

    def update_grant_status(self, thread_id: int, status: str, guild_id: Optional[int] = None, **kwargs) -> bool:
        """Update a grant application's status and any additional fields."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            data = {'status': status}
            # Handle timestamp fields that use 'now()'
            for key, value in kwargs.items():
                if value == 'now()':
                    data[key] = datetime.now(timezone.utc).isoformat()
                else:
                    data[key] = value
            query = (
                self.storage_handler.supabase_client.table('grant_applications')
                .update(data)
                .eq('thread_id', thread_id)
            )
            if guild_id:
                query = query.eq('guild_id', guild_id)
            query.execute()
            return True
        except Exception as e:
            logger.error(f"Error updating grant status for thread {thread_id}: {e}", exc_info=True)
            return False

    def record_grant_payment(self, thread_id: int, tx_signature: str, sol_amount: float, sol_price_usd: float,
                             guild_id: Optional[int] = None) -> bool:
        """Record a successful grant payment."""
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            query = (
                self.storage_handler.supabase_client.table('grant_applications')
                .update({
                    'status': 'paid',
                    'payment_status': 'confirmed',
                    'tx_signature': tx_signature,
                    'sol_amount': sol_amount,
                    'sol_price_usd': sol_price_usd,
                    'paid_at': datetime.now(timezone.utc).isoformat(),
                })
                .eq('thread_id', thread_id)
            )
            if guild_id:
                query = query.eq('guild_id', guild_id)
            query.execute()
            return True
        except Exception as e:
            logger.error(f"Error recording grant payment for thread {thread_id}: {e}", exc_info=True)
            return False

    def get_inflight_payments(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Return grants where payment needs recovery: in-flight or pending retry."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('*')
                .in_('payment_status', ['sending', 'sent', 'retry'])
            )
            if guild_id:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching inflight payments: {e}", exc_info=True)
            return []

    def get_member_engagement(self, member_id: int, guild_id: Optional[int] = None) -> Dict:
        """Get engagement stats for a member: total message count and last 20 messages >50 chars."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return {'total_messages': 0, 'recent_messages': []}
        try:
            sb = self.storage_handler.supabase_client
            # Total message count
            count_q = sb.table('discord_messages').select('message_id', count='exact').eq('author_id', member_id)
            if guild_id:
                count_q = count_q.eq('guild_id', guild_id)
            count_resp = count_q.execute()
            total = count_resp.count or 0
            # Last 20 substantive messages (>50 chars)
            msgs_q = (
                sb.table('discord_messages')
                .select('content,channel_id,created_at')
                .eq('author_id', member_id)
                .gt('content', '')  # non-empty
                .order('created_at', desc=True)
                .limit(100)
            )
            if guild_id:
                msgs_q = msgs_q.eq('guild_id', guild_id)
            msgs_resp = msgs_q.execute()
            # Filter to >50 chars client-side (Supabase REST can't filter by length)
            substantive = [
                {'content': m['content'][:200], 'channel_id': m['channel_id'], 'created_at': m['created_at'][:10]}
                for m in (msgs_resp.data or [])
                if m.get('content') and len(m['content']) > 50
            ][:20]
            return {'total_messages': total, 'recent_messages': substantive}
        except Exception as e:
            logger.error(f"Error fetching engagement for member {member_id}: {e}", exc_info=True)
            return {'total_messages': 0, 'recent_messages': []}

    def get_active_grants_for_applicant(self, applicant_id: int, guild_id: Optional[int] = None) -> List[Dict]:
        """Return active (non-terminal) grant applications for a user."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('*')
                .eq('applicant_id', applicant_id)
                .in_('status', ['reviewing', 'awaiting_wallet', 'payment_requested'])
            )
            if guild_id:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching active grants for applicant {applicant_id}: {e}", exc_info=True)
            return []

    def get_grant_history_for_applicant(self, applicant_id: int, guild_id: Optional[int] = None) -> List[Dict]:
        """Return all past grant applications for a user (any status)."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('grant_applications')
                .select('thread_id,status,gpu_type,recommended_hours,total_cost_usd,created_at,paid_at')
                .eq('applicant_id', applicant_id)
                .order('created_at', desc=True)
            )
            if guild_id:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching grant history for applicant {applicant_id}: {e}", exc_info=True)
            return []

    def get_messages_in_range(self, start_date: datetime, end_date: datetime,
                             channel_id: Optional[int] = None,
                             guild_id: Optional[int] = None) -> List[Dict]:
        """Get messages within a date range."""
        try:
            logger.debug(f"Querying messages in range from Supabase (channel_id={channel_id}, guild_id={guild_id})")
            return self._run_async_in_thread(
                self.query_handler.get_messages_in_range(start_date, end_date, channel_id,
                                                         guild_id=guild_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    def get_messages_by_authors_in_range(self, author_ids: List[int], start_date: datetime,
                                        end_date: datetime, guild_id: Optional[int] = None) -> List[Dict]:
        """Get messages by specific authors in a date range."""
        try:
            logger.debug(f"Querying messages by authors from Supabase ({len(author_ids)} authors)")
            return self._run_async_in_thread(
                self.query_handler.get_messages_by_authors_in_range(author_ids, start_date, end_date,
                                                                     guild_id=guild_id)
            )
        except Exception as e:
            logger.error(f"Supabase query failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Competitions
    # ------------------------------------------------------------------

    def upsert_competition(self, data: Dict, guild_id: Optional[int] = None) -> bool:
        payload = dict(data)
        effective_guild_id = guild_id or payload.get('guild_id')
        slug = payload.get('slug')
        if effective_guild_id is not None:
            payload['guild_id'] = effective_guild_id
        payload['type'] = 'community'
        if effective_guild_id is None or not slug:
            logger.error("upsert_competition requires guild_id and slug")
            return False
        if not self._gate_check(effective_guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            existing = (
                self.storage_handler.supabase_client.table('competitions')
                .select('id')
                .eq('type', 'community')
                .eq('guild_id', effective_guild_id)
                .eq('slug', slug)
                .limit(1)
                .execute()
            )

            if existing.data:
                (
                    self.storage_handler.supabase_client.table('competitions')
                    .update(payload)
                    .eq('id', existing.data[0]['id'])
                    .execute()
                )
            else:
                self.storage_handler.supabase_client.table('competitions').insert(payload).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting competition: {e}", exc_info=True)
            return False

    def get_competition(self, slug: str, guild_id: Optional[int] = None) -> Optional[Dict]:
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return None
        try:
            query = (
                self.storage_handler.supabase_client.table('competitions')
                .select('*')
                .eq('type', 'community')
                .eq('slug', slug)
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error fetching competition {slug}: {e}", exc_info=True)
            return None

    def get_active_competitions(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Return competitions with status 'voting'."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('competitions')
                .select('*')
                .eq('type', 'community')
                .eq('status', 'voting')
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching active competitions: {e}", exc_info=True)
            return []

    def get_scheduled_competitions(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Return competitions in 'setup' status that have a voting_start set."""
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('competitions')
                .select('*')
                .eq('type', 'community')
                .eq('status', 'setup')
                .not_.is_('voting_start', 'null')
            )
            if guild_id is not None:
                query = query.eq('guild_id', guild_id)
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching scheduled competitions: {e}", exc_info=True)
            return []

    def update_competition(self, slug: str, data: Dict, guild_id: Optional[int] = None) -> bool:
        payload = dict(data)
        effective_guild_id = guild_id or payload.get('guild_id')
        if effective_guild_id is not None:
            payload['guild_id'] = effective_guild_id
        if effective_guild_id is None:
            logger.error(f"update_competition({slug}): guild_id is required")
            return False
        payload['type'] = 'community'
        if not self._gate_check(effective_guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('competitions')
                .update(payload)
                .eq('type', 'community')
                .eq('slug', slug)
                .eq('guild_id', effective_guild_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error updating competition {slug}: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Competition entries
    # ------------------------------------------------------------------

    def upsert_competition_entry(self, entry: Dict, guild_id: Optional[int] = None) -> bool:
        payload = dict(entry)
        competition_id = payload.get('competition_id')
        if not competition_id:
            logger.error("upsert_competition_entry requires competition_id")
            return False
        if 'author_id' in payload and 'member_id' not in payload:
            payload['member_id'] = payload.pop('author_id')
        payload['entry_type'] = 'community'
        if guild_id is not None and not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            self.storage_handler.supabase_client.table('competition_entries').upsert(
                payload, on_conflict='competition_id,message_id'
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error upserting competition entry: {e}", exc_info=True)
            return False

    def get_competition_entries(self, competition_id: str, guild_id: Optional[int] = None) -> List[Dict]:
        if guild_id is not None and not self._gate_check(guild_id):
            return []
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return []
        try:
            query = (
                self.storage_handler.supabase_client.table('competition_entries')
                .select('*')
                .eq('competition_id', competition_id)
                .eq('entry_type', 'community')
                .order('created_at')
            )
            result = query.execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching competition entries for {competition_id}: {e}", exc_info=True)
            return []

    def delete_competition_entry(self, competition_id: str, message_id: int, guild_id: Optional[int] = None) -> bool:
        if not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('competition_entries')
                .delete()
                .eq('competition_id', competition_id)
                .eq('message_id', message_id)
                .eq('entry_type', 'community')
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error deleting competition entry {message_id}: {e}", exc_info=True)
            return False

    def clear_competition_entries(self, competition_id: str, guild_id: Optional[int] = None) -> bool:
        if guild_id is not None and not self._gate_check(guild_id):
            return False
        if not self.storage_handler or not self.storage_handler.supabase_client:
            return False
        try:
            (
                self.storage_handler.supabase_client.table('competition_entries')
                .delete()
                .eq('competition_id', competition_id)
                .eq('entry_type', 'community')
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"Error clearing competition entries for {competition_id}: {e}", exc_info=True)
            return False
