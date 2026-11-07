from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_fish_tts.commands import TTSCog
from discord_fish_tts.config_store import ConfigStore

OWNER, ADMIN, MEMBER = 1, 2, 3
GUILD, OTHER = 100, 200
CH_A, CH_B, CH_ELSEWHERE = 11, 12, 21


def test_voice_order_user_then_server_then_global(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.set_voice_id("global")
    assert cfg.get_voice_for(MEMBER, GUILD) == "global"
    cfg.set_guild_default_voice(GUILD, "https://fish.audio/app/m/0123456789abcdef0123456789abcdef/")
    assert cfg.get_voice_for(MEMBER, GUILD) == "0123456789abcdef0123456789abcdef"
    assert cfg.get_voice_for(MEMBER, OTHER) == "global"
    cfg.set_user_voice(MEMBER, "mine")
    assert cfg.get_voice_for(MEMBER, GUILD) == "mine"
    cfg.set_guild_default_voice(GUILD, None)
    assert ConfigStore(tmp_path / "c.json").guild_default_voice(GUILD) is None


def test_toggles_follow_server_default_until_chosen(tmp_path: Path):
    path = tmp_path / "c.json"
    cfg = ConfigStore(path)
    assert not cfg.is_auto_tts(MEMBER, GUILD) and cfg.is_autocorrect(MEMBER, GUILD)
    cfg.set_guild_setting(GUILD, "auto_speak_default", True)
    cfg.set_guild_setting(GUILD, "autocorrect_default", False)
    assert cfg.is_auto_tts(MEMBER, GUILD) and not cfg.is_auto_tts(MEMBER, OTHER)
    assert not cfg.is_autocorrect(MEMBER, GUILD) and cfg.is_autocorrect(MEMBER, OTHER)
    cfg.set_auto_tts(MEMBER, False)           # personal choice beats the server default
    cfg.set_autocorrect(MEMBER, True)
    again = ConfigStore(path)
    assert not again.is_auto_tts(MEMBER, GUILD) and again.is_autocorrect(MEMBER, GUILD)
    again.set_auto_tts(MEMBER, None)          # back to "follow the server"
    assert again.is_auto_tts(MEMBER, GUILD) and again.auto_tts_choice(MEMBER) is None


def test_existing_toggle_users_keep_their_choice(tmp_path: Path):
    import json
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"auto_tts_users": [MEMBER], "autocorrect_off_users": [ADMIN]}))
    cfg = ConfigStore(path)
    cfg.set_guild_setting(GUILD, "auto_speak_default", False)
    assert cfg.is_auto_tts(MEMBER, GUILD) and not cfg.is_autocorrect(ADMIN, GUILD)


def _guild(gid, channels):
    g = MagicMock(id=gid, voice_client=None)
    g.name = "Home" if gid == GUILD else "Other"
    g.get_channel = lambda cid: MagicMock(id=cid) if cid in channels else None
    return g


def _cog(tmp_path):
    cfg = ConfigStore(tmp_path / "c.json", default_auto_channels=())
    cfg.init_guild_gate([GUILD])
    cfg.set_voice_id("global")
    for u in (ADMIN, MEMBER):
        cfg.add_user(u)
    guilds = {GUILD: _guild(GUILD, {CH_A, CH_B}), OTHER: _guild(OTHER, {CH_ELSEWHERE})}
    bot = MagicMock()
    bot.user.name = "Starie"
    bot.get_guild = lambda gid: guilds.get(gid)
    cog = TTSCog(bot=bot, voice_manager=MagicMock(), registry=MagicMock(), config_store=cfg,
                 owner_id_provider=lambda: OWNER, max_text_len=200, idle_timeout_s=0)
    cog._lookup_voice = AsyncMock(return_value=(True, "Verity"))
    cog.registry.get_or_create.return_value.enqueue = AsyncMock()
    return cog


def _it(uid, manage=False):
    it = MagicMock()
    it.user.id = uid
    it.user.guild_permissions = discord.Permissions(manage_guild=manage)
    it.guild_id = GUILD
    it.response.send_message = AsyncMock()
    it.response.defer = AsyncMock()
    it.followup.send = AsyncMock()
    return it


def _channel(cid):
    ch = MagicMock(id=cid, mention=f"<#{cid}>")
    ch.name = f"ch{cid}"
    return ch


@pytest.mark.asyncio
async def test_config_needs_manage_server_or_owner(tmp_path: Path):
    cog = _cog(tmp_path)
    it = _it(MEMBER)
    await cog.config_channel_add.callback(cog, it, channel=_channel(CH_A))
    assert "Manage Server" in it.response.send_message.await_args.args[0]
    assert not cog.config.is_auto_tts_channel(CH_A)
    for it in (_it(ADMIN, manage=True), _it(OWNER)):
        await cog.config_channel_add.callback(cog, it, channel=_channel(CH_A))
    assert cog.config.is_auto_tts_channel(CH_A)


