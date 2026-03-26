# cogs/ffxiv_resets.py
from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime

import discord
from discord.ext import commands, tasks
from discord import app_commands

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore


LOG = logging.getLogger(__name__)

STATE_PATH = "data/ffxiv_resets.json"

# Cozy: default channel for daily/weekly posts
DEFAULT_CHANNEL_ID = 1425974792745648252

# DST-proof schedule: anchor to UTC, not local time
# FFXIV Daily Reset: 15:00 UTC
DAILY_RESET_UTC = dtime(hour=15, minute=0, tzinfo=timezone.utc)

# FFXIV Weekly Reset: Tuesday 08:00 UTC
WEEKLY_RESET_UTC = dtime(hour=8, minute=0, tzinfo=timezone.utc)
WEEKLY_RESET_WEEKDAY = 1  # Monday=0, Tuesday=1, ...


# Optional: restrict admin commands to these roles (or admin permission)
MAMA_CAT_ROLE_NAME = "Mama Cat"
GHOUL_ROLE_NAME = "Ghoul"


DAILY_LINES = [
    "Your dailies are up. I expect results, not excuses.",
    "Daily reset. Go collect your little chores and pretend it’s fun.",
    "Your daily responsibilities have respawned. Tragic.",
    "Dailies are ready. Get in there and act employed.",
    "Time to do your dailies. I do not care if you were comfy on your sofa.",
    "Daily reset. Please act like a functional member of Eorzea.",
    "It's reset... take your ERP pants off and go do some content, will you? Oh and I need food.",
    "Dailies are live. Try not to queue with clown energy.",
    "Your dailies are waiting. I expect effort, not theatrics.",
    "Daily reset. Go spin the roulette wheel of regret.",
    "Your dailies are up. Stop flirting and start working.",
    "Dailies are live. If I catch you ERPing before your roulettes are done, I’m biting.",
    "Daily reset. If you have time to pose, you have time to queue.",
    "Dailies are live. Stop loitering and go earn your serotonin scraps.",
    "Daily reset. Get in there before I report you for being idle and annoying.",
    "Daily reset. If you queue like you dress, this is going to be tragic.",
    "Daily reset. Maybe today you’ll get something other than Syrcus Tower.",
]

WEEKLY_LINES = [
    "Your weekly responsibilities are back. I assume you’re thrilled.",
    "Weekly reset. Step away from the glamour plate and go do content.",
    "Weeklies are live. I expect movement, not standing in Limsa.",
    "Weekly reset. Get up, get moving, and bring me snacks.",
    "A new week begins. Please try to be useful.",
    "A new week has begun. Unfortunately, so have your chores.",
    "Weekly reset. Time to act like you have a plan.",
    "Your weeklies are back. I didn’t ask for this either.",
    "Weekly reset. If you need me, I’ll be judging from a warm surface.",
    "Weekly reset. Go do your chores before you get trapped in /gpose again.",
    "Your weekly nonsense has refreshed. Put the catboy away and get moving.",
]


def _member_has_power(member: discord.Member) -> bool:
    names = {r.name for r in member.roles}
    return (
        member.guild_permissions.administrator
        or (MAMA_CAT_ROLE_NAME in names)
        or (GHOUL_ROLE_NAME in names)
        or member.guild_permissions.manage_guild
    )


@dataclass
class ResetState:
    channel_id: int | None = None
    last_daily_fired_utc_date: str | None = None   # "YYYY-MM-DD"
    last_weekly_fired_utc_date: str | None = None  # "YYYY-MM-DD"

    @staticmethod
    def load() -> "ResetState":
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            return ResetState(
                channel_id=raw.get("channel_id"),
                last_daily_fired_utc_date=raw.get("last_daily_fired_utc_date"),
                last_weekly_fired_utc_date=raw.get("last_weekly_fired_utc_date"),
            )
        except FileNotFoundError:
            return ResetState()
        except Exception:
            LOG.exception("Failed to load %s", STATE_PATH)
            return ResetState()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "channel_id": self.channel_id,
                        "last_daily_fired_utc_date": self.last_daily_fired_utc_date,
                        "last_weekly_fired_utc_date": self.last_weekly_fired_utc_date,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            LOG.exception("Failed to save %s", STATE_PATH)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def fmt_dt(dt: datetime) -> str:
    ts = int(dt.timestamp())
    return f"<t:{ts}:F> (<t:{ts}:R>)"


