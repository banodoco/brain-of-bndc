from src.common.base_bot import _current_commit_sha
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
