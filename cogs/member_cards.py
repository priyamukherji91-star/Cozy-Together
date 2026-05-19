from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

LOG = logging.getLogger(__name__)

# Channel where !register / !whoami may be used and where cards are posted.
MEMBER_CARD_CHANNEL_ID = 1436115021066408016

# Railway Volume Storage (matches birthday.py / morning_news.py convention).
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_PATH = DATA_DIR / "member_cards.json"

FONT_DIR  = Path(__file__).resolve().parent.parent / "assets" / "fonts"
LODESTONE = "https://eu.finalfantasyxiv.com/lodestone/character"
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Palette ───────────────────────────────────────────────────────────────────
_BG_TOP  = (0x1a, 0x1d, 0x29)
_BG_BOT  = (0x0f, 0x11, 0x18)
_TEXT    = (220, 220, 220, 255)
_LABEL   = (160, 163, 181, 255)
_MUTED   = (110, 113, 131, 255)
_DIVIDER = (52,  55,  68,  255)
_GOLD    = (212, 175,  55, 255)

GC_COLORS: dict[str, tuple[int, int, int, int]] = {
    "Maelstrom":               (170,  28,  28, 255),
    "Order of the Twin Adder": (184, 148,  10, 255),
    "Immortal Flames":         (191,  74,   0, 255),
}
_ACCENT_DEFAULT = (59, 154, 192, 255)

# ── Job table ─────────────────────────────────────────────────────────────────
_JOB: dict[str, tuple[str, str]] = {
    "Paladin / Gladiator":       ("PLD", "Tank"),
    "Warrior / Marauder":        ("WAR", "Tank"),
    "Dark Knight":               ("DRK", "Tank"),
    "Gunbreaker":                ("GNB", "Tank"),
    "White Mage / Conjurer":     ("WHM", "Healer"),
    "Scholar":                   ("SCH", "Healer"),
    "Astrologian":               ("AST", "Healer"),
    "Sage":                      ("SGE", "Healer"),
    "Monk / Pugilist":           ("MNK", "Melee"),
    "Dragoon / Lancer":          ("DRG", "Melee"),
    "Ninja / Rogue":             ("NIN", "Melee"),
    "Samurai":                   ("SAM", "Melee"),
    "Reaper":                    ("RPR", "Melee"),
    "Viper":                     ("VPR", "Melee"),
    "Bard / Archer":             ("BRD", "Phys Ranged"),
    "Machinist":                 ("MCH", "Phys Ranged"),
    "Dancer":                    ("DNC", "Phys Ranged"),
    "Black Mage / Thaumaturge":  ("BLM", "Magic Ranged"),
    "Summoner / Arcanist":       ("SMN", "Magic Ranged"),
    "Red Mage":                  ("RDM", "Magic Ranged"),
    "Pictomancer":               ("PCT", "Magic Ranged"),
    "Blue Mage (Limited Job)":   ("BLU", "Magic Ranged"),
    "Carpenter":                 ("CRP", "Crafter"),
    "Blacksmith":                ("BSM", "Crafter"),
    "Armorer":                   ("ARM", "Crafter"),
    "Goldsmith":                 ("GSM", "Crafter"),
    "Leatherworker":             ("LTW", "Crafter"),
    "Weaver":                    ("WVR", "Crafter"),
    "Alchemist":                 ("ALC", "Crafter"),
    "Culinarian":                ("CUL", "Crafter"),
    "Miner":                     ("MIN", "Gatherer"),
    "Botanist":                  ("BTN", "Gatherer"),
    "Fisher":                    ("FSH", "Gatherer"),
}
_ROLE_ORDER = ["Tank", "Healer", "Melee", "Phys Ranged", "Magic Ranged", "Crafter", "Gatherer"]


# ── Helpers ───────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _load() -> dict:
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {}


