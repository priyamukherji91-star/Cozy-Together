# common/lines.py
# -*- coding: utf-8 -*-
"""Mittens' voice, editable from the panel instead of from a commit.

Every pool of lines the bot picks from at random — his Discord statuses, the
reset announcements, the timeout quips, the pet chatter, the wall-of-shame
taunts — registers itself here with a stable key and reads back through
`pool()` or `pick()`. If somebody has edited that pool from the staff panel,
the edit is what comes back; otherwise the code's own default does.

**The defaults stay in the code.** `lines.json` on the volume holds *only*
overrides, and that shape is doing three jobs at once: a deploy cannot wipe an
edit, deleting an override restores the original rather than leaving a hole,
and a corrupt or missing file falls back to a bot that still speaks rather than
one posting empty strings. It also means a pool nobody has touched keeps
improving when the code does, instead of being frozen the day this shipped.

Lives in `common/` rather than at the repo root because `bot.py` executes every
top-level `*.py` looking for a `setup(bot)`, which would run this module's body
a second time under a different name. `common/` is explicitly not an extension
package, for exactly this reason.

**Placeholders are checked on the way in, not discovered at post time.** A line
like "{pet} accepts your offering" is handed to `str.format`, so an edit that
invents `{name}` raises KeyError in a background task and the post simply never
appears — the worst kind of failure, silent and hours late. Each pool records
which placeholders its own defaults use: any placeholder the defaults never use
is refused, and one that appears in *every* default is required (every feed line
names the pet; one that doesn't would be a line about nobody). The refusal names
the placeholder and lists what is available, because "invalid" alone doesn't
tell you what to type instead.

Reads come from memory, so `pick()` costs nothing on a hot path; writes go
through `storage.save_json` and update that memory, so an edit is live on the
next post with no restart.
"""
from __future__ import annotations

import logging
import random
import re
import threading
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from common import storage

log = logging.getLogger("cozy.lines")

PATH = storage.DATA_DIR / "lines.json"

# {name} — but not {{escaped}} braces, which format() renders literally.
_PLACEHOLDER = re.compile(r"(?<!\{)\{(\w+)\}")

MAX_LINE = 1000     # a modal's paragraph input holds 4000; this is generous
MAX_LINES = 300     # per pool, so one runaway paste can't bloat the file


@dataclass(frozen=True)
class Pool:
    """One editable pool, and everything the panel needs to explain it."""

    key: str
    label: str
    where: str                      # where these show up, in a few words
    default: tuple[str, ...]
    allowed: frozenset[str]         # placeholders any line may use
    required: frozenset[str]        # placeholders every line must use

    def hint(self) -> str:
        """What a person editing this needs to know about placeholders."""
        if not self.allowed:
            return "Plain text — no placeholders."
        bits = []
        if self.required:
            bits.append("must include " + ", ".join(f"`{{{p}}}`" for p in sorted(self.required)))
        optional = self.allowed - self.required
        if optional:
            bits.append("may use " + ", ".join(f"`{{{p}}}`" for p in sorted(optional)))
        return "Each line " + " and ".join(bits) + "."


_POOLS: dict[str, Pool] = {}
_overrides: Optional[dict[str, list[str]]] = None
_LOCK = threading.Lock()


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def register(
    key: str, label: str, default: Sequence[str], *, where: str = ""
) -> tuple[str, ...]:
    """Declare a pool. Returns the defaults, so a cog can keep its own constant:

        FEED_LINES = lines.register("pets.feed", "Pet feeding", (...))

    Registering the *same* key with the *same* defaults twice is fine and
    expected: `load_extension` builds a fresh module object and executes it even
    when the module is already in `sys.modules`, so a cog another cog imports
    has its body run twice at boot. Raising on that would take the bot down for
    a reason nobody could see from the code.

    Two *different* pools under one key is still an error, because the second
    would silently win and the first's lines would vanish from a panel that
    still lists the key.
    """
    rows = tuple(str(line) for line in default)
    existing = _POOLS.get(key)
    if existing is not None:
        if existing.default != rows:
            raise ValueError(
                f"lines: {key!r} is registered twice with different lines — "
                "two pools are sharing a key"
            )
        return existing.default
    per_line = [_placeholders(line) for line in rows]
    allowed = frozenset().union(*per_line) if per_line else frozenset()
    required = frozenset(set.intersection(*per_line)) if per_line else frozenset()
    _POOLS[key] = Pool(key, label, where, rows, allowed, required)
    return rows


