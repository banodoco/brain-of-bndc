from datetime import date

from src.features.summarising.topic_editor import (
    TopicIdentity,
    build_override_transitions,
    build_rejected_transition,
    canonicalize_proposed_key,
    canonicalize_topic_key,
    detect_topic_collisions,
    resolve_topic_alias,
    shape_transition_payload,
    trigram_similarity,
    unresolved_collisions,
)


def test_topic_editor_canonicalizer_uses_locked_slug_creator_and_date():
    assert canonicalize_topic_key(
        "The OmniNFT LoRA for LTX 2.3!",
        creator_name="Gleb",
        topic_date=date(2026, 5, 13),
    ) == "gleb-the-omninft-lora-for-ltx-2-3-2026-05-13"

    assert canonicalize_proposed_key(None, "OpenCS2 dataset shipped") == "opencs2-dataset-shipped"


def test_topic_editor_alias_resolution_is_scope_aware():
    aliases = [
        {"topic_id": "wrong-env", "alias_key": "OmniNFT LTX", "environment": "dev", "guild_id": 1},
        {"topic_id": "topic-1", "alias_key": "OmniNFT LTX", "environment": "prod", "guild_id": 1},
    ]

    assert resolve_topic_alias("omninft-ltx", aliases, environment="prod", guild_id=1)["topic_id"] == "topic-1"
    assert resolve_topic_alias("omninft-ltx", aliases, environment="prod", guild_id=2) is None


def test_topic_editor_collision_detection_prefix_and_similarity_with_author_overlap():
    existing = [
        TopicIdentity(
            topic_id="prefix",
            canonical_key="gleb-omninft-lora",
            headline="Gleb previews OmniNFT LoRA for LTX",
            source_authors=("Gleb",),
        ),
        TopicIdentity(
            topic_id="similar",
            canonical_key="other-key",
            headline="OpenCS2 Counter Strike dataset gets released",
            source_authors=("alice",),
        ),
        TopicIdentity(
            topic_id="no-author-overlap",
            canonical_key="different-key",
            headline="OpenCS2 Counter Strike dataset gets released",
            source_authors=("bob",),
        ),
    ]

    collisions = detect_topic_collisions(
        proposed_canonical_key="gleb-omninft-lora-2026-05-13",
        headline="OpenCS2 Counter Strike dataset released",
        source_authors=["alice"],
        existing_topics=existing,
    )

    assert [collision.topic_id for collision in collisions] == ["prefix", "similar"]
    assert collisions[0].reason == "canonical_key_prefix"
    assert collisions[1].reason == "headline_similarity_author_overlap"
    assert trigram_similarity("OpenCS2 dataset", "OpenCS2 dataset") == 1.0


def test_topic_editor_override_and_rejected_transition_payloads_are_deterministic():
    collisions = detect_topic_collisions(
        proposed_canonical_key="demo-topic-v2",
        headline="Demo topic v2",
        source_authors=["alice"],
        existing_topics=[TopicIdentity("topic-1", "demo-topic", "Demo topic", ("alice",))],
    )
    assert unresolved_collisions(collisions, [{"topic_id": "topic-1", "reason": "different artifact"}]) == []

    payload = shape_transition_payload(
        outcome="tool_error",
        tool_name="post_simple_topic",
        canonical_key="demo-topic-v2",
        collisions=collisions,
        source_message_ids=[123],
        error="collision",
    )
    row = build_rejected_transition(
        run_id="run-1",
        environment="prod",
        guild_id=1,
        action="rejected_post_simple",
        tool_call_id="tool-1",
        reason="collision",
        payload=payload,
    )
    assert row["payload"]["collisions"][0]["topic_id"] == "topic-1"
    assert row["action"] == "rejected_post_simple"

    overrides = build_override_transitions(
        run_id="run-1",
        environment="prod",
        guild_id=1,
        topic_id="new-topic",
        override_collisions=[{"topic_id": "topic-1", "reason": "false positive"}],
    )
    assert overrides == [{
        "topic_id": "new-topic",
        "run_id": "run-1",
        "environment": "prod",
        "guild_id": 1,
        "tool_call_id": None,
        "action": "override",
        "reason": "false positive",
        "payload": {"overridden_topic_id": "topic-1", "reason": "false positive"},
        "model": None,
    }]
