# cogs/onboarding.py
# -*- coding: utf-8 -*-

import json
import random
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord.ext import commands

# ── IDs ───────────────────────────────────────────────────────────
GUILD_ID = 1425974791516586045
GET_ROLES_CHANNEL_ID = 1426292897321455779
ACTIVITY_PING_CHANNEL_ID = 1444407439016595487

# ── Core roles ────────────────────────────────────────────────────
FRESH_MEAT_ID = 1435680183700160662
COZY_GREMLINS_ID = 1425978340304621769

# ── Pronouns ──────────────────────────────────────────────────────
HE_HIM_ID    = 1426292898025967667
SHE_HER_ID   = 1426292898474758336
THEY_THEM_ID = 1426292899032733716

EMOJI_HE   = "💙"
EMOJI_SHE  = "💖"
EMOJI_THEY = "💜"

# ── DM preferences ────────────────────────────────────────────────
OPEN_DM_ID = 1435676962315309136
NO_DM_ID   = 1435677043143741540

EMOJI_OPEN = "✅"
EMOJI_NO   = "❌"

# ── Server dropdown ───────────────────────────────────────────────
SERVER_ROLE_ENTRIES = [
    ("Cerberus",    1426296610312425472),
    ("Louisoix",    1426297034922659972),
    ("Moogle",      1426297089243091014),
    ("Omega",       1426297140866584728),
    ("Phantom",     1426297204548829205),
    ("Ragnarok",    1426297277231665364),
    ("Sagittarius", 1426297341178282087),
    ("Spriggan",    1426297387508302014),

    ("Alpha",       1483873328987897997),
    ("Lich",        1483873463226859600),
    ("Odin",        1483873525738508453),
    ("Phoenix",     1483873559502913749),
    ("Raiden",      1483873596714778645),
    ("Shiva",       1483874071874638019),
    ("Twintania",   1483873638267490474),
    ("Zodiark",     1483873702528549077),
]

# ── Activity pings ────────────────────────────────────────────────
ACTIVITY_ROLE_ENTRIES = [
    ("Mount Farms (EX)",   1444408153285333033),
    ("Deep Dungeons",      1444408253340586137),
    ("Treasure Maps",      1444408302778843269),
    ("Venue Enthusiasts",  1444408362082107393),
    ("Daily Roulettes",    1444408610485436566),
    ("Unreal",             1452374560694472799),
]

FIXED_ACTIVITY_EMOJIS = {
    "Unreal": "⚔️",
}

ACTIVITY_EMOJI_POOL = ["🐎", "🕳️", "🗺️", "🏰", "🎲", "🎭", "📯", "💫"]

# ── Persistence ───────────────────────────────────────────────────
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = DATA_DIR / "onboarding_config.json"


@dataclass
class RoleMessageConfig:
    pronouns_message_id: Optional[int] = None
    dms_message_id: Optional[int] = None
    server_message_id: Optional[int] = None
    activity_message_id: Optional[int] = None
    activity_emoji_map: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls):
        if CONFIG_PATH.exists():
            return cls(**json.loads(CONFIG_PATH.read_text()))
        return cls()

    def save(self):
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))


# ── Server dropdown view ──────────────────────────────────────────
class ServerSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=n, value=str(r)) for n, r in SERVER_ROLE_ENTRIES]
        super().__init__(
            placeholder="Choose your server…",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="server_select_v1",
        )
        self.role_ids = [r for _, r in SERVER_ROLE_ENTRIES]

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        chosen = guild.get_role(int(self.values[0]))
        if not chosen:
            return await interaction.response.send_message("Role missing.", ephemeral=True)

        for rid in self.role_ids:
            role = guild.get_role(rid)
            if role and role in member.roles and role != chosen:
                await member.remove_roles(role)

        if chosen not in member.roles:
            await member.add_roles(chosen)

        await interaction.response.send_message(
            f"Server set to **{chosen.name}**.", ephemeral=True
        )


class ServerSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ServerSelect())


