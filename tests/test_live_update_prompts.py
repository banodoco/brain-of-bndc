import asyncio
import json

from src.features.summarising.live_update_prompts import LiveUpdateCandidateGenerator


def test_heuristic_candidate_generator_emits_multiple_auditable_update_types():
    generator = LiveUpdateCandidateGenerator(max_candidates=5)

    candidates = asyncio.run(generator.generate_candidates(
        messages=[
            {
                "message_id": 101,
                "channel_id": 11,
                "author_id": 1001,
                "content": "We shipped the new storyboard release with better image controls and project export.",
                "reaction_count": 4,
                "created_at": "2026-05-08T10:00:00Z",
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 1001, "display_name": "Ari"},
            },
            {
                "message_id": 102,
                "channel_id": 11,
                "author_id": 1002,
                "content": "The creator onboarding beta needs testers for the new workflow handoff before tomorrow.",
                "reaction_count": 6,
                "reply_count": 3,
                "created_at": "2026-05-08T10:05:00Z",
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 1002, "display_name": "Bea"},
            },
            {
                "message_id": 103,
                "channel_id": 11,
                "author_id": 1003,
                "content": "New generation test from the motion workflow.",
                "reaction_count": 5,
                "created_at": "2026-05-08T10:10:00Z",
                "attachments": json.dumps([{
                    "url": "https://cdn.example.test/render.png",
                    "content_type": "image/png",
                    "filename": "render.png",
                }]),
                "embeds": [],
                "author_context_snapshot": {"member_id": 1003, "display_name": "Cam"},
            },
            {
                "message_id": 104,
                "channel_id": 11,
                "author_id": 1004,
                "content": "yeah, I just want something good cooking in the background while I work on my frame analysis code",
                "reaction_count": 0,
                "created_at": "2026-05-08T10:15:00Z",
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 1004, "display_name": "Dee"},
            },
        ],
        run_id="run-1",
        guild_id=123,
        memory=[],
        watchlist=[{"watch_key": "storyboard"}],
    ))

    update_types = {candidate["update_type"] for candidate in candidates}
    assert {"release", "request", "showcase"}.issubset(update_types)
    for candidate in candidates:
        assert candidate["run_id"] == "run-1"
        assert candidate["guild_id"] == 123
        assert candidate["title"]
        assert candidate["body"]
        assert candidate["source_message_ids"]
        assert candidate["author_context_snapshot"]
        assert candidate["duplicate_key"]
        assert isinstance(candidate["confidence"], float)
        assert isinstance(candidate["priority"], int)
        assert candidate["rationale"]
        assert candidate["raw_agent_output"]["generator"] == "heuristic_live_update_editor_v1"


def test_llm_candidate_generator_normalizes_json_and_preserves_raw_output():
    raw_response = json.dumps({
        "candidates": [
            {
                "update_type": "release",
                "title": "Storyboard release is ready",
                "body": "The new storyboard release is ready for creators.",
                "media_refs": [{"kind": "attachment", "url": "https://cdn.example.test/demo.png"}],
                "source_message_ids": ["201"],
                "confidence": 0.91,
                "priority": 4,
                "rationale": "The source message announces a concrete release.",
            },
            {
                "update_type": "question",
                "title": "Creator onboarding needs testers",
                "body": "The creator onboarding beta needs testers for the new workflow handoff.",
                "source_message_ids": ["202"],
                "author_context_snapshot": {"member_id": 2002, "display_name": "Drew"},
                "confidence": 0.74,
                "priority": 2,
                "rationale": "The question is actionable for the community.",
            },
        ]
    })

    class FakeLLM:
        async def generate_chat_completion(self, **kwargs):
            assert kwargs["system_prompt"]
            assert kwargs["messages"][0]["role"] == "user"
            return raw_response

    generator = LiveUpdateCandidateGenerator(llm_client=FakeLLM(), max_candidates=5)
    candidates = asyncio.run(generator.generate_candidates(
        messages=[
            {
                "message_id": 201,
                "channel_id": 21,
                "author_id": 2001,
                "content": "The new storyboard release is ready for creators.",
                "reaction_count": 3,
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 2001, "display_name": "Cai"},
            },
            {
                "message_id": 202,
                "channel_id": 21,
                "author_id": 2002,
                "content": "The creator onboarding beta needs testers for the new workflow handoff.",
                "reaction_count": 6,
                "reply_count": 3,
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 2002, "display_name": "Drew"},
            },
        ],
        run_id="run-2",
        guild_id=456,
        memory=[],
        watchlist=[],
    ))

    assert [candidate["update_type"] for candidate in candidates] == ["release", "question"]
    assert candidates[0]["media_refs"][0]["url"] == "https://cdn.example.test/demo.png"
    assert candidates[0]["author_context_snapshot"]["display_name"] == "Cai"
    assert candidates[0]["duplicate_key"]
    assert candidates[0]["raw_agent_output"]["generator"] == "live_update_editor_llm_v1"
    assert candidates[0]["raw_agent_output"]["raw_text"] == raw_response
    assert candidates[0]["raw_agent_output"]["raw_candidate"]["title"] == "Storyboard release is ready"
    assert candidates[1]["author_context_snapshot"]["display_name"] == "Drew"


