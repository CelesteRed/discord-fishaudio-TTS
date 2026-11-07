from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_fish_tts.commands import TTSCog
from discord_fish_tts.config_store import ConfigStore
from discord_fish_tts.notifier import ServerNotifier
from discord_fish_tts.usage import UsageStore

OWNER, FRIEND = 1, 5
HOME, NEW = 1555774798213161072, 2222222222222222222


def test_gate_seeds_existing_servers_and_persists(tmp_path: Path):
    path = tmp_path / "c.json"
    cfg = ConfigStore(path)
    assert not cfg.guild_gate_initialized()
    cfg.init_guild_gate([HOME])
    assert cfg.is_guild_enabled(HOME) and not cfg.is_guild_enabled(NEW)
    assert cfg.enable_guild(NEW) and not cfg.enable_guild(NEW)
    assert cfg.disable_guild(HOME)
    again = ConfigStore(path)
    assert again.guild_gate_initialized()
    assert again.list_enabled_guilds() == [NEW]


def test_disabling_everything_keeps_gate_initialised(tmp_path: Path):
    path = tmp_path / "c.json"
    cfg = ConfigStore(path)
    cfg.init_guild_gate([HOME])
    cfg.disable_guild(HOME)
    again = ConfigStore(path)
    assert again.guild_gate_initialized() and again.list_enabled_guilds() == []


def _member(uid, channel=None):
    m = MagicMock(id=uid)
    m.voice = MagicMock(channel=channel) if channel else None
    return m


def _guild(gid, name, members=None, bot_channel=None):
    g = MagicMock(id=gid, member_count=3, owner_id=99, icon=None)
    g.name = name
    members = members or {}
    g.get_member = lambda uid: members.get(uid)
    g.voice_client = MagicMock(channel=bot_channel) if bot_channel else None
    return g


def _cog(tmp_path, guilds, enabled=(HOME,)):
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.init_guild_gate(enabled)
    cfg.add_user(FRIEND)
    bot = MagicMock()
    bot.user.name = "Starie"
    bot.user.id = 4242
    bot.guilds = guilds
    bot.get_guild = lambda gid: next((g for g in guilds if g.id == gid), None)
    owner = MagicMock()
    owner.send = AsyncMock()
    bot.get_user = lambda uid: owner if uid == OWNER else None
    notifier = MagicMock()
    notifier.usage = UsageStore(tmp_path / "u.db")
    cog = TTSCog(bot=bot, voice_manager=MagicMock(), registry=MagicMock(), config_store=cfg,
                 owner_id_provider=lambda: OWNER, max_text_len=200, idle_timeout_s=0, notifier=notifier)
    return cog, owner


def _interaction(user_id, guild_id, command):
    it = MagicMock()
    it.user.id = user_id
    it.guild_id = guild_id
    it.command.qualified_name = command
    it.response.send_message = AsyncMock()
    return it


