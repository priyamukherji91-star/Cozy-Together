# petcare/pet_registry.py
# -*- coding: utf-8 -*-
"""The pet register: who owns what, what it looks like, and its profile.

Used by `cogs/pet_care.py` (the panel) and by the ship cards in
`cogs/shipping.py`. The treat ledger is *not* in here — that lives in
`cogs/pet_care.py`, so the register stays about what a pet is rather than how
often it eats.

Lives in `petcare/` rather than beside `bot.py` because bot.py imports every
top-level `*.py` looking for a `setup(bot)`, and this has none.

Photos are stored as bytes on disk, not as Discord CDN URLs. Attachment links
are signed and expire (`?ex=…&is=…&hm=…`), so a stored URL renders a broken pet
a day later. Each photo is cropped square and downscaled to 256px on the way in,
which is the size the ship cards draw avatars at, and lands around 60 KB.
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from petcare import storage
from petcare.storage import PetError, prepare_image  # re-exported for callers

LOG = logging.getLogger(__name__)

# The Railway volume. Everything the pet system writes goes under here.
DATA_DIR = storage.DATA_DIR
PETS_PATH = DATA_DIR / "pets.json"
PETS_IMAGE_DIR = DATA_DIR / "pets"

MAX_PETS_PER_OWNER = 6
MAX_NAME_LENGTH = 24
IMAGE_SIZE = storage.IMAGE_SIZE

# The photo grid a multi-pet feed posts — see `render_grid`. Five across, so a
# whole day's allowance spent at once is two readable rows rather than one long
# strip nobody can make out on a phone.
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
GRID_COLUMNS = 5
GRID_GAP = 14
GRID_CORNER_RADIUS = 20
GRID_FONT_SIZE = 32
GRID_LABEL_INSET = 12
GRID_SCRIM_SHARE = 0.30      # how much of the tile the name sits over
GRID_SCRIM_ALPHA = 205

# Only a decode guard, not a quality one. The photo is cropped square and
# downscaled to IMAGE_SIZE immediately — around 60 KB on disk — so nothing here
# cares how big it arrived. The old 10 MB ceiling was useless twice over:
# Discord enforces its own limit before the bot ever sees the file, so the check
# only ever fired on uploads Discord had already accepted, and on a boosted
# server it refused perfectly good phone photos and blamed the member for them.
# A modern camera clears 10 MB without trying.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

# One message for every caller, derived from the cap. The three copies of this
# had already drifted from the constant once.
TOO_BIG = (
    f"That photo is too big. Something under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
)

# The optional profile. Every one of these defaults to empty, so the pets
# registered before profiles existed load exactly as they did — a blank field
# simply doesn't render. Caps are generous but finite; a bio is not a novel.
PROFILE_FIELDS: dict[str, int] = {
    "species": 32,
    "born": 12,
    "treat": 32,
    "toy": 32,
    "known_for": 300,
    "traits": 120,
}


@dataclass(frozen=True)
class Pet:
    pet_id: str
    guild_id: int
    owner_id: int
    name: str
    filename: str
    added: str
    species: str = ""
    born: str = ""
    treat: str = ""
    toy: str = ""
    known_for: str = ""
    traits: str = ""

    @property
    def path(self) -> Path:
        return PETS_IMAGE_DIR / self.filename

    @property
    def has_profile(self) -> bool:
        return any(getattr(self, key) for key in PROFILE_FIELDS)


# add_pet and remove_pet read the index, change it, and write it back. The cog
# runs them on worker threads, so two registrations landing together would
# otherwise race and drop one.
_WRITE_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────
# Read cache. Rendering one board asked for thirty pets one at a time, and every
# one of those was a fresh read of this whole file off Railway's volume.
#
# Keyed on the file's (mtime, size) *and* a counter this process bumps on every
# save: the stat catches a write from anywhere else, and the counter covers the
# case where two writes land inside one filesystem timestamp tick.
#
# It holds the JSON *text* rather than the parsed object, so every caller still
# gets its own structure to do as it likes with. Several of them mutate what
# they get back — `_add_pet_locked` builds the new state in place before saving —
# and handing them all one shared dict would let an unsaved edit, or a failed
# save, show up in somebody else's read.
_CACHE_LOCK = threading.Lock()
_cache_key: tuple[Any, ...] | None = None
_cache_text: str = ""
_writes = 0


def _cache_stamp() -> tuple[Any, ...]:
    try:
        st = PETS_PATH.stat()
        return (st.st_mtime_ns, st.st_size, _writes)
    except OSError:
        return ("missing", _writes)


def _load() -> dict[str, dict[str, Any]]:
    global _cache_key, _cache_text

    stamp = _cache_stamp()
    with _CACHE_LOCK:
        cached = _cache_text if (_cache_key == stamp and _cache_text) else ""

    if cached:
        try:
            data = json.loads(cached)
            return data if isinstance(data, dict) else {}
        except ValueError:      # can't happen — it was dumped from a dict — but
            pass                # a stale cache must never be worse than no cache

    data = storage.load_json(PETS_PATH, default={})
    if not isinstance(data, dict):
        data = {}

    try:
        text = json.dumps(data)
    except (TypeError, ValueError):
        text = ""               # unserialisable: skip the cache, keep working
    with _CACHE_LOCK:
        _cache_key, _cache_text = stamp, text

    return data


def _save(data: dict[str, dict[str, Any]]) -> bool:
    global _writes
    ok = storage.save_json(PETS_PATH, data)
    # Bumped whether or not the write succeeded: a failed save may still have
    # touched the file, and the next read must not trust what it has.
    _writes += 1
    return ok


def _to_pet(guild_id: int, pet_id: str, raw: dict[str, Any]) -> Pet | None:
    try:
        return Pet(
            pet_id=pet_id,
            guild_id=guild_id,
            owner_id=int(raw["owner_id"]),
            name=str(raw["name"]),
            filename=str(raw["file"]),
            added=str(raw.get("added", "")),
            **{key: str(raw.get(key, "") or "") for key in PROFILE_FIELDS},
        )
    except Exception:
        LOG.warning("Skipping malformed pet record %s in guild %s", pet_id, guild_id)
        return None


def all_pets(guild_id: int) -> list[Pet]:
    """Every registered pet in the guild, sorted by name."""
    guild = _load().get(str(guild_id), {})
    pets = [p for pid, raw in guild.items() if (p := _to_pet(guild_id, pid, raw))]
    return sorted(pets, key=lambda p: p.name.lower())


def pets_of(guild_id: int, owner_id: int) -> list[Pet]:
    return [p for p in all_pets(guild_id) if p.owner_id == owner_id]


def get_pet(guild_id: int, pet_id: str) -> Pet | None:
    raw = _load().get(str(guild_id), {}).get(pet_id)
    return _to_pet(guild_id, pet_id, raw) if raw else None


def find_by_name(guild_id: int, name: str, owner_id: int | None = None) -> Pet | None:
    """Case-insensitive name lookup, optionally narrowed to one owner."""
    wanted = name.strip().casefold()
    for pet in all_pets(guild_id):
        if pet.name.casefold() != wanted:
            continue
        if owner_id is None or pet.owner_id == owner_id:
            return pet
    return None


# ──────────────────────────────────────────────────────────────
# Images
# ──────────────────────────────────────────────────────────────
def image_bytes(pet: Pet) -> bytes | None:
    """The stored photo, or None if it has gone missing. Never raises."""
    try:
        return pet.path.read_bytes()
    except FileNotFoundError:
        LOG.warning("Pet %s (%s) has no image at %s", pet.name, pet.pet_id, pet.path)
    except Exception:
        LOG.exception("Could not read pet image %s", pet.path)
    return None


def render_grid(pets: list[Pet]) -> bytes | None:
    """Several pets' photos drawn into one picture, names burnt onto them.

    A Discord embed carries one image, so a feed covering several pets can only
    show several faces by pasting them together. The photos are already square
    and IMAGE_SIZE wide on disk, so this is a paste rather than a resize.

    The name goes *on* the photo behind a dark scrim rather than underneath it
    on a background. The picture sits on Discord's embed, which is near-white
    for some members and near-black for others, so any background or text
    colour chosen here would be wrong for half the server. White on a scrim
    reads on both, and the surround stays transparent.

    A pet whose photo has gone missing is skipped and the grid closes up around
    it. Returns None when that leaves nothing to draw.
    """
    tiles: list[tuple[Image.Image, str]] = []
    for pet in pets:
        raw = image_bytes(pet)
        if raw is None:
            continue
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
            img = img.convert("RGBA")
        except Exception:
            LOG.warning("Could not decode %s for the grid", pet.path, exc_info=True)
            continue
        if img.size != (IMAGE_SIZE, IMAGE_SIZE):
            img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
        tiles.append((img, pet.name))

    if not tiles:
        return None

    cols = min(GRID_COLUMNS, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    width = cols * IMAGE_SIZE + (cols + 1) * GRID_GAP
    height = rows * IMAGE_SIZE + (rows + 1) * GRID_GAP

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    font = _grid_font()

    for index, (img, name) in enumerate(tiles):
        # Label first, round second: compositing the scrim over an already
        # rounded tile would fill the bottom corners back in.
        _label_tile(img, name, font)
        tile = _round_corners(img)
        row, col = divmod(index, cols)
        canvas.paste(
            tile,
            (GRID_GAP + col * (IMAGE_SIZE + GRID_GAP),
             GRID_GAP + row * (IMAGE_SIZE + GRID_GAP)),
            tile,
        )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _grid_font() -> Any:
    try:
        return ImageFont.truetype(str(FONT_DIR / "NotoSans-Bold.ttf"), GRID_FONT_SIZE)
    except Exception:
        LOG.warning("Grid font missing; falling back to Pillow's default", exc_info=True)
        return ImageFont.load_default()


def _fit(text: str, font: Any, draw: "ImageDraw.ImageDraw", room: int) -> str:
    """Trim a name until it fits the tile, rather than letting it run off it."""
    if draw.textlength(text, font=font) <= room:
        return text
    while text and draw.textlength(text + "…", font=font) > room:
        text = text[:-1]
    return (text + "…") if text else ""


def _label_tile(tile: Image.Image, name: str, font: Any) -> None:
    """Darken the bottom of the photo and write the pet's name across it."""
    width, height = tile.size
    band = int(height * GRID_SCRIM_SHARE)

    # A one-pixel column resized to width is an exact horizontal gradient and
    # costs nothing next to walking every pixel.
    column = Image.new("L", (1, band))
    for y in range(band):
        column.putpixel((0, y), int(GRID_SCRIM_ALPHA * (y / max(1, band - 1)) ** 1.6))
    scrim = Image.new("RGBA", (width, band), (0, 0, 0, 255))
    scrim.putalpha(column.resize((width, band), Image.BILINEAR))
    tile.alpha_composite(scrim, (0, height - band))

    draw = ImageDraw.Draw(tile)
    label = _fit(" ".join(name.split()), font, draw, width - 2 * GRID_LABEL_INSET)
    if not label:
        return
    text_width = draw.textlength(label, font=font)
    draw.text(
        ((width - text_width) / 2, height - band + (band - GRID_FONT_SIZE) / 2 - 2),
        label,
        font=font,
        fill=(255, 255, 255, 255),
    )


