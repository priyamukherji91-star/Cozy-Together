# -*- coding: utf-8 -*-
import datetime
import io
import random

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
SHIPPING_CHANNEL_ID = 1436115021066408016  # only allowed here

# ───────────────────────────────────────────────
# Image constants
# ───────────────────────────────────────────────
_W, _H = 480, 185
_BG = (0x1E, 0x1F, 0x22)
_AV_SIZE = 120
_AV_LEFT_X = 15
_AV_RIGHT_X = 345
_AV_Y = 12
_HEART_COLOR = (0xFF, 0x69, 0x87)
_HEART_CX = _W // 2           # 240
_HEART_CY = _AV_Y + _AV_SIZE // 2  # 72  (vertical mid of avatars)
_HEART_SIZE = 18              # half-width; keeps bottom at y=90, clear of score at y=94
_NAME_Y = 137                 # 5 px below avatar bottom (132)
_SCORE_Y = _HEART_CY + 22    # 94


# ───────────────────────────────────────────────
# Visual helpers (Pillow)
# ───────────────────────────────────────────────
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    win = ["arialbd.ttf", "Arial Bold.ttf"] if bold else ["arial.ttf", "Arial.ttf"]
    lin_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    lin_reg = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans.ttf",
    ]
    mac = ["/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf"]
    for name in win + (lin_bold if bold else lin_reg) + mac:
        try:
            return ImageFont.truetype(name, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


async def _get_avatar_image(user: discord.User, size: int = 256) -> Image.Image | None:
    try:
        data = await user.display_avatar.replace(size=size, format="png").read()
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def _circular_avatar(img: Image.Image | None, size: int) -> Image.Image:
    """Return a circular-cropped avatar; grey placeholder if img is None."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if img is None:
        ImageDraw.Draw(out).ellipse((0, 0, size, size), fill=(128, 128, 128, 255))
        return out
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out.paste(img, (0, 0), mask)
    return out


def _draw_heart(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy_center: int,
    size: int,
    color: tuple,
) -> None:
    """Two overlapping circles + downward triangle, visually centred at (cx, cy_center)."""
    r = size // 2
    y = cy_center - r  # shift so visual centre lands on cy_center
    # Left and right bumps (circles)
    draw.ellipse((cx - size, y - r, cx, y + r), fill=color)
    draw.ellipse((cx, y - r, cx + size, y + r), fill=color)
    # Downward triangle
    draw.polygon([(cx - size, y), (cx + size, y), (cx, y + size + r)], fill=color)


def _compose_ship_image(
    avatar1: Image.Image | None,
    avatar2: Image.Image | None,
    score: int,
    name1: str,
    name2: str,
) -> Image.Image:
    canvas = Image.new("RGB", (_W, _H), _BG)
    draw = ImageDraw.Draw(canvas)

    # Avatars
    av1 = _circular_avatar(avatar1, _AV_SIZE)
    av2 = _circular_avatar(avatar2, _AV_SIZE)
    canvas.paste(av1, (_AV_LEFT_X, _AV_Y), av1)
    canvas.paste(av2, (_AV_RIGHT_X, _AV_Y), av2)

    # Heart
    _draw_heart(draw, _HEART_CX, _HEART_CY, _HEART_SIZE, _HEART_COLOR)

    # Score
    font_score = _load_font(26, bold=True)
    score_text = f"{score}%"
    bb = draw.textbbox((0, 0), score_text, font=font_score)
    draw.text(
        ((_W - (bb[2] - bb[0])) // 2, _SCORE_Y),
        score_text,
        fill=(255, 255, 255),
        font=font_score,
    )

    # Names (centred under each avatar, truncated at 16 chars)
    font_name = _load_font(15, bold=True)
    for name, av_cx in (
        (name1[:16], _AV_LEFT_X + _AV_SIZE // 2),
        (name2[:16], _AV_RIGHT_X + _AV_SIZE // 2),
    ):
        bb = draw.textbbox((0, 0), name, font=font_name)
        draw.text(
            (av_cx - (bb[2] - bb[0]) // 2, _NAME_Y),
            name,
            fill=(255, 255, 255),
            font=font_name,
        )

    return canvas


# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────
def _score_bar(score: int, length: int = 10) -> str:
    score = max(0, min(100, score))
    filled = min(length, max(0, int(round((score / 100) * length))))
    return f"`{'█' * filled}{'░' * (length - filled)} {score}%`"


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
        return [m for m in guild.members if not m.bot and m.id not in excluded]

    @app_commands.command(name="ship", description="Ship two users and let Mittens stir up trouble 💞")
    @app_commands.describe(user1="First user", user2="Second user")
    async def ship(self, interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        await self._run_ship(interaction, user1, user2)

    @app_commands.command(name="shiprandom", description="Randomly ship two random server members 💘")
    async def shiprandom(self, interaction: discord.Interaction):
        if not await self._ensure_shipping_channel(interaction):
            return
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(
                "⚠️ This command can only be used in a server.", ephemeral=True
            )
        members = self._eligible_members(guild)
        if len(members) < 2:
            return await interaction.response.send_message("❌ Not enough members to ship!", ephemeral=True)
        user1, user2 = random.sample(members, 2)
        await self._run_ship(interaction, user1, user2)

    @app_commands.command(name="shipwithrandom", description="Ship one chosen user with a random server member 💞")
    @app_commands.describe(user="The user Mittens will pair with someone random")
    async def shipwithrandom(self, interaction: discord.Interaction, user: discord.User):
        if not await self._ensure_shipping_channel(interaction):
            return
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(
                "⚠️ This command can only be used in a server.", ephemeral=True
            )
        if user.bot:
            return await interaction.response.send_message(
                "🤖 Mittens refuses to ship bots. Even chaos has standards.", ephemeral=True
            )
        members = self._eligible_members(guild, exclude_user_ids={user.id})
        if not members:
            return await interaction.response.send_message(
                "❌ Not enough eligible members to pair with that user!", ephemeral=True
            )
        await self._run_ship(interaction, user, random.choice(members))

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
        score = 100 if user1.id == user2.id else random.Random(f"{combo[0]}-{combo[1]}-{today}").randint(0, 100)

        avatar1_img = await _get_avatar_image(user1)
        avatar2_img = await _get_avatar_image(user2)
        image = _compose_ship_image(avatar1_img, avatar2_img, score, user1.display_name, user2.display_name)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        ship_id = abs(hash((combo, today))) % 10000
        embed = discord.Embed(
            title=f"💘 {user1.display_name} × {user2.display_name}",
            color=_score_color(score),
        )
        embed.add_field(name="Pair", value=f"{user1.mention} **×** {user2.mention}", inline=False)
        embed.add_field(name="Compatibility", value=_score_bar(score), inline=False)
        embed.set_image(url="attachment://ship.png")
        embed.set_footer(text=f"Ship ID: #{ship_id} • Results reset daily ❤️")
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed, file=discord.File(buffer, filename="ship.png"))


async def setup(bot: commands.Bot):
    await bot.add_cog(MittensShipping(bot))
