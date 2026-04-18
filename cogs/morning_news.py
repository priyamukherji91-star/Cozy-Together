# cogs/morning_news.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from openai import OpenAI

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
TIMEZONE = ZoneInfo("Europe/Brussels")
POST_HOUR = 8
POST_MINUTE = 0

LIVE_POST_CHANNEL_ID = 1425974792745648252
TEST_POST_CHANNEL_ID = 1426295618934149212

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

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

POST_WINDOW_MINUTES = 10
IGNORED_PREFIXES = ("!", "/", ".")
MAX_LINE_LENGTH = 260
MAX_TRANSCRIPT_LINES = 180

MENTION_RE = re.compile(r"<@!?(?P<id>\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(?P<id>\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(?P<id>\d+)>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.I)
MULTISPACE_RE = re.compile(r"\s+")
BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
EMOJI_ONLY_RE = re.compile(r"^\s*(?:<a?:\w+:\d+>|[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\s])+\s*$")
WORD_RE = re.compile(r"[A-Za-z0-9']+")

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "have", "from", "your", "you",
    "was", "were", "they", "them", "their", "then", "just", "like", "into", "about",
    "what", "when", "where", "would", "could", "should", "there", "been", "being",
    "because", "while", "over", "under", "more", "less", "very", "really", "still",
    "also", "only", "even", "than", "after", "before", "did", "does", "doing",
    "its", "it's", "cant", "can't", "dont", "don't", "im", "i'm", "ive", "i've",
    "lol", "lmao", "nah", "bro", "help", "yeah", "ye", "pls", "please", "okay", "ok",
    "got", "get", "getting", "went", "well", "too", "much", "some", "any", "all",
    "off", "out", "not", "yes", "one", "two", "three", "four", "five", "here"
}

TITLE_POOL = [
    "Public Nuisance Update",
    "Local Disturbance Continues",
    "Witnesses Regret Everything",
    "Chronically Online Developments",
    "Minor Disaster, Ongoing",
    "Scenes of Deep Embarrassment",
    "Unsupervised Behavior Detected",
    "Civil Order Remains Fragile",
    "Questionable Judgment Returns",
    "Needlessly Loud Affairs",
    "A Situation Has Developed",
    "The Noise Did Not Improve",
]

QUIET_OPENERS = [
    "Against all odds, some of you managed to spend an entire day being only mildly embarrassing.",
    "The last day was quieter than usual, which I did not enjoy and do not respect.",
    "Public activity was disappointingly restrained, though not restrained enough to qualify as dignity.",
]

SUMMARY_FILLERS = [
    "None of this improved with context.",
    "Nobody involved looked smarter by the end of it.",
    "The atmosphere remained deeply unsupervised.",
    "Confidence stayed high. Standards did not.",
    "The public record continues to be a mistake.",
    "I regret to inform you that people kept talking.",
]

# ──────────────────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────────────────
@dataclass
class MorningNewsState:
    last_live_post_date: str | None = None

    @classmethod
    def load(cls) -> "MorningNewsState":
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                return cls(**data)
            except Exception:
                return cls()
        return cls()

    def save(self) -> None:
        STATE_PATH.write_text(
            json.dumps({"last_live_post_date": self.last_live_post_date}, indent=2),
            encoding="utf-8"
        )


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


def extract_keywords(messages: list[str], limit: int = 3) -> list[str]:
    counts: dict[str, int] = defaultdict(int)

    for msg in messages:
        for raw in WORD_RE.findall(msg.lower()):
            word = raw.strip("'")
            if len(word) < 4:
                continue
            if word in STOPWORDS:
                continue
            if word.isdigit():
                continue
            counts[word] += 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:limit]]


