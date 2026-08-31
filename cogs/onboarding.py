# cogs/onboarding.py
# -*- coding: utf-8 -*-

import logging
from dataclasses import dataclass, asdict, fields
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

from common import storage

LOG = logging.getLogger("cozy.onboarding")

# ── IDs ───────────────────────────────────────────────────────────
GUILD_ID = 1425974791516586045
GET_ROLES_CHANNEL_ID = 1426292897321455779
ACTIVITY_PING_CHANNEL_ID = 1444407439016595487

# ── Core roles ────────────────────────────────────────────────────
FRESH_MEAT_ID = 1435680183700160662
COZY_GREMLINS_ID = 1425978340304621769

# ── Pronouns ──────────────────────────────────────────────────────
HE_HIM_ID = 1426292898025967667
SHE_HER_ID = 1426292898474758336
THEY_THEM_ID = 1426292898793783357

# ── DM preferences ────────────────────────────────────────────────
OPEN_DM_ID = 1435676962315309136
NO_DM_ID = 1435677043143741540

# ── Server dropdown ───────────────────────────────────────────────
SERVER_ROLE_ENTRIES = [
    ("Cerberus", 1426296610312425472),
    ("Louisoix", 1426297034922659972),
    ("Moogle", 1426297089243091014),
    ("Omega", 1426297140866584728),
    ("Phantom", 1426297204548829205),
    ("Ragnarok", 1426297277231665364),
    ("Sagittarius", 1426297341178282087),
    ("Spriggan", 1426297387508302014),
    ("Alpha", 1483873328987897997),
    ("Lich", 1483873463226859600),
    ("Odin", 1483873525738508453),
    ("Phoenix", 1483873559502913749),
    ("Raiden", 1483873596714778645),
    ("Shiva", 1483874071874638019),
    ("Twintania", 1483873638267490474),
    ("Zodiark", 1483873702528549077),
]

# ── Activity pings ────────────────────────────────────────────────
ACTIVITY_ROLE_ENTRIES = [
    ("Mount Farms (EX)", 1444408153285333033),
    ("Deep Dungeons", 1444408253340586137),
    ("Treasure Maps", 1444408302778843269),
    ("Venue Enthusiasts", 1444408362082107393),
    ("Daily Roulettes", 1444408610485436566),
    ("Unreal", 1452374560694472799),
]

_ACTIVITY_ROLE_MAP = {name: role_id for name, role_id in ACTIVITY_ROLE_ENTRIES}

# ── Persistence ───────────────────────────────────────────────────
# This used to be a relative Path("data"), which is inside the container rather
# than on the volume — so every deploy forgot which messages carried the role
# reactions, and the posts went inert until they were set up again.
CONFIG_PATH = storage.DATA_DIR / "onboarding_config.json"


@dataclass
class RoleMessageConfig:
    pronouns_message_id: Optional[int] = None
    dms_message_id: Optional[int] = None
    server_message_id: Optional[int] = None
    activity_message_id: Optional[int] = None

    @classmethod
    def load(cls):
        raw = storage.load_json(CONFIG_PATH, default=None)
        if not isinstance(raw, dict):
            return cls()
        # Ignore keys we no longer know about rather than blowing up on them.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self):
        storage.save_json(CONFIG_PATH, asdict(self))


# ── Shared role helpers ───────────────────────────────────────────
def _get_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    return guild.get_role(role_id)


async def _toggle_role(
    interaction: discord.Interaction,
    *,
    role_id: int,
    label: str,
) -> None:
    guild = interaction.guild
    member = interaction.user

    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return

    role = _get_role(guild, role_id)
    if role is None:
        await interaction.response.send_message(f"Role for **{label}** is missing.", ephemeral=True)
        return

    if role in member.roles:
        await member.remove_roles(role, reason="Self-assigned role toggle")
        await interaction.response.send_message(f"Removed **{label}**.", ephemeral=True)
        return

    await member.add_roles(role, reason="Self-assigned role toggle")
    await interaction.response.send_message(f"Added **{label}**.", ephemeral=True)


async def _toggle_dm_role(
    interaction: discord.Interaction,
    *,
    add_role_id: int,
    remove_role_id: int,
    label: str,
) -> None:
    guild = interaction.guild
    member = interaction.user

    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return

    add_role = _get_role(guild, add_role_id)
    remove_role = _get_role(guild, remove_role_id)

    if add_role is None:
        await interaction.response.send_message(f"Role for **{label}** is missing.", ephemeral=True)
        return

    if add_role in member.roles:
        await member.remove_roles(add_role, reason="Self-removed DM preference")
        await interaction.response.send_message(f"Removed **{label}**.", ephemeral=True)
        return

    roles_to_add = [add_role]
    roles_to_remove = [remove_role] if remove_role and remove_role in member.roles else []

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="Swapped DM preference")
    await member.add_roles(*roles_to_add, reason="Self-assigned DM preference")
    await interaction.response.send_message(f"Added **{label}**.", ephemeral=True)


