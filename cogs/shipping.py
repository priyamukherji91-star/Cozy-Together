# -*- coding: utf-8 -*-
import random
import datetime
import io

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


def _compose_ship_image(avatar1: Image.Image, avatar2: Image.Image, rng: random.Random) -> Image.Image:
    """
    Create a ship image with multiple possible templates.
    Returns a Pillow Image (RGB).
    """
    width, height = 800, 400
    base = Image.new("RGBA", (width, height), (20, 20, 30, 255))
    draw = ImageDraw.Draw(base)

    # Different palettes/templates for chaos
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

    # Background
    base.paste(Image.new("RGBA", (width, height), palette["bg"]), (0, 0))

    # Subtle vignette / glow
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow)
    g_draw.ellipse(
        (-100, -50, width + 100, height + 150),
        fill=(palette["accent2"][0], palette["accent2"][1], palette["accent2"][2], 90),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    base = Image.alpha_composite(base, glow)

    # Create circular avatars
    avatar_size = 260
    av1 = _circular_avatar(avatar1, avatar_size)
    av2 = _circular_avatar(avatar2, avatar_size)

    # Different layout per template
    style = palette["name"]

    if style == "soft_heart":
        # side by side, centered
        y = (height - avatar_size) // 2
        x1 = 90
        x2 = width - avatar_size - 90

        # soft frames
        frame_pad = 10
        for (x, color) in [(x1 - frame_pad, palette["accent"]), (x2 - frame_pad, palette["accent2"])]:
            draw.rounded_rectangle(
                (x, y - frame_pad, x + avatar_size + 2 * frame_pad, y + avatar_size + 2 * frame_pad),
                radius=40,
                outline=color,
                width=3,
            )

        base.paste(av1, (x1, y), av1)
        base.paste(av2, (x2, y), av2)

        # central heart
        heart_x = width // 2
        heart_y = height // 2
        _draw_heart(draw, heart_x, heart_y, 40, palette["accent"])

    elif style == "neon_split":
        # slight overlap in the centre
        y = (height - avatar_size) // 2
        x1 = width // 2 - avatar_size - 40
        x2 = width // 2 - avatar_size // 2 + 40

        # diagonal-ish neon bars
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

        # small hearts trail
        for i in range(5):
            size = 16 + i * 4
            offset_x = width // 2 - 80 + i * 30
            offset_y = 70 + i * 12
            _draw_heart(draw, offset_x, offset_y, size, palette["accent2"])

    else:  # dark_grunge
        # grungy bars
        for i in range(6):
            bar_y = 30 + i * 50
            alpha = 40 + i * 15
            draw.rectangle(
                (0, bar_y, width, bar_y + 25),
                fill=(255, 255, 255, alpha),
            )

        y = (height - avatar_size) // 2
        x1 = 110
        x2 = width - avatar_size - 110

        base.paste(av1, (x1, y), av1)
        base.paste(av2, (x2, y), av2)

        # central big heart
        _draw_heart(draw, width // 2, height // 2, 55, palette["accent"])

    return base.convert("RGB")


def _draw_heart(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color):
    """Draw a simple heart shape centered at (x, y)."""
    w = size
    h = int(size * 0.9)
    top_box = (x - w // 2, y - h // 2, x + w // 2, y + h // 2)

    # Two circles + a triangle = heart
    r = w // 2
    # left circle
    draw.ellipse(
        (top_box[0], top_box[1], top_box[0] + r, top_box[1] + r),
        fill=color,
    )
    # right circle
    draw.ellipse(
        (top_box[2] - r, top_box[1], top_box[2], top_box[1] + r),
        fill=color,
    )
    # bottom triangle
    draw.polygon(
        [
            (top_box[0], top_box[1] + r // 2),
            (top_box[2], top_box[1] + r // 2),
            (x, top_box[3]),
        ],
        fill=color,
    )


def _score_color(score: int) -> discord.Color:
    """Return an embed color based on compatibility score."""
    if score >= 85:
        return discord.Color.from_rgb(255, 105, 180)  # hot pink
    if score >= 65:
        return discord.Color.orange()
    if score >= 45:
        return discord.Color.gold()
    if score >= 25:
        return discord.Color.blurple()
    return discord.Color.dark_red()


def _score_tier(score: int) -> str:
    """Short ship tier label."""
    if score >= 95:
        return "💍 Soulmates"
    if score >= 85:
        return "💖 Canon couple"
    if score >= 70:
        return "🔥 Chaotic good"
    if score >= 55:
        return "😼 Situationship"
    if score >= 40:
        return "🚩 Red flags"
    if score >= 20:
        return "🤝 Besties energy"
    return "💀 Shipwreck"


def _score_bar(score: int, length: int = 10) -> str:
    """Text progress bar for the score, e.g. ██████░░░░ 60%."""
    score = max(0, min(100, score))
    filled = int(round((score / 100) * length))
    filled = min(length, max(0, filled))
    bar = "█" * filled + "░" * (length - filled)
    return f"`{bar} {score}%`"


class MittensShipping(commands.Cog):
    """💘 Ship command — chaotic, daily, and exclusive to the shipping channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────────────────────────────────────────────
    # /ship Command
    # ───────────────────────────────────────────────
    @app_commands.command(
        name="ship",
        description="Ship two users and let Mittens deliver the judgment 💞",
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
        if not interaction.channel or interaction.channel.id != SHIPPING_CHANNEL_ID:
            return await interaction.response.send_message(
                "🚫 This command only works in <#1436115021066408016> — go spread chaos there.",
                ephemeral=True,
            )

        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(
                "⚠️ This command can only be used in a server.",
                ephemeral=True,
            )

        members = [m for m in guild.members if not m.bot]
        if len(members) < 2:
            return await interaction.response.send_message(
                "❌ Not enough members to ship!",
                ephemeral=True,
            )

        user1, user2 = random.sample(members, 2)
        await self._run_ship(interaction, user1, user2)

    # ───────────────────────────────────────────────
    # Internal shared logic
    # ───────────────────────────────────────────────
    async def _run_ship(
        self,
        interaction: discord.Interaction,
        user1: discord.User,
        user2: discord.User,
    ):
        if not interaction.channel or interaction.channel.id != SHIPPING_CHANNEL_ID:
            return await interaction.response.send_message(
                "🚫 This command only works in <#1436115021066408016> — go spread chaos there.",
                ephemeral=True,
            )

        self_ship = user1.id == user2.id

        # deterministic random seed (daily)
        today = datetime.date.today().toordinal()
        combo = tuple(sorted([user1.id, user2.id]))
        rng = random.Random(int(f"{combo[0]}{combo[1]}{today}"))
        score = rng.randint(0, 100)

        # couple mash name
        n1, n2 = user1.display_name, user2.display_name
        couple_name = (
            (n1[:3] + n1[-3:]).capitalize()
            if self_ship
            else (n1[:3] + n2[-3:]).capitalize()
        )

        # ── Flavor pools ───────────────────────────────
        pure_love = [
            "💞 Their chemistry could power a star.",
            "💍 Destined to outlive every ship war.",
            "🌹 A love story strong enough to crash servers.",
            "🔥 One look and the bot blushed.",
            "💘 Canon since day one.",
            "🧃 They give ‘it’s complicated’ a happy ending.",
            "🎇 Sparks, serotonin, and sweet chaos.",
            "🥰 OTP, no discussion.",
            "💫 Too cute; FDA needs to regulate this.",
            "💋 Netflix called — season 2 confirmed.",
            "🌈 Perfect balance of chaos and cuddles.",
            "🎵 They harmonize in the key of love.",
            "💎 Relationship goals: achieved.",
            "🫶 They literally broke the algorithm.",
        ]

        mild_chaos = [
            "😅 A stable 60 FPS of emotional instability.",
            "🎢 70% flirting, 30% existential dread.",
            "🍷 Chaotic good meets lawful dumb.",
            "🧩 Puzzle pieces that kinda fit but also fight.",
            "😌 They’d be cute if they stopped arguing.",
            "🛠️ Patch notes: communication fixes pending.",
            "🪞 One’s the mirror, the other’s the reflection — cracked.",
            "🎭 Enemies to lovers speedrun category.",
            "📞 Relationship tech support on standby.",
            "💅 Spicy energy; HR would have questions.",
            "🧃 Off the charts in chaos compatibility.",
            "🧠 Brains? Shared cell. Vibes? 10/10.",
            "⚡ Electricity in the air… and slight concern.",
            "🧸 Soft chaos, certified gremlin duo.",
        ]

        dramatic = [
            "🍿 Drama so juicy it gets its own recap thread.",
            "🎬 One’s a romcom, the other’s a horror — and it works.",
            "💅 Scandalous levels of chemistry.",
            "🪞 Main character x plot twist energy.",
            "🧃 Mutual chaos, zero regrets.",
            "🎭 They flirt like it’s a contact sport.",
            "💌 Public menace meets private simp.",
            "⚡ Sparks + explosions + emotional damage.",
            "🧠 Banter game strong, life choices weak.",
            "🎤 They’d argue, then make up spectacularly.",
            "🧨 Passion so loud it triggers mod alerts.",
            "🔥 A love story written in caps lock.",
            "🪩 They need a reality show, not therapy.",
            "👀 ‘Will they / won’t they’ — server edition.",
        ]

        doomed = [
            "💀 Relationship.exe has crashed.",
            "🥴 One ghosted mid-typing.",
            "🪦 Compatibility not found (404).",
            "📉 Stock fell faster than Bitcoin 2018.",
            "🤡 They’re fighting over pizza toppings already.",
            "🚪 Door slammed before the ship even launched.",
            "💢 Chaotic evil meets lawful disaster.",
            "🥀 This pairing belongs in a case study.",
            "🔥 Hot mess express with no brakes.",
            "😬 The breakup arc writes itself.",
            "💣 Love bombed then rage quit.",
            "🫠 Chemistry? Yes. Stability? No.",
            "🦴 Skeletons in the DMs.",
            "🤖 Even the AI said ‘nah’.",
        ]

        tragic_comedy = [
            "🤣 Pure comedy gold — the universe’s favorite bit.",
            "🫣 They’d roast each other daily but secretly adore it.",
            "💅 Not healthy, but entertaining as hell.",
            "😂 Even Mittens can’t look away.",
            "🤔 Friends? Enemies? Both.",
            "📺 New sitcom unlocked: *Two Idiots and a Typo.*",
            "🎯 Accidentally perfect, intentionally a mess.",
            "🎢 Ride or die — mostly ride.",
            "🥂 Chaos, charm, and questionable taste.",
            "🧃 The server’s favorite problematic duo.",
            "💫 Their love language is sarcasm.",
            "😈 The definition of ‘it’s complicated.’",
            "🐾 Mittens ships it for the drama.",
            "🎤 Constantly roasting, never ghosting.",
        ]

        self_love = [
            "🪞 Radical self-love speedrun any%.",
            "💅 Honestly, you’re your best match.",
            "💖 Main character refuses to settle for less than themselves.",
            "🧃 Self-ship so strong it bends canon.",
            "👑 If no one else, at least you’ve got you.",
            "✨ Peak energy: dating your own potential.",
        ]

        # Base comment selection
        if self_ship:
            comment = rng.choice(self_love)
        else:
            if score >= 85:
                comment = rng.choice(pure_love)
            elif score >= 65:
                comment = rng.choice(dramatic)
            elif score >= 45:
                comment = rng.choice(mild_chaos)
            elif score >= 25:
                comment = rng.choice(tragic_comedy)
            else:
                comment = rng.choice(doomed)

        # Easter eggs
        if self_ship and score == 100:
            comment = "💖 Perfect self-love arc unlocked. Therapist approved."
        elif not self_ship and score == 100:
            comment = "💍 100% — devs hard-coded this ship into canon."
        elif score == 69:
            comment = "😏 69% — the server did *not* need to see this, but here we are."
        elif score == 0:
            comment = "💀 0% — Mittens quietly backed away from this one."

        adjectives = [
            "chaotic",
            "forbidden",
            "galactic",
            "unholy",
            "divine",
            "mildly concerning",
            "dramatic",
            "feral",
            "cat-approved",
            "AI-rejected",
            "server-breaking",
        ]
        comment = f"{comment} ({rng.choice(adjectives)} energy detected.)"

        # Build ship image with Pillow
        avatar1_img = await _get_avatar_image(user1)
        avatar2_img = await _get_avatar_image(user2)
        image = _compose_ship_image(avatar1_img, avatar2_img, rng)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        ship_file = discord.File(buffer, filename="ship.png")

        # Ship ID
        ship_id = abs(hash((combo, today))) % 10000

        # Embed
        title_emoji = "💘" if score >= 70 else ("🔥" if score >= 40 else "💀")
        pair_title = (
            f"{user1.display_name} × {user2.display_name}"
            if not self_ship
            else f"{user1.display_name} × {user1.display_name}"
        )

        embed = discord.Embed(
            title=f"{title_emoji} {pair_title}",
            color=_score_color(score),
        )

        # Pair field
        if self_ship:
            embed.add_field(
                name="Pair",
                value=f"{user1.mention} **×** {user1.mention} *(self-ship)*",
                inline=False,
            )
        else:
            embed.add_field(
                name="Pair",
                value=f"{user1.mention} **×** {user2.mention}",
                inline=False,
            )

        # Score + bar
        embed.add_field(
            name="Compatibility",
            value=_score_bar(score),
            inline=False,
        )

        # Couple name + tier
        embed.add_field(
            name="Couple tag",
            value=f"`{couple_name}`",
            inline=True,
        )
        embed.add_field(
            name="Ship tier",
            value=_score_tier(score),
            inline=True,
        )

        # Verdict
        embed.add_field(
            name="Mittens’ verdict",
            value=comment,
            inline=False,
        )

        # Image inside the embed (single message, single embed)
        embed.set_image(url="attachment://ship.png")

        embed.set_footer(text=f"Ship ID: #{ship_id:04d} • Results reset daily ❤️")
        embed.timestamp = datetime.datetime.utcnow()

        # Content pinging people
        if self_ship:
            content = f"💌 New **self-ship** just dropped: {user1.mention} × {user1.mention}"
        else:
            content = f"💌 New ship just dropped: {user1.mention} × {user2.mention}"

        await interaction.response.send_message(
            content=content,
            embed=embed,
            file=ship_file,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MittensShipping(bot))