def test_candidate_generator_rejects_minor_chatter_even_when_llm_selects_it():
    raw_response = json.dumps({
        "candidates": [
            {
                "update_type": "project_update",
                "title": "Frame analysis work continues",
                "body": "A member said they want something cooking while working on frame analysis code.",
                "source_message_ids": ["301"],
                "confidence": 0.93,
                "priority": 4,
                "rationale": "The LLM incorrectly treated a personal status update as news.",
            },
            {
                "update_type": "question",
                "title": "General help request",
                "body": "Someone asked if anyone can help with a prompt.",
                "source_message_ids": ["302"],
                "confidence": 0.91,
                "priority": 3,
                "rationale": "The LLM incorrectly treated ordinary help chatter as news.",
            },
        ]
    })

    class FakeLLM:
        async def generate_chat_completion(self, **kwargs):
            return raw_response

    generator = LiveUpdateCandidateGenerator(llm_client=FakeLLM(), max_candidates=5)
    candidates = asyncio.run(generator.generate_candidates(
        messages=[
            {
                "message_id": 301,
                "channel_id": 21,
                "author_id": 2001,
                "content": "yeah, I just want something good cooking in the background while I work on my frame analysis code",
                "reaction_count": 0,
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 2001, "display_name": "Cai"},
            },
            {
                "message_id": 302,
                "channel_id": 21,
                "author_id": 2002,
                "content": "Can anyone help me come up with a different motion prompt?",
                "reaction_count": 1,
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 2002, "display_name": "Drew"},
            },
        ],
        run_id="run-3",
        guild_id=456,
        memory=[],
        watchlist=[],
    ))

    assert candidates == []


def test_candidate_generator_rejects_llm_candidate_without_valid_source_ids():
    raw_response = json.dumps({
        "candidates": [
            {
                "decision": "publish",
                "update_type": "release",
                "title": "Storyboard release is ready",
                "body": "The new storyboard release is ready for creators.",
                "source_message_ids": [],
                "confidence": 0.97,
                "priority": 5,
                "rationale": "Missing provenance should not be accepted.",
            }
        ]
    })

    class FakeLLM:
        async def generate_chat_completion(self, **kwargs):
            return raw_response

    generator = LiveUpdateCandidateGenerator(llm_client=FakeLLM(), max_candidates=5)
    candidates = asyncio.run(generator.generate_candidates(
        messages=[
            {
                "message_id": 401,
                "channel_id": 21,
                "author_id": 2001,
                "content": "The new storyboard release is ready for creators.",
                "reaction_count": 5,
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 2001, "display_name": "Cai"},
            },
        ],
        run_id="run-4",
        guild_id=456,
        memory=[],
        watchlist=[],
    ))

    assert candidates == []


