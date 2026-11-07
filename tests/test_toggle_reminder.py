from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_fish_tts import commands as cmds
from discord_fish_tts.commands import TTSCog
from discord_fish_tts.config_store import ConfigStore
from discord_fish_tts.usage import UsageStore

OWNER, FRIEND, STRANGER = 1, 5, 9
GUILD, NOMIC = 100, 1567826418329587783


def _cog(tmp_path):
    cfg = ConfigStore(tmp_path / "c.json")
    cfg.init_guild_gate([GUILD])
    cfg.add_user(FRIEND)
    cfg.set_voice_id("v")
    notifier = MagicMock()
    notifier.usage = UsageStore(tmp_path / "u.db")
    bot = MagicMock()
    channel = MagicMock()
    edits: list[tuple[int, str]] = []

    def partial(mid):
        pm = MagicMock()
        pm.edit = AsyncMock(side_effect=lambda content: edits.append((mid, content)))
        return pm
    channel.get_partial_message = partial
    bot.get_channel = lambda cid: channel if cid == NOMIC else None
    cog = TTSCog(bot=bot, voice_manager=MagicMock(), registry=MagicMock(), config_store=cfg,
                 owner_id_provider=lambda: OWNER, max_text_len=200, idle_timeout_s=0, notifier=notifier)
    cog.registry.get_or_create.return_value.enqueue = AsyncMock()
    cog._voice_label = AsyncMock(return_value="**V**")
    return cog, edits


_next_id = [1000]


def _msg(uid, content="hello", in_voice=True, channel=NOMIC):
    m = MagicMock()
    m.author = MagicMock(spec=discord.Member)
    m.author.id = uid
    m.author.voice = MagicMock(channel=MagicMock(spec=discord.VoiceChannel)) if in_voice else None
    m.guild.id = GUILD
    m.channel.id = channel
    m.content = content
    _next_id[0] += 1
    m.reply = AsyncMock(return_value=MagicMock(id=_next_id[0]))
    return m


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(cmds.time, "time", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_reminder_shows_timestamp_and_is_tracked(tmp_path: Path, clock):
    cog, _ = _cog(tmp_path)
    first, second = _msg(FRIEND), _msg(FRIEND)
    await cog._handle_auto_tts(first)
    await cog._handle_auto_tts(second)
    text = first.reply.await_args.args[0]
    assert "`/toggle on`" in text
    assert text.endswith(f"\n-# will send again <t:{int(clock[0]) + 3600}:R>")
    assert "delete_after" not in first.reply.await_args.kwargs
    second.reply.assert_not_awaited()  # cooldown
    cog.registry.get_or_create.return_value.enqueue.assert_not_awaited()  # nothing spoken
    [row] = cog._usage_store.reminders(FRIEND)
    assert (row["channel_id"], row["message_id"], row["expired"]) == (NOMIC, _next_id[0] - 1, 0)


@pytest.mark.asyncio
async def test_expired_reminder_is_edited_then_can_send_again(tmp_path: Path, clock):
    cog, edits = _cog(tmp_path)
    m1 = _msg(FRIEND)
    await cog._handle_auto_tts(m1)
    clock[0] += 1800
    assert await cog.expire_reminders() == 0 and edits == []  # not yet
    clock[0] += 1801
    assert await cog.expire_reminders() == 1
    mid = m1.reply.return_value.id
    assert edits == [(mid, f"🔇 <@{FRIEND}>, Auto TTS is turned **off** for you, so this wasn't spoken. "
                           "Run `/toggle on` to turn it on.\n-# will remind you next time")]
    assert await cog.expire_reminders() == 0  # edited once only
    m2 = _msg(FRIEND)
    await cog._handle_auto_tts(m2)
    m2.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_two_most_recent_reminders_kept_per_user(tmp_path: Path, clock):
    cog, _ = _cog(tmp_path)
    ids = []
    for _ in range(3):
        m = _msg(FRIEND)
        await cog._handle_auto_tts(m)
        ids.append(m.reply.return_value.id)
        clock[0] += 3601
        await cog.expire_reminders()
    assert [r["message_id"] for r in cog._usage_store.reminders(FRIEND)] == [ids[2], ids[1]]


@pytest.mark.asyncio
async def test_toggle_on_expires_reminder_early(tmp_path: Path, clock):
    cog, edits = _cog(tmp_path)
    m = _msg(FRIEND)
    await cog._handle_auto_tts(m)
    it = MagicMock()
    it.user.id = FRIEND
    it.response.send_message = AsyncMock()
    await cog.toggle.callback(cog, it, state="on")
    assert edits and edits[0][1].endswith("-# will remind you next time")
    assert cog._usage_store.last_reminder_ts(FRIEND) is None
    await cog.toggle.callback(cog, it, state="off")
    again = _msg(FRIEND)
    await cog._handle_auto_tts(again)
    again.reply.assert_awaited_once()  # fresh reminder right away


@pytest.mark.asyncio
async def test_no_reminder_for_non_whitelisted_other_channels_or_not_in_voice(tmp_path: Path, clock):
    cog, _ = _cog(tmp_path)
    for m in (_msg(STRANGER), _msg(FRIEND, channel=123), _msg(FRIEND, in_voice=False),
              _msg(FRIEND, content="/toggle on")):
        await cog._handle_auto_tts(m)
        m.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_toggle_on_off_argument(tmp_path: Path, clock):
    cog, _ = _cog(tmp_path)
    it = MagicMock()
    it.user.id = FRIEND
    it.response.send_message = AsyncMock()
    for state, expected in (("on", True), ("on", True), ("off", False), (None, True)):
        await cog.toggle.callback(cog, it, state=state)
        assert cog.config.is_auto_tts(FRIEND) is expected
