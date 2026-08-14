# petcare/storage.py
# -*- coding: utf-8 -*-
"""Atomic JSON persistence and photo preparation for the pet system.

Two things in here are load-bearing:

1. Saves are atomic — write to a temp file, fsync, rename over the target. A
   plain open(path, "w") truncates before writing, so a restart landing
   mid-write loses the whole file. Railway restarts on deploy, so this matters.
2. Photos are prepared here and stored as BYTES ON DISK by pet_registry, never
   as Discord CDN URLs. Attachment links are signed and expire
   (?ex=...&is=...&hm=...), so a stored URL renders a broken image a day later.

The register itself lives in `pet_registry`, the treat ledger in
`cogs/pet_care.py`, and both go through `load_json` / `save_json` here rather
than touching the filesystem themselves.
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Union

from PIL import Image, ImageFilter

LOG = logging.getLogger(__name__)

PathLike = Union[str, Path]

# Railway Volume Storage (matches birthday.py / member_cards.py convention).
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))

IMAGE_SIZE = 256                      # stored square size, in pixels

# Crop analysis. CROP_FOCUS is where the subject usually sits vertically:
# 0.0 is the top edge, 1.0 the bottom. Phone photos of animals put the head
# above the middle, hence 0.42.
CROP_ANALYSIS_SIZE = 160
CROP_FOCUS = 0.42
CROP_FOCUS_SPREAD = 0.30


class PetError(Exception):
    """Anything the member should be told about in plain words.

    Defined here rather than in `pet_registry` because `prepare_image` raises it
    and the registry imports this module, not the other way round.
    """


# ──────────────────────────────────────────────────────────────
# Atomic JSON
# ──────────────────────────────────────────────────────────────
def load_json(path: PathLike, default: Any = None) -> Any:
    """Read JSON from `path`, returning `default` if it's missing or unreadable.

    A file that exists but won't parse is set aside as `<name>.corrupt` before
    the default is returned — otherwise the next save would overwrite the only
    copy of the damaged data and take any chance of hand-recovery with it.
    """
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        LOG.exception("Corrupt JSON in %s — falling back to default", p)
        _quarantine(p)
        return default
    except Exception:
        LOG.exception("Failed reading %s — falling back to default", p)
        return default


def save_json(path: PathLike, data: Any) -> bool:
    """Atomically write `data` to `path` as JSON. Returns True on success.

    Never raises: a cog that can't persist should keep serving from memory
    rather than take the bot down with it. Failures are logged.
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)

        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            # Push it out of the OS buffer, so the rename below can't beat the
            # data to disk if the container dies right after.
            os.fsync(f.fileno())

        tmp.replace(p)  # atomic
        return True
    except Exception:
        LOG.exception("Failed writing %s", p)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _quarantine(p: Path) -> None:
    """Move an unparseable file aside so it isn't silently overwritten."""
    try:
        p.replace(p.with_suffix(p.suffix + ".corrupt"))
        LOG.warning("Preserved unreadable %s as %s.corrupt", p, p.name)
    except Exception:
        LOG.exception("Could not quarantine %s", p)


# ──────────────────────────────────────────────────────────────
# Images
# ──────────────────────────────────────────────────────────────
def _band_energy(img: Image.Image, vertical: bool) -> list[float]:
    """Detail per row (or column), from an edge-detected thumbnail.

    Squashing the edge map to one pixel wide averages each row exactly, which is
    far quicker than walking pixels and needs no numpy.
    """
    small = img.convert("L")
    small.thumbnail((CROP_ANALYSIS_SIZE, CROP_ANALYSIS_SIZE), Image.BILINEAR)
    edges = small.filter(ImageFilter.FIND_EDGES)
    aw, ah = edges.size
    strip = edges.resize((1, ah) if vertical else (aw, 1), Image.BILINEAR)
    return [float(v) for v in strip.getdata()]


def _centre_crop_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def smart_crop_square(img: Image.Image) -> Image.Image:
    """Crop to a square keeping the busiest part of the picture.

    A centre crop cuts the head off anything shot in portrait. This slides the
    crop window along the edge-detail profile and keeps the position holding the
    most detail, leaning slightly upward to break ties the way faces sit.
    """
    w, h = img.size
    if w <= 0 or h <= 0 or w == h:
        return img

    vertical = h > w
    window, extent = (w, h) if vertical else (h, w)
    if window <= 0 or window >= extent:
        return img

    try:
        profile = _band_energy(img, vertical)
    except Exception:
        LOG.warning("Could not measure photo detail; centre-cropping", exc_info=True)
        return _centre_crop_square(img)

    n = len(profile)
    if n < 2:
        return _centre_crop_square(img)

    span = max(1, min(n, round(window * n / extent)))
    prefix = [0.0]
    for value in profile:
        prefix.append(prefix[-1] + value)

    best_index, best_score = 0, -1.0
    for index in range(n - span + 1):
        detail = prefix[index + span] - prefix[index]
        centre = (index + span / 2) / n
        # Gaussian weight toward CROP_FOCUS, without importing math.exp on a hot
        # path — a plain quadratic falloff is close enough and cheaper.
        offset = (centre - CROP_FOCUS) / CROP_FOCUS_SPREAD
        weight = 1.0 / (1.0 + offset * offset)
        score = detail * weight
        if score > best_score:
            best_score, best_index = score, index

    offset_px = round(best_index * extent / n)
    offset_px = max(0, min(offset_px, extent - window))
    if vertical:
        return img.crop((0, offset_px, w, offset_px + window))
    return img.crop((offset_px, 0, offset_px + window, h))


def prepare_image(raw: bytes) -> bytes:
    """Crop square, downscale, re-encode as PNG.

    Re-encoding strips whatever metadata the phone attached, and makes a corrupt
    or hostile file fail here rather than somewhere less convenient.
    """
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:
        raise PetError("That file could not be read as an image. Use a PNG or a JPEG.") from exc

    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    img = smart_crop_square(img)
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)

    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
