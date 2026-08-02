# cogs/seasonal_avatar.py
# -*- coding: utf-8 -*-
"""
Swaps Mittens' profile picture based on the season / occasion, and reverts
to the default when nothing is running.

Images live in ./assets/avatars/ and are matched by filename.

Dates that move year-to-year (Easter, Ramadan, Eid) are *computed*, not
hardcoded, so this keeps working without anyone touching it.

Discord rate-limits avatar changes hard, so we:
  - only check once an hour,
  - only call the API when the occasion actually changed,
  - remember the last applied occasion in ./data/avatar_state.json.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image

log = logging.getLogger("cozy.seasonal_avatar")

ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = ROOT / "assets" / "avatars"
STATE_FILE = Path(os.getenv("DATA_DIR", ROOT / "data")) / "avatar_state.json"

TIMEZONE = ZoneInfo("Europe/Brussels")
CHECK_MINUTES = 60

# Avatar commands only work in this channel.
CONTROL_CHANNEL_ID = 1429796227192459264

DEFAULT_KEY = "default"
MAX_EDGE = 1024          # Discord displays avatars small; 1024 is plenty
MAX_BYTES = 1_000_000    # keep the upload payload comfortably small


# ──────────────────────────────────────────────────────────────
# Moving-date maths
# ──────────────────────────────────────────────────────────────
def _easter_sunday(year: int) -> date:
    """Western (Gregorian) Easter — anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    month = (h + lo - 7 * m + 114) // 31
    day = ((h + lo - 7 * m + 114) % 31) + 1
    return date(year, month, day)


# Fallback if `hijridate` is unavailable — Ramadan 1 (Umm al-Qura).
# Only used when the import fails; the library covers every year.
_RAMADAN_FALLBACK: dict[int, tuple[date, date]] = {
    2026: (date(2026, 2, 18), date(2026, 3, 20)),
    2027: (date(2027, 2, 8), date(2027, 3, 9)),
    2028: (date(2028, 1, 28), date(2028, 2, 26)),
    2029: (date(2029, 1, 16), date(2029, 2, 14)),
    2030: (date(2030, 1, 5), date(2030, 2, 4)),
    2031: (date(2030, 12, 26), date(2031, 1, 24)),
    2032: (date(2031, 12, 15), date(2032, 1, 14)),
    2033: (date(2032, 12, 4), date(2033, 1, 2)),
    2034: (date(2033, 11, 23), date(2033, 12, 23)),
    2035: (date(2034, 11, 12), date(2034, 12, 12)),
}


def _ramadan_windows(year: int) -> list[tuple[date, date]]:
    """
    Every (Ramadan 1 → Eid al-Fitr + 2) window that touches `year`.

    A Gregorian year can contain two Ramadans (e.g. 2030), and a window can
    straddle New Year, so we return a list rather than a single range.
    """
    windows: list[tuple[date, date]] = []
    try:
        from hijridate import Gregorian, Hijri  # type: ignore

        hy = Gregorian(year, 6, 1).to_hijri().year
        for h in (hy - 1, hy, hy + 1):
            try:
                start = Hijri(h, 9, 1).to_gregorian()
                eid = Hijri(h, 10, 1).to_gregorian()
            except (ValueError, OverflowError):
                continue
            start_d = date(start.year, start.month, start.day)
            eid_d = date(eid.year, eid.month, eid.day)
            windows.append((start_d, _add_days(eid_d, 2)))
    except Exception:
        log.warning("hijridate unavailable — using built-in Ramadan table")
        for y in (year - 1, year, year + 1):
            if y in _RAMADAN_FALLBACK:
                start_d, eid_d = _RAMADAN_FALLBACK[y]
                windows.append((start_d, _add_days(eid_d, 2)))
    return windows


def _add_days(d: date, n: int) -> date:
    from datetime import timedelta

    return d + timedelta(days=n)


# ──────────────────────────────────────────────────────────────
# Occasion rules — FIRST MATCH WINS, so keep short/specific ones on top.
# Each rule yields the windows that touch the given year.
# ──────────────────────────────────────────────────────────────
def _fixed(year: int, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[date, date]]:
    """A fixed month/day range. Handles ranges that wrap past New Year."""
    s = date(year, *start)
    e = date(year, *end)
    if e < s:  # wraps the year boundary
        return [(s, date(year + 1, *end)), (date(year - 1, *start), e)]
    return [(s, e)]


