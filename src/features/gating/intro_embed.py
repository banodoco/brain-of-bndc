import re
from typing import Optional

import discord

APP_MARKER_PREFIX = "app:"
_APP_MARKER_RE = re.compile(
    r"app:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def extract_approval_request_marker(message) -> Optional[str]:
    for embed in getattr(message, 'embeds', []) or []:
        footer = getattr(embed, 'footer', None)
        footer_text = getattr(footer, 'text', None)
        if not footer_text:
            continue
        match = _APP_MARKER_RE.search(footer_text)
        if match:
            return match.group(1)
    return None


def _truncate(value: Optional[str], limit: int) -> str:
    text = (value or '').strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _display_name(member_row: dict) -> str:
    return (
        member_row.get('global_name')
        or member_row.get('server_nick')
        or member_row.get('real_name')
        or member_row.get('username')
        or 'A community member'
    )


def _clean_art_title(value: Optional[str]) -> str:
    title = (value or '').strip()
    if not title or title.lower() == 'untitled':
        return ''
    return title


def build_application_embed(member_row: dict, approval_request: dict, art: Optional[dict]) -> discord.Embed:
    display_name = _display_name(member_row)
    # members.bio is the source of truth. If the applicant clears it, do not
    # resurrect stale text from bio_snapshot.
    bio = _truncate(member_row.get('bio'), 500)
    art = art or {}
    resource_name = (art.get('name') or '').strip()
    title = resource_name or f"{display_name} would like to become a speaker"
    slug = (member_row.get('username') or '').strip()
    profile_url = f"https://banodoco.ai/@{slug}" if slug else None
    description = f"*{bio}*" if bio else "No bio provided."
    embed = discord.Embed(
        title=title,
        url=profile_url,
        description=description,
        color=discord.Color.blue(),
    )

    avatar_url = member_row.get('avatar_url')
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    art_title = _clean_art_title(art.get('title'))
    if art_title:
        embed.add_field(name="Artwork title", value=_truncate(art_title, 256), inline=False)

    art_description = _truncate(art.get('description'), 700) if not resource_name else ''
    if art_description:
        embed.add_field(name="Artwork description", value=art_description, inline=False)

    resource_description = _truncate(art.get('description'), 700) if resource_name else ''
    if resource_description:
        embed.add_field(name="Resource description", value=resource_description, inline=False)

    image_url = (
        art.get('preview_url')
        or art.get('backup_thumbnail_url')
        or art.get('cloudflare_thumbnail_url')
        or art.get('url')
    )
    if image_url:
        embed.set_image(url=image_url)

    made_this_value = (
        art.get('profile_url')
        or art.get('download_link')
        or art.get('url')
        or art.get('id')
        or 'Attached submission'
    )
    embed.add_field(name="I made this", value=str(made_this_value), inline=False)

    if profile_url:
        embed.add_field(name="Profile", value=profile_url, inline=False)

    embed.set_footer(text=f"React to admit · {APP_MARKER_PREFIX}{approval_request['id']}")
    return embed
