import asyncio
from types import SimpleNamespace

from src.features.admin_chat import agent as admin_agent
from src.features.admin_chat import tools as admin_tools
from src.features.health.health_check_cog import HealthCheckCog


class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = len(data) if count is None else count


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.rows = [row for row in self.rows if row.get(key) == value]
        return self

    def gte(self, key, value):
        self.rows = [row for row in self.rows if str(row.get(key) or "") >= str(value)]
        return self

    def order(self, key, desc=False):
        self.rows = sorted(self.rows, key=lambda row: str(row.get(key) or ""), reverse=desc)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = self.rows[: self._limit] if self._limit is not None else self.rows
        return FakeResult(rows, count=len(self.rows))


class FakeSupabase:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeQuery(self.tables.get(name, []))


class RaisingSupabase(FakeSupabase):
    def __init__(self, tables, broken_tables):
        super().__init__(tables)
        self.broken_tables = set(broken_tables)

    def table(self, name):
        if name in self.broken_tables:
            raise RuntimeError(f'relation "public.{name}" does not exist')
        return super().table(name)


def make_health_cog(fake_supabase):
    cog = HealthCheckCog.__new__(HealthCheckCog)
    cog.db = SimpleNamespace(
        storage_handler=SimpleNamespace(supabase_client=fake_supabase)
    )
    return cog


def test_health_check_alerts_when_live_editor_has_no_recent_runs():
    cog = make_health_cog(FakeSupabase({"topic_editor_runs": []}))

    alerts = cog._check_live_update_editor()

    assert alerts == ["No topic-editor runs recorded in the last 2 hours"]


def test_health_check_alerts_when_latest_live_editor_run_failed():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "failed",
                "started_at": "2999-01-01T00:00:00+00:00",
                "error_message": "publish failed",
            }
        ]
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == ["Latest topic-editor run failed: publish failed"]


def test_health_check_does_not_alert_no_source_data_for_running_startup_run():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "running",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 0,
                "failed_publish_count": 0,
            }
        ],
        "topic_editor_drafts": [],
        "topics": [],
        "topic_transitions": [],
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == []


def test_health_check_alerts_no_source_data_for_completed_run():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 0,
                "failed_publish_count": 0,
            }
        ],
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == ["Latest topic-editor run had no source data"]


def test_health_check_alerts_when_topic_publication_partial():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": "2999-01-01T00:00:00+00:00",
                "failed_publish_count": 0,
            }
        ],
        "topics": [
            {
                "topic_id": "topic-1",
                "headline": "Launch",
                "publication_status": "partial",
                "updated_at": "2999-01-01T00:00:01+00:00",
            }
        ],
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == ["Topic-editor has failed or partial publications: Launch (partial)"]


def test_admin_agent_prompt_and_queryable_tables_label_live_state():
    assert "get_live_update_status" in admin_agent.SYSTEM_PROMPT
    assert "daily_summaries" in admin_agent.SYSTEM_PROMPT
    assert "legacy history only" in admin_agent.SYSTEM_PROMPT
    assert "legacy rollback only" in admin_agent.SYSTEM_PROMPT
    assert "topic_editor_runs" in admin_tools.QUERYABLE_TABLES
    assert "topic_editor_drafts" in admin_tools.QUERYABLE_TABLES
    assert "topic_editor_drafts" in admin_agent.SYSTEM_PROMPT
    assert "topics" in admin_tools.QUERYABLE_TABLES
    assert "topic_transitions" in admin_tools.QUERYABLE_TABLES
    assert "live_update_feed_items" in admin_tools.QUERYABLE_TABLES
    assert "live_update_duplicate_state" in admin_tools.QUERYABLE_TABLES

    daily_tool = next(tool for tool in admin_tools.TOOLS if tool["name"] == "get_daily_summaries")
    live_tool = next(tool for tool in admin_tools.TOOLS if tool["name"] == "get_live_update_status")
    assert "legacy" in daily_tool["description"]
    assert "active topic-editor state" in live_tool["description"]
    assert "null-topic" in live_tool["description"]