OCCASIONS: list[tuple[str, str]] = [
    # (key, filename) — key is what gets stored in the state file
    ("valentinesday", "valentinesday.png"),
    ("easter", "easter.png"),
    ("ramadanoreid", "ramadanoreid.png"),
    ("halloween", "halloween.png"),
    ("christmas", "christmas.png"),
    ("prideday", "prideday.png"),
    ("summer", "summer.png"),
]

_FILENAMES: dict[str, str] = dict(OCCASIONS)
_FILENAMES[DEFAULT_KEY] = "default.jpg"


def _windows_for(key: str, year: int) -> list[tuple[date, date]]:
    if key == "valentinesday":
        return _fixed(year, (2, 13), (2, 15))
    if key == "easter":
        sunday = _easter_sunday(year)
        return [(_add_days(sunday, -2), _add_days(sunday, 1))]  # Good Fri → Easter Mon
    if key == "ramadanoreid":
        return _ramadan_windows(year)
    if key == "halloween":
        return _fixed(year, (10, 24), (11, 1))
    if key == "christmas":
        return _fixed(year, (12, 1), (12, 26))
    if key == "prideday":
        return _fixed(year, (6, 1), (6, 30))
    if key == "summer":
        return _fixed(year, (7, 1), (8, 31))
    return []


def occasion_for(today: date) -> str:
    """Which avatar should be live right now? Falls back to the default."""
    for key, _ in OCCASIONS:
        for start, end in _windows_for(key, today.year):
            if start <= today <= end:
                return key
    return DEFAULT_KEY


