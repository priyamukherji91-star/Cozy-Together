# cogs/x_fix.py
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Tuple, Optional, Iterable, List

import aiohttp
import discord
from discord.ext import commands
from urllib.parse import urljoin

log = logging.getLogger("cozy.x_fix")

TWITTER_DOMAINS = {"twitter.com", "www.twitter.com", "mobile.twitter.com"}
X_DOMAINS = {"x.com", "www.x.com", "mobile.x.com"}
INSTAGRAM_DOMAINS = {"instagram.com", "www.instagram.com"}
# ddinstagram.com is gone — the host no longer resolves at all, so every link the
# bot rewrote to it turned into a dead link. kkinstagram.com is the mirror that
# took over; these hosts are rewritten to it alongside instagram.com itself so
# older ddinstagram links people paste in get repaired too.
DEAD_INSTAGRAM_DOMAINS = {"ddinstagram.com", "www.ddinstagram.com", "d.ddinstagram.com"}
# Only these Instagram paths have something to embed. Profile links (/<user>/)
# 404 on the mirror, so they are left exactly as posted.
INSTAGRAM_EMBEDDABLE_PATH = re.compile(r"^(p|reel|reels|tv|share|stories)(/|$)", re.IGNORECASE)

# Ordered mirror candidates per platform. Every rewrite is probed before it is
# used (see _probe_embeddable), and the first mirror that actually serves an
# embeddable response wins. When one dies the next takes over on its own — no
# redeploy, and no repeat of the ddinstagram/rxddit/fxfacebook breakages where a
# host vanished and the bot went on replacing people's posts with dead links.
TWITTER_MIRRORS = ("fxtwitter.com", "vxtwitter.com")
X_MIRRORS = ("fixupx.com", "fixvx.com")
INSTAGRAM_MIRRORS = ("kkinstagram.com", "eeinstagram.com")
MIRROR_HOSTS = frozenset(TWITTER_MIRRORS + X_MIRRORS + INSTAGRAM_MIRRORS)

# NOTE: Reddit and Facebook are deliberately not rewritten, and their links are
# left exactly as posted. Reddit is actively blocking the mirrors (rxddit.com now
# 502s "Forbidden."), and fxfacebook.com has no DNS record at all — rewriting to
# either replaced people's posts with dead links.
SKIP_DOMAINS = set(MIRROR_HOSTS)
FIXABLE_DOMAINS = ("twitter.com", "x.com", "instagram.com")

# ── Embed probing ──────────────────────────────────────────────────────
# Discord's crawler is what has to be satisfied, so ask as Discord asks.
PROBE_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
PROBE_TIMEOUT_SECONDS = 6
PROBE_MAX_REDIRECTS = 3
PROBE_HTML_READ_BYTES = 64 * 1024
# A mirror handing us straight to the CDN is the success case for video posts.
MEDIA_HOST_HINTS = ("cdninstagram.com", "fbcdn.net", "twimg.com")
# HTTP 200 is not proof of anything: eeinstagram.com answers 200 with a perfectly
# well-formed page whose og:description reads "Post not found". Require real
# media tags, and reject the pages that politely announce their own failure.
OG_MEDIA_RE = re.compile(
    rb"""<meta[^>]+(?:property|name)=["'](?:og:video(?::(?:url|secure_url))?|og:image|twitter:image|twitter:player)["']""",
    re.IGNORECASE,
)
OG_MISSING_RE = re.compile(
    rb"""(?:og:description|og:title)["'][^>]*content=["'][^"']*(?:not found|unavailable|no longer|private|error)""",
    re.IGNORECASE,
)
# Consecutive failures before a mirror is benched, and for how long.
HOST_FAIL_THRESHOLD = 3
HOST_BLOCK_SECONDS = 600

URL_REGEX = re.compile(r"(?<!<)(https?://[^\s>]+)")
WEBHOOK_NAME = "LinkFix Bridge"
DEDUP_TTL_SECONDS = 30
HISTORY_DEDUP_LOOKBACK = 8
MAX_FORWARD_ATTACH_TOTAL_BYTES = 8 * 1024 * 1024

# Only process messages posted in these channels. Anything else is logged and ignored.
# #general (1425974792745648252) is deliberately absent — links are left exactly as
# posted there.
ALLOWED_CHANNEL_IDS = {
    1425974830582464522,
    1425974866741563432,
    1425975425238175764,
    1425974842762596414,
}


def _candidate_urls(url: str) -> List[str]:
    """Every mirror rewrite worth trying for this URL, best first. Empty means leave it alone."""
    # Rebuild the URL from its parts rather than str.replace()-ing the host: the
    # host is lowercased for matching, so replacing it in a mixed-case URL either
    # missed entirely or, worse, hit a matching substring further down the path.
    try:
        _scheme, after = url.split("://", 1)
    except ValueError:
        return []
    host, slash, path = after.partition("/")
    lhost = host.lower()
    if lhost in SKIP_DOMAINS:
        return []
    if lhost in TWITTER_DOMAINS:
        mirrors = TWITTER_MIRRORS
    elif lhost in X_DOMAINS:
        mirrors = X_MIRRORS
    elif lhost in INSTAGRAM_DOMAINS or lhost in DEAD_INSTAGRAM_DOMAINS:
        if not INSTAGRAM_EMBEDDABLE_PATH.match(path.split("?", 1)[0].split("#", 1)[0]):
            return []
        mirrors = INSTAGRAM_MIRRORS
    else:
        return []
    # Mirrors are https-only; never carry a plaintext scheme across.
    return [f"https://{m}{slash}{path}" for m in mirrors]


