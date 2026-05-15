#!/usr/bin/env python3
"""
Unified debug and monitoring utility for the Discord bot.

Combines log analysis, database queries, and deployment debugging.

Usage:
    # Quick checks
    python scripts/debug.py health              # Health check (errors, warnings, bot activity)
    python scripts/debug.py errors              # Show all errors
    python scripts/debug.py errors --hours 6    # Errors from last 6 hours
    
    # Log analysis
    python scripts/debug.py search "AdminChat"  # Search logs by message
    python scripts/debug.py tail                # Live tail of logs
    python scripts/debug.py trace live-update   # Trace the active live-update editor
    
    # Database queries
    python scripts/debug.py db-stats            # Database statistics
    python scripts/debug.py channels            # List channels
    python scripts/debug.py messages --channel ID
    python scripts/debug.py channel-info ID     # Details about a channel
    
    # Environment & config
    python scripts/debug.py env                 # Show env config
    
    # Railway/deployment
    python scripts/debug.py bot-status          # Bot health via endpoint
    python scripts/debug.py railway-status      # Railway service status
    python scripts/debug.py deployments         # Deployment history
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

# ========== Colors ==========
COLORS = {
    'DEBUG': '\033[90m',     # Gray
    'INFO': '\033[32m',      # Green
    'WARNING': '\033[33m',   # Yellow
    'ERROR': '\033[31m',     # Red
    'CRITICAL': '\033[35m',  # Magenta
}
RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'
GREEN = '\033[32m'
YELLOW = '\033[33m'
RED = '\033[31m'
CYAN = '\033[36m'


# ========== Utilities ==========

def get_client():
    """Get Supabase client."""
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        print(f"{RED}Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set{RESET}")
        sys.exit(1)
    return create_client(url, key)


def format_log(log, verbose=False):
    """Format a single log entry for display."""
    ts = log['timestamp'][:19].replace('T', ' ')
    level = log['level']
    color = COLORS.get(level, '')
    logger = log.get('logger_name', 'Unknown')
    message = log['message']
    
    output = f"{DIM}{ts}{RESET} {color}{BOLD}{level:8}{RESET} {DIM}[{logger}]{RESET}\n"
    
    if verbose or level in ('ERROR', 'CRITICAL'):
        output += f"  {message}\n"
    else:
        msg_preview = message[:150] + ('...' if len(message) > 150 else '')
        output += f"  {msg_preview}\n"
    
    if log.get('exception') and (verbose or level in ('ERROR', 'CRITICAL')):
        output += f"\n  {color}Exception:{RESET}\n"
        for line in log['exception'].split('\n')[:15]:
            output += f"    {DIM}{line}{RESET}\n"
    
    return output


def format_row(row, max_width=100):
    """Format a row for display, truncating long values."""
    formatted = {}
    for k, v in row.items():
        if isinstance(v, str) and len(v) > max_width:
            v = v[:max_width] + "..."
        formatted[k] = v
    return formatted


# ========== Health & Monitoring Commands ==========

def cmd_health(args):
    """Quick health check - shows errors, warnings, and bot activity."""
    supabase = get_client()
    
    print(f"\n{BOLD}🏥 System Health Check{RESET}")
    print("=" * 60)
    
    now = datetime.utcnow()
    last_1h = (now - timedelta(hours=1)).isoformat()
    last_6h = (now - timedelta(hours=6)).isoformat()
    last_24h = (now - timedelta(hours=24)).isoformat()
    
    # Error counts
    print(f"\n{BOLD}🚨 Errors & Warnings:{RESET}")
    for hours, since, label in [(1, last_1h, 'Last hour'), (6, last_6h, 'Last 6h'), (24, last_24h, 'Last 24h')]:
        err_resp = supabase.table('system_logs').select('id', count='exact').in_('level', ['ERROR', 'CRITICAL']).gte('timestamp', since).execute()
        err_count = err_resp.count or 0
        
        warn_resp = supabase.table('system_logs').select('id', count='exact').eq('level', 'WARNING').gte('timestamp', since).execute()
        warn_count = warn_resp.count or 0
        
        if err_count > 0:
            print(f"  {label}: {RED}{err_count} errors{RESET}, {YELLOW}{warn_count} warnings{RESET}")
        elif warn_count > 0:
            print(f"  {label}: {GREEN}0 errors{RESET}, {YELLOW}{warn_count} warnings{RESET}")
        else:
            print(f"  {label}: {GREEN}✓ No errors or warnings{RESET}")
    
    # Recent errors
    err_response = supabase.table('system_logs').select('*').in_('level', ['ERROR', 'CRITICAL']).gte('timestamp', last_24h).order('timestamp', desc=True).limit(3).execute()
    if err_response.data:
        print(f"\n{BOLD}📋 Recent Errors (last 24h):{RESET}")
        for log in err_response.data:
            ts = log['timestamp'][:16].replace('T', ' ')
            msg = log['message'][:100] + ('...' if len(log['message']) > 100 else '')
            print(f"  {DIM}{ts}{RESET} {msg}")
    
    # Bot activity
    print(f"\n{BOLD}🤖 Bot Activity:{RESET}")
    recent_logs = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    if recent_logs.data:
        last_log = recent_logs.data[0]['timestamp'][:19].replace('T', ' ')
        last_log_dt = datetime.fromisoformat(recent_logs.data[0]['timestamp'][:19])
        age_mins = (now - last_log_dt).total_seconds() / 60
        
        if age_mins < 5:
            print(f"  Last log: {GREEN}{last_log} ({age_mins:.0f}m ago) ✓ Active{RESET}")
        elif age_mins < 30:
            print(f"  Last log: {YELLOW}{last_log} ({age_mins:.0f}m ago){RESET}")
        else:
            print(f"  Last log: {RED}{last_log} ({age_mins:.0f}m ago) ⚠️ No recent activity{RESET}")
    else:
        print(f"  {RED}No logs found{RESET}")

    # Active overview system
    print(f"\n{BOLD}📝 Live Update Editor:{RESET}")
    try:
        live_runs = (
            supabase.table('live_update_editor_runs')
            .select('run_id,status,trigger,created_at,error_message')
            .gte('created_at', last_24h)
            .order('created_at', desc=True)
            .limit(1)
            .execute()
        )
        feed_count = (
            supabase.table('live_update_feed_items')
            .select('feed_item_id', count='exact')
            .gte('created_at', last_24h)
            .execute()
        )
        if live_runs.data:
            latest = live_runs.data[0]
            status = latest.get('status')
            color = GREEN if status in {'completed', 'skipped'} else RED if status == 'failed' else YELLOW
            print(f"  Latest run: {color}{status}{RESET} trigger={latest.get('trigger')} at {latest.get('created_at')}")
        else:
            print(f"  {YELLOW}No live-update editor runs in the last 24h{RESET}")
        print(f"  Live feed items posted in last 24h: {feed_count.count or 0}")
        print(f"  {DIM}daily_summaries is legacy history only; active overview state is live_update_* tables.{RESET}")
    except Exception as e:
        print(f"  {YELLOW}Could not inspect live-update tables: {e}{RESET}")
    
    print("=" * 60)


def cmd_errors(args):
    """Show errors - ALL by default, or filtered by hours."""
    supabase = get_client()
    
    query = supabase.table('system_logs').select('*').in_('level', ['ERROR', 'CRITICAL'])
    
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('timestamp', since)
        time_desc = f"last {args.hours} hours"
    else:
        time_desc = "all time"
    
    response = query.order('timestamp', desc=True).limit(args.limit).execute()
    
    if not response.data:
        print(f"\n{GREEN}✅ No errors found ({time_desc}){RESET}")
        return
    
    print(f"\n{RED}{BOLD}🚨 {len(response.data)} errors ({time_desc}):{RESET}\n")
    print("-" * 60)
    
    for log in response.data:
        print(format_log(log, verbose=args.verbose))
        print("-" * 60)


def cmd_search(args):
    """Search logs by message or logger."""
    supabase = get_client()
    
    query = supabase.table('system_logs').select('*')
    
    if args.pattern:
        query = query.ilike('message', f'%{args.pattern}%')
    
    if args.logger:
        query = query.ilike('logger_name', f'%{args.logger}%')
    
    if args.level:
        query = query.eq('level', args.level.upper())
    
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('timestamp', since)
    
    response = query.order('timestamp', desc=True).limit(args.limit).execute()
    
    if not response.data:
        print("No matching logs found")
        return
    
    print(f"\n{BOLD}🔍 Found {len(response.data)} matching logs:{RESET}\n")
    print("-" * 60)
    
    for log in response.data:
        print(format_log(log, verbose=args.verbose))
        print("-" * 60)


def cmd_tail(args):
    """Live tail of logs (polling)."""
    supabase = get_client()
    
    print(f"{BOLD}📡 Tailing logs (Ctrl+C to stop)...{RESET}\n")
    
    response = supabase.table('system_logs').select('timestamp').order('timestamp', desc=True).limit(1).execute()
    last_ts = response.data[0]['timestamp'] if response.data else datetime.utcnow().isoformat()
    
    seen_ids = set()
    level_order = {'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3, 'CRITICAL': 4}
    min_level = level_order.get(args.level.upper() if args.level else 'DEBUG', 0)
    
    try:
        while True:
            query = supabase.table('system_logs').select('*').gt('timestamp', last_ts)
            response = query.order('timestamp', desc=False).limit(50).execute()
            
            for log in response.data:
                log_id = log.get('id')
                if log_id and log_id not in seen_ids:
                    log_level = level_order.get(log['level'], 1)
                    if log_level < min_level:
                        continue
                    
                    seen_ids.add(log_id)
                    ts = log['timestamp'][:19].replace('T', ' ')
                    level = log['level']
                    color = COLORS.get(level, '')
                    logger = log.get('logger_name', '?')
                    msg = log['message'][:120] + ('...' if len(log['message']) > 120 else '')
                    print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {DIM}[{logger}]{RESET} {msg}")
                    
                    if log['timestamp'] > last_ts:
                        last_ts = log['timestamp']
            
            if len(seen_ids) > 1000:
                seen_ids = set(list(seen_ids)[-500:])
            
            time.sleep(args.interval)
            
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


def cmd_trace(args):
    """Trace a specific feature/operation by keyword."""
    supabase = get_client()
    
    FEATURES = {
        'live-update': ['LiveUpdateEditor', 'live_update', 'live-update', 'live_update_editor_runs', 'live_update_feed_items'],
        'summary': ['topic editor', 'TopicEditor', 'topic_editor_runs', 'topics', 'live-update'],
        'archive': ['[Archive]', 'archive_discord', 'archiving'],
        'share': ['sharer', 'sharing', 'twitter', 'social_poster', 'tweet'],
        'react': ['reactor', 'reaction', 'watchlist'],
        'llm': ['claude', 'anthropic', 'openai', 'gemini', 'llm', 'rate limit'],
        'admin': ['AdminChat', 'admin_chat', 'admin'],
    }
    
    feature = args.feature.lower()
    if feature in FEATURES:
        keywords = FEATURES[feature]
        print(f"\n{BOLD}🔍 Tracing '{feature}' feature{RESET}")
    else:
        keywords = [args.feature]
        print(f"\n{BOLD}🔍 Tracing custom keyword: '{args.feature}'{RESET}")
    
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
    else:
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    
    print(f"Keywords: {', '.join(keywords)}")
    print(f"Time range: since {since[:19]}")
    print("=" * 60)
    
    all_logs = []
    seen_ids = set()
    
    for keyword in keywords[:3]:
        try:
            response = supabase.table('system_logs').select('*').ilike('message', f'%{keyword}%').gte('timestamp', since).order('timestamp', desc=False).limit(100).execute()
            for log in response.data:
                if log['id'] not in seen_ids:
                    seen_ids.add(log['id'])
                    all_logs.append(log)
        except Exception as e:
            print(f"{DIM}Warning: search for '{keyword}' failed: {e}{RESET}", file=sys.stderr)
    
    all_logs.sort(key=lambda x: x['timestamp'])
    
    if not all_logs:
        print(f"\n{DIM}No logs found for '{feature}'{RESET}")
        return
    
    print(f"\n{BOLD}Found {len(all_logs)} related logs:{RESET}\n")
    
    for log in all_logs:
        ts = log['timestamp'][:19].replace('T', ' ')
        level = log['level']
        color = COLORS.get(level, '')
        msg = log['message'][:140] + ('...' if len(log['message']) > 140 else '')
        print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {msg}")
    
    print("=" * 60)


def cmd_recent(args):
    """Show most recent logs."""
    supabase = get_client()
    
    query = supabase.table('system_logs').select('*')
    
    if args.level:
        query = query.eq('level', args.level.upper())
    
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('timestamp', since)
    
    response = query.order('timestamp', desc=True).limit(args.limit).execute()
    
    if not response.data:
        print("No logs found")
        return
    
    print(f"\n{BOLD}📋 Last {len(response.data)} logs:{RESET}\n")
    
    for log in reversed(response.data):
        ts = log['timestamp'][:19].replace('T', ' ')
        level = log['level']
        color = COLORS.get(level, '')
        logger = log.get('logger_name', '?')
        msg = log['message'][:120] + ('...' if len(log['message']) > 120 else '')
        print(f"{DIM}{ts}{RESET} {color}{level:8}{RESET} {DIM}[{logger}]{RESET} {msg}")


def cmd_stats(args):
    """Show detailed log statistics."""
    supabase = get_client()
    
    print(f"\n{BOLD}📊 Log Statistics{RESET}")
    print("=" * 60)
    
    response = supabase.table('system_logs').select('id', count='exact').execute()
    total = response.count or 0
    print(f"Total logs: {total:,}")
    
    print(f"\n{BOLD}By Level:{RESET}")
    for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        response = supabase.table('system_logs').select('id', count='exact').eq('level', level).execute()
        count = response.count or 0
        pct = (count / total * 100) if total > 0 else 0
        color = COLORS.get(level, '')
        bar = '█' * int(pct / 2) if pct > 0 else ''
        print(f"  {color}{level:10}{RESET} {count:>8,} ({pct:5.1f}%) {bar}")
    
    yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
    response = supabase.table('system_logs').select('id', count='exact').gte('timestamp', yesterday).execute()
    last_24h = response.count or 0
    print(f"\nLast 24 hours: {last_24h:,}")
    
    print("=" * 60)


# ========== Database Commands ==========

def cmd_db_stats(args):
    """Show database statistics."""
    supabase = get_client()
    
    print(f"\n{BOLD}📊 Database Statistics{RESET}")
    print("=" * 60)
    
    tables = {
        'discord_messages': 'Messages',
        'discord_channels': 'Channels',
        'members': 'Members',
        'live_update_editor_runs': 'Live Update Runs',
        'live_update_candidates': 'Live Update Candidates',
        'live_update_decisions': 'Live Update Decisions',
        'live_update_feed_items': 'Live Update Feed Items',
        'live_update_editorial_memory': 'Live Editorial Memory',
        'live_update_duplicate_state': 'Live Duplicate State',
        'live_top_creation_runs': 'Live Top Creation Runs',
        'live_top_creation_posts': 'Live Top Creation Posts',
        'daily_summaries': 'Legacy Daily Summaries',
        'system_logs': 'System Logs',
    }
    
    print("\nTable Sizes:")
    for table, name in tables.items():
        try:
            result = supabase.table(table).select('*', count='exact').limit(1).execute()
            count = result.count if result.count is not None else 0
            print(f"  {name:20} {count:>10,} rows")
        except Exception:
            print(f"  {name:20} {'Error':>10}")
    
    print("\nRecent Activity (last 24 hours):")
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    
    try:
        msgs = supabase.table('discord_messages').select('message_id', count='exact').gte('created_at', cutoff).execute()
        print(f"  New messages:        {msgs.count:>10,}")
    except Exception:
        print(f"  New messages:        {'Error':>10}")
    
    try:
        errs = supabase.table('system_logs').select('id', count='exact').gte('timestamp', cutoff).eq('level', 'ERROR').execute()
        print(f"  Errors logged:       {errs.count:>10,}")
    except Exception:
        print(f"  Errors logged:       {'Error':>10}")

    try:
        runs = supabase.table('live_update_editor_runs').select('run_id', count='exact').gte('created_at', cutoff).execute()
        print(f"  Live editor runs:    {runs.count:>10,}")
    except Exception:
        print(f"  Live editor runs:    {'Error':>10}")

    try:
        feed_items = supabase.table('live_update_feed_items').select('feed_item_id', count='exact').gte('created_at', cutoff).execute()
        print(f"  Live feed items:     {feed_items.count:>10,}")
    except Exception:
        print(f"  Live feed items:     {'Error':>10}")
    
    print("=" * 60)


def cmd_channels(args):
    """List channels from database."""
    supabase = get_client()
    
    query = supabase.table('discord_channels').select('channel_id, channel_name, category_name')
    results = query.limit(args.limit).execute()
    
    if not results.data:
        print("No channels found")
        return
    
    print(f"\n{BOLD}📺 Channels ({len(results.data)}):{RESET}\n")
    for ch in results.data:
        cat = ch.get('category_name', 'No category')
        print(f"  {ch['channel_id']} - {ch.get('channel_name', 'Unknown')} ({cat})")


def cmd_messages(args):
    """List messages from database."""
    supabase = get_client()
    
    query = supabase.table('discord_messages').select('message_id, channel_id, author_id, content, created_at')
    
    if args.channel:
        query = query.eq('channel_id', args.channel)
    
    if args.hours:
        since = (datetime.utcnow() - timedelta(hours=args.hours)).isoformat()
        query = query.gte('created_at', since)
    
    results = query.order('created_at', desc=True).limit(args.limit).execute()
    
    if not results.data:
        print("No messages found")
        return
    
    print(f"\n{BOLD}💬 Messages ({len(results.data)}):{RESET}\n")
    for msg in results.data:
        ts = msg['created_at'][:19].replace('T', ' ') if msg.get('created_at') else '?'
        content = (msg.get('content') or '')[:80]
        print(f"  {DIM}{ts}{RESET} [{msg['channel_id']}] {content}")


def cmd_channel_info(args):
    """Get details about a specific channel."""
    if not args.channel_id:
        print("Error: channel-info requires a channel ID")
        return
    
    supabase = get_client()
    channel_id = int(args.channel_id)
    
    result = supabase.table('discord_channels').select('*').eq('channel_id', channel_id).limit(1).execute()
    
    if result.data:
        print(f"\n{BOLD}📺 Channel {channel_id}:{RESET}\n")
        for k, v in result.data[0].items():
            print(f"  {k}: {v}")
    else:
        print(f"\n❌ Channel {channel_id} not found in database")


# ========== Environment Commands ==========

def cmd_env(args):
    """Show environment configuration."""
    print(f"\n{BOLD}🔧 Environment Configuration{RESET}")
    print("=" * 60)
    
    env_vars = [
        ("GUILD_ID", False),
        ("SUMMARY_CHANNEL_ID", False),
        ("TOP_GENS_ID", False),
        ("ART_CHANNEL_ID", False),
        ("ADMIN_USER_ID", False),
        ("DEV_MODE", False),
    ]
    
    print("\n  Key IDs:")
    for var, _ in env_vars:
        val = os.getenv(var)
        if val:
            print(f"    {var} = {val}")
        else:
            print(f"    {var} = {DIM}(not set){RESET}")
    
    print("\n  Credentials:")
    for var in ["DISCORD_BOT_TOKEN", "SUPABASE_URL", "ANTHROPIC_API_KEY"]:
        val = os.getenv(var)
        if val:
            print(f"    {var} = {GREEN}✓ set{RESET}")
        else:
            print(f"    {var} = {RED}✗ not set{RESET}")
    
    print("=" * 60)


# ========== Railway Commands ==========

def cmd_bot_status(args):
    """Check bot status via health endpoint."""
    print(f"\n{BOLD}🤖 Bot Status{RESET}")
    print("=" * 60)
    
    try:
        import requests
        
        result = subprocess.run(['railway', 'domain'], capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            print("❌ Could not get Railway domain")
            return
        
        import re
        output = result.stdout.strip()
        match = re.search(r'https://[^\s]+', output)
        service_url = match.group(0) if match else output
        
        if not service_url:
            print("❌ No service URL found")
            return
        
        print(f"Service URL: {service_url}")
        
        response = requests.get(f"{service_url}/health", timeout=5)
        if response.status_code == 200:
            print(f"\n{GREEN}✅ Bot is healthy{RESET}")
        else:
            print(f"\n{YELLOW}⚠️ Health check returned {response.status_code}{RESET}")
            
    except ImportError:
        print("Install 'requests' to check bot status: pip install requests")
    except subprocess.TimeoutExpired:
        print("❌ Command timed out")
    except FileNotFoundError:
        print("❌ Railway CLI not found. Install with: npm i -g @railway/cli")
    except (subprocess.SubprocessError, ConnectionError, OSError) as e:  # Subprocess/network errors
        print(f"❌ Error: {e}")


def _run_railway_cmd(title: str, cmd: list, timeout: int = 10):
    """Run a Railway CLI command with standard error handling."""
    print(f"\n{BOLD}🚂 {title}{RESET}")
    print("=" * 60)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"❌ Error: {result.stderr}")
    except FileNotFoundError:
        print("❌ Railway CLI not found. Install with: npm i -g @railway/cli")
    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:  # Subprocess errors
        print(f"❌ Error: {e}")


def cmd_railway_status(args):
    """Check Railway service status."""
    _run_railway_cmd("Railway Service Status", ['railway', 'status'])


def cmd_railway_logs(args):
    """Fetch Railway platform logs."""
    _run_railway_cmd("Railway Logs", ['railway', 'logs', '--lines', str(args.limit)], timeout=30)


def cmd_deployments(args):
    """Analyze Railway deployment history."""
    print(f"\n{BOLD}🚀 Railway Deployments{RESET}")
    print("=" * 60)
    
    try:
        result = subprocess.run(
            ['railway', 'deployment', 'list', '--limit', str(args.limit), '--json'],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            print(f"❌ Error: {result.stderr}")
            return
        
        deployments = json.loads(result.stdout)
        
        if not deployments:
            print("No deployments found.")
            return
        
        print(f"Recent {len(deployments)} deployments:\n")
        
        for d in deployments[:15]:
            ts = d.get('createdAt', '')[:19].replace('T', ' ')
            status = d.get('status', 'UNKNOWN')
            commit = d.get('meta', {}).get('commitHash', 'unknown')[:7]
            msg = d.get('meta', {}).get('commitMessage', '').split('\n')[0][:50]
            
            status_emoji = {'SUCCESS': '✅', 'FAILED': '❌', 'CRASHED': '💥'}.get(status, '❓')
            print(f"  {status_emoji} {ts} [{status:10}] {commit} {msg}")
            
    except FileNotFoundError:
        print("❌ Railway CLI not found. Install with: npm i -g @railway/cli")
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError) as e:  # Subprocess/parse errors
        print(f"❌ Error: {e}")


# ========== Main ==========

def main():
    parser = argparse.ArgumentParser(
        description='Unified debug and monitoring utility',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  health          Quick health check (errors, warnings, activity)
  errors          Show errors (--hours N to filter)
  search PATTERN  Search logs by message content
  tail            Live tail of logs
  trace FEATURE   Trace: live-update, summary (legacy), share, react, llm, admin
  recent          Show recent logs
  stats           Log statistics
  
  db-stats        Database statistics
  channels        List channels
  messages        List messages (--channel ID to filter)
  channel-info ID Details about a specific channel
  
  env             Show environment configuration
  bot-status      Check bot via health endpoint
  railway-status  Railway service status
  railway-logs    Railway platform logs
  deployments     Deployment history

Examples:
  %(prog)s health
  %(prog)s errors --hours 6
  %(prog)s search "AdminChat"
  %(prog)s trace admin --hours 1
  %(prog)s messages --channel 123456789 --limit 20
        """
    )
    
    parser.add_argument('command', help='Command to run')
    parser.add_argument('pattern', nargs='?', help='Search pattern or feature name')
    parser.add_argument('--channel', type=int, help='Filter by channel ID')
    parser.add_argument('--hours', type=int, help='Filter to last N hours')
    parser.add_argument('--limit', '-n', type=int, default=20, help='Limit results')
    parser.add_argument('--level', help='Filter by log level')
    parser.add_argument('--logger', help='Filter by logger name')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    parser.add_argument('--interval', type=float, default=2.0, help='Tail poll interval')
    parser.add_argument('channel_id', nargs='?', help='Channel ID for channel-info')
    
    # Handle 'feature' argument for trace command
    class Args:
        pass
    
    args = parser.parse_args()
    
    # Map pattern to feature for trace command
    if args.command == 'trace' and args.pattern:
        args.feature = args.pattern
    elif args.command == 'trace':
        args.feature = 'live-update'  # default
    
    commands = {
        'health': cmd_health,
        'errors': cmd_errors,
        'search': cmd_search,
        'tail': cmd_tail,
        'trace': cmd_trace,
        'recent': cmd_recent,
        'stats': cmd_stats,
        'db-stats': cmd_db_stats,
        'channels': cmd_channels,
        'messages': cmd_messages,
        'channel-info': cmd_channel_info,
        'env': cmd_env,
        'bot-status': cmd_bot_status,
        'railway-status': cmd_railway_status,
        'railway-logs': cmd_railway_logs,
        'deployments': cmd_deployments,
    }
    
    if args.command not in commands:
        print(f"Unknown command: {args.command}")
        print(f"Available: {', '.join(commands.keys())}")
        sys.exit(1)
    
    commands[args.command](args)


if __name__ == '__main__':
    main()
