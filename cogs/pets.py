# cogs/pets.py
# -*- coding: utf-8 -*-
"""Registering pets, feeding them, and the scoreboards that come out of it.

Members register their own pets with a photo and everyone can feed them. Photos
are stored as bytes on disk by petcare.storage — never as Discord attachment
URLs, which expire.

Channel rules: registering happens in the pet photos channel, because that is
where the photos already are. Everything else is bot-commands only.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import logging
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from petcare import storage as store

LOG = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
GUILD_ID = 1425974791516586045

REGISTER_CHANNEL_ID = 1427657614061207724   # pet photos: /pet add + right-click claim
COMMANDS_CHANNEL_ID = 1436115021066408016   # bot commands: everything else

# When the daily treat allowance rolls over (matches morning_news.py).
TIMEZONE = ZoneInfo("Europe/Brussels")

CLAIM_MENU_NAME = "This is my pet"
ACCENT = discord.Colour(0xE0708A)

# Never let a pet name or a display name ping a role or @everyone.
MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)

# Hunger nudge: posts once a day, and only when a pet has genuinely been
# forgotten. Set HUNGER_DAYS = 0 to switch it off entirely.
HUNGER_DAYS = 7
MAX_NUDGE_PETS = 4
NUDGE_TIME = datetime.time(hour=18, minute=30, tzinfo=TIMEZONE)


def _is_mod(user: discord.User | discord.Member) -> bool:
    """Who may remove someone else's pet."""
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_messages


def _display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else "someone who left"


