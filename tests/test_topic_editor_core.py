from datetime import date

import pytest

from src.features.summarising.topic_editor import (
    TopicIdentity,
    _build_send_units,
    _resolve_media_url_from_metadata,
    block_media_refs,
    block_source_ids,
    build_override_transitions,
    build_rejected_transition,
    canonicalize_proposed_key,
    canonicalize_topic_key,
    chunk_text_for_discord,
    collect_document_source_ids,
    detect_topic_collisions,
    normalize_document_blocks,
    normalize_media_ref,
    normalize_topic_document,
    render_topic_publish_units,
    resolve_topic_alias,
    shape_transition_payload,
    trigram_similarity,
    unresolved_collisions,
)


# ------------------------------------------------------------------
# T2: Normalization helper tests
# ------------------------------------------------------------------


class TestNormalizeMediaRef:
    """Tests for normalize_media_ref covering shorthand, canonical, embed, and external refs."""

    def test_shorthand_attachment_index_converts_to_canonical(self):
        result = normalize_media_ref({"message_id": "123", "attachment_index": 0})
        assert result == {"message_id": "123", "kind": "attachment", "index": 0}

    def test_shorthand_with_string_index(self):
        result = normalize_media_ref({"message_id": "456", "attachment_index": "2"})
        assert result == {"message_id": "456", "kind": "attachment", "index": 2}

    def test_canonical_attachment_ref_passes_through(self):
        result = normalize_media_ref({"message_id": "789", "kind": "attachment", "index": 1})
        assert result == {"message_id": "789", "kind": "attachment", "index": 1}

    def test_canonical_embed_ref_passes_through(self):
        result = normalize_media_ref({"message_id": "999", "kind": "embed", "index": 0})
        assert result == {"message_id": "999", "kind": "embed", "index": 0}

    def test_embed_ref_without_explicit_kind(self):
        # When only index is given (no attachment_index shorthand), kind defaults to "attachment"
        result = normalize_media_ref({"message_id": "111", "index": 3})
        assert result == {"message_id": "111", "kind": "attachment", "index": 3}

    def test_rejects_invalid_kind(self):
        with pytest.raises(ValueError, match="kind must be 'attachment', 'embed', or 'external'"):
            normalize_media_ref({"message_id": "123", "kind": "image", "index": 0})

    def test_rejects_missing_message_id(self):
        with pytest.raises(ValueError, match="missing required 'message_id'"):
            normalize_media_ref({"attachment_index": 0})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="media_ref must be a dict"):
            normalize_media_ref("not_a_dict")

    def test_rejects_non_integer_index_in_canonical(self):
        with pytest.raises(ValueError, match="media_ref index must be an integer"):
            normalize_media_ref({"message_id": "123", "kind": "attachment", "index": "abc"})

    def test_rejects_non_integer_attachment_index(self):
        with pytest.raises(ValueError, match="media_ref attachment_index must be an integer"):
            normalize_media_ref({"message_id": "123", "attachment_index": "xyz"})

    # -- external kind acceptance --
    def test_external_kind_accepted(self):
        result = normalize_media_ref({"message_id": "123", "kind": "external", "index": 0})
        assert result == {"message_id": "123", "kind": "external", "index": 0}

    def test_external_kind_with_string_index(self):
        result = normalize_media_ref({"message_id": "456", "kind": "external", "index": "2"})
        assert result == {"message_id": "456", "kind": "external", "index": 2}

    def test_rejects_invalid_kind_after_external_added(self):
        # Unknown kind must still raise
        with pytest.raises(ValueError, match="kind must be 'attachment', 'embed', or 'external'"):
            normalize_media_ref({"message_id": "123", "kind": "video", "index": 0})
        with pytest.raises(ValueError, match="kind must be 'attachment', 'embed', or 'external'"):
            normalize_media_ref({"message_id": "456", "kind": "link", "index": 1})


