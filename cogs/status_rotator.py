# cogs/status_rotator.py
# -*- coding: utf-8 -*-
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks

ROTATE_MINUTES = 5  # change if you want a different interval


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
            "Plotting against France",
            "do not disturb (zoomies)",
            "caught the cursor (again)",
            "snack time is now",
            "lint roller final boss",
            "Biting Cookie hehe",
            "Land Chicken Hater",
            "Eating Oem’s mouse",
            "hiding Champa’s socks",
            "claws deployed",
            "nap.exe running",
            "scratching the firewall",
            "Work? No.",
            "catastrophe pending",
            "meowgical nonsense",
            "not my problem",
            "absolute feral hours",
            "blink and suffer",
            "tiny disaster mode",
            "too online for this",
            "no thoughts, claws",
            "current mood: hissing",
            "petty and thriving",
            "already annoyed",
            "ran out of patience",
            "suspiciously unhelpful",
            "anti-social unit",
            "blacklist energy",
            "trust issues deluxe",
            "chaos with manners",
            "feline bureaucracy",
            "dead inside, purring",
            "Stole your lunch",
            "running on spite",
            "midday menace",
            "I hate you tbh",
            "maintenance is a curse",
            "patch day victim",
            "team meeting hostage",
            "efficiency, but evil",
            "hide and seek champ",
            "botting with judgment",
            "I'm hot and you know it",
            "Wuk La who?",
            "Confused and dehydrated",
            "Shat in your bed",
            "bullying the French again",
            "France is not invited",
            "Kaori did 1789 wrong",
            "anti-French propaganda",
            "baguette surveillance",
            "macron.exe error",
            "plotting against Kaori",
            "Kaori apology denied",
            "Ela can stay mad",
            "professionally annoying Ela",
            "thriving off Ela’s rage",
            "Ela’s favorite bot",
            "Shadow stole my rights",
            "Shadow owes me chicken",
            "chasing Shadow legally",
            "Oems ate the evidence",
            "robbed by Oems again",
            "mouse theft investigator",
            "Cookie looked snackable",
            "Cookie failed inspection",
            "Cookie damage control",
            "Champa sock thief",
            "hiding from Champa",
            "Champa caused this",
            "anti-Champa measures",
            "Jupi made it worse",
            "orbiting Jupi drama",
            "Jupi incident ongoing",
            "celestial nuisance unit",
            "Akio under suspicion",
            "auditing Akio’s crimes",
            "Akio owes an explanation",
            "Maurelin made it weird",
            "filed against Maurelin",
            "monitoring Maurelin",
            "Artemisa in the logs",
            "moonlit HR violation",
            "Artemisa did something",
            "divine nuisance report",
            "Blair escalated things",
            "Blair made a face",
            "suspicious activity: Blair",
            "Blair is being reviewed",
            "Lumi ruined the vibe",
            "Lumi too cool to care",
            "romantically unapproachable",
            "miserable but iconic",
            "kaz did something weird",
            "classic kaz behavior",
            "kaz remains unwell",
            "ask kaz what happened",
            "certified kaz nonsense",
            "kaz is the problem",
            "Kaori in the pond",
            "HR’s worst nightmare",
            "born to inconvenience",
            "this feels targeted",
            "extremely off-putting",
            "too glamorous to care",
            "menace in production",
            "emotionally unavailable bot",
            "unbothered and unlawful",
            "unionizing the zoomies",
            "low morale specialist",
            "operationally hostile",
            "built from bad intentions",
            "customer support menace",
            "malicious compliance cat",
            "professionally difficult",
            "error 404: empathy",
            "smug beyond reason",
            "tiny tyrant online",
            "all bite no briefing",
            "hostile by design",
            "deeply unchuffed",
            "not arsed tbh",
            "bit rude innit",
            "catastrophic little beast",
            "one claw from violence",
            "petty crimes enthusiast",
            "allergic to decency",
            "baguette war criminal",
            "France made this worse",
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
