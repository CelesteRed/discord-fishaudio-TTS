"""Thin wrapper over discord.py voice client lifecycle."""
from __future__ import annotations

import discord


class VoiceManagerError(Exception):
    pass


class VoiceManager:
    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def ensure_in_channel(
        self, guild_id: int, channel_id: int
    ) -> discord.VoiceClient:
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            raise VoiceManagerError(f"guild {guild_id} not visible to bot")
        channel = guild.get_channel(channel_id)
        if channel is None:
            raise VoiceManagerError(f"channel {channel_id} not found")

        vc = guild.voice_client
        if vc is None:
            return await channel.connect()
        if vc.channel.id != channel_id:
            await vc.move_to(channel)
        return vc

    async def leave(self, guild_id: int) -> bool:
        guild = self._bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return False
        await guild.voice_client.disconnect(force=False)
        return True

    def is_connected(self, guild_id: int) -> bool:
        guild = self._bot.get_guild(guild_id)
        return guild is not None and guild.voice_client is not None

    def get_voice_client_in_channel(
        self, guild_id: int, channel_id: int
    ) -> discord.VoiceClient | None:
        """Return the active VoiceClient if the bot is currently connected
        to that exact channel; otherwise None. Used by /say and DM trigger
        to enforce 'bot must already be summoned' semantics — neither
        connects nor moves the bot.
        """
        guild = self._bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return None
        vc = guild.voice_client
        if vc.channel.id != channel_id:
            return None
        return vc

    def current_channel_id(self, guild_id: int) -> int | None:
        """Returns the channel ID the bot is currently in, or None."""
        guild = self._bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return None
        return guild.voice_client.channel.id
