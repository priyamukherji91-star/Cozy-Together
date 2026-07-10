from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path

import discord
from discord.ext import commands, tasks
from discord import app_commands

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore


LOG = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
STATE_PATH = DATA_DIR / "ffxiv_resets.json"

# Cozy: default channel for daily/weekly posts
DEFAULT_CHANNEL_ID = 1425974792745648252
TEST_CHANNEL_ID = 1426295618934149212

# DST-proof schedule: anchor to UTC, not local time
# FFXIV Daily Reset: 15:00 UTC
DAILY_RESET_UTC = dtime(hour=15, minute=0, tzinfo=timezone.utc)

# FFXIV Weekly Reset: Tuesday 08:00 UTC
WEEKLY_RESET_UTC = dtime(hour=8, minute=0, tzinfo=timezone.utc)
WEEKLY_RESET_WEEKDAY = 1  # Monday=0, Tuesday=1, ...


# Optional: restrict admin commands to these roles (or admin permission)
MAMA_CAT_ROLE_NAME = "Mama Cat"
GHOUL_ROLE_NAME = "Ghoul"
TEST_ALLOWED_ROLE_IDS = {1425977436859797595, 1426194314337189949}


DAILY_LINES = [
    "Your dailies are up. Bring me results, not the little bedtime stories you tell yourself.",
    "Off you go, scavenging tomes and dignity in equal measure.",
    "Your dailies are up. For the three of you who survived Dawntrail and stayed subscribed, congrats.",
    "Time to perform meaningless labor for tomestones and emotional scraps.",
    "Get off the sofa, you upholstered excuse for a Warrior of Light.",
    "Pretend you have purpose beyond standing in Limsa.",
    "Remove the horny little glam and go engage with combat, freak.",
    "Queue up, and this time try not to play like a community warning.",
    "Your dailies are waiting. Save the main character syndrome for Limsa.",
    "Reset... go contribute something, you decorative parasite",
    "The aetheryte is not your workstation, you idle little barnacle.",
    "If you have time to pose, you have time to stop being ornamental and queue.",
    "Get in there before I file a formal complaint about your decorative existence.",
    "One had hoped you might eventually justify your subscription.",
    "One does tire of seeing so much glamour and so little competence.",
    "Your roulettes are available. Try not to make a spectacle of your inadequacy.",
    "Your roulettes await. Even now, I cling to the vulgar hope that you may be useful.",
    "Your dailies are up. I have seen retainers with more initiative.",
    "Your roulettes are available. Try to remember that confidence and competence are not hereditary.",
    "Your dailies are up. Really, dear, must your entire personality remain in /gpose?",
    "Daily reset. I will not say you are useless. I will merely observe that Eorzea has yet to notice your absence.",
    "One must accept that not everyone can be excellent. But you might at least be occupied.",
    "Must you always look so committed to doing nothing?",
    "There is something deeply reassuring about your consistency. You are idle in every expansion.",
    "Your dailies await. If you moved any less, we’d have to water you.",
    "Your dailies are up. One hates to interrupt such passionate loafing…",
    "The realm remains in peril, though naturally you are still dressed for brunch.",
    "I cannot say whether you are lazy or merely committed to atmosphere.",
    "One trembles to think of four or seven strangers relying on you.",
    "For someone allegedly touched by destiny, you do lounge remarkably hard.",
    "The Scions crossed continents, dimensions, and death itself. You can manage a roulette.",
]

WEEKLY_LINES = [
    "Your weekly obligations have returned. I trust your despair is suitably dignified.",
    "Weekly reset. Do step away from the glamour plate, dear. Beauty is no substitute for output.",
    "Your weeklies are available. I expect motion, not another week of decorative paralysis in Limsa.",
    "A new week begins. Do make some modest effort toward usefulness.",
    "Your weekly duties await. I would not call them enjoyable, but then neither are you.",
    "Your weeklies are up. How charming that Eorzea still believes in your potential.",
]


def _member_has_power(member: discord.Member) -> bool:
    names = {r.name for r in member.roles}
    return (
        member.guild_permissions.administrator
        or (MAMA_CAT_ROLE_NAME in names)
        or (GHOUL_ROLE_NAME in names)
        or member.guild_permissions.manage_guild
    )


def _member_can_test_resets(member: discord.Member) -> bool:
    role_ids = {r.id for r in member.roles}
    return member.guild_permissions.administrator or bool(role_ids & TEST_ALLOWED_ROLE_IDS)


@dataclass
class ResetState:
    channel_id: int | None = None
    last_daily_fired_utc_date: str | None = None   # "YYYY-MM-DD"
    last_weekly_fired_utc_date: str | None = None  # "YYYY-MM-DD"

    @staticmethod
    def load() -> "ResetState":
        if not STATE_PATH.exists():
            return ResetState()
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
            return ResetState(
                channel_id=raw.get("channel_id"),
                last_daily_fired_utc_date=raw.get("last_daily_fired_utc_date"),
                last_weekly_fired_utc_date=raw.get("last_weekly_fired_utc_date"),
            )
        except Exception:
            LOG.exception("Failed to load %s", STATE_PATH)
            return ResetState()

    def save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(
                json.dumps(
                    {
                        "channel_id": self.channel_id,
                        "last_daily_fired_utc_date": self.last_daily_fired_utc_date,
                        "last_weekly_fired_utc_date": self.last_weekly_fired_utc_date,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            LOG.exception("Failed to save %s", STATE_PATH)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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

    async def _already_posted_today(self, channel: discord.TextChannel, title: str) -> bool:
        today_utc = utc_date_str(utc_now())
        try:
            async for msg in channel.history(limit=5):
                if msg.author.id == self.bot.user.id:
                    for embed in msg.embeds:
                        if embed.title == title and utc_date_str(msg.created_at) == today_utc:
                            return True
        except Exception:
            pass
        return False

    def _resolve_channel(self, guild: discord.Guild, channel_id: int | None = None) -> discord.TextChannel | None:
        ch = guild.get_channel(channel_id or self._channel_id())
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _post_embed(
        self,
        guild: discord.Guild,
        *,
        title: str,
        body: str,
        channel_id: int | None = None,
    ) -> None:
        ch = self._resolve_channel(guild, channel_id=channel_id)
        if ch is None:
            return

        embed = discord.Embed(
            title=title,
            description=body,
            color=discord.Color.blurple(),
        )

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
            ch = self._resolve_channel(guild)
            if ch is not None and await self._already_posted_today(ch, "☀️ Daily Reset (FFXIV)"):
                continue
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
            ch = self._resolve_channel(guild)
            if ch is not None and await self._already_posted_today(ch, "🗓️ Weekly Reset (FFXIV)"):
                continue
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

    @resets.command(name="test", description="Send a random daily or weekly reset message to the test channel.")
    @app_commands.describe(kind="Choose whether to test a daily or weekly reset message.")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="weekly", value="weekly"),
        ]
    )
    async def test_cmd(self, interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        if not _member_can_test_resets(interaction.user):
            return await interaction.response.send_message("You don’t have paws for this. 🐾", ephemeral=True)

        if kind.value == "daily":
            title = "☀️ Daily Reset (FFXIV)"
            body = random.choice(DAILY_LINES)
        else:
            title = "🗓️ Weekly Reset (FFXIV)"
            body = random.choice(WEEKLY_LINES)

        await self._post_embed(
            interaction.guild,
            title=title,
            body=body,
            channel_id=TEST_CHANNEL_ID,
        )
        await interaction.response.send_message(
            f"Sent a random {kind.value} reset test to <#{TEST_CHANNEL_ID}>.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FFXIVResets(bot))