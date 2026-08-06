# newsroom/newspaper.py
# -*- coding: utf-8 -*-
"""Renders Mitten's Morning News as a newspaper front page PNG.

Lives outside cogs/ on purpose: bot.py auto-loads cogs/*.py (and top-level *.py)
as discord.py extensions, and this module has no setup(bot).

The page is portrait and its WIDTH is fixed; the height grows with the copy, so a
busy day produces a longer paper rather than truncated stories. Everything is
drawn onto an over-tall canvas which is cropped to the used height at the end —
there is no separate measure pass to drift out of sync with the draw pass.
"""
from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFile, ImageFilter, ImageFont

LOG = logging.getLogger(__name__)

# Members' uploads are not always byte-perfect. A photo missing its last few bytes
# should still print rather than take the whole page down.
ImageFile.LOAD_TRUNCATED_IMAGES = True

ASSETS = Path(__file__).resolve().parent.parent / "assets"
FONT_DIR = ASSETS / "fonts"
ROMAN_FONT = FONT_DIR / "NotoSerif[wdth,wght].ttf"
ITALIC_FONT = FONT_DIR / "NotoSerif-Italic[wdth,wght].ttf"
LOGO_PATH = ASSETS / "avatars" / "newsmittens.png"

# How many short items run under ALSO FILED TODAY. One line to change; the model
# prompt and the page layout both read it from here.
BRIEF_COUNT = 8

# ── Page geometry ─────────────────────────────────────────────────────────────
PAGE_W = 1400
MARGIN = 64
CONTENT_W = PAGE_W - 2 * MARGIN          # 1272
GUTTER = 44
COL_W = (CONTENT_W - GUTTER) // 2        # 614
COL_X = (MARGIN, MARGIN + COL_W + GUTTER)

# Drawn onto this, then cropped. Generous enough that no plausible day overflows.
MAX_CANVAS_H = 9000

PHOTO_ASPECT = 1.6                       # hero photo width / height
PHOTO_H = int(round(CONTENT_W / PHOTO_ASPECT))

# ── Palette ───────────────────────────────────────────────────────────────────
PAPER = (246, 242, 232)
INK = (24, 22, 20)
INK_SOFT = (96, 90, 82)
HAIRLINE = (168, 160, 148)

# ── Type scale ────────────────────────────────────────────────────────────────
SZ_MASTHEAD_MAX = 96
SZ_EDITION = 19
SZ_DATELINE = 21
SZ_CAPTION_LABEL = 17
SZ_CAPTION = 24
SZ_LEAD_HEAD = 76
SZ_LEAD_BODY = 25
SZ_TEASE = 27
SZ_SECTION = 27
SZ_BRIEF_HEAD = 29
SZ_BRIEF_BODY = 21
SZ_COLOPHON = 16

LEADING = 1.42          # body copy line height multiplier
HEAD_LEADING = 1.06     # headlines set tight

TRACK_MASTHEAD = 2.0
TRACK_LABEL = 2.2
TRACK_SECTION = 4.0


# ──────────────────────────────────────────────────────────────
# CONTENT
# ──────────────────────────────────────────────────────────────
@dataclass
class NewspaperContent:
    paper_name: str
    edition_line: str
    dateline: str
    messages_line: str
    lead_headline: str
    lead_body: str
    tease: str
    briefs: list[tuple[str, str]] = field(default_factory=list)
    photo_bytes: bytes | None = None
    photo_label: str = ""
    photo_caption: str = ""
    colophon: str = ""

    # Set by render_front_page: True only once the photo has actually been drawn
    # onto the page. Supplying bytes is not enough — they may still fail to decode,
    # and a caller that treats a photo as spent before it prints loses it.
    photo_printed: bool = False


# ──────────────────────────────────────────────────────────────
# FONTS
# ──────────────────────────────────────────────────────────────
# Noto Serif ships one variable file per style; the named instances differ between
# them (the italic has "Italic"/"Bold Italic", not "Regular"/"Bold").
_ITALIC_NAMES = {
    "Regular": "Italic",
    "Medium": "Medium Italic",
    "SemiBold": "SemiBold Italic",
    "Bold": "Bold Italic",
    "ExtraBold": "ExtraBold Italic",
    "Black": "Black Italic",
}


