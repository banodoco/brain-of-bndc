"""Tool definitions and executors for admin chat.

Tools call into existing bot functionality to maintain consistency.
Following the Arnold pattern - includes a 'reply' tool that the LLM uses to respond.
"""
import os
import re
import sys
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4
import discord
from dotenv import load_dotenv
from src.common.db_handler import WalletUpdateBlockedError
from src.features.grants.solana_client import is_valid_solana_address
from src.features.sharing.models import PublicationSourceContext, SocialPublishRequest

load_dotenv()

logger = logging.getLogger('DiscordBot')

# Add project root to path for weekly_digest imports
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Cached Supabase client
_supabase_client = None


def _get_supabase():
    """Get or create a cached Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        _supabase_client = create_client(url, key)
    return _supabase_client


_server_config = None


def _get_server_config():
    global _server_config
    if _server_config is None:
        from src.common.server_config import ServerConfig
        _server_config = ServerConfig(_get_supabase())
    return _server_config


def _resolve_guild_id(params: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Prefer explicit tool input, then fall back to the configured default guild."""
    explicit = None
    if params:
        raw = params.get('guild_id')
        if raw not in (None, '', 0, '0'):
            try:
                explicit = int(raw)
            except (TypeError, ValueError):
                pass
    return _get_server_config().resolve_guild_id(explicit, require_write=True)

# Tables the agent is allowed to query
QUERYABLE_TABLES = {
    'competitions', 'competition_entries', 'discord_reactions',
    'discord_messages', 'members', 'discord_channels',
    'events', 'invite_codes', 'grant_applications',
    'daily_summaries', 'channel_summary', 'shared_posts',
    'pending_intros', 'intro_votes', 'timed_mutes',
    'social_publications', 'social_channel_routes',
    'topic_editor_runs', 'topics', 'topic_sources', 'topic_aliases',
    'topic_transitions', 'editorial_observations', 'topic_editor_checkpoints',
    'live_update_editor_runs', 'live_update_candidates',
    'live_update_decisions', 'live_update_feed_items',
    'live_update_editorial_memory', 'live_update_watchlist',
    'live_update_duplicate_state', 'live_update_checkpoints',
    'live_top_creation_runs', 'live_top_creation_posts',
    'live_top_creation_checkpoints',
}
assert 'payment_requests' not in QUERYABLE_TABLES
assert 'payment_channel_routes' not in QUERYABLE_TABLES
assert 'wallet_registry' not in QUERYABLE_TABLES


# ========== Tool Definitions (Anthropic format) ==========

TOOLS = [
    {
        "name": "reply",
        "description": "Send one or more messages back to the user. Use this to respond. Can send multiple messages if needed (e.g., for long content or separate topics).",
        "input_schema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of messages to send to the user (sent as separate Discord messages)"
                },
                "message": {
                    "type": "string",
                    "description": "Single message to send (alternative to messages array)"
                }
            },
            "required": []
        }
    },
    {
        "name": "end_turn",
        "description": "End the current turn without sending a message. Use when you've completed actions silently or when no response is needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional: brief reason for ending without reply (for logging)"
                }
            },
            "required": []
        }
    },
    {
        "name": "find_messages",
        "description": "Search and browse Discord messages. Combine any filters. Use for ALL message finding: top posts, user posts, content search, channel browsing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text search (case-insensitive)"
                },
                "username": {
                    "type": "string",
                    "description": "Filter by user (partial match)"
                },
                "channel_id": {
                    "type": "string",
                    "description": "Filter to channel/thread"
                },
                "min_reactions": {
                    "type": "integer",
                    "description": "Min reactions (default 0)"
                },
                "has_media": {
                    "type": "boolean",
                    "description": "Only posts with attachments"
                },
                "days": {
                    "type": "integer",
                    "description": "Filter to last N days. Omit to search all time."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20, max 100)"
                },
                "sort": {
                    "type": "string",
                    "enum": ["reactions", "unique_reactors", "date"],
                    "description": "Sort order (default: date, most recent first). reactions = most reacted. unique_reactors = most distinct reactors."
                },
                "refresh_media": {
                    "type": "boolean",
                    "description": "Get fresh media URLs for results (use for showing images/videos)"
                },
                "live": {
                    "type": "boolean",
                    "description": "Use live Discord API instead of DB (requires channel_id). Good for seeing current state including bot posts."
                }
            },
            "required": []
        }
    },
    {
        "name": "inspect_message",
        "description": "Deep look at one message: full content, reactions with emoji counts, surrounding context, replies, fresh media URLs. Use to drill into a specific post.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID"
                },
                "context_size": {
                    "type": "integer",
                    "description": "Number of surrounding messages to include (default 3)"
                }
            },
            "required": ["message_id"]
        }
    },
    {
        "name": "share_to_social",
        "description": "Share or schedule a Discord message to social media. Supports X/Twitter posts, replies, retweets, and quote tweets, plus YouTube posts via Zapier. Use schedule_for for queued publishing with an ISO-8601 timestamp. Use action plus target_post for X replies/retweets. reply_to_tweet remains supported as a reply-only alias and replies still default to text_only=true unless you explicitly set text_only=false. Publishing resolves the configured social route for the message channel unless you supply route_key to force a specific route. Also supports direct posting with media_urls + tweet_text, bypassing Discord message lookup. Immediate success returns provider_url/provider_ref plus legacy tweet_url/tweet_id aliases when applicable; scheduled success returns publication_id with queued status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_link": {
                    "type": "string",
                    "description": "Discord message link (e.g., https://discord.com/channels/123/456/789)"
                },
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID (alternative to message_link)"
                },
                "tweet_text": {
                    "type": "string",
                    "description": "Custom tweet text (max 280 chars). If provided, this exact text is used as the tweet instead of auto-generating."
                },
                "media_urls": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    },
                    "description": "Direct media URLs to post (e.g. Supabase video links). Use instead of message_link/message_id for external media. tweet_text is required when using media_urls."
                },
                "action": {
                    "type": "string",
                    "enum": ["post", "reply", "retweet", "quote"],
                    "description": "Publish action. Defaults to post. If omitted but target_post/reply_to_tweet is provided, reply is assumed. Use quote to quote-tweet a target_post."
                },
                "target_post": {
                    "type": "string",
                    "description": "Target tweet/X post ID or full URL for reply/retweet actions."
                },
                "reply_to_tweet": {
                    "type": "string",
                    "description": "Backward-compatible alias for target_post when action=reply. Accepts a Tweet ID or full tweet URL."
                },
                "text_only": {
                    "type": "boolean",
                    "description": "Skip the source message's attachments and post text only. Defaults to true for thread replies (so a follow-up doesn't reattach the same media as the parent tweet) and false otherwise. Set to false explicitly to force a reply that DOES include media."
                },
                "schedule_for": {
                    "type": "string",
                    "description": "Optional ISO-8601 timestamp for queued publishing. Stored in UTC. Example: 2026-04-09T18:30:00Z"
                },
                "platform": {
                    "type": "string",
                    "description": "Optional platform override. Supported: twitter/x and youtube."
                },
                "route_key": {
                    "type": "string",
                    "description": "Optional explicit social route override. Use a social_channel_routes.id value when you need to bypass the normal channel -> parent -> guild-default route resolution."
                }
            },
            "required": []
        }
    },
    {
        "name": "list_social_routes",
        "description": "List configured outbound social routes for the current guild. Use this before creating or overriding routes. Omit channel_id to include guild-default and channel-specific rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "Optional platform filter. Defaults to twitter. Supported: twitter/x and youtube."
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID filter."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Optional enabled-state filter."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 100)."
                }
            },
            "required": []
        }
    },
    {
        "name": "create_social_route",
        "description": "Create a social route row for a guild default or a specific channel. Omit channel_id for the guild-default route. For twitter, route_config needs {\"account\": \"main\"}. For youtube, route_config can include privacy_status, default_tags, playlist_id, made_for_kids, and webhook_env_var.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "Platform name. Defaults to twitter. Supported: twitter/x and youtube."
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID. Omit for the guild-default route."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the route is enabled. Defaults to true."
                },
                "route_config": {
                    "type": "object",
                    "description": "JSON route config payload, for example {\"account\": \"main\"} for twitter or {\"privacy_status\": \"private\", \"default_tags\": [\"ADOS\"]} for youtube."
                }
            },
            "required": []
        }
    },
    {
        "name": "update_social_route",
        "description": "Update one social route row by id. Pass channel_id as an empty string to convert it to the guild-default route.",
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {
                    "type": "string",
                    "description": "social_channel_routes.id to update."
                },
                "platform": {
                    "type": "string",
                    "description": "Optional platform override."
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID. Pass an empty string to clear to the guild-default route."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Optional enabled-state override."
                },
                "route_config": {
                    "type": "object",
                    "description": "Optional replacement JSON route config payload."
                }
            },
            "required": ["route_id"]
        }
    },
    {
        "name": "delete_social_route",
        "description": "Delete one social route row by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {
                    "type": "string",
                    "description": "social_channel_routes.id to delete."
                }
            },
            "required": ["route_id"]
        }
    },
    {
        "name": "list_payment_routes",
        "description": "List configured payment routes for the active guild. Use this before creating or overriding payment confirmations/notifications.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producer": {
                    "type": "string",
                    "description": "Optional producer filter, for example grants."
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID filter."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Optional enabled-state filter."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 100)."
                }
            },
            "required": []
        }
    },
    {
        "name": "create_payment_route",
        "description": "Create a payment route row for a guild default or a specific channel. route_config can direct confirmations/notifications to the source thread/forum post or to explicit channel/thread IDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producer": {
                    "type": "string",
                    "description": "Producer name, for example grants."
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID. Omit for the guild-default route."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the route is enabled. Defaults to true."
                },
                "route_config": {
                    "type": "object",
                    "description": "JSON route config payload, for example {\"use_source_thread\": true} or explicit confirm/notify destination IDs."
                }
            },
            "required": ["producer"]
        }
    },
    {
        "name": "update_payment_route",
        "description": "Update one payment route row by id. Pass channel_id as an empty string to convert it to the guild-default route.",
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {
                    "type": "string",
                    "description": "payment_channel_routes.id to update."
                },
                "producer": {
                    "type": "string",
                    "description": "Optional producer override."
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID. Pass an empty string to clear to the guild-default route."
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Optional enabled-state override."
                },
                "route_config": {
                    "type": "object",
                    "description": "Optional replacement JSON route config payload."
                }
            },
            "required": ["route_id"]
        }
    },
    {
        "name": "delete_payment_route",
        "description": "Delete one payment route row by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "route_id": {
                    "type": "string",
                    "description": "payment_channel_routes.id to delete."
                }
            },
            "required": ["route_id"]
        }
    },
    {
        "name": "list_wallets",
        "description": "List wallet registry rows for the active guild with redacted wallet addresses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chain": {
                    "type": "string",
                    "description": "Optional chain filter, for example solana."
                },
                "discord_user_id": {
                    "type": "string",
                    "description": "Optional Discord user ID filter."
                },
                "verified": {
                    "type": "boolean",
                    "description": "Optional filter for whether a wallet has been verified."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 100)."
                }
            },
            "required": []
        }
    },
    {
        "name": "list_payments",
        "description": "List payment ledger rows for the active guild with redacted wallet addresses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional payment status filter."
                },
                "producer": {
                    "type": "string",
                    "description": "Optional producer filter, for example grants."
                },
                "recipient_discord_id": {
                    "type": "string",
                    "description": "Optional recipient Discord user ID filter."
                },
                "wallet_id": {
                    "type": "string",
                    "description": "Optional wallet_id filter."
                },
                "is_test": {
                    "type": "boolean",
                    "description": "Optional test-vs-final filter."
                },
                "route_key": {
                    "type": "string",
                    "description": "Optional route key filter."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 100)."
                }
            },
            "required": []
        }
    },
    {
        "name": "get_payment_status",
        "description": "Get one payment row by payment_id with redacted wallet address and status details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_id": {
                    "type": "string",
                    "description": "payment_requests.payment_id to inspect."
                }
            },
            "required": ["payment_id"]
        }
    },
    {
        "name": "retry_payment",
        "description": "Move one failed payment back to queued. This is blocked for submitted and manual_hold rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_id": {
                    "type": "string",
                    "description": "payment_requests.payment_id to retry."
                }
            },
            "required": ["payment_id"]
        }
    },
    {
        "name": "hold_payment",
        "description": "Force one payment into manual_hold with a reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_id": {
                    "type": "string",
                    "description": "payment_requests.payment_id to hold."
                },
                "reason": {
                    "type": "string",
                    "description": "Reason to record on the payment."
                }
            },
            "required": ["payment_id", "reason"]
        }
    },
    {
        "name": "release_payment",
        "description": "Release one manual_hold payment. Allowed targets are failed or manual_hold.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_id": {
                    "type": "string",
                    "description": "payment_requests.payment_id to release."
                },
                "new_status": {
                    "type": "string",
                    "enum": ["failed", "manual_hold"],
                    "description": "Target status."
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason to record."
                }
            },
            "required": ["payment_id", "new_status"]
        }
    },
    {
        "name": "cancel_payment",
        "description": "Cancel one payment if it has never entered submitted or manual_hold.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_id": {
                    "type": "string",
                    "description": "payment_requests.payment_id to cancel."
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason to record."
                }
            },
            "required": ["payment_id"]
        }
    },
    {
        "name": "initiate_payment",
        "description": (
            "Create an admin-triggered payment request for a Banodoco user. "
            "The flow branches on the recipient's current wallet state: a verified wallet goes "
            "straight to admin approval; an unverified wallet already on file (from a prior "
            "attempt or from the inline wallet_address below) is reused — the bot fires the test "
            "payment immediately without re-asking the recipient; no wallet on file puts the "
            "intent in awaiting_wallet and prompts the recipient in-channel. Use wallet_address "
            "when the admin gives you the address inline ('pay @X Y SOL, wallet is Z') so the "
            "whole flow happens in one tool call instead of two."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient_user_id": {
                    "type": "string",
                    "description": "Discord user ID of the intended payment recipient."
                },
                "amount_sol": {
                    "type": "number",
                    "description": "SOL amount requested by the admin."
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason to store with the payment intent."
                },
                "wallet_address": {
                    "type": "string",
                    "description": (
                        "Optional Solana wallet address to register for the recipient inline. "
                        "If provided, the tool upserts the wallet (unverified) before running "
                        "the flow, so the recipient skips the awaiting_wallet step and the "
                        "test payment fires immediately. Only pass this when the admin explicitly "
                        "provides a wallet address in their command."
                    )
                }
            },
            "required": ["recipient_user_id", "amount_sol"]
        }
    },
    {
        "name": "initiate_batch_payment",
        "description": "Create 1-20 admin-triggered payment intents atomically for multiple Banodoco users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payments": {
                    "type": "array",
                    "description": "Batch of intended recipients and SOL amounts.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "recipient_user_id": {
                                "type": "string",
                                "description": "Discord user ID of the intended payment recipient."
                            },
                            "amount_sol": {
                                "type": "number",
                                "description": "SOL amount requested by the admin."
                            },
                            "reason": {
                                "type": "string",
                                "description": "Optional reason to store with this payment intent."
                            }
                        },
                        "required": ["recipient_user_id", "amount_sol"]
                    }
                }
            },
            "required": ["payments"]
        }
    },
    {
        "name": "query_payment_state",
        "description": "Read-only payment state lookup by payment_id or by recipient user_id. Wallets are redacted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_id": {
                    "type": "string",
                    "description": "Optional payment_requests.payment_id to inspect."
                },
                "user_id": {
                    "type": "string",
                    "description": "Optional Discord user ID to list recent payments for."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max recent rows when querying by user_id (default 20, max 100)."
                }
            },
            "required": []
        }
    },
    {
        "name": "query_wallet_state",
        "description": "Read-only wallet_registry lookup for a Discord user with redacted wallet addresses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID whose wallets should be listed."
                }
            },
            "required": ["user_id"]
        }
    },
    {
        "name": "list_recent_payments",
        "description": "Read-only list of recent payment_requests rows with redacted wallet addresses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producer": {
                    "type": "string",
                    "description": "Optional producer filter, for example admin_chat."
                },
                "user_id": {
                    "type": "string",
                    "description": "Optional recipient Discord user ID filter."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 20, max 100)."
                }
            },
            "required": []
        }
    },
    {
        "name": "upsert_wallet_for_user",
        "description": (
            "Admin-only wallet upsert for a user. The wallet remains unverified until a test "
            "payment round-trip completes. If this user has a pending awaiting_wallet intent, "
            "this tool also advances it to the test-payment phase automatically — the bot fires "
            "the test payment and asks the recipient to confirm receipt, just as if they had "
            "posted the wallet themselves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID whose wallet should be created or updated."
                },
                "wallet_address": {
                    "type": "string",
                    "description": "Solana wallet address to register."
                },
                "chain": {
                    "type": "string",
                    "description": "Blockchain identifier. Only solana is supported."
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason to store in wallet metadata."
                }
            },
            "required": ["user_id", "wallet_address"]
        }
    },
    {
        "name": "resolve_admin_intent",
        "description": "Admin-only cancel path for one admin payment intent. Cancels linked payments when they are still cancellable.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {
                    "type": "string",
                    "description": "admin_payment_intents.intent_id to cancel."
                },
                "note": {
                    "type": "string",
                    "description": "Optional operator note to include in the cancellation reason."
                }
            },
            "required": ["intent_id"]
        }
    },
    {
        "name": "get_active_channels",
        "description": "List channels that have been active recently, sorted by message count. Use this to find where the activity is.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to check (default 7)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_daily_summaries",
        "description": "Get legacy bot-generated daily_summaries history. This is not the active overview system; use get_live_update_status for current live-editor state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days of summaries to fetch (default 7)"
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional: filter to a specific channel"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_live_update_status",
        "description": "Inspect active topic-editor state: recent topic_editor_runs, topic counts, transitions/rejections, publication failures, override rate, and legacy live-update rollback state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "How far back to inspect recent live-editor activity (default 24, max 168)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows per recent section (default 5, max 25)"
                }
            },
            "required": []
        }
    },
    {
        "name": "get_member_info",
        "description": "Get information about a Discord member including their sharing preferences and social handles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID"
                },
                "username": {
                    "type": "string",
                    "description": "Discord username to search for (alternative to user_id)"
                }
            },
            "required": []
        }
    },
    {
        "name": "update_member_socials",
        "description": (
            "Set or clear a member's Twitter/Reddit handle on file. The Twitter handle is "
            "used by social picks to auto-tag the creator when drafting tweets. Pass an "
            "empty string to clear a field; omit a field to leave it unchanged. Accepts "
            "@handle, full URLs (twitter.com/x.com), or plain usernames — they'll be "
            "normalized when used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID of the member to update"
                },
                "twitter_url": {
                    "type": "string",
                    "description": "Twitter handle (@handle, URL, or username). Empty string clears it."
                },
                "reddit_url": {
                    "type": "string",
                    "description": "Reddit handle (u/name or URL). Empty string clears it."
                }
            },
            "required": ["user_id"]
        }
    },
    {
        "name": "mute_speaker",
        "description": (
            "Remove the Speaker role from a member, or update the duration of an existing "
            "mute. Mirrors the /mute slash command. Pass an optional `duration` like "
            "'1h', '7d', '2w' for an auto-unmute; omit for a permanent mute. If the user "
            "is already muted, this updates/replaces their timed-mute record (e.g. "
            "convert a permanent mute to a 30-day mute, or extend a 7-day to 30-day). "
            "`reason` is required and is posted to the moderation log channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID of the member to mute"
                },
                "reason": {
                    "type": "string",
                    "description": "Required free-text reason recorded with the mute and posted to the moderation channel."
                },
                "duration": {
                    "type": "string",
                    "description": "Optional duration like '1h', '7d', '2w'. Omit for a permanent mute."
                }
            },
            "required": ["user_id", "reason"]
        }
    },
    {
        "name": "unmute_speaker",
        "description": (
            "Restore the Speaker role to a member, reversing a /mute or mute_speaker call. "
            "Mirrors the /unmute slash command. Clears any pending timed-mute record."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Discord user ID of the member to unmute"
                },
                "reason": {
                    "type": "string",
                    "description": "Optional free-text reason recorded in the audit log."
                }
            },
            "required": ["user_id"]
        }
    },
    {
        "name": "get_bot_status",
        "description": "Get the bot's current status including uptime and connections.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "search_logs",
        "description": "Search the bot's system logs. See errors, recent tool calls, feature traces. Use to check what happened, diagnose issues, or review your own recent actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for in log messages (e.g. 'AdminChat', 'error', 'share')"
                },
                "level": {
                    "type": "string",
                    "enum": ["ERROR", "WARNING", "INFO"],
                    "description": "Filter by log level (default: all levels)"
                },
                "hours": {
                    "type": "integer",
                    "description": "Hours back to search (default 6, max 48)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 30, max 100)"
                }
            },
            "required": []
        }
    },
    {
        "name": "send_message",
        "description": "Send a message to a Discord channel as the bot. Can optionally reply to a specific message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel or thread ID to send to"
                },
                "content": {
                    "type": "string",
                    "description": "Message content to send"
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional: message ID to reply to"
                }
            },
            "required": ["channel_id", "content"]
        }
    },
    {
        "name": "edit_message",
        "description": "Edit a bot message in a Discord channel. Can only edit messages sent by the bot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID"
                },
                "message_id": {
                    "type": "string",
                    "description": "Message ID to edit"
                },
                "content": {
                    "type": "string",
                    "description": "New message content"
                }
            },
            "required": ["channel_id", "message_id", "content"]
        }
    },
    {
        "name": "delete_message",
        "description": "Delete one or more messages by ID. Use find_messages(live=true) first to see messages and their IDs, then delete the ones that need removing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID"
                },
                "message_id": {
                    "type": "string",
                    "description": "Single message ID to delete"
                },
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of message IDs to delete (for bulk deletion)"
                }
            },
            "required": ["channel_id"]
        }
    },
    {
        "name": "upload_file",
        "description": "Upload a file to a Discord channel. Use for sharing videos, images, or other files. The file must be accessible on the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID"
                },
                "file_path": {
                    "type": "string",
                    "description": "Local file path to upload"
                },
                "content": {
                    "type": "string",
                    "description": "Optional message to send with the file"
                }
            },
            "required": ["channel_id", "file_path"]
        }
    },
    {
        "name": "resolve_user",
        "description": "Resolve a username to a Discord user ID (for mentions). Also returns their display name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username to look up"
                }
            },
            "required": ["username"]
        }
    },
    {
        "name": "query_table",
        "description": "Query any database table directly. Use for data that isn't covered by other tools (e.g. competition_entries, competitions, discord_reactions, social_publications, social_channel_routes, events, grant_applications). Returns up to `limit` rows matching the filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name (e.g. competition_entries, competitions, discord_reactions, social_publications, social_channel_routes, events, invite_codes, grant_applications, members, discord_messages, discord_channels)"
                },
                "select": {
                    "type": "string",
                    "description": "Comma-separated columns to return (default: *)"
                },
                "filters": {
                    "type": "object",
                    "description": "Equality filters as {column: value}. Use special prefixes for other operators: 'gt.', 'gte.', 'lt.', 'lte.', 'neq.', 'like.', 'ilike.' (e.g. {\"reaction_count\": \"gte.5\", \"author_id\": \"123456789\"})"
                },
                "order": {
                    "type": "string",
                    "description": "Column to order by (prefix with '-' for descending). Use a real column for the table you queried (e.g. '-reaction_count' for discord_messages)."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default: 25, max: 100)"
                }
            },
            "required": ["table"]
        }
    },
    {
        "name": "download_media",
        "description": "Download attachments from a Discord message to the local filesystem for processing. Files are saved to /tmp/media/{message_id}/. Use before run_media_command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Discord message ID"
                },
                "channel_id": {
                    "type": "string",
                    "description": "Channel/thread ID containing the message"
                }
            },
            "required": ["message_id", "channel_id"]
        }
    },
    {
        "name": "run_media_command",
        "description": "Run a media processing command (ffmpeg, ffprobe, or python3 for PIL/Pillow). Working directory is /tmp/media/. 5 minute timeout. Use for combining images, transcoding video, generating thumbnails, image compositing with PIL, etc. For PIL: python3 -c \"from PIL import Image; ...\"",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run. Must start with ffmpeg, ffprobe, or python3."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "list_media_files",
        "description": "List files in the media working directory (/tmp/media/). Use to see downloaded files and processing results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Subdirectory to list (relative to /tmp/media/). Defaults to listing all."
                }
            },
            "required": []
        }
    },
]

