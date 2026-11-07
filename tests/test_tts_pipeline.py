from __future__ import annotations

import asyncio
import audioop
import threading
import time
from unittest.mock import MagicMock

import pytest

from discord_fish_tts import tts_pipeline as tp
from discord_fish_tts.config_store import ConfigStore
from discord_fish_tts.fish_client import FishConfig
from discord_fish_tts.mixer import FRAME_BYTES, SILENCE, Mixer, Track, frames
from discord_fish_tts.tts_pipeline import GuildTTSPipeline, TTSJob, may_start, overlap_mode


def pcm(value: int) -> bytes:
    return value.to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)


# ---- mixer ----

def test_mixer_sums_tracks_and_ends_when_empty():
    m = Mixer()
    a, b = Track(), Track()
    a.push(pcm(1000)); a.close()
    b.push(pcm(500)); b.push(pcm(500)); b.close()
    m.add(a); m.add(b)
    assert audioop.getsample(m.read(), 2, 0) == 1500
    assert audioop.getsample(m.read(), 2, 0) == 500
    assert m.read() == b""            # all done -> playback ends
    assert m.ended and a.finished and b.finished


def test_mixer_underrun_gives_silence_not_end():
    m = Mixer()
    t = Track()
    m.add(t)
    assert m.read() == SILENCE        # still synthesizing
    t.push(pcm(7)); t.close()
    assert audioop.getsample(m.read(), 2, 0) == 7


def test_track_fade_in_ramps_gain():
    t = Track()
    for _ in range(5):
        t.push(pcm(10000))
    t.fade(0.0, 1.0, 0.1)             # 5 frames
    samples = [audioop.getsample(t.read_frame(), 2, 0) for _ in range(5)]
    assert samples == sorted(samples) and samples[0] < 3000 and samples[-1] == 10000


def test_stopped_track_is_dropped():
    m = Mixer()
    t = Track()
    t.push(pcm(1)); m.add(t)
    t.stop()
    assert m.read() == b""


# ---- overlap rules ----

def job(uid, at, text="x"):
    return TTSJob(text, uid, 1, 1, enqueued_at=at)


def test_overlap_mode_windows():
    assert overlap_mode(job(1, 0), job(2, 9.5)) == "cross"
    assert overlap_mode(job(1, 0), job(2, 10.5)) is None
    assert overlap_mode(job(1, 0), job(1, 0.8)) == "same"
    assert overlap_mode(job(1, 0), job(1, 2.0)) is None


def test_may_start_waits_for_lead_and_known_end():
    prev = Track()
    for _ in range(200):
        prev.push(pcm(1))
    assert not may_start(prev, None)
    assert not may_start(prev, "cross")              # still synthesizing, end unknown
    prev.close()
    assert not may_start(prev, "cross")              # 200 frames (4 s) left
    for _ in range(200 - frames(tp.OVERLAP_S)):
        prev.read_frame()
    assert may_start(prev, "cross")                  # last 1.2 s
    fresh = Track(); fresh.push(pcm(1)); fresh.close()
    assert not may_start(fresh, "cross")             # hasn't spoken MIN_LEAD_S yet
    assert may_start(fresh, "same")


# ---- pipeline with a fake voice connection ----

class FakeVC:
    """Pulls from the AudioSource on a thread like discord.py's AudioPlayer."""

    def __init__(self, tick=0.002):
        self.tick = tick
        self.snapshots: list[list[str]] = []
        self.plays = 0
        self._playing = False
        self._connection = MagicMock(dave_session=None)

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing

    def play(self, source, after):
        assert not self._playing, "play() while already playing"
        self.plays += 1
        self._playing = True

        def run():
            while True:
                self.snapshots.append([t.job.text for t in source.tracks if not t.finished])
                if not source.read():
                    break
                time.sleep(self.tick)
            self._playing = False
            after(None)
        threading.Thread(target=run, daemon=True).start()


def make_pipeline(tmp_path, seconds_per_job=1.0, synth_delay=0.0):
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.set_voice_id("voice")
    vc = FakeVC()
    vm = MagicMock()
    vm.get_voice_client_in_channel = MagicMock(return_value=vc)
    vm.get_voice_client = MagicMock(return_value=vc)
    usage: list[tuple] = []

    async def synth(text, fish_cfg, track):
        track.chunks += 1
        if synth_delay:
            await asyncio.sleep(synth_delay)
        for _ in range(frames(seconds_per_job)):
            yield pcm(100)

    p = GuildTTSPipeline(1, FishConfig(api_key="k", voice_id=""), vm, asyncio.get_running_loop(), cfg,
                         usage_hook=lambda *a: usage.append(a), synth=synth)
    return p, vc, usage


