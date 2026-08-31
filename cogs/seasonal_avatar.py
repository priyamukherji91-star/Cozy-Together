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
  - remember the last applied occasion in avatar_state.json on the volume.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image

from cogs.admin_panel import PANEL_CHANNEL_ID
from common import storage

log = logging.getLogger("cozy.seasonal_avatar")

ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = ROOT / "assets" / "avatars"
STATE_FILE = storage.DATA_DIR / "avatar_state.json"

TIMEZONE = ZoneInfo("Europe/Brussels")
CHECK_MINUTES = 60

# Avatar commands only work in these channels.
CONTROL_CHANNEL_ID = 1429796227192459264
# The staff panel's button for this runs wherever the panel is posted, and an
# interaction can't be moved to another channel, so that channel counts too.
# Imported rather than copied: a second literal would silently disagree the
# day the panel moves.
CONTROL_CHANNEL_IDS = {CONTROL_CHANNEL_ID, PANEL_CHANNEL_ID}

DEFAULT_KEY = "default"
MAX_EDGE = 1024          # Discord displays avatars small; 1024 is plenty
MAX_BYTES = 1_000_000    # keep the upload payload comfortably small

# Hand-added avatars and their date windows. These live on the Railway volume
# rather than in ./assets, because a picture somebody uploads through Discord
# has to survive the next deploy - anything written into the repo checkout does
# not. The JSON holds the windows; the images sit beside it.
CUSTOM_FILE = storage.DATA_DIR / "custom_avatars.json"
CUSTOM_DIR = storage.DATA_DIR / "avatars"

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
UPLOAD_WAIT_SECONDS = 180

# A key is what the state file stores and what /avatar_set takes, so it has to
# be typeable and stable: lowercase, no spaces.
KEY_MAX = 24


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


# ──────────────────────────────────────────────────────────────
# Hand-added avatars
#
# The built-in occasions are rules: "halloween" is a computation over a year,
# and it repeats forever without anybody touching it. A hand-added one is the
# opposite - a picture and two dates, once. Keeping them in separate stores is
# what lets the built-ins stay code and the added ones stay data, and it means
# a bad upload can never break the calendar that runs without supervision.
#
# Windows are absolute, including the year. A yearly repeat would be guessing
# at intent: "put this up for the guild anniversary weekend" is a date, not a
# rule, and anything that wanted to be a rule belongs in OCCASIONS with the
# others.
# ──────────────────────────────────────────────────────────────
class AvatarError(ValueError):
    """A rejected upload, worded for the person who tried it."""


def _read_customs() -> dict:
    data = storage.load_json(CUSTOM_FILE, default={})
    return data if isinstance(data, dict) else {}


def _write_customs(data: dict) -> bool:
    return storage.save_json(CUSTOM_FILE, data)


def custom_windows() -> list[tuple[str, date, date]]:
    """Every hand-added avatar as (key, start, end), earliest first."""
    out: list[tuple[str, date, date]] = []
    for key, rec in _read_customs().items():
        try:
            start = date.fromisoformat(str(rec["start"]))
            end = date.fromisoformat(str(rec["end"]))
        except (KeyError, TypeError, ValueError):
            log.warning("custom avatar %r has unreadable dates - ignoring it", key)
            continue
        out.append((str(key), start, end))
    out.sort(key=lambda row: row[1])
    return out


def custom_keys() -> list[str]:
    return [key for key, _, _ in custom_windows()]


def path_for(key: str) -> Optional[Path]:
    """The image behind a key, whether it shipped with the bot or was added."""
    if key in _FILENAMES:
        path = AVATAR_DIR / _FILENAMES[key]
        return path if path.is_file() else None
    rec = _read_customs().get(key)
    if not isinstance(rec, dict):
        return None
    path = CUSTOM_DIR / str(rec.get("file", ""))
    return path if path.is_file() else None


def known_keys() -> list[str]:
    """Everything /avatar_set will accept, built-in first."""
    return [DEFAULT_KEY] + [k for k, _ in OCCASIONS] + custom_keys()


def occasion_for(today: date) -> str:
    """Which avatar should be live right now? Falls back to the default.

    Hand-added windows win over the built-in ones. Somebody picked those dates
    on purpose and recently; the calendar rules have every other day of the year.
    """
    for key, start, end in custom_windows():
        if start <= today <= end:
            return key
    for key, _ in OCCASIONS:
        for start, end in _windows_for(key, today.year):
            if start <= today <= end:
                return key
    return DEFAULT_KEY


