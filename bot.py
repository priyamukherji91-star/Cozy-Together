# -*- coding: utf-8 -*-
"""
Entry point for Cozy Bot on Railway.

This version **auto-loads all top-level cogs** (any `*.py` file next to
this file that exposes `async def setup(bot)`), *and* anything inside a
`./cogs` package if you later add one. It also performs a clean, resilient
slash-command sync.

Tested with discord.py 2.4.x.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Iterable, Optional

import discord
from discord.ext import commands

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*_args, **_kwargs):
        return False

# ──────────────────────────────────────────────────────────────
# Config / logging
# ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
load_dotenv()  # no-op if package missing

TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
DEV_GUILD_ID: Optional[int] = None
_gid = os.getenv("GUILD_ID")
if _gid:
    try:
        DEV_GUILD_ID = int(_gid)
    except ValueError:
        DEV_GUILD_ID = None

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cozy.bot")

# ──────────────────────────────────────────────────────────────
# Intents / Bot
# ──────────────────────────────────────────────────────────────
MESSAGE_CONTENT_INTENT = os.getenv("MESSAGE_CONTENT_INTENT", "false").lower() in {"1","true","yes","y"}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # required for onboarding & role ops
intents.message_content = MESSAGE_CONTENT_INTENT  # needed for X/Twitter fixer

bot = commands.Bot(command_prefix="!", intents=intents)

# ──────────────────────────────────────────────────────────────
# Cog loaders
# ──────────────────────────────────────────────────────────────
async def _load_extension_safe(ext_name: str) -> bool:
    """Try to load a standard discord.py extension (e.g. `cogs.name`)."""
    try:
        if ext_name in bot.extensions:
            log.info("Extension already loaded: %s", ext_name)
            return True
        await bot.load_extension(ext_name)
        log.info("Loaded extension: %s", ext_name)
        return True
    except commands.ExtensionFailed:
        log.exception("Extension failed: %s", ext_name)
    except commands.ExtensionNotFound:
        pass
    except Exception:
        log.exception("Unexpected error loading %s", ext_name)
    return False

async def _load_module_from_path(py: Path) -> bool:
    """Import an arbitrary `*.py` as a module and call its async `setup(bot)` if present."""
    if not py.is_file() or py.suffix != ".py":
        return False
    mod_name = f"_cozy_cog_{py.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(py))
        if not spec or not spec.loader:
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        setup = getattr(module, "setup", None)
        if setup is None:
            log.debug("No setup(bot) in %s — skipping", py.name)
            return False
        if asyncio.iscoroutinefunction(setup):
            await setup(bot)
        else:
            # support sync setup that returns maybe a task
            maybe = setup(bot)
            if asyncio.iscoroutine(maybe):
                await maybe
        log.info("Loaded cog via file: %s", py.name)
        return True
    except Exception:
        log.exception("Failed to load cog file: %s", py)
        return False

async def load_all_cogs() -> None:
    """Load from `./cogs/*.py` (as extensions) and top-level `*.py` files (direct)."""
    # 1) Package-style: ./cogs/*.py
    cogs_dir = ROOT / "cogs"
    if cogs_dir.exists():
        init_file = cogs_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("# package marker\n", encoding="utf-8")
        for py in sorted(cogs_dir.glob("*.py")):
            if py.name.startswith("_"):
                continue
            await _load_extension_safe(f"cogs.{py.stem}")

    # 2) Top-level modules next to bot.py
    for py in sorted(ROOT.glob("*.py")):
        if py.name in {"bot.py", "__init__.py"}:
            continue
        if py.name.startswith("_"):
            continue
        # If something with same stem was loaded as extension, skip
        if f"cogs.{py.stem}" in bot.extensions:
            continue
        await _load_module_from_path(py)

# ──────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────
@bot.event
async def setup_hook():
    # Load cogs before syncing commands
    await load_all_cogs()

    # Slash command sync: prefer dev guild for fast iteration if provided
    try:
        if DEV_GUILD_ID:
            dev = discord.Object(id=DEV_GUILD_ID)
            bot.tree.copy_global_to(guild=dev)
            synced = await bot.tree.sync(guild=dev)
            log.info("Synced %d app commands to dev guild %s.", len(synced), DEV_GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Globally synced %d app commands.", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands")

@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    # Friendly default presence
    try:
        await bot.change_presence(activity=discord.Game(name="/help"))
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────
# Simple healthcheck (prefix)
# ──────────────────────────────────────────────────────────────
@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.reply(f"Pong! {round(bot.latency * 1000)}ms")

# ──────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN env var")
    bot.run(TOKEN)