def _host_of(url: str) -> str:
    try:
        return url.split("://", 1)[1].partition("/")[0].lower()
    except IndexError:
        return ""


async def _probe_embeddable(session: aiohttp.ClientSession, url: str) -> bool:
    """True only if Discord's crawler would get something embeddable out of this URL."""
    current = url
    for _ in range(PROBE_MAX_REDIRECTS + 1):
        async with session.get(
            current,
            headers={"User-Agent": PROBE_UA},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location") or ""
                if not loc:
                    return False
                nxt = urljoin(current, loc)
                nhost = _host_of(nxt)
                if any(hint in nhost for hint in MEDIA_HOST_HINTS):
                    return True  # handed straight to the media CDN — that embeds
                if nhost not in MIRROR_HOSTS:
                    # Punted back to instagram.com/x.com, or off to an ad network
                    # (instagramez.com does exactly this). Neither embeds.
                    return False
                current = nxt
                continue

            if resp.status != 200:
                return False
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype.startswith(("video/", "image/")):
                return True
            if "html" not in ctype:
                return False
            body = await resp.content.read(PROBE_HTML_READ_BYTES)
            if OG_MISSING_RE.search(body):
                return False
            return bool(OG_MEDIA_RE.search(body))
    return False


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
    """Fixes X/Twitter/Instagram links by reposting once via webhook as the original poster."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._recent_ids: set[int] = set()
        self._recent_fps: dict[str, float] = {}
        self._fp_lock = asyncio.Lock()
        self._sweeper_started = False
        self._http: Optional[aiohttp.ClientSession] = None
        # host -> (consecutive failures, blocked-until timestamp)
        self._host_health: dict[str, Tuple[int, float]] = {}

    async def cog_unload(self):
        if self._http and not self._http.closed:
            await self._http.close()

    # ── Mirror selection ───────────────────────────────────────────────
    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def _mirror_ok(self, url: str) -> bool:
        """Probe one candidate, with a circuit breaker so a dead host isn't re-dialled forever."""
        host = _host_of(url)
        now = time.time()
        fails, blocked_until = self._host_health.get(host, (0, 0.0))
        if now < blocked_until:
            return False
        try:
            ok = await _probe_embeddable(self._session(), url)
        except Exception as e:
            log.info("Probe of %s failed: %s", url, e)
            ok = False
        if ok:
            self._host_health.pop(host, None)
            return True
        fails += 1
        blocked = now + HOST_BLOCK_SECONDS if fails >= HOST_FAIL_THRESHOLD else 0.0
        self._host_health[host] = (fails, blocked)
        if blocked:
            log.warning("Mirror %s benched for %ds after %d consecutive failures — is it dead?",
                        host, HOST_BLOCK_SECONDS, fails)
        return False

    async def _rewrite_urls(self, content: str) -> Tuple[str, int, List[str]]:
        """Rewrite every link whose mirror is verified to embed. Returns (content, swapped, notes).

        A link whose mirrors all fail is left exactly as posted — better a bare
        link than deleting someone's message and reposting a dead one.
        """
        out: List[str] = []
        notes: List[str] = []
        last = 0
        count = 0
        for m in URL_REGEX.finditer(content):
            candidates = _candidate_urls(m.group(1))
            if not candidates:
                continue
            chosen = None
            for cand in candidates:
                if await self._mirror_ok(cand):
                    chosen = cand
                    break
            out.append(content[last:m.start()])
            if chosen:
                out.append(chosen)
                count += 1
                notes.append(f"{_host_of(m.group(1))} → {_host_of(chosen)}")
            else:
                out.append(m.group(1))
                notes.append(f"{_host_of(m.group(1))} → no working mirror ({len(candidates)} tried)")
            last = m.end()
        out.append(content[last:])
        return "".join(out), count, notes

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

        fixed, num, notes = await self._rewrite_urls(message.content)
        if num <= 0:
            # Either nothing was rewritable, or every mirror failed its probe. In
            # the latter case the message stays exactly as posted — the bot never
            # deletes a post it cannot actually replace with a working link.
            log.info("RETURN id=%s channel=%s: no URLs swapped (num=%d) notes=%s",
                     message.id, cid, num, notes or "none")
            return
        log.info("id=%s channel=%s: mirrors chosen: %s", message.id, cid, "; ".join(notes))

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
        fixed, num, notes = await self._rewrite_urls(target.content)
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
            f"• mirror probe: {', '.join(f'`{n}`' for n in notes) if notes else '*no rewritable links*'}",
            f"• duplicate in last {HISTORY_DEDUP_LOOKBACK} of history: **{hist_dupe}**",
            f"• webhook available: **{wh is not None}**",
        ]
        if perms is not None:
            lines.append(f"• perms: manage_webhooks=**{perms.manage_webhooks}** manage_messages=**{perms.manage_messages}**")
        benched = [h for h, (_f, until) in self._host_health.items() if time.time() < until]
        if benched:
            lines.append(f"• ⚠️ benched mirrors: {', '.join(f'`{h}`' for h in sorted(benched))}")
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
            verdict = ("❌ Would SKIP: no mirror passed its embed probe — message left as posted."
                       if notes else "❌ Would SKIP: no URLs swapped.")
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
