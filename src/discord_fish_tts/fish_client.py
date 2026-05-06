"""Fish Audio TTS Live WebSocket client.

Wire format: MessagePack (verified empirically; JSON is not accepted).
Audio format: Ogg/Opus 48kHz mono 20ms (verified empirically).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

import msgpack
import websockets


class FishError(Exception):
    """Base class for Fish client errors."""


class FishConnectError(FishError):
    """WebSocket handshake or TCP failure."""


class FishProtocolError(FishError):
    """Server returned finish event with reason=error."""


class FishTimeoutError(FishError):
    """A timeout was exceeded.

    Attribute `stage` is one of: 'connect', 'first_audio', 'idle'.
    """

    def __init__(self, stage: str):
        super().__init__(f"Fish Audio TTS Live timeout at stage={stage}")
        self.stage = stage


@dataclass
class FishConfig:
    api_key: str
    voice_id: str
    model: str = "s2-pro"
    latency: str = "low"
    base_url: str = "wss://api.fish.audio"
    connect_timeout: float = 10.0
    first_audio_timeout: float = 8.0
    idle_timeout: float = 8.0


def _start_payload(text: str, cfg: FishConfig) -> dict:
    body: dict = {
        "text": "",
        "reference_id": cfg.voice_id,
        "format": "opus",
    }
    if cfg.latency != "normal":
        body["latency"] = cfg.latency
    return {"event": "start", "request": body}


async def stream_opus_ogg(text: str, cfg: FishConfig) -> AsyncIterator[bytes]:
    """Open Fish WS, send msgpack control frames, yield Ogg/Opus audio chunks
    as they arrive. Closes WS on normal completion or any exception.
    """
    url = f"{cfg.base_url.rstrip('/')}/v1/tts/live"
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "model": cfg.model,
    }

    try:
        ws_ctx = websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=cfg.connect_timeout,
        )
    except Exception as e:
        raise FishConnectError(str(e)) from e

    try:
        async with ws_ctx as ws:
            try:
                await ws.send(msgpack.packb(_start_payload(text, cfg), use_bin_type=True))
                await ws.send(msgpack.packb({"event": "text", "text": text}, use_bin_type=True))
                await ws.send(msgpack.packb({"event": "stop"}, use_bin_type=True))
            except Exception as e:
                raise FishConnectError(f"send failed: {e}") from e

            it = ws.__aiter__()
            first = True
            while True:
                timeout = cfg.first_audio_timeout if first else cfg.idle_timeout
                try:
                    msg = await asyncio.wait_for(it.__anext__(), timeout=timeout)
                except asyncio.TimeoutError as e:
                    raise FishTimeoutError("first_audio" if first else "idle") from e
                except StopAsyncIteration:
                    return

                if isinstance(msg, str):
                    continue
                try:
                    evt = msgpack.unpackb(msg, raw=False)
                except Exception:
                    continue
                if not isinstance(evt, dict):
                    continue

                ev = evt.get("event")
                if ev == "audio":
                    audio = evt.get("audio")
                    if isinstance(audio, (bytes, bytearray)):
                        first = False
                        yield bytes(audio)
                elif ev == "finish":
                    if evt.get("reason") == "stop":
                        return
                    raise FishProtocolError(evt.get("message") or "unknown error")
                else:
                    continue
    except FishError:
        raise
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as e:
        raise FishConnectError(str(e)) from e