# ── Cog ───────────────────────────────────────────────────────────
class WelcomeSetup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = RoleMessageConfig.load()

    def _pronoun_map(self):
        return {EMOJI_HE: HE_HIM_ID, EMOJI_SHE: SHE_HER_ID, EMOJI_THEY: THEY_THEM_ID}

    def _dms_map(self):
        return {EMOJI_OPEN: OPEN_DM_ID, EMOJI_NO: NO_DM_ID}

    # ── ONE COMMAND: POST EVERYTHING ──────────────────────────────
    @commands.command(name="setup_roles")
    @commands.has_permissions(manage_guild=True)
    async def setup_roles(self, ctx):
        channel = ctx.guild.get_channel(GET_ROLES_CHANNEL_ID)
        if not channel:
            return await ctx.send("❌ get-roles channel not found.")

        await ctx.send("⏳ Posting role messages…")

        # Pronouns
        p_embed = discord.Embed(
            title="Pick Your Pronouns",
            description=f"{EMOJI_HE} He/Him\n{EMOJI_SHE} She/Her\n{EMOJI_THEY} They/Them",
            colour=discord.Colour.blurple(),
        )
        p_msg = await channel.send(embed=p_embed)
        for e in self._pronoun_map():
            await p_msg.add_reaction(e)

        # Server dropdown
        s_embed = discord.Embed(
            title="Choose Your Server",
            description="Select exactly one FFXIV server.",
            colour=discord.Colour.green(),
        )
        s_msg = await channel.send(embed=s_embed, view=ServerSelectView())

        # DM prefs
        d_embed = discord.Embed(
            title="DM Preferences",
            description=f"{EMOJI_OPEN} Open for DMs\n{EMOJI_NO} No DMs",
            colour=discord.Colour.orange(),
        )
        d_msg = await channel.send(embed=d_embed)
        for e in self._dms_map():
            await d_msg.add_reaction(e)

        # Activity pings
        pool = ACTIVITY_EMOJI_POOL.copy()
        random.shuffle(pool)

        emoji_map = {}
        lines = []

        for name, role_id in ACTIVITY_ROLE_ENTRIES:
            emoji = FIXED_ACTIVITY_EMOJIS.get(name) or pool.pop(0)
            emoji_map[emoji] = role_id
            lines.append(f"{emoji} {name}")

        a_embed = discord.Embed(
            title="Activity Pings",
            description=(
                f"React to get ping roles.\n"
                f"Only pingable in <#{ACTIVITY_PING_CHANNEL_ID}>.\n\n"
                + "\n".join(lines)
            ),
            colour=discord.Colour.purple(),
        )
        a_msg = await channel.send(embed=a_embed)
        for e in emoji_map:
            await a_msg.add_reaction(e)

        self.config.pronouns_message_id = p_msg.id
        self.config.server_message_id = s_msg.id
        self.config.dms_message_id = d_msg.id
        self.config.activity_message_id = a_msg.id
        self.config.activity_emoji_map = emoji_map
        self.config.save()

        await ctx.send("✅ Role setup complete.")

    # ── REACTION ADD ──────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id != GUILD_ID:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if not member:
            return

        emoji = str(payload.emoji)

        # Pronouns
        if payload.channel_id == GET_ROLES_CHANNEL_ID:
            pmap = self._pronoun_map()
            if emoji in pmap:
                role = guild.get_role(pmap[emoji])
                if role:
                    await member.add_roles(role)
                return

        # DM prefs (exclusive)
        if payload.message_id == self.config.dms_message_id:
            dmap = self._dms_map()
            if emoji in dmap:
                add = guild.get_role(dmap[emoji])
                remove = guild.get_role(NO_DM_ID if dmap[emoji] == OPEN_DM_ID else OPEN_DM_ID)
                if remove and remove in member.roles:
                    await member.remove_roles(remove)
                if add and add not in member.roles:
                    await member.add_roles(add)
                return

        # Activity pings
        if payload.message_id == self.config.activity_message_id:
            role_id = self.config.activity_emoji_map.get(emoji)
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    await member.add_roles(role)

    # ── REACTION REMOVE ───────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id != GUILD_ID:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if not member:
            return

        emoji = str(payload.emoji)

        # Pronouns
        pmap = self._pronoun_map()
        if payload.channel_id == GET_ROLES_CHANNEL_ID and emoji in pmap:
            role = guild.get_role(pmap[emoji])
            if role and role in member.roles:
                await member.remove_roles(role)
            return

        # DM prefs
        if payload.message_id == self.config.dms_message_id:
            dmap = self._dms_map()
            if emoji in dmap:
                role = guild.get_role(dmap[emoji])
                if role and role in member.roles:
                    await member.remove_roles(role)
                return

        # Activity pings
        if payload.message_id == self.config.activity_message_id:
            role_id = self.config.activity_emoji_map.get(emoji)
            if role_id:
                role = guild.get_role(role_id)
                if role and role in member.roles:
                    await member.remove_roles(role)


async def setup(bot):
    bot.add_view(ServerSelectView())
    await bot.add_cog(WelcomeSetup(bot))