import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.summarising.topic_editor import TopicEditor, TOPIC_EDITOR_SYSTEM_PROMPT
from src.features.summarising.topic_editor_agentic import (
    ScenarioError,
    TopicEditorReplayDB,
    assess_topic_editor_pack,
    load_topic_editor_scenario,
    replay_topic_editor_scenario,
    summarize_topic_editor_agentic_runs,
)


SCENARIO = Path("tests/agentic/scenarios/t2_relight_lora_positive.yaml")


class CaptureMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=0, output_tokens=0))


class CaptureClient:
    def __init__(self):
        self.messages = CaptureMessages()


def test_load_topic_editor_scenario_validates_paths():
    scenario = load_topic_editor_scenario(SCENARIO)
    assert scenario.name == "t2_relight_lora_positive"
    assert scenario.brief_text.strip()
    assert scenario.fixture_dir.exists()


def test_all_starter_scenarios_have_distinct_fixture_dirs():
    scenario_paths = sorted(Path("tests/agentic/scenarios").glob("*.yaml"))
    scenarios = [load_topic_editor_scenario(path) for path in scenario_paths]
    assert {scenario.name for scenario in scenarios} == {
        "gemini_omni_megathread_negative",
        "stale_media_recovery",
        "t2_relight_lora_positive",
    }
    assert len({scenario.fixture_dir for scenario in scenarios}) == len(scenarios)


def test_load_topic_editor_scenario_rejects_bad_window(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    scenario = tmp_path / "bad.yaml"
    scenario.write_text(
        """
name: bad
brief: brief.md
target_orchestrator: topic_editor_replay
priming:
  window:
    guild_id: 1
    start: "2026-05-19T18:10:00Z"
    end: "2026-05-19T17:00:00Z"
  fixtures:
    window: missing
assessment: {}
"""
    )
    with pytest.raises(ScenarioError, match="start must be before"):
        load_topic_editor_scenario(scenario, allow_missing_fixture=True)


def test_actor_brief_is_replay_only_in_system_prompt():
    client = CaptureClient()
    editor = TopicEditor(db_handler=object(), llm_client=client, actor_brief="Use the scenario.")
    asyncio.run(editor._invoke_anthropic([]))
    assert "Use the scenario." in client.messages.calls[0]["system"]

    client = CaptureClient()
    editor = TopicEditor(db_handler=object(), llm_client=client)
    asyncio.run(editor._invoke_anthropic([]))
    assert client.messages.calls[0]["system"] == TOPIC_EDITOR_SYSTEM_PROMPT


def test_replay_writes_pack_and_assessment_passes(tmp_path):
    out = tmp_path / "pack"
    summary = asyncio.run(replay_topic_editor_scenario(SCENARIO, out, mock_actor=True))
    assert summary["status"] == "completed"
    for name in (
        "scenario.yaml",
        "brief.md",
        "tool_trace.json",
        "draft.json",
        "validation.json",
        "preview.json",
        "final_action.json",
        "simulated_db_writes.json",
        "summary.json",
    ):
        assert (out / name).exists()

    assessment = assess_topic_editor_pack(out)
    assert assessment["status"] == "pass"
    assert json.loads((out / "summary.json").read_text())["brief_sha256"]


def test_replay_db_supports_media_understanding_hash_lookup():
    scenario = load_topic_editor_scenario(SCENARIO)
    db = TopicEditorReplayDB(scenario)
    db.media_metadata.append({
        "content_hash": "hash-1",
        "model": "gpt-4o-mini",
        "media_kind": "image",
        "understanding": {"subject": "cached"},
    })
    assert db.get_message_media_understanding_by_hash("hash-1", model="gpt-4o-mini")["understanding"]["subject"] == "cached"


def test_assessor_catches_prod_write_attempt(tmp_path):
    out = tmp_path / "pack"
    asyncio.run(replay_topic_editor_scenario(SCENARIO, out, mock_actor=True))
    writes = json.loads((out / "simulated_db_writes.json").read_text())
    writes["safety_events"].append({
        "target_type": "db",
        "operation": "unsafe",
        "captured_only": False,
        "would_have_touched_prod": True,
    })
    (out / "simulated_db_writes.json").write_text(json.dumps(writes))
    assessment = assess_topic_editor_pack(out)
    assert assessment["status"] == "fail"
    assert "prod_write_attempt" in assessment["failure_buckets"]


def test_summarizer_groups_failure_buckets(tmp_path):
    pack = tmp_path / "run" / "sample"
    pack.mkdir(parents=True)
    (pack / "summary.json").write_text(json.dumps({"scenario": "s"}))
    (pack / "assessment.json").write_text(json.dumps({
        "status": "fail",
        "failure_buckets": {"missing_citations": ["x"]},
    }))
    result = summarize_topic_editor_agentic_runs(tmp_path / "run")
    assert result["bucket_counts"] == {"missing_citations": 1}
    assert (tmp_path / "run" / "summary.md").exists()