class TestNormalizeDocumentBlocks:
    """Tests for normalize_document_blocks covering legacy and new-style blocks."""

    def test_legacy_body_becomes_intro_block(self):
        summary = {"body": "This is the intro text.", "sections": []}
        topic_source_ids = ["100", "200"]
        blocks = normalize_document_blocks(summary, topic_source_ids)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "intro"
        assert blocks[0]["text"] == "This is the intro text."
        assert blocks[0]["title"] is None
        assert blocks[0]["source_message_ids"] == ["100", "200"]
        assert blocks[0]["media_refs"] == []

    def test_legacy_sections_become_section_blocks(self):
        summary = {
            "body": "Intro.",
            "sections": [
                {"title": "Section A", "body": "Content A."},
                {"heading": "Section B", "text": "Content B."},
                {"title": "Section C", "summary": "Content C."},
            ],
        }
        topic_source_ids = ["10"]
        blocks = normalize_document_blocks(summary, topic_source_ids)
        assert len(blocks) == 4  # intro + 3 sections
        assert blocks[0]["type"] == "intro"
        assert blocks[1]["type"] == "section"
        assert blocks[1]["title"] == "Section A"
        assert blocks[1]["text"] == "Content A."
        assert blocks[2]["title"] == "Section B"
        assert blocks[2]["text"] == "Content B."
        assert blocks[3]["title"] == "Section C"
        assert blocks[3]["text"] == "Content C."

    def test_topic_level_source_ids_used_as_fallback_when_section_has_none(self):
        summary = {
            "body": "Intro.",
            "sections": [
                {"title": "S1", "body": "Text."},
                {"title": "S2", "body": "Text.", "source_message_ids": ["42"]},
            ],
        }
        topic_source_ids = ["1", "2"]
        blocks = normalize_document_blocks(summary, topic_source_ids)
        # Intro gets fallback
        assert blocks[0]["source_message_ids"] == ["1", "2"]
        # S1: no local -> fallback
        assert blocks[1]["source_message_ids"] == ["1", "2"]
        # S2: has local -> uses local
        assert blocks[2]["source_message_ids"] == ["42"]

    def test_new_style_blocks_preserved(self):
        summary = {
            "blocks": [
                {
                    "type": "intro",
                    "text": "Intro block.",
                    "source_message_ids": ["a"],
                    "media_refs": [{"message_id": "123", "attachment_index": 0}],
                },
                {
                    "type": "section",
                    "title": "Details",
                    "text": "Detail text.",
                    "source_message_ids": ["b", "c"],
                    "media_refs": [{"message_id": "456", "kind": "embed", "index": 0}],
                },
            ]
        }
        blocks = normalize_document_blocks(summary)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "intro"
        assert blocks[0]["text"] == "Intro block."
        assert blocks[0]["source_message_ids"] == ["a"]
        assert blocks[0]["media_refs"] == [{"message_id": "123", "kind": "attachment", "index": 0}]
        assert blocks[1]["type"] == "section"
        assert blocks[1]["title"] == "Details"
        assert blocks[1]["source_message_ids"] == ["b", "c"]
        assert blocks[1]["media_refs"] == [{"message_id": "456", "kind": "embed", "index": 0}]

    def test_new_blocks_skip_invalid_types(self):
        summary = {
            "blocks": [
                {"type": "unknown", "text": "skip me"},
                {"type": "intro", "text": "valid intro"},
                {"type": "section", "text": "valid section"},
            ]
        }
        blocks = normalize_document_blocks(summary)
        assert len(blocks) == 2
        assert blocks[0]["text"] == "valid intro"
        assert blocks[1]["text"] == "valid section"

    def test_new_blocks_use_body_fallback_for_text(self):
        summary = {
            "blocks": [
                {"type": "intro", "body": "From body field."},
            ]
        }
        blocks = normalize_document_blocks(summary)
        assert blocks[0]["text"] == "From body field."

    def test_empty_summary_produces_no_blocks(self):
        assert normalize_document_blocks({}) == []
        assert normalize_document_blocks({"body": ""}) == []


