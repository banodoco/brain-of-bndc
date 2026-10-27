import asyncio
from types import SimpleNamespace

import pytest

from src.features.grants.delivery_evidence import collect_delivery_evidence


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


class Channel:
    def __init__(self, message=None, error=None, bot_can_read=True, applicant_can_read=True, public_can_read=True):
        self.message = message
        self.error = error
        self.bot_can_read = bot_can_read
        self.applicant_can_read = applicant_can_read
        self.public_can_read = public_can_read
        self.fetches = []

    def permissions_for(self, member):
        if member.id == 999:
            return SimpleNamespace(view_channel=self.bot_can_read, read_message_history=self.bot_can_read)
        if member.id == -1:
            return SimpleNamespace(view_channel=self.public_can_read, read_message_history=self.public_can_read)
        return SimpleNamespace(view_channel=self.applicant_can_read, read_message_history=self.applicant_can_read)

    async def fetch_message(self, message_id):
        self.fetches.append(message_id)
        if self.error:
            raise self.error
        return self.message


def setup_source(content, channel, guild_id=42):
    applicant = SimpleNamespace(id=7)
    bot_member = SimpleNamespace(id=999)
    guild = SimpleNamespace(
        id=guild_id, me=bot_member, default_role=SimpleNamespace(id=-1),
        get_channel_or_thread=lambda channel_id: channel if channel_id == 11 else None,
        get_channel=lambda channel_id: None,
        get_member=lambda member_id: applicant if member_id == applicant.id else None,
    )
    return SimpleNamespace(content=content, guild=guild, author=applicant)


def test_collects_same_guild_discord_message_and_bounded_metadata():
    linked = SimpleNamespace(
        id=33, guild=SimpleNamespace(id=42), channel=SimpleNamespace(id=11),
        author=SimpleNamespace(id=8), content="Delivered the requested change.",
        attachments=[SimpleNamespace(id=5, filename="proof.png", content_type="image/png", size=123, url="https://cdn.example/proof.png")],
        embeds=[SimpleNamespace(title="Build", description="Passed", url="https://github.com/org/repo")],
    )
    channel = Channel(linked)
    source = setup_source("Progress: https://discord.com/channels/42/11/33", channel)
    evidence = asyncio.run(collect_delivery_evidence(None, source))

    assert len(evidence) == 1
    item = evidence[0]
    assert item["id"] == "discord:42:11:33"
    assert item["status"] == "available"
    assert item["author_id"] == "8"
    assert item["content"] == linked.content
    assert item["attachments"][0]["inspected"] is False
    assert item["attachments"][0]["filename"] == "proof.png"
    assert item["embeds"][0]["title"] == "Build"
    assert channel.fetches == [33]


def test_never_fetches_foreign_guild_link():
    channel = Channel(SimpleNamespace())
    source = setup_source("https://discord.com/channels/77/11/33", channel)
    evidence = asyncio.run(collect_delivery_evidence(None, source))
    assert evidence[0]["status"] == "unavailable"
    assert evidence[0]["error"] == "foreign_guild"
    assert channel.fetches == []


@pytest.mark.parametrize("bot_read,applicant_read,error", [
    (False, True, "bot_cannot_read_channel"),
    (True, False, "applicant_cannot_read_channel"),
])
def test_permission_boundary_prevents_fetch(bot_read, applicant_read, error):
    channel = Channel(SimpleNamespace(), bot_can_read=bot_read, applicant_can_read=applicant_read)
    source = setup_source("https://discord.com/channels/42/11/33", channel)
    evidence = asyncio.run(collect_delivery_evidence(None, source))
    assert evidence[0]["error"] == error
    assert channel.fetches == []


def test_private_channel_is_not_collected_even_when_applicant_and_bot_can_read():
    channel = Channel(SimpleNamespace(), public_can_read=False)
    source = setup_source("https://discord.com/channels/42/11/33", channel)
    evidence = asyncio.run(collect_delivery_evidence(None, source))
    assert evidence[0]["status"] == "unavailable"
    assert evidence[0]["error"] == "channel_not_publicly_readable"
    assert channel.fetches == []


def test_private_thread_is_rejected_even_when_parent_permissions_look_public():
    class PrivateThread(Channel):
        def is_private(self):
            return True

    channel = PrivateThread(SimpleNamespace(), public_can_read=True)
    source = setup_source("https://discord.com/channels/42/11/33", channel)

    evidence = asyncio.run(collect_delivery_evidence(None, source))

    assert evidence[0]["status"] == "unavailable"
    assert evidence[0]["error"] == "private_thread_not_public"
    assert channel.fetches == []


@pytest.mark.parametrize("exception,expected", [(Forbidden(), "forbidden"), (NotFound(), "not_found")])
def test_fetch_errors_are_explicit_and_nonfatal(exception, expected):
    channel = Channel(error=exception)
    source = setup_source("https://discord.com/channels/42/11/33", channel)
    evidence = asyncio.run(collect_delivery_evidence(None, source))
    assert evidence[0]["status"] == "unavailable"
    assert evidence[0]["error"] == expected


def test_external_github_youtube_are_retained_without_fetch_and_other_urls_ignored():
    source = setup_source(
        "See https://github.com/acme/project and https://youtu.be/abc plus https://example.com/private",
        Channel(),
    )
    evidence = asyncio.run(collect_delivery_evidence(None, source))
    assert [item["status"] for item in evidence] == ["unverified_reference", "unverified_reference"]
    assert [item["reference_type"] for item in evidence] == ["external", "external"]
    assert all(item["content"] == "" and not item["attachments"] for item in evidence)


def test_attachment_only_update_emits_metadata_only_source_evidence():
    source = setup_source("", Channel())
    source.id = 88
    source.channel = SimpleNamespace(id=11)
    source.jump_url = "https://discord.com/channels/42/11/88"
    source.attachments = [SimpleNamespace(
        id=9, filename="results.zip", content_type="application/zip", size=500,
        url="https://cdn.example/results.zip",
    )]

    evidence = asyncio.run(collect_delivery_evidence(None, source))

    assert evidence == [{
        "id": "source_attachment:88",
        "url": source.jump_url,
        "status": "unverified_reference",
        "author_id": "7",
        "content": "",
        "attachments": [{
            "id": "9", "filename": "results.zip", "content_type": "application/zip",
            "size": 500, "url": "https://cdn.example/results.zip", "inspected": False,
        }],
        "embeds": [],
        "reference_type": "source_attachment",
    }]


def test_link_and_content_limits_and_malformed_input_do_not_crash():
    urls = " ".join(f"https://github.com/org/repo{i}" for i in range(20))
    source = setup_source("https://discord.com/channels/nope/11/33 " + urls, Channel())
    evidence = asyncio.run(collect_delivery_evidence(None, source))
    assert len(evidence) == 12  # malformed Discord link is explicit; links are bounded
    assert evidence[0]["error"] == "malformed_link"
    assert all(len(item["content"]) <= 4_000 for item in evidence)