class ClaimPetModal(discord.ui.Modal):
    """Right-click -> Apps -> This is my pet. Only the name is left to ask for."""

    def __init__(self, cog: "Pets", image: discord.Attachment) -> None:
        super().__init__(title="Register this pet")
        self.cog = cog
        self.image = image
        self.pet_name = discord.ui.TextInput(
            label="What is the pet called?",
            max_length=store.MAX_NAME_LENGTH,
            required=True,
        )
        self.add_item(self.pet_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            raw = await self.image.read()
        except Exception:
            LOG.exception("[pets] could not read claimed attachment")
            await interaction.followup.send("That photo could not be downloaded.", ephemeral=True)
            return
        await self.cog._register(interaction, str(self.pet_name), raw)


class Pets(commands.Cog):
    """Pet registration, feeding and scoreboards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Context menus can't use decorators inside a Cog, so build it by hand.
        self._ctx_menu: app_commands.ContextMenu | None = app_commands.ContextMenu(
            name=CLAIM_MENU_NAME, callback=self.claim_pet_from_message
        )
        try:
            bot.tree.add_command(self._ctx_menu)
        except Exception:
            # Losing the right-click entry beats losing the whole cog.
            LOG.exception("[pets] could not register the %r menu", CLAIM_MENU_NAME)

        if HUNGER_DAYS:
            self.hunger_nudge.start()

    async def cog_unload(self) -> None:
        self.hunger_nudge.cancel()
        if self._ctx_menu is not None:
            kind = self._ctx_menu.type
            self.bot.tree.remove_command(CLAIM_MENU_NAME, type=kind)
            self.bot.tree.remove_command(
                CLAIM_MENU_NAME, type=kind, guild=discord.Object(id=GUILD_ID)
            )
            self._ctx_menu = None

    # ──────────────────────────────────────────────────────────
    # Channel gates
    # ──────────────────────────────────────────────────────────
    async def _needs_channel(self, interaction: discord.Interaction, channel_id: int) -> bool:
        """True (and already answered) when the command was run in the wrong place."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return True
        if not interaction.channel or interaction.channel.id != channel_id:
            await interaction.response.send_message(
                f"This command belongs in <#{channel_id}>.", ephemeral=True
            )
            return True
        return False

    # ──────────────────────────────────────────────────────────
    # Shared helpers
    # ──────────────────────────────────────────────────────────
    async def _pet_choices(
        self, interaction: discord.Interaction, current: str, own_only: bool = False
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        pets = await asyncio.to_thread(store.all_pets, interaction.guild.id)
        if own_only and not _is_mod(interaction.user):
            pets = [p for p in pets if p.owner_id == interaction.user.id]

        needle = current.strip().casefold()
        out: list[app_commands.Choice[str]] = []
        for p in pets:
            if needle and needle not in p.name.casefold():
                continue
            label = f"{p.name} — {_display_name(interaction.guild, p.owner_id)}'s"
            out.append(app_commands.Choice(name=label[:100], value=p.pet_id))
            if len(out) == 25:
                break
        return out

    async def _resolve(self, guild: discord.Guild, token: str) -> store.Pet | None:
        """Accept an autocomplete value or a plain typed name."""
        pet = await asyncio.to_thread(store.get_pet, guild.id, token)
        if pet is None:
            pet = await asyncio.to_thread(store.find_by_name, guild.id, token)
        return pet

    async def _thumb(self, pet: store.Pet) -> discord.File | None:
        """The pet's photo as an attachment, or None if the file has gone missing."""
        raw = await asyncio.to_thread(store.image_bytes, pet)
        return discord.File(io.BytesIO(raw), filename="pet.png") if raw else None

    async def _register(self, interaction: discord.Interaction, name: str, raw: bytes) -> None:
        """Shared tail of both registration routes. Assumes the response is deferred."""
        assert interaction.guild is not None
        try:
            pet = await asyncio.to_thread(
                store.add_pet, interaction.guild.id, interaction.user.id, name, raw
            )
        except store.PetError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            LOG.exception("[pets] registration failed")
            await interaction.followup.send(
                "Something went wrong saving that. Try again in a moment.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"{pet.name} is registered",
            description=f"Feed them with `/feed` in <#{COMMANDS_CHANNEL_ID}>.",
            colour=ACCENT,
        )
        file = await self._thumb(pet)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────────────────────
    # /pet — add, list, remove
    # ──────────────────────────────────────────────────────────
    pet = app_commands.Group(name="pet", description="Register and manage your pets")

    @pet.command(name="add", description="Register one of your pets")
    @app_commands.describe(name="What the pet is called", photo="A picture of them")
    @app_commands.checks.cooldown(2, 30.0)
    async def pet_add(
        self, interaction: discord.Interaction, name: str, photo: discord.Attachment
    ) -> None:
        if await self._needs_channel(interaction, REGISTER_CHANNEL_ID):
            return
        if not (photo.content_type or "").startswith("image/"):
            await interaction.response.send_message(
                "That attachment needs to be an image.", ephemeral=True
            )
            return
        if photo.size > store.MAX_UPLOAD_BYTES:
            await interaction.response.send_message(
                "That photo is too big. Use something under 10 MB.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            raw = await photo.read()
        except Exception:
            LOG.exception("[pets] could not read attachment")
            await interaction.followup.send("That photo could not be downloaded.", ephemeral=True)
            return
        await self._register(interaction, name, raw)

    @pet.command(name="list", description="Every pet registered in the server")
    async def pet_list(self, interaction: discord.Interaction) -> None:
        if await self._needs_channel(interaction, COMMANDS_CHANNEL_ID):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        pets = await asyncio.to_thread(store.all_pets, interaction.guild.id)
        if not pets:
            await interaction.followup.send(
                f"No pets are registered yet. Add one with `/pet add` in "
                f"<#{REGISTER_CHANNEL_ID}>."
            )
            return

        lines = [
            f"**{p.name}** — {_display_name(interaction.guild, p.owner_id)}'s" for p in pets
        ]
        embed = discord.Embed(
            title="Registered pets", description="\n".join(lines[:40]), colour=ACCENT
        )
        if len(pets) > 40:
            embed.set_footer(text=f"and {len(pets) - 40} more")
        await interaction.followup.send(embed=embed)

    @pet.command(name="remove", description="Remove one of your pets")
    @app_commands.describe(pet="Which pet to remove")
    async def pet_remove(self, interaction: discord.Interaction, pet: str) -> None:
        if await self._needs_channel(interaction, COMMANDS_CHANNEL_ID):
            return
        assert interaction.guild is not None

        record = await self._resolve(interaction.guild, pet)
        if record is None:
            await interaction.response.send_message(
                "There is no pet registered by that name.", ephemeral=True
            )
            return
        if record.owner_id != interaction.user.id and not _is_mod(interaction.user):
            await interaction.response.send_message(
                "That pet is not yours.", ephemeral=True
            )
            return

        await interaction.response.defer()
        try:
            await asyncio.to_thread(store.remove_pet, interaction.guild.id, record.pet_id)
        except store.PetError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            LOG.exception("[pets] removal failed")
            await interaction.followup.send(
                "Something went wrong removing that. Try again in a moment.", ephemeral=True
            )
            return

        await interaction.followup.send(f"**{record.name}** has been removed.")

    @pet_remove.autocomplete("pet")
    async def _remove_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._pet_choices(interaction, current, own_only=True)

    # ── right-click -> Apps -> This is my pet ─────────────────
    async def claim_pet_from_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return
        if message.channel.id != REGISTER_CHANNEL_ID:
            await interaction.response.send_message(
                f"Register pets from photos in <#{REGISTER_CHANNEL_ID}>.", ephemeral=True
            )
            return

        # Own messages only. Otherwise anyone could claim someone else's pet.
        if message.author.id != interaction.user.id:
            await interaction.response.send_message(
                "You can only register a pet from a photo **you** posted.", ephemeral=True
            )
            return

        image = next(
            (a for a in message.attachments if (a.content_type or "").startswith("image/")), None
        )
        if image is None:
            await interaction.response.send_message(
                "That message has no image attached.", ephemeral=True
            )
            return
        if image.size > store.MAX_UPLOAD_BYTES:
            await interaction.response.send_message(
                "That photo is too big. Use something under 10 MB.", ephemeral=True
            )
            return

        await interaction.response.send_modal(ClaimPetModal(self, image))

    # ──────────────────────────────────────────────────────────
    # /feed
    # ──────────────────────────────────────────────────────────
    @app_commands.command(name="feed", description="Give one of your daily treats to a pet")
    @app_commands.describe(pet="Which pet — start typing a name")
    @app_commands.checks.cooldown(1, 3.0)
    async def feed_cmd(self, interaction: discord.Interaction, pet: str) -> None:
        if await self._needs_channel(interaction, COMMANDS_CHANNEL_ID):
            return
        assert interaction.guild is not None

        record = await self._resolve(interaction.guild, pet)
        if record is None:
            await interaction.response.send_message(
                "There is no pet registered by that name. Pick one from the list.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                store.feed, interaction.guild.id, interaction.user.id, record.pet_id, TIMEZONE
            )
        except store.PetError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            LOG.exception("[pets] feed failed")
            await interaction.followup.send(
                "Something went wrong. Try again in a moment.", ephemeral=True
            )
            return

        owner = interaction.guild.get_member(record.owner_id)
        mention_owner = owner is not None and owner.id != interaction.user.id
        embed = discord.Embed(
            title=f"{record.name} has been fed",
            description=(
                f"{interaction.user.mention} gave **{record.name}** a treat"
                + (f", {owner.mention}." if mention_owner else ".")
            ),
            colour=ACCENT,
        )
        if result.new_favourite:
            embed.add_field(
                name="New favourite human",
                value=f"{interaction.user.mention} is now **{record.name}**'s favourite human.",
                inline=False,
            )
        embed.set_footer(
            text=(
                f"{result.treats_left} treats left today · "
                f"{record.name} has {result.total} in total"
            )
        )

        file = await self._thumb(record)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")
            await interaction.followup.send(embed=embed, file=file, allowed_mentions=MENTIONS)
            return
        await interaction.followup.send(embed=embed, allowed_mentions=MENTIONS)

    @feed_cmd.autocomplete("pet")
    async def _feed_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._pet_choices(interaction, current)

    # ──────────────────────────────────────────────────────────
    # /treats
    # ──────────────────────────────────────────────────────────
    @app_commands.command(name="treats", description="How many treats you have left today")
    async def treats_cmd(self, interaction: discord.Interaction) -> None:
        if await self._needs_channel(interaction, COMMANDS_CHANNEL_ID):
            return
        assert interaction.guild is not None

        await interaction.response.defer()
        left, fed_ids = await asyncio.to_thread(
            store.treats_left, interaction.guild.id, interaction.user.id, TIMEZONE
        )

        lines = [
            f"{interaction.user.mention} has **{left}** of {store.DAILY_TREATS} treats left today."
        ]
        names = []
        for pid in fed_ids:
            fed_pet = await asyncio.to_thread(store.get_pet, interaction.guild.id, pid)
            if fed_pet:
                names.append(fed_pet.name)
        if names:
            lines.append("Already fed today: " + ", ".join(f"**{n}**" for n in names) + ".")
        if left == 0:
            lines.append("The allowance resets at midnight.")

        await interaction.followup.send("\n".join(lines), allowed_mentions=MENTIONS)

    # ──────────────────────────────────────────────────────────
    # /petinfo
    # ──────────────────────────────────────────────────────────
    @app_commands.command(name="petinfo", description="How one pet is doing")
    @app_commands.describe(pet="Which pet — start typing a name")
    async def petinfo_cmd(self, interaction: discord.Interaction, pet: str) -> None:
        if await self._needs_channel(interaction, COMMANDS_CHANNEL_ID):
            return
        assert interaction.guild is not None

        record = await self._resolve(interaction.guild, pet)
        if record is None:
            await interaction.response.send_message(
                "There is no pet registered by that name.", ephemeral=True
            )
            return

        await interaction.response.defer()
        stats = await asyncio.to_thread(store.pet_stats, interaction.guild.id, record.pet_id)
        by: dict[str, Any] = stats.get("by", {})

        embed = discord.Embed(
            title=record.name,
            description=f"{_display_name(interaction.guild, record.owner_id)}'s",
            colour=ACCENT,
        )
        embed.add_field(name="Treats eaten", value=str(stats.get("total", 0)))

        fav_id = store.favourite_id(by)
        if fav_id:
            embed.add_field(
                name="Favourite human",
                value=f"{_display_name(interaction.guild, int(fav_id))} ({by[fav_id]})",
            )
        else:
            embed.add_field(name="Favourite human", value="nobody yet")

        ranked = sorted(by.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:5]
        if ranked:
            rows = [
                f"**{count}** — {_display_name(interaction.guild, int(uid))}"
                for uid, count in ranked
            ]
            embed.add_field(name="Fed by", value="\n".join(rows), inline=False)

        file = await self._thumb(record)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")
            await interaction.followup.send(embed=embed, file=file)
            return
        await interaction.followup.send(embed=embed)

    @petinfo_cmd.autocomplete("pet")
    async def _petinfo_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._pet_choices(interaction, current)

    # ──────────────────────────────────────────────────────────
    # /petboard
    # ──────────────────────────────────────────────────────────
    @app_commands.command(name="petboard", description="The best-fed pets in the server")
    async def petboard_cmd(self, interaction: discord.Interaction) -> None:
        if await self._needs_channel(interaction, COMMANDS_CHANNEL_ID):
            return
        guild = interaction.guild
        assert guild is not None

        await interaction.response.defer()
        pets = await asyncio.to_thread(store.top_pets, guild.id)
        feeders = await asyncio.to_thread(store.top_feeders, guild.id)

        embed = discord.Embed(title="Best-fed pets", colour=ACCENT)

        rows = []
        for i, (pid, total, fav_id) in enumerate(pets, 1):
            record = await asyncio.to_thread(store.get_pet, guild.id, pid)
            if record is None:
                continue
            owner = _display_name(guild, record.owner_id)
            fav = _display_name(guild, int(fav_id)) if fav_id else "nobody yet"
            rows.append(
                f"`{i}.` **{record.name}** ({owner}'s) — {total} treats · favourite: {fav}"
            )
        embed.add_field(
            name="Most treats",
            value="\n".join(rows) or "Nobody has fed anything yet.",
            inline=False,
        )

        if feeders:
            rows = [
                f"`{i}.` {_display_name(guild, uid)} — {total} treats given"
                for i, (uid, total) in enumerate(feeders, 1)
            ]
            embed.add_field(name="Most generous", value="\n".join(rows), inline=False)

        embed.set_footer(
            text=f"{store.DAILY_TREATS} treats each per day · resets at midnight"
        )
        await interaction.followup.send(embed=embed)

    # ──────────────────────────────────────────────────────────
    # The hunger nudge
    # ──────────────────────────────────────────────────────────
    @tasks.loop(time=NUDGE_TIME)
    async def hunger_nudge(self) -> None:
        """Once a day, and only when there is something to say.

        Most days this posts nothing at all — that is the design. A bot that
        speaks unprompted every day stops being noticed and starts being muted.
        """
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        channel = guild.get_channel(COMMANDS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        pets = await asyncio.to_thread(store.all_pets, guild.id)
        hungry = []
        for p in pets:
            days = await asyncio.to_thread(store.days_since_fed, guild.id, p)
            if days is not None and days >= HUNGER_DAYS:
                hungry.append((p, days))
        if not hungry:
            return

        hungry.sort(key=lambda pair: -pair[1])
        shown = hungry[:MAX_NUDGE_PETS]

        rows = [
            f"**{p.name}** ({_display_name(guild, p.owner_id)}'s) — {days} days"
            for p, days in shown
        ]
        embed = discord.Embed(
            title="These pets have not been fed in a while",
            description="\n".join(rows),
            colour=ACCENT,
        )
        embed.set_footer(
            text=(
                f"and {len(hungry) - len(shown)} more. Use /feed."
                if len(hungry) > len(shown)
                else "Use /feed."
            )
        )
        try:
            await channel.send(embed=embed)
        except Exception:
            LOG.warning("[pets] hunger nudge failed", exc_info=True)

    @hunger_nudge.before_loop
    async def _before_nudge(self) -> None:
        await self.bot.wait_until_ready()

    # ──────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Slow down. Try again in {error.retry_after:.1f}s.", ephemeral=True
                )
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Pets(bot))
