# src/common/openmuse_interactor.py

import logging
import asyncio
from typing import Optional, Dict, Any
import discord
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from postgrest.exceptions import APIError # For specific Postgrest errors
import re  # For URL validation

# --- Added imports needed for upload logic ---
import cv2
import tempfile
import os
import httpx
from urllib.parse import quote # For profile URL generation
# --- End Added imports ---

# Helper function for logging truncation (can be outside class or a private method)
def _get_truncated_data_for_logging(data_obj: Any, avatar_key: str = 'discord_avatar_base64', max_len: int = 70) -> Any:
    if isinstance(data_obj, dict):
        copied_dict = data_obj.copy()
        if avatar_key in copied_dict and isinstance(copied_dict[avatar_key], str):
            full_avatar_string = copied_dict[avatar_key]
            prefix_to_keep = ""
            if "base64," in full_avatar_string:
                parts = full_avatar_string.split("base64,", 1)
                prefix_to_keep = parts[0] + "base64,"
                actual_b64_data = parts[1]
            else:
                actual_b64_data = full_avatar_string
            
            if len(actual_b64_data) > max_len:
                copied_dict[avatar_key] = prefix_to_keep + actual_b64_data[:max_len] + "... (truncated)"
        return copied_dict
    elif isinstance(data_obj, list):
        return [_get_truncated_data_for_logging(item, avatar_key, max_len) for item in data_obj]
    # Add handling for Supabase response objects if needed, by accessing their .data attribute
    elif hasattr(data_obj, 'data'): # Heuristic for Supabase response like objects
        # Don't modify original object, create a representation for logging
        class ResponseLogWrapper:
            def __init__(self, original_response, truncated_data):
                self.original_response = original_response
                self.truncated_data = truncated_data
            def __repr__(self):
                original_attrs = {attr: getattr(self.original_response, attr) for attr in dir(self.original_response) if not attr.startswith('_') and not callable(getattr(self.original_response, attr)) and attr != 'data'}
                return f"SupabaseResponse(data={self.truncated_data}, {original_attrs})"

        truncated_data_list = _get_truncated_data_for_logging(data_obj.data, avatar_key, max_len)
        return ResponseLogWrapper(data_obj, truncated_data_list)
    return data_obj

# Define the structure based on provided schema (for reference)
# All profile data lives in the members table.
MEMBERS_TABLE = "members"
MEDIA_TABLE = "media"
VIDEO_BUCKET_NAME = "videos"
THUMBNAIL_BUCKET_NAME = "thumbnails"
# --- Added constants for upload logic ---
MAX_UPLOAD_ATTEMPTS = 3
BASE_RETRY_DELAY_SECONDS = 2
MAX_FILE_SIZE_BYTES = 512 * 1024 * 1024 # 512 MiB - Make this configurable?
# --- End Added constants ---

# Refined helper function for truncating avatar string within a dictionary for logging
def _truncate_avatar_in_dict_for_logging(data_dict: Optional[Dict], avatar_key: str = 'discord_avatar_base64', max_len: int = 70) -> Optional[Dict]:
    if not isinstance(data_dict, dict):
        return data_dict # Return as is if not a dict (e.g., None)
    copied_dict = data_dict.copy() # Work on a copy for logging
    if avatar_key in copied_dict and isinstance(copied_dict[avatar_key], str):
        full_avatar_string = copied_dict[avatar_key]
        prefix_to_keep = ""
        actual_b64_data = full_avatar_string
        try:
            prefix_end_idx = full_avatar_string.find("base64,")
            if prefix_end_idx != -1:
                prefix_to_keep = full_avatar_string[:prefix_end_idx + len("base64,")]
                actual_b64_data = full_avatar_string[len(prefix_to_keep):]
        except Exception:
            pass # If string operations fail, proceed with actual_b64_data as full_avatar_string
        
        if len(actual_b64_data) > max_len:
            copied_dict[avatar_key] = prefix_to_keep + actual_b64_data[:max_len] + "... (truncated)"
    return copied_dict

