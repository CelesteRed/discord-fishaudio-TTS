"""Local-only control socket (Unix domain, inside DATA_DIR) for operator tests.

Protocol: client sends one JSON line, server replies with one JSON line.
  {"cmd": "status"}
  {"cmd": "join", "channel_id": 123}
  {"cmd": "announce", "text": "..."}   SERVER voice in every voice channel the bot is in
  {"cmd": "alert", "text": "..."}      DM owner + announce
  {"cmd": "say", "text": "...", "user_id": 0, "channel_id": 123}   user_id defaults to SERVER
("say" joins channel_id first if given and the bot is not already there.)
Only reachable by root and the service user (socket mode 0660).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import discord

from .config_store import SERVER_USER_ID
from .tts_pipeline import PipelineRegistry, TTSJob
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)

def _davey_info(s) -> dict | None:
    if s is None:
        return None
    out = {}
    for n in ("status", "ready", "can_encrypt", "epoch", "protocol_version", "own_leaf_index"):
        try:
            out[n] = str(getattr(s, n))
        except Exception as e:
            out[n] = f"err:{e!r}"
    try:
        out["users"] = [str(u) for u in s.get_user_ids()]
    except Exception as e:
        out["users"] = f"err:{e!r}"
    return out


def _voice_info(bot: discord.Client) -> list[dict]:
    out = []
    for g in bot.guilds:
        vc = g.voice_client
        if vc is None:
            continue
        conn = getattr(vc, "_connection", None)
        out.append({
            "guild": g.name, "guild_id": g.id,
            "channel": vc.channel.name, "channel_id": vc.channel.id,
            "listeners": [m.display_name for m in vc.channel.members if not m.bot],
            "connected": vc.is_connected(), "playing": vc.is_playing(),
            "mode": getattr(vc, "mode", None),
            "dave_version": getattr(conn, "dave_protocol_version", None),
            "dave_session": bool(getattr(conn, "dave_session", None)),
            "can_encrypt": getattr(conn, "can_encrypt", None),
            "davey": _davey_info(getattr(conn, "dave_session", None)),
        })
    return out


async def start_control_server(bot: discord.Client, registry: PipelineRegistry, voice_mgr: VoiceManager,
                               path: Path, notifier=None) -> asyncio.AbstractServer:
    async def join(channel_id: int) -> str:
        ch = bot.get_channel(channel_id)
        if ch is None or not hasattr(ch, "guild"):
            raise ValueError(f"voice channel {channel_id} not visible to bot")
        vc = await voice_mgr.ensure_in_channel(ch.guild.id, channel_id)
        log.info("control: joined #%s", vc.channel.name)
        return vc.channel.name

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads((await reader.readline()).decode() or "{}")
            cmd = req.get("cmd")
            if cmd == "status":
                resp = {"ok": True, "voice": _voice_info(bot)}
            elif cmd == "announce":
                if notifier is None:
                    resp = {"ok": False, "error": "no notifier"}
                else:
                    resp = {"ok": True, "queued_in": await notifier.announce(str(req["text"])[:500])}
            elif cmd == "alert":
                await notifier.alert(str(req["text"])[:500])
                resp = {"ok": True}
            elif cmd == "join":
                resp = {"ok": True, "joined": await join(int(req["channel_id"]))}
            elif cmd == "say":
                if req.get("channel_id"):
                    cid = int(req["channel_id"])
                    if not any(v["channel_id"] == cid for v in _voice_info(bot)):
                        await join(cid)
                        await asyncio.sleep(2.0)  # let the voice/E2EE handshake settle
                info = _voice_info(bot)
                if not info:
                    resp = {"ok": False, "error": "bot is not in any voice channel"}
                else:
                    v = info[0]
                    await registry.get_or_create(v["guild_id"]).enqueue(TTSJob(
                        text=str(req["text"])[:500], user_id=int(req.get("user_id", SERVER_USER_ID)), source="server",
                        guild_id=v["guild_id"], voice_channel_id=v["channel_id"],
                    ))
                    log.info("control: queued %r as %s in #%s", str(req["text"])[:60], req.get("user_id", SERVER_USER_ID), v["channel"])
                    resp = {"ok": True, "queued_in": v["channel"], "listeners": v["listeners"]}
            else:
                resp = {"ok": False, "error": f"unknown cmd {cmd!r}"}
        except Exception as e:  # never crash the bot from the control path
            log.exception("control request failed")
            resp = {"ok": False, "error": repr(e)}
        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
        writer.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = await asyncio.start_unix_server(handle, path=str(path))
    os.chmod(path, 0o660)
    log.info("control socket listening at %s", path)
    return server