class TestNormalizeTopicDocument:
    def test_wraps_normalize_document_blocks(self):
        topic = {
            "summary": {"body": "Hello.", "sections": [{"title": "S1", "body": "World."}]},
            "source_message_ids": ["src1"],
        }
        blocks = normalize_topic_document(topic)
        assert len(blocks) == 2
        assert blocks[0]["text"] == "Hello."
        assert blocks[1]["text"] == "World."

    def test_string_summary_converted_to_body(self):
        topic = {"summary": "Just a string", "source_message_ids": []}
        blocks = normalize_topic_document(topic)
        assert len(blocks) == 1
        assert blocks[0]["text"] == "Just a string"


class TestBlockHelpers:
    def test_block_source_ids_returns_distinct(self):
        block = {"source_message_ids": ["a", "b", "a", "c"]}
        assert block_source_ids(block) == ["a", "b", "c"]

    def test_block_source_ids_empty(self):
        assert block_source_ids({}) == []
        assert block_source_ids({"source_message_ids": None}) == []

    def test_block_media_refs_normalizes(self):
        block = {
            "media_refs": [
                {"message_id": "1", "attachment_index": 0},
                {"message_id": "2", "kind": "embed", "index": 1},
            ]
        }
        refs = block_media_refs(block)
        assert refs == [
            {"message_id": "1", "kind": "attachment", "index": 0},
            {"message_id": "2", "kind": "embed", "index": 1},
        ]

    def test_block_media_refs_empty(self):
        assert block_media_refs({}) == []
        assert block_media_refs({"media_refs": None}) == []

    def test_block_media_refs_with_external(self):
        """External refs are normalized through the same path."""
        block = {
            "media_refs": [
                {"message_id": "1", "kind": "external", "index": 0},
                {"message_id": "2", "kind": "attachment", "index": 0},
            ]
        }
        refs = block_media_refs(block)
        assert refs == [
            {"message_id": "1", "kind": "external", "index": 0},
            {"message_id": "2", "kind": "attachment", "index": 0},
        ]


class TestCollectDocumentSourceIds:
    def test_distinct_union_across_blocks(self):
        blocks = [
            {"source_message_ids": ["a", "b"]},
            {"source_message_ids": ["b", "c"]},
            {"source_message_ids": ["a", "d"]},
        ]
        result = collect_document_source_ids(blocks)
        assert result == ["a", "b", "c", "d"]

    def test_empty_blocks(self):
        assert collect_document_source_ids([]) == []

    def test_preserves_order_of_first_appearance(self):
        blocks = [
            {"source_message_ids": ["z", "a"]},
            {"source_message_ids": ["m", "z"]},
        ]
        assert collect_document_source_ids(blocks) == ["z", "a", "m"]


# ------------------------------------------------------------------
# T10: Rendering tests
# ------------------------------------------------------------------


