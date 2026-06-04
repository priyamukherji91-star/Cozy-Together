# cogs/events.py
# -*- coding: utf-8 -*-
"""
/event — one command to create a tied-together event.

UX: `/event` has exactly ONE option — an optional IMAGE attachment. After you
(optionally) drop an image, a popup form (modal) asks for name, date, time,
description and an optional duration. If no image is attached, the form opens
straight away. On submit the bot:
  1. Creates a native Discord Scheduled Event (external type — a forum channel
     can't be a native event location, so we use EntityType.external), using the
     attached image as the cover if one was given (otherwise no cover).
  2. Creates a forum post/thread in the events forum for discussion.
  3. Edits the event so its location/description point at that thread.
  4. Posts an announcement in #general linking both.

Times are ALWAYS interpreted as UK local (Europe/London, auto GMT/BST) — every
member is UK-based, so there's no timezone picker. The time field is AM/PM only
(e.g. 7:30pm); 24-hour input is rejected.

Why the image is a command option and not in the form: Discord modals only hold
text inputs — they can't carry a file upload — so the image has to sit on the
slash command, and everything else lives in the popup.

Reminders (step 5) are deliberately NOT built here — they need restart-safe
persistence and live in a separate follow-up.

Call order (handles the chicken-and-egg cleanly):
  create event -> create forum thread (event link in opening post)
  -> event.edit() to add the thread link -> announce both in #general.

Tested against discord.py 2.4.x.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import MISSING

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # py3.9+
except Exception:  # pragma: no cover - zoneinfo always present on Railway py3.11
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

LOG = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONFIG  (hardcoded constants, matching the existing cogs)
# ──────────────────────────────────────────────────────────────
GUILD_ID = 1425974791516586045
FORUM_CHANNEL_ID = 1441764350930063400   # events forum (forum channel, not text)
GENERAL_CHANNEL_ID = 1425974792745648252  # where the announcement is posted

# /event may only be invoked from this channel...
COMMAND_CHANNEL_ID = 1429796227192459264
# ...and only by members holding one of these roles.
ALLOWED_ROLE_IDS = {1425977436859797595, 1426194314337189949}

# Everyone is UK-based, so typed times are always UK local. "Europe/London" is the
# tz-database name for the UK zone; it auto-handles GMT/BST.
UK_TZ_NAME = "Europe/London"

# ── Reminders ─────────────────────────────────────────────────
# One reminder, fired 1 hour before the event starts. Restart-safe: the loop re-reads
# events from Discord each tick and records what it has sent in a JSON ledger on the
# Railway volume (same DATA_DIR the other cogs use), keyed by "event_id:offset_seconds".
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
STATE_PATH = DATA_DIR / "events_reminders.json"

REMINDER_OFFSET = timedelta(hours=1)
REMINDER_OFFSET_KEY = str(int(REMINDER_OFFSET.total_seconds()))  # "3600"
# If the bot was offline past the 1-hour mark, only still fire within this grace window
# (the copy says "60 minutes", so firing much later would be a lie) — otherwise suppress.
REMINDER_CATCHUP_GRACE = timedelta(minutes=10)

# {event} = name, {event_link} = discord.com/events URL, {thread} = forum jump_url.
PUBLIC_REMINDER = (
    'Reminder: "{event}" begins in 60 minutes. I did the hard part. Showing up is '
    "*your* job. Bring Tuna and Dreamies. 🐾\n{event_link}\n{thread}"
)
DM_REMINDER = (
    'You clicked Interested, so this one\'s on you. "{event}" starts in 60 minutes — '
    "I did the hard part, showing up is *your* job. Bring Tuna and Dreamies. "
    "I'm watching. 🐾\n{event_link}\n{thread}"
)

DEFAULT_DURATION_MINUTES = 120  # external events require an end time; default start + 2h
MAX_DURATION_MINUTES = 60 * 24 * 7  # one week ceiling, just to catch typos

# Discord field limits we have to respect.
EVENT_NAME_MAX = 100
EVENT_DESC_MAX = 1000
EVENT_LOCATION_MAX = 100
THREAD_NAME_MAX = 100


# ──────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────
def _tz(tz_name: str) -> "ZoneInfo":
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is unavailable in this runtime.")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:  # missing tz database in a slim container
        raise RuntimeError(
            "Timezone data is missing on the host. Add `tzdata` to requirements.txt."
        ) from exc


def _parse_date(date: str):
    try:
        return datetime.strptime(date.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Couldn't read that date. Use `YYYY-MM-DD`, e.g. `2026-06-20`.") from None


def _parse_time(time_str: str):
    """AM/PM only — e.g. 7:30pm, 7pm, 12:00am. (24-hour input is rejected.)"""
    raw = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise ValueError(
        "Couldn't read that time. Use AM/PM like `7:30pm` or `7pm`."
    )


def _parse_start_utc(date: str, time_str: str) -> datetime:
    """Combine date + time, read as UK local, and return a tz-aware UTC instant."""
    naive = datetime.combine(_parse_date(date), _parse_time(time_str))
    # Attach UK wall-clock zone, then convert to an absolute UTC instant.
    # (fold defaults to 0; the rare DST-transition ambiguity isn't worth a UI for here.)
    local = naive.replace(tzinfo=_tz(UK_TZ_NAME))
    return local.astimezone(timezone.utc)


def _parse_duration(raw: str) -> int:
    """Parse the optional duration field (minutes) from the modal."""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_DURATION_MINUTES
    try:
        minutes = int(raw)
    except ValueError:
        raise ValueError("Duration must be a whole number of minutes, e.g. 120.") from None
    if minutes <= 0:
        raise ValueError("Duration must be a positive number of minutes.")
    if minutes > MAX_DURATION_MINUTES:
        raise ValueError("That duration is implausibly long. Keep it under a week.")
    return minutes


def _pick_forum_tags(forum: discord.ForumChannel) -> List[discord.ForumTag]:
    """Defensive tag handling: only apply a tag if the forum requires one."""
    if not forum.flags.require_tag:
        return []
    available = forum.available_tags
    if not available:
        return []
    preferred = next(
        (t for t in available if t.name.strip().lower() in {"event", "events"}),
        available[0],
    )
    return [preferred]


def _event_url(guild_id: int, event_id: int) -> str:
    return f"https://discord.com/events/{guild_id}/{event_id}"


def _is_image(attachment: discord.Attachment) -> bool:
    return (attachment.content_type or "").lower().startswith("image/")


def _format_reminder(template: str, event_name: str, event_link: str, thread_url: Optional[str]) -> str:
    # Drop the trailing {thread} line cleanly when there's no thread to link.
    return template.format(
        event=event_name, event_link=event_link, thread=thread_url or ""
    ).rstrip()


# ──────────────────────────────────────────────────────────────
# Popup form (modal)
# ──────────────────────────────────────────────────────────────
class EventModal(discord.ui.Modal, title="Create an event"):
    # Exactly 5 text inputs — Discord's per-modal maximum.
    name = discord.ui.TextInput(
        label="Event name",
        placeholder="Movie night",
        max_length=EVENT_NAME_MAX,
        required=True,
    )
    date = discord.ui.TextInput(
        label="Date (YYYY-MM-DD)",
        placeholder="2026-06-20",
        max_length=10,
        required=True,
    )
    time = discord.ui.TextInput(
        label="Start time (AM/PM)",
        placeholder="7:30pm  or  7pm",
        max_length=8,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        max_length=900,
        required=True,
    )
    duration = discord.ui.TextInput(
        label="Duration in minutes (optional)",
        placeholder="120 (defaults to 2 hours)",
        max_length=5,
        required=False,
    )

    def __init__(self, cog: "EventsCog", image: Optional[discord.Attachment]):
        super().__init__()
        self.cog = cog
        self.image = image

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._build_event(interaction, self)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOG.exception("EventModal failed", exc_info=error)
        msg = "Something went wrong creating the event. Mittens knocked it off the table. 🐾"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# ──────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────
class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Guards the JSON ledger against the create-path and the loop writing at once.
        self._state_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        if not self.reminder_loop.is_running():
            self.reminder_loop.start()

    def cog_unload(self) -> None:
        if self.reminder_loop.is_running():
            self.reminder_loop.cancel()

    @app_commands.command(
        name="event",
        description="Create a scheduled event, a forum discussion thread, and announce both.",
    )
    @app_commands.describe(image="Optional cover image for the event.")
    async def event(
        self,
        interaction: discord.Interaction,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)

        # Restrict where the command runs and who may run it. These gates must come
        # before send_modal — we get exactly one response (an error OR the popup).
        if interaction.channel is None or interaction.channel.id != COMMAND_CHANNEL_ID:
            return await interaction.response.send_message(
                f"Use this in <#{COMMAND_CHANNEL_ID}>. Mittens is very strict and deeply annoying.",
                ephemeral=True,
            )
        if not any(role.id in ALLOWED_ROLE_IDS for role in interaction.user.roles):
            return await interaction.response.send_message(
                "You don’t have paws for that. 🐾",
                ephemeral=True,
            )

        # Fail fast on a non-image attachment *before* opening the form, since we
        # can't send both an error and a modal as the single allowed response.
        if image is not None and not _is_image(image):
            return await interaction.response.send_message(
                "That attachment isn't an image. Attach a picture, or leave it empty.",
                ephemeral=True,
            )

        # Single option done; open the popup (image is optional and may be None).
        modal = EventModal(self, image)
        await interaction.response.send_modal(modal)

    # ──────────────────────────────────────────────────────────
    # The actual work, run when the modal is submitted.
    # ──────────────────────────────────────────────────────────
    async def _build_event(self, interaction: discord.Interaction, modal: EventModal) -> None:
        # Several REST round-trips follow; defer so we don't blow the 3s window.
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("Guild only.", ephemeral=True)

        # ── Validate inputs ────────────────────────────────────
        name = modal.name.value.strip()
        description = modal.description.value.strip()
        if not name:
            return await interaction.followup.send("The event needs a name.", ephemeral=True)

        try:
            start_utc = _parse_start_utc(modal.date.value, modal.time.value)
            duration = _parse_duration(modal.duration.value)
        except (ValueError, RuntimeError) as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)

        now = datetime.now(timezone.utc)
        if start_utc <= now:
            return await interaction.followup.send(
                "That start time (UK time) is in the past. Pick a future date/time.",
                ephemeral=True,
            )
        end_utc = start_utc + timedelta(minutes=duration)
        start_unix = int(start_utc.timestamp())

        # ── Resolve channels ───────────────────────────────────
        forum = guild.get_channel(FORUM_CHANNEL_ID)
        general = guild.get_channel(GENERAL_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            return await interaction.followup.send(
                "The events forum channel is missing or isn't a forum. Check FORUM_CHANNEL_ID.",
                ephemeral=True,
            )
        if not isinstance(general, (discord.TextChannel, discord.Thread)):
            return await interaction.followup.send(
                "The #general announcement channel is missing or unusable. Check GENERAL_CHANNEL_ID.",
                ephemeral=True,
            )

        # ── Read the cover image once (reused for event/forum/announcement) ──
        image_bytes: Optional[bytes] = None
        image_name: Optional[str] = None
        if modal.image is not None:
            try:
                image_bytes = await modal.image.read()
                image_name = modal.image.filename or "cover.png"
            except discord.HTTPException:
                LOG.warning("Could not download the attached image; continuing without it.")

        # ── Step 1: native scheduled event (external) ──────────
        try:
            scheduled_event = await guild.create_scheduled_event(
                name=name,
                description=description[:EVENT_DESC_MAX],
                start_time=start_utc,
                end_time=end_utc,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location="See forum thread 👇",  # placeholder; updated on the edit pass
                image=image_bytes if image_bytes is not None else MISSING,
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "I can't create scheduled events here. I need the **Manage Events** permission.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            LOG.exception("create_scheduled_event failed")
            return await interaction.followup.send(
                f"Discord rejected the event: {exc.text or exc}", ephemeral=True
            )

        event_link = _event_url(guild.id, scheduled_event.id)

        # ── Step 2: forum post/thread ──────────────────────────
        opening_post = (
            f"**{name}**\n\n"
            f"{description}\n\n"
            f"🗓️ Starts <t:{start_unix}:F> (<t:{start_unix}:R>)\n"
            f"📅 Event: {event_link}\n\n"
            f"Use this thread to chat about it."
        )
        try:
            tags = _pick_forum_tags(forum)
            # No file attachment here on purpose: the event link in the opening post
            # already unfurls the event's cover image into the thread. Attaching the
            # file too would show the same image twice.
            created = await forum.create_thread(
                name=name[:THREAD_NAME_MAX],
                content=opening_post,
                applied_tags=tags,
            )
            thread = created.thread
        except discord.Forbidden:
            await self._safe_delete_event(scheduled_event)
            return await interaction.followup.send(
                "I can't post in the events forum. I need **Create Posts** (Send Messages) "
                "and **Send Messages in Threads** there. The scheduled event was rolled back.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await self._safe_delete_event(scheduled_event)
            LOG.exception("forum create_thread failed")
            return await interaction.followup.send(
                f"Discord rejected the forum post: {exc.text or exc}. "
                "The scheduled event was rolled back.",
                ephemeral=True,
            )

        thread_link = thread.jump_url

        # Persist the event->thread mapping so the 1h reminder can link the thread,
        # even after a restart (the loop has no other record of which thread is which).
        try:
            async with self._state_lock:
                state = self._load_state()
                state["threads"][str(scheduled_event.id)] = thread_link
                self._save_state(state)
        except Exception:
            LOG.exception("Failed to persist event->thread mapping (reminder may omit the thread link)")

        # ── Step 3: tie the event back to the thread ───────────
        tied_description = description
        link_line = f"\n\n💬 Discussion thread: {thread_link}"
        if len(tied_description) + len(link_line) <= EVENT_DESC_MAX:
            tied_description = tied_description + link_line
        try:
            await scheduled_event.edit(
                description=tied_description[:EVENT_DESC_MAX],
                location=thread_link[:EVENT_LOCATION_MAX],
            )
        except discord.HTTPException:
            # Non-fatal: the event and thread both exist; the link-back just didn't apply.
            LOG.exception("scheduled_event.edit failed (event and thread still exist)")

        # ── Step 4: announce both in #general ──────────────────
        embed = discord.Embed(
            title=name,
            description=description,
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="When",
            value=f"<t:{start_unix}:F>\n(<t:{start_unix}:R>)",
            inline=False,
        )
        embed.add_field(name="Event", value=f"[Open in Events]({event_link})", inline=True)
        embed.add_field(name="Discussion", value=f"[Forum thread]({thread_link})", inline=True)

        announce_file = self._make_file(image_bytes, image_name)
        if announce_file is not None:
            embed.set_image(url=f"attachment://{image_name}")

        announced = True
        try:
            await general.send(
                content=f"📢 New event: **{name}**",
                embed=embed,
                file=announce_file or MISSING,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            announced = False
            LOG.warning("Could not announce in #general (missing Send Messages/Embed Links).")
        except discord.HTTPException:
            announced = False
            LOG.exception("Announcement send failed")

        # ── Report back to the creator ─────────────────────────
        summary = f"Done. 🐾\n• Event: {event_link}\n• Thread: {thread_link}"
        if not announced:
            summary += (
                f"\n\n⚠️ I couldn't post the announcement in <#{GENERAL_CHANNEL_ID}> "
                "(missing **Send Messages** / **Embed Links** there). The event and thread are live."
            )
        await interaction.followup.send(summary, ephemeral=True)

    # ──────────────────────────────────────────────────────────
    # Small helpers
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def _make_file(data: Optional[bytes], filename: Optional[str]) -> Optional[discord.File]:
        """Fresh discord.File per send — a BytesIO is consumed once it's uploaded."""
        if not data:
            return None
        return discord.File(io.BytesIO(data), filename=filename or "cover.png")

    @staticmethod
    async def _safe_delete_event(event: discord.ScheduledEvent) -> None:
        try:
            await event.delete()
        except discord.HTTPException:
            LOG.exception("Failed to roll back scheduled event %s", event.id)

    # ──────────────────────────────────────────────────────────
    # Reminders — restart-safe 1-hour-before nudge.
    # ──────────────────────────────────────────────────────────
    @tasks.loop(minutes=1)
    async def reminder_loop(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        channel = guild.get_channel(GENERAL_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        # The events live server-side, so we re-read them every tick — nothing about
        # the schedule is kept in memory or needs to survive a restart.
        try:
            events = await guild.fetch_scheduled_events()
        except discord.HTTPException:
            LOG.exception("fetch_scheduled_events failed")
            return

        now = datetime.now(timezone.utc)
        async with self._state_lock:
            state = self._load_state()
            sent: Dict[str, bool] = state["sent"]
            threads: Dict[str, str] = state["threads"]
            live_ids = set()
            changed = False

            for ev in events:
                try:
                    if ev.status is not discord.EventStatus.scheduled or ev.start_time is None:
                        continue
                    live_ids.add(str(ev.id))

                    key = f"{ev.id}:{REMINDER_OFFSET_KEY}"
                    if key in sent:
                        continue

                    trigger = ev.start_time - REMINDER_OFFSET
                    if now < trigger:
                        continue  # not time yet

                    # Stale: event already started, or we're so far past the 1-hour mark
                    # (after downtime) that "60 minutes" would be wrong. Suppress silently.
                    if now >= ev.start_time or now > trigger + REMINDER_CATCHUP_GRACE:
                        sent[key] = True
                        changed = True
                        continue

                    thread_url = self._thread_for(ev, threads)
                    event_link = _event_url(guild.id, ev.id)
                    await self._fire_reminder(ev, channel, event_link, thread_url)
                    sent[key] = True
                    changed = True
                except Exception:
                    LOG.exception("Reminder handling failed for event %s", getattr(ev, "id", "?"))

            # Prune ledger entries for events that are no longer scheduled (started,
            # cancelled, or deleted) so the file can't grow without bound.
            for key in list(sent.keys()):
                if key.split(":", 1)[0] not in live_ids:
                    del sent[key]
                    changed = True
            for eid in list(threads.keys()):
                if eid not in live_ids:
                    del threads[eid]
                    changed = True

            if changed:
                self._save_state(state)

    @reminder_loop.before_loop
    async def _before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _fire_reminder(
        self,
        ev: discord.ScheduledEvent,
        channel: discord.abc.Messageable,
        event_link: str,
        thread_url: Optional[str],
    ) -> None:
        # Public nudge in #general — no ping, just visible.
        public = _format_reminder(PUBLIC_REMINDER, ev.name, event_link, thread_url)
        try:
            await channel.send(public, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            LOG.exception("Failed to post public reminder for event %s", ev.id)

        # DM each subscriber (people who clicked Interested). Restart-safe: the list
        # lives on Discord. Skip silently if their DMs are closed.
        dm = _format_reminder(DM_REMINDER, ev.name, event_link, thread_url)
        try:
            async for user in ev.users():
                try:
                    await user.send(dm)
                except discord.HTTPException:
                    pass  # DMs off, blocked, or a bot — not our problem
        except discord.HTTPException:
            LOG.exception("Failed to enumerate subscribers for event %s", ev.id)

    @staticmethod
    def _thread_for(ev: discord.ScheduledEvent, threads: Dict[str, str]) -> Optional[str]:
        url = threads.get(str(ev.id))
        if url:
            return url
        # Fallback for events we didn't record (e.g. created before this feature): we
        # set the event's location to the thread jump_url, so reuse it if it looks right.
        loc = ev.location or ""
        return loc if loc.startswith("https://discord.com/channels/") else None

    # ── JSON ledger on the Railway volume ──────────────────────
    @staticmethod
    def _ensure_dir() -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            LOG.exception("Could not create events data directory: %s", DATA_DIR)

    def _load_state(self) -> Dict[str, dict]:
        self._ensure_dir()
        if not STATE_PATH.exists():
            return {"sent": {}, "threads": {}}
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return {
                "sent": dict(raw.get("sent", {})),
                "threads": dict(raw.get("threads", {})),
            }
        except Exception:
            LOG.exception("Failed to load events reminder state; starting empty.")
            return {"sent": {}, "threads": {}}

    def _save_state(self, state: Dict[str, dict]) -> None:
        self._ensure_dir()
        try:
            STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            LOG.exception("Failed to save events reminder state")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
