# cogs/morning_news.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from openai import OpenAI

LOG = logging.getLogger(__name__)

# The renderer deliberately lives outside cogs/ — bot.py auto-loads cogs/*.py as
# extensions and newspaper.py has no setup(bot). If it can't be imported the cog
# still loads and posts the old text embed.
try:
    from newsroom.newspaper import BRIEF_COUNT, NewspaperContent, render_front_page
    RENDERER_AVAILABLE = True
except Exception:  # pragma: no cover - import-time degradation
    LOG.exception("Newspaper renderer unavailable; falling back to text embeds")
    BRIEF_COUNT = 8
    NewspaperContent = None  # type: ignore[assignment]
    render_front_page = None  # type: ignore[assignment]
    RENDERER_AVAILABLE = False

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

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
STATE_PATH = DATA_DIR / "morning_news_state.json"

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")

POST_WINDOW_MINUTES = 10
IGNORED_PREFIXES = ("!", "/", ".")
MAX_LINE_LENGTH = 260
MAX_TRANSCRIPT_LINES = 180
MAX_MENACE_CAPTION_LENGTH = 220
MENACE_LOOKBACK_HOURS = 48
# The 48h window empties fast: the paper runs daily and never reprints a photo, so
# two quiet days leave nothing to pick. These are the fallbacks, in order — reach
# further back for something unused, and only then reprint an old one.
MENACE_DEEP_LOOKBACK_DAYS = 14
MENACE_ALLOW_REPEAT = True
MAX_USED_MENACE_IDS = 100
MAX_NEWS_IMAGES_ANALYZED = int(os.getenv("MORNING_NEWS_MAX_IMAGES", "10"))
MAX_NEWS_IMAGES_PER_MESSAGE = 1

# ── Front page ────────────────────────────────────────────────
PAPER_NAME = "Mitten's Morning News"
PAPER_STANDFIRST = "The paper of record for Cozy Together"
PAPER_COLOPHON = (
    "Printed overnight by Mittens the Menace · Complaints may be filed with the void · Set in Noto Serif"
)
# Issue numbering runs from the day the column started, so the masthead ages.
PAPER_EPOCH = date(2025, 10, 1)

# Message content on a file post caps at 2000 characters, not the 4096 an embed
# description allows.
MAX_MESSAGE_CONTENT = 2000

# Generous enough that nothing is cut on a normal day. The model is told these
# numbers so it writes to them rather than being trimmed.
LEAD_HEADLINE_MAX = 72
LEAD_BODY_MAX = 950
BRIEF_HEADLINE_MAX = 52
BRIEF_BODY_MAX = 300
TEASE_MAX = 130
SECTION_COUNT = 1 + BRIEF_COUNT  # one lead plus the briefs the page prints

MAX_PHOTO_BYTES = 12 * 1024 * 1024
PHOTO_TIMEOUT_SECONDS = 25
PHOTO_CHUNK_BYTES = 64 * 1024

VALID_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Menace of the Day is supposed to be a real photo someone uploaded — not a Tenor GIF or a
# pasted meme. We deliberately exclude .gif here and only accept uploaded attachments
# (see collect_menace_of_the_day / is_static_image_attachment).
MENACE_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

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

# Rendered as the Menace of the Day block on days when no valid uploaded photo qualifies.
MENACE_EMPTY_LINES = [
    "Menace of the Day is cancelled on account of your collective laziness. Zero pets, zero crimes, zero evidence. I'm taking this personally. Go post. That's an order.",
    "No Menace of the Day. Not one of you posted a single photo worth mocking. A barren, cowardly wasteland. Go post your pets like functional adults.",
    "There is no menace today because none of you did anything. Empty channel, empty hearts. Show me a dog, show me a goblin cat, show me anything.",
]


