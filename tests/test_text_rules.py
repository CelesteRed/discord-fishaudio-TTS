from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_fish_tts.commands import TTSCog
from discord_fish_tts.config_store import ConfigStore
from discord_fish_tts.text_rules import AutoCorrector, Shortcut, TextStore, apply_shortcuts, ascii_table

OWNER, FRIEND = 1, 5
AC = AutoCorrector()


def test_shortcuts_whole_words_case_insensitive_longest_first():
    scs = [Shortcut(1, "brb", "be right back"), Shortcut(2, "gn", "good night"),
           Shortcut(3, "gn all", "good night everyone")]
    out, used = apply_shortcuts("BRB, gn all! gnome", scs)
    assert out == "be right back, good night everyone! gnome"
    assert used == [1, 3]


def test_shortcut_store_ids_per_user(tmp_path: Path):
    st = TextStore(tmp_path / "t.db")
    assert st.add_shortcut(FRIEND, "brb", "be right back").sid == 1
    assert st.add_shortcut(FRIEND, "ty", "thank you").sid == 2
    assert st.add_shortcut(OWNER, "x", "y").sid == 1
    assert st.update_shortcut(FRIEND, 2, "thanks a lot").replacement == "thanks a lot"
    assert st.update_shortcut(FRIEND, 9, "nope") is None
    assert st.delete_shortcut(FRIEND, 1).input == "brb"
    assert [s.sid for s in st.shortcuts(FRIEND)] == [2]
    assert st.add_shortcut(FRIEND, "new", "one").sid == 3   # ids never reused
    assert st.find_shortcut(FRIEND, " TY ").sid == 2


def test_dictionary(tmp_path: Path):
    st = TextStore(tmp_path / "t.db")
    assert st.add_words(["Cosmiq", "lauging"], FRIEND) == ["cosmiq", "lauging"]
    assert st.add_words(["cosmiq"], OWNER) == []
    assert st.dictionary() == {"cosmiq", "lauging"}
    assert st.remove_word("LAUGING")["added_by"] == FRIEND
    assert st.dictionary() == {"cosmiq"}


def test_autocorrect_fixes_typos_but_leaves_chat_alone():
    out, fixes = AC.correct("I was lauging so hard becuase of you", set())
    assert out == "I was laughing so hard because of you"
    assert fixes == [("lauging", "laughing"), ("becuase", "because")]
    keep = "bruh lmao ngl dont LAUGING Lauging? @lauging [lauging] sooooo Starie"
    out, fixes = AC.correct(keep, set())
    assert ("LAUGING" in out and "@lauging" in out and "[lauging]" in out and "bruh lmao" in out
            and "sooooo" in out and "Starie" in out and "dont" in out)
    assert fixes == [("Lauging", "Laughing")]   # capitalised start-of-word typo is still fixed


def test_autocorrect_respects_protected_words():
    out, fixes = AC.correct("lauging wierd", {"lauging"})
    assert out == "lauging weird" and fixes == [("wierd", "weird")]


def test_ascii_table():
    t = ascii_table([("1", "brb", "be right back")])
    assert t.splitlines()[1].startswith("id   | input") and "1    | brb" in t


def _cog(tmp_path):
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.init_guild_gate([100])
    cfg.add_user(FRIEND)
    cfg.set_voice_id("v")
    store = TextStore(tmp_path / "t.db")
    reg = MagicMock()
    cog = TTSCog(bot=MagicMock(), voice_manager=MagicMock(), registry=reg, config_store=cfg,
                 owner_id_provider=lambda: OWNER, max_text_len=200, idle_timeout_s=0,
                 text_store=store, autocorrector=AC)
    return cog


def _it(uid, guild_id=100):
    it = MagicMock()
    it.user.id = uid
    it.guild_id = guild_id
    it.response.send_message = AsyncMock()
    return it


def test_prepare_text_order_and_toggle(tmp_path: Path):
    cog = _cog(tmp_path)
    cog.text_store.add_shortcut(FRIEND, "lolz", "laughing out loud")
    text, fixes, used = cog._prepare_text(FRIEND, "lolz that was wierd")
    assert text == "laughing out loud that was weird" and used == [1] and fixes == [("wierd", "weird")]
    cog.config.set_autocorrect(FRIEND, False)
    assert cog._prepare_text(FRIEND, "that was wierd")[0] == "that was wierd"


@pytest.mark.asyncio
async def test_shortcut_create_protects_words_and_shows_table(tmp_path: Path):
    cog = _cog(tmp_path)
    it = _it(FRIEND)
    await cog.shortcut_create.callback(cog, it, input="kekw", replacement="huge lmfaoo moment")
    msg = it.response.send_message.await_args.args[0]
    assert "Shortcut **#1** saved" in msg and "1    | kekw" in msg
    assert "lmfaoo" in cog.text_store.dictionary()
    it2 = _it(FRIEND)
    await cog.shortcut_create.callback(cog, it2, input="KEKW", replacement="x")
    assert "already have" in it2.response.send_message.await_args.args[0]
    it3 = _it(FRIEND)
    await cog.shortcut_replace.callback(cog, it3, id=1, rephrase="big laugh")
    assert cog.text_store.shortcuts(FRIEND)[0].replacement == "big laugh"


@pytest.mark.asyncio
async def test_stop_owner_vs_user(tmp_path: Path):
    cog = _cog(tmp_path)
    p = MagicMock()
    p.stop_all.return_value = 4
    p.stop_user.return_value = 2
    cog.registry.get.return_value = p
    it = _it(FRIEND)
    await cog.stop.callback(cog, it)
    p.stop_user.assert_called_once_with(FRIEND)
    assert "Stopped your 2 messages" in it.response.send_message.await_args.args[0]
    it = _it(OWNER)
    await cog.stop.callback(cog, it)
    p.stop_all.assert_called_once()
    assert "Stopped everyone in this server" in it.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_queue_embed(tmp_path: Path):
    from discord_fish_tts.mixer import Track
    from discord_fish_tts.tts_pipeline import TTSJob
    cog = _cog(tmp_path)
    playing = Track(TTSJob("hello there", FRIEND, 100, 1)); playing.started = True
    waiting = Track(TTSJob("next one", OWNER, 100, 1))
    p = MagicMock()
    p.snapshot.return_value = ([playing, waiting], [TTSJob("later @everyone", 0, 100, 1, source="server")])
    cog.registry.get.return_value = p
    it = _it(FRIEND)
    await cog.queue.callback(cog, it)
    d = it.response.send_message.await_args.kwargs["embed"].description
    assert "**Speaking now**" in d and "<@5>" in d and "hello there" in d
    assert d.index("next one") < d.index("later") and "**SERVER**" in d and "@\u200beveryone" in d