def next_daily_reset(now_utc: datetime) -> datetime:
    candidate = now_utc.replace(
        hour=DAILY_RESET_UTC.hour,
        minute=DAILY_RESET_UTC.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now_utc:
        candidate += timedelta(days=1)
    return candidate


def next_weekly_reset(now_utc: datetime) -> datetime:
    base = now_utc.replace(
        hour=WEEKLY_RESET_UTC.hour,
        minute=WEEKLY_RESET_UTC.minute,
        second=0,
        microsecond=0,
    )
    days_ahead = (WEEKLY_RESET_WEEKDAY - base.weekday()) % 7
    if days_ahead == 0 and base <= now_utc:
        days_ahead = 7
    return base + timedelta(days=days_ahead)


def maybe_localize(dt_utc: datetime, tz_name: str) -> datetime | None:
    if ZoneInfo is None:
        return None
    try:
        return dt_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None


class FFXIVResets(commands.Cog):
    """Posts FFXIV daily/weekly reset announcements (UTC-anchored, DST-proof)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.state = ResetState.load()

        self.daily_reset_post.start()
        self.weekly_reset_post.start()

    def cog_unload(self) -> None:
        self.daily_reset_post.cancel()
        self.weekly_reset_post.cancel()

    def _channel_id(self) -> int:
        return int(self.state.channel_id or DEFAULT_CHANNEL_ID)

    def _resolve_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        ch = guild.get_channel(self._channel_id())
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _post_embed(self, guild: discord.Guild, *, title: str, body: str) -> None:
        ch = self._resolve_channel(guild)
        if ch is None:
            return

        embed = discord.Embed(
            title=title,
            description=body,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Mittens the Menace • reset ping 🐾")

        try:
            await ch.send(embed=embed)
        except Exception:
            LOG.exception("Failed posting reset message in guild %s", guild.id)

    # ---------------------------
    # Automatic posts
    # ---------------------------

    @tasks.loop(time=DAILY_RESET_UTC)
    async def daily_reset_post(self) -> None:
        now = utc_now()
        today = utc_date_str(now)

        if self.state.last_daily_fired_utc_date == today:
            return

        daily_line = random.choice(DAILY_LINES)

        for guild in self.bot.guilds:
            await self._post_embed(
                guild,
                title="☀️ Daily Reset (FFXIV)",
                body=daily_line,
            )

        self.state.last_daily_fired_utc_date = today
        self.state.save()

    @daily_reset_post.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=WEEKLY_RESET_UTC)
    async def weekly_reset_post(self) -> None:
        now = utc_now()
        today = utc_date_str(now)

        # Runs daily at 08:00 UTC; only posts on Tuesday.
        if now.weekday() != WEEKLY_RESET_WEEKDAY:
            return

        if self.state.last_weekly_fired_utc_date == today:
            return

        weekly_line = random.choice(WEEKLY_LINES)

        for guild in self.bot.guilds:
            await self._post_embed(
                guild,
                title="🗓️ Weekly Reset (FFXIV)",
                body=weekly_line,
            )

        self.state.last_weekly_fired_utc_date = today
        self.state.save()

    @weekly_reset_post.before_loop
    async def _before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    # ---------------------------
    # Slash commands
    # ---------------------------

    resets = app_commands.Group(
        name="resets",
        description="FFXIV reset announcements + info tools.",
    )

    @resets.command(name="set_channel", description="Set the channel for reset announcements.")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _member_has_power(interaction.user):
            return await interaction.response.send_message("You don’t have paws for this. 🐾", ephemeral=True)

        self.state.channel_id = channel.id
        self.state.save()
        await interaction.response.send_message(
            f"Locked in. I’ll post daily/weekly resets in {channel.mention}.",
            ephemeral=True,
        )

    @resets.command(name="next", description="Show when the next daily + weekly resets happen.")
    async def next_cmd(self, interaction: discord.Interaction) -> None:
        now = utc_now()
        nd = next_daily_reset(now)
        nw = next_weekly_reset(now)

        nd_lux = maybe_localize(nd, "Europe/Luxembourg")
        nw_lux = maybe_localize(nw, "Europe/Luxembourg")

        lines = [
            f"Posting channel: <#{self._channel_id()}>",
            "",
            f"**Next Daily Reset (UTC):** {fmt_dt(nd)}",
        ]
        if nd_lux:
            lines.append(f"**Next Daily Reset (Lux):** {fmt_dt(nd_lux)}")

        lines += [
            "",
            f"**Next Weekly Reset (UTC):** {fmt_dt(nw)}",
        ]
        if nw_lux:
            lines.append(f"**Next Weekly Reset (Lux):** {fmt_dt(nw_lux)}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @resets.command(name="countdown", description="Show a simple countdown to the next reset.")
    async def countdown_cmd(self, interaction: discord.Interaction) -> None:
        now = utc_now()
        nd = next_daily_reset(now)
        nw = next_weekly_reset(now)

        next_one = nd if nd <= nw else nw
        label = "Daily" if next_one == nd else "Weekly"

        await interaction.response.send_message(
            f"**Next reset:** {label}\n{fmt_dt(next_one)}",
            ephemeral=True,
        )

    @resets.command(name="test", description="Send a test reset message to the configured channel.")
    async def test_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _member_has_power(interaction.user):
            return await interaction.response.send_message("You don’t have paws for this. 🐾", ephemeral=True)

        await self._post_embed(
            interaction.guild,
            title="✅ Test: Reset Announcements",
            body="If you see this, Mittens can post daily/weekly resets in the configured channel.",
        )
        await interaction.response.send_message("Test message sent.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FFXIVResets(bot))
