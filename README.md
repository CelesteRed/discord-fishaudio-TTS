# discord-fishaudio-TTS

Standalone Python Discord bot that speaks user-provided text in voice channels via [Fish Audio](https://fish.audio) TTS Live (streaming WebSocket). Companion project to my TypeScript [openclaw-fish-audio-live](https://github.com/averatec/openclaw-fish-audio-live) plugin — same API, different deployment pattern.

## Features (v1 MVP)

- `/say <text>` — bot joins your voice channel and speaks
- DM the bot text → bot speaks in whichever authorized server you're currently in voice
- `/join` `/leave` `/skip` — manual controls
- Per-guild FIFO queue
- **Real streaming playback** — first audio plays before full synthesis completes
- Guild whitelist
- 200-char text limit

## Architecture

Fish Audio outputs Ogg/Opus 48 kHz mono 20 ms — a hand-rolled Ogg page parser extracts raw Opus packets and feeds them to a custom `discord.AudioSource` with `is_opus()=True`, so packets are passed straight into Discord's RTP without any decode/re-encode step. Wire protocol with Fish is MessagePack.

```
Fish WS (msgpack)  ─►  Ogg/Opus chunks  ─►  Ogg demuxer  ─►  Opus packets  ─►  Discord RTP
                                                              (48k mono 20ms — pass-through)
```

| Module | Responsibility |
|---|---|
| `fish_client.py` | MessagePack WebSocket client, async generator of Ogg/Opus chunks |
| `ogg_demux.py` | Streaming Ogg page → Opus packet demuxer (RFC 3533/7845) |
| `audio_source.py` | Thread-safe queue → `discord.AudioSource.read()` bridge |
| `tts_pipeline.py` | Per-guild FIFO queue, worker, skip semantics |
| `voice_manager.py` | discord.py voice client lifecycle wrapper |
| `validators.py` | Guild whitelist, text length, env CSV parsing |
| `commands.py` | Slash commands and DM handler (Cog) |
| `bot.py` | Entry point, `.env` loading, intents |

## Setup

Requires Python 3.11+ and Discord bot with Message Content + Server Members intents.

```bash
git clone https://github.com/<you>/discord-fishaudio-TTS
cd discord-fishaudio-TTS
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env.local
# Edit .env.local with:
#   DISCORD_TOKEN    — https://discord.com/developers/applications → Bot → Reset Token
#   FISH_AUDIO_API_KEY — https://fish.audio Account → API
#   FISH_VOICE_ID    — https://fish.audio Voice Library
#   ALLOWED_GUILDS   — comma-separated Discord server IDs

discord-fish-tts
```

Invite the bot to your server with scopes `bot applications.commands` and permissions `Connect`, `Speak`, `Send Messages`, `Use Slash Commands`.

## Tests

```bash
pytest -v
```

31 tests covering Ogg demuxing (against a captured Fish output fixture), MessagePack WebSocket protocol mocking, cross-thread audio source bridging, FIFO queue with skip semantics, and input validation.

## Roadmap

- Manual `/join` summoning + voice channel locking
- Per-user whitelist
- `/voice` selection from your Fish Audio voice library
- Inline directives (`[[voice=...]] [[speed=...]]`)
- Latency telemetry display
- HTTP fallback transport
- Idle auto-leave
- SQLite persistence for user preferences

## License

MIT