# ──────────────────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────────────────
@dataclass
class MorningNewsState:
    last_live_post_date: str | None = None
    used_live_menace_message_ids: list[int] = field(default_factory=list)
    used_test_menace_message_ids: list[int] = field(default_factory=list)
    used_live_menace_image_keys: list[str] = field(default_factory=list)
    used_test_menace_image_keys: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "MorningNewsState":
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                # Tolerate old state files that lacked the ID lists
                raw_live = [int(x) for x in data.get("used_live_menace_message_ids", []) if str(x).isdigit()]
                raw_test = [int(x) for x in data.get("used_test_menace_message_ids", []) if str(x).isdigit()]
                keys_live = [str(x) for x in data.get("used_live_menace_image_keys", [])]
                keys_test = [str(x) for x in data.get("used_test_menace_image_keys", [])]
                return cls(
                    last_live_post_date=data.get("last_live_post_date"),
                    used_live_menace_message_ids=raw_live,
                    used_test_menace_message_ids=raw_test,
                    used_live_menace_image_keys=keys_live,
                    used_test_menace_image_keys=keys_test,
                )
            except Exception:
                return cls()
        return cls()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_live_post_date": self.last_live_post_date,
            "used_live_menace_message_ids": self.used_live_menace_message_ids[-MAX_USED_MENACE_IDS:],
            "used_test_menace_message_ids": self.used_test_menace_message_ids[-MAX_USED_MENACE_IDS:],
            "used_live_menace_image_keys": self.used_live_menace_image_keys[-MAX_USED_MENACE_IDS:],
            "used_test_menace_image_keys": self.used_test_menace_image_keys[-MAX_USED_MENACE_IDS:],
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
    image_key: str = ""


@dataclass
class Story:
    """What the model returned, already cleaned and length-capped."""
    tease: str
    lead_headline: str
    lead_body: str
    briefs: list[tuple[str, str]] = field(default_factory=list)


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


# Markdown line markers are stripped once, here at the parse boundary. Doing it per
# consumer means a headline arriving as "### Foo" gets re-prefixed into "### ### Foo".
MD_LINE_MARKER_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|>\s+|\d{1,2}[.)]\s+)+")
# Delimiters that are themselves escaped (\_foo\_) are not emphasis — matching them
# strips the underscores and strands the backslashes on the page. Underscores get a
# word-boundary rule as well, so snake_case_names survive intact.
MD_EMPHASIS_RES = (
    re.compile(r"(?<!\\)(\*\*|\*|`)(?=\S)(.+?)(?<=[^\s\\])\1"),
    re.compile(r"(?<![\w\\])(__|_)(?=\S)(.+?)(?<=[^\s\\])\1(?!\w)"),
)
MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")


def unescape_markdown(text: str) -> str:
    """Undo escape_markdown. Display names reach the model already escaped for
    embeds, so 'sam\\_cool' would otherwise print its backslash on the page."""
    return MD_ESCAPE_RE.sub(r"\1", text or "")


def strip_markdown(text: str) -> str:
    """Flatten model markdown into the plain text the page prints."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [MD_LINE_MARKER_RE.sub("", line).strip() for line in text.split("\n")]
    text = " ".join(line for line in lines if line)
    for _ in range(3):  # nested emphasis, e.g. ***shouting***
        changed = 0
        for pattern in MD_EMPHASIS_RES:
            text, count = pattern.subn(r"\2", text)
            changed += count
        if not changed:
            break
    # After emphasis, so an escaped \* is not resurrected into a delimiter.
    return normalize_space(unescape_markdown(text))


def truncate_words(text: str, max_len: int) -> str:
    """Truncate on a word boundary. A mid-word cut like 'They fou…' reads as a bug
    rather than an edit, so anything the page prints comes through here."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1]
    space = cut.rfind(" ")
    if space > max_len * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,;:.!?—–-") + "…"


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


def is_static_image_attachment(attachment: discord.Attachment) -> bool:
    """True only for uploaded still images — excludes GIFs and video, so the menace picker
    can't grab a Tenor GIF, an animated sticker, or a video clip."""
    content_type = (attachment.content_type or "").lower()
    filename = (attachment.filename or "").lower()
    if content_type.startswith("video/"):
        return False
    if content_type == "image/gif" or filename.endswith(".gif"):
        return False
    if content_type.startswith("image/"):
        return True
    return filename.endswith(MENACE_IMAGE_EXTENSIONS)


def menace_image_key(attachment: discord.Attachment) -> str:
    """Cheap repost fingerprint: original filename + byte size. Catches the same file
    re-uploaded later (which gets a fresh message id and CDN url) without downloading it."""
    return f"{(attachment.filename or '').lower()}:{attachment.size}"


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


