"""Unit tests for the daily hivemind workflow-source scan."""

from src.features.summarising.workflow_source_scan import (
    AUTHOR_WHITELIST,
    QUALITY_CHANNELS,
    SOURCE,
    _author_score,
    _build_envelope,
    _canonical_url,
    _channel_score,
    _external_id,
    _extract_workflow_urls,
    _looks_like_workflow_url,
    _passes_quality_gates,
    _praise_score,
    _reaction_count,
    _title_from_url,
)


def test_quality_channel_scope():
    assert "minimax_h3_resources" in QUALITY_CHANNELS
    assert "minimax_h3_chatter" in QUALITY_CHANNELS
    assert "kijai" in AUTHOR_WHITELIST


def test_channel_score():
    assert _channel_score("minimax_h3_resources") == 2
    assert _channel_score("resources") == 2
    assert _channel_score("minimax_h3_chatter") == 1
    # Curated digests and generation highlights are NOT auto-pass channels.
    assert _channel_score("top_gens") == 0
    assert _channel_score("daily_summaries") == 0
    assert _channel_score("off-topic") == 0
    assert _channel_score(None) == 0


def test_author_score_exact_match_only():
    assert _author_score("Kijai") == 3
    assert _author_score("kijai") == 3
    assert _author_score("mdkb") == 3
    # Substring matches must NOT count: "mdkb" must not match "some_mdkb_fan".
    assert _author_score("some_mdkb_fan") == 0
    assert _author_score("some_rando") == 0
    assert _author_score(None) == 0


def test_praise_score():
    assert _praise_score("this workflow is insane") == 1
    assert _praise_score("nothing here") == 0


def test_reaction_count_reads_archive_column():
    # The discord_messages archive stores reaction_count (structure.md:180).
    assert _reaction_count({"reaction_count": 7}) == 7
    assert _reaction_count({"reaction_count": 0}) == 0
    # Tolerate the legacy/alternate shapes too.
    assert _reaction_count({"reactions": {"count": 7}}) == 7
    assert _reaction_count({"reactions": [{"count": 2}, {"count": 3}]}) == 5
    assert _reaction_count({"reactions": None}) == 0
    assert _reaction_count({}) == 0


def test_looks_like_workflow_url():
    assert _looks_like_workflow_url(
        "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_minimax_h3_t2v.json"
    )
    assert _looks_like_workflow_url(
        "https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo/blob/main/example_workflows/minimax_h3_t2v_turbo.json"
    )
    assert _looks_like_workflow_url(
        "https://cdn.discordapp.com/attachments/1/2/wf.png",
        filename="wf.png",
    )
    # Repo homepages, .py blob pages, and arbitrary image hosts are NOT workflows.
    assert not _looks_like_workflow_url("https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop")
    assert not _looks_like_workflow_url(
        "https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef/blob/abc123/nodes.py#L168-L200"
    )
    assert not _looks_like_workflow_url("https://huggingface.co/Alissonerdx/CharacterSheet/tree/main")
    assert not _looks_like_workflow_url("https://example.com/image.png")


def test_extract_workflow_urls():
    message = {
        "content": (
            "check https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop"
            "/blob/main/example_workflows/Loop.json and "
            "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/"
            "templates/video_minimax_h3_t2v.json plus the repo "
            "https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop"
        ),
        "attachments": [
            {"url": "https://cdn.discordapp.com/attachments/1/2/wf.png", "filename": "wf.png"},
            {"url": "https://example.com/not-a-workflow", "filename": "readme.txt"},
        ],
    }
    urls = _extract_workflow_urls(message)
    assert len(urls) == 3  # repo homepage and readme.txt are excluded
    assert any("Loop.json" in u for u in urls)
    assert any("raw.githubusercontent.com" in u for u in urls)
    assert any("cdn.discordapp.com" in u for u in urls)
    assert not any(u.endswith("Contex-Loop") for u in urls)


