"""Unified runtime config: whitelist, voices, auto-TTS toggles, first-run flag.

All state persists to a single JSON file at the configured path. The store
does NOT know about owner identity — owner is derived at runtime from the
Discord application metadata (`bot.application.owner.id`). The command
layer combines `is_user_whitelisted(user_id) or user_id == owner_id` for
authorization.

Config file shape::

    {
      "voice_id": "<fish_reference_id>" | null,      # default voice
      "whitelist": [<user_id>, ...],
      "first_run_done": true | false,
      "user_voices": {"<user_id>": "<fish_reference_id>", ...},
      "auto_tts_users": [<user_id>, ...],             # /toggle on
      "auto_tts_channels": [<text_channel_id>, ...]   # where /toggle listens
    }

For backward compatibility, the loader also accepts the v1.1 key
``allowed_users`` as an alias for ``whitelist``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

_VOICE_ID_RE = re.compile(r"[0-9a-f]{32}")

# #no-mic in Cosmiq Studio. Used when the config file has no channel list yet.
DEFAULT_AUTO_TTS_CHANNELS: tuple[int, ...] = (1567826418329587783,)

# Pseudo-user for system messages (operator/Claude relays, /announce, alerts).
# Stored like any user voice (user_voices["0"]) so it survives default changes.
SERVER_USER_ID = 0


def normalize_voice_id(value: str | None) -> str | None:
    """Accept a bare reference_id or a fish.audio URL (…/m/<id>/) and return the id."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if "/" in value:
        m = _VOICE_ID_RE.search(value.lower())
        if m:
            return m.group(0)
    return value


def _int_set(values: Any) -> set[int]:
    try:
        return {int(v) for v in values or []}
    except (TypeError, ValueError):
        return set()