def conflicts_with(
    start: date, end: date, *, ignore: Optional[str] = None
) -> list[tuple[str, date, date]]:
    """Everything already occupying any day between `start` and `end`.

    Checked against the built-in rules for every year the window touches as well
    as the other hand-added ones, because two avatars claiming the same day is
    not something the schedule can resolve: first match wins, silently, and the
    one just uploaded is the one that loses.
    """
    found: list[tuple[str, date, date]] = []
    for key, s, e in custom_windows():
        if key != ignore and s <= end and start <= e:
            found.append((key, s, e))
    for key, _ in OCCASIONS:
        for year in range(start.year - 1, end.year + 2):
            for s, e in _windows_for(key, year):
                if s <= end and start <= e and (key, s, e) not in found:
                    found.append((key, s, e))
    found.sort(key=lambda row: row[1])
    return found


def check_key(raw: str) -> str:
    """Names get typed into /avatar_set later, so they are kept plain."""
    key = "".join(ch for ch in raw.strip().lower() if ch.isalnum() or ch in "-_")
    if not key:
        raise AvatarError("Give it a name - letters and numbers, no spaces.")
    if len(key) > KEY_MAX:
        raise AvatarError(f"That name is too long; keep it under {KEY_MAX} characters.")
    if key in _FILENAMES:
        raise AvatarError(
            f"`{key}` is one of the built-in occasions. Pick another name - the "
            "built-in ones are rules in the code and cannot be replaced from here."
        )
    return key


def parse_day(raw: str) -> date:
    """DD/MM/YYYY, or YYYY-MM-DD for anyone who thinks in ISO."""
    text = raw.strip().replace(".", "/").replace(" ", "")
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise AvatarError(f"`{raw}` is not a date I can read. Use DD/MM/YYYY, like 24/12/2026.")


def check_window(start: date, end: date, *, ignore: Optional[str] = None) -> None:
    """Refuse a window that runs backwards, or that lands on an occupied day."""
    if end < start:
        raise AvatarError("The end date is before the start date.")
    if (end - start).days > 366:
        raise AvatarError("That window is over a year long. Split it up.")
    clash = conflicts_with(start, end, ignore=ignore)
    if clash:
        listed = "\n".join(
            f"- `{key}` - {s:%d %b %Y} to {e:%d %b %Y}" for key, s, e in clash[:6]
        )
        raise AvatarError(
            "Those dates are already taken:\n" + listed +
            "\n\nPick dates that do not overlap, or remove the one in the way first."
        )


def add_custom(key: str, raw_image: bytes, start: date, end: date, added_by: int) -> None:
    """Save an uploaded picture and claim its window. Validates before writing."""
    key = check_key(key)
    check_window(start, end, ignore=key)
    if len(raw_image) > MAX_UPLOAD_BYTES:
        raise AvatarError("That image is too big. Keep it under 8 MB.")

    try:
        payload = _encode_bytes(raw_image)
    except Exception as exc:
        raise AvatarError("I could not read that as an image.") from exc

    try:
        CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        target = CUSTOM_DIR / f"{key}.png"
        tmp = target.with_suffix(".png.tmp")
        tmp.write_bytes(payload)
        tmp.replace(target)
    except Exception as exc:
        raise AvatarError("I could not save the image. Nothing changed.") from exc

    data = _read_customs()
    data[key] = {
        "file": target.name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "added_by": int(added_by),
        "added": datetime.now(TIMEZONE).isoformat(),
    }
    if not _write_customs(data):
        raise AvatarError("I saved the image but could not write the dates. Try again.")
    log.info("custom avatar %r added: %s to %s", key, start, end)


def remove_custom(key: str) -> bool:
    """Forget a hand-added avatar. The image goes with it."""
    data = _read_customs()
    rec = data.pop(key, None)
    if rec is None:
        return False
    if not _write_customs(data):
        raise AvatarError("Could not write the file, so nothing changed.")
    try:
        (CUSTOM_DIR / str(rec.get("file", ""))).unlink(missing_ok=True)
    except Exception:
        log.debug("could not delete the image for %r", key, exc_info=True)
    log.info("custom avatar %r removed", key)
    return True


# ──────────────────────────────────────────────────────────────
# Image prep
# ──────────────────────────────────────────────────────────────
def _encode_bytes(raw: bytes) -> bytes:
    """The same treatment, for an image that arrived over Discord."""
    return _encode_image(io.BytesIO(raw))


