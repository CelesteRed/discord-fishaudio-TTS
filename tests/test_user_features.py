from __future__ import annotations

import json
from pathlib import Path

from discord_fish_tts.commands import speakable_text
from discord_fish_tts.config_store import DEFAULT_AUTO_TTS_CHANNELS, ConfigStore

V1 = "906b8b47c30d4ba2a7f1287773e8a3ed"
V2 = "a7b9531163114aba9b7ae8406e7d49fd"


def test_user_voice_falls_back_to_default(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "c.json")
    assert cfg.get_voice_for(1) is None
    cfg.set_voice_id(V1)
    assert cfg.get_voice_for(1) == V1
    cfg.set_user_voice(1, f"https://fish.audio/app/m/{V2}/")
    assert cfg.get_user_voice(1) == V2
    assert cfg.get_voice_for(1) == V2
    assert cfg.get_voice_for(2) == V1  # others keep the default
    cfg.set_user_voice(1, None)
    assert cfg.get_voice_for(1) == V1


def test_user_voices_and_toggles_persist(tmp_path: Path):
    path = tmp_path / "c.json"
    cfg = ConfigStore(path)
    cfg.add_user(5)
    cfg.set_user_voice(5, V2)
    cfg.set_auto_tts(5, True)
    cfg.add_auto_tts_channel(42)
    again = ConfigStore(path)
    assert again.get_user_voice(5) == V2
    assert again.is_auto_tts(5)
    assert again.is_auto_tts_channel(42)
    assert again.is_auto_tts_channel(DEFAULT_AUTO_TTS_CHANNELS[0])
    raw = json.loads(path.read_text())
    assert raw["user_voices"] == {"5": V2}


def test_default_channel_used_only_when_config_has_none(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"whitelist": [1]}))  # pre-feature config file
    assert ConfigStore(path).list_auto_tts_channels() == list(DEFAULT_AUTO_TTS_CHANNELS)
    cfg = ConfigStore(path)
    cfg.remove_auto_tts_channel(DEFAULT_AUTO_TTS_CHANNELS[0])
    assert ConfigStore(path).list_auto_tts_channels() == []  # removal sticks


def test_removing_user_turns_off_their_toggle(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.add_user(7)
    cfg.set_auto_tts(7, True)
    cfg.remove_user(7)
    assert not cfg.is_auto_tts(7)


def test_speakable_text_cleans_chat():
    assert speakable_text("hey <:pog:123456> check https://x.com/a?b=1 ok", 200) == "hey pog check link ok"
    assert speakable_text("the answer is ||secret|| lol", 200) == "the answer is spoiler lol"
    assert speakable_text("   \n  ", 200) == ""
    long = "word " * 100
    out = speakable_text(long, 200)
    assert len(out) <= 200 and not out.endswith(" ") and out.endswith("word")
