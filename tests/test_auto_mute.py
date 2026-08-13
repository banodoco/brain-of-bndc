"""Tests for the auto-mute guard (image-only posts) and the shared speaker-mute helper."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.features.auto_moderation.auto_mute_cog import (
    DEFAULT_DM_MESSAGE,
    image_attachment_count,
    should_auto_mute,
)


# ═══════════════════════════════════════════════════════════════════
# image_attachment_count — content-type AND extension detection
# ═══════════════════════════════════════════════════════════════════

def _att(*, ctype='image/png', filename='pic.png'):
    return SimpleNamespace(content_type=ctype, filename=filename)


def _msg(*, guild=True, bot=False, attachments=(), content='', channel_id=111,
         parent_id=None, manage_messages=False, administrator=False, moderate_members=False,
         author_roles=True):
    author_kwargs = dict(bot=bot, guild_permissions=SimpleNamespace(
        manage_messages=manage_messages, administrator=administrator, moderate_members=moderate_members))
    if author_roles:
        author_kwargs['roles'] = []
    author = SimpleNamespace(**author_kwargs)
    return SimpleNamespace(
        guild=SimpleNamespace(id=1) if guild else None,
        author=author,
        attachments=list(attachments),
        content=content,
        channel=SimpleNamespace(id=channel_id, parent_id=parent_id),
    )


def test_image_count_by_content_type():
    assert image_attachment_count(SimpleNamespace(attachments=[_att()] * 4)) == 4


def test_image_count_falls_back_to_extension_when_content_type_none():
    atts = [_att(ctype=None, filename='a.png'), _att(ctype=None, filename='b.JPG'),
            _att(ctype=None, filename='c.webp'), _att(ctype=None, filename='d.pdf')]
    assert image_attachment_count(SimpleNamespace(attachments=atts)) == 3


def test_image_count_ignores_non_images():
    atts = [_att(ctype='application/pdf', filename='a.pdf'), _att(ctype='video/mp4', filename='b.mp4')]
    assert image_attachment_count(SimpleNamespace(attachments=atts)) == 0


# ═══════════════════════════════════════════════════════════════════
# should_auto_mute — pure detector decision matrix
# ═══════════════════════════════════════════════════════════════════

def _img(n: int = 1, ctype: str = 'image/png'):
    return [_att(ctype=ctype, filename=f'pic{i}.png') for i in range(n)]


def test_should_auto_mute_four_images_no_text():
    assert should_auto_mute(_msg(attachments=_img(4))) is True


def test_should_auto_mute_four_images_whitespace_only():
    assert should_auto_mute(_msg(attachments=_img(4), content='   \n\t ')) is True


def test_should_auto_mute_three_images_is_not_enough():
    assert should_auto_mute(_msg(attachments=_img(3))) is False


def test_should_auto_mute_text_content_disqualifies():
    assert should_auto_mute(_msg(attachments=_img(4), content='made these with a new LoRA')) is False


def test_should_auto_mute_bot_never():
    assert should_auto_mute(_msg(bot=True, attachments=_img(4))) is False


def test_should_auto_mute_no_guild_never():
    assert should_auto_mute(_msg(guild=False, attachments=_img(4))) is False


def test_should_auto_mute_non_member_author_skipped():
    # A User (not Member) has no .roles — must not crash and must not mute.
    assert should_auto_mute(_msg(attachments=_img(4), author_roles=False)) is False


def test_should_auto_mute_non_image_attachments_not_counted():
    atts = _img(3) + [_att(ctype='application/pdf', filename='doc.pdf')]
    assert should_auto_mute(_msg(attachments=atts)) is False


def test_should_auto_mute_image_by_extension_when_content_type_none():
    atts = [_att(ctype=None, filename='a.png') for _ in range(4)]
    assert should_auto_mute(_msg(attachments=atts)) is True


def test_should_auto_mute_staff_excluded():
    assert should_auto_mute(_msg(attachments=_img(4), manage_messages=True)) is False
    assert should_auto_mute(_msg(attachments=_img(4), administrator=True)) is False
    assert should_auto_mute(_msg(attachments=_img(4), moderate_members=True)) is False


def test_should_auto_mute_exempt_channel():
    assert should_auto_mute(_msg(attachments=_img(4), channel_id=42), exempt_channel_ids=frozenset({42})) is False


def test_should_auto_mute_exempt_thread_parent():
    assert should_auto_mute(_msg(attachments=_img(4), channel_id=777, parent_id=42), exempt_channel_ids=frozenset({42})) is False


def test_should_auto_mute_min_images_config():
    assert should_auto_mute(_msg(attachments=_img(2)), min_images=2) is True
    assert should_auto_mute(_msg(attachments=_img(2)), min_images=3) is False


# ═══════════════════════════════════════════════════════════════════
# _parse_duration — minutes unit
# ═══════════════════════════════════════════════════════════════════

def test_parse_duration_minutes():
    from src.common.speaker_mute import _parse_duration
    assert _parse_duration('5m') == timedelta(minutes=5)
    assert _parse_duration('90m') == timedelta(minutes=90)
    assert _parse_duration(' 10m ') == timedelta(minutes=10)
    assert _parse_duration('1h') == timedelta(hours=1)
    assert _parse_duration('7d') == timedelta(days=7)
    assert _parse_duration('2w') == timedelta(weeks=2)
    assert _parse_duration('5x') is None
    assert _parse_duration('') is None


def test_parse_duration_reexported_from_admin_cog():
    from src.features.admin.admin_cog import _parse_duration
    assert _parse_duration('5m') == timedelta(minutes=5)


# ═══════════════════════════════════════════════════════════════════
# Shared mute helper — already-muted semantics
# ═══════════════════════════════════════════════════════════════════

class _FakeDB:
    """Call-tracking stand-in for DatabaseHandler (mirrors test_three_tier_member_model)."""

    def __init__(self, status='speaker', prior_status='speaker', prior_cmb=True, timed_mute_row=None):
        self.status = status
        self.prior_status = prior_status
        self.prior_cmb = prior_cmb
        self.timed_mute_row = timed_mute_row
        self.calls = []

    def get_member_status(self, member_id, guild_id=None):
        self.calls.append(('get_member_status', member_id))
        return self.status

    def get_member_prior_status(self, member_id, guild_id):
        return self.prior_status

    def get_guild_member(self, member_id, guild_id):
        return {'prior_can_message_bot': self.prior_cmb, 'prior_status': self.prior_status}

    def get_member_can_message_bot(self, member_id):
        return True

    def set_member_status(self, member_id, guild_id, status, prior_status=None, set_prior=False):
        self.calls.append(('set_member_status', member_id, status, prior_status, set_prior))
        self.status = status
        return True

    def set_member_can_message_bot(self, member_id, can_message_bot, username=None):
        self.calls.append(('set_member_can_message_bot', member_id, can_message_bot))
        return True

    def create_timed_mute(self, **kwargs):
        self.calls.append(('create_timed_mute', kwargs))
        return True

    def get_timed_mute(self, member_id, guild_id):
        self.calls.append(('get_timed_mute', member_id))
        return self.timed_mute_row

    def delete_timed_mute(self, member_id, guild_id):
        self.calls.append(('delete_timed_mute', member_id))
        return True


def _tier_roles():
    return {
        'newbie': SimpleNamespace(id=1, name='Newbie'),
        'speaker': SimpleNamespace(id=2, name='Speaker'),
        'moderated': SimpleNamespace(id=3, name='Moderated'),
    }


def _member(*, tier='speaker'):
    roles = {'newbie': _tier_roles()['newbie'], 'speaker': _tier_roles()['speaker'],
             'moderated': _tier_roles()['moderated']}
    member = SimpleNamespace(id=111, name='testuser', roles=[roles[tier]])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


@pytest.mark.asyncio
async def test_helper_mutes_speaker_to_moderated():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB()
    member = _member()
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='spam', actor_label='tester', duration='5m',
        mute_end_at=datetime.now(timezone.utc) + timedelta(minutes=5), allow_update=False,
    )
    assert result['success'] is True
    assert result['prior_status'] == 'speaker'
    assert member.remove_roles.call_args.args[0].id == 2  # speaker removed
    assert member.add_roles.call_args.args[0].id == 3      # moderated added
    assert ('set_member_status', 111, 'moderated', 'speaker', True) in db.calls
    assert ('set_member_can_message_bot', 111, False) in db.calls
    assert any(c[0] == 'create_timed_mute' for c in db.calls)
    created = [c[1] for c in db.calls if c[0] == 'create_timed_mute'][0]
    end = datetime.fromisoformat(created['mute_end_at'])
    assert timedelta(minutes=4, seconds=30) <= end - datetime.now(timezone.utc) <= timedelta(minutes=5, seconds=30)
    assert created['prior_status'] == 'speaker'
    assert created['muted_by_id'] is None


@pytest.mark.asyncio
async def test_helper_already_muted_without_update_does_nothing():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB(status='moderated')
    member = _member(tier='moderated')
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='spam', actor_label='tester', allow_update=False,
    )
    assert result['already_muted'] is True
    assert result['success'] is True
    assert not member.add_roles.called
    assert not member.remove_roles.called
    assert not any(c[0] in ('set_member_status', 'create_timed_mute') for c in db.calls)


@pytest.mark.asyncio
async def test_helper_rolls_back_db_status_when_role_swap_fails():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB()
    member = _member()
    member.remove_roles = AsyncMock(side_effect=Exception('role API down'))
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='spam', actor_label='tester', allow_update=False,
    )
    assert result['success'] is False
    # status was set to moderated, then rolled back to the prior tier
    set_calls = [c for c in db.calls if c[0] == 'set_member_status']
    assert set_calls[0][2] == 'moderated'
    assert set_calls[-1][2] == 'speaker'
    assert not any(c[0] == 'create_timed_mute' for c in db.calls)


@pytest.mark.asyncio
async def test_helper_allow_update_preserves_original_snapshot():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB(status='moderated', prior_status='newbie', prior_cmb=False)
    member = _member(tier='moderated')
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='still spam', actor_label='tester', duration='5m',
        mute_end_at=datetime.now(timezone.utc) + timedelta(minutes=5), allow_update=True,
    )
    assert result['success'] is True
    assert result['was_already_muted'] is True
    assert ('set_member_status', 111, 'moderated', 'newbie', False) in db.calls
    created = [c[1] for c in db.calls if c[0] == 'create_timed_mute'][0]
    assert created['prior_status'] == 'newbie'  # original snapshot preserved


# ═══════════════════════════════════════════════════════════════════
# AutoMuteCog.on_message — end to end
# ═══════════════════════════════════════════════════════════════════

def _make_cog():
    from src.features.auto_moderation.auto_mute_cog import AutoMuteCog
    cog = AutoMuteCog.__new__(AutoMuteCog)
    cog.bot = SimpleNamespace(get_cog=lambda name: None)
    cog.db_handler = _FakeDB()
    cog.server_config = None
    cog._in_flight = set()
    cog._is_enabled = lambda guild_id: True
    cog._resolve_tier_roles = lambda guild: _tier_roles()
    cog._get_exempt_channels = lambda guild_id: frozenset()
    return cog


def _guild_msg(member, *, channel_id=111, parent_id=None, attachments=None, content=''):
    msg = SimpleNamespace(
        id=222,
        guild=SimpleNamespace(id=456),
        author=member,
        channel=SimpleNamespace(id=channel_id, parent_id=parent_id),
        content=content,
        attachments=attachments or [],
        jump_url='https://discord.com/channels/456/111/222',
    )
    return msg


def _speaker_member():
    member = SimpleNamespace(
        id=111, name='testuser', bot=False,
        roles=[_tier_roles()['speaker']],
        guild_permissions=SimpleNamespace(manage_messages=False, administrator=False, moderate_members=False),
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    return member


def test_on_message_auto_mutes_four_image_post():
    cog = _make_cog()
    member = _speaker_member()
    msg = _guild_msg(member, attachments=_img(4))

    with patch('src.features.auto_moderation.auto_mute_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)) as modlog:
        asyncio.run(cog.on_message(msg))

    assert member.remove_roles.call_args.args[0].id == 2  # speaker removed
    assert member.add_roles.call_args.args[0].id == 3      # moderated added
    assert any(c[0] == 'create_timed_mute' for c in cog.db_handler.calls)
    member.send.assert_awaited_once()
    assert '4 images' in member.send.call_args.args[0]
    assert 'discord.com/channels' in member.send.call_args.args[0]  # jump URL in DM
    modlog.assert_awaited_once()
    # reason includes the message link for the moderation log
    assert 'discord.com/channels' in modlog.call_args.kwargs['reason']


def test_on_message_does_not_mute_with_text():
    cog = _make_cog()
    member = _speaker_member()
    msg = _guild_msg(member, attachments=_img(4), content='here is my WIP')

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.add_roles.called
    assert not member.send.called


def test_on_message_skips_already_moderated():
    cog = _make_cog()
    tier = _tier_roles()
    member = SimpleNamespace(
        id=111, name='testuser', bot=False, roles=[tier['moderated']],
        guild_permissions=SimpleNamespace(manage_messages=False, administrator=False, moderate_members=False),
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    msg = _guild_msg(member, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.add_roles.called
    assert not member.send.called


def test_on_message_skips_newbie():
    cog = _make_cog()
    tier = _tier_roles()
    member = SimpleNamespace(
        id=111, name='testuser', bot=False, roles=[tier['newbie']],
        guild_permissions=SimpleNamespace(manage_messages=False, administrator=False, moderate_members=False),
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    msg = _guild_msg(member, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_skips_when_disabled():
    cog = _make_cog()
    cog._is_enabled = lambda guild_id: False
    member = _speaker_member()
    msg = _guild_msg(member, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_skips_when_speaker_management_disabled():
    cog = _make_cog()
    cog._is_enabled = lambda guild_id: False  # _is_enabled already gates on speaker management
    member = _speaker_member()
    msg = _guild_msg(member, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_skips_exempt_channel():
    cog = _make_cog()
    cog._get_exempt_channels = lambda guild_id: frozenset({42})
    member = _speaker_member()
    msg = _guild_msg(member, channel_id=42, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_skips_exempt_thread_parent():
    cog = _make_cog()
    cog._get_exempt_channels = lambda guild_id: frozenset({42})
    member = _speaker_member()
    msg = _guild_msg(member, channel_id=777, parent_id=42, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_in_flight_guard_prevents_second_mute():
    cog = _make_cog()
    member = _speaker_member()
    cog._in_flight.add((456, member.id))
    msg = _guild_msg(member, attachments=_img(4))

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_dm_failure_does_not_break_mute():
    cog = _make_cog()
    member = _speaker_member()
    member.send = AsyncMock(side_effect=Exception('DMs closed'))
    msg = _guild_msg(member, attachments=_img(4))

    with patch('src.features.auto_moderation.auto_mute_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)):
        asyncio.run(cog.on_message(msg))

    assert member.remove_roles.call_args.args[0].id == 2
    assert any(c[0] == 'create_timed_mute' for c in cog.db_handler.calls)


def test_default_dm_message_is_formattable():
    text = DEFAULT_DM_MESSAGE.format(count=4, duration='5m', url='https://discord.com/channels/x')
    assert '4 images' in text
    assert '5m' in text
    assert 'discord.com/channels' in text
    assert '<#' not in text  # no broken mentions


# ═══════════════════════════════════════════════════════════════════
# Exact-time restore guard
# ═══════════════════════════════════════════════════════════════════

def _make_restore_cog(timed_mute_row, *, member_status='moderated', member_in_guild=True):
    cog = _make_cog()
    tier = _tier_roles()
    member = SimpleNamespace(id=111, name='testuser', roles=[tier['moderated']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = SimpleNamespace(id=456, get_member=lambda mid: member if member_in_guild else None)
    cog.bot = SimpleNamespace(get_guild=lambda gid: guild, get_cog=lambda name: None)
    cog.db_handler = _FakeDB(status=member_status, timed_mute_row=timed_mute_row)
    return cog, tier, member


def test_restore_restores_when_mute_still_active():
    mute_end_iso = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    cog, tier, member = _make_restore_cog({'mute_end_at': mute_end_iso})
    asyncio.run(cog._restore_if_still_muted(456, 111, mute_end_iso, 'speaker', True, tier))

    assert member.remove_roles.call_args.args[0].id == 3  # moderated removed
    assert member.add_roles.call_args.args[0].id == 2      # speaker restored
    assert ('set_member_status', 111, 'speaker', None, False) in cog.db_handler.calls
    assert ('delete_timed_mute', 111) in cog.db_handler.calls


def test_restore_skips_when_timed_mute_row_missing():
    mute_end_iso = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    cog, tier, member = _make_restore_cog(None)
    asyncio.run(cog._restore_if_still_muted(456, 111, mute_end_iso, 'speaker', True, tier))

    assert not member.remove_roles.called
    assert not member.add_roles.called


def test_restore_skips_when_mute_superseded_by_different_end():
    # A re-mute with a different (longer) end is active — do not restore.
    old_end = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    new_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    cog, tier, member = _make_restore_cog({'mute_end_at': new_end})
    asyncio.run(cog._restore_if_still_muted(456, 111, old_end, 'speaker', True, tier))

    assert not member.remove_roles.called
    assert not member.add_roles.called


def test_restore_skips_when_already_unmuted_in_db():
    mute_end_iso = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    cog, tier, member = _make_restore_cog({'mute_end_at': mute_end_iso}, member_status='speaker')
    asyncio.run(cog._restore_if_still_muted(456, 111, mute_end_iso, 'speaker', True, tier))

    assert not member.remove_roles.called
    assert not member.add_roles.called
