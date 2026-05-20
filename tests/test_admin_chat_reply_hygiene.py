from types import SimpleNamespace

from src.common.base_bot import _claim_startup_notification, _current_commit_sha
from src.features.admin_chat.admin_chat_cog import AdminChatCog


def test_strip_fallback_reply_lines_keeps_real_answer():
    content = "\n".join([
        "You restarted me twice in four minutes.",
        "I hit an internal error while trying to do that.",
        "Pom. You're back. Looks stable now.",
    ])

    assert AdminChatCog._strip_fallback_reply_lines(content) == "\n".join([
        "You restarted me twice in four minutes.",
        "Pom. You're back. Looks stable now.",
    ])


def test_current_commit_sha_prefers_available_env(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_SHA", "abcdef123456")

    assert _current_commit_sha() == "abcdef1"


def test_admin_chat_claim_uses_shared_event_lock(monkeypatch):
    calls = []

    class FakeDB:
        def try_claim_bot_event(self, **kwargs):
            calls.append(kwargs)
            return False

    bot = SimpleNamespace(payment_service=None)
    cog = AdminChatCog(bot, FakeDB(), sharer=object())
    message = SimpleNamespace(
        id=123,
        channel=SimpleNamespace(id=456),
        author=SimpleNamespace(id=789),
    )

    assert cog._claim_admin_chat_message(message) is False
    assert calls[0]["event_key"] == "admin_chat_message:123"
    assert calls[0]["event_type"] == "admin_chat_message"


def test_startup_notification_claim_uses_cooldown_bucket(monkeypatch):
    calls = []

    class FakeDB:
        def try_claim_bot_event(self, **kwargs):
            calls.append(kwargs)
            return False

    monkeypatch.setattr("src.common.base_bot.time.time", lambda: 1234)
    bot = SimpleNamespace(db_handler=FakeDB())

    assert _claim_startup_notification(bot, 42, "abcdef1") is False
    assert calls[0]["event_key"] == "startup_admin_dm:42:2"
    assert calls[0]["event_type"] == "startup_admin_dm"