def _encode_avatar(path: Path) -> bytes:
    """Downscale and compress so the upload payload stays small."""
    return _encode_image(path)


def _encode_image(source) -> bytes:
    with Image.open(source) as im:
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
        # Members with an upload open. One wait each: two at once and the first
        # picture posted would satisfy both, saving one image under two names.
        self._awaiting_avatar: set[int] = set()

    # ---------------- State ----------------

    def _read_state(self) -> dict:
        data = storage.load_json(STATE_FILE, default={})
        return data if isinstance(data, dict) else {}

    def _write_state(self, key: str, override: Optional[str]) -> None:
        storage.save_json(
            STATE_FILE,
            {
                "occasion": key,
                "override": override,
                "updated": datetime.now(TIMEZONE).isoformat(),
            },
        )

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

        # Resolved through path_for rather than off _FILENAMES, so a hand-added
        # avatar is looked up the same way a built-in one is. A key with no image
        # behind it falls back rather than failing: an occasion whose file went
        # missing should cost the occasion, not the profile picture.
        path = path_for(want)
        if path is None:
            log.warning("No image for %r - falling back to the default", want)
            want, override = DEFAULT_KEY, None
            path = path_for(want)

        if want == current and not force:
            self._write_state(want, override)  # persist override change even if image is same
            return want, False, "already current"

        if path is None:
            log.error("The default avatar image is missing from %s", AVATAR_DIR)
            return want, False, "the image is missing"

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
        if interaction.channel_id in CONTROL_CHANNEL_IDS:
            return False
        await interaction.response.send_message(
            f"Not here. Use <#{CONTROL_CHANNEL_ID}>. 😼",
            ephemeral=True,
        )
        return True

    async def _key_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete rather than a fixed choice list.

        Choices are baked in at import; a picture added last week would never
        appear in one. Reading the keys per keystroke is what keeps typing the
        command and pressing the button offering the same set.
        """
        needle = (current or "").lower()
        keys = ["auto"] + known_keys()
        return [
            app_commands.Choice(name=k, value=k) for k in keys if needle in k.lower()
        ][:25]

    @app_commands.command(
        name="avatar_set",
        description="Pick Mittens' profile picture manually (pins it until set back to auto).",
    )
    @app_commands.describe(pick="Choose an avatar, or 'auto' to follow the calendar again.")
    @app_commands.autocomplete(pick=_key_choices)
    @app_commands.default_permissions(manage_guild=True)
    async def avatar_set(self, interaction: discord.Interaction, pick: str):
        if await self._wrong_channel(interaction):
            return
        pick = (pick or "").strip().lower()
        if pick and pick != "auto" and pick not in known_keys():
            return await interaction.response.send_message(
                f"There is no avatar called `{pick}`. `/avatar_schedule` lists them.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)

        if pick in ("", "auto"):
            occasion, changed, detail = await self._apply(clear_override=True)
            note = f"Back on schedule — currently **{occasion}** ({detail})."
        else:
            occasion, changed, detail = await self._apply(set_override=pick, force=True)
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

    # ---------------- Hand-added avatars ----------------

    @app_commands.command(
        name="avatar_add",
        description="Add a picture of your own and the dates it should be up.",
    )
    @app_commands.describe(
        name="What to call it, e.g. anniversary.",
        image="The picture. Square-ish works best.",
        start="First day it goes up. DD/MM/YYYY.",
        end="Last day it stays up. DD/MM/YYYY.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def avatar_add(
        self,
        interaction: discord.Interaction,
        name: str,
        image: discord.Attachment,
        start: str,
        end: str,
    ):
        if await self._wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if not (image.content_type or "").startswith("image/"):
            return await interaction.followup.send(
                "❌ That attachment is not an image.", ephemeral=True
            )
        try:
            first, last = parse_day(start), parse_day(end)
            raw = await image.read()
            await asyncio.to_thread(
                add_custom, name, raw, first, last, interaction.user.id
            )
        except AvatarError as exc:
            return await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        except Exception:
            log.exception("avatar_add failed")
            return await interaction.followup.send(
                "❌ That did not take. The log has the details.", ephemeral=True
            )
        await interaction.followup.send(
            await self._added_note(check_key(name), first, last), ephemeral=True
        )

    async def _added_note(self, key: str, first, last) -> str:
        """Confirm, and say whether it is up now or waiting its turn."""
        today = datetime.now(TIMEZONE).date()
        if first <= today <= last:
            _, changed, detail = await self._apply(force=True)
            live = " It is up now." if changed else f" ({detail})"
        else:
            live = f" It goes up on {first:%d %b %Y}."
        return f"✅ Saved **{key}** for {first:%d %b %Y} → {last:%d %b %Y}.{live}"

    @app_commands.command(
        name="avatar_remove",
        description="Remove a picture that was added by hand.",
    )
    @app_commands.describe(name="Which one. Only hand-added ones can go.")
    @app_commands.default_permissions(manage_guild=True)
    async def avatar_remove(self, interaction: discord.Interaction, name: str):
        if await self._wrong_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        key = (name or "").strip().lower()
        try:
            gone = await asyncio.to_thread(remove_custom, key)
        except AvatarError as exc:
            return await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        if not gone:
            return await interaction.followup.send(
                f"❌ There is no added avatar called `{key}`. "
                "The built-in occasions live in the code and stay there.",
                ephemeral=True,
            )
        # If the one just removed was the one on his face, put the right one back.
        state = self._read_state()
        if state.get("occasion") == key or state.get("override") == key:
            await self._apply(clear_override=state.get("override") == key, force=True)
        await interaction.followup.send(f"✅ **{key}** is gone.", ephemeral=True)

    @avatar_remove.autocomplete("name")
    async def _remove_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = (current or "").lower()
        return [
            app_commands.Choice(name=f"{k} ({s:%d %b %Y} - {e:%d %b %Y})"[:100], value=k)
            for k, s, e in custom_windows()
            if needle in k.lower()
        ][:25]

    async def await_custom_upload(
        self, interaction: discord.Interaction, key: str, first: date, last: date
    ) -> None:
        """Take the next picture this person posts here and make it an avatar.

        A button cannot open an upload box: Discord modals hold text inputs and
        nothing else, which is the same wall /event hit with its cover image and
        the pet panel hit with photo swaps. So the panel asks for the name and
        the dates in a modal, and this watches the channel for the picture —
        the same shape pet_care already uses, because members have met it there.

        The dates are checked before the wait rather than after it: being told
        the window is taken should not cost you an upload.
        """
        channel = interaction.channel
        if channel is None:
            return
        if interaction.user.id in self._awaiting_avatar:
            return await interaction.response.send_message(
                "📷 I am already waiting on a picture from you. Post it, or leave it a minute.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"📷 Post the picture for **{key}** in this channel in the next "
            f"{UPLOAD_WAIT_SECONDS // 60} minutes and I will save it for "
            f"{first:%d %b %Y} → {last:%d %b %Y}.",
            ephemeral=True,
        )

        def is_the_photo(message: discord.Message) -> bool:
            return (
                message.author.id == interaction.user.id
                and message.channel.id == channel.id
                and any(
                    (a.content_type or "").startswith("image/") for a in message.attachments
                )
            )

        self._awaiting_avatar.add(interaction.user.id)
        try:
            message = await self.bot.wait_for(
                "message", check=is_the_photo, timeout=UPLOAD_WAIT_SECONDS
            )
        except asyncio.TimeoutError:
            return await interaction.edit_original_response(
                content=f"📷 Nothing arrived, so **{key}** was not saved. Press it again when you have the picture."
            )
        finally:
            self._awaiting_avatar.discard(interaction.user.id)

        attachment = next(
            a for a in message.attachments if (a.content_type or "").startswith("image/")
        )
        try:
            raw = await attachment.read()
            await asyncio.to_thread(add_custom, key, raw, first, last, interaction.user.id)
        except AvatarError as exc:
            return await interaction.edit_original_response(content=f"❌ {exc}")
        except Exception:
            log.exception("panel avatar upload failed")
            return await interaction.edit_original_response(
                content="❌ That did not take. The log has the details."
            )
        await interaction.edit_original_response(
            content=await self._added_note(key, first, last)
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

        # Listed apart from the calendar rules above, because these are the ones
        # somebody can change: they have dates a person chose and can remove.
        added = [row for row in custom_windows() if row[2] >= today]
        past = len(custom_windows()) - len(added)
        if added:
            lines.append("")
            lines.append("**Added by hand**")
            for key, start, end in added:
                now_note = " ← up now" if start <= today <= end else ""
                lines.append(f"`{key}` — {start:%d %b %Y} → {end:%d %b %Y}{now_note}")
        if past:
            lines.append(f"*…and {past} added one(s) whose dates have passed.*")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SeasonalAvatar(bot))
