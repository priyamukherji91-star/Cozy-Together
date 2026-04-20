# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from openai import OpenAI

TIMEZONE = ZoneInfo("Europe/Brussels")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SOURCE_CHANNEL_IDS = [
    1425974792745648252,
    1425974842762596414,
    1425976615451623484,
    1425975425238175764,
]

OUTPUT_CHANNEL_ID = 1426295618934149212
ALLOWED_ROLE_ID = 1425977436859797595

LOOKBACK_DAYS = 30
MAX_SOURCE_LINES = 220
MAX_LINE_LENGTH = 120
MAX_OUTPUT_LENGTH = 1900
IDEA_COUNT = 30

URL_RE = re.compile(r"https?://\S+", re.I)
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
MULTISPACE_RE = re.compile(r"\s+")
BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
EMOJI_ONLY_RE = re.compile(r"^\s*(?:<a?:\w+:\d+>|[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\s:])+$")
DUP_SPACE_RE = re.compile(r"\s+")


class StatusSuggester(commands.Cog):
    """Generate short status suggestions from recent public chat."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client: OpenAI | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if api_key:
            self.client = OpenAI(api_key=api_key)

    # ---------------- checks ----------------

    @staticmethod
    def _has_power(member: discord.Member) -> bool:
        return member.guild_permissions.administrator or any(r.id == ALLOWED_ROLE_ID for r in member.roles)

    # ---------------- cleaners ----------------

    @staticmethod
    def _normalize(text: str) -> str:
        return MULTISPACE_RE.sub(" ", text).strip()

    def _replace_mentions(self, text: str, guild: discord.Guild) -> str:
        def user_repl(match: re.Match) -> str:
            member = guild.get_member(int(match.group(1)))
            return member.display_name if member else "someone"

        def role_repl(match: re.Match) -> str:
            role = guild.get_role(int(match.group(1)))
            return role.name if role else "some role"

        text = MENTION_RE.sub(user_repl, text)
        text = ROLE_MENTION_RE.sub(role_repl, text)
        text = CHANNEL_MENTION_RE.sub("somewhere", text)
        return text

    def _clean_line(self, message: discord.Message) -> str:
        content = message.content or ""
        if not content.strip():
            return ""

        content = BAD_CONTROL_RE.sub("", content)
        content = URL_RE.sub("", content)
        content = CUSTOM_EMOJI_RE.sub(r":\1:", content)
        content = self._replace_mentions(content, message.guild)
        content = self._normalize(content)

        if not content:
            return ""
        if EMOJI_ONLY_RE.match(content):
            return ""
        if len(content) < 4:
            return ""

        if len(content) > MAX_LINE_LENGTH:
            content = content[: MAX_LINE_LENGTH - 1].rstrip() + "…"
        return content

    # ---------------- data collection ----------------

    async def _collect_lines(self, guild: discord.Guild) -> list[str]:
        end_time = datetime.now(TIMEZONE)
        start_time = end_time - timedelta(days=LOOKBACK_DAYS)
        collected: list[tuple[datetime, str]] = []

        for channel_id in SOURCE_CHANNEL_IDS:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                async for msg in channel.history(limit=None, after=start_time, oldest_first=False):
                    if msg.author.bot or not msg.content:
                        continue
                    cleaned = self._clean_line(msg)
                    if not cleaned:
                        continue
                    collected.append((msg.created_at, cleaned))
            except discord.Forbidden:
                continue
            except Exception:
                continue

        collected.sort(key=lambda item: item[0], reverse=True)

        deduped: list[str] = []
        seen: set[str] = set()
        for _, line in collected:
            key = DUP_SPACE_RE.sub(" ", line.lower()).strip(" .!?,:;-'\"")
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(line)
            if len(deduped) >= MAX_SOURCE_LINES:
                break

        return deduped

    # ---------------- generation ----------------

    async def _generate_ideas(self, lines: list[str]) -> list[str]:
        if not self.client or not lines:
            return []

        transcript = "\n".join(f"- {line}" for line in lines)

        system_prompt = (
            "You write status ideas for a Discord bot called Mittens. "
            "The vibe is short, sharp, dry, bratty, cat-chaotic, and a little spicy. "
            "Return only status ideas, one per line. "
            "Each idea must be very short: ideally 2 to 5 words, hard cap 7 words. "
            "No bullet symbols, no numbering, no quotes, no emojis. "
            "No Discord mentions, no channel names, no links. "
            "Do not copy lines from the source verbatim. "
            "Do not make them sexual, hateful, or targeted at protected traits. "
            "Keep them usable as rotating custom statuses."
        )

        user_prompt = (
            f"Using the recent server chat below, invent {IDEA_COUNT} short status ideas for Mittens. "
            "Make them feel like things Mittens would display as a status, not full sentences or announcements. "
            "Recent chat sample:\n\n"
            f"{transcript}"
        )

        completion = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=OPENAI_MODEL,
            temperature=1.15,
            max_completion_tokens=500,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        text = (completion.choices[0].message.content or "").strip()
        raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        cleaned: list[str] = []
        seen: set[str] = set()
        for line in raw_lines:
            line = re.sub(r"^[-*•\d.\s]+", "", line).strip()
            line = line.strip('"“”\'')
            line = URL_RE.sub("", line)
            line = self._normalize(line)
            if not line:
                continue
            if len(line.split()) > 7:
                continue
            if len(line) > 60:
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(line)

        return cleaned[:IDEA_COUNT]

    @staticmethod
    def _fallback_ideas(lines: list[str]) -> list[str]:
        seeds = [
            "lurking professionally",
            "judging the transcript",
            "reading your nonsense",
            "collecting fresh evidence",
            "supervising poor decisions",
            "ignoring your links",
            "allergic to dignity",
            "archiving the chaos",
            "too many witnesses",
            "napping through discourse",
            "stealing chat material",
            "watching bad takes hatch",
        ]

        ideas: list[str] = []
        seen = set()
        for item in seeds:
            if item not in seen:
                ideas.append(item)
                seen.add(item)
        for line in lines[:10]:
            words = [w for w in re.findall(r"[A-Za-z0-9']+", line.lower()) if len(w) >= 4]
            if not words:
                continue
            idea = f"{words[0]} incident ongoing"
            if idea not in seen:
                ideas.append(idea)
                seen.add(idea)
        return ideas[:IDEA_COUNT]

    # ---------------- command ----------------

    @app_commands.command(
        name="status_ideas",
        description="Generate fresh Mittens status suggestions in the test channel.",
    )
    async def status_ideas(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return

        if not self._has_power(interaction.user):
            await interaction.response.send_message("You don’t have paws for that.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        output_channel = interaction.guild.get_channel(OUTPUT_CHANNEL_ID)
        if not isinstance(output_channel, discord.TextChannel):
            await interaction.followup.send("Output channel not found.", ephemeral=True)
            return

        lines = await self._collect_lines(interaction.guild)
        if not lines:
            await interaction.followup.send("Couldn’t find enough usable chat lines.", ephemeral=True)
            return

        try:
            ideas = await self._generate_ideas(lines)
        except Exception:
            ideas = []

        if not ideas:
            ideas = self._fallback_ideas(lines)

        header = (
            "**Mittens status ideas**\n"
            f"Pulled from the last {LOOKBACK_DAYS} days in the configured channels.\n"
            f"Used {len(lines)} cleaned chat lines.\n\n"
        )
        body = "\n".join(f"• {idea}" for idea in ideas)
        message = header + body
        if len(message) > MAX_OUTPUT_LENGTH:
            message = message[: MAX_OUTPUT_LENGTH - 1].rstrip() + "…"

        await output_channel.send(message)
        await interaction.followup.send(f"Posted fresh status ideas in {output_channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusSuggester(bot))
