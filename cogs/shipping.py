# -*- coding: utf-8 -*-
import datetime
import io
import random

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFilter

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
SHIPPING_CHANNEL_ID = 1436115021066408016  # only allowed here


# ───────────────────────────────────────────────
# Visual helpers (Pillow)
# ───────────────────────────────────────────────
async def _get_avatar_image(user: discord.User, size: int = 512) -> Image.Image:
    """Fetch a user's avatar as a Pillow Image."""
    asset = user.display_avatar.replace(size=size, format="png")
    data = await asset.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _circular_avatar(img: Image.Image, size: int) -> Image.Image:
    """Return a circular-cropped avatar of the given size."""
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _draw_heart(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color: tuple[int, int, int]) -> None:
    """Draw a simple heart shape centered around x/y."""
    radius = size // 2
    draw.ellipse((x - size, y - radius, x, y + radius), fill=color)
    draw.ellipse((x, y - radius, x + size, y + radius), fill=color)
    draw.polygon(
        [
            (x - size, y),
            (x + size, y),
            (x, y + size + radius),
        ],
        fill=color,
    )


def _compose_ship_image(avatar1: Image.Image, avatar2: Image.Image, rng: random.Random) -> Image.Image:
    """Create a ship image with multiple possible templates."""
    width, height = 800, 400
    base = Image.new("RGBA", (width, height), (20, 20, 30, 255))
    draw = ImageDraw.Draw(base)

    palettes = [
        {
            "name": "soft_heart",
            "bg": (30, 24, 48),
            "accent": (255, 158, 193),
            "accent2": (140, 111, 255),
        },
        {
            "name": "neon_split",
            "bg": (10, 8, 24),
            "accent": (0, 255, 200),
            "accent2": (255, 0, 153),
        },
        {
            "name": "dark_grunge",
            "bg": (8, 8, 12),
            "accent": (237, 76, 103),
            "accent2": (255, 189, 89),
        },
    ]
    palette = rng.choice(palettes)

    base.paste(Image.new("RGBA", (width, height), palette["bg"]), (0, 0))

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow)
    g_draw.ellipse(
        (-100, -50, width + 100, height + 150),
        fill=(palette["accent2"][0], palette["accent2"][1], palette["accent2"][2], 90),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    base = Image.alpha_composite(base, glow)

    avatar_size = 260
    av1 = _circular_avatar(avatar1, avatar_size)
    av2 = _circular_avatar(avatar2, avatar_size)

    style = palette["name"]

    if style == "soft_heart":
        y = (height - avatar_size) // 2
        x1 = 90
        x2 = width - avatar_size - 90

        frame_pad = 10
        for x, color in ((x1 - frame_pad, palette["accent"]), (x2 - frame_pad, palette["accent2"])):
            draw.rounded_rectangle(
                (x, y - frame_pad, x + avatar_size + 2 * frame_pad, y + avatar_size + 2 * frame_pad),
                radius=40,
                outline=color,
                width=3,
            )

        base.paste(av1, (x1, y), av1)
        base.paste(av2, (x2, y), av2)

        _draw_heart(draw, width // 2, height // 2, 40, palette["accent"])

    elif style == "neon_split":
        y = (height - avatar_size) // 2
        x1 = width // 2 - avatar_size - 40
        x2 = width // 2 - avatar_size // 2 + 40

        draw.rectangle(
            (-40, height // 2 - 120, width + 40, height // 2 - 70),
            fill=(palette["accent"][0], palette["accent"][1], palette["accent"][2], 80),
        )
        draw.rectangle(
            (-40, height // 2 + 70, width + 40, height // 2 + 120),
            fill=(palette["accent2"][0], palette["accent2"][1], palette["accent2"][2], 80),
        )

        base.paste(av1, (x1, y), av1)
        base.paste(av2, (x2, y), av2)

    else:  # dark_grunge
        y = (height - avatar_size) // 2
        x1 = 120
        x2 = width - avatar_size - 120

        for offset in range(0, 20, 4):
            draw.line(
                (x1 - 30 + offset, y - 20, x1 + avatar_size + 20 + offset, y + avatar_size + 30),
                fill=(palette["accent"][0], palette["accent"][1], palette["accent"][2], 40),
                width=3,
            )
            draw.line(
                (x2 - 20 - offset, y - 20, x2 + avatar_size + 30 - offset, y + avatar_size + 30),
                fill=(palette["accent2"][0], palette["accent2"][1], palette["accent2"][2], 40),
                width=3,
            )

        base.paste(av1, (x1, y), av1)
        base.paste(av2, (x2, y), av2)

    return base.convert("RGB")


# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────
def _score_bar(score: int, length: int = 10) -> str:
    """Text progress bar for the score, e.g. ██████░░░░ 60%."""
    score = max(0, min(100, score))
    filled = int(round((score / 100) * length))
    filled = min(length, max(0, filled))
    bar = "█" * filled + "░" * (length - filled)
    return f"`{bar} {score}%`"


def _score_color(score: int) -> discord.Color:
    if score >= 85:
        return discord.Color.from_rgb(255, 105, 180)
    if score >= 60:
        return discord.Color.from_rgb(255, 85, 85)
    if score >= 35:
        return discord.Color.from_rgb(255, 170, 0)
    return discord.Color.from_rgb(120, 120, 120)


class MittensShipping(commands.Cog):
    """💘 Ship command — daily chaos, exclusive to the shipping channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        return [member for member in guild.members if not member.bot and member.id not in excluded]

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

    async def _run_ship(
        self,
        interaction: discord.Interaction,
        user1: discord.User,
        user2: discord.User,
    ):
        if not await self._ensure_shipping_channel(interaction):
            return

        today = datetime.date.today().toordinal()
        combo = tuple(sorted([user1.id, user2.id]))
        rng = random.Random(int(f"{combo[0]}{combo[1]}{today}"))
        score = rng.randint(0, 100)

        avatar1_img = await _get_avatar_image(user1)
        avatar2_img = await _get_avatar_image(user2)
        image = _compose_ship_image(avatar1_img, avatar2_img, rng)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        ship_file = discord.File(buffer, filename="ship.png")

        ship_id = abs(hash((combo, today))) % 10000

        embed = discord.Embed(
            title=f"💘 {user1.display_name} × {user2.display_name}",
            color=_score_color(score),
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
        embed.set_image(url="attachment://ship.png")
        embed.set_footer(text=f"Ship ID: #{ship_id} • Results reset daily ❤️")
        embed.timestamp = datetime.datetime.utcnow()

        await interaction.response.send_message(embed=embed, file=ship_file)


async def setup(bot: commands.Bot):
    await bot.add_cog(MittensShipping(bot))