def test_candidate_generator_can_take_tool_turn_before_final_candidates():
    responses = [
        json.dumps({
            "tool_requests": [
                {"tool": "search_messages", "args": {"query": "storyboard release", "limit": 3}}
            ]
        }),
        json.dumps({
            "candidates": [
                {
                    "decision": "publish",
                    "update_type": "release",
                    "title": "Storyboard release is ready",
                    "body": "The new storyboard release shipped with project export controls for creators.",
                    "source_message_ids": ["501"],
                    "confidence": 0.92,
                    "priority": 4,
                    "rationale": "The source and search results confirm a concrete release.",
                }
            ]
        }),
    ]

    class FakeLLM:
        def __init__(self):
            self.calls = []

        async def generate_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            return responses[len(self.calls) - 1]

    tool_calls = []

    async def fake_tool_runner(tool, args):
        tool_calls.append((tool, args))
        return {"messages": [{"message_id": "490", "content": "Earlier storyboard release context"}]}

    llm = FakeLLM()
    generator = LiveUpdateCandidateGenerator(llm_client=llm, max_candidates=5)
    candidates = asyncio.run(generator.generate_candidates(
        messages=[
            {
                "message_id": 501,
                "channel_id": 21,
                "author_id": 2001,
                "content": "We shipped the new storyboard release with project export controls.",
                "reaction_count": 5,
                "attachments": [],
                "embeds": [],
                "author_context_snapshot": {"member_id": 2001, "display_name": "Cai"},
            },
        ],
        run_id="run-5",
        guild_id=456,
        memory=[],
        watchlist=[],
        tool_runner=fake_tool_runner,
    ))

    assert len(llm.calls) == 2
    assert tool_calls == [("search_messages", {"query": "storyboard release", "limit": 3})]
    assert candidates[0]["raw_agent_output"]["agent_turn_count"] == 2
    assert candidates[0]["raw_agent_output"]["tool_trace"][0]["ok"] is True


def test_llm_prompt_exposes_agent_budget_and_media_reaction_guidance():
    captured = {}

    class FakeLLM:
        async def generate_chat_completion(self, **kwargs):
            captured["system_prompt"] = kwargs["system_prompt"]
            captured["user_payload"] = json.loads(kwargs["messages"][0]["content"])
            return json.dumps({"candidates": []})

    generator = LiveUpdateCandidateGenerator(
        llm_client=FakeLLM(),
        max_agent_turns=100,
        max_tool_requests_per_turn=8,
    )
    asyncio.run(generator.generate_candidates(
        messages=[
            {
                "message_id": 601,
                "guild_id": 456,
                "channel_id": 21,
                "thread_id": 22,
                "reference_id": 600,
                "author_id": 2001,
                "content": "Sharing a new motion test that people seem to like.",
                "reaction_count": 8,
                "attachments": json.dumps([{
                    "url": "https://cdn.example.test/motion.mp4",
                    "content_type": "video/mp4",
                    "filename": "motion.mp4",
                }]),
                "embeds": [],
                "author_context_snapshot": {"member_id": 2001, "display_name": "Cai"},
            },
        ],
        run_id="run-6",
        guild_id=456,
        memory=[],
        watchlist=[],
    ))

    assert captured["user_payload"]["agent_runtime_budget"]["max_agent_turns"] == 100
    assert captured["user_payload"]["agent_runtime_budget"]["max_tool_requests_per_turn"] == 8
    assert "media_selected_when_useful" in captured["system_prompt"]
    assert "whether a media post drew real response" in captured["system_prompt"]
    assert "who reacted, and whether the engagement came from high-signal members" in json.dumps(captured["user_payload"]["available_tools"])
    assert any(tool["tool"] == "get_engagement_context" for tool in captured["user_payload"]["available_tools"])
    assert captured["user_payload"]["message_window_coverage"]["prompt_message_count"] == 1
    assert "is_last_hour" in captured["user_payload"]["messages"][0]
    assert "age_bucket" in captured["user_payload"]["messages"][0]
    assert captured["user_payload"]["messages"][0]["is_reply"] is True
    assert captured["user_payload"]["messages"][0]["reference_id"] == 600
    assert captured["user_payload"]["messages"][0]["is_thread_message"] is True
    assert captured["user_payload"]["messages"][0]["reply_jump_url"].endswith("/22/600")
    assert "reply_jump_url" in captured["user_payload"]["messages"][0]
    assert "older message can become newly relevant" in captured["system_prompt"]
    assert any(tool["tool"] == "get_recent_reactions" for tool in captured["user_payload"]["available_tools"])


# ── Bar-relaxation tests (T15) ──


def _make_msg(message_id, content, reaction_count=0, reply_count=0, has_media=True):
    """Helper to build a source message dict for editorial bar tests."""
    attachments = []
    if has_media:
        attachments = json.dumps([{"url": "https://cdn.example.test/img.png", "content_type": "image/png"}])
    return {
        "message_id": message_id,
        "channel_id": 10,
        "author_id": 1000 + message_id,
        "content": content,
        "reaction_count": reaction_count,
        "reply_count": reply_count,
        "attachments": attachments,
        "embeds": [],
    }


def _media_refs():
    return [{"kind": "attachment", "url": "https://cdn.example.test/img.png"}]


