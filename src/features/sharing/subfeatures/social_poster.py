# Placeholder for social_poster functions 

import tweepy
import os
import asyncio
import logging
import cv2
import shutil
import base64
from typing import Dict, Optional, List
from pathlib import Path

from src.common.llm import get_llm_response

logger = logging.getLogger('DiscordBot')

# --- Environment Variable Check ---
# Check for keys at import time to fail fast
CONSUMER_KEY = os.getenv("TWITTER_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("TWITTER_CONSUMER_SECRET")
ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
_cached_screen_name: Optional[str] = None

if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
    logger.critical("Twitter API credentials missing in environment variables!")
    # raise ValueError("Missing Twitter API credentials") # Consider uncommenting

# --- Helper Functions ---

def _truncate_with_ellipsis(text: str, max_length: int) -> str:
    """Truncates text to max_length, adding ellipsis if needed."""
    if len(text) <= max_length:
        return text
    else:
        # Adjust length to account for ellipsis and potential space
        return text[:max_length-4] + "..."

def _get_cached_screen_name(api_v1: tweepy.API) -> str:
    """Fetches and caches the authenticated bot screen name for tweet URLs."""
    global _cached_screen_name

    if _cached_screen_name:
        return _cached_screen_name

    try:
        credentials = api_v1.verify_credentials()
        screen_name = getattr(credentials, 'screen_name', None)
        if screen_name:
            _cached_screen_name = screen_name
            return screen_name
        logger.warning("Falling back to placeholder tweet URL username because verify_credentials returned no screen_name.")
    except Exception as e:
        logger.warning(f"Falling back to placeholder tweet URL username after screen-name lookup failed: {e}")

    return "user"

def _build_tweet_caption(base_description: str, user_details: Dict, original_content: Optional[str]) -> str:
    """Builds the final tweet caption, prioritizing base_description and then adding credit."""
    
    user_global_name = user_details.get('global_name')
    user_discord_name = user_details.get('username')
    raw_twitter_handle = user_details.get('twitter_url')
    artist_credit_text = None

    if raw_twitter_handle:
        handle_val = raw_twitter_handle.strip()
        extracted_username = None
        
        # If it's already an @ handle, just use it
        if handle_val.startswith('@'):
            extracted_username = handle_val[1:]  # Remove the @ for processing
        # If it's a URL, extract the username from the end
        elif any(domain in handle_val.lower() for domain in ['twitter.com/', 'x.com/', '://']):
            # Handle full URLs
            if '://' in handle_val:
                path_after_scheme = handle_val.split('://', 1)[-1]
            else:
                path_after_scheme = handle_val
                
            # Extract username from various URL patterns
            path_lower = path_after_scheme.lower()
            if 'twitter.com/' in path_lower:
                start_idx = path_lower.find('twitter.com/') + len('twitter.com/')
                extracted_username = path_after_scheme[start_idx:].split('/')[0]
            elif 'x.com/' in path_lower:
                start_idx = path_lower.find('x.com/') + len('x.com/')
                extracted_username = path_after_scheme[start_idx:].split('/')[0]
        else:
            # Plain username without @ - just use it
            extracted_username = handle_val
            
        if extracted_username:
            # Clean up the username (remove query params, fragments, extra @)
            cleaned_username = extracted_username.split('?')[0].split('#')[0].strip()
            if cleaned_username.startswith('@'):
                cleaned_username = cleaned_username[1:]
            if cleaned_username:
                artist_credit_text = f"@{cleaned_username}"

    if not artist_credit_text: 
        if user_global_name:
            artist_credit_text = user_global_name
        elif user_discord_name:
            artist_credit_text = user_discord_name
        else:
            artist_credit_text = "the artist" # Fallback if no names found
    
    was_base_description_provided = base_description and base_description.strip()

    if was_base_description_provided:
        final_caption = base_description.strip() # Use the provided base_description directly
    else:
        # If no base_description, construct the default "Top art post..." and add artist comment
        final_caption = f"Top art post of the day by {artist_credit_text}"

        if original_content and original_content.strip():
            comment_to_add = original_content.strip()
            # Using a simpler format for the artist comment part from the default flow
            # to distinguish from the potentially more custom format passed in base_description.
            # Max length for tweet is 280. Ellipsis is 3 chars. "\n\nArtist's Comment: \"...\"" is ~24 chars.
            # So, caption + comment_structure should be less than 280.
            # Available for comment_to_add = 280 - len(final_caption) - 24
            comment_format_overhead = len("\n\nArtist's Comment: \"\"") 
            full_comment_section = f"\n\nArtist's Comment: \"{comment_to_add}\""

            if len(final_caption) + len(full_comment_section) <= 280:
                final_caption += full_comment_section
            else:
                available_len_for_comment_text = 280 - len(final_caption) - comment_format_overhead
                if available_len_for_comment_text > 10: # Ensure some space for a meaningful truncated comment
                    truncated_comment = _truncate_with_ellipsis(comment_to_add, available_len_for_comment_text)
                    final_caption += f"\n\nArtist's Comment: \"{truncated_comment}\""
                else:
                     # Not enough space to add even a truncated comment meaningfully.
                     logger.warning(f"Caption (default flow) for '{artist_credit_text}' too long to add artist comment: '{comment_to_add[:30]}...'")
            
    # Final length check and truncation if necessary (applies to both cases)
    if len(final_caption) > 280:
        logger.warning(f"Final constructed caption exceeds 280 chars. Truncating. Original: '{final_caption}'")
        # A more generic truncation if it still exceeds, regardless of how it was formed.
        final_caption = _truncate_with_ellipsis(final_caption, 280)

    return final_caption.strip()

# --- Added Title Generation Helpers ---

def _image_to_base64(image_path: str) -> Optional[str]:
    """Converts an image file to a base64 encoded string."""
    try:
        with open(image_path, 'rb') as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error encoding image {image_path} to base64: {e}", exc_info=True)
        return None

def _extract_frames(video_path: str, num_frames: int, save_dir: Path) -> bool:
    """Extracts a specified number of evenly distributed frames from a video."""
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        vidcap = cv2.VideoCapture(video_path)
        if not vidcap.isOpened():
            logger.error(f"Failed to open video file: {video_path}")
            return False

        total_frames = int(vidcap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames < 1:
            logger.warning(f"Video {video_path} has no frames.")
            vidcap.release()
            return False
        
        # Ensure num_frames is not greater than total_frames
        num_frames = min(num_frames, total_frames)
        if num_frames < 1:
            logger.warning(f"Cannot extract less than 1 frame from {video_path}.")
            vidcap.release()
            return False
            
        # Calculate interval, ensuring it's at least 1 frame
        frames_interval = max(1, total_frames // num_frames)
        
        extracted_count = 0
        for i in range(num_frames):
            frame_id = i * frames_interval
            vidcap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
            success, image = vidcap.read()
            if success:
                save_path = save_dir / f"frame_{extracted_count}.jpg"
                cv2.imwrite(str(save_path), image)
                extracted_count += 1
            else:
                logger.warning(f"Failed to read frame {frame_id} from {video_path}")
                # Optionally break if frame read fails

        vidcap.release()
        logger.info(f"Extracted {extracted_count} frames from {video_path} to {save_dir}")
        return extracted_count > 0
    except Exception as e:
        logger.error(f"Error extracting frames from {video_path}: {e}", exc_info=True)
        if vidcap.isOpened(): vidcap.release() # Ensure release on error
        return False

# --- Claude Interaction (Video Frames) --- REFACTORED ---
# Make the function async
async def _make_claude_title_request(frames_dir: Path,
                                     original_comment: Optional[str]) -> Optional[str]:
    """Makes a request via LLM dispatcher to generate a title from video frames."""
    try:
        # Prepare ≤ 5 JPEG frames (already extracted elsewhere) as base64
        image_paths = list(frames_dir.glob("*.jpg"))[:5]
        content_blocks = []
        for image_path in image_paths:
            base64_image = _image_to_base64(str(image_path))
            if base64_image:
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64_image
                    }
                })

        if not any(item['type'] == 'image' for item in content_blocks):
            logger.warning(f"No valid image frames found in {frames_dir} to send via dispatcher.")
            return None

        # Define System Prompt
        system_prompt = (
            "Analyze these video frames. Create a short, interesting, unique "
            "title (max 3-4 words). Avoid clichés. If the user comment suggests "
            "a title, prioritize that. Output ONLY the title."
        )
        
        # Define User Prompt Text Block
        user_prompt_text = "Generate a title for the attached video frames."
        if original_comment and original_comment.strip():
             user_prompt_text += f'\n\nArtist\'s comment: "{original_comment}"'

        # Add text block *first* for dispatcher format
        content_blocks.insert(0, {"type": "text", "text": user_prompt_text})
        
        # Prepare messages list for dispatcher
        messages_for_dispatcher = [{"role": "user", "content": content_blocks}]

        # ---- Call LLM Dispatcher ----
        logger.info("Requesting video title via dispatcher (Claude Sonnet)...")
        generated_title = await get_llm_response(
            client_name="claude",
            model="claude-sonnet-4-5-20250929",
            system_prompt=system_prompt,
            messages=messages_for_dispatcher,
            max_tokens=50,
            temperature=0.5 # Pass other kwargs if needed
        )

        # Dispatcher should return string or raise error
        if generated_title and isinstance(generated_title, str):
            cleaned_title = generated_title.strip().strip('\"\'')
            logger.info(f"Dispatcher generated title from video frames: {cleaned_title}")
            return cleaned_title
        else:
            # This case might indicate an issue with dispatcher response handling if reached
            logger.warning(f"Dispatcher returned unexpected response for video title: {generated_title}")
            return None

    except Exception as e:
        # Catch errors from dispatcher or content preparation
        logger.error(f"Error generating title from video frames via dispatcher: {e}", exc_info=True)
        return None