async def drain(p, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while p.queue_size or p.snapshot()[0]:
        assert loop.time() < end, "pipeline did not drain"
        await asyncio.sleep(0.02)


def first_seen(vc, text):
    return next(i for i, s in enumerate(vc.snapshots) if text in s)


@pytest.mark.asyncio
async def test_sequential_when_replies_are_far_apart(tmp_path):
    p, vc, usage = make_pipeline(tmp_path, seconds_per_job=1.0)
    await p.start()
    await p.enqueue(TTSJob("A", 1, 1, 1, enqueued_at=0.0))
    await p.enqueue(TTSJob("B", 2, 1, 1, enqueued_at=30.0))
    await drain(p)
    await p.stop()
    assert not any(len(s) > 1 for s in vc.snapshots)
    assert first_seen(vc, "A") < first_seen(vc, "B")
    assert [u[0].text for u in usage] == ["A", "B"] and all(u[2] == 1000 for u in usage)


@pytest.mark.asyncio
async def test_quick_reply_from_someone_else_talks_over_the_end(tmp_path):
    p, vc, _ = make_pipeline(tmp_path, seconds_per_job=3.0)
    await p.start()
    await p.enqueue(TTSJob("A", 1, 1, 1, enqueued_at=0.0))
    await p.enqueue(TTSJob("B", 2, 1, 1, enqueued_at=2.0))
    await drain(p)
    await p.stop()
    both = [i for i, s in enumerate(vc.snapshots) if s == ["A", "B"]]
    assert both, "B never overlapped A"
    overlap_frames = len(both)
    assert frames(tp.OVERLAP_S) * 0.5 <= overlap_frames <= frames(tp.OVERLAP_S) + 15
    assert first_seen(vc, "B") >= frames(tp.MIN_LEAD_S)


@pytest.mark.asyncio
async def test_rapid_messages_from_one_person_chain_with_short_blend(tmp_path):
    p, vc, _ = make_pipeline(tmp_path, seconds_per_job=1.0)
    await p.start()
    await p.enqueue(TTSJob("A1", 1, 1, 1, enqueued_at=0.0))
    await p.enqueue(TTSJob("A2", 1, 1, 1, enqueued_at=0.5))
    await drain(p)
    await p.stop()
    both = [s for s in vc.snapshots if s == ["A1", "A2"]]
    assert 0 < len(both) <= frames(tp.SAME_OVERLAP_S) + 15


@pytest.mark.asyncio
async def test_stop_user_and_stop_all(tmp_path):
    p, vc, usage = make_pipeline(tmp_path, seconds_per_job=2.0)
    await p.start()
    for i, uid in enumerate([1, 2, 1, 2]):
        await p.enqueue(TTSJob(f"m{i}", uid, 1, 1, enqueued_at=100.0 * i))
    await asyncio.sleep(0.1)
    assert p.stop_user(1) >= 1
    tracks, pending = p.snapshot()
    assert all(j.user_id == 2 for j in pending)
    assert p.stop_all() >= 1
    await drain(p)
    await p.stop()
    spoken = {u[0].text for u in usage}
    assert "m2" not in spoken            # user 1's queued message was removed before synthesis
    assert any(u[3] for u in usage)      # something was cut off mid-way (skipped=True)


@pytest.mark.asyncio
async def test_skip_stops_only_whats_playing(tmp_path):
    p, vc, usage = make_pipeline(tmp_path, seconds_per_job=2.0)
    await p.start()
    await p.enqueue(TTSJob("long", 1, 1, 1, enqueued_at=0.0))
    await p.enqueue(TTSJob("after", 2, 1, 1, enqueued_at=50.0))
    await asyncio.sleep(0.1)
    assert await p.skip_current() is True
    await drain(p)
    await p.stop()
    assert [u[0].text for u in usage] == ["long", "after"]
    assert usage[0][3] is True and usage[1][3] is False


@pytest.mark.asyncio
async def test_playback_restarts_after_silence(tmp_path):
    p, vc, _ = make_pipeline(tmp_path, seconds_per_job=0.2)
    await p.start()
    await p.enqueue(TTSJob("one", 1, 1, 1, enqueued_at=0.0))
    await drain(p)
    await asyncio.sleep(0.05)
    await p.enqueue(TTSJob("two", 1, 1, 1, enqueued_at=60.0))
    await drain(p)
    await p.stop()
    assert vc.plays == 2


@pytest.mark.asyncio
async def test_waits_for_dave_key_before_first_audio(tmp_path):
    p, vc, _ = make_pipeline(tmp_path, seconds_per_job=0.2)
    ready = {"v": False}
    conn = MagicMock()
    conn.dave_session = object()
    type(conn).can_encrypt = property(lambda self: ready["v"])
    vc._connection = conn
    await p.start()
    await p.enqueue(TTSJob("hi", 1, 1, 1))
    await asyncio.sleep(0.2)
    assert vc.plays == 0
    ready["v"] = True
    await drain(p)
    await p.stop()
    assert vc.plays == 1


@pytest.mark.asyncio
async def test_queue_full_raises(tmp_path):
    p, _, _ = make_pipeline(tmp_path)
    p._maxsize = 1
    await p.enqueue(TTSJob("a", 1, 1, 1))
    with pytest.raises(asyncio.QueueFull):
        await p.enqueue(TTSJob("b", 1, 1, 1))


@pytest.mark.asyncio
async def test_missing_voice_reports_error(tmp_path):
    p, vc, usage = make_pipeline(tmp_path)
    p._config_store.set_voice_id(None)
    errors = []
    p._error_hook = lambda j, e: errors.append(e)
    await p.start()
    await p.enqueue(TTSJob("x", 1, 1, 1))
    await drain(p)
    await p.stop()
    assert errors and "no voice" in str(errors[0]) and vc.plays == 0
