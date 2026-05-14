# cogs/morning_news.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from openai import OpenAI

LOG = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
TIMEZONE = ZoneInfo("Europe/Brussels")
POST_HOUR = 8
POST_MINUTE = 0

LIVE_POST_CHANNEL_ID = 1494993533470507048
TEST_POST_CHANNEL_ID = 1426295618934149212
MENACE_SOURCE_CHANNEL_ID = 1427657614061207724

SOURCE_CHANNEL_IDS = [
    1425974792745648252,
    1425974842762596414,
    1444407439016595487,
    1426112638806523985,
    1425975425238175764,
    1425974830582464522,
    1425974866741563432,
]

TEST_ALLOWED_ROLE_IDS = {
    1426194314337189949,
    1425977436859797595,
}

STATE_DIR = Path("data")
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = STATE_DIR / "morning_news_state.json"

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")

POST_WINDOW_MINUTES = 10
IGNORED_PREFIXES = ("!", "/", ".")
MAX_LINE_LENGTH = 260
MAX_TRANSCRIPT_LINES = 180
MAX_EMBED_BODY_LENGTH = 3500
MAX_SECTION_TITLE_LENGTH = 48
MAX_MENACE_CAPTION_LENGTH = 220
MENACE_LOOKBACK_HOURS = 48
MAX_USED_MENACE_IDS = 100
MAX_NEWS_IMAGES_ANALYZED = int(os.getenv("MORNING_NEWS_MAX_IMAGES", "10"))
MAX_NEWS_IMAGES_PER_MESSAGE = 1

VALID_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