@pytest.mark.asyncio
async def test_commands_blocked_in_disabled_server(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Home"), _guild(NEW, "New")])
    it = _interaction(FRIEND, NEW, "say")
    assert await cog.interaction_check(it) is False
    assert "isn't enabled in this server" in it.response.send_message.await_args.args[0]
    assert await cog.interaction_check(_interaction(FRIEND, HOME, "say")) is True
    assert await cog.interaction_check(_interaction(FRIEND, None, "voice set")) is True  # DMs
    assert await cog.interaction_check(_interaction(OWNER, NEW, "servers enable")) is True
    assert await cog.interaction_check(_interaction(OWNER, NEW, "say")) is False  # owner too, except mgmt


@pytest.mark.asyncio
async def test_enable_and_disable_commands(tmp_path: Path):
    new = _guild(NEW, "New Place", bot_channel=MagicMock())
    new.voice_client.disconnect = AsyncMock()
    new.leave = AsyncMock()
    cog, _ = _cog(tmp_path, [_guild(HOME, "Home"), new])
    cog.registry.get_or_create.return_value.skip_current = AsyncMock()

    it = _interaction(OWNER, None, "servers enable")
    await cog.servers_enable.callback(cog, it, server=str(NEW))
    assert cog.config.is_guild_enabled(NEW)
    assert "Enabled in **New Place**" in it.response.send_message.await_args.args[0]

    it = _interaction(OWNER, None, "servers disable")
    await cog.servers_disable.callback(cog, it, server=str(NEW), leave=True)
    assert not cog.config.is_guild_enabled(NEW)
    new.voice_client.disconnect.assert_awaited()
    new.leave.assert_awaited()

    it = _interaction(FRIEND, None, "servers enable")
    await cog.servers_enable.callback(cog, it, server=str(HOME))
    assert "Only the bot owner" in it.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_gate_autocomplete_offers_the_right_servers(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Home"), _guild(NEW, "New Place")])
    owner = _interaction(OWNER, None, "servers enable")
    assert [c.value for c in await cog._enable_autocomplete(owner, "")] == [str(NEW)]
    assert [c.value for c in await cog._disable_autocomplete(owner, "")] == [str(HOME)]


@pytest.mark.asyncio
async def test_join_dms_owner_once_including_offline_catch_up(tmp_path: Path):
    new = _guild(NEW, "New Place")
    cog, owner = _cog(tmp_path, [_guild(HOME, "Home"), new])

    async def no_audit(**kw):
        raise __import__("discord").Forbidden(MagicMock(status=403), "no")
        yield  # pragma: no cover
    new.audit_logs = no_audit
    await cog.notify_unseen_guilds()
    await cog.notify_unseen_guilds()  # restart: no repeat
    assert owner.send.await_count == 1
    embed = owner.send.await_args.kwargs["embed"]
    assert "New Place" in embed.title and f"/servers enable server:{NEW}" in embed.description


def test_dm_speech_prefers_server_where_bot_shares_the_channel(tmp_path: Path):
    ch_home, ch_new = MagicMock(id=10), MagicMock(id=20)
    home = _guild(HOME, "Home", members={FRIEND: _member(FRIEND, ch_home)})
    new = _guild(NEW, "New", members={FRIEND: _member(FRIEND, ch_new)}, bot_channel=ch_new)
    cog, _ = _cog(tmp_path, [home, new], enabled=(HOME, NEW))
    assert cog._find_user_voice_anywhere(FRIEND) == (NEW, 20)
    cog.config.disable_guild(NEW)
    assert cog._find_user_voice_anywhere(FRIEND) == (HOME, 10)


@pytest.mark.asyncio
async def test_auto_speak_ignored_in_disabled_server(tmp_path: Path):
    import discord
    cog, _ = _cog(tmp_path, [])
    msg = MagicMock()
    msg.author = MagicMock(spec=discord.Member)
    msg.author.id = FRIEND
    msg.guild.id = NEW
    cog.config.set_auto_tts(FRIEND, True)
    cog.registry.get_or_create.return_value.enqueue = AsyncMock()
    await cog._handle_auto_tts(msg)
    cog.registry.get_or_create.return_value.enqueue.assert_not_awaited()


def test_server_announcements_skip_disabled_servers(tmp_path: Path):
    listener = MagicMock(bot=False)
    vc = MagicMock()
    vc.channel.members = [listener]
    vc.channel.name = "Lounge"
    home, new = _guild(HOME, "Home"), _guild(NEW, "New")
    home.voice_client = vc
    new.voice_client = vc
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.init_guild_gate([HOME])
    bot = MagicMock(guilds=[home, new])
    n = ServerNotifier(bot, MagicMock(), cfg, lambda: OWNER, "k", "s2.1-pro-free")
    assert [t[0] for t in n.voice_targets()] == [HOME]


# ---- commands-only servers + name matching ----

CMD_ONLY = 1533366845212721213


@pytest.mark.asyncio
async def test_enable_by_name_ignores_emoji_and_case(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Cosmiq Studio"), _guild(NEW, "🎃 Cosmiq")])
    it = _interaction(OWNER, None, "servers enable")
    await cog.servers_enable.callback(cog, it, server="cosmiq")     # exact folded match wins over "Cosmiq Studio"
    assert cog.config.is_guild_enabled(NEW)
    assert "Enabled in **🎃 Cosmiq** (`2222222222222222222`)" in it.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_ambiguous_name_lists_candidates(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Cosmiq Studio"), _guild(NEW, "Cosmiq Lounge")])
    it = _interaction(OWNER, None, "servers enable")
    await cog.servers_enable.callback(cog, it, server="cosmiq")
    msg = it.response.send_message.await_args.args[0]
    assert "Several servers match" in msg and "Cosmiq Lounge" in msg and "Cosmiq Studio" in msg


@pytest.mark.asyncio
async def test_commands_only_server_is_detected_and_explained(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Home")])
    cog.bot.application_id = 4242
    friend = _interaction(FRIEND, CMD_ONLY, "say")
    assert await cog.interaction_check(friend) is False
    assert "commands only" in friend.response.send_message.await_args.args[0]
    assert "client_id" not in friend.response.send_message.await_args.args[0]   # link is owner-only
    assert CMD_ONLY in cog._commands_only_guilds()
    owner = _interaction(OWNER, CMD_ONLY, "say")
    assert await cog.interaction_check(owner) is False
    assert f"guild_id={CMD_ONLY}" in owner.response.send_message.await_args.args[0]
    assert await cog.interaction_check(_interaction(OWNER, CMD_ONLY, "servers enable")) is True


@pytest.mark.asyncio
async def test_enable_commands_only_id_pre_enables_with_invite(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Home")])
    cog.bot.application_id = 4242
    cog._note_commands_only(CMD_ONLY)
    it = _interaction(OWNER, None, "servers enable")
    await cog.servers_enable.callback(cog, it, server=str(CMD_ONLY))
    msg = it.response.send_message.await_args.args[0]
    assert cog.config.is_guild_enabled(CMD_ONLY)
    assert "Pre-enabled" in msg and "scope=bot+applications.commands" in msg and f"guild_id={CMD_ONLY}" in msg


@pytest.mark.asyncio
async def test_unknown_server_lists_servers_and_invite(tmp_path: Path):
    cog, _ = _cog(tmp_path, [_guild(HOME, "Home")])
    cog.bot.application_id = 4242
    it = _interaction(OWNER, None, "servers enable")
    await cog.servers_enable.callback(cog, it, server="9999999999999999999")
    msg = it.response.send_message.await_args.args[0]
    assert "not in a server matching" in msg and "**Home**" in msg and "scope=bot" in msg


@pytest.mark.asyncio
async def test_servers_shows_enabled_disabled_and_commands_only(tmp_path: Path):
    home, new = _guild(HOME, "Home"), _guild(NEW, "New Place")
    for g in (home, new):
        g.voice_client = None
        g.get_channel = lambda cid: None
    cog, _ = _cog(tmp_path, [home, new])
    cog.bot.application_id = 4242
    cog._note_commands_only(CMD_ONLY)
    it = _interaction(OWNER, None, "servers list")
    await cog.servers_list.callback(cog, it)
    d = it.response.send_message.await_args.kwargs["embed"].description
    assert "**✅ Enabled (1)**" in d and "**Home**" in d
    assert "**⛔ Not enabled (1)**" in d and "**New Place**" in d and f"/servers enable server:{NEW}" in d
    assert "Commands only" in d and str(CMD_ONLY) in d
