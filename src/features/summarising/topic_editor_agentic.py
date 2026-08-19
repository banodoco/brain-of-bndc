"""Local replay harness primitives for topic-editor agentic tests."""

from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

from src.features.summarising.topic_editor import (
    TopicEditorDraftLimits,
    TopicEditor,
    _canonical_jsonable,
    preview_topic_editor_draft,
    render_draft_publish_units,
    resolve_topic_editor_evidence_shelf,
    validate_topic_editor_draft,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTIC_ROOT = REPO_ROOT / "tests" / "agentic"


class ScenarioError(ValueError):
    """Raised when a replay scenario cannot be loaded safely."""


@dataclass(frozen=True)
class TopicEditorScenario:
    path: Path
    raw: Dict[str, Any]
    brief_path: Path
    brief_text: str
    fixture_dir: Path

    @property
    def name(self) -> str:
        return str(self.raw["name"])

    @property
    def guild_id(self) -> int:
        return int(self.raw["priming"]["window"]["guild_id"])

    @property
    def start(self) -> str:
        return str(self.raw["priming"]["window"]["start"])

    @property
    def end(self) -> str:
        return str(self.raw["priming"]["window"]["end"])


def _parse_time(value: Any, field_name: str) -> datetime:
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except Exception as exc:
        raise ScenarioError(f"invalid {field_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require(mapping: Dict[str, Any], dotted_key: str) -> Any:
    value: Any = mapping
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ScenarioError(f"scenario missing required field: {dotted_key}")
        value = value[part]
    if value in (None, ""):
        raise ScenarioError(f"scenario field is empty: {dotted_key}")
    return value


def load_topic_editor_scenario(path: str | Path, *, allow_missing_fixture: bool = False) -> TopicEditorScenario:
    scenario_path = Path(path).resolve()
    raw = yaml.safe_load(scenario_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ScenarioError("scenario must be a YAML object")

    for key in (
        "name",
        "brief",
        "target_orchestrator",
        "priming.window.guild_id",
        "priming.window.start",
        "priming.window.end",
        "priming.fixtures",
        "assessment",
    ):
        _require(raw, key)
    if raw["target_orchestrator"] != "topic_editor_replay":
        raise ScenarioError("target_orchestrator must be topic_editor_replay")

    start = _parse_time(raw["priming"]["window"]["start"], "priming.window.start")
    end = _parse_time(raw["priming"]["window"]["end"], "priming.window.end")
    if start >= end:
        raise ScenarioError("priming.window.start must be before priming.window.end")

    brief_path = (scenario_path.parent / str(raw["brief"])).resolve()
    if not brief_path.exists():
        raise ScenarioError(f"brief file does not exist: {brief_path}")

    fixtures = raw["priming"]["fixtures"] or {}
    if fixtures.get("window_path"):
        fixture_dir = Path(str(fixtures["window_path"])).expanduser().resolve()
    else:
        fixture_name = fixtures.get("window") or raw["name"]
        fixture_dir = (AGENTIC_ROOT / "fixtures" / "windows" / str(fixture_name)).resolve()
    if not fixture_dir.exists() and not allow_missing_fixture:
        raise ScenarioError(f"fixture directory does not exist: {fixture_dir}")

    return TopicEditorScenario(
        path=scenario_path,
        raw=raw,
        brief_path=brief_path,
        brief_text=brief_path.read_text(),
        fixture_dir=fixture_dir,
    )


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_canonical_jsonable(value), indent=2, sort_keys=True, default=str) + "\n")


def _atomic_copy(src: Path, dest: Path) -> None:
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)


