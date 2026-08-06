"""Validate member tier labels (roles) against DB status — read-only.

Checks every non-bot guild member's tier role(s) against their DB
member_status and reports:
  a. members holding a tier role that does NOT match their DB status
  b. members holding 2+ tier roles
  c. members holding a tier role with no DB status row (orphan)

Exits 0 if no violations, 1 if any.

Usage:
    python scripts/validate_tier_labels.py
"""
import logging
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.common.db_handler import DatabaseHandler

load_dotenv()

logger = logging.getLogger("ValidateLabels")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("TARGET_GUILD_ID", os.getenv("GUILD_ID", "0"))) or None

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

STATUS_TO_ROLE = {'newbie': 'newbie', 'speaker': 'speaker', 'moderated': 'moderated'}


def _int_env(name: str) -> int | None:
    val = os.getenv(name)
    return int(val) if val else None


@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    if not GUILD_ID:
        logger.error("TARGET_GUILD_ID or GUILD_ID must be configured")
        await client.close()
        sys.exit(1)

    db = DatabaseHandler()
    newbie_id = _int_env('NEWBIE_ROLE_ID')
    speaker_id = _int_env('SPEAKER_ROLE_ID')
    moderated_id = _int_env('MODERATED_ROLE_ID')

    guild = client.get_guild(GUILD_ID)
    if not guild:
        logger.error(f"Guild {GUILD_ID} not found")
        await client.close()
        sys.exit(1)

    tier_ids = {'newbie': newbie_id, 'speaker': speaker_id, 'moderated': moderated_id}
    if not all(tier_ids.values()):
        logger.error("NEWBIE_ROLE_ID / SPEAKER_ROLE_ID / MODERATED_ROLE_ID must all resolve.")
        await client.close()
        sys.exit(1)

    status_map = db.get_guild_member_statuses(GUILD_ID)
    logger.info(f"Loaded {len(status_map)} DB status rows")

    members = [m for m in guild.members if not m.bot]
    violations = []
    counts = {}
    for m in members:
        held = [k for k, rid in tier_ids.items() if rid and any(r.id == rid for r in m.roles)]
        counts[len(held)] = counts.get(len(held), 0) + 1
        if len(held) > 1:
            violations.append(f"member {m.name} ({m.id}) holds {len(held)} tier roles: {held}")
            continue
        db_row = status_map.get(m.id)
        db_status = (db_row or {}).get('member_status')
        expected_role = STATUS_TO_ROLE.get(db_status)
        if held:
            role_key = held[0]
            if expected_role != role_key:
                violations.append(
                    f"member {m.name} ({m.id}) role={role_key} but DB status={db_status or 'MISSING'}"
                )
        elif db_status and db_status in STATUS_TO_ROLE:
            violations.append(
                f"member {m.name} ({m.id}) DB status={db_status} but holds NO tier role"
            )
        elif not db_row:
            # No DB row and no tier role: brand-new member, not yet assigned. Not a violation.
            pass

    logger.info(
        f"Tier-role distribution: {counts.get(0, 0)} members with 0 tier roles, "
        f"{counts.get(1, 0)} with exactly 1, {counts.get(2, 0)} with 2+, "
        f"({counts.get(1, 0) + counts.get(2, 0)} total with a role)"
    )
    if violations:
        logger.error(f"LABEL VALIDATION FAILED: {len(violations)} violation(s):")
        for v in violations[:20]:
            logger.error(f"  ✗ {v}")
        await client.close()
        sys.exit(1)
    logger.info("LABEL VALIDATION PASSED — no incorrect tier labels.")
    await client.close()


client.run(TOKEN)
