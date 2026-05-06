import queue as _queue
import pytest
from discord_fish_tts.audio_source import (
    StreamingOpusAudioSource, SILENCE_OPUS,
)


def test_read_returns_packets_in_order():
    q: _queue.Queue = _queue.Queue()
    q.put(b"pkt1")
    q.put(b"pkt2")
    q.put(None)  # EOF

    source = StreamingOpusAudioSource(q)
    assert source.read() == b"pkt1"
    assert source.read() == b"pkt2"
    assert source.read() == b""


def test_read_returns_silence_on_underrun():
    q: _queue.Queue = _queue.Queue()
    source = StreamingOpusAudioSource(q)
    assert source.read() == SILENCE_OPUS


def test_read_returns_eof_after_none():
    q: _queue.Queue = _queue.Queue()
    q.put(None)
    source = StreamingOpusAudioSource(q)
    assert source.read() == b""
    # Subsequent reads also EOF, even if more items appear (defensive)
    q.put(b"should-not-emit")
    assert source.read() == b""


def test_is_opus_true():
    q: _queue.Queue = _queue.Queue()
    source = StreamingOpusAudioSource(q)
    assert source.is_opus() is True


def test_underrun_does_not_consume_future_packet():
    """Regression test: previous implementation used asyncio.run_coroutine_threadsafe
    with a 15ms timeout on get(), which left an orphaned coroutine that
    would silently consume the next packet. This test guards that fix.
    """
    q: _queue.Queue = _queue.Queue()
    source = StreamingOpusAudioSource(q)
    # First read on empty queue: underrun
    assert source.read() == SILENCE_OPUS
    # Now push a packet — the next read MUST get it (not have it eaten by
    # an orphan from the previous read).
    q.put(b"first-real-packet")
    assert source.read() == b"first-real-packet"