MENTION_RE = re.compile(r"<@!?(?P<id>\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(?P<id>\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(?P<id>\d+)>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.I)
MULTISPACE_RE = re.compile(r"\s+")
BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
EMOJI_ONLY_RE = re.compile(r"^\s*(?:<a?:\w+:\d+>|[\U00010000-\U0010ffff☀-➿⌀-⏿\s])+\s*$")
DIVIDER = "━━━━━━━━━━━━"
DIVIDER_LINE_RE = re.compile(r"(?m)^\s*[━─]{4,}\s*$")
MENACE_TITLE = "**Menace of the Day**"

QUIET_OPENERS = [
    "Against all odds, some of you managed to spend an entire day being only mildly embarrassing.",
    "The last day was quieter than usual, which I did not enjoy and do not respect.",
    "Public activity was disappointingly restrained, though not restrained enough to qualify as dignity.",
]


# ──────────────────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────────────────
@dataclass
class MorningNewsState:
    last_live_post_date: str | None = None
    used_live_menace_message_ids: list[int] = field(default_factory=list)
    used_test_menace_message_ids: list[int] = field(default_factory=list)

    @classmethod
    def load(cls) -> "MorningNewsState":
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                # Tolerate old state files that lacked the ID lists
                raw_live = [int(x) for x in data.get("used_live_menace_message_ids", []) if str(x).isdigit()]
                raw_test = [int(x) for x in data.get("used_test_menace_message_ids", []) if str(x).isdigit()]
                return cls(
                    last_live_post_date=data.get("last_live_post_date"),
                    used_live_menace_message_ids=raw_live,
                    used_test_menace_message_ids=raw_test,
                )
            except Exception:
                return cls()
        return cls()

    def save(self) -> None:
        payload = {
            "last_live_post_date": self.last_live_post_date,
            "used_live_menace_message_ids": self.used_live_menace_message_ids[-MAX_USED_MENACE_IDS:],
            "used_test_menace_message_ids": self.used_test_menace_message_ids[-MAX_USED_MENACE_IDS:],
        }
        STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class MenaceCandidate:
    message_id: int
    image_url: str
    author_name: str
    posted_at: datetime
    reaction_count: int
    context_text: str


# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────
def local_now() -> datetime:
    return datetime.now(TIMEZONE)


def in_post_window(now_local: datetime) -> bool:
    target = now_local.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
    delta = now_local - target
    return timedelta(0) <= delta < timedelta(minutes=POST_WINDOW_MINUTES)


def has_test_role(member: discord.Member) -> bool:
    return any(role.id in TEST_ALLOWED_ROLE_IDS for role in member.roles)


def normalize_space(text: str) -> str:
    return MULTISPACE_RE.sub(" ", text).strip()


def clamp_text(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def is_command_like(content: str) -> bool:
    stripped = content.strip()
    return (not stripped) or stripped.startswith(IGNORED_PREFIXES)


def clean_custom_emoji(text: str) -> str:
    return CUSTOM_EMOJI_RE.sub(r":\1:", text)


def replace_mentions(text: str, guild: discord.Guild) -> str:
    def user_repl(match: re.Match) -> str:
        uid = int(match.group("id"))
        member = guild.get_member(uid)
        return member.display_name if member else "someone"

    def role_repl(match: re.Match) -> str:
        rid = int(match.group("id"))
        role = guild.get_role(rid)
        return role.name if role else "some role"

    def channel_repl(match: re.Match) -> str:
        return "somewhere"

    text = MENTION_RE.sub(user_repl, text)
    text = ROLE_MENTION_RE.sub(role_repl, text)
    text = CHANNEL_MENTION_RE.sub(channel_repl, text)
    return text


def clean_message_content(message: discord.Message) -> str:
    content = message.content or ""

    if is_command_like(content):
        return ""

    content = BAD_CONTROL_RE.sub("", content)
    content = clean_custom_emoji(content)
    content = replace_mentions(content, message.guild)
    content = normalize_space(content)

    if not content:
        return ""
    if URL_ONLY_RE.match(content):
        return ""
    if EMOJI_ONLY_RE.match(content):
        return ""
    if len(content) < 4:
        return ""

    return clamp_text(content, MAX_LINE_LENGTH)


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    filename = (attachment.filename or "").lower()
    if content_type.startswith("image/"):
        return True
    return filename.endswith(VALID_IMAGE_EXTENSIONS)


def is_supported_image_url(url: str) -> bool:
    lowered = url.lower().split("?", 1)[0]
    return lowered.endswith(VALID_IMAGE_EXTENSIONS)


def message_image_urls(message: discord.Message) -> list[str]:
    urls: list[str] = []

    for attachment in message.attachments:
        if is_image_attachment(attachment):
            urls.append(attachment.url)

    for embed in message.embeds:
        if embed.image and embed.image.url and is_supported_image_url(embed.image.url):
            urls.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url and is_supported_image_url(embed.thumbnail.url):
            urls.append(embed.thumbnail.url)

    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def build_menace_block(caption: str) -> str:
    return f"{MENACE_TITLE}\n\n{caption.strip()}"


def score_line(line: str) -> int:
    lowered = line.lower()
    score = min(len(line) // 25, 8)
    if any(ch in line for ch in ("?", "!", "…", "—")):
        score += 1
    if re.search(r"\b(ship|kiss|marry|divorce|cry|scream|wild|insane|trailer|spoiler|work|internship|food|farm|help)\b", lowered):
        score += 2
    if '"' in line or "'" in line:
        score += 1
    return score


def choose_relevant_lines(lines: list[str], max_lines: int) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    scored = [(score_line(line), idx, line) for idx, line in enumerate(lines)]
    picked = sorted(scored, key=lambda x: (-x[0], x[1]))[:max_lines]
    picked.sort(key=lambda x: x[1])
    return [line for _, _, line in picked]


def split_embed_description(text: str, limit: int = 4096) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def split_embed_description_preserving_menace(text: str, limit: int = 4096) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text

    menace_index = text.find(MENACE_TITLE)
    if menace_index == -1:
        return split_embed_description(text, limit=limit)

    main_body = text[:menace_index].strip()
    menace_body = text[menace_index:].strip()

    reserved = len(menace_body) + len(f"\n\n{DIVIDER}\n")
    available_for_main = max(0, limit - reserved - 1)

    if available_for_main <= 0:
        return split_embed_description(menace_body, limit=limit)

    trimmed_main = split_embed_description(main_body, limit=available_for_main).strip()
    if not trimmed_main:
        return split_embed_description(menace_body, limit=limit)

    combined = f"{trimmed_main}\n\n{DIVIDER}\n{menace_body}"
    return split_embed_description(combined, limit=limit)


def normalize_news_format(text: str) -> str:
    # Simplified from the original 8-pass regex pipeline. The old passes were fighting model
    # output more than helping it. Now we just: normalize divider variants, strip stray category
    # tags, ensure a blank line after bold titles, and clean up section spacing.
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    text = DIVIDER_LINE_RE.sub(DIVIDER, text)

    text = re.sub(r"(?m)^\s*【[^】\n]{1,60}】\s+(\*\*[^\n]+?\*\*)", r"\1", text)
    text = re.sub(r"(?m)^\s*【[^】\n]{1,60}】\s*\n\s*(\*\*)", r"\1", text)
    text = re.sub(r"(?m)^\s*【[^】\n]{1,60}】\s*$\n?", "", text)

    text = re.sub(r"(?m)^(\*\*[^\n]+?\*\*)\n(?!\n)", r"\1\n\n", text)

    parts = [p.strip() for p in re.split(rf"\n*{re.escape(DIVIDER)}\n*", text)]
    parts = [re.sub(r"\n{3,}", "\n\n", p).strip() for p in parts if p.strip()]

    return f"\n\n{DIVIDER}\n\n".join(parts)


def build_fallback_news(grouped: dict[str, list[str]], total_messages: int) -> str:
    """Used only when there is genuinely no transcript to pass to the model."""
    import random
    if not grouped:
        return (
            f"**{random.choice(QUIET_OPENERS[:2])}**\n\n"
            "There was nothing on record worth reporting. "
            "This either means the day was unusually peaceful, or that everyone was careful. "
            "I do not know which is worse."
        )

    ordered = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    chosen = ordered[:min(3, len(ordered))]
    intro = (
        "The day passed. Things were said. Mittens was unavailable for full comment."
        if total_messages < 25
        else f"Another 24 hours produced {total_messages} messages. The record stands, even without full analysis."
    )
    parts = [f"**Daily Damage Report**\n\n{intro}"]
    for name, msgs in chosen:
        parts.append(f"**{name}**\n\n{name} contributed {len(msgs)} message(s).")
    if len(grouped) > len(chosen):
        extras = len(grouped) - len(chosen)
        parts.append(f"**Also Present**\n\n{extras} others were there too.")
    return normalize_news_format(f"\n\n{DIVIDER}\n\n".join(parts))


# ──────────────────────────────────────────────────────────────
# COG
# ──────────────────────────────────────────────────────────────
class MorningNews(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = MorningNewsState.load()
        self.client: OpenAI | None = None
        self._startup_task: asyncio.Task | None = None

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if api_key:
            self.client = OpenAI(api_key=api_key)

    async def cog_load(self) -> None:
        self._startup_task = asyncio.create_task(self._start_loop_after_ready())

    def cog_unload(self) -> None:
        if self.post_loop.is_running():
            self.post_loop.cancel()
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

    async def _start_loop_after_ready(self) -> None:
        await self.bot.wait_until_ready()
        if not self.post_loop.is_running():
            self.post_loop.start()

    @tasks.loop(minutes=1)
    async def post_loop(self) -> None:
        now = local_now()

        if not in_post_window(now):
            return

        today_key = now.date().isoformat()
        if self.state.last_live_post_date == today_key:
            return

        channel = self.bot.get_channel(LIVE_POST_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            embed, menace = await self.build_news_embed(for_test=False)
            await channel.send(embed=embed)
            self.state.last_live_post_date = today_key
            if menace:
                self._remember_used_menace(menace.message_id, pool="live")
            self.state.save()
        except Exception as e:
            LOG.error("Automatic live post failed: %s", e)

    @post_loop.before_loop
    async def before_post_loop(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="test_morning_news",
        description="Generate a test Mitten's Morning News post in the test channel."
    )
    async def test_morning_news(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return

        if not has_test_role(interaction.user):
            await interaction.response.send_message("You don't have paws for that.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.guild.get_channel(TEST_POST_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Test channel not found.", ephemeral=True)
            return

        try:
            embed, menace = await self.build_news_embed(for_test=True)
            await channel.send(embed=embed)
            if menace:
                self._remember_used_menace(menace.message_id, pool="test")
                self.state.save()
            await interaction.followup.send("Test post sent. 🐾", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Test failed: `{e}`", ephemeral=True)

    @app_commands.command(
        name="repost_morning_news",
        description="Post Mitten's Morning News to the live morning news channel."
    )
    async def repost_morning_news(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return

        if not has_test_role(interaction.user):
            await interaction.response.send_message("You don't have paws for that.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.guild.get_channel(LIVE_POST_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Live post channel not found.", ephemeral=True)
            return

        try:
            embed, menace = await self.build_news_embed(for_test=False)
            await channel.send(embed=embed)
            # Keep last_live_post_date in sync so the 8am loop doesn't double-post.
            self.state.last_live_post_date = local_now().date().isoformat()
            if menace:
                self._remember_used_menace(menace.message_id, pool="live")
            self.state.save()
            await interaction.followup.send("Repost sent. 🐾", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Repost failed: `{e}`", ephemeral=True)

    def _remember_used_menace(self, message_id: int | None, pool: str) -> None:
        if not message_id:
            return
        target = (
            self.state.used_test_menace_message_ids
            if pool == "test"
            else self.state.used_live_menace_message_ids
        )
        if message_id not in target:
            target.append(message_id)
        if len(target) > MAX_USED_MENACE_IDS:
            del target[:-MAX_USED_MENACE_IDS]

    async def build_news_embed(self, for_test: bool) -> tuple[discord.Embed, MenaceCandidate | None]:
        now = local_now()
        end_time = now.replace(second=0, microsecond=0)
        start_time = end_time - timedelta(hours=24)

        transcript_lines, grouped_messages, total_messages = await self.collect_transcript_data(
            start_time=start_time,
            end_time=end_time,
        )

        body = await self.generate_news_text(
            transcript_lines=transcript_lines,
            grouped_messages=grouped_messages,
            total_messages=total_messages,
        )

        pool = "test" if for_test else "live"
        menace = await self.collect_menace_of_the_day(end_time=end_time, pool=pool)

        if menace is not None:
            caption = await self.generate_menace_caption(menace) or f"{menace.author_name} posted this. I have no further comment."
            body = f"{body.strip()}\n\n{DIVIDER}\n{build_menace_block(caption)}"

        body = normalize_news_format(body)

        title_date = now.strftime("%B %d, %Y")
        embed = discord.Embed(
            title=f"Mitten's Morning News — {title_date}",
            description=split_embed_description_preserving_menace(body, limit=MAX_EMBED_BODY_LENGTH),
            color=discord.Color.random(),
        )

        if menace is not None:
            embed.set_image(url=menace.image_url)

        return embed, menace

    async def collect_menace_of_the_day(
        self,
        end_time: datetime,
        pool: str,
    ) -> MenaceCandidate | None:
        channel = self.bot.get_channel(MENACE_SOURCE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return None

        start_time = end_time - timedelta(hours=MENACE_LOOKBACK_HOURS)
        used_ids = set(
            self.state.used_test_menace_message_ids
            if pool == "test"
            else self.state.used_live_menace_message_ids
        )

        candidates: list[MenaceCandidate] = []

        try:
            async for msg in channel.history(limit=2000, after=start_time, oldest_first=False):
                if msg.created_at > end_time:
                    continue
                if msg.author.bot:
                    continue
                if msg.id in used_ids:
                    continue

                image_url: str | None = None
                for attachment in msg.attachments:
                    if is_image_attachment(attachment):
                        image_url = attachment.url
                        break

                if not image_url:
                    for embed in msg.embeds:
                        if embed.image and embed.image.url and is_supported_image_url(embed.image.url):
                            image_url = embed.image.url
                            break
                        if embed.thumbnail and embed.thumbnail.url and is_supported_image_url(embed.thumbnail.url):
                            image_url = embed.thumbnail.url
                            break

                if not image_url:
                    continue

                reaction_count = sum(r.count for r in msg.reactions)
                author_name = discord.utils.escape_markdown(msg.author.display_name, as_needed=True)
                context_text = clean_message_content(msg)

                candidates.append(MenaceCandidate(
                    message_id=msg.id,
                    image_url=image_url,
                    author_name=author_name,
                    posted_at=msg.created_at,
                    reaction_count=reaction_count,
                    context_text=context_text,
                ))

        except discord.Forbidden:
            return None
        except Exception:
            LOG.exception("Unexpected error collecting menace of the day")
            return None

        if not candidates:
            return None

        # Most-reacted image wins; recency breaks ties.
        candidates.sort(key=lambda c: (-c.reaction_count, -c.posted_at.timestamp()))
        return candidates[0]

    async def generate_menace_caption(self, menace: MenaceCandidate) -> str | None:
        if not self.client:
            return None

        system_prompt = (
            "You are Mittens the Menace writing the 'Menace of the Day' caption for a Discord daily news post. "
            "Write in English only. "
            "Tone: dry, quietly judgmental, and unimpressed — not a tabloid, not a meme account. "
            "Describe what you actually see in the image: a pet, a screenshot, a meme, a meal, a selfie, whatever it is. "
            "The caption should be grounded in what is visible, not in assumptions about the poster. "
            "Keep it short and sharp: 1 or 2 sentences, under 220 characters. "
            "Do not use hashtags, bullet points, or @ symbols. "
            "Do not invent relationships or events not visible in the image. "
            "Do not use old-timey newspaper language."
        )

        context_bits = [
            f"Posted by: {menace.author_name}",
            f"Posted at: {menace.posted_at.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M')}",
        ]
        if menace.context_text:
            context_bits.append(f"Surrounding message text: {menace.context_text}")

        user_content = [
            {
                "type": "text",
                "text": "Look at this image and write the Menace of the Day caption.\n" + "\n".join(context_bits),
            },
            {
                "type": "image_url",
                "image_url": {"url": menace.image_url},
            },
        ]

        try:
            completion = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=OPENAI_MODEL,
                temperature=1.0,
                max_completion_tokens=120,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            text = (completion.choices[0].message.content or "").strip()
            if text:
                return clamp_text(text, MAX_MENACE_CAPTION_LENGTH)
        except Exception as e:
            LOG.warning("Menace caption generation failed: %s", e)

        return None

    async def collect_transcript_data(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[list[str], dict[str, list[str]], int]:
        collected: list[tuple[datetime, str, str]] = []
        remaining_image_budget = MAX_NEWS_IMAGES_ANALYZED if self.client else 0

        for channel_id in SOURCE_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                async for msg in channel.history(limit=2000, after=start_time, oldest_first=True):
                    if msg.created_at > end_time:
                        continue
                    if msg.author.bot:
                        continue

                    cleaned = clean_message_content(msg)
                    image_notes: list[str] = []

                    if remaining_image_budget > 0:
                        urls = message_image_urls(msg)[:MAX_NEWS_IMAGES_PER_MESSAGE]
                        for image_url in urls:
                            if remaining_image_budget <= 0:
                                break
                            note = await self.describe_news_image(msg, image_url)
                            remaining_image_budget -= 1
                            if note:
                                image_notes.append(note)

                    if not cleaned and not image_notes:
                        continue

                    author_name = discord.utils.escape_markdown(msg.author.display_name, as_needed=True)

                    if cleaned:
                        collected.append((msg.created_at, author_name, cleaned))
                    for note in image_notes:
                        collected.append((msg.created_at, author_name, note))

            except discord.Forbidden:
                continue
            except Exception:
                LOG.exception("Unexpected error reading history for channel %s", channel_id)
                continue

        collected.sort(key=lambda item: item[0])

        grouped: dict[str, list[str]] = defaultdict(list)
        lines = []

        for _, author_name, cleaned in collected:
            grouped[author_name].append(cleaned)
            lines.append(f"{author_name}: {cleaned}")

        lines = choose_relevant_lines(lines, MAX_TRANSCRIPT_LINES)
        return lines, dict(grouped), len(collected)

    async def describe_news_image(self, message: discord.Message, image_url: str) -> str | None:
        if not self.client:
            return None

        author_name = discord.utils.escape_markdown(message.author.display_name, as_needed=True)
        surrounding_text = clean_message_content(message)

        system_prompt = (
            "You are reading a Discord image to add context to a daily server recap. "
            "Describe what is visible: screenshot content, meme captions, readable text, and anything contextually relevant. "
            "Write in English only. "
            "Return one compact plain sentence under 240 characters. "
            "Do not use labels like '[image]' or '[meme]'. "
            "Do not editorialize or roast — just describe what is there. "
            "Do not invent names, relationships, or events not visible in the image. "
            "If the image has no readable or useful context, return nothing."
        )

        context = [
            f"Posted by: {author_name}",
            f"Posted at: {message.created_at.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M')}",
        ]
        if surrounding_text:
            context.append(f"Surrounding message text: {surrounding_text}")

        user_content = [
            {
                "type": "text",
                "text": "Describe this Discord image for the daily recap.\n" + "\n".join(context),
            },
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            },
        ]

        try:
            completion = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=OPENAI_MODEL,
                temperature=0.4,
                max_completion_tokens=100,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            text = (completion.choices[0].message.content or "").strip()
            if text:
                return clamp_text(text, 240)
        except Exception as e:
            LOG.warning("News image analysis failed, skipping: %s", e)

        return None

    async def generate_news_text(
        self,
        transcript_lines: list[str],
        grouped_messages: dict[str, list[str]],
        total_messages: int,
    ) -> str:
        transcript = "\n".join(transcript_lines).strip()
        if not transcript:
            return build_fallback_news(grouped_messages, total_messages)

        if not self.client:
            return (
                "**Mittens Is Unavailable**\n\n"
                "The recap did not happen today. This is a technical issue, not a judgment call. "
                "Probably both."
            )

        system_prompt = (
            "You are Mittens the Menace writing 'Mitten's Morning News' for a Discord server. "
            "Write in English only. "
            "Tone: mean, judgmental, dryly sarcastic, unhinged little menace, but funny rather than cruel. "
            "Do not target gender, sexuality, race, ethnicity, religion, disability, or identity. "
            "Do not mention channel names. "
            "Do not sound like a newspaper. "
            "Do not use bullet points. "
            "Do not use real Discord mentions or @ symbols before names. "
            "When referring to someone, use their plain display name only. "
            "Write the recap as a series of short readable mini-sections, each with a bold funny title, one blank line, and then 1-2 sentences summarizing what that person or small cluster contributed. "
            f"Keep each section title under {MAX_SECTION_TITLE_LENGTH} characters so it stays on one line in Discord. "
            "Do not use category labels or bracket tags such as 【COMMON SENSE MISSING】. "
            "Keep quoting to a minimum. "
            "Prefer summary over raw transcript repetition. "
            "Keep it varied, readable, entertaining, and compact. "
            "Do not ramble. "
            "Aim for roughly 6 to 9 sections total. "
            "Keep the full recap comfortably under 3500 characters. "
            f"Put {DIVIDER} between sections — never before the first section and never after the last. "
            "Some transcript lines may come from image or screenshot analysis; treat them as normal context. "
            "Do not use these words or phrases unless directly quoted from the transcript: "
            "public record, civic concern, civilian activity, proceedings, fragile civil order, "
            "public square, affairs, documentary titled, in attendance, on the record, "
            "presided over, bearing witness, dispatches, filed a report, entered the chat."
        )

        user_prompt = (
            "Turn this cleaned public transcript into a readable daily recap.\n\n"
            "Formatting rules:\n"
            "- Each section: **Funny headline**\n\n  Short paragraph.\n"
            f"- Keep every headline under {MAX_SECTION_TITLE_LENGTH} characters.\n"
            f"- Put {DIVIDER} between sections only — never before the first or after the last.\n"
            "- Do not use category labels or bracket tags.\n"
            "- No @ before names.\n"
            "- Keep quotes rare.\n"
            "- Keep sections punchy, not long.\n"
            "- Keep the total output under 3500 characters.\n\n"
            "Transcript:\n"
            f"{transcript}"
        )

        try:
            completion = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=OPENAI_MODEL,
                temperature=1.0,
                max_completion_tokens=1500,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = (completion.choices[0].message.content or "").strip()
            if text:
                return normalize_news_format(text)
        except Exception as e:
            LOG.warning("OpenAI generation failed: %s", e)

        return (
            "**Mittens Is Napping**\n\n"
            "The recap failed to generate today. This is being treated as a personal slight. "
            "The server's crimes remain unlogged, which is somehow worse."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MorningNews(bot))
