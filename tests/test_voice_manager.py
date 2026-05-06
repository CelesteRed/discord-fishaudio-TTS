import pytest
from unittest.mock import AsyncMock, MagicMock
from discord_fish_tts.voice_manager import VoiceManager


@pytest.mark.asyncio
async def test_ensure_in_channel_connects_when_not_connected():
    bot = MagicMock()
    guild = MagicMock()
    guild.voice_client = None
    channel = MagicMock()
    channel.connect = AsyncMock(return_value=MagicMock(name="VoiceClient"))
    guild.get_channel = MagicMock(return_value=channel)
    bot.get_guild = MagicMock(return_value=guild)

    mgr = VoiceManager(bot)
    vc = await mgr.ensure_in_channel(123, 456)
    channel.connect.assert_awaited_once()
    assert vc is channel.connect.return_value


@pytest.mark.asyncio
async def test_ensure_in_channel_moves_when_in_wrong_channel():
    bot = MagicMock()
    guild = MagicMock()
    target = MagicMock()
    target.id = 456
    other = MagicMock()
    other.id = 999
    existing_vc = MagicMock()
    existing_vc.channel = other
    existing_vc.move_to = AsyncMock()
    guild.voice_client = existing_vc
    guild.get_channel = MagicMock(return_value=target)
    bot.get_guild = MagicMock(return_value=guild)

    mgr = VoiceManager(bot)
    vc = await mgr.ensure_in_channel(123, 456)
    existing_vc.move_to.assert_awaited_once_with(target)
    assert vc is existing_vc


@pytest.mark.asyncio
async def test_ensure_in_channel_noop_when_already_correct():
    bot = MagicMock()
    guild = MagicMock()
    target = MagicMock()
    target.id = 456
    existing_vc = MagicMock()
    existing_vc.channel = target
    existing_vc.move_to = AsyncMock()
    guild.voice_client = existing_vc
    guild.get_channel = MagicMock(return_value=target)
    bot.get_guild = MagicMock(return_value=guild)

    mgr = VoiceManager(bot)
    vc = await mgr.ensure_in_channel(123, 456)
    existing_vc.move_to.assert_not_awaited()
    assert vc is existing_vc
