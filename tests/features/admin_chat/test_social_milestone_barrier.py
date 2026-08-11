"""Tests for the social-milestone turn barrier in the admin-chat agent.

Covers the executor-level guard that develop/approve/publish must each be
their own turn (a single model response cannot chain approve→publish), plus
the synthesized confirmation so the admin never sees silence after a
milestone.
"""

from __future__ import annotations

from src.features.admin_chat.agent import (
    _SOCIAL_MILESTONE_TOOLS,
    _milestone_confirmation,
)


class TestMilestoneToolSet:
    """The milestone allowlist is exactly develop/approve/publish."""

    def test_milestone_tools_are_the_three_terminal_actions(self):
        assert _SOCIAL_MILESTONE_TOOLS == {
            "develop_social_proposal",
            "approve_social_draft",
            "publish_social_draft",
        }

    def test_edit_is_not_a_milestone(self):
        # update_social_draft must NOT be a milestone — the admin iterates
        # freely on drafts within one turn; only approval/publish boundaries
        # require a fresh turn.
        assert "update_social_draft" not in _SOCIAL_MILESTONE_TOOLS
        assert "discard_social_draft" not in _SOCIAL_MILESTONE_TOOLS
        assert "preview_social_draft" not in _SOCIAL_MILESTONE_TOOLS


class TestMilestoneConfirmation:
    """The barrier synthesizes a visible confirmation per milestone type."""

    def test_develop_confirmation_includes_theme(self):
        text = _milestone_confirmation([
            {"tool": "reply", "result": {"success": True}},
            {"tool": "develop_social_proposal", "result": {
                "success": True,
                "proposal_theme": "Wan reel",
            }},
        ])
        assert text is not None
        assert "Developed" in text
        assert "Wan reel" in text

    def test_approve_confirmation_asks_for_post(self):
        text = _milestone_confirmation([
            {"tool": "approve_social_draft", "result": {"success": True}},
        ])
        assert text is not None
        assert "Approved" in text
        assert "post it" in text

    def test_publish_confirmation_includes_url(self):
        text = _milestone_confirmation([
            {"tool": "publish_social_draft", "result": {
                "success": True,
                "tweet_url": "https://x.com/banodoco/status/1",
            }},
        ])
        assert text is not None
        assert "Published" in text
        assert "https://x.com/banodoco/status/1" in text

    def test_no_milestone_returns_none(self):
        assert _milestone_confirmation([
            {"tool": "update_social_draft", "result": {"success": True}},
        ]) is None

    def test_failed_milestone_returns_none(self):
        assert _milestone_confirmation([
            {"tool": "publish_social_draft", "result": {"success": False}},
        ]) is None