def _meets_bar(update_type, body="Test body with enough detail for a live update.", title="Test Title",
               media_refs=None, reaction_count=0, reply_count=0, scanned_message_count=100,
               is_last_call=False):
    """Shortcut to call _meets_editorial_bar with minimal boilerplate."""
    if media_refs is None:
        media_refs = _media_refs()
    return LiveUpdateCandidateGenerator._meets_editorial_bar(
        update_type=update_type,
        body=body,
        title=title,
        media_refs=media_refs,
        source_messages=[_make_msg(1, body, reaction_count, reply_count, has_media=bool(media_refs))],
        confidence=0.8,
        scanned_message_count=scanned_message_count,
        is_last_call=is_last_call,
    )


# ── (a) New thresholds ──


def test_showcase_bar_relaxed_to_reactions_3_or_reply_2():
    """Showcase now accepts reactions>=3 OR reply_count>=2 (was >=5)."""
    assert _meets_bar("showcase", reaction_count=3, reply_count=0)   # reactions=3 passes
    assert _meets_bar("showcase", reaction_count=2, reply_count=2)   # reply=2 passes
    assert _meets_bar("showcase", reaction_count=1, reply_count=2)   # reply=2 alone passes
    assert not _meets_bar("showcase", reaction_count=2, reply_count=1)  # neither meets bar


def test_top_creation_bar_relaxed_to_reactions_3():
    """Top_creation now accepts reactions>=3 (was >=5)."""
    assert _meets_bar("top_creation", reaction_count=3)  # passes at 3
    assert _meets_bar("top_creation", reaction_count=5)  # 5 still passes
    assert not _meets_bar("top_creation", reaction_count=2)  # 2 fails


def test_project_update_bar_unchanged():
    """Project_update threshold remains permissive but concrete-artifact signal + engagement required."""
    body = "We shipped the new storyboard release with project export controls and GitHub link."
    assert _meets_bar("project_update", body=body, reaction_count=3)
    # Without media, needs reactions>=3 or reply>=2
    assert not _meets_bar("project_update", body=body, reaction_count=0, media_refs=[])  # no engagement, no media → rejected
    assert _meets_bar("project_update", body=body, reaction_count=3, media_refs=[])  # reactions=3 passes


def test_showcase_requires_media():
    """Showcase must have has_media=True even with enough reactions."""
    assert not _meets_bar("showcase", reaction_count=5, media_refs=[])  # no media → rejected


def test_top_creation_requires_media():
    """Top_creation must have has_media=True."""
    assert not _meets_bar("top_creation", reaction_count=5, media_refs=[])  # no media → rejected


def test_author_is_high_signal_degrades_gracefully():
    """author_is_high_signal does not exist on candidate payloads — degrades to False.
    The OR clause is a no-op: showcase still needs reactions>=3 or reply_count>=2."""
    # reactions=3 passes on its own; author_is_high_signal=False doesn't change that
    assert _meets_bar("showcase", reaction_count=3)
    # reactions=2, reply=1 normally fails — author_is_high_signal=False doesn't rescue it
    assert not _meets_bar("showcase", reaction_count=2, reply_count=1)


# ── (b) Quiet-hour rule (<50 scanned messages) ──


def test_quiet_hour_drops_showcase_bar():
    """When scanned_message_count < 50, showcase drops to reactions>=2 OR reply>=1."""
    assert _meets_bar("showcase", reaction_count=2, reply_count=0, scanned_message_count=30)
    assert _meets_bar("showcase", reaction_count=1, reply_count=1, scanned_message_count=30)
    assert not _meets_bar("showcase", reaction_count=1, reply_count=0, scanned_message_count=30)


def test_quiet_hour_drops_top_creation_bar():
    """When scanned_message_count < 50, top_creation drops to reactions>=2."""
    assert _meets_bar("top_creation", reaction_count=2, scanned_message_count=30)
    assert not _meets_bar("top_creation", reaction_count=1, scanned_message_count=30)


def test_quiet_hour_no_effect_when_plenty_of_messages():
    """When scanned_message_count >= 50, normal thresholds apply."""
    assert not _meets_bar("showcase", reaction_count=2, reply_count=1, scanned_message_count=60)
    assert _meets_bar("showcase", reaction_count=3, reply_count=1, scanned_message_count=60)  # normal bar: >=3 reactions


# ── (c) Last-call watchlist bar ──