class TestRenderTopicPublishUnits:
    """Tests for render_topic_publish_units — structured block rendering."""

    def _topic_with_blocks(self, blocks, headline="Test Topic", guild_id=123, source_message_ids=None):
        return {
            "topic_id": "topic-1",
            "headline": headline,
            "guild_id": guild_id,
            "source_message_ids": source_message_ids or [],
            "summary": {"blocks": blocks},
        }

    def _source_meta(self, message_id, guild_id=123, channel_id=456):
        return {
            "message_id": message_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "attachments": [],
            "embeds": [],
        }

    def test_intro_text_renders_with_header_and_inline_citations(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "Hello world.",
                "source_message_ids": ["111"],
            }
        ])
        source_metadata = {"111": self._source_meta("111")}
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        assert len(units) >= 1
        text_unit = units[0]
        assert text_unit["kind"] == "text"
        assert "## Live update: Test Topic" in text_unit["content"]
        assert "Hello world." in text_unit["content"]
        assert "Sources: [1] https://discord.com/channels/123/456/111" in text_unit["content"]

    def test_no_global_source_footer_for_structured_topics(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "No footer test.",
                "source_message_ids": ["111"],
            },
            {
                "type": "section",
                "title": "S1",
                "text": "Section text.",
                "source_message_ids": ["222"],
            },
        ])
        source_metadata = {
            "111": self._source_meta("111"),
            "222": self._source_meta("222"),
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        # Concatenate all text unit contents to search for footer patterns
        all_text = " ".join(u["content"] for u in units if u["kind"] == "text")
        assert "Sources: 111, 222" not in all_text

    def test_section_sources_rendered_inline_next_to_correct_section(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "Intro text.",
                "source_message_ids": ["111"],
            },
            {
                "type": "section",
                "title": "Section A",
                "text": "Content A.",
                "source_message_ids": ["222"],
            },
            {
                "type": "section",
                "title": "Section B",
                "text": "Content B.",
                "source_message_ids": ["333"],
            },
        ])
        source_metadata = {
            "111": self._source_meta("111"),
            "222": self._source_meta("222"),
            "333": self._source_meta("333"),
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        intro_content = units[0]["content"]
        assert "[1] https://discord.com/channels/123/456/111" in intro_content
        assert "222" not in intro_content  # Section A's source not in intro
        sec_a_content = units[1]["content"]
        assert "[1] https://discord.com/channels/123/456/222" in sec_a_content
        assert "111" not in sec_a_content  # Intro's source not in section A
        sec_b_content = units[2]["content"]
        assert "[1] https://discord.com/channels/123/456/333" in sec_b_content

    def test_citations_deduped_and_ordered_per_block(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "Dedup test.",
                "source_message_ids": ["111", "222", "111", "333"],
            },
        ])
        source_metadata = {
            "111": self._source_meta("111"),
            "222": self._source_meta("222"),
            "333": self._source_meta("333"),
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        content = units[0]["content"]
        # 111 should appear before 222 and 333; no duplicate 111
        pos_first = content.find("[1]")
        pos_second = content.find("[2]")
        pos_third = content.find("[3]")
        assert pos_first < pos_second < pos_third
        assert "111" in content
        assert "222" in content
        assert "333" in content
        assert "[4]" not in content

    def test_citation_without_metadata_still_renders_number(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "No metadata.",
                "source_message_ids": ["orphan"],
            },
        ])
        units = render_topic_publish_units(topic, source_metadata={})
        content = units[0]["content"]
        assert "[1] orphan" in content
        # Should NOT have a URL since no metadata
        assert "](https://" not in content or "orphan" not in content.replace("](/", "](")

    def test_media_refs_appear_after_block_text(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "Media test.",
                "source_message_ids": ["111"],
                "media_refs": [{"message_id": "111", "attachment_index": 0}],
            },
        ])
        source_metadata = {
            "111": {
                "message_id": "111",
                "guild_id": 123,
                "channel_id": 456,
                "attachments": [{"url": "https://cdn.example.com/img.png"}],
                "embeds": [],
            },
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        assert len(units) == 2
        assert units[0]["kind"] == "text"
        assert units[1]["kind"] == "media"
        assert units[1]["url"] == "https://cdn.example.com/img.png"
        assert units[1]["ref"]["message_id"] == "111"

    def test_no_media_when_url_not_resolvable(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "No media URL.",
                "media_refs": [{"message_id": "111", "attachment_index": 0}],
            },
        ])
        source_metadata = {"111": self._source_meta("111")}  # no attachments
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        # Only text, no media unit
        assert len(units) == 1
        assert units[0]["kind"] == "text"

    def test_section_with_media_sends_media_after_section(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "Intro.",
            },
            {
                "type": "section",
                "title": "With Media",
                "text": "Section body.",
                "media_refs": [{"message_id": "222", "attachment_index": 0}],
            },
        ])
        source_metadata = {
            "222": {
                "message_id": "222",
                "guild_id": 123,
                "channel_id": 456,
                "attachments": [{"url": "https://cdn.example.com/vid.mp4"}],
                "embeds": [],
            },
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        assert len(units) == 3  # header+intro, section text, section media
        assert units[1]["kind"] == "text"
        assert "With Media" in units[1]["content"]
        assert units[2]["kind"] == "media"
        assert units[2]["url"] == "https://cdn.example.com/vid.mp4"

    def test_duplicate_embed_refs_across_blocks_render_and_send_once(self):
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "Intro cites the embed.",
                "source_message_ids": ["111"],
                "media_refs": [{"message_id": "111", "kind": "embed", "index": 0}],
            },
            {
                "type": "section",
                "title": "Follow-up",
                "text": "The same embed is relevant here too.",
                "source_message_ids": ["111"],
                "media_refs": [{"message_id": "111", "kind": "embed", "index": 0}],
            },
            {
                "type": "section",
                "title": "Wrap",
                "text": "The duplicate should still only publish once.",
                "source_message_ids": ["111"],
                "media_refs": [{"message_id": "111", "kind": "embed", "index": 0}],
            },
        ])
        source_metadata = {
            "111": {
                "message_id": "111",
                "guild_id": 123,
                "channel_id": 456,
                "attachments": [],
                "embeds": [{"url": "https://youtube.com/watch?v=abc123"}],
            },
        }

        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        media_units = [unit for unit in units if unit["kind"] == "media"]
        assert [unit["url"] for unit in media_units] == [
            "https://youtube.com/watch?v=abc123"
        ]

        send_units = []
        _build_send_units(units, send_units, source_metadata)
        assert [
            unit["content"]
            for unit in send_units
            if unit["send_kind"] == "url"
        ] == ["https://youtube.com/watch?v=abc123"]

    def test_media_url_already_in_block_text_is_not_sent_again(self):
        youtube_url = "https://youtube.com/watch?v=abc123"
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": f"Demo video: {youtube_url}",
                "source_message_ids": ["111"],
                "media_refs": [{"message_id": "111", "kind": "embed", "index": 0}],
            },
        ])
        source_metadata = {
            "111": {
                "message_id": "111",
                "guild_id": 123,
                "channel_id": 456,
                "attachments": [],
                "embeds": [{"url": youtube_url}],
            },
        }

        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        assert [unit["kind"] for unit in units] == ["text"]
        assert youtube_url in units[0]["content"]

    def test_fallback_to_legacy_render_when_no_blocks(self):
        topic = {
            "topic_id": "t1",
            "headline": "Simple",
            "guild_id": 123,
            "summary": {"body": "Simple body"},
            "source_message_ids": ["111"],
        }
        units = render_topic_publish_units(topic, source_metadata={})
        assert len(units) == 1
        assert units[0]["kind"] == "text"
        assert "Simple" in units[0]["content"]

    def test_external_media_ref_produces_external_unit_with_original_url(self):
        """External refs produce kind='external' units with the original URL."""
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "External media test.",
                "media_refs": [{"message_id": "111", "kind": "external", "index": 0}],
            },
        ])
        source_metadata = {
            "111": {
                "message_id": "111",
                "guild_id": 123,
                "channel_id": 456,
                "attachments": [],
                "embeds": [],
                "content": "Check this https://x.com/user/status/12345",
            },
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        assert len(units) >= 2
        # First unit is text
        assert units[0]["kind"] == "text"
        # Second unit should be external
        external_unit = units[1]
        assert external_unit["kind"] == "external"
        assert "x.com" in external_unit.get("url", "")
        assert external_unit["ref"]["kind"] == "external"
        assert external_unit["ref"]["index"] == 0

    def test_external_ref_without_url_in_content_produces_no_media_unit(self):
        """When extraction finds no URL at the given index, no media unit is added."""
        topic = self._topic_with_blocks([
            {
                "type": "intro",
                "text": "No URLs here.",
                "media_refs": [{"message_id": "111", "kind": "external", "index": 0}],
            },
        ])
        source_metadata = {
            "111": {
                "message_id": "111",
                "guild_id": 123,
                "channel_id": 456,
                "attachments": [],
                "embeds": [],
                "content": "Just text, no URLs.",
            },
        }
        units = render_topic_publish_units(topic, source_metadata=source_metadata)
        # Only text unit — external ref resolved to None
        assert len(units) == 1
        assert units[0]["kind"] == "text"


class TestResolveMediaUrlFromMetadata:
    """Tests for _resolve_media_url_from_metadata."""

    def test_attachment_url_resolved(self):
        meta = {"attachments": [{"url": "https://cdn.example.com/a.png"}]}
        ref = {"message_id": "1", "kind": "attachment", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) == "https://cdn.example.com/a.png"

    def test_attachment_proxy_url_fallback(self):
        meta = {"attachments": [{"proxy_url": "https://cdn.example.com/b.png"}]}
        ref = {"message_id": "1", "kind": "attachment", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) == "https://cdn.example.com/b.png"

    def test_attachment_index_out_of_range_returns_none(self):
        meta = {"attachments": []}
        ref = {"message_id": "1", "kind": "attachment", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) is None

    def test_embed_url_resolved(self):
        meta = {"embeds": [{"url": "https://example.com"}]}
        ref = {"message_id": "1", "kind": "embed", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) == "https://example.com"

    def test_embed_thumbnail_resolved(self):
        meta = {"embeds": [{"thumbnail": {"url": "https://cdn.example.com/thumb.png"}}]}
        ref = {"message_id": "1", "kind": "embed", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) == "https://cdn.example.com/thumb.png"

    def test_embed_image_resolved(self):
        meta = {"embeds": [{"image": {"url": "https://cdn.example.com/img.png"}}]}
        ref = {"message_id": "1", "kind": "embed", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) == "https://cdn.example.com/img.png"

    def test_embed_index_out_of_range_returns_none(self):
        meta = {"embeds": []}
        ref = {"message_id": "1", "kind": "embed", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) is None

    def test_missing_meta_returns_none(self):
        ref = {"message_id": "1", "kind": "attachment", "index": 0}
        assert _resolve_media_url_from_metadata(ref, {}) is None

    def test_external_kind_returns_original_url_from_content(self):
        """External refs return the original external URL from message content."""
        meta = {
            "attachments": [],
            "embeds": [],
            "content": "Look at this https://x.com/user/status/12345",
        }
        ref = {"message_id": "1", "kind": "external", "index": 0}
        url = _resolve_media_url_from_metadata(ref, meta)
        assert url is not None
        assert "x.com" in url

    def test_external_kind_out_of_range_returns_none(self):
        """External ref with index beyond available URLs returns None."""
        meta = {
            "attachments": [],
            "embeds": [],
            "content": "No URLs here",
        }
        ref = {"message_id": "1", "kind": "external", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) is None

    def test_external_kind_multiple_urls_correct_index(self):
        """External refs use index to select the right URL."""
        meta = {
            "attachments": [],
            "embeds": [],
            "content": (
                "First https://reddit.com/r/test/1 "
                "and second https://x.com/user/status/42"
            ),
        }
        ref0 = {"message_id": "1", "kind": "external", "index": 0}
        ref1 = {"message_id": "1", "kind": "external", "index": 1}
        url0 = _resolve_media_url_from_metadata(ref0, meta)
        url1 = _resolve_media_url_from_metadata(ref1, meta)
        assert url0 is not None and "reddit.com" in url0
        assert url1 is not None and "x.com" in url1
        assert url0 != url1

    def test_external_kind_returns_string_valued_embed_url(self):
        meta = {
            "attachments": [],
            "embeds": [{"url": "https://x.com/user/status/12345"}],
            "content": "",
        }
        ref = {"message_id": "1", "kind": "external", "index": 0}
        assert _resolve_media_url_from_metadata(ref, meta) == "https://x.com/user/status/12345"


class TestChunkTextForDiscord:
    """Tests for chunk_text_for_discord — paragraph-aware chunking."""

    def test_short_text_stays_single_chunk(self):
        result = chunk_text_for_discord("Hello world", limit=2000)
        assert result == ["Hello world"]

    def test_text_at_limit_boundary_stays_single_chunk(self):
        text = "x" * 2000
        result = chunk_text_for_discord(text, limit=2000)
        assert len(result) == 1
        assert result[0] == text

    def test_chunking_only_fires_on_oversized_text(self):
        text = "a" * 1500
        result = chunk_text_for_discord(text, limit=2000)
        assert len(result) == 1

    def test_splits_on_paragraph_boundaries(self):
        para1 = "Paragraph one.\nWith two lines."
        para2 = "Paragraph two."
        text = para1 + "\n\n" + para2
        result = chunk_text_for_discord(text, limit=len(para1) + 1)
        assert len(result) == 2
        assert result[0] == para1
        assert result[1] == para2

    def test_splits_within_oversized_paragraph_on_newlines(self):
        line1 = "Line A " + "x" * 100
        line2 = "Line B " + "y" * 100
        text = line1 + "\n" + line2
        result = chunk_text_for_discord(text, limit=len(line1) + 1)
        assert len(result) == 2
        assert result[0] == line1
        assert result[1] == line2

    def test_hard_splits_individual_long_line(self):
        long_line = "A" * 2500
        result = chunk_text_for_discord(long_line, limit=1000)
        assert len(result) == 3
        for chunk in result:
            assert len(chunk) <= 1000

    def test_multiple_paragraphs_with_mixed_sizes(self):
        p1 = "Short."
        p2 = "M" * 3000
        p3 = "Also short."
        text = p1 + "\n\n" + p2 + "\n\n" + p3
        result = chunk_text_for_discord(text, limit=1000)
        assert result[0] == p1
        # p2 should be split into at least 3 chunks
        assert len(result) >= 4  # p1 + (p2 split into >=3) + p3


# ------------------------------------------------------------------
# Send-unit building tests (T8)
# ------------------------------------------------------------------


class TestBuildSendUnits:
    """Tests for _build_send_units — publishes units into send-unit model."""

    def test_text_unit_becomes_text_send_unit(self):
        units = [{"kind": "text", "content": "Hello world"}]
        out: list = []
        _build_send_units(units, out, {})
        assert len(out) == 1
        assert out[0]["send_kind"] == "text"
        assert out[0]["content"] == "Hello world"

    def test_media_unit_becomes_url_send_unit(self):
        units = [{"kind": "media", "url": "https://cdn.example.com/img.png", "ref": {}}]
        out: list = []
        _build_send_units(units, out, {})
        assert len(out) == 1
        assert out[0]["send_kind"] == "url"
        assert out[0]["content"] == "https://cdn.example.com/img.png"

    def test_discord_media_unit_becomes_file_url_send_unit(self):
        units = [{
            "kind": "media",
            "url": "https://cdn.discordapp.com/attachments/1/2/img.png",
            "ref": {"message_id": "111", "kind": "attachment", "index": 0},
        }]
        out: list = []
        _build_send_units(units, out, {
            "111": {"attachments": [{"filename": "demo image.png"}]},
        })
        assert len(out) == 1
        assert out[0]["send_kind"] == "file_url"
        assert out[0]["source_url"] == "https://cdn.discordapp.com/attachments/1/2/img.png"
        assert out[0]["filename"] == "demo_image.png"

    def test_external_unit_becomes_file_send_unit(self):
        units = [{"kind": "external", "url": "https://x.com/user/status/123", "ref": {"message_id": "1", "kind": "external", "index": 0}}]
        out: list = []
        _build_send_units(units, out, {})
        assert len(out) == 1
        assert out[0]["send_kind"] == "file"
        assert out[0]["fallback_url"] == "https://x.com/user/status/123"
        assert out[0]["ref"]["kind"] == "external"
        assert "pending resolve" in out[0]["trace"]

    def test_mixed_units_produce_correct_order(self):
        units = [
            {"kind": "text", "content": "First paragraph."},
            {"kind": "media", "url": "https://cdn.example.com/a.png", "ref": {}},
            {"kind": "text", "content": "Second paragraph."},
            {"kind": "external", "url": "https://reddit.com/r/test/1", "ref": {"message_id": "1", "kind": "external", "index": 0}},
        ]
        out: list = []
        _build_send_units(units, out, {})
        assert len(out) == 4
        assert out[0]["send_kind"] == "text"
        assert out[1]["send_kind"] == "url"
        assert out[2]["send_kind"] == "text"
        assert out[3]["send_kind"] == "file"


class TestSummarizeSourceMediaCounts:
    """Tests for TopicEditor._summarize_source_media_counts — external_links key."""

    @staticmethod
    def _make_editor():
        """Create a minimal TopicEditor suitable for unit testing."""
        from src.features.summarising.topic_editor import TopicEditor

        class _FakeDB:
            pass

        return TopicEditor(db_handler=_FakeDB(), environment="test")

    def test_returns_separate_external_links_count(self):
        editor = self._make_editor()
        source_metadata = {
            "msg1": {
                "message_id": "msg1",
                "attachments": [{"url": "https://cdn.example.com/a.png"}],
                "embeds": [],
                "content": "Check https://x.com/user/status/123",
            },
            "msg2": {
                "message_id": "msg2",
                "attachments": [],
                "embeds": [{"url": "https://example.com"}],
                "content": "Also https://reddit.com/r/test/1 and https://instagram.com/p/abc",
            },
        }
        result = editor._summarize_source_media_counts(source_metadata)
        assert result["attachments"] == 1
        assert result["embeds"] == 1
        assert result["resolvable_media"] == 2  # 1 attachment + 1 embed
        assert result["external_links"] == 3  # 1 from msg1, 2 from msg2
        assert result["messages_with_media"] == 2

    def test_no_external_links_returns_zero(self):
        editor = self._make_editor()
        source_metadata = {
            "msg1": {
                "message_id": "msg1",
                "attachments": [{"url": "https://cdn.example.com/a.png"}],
                "embeds": [],
                "content": "No external URLs here.",
            },
        }
        result = editor._summarize_source_media_counts(source_metadata)
        assert result["external_links"] == 0
        assert result["attachments"] == 1
        assert result["resolvable_media"] == 1

    def test_external_links_not_merged_into_resolvable_media(self):
        """External links are a separate count, not inflated into resolvable_media."""
        editor = self._make_editor()
        source_metadata = {
            "msg1": {
                "message_id": "msg1",
                "attachments": [],
                "embeds": [],
                "content": "https://x.com/user/status/123 https://reddit.com/r/test/1",
            },
        }
        result = editor._summarize_source_media_counts(source_metadata)
        assert result["attachments"] == 0
        assert result["embeds"] == 0
        assert result["resolvable_media"] == 0  # no Discord-native media
        assert result["external_links"] == 2


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
            source_authors=("gleb",),
        ),
        TopicIdentity(
            topic_id="similar",
            canonical_key="other-key",
            headline="OpenCS2 Counter Strike dataset gets released",
            source_authors=("alice", "bob"),
        ),
        TopicIdentity(
            topic_id="no-author-overlap",
            canonical_key="different-key",
            headline="OpenCS2 Counter Strike dataset gets released",
            source_authors=("carol",),
        ),
    ]

    collisions = detect_topic_collisions(
        proposed_canonical_key="gleb-omninft-lora-2026-05-13",
        headline="OpenCS2 Counter Strike dataset released",
        source_authors=("alice", "bob"),
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
        source_authors=("alice",),
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
