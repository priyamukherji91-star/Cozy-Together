# -*- coding: utf-8 -*-
import asyncio
import datetime
import io
import logging
import random
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

LOG = logging.getLogger(__name__)

# The pet registry lives outside cogs/ — see petcare/__init__.py. If it can't be
# imported the ship commands still work; only /shippet drops out.
try:
    from petcare import storage as petstore
    PETS_AVAILABLE = True
except Exception:  # pragma: no cover - import-time degradation
    LOG.exception("Pet registry unavailable; /shippet will refuse politely")
    petstore = None  # type: ignore[assignment]
    PETS_AVAILABLE = False

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
SHIPPING_CHANNEL_ID = 1436115021066408016  # only allowed here

# Pet names and owner mentions go in the message body, so pin the mentions down.
MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)

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


# ───────────────────────────────────────────────
# Ship sides — a member or a pet
# ───────────────────────────────────────────────
@dataclass(frozen=True)
class Side:
    """One half of a ship.

    The card renderer only ever wanted a name and a picture, so members and pets
    collapse to this and _run_ship no longer cares which it was handed.
    """
    key: str                                        # stable, seeds the daily score
    name: str                                       # drawn under the circle
    tag: str                                        # how it reads in the embed
    user: discord.User | discord.Member | None = None   # members: avatar fetched at post time
    image: bytes | None = None                      # pets: already on disk


def _user_side(user: discord.User | discord.Member) -> Side:
    return Side(key=f"u{user.id}", name=user.display_name, tag=user.mention, user=user)


def _pet_side(pet, owner: discord.Member | None, image: bytes | None) -> Side:
    # Pets can't be pinged, so the mention lands on whoever owns them.
    owned_by = f" ({owner.mention}'s)" if owner is not None else ""
    return Side(
        key=f"p{pet.pet_id}",
        name=pet.name,
        tag=f"**{pet.name}**{owned_by}",
        image=image,
    )


def _image_from_bytes(raw: bytes) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        LOG.warning("[ship] could not decode a stored pet photo", exc_info=True)
        return None


