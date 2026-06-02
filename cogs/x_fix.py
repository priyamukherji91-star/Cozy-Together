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

# Only process messages posted in these channels. Anything else is logged and ignored.
ALLOWED_CHANNEL_IDS = {
    1425974830582464522,
    1425974866741563432,
    1425974792745648252,
    1425975425238175764,
    1425974842762596414,
}


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
        cid = getattr(message.channel, "id", None)
        # (2) Fires for EVERY message before any guard — confirms on_message reaches the cog.
        log.info("on_message: id=%s channel=%s author=%s bot=%s content_len=%d",
                 message.id, cid, getattr(message.author, "id", "?"),
                 getattr(message.author, "bot", "?"), len(message.content or ""))

        if not message.guild:
            log.info("RETURN id=%s: no guild (DM or system message)", message.id)
            return
        if message.author.bot:
            log.info("RETURN id=%s channel=%s: author is a bot", message.id, cid)
            return
        if not message.content:
            log.info("RETURN id=%s channel=%s: empty content (message_content intent missing/disabled?)", message.id, cid)
            return

        # (3) Channel allowlist — only operate in the configured channels.
        if cid not in ALLOWED_CHANNEL_IDS:
            log.info("RETURN id=%s: channel %s not in allowlist %s", message.id, cid, sorted(ALLOWED_CHANNEL_IDS))
            return

        if self._mark_and_check_recent_id(message.id):
            log.info("RETURN id=%s channel=%s: already-seen message id (recent_ids dedup)", message.id, cid)
            return

        lcontent = message.content.lower()
        if not any(d in lcontent for d in FIXABLE_DOMAINS):
            log.info("RETURN id=%s channel=%s: no fixable domain in content", message.id, cid)
            return
        if _has_skip_domain(message.content):
            log.info("RETURN id=%s channel=%s: content already contains a fixed/skip domain", message.id, cid)
            return

        fixed, num = _fix_message_content(message.content)
        if num <= 0:
            log.info("RETURN id=%s channel=%s: no URLs swapped (num=%d)", message.id, cid, num)
            return

        fp = _fingerprint(message.channel.id, fixed)
        if await self._mark_and_check_fp(fp):
            log.info("RETURN id=%s channel=%s: deduped (recent fingerprint cache)", message.id, cid)
            return
        if await self._history_has_same_fp(message.channel, fp):
            log.info("RETURN id=%s channel=%s: deduped (matching message in last %d of history) — already-fixed link nearby?",
                     message.id, cid, HISTORY_DEDUP_LOOKBACK)
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
            log.warning("RETURN id=%s channel=%s: no webhook available — skipping (no fallback)", message.id, cid)
            return
        if message.attachments and not forward_attachments:
            log.info("RETURN id=%s channel=%s: attachments could not be forwarded", message.id, cid)
            return

        try:
            # Build kwargs conditionally: discord.py defaults these to MISSING, and
            # passing None explicitly is treated as "provided" and breaks (files=None
            # raises 'NoneType is not iterable'; thread=None raises on .id).
            avatar_url = message.author.display_avatar.url
            log.info("Webhook impersonating %r avatar=%s", message.author.display_name, avatar_url)
            send_kwargs = dict(
                content=fixed,
                username=message.author.display_name,
                avatar_url=avatar_url,
                allowed_mentions=allow_mentions,
                wait=True,
            )
            if files:
                send_kwargs["files"] = files
            if isinstance(message.channel, discord.Thread):
                send_kwargs["thread"] = message.channel

            await wh.send(**send_kwargs)
        except Exception:
            log.exception("RETURN id=%s channel=%s: webhook send failed — doing nothing (no fallback)", message.id, cid)
            return  # webhook failed — do nothing, no fallback

        log.info("OK id=%s channel=%s: reposted fixed link via webhook (%d url(s) swapped)", message.id, cid, num)
        try:
            await message.delete()
        except Exception:
            log.warning("Could not delete original message %s in channel %s", message.id, cid)

    # ── Debug command ──────────────────────────────────────────────────
    @commands.command(name="fixtest")
    async def fixtest(self, ctx: commands.Context):
        """Find the most recent x.com/twitter.com (or other fixable) link in this channel
        and report how fix_x_links would handle it, including where it would bail out."""
        target: Optional[discord.Message] = None
        try:
            async for msg in ctx.channel.history(limit=50):
                if msg.id == ctx.message.id:
                    continue
                if msg.content and any(d in msg.content.lower() for d in FIXABLE_DOMAINS):
                    target = msg
                    break
        except Exception as e:
            await ctx.send(f"🔍 fixtest: could not read channel history ({e}).")
            return

        if target is None:
            await ctx.send("🔍 fixtest: no message with a fixable link found in the last 50 messages here.")
            return

        in_allowlist = ctx.channel.id in ALLOWED_CHANNEL_IDS
        has_fixable = any(d in target.content.lower() for d in FIXABLE_DOMAINS)
        has_skip = _has_skip_domain(target.content)
        fixed, num = _fix_message_content(target.content)
        fp = _fingerprint(ctx.channel.id, fixed)
        hist_dupe = await self._history_has_same_fp(ctx.channel, fp)
        wh = await _get_or_create_webhook(ctx.channel)
        perms = ctx.channel.permissions_for(ctx.guild.me) if ctx.guild else None

        lines = [
            f"🔍 **fixtest** on message `{target.id}` by **{target.author.display_name}**",
            f"• channel `{ctx.channel.id}` in allowlist: **{in_allowlist}**",
            f"• message_content intent (requested): **{self.bot.intents.message_content}** | content len: **{len(target.content or '')}**",
            f"• author is bot: **{target.author.bot}**",
            f"• contains fixable domain: **{has_fixable}**",
            f"• already-fixed/skip domain present: **{has_skip}**",
            f"• URLs swapped: **{num}**",
            f"• duplicate in last {HISTORY_DEDUP_LOOKBACK} of history: **{hist_dupe}**",
            f"• webhook available: **{wh is not None}**",
        ]
        if perms is not None:
            lines.append(f"• perms: manage_webhooks=**{perms.manage_webhooks}** manage_messages=**{perms.manage_messages}**")
        if num > 0:
            lines.append(f"• fixed → `{fixed[:300]}`")

        # Verdict: walk the same guards fix_x_links uses, in order.
        if not in_allowlist:
            verdict = "❌ Would SKIP: channel not in allowlist."
        elif target.author.bot:
            verdict = "❌ Would SKIP: author is a bot."
        elif not target.content:
            verdict = "❌ Would SKIP: empty content (intent disabled?)."
        elif not has_fixable:
            verdict = "❌ Would SKIP: no fixable domain."
        elif has_skip:
            verdict = "❌ Would SKIP: content already contains a fixed/skip domain."
        elif num <= 0:
            verdict = "❌ Would SKIP: no URLs swapped."
        elif hist_dupe:
            verdict = "❌ Would SKIP: dedup — matching message already in recent history."
        elif wh is None:
            verdict = "❌ Would SKIP: no webhook available (check Manage Webhooks permission)."
        else:
            verdict = "✅ Would REPOST the fixed link via webhook and delete the original."
        lines.append(verdict)

        await ctx.send("\n".join(lines))

    # ── Cog setup ──────────────────────────────────────────────────────
    @commands.Cog.listener("on_ready")
    async def _on_ready(self):
        # (5) Confirm the message_content intent is actually live at runtime.
        #     setup() already refuses to load without it, but log it here too for visibility.
        #     NOTE: this only reflects what the bot REQUESTED — the matching toggle in the
        #     Discord Developer Portal must also be ON, or message.content arrives empty.
        log.info("x_fix ready. Requested message_content intent: %s", self.bot.intents.message_content)

        # (4) Report Manage Webhooks (and related) permissions for each allowlisted channel.
        for cid in sorted(ALLOWED_CHANNEL_IDS):
            chan = self.bot.get_channel(cid)
            if chan is None:
                try:
                    chan = await self.bot.fetch_channel(cid)
                except Exception as e:
                    log.warning("Allowlist channel %s: cannot fetch (%s) — wrong ID or bot not in that guild?", cid, e)
                    continue
            guild = getattr(chan, "guild", None)
            me = guild.me if guild else None
            if me is None:
                log.warning("Allowlist channel %s (#%s): no guild member context", cid, getattr(chan, "name", "?"))
                continue
            perms = chan.permissions_for(me)
            log.info("Allowlist channel %s (#%s): manage_webhooks=%s send_messages=%s manage_messages=%s view_channel=%s",
                     cid, getattr(chan, "name", "?"),
                     perms.manage_webhooks, perms.send_messages, perms.manage_messages, perms.view_channel)

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