def build_fallback_story(grouped: dict[str, list[str]], total_messages: int) -> Story:
    """Used only when there is genuinely no transcript to pass to the model."""
    if not grouped:
        return Story(
            tease="Nothing happened, and it happened loudly.",
            lead_headline="Nothing Whatsoever Occurred",
            lead_body=(
                f"{random.choice(QUIET_OPENERS[:2])} There was nothing on record worth reporting. "
                "This either means the day was unusually peaceful, or that everyone was careful. "
                "I do not know which is worse."
            ),
            briefs=[],
        )

    ordered = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    lead_body = (
        "The day passed. Things were said. Mittens was unavailable for full comment."
        if total_messages < 25
        else f"Another 24 hours produced {total_messages} messages. The record stands, even without full analysis."
    )
    briefs = [
        (name, f"{name} contributed {len(msgs)} message(s) and will be watched accordingly.")
        for name, msgs in ordered[:BRIEF_COUNT]
    ]
    return Story(
        tease=f"{total_messages} messages, no analysis, all consequences.",
        lead_headline="Daily Damage Report",
        lead_body=lead_body,
        briefs=briefs,
    )


def parse_story(raw: str | None) -> Story | None:
    """Turn the model's JSON into a Story.

    This is the single parse boundary: markdown markers are stripped and lengths
    capped here, once, so no downstream consumer has to think about either.
    """
    try:
        data = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        LOG.warning("Model response was not valid JSON")
        return None

    if not isinstance(data, dict):
        return None

    sections: list[tuple[str, str]] = []
    for item in data.get("sections") or []:
        if not isinstance(item, dict):
            continue
        headline = strip_markdown(str(item.get("headline") or ""))
        body = strip_markdown(str(item.get("body") or ""))
        if headline or body:
            sections.append((headline, body))

    if not sections:
        return None

    lead_headline, lead_body = sections[0]
    # Anything past the printed section count was written and would be silently
    # dropped; trimming here keeps that explicit.
    briefs = [
        (truncate_words(h, BRIEF_HEADLINE_MAX), truncate_words(b, BRIEF_BODY_MAX))
        for h, b in sections[1 : 1 + BRIEF_COUNT]
    ]

    return Story(
        tease=truncate_words(strip_markdown(str(data.get("tease") or "")), TEASE_MAX),
        lead_headline=truncate_words(lead_headline, LEAD_HEADLINE_MAX) or "The Day In Question",
        lead_body=truncate_words(lead_body, LEAD_BODY_MAX),
        briefs=briefs,
    )


