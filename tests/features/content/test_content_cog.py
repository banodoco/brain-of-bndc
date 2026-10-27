import asyncio
from types import SimpleNamespace

import discord
import pytest

from src.features.content.content_cog import ContentCog, ContentSegment


class FakeMessage:
    _ids = 1

    def __init__(self, content):
        self.id = FakeMessage._ids
        FakeMessage._ids += 1
        self.content = content
        self.author = SimpleNamespace(id=42)
        self.attachments = []
        self.deleted = False

    async def edit(self, content):
        self.content = content

    async def delete(self):
        self.deleted = True


class FakeThread:
    def __init__(self, messages):
        self.messages = messages
        self.archived = False
        self.locked = False
        self.sent = []
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)

    def history(self, **kwargs):
        async def iterate():
            for message in self.messages:
                if not message.deleted:
                    yield message
        return iterate()

    async def send(self, content, **kwargs):
        message = FakeMessage(content)
        self.messages.append(message)
        self.sent.append(message)
        return message


@pytest.fixture(autouse=True)
def no_discord_rate_limit_sleep(monkeypatch):
    async def no_sleep(_):
        pass
    monkeypatch.setattr('src.features.content.content_cog.asyncio.sleep', no_sleep)


def test_forum_guide_insertion_and_removal_reuses_ordered_slots():
    async def run():
        original = [ContentSegment(f"Section {letter}") for letter in 'ABC']
        packed_original = ContentCog._pack_forum_messages(original)
        thread = FakeThread([FakeMessage(segment.text) for segment in packed_original])
        cog = ContentCog(SimpleNamespace(db_handler=None, user=SimpleNamespace(id=42)))

        inserted = [ContentSegment('Section A'), ContentSegment('New section'),
                    ContentSegment('Section B'), ContentSegment('Section C')]
        ids = await cog._edit_forum_thread(thread, inserted)
        live = [m for m in thread.messages if not m.deleted]
        assert len(live) == 3
        assert '\n\n'.join(part for msg in live for part in msg.content.split('\n\n')) == \
            'Section A\n\nNew section\n\nSection B\n\nSection C'
        assert len(ids) == 3

    # Repeating the same desired state is idempotent and creates nothing.
        ids_again = await cog._edit_forum_thread(thread, inserted)
        assert ids_again == ids
        assert len(thread.sent) == 0

        removed = [ContentSegment('Section A'), ContentSegment('Section C')]
        await cog._edit_forum_thread(thread, removed)
        assert [m.content for m in thread.messages if not m.deleted] == [
            'Section A', 'Section C'
        ]

    asyncio.run(run())


def test_forum_messages_are_packed_in_order_under_discord_limit():
    segments = [ContentSegment('A' * 1200), ContentSegment('B' * 900), ContentSegment('C')]
    packed = ContentCog._pack_forum_messages(segments, max_messages=2)
    assert packed is not None
    assert all(len(segment.text) <= 2000 for segment in packed)
    assert '\n\n'.join(part for msg in packed for part in msg.text.split('\n\n')) == \
        '\n\n'.join(segment.text for segment in segments)


def test_forum_packing_preserves_all_existing_slots_and_fails_closed_on_overflow():
    unchanged = [ContentSegment(f"Section {i}") for i in range(9)]
    assert ContentCog._pack_forum_messages(
        unchanged, max_messages=9, existing_texts=[segment.text for segment in unchanged]
    ) == unchanged
    too_large_to_merge = [ContentSegment('A' * 1500), ContentSegment('B' * 1500)]
    assert ContentCog._pack_forum_messages(too_large_to_merge, max_messages=1) is None


def test_forum_overflow_does_not_unlock_archived_thread():
    async def run():
        thread = FakeThread([FakeMessage('A' * 1500)])
        thread.archived = True
        thread.locked = True
        cog = ContentCog(SimpleNamespace(db_handler=None, user=SimpleNamespace(id=42)))
        result = await cog._edit_forum_thread(
            thread, [ContentSegment('A' * 1500), ContentSegment('B' * 1500)]
        )
        assert result == []
        assert thread.archived and thread.locked
        assert thread.edits == []
    asyncio.run(run())


def test_tracked_forum_thread_transient_fetch_error_does_not_fall_through_to_create():
    response = SimpleNamespace(status=503, reason='Unavailable')

    class Bot:
        user = SimpleNamespace(id=42)

        async def fetch_channel(self, _):
            raise discord.HTTPException(response, 'temporary failure')

    cog = ContentCog(Bot())
    forum = SimpleNamespace(id=100, threads=[])
    async def run():
        with pytest.raises(discord.HTTPException):
            await cog._find_forum_thread(forum, 'Guide', {'thread_id': 123})
    asyncio.run(run())
