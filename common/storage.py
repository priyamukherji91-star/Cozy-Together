# common/storage.py
# -*- coding: utf-8 -*-
"""Where the bot's state lives, and how it gets written.

Two things in here are load-bearing, and both were learned the hard way by the
pet system before they were shared out:

1. **One DATA_DIR.** Every cog that persists anything resolves its path from
   `DATA_DIR` here rather than calling `os.getenv` with its own default. Cogs
   used to disagree — one defaulted to `/data`, another to a *relative* `data/`
   that isn't on the volume at all — so state silently scattered, and the
   relative one was wiped by every deploy.

2. **Saves are atomic.** Write to a temp file, fsync, rename over the target. A
   plain `open(path, "w")` truncates before writing, so a restart landing
   mid-write loses the whole file. Railway restarts on deploy, so this matters.

A file that exists but won't parse is set aside as `<name>.corrupt` rather than
being overwritten by the next save — `scripts/volume_cleanup.py` knows to sweep
those up, along with any `.tmp` left behind by a save that died mid-flight.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Union

LOG = logging.getLogger(__name__)

PathLike = Union[str, Path]

# The Railway volume. Override with the DATA_DIR env var if the mount moves;
# `/app/data` is the mount path the service is configured with today.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))


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
