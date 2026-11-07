from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_fish_tts.notifier import ServerNotifier
from discord_fish_tts.tts_pipeline import TTSJob
from discord_fish_tts.usage import UsageStore, month_start_ts

NOV = datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()
OCT = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()


def test_month_start():
    assert month_start_ts(NOV) == datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()


def test_record_counts_utf8_bytes_and_ranks(tmp_path: Path):
    u = UsageStore(tmp_path / "u.db")
    assert u.record(user_id=1, guild_id=9, channel_id=5, source="say", voice_id="v", text="héllo", ts=NOV) == 6
    u.record(user_id=2, guild_id=9, channel_id=5, source="auto", voice_id="w", text="a" * 50, ts=OCT)
    u.record(user_id=2, guild_id=9, channel_id=5, source="say", voice_id="w", text="bb", ts=NOV, skipped=True)
    ranking = u.ranking(now=NOV)
    assert [r.user_id for r in ranking] == [2, 1]
    assert ranking[0].messages == 2 and ranking[0].bytes == 52 and ranking[0].month_bytes == 2
    assert u.totals() == (3, 58)
    assert u.bytes_since(month_start_ts(NOV)) == 8


def test_user_summary_and_recent(tmp_path: Path):
    u = UsageStore(tmp_path / "u.db")
    for i, src in enumerate(["say", "auto", "auto"]):
        u.record(user_id=7, guild_id=9, channel_id=5, source=src, voice_id="v", text=f"msg {i}",
                 audio_ms=1000, ts=NOV + i)
    s = u.user_summary(7, now=NOV)
    assert s["messages"] == 3 and s["month_messages"] == 3 and s["audio_ms"] == 3000
    assert s["sources"][0] == ("auto", 2)
    assert [r["text"] for r in u.recent(7, 2)] == ["msg 2", "msg 1"]
    assert u.user_summary(999)["messages"] == 0


def test_meta_roundtrip(tmp_path: Path):
    u = UsageStore(tmp_path / "u.db")
    assert u.get_meta("x") is None
    u.set_meta("x", "1"); u.set_meta("x", "2")
    assert u.get_meta("x") == "2"


def _notifier(tmp_path, model="s2.1-pro-free", budget=1000):
    bot = MagicMock()
    bot.change_presence = AsyncMock()
    return ServerNotifier(bot, MagicMock(), MagicMock(), lambda: 1, "k", model,
                          usage=UsageStore(tmp_path / "u.db"), usage_budget_bytes=budget)


def test_status_never_rounds_up_to_100():
    assert ServerNotifier.format_status(99.9928) == "Usage: 99.9% remaining"
    assert ServerNotifier.format_status(100.0) == "Usage: 100% remaining"
    assert ServerNotifier.format_status(42.9) == "Usage: 42% remaining"


def test_status_text_from_monthly_budget(tmp_path: Path):
    n = _notifier(tmp_path, budget=1000)
    assert n.format_status(n.usage_report()["pct"]) == "Usage: 100% remaining"
    n.usage.record(user_id=1, guild_id=1, channel_id=1, source="say", voice_id="v", text="x" * 5)
    assert n.format_status(n.usage_report()["pct"]) == "Usage: 99.5% remaining"
    n.usage.record(user_id=1, guild_id=1, channel_id=1, source="say", voice_id="v", text="x" * 400)
    assert n.format_status(n.usage_report()["pct"]) == "Usage: 59% remaining"


@pytest.mark.asyncio
async def test_status_from_credit_on_paid_model_and_topup_reset(tmp_path: Path):
    n = _notifier(tmp_path, model="s2.1-pro")
    n.alert = AsyncMock()
    n.fetch_credit = AsyncMock(return_value=10.0)
    await n.check_credit_once()
    n.fetch_credit.return_value = 4.0
    await n.check_credit_once()
    assert n.usage_report()["kind"] == "credit" and round(n.usage_report()["pct"]) == 40
    n.fetch_credit.return_value = 6.0  # top-up after spending
    await n.check_credit_once()
    assert round(n.usage_report()["pct"]) == 100


@pytest.mark.asyncio
async def test_on_usage_records_job_and_sets_presence(tmp_path: Path):
    n = _notifier(tmp_path, budget=100)
    n._last_status_at = -1e9
    n.on_usage(TTSJob("hello", 5, 1, 2, source="auto"), "voice-x", 1200, False)
    row = n.usage.recent(5, 1)[0]
    assert (row["source"], row["text"]) == ("auto", "hello")
    await asyncio.sleep(0.05)
    activity = n.bot.change_presence.await_args.kwargs["activity"]
    assert activity.name == "Usage: 95% remaining"


def _guild(gid, name, members=()):
    g = MagicMock(id=gid, member_count=10, owner_id=1, voice_client=None, icon=None)
    g.name = name
    g.get_member = lambda uid: MagicMock(joined_at=None, voice=None) if uid in members else None
    g.get_channel = lambda cid: None
    g.me = None
    return g


