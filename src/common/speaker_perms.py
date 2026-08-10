"""Shared helpers for three-tier role permission enforcement.

The model manages four roles per channel — @everyone, Newbie, Speaker, and
Moderated — where @everyone is denied send in every mode (no open channels).
Each channel carries a posting mode that decides which of the managed roles
may send:
  - bot       → nobody posts (gate channel)
  - newbie    → Newbie + Speaker (introductions, grants forum, help/support)
  - community → Speaker only (all topical channels)
  - appeal    → Speaker + Moderated (moderation / appeal channel)
  - admin     → nobody except Administrator bypass (announcement channels:
                rules, updates, getting-started). The admin role holds the
                guild-level Administrator permission, so it is unaffected by
                channel overwrites and needs no explicit allow.

View (read) access is managed only where the model says so — see
`_VIEW_ROLE_ALLOWED`. The gate channel ('bot' mode) must be readable by Newbie
and Speaker so they can see the pinned onboarding / welcome message, and the
'newbie' mode channels (introductions, grants forum, help/support) must be
readable by the roles that can post there. For those roles `view_channel` is
enforced to True while @everyone and Moderated are left to manual setup. All
other modes do not manage view at all.

Moderated is denied everywhere except `appeal`.

IMPORTANT (Discord permission semantics): for a member holding multiple roles,
channel overwrite allows and denies are OR'd across all roles and then applied
as `(base & ~denies) | allows` — so an ALLOW from any role wins over a DENY from
another role for the same member. Role position does NOT make a deny beat an
allow. The Moderated tier therefore only blocks a member if they hold NO role
that grants send in that channel. Moderation enforces this by REMOVING the
Newbie/Speaker roles synchronously (swap -> Moderated), and the 5-minute
reconciliation loop + `on_member_update` strip any stale tier role within
minutes. Moderated is still positioned above Newbie/Speaker as defense in depth,
but that ordering is not what enforces the block.
"""
import logging
from typing import Mapping, Tuple

import discord

logger = logging.getLogger('DiscordBot')

# The permission attributes we manage on every channel.
SEND_PERMS = [
    'send_messages',
    'send_messages_in_threads',
    'create_public_threads',
    'create_private_threads',
]

# Pinning is a standalone permission (discord.py >=2.7.0). We grant it to the
# posting roles ONLY on forum channels, so members can pin/unpin messages inside
# forum posts but not in regular channels. See pin_allowed().
PIN_PERMS = [
    'pin_messages',
]

# Per-channel posting mode -> which of the managed roles may send.
# @everyone is denied in every mode.
_MODE_ROLE_ALLOWED = {
    'bot':       {'everyone': False, 'newbie': False, 'speaker': False, 'moderated': False},
    'newbie':    {'everyone': False, 'newbie': True,  'speaker': True,  'moderated': False},
    'community': {'everyone': False, 'newbie': False, 'speaker': True,  'moderated': False},
    'appeal':    {'everyone': False, 'newbie': False, 'speaker': True,  'moderated': True},
    'admin':     {'everyone': False, 'newbie': False, 'speaker': False, 'moderated': False},
}

# Per-channel posting mode -> view_channel value the bot enforces for each role.
# `None` (or an absent role) means the bot leaves that role's view overwrite to
# manual setup. Enforced for the roles that need to SEE the channel:
#   - 'bot' mode: the gate channel pins the onboarding / welcome message, so
#     Newbie and Speaker must be able to read it.
#   - 'newbie' mode: introductions / grants forum / help-support, where Newbie
#     and Speaker can post — posting is pointless if they can't see the channel.
# @everyone and Moderated are not managed.
_VIEW_ROLE_ALLOWED = {
    'bot': {'newbie': True, 'speaker': True},
    'newbie': {'newbie': True, 'speaker': True},
}

VALID_MODES = frozenset(_MODE_ROLE_ALLOWED.keys())
ROLE_KEYS = ('everyone', 'newbie', 'speaker', 'moderated')


def _expected_values(mode: str, role_key: str) -> dict:
    """Return the expected permission values for a role in a channel mode.

    Args:
        mode: 'bot', 'newbie', 'community', or 'appeal'.
        role_key: 'everyone', 'newbie', 'speaker', or 'moderated'.

    Returns:
        Dict mapping each SEND_PERMS attr to True/False, plus `view_channel`
        when the mode manages view for that role (see `_VIEW_ROLE_ALLOWED`).
    """
    allowed = _MODE_ROLE_ALLOWED.get(mode, _MODE_ROLE_ALLOWED['community'])
    can_send = allowed.get(role_key, False)
    expected = {p: can_send for p in SEND_PERMS}
    view = _VIEW_ROLE_ALLOWED.get(mode, {}).get(role_key)
    if view is not None:
        expected['view_channel'] = view
    return expected


def pin_allowed(mode: str, role_key: str, *, is_forum: bool) -> bool:
    """Whether a role may pin messages in a channel of the given type.

    Pinning is forum-only: no role may pin in regular channels (``is_forum=False``).
    In a forum channel, the roles that may post in that mode may also pin.
    """
    if not is_forum:
        return False
    return _MODE_ROLE_ALLOWED.get(mode, _MODE_ROLE_ALLOWED['community']).get(role_key, False)


def check_overwrite_matches(overwrite: discord.PermissionOverwrite, expected: dict) -> bool:
    """Check if existing cached overwrite already matches expected values.

    Avoids unnecessary Discord API calls when perms are already correct.
    """
    for attr, value in expected.items():
        current = getattr(overwrite, attr)
        if current != value:
            return False
    return True


async def apply_perms_to_channel(
    channel: discord.abc.GuildChannel,
    roles: Mapping[str, discord.Role],
    mode: str,
) -> Tuple[bool, int]:
    """Check and correct permissions for a single channel.

    Args:
        channel: The Discord channel to enforce.
        roles: Mapping of role_key -> Role for 'everyone', 'newbie', 'speaker',
            'moderated'.
        mode: 'bot', 'newbie', 'community', or 'appeal'.

    Returns:
        (changed, api_calls) — whether anything was fixed, and how many API calls made.
    """
    if mode not in VALID_MODES:
        mode = 'community'
    changed = False
    api_calls = 0

    for role_key in ROLE_KEYS:
        role = roles.get(role_key)
        if role is None:
            continue

        expected = _expected_values(mode, role_key)
        # Pinning is enabled only inside forum posts (not regular channels).
        # Use ChannelType.forum rather than isinstance(ForumChannel): discord.py
        # maps GUILD_MEDIA channels to ForumChannel too, and they must stay denied.
        expected['pin_messages'] = pin_allowed(
            mode, role_key,
            is_forum=channel.type is discord.ChannelType.forum,
        )
        ow = channel.overwrites_for(role)
        needs_update = not check_overwrite_matches(ow, expected)
        # Clean up legacy add_reactions overwrite (no longer managed)
        if ow.add_reactions is not None:
            ow.add_reactions = None
            needs_update = True
        if needs_update:
            for attr, value in expected.items():
                setattr(ow, attr, value)
            await channel.set_permissions(
                role, overwrite=ow,
                reason=f"Speaker perm enforcement — {mode} {role_key}",
            )
            api_calls += 1
            changed = True

    return (changed, api_calls)
