"""Discord slash commands, auto-TTS listener and DM handler.

Authorization model:
  - Owner is derived from `bot.application.owner.id` and passed in at
    construction time. Owner has full rights including whitelist management.
  - Whitelisted users (in ConfigStore) can use voice/TTS commands, pick their
    own voice and use /toggle auto-speak.
  - Everyone else is told they have no access and who to contact.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config_store import SERVER_USER_ID, ConfigStore, normalize_voice_id
from .text_rules import (MAX_SHORTCUT_INPUT, MAX_SHORTCUT_REPLACEMENT, MAX_SHORTCUTS_PER_USER,
                         AutoCorrector, TextStore, apply_shortcuts, ascii_table, words_of)
from .tts_pipeline import PipelineRegistry, TTSJob
from .validators import ValidationError, check_text
from .voice_manager import VoiceManager

PAID_USD_PER_BYTE = 15.0 / 1_000_000  # s2.1-pro list price, for "≈ cost" hints

log = logging.getLogger(__name__)

# Catches messages that look like a slash command typed as text
# (e.g. "/help" or "/say hello"). Lets users type things starting
# with a single non-alpha character through ("/dev/null", "//path").
_SLASH_TEXT_RE = re.compile(r'^\s*/[a-zA-Z]\w*')
_URL_RE = re.compile(r"https?://\S+")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_SPOILER_RE = re.compile(r"\|\|.+?\|\|", re.S)

VOICE_LIBRARY_URL = "https://fish.audio/app/discovery"
DM_STATS_WORDS = {"stats", "view", "usage"}
TOGGLE_REMINDER_COOLDOWN_S = 3600  # "auto-speak is off" reminder, per user
# Owner commands that still work inside a server that isn't enabled yet.
GATE_EXEMPT_COMMANDS = {"enable", "disable", "servers", "view"}
OWNER_HANDLE = "@CelesteRed"


def speakable_text(raw: str, max_len: int) -> str:
    """Turn a chat message (already clean_content) into something worth speaking."""
    text = _CUSTOM_EMOJI_RE.sub(r"\1", raw)
    text = _SPOILER_RE.sub(" spoiler ", text)
    text = _URL_RE.sub(" link ", text)
    text = " ".join(text.split())
    if len(text) > max_len:
        cut = text[:max_len]
        text = cut.rsplit(" ", 1)[0] if " " in cut[max_len // 2:] else cut
    return text.strip()


def _lookup_voice_sync(voice_id: str, api_key: str | None) -> tuple[bool | None, str | None]:
    """(exists, title). exists=None means Fish could not be reached."""
    headers = {"User-Agent": "discord-fish-tts"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"https://api.fish.audio/model/{voice_id}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
            return True, data.get("title")
    except urllib.error.HTTPError as e:
        if e.code in (400, 404, 422):
            return False, None
        return None, None
    except Exception:
        return None, None


class TTSCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        voice_manager: VoiceManager,
        registry: PipelineRegistry,
        config_store: ConfigStore,
        owner_id_provider,  # () -> int | None — late-bound (owner known after on_ready)
        max_text_len: int,
        idle_timeout_s: int = 300,
        fish_api_key: str | None = None,
        notifier=None,  # ServerNotifier (SERVER voice announcements)
        text_store: TextStore | None = None,  # shortcuts + auto-correct dictionary
        autocorrector: AutoCorrector | None = None,
    ) -> None:
        self.bot = bot
        self.voice_manager = voice_manager
        self.registry = registry
        self.config = config_store
        self._owner_id_provider = owner_id_provider
        self.max_text_len = max_text_len
        self._idle_timeout_s = idle_timeout_s
        self._fish_api_key = fish_api_key
        self.notifier = notifier
        self.text_store = text_store
        self.autocorrector = autocorrector if autocorrector is not None else (AutoCorrector() if text_store else None)
        self._voice_titles: dict[str, str] = {}
        self._join_locks: dict[int, asyncio.Lock] = {}
        self._toggle_reminded: dict[int, float] = {}  # user_id -> monotonic time of last reminder
        # guild_id → first time bot found itself alone in voice (epoch s)
        self._empty_since: dict[int, float] = {}
        if idle_timeout_s > 0:
            self._idle_check.start()

    def cog_unload(self) -> None:
        self._idle_check.cancel()
        self._reminder_expiry.cancel()

    @tasks.loop(seconds=60)
    async def _idle_check(self) -> None:
        """Disconnect from voice channels that have had no human listeners
        for `_idle_timeout_s` seconds. Prevents wasted Fish API quota and
        leftover voice connections from forgotten sessions."""
        try:
            now = time.time()
            for guild in list(self.bot.guilds):
                vc = guild.voice_client
                if vc is None:
                    self._empty_since.pop(guild.id, None)
                    continue
                # Count human (non-bot) members in the bot's channel
                channel = vc.channel
                humans = [m for m in channel.members if not m.bot]
                if humans:
                    self._empty_since.pop(guild.id, None)
                    continue
                first_empty = self._empty_since.get(guild.id)
                if first_empty is None:
                    self._empty_since[guild.id] = now
                    continue
                if now - first_empty >= self._idle_timeout_s:
                    log.info(
                        "guild %d (%s): voice channel #%s empty for %ds, leaving",
                        guild.id, guild.name, channel.name,
                        int(now - first_empty),
                    )
                    try:
                        await vc.disconnect(force=False)
                    except Exception:
                        log.exception("idle disconnect failed for guild %d", guild.id)
                    finally:
                        self._empty_since.pop(guild.id, None)
        except Exception:
            log.exception("idle check loop iteration failed")

    @_idle_check.before_loop
    async def _idle_check_before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- helpers ----

    def _owner_id(self) -> int | None:
        return self._owner_id_provider()

    def _is_owner(self, user_id: int) -> bool:
        oid = self._owner_id()
        return oid is not None and user_id == oid

    def _is_allowed(self, user_id: int) -> bool:
        return self._is_owner(user_id) or self.config.is_user_whitelisted(user_id)

    def _bot_name(self) -> str:
        return self.bot.user.name if self.bot.user else "this bot"

    def _no_access_message(self) -> str:
        oid = self._owner_id()
        contact = f"<@{oid}> ({OWNER_HANDLE})" if oid else OWNER_HANDLE
        return f"🚫 You do not have access to **{self._bot_name()}**. Contact {contact} to get access."

    def _user_voice_channel(self, member: discord.Member) -> discord.VoiceChannel | None:
        if member.voice and isinstance(member.voice.channel, discord.VoiceChannel):
            return member.voice.channel
        return None

    def _find_user_voice_anywhere(
        self, user_id: int
    ) -> tuple[int, int] | None:
        """For DM: find the user's voice channel in an enabled server, preferring one
        where the bot is already in that same channel (that's where /say would play)."""
        fallback = None
        for guild in self.bot.guilds:
            if not self.config.is_guild_enabled(guild.id):
                continue
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                vc = guild.voice_client
                if vc is not None and vc.channel.id == member.voice.channel.id:
                    return guild.id, member.voice.channel.id
                fallback = fallback or (guild.id, member.voice.channel.id)
        return fallback

    def _channels_text(self) -> str:
        chans = self.config.list_auto_tts_channels()
        return ", ".join(f"<#{c}>" for c in chans) if chans else "*(no auto-speak channels set)*"

    async def _lookup_voice(self, voice_id: str) -> tuple[bool | None, str | None]:
        exists, title = await asyncio.to_thread(_lookup_voice_sync, voice_id, self._fish_api_key)
        if title:
            self._voice_titles[voice_id] = title
        return exists, title

    async def _voice_label(self, voice_id: str | None) -> str:
        if not voice_id:
            return "*(none)*"
        title = self._voice_titles.get(voice_id)
        if title is None:
            # Bounded: callers reply to an interaction (3 s deadline).
            try:
                _, title = await asyncio.wait_for(self._lookup_voice(voice_id), timeout=1.5)
            except asyncio.TimeoutError:
                title = None
        return f"**{title}** (`{voice_id}`)" if title else f"`{voice_id}`"

    async def warm_voice_titles(self) -> None:
        """Pre-fetch titles of all configured voices so replies don't wait on Fish."""
        ids = {self.config.get_voice_id(), *self.config._user_voices.values()}
        for vid in filter(None, ids):
            if vid not in self._voice_titles:
                await self._lookup_voice(vid)

    async def _deny_if_not_allowed(self, interaction: discord.Interaction) -> bool:
        if not self._is_allowed(interaction.user.id):
            await interaction.response.send_message(self._no_access_message(), ephemeral=True)
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

    def _prepare_text(self, user_id: int, text: str) -> tuple[str, list[tuple[str, str]], list[int]]:
        """Auto-correct (if on for this person), then their shortcuts. Returns
        (text to speak, [(misspelling, fix)], [shortcut ids used])."""
        if self.text_store is None:
            return text, [], []
        shortcuts = self.text_store.shortcuts(user_id)
        fixes: list[tuple[str, str]] = []
        if self.autocorrector is not None and self.config.is_autocorrect(user_id):
            protected = self.text_store.dictionary() | self.text_store.all_shortcut_words()
            try:
                text, fixes = self.autocorrector.correct(text, protected)
            except Exception:
                log.exception("auto-correct failed; speaking the original text")
        text, used = apply_shortcuts(text, shortcuts)
        limit = self.max_text_len * 2  # shortcuts may expand a message
        if len(text) > limit:
            cut = text[:limit]
            text = cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut
        return text, fixes, used

    @app_commands.command(name="say", description="Speak text in the bot's current voice channel.")
    @app_commands.describe(text="Text to speak (max 200 chars)")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        log.info("/say from %s in guild %s: %r", interaction.user, interaction.guild_id, text[:60])
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

        if self.config.get_voice_for(interaction.user.id) is None:
            await interaction.response.send_message(
                f"No voice set. Pick one with `/voice set <link>` — browse voices at <{VOICE_LIBRARY_URL}>.",
                ephemeral=True,
            )
            return

        user_ch = self._user_voice_channel(interaction.user)
        if user_ch is None:
            log.info("/say rejected: user not in a voice channel")
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True,
            )
            return

        bot_ch_id = self.voice_manager.current_channel_id(interaction.guild_id)  # type: ignore[arg-type]
        if bot_ch_id is None:
            log.info("/say rejected: bot not in voice")
            await interaction.response.send_message(
                "I'm not in voice. Run `/join` to summon me.", ephemeral=True,
            )
            return
        if bot_ch_id != user_ch.id:
            log.info("/say rejected: bot in channel %s, user in %s", bot_ch_id, user_ch.id)
            bot_ch = interaction.guild.get_channel(bot_ch_id) if interaction.guild else None
            ch_name = bot_ch.name if bot_ch else str(bot_ch_id)
            guild_name = interaction.guild.name if interaction.guild else "this server"
            await interaction.response.send_message(
                f"I'm in **#{ch_name}** of **{guild_name}**. "
                f"Run `/join` from your channel to bring me over.",
                ephemeral=True,
            )
            return

        text, fixes, _ = self._prepare_text(interaction.user.id, text)
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

        log.info("/say queued for channel %s", user_ch.id)
        note = ""
        if fixes:
            note = " · auto-corrected " + ", ".join(f"*{a}* → *{b}*" for a, b in fixes[:5]) + \
                   " (`/auto-correct add-dictionary` to keep a spelling)"
        await interaction.response.send_message("Queued." + note, ephemeral=True)

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

        # Refuse to hijack: if bot is in another channel of this guild AND
        # currently playing, force the requester to /skip first to avoid
        # silently teleporting audio away from listeners in that channel.
        current_ch_id = self.voice_manager.current_channel_id(interaction.guild_id)
        if (current_ch_id is not None and current_ch_id != target.id):
            guild = interaction.guild
            vc = guild.voice_client if guild else None
            if vc is not None and vc.is_playing():
                cur_ch = guild.get_channel(current_ch_id) if guild else None
                cur_name = cur_ch.name if cur_ch else str(current_ch_id)
                await interaction.response.send_message(
                    f"I'm currently playing TTS in **#{cur_name}**. "
                    f"Run `/skip` first if you want to move me.",
                    ephemeral=True,
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

    # ---- /toggle ----

    @app_commands.command(
        name="toggle",
        description="Auto-speak everything you type in the no-mic channel while you're in voice.",
    )
    @app_commands.describe(state="on or off (leave empty to flip it)")
    async def toggle(self, interaction: discord.Interaction,
                     state: Literal["on", "off"] | None = None) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        uid = interaction.user.id
        enabled = (not self.config.is_auto_tts(uid)) if state is None else (state == "on")
        self.config.set_auto_tts(uid, enabled)
        log.info("/toggle: %s auto-speak %s", interaction.user, "on" if enabled else "off")
        if enabled:
            voice = await self._voice_label(self.config.get_voice_for(uid))
            msg = (
                f"🔊 Auto-speak **on**. Messages you type in {self._channels_text()} are spoken "
                f"while you're in a voice channel (I'll join you if I'm not in voice yet).\n"
                f"Your voice: {voice}. Run `/toggle off` to turn it off."
            )
            if self.config.get_voice_for(uid) is None:
                msg += f"\n⚠️ You have no voice yet — `/voice set <link>` (browse <{VOICE_LIBRARY_URL}>)."
        else:
            msg = "🔇 Auto-speak **off**."
        await interaction.response.send_message(msg, ephemeral=True)
        if enabled:
            # Turning it on ends the cooldown: their reminders now say "will remind you next time".
            await self.expire_reminders(user_id=uid)

    # ---- /voice ----

    voice_group = app_commands.Group(
        name="voice",
        description="Pick your own Fish Audio voice.",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True,
        ),
    )

    @voice_group.command(name="show", description="Show your voice and the default voice.")
    async def voice_show(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        uid = interaction.user.id
        mine = self.config.get_user_voice(uid)
        default = self.config.get_voice_id()
        lines = [
            f"Your voice: {await self._voice_label(mine) if mine else '*(not set — using the default)*'}",
            f"Default voice: {await self._voice_label(default)}",
            f"Auto-speak (`/toggle`): **{'on' if self.config.is_auto_tts(uid) else 'off'}**",
        ]
        if self._is_owner(uid):
            lines.append(f"SERVER voice: {await self._voice_label(self.config.get_server_voice())}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @voice_group.command(name="set", description="Set YOUR voice (paste a fish.audio voice link or ID).")
    @app_commands.describe(voice="fish.audio voice link (…/app/m/<id>) or the voice ID")
    async def voice_set(self, interaction: discord.Interaction, voice: str) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        vid = normalize_voice_id(voice)
        if not vid:
            await interaction.response.send_message("Paste a fish.audio voice link or ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        exists, title = await self._lookup_voice(vid)
        if exists is False:
            await interaction.followup.send(
                f"❌ Fish Audio has no voice `{vid}`. Open a voice at <{VOICE_LIBRARY_URL}> and copy its link.",
                ephemeral=True,
            )
            return
        self.config.set_user_voice(interaction.user.id, vid)
        log.info("/voice set: %s -> %s (%s)", interaction.user, vid, title)
        note = "" if exists else " (couldn't reach Fish Audio to verify it, saved anyway)"
        await interaction.followup.send(
            f"✅ Your voice is now {f'**{title}** (`{vid}`)' if title else f'`{vid}`'}{note}.",
            ephemeral=True,
        )

    @voice_group.command(name="reset", description="Go back to the default voice.")
    async def voice_reset(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        self.config.set_user_voice(interaction.user.id, None)
        default = await self._voice_label(self.config.get_voice_id())
        await interaction.response.send_message(f"Reset. You'll use the default voice: {default}.", ephemeral=True)

    @voice_group.command(name="default", description="Set the default voice for everyone without their own (owner only).")
    @app_commands.describe(voice="fish.audio voice link or ID")
    async def voice_default(self, interaction: discord.Interaction, voice: str) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        vid = normalize_voice_id(voice)
        if not vid:
            await interaction.response.send_message("Paste a fish.audio voice link or ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        exists, title = await self._lookup_voice(vid)
        if exists is False:
            await interaction.followup.send(f"❌ Fish Audio has no voice `{vid}`.", ephemeral=True)
            return
        self.config.set_voice_id(vid)
        await interaction.followup.send(
            f"✅ Default voice is now {f'**{title}** (`{vid}`)' if title else f'`{vid}`'}.", ephemeral=True,
        )

    @voice_group.command(name="server", description="Set the SERVER voice used for announcements and alerts (owner only).")
    @app_commands.describe(voice="fish.audio voice link or ID")
    async def voice_server(self, interaction: discord.Interaction, voice: str) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        vid = normalize_voice_id(voice)
        if not vid:
            await interaction.response.send_message("Paste a fish.audio voice link or ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        exists, title = await self._lookup_voice(vid)
        if exists is False:
            await interaction.followup.send(f"❌ Fish Audio has no voice `{vid}`.", ephemeral=True)
            return
        self.config.set_server_voice(vid)
        await interaction.followup.send(
            f"✅ SERVER voice is now {f'**{title}** (`{vid}`)' if title else f'`{vid}`'}.", ephemeral=True,
        )

    # ---- /announce ----

    @app_commands.command(name="announce", description="Speak a message in the SERVER voice (owner only).")
    @app_commands.describe(text="What the SERVER should say")
    @app_commands.guild_only()
    async def announce(self, interaction: discord.Interaction, text: str) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        try:
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if self.notifier is None:
            await interaction.response.send_message("Announcements are not available.", ephemeral=True)
            return
        gid = interaction.guild_id
        if self.voice_manager.current_channel_id(gid) is None and isinstance(interaction.user, discord.Member):
            user_ch = self._user_voice_channel(interaction.user)
            if user_ch is not None:
                await self.voice_manager.ensure_in_channel(gid, user_ch.id)
        queued = await self.notifier.announce(text, guild_id=gid)
        if queued:
            await interaction.response.send_message(f"📢 SERVER is announcing in **#{queued[0]}**.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "I'm not in a voice channel with anyone here. Join voice (I'll come to you) and try again.",
                ephemeral=True,
            )

    # ---- /ttschannel ----

    ttschannel_group = app_commands.Group(
        name="ttschannel",
        description="Channels where /toggle auto-speak listens (owner only).",
        guild_only=True,
    )

    @ttschannel_group.command(name="add", description="Let /toggle auto-speak work in a text channel.")
    async def ttschannel_add(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        added = self.config.add_auto_tts_channel(channel.id)
        msg = f"Added {channel.mention}." if added else f"{channel.mention} is already an auto-speak channel."
        await interaction.response.send_message(f"{msg}\nAuto-speak channels: {self._channels_text()}", ephemeral=True)

    @ttschannel_group.command(name="remove", description="Stop /toggle auto-speak in a text channel.")
    async def ttschannel_remove(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        removed = self.config.remove_auto_tts_channel(channel.id)
        msg = f"Removed {channel.mention}." if removed else f"{channel.mention} wasn't an auto-speak channel."
        await interaction.response.send_message(f"{msg}\nAuto-speak channels: {self._channels_text()}", ephemeral=True)

    @ttschannel_group.command(name="list", description="List auto-speak channels.")
    async def ttschannel_list(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        await interaction.response.send_message(f"Auto-speak channels: {self._channels_text()}", ephemeral=True)

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
        welcome = (
            f"👋 You can use **{self._bot_name()}** now!\n\n"
            f"🗣️ Pick your own voice: browse <{VOICE_LIBRARY_URL}>, open a voice, copy its link, "
            "then run `/voice set <link>`.\n"
            "🎤 In voice: `/join` (summon me) → `/say hello`\n"
            f"⌨️ No mic? Run `/toggle on` and everything you type in {self._channels_text()} "
            "is spoken while you're in voice.\n"
            "💬 Or DM me text and I'll speak it in your voice channel.\n\n"
            "Run `/help` anytime for the full command list."
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
        server = await self._voice_label(self.config.get_server_voice())
        server_line = f"• **SERVER** (system announcements) — voice {server}"
        mentions = "\n".join([server_line] + [f"• <@{u}>" for u in users])
        await interaction.response.send_message(
            f"{owner_line}\nWhitelisted users:\n{mentions}", ephemeral=True,
        )

    # ---- /view (owner usage reports) ----

    def _who(self, user_id: int) -> str:
        return "**SERVER**" if user_id == SERVER_USER_ID else f"<@{user_id}>"

    @staticmethod
    def _kb(nbytes: int) -> str:
        return f"{nbytes / 1000:.1f} KB" if nbytes >= 1000 else f"{nbytes} B"

    @staticmethod
    def _cost(nbytes: int) -> str:
        return f"${nbytes * PAID_USD_PER_BYTE:.2f}"

    def _usage_header(self) -> str:
        n = self.notifier
        if n is None or n.usage is None:
            return "Usage tracking is off."
        r = n.usage_report()
        if r["kind"] == "credit":
            return (f"**{n.format_status(r['pct'])}** — Fish credit ${r['credit']:.2f} of ${r['ref']:.2f} "
                    f"(model `{n.fish_model}`)")
        return (f"**{n.format_status(r['pct'])}** — {self._kb(r['used'])} of {self._kb(r['budget'])} monthly "
                f"budget used (model `{n.fish_model}`, free; ≈{self._cost(r['used'])} at paid rates)")

    # -- people / servers lookup (works from DMs too) --

    def _name(self, user_id: int) -> str:
        if user_id == SERVER_USER_ID:
            return "SERVER (system voice)"
        u = self.bot.get_user(user_id)
        if u is None:
            for g in self.bot.guilds:
                u = g.get_member(user_id)
                if u is not None:
                    break
        return f"{u.display_name} (@{u.name})" if u is not None else str(user_id)

    def _known_people(self) -> list[int]:
        oid = self._owner_id()
        ids = ([oid] if oid else []) + self.config.list_users()
        if self.notifier is not None and self.notifier.usage is not None:
            ids += self.notifier.usage.known_user_ids()
        return list(dict.fromkeys(ids + [SERVER_USER_ID]))

    def _known_guild_ids(self) -> list[int]:
        ids = [g.id for g in self.bot.guilds]
        if self.notifier is not None and self.notifier.usage is not None:
            ids += list(self.notifier.usage.guild_totals())
        return list(dict.fromkeys(ids))

    def _guild_name(self, guild_id: int) -> str:
        g = self.bot.get_guild(guild_id)
        return g.name if g is not None else f"Server {guild_id} ({self._bot_name()} left)"

    async def _view_user_autocomplete(self, interaction: discord.Interaction, current: str):
        if not self._is_owner(interaction.user.id):
            return []
        cur = current.lower().strip()
        out = []
        for uid in self._known_people():
            name = self._name(uid)
            if not cur or cur in name.lower() or cur in str(uid):
                out.append(app_commands.Choice(name=name[:100], value=str(uid)))
        return out[:25]

    async def _view_server_autocomplete(self, interaction: discord.Interaction, current: str):
        if not self._is_owner(interaction.user.id):
            return []
        cur = current.lower().strip()
        out = []
        for gid in self._known_guild_ids():
            name = self._guild_name(gid)
            if not cur or cur in name.lower() or cur in str(gid):
                state = "enabled" if self.config.is_guild_enabled(gid) else "disabled"
                out.append(app_commands.Choice(name=f"{name} ({state})"[:100], value=str(gid)))
        return out[:25]

    @staticmethod
    def _parse_id(value: str) -> int | None:
        if value.strip().lower() in ("server", "0"):
            return SERVER_USER_ID
        m = re.search(r"\d{15,21}", value)
        return int(m.group(0)) if m else None

    @app_commands.command(name="view", description="TTS usage across every server, one server, or one user (owner only).")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="Anyone who has used the bot — pick from the list, or paste a mention/ID",
                           server="One server's ranking and details")
    @app_commands.autocomplete(user=_view_user_autocomplete, server=_view_server_autocomplete)
    async def view(self, interaction: discord.Interaction, user: str | None = None,
                   server: str | None = None) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        if self.notifier is None or self.notifier.usage is None:
            await interaction.response.send_message("Usage tracking is off.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if user:
            uid = self._parse_id(user)
            if uid is None:
                await interaction.followup.send(f"Couldn't find a user from `{user}`.", ephemeral=True)
                return
            embed = await self._view_user(uid)
        elif server:
            gid = self._parse_id(server)
            if gid is None or gid not in self._known_guild_ids():
                await interaction.followup.send(f"Couldn't find a server from `{server}`.", ephemeral=True)
                return
            embed = await self._view_server(gid)
        else:
            embed = await self._view_overview()
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _ranking_lines(self, rows, total_bytes: int, limit: int) -> list[str]:
        lines = []
        for i, r in enumerate(rows[:limit], 1):
            share = r.bytes / total_bytes * 100 if total_bytes else 0
            own = self.config.get_user_voice(r.user_id) is not None
            voice = await self._voice_label(self.config.get_voice_for(r.user_id))
            lines.append(
                f"**{i}.** {self._who(r.user_id)} — {r.messages} msgs · {self._kb(r.bytes)} ({share:.0f}%)"
                f" · this month {self._kb(r.month_bytes)} · last <t:{int(r.last_ts)}:R>\n"
                f"   voice: {voice}{'' if own else ' *(default)*'}"
            )
        return lines

    def _guild_status(self, guild: discord.Guild) -> str:
        vc = guild.voice_client
        if vc is not None:
            n = sum(1 for m in vc.channel.members if not m.bot)
            voice = f"🔊 in {vc.channel.mention} ({n} listening)"
        else:
            voice = "🔈 not in voice"
        autos = [f"<#{c}>" for c in self.config.list_auto_tts_channels() if guild.get_channel(c) is not None]
        gate = "✅ enabled" if self.config.is_guild_enabled(guild.id) else "⛔ disabled"
        return (f"{gate} · {voice} · auto-speak: {', '.join(autos) if autos else '—'}"
                f" · {guild.member_count or '?'} members")

    async def _view_overview(self) -> discord.Embed:
        usage = self.notifier.usage
        total_msgs, total_bytes = usage.totals()
        per_guild = usage.guild_totals()
        servers = []
        for gid in sorted(self._known_guild_ids(), key=lambda g: -(per_guild.get(g, {}).get("bytes") or 0)):
            t = per_guild.get(gid)
            guild = self.bot.get_guild(gid)
            if t:
                share = t["bytes"] / total_bytes * 100 if total_bytes else 0
                line = (f"**{self._guild_name(gid)}** — {t['messages']} msgs · {self._kb(t['bytes'])} ({share:.0f}%)"
                        f" · {t['users']} users · top {self._who(t['top_user'])} · last <t:{int(t['last_ts'])}:R>")
            else:
                line = f"**{self._guild_name(gid)}** — no usage yet"
            if guild is not None:
                line += f"\n   {self._guild_status(guild)}"
            servers.append(line)
        rows = usage.ranking()
        people = await self._ranking_lines(rows, total_bytes, 10)
        used = {r.user_id for r in rows}
        idle = [u for u in self._known_people() if u not in used and u != SERVER_USER_ID]
        if idle:
            people.append("*No usage yet:* " + ", ".join(f"<@{u}>" for u in idle))
        desc = (
            f"{self._usage_header()}\n\n"
            f"**🌐 Servers ({len(self.bot.guilds)})**\n" + "\n".join(servers or ["Not in any server."]) +
            "\n\n**🏆 Top users (all servers)**\n" + "\n".join(people or ["No TTS usage recorded yet."])
        )
        embed = discord.Embed(title=f"🎙️ {self._bot_name()} usage — every server", description=desc[:4096],
                              color=discord.Color.blurple())
        embed.set_footer(text=f"All time: {total_msgs} messages · {self._kb(total_bytes)} · ≈{self._cost(total_bytes)}"
                              " at paid rates · /view server:… or /view user:… for details")
        return embed

    async def _view_server(self, guild_id: int) -> discord.Embed:
        usage = self.notifier.usage
        guild = self.bot.get_guild(guild_id)
        msgs, nbytes = usage.totals(guild_id)
        _, all_bytes = usage.totals()
        rows = usage.ranking(guild_id=guild_id)
        lines = await self._ranking_lines(rows, nbytes, 20)
        head = [f"{msgs} msgs · {self._kb(nbytes)} ({nbytes / all_bytes * 100 if all_bytes else 0:.0f}% of all usage)"
                f" · ≈{self._cost(nbytes)} at paid rates"]
        if guild is not None:
            head.append(self._guild_status(guild))
            if guild.me and guild.me.joined_at:
                head.append(f"{self._bot_name()} joined <t:{int(guild.me.joined_at.timestamp())}:R> · owner <@{guild.owner_id}>")
            used = {r.user_id for r in rows}
            idle = [u for u in self.config.list_users() if guild.get_member(u) is not None and u not in used]
            if idle:
                lines.append("*Whitelisted here, no usage yet:* " + ", ".join(f"<@{u}>" for u in idle))
        embed = discord.Embed(title=f"🎙️ {self._guild_name(guild_id)}",
                              description=("\n".join(head) + "\n\n" + "\n".join(lines or ["No TTS usage here yet."]))[:4096],
                              color=discord.Color.blurple())
        if guild is not None and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    async def _view_user(self, uid: int) -> discord.Embed:
        usage = self.notifier.usage
        s = usage.user_summary(uid)
        _, total_bytes = usage.totals()
        user = None
        if uid != SERVER_USER_ID:
            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except discord.HTTPException:
                    user = None

        if uid == SERVER_USER_ID:
            embed = discord.Embed(title="🎙️ SERVER", color=discord.Color.blurple())
            embed.description = (f"System voice for announcements, alerts and Claude relays\n"
                                 f"Voice: {await self._voice_label(self.config.get_server_voice())}")
        else:
            access = "👑 owner" if self._is_owner(uid) else ("✅ whitelisted" if self.config.is_user_whitelisted(uid)
                                                             else "🚫 no access")
            own = self.config.get_user_voice(uid)
            voice = ((await self._voice_label(own)) if own
                     else f"{await self._voice_label(self.config.get_voice_id())} *(default)*")
            embed = discord.Embed(title=f"🎙️ {user.display_name if user else uid}", color=discord.Color.blurple())
            if user is not None:
                embed.set_thumbnail(url=user.display_avatar.url)
            embed.description = (
                f"<@{uid}> · `{user.name if user else '?'}` · ID `{uid}`\n"
                f"Access: {access} · Auto-speak: **{'on' if self.config.is_auto_tts(uid) else 'off'}**\n"
                f"Voice: {voice}"
            )

        if not s["messages"]:
            embed.add_field(name="Usage", value="No TTS usage recorded yet.", inline=False)
        else:
            share = s["bytes"] / total_bytes * 100 if total_bytes else 0
            embed.add_field(name="Messages", value=f"{s['messages']} total\n{s['month_messages']} this month")
            embed.add_field(name="Usage", value=f"{self._kb(s['bytes'])} ({share:.0f}% of all)\n"
                                                f"{self._kb(s['month_bytes'])} this month")
            embed.add_field(name="Audio spoken", value=f"{s['audio_ms'] / 60000:.1f} min\n"
                                                       f"≈{self._cost(s['bytes'])} at paid rates")
            embed.add_field(name="First / last", value=f"<t:{int(s['first_ts'])}:R>\n<t:{int(s['last_ts'])}:R>")
            embed.add_field(name="How", value="\n".join(f"{src}: {n}" for src, n in s["sources"])
                            + (f"\nskipped: {s['skipped']}" if s["skipped"] else ""))
            servers = usage.user_guilds(uid)
            if servers:
                embed.add_field(name="Servers", value="\n".join(
                    f"{self._guild_name(g)}: {n} msgs · {self._kb(b)}" for g, n, b in servers)[:1024])
            if s["channels"]:
                embed.add_field(name="Voice channels", value="\n".join(f"<#{c}>: {n}" for c, n in s["channels"]))
            if len(s["voices"]) > 1 or (s["voices"] and s["voices"][0][0] != self.config.get_voice_for(uid)):
                vlines = [f"{await self._voice_label(v)}: {n}" for v, n in s["voices"]]
                embed.add_field(name="Voices used", value="\n".join(vlines)[:1024], inline=False)
            recent = []
            for row in usage.recent(uid, 10):
                text = discord.utils.escape_markdown(discord.utils.escape_mentions(row["text"]))
                text = text if len(text) <= 90 else text[:87] + "…"
                tag = f"{row['source']}{', skipped' if row['skipped'] else ''}"
                recent.append(f"<t:{int(row['ts'])}:R> `{tag}` {text}")
            embed.add_field(name="Recent messages", value="\n".join(recent)[:1024], inline=False)

        if user is not None:
            account = [f"Account created <t:{int(user.created_at.timestamp())}:R>"]
            for g in self.bot.guilds:
                m = g.get_member(uid)
                if m is None:
                    continue
                bit = f"**{g.name}**: joined <t:{int(m.joined_at.timestamp())}:R>" if m.joined_at else f"**{g.name}**"
                if m.voice and m.voice.channel:
                    bit += f" · in voice now: {m.voice.channel.mention}"
                account.append(bit)
            embed.add_field(name="Discord", value="\n".join(account)[:1024], inline=False)
        return embed

    # ---- server gate: new servers stay off until the owner runs /enable ----

    def _disabled_message(self) -> str:
        oid = self._owner_id()
        who = f"<@{oid}> ({OWNER_HANDLE})" if oid else OWNER_HANDLE
        return (f"⛔ **{self._bot_name()}** isn't enabled in this server yet. "
                f"Ask {who} to enable it.")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Runs before every slash command in this cog."""
        gid = interaction.guild_id
        if gid is None or self.config.is_guild_enabled(gid):
            return True
        root = interaction.command.qualified_name.split()[0] if interaction.command else ""
        if root in GATE_EXEMPT_COMMANDS and self._is_owner(interaction.user.id):
            return True
        log.info("blocked /%s from %s in disabled server %s", root, interaction.user, gid)
        await interaction.response.send_message(self._disabled_message(), ephemeral=True)
        return False

    async def cog_app_command_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # already answered by interaction_check
        log.error("slash command failed", exc_info=error)

    async def _dm_owner_embed(self, embed: discord.Embed) -> None:
        oid = self._owner_id()
        if oid is None:
            return
        try:
            user = self.bot.get_user(oid) or await self.bot.fetch_user(oid)
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not DM owner about server change")

    def _notified_guilds(self) -> set[int]:
        usage = self.notifier.usage if self.notifier is not None else None
        raw = usage.get_meta("gate_notified") if usage is not None else None
        return {int(x) for x in raw.split(",") if x} if raw else set()

    def _mark_notified(self, guild_id: int) -> None:
        usage = self.notifier.usage if self.notifier is not None else None
        if usage is not None:
            usage.set_meta("gate_notified", ",".join(str(g) for g in sorted(self._notified_guilds() | {guild_id})))

    async def notify_unseen_guilds(self) -> None:
        """Startup catch-up: servers that added the bot while it was offline."""
        seen = self._notified_guilds()
        for g in self.bot.guilds:
            if not self.config.is_guild_enabled(g.id) and g.id not in seen:
                await self.on_guild_join(g)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._mark_notified(guild.id)
        enabled = self.config.is_guild_enabled(guild.id)
        log.info("joined server %s (%s); enabled=%s", guild.name, guild.id, enabled)
        inviter = None
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                if entry.target is not None and self.bot.user and entry.target.id == self.bot.user.id:
                    inviter = entry.user
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
        embed = discord.Embed(
            title=f"🆕 Added to a new server: {guild.name}",
            color=discord.Color.green() if enabled else discord.Color.orange(),
            description=(
                f"Server ID: `{guild.id}`\n"
                f"Owner: <@{guild.owner_id}> · {guild.member_count or '?'} members\n"
                f"Added by: {inviter.mention + f' (`{inviter.name}`)' if inviter else '*unknown (no audit-log access)*'}\n"
                f"Server created <t:{int(guild.created_at.timestamp())}:R>\n\n"
                + ("✅ Already enabled." if enabled else
                   f"⛔ **Disabled.** Nobody can use me there until you run\n`/enable server:{guild.id}`\n"
                   f"Or `/disable server:{guild.id} leave:True` to make me leave.")
            ),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await self._dm_owner_embed(embed)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("removed from server %s (%s)", guild.name, guild.id)
        await self._dm_owner_embed(discord.Embed(
            title=f"👋 Removed from {guild.name}", color=discord.Color.dark_grey(),
            description=f"Server ID: `{guild.id}` · was {'enabled' if self.config.is_guild_enabled(guild.id) else 'disabled'}",
        ))

    async def _gate_autocomplete(self, interaction: discord.Interaction, current: str, want_enabled: bool):
        if not self._is_owner(interaction.user.id):
            return []
        cur = current.lower().strip()
        out = []
        for g in self.bot.guilds:
            if self.config.is_guild_enabled(g.id) != want_enabled:
                continue
            if not cur or cur in g.name.lower() or cur in str(g.id):
                out.append(app_commands.Choice(name=f"{g.name} ({g.id})"[:100], value=str(g.id)))
        return out[:25]

    async def _enable_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._gate_autocomplete(interaction, current, want_enabled=False)

    async def _disable_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._gate_autocomplete(interaction, current, want_enabled=True)

    def _resolve_guild(self, value: str) -> discord.Guild | None:
        gid = self._parse_id(value)
        return self.bot.get_guild(gid) if gid else None

    @app_commands.command(name="enable", description="Allow the bot to be used in a server (owner only).")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(server="Server name or ID (pick from the list)")
    @app_commands.autocomplete(server=_enable_autocomplete)
    async def enable(self, interaction: discord.Interaction, server: str) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        guild = self._resolve_guild(server)
        if guild is None:
            await interaction.response.send_message(f"I'm not in a server matching `{server}`.", ephemeral=True)
            return
        changed = self.config.enable_guild(guild.id)
        log.info("server %s (%s) enabled by owner", guild.name, guild.id)
        await interaction.response.send_message(
            f"✅ Enabled in **{guild.name}** (`{guild.id}`)." if changed
            else f"Already enabled in **{guild.name}**.", ephemeral=True)

    @app_commands.command(name="disable", description="Block the bot in a server, optionally leaving it (owner only).")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(server="Server name or ID (pick from the list)", leave="Also make the bot leave that server")
    @app_commands.autocomplete(server=_disable_autocomplete)
    async def disable(self, interaction: discord.Interaction, server: str, leave: bool = False) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        guild = self._resolve_guild(server)
        if guild is None:
            await interaction.response.send_message(f"I'm not in a server matching `{server}`.", ephemeral=True)
            return
        self.config.disable_guild(guild.id)
        if guild.voice_client is not None:
            await self.registry.get_or_create(guild.id).skip_current()
            await guild.voice_client.disconnect(force=False)
        log.info("server %s (%s) disabled by owner (leave=%s)", guild.name, guild.id, leave)
        msg = f"⛔ Disabled in **{guild.name}** (`{guild.id}`)."
        if leave:
            await interaction.response.send_message(msg + " Leaving the server now.", ephemeral=True)
            await guild.leave()
            return
        await interaction.response.send_message(msg + f" Re-enable with `/enable server:{guild.id}`.", ephemeral=True)

    @app_commands.command(name="servers", description="List every server the bot is in and whether it's enabled (owner only).")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def servers(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        lines = []
        for g in sorted(self.bot.guilds, key=lambda g: (not self.config.is_guild_enabled(g.id), g.name.lower())):
            lines.append(f"**{g.name}** · `{g.id}`\n   {self._guild_status(g)}")
        embed = discord.Embed(title=f"🌐 {self._bot_name()} is in {len(self.bot.guilds)} server(s)",
                              description="\n".join(lines)[:4096] or "Not in any server.",
                              color=discord.Color.blurple())
        embed.set_footer(text="/enable server:… · /disable server:… [leave:True]")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- /stop and /queue ----

    @app_commands.command(name="stop", description="Stop talking: your own messages, or everyone's if you're the owner.")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stop(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        uid = interaction.user.id
        gid = interaction.guild_id
        pipelines = [self.registry.get(gid)] if gid is not None else self.registry.all()
        pipelines = [p for p in pipelines if p is not None]
        if self._is_owner(uid):
            n = sum(p.stop_all() for p in pipelines)
            where = "this server" if gid is not None else "every server"
            log.info("/stop (owner): cleared %d messages in %s", n, where)
            msg = f"⏹️ Stopped everyone in {where} ({n} message{'s' if n != 1 else ''} stopped or removed)."
        else:
            n = sum(p.stop_user(uid) for p in pipelines)
            log.info("/stop: %s cleared %d of their messages", interaction.user, n)
            msg = (f"⏹️ Stopped your {n} message{'s' if n != 1 else ''}." if n
                   else "You have nothing playing or queued.")
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="queue", description="See what's being said now and what's coming up.")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        pipeline = self.registry.get(interaction.guild_id)
        tracks, pending = pipeline.snapshot() if pipeline is not None else ([], [])

        def line(job, prefix: str) -> str:
            text = discord.utils.escape_markdown(discord.utils.escape_mentions(job.text))
            text = text if len(text) <= 80 else text[:77] + "…"
            return f"{prefix} {self._who(job.user_id)} `{job.source}` — {text}"

        now = [line(t.job, f"🔊 *{t.frames_played * 20 / 1000:.0f}s*") for t in tracks if t.started and not t.finished]
        nxt = [line(t.job, "⏳ next") for t in tracks if not t.started and not t.finished]
        upcoming = [line(j, f"**{i}.**") for i, j in enumerate(pending, 1)]
        parts = []
        if now:
            parts.append("**Speaking now**\n" + "\n".join(now))
        if nxt or upcoming:
            parts.append("**Up next**\n" + "\n".join(nxt + upcoming[:15])
                         + (f"\n…and {len(upcoming) - 15} more" if len(upcoming) > 15 else ""))
        embed = discord.Embed(title="🎙️ Voice queue", color=discord.Color.blurple(),
                              description="\n\n".join(parts) or "Nothing playing and nothing queued.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- /shortcut ----

    shortcut_group = app_commands.Group(
        name="shortcut",
        description="Your personal word/phrase shortcuts (e.g. brb → be right back).",
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
    )

    def _protect_words(self, user_id: int, *texts: str) -> list[str]:
        """Shortcut words auto-correct would otherwise change go into the dictionary."""
        if self.text_store is None or self.autocorrector is None:
            return []
        words = set()
        for t in texts:
            words |= words_of(t)
        unknown = [w for w in words if len(w) >= 4 and not self.autocorrector.is_known(w)]
        return self.text_store.add_words(unknown, user_id, source="shortcut")

    def _shortcut_table(self, user_id: int) -> str:
        rows = [(str(sc.sid), sc.input, sc.replacement) for sc in self.text_store.shortcuts(user_id)]
        return ascii_table(rows) if rows else "*No shortcuts yet — `/shortcut create input:brb replacement:be right back`*"

    async def _text_store_ready(self, interaction: discord.Interaction) -> bool:
        if await self._deny_if_not_allowed(interaction):
            return False
        if self.text_store is None:
            await interaction.response.send_message("Shortcuts aren't available.", ephemeral=True)
            return False
        return True

    @shortcut_group.command(name="create", description="Make a shortcut: when you say <input>, I say <replacement>.")
    @app_commands.describe(input="Word or phrase you type", replacement="What I should say instead")
    async def shortcut_create(self, interaction: discord.Interaction, input: str, replacement: str) -> None:
        if not await self._text_store_ready(interaction):
            return
        uid = interaction.user.id
        input, replacement = " ".join(input.split()), " ".join(replacement.split())
        if not re.search(r"\w", input) or not replacement:
            await interaction.response.send_message("Both the input and the replacement need some text.", ephemeral=True)
            return
        if len(input) > MAX_SHORTCUT_INPUT or len(replacement) > MAX_SHORTCUT_REPLACEMENT:
            await interaction.response.send_message(
                f"Too long — input max {MAX_SHORTCUT_INPUT} characters, replacement max {MAX_SHORTCUT_REPLACEMENT}.",
                ephemeral=True)
            return
        existing = self.text_store.find_shortcut(uid, input)
        if existing is not None:
            await interaction.response.send_message(
                f"You already have `{input}` as shortcut **#{existing.sid}**. "
                f"Change it with `/shortcut replace id:{existing.sid} rephrase:…`", ephemeral=True)
            return
        if len(self.text_store.shortcuts(uid)) >= MAX_SHORTCUTS_PER_USER:
            await interaction.response.send_message(
                f"You've hit the limit of {MAX_SHORTCUTS_PER_USER} shortcuts. Delete one first.", ephemeral=True)
            return
        sc = self.text_store.add_shortcut(uid, input, replacement)
        protected = self._protect_words(uid, input, replacement)
        log.info("/shortcut create: %s #%d %r -> %r", interaction.user, sc.sid, input, replacement)
        note = f"\n-# Added to the auto-correct dictionary: {', '.join(protected)}" if protected else ""
        await interaction.response.send_message(
            f"✅ Shortcut **#{sc.sid}** saved.\n{self._shortcut_table(uid)}{note}", ephemeral=True)

    async def _shortcut_list(self, interaction: discord.Interaction, user: discord.User | None) -> None:
        if not await self._text_store_ready(interaction):
            return
        target = interaction.user
        if user is not None and user.id != interaction.user.id:
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("You can only view your own shortcuts.", ephemeral=True)
                return
            target = user
        embed = discord.Embed(title=f"⌨️ Shortcuts — {target.display_name}", color=discord.Color.blurple(),
                              description=self._shortcut_table(target.id)[:4096])
        embed.set_footer(text="/shortcut create · /shortcut replace id: rephrase: · /shortcut delete id:")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @shortcut_group.command(name="list", description="Show your shortcuts as a table.")
    @app_commands.describe(user="Owner only: someone else's shortcuts")
    async def shortcut_list(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        await self._shortcut_list(interaction, user)

    @shortcut_group.command(name="view", description="Show your shortcuts as a table.")
    @app_commands.describe(user="Owner only: someone else's shortcuts")
    async def shortcut_view(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        await self._shortcut_list(interaction, user)

    @shortcut_group.command(name="replace", description="Change what one of your shortcuts says.")
    @app_commands.describe(id="Shortcut id from /shortcut list", rephrase="The new replacement")
    async def shortcut_replace(self, interaction: discord.Interaction, id: app_commands.Range[int, 1, 10000],
                               rephrase: str) -> None:
        if not await self._text_store_ready(interaction):
            return
        uid = interaction.user.id
        rephrase = " ".join(rephrase.split())
        if not rephrase or len(rephrase) > MAX_SHORTCUT_REPLACEMENT:
            await interaction.response.send_message(
                f"The rephrase needs 1–{MAX_SHORTCUT_REPLACEMENT} characters.", ephemeral=True)
            return
        sc = self.text_store.update_shortcut(uid, id, rephrase)
        if sc is None:
            await interaction.response.send_message(f"You don't have a shortcut **#{id}**.", ephemeral=True)
            return
        protected = self._protect_words(uid, rephrase)
        note = f"\n-# Added to the auto-correct dictionary: {', '.join(protected)}" if protected else ""
        await interaction.response.send_message(
            f"✏️ Shortcut **#{id}** updated.\n{self._shortcut_table(uid)}{note}", ephemeral=True)

    @shortcut_group.command(name="delete", description="Delete one of your shortcuts.")
    @app_commands.describe(id="Shortcut id from /shortcut list")
    async def shortcut_delete(self, interaction: discord.Interaction, id: app_commands.Range[int, 1, 10000]) -> None:
        if not await self._text_store_ready(interaction):
            return
        sc = self.text_store.delete_shortcut(interaction.user.id, id)
        if sc is None:
            await interaction.response.send_message(f"You don't have a shortcut **#{id}**.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ Deleted **#{id}** (`{sc.input}` → `{sc.replacement}`).\n{self._shortcut_table(interaction.user.id)}",
            ephemeral=True)

    # ---- /auto-correct ----

    autocorrect_group = app_commands.Group(
        name="auto-correct",
        description="Spelling fixes before I speak (on by default).",
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
    )

    @autocorrect_group.command(name="toggle", description="Turn auto-correct on or off for your messages.")
    @app_commands.describe(state="on or off (leave empty to flip it)")
    async def autocorrect_toggle(self, interaction: discord.Interaction,
                                 state: Literal["on", "off"] | None = None) -> None:
        if not await self._text_store_ready(interaction):
            return
        uid = interaction.user.id
        enabled = (not self.config.is_autocorrect(uid)) if state is None else state == "on"
        self.config.set_autocorrect(uid, enabled)
        await interaction.response.send_message(
            "✅ Auto-correct **on** — I'll fix clear misspellings before speaking." if enabled
            else "Auto-correct **off** — I'll say exactly what you type.", ephemeral=True)

    @autocorrect_group.command(name="add-dictionary", description="Never auto-correct this spelling (shared by everyone).")
    @app_commands.describe(word="The word to keep as typed")
    async def autocorrect_add(self, interaction: discord.Interaction, word: str) -> None:
        if not await self._text_store_ready(interaction):
            return
        word = word.strip().lower()
        if not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", word) or len(word) > 40:
            await interaction.response.send_message("Add one word (letters only, max 40).", ephemeral=True)
            return
        added = self.text_store.add_words([word], interaction.user.id)
        await interaction.response.send_message(
            f"📖 `{word}` added — I won't auto-correct it." if added else f"`{word}` is already in the dictionary.",
            ephemeral=True)

    @autocorrect_group.command(name="remove-dictionary", description="Remove a word you added to the dictionary.")
    @app_commands.describe(word="The word to remove")
    async def autocorrect_remove(self, interaction: discord.Interaction, word: str) -> None:
        if not await self._text_store_ready(interaction):
            return
        entry = next((e for e in self.text_store.dictionary_entries() if e["word"] == word.strip().lower()), None)
        if entry is None:
            await interaction.response.send_message(f"`{word}` isn't in the dictionary.", ephemeral=True)
            return
        if not self._is_owner(interaction.user.id) and entry["added_by"] != interaction.user.id:
            await interaction.response.send_message("Only the person who added it (or the owner) can remove it.",
                                                    ephemeral=True)
            return
        self.text_store.remove_word(entry["word"])
        await interaction.response.send_message(f"Removed `{entry['word']}` from the dictionary.", ephemeral=True)

    @autocorrect_group.command(name="dictionary", description="Show the words auto-correct leaves alone.")
    async def autocorrect_dictionary(self, interaction: discord.Interaction) -> None:
        if not await self._text_store_ready(interaction):
            return
        entries = self.text_store.dictionary_entries()
        manual = [e["word"] for e in entries if e["source"] != "shortcut"]
        from_shortcuts = [e["word"] for e in entries if e["source"] == "shortcut"]
        desc = (f"**Added by people ({len(manual)})**\n{', '.join(manual) or '—'}\n\n"
                f"**From shortcuts ({len(from_shortcuts)})**\n{', '.join(from_shortcuts) or '—'}")
        embed = discord.Embed(title="📖 Auto-correct dictionary", description=desc[:4096], color=discord.Color.blurple())
        embed.set_footer(text="Common chat slang (lol, bruh, ngl…) is always left alone too.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autocorrect_group.command(name="test", description="Preview what I'd say for some text (nothing is spoken).")
    @app_commands.describe(text="Text to check")
    async def autocorrect_test(self, interaction: discord.Interaction, text: str) -> None:
        if not await self._text_store_ready(interaction):
            return
        out, fixes, used = self._prepare_text(interaction.user.id, text[:self.max_text_len])
        lines = [f"**I'd say:** {discord.utils.escape_mentions(out)}"]
        lines.append("**Fixed:** " + (", ".join(f"*{a}* → *{b}*" for a, b in fixes) if fixes else "nothing")
                     + ("" if self.config.is_autocorrect(interaction.user.id) else " (auto-correct is off for you)"))
        if used:
            lines.append("**Shortcuts used:** " + ", ".join(f"#{i}" for i in used))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- /help ----

    @app_commands.command(name="help", description="Show how to use this bot.")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if not self._is_allowed(uid):
            await interaction.response.send_message(self._no_access_message(), ephemeral=True)
            return
        lines = [
            f"**{self._bot_name()}** — text-to-speech in voice channels.",
            "",
            "🗣️ **Your voice**",
            f"• Browse voices: <{VOICE_LIBRARY_URL}> — open one and copy its link",
            "• `/voice set <link>` — use that voice for everything you say",
            "• `/voice show` — your voice + auto-speak status · `/voice reset` — back to the default",
            "",
            "🎤 **In voice**",
            "• `/join [channel]` — bring me to a voice channel",
            "• `/say <text>` — I speak it (I must be in your voice channel)",
            "• `/skip` — stop the current line · `/stop` — stop all your messages · `/leave` — disconnect me",
            "• `/queue` — what's being said now and what's coming up",
            "• Quick replies within 10s talk over the end of the last line, like a real conversation",
            "",
            "✏️ **Your text**",
            "• `/shortcut create input:brb replacement:be right back` · `/shortcut list` · `/shortcut replace id: rephrase:` · `/shortcut delete id:`",
            "• Auto-correct fixes typos (on by default): `/auto-correct toggle` · `/auto-correct add-dictionary <word>` · `/auto-correct test <text>`",
            "",
            "⌨️ **No mic?**",
            f"• `/toggle on` / `/toggle off` — auto-speak everything you type in {self._channels_text()} while you're in voice",
            "",
            "💬 **DM me** any text and I'll speak it in your voice channel (I must already be there)",
        ]
        if self._is_owner(uid):
            lines += [
                "",
                "👑 **Owner**",
                "• `/voice default <link>` — default voice for people without their own",
                "• `/announce <text>` — speak a message in the SERVER voice · `/voice server <link>` — change it",
                "• `/stop` — stops **everyone** (in DMs: every server)",
                "• `/view` — usage across every server · `/view server:…` · `/view user:…` — stats + recent messages",
                "• DM me `stats` for the every-server report (works from anywhere)",
                "• `/servers` — every server I'm in · `/enable server:…` · `/disable server:… [leave:True]`",
                "  (new servers stay disabled until you `/enable` them — I DM you when I'm added)",
                "• `/whitelist add|remove @user` · `/whitelist list`",
                "• `/ttschannel add|remove #channel` · `/ttschannel list` — where `/toggle` works",
            ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- message listener: auto-speak (guild) + DM speak ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            await self._handle_auto_tts(message)
            return
        await self._handle_dm(message)

    async def _handle_auto_tts(self, message: discord.Message) -> None:
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if message.guild is None or not self.config.is_guild_enabled(message.guild.id):
            return
        if not self.config.is_auto_tts_channel(message.channel.id):
            return
        if not self._is_allowed(member.id):
            return
        if not self.config.is_auto_tts(member.id):
            await self._maybe_remind_toggle_off(message)
            return
        user_ch = self._user_voice_channel(member)
        if user_ch is None:
            return  # toggle only works while the user is in voice
        if _SLASH_TEXT_RE.match(message.content):
            return
        if self.config.get_voice_for(member.id) is None:
            return
        text = speakable_text(message.clean_content, self.max_text_len)
        if not text:
            return
        text, _, _ = self._prepare_text(member.id, text)

        guild = message.guild
        lock = self._join_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            bot_ch_id = self.voice_manager.current_channel_id(guild.id)
            if bot_ch_id is None:
                try:
                    await self.voice_manager.ensure_in_channel(guild.id, user_ch.id)
                    log.info("auto-speak: joined #%s for %s", user_ch.name, member)
                except Exception:
                    log.exception("auto-speak: could not join #%s", user_ch.name)
                    return
            elif bot_ch_id != user_ch.id:
                return  # bot is busy in another channel; never hijack it

        try:
            await self.registry.get_or_create(guild.id).enqueue(TTSJob(
                text=text, user_id=member.id, guild_id=guild.id, voice_channel_id=user_ch.id, source="auto",
            ))
            log.info("auto-speak queued for %s: %r", member, text[:60])
        except asyncio.QueueFull:
            log.warning("auto-speak: queue full, dropped message from %s", member)

    @property
    def _usage_store(self):
        return self.notifier.usage if self.notifier is not None else None

    @staticmethod
    def _reminder_text(user_id: int, expires_ts: float | None) -> str:
        footer = f"-# will send again <t:{int(expires_ts)}:R>" if expires_ts else "-# will remind you next time"
        return (f"🔇 <@{user_id}>, Auto TTS is turned **off** for you, so this wasn't spoken. "
                f"Run `/toggle on` to turn it on.\n{footer}")

    async def _maybe_remind_toggle_off(self, message: discord.Message) -> None:
        """Whitelisted user typing in an auto-speak channel while in voice, with auto-speak
        off: remind them once per hour (they may have turned it off on purpose). The reply
        shows when it can come back, and is edited once the hour is up."""
        member = message.author
        if self._user_voice_channel(member) is None or _SLASH_TEXT_RE.match(message.content):
            return
        now = time.time()
        store = self._usage_store
        last = store.last_reminder_ts(member.id) if store is not None else self._toggle_reminded.get(member.id)
        if last is not None and now - last < TOGGLE_REMINDER_COOLDOWN_S:
            return
        expires = now + TOGGLE_REMINDER_COOLDOWN_S
        self._toggle_reminded[member.id] = now
        log.info("auto-speak off reminder for %s", member)
        try:
            sent = await message.reply(
                self._reminder_text(member.id, expires),
                allowed_mentions=discord.AllowedMentions(users=[member], replied_user=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not send auto-speak reminder in #%s", message.channel)
            return
        if store is not None:
            store.add_reminder(user_id=member.id, guild_id=message.guild.id, channel_id=message.channel.id,
                               message_id=sent.id, sent_ts=now, expires_ts=expires)

    async def expire_reminders(self, user_id: int | None = None) -> int:
        """Edit reminders whose hour is up (or all of `user_id`'s active ones) to
        'will remind you next time'. Returns how many were handled."""
        store = self._usage_store
        if store is None:
            if user_id is not None:
                self._toggle_reminded.pop(user_id, None)
            return 0
        rows = store.due_reminders(time.time(), user_id=user_id)
        for r in rows:
            channel = self.bot.get_channel(r["channel_id"])
            try:
                if channel is not None:
                    await channel.get_partial_message(r["message_id"]).edit(
                        content=self._reminder_text(r["user_id"], None))
            except discord.NotFound:
                pass  # someone deleted it
            except (discord.Forbidden, discord.HTTPException):
                log.warning("could not edit reminder %s", r["message_id"])
                continue  # retry next tick
            store.mark_reminder_expired(r["id"])
        if user_id is not None:
            self._toggle_reminded.pop(user_id, None)
        return len(rows)

    @tasks.loop(seconds=15)
    async def _reminder_expiry(self) -> None:
        try:
            await self.expire_reminders()
        except Exception:
            log.exception("reminder expiry tick failed")

    @_reminder_expiry.before_loop
    async def _reminder_expiry_before(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_load(self) -> None:
        self._reminder_expiry.start()

    async def _handle_dm(self, message: discord.Message) -> None:
        if not self._is_allowed(message.author.id):
            await message.reply(self._no_access_message())
            return

        # Owner shortcut: DM "stats" to get the every-server usage report.
        if (self._is_owner(message.author.id) and message.content.strip().lower() in DM_STATS_WORDS
                and self.notifier is not None and self.notifier.usage is not None):
            await message.reply(embed=await self._view_overview())
            return

        # UX guard: users sometimes type "/help" as text instead of using
        # the slash command popup. Don't speak it — guide them.
        if _SLASH_TEXT_RE.match(message.content):
            await message.reply(
                "💡 That looks like a slash command typed as text. "
                "Type `/` and pick from the popup that appears, instead."
            )
            return

        if self.config.get_voice_for(message.author.id) is None:
            await message.reply(
                f"No voice set. Run `/voice set <link>` first — browse voices at <{VOICE_LIBRARY_URL}>."
            )
            return

        text = message.content
        try:
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await message.reply(str(e))
            return
        text, _, _ = self._prepare_text(message.author.id, text)

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
            guild = self.bot.get_guild(guild_id)
            bot_ch = guild.get_channel(bot_ch_id) if guild else None
            ch_name = bot_ch.name if bot_ch else str(bot_ch_id)
            guild_name = guild.name if guild else "the server"
            await message.reply(
                f"I'm in **#{ch_name}** of **{guild_name}**. "
                "Run `/join` from your channel there to bring me over."
            )
            return

        pipeline = self.registry.get_or_create(guild_id)
        try:
            await pipeline.enqueue(TTSJob(
                text=text,
                user_id=message.author.id,
                guild_id=guild_id,
                voice_channel_id=channel_id,
                source="dm",
            ))
        except asyncio.QueueFull:
            await message.reply("Queue is full, try again in a moment.")
            return
        await message.add_reaction("✅")
