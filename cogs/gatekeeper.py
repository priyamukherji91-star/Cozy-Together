# -*- coding: utf-8 -*-
"""
Gatekeeper Cog — Landing Zone ✅ -> role grant

• /setup_gate — posts (or re-posts) the landing-zone message and seeds ✅
• on_raw_reaction_add — when a user reacts ✅ in landing-zone:
      -> add Cozy Gremlins
      -> remove Fresh Meat
"""

import discord
from discord.ext import commands
from discord import app_commands

# ── IDs ─────────────────────────────────────────
GUILD_ID             = 1425974791516586045
LANDING_ZONE_ID      = 1428313417244213298
FRESH_MEAT_ID        = 1435680183700160662
COZY_GREMLINS_ID     = 1425978340304621769

ANNOUNCE_CHANNEL_ID  = 1440723935220994048

MAMA_CAT_ROLE_NAME = "Mama Cat"
GHOUL_ROLE_NAME    = "Ghoul"

ACCEPT_EMOJIS = {"✅", "☑️", "✔️"}

GATE_TITLE = "Welcome to Cozy Together"
GATE_TEXT = (
    "Read **da-rulez** and then react with ✅ here to enter.\n\n"
    "**You’ll be granted _Cozy Gremlins_ and _Fresh Meat_ will be removed.**\n"
    "If you change your mind later, ask a mod."
)

# ──────────────────────────────────────────────
def _member_has_power(member: discord.Member) -> bool:
    names = {r.name for r in member.roles}
    return (
        MAMA_CAT_ROLE_NAME in names
        or GHOUL_ROLE_NAME in names
        or member.guild_permissions.administrator
    )

class Gatekeeper(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ────────────── JOIN (embed + avatar) ──────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or member.guild.id != GUILD_ID:
            return

        ch = member.guild.get_channel(ANNOUNCE_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return

        num = member.guild.member_count or 0

        embed = discord.Embed(
            title=member.display_name,
            description=(
                f"**Welcome, {member.mention}! ✨**\n\n"
                f"You are member **#{num}** to join\n"
                f"Make yourself at home and get cozy."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Cozy Together")

        # ✅ avatar thumbnail
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    # ────────────── LEAVE (embed, no mention) ──────────────
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            if not member.guild or member.guild.id != GUILD_ID:
                return
            if getattr(member, "bot", False):
                return
        except Exception:
            return

        ch = member.guild.get_channel(ANNOUNCE_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return

        name = getattr(member, "display_name", None) or getattr(member, "name", "Someone")

        embed = discord.Embed(
            title=name,
            description=f"**{name} left the server**",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Cozy Together")

        try:
            await ch.send(embed=embed)
        except Exception:
            pass

    # ────────────── /setup_gate ──────────────
    @app_commands.command(name="setup_gate", description="Recreate the landing-zone gate message with ✅.")
    async def setup_gate(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild-only command.", ephemeral=True)

        if not _member_has_power(interaction.user):
            return await interaction.response.send_message("You don’t have paws for this. 🐾", ephemeral=True)

        channel = interaction.guild.get_channel(LANDING_ZONE_ID)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Landing-zone channel not found.", ephemeral=True)

        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.InteractionResponded:
            pass

        embed = discord.Embed(
            title=GATE_TITLE,
            description=GATE_TEXT,
            color=0x2ecc71
        )
        embed.set_footer(text="React with ✅ below")

        msg = await channel.send(embed=embed)
        try:
            await msg.add_reaction("✅")
        except Exception:
            pass

        await interaction.followup.send(f"Gate message posted in {channel.mention}.", ephemeral=True)

    # ────────────── REACTION GATE ──────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id != GUILD_ID or payload.channel_id != LANDING_ZONE_ID:
            return

        if str(payload.emoji) not in ACCEPT_EMOJIS:
            return

        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return

        if member.bot:
            return

        cozy = guild.get_role(COZY_GREMLINS_ID)
        fresh = guild.get_role(FRESH_MEAT_ID)
        if not cozy:
            return

        me = guild.me
        if not me or not me.guild_permissions.manage_roles or me.top_role <= cozy:
            return

        try:
            if fresh and fresh in member.roles:
                await member.remove_roles(fresh, reason="Accepted rules ✅ (gate)")
            if cozy not in member.roles:
                await member.add_roles(cozy, reason="Accepted rules ✅ (gate)")
        except Exception:
            return

# ──────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(Gatekeeper(bot))
