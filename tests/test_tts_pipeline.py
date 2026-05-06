import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord_fish_tts.tts_pipeline import (
    GuildTTSPipeline, PipelineRegistry, TTSJob,
)
from discord_fish_tts.fish_client import FishConfig


@pytest.fixture
def fish_cfg():
    return FishConfig(api_key="x", voice_id="y")


def make_voice_mgr():
    vm = MagicMock()
    vc = MagicMock()
    vc.is_playing = MagicMock(return_value=False)
    vc.play = MagicMock()
    vc.stop = MagicMock()
    vm.ensure_in_channel = AsyncMock(return_value=vc)
    vm._vc = vc
    return vm


@pytest.mark.asyncio
async def test_jobs_run_in_fifo_order(fish_cfg):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop)

    played: list[str] = []

    async def fake_stream(text, cfg):
        played.append(text)
        # Yield one fake chunk so producer terminates
        yield b""

    # Simulate voice_client.play immediately calling 'after'
    def play(source, after):
        loop.call_soon(after, None)
    vm._vc.play = MagicMock(side_effect=play)

    with patch("discord_fish_tts.tts_pipeline.stream_opus_ogg", fake_stream):
        await pipeline.start()
        try:
            await pipeline.enqueue(TTSJob("first", 1, 1, 1))
            await pipeline.enqueue(TTSJob("second", 1, 1, 1))
            await pipeline.enqueue(TTSJob("third", 1, 1, 1))
            # Wait until queue drains
            await asyncio.wait_for(pipeline._queue.join(), timeout=5.0)
        finally:
            await pipeline.stop()

    assert played == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_skip_cancels_current(fish_cfg):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_stream(text, cfg):
        started.set()
        try:
            await asyncio.sleep(10)
            yield b""
        except asyncio.CancelledError:
            cancelled.set()
            raise

    # play() never auto-completes; we'll skip
    vm._vc.play = MagicMock()

    with patch("discord_fish_tts.tts_pipeline.stream_opus_ogg", slow_stream):
        await pipeline.start()
        try:
            await pipeline.enqueue(TTSJob("hang", 1, 1, 1))
            await asyncio.wait_for(started.wait(), timeout=2.0)
            ok = await pipeline.skip_current()
            assert ok is True
            await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        finally:
            await pipeline.stop()


@pytest.mark.asyncio
async def test_skip_returns_false_when_idle(fish_cfg):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop)
    await pipeline.start()
    try:
        ok = await pipeline.skip_current()
        assert ok is False
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_registry_lazy_create(fish_cfg):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    reg = PipelineRegistry(fish_cfg, vm, loop)
    p1 = reg.get_or_create(1)
    p2 = reg.get_or_create(1)
    p3 = reg.get_or_create(2)
    assert p1 is p2
    assert p1 is not p3
    await reg.shutdown_all()
