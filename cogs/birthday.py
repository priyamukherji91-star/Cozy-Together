# cogs/birthday.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

LOG = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
GUILD_ID = 1425974791516586045

ANNOUNCEMENT_CHANNEL_ID = 1437582015925850239
MEMBER_COMMAND_CHANNEL_ID = 1425974792745648252
ADMIN_COMMAND_CHANNEL_ID = 1429796227192459264

# Fixed GMT+1, exactly as requested: 9am GMT+1 every day.
BIRTHDAY_TZ = timezone(timedelta(hours=1), name="GMT+1")
POST_HOUR = 9
POST_MINUTE = 0
POST_WINDOW_MINUTES = 10

# Railway Volume Storage. Railway/Linux paths are case-sensitive.
# Default to /data, but allow override with DATA_DIR if your Railway volume uses another mount path.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
BIRTHDAY_PATH = DATA_DIR / "birthdays.json"
STATE_PATH = DATA_DIR / "birthday_state.json"

BDAY_WISHES = [
    "Everyone gather. {user} has survived another year, somehow. Happy birthday. 🐾",
    "Happy birthday {user}. Mittens has decided not to bite you today. Probably.",
    "Happy birthday {user}. May your loot be better than your life choices.",
    "Happy birthday {user}. You are the main character today. Briefly. Do not get used to it.",
    "{user}, happy birthday. You are legally entitled to cake and questionable attention.",
]

DATE_RE = re.compile(r"^(?P<day>\d{1,2})/(?P<month>\d{1,2})$")


def _ensure_data_dir() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        LOG.exception("Could not create birthday data directory: %s", DATA_DIR)
        raise