def pools() -> list[Pool]:
    """Every registered pool, in a stable order for the panel."""
    return sorted(_POOLS.values(), key=lambda p: p.label.lower())


def get_pool(key: str) -> Optional[Pool]:
    return _POOLS.get(key)


def _load() -> dict[str, list[str]]:
    global _overrides
    if _overrides is None:
        raw = storage.load_json(PATH, default={})
        clean: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list):
                    rows = [str(v) for v in value if isinstance(v, str) and v.strip()]
                    if rows:
                        clean[str(key)] = rows
        _overrides = clean
        if clean:
            log.info("[lines] %d pool(s) overridden: %s", len(clean), ", ".join(sorted(clean)))
    return _overrides


def pool(key: str) -> list[str]:
    """The live lines for a pool: the override if there is one, else the code's.

    An override that has been emptied falls back rather than returning nothing —
    a pool with no lines is a post that never happens, and that failure shows up
    as silence rather than as an error.
    """
    with _LOCK:
        override = _load().get(key)
    if override:
        return list(override)
    known = _POOLS.get(key)
    return list(known.default) if known else []


def pick(key: str, *, avoid: Optional[str] = None) -> str:
    """One line at random, optionally not the one just used."""
    rows = pool(key)
    if not rows:
        return ""
    choices = [row for row in rows if row != avoid] or rows
    return random.choice(choices)


def is_overridden(key: str) -> bool:
    with _LOCK:
        return key in _load()


class LineError(ValueError):
    """A rejected edit, worded for the person who typed it."""


def check(key: str, text: str) -> str:
    """Validate one line for a pool. Returns it stripped, or raises LineError."""
    known = _POOLS.get(key)
    if known is None:
        raise LineError(f"There's no pool called `{key}`.")
    line = text.strip()
    if not line:
        raise LineError("An empty line isn't much of a line.")
    if len(line) > MAX_LINE:
        raise LineError(f"That's {len(line)} characters; the limit is {MAX_LINE}.")

    used = _placeholders(line)
    unknown = used - known.allowed
    if unknown:
        offer = (
            ", ".join(f"`{{{p}}}`" for p in sorted(known.allowed))
            if known.allowed else "none at all"
        )
        raise LineError(
            f"{', '.join(f'`{{{p}}}`' for p in sorted(unknown))} isn't a thing here — "
            f"this pool knows {offer}. Anything else would throw when the line is posted."
        )
    missing = known.required - used
    if missing:
        raise LineError(
            f"Every line here needs {', '.join(f'`{{{p}}}`' for p in sorted(missing))} — "
            "without it the line posts, but not about anyone."
        )
    return line


def save(key: str, rows: Iterable[str]) -> None:
    """Write a pool's lines. Validates every one before writing any."""
    if key not in _POOLS:
        raise LineError(f"There's no pool called `{key}`.")
    clean = [check(key, row) for row in rows]
    if not clean:
        raise LineError("A pool needs at least one line. Use Reset to restore the originals.")
    if len(clean) > MAX_LINES:
        raise LineError(f"That's {len(clean)} lines; the limit is {MAX_LINES}.")

    with _LOCK:
        data = _load()
        previous = data.get(key)
        data[key] = clean
        if not storage.save_json(PATH, data):
            if previous is None:
                data.pop(key, None)
            else:
                data[key] = previous
            raise LineError("Couldn't write the file, so nothing changed.")
    log.info("[lines] %s now has %d line(s)", key, len(clean))


def reset(key: str) -> bool:
    """Drop the override and go back to the code's own lines."""
    with _LOCK:
        data = _load()
        if key not in data:
            return False
        removed = data.pop(key)
        if not storage.save_json(PATH, data):
            data[key] = removed
            raise LineError("Couldn't write the file, so nothing changed.")
    log.info("[lines] %s reset to its defaults", key)
    return True
