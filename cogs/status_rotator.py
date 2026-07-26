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
            "Meow..Meow fuck off",
            "Purring in your ear UwU",
            "Plotting against France",
            "Bring me snacks!",
            "Biting Cookie hehe",
            "Eating Land Chicken",
            "Ela hates me bo-hoo...",
            "Work sucks, like you haha",
            "Claws activated",
            "Judging your life choices",
            "Survived Yume's cooking",
            "Yume twerks, make him stop.",
            "Protecting toes from Oems",
            "Resisting the feet agenda",
            "muting DJ Champa",
            "Caffeine level : Shadow",
            "teaching Kaori English",
            "Living rent free in Ela's head",
            "Ela still hates me",
            "Maur x Vamp Fatale",
            "Lumi surviving London",
            "chatting up Bushra",
            "parking outside Asda",
            "another cheeky Nando's",
            "taking the scenic route to Bradford",
            "reporting Yume to Gordon Ramsay",
            "meow.exe crashed thanks Oems",
            "Oems breaking mods",
            "living just to annoy Ela",
            "Monster nr. 5, no sleep",
            "lying about quitting Monster",
            "Cookie disappeared again",
            "reminding Champa to blink",
            "Shadow zooming through the server",
            "saying bonjour incorrectly",
            "Definitely just friends",
            "Ela blocked me mentally",
            "Fortnite Rule 34",
            "Approving Lumi's annual leave",
            "Another pointless meeting",
            "keeping Cookie PG-13",
            "bit warm, innit",
            "Low it fam",
            "Cookie went AFK again",
            "Bossman energy",
            "pretending I saw nothing",
            "Cookie definitely started it",
            "Mittens > Admin",
            "Wagwan",
            "I hate you tbh",
            "Bellend detected",
            "petty and thriving",
            "ran out of patience",
            "suspiciously unhelpful",
            "blacklist energy",
            "trust issues deluxe",
            "maintenance is a curse",
            "patch day victim",
            "efficiency, but evil",
            "I'm hot and you know it",
            "Shat in your bed",
            "Oui Oui Croissant",
            "Bonjour le cochon",
            "France is not invited",
            "Kaori did 1789 wrong",
            "Ela can stay mad",
            "thriving off Ela's rage",
            "Ela's favorite bot",
            "Shadow owes me chicken",
            "Biting Shadow's cheeks",
            "Oems is my Mommy",
            "Cookie damage control",
            "Champa is a Destiny's Child",
            "hiding from Champa",
            "Jupi ate my tuna.",
            "Jupi banned from shipping",
            "Jupi loves me.",
            "monitoring Maurelin",
            "Artemisa in the logs",
            "Artemisa doxed herself",
            "Blair, make me a cake, ty.",
            "Lumi soft launching Blair",
            "Lumi too cool to care",
            "romantically unapproachable",
            "miserable but iconic",
            "born to inconvenience",
            "unbothered and unlawful",
            "low morale specialist",
            "operationally hostile",
            "built from bad intentions",
            "error 404: empathy",
            "smug beyond reason",
            "not arsed tbh",
            "bit rude innit",
            "one claw from violence",
            "petty crimes enthusiast",
            "allergic to decency",
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