# ──────────────────────────────────────────────────────────────
# Image prep
# ──────────────────────────────────────────────────────────────
def _encode_avatar(path: Path) -> bytes:
    """Downscale and compress so the upload payload stays small."""
    with Image.open(path) as im:
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        has_alpha = im.mode in ("RGBA", "LA", "P")

        if has_alpha:
            rgba = im.convert("RGBA")
            buf = io.BytesIO()
            rgba.save(buf, "PNG", optimize=True)
            if buf.tell() <= MAX_BYTES:
                return buf.getvalue()
            # Too big for PNG — flatten onto white so transparency doesn't go black.
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[3])
            im = flat

        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        return buf.getvalue()


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class SeasonalAvatar(commands.Cog):
    """Auto-swaps Mittens' avatar for seasons/occasions, reverts to default."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_task: Optional[asyncio.Task] = None

    # ---------------- State ----------------

    def _read_state(self) -> dict:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, key: str, override: Optional[str]) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(
                    {
                        "occasion": key,
                        "override": override,
                        "updated": datetime.now(TIMEZONE).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            log.exception("Could not persist avatar state to %s", STATE_FILE)

    # ---------------- Core ----------------

    async def _apply(
        self,
        *,
        force: bool = False,
        set_override: Optional[str] = None,
        clear_override: bool = False,
    ) -> tuple[str, bool, str]:
        """
        Returns (occasion, changed, detail).

        Only touches the Discord API when the occasion differs from the last
        applied one — avatar edits are heavily rate-limited.

        A manual override pins the avatar until it's cleared, so the hourly
        loop won't undo a pick made with /avatar_set.
        """
        await self.bot.wait_until_ready()

        state = self._read_state()
        current = state.get("occasion")
        override = None if clear_override else (set_override or state.get("override"))

        today = datetime.now(TIMEZONE).date()
        want = override or occasion_for(today)

        if want not in _FILENAMES:
            log.warning("Unknown occasion %r — falling back to default", want)
            want, override = DEFAULT_KEY, None

        if want == current and not force:
            self._write_state(want, override)  # persist override change even if image is same
            return want, False, "already current"

        path = AVATAR_DIR / _FILENAMES[want]
        if not path.is_file():
            log.error("Avatar file missing: %s", path)
            return want, False, f"file missing: {path.name}"

        try:
            payload = await asyncio.to_thread(_encode_avatar, path)
        except Exception:
            log.exception("Could not encode avatar %s", path)
            return want, False, "could not read image"

        try:
            await self.bot.user.edit(avatar=payload)
        except discord.HTTPException as exc:
            # Leave state untouched so the next tick retries.
            log.warning("Avatar edit failed (%s): %s", exc.status, exc.text)
            if exc.status == 429:
                return want, False, "rate limited by Discord — will retry"
            return want, False, f"Discord rejected it: {exc.text}"

        self._write_state(want, override)
        log.info("Avatar changed: %s -> %s (%.0f KB)", current, want, len(payload) / 1024)
        pinned = " (pinned — use `/avatar_set auto` to resume the schedule)" if override else ""
        return want, True, f"{current or 'unknown'} → {want}{pinned}"

    # ---------------- Loop ----------------

    @tasks.loop(minutes=CHECK_MINUTES)
    async def check_avatar(self):
        try:
            await self._apply()
        except Exception:
            log.exception("Seasonal avatar check failed")

    async def cog_load(self):
        self._startup_task = asyncio.create_task(self._startup_after_ready())

    def cog_unload(self):
        if self.check_avatar.is_running():
            self.check_avatar.cancel()
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

    async def _startup_after_ready(self):
        await self.bot.wait_until_ready()
        if not self.check_avatar.is_running():
            self.check_avatar.start()

    # ---------------- Commands ----------------

    async def _wrong_channel(self, interaction: discord.Interaction) -> bool:
        """True (and replies) if this isn't the control channel."""
        if interaction.channel_id == CONTROL_CHANNEL_ID:
            return False
        await interaction.response.send_message(
            f"Not here. Use <#{CONTROL_CHANNEL_ID}>. 😼",
            ephemeral=True,
        )
        return True

    @app_commands.command(
        name="avatar_set",
        description="Pick Mittens' profile picture manually (pins it until set back to auto).",
    )
    @app_commands.describe(pick="Choose an avatar, or 'auto' to follow the calendar again.")
    @app_commands.choices(
        pick=[
            app_commands.Choice(name="auto (follow the calendar)", value="auto"),
            app_commands.Choice(name="default", value=DEFAULT_KEY),
            *[app_commands.Choice(name=k, value=k) for k, _ in OCCASIONS],
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def avatar_set(self, interaction: discord.Interaction, pick: app_commands.Choice[str]):
        if await self._wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if pick.value == "auto":
            occasion, changed, detail = await self._apply(clear_override=True)
            note = f"Back on schedule — currently **{occasion}** ({detail})."
        else:
            occasion, changed, detail = await self._apply(set_override=pick.value, force=True)
            note = f"Pinned to **{occasion}** — {detail}"

        await interaction.followup.send(
            f"{'✅' if changed else '😼'} {note}",
            ephemeral=True,
        )

    @app_commands.command(
        name="avatar_sync",
        description="Check the calendar and update Mittens' profile picture now.",
    )
    @app_commands.describe(force="Re-upload even if the occasion hasn't changed.")
    @app_commands.default_permissions(manage_guild=True)
    async def avatar_sync(self, interaction: discord.Interaction, force: bool = False):
        if await self._wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        occasion, changed, detail = await self._apply(force=force)
        icon = "✅" if changed else "😼"
        await interaction.followup.send(
            f"{icon} Occasion: **{occasion}** — {detail}",
            ephemeral=True,
        )

    @app_commands.command(
        name="avatar_schedule",
        description="Show which profile picture is active and what's coming up.",
    )
    async def avatar_schedule(self, interaction: discord.Interaction):
        if await self._wrong_channel(interaction):
            return
        today = datetime.now(TIMEZONE).date()
        state = self._read_state()
        override = state.get("override")

        lines = [f"**Scheduled right now:** `{occasion_for(today)}`"]
        if override:
            lines.append(f"**Pinned manually to:** `{override}` — `/avatar_set auto` to release")
        lines.append("")

        upcoming: list[tuple[date, date, str]] = []
        for key, _ in OCCASIONS:
            for year in (today.year, today.year + 1):
                for start, end in _windows_for(key, year):
                    if end >= today:
                        upcoming.append((start, end, key))
        upcoming.sort()

        seen: set[str] = set()
        for start, end, key in upcoming:
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"`{key}` — {start:%d %b %Y} → {end:%d %b %Y}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SeasonalAvatar(bot))