class OpenMuseInteractor:
    """Handles interactions with the OpenMuse Supabase backend."""

    MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_BYTES

    def __init__(self, supabase_url: str, supabase_key: str, logger: logging.Logger):
        """
        Initializes the OpenMuseInteractor.

        Args:
            supabase_url: The URL for the Supabase project.
            supabase_key: The Supabase service role key.
            logger: The logger instance for logging messages.
        """
        self.logger = logger
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_BYTES
        self.supabase: Optional[Client] = self._init_supabase()

    async def _select_single_row(
        self,
        table_name: str,
        filters: Dict[str, Any],
        columns: str = "*"
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single row matching all equality filters."""
        if not self.supabase:
            return None

        def _execute():
            query = self.supabase.table(table_name).select(columns)
            for key, value in filters.items():
                query = query.eq(key, value)
            return query.limit(1).execute()

        response = await asyncio.to_thread(_execute)
        if response.data:
            return response.data[0]
        return None

    def _build_member_profile(self, member_row: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize the shared members row into the legacy profile shape."""
        member_id = int(member_row["member_id"])
        return {
            "id": str(member_id),
            "member_id": member_id,
            "discord_user_id": str(member_id),
            "username": member_row.get("username"),
            "discord_username": member_row.get("username"),
            "display_name": member_row.get("global_name") or member_row.get("username"),
            "avatar_url": member_row.get("stored_avatar_url") or member_row.get("avatar_url"),
            "description": member_row.get("bio"),
            "real_name": member_row.get("real_name"),
            "links": [],
            "background_image_url": None,
            "discord_connected": None,
            "created_at": member_row.get("created_at"),
            "updated_at": member_row.get("updated_at"),
        }

    async def _mark_profile_connected_and_send_welcome_dm(
        self,
        member_id: str,
        author: discord.User | discord.Member,
        profile_data: Dict[str, Any]
    ) -> None:
        """Send the first-upload DM and flip discord_connected once."""
        initial_discord_connected = profile_data.get("discord_connected")
        if initial_discord_connected is not False:
            self.logger.info(
                f"[OpenMuseInteractor] Skipping Welcome DM for member {member_id} "
                f"(Initial Connected Status: {initial_discord_connected})."
            )
            return

        try:
            username_for_url = profile_data.get("username") or author.name
            formatted_username = quote(username_for_url, safe="")
            profile_url = f"https://openmuse.ai/profile/{formatted_username}"
            dm_message_content = (
                "Your first upload to OpenMuse via Discord has been successful!\n\n"
                f"You can see your profile here: {profile_url}"
            )

            self.logger.info(f"[OpenMuseInteractor] --> Sending Welcome DM to user {author.id}.")
            await author.send(dm_message_content)
            self.logger.info(f"[OpenMuseInteractor] <-- Successfully sent Welcome DM to user {author.id}.")

            self.logger.info(
                f"[OpenMuseInteractor] --> Attempting to update discord_connected to True "
                f"for member {member_id}."
            )
            await asyncio.to_thread(
                self.supabase.table(MEMBERS_TABLE)
                .update({"discord_connected": True})
                .eq("member_id", int(member_id))
                .execute
            )
            self.logger.info(
                f"[OpenMuseInteractor] <-- discord_connected status updated for member {member_id}."
            )
            profile_data["discord_connected"] = True
        except discord.Forbidden:
            self.logger.warning(
                f"[OpenMuseInteractor] Failed to send Welcome DM to user {author.id}. DMs disabled?"
            )
        except Exception as dm_update_ex:
            self.logger.error(
                f"[OpenMuseInteractor] Error sending Welcome DM or updating discord_connected "
                f"for member {member_id}: {dm_update_ex}",
                exc_info=True,
            )

    def _init_supabase(self) -> Optional[Client]:
        """Initializes the Supabase client."""
        if not self.supabase_url or not self.supabase_key:
            self.logger.error("[OpenMuseInteractor] Supabase URL or Service Key is missing. Cannot initialize client.")
            return None
        try:
            self.logger.info("[OpenMuseInteractor] Initializing Supabase client.")
            # Try with ClientOptions (newer API)
            try:
                options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=30)
                client: Client = create_client(self.supabase_url, self.supabase_key, options=options)
            except (AttributeError, TypeError):
                # Fall back to creating client without options if ClientOptions API has changed
                client: Client = create_client(self.supabase_url, self.supabase_key)
            self.logger.info("[OpenMuseInteractor] Supabase client initialized successfully.")
            return client
        except Exception as e:
            self.logger.error(f"[OpenMuseInteractor] Failed to initialize Supabase client: {e}", exc_info=True)
            return None

    async def find_or_create_profile(self, user: discord.User | discord.Member) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Ensure the shared members row exists for a Discord member.

        Args:
            user: The discord.User or discord.Member object.

        Returns:
            A tuple containing (profile_data_dict, member_id_str) if successful,
            otherwise (None, None).
        """
        if not self.supabase:
            self.logger.error("[OpenMuseInteractor] Supabase client not initialized. Cannot find or create profile.")
            return None, None

        member_id = int(user.id)
        member_id_str = str(member_id)
        current_avatar_url = str(user.display_avatar.url) if user.display_avatar else None
        current_display_name = getattr(user, "display_name", user.name)
        self.logger.info(
            f"[OpenMuseInteractor] Finding or creating profile for Discord member ID: "
            f"{member_id_str} ({user.name})"
        )

        try:
            member_row = await self._select_single_row(
                MEMBERS_TABLE,
                {"member_id": member_id},
            )
            if member_row:
                member_updates = {}
                if user.name != member_row.get("username"):
                    member_updates["username"] = user.name
                if current_display_name != member_row.get("global_name"):
                    member_updates["global_name"] = current_display_name
                if current_avatar_url != member_row.get("avatar_url"):
                    member_updates["avatar_url"] = current_avatar_url

                if member_updates:
                    self.logger.info(
                        f"[OpenMuseInteractor] Updating members row for {member_id_str} "
                        f"with data: {member_updates}"
                    )
                    await asyncio.to_thread(
                        self.supabase.table(MEMBERS_TABLE)
                        .update(member_updates)
                        .eq("member_id", member_id)
                        .execute
                    )
                    member_row.update(member_updates)
            else:
                self.logger.info(
                    f"[OpenMuseInteractor] No members row found for {member_id_str}. "
                    "Creating one."
                )
                member_payload = {
                    "member_id": member_id,
                    "username": user.name,
                    "global_name": current_display_name,
                    "avatar_url": current_avatar_url,
                }
                insert_response = await asyncio.to_thread(
                    self.supabase.table(MEMBERS_TABLE)
                    .insert(member_payload)
                    .execute
                )
                if not insert_response.data:
                    self.logger.error(
                        f"[OpenMuseInteractor] Supabase insert returned no members "
                        f"row for {member_id_str}."
                    )
                    return None, None
                member_row = insert_response.data[0]

            combined_profile = self._build_member_profile(member_row)
            self.logger.debug(
                f"[OpenMuseInteractor] Resolved canonical profile: "
                f"{_truncate_avatar_in_dict_for_logging(combined_profile)}"
            )
            return _truncate_avatar_in_dict_for_logging(combined_profile), member_id_str

        except APIError as select_err:
             self.logger.error(f"[OpenMuseInteractor] Supabase API error finding profile for {member_id_str}: {select_err}")
             return None, None
        except Exception as e:
            self.logger.error(f"[OpenMuseInteractor] Unexpected error during find_or_create_profile for {member_id_str}: {e}", exc_info=True)
            return None, None

    def _is_valid_url(self, url: str) -> bool:
        """Return True if the URL is a valid HTTP(S) URL."""
        if not url or not isinstance(url, str):
            return False
        # Simple regex for http(s) URLs
        return re.match(r"^https?://[\w\-\.]+(:\d+)?(/[\w\-\.~:/?#\[\]@!$&'()*+,;=%]*)?$", url) is not None

    async def _upload_bytes_to_storage(
        self,
        file_bytes: bytes,
        bucket_name: str,
        storage_path: str,
        content_type: str,
        upsert: bool = True
    ) -> Optional[str]:
        """
        Uploads raw bytes to a specified Supabase storage bucket with retry logic.

        Args:
            file_bytes: The raw bytes of the file to upload.
            bucket_name: The name of the target Supabase storage bucket.
            storage_path: The desired path (including filename) within the bucket.
            content_type: The MIME type of the file.
            upsert: Whether to overwrite the file if it already exists.

        Returns:
            The public URL of the uploaded file if successful, otherwise None.
        """
        if not self.supabase:
            self.logger.error(f"[OpenMuseInteractor_UploadBytes] Supabase client not initialized. Cannot upload to {bucket_name}/{storage_path}.")
            return None

        self.logger.info(f"[OpenMuseInteractor_UploadBytes] --> Attempting to upload {len(file_bytes)} bytes to bucket '{bucket_name}' at path '{storage_path}' (Content-Type: {content_type}).")
        
        upload_successful = False
        for attempt in range(MAX_UPLOAD_ATTEMPTS):
            try:
                await asyncio.to_thread(
                    self.supabase.storage.from_(bucket_name).upload,
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": content_type, "upsert": str(upsert).lower()} # upsert needs to be string "true" or "false"
                )
                self.logger.info(f"[OpenMuseInteractor_UploadBytes] <-- Successfully uploaded to '{bucket_name}/{storage_path}' (Attempt {attempt + 1}).")
                upload_successful = True
                break
            except (Exception, httpx.WriteError) as upload_ex: # Catch generic Exception and specific httpx.WriteError
                self.logger.warning(f"[OpenMuseInteractor_UploadBytes] Upload attempt {attempt + 1}/{MAX_UPLOAD_ATTEMPTS} for '{bucket_name}/{storage_path}' failed: {upload_ex}")
                if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                    delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
                    self.logger.info(f"[OpenMuseInteractor_UploadBytes] Retrying upload in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(f"[OpenMuseInteractor_UploadBytes] Upload for '{bucket_name}/{storage_path}' failed after {MAX_UPLOAD_ATTEMPTS} attempts.")
        
        if not upload_successful:
            return None

        # Get Public URL
        try:
            self.logger.info(f"[OpenMuseInteractor_UploadBytes] --> Getting public URL for '{bucket_name}/{storage_path}'.")
            public_url_response = await asyncio.to_thread(
                self.supabase.storage.from_(bucket_name).get_public_url, storage_path
            )
            public_url = public_url_response
            self.logger.info(f"[OpenMuseInteractor_UploadBytes] <-- Got public URL: {public_url}")

            if isinstance(public_url, str):
                trimmed_url = public_url.strip()
                if trimmed_url != public_url:
                    self.logger.info(f"[OpenMuseInteractor_UploadBytes] Trimmed whitespace from URL: '{public_url}' -> '{trimmed_url}'")
                public_url = trimmed_url
            
            if not self._is_valid_url(public_url):
                self.logger.warning(f"[OpenMuseInteractor_UploadBytes] Invalid or empty URL after trimming: '{public_url}' for '{bucket_name}/{storage_path}'. Setting to None.")
                return None
            return public_url
        except Exception as url_ex:
            self.logger.error(f"[OpenMuseInteractor_UploadBytes] Failed to get public URL for '{bucket_name}/{storage_path}': {url_ex}")
            return None

    async def upload_file_to_storage(
        self,
        file_source: discord.Attachment | bytes,
        bucket_name: str,
        storage_path: str,
        content_type: Optional[str] = None,
        upsert: bool = True
    ) -> Optional[str]:
        """
        Generalized method to upload a file (from discord.Attachment or raw bytes)
        to a specified Supabase storage bucket.

        Args:
            file_source: A discord.Attachment object or raw bytes of the file.
            bucket_name: The target Supabase storage bucket (e.g., "workflows", "videos").
            storage_path: The desired path (including filename) within the bucket.
            content_type: The MIME type of the file. If None and file_source is
                          discord.Attachment, it's inferred from the attachment.
                          Defaults to 'application/octet-stream' if not determinable.
            upsert: Whether to overwrite the file if it already exists.

        Returns:
            The public URL of the uploaded file if successful, otherwise None.
        """
        if not self.supabase:
            self.logger.error(f"[OpenMuseInteractor_UploadFile] Supabase client not initialized. Cannot upload.")
            return None

        file_bytes: Optional[bytes] = None
        final_content_type: str

        if isinstance(file_source, discord.Attachment):
            self.logger.info(f"[OpenMuseInteractor_UploadFile] Reading bytes from discord.Attachment '{file_source.filename}'.")
            try:
                file_bytes = await file_source.read()
                final_content_type = content_type or file_source.content_type or 'application/octet-stream'
                self.logger.info(f"[OpenMuseInteractor_UploadFile] Read {len(file_bytes)} bytes. Determined content type: {final_content_type}.")
            except discord.HTTPException as e:
                self.logger.error(f"[OpenMuseInteractor_UploadFile] Discord HTTP error reading attachment {file_source.filename}: {e}")
                return None
            except Exception as e:
                self.logger.error(f"[OpenMuseInteractor_UploadFile] Error reading attachment {file_source.filename}: {e}", exc_info=True)
                return None
        elif isinstance(file_source, bytes):
            file_bytes = file_source
            final_content_type = content_type or 'application/octet-stream'
            self.logger.info(f"[OpenMuseInteractor_UploadFile] Using provided {len(file_bytes)} bytes. Content type: {final_content_type}.")
        else:
            self.logger.error(f"[OpenMuseInteractor_UploadFile] Invalid file_source type: {type(file_source)}. Must be discord.Attachment or bytes.")
            return None

        if not file_bytes: # Should be caught above, but as a safeguard
            self.logger.error(f"[OpenMuseInteractor_UploadFile] File bytes are empty. Cannot upload.")
            return None

        return await self._upload_bytes_to_storage(
            file_bytes=file_bytes,
            bucket_name=bucket_name,
            storage_path=storage_path,
            content_type=final_content_type,
            upsert=upsert
        )

    async def upload_discord_attachment(
        self,
        attachment: discord.Attachment,
        author: discord.User | discord.Member,
        message: discord.Message,
        admin_status: str = 'Listed'
        # Optional: Pass reaction if needed for metadata, though probably not
        # reaction: discord.Reaction | None = None
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Handles uploading a discord attachment to Supabase storage,
        generating thumbnails for videos, and creating a media record.

        Args:
            attachment: The discord.Attachment object to upload.
            author: The discord.User or discord.Member who authored the message.
            message: The original discord.Message containing the attachment.
            admin_status: The admin_status for the media record

        Returns:
            A tuple containing (media_record, profile_record) upon successful
            upload and database insertion, otherwise (None, None).
            Returns potentially stale profile_record even on media insert failure if profile was found/created.
        """
        self.logger.info(f"[OpenMuseInteractor] Initiating upload for attachment '{attachment.filename}' from message {message.id} by author {author.id}.")

        if not self.supabase:
            self.logger.error("[OpenMuseInteractor] Supabase client not initialized. Cannot upload attachment.")
            return None, None

        # --- 1. Find or Create Author Profile --- Find MUST happen first
        profile_data, member_id = await self.find_or_create_profile(author)

        if not profile_data or not member_id:
            self.logger.error(f"[OpenMuseInteractor] Failed to find or create profile for author {author.id}. Aborting upload for message {message.id}.")
            return None, None

        # --- 2. Check File Size --- Done after profile check
        if attachment.size > MAX_FILE_SIZE_BYTES:
            self.logger.warning(f"[OpenMuseInteractor] Attachment '{attachment.filename}' ({attachment.size} bytes) from message {message.id} exceeds max size ({MAX_FILE_SIZE_BYTES} bytes). Aborting upload.")
            return None, _truncate_avatar_in_dict_for_logging(profile_data) # Return profile data even if upload fails

        # --- 3. Download File Content --- Download only if size is okay
        try:
            self.logger.info(f"[OpenMuseInteractor] --> Reading attachment '{attachment.filename}' bytes from message {message.id}.")
            file_bytes = await attachment.read()
            self.logger.info(f"[OpenMuseInteractor] <-- Read {len(file_bytes)} bytes for attachment '{attachment.filename}'.")
        except discord.HTTPException as e:
            self.logger.error(f"[OpenMuseInteractor] Discord HTTP error reading attachment {attachment.filename} from message {message.id}: {e}")
            return None, _truncate_avatar_in_dict_for_logging(profile_data)
        except Exception as e:
            self.logger.error(f"[OpenMuseInteractor] Error reading attachment {attachment.filename} from message {message.id}: {e}", exc_info=True)
            return None, _truncate_avatar_in_dict_for_logging(profile_data)

        # --- 4. Process Video Thumbnail & Aspect Ratio (if applicable) ---
        content_type = attachment.content_type or 'application/octet-stream'
        placeholder_image_url = None
        calculated_aspect_ratio = None
        _thumbnail_upload_success = False # Required only if it's a video

        if content_type.startswith('video/'):
            self.logger.info(f"[OpenMuseInteractor] Attachment '{attachment.filename}' is video ({content_type}). Processing thumbnail/ratio.")
            temp_video_file = None
            cap = None
            frame = None
            try:
                # Write video bytes to a temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(attachment.filename)[1]) as temp_video_file:
                    temp_video_file.write(file_bytes)
                    temp_video_path = temp_video_file.name
                self.logger.info(f"[OpenMuseInteractor] Video bytes written to temporary file: {temp_video_path}")

                cap = cv2.VideoCapture(temp_video_path)
                if not cap.isOpened():
                    self.logger.error(f"[OpenMuseInteractor] OpenCV could not open temporary video file: {temp_video_path}")
                else:
                    ret, frame = cap.read()
                    if ret:
                        self.logger.info(f"[OpenMuseInteractor] Successfully read first frame from video using OpenCV.")
                        # Calculate Aspect Ratio
                        try:
                            h, w = frame.shape[:2]
                            if h > 0:
                                calculated_aspect_ratio = round(w / h, 2)
                                self.logger.info(f"[OpenMuseInteractor] OpenCV Calculated aspect ratio: {calculated_aspect_ratio} (w={w}, h={h})")
                            else:
                                self.logger.warning("[OpenMuseInteractor] Frame height is 0, cannot calculate aspect ratio.")
                        except Exception as ar_ex:
                            self.logger.error(f"[OpenMuseInteractor] Error calculating aspect ratio from frame shape: {ar_ex}")

                        # Encode frame as JPEG
                        is_success, buffer = cv2.imencode(".jpg", frame)
                        if is_success:
                            thumbnail_bytes = buffer.tobytes()
                            self.logger.info(f"[OpenMuseInteractor] Encoded frame to {len(thumbnail_bytes)} bytes (JPEG).")

                            thumbnail_filename = f"{os.path.splitext(attachment.filename)[0]}_thumb.jpg"
                            thumbnail_storage_path = f"user_media/{member_id}/{message.id}_{thumbnail_filename}"
                            
                            placeholder_image_url = await self._upload_bytes_to_storage(
                                file_bytes=thumbnail_bytes,
                                bucket_name=THUMBNAIL_BUCKET_NAME,
                                storage_path=thumbnail_storage_path,
                                content_type="image/jpeg",
                                upsert=True # Typically true for thumbnails derived from same source
                            )

                            if placeholder_image_url:
                                self.logger.info(f"[OpenMuseInteractor] Thumbnail uploaded successfully. URL: {placeholder_image_url}")
                                _thumbnail_upload_success = True # Mark as success if URL is obtained
                            else:
                                self.logger.error(f"[OpenMuseInteractor] Thumbnail upload failed for '{thumbnail_storage_path}' (no URL returned or error in _upload_bytes_to_storage).")
                                # thumbnail_upload_success remains False
                        else:
                            self.logger.error("[OpenMuseInteractor] Failed to encode video frame to JPEG using OpenCV.")
                    else:
                        self.logger.error("[OpenMuseInteractor] Failed to read first frame from video using OpenCV.")
            except Exception as thumb_ex:
                 self.logger.error(f"[OpenMuseInteractor] Error during thumbnail/ratio processing (OpenCV): {thumb_ex}", exc_info=True)
            finally:
                if cap and cap.isOpened():
                    cap.release()
                if temp_video_file and os.path.exists(temp_video_path):
                    try:
                        os.remove(temp_video_path)
                    except OSError as e:
                        self.logger.error(f"[OpenMuseInteractor] Error removing temporary file {temp_video_path}: {e}")
        else:
            self.logger.info(f"[OpenMuseInteractor] Attachment '{attachment.filename}' is not video. Skipping thumbnail generation.")

        # --- 5. Upload Original File --- Always attempt this
        storage_path = f"user_media/{member_id}/{message.id}_{attachment.filename}"
        self.logger.info(f"[OpenMuseInteractor] --> Attempting to upload original file '{attachment.filename}' to bucket '{VIDEO_BUCKET_NAME}' at path '{storage_path}'.")
        
        public_url = await self._upload_bytes_to_storage(
            file_bytes=file_bytes, # These are the bytes of the original attachment read earlier
            bucket_name=VIDEO_BUCKET_NAME, # Or more generic like 'media_files_bucket'
            storage_path=storage_path,
            content_type=content_type, # Original content type of the attachment
            upsert=True # Standard upsert policy
        )

        if not public_url:
            self.logger.error(f"[OpenMuseInteractor] Original file upload failed for '{storage_path}'. Aborting media record creation.")
            return None, _truncate_avatar_in_dict_for_logging(profile_data)
        
        self.logger.info(f"[OpenMuseInteractor] Original file uploaded successfully. URL: {public_url}")
        main_upload_success = True # If public_url is not None, it was successful.

        # --- 6. Get Original File Public URL (if upload succeeded) ---
        # This step is now integrated into _upload_bytes_to_storage and its return value (public_url)
        # Validation of the URL also happens within _upload_bytes_to_storage.
        # So, no separate "Get Original File Public URL" section needed here.
        
        # main_upload_success is true if public_url is not None
        if not main_upload_success: # Should be redundant if logic above is correct
             self.logger.error("[OpenMuseInteractor] Reached code after main upload failure - should not happen.")
             return None, _truncate_avatar_in_dict_for_logging(profile_data)

        # Trim and validate thumbnail URL if present (already handled by _upload_bytes_to_storage for placeholder_image_url)
        # The placeholder_image_url is already validated or None if it failed.

        # --- 7. Insert Media Record --- Requires successful profile step
        classification = 'art' if message.channel and hasattr(message.channel, 'name') and message.channel.name.lower().startswith('art') else 'gen'
        media_type = 'video' if content_type.startswith('video/') else content_type # Simplified type
        media_title = None if media_type == 'video' else attachment.filename

        media_data = {
            'member_id': int(member_id),
            'title': media_title,
            'url': public_url,
            'placeholder_image': placeholder_image_url,
            'type': media_type,
            'classification': classification,
            'admin_status': admin_status,
            'user_status': 'Listed', # Changed from 'View'
            'description': message.content,
            'metadata': {
                "discord_message_id": str(message.id),
                "discord_channel_id": str(message.channel.id),
                "discord_guild_id": str(message.guild.id) if message.guild else None,
                "discord_attachment_url": attachment.url,
                # "reacted_by_discord_user_id": str(reaction.user.id) if reaction else None, # Removed - belongs in Reactor
                # "trigger_emoji": str(reaction.emoji) if reaction else None, # Removed - belongs in Reactor
                "aspectRatio": calculated_aspect_ratio,
                "original_filename": attachment.filename,
                "original_content_type": content_type
            }
        }

        try:
            self.logger.info(f"[OpenMuseInteractor] --> Attempting to insert record into Supabase table '{MEDIA_TABLE}'.")
            insert_response = await asyncio.to_thread(
                 self.supabase.table(MEDIA_TABLE).insert(media_data).execute
            )
            self.logger.debug(f"[OpenMuseInteractor] Media insert response: {_truncate_avatar_in_dict_for_logging(insert_response)}")

            if insert_response.data:
                inserted_media_record = insert_response.data[0]
                self.logger.info(f"[OpenMuseInteractor] <-- Successfully inserted media record into table '{MEDIA_TABLE}'. Media ID: {inserted_media_record.get('id')}")

                # --- 8. Handle Welcome DM logic / discord_connected update ---
                await self._mark_profile_connected_and_send_welcome_dm(
                    member_id=member_id,
                    author=author,
                    profile_data=profile_data,
                )

                # Return successful media record and the (potentially updated) profile data
                return inserted_media_record, _truncate_avatar_in_dict_for_logging(profile_data)
            else:
                self.logger.error(f"[OpenMuseInteractor] Supabase media insert for message {message.id} executed but returned no data. Insert failed? RLS?",
                                 extra={"response": _truncate_avatar_in_dict_for_logging(insert_response)}), _truncate_avatar_in_dict_for_logging(profile_data) # Log full response if possible
                return None, _truncate_avatar_in_dict_for_logging(profile_data) # Return profile, but indicate media insert failure

        except APIError as insert_err:
             self.logger.error(f"[OpenMuseInteractor] Supabase API error inserting media record for message {message.id}: {insert_err}")
             return None, _truncate_avatar_in_dict_for_logging(profile_data)
        except Exception as insert_ex:
            self.logger.error(f"[OpenMuseInteractor] Unexpected error inserting media record for message {message.id}: {insert_ex}", exc_info=True)
            return None, _truncate_avatar_in_dict_for_logging(profile_data)

    async def create_media_record(
        self,
        member_id: str,
        media_url: str,
        filename: str, 
        content_type: str,
        file_size: int, 
        description: Optional[str], 
        message: discord.Message, 
        author_discord_user: discord.User, 
        profile_data: Dict[str, Any], 
        admin_status: str = "Listed",
        user_status: str = "Listed",
        title: Optional[str] = None,
        placeholder_image_url: Optional[str] = None,
        calculated_aspect_ratio: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Creates a media record in the Supabase 'media' table.
        This is a more direct way to insert a media record when the file is already uploaded
        or when detailed processing like thumbnailing is handled externally or skipped.
        Args:
            member_id: The Discord member ID string for the canonical user.
            media_url: The public URL of the uploaded media.
            filename: The filename of the media (e.g., original_filename or new .mp4 filename).
            content_type: The MIME type of the media.
            file_size: The size of the media file in bytes.
            description: Optional description for the media (e.g., from the Discord message content).
            message: The discord.Message object for additional metadata (channel, guild ID).
            author_discord_user: The discord.User who created the media, for welcome DM.
            profile_data: The profile data dictionary for the author, used for welcome DM logic.
            admin_status: Admin status for the media record.
            user_status: User status for the media record.
            title: Optional title for the media.
            placeholder_image_url: Optional URL for a placeholder/thumbnail image.
            calculated_aspect_ratio: Optional aspect ratio of the media.
        Returns:
            A dictionary representing the inserted media record if successful, otherwise None.
        """
        if not self.supabase:
            self.logger.error("[OpenMuseInteractor_CreateMedia] Supabase client not initialized. Cannot create media record.")
            return None

        self.logger.info(f"[OpenMuseInteractor_CreateMedia] Creating media record for URL: {media_url}, Filename: {filename}")

        classification = 'art' if message.channel and hasattr(message.channel, 'name') and message.channel.name.lower().startswith('art') else 'gen'
        media_type = 'video' if content_type.startswith('video/') else content_type 
        
        if title is None and media_type != 'video': 
            title = filename

        media_data_payload = {
            'member_id': int(member_id),
            'title': title,
            'url': media_url,
            'placeholder_image': placeholder_image_url,
            'type': media_type,
            'classification': classification,
            'admin_status': admin_status,
            'user_status': user_status,
            'description': description,
            'metadata': {
                "discord_message_id": str(message.id),
                "discord_channel_id": str(message.channel.id),
                "discord_guild_id": str(message.guild.id) if message.guild else None,
                "aspectRatio": calculated_aspect_ratio,
                "original_filename": filename, 
                "original_content_type": content_type,
                "file_size": file_size 
            }
        }
        # Remove None keys from metadata before insert if Supabase prefers that
        media_data_payload['metadata'] = {k: v for k, v in media_data_payload['metadata'].items() if v is not None}


        try:
            self.logger.info(f"[OpenMuseInteractor_CreateMedia] --> Attempting to insert record into Supabase table '{MEDIA_TABLE}' with payload: {_truncate_avatar_in_dict_for_logging(media_data_payload)}")
            insert_response = await asyncio.to_thread(
                 self.supabase.table(MEDIA_TABLE).insert(media_data_payload).execute
            )
            self.logger.debug(f"[OpenMuseInteractor_CreateMedia] Media insert response: {_truncate_avatar_in_dict_for_logging(insert_response)}")

            if insert_response.data:
                inserted_media_record = insert_response.data[0]
                self.logger.info(f"[OpenMuseInteractor_CreateMedia] <-- Successfully inserted media record. Media ID: {inserted_media_record.get('id')}")

                await self._mark_profile_connected_and_send_welcome_dm(
                    member_id=member_id,
                    author=author_discord_user,
                    profile_data=profile_data,
                )

                return inserted_media_record
            else:
                self.logger.error(f"[OpenMuseInteractor_CreateMedia] Supabase media insert executed but returned no data. Insert failed? RLS?",
                                 extra={"response": _truncate_avatar_in_dict_for_logging(insert_response)})
                return None
        except APIError as insert_err:
             self.logger.error(f"[OpenMuseInteractor_CreateMedia] Supabase API error inserting media record: {insert_err}")
             return None
        except Exception as insert_ex:
            self.logger.error(f"[OpenMuseInteractor_CreateMedia] Unexpected error inserting media record: {insert_ex}", exc_info=True)
            return None

    # --- Add other Supabase interaction methods here as needed ---
    # Example: async def get_media_by_id(self, media_id: str): ...
    # Example: async def update_media_status(self, media_id: str, status: str): ...
