"""Per-request TTS usage log (SQLite in DATA_DIR).

Fish bills TTS by UTF-8 bytes of input text, so every request that reached
Fish is recorded with its byte count, speaker, voice, source and text. This
feeds the bot status ("Usage: N% remaining") and the owner's /view reports.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tts_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    source TEXT NOT NULL,
    voice_id TEXT,
    text TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    audio_ms INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tts_usage_user_ts ON tts_usage(user_id, ts);
CREATE INDEX IF NOT EXISTS tts_usage_ts ON tts_usage(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS toggle_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    expired INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS toggle_reminders_user ON toggle_reminders(user_id, sent_ts);
"""


def month_start_ts(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


@dataclass
class UserTotal:
    user_id: int
    messages: int
    bytes: int
    month_bytes: int
    audio_ms: int
    last_ts: float


class UsageStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- writes ----

    def record(self, *, user_id: int, guild_id: int | None, channel_id: int | None, source: str,
               voice_id: str | None, text: str, audio_ms: int = 0, skipped: bool = False,
               ts: float | None = None) -> int:
        nbytes = len(text.encode("utf-8"))
        with self._lock:
            self._db.execute(
                "INSERT INTO tts_usage (ts, user_id, guild_id, channel_id, source, voice_id, text, bytes,"
                " audio_ms, skipped) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts if ts is not None else time.time(), user_id, guild_id, channel_id, source, voice_id,
                 text, nbytes, int(audio_ms), int(skipped)),
            )
            self._db.commit()
        return nbytes

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self._db.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ---- reads ----

    def bytes_since(self, since_ts: float) -> int:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(SUM(bytes), 0) AS b FROM tts_usage WHERE ts >= ?",
                                   (since_ts,)).fetchone()
        return int(row["b"])

    def totals(self, guild_id: int | None = None) -> tuple[int, int]:
        """(messages, bytes) all time, optionally for one guild."""
        where, args = ("WHERE guild_id = ?", (guild_id,)) if guild_id is not None else ("", ())
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM tts_usage {where}", args).fetchone()
        return int(row["n"]), int(row["b"])

    def ranking(self, now: float | None = None, guild_id: int | None = None) -> list[UserTotal]:
        ms = month_start_ts(now)
        where, args = ("WHERE guild_id = ?", (ms, guild_id)) if guild_id is not None else ("", (ms,))
        with self._lock:
            rows = self._db.execute(
                "SELECT user_id, COUNT(*) AS n, SUM(bytes) AS b,"
                " SUM(CASE WHEN ts >= ? THEN bytes ELSE 0 END) AS mb,"
                " SUM(audio_ms) AS ams, MAX(ts) AS last"
                f" FROM tts_usage {where} GROUP BY user_id ORDER BY b DESC, n DESC", args,
            ).fetchall()
        return [UserTotal(r["user_id"], r["n"], r["b"], r["mb"], r["ams"] or 0, r["last"]) for r in rows]

    def guild_totals(self, now: float | None = None) -> dict[int, dict]:
        """Per guild: messages, bytes, month_bytes, users, last_ts, top_user."""
        ms = month_start_ts(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT guild_id, COUNT(*) AS n, SUM(bytes) AS b,"
                " SUM(CASE WHEN ts >= ? THEN bytes ELSE 0 END) AS mb,"
                " COUNT(DISTINCT user_id) AS users, MAX(ts) AS last"
                " FROM tts_usage WHERE guild_id IS NOT NULL GROUP BY guild_id", (ms,),
            ).fetchall()
            tops = self._db.execute(
                "SELECT guild_id, user_id, SUM(bytes) AS b FROM tts_usage WHERE guild_id IS NOT NULL"
                " GROUP BY guild_id, user_id ORDER BY guild_id, b DESC",
            ).fetchall()
        top: dict[int, int] = {}
        for r in tops:
            top.setdefault(r["guild_id"], r["user_id"])
        return {r["guild_id"]: {"messages": r["n"], "bytes": r["b"], "month_bytes": r["mb"], "users": r["users"],
                                "last_ts": r["last"], "top_user": top.get(r["guild_id"])} for r in rows}

    def user_guilds(self, user_id: int) -> list[tuple[int, int, int]]:
        """(guild_id, messages, bytes) where this user used TTS."""
        with self._lock:
            rows = self._db.execute(
                "SELECT guild_id, COUNT(*) AS n, SUM(bytes) AS b FROM tts_usage"
                " WHERE user_id = ? AND guild_id IS NOT NULL GROUP BY guild_id ORDER BY b DESC", (user_id,),
            ).fetchall()
        return [(r["guild_id"], r["n"], r["b"]) for r in rows]

    def known_user_ids(self) -> list[int]:
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT user_id FROM tts_usage").fetchall()
        return [r["user_id"] for r in rows]

    def user_summary(self, user_id: int, now: float | None = None) -> dict:
        ms = month_start_ts(now)
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b,"
                " COALESCE(SUM(CASE WHEN ts >= ? THEN bytes ELSE 0 END),0) AS mb,"
                " COALESCE(SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END),0) AS mn,"
                " COALESCE(SUM(audio_ms),0) AS ams, COALESCE(SUM(skipped),0) AS sk,"
                " MIN(ts) AS first, MAX(ts) AS last"
                " FROM tts_usage WHERE user_id = ?", (ms, ms, user_id),
            ).fetchone()
            sources = self._db.execute(
                "SELECT source, COUNT(*) AS n FROM tts_usage WHERE user_id = ? GROUP BY source ORDER BY n DESC",
                (user_id,),
            ).fetchall()
            channels = self._db.execute(
                "SELECT channel_id, COUNT(*) AS n FROM tts_usage WHERE user_id = ? AND channel_id IS NOT NULL"
                " GROUP BY channel_id ORDER BY n DESC LIMIT 3", (user_id,),
            ).fetchall()
            voices = self._db.execute(
                "SELECT voice_id, COUNT(*) AS n FROM tts_usage WHERE user_id = ? AND voice_id IS NOT NULL"
                " GROUP BY voice_id ORDER BY n DESC LIMIT 3", (user_id,),
            ).fetchall()
        return {
            "messages": row["n"], "bytes": row["b"], "month_bytes": row["mb"], "month_messages": row["mn"],
            "audio_ms": row["ams"], "skipped": row["sk"], "first_ts": row["first"], "last_ts": row["last"],
            "sources": [(r["source"], r["n"]) for r in sources],
            "channels": [(r["channel_id"], r["n"]) for r in channels],
            "voices": [(r["voice_id"], r["n"]) for r in voices],
        }

    def recent(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT ts, source, text, skipped FROM tts_usage WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    # ---- "auto-speak is off" reminders (cooldown + later edit) ----

    def add_reminder(self, *, user_id: int, guild_id: int, channel_id: int, message_id: int,
                     sent_ts: float, expires_ts: float, keep: int = 2) -> None:
        """Track a sent reminder; only the `keep` most recent per user are kept."""
        with self._lock:
            self._db.execute(
                "INSERT INTO toggle_reminders (user_id, guild_id, channel_id, message_id, sent_ts, expires_ts)"
                " VALUES (?,?,?,?,?,?)", (user_id, guild_id, channel_id, message_id, sent_ts, expires_ts))
            self._db.execute(
                "DELETE FROM toggle_reminders WHERE user_id = ? AND id NOT IN ("
                " SELECT id FROM toggle_reminders WHERE user_id = ? ORDER BY sent_ts DESC, id DESC LIMIT ?)",
                (user_id, user_id, keep))
            self._db.commit()

    def last_reminder_ts(self, user_id: int) -> float | None:
        """When the user's cooldown started, or None if it's over (or never started)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(sent_ts) AS t FROM toggle_reminders WHERE user_id = ? AND expired = 0",
                (user_id,)).fetchone()
        return row["t"]

    def reminders(self, user_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM toggle_reminders WHERE user_id = ? ORDER BY sent_ts DESC", (user_id,)).fetchall()

    def due_reminders(self, now: float, user_id: int | None = None) -> list[sqlite3.Row]:
        """Active reminders whose cooldown is over (or all active ones for `user_id`)."""
        with self._lock:
            if user_id is not None:
                return self._db.execute(
                    "SELECT * FROM toggle_reminders WHERE expired = 0 AND user_id = ?", (user_id,)).fetchall()
            return self._db.execute(
                "SELECT * FROM toggle_reminders WHERE expired = 0 AND expires_ts <= ?", (now,)).fetchall()

    def mark_reminder_expired(self, reminder_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE toggle_reminders SET expired = 1 WHERE id = ?", (reminder_id,))
            self._db.commit()