# --- Main Title Generation Function --- REFACTORED ---
async def generate_media_title(attachment: Dict, original_comment: Optional[str], post_id: int) -> str:
    """
    Generates a social media title using the LLM Dispatcher (ClaudeClient).
    For videos: Extracts frames, prepares content, calls dispatcher.
    For images: Prepares base64 image content, calls dispatcher.
    """
    media_local_path = attachment.get('local_path')
    content_type = attachment.get('content_type', '')
    title = "Featured Artwork"  # Default title
    temp_frames_dir = None

    if not media_local_path or not os.path.exists(media_local_path):
        logger.error(f"Media file not found for title generation: {media_local_path}, Post ID: {post_id}")
        return title

    is_video = content_type.startswith('video') or Path(media_local_path).suffix.lower() in ['.mp4', '.mov', '.webm', '.avi', '.mkv']
    is_image = content_type.startswith('image') # Includes gifs

    try:
        generated_title_text = None

        if is_video:
            logger.info(f"Generating title for video via dispatcher: {media_local_path} (Post ID: {post_id})")
            temp_frames_dir = Path(f"./temp_title_frames_{post_id}_{os.urandom(4).hex()}")
            if _extract_frames(video_path=media_local_path, num_frames=5, save_dir=temp_frames_dir):
                # Call the refactored async helper directly
                generated_title_text = await _make_claude_title_request(temp_frames_dir, original_comment)
            else:
                logger.warning(f"Frame extraction failed for {media_local_path}. Cannot generate title from video.")

        elif is_image:
            logger.info(f"Generating title for image via dispatcher: {media_local_path} (Post ID: {post_id})")
            base64_image = _image_to_base64(media_local_path)
            if base64_image:
                 # Determine mime type
                 mime_type = content_type if content_type.startswith('image/') else 'image/jpeg'
                 suffix = Path(media_local_path).suffix.lower()
                 if suffix == '.gif': mime_type = 'image/gif'
                 elif suffix == '.png': mime_type = 'image/png'
                 elif suffix == '.webp': mime_type = 'image/webp'
                 
                 # Prepare content blocks for dispatcher
                 content_blocks = [
                     { # Text block first
                        "type": "text",
                         "text": (
                             f"Generate a title for the attached image." +
                             (f'\n\nArtist\'s comment: "{original_comment}"' if original_comment and original_comment.strip() else "")
                         )
                     },
                     { # Image block
                        "type": "image",
                         "source": {
                             "type": "base64",
                             "media_type": mime_type,
                             "data": base64_image
                         }
                     }
                 ]
                 
                 # System prompt for images
                 system_prompt = (
                     "Analyze this image. Create a short, interesting, unique "
                     "title (max 3-4 words). Avoid clichés. If the user comment suggests "
                     "a title, prioritize that. Output ONLY the title."
                 )

                 # Prepare messages list for dispatcher
                 messages_for_dispatcher = [{"role": "user", "content": content_blocks}]

                 # ---- Call LLM Dispatcher ----
                 logger.info("Requesting image title via dispatcher (Claude Sonnet)...")
                 llm_response = await get_llm_response(
                     client_name="claude",
                     model="claude-sonnet-4-5-20250929",
                     system_prompt=system_prompt,
                     messages=messages_for_dispatcher,
                     max_tokens=50,
                     temperature=0.5
                 )
                 
                 if llm_response and isinstance(llm_response, str):
                     generated_title_text = llm_response.strip().strip('\"\'')
                     logger.info(f"Dispatcher generated title from image: {generated_title_text}")
                 else:
                     logger.warning(f"Dispatcher returned unexpected response for image title: {llm_response}")

            else:
                logger.warning(f"Could not encode image {media_local_path} to base64. Cannot generate title.")
        else:
            logger.warning(f"Unsupported media type for title generation: {content_type}, Post ID: {post_id}. Using default title.")

        # Update title if generation was successful
        if generated_title_text:
            title = generated_title_text

    except Exception as e:
        logger.error(f"Error in generate_media_title (dispatcher path) for Post ID {post_id}: {e}", exc_info=True)
        # Fallback to default title is handled by initial assignment

    finally:
        # Clean up temporary frame directory if it was created
        if temp_frames_dir and temp_frames_dir.exists():
            try:
                shutil.rmtree(temp_frames_dir)
                logger.debug(f"Cleaned up temp frame directory: {temp_frames_dir}")
            except Exception as e_clean:
                logger.error(f"Error cleaning up temp frame directory {temp_frames_dir}: {e_clean}", exc_info=True)

    return title