def _round_corners(img: Image.Image, radius: int = GRID_CORNER_RADIUS) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255
    )
    out = img.copy()
    out.putalpha(mask)
    return out


# ──────────────────────────────────────────────────────────────
# Mutations
# ──────────────────────────────────────────────────────────────
def add_pet(guild_id: int, owner_id: int, name: str, raw_image: bytes) -> Pet:
    """Register a pet. Raises PetError with a member-facing message."""
    with _WRITE_LOCK:
        return _add_pet_locked(guild_id, owner_id, name, raw_image)


def _add_pet_locked(guild_id: int, owner_id: int, name: str, raw_image: bytes) -> Pet:
    name = " ".join(name.split())
    if not name:
        raise PetError("Give them a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise PetError(f"That name is too long — keep it under {MAX_NAME_LENGTH} characters.")
    if len(raw_image) > MAX_UPLOAD_BYTES:
        raise PetError(TOO_BIG)

    if find_by_name(guild_id, name, owner_id) is not None:
        raise PetError(f"You already have a **{name}**. Remove them first, or pick another name.")

    data = _load()
    guild = data.setdefault(str(guild_id), {})

    owned = sum(1 for raw in guild.values() if int(raw.get("owner_id", 0)) == owner_id)
    if owned >= MAX_PETS_PER_OWNER:
        raise PetError(
            f"You've registered {MAX_PETS_PER_OWNER} already — that's the limit. "
            "Remove one from 🐾 Manage my pets to make room."
        )

    image = prepare_image(raw_image)

    pet_id = uuid.uuid4().hex[:12]
    filename = f"{guild_id}_{pet_id}.png"
    try:
        PETS_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (PETS_IMAGE_DIR / filename).write_bytes(image)
    except Exception as exc:
        LOG.exception("Could not write pet image %s", filename)
        raise PetError("I couldn't save that photo. Try again in a moment.") from exc

    guild[pet_id] = {
        "owner_id": owner_id,
        "name": name,
        "file": filename,
        "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # The image is on disk before the index is written, so a failed save leaves
    # an orphan file rather than an index entry pointing at nothing.
    if not _save(data):
        try:
            (PETS_IMAGE_DIR / filename).unlink(missing_ok=True)
        except Exception:
            LOG.exception("Could not clean up %s after a failed save", filename)
        raise PetError("I couldn't save that. Try again in a moment.")

    return Pet(pet_id, guild_id, owner_id, name, filename, guild[pet_id]["added"])


_RUNS_OF_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def _tidy(text: str) -> str:
    """Tidy whitespace without flattening the text.

    Runs of spaces collapse and blank lines close up, but newlines survive —
    "What are they like?" is a paragraph box, and laying it out over a few lines
    is the obvious thing to do.
    """
    text = _RUNS_OF_SPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def save_profile(
    guild_id: int, pet_id: str, values: dict[str, str], *, new_name: str | None = None
) -> Pet:
    """Write whichever profile fields were supplied, and optionally rename.

    Only keys in PROFILE_FIELDS are written, so a caller cannot smuggle in
    anything else. Fields absent from `values` are left as they were — the
    profile is collected in more than one place, and saving one form must not
    wipe what another wrote. Clearing a field is done by submitting it empty,
    which is why the blank string is stored rather than skipped.
    """
    with _WRITE_LOCK:
        data = _load()
        guild = data.setdefault(str(guild_id), {})
        raw = guild.get(pet_id)
        if raw is None:
            raise PetError("That pet isn't registered any more.")

        if new_name is not None:
            name = " ".join(new_name.split())
            if not name:
                raise PetError("Give them a name.")
            if len(name) > MAX_NAME_LENGTH:
                raise PetError(
                    f"That name is too long — keep it under {MAX_NAME_LENGTH} characters."
                )
            clash = find_by_name(guild_id, name, int(raw.get("owner_id", 0)))
            if clash is not None and clash.pet_id != pet_id:
                raise PetError(f"You already have a **{name}**. Pick another name.")
            raw["name"] = name

        for key, cap in PROFILE_FIELDS.items():
            if key in values:
                raw[key] = _tidy(str(values[key]))[:cap]

        if not _save(data):
            raise PetError("I couldn't save that. Try again in a moment.")

        pet = _to_pet(guild_id, pet_id, raw)
        if pet is None:
            raise PetError("I couldn't read that back. Try again in a moment.")
        return pet


def remove_pet(guild_id: int, pet_id: str) -> Pet | None:
    with _WRITE_LOCK:
        return _remove_pet_locked(guild_id, pet_id)


def _remove_pet_locked(guild_id: int, pet_id: str) -> Pet | None:
    data = _load()
    guild = data.get(str(guild_id), {})
    raw = guild.pop(pet_id, None)
    if raw is None:
        return None

    pet = _to_pet(guild_id, pet_id, raw)
    if not _save(data):
        raise PetError("I couldn't update the register. Try again in a moment.")

    if pet is not None:
        try:
            pet.path.unlink(missing_ok=True)
        except Exception:
            LOG.exception("Could not delete pet image %s", pet.path)
    return pet