def test_last_call_showcase_bar_lower():
    """When is_last_call=True, showcase accepts reactions>=2 OR reply>=1 (+media)."""
    assert _meets_bar("showcase", reaction_count=2, reply_count=0, is_last_call=True)
    assert _meets_bar("showcase", reaction_count=1, reply_count=1, is_last_call=True)
    assert not _meets_bar("showcase", reaction_count=1, reply_count=0, is_last_call=True)


def test_last_call_top_creation_bar_lower():
    """When is_last_call=True, top_creation accepts reactions>=2 OR reply>=1 (+media)."""
    assert _meets_bar("top_creation", reaction_count=2, reply_count=0, is_last_call=True)
    assert _meets_bar("top_creation", reaction_count=1, reply_count=1, is_last_call=True)
    assert not _meets_bar("top_creation", reaction_count=1, reply_count=0, is_last_call=True)


def test_last_call_project_update_bar_lower():
    """When is_last_call=True, project_update accepts reactions>=1 OR reply>=1."""
    body = "We shipped the new storyboard release with project export controls and GitHub link."
    assert _meets_bar("project_update", body=body, reaction_count=1, reply_count=0, is_last_call=True)
    assert _meets_bar("project_update", body=body, reaction_count=0, reply_count=1, is_last_call=True)


def test_last_call_still_requires_media_for_showcase():
    """Last-call bar still needs media for showcase/top_creation."""
    assert not _meets_bar("showcase", reaction_count=3, media_refs=[], is_last_call=True)


# ── Parser fallback chain tests (T14) ──


def _make_generator():
    """Create a minimal LiveUpdateCandidateGenerator for parser testing."""
    return LiveUpdateCandidateGenerator(max_candidates=5)


def test_parser_fallback_top_level_editor_reasoning():
    """Branch 1: top-level `editor_reasoning` key."""
    gen = _make_generator()
    raw = json.dumps({
        "editor_reasoning": "Scanned messages, found one showcase and one question.",
        "candidates": [],
    })
    gen._parse_raw_candidates(raw)
    assert gen.last_editor_reasoning == "Scanned messages, found one showcase and one question."
    assert gen.reasoning_recovery_path == "top_level"


def test_parser_fallback_alias_reasoning():
    """Branch 2: alias key `reasoning` (no top-level `editor_reasoning`)."""
    gen = _make_generator()
    raw = json.dumps({
        "reasoning": "Nothing strong enough to publish this hour.",
        "candidates": [],
    })
    gen._parse_raw_candidates(raw)
    assert gen.last_editor_reasoning == "Nothing strong enough to publish this hour."
    assert gen.reasoning_recovery_path == "alias:reasoning"


def test_parser_fallback_per_candidate_concatenation():
    """Branch 3: per-candidate `editor_reasoning` concatenated with ` | `."""
    gen = _make_generator()
    raw = json.dumps({
        "candidates": [
            {"title": "A", "editor_reasoning": "First candidate reasoning."},
            {"title": "B", "editor_reasoning": "Second candidate reasoning."},
        ],
    })
    gen._parse_raw_candidates(raw)
    assert gen.last_editor_reasoning == "First candidate reasoning. | Second candidate reasoning."
    assert gen.reasoning_recovery_path == "per_candidate"


def test_parser_fallback_reasoning_prefix_regex():
    """Branch 4: `REASONING:` prose prefix matched via regex."""
    gen = _make_generator()
    raw = "REASONING: Scanned 83 messages across 14 channels.\n\n" + '{"candidates": []}'
    gen._parse_raw_candidates(raw)
    assert "Scanned 83 messages across 14 channels" in gen.last_editor_reasoning
    assert gen.reasoning_recovery_path == "reasoning_prefix"


def test_parser_fallback_prose_first_paragraph():
    """Branch 5: first ≤3 sentences before JSON span as last resort."""
    gen = _make_generator()
    raw = "The community was quiet this hour. No standout creations or conversations.\n" + '{"candidates": []}'
    gen._parse_raw_candidates(raw)
    assert gen.last_editor_reasoning != ""
    assert gen.reasoning_recovery_path == "prose_first_paragraph"


def test_parser_fallback_empty_input_none():
    """Branch 6: totally empty input → `reasoning_recovery_path='none'`, no crash."""
    gen = _make_generator()
    gen._parse_raw_candidates("")
    assert gen.last_editor_reasoning == ""
    assert gen.reasoning_recovery_path == "none"