def test_get_live_update_status_reads_topic_editor_primary_and_labels_legacy_rollback(monkeypatch):
    fake_supabase = FakeSupabase({
        "topic_editor_runs": [
            {
                "guild_id": 1,
                "run_id": "run-1",
                "status": "completed",
                "trigger": "scheduled",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 3,
                "tool_call_count": 4,
                "accepted_count": 1,
                "rejected_count": 1,
                "override_count": 1,
                "observation_count": 1,
                "published_count": 1,
                "failed_publish_count": 0,
            }
        ],
        "topics": [
            {
                "guild_id": 1,
                "topic_id": "topic-1",
                "state": "posted",
                "headline": "Launch",
                "canonical_key": "launch",
                "publication_status": "sent",
                "discord_message_ids": ["100", "101"],
                "updated_at": "2999-01-01T00:00:03+00:00",
            },
            {
                "guild_id": 1,
                "topic_id": "topic-2",
                "state": "watching",
                "headline": "Beta",
                "canonical_key": "beta",
                "publication_status": "partial",
                "publication_error": "one send failed",
                "updated_at": "2999-01-01T00:00:04+00:00",
            },
        ],
        "topic_transitions": [
            {
                "guild_id": 1,
                "transition_id": "trans-1",
                "run_id": "run-1",
                "topic_id": "topic-1",
                "action": "override",
                "from_state": "watching",
                "to_state": "posted",
                "created_at": "2999-01-01T00:00:02+00:00",
            },
            {
                "guild_id": 1,
                "transition_id": "trans-2",
                "run_id": "run-1",
                "topic_id": None,
                "action": "rejected_watch",
                "reason": "collision",
                "created_at": "2999-01-01T00:00:01+00:00",
            },
        ],
        "topic_editor_drafts": [
            {
                "guild_id": 1,
                "draft_id": "draft-1",
                "run_id": "run-1",
                "topic_id": None,
                "status": "needs_revision",
                "validation_result": {
                    "errors": [{"path": "latest_valid_preview_hash", "message": "missing preview"}],
                    "warnings": [],
                },
                "preview_units": [{"type": "text", "content": "preview"}],
                "publish_result": None,
                "publish_diagnostics": {
                    "reason_codes": ["media_url_expired"],
                    "renderer_safety_chunking_used": True,
                    "media_failures": [{"reason_code": "media_payload_too_large"}],
                },
                "revision_number": 2,
                "revision_hash": "hash-2",
                "revision_attempts": 1,
                "latest_valid_preview_hash": None,
                "updated_at": "2999-01-01T00:00:05+00:00",
            }
        ],
        "editorial_observations": [
            {
                "guild_id": 1,
                "observation_id": "obs-1",
                "run_id": "run-1",
                "observation_kind": "near_miss",
                "reason": "weak",
                "created_at": "2999-01-01T00:00:01+00:00",
            },
        ],
        "live_update_editor_runs": [
            {"guild_id": 1, "run_id": "legacy-run", "status": "completed", "created_at": "2999-01-01T00:00:00+00:00"}
        ],
        "live_update_feed_items": [{"guild_id": 1, "created_at": "2999-01-01T00:00:03+00:00"}],
        "live_update_watchlist": [{"guild_id": 1}],
        "live_update_duplicate_state": [{"guild_id": 1}],
    })

    monkeypatch.setattr(admin_tools, "_get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(admin_tools, "_resolve_guild_id", lambda _params: 1)

    result = asyncio.run(admin_tools.execute_get_live_update_status({"hours": 24, "limit": 5}))

    assert result["success"] is True
    assert result["runs"][0]["run_id"] == "run-1"
    assert result["topics"][0]["headline"] == "Beta"
    assert result["topic_counts"]["posted"] == 1
    assert result["topic_counts"]["watching"] == 1
    assert result["state_counts"]["recent_rejections"] == 1
    assert result["state_counts"]["recent_overrides"] == 1
    assert result["state_counts"]["publication_problems"] == 1
    assert result["state_counts"]["draft_validation_failures"] == 1
    assert result["drafts"][0]["topic_id"] is None
    assert result["draft_summaries"][0]["revision_hash"] == "hash-2"
    assert result["draft_summaries"][0]["preview_text_unit_count"] == 1
    assert "draft_validation_failed" in result["diagnostic_categories"]
    assert "stale_or_missing_preview" in result["diagnostic_categories"]
    assert "media_url_expired" in result["diagnostic_categories"]
    assert "media_payload_too_large" in result["diagnostic_categories"]
    assert "renderer_safety_chunking_used" in result["diagnostic_categories"]
    assert result["override_rate"] == 0.5
    assert "Topic-editor primary status" in result["summary"]
    assert "Failed/partial publications: 1 topics" in result["summary"]
    assert "Recent rejections:" in result["summary"]
    assert "Rollback legacy live-update state only" in result["summary"]
    assert result["legacy_rollback_state"]["runs"][0]["run_id"] == "legacy-run"


def test_get_live_update_status_reports_optional_schema_gaps_without_failing(monkeypatch):
    fake_supabase = RaisingSupabase({
        "topic_editor_runs": [
            {
                "guild_id": 1,
                "run_id": "run-1",
                "status": "running",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 0,
                "failed_publish_count": 0,
            }
        ],
        "topics": [],
        "topic_transitions": [],
        "live_update_editor_runs": [],
        "live_update_feed_items": [],
        "live_update_watchlist": [],
        "live_update_duplicate_state": [],
    }, broken_tables={"topic_editor_drafts"})

    monkeypatch.setattr(admin_tools, "_get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(admin_tools, "_resolve_guild_id", lambda _params: 1)

    result = asyncio.run(admin_tools.execute_get_live_update_status({"hours": 24, "limit": 5}))

    assert result["success"] is True
    assert "topic_editor_drafts" in result["table_errors"]
    assert "missing_or_unreadable_topic_editor_drafts" in result["diagnostic_categories"]
    assert "no_recent_source_data" not in result["diagnostic_categories"]
    assert "Schema/data gaps: topic_editor_drafts" in result["summary"]


def test_health_check_alerts_for_draft_validation_before_publication_problems():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 2,
                "failed_publish_count": 0,
            }
        ],
        "topic_editor_drafts": [
            {
                "draft_id": "draft-1",
                "status": "needs_revision",
                "validation_result": {"errors": [{"message": "card too long"}]},
                "updated_at": "2999-01-01T00:00:01+00:00",
            }
        ],
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == ["Topic-editor draft validation failed: draft-1 — card too long"]