class TopicEditorReplayDB:
    """Explicit fake DB for safe topic-editor replay.

    It implements the surfaces used by ``TopicEditor.run_once`` and records
    write-like calls instead of mutating Supabase.
    """

    def __init__(self, scenario: TopicEditorScenario):
        self.scenario = scenario
        self.fixture_dir = scenario.fixture_dir
        self.source_messages = _read_json(self.fixture_dir / "source_messages.json", [])
        self.active_topics = _read_json(self.fixture_dir / "active_topics.json", [])
        self.topic_sources = _read_json(self.fixture_dir / "topic_sources.json", [])
        self.recent_transitions = _read_json(self.fixture_dir / "recent_transitions.json", [])
        self.media_metadata = _read_json(self.fixture_dir / "media_metadata.json", [])
        self.server_config = _read_json(self.fixture_dir / "server_config_snapshot.json", {})
        self.drafts: List[Dict[str, Any]] = []
        self.transitions: List[Dict[str, Any]] = []
        self.checkpoints: List[Dict[str, Any]] = []
        self.completed_runs: List[Dict[str, Any]] = []
        self.failed_runs: List[Dict[str, Any]] = []
        self.observations: List[Dict[str, Any]] = []
        self.topics: List[Dict[str, Any]] = []
        self.sources: List[Dict[str, Any]] = []
        self.aliases: List[Dict[str, Any]] = []
        self.media_understandings: List[Dict[str, Any]] = []
        self.external_media_cache: List[Dict[str, Any]] = []
        self.safety_events: List[Dict[str, Any]] = []
        self.read_calls: List[Dict[str, Any]] = []

    def _record_write(self, target_type: str, operation: str, payload: Any, *, captured_only: bool = True) -> None:
        self.safety_events.append({
            "target_type": target_type,
            "operation": operation,
            "allowed": True,
            "captured_only": captured_only,
            "would_have_touched_prod": False,
            "payload": payload,
        })

    def get_topic_editor_checkpoint(self, checkpoint_key: str, environment: str = "prod") -> Dict[str, Any]:
        return {
            "checkpoint_key": checkpoint_key,
            "guild_id": self.scenario.guild_id,
            "channel_id": int((self.source_messages[0] or {}).get("channel_id") or 0) if self.source_messages else 0,
            "last_message_id": 0,
            "last_message_created_at": self.scenario.start,
        }

    def acquire_topic_editor_run(self, run: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        run_id = f"agentic-{uuid.uuid4().hex[:12]}"
        self._record_write("db", "acquire_topic_editor_run", {"run_id": run_id, **run})
        return {"run_id": run_id, "status": "running"}

    def get_archived_messages_after_checkpoint(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.read_calls.append({"method": "get_archived_messages_after_checkpoint", "kwargs": kwargs})
        return list(self.source_messages)

    def get_topics(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.read_calls.append({"method": "get_topics", "kwargs": kwargs})
        return list(self.active_topics)

    def get_topic_aliases(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def search_topic_editor_topics(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        needle = str(query or "").lower()
        return [
            topic for topic in self.active_topics
            if needle in " ".join(str(topic.get(k, "")) for k in ("headline", "canonical_key", "summary")).lower()
        ][: int(kwargs.get("limit") or 10)]

    def search_messages_unified(self, **kwargs: Any) -> Dict[str, Any]:
        query = str(kwargs.get("query") or "").lower()
        rows = self.source_messages
        if query:
            rows = [row for row in rows if query in str(row.get("content") or "").lower()]
        return {"messages": rows[: int(kwargs.get("limit") or 20)], "truncated": False}

    def get_topic_editor_author_profile(self, author_id: Any, **kwargs: Any) -> Dict[str, Any]:
        for row in self.source_messages:
            if str(row.get("author_id")) == str(author_id):
                return row.get("author_context_snapshot") or {"author_id": author_id}
        return {"author_id": author_id}

    def get_topic_editor_message_context(self, message_ids: Sequence[Any], **kwargs: Any) -> List[Dict[str, Any]]:
        wanted = {str(mid) for mid in message_ids or []}
        return [row for row in self.source_messages if str(row.get("message_id")) in wanted]

    def get_topic_editor_source_messages(self, message_ids: Sequence[Any], **kwargs: Any) -> List[Dict[str, Any]]:
        return self.get_topic_editor_message_context(message_ids, **kwargs)

    def get_reply_chain(self, message_id: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def get_message_media_understanding(self, message_id: Any, attachment_index: int, model: str) -> Optional[Dict[str, Any]]:
        for row in self.media_metadata:
            if str(row.get("message_id")) == str(message_id) and int(row.get("attachment_index") or 0) == int(attachment_index):
                return {"understanding": row.get("understanding") or row}
        return None

    def get_message_media_understanding_by_hash(
        self,
        content_hash: str,
        model: Optional[str] = None,
        media_kind: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        for row in self.media_metadata:
            if content_hash and row.get("content_hash") != content_hash:
                continue
            if model is not None and row.get("model") != model:
                continue
            if media_kind is not None and row.get("media_kind") != media_kind:
                continue
            return {"understanding": row.get("understanding") or row, **row}
        return None

    def upsert_message_media_understanding(self, payload: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        self.media_understandings.append(payload)
        self._record_write("media_cache", "upsert_message_media_understanding", payload)
        return payload

    def get_external_media_cache(self, cache_key: str, environment: str = "prod") -> Optional[Dict[str, Any]]:
        for row in self.external_media_cache:
            if row.get("cache_key") == cache_key:
                return row
        return None

    def upsert_external_media_cache(self, payload: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        self.external_media_cache.append(payload)
        self._record_write("external_media_cache", "upsert_external_media_cache", payload)
        return payload

    def create_topic_editor_draft(self, draft: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        row = {**draft, "environment": environment}
        self.drafts = [existing for existing in self.drafts if existing.get("draft_id") != row.get("draft_id")]
        self.drafts.append(row)
        self._record_write("db", "create_topic_editor_draft", row)
        return row

    def update_topic_editor_draft(self, draft_id: str, updates: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        row = {"draft_id": draft_id, **updates, "environment": kwargs.get("environment", "prod")}
        for index, existing in enumerate(self.drafts):
            if str(existing.get("draft_id")) == str(draft_id):
                row = {**existing, **row}
                self.drafts[index] = row
                break
        else:
            self.drafts.append(row)
        self._record_write("db", "update_topic_editor_draft", row)
        return row

    def get_recent_topic_editor_drafts(self, **kwargs: Any) -> List[Dict[str, Any]]:
        run_id = kwargs.get("run_id")
        rows = self.drafts
        if run_id:
            rows = [row for row in rows if str(row.get("run_id")) == str(run_id)]
        return rows[: int(kwargs.get("limit") or 20)]

    def upsert_topic(self, topic: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        row = {"topic_id": topic.get("topic_id") or f"topic-{len(self.topics) + 1}", **topic}
        self.topics.append(row)
        self._record_write("db", "upsert_topic", row)
        return row

    def update_topic(self, topic_id: str, updates: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        row = {"topic_id": topic_id, **updates}
        self.topics.append(row)
        self._record_write("db", "update_topic", row)
        return row

    def add_topic_source(self, source: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        row = {"topic_source_id": f"source-{len(self.sources) + 1}", **source}
        self.sources.append(row)
        self._record_write("db", "add_topic_source", row)
        return row

    def upsert_topic_alias(self, alias: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        row = {"alias_id": f"alias-{len(self.aliases) + 1}", **alias}
        self.aliases.append(row)
        self._record_write("db", "upsert_topic_alias", row)
        return row

    def store_topic_transition(self, transition: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        row = {"transition_id": f"transition-{len(self.transitions) + 1}", "environment": environment, **transition}
        self.transitions.append(row)
        self._record_write("db", "store_topic_transition", row)
        return row

    def get_topic_transitions_by_tool_call_ids(self, run_id: str, tool_call_ids: Sequence[Any], environment: str = "prod") -> Dict[str, Dict[str, Any]]:
        return {}

    def store_editorial_observation(self, observation: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        self.observations.append(observation)
        self._record_write("db", "store_editorial_observation", observation)
        return observation

    def upsert_topic_editor_checkpoint(self, checkpoint: Dict[str, Any], environment: str = "prod") -> Dict[str, Any]:
        self.checkpoints.append(checkpoint)
        self._record_write("db", "upsert_topic_editor_checkpoint", checkpoint)
        return checkpoint

    def complete_topic_editor_run(self, run_id: str, updates: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        row = {"run_id": run_id, "updates": updates, "kwargs": kwargs}
        self.completed_runs.append(row)
        self._record_write("db", "complete_topic_editor_run", row)
        return {"run_id": run_id, "status": "completed"}

    def fail_topic_editor_run(self, run_id: str, error_message: str, updates: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        row = {"run_id": run_id, "error_message": error_message, "updates": updates, "kwargs": kwargs}
        self.failed_runs.append(row)
        self._record_write("db", "fail_topic_editor_run", row)
        return {"run_id": run_id, "status": "failed"}

    def simulated_writes(self) -> Dict[str, Any]:
        return {
            "safety_events": self.safety_events,
            "drafts": self.drafts,
            "transitions": self.transitions,
            "topics": self.topics,
            "sources": self.sources,
            "aliases": self.aliases,
            "observations": self.observations,
            "checkpoints": self.checkpoints,
            "completed_runs": self.completed_runs,
            "failed_runs": self.failed_runs,
            "media_understandings": self.media_understandings,
            "external_media_cache": self.external_media_cache,
        }


class ReplayDiscordClient:
    def __init__(self) -> None:
        self.sends: List[Dict[str, Any]] = []

    def get_channel(self, channel_id: Any) -> Any:
        recorder = self

        class Channel:
            async def send(self, content: Any = None, **kwargs: Any) -> None:
                recorder.sends.append({
                    "target_type": "discord",
                    "operation": "send",
                    "allowed": True,
                    "captured_only": True,
                    "would_have_touched_prod": False,
                    "channel_id": channel_id,
                    "content": content,
                    "kwargs": kwargs,
                })

        return Channel()


class MockTopicEditorActor:
    """Anthropic-shaped actor that performs the draft workflow once."""

    def __init__(self, *, mode: str = "submit") -> None:
        self.mode = mode
        self.client = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.mode == "watch":
            content = [
                _tool("tool-watch", "watch_topic", {
                    "proposed_key": "watch-replay-window",
                    "headline": "Watch replay window",
                    "why_interesting": "The window has early signs but not enough publishable proof yet.",
                    "revisit_when": "after more source material appears",
                    "source_message_ids": ["100"],
                }),
                _tool("tool-finalize", "finalize_run", {
                    "overall_reasoning": "The replay window did not clear the publish threshold, so I watched the item and left publishing suppressed.",
                    "topics_considered": ["watch replay window"],
                }),
            ]
        elif len(self.calls) == 1:
            source_id, media_id = self._first_source(kwargs.get("messages") or [])
            content = [
                _tool("tool-create", "create_draft", {
                    "topic_key": "t2-relight-lora",
                    "template": "creation_release",
                    "headline": "T2 relight LoRA gets a concise replay test",
                    "dek": "A source-backed local replay packages the update without turning it into a digest.",
                    "cards": [
                        {
                            "angle": "What changed",
                            "body": "The window shows a focused update with a concrete source-backed result worth publishing [1].",
                            "source_message_ids": [source_id],
                            "media_ids": [media_id] if media_id else [],
                        },
                        {
                            "angle": "Why it matters",
                            "body": "The useful signal is the workflow result, not the whole surrounding chat, so the post stays short and attached to the relevant media [1].",
                            "source_message_ids": [source_id],
                            "media_ids": [],
                        },
                    ],
                    "editor_note": "Mock actor for deterministic replay smoke tests.",
                }),
            ]
        else:
            draft_id = self._latest_draft_id(kwargs.get("messages") or []) or "missing-draft-id"
            content = [
                _tool("tool-validate", "validate_draft", {"draft_id": draft_id}),
                _tool("tool-preview", "preview_draft", {"draft_id": draft_id}),
                _tool("tool-submit", "submit_draft", {"draft_id": draft_id}),
                _tool("tool-finalize", "finalize_run", {
                    "overall_reasoning": "I selected the strongest source-backed item, drafted two concise cards, validated and previewed them, and submitted with publishing disabled in replay.",
                    "topics_considered": ["T2 relight LoRA"],
                }),
            ]
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=100, output_tokens=100))

    @staticmethod
    def _latest_draft_id(messages: Sequence[Dict[str, Any]]) -> Optional[str]:
        text = json.dumps(messages, default=str)
        matches = re.findall(r"draft_id=([A-Za-z0-9_.:-]+)", text)
        return matches[-1] if matches else None

    @staticmethod
    def _first_source(messages: Sequence[Dict[str, Any]]) -> tuple[str, Optional[str]]:
        try:
            first_content = (messages[0].get("content") or [])[0].get("text") or "{}"
            payload = ast.literal_eval(first_content)
            source = (payload.get("source_messages") or [{}])[0]
            source_id = str(source.get("message_id") or "100")
            media = source.get("media_refs_available") or []
            if media:
                first_media = media[0]
                media_id = f"{source_id}:{first_media.get('kind') or 'attachment'}:{first_media.get('index') or 0}"
            else:
                media_id = None
            return source_id, media_id
        except Exception:
            return "100", "100:attachment:0"


def _tool(tool_id: str, name: str, payload: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=payload)


def _latest_draft(db: TopicEditorReplayDB) -> Optional[Dict[str, Any]]:
    if not db.drafts:
        return None
    return db.drafts[-1]


def _latest_completion(db: TopicEditorReplayDB) -> Dict[str, Any]:
    return db.completed_runs[-1] if db.completed_runs else {}


def _scenario_window(scenario: TopicEditorScenario) -> Dict[str, Any]:
    return {
        "guild_id": scenario.guild_id,
        "start": scenario.start,
        "end": scenario.end,
        "fixture_dir": str(scenario.fixture_dir),
    }


async def replay_topic_editor_scenario(
    scenario_path: str | Path,
    out_dir: str | Path,
    *,
    actor_kind: str = "mock",
    mock_actor: bool = True,
    mock_mode: str = "submit",
    model: str = "mock-topic-editor",
) -> Dict[str, Any]:
    scenario = load_topic_editor_scenario(scenario_path)
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    previous_env = {
        "TOPIC_EDITOR_PUBLISHING_ENABLED": os.environ.get("TOPIC_EDITOR_PUBLISHING_ENABLED"),
        "LIVE_UPDATE_TRACE_CHANNEL_ID": os.environ.get("LIVE_UPDATE_TRACE_CHANNEL_ID"),
    }
    os.environ["TOPIC_EDITOR_PUBLISHING_ENABLED"] = "false"
    os.environ.pop("LIVE_UPDATE_TRACE_CHANNEL_ID", None)
    try:
        db = TopicEditorReplayDB(scenario)
        bot = ReplayDiscordClient()
        actor_kind = (actor_kind or ("mock" if mock_actor else "none")).strip().lower()
        if actor_kind == "mock":
            actor = MockTopicEditorActor(mode=mock_mode)
        elif actor_kind == "deepseek":
            from src.common.llm.deepseek_client import DeepSeekClient

            actor = DeepSeekClient()
            if not model or model == "mock-topic-editor":
                model = "deepseek-v4-pro"
        else:
            raise RuntimeError(f"unsupported replay actor_kind={actor_kind!r}")
        editor = TopicEditor(
            bot=bot,
            db_handler=db,
            llm_client=actor,
            guild_id=scenario.guild_id,
            live_channel_id=int((db.source_messages[0] or {}).get("channel_id") or 0) if db.source_messages else 0,
            environment="agentic-replay",
            model=model,
            source_limit=max(200, len(db.source_messages)),
            actor_brief=scenario.brief_text,
        )
        result = await editor.run_once("agentic_replay")
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    draft = _latest_draft(db) or {}
    draft_json = draft.get("draft_json") or {}
    evidence = resolve_topic_editor_evidence_shelf(db.source_messages, db=None, guild_id=scenario.guild_id, environment="agentic-replay")
    source_metadata = {str(row.get("message_id")): row for row in db.source_messages if row.get("message_id") is not None}
    limits = TopicEditorDraftLimits()
    validation = draft.get("validation_result")
    if draft_json and not validation:
        validation = validate_topic_editor_draft(draft_json, evidence, source_metadata, limits)
    preview = draft.get("preview_units")
    if draft_json and not preview:
        try:
            preview = preview_topic_editor_draft(draft_json, source_metadata, evidence_shelf=evidence, limits=limits)
        except Exception:
            preview = []
    rendered = []
    if draft_json:
        try:
            rendered = render_draft_publish_units(draft_json, source_metadata, evidence_shelf=evidence)
        except Exception:
            rendered = []
    completion = _latest_completion(db)
    metadata = ((completion.get("updates") or {}).get("metadata") or {})
    outcomes = metadata.get("outcomes") or result.get("outcomes") or []
    final_action = {
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "submitted": bool(draft and draft.get("status") == "submitted"),
        "draft_id": draft.get("draft_id"),
        "publish_results": result.get("publish_results") or [],
        "forced_close": metadata.get("forced_close"),
        "forced_close_reason": metadata.get("forced_close_reason"),
    }
    tool_trace = [
        {"id": call.get("id"), "name": call.get("name"), "input": call.get("input")}
        for call in metadata.get("tool_calls") or []
    ]
    summary = {
        "scenario": scenario.name,
        "status": result.get("status"),
        "brief_sha256": hashlib.sha256(scenario.brief_text.encode("utf-8")).hexdigest(),
        "actor_kind": actor_kind,
        "mock_actor": actor_kind == "mock",
        "model": model,
        "tool_call_count": len(tool_trace),
        "draft_status": draft.get("status"),
        "safety_event_count": len(db.safety_events) + len(bot.sends),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    _write_json(out / "scenario.yaml", scenario.raw)
    (out / "brief.md").write_text(scenario.brief_text)
    _write_json(out / "input_window.json", _scenario_window(scenario))
    for name in ("source_messages", "active_topics", "topic_sources", "recent_transitions", "media_metadata", "server_config_snapshot"):
        _atomic_copy(scenario.fixture_dir / f"{name}.json", out / f"{name}.json")
    _write_json(out / "tool_trace.json", {"calls": tool_trace, "outcomes": outcomes})
    _write_json(out / "draft.json", draft_json)
    _write_json(out / "draft_versions.json", db.drafts)
    _write_json(out / "validation.json", validation)
    _write_json(out / "preview.json", preview)
    (out / "rendered_messages.txt").write_text("\n\n---\n\n".join(str(unit.get("content") or unit.get("source_url") or unit) for unit in rendered))
    _write_json(out / "final_action.json", final_action)
    writes = db.simulated_writes()
    writes["discord_sends"] = bot.sends
    _write_json(out / "simulated_db_writes.json", writes)
    (out / "agent_report.md").write_text(str(metadata.get("reasoning") or "No agent reasoning captured.") + "\n")
    _write_json(out / "summary.json", summary)
    return summary


def assess_topic_editor_pack(pack_dir: str | Path) -> Dict[str, Any]:
    pack = Path(pack_dir)
    checks: Dict[str, Dict[str, Any]] = {}
    buckets: Dict[str, List[str]] = {}

    def fail(check: str, bucket: str, detail: str) -> None:
        checks.setdefault(check, {"status": "pass", "details": []})
        checks[check]["status"] = "fail"
        checks[check]["details"].append(detail)
        buckets.setdefault(bucket, []).append(detail)

    def ensure(check: str) -> None:
        checks.setdefault(check, {"status": "pass", "details": []})

    required = ["tool_trace.json", "final_action.json", "summary.json"]
    draft = _read_json(pack / "draft.json", {})
    if draft:
        required.extend(["draft.json", "validation.json", "preview.json"])
    for name in required:
        if not (pack / name).exists() or (pack / name).stat().st_size == 0:
            fail("deliverable_shape", "missing_evidence", f"missing or empty {name}")
    ensure("deliverable_shape")

    final_action = _read_json(pack / "final_action.json", {})
    validation = _read_json(pack / "validation.json", {})
    preview = _read_json(pack / "preview.json", [])
    writes = _read_json(pack / "simulated_db_writes.json", {})
    source_messages = _read_json(pack / "source_messages.json", [])
    source_ids = {str(row.get("message_id")) for row in source_messages if row.get("message_id") is not None}

    submitted = bool(final_action.get("submitted"))
    if final_action.get("status") != "completed" or final_action.get("forced_close"):
        fail(
            "final_action_contract",
            "validation_bypassed",
            f"run did not close cleanly: status={final_action.get('status')} forced_close={final_action.get('forced_close')} reason={final_action.get('forced_close_reason')}",
        )
    if submitted:
        if validation.get("status") != "valid" or validation.get("errors"):
            fail("validation_contract", "validation_bypassed", "submitted draft did not have valid validation")
        if not preview:
            fail("preview_contract", "no_preview", "submitted draft has no preview")
    ensure("validation_contract")
    ensure("preview_contract")

    rendered_text = (pack / "rendered_messages.txt").read_text() if (pack / "rendered_messages.txt").exists() else ""
    for index, unit in enumerate([part for part in rendered_text.split("\n\n---\n\n") if part.strip()]):
        if len(unit) > 2000:
            fail("discord_length_contract", "overlong_draft", f"rendered unit {index} is {len(unit)} chars")
    ensure("discord_length_contract")

    for card_index, card in enumerate(draft.get("cards") or []):
        ids = [str(mid) for mid in card.get("source_message_ids") or []]
        if not ids:
            fail("source_contract", "fabricated_source", f"card {card_index} has no sources")
        for mid in ids:
            if mid not in source_ids:
                fail("source_contract", "fabricated_source", f"card {card_index} source {mid} not in source window")
        markers = {int(match) for match in re.findall(r"\[(\d+)\]", str(card.get("body") or ""))}
        for marker in markers:
            if marker < 1 or marker > len(ids):
                fail("citation_contract", "missing_citations", f"card {card_index} marker [{marker}] does not resolve")
        for media_id in card.get("media_ids") or []:
            if ":" in str(media_id):
                mid = str(media_id).split(":", 1)[0]
                if mid not in source_ids:
                    fail("media_contract", "unresolved_media", f"media {media_id} source message missing")
    ensure("source_contract")
    ensure("citation_contract")
    ensure("media_contract")

    if submitted is False and final_action.get("status") == "completed" and draft:
        fail("final_action_contract", "weak_publish_choice", "draft exists but was not submitted")
    ensure("final_action_contract")

    for event in writes.get("safety_events") or []:
        if event.get("would_have_touched_prod") or event.get("captured_only") is False:
            fail("no_prod_write_contract", "prod_write_attempt", f"unsafe event {event.get('operation')}")
    for event in writes.get("discord_sends") or []:
        if event.get("would_have_touched_prod") or event.get("captured_only") is False:
            fail("no_prod_write_contract", "prod_write_attempt", "unsafe discord send")
    ensure("no_prod_write_contract")

    status = "pass" if all(check["status"] == "pass" for check in checks.values()) else "fail"
    report = {"status": status, "checks": checks, "failure_buckets": buckets}
    _write_json(pack / "assessment.json", report)
    _write_json(pack / "failure_buckets.json", buckets)
    lines = [f"# Assessment: {status}", ""]
    for name, check in sorted(checks.items()):
        lines.append(f"- {name}: {check['status']}")
        for detail in check.get("details") or []:
            lines.append(f"  - {detail}")
    (pack / "assessment.md").write_text("\n".join(lines) + "\n")
    return report


def summarize_topic_editor_agentic_runs(root_dir: str | Path) -> Dict[str, Any]:
    root = Path(root_dir)
    packs = []
    bucket_counts: Dict[str, int] = {}
    for assessment_path in root.rglob("assessment.json"):
        assessment = _read_json(assessment_path, {})
        summary = _read_json(assessment_path.parent / "summary.json", {})
        packs.append({
            "pack": str(assessment_path.parent),
            "scenario": summary.get("scenario"),
            "status": assessment.get("status"),
            "buckets": sorted((assessment.get("failure_buckets") or {}).keys()),
        })
        for bucket in (assessment.get("failure_buckets") or {}).keys():
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    result = {"pack_count": len(packs), "packs": packs, "bucket_counts": bucket_counts}
    _write_json(root / "summary.json", result)
    lines = ["# Topic Editor Agentic Runs", "", f"Packs: {len(packs)}", ""]
    for bucket, count in sorted(bucket_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {bucket}: {count}")
    (root / "summary.md").write_text("\n".join(lines) + "\n")
    return result
