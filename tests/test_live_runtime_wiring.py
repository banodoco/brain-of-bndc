import asyncio
import logging
from types import SimpleNamespace

from src.features.summarising.summariser_cog import SummarizerCog


class FakeLegacySummarizer:
    def __init__(self):
        self.calls = []

    async def generate_summary(self):
        self.calls.append("generate_summary")


class FakeLiveEditor:
    def __init__(self, events=None):
        self.triggers = []
        self.events = events if events is not None else []
        self.flushed = False

    async def run_once(self, trigger):
        self.triggers.append(trigger)
        self.events.append(f"live:{trigger}")
        return {"status": "completed", "trigger": trigger}

    async def flush_pending_reasoning(self):
        self.flushed = True


class FakeTopCreations:
    def __init__(self):
        self.triggers = []

    async def run_once(self, trigger):
        self.triggers.append(trigger)
        return {"status": "completed", "trigger": trigger}


class FakeServerConfig:
    def __init__(self, guilds=None):
        self.guilds = guilds or [{"guild_id": 1}]
        self.bndc_guild_id = 1

    def get_guilds_to_archive(self):
        return list(self.guilds)


class FakeLoop:
    def __init__(self):
        self.interval_minutes = None
        self.started = False
        self.cancelled = False

    def change_interval(self, *, minutes=None, hours=None):
        if minutes is not None:
            self.interval_minutes = minutes
        elif hours is not None:
            self.interval_minutes = hours * 60

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def is_running(self):
        return self.started and not self.cancelled


class FakeBot:
    def __init__(self, *, summary_now=False, legacy_summary_now=False, archive_days=None, events=None, dev_mode=False):
        self.db_handler = SimpleNamespace(server_config=FakeServerConfig())
        self.server_config = self.db_handler.server_config
        self.logger = logging.getLogger("test-live-runtime")
        self.dev_mode = dev_mode
        self.summary_now = summary_now
        self.legacy_summary_now = legacy_summary_now
        self.archive_days = archive_days
        self.summary_completed = asyncio.Event()
        if not summary_now and not legacy_summary_now:
            self.summary_completed.set()
        self.events = events if events is not None else []
        self.claude_client = SimpleNamespace()

    async def wait_until_ready(self):
        self.events.append("ready")


class FakeCtx:
    def __init__(self):
        self.author = SimpleNamespace(name="owner")
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def make_cog(bot, legacy=None, live_editor=None, top_creations=None):
    return SummarizerCog(
        bot,
        legacy or FakeLegacySummarizer(),
        live_update_editor=live_editor or FakeLiveEditor(),
        live_top_creations=top_creations or FakeTopCreations(),
        start_loops=False,
    )