# --- Main Posting Function ---

async def post_tweet(
    generated_description: str,
    user_details: Dict,
    attachments: Optional[List[Dict]],
    original_content: Optional[str],
    in_reply_to_tweet_id: Optional[str] = None,
    quote_tweet_id: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """Uploads media and posts a tweet with a generated caption.
    
    Returns:
        Dict with 'url' and 'id' keys if successful, None if failed
    """
    
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
         logger.error("Cannot post tweet, API credentials missing.")
         return None

    attachments = attachments or []

    # Build the final caption
    final_caption = _build_tweet_caption(generated_description, user_details, original_content)
    logger.info(f"Final Tweet Caption: {final_caption}") # Log the caption being used
    if in_reply_to_tweet_id:
        logger.info(f"Posting tweet as reply to tweet ID: {in_reply_to_tweet_id}")

    try:
        # --- Media Upload (v1.1 API) ---
        auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
        auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
        api_v1 = tweepy.API(auth)
        
        loop = asyncio.get_event_loop()
        media_id = None

        if attachments:
            # Assume the first attachment is the primary one to post
            # TODO: Handle multiple attachments if Twitter API allows/needed
            attachment = attachments[0]
            media_path = attachment.get('local_path')
            durable_url = attachment.get('durable_url')
            
            # ── durable_url fallback: download to temp file when local_path absent ──
            if (not media_path or not os.path.exists(media_path)) and durable_url:
                logger.info(
                    "post_tweet: local_path missing for %s, downloading from durable_url: %s",
                    attachment.get('filename', 'unknown'), durable_url[:80],
                )
                try:
                    import tempfile as _tempfile
                    from src.features.sharing.live_update_social.helpers import download_media_url
                    
                    temp_dir = _tempfile.mkdtemp(prefix="tweet_media_")
                    downloaded = await download_media_url(
                        url=durable_url,
                        dest_dir=temp_dir,
                        filename_prefix="tweet",
                    )
                    if downloaded and downloaded.get("local_path") and os.path.exists(downloaded["local_path"]):
                        media_path = downloaded["local_path"]
                        attachment["local_path"] = media_path
                        logger.info("post_tweet: downloaded durable_url → %s", media_path)
                    else:
                        logger.error("post_tweet: durable_url download failed for %s", durable_url[:80])
                        return None
                except Exception as _dl_err:
                    logger.error("post_tweet: durable_url download error: %s", _dl_err, exc_info=True)
                    return None
            
            if not media_path or not os.path.exists(media_path):
                logger.error(f"Cannot post tweet, media file path invalid or file missing: {media_path}")
                return None

            filename = attachment.get('filename', Path(media_path).name)
            file_extension = Path(filename).suffix.lower()

            logger.info(f"Uploading media ({filename}) to Twitter...")
            if file_extension == '.gif':
                # GIFs need chunked upload and specific media category
                media = await loop.run_in_executor(None,
                    lambda: api_v1.media_upload(media_path, chunked=True, media_category="tweet_gif")
                )
            else:
                 # Other types (images/videos) - use standard upload (chunked is good practice for videos)
                 # Tweepy v1's media_upload handles chunking automatically if file is large enough
                 media = await loop.run_in_executor(None,
                     lambda: api_v1.media_upload(media_path, chunked=True)
                 )

            media_id = media.media_id_string
            logger.info(f"Twitter Media Upload successful. Media ID: {media_id}")
        else:
            logger.info("Posting text-only tweet without media attachments.")

        # --- Create Tweet (v2 API) ---
        client_v2 = tweepy.Client(
            consumer_key=CONSUMER_KEY,
            consumer_secret=CONSUMER_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
        )
        
        logger.info("Creating tweet...")
        create_tweet_kwargs = {
            'text': final_caption,
            'in_reply_to_tweet_id': in_reply_to_tweet_id,
            'quote_tweet_id': quote_tweet_id,
        }
        if media_id:
            create_tweet_kwargs['media_ids'] = [media_id]
        tweet = await loop.run_in_executor(None,
             lambda: client_v2.create_tweet(**create_tweet_kwargs)
        )

        tweet_id = tweet.data['id']
        screen_name = _get_cached_screen_name(api_v1)
        tweet_url = f"https://twitter.com/{screen_name}/status/{tweet_id}"
        
        logger.info(f"Tweet posted successfully: {tweet_url}")
        return {'url': tweet_url, 'id': tweet_id, 'media_id': media_id}

    except tweepy.errors.TweepyException as e:
        logger.error(f"Twitter API error during posting: {e}", exc_info=True)
        # Specific error handling can be added here (e.g., rate limits, media processing errors)
        if "duplicate content" in str(e).lower():
             logger.warning("Tweet failed due to duplicate content.")
             # Decide if you want to return a specific marker or None
        return None
    except Exception as e:
        logger.error(f"Unexpected error during Twitter posting: {e}", exc_info=True)
        return None 

# --- Delete Tweet Function ---

async def delete_tweet(tweet_id: str) -> bool:
    """Deletes a tweet by its ID using the Twitter v2 API.
    
    Args:
        tweet_id: The ID of the tweet to delete
        
    Returns:
        True if deletion was successful, False otherwise
    """
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        logger.error("Cannot delete tweet, API credentials missing.")
        return False
    
    if not tweet_id:
        logger.error("Cannot delete tweet, no tweet_id provided.")
        return False
    
    try:
        client_v2 = tweepy.Client(
            consumer_key=CONSUMER_KEY,
            consumer_secret=CONSUMER_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
        )
        
        loop = asyncio.get_event_loop()
        logger.info(f"Attempting to delete tweet {tweet_id}...")
        
        result = await loop.run_in_executor(None,
            lambda: client_v2.delete_tweet(tweet_id)
        )
        
        # Check if deletion was successful
        if result and result.data and result.data.get('deleted'):
            logger.info(f"Successfully deleted tweet {tweet_id}")
            return True
        else:
            logger.warning(f"Tweet deletion response unclear for {tweet_id}: {result}")
            return False
            
    except tweepy.errors.NotFound:
        logger.warning(f"Tweet {tweet_id} not found (may have already been deleted)")
        return True  # Consider this a success since the tweet is gone
    except tweepy.errors.Forbidden as e:
        logger.error(f"Forbidden to delete tweet {tweet_id}: {e}")
        return False
    except tweepy.errors.TweepyException as e:
        logger.error(f"Twitter API error during tweet deletion: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error deleting tweet {tweet_id}: {e}", exc_info=True)
        return False
