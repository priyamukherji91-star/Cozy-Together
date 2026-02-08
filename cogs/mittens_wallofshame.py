# -*- coding: utf-8 -*-
import re
import random
import textwrap
import discord
from discord.ext import commands
from discord import app_commands

# ── CONFIG ─────────────────────────────────────────────────────────
WALL_CHANNEL_ID = 1426306248042614906   # wall-of-shame
WALL_CHANNEL_NAME = "wall-of-shame"     # fallback by name
FRESH_MEAT_ROLE_ID = 1435680183700160662

# ── FOOTER LINES (Mittens’ judgments) ───────────────────────────────
MITTENS_TAUNTS = [
    # Playfully menacing
    "Next time, try using your brain before your keyboard.",
    "Mittens has judged your message. Verdict: cringe.",
    "You’ve been scratched into history.",
    "The cat saw. The cat disapproved.",
    "Confiscated by order of Mittens.",
    "One meow closer to shame.",
    "Mittens found this too funny not to share.",
    "Shame fur you, pride for Mittens.",
    "You post; Mittens exposes.",
    "Mittens knocked this message off the table.",

    # Mock-serious
    "Recorded in the Annals of Embarrassment.",
    "Another case closed in the Court of Mittens.",
    "The defendant: guilty of posting that.",
    "Officially documented by the Menace herself.",
    "The jury of cats did not approve.",
    "Filed under: what were you thinking.",
    "Judgment delivered. Sentence: eternal meowmockery.",

    # Meme / sass
    "Bro thought this was a good idea 💀",
    "You posted that?",
    "Instant regret. Courtesy of Mittens.",
    "Caught lacking in 4K.",
    "Shame speedrun completed.",
    "Mittens clipped this for evidence.",
    "Should’ve stayed in drafts.",
    "The audacity is purring.",
]

MESSAGE_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)"
)

# ── HELPERS ────────────────────────────────────────────────────────
def can_shame(member: discord.Member) -> bool:
    """Everyone except Fresh Meat can shame. Admins always can."""
    if getattr(member.guild_permissions, "administrator", False):
        return True
    return FRESH_MEAT_ROLE_ID not in {r.id for r in member.roles}

def pick_wall_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Find wall-of-shame channel by ID or name."""
    ch = guild.get_channel(WALL_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        return ch
    for c in guild.text_channels:
        if c.name == WALL_CHANNEL_NAME:
            return c
    return None

def emphasize_block(text: str) -> str:
    """Pretty code-block style quote."""
    trimmed = textwrap.shorten(text, width=1800, placeholder=" …")
    return f"```\n{trimmed}\n```"

def random_taunt(name: str | None = None) -> str:
    return random.choice(MITTENS_TAUNTS).replace("{name}", name or "this one")

def build_embed(msg: discord.Message, footer_text: str, reporter_name: str | None = None) -> discord.Embed:
    desc_parts = []
    if msg.content:
        desc_parts.append(emphasize_block(msg.content))
    if msg.attachments:
        files_list = ", ".join(a.filename for a in msg.attachments[:3])
        if len(msg.attachments) > 3:
            files_list += f" (+{len(msg.attachments)-3} more)"
        desc_parts.append(f"*Attachments:* {files_list}")

    e = discord.Embed(
        title="🐾 Mittens’ Evidence Locker",
        description="\n".join(desc_parts) if desc_parts else "*No text content.*",
        color=discord.Color.purple(),
        timestamp=msg.created_at,
    )
    e.add_field(name="Suspect", value=msg.author.display_name, inline=True)
    e.add_field(name="Channel", value=msg.channel.mention, inline=True)
    if reporter_name:
        e.add_field(name="Reporter", value=reporter_name, inline=True)
    e.add_field(name="Jump", value=f"[Go to message]({msg.jump_url})", inline=False)

    e.set_footer(text=footer_text)
    if msg.author.display_avatar:
        e.set_thumbnail(url=msg.author.display_avatar.url)
    return e

async def send_to_wall(
    guild: discord.Guild,
    target: discord.Message,
    custom_footer: str | None = None,
    reporter_name: str | None = None,
    ping_user: bool = True,
):
    """Send the selected message to #wall-of-shame."""
    channel = pick_wall_channel(guild)
    if not channel:
        raise RuntimeError(f"Can't find #{WALL_CHANNEL_NAME}. Check WALL_CHANNEL_ID.")
    footer = (custom_footer or random_taunt(target.author.display_name)).strip()
    embed = build_embed(target, footer, reporter_name=reporter_name)

    await channel.send(
        content=target.author.mention if ping_user else target.author.display_name,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            everyone=False, users=ping_user, roles=False, replied_user=False
        ),
    )

# ── CONTEXT MENU ──────────────────────────────────────────────────
@app_commands.context_menu(name="Send to Wall")
async def send_to_wall_ctx(interaction: discord.Interaction, message: discord.Message):
    """Right-click → Apps → Send to Wall (guild only)."""
    if interaction.guild is None:
        return await interaction.response.send_message("Guild only.", ephemeral=True)

    member = interaction.user
    if not isinstance(member, discord.Member) or not can_shame(member):
        return await interaction.response.send_message(
            "Fresh Meat can’t send to the Wall 🐾", ephemeral=True
        )

    try:
        await send_to_wall(
            interaction.guild,
            message,
            reporter_name=interaction.user.display_name,
            ping_user=True,
        )
        await interaction.response.send_message("Sent to the Wall 🐾", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Couldn’t send it: `{e}`", ephemeral=True)

# ── COG WITH SLASH COMMAND ─────────────────────────────────────────
class MittensWallSlash(commands.Cog):
    """Slash helper to send a message link to Mittens’ Evidence Locker."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="shame",
        description="Send a message link to #wall-of-shame (optionally ping the author)."
    )
    @app_commands.describe(
        link="Paste a Discord message link",
        taunt="Optional custom footer (otherwise Mittens picks one).",
        ping="Ping the user? (default: true)"
    )
    async def shame_slash(
        self,
        interaction: discord.Interaction,
        link: str,
        taunt: str | None = None,
        ping: bool = True
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        member = interaction.user
        if not isinstance(member, discord.Member) or not can_shame(member):
            return await interaction.response.send_message(
                "Fresh Meat can’t send to the Wall 🐾", ephemeral=True
            )

        m = MESSAGE_LINK_RE.search(link)
        if not m:
            return await interaction.response.send_message(
                "That doesn’t look like a message link.", ephemeral=True
            )

        try:
            channel_id = int(m.group(1)); message_id = int(m.group(2))
            ch = interaction.guild.get_channel(channel_id)
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                return await interaction.response.send_message("Can't access that channel.", ephemeral=True)
            target = await ch.fetch_message(message_id)
        except Exception:
            return await interaction.response.send_message("Couldn't fetch that message.", ephemeral=True)

        try:
            footer = (taunt or random_taunt(target.author.display_name))
            await send_to_wall(
                interaction.guild,
                target,
                custom_footer=footer,
                reporter_name=interaction.user.display_name,
                ping_user=ping,
            )
            await interaction.response.send_message("Sent to the Wall 🐾", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Couldn’t send it: `{e}`", ephemeral=True)

async def setup(bot: commands.Bot):
    # Load cog & add context menu
    await bot.add_cog(MittensWallSlash(bot))
    bot.tree.add_command(send_to_wall_ctx)

    # Force guild-scoped sync everywhere the bot is.
    # This makes context menu + slash appear instantly in all servers.
    for g in bot.guilds:
        try:
            await bot.tree.sync(guild=discord.Object(id=g.id))
        except Exception:
            # Don't hard-fail setup if one guild errors
            pass