class ConfigStore:
    def __init__(self, path: Path, default_auto_channels: Iterable[int] = DEFAULT_AUTO_TTS_CHANNELS) -> None:
        self._path = Path(path)
        self._whitelist: set[int] = set()
        self._voice_id: str | None = None
        self._first_run_done: bool = False
        self._user_voices: dict[int, str] = {}
        self._auto_tts_users: set[int] = set()       # chose auto-speak ON
        self._auto_tts_off: set[int] = set()         # chose auto-speak OFF (unset = server default)
        self._auto_tts_channels: set[int] = set(default_auto_channels)
        self._enabled_guilds: set[int] = set()
        self._autocorrect_off: set[int] = set()      # chose auto-correct OFF
        self._autocorrect_on: set[int] = set()       # chose auto-correct ON (unset = server default)
        self._guild_settings: dict[int, dict] = {}   # per-server /config values
        self._guild_gate_initialized = False
        self._load()

    # ---- whitelist ----

    def is_user_whitelisted(self, user_id: int) -> bool:
        return user_id in self._whitelist

    def add_user(self, user_id: int) -> bool:
        if user_id in self._whitelist:
            return False
        self._whitelist.add(user_id)
        self._save()
        return True

    def remove_user(self, user_id: int) -> bool:
        if user_id not in self._whitelist:
            return False
        self._whitelist.discard(user_id)
        self._auto_tts_users.discard(user_id)
        self._auto_tts_off.discard(user_id)
        self._save()
        return True

    def list_users(self) -> list[int]:
        return sorted(self._whitelist)

    # ---- default voice ----

    def get_voice_id(self) -> str | None:
        return self._voice_id

    def set_voice_id(self, voice_id: str | None) -> None:
        self._voice_id = normalize_voice_id(voice_id)
        self._save()

    # ---- per-user voices ----

    def get_user_voice(self, user_id: int) -> str | None:
        return self._user_voices.get(user_id)

    def set_user_voice(self, user_id: int, voice_id: str | None) -> None:
        vid = normalize_voice_id(voice_id)
        if vid is None:
            self._user_voices.pop(user_id, None)
        else:
            self._user_voices[user_id] = vid
        self._save()

    def get_voice_for(self, user_id: int, guild_id: int | None = None) -> str | None:
        """The user's own voice, else the server's default voice, else the global default."""
        return (self._user_voices.get(user_id)
                or (self.guild_default_voice(guild_id) if guild_id is not None else None)
                or self._voice_id)

    # ---- SERVER pseudo-user ----

    def get_server_voice(self) -> str | None:
        return self._user_voices.get(SERVER_USER_ID)

    def set_server_voice(self, voice_id: str | None) -> None:
        self.set_user_voice(SERVER_USER_ID, voice_id)

    # ---- auto-TTS (/toggle): personal choice, else the server's default ----

    def auto_tts_choice(self, user_id: int) -> bool | None:
        if user_id in self._auto_tts_users:
            return True
        if user_id in self._auto_tts_off:
            return False
        return None

    def is_auto_tts(self, user_id: int, guild_id: int | None = None) -> bool:
        choice = self.auto_tts_choice(user_id)
        if choice is not None:
            return choice
        return bool(self.guild_setting(guild_id, "auto_speak_default", False)) if guild_id else False

    def set_auto_tts(self, user_id: int, enabled: bool | None) -> None:
        """True/False = personal choice everywhere; None = follow each server's default."""
        self._auto_tts_users.discard(user_id)
        self._auto_tts_off.discard(user_id)
        if enabled is True:
            self._auto_tts_users.add(user_id)
        elif enabled is False:
            self._auto_tts_off.add(user_id)
        self._save()

    def is_auto_tts_channel(self, channel_id: int) -> bool:
        return channel_id in self._auto_tts_channels

    def list_auto_tts_channels(self) -> list[int]:
        return sorted(self._auto_tts_channels)

    def add_auto_tts_channel(self, channel_id: int) -> bool:
        if channel_id in self._auto_tts_channels:
            return False
        self._auto_tts_channels.add(channel_id)
        self._save()
        return True

    def remove_auto_tts_channel(self, channel_id: int) -> bool:
        if channel_id not in self._auto_tts_channels:
            return False
        self._auto_tts_channels.discard(channel_id)
        self._save()
        return True

    # ---- auto-correct: personal choice, else the server's default (on) ----

    def autocorrect_choice(self, user_id: int) -> bool | None:
        if user_id in self._autocorrect_on:
            return True
        if user_id in self._autocorrect_off:
            return False
        return None

    def is_autocorrect(self, user_id: int, guild_id: int | None = None) -> bool:
        choice = self.autocorrect_choice(user_id)
        if choice is not None:
            return choice
        return bool(self.guild_setting(guild_id, "autocorrect_default", True)) if guild_id else True

    def set_autocorrect(self, user_id: int, enabled: bool | None) -> None:
        self._autocorrect_on.discard(user_id)
        self._autocorrect_off.discard(user_id)
        if enabled is True:
            self._autocorrect_on.add(user_id)
        elif enabled is False:
            self._autocorrect_off.add(user_id)
        self._save()

    # ---- per-server settings (/config) ----

    def guild_setting(self, guild_id: int | None, key: str, default=None):
        if guild_id is None:
            return default
        value = self._guild_settings.get(guild_id, {}).get(key)
        return default if value is None else value

    def set_guild_setting(self, guild_id: int, key: str, value) -> None:
        settings = self._guild_settings.setdefault(guild_id, {})
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
        if not settings:
            self._guild_settings.pop(guild_id, None)
        self._save()

    def guild_default_voice(self, guild_id: int | None) -> str | None:
        return self.guild_setting(guild_id, "default_voice")

    def set_guild_default_voice(self, guild_id: int, voice_id: str | None) -> None:
        self.set_guild_setting(guild_id, "default_voice", normalize_voice_id(voice_id))

    # ---- server gate (/enable, /disable) ----

    def guild_gate_initialized(self) -> bool:
        return self._guild_gate_initialized

    def init_guild_gate(self, guild_ids: Iterable[int]) -> None:
        """First run of the gate: servers the bot is already in stay usable."""
        self._enabled_guilds = set(guild_ids)
        self._guild_gate_initialized = True
        self._save()

    def is_guild_enabled(self, guild_id: int) -> bool:
        return guild_id in self._enabled_guilds

    def list_enabled_guilds(self) -> list[int]:
        return sorted(self._enabled_guilds)

    def enable_guild(self, guild_id: int) -> bool:
        self._guild_gate_initialized = True
        if guild_id in self._enabled_guilds:
            return False
        self._enabled_guilds.add(guild_id)
        self._save()
        return True

    def disable_guild(self, guild_id: int) -> bool:
        self._guild_gate_initialized = True
        if guild_id not in self._enabled_guilds:
            self._save()
            return False
        self._enabled_guilds.discard(guild_id)
        self._save()
        return True

    # ---- first-run ----

    def is_first_run(self) -> bool:
        return not self._first_run_done

    def mark_first_run_done(self) -> None:
        if not self._first_run_done:
            self._first_run_done = True
            self._save()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data: Any = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return

        # whitelist (with legacy alias `allowed_users`)
        wl = data.get("whitelist")
        if wl is None:
            wl = data.get("allowed_users", [])
        self._whitelist = _int_set(wl)

        v = data.get("voice_id")
        self._voice_id = normalize_voice_id(v) if isinstance(v, str) else None

        self._first_run_done = bool(data.get("first_run_done", False))

        uv = data.get("user_voices")
        if isinstance(uv, dict):
            for k, val in uv.items():
                vid = normalize_voice_id(val) if isinstance(val, str) else None
                try:
                    if vid:
                        self._user_voices[int(k)] = vid
                except (TypeError, ValueError):
                    continue

        self._auto_tts_users = _int_set(data.get("auto_tts_users"))
        if "auto_tts_channels" in data:
            self._auto_tts_channels = _int_set(data.get("auto_tts_channels"))
        self._autocorrect_off = _int_set(data.get("autocorrect_off_users"))
        self._autocorrect_on = _int_set(data.get("autocorrect_on_users"))
        self._auto_tts_off = _int_set(data.get("auto_tts_off_users"))
        gs = data.get("guild_settings")
        if isinstance(gs, dict):
            for k, v in gs.items():
                try:
                    if isinstance(v, dict) and v:
                        self._guild_settings[int(k)] = dict(v)
                except (TypeError, ValueError):
                    continue
        if "enabled_guilds" in data:
            self._enabled_guilds = _int_set(data.get("enabled_guilds"))
            self._guild_gate_initialized = True

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "voice_id": self._voice_id,
            "whitelist": sorted(self._whitelist),
            "first_run_done": self._first_run_done,
            "user_voices": {str(k): v for k, v in sorted(self._user_voices.items())},
            "auto_tts_users": sorted(self._auto_tts_users),
            "auto_tts_off_users": sorted(self._auto_tts_off),
            "auto_tts_channels": sorted(self._auto_tts_channels),
            "autocorrect_off_users": sorted(self._autocorrect_off),
            "autocorrect_on_users": sorted(self._autocorrect_on),
            "guild_settings": {str(k): v for k, v in sorted(self._guild_settings.items())},
        }
        if self._guild_gate_initialized:
            payload["enabled_guilds"] = sorted(self._enabled_guilds)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)