async def _side_image(side: Side) -> Image.Image | None:
    """The circle picture for a side. A missing one becomes a grey placeholder."""
    if side.image is not None:
        return await asyncio.to_thread(_image_from_bytes, side.image)
    if side.user is not None:
        return await _get_avatar_image(side.user)
    return None


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
        await self._run_ship(interaction, _user_side(user1), _user_side(user2))

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
        await self._run_ship(interaction, _user_side(user1), _user_side(user2))

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
        await self._run_ship(interaction, _user_side(user), _user_side(random.choice(members)))

    # ── pets ────────────────────────────────────────────────────
    @app_commands.command(
        name="shippet", description="Ship a pet with a member or another pet 🐾💞"
    )
    @app_commands.describe(
        pet="Whose pet — start typing a name",
        partner="Another pet or a member. Leave empty to ship yourself with them.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def shippet(
        self,
        interaction: discord.Interaction,
        pet: str,
        partner: str | None = None,
    ):
        if not await self._ensure_shipping_channel(interaction):
            return
        if interaction.guild is None:
            return await interaction.response.send_message(
                "⚠️ This command can only be used in a server.", ephemeral=True
            )
        if not PETS_AVAILABLE:
            return await interaction.response.send_message(
                "🚫 The pet registry isn't loaded right now.", ephemeral=True
            )

        left = await self._resolve_side(interaction, f"p:{pet}")
        if left is None:
            return await interaction.response.send_message(
                "❌ I don't know that pet. Pick one from the list.", ephemeral=True
            )

        if partner is None:
            right = _user_side(interaction.user)
        else:
            right = await self._resolve_side(interaction, partner)
            if right is None:
                return await interaction.response.send_message(
                    "❌ I don't know who or what that is. Pick one from the list.",
                    ephemeral=True,
                )

        # The human reads better on the left.
        if right.key.startswith("u") and left.key.startswith("p"):
            left, right = right, left

        await self._run_ship(interaction, left, right)

    async def _resolve_side(
        self, interaction: discord.Interaction, token: str
    ) -> Side | None:
        """Turn an autocomplete value (`p:<pet_id>` or `u:<user_id>`) into a Side."""
        guild = interaction.guild
        assert guild is not None
        kind, _, ident = token.partition(":")

        if kind == "u":
            if not ident.isdigit():
                return None
            member = guild.get_member(int(ident))
            return _user_side(member) if member else None

        if kind != "p":
            return None

        record = await asyncio.to_thread(petstore.get_pet, guild.id, ident)
        if record is None:
            record = await asyncio.to_thread(petstore.find_by_name, guild.id, ident)
        if record is None:
            return None

        image = await asyncio.to_thread(petstore.image_bytes, record)
        if image is None:
            # Index says it exists, disk disagrees. Better a grey circle than a crash.
            LOG.warning("[ship] pet %s has no readable image", record.name)
        return _pet_side(record, guild.get_member(record.owner_id), image)

    def _pet_choices(
        self, guild: discord.Guild, pets: list, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.strip().casefold()
        out: list[app_commands.Choice[str]] = []
        for p in pets:
            if needle and needle not in p.name.casefold():
                continue
            owner = guild.get_member(p.owner_id)
            who = owner.display_name if owner else "someone who left"
            out.append(
                app_commands.Choice(name=f"{p.name} — {who}'s"[:100], value=p.pet_id)
            )
            if len(out) == 25:
                break
        return out

    @shippet.autocomplete("pet")
    async def _shippet_pet_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None or not PETS_AVAILABLE:
            return []
        pets = await asyncio.to_thread(petstore.all_pets, interaction.guild.id)
        return self._pet_choices(interaction.guild, pets, current)

    @shippet.autocomplete("partner")
    async def _shippet_partner_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Pets and members in one list — that's what lets one command cover
        pet × you, pet × pet and pet × member."""
        guild = interaction.guild
        if guild is None or not PETS_AVAILABLE:
            return []

        needle = current.strip().casefold()
        pets = await asyncio.to_thread(petstore.all_pets, guild.id)
        out = [
            app_commands.Choice(name=f"🐾 {c.name}"[:100], value=f"p:{c.value}")
            for c in self._pet_choices(guild, pets, current)
        ]

        for member in guild.members:
            if len(out) >= 25:
                break
            if member.bot:
                continue
            if needle and needle not in member.display_name.casefold():
                continue
            out.append(
                app_commands.Choice(
                    name=f"👤 {member.display_name}"[:100], value=f"u:{member.id}"
                )
            )
        return out[:25]

    async def _run_ship(
        self,
        interaction: discord.Interaction,
        left: Side,
        right: Side,
    ):
        if not await self._ensure_shipping_channel(interaction):
            return

        # Fetching an avatar and drawing the card can outrun the 3s response
        # window, and a pet photo adds a disk read on top.
        await interaction.response.defer()

        today = datetime.date.today().toordinal()
        combo = tuple(sorted([left.key, right.key]))
        score = (
            100 if left.key == right.key
            else random.Random(f"{combo[0]}-{combo[1]}-{today}").randint(0, 100)
        )

        image1 = await _side_image(left)
        image2 = await _side_image(right)
        image = await asyncio.to_thread(
            _compose_ship_image, image1, image2, score, left.name, right.name
        )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        ship_id = abs(hash((combo, today))) % 10000
        embed = discord.Embed(
            title=f"💘 {left.name} × {right.name}",
            color=_score_color(score),
        )
        embed.add_field(name="Pair", value=f"{left.tag} **×** {right.tag}", inline=False)
        embed.add_field(name="Compatibility", value=_score_bar(score), inline=False)
        embed.set_image(url="attachment://ship.png")
        embed.set_footer(text=f"Ship ID: #{ship_id} • Results reset daily ❤️")
        embed.timestamp = discord.utils.utcnow()

        await interaction.followup.send(
            embed=embed,
            file=discord.File(buffer, filename="ship.png"),
            allowed_mentions=MENTIONS,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"⏳ Slow down. Try again in {error.retry_after:.1f}s.", ephemeral=True
                )
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(MittensShipping(bot))
