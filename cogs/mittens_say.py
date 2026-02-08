# cogs/mittens_say.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
import discord
from discord import app_commands
from discord.ext import commands


def _safe_allowed_mentions(allow_user_mentions: bool) -> discord.AllowedMentions:
    """
    Safety: never allow @everyone/@here or role pings.
    Optionally allow user mentions if admin explicitly opts in.
    """
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=allow_user_mentions,
        replied_user=False,  # don't auto-ping the person being replied to
    )


def _is_jump_url(text: str) -> bool:
    # Supports: https://discord.com/channels/<guild>/<channel>/<message>
    return bool(re.search(r"https?://(?:ptb\.|canary\.)?discord\.com/channels/\d+/\d+/\d+", text.strip()))


class MittensSay(commands.Cog):
    """
    Admin-only:
    - /mittensay -> posts as Mittens in a channel (optional) with ephemeral confirmation
    - message context menu: "Mittens: reply" -> replies to a specific message as Mittens
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Context menu command (right-click a message)
        self.reply_as_mittens_menu = app_commands.ContextMenu(
            name="Mittens: reply",
            callback=self._reply_as_mittens_context,
        )
        self.bot.tree.add_command(self.reply_as_mittens_menu)

    def cog_unload(self) -> None:
        # Clean removal if cog is unloaded/reloaded
        try:
            self.bot.tree.remove_command(self.reply_as_mittens_menu.name, type=self.reply_as_mittens_menu.type)
        except Exception:
            pass

    # -------------------------
    # Slash command
    # -------------------------
    @app_commands.command(
        name="mittensay",
        description="Post a message as Mittens (admin-only).",
    )
    @app_commands.describe(
        text="What Mittens should say.",
        channel="Optional: where to post it (defaults to this channel).",
        mention="Optional: mention a specific user (only if you enable mentions).",
        allow_mentions="Allow user mentions in the message (never @everyone/@here/roles).",
        reply_to="Optional: paste a Discord message link to reply to.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def mittensay(
        self,
        interaction: discord.Interaction,
        text: str,
        channel: discord.TextChannel | discord.Thread | None = None,
        mention: discord.Member | None = None,
        allow_mentions: bool = False,
        reply_to: str | None = None,
    ):
        text = (text or "").strip()
        if not text:
            await interaction.response.send_message("Type something for Mittens to say. 😾", ephemeral=True)
            return
        if len(text) > 2000:
            await interaction.response.send_message("Too long — keep it under 2000 characters.", ephemeral=True)
            return

        target = channel or interaction.channel
        if target is None:
            await interaction.response.send_message("I can’t figure out which channel to post in.", ephemeral=True)
            return

        # Build final content (optional explicit mention)
        content = text
        if mention is not None:
            if not allow_mentions:
                await interaction.response.send_message(
                    "If you want to @mention someone, toggle `allow_mentions: true`.",
                    ephemeral=True,
                )
                return
            content = f"{mention.mention} {text}"

        # Ack immediately (ephemeral = no trace)
        await interaction.response.send_message("Sent ✅", ephemeral=True)

        # Optional reply via jump URL (reliable for slash)
        reference: discord.Message | None = None
        if reply_to:
            reply_to = reply_to.strip()
            if not _is_jump_url(reply_to):
                await interaction.followup.send(
                    "That `reply_to` doesn’t look like a valid Discord message link.",
                    ephemeral=True,
                )
            else:
                try:
                    # discord.py can fetch the message from the link
                    reference = await self._fetch_message_from_jump_url(interaction, reply_to)
                except Exception:
                    reference = None
                    await interaction.followup.send(
                        "Couldn’t fetch that message to reply to (permissions or it was deleted).",
                        ephemeral=True,
                    )

        try:
            await target.send(
                content=content,
                reference=reference,
                allowed_mentions=_safe_allowed_mentions(allow_user_mentions=allow_mentions),
            )
        except discord.Forbidden:
            await interaction.followup.send("I don’t have permission to post there.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("Discord refused the message (send failed).", ephemeral=True)

    # -------------------------
    # Context menu: reply to a specific message
    # -------------------------
    @app_commands.checks.has_permissions(administrator=True)
    async def _reply_as_mittens_context(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ):
        """
        Right-click a message -> Apps -> "Mittens: reply"
        Then the modal asks for text, and Mittens replies to that message.
        """
        modal = _MittensReplyModal(parent=self, target_message=message)
        await interaction.response.send_modal(modal)

    # -------------------------
    # Helpers
    # -------------------------
    async def _fetch_message_from_jump_url(self, interaction: discord.Interaction, url: str) -> discord.Message:
        """
        Parses and fetches a Discord message from a jump URL:
        https://discord.com/channels/<guild_id>/<channel_id>/<message_id>
        """
        parts = url.strip().split("/")
        guild_id = int(parts[-3])
        channel_id = int(parts[-2])
        message_id = int(parts[-1])

        # Ensure we're in the same guild context
        if interaction.guild is None or interaction.guild.id != guild_id:
            raise ValueError("Jump URL is not from this guild.")

        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            # Could be a thread; try fetching
            channel = await interaction.guild.fetch_channel(channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise ValueError("Channel type not supported for replying.")

        return await channel.fetch_message(message_id)

    @mittensay.error
    async def mittensay_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            # Keep it quiet
            if interaction.response.is_done():
                await interaction.followup.send("Admins only. 😼", ephemeral=True)
            else:
                await interaction.response.send_message("Admins only. 😼", ephemeral=True)
            return

        # Generic fallback
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something broke — check logs.", ephemeral=True)
            else:
                await interaction.response.send_message("Something broke — check logs.", ephemeral=True)
        except Exception:
            pass


class _MittensReplyModal(discord.ui.Modal, title="Mittens: Reply"):
    text = discord.ui.TextInput(
        label="What should Mittens say?",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
        placeholder="Type your menace…",
    )

    allow_mentions = discord.ui.TextInput(
        label="Allow user mentions? (true/false)",
        style=discord.TextStyle.short,
        max_length=5,
        required=False,
        placeholder="false",
        default="false",
    )

    def __init__(self, parent: MittensSay, target_message: discord.Message):
        super().__init__()
        self.parent = parent
        self.target_message = target_message

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.allow_mentions.value or "false").strip().lower()
        allow_mentions = raw in ("true", "1", "yes", "y")

        content = self.text.value.strip()
        if not content:
            await interaction.response.send_message("Empty message. 😾", ephemeral=True)
            return

        # Ephemeral confirmation (no trace)
        await interaction.response.send_message("Replied ✅", ephemeral=True)

        try:
            await self.target_message.reply(
                content=content,
                allowed_mentions=_safe_allowed_mentions(allow_user_mentions=allow_mentions),
            )
        except discord.Forbidden:
            await interaction.followup.send("I can’t reply there (missing permissions).", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("Discord refused the reply (send failed).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MittensSay(bot))
