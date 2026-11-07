"""Per-guild TTS pipeline: FIFO queue, conversational overlap, skip/stop.

Messages play in order. Two messages may overlap like real conversation:
  * a *different* person's message that arrived within CROSS_USER_WINDOW_S of
    the previous one cuts in during the last ~OVERLAP_S of it, fading in while
    the earlier voice ducks;
  * the *same* person's messages sent within SAME_USER_WINDOW_S of each other
    are chained with a short crossfade so the voice keeps talking.
The next message is synthesized while the current one plays, so it can start
on time.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import ctypes.util
import itertools
import logging
import time
from dataclasses import dataclass, field, replace
from typing import AsyncIterator, Optional

import discord

from .config_store import ConfigStore
from .fish_client import FishConfig, stream_opus_ogg
from .mixer import FRAME_BYTES, FRAME_MS, Mixer, Track, frames
from .ogg_demux import OggDemuxer
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)

QUEUE_MAXSIZE_DEFAULT = 20
PLAY_TIMEOUT_S = 240  # a 2000-character message is ~2 min of speech
DAVE_READY_TIMEOUT_S = 5.0

CROSS_USER_WINDOW_S = 10.0   # other person replying this soon may talk over the end
SAME_USER_WINDOW_S = 1.0     # same person's rapid messages blend into one stream
OVERLAP_S = 1.2              # how much of the earlier message gets talked over
MIN_LEAD_S = 1.0             # never cut in before the earlier voice has spoken this long
CROSS_FADE_IN_S = 0.6
CROSS_DUCK_GAIN = 0.65
SAME_OVERLAP_S = 0.25
SAME_FADE_IN_S = 0.15

_job_ids = itertools.count(1)


@dataclass
class TTSJob:
    text: str
    user_id: int
    guild_id: int
    voice_channel_id: int
    source: str = "say"          # say | auto | dm | server — for usage reports
    wait_encrypt: bool = True    # wait (max 5 s) for DAVE E2EE keys before the first frame
    enqueued_at: float = field(default_factory=time.monotonic)
    id: int = field(default_factory=lambda: next(_job_ids))


def overlap_mode(prev: TTSJob, nxt: TTSJob) -> str | None:
    """How `nxt` may overlap `prev`: "cross" (another person cutting in),
    "same" (one person's rapid-fire messages) or None (wait for silence)."""
    gap = nxt.enqueued_at - prev.enqueued_at
    if prev.user_id == nxt.user_id:
        return "same" if gap <= SAME_USER_WINDOW_S else None
    return "cross" if gap <= CROSS_USER_WINDOW_S else None


def may_start(prev: Track, mode: str | None) -> bool:
    """Whether the next track may start now, given the previous one."""
    if prev.finished:
        return True
    if mode is None:
        return False
    if mode == "cross" and prev.frames_played < frames(MIN_LEAD_S):
        return False
    remaining = prev.remaining_frames
    if remaining is None:
        return False  # still synthesizing: we don't know where it ends yet
    return remaining <= frames(OVERLAP_S if mode == "cross" else SAME_OVERLAP_S)


async def wait_for_dave_ready(voice_client, timeout: float = DAVE_READY_TIMEOUT_S) -> bool:
    """discord.py sends frames unencrypted while a DAVE session has no key yet, and
    clients drop them. Wait until it can encrypt (True), or give up (False)."""
    conn = getattr(voice_client, "_connection", None)
    if conn is None or getattr(conn, "dave_session", None) is None:
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not getattr(conn, "can_encrypt", True):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _opus_decoder():
    from discord.opus import Decoder, is_loaded, load_opus
    if not is_loaded():
        load_opus(ctypes.util.find_library("opus") or "libopus.so.0")
    return Decoder()


async def fish_pcm_frames(text: str, cfg: FishConfig, track: Track) -> AsyncIterator[bytes]:
    """Fish Ogg/Opus stream -> 20 ms stereo PCM frames."""
    demux = OggDemuxer()
    decoder = _opus_decoder()
    pending = b""

    def decode(pkt: bytes) -> list[bytes]:
        nonlocal pending
        pending += decoder.decode(pkt, fec=False)
        out = []
        while len(pending) >= FRAME_BYTES:
            out.append(pending[:FRAME_BYTES])
            pending = pending[FRAME_BYTES:]
        return out

    async for chunk in stream_opus_ogg(text, cfg):
        track.chunks += 1
        demux.feed(chunk)
        for pkt in demux.packets():
            for f in decode(pkt):
                yield f
    for pkt in demux.flush():
        for f in decode(pkt):
            yield f
    if pending:
        yield pending.ljust(FRAME_BYTES, b"\x00")


class GuildTTSPipeline:
    def __init__(
        self,
        guild_id: int,
        fish_cfg: FishConfig,
        voice_manager: VoiceManager,
        loop: asyncio.AbstractEventLoop,
        config_store: ConfigStore,
        queue_maxsize: int = QUEUE_MAXSIZE_DEFAULT,
        error_hook=None,  # (job, exc) -> None; called when a job fails
        usage_hook=None,  # (job, voice_id, audio_ms, skipped) -> None; per billed request
        synth=None,       # (text, cfg, track) -> async iterator of PCM frames (tests)
    ) -> None:
        self.guild_id = guild_id
        self._fish_cfg = fish_cfg
        self._voice_manager = voice_manager
        self._loop = loop
        self._config_store = config_store
        self._maxsize = queue_maxsize
        self._pending: collections.deque[TTSJob] = collections.deque()
        self._has_pending = asyncio.Event()
        self._tracks: list[Track] = []  # waiting to start + playing
        self._mixer = Mixer()
        self._mixer_running = False
        self._worker: Optional[asyncio.Task] = None
        self._stop_flag = False
        self._error_hook = error_hook
        self._usage_hook = usage_hook
        self._synth = synth or fish_pcm_frames

    # ---- lifecycle ----

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name=f"tts-worker-{self.guild_id}")

    async def stop(self) -> None:
        self._stop_flag = True
        self.stop_all()
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
            self._worker = None

    # ---- queue API ----

    async def enqueue(self, job: TTSJob) -> None:
        if len(self._pending) >= self._maxsize:
            raise asyncio.QueueFull
        self._pending.append(job)
        self._has_pending.set()

    def snapshot(self) -> tuple[list[Track], list[TTSJob]]:
        """(tracks playing or about to start, jobs still waiting)."""
        return list(self._tracks), list(self._pending)

    async def skip_current(self) -> bool:
        playing = [t for t in self._tracks if t.started and not t.finished]
        for t in playing:
            t.stop()
        return bool(playing)

    def stop_user(self, user_id: int) -> int:
        """Stop everything this person has playing or queued. Returns count."""
        n = 0
        for t in self._tracks:
            if t.job.user_id == user_id and not t.finished:
                t.stop(); n += 1
        kept = [j for j in self._pending if j.user_id != user_id]
        n += len(self._pending) - len(kept)
        self._pending = collections.deque(kept)
        return n

    def stop_all(self) -> int:
        n = len(self._pending) + sum(1 for t in self._tracks if not t.finished)
        self._pending.clear()
        for t in self._tracks:
            t.stop()
        return n

    @property
    def queue_size(self) -> int:
        return len(self._pending)

    # ---- worker ----

    async def _next_job(self) -> TTSJob:
        while not self._pending:
            self._has_pending.clear()
            await self._has_pending.wait()
        return self._pending.popleft()

    async def _run(self) -> None:
        prev: Track | None = None
        while not self._stop_flag:
            try:
                job = await self._next_job()
            except asyncio.CancelledError:
                return
            track = Track(job)
            self._tracks.append(track)
            producer = asyncio.create_task(self._produce(job, track), name=f"tts-synth-{job.id}")
            try:
                mode = overlap_mode(prev.job, job) if prev is not None and not prev.finished else None
                while prev is not None and not track.finished and not may_start(prev, mode):
                    await asyncio.sleep(FRAME_MS / 1000)
                # Don't go "on air" until this message has audio (or has failed).
                while not track.finished and track.buffered == 0 and not track.producer_done:
                    await asyncio.sleep(0.01)
                if track.finished:  # stopped (or failed) while waiting its turn
                    asyncio.create_task(self._finish(track, producer), name=f"tts-finish-{job.id}")
                    continue
                await self._start_track(track, prev, mode)
            except asyncio.CancelledError:
                producer.cancel()
                return
            except Exception as e:
                track.error = track.error or e
                track.stop()
                log.exception("TTS job failed")
            asyncio.create_task(self._finish(track, producer), name=f"tts-finish-{job.id}")
            prev = track

    async def _produce(self, job: TTSJob, track: Track) -> None:
        voice_id = self._config_store.get_voice_for(job.user_id)
        track.voice_id = voice_id
        if not voice_id:
            track.error = RuntimeError("no voice configured; run /voice set <id> (or owner: /voice default <id>)")
            track.stop()
            return
        cfg = replace(self._fish_cfg, voice_id=voice_id)
        try:
            async for frame in self._synth(job.text, cfg, track):
                while not track.push(frame):
                    if track.finished:
                        return
                    await asyncio.sleep(0.01)
                if track.finished:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            track.error = e
            log.warning("Fish stream failed for job %s: %r", job.id, e)
            if not track.frames_played:
                track.stop()
        finally:
            track.close()

    def _voice_client(self, job: TTSJob):
        vc = self._voice_manager.get_voice_client_in_channel(job.guild_id, job.voice_channel_id)
        if vc is None:
            raise RuntimeError("bot is not in the requested voice channel; /join required")
        return vc

    async def _start_track(self, track: Track, prev: Track | None, mode: str | None) -> None:
        vc = self._voice_client(track.job)
        if mode == "cross" and prev is not None and not prev.finished:
            track.fade(0.0, 1.0, CROSS_FADE_IN_S)
            prev.fade(None, CROSS_DUCK_GAIN, 0.2)
        elif mode == "same" and prev is not None and not prev.finished:
            track.fade(0.3, 1.0, SAME_FADE_IN_S)
        if not self._mixer_running and track.job.wait_encrypt:
            if not await wait_for_dave_ready(vc):
                log.warning("DAVE E2EE session has no encryption key after %.0fs; listeners will "
                            "not hear audio. Check the installed `davey` version.", DAVE_READY_TIMEOUT_S)
        log.info("TTS start: job=%s user=%s overlap=%s voice=%s", track.job.id, track.job.user_id,
                 mode or "-", getattr(track, "voice_id", None))
        needs_restart = self._mixer.add(track)
        if needs_restart or not self._mixer_running:
            self._ensure_playing(vc)

    def _ensure_playing(self, vc) -> None:
        if self._mixer_running:
            return
        self._mixer.reset()
        self._mixer_running = True

        def after(error: Optional[Exception]) -> None:
            if error is not None:
                log.warning("mixer playback error: %r", error)
            self._loop.call_soon_threadsafe(self._on_mixer_stopped)

        try:
            vc.play(self._mixer, after=after)
        except Exception:
            self._mixer_running = False
            raise

    def _on_mixer_stopped(self) -> None:
        self._mixer_running = False
        live = [t for t in self._mixer.tracks if not t.finished]
        if not live:
            return
        vc = self._voice_manager.get_voice_client(self.guild_id)
        if vc is None or not vc.is_connected():
            for t in live:
                t.stop()
            return
        self._mixer.reset()
        self._ensure_playing(vc)  # a track slipped in while playback was ending

    async def _finish(self, track: Track, producer: asyncio.Task) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PLAY_TIMEOUT_S
        while not track.finished:
            if loop.time() > deadline:
                log.warning("job %s exceeded %ss, stopping", track.job.id, PLAY_TIMEOUT_S)
                track.stop()
                break
            await asyncio.sleep(0.05)
        producer.cancel()
        with contextlib.suppress(BaseException):
            await producer
        log.info("TTS done: job=%s %d frames (%.1fs)%s", track.job.id, track.frames_played,
                 track.frames_played * FRAME_MS / 1000, " [stopped]" if track.stopped else "")
        self._discard(track, None)
        if track.chunks > 0 and self._usage_hook is not None:
            try:  # Fish bills the request once it starts streaming, even if skipped
                self._usage_hook(track.job, getattr(track, "voice_id", None),
                                 track.frames_played * FRAME_MS, track.stopped)
            except Exception:
                log.exception("usage hook failed")
        if track.error is not None and self._error_hook is not None:
            try:
                self._error_hook(track.job, track.error)
            except Exception:
                log.exception("TTS error hook failed")

    def _discard(self, track: Track, producer: Optional[asyncio.Task]) -> None:
        if track in self._tracks:
            self._tracks.remove(track)


