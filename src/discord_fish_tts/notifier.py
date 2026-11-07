"""SERVER voice: system announcements and alerts.

- `announce()` speaks text with the SERVER pseudo-user's voice in every voice
  channel the bot is in (or one guild), via the normal per-guild TTS queue.
- `alert()` DMs the owner and, when the bot is in voice, also announces.
- A background check watches the Fish API credit (paid models only).
- `on_tts_error()` is wired as the pipeline error hook: Fish auth / payment
  failures are DMed to the owner (SERVER can't speak when Fish is refusing).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import urllib.request

import discord

from .config_store import SERVER_USER_ID, ConfigStore
from .tts_pipeline import PipelineRegistry, TTSJob
from .usage import UsageStore, month_start_ts

log = logging.getLogger(__name__)

FISH_BILLING_URL = "https://fish.audio/app/developers/"
_PAYMENT_RE = re.compile(r"\b402\b|payment|insufficient|credit|balance", re.I)
_AUTH_RE = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid api key", re.I)


def credit_alert_needed(model: str, credit: float | None, threshold: float) -> bool:
    """Free models don't draw credit, so only paid models can run low."""
    return not model.endswith("-free") and credit is not None and credit < threshold


def classify_fish_error(exc: BaseException) -> str | None:
    msg = f"{type(exc).__name__}: {exc}"
    if _AUTH_RE.search(msg):
        return "auth"
    if _PAYMENT_RE.search(msg):
        return "payment"
    return None


