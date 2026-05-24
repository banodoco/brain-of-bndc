from datetime import datetime, timezone
from types import SimpleNamespace

from src.features.logging.logger_cog import LoggerCog


class FakeDB:
    server_config = None

    def __init__(self):
        self.members = []

    def create_or_update_member(self, *args, **kwargs):
        self.members.append((args, kwargs))
        return True


class FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def test_logger_cog_stores_message_author_profile(monkeypatch):
    monkeypatch.setenv("BOT_USER_ID", "999")
    db = FakeDB()
    bot = SimpleNamespace(db_handler=db)
    cog = LoggerCog(bot, FakeLogger())
    role = SimpleNamespace(id=123)
    member = SimpleNamespace(
        display_name="Forum Tester",
        joined_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        roles=[role],
    )
    guild = SimpleNamespace(id=1076117621407223829, get_member=lambda user_id: member)
    author = SimpleNamespace(
        id=863804502633480192,
        name="testerhandle",
        display_name="Fallback Tester",
        global_name="Global Tester",
        avatar=None,
        discriminator="0",
        bot=False,
        system=False,
        accent_color=None,
        banner=None,
        created_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
    )
    message = SimpleNamespace(id=1508112456424231073, author=author, guild=guild)

    cog._store_message_author(message)

    args, kwargs = db.members[0]
    assert args[:4] == (
        863804502633480192,
        "testerhandle",
        "Forum Tester",
        "Global Tester",
    )
    assert kwargs["guild_id"] == 1076117621407223829
    assert args[12] == "[123]"
