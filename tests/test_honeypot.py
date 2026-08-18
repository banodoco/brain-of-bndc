"""Tests for the rules-channel honeypot and the shared speaker-mute helper."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.features.auto_moderation.honeypot_cog import HONEYPOT_NOTICE_MESSAGE, is_honeypot_post

RULES_CHANNEL = 1138515622582562947


# ═══════════════════════════════════════════════════════════════════
# is_honeypot_post — pure detector decision matrix
# ═══════════════════════════════════════════════════════════════════

def _msg(*, guild=True, bot=False, channel_id=111, parent_id=None,
         manage_messages=False, administrator=False, moderate_members=False, author_roles=True):
    author_kwargs = dict(bot=bot, guild_permissions=SimpleNamespace(
        manage_messages=manage_messages, administrator=administrator, moderate_members=moderate_members))
    if author_roles:
        author_kwargs['roles'] = []
    author = SimpleNamespace(**author_kwargs)
    return SimpleNamespace(
        guild=SimpleNamespace(id=1) if guild else None,
        author=author,
        channel=SimpleNamespace(id=channel_id, parent_id=parent_id),
    )


def test_post_in_rules_channel_is_trapped():
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL), RULES_CHANNEL) is True


def test_post_with_text_still_trapped():
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL), RULES_CHANNEL) is True


def test_post_in_other_channel_not_trapped():
    assert is_honeypot_post(_msg(channel_id=999), RULES_CHANNEL) is False


def test_thread_inside_rules_channel_is_trapped():
    assert is_honeypot_post(_msg(channel_id=777, parent_id=RULES_CHANNEL), RULES_CHANNEL) is True


def test_bot_never_trapped():
    assert is_honeypot_post(_msg(bot=True, channel_id=RULES_CHANNEL), RULES_CHANNEL) is False


def test_no_guild_never_trapped():
    assert is_honeypot_post(_msg(guild=False, channel_id=RULES_CHANNEL), RULES_CHANNEL) is False


def test_non_member_author_skipped():
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL, author_roles=False), RULES_CHANNEL) is False


def test_staff_never_trapped():
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL, manage_messages=True), RULES_CHANNEL) is False
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL, administrator=True), RULES_CHANNEL) is False
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL, moderate_members=True), RULES_CHANNEL) is False


def test_unconfigured_honeypot_never_traps():
    assert is_honeypot_post(_msg(channel_id=RULES_CHANNEL), None) is False


# ═══════════════════════════════════════════════════════════════════
# _parse_duration — hours
# ═══════════════════════════════════════════════════════════════════

def test_parse_duration_hour():
    from src.common.speaker_mute import _parse_duration
    assert _parse_duration('1h') == timedelta(hours=1)
    assert _parse_duration('5m') == timedelta(minutes=5)
    assert _parse_duration('7d') == timedelta(days=7)
    assert _parse_duration('2w') == timedelta(weeks=2)


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
    roles_by_name = {'newbie': _tier_roles()['newbie'], 'speaker': _tier_roles()['speaker'],
                     'moderated': _tier_roles()['moderated']}
    member = SimpleNamespace(id=111, name='testuser', roles=[roles_by_name[tier]])

    async def add_role(role, **kwargs):
        if role not in member.roles:
            member.roles.append(role)

    async def remove_role(role, **kwargs):
        if role in member.roles:
            member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add_role)
    member.remove_roles = AsyncMock(side_effect=remove_role)
    return member


@pytest.mark.asyncio
async def test_helper_mutes_speaker_for_one_hour():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB()
    member = _member()
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='honeypot', actor_label='Honeypot guard', duration='1h',
        mute_end_at=datetime.now(timezone.utc) + timedelta(hours=1), allow_update=False,
    )
    assert result['success'] is True
    assert member.remove_roles.call_args.args[0].id == 2
    assert member.add_roles.call_args.args[0].id == 3
    created = [c[1] for c in db.calls if c[0] == 'create_timed_mute'][0]
    end = datetime.fromisoformat(created['mute_end_at'])
    assert timedelta(minutes=59) <= end - datetime.now(timezone.utc) <= timedelta(hours=1, minutes=1)
    assert created['prior_status'] == 'speaker'


@pytest.mark.asyncio
async def test_helper_already_muted_without_update_does_nothing():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB(status='moderated')
    member = _member(tier='moderated')
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='honeypot', actor_label='Honeypot guard', allow_update=False,
    )
    assert result['already_muted'] is True
    assert not member.add_roles.called
    assert not member.remove_roles.called


@pytest.mark.asyncio
async def test_helper_rolls_back_db_status_when_role_swap_fails():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB()
    member = _member()
    member.remove_roles = AsyncMock(side_effect=Exception('role API down'))
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='honeypot', actor_label='Honeypot guard', allow_update=False,
    )
    assert result['success'] is False
    set_calls = [c for c in db.calls if c[0] == 'set_member_status']
    assert set_calls[0][2] == 'moderated'
    assert set_calls[-1][2] == 'speaker'
    assert not any(c[0] == 'create_timed_mute' for c in db.calls)


@pytest.mark.asyncio
async def test_helper_rolls_back_when_timed_mute_record_fails():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB()
    db.create_timed_mute = lambda **kw: db.calls.append(('create_timed_mute', kw)) or False
    member = _member()
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='honeypot', actor_label='Honeypot guard', duration='1h',
        mute_end_at=datetime.now(timezone.utc) + timedelta(hours=1),
        allow_update=False, rollback_on_timer_failure=True,
    )
    assert result['success'] is False
    # full undo: moderated removed, speaker re-added, status rolled back
    assert member.add_roles.call_args.args[0].id == 2  # speaker restored
    assert member.remove_roles.call_args.args[0].id == 3  # moderated removed
    set_calls = [c for c in db.calls if c[0] == 'set_member_status']
    assert set_calls[0][2] == 'moderated'
    assert set_calls[-1][2] == 'speaker'


@pytest.mark.asyncio
async def test_helper_without_timer_rollback_flag_keeps_legacy_behavior():
    from src.common.speaker_mute import mute_speaker_member
    db = _FakeDB()
    db.create_timed_mute = lambda **kw: db.calls.append(('create_timed_mute', kw)) or False
    member = _member()
    result = await mute_speaker_member(
        db, guild=SimpleNamespace(id=456), member=member, tier_roles=_tier_roles(),
        reason='honeypot', actor_label='Honeypot guard', duration='1h',
        mute_end_at=datetime.now(timezone.utc) + timedelta(hours=1),
        allow_update=False, rollback_on_timer_failure=False,
    )
    assert result['success'] is True
    assert result['timed_mute_scheduled'] is False
    assert member.add_roles.call_args.args[0].id == 3  # moderated stays


# ═══════════════════════════════════════════════════════════════════
# HoneypotCog.on_message — end to end
# ═══════════════════════════════════════════════════════════════════

def _make_cog(honeypot_channel_id=RULES_CHANNEL, duration='1h'):
    from src.features.auto_moderation.honeypot_cog import HoneypotCog
    cog = HoneypotCog.__new__(HoneypotCog)
    cog.bot = SimpleNamespace(get_cog=lambda name: None)
    cog.db_handler = _FakeDB()
    cog.server_config = None
    cog._in_flight = set()
    cog._sweep_locks = {}
    cog._is_enabled = lambda guild_id: True
    cog._resolve_tier_roles = lambda guild: _tier_roles()
    cog._get_channel_id = lambda guild_id: honeypot_channel_id
    cog._get_duration = lambda guild_id: duration
    return cog


def _guild_msg(member, *, channel_id=RULES_CHANNEL, parent_id=None):
    msg = SimpleNamespace(
        id=222,
        guild=SimpleNamespace(id=456),
        author=member,
        channel=SimpleNamespace(id=channel_id, parent_id=parent_id, send=AsyncMock()),
        jump_url='https://discord.com/channels/456/1138515622582562947/222',
    )
    msg.delete = AsyncMock()
    return msg


def _speaker_member():
    member = SimpleNamespace(
        id=111, name='testuser', mention='<@111>', bot=False,
        roles=[_tier_roles()['speaker']],
        guild_permissions=SimpleNamespace(manage_messages=False, administrator=False, moderate_members=False),
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    return member


def test_on_message_traps_speaker_in_rules_channel():
    cog = _make_cog()
    member = _speaker_member()
    msg = _guild_msg(member)

    with patch('src.features.auto_moderation.honeypot_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)) as modlog:
        asyncio.run(cog.on_message(msg))

    assert member.remove_roles.call_args.args[0].id == 2  # speaker removed
    assert member.add_roles.call_args.args[0].id == 3      # moderated added
    assert any(c[0] == 'create_timed_mute' for c in cog.db_handler.calls)
    created = [c[1] for c in cog.db_handler.calls if c[0] == 'create_timed_mute'][0]
    end = datetime.fromisoformat(created['mute_end_at'])
    assert timedelta(minutes=59) <= end - datetime.now(timezone.utc) <= timedelta(hours=1, minutes=1)
    # spam deleted, tagged notice posted IN the rules channel, no DM
    msg.delete.assert_awaited_once()
    msg.channel.send.assert_awaited_once()
    assert member.mention in msg.channel.send.call_args.args[0]
    member.send.assert_not_called()
    modlog.assert_awaited_once()
    assert 'honeypot' in modlog.call_args.kwargs['reason'].lower()


def test_on_message_traps_newbie_in_rules_channel():
    cog = _make_cog()
    tier = _tier_roles()
    member = SimpleNamespace(
        id=111, name='testuser', bot=False, roles=[tier['newbie']],
        guild_permissions=SimpleNamespace(manage_messages=False, administrator=False, moderate_members=False),
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    msg = _guild_msg(member)

    with patch('src.features.auto_moderation.honeypot_cog.post_mute_to_moderation', new=AsyncMock()):
        asyncio.run(cog.on_message(msg))

    assert member.remove_roles.call_args.args[0].id == 1  # newbie removed
    assert member.add_roles.call_args.args[0].id == 3


def test_on_message_ignores_other_channels():
    cog = _make_cog()
    member = _speaker_member()
    msg = _guild_msg(member, channel_id=999)

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_traps_thread_in_rules_channel():
    cog = _make_cog()
    member = _speaker_member()
    msg = _guild_msg(member, channel_id=777, parent_id=RULES_CHANNEL)

    with patch('src.features.auto_moderation.honeypot_cog.post_mute_to_moderation', new=AsyncMock()):
        asyncio.run(cog.on_message(msg))

    assert member.remove_roles.call_args.args[0].id == 2


def test_on_message_skips_staff():
    cog = _make_cog()
    member = _speaker_member()
    member.guild_permissions = SimpleNamespace(manage_messages=True, administrator=False, moderate_members=False)
    msg = _guild_msg(member)

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_skips_when_disabled():
    cog = _make_cog()
    cog._is_enabled = lambda guild_id: False
    member = _speaker_member()
    msg = _guild_msg(member)

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
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
    msg = _guild_msg(member)

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_skips_member_without_tier_role():
    cog = _make_cog()
    tier = _tier_roles()
    member = SimpleNamespace(
        id=111, name='testuser', bot=False, roles=[tier['everyone']] if 'everyone' in tier else [],
        guild_permissions=SimpleNamespace(manage_messages=False, administrator=False, moderate_members=False),
    )
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    msg = _guild_msg(member)

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called
    assert not member.send.called


def test_on_message_in_flight_guard_deletes_orphan():
    cog = _make_cog()
    member = _speaker_member()
    cog._in_flight.add((456, member.id))
    msg = _guild_msg(member)

    asyncio.run(cog.on_message(msg))

    assert not member.remove_roles.called  # no double mute
    assert not member.send.called
    msg.delete.assert_awaited_once()  # orphan spam still removed


def test_on_message_notice_failure_does_not_break_trap():
    cog = _make_cog()
    member = _speaker_member()
    msg = _guild_msg(member)
    msg.channel.send = AsyncMock(side_effect=Exception('channel gone'))

    with patch('src.features.auto_moderation.honeypot_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)):
        asyncio.run(cog.on_message(msg))

    assert member.remove_roles.call_args.args[0].id == 2
    assert msg.delete.await_count == 1  # deletion still happened
    assert any(c[0] == 'create_timed_mute' for c in cog.db_handler.calls)


def test_on_message_passes_guild_moderation_channel():
    cog = _make_cog()
    cog.server_config = SimpleNamespace(
        get_server_field=lambda guild_id, field, cast=None: 424242 if field == 'moderation_channel_id' else None,
    )
    member = _speaker_member()
    msg = _guild_msg(member)

    with patch('src.features.auto_moderation.honeypot_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)) as modlog:
        asyncio.run(cog.on_message(msg))

    modlog.assert_awaited_once()
    assert modlog.call_args.kwargs['channel_id'] == 424242


def test_on_message_moderation_channel_none_without_config():
    cog = _make_cog()  # server_config is None — falls back to env/default at post time
    member = _speaker_member()
    msg = _guild_msg(member)

    with patch('src.features.auto_moderation.honeypot_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)) as modlog:
        asyncio.run(cog.on_message(msg))

    modlog.assert_awaited_once()
    assert modlog.call_args.kwargs['channel_id'] is None


def test_post_mute_to_moderation_explicit_channel_beats_env():
    import src.common.speaker_mute as sm
    os.environ['MODERATION_CHANNEL_ID'] = '555'
    try:
        ch = SimpleNamespace(id=777, send=AsyncMock())
        bot = SimpleNamespace(get_channel=lambda cid: ch)
        ok = asyncio.run(sm.post_mute_to_moderation(
            bot, target_user_id=111, target_username='u', actor_user_id=None,
            actor_label='Honeypot guard', duration='1h', mute_end_at_iso=None,
            reason='test', channel_id=777,
        ))
        assert ok is True
        assert ch.send.await_count == 1
    finally:
        del os.environ['MODERATION_CHANNEL_ID']


def test_post_mute_to_moderation_env_fallback_when_no_channel():
    import src.common.speaker_mute as sm
    os.environ['MODERATION_CHANNEL_ID'] = '555'
    try:
        ch = SimpleNamespace(id=555, send=AsyncMock())
        bot = SimpleNamespace(get_channel=lambda cid: ch)
        ok = asyncio.run(sm.post_mute_to_moderation(
            bot, target_user_id=111, target_username='u', actor_user_id=None,
            actor_label='Honeypot guard', duration='1h', mute_end_at_iso=None,
            reason='test',
        ))
        assert ok is True
        assert ch.send.await_count == 1
    finally:
        del os.environ['MODERATION_CHANNEL_ID']


def test_notice_message_is_formattable():
    text = HONEYPOT_NOTICE_MESSAGE.format(mention='<@111>')
    assert '<@111>' in text
    assert '1 hour' in text
    assert 'deleted' in text
    assert '<#' not in text  # no broken mentions


def test_delete_notice_after_removes_own_message():
    cog = _make_cog()
    notice = SimpleNamespace(id=999)
    notice.delete = AsyncMock()
    with patch('src.features.auto_moderation.honeypot_cog.asyncio.sleep', new=AsyncMock()):
        asyncio.run(cog._delete_notice_after(notice))
    notice.delete.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════
# Recent-message sweep
# ═══════════════════════════════════════════════════════════════════

def _msg_in_history(mid, author_id):
    m = SimpleNamespace(id=mid, author=SimpleNamespace(id=author_id))
    m.delete = AsyncMock()
    return m


def test_sweep_one_channel_deletes_author_messages_only():
    from src.features.auto_moderation.honeypot_cog import RECENT_SWEEP_MINUTES
    cog = _make_cog()
    target = _msg_in_history(1, 111)
    other = _msg_in_history(2, 999)
    calls = {}

    def fake_history(**kwargs):
        calls['kwargs'] = kwargs
        async def gen():
            yield target
            yield other
        return gen()

    ch = SimpleNamespace(id=10, name='ch1', history=fake_history)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RECENT_SWEEP_MINUTES)
    deleted = asyncio.run(cog._sweep_one_channel(ch, 111, cutoff))

    assert deleted == 1
    target.delete.assert_awaited_once()
    other.delete.assert_not_called()
    assert calls['kwargs']['after'] == cutoff
    assert 'limit' not in calls['kwargs'] or calls['kwargs']['limit'] is None  # full window, no oldest-100 cap


def test_sweep_recent_messages_across_channels():
    cog = _make_cog()
    target = _msg_in_history(1, 111)
    ch1 = SimpleNamespace(id=10, name='ch1', history=lambda **kw: _agen(target))
    ch2 = SimpleNamespace(id=11, name='ch2', history=lambda **kw: _agen())
    guild = SimpleNamespace(id=456, text_channels=[ch1, ch2], threads=[])

    asyncio.run(cog._sweep_recent_messages(guild, SimpleNamespace(id=111)))

    target.delete.assert_awaited_once()


def test_sweep_skips_when_already_running_for_guild():
    cog = _make_cog()
    target = _msg_in_history(1, 111)
    ch1 = SimpleNamespace(id=10, name='ch1', history=lambda **kw: _agen(target))
    guild = SimpleNamespace(id=456, text_channels=[ch1], threads=[])
    cog._sweep_locks[456] = asyncio.Lock()

    async def hold_lock():
        await cog._sweep_locks[456].acquire()
    asyncio.run(hold_lock())  # a sweep is already in progress

    asyncio.run(cog._sweep_recent_messages(guild, SimpleNamespace(id=111)))

    target.delete.assert_not_called()  # second sweep skipped, no stacked scans


def _agen(*items):
    async def gen():
        for item in items:
            yield item
    return gen()


# ═══════════════════════════════════════════════════════════════════
# Exact-time restore guard
# ═══════════════════════════════════════════════════════════════════

def _make_restore_cog(timed_mute_row, *, member_status='moderated'):
    cog = _make_cog()
    tier = _tier_roles()
    member = SimpleNamespace(id=111, name='testuser', roles=[tier['moderated']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = SimpleNamespace(id=456, get_member=lambda mid: member)
    cog.bot = SimpleNamespace(get_guild=lambda gid: guild, get_cog=lambda name: None)
    cog.db_handler = _FakeDB(status=member_status, timed_mute_row=timed_mute_row)
    return cog, tier, member


def test_restore_restores_when_timeout_still_active():
    mute_end_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cog, tier, member = _make_restore_cog({'mute_end_at': mute_end_iso})
    asyncio.run(cog._restore_if_still_muted(456, 111, mute_end_iso, 'speaker', True, tier))

    assert member.remove_roles.call_args.args[0].id == 3
    assert member.add_roles.call_args.args[0].id == 2
    assert ('set_member_status', 111, 'speaker', None, False) in cog.db_handler.calls
    assert ('delete_timed_mute', 111) in cog.db_handler.calls


def test_restore_skips_when_timed_mute_row_missing():
    mute_end_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cog, tier, member = _make_restore_cog(None)
    asyncio.run(cog._restore_if_still_muted(456, 111, mute_end_iso, 'speaker', True, tier))

    assert not member.remove_roles.called
    assert not member.add_roles.called


def test_restore_skips_when_timeout_superseded():
    old_end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    new_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    cog, tier, member = _make_restore_cog({'mute_end_at': new_end})
    asyncio.run(cog._restore_if_still_muted(456, 111, old_end, 'speaker', True, tier))

    assert not member.remove_roles.called
    assert not member.add_roles.called


def test_restore_skips_when_already_unmuted_in_db():
    mute_end_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cog, tier, member = _make_restore_cog({'mute_end_at': mute_end_iso}, member_status='speaker')
    asyncio.run(cog._restore_if_still_muted(456, 111, mute_end_iso, 'speaker', True, tier))

    assert not member.remove_roles.called
    assert not member.add_roles.called
