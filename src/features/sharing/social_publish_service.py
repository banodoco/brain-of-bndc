from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .models import PublicationSourceContext, SocialPublishRequest, SocialPublishResult
from .providers import SocialPublishProvider

if TYPE_CHECKING:
    from src.common.db_handler import DatabaseHandler

logger = logging.getLogger('DiscordBot')


class SocialPublishService:
    """Canonical publish/delete service shared by all social caller paths."""
    SIGNATURE_VERSION = 'v1'

    def __init__(
        self,
        db_handler: 'DatabaseHandler',
        providers: Optional[Dict[str, SocialPublishProvider]] = None,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.db_handler = db_handler
        self.logger = logger_instance or logger
        if providers is None:
            from .providers.x_provider import XProvider
            from .providers.youtube_zapier_provider import YouTubeZapierProvider

            default_x_provider = XProvider()
            default_youtube_provider = YouTubeZapierProvider()
            self.providers = {
                'twitter': default_x_provider,
                'x': default_x_provider,
                'youtube': default_youtube_provider,
            }
        else:
            self.providers = providers
        self._publication_signing_secret = (
            os.getenv('SOCIAL_PUBLISH_SIGNING_SECRET')
            or os.getenv('DISCORD_BOT_TOKEN')
        )

    # ── Sprint-1-only read-only preview facade ─────────────────────────
    # This method MUST NOT call create_social_publication, publish_now,
    # enqueue, or any other mutating path.  It MUST NOT insert rows into
    # the social_publications table.  Existing callers (admin-chat, sharer,
    # sharing_cog) remain on their current publish paths unchanged.
    async def preview_publish_readiness(self, request: SocialPublishRequest) -> dict:
        """Return readiness information without mutating state or publishing.

        Sprint-1-only: provides route normalisation and provider availability
        for the live-update social loop so it can surface needs_review when
        routes or providers are absent.
        """
        result: dict = {
            "ready": False,
            "platform": request.platform,
            "action": request.action,
            "route_normalized": False,
            "provider_available": False,
            "errors": [],
        }

        # ── normalise route ────────────────────────────────────────────
        normalized_request, route_error = self._prepare_request_for_delivery(request)
        if route_error:
            result["errors"].append(route_error)
        else:
            result["route_normalized"] = True

        # ── check provider ─────────────────────────────────────────────
        provider = self._get_provider(request.platform)
        if not provider:
            result["errors"].append(
                f"Unsupported platform: {request.platform}"
            )
        else:
            result["provider_available"] = True

        result["ready"] = (
            result["route_normalized"]
            and result["provider_available"]
            and len(result["errors"]) == 0
        )
        return result

    async def publish_now(self, request: SocialPublishRequest) -> SocialPublishResult:
        if not self._publication_signing_secret:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error="Social publication signing secret is not configured",
            )

        provider = self._get_provider(request.platform)
        if not provider:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error=f"Unsupported platform: {request.platform}",
            )

        request, route_error = self._prepare_request_for_delivery(request)
        if route_error:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error=route_error,
            )

        publication = self.db_handler.create_social_publication(
            self._build_publication_record(request, status='processing'),
            guild_id=request.guild_id,
        )
        if not publication:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error="Failed to create social publication record",
            )

        publication_id = publication.get('publication_id')
        try:
            provider_result = await provider.publish(request)
            if not provider_result:
                self.db_handler.mark_social_publication_failed(
                    publication_id,
                    last_error="Provider publish failed",
                    guild_id=request.guild_id,
                )
                return SocialPublishResult(
                    publication_id=publication_id,
                    success=False,
                    error="Provider publish failed",
                )

            self.db_handler.mark_social_publication_succeeded(
                publication_id,
                guild_id=request.guild_id,
                provider_ref=provider_result.get('provider_ref'),
                provider_url=provider_result.get('provider_url'),
                delete_supported=bool(provider_result.get('delete_supported')),
            )
            self._record_legacy_shared_post(request, provider_result)
            return SocialPublishResult(
                publication_id=publication_id,
                success=True,
                tweet_id=provider_result.get('tweet_id'),
                tweet_url=provider_result.get('tweet_url'),
                provider_ref=provider_result.get('provider_ref'),
                provider_url=provider_result.get('provider_url'),
                delete_supported=bool(provider_result.get('delete_supported')),
            )
        except Exception as e:
            self.logger.error(f"[SocialPublishService] publish_now failed for {publication_id}: {e}", exc_info=True)
            self.db_handler.mark_social_publication_failed(
                publication_id,
                last_error=str(e),
                guild_id=request.guild_id,
            )
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error=str(e),
            )

    async def enqueue(self, request: SocialPublishRequest) -> SocialPublishResult:
        if not self._publication_signing_secret:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error="Social publication signing secret is not configured",
            )

        request, route_error = self._prepare_request_for_delivery(request)
        if route_error:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error=route_error,
            )

        publication = self.db_handler.create_social_publication(
            self._build_publication_record(request, status='queued'),
            guild_id=request.guild_id,
        )
        if not publication:
            return SocialPublishResult(
                publication_id=None,
                success=False,
                error="Failed to enqueue social publication",
            )
        return SocialPublishResult(
            publication_id=publication.get('publication_id'),
            success=True,
        )

    async def execute_publication(self, publication_id: str) -> SocialPublishResult:
        if not self._publication_signing_secret:
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error="Social publication signing secret is not configured",
            )

        publication = self.db_handler.get_social_publication_by_id(publication_id)
        if not publication:
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error="Publication not found",
            )
        if not self._verify_publication_integrity(publication):
            self.db_handler.mark_social_publication_failed(
                publication_id,
                last_error="Invalid publication signature",
                guild_id=publication.get('guild_id'),
            )
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error="Invalid publication signature",
            )

        request = self._request_from_publication(publication)
        if not request:
            self.db_handler.mark_social_publication_failed(
                publication_id,
                last_error="Unable to reconstruct request payload",
                guild_id=publication.get('guild_id'),
            )
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error="Unable to reconstruct request payload",
            )

        self.db_handler.mark_social_publication_processing(
            publication_id,
            guild_id=publication.get('guild_id'),
            attempt_count=publication.get('attempt_count'),
        )
        provider = self._get_provider(request.platform)
        if not provider:
            self.db_handler.mark_social_publication_failed(
                publication_id,
                last_error=f"Unsupported platform: {request.platform}",
                guild_id=request.guild_id,
            )
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error=f"Unsupported platform: {request.platform}",
            )

        try:
            provider_result = await provider.publish(request)
            if not provider_result:
                self.db_handler.mark_social_publication_failed(
                    publication_id,
                    last_error="Provider publish failed",
                    guild_id=request.guild_id,
                )
                return SocialPublishResult(
                    publication_id=publication_id,
                    success=False,
                    error="Provider publish failed",
                )

            self.db_handler.mark_social_publication_succeeded(
                publication_id,
                guild_id=request.guild_id,
                provider_ref=provider_result.get('provider_ref'),
                provider_url=provider_result.get('provider_url'),
                delete_supported=bool(provider_result.get('delete_supported')),
            )
            self._record_legacy_shared_post(request, provider_result)
            return SocialPublishResult(
                publication_id=publication_id,
                success=True,
                tweet_id=provider_result.get('tweet_id'),
                tweet_url=provider_result.get('tweet_url'),
                provider_ref=provider_result.get('provider_ref'),
                provider_url=provider_result.get('provider_url'),
                delete_supported=bool(provider_result.get('delete_supported')),
            )
        except Exception as e:
            self.logger.error(f"[SocialPublishService] execute_publication failed for {publication_id}: {e}", exc_info=True)
            self.db_handler.mark_social_publication_failed(
                publication_id,
                last_error=str(e),
                guild_id=request.guild_id,
            )
            return SocialPublishResult(
                publication_id=publication_id,
                success=False,
                error=str(e),
            )

    async def delete_publication(self, publication_id: str) -> bool:
        publication = self.db_handler.get_social_publication_by_id(publication_id)
        if not publication:
            return False
        if not self._verify_publication_integrity(publication):
            self.logger.warning(
                "[SocialPublishService] Refusing delete for publication %s due to invalid signature.",
                publication_id,
            )
            return False
        if not publication.get('delete_supported'):
            return False

        provider = self._get_provider(publication.get('platform'))
        if not provider:
            return False

        success = await provider.delete(publication)
        if not success:
            return False

        self.db_handler.mark_social_publication_cancelled(
            publication_id,
            guild_id=publication.get('guild_id'),
        )
        if getattr(self.db_handler, 'supabase', None):
            self.db_handler.supabase.table('social_publications').update({
                'deleted_at': datetime.now(timezone.utc).isoformat(),
            }).eq('publication_id', publication_id).execute()
        if publication.get('action') == 'post':
            self.db_handler.mark_shared_post_deleted(
                publication.get('message_id'),
                self._legacy_platform_name(publication.get('platform')),
                guild_id=publication.get('guild_id'),
            )
        return True

    def _get_provider(self, platform: Optional[str]) -> Optional[SocialPublishProvider]:
        if not platform:
            return None
        return self.providers.get(str(platform).lower())

    def _build_publication_record(self, request: SocialPublishRequest, status: str) -> Dict[str, Any]:
        target_ref = request.target_post_ref
        provider = self._get_provider(request.platform)
        if provider:
            target_ref = provider.normalize_target_ref(target_ref)

        record = {
            'guild_id': request.guild_id,
            'channel_id': request.channel_id,
            'message_id': request.message_id,
            'user_id': request.user_id,
            'source_kind': request.source_kind,
            'platform': request.platform,
            'action': request.action,
            'route_key': self._route_key_for(request),
            'request_payload': self._request_payload(request),
            'target_post_ref': target_ref,
            'scheduled_at': request.scheduled_at or datetime.now(timezone.utc),
            'status': status,
        }
        record['integrity_version'] = self.SIGNATURE_VERSION
        record['integrity_signature'] = self._sign_publication_payload(record)
        return record

    def _request_payload(self, request: SocialPublishRequest) -> Dict[str, Any]:
        payload = asdict(request)
        payload['target_post_ref'] = request.target_post_ref
        return payload

    def _immutable_publication_payload(self, publication: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'guild_id': publication.get('guild_id'),
            'channel_id': publication.get('channel_id'),
            'message_id': publication.get('message_id'),
            'user_id': publication.get('user_id'),
            'source_kind': publication.get('source_kind'),
            'platform': publication.get('platform'),
            'action': publication.get('action'),
            'route_key': publication.get('route_key'),
            'request_payload': publication.get('request_payload') or {},
            'target_post_ref': publication.get('target_post_ref'),
            'scheduled_at': self._serialize_signature_value(publication.get('scheduled_at')),
            'integrity_version': publication.get('integrity_version') or self.SIGNATURE_VERSION,
        }

    def _serialize_signature_value(self, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        if isinstance(value, dict):
            return {key: self._serialize_signature_value(val) for key, val in value.items()}
        if isinstance(value, list):
            return [self._serialize_signature_value(item) for item in value]
        return value

    def _sign_publication_payload(self, publication: Dict[str, Any]) -> Optional[str]:
        if not self._publication_signing_secret:
            return None
        canonical_payload = self._immutable_publication_payload(publication)
        encoded = json.dumps(
            self._serialize_signature_value(canonical_payload),
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hmac.new(
            self._publication_signing_secret.encode('utf-8'),
            encoded,
            hashlib.sha256,
        ).hexdigest()

    def _verify_publication_integrity(self, publication: Dict[str, Any]) -> bool:
        expected_signature = self._sign_publication_payload(publication)
        actual_signature = publication.get('integrity_signature')
        if not expected_signature or not actual_signature:
            return False
        return hmac.compare_digest(str(actual_signature), expected_signature)

    def _route_key_for(self, request: SocialPublishRequest) -> Optional[str]:
        route_override = self._normalize_route_override(request.route_override)
        if route_override:
            route_key = route_override.get('route_key')
            if route_key is not None:
                return str(route_key)
        return None

    def _prepare_request_for_delivery(
        self,
        request: SocialPublishRequest,
    ) -> Tuple[SocialPublishRequest, Optional[str]]:
        server_config = getattr(self.db_handler, 'server_config', None)
        if server_config is None:
            normalized_override = self._normalize_route_override(request.route_override)
            if normalized_override == request.route_override:
                return request, None
            return replace(request, route_override=normalized_override), None

        if not server_config.is_feature_enabled(request.guild_id, request.channel_id, 'sharing'):
            return request, "Sharing is not enabled for this channel."

        normalized_override = self._normalize_route_override(request.route_override)
        if normalized_override:
            return replace(request, route_override=normalized_override), None

        route = server_config.resolve_social_route(
            request.guild_id,
            request.channel_id,
            request.platform,
        )
        if not route:
            return request, "No social route is configured for this channel and platform."

        return replace(request, route_override=self._normalize_route_override(route)), None

    def _normalize_route_override(
        self,
        route_override: Any,
    ) -> Optional[Dict[str, Any]]:
        if route_override in (None, ''):
            return None

        if isinstance(route_override, str):
            return {'route_key': route_override}

        if isinstance(route_override, dict):
            normalized = dict(route_override)
            route_key = normalized.get('route_key')
            if route_key is None and normalized.get('id') is not None:
                route_key = normalized.get('id')
            if route_key is not None:
                normalized['route_key'] = str(route_key)
            route_config = normalized.get('route_config')
            if route_config is None:
                normalized['route_config'] = {}
            return normalized

        return None

    def _record_legacy_shared_post(self, request: SocialPublishRequest, provider_result: Dict[str, Any]) -> None:
        policy = request.legacy_shared_post_policy or {}
        if policy.get('enabled', True) is False:
            return
        if request.action != 'post':
            return

        provider_ref = provider_result.get('provider_ref')
        provider_url = provider_result.get('provider_url')
        if not provider_ref:
            return

        self.db_handler.record_shared_post(
            discord_message_id=request.message_id,
            discord_user_id=request.user_id,
            platform=self._legacy_platform_name(request.platform),
            platform_post_id=provider_ref,
            platform_post_url=provider_url,
            delete_eligible_hours=int(policy.get('delete_eligible_hours', 6)),
            guild_id=request.guild_id,
        )

    def _request_from_publication(self, publication: Dict[str, Any]) -> Optional[SocialPublishRequest]:
        payload = publication.get('request_payload') or {}
        if not isinstance(payload, dict):
            return None

        source_context = payload.get('source_context')
        if isinstance(source_context, dict):
            payload['source_context'] = PublicationSourceContext(
                source_kind=source_context.get('source_kind', publication.get('source_kind')),
                metadata=source_context.get('metadata') or {},
            )

        scheduled_at = payload.get('scheduled_at')
        if isinstance(scheduled_at, str):
            payload['scheduled_at'] = self._parse_datetime(scheduled_at)

        payload.setdefault('message_id', publication.get('message_id'))
        payload.setdefault('channel_id', publication.get('channel_id'))
        payload.setdefault('guild_id', publication.get('guild_id'))
        payload.setdefault('user_id', publication.get('user_id'))
        payload.setdefault('platform', publication.get('platform'))
        payload.setdefault('action', publication.get('action'))
        payload.setdefault('source_kind', publication.get('source_kind'))
        payload.setdefault('target_post_ref', publication.get('target_post_ref'))
        return SocialPublishRequest(**payload)

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _legacy_platform_name(self, platform: Optional[str]) -> str:
        normalized = (platform or 'twitter').lower()
        if normalized == 'x':
            return 'twitter'
        return normalized
