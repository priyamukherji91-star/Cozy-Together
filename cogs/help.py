# cogs/help.py
# -*- coding: utf-8 -*-
"""`/help` — what a member can actually do.

The bot has ~34 commands across 17 cogs and, before this, no way to see any of
them. The presence line advertised `/help` (bot.py) while the only thing that
answered was discord.py's default `!help`, which lists the five prefix commands
— ping, fixtest, register, statusnow, whoami — and none of the slash ones. A
new member ran the advertised command and concluded the bot did nearly nothing.

Two rules shape what's in here:

1. **Only what the caller can use.** Roughly two thirds of the commands are
   gated to mods, admins, or the bot owner. Listing those teaches members the
   bot is mostly off-limits and invites refusals. `/shame` and `/event` are
   role-gated rather than permission-gated, so they're decided per-caller
   against the same role IDs their own cogs check.
2. **The commands aren't the whole bot.** The newspaper, the birthday posts,
   the link fixing and the reaction roles have no command at all, so no command
   list would ever reveal them. They get their own section.

The gates elsewhere are inline `if` checks inside command bodies rather than
decorators, so they can't be introspected — this list is hand-maintained and
will drift when commands are added. Keep it in step.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

# Imported rather than re-declared: these IDs decide who sees what, and a copy
# here would silently disagree the day one of them moves.
from cogs.admin_panel import PANEL_CHANNEL_ID, may_open
from cogs.events import ALLOWED_ROLE_IDS as EVENT_ROLE_IDS
from cogs.member_cards import MEMBER_CARD_CHANNEL_ID
from cogs.mittens_wallofshame import FRESH_MEAT_ROLE_ID
from cogs.morning_news import LIVE_POST_CHANNEL_ID
from cogs.onboarding import GET_ROLES_CHANNEL_ID
from cogs.pet_care import PET_CARE_CHANNEL_ID, PET_PHOTO_CHANNEL_ID

LOG = logging.getLogger(__name__)

# The bot-commands channel — the one !register and !whoami already live in.
HELP_CHANNEL_ID = MEMBER_CARD_CHANNEL_ID


def _roles_of(user: discord.abc.User) -> set[int]:
    return {r.id for r in getattr(user, "roles", [])}


def _can_shame(user: discord.abc.User) -> bool:
    """Mirrors `mittens_wallofshame.can_shame`: everyone but Fresh Meat."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    return FRESH_MEAT_ROLE_ID not in _roles_of(user)


def _can_event(user: discord.abc.User) -> bool:
    """Mirrors the role check at the top of `events.event`."""
    return bool(_roles_of(user) & EVENT_ROLE_IDS)


def _is_staff(user: discord.abc.User) -> bool:
    """Whoever the staff panel would open for — asked rather than restated."""
    return isinstance(user, discord.Member) and may_open(user)


class Help(commands.Cog):
    """One command, one ephemeral answer."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _build(self, user: discord.abc.User) -> discord.Embed:
        embed = discord.Embed(
            title="What Mittens can do for you",
            description=(
                "Everything below is yours to use. She keeps the rest to herself.\n"
                "This message is only visible to you."
            ),
            colour=discord.Color.blurple(),
        )

        embed.add_field(
            name="🐾 Pets",
            value=(
                f"The panel in <#{PET_CARE_CHANNEL_ID}> is the main way in — feed, play, "
                "the dex, the board, and your own pets, all from its buttons.\n"
                "`/pet add` — register a pet with a photo\n"
                "`/pet list` — every pet in the server\n"
                "`/pet remove` — remove one of yours\n"
                f"Or right-click a photo you posted in <#{PET_PHOTO_CHANNEL_ID}> → "
                "**Apps** → **This is my pet**"
            ),
            inline=False,
        )

        embed.add_field(
            name="🎂 Birthdays",
            value="`/birthday set` — tell her the day, and she'll say so on the morning.",
            inline=False,
        )

        embed.add_field(
            name="⚔️ FFXIV",
            value=(
                "`/resets next` — when the daily and weekly resets land\n"
                "`/resets countdown` — the short version\n"
                "`!register` — link your Lodestone character and get a card\n"
                "`!whoami` — show your card again\n"
                "*(those last two start with `!`, not `/`)*"
            ),
            inline=False,
        )

        fun = [
            "`/ship` — ship two members",
            "`/shiprandom` — ship two at random",
            "`/shipwithrandom` — ship someone with a random victim",
            "`/shippet` — ship a pet with a member, or with another pet",
        ]
        if _can_shame(user):
            fun.append("`/shame` — send a message link to the wall of shame")
        embed.add_field(name="💞 Fun", value="\n".join(fun), inline=False)

        if _can_event(user):
            embed.add_field(
                name="🗓️ Events",
                value=(
                    "`/event` — create a scheduled event, a forum thread for it, "
                    "and the announcement, in one go."
                ),
                inline=False,
            )

        # Staff only, and only a pointer: the panel explains itself once it's
        # open, and the whole reason it exists is that nobody should have to
        # read a list of twenty staff commands anywhere.
        if _is_staff(user):
            embed.add_field(
                name="😾 Staff",
                value=(
                    f"The panel in <#{PANEL_CHANNEL_ID}> holds every staff command "
                    "there is — moderation, the paper, the pets, his face. "
                    "`/adminpanel` puts it back if it goes missing."
                ),
                inline=False,
            )

        embed.add_field(
            name="✨ …and things she does unasked",
            value=(
                f"• A newspaper every morning in <#{LIVE_POST_CHANNEL_ID}>, written from "
                "what you all said the day before.\n"
                "• Birthday announcements, on the day.\n"
                "• FFXIV reset reminders, daily and weekly.\n"
                "• X, Twitter and Instagram links reposted so the embeds actually work.\n"
                f"• Reaction roles in <#{GET_ROLES_CHANNEL_ID}> — pronouns, DMs, and pings."
            ),
            inline=False,
        )

        embed.set_footer(text="Ask a mod if something here refuses you 🐾")
        return embed

    @app_commands.command(name="help", description="What Mittens can do for you 🐾")
    @app_commands.guild_only()
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None or interaction.channel.id != HELP_CHANNEL_ID:
            return await interaction.response.send_message(
                f"Use this in <#{HELP_CHANNEL_ID}>. Mittens is very strict and deeply annoying.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=self._build(interaction.user), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
