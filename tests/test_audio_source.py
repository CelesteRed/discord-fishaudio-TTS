import asyncio
import pytest
from discord_fish_tts.audio_source import (
    StreamingOpusAudioSource, SILENCE_OPUS,
)


@pytest.mark.asyncio
async def test_read_returns_packets_in_order():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(b"pkt1")
    await queue.put(b"pkt2")
    await queue.put(None)  # EOF

    source = StreamingOpusAudioSource(queue, loop)
    # Run read() on a thread to honor cross-thread contract
    a = await asyncio.to_thread(source.read)
    b = await asyncio.to_thread(source.read)
    eof = await asyncio.to_thread(source.read)

    assert a == b"pkt1"
    assert b == b"pkt2"
    assert eof == b""


@pytest.mark.asyncio
async def test_read_returns_silence_on_underrun():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    source = StreamingOpusAudioSource(queue, loop)
    # Empty queue → underrun → silence
    out = await asyncio.to_thread(source.read)
    assert out == SILENCE_OPUS


@pytest.mark.asyncio
async def test_read_returns_eof_after_none():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(None)
    source = StreamingOpusAudioSource(queue, loop)
    eof = await asyncio.to_thread(source.read)
    assert eof == b""
    # Subsequent reads also EOF
    eof2 = await asyncio.to_thread(source.read)
    assert eof2 == b""


def test_is_opus_true():
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.new_event_loop()
    try:
        source = StreamingOpusAudioSource(queue, loop)
        assert source.is_opus() is True
    finally:
        loop.close()