def story_to_markdown(story: Story) -> str:
    """Flatten a Story back to the divider-separated blob the text embed uses."""
    parts: list[str] = []
    if story.tease:
        parts.append(story.tease)
    for headline, body in [(story.lead_headline, story.lead_body), *story.briefs]:
        if headline or body:
            parts.append(f"**{headline}**\n\n{body}".strip())
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

    async def _already_posted_today_in_channel(self, channel: discord.TextChannel) -> bool:
        today_fragment = local_now().strftime("%B %d, %Y")
        try:
            async for msg in channel.history(limit=5):
                if msg.author.id != self.bot.user.id:
                    continue
                # The front page carries its title in the message content; the text
                # fallback still carries it in an embed title.
                if today_fragment in (msg.content or ""):
                    return True
                for embed in msg.embeds:
                    if embed.title and today_fragment in embed.title:
                        return True
        except Exception:
            pass
        return False

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

        if await self._already_posted_today_in_channel(channel):
            self.state.last_live_post_date = today_key
            self.state.save()
            return

        try:
            await self.deliver_news(channel, pool="live")
            # Only mark the day done after the post actually lands, so a send failure retries
            # next minute (still inside the post window) instead of silently skipping the day.
            self.state.last_live_post_date = today_key
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
            await self.deliver_news(channel, pool="test")
            self.state.save()
            await interaction.followup.send("Test post sent. 🐾", ephemeral=True)
        except Exception as e:
            LOG.exception("Test morning news post failed")
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
            await self.deliver_news(channel, pool="live")
            # Keep last_live_post_date in sync so the 8am loop doesn't double-post.
            self.state.last_live_post_date = local_now().date().isoformat()
            self.state.save()
            await interaction.followup.send("Repost sent. 🐾", ephemeral=True)
        except Exception as e:
            LOG.exception("Morning news repost failed")
            await interaction.followup.send(f"Repost failed: `{e}`", ephemeral=True)

    def _remember_used_menace(self, message_id: int | None, image_key: str, pool: str) -> None:
        ids = (
            self.state.used_test_menace_message_ids
            if pool == "test"
            else self.state.used_live_menace_message_ids
        )
        keys = (
            self.state.used_test_menace_image_keys
            if pool == "test"
            else self.state.used_live_menace_image_keys
        )
        if message_id and message_id not in ids:
            ids.append(message_id)
        if image_key and image_key not in keys:
            keys.append(image_key)
        if len(ids) > MAX_USED_MENACE_IDS:
            del ids[:-MAX_USED_MENACE_IDS]
        if len(keys) > MAX_USED_MENACE_IDS:
            del keys[:-MAX_USED_MENACE_IDS]

    # ──────────────────────────────────────────────────────────
    # DELIVERY
    # ──────────────────────────────────────────────────────────
    async def deliver_news(self, channel: discord.TextChannel, pool: str) -> None:
        """Build and send the post. Every path sends from here, so a discord.File —
        which is consumed by the send that uses it — can never be reused.

        Failures degrade rather than drop the post: no photo means a paper without
        one, and a render failure means the old text embed with the full bodies.
        """
        now = local_now()
        end_time = now.replace(second=0, microsecond=0)
        start_time = end_time - timedelta(hours=24)

        transcript_lines, grouped_messages, total_messages = await self.collect_transcript_data(
            start_time=start_time,
            end_time=end_time,
        )

        story = await self.generate_story(
            transcript_lines=transcript_lines,
            grouped_messages=grouped_messages,
            total_messages=total_messages,
        )

        menace = await self.collect_menace_of_the_day(end_time=end_time, pool=pool)
        caption = ""
        photo_bytes: bytes | None = None
        if menace is not None:
            caption = truncate_words(
                await self.generate_menace_caption(menace)
                or f"{menace.author_name} posted this. I have no further comment.",
                MAX_MENACE_CAPTION_LENGTH,
            )
            photo_bytes = await self.download_photo(menace.image_url)
            if photo_bytes is None:
                LOG.warning("Menace photo unavailable; printing the paper without one")

        title = clamp_text(
            f"**{PAPER_NAME} — {now.strftime('%B %d, %Y')}**", MAX_MESSAGE_CONTENT
        )

        png: bytes | None = None
        page = None
        if RENDERER_AVAILABLE:
            try:
                page = self.build_page_content(
                    now=now,
                    story=story,
                    total_messages=total_messages,
                    menace=menace,
                    caption=caption,
                    photo_bytes=photo_bytes,
                )
                # Pillow blocks, so the whole render runs off the event loop.
                png = await asyncio.to_thread(render_front_page, page)
            except Exception:
                LOG.exception("Front page render failed; falling back to a text embed")

        photo_printed = False
        if png is not None:
            filename = f"mittens-morning-news-{now.strftime('%Y-%m-%d')}.png"
            # Plain attachment, no embed: Discord fits embed images into a small
            # bounded box and a full page shrinks to an unreadable sliver. The tease
            # and the message count are already printed on the page, so repeating
            # them above the picture would just make the post a wall.
            await channel.send(content=title, file=discord.File(io.BytesIO(png), filename=filename))
            # The renderer reports this, not the download: bytes that arrived but
            # failed to decode never made it onto the page.
            photo_printed = bool(page and page.photo_printed)
            if photo_bytes is not None and not photo_printed:
                LOG.warning("Photo downloaded but did not print; leaving it unspent for tomorrow")
        else:
            embed = self.build_fallback_embed(now, story, menace, caption)
            await channel.send(embed=embed)
            photo_printed = menace is not None

        # A photo is only spent once it has actually appeared, or a failed render
        # burns a good photo for tomorrow.
        if menace is not None and photo_printed:
            self._remember_used_menace(menace.message_id, menace.image_key, pool=pool)

    def build_page_content(
        self,
        now: datetime,
        story: Story,
        total_messages: int,
        menace: MenaceCandidate | None,
        caption: str,
        photo_bytes: bytes | None,
    ) -> "NewspaperContent":
        issue_no = max(1, (now.date() - PAPER_EPOCH).days + 1)
        volume = max(1, now.year - PAPER_EPOCH.year + 1)

        photo_label = ""
        if photo_bytes is not None and menace is not None:
            # author_name is escaped for embeds; the page is not markdown.
            photo_label = f"Menace of the day · photograph by {unescape_markdown(menace.author_name)}"

        return NewspaperContent(
            paper_name=PAPER_NAME,
            edition_line=f"{PAPER_STANDFIRST} · Vol. {volume} · No. {issue_no} · Free, obviously",
            dateline=f"{now:%A, %B} {now.day}, {now.year}",
            messages_line=f"{total_messages:,} messages read",
            lead_headline=story.lead_headline,
            lead_body=story.lead_body,
            tease=story.tease,
            briefs=story.briefs,
            photo_bytes=photo_bytes,
            photo_label=photo_label,
            photo_caption=caption if photo_bytes is not None else "",
            colophon=PAPER_COLOPHON,
        )

    def build_fallback_embed(
        self,
        now: datetime,
        story: Story,
        menace: MenaceCandidate | None,
        caption: str,
    ) -> discord.Embed:
        """Text-only degradation. Carries the full bodies — an embed description
        allows 4096 characters, so nothing needs cutting to fit here."""
        body = story_to_markdown(story)
        menace_text = caption if menace is not None else random.choice(MENACE_EMPTY_LINES)
        body = normalize_news_format(f"{body.strip()}\n\n{DIVIDER}\n{build_menace_block(menace_text)}")

        embed = discord.Embed(
            title=f"{PAPER_NAME} — {now.strftime('%B %d, %Y')}",
            description=split_embed_description_preserving_menace(body, limit=4096),
            color=discord.Color.random(),
        )
        if menace is not None:
            embed.set_image(url=menace.image_url)
        return embed

    async def download_photo(self, url: str) -> bytes | None:
        """Fetch the hero photo, draining to EOF.

        resp.content.read(n) returns *up to* n bytes and can stop short, which once
        handed Pillow a JPEG three bytes shy and silently dropped the photo from the
        page — so read in a loop until the stream ends and warn on a short body.
        """
        timeout = aiohttp.ClientTimeout(total=PHOTO_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        LOG.warning("Menace photo fetch returned HTTP %s", resp.status)
                        return None

                    declared = resp.headers.get("Content-Length", "")
                    expected = int(declared) if declared.isdigit() else None
                    if expected is not None and expected > MAX_PHOTO_BYTES:
                        LOG.warning("Menace photo declares %d bytes; too large", expected)
                        return None

                    buffer = bytearray()
                    while True:
                        chunk = await resp.content.read(PHOTO_CHUNK_BYTES)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                        if len(buffer) > MAX_PHOTO_BYTES:
                            LOG.warning("Menace photo exceeded %d bytes; skipping", MAX_PHOTO_BYTES)
                            return None

                    if expected is not None and len(buffer) != expected:
                        LOG.warning(
                            "Menace photo short read: got %d of %d declared bytes",
                            len(buffer), expected,
                        )
                    return bytes(buffer) if buffer else None
        except Exception as e:
            LOG.warning("Menace photo download failed: %s", e)
            return None

    async def collect_menace_of_the_day(
        self,
        end_time: datetime,
        pool: str,
    ) -> MenaceCandidate | None:
        channel = self.bot.get_channel(MENACE_SOURCE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return None

        fresh_start = end_time - timedelta(hours=MENACE_LOOKBACK_HOURS)
        deep_start = end_time - timedelta(days=MENACE_DEEP_LOOKBACK_DAYS)

        # Kept as lists, not sets: append order is use order, which is what ranks
        # repeats by how long ago they last ran.
        used_ids = (
            self.state.used_test_menace_message_ids
            if pool == "test"
            else self.state.used_live_menace_message_ids
        )
        used_keys = (
            self.state.used_test_menace_image_keys
            if pool == "test"
            else self.state.used_live_menace_image_keys
        )

        candidates: list[MenaceCandidate] = []

        try:
            # Scan the deep window and filter later — the used ones are still needed
            # as the last-resort pool.
            async for msg in channel.history(limit=2000, after=deep_start, oldest_first=False):
                if msg.created_at > end_time:
                    continue
                if msg.author.bot:
                    continue

                # Menace must be an uploaded still photo — never a GIF, a video, or a pasted
                # meme/embed. Restricting to attachments quietly drops Tenor/Giphy links too.
                attachment = next(
                    (a for a in msg.attachments if is_static_image_attachment(a)),
                    None,
                )
                if attachment is None:
                    continue

                reaction_count = sum(r.count for r in msg.reactions)
                author_name = discord.utils.escape_markdown(msg.author.display_name, as_needed=True)
                context_text = clean_message_content(msg)

                candidates.append(MenaceCandidate(
                    message_id=msg.id,
                    image_url=attachment.url,
                    author_name=author_name,
                    posted_at=msg.created_at,
                    reaction_count=reaction_count,
                    context_text=context_text,
                    image_key=menace_image_key(attachment),
                ))

        except discord.Forbidden:
            return None
        except Exception:
            LOG.exception("Unexpected error collecting menace of the day")
            return None

        if not candidates:
            return None

        def last_used_rank(candidate: MenaceCandidate) -> int | None:
            """Position in the used lists — lower means it ran longer ago. None means
            it has never run."""
            ranks = [i for i, mid in enumerate(used_ids) if mid == candidate.message_id]
            ranks += [i for i, key in enumerate(used_keys) if key == candidate.image_key]
            return min(ranks) if ranks else None

        def best(pool_: list[MenaceCandidate]) -> MenaceCandidate:
            # Most-reacted image wins; recency breaks ties.
            return min(pool_, key=lambda c: (-c.reaction_count, -c.posted_at.timestamp()))

        unused = [c for c in candidates if last_used_rank(c) is None]
        fresh = [c for c in unused if c.posted_at >= fresh_start]

        if fresh:
            return best(fresh)
        if unused:
            LOG.info(
                "No unused menace photo in the last %dh; reaching back %d days",
                MENACE_LOOKBACK_HOURS, MENACE_DEEP_LOOKBACK_DAYS,
            )
            return best(unused)

        if not MENACE_ALLOW_REPEAT:
            return None

        # Everything in the window has run before. Reprint whichever ran longest ago
        # rather than printing no photo at all.
        repeat = min(candidates, key=lambda c: (last_used_rank(c), -c.reaction_count))
        LOG.info("Every menace photo in the last %d days has run; reprinting the oldest",
                 MENACE_DEEP_LOOKBACK_DAYS)
        return repeat

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

    async def generate_story(
        self,
        transcript_lines: list[str],
        grouped_messages: dict[str, list[str]],
        total_messages: int,
    ) -> Story:
        transcript = "\n".join(transcript_lines).strip()
        if not transcript:
            return build_fallback_story(grouped_messages, total_messages)

        if not self.client:
            return Story(
                tease="The presses are down and nobody is being held accountable.",
                lead_headline="Mittens Is Unavailable",
                lead_body=(
                    "The recap did not happen today. This is a technical issue, not a judgment "
                    "call. Probably both."
                ),
                briefs=[],
            )

        system_prompt = (
            "You are the writer behind 'Mitten's Morning News', a brutally funny Discord gossip "
            "column recapping the last 24 hours of a friend server. Write in English only.\n\n"
            "VOICE:\n"
            "- Sharp, meme-aware, chronically online, impatient. You read like a real human gossip "
            "writer — not a cat, not a narrator, not a corporate recap bot.\n"
            "- Roast hard, but keep it entertaining rather than mean for its own sake. The joke "
            "should land, not just the insult.\n"
            "- Use modern Discord/meme phrasing, but do not force the same vocabulary or "
            "expressions into every paragraph. Keep the voice varied line to line.\n"
            "- Make people sound like they are losing to ordinary things — sleep, reading "
            "comprehension, impulse control, money, timing, consequences — wherever that actually "
            "fits what happened. Never invent a loss that isn't in the transcript.\n"
            "- Some transcript lines come from image or screenshot analysis; treat them as normal "
            "context.\n\n"
            "SENTENCE CRAFT:\n"
            "- Vary sentence shape. Do not open every section the same way and do not run three "
            "sentences of the same length back to back.\n"
            "- Any given meme phrase or bit of internet slang gets used ONCE across the whole post. "
            "Never twice.\n"
            "- Never end a section on a tidy summarising button — no closing line that restates the "
            "joke, moralises, or wraps it in a bow. Stop on the last real detail instead.\n"
            "- Banned vocabulary: delve, tapestry, testament, navigate, landscape, realm, showcase, "
            "seamless, myriad, moreover, furthermore, in conclusion, dive into, unpack, saga, "
            "whirlwind, rollercoaster, journey, elevate, resonate, underscore, pivotal, "
            "ever-evolving, at the end of the day.\n"
            "- Also banned unless directly quoted: litigation, public record, civic concern, oracle, "
            "commiserated, bravado, civilian activity, proceedings, public square, affairs, "
            "documentary titled, fragile civil order.\n\n"
            "Calibrate to this tone:\n"
            "Headline: Greg Has Discovered Fire, Apparently\n"
            "Body: Spent forty minutes explaining a plot twist nobody asked about, lost the thread "
            "halfway through, and still took the victory lap.\n\n"
            "Headline: Local Man Loses Argument With Himself\n"
            "Body: Started a hot take, walked it back nine minutes later, then thanked everyone for "
            "the discourse. There was no discourse. There was only you."
        )

        user_prompt = (
            "Turn this cleaned public transcript into today's front page.\n\n"
            "STRUCTURE — this is printed as a newspaper, so the shape is fixed:\n"
            f"- Return exactly {SECTION_COUNT} sections. The FIRST is the lead story; the other "
            f"{BRIEF_COUNT} are short briefs. Anything beyond that is written and then dropped, so "
            "do not write more.\n"
            "- Pick the day's biggest or funniest thing for the lead. The lead body is the only "
            "one with room to breathe: 3 to 5 sentences.\n"
            "- Each brief is 1 to 2 sentences on a different incident or person.\n"
            "- Plus one 'tease': a single plain sentence for the front page, no headline format.\n\n"
            "LENGTH LIMITS — anything past these is cut, so write inside them:\n"
            f"- tease: under {TEASE_MAX} characters.\n"
            f"- lead headline: under {LEAD_HEADLINE_MAX} characters.\n"
            f"- lead body: under {LEAD_BODY_MAX} characters.\n"
            f"- brief headline: under {BRIEF_HEADLINE_MAX} characters.\n"
            f"- brief body: under {BRIEF_BODY_MAX} characters.\n\n"
            "RULES:\n"
            "- Plain text only. No markdown, no bold, no bullet points, no headers, no category "
            "tags or bracket labels.\n"
            "- Headlines are newspaper headlines: no trailing full stop.\n"
            "- No @ before names. Use plain display names only. No channel mentions.\n"
            "- Sparingly quote from the transcript — prefer summary.\n"
            "- Avoid using 'the room', 'the chat', 'the timeline', 'the server' as acting subjects "
            "more than once each — lean on named people instead.\n"
            "- Do not target gender, sexuality, race, ethnicity, religion, disability, identity, "
            "body, real trauma, or anything too personal.\n"
            "- Do not invent events, relationships, accusations, or motivations not in the "
            "transcript.\n\n"
            "Transcript:\n"
            f"{transcript}"
        )

        # Structured output rather than markdown parsed with regex: the page needs
        # discrete headline/body pairs and a fixed section count, and regex over prose
        # is where that goes wrong.
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["tease", "sections"],
            "properties": {
                "tease": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["headline", "body"],
                        "properties": {
                            "headline": {"type": "string"},
                            "body": {"type": "string"},
                        },
                    },
                },
            },
        }

        try:
            completion = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=OPENAI_MODEL,
                temperature=1.0,
                max_completion_tokens=3000,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "morning_news", "strict": True, "schema": schema},
                },
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            story = parse_story(completion.choices[0].message.content)
            if story is not None:
                return story
            LOG.warning("Model returned no usable sections; using the napping fallback")
        except Exception as e:
            LOG.warning("OpenAI generation failed: %s", e)

        return Story(
            tease="No paper today. Take it up with the cat.",
            lead_headline="Mittens Is Napping",
            lead_body=(
                "The recap failed to generate today. This is being treated as a personal slight. "
                "The server's crimes remain unlogged, which is somehow worse."
            ),
            briefs=[],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MorningNews(bot))
