"""Per-guild TTS pipeline: FIFO queue, dedicated worker, skip semantics."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Optional

import discord

from .audio_source import StreamingOpusAudioSource
from .fish_client import FishConfig, stream_opus_ogg
from .ogg_demux import OggDemuxer
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)

QUEUE_MAXSIZE_DEFAULT = 20
PLAY_TIMEOUT_S = 120
FRAME_QUEUE_MAXSIZE = 200


@dataclass
class TTSJob:
    text: str
    user_id: int
    guild_id: int
    voice_channel_id: int


class GuildTTSPipeline:
    def __init__(
        self,
        guild_id: int,
        fish_cfg: FishConfig,
        voice_manager: VoiceManager,
        loop: asyncio.AbstractEventLoop,
        queue_maxsize: int = QUEUE_MAXSIZE_DEFAULT,
    ) -> None:
        self.guild_id = guild_id
        self._fish_cfg = fish_cfg
        self._voice_manager = voice_manager
        self._loop = loop
        self._queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._stop_flag = False

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name=f"tts-worker-{self.guild_id}")

    async def stop(self) -> None:
        self._stop_flag = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
            self._worker = None

    async def enqueue(self, job: TTSJob) -> None:
        self._queue.put_nowait(job)  # raises asyncio.QueueFull on overflow

    async def skip_current(self) -> bool:
        t = self._current_task
        if t and not t.done():
            t.cancel()
            return True
        return False

    async def _run(self) -> None:
        while not self._stop_flag:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                self._current_task = asyncio.create_task(
                    self._play_one(job), name=f"tts-job-{self.guild_id}"
                )
                await self._current_task
            except asyncio.CancelledError:
                # /skip path
                pass
            except Exception:
                log.exception("TTS job failed")
            finally:
                self._current_task = None
                self._queue.task_done()

    async def _play_one(self, job: TTSJob) -> None:
        voice_client = await self._voice_manager.ensure_in_channel(
            job.guild_id, job.voice_channel_id
        )

        frame_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=FRAME_QUEUE_MAXSIZE
        )
        demux = OggDemuxer()
        source = StreamingOpusAudioSource(frame_queue, self._loop)

        async def producer() -> None:
            try:
                async for chunk in stream_opus_ogg(job.text, self._fish_cfg):
                    demux.feed(chunk)
                    for pkt in demux.packets():
                        await frame_queue.put(pkt)
                for pkt in demux.flush():
                    await frame_queue.put(pkt)
            finally:
                with contextlib.suppress(asyncio.QueueFull):
                    frame_queue.put_nowait(None)

        producer_task = asyncio.create_task(producer(), name="tts-producer")

        play_done = asyncio.Event()

        def after(error: Optional[Exception]) -> None:
            if error is not None:
                log.warning("voice_client.play after-error: %r", error)
            self._loop.call_soon_threadsafe(play_done.set)

        try:
            voice_client.play(source, after=after)
            await asyncio.wait_for(play_done.wait(), timeout=PLAY_TIMEOUT_S)
        finally:
            producer_task.cancel()
            with contextlib.suppress(BaseException):
                await producer_task
            try:
                if voice_client.is_playing():
                    voice_client.stop()
            except Exception:
                pass


class PipelineRegistry:
    def __init__(
        self,
        fish_cfg: FishConfig,
        voice_manager: VoiceManager,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._fish_cfg = fish_cfg
        self._voice_manager = voice_manager
        self._loop = loop
        self._pipelines: dict[int, GuildTTSPipeline] = {}

    def get_or_create(self, guild_id: int) -> GuildTTSPipeline:
        p = self._pipelines.get(guild_id)
        if p is None:
            p = GuildTTSPipeline(guild_id, self._fish_cfg, self._voice_manager, self._loop)
            self._pipelines[guild_id] = p
            asyncio.create_task(p.start())
        return p

    async def shutdown_all(self) -> None:
        for p in list(self._pipelines.values()):
            await p.stop()
        self._pipelines.clear()