class PipelineRegistry:
    def __init__(
        self,
        fish_cfg: FishConfig,
        voice_manager: VoiceManager,
        loop: asyncio.AbstractEventLoop,
        config_store: ConfigStore,
        synth=None,
    ) -> None:
        self._fish_cfg = fish_cfg
        self._voice_manager = voice_manager
        self._loop = loop
        self._config_store = config_store
        self._synth = synth
        self._pipelines: dict[int, GuildTTSPipeline] = {}
        self.error_hook = None  # set by bot.py (ServerNotifier)
        self.usage_hook = None  # set by bot.py (usage tracking)

    def get(self, guild_id: int) -> GuildTTSPipeline | None:
        return self._pipelines.get(guild_id)

    def all(self) -> list[GuildTTSPipeline]:
        return list(self._pipelines.values())

    def get_or_create(self, guild_id: int) -> GuildTTSPipeline:
        p = self._pipelines.get(guild_id)
        if p is None:
            p = GuildTTSPipeline(
                guild_id, self._fish_cfg, self._voice_manager,
                self._loop, self._config_store,
                error_hook=lambda job, exc: self.error_hook and self.error_hook(job, exc),
                usage_hook=lambda *a: self.usage_hook and self.usage_hook(*a),
                synth=self._synth,
            )
            self._pipelines[guild_id] = p
            asyncio.create_task(p.start())
        return p

    async def shutdown_all(self) -> None:
        for p in list(self._pipelines.values()):
            await p.stop()
        self._pipelines.clear()
