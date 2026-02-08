# -*- coding: utf-8 -*-
"""
Mittens the Menace — Moderation Cog (Slash version)

Slash Commands:
  /purge amount:int                          – delete recent messages (Mama Cat & Ghoul only)
  /timeout user:member duration:str reason   – timeout a member with Mittens flair
  /untimeout user:member                     – remove timeout early

Role policy:
  - Only roles "Mama Cat" and "Ghoul" may use these commands.
  - Roles "Fresh Meat" and "Cozy Gremlins" are blocked from using commands.

Notes:
  - Timeout duration supports suffixes: s (seconds), m (minutes), h (hours), d (days).
    No suffix → minutes (e.g., "10" == "10m").
  - Timeout announcement is public and kept. Slash confirmation is ephemeral.
"""

import re
import random
from datetime import timedelta

import discord
from discord.ext import commands
from discord import app_commands

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
MAMA_CAT_ROLE_NAME = "Mama Cat"
GHOUL_ROLE_NAME    = "Ghoul"
BLOCKED_ROLE_NAMES = {"Fresh Meat", "Cozy Gremlins"}

TIMEOUT_LINES = [
    "Mittens has placed {user} in the corner for {duration}. Think about your life choices.",
    "{user} has been benched for {duration}. The claws were faster.",
    "Shhh… {user} is in cool-down mode for {duration} 💤",
    "{user} triggered the paw of justice. {duration} in the box.",
    "{user} poked the wrong cat. {duration} of silence awarded 🐾",
    "{user} was too loud. Mittens muted them for {duration} ⏰",
    "{user} has been bonked by Mittens’ paw. {duration} penalty applied.",
    "{user} tried to meow over Mittens. {duration} time-out imposed 😾",
    "Mittens has spoken. {user} will serve {duration} in timeout purgatory.",
]

# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────
def _names(member: discord.Member) -> set[str]:
    return {r.name for r in member.roles}

def has_mittens_power(member: discord.Member) -> bool:
    names = _names(member)
    return any(n in names for n in (MAMA_CAT_ROLE_NAME, GHOUL_ROLE_NAME))

def is_blocked(member: discord.Member) -> bool:
    return any(n in _names(member) for n in BLOCKED_ROLE_NAMES)

_DURATION_RE = re.compile(r"^(\d+)([smhd]?)$", re.I)

def parse_duration(raw: str) -> timedelta:
    m = _DURATION_RE.match(raw.strip())
    if not m:
        raise ValueError("Invalid duration format.")
    num, unit = m.groups()
    num = int(num)
    unit = unit.lower()
    if unit == "s":
        return timedelta(seconds=num)
    if unit == "h":
        return timedelta(hours=num)
    if unit == "d":
        return timedelta(days=num)
    # default or explicit 'm'
    return timedelta(minutes=num)

def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"

async def _deny(inter: discord.Interaction, text: str):
    try:
        await inter.response.send_message(text, ephemeral=True)
    except discord.InteractionResponded:
        await inter.followup.send(text, ephemeral=True)

# ──────────────────────────────────────────────────────────────
# COG
# ──────────────────────────────────────────────────────────────
class MittensModeration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Global gate for slash commands in this cog
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        member: discord.Member = interaction.user

        if is_blocked(member):
            await _deny(interaction, "Mittens doesn’t take orders from you. 🐾")
            return False

        if not has_mittens_power(member):
            await _deny(interaction, f"You lack the whiskers for that, {member.display_name}. 🐱‍👓")
            return False

        return True

    # ────────────── /purge ───────────────
    @app_commands.command(name="purge", description="Delete recent messages in this channel (max 200).")
    @app_commands.describe(amount="How many recent messages to delete (1–200).")
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200]):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await _deny(interaction, "This can only be used in a text channel.")

        perms = channel.permissions_for(interaction.guild.me)  # type: ignore
        if not (perms.manage_messages and perms.read_message_history):
            return await _deny(interaction, "I need **Manage Messages** and **Read Message History** here.")

        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            deleted = await channel.purge(limit=amount, bulk=True)
        except discord.Forbidden:
            return await interaction.followup.send("I lack permission to delete messages here.", ephemeral=True)
        except Exception:
            return await interaction.followup.send("Purge failed due to an error.", ephemeral=True)

        # Public lightweight confirmation that self-destructs
        try:
            public = await channel.send(f"Deleted {len(deleted)} messages 🧹")
            await public.delete(delay=7)
        except Exception:
            pass

        await interaction.followup.send(f"Done. Removed {len(deleted)}.", ephemeral=True)

    # ────────────── /timeout ───────────────
    @app_commands.command(name="timeout", description="Timeout a member. Example: 60s, 10m, 2h, 1d (default minutes).")
    @app_commands.describe(
        user="Member to timeout",
        duration="Duration like 60s, 10m, 2h, 1d (no suffix = minutes).",
        reason="Optional reason"
    )
    async def timeout(self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str | None = None):
        try:
            td = parse_duration(duration)
        except ValueError:
            return await _deny(interaction, "Invalid duration. Use formats like 60s, 10m, 2h, 1d; no suffix = minutes.")

        # Apply timeout (positional-only arg in discord.py 2.6.x)
        until_dt = discord.utils.utcnow() + td
        try:
            await user.timeout(until_dt, reason=reason or f"Timed out by {interaction.user}")
        except discord.Forbidden:
            return await _deny(interaction, "I lack the whiskers to timeout that one.")
        except Exception:
            return await _deny(interaction, "Could not timeout that member (role hierarchy or permissions).")

        # Public roast (kept)
        line = random.choice(TIMEOUT_LINES).format(user=user.mention, duration=format_duration(td))
        try:
            await interaction.channel.send(line)  # type: ignore
        except Exception:
            pass

        # Ephemeral OK
        await _deny(interaction, f"Timed out {user.display_name} for {format_duration(td)}.")

    # ────────────── /untimeout ───────────────
    @app_commands.command(name="untimeout", description="Remove timeout early from a member.")
    @app_commands.describe(user="Member to untimeout")
    async def untimeout(self, interaction: discord.Interaction, user: discord.Member):
        try:
            await user.timeout(None, reason=f"Untimed out by {interaction.user}")  # positional-only
        except discord.Forbidden:
            return await _deny(interaction, "I lack the whiskers to untimeout that one.")
        except Exception:
            return await _deny(interaction, "Could not untimeout that member (role hierarchy or permissions).")

        # Short public note (auto-cleans)
        try:
            msg = await interaction.channel.send(f"Mittens released {user.mention} from the box 🐾")  # type: ignore
            await msg.delete(delay=7)
        except Exception:
            pass

        await _deny(interaction, f"Removed timeout from {user.display_name}.")

# ──────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(MittensModeration(bot))
