"""Regression tests for the grants root-cause fixes:

1. ``_reassess_application`` must apply the same content-sufficiency gate as
   new applications — a title-only post plus chatter must never reach the LLM
   reviewer (it fabricates a project from the title).
2. Applicant messages at stages the handler doesn't process must not vanish:
   a wallet-shaped message gets an explicit stage reply instead of silence.
3. The needs_review post must read as *pending*, not as approval.
"""

from types import SimpleNamespace

import pytest

import src.features.grants.grants_cog as grants_cog_module
from src.features.grants.grants_cog import GrantsCog


pytestmark = pytest.mark.anyio

VALID_WALLET = "FitfJAsxLUBuSgDJJaHgBXJpt1sMm5FzF1Tvf1SHW5Up"
APPLICANT_ID = 222
LONG_WRITEUP = (
    "I built a ComfyUI node pack for X and published it under MIT license. "
    "I need GPU hours to benchmark it across multi-GPU hosts and fix the "
    "queueing bugs I know about. Repo: https://example.com/project."
)


class FakeServerConfig:
    def get_first_server_with_field(self, field, require_write=False):
        return {"guild_id": 1, "grants_channel_id": 100}


class FakeDB:
    def __init__(self):
        self.server_config = FakeServerConfig()
        self.status_updates = []

    def get_grant_by_thread(self, thread_id, guild_id=None):
        return {"thread_id": thread_id, "applicant_id": APPLICANT_ID, "status": "needs_info"}

    def update_grant_status(self, thread_id, status, guild_id=None, **kwargs):
        self.status_updates.append((thread_id, status, kwargs))
        return True

    def get_grant_history_for_applicant(self, applicant_id, guild_id=None):
        return []

    def get_member_engagement(self, applicant_id, guild_id=None):
        return {}


def make_bot():
    return SimpleNamespace(db_handler=FakeDB(), claude_client=object(), payment_service=None)


def make_cog(monkeypatch=None):
    return GrantsCog(make_bot())


class FakeHistoryMsg(SimpleNamespace):
    pass


class FakeThread:
    def __init__(self, history):
        self.id = 200
        self.name = "MageTrail: Proof-of-concept Finetuning on MageFlow 4B"
        self.owner_id = APPLICANT_ID
        self.guild = SimpleNamespace(id=1)
        self._history = history
        self.sent = []

    def history(self, limit=100, oldest_first=False):
        assert oldest_first is True

        async def gen():
            for msg in self._history:
                yield msg

        return gen()


    async def send(self, content):
        self.sent.append(content)

    async def join(self):
        return None

    async def edit(self, **kwargs):
        return None


async def run_reassess(cog, thread, grant_status="needs_info"):
    grant = {"thread_id": thread.id, "applicant_id": APPLICANT_ID, "status": grant_status}
    await cog._reassess_application(thread, grant)


async def test_reassess_gates_title_only_chatter(monkeypatch):
    cog = make_cog()
    thread = FakeThread([
        FakeHistoryMsg(author=SimpleNamespace(bot=True), content="More information needed..."),
        FakeHistoryMsg(author=SimpleNamespace(bot=False), content="wtf this is scuffed af"),
        FakeHistoryMsg(author=SimpleNamespace(bot=False), content="@pom can you fix?"),
    ])

    called = []
    async def fake_assess(*args, **kwargs):
        called.append(kwargs)
        raise AssertionError("reviewer must not run on gated threads")

    monkeypatch.setattr(grants_cog_module, "assess_application", fake_assess)
    await run_reassess(cog, thread)

    assert cog.db.status_updates[-1][1] == "needs_info"
    assert called == []
    assert any("More information needed" in m for m in thread.sent)


async def test_reassess_proceeds_once_writeup_is_sufficient(monkeypatch):
    cog = make_cog()
    thread = FakeThread([
        FakeHistoryMsg(author=SimpleNamespace(bot=True), content="More information needed..."),
        FakeHistoryMsg(author=SimpleNamespace(bot=False), content=LONG_WRITEUP),
    ])

    called = {}

    async def fake_assess(thread_content, **kwargs):
        called["content"] = thread_content
        return {
            "decision": "needs_review",
            "response": "ok",
            "reasoning": "r",
            "gpu_type": None,
            "recommended_hours": None,
        }

    monkeypatch.setattr(grants_cog_module, "assess_application", fake_assess)
    await run_reassess(cog, thread)

    assert called  # reviewer ran
    assert cog.db.status_updates[0][1] == "reviewing"


class FakeDiscordShim:
    """Minimal stand-in so on_message's isinstance(channel, discord.Thread) passes."""

    class Thread:
        pass


def make_message(content, channel_cls, status="needs_review"):
    channel = channel_cls()
    channel.id = 200
    channel.parent_id = 100
    msg = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=APPLICANT_ID),
        guild=SimpleNamespace(id=1),
        channel=channel,
        content=content,
        replies=[],
    )
    async def reply(content, *_a, **_k):
        msg.replies.append(content)
    msg.reply = reply
    return msg


async def test_wallet_outside_awaiting_wallet_gets_stage_reply(monkeypatch):
    cog = make_cog()
    monkeypatch.setattr(grants_cog_module, "discord", FakeDiscordShim)

    def fake_get_grant(thread_id, guild_id=None):
        return {"thread_id": thread_id, "applicant_id": APPLICANT_ID, "status": "needs_review"}
    monkeypatch.setattr(cog.db, "get_grant_by_thread", fake_get_grant)

    msg = make_message(VALID_WALLET, FakeDiscordShim.Thread)
    await cog.on_message(msg)

    assert msg.replies, "applicant must not be left in silence"
    assert "not collecting a wallet address" in msg.replies[0]
    assert "`needs_review`" in msg.replies[0]


async def test_non_wallet_chatter_at_unhandled_stage_stays_silent(monkeypatch):
    cog = make_cog()
    monkeypatch.setattr(grants_cog_module, "discord", FakeDiscordShim)

    def fake_get_grant(thread_id, guild_id=None):
        return {"thread_id": thread_id, "applicant_id": APPLICANT_ID, "status": "needs_review"}
    monkeypatch.setattr(cog.db, "get_grant_by_thread", fake_get_grant)

    msg = make_message("hello?", FakeDiscordShim.Thread)
    await cog.on_message(msg)

    assert msg.replies == []  # no noise on ordinary messages


def make_handle_thread():
    thread = FakeThread([])
    thread.owner_id = APPLICANT_ID
    return thread


async def test_needs_review_post_reads_as_pending_not_approval():
    cog = make_cog()
    thread = make_handle_thread()
    assessment = {
        "decision": "needs_review",
        "response": "Nice idea.",
        "reasoning": "INTERNAL REASONING SHOULD NOT POST",
        "gpu_type": "H100_80GB",
        "recommended_hours": 30,
    }
    await cog._handle_assessment(thread, assessment)

    posted = "\n".join(thread.sent)
    assert "This is not an approval yet" in posted
    assert "(pending human approval)" in posted
    assert "don't send a wallet address" in posted
    assert "if approved" not in posted
    assert "Reasoning:" not in posted  # internal reasoning stays internal
