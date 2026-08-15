# petcare/storage.py
# -*- coding: utf-8 -*-
"""Photo preparation for the pet system, and its door to shared storage.

Photos are prepared here and stored as BYTES ON DISK by pet_registry, never as
Discord CDN URLs. Attachment links are signed and expire
(?ex=...&is=...&hm=...), so a stored URL renders a broken image a day later.

The JSON persistence that used to live here now lives in `common.storage`,
which every cog shares — it was worth having once, and worth having everywhere.
`DATA_DIR`, `load_json` and `save_json` are re-exported below so the pet modules
can keep importing them from here.

The register itself lives in `pet_registry` and the treat ledger in
`cogs/pet_care.py`; both go through `load_json` / `save_json` rather than
touching the filesystem themselves.
"""
from __future__ import annotations

import io
import logging

from PIL import Image, ImageFilter

from common.storage import DATA_DIR, load_json, save_json  # re-exported

LOG = logging.getLogger(__name__)

__all__ = [
    "DATA_DIR",
    "IMAGE_SIZE",
    "PetError",
    "load_json",
    "prepare_image",
    "save_json",
    "smart_crop_square",
]

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
