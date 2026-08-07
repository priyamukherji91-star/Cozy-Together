# petcare/storage.py
# -*- coding: utf-8 -*-
"""Storage for the pet system: the pet register and the treat ledger.

Depends only on Pillow. Two things in here are load-bearing:

1. Photos are stored as BYTES ON DISK, never as Discord CDN URLs. Attachment
   links are signed and expire (?ex=...&is=...&hm=...), so a stored URL renders
   a broken image a day later.
2. Saves are atomic — write to a temp file, fsync, rename over the target. A
   plain open(path, "w") truncates before writing, so a restart landing
   mid-write loses the whole file. Railway restarts on deploy, so this matters.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter

LOG = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
# Railway Volume Storage (matches birthday.py / member_cards.py convention).
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))

PETS_PATH = DATA_DIR / "pets.json"
PETS_IMAGE_DIR = DATA_DIR / "pets"
TREATS_PATH = DATA_DIR / "pet_treats.json"

MAX_PETS_PER_OWNER = 6
MAX_NAME_LENGTH = 24
IMAGE_SIZE = 256                      # stored square size, in pixels
MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # refuse before decoding

DAILY_TREATS = 6

# Crop analysis. CROP_FOCUS is where the subject usually sits vertically:
# 0.0 is the top edge, 1.0 the bottom. Phone photos of animals put the head
# above the middle, hence 0.42.
CROP_ANALYSIS_SIZE = 160
CROP_FOCUS = 0.42
CROP_FOCUS_SPREAD = 0.30


class PetError(Exception):
    """Anything the member should be told about in plain words."""


@dataclass(frozen=True)
class Pet:
    pet_id: str
    guild_id: int
    owner_id: int
    name: str
    filename: str
    added: str

    @property
    def path(self) -> Path:
        return PETS_IMAGE_DIR / self.filename


@dataclass(frozen=True)
class FeedResult:
    total: int             # the pet's lifetime treats
    treats_left: int       # the feeder's remaining allowance today
    from_you: int          # how many of that pet's treats came from this feeder
    new_favourite: bool    # did this feed take the title


# ──────────────────────────────────────────────────────────────
# Atomic JSON
# ──────────────────────────────────────────────────────────────
def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        LOG.exception("Corrupt JSON in %s — preserving it as .corrupt", path)
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            LOG.exception("Could not quarantine %s", path)
        return default
    except Exception:
        LOG.exception("Failed reading %s", path)
        return default


def save_json(path: Path, data: Any) -> bool:
    """Write atomically. Returns False on failure rather than raising."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)  # atomic
        return True
    except Exception:
        LOG.exception("Failed writing %s", path)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# Both registers are read-modify-write and the cog calls them from worker
# threads, so concurrent writes need guarding or one of them is silently lost.
_LOCK = threading.Lock()


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


def image_bytes(pet: Pet) -> bytes | None:
    """The stored photo, or None if it has gone missing. Never raises."""
    try:
        return pet.path.read_bytes()
    except FileNotFoundError:
        LOG.warning("Pet %s (%s) has no image at %s", pet.name, pet.pet_id, pet.path)
    except Exception:
        LOG.exception("Could not read pet image %s", pet.path)
    return None


# ──────────────────────────────────────────────────────────────
# The pet register
# ──────────────────────────────────────────────────────────────
def _load_pets() -> dict[str, dict[str, Any]]:
    data = load_json(PETS_PATH, {})
    return data if isinstance(data, dict) else {}


def _to_pet(guild_id: int, pet_id: str, raw: dict[str, Any]) -> Pet | None:
    try:
        return Pet(
            pet_id=pet_id,
            guild_id=guild_id,
            owner_id=int(raw["owner_id"]),
            name=str(raw["name"]),
            filename=str(raw["file"]),
            added=str(raw.get("added", "")),
        )
    except Exception:
        LOG.warning("Skipping malformed pet record %s in guild %s", pet_id, guild_id)
        return None


def all_pets(guild_id: int) -> list[Pet]:
    guild = _load_pets().get(str(guild_id), {})
    pets = [p for pid, raw in guild.items() if (p := _to_pet(guild_id, pid, raw))]
    return sorted(pets, key=lambda p: p.name.lower())