# ========== Helper Functions ==========

_VISIBLE_CHANNEL_CACHE_TTL_SECONDS = 60
_visible_channel_cache: Dict[Tuple[int, int], Tuple[float, Set[int]]] = {}

def parse_message_link(link: str) -> Optional[Dict[str, int]]:
    """Parse a Discord message link into guild_id, channel_id, message_id."""
    pattern = r'https?://(?:discord\.com|discordapp\.com)/channels/(\d+)/(\d+)/(\d+)'
    match = re.match(pattern, link)
    if match:
        return {
            'guild_id': int(match.group(1)),
            'channel_id': int(match.group(2)),
            'message_id': int(match.group(3))
        }
    return None


def _normalize_social_platform(platform: Any) -> str:
    normalized = str(platform or 'twitter').strip().lower()
    if normalized == 'x':
        return 'twitter'
    return normalized


def _social_posted_message(platform: Any, action: str, provider_url: Optional[str]) -> str:
    normalized = _normalize_social_platform(platform)
    if normalized == 'youtube':
        return f"Queued YouTube upload via Zapier: {provider_url or 'URL pending from Zapier'}"
    if action == 'reply':
        return f"Posted tweet: {provider_url} (reply in thread)"
    if action == 'retweet':
        return f"Retweeted post: {provider_url}"
    if action == 'quote':
        return f"Quote tweeted: {provider_url}"
    return f"Posted tweet: {provider_url}"


def _normalize_payment_producer(producer: Any) -> str:
    normalized = str(producer or '').strip().lower()
    if not normalized:
        raise ValueError("producer is required")
    return normalized


def _parse_optional_channel_id(raw_value: Any, *, allow_clear: bool = True) -> Optional[int]:
    if raw_value is None:
        return None
    if raw_value == '' and allow_clear:
        return None
    if raw_value == '':
        raise ValueError("channel_id cannot be empty")
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("channel_id must be a Discord channel ID") from exc


def _normalize_route_config(route_config: Any) -> Dict[str, Any]:
    if route_config in (None, ''):
        return {}
    if not isinstance(route_config, dict):
        raise ValueError("route_config must be a JSON object")
    return dict(route_config)


def _normalize_social_route_config(route_config: Any, *, platform: Any) -> Dict[str, Any]:
    normalized = _normalize_route_config(route_config)
    normalized_platform = _normalize_social_platform(platform)
    if normalized_platform == 'twitter':
        account = str(normalized.get('account') or '').strip()
        if not account:
            raise ValueError(
                "social twitter routes require route_config.account. "
                "If you meant payouts or payment confirmations, use create_payment_route instead."
            )
        normalized['account'] = account
    return normalized