def _fetch_credit_sync(api_key: str) -> float | None:
    req = urllib.request.Request(
        "https://api.fish.audio/wallet/self/api-credit",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "discord-fish-tts"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return float(json.load(r).get("credit"))
    except Exception:
        log.warning("could not read Fish API credit", exc_info=True)
        return None


class ServerNotifier:
    def __init__(
        self,
        bot: discord.Client,
        registry: PipelineRegistry,
        config: ConfigStore,
        owner_id_provider,
        fish_api_key: str,
        fish_model: str,
        credit_alert_usd: float = 1.0,
        check_interval_s: float = 6 * 3600,
        error_dm_cooldown_s: float = 3600,
        usage: UsageStore | None = None,
        usage_budget_bytes: int = 1_000_000,
        status_interval_s: float = 600,
    ) -> None:
        self.bot = bot
        self.registry = registry
        self.config = config
        self._owner_id_provider = owner_id_provider
        self._fish_api_key = fish_api_key
        self.fish_model = fish_model
        self.credit_alert_usd = credit_alert_usd
        self._check_interval_s = check_interval_s
        self._error_dm_cooldown_s = error_dm_cooldown_s
        self._credit_alerted = False
        self._last_error_dm: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self.usage = usage
        self.usage_budget_bytes = max(1, int(usage_budget_bytes))
        self._status_interval_s = status_interval_s
        self._last_credit: float | None = None
        self._status_text: str | None = None
        self._status_pending = False
        self._last_status_at = 0.0

    @property
    def is_free_model(self) -> bool:
        return self.fish_model.endswith("-free")

    # ---- speaking ----

    def voice_targets(self, guild_id: int | None = None) -> list[tuple[int, int, str]]:
        """(guild_id, channel_id, channel_name) where the bot is in voice with people."""
        out = []
        for g in self.bot.guilds:
            if guild_id is not None and g.id != guild_id:
                continue
            if not self.config.is_guild_enabled(g.id):
                continue
            vc = g.voice_client
            if vc is None or not any(not m.bot for m in vc.channel.members):
                continue
            out.append((g.id, vc.channel.id, vc.channel.name))
        return out

    async def announce(self, text: str, guild_id: int | None = None) -> list[str]:
        """Speak `text` in the SERVER voice. Returns the channel names queued in."""
        if self.config.get_voice_for(SERVER_USER_ID) is None:
            log.warning("announce skipped: no SERVER or default voice set")
            return []
        queued = []
        for gid, cid, name in self.voice_targets(guild_id):
            try:
                await self.registry.get_or_create(gid).enqueue(TTSJob(
                    text=text, user_id=SERVER_USER_ID, guild_id=gid, voice_channel_id=cid, source="server",
                ))
                queued.append(name)
            except asyncio.QueueFull:
                log.warning("announce: queue full in #%s", name)
        log.info("SERVER announce %r -> %s", text[:80], queued or "(no voice channel)")
        return queued

    async def dm_owner(self, text: str) -> bool:
        oid = self._owner_id_provider()
        if oid is None:
            return False
        try:
            user = self.bot.get_user(oid) or await self.bot.fetch_user(oid)
            await user.send(text)
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not DM owner")
            return False

    async def alert(self, text: str, spoken: str | None = None) -> None:
        await self.dm_owner(f"🛰️ **SERVER:** {text}")
        await self.announce(spoken or text)

    # ---- credit monitor ----

    async def fetch_credit(self) -> float | None:
        return await asyncio.to_thread(_fetch_credit_sync, self._fish_api_key)

    async def check_credit_once(self) -> float | None:
        credit = await self.fetch_credit()
        if credit is None:
            return None
        prev = self._last_credit
        self._last_credit = credit
        if self.usage is not None:
            ref = float(self.usage.get_meta("credit_ref") or 0)
            # First reading, or the balance went up (a top-up): that's the new 100%.
            if credit > ref or (prev is not None and credit > prev + 0.01):
                self.usage.set_meta("credit_ref", f"{credit}")
        if credit_alert_needed(self.fish_model, credit, self.credit_alert_usd):
            if not self._credit_alerted:
                self._credit_alerted = True
                await self.alert(
                    f"Fish Audio API credit is low: **${credit:.2f}** left (model `{self.fish_model}`). "
                    f"Top up at <{FISH_BILLING_URL}>.",
                    spoken=f"Heads up from the server. The Fish Audio balance is low, about "
                           f"{credit:.2f} dollars left.",
                )
        else:
            self._credit_alerted = False
        return credit

    # ---- usage % (bot status) ----

    def usage_report(self, now: float | None = None) -> dict:
        """Remaining-usage numbers behind the status line and /view header."""
        if not self.is_free_model and self._last_credit is not None and self.usage is not None:
            ref = float(self.usage.get_meta("credit_ref") or 0) or self._last_credit or 1.0
            pct = max(0.0, min(100.0, self._last_credit / ref * 100)) if ref > 0 else 0.0
            return {"kind": "credit", "pct": pct, "credit": self._last_credit, "ref": ref}
        used = self.usage.bytes_since(month_start_ts(now)) if self.usage is not None else 0
        pct = max(0.0, 100.0 - used / self.usage_budget_bytes * 100)
        return {"kind": "budget", "pct": pct, "used": used, "budget": self.usage_budget_bytes}

    @staticmethod
    def format_status(pct: float) -> str:
        # Always round DOWN so a little usage never displays as 100%.
        shown = f"{math.floor(pct * 10) / 10:.1f}" if 99.0 <= pct < 100.0 else f"{int(pct)}"
        return f"Usage: {shown}% remaining"

    async def refresh_status(self, force: bool = False) -> None:
        report = self.usage_report()
        text = self.format_status(report["pct"])
        if report["kind"] == "budget" and self.usage is not None:
            await self._maybe_budget_alert(report)
        if text == self._status_text and not force:
            return
        try:
            await self.bot.change_presence(activity=discord.CustomActivity(name=text))
            self._status_text = text
            self._last_status_at = time.monotonic()
        except Exception:
            log.warning("could not update presence", exc_info=True)

    async def _maybe_budget_alert(self, report: dict) -> None:
        month = time.strftime("%Y-%m", time.gmtime())
        if report["pct"] < 10 and self.usage.get_meta("budget_alert_month") != month:
            self.usage.set_meta("budget_alert_month", month)
            await self.alert(
                f"Monthly TTS budget almost used: **{report['pct']:.0f}%** remaining "
                f"({report['used']:,} of {report['budget']:,} bytes). Raise `USAGE_BUDGET_BYTES` if needed.",
                spoken=f"Heads up from the server. Only {report['pct']:.0f} percent of this month's "
                       "voice budget is left.",
            )

    def on_usage(self, job: TTSJob, voice_id: str | None, audio_ms: int, skipped: bool) -> None:
        """Pipeline usage hook: record the billed request, then refresh the status soon."""
        if self.usage is None:
            return
        guild = self.bot.get_guild(job.guild_id)
        self.usage.record(
            user_id=job.user_id, guild_id=job.guild_id, channel_id=job.voice_channel_id,
            source=job.source, voice_id=voice_id, text=job.text, audio_ms=audio_ms, skipped=skipped,
        )
        log.debug("usage recorded for %s in %s", job.user_id, guild)
        if not self._status_pending:
            self._status_pending = True
            asyncio.get_running_loop().create_task(self._debounced_status())

    async def _debounced_status(self) -> None:
        # Presence updates are rate-limited by Discord; at most one per ~30 s.
        wait = max(0.0, 30 - (time.monotonic() - self._last_status_at))
        await asyncio.sleep(wait)
        self._status_pending = False
        await self.refresh_status()

    # ---- background loop ----

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="server-monitor")
            if self.is_free_model:
                log.info("usage status on: free model, monthly budget %d bytes", self.usage_budget_bytes)
            else:
                log.info("usage status on: credit-based; low-credit alert below $%.2f", self.credit_alert_usd)

    async def _loop(self) -> None:
        while True:
            try:
                if not self.is_free_model:
                    await self.check_credit_once()  # fresh balance for the status; alerts are one-shot
                # Always re-send: Discord drops presence after a fresh gateway session.
                await self.refresh_status(force=True)
            except Exception:
                log.exception("server monitor tick failed")
            await asyncio.sleep(self._status_interval_s)

    # ---- pipeline error hook ----

    def on_tts_error(self, job: TTSJob, exc: BaseException) -> None:
        kind = classify_fish_error(exc)
        if kind is None:
            return
        now = time.monotonic()
        if now - self._last_error_dm.get(kind, -1e9) < self._error_dm_cooldown_s:
            return
        self._last_error_dm[kind] = now
        if kind == "payment":
            text = (
                f"Fish Audio refused a TTS request (payment/credit) on model `{self.fish_model}`. "
                + ("The free model may have ended (it was promised through Nov 30, 2026): set "
                   "`FISH_MODEL=s2.1-pro` and add API credit. " if self.fish_model.endswith("-free") else
                   "The API balance is probably empty. ")
                + f"Billing: <{FISH_BILLING_URL}>\n`{str(exc)[:200]}`"
            )
        else:
            text = (
                "Fish Audio rejected the API key. Create a new key and run `fishtts-setup` on the VPS.\n"
                f"`{str(exc)[:200]}`"
            )
        asyncio.get_running_loop().create_task(self.dm_owner(f"🛰️ **SERVER:** {text}"))
