from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

import tweepy

from ..models import SocialPublishRequest
from ..subfeatures.social_poster import (
    ACCESS_TOKEN,
    ACCESS_TOKEN_SECRET,
    CONSUMER_KEY,
    CONSUMER_SECRET,
    delete_tweet,
    post_tweet,
)
from . import SocialPublishProvider

logger = logging.getLogger('DiscordBot')

STATUS_URL_RE = re.compile(r'(?:twitter|x)\.com/.+/status/(\d+)', re.IGNORECASE)


class XProvider(SocialPublishProvider):
    """X/Twitter provider implementation."""

    async def publish(self, request: SocialPublishRequest) -> Optional[Dict[str, Any]]:
        metadata = self._request_metadata(request)
        user_details = metadata.get('user_details')
        if not user_details:
            logger.error("[XProvider] user_details are required to publish to X")
            return None

        target_ref = self.normalize_target_ref(request.target_post_ref)

        if request.action == 'retweet':
            if not target_ref:
                logger.error("[XProvider] Retweet requested without target_post_ref")
                return None
            return await self._retweet(target_ref)

        if request.action in ('reply', 'quote') and not target_ref:
            logger.error(f"[XProvider] {request.action} requested without target_post_ref")
            return None

        tweet_result = await post_tweet(
            generated_description=request.text or '',
            user_details=user_details,
            attachments=request.media_hints,
            original_content=metadata.get('original_content'),
            in_reply_to_tweet_id=target_ref if request.action == 'reply' else None,
            quote_tweet_id=target_ref if request.action == 'quote' else None,
        )
        if not tweet_result:
            return None

        tweet_id = tweet_result.get('id')
        tweet_url = tweet_result.get('url')
        media_id = tweet_result.get('media_id')
        media_ids: list = []
        if media_id is not None:
            media_ids = [media_id]
        return {
            'provider_ref': tweet_id,
            'provider_url': tweet_url,
            'tweet_id': tweet_id,
            'tweet_url': tweet_url,
            'delete_supported': True,
            'media_ids': media_ids,
        }

    async def delete(self, publication: Dict[str, Any]) -> bool:
        if publication.get('action') == 'retweet':
            return False
        provider_ref = publication.get('provider_ref')
        if not provider_ref:
            return False
        return await delete_tweet(provider_ref)

    def normalize_target_ref(self, target_ref: Optional[str]) -> Optional[str]:
        if target_ref in (None, ''):
            return None

        ref = str(target_ref).strip()
        status_match = STATUS_URL_RE.search(ref)
        if status_match:
            return status_match.group(1)
        if ref.isdigit():
            return ref
        return ref

    def _request_metadata(self, request: SocialPublishRequest) -> Dict[str, Any]:
        if request.source_context and request.source_context.metadata:
            return request.source_context.metadata
        return {}

    async def _retweet(self, target_tweet_id: str) -> Optional[Dict[str, Any]]:
        if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
            logger.error("[XProvider] Cannot retweet, API credentials missing.")
            return None

        try:
            auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
            auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
            api_v1 = tweepy.API(auth)

            credentials = await asyncio.get_event_loop().run_in_executor(None, api_v1.verify_credentials)
            user_id = getattr(credentials, 'id', None)
            screen_name = getattr(credentials, 'screen_name', 'user')
            if not user_id:
                logger.error("[XProvider] Could not resolve authenticated user ID for retweet")
                return None

            client_v2 = tweepy.Client(
                consumer_key=CONSUMER_KEY,
                consumer_secret=CONSUMER_SECRET,
                access_token=ACCESS_TOKEN,
                access_token_secret=ACCESS_TOKEN_SECRET,
            )
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client_v2.retweet(user_id, target_tweet_id),
            )
            provider_url = f"https://twitter.com/{screen_name}/status/{target_tweet_id}"
            return {
                'provider_ref': target_tweet_id,
                'provider_url': provider_url,
                'tweet_id': target_tweet_id,
                'tweet_url': provider_url,
                'delete_supported': False,
            }
        except Exception as e:
            logger.error(f"[XProvider] Error retweeting {target_tweet_id}: {e}", exc_info=True)
            return None
