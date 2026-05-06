"""Bridge async producer (asyncio.Queue of Opus packets) to discord.py's
synchronous AudioSource interface. With is_opus()=True, discord.py treats
each read() return value as a complete Opus packet and forwards it directly
into RTP — no PCM↔Opus conversion happens, enabling mono pass-through.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutTimeoutError
from typing import Optional

import discord

# 3-byte Opus silence frame (CELT 20ms mono, used by discord.py's own
# silence emitter; safe to send during transient producer underruns).
SILENCE_OPUS: bytes = b"\xf8\xff\xfe"

# Hard cap on how long read() waits for a packet before returning silence.
# Must be < 20ms so discord.py's player loop stays in cadence.
_READ_BUDGET_S = 0.015


class StreamingOpusAudioSource(discord.AudioSource):
    def __init__(
        self,
        queue: "asyncio.Queue[Optional[bytes]]",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._queue = queue
        self._loop = loop
        self._eof = False

    def is_opus(self) -> bool:
        return True

    def read(self) -> bytes:
        if self._eof:
            return b""
        try:
            fut = asyncio.run_coroutine_threadsafe(self._queue.get(), self._loop)
            item = fut.result(timeout=_READ_BUDGET_S)
        except FutTimeoutError:
            return SILENCE_OPUS
        except RuntimeError:
            # Loop closed; treat as EOF
            self._eof = True
            return b""

        if item is None:
            self._eof = True
            return b""
        return item

    def cleanup(self) -> None:
        self._eof = True
