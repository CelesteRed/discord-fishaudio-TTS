"""Text rules applied before speaking: auto-correct, then personal shortcuts.

- Shortcuts are per person: "brb" -> "be right back". Matched as whole words or
  phrases, case-insensitively.
- Auto-correct (on by default, per-person toggle) fixes clear misspellings with
  pyspellchecker (offline English word-frequency list, edit distance 1). It
  never touches [emotion tags], short words, ALL-CAPS / mixed-case words,
  @names, stretched words (sooo), common chat slang, the shared dictionary, or
  anyone's shortcut words.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_SHORTCUTS_PER_USER = 50
MAX_SHORTCUT_INPUT = 60
MAX_SHORTCUT_REPLACEMENT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shortcuts (
    user_id INTEGER NOT NULL,
    sid INTEGER NOT NULL,
    input TEXT NOT NULL,
    replacement TEXT NOT NULL,
    created_ts REAL NOT NULL,
    PRIMARY KEY (user_id, sid)
);
CREATE TABLE IF NOT EXISTS dictionary (
    word TEXT PRIMARY KEY,
    added_by INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',
    ts REAL NOT NULL
);
"""

# Chat words a dictionary spell-checker would "fix" into something else.
SLANG = frozenset("""
lmao lmfao rofl bruh bro bros brah sis fam yall y'all gonna wanna gotta kinda sorta lemme gimme dunno
ngl tbh imo imho idk idc ikr smh fyi btw omg omfg wtf wth stfu af irl rn fr frfr ong istg tho thx
pls plz ppl yeah yeh yep yup nah nope naw ayy ayo aye bet cap nocap lowkey highkey sus yeet vibe vibing
simp poggers pog pogchamp kekw xd haha hahaha hehe lol lolol lul welp oof meh hmm hmmm uhh umm ugh
dont cant wont isnt arent wasnt werent didnt doesnt hasnt havent hadnt couldnt shouldnt wouldnt aint
youre theyre thats whats hows wheres whos ive youve weve theyve itll youll theyll shes hes lets
discord cosmiq starie fishaudio emoji emojis gif gifs pfp dms vc vcs
""".split())

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_TAG_RE = re.compile(r"\[[^\]]*\]")  # Fish emotion / delivery tags: keep verbatim
_STRETCH_RE = re.compile(r"(.)\1\1")


