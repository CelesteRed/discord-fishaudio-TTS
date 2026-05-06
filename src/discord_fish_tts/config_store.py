"""Unified runtime config: whitelist, voice_id, first-run flag.

All state persists to a single JSON file at the configured path. The store
does NOT know about owner identity — owner is derived at runtime from the
Discord application metadata (`bot.application.owner.id`). The command
layer combines `is_user_whitelisted(user_id) or user_id == owner_id` for
authorization.

Config file shape::

    {
      "voice_id": "<fish_reference_id>" | null,
      "whitelist": [<user_id>, ...],
      "first_run_done": true | false
    }

For backward compatibility, the loader also accepts the v1.1 key
``allowed_users`` as an alias for ``whitelist``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._whitelist: set[int] = set()
        self._voice_id: str | None = None
        self._first_run_done: bool = False
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
        self._save()
        return True

    def list_users(self) -> list[int]:
        return sorted(self._whitelist)

    # ---- voice_id ----

    def get_voice_id(self) -> str | None:
        return self._voice_id

    def set_voice_id(self, voice_id: str | None) -> None:
        self._voice_id = voice_id or None
        self._save()

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
        try:
            self._whitelist = {int(u) for u in wl}
        except (TypeError, ValueError):
            self._whitelist = set()

        v = data.get("voice_id")
        self._voice_id = v if isinstance(v, str) and v.strip() else None

        self._first_run_done = bool(data.get("first_run_done", False))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "voice_id": self._voice_id,
            "whitelist": sorted(self._whitelist),
            "first_run_done": self._first_run_done,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)
