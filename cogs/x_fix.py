# cogs/x_fix.py
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Tuple, Optional, Iterable, List

import discord
from discord.ext import commands

TWITTER_DOMAINS = {"twitter.com", "www.twitter.com", "mobile.twitter.com"}
X_DOMAINS = {"x.com", "www.x.com", "mobile.x.com"}
SKIP_DOMAINS = {"fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com"}

URL_REGEX = re.compile(r"(?<!<)(https?://[^\s>]+)")
WEBHOOK_NAME = "FixTweet Bridge"
DEDUP_TTL_SECONDS = 30
HISTORY_DEDUP_LOOKBACK = 8
MAX_FORWARD_ATTACH_TOTAL_BYTES = 8 * 1024 * 1024


def _swap_domain(url: str) -> str:
    l = url.lower()
    if any(d in l for d in SKIP_DOMAINS):
        return url
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].lower()
    except Exception:
        return url
    if host in TWITTER_DOMAINS:
        return url.replace(host, "fxtwitter.com", 1)
    if host in X_DOMAINS:
        return url.replace(host, "fixupx.com", 1)
    return url


def _has_skip_domain(text: str) -> bool:
    return any(d in text.lower() for d in SKIP_DOMAINS)


def _fix_message_content(content: str) -> Tuple[str, int]:
    count = 0
    def repl(m: re.Match) -> str:
        nonlocal count
        url = m.group(1)
        new_url = _swap_domain(url)
        if new_url != url:
            count += 1
        return new_url
    return URL_REGEX.sub(repl, content), count


def _fingerprint(channel_id: int, content: str) -> str:
    norm = " ".join(content.split())
    return f"{channel_id}:{hashlib.sha256(norm.encode('utf-8')).hexdigest()}"


async def _get_or_create_webhook(channel: discord.abc.GuildChannel) -> Optional[discord.Webhook]:
    """Return a webhook with a token (so we can set username/avatar). Recreate if tokenless. Works in threads."""
    try:
        if isinstance(channel, discord.Thread):
            text_chan = channel.parent if isinstance(channel.parent, discord.TextChannel) else None
        else:
            text_chan = channel if isinstance(channel, discord.TextChannel) else None
        if not text_chan:
            return None

        hooks = await text_chan.webhooks()
        wh = next((h for h in hooks if h.name == WEBHOOK_NAME), None)

        # Try refetch to obtain token (list may omit it)
        if wh and not wh.token:
            try:
                wh = await text_chan.fetch_webhook(wh.id)
            except Exception:
                pass

        # If still missing or tokenless, recreate
        if not wh or not wh.token:
            try:
                if wh and not wh.token:
                    await wh.delete(reason="Recreating webhook to obtain token")
            except Exception:
                pass
            wh = await text_chan.create_webhook(name=WEBHOOK_NAME)

        return wh if wh and wh.token else None
    except discord.Forbidden:
        return None
    except Exception:
        return None


class XFixCog(commands.Cog):
    """Fixes X/Twitter links with one clean message. Shows who posted (webhook impersonation or fallback prefix)."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._recent_ids: set[int] = set()
        self._recent_fps: dict[str, float] = {}
        self._sweeper_started = False

    # ── Helpers ────────────────────────────────────────────────────────
    def _mark_and_check_recent_id(self, mid: int) -> bool:
        if mid in self._recent_ids:
            return True
        self._recent_ids.add(mid)
        self.bot.loop.create_task(self._prune_recent_id(mid))
        return False

    async def _prune_recent_id(self, mid: int):
        await asyncio.sleep(15)
        self._recent_ids.discard(mid)

    def _mark_and_check_fp(self, fp: str) -> bool:
        now = time.time()
        for k, t in list(self._recent_fps.items()):
            if t <= now:
                self._recent_fps.pop(k, None)
        if fp in self._recent_fps:
            return True
        self._recent_fps[fp] = now + DEDUP_TTL_SECONDS
        return False

    async def _history_has_same_fp(self, channel: discord.abc.Messageable, fp: str) -> bool:
        try:
            async for msg in channel.history(limit=HISTORY_DEDUP_LOOKBACK):
                if msg.content and _fingerprint(msg.channel.id, msg.content) == fp:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _should_forward_attachments(atts: Iterable[discord.Attachment]) -> bool:
        total = 0
        for a in atts:
            if a.size is None:
                return False
            total += int(a.size)
            if total > MAX_FORWARD_ATTACH_TOTAL_BYTES:
                return False
        return total > 0

    # ── Listener ───────────────────────────────────────────────────────
    @commands.Cog.listener("on_message")
    async def fix_x_links(self, message: discord.Message):
        if not message.guild or message.author.bot or not message.content:
            return
        if self._mark_and_check_recent_id(message.id):
            return

        lcontent = message.content.lower()
        if ("twitter.com" not in lcontent and "x.com" not in lcontent) or _has_skip_domain(message.content):
            return

        fixed, num = _fix_message_content(message.content)
        if num <= 0:
            return

        fp = _fingerprint(message.channel.id, fixed)
        if self._mark_and_check_fp(fp) or await self._history_has_same_fp(message.channel, fp):
            return

        allow_mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True)
        forward_attachments = self._should_forward_attachments(message.attachments)
        files: List[discord.File] = []
        try:
            if forward_attachments:
                for a in message.attachments:
                    files.append(discord.File(fp=await a.read(), filename=a.filename))
        except Exception:
            files.clear()
            forward_attachments = False

        # --- Preferred: webhook impersonation (shows UserName • APP) ---
        wh = await _get_or_create_webhook(message.channel)
        if wh and (not message.attachments or forward_attachments):
            try:
                username = message.author.display_name
                avatar_url = message.author.display_avatar.url
                thread = message.channel if isinstance(message.channel, discord.Thread) else None

                await wh.send(
                    content=fixed,
                    username=username,
                    avatar_url=avatar_url,
                    files=files or None,
                    allowed_mentions=allow_mentions,
                    wait=False,
                    thread=thread,
                )
                try:
                    await message.delete()
                except Exception:
                    pass
                return
            except Exception:
                pass  # fall through

        # --- Fallback: reply as bot, but prefix with the poster’s name/mention ---
        poster = f"**{message.author.display_name}**"
        # include a mention too so it's crystal-clear in busy channels
        header = f"{poster} ({message.author.mention})"
        fallback_text = f"{header}\n{fixed}"

        try:
            sent = await message.reply(
                fallback_text,
                files=files or None,
                allowed_mentions=allow_mentions,
                mention_author=False,
                suppress=True
            )
        except TypeError:
            sent = await message.reply(
                fallback_text,
                files=files or None,
                allowed_mentions=allow_mentions,
                mention_author=False,
            )

        # Delete original when safe to avoid doubles
        try:
            perms = message.channel.permissions_for(message.guild.me)
            if perms.manage_messages and (not message.attachments or forward_attachments):
                await message.delete()
        except Exception:
            pass

    # ── Cog setup ──────────────────────────────────────────────────────
    @commands.Cog.listener("on_ready")
    async def _on_ready(self):
        async def _sweeper():
            while not self.bot.is_closed():
                now = time.time()
                for k, t in list(self._recent_fps.items()):
                    if t <= now:
                        self._recent_fps.pop(k, None)
                await asyncio.sleep(60)
        if not self._sweeper_started:
            self._sweeper_started = True
            self.bot.loop.create_task(_sweeper())


async def setup(bot: commands.Bot):
    await bot.add_cog(XFixCog(bot))
