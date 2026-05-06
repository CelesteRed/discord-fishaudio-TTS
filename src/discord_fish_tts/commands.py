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
from .whitelist import UserWhitelist

log = logging.getLogger(__name__)


class TTSCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        voice_manager: VoiceManager,
        registry: PipelineRegistry,
        whitelist: UserWhitelist,
        allowed_guilds: set[int],
        max_text_len: int,
    ) -> None:
        self.bot = bot
        self.voice_manager = voice_manager
        self.registry = registry
        self.whitelist = whitelist
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

    async def _deny_if_not_allowed(
        self, interaction: discord.Interaction
    ) -> bool:
        """Replies ephemeral and returns True if user is not whitelisted."""
        if not self.whitelist.is_allowed(interaction.user.id):
            await interaction.response.send_message(
                "You are not authorized to use this bot.", ephemeral=True
            )
            return True
        return False

    # ----- slash commands -----

    @app_commands.command(name="say", description="Speak text in the bot's current voice channel.")
    @app_commands.describe(text="Text to speak (max 200 chars)")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if await self._deny_if_not_allowed(interaction):
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run /say from a server.", ephemeral=True)
            return

        user_ch = self._user_voice_channel(interaction.user)
        if user_ch is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return

        # Bot must already be in the user's exact channel; /say does not summon.
        bot_ch_id = self.voice_manager.current_channel_id(interaction.guild_id)  # type: ignore[arg-type]
        if bot_ch_id is None:
            await interaction.response.send_message(
                "I'm not in any voice channel. Use `/join` to summon me.",
                ephemeral=True,
            )
            return
        if bot_ch_id != user_ch.id:
            bot_ch = interaction.guild.get_channel(bot_ch_id) if interaction.guild else None
            ch_name = bot_ch.name if bot_ch else f"<#{bot_ch_id}>"
            await interaction.response.send_message(
                f"I'm currently in **{ch_name}**. Join that channel, "
                f"or use `/leave` and then `/join` from your channel.",
                ephemeral=True,
            )
            return

        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        try:
            await pipeline.enqueue(TTSJob(
                text=text,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,  # type: ignore[arg-type]
                voice_channel_id=user_ch.id,
            ))
        except asyncio.QueueFull:
            await interaction.response.send_message(
                "Queue is full, try again in a moment.", ephemeral=True
            )
            return

        await interaction.response.send_message("Queued.", ephemeral=True)

    @app_commands.command(name="join", description="Summon the bot to your current voice channel.")
    async def join(self, interaction: discord.Interaction) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if await self._deny_if_not_allowed(interaction):
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

    @app_commands.command(name="leave", description="Disconnect the bot from its voice channel.")
    async def leave(self, interaction: discord.Interaction) -> None:
        try:
            check_guild(interaction.guild_id, allowed=self.allowed_guilds)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if await self._deny_if_not_allowed(interaction):
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
        if await self._deny_if_not_allowed(interaction):
            return
        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        ok = await pipeline.skip_current()
        msg = "Skipped." if ok else "Nothing playing."
        await interaction.response.send_message(msg, ephemeral=True)

    # ----- /whitelist (owner-only) -----

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="Manage the user whitelist (owner only).",
    )

    async def _deny_if_not_owner(
        self, interaction: discord.Interaction
    ) -> bool:
        if not self.whitelist.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "Only the bot owner can manage the whitelist.", ephemeral=True
            )
            return True
        return False

    @whitelist_group.command(name="add", description="Add a user to the whitelist.")
    @app_commands.describe(user="The Discord user to add.")
    async def whitelist_add(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        if self.whitelist.is_owner(user.id):
            await interaction.response.send_message(
                f"{user.mention} is the owner — already always allowed.",
                ephemeral=True,
            )
            return
        added = self.whitelist.add(user.id)
        msg = (
            f"Added {user.mention} to the whitelist."
            if added else f"{user.mention} is already in the whitelist."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @whitelist_group.command(name="remove", description="Remove a user from the whitelist.")
    @app_commands.describe(user="The Discord user to remove.")
    async def whitelist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        if self.whitelist.is_owner(user.id):
            await interaction.response.send_message(
                "Cannot remove the owner.", ephemeral=True
            )
            return
        removed = self.whitelist.remove(user.id)
        msg = (
            f"Removed {user.mention} from the whitelist."
            if removed else f"{user.mention} is not in the whitelist."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @whitelist_group.command(name="list", description="List whitelisted users.")
    async def whitelist_list(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        owner_id = self.whitelist.owner_id
        users = self.whitelist.list_users()
        if not users:
            await interaction.response.send_message(
                f"Owner: <@{owner_id}>\nWhitelist: (empty)",
                ephemeral=True,
            )
            return
        mentions = "\n".join(f"• <@{u}>" for u in users)
        await interaction.response.send_message(
            f"Owner: <@{owner_id}>\nWhitelisted users:\n{mentions}",
            ephemeral=True,
        )

    # ----- DM listener -----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return  # only react to DMs

        if not self.whitelist.is_allowed(message.author.id):
            await message.reply("You are not authorized to use this bot.")
            return

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

        # Manual-summon model: bot must already be in the user's channel.
        bot_ch_id = self.voice_manager.current_channel_id(guild_id)
        if bot_ch_id is None:
            await message.reply(
                "I'm not in a voice channel. Use `/join` first."
            )
            return
        if bot_ch_id != channel_id:
            await message.reply(
                "I'm in a different voice channel. Move to mine, or use "
                "`/leave` and `/join` from your channel."
            )
            return

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
        await message.add_reaction("✅")