# ── Server dropdown view ──────────────────────────────────────────
class ServerSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=name, value=str(role_id)) for name, role_id in SERVER_ROLE_ENTRIES]
        super().__init__(
            placeholder="Choose your server…",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="server_select_v1",
        )
        self.role_ids = [role_id for _, role_id in SERVER_ROLE_ENTRIES]

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        chosen = guild.get_role(int(self.values[0]))
        if not chosen:
            return await interaction.response.send_message("Role missing.", ephemeral=True)

        for rid in self.role_ids:
            role = guild.get_role(rid)
            if role and role in member.roles and role != chosen:
                await member.remove_roles(role, reason="Changed self-selected server")

        if chosen not in member.roles:
            await member.add_roles(chosen, reason="Selected server role")

        await interaction.response.send_message(f"Server set to **{chosen.name}**.", ephemeral=True)


class ServerSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ServerSelect())


# ── Pronoun buttons ───────────────────────────────────────────────
class PronounView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="He/Him", style=discord.ButtonStyle.primary, custom_id="roles_pronoun_hehim", row=0)
    async def he_him(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=HE_HIM_ID, label="He/Him")

    @discord.ui.button(label="She/Her", style=discord.ButtonStyle.primary, custom_id="roles_pronoun_sheher", row=0)
    async def she_her(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=SHE_HER_ID, label="She/Her")

    @discord.ui.button(label="They/Them", style=discord.ButtonStyle.primary, custom_id="roles_pronoun_theythem", row=0)
    async def they_them(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=THEY_THEM_ID, label="They/Them")


# ── DM preference buttons ─────────────────────────────────────────
class DMPreferenceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open for DMs", style=discord.ButtonStyle.success, custom_id="roles_dm_open", row=0)
    async def open_dms(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_dm_role(
            interaction,
            add_role_id=OPEN_DM_ID,
            remove_role_id=NO_DM_ID,
            label="Open for DMs",
        )

    @discord.ui.button(label="No DMs", style=discord.ButtonStyle.danger, custom_id="roles_dm_closed", row=0)
    async def no_dms(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_dm_role(
            interaction,
            add_role_id=NO_DM_ID,
            remove_role_id=OPEN_DM_ID,
            label="No DMs",
        )


# ── Activity ping buttons ─────────────────────────────────────────
class ActivityPingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mount Farms (EX)", style=discord.ButtonStyle.secondary, custom_id="roles_activity_mountfarms", row=0)
    async def mount_farms(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=_ACTIVITY_ROLE_MAP["Mount Farms (EX)"], label="Mount Farms (EX)")

    @discord.ui.button(label="Deep Dungeons", style=discord.ButtonStyle.secondary, custom_id="roles_activity_deepdungeons", row=0)
    async def deep_dungeons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=_ACTIVITY_ROLE_MAP["Deep Dungeons"], label="Deep Dungeons")

    @discord.ui.button(label="Treasure Maps", style=discord.ButtonStyle.secondary, custom_id="roles_activity_treasuremaps", row=0)
    async def treasure_maps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=_ACTIVITY_ROLE_MAP["Treasure Maps"], label="Treasure Maps")

    @discord.ui.button(label="Venue Enthusiasts", style=discord.ButtonStyle.secondary, custom_id="roles_activity_venueenthusiasts", row=1)
    async def venue_enthusiasts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=_ACTIVITY_ROLE_MAP["Venue Enthusiasts"], label="Venue Enthusiasts")

    @discord.ui.button(label="Daily Roulettes", style=discord.ButtonStyle.secondary, custom_id="roles_activity_dailyroulettes", row=1)
    async def daily_roulettes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=_ACTIVITY_ROLE_MAP["Daily Roulettes"], label="Daily Roulettes")

    @discord.ui.button(label="Unreal", style=discord.ButtonStyle.secondary, custom_id="roles_activity_unreal", row=1)
    async def unreal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_role(interaction, role_id=_ACTIVITY_ROLE_MAP["Unreal"], label="Unreal")