def words_of(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


@dataclass
class Shortcut:
    sid: int
    input: str
    replacement: str


class TextStore:
    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._dict_cache: set[str] | None = None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- shortcuts ----

    def shortcuts(self, user_id: int) -> list[Shortcut]:
        with self._lock:
            rows = self._db.execute("SELECT sid, input, replacement FROM shortcuts WHERE user_id = ? ORDER BY sid",
                                    (user_id,)).fetchall()
        return [Shortcut(r["sid"], r["input"], r["replacement"]) for r in rows]

    def find_shortcut(self, user_id: int, text: str) -> Shortcut | None:
        key = text.strip().lower()
        return next((s for s in self.shortcuts(user_id) if s.input.lower() == key), None)

    def add_shortcut(self, user_id: int, inp: str, replacement: str) -> Shortcut:
        with self._lock:
            sid = self._db.execute("SELECT COALESCE(MAX(sid), 0) + 1 AS n FROM shortcuts WHERE user_id = ?",
                                   (user_id,)).fetchone()["n"]
            self._db.execute("INSERT INTO shortcuts (user_id, sid, input, replacement, created_ts) VALUES (?,?,?,?,?)",
                             (user_id, sid, inp, replacement, time.time()))
            self._db.commit()
        return Shortcut(sid, inp, replacement)

    def update_shortcut(self, user_id: int, sid: int, replacement: str) -> Shortcut | None:
        with self._lock:
            cur = self._db.execute("UPDATE shortcuts SET replacement = ? WHERE user_id = ? AND sid = ?",
                                   (replacement, user_id, sid))
            self._db.commit()
            if not cur.rowcount:
                return None
            r = self._db.execute("SELECT sid, input, replacement FROM shortcuts WHERE user_id = ? AND sid = ?",
                                 (user_id, sid)).fetchone()
        return Shortcut(r["sid"], r["input"], r["replacement"])

    def delete_shortcut(self, user_id: int, sid: int) -> Shortcut | None:
        with self._lock:
            r = self._db.execute("SELECT sid, input, replacement FROM shortcuts WHERE user_id = ? AND sid = ?",
                                 (user_id, sid)).fetchone()
            if r is None:
                return None
            self._db.execute("DELETE FROM shortcuts WHERE user_id = ? AND sid = ?", (user_id, sid))
            self._db.commit()
        return Shortcut(r["sid"], r["input"], r["replacement"])

    def all_shortcut_words(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT input, replacement FROM shortcuts").fetchall()
        out: set[str] = set()
        for r in rows:
            out |= words_of(r["input"]) | words_of(r["replacement"])
        return out

    # ---- shared dictionary (words auto-correct must leave alone) ----

    def dictionary(self) -> set[str]:
        if self._dict_cache is None:
            with self._lock:
                self._dict_cache = {r["word"] for r in self._db.execute("SELECT word FROM dictionary")}
        return set(self._dict_cache)

    def dictionary_entries(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT word, added_by, source, ts FROM dictionary ORDER BY word").fetchall()

    def add_words(self, words, added_by: int | None, source: str = "manual") -> list[str]:
        added = []
        with self._lock:
            for w in {w.lower() for w in words if w}:
                cur = self._db.execute("INSERT OR IGNORE INTO dictionary (word, added_by, source, ts) VALUES (?,?,?,?)",
                                       (w, added_by, source, time.time()))
                if cur.rowcount:
                    added.append(w)
            self._db.commit()
        self._dict_cache = None
        return sorted(added)

    def remove_word(self, word: str) -> sqlite3.Row | None:
        with self._lock:
            r = self._db.execute("SELECT word, added_by, source FROM dictionary WHERE word = ?",
                                 (word.lower(),)).fetchone()
            if r is not None:
                self._db.execute("DELETE FROM dictionary WHERE word = ?", (word.lower(),))
                self._db.commit()
        self._dict_cache = None
        return r


class AutoCorrector:
    def __init__(self) -> None:
        self._spell = None

    @property
    def spell(self):
        if self._spell is None:
            from spellchecker import SpellChecker
            self._spell = SpellChecker(distance=1)
        return self._spell

    def is_known(self, word: str) -> bool:
        return bool(self.spell.known([word.lower()]))

    def _fix(self, word: str, protected: set[str]) -> str | None:
        low = word.lower()
        if len(word) < 4 or low in protected or low in SLANG or "'" in word:
            return None
        if any(c.isupper() for c in word[1:]) or _STRETCH_RE.search(low):
            return None
        if self.spell.known([low]):
            return None
        cand = self.spell.correction(low)
        if not cand or cand == low or " " in cand:
            return None
        return cand.capitalize() if word[0].isupper() else cand

    def correct(self, text: str, protected: set[str]) -> tuple[str, list[tuple[str, str]]]:
        """Returns (corrected text, [(original, replacement), ...])."""
        changes: list[tuple[str, str]] = []
        tags = [m.span() for m in _TAG_RE.finditer(text)]

        def repl(m: re.Match) -> str:
            start = m.start()
            if any(a <= start < b for a, b in tags) or (start > 0 and text[start - 1] in "@#:"):
                return m.group(0)
            fixed = self._fix(m.group(0), protected)
            if fixed is None:
                return m.group(0)
            changes.append((m.group(0), fixed))
            return fixed

        return _WORD_RE.sub(repl, text), changes


def apply_shortcuts(text: str, shortcuts: list[Shortcut]) -> tuple[str, list[int]]:
    """Replace whole-word/phrase matches (longest first). Returns (text, used ids)."""
    if not shortcuts:
        return text, []
    by_key = {s.input.lower(): s for s in shortcuts}
    keys = sorted(by_key, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)", re.IGNORECASE)
    used: list[int] = []

    def repl(m: re.Match) -> str:
        s = by_key[m.group(0).lower()]
        used.append(s.sid)
        return s.replacement

    return pattern.sub(repl, text), used


def ascii_table(rows: list[tuple[str, str, str]], header=("id", "input", "rephrase"), width=(4, 22, 40)) -> str:
    def cell(v: str, w: int) -> str:
        v = v.replace("`", "'").replace("\n", " ")
        return (v if len(v) <= w else v[: w - 1] + "…").ljust(w)
    lines = [" | ".join(cell(h, w) for h, w in zip(header, width)).rstrip(),
             "-+-".join("-" * w for w in width)]
    lines += [" | ".join(cell(v, w) for v, w in zip(r, width)).rstrip() for r in rows]
    return "```\n" + "\n".join(lines) + "\n```"
