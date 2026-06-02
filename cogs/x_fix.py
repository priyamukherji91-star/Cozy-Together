# cogs/x_fix.py
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Tuple, Optional, Iterable, List

import discord
from discord.ext import commands

log = logging.getLogger("cozy.x_fix")

TWITTER_DOMAINS = {"twitter.com", "www.twitter.com", "mobile.twitter.com"}
X_DOMAINS = {"x.com", "www.x.com", "mobile.x.com"}
REDDIT_DOMAINS = {"reddit.com", "www.reddit.com", "old.reddit.com"}
INSTAGRAM_DOMAINS = {"instagram.com", "www.instagram.com"}
FACEBOOK_DOMAINS = {"facebook.com", "www.facebook.com", "m.facebook.com"}
SKIP_DOMAINS = {
    "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com",
    "rxddit.com", "vxreddit.com",
    "ddinstagram.com",
    "fxfacebook.com",
}
FIXABLE_DOMAINS = ("twitter.com", "x.com", "reddit.com", "instagram.com", "facebook.com")

URL_REGEX = re.compile(r"(?<!<)(https?://[^\s>]+)")
WEBHOOK_NAME = "LinkFix Bridge"
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
    if host in REDDIT_DOMAINS:
        return url.replace(host, "rxddit.com", 1)
    if host in INSTAGRAM_DOMAINS:
        return url.replace(host, "ddinstagram.com", 1)
    if host in FACEBOOK_DOMAINS:
        return url.replace(host, "fxfacebook.com", 1)
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
            log.warning("No usable text channel for webhook (channel=%s type=%s)", getattr(channel, "id", "?"), type(channel).__name__)
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
        log.warning("Forbidden creating/fetching webhook in channel %s — bot likely missing Manage Webhooks", getattr(channel, "id", "?"))
        return None
    except Exception:
        log.exception("Unexpected error obtaining webhook in channel %s", getattr(channel, "id", "?"))
        return None


class XFixCog(commands.Cog):
    """Fixes X/Twitter/Reddit/Instagram/Facebook links by reposting once via webhook as the original poster."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._recent_ids: set[int] = set()
        self._recent_fps: dict[str, float] = {}
        self._fp_lock = asyncio.Lock()
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

    async def _mark_and_check_fp(self, fp: str) -> bool:
        async with self._fp_lock:
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
            if int(a.size) > MAX_FORWARD_ATTACH_TOTAL_BYTES:
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
        if not any(d in lcontent for d in FIXABLE_DOMAINS) or _has_skip_domain(message.content):
            return

        fixed, num = _fix_message_content(message.content)
        if num <= 0:
            return

        fp = _fingerprint(message.channel.id, fixed)
        if await self._mark_and_check_fp(fp):
            log.info("Deduped (recent fingerprint cache) in channel %s", message.channel.id)
            return
        if await self._history_has_same_fp(message.channel, fp):
            log.info("Deduped (matching message in last %d of history) in channel %s — already-fixed link nearby?",
                     HISTORY_DEDUP_LOOKBACK, message.channel.id)
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

        # --- Webhook impersonation: delete original and repost once as the poster ---
        # The webhook post IS the replacement. If the webhook is unavailable or the
        # send fails, do nothing (no bot-reply fallback — that causes double posts).
        wh = await _get_or_create_webhook(message.channel)
        if not wh:
            log.warning("No webhook available for channel %s — skipping (no fallback)", message.channel.id)
            return
        if message.attachments and not forward_attachments:
            log.info("Skipping channel %s — attachments could not be forwarded", message.channel.id)
            return

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
                wait=True,
                thread=thread,
            )
        except Exception:
            log.exception("Webhook send failed in channel %s — doing nothing (no fallback)", message.channel.id)
            return  # webhook failed — do nothing, no fallback

        log.info("Reposted fixed link via webhook in channel %s (%d url(s) swapped)", message.channel.id, num)
        try:
            await message.delete()
        except Exception:
            log.warning("Could not delete original message %s in channel %s", message.id, message.channel.id)

    # ── Cog setup ──────────────────────────────────────────────────────
    @commands.Cog.listener("on_ready")
    async def _on_ready(self):
        async def _sweeper():
            while not self.bot.is_closed():
                async with self._fp_lock:
                    now = time.time()
                    for k, t in list(self._recent_fps.items()):
                        if t <= now:
                            self._recent_fps.pop(k, None)
                await asyncio.sleep(60)
        if not self._sweeper_started:
            self._sweeper_started = True
            self.bot.loop.create_task(_sweeper())


async def setup(bot: commands.Bot):
    if not bot.intents.message_content:
        raise RuntimeError(
            "cogs.x_fix requires the message_content privileged intent — "
            "set MESSAGE_CONTENT_INTENT=true in your environment."
        )
    await bot.add_cog(XFixCog(bot))
