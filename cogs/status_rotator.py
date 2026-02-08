# cogs/status_rotator.py
# -*- coding: utf-8 -*-
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks

ROTATE_MINUTES = 10  # change if you want a different interval


class StatusRotator(commands.Cog):
    """
    Rotates Mittens' presence every ROTATE_MINUTES using short, cat-chaotic lines.
    Includes /status_now and !statusnow to rotate on demand.

    Startup notes:
    - We DO NOT await wait_until_ready() in cog_load (that deadlocks startup).
    - Instead, we schedule a background task to wait for ready, then start the loop.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_task: asyncio.Task | None = None

        # --- Approved lines ---
        self.status_lines = [
            "meowing for no reason",
            "chasing invisible lasers",
            "mod of the red dot",
            "purring in caps lock",
            "box > bed, sorry",
            "brb, plotting",
            "do not disturb (zoomies)",
            "caught the cursor (again)",
            "snack time is now",
            "lint roller final boss",
            "Biting Cookie hehe",
            "Chasing Shadow’s chicken",
            "Eating Oem’s mouse",
            "hiding Champa’s socks",
            "claws deployed",
            "nap.exe running",
            "scratching the firewall",
            "Work? No.",
        ]

        # Randomize among these activity types for variety
        self.activity_types = [
            discord.ActivityType.playing,
            discord.ActivityType.watching,
            discord.ActivityType.listening,
            discord.ActivityType.competing,
        ]

    # ---------------- Helpers ----------------

    async def _set_random_status(self):
        """Safely set a random presence (waits for gateway; retries once)."""
        await self.bot.wait_until_ready()

        line = random.choice(self.status_lines)
        activity_type = random.choice(self.activity_types)

        if activity_type is discord.ActivityType.playing:
            activity = discord.Game(name=line)
        else:
            activity = discord.Activity(type=activity_type, name=line)

        try:
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=activity,
            )
        except AttributeError:
            await asyncio.sleep(1.0)
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=activity,
            )

    # ---------------- Loop ----------------

    @tasks.loop(minutes=ROTATE_MINUTES)
    async def rotate_status(self):
        await self._set_random_status()

    async def cog_load(self):
        self._startup_task = asyncio.create_task(self._startup_after_ready())

    def cog_unload(self):
        if self.rotate_status.is_running():
            self.rotate_status.cancel()
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

    async def _startup_after_ready(self):
        await self.bot.wait_until_ready()
        if not self.rotate_status.is_running():
            self.rotate_status.start()
        # Prime immediately on startup
        await self._set_random_status()

    # ---------------- Commands ----------------

    @app_commands.command(
        name="status_now",
        description="Rotate Mittens' status immediately.",
    )
    async def status_now(self, interaction: discord.Interaction):
        await self._set_random_status()
        await interaction.response.send_message(
            "New status set. 🐾",
            ephemeral=True,
        )

    @commands.command(
        name="statusnow",
        help="Rotate Mittens' status immediately.",
    )
    @commands.has_permissions(manage_guild=True)
    async def statusnow_prefix(self, ctx: commands.Context):
        await self._set_random_status()
        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass
        await ctx.reply(
            "New status set. 🐾",
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusRotator(bot))
