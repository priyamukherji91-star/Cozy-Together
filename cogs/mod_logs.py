# cogs/mod_logs.py
# -*- coding: utf-8 -*-
import datetime
from typing import Iterable, Optional, List, Tuple

import discord
from discord.ext import commands

GUILD_ID = 1425974791516586045
LOG_CHANNEL_ID = 1440788870663770302


def _shorten(text: str, limit: int = 1024) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class ModLogs(commands.Cog):
    """
    Mittens' Moderation Logs

    Logs (as embeds in #logs):
      • Message deletes / bulk deletes / edits
      • Reactions add/remove
      • Pins / unpins
      • Role changes, nick changes, timeouts
      • Bans, unbans, kicks (best-effort via audit log)
      • Channel + thread create/delete/update
      • Voice joins/leaves/moves + mute/deaf/stream changes
      • Guild config changes (name, icon, banner)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────
    def _get_log_channel(self, guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
        if not guild or guild.id != GUILD_ID:
            return None
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
        return None

    async def _send_log(
        self,
        guild: Optional[discord.Guild],
        title: str,
        description: Optional[str] = None,
        *,
        color: discord.Color = discord.Color.blurple(),
        fields: Optional[List[Tuple[str, str, bool]]] = None,
        timestamp: Optional[datetime.datetime] = None,
        footer: Optional[str] = None,
    ):
        channel = self._get_log_channel(guild)
        if not channel:
            return

        embed = discord.Embed(
            title=title,
            description=_shorten(description or "", 2000) if description else discord.Embed.Empty,
            color=color,
        )
        embed.timestamp = timestamp or datetime.datetime.utcnow()

        if fields:
            for name, value, inline in fields:
                if value is None or value == "":
                    continue
                embed.add_field(name=name, value=_shorten(value, 1024), inline=inline)

        if footer:
            embed.set_footer(text=_shorten(footer, 128))

        try:
            await channel.send(embed=embed)
        except Exception:
            # Never crash the bot because logs failed
            pass

    async def _find_message_deleter(
        self,
        guild: discord.Guild,
        message: discord.Message,
    ) -> Optional[discord.Member | discord.User]:
        """Best-effort: look in audit logs to see who deleted a message."""
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None

        try:
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(
                limit=6,
                action=discord.AuditLogAction.message_delete,
            ):
                # Only consider very recent entries
                if (now - entry.created_at).total_seconds() > 15:
                    continue
                target = entry.target
                extra = entry.extra
                if not isinstance(target, discord.Member | discord.User):
                    continue
                if target.id != message.author.id:
                    continue
                if getattr(extra, "channel", None) and getattr(extra.channel, "id", None) != message.channel.id:
                    continue
                return entry.user
        except Exception:
            return None
        return None

    async def _find_kicker(
        self,
        guild: discord.Guild,
        user: discord.Member,
    ) -> Optional[discord.Member | discord.User]:
        """Best-effort: when a member leaves, check if it was a kick via audit log."""
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None

        try:
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(
                limit=6,
                action=discord.AuditLogAction.kick,
            ):
                if (now - entry.created_at).total_seconds() > 30:
                    continue
                if entry.target and entry.target.id == user.id:
                    return entry.user
        except Exception:
            return None
        return None

    async def _find_member_update_actor(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> Optional[discord.Member | discord.User]:
        """Best-effort: who updated this member (roles/timeout) via audit log."""
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None

        try:
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(
                limit=6,
                action=discord.AuditLogAction.member_update,
            ):
                if (now - entry.created_at).total_seconds() > 30:
                    continue
                if entry.target and entry.target.id == member.id:
                    return entry.user
        except Exception:
            return None
        return None

    # ──────────────────────────────────────────────────────────────
    # Message events
    # ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel | discord.Thread):
            return

        # Ignore very old uncached system things, but log normal messages
        content = message.content or "*no content*"
        attach_info = ""
        if message.attachments:
            names = ", ".join(a.filename for a in message.attachments[:5])
            more = len(message.attachments) - 5
            if more > 0:
                names += f" (+{more} more)"
            attach_info = names

        deleter = await self._find_message_deleter(message.guild, message)
        deleter_str = (
            f"{deleter.mention} ({deleter.id})" if isinstance(deleter, discord.Member | discord.User) else "Unknown / self-delete"
        )

        fields = [
            ("Author", f"{message.author.mention} (`{message.author.id}`)", True),
            ("Channel", f"{message.channel.mention}", True),
            ("Message ID", f"`{message.id}`", True),
        ]
        if attach_info:
            fields.append(("Attachments", _shorten(attach_info, 1024), False))
        fields.append(("Deleted by", deleter_str, True))
        fields.append(("Created at", discord.utils.format_dt(message.created_at, style='F'), True))

        desc = _shorten(content, 1900)
        await self._send_log(
            message.guild,
            "🗑 Message deleted",
            description=f"```{desc}```" if desc else "*no text*",
            color=discord.Color.red(),
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: Iterable[discord.Message]):
        messages = list(messages)
        if not messages:
            return
        guild = messages[0].guild
        if not guild:
            return
        channel = messages[0].channel
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            return

        count = len(messages)
        preview_lines = []
        for m in messages[:5]:
            preview_lines.append(
                f"[{m.author.display_name}]: {_shorten(m.content or '[no content]', 80)}"
            )
        if count > 5:
            preview_lines.append(f"... and {count - 5} more messages.")

        await self._send_log(
            guild,
            "🧹 Bulk message delete",
            description="\n".join(preview_lines),
            color=discord.Color.dark_red(),
            fields=[
                ("Channel", channel.mention, True),
                ("Count", str(count), True),
            ],
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild:
            return
        if before.author.bot:
            return
        if before.content == after.content:
            return
        if not isinstance(before.channel, discord.TextChannel | discord.Thread):
            return

        before_text = before.content or "*no content*"
        after_text = after.content or "*no content*"

        desc = (
            f"**Before:**\n```{_shorten(before_text, 900)}```\n"
            f"**After:**\n```{_shorten(after_text, 900)}```"
        )

        await self._send_log(
            before.guild,
            "✏️ Message edited",
            description=desc,
            color=discord.Color.orange(),
            fields=[
                ("Author", f"{before.author.mention} (`{before.author.id}`)", True),
                ("Channel", before.channel.mention, True),
                ("Message ID", f"`{before.id}`", True),
                ("Jump", f"[Jump to message]({before.jump_url})", False),
            ],
        )

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        """
        Reactions logging disabled (too noisy for this server).
        Keep the listener so nothing else breaks, but exit immediately.
        """
        return

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction: discord.Reaction, user: discord.User):
        """
        Reactions logging disabled (too noisy for this server).
        Keep the listener so nothing else breaks, but exit immediately.
        """
        return

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(
        self,
        channel: discord.abc.GuildChannel,
        last_pin: Optional[datetime.datetime],
    ):
        guild = getattr(channel, "guild", None)
        if not guild or guild.id != GUILD_ID:
            return

        text = channel.mention if hasattr(channel, "mention") else str(channel)
        when = discord.utils.format_dt(last_pin, style="F") if last_pin else "Unknown"

        await self._send_log(
            guild,
            "📌 Pins updated",
            description=f"Pins changed in {text}.",
            color=discord.Color.gold(),
            fields=[
                ("Channel", text, True),
                ("Last pin at", when, True),
            ],
        )

    # ──────────────────────────────────────────────────────────────
    # Member updates (roles, nick, timeout)
    # ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.guild.id != GUILD_ID:
            return

        guild = before.guild
        changes: List[Tuple[str, str]] = []

        if before.nick != after.nick:
            changes.append(("Nickname", f"`{before.nick}` → `{after.nick}`"))

        # Roles
        if before.roles != after.roles:
            before_set = {r.id for r in before.roles}
            after_set = {r.id for r in after.roles}
            added = [r for r in after.roles if r.id not in before_set and r.name != "@everyone"]
            removed = [r for r in before.roles if r.id not in after_set and r.name != "@everyone"]

            lines = []
            if added:
                lines.append("**Added:** " + ", ".join(r.mention for r in added))
            if removed:
                lines.append("**Removed:** " + ", ".join(r.mention for r in removed))
            if lines:
                changes.append(("Roles", "\n".join(lines)))

        # Timeout
        if before.timed_out_until != after.timed_out_until:
            before_to = before.timed_out_until
            after_to = after.timed_out_until
            if after_to is not None:
                val = f"Timed out until {discord.utils.format_dt(after_to, style='F')}"
            else:
                val = "Timeout cleared."
            changes.append(("Timeout", val))

        if not changes:
            return

        actor = await self._find_member_update_actor(guild, after)
        actor_str = (
            f"{actor.mention} (`{actor.id}`)" if isinstance(actor, discord.Member | discord.User) else "Unknown"
        )

        fields = [
            ("Member", f"{after.mention} (`{after.id}`)", True),
            ("Changed by", actor_str, True),
        ]
        for name, value in changes:
            fields.append((name, value, False))

        await self._send_log(
            guild,
            "🧬 Member updated",
            description=None,
            color=discord.Color.blue(),
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        # This fires for global username / avatar; we log only if the user is in our guild
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        member = guild.get_member(after.id)
        if not member:
            return

        changes = []
        if before.name != after.name:
            changes.append(("Username", f"`{before.name}` → `{after.name}`"))
        if before.discriminator != after.discriminator:
            changes.append(("Discriminator", f"`{before.discriminator}` → `{after.discriminator}`"))
        if before.avatar != after.avatar:
            changes.append(("Avatar", "Avatar changed."))

        if not changes:
            return

        fields = [("User", f"{member.mention} (`{member.id}`)", True)]
        for name, value in changes:
            fields.append((name, value, False))

        await self._send_log(
            guild,
            "🧿 User profile updated",
            description=None,
            color=discord.Color.teal(),
            fields=fields,
        )

    # ──────────────────────────────────────────────────────────────
    # Bans / unbans / kicks
    # ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if guild.id != GUILD_ID:
            return

        me = guild.me
        reason = None
        actor_str = "Unknown"
        if me and me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(
                    limit=5,
                    action=discord.AuditLogAction.ban,
                ):
                    if entry.target and entry.target.id == user.id:
                        actor = entry.user
                        reason = entry.reason
                        if actor:
                            actor_str = f"{actor.mention} (`{actor.id}`)"
                        break
            except Exception:
                pass

        fields = [
            ("User", f"{user.mention if isinstance(user, discord.Member) else user} (`{user.id}`)", True),
            ("Banned by", actor_str, True),
        ]
        if reason:
            fields.append(("Reason", reason, False))

        await self._send_log(
            guild,
            "⛔ User banned",
            color=discord.Color.dark_red(),
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if guild.id != GUILD_ID:
            return

        me = guild.me
        reason = None
        actor_str = "Unknown"
        if me and me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(
                    limit=5,
                    action=discord.AuditLogAction.unban,
                ):
                    if entry.target and entry.target.id == user.id:
                        actor = entry.user
                        reason = entry.reason
                        if actor:
                            actor_str = f"{actor.mention} (`{actor.id}`)"
                        break
            except Exception:
                pass

        fields = [
            ("User", f"{user} (`{user.id}`)", True),
            ("Unbanned by", actor_str, True),
        ]
        if reason:
            fields.append(("Reason", reason, False))

        await self._send_log(
            guild,
            "✅ User unbanned",
            color=discord.Color.green(),
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Only logs if it looks like a kick; normal leaves you already log elsewhere."""
        guild = member.guild
        if guild.id != GUILD_ID:
            return

        kicker = await self._find_kicker(guild, member)
        if not kicker:
            # Probably just left; you said you already have a join/leave channel, so ignore.
            return

        fields = [
            ("User", f"{member} (`{member.id}`)", True),
            ("Kicked by", f"{kicker.mention} (`{kicker.id}`)", True),
        ]

        await self._send_log(
            guild,
            "🥾 Member kicked",
            color=discord.Color.dark_orange(),
            fields=fields,
        )

    # ──────────────────────────────────────────────────────────────
    # Channels & threads
    # ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        guild = getattr(channel, "guild", None)
        if not guild or guild.id != GUILD_ID:
            return

        kind = channel.__class__.__name__
        name = getattr(channel, "mention", None) or f"#{getattr(channel, 'name', '?')}"
        await self._send_log(
            guild,
            "📂 Channel created",
            description=f"{name} ({kind}) created.",
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = getattr(channel, "guild", None)
        if not guild or guild.id != GUILD_ID:
            return

        kind = channel.__class__.__name__
        name = f"#{getattr(channel, 'name', '?')}"
        await self._send_log(
            guild,
            "🗑 Channel deleted",
            description=f"{name} ({kind}) deleted.",
            color=discord.Color.dark_red(),
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ):
        guild = getattr(after, "guild", None)
        if not guild or guild.id != GUILD_ID:
            return

        changes = []
        if getattr(before, "name", None) != getattr(after, "name", None):
            changes.append(f"**Name:** `#{getattr(before, 'name', '?')}` → `#{getattr(after, 'name', '?')}`")

        if hasattr(before, "topic") and hasattr(after, "topic"):
            if before.topic != after.topic:
                changes.append("**Topic changed.**")

        if not changes:
            return

        name = getattr(after, "mention", None) or f"#{getattr(after, 'name', '?')}"
        await self._send_log(
            guild,
            "🛠 Channel updated",
            description=f"{name}\n" + "\n".join(changes),
            color=discord.Color.blurple(),
        )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        guild = thread.guild
        if not guild or guild.id != GUILD_ID:
            return

        parent = thread.parent.mention if thread.parent else "Unknown"
        await self._send_log(
            guild,
            "🧵 Thread created",
            description=f"{thread.mention} in {parent}",
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        guild = thread.guild
        if not guild or guild.id != GUILD_ID:
            return

        parent = thread.parent.mention if thread.parent else "Unknown"
        await self._send_log(
            guild,
            "✂️ Thread deleted",
            description=f"#{thread.name} from {parent}",
            color=discord.Color.dark_red(),
        )

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        guild = after.guild
        if not guild or guild.id != GUILD_ID:
            return

        changes = []
        if before.archived != after.archived:
            changes.append(f"**Archived:** `{before.archived}` → `{after.archived}`")
        if before.locked != after.locked:
            changes.append(f"**Locked:** `{before.locked}` → `{after.locked}`")
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")

        if not changes:
            return

        await self._send_log(
            guild,
            "🧵 Thread updated",
            description=f"{after.mention}\n" + "\n".join(changes),
            color=discord.Color.blurple(),
        )

    # ──────────────────────────────────────────────────────────────
    # Voice events
    # ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        if guild.id != GUILD_ID:
            return

        desc_lines = []

        # Channel join/leave/move
        if before.channel != after.channel:
            if before.channel is None and after.channel is not None:
                desc_lines.append(f"Joined **{after.channel.mention}**")
            elif before.channel is not None and after.channel is None:
                desc_lines.append(f"Left **{before.channel.mention}**")
            else:
                desc_lines.append(
                    f"Moved **{before.channel.mention}** → **{after.channel.mention}**"
                )

        # Flags
        flag_changes = []
        pairs = [
            ("self_mute", "Self mute"),
            ("self_deaf", "Self deaf"),
            ("mute", "Server mute"),
            ("deaf", "Server deaf"),
            ("self_stream", "Streaming"),
            ("self_video", "Video"),
        ]
        for attr, label in pairs:
            if getattr(before, attr) != getattr(after, attr):
                flag_changes.append(
                    f"{label}: `{getattr(before, attr)}` → `{getattr(after, attr)}`"
                )

        if flag_changes:
            desc_lines.append("**State:**\n" + "\n".join(flag_changes))

        if not desc_lines:
            return

        await self._send_log(
            guild,
            "🎧 Voice state updated",
            description=f"{member.mention}\n" + "\n".join(desc_lines),
            color=discord.Color.dark_teal(),
        )

    # ──────────────────────────────────────────────────────────────
    # Guild updates
    # ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if after.id != GUILD_ID:
            return

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.icon != after.icon:
            changes.append("**Icon changed.**")
        if before.banner != after.banner:
            changes.append("**Banner changed.**")
        if before.vanity_url_code != after.vanity_url_code:
            changes.append(
                f"**Vanity URL:** `{before.vanity_url_code}` → `{after.vanity_url_code}`"
            )
        if before.premium_tier != after.premium_tier:
            changes.append(
                f"**Boost level:** `{before.premium_tier}` → `{after.premium_tier}`"
            )

        if not changes:
            return

        await self._send_log(
            after,
            "🏰 Server updated",
            description="\n".join(changes),
            color=discord.Color.purple(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ModLogs(bot))