@pytest.mark.asyncio
async def test_multiple_channels_listed_per_server(tmp_path: Path):
    cog = _cog(tmp_path)
    cog.config.add_auto_tts_channel(CH_ELSEWHERE)    # another server's channel
    for cid in (CH_A, CH_B):
        await cog.config_channel_add.callback(cog, _it(ADMIN, True), channel=_channel(cid))
    it = _it(MEMBER)
    await cog.config_show.callback(cog, it)
    d = it.response.send_message.await_args.kwargs["embed"].description
    assert f"<#{CH_A}>" in d and f"<#{CH_B}>" in d and f"<#{CH_ELSEWHERE}>" not in d
    await cog.config_channel_remove.callback(cog, _it(ADMIN, True), channel=_channel(CH_B))
    assert not cog.config.is_auto_tts_channel(CH_B) and cog.config.is_auto_tts_channel(CH_A)


@pytest.mark.asyncio
async def test_defaults_and_server_voice(tmp_path: Path):
    cog = _cog(tmp_path)
    admin = _it(ADMIN, True)
    await cog.config_auto_speak_default.callback(cog, admin, state="on")
    await cog.config_auto_correct_default.callback(cog, _it(ADMIN, True), state="off")
    await cog.config_server_voice.callback(cog, _it(ADMIN, True), voice="0123456789abcdef0123456789abcdef")
    assert cog.config.is_auto_tts(MEMBER, GUILD) and not cog.config.is_autocorrect(MEMBER, GUILD)
    assert cog.config.get_voice_for(MEMBER, GUILD) == "0123456789abcdef0123456789abcdef"
    d = admin.response.send_message.await_args.kwargs["embed"].description
    assert "**Auto-speak default:** on" in d
    await cog.config_server_voice.callback(cog, _it(ADMIN, True), voice=None)
    assert cog.config.get_voice_for(MEMBER, GUILD) == "global"


@pytest.mark.asyncio
async def test_auto_speak_default_on_speaks_for_members_who_never_toggled(tmp_path: Path):
    cog = _cog(tmp_path)
    cog.config.add_auto_tts_channel(CH_A)
    cog.config.set_guild_setting(GUILD, "auto_speak_default", True)
    cog.voice_manager.current_channel_id.return_value = 77

    def msg(uid):
        m = MagicMock()
        m.author = MagicMock(spec=discord.Member)
        m.author.id = uid
        m.author.voice = MagicMock(channel=MagicMock(spec=discord.VoiceChannel, id=77))
        m.guild.id = GUILD
        m.channel.id = CH_A
        m.content = m.clean_content = "hello there"
        m.reply = AsyncMock()
        return m

    await cog._handle_auto_tts(msg(MEMBER))
    assert cog.registry.get_or_create.return_value.enqueue.await_count == 1
    cog.config.set_auto_tts(ADMIN, False)
    await cog._handle_auto_tts(msg(ADMIN))       # chose off: not spoken (gets the reminder instead)
    assert cog.registry.get_or_create.return_value.enqueue.await_count == 1


@pytest.mark.asyncio
async def test_toggle_default_resets_personal_choice(tmp_path: Path):
    cog = _cog(tmp_path)
    cog._voice_label = AsyncMock(return_value="**V**")
    cog.config.set_guild_setting(GUILD, "auto_speak_default", True)
    it = _it(MEMBER)
    await cog.toggle.callback(cog, it, state="off")
    assert cog.config.auto_tts_choice(MEMBER) is False
    it = _it(MEMBER)
    await cog.toggle.callback(cog, it, state="default")
    assert cog.config.auto_tts_choice(MEMBER) is None and cog.config.is_auto_tts(MEMBER, GUILD)
    assert "following this server's default" in it.response.send_message.await_args.args[0]


def test_command_tree_shape():
    names = {c.name: c for c in TTSCog.__cog_app_commands__}
    assert "enable" not in names and "disable" not in names and "ttschannel" not in names
    assert {c.name for c in names["servers"].commands} == {"list", "view", "enable", "disable"}
    assert {c.name for c in names["config"].commands} == {
        "show", "channel-add", "channel-remove", "auto-speak-default", "auto-correct-default", "server-voice"}
    assert names["config"].default_permissions.manage_guild
    for group in (names["servers"], names["config"]):
        for c in group.commands:
            assert len(c.description) <= 100
