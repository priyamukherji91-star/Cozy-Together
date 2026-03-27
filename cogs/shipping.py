# -*- coding: utf-8 -*-
import datetime
import random

import discord
from discord import app_commands
from discord.ext import commands

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
SHIPPING_CHANNEL_ID = 1436115021066408016  # only allowed here


def _score_bar(score: int, length: int = 10) -> str:
    """Text progress bar for the score, e.g. ██████░░░░ 60%."""
    score = max(0, min(100, score))
    filled = int(round((score / 100) * length))
    filled = min(length, max(0, filled))
    bar = "█" * filled + "░" * (length - filled)
    return f"`{bar} {score}%`"


class MittensShipping(commands.Cog):
    """💘 Ship command — daily chaos, exclusive to the shipping channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────────────────────────────────────────────
    # Helpers
    # ───────────────────────────────────────────────
    async def _ensure_shipping_channel(self, interaction: discord.Interaction) -> bool:
        if not interaction.channel or interaction.channel.id != SHIPPING_CHANNEL_ID:
            await interaction.response.send_message(
                "🚫 This command only works in <#1436115021066408016> — go spread chaos there.",
                ephemeral=True,
            )
            return False
        return True

    def _eligible_members(
        self,
        guild: discord.Guild,
        exclude_user_ids: set[int] | None = None,
    ) -> list[discord.Member]:
        excluded = exclude_user_ids or set()
        return [m for m in guild.members if not m.bot and m.id not in excluded]

    # ───────────────────────────────────────────────
    # /ship Command
    # ───────────────────────────────────────────────
    @app_commands.command(
        name="ship",
        description="Ship two users and let Mittens stir up trouble 💞",
    )
    @app_commands.describe(user1="First user", user2="Second user")
    async def ship(
        self,
        interaction: discord.Interaction,
        user1: discord.User,
        user2: discord.User,
    ):
        await self._run_ship(interaction, user1, user2)

    # ───────────────────────────────────────────────
    # /shiprandom Command
    # ───────────────────────────────────────────────
    @app_commands.command(
        name="shiprandom",
        description="Randomly ship two random server members 💘",
    )
    async def shiprandom(self, interaction: discord.Interaction):
        if not await self._ensure_shipping_channel(interaction):
            return

        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(
                "⚠️ This command can only be used in a server.",
                ephemeral=True,
            )

        members = self._eligible_members(guild)
        if len(members) < 2:
            return await interaction.response.send_message(
                "❌ Not enough members to ship!",
                ephemeral=True,
            )

        user1, user2 = random.sample(members, 2)
        await self._run_ship(interaction, user1, user2)

    # ───────────────────────────────────────────────
    # /shipwithrandom Command
    # ───────────────────────────────────────────────
    @app_commands.command(
        name="shipwithrandom",
        description="Ship one chosen user with a random server member 💞",
    )
    @app_commands.describe(user="The user Mittens will pair with someone random")
    async def shipwithrandom(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ):
        if not await self._ensure_shipping_channel(interaction):
            return

        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(
                "⚠️ This command can only be used in a server.",
                ephemeral=True,
            )

        if user.bot:
            return await interaction.response.send_message(
                "🤖 Mittens refuses to ship bots. Even chaos has standards.",
                ephemeral=True,
            )

        members = self._eligible_members(guild, exclude_user_ids={user.id})
        if not members:
            return await interaction.response.send_message(
                "❌ Not enough eligible members to pair with that user!",
                ephemeral=True,
            )

        random_partner = random.choice(members)
        await self._run_ship(interaction, user, random_partner)

    # ───────────────────────────────────────────────
    # Internal shared logic
    # ───────────────────────────────────────────────
    async def _run_ship(
        self,
        interaction: discord.Interaction,
        user1: discord.User,
        user2: discord.User,
    ):
        if not await self._ensure_shipping_channel(interaction):
            return

        # deterministic random seed (daily)
        today = datetime.date.today().toordinal()
        combo = tuple(sorted([user1.id, user2.id]))
        rng = random.Random(int(f"{combo[0]}{combo[1]}{today}"))
        score = rng.randint(0, 100)

        ship_img = (
            f"https://api.luminabot.xyz/image/ship?"
            f"user1={user1.display_avatar.url}&user2={user2.display_avatar.url}"
        )

        embed = discord.Embed(
            title=f"💘 {user1.display_name} × {user2.display_name}",
            color=discord.Color.random(),
        )
        embed.add_field(
            name="Pair",
            value=f"{user1.mention} **×** {user2.mention}",
            inline=False,
        )
        embed.add_field(
            name="Compatibility",
            value=_score_bar(score),
            inline=False,
        )
        embed.set_image(url=ship_img)
        embed.set_footer(text="Results reset daily ❤️")
        embed.timestamp = datetime.datetime.utcnow()

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MittensShipping(bot))
