"""Owner + JSON-backed user whitelist.

Authorization model:
  - Exactly one owner (configured via env, immutable at runtime).
  - Owner is always allowed and cannot be removed.
  - Owner is the only user who can manage the whitelist via /whitelist commands.
  - Allowed users (added by owner) can use voice/TTS commands.

Persistence:
  - JSON file with shape {"allowed_users": [<int>, ...]}.
  - Saves are atomic (write temp + rename).
  - Corrupt file → fall back to empty allowlist (defensive; logs not added in v1.1).
"""
from __future__ import annotations

import json
from pathlib import Path


class UserWhitelist:
    def __init__(self, owner_id: int, store_path: Path) -> None:
        self._owner_id = owner_id
        self._path = Path(store_path)
        self._allowed: set[int] = self._load()

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def is_owner(self, user_id: int) -> bool:
        return user_id == self._owner_id

    def is_allowed(self, user_id: int) -> bool:
        return user_id == self._owner_id or user_id in self._allowed

    def add(self, user_id: int) -> bool:
        """Returns True if newly added; False if owner or already present."""
        if user_id == self._owner_id:
            return False
        if user_id in self._allowed:
            return False
        self._allowed.add(user_id)
        self._save()
        return True

    def remove(self, user_id: int) -> bool:
        """Returns True if removed; False if owner or not present."""
        if user_id == self._owner_id:
            return False
        if user_id not in self._allowed:
            return False
        self._allowed.discard(user_id)
        self._save()
        return True

    def list_users(self) -> list[int]:
        """Sorted list of allowed user IDs (excluding owner)."""
        return sorted(self._allowed)

    def _load(self) -> set[int]:
        if not self._path.exists():
            return set()
        try:
            data = json.loads(self._path.read_text())
            return {int(u) for u in data.get("allowed_users", [])}
        except (json.JSONDecodeError, ValueError, KeyError, OSError):
            return set()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"allowed_users": sorted(self._allowed)}, indent=2,
        ))
        tmp.replace(self._path)