@lru_cache(maxsize=128)
def face(size: int, weight: str = "Regular", italic: bool = False) -> ImageFont.FreeTypeFont:
    """A configured font face, cached by (size, weight, italic).

    set_variation_by_name MUTATES the font object, so each face is configured once
    here at creation and an unset one is never handed out.
    """
    path = ITALIC_FONT if italic else ROMAN_FONT
    font = ImageFont.truetype(str(path), size)
    name = _ITALIC_NAMES.get(weight, "Italic") if italic else weight
    try:
        font.set_variation_by_name(name)
    except Exception:
        LOG.warning("Font variation %r unavailable in %s", name, path.name)
    return font


def line_height(font: ImageFont.FreeTypeFont, mult: float = LEADING) -> int:
    ascent, descent = font.getmetrics()
    return int(round((ascent + descent) * mult))


# ──────────────────────────────────────────────────────────────
# TEXT
# ──────────────────────────────────────────────────────────────
# Noto Serif has no emoji or dingbats; anything it cannot draw comes out as tofu.
_UNPRINTABLE_RE = re.compile(
    "["
    "\U00010000-\U0010ffff"   # astral plane: emoji, most pictographs
    "←-⯿"           # arrows, technical, dingbats, misc symbols
    "︀-️"           # variation selectors
    "​-‏‪-‮⁠"
    "\x00-\x08\x0b-\x1f\x7f"
    "]+"
)
_WS_RE = re.compile(r"\s+")


def sanitize(text: str) -> str:
    return _WS_RE.sub(" ", _UNPRINTABLE_RE.sub("", text or "")).strip()


def wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Greedy word wrap. Words wider than the measure are hard-split rather than
    allowed to run into the gutter."""
    text = sanitize(text)
    if not text:
        return []

    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if not current or font.getlength(candidate) <= max_w:
            current = candidate
            continue
        lines.append(current)
        current = word

        while font.getlength(current) > max_w and len(current) > 1:
            cut = len(current) - 1
            while cut > 1 and font.getlength(current[:cut]) > max_w:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]

    if current:
        lines.append(current)
    return lines


def tracked_width(text: str, font: ImageFont.FreeTypeFont, tracking: float) -> float:
    """Pillow has no letter-spacing, so tracked text is measured character by
    character with the manual advance added back in."""
    if not text:
        return 0.0
    return sum(font.getlength(ch) for ch in text) + tracking * (len(text) - 1)


def draw_tracked(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    tracking: float,
) -> float:
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking
    return x


def fit_tracked_size(
    text: str, max_w: int, start: int, weight: str, tracking: float, floor: int = 24
) -> ImageFont.FreeTypeFont:
    """Largest face at which `text` still fits `max_w` when tracked."""
    size = start
    while size > floor:
        font = face(size, weight)
        if tracked_width(text, font, tracking) <= max_w:
            return font
        size -= 2
    return face(floor, weight)


# ──────────────────────────────────────────────────────────────
# LOGO
# ──────────────────────────────────────────────────────────────
@lru_cache(maxsize=4)
def _masthead_logo(path: str, mtime: float) -> Image.Image | None:
    """Load the masthead logo as RGBA, trimmed to its subject.

    The supplied logo is an RGB PNG with a fake "transparency" checkerboard baked
    into the pixels, so it is keyed out here: pick out light neutral pixels, then
    keep only the ones reachable from the border. The connectivity step is what
    protects the white PRESS card and the notepad, which are the same colour but
    enclosed by the subject.
    """
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        LOG.exception("Masthead logo could not be opened: %s", path)
        return None

    try:
        if img.mode in ("RGBA", "LA") or "transparency" in img.info:
            rgba = img.convert("RGBA")
            if rgba.getchannel("A").getextrema()[0] < 250:
                return _trim(rgba)

        rgb = img.convert("RGB")
        w, h = rgb.size
        r, g, b = rgb.split()
        brightest = ImageChops.lighter(ImageChops.lighter(r, g), b)
        darkest = ImageChops.darker(ImageChops.darker(r, g), b)
        saturation = ImageChops.subtract(brightest, darkest)

        candidate = ImageChops.multiply(
            darkest.point(lambda v: 255 if v >= 222 else 0),
            saturation.point(lambda v: 255 if v <= 16 else 0),
        )

        flooded = candidate.copy()
        for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
                     (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)):
            if flooded.getpixel(seed) == 255:
                ImageDraw.floodfill(flooded, seed, 128, thresh=0)

        background = flooded.point(lambda v: 255 if v == 128 else 0)
        if sum(background.histogram()[255:]) < (w * h) * 0.02:
            return _trim(rgb.convert("RGBA"))    # no checkerboard to remove

        # Eroding the alpha by a pixel eats the light fringe the checkerboard
        # leaves along antialiased edges.
        alpha = ImageChops.invert(background).filter(ImageFilter.MinFilter(3))
        out = rgb.convert("RGBA")
        out.putalpha(alpha)
        return _trim(out)
    except Exception:
        LOG.exception("Masthead logo keying failed; using it as-is")
        try:
            return img.convert("RGBA")
        except Exception:
            return None


def _trim(img: Image.Image) -> Image.Image:
    box = img.getchannel("A").getbbox()
    return img.crop(box) if box else img


def load_logo() -> Image.Image | None:
    try:
        return _masthead_logo(str(LOGO_PATH), LOGO_PATH.stat().st_mtime)
    except OSError:
        LOG.warning("Masthead logo missing: %s", LOGO_PATH)
        return None


# ──────────────────────────────────────────────────────────────
# PHOTO
# ──────────────────────────────────────────────────────────────
def _centre_crop(img: Image.Image, ratio: float) -> Image.Image:
    w, h = img.size
    if w / h > ratio:
        cw, ch = int(round(h * ratio)), h
    else:
        cw, ch = w, int(round(w / ratio))
    left = (w - cw) // 2
    top = (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch))


def _best_window(profile: list[float], window: int, centre: float, spread: float) -> int:
    """Index of the `window`-long run of `profile` carrying the most weighted detail.

    Pure edge energy on its own is the wrong objective: a striped cat's body has
    far more of it than its face, so a detail-seeking crop frames the fur and
    takes the head off. Weighting each bin by a gaussian over its position in the
    source biases toward where subjects actually sit, while the spread is wide
    enough that a genuinely off-centre subject still wins.
    """
    n = len(profile)
    if window >= n:
        return 0

    weighted = []
    for i, value in enumerate(profile):
        pos = i / (n - 1) if n > 1 else 0.5
        weighted.append(value * math.exp(-((pos - centre) ** 2) / (2 * spread * spread)))

    prefix = [0.0]
    for value in weighted:
        prefix.append(prefix[-1] + value)

    best_start, best_score = 0, -1.0
    for start in range(0, n - window + 1):
        score = prefix[start + window] - prefix[start]
        if score > best_score:
            best_start, best_score = start, score
    return best_start


def smart_crop(img: Image.Image, ratio: float) -> Image.Image:
    """Crop to `ratio` by choosing the best window rather than the middle."""
    try:
        w, h = img.size
        if w <= 0 or h <= 0:
            return img
        if abs(w / h - ratio) < 0.01:
            return img

        thumb = img.convert("L")
        thumb.thumbnail((240, 240), Image.Resampling.BILINEAR)
        edges = thumb.filter(ImageFilter.FIND_EDGES)
        # FIND_EDGES lights up the frame itself; blank it so it cannot attract the window.
        ImageDraw.Draw(edges).rectangle((0, 0, edges.width - 1, edges.height - 1), outline=0, width=1)

        if w / h > ratio:
            # Source is wider than the target: slide horizontally.
            crop_w, crop_h = int(round(h * ratio)), h
            profile = list(edges.resize((edges.width, 1), Image.Resampling.BOX).tobytes())
            window = max(1, int(round(edges.width * crop_w / w)))
            start = _best_window(profile, window, centre=0.5, spread=0.30)
            left = int(round(start / len(profile) * w))
            left = max(0, min(left, w - crop_w))
            top = 0
        else:
            # Source is taller: slide vertically. Averaging by resizing the edge map
            # to a single pixel wide gives an exact per-row mean without numpy.
            crop_w, crop_h = w, int(round(w / ratio))
            profile = list(edges.resize((1, edges.height), Image.Resampling.BOX).tobytes())
            window = max(1, int(round(edges.height * crop_h / h)))
            start = _best_window(profile, window, centre=0.35, spread=0.30)
            top = int(round(start / len(profile) * h))
            top = max(0, min(top, h - crop_h))
            left = 0

        return img.crop((left, top, left + crop_w, top + crop_h))
    except Exception:
        # Never drop the photo over a measurement failure.
        LOG.exception("Smart crop failed; falling back to a centre crop")
        return _centre_crop(img, ratio)


def prepare_photo(raw: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGB")
        img = smart_crop(img, PHOTO_ASPECT)
        return img.resize((CONTENT_W, PHOTO_H), Image.Resampling.LANCZOS)
    except Exception:
        LOG.exception("Hero photo could not be prepared")
        return None


# ──────────────────────────────────────────────────────────────
# RENDER
# ──────────────────────────────────────────────────────────────
@dataclass
class _Brief:
    head_lines: list[str]
    body_lines: list[str]
    head_leading: int
    body_leading: int

    @property
    def height(self) -> int:
        return (
            len(self.head_lines) * self.head_leading
            + 10
            + len(self.body_lines) * self.body_leading
        )


def _rule(draw: ImageDraw.ImageDraw, y: int, width: int, colour=INK, x0=MARGIN, x1=PAGE_W - MARGIN) -> int:
    draw.rectangle((x0, y, x1 - 1, y + width - 1), fill=colour)
    return y + width


def _draw_lines(draw, x, y, lines, font, leading, fill=INK) -> int:
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += leading
    return y


def render_front_page(content: NewspaperContent) -> bytes:
    """Render the front page and return PNG bytes. Blocking — call it in a thread."""
    canvas = Image.new("RGB", (PAGE_W, MAX_CANVAS_H), PAPER)
    draw = ImageDraw.Draw(canvas)
    y = MARGIN

    # ── Masthead ──────────────────────────────────────────────
    logo = load_logo()
    logo_w = 0
    if logo is not None:
        target_h = 150
        scaled_w = max(1, int(round(logo.width * target_h / logo.height)))
        logo_img = logo.resize((scaled_w, target_h), Image.Resampling.LANCZOS)
        canvas.paste(logo_img, (MARGIN, y), logo_img)
        logo_w = scaled_w + 34

    name = sanitize(content.paper_name).upper()
    name_font = fit_tracked_size(
        name, CONTENT_W - logo_w, SZ_MASTHEAD_MAX, "Black", TRACK_MASTHEAD, floor=34
    )
    name_h = line_height(name_font, 1.0)
    text_x = MARGIN + logo_w
    draw_tracked(draw, text_x, y + 8, name, name_font, INK, TRACK_MASTHEAD)

    edition = sanitize(content.edition_line).upper()
    edition_font = face(SZ_EDITION, "Medium")
    draw_tracked(draw, text_x, y + 8 + name_h + 14, edition, edition_font, INK_SOFT, TRACK_LABEL)

    y = max(y + 150, y + 8 + name_h + 14 + line_height(edition_font, 1.0)) + 20

    # ── Thick rule ────────────────────────────────────────────
    y = _rule(draw, y, 7)
    y += 12

    # ── Dateline row ──────────────────────────────────────────
    date_font = face(SZ_DATELINE, "SemiBold")
    dateline = sanitize(content.dateline).upper()
    messages = sanitize(content.messages_line).upper()
    draw_tracked(draw, MARGIN, y, dateline, date_font, INK, TRACK_LABEL)
    right_w = tracked_width(messages, date_font, TRACK_LABEL)
    draw_tracked(draw, PAGE_W - MARGIN - right_w, y, messages, date_font, INK, TRACK_LABEL)
    y += line_height(date_font, 1.0) + 12
    y = _rule(draw, y, 2)
    y += 30

    # ── Hero photo, full width ────────────────────────────────
    photo = prepare_photo(content.photo_bytes) if content.photo_bytes else None
    content.photo_printed = photo is not None
    if photo is not None:
        canvas.paste(photo, (MARGIN, y))
        y += PHOTO_H + 12

        # Caption goes UNDER the photo. Reversing it out of a gradient on top means
        # fighting whatever happens to be behind it.
        label = sanitize(content.photo_label).upper()
        if label:
            label_font = face(SZ_CAPTION_LABEL, "Bold")
            draw_tracked(draw, MARGIN, y, label, label_font, INK, TRACK_LABEL)
            y += line_height(label_font, 1.0) + 7

        caption_font = face(SZ_CAPTION, "Regular", italic=True)
        caption_lines = wrap(content.photo_caption, caption_font, CONTENT_W)
        y = _draw_lines(draw, MARGIN, y, caption_lines, caption_font,
                        line_height(caption_font, 1.34), INK_SOFT)
        y += 30

    # ── Lead headline ─────────────────────────────────────────
    head_font = face(SZ_LEAD_HEAD, "Black")
    head_lines = wrap(content.lead_headline.upper(), head_font, CONTENT_W)
    if len(head_lines) > 3:
        # Very long headline: drop a step rather than eat a third of the page.
        head_font = face(SZ_LEAD_HEAD - 14, "Black")
        head_lines = wrap(content.lead_headline.upper(), head_font, CONTENT_W)
    y = _draw_lines(draw, MARGIN, y, head_lines, head_font, line_height(head_font, HEAD_LEADING))
    y += 26

    # ── Lead body, two columns ────────────────────────────────
    # A single column across the full measure is ~110 characters — bad typography
    # and taller than it needs to be.
    body_font = face(SZ_LEAD_BODY, "Regular")
    body_leading = line_height(body_font, LEADING)
    body_lines = wrap(content.lead_body, body_font, COL_W)
    split = math.ceil(len(body_lines) / 2)
    columns = (body_lines[:split], body_lines[split:])

    col_top = y
    col_bottom = y
    for idx, lines in enumerate(columns):
        end = _draw_lines(draw, COL_X[idx], col_top, lines, body_font, body_leading)
        col_bottom = max(col_bottom, end)
    if columns[1]:
        rule_x = MARGIN + COL_W + GUTTER // 2
        draw.rectangle((rule_x, col_top + 4, rule_x, col_bottom - 12), fill=HAIRLINE)
    y = col_bottom + 34

    # ── Tease ─────────────────────────────────────────────────
    tease = sanitize(content.tease)
    if tease:
        y = _rule(draw, y, 1, HAIRLINE)
        y += 16
        tease_font = face(SZ_TEASE, "Medium", italic=True)
        tease_leading = line_height(tease_font, 1.34)
        for line in wrap(tease, tease_font, CONTENT_W - 120):
            width = tease_font.getlength(line)
            draw.text(((PAGE_W - width) / 2, y), line, font=tease_font, fill=INK)
            y += tease_leading
        y += 16
        y = _rule(draw, y, 1, HAIRLINE)
        y += 34

    # ── ALSO FILED TODAY ──────────────────────────────────────
    briefs = [b for b in content.briefs if b[0] or b[1]][:BRIEF_COUNT]
    if briefs:
        section_font = face(SZ_SECTION, "Bold")
        draw_tracked(draw, MARGIN, y, "ALSO FILED TODAY", section_font, INK, TRACK_SECTION)
        y += line_height(section_font, 1.0) + 12
        y = _rule(draw, y, 5)
        y += 24

        bh_font = face(SZ_BRIEF_HEAD, "Bold")
        bb_font = face(SZ_BRIEF_BODY, "Regular")
        bh_leading = line_height(bh_font, 1.12)
        bb_leading = line_height(bb_font, LEADING)

        # Wrapped once here, then used for both balancing and drawing.
        laid_out = [
            _Brief(
                head_lines=wrap(head.upper(), bh_font, COL_W),
                body_lines=wrap(body, bb_font, COL_W),
                head_leading=bh_leading,
                body_leading=bb_leading,
            )
            for head, body in briefs
        ]

        gap = 30
        heights = [b.height + gap for b in laid_out]
        total = sum(heights)
        split_at, best_delta = len(laid_out), None
        running = 0
        for i in range(1, len(laid_out)):
            running += heights[i - 1]
            delta = abs(running - (total - running))
            if best_delta is None or delta < best_delta:
                split_at, best_delta = i, delta

        col_top = y
        col_bottom = y
        for idx, group in enumerate((laid_out[:split_at], laid_out[split_at:])):
            cy = col_top
            for brief in group:
                cy = _draw_lines(draw, COL_X[idx], cy, brief.head_lines, bh_font, bh_leading)
                cy += 10
                cy = _draw_lines(draw, COL_X[idx], cy, brief.body_lines, bb_font, bb_leading, INK_SOFT)
                cy += gap
            col_bottom = max(col_bottom, cy)

        if split_at < len(laid_out):
            rule_x = MARGIN + COL_W + GUTTER // 2
            draw.rectangle((rule_x, col_top + 4, rule_x, col_bottom - gap), fill=HAIRLINE)
        y = col_bottom - gap + 34

    # ── Colophon ──────────────────────────────────────────────
    colophon = sanitize(content.colophon).upper()
    if colophon:
        y = _rule(draw, y, 2)
        y += 14
        colo_font = face(SZ_COLOPHON, "Medium")
        width = tracked_width(colophon, colo_font, TRACK_LABEL)
        draw_tracked(draw, (PAGE_W - width) / 2, y, colophon, colo_font, INK_SOFT, TRACK_LABEL)
        y += line_height(colo_font, 1.0)

    # ── Crop to what was used ─────────────────────────────────
    page = canvas.crop((0, 0, PAGE_W, min(MAX_CANVAS_H, y + MARGIN)))

    # No optimize=True: it costs ~3s of the render for ~3% off the file, and no
    # paper grain either — it was invisible at viewing size and quadrupled the PNG.
    buffer = io.BytesIO()
    page.save(buffer, format="PNG")
    return buffer.getvalue()