def test_default_runtime_backend_constructs_topic_editor(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATE_EDITOR_BACKEND", raising=False)
    constructed = {}

    class FakeTopicEditor:
        def __init__(self, *, bot, db_handler, environment):
            constructed["bot"] = bot
            constructed["db_handler"] = db_handler
            constructed["environment"] = environment

        async def run_once(self, trigger):
            return {"status": "completed", "trigger": trigger}

    import src.features.summarising.summariser_cog as summariser_cog_module

    monkeypatch.setattr(summariser_cog_module, "TopicEditor", FakeTopicEditor)
    bot = FakeBot(summary_now=False, dev_mode=False)

    cog = SummarizerCog(
        bot,
        FakeLegacySummarizer(),
        live_top_creations=FakeTopCreations(),
        start_loops=False,
    )

    assert isinstance(cog.live_update_editor, FakeTopicEditor)
    assert constructed == {
        "bot": bot,
        "db_handler": bot.db_handler,
        "environment": "prod",
    }


def test_legacy_backend_selector_constructs_legacy_live_update_editor(monkeypatch):
    monkeypatch.setenv("LIVE_UPDATE_EDITOR_BACKEND", "legacy")
    constructed = {}

    class FakeLegacyEditor:
        def __init__(self, db_handler, *, bot, logger_instance, dry_run_lookback_hours, environment):
            constructed["db_handler"] = db_handler
            constructed["bot"] = bot
            constructed["logger_instance"] = logger_instance
            constructed["dry_run_lookback_hours"] = dry_run_lookback_hours
            constructed["environment"] = environment

        async def run_once(self, trigger):
            return {"status": "completed", "trigger": trigger}

    import src.features.summarising.summariser_cog as summariser_cog_module

    monkeypatch.setattr(summariser_cog_module, "LegacyLiveUpdateEditor", FakeLegacyEditor)
    bot = FakeBot(summary_now=False, dev_mode=True)

    cog = SummarizerCog(
        bot,
        FakeLegacySummarizer(),
        live_top_creations=FakeTopCreations(),
        start_loops=False,
    )

    assert isinstance(cog.live_update_editor, FakeLegacyEditor)
    assert constructed["db_handler"] is bot.db_handler
    assert constructed["bot"] is bot
    assert constructed["logger_instance"] is bot.logger
    assert constructed["dry_run_lookback_hours"] == 6
    assert constructed["environment"] == "dev"


def test_constructor_injected_editor_overrides_backend_selector(monkeypatch):
    monkeypatch.setenv("LIVE_UPDATE_EDITOR_BACKEND", "legacy")
    injected = FakeLiveEditor()
    bot = FakeBot(summary_now=False)

    cog = make_cog(bot, live_editor=injected)

    assert cog.live_update_editor is injected


def test_summarynow_runs_live_editor_not_legacy_daily_summary():
    async def run():
        bot = FakeBot(summary_now=False)
        legacy = FakeLegacySummarizer()
        live_editor = FakeLiveEditor()
        cog = make_cog(bot, legacy=legacy, live_editor=live_editor)
        ctx = FakeCtx()

        await cog.summary_now_command.callback(cog, ctx)

        assert live_editor.triggers == ["owner_summarynow"]
        assert legacy.calls == []
        assert ctx.messages == [
            "Starting live-update editor pass...",
            "Live-update editor pass complete: completed.",
        ]

    asyncio.run(run())


def test_startup_summary_now_runs_archive_before_live_editor(monkeypatch):
    async def run():
        events = []

        class FakeArchiveRunner:
            async def run_archive(self, days, dev_mode, in_depth=False, guild_id=None):
                events.append(f"archive:{days}:{guild_id}:{in_depth}")
                return True

        import src.common.archive_runner as archive_runner_module

        monkeypatch.setattr(archive_runner_module, "ArchiveRunner", FakeArchiveRunner)
        bot = FakeBot(summary_now=True, archive_days=2, events=events)
        bot.server_config = FakeServerConfig(guilds=[{"guild_id": 10}, {"guild_id": 20}])
        bot.db_handler.server_config = bot.server_config
        live_editor = FakeLiveEditor(events)
        cog = make_cog(bot, live_editor=live_editor)

        await cog.on_ready()

        assert events == [
            "archive:2:10:True",
            "archive:2:20:True",
            "live:startup_summary_now",
        ]
        assert live_editor.triggers == ["startup_summary_now"]
        assert bot.summary_completed.is_set()

    asyncio.run(run())


def test_startup_without_summary_now_releases_live_gate_without_running_editor():
    async def run():
        bot = FakeBot(summary_now=False)
        bot.summary_completed.clear()
        live_editor = FakeLiveEditor()
        cog = make_cog(bot, live_editor=live_editor)

        await cog.on_ready()

        assert live_editor.triggers == []
        assert bot.summary_completed.is_set()

    asyncio.run(run())


def test_legacy_summary_command_is_explicit_backfill_only():
    async def run():
        bot = FakeBot(summary_now=False)
        legacy = FakeLegacySummarizer()
        live_editor = FakeLiveEditor()
        cog = make_cog(bot, legacy=legacy, live_editor=live_editor)
        ctx = FakeCtx()

        await cog.legacy_summary_now_command.callback(cog, ctx)

        assert legacy.calls == ["generate_summary"]
        assert live_editor.triggers == []
        assert ctx.messages == [
            "Starting legacy daily-summary generation...",
            "Legacy daily-summary generation complete.",
        ]

    asyncio.run(run())


def test_legacy_startup_flag_runs_legacy_backfill_not_live_editor():
    async def run():
        bot = FakeBot(summary_now=False, legacy_summary_now=True)
        legacy = FakeLegacySummarizer()
        live_editor = FakeLiveEditor()
        cog = make_cog(bot, legacy=legacy, live_editor=live_editor)

        await cog.on_ready()

        assert legacy.calls == ["generate_summary"]
        assert live_editor.triggers == []
        assert bot.summary_completed.is_set()

    asyncio.run(run())


def test_production_live_loops_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_TOP_CREATIONS_ENABLED", raising=False)
    bot = FakeBot(summary_now=False, dev_mode=False)
    cog = SummarizerCog(
        bot,
        FakeLegacySummarizer(),
        live_update_editor=FakeLiveEditor(),
        live_top_creations=FakeTopCreations(),
        start_loops=False,
    )
    cog.run_live_pass = FakeLoop()
    cog.__init__(
        bot,
        FakeLegacySummarizer(),
        live_update_editor=FakeLiveEditor(),
        live_top_creations=FakeTopCreations(),
        start_loops=True,
    )

    assert cog.live_updates_enabled is False
    assert cog.live_top_creations_enabled is False
    assert cog.run_live_pass.started is False


def test_dev_live_loops_run_hourly_without_env(monkeypatch):
    monkeypatch.delenv("LIVE_UPDATES_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_TOP_CREATIONS_ENABLED", raising=False)
    bot = FakeBot(summary_now=False, dev_mode=True)
    cog = SummarizerCog(
        bot,
        FakeLegacySummarizer(),
        live_update_editor=FakeLiveEditor(),
        live_top_creations=FakeTopCreations(),
        start_loops=False,
    )
    cog.run_live_pass = FakeLoop()
    cog.__init__(
        bot,
        FakeLegacySummarizer(),
        live_update_editor=FakeLiveEditor(),
        live_top_creations=FakeTopCreations(),
        start_loops=True,
    )

    assert cog.live_updates_enabled is True
    assert cog.live_top_creations_enabled is False
    assert cog.run_live_pass.started is True
    assert cog.run_live_pass.interval_minutes == 60
