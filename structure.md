# BNDC Bot: Developer Guide

> **How to Use This Guide**  
> • Skim the Tech Stack & Feature tables to orient yourself.  
> • Use the Directory Tree to find specific files.  
> • When in doubt, the source of truth is always the code – this guide just points you in the right direction.

> **When to Update This Guide**  
> • Add, delete, or rename files/directories.  
> • Add new features or significantly refactor existing ones.  
> • Modify database schema or add migrations.  
> • Change environment variables or deployment config.  
> • Any change that would confuse a new dev skimming this file.

> **Who This Guide Is For**  
> • 🤖 AI assistants + 👨‍💻 Human developers

---

## Table of Contents
- [Tech Stack](#tech-stack)
- [Key Concepts](#key-concepts)
- [Features Overview](#features-overview)
- [Directory Structure](#directory-structure)
- [Supabase Schema](#supabase-schema)

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| **Bot Framework** | Discord.py | Discord bot with cogs architecture |
| **Database** | Supabase (PostgreSQL) | Message archive, member profiles, summaries, logs |
| **LLM Providers** | Claude, OpenAI, Gemini | Summaries, content analysis, dispute resolution |
| **Deployment** | Railway + Docker | Production hosting with Nixpacks builds |
| **Logging** | Python logging → Supabase | Centralized logs with 48h retention |

### Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `DISCORD_BOT_TOKEN` | Bot authentication |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Database connection |
| `REACTION_WATCHLIST` | JSON config for reaction-triggered workflows |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM provider keys |
| `ADMIN_USER_ID` | Discord user ID for admin chat feature (DM-only access) |
| `DEV_MODE` | Enables verbose logging, skips "already summarized" checks |
| `OPENMUSE_FEATURING_CHANNEL_ID` | Channel ID for OpenMuse featuring posts |
| `NO_SHARING_ROLE_ID` | Discord role ID assigned to users who opt out of content sharing |

---

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Cogs** | Discord.py's modular extension system. Each feature has a `_cog.py` that registers commands/listeners with the bot. |
| **Feature Structure** | Features live in `src/features/[name]/` with: core logic (`reactor.py`) + Discord integration (`reactor_cog.py`) + optional `subfeatures/` for complex actions. |
| **Reaction Watchlist** | JSON env var (`REACTION_WATCHLIST`) that configures which emoji reactions trigger which actions. Central routing for all reaction-based workflows. |
| **Archiving** | Messages are archived from Discord → Supabase via `archive_runner.py`. Can run on-demand or scheduled. |
| **Live Updates** | Agentic editorial loop over archived messages. Accepted updates append as feed items in the configured summary/live-updates channel and are audited in `live_update_*` tables. |
| **Legacy Summaries** | Historical/backfill-only daily digests in `daily_summaries` and old summary thread mappings. These are not the active overview system. |
| **Member Permissions** | Two boolean flags with TRUE defaults: `include_in_updates` (can be mentioned in summaries/digests) and `allow_content_sharing` (content can be shared externally). When `allow_content_sharing=FALSE`, a Discord role is assigned to make opt-out visible. |

---

## Features Overview

| Feature | Location | Purpose |
|---------|----------|---------|
| **Admin** | `src/features/admin/` | Owner commands: reload cogs, diagnostics, sync management |
| **Admin Chat** | `src/features/admin_chat/` | Claude-powered DM chat for ADMIN_USER_ID with tool use (search messages, share to social, etc.) |
| **Answering** | `src/features/answering/` | RAG-based Q&A over archived messages |
| **Archive** | `src/features/archive/` | Commands to trigger message archiving |
| **Curating** | `src/features/curating/` | Identify & manage high-quality posts for external sharing |
| **Logging** | `src/features/logging/` | Real-time message logging to Supabase |
| **Reacting** | `src/features/reacting/` | Reaction-triggered workflows (tweets, uploads, disputes, etc.) |
| **Relaying** | `src/features/relaying/` | Webhook relay to external services |
| **Sharing** | `src/features/sharing/` | Social media cross-posting (Twitter, etc.) |
| **Summarising** | `src/features/summarising/` | Live-update editor, independent top-creations loop, and legacy summary backfill helpers |

---

## Directory Structure

```
.
├── main.py                      # Entry point – bootstraps bot, loads cogs
├── requirements.txt             # Python dependencies
├── Procfile / railway.json      # Railway deployment config
├── Dockerfile / nixpacks.toml   # Container build config
│
├── scripts/                     # One-off maintenance utilities
│   ├── archive_discord.py          # Bulk archive messages to Supabase
│   ├── logs.py                      # Unified log monitoring tool (health, live-update, summary legacy, errors, tail)
│   └── ...                          # Other utilities (see tree below)
│
├── ../supabase/migrations/       # Workspace-level Supabase repo (separate git root) holds the canonical timestamped SQL migrations
│
└── src/
    ├── common/                      # Shared infrastructure
    │   ├── content_moderator.py         # Image content moderation (WaveSpeed AI API)
    │   ├── db_handler.py                # Database abstraction layer
    │   ├── discord_utils.py             # Discord API helpers (safe_send_message, etc.)
    │   ├── error_handler.py             # @handle_errors decorator
    │   ├── log_handler.py               # Centralized logging setup
    │   ├── schema.py                    # Pydantic models for DB tables
    │   ├── storage_handler.py           # Supabase write operations
    │   ├── openmuse_interactor.py       # OpenMuse media uploads
    │   └── llm/                         # LLM client abstractions
    │       ├── __init__.py                  # Factory (get_llm_client)
    │       ├── claude_client.py
    │       ├── openai_client.py
    │       └── gemini_client.py
    │
    └── features/                    # Bot capabilities (one per subdirectory)
        ├── admin/
        │   └── admin_cog.py
        ├── admin_chat/
        │   ├── admin_chat_cog.py    # Discord DM listener for ADMIN_USER_ID
        │   ├── agent.py              # Claude agent with tool use loop (Arnold pattern)
        │   └── tools.py              # Tool definitions & executors (search, share, refresh_media, etc.)
        ├── answering/
        │   └── answerer.py
        ├── archive/
        │   └── archive_cog.py
        ├── curating/
        │   ├── curator.py
        │   └── curator_cog.py
        ├── logging/
        │   ├── logger.py
        │   └── logger_cog.py
        ├── reacting/
        │   ├── reactor.py               # Watchlist matching & action dispatch
        │   ├── reactor_cog.py
        │   └── subfeatures/
        │       ├── dispute_resolver.py      # LLM-powered dispute resolution
        │       ├── message_linker.py        # Unfurl Discord message links
        │       ├── openmuse_uploader.py     # Upload media to OpenMuse
        │       ├── permission_handler.py    # Curation consent flow
        │       ├── tweet_sharer_bridge.py   # Bridge to sharing feature
        │       └── workflow_uploader.py     # ComfyUI workflow uploads
        ├── relaying/
        │   ├── relayer.py
        │   └── relaying_cog.py
        ├── sharing/
        │   ├── sharer.py
        │   ├── sharing_cog.py
        │   └── subfeatures/
        │       ├── content_analyzer.py      # Extract hashtags, metadata
        │       ├── notify_user.py           # DM users about shares
        │       └── social_poster.py         # Platform-specific posting
        └── summarising/
            ├── summariser.py
            ├── summariser_cog.py
            └── subfeatures/
                ├── news_summary.py
                ├── top_art_sharing.py
                └── top_generations.py
```

### Scripts Reference

| Script | Purpose |
|--------|---------|
| `archive_discord.py` | Bulk archive messages & attachments to Supabase |
| `analyze_channels.py` | Analyse channels with LLM, export stats |
| `backfill_reactions.py` | Populate missing reaction records |
| `logs.py` | Unified log monitoring: `health`, `live-update`, `summary` legacy, `errors`, `recent`, `search`, `tail`, `stats` |

---

## Supabase Schema

### Core Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `discord_messages` | Archived messages | `message_id` (PK), `channel_id`, `author_id`, `content`, `created_at`, `attachments` (JSONB), `reaction_count`, `is_deleted` |
| `discord_members` | Member profiles & permissions | `member_id` (PK), `username`, `global_name`, `twitter_handle`, `reddit_handle`, `include_in_updates` (default TRUE), `allow_content_sharing` (default TRUE) |
| `discord_channels` | Channel metadata | `channel_id` (PK), `channel_name`, `description`, `suitable_posts`, `unsuitable_posts`, `enriched` |
| `live_update_editor_runs` | Active live-update editor run audit | `run_id` (PK), `guild_id`, `trigger`, `status`, `live_channel_id`, `candidate_count`, `accepted_count`, `checkpoint_before`, `checkpoint_after`, `metadata` |
| `live_update_candidates` | Generated live-update candidates | `candidate_id` (PK), `run_id`, `guild_id`, `update_type`, `title`, `body`, `source_message_ids`, `author_context_snapshot`, `duplicate_key`, `status` |
| `live_update_decisions` | Accept/reject/defer/duplicate/failed-post decisions | `decision_id` (PK), `run_id`, `candidate_id`, `decision`, `reason`, `duplicate_key`, `decision_payload` |
| `live_update_feed_items` | Posted logical feed items | `feed_item_id` (PK), `candidate_id`, `live_channel_id`, `discord_message_ids` (ordered JSON array), `duplicate_key`, `status` |
| `live_update_editorial_memory` | Editorial memory for active live updates | `memory_id` (PK), `guild_id`, `memory_key`, `subject_type`, `summary`, `state`, `last_seen_at` |
| `live_update_watchlist` | Editorial watchlist for active live updates | `watch_id` (PK), `guild_id`, `watch_key`, `subject_type`, `criteria`, `status`, `priority` |
| `live_update_duplicate_state` | Duplicate suppression state | `duplicate_key` (PK), `guild_id`, `last_seen_candidate_id`, `feed_item_id`, `status`, `seen_count` |
| `live_update_checkpoints` | Archived-message checkpoints for live updates | `checkpoint_key` (PK), `guild_id`, `channel_id`, `last_message_id`, `last_run_id`, `state` |
| `live_top_creation_runs` | Independent top-creations run audit | `top_creation_run_id` (PK), `guild_id`, `trigger`, `status`, `candidate_count`, `posted_count`, `checkpoint_after` |
| `live_top_creation_posts` | Independent top-creations posts | `top_creation_post_id` (PK), `guild_id`, `source_kind`, `source_message_id`, `discord_message_ids` (ordered JSON array), `duplicate_key`, `status` |
| `live_top_creation_checkpoints` | Independent top-creations checkpoints | `checkpoint_key` (PK), `guild_id`, `last_source_message_id`, `state` |
| `daily_summaries` | Legacy daily summary history/backfill input | `daily_summary_id` (PK), `date`, `channel_id`, `full_summary`, `short_summary`, `included_in_main_summary`, `dev_mode` |
| `channel_summary` | Legacy summary thread mapping | `channel_id` (PK), `summary_thread_id` |
| `system_logs` | Application logs | `id` (PK), `timestamp`, `level`, `logger_name`, `message`, `exception` |
| `sync_status` | Sync state tracking | `table_name`, `last_sync_timestamp`, `sync_status` |

### Views

| View | Purpose |
|------|---------|
| `recent_messages` | Last 7 days of messages with author/channel names |
| `message_stats` | Per-channel message counts and date ranges |
| `recent_errors` | ERROR/CRITICAL logs from last 24 hours |
| `log_stats` | Hourly log counts by level |

### Edge Functions

| Function | Purpose | Secrets Required |
|----------|---------|------------------|
| `refresh-media-urls` | Refresh expired Discord CDN attachment URLs | `DISCORD_BOT_TOKEN` |

**Deployment:**
```bash
# Deploy function
supabase functions deploy refresh-media-urls

# Set secrets
supabase secrets set DISCORD_BOT_TOKEN=your_token_here
```

**Usage:**
```bash
# Refresh URLs for a specific message
curl -X POST 'https://<project>.supabase.co/functions/v1/refresh-media-urls' \
  -H 'Authorization: Bearer <anon_key>' \
  -H 'Content-Type: application/json' \
  -d '{"message_id": "123456789"}'

# Response:
# {
#   "success": true,
#   "message_id": "123456789",
#   "attachments": [{"id": "...", "filename": "image.png", "url": "https://cdn.discordapp.com/...", ...}],
#   "urls_updated": 1
# }
```

### Notes
- All tables have RLS enabled (service-role access only)
- `system_logs` auto-cleaned hourly via `pg_cron` (48h retention)
- Full-text search on `discord_messages.content` and `system_logs.message`
