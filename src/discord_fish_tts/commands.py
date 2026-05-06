"""Discord slash commands + DM handler."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .tts_pipeline import PipelineRegistry, TTSJob
from .validators import (
    ValidationError, check_guild, check_text,
)
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)


class TTSCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        voice_manager: VoiceManager,
        registry: PipelineRegistry,
        allowed_guilds: set[int],
        max_text_len: int,
    ) -> None:
        self.bot = bot
        self.voice_manager = voice_manager
        self.registry = registry
        self.allowed_guilds = allowed_guilds
        self.max_text_len = max_text_len

    # ----- helpers -----

    def _user_voice_channel(self, member: discord.Member) -> discord.VoiceChannel | None:
        if member.voice and isinstance(member.voice.channel, (discord.VoiceChannel,)):
            return member.voice.channel
        return None

    def _find_user_voice_anywhere(
        self, user_id: int
    ) -> tuple[int, int] | None:
        """For DM: scan whitelisted guilds in iteration order."""
        for gid in self.allowed_guilds:
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                return guild.id, member.voice.channel.id
        return None

    # ----- slash commands -----

    @app_commands.command(name="say", description="Speak text in your current voice channel.")
    @app_commands.describe(text="Text to speak (max 200 chars)")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run /say from a server.", ephemeral=True)
            return

        ch = self._user_voice_channel(interaction.user)
        if ch is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return

        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        try:
            await pipeline.enqueue(TTSJob(
                text=text,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,  # type: ignore[arg-type]
                voice_channel_id=ch.id,
            ))
        except asyncio.QueueFull:
            await interaction.response.send_message(
                "Queue is full, try again in a moment.", ephemeral=True
            )
            return

        await interaction.response.send_message("Queued.", ephemeral=True)

    @app_commands.command(name="join", description="Join your current voice channel.")
    async def join(self, interaction: discord.Interaction) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run /join from a server.", ephemeral=True)
            return
        ch = self._user_voice_channel(interaction.user)
        if ch is None:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return
        await self.voice_manager.ensure_in_channel(interaction.guild_id, ch.id)  # type: ignore[arg-type]
        await interaction.response.send_message(f"Joined {ch.name}.", ephemeral=True)

    @app_commands.command(name="leave", description="Leave the current voice channel.")
    async def leave(self, interaction: discord.Interaction) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        ok = await self.voice_manager.leave(interaction.guild_id)  # type: ignore[arg-type]
        msg = "Left the voice channel." if ok else "I'm not in a voice channel here."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="skip", description="Skip the currently playing TTS.")
    async def skip(self, interaction: discord.Interaction) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        ok = await pipeline.skip_current()
        msg = "Skipped." if ok else "Nothing playing."
        await interaction.response.send_message(msg, ephemeral=True)

    # ----- DM listener -----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return  # only react to DMs

        text = message.content
        try:
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await message.reply(str(e))
            return

        found = self._find_user_voice_anywhere(message.author.id)
        if found is None:
            await message.reply(
                "You're not in a voice channel in any authorized server."
            )
            return
        guild_id, channel_id = found

        pipeline = self.registry.get_or_create(guild_id)
        try:
            await pipeline.enqueue(TTSJob(
                text=text,
                user_id=message.author.id,
                guild_id=guild_id,
                voice_channel_id=channel_id,
            ))
        except asyncio.QueueFull:
            await message.reply("Queue is full, try again in a moment.")
            return
        await message.add_reaction("✅")  # green check