def _cog(tmp_path):
    from discord_fish_tts.commands import TTSCog
    from discord_fish_tts.config_store import ConfigStore
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.set_voice_id("default-voice")
    cfg.set_server_voice("morgan")
    cfg.add_user(5); cfg.add_user(6)
    cfg.set_user_voice(5, "verity")
    n = _notifier(tmp_path, budget=1000)
    bot = MagicMock()
    bot.user.name = "Starie"
    g1, g2 = _guild(9, "Cosmiq Studio", members=(5, 6)), _guild(8, "Other Place")
    bot.guilds = [g1, g2]
    bot.get_guild = lambda gid: {9: g1, 8: g2}.get(gid)
    bot.get_user = lambda uid: None
    lemon = MagicMock(id=5, display_name="Lemon")
    lemon.name = "lemon"
    bot.fetch_user = AsyncMock(return_value=lemon)
    cog = TTSCog(bot=bot, voice_manager=MagicMock(), registry=MagicMock(), config_store=cfg,
                 owner_id_provider=lambda: 1, max_text_len=200, idle_timeout_s=0, notifier=n)
    cog._lookup_voice = AsyncMock(side_effect=lambda vid: (True, vid.title()))
    for uid, gid, text in [(5, 9, "hello there"), (5, 9, "again"), (0, 9, "server says hi"),
                           (1, 8, "owner"), (5, 8, "elsewhere")]:
        n.usage.record(user_id=uid, guild_id=gid, channel_id=3, source="say", voice_id="x", text=text)
    return cog


def test_guild_totals_and_user_guilds(tmp_path: Path):
    u = _cog(tmp_path).notifier.usage
    g = u.guild_totals()
    assert g[9]["messages"] == 3 and g[9]["users"] == 2 and g[9]["top_user"] == 5
    assert g[8]["bytes"] == 14 and g[8]["top_user"] == 5
    assert u.user_guilds(5) == [(9, 2, 16), (8, 1, 9)]
    assert [r.user_id for r in u.ranking(guild_id=8)] == [5, 1]
    assert u.totals(8) == (2, 14)


@pytest.mark.asyncio
async def test_view_overview_lists_every_server(tmp_path: Path):
    cog = _cog(tmp_path)
    d = (await cog._view_overview()).description
    assert d.startswith("**Usage: 95% remaining**")
    assert "**🌐 Servers (2)**" in d
    assert d.index("**Cosmiq Studio**") < d.index("**Other Place**")  # most usage first
    assert d.index("<@5>") < d.index("**SERVER**") < d.index("<@1>")
    assert "**Verity**" in d and "**Morgan**" in d and "*(default)*" in d
    assert "*No usage yet:* <@6>" in d


@pytest.mark.asyncio
async def test_view_server_embed(tmp_path: Path):
    cog = _cog(tmp_path)
    e = await cog._view_server(8)
    assert e.title == "🎙️ Other Place"
    assert e.description.startswith("2 msgs · 14 B")
    assert e.description.index("<@5>") < e.description.index("<@1>")


@pytest.mark.asyncio
async def test_view_user_embed_from_dm(tmp_path: Path):
    cog = _cog(tmp_path)
    e = await cog._view_user(5)
    fields = {f.name: f.value for f in e.fields}
    assert e.title == "🎙️ Lemon"
    assert "✅ whitelisted" in e.description and "**Verity**" in e.description
    assert fields["Messages"].startswith("3 total")
    assert fields["Servers"] == "Cosmiq Studio: 2 msgs · 16 B\nOther Place: 1 msgs · 9 B"
    assert "elsewhere" in fields["Recent messages"]
    server = await cog._view_user(0)
    assert server.title == "🎙️ SERVER" and "**Morgan**" in server.description


@pytest.mark.asyncio
async def test_view_autocomplete_and_id_parsing(tmp_path: Path):
    cog = _cog(tmp_path)
    owner = MagicMock(); owner.user.id = 1
    stranger = MagicMock(); stranger.user.id = 77
    names = [c.name for c in await cog._view_server_autocomplete(owner, "other")]
    assert names == ["Other Place (disabled)"]  # gate not initialised in this fixture
    assert await cog._view_user_autocomplete(stranger, "") == []
    values = [c.value for c in await cog._view_user_autocomplete(owner, "")]
    assert set(values) >= {"1", "5", "6", "0"}
    assert cog._parse_id("<@1057049596334977065>") == 1057049596334977065
    assert cog._parse_id("server") == 0 and cog._parse_id("nobody") is None


@pytest.mark.asyncio
async def test_owner_dm_stats_shortcut(tmp_path: Path):
    cog = _cog(tmp_path)
    msg = MagicMock(content=" Stats ")
    msg.author.id = 1
    msg.reply = AsyncMock()
    await cog._handle_dm(msg)
    embed = msg.reply.await_args.kwargs["embed"]
    assert "every server" in embed.title