# ── Cog ───────────────────────────────────────────────────────────
class WelcomeSetup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = RoleMessageConfig.load()

    async def post_role_menus(self, guild: discord.Guild):
        """Post the four role menus in get-roles, replacing the last set.

        The previous four are deleted by the ids the config already stores, so
        pressing this twice leaves four menus rather than eight — the old ones
        keep working, but only one set is the one being maintained, and a member
        cannot tell them apart. Anything the ids no longer point at is left
        alone: a message somebody deleted by hand is not an error.
        """
        channel = guild.get_channel(GET_ROLES_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return None

        stale = [
            self.config.pronouns_message_id,
            self.config.server_message_id,
            self.config.dms_message_id,
            self.config.activity_message_id,
        ]

        # Pronouns
        p_embed = discord.Embed(
            title="Pick Your Pronouns",
            description="Click to add a pronoun role. Click again to remove it.",
            colour=discord.Colour.blurple(),
        )
        p_msg = await channel.send(embed=p_embed, view=PronounView())

        # Server dropdown
        s_embed = discord.Embed(
            title="Choose Your Server",
            description="Select exactly one FFXIV server.",
            colour=discord.Colour.green(),
        )
        s_msg = await channel.send(embed=s_embed, view=ServerSelectView())

        # DM preferences
        d_embed = discord.Embed(
            title="DM Preferences",
            description="Click to add a DM preference. Click again to remove it.",
            colour=discord.Colour.orange(),
        )
        d_msg = await channel.send(embed=d_embed, view=DMPreferenceView())

        # Activity pings
        activity_lines = [name for name, _ in ACTIVITY_ROLE_ENTRIES]
        a_embed = discord.Embed(
            title="Activity Pings",
            description=(
                "Click to add a ping role. Click again to remove it.\n"
                f"Only pingable in <#{ACTIVITY_PING_CHANNEL_ID}>.\n\n"
                + "\n".join(f"• {name}" for name in activity_lines)
            ),
            colour=discord.Colour.purple(),
        )
        a_msg = await channel.send(embed=a_embed, view=ActivityPingView())

        self.config.pronouns_message_id = p_msg.id
        self.config.server_message_id = s_msg.id
        self.config.dms_message_id = d_msg.id
        self.config.activity_message_id = a_msg.id
        self.config.save()

        # Last, so a failure while posting never leaves the channel with none.
        for message_id in stale:
            if not message_id:
                continue
            try:
                await (await channel.fetch_message(int(message_id))).delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            except Exception:
                LOG.debug("could not clear old role menu %s", message_id, exc_info=True)
        return channel

    @app_commands.command(name="setup_roles", description="Post all role selection messages in get-roles.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_roles(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        await interaction.response.send_message("⏳ Posting role messages…", ephemeral=True)
        channel = await self.post_role_menus(interaction.guild)
        if channel is None:
            return await interaction.edit_original_response(
                content="❌ get-roles channel not found."
            )
        await interaction.edit_original_response(
            content=f"✅ Role menus posted in {channel.mention}."
        )

    @app_commands.command(
        name="setup_panels",
        description="Repost the landing-zone gate and the get-roles menus, in their own channels.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_panels(self, interaction: discord.Interaction):
        """Both doors at once.

        They are two commands in two cogs because they are two channels, but
        they are one job — the way in — and doing half of it is how a landing
        zone ends up pointing at role menus that are no longer there. The gate
        is imported here rather than reached through its cog: `gatekeeper` may
        not have loaded, and one door posted is better than an error.
        """
        if interaction.guild is None:
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        await interaction.response.send_message("⏳ Reposting both…", ephemeral=True)
        done: list[str] = []
        failed: list[str] = []

        roles_channel = await self.post_role_menus(interaction.guild)
        (done if roles_channel else failed).append(
            f"Role menus in {roles_channel.mention}" if roles_channel
            else "get-roles channel not found"
        )

        try:
            from cogs.gatekeeper import post_gate  # noqa: PLC0415

            gate_channel = await post_gate(interaction.guild)
        except Exception:
            LOG.exception("could not post the gate")
            gate_channel = None
        (done if gate_channel else failed).append(
            f"Gate in {gate_channel.mention}" if gate_channel
            else "landing-zone gate could not be posted"
        )

        note = "\n".join(f"✅ {line}" for line in done)
        if failed:
            note += ("\n" if note else "") + "\n".join(f"❌ {line}" for line in failed)
        await interaction.edit_original_response(content=note)


async def setup(bot):
    bot.add_view(PronounView())
    bot.add_view(ServerSelectView())
    bot.add_view(DMPreferenceView())
    bot.add_view(ActivityPingView())
    await bot.add_cog(WelcomeSetup(bot))