def _resolve_db_handler_guild_id(db_handler: Any, params: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Resolve a guild for db-handler-backed tools, with a narrow in-memory fallback for tests."""
    params = params or {}
    if params.get('guild_id') not in (None, '', 0, '0'):
        return _resolve_guild_id(params)

    server_config = getattr(db_handler, 'server_config', None)
    if server_config is not None:
        try:
            return server_config.resolve_guild_id(None, require_write=True)
        except Exception:
            pass

    for attr_name in ('payment_routes', 'wallets', 'payments'):
        rows = getattr(db_handler, attr_name, None)
        if not isinstance(rows, list):
            continue
        guild_ids = {
            int(row['guild_id'])
            for row in rows
            if isinstance(row, dict) and row.get('guild_id') is not None
        }
        if len(guild_ids) == 1:
            return next(iter(guild_ids))

    return _resolve_guild_id(params)


def _list_wallet_rows(
    db_handler: Any,
    *,
    guild_id: int,
    chain: Optional[str],
    discord_user_id: Optional[int],
    verified: Optional[bool],
    limit: int,
) -> List[Dict[str, Any]]:
    """Support both the production db handler and lightweight test doubles."""
    try:
        return db_handler.list_wallets(
            guild_id=guild_id,
            chain=chain,
            discord_user_id=discord_user_id,
            verified_only=bool(verified) if verified is not None else False,
            limit=limit,
        )
    except TypeError:
        return db_handler.list_wallets(
            guild_id=guild_id,
            chain=chain,
            discord_user_id=discord_user_id,
            verified=verified,
            limit=limit,
        )


def _redact_wallet_address(wallet_address: Any) -> Optional[str]:
    if wallet_address in (None, ''):
        return None
    wallet = str(wallet_address)
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:4]}...{wallet[-4:]}"


def _redact_payment_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "payment_id": row.get("payment_id"),
        "guild_id": row.get("guild_id"),
        "producer": row.get("producer"),
        "producer_ref": row.get("producer_ref"),
        "wallet_id": row.get("wallet_id"),
        "recipient_discord_id": row.get("recipient_discord_id"),
        "recipient_wallet": _redact_wallet_address(row.get("recipient_wallet")),
        "chain": row.get("chain"),
        "provider": row.get("provider"),
        "is_test": row.get("is_test"),
        "route_key": row.get("route_key"),
        "confirm_channel_id": row.get("confirm_channel_id"),
        "confirm_thread_id": row.get("confirm_thread_id"),
        "notify_channel_id": row.get("notify_channel_id"),
        "notify_thread_id": row.get("notify_thread_id"),
        "amount_token": row.get("amount_token"),
        "amount_usd": row.get("amount_usd"),
        "token_price_usd": row.get("token_price_usd"),
        "status": row.get("status"),
        "send_phase": row.get("send_phase"),
        "tx_signature": row.get("tx_signature"),
        "attempt_count": row.get("attempt_count"),
        "retry_after": row.get("retry_after"),
        "scheduled_at": row.get("scheduled_at"),
        "confirmed_by": row.get("confirmed_by"),
        "confirmed_by_user_id": row.get("confirmed_by_user_id"),
        "confirmed_at": row.get("confirmed_at"),
        "submitted_at": row.get("submitted_at"),
        "completed_at": row.get("completed_at"),
        "last_error": row.get("last_error"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _redact_wallet_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "wallet_id": row.get("wallet_id"),
        "guild_id": row.get("guild_id"),
        "discord_user_id": row.get("discord_user_id"),
        "chain": row.get("chain"),
        "wallet_address": _redact_wallet_address(row.get("wallet_address")),
        "verified_at": row.get("verified_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _pop_and_warn_wallet_override(params: Dict[str, Any], *, tool_name: str) -> Dict[str, Any]:
    trusted = dict(params or {})
    popped_wallet_address = trusted.pop('wallet_address', None)
    popped_recipient_wallet = trusted.pop('recipient_wallet', None)
    if popped_wallet_address not in (None, '') or popped_recipient_wallet not in (None, ''):
        logger.warning(
            "[AdminChat] Ignoring injected wallet fields for %s: wallet_address=%s recipient_wallet=%s",
            tool_name,
            _redact_wallet_address(popped_wallet_address),
            _redact_wallet_address(popped_recipient_wallet),
        )
    return trusted


def _get_configured_admin_user_id() -> Optional[int]:
    raw_admin_user_id = os.getenv('ADMIN_USER_ID')
    if raw_admin_user_id in (None, ''):
        return None
    try:
        return int(raw_admin_user_id)
    except (TypeError, ValueError):
        logger.error("[AdminChat] Invalid ADMIN_USER_ID value: %r", raw_admin_user_id)
        return None


def _require_injected_admin_user_id(params: Dict[str, Any]) -> tuple[Optional[int], Optional[str]]:
    configured_admin_user_id = _get_configured_admin_user_id()
    if configured_admin_user_id is None:
        return None, "ADMIN_USER_ID is not configured"

    try:
        injected_admin_user_id = int(params.get('admin_user_id'))
    except (TypeError, ValueError):
        return None, "Permission denied"

    if injected_admin_user_id != configured_admin_user_id:
        return None, "Permission denied"
    return configured_admin_user_id, None


async def _dm_admin_user(
    bot: discord.Client,
    admin_user_id: int,
    message: str,
    *,
    log_context: str,
) -> bool:
    if bot is None:
        return False
    try:
        admin_user = await bot.fetch_user(admin_user_id)
        await admin_user.send(message)
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("[AdminChat] Failed to DM admin for %s: %s", log_context, e)
        return False


def _force_wallet_unverified(db_handler: Any, wallet_record: Dict[str, Any], *, guild_id: int) -> Dict[str, Any]:
    if not wallet_record:
        return wallet_record
    wallet_id = wallet_record.get('wallet_id')
    if wallet_id and getattr(db_handler, 'supabase', None):
        try:
            result = (
                db_handler.supabase.table('wallet_registry')
                .update({'verified_at': None})
                .eq('wallet_id', wallet_id)
                .eq('guild_id', guild_id)
                .execute()
            )
            if result.data:
                return result.data[0]
        except Exception as e:
            logger.error(
                "[AdminChat] Error clearing verification for wallet %s in guild %s: %s",
                wallet_id,
                guild_id,
                e,
                exc_info=True,
            )
    updated_wallet = dict(wallet_record)
    updated_wallet['verified_at'] = None
    return updated_wallet


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be true or false")


def format_message_for_llm(msg: Dict, include_link: bool = True) -> Dict:
    """Format a message dict for LLM consumption."""
    result = {
        "message_id": str(msg.get('message_id')),
        "author": msg.get('author_name', 'Unknown'),
        "content": (msg.get('content', '') or '')[:300],
        "reactions": msg.get('reaction_count', 0),
        "unique_reactors": msg.get('unique_reactor_count'),
        "has_media": bool(msg.get('attachments') or msg.get('attachment_urls')),
        "date": msg.get('created_at', '')[:10] if msg.get('created_at') else None,
        "channel_id": str(msg.get('channel_id', '')),
    }
    if msg.get('channel_name'):
        result["channel"] = msg.get('channel_name')
    if msg.get('media_urls'):
        result["media_urls"] = msg['media_urls']
    if include_link:
        link_guild_id = msg.get('guild_id') or _get_server_config().resolve_guild_id(require_write=True)
        # Route through thread_id when set so links to forum posts land on the
        # specific thread instead of the parent forum's main page.
        thread_id = msg.get('thread_id')
        route_id = thread_id if thread_id else msg.get('channel_id')
        result["link"] = f"https://discord.com/channels/{link_guild_id}/{route_id}/{msg.get('message_id')}"
    return result


def _build_summary(formatted: List[Dict], header: str, media_urls_map: Dict[str, str] = None) -> str:
    """Build a pre-formatted summary string from formatted messages.

    Uses ---SPLIT--- markers so the cog sends each entry as a separate message
    for proper media embedding.
    """
    media_urls_map = media_urls_map or {}
    SPLIT_MARKER = "\n---SPLIT---\n"

    parts = [header]

    for i, msg in enumerate(formatted, 1):
        content_preview = msg.get('content', '')[:100]
        if len(msg.get('content', '')) > 100:
            content_preview += "..."

        media_url = media_urls_map.get(msg['message_id'])
        channel_tag = f" in #{msg['channel']}" if msg.get('channel') else ""

        ur = msg.get('unique_reactors')
        react_str = f"{ur} unique reactors" if ur is not None else f"{msg['reactions']} reactions"
        entry = f"**{i}. {msg['author']}** — {react_str}{channel_tag}"
        if content_preview:
            entry += f"\n> {content_preview}"
        entry += f"\n`{msg['message_id']}`"

        if media_url:
            entry += f"\n{media_url}"

        parts.append(entry)

    return SPLIT_MARKER.join(parts)


async def _get_visible_channel_ids(bot: discord.Client, guild_id: int, user_id: int) -> Set[int]:
    """Return channel and active thread IDs the requester can view.

    Cached for 60s to avoid repeated Discord API lookups. Permission changes can
    take up to one cache window to propagate here.
    """
    cache_key = (guild_id, user_id)
    now = time.monotonic()
    cached = _visible_channel_cache.get(cache_key)
    if cached and now - cached[0] < _VISIBLE_CHANNEL_CACHE_TTL_SECONDS:
        return set(cached[1])

    if not bot:
        return set()

    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except Exception:
            return set()

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return set()

    visible_channel_ids: Set[int] = set()
    for channel in list(guild.channels) + list(guild.threads):
        try:
            if channel.permissions_for(member).view_channel:
                visible_channel_ids.add(channel.id)
        except Exception:
            continue

    _visible_channel_cache[cache_key] = (now, visible_channel_ids)
    return set(visible_channel_ids)


# ========== Tool Executors ==========

def execute_reply(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the reply tool - returns message(s) to send."""
    # Support both single message and array of messages
    messages = params.get('messages', [])
    single_message = params.get('message', '')

    # Handle case where messages is passed as a string instead of array
    if isinstance(messages, str):
        messages = [messages]

    if single_message and not messages:
        messages = [single_message]

    if not messages:
        return {"success": False, "error": "No message provided"}

    # Filter out empty messages
    messages = [m for m in messages if m and m.strip()]

    if not messages:
        return {"success": False, "error": "All messages were empty"}

    return {
        "success": True,
        "messages": messages  # Array of messages to send
    }


def execute_end_turn(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the end_turn tool - ends without sending a message."""
    reason = params.get('reason', 'No reason provided')
    logger.info(f"[AdminChat] End turn: {reason}")
    return {
        "success": True,
        "end_turn": True,
        "reason": reason
    }


async def execute_find_messages(
    params: Dict[str, Any],
    bot: discord.Client = None,
    visible_channels: Optional[Set[int]] = None,
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Unified message search. Delegates DB queries to discord_tools.find_messages,
    keeping the live Discord API path and LLM formatting here."""
    from scripts.discord_tools import (
        find_messages as dt_find, resolve_user as dt_resolve_user,
        _set_active_guild_id, _channel_map,
    )
    from src.common.discord_utils import refresh_media_url

    query = params.get('query', '')
    username = params.get('username', '')
    channel_id = params.get('channel_id', '')
    min_reactions = params.get('min_reactions', 0)
    has_media = params.get('has_media', False)
    days = params.get('days')  # None = all time
    limit = min(params.get('limit', 20), 100)
    sort = params.get('sort', 'date')
    do_refresh_media = params.get('refresh_media', False)
    live = params.get('live', False)

    # Resolve username to author_id upfront (used by both paths)
    author_id = None
    resolved_username = None
    if username:
        user_data = dt_resolve_user(username)
        if not user_data:
            return {"success": False, "error": f"User '{username}' not found"}
        author_id = user_data['member_id']
        resolved_username = user_data.get('username', username)

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)

        # ---- Live path: use Discord API directly ----
        if live:
            if not channel_id:
                return {"success": False, "error": "channel_id is required when live=true"}
            if not bot:
                return {"success": False, "error": "Bot not available for live queries"}

            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))
            if visible_channels is not None and channel.id not in visible_channels:
                return {"success": False, "error": "Permission denied"}

            messages = []
            need_all = sort in ('reactions', 'unique_reactors')
            fetch_limit = limit * 3 if need_all else limit * 2
            async for msg in channel.history(limit=min(fetch_limit, 500)):
                if author_id and msg.author.id != author_id:
                    continue
                if query and query.lower() not in (msg.content or '').lower():
                    continue
                total_reactions = sum(r.count for r in msg.reactions) if msg.reactions else 0
                if min_reactions and total_reactions < min_reactions:
                    continue
                if has_media and not msg.attachments:
                    continue

                messages.append({
                    "message_id": msg.id,
                    "channel_id": int(channel_id),
                    "author_name": msg.author.display_name,
                    "content": (msg.content or '')[:400],
                    "reaction_count": total_reactions,
                    "attachments": [a.url for a in msg.attachments] if msg.attachments else [],
                    "media_urls": [a.url for a in msg.attachments] if msg.attachments else None,
                    "created_at": msg.created_at.isoformat(),
                })
                if not need_all and len(messages) >= limit:
                    break

            if sort == 'unique_reactors':
                messages.sort(key=lambda m: m.get('unique_reactor_count', 0), reverse=True)
            elif sort == 'reactions':
                messages.sort(key=lambda m: m['reaction_count'], reverse=True)
            messages = messages[:limit]

        # ---- DB path: delegate to discord_tools ----
        else:
            _set_active_guild_id(resolved_guild_id)

            # Build allowed channel list (non-NSFW + visible)
            allowed_channel_ids = None
            if channel_id:
                requested_channel_id = int(channel_id)
                if visible_channels is not None and requested_channel_id not in visible_channels:
                    return {"success": False, "error": "Permission denied"}
                # channel_id is passed directly, no need for allowed list
            else:
                cmap = _channel_map(exclude_nsfw=True)
                safe_ids = set(cmap.keys())
                if visible_channels is not None:
                    safe_ids = safe_ids & visible_channels
                if not safe_ids:
                    return {
                        "success": True, "count": 0,
                        "summary": f"No messages found matching your filters.",
                        "messages": []
                    }
                allowed_channel_ids = list(safe_ids)

            messages = dt_find(
                query=query, days=days,
                channel_id=int(channel_id) if channel_id else None,
                author_id=author_id,
                min_reactions=min_reactions, has_media=has_media,
                limit=limit, sort=sort,
                exclude_nsfw=False,  # handled above via allowed_channel_ids
                allowed_channel_ids=allowed_channel_ids,
            )

        # ---- Common output for both paths ----
        if not messages:
            desc_parts = []
            if query:
                desc_parts.append(f"matching '{query}'")
            if resolved_username:
                desc_parts.append(f"from {resolved_username}")
            if min_reactions:
                desc_parts.append(f"with {min_reactions}+ reactions")
            desc = " ".join(desc_parts) or "matching your filters"
            time_desc = f"in the last {days} days" if days else "across all time"
            return {
                "success": True,
                "count": 0,
                "summary": f"No messages found {desc} {time_desc}.",
                "messages": []
            }

        # Refresh media URLs for top results if requested
        media_urls_map = {}
        if do_refresh_media and bot:
            for msg in messages[:min(limit, 20)]:
                try:
                    ch_id = msg.get('channel_id')
                    m_id = msg.get('message_id')
                    result = await refresh_media_url(bot, ch_id, m_id, logger)
                    if result and result.get('success'):
                        urls = [att['url'] for att in result.get('attachments', []) if att.get('url')]
                        if urls:
                            media_urls_map[str(m_id)] = urls[0]
                            msg['media_urls'] = urls
                except Exception as e:
                    logger.debug(f"[AdminChat] Could not refresh media for {msg.get('message_id')}: {e}")

        formatted = [format_message_for_llm(msg) for msg in messages]

        # Build header
        hit_cap = len(formatted) >= limit
        count_str = f"{len(formatted)}+" if hit_cap else str(len(formatted))
        header_parts = [f"**Found {count_str} messages"]
        if resolved_username:
            header_parts.append(f" from {resolved_username}")
        if query:
            header_parts.append(f" matching '{query}'")
        if live:
            header_parts.append(f" in <#{channel_id}>")
        if days:
            header_parts.append(f" (last {days} days)")
        else:
            header_parts.append(" (all time)")
        header_parts.append(f", sorted by {sort}")
        if hit_cap:
            header_parts.append(f" (showing top {limit}, use limit param for more)")
        header_parts.append(":**")

        summary = _build_summary(formatted, "".join(header_parts), media_urls_map)

        return {
            "success": True,
            "count": len(formatted),
            "summary": summary,
            "messages": formatted
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in find_messages: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_inspect_message(
    params: Dict[str, Any],
    bot: discord.Client = None,
    visible_channels: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """Deep look at one message: content, reactions, context, replies, fresh media."""
    from scripts.discord_tools import context as dt_context, _set_active_guild_id
    from src.common.discord_utils import refresh_media_url

    message_id = params.get('message_id', '')
    context_size = params.get('context_size', 3)

    if not message_id:
        return {"success": False, "error": "message_id is required"}

    try:
        resolved_guild_id = _resolve_guild_id(params)
        if resolved_guild_id:
            _set_active_guild_id(resolved_guild_id)

        # Get message + context from DB via discord_tools
        ctx = dt_context(int(message_id), surrounding=context_size)

        if ctx.get('error'):
            return {"success": False, "error": ctx['error']}

        target = ctx.get('target', {})
        replies = ctx.get('replies', [])
        before = ctx.get('before', [])
        after = ctx.get('after', [])

        # Try to get live data from Discord API (fresh URLs + reaction detail)
        live_reactions = []
        media_urls = []
        channel_id = target.get('channel_id')
        if visible_channels is not None and channel_id not in visible_channels:
            return {"success": False, "error": "Permission denied"}

        if bot and channel_id:
            try:
                channel = bot.get_channel(channel_id)
                if not channel:
                    channel = await bot.fetch_channel(channel_id)

                # Handle ForumChannel
                if isinstance(channel, discord.ForumChannel):
                    try:
                        thread = await bot.fetch_channel(int(message_id))
                        if isinstance(thread, discord.Thread):
                            channel = thread
                    except Exception:
                        pass

                if hasattr(channel, 'fetch_message'):
                    live_msg = await channel.fetch_message(int(message_id))

                    # Fresh reaction detail
                    for r in live_msg.reactions:
                        live_reactions.append({
                            "emoji": str(r.emoji),
                            "count": r.count
                        })

                    # Fresh attachment URLs
                    for att in live_msg.attachments:
                        media_urls.append({
                            "filename": att.filename,
                            "url": att.url,
                            "content_type": att.content_type,
                        })
            except Exception as e:
                logger.debug(f"[AdminChat] Could not fetch live message {message_id}: {e}")

        # Format target
        formatted_target = format_message_for_llm(target, include_link=True)
        # Override with full content (not truncated)
        formatted_target["content"] = (target.get('content', '') or '')

        # Format replies
        formatted_replies = [
            {
                "author": r.get('author_name', 'Unknown'),
                "content": (r.get('content', '') or '')[:200],
                "message_id": str(r.get('message_id', '')),
                "reactions": r.get('reaction_count', 0),
            }
            for r in replies[:10]
        ]

        # Format surrounding context
        formatted_before = [
            {"author": m.get('author_name', 'Unknown'), "content": (m.get('content', '') or '')[:150]}
            for m in before
        ]
        formatted_after = [
            {"author": m.get('author_name', 'Unknown'), "content": (m.get('content', '') or '')[:150]}
            for m in after
        ]

        # Build summary using ---SPLIT--- so the cog sends media URLs as separate messages
        SPLIT = "\n---SPLIT---\n"
        total_reactions = sum(r['count'] for r in live_reactions) if live_reactions else target.get('reaction_count', 0)

        # First part: message info
        info_lines = [f"**Message by {formatted_target['author']}** — {total_reactions} reactions"]
        if formatted_target['content']:
            info_lines.append(f"> {formatted_target['content'][:500]}")
        else:
            info_lines.append("*(no text content)*")
        info_lines.append(formatted_target.get('link', ''))
        if live_reactions:
            reaction_str = "  ".join(f"{r['emoji']} {r['count']}" for r in live_reactions)
            info_lines.append(reaction_str)
        if formatted_replies:
            info_lines.append(f"\n**Replies** ({len(formatted_replies)})")
            for r in formatted_replies[:5]:
                reply_preview = r['content'][:100] + ("..." if len(r['content']) > 100 else "")
                info_lines.append(f"> **{r['author']}:** {reply_preview}")

        parts = ["\n".join(info_lines)]

        # Each media URL as its own split part so it embeds properly
        for m in media_urls:
            parts.append(m['url'])

        return {
            "success": True,
            "message": formatted_target,
            "reactions": live_reactions,
            "media": media_urls,
            "replies": formatted_replies,
            "reply_count": len(replies),
            "context_before": formatted_before,
            "context_after": formatted_after,
            "summary": SPLIT.join(parts),
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in inspect_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_share_to_social(
    bot: discord.Client,
    sharer,
    params: Dict[str, Any],
    guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute the share_to_social tool through the unified social publish service."""

    message_link = params.get('message_link', '')
    message_id = params.get('message_id', '')
    media_urls = params.get('media_urls') or []
    target_post_input = params.get('target_post')
    raw_reply_to_tweet = params.get('reply_to_tweet')
    raw_action = str(params.get('action', '') or '').strip().lower()
    raw_route_override = params.get('route_key')
    if raw_route_override in (None, ''):
        raw_route_override = params.get('route_override')
    target_post_id = None
    reply_to_tweet_id = None

    target_input = target_post_input if target_post_input not in (None, '') else raw_reply_to_tweet

    if target_input not in (None, ''):
        reply_value = str(target_input).strip()
        status_match = re.search(r'status/(\d+)', reply_value)
        if status_match:
            target_post_id = status_match.group(1)
        elif reply_value.isdigit():
            target_post_id = reply_value
        else:
            return {
                "success": False,
                "error": "target_post/reply_to_tweet must be a Tweet ID or a tweet URL containing status/<digits>"
            }

    if raw_action and raw_action not in {"post", "reply", "retweet", "quote"}:
        return {"success": False, "error": "action must be one of: post, reply, retweet, quote"}

    if raw_action:
        action = raw_action
    elif raw_reply_to_tweet not in (None, '') or target_post_id:
        action = 'reply'
    else:
        action = 'post'

    if action in {'reply', 'retweet', 'quote'} and not target_post_id:
        return {"success": False, "error": f"action={action} requires target_post or reply_to_tweet"}
    if action == 'post' and target_post_id:
        return {"success": False, "error": "target_post/reply_to_tweet is only valid for reply, retweet, or quote actions"}

    reply_to_tweet_id = target_post_id if action == 'reply' else None

    # Default text_only=True for thread replies so a follow-up doesn't
    # reattach the parent tweet's media. Caller can override with explicit false.
    raw_text_only = params.get('text_only')
    if raw_text_only is None:
        text_only = action == 'reply'
    else:
        text_only = bool(raw_text_only)

    platform = str(params.get('platform') or 'twitter').strip().lower()
    if platform == 'x':
        platform = 'twitter'
    if platform not in {'twitter', 'youtube'}:
        return {"success": False, "error": f"Unsupported platform: {platform}"}
    if platform == 'youtube' and action != 'post':
        return {"success": False, "error": "YouTube only supports action=post"}

    route_override = None
    if raw_route_override not in (None, ''):
        route_override = {'route_key': str(raw_route_override).strip()}

    scheduled_at = None
    raw_schedule_for = params.get('schedule_for')
    if raw_schedule_for not in (None, ''):
        schedule_value = str(raw_schedule_for).strip().replace('Z', '+00:00')
        try:
            scheduled_at = datetime.fromisoformat(schedule_value)
        except ValueError:
            return {"success": False, "error": "schedule_for must be a valid ISO-8601 timestamp"}
        if scheduled_at.tzinfo is None:
            return {"success": False, "error": "schedule_for must include a timezone offset or Z suffix"}
        scheduled_at = scheduled_at.astimezone(timezone.utc)

    tweet_text = params.get('tweet_text', '').strip() or None

    # Parse link or use direct ID
    if message_link:
        parsed = parse_message_link(message_link)
        if not parsed:
            return {"success": False, "error": "Invalid message link format"}
        channel_id = parsed['channel_id']
        message_id = parsed['message_id']
    elif message_id:
        # Need to find the channel - search in DB
        from scripts.discord_tools import get_message as dt_get_message
        msg_data = dt_get_message(int(message_id))
        if not msg_data:
            return {"success": False, "error": f"Message {message_id} not found in database"}
        channel_id = msg_data['channel_id']
        message_id = int(message_id)
    else:
        if not isinstance(media_urls, list):
            return {"success": False, "error": "media_urls must be an array of URL strings"}

        direct_media_urls = [str(url).strip() for url in media_urls if str(url).strip()]
        if action == 'retweet':
            if tweet_text:
                return {"success": False, "error": "tweet_text is not supported for retweet actions"}
            if direct_media_urls:
                return {"success": False, "error": "media_urls is not supported for retweet actions"}
        elif not tweet_text:
            return {
                "success": False,
                "error": "tweet_text is required for post or reply actions without message_link or message_id",
            }

        if guild_id is None:
            guild_id = int(os.getenv('GUILD_ID', 0)) or None
        if guild_id is None:
            return {"success": False, "error": "guild_id is required for direct social posts"}

        social_publish_service = getattr(sharer, 'social_publish_service', None)
        if social_publish_service is None:
            return {"success": False, "error": "Social publish service is not available"}

        downloaded_attachments = []
        preserve_downloads = False
        try:
            for index, url in enumerate(direct_media_urls):
                downloaded_item = await sharer._download_media_from_url(url, 'direct', index)
                if not downloaded_item or not downloaded_item.get('local_path'):
                    return {"success": False, "error": f"Failed to download media from URL: {url}"}
                downloaded_attachments.append(downloaded_item)

            request = SocialPublishRequest(
                message_id=0,
                channel_id=0,
                guild_id=guild_id,
                user_id=0,
                platform=platform,
                action=action,
                scheduled_at=scheduled_at,
                target_post_ref=target_post_id,
                route_override=route_override or {'route_key': 'direct'},
                text=tweet_text if action != 'retweet' else None,
                media_hints=downloaded_attachments,
                source_kind='admin_chat',
                duplicate_policy={'check_existing': False},
                text_only=not downloaded_attachments,
                announce_policy={'enabled': False},
                first_share_notification_policy={'enabled': False},
                legacy_shared_post_policy={'enabled': False},
                source_context=PublicationSourceContext(
                    source_kind='admin_chat',
                    metadata={
                        'user_details': {'direct_post': True},
                        'guild_id': guild_id,
                    },
                ),
            )

            if scheduled_at is not None:
                result = await social_publish_service.enqueue(request)
                if not result.success:
                    return {"success": False, "error": result.error or "Scheduling failed"}
                preserve_downloads = True
                return {
                    "success": True,
                    "message": f"Scheduled {action} for {scheduled_at.isoformat()}",
                    "tweet_url": None,
                    "tweet_id": None,
                    "publication_id": result.publication_id,
                    "status": "queued",
                    "already_shared": False,
                }

            result = await social_publish_service.publish_now(request)
            if not result.success:
                return {"success": False, "error": result.error or "Sharing failed"}

            tweet_url = getattr(result, 'tweet_url', None)
            tweet_id = getattr(result, 'tweet_id', None)
            provider_url = getattr(result, 'provider_url', None) or tweet_url
            provider_ref = getattr(result, 'provider_ref', None) or tweet_id
            publication_id = getattr(result, 'publication_id', None)

            response_message = _social_posted_message(platform, action, provider_url)

            return {
                "success": True,
                "message": response_message,
                "tweet_url": tweet_url,
                "tweet_id": tweet_id,
                "provider_url": provider_url,
                "provider_ref": provider_ref,
                "publication_id": publication_id,
                "already_shared": False,
            }
        finally:
            if not preserve_downloads:
                sharer._cleanup_files([
                    att['local_path']
                    for att in downloaded_attachments
                    if att.get('local_path')
                ])

    try:
        social_publish_service = getattr(sharer, 'social_publish_service', None)
        if social_publish_service is None:
            return {"success": False, "error": "Social publish service is not available"}

        channel = bot.get_channel(channel_id)
        if channel is None:
            return {"success": False, "error": f"Could not find channel {channel_id}"}

        if isinstance(channel, discord.ForumChannel) or not hasattr(channel, 'fetch_message'):
            resolved_channel = None
            guild = getattr(channel, 'guild', None)
            if guild:
                resolved_channel = guild.get_thread(int(message_id))

            if resolved_channel is None:
                try:
                    fetched_channel = await bot.fetch_channel(int(message_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched_channel = None

                if isinstance(fetched_channel, discord.Thread):
                    resolved_channel = fetched_channel

            if resolved_channel is None or not hasattr(resolved_channel, 'fetch_message'):
                return {"success": False, "error": f"Could not resolve thread for forum post {message_id}"}

            channel = resolved_channel

        message = await channel.fetch_message(message_id)
        if not message:
            return {"success": False, "error": f"Could not find message {message_id}"}

        guild_id = getattr(message.guild, 'id', None)
        if guild_id is None:
            return {"success": False, "error": "This message is not in a server-backed channel"}

        if action == 'retweet' and tweet_text:
            return {"success": False, "error": "tweet_text is not supported for retweet actions"}
        logger.info(
            f"[AdminChat] Triggering share for message {message_id} by user {message.author.id}" +
            (f" with custom tweet: '{tweet_text[:80]}...'" if tweet_text else "") +
            (f" with action={action}" if action else "") +
            (f" targeting tweet {target_post_id}" if target_post_id else "") +
            (f" scheduled for {scheduled_at.isoformat()}" if scheduled_at else "")
        )

        user_details = sharer.db_handler.get_member(message.author.id)
        if not user_details:
            return {"success": False, "error": f"Could not load member {message.author.id} from the database"}

        existing_publication = sharer._find_existing_publication(
            message_id=message.id,
            guild_id=guild_id,
            platform=platform,
            action=action,
            source_kind='admin_chat',
        )
        if existing_publication:
            provider_url = existing_publication.get('provider_url')
            provider_ref = existing_publication.get('provider_ref')
            return {
                "success": True,
                "message": f"Already shared: {provider_url}",
                "tweet_url": provider_url if platform == 'twitter' else None,
                "tweet_id": provider_ref if platform == 'twitter' else None,
                "provider_url": provider_url,
                "provider_ref": provider_ref,
                "publication_id": existing_publication.get('publication_id'),
                "already_shared": True,
            }

        downloaded_attachments = []
        if not text_only and action != 'retweet':
            for attachment in message.attachments:
                downloaded_item = await sharer._download_attachment(attachment)
                if downloaded_item:
                    downloaded_attachments.append(downloaded_item)

        twitter_content = None
        if action != 'retweet':
            if tweet_text:
                twitter_content = tweet_text
            else:
                twitter_content = f"Check out this post by {message.author.display_name}! {message.jump_url}"

        request = SocialPublishRequest(
            message_id=message.id,
            channel_id=channel.id,
            guild_id=guild_id,
            user_id=message.author.id,
            platform=platform,
            action=action,
            scheduled_at=scheduled_at,
            target_post_ref=target_post_id,
            route_override=route_override,
            text=twitter_content,
            media_hints=downloaded_attachments,
            source_kind='admin_chat',
            duplicate_policy={'check_existing': action == 'post'},
            text_only=text_only,
            announce_policy={
                'enabled': True,
                'author_display_name': message.author.display_name,
                'original_message_jump_url': message.jump_url,
            },
            first_share_notification_policy={'enabled': True},
            legacy_shared_post_policy={'enabled': action == 'post', 'delete_eligible_hours': 6},
            source_context=PublicationSourceContext(
                source_kind='admin_chat',
                metadata={
                    'user_details': user_details,
                    'original_content': message.content,
                    'author_display_name': message.author.display_name,
                    'original_message_jump_url': message.jump_url,
                    'guild_id': guild_id,
                },
            ),
        )

        if scheduled_at is not None:
            result = await social_publish_service.enqueue(request)
            if not result.success:
                return {"success": False, "error": result.error or "Scheduling failed"}
            return {
                "success": True,
                "message": f"Scheduled {action} for {scheduled_at.isoformat()}",
                "publication_id": result.publication_id,
                "status": "queued",
                "already_shared": False,
            }

        try:
            result = await social_publish_service.publish_now(request)
        finally:
            sharer._cleanup_files([
                att['local_path']
                for att in downloaded_attachments
                if att.get('local_path')
            ])

        if not result.success:
            return {"success": False, "error": result.error or "Sharing failed"}

        tweet_url = getattr(result, 'tweet_url', None)
        tweet_id = getattr(result, 'tweet_id', None)
        provider_url = getattr(result, 'provider_url', None) or tweet_url
        provider_ref = getattr(result, 'provider_ref', None) or tweet_id
        publication_id = getattr(result, 'publication_id', None)

        if platform == 'twitter' and tweet_url:
            await sharer._announce_tweet_url(
                tweet_url,
                message.author.display_name,
                message.jump_url,
                str(message.id),
                guild_id=guild_id,
                is_reply=(action == 'reply'),
            )

        from src.features.sharing.subfeatures.notify_user import send_post_share_notification

        is_first_share = platform == 'twitter' and sharer.db_handler.mark_member_first_shared(message.author.id, guild_id=guild_id)
        if is_first_share:
            await send_post_share_notification(
                bot=bot,
                user=message.author,
                discord_message=message,
                publication_id=publication_id,
                tweet_id=tweet_id,
                tweet_url=tweet_url,
                db_handler=sharer.db_handler,
            )

        response_message = _social_posted_message(platform, action, provider_url)

        return {
            "success": True,
            "message": response_message,
            "tweet_url": tweet_url,
            "tweet_id": tweet_id,
            "provider_url": provider_url,
            "provider_ref": provider_ref,
            "publication_id": publication_id,
            "already_shared": False,
        }

    except discord.NotFound:
        return {"success": False, "error": f"Message {message_id} not found"}
    except discord.Forbidden:
        return {"success": False, "error": "Bot doesn't have permission to access that channel/message"}
    except Exception as e:
        logger.error(f"[AdminChat] Error in share_to_social: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_active_channels(
    params: Dict[str, Any],
    visible_channels: Optional[Set[int]] = None,
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Get list of active channels."""
    from scripts.discord_tools import channels as dt_channels, _set_active_guild_id

    days = params.get('days', 7)

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)
        if resolved_guild_id:
            _set_active_guild_id(resolved_guild_id)

        chs = dt_channels(days=days)

        # Apply visible_channels filter
        if visible_channels is not None:
            chs = [ch for ch in chs if ch['channel_id'] in visible_channels]

        # Format for LLM (top 20)
        formatted = [
            {
                "channel_id": str(ch['channel_id']),
                "name": ch['channel_name'],
                "messages": ch['messages']
            }
            for ch in chs[:20]
        ]

        return {
            "success": True,
            "count": len(formatted),
            "channels": formatted
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_active_channels: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_daily_summaries(
    params: Dict[str, Any],
    visible_channels: Optional[Set[int]] = None,
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Get daily summaries."""
    from collections import defaultdict
    from scripts.discord_tools import summaries as dt_summaries, _set_active_guild_id

    days = params.get('days', 7)
    channel_id = params.get('channel_id')

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)
        if resolved_guild_id:
            _set_active_guild_id(resolved_guild_id)

        # Permission check for specific channel
        if channel_id and visible_channels is not None:
            if int(channel_id) not in visible_channels:
                return {"success": False, "error": "Permission denied"}

        rows = dt_summaries(days=days, channel_id=int(channel_id) if channel_id else None)

        # Apply visible_channels filter
        if visible_channels is not None:
            rows = [row for row in rows if int(row['channel_id']) in visible_channels]

        by_date = defaultdict(list)
        for r in rows:
            by_date[r['date']].append(r)

        summary_lines = []
        for date in sorted(by_date.keys(), reverse=True):
            items = by_date[date]
            summary_lines.append(f"\n**{date}** ({len(items)} channels)")
            for item in items:
                s = (item.get('short_summary') or '')[:300]
                summary_lines.append(f"  [{item['channel_id']}] {s}")

        return {
            "success": True,
            "days": days,
            "summary": "\n".join(summary_lines),
            "total_summaries": len(rows)
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in get_daily_summaries: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_live_update_status(
    params: Dict[str, Any],
    resolved_guild_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Inspect active topic-editor state with legacy rollback visibility."""
    hours = min(int(params.get('hours') or 24), 168)
    limit = min(int(params.get('limit') or 5), 25)
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    def _recent(sb, table: str, select_cols: str, order_col: str = 'created_at') -> List[Dict[str, Any]]:
        query = sb.table(table).select(select_cols)
        if resolved_guild_id:
            query = query.eq('guild_id', resolved_guild_id)
        return query.gte(order_col, cutoff).order(order_col, desc=True).limit(limit).execute().data or []

    def _count(
        sb,
        table: str,
        since_col: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> int:
        query = sb.table(table).select('*', count='exact').limit(1)
        if resolved_guild_id:
            query = query.eq('guild_id', resolved_guild_id)
        for key, value in (filters or {}).items():
            query = query.eq(key, value)
        if since_col:
            query = query.gte(since_col, cutoff)
        result = query.execute()
        return result.count if result.count is not None else len(result.data or [])

    try:
        resolved_guild_id = resolved_guild_id or _resolve_guild_id(params)
        sb = _get_supabase()

        runs = _recent(
            sb,
            'topic_editor_runs',
            'run_id,status,trigger,started_at,completed_at,source_message_count,tool_call_count,accepted_count,rejected_count,override_count,observation_count,published_count,failed_publish_count,input_tokens,output_tokens,cost_usd,latency_ms,publishing_enabled,error_message',
            order_col='started_at',
        )
        transitions = _recent(
            sb,
            'topic_transitions',
            'transition_id,topic_id,run_id,action,from_state,to_state,reason,payload,created_at',
        )
        recent_topics = _recent(
            sb,
            'topics',
            'topic_id,state,headline,canonical_key,publication_status,publication_error,discord_message_ids,publication_attempts,last_published_at,updated_at',
            order_col='updated_at',
        )
        observations = _recent(
            sb,
            'editorial_observations',
            'observation_id,run_id,observation_kind,reason,source_message_ids,created_at',
        )
        rejected_transitions = [
            row for row in transitions if str(row.get('action') or '').startswith('rejected_')
        ]
        override_transitions = [row for row in transitions if row.get('action') == 'override']
        decision_transition_count = sum(
            1 for row in transitions
            if row.get('action') not in {None, 'observation'}
        )
        override_rate = (
            len(override_transitions) / decision_transition_count
            if decision_transition_count else 0.0
        )

        topic_counts = {
            "posted": _count(sb, 'topics', filters={'state': 'posted'}),
            "watching": _count(sb, 'topics', filters={'state': 'watching'}),
            "discarded": _count(sb, 'topics', filters={'state': 'discarded'}),
            "recent_updated": _count(sb, 'topics', since_col='updated_at'),
        }
        publication_problem_topics = [
            row for row in recent_topics
            if row.get('publication_status') in {'failed', 'partial'}
        ]
        recent_failed_runs = [
            row for row in runs
            if row.get('status') == 'failed' or int(row.get('failed_publish_count') or 0) > 0
        ]

        legacy_runs = _recent(
            sb,
            'live_update_editor_runs',
            'run_id,status,trigger,created_at,completed_at,candidate_count,accepted_count,error_message',
        )
        legacy_feed_count = _count(sb, 'live_update_feed_items', since_col='created_at')
        legacy_watchlist_count = _count(sb, 'live_update_watchlist')
        legacy_duplicate_count = _count(sb, 'live_update_duplicate_state')

        latest_run = runs[0] if runs else None
        summary_lines = [
            f"Topic-editor primary status for last {hours}h",
            f"Recent topic_editor_runs: {len(runs)}",
            "Topics: posted={posted} watching={watching} discarded={discarded} recent_updated={recent_updated}".format(**topic_counts),
            f"Recent transitions: {len(transitions)} rejections={len(rejected_transitions)} overrides={len(override_transitions)} override_rate={override_rate:.1%}",
            f"Recent observations: {len(observations)}",
            f"Failed/partial publications: {len(publication_problem_topics)} topics; runs with failures={len(recent_failed_runs)}",
        ]
        if latest_run:
            summary_lines.append(
                "Latest topic run: {status} trigger={trigger} sources={sources} tools={tools} accepted={accepted} rejected={rejected} published={published} failed_publish={failed_publish}".format(
                    status=latest_run.get('status'),
                    trigger=latest_run.get('trigger'),
                    sources=latest_run.get('source_message_count'),
                    tools=latest_run.get('tool_call_count'),
                    accepted=latest_run.get('accepted_count'),
                    rejected=latest_run.get('rejected_count'),
                    published=latest_run.get('published_count'),
                    failed_publish=latest_run.get('failed_publish_count'),
                )
            )
        if rejected_transitions:
            summary_lines.append("Recent rejections:")
            for item in rejected_transitions[:3]:
                summary_lines.append(
                    f"- {item.get('action')} topic={item.get('topic_id') or 'n/a'} reason={item.get('reason') or ''}"
                )
        if transitions:
            summary_lines.append("Recent transitions:")
            for item in transitions[:3]:
                summary_lines.append(
                    f"- {item.get('action')} {item.get('from_state') or '-'}->{item.get('to_state') or '-'} topic={item.get('topic_id') or 'n/a'}"
                )
        if publication_problem_topics:
            summary_lines.append("Publication problems:")
            for item in publication_problem_topics[:3]:
                summary_lines.append(
                    f"- {item.get('headline') or item.get('topic_id')}: {item.get('publication_status')} {item.get('publication_error') or ''}".strip()
                )
        summary_lines.append(
            "Rollback legacy live-update state only: runs={runs} recent_feed_items={feed} watchlist={watchlist} duplicate_state={duplicates}".format(
                runs=len(legacy_runs),
                feed=legacy_feed_count,
                watchlist=legacy_watchlist_count,
                duplicates=legacy_duplicate_count,
            )
        )

        return {
            "success": True,
            "guild_id": resolved_guild_id,
            "hours": hours,
            "summary": "\n".join(summary_lines),
            "runs": runs,
            "topics": recent_topics,
            "topic_counts": topic_counts,
            "transitions": transitions,
            "rejections": rejected_transitions,
            "observations": observations,
            "publication_problems": publication_problem_topics,
            "override_rate": override_rate,
            "state_counts": {
                **topic_counts,
                "recent_transitions": len(transitions),
                "recent_rejections": len(rejected_transitions),
                "recent_overrides": len(override_transitions),
                "recent_observations": len(observations),
                "publication_problems": len(publication_problem_topics),
                "failed_or_partial_publication_runs": len(recent_failed_runs),
            },
            "legacy_rollback_state": {
                "runs": legacy_runs,
                "recent_feed_items": legacy_feed_count,
                "watchlist": legacy_watchlist_count,
                "duplicate_state": legacy_duplicate_count,
            },
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in get_live_update_status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_member_info(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Get member information from the database."""

    user_id = params.get('user_id')
    username = params.get('username')

    if not user_id and not username:
        return {"success": False, "error": "Provide either user_id or username"}

    try:
        if user_id:
            member = db_handler.get_member(int(user_id))
        else:
            result = db_handler._run_async_in_thread(
                db_handler.storage_handler.supabase_client.table('members')
                .select('*')
                .ilike('username', f'%{username}%')
                .limit(5)
                .execute
            )
            if result.data:
                if len(result.data) > 1:
                    usernames = [m.get('username', 'unknown') for m in result.data]
                    return {"success": False, "error": f"Multiple matches: {', '.join(usernames)}. Use user_id for exact match."}
                member = result.data[0]
            else:
                member = None

        if not member:
            return {"success": False, "error": f"No member found"}

        return {
            "success": True,
            "member": {
                "id": member.get('member_id'),
                "username": member.get('username'),
                "display_name": member.get('global_name') or member.get('server_nick'),
                "include_in_updates": member.get('include_in_updates'),
                "allow_content_sharing": member.get('allow_content_sharing'),
                "first_shared_at": member.get('first_shared_at'),
                "twitter_url": member.get('twitter_url'),
                "reddit_url": member.get('reddit_url'),
            }
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_member_info: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_list_social_routes(params: Dict[str, Any]) -> Dict[str, Any]:
    """List configured social routes for the active guild."""
    limit = min(int(params.get('limit', 50)), 100)
    try:
        guild_id = _resolve_guild_id(params)
        platform = _normalize_social_platform(params.get('platform'))
        channel_id = None
        if params.get('channel_id') not in (None, ''):
            channel_id = _parse_optional_channel_id(params.get('channel_id'), allow_clear=False)

        sb = _get_supabase()
        query = (
            sb.table('social_channel_routes')
            .select('*')
            .eq('guild_id', guild_id)
            .eq('platform', platform)
        )
        if channel_id is None:
            pass
        else:
            query = query.eq('channel_id', channel_id)
        if 'enabled' in params and params.get('enabled') is not None:
            query = query.eq('enabled', _coerce_bool(params.get('enabled'), 'enabled'))

        result = query.limit(limit).execute()
        return {
            "success": True,
            "count": len(result.data or []),
            "data": result.data or [],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in list_social_routes: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_create_social_route(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a social route row."""
    try:
        guild_id = _resolve_guild_id(params)
        platform = _normalize_social_platform(params.get('platform'))
        payload = {
            'guild_id': guild_id,
            'platform': platform,
            'channel_id': _parse_optional_channel_id(params.get('channel_id')),
            'enabled': _coerce_bool(params['enabled'], 'enabled') if 'enabled' in params else True,
            'route_config': _normalize_social_route_config(params.get('route_config'), platform=platform),
        }
        result = _get_supabase().table('social_channel_routes').insert(payload).execute()
        return {
            "success": True,
            "message": "Social route created",
            "route": result.data[0] if result.data else payload,
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in create_social_route: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_update_social_route(params: Dict[str, Any]) -> Dict[str, Any]:
    """Update one social route row."""
    route_id = str(params.get('route_id') or '').strip()
    if not route_id:
        return {"success": False, "error": "route_id is required"}

    try:
        guild_id = _resolve_guild_id(params)
        updates: Dict[str, Any] = {}
        platform = _normalize_social_platform(params.get('platform'))
        existing_route = None
        if 'platform' in params:
            updates['platform'] = platform
        if 'channel_id' in params:
            updates['channel_id'] = _parse_optional_channel_id(params.get('channel_id'))
        if 'enabled' in params:
            updates['enabled'] = _coerce_bool(params.get('enabled'), 'enabled')
        if 'route_config' in params:
            if 'platform' not in params:
                existing_result = (
                    _get_supabase().table('social_channel_routes')
                    .select('platform')
                    .eq('id', route_id)
                    .eq('guild_id', guild_id)
                    .limit(1)
                    .execute()
                )
                existing_route = (existing_result.data or [None])[0]
                platform = _normalize_social_platform((existing_route or {}).get('platform'))
            updates['route_config'] = _normalize_social_route_config(params.get('route_config'), platform=platform)

        if not updates:
            return {"success": False, "error": "Provide at least one field to update"}

        result = (
            _get_supabase().table('social_channel_routes')
            .update(updates)
            .eq('guild_id', guild_id)
            .eq('id', route_id)
            .execute()
        )
        if not result.data:
            return {"success": False, "error": f"Route {route_id} not found"}
        return {
            "success": True,
            "message": "Social route updated",
            "route": result.data[0],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in update_social_route: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_delete_social_route(params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete one social route row."""
    route_id = str(params.get('route_id') or '').strip()
    if not route_id:
        return {"success": False, "error": "route_id is required"}

    try:
        guild_id = _resolve_guild_id(params)
        result = (
            _get_supabase().table('social_channel_routes')
            .delete()
            .eq('guild_id', guild_id)
            .eq('id', route_id)
            .execute()
        )
        if not result.data:
            return {"success": False, "error": f"Route {route_id} not found"}
        return {
            "success": True,
            "message": "Social route deleted",
            "route": result.data[0],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in delete_social_route: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_list_payment_routes(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """List configured payment routes for the active guild."""
    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        channel_id = None
        if params.get('channel_id') not in (None, ''):
            channel_id = _parse_optional_channel_id(params.get('channel_id'), allow_clear=False)
        producer = None
        if params.get('producer') not in (None, ''):
            producer = _normalize_payment_producer(params.get('producer'))
        enabled = params.get('enabled') if 'enabled' in params else None
        if enabled is not None:
            enabled = _coerce_bool(enabled, 'enabled')

        rows = db_handler.list_payment_routes(
            guild_id=guild_id,
            producer=producer,
            channel_id=channel_id,
            enabled=enabled,
            limit=min(int(params.get('limit', 50)), 100),
        )
        return {"success": True, "count": len(rows), "data": rows}
    except Exception as e:
        logger.error(f"[AdminChat] Error in list_payment_routes: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_create_payment_route(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a payment route row."""
    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        route = db_handler.create_payment_route(
            {
                'guild_id': guild_id,
                'producer': _normalize_payment_producer(params.get('producer')),
                'channel_id': _parse_optional_channel_id(params.get('channel_id')),
                'enabled': _coerce_bool(params['enabled'], 'enabled') if 'enabled' in params else True,
                'route_config': _normalize_route_config(params.get('route_config')),
            },
            guild_id=guild_id,
        )
        if not route:
            return {"success": False, "error": "Failed to create payment route"}
        return {"success": True, "message": "Payment route created", "route": route}
    except Exception as e:
        logger.error(f"[AdminChat] Error in create_payment_route: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_update_payment_route(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Update one payment route row."""
    route_id = str(params.get('route_id') or '').strip()
    if not route_id:
        return {"success": False, "error": "route_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        updates: Dict[str, Any] = {}
        if 'producer' in params:
            updates['producer'] = _normalize_payment_producer(params.get('producer'))
        if 'channel_id' in params:
            updates['channel_id'] = _parse_optional_channel_id(params.get('channel_id'))
        if 'enabled' in params:
            updates['enabled'] = _coerce_bool(params.get('enabled'), 'enabled')
        if 'route_config' in params:
            updates['route_config'] = _normalize_route_config(params.get('route_config'))

        if not updates:
            return {"success": False, "error": "Provide at least one field to update"}

        route = db_handler.update_payment_route(route_id, updates, guild_id=guild_id)
        if not route:
            return {"success": False, "error": f"Route {route_id} not found"}
        return {"success": True, "message": "Payment route updated", "route": route}
    except Exception as e:
        logger.error(f"[AdminChat] Error in update_payment_route: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_delete_payment_route(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete one payment route row."""
    route_id = str(params.get('route_id') or '').strip()
    if not route_id:
        return {"success": False, "error": "route_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        route = db_handler.delete_payment_route(route_id, guild_id=guild_id)
        if not route:
            return {"success": False, "error": f"Route {route_id} not found"}
        return {"success": True, "message": "Payment route deleted", "route": route}
    except Exception as e:
        logger.error(f"[AdminChat] Error in delete_payment_route: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_list_wallets(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """List wallet registry rows with redacted addresses."""
    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        chain = params.get('chain')
        discord_user_id = params.get('discord_user_id')
        if discord_user_id not in (None, ''):
            discord_user_id = int(discord_user_id)
        verified = params.get('verified') if 'verified' in params else None
        if verified is not None:
            verified = _coerce_bool(verified, 'verified')

        rows = _list_wallet_rows(
            db_handler,
            guild_id=guild_id,
            chain=chain,
            discord_user_id=discord_user_id,
            verified=verified,
            limit=min(int(params.get('limit', 50)), 100),
        )
        return {
            "success": True,
            "count": len(rows),
            "data": [_redact_wallet_row(row) for row in rows],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in list_wallets: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_list_payments(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """List payment ledger rows with redacted wallet addresses."""
    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        producer = None
        if params.get('producer') not in (None, ''):
            producer = _normalize_payment_producer(params.get('producer'))
        recipient_discord_id = params.get('recipient_discord_id')
        if recipient_discord_id not in (None, ''):
            recipient_discord_id = int(recipient_discord_id)
        is_test = params.get('is_test') if 'is_test' in params else None
        if is_test is not None:
            is_test = _coerce_bool(is_test, 'is_test')

        rows = db_handler.list_payment_requests(
            guild_id=guild_id,
            status=params.get('status'),
            producer=producer,
            recipient_discord_id=recipient_discord_id,
            wallet_id=params.get('wallet_id'),
            is_test=is_test,
            route_key=params.get('route_key'),
            limit=min(int(params.get('limit', 50)), 100),
        )
        return {
            "success": True,
            "count": len(rows),
            "data": [_redact_payment_row(row) for row in rows],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in list_payments: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_payment_status(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch one payment row by id with redacted wallet details."""
    payment_id = str(params.get('payment_id') or '').strip()
    if not payment_id:
        return {"success": False, "error": "payment_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
        if not row:
            return {"success": False, "error": f"Payment {payment_id} not found"}
        return {"success": True, "payment": _redact_payment_row(row)}
    except Exception as e:
        logger.error(f"[AdminChat] Error in get_payment_status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_retry_payment(bot, db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Retry one failed payment."""
    configured_admin_user_id, admin_error = _require_injected_admin_user_id(params)
    if admin_error:
        return {"success": False, "error": admin_error}

    payment_id = str(params.get('payment_id') or '').strip()
    if not payment_id:
        return {"success": False, "error": "payment_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        payment_service = getattr(bot, 'payment_service', None)
        if payment_service is None:
            return {"success": False, "error": "payment_service unavailable"}

        decision = await payment_service.reconcile_with_chain(payment_id, guild_id=guild_id)
        if decision.decision in {'reconciled_confirmed', 'reconciled_failed'}:
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            return {
                "success": True,
                "decision": decision.decision,
                "payment": _redact_payment_row(row or {"payment_id": payment_id}),
            }
        if decision.decision == 'keep_in_hold':
            db_handler.mark_payment_manual_hold(payment_id, reason=decision.reason, guild_id=guild_id)
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            return {
                "success": False,
                "error": decision.reason,
                "payment": _redact_payment_row(row or {"payment_id": payment_id}),
            }
        if decision.decision == 'not_applicable':
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            return {
                "success": False,
                "error": decision.reason,
                "payment": _redact_payment_row(row or {"payment_id": payment_id}),
            }

        success = db_handler.requeue_payment(payment_id, guild_id=guild_id)
        if not success:
            return {"success": False, "error": "Payment is not in a retryable failed state"}
        row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
        return {"success": True, "payment": _redact_payment_row(row or {"payment_id": payment_id})}
    except Exception as e:
        logger.error(f"[AdminChat] Error in retry_payment: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_hold_payment(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Force one payment into manual_hold."""
    configured_admin_user_id, admin_error = _require_injected_admin_user_id(params)
    if admin_error:
        return {"success": False, "error": admin_error}

    payment_id = str(params.get('payment_id') or '').strip()
    reason = str(params.get('reason') or '').strip()
    if not payment_id or not reason:
        return {"success": False, "error": "payment_id and reason are required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        success = db_handler.mark_payment_manual_hold(payment_id, reason=reason, guild_id=guild_id)
        if not success:
            return {"success": False, "error": "Payment could not be moved to manual_hold"}
        row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
        return {"success": True, "payment": _redact_payment_row(row or {"payment_id": payment_id})}
    except Exception as e:
        logger.error(f"[AdminChat] Error in hold_payment: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_release_payment(bot, db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Release one manual_hold payment."""
    configured_admin_user_id, admin_error = _require_injected_admin_user_id(params)
    if admin_error:
        return {"success": False, "error": admin_error}

    payment_id = str(params.get('payment_id') or '').strip()
    new_status = str(params.get('new_status') or '').strip()
    if not payment_id or not new_status:
        return {"success": False, "error": "payment_id and new_status are required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        payment_service = getattr(bot, 'payment_service', None)
        if payment_service is None:
            return {"success": False, "error": "payment_service unavailable"}

        decision = await payment_service.reconcile_with_chain(payment_id, guild_id=guild_id)
        if decision.decision in {'reconciled_confirmed', 'reconciled_failed'}:
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            return {
                "success": True,
                "decision": decision.decision,
                "payment": _redact_payment_row(row or {"payment_id": payment_id}),
            }
        if decision.decision == 'keep_in_hold':
            db_handler.mark_payment_manual_hold(payment_id, reason=decision.reason, guild_id=guild_id)
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            return {
                "success": False,
                "error": decision.reason,
                "payment": _redact_payment_row(row or {"payment_id": payment_id}),
            }
        if decision.decision == 'not_applicable':
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            return {
                "success": False,
                "error": decision.reason,
                "payment": _redact_payment_row(row or {"payment_id": payment_id}),
            }

        success = db_handler.release_payment_hold(
            payment_id,
            new_status=new_status,
            guild_id=guild_id,
            reason=params.get('reason'),
        )
        if not success:
            return {"success": False, "error": "Payment could not be released from manual_hold"}
        row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
        return {"success": True, "payment": _redact_payment_row(row or {"payment_id": payment_id})}
    except Exception as e:
        logger.error(f"[AdminChat] Error in release_payment: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_cancel_payment(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel one payment."""
    configured_admin_user_id, admin_error = _require_injected_admin_user_id(params)
    if admin_error:
        return {"success": False, "error": admin_error}

    payment_id = str(params.get('payment_id') or '').strip()
    if not payment_id:
        return {"success": False, "error": "payment_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        success = db_handler.cancel_payment(
            payment_id,
            guild_id=guild_id,
            reason=params.get('reason'),
        )
        if not success:
            return {"success": False, "error": "Payment could not be cancelled from its current state"}
        row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
        return {"success": True, "payment": _redact_payment_row(row or {"payment_id": payment_id})}
    except Exception as e:
        logger.error(f"[AdminChat] Error in cancel_payment: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_initiate_payment(bot: discord.Client, db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Create or resume one admin-triggered payment intent.

    Flow branches based on the recipient's wallet state, in this order:
      1. Verified wallet on file → verified fast path: real payment goes
         directly to admin approval (no test needed).
      2. Unverified wallet on file → reuse the existing address, skip the
         awaiting_wallet state entirely, and fire the test payment
         immediately. The recipient only needs to confirm receipt.
      3. No wallet → create the intent in awaiting_wallet and prompt
         the recipient to post their Solana address.

    The optional wallet_address parameter lets the admin provide the address
    inline. When provided, it is upserted into wallet_registry as unverified
    and the flow proceeds through branch (2) above. This is the intended
    shape of 'pay @X Y SOL, wallet is Z' as a single atomic admin command.
    """
    try:
        guild_id = int(params.get('guild_id'))
        source_channel_id = int(params.get('source_channel_id'))
        recipient_user_id = int(params.get('recipient_user_id'))
        amount_sol = float(params.get('amount_sol'))
    except (TypeError, ValueError):
        return {"success": False, "error": "guild_id, source_channel_id, recipient_user_id, and amount_sol must be valid"}
    if guild_id <= 0 or source_channel_id <= 0 or recipient_user_id <= 0 or amount_sol <= 0:
        return {"success": False, "error": "guild_id, source_channel_id, recipient_user_id, and amount_sol must be > 0"}
    if not getattr(bot, 'payment_service', None):
        return {"success": False, "error": "payment_service is not configured"}

    existing = db_handler.get_active_intent_for_recipient(guild_id, source_channel_id, recipient_user_id)
    if existing:
        return {"success": True, "duplicate": True, "intent": existing}

    channel = bot.get_channel(source_channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(source_channel_id)
        except Exception as e:
            logger.error(f"[AdminChat] Error fetching initiate_payment channel {source_channel_id}: {e}", exc_info=True)
            return {"success": False, "error": "Could not access source channel"}

    reason = str(params.get('reason') or '').strip() or None
    admin_user_id = None
    if params.get('admin_user_id') is not None:
        try:
            parsed_admin_user_id = int(params.get('admin_user_id'))
            if parsed_admin_user_id > 0:
                admin_user_id = parsed_admin_user_id
        except (TypeError, ValueError):
            return {"success": False, "error": "admin_user_id must be a valid positive integer when provided"}
    producer_ref = f"{guild_id}_{recipient_user_id}_{int(time.time() * 1000)}"

    # Optional: admin provides a wallet address inline. Upsert it (unverified)
    # before the wallet-state lookup below so branch (2) picks it up.
    #
    # Security note: this relaxes the earlier "LLM never sources wallet addresses"
    # rule. The admin DM approval gate remains the authoritative money-movement
    # check — the admin sees the target wallet in their approval DM before any
    # real payment fires. The inline path is an ergonomic shortcut for admins,
    # not a security bypass. Every use is audit-logged below for traceability.
    inline_wallet_address = str(params.get('wallet_address') or '').strip()
    if inline_wallet_address:
        if not is_valid_solana_address(inline_wallet_address):
            return {"success": False, "error": "wallet_address is not a valid Solana address"}
        logger.info(
            "[AdminChat] initiate_payment inline wallet used: recipient=%s wallet=%s admin=%s",
            recipient_user_id,
            _redact_wallet_address(inline_wallet_address),
            admin_user_id,
        )
        try:
            db_handler.upsert_wallet(
                guild_id=guild_id,
                discord_user_id=recipient_user_id,
                chain='solana',
                address=inline_wallet_address,
                metadata={
                    'producer': 'admin_chat',
                    'source': 'initiate_payment_inline',
                    'channel_id': source_channel_id,
                },
            )
        except WalletUpdateBlockedError as exc:
            return {"success": False, "error": f"Cannot update wallet inline: {exc}"}
        except Exception as e:
            logger.error(
                f"[AdminChat] Error upserting inline wallet for user {recipient_user_id}: {e}",
                exc_info=True,
            )
            return {"success": False, "error": "Failed to persist inline wallet"}

    wallet_record = db_handler.get_wallet(guild_id, recipient_user_id, 'solana')

    # Branch 1: verified wallet → fast path straight to admin approval.
    if wallet_record and wallet_record.get('verified_at'):
        cog = bot.get_cog('AdminChatCog')
        if not cog or not hasattr(cog, '_gate_fresh_intent_atomic'):
            return {"success": False, "error": "Admin payment flow is not available"}
        try:
            gated = await cog._gate_fresh_intent_atomic(
                channel,
                guild_id,
                recipient_user_id,
                amount_sol,
                source_channel_id,
                wallet_record,
                admin_user_id,
                reason,
                producer_ref,
            )
        except Exception as e:
            logger.error(
                f"[AdminChat] Error starting verified fast-path payment flow for user {recipient_user_id}: {e}",
                exc_info=True,
            )
            return {"success": False, "error": "Failed to start payment flow"}

        if not gated:
            return {"success": False, "error": "Failed to start payment flow"}
        if gated.get('duplicate'):
            return {"success": True, "duplicate": True, "intent": gated.get('intent')}
        return {
            "success": True,
            "intent": gated.get('intent'),
            "wallet_on_file": True,
            "verified": True,
        }

    # Branch 2: unverified wallet already on file → use it, skip awaiting_wallet,
    # fire the test payment immediately. This covers both the "wallet set by a
    # previous attempt" case and the inline-wallet_address case (because we
    # just upserted it above).
    if wallet_record:
        intent = db_handler.create_admin_payment_intent(
            {
                'guild_id': guild_id,
                'channel_id': source_channel_id,
                'admin_user_id': admin_user_id,
                'recipient_user_id': recipient_user_id,
                'wallet_id': wallet_record.get('wallet_id'),
                'requested_amount_sol': amount_sol,
                'producer_ref': producer_ref,
                'reason': reason,
                'status': 'awaiting_test',
            },
            guild_id=guild_id,
        )
        if not intent:
            return {"success": False, "error": "Failed to create payment intent"}

        cog = bot.get_cog('AdminChatCog')
        if not cog or not hasattr(cog, '_start_admin_payment_flow'):
            db_handler.update_admin_payment_intent(intent['intent_id'], {'status': 'failed'}, guild_id)
            return {"success": False, "error": "Admin payment flow is not available"}

        try:
            await cog._start_admin_payment_flow(channel, intent)
        except Exception as e:
            logger.error(
                f"[AdminChat] Error starting unverified on-file payment flow for intent {intent.get('intent_id')}: {e}",
                exc_info=True,
            )
            db_handler.update_admin_payment_intent(intent['intent_id'], {'status': 'failed'}, guild_id)
            return {"success": False, "error": "Failed to start test payment flow"}

        return {
            "success": True,
            "intent": intent,
            "wallet_on_file": True,
            "verified": False,
        }

    # Branch 3: no wallet at all → create intent in awaiting_wallet and ask
    # the recipient to post their address.
    intent = db_handler.create_admin_payment_intent(
        {
            'guild_id': guild_id,
            'channel_id': source_channel_id,
            'admin_user_id': admin_user_id,
            'recipient_user_id': recipient_user_id,
            'wallet_id': None,
            'requested_amount_sol': amount_sol,
            'producer_ref': producer_ref,
            'reason': reason,
            'status': 'awaiting_wallet',
        },
        guild_id=guild_id,
    )
    if not intent:
        return {"success": False, "error": "Failed to create payment intent"}

    try:
        prompt = await channel.send(
            f"<@{recipient_user_id}> — a payment of {amount_sol} SOL has been initiated for you. "
            "Please reply with your Solana wallet address."
        )
    except Exception as e:
        logger.error(f"[AdminChat] Error sending wallet prompt for intent {intent.get('intent_id')}: {e}", exc_info=True)
        db_handler.update_admin_payment_intent(intent['intent_id'], {'status': 'failed'}, guild_id)
        return {"success": False, "error": "Failed to prompt recipient for wallet"}

    intent_id = intent['intent_id']
    intent = db_handler.update_admin_payment_intent(
        intent_id,
        {'prompt_message_id': prompt.id},
        guild_id,
    )
    if not intent:
        db_handler.update_admin_payment_intent(intent_id, {'status': 'failed'}, guild_id)
        return {"success": False, "error": "Failed to persist wallet prompt"}
    return {"success": True, "intent": intent, "wallet_on_file": False, "verified": False}


async def execute_initiate_batch_payment(bot: discord.Client, db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Create multiple admin payment intents atomically, then fan out per-recipient flow."""
    params = _pop_and_warn_wallet_override(params, tool_name='initiate_batch_payment')
    if not getattr(bot, 'payment_service', None):
        return {"success": False, "error": "payment_service is not configured"}

    try:
        guild_id = int(params.get('guild_id'))
        source_channel_id = int(params.get('source_channel_id'))
    except (TypeError, ValueError):
        return {"success": False, "error": "guild_id and source_channel_id must be valid"}
    if guild_id <= 0 or source_channel_id <= 0:
        return {"success": False, "error": "guild_id and source_channel_id must be > 0"}

    raw_payments = params.get('payments')
    if not isinstance(raw_payments, list):
        return {"success": False, "error": "payments must be an array"}
    if not 1 <= len(raw_payments) <= 20:
        return {"success": False, "error": "payments must contain between 1 and 20 entries"}

    amount_by_user_id: Dict[int, float] = {}
    wallet_by_user_id: Dict[int, Optional[Dict[str, Any]]] = {}
    prepared_records: List[Dict[str, Any]] = []
    seen_recipients: Set[int] = set()

    admin_user_id = None
    if params.get('admin_user_id') is not None:
        try:
            parsed_admin_user_id = int(params.get('admin_user_id'))
            if parsed_admin_user_id > 0:
                admin_user_id = parsed_admin_user_id
        except (TypeError, ValueError):
            return {"success": False, "error": "admin_user_id must be a valid positive integer when provided"}

    for index, raw_payment in enumerate(raw_payments):
        if not isinstance(raw_payment, dict):
            return {"success": False, "error": f"payments[{index}] must be an object"}

        payment_params = _pop_and_warn_wallet_override(raw_payment, tool_name=f'initiate_batch_payment[{index}]')
        try:
            recipient_user_id = int(payment_params.get('recipient_user_id'))
            amount_sol = float(payment_params.get('amount_sol'))
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": f"payments[{index}].recipient_user_id and payments[{index}].amount_sol must be valid",
            }
        if recipient_user_id <= 0 or amount_sol <= 0:
            return {
                "success": False,
                "error": f"payments[{index}].recipient_user_id and payments[{index}].amount_sol must be > 0",
            }
        if recipient_user_id in seen_recipients:
            return {"success": False, "error": f"Duplicate recipient_user_id in batch: {recipient_user_id}"}
        seen_recipients.add(recipient_user_id)

        existing = db_handler.get_active_intent_for_recipient(guild_id, source_channel_id, recipient_user_id)
        if existing:
            return {
                "success": False,
                "error": f"Recipient {recipient_user_id} already has an active payment intent in this channel",
            }

        reason = str(payment_params.get('reason') or '').strip() or None
        producer_ref = f"{guild_id}_{recipient_user_id}_{int(time.time() * 1000)}_{index}"
        wallet_record = db_handler.get_wallet(guild_id, recipient_user_id, 'solana')
        if wallet_record and not wallet_record.get('verified_at'):
            wallet_record = None

        wallet_by_user_id[recipient_user_id] = wallet_record
        amount_by_user_id[recipient_user_id] = amount_sol
        prepared_records.append(
            {
                'intent_id': str(uuid4()),
                'channel_id': source_channel_id,
                'admin_user_id': admin_user_id,
                'recipient_user_id': recipient_user_id,
                'wallet_id': wallet_record.get('wallet_id') if wallet_record else None,
                'requested_amount_sol': amount_sol,
                'producer_ref': producer_ref,
                'reason': reason,
                'status': 'awaiting_admin_init' if wallet_record else 'awaiting_wallet',
                'final_payment_id': None,
            }
        )

    channel = bot.get_channel(source_channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(source_channel_id)
        except Exception as e:
            logger.error(f"[AdminChat] Error fetching initiate_batch_payment channel {source_channel_id}: {e}", exc_info=True)
            return {"success": False, "error": "Could not access source channel"}

    created_intents = db_handler.create_admin_payment_intents_batch(prepared_records, guild_id=guild_id)
    if created_intents is None:
        return {"success": False, "error": "Failed to create payment intents"}

    cog = bot.get_cog('AdminChatCog')
    if any(wallet_by_user_id.get(int(intent['recipient_user_id'])) for intent in created_intents):
        if not cog or not hasattr(cog, '_gate_existing_intent'):
            return {"success": False, "error": "Admin payment flow is not available"}

    try:
        for intent in created_intents:
            recipient_user_id = int(intent['recipient_user_id'])
            wallet_record = wallet_by_user_id.get(recipient_user_id)
            if wallet_record:
                gated = await cog._gate_existing_intent(
                    channel,
                    intent,
                    wallet_record,
                    amount_by_user_id[recipient_user_id],
                )
                if not gated:
                    raise RuntimeError(f"Failed to gate verified intent {intent['intent_id']}")
                continue

            prompt = await channel.send(
                f"<@{recipient_user_id}> — a payment of {amount_by_user_id[recipient_user_id]} SOL has been initiated for you. "
                "Please reply with your Solana wallet address."
            )
            updated_intent = db_handler.update_admin_payment_intent(
                intent['intent_id'],
                {'prompt_message_id': prompt.id},
                guild_id,
            )
            if not updated_intent:
                raise RuntimeError(f"Failed to persist wallet prompt for intent {intent['intent_id']}")
        return {
            "success": True,
            "count": len(created_intents),
            "intents": created_intents,
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error during initiate_batch_payment fan-out: {e}", exc_info=True)
        return {"success": False, "error": "Failed to fan out payment intents", "count": len(created_intents)}


async def execute_query_payment_state(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only payment lookup by id or recipient user."""
    trusted_params = _pop_and_warn_wallet_override(params, tool_name='query_payment_state')
    payment_id = str(trusted_params.get('payment_id') or '').strip()
    user_id = trusted_params.get('user_id')
    limit = min(int(trusted_params.get('limit', 20)), 100)

    if not payment_id and user_id in (None, ''):
        return {"success": False, "error": "payment_id or user_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, trusted_params)
        response: Dict[str, Any] = {"success": True}

        if payment_id:
            row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            if not row:
                return {"success": False, "error": f"Payment {payment_id} not found"}
            response["payment"] = _redact_payment_row(row)

        if user_id not in (None, ''):
            recipient_user_id = int(user_id)
            rows = db_handler.list_payment_requests(
                guild_id=guild_id,
                recipient_discord_id=recipient_user_id,
                limit=limit,
            )
            response["user_id"] = recipient_user_id
            response["count"] = len(rows)
            response["payments"] = [_redact_payment_row(row) for row in rows]

        return response
    except Exception as e:
        logger.error(f"[AdminChat] Error in query_payment_state: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_query_wallet_state(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only wallet lookup for one user."""
    trusted_params = _pop_and_warn_wallet_override(params, tool_name='query_wallet_state')
    user_id = trusted_params.get('user_id')
    if user_id in (None, ''):
        return {"success": False, "error": "user_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, trusted_params)
        discord_user_id = int(user_id)
        rows = _list_wallet_rows(
            db_handler,
            guild_id=guild_id,
            chain=None,
            discord_user_id=discord_user_id,
            verified=None,
            limit=100,
        )
        return {
            "success": True,
            "user_id": discord_user_id,
            "count": len(rows),
            "wallets": [_redact_wallet_row(row) for row in rows],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in query_wallet_state: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_list_recent_payments(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only recent payment listing."""
    trusted_params = _pop_and_warn_wallet_override(params, tool_name='list_recent_payments')

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, trusted_params)
        producer = None
        if trusted_params.get('producer') not in (None, ''):
            producer = _normalize_payment_producer(trusted_params.get('producer'))

        recipient_discord_id = trusted_params.get('user_id')
        if recipient_discord_id not in (None, ''):
            recipient_discord_id = int(recipient_discord_id)

        rows = db_handler.list_payment_requests(
            guild_id=guild_id,
            producer=producer,
            recipient_discord_id=recipient_discord_id,
            limit=min(int(trusted_params.get('limit', 20)), 100),
        )
        return {
            "success": True,
            "count": len(rows),
            "payments": [_redact_payment_row(row) for row in rows],
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in list_recent_payments: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_upsert_wallet_for_user(bot: discord.Client, db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Admin-only wallet upsert that always leaves the wallet unverified."""
    configured_admin_user_id, admin_error = _require_injected_admin_user_id(params)
    if admin_error:
        return {"success": False, "error": admin_error}

    user_id = params.get('user_id')
    wallet_address = str(params.get('wallet_address') or '').strip()
    chain = str(params.get('chain') or 'solana').strip().lower()
    reason = str(params.get('reason') or '').strip() or None

    if user_id in (None, ''):
        return {"success": False, "error": "user_id is required"}
    try:
        recipient_user_id = int(user_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "user_id must be a valid positive integer"}
    if recipient_user_id <= 0:
        return {"success": False, "error": "user_id must be a valid positive integer"}
    if chain != 'solana':
        return {"success": False, "error": "Only chain='solana' is supported"}
    if not wallet_address:
        return {"success": False, "error": "wallet_address is required"}
    if not is_valid_solana_address(wallet_address):
        return {"success": False, "error": "wallet_address must be a valid Solana address"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        # When called from a DM, the resolver defaults to the "home" guild which
        # may differ from the guild where the recipient's intent lives. Check all
        # writable guilds for a pending awaiting_wallet intent and prefer that
        # guild so the wallet and intent stay in sync.
        server_config = getattr(db_handler, 'server_config', None)
        if server_config is not None:
            try:
                writable_guilds = [
                    int(s['guild_id'])
                    for s in server_config.get_enabled_servers(require_write=True)
                    if s.get('guild_id') is not None
                ]
            except Exception:
                writable_guilds = []
            for candidate_guild_id in writable_guilds:
                if candidate_guild_id == guild_id:
                    continue
                try:
                    pending = db_handler.get_awaiting_wallet_intent_for_user(
                        candidate_guild_id, recipient_user_id,
                    )
                    if pending:
                        guild_id = candidate_guild_id
                        break
                except Exception:
                    pass
        metadata: Dict[str, Any] = {'producer': 'admin_chat', 'source': 'upsert_wallet_for_user'}
        if reason:
            metadata['reason'] = reason
        wallet_record = db_handler.upsert_wallet(
            guild_id=guild_id,
            discord_user_id=recipient_user_id,
            chain=chain,
            address=wallet_address,
            metadata=metadata,
        )
    except WalletUpdateBlockedError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[AdminChat] Error in upsert_wallet_for_user: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

    if not wallet_record:
        return {"success": False, "error": "Failed to upsert wallet"}

    wallet_record = _force_wallet_unverified(db_handler, wallet_record, guild_id=guild_id)
    redacted_wallet = _redact_wallet_address(wallet_record.get('wallet_address') or wallet_address)

    # If there's a pending intent blocked on wallet collection for this user,
    # advance it to the test phase now that the wallet is on file. This is the
    # "admin provides wallet later" flow: admin says 'pay X Y SOL', recipient
    # never posts, admin follows up in chat with 'X's wallet is Z' — the
    # agent calls upsert_wallet_for_user, and this branch unblocks the
    # pending intent without requiring a separate step.
    advanced_intent_id: Optional[str] = None
    pending_intent = None
    try:
        pending_intent = db_handler.get_awaiting_wallet_intent_for_user(guild_id, recipient_user_id)
    except Exception as e:
        logger.warning(
            "[AdminChat] Failed to look up awaiting_wallet intent for user %s: %s",
            recipient_user_id,
            e,
        )
    if pending_intent:
        cog = bot.get_cog('AdminChatCog')
        if cog and hasattr(cog, '_advance_intent_to_test_phase'):
            channel_id = int(pending_intent.get('channel_id') or 0)
            pending_channel = None
            if channel_id:
                pending_channel = bot.get_channel(channel_id)
                if pending_channel is None:
                    try:
                        pending_channel = await bot.fetch_channel(channel_id)
                    except Exception as fetch_err:
                        logger.warning(
                            "[AdminChat] Failed to fetch channel %s while advancing intent %s: %s",
                            channel_id,
                            pending_intent.get('intent_id'),
                            fetch_err,
                        )
            if pending_channel is not None:
                try:
                    advanced = await cog._advance_intent_to_test_phase(
                        pending_channel,
                        pending_intent,
                        wallet_record,
                    )
                    if advanced:
                        advanced_intent_id = str(pending_intent.get('intent_id'))
                        logger.info(
                            "[AdminChat] upsert_wallet_for_user advanced intent %s to awaiting_test",
                            advanced_intent_id,
                        )
                except Exception as e:
                    logger.error(
                        "[AdminChat] Failed to advance intent %s after wallet upsert: %s",
                        pending_intent.get('intent_id'),
                        e,
                        exc_info=True,
                    )

    advance_note = ""
    if advanced_intent_id:
        advance_note = f" Pending intent `{advanced_intent_id}` advanced to test phase."
    await _dm_admin_user(
        bot,
        configured_admin_user_id,
        f"wallet for <@{recipient_user_id}> set to {redacted_wallet}, will be verified on next payment.{advance_note}",
        log_context=f"upsert_wallet_for_user:{recipient_user_id}",
    )
    return {
        "success": True,
        "wallet": _redact_wallet_row(wallet_record),
        "advanced_intent_id": advanced_intent_id,
        "message": (
            f"wallet for <@{recipient_user_id}> set to {redacted_wallet}, "
            f"will be verified on next payment.{advance_note}"
        ),
    }


async def execute_resolve_admin_intent(bot: discord.Client, db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel one admin payment intent and any linked cancellable payments."""
    configured_admin_user_id, admin_error = _require_injected_admin_user_id(params)
    if admin_error:
        return {"success": False, "error": admin_error}
    if 'resolution' in params:
        return {
            "success": False,
            "error": "resolve_admin_intent is cancel-only. Use /payment-resolve if you need to mark a payment completed.",
        }

    intent_id = str(params.get('intent_id') or '').strip()
    note = str(params.get('note') or '').strip() or None
    if not intent_id:
        return {"success": False, "error": "intent_id is required"}

    try:
        guild_id = _resolve_db_handler_guild_id(db_handler, params)
        intent = db_handler.get_admin_payment_intent(intent_id, guild_id)
        if not intent:
            return {"success": False, "error": f"Intent {intent_id} not found"}

        intent_status = str(intent.get('status') or '').strip().lower()
        if intent_status in {'completed', 'failed', 'cancelled'}:
            return {"success": False, "error": f"Intent {intent_id} is already terminal ({intent_status})"}

        linked_payments: List[Dict[str, Any]] = []
        cancel_reason = note or "Admin cancelled payment intent"
        for payment_field in ('test_payment_id', 'final_payment_id'):
            payment_id = intent.get(payment_field)
            if not payment_id:
                continue

            payment_row = db_handler.get_payment_request(payment_id, guild_id=guild_id)
            if not payment_row:
                continue

            payment_status = str(payment_row.get('status') or '').strip().lower()
            if payment_status in {'submitted', 'confirmed', 'manual_hold'}:
                return {
                    "success": False,
                    "error": (
                        f"Linked payment {payment_id} is {payment_status}. "
                        "Use /payment-resolve for submitted, confirmed, or manual_hold payments."
                    ),
                }

            if payment_status != 'cancelled':
                cancelled = db_handler.cancel_payment(payment_id, guild_id=guild_id, reason=cancel_reason)
                if not cancelled:
                    return {
                        "success": False,
                        "error": f"Linked payment {payment_id} could not be cancelled from status {payment_status}",
                    }
                payment_row = db_handler.get_payment_request(payment_id, guild_id=guild_id) or payment_row

            linked_payments.append(_redact_payment_row(payment_row))

        updated_intent = db_handler.update_admin_payment_intent(
            intent_id,
            {'status': 'cancelled'},
            guild_id,
        )
        if not updated_intent:
            return {"success": False, "error": f"Failed to cancel intent {intent_id}"}

        await _dm_admin_user(
            bot,
            configured_admin_user_id,
            f"admin payment intent `{intent_id}` cancelled for <@{intent.get('recipient_user_id')}>",
            log_context=f"resolve_admin_intent:{intent_id}",
        )
        return {
            "success": True,
            "intent": updated_intent,
            "payments": linked_payments,
            "note": note,
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in resolve_admin_intent: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_update_member_socials(db_handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Update a member's twitter/reddit handle without touching anything else."""
    user_id = params.get('user_id')
    if not user_id:
        return {"success": False, "error": "user_id is required"}
    try:
        member_id = int(user_id)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid user_id: {user_id!r}"}

    twitter_url = params.get('twitter_url')
    reddit_url = params.get('reddit_url')
    if twitter_url is None and reddit_url is None:
        return {"success": False, "error": "Provide twitter_url and/or reddit_url"}

    try:
        existing = db_handler.get_member(member_id)
        if not existing:
            return {"success": False, "error": f"No member found for user_id {member_id}"}

        ok = await asyncio.to_thread(
            db_handler.update_member_socials,
            member_id,
            twitter_url,
            reddit_url,
        )
        if not ok:
            return {"success": False, "error": "Database update failed"}

        updated = db_handler.get_member(member_id)
        return {
            "success": True,
            "member_id": member_id,
            "username": (updated or {}).get('username'),
            "twitter_url": (updated or {}).get('twitter_url'),
            "reddit_url": (updated or {}).get('reddit_url'),
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in update_member_socials: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def _resolve_speaker_role_id(guild_id: int) -> Optional[int]:
    """Mirror AdminCog._get_speaker_role_id: server_config field, then env fallback."""
    sc = _get_server_config()
    value = sc.get_server_field(guild_id, 'speaker_role_id', cast=int)
    if value is not None:
        return value
    env_value = os.getenv('SPEAKER_ROLE_ID')
    return int(env_value) if env_value else None


def _is_speaker_management_enabled(guild_id: int) -> bool:
    """Mirror AdminCog._is_speaker_management_enabled."""
    sc = _get_server_config()
    server = sc.get_server(guild_id)
    if server and server.get('speaker_management_enabled') is not None:
        return bool(server.get('speaker_management_enabled'))
    if guild_id == sc.bndc_guild_id:
        return True
    return False


async def execute_mute_speaker(
    bot: discord.Client,
    db_handler,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Remove the Speaker role from a user — DM/admin-chat counterpart to /mute.

    If the user is already muted, this updates/replaces their timed-mute record so
    the admin can extend, shorten, or convert a permanent mute to a timed one
    without first calling unmute.
    """
    from src.features.admin.admin_cog import _parse_duration, post_mute_to_moderation

    user_id_raw = params.get('user_id')
    if not user_id_raw:
        return {"success": False, "error": "user_id is required"}
    try:
        member_id = int(user_id_raw)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid user_id: {user_id_raw!r}"}

    reason_text = (params.get('reason') or "").strip()
    if not reason_text:
        return {"success": False, "error": "reason is required (will be posted to the moderation channel)"}

    guild_id = _resolve_guild_id(params)
    if not guild_id:
        return {"success": False, "error": "Could not resolve guild_id"}

    if not _is_speaker_management_enabled(guild_id):
        return {"success": False, "error": "Speaker mute controls are not enabled in this server."}

    role_id = _resolve_speaker_role_id(guild_id)
    if not role_id:
        return {"success": False, "error": "SPEAKER_ROLE_ID is not configured."}

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"success": False, "error": f"Guild {guild_id} not in cache"}

    role = guild.get_role(role_id)
    if not role:
        return {"success": False, "error": "Speaker role not found in this server."}

    member = guild.get_member(member_id)
    if not member:
        try:
            member = await guild.fetch_member(member_id)
        except discord.NotFound:
            return {"success": False, "error": f"Member {member_id} not in guild"}
        except discord.HTTPException as e:
            return {"success": False, "error": f"Failed to fetch member: {e}"}

    duration = params.get('duration')
    td = None
    if duration:
        td = _parse_duration(duration)
        if td is None:
            return {
                "success": False,
                "error": f"Invalid duration {duration!r}. Use a number + h/d/w (e.g. '1h', '7d', '2w').",
            }

    admin_user_id = params.get('admin_user_id')
    actor_label = f"admin {admin_user_id}" if admin_user_id else "admin chat"
    audit_reason = (
        f"Muted by {actor_label}: {reason_text}" + (f" (for {duration})" if duration else "")
    )

    was_already_muted = role not in member.roles

    try:
        if db_handler:
            db_handler.set_is_speaker(member_id, False, guild_id=guild_id)

        if not was_already_muted:
            await member.remove_roles(role, reason=audit_reason[:512])

        mute_end_iso = None
        timed_saved = False
        if td and db_handler:
            mute_end = datetime.now(timezone.utc) + td
            mute_end_iso = mute_end.isoformat()
            timed_saved = db_handler.create_timed_mute(
                member_id=member_id,
                guild_id=guild_id,
                mute_end_at=mute_end_iso,
                reason=reason_text,
                muted_by_id=int(admin_user_id) if admin_user_id else None,
            )
        elif was_already_muted and not td and db_handler:
            # Converting an existing timed mute back to permanent: clear the timer.
            db_handler.delete_timed_mute(member_id, guild_id)

        mod_log_posted = await post_mute_to_moderation(
            bot,
            target_user_id=member_id,
            target_username=member.name,
            actor_user_id=int(admin_user_id) if admin_user_id else None,
            actor_label=actor_label,
            duration=duration,
            mute_end_at_iso=mute_end_iso,
            reason=reason_text,
        )

        action_verb = "updated mute on" if was_already_muted else "muted"
        logger.info(
            f"[AdminChat] mute_speaker: {action_verb} {member_id} ({member.name}) by {actor_label}"
            + (f" for {duration}" if duration else " permanently")
            + f" — reason: {reason_text}"
        )

        if was_already_muted:
            if duration and timed_saved:
                msg = f"<@{member_id}> was already muted — updated to expire after {duration}."
            elif duration and not timed_saved:
                msg = f"<@{member_id}> was already muted — but the {duration} auto-unmute couldn't be scheduled."
            else:
                msg = f"<@{member_id}> was already muted — converted to permanent (cleared any timer)."
        else:
            if duration and timed_saved:
                msg = f"Muted <@{member_id}> for {duration} — Speaker role removed."
            elif duration and not timed_saved:
                msg = f"Muted <@{member_id}> — Speaker role removed, but auto-unmute couldn't be scheduled."
            else:
                msg = f"Muted <@{member_id}> — Speaker role removed."

        if not mod_log_posted:
            msg += " (Note: couldn't post to the moderation log channel — check bot permissions / channel type.)"

        return {
            "success": True,
            "user_id": str(member_id),
            "username": member.name,
            "duration": duration,
            "permanent": duration is None,
            "mute_end_at": mute_end_iso,
            "timed_mute_scheduled": timed_saved,
            "was_already_muted": was_already_muted,
            "reason": reason_text,
            "moderation_log_posted": mod_log_posted,
            "message": msg,
        }
    except discord.Forbidden:
        return {"success": False, "error": "I don't have permission to remove that role."}
    except Exception as e:
        logger.error(f"[AdminChat] Error in mute_speaker for {member_id}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_unmute_speaker(
    bot: discord.Client,
    db_handler,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore the Speaker role — DM/admin-chat counterpart to /unmute."""
    user_id_raw = params.get('user_id')
    if not user_id_raw:
        return {"success": False, "error": "user_id is required"}
    try:
        member_id = int(user_id_raw)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Invalid user_id: {user_id_raw!r}"}

    guild_id = _resolve_guild_id(params)
    if not guild_id:
        return {"success": False, "error": "Could not resolve guild_id"}

    if not _is_speaker_management_enabled(guild_id):
        return {"success": False, "error": "Speaker mute controls are not enabled in this server."}

    role_id = _resolve_speaker_role_id(guild_id)
    if not role_id:
        return {"success": False, "error": "SPEAKER_ROLE_ID is not configured."}

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"success": False, "error": f"Guild {guild_id} not in cache"}

    role = guild.get_role(role_id)
    if not role:
        return {"success": False, "error": "Speaker role not found in this server."}

    member = guild.get_member(member_id)
    if not member:
        try:
            member = await guild.fetch_member(member_id)
        except discord.NotFound:
            return {"success": False, "error": f"Member {member_id} not in guild"}
        except discord.HTTPException as e:
            return {"success": False, "error": f"Failed to fetch member: {e}"}

    admin_user_id = params.get('admin_user_id')
    actor_label = f"admin {admin_user_id}" if admin_user_id else "admin chat"
    reason_text = (params.get('reason') or "").strip() or "Unmuted via admin chat"

    if role in member.roles:
        # Make sure DB & timed-mute state are in sync even if Discord role is already correct.
        if db_handler:
            db_handler.set_is_speaker(member_id, True, guild_id=guild_id)
            db_handler.delete_timed_mute(member_id, guild_id)
        return {
            "success": True,
            "already_unmuted": True,
            "user_id": str(member_id),
            "username": member.name,
            "message": f"<@{member_id}> already has the Speaker role.",
        }

    try:
        if db_handler:
            db_handler.set_is_speaker(member_id, True, guild_id=guild_id)
        await member.add_roles(role, reason=f"Unmuted by {actor_label}: {reason_text}"[:512])
        if db_handler:
            db_handler.delete_timed_mute(member_id, guild_id)
        logger.info(
            f"[AdminChat] unmute_speaker: {member_id} ({member.name}) unmuted by {actor_label} — reason: {reason_text}"
        )
        return {
            "success": True,
            "user_id": str(member_id),
            "username": member.name,
            "message": f"Unmuted <@{member_id}> — Speaker role restored.",
        }
    except discord.Forbidden:
        return {"success": False, "error": "I don't have permission to add that role."}
    except Exception as e:
        logger.error(f"[AdminChat] Error in unmute_speaker for {member_id}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_get_bot_status(bot: discord.Client) -> Dict[str, Any]:
    """Get bot status information."""
    import time

    try:
        uptime_seconds = None
        if hasattr(bot, 'start_time'):
            uptime_seconds = int(time.time() - bot.start_time)
        summarizer_cog = bot.get_cog("SummarizerCog") if hasattr(bot, "get_cog") else None
        live_pass_loop = getattr(summarizer_cog, "run_live_pass", None)

        return {
            "success": True,
            "status": {
                "online": bot.is_ready(),
                "latency_ms": round(bot.latency * 1000, 2),
                "uptime_seconds": uptime_seconds,
                "dev_mode": getattr(bot, 'dev_mode', False),
                "guilds": len(bot.guilds),
                "live_pass_loop_running": bool(
                    live_pass_loop and hasattr(live_pass_loop, "is_running") and live_pass_loop.is_running()
                ),
                "daily_summaries_mode": "legacy/backfill only",
            }
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in get_bot_status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_search_logs(params: Dict[str, Any]) -> Dict[str, Any]:
    """Search bot system logs from Supabase."""
    from supabase import create_client as sc

    query = params.get('query', '')
    level = params.get('level', '')
    hours = min(params.get('hours', 6), 48)
    limit = min(params.get('limit', 30), 100)

    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        client = sc(url, key)

        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        q = client.table('system_logs').select(
            'timestamp, level, logger_name, message'
        ).gte('timestamp', cutoff).order('timestamp', desc=True)

        if level:
            q = q.eq('level', level)
        if query:
            q = q.ilike('message', f'%{query}%')
        q = q.limit(limit)

        rows = q.execute().data

        if not rows:
            return {
                "success": True,
                "count": 0,
                "summary": f"No logs found{' matching ' + repr(query) if query else ''} in the last {hours}h."
            }

        # Format oldest-first for readability
        rows.reverse()
        lines = []
        for r in rows:
            ts = r['timestamp'][:19].replace('T', ' ')
            lvl = r['level'][:4]
            msg = r['message'][:200]
            lines.append(f"`{ts}` **{lvl}** {msg}")

        return {
            "success": True,
            "count": len(rows),
            "summary": "\n".join(lines)
        }

    except Exception as e:
        logger.error(f"[AdminChat] Error in search_logs: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def _refresh_cdn_urls(bot: discord.Client, content: str) -> str:
    """Replace expired Discord CDN URLs in content with fresh ones.

    Finds Discord CDN attachment URLs, extracts channel_id/message_id,
    fetches fresh URLs from the Discord API, and substitutes them.
    """
    from src.common.discord_utils import refresh_media_url

    cdn_pattern = re.compile(
        r'https://cdn\.discordapp\.com/attachments/(\d+)/(\d+)/([^\s\)]+)'
    )
    matches = list(cdn_pattern.finditer(content))
    if not matches:
        return content

    # Deduplicate by (channel_id, attachment_id) to avoid redundant API calls
    seen = {}
    for match in matches:
        ch_id, att_id = int(match.group(1)), int(match.group(2))
        if (ch_id, att_id) not in seen:
            seen[(ch_id, att_id)] = match

    # For each unique attachment, find the source message and refresh
    # The channel_id in a CDN URL is the channel where the file was uploaded
    # We need to find the message that contains this attachment
    for (ch_id, att_id), match in seen.items():
        try:
            result = await refresh_media_url(bot, ch_id, att_id, logger)
            if result and result.get('attachments'):
                old_filename = match.group(3).split('?')[0]  # Strip query params
                for att in result['attachments']:
                    if att.get('filename') == old_filename or old_filename in att.get('url', ''):
                        content = content.replace(match.group(0), att['url'])
                        break
        except Exception as e:
            logger.debug(f"[AdminChat] Could not refresh CDN URL: {e}")

    return content


async def execute_send_message(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Send a message to a channel, optionally as a reply. Auto-refreshes Discord CDN URLs."""
    channel_id = params.get('channel_id', '')
    content = params.get('content', '')
    reply_to = params.get('reply_to')

    if not channel_id or not content:
        return {"success": False, "error": "channel_id and content are required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))

        # Auto-refresh any Discord CDN URLs before sending
        content = await _refresh_cdn_urls(bot, content)

        kwargs = {}
        if reply_to:
            try:
                ref_msg = await channel.fetch_message(int(reply_to))
                kwargs['reference'] = ref_msg
            except Exception:
                pass  # Send without reply if message not found

        msg = await channel.send(content, **kwargs)
        return {
            "success": True,
            "message_id": str(msg.id),
            "jump_url": msg.jump_url
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in send_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_edit_message(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Edit a bot message."""
    channel_id = params.get('channel_id', '')
    message_id = params.get('message_id', '')
    content = params.get('content', '')

    if not all([channel_id, message_id, content]):
        return {"success": False, "error": "channel_id, message_id, and content are required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        if msg.author.id != bot.user.id:
            return {"success": False, "error": "Can only edit bot's own messages"}
        await msg.edit(content=content)
        return {"success": True, "message_id": str(msg.id)}
    except discord.NotFound:
        return {"success": False, "error": "Message not found"}
    except Exception as e:
        logger.error(f"[AdminChat] Error in edit_message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_delete_message(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Delete one or more messages by ID."""
    channel_id = params.get('channel_id', '')
    message_id = params.get('message_id', '')
    message_ids = params.get('message_ids', [])

    if not channel_id:
        return {"success": False, "error": "channel_id is required"}

    # Combine single + list into one list
    ids_to_delete = list(message_ids)
    if message_id:
        ids_to_delete.append(message_id)
    if not ids_to_delete:
        return {"success": False, "error": "message_id or message_ids is required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
    except Exception as e:
        return {"success": False, "error": f"Could not find channel: {e}"}

    deleted = []
    errors = []
    for mid in ids_to_delete:
        try:
            msg = await channel.fetch_message(int(mid))
            await msg.delete()
            deleted.append(mid)
        except discord.NotFound:
            errors.append(f"{mid}: not found")
        except discord.Forbidden:
            errors.append(f"{mid}: missing permissions")
        except Exception as e:
            errors.append(f"{mid}: {e}")

    result = {"success": True, "deleted": len(deleted), "deleted_ids": deleted}
    if errors:
        result["errors"] = errors
    return result


async def execute_upload_file(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Upload a file to a channel."""
    channel_id = params.get('channel_id', '')
    file_path = params.get('file_path', '')
    content = params.get('content', '')

    if not channel_id or not file_path:
        return {"success": False, "error": "channel_id and file_path are required"}

    if not os.path.exists(file_path):
        return {"success": False, "error": f"File not found: {file_path}"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        file = discord.File(file_path)
        msg = await channel.send(content=content or None, file=file)
        urls = [a.url for a in msg.attachments]
        return {
            "success": True,
            "message_id": str(msg.id),
            "attachment_urls": urls
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in upload_file: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_resolve_user(params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a username to a Discord user ID."""
    from scripts.discord_tools import resolve_user as dt_resolve_user

    username = params.get('username', '')
    if not username:
        return {"success": False, "error": "username is required"}

    try:
        user_data = dt_resolve_user(username)
        if not user_data:
            return {"success": False, "error": f"User '{username}' not found"}
        return {
            "success": True,
            "user_id": str(user_data['member_id']),
            "username": user_data.get('username'),
            "display_name": user_data.get('global_name') or user_data.get('server_nick'),
            "mention": f"<@{user_data['member_id']}>"
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in resolve_user: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def execute_query_table(params: Dict[str, Any]) -> Dict[str, Any]:
    """Query any allowed database table with filters."""
    table = params.get('table', '')
    select_cols = params.get('select', '*')
    filters = params.get('filters', {})
    order = params.get('order', '')
    limit = min(params.get('limit', 25), 100)

    if not table:
        return {"success": False, "error": "table is required"}
    if table not in QUERYABLE_TABLES:
        return {"success": False, "error": f"Table '{table}' not allowed. Available: {', '.join(sorted(QUERYABLE_TABLES))}"}

    try:
        resolved_guild_id = _resolve_guild_id(params)
        sb = _get_supabase()
        query = sb.table(table).select(select_cols)

        # Auto-scope guild_id for tables that have it
        GUILD_SCOPED_TABLES = {
            'discord_messages', 'discord_channels', 'daily_summaries',
            'shared_posts', 'pending_intros', 'discord_reactions',
            'discord_reaction_log', 'competitions', 'social_publications',
            'social_channel_routes', 'topic_editor_runs', 'topics',
            'topic_sources', 'topic_aliases', 'topic_transitions',
            'editorial_observations', 'topic_editor_checkpoints',
            'live_update_editor_runs',
            'live_update_candidates', 'live_update_decisions',
            'live_update_feed_items', 'live_update_editorial_memory',
            'live_update_watchlist', 'live_update_duplicate_state',
            'live_update_checkpoints', 'live_top_creation_runs',
            'live_top_creation_posts', 'live_top_creation_checkpoints',
        }
        if table in GUILD_SCOPED_TABLES and 'guild_id' not in filters:
            if resolved_guild_id:
                query = query.eq('guild_id', resolved_guild_id)

        # Apply filters with operator support
        for col, val in filters.items():
            val_str = str(val)
            if val_str.startswith('gt.'):
                query = query.gt(col, val_str[3:])
            elif val_str.startswith('gte.'):
                query = query.gte(col, val_str[4:])
            elif val_str.startswith('lt.'):
                query = query.lt(col, val_str[3:])
            elif val_str.startswith('lte.'):
                query = query.lte(col, val_str[4:])
            elif val_str.startswith('neq.'):
                query = query.neq(col, val_str[4:])
            elif val_str.startswith('like.'):
                query = query.like(col, val_str[5:])
            elif val_str.startswith('ilike.'):
                query = query.ilike(col, val_str[6:])
            elif val_str.startswith('in.'):
                # Comma-separated list: "in.a,b,c"
                values = val_str[3:].split(',')
                query = query.in_(col, values)
            elif val_str == 'is.null':
                query = query.is_(col, 'null')
            elif val_str == 'not.null':
                query = query.not_.is_(col, 'null')
            else:
                query = query.eq(col, val)

        # Apply ordering
        if order:
            desc = order.startswith('-')
            col_name = order.lstrip('-')
            query = query.order(col_name, desc=desc)

        query = query.limit(limit)
        result = query.execute()

        return {
            "success": True,
            "count": len(result.data),
            "data": result.data,
        }
    except Exception as e:
        logger.error(f"[AdminChat] Error in query_table: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ========== Media Tools ==========

MEDIA_DIR = '/tmp/media'
ALLOWED_MEDIA_BINARIES = {'ffmpeg', 'ffprobe', 'python3'}


async def execute_download_media(bot: discord.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    """Download attachments from a Discord message to /tmp/media/."""
    import aiohttp

    message_id = params.get('message_id', '')
    channel_id = params.get('channel_id', '')

    if not message_id or not channel_id:
        return {"success": False, "error": "message_id and channel_id are required"}

    try:
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch message: {e}"}

    if not message.attachments:
        return {"success": False, "error": "Message has no attachments"}

    out_dir = os.path.join(MEDIA_DIR, str(message_id))
    os.makedirs(out_dir, exist_ok=True)

    downloaded = []
    async with aiohttp.ClientSession() as session:
        for att in message.attachments:
            file_path = os.path.join(out_dir, att.filename)
            try:
                async with session.get(att.url) as resp:
                    if resp.status == 200:
                        with open(file_path, 'wb') as f:
                            f.write(await resp.read())
                        downloaded.append({
                            "filename": att.filename,
                            "path": file_path,
                            "size_bytes": os.path.getsize(file_path),
                            "content_type": att.content_type,
                        })
            except Exception as e:
                downloaded.append({"filename": att.filename, "error": str(e)})

    return {"success": True, "directory": out_dir, "files": downloaded}


async def execute_run_media_command(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a whitelisted media command (ffmpeg, ffprobe, python3)."""
    import asyncio as _asyncio
    import shlex

    command = params.get('command', '').strip()
    if not command:
        return {"success": False, "error": "command is required"}

    # Validate the binary is allowed
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return {"success": False, "error": f"Invalid command syntax: {e}"}

    binary = os.path.basename(parts[0])
    if binary not in ALLOWED_MEDIA_BINARIES:
        return {"success": False, "error": f"Binary '{binary}' not allowed. Use: {', '.join(sorted(ALLOWED_MEDIA_BINARIES))}"}

    os.makedirs(MEDIA_DIR, exist_ok=True)

    try:
        proc = await _asyncio.create_subprocess_shell(
            command,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            cwd=MEDIA_DIR,
        )
        stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=300)
        return {
            "success": proc.returncode == 0,
            "return_code": proc.returncode,
            "stdout": stdout.decode(errors='replace')[:4000],
            "stderr": stderr.decode(errors='replace')[:4000],
        }
    except _asyncio.TimeoutError:
        proc.kill()
        return {"success": False, "error": "Command timed out after 5 minutes"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def execute_list_media_files(params: Dict[str, Any]) -> Dict[str, Any]:
    """List files in the media working directory."""
    sub = params.get('path', '')
    base = os.path.join(MEDIA_DIR, sub) if sub else MEDIA_DIR

    # Prevent directory traversal
    base = os.path.realpath(base)
    if not base.startswith(MEDIA_DIR):
        return {"success": False, "error": "Path must be within /tmp/media/"}

    if not os.path.exists(base):
        return {"success": True, "files": [], "note": "Directory does not exist yet. Download some media first."}

    files = []
    for root, dirs, filenames in os.walk(base):
        for fn in filenames:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, MEDIA_DIR)
            files.append({
                "path": fp,
                "relative": rel,
                "size_bytes": os.path.getsize(fp),
            })

    return {"success": True, "directory": base, "files": files}


# ========== Tool Executor Dispatcher ==========

async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    bot: discord.Client,
    db_handler,
    sharer,
    allowed_tools: Optional[Set[str]] = None,
    requester_id: Optional[int] = None,
    trusted_guild_id: Optional[int] = None,
    dm_channel_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute a tool by name and return the result as a dict."""

    if allowed_tools is not None and tool_name not in allowed_tools:
        return {"success": False, "error": "Permission denied"}

    # Log params for search tools (skip reply/end_turn which are noisy)
    if tool_name not in ("reply", "end_turn"):
        logger.info(f"[AdminChat] Executing tool: {tool_name} {tool_input}")
    else:
        logger.info(f"[AdminChat] Executing tool: {tool_name}")

    trusted_tool_input = dict(tool_input)
    resolved_guild_id = trusted_guild_id if requester_id is not None else None
    visible_channels: Optional[Set[int]] = None

    if requester_id is not None and trusted_guild_id is not None:
        trusted_tool_input['guild_id'] = trusted_guild_id
        if tool_name in {"find_messages", "inspect_message", "get_active_channels", "get_daily_summaries"}:
            visible_channels = await _get_visible_channel_ids(bot, trusted_guild_id, requester_id)
            # Allow the requester to read their own DM with the bot via live=true.
            if dm_channel_id is not None:
                if visible_channels is None:
                    visible_channels = {dm_channel_id}
                else:
                    visible_channels = set(visible_channels) | {dm_channel_id}
    if requester_id is not None and tool_name in {"upsert_wallet_for_user", "resolve_admin_intent"}:
        trusted_tool_input['admin_user_id'] = requester_id

    if tool_name == "reply":
        return execute_reply(trusted_tool_input)
    elif tool_name == "end_turn":
        return execute_end_turn(trusted_tool_input)
    elif tool_name == "find_messages":
        return await execute_find_messages(
            trusted_tool_input,
            bot,
            visible_channels=visible_channels,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "inspect_message":
        return await execute_inspect_message(
            trusted_tool_input,
            bot,
            visible_channels=visible_channels,
        )
    elif tool_name == "share_to_social":
        return await execute_share_to_social(
            bot,
            sharer,
            trusted_tool_input,
            guild_id=trusted_guild_id,
        )
    elif tool_name == "list_social_routes":
        return await execute_list_social_routes(trusted_tool_input)
    elif tool_name == "create_social_route":
        return await execute_create_social_route(trusted_tool_input)
    elif tool_name == "update_social_route":
        return await execute_update_social_route(trusted_tool_input)
    elif tool_name == "delete_social_route":
        return await execute_delete_social_route(trusted_tool_input)
    elif tool_name == "list_payment_routes":
        return await execute_list_payment_routes(db_handler, trusted_tool_input)
    elif tool_name == "create_payment_route":
        return await execute_create_payment_route(db_handler, trusted_tool_input)
    elif tool_name == "update_payment_route":
        return await execute_update_payment_route(db_handler, trusted_tool_input)
    elif tool_name == "delete_payment_route":
        return await execute_delete_payment_route(db_handler, trusted_tool_input)
    elif tool_name == "list_wallets":
        return await execute_list_wallets(db_handler, trusted_tool_input)
    elif tool_name == "list_payments":
        return await execute_list_payments(db_handler, trusted_tool_input)
    elif tool_name == "get_payment_status":
        return await execute_get_payment_status(db_handler, trusted_tool_input)
    elif tool_name == "retry_payment":
        return await execute_retry_payment(bot, db_handler, trusted_tool_input)
    elif tool_name == "hold_payment":
        return await execute_hold_payment(db_handler, trusted_tool_input)
    elif tool_name == "release_payment":
        return await execute_release_payment(bot, db_handler, trusted_tool_input)
    elif tool_name == "cancel_payment":
        return await execute_cancel_payment(db_handler, trusted_tool_input)
    elif tool_name == "initiate_payment":
        return await execute_initiate_payment(bot, db_handler, trusted_tool_input)
    elif tool_name == "initiate_batch_payment":
        return await execute_initiate_batch_payment(bot, db_handler, trusted_tool_input)
    elif tool_name == "query_payment_state":
        return await execute_query_payment_state(db_handler, trusted_tool_input)
    elif tool_name == "query_wallet_state":
        return await execute_query_wallet_state(db_handler, trusted_tool_input)
    elif tool_name == "list_recent_payments":
        return await execute_list_recent_payments(db_handler, trusted_tool_input)
    elif tool_name == "upsert_wallet_for_user":
        return await execute_upsert_wallet_for_user(bot, db_handler, trusted_tool_input)
    elif tool_name == "resolve_admin_intent":
        return await execute_resolve_admin_intent(bot, db_handler, trusted_tool_input)
    elif tool_name == "get_active_channels":
        return await execute_get_active_channels(
            trusted_tool_input,
            visible_channels=visible_channels,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "get_daily_summaries":
        return await execute_get_daily_summaries(
            trusted_tool_input,
            visible_channels=visible_channels,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "get_live_update_status":
        return await execute_get_live_update_status(
            trusted_tool_input,
            resolved_guild_id=resolved_guild_id,
        )
    elif tool_name == "get_member_info":
        return await execute_get_member_info(db_handler, trusted_tool_input)
    elif tool_name == "update_member_socials":
        return await execute_update_member_socials(db_handler, trusted_tool_input)
    elif tool_name == "mute_speaker":
        return await execute_mute_speaker(bot, db_handler, trusted_tool_input)
    elif tool_name == "unmute_speaker":
        return await execute_unmute_speaker(bot, db_handler, trusted_tool_input)
    elif tool_name == "get_bot_status":
        return await execute_get_bot_status(bot)
    elif tool_name == "search_logs":
        return await execute_search_logs(trusted_tool_input)
    elif tool_name == "send_message":
        return await execute_send_message(bot, trusted_tool_input)
    elif tool_name == "edit_message":
        return await execute_edit_message(bot, trusted_tool_input)
    elif tool_name == "delete_message":
        return await execute_delete_message(bot, trusted_tool_input)
    elif tool_name == "upload_file":
        return await execute_upload_file(bot, trusted_tool_input)
    elif tool_name == "resolve_user":
        return await execute_resolve_user(trusted_tool_input)
    elif tool_name == "query_table":
        return await execute_query_table(trusted_tool_input)
    elif tool_name == "download_media":
        return await execute_download_media(bot, trusted_tool_input)
    elif tool_name == "run_media_command":
        return await execute_run_media_command(trusted_tool_input)
    elif tool_name == "list_media_files":
        return await execute_list_media_files(trusted_tool_input)
    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
