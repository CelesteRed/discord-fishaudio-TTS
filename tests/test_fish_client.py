import asyncio
import msgpack
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from discord_fish_tts.fish_client import (
    FishConfig, stream_opus_ogg,
    FishProtocolError, FishTimeoutError,
)


@pytest.fixture
def cfg():
    return FishConfig(
        api_key="test-key",
        voice_id="test-voice",
        first_audio_timeout=0.5,
        idle_timeout=0.5,
        connect_timeout=1.0,
    )


class FakeWS:
    """Minimal async-iterable, async-context-manager WebSocket double."""

    def __init__(self, server_msgs: list[bytes]):
        self._server_msgs = list(server_msgs)
        self.sent: list[bytes] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        async def gen():
            for m in self._server_msgs:
                yield m
        return gen()


def make_audio_msg(payload: bytes) -> bytes:
    return msgpack.packb({"event": "audio", "audio": payload}, use_bin_type=True)


def make_finish_ok() -> bytes:
    return msgpack.packb({"event": "finish", "reason": "stop"}, use_bin_type=True)


def make_finish_err(message: str) -> bytes:
    return msgpack.packb(
        {"event": "finish", "reason": "error", "message": message},
        use_bin_type=True,
    )


@pytest.mark.asyncio
async def test_stream_yields_audio_then_finishes(cfg):
    fake = FakeWS([
        make_audio_msg(b"chunk-1"),
        make_audio_msg(b"chunk-2"),
        make_finish_ok(),
    ])
    with patch("discord_fish_tts.fish_client.websockets.connect", return_value=fake):
        chunks = [c async for c in stream_opus_ogg("hello", cfg)]
    assert chunks == [b"chunk-1", b"chunk-2"]


@pytest.mark.asyncio
async def test_stream_sends_msgpack_start_text_stop(cfg):
    fake = FakeWS([make_finish_ok()])
    with patch("discord_fish_tts.fish_client.websockets.connect", return_value=fake):
        async for _ in stream_opus_ogg("hi there", cfg):
            pass

    assert len(fake.sent) == 3
    start = msgpack.unpackb(fake.sent[0], raw=False)
    text = msgpack.unpackb(fake.sent[1], raw=False)
    stop = msgpack.unpackb(fake.sent[2], raw=False)
    assert start == {
        "event": "start",
        "request": {
            "text": "",
            "reference_id": "test-voice",
            "format": "opus",
            "latency": "low",
        },
    }
    assert text == {"event": "text", "text": "hi there"}
    assert stop == {"event": "stop"}


@pytest.mark.asyncio
async def test_stream_raises_on_finish_error(cfg):
    fake = FakeWS([make_finish_err("voice not found")])
    with patch("discord_fish_tts.fish_client.websockets.connect", return_value=fake):
        with pytest.raises(FishProtocolError, match="voice not found"):
            async for _ in stream_opus_ogg("x", cfg):
                pass


@pytest.mark.asyncio
async def test_stream_raises_first_audio_timeout(cfg):
    """Server connects but never sends audio."""

    class HangingWS(FakeWS):
        def __aiter__(self):
            async def gen():
                # Hang forever
                await asyncio.sleep(10)
                yield b""
            return gen()

    fake = HangingWS([])
    with patch("discord_fish_tts.fish_client.websockets.connect", return_value=fake):
        with pytest.raises(FishTimeoutError):
            async for _ in stream_opus_ogg("x", cfg):
                pass