def _today_key(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(BIRTHDAY_TZ)
    return current.astimezone(BIRTHDAY_TZ).date().isoformat()


def _valid_ddmm(raw: str) -> tuple[str, str]:
    value = (raw or "").strip()
    match = DATE_RE.match(value)
    if not match:
        raise ValueError("Use DD/MM, for example 05/09.")

    day = int(match.group("day"))
    month = int(match.group("month"))

    if month < 1 or month > 12:
        raise ValueError("Month must be between 01 and 12.")

    # Leap year used only to validate 29/02 cleanly.
    try:
        datetime(year=2024, month=month, day=day)
    except ValueError:
        raise ValueError("That date does not exist.") from None

    return f"{day:02d}", f"{month:02d}"


@dataclass
class BirthdayEntry:
    user_id: int
    name: str
    day: str
    month: str

    @property
    def ddmm(self) -> str:
        return f"{self.day}/{self.month}"

    @property
    def display_line(self) -> str:
        return f"{self.name} {self.ddmm}"


@dataclass
class BirthdayState:
    last_announcement_date: Optional[str] = None

    @classmethod
    def load(cls) -> "BirthdayState":
        _ensure_data_dir()
        if not STATE_PATH.exists():
            return cls()
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return cls(**raw)
        except Exception:
            LOG.exception("Failed to load birthday state; using empty state.")
            return cls()

    def save(self) -> None:
        _ensure_data_dir()
        STATE_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


class BirthdayStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, BirthdayEntry]:
        _ensure_data_dir()
        if not self.path.exists():
            return {}

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            LOG.exception("Failed to read birthday storage; using empty store.")
            return {}

        entries: Dict[str, BirthdayEntry] = {}

        # Current format: {"user_id": {"user_id": int, "name": str, "day": "DD", "month": "MM"}}
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    if isinstance(value, dict):
                        entry = BirthdayEntry(
                            user_id=int(value.get("user_id", key)),
                            name=str(value.get("name", "Unknown")),
                            day=str(value.get("day", "")).zfill(2),
                            month=str(value.get("month", "")).zfill(2),
                        )
                        entries[str(entry.user_id)] = entry
                except Exception:
                    continue

        return entries

    def save(self, entries: Dict[str, BirthdayEntry]) -> None:
        _ensure_data_dir()
        serialised = {
            str(entry.user_id): asdict(entry)
            for entry in sorted(entries.values(), key=lambda e: (e.month, e.day, e.name.lower()))
        }
        self.path.write_text(
            json.dumps(serialised, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set_birthday(self, member: discord.Member, day: str, month: str) -> BirthdayEntry:
        entries = self.load()
        entry = BirthdayEntry(
            user_id=member.id,
            name=member.display_name,
            day=day,
            month=month,
        )
        entries[str(member.id)] = entry
        self.save(entries)
        return entry

    def all_entries(self) -> list[BirthdayEntry]:
        return sorted(
            self.load().values(),
            key=lambda e: (e.month, e.day, e.name.lower()),
        )

    def remove_birthday(self, user_id: int) -> bool:
        entries = self.load()
        key = str(user_id)
        if key not in entries:
            return False
        del entries[key]
        self.save(entries)
        return True

    def entries_for_today(self, now: Optional[datetime] = None) -> list[BirthdayEntry]:
        current = now or datetime.now(BIRTHDAY_TZ)
        day = f"{current.day:02d}"
        month = f"{current.month:02d}"
        return [entry for entry in self.all_entries() if entry.day == day and entry.month == month]


class BirthdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = BirthdayStore(BIRTHDAY_PATH)
        self.state = BirthdayState.load()
        self._startup_task = None

    async def cog_load(self) -> None:
        self._startup_task = self.bot.loop.create_task(self._start_after_ready())

    def cog_unload(self) -> None:
        if self.birthday_loop.is_running():
            self.birthday_loop.cancel()
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

    async def _start_after_ready(self) -> None:
        await self.bot.wait_until_ready()
        if not self.birthday_loop.is_running():
            self.birthday_loop.start()

    birthday = app_commands.Group(
        name="birthday",
        description="Birthday registration and announcement tools.",
    )

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────
    def _is_member_channel(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.channel and interaction.channel.id == MEMBER_COMMAND_CHANNEL_ID)

    def _is_admin_channel(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.channel and interaction.channel.id == ADMIN_COMMAND_CHANNEL_ID)

    @staticmethod
    def _is_admin_member(member: discord.Member) -> bool:
        return member.guild_permissions.administrator or member.guild_permissions.manage_guild

    async def _deny_member_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"Use this in <#{MEMBER_COMMAND_CHANNEL_ID}>. Mittens is very strict and deeply annoying.",
            ephemeral=True,
        )

    async def _deny_admin_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"Admin birthday commands belong in <#{ADMIN_COMMAND_CHANNEL_ID}>.",
            ephemeral=True,
        )

    async def _deny_admin(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "You don’t have paws for that. 🐾",
            ephemeral=True,
        )

    async def _resolve_member(self, guild: discord.Guild, entry: BirthdayEntry) -> Optional[discord.Member]:
        member = guild.get_member(entry.user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(entry.user_id)
        except Exception:
            return None

    async def _build_today_lines(self, guild: discord.Guild, entries: list[BirthdayEntry]) -> list[str]:
        lines: list[str] = []
        for entry in entries:
            member = await self._resolve_member(guild, entry)
            if member:
                lines.append(f"{member.mention} — {entry.display_line}")
            else:
                lines.append(entry.display_line)
        return lines

    # ──────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────
    @birthday.command(name="set", description="Set your birthday. Format: DD/MM")
    @app_commands.describe(date="Your birthday in DD/MM format, for example 05/09.")
    async def birthday_set(self, interaction: discord.Interaction, date: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        if not self._is_member_channel(interaction):
            return await self._deny_member_channel(interaction)

        try:
            day, month = _valid_ddmm(date)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)

        entry = self.store.set_birthday(interaction.user, day, month)
        await interaction.response.send_message(
            f"Saved. Mittens has written you down as: **{entry.display_line}** 🐾",
            ephemeral=True,
        )

    @birthday.command(name="check", description="Show all saved birthdays.")
    async def birthday_check(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        if not self._is_admin_channel(interaction):
            return await self._deny_admin_channel(interaction)
        if not self._is_admin_member(interaction.user):
            return await self._deny_admin(interaction)

        entries = self.store.all_entries()
        if not entries:
            return await interaction.response.send_message(
                "No birthdays stored yet. Suspiciously ageless server.",
                ephemeral=False,
            )

        lines = [entry.display_line for entry in entries]
        chunks = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 1800:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        await interaction.response.send_message(
            "**Saved birthdays**\n" + chunks[0],
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for chunk in chunks[1:]:
            await interaction.followup.send(
                chunk,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @birthday.command(name="today", description="Show today’s birthdays.")
    async def birthday_today(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        if not self._is_admin_channel(interaction):
            return await self._deny_admin_channel(interaction)
        if not self._is_admin_member(interaction.user):
            return await self._deny_admin(interaction)

        entries = self.store.entries_for_today()
        if not entries:
            return await interaction.response.send_message(
                "No birthdays today. The cake economy is safe for now.",
                ephemeral=False,
            )

        lines = await self._build_today_lines(interaction.guild, entries)
        await interaction.response.send_message(
            "**Birthdays today**\n" + "\n".join(lines),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    @birthday.command(name="remove", description="Remove a member's birthday entry.")
    @app_commands.describe(member="The member whose birthday to remove.")
    async def birthday_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        if not self._is_admin_channel(interaction):
            return await self._deny_admin_channel(interaction)
        if not self._is_admin_member(interaction.user):
            return await self._deny_admin(interaction)

        removed = self.store.remove_birthday(member.id)
        if removed:
            await interaction.response.send_message(
                f"Removed {member.display_name}'s birthday. Mittens has crossed them out. 🐾",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message(
                f"{member.display_name} wasn't in the birthday list anyway.",
                ephemeral=True,
            )

    # ──────────────────────────────────────────────────────────────
    # Automatic announcement
    # ──────────────────────────────────────────────────────────────
    def _in_post_window(self, now: datetime) -> bool:
        target = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
        delta = now - target
        return timedelta(0) <= delta < timedelta(minutes=POST_WINDOW_MINUTES)

    @tasks.loop(minutes=1)
    async def birthday_loop(self) -> None:
        now = datetime.now(BIRTHDAY_TZ)
        if not self._in_post_window(now):
            return

        today = _today_key(now)
        if self.state.last_announcement_date == today:
            return

        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return

        channel = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        entries = self.store.entries_for_today(now)
        self.state.last_announcement_date = today
        self.state.save()

        if not entries:
            return

        for entry in entries:
            member = await self._resolve_member(guild, entry)
            user_text = member.mention if member else entry.name
            wish = random.choice(BDAY_WISHES).format(user=user_text)

            await channel.send(
                wish,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )

    @birthday_loop.before_loop
    async def before_birthday_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BirthdayCog(bot))
