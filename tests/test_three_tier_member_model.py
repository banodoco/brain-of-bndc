"""Unit tests for the three-tier member model (Newbie / Speaker / Moderated).

Pure-logic + mocked-Discord tests following the style of test_gating_intro_post.py.
No live Discord or Supabase required.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from src.common.db_handler import (
    DatabaseHandler,
    MEMBER_STATUS_MODERATED,
    MEMBER_STATUS_NEWBIE,
    MEMBER_STATUS_SPEAKER,
    _LEGACY_MODE_MAP,
)
from src.common.speaker_perms import (
    PIN_PERMS,
    SEND_PERMS,
    ROLE_KEYS,
    VALID_MODES,
    _expected_values,
    pin_allowed,
)


# ═══════════════════════════════════════════════════════════════════
# speaker_perms mode table
# ═══════════════════════════════════════════════════════════════════

def test_everyone_always_denied_in_every_mode():
    for mode in VALID_MODES:
        values = _expected_values(mode, 'everyone')
        assert all(v is False for v in values.values())


def test_bot_mode_nobody_posts_but_newbie_and_speaker_can_view():
    # Gate channel ('bot' mode): nobody may send, but Newbie + Speaker must be
    # able to read the pinned onboarding / welcome message.
    for role_key in ROLE_KEYS:
        values = _expected_values('bot', role_key)
        for send_perm in SEND_PERMS:
            assert values[send_perm] is False
    assert _expected_values('bot', 'newbie').get('view_channel') is True
    assert _expected_values('bot', 'speaker').get('view_channel') is True
    # @everyone and Moderated are left to manual view setup on the gate channel.
    assert 'view_channel' not in _expected_values('bot', 'everyone')
    assert 'view_channel' not in _expected_values('bot', 'moderated')


def test_newbie_mode_newbie_and_speaker_can_view():
    # Newbie-mode channels (introductions, grants forum, help/support) are
    # postable by Newbie + Speaker — they must be able to see them too.
    assert _expected_values('newbie', 'newbie').get('view_channel') is True
    assert _expected_values('newbie', 'speaker').get('view_channel') is True
    assert 'view_channel' not in _expected_values('newbie', 'everyone')
    assert 'view_channel' not in _expected_values('newbie', 'moderated')


def test_non_bot_modes_do_not_manage_view():
    for mode in ('community', 'appeal'):
        for role_key in ROLE_KEYS:
            assert 'view_channel' not in _expected_values(mode, role_key)


def test_newbie_mode_newbie_and_speaker_can_post():
    assert all(_expected_values('newbie', 'newbie').values())
    assert all(_expected_values('newbie', 'speaker').values())
    assert all(v is False for v in _expected_values('newbie', 'moderated').values())


def test_community_mode_speaker_only():
    assert all(v is False for v in _expected_values('community', 'newbie').values())
    assert all(_expected_values('community', 'speaker').values())
    assert all(v is False for v in _expected_values('community', 'moderated').values())


def test_appeal_mode_speaker_and_moderated():
    assert all(v is False for v in _expected_values('appeal', 'newbie').values())
    assert all(_expected_values('appeal', 'speaker').values())
    assert all(_expected_values('appeal', 'moderated').values())


def test_unknown_mode_falls_back_to_community():
    assert all(_expected_values('bogus', 'speaker').values())
    assert all(v is False for v in _expected_values('bogus', 'newbie').values())


def test_expected_values_cover_all_send_perms():
    for mode in VALID_MODES:
        for role_key in ROLE_KEYS:
            values = _expected_values(mode, role_key)
            assert set(SEND_PERMS) <= set(values.keys())
            # Only the managed view_channel may appear beyond SEND_PERMS.
            assert set(values.keys()) - set(SEND_PERMS) <= {'view_channel'}


# ═══════════════════════════════════════════════════════════════════
# forum-only message pinning
# ═══════════════════════════════════════════════════════════════════

def test_dependency_contract_pin_messages_flag_present():
    """Fails fast if discord.py lacks the pin_messages flag (needs >=2.7.0)."""
    assert PIN_PERMS == ['pin_messages']
    assert 'pin_messages' in discord.Permissions.VALID_FLAGS
    assert 'pin_messages' in discord.PermissionOverwrite.VALID_NAMES
    allow, _ = discord.PermissionOverwrite(pin_messages=True).pair()
    assert allow.pin_messages is True
    _, deny = discord.PermissionOverwrite(pin_messages=False).pair()
    assert deny.pin_messages is True  # deny bit serializes correctly


def test_pin_allowed_forum_only():
    # Non-forum channels: nobody may pin, regardless of mode or role.
    for mode in VALID_MODES:
        for role_key in ROLE_KEYS:
            assert pin_allowed(mode, role_key, is_forum=False) is False
    # Forum channels: the roles that may post in that mode may also pin.
    assert pin_allowed('community', 'speaker', is_forum=True) is True
    assert pin_allowed('community', 'newbie', is_forum=True) is False
    assert pin_allowed('community', 'moderated', is_forum=True) is False
    assert pin_allowed('newbie', 'newbie', is_forum=True) is True
    assert pin_allowed('newbie', 'speaker', is_forum=True) is True
    assert pin_allowed('newbie', 'moderated', is_forum=True) is False
    assert pin_allowed('appeal', 'speaker', is_forum=True) is True
    assert pin_allowed('appeal', 'moderated', is_forum=True) is True
    assert pin_allowed('appeal', 'newbie', is_forum=True) is False
    assert pin_allowed('bot', 'speaker', is_forum=True) is False  # bot mode: nobody posts
    # @everyone never pins, even in a forum.
    for mode in VALID_MODES:
        assert pin_allowed(mode, 'everyone', is_forum=True) is False


def _make_perm_channel(channel_type):
    channel = MagicMock()
    channel.type = channel_type
    # Fresh overwrite per role — apply_perms_to_channel mutates the returned
    # object in place, so a shared return_value would leak the last role's state.
    channel.overwrites_for.side_effect = lambda role: discord.PermissionOverwrite()
    channel.set_permissions = AsyncMock()
    return channel


def _overwrite_for_role(channel, role_id):
    """Pull the overwrite passed to an *awaited* set_permissions call for a role id."""
    for call in channel.set_permissions.await_args_list:
        if call.args[0].id == role_id:
            return call.kwargs['overwrite']
    raise AssertionError(f"set_permissions never awaited for role id {role_id}")


def test_apply_perms_grants_pin_only_on_forum_channels():
    from src.common.speaker_perms import apply_perms_to_channel
    roles = _make_tier_roles()

    # Forum channel (community mode): only Speaker may pin.
    forum = _make_perm_channel(discord.ChannelType.forum)
    changed, api_calls = asyncio.run(apply_perms_to_channel(forum, roles, 'community'))
    assert changed is True and api_calls == 4  # all four roles rewritten
    assert _overwrite_for_role(forum, 2).pin_messages is True   # speaker
    assert _overwrite_for_role(forum, 1).pin_messages is False  # newbie
    assert _overwrite_for_role(forum, 3).pin_messages is False  # moderated
    assert _overwrite_for_role(forum, 0).pin_messages is False  # @everyone

    # Text channel: every managed role is denied pin.
    text = _make_perm_channel(discord.ChannelType.text)
    asyncio.run(apply_perms_to_channel(text, roles, 'community'))
    for role_id in (0, 1, 2, 3):
        assert _overwrite_for_role(text, role_id).pin_messages is False

    # Media channel (also ForumChannel under the hood): denied too.
    media = _make_perm_channel(discord.ChannelType.media)
    asyncio.run(apply_perms_to_channel(media, roles, 'community'))
    assert _overwrite_for_role(media, 2).pin_messages is False


_ROLE_ID_TO_KEY = {0: 'everyone', 1: 'newbie', 2: 'speaker', 3: 'moderated'}


def _seeded_overwrite(role_id, mode):
    """An overwrite whose SEND_PERMS already match the mode table, but whose
    pin_messages is None — simulating the rollout case where only the new pin
    grant has drifted and send permissions are already correct."""
    ow = discord.PermissionOverwrite()
    for perm, value in _expected_values(mode, _ROLE_ID_TO_KEY[role_id]).items():
        setattr(ow, perm, value)
    return ow


def test_apply_perms_fixes_pin_only_drift_and_is_idempotent():
    """The rollout-critical case: send perms already correct, pin_messages=None."""
    from src.common.speaker_perms import apply_perms_to_channel
    roles = _make_tier_roles()
    forum = MagicMock()
    forum.type = discord.ChannelType.forum
    stored = {}

    def overwrites_for(role):
        return stored.get(role.id) or _seeded_overwrite(role.id, 'community')

    async def set_perms(role, overwrite, reason):
        stored[role.id] = overwrite

    forum.overwrites_for.side_effect = overwrites_for
    forum.set_permissions = AsyncMock(side_effect=set_perms)

    # First pass: pin_messages drifted (None), so all four roles get rewritten.
    changed, api_calls = asyncio.run(apply_perms_to_channel(forum, roles, 'community'))
    assert changed is True and api_calls == 4
    assert stored[2].pin_messages is True   # speaker grant applied
    assert stored[1].pin_messages is False  # newbie denied
    assert stored[0].pin_messages is False  # @everyone denied

    # Second pass: state now matches expected — no further API calls.
    changed2, api_calls2 = asyncio.run(apply_perms_to_channel(forum, roles, 'community'))
    assert changed2 is False and api_calls2 == 0


def test_apply_perms_grants_view_to_newbie_and_speaker_on_bot_mode_gate():
    from src.common.speaker_perms import apply_perms_to_channel
    roles = _make_tier_roles()
    gate = _make_perm_channel(discord.ChannelType.text)

    changed, api_calls = asyncio.run(apply_perms_to_channel(gate, roles, 'bot'))
    assert changed is True and api_calls == 4  # all four roles rewritten (send denied)

    assert _overwrite_for_role(gate, 1).view_channel is True  # newbie
    assert _overwrite_for_role(gate, 2).view_channel is True  # speaker
    # @everyone and Moderated view are not managed on the gate channel.
    assert _overwrite_for_role(gate, 0).view_channel is None
    assert _overwrite_for_role(gate, 3).view_channel is None
    # Nobody may post in bot mode.
    for role_id in (0, 1, 2, 3):
        assert _overwrite_for_role(gate, role_id).send_messages is False


def test_apply_perms_community_does_not_manage_view():
    from src.common.speaker_perms import apply_perms_to_channel
    roles = _make_tier_roles()
    text = _make_perm_channel(discord.ChannelType.text)
    asyncio.run(apply_perms_to_channel(text, roles, 'community'))
    for role_id in (0, 1, 2, 3):
        assert _overwrite_for_role(text, role_id).view_channel is None


# ═══════════════════════════════════════════════════════════════════
# db_handler member status
# ═══════════════════════════════════════════════════════════════════

def _make_db(rows=None):
    """DatabaseHandler with a mocked supabase chain that returns `rows` from execute()."""
    db = object.__new__(DatabaseHandler)
    client = MagicMock()
    execute_result = SimpleNamespace(data=rows or [])
    select_mock = client.table.return_value.select.return_value
    select_mock.execute.return_value = execute_result
    select_mock.eq.return_value.execute.return_value = execute_result
    select_mock.eq.return_value.limit.return_value.execute.return_value = execute_result
    select_mock.eq.return_value.eq.return_value.limit.return_value.execute.return_value = execute_result
    # paginated paths: .range(offset, end).execute()
    select_mock.range.return_value.execute.return_value = execute_result
    select_mock.eq.return_value.range.return_value.execute.return_value = execute_result
    select_mock.or_.return_value.range.return_value.execute.return_value = execute_result
    client.table.return_value.upsert.return_value.execute.return_value = execute_result
    client.table.return_value.or_.return_value.execute.return_value = execute_result
    db.storage_handler = SimpleNamespace(supabase_client=client)
    db.server_config = SimpleNamespace(
        is_write_allowed=lambda guild_id: True,
        is_guild_enabled=lambda guild_id: True,
    )
    return db


def test_legacy_mode_map_matches_new_modes():
    assert _LEGACY_MODE_MAP == {'normal': 'community', 'readonly': 'bot', 'exempt': 'appeal'}


def test_get_member_status_legacy_moderated_from_speaker_muted():
    db = _make_db([{'member_status': None, 'speaker_muted': True}])
    assert db.get_member_status(123, guild_id=456) == MEMBER_STATUS_MODERATED


def test_get_member_status_legacy_speaker_when_not_muted():
    db = _make_db([{'member_status': None, 'speaker_muted': False}])
    assert db.get_member_status(123, guild_id=456) == MEMBER_STATUS_SPEAKER


def test_get_member_status_explicit_status_wins():
    db = _make_db([{'member_status': 'newbie', 'speaker_muted': True}])
    assert db.get_member_status(123, guild_id=456) == MEMBER_STATUS_NEWBIE


def test_get_member_status_unknown_member_defaults_speaker():
    db = _make_db([])
    assert db.get_member_status(123, guild_id=456) == MEMBER_STATUS_SPEAKER


def test_set_member_status_moderated_writes_derived_speaker_muted():
    db = _make_db([])
    assert db.set_member_status(123, 456, MEMBER_STATUS_MODERATED) is True
    payload = db.storage_handler.supabase_client.table('guild_members').upsert.call_args[0][0]
    assert payload['member_status'] == 'moderated'
    assert payload['speaker_muted'] is True


def test_set_member_status_speaker_writes_speaker_muted_false():
    db = _make_db([])
    assert db.set_member_status(123, 456, MEMBER_STATUS_SPEAKER) is True
    payload = db.storage_handler.supabase_client.table('guild_members').upsert.call_args[0][0]
    assert payload['member_status'] == 'speaker'
    assert payload['speaker_muted'] is False


def test_set_member_status_rejects_invalid_status():
    db = _make_db([])
    assert db.set_member_status(123, 456, 'bogus') is False
    assert not db.storage_handler.supabase_client.table('guild_members').upsert.called


def test_get_all_channel_speaker_modes_normalizes_legacy_values():
    db = _make_db([
        {'channel_id': 1, 'speaker_mode': 'normal'},
        {'channel_id': 2, 'speaker_mode': 'readonly'},
        {'channel_id': 3, 'speaker_mode': 'exempt'},
        {'channel_id': 4, 'speaker_mode': 'newbie'},
        {'channel_id': 5, 'speaker_mode': None},
    ])
    modes = db.get_all_channel_speaker_modes(guild_id=456)
    assert modes[1] == 'community'
    assert modes[2] == 'bot'
    assert modes[3] == 'appeal'
    assert modes[4] == 'newbie'
    assert modes[5] == 'community'


# ═══════════════════════════════════════════════════════════════════
# admin_cog mute / unmute / on_member_update
# ═══════════════════════════════════════════════════════════════════

class _FakeDB:
    """Call-tracking stand-in for DatabaseHandler used by the cog under test."""

    def __init__(self):
        self.status = MEMBER_STATUS_SPEAKER
        self.prior_status = MEMBER_STATUS_SPEAKER
        self.prior_cmb = True
        self.calls = []
        self.expired_mutes = []

    def get_member_status(self, member_id, guild_id=None):
        self.calls.append(('get_member_status', member_id))
        return self.status

    def get_member_prior_status(self, member_id, guild_id):
        self.calls.append(('get_member_prior_status', member_id))
        return self.prior_status

    def get_guild_member(self, member_id, guild_id):
        return {'prior_can_message_bot': self.prior_cmb}

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
        return False

    def delete_timed_mute(self, member_id, guild_id):
        self.calls.append(('delete_timed_mute', member_id))
        return True

    def get_expired_mutes(self):
        return self.expired_mutes

    def get_members_by_status(self, guild_id, status):
        return []


def _make_tier_roles():
    return {
        'everyone': SimpleNamespace(id=0, name='@everyone', position=0),
        'newbie': SimpleNamespace(id=1, name='Newbie', position=1),
        'speaker': SimpleNamespace(id=2, name='Speaker', position=2),
        'moderated': SimpleNamespace(id=3, name='Moderated', position=3),
    }


def _make_cog():
    from src.features.admin.admin_cog import AdminCog
    cog = AdminCog.__new__(AdminCog)
    cog.bot = SimpleNamespace(is_owner=AsyncMock(return_value=True), guilds=[])
    cog.db_handler = _FakeDB()
    cog.server_config = None
    cog._dm_access_cache = {}
    cog._is_speaker_management_enabled = lambda guild_id: True
    return cog


def _make_interaction(cog):
    return SimpleNamespace(
        user=SimpleNamespace(id=999, name='admin'),
        guild_id=456,
        guild=SimpleNamespace(id=456),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def test_mute_user_swaps_speaker_to_moderated():
    from src.features.admin.admin_cog import AdminCog
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    member = SimpleNamespace(id=111, name='testuser', mention='<@111>',
                             roles=[tier['newbie'], tier['speaker']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    interaction = _make_interaction(cog)

    with patch('src.features.admin.admin_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)):
        asyncio.run(cog.mute_user.callback(cog, interaction, member, 'test reason', None))

    # swapped: newbie+speaker removed, moderated added
    assert len(member.remove_roles.call_args.args) == 2
    removed_ids = {r.id for r in member.remove_roles.call_args.args}
    assert removed_ids == {1, 2}
    assert member.add_roles.call_args.args[0].id == 3
    # DB: status moderated with prior snapshot, DM revoked, cache invalidated
    assert ('set_member_status', 111, 'moderated', 'speaker', True) in cog.db_handler.calls
    assert ('set_member_can_message_bot', 111, False) in cog.db_handler.calls


def test_mute_user_treats_moderated_role_as_already_muted():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    member = SimpleNamespace(id=111, name='testuser', mention='<@111>', roles=[tier['moderated']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    interaction = _make_interaction(cog)

    with patch('src.features.admin.admin_cog.post_mute_to_moderation', new=AsyncMock()):
        asyncio.run(cog.mute_user.callback(cog, interaction, member, 'test reason', None))

    interaction.response.send_message.assert_awaited_once()
    assert 'already muted' in interaction.response.send_message.call_args.args[0]
    assert not member.add_roles.called
    assert not member.remove_roles.called


def test_unmute_user_restores_prior_speaker():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    member = SimpleNamespace(id=111, name='testuser', mention='<@111>', roles=[tier['moderated']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    interaction = _make_interaction(cog)

    asyncio.run(cog.unmute_user.callback(cog, interaction, member))

    assert member.remove_roles.call_args.args[0].id == 3  # moderated removed
    assert member.add_roles.call_args.args[0].id == 2      # speaker restored
    assert ('set_member_status', 111, 'speaker', None, False) in cog.db_handler.calls
    assert ('set_member_can_message_bot', 111, True) in cog.db_handler.calls
    assert ('delete_timed_mute', 111) in cog.db_handler.calls


def test_on_member_update_restores_newbie_when_db_says_newbie():
    cog = _make_cog()
    cog.db_handler.status = MEMBER_STATUS_NEWBIE
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    before = SimpleNamespace(id=111, roles=[tier['newbie']])
    after = SimpleNamespace(id=111, name='testuser', guild=SimpleNamespace(id=456), roles=[])
    after.add_roles = AsyncMock()
    after.remove_roles = AsyncMock()

    asyncio.run(cog.on_member_update(before, after))

    assert after.add_roles.call_args.args[0].id == 1  # newbie re-added
    assert not after.remove_roles.called


def test_on_member_update_never_restores_speaker_for_moderated():
    cog = _make_cog()
    cog.db_handler.status = MEMBER_STATUS_MODERATED
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    before = SimpleNamespace(id=111, roles=[tier['speaker']])
    after = SimpleNamespace(id=111, name='testuser', guild=SimpleNamespace(id=456), roles=[])
    after.add_roles = AsyncMock()
    after.remove_roles = AsyncMock()

    asyncio.run(cog.on_member_update(before, after))

    # moderated: only Moderated is re-added; Speaker is NOT restored
    assert after.add_roles.call_args.args[0].id == 3
    assert not any(call.args and call.args[0].id == 2 for call in after.add_roles.call_args_list)


def test_check_expired_mutes_phase1_restores_prior_newbie():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    member = SimpleNamespace(id=111, name='testuser', roles=[tier['moderated']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = SimpleNamespace(id=456, get_member=lambda mid: member)
    cog.bot = SimpleNamespace(is_owner=AsyncMock(return_value=True), get_guild=lambda gid: guild, guilds=[guild])
    cog.db_handler.expired_mutes = [{
        'member_id': 111, 'guild_id': 456,
        'prior_status': MEMBER_STATUS_NEWBIE, 'prior_can_message_bot': True,
    }]

    asyncio.run(cog.check_expired_mutes())

    assert member.remove_roles.call_args.args[0].id == 3  # moderated removed
    assert member.add_roles.call_args.args[0].id == 1      # newbie restored
    assert ('set_member_status', 111, 'newbie', None, False) in cog.db_handler.calls
    assert ('set_member_can_message_bot', 111, True) in cog.db_handler.calls
    assert ('delete_timed_mute', 111) in cog.db_handler.calls


# ═══════════════════════════════════════════════════════════════════
# gating _approve_member swap
# ═══════════════════════════════════════════════════════════════════

def test_approve_member_swaps_newbie_to_speaker():
    from src.features.gating.gating_cog import GatingCog

    newbie = SimpleNamespace(id=1, name='Newbie')
    speaker = SimpleNamespace(id=2, name='Speaker')
    moderated = SimpleNamespace(id=3, name='Moderated')
    roles = {1: newbie, 2: speaker, 3: moderated}
    member = SimpleNamespace(id=111, name='testuser', display_name='X', roles=[newbie], send=AsyncMock())
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = SimpleNamespace(id=456, get_role=lambda rid: roles.get(rid), get_member=lambda mid: member)

    cog = GatingCog.__new__(GatingCog)
    cog.db = _FakeGatingDB()
    cog._remove_member_messages = lambda member_id: None
    cfg = {'speaker_role_id': 2, 'newbie_role_id': 1, 'moderated_role_id': 3}
    intro = {'member_id': 111, 'message_id': 10, 'channel_id': 20, 'guild_id': 456, 'approval_request_id': None}

    asyncio.run(cog._approve_member(guild, intro, cfg, reacted_message_id=None))

    assert member.remove_roles.call_args.args[0].id == 1  # newbie removed
    assert member.add_roles.call_args.args[0].id == 2      # speaker added
    assert cog.db.status == MEMBER_STATUS_SPEAKER


def test_approve_member_refuses_moderated_member():
    from src.features.gating.gating_cog import GatingCog

    newbie = SimpleNamespace(id=1, name='Newbie')
    speaker = SimpleNamespace(id=2, name='Speaker')
    moderated = SimpleNamespace(id=3, name='Moderated')
    roles = {1: newbie, 2: speaker, 3: moderated}
    member = SimpleNamespace(id=111, name='testuser', display_name='X', roles=[moderated], send=AsyncMock())
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = SimpleNamespace(id=456, get_role=lambda rid: roles.get(rid), get_member=lambda mid: member)

    cog = GatingCog.__new__(GatingCog)
    cog.db = _FakeGatingDB()
    cog._remove_member_messages = lambda member_id: None
    cfg = {'speaker_role_id': 2, 'newbie_role_id': 1, 'moderated_role_id': 3}
    intro = {'member_id': 111, 'message_id': 10, 'channel_id': 20, 'guild_id': 456, 'approval_request_id': None}

    asyncio.run(cog._approve_member(guild, intro, cfg, reacted_message_id=None))

    assert not member.add_roles.called
    assert not member.remove_roles.called
    assert cog.db.status is None  # never approved


def test_approve_member_refuses_db_moderated_member():
    from src.features.gating.gating_cog import GatingCog

    newbie = SimpleNamespace(id=1, name='Newbie')
    speaker = SimpleNamespace(id=2, name='Speaker')
    moderated = SimpleNamespace(id=3, name='Moderated')
    roles = {1: newbie, 2: speaker, 3: moderated}
    member = SimpleNamespace(id=111, name='testuser', display_name='X', roles=[newbie], send=AsyncMock())
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = SimpleNamespace(id=456, get_role=lambda rid: roles.get(rid), get_member=lambda mid: member)

    cog = GatingCog.__new__(GatingCog)
    cog.db = _FakeGatingDB()
    cog.db.status = MEMBER_STATUS_MODERATED  # DB says moderated even though role missing
    cog._remove_member_messages = lambda member_id: None
    cfg = {'speaker_role_id': 2, 'newbie_role_id': 1, 'moderated_role_id': 3}
    intro = {'member_id': 111, 'message_id': 10, 'channel_id': 20, 'guild_id': 456, 'approval_request_id': None}

    asyncio.run(cog._approve_member(guild, intro, cfg, reacted_message_id=None))

    assert not member.add_roles.called
    assert not member.remove_roles.called


def test_execute_mute_speaker_preserves_snapshot_on_remute():
    """Re-muting an already-moderated member must not clobber the prior tier/DM snapshot."""
    from src.features.admin_chat.tools import execute_mute_speaker

    tier = _make_tier_roles()
    member = SimpleNamespace(id=111, name='testuser', roles=[tier['moderated']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    role_by_id = {1: tier['newbie'], 2: tier['speaker'], 3: tier['moderated']}
    guild = SimpleNamespace(id=456, get_role=lambda rid: role_by_id.get(rid), get_member=lambda mid: member)

    class _ReMuteDB(_FakeDB):
        def get_member_status(self, member_id, guild_id=None):
            return MEMBER_STATUS_MODERATED  # currently moderated (re-mute)

        def get_guild_member(self, member_id, guild_id):
            return {'prior_status': 'newbie', 'prior_can_message_bot': True}

    db = _ReMuteDB()
    bot = SimpleNamespace(get_guild=lambda gid: guild, get_cog=lambda name: None)
    params = {'user_id': '111', 'reason': 'extend', 'duration': '1w',
              'guild_id': '456', 'admin_user_id': '999'}

    with patch('src.features.admin_chat.tools._resolve_guild_id', return_value=456), \
         patch('src.features.admin_chat.tools._is_speaker_management_enabled', return_value=True), \
         patch('src.features.admin_chat.tools._resolve_speaker_role_id', return_value=2), \
         patch('src.features.admin_chat.tools._resolve_newbie_role_id', return_value=1), \
         patch('src.features.admin_chat.tools._resolve_moderated_role_id', return_value=3), \
         patch('src.features.admin.admin_cog.post_mute_to_moderation', new=AsyncMock(return_value=True)):
        result = asyncio.run(execute_mute_speaker(bot, db, params))

    assert result['success'] is True
    # set_prior=False on the re-mute so the original snapshot survives
    set_calls = [c for c in db.calls if c[0] == 'set_member_status']
    assert set_calls, "set_member_status should have been called"
    assert set_calls[-1][-1] is False  # set_prior kwarg
    # timed mute carries the ORIGINAL prior tier (newbie), not a fresh 'speaker'
    cm_calls = [c for c in db.calls if c[0] == 'create_timed_mute']
    assert cm_calls and cm_calls[0][1]['prior_status'] == 'newbie'
    assert cm_calls[0][1]['prior_can_message_bot'] is True


def test_invalidate_dm_cache_finds_admincog_name():
    """discord.py keeps the full class name as the cog name (AdminCog, not Admin)."""
    from src.features.admin_chat.tools import _invalidate_dm_cache

    cache = {111: (0, True)}
    admin_cog = SimpleNamespace(_dm_access_cache=cache)
    bot = SimpleNamespace(get_cog=lambda name: admin_cog if name == 'AdminCog' else None)
    _invalidate_dm_cache(bot, 111)
    assert 111 not in cache


def test_on_member_join_writes_status_and_assigns_newbie():
    cog = _make_cog()
    cog._is_speaker_management_enabled = lambda gid: True
    cog._get_newbie_role_id = lambda gid: 1
    newbie_role = SimpleNamespace(id=1, name='Newbie')
    member = SimpleNamespace(id=111, name='x', bot=False, roles=[],
                             guild=SimpleNamespace(id=456, get_role=lambda rid: newbie_role))
    member.add_roles = AsyncMock()

    asyncio.run(cog.on_member_join(member))

    assert ('set_member_status', 111, 'newbie', None, False) in cog.db_handler.calls
    assert member.add_roles.call_args.args[0].id == 1


# ═══════════════════════════════════════════════════════════════════
# Speaker-invite auto-assignment
# ═══════════════════════════════════════════════════════════════════

def _make_invite_cog():
    from src.features.admin.admin_cog import AdminCog
    cog = AdminCog.__new__(AdminCog)
    cog.bot = SimpleNamespace()
    cog._get_speaker_invite_code = lambda gid: 'abc'
    cog._speaker_invite_uses = {}
    return cog


def test_speaker_invite_join_detected_when_uses_increment():
    cog = _make_invite_cog()
    cog.bot.fetch_invite = AsyncMock(return_value=SimpleNamespace(uses=5))
    cog._speaker_invite_uses = {456: 4}
    assert asyncio.run(cog._is_speaker_invite_join(SimpleNamespace(id=456))) is True


def test_speaker_invite_join_not_detected_without_baseline():
    cog = _make_invite_cog()
    cog.bot.fetch_invite = AsyncMock(return_value=SimpleNamespace(uses=5))
    assert asyncio.run(cog._is_speaker_invite_join(SimpleNamespace(id=456))) is False


def test_speaker_invite_join_not_detected_when_uses_unchanged():
    cog = _make_invite_cog()
    cog.bot.fetch_invite = AsyncMock(return_value=SimpleNamespace(uses=4))
    cog._speaker_invite_uses = {456: 4}
    assert asyncio.run(cog._is_speaker_invite_join(SimpleNamespace(id=456))) is False


def test_speaker_invite_join_fails_closed_on_fetch_error():
    cog = _make_invite_cog()
    resp = SimpleNamespace(status=404, reason='Not Found')
    cog.bot.fetch_invite = AsyncMock(side_effect=discord.NotFound(resp, 'x'))
    cog._speaker_invite_uses = {456: 4}
    assert asyncio.run(cog._is_speaker_invite_join(SimpleNamespace(id=456))) is False


def test_assign_speaker_on_join_swaps_newbie_to_speaker():
    cog = _make_cog()
    tier = _make_tier_roles()
    cog._resolve_tier_roles = lambda guild: tier
    member = SimpleNamespace(id=111, name='x', guild=SimpleNamespace(id=456), roles=[tier['newbie']])
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()

    asyncio.run(cog._assign_speaker_on_join(member))

    assert member.remove_roles.call_args.args[0].id == 1  # newbie removed
    assert member.add_roles.call_args.args[0].id == 2      # speaker added
    assert ('set_member_status', 111, 'speaker', None, False) in cog.db_handler.calls


def test_direct_invite_requires_equity_holders_role():
    cog = _make_cog()
    cog._speaker_invite_uses = {}
    cog._get_equity_holders_role_id = lambda gid: 999
    channel = SimpleNamespace(create_invite=AsyncMock())
    cog._get_default_invite_channel = lambda guild: channel
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name='x', roles=[]),  # not equity holder
        guild_id=456, guild=SimpleNamespace(id=456, get_member=lambda uid: None,
                                            fetch_member=AsyncMock(return_value=None)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    asyncio.run(cog.direct_invite.callback(cog, interaction))
    assert interaction.response.defer.called
    assert 'Equity Holders' in interaction.followup.send.call_args.args[0]
    assert not channel.create_invite.called


def test_direct_invite_creates_in_live_updates_with_default_uses():
    cog = _make_cog()
    cog._speaker_invite_uses = {}
    cog._get_equity_holders_role_id = lambda gid: 999
    invite = SimpleNamespace(code='abc123', url='https://discord.gg/abc123', uses=0)
    channel = SimpleNamespace(create_invite=AsyncMock(return_value=invite))
    cog._get_default_invite_channel = lambda guild: channel
    eq_role = SimpleNamespace(id=999)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name='admin', roles=[eq_role]),
        guild_id=456, guild=SimpleNamespace(id=456, get_member=lambda uid: None,
                                            fetch_member=AsyncMock(return_value=None)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    sc = SimpleNamespace(set_server_field=MagicMock(return_value=True))
    cog.server_config = sc
    asyncio.run(cog.direct_invite.callback(cog, interaction))

    assert channel.create_invite.called
    assert channel.create_invite.call_args.kwargs['max_uses'] == 10  # default cap
    assert sc.set_server_field.called
    assert cog._speaker_invite_uses[456] == 0
    assert 'https://discord.gg/abc123' in interaction.followup.send.call_args.args[0]


def test_direct_invite_clamps_max_uses_to_10():
    cog = _make_cog()
    cog._speaker_invite_uses = {}
    cog._get_equity_holders_role_id = lambda gid: 999
    channel = SimpleNamespace(create_invite=AsyncMock(return_value=SimpleNamespace(code='x', url='u', uses=0)))
    cog._get_default_invite_channel = lambda guild: channel
    eq_role = SimpleNamespace(id=999)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name='admin', roles=[eq_role]),
        guild_id=456, guild=SimpleNamespace(id=456, get_member=lambda uid: None,
                                            fetch_member=AsyncMock(return_value=None)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    sc = SimpleNamespace(set_server_field=MagicMock(return_value=True))
    cog.server_config = sc
    asyncio.run(cog.direct_invite.callback(cog, interaction, 100))  # clamp to 10
    assert channel.create_invite.call_args.kwargs['max_uses'] == 10


def test_direct_invite_resolves_plain_user_to_member_for_role_check():
    cog = _make_cog()
    cog._speaker_invite_uses = {}
    cog._get_equity_holders_role_id = lambda gid: 999
    eq_member = SimpleNamespace(id=1, name='admin', roles=[SimpleNamespace(id=999)])
    channel = SimpleNamespace(create_invite=AsyncMock(return_value=SimpleNamespace(code='x', url='u', uses=0)))
    cog._get_default_invite_channel = lambda guild: channel
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name='admin'),  # plain User, no roles attr
        guild_id=456, guild=SimpleNamespace(id=456, get_member=lambda uid: eq_member,
                                            fetch_member=AsyncMock(return_value=eq_member)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    sc = SimpleNamespace(set_server_field=MagicMock(return_value=True))
    cog.server_config = sc
    asyncio.run(cog.direct_invite.callback(cog, interaction))
    assert channel.create_invite.called  # role check passed via resolved member


def test_direct_invite_fetches_member_when_not_in_cache():
    cog = _make_cog()
    cog._speaker_invite_uses = {}
    cog._get_equity_holders_role_id = lambda gid: 999
    eq_member = SimpleNamespace(id=1, name='admin', roles=[SimpleNamespace(id=999)])
    channel = SimpleNamespace(create_invite=AsyncMock(return_value=SimpleNamespace(code='x', url='u', uses=0)))
    cog._get_default_invite_channel = lambda guild: channel
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name='admin'),
        guild_id=456,
        guild=SimpleNamespace(id=456, get_member=lambda uid: None,
                              fetch_member=AsyncMock(return_value=eq_member)),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    sc = SimpleNamespace(set_server_field=MagicMock(return_value=True))
    cog.server_config = sc
    asyncio.run(cog.direct_invite.callback(cog, interaction))
    assert interaction.guild.fetch_member.called
    assert channel.create_invite.called


def test_direct_invite_works_from_dm():
    cog = _make_cog()
    cog._speaker_invite_uses = {}
    cog._get_equity_holders_role_id = lambda gid: 999
    cog._is_speaker_management_enabled = lambda gid: True
    eq_member = SimpleNamespace(id=1, name='admin', roles=[SimpleNamespace(id=999)])
    channel = SimpleNamespace(create_invite=AsyncMock(return_value=SimpleNamespace(code='x', url='u', uses=0)))
    cog._get_default_invite_channel = lambda guild: channel
    guild = SimpleNamespace(id=456, get_member=lambda uid: eq_member,
                            fetch_member=AsyncMock(return_value=eq_member))
    cog.bot.guilds = [guild]  # DM: no interaction.guild, bot resolves the managed guild
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name='admin'),
        guild_id=None,  # DM context
        guild=None,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    sc = SimpleNamespace(set_server_field=MagicMock(return_value=True))
    cog.server_config = sc
    asyncio.run(cog.direct_invite.callback(cog, interaction))
    assert channel.create_invite.called
    assert sc.set_server_field.called


class _FakeGatingDB:
    def __init__(self):
        self.status = None

    def approve_pending_intro(self, message_id, guild_id=None):
        self.approved = (message_id, guild_id)

    def get_member_status(self, member_id, guild_id=None):
        return self.status

    def set_member_status(self, member_id, guild_id, status, **kwargs):
        self.status = status
        return True

    def get_member_for_approval(self, member_id):
        return None
