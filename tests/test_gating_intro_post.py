"""Tests for the web-application -> intro-channel poster in gating_cog.

Implements the marker round-trip test inline. The remaining 16 cases from the
T12 specification are scaffolded with `pytest.mark.skip` stubs so the gaps are
discoverable in CI output and easy to flesh out incrementally.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.features.gating.intro_embed import (
    APP_MARKER_PREFIX,
    build_application_embed,
    extract_approval_request_marker,
)


# ---- (1) Marker round-trip ---------------------------------------------------


def _make_message(footer_text):
    embed = SimpleNamespace(footer=SimpleNamespace(text=footer_text))
    return SimpleNamespace(embeds=[embed])


def test_build_application_embed_writes_marker_footer():
    member = {"username": "creator", "global_name": "Creator", "avatar_url": None, "bio": None}
    approval_request = {"id": "00000000-0000-4000-8000-000000000001", "bio_snapshot": "hello"}
    embed = build_application_embed(member, approval_request, None)
    assert embed.footer.text.endswith(f"{APP_MARKER_PREFIX}{approval_request['id']}")


def test_extract_approval_request_marker_round_trip():
    member = {"username": "creator", "global_name": "Creator", "avatar_url": None, "bio": None}
    approval_request = {"id": "11111111-1111-4111-8111-111111111111", "bio_snapshot": "hi"}
    embed = build_application_embed(member, approval_request, None)
    msg = SimpleNamespace(embeds=[embed])
    assert extract_approval_request_marker(msg) == approval_request["id"]


def test_extract_approval_request_marker_returns_none_without_footer():
    msg = SimpleNamespace(embeds=[SimpleNamespace(footer=None)])
    assert extract_approval_request_marker(msg) is None


def test_extract_approval_request_marker_returns_none_without_marker():
    msg = _make_message("just a regular footer with no marker")
    assert extract_approval_request_marker(msg) is None


def test_extract_approval_request_marker_returns_none_for_no_embeds():
    msg = SimpleNamespace(embeds=[])
    assert extract_approval_request_marker(msg) is None


# ---- (2) DB layer ------------------------------------------------------------


@pytest.mark.skip(reason="TODO: assert create_pending_intro includes approval_request_id when supplied; returns None on stubbed 23505; re-raises otherwise")
def test_create_pending_intro_includes_approval_request_id():
    pass


@pytest.mark.skip(reason="TODO: assert claim_pending_approval_requests invokes the RPC with the correct limit")
def test_claim_pending_approval_requests_uses_limit():
    pass


@pytest.mark.skip(reason="TODO: assert mark_approval_request_posted issues the right UPDATE")
def test_mark_approval_request_posted_issues_update():
    pass


@pytest.mark.skip(reason="TODO: assert get_pending_intro_by_approval_request, get_approval_request, list_unstamped_intros issue the right calls")
def test_db_helper_calls():
    pass


# ---- (3-11) Poller paths -----------------------------------------------------


@pytest.mark.skip(reason="TODO: poller happy path — pre-send lookup None, send, insert OK, dict populated, _stamp_with_retry called; assert ordering")
def test_poller_happy_path():
    pass


@pytest.mark.skip(reason="TODO: poller pre-send short-circuit — existing row found, _stamp_with_retry called BEFORE channel.send; channel.send NEVER called; dict NOT mutated; insert NOT called")
def test_poller_pre_send_short_circuit():
    pass


@pytest.mark.skip(reason="TODO: poller send failure — no insert, no dict mutation, no stamp call")
def test_poller_send_failure():
    pass


@pytest.mark.skip(reason="TODO: poller 23505 with existing row — pre-send None, insert returns None, second lookup hits, _stamp_with_retry(ar_id, existing.message_id), msg.delete() called, dict NOT mutated")
def test_poller_23505_with_existing_row():
    pass


@pytest.mark.skip(reason="TODO: poller 23505 with no existing row — _stamp_with_retry NOT called, msg.delete() IS called")
def test_poller_23505_without_existing_row():
    pass


@pytest.mark.skip(reason="TODO: poller stamp first-call False / second True — dict stays populated; one extra mark_approval_request_posted observed")
def test_poller_stamp_retry():
    pass


@pytest.mark.skip(reason="TODO: poller stamp persistent failure — warning logged, row left for next-tick recovery")
def test_poller_stamp_persistent_failure():
    pass


@pytest.mark.skip(reason="TODO: poller non-23505 insert error — embed NOT deleted; reconciliation will stitch")
def test_poller_non_23505_insert_error():
    pass


@pytest.mark.skip(reason="TODO: ordering invariant — create_pending_intro called BEFORE _pending_messages.__setitem__ AND BEFORE mark_approval_request_posted")
def test_poller_ordering_invariant():
    pass


# ---- (12-15) Reconciliation phases ------------------------------------------


@pytest.mark.skip(reason="TODO: reconciliation phase 1 — fixture pending_intros with NULL ar.posted_message_id triggers mark_approval_request_posted")
def test_reconciliation_phase_1_stamps_unstamped():
    pass


@pytest.mark.skip(reason="TODO: reconciliation phase 2 stitch — bot embed with marker found, ar pending, no pending_intros: insert + populate dict + stamp")
def test_reconciliation_phase_2_stitches_orphan():
    pass


@pytest.mark.skip(reason="TODO: reconciliation phase 2 newest-first dedupe — two messages with same marker (oldest_first=False), older message.delete() called")
def test_reconciliation_phase_2_dedupe_keeps_newest():
    pass


@pytest.mark.skip(reason="TODO: reconciliation phase 2 already-decided skip — marker matches ar with status='approved', no insert/delete/stamp")
def test_reconciliation_phase_2_skips_decided():
    pass


# ---- (16) DM extension -------------------------------------------------------


@pytest.mark.skip(reason="TODO: DM extension — personalized URL when slug present, generic line otherwise; never raises on lookup error")
def test_approval_dm_extension():
    pass


# ---- (17) Organic intro regression ------------------------------------------


@pytest.mark.skip(reason="TODO: organic intro regression — on_message for non-Speaker still inserts pending_intros with approval_request_id IS NULL; reaction grants Speaker; trigger does NOT fire")
def test_organic_intro_regression():
    pass


# Suppress unused-import warning for test scaffolding.
_ = MagicMock


# =============================================================================
# MP4 — bot-side edit-dirty refresh coverage
#
# Covers:
#   - DatabaseHandler.claim_dirty_intro_edits / mark_embed_updated /
#     clear_posted_message_id (db_handler.py:3428-3505)
#   - The edit-dirty branch of GatingCog.poll_approval_requests
#     (gating_cog.py:616-684)
#   - The members.bio-first preference in build_application_embed
#     (intro_embed.py:48-55)
#
# All tests are pure unit tests: no Discord, no Supabase, no network.
# =============================================================================


# ── DB layer ─────────────────────────────────────────────────────────────────


def _make_fake_supabase_chain(rows):
    """Return a stub supabase client whose from_(...).select(...).chain... matches
    the call sequence used by claim_dirty_intro_edits.

    The returned object exposes a ``.calls`` dict so each link's args can be
    asserted in the test.
    """

    calls = {}
    chain = MagicMock()
    chain.calls = calls

    # The fluent chain repeatedly returns ``chain`` itself, which means each
    # method call is recorded on the same MagicMock and we can drill into
    # ``chain.<method>.call_args_list`` from the test.
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.or_.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain

    # ``.not_`` is an attribute (not a call), and ``.is_(...)`` is a call on it.
    not_proxy = MagicMock()
    not_proxy.is_.return_value = chain
    chain.not_ = not_proxy

    # ``.execute()`` returns the data envelope.
    chain.execute.return_value = SimpleNamespace(data=rows)

    client = MagicMock()
    client.from_.return_value = chain
    return client, chain, not_proxy


def _make_db_handler_stub():
    """Build a bare DatabaseHandler-like object that bypasses the heavy
    __init__. Tests attach a fake supabase_client and patch the embed
    hydration helpers as needed."""
    from src.common.db_handler import DatabaseHandler

    db = DatabaseHandler.__new__(DatabaseHandler)
    db.storage_handler = SimpleNamespace(supabase_client=MagicMock())
    return db


def test_claim_dirty_intro_edits_filters_pending_dirty_unstaled():
    db = _make_db_handler_stub()
    rows = [
        {
            'id': 'ar-1',
            'member_id': 11,
            'attached_media_id': 'media-1',
            'attached_resource_id': None,
            'posted_message_id': '999',
            'bio_snapshot': 'snap',
            'status': 'pending',
            'embed_dirty': True,
            'embed_updated_at': None,
            'created_at': '2026-04-24T00:00:00Z',
        },
    ]
    client, chain, not_proxy = _make_fake_supabase_chain(rows)
    db.storage_handler.supabase_client = client

    # Capture hydration calls so we can assert media/asset are attached.
    db._get_media_for_embed = MagicMock(return_value={'id': 'media-1', 'preview_url': 'p'})
    db._get_asset_for_embed = MagicMock(return_value=None)

    out = db.claim_dirty_intro_edits(limit=7)

    assert len(out) == 1
    assert out[0]['media'] == {'id': 'media-1', 'preview_url': 'p'}
    assert out[0]['asset'] is None

    # Table targeted via from_('approval_requests').
    client.from_.assert_called_once_with('approval_requests')

    # status='pending' AND embed_dirty=True both pinned via .eq().
    eq_args = [c.args for c in chain.eq.call_args_list]
    assert ('status', 'pending') in eq_args
    assert ('embed_dirty', True) in eq_args

    # not_.is_('posted_message_id', 'null') — exclude already-deleted rows.
    not_proxy.is_.assert_called_once_with('posted_message_id', 'null')

    # or_ clause covers the 30s staleness threshold.
    or_arg = chain.or_.call_args.args[0]
    assert 'embed_updated_at.is.null' in or_arg
    assert 'embed_updated_at.lt.' in or_arg

    # Two .order() calls: embed_updated_at NULLS FIRST then created_at ASC.
    order_calls = chain.order.call_args_list
    assert len(order_calls) == 2
    assert order_calls[0].args[0] == 'embed_updated_at'
    assert order_calls[0].kwargs.get('nullsfirst') is True
    assert order_calls[1].args[0] == 'created_at'

    # Limit propagated.
    chain.limit.assert_called_once_with(7)

    # Hydration helpers actually invoked for the row's media/asset ids.
    db._get_media_for_embed.assert_called_once_with('media-1')
    db._get_asset_for_embed.assert_called_once_with(None)


def test_claim_dirty_intro_edits_returns_empty_list_on_error(caplog):
    db = _make_db_handler_stub()
    client = MagicMock()
    client.from_.side_effect = RuntimeError('boom')
    db.storage_handler.supabase_client = client

    with caplog.at_level(logging.ERROR, logger='DiscordBot'):
        out = db.claim_dirty_intro_edits(limit=5)

    assert out == []
    assert any('Error claiming dirty intro edits' in rec.message for rec in caplog.records)


def test_mark_embed_updated_writes_dirty_false_and_timestamp():
    db = _make_db_handler_stub()
    client = db.storage_handler.supabase_client

    table_chain = MagicMock()
    table_chain.update.return_value = table_chain
    table_chain.eq.return_value = table_chain
    table_chain.execute.return_value = SimpleNamespace(data=[{'id': 'ar-9'}])
    client.table.return_value = table_chain

    ok = db.mark_embed_updated('ar-9')

    assert ok is True
    client.table.assert_called_once_with('approval_requests')
    payload = table_chain.update.call_args.args[0]
    assert payload['embed_dirty'] is False
    # ISO 8601 timestamp — must contain a 'T' separator.
    assert isinstance(payload['embed_updated_at'], str)
    assert 'T' in payload['embed_updated_at']
    table_chain.eq.assert_called_once_with('id', 'ar-9')


def test_clear_posted_message_id_writes_null():
    db = _make_db_handler_stub()
    client = db.storage_handler.supabase_client

    table_chain = MagicMock()
    table_chain.update.return_value = table_chain
    table_chain.eq.return_value = table_chain
    table_chain.execute.return_value = SimpleNamespace(data=[{'id': 'ar-3'}])
    client.table.return_value = table_chain

    ok = db.clear_posted_message_id('ar-3')

    assert ok is True
    client.table.assert_called_once_with('approval_requests')
    payload = table_chain.update.call_args.args[0]
    assert payload == {'posted_message_id': None}
    table_chain.eq.assert_called_once_with('id', 'ar-3')


# ── Edit-dirty branch of poll_approval_requests ─────────────────────────────


def _make_gating_cog():
    """Construct a GatingCog with a stub bot and a stub DB. Patches
    _get_primary_intro_target so the loop sees a usable channel."""
    # Lazy import so the file remains importable on Python 3.8 even before
    # the module-level future-annotations switch is applied.
    from src.features.gating.gating_cog import GatingCog

    bot = SimpleNamespace(db_handler=None, guilds=[], user=SimpleNamespace(id=42))
    cog = GatingCog(bot)
    cog.db = MagicMock()
    # No new posts in any of the edit-dirty tests; keep the post-loop empty.
    cog.db.claim_pending_approval_requests = MagicMock(return_value=[])

    guild = SimpleNamespace(id=1)
    intro_channel = SimpleNamespace(id=2, fetch_message=AsyncMock())
    cog._get_primary_intro_target = MagicMock(return_value=(guild, intro_channel, {}))
    return cog, intro_channel


def _run_poll(cog):
    """Drive the underlying coroutine of the poll_approval_requests loop."""
    return asyncio.get_event_loop().run_until_complete(cog.poll_approval_requests())


@pytest.fixture
def fresh_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _make_dirty_row(ar_id='ar-dirty', member_id=77, posted_message_id=12345):
    return {
        'id': ar_id,
        'member_id': member_id,
        'posted_message_id': posted_message_id,
        'bio_snapshot': 'snap',
        'media': None,
        'asset': None,
    }


def _attach_member(cog, member_id=77):
    cog.db.get_member_for_approval = MagicMock(return_value={
        'member_id': member_id,
        'username': 'u',
        'global_name': 'U',
        'avatar_url': None,
        'bio': 'fresh bio',
    })


def test_dirty_edit_happy_path(fresh_event_loop):
    cog, intro_channel = _make_gating_cog()
    row = _make_dirty_row()
    cog.db.claim_dirty_intro_edits = MagicMock(return_value=[row])
    _attach_member(cog)
    cog.db.mark_embed_updated = MagicMock(return_value=True)
    cog.db.clear_posted_message_id = MagicMock()

    msg = SimpleNamespace(edit=AsyncMock())
    intro_channel.fetch_message = AsyncMock(return_value=msg)

    fresh_event_loop.run_until_complete(cog.poll_approval_requests())

    intro_channel.fetch_message.assert_awaited_once_with(12345)
    msg.edit.assert_awaited_once()
    # The edit must pass a Discord embed.
    embed_arg = msg.edit.await_args.kwargs.get('embed')
    assert isinstance(embed_arg, discord.Embed)

    cog.db.mark_embed_updated.assert_called_once_with('ar-dirty')
    cog.db.clear_posted_message_id.assert_not_called()


def test_dirty_edit_message_deleted_clears_posted_id(fresh_event_loop):
    cog, intro_channel = _make_gating_cog()
    row = _make_dirty_row()
    cog.db.claim_dirty_intro_edits = MagicMock(return_value=[row])
    _attach_member(cog)
    cog.db.mark_embed_updated = MagicMock()
    cog.db.clear_posted_message_id = MagicMock(return_value=True)

    fake_resp = SimpleNamespace(status=404, reason='Not Found')
    intro_channel.fetch_message = AsyncMock(side_effect=discord.NotFound(fake_resp, 'gone'))

    fresh_event_loop.run_until_complete(cog.poll_approval_requests())

    cog.db.clear_posted_message_id.assert_called_once_with('ar-dirty')
    cog.db.mark_embed_updated.assert_not_called()


def test_dirty_edit_http_exception_leaves_dirty(fresh_event_loop, caplog):
    cog, intro_channel = _make_gating_cog()
    row = _make_dirty_row()
    cog.db.claim_dirty_intro_edits = MagicMock(return_value=[row])
    _attach_member(cog)
    cog.db.mark_embed_updated = MagicMock()
    cog.db.clear_posted_message_id = MagicMock()

    fake_resp = SimpleNamespace(status=429, reason='Too Many Requests')
    msg = SimpleNamespace(edit=AsyncMock(side_effect=discord.HTTPException(fake_resp, 'rate-limited')))
    intro_channel.fetch_message = AsyncMock(return_value=msg)

    with caplog.at_level(logging.WARNING, logger='DiscordBot'):
        fresh_event_loop.run_until_complete(cog.poll_approval_requests())

    cog.db.mark_embed_updated.assert_not_called()
    cog.db.clear_posted_message_id.assert_not_called()
    assert any('edit failed for approval ar-dirty' in rec.message for rec in caplog.records)


def test_dirty_edit_one_bad_row_doesnt_kill_loop(fresh_event_loop):
    cog, intro_channel = _make_gating_cog()
    bad = _make_dirty_row(ar_id='ar-bad', posted_message_id=11)
    good = _make_dirty_row(ar_id='ar-good', posted_message_id=22)
    cog.db.claim_dirty_intro_edits = MagicMock(return_value=[bad, good])
    _attach_member(cog)
    cog.db.mark_embed_updated = MagicMock(return_value=True)
    cog.db.clear_posted_message_id = MagicMock()

    good_msg = SimpleNamespace(edit=AsyncMock())

    async def fetch_side_effect(message_id):
        if message_id == 11:
            raise RuntimeError('exploded')
        return good_msg

    intro_channel.fetch_message = AsyncMock(side_effect=fetch_side_effect)

    fresh_event_loop.run_until_complete(cog.poll_approval_requests())

    # Bad row didn't stop processing; the second row was still marked done.
    cog.db.mark_embed_updated.assert_called_once_with('ar-good')
    good_msg.edit.assert_awaited_once()


def test_dirty_edit_outer_failure_doesnt_break_post_loop(fresh_event_loop, caplog):
    cog, intro_channel = _make_gating_cog()
    cog.db.claim_dirty_intro_edits = MagicMock(side_effect=RuntimeError('db down'))
    cog.db.mark_embed_updated = MagicMock()
    cog.db.clear_posted_message_id = MagicMock()

    with caplog.at_level(logging.ERROR, logger='DiscordBot'):
        # Must not raise — the outer try/except has to swallow this so the
        # next poll tick still runs.
        fresh_event_loop.run_until_complete(cog.poll_approval_requests())

    # The outer except wrote a log entry and we never reached per-row state
    # mutators.
    cog.db.mark_embed_updated.assert_not_called()
    cog.db.clear_posted_message_id.assert_not_called()
    assert any('dirty-edit refresh block failed' in rec.message for rec in caplog.records)


# ── build_application_embed bio freshness ───────────────────────────────────


def _member(bio):
    return {
        'username': 'creator',
        'global_name': 'Creator',
        'avatar_url': None,
        'bio': bio,
    }


def test_build_application_embed_prefers_member_bio_over_snapshot():
    member = _member(bio='fresh')
    ar = {'id': '00000000-0000-4000-8000-000000000001', 'bio_snapshot': 'stale'}
    embed = build_application_embed(member, ar, None)
    assert 'fresh' in embed.description
    assert 'stale' not in embed.description


def test_build_application_embed_omits_snapshot_if_member_bio_missing():
    member = _member(bio=None)
    ar = {'id': '00000000-0000-4000-8000-000000000002', 'bio_snapshot': 'snapshot text'}
    embed = build_application_embed(member, ar, None)
    assert embed.description == 'No bio provided.'
    assert 'snapshot text' not in embed.description


def test_build_application_embed_omits_snapshot_if_member_bio_empty():
    member = _member(bio='')
    ar = {'id': '00000000-0000-4000-8000-000000000003', 'bio_snapshot': 'old text'}
    embed = build_application_embed(member, ar, None)
    assert embed.description == 'No bio provided.'
    assert 'old text' not in embed.description


def test_build_application_embed_no_bio_provided():
    member = _member(bio=None)
    ar = {'id': '00000000-0000-4000-8000-000000000003', 'bio_snapshot': None}
    embed = build_application_embed(member, ar, None)
    assert embed.description == 'No bio provided.'


# =============================================================================
# _recover_untracked_intro — admit a member from ANY of their messages
#
# Regression coverage for the fix where an approver reacting to a member's
# reply / follow-up in the intro channel was silently dropped. A member who
# already has an open pending intro must now be admitted from any of their
# messages, not just the originally-tracked intro.
# =============================================================================

_GUILD_ID = 1076117621407223829
_INTRO_CHANNEL_ID = 1138861011206688829
_SPEAKER_ROLE_ID = 1475121624855482418
_MEMBER_ID = 618927024295116810   # 0xdeluxa
_APPROVER_ID = 301463647895683072


def _make_cog_for_recovery(*, speaker=False):
    """Build a GatingCog wired for unit-testing _recover_untracked_intro.

    ``speaker=True`` makes the message author already hold the Speaker role so
    the early-out guard is exercised.
    """
    from src.features.gating.gating_cog import GatingCog

    bot = SimpleNamespace(user=SimpleNamespace(id=42))
    cog = GatingCog(bot)
    cog.db = MagicMock()
    cog._get_gating_config = MagicMock(return_value={
        'gate_channel_id': 100,
        'intro_channel_id': _INTRO_CHANNEL_ID,
        'speaker_role_id': _SPEAKER_ROLE_ID,
        'approver_role_id': 1328101710488408207,
        'super_approver_role_id': 1138851070311931946,
    })

    speaker_role = SimpleNamespace(id=_SPEAKER_ROLE_ID)
    member = SimpleNamespace(id=_MEMBER_ID, roles=[speaker_role] if speaker else [])

    guild = MagicMock()
    guild.get_role.return_value = speaker_role
    guild.get_member.return_value = member
    guild.fetch_member = AsyncMock(return_value=member)
    bot.get_guild = MagicMock(return_value=guild)

    channel = MagicMock()
    guild.get_channel.return_value = channel
    return cog, channel


def _reply_message(msg_id, author_id, replied_author_id):
    """A message that replies to a different author (a conversation, not an intro)."""
    return SimpleNamespace(
        id=msg_id,
        author=SimpleNamespace(id=author_id, bot=False),
        reference=SimpleNamespace(
            resolved=SimpleNamespace(author=SimpleNamespace(id=replied_author_id))
        ),
    )


def _top_level_message(msg_id, author_id):
    """A standalone (non-reply) message."""
    return SimpleNamespace(
        id=msg_id, author=SimpleNamespace(id=author_id, bot=False), reference=None
    )


def _payload(msg_id):
    return SimpleNamespace(guild_id=_GUILD_ID, channel_id=_INTRO_CHANNEL_ID, message_id=msg_id)


def test_recovery_resolves_reply_to_existing_pending_intro(fresh_event_loop):
    # The bug: approver reacted to the member's *reply*, not their tracked intro.
    cog, channel = _make_cog_for_recovery()
    channel.fetch_message = AsyncMock(
        return_value=_reply_message(1520415968205996034, _MEMBER_ID, _APPROVER_ID)
    )
    cog.db.get_pending_intro_by_member = MagicMock(
        return_value={'id': 501, 'member_id': _MEMBER_ID}
    )

    result = fresh_event_loop.run_until_complete(
        cog._recover_untracked_intro(_payload(1520415968205996034))
    )

    assert result == _MEMBER_ID
    assert cog._pending_messages[1520415968205996034] == _MEMBER_ID
    cog.db.get_pending_intro_by_member.assert_called_once()


def test_recovery_creates_intro_from_reply_without_pending_intro(fresh_event_loop):
    # Any of the member's messages — including a reply — can seed a pending intro, so an
    # approver reacting to a reply admits them even with no prior intro tracked.
    cog, channel = _make_cog_for_recovery()
    channel.fetch_message = AsyncMock(
        return_value=_reply_message(111, _MEMBER_ID, _APPROVER_ID)
    )
    cog.db.get_pending_intro_by_member = MagicMock(return_value=None)
    cog.db.create_pending_intro = MagicMock(return_value={'id': 999})

    result = fresh_event_loop.run_until_complete(cog._recover_untracked_intro(_payload(111)))

    assert result == _MEMBER_ID
    cog.db.create_pending_intro.assert_called_once()
    assert cog._pending_messages[111] == _MEMBER_ID


def test_recovery_creates_intro_for_top_level_message(fresh_event_loop):
    # Pre-existing behaviour preserved: an untracked standalone intro is created on the fly.
    cog, channel = _make_cog_for_recovery()
    channel.fetch_message = AsyncMock(return_value=_top_level_message(222, _MEMBER_ID))
    cog.db.get_pending_intro_by_member = MagicMock(return_value=None)
    cog.db.create_pending_intro = MagicMock(return_value={'id': 999})

    result = fresh_event_loop.run_until_complete(cog._recover_untracked_intro(_payload(222)))

    assert result == _MEMBER_ID
    cog.db.create_pending_intro.assert_called_once()
    assert cog._pending_messages[222] == _MEMBER_ID


def test_recovery_skips_already_speaker(fresh_event_loop):
    cog, channel = _make_cog_for_recovery(speaker=True)
    channel.fetch_message = AsyncMock(
        return_value=_reply_message(333, _MEMBER_ID, _APPROVER_ID)
    )
    cog.db.get_pending_intro_by_member = MagicMock(
        return_value={'id': 501, 'member_id': _MEMBER_ID}
    )

    result = fresh_event_loop.run_until_complete(cog._recover_untracked_intro(_payload(333)))

    assert result is None
    # Bailed at the speaker guard before ever consulting the pending-intro row.
    cog.db.get_pending_intro_by_member.assert_not_called()


def test_recovery_skips_bot_message(fresh_event_loop):
    cog, channel = _make_cog_for_recovery()
    msg = _top_level_message(444, _MEMBER_ID)
    msg.author = SimpleNamespace(id=_MEMBER_ID, bot=True)
    channel.fetch_message = AsyncMock(return_value=msg)
    cog.db.get_pending_intro_by_member = MagicMock()

    result = fresh_event_loop.run_until_complete(cog._recover_untracked_intro(_payload(444)))

    assert result is None
    cog.db.get_pending_intro_by_member.assert_not_called()


# =============================================================================
# Periodic intro reconciliation — recover missed gateway events
# =============================================================================


class _HistoryChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self._messages = messages

    async def history(self, *, limit):
        assert limit == 200
        for message in self._messages:
            yield message


class _HistoryReaction:
    def __init__(self, users):
        self._users = users

    async def users(self):
        for user in self._users:
            yield user


def _make_reconcile_cog(messages, *, target_roles=None, reactor_members=None):
    from src.features.gating.gating_cog import GatingCog

    newbie_role = SimpleNamespace(id=1534854919562072115)
    speaker_role = SimpleNamespace(id=_SPEAKER_ROLE_ID)
    moderated_role = SimpleNamespace(id=1534854920732151838)
    channel = _HistoryChannel(_INTRO_CHANNEL_ID, messages)
    for message in messages:
        message.channel = channel

    target_member = SimpleNamespace(
        id=_MEMBER_ID,
        roles=list(target_roles if target_roles is not None else [newbie_role]),
    )
    member_map = {_MEMBER_ID: target_member, **(reactor_members or {})}
    role_map = {
        newbie_role.id: newbie_role,
        speaker_role.id: speaker_role,
        moderated_role.id: moderated_role,
    }
    guild = SimpleNamespace(
        id=_GUILD_ID,
        name='BNDC',
        get_channel=MagicMock(return_value=channel),
        get_member=MagicMock(side_effect=lambda member_id: member_map.get(member_id)),
        fetch_member=AsyncMock(side_effect=lambda member_id: member_map[member_id]),
        get_role=MagicMock(side_effect=lambda role_id: role_map.get(role_id)),
    )
    bot = SimpleNamespace(db_handler=None, user=SimpleNamespace(id=42), guilds=[guild])
    cog = GatingCog(bot)
    cog.db = MagicMock()
    cfg = {
        'gate_channel_id': 100,
        'intro_channel_id': _INTRO_CHANNEL_ID,
        'speaker_role_id': _SPEAKER_ROLE_ID,
        'approver_role_id': 1328101710488408207,
        'super_approver_role_id': 1138851070311931946,
        'moderated_role_id': moderated_role.id,
    }
    cog._get_gating_config = MagicMock(return_value=cfg)
    return cog, guild, target_member


def test_reconcile_discovers_intro_with_empty_pending_map_and_is_repeatable(fresh_event_loop):
    message = _top_level_message(7001, _MEMBER_ID)
    message.reactions = []
    cog, _guild, _target = _make_reconcile_cog([message])

    pending = {'row': None}
    cog.db.get_guild_member = MagicMock(return_value={'member_status': 'newbie'})
    cog.db.get_pending_intro_by_member = MagicMock(side_effect=lambda *_args, **_kwargs: pending['row'])

    def create_pending(member_id, message_id, channel_id, guild_id=None):
        pending['row'] = {
            'id': 81,
            'member_id': member_id,
            'message_id': message_id,
            'channel_id': channel_id,
            'guild_id': guild_id,
        }
        return pending['row']

    cog.db.create_pending_intro = MagicMock(side_effect=create_pending)

    fresh_event_loop.run_until_complete(cog.scan_intro_channels())
    fresh_event_loop.run_until_complete(cog.scan_intro_channels())

    cog.db.create_pending_intro.assert_called_once_with(
        _MEMBER_ID, 7001, _INTRO_CHANNEL_ID, guild_id=_GUILD_ID,
    )
    assert cog._pending_messages == {7001: _MEMBER_ID}


def test_reconcile_replays_human_approver_reaction_once(fresh_event_loop):
    approver_role = SimpleNamespace(id=1328101710488408207)
    approver = SimpleNamespace(id=_APPROVER_ID, bot=False, roles=[approver_role])
    bot_reactor = SimpleNamespace(id=42, bot=True, roles=[approver_role])
    ordinary_reactor = SimpleNamespace(id=99, bot=False, roles=[])
    message = _top_level_message(7002, _MEMBER_ID)
    message.reactions = [_HistoryReaction([bot_reactor, ordinary_reactor, approver])]
    cog, _guild, target = _make_reconcile_cog(
        [message], reactor_members={_APPROVER_ID: approver, 99: ordinary_reactor},
    )

    pending = {'row': None}
    status = {'value': 'newbie'}
    cog.db.get_guild_member = MagicMock(
        side_effect=lambda *_args, **_kwargs: {'member_status': status['value']}
    )
    cog.db.get_pending_intro_by_member = MagicMock(side_effect=lambda *_args, **_kwargs: pending['row'])

    def create_pending(member_id, message_id, channel_id, guild_id=None):
        pending['row'] = {
            'id': 82,
            'member_id': member_id,
            'message_id': message_id,
            'channel_id': channel_id,
            'guild_id': guild_id,
        }
        return pending['row']

    cog.db.create_pending_intro = MagicMock(side_effect=create_pending)
    cog.db.record_intro_vote = MagicMock(return_value=True)

    async def approve(_guild, _intro, _cfg, reacted_message_id=None):
        assert reacted_message_id == 7002
        status['value'] = 'speaker'
        pending['row'] = None
        target.roles.append(SimpleNamespace(id=_SPEAKER_ROLE_ID))

    cog._approve_member = AsyncMock(side_effect=approve)

    fresh_event_loop.run_until_complete(cog.scan_intro_channels())
    fresh_event_loop.run_until_complete(cog.scan_intro_channels())

    cog.db.record_intro_vote.assert_called_once_with(
        82, 7002, _APPROVER_ID, 'approver', guild_id=_GUILD_ID,
    )
    cog._approve_member.assert_awaited_once()
    # The bot reaction and ordinary user's reaction were not recorded.
    assert cog.db.record_intro_vote.call_count == 1


@pytest.mark.parametrize('blocked_role_id', [_SPEAKER_ROLE_ID, 1534854920732151838])
def test_reconcile_skips_speaker_and_moderated_members(fresh_event_loop, blocked_role_id):
    blocked_role = SimpleNamespace(id=blocked_role_id)
    message = _top_level_message(7003, _MEMBER_ID)
    message.reactions = []
    cog, _guild, _target = _make_reconcile_cog([message], target_roles=[blocked_role])
    cog.db.get_guild_member = MagicMock(return_value={'member_status': 'newbie'})

    fresh_event_loop.run_until_complete(cog.scan_intro_channels())

    cog.db.create_pending_intro.assert_not_called()
    assert cog._pending_messages == {}


# Suppress unused-import warning for shared scaffolding.
_ = AsyncMock


# =============================================================================
# Approval welcome — temporary tagged post in the getting-started channel
#
# Ported from the codex/fix-intro-auto-moderation branch (cf3bcfb). A newly
# approved Speaker gets "{mention}, you're now a speaker! …" posted beside the
# persistent Getting Started content, tracked in _temp_welcomes and auto-deleted
# after 5 minutes (native delete_after + the cleanup loop).
# =============================================================================


class _FakeWelcomeChannel:
    def __init__(self, history_messages=()):
        self.id = 20
        self._history_messages = list(history_messages)
        self.send = AsyncMock()
        self.fetch_message = AsyncMock()

    async def history(self, *, limit, oldest_first=False):
        assert limit in (50, 100)
        for message in self._history_messages:
            yield message


def _make_approval_cog(channel):
    from src.features.gating.gating_cog import GatingCog

    bot = SimpleNamespace(
        db_handler=None,
        user=SimpleNamespace(id=42),
        guilds=[],
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    cog = GatingCog(bot)
    cog.db = MagicMock()
    return cog


def test_approval_welcome_tags_member_references_content_and_tracks_ttl(fresh_event_loop):
    non_bot_message = SimpleNamespace(author=SimpleNamespace(id=7))
    persistent_welcome = SimpleNamespace(author=SimpleNamespace(id=42))
    channel = _FakeWelcomeChannel([non_bot_message, persistent_welcome])
    sent_at = datetime.now(timezone.utc)
    sent = SimpleNamespace(id=9001, created_at=sent_at)
    channel.send.return_value = sent
    cog = _make_approval_cog(channel)
    guild = SimpleNamespace(get_channel=MagicMock(return_value=channel))
    member = SimpleNamespace(id=77, mention='<@77>')

    fresh_event_loop.run_until_complete(
        cog._send_speaker_welcome(guild, member, {'welcome_channel_id': 20})
    )

    channel.send.assert_awaited_once()
    call = channel.send.await_args
    assert call.args == (
        "<@77>, you're now a speaker! Check out the welcome message above.",
    )
    assert call.kwargs['reference'] is persistent_welcome
    assert call.kwargs['delete_after'] == 300.0
    allowed_mentions = call.kwargs['allowed_mentions']
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is True
    assert allowed_mentions.roles is False
    assert allowed_mentions.replied_user is False
    assert cog._temp_welcomes == {9001: (20, sent_at)}


def test_approval_welcome_is_deleted_after_five_minutes(fresh_event_loop):
    channel = _FakeWelcomeChannel()
    message = SimpleNamespace(delete=AsyncMock())
    channel.fetch_message.return_value = message
    cog = _make_approval_cog(channel)
    cog._startup_scan_done = True
    cog._temp_welcomes[9001] = (
        channel.id,
        datetime.now(timezone.utc) - timedelta(minutes=6),
    )

    fresh_event_loop.run_until_complete(cog.cleanup_temp_welcomes())

    channel.fetch_message.assert_awaited_once_with(9001)
    message.delete.assert_awaited_once_with()
    assert cog._temp_welcomes == {}


def test_startup_cleanup_removes_orphaned_approval_welcome(fresh_event_loop):
    orphan = SimpleNamespace(
        author=SimpleNamespace(id=42),
        content="<@77>, you're now a speaker! Check out the welcome message above.",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=6),
        delete=AsyncMock(),
    )
    channel = _FakeWelcomeChannel([orphan])
    guild = SimpleNamespace(id=1, name='BNDC')
    cog = _make_approval_cog(channel)
    cog.bot.guilds = [guild]
    cog._startup_scan_done = False
    cog._get_guild_config = MagicMock(return_value={
        'gate_channel_id': None,
        'welcome_channel_id': channel.id,
    })

    fresh_event_loop.run_until_complete(cog.cleanup_temp_welcomes())

    orphan.delete.assert_awaited_once_with()
    assert cog._startup_scan_done is True


def test_public_welcome_failure_does_not_block_approval_dm(fresh_event_loop):
    channel = _FakeWelcomeChannel()
    channel.send.side_effect = RuntimeError('discord unavailable')
    cog = _make_approval_cog(channel)

    speaker_role = SimpleNamespace(id=10)
    member = SimpleNamespace(
        id=77,
        mention='<@77>',
        display_name='New Member',
        roles=[],
        add_roles=AsyncMock(),
        send=AsyncMock(),
    )
    guild = SimpleNamespace(
        id=1,
        name='BNDC',
        get_member=MagicMock(return_value=member),
        get_role=MagicMock(return_value=speaker_role),
        get_channel=MagicMock(return_value=channel),
    )
    intro = {
        'member_id': 77,
        'message_id': 123,
        'guild_id': 1,
        'channel_id': 2,
    }

    fresh_event_loop.run_until_complete(
        cog._approve_member(
            guild,
            intro,
            {'speaker_role_id': 10, 'welcome_channel_id': 20},
        )
    )

    member.add_roles.assert_awaited_once_with(
        speaker_role, reason='Intro approved by community'
    )
    cog.db.approve_pending_intro.assert_called_once_with(123, guild_id=1)
    member.send.assert_awaited_once_with(
        "Hey New Member! You've been approved to speak in **BNDC**. Welcome aboard 🎉"
    )


# =============================================================================
# _format_attachments — give the text-only intro reviewer the media details
#
# The reviewer can't play a video or render an image, so we hand it filename,
# type, dimensions, size, and URL instead of a bare boolean. These tests pin
# that formatting so the reviewer can tell the attached media IS the member's
# work (the helium video-intro regression).
# =============================================================================


def _att(filename, *, content_type="video/mp4", size=1024 * 1024,
         width=None, height=None, url="https://cdn.discordapp.com/attachments/1/x"):
    return SimpleNamespace(
        filename=filename, content_type=content_type, size=size,
        width=width, height=height, url=url,
    )


def test_format_attachments_none():
    from src.features.gating.gating_cog import _format_attachments
    assert _format_attachments(SimpleNamespace(attachments=[])) == "none"


def test_format_attachments_video_includes_type_size_and_url():
    from src.features.gating.gating_cog import _format_attachments
    att = _att("movie.mp4", content_type="video/mp4",
               size=int(12.5 * 1024 * 1024),
               url="https://cdn.discordapp.com/attachments/9/movie.mp4")
    out = _format_attachments(SimpleNamespace(attachments=[att]))
    assert "movie.mp4" in out
    assert "video/mp4" in out
    assert "12.5MB" in out
    assert "<https://cdn.discordapp.com/attachments/9/movie.mp4>" in out


def test_format_attachments_image_includes_dimensions():
    from src.features.gating.gating_cog import _format_attachments
    att = _att("shot.png", content_type="image/png", size=204800,
               width=1920, height=1080)
    out = _format_attachments(SimpleNamespace(attachments=[att]))
    assert "shot.png" in out
    assert "image/png" in out
    assert "1920x1080" in out
    assert "0.2MB" in out


def test_format_attachments_caps_at_five():
    from src.features.gating.gating_cog import _format_attachments
    atts = [_att(f"f{i}.mp4", url=f"https://x/{i}") for i in range(7)]
    out = _format_attachments(SimpleNamespace(attachments=atts))
    assert out.count("MB") == 5


# =============================================================================
# Bot-reply review context — the reviewer sees both sides of the conversation
#
# Previously _intro_history only held the member's own messages, so the reviewer
# had no memory of what it already said to them (and could repeat itself). The
# bot's own replies in the intro channel that reference a tracked member message
# are now recorded with is_bot=True so later reviews see what the bot said and
# what the member did after. Deletion paths clear the context so a deleted
# conversation can't leak into a future review.
# =============================================================================


def _bot_reply_message(msg_id, replied_to_id, content, author_id=42):
    return SimpleNamespace(
        id=msg_id,
        guild=SimpleNamespace(id=_GUILD_ID),
        channel=SimpleNamespace(id=_INTRO_CHANNEL_ID),
        author=SimpleNamespace(id=author_id, bot=True),
        content=content,
        reference=SimpleNamespace(message_id=replied_to_id),
    )


def _member_message(msg_id, content):
    return SimpleNamespace(
        id=msg_id,
        author=SimpleNamespace(id=_MEMBER_ID, bot=False),
        content=content,
    )


def test_bot_reply_recorded_as_context_with_role():
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "I train LoRAs with Kohya"))

    cog._record_bot_reply_context(
        _bot_reply_message(9001, 100, "Nice, what models are you training?")
    )

    assert cog._member_context(_MEMBER_ID) == [
        (100, "I train LoRAs with Kohya", False),
        (9001, "Nice, what models are you training?", True),
    ]


def test_other_bot_reply_not_recorded():
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID

    # A different bot replying to the member's message must NOT be attributed
    # as this reviewer's own reply.
    cog._record_bot_reply_context(_bot_reply_message(9004, 100, "hey!", author_id=999))

    assert _MEMBER_ID not in cog._intro_history


def test_bot_reply_without_reference_skipped():
    cog, _channel = _make_cog_for_recovery()
    msg = _bot_reply_message(9002, 100, "welcome!")
    msg.reference = None

    cog._record_bot_reply_context(msg)

    assert _MEMBER_ID not in cog._intro_history


def test_bot_reply_to_untracked_message_skipped():
    cog, _channel = _make_cog_for_recovery()
    # No _pending_messages entry for 100 → the reply can't be attributed.
    cog._record_bot_reply_context(_bot_reply_message(9003, 100, "welcome!"))

    assert _MEMBER_ID not in cog._intro_history


def test_bot_reply_shares_member_context_cap():
    from src.features.gating.gating_cog import INTRO_REVIEW_CONTEXT_MESSAGES

    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    for i in range(INTRO_REVIEW_CONTEXT_MESSAGES):
        mid = 100 + i
        cog._pending_messages[mid] = _MEMBER_ID
        cog._append_member_history(_MEMBER_ID, _member_message(mid, f"msg {i}"))

    # Bot reply counts toward the same window → oldest member msg evicted.
    cog._record_bot_reply_context(_bot_reply_message(9999, 100, "hey"))

    context = cog._member_context(_MEMBER_ID)
    assert len(context) == INTRO_REVIEW_CONTEXT_MESSAGES
    assert context[0][0] == 101  # msg 100 evicted
    assert context[-1] == (9999, "hey", True)


def test_on_message_bot_reply_records_context_without_tracking_intro(fresh_event_loop):
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID

    fresh_event_loop.run_until_complete(
        cog.on_message(_bot_reply_message(9001, 100, "Nice, what models are you training?"))
    )

    # Recorded as context, but never treated as a pending intro.
    assert (9001, "Nice, what models are you training?", True) in cog._member_context(_MEMBER_ID)
    cog.db.get_pending_intro_by_member.assert_not_called()
    cog.db.create_pending_intro.assert_not_called()
    assert 9001 not in cog._pending_messages


def test_review_context_renders_role_labels(fresh_event_loop, monkeypatch):
    from src.features.gating import gating_cog as gc

    cog, _channel = _make_cog_for_recovery()
    cog._get_guild_config = MagicMock(
        return_value={'help_channel_id': 999, 'gate_channel_id': 100}
    )
    cog._pending_messages[100] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "hi there"))
    cog._record_bot_reply_context(_bot_reply_message(9001, 100, "welcome!"))

    captured = {}

    async def fake_llm(**kwargs):
        captured['content'] = kwargs['messages'][0]['content']
        return "KEEP\nwelcome aboard!"

    monkeypatch.setattr(gc, 'get_llm_response', fake_llm)

    message = SimpleNamespace(
        id=100,
        author=SimpleNamespace(id=_MEMBER_ID, display_name="Newbie", bot=False),
        guild=SimpleNamespace(id=_GUILD_ID),
        content="hi there",
        attachments=[],
        reply=AsyncMock(),
    )
    fresh_event_loop.run_until_complete(
        cog._review_intro(message, history=cog._member_context(_MEMBER_ID))
    )

    assert "[member]: hi there" in captured['content']
    assert "[bot]: welcome!" in captured['content']


def test_intro_review_prompt_includes_no_reply_action():
    from src.features.gating.gating_cog import _INTRO_REVIEW_PROMPT

    assert "NO_REPLY" in _INTRO_REVIEW_PROMPT


def test_review_no_reply_posts_nothing_and_keeps_message(fresh_event_loop, monkeypatch):
    from src.features.gating import gating_cog as gc

    cog, _channel = _make_cog_for_recovery()
    cog._get_guild_config = MagicMock(
        return_value={'help_channel_id': 999, 'gate_channel_id': 100}
    )
    cog._pending_messages[100] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "thanks!"))
    cog.db.expire_pending_intro = MagicMock()

    async def fake_llm(**kwargs):
        return "NO_REPLY\n(no note needed)"

    monkeypatch.setattr(gc, 'get_llm_response', fake_llm)

    message = SimpleNamespace(
        id=100,
        author=SimpleNamespace(id=_MEMBER_ID, display_name="Newbie", bot=False),
        guild=SimpleNamespace(id=_GUILD_ID),
        content="thanks!",
        attachments=[],
        reply=AsyncMock(),
        delete=AsyncMock(),
    )
    fresh_event_loop.run_until_complete(cog._review_intro(message))

    # No reply, no deletion, pending intro and review context untouched.
    message.reply.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert cog._pending_messages == {100: _MEMBER_ID}
    assert cog._member_context(_MEMBER_ID) == [(100, "thanks!", False)]
    cog.db.expire_pending_intro.assert_not_called()


@pytest.mark.parametrize("action", ["KEEP", "FEEDBACK"])
def test_intro_review_prompt_mentions_human_review(action):
    from src.features.gating.gating_cog import _INTRO_REVIEW_PROMPT

    # The agent (not the harness) writes the reply; it must be told to mention
    # that a human will review the member's intro.
    assert "human will review their intro" in _INTRO_REVIEW_PROMPT


def test_intro_review_prompt_forbids_claiming_speaker_role():
    from src.features.gating.gating_cog import _INTRO_REVIEW_PROMPT

    # The reviewer reply must not tell a member they are already a Speaker —
    # only a human approver grants the role. This is the regression behind the
    # pinned #support thread ("now you're a Speaker" replies without a role).
    assert "NEVER tell them they are already a Speaker" in _INTRO_REVIEW_PROMPT
    assert "cannot grant roles" in _INTRO_REVIEW_PROMPT

    # The prohibition must be global (any action, any reply), not just the
    # KEEP branch — the "now you're a Speaker" phrasing happened in a KEEP
    # welcome, but feedback replies must not claim the role either.
    assert "NEVER tell a member they are already a Speaker" in _INTRO_REVIEW_PROMPT
    assert "now you're a Speaker" in _INTRO_REVIEW_PROMPT


def test_remove_member_messages_clears_review_context():
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "hi"))
    cog._record_bot_reply_context(_bot_reply_message(9001, 100, "welcome!"))

    cog._remove_member_messages(_MEMBER_ID)

    assert cog._pending_messages == {}
    assert _MEMBER_ID not in cog._intro_history


def _off_topic_message(msg_id):
    """A member message wired for _delete_off_topic (async delete + hint send)."""
    channel = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
    )
    return SimpleNamespace(
        id=msg_id,
        author=SimpleNamespace(id=_MEMBER_ID, bot=False, mention=f"<@{_MEMBER_ID}>"),
        guild=SimpleNamespace(id=_GUILD_ID),
        channel=channel,
        content="spam",
        delete=AsyncMock(),
    )


def test_delete_off_topic_clears_history_when_last_message(fresh_event_loop):
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "hi"))
    cog._record_bot_reply_context(_bot_reply_message(9001, 100, "welcome!"))

    fresh_event_loop.run_until_complete(
        cog._delete_off_topic(_off_topic_message(100), "bye", hint_seconds=0)
    )

    # Last tracked message gone → whole conversation dropped, so a future
    # re-intro can't inherit the deleted exchange.
    assert cog._pending_messages == {}
    assert _MEMBER_ID not in cog._intro_history


def test_delete_off_topic_drops_only_deleted_message(fresh_event_loop):
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    cog._pending_messages[101] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "first"))
    cog._append_member_history(_MEMBER_ID, _member_message(101, "second"))

    fresh_event_loop.run_until_complete(
        cog._delete_off_topic(_off_topic_message(100), "bye", hint_seconds=0)
    )

    assert cog._pending_messages == {101: _MEMBER_ID}
    assert cog._member_context(_MEMBER_ID) == [(101, "second", False)]


def test_on_raw_message_delete_clears_history_when_last_message(fresh_event_loop):
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "hi"))
    cog._record_bot_reply_context(_bot_reply_message(9001, 100, "welcome!"))

    fresh_event_loop.run_until_complete(
        cog.on_raw_message_delete(SimpleNamespace(message_id=100, guild_id=_GUILD_ID))
    )

    assert cog._pending_messages == {}
    assert _MEMBER_ID not in cog._intro_history


def test_on_raw_message_delete_drops_only_deleted_message(fresh_event_loop):
    cog, _channel = _make_cog_for_recovery()
    cog._pending_messages[100] = _MEMBER_ID
    cog._pending_messages[101] = _MEMBER_ID
    cog._append_member_history(_MEMBER_ID, _member_message(100, "first"))
    cog._append_member_history(_MEMBER_ID, _member_message(101, "second"))

    fresh_event_loop.run_until_complete(
        cog.on_raw_message_delete(SimpleNamespace(message_id=100, guild_id=_GUILD_ID))
    )

    assert cog._pending_messages == {101: _MEMBER_ID}
    assert cog._member_context(_MEMBER_ID) == [(101, "second", False)]