def pets_of(guild_id: int, owner_id: int) -> list[Pet]:
    return [p for p in all_pets(guild_id) if p.owner_id == owner_id]


def get_pet(guild_id: int, pet_id: str) -> Pet | None:
    raw = _load_pets().get(str(guild_id), {}).get(pet_id)
    return _to_pet(guild_id, pet_id, raw) if raw else None


def find_by_name(guild_id: int, name: str, owner_id: int | None = None) -> Pet | None:
    wanted = name.strip().casefold()
    for pet in all_pets(guild_id):
        if pet.name.casefold() != wanted:
            continue
        if owner_id is None or pet.owner_id == owner_id:
            return pet
    return None


def add_pet(guild_id: int, owner_id: int, name: str, raw_image: bytes) -> Pet:
    with _LOCK:
        return _add_pet_locked(guild_id, owner_id, name, raw_image)


def _add_pet_locked(guild_id: int, owner_id: int, name: str, raw_image: bytes) -> Pet:
    name = " ".join(name.split())
    if not name:
        raise PetError("Enter a name for the pet.")
    if len(name) > MAX_NAME_LENGTH:
        raise PetError(f"That name is too long — keep it to {MAX_NAME_LENGTH} characters or fewer.")
    if len(raw_image) > MAX_UPLOAD_BYTES:
        raise PetError("That photo is too big. Use something under 10 MB.")
    if find_by_name(guild_id, name, owner_id) is not None:
        raise PetError(f"You already have a pet called **{name}**. Pick another name.")

    data = _load_pets()
    guild = data.setdefault(str(guild_id), {})

    owned = sum(1 for raw in guild.values() if int(raw.get("owner_id", 0)) == owner_id)
    if owned >= MAX_PETS_PER_OWNER:
        raise PetError(
            f"You have registered {MAX_PETS_PER_OWNER} pets, which is the limit. "
            "Use `/pet remove` to make room."
        )

    image = prepare_image(raw_image)

    pet_id = uuid.uuid4().hex[:12]
    filename = f"{guild_id}_{pet_id}.png"
    try:
        PETS_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (PETS_IMAGE_DIR / filename).write_bytes(image)
    except Exception as exc:
        LOG.exception("Could not write pet image %s", filename)
        raise PetError("The photo could not be saved. Try again in a moment.") from exc

    guild[pet_id] = {
        "owner_id": owner_id,
        "name": name,
        "file": filename,
        "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # Image lands before the index, so a failed save leaves an orphan file
    # rather than an index entry pointing at nothing.
    if not save_json(PETS_PATH, data):
        try:
            (PETS_IMAGE_DIR / filename).unlink(missing_ok=True)
        except Exception:
            LOG.exception("Could not clean up %s after a failed save", filename)
        raise PetError("That could not be saved. Try again in a moment.")

    return Pet(pet_id, guild_id, owner_id, name, filename, guild[pet_id]["added"])


def remove_pet(guild_id: int, pet_id: str) -> Pet | None:
    with _LOCK:
        data = _load_pets()
        guild = data.get(str(guild_id), {})
        raw = guild.pop(pet_id, None)
        if raw is None:
            return None

        pet = _to_pet(guild_id, pet_id, raw)
        if not save_json(PETS_PATH, data):
            raise PetError("The register could not be updated. Try again in a moment.")

        if pet is not None:
            try:
                pet.path.unlink(missing_ok=True)
            except Exception:
                LOG.exception("Could not delete pet image %s", pet.path)
        return pet


# ──────────────────────────────────────────────────────────────
# The treat ledger
# ──────────────────────────────────────────────────────────────
# The daily allowance resets by comparing a stored date whenever the ledger is
# read. No scheduler, so a restart cannot miss a rollover.

def _today(tz) -> str:
    return datetime.now(tz).date().isoformat()


def _load_treats() -> dict[str, Any]:
    data = load_json(TREATS_PATH, {})
    return data if isinstance(data, dict) else {}


def _guild_block(data: dict[str, Any], guild_id: int) -> dict[str, Any]:
    block = data.setdefault(str(guild_id), {})
    block.setdefault("pets", {})
    block.setdefault("feeders", {})
    return block


def _feeder_today(block: dict[str, Any], user_id: int, tz) -> dict[str, Any]:
    rec = block["feeders"].setdefault(str(user_id), {})
    if rec.get("date") != _today(tz):
        rec["date"] = _today(tz)
        rec["spent"] = 0
        rec["fed"] = []
    rec.setdefault("lifetime", 0)
    return rec


def favourite_id(by: dict[str, Any]) -> str | None:
    """Whoever has fed a pet most, or None if nobody leads outright.

    A tie deliberately has no favourite — a title that flips on every feed is
    noise, and a joint title is not worth reading.
    """
    if not by:
        return None
    ranked = sorted(by.items(), key=lambda kv: (-int(kv[1]), kv[0]))
    if len(ranked) > 1 and int(ranked[0][1]) == int(ranked[1][1]):
        return None
    return ranked[0][0]


def treats_left(guild_id: int, user_id: int, tz) -> tuple[int, list[str]]:
    """Remaining allowance, and the pet ids already fed today."""
    block = _guild_block(_load_treats(), guild_id)
    rec = _feeder_today(block, user_id, tz)
    return max(0, DAILY_TREATS - int(rec.get("spent", 0))), list(rec.get("fed", []))


def feed(guild_id: int, user_id: int, pet_id: str, tz) -> FeedResult:
    with _LOCK:
        data = _load_treats()
        block = _guild_block(data, guild_id)
        rec = _feeder_today(block, user_id, tz)

        spent = int(rec.get("spent", 0))
        if spent >= DAILY_TREATS:
            raise PetError(
                f"You have used all {DAILY_TREATS} of your treats for today. "
                "The allowance resets at midnight."
            )
        if pet_id in rec.get("fed", []):
            raise PetError("You have already fed this pet today. One treat per pet per day.")

        stats = block["pets"].setdefault(pet_id, {"total": 0, "by": {}, "last_fed": None})
        by: dict[str, Any] = stats.setdefault("by", {})
        previous_favourite = favourite_id(by)

        stats["total"] = int(stats.get("total", 0)) + 1
        by[str(user_id)] = int(by.get(str(user_id), 0)) + 1
        stats["last_fed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        rec["spent"] = spent + 1
        rec.setdefault("fed", []).append(pet_id)
        rec["lifetime"] = int(rec.get("lifetime", 0)) + 1

        if not save_json(TREATS_PATH, data):
            raise PetError("That could not be recorded. Try again in a moment.")

        now_favourite = favourite_id(by)
        return FeedResult(
            total=stats["total"],
            treats_left=DAILY_TREATS - rec["spent"],
            from_you=by[str(user_id)],
            new_favourite=now_favourite == str(user_id) and previous_favourite != now_favourite,
        )


def pet_stats(guild_id: int, pet_id: str) -> dict[str, Any]:
    block = _guild_block(_load_treats(), guild_id)
    return block["pets"].get(pet_id, {"total": 0, "by": {}, "last_fed": None})


def top_pets(guild_id: int, limit: int = 10) -> list[tuple[str, int, str | None]]:
    """(pet_id, treats, favourite_user_id), best first.

    The favourite comes back with the row so rendering the board reads the
    ledger once instead of once per pet.
    """
    block = _guild_block(_load_treats(), guild_id)
    rows = [
        (pid, int(s.get("total", 0)), favourite_id(s.get("by", {})))
        for pid, s in block["pets"].items()
    ]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return [r for r in rows if r[1] > 0][:limit]


def top_feeders(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    block = _guild_block(_load_treats(), guild_id)
    rows = [(int(uid), int(r.get("lifetime", 0))) for uid, r in block["feeders"].items()]
    rows.sort(key=lambda kv: (-kv[1], kv[0]))
    return [r for r in rows if r[1] > 0][:limit]


def days_since_fed(guild_id: int, pet: Pet) -> int | None:
    """Whole days since a pet last ate, counting from registration if never fed."""
    stamp = pet_stats(guild_id, pet.pet_id).get("last_fed") or pet.added
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - when).days)