def make_funny_title(name: str, messages: list[str]) -> str:
    text = " ".join(messages).lower()

    if any(w in text for w in ("spoiler", "trailer", "pll", "live")):
        return "Breaking Developments in Public Hysteria"
    if any(w in text for w in ("work", "internship", "hours", "overtime", "rights")):
        return "Labour Conditions Continue to Alarm"
    if any(w in text for w in ("food", "eat", "hungry", "farm", "mists")):
        return "Agricultural and Nutritional Affairs"
    if any(w in text for w in ("ship", "kiss", "marry", "love", "date")):
        return "Romantic Mismanagement Desk"
    if any(w in text for w in ("help", "can someone", "please", "pls")):
        return "Emergency Services Were Consulted"
    if any(w in text for w in ("sleep", "tired", "awake")):
        return "Fatigue Has Entered the Chat"
    if any(w in text for w in ("wtf", "wild", "insane", "crazy", "screaming", "clapping")):
        return "Escalation Without Supervision"
    return random.choice(TITLE_POOL)


def summarize_person(messages: list[str]) -> str:
    if not messages:
        return "Was present in the public record and still somehow left questions unanswered."

    joined = " ".join(messages)
    lowered = joined.lower()
    keywords = extract_keywords(messages, limit=3)

    angle_bits = []

    if any(w in lowered for w in ("spoiler", "trailer", "live", "pll")):
        angle_bits.append("spent a suspicious amount of energy reacting to developments in real time")
    if any(w in lowered for w in ("work", "internship", "hours", "overtime")):
        angle_bits.append("brought labour grievances directly into the public square")
    if any(w in lowered for w in ("food", "farm", "mists", "hungry")):
        angle_bits.append("turned basic survival needs into a shared civic concern")
    if any(w in lowered for w in ("help", "please", "pls", "can someone")):
        angle_bits.append("appealed to the crowd as though any of you are licensed to assist")
    if any(w in lowered for w in ("clapping", "screaming", "looting", "hollering", "wild")):
        angle_bits.append("handled excitement with the restraint of a shopping cart on ice")
    if any(w in lowered for w in ("sleep", "tired", "awake")):
        angle_bits.append("sounded one inconvenience away from collapsing decoratively")

    if not angle_bits:
        angle_bits.append("contributed steadily to the general public unrest")
        if keywords:
            angle_bits.append(f"with recurring themes including {', '.join(keywords)}")

    first = angle_bits[0].capitalize() + "."
    second = ""
    if len(angle_bits) > 1:
        second = " " + angle_bits[1].capitalize() + "."

    if keywords and len(angle_bits) == 1:
        second = f" The keywords left behind were {', '.join(keywords)}, which frankly explains a lot."

    closer = " " + random.choice(SUMMARY_FILLERS)
    return first + second + closer


