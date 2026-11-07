"""Multi-voice mixer.

Discord plays one AudioSource per voice connection, so overlapping speakers are
mixed here: each spoken message is a Track (48 kHz stereo s16le PCM frames of
20 ms) and the Mixer sums all live tracks every 20 ms with per-track gain
(fade-in when a voice "cuts in", ducking for the voice being talked over).
discord.py then Opus-encodes the mix.
"""
from __future__ import annotations

import audioop
import itertools
import queue as _queue
import threading

import discord

FRAME_MS = 20
FRAME_BYTES = 3840  # 20 ms * 48 kHz * 2 ch * 2 bytes
SILENCE = b"\x00" * FRAME_BYTES
_ids = itertools.count(1)


def frames(seconds: float) -> int:
    return max(1, int(round(seconds * 1000 / FRAME_MS)))


class Track:
    """One message being spoken. Frames are pushed from the event loop (producer)
    and pulled by the mixer on discord.py's player thread."""

    def __init__(self, job=None, max_buffer: int = 9000) -> None:
        self.id = next(_ids)
        self.job = job
        self._q: _queue.Queue[bytes | None] = _queue.Queue(maxsize=max_buffer)
        self.frames_played = 0
        self.producer_done = False
        self.finished = False      # no more audio will come out of this track
        self.stopped = False       # ended early by /skip, /stop or an error
        self.started = False       # handed to the mixer
        self.chunks = 0            # Fish audio chunks received (billing signal)
        self.error: BaseException | None = None
        self.voice_id: str | None = None
        self._sentinel = False     # end marker queued behind the last frame
        self._gain = 1.0
        self._target = 1.0
        self._step = 0.0

    # -- producer side (event loop) --
    def push(self, frame: bytes) -> bool:
        """False when the buffer is full (caller retries)."""
        try:
            self._q.put_nowait(frame)
            return True
        except _queue.Full:
            return False

    def close(self) -> None:
        self.producer_done = True
        try:
            self._q.put_nowait(None)
            self._sentinel = True
        except _queue.Full:
            pass  # the reader treats producer_done + empty as the end

    def stop(self) -> None:
        self.stopped = True
        self.finished = True

    @property
    def buffered(self) -> int:
        return self._q.qsize()

    @property
    def remaining_frames(self) -> int | None:
        """Frames left to play, once synthesis is complete (else None)."""
        if not self.producer_done:
            return None
        return max(0, self.buffered - (1 if self._sentinel and not self.finished else 0))

    # -- gain envelope --
    def fade(self, start: float | None, target: float, seconds: float) -> None:
        if start is not None:
            self._gain = start
        self._target = target
        self._step = (target - self._gain) / frames(seconds) if seconds > 0 else (target - self._gain)

    def _next_gain(self) -> float:
        if self._gain != self._target:
            g = self._gain + self._step
            if (self._step > 0 and g >= self._target) or (self._step < 0 and g <= self._target):
                g = self._target
            self._gain = g
        return self._gain

    # -- reader side (player thread) --
    def read_frame(self) -> bytes | None:
        """Next frame, or None while synthesis is behind. Sets finished at the end."""
        if self.finished:
            return None
        try:
            item = self._q.get_nowait()
        except _queue.Empty:
            if self.producer_done:
                self.finished = True
            return None
        if item is None:
            self.finished = True
            return None
        self.frames_played += 1
        gain = self._next_gain()
        if gain >= 0.999:
            return item
        return audioop.mul(item, 2, max(0.0, gain))


class Mixer(discord.AudioSource):
    """Sums every live track. Returns b"" (ends playback) when no tracks remain,
    so the bot isn't "speaking" silence between messages."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracks: list[Track] = []
        self.ended = False

    def is_opus(self) -> bool:
        return False

    def add(self, track: Track) -> bool:
        """Add a track. Returns True if playback had ended and must be restarted."""
        with self._lock:
            track.started = True
            self._tracks.append(track)
            return self.ended

    def reset(self) -> None:
        with self._lock:
            self.ended = False

    @property
    def tracks(self) -> list[Track]:
        with self._lock:
            return list(self._tracks)

    def read(self) -> bytes:
        with self._lock:
            out: bytes | None = None
            for t in self._tracks:
                frame = t.read_frame()
                if frame is None:
                    continue
                out = frame if out is None else audioop.add(out, frame, 2)
            self._tracks = [t for t in self._tracks if not t.finished]
            if out is None and not self._tracks:
                self.ended = True
                return b""
            return out if out is not None else SILENCE

    def cleanup(self) -> None:
        pass  # tracks outlive a single play() call