def test_quality_gate_pass_fail():
    weak = {"channel_name": "off-topic", "author_name": "nobody", "content": "meh", "reaction_count": 0}
    assert _passes_quality_gates(weak, min_reactions=2) is False
    resource = {"channel_name": "minimax_h3_resources", "author_name": "nobody", "content": "meh", "reaction_count": 0}
    assert _passes_quality_gates(resource, min_reactions=2) is True
    praised = {"channel_name": "minimax_h3_chatter", "author_name": "nobody", "content": "this workflow is honestly amazing, great quality", "reaction_count": 0}
    assert _passes_quality_gates(praised, min_reactions=2) is True
    # top_gens / daily_summaries do not auto-pass even with reactions.
    gens = {"channel_name": "top_gens", "author_name": "nobody", "content": "meh", "reaction_count": 5}
    assert _passes_quality_gates(gens, min_reactions=2) is True  # reactions still count
    gens_no_react = {"channel_name": "top_gens", "author_name": "nobody", "content": "meh", "reaction_count": 0}
    assert _passes_quality_gates(gens_no_react, min_reactions=2) is False
    reacted = {"channel_name": "off-topic", "author_name": "nobody", "content": "meh", "reaction_count": 5}
    assert _passes_quality_gates(reacted, min_reactions=2) is True
    # One-word praise in a chatter channel is too weak.
    terse = {"channel_name": "minimax_h3_chatter", "author_name": "nobody", "content": "amazing", "reaction_count": 0}
    assert _passes_quality_gates(terse, min_reactions=2) is False


def test_title_from_url():
    assert _title_from_url(
        "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/"
        "templates/video_minimax_h3_t2v.json"
    ) == "Video Minimax H3 T2V"
    assert _title_from_url(
        "https://cdn.discordapp.com/attachments/1535700117452226560/1537037468736557117/my_workflow.json"
    ) == "My Workflow"
    assert _title_from_url(
        "https://cdn.discordapp.com/attachments/1535700117452226560/1537037468736557117/image.png?ex=1"
    ) == "Discord workflow share"
    assert _title_from_url("https://huggingface.co/Alissonerdx/CharacterSheet/tree/main") == "Discord workflow share"


def test_canonical_url_strips_signed_tokens():
    raw = "https://cdn.discordapp.com/attachments/1535700117452226560/1537037468736557117/wf.json?ex=6a7e3df1&is=6a7cec71&hm=abc123"
    assert _canonical_url(raw) == (
        "https://cdn.discordapp.com/attachments/1535700117452226560/1537037468736557117/wf.json"
    )
    # Non-CDN URLs pass through unchanged.
    assert _canonical_url("https://raw.githubusercontent.com/a/b/main/wf.json") == (
        "https://raw.githubusercontent.com/a/b/main/wf.json"
    )


def test_external_id_stable_across_signed_token_refresh():
    message = {"message_id": 123456789012345678}
    url_a = "https://cdn.discordapp.com/attachments/1/2/wf.json?ex=AAA&hm=111"
    url_b = "https://cdn.discordapp.com/attachments/1/2/wf.json?ex=BBB&hm=222"
    assert _external_id(message, url_a, 0) == _external_id(message, url_b, 0)


def test_envelope_shape_and_idempotency():
    message = {
        "message_id": 123456789012345678,
        "guild_id": 111,
        "channel_id": 222,
        "channel_name": "minimax_h3_resources",
        "author_name": "mdkb",
        "content": "new wf",
        "reaction_count": 0,
    }
    url = "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_minimax_h3_t2v.json"
    envelope = _build_envelope(message, url, 0)
    data = envelope["data"]
    assert envelope["action"] == "add_resource"
    assert data["kind"] == "workflow"
    assert data["source"] == SOURCE
    assert data["url"] == url
    assert "discord.com/channels/111/222/123456789012345678" in data["metadata"]["provenance"]["discord_message_url"]
    assert _external_id(message, url, 0) == data["external_id"]
    assert _external_id(message, url, 0) == _external_id(message, url, 0)


def test_envelope_url_is_canonical_for_cdn():
    message = {
        "message_id": 1,
        "channel_name": "minimax_h3_resources",
        "author_name": "ketamin",
        "content": "wf here",
        "reaction_count": 0,
    }
    url = "https://cdn.discordapp.com/attachments/1/2/wf.json?ex=AAA&hm=111"
    envelope = _build_envelope(message, url, 0)
    assert envelope["data"]["url"] == "https://cdn.discordapp.com/attachments/1/2/wf.json"
    assert "?ex=" not in envelope["data"]["url"]


def test_envelope_title_fallback_to_content():
    message = {
        "message_id": 1,
        "channel_name": "minimax_h3_resources",
        "author_name": "ketamin",
        "content": "just this https://huggingface.co/Alissonerdx/CharacterSheet/tree/main",
        "reaction_count": 0,
    }
    envelope = _build_envelope(
        message,
        "https://huggingface.co/Alissonerdx/CharacterSheet/tree/main",
        0,
    )
    assert envelope["data"]["title"] != "Discord workflow share"