def build_fallback_news(grouped: dict[str, list[str]], total_messages: int) -> str:
    if not grouped:
        return (
            f"{random.choice(QUIET_OPENERS)} There was very little to report beyond the usual low-level lurking and the vague sense that several of you were one bad decision away from making my morning easier. "
            "Even so, the silence itself felt suspicious. I will be monitoring that."
        )

    ordered = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    chosen = ordered[: min(5, len(ordered))]

    intro = (
        f"{random.choice(QUIET_OPENERS)} {total_messages} usable messages survived inspection, which is an upsettingly solid amount of evidence for one day."
        if total_messages < 25
        else f"Another full day of public activity has been dragged into the light, and unfortunately many of you were far too comfortable speaking in front of witnesses. {total_messages} usable messages were collected, which was more than enough to confirm that standards remain missing."
    )

    sections = [intro]

    for name, msgs in chosen:
        title = make_funny_title(name, msgs)
        summary = summarize_person(msgs)
        sections.append(f"**{title} — {name}**\n{summary}")

    if len(grouped) > len(chosen):
        leftovers = len(grouped) - len(chosen)
        sections.append(
            f"**Additional Civilian Activity**\n"
            f"{leftovers} other people also left enough traces to remind me that peace remains a temporary condition."
        )

    return "\n\n".join(sections)


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
            embed = await self.build_news_embed(for_test=False)
            await channel.send(embed=embed)
            self.state.last_live_post_date = today_key
            self.state.save()
        except Exception as e:
            print(f"[MorningNews] Automatic live post failed: {e}")

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
            await interaction.response.send_message("You don’t have paws for that.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.guild.get_channel(TEST_POST_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Test channel not found.", ephemeral=True)
            return

        try:
            embed = await self.build_news_embed(for_test=True)
            await channel.send(embed=embed)
            await interaction.followup.send("Test post sent. 🐾", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Test failed: `{e}`", ephemeral=True)

    async def build_news_embed(self, for_test: bool) -> discord.Embed:
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
            for_test=for_test,
        )

        title_date = now.strftime("%B %d, %Y")
        embed = discord.Embed(
            title=f"Mitten's Morning News — {title_date}",
            description=split_embed_description(body),
            color=discord.Color.random(),
        )
        return embed

    async def collect_transcript_data(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[list[str], dict[str, list[str]], int]:
        collected: list[tuple[datetime, str, str]] = []

        for channel_id in SOURCE_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                async for msg in channel.history(limit=None, after=start_time, oldest_first=True):
                    if msg.created_at.replace(tzinfo=msg.created_at.tzinfo or TIMEZONE) > end_time:
                        continue
                    if msg.author.bot:
                        continue
                    if not msg.content:
                        continue

                    cleaned = clean_message_content(msg)
                    if not cleaned:
                        continue

                    author_name = discord.utils.escape_markdown(msg.author.display_name, as_needed=True)
                    collected.append((msg.created_at, author_name, cleaned))
            except discord.Forbidden:
                continue
            except Exception:
                continue

        collected.sort(key=lambda item: item[0])

        grouped: dict[str, list[str]] = defaultdict(list)
        lines = []

        for _, author_name, cleaned in collected:
            grouped[author_name].append(cleaned)
            lines.append(f"{author_name}: {cleaned}")

        lines = choose_relevant_lines(lines, MAX_TRANSCRIPT_LINES)
        return lines, dict(grouped), len(collected)

    async def generate_news_text(
        self,
        transcript_lines: list[str],
        grouped_messages: dict[str, list[str]],
        total_messages: int,
        for_test: bool,
    ) -> str:
        # If no API key at all, go straight to the better fallback
        if not self.client:
            return build_fallback_news(grouped_messages, total_messages)

        transcript = "\n".join(transcript_lines).strip()
        if not transcript:
            return build_fallback_news(grouped_messages, total_messages)

        system_prompt = (
            "You are Mittens the Menace writing 'Mitten's Morning News' for a Discord server. "
            "Write in English only. "
            "Tone: mean, judgmental, dryly sarcastic, unhinged little menace, but funny rather than cruel. "
            "Do not target gender, sexuality, race, ethnicity, religion, disability, or identity. "
            "Do not mention channel names. "
            "Do not sound like a newspaper. "
            "Do not use bullet points. "
            "Do not use real Discord mentions or @ symbols before names. "
            "When referring to someone, use their plain display name only, and bold the person's name when introducing their section. "
            "Write the recap as a series of short readable mini-sections, each with a bold funny title and then 1-3 sentences summarizing what that person or small cluster contributed. "
            "Keep quoting to a minimum. "
            "Prefer summary over raw transcript repetition. "
            "Keep it varied, readable, and entertaining."
        )

        user_prompt = (
            "Turn this cleaned public transcript into a long but readable daily recap.\n\n"
            "Formatting rules:\n"
            "- Each item should look like a little funny headline followed by a short summary.\n"
            "- Headline must be bold.\n"
            "- Person name should be bold too when introduced.\n"
            "- No @ before names.\n"
            "- Keep quotes rare.\n"
            "- Make the recap easy to read in Discord.\n"
            "- Do not mention channels.\n"
            "- Keep people weighted fairly evenly.\n\n"
            "Transcript:\n"
            f"{transcript}"
        )

        try:
            completion = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=OPENAI_MODEL,
                temperature=1.0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = (completion.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            print(f"[MorningNews] OpenAI generation failed, using fallback: {e}")

        return build_fallback_news(grouped_messages, total_messages)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MorningNews(bot))