def test_health_check_draft_validation_detail_dedupes_and_caps():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 2,
                "failed_publish_count": 0,
            }
        ],
        "topic_editor_drafts": [
            {
                "draft_id": "draft-dup",
                "status": "needs_revision",
                "validation_result": {
                    "errors": [
                        {"path": "cards[0].body", "message": "marker [5] unmapped"},
                        {"path": "cards[1].body", "message": "marker [5] unmapped"},
                        {"path": "cards[2].body", "message": "marker [6] unmapped"},
                    ]
                },
                "updated_at": "2999-01-01T00:00:01+00:00",
            }
        ],
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == [
        "Topic-editor draft validation failed: draft-dup — marker [5] unmapped; marker [6] unmapped"
    ]


def test_health_check_alerts_for_legacy_and_media_diagnostics_from_transitions():
    cog = make_health_cog(FakeSupabase({
        "topic_editor_runs": [
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": "2999-01-01T00:00:00+00:00",
                "source_message_count": 2,
                "failed_publish_count": 0,
            }
        ],
        "topic_editor_drafts": [],
        "topics": [],
        "topic_transitions": [
            {
                "transition_id": "trans-1",
                "run_id": "run-1",
                "action": "publish_partial",
                "extra": {
                    "publish_diagnostics": {
                        "reason_codes": ["legacy_post_disabled", "media_payload_too_large"]
                    }
                },
                "created_at": "2999-01-01T00:00:01+00:00",
            }
        ],
    }))

    alerts = cog._check_live_update_editor()

    assert alerts == ["Topic-editor legacy direct-post refusal: trans-1"]
