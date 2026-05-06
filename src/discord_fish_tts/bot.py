"""Bot entry point. Reads env from .env.local and starts the gateway."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from .commands import TTSCog
from .fish_client import FishConfig
from .tts_pipeline import PipelineRegistry
from .validators import parse_int_set
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"environment variable {name} is required")
    return val


async def amain() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    discord_token = _required("DISCORD_TOKEN")
    fish_cfg = FishConfig(
        api_key=_required("FISH_AUDIO_API_KEY"),
        voice_id=_required("FISH_VOICE_ID"),
        model=os.environ.get("FISH_MODEL", "s2-pro"),
        latency=os.environ.get("FISH_LATENCY", "low"),
    )
    allowed = parse_int_set(os.environ.get("ALLOWED_GUILDS"))
    if not allowed:
        raise SystemExit("ALLOWED_GUILDS must contain at least one guild ID")
    max_text_len = int(os.environ.get("MAX_TEXT_LEN", "200"))

    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.members = True  # needed to look up user across guilds for DM

    bot = commands.Bot(command_prefix="!", intents=intents)
    voice_mgr = VoiceManager(bot)
    registry = PipelineRegistry(fish_cfg, voice_mgr, asyncio.get_running_loop())

    @bot.event
    async def on_ready() -> None:
        log.info("logged in as %s (id=%s)", bot.user, getattr(bot.user, "id", "?"))
        synced = await bot.tree.sync()
        log.info("synced %d slash commands", len(synced))

    await bot.add_cog(TTSCog(
        bot=bot,
        voice_manager=voice_mgr,
        registry=registry,
        allowed_guilds=allowed,
        max_text_len=max_text_len,
    ))

    try:
        await bot.start(discord_token)
    finally:
        await registry.shutdown_all()
        await bot.close()


def cli() -> None:
    # Load .env.local from cwd or repo root
    for candidate in (Path.cwd() / ".env.local", Path(__file__).resolve().parents[2] / ".env.local"):
        if candidate.exists():
            load_dotenv(candidate)
            break
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli()