def _save(data: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _t(soup: BeautifulSoup, sel: str, default: str = "?") -> str:
    el = soup.select_one(sel)
    return el.get_text(strip=True) if el else default


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size[0] - 1, size[1] - 1)], radius=radius, fill=255
    )
    return mask


class MemberCards(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._icon_cache: dict[str, bytes] = {}
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        for style in ("reg", "bold", "ital"):
            for size in (10, 11, 12, 13, 14, 17, 32):
                self._f(style, size)

    async def cog_unload(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _f(self, style: str, size: int) -> ImageFont.FreeTypeFont:
        key = (style, size)
        if key in self._fonts:
            return self._fonts[key]
        paths = {
            "reg":  FONT_DIR / "NotoSans-Regular.ttf",
            "bold": FONT_DIR / "NotoSans-Bold.ttf",
            "ital": FONT_DIR / "NotoSans-Italic.ttf",
        }
        try:
            font = ImageFont.truetype(str(paths.get(style, paths["reg"])), size)
        except Exception:
            font = ImageFont.load_default()
        self._fonts[key] = font
        return font

    def _in_card_channel(self, ctx: commands.Context) -> bool:
        return ctx.channel.id == MEMBER_CARD_CHANNEL_ID

    # ── Network ───────────────────────────────────────────────────────────────
    async def _get(self, url: str, **kwargs) -> Optional[str]:
        try:
            async with self._session.get(
                url, headers=_UA, timeout=aiohttp.ClientTimeout(total=15), **kwargs
            ) as r:
                return await r.text() if r.status == 200 else None
        except Exception:
            LOG.exception("Lodestone fetch failed: %s", url)
            return None

    async def _fetch_bytes(self, url: str) -> Optional[bytes]:
        if url in self._icon_cache:
            return self._icon_cache[url]
        try:
            async with self._session.get(
                url, headers=_UA, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    data = await r.read()
                    self._icon_cache[url] = data
                    return data
        except Exception:
            LOG.warning("Image fetch failed: %s", url)
        return None

    async def _search(self, name: str, server: str) -> Optional[tuple[str, str | None]]:
        query = _norm(name)
        LOG.info("[register] search name=%r world=%r  normalised=%r", name, server, query)
        _CHAR_HREF = re.compile(r"/lodestone/character/(\d+)/")

        def _exact_from_html(html: str) -> Optional[tuple[str, str | None]]:
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.find_all("a", href=_CHAR_HREF):
                m = _CHAR_HREF.search(link["href"])
                if not m:
                    continue
                char_id = m.group(1)
                img_el  = link.find("img")
                avatar  = img_el.get("src") if img_el else None

                # Walk up to the entry container for this result
                container = link
                for _ in range(10):
                    container = container.parent
                    if container is None:
                        break
                    tag = getattr(container, "name", "")
                    cls = " ".join(container.get("class") or []).lower()
                    if tag == "li" or "entry" in cls:
                        break

                if container is None:
                    continue

                # Only match if the exact name appears as a text node within this entry
                for text_node in container.find_all(string=True):
                    if _norm(str(text_node)) == query:
                        LOG.info("[register]   -> id=%s matched name=%r in entry", char_id, query)
                        return char_id, avatar
            return None

        # Pass 1: full name search
        html = await self._get(f"{LODESTONE}/", params={"q": name, "worldname": server})
        if html:
            result = _exact_from_html(html)
            if result:
                LOG.info("[register]   => exact match (full-name search) id=%s", result[0])
                return result

        # Pass 2: last-name-only search (catches cases where Lodestone fuzzes the first name)
        last_word = name.rsplit(None, 1)[-1]
        if _norm(last_word) != query:
            LOG.info("[register]   => retrying with last name only: %r", last_word)
            html2 = await self._get(f"{LODESTONE}/", params={"q": last_word, "worldname": server})
            if html2:
                result = _exact_from_html(html2)
                if result:
                    LOG.info("[register]   => exact match (last-name search) id=%s", result[0])
                    return result

        LOG.info("[register]   => no exact match found")
        return None

    async def _profile(self, char_id: str, avatar_seed: str | None = None) -> Optional[dict]:
        html = await self._get(f"{LODESTONE}/{char_id}/")
        if not html:
            return None
        return self._parse(char_id, html, avatar_seed)

    # ── Parsing ───────────────────────────────────────────────────────────────
    def _parse(self, char_id: str, html: str, avatar_seed: str | None) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        name  = _t(soup, ".frame__chara__name", "Unknown")
        title = _t(soup, ".frame__chara__title", "")

        world_raw = _t(soup, ".frame__chara__world", "")
        wm     = re.search(r"(.+?)\[(.+?)\]", world_raw)
        server = wm.group(1).strip() if wm else "?"
        dc     = wm.group(2).strip() if wm else "?"

        face_img = (
            soup.select_one(".frame__chara__face img") or
            soup.select_one(".character-block__face img")
        )
        avatar = (face_img.get("src") if face_img else None) or avatar_seed

        port_el  = soup.select_one(".character__detail__image a")
        portrait = port_el.get("href") if port_el else None

        blocks: dict[str, str] = {}
        nameday = "?"
        for title_el in soup.select(".character-block__box .character-block__title"):
            key     = title_el.get_text(strip=True).lower()
            next_el = title_el.find_next_sibling()
            if next_el is None:
                continue
            classes = next_el.get("class") or []
            if "character-block__birth" in classes:
                nameday = next_el.get_text(strip=True)
            elif "character-block__name" in classes:
                blocks[key] = next_el.get_text(separator="\n", strip=True)

        race_raw   = blocks.get("race/clan/gender", "")
        race_lines = [ln.strip() for ln in race_raw.split("\n") if ln.strip()]
        race  = race_lines[0] if race_lines else "?"
        tribe = race_lines[1].split(" / ")[0].strip() if len(race_lines) > 1 else "?"

        guardian = blocks.get("guardian") or blocks.get("guardian deity") or "?"

        gc_raw   = blocks.get("grand company", "")
        gc_parts = [p.strip() for p in gc_raw.split("\n")] if gc_raw else []
        gc_parts = [p for p in gc_parts if p]
        if len(gc_parts) == 1 and " / " in gc_parts[0]:
            gc_parts = [p.strip() for p in gc_parts[0].split(" / ")]
        gc_name = gc_parts[0] if gc_parts else ""
        gc_rank = gc_parts[1] if len(gc_parts) > 1 else ""

        fc_el   = soup.select_one(".character__freecompany__name h4 a")
        fc_name = fc_el.get_text(strip=True) if fc_el else ""

        icon_map: dict[str, tuple[str, str, str]] = {}
        jobs_by_role: dict[str, list[tuple[str, int, str]]] = {r: [] for r in _ROLE_ORDER}

        for li in soup.select(".character__level__list li"):
            img_el  = li.find("img")
            if not img_el:
                continue
            tooltip = img_el.get("data-tooltip", "")
            src     = img_el.get("src", "")
            try:
                lvl = int(li.get_text(strip=True))
            except ValueError:
                lvl = 0
            info = _JOB.get(tooltip)
            if not info:
                continue
            abbr, role = info
            display = tooltip.split(" / ")[0].split(" (")[0]
            if src:
                icon_map[src] = (display, abbr, role)
            if role in jobs_by_role:
                jobs_by_role[role].append((abbr, lvl, src))

        active_job = "?"
        active_lvl: int | str = "?"
        lvl_p = soup.select_one(".character__class__data p")
        if lvl_p:
            lvl_m = re.search(r"\d+", lvl_p.get_text())
            if lvl_m:
                active_lvl = int(lvl_m.group())
        active_icon = soup.select_one(".character__class_icon img")
        if active_icon:
            match = icon_map.get(active_icon.get("src", ""))
            if match:
                active_job = match[0]

        return {
            "Character": {
                "ID":             int(char_id),
                "Name":           name,
                "Title":          {"Name": title},
                "Race":           {"Name": race},
                "Tribe":          {"Name": tribe},
                "Nameday":        nameday,
                "GuardianDeity":  {"Name": guardian},
                "Server":         server,
                "DC":             dc,
                "GrandCompany":   {"Company": {"Name": gc_name}, "Rank": {"Name": gc_rank}},
                "FreeCompanyName": fc_name,
                "Avatar":         avatar,
                "Portrait":       portrait,
                "ActiveClassJob": {"Job": {"Name": active_job}, "Level": active_lvl},
            },
            "JobsByRole": jobs_by_role,
        }

    def _dig(self, obj: object, *keys: str, fallback: str = "?") -> str:
        for k in keys:
            if not isinstance(obj, dict):
                return fallback
            obj = obj.get(k)
            if obj is None:
                return fallback
        return str(obj) if obj else fallback

    # ── PNG rendering ─────────────────────────────────────────────────────────
    async def _render_card(self, profile: dict, icon_data: dict[str, bytes]) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._render_sync, profile, icon_data)

    def _render_sync(self, profile: dict, icon_data: dict[str, bytes]) -> bytes:  # noqa: C901
        # ── Canvas constants ──────────────────────────────────────────────
        W     = 800
        H_MAX = 1100   # draw here; crop tight to content at the end

        # Left column (portrait)
        PX  = 18          # portrait x start (after 6px stripe + gap)
        PY  = 18          # portrait y start
        PW  = 310         # portrait width

        # Right column (all text + jobs)
        TX    = PX + PW + 14     # = 342
        MR    = 14               # right margin
        RCW   = W - TX - MR     # right column width = 444

        # Job tile geometry sized to fit 5 per row in the right column
        ICON_S = 28
        TILE_W = RCW // 5        # = 88  (5 per row exactly)
        TILE_H = 42

        # ── Unpack profile ────────────────────────────────────────────────
        char         = profile["Character"]
        jobs_by_role = profile.get("JobsByRole") or {}
        name         = char.get("Name", "Unknown")
        title        = self._dig(char, "Title",         "Name", fallback="")
        server       = char.get("Server", "?")
        dc           = char.get("DC",     "?")
        race         = self._dig(char, "Race",  "Name")
        tribe        = self._dig(char, "Tribe", "Name")
        guardian     = self._dig(char, "GuardianDeity", "Name")
        gc_name      = self._dig(char, "GrandCompany", "Company", "Name", fallback="")
        fc_name      = char.get("FreeCompanyName") or ""
        nameday      = char.get("Nameday", "?")
        portrait_url = char.get("Portrait")

        accent = GC_COLORS.get(gc_name, _ACCENT_DEFAULT)
        f      = self._f

        # ── Gradient background ───────────────────────────────────────────
        r0, g0, b0 = _BG_TOP
        r1, g1, b1 = _BG_BOT
        grad = Image.new("RGBA", (1, H_MAX))
        for y in range(H_MAX):
            t = y / (H_MAX - 1)
            grad.putpixel((0, y), (
                int(r0 + (r1 - r0) * t),
                int(g0 + (g1 - g0) * t),
                int(b0 + (b1 - b0) * t),
                255,
            ))
        img  = grad.resize((W, H_MAX), Image.NEAREST)
        draw = ImageDraw.Draw(img)

        # ── RIGHT COLUMN — header text ────────────────────────────────────
        ty = PY + 6
        draw.text((TX, ty), name, font=f("bold", 32), fill=_TEXT)
        ty += 42
        if title:
            draw.text((TX, ty), title, font=f("ital", 17), fill=accent)
            ty += 26
        draw.text((TX, ty), f"{server}  [{dc}]", font=f("reg", 14), fill=_LABEL)
        ty += 22
        if fc_name:
            draw.text((TX, ty), f"《{fc_name}》", font=f("reg", 14), fill=_MUTED)
            ty += 22

        # ── RIGHT COLUMN — info block (two sub-cols) ──────────────────────
        IY      = ty + 16
        LX_info = TX
        RX_info = TX + 222   # right sub-col starts here

        def draw_info(x: int, y: int, label: str, value: str) -> int:
            draw.text((x, y),      label.upper(), font=f("bold", 12), fill=_LABEL)
            draw.text((x, y + 16), value or "—",  font=f("reg",  14), fill=_TEXT)
            return y + 16 + 18 + 8   # next y (= +42)

        race_clan = f"{race}, {tribe}" if tribe not in ("?", "") else race

        ly = IY
        ly = draw_info(LX_info, ly, "Race / Clan",  race_clan)
        ly = draw_info(LX_info, ly, "Guardian",     guardian)
        ly = draw_info(LX_info, ly, "Grand Company", gc_name or "—")

        ry = IY
        ry = draw_info(RX_info, ry, "Nameday",      nameday)
        ry = draw_info(RX_info, ry, "Free Company", fc_name or "—")

        # Thin divider between info and jobs (right column only)
        div_y = max(ly, ry) + 10
        draw.line([(TX, div_y), (W - MR, div_y)], fill=_DIVIDER, width=1)

        # ── RIGHT COLUMN — jobs section ───────────────────────────────────
        JY = div_y + 12

        for role in _ROLE_ORDER:
            jobs = jobs_by_role.get(role, [])
            if not jobs:
                continue

            draw.text((TX, JY), role.upper(), font=f("bold", 12), fill=accent)
            JY += 17

            jx = TX
            for i, (abbr, lvl, icon_url) in enumerate(jobs):
                ib = icon_data.get(icon_url)
                if ib:
                    try:
                        ico = Image.open(io.BytesIO(ib)).convert("RGBA")
                        ico = ico.resize((ICON_S, ICON_S), Image.LANCZOS)
                        img.paste(ico, (jx, JY + 3), ico)
                    except Exception:
                        pass

                if lvl >= 100:
                    draw.rectangle(
                        [(jx - 1, JY + 2), (jx + ICON_S, JY + ICON_S + 3)],
                        outline=_GOLD, width=2,
                    )

                tx = jx + ICON_S + 4
                draw.text((tx, JY + 3),  abbr,    font=f("bold", 11), fill=_TEXT)
                lvl_str = str(lvl) if lvl > 0 else "—"
                draw.text((tx, JY + 16), lvl_str, font=f("reg",  11),
                          fill=_GOLD if lvl >= 100 else _MUTED)

                jx += TILE_W
                if i < len(jobs) - 1 and jx + TILE_W > TX + RCW:
                    jx  = TX
                    JY += TILE_H

            JY += TILE_H + 6

        # ── Calculate portrait height to match right-column content ───────
        crop_h = JY + 16
        PH     = crop_h - PY - 16   # portrait runs from PY to (crop_h - bottom_margin)

        # ── LEFT COLUMN — full-body portrait, zoomed to fill (PW × PH) ───
        portrait_bytes = icon_data.get(portrait_url) if portrait_url else None
        if portrait_bytes:
            try:
                raw = Image.open(io.BytesIO(portrait_bytes)).convert("RGBA")
                # ImageOps.fit zoom-crops to exactly (PW, PH).
                # centering=(0.5, 0.15) keeps the character's head near the top
                # while still showing as much of the body as possible.
                raw  = ImageOps.fit(raw, (PW, PH), method=Image.LANCZOS,
                                    centering=(0.5, 0.15))
                mask = _rounded_mask((PW, PH), radius=10)
                raw.putalpha(mask)
                img.paste(raw, (PX, PY), raw)
            except Exception:
                LOG.exception("Portrait render failed")

        draw.rounded_rectangle(
            [(PX, PY), (PX + PW - 1, PY + PH - 1)],
            radius=10, outline=accent, width=2,
        )

        # ── Crop and draw accent stripe on top ────────────────────────────
        img      = img.crop((0, 0, W, crop_h))
        top_draw = ImageDraw.Draw(img)
        top_draw.rectangle([(0, 0), (5, crop_h - 1)], fill=accent)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf.read()

    # ── Shared fetch + render ─────────────────────────────────────────────────
    async def _card_file(self, profile: dict) -> discord.File:
        char         = profile["Character"]
        jobs_by_role = profile.get("JobsByRole") or {}

        urls: list[str] = []
        if url := char.get("Portrait"):
            urls.append(url)
        for role_jobs in jobs_by_role.values():
            for _, _, icon_url in role_jobs:
                if icon_url and icon_url not in urls:
                    urls.append(icon_url)

        async def _fetch(u: str) -> tuple[str, bytes | None]:
            return u, await self._fetch_bytes(u)

        pairs     = await asyncio.gather(*[_fetch(u) for u in urls])
        icon_data = {u: d for u, d in pairs if d}

        png = await self._render_card(profile, icon_data)
        return discord.File(io.BytesIO(png), filename="card.png")

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.command(name="register")
    async def register(self, ctx: commands.Context, *, args: str = "") -> None:
        """!register <name> <server>  |  !register <lodestone_id>  |  !register <lodestone_url>"""
        if not self._in_card_channel(ctx):
            return

        target = ctx.author
        char_id: Optional[str] = None
        avatar_seed: Optional[str] = None

        # Direct Lodestone URL or bare numeric ID — skips search entirely
        url_m = re.search(r"/lodestone/character/(\d+)", args)
        if url_m:
            char_id = url_m.group(1)
        elif args.strip().isdigit():
            char_id = args.strip()

        # Name + server search (only when no direct ID was provided)
        if char_id is None:
            parts = args.rsplit(None, 1)
            if len(parts) < 2:
                await ctx.send(
                    "Usage: `!register <character name> <server>`\n"
                    "If search picks the wrong character, use `!register <id>` with your Lodestone character ID instead.",
                    delete_after=15,
                )
                return
            char_name, server = parts[0], parts[1]
            LOG.info("[register] parsed: char_name=%r  server=%r  target=%s", char_name, server, target.id)

            async with ctx.typing():
                result = await self._search(char_name, server)
            if not result:
                await ctx.send(
                    f"Couldn't find **{char_name}** on **{server}**. "
                    "Check the spelling, or use `!register <id>` with your Lodestone character ID.",
                    delete_after=15,
                )
                return
            char_id, avatar_seed = result

        async with ctx.typing():
            profile = await self._profile(char_id, avatar_seed)
            if not profile:
                await ctx.send(
                    "Found the character but couldn't load their profile — try again in a moment.",
                    delete_after=12,
                )
                return

            cards = _load()
            cards.setdefault(str(ctx.guild.id), {})[str(target.id)] = {
                "char_id":       char_id,
                "char_name":     profile["Character"]["Name"],
                "world":         profile["Character"].get("Server", "?"),
                "registered_at": datetime.now(timezone.utc).date().isoformat(),
            }
            _save(cards)

            await ctx.send(file=await self._card_file(profile))

    @commands.command(name="whoami")
    async def whoami(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """!whoami  or  !whoami @user"""
        if not self._in_card_channel(ctx):
            return
        target = member or ctx.author
        entry  = _load().get(str(ctx.guild.id), {}).get(str(target.id))

        if not entry:
            msg = (
                "You haven't registered yet — use `!register <name> <server>`."
                if target == ctx.author
                else f"{target.display_name} hasn't registered a character yet."
            )
            await ctx.send(msg, delete_after=10)
            return

        async with ctx.typing():
            profile = await self._profile(entry["char_id"])
            if not profile:
                await ctx.send(
                    "Couldn't fetch character data right now — try again in a moment.",
                    delete_after=10,
                )
                return
            await ctx.send(file=await self._card_file(profile))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberCards(bot))
