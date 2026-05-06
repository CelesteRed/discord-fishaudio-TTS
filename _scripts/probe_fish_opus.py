"""Probe Fish Audio TTS Live WebSocket Opus output format.

Fish Audio uses MessagePack on the wire (per official docs).
Goal: determine the format=opus stream container and audio params, plus
streaming chunk cadence.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import msgpack
import websockets

API_KEY = os.environ["FISH_AUDIO_API_KEY"]
VOICE_ID = "bf86e0ce9e1b47dd97efdad184608a67"
TEXT = "测试一下Fish Audio的Opus输出格式，这是一段验证音频，足够长以便观察分块行为。"
OUT_DIR = Path("/tmp/fish_probe")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def pack(d: dict) -> bytes:
    return msgpack.packb(d, use_bin_type=True)


async def probe(fmt: str) -> None:
    print(f"\n=== format={fmt} ===", flush=True)
    url = "wss://api.fish.audio/v1/tts/live"
    headers = {"Authorization": f"Bearer {API_KEY}", "model": "s2-pro"}
    print(f"connecting ...", flush=True)
    async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
        print("connected", flush=True)
        start = {
            "event": "start",
            "request": {
                "text": "",
                "reference_id": VOICE_ID,
                "format": fmt,
                "latency": "low",
            },
        }
        await ws.send(pack(start))
        await ws.send(pack({"event": "text", "text": TEXT}))
        await ws.send(pack({"event": "stop"}))
        print("sent start/text/stop, waiting...", flush=True)

        chunks: list[bytes] = []
        chunk_sizes: list[int] = []
        msg_count = 0
        async for msg in ws:
            msg_count += 1
            if isinstance(msg, str):
                print(f"[msg#{msg_count}] TEXT (unexpected): {msg[:200]!r}", flush=True)
                continue
            try:
                evt = msgpack.unpackb(msg, raw=False)
            except Exception as e:
                print(f"[msg#{msg_count}] not msgpack ({e}); first 32 bytes: {msg[:32].hex()}", flush=True)
                continue
            ev = evt.get("event") if isinstance(evt, dict) else None
            if ev == "audio":
                b = evt.get("audio")
                if isinstance(b, (bytes, bytearray)):
                    chunks.append(bytes(b))
                    chunk_sizes.append(len(b))
                    if msg_count <= 3 or msg_count % 10 == 0:
                        print(f"[msg#{msg_count}] audio chunk {len(b)} bytes head={bytes(b)[:8].hex()}", flush=True)
                else:
                    print(f"[msg#{msg_count}] audio not bytes: type={type(b).__name__}", flush=True)
            elif ev == "finish":
                print(f"[msg#{msg_count}] FINISH reason={evt.get('reason')} message={evt.get('message')}", flush=True)
                break
            elif ev == "log":
                print(f"[msg#{msg_count}] LOG {evt}", flush=True)
            else:
                print(f"[msg#{msg_count}] event={ev} payload-keys={list(evt.keys()) if isinstance(evt, dict) else evt}", flush=True)

        total = b"".join(chunks)
        path = OUT_DIR / f"probe.{fmt}"
        path.write_bytes(total)

        if chunks:
            print(f"chunks: {len(chunks)} sizes min={min(chunk_sizes)} max={max(chunk_sizes)} avg={sum(chunk_sizes)//len(chunk_sizes)}")
            print(f"total bytes: {len(total)}")
            print(f"first 32 bytes: {total[:32].hex()}")
            print(f"first 4 ascii: {total[:4]!r}")
            print(f"saved: {path}")
        else:
            print("no audio received")


async def main() -> None:
    formats = sys.argv[1:] or ["opus", "pcm"]
    for fmt in formats:
        try:
            await probe(fmt)
        except Exception as e:
            print(f"format={fmt} failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
