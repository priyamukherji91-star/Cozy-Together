#!/usr/bin/env python3
"""Report what is using the Railway volume, and clear out the junk.

    python scripts/volume_cleanup.py            # report + dry run, deletes nothing
    python scripts/volume_cleanup.py --apply    # actually delete what it listed

Run it inside the container, where the volume is mounted:

    railway ssh --service worker
    python scripts/volume_cleanup.py

**Dry run by default.** Nothing is deleted unless you pass --apply, and even
then only the four categories below — every file the bot actually reads is on a
protected list and is never a candidate, whatever else happens in here.

What it removes:

  *.tmp         Half-written files from `storage.save_json`. It writes to a
                temp file and renames it over the target, and unlinks the temp
                if the write failed — but a *full* volume is exactly the case
                where that unlink can fail too, so these are the one kind of
                junk a full disk actively produces.
  *.corrupt     Files quarantined by `storage.load_json` after they failed to
                parse. Kept deliberately so they can be recovered by hand;
                worth deleting once you have decided you won't.
  __pycache__/  Bytecode. Only ever appears on the volume if the mount covers
                the code, which is itself worth knowing — see the report.
  orphan photos PNGs in pets/ that no pet in pets.json points at. A registration
                whose index write failed leaves one behind by design: the image
                lands first, so a failure orphans a file rather than leaving an
                index entry pointing at nothing.

Anything modified in the last hour is left alone regardless, so a file being
written while this runs is never a candidate.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
PETS_JSON = DATA_DIR / "pets.json"
PETS_IMAGE_DIR = DATA_DIR / "pets"

# Files the bot reads. Never candidates, no matter what else this script decides.
# Listed by name rather than derived, so a rename in a cog can't silently make
# somebody's state deletable.
PROTECTED = frozenset({
    "pets.json",
    "pet_treats.json",
    "pet_panel.json",
    "birthdays.json",
    "birthday_state.json",
    "events_reminders.json",
    "ffxiv_resets.json",
    "member_cards.json",
    "morning_news_state.json",
    "avatar_state.json",
    "onboarding_config.json",
})

# A file being written right now must never be a candidate. Comfortably longer
# than any write the bot does, which are all sub-second.
GRACE_SECONDS = 3600


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def tree_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def recently_touched(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < GRACE_SECONDS
    except OSError:
        return True    # can't tell → treat as in use


def report_disk() -> None:
    print("── the volume ────────────────────────────────────────────")
    try:
        usage = shutil.disk_usage(DATA_DIR)
    except OSError as exc:
        print(f"  can't stat {DATA_DIR}: {exc}")
        return
    pct = usage.used / usage.total * 100 if usage.total else 0
    print(f"  mount holding {DATA_DIR}")
    print(f"  total {human(usage.total)} · used {human(usage.used)} "
          f"({pct:.0f}%) · free {human(usage.free)}")

    data = tree_size(DATA_DIR)
    share = data / usage.used * 100 if usage.used else 0
    print(f"  {DATA_DIR} itself: {human(data)} — {share:.1f}% of what's used")
    if share < 25 and usage.used > 100 * 1024 * 1024:
        print()
        print("  ⚠ The bot's data is a small share of a large amount of used space.")
        print("    Whatever is filling this is not the bot's state. The usual cause")
        print("    is the volume being mounted at /app rather than /app/data, which")
        print("    puts the code and every installed package on it. Check the mount")
        print("    path in Railway before buying more space.")
    print()


def report_contents() -> None:
    print("── what's in the data dir ────────────────────────────────")
    if not DATA_DIR.exists():
        print(f"  {DATA_DIR} does not exist")
        print()
        return
    rows = []
    for item in sorted(DATA_DIR.iterdir()):
        rows.append((tree_size(item), item.name + ("/" if item.is_dir() else "")))
    if not rows:
        print("  empty")
    for size, name in sorted(rows, reverse=True):
        print(f"  {human(size):>10}  {name}")
    print()


def load_referenced_images() -> set[str] | None:
    """Every filename pets.json points at, or None if it can't be trusted.

    None is the important case: if the index is missing or unreadable, *every*
    photo looks orphaned, and deleting on that basis would wipe the lot. The
    caller skips the whole category instead.
    """
    try:
        raw = json.loads(PETS_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("  ! pets.json not found — skipping the orphan check entirely.")
        return None
    except Exception as exc:
        print(f"  ! pets.json unreadable ({exc}) — skipping the orphan check.")
        return None

    if not isinstance(raw, dict):
        print("  ! pets.json is not the shape I expect — skipping the orphan check.")
        return None

    referenced: set[str] = set()
    for guild in raw.values():
        if not isinstance(guild, dict):
            continue
        for pet in guild.values():
            if isinstance(pet, dict) and pet.get("file"):
                referenced.add(str(pet["file"]))
    return referenced


def find_junk() -> list[tuple[str, Path, int]]:
    """(category, path, bytes) for everything safe to remove."""
    found: list[tuple[str, Path, int]] = []
    if not DATA_DIR.exists():
        return found

    for pattern, label in ((("*.tmp"), "tmp"), (("*.corrupt"), "corrupt")):
        for path in DATA_DIR.rglob(pattern):
            if not path.is_file() or path.name in PROTECTED:
                continue
            if recently_touched(path):
                continue
            found.append((label, path, tree_size(path)))

    for path in DATA_DIR.rglob("__pycache__"):
        if path.is_dir():
            found.append(("pycache", path, tree_size(path)))

    if PETS_IMAGE_DIR.is_dir():
        referenced = load_referenced_images()
        if referenced is not None:
            for path in PETS_IMAGE_DIR.iterdir():
                if not path.is_file() or path.suffix.lower() != ".png":
                    continue
                if path.name in referenced or recently_touched(path):
                    continue
                found.append(("orphan photo", path, tree_size(path)))

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually delete (default: dry run)"
    )
    args = parser.parse_args()

    print()
    report_disk()
    report_contents()

    print("── junk ──────────────────────────────────────────────────")
    junk = find_junk()
    if not junk:
        print("  Nothing to clean. The data dir is already tidy.")
        print()
        return 0

    total = sum(size for _, _, size in junk)
    for label, path, size in sorted(junk, key=lambda row: -row[2]):
        try:
            shown = path.relative_to(DATA_DIR)
        except ValueError:
            shown = path
        print(f"  {human(size):>10}  [{label}] {shown}")
    print(f"\n  {len(junk)} item(s), {human(total)} total")
    print()

    if not args.apply:
        print("  Dry run — nothing was deleted.")
        print("  Run again with --apply to remove the above.")
        print()
        return 0

    removed = freed = 0
    failed = 0
    for _label, path, size in junk:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
            freed += size
        except Exception as exc:
            failed += 1
            print(f"  could not remove {path}: {exc}")

    print(f"  Removed {removed} item(s), freed {human(freed)}.")
    if failed:
        print(f"  {failed} could not be removed — see above.")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
