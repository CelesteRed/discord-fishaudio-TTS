"""Discord slash commands + DM handler.

Authorization model:
  - Owner is derived from `bot.application.owner.id` and passed in at
    construction time. Owner has full rights including whitelist management.
  - Whitelisted users (in ConfigStore) can use voice/TTS commands.
  - Everyone else is denied (silently for DMs to non-allowed users? no —
    we reply to keep behavior obvious).
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config_store import ConfigStore
from .tts_pipeline import PipelineRegistry, TTSJob
from .validators import ValidationError, check_text
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)


class TTSCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        voice_manager: VoiceManager,
        registry: PipelineRegistry,
        config_store: ConfigStore,
        owner_id_provider,  # () -> int | None — late-bound (owner known after on_ready)
        max_text_len: int,
    ) -> None:
        self.bot = bot
        self.voice_manager = voice_manager
        self.registry = registry
        self.config = config_store
        self._owner_id_provider = owner_id_provider
        self.max_text_len = max_text_len

    # ---- helpers ----

    def _owner_id(self) -> int | None:
        return self._owner_id_provider()

    def _is_owner(self, user_id: int) -> bool:
        oid = self._owner_id()
        return oid is not None and user_id == oid

    def _is_allowed(self, user_id: int) -> bool:
        return self._is_owner(user_id) or self.config.is_user_whitelisted(user_id)

    def _user_voice_channel(self, member: discord.Member) -> discord.VoiceChannel | None:
        if member.voice and isinstance(member.voice.channel, discord.VoiceChannel):
            return member.voice.channel
        return None

    def _find_user_voice_anywhere(
        self, user_id: int
    ) -> tuple[int, int] | None:
        """For DM: scan all guilds the bot is in for this user's voice channel."""
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                return guild.id, member.voice.channel.id
        return None

    async def _deny_if_not_allowed(self, interaction: discord.Interaction) -> bool:
        if not self._is_allowed(interaction.user.id):
            await interaction.response.send_message(
                "You are not authorized to use this bot.", ephemeral=True
            )
            return True
        return False

    async def _deny_if_not_owner(self, interaction: discord.Interaction) -> bool:
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return True
        return False

    # ---- /say ----

    @app_commands.command(name="say", description="Speak text in the bot's current voice channel.")
    @app_commands.describe(text="Text to speak (max 200 chars)")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        try:
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if await self._deny_if_not_allowed(interaction):
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run /say from a server.", ephemeral=True)
            return

        if self.config.get_voice_id() is None:
            await interaction.response.send_message(
                "No voice configured yet. Owner must run `/voice set <fish_voice_id>` first.",
                ephemeral=True,
            )
            return

        user_ch = self._user_voice_channel(interaction.user)
        if user_ch is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return

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

    # ---- /join /leave /skip ----

    @app_commands.command(
        name="join",
        description="Summon the bot to a voice channel (defaults to yours).",
    )
    @app_commands.describe(
        channel="Voice channel to summon to. Leave empty to use your current channel.",
    )
    async def join(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run /join from a server, not in DM.", ephemeral=True,
            )
            return

        target = channel
        if target is None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Run /join from a server.", ephemeral=True,
                )
                return
            target = self._user_voice_channel(interaction.user)
            if target is None:
                await interaction.response.send_message(
                    "Join a voice channel first, or specify one: `/join channel:<name>`.",
                    ephemeral=True,
                )
                return
        elif target.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "That voice channel is not in this server.", ephemeral=True,
            )
            return

        await self.voice_manager.ensure_in_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(f"Joined {target.name}.", ephemeral=True)

    @app_commands.command(name="leave", description="Disconnect the bot from its voice channel.")
    async def leave(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        ok = await self.voice_manager.leave(interaction.guild_id)  # type: ignore[arg-type]
        msg = "Left the voice channel." if ok else "I'm not in a voice channel here."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="skip", description="Skip the currently playing TTS.")
    async def skip(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        ok = await pipeline.skip_current()
        msg = "Skipped." if ok else "Nothing playing."
        await interaction.response.send_message(msg, ephemeral=True)

    # ---- /voice ----

    voice_group = app_commands.Group(
        name="voice",
        description="Show or change the Fish Audio voice (owner can set).",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True,
        ),
    )

    @voice_group.command(name="show", description="Show the current Fish Audio voice ID.")
    async def voice_show(self, interaction: discord.Interaction) -> None:
        vid = self.config.get_voice_id()
        msg = f"Current voice: `{vid}`" if vid else "No voice configured. Owner: run `/voice set <id>`."
        await interaction.response.send_message(msg, ephemeral=True)

    @voice_group.command(name="set", description="Set the global Fish Audio voice ID (owner only).")
    @app_commands.describe(voice_id="A Fish Audio reference_id")
    async def voice_set(self, interaction: discord.Interaction, voice_id: str) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        voice_id = voice_id.strip()
        if not voice_id:
            await interaction.response.send_message("voice_id cannot be empty.", ephemeral=True)
            return
        self.config.set_voice_id(voice_id)
        await interaction.response.send_message(
            f"Voice set to `{voice_id}`.", ephemeral=True
        )

    # ---- /whitelist ----

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="Manage the user whitelist (owner only).",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True,
        ),
    )

    @whitelist_group.command(name="add", description="Add a user to the whitelist.")
    @app_commands.describe(user="The Discord user to add.")
    async def whitelist_add(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        if self._is_owner(user.id):
            await interaction.response.send_message(
                f"{user.mention} is the owner — already always allowed.",
                ephemeral=True,
            )
            return
        added = self.config.add_user(user.id)
        if not added:
            await interaction.response.send_message(
                f"{user.mention} is already in the whitelist.", ephemeral=True,
            )
            return

        # Try welcome DM. Don't block the command on DM failure.
        oid = self._owner_id()
        owner_mention = f"<@{oid}>" if oid is not None else "the owner"
        bot_name = (interaction.client.user.name
                    if interaction.client.user else "this bot")
        welcome = (
            f"👋 You've been added to **{bot_name}**'s whitelist by {owner_mention}.\n\n"
            "**You can now:**\n"
            "• In any server I'm in: join a voice channel → `/join` → `/say <text>` and "
            "I'll speak via Fish Audio TTS.\n"
            "• DM me text → I speak it in your voice channel (I must already be there — "
            "use `/join` first in a server we share).\n"
            "• `/help` for the full command list.\n\n"
            "Welcome!"
        )
        dm_ok = True
        try:
            await user.send(welcome)
        except (discord.Forbidden, discord.HTTPException):
            dm_ok = False

        suffix = "" if dm_ok else " (could not DM them — they may have DMs disabled)"
        await interaction.response.send_message(
            f"Added {user.mention} to the whitelist.{suffix}",
            ephemeral=True,
        )

    @whitelist_group.command(name="remove", description="Remove a user from the whitelist.")
    @app_commands.describe(user="The Discord user to remove.")
    async def whitelist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        removed = self.config.remove_user(user.id)
        msg = (
            f"Removed {user.mention} from the whitelist."
            if removed else f"{user.mention} is not in the whitelist."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @whitelist_group.command(name="list", description="List whitelisted users.")
    async def whitelist_list(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        oid = self._owner_id()
        users = self.config.list_users()
        owner_line = f"Owner: <@{oid}>" if oid else "Owner: (unknown)"
        if not users:
            await interaction.response.send_message(
                f"{owner_line}\nWhitelist: (empty)", ephemeral=True,
            )
            return
        mentions = "\n".join(f"• <@{u}>" for u in users)
        await interaction.response.send_message(
            f"{owner_line}\nWhitelisted users:\n{mentions}", ephemeral=True,
        )

    # ---- /help ----

    @app_commands.command(name="help", description="Show how to use this bot.")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        oid = self._owner_id()
        is_owner = oid is not None and interaction.user.id == oid
        lines = [
            "**discord-fishaudio-TTS** — text-to-speech in voice channels via Fish Audio.",
            "",
            "**Voice commands** (whitelisted users only):",
            "• `/join` — summon the bot to your current voice channel",
            "• `/say <text>` — speak text (bot must be in your channel)",
            "• `/skip` — stop the currently-playing TTS",
            "• `/leave` — disconnect the bot",
            "• DM the bot text → speaks in your voice channel (bot must already be there)",
            "",
            "**Voice config:**",
            "• `/voice show` — current Fish Audio voice ID",
        ]
        if is_owner:
            lines += [
                "• `/voice set <id>` — change the voice (owner only)",
                "",
                "**Whitelist (owner only):**",
                "• `/whitelist add @user` — grant access",
                "• `/whitelist remove @user` — revoke access",
                "• `/whitelist list` — show current whitelist",
            ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- DM listener ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return  # DM-only

        if not self._is_allowed(message.author.id):
            await message.reply("You are not authorized to use this bot.")
            return

        if self.config.get_voice_id() is None:
            await message.reply(
                "No voice configured. Run `/voice set <fish_voice_id>` first."
            )
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
                "You're not in a voice channel in any server I'm in."
            )
            return
        guild_id, channel_id = found

        bot_ch_id = self.voice_manager.current_channel_id(guild_id)
        if bot_ch_id is None:
            await message.reply("I'm not in a voice channel. Use `/join` first.")
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
