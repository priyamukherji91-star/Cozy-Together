# cogs/pet_care.py
# -*- coding: utf-8 -*-
"""Feeding members' registered pets, and the scoreboard that comes out of it.

The register itself lives in `petcare/pet_registry.py`; this owns the treat
ledger, the panel, and registration.

You get a handful of treats a day and there are more pets than treats, so
feeding is a choice about whose animal eats. Each pet remembers who has fed it
most — its "favourite human" — and that title is the whole game.

**Feeding has no slash command.** Everything happens through one panel, which
the cog keeps as the last message in the pet channel so nobody has to scroll
back to find it. `/pet add` stays a command because a Discord modal cannot carry
a file upload, so registering a pet with a photo has no button-shaped
equivalent.

There is no background task. The daily allowance resets by comparing a stored
date whenever the ledger is read, which needs no scheduler and cannot be missed
by a restart.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from petcare import pet_profile, pet_registry, pet_treats, storage

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
GUILD_ID = 1425974791516586045

# Pet care: the panel lives here, and so does everything you do with it.
PET_CARE_CHANNEL_ID = 1537820996390883440

# The pet gallery — where members actually post their photos. The pet system
# does not run in here; only the right-click claim reaches into it, below.
PET_PHOTO_CHANNEL_ID = 1427657614061207724

# `/pet add` belongs with the panel — registering and feeding are the same trip.
PET_REGISTER_CHANNEL_IDS: frozenset[int] = frozenset({PET_CARE_CHANNEL_ID})

# The right-click claim is the exception: it works on a photo, so it has to work
# where the photos are. Claiming from the pet channel stays allowed too.
PET_CLAIM_CHANNEL_IDS: frozenset[int] = frozenset(
    {PET_CARE_CHANNEL_ID, PET_PHOTO_CHANNEL_ID}
)

# Only this account may hand itself treats back — see `/pettreats`.
OWNER_USER_ID = 1130859582407847977

# When the daily allowance rolls over (matches morning_news.py).
TIMEZONE = "Europe/Brussels"

CLAIM_MENU_NAME = "This is my pet"

FEED_CHANNEL_ID = PET_CARE_CHANNEL_ID
LEDGER_PATH = pet_registry.DATA_DIR / "pet_treats.json"
PANEL_PATH = pet_registry.DATA_DIR / "pet_panel.json"

DAILY_TREATS = 10

# Playing is the scarce one on purpose. Feeding is routine and generous; if
# both were plentiful neither would feel like a choice.
DAILY_PLAYS = 3

# How long the channel has to go quiet before the panel moves to the bottom.
# Reposting on every message makes it strobe through a conversation and spends
# rate limit for nothing; a burst of chatter should cost exactly one repost.
REPOST_DEBOUNCE = 3.0

# Component clicks don't inherit @app_commands.checks.cooldown, so they need
# their own guard or one person can machine-gun the dropdown.
CLICK_COOLDOWN = 2.0

# Sweep the click record once it passes this many members. Comfortably above any
# plausible number of people clicking within one cooldown, so the sweep is rare.
CLICK_SWEEP_AT = 256

# How long a ticked selection waits for the Feed button before it is forgotten.
# Long enough to get distracted, short enough that yesterday's ticks can't be
# spent by a stray click today.
PICK_TTL = 600.0

# How long the private feeding list stays usable before its buttons go dead.
FEED_LIST_TIMEOUT = 300.0

# Discord's ceiling on message content, which is what the feeding list is.
MAX_LIST_LENGTH = 2000

# Hard floor between two placements. The debounce alone is not enough: any bug
# that makes the panel react to its own arrival turns into one panel every few
# seconds forever. This is the circuit breaker, and it is deliberately not
# derived from REPOST_DEBOUNCE.
MIN_PLACE_INTERVAL = 8.0

# Identifies our own panels in channel history, for clearing up duplicates.
PANEL_TITLE = "🐾 Cozy Together Pets"

# Every title the panel has ever had. A rename would otherwise strand the
# panels already sitting in the channel: the sweeper wouldn't recognise them,
# and nothing else deletes them. Keep old names here forever, they cost nothing.
PANEL_TITLES: frozenset[str] = frozenset({PANEL_TITLE})
STRAY_SCAN = 100
STRAY_DELETE_CAP = 40

# ── the panel's "Still waiting" grid ──────────────────────────────────────────
# Discord has no card, so a card is an inline field: three to a row on desktop,
# two on mobile. Six is two full desktop rows, which is as much as the panel can
# carry before it stops being glanceable.
PANEL_GRID_MAX = 6

# Emoji squares, not block characters. A `▰` in an embed takes the surrounding
# text colour, so a bar built from them is one flat grey no matter the state —
# and the state reading as *colour* is the entire point of the bar.
#
# Black is the empty segment because the embed sits on #2b2d31 for most of the
# server; a white square there reads as another filled one.
PANEL_BAR_STARVING = "🟥⬛⬛⬛⬛"
PANEL_BAR_PECKISH = "🟧🟧⬛⬛⬛"
PANEL_BAR_FINE = "🟩🟩🟩🟩⬛"

# Used when the guild has no emoji named after the pet's species.
PANEL_DEFAULT_ICON = "🐾"

COLOUR_HUNGRY = discord.Colour(0xE8A33D)   # warm amber — something is waiting
COLOUR_FED = discord.Colour(0x76C58C)      # soft green — the yard is quiet

SELECT_LIMIT = 25        # Discord's hard cap on select options
BOARD_SIZE = 10          # rows the board aims to show

# Ledgers can still carry entries for pets that were removed, and those rows
# resolve to nothing at render time. Over-fetch so the board can skip them and
# still fill up.
BOARD_FETCH = BOARD_SIZE * 3
OWNERS_PER_PAGE = 25     # same cap, applied to the owner browser
DEX_INDEX_PAGE = 20      # dex entries per index screen

EMBED_COLOUR = discord.Colour(0xE0708A)

# Never let a pet name or a display name ping a role or @everyone.
MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)

# He is a cat, and you are feeding other animals in front of him.
FEED_LINES: tuple[str, ...] = (
    "{pet} accepts your offering. {pet} has never once thanked me for anything.",
    "Fed. I notice you walked straight past me to do it.",
    "{pet} is delighted. {pet} is delighted by everything. It means nothing.",
    "You gave that to {pet}. I was here first, you know.",
    "{pet} ate it without chewing. No appreciation. No ceremony.",
    "A {treat} for {pet}, then. I'll remember this.",
    "{pet} looks up at you like you invented food. Embarrassing, for both of you.",
    "Consider {pet} fed. Consider me watching.",
    "{pet} has taken the {treat} and offered nothing in return. Learn from them.",
    "Fine. Feed {pet}. See if I care. I don't. Obviously.",
    "{pet} accepted. I could have accepted. Nobody asked me.",
    "That's twice now you've chosen {pet} over the cat who lives in your notifications.",
)

# A default favourite rather than a hardcoded rule: any pet called Donny gets a
# fishcake until somebody fills in their profile and takes the choice over.
# Matched on name rather than pet id because ids are handed out at registration.
FISHCAKE_PET_NAMES = frozenset({"donny"})

# Same register as the feed lines: he is a cat, and you are on the floor with
# somebody else's animal.
PLAY_LINES: tuple[str, ...] = (
    "{pet} has the {toy}. {pet} has always had the {toy}. Nobody plays with me.",
    "You threw the {toy}. {pet} brought it back. I would not have.",
    "Fine. Play with {pet}. I'll be here. Counting.",
    "{pet} is exhausted and delighted. I am neither, since you didn't ask.",
    "That's my {toy}, actually. It was. It isn't now.",
    "{pet} thinks you're wonderful. {pet} also thinks a paper bag is wonderful.",
    "Ten minutes with a {toy} and {pet} adores you. Cheap, isn't it.",
    "Careful with {pet}. I remember who plays and who watches.",
    "{pet} won. {pet} always wins. I'd have let you win.",
    "You're on the floor for {pet} and I'm expected to find that normal.",
    "{pet} has the {toy} and the undivided attention. I had neither, all week.",
    "Enjoy the {toy}, {pet}. I'll enjoy remembering this.",
)

# One line for the whole selection, in place of the same joke six times over.
MULTI_FEED_LINES: tuple[str, ...] = (
    "You went straight down the line without looking at me once.",
    "All {n} of them. Not one of you thought to ask whether I'd eaten.",
    "{n} animals, handed out like it costs you nothing. I notice these things.",
    "A full round. I'll be here, remembered by nobody.",
    "{n} at once. Efficient. Cold, but efficient.",
    "You've made {n} creatures very happy and one cat extremely aware of it.",
    "{n} of them fed in a single motion. I hope somebody was counting.",
)

# The panel's footer line, by state. He is a cat, watching other animals be fed.
PANEL_LINES: dict[str, tuple[str, ...]] = {
    "hungry": (
        "I have eaten nothing. Nobody asks about that.",
        "There are empty bowls in this room. I've counted. Twice.",
        "Some of them are still waiting. They'll live. I always do.",
        "A few of them haven't eaten. I mention it only in passing.",
    ),
    "fed": (
        "Every animal here has eaten today. I have eaten nothing.",
        "Not one empty bowl. The room is insufferably content.",
        "All fed. Enjoy it, it won't last the night.",
    ),
    "empty": (
        "An empty yard. Peaceful. Suspicious.",
        "Nothing to feed and nobody to blame. A rare afternoon.",
    ),
}

FAVOURITE_LINES: tuple[str, ...] = (
    "🏆 {user} is now **{pet}**'s favourite human. Sickening.",
    "🏆 {pet} has transferred their affections to {user}.",
    "🏆 {user} has bought **{pet}**'s loyalty outright. It was cheaper than you'd think.",
)

T = TypeVar("T")


# ──────────────────────────────────────────────────────────────
# Who may do what
# ──────────────────────────────────────────────────────────────
def _is_mod(user: discord.abc.User) -> bool:
    """Who may take somebody else's pet down. Matches the rest of the bot."""
    return isinstance(user, discord.Member) and user.guild_permissions.manage_messages


def _is_admin(user: discord.abc.User) -> bool:
    """Stricter: who may repost the panel by hand."""
    if not isinstance(user, discord.Member):
        return False
    perms = user.guild_permissions
    return perms.administrator or perms.manage_guild


def admin_only() -> Callable[[T], T]:
    def check(interaction: discord.Interaction) -> bool:
        if _is_admin(interaction.user):
            return True
        raise app_commands.CheckFailure("You do not have permission to use this command.")

    return app_commands.check(check)


def owner_only() -> Callable[[T], T]:
    """The narrowest rung: one account, by id."""
    def check(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_USER_ID:
            return True
        raise app_commands.CheckFailure("You do not have permission to use this command.")

    return app_commands.check(check)


def _last_treat(days: int | None) -> str:
    """How long since this pet ate, phrased as history rather than permission.

    The allowance is per member: a pet somebody else fed an hour ago is still
    yours to feed. Plain past tense about the pet carries no implication that
    there is nothing to do here — say when it ate and nothing more.
    """
    if days is None:
        return "never eaten"
    if days == 0:
        return "ate today"
    if days == 1:
        return "ate yesterday"
    return f"ate {days} days ago"


def _hunger(days: int | None) -> tuple[str, str]:
    """A five-segment bar and the word for it, for a pet nobody has fed today.

    An unreadable timestamp is drawn as the *least* alarming state, not the
    most. `_days_since` returns None rather than guessing so a broken record
    cannot invent a starving animal, and that restraint is worth nothing if the
    bar then paints it red.
    """
    if days is None:
        return PANEL_BAR_FINE, "fine"
    if days >= 3:
        return PANEL_BAR_STARVING, "starving"
    if days == 2:
        return PANEL_BAR_PECKISH, "peckish"
    return PANEL_BAR_FINE, "fine"


def _species_icon(icons: dict[str, str], pet: pet_registry.Pet) -> str:
    """A guild emoji named after the pet's species, or the shared paw.

    Every card gets *something*, even when the guild has no emoji for a hamster:
    a row where some names start with an icon and others don't stops being a
    grid, and the alignment is worth more than the variety.
    """
    key = "".join(ch for ch in (pet.species or "").lower() if ch.isalnum())
    if key and key in icons:
        return icons[key]
    return PANEL_DEFAULT_ICON


def _next_reset_unix() -> int:
    """Midnight tonight, in the timezone the allowance actually rolls over in."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.datetime.now(tz)
    tomorrow = (now + datetime.timedelta(days=1)).date()
    return int(
        datetime.datetime.combine(tomorrow, datetime.time.min, tzinfo=tz).timestamp()
    )


def _panel_line(state: str) -> str:
    """One line of Mittens, picked from the state and the hour.

    Deterministic on purpose. The panel is rebuilt every time somebody talks in
    the channel, and a line that rerolled each time would be the only thing on
    screen changing while nothing had happened. Keyed on the hour instead, so it
    moves through the day and holds still in between.

    Built from ord() rather than hash(), which is salted per process and would
    hand a different line to every restart.
    """
    pool = PANEL_LINES.get(state) or PANEL_LINES["hungry"]
    hour = datetime.datetime.now(ZoneInfo(TIMEZONE)).hour
    return pool[(hour + sum(ord(c) for c in state)) % len(pool)]


def _eats_fishcake(name: str) -> bool:
    """Case- and punctuation-insensitive, so "donny" and "Donny 🐟" both match."""
    words = re.findall(r"[a-z0-9]+", name.casefold())
    return any(w in FISHCAKE_PET_NAMES for w in words)


class FeedError(Exception):
    """Something the member should be told in plain words."""


@dataclass(frozen=True)
class FeedResult:
    total: int              # the pet's lifetime treats
    treats_left: int        # the feeder's remaining allowance today
    from_you: int           # how many of that pet's treats came from this feeder
    new_favourite: bool     # did this feed take the title
    treat: str = ""         # what actually got handed over
    perfect: bool = False   # was it the one this pet wanted


@dataclass(frozen=True)
class FedOne:
    """One pet inside a multi-pet feed."""
    pet_id: str
    total: int              # the pet's lifetime treats
    from_you: int           # how many of them came from this feeder
    new_favourite: bool     # did this feed take the title
    treat: str              # what actually got handed over
    perfect: bool           # was it the one this pet wanted


@dataclass(frozen=True)
class MultiFeedResult:
    """What came of a whole selection: what ate, and what didn't, and why."""
    fed: list[FedOne]
    already: list[str]      # pet ids skipped — this feeder fed them today
    no_treats: list[str]    # pet ids that didn't fit in what was left
    treats_left: int


@dataclass(frozen=True)
class PlayResult:
    total: int              # times this pet has been played with, ever
    plays_left: int         # the player's remaining plays today
    from_you: int           # how many of those were this player
    toy: str                # what they played with
    own_toy: bool           # was it the pet's own favourite


# ──────────────────────────────────────────────────────────────
# The treat ledger
# ──────────────────────────────────────────────────────────────
# Feeds are handled on worker threads, so read-modify-write needs guarding or two
# people feeding at once lose one of the treats.
_LOCK = threading.Lock()


def _today() -> str:
    return datetime.datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


# Read cache, the same one `pet_registry` uses and for the same reason: nearly
# every panel render, board, dex page and allowance check read this whole file
# off the volume, several times per click.
#
# The text is cached rather than the parsed object, which matters more here than
# it does for the registry — `_guild_block` and `_feeder_today` both *write* into
# what `_load` hands back (creating the guild block, rolling the day over), and
# they are called by plain readers on worker threads. Sharing one dict between
# them would be a data race over an object nobody has saved.
_CACHE_LOCK = threading.Lock()
_cache_key: tuple[Any, ...] | None = None
_cache_text: str = ""
_writes = 0


def _cache_stamp() -> tuple[Any, ...]:
    try:
        st = LEDGER_PATH.stat()
        return (st.st_mtime_ns, st.st_size, _writes)
    except OSError:
        return ("missing", _writes)


def _load() -> dict[str, Any]:
    global _cache_key, _cache_text

    stamp = _cache_stamp()
    with _CACHE_LOCK:
        cached = _cache_text if (_cache_key == stamp and _cache_text) else ""

    if cached:
        try:
            data = json.loads(cached)
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass

    data = storage.load_json(LEDGER_PATH, default={})
    if not isinstance(data, dict):
        data = {}

    try:
        text = json.dumps(data)
    except (TypeError, ValueError):
        text = ""
    with _CACHE_LOCK:
        _cache_key, _cache_text = stamp, text

    return data


def _save(data: dict[str, Any]) -> bool:
    """Every write to the ledger goes through here, so the cache can't go stale."""
    global _writes
    ok = storage.save_json(LEDGER_PATH, data)
    _writes += 1
    return ok


def _guild_block(data: dict[str, Any], guild_id: int) -> dict[str, Any]:
    block = data.setdefault(str(guild_id), {})
    block.setdefault("pets", {})
    block.setdefault("feeders", {})
    return block


def _feeder_today(block: dict[str, Any], user_id: int) -> dict[str, Any]:
    """The feeder's record, rolled over to today if it's stale."""
    feeders = block["feeders"]
    rec = feeders.setdefault(str(user_id), {})
    if rec.get("date") != _today():
        rec["date"] = _today()
        rec["spent"] = 0
        rec["fed"] = []
        rec["plays"] = 0
        rec["played"] = []
    rec.setdefault("lifetime", 0)
    rec.setdefault("plays", 0)
    rec.setdefault("played", [])
    return rec


def treats_left(guild_id: int, user_id: int) -> tuple[int, list[str]]:
    """Remaining allowance and the pet ids already fed today."""
    data = _load()
    block = _guild_block(data, guild_id)
    rec = _feeder_today(block, user_id)
    return max(0, DAILY_TREATS - int(rec.get("spent", 0))), list(rec.get("fed", []))


def feed_many(
    guild_id: int,
    user_id: int,
    pet_ids: list[str],
    favourites: dict[str, str] | None = None,
) -> MultiFeedResult:
    """Feed a whole selection under one lock and one write.

    The feeding list returns everything that was ticked, so this is the real
    entry point and `feed` is a single-pet wrapper over it. Doing it per pet
    instead would take the lock and fsync the ledger ten times for one click,
    and leave a half-fed selection behind if the disk gave out in the middle.

    Nothing here raises for a pet it can't feed. A selection is not an
    all-or-nothing request — feeding eight of the ten you ticked is the right
    answer — so the ones that don't happen come back named, and the caller says
    so. It only raises if the write itself fails.
    """
    favourites = favourites or {}

    with _LOCK:
        data = _load()
        block = _guild_block(data, guild_id)
        rec = _feeder_today(block, user_id)
        pets = block["pets"]

        spent = int(rec.get("spent", 0))
        already_today: list[str] = list(rec.get("fed", []))

        fed: list[FedOne] = []
        already: list[str] = []
        no_treats: list[str] = []

        for pet_id in pet_ids:
            if pet_id in already_today:
                already.append(pet_id)
                continue
            if spent >= DAILY_TREATS:
                no_treats.append(pet_id)
                continue

            given, own_favourite = pet_treats.pick(favourites.get(pet_id, ""))

            stats = pets.setdefault(pet_id, {"total": 0, "by": {}, "last_fed": None})
            by: dict[str, Any] = stats.setdefault("by", {})
            previous_favourite = _favourite_id(by)

            stats["total"] = int(stats.get("total", 0)) + 1
            by[str(user_id)] = int(by.get(str(user_id), 0)) + 1
            stats["last_fed"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            )

            spent += 1
            already_today.append(pet_id)
            now_favourite = _favourite_id(by)

            fed.append(FedOne(
                pet_id=pet_id,
                total=int(stats["total"]),
                from_you=int(by[str(user_id)]),
                new_favourite=(
                    now_favourite == str(user_id) and previous_favourite != now_favourite
                ),
                treat=given,
                perfect=own_favourite,
            ))

        if fed:
            rec["spent"] = spent
            rec["fed"] = already_today
            rec["lifetime"] = int(rec.get("lifetime", 0)) + len(fed)

            if not _save(data):
                raise FeedError("I couldn't write that down. Try again in a moment.")

        return MultiFeedResult(
            fed=fed,
            already=already,
            no_treats=no_treats,
            treats_left=max(0, DAILY_TREATS - spent),
        )


def feed(
    guild_id: int,
    user_id: int,
    pet_id: str,
    favourite: str = "",
) -> FeedResult:
    """One pet, with the refusals raised as messages. `feed_many` does the work.

    Kept because the dex and the search results still feed one at a time, and
    they want to be told *why* nothing happened rather than handed an empty
    result to interpret.
    """
    result = feed_many(guild_id, user_id, [pet_id], {pet_id: favourite})

    if result.already:
        raise FeedError("You've already fed them today. Spread it around.")
    if result.no_treats:
        raise FeedError(
            f"You're out of treats for today — you get {DAILY_TREATS}. "
            "They come back at midnight."
        )
    if not result.fed:
        raise FeedError("I couldn't write that down. Try again in a moment.")

    one = result.fed[0]
    return FeedResult(
        total=one.total,
        treats_left=result.treats_left,
        from_you=one.from_you,
        new_favourite=one.new_favourite,
        treat=one.treat,
        perfect=one.perfect,
    )


def plays_left(guild_id: int, user_id: int) -> tuple[int, list[str]]:
    """Remaining plays and the pet ids already played with today."""
    block = _guild_block(_load(), guild_id)
    rec = _feeder_today(block, user_id)
    return max(0, DAILY_PLAYS - int(rec.get("plays", 0))), list(rec.get("played", []))


def play(guild_id: int, user_id: int, pet_id: str, favourite_toy: str = "") -> PlayResult:
    """Play with a pet. Mirrors `feed`, on its own allowance and its own tallies.

    Kept separate from the treat ledger rather than folded in: the two have
    different limits and different meanings, and the favourite-human race stays
    about food so it can't be won by whoever clicks the newer button.
    """
    with _LOCK:
        data = _load()
        block = _guild_block(data, guild_id)
        rec = _feeder_today(block, user_id)

        spent = int(rec.get("plays", 0))
        if spent >= DAILY_PLAYS:
            raise FeedError(
                f"You've played enough for today — you get {DAILY_PLAYS}. "
                "More tomorrow."
            )
        if pet_id in rec.get("played", []):
            raise FeedError("You've already played with them today. Try somebody else.")

        given, own_toy = pet_treats.pick_toy(favourite_toy)

        pets = block["pets"]
        stats = pets.setdefault(pet_id, {"total": 0, "by": {}, "last_fed": None})
        play_by: dict[str, Any] = stats.setdefault("play_by", {})

        stats["plays"] = int(stats.get("plays", 0)) + 1
        play_by[str(user_id)] = int(play_by.get(str(user_id), 0)) + 1
        stats["last_played"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )

        rec["plays"] = spent + 1
        rec.setdefault("played", []).append(pet_id)

        if not _save(data):
            raise FeedError("I couldn't write that down. Try again in a moment.")

        return PlayResult(
            total=stats["plays"],
            plays_left=DAILY_PLAYS - rec["plays"],
            from_you=play_by[str(user_id)],
            toy=given,
            own_toy=own_toy,
        )


def refill(guild_id: int, user_id: int) -> tuple[int, int]:
    """Hand today's allowance back. Returns (treats, plays) now available.

    A reset rather than a standing exemption. An "ignore the limits" flag would
    have to be remembered, and a flag like that is only ever noticed again when
    somebody wonders why one person's numbers look impossible — this leaves no
    state behind at all, and the limits keep working the moment it's spent.

    The list of pets fed today goes with it, so the same pets can be fed again;
    that list is what the feeding list reads to decide who is still available.
    """
    with _LOCK:
        data = _load()
        block = _guild_block(data, guild_id)
        rec = _feeder_today(block, user_id)
        rec["spent"] = 0
        rec["fed"] = []
        rec["plays"] = 0
        rec["played"] = []
        if not _save(data):
            raise FeedError("I couldn't write that down. Try again in a moment.")
        return DAILY_TREATS, DAILY_PLAYS


def forget_pet(guild_id: int, pet_id: str) -> bool:
    """Drop a removed pet's ledger entry. True once it is dropped *and* written.

    Leaving the totals behind goes wrong two ways: the confirmation promises the
    treat history goes too, and `top_pets` would hand the board a row whose pet
    no longer resolves — the renderer skips it, so a board asking for ten
    silently draws nine.

    False covers two situations that are not remotely alike, so they are logged
    apart. A pet nobody ever fed has no entry to drop, which is the expected
    ending and worth nothing louder than a debug line. A failed write is the
    other one: the pet is gone from the registry and its totals are still in
    here, which is exactly the ghost row above.

    Today's allowance is deliberately left alone. A treat already spent on this
    pet stays spent; refunding it on removal would let anyone re-roll their day
    by registering a pet, feeding it and deleting it.
    """
    with _LOCK:
        data = _load()
        block = _guild_block(data, guild_id)
        if block["pets"].pop(pet_id, None) is None:
            log.debug("[pets] %s had no ledger entry to drop", pet_id)
            return False
        if not _save(data):
            log.warning(
                "[pets] %s was removed but its ledger entry could not be dropped — "
                "the board will skip it", pet_id
            )
            return False
        return True


def _favourite_id(by: dict[str, Any]) -> str | None:
    """Whoever has fed this pet most, or None if nobody leads outright.

    A tie deliberately has no favourite: announcing a title that flips back and
    forth on every feed would be noise, and "joint favourite" isn't a thing a cat
    would recognise.
    """
    if not by:
        return None
    ranked = sorted(by.items(), key=lambda kv: (-int(kv[1]), kv[0]))
    if len(ranked) > 1 and int(ranked[0][1]) == int(ranked[1][1]):
        return None
    return ranked[0][0]


def pet_stats(guild_id: int, pet_id: str) -> dict[str, Any]:
    block = _guild_block(_load(), guild_id)
    return block["pets"].get(pet_id, {"total": 0, "by": {}, "last_fed": None})


def top_pets(guild_id: int, limit: int = 10) -> list[tuple[str, int, str | None]]:
    """Best-fed pets as (pet_id, treats, favourite_user_id).

    The favourite comes back with the row rather than being looked up per pet
    afterwards, so rendering the board reads the ledger once instead of N times.
    """
    block = _guild_block(_load(), guild_id)
    rows = [
        (pid, int(s.get("total", 0)), _favourite_id(s.get("by", {})))
        for pid, s in block["pets"].items()
    ]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return [r for r in rows if r[1] > 0][:limit]


def top_feeders(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    block = _guild_block(_load(), guild_id)
    rows = [(int(uid), int(r.get("lifetime", 0))) for uid, r in block["feeders"].items()]
    rows.sort(key=lambda kv: (-kv[1], kv[0]))
    return [r for r in rows if r[1] > 0][:limit]


def dex_caught(guild_id: int, user_id: int, pet_ids: list[str]) -> set[str]:
    """Which of these pets this member has ever fed, at any point.

    Distinct pets, not treats — the ledger's per-feeder tallies already hold it,
    so dex completion stores nothing new. A set rather than a count, because the
    jump menu marks every entry and the footer only needs its length.
    """
    block = _guild_block(_load(), guild_id)
    stats = block["pets"]
    uid = str(user_id)
    return {
        pid for pid in pet_ids
        if int(((stats.get(pid) or {}).get("by") or {}).get(uid, 0)) > 0
    }


def treats_given_today(guild_id: int) -> int:
    block = _guild_block(_load(), guild_id)
    today = _today()
    return sum(
        int(r.get("spent", 0))
        for r in block["feeders"].values()
        if r.get("date") == today
    )


def _days_since(stamp: str | None) -> int | None:
    """Whole days since an ISO timestamp, or None if it doesn't parse.

    None rather than a guess, so a bad record can't fake a starving pet.
    """
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0, (datetime.datetime.now(datetime.timezone.utc) - when).days)


def hungriest(
    guild_id: int, pets: list[pet_registry.Pet]
) -> list[tuple[pet_registry.Pet, int | None]]:
    """Every pet with its days-since-fed, hungriest first.

    Reads the ledger once for the whole list — the panel re-renders often enough
    that a per-pet lookup would mean N file reads every time somebody talks.
    Registration date stands in for pets that have never been fed.
    """
    block = _guild_block(_load(), guild_id)
    stats = block["pets"]
    out: list[tuple[pet_registry.Pet, int | None]] = []
    for p in pets:
        raw = stats.get(p.pet_id) or {}
        out.append((p, _days_since(raw.get("last_fed") or p.added)))
    out.sort(key=lambda pair: (-(pair[1] if pair[1] is not None else -1), pair[0].name.casefold()))
    return out


# ──────────────────────────────────────────────────────────────
# Panel bookkeeping
# ──────────────────────────────────────────────────────────────
def _load_panel() -> dict[str, Any]:
    data = storage.load_json(PANEL_PATH, default={})
    return data if isinstance(data, dict) else {}


def _remember_panel(guild_id: int, channel_id: int, message_id: int) -> None:
    data = _load_panel()
    data[str(guild_id)] = {"channel_id": channel_id, "message_id": message_id}
    storage.save_json(PANEL_PATH, data)


def _recall_panel(guild_id: int) -> tuple[int, int] | None:
    rec = _load_panel().get(str(guild_id))
    if not isinstance(rec, dict):
        return None
    try:
        return int(rec["channel_id"]), int(rec["message_id"])
    except (KeyError, TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────
# Components
# ──────────────────────────────────────────────────────────────
class AboutButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"petcare:about:(?P<pet_id>[0-9a-f]{1,32})",
):
    """Rides on every feed post, so a pet's record is one click from the feeding.

    Dynamic rather than a plain button because feed posts outlive the process —
    nothing sweeps them — and a restart would otherwise leave a channel full of
    buttons that answer "this interaction failed".
    """

    def __init__(self, pet_id: str, label: str) -> None:
        self.pet_id = pet_id
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"petcare:about:{pet_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(match["pet_id"], item.label or "🐾 About")

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("PetCare")
        if isinstance(cog, PetCare):
            await cog.send_about(interaction, self.pet_id)


class DexButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"petcare:dex:(?P<page>\d{1,4})",
):
    """Opens the pet dex. Dynamic for the same reason `AboutButton` is — it sits
    on public posts that outlive the process."""

    def __init__(self, page: int = 0) -> None:
        self.page = page
        super().__init__(
            discord.ui.Button(
                label="📖 Pet dex",
                style=discord.ButtonStyle.secondary,
                custom_id=f"petcare:dex:{page}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["page"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("PetCare")
        if isinstance(cog, PetCare):
            await cog.open_dex(interaction, self.page)


def _jump_options(
    entries: list[tuple[int, pet_registry.Pet, str, bool]], around: int
) -> list[discord.SelectOption]:
    """Up to 25 dex entries for a jump menu, windowed around the current one.

    A select caps at 25. Rather than only ever offering the first 25, the window
    slides with you, so page 40 of a large dex can still reach its neighbours.
    """
    if len(entries) > SELECT_LIMIT:
        start = max(0, min(around - SELECT_LIMIT // 2, len(entries) - SELECT_LIMIT))
        entries = entries[start:start + SELECT_LIMIT]
    return [
        discord.SelectOption(
            label=f"#{n:03d} · {pet.name}"[:100],
            value=str(n - 1),
            description=f"{who}'s · {'fed by you' if caught else 'not fed by you'}"[:100],
            default=(n - 1) == around,
        )
        for n, pet, who, caught in entries
    ]


class DexView(discord.ui.View):
    """One pet per page. The dex reads, it does not act.

    No Feed button: with feeding a list of its own and editing behind Manage my
    pets, it would be a third place to do the same thing, and one that quietly
    bypassed the treat list. Reference material and the controls that spend
    things are better kept apart.

    Arrows alone meant walking the whole list to reach the end, so there is also
    a jump menu and a way out to the index.
    """

    def __init__(
        self,
        cog: "PetCare",
        page: int,
        total: int,
        pet_id: str,
        entries: list[tuple[int, pet_registry.Pet, str, bool]],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.page = page
        self.total = total
        self.pet_id = pet_id

        prev = discord.ui.Button(
            label="◀", style=discord.ButtonStyle.secondary, row=0, disabled=page == 0
        )
        prev.callback = self._prev
        self.add_item(prev)

        nxt = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, row=0,
            disabled=page >= total - 1,
        )
        nxt.callback = self._next
        self.add_item(nxt)

        jump = discord.ui.Select(
            placeholder="Jump to…", row=1, options=_jump_options(entries, page)
        )
        jump.callback = self._jump
        self.add_item(jump)

        # The dex is private, so this is the only way a pet you are looking at
        # reaches anybody else. Primary, because it is the one thing on this
        # screen that leaves a mark on the channel.
        show = discord.ui.Button(
            label="📢 Show everyone", style=discord.ButtonStyle.primary, row=2
        )
        show.callback = self._show
        self.add_item(show)

        index = discord.ui.Button(
            label="📋 All pets", style=discord.ButtonStyle.secondary, row=2
        )
        index.callback = self._index
        self.add_item(index)

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dex(interaction, self.page - 1, edit=True)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dex(interaction, self.page + 1, edit=True)

    async def _show(self, interaction: discord.Interaction) -> None:
        await self.cog.post_bio(interaction, self.pet_id)

    async def _index(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dex_index(interaction, self.page // DEX_INDEX_PAGE)

    async def _jump(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await interaction.response.defer()
        await self.cog.open_dex(interaction, int(values[0]), edit=True)


class DexIndexView(discord.ui.View):
    """The whole dex on one screen — the fastest way to find anything in it."""

    def __init__(
        self,
        cog: "PetCare",
        entries: list[tuple[int, pet_registry.Pet, str, bool]],
        page: int,
        pages: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.page = page

        open_sel = discord.ui.Select(
            placeholder="Open an entry…",
            row=0,
            options=_jump_options(entries, entries[0][0] - 1 if entries else 0),
        )
        open_sel.callback = self._open
        self.add_item(open_sel)

        if pages > 1:
            prev = discord.ui.Button(
                label="◀", style=discord.ButtonStyle.secondary, row=1, disabled=page == 0
            )
            prev.callback = self._prev
            self.add_item(prev)

            nxt = discord.ui.Button(
                label="▶", style=discord.ButtonStyle.secondary, row=1,
                disabled=page >= pages - 1,
            )
            nxt.callback = self._next
            self.add_item(nxt)

    async def _open(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await interaction.response.defer()
        await self.cog.open_dex(interaction, int(values[0]), edit=True)

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dex_index(interaction, self.page - 1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self.cog.open_dex_index(interaction, self.page + 1)


class PlayPickView(discord.ui.View):
    """Who to play with. Its own picker rather than the feeding list, which
    belongs to feeding — one menu doing two things would be a menu you have to
    read twice."""

    def __init__(
        self,
        cog: "PetCare",
        guild: discord.Guild,
        entries: list[tuple[pet_registry.Pet, int]],
        played: set[str],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog

        options: list[discord.SelectOption] = []
        for pet, plays in entries:
            if pet.pet_id in played:
                continue
            owner = guild.get_member(pet.owner_id)
            who = owner.display_name if owner else "someone who left"
            if plays == 0:
                note = "never been played with"
            elif plays == 1:
                note = "played with once"
            else:
                note = f"played with {plays} times"
            options.append(
                discord.SelectOption(
                    label=pet.name[:100],
                    value=pet.pet_id,
                    description=f"{who}'s · {note}"[:100],
                )
            )
            if len(options) == SELECT_LIMIT:
                break

        select = discord.ui.Select(
            placeholder=(
                "Who wants the toy?" if options
                else "You've played with everyone today"
            ),
            options=options or [discord.SelectOption(label="—", value="none")],
            disabled=not options,
        )
        select.callback = self._pick
        self.add_item(select)

    async def _pick(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or values[0] == "none":
            return await interaction.response.defer()
        await self.cog.play_pet(interaction, values[0])


class ManageListView(discord.ui.View):
    """Your own pets, and nobody else's."""

    def __init__(
        self, cog: "PetCare", entries: list[tuple[pet_registry.Pet, int | None]]
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog

        select = discord.ui.Select(
            placeholder="Which of yours?",
            row=0,
            options=[
                discord.SelectOption(
                    label=pet.name[:100],
                    value=pet.pet_id,
                    description=_last_treat(days)[:100],
                )
                for pet, days in entries[:SELECT_LIMIT]
            ] or [discord.SelectOption(label="—", value="none")],
            disabled=not entries,
        )
        select.callback = self._pick
        self.add_item(select)

    async def _pick(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or values[0] == "none":
            return await interaction.response.defer()
        await self.cog.show_manage_pet(interaction, values[0])


class ManagePetView(discord.ui.View):
    """One of your pets, with the destructive thing behind a confirmation."""

    def __init__(self, cog: "PetCare", pet_id: str, confirming: bool = False) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.pet_id = pet_id

        if confirming:
            yes = discord.ui.Button(label="Yes, remove them", style=discord.ButtonStyle.danger)
            yes.callback = self._confirm
            self.add_item(yes)

            no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            no.callback = self._cancel
            self.add_item(no)
            return

        edit = discord.ui.Button(label="✏️ Edit bio", style=discord.ButtonStyle.primary)
        edit.callback = self._edit
        self.add_item(edit)

        rename = discord.ui.Button(label="🏷️ Rename", style=discord.ButtonStyle.secondary)
        rename.callback = self._rename
        self.add_item(rename)

        remove = discord.ui.Button(label="🗑️ Remove", style=discord.ButtonStyle.danger)
        remove.callback = self._ask
        self.add_item(remove)

        back = discord.ui.Button(label="◀ My pets", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _edit(self, interaction: discord.Interaction) -> None:
        await self.cog.open_editor(interaction, self.pet_id)

    async def _rename(self, interaction: discord.Interaction) -> None:
        await self.cog.open_rename(interaction, self.pet_id)

    async def _ask(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage_pet(interaction, self.pet_id, confirming=True)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self.cog.show_manage_pet(interaction, self.pet_id)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.remove_pet(interaction, self.pet_id)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog.open_manage(interaction, edit=True)


class FeedListView(discord.ui.View):
    """Your own feeding list, with the boxes staying ticked as you go.

    This exists because the panel cannot do it. The panel is one shared message,
    so marking an option as ticked there would show your choices to everybody
    and hand your selection to whoever pressed Feed next. Discord also clears a
    select's ticks the moment the menu closes, and the message is the only place
    that state could otherwise live.

    An ephemeral message belongs to exactly one member, which means it can be
    re-rendered after every tick with the boxes still filled in — the list below
    and the menu above are redrawn together, so what you have chosen is on
    screen the whole time rather than remembered.
    """

    def __init__(
        self,
        cog: "PetCare",
        guild: discord.Guild,
        feedable: list[tuple[pet_registry.Pet, int | None]],
        picked: set[str],
        allowance: int,
    ) -> None:
        super().__init__(timeout=FEED_LIST_TIMEOUT)
        self.cog = cog

        # Only what you can still feed. A pet you have fed today is named under
        # the list instead — offering it here would be offering a refusal.
        options: list[discord.SelectOption] = []
        for pet, _days in feedable[:SELECT_LIMIT]:
            owner = guild.get_member(pet.owner_id)
            who = owner.display_name if owner else "someone who left"
            options.append(
                discord.SelectOption(
                    label=pet.name[:100],
                    value=pet.pet_id,
                    description=f"{who}'s"[:100],
                    default=pet.pet_id in picked,
                )
            )

        # min_values=0 so unticking everything is allowed — otherwise the only
        # way out of a selection is to feed it.
        select = discord.ui.Select(
            placeholder=(
                "Tick the ones to feed…" if options
                else "You've fed everyone here today"
            ),
            min_values=0,
            max_values=max(1, min(DAILY_TREATS, len(options))),
            row=0,
            options=options or [discord.SelectOption(label="—", value="none")],
            disabled=not options,
        )
        select.callback = self._on_tick
        self.add_item(select)

        count = len(picked)
        feed_btn = discord.ui.Button(
            label=f"🍖 Feed the {count} ticked" if count else "🍖 Nothing ticked yet",
            style=discord.ButtonStyle.primary if count else discord.ButtonStyle.secondary,
            row=1,
            disabled=count == 0 or allowance <= 0,
        )
        feed_btn.callback = self._on_feed
        self.add_item(feed_btn)

    async def _on_tick(self, interaction: discord.Interaction) -> None:
        values = [v for v in ((interaction.data or {}).get("values") or []) if v != "none"]
        await self.cog.update_feed_list(interaction, values)

    async def _on_feed(self, interaction: discord.Interaction) -> None:
        await self.cog.feed_from_list(interaction)


class FoodBowlView(discord.ui.View):
    """The panel's controls. Rebuilt on every render so the list is never stale.

    `timeout=None` because the message it rides on is meant to sit there; the
    view object is replaced whenever the panel moves, so nothing accumulates.

    An empty one is also registered at `setup()` as a persistent view, which is
    what keeps the *previous* process's panel alive. `_boot` replaces that panel
    within a second or two of the bot coming up, but anyone who clicks inside
    that window — or at any point after a `_boot` that could not reach the
    channel — would otherwise get "this interaction failed". Registering costs
    nothing because no state has to survive with it: every custom_id here is
    fixed, and the callbacks read what they need out of the interaction rather
    than out of the view, so a shell with no options still routes a real click.
    """

    def __init__(
        self,
        cog: "PetCare",
        guild: discord.Guild | None,
        entries: list[tuple[pet_registry.Pet, int | None]],
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog

        options: list[discord.SelectOption] = []
        for pet, days in entries[:SELECT_LIMIT]:
            owner = guild.get_member(pet.owner_id) if guild else None
            who = owner.display_name if owner else "someone who left"
            options.append(
                discord.SelectOption(
                    label=pet.name[:100],
                    value=pet.pet_id,
                    description=f"{who}'s · {_last_treat(days)}"[:100],
                )
            )

        # For the placement log. The panel no longer picks anything itself — the
        # menu moved into the private list, where a tick can stay ticked.
        self.option_count = len(options)
        self.max_pick = min(DAILY_TREATS, max(1, len(options)))

    # First row, with Play and the treat bag: the three that do something.
    # A dropdown lived here and it was the whole problem: Discord wipes a
    # select's ticks when the menu closes and a shared message cannot hold one
    # person's choices, so ticking left nothing on screen and the feature looked
    # like it had never shipped.
    @discord.ui.button(
        label="🍖 Feed pets", style=discord.ButtonStyle.primary, row=0,
        custom_id="petcare:panel:feed",
    )
    async def feed_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.open_feed_list(interaction)

    @discord.ui.button(
        label="🧸 Play", style=discord.ButtonStyle.secondary, row=0,
        custom_id="petcare:panel:play",
    )
    async def play_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.open_play(interaction)

    @discord.ui.button(
        label="🍬 Treat bag", style=discord.ButtonStyle.secondary, row=0,
        custom_id="petcare:panel:treats",
    )
    async def treats_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.send_treats(interaction)

    @discord.ui.button(
        label="🏆 Board", style=discord.ButtonStyle.secondary, row=1,
        custom_id="petcare:panel:board",
    )
    async def board_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.send_board(interaction)

    @discord.ui.button(
        label="📖 Pet dex", style=discord.ButtonStyle.secondary, row=1,
        custom_id="petcare:panel:dex",
    )
    async def dex_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.open_dex(interaction)

    # Second row. Six buttons split three and three rather than five and one:
    # five per row is Discord's limit, and filling one row leaves the sixth
    # sitting on its own looking like an afterthought. The split is also the
    # honest grouping — things you do above, things you look at below.
    @discord.ui.button(
        label="🐾 Manage my pets", style=discord.ButtonStyle.secondary, row=1,
        custom_id="petcare:panel:manage",
    )
    async def manage_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.open_manage(interaction)


# ──────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────
class ClaimPetModal(discord.ui.Modal):
    """Registration, from either route. Name, species and year — nothing else.

    Deliberately the short half of the profile: making somebody fill in seven
    boxes before their pet exists is how you end up with no pets registered. The
    rest is offered on a button straight afterwards, and lives on the pet's card
    from then on.
    """

    def __init__(self, cog: "PetCare", image: discord.Attachment) -> None:
        super().__init__(title="Register this pet")
        self.cog = cog
        self.image = image

        self.pet_name = discord.ui.TextInput(
            label="What are they called?",
            max_length=pet_registry.MAX_NAME_LENGTH,
            required=True,
        )
        self.add_item(self.pet_name)

        self.boxes: list[tuple[str, discord.ui.TextInput]] = []
        for field in pet_profile.BASICS:
            box = discord.ui.TextInput(
                label=field.label,
                placeholder=field.placeholder,
                max_length=field.cap,
                required=False,
            )
            self.add_item(box)
            self.boxes.append((field.key, box))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            raw = await self.image.read()
        except Exception:
            log.exception("[pets] could not read claimed attachment")
            return await interaction.followup.send(
                "❌ I couldn't download that photo.", ephemeral=True
            )
        profile = {key: str(box.value or "") for key, box in self.boxes}
        await self.cog._register(interaction, str(self.pet_name), raw, profile)


class MoreDetailsView(discord.ui.View):
    """Offered right after registering, so the character half is one click away
    rather than something you have to go and find later."""

    def __init__(self, pet: pet_registry.Pet) -> None:
        super().__init__(timeout=600)
        self.pet = pet

        button = discord.ui.Button(
            label="✨ Add what they're like", style=discord.ButtonStyle.primary
        )
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction: discord.Interaction) -> None:
        async def save(
            inter: discord.Interaction, values: dict[str, str], _name: str | None
        ) -> None:
            try:
                await asyncio.to_thread(
                    pet_registry.save_profile, self.pet.guild_id, self.pet.pet_id, values
                )
            except pet_registry.PetError as e:
                return await inter.response.send_message(f"❌ {e}", ephemeral=True)
            await inter.response.send_message(
                f"✅ **{self.pet.name}**'s profile is saved.", ephemeral=True
            )

        await interaction.response.send_modal(pet_profile.bio_modal(save, self.pet))


# ──────────────────────────────────────────────────────────────
# The cog
# ──────────────────────────────────────────────────────────────
class PetCare(commands.Cog):
    """🍖 Feeding other people's animals, competitively."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._panel: discord.Message | None = None
        self._repost: asyncio.Task[None] | None = None
        self._clicks: dict[int, float] = {}
        # user id -> (when it was ticked, pet ids) — see `update_feed_list`.
        self._picked: dict[int, tuple[float, list[str]]] = {}
        self._lock = asyncio.Lock()
        self._last_place = 0.0

        # Context menus can't use decorators inside a Cog, so build it by hand.
        self._ctx_menu: app_commands.ContextMenu | None = app_commands.ContextMenu(
            name=CLAIM_MENU_NAME, callback=self.claim_pet_from_message
        )
        try:
            bot.tree.add_command(self._ctx_menu)
        except Exception:
            # Losing the right-click entry beats losing the whole cog.
            log.exception("[pets] could not register the %r menu", CLAIM_MENU_NAME)

    async def cog_load(self) -> None:
        # asyncio.create_task rather than bot.loop.create_task: `bot.loop` is
        # only set once the client has logged in, and a cog can be loaded before
        # that. This is always called from inside the loop, so it is safe either
        # way round.
        asyncio.create_task(self._boot())

    async def cog_unload(self) -> None:
        if self._repost and not self._repost.done():
            self._repost.cancel()
        if self._ctx_menu is not None:
            kind = self._ctx_menu.type
            self.bot.tree.remove_command(CLAIM_MENU_NAME, type=kind)
            self.bot.tree.remove_command(
                CLAIM_MENU_NAME, type=kind, guild=discord.Object(id=GUILD_ID)
            )
            self._ctx_menu = None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _channel(self) -> discord.TextChannel | None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return None
        channel = guild.get_channel(FEED_CHANNEL_ID)
        return channel if isinstance(channel, discord.TextChannel) else None

    def _too_fast(self, user_id: int) -> bool:
        now = time.monotonic()
        last = self._clicks.get(user_id, 0.0)
        if now - last < CLICK_COOLDOWN:
            return True
        self._clicks[user_id] = now
        # Entries older than the cooldown can never deny anyone, so keeping them
        # only grows the dict by one per member who has ever clicked. Swept here
        # rather than on a timer — this is the only thing that adds to it.
        if len(self._clicks) > CLICK_SWEEP_AT:
            cutoff = now - CLICK_COOLDOWN
            self._clicks = {
                uid: seen for uid, seen in self._clicks.items() if seen > cutoff
            }
        return False

    async def _resolve(self, guild: discord.Guild, token: str) -> pet_registry.Pet | None:
        pet = await asyncio.to_thread(pet_registry.get_pet, guild.id, token)
        if pet is None:
            pet = await asyncio.to_thread(pet_registry.find_by_name, guild.id, token)
        return pet

    def _thumb(self, pet: pet_registry.Pet) -> discord.File | None:
        raw = pet_registry.image_bytes(pet)
        if raw is None:
            return None
        return discord.File(io.BytesIO(raw), filename="pet.png")

    @staticmethod
    async def _deny(interaction: discord.Interaction, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    # ── the panel ─────────────────────────────────────────────────────────────

    async def _build_panel(
        self, guild: discord.Guild
    ) -> tuple[discord.Embed, FoodBowlView]:
        pets = await asyncio.to_thread(pet_registry.all_pets, guild.id)
        entries = await asyncio.to_thread(hungriest, guild.id, pets)
        given = await asyncio.to_thread(treats_given_today, guild.id)

        # A pet counts as waiting when nobody has fed it today. Everything on
        # this panel is server-wide for the same reason it always was: one
        # shared message cannot show a different number to each reader.
        waiting = [(p, d) for p, d in entries if d is None or d >= 1]
        state = "empty" if not entries else ("fed" if not waiting else "hungry")

        embed = discord.Embed(
            title=PANEL_TITLE,
            colour=COLOUR_FED if state == "fed" else COLOUR_HUNGRY,
            description=(
                "Everyone has their own treats. A pet someone else fed today "
                "is still yours to feed."
                if entries else
                "Nobody has registered a pet yet. `/pet add` fixes that."
            ),
        )
        # No author line. It read "Cozy Together · pet-care" directly above a
        # title already saying Cozy Together Pets, in a channel you are standing
        # in — three ways of telling you where you are, stacked.

        if entries:
            embed.add_field(
                name="Hungry right now",
                value=f"**{len(waiting)}** of {len(entries)}",
            )
            # A real timestamp, so it counts itself down in each member's own
            # timezone and the panel never has to be redrawn to stay right.
            embed.add_field(name="Resets in", value=f"<t:{_next_reset_unix()}:R>")
            embed.add_field(name="Fed here today", value=f"**{given}**")

        if waiting:
            # One card per pet, built out of inline fields because Discord has no
            # card of its own — it lays inline fields three to a row on desktop
            # and two on mobile, which is the grid.
            #
            # This full-width field is both the heading and the row break: without
            # it the cards would flow onto the end of the summary row above.
            embed.add_field(name="Still waiting", value="​", inline=False)

            icons = {e.name.lower(): str(e) for e in guild.emojis}
            for pet, days in waiting[:PANEL_GRID_MAX]:
                bar, word = _hunger(days)
                embed.add_field(
                    name=f"{_species_icon(icons, pet)} {pet.name}"[:256],
                    value=f"{bar}\n{word}",
                )

            more = len(waiting) - PANEL_GRID_MAX
            if more > 0:
                embed.add_field(name=f"+{more} more", value="see 📖 Pet dex")
        elif entries:
            embed.add_field(
                name="Still waiting",
                value="Nobody. Every animal here has eaten today. "
                      "I have eaten nothing, but nobody asks about that.",
                inline=False,
            )

        # Mittens goes in the footer: small, italic, under everything, commenting
        # rather than instructing. The rule he used to share it with is in the
        # description now, where it is read.
        embed.set_footer(text=_panel_line(state))
        return embed, FoodBowlView(self, guild, entries)

    async def _clear_strays(self, channel: discord.TextChannel, keep_id: int) -> None:
        """Delete any other panel of ours still sitting in the channel.

        Belt and braces for the duplicate-panel bug: whatever leaves a stray
        behind — a crash between send and delete, a second process, a botched
        deploy — the next placement tidies it away instead of stacking.
        """
        removed = 0
        try:
            async for message in channel.history(limit=STRAY_SCAN):
                if removed >= STRAY_DELETE_CAP:
                    break
                if message.id == keep_id or self.bot.user is None:
                    continue
                if message.author.id != self.bot.user.id or not message.embeds:
                    continue
                if (message.embeds[0].title or "") not in PANEL_TITLES:
                    continue
                try:
                    await message.delete()
                    removed += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
        except Exception:
            log.debug("[pets] stray sweep failed", exc_info=True)
        if removed:
            log.info("[pets] cleared %d stray panel(s)", removed)

    async def _place_panel(
        self, channel: discord.TextChannel, *, force: bool = False, sweep: bool = False
    ) -> None:
        """Put a fresh panel at the bottom and clear the one above it.

        `force` skips the circuit breaker — for `/petpanel`, where a human has
        asked for it directly and waiting out the floor would just look broken.

        `sweep` also rakes the channel for older panels of ours. That is a
        hundred-message history fetch, so it only runs where an abnormal state is
        expected: at boot, and when an admin asks for a panel by hand. The
        ordinary path already deletes the panel it replaces.
        """
        async with self._lock:
            now = time.monotonic()
            if not force and now - self._last_place < MIN_PLACE_INTERVAL:
                return
            self._last_place = now

            old = self._panel
            if old is None:
                recalled = await asyncio.to_thread(_recall_panel, channel.guild.id)
                if recalled and recalled[0] == channel.id:
                    try:
                        old = await channel.fetch_message(recalled[1])
                    except (discord.NotFound, discord.Forbidden):
                        old = None
                    except Exception:
                        log.debug("[pets] could not fetch the stored panel", exc_info=True)
                        old = None

            try:
                embed, view = await self._build_panel(channel.guild)
                fresh = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                log.warning("[pets] cannot post the panel in #%s", channel.name)
                return
            except Exception:
                log.exception("[pets] could not post the panel")
                return

            self._panel = fresh
            log.info(
                "[pets] panel %s placed in #%s — %d pets listed, up to %d per go",
                fresh.id, channel.name, view.option_count, view.max_pick,
            )
            await asyncio.to_thread(_remember_panel, channel.guild.id, channel.id, fresh.id)

            # Last, so a failure above never leaves the channel with no panel.
            if old is not None and old.id != fresh.id:
                try:
                    await old.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                except Exception:
                    log.debug("[pets] could not delete the old panel", exc_info=True)

            if sweep:
                await self._clear_strays(channel, fresh.id)

    async def _needs_moving(self, channel: discord.TextChannel) -> bool:
        if self._panel is None:
            return True
        try:
            async for message in channel.history(limit=1):
                return message.id != self._panel.id
        except Exception:
            log.debug("[pets] could not read the channel tail", exc_info=True)
            return False
        return True

    async def _repost_soon(self) -> None:
        """Debounced: a burst of conversation costs exactly one repost.

        The rate floor is waited out here rather than enforced by dropping the
        move. Skipping it outright meant anything happening within
        MIN_PLACE_INTERVAL of a placement — feeding a pet moments after the panel
        landed, which is the common case — lost its move entirely and left the
        panel stranded above the newest post until somebody else spoke.
        """
        try:
            await asyncio.sleep(REPOST_DEBOUNCE)
            waited = time.monotonic() - self._last_place
            if waited < MIN_PLACE_INTERVAL:
                await asyncio.sleep(MIN_PLACE_INTERVAL - waited)
        except asyncio.CancelledError:
            return
        channel = self._channel()
        if channel is None:
            return
        if await self._needs_moving(channel):
            await self._place_panel(channel)

    def _schedule_repost(self) -> None:
        if self._repost and not self._repost.done():
            self._repost.cancel()
        self._repost = asyncio.create_task(self._repost_soon())

    async def _boot(self) -> None:
        await self.bot.wait_until_ready()
        channel = self._channel()
        if channel is None:
            log.warning("[pets] pet care channel %s is not reachable", FEED_CHANNEL_ID)
            return
        await self._place_panel(channel, sweep=True)

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        """Move the panel down for other people's messages — never for our own.

        Matching on our previous panel's id is not enough. The gateway can
        deliver MESSAGE_CREATE for a panel before `channel.send` has returned and
        `self._panel` has been reassigned, so the panel would see its own arrival
        as foreign traffic and schedule another move: one new panel every few
        seconds, forever. Two copies of the bot (an overlapping deploy) produce
        the same loop by seeing each other's panels.

        Ignoring our own user id kills both, and the cog's own posts schedule
        their move explicitly instead — see `_after_post`.
        """
        if message.guild is None or message.channel.id != FEED_CHANNEL_ID:
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        self._schedule_repost()

    def _after_post(self) -> None:
        """Call after the cog posts something public, in place of on_message."""
        self._schedule_repost()

    # ── the two commands that aren't registration, and they're for you ────────

    @app_commands.command(name="petpanel", description="Post the pet panel again 🐾")
    @admin_only()
    async def petpanel_cmd(self, interaction: discord.Interaction) -> None:
        """Manual repost. Needed after purging the channel, since the panel the
        cog is holding on to no longer exists and nothing else will replace it
        until somebody talks."""
        channel = self._channel()
        if channel is None:
            return await interaction.response.send_message(
                "❌ I can't see the pet channel.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        self._panel = None          # whatever it was pointing at may be purged
        if self._repost and not self._repost.done():
            self._repost.cancel()   # don't let a queued move double up behind us

        await self._place_panel(channel, force=True, sweep=True)

        if self._panel is None:
            return await interaction.followup.send(
                "❌ I couldn't post it. Check I have Send Messages and Embed Links there.",
                ephemeral=True,
            )
        await interaction.followup.send(
            f"🍖 Panel posted in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="pettreats", description="Give yourself today's treats back 🍬"
    )
    @owner_only()
    async def pettreats_cmd(self, interaction: discord.Interaction) -> None:
        """Refill your own allowance, for testing the feeding without waiting
        for midnight. Owner rung, and it only ever touches the caller's own
        record — there is no version of this that hands somebody else treats."""
        if interaction.guild is None:
            return await interaction.response.send_message(
                "⚠️ This only works in a server.", ephemeral=True
            )

        try:
            treats, plays = await asyncio.to_thread(
                refill, interaction.guild.id, interaction.user.id
            )
        except FeedError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        await interaction.response.send_message(
            f"🍬 Topped up: **{treats}** treats and **{plays}** plays. "
            "Every pet is feedable again.",
            ephemeral=True,
        )

    # ── registration ──────────────────────────────────────────────────────────

    def _cannot_register_here(self, interaction: discord.Interaction) -> bool:
        return (
            not interaction.channel
            or interaction.channel.id not in PET_REGISTER_CHANNEL_IDS
        )

    async def _deny_register_channel(self, interaction: discord.Interaction) -> None:
        rooms = " or ".join(f"<#{cid}>" for cid in sorted(PET_REGISTER_CHANNEL_IDS))
        await interaction.response.send_message(
            f"🚫 Register pets in {rooms}.", ephemeral=True
        )

    def _cannot_claim_here(self, interaction: discord.Interaction) -> bool:
        """The right-click claim reaches further than `/pet add` — it works on a
        photo, so it has to work where the photos are."""
        return (
            not interaction.channel
            or interaction.channel.id not in PET_CLAIM_CHANNEL_IDS
        )

    async def _register(
        self,
        interaction: discord.Interaction,
        name: str,
        raw: bytes,
        profile: dict[str, str] | None = None,
    ) -> None:
        """Shared tail of both registration routes. Assumes a deferred response."""
        assert interaction.guild is not None
        try:
            pet = await asyncio.to_thread(
                pet_registry.add_pet, interaction.guild.id, interaction.user.id, name, raw
            )
        except pet_registry.PetError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except Exception:
            log.exception("[pets] registration failed")
            return await interaction.followup.send(
                "❌ Something went wrong saving that. Try again in a moment.", ephemeral=True
            )

        if profile and any(v.strip() for v in profile.values()):
            try:
                pet = await asyncio.to_thread(
                    pet_registry.save_profile, interaction.guild.id, pet.pet_id, profile
                )
            except Exception:
                # The pet exists; a profile that didn't stick is editable later.
                log.warning("[pets] could not save the profile for %s", pet.pet_id, exc_info=True)

        # No `/shippet` pointer here on purpose: shipping is gated to its own
        # channel, and advertising it from this one only sends people somewhere
        # the command refuses to run.
        await interaction.followup.send(
            f"✅ **{pet.name}** is registered. Feed them from the panel in "
            f"<#{PET_CARE_CHANNEL_ID}>.",
            view=MoreDetailsView(pet),
            ephemeral=True,
        )
        self._schedule_repost()   # the panel has a new pet to list

    pet = app_commands.Group(name="pet", description="Register and manage your pets 🐾")

    @pet.command(name="add", description="Register one of your pets 🐾")
    @app_commands.describe(name="What they're called", photo="A picture of them")
    @app_commands.checks.cooldown(2, 30.0)
    async def pet_add(
        self,
        interaction: discord.Interaction,
        name: str,
        photo: discord.Attachment,
    ) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "⚠️ This only works in a server.", ephemeral=True
            )
        if self._cannot_register_here(interaction):
            return await self._deny_register_channel(interaction)
        if not (photo.content_type or "").startswith("image/"):
            return await interaction.response.send_message(
                "❌ That needs to be an image.", ephemeral=True
            )
        if photo.size > pet_registry.MAX_UPLOAD_BYTES:
            return await interaction.response.send_message(
                f"❌ {pet_registry.TOO_BIG}", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            raw = await photo.read()
        except Exception:
            log.exception("[pets] could not read attachment")
            return await interaction.followup.send(
                "❌ I couldn't download that photo.", ephemeral=True
            )
        await self._register(interaction, name, raw)

    @pet.command(name="list", description="See every pet registered in the server 🐾")
    async def pet_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "⚠️ This only works in a server.", ephemeral=True
            )

        pets = await asyncio.to_thread(pet_registry.all_pets, interaction.guild.id)
        if not pets:
            return await interaction.response.send_message(
                "No pets registered yet. Be the first with `/pet add`.", ephemeral=True
            )

        lines = []
        for p in pets:
            owner = interaction.guild.get_member(p.owner_id)
            who = owner.display_name if owner else "someone who left"
            lines.append(f"🐾 **{p.name}** — {who}'s")

        embed = discord.Embed(
            title="Registered pets",
            description="\n".join(lines[:40]),
            colour=EMBED_COLOUR,
        )
        if len(pets) > 40:
            embed.set_footer(text=f"…and {len(pets) - 40} more")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @pet.command(name="remove", description="Remove one of your pets 🐾")
    @app_commands.describe(pet="Which one — mods can remove anyone's")
    async def pet_remove(self, interaction: discord.Interaction, pet: str) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "⚠️ This only works in a server.", ephemeral=True
            )

        record = await asyncio.to_thread(pet_registry.get_pet, interaction.guild.id, pet)
        if record is None:
            record = await asyncio.to_thread(
                pet_registry.find_by_name, interaction.guild.id, pet, interaction.user.id
            )
        if record is None:
            return await interaction.response.send_message(
                "❌ I don't have a pet by that name.", ephemeral=True
            )

        if record.owner_id != interaction.user.id and not _is_mod(interaction.user):
            return await interaction.response.send_message(
                "❌ That's not your pet.", ephemeral=True
            )

        try:
            await asyncio.to_thread(pet_registry.remove_pet, interaction.guild.id, record.pet_id)
        except pet_registry.PetError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        # After the registry, not before: a failed removal above must not throw
        # away the treat history of a pet that is still registered.
        await asyncio.to_thread(forget_pet, interaction.guild.id, record.pet_id)

        await interaction.response.send_message(
            f"🗑️ **{record.name}** has been removed.", ephemeral=True
        )
        self._schedule_repost()   # the panel's list just changed

    @pet_remove.autocomplete("pet")
    async def _remove_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Own pets only, unless you're a mod — you can't remove what you can't pick."""
        if interaction.guild is None:
            return []
        pets = await asyncio.to_thread(pet_registry.all_pets, interaction.guild.id)
        if not _is_mod(interaction.user):
            pets = [p for p in pets if p.owner_id == interaction.user.id]
        return self._pet_choices(interaction, pets, current)

    def _pet_choices(
        self,
        interaction: discord.Interaction,
        pets: list[pet_registry.Pet],
        current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = current.strip().casefold()
        out: list[app_commands.Choice[str]] = []
        for p in pets:
            if needle and needle not in p.name.casefold():
                continue
            owner = interaction.guild.get_member(p.owner_id) if interaction.guild else None
            who = owner.display_name if owner else "?"
            out.append(app_commands.Choice(name=f"{p.name} — {who}'s"[:100], value=p.pet_id))
            if len(out) == 25:
                break
        return out

    # ── right-click → Apps → This is my pet ───────────────────────────────────

    async def claim_pet_from_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "⚠️ This only works in a server.", ephemeral=True
            )
        if self._cannot_claim_here(interaction):
            rooms = " or ".join(f"<#{cid}>" for cid in sorted(PET_CLAIM_CHANNEL_IDS))
            return await interaction.response.send_message(
                f"🚫 Register pets from photos in {rooms}.", ephemeral=True
            )

        # Own messages only. Otherwise anyone could claim someone else's pet.
        if message.author.id != interaction.user.id:
            return await interaction.response.send_message(
                "❌ You can only register a pet from a photo **you** posted.", ephemeral=True
            )

        image = next(
            (a for a in message.attachments if (a.content_type or "").startswith("image/")),
            None,
        )
        if image is None:
            return await interaction.response.send_message(
                "❌ That message has no image attached.", ephemeral=True
            )
        if image.size > pet_registry.MAX_UPLOAD_BYTES:
            return await interaction.response.send_message(
                f"❌ {pet_registry.TOO_BIG}", ephemeral=True
            )

        await interaction.response.send_modal(ClaimPetModal(self, image))

    # ── actions behind the components ─────────────────────────────────────────

    async def _feed_list(
        self, guild: discord.Guild, user_id: int, picked: set[str]
    ) -> tuple[str, "FeedListView"] | None:
        """The private list and its controls, drawn from scratch every tick."""
        pets = await asyncio.to_thread(pet_registry.all_pets, guild.id)
        if not pets:
            return None

        entries = await asyncio.to_thread(hungriest, guild.id, pets)
        left, fed_ids = await asyncio.to_thread(treats_left, guild.id, user_id)
        fed_today = set(fed_ids)

        # The ones you can still feed are the list; the ones you have fed are a
        # footnote. Marking them in place put "you fed them today already" down
        # the right-hand side of half the rows, which is a lot of text saying
        # the same thing about pets you cannot do anything with anyway.
        feedable = [(p, d) for p, d in entries if p.pet_id not in fed_today]
        done = [p for p, _ in entries if p.pet_id in fed_today]

        # Spelling out the close step, because Discord tells the bot nothing
        # until the menu is shut — the ticks cannot appear here before then, and
        # not knowing that makes the menu look broken rather than pending.
        lines = [
            f"🍖 You have **{left}** of {DAILY_TREATS} treats left today.",
            "*Tick who eats · close the menu · press Feed.*",
        ]
        for pet, _days in feedable[:SELECT_LIMIT]:
            owner = guild.get_member(pet.owner_id)
            who = owner.display_name if owner else "someone who left"
            box = "☑" if pet.pet_id in picked else "☐"
            lines.append(f"{box} **{pet.name}** — *{who}'s*")

        if not feedable:
            lines.append("*Everyone here has had something from you today.*")
        elif len(feedable) > SELECT_LIMIT:
            # 25 is Discord's cap on a select, and the list is ordered
            # hungriest first, so the ones that fall off are the ones least in
            # need of a treat.
            lines.append(
                f"*…and {len(feedable) - SELECT_LIMIT} more, who have all eaten "
                "more recently than these.*"
            )

        if done:
            lines.append("")
            lines.append("**Pets you already fed**")
            lines.append(", ".join(p.name for p in done))

        text = "\n".join(lines)[:MAX_LIST_LENGTH]
        return text, FeedListView(self, guild, feedable, picked, left)

    async def open_feed_list(self, interaction: discord.Interaction) -> None:
        """Open this member's own feeding list.

        Ephemeral, which is the entire point — it belongs to one person, so it
        can be redrawn after every tick with the boxes still filled in.
        """
        guild = interaction.guild
        if guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        # A list opened fresh starts empty rather than resuming what was ticked
        # ten minutes ago and forgotten about.
        self._picked.pop(interaction.user.id, None)

        built = await self._feed_list(guild, interaction.user.id, set())
        if built is None:
            return await self._deny(
                interaction, "🍖 Nobody has registered a pet yet. `/pet add` fixes that."
            )
        text, view = built
        await interaction.response.send_message(text, view=view, ephemeral=True)

    async def update_feed_list(
        self, interaction: discord.Interaction, pet_ids: list[str]
    ) -> None:
        """Redraw the list with the new ticks showing."""
        guild = interaction.guild
        if guild is None:
            return

        now = time.monotonic()
        self._picked = {
            uid: pick for uid, pick in self._picked.items() if now - pick[0] < PICK_TTL
        }
        self._picked[interaction.user.id] = (now, pet_ids)

        built = await self._feed_list(guild, interaction.user.id, set(pet_ids))
        if built is None:
            return await interaction.response.defer()
        text, view = built
        await interaction.response.edit_message(content=text, view=view)

    async def feed_from_list(self, interaction: discord.Interaction) -> None:
        """Spend what's ticked, then hand the post to the channel.

        The private list is cleared first, which also marks the interaction as
        answered — so `feed_pets` posts through a followup and the public embed
        lands in the channel rather than replacing the list.
        """
        pick = self._picked.pop(interaction.user.id, None)
        if pick is None or not pick[1]:
            return await self._deny(interaction, "🍖 Nothing ticked yet.")
        if time.monotonic() - pick[0] >= PICK_TTL:
            return await self._deny(
                interaction, "🍖 That selection went stale. Open the list again."
            )

        await interaction.response.edit_message(content="🍖 Off they go.", view=None)
        await self.feed_pets(interaction, pick[1])

    async def panel_is_stale(self, interaction: discord.Interaction) -> bool:
        """Whether this click came from a panel we have already replaced.

        Registering the view as persistent means an old panel now *works*, using
        whatever options and limits it was built with. Someone clicking a panel
        from before a deploy would get the previous version of the feature and no
        hint that a newer one exists.

        The old message is deleted rather than left to catch the next person; it
        should not have outlived the boot sweep in the first place.
        """
        message = interaction.message
        if message is None or self._panel is None or message.id == self._panel.id:
            return False

        await self._deny(
            interaction,
            "🐾 That was an older panel, so I've cleared it away. "
            "The current one is at the bottom of the channel — everything works from there.",
        )
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception:
            log.debug("[pets] could not delete a stale panel", exc_info=True)
        self._schedule_repost()
        return True

    async def feed_pets(self, interaction: discord.Interaction, pet_ids: list[str]) -> None:
        """A whole ticked selection. One pet falls through to the single path,
        which still shows their photo as the thumbnail and reads exactly as it
        always has — nothing about feeding one animal changes."""
        if len(pet_ids) == 1:
            return await self.feed_pet(interaction, pet_ids[0])

        guild = interaction.guild
        if guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        records = await asyncio.to_thread(self._resolve_all, guild.id, pet_ids)
        if not records:
            return await self._deny(
                interaction, "❌ I don't know any of those. They may have just been removed."
            )

        favourites = {
            p.pet_id: p.treat or ("fishcake" if _eats_fishcake(p.name) else "")
            for p in records
        }

        try:
            result = await asyncio.to_thread(
                feed_many, guild.id, interaction.user.id,
                [p.pet_id for p in records], favourites,
            )
        except FeedError as e:
            return await self._deny(interaction, f"❌ {e}")
        except Exception:
            log.exception("[pets] multi feed failed")
            return await self._deny(
                interaction, "❌ Something went wrong. Try again in a moment."
            )

        by_id = {p.pet_id: p for p in records}
        fed_pets = [by_id[f.pet_id] for f in result.fed if f.pet_id in by_id]

        # Nothing ate: that is a private answer, not a public post about a
        # non-event. The reasons are the whole message.
        if not fed_pets:
            reasons = []
            if result.already:
                reasons.append("you've already fed them today")
            if result.no_treats:
                reasons.append("you're out of treats")
            return await self._deny(
                interaction, "❌ Nothing to feed — " + " and ".join(reasons) + "."
            )

        rng = random.Random(f"many-{interaction.user.id}-{_today()}-{len(fed_pets)}")

        # The mention goes in the description, not the title — an embed title
        # renders a mention as raw <@1234>.
        embed = discord.Embed(
            title=f"🍖 You fed {len(fed_pets)} pets",
            description=(
                f"{interaction.user.mention} — "
                + rng.choice(MULTI_FEED_LINES).format(n=len(fed_pets))
            ),
            colour=EMBED_COLOUR,
        )
        embed.add_field(
            name="Fed",
            value="\n".join(
                f"{pet_treats.emoji_for(one.treat)} **{by_id[one.pet_id].name}** — "
                f"{one.treat}" + (" · **their favourite**" if one.perfect else "")
                for one in result.fed if one.pet_id in by_id
            )[:1024],
            inline=False,
        )

        crowned = [by_id[one.pet_id].name for one in result.fed
                   if one.new_favourite and one.pet_id in by_id]
        if crowned:
            embed.add_field(
                name="​",
                value="\n".join(
                    rng.choice(FAVOURITE_LINES).format(
                        user=interaction.user.mention, pet=name
                    )
                    for name in crowned
                )[:1024],
                inline=False,
            )

        if result.already:
            embed.add_field(
                name="Skipped",
                value=self._name_list(by_id, result.already) + " — you fed them today already.",
                inline=False,
            )
        if result.no_treats:
            embed.add_field(
                name="No treats left",
                value=self._name_list(by_id, result.no_treats) + " — they'll keep until tomorrow.",
                inline=False,
            )

        # The mention above already says who did this, so the footer is just the
        # allowance rather than the name a second time.
        embed.set_footer(text=f"{result.treats_left} treats left today")

        grid = await asyncio.to_thread(pet_registry.render_grid, fed_pets)
        file = None
        if grid:
            file = discord.File(io.BytesIO(grid), filename="fed.png")
            embed.set_image(url="attachment://fed.png")

        # Five is a row, and a row is enough — a ten-pet feed would otherwise
        # bury the post under buttons.
        view = discord.ui.View(timeout=None)
        for pet in fed_pets[:5]:
            view.add_item(AboutButton(pet.pet_id, f"🐾 {pet.name}"))

        # Followup when the feeding list already answered this interaction,
        # otherwise the public post would replace the private list instead of
        # landing in the channel.
        send = (
            interaction.followup.send if interaction.response.is_done()
            else interaction.response.send_message
        )
        await send(
            embed=embed,
            view=view,
            file=file or discord.utils.MISSING,
            allowed_mentions=MENTIONS,
        )
        self._after_post()

    @staticmethod
    def _resolve_all(guild_id: int, pet_ids: list[str]) -> list[pet_registry.Pet]:
        """Ids to pets, in the order they were ticked, dropping any that vanished."""
        found = []
        for pet_id in pet_ids:
            pet = pet_registry.get_pet(guild_id, pet_id)
            if pet is not None:
                found.append(pet)
        return found

    @staticmethod
    def _name_list(by_id: dict[str, pet_registry.Pet], pet_ids: list[str]) -> str:
        names = [f"**{by_id[pid].name}**" for pid in pet_ids if pid in by_id]
        return ", ".join(names) if names else "Some of them"

    async def feed_pet(self, interaction: discord.Interaction, pet_id: str) -> None:
        if interaction.guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        record = await self._resolve(interaction.guild, pet_id)
        if record is None:
            return await self._deny(
                interaction, "❌ I don't know that pet. It may have just been removed."
            )

        favourite = record.treat or ("fishcake" if _eats_fishcake(record.name) else "")

        try:
            result = await asyncio.to_thread(
                feed, interaction.guild.id, interaction.user.id, record.pet_id, favourite
            )
        except FeedError as e:
            return await self._deny(interaction, f"❌ {e}")
        except Exception:
            log.exception("[pets] feed failed")
            return await self._deny(
                interaction, "❌ Something went wrong. Try again in a moment."
            )

        # Stable per pet, per feeder, per day, the same way ship scores are.
        rng = random.Random(f"{record.pet_id}-{interaction.user.id}-{_today()}")
        given = result.treat or "treat"
        line = rng.choice(FEED_LINES).format(pet=record.name, treat=given)

        embed = discord.Embed(
            title=(
                f"{pet_treats.emoji_for(given)} {record.name} got their favourite!"
                if result.perfect
                else f"🍖 {record.name} has been fed a little snackie!"
            ),
            description=line,
            colour=EMBED_COLOUR,
        )
        embed.add_field(
            name="Treat given",
            value=(
                f"{pet_treats.label(given)} — **their favourite**"
                if result.perfect
                else pet_treats.label(given)
            ),
            inline=False,
        )
        if result.new_favourite:
            embed.add_field(
                name="​",
                value=rng.choice(FAVOURITE_LINES).format(
                    user=interaction.user.mention, pet=record.name
                ),
                inline=False,
            )
        embed.set_footer(
            text=(
                f"{interaction.user.display_name} · {result.treats_left} treats left today"
                f" · {record.name} has {result.total} in total"
            )
        )

        view = discord.ui.View(timeout=None)
        view.add_item(AboutButton(record.pet_id, f"🐾 About {record.name}"))

        file = self._thumb(record)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")

        # Followup when the feeding list already answered this interaction,
        # otherwise the public post would replace the private list instead of
        # landing in the channel.
        send = (
            interaction.followup.send if interaction.response.is_done()
            else interaction.response.send_message
        )
        await send(
            embed=embed,
            view=view,
            file=file or discord.utils.MISSING,
            allowed_mentions=MENTIONS,
        )
        self._after_post()

    def profile_saver(self, pet_id: str):
        """The callback both profile modals hand their values to.

        Ownership is re-checked here rather than trusted from whoever opened the
        modal — the modal is just a form, this is the gate.
        """
        async def save(
            interaction: discord.Interaction, values: dict[str, str], name: str | None
        ) -> None:
            guild = interaction.guild
            if guild is None:
                return
            record = await asyncio.to_thread(pet_registry.get_pet, guild.id, pet_id)
            if record is None:
                return await self._deny(interaction, "❌ That pet isn't registered any more.")
            if not self._may_edit(interaction.user, record):
                return await self._deny(interaction, "❌ That's not your pet.")

            try:
                pet = await asyncio.to_thread(
                    pet_registry.save_profile, guild.id, pet_id, values, new_name=name
                )
            except pet_registry.PetError as e:
                return await self._deny(interaction, f"❌ {e}")
            except Exception:
                log.exception("[pets] profile save failed")
                return await self._deny(
                    interaction, "❌ Something went wrong saving that."
                )

            left = pet_profile.missing(pet)
            note = f"\nStill blank: {', '.join(left)}." if left else ""
            await self._deny(interaction, f"✅ **{pet.name}** updated.{note}")

        return save

    async def open_editor(self, interaction: discord.Interaction, pet_id: str) -> None:
        """Straight into the form — the whole bio on one screen."""
        await self._open_form(interaction, pet_id, pet_profile.bio_modal)

    async def open_rename(self, interaction: discord.Interaction, pet_id: str) -> None:
        await self._open_form(interaction, pet_id, pet_profile.name_modal)

    async def _open_form(self, interaction: discord.Interaction, pet_id: str, build) -> None:
        """Check it's theirs, then hand over the modal.

        Checked here *and* again in `profile_saver` when it comes back: a modal
        can sit open for as long as somebody likes, and the pet may be gone or
        no longer theirs by the time they press submit.
        """
        guild = interaction.guild
        if guild is None:
            return
        record = await asyncio.to_thread(pet_registry.get_pet, guild.id, pet_id)
        if record is None:
            return await self._deny(interaction, "❌ That pet isn't registered any more.")
        if not self._may_edit(interaction.user, record):
            return await self._deny(interaction, "❌ That's not your pet.")

        await interaction.response.send_modal(
            build(self.profile_saver(record.pet_id), record)
        )

    def _may_manage(self, user: discord.abc.User, pet: pet_registry.Pet) -> bool:
        """Owner, or a mod. For *removal* — the same rule `/pet remove` applies.

        Mods keep this because taking a pet down is moderation: somebody has to
        be able to remove a photo that shouldn't be up.
        """
        if pet.owner_id == user.id:
            return True
        return _is_mod(user)

    @staticmethod
    def _may_edit(user: discord.abc.User, pet: pet_registry.Pet) -> bool:
        """Owner only, for the bio and the name.

        Deliberately narrower than `_may_manage`. Taking a pet down is
        moderation; rewriting its bio is writing in somebody else's name, and no
        amount of manage-server permission makes that a thing anyone needs to do.
        """
        return pet.owner_id == user.id

    async def open_manage(
        self, interaction: discord.Interaction, *, edit: bool = False
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        pets = await asyncio.to_thread(
            pet_registry.pets_of, guild.id, interaction.user.id
        )
        if not pets:
            return await self._deny(
                interaction,
                "🐾 You haven't registered any pets. `/pet add` in this channel, "
                "or right-click a photo you posted → Apps → This is my pet.",
            )

        entries = await asyncio.to_thread(hungriest, guild.id, pets)
        text = f"🐾 You have **{len(pets)}** pet" + ("" if len(pets) == 1 else "s")
        view = ManageListView(self, entries)

        if edit:
            await interaction.response.edit_message(
                content=text, embed=None, view=view, attachments=[]
            )
        else:
            await interaction.response.send_message(text, view=view, ephemeral=True)

    async def show_manage_pet(
        self, interaction: discord.Interaction, pet_id: str, *, confirming: bool = False
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        record = await asyncio.to_thread(pet_registry.get_pet, guild.id, pet_id)
        if record is None:
            return await self._deny(interaction, "❌ That pet isn't registered any more.")
        if not self._may_manage(interaction.user, record):
            return await self._deny(interaction, "❌ That's not your pet.")

        stats = await asyncio.to_thread(pet_stats, guild.id, record.pet_id)
        embed = discord.Embed(
            title=f"🐾 {record.name}",
            colour=EMBED_COLOUR,
            description=(
                "**Remove them?** This deletes their photo and takes them off the "
                "board. Their treat history goes with them."
                if confirming else None
            ),
        )
        embed.add_field(name="Treats eaten", value=str(stats.get("total", 0)))
        embed.add_field(
            name="Last meal",
            value=_last_treat(_days_since(stats.get("last_fed") or record.added)),
        )
        if not confirming:
            pet_profile.apply(embed, record)
            still_blank = pet_profile.missing(record)
            if still_blank:
                embed.set_footer(text="Still blank: " + ", ".join(still_blank))

        file = self._thumb(record)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=ManagePetView(self, record.pet_id, confirming=confirming),
            attachments=[file] if file else [],
        )

    async def remove_pet(self, interaction: discord.Interaction, pet_id: str) -> None:
        guild = interaction.guild
        if guild is None:
            return
        record = await asyncio.to_thread(pet_registry.get_pet, guild.id, pet_id)
        if record is None:
            return await self._deny(interaction, "❌ That pet isn't registered any more.")
        # Checked again here, not just when the button was drawn — the view is a
        # suggestion, this is the gate.
        if not self._may_manage(interaction.user, record):
            return await self._deny(interaction, "❌ That's not your pet.")

        try:
            await asyncio.to_thread(pet_registry.remove_pet, guild.id, record.pet_id)
        except pet_registry.PetError as e:
            return await self._deny(interaction, f"❌ {e}")

        # After the registry, not before: a failed removal above must not throw
        # away the treat history of a pet that is still registered.
        await asyncio.to_thread(forget_pet, guild.id, record.pet_id)

        await interaction.response.edit_message(
            content=f"🗑️ **{record.name}** has been removed.",
            embed=None, view=None, attachments=[],
        )
        self._schedule_repost()   # the panel's list just changed

    async def _dex_entries(
        self, guild: discord.Guild, user_id: int
    ) -> tuple[list[pet_registry.Pet], list[tuple[int, pet_registry.Pet, str, bool]], set[str]]:
        """The dex in order, with owner names and your caught marks.

        Numbered by registration date so the numbers are stable — a pet added
        later lands at the end instead of shifting everyone along.
        """
        pets = await asyncio.to_thread(pet_registry.all_pets, guild.id)
        pets.sort(key=lambda p: (p.added or "", p.name.casefold()))
        caught = await asyncio.to_thread(
            dex_caught, guild.id, user_id, [p.pet_id for p in pets]
        )
        rows = []
        for n, pet in enumerate(pets, 1):
            owner = guild.get_member(pet.owner_id)
            rows.append((
                n, pet,
                owner.display_name if owner else "someone who left",
                pet.pet_id in caught,
            ))
        return pets, rows, caught

    async def open_dex_index(
        self, interaction: discord.Interaction, page: int = 0
    ) -> None:
        """The whole dex at a glance — 13 arrow clicks to reach the end was silly."""
        guild = interaction.guild
        if guild is None:
            return
        pets, rows, caught = await self._dex_entries(guild, interaction.user.id)
        if not pets:
            return await self._deny(
                interaction, "📖 The dex is empty. `/pet add` starts it off."
            )

        pages = max(1, (len(rows) + DEX_INDEX_PAGE - 1) // DEX_INDEX_PAGE)
        page = max(0, min(page, pages - 1))
        window = rows[page * DEX_INDEX_PAGE:(page + 1) * DEX_INDEX_PAGE]

        embed = discord.Embed(
            title="📖 The pet dex",
            description="\n".join(
                f"{'✅' if seen else '▫️'} `#{n:03d}` **{pet.name}** — {who}'s"
                for n, pet, who, seen in window
            ),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(
            text=(
                f"you've fed {len(caught)} of {len(rows)}"
                + (f" · page {page + 1}/{pages}" if pages > 1 else "")
            )
        )

        view = DexIndexView(self, window, page, pages)
        if interaction.response.is_done():
            await interaction.edit_original_response(
                content=None, embed=embed, view=view, attachments=[]
            )
        else:
            await interaction.response.edit_message(
                content=None, embed=embed, view=view, attachments=[]
            )

    async def open_dex(
        self, interaction: discord.Interaction, page: int = 0, *, edit: bool = False
    ) -> None:
        """One page per pet, numbered by registration so the numbers never move."""
        guild = interaction.guild
        if guild is None:
            return
        pets, rows, caught = await self._dex_entries(guild, interaction.user.id)
        if not pets:
            return await self._deny(
                interaction, "📖 The dex is empty. `/pet add` starts it off."
            )

        total = len(pets)
        page = max(0, min(page, total - 1))
        pet = pets[page]

        stats = await asyncio.to_thread(pet_stats, guild.id, pet.pet_id)
        by: dict[str, Any] = stats.get("by", {})
        yours = int(by.get(str(interaction.user.id), 0))
        owner = guild.get_member(pet.owner_id)

        embed = discord.Embed(
            title=f"#{page + 1:03d} · {pet.name}",
            description=f"{owner.display_name if owner else 'someone who left'}'s",
            colour=EMBED_COLOUR,
        )
        embed.add_field(name="Treats eaten", value=str(stats.get("total", 0)))

        fav_id = _favourite_id(by)
        if fav_id:
            fav = guild.get_member(int(fav_id))
            embed.add_field(
                name="Favourite human",
                value=fav.display_name if fav else "someone who left",
            )
        else:
            embed.add_field(name="Favourite human", value="nobody yet")

        embed.add_field(
            name="Last meal", value=_last_treat(_days_since(stats.get("last_fed") or pet.added))
        )
        pet_profile.apply(embed, pet)
        embed.add_field(
            name="Your record",
            value=(
                f"✅ you've fed them **{yours}** time{'' if yours == 1 else 's'}"
                if yours
                else "▫️ you've never fed them"
            ),
            inline=False,
        )
        embed.set_footer(text=f"{page + 1} of {total} · you've fed {len(caught)} of {total}")

        file = self._thumb(pet)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")

        view = DexView(self, page, total, pet.pet_id, entries=rows)

        if edit:
            await interaction.response.edit_message(
                content=None, embed=embed, view=view,
                attachments=[file] if file else [],
            )
        else:
            await interaction.response.send_message(
                embed=embed, view=view,
                file=file or discord.utils.MISSING, ephemeral=True,
            )

    def _play_order(
        self, guild_id: int, pets: list[pet_registry.Pet]
    ) -> list[tuple[pet_registry.Pet, int]]:
        """Least-played first, so the overlooked ones are the easy ones to pick."""
        block = _guild_block(_load(), guild_id)
        stats = block["pets"]
        rows = [(p, int((stats.get(p.pet_id) or {}).get("plays", 0))) for p in pets]
        rows.sort(key=lambda pair: (pair[1], pair[0].name.casefold()))
        return rows

    async def open_play(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        left, played = await asyncio.to_thread(plays_left, guild.id, interaction.user.id)
        if left <= 0:
            return await self._deny(
                interaction,
                f"🧸 You've played enough for today — you get {DAILY_PLAYS}. More tomorrow.",
            )

        pets = await asyncio.to_thread(pet_registry.all_pets, guild.id)
        if not pets:
            return await self._deny(
                interaction, "🧸 Nobody has registered a pet yet. `/pet add` fixes that."
            )
        entries = await asyncio.to_thread(self._play_order, guild.id, pets)

        await interaction.response.send_message(
            f"🧸 You have **{left}** of {DAILY_PLAYS} plays left today.",
            view=PlayPickView(self, guild, entries, set(played)),
            ephemeral=True,
        )

    async def play_pet(self, interaction: discord.Interaction, pet_id: str) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        record = await self._resolve(guild, pet_id)
        if record is None:
            return await self._deny(
                interaction, "❌ I don't know that pet. It may have just been removed."
            )

        try:
            result = await asyncio.to_thread(
                play, guild.id, interaction.user.id, record.pet_id, record.toy
            )
        except FeedError as e:
            return await self._deny(interaction, f"❌ {e}")
        except Exception:
            log.exception("[pets] play failed")
            return await self._deny(
                interaction, "❌ Something went wrong. Try again in a moment."
            )

        rng = random.Random(f"play-{record.pet_id}-{interaction.user.id}-{_today()}")
        toy = result.toy or "toy"
        line = rng.choice(PLAY_LINES).format(pet=record.name, toy=toy)

        embed = discord.Embed(
            title=(
                f"{pet_treats.toy_emoji_for(toy)} {record.name} got their {toy}!"
                if result.own_toy
                else f"🧸 {record.name} has been played with"
            ),
            description=line,
            colour=EMBED_COLOUR,
        )
        embed.add_field(
            name="Played with",
            value=(
                f"{pet_treats.toy_label(toy)} — **their favourite**"
                if result.own_toy
                else pet_treats.toy_label(toy)
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"{interaction.user.display_name} · {result.plays_left} plays left today"
                f" · {record.name} has been played with {result.total} times"
            )
        )

        file = self._thumb(record)
        if file is not None:
            embed.set_thumbnail(url="attachment://pet.png")

        view = discord.ui.View(timeout=None)
        view.add_item(AboutButton(record.pet_id, f"🐾 About {record.name}"))

        await interaction.response.send_message(
            embed=embed, view=view, file=file or discord.utils.MISSING
        )
        self._after_post()

    async def send_treats(self, interaction: discord.Interaction) -> None:
        """Private on purpose — your allowance is yours, and posting it is noise."""
        if interaction.guild is None:
            return
        left, fed_ids = await asyncio.to_thread(
            treats_left, interaction.guild.id, interaction.user.id
        )
        plays, _played = await asyncio.to_thread(
            plays_left, interaction.guild.id, interaction.user.id
        )
        lines = [
            f"🍬 You have **{left}** of {DAILY_TREATS} treats left today.",
            f"🧸 And **{plays}** of {DAILY_PLAYS} plays.",
        ]
        names = []
        for pid in fed_ids:
            p = await asyncio.to_thread(pet_registry.get_pet, interaction.guild.id, pid)
            if p:
                names.append(p.name)
        if names:
            lines.append("Already fed: " + ", ".join(f"**{n}**" for n in names) + ".")
        if left == 0:
            lines.append("They come back at midnight.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def send_board(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        guild = interaction.guild
        pets = await asyncio.to_thread(top_pets, guild.id, BOARD_FETCH)
        feeders = await asyncio.to_thread(top_feeders, guild.id, BOARD_SIZE)

        embed = discord.Embed(
            title="🏆 The best-fed pets in Cozy Together", colour=EMBED_COLOUR
        )

        if pets:
            rows = []
            for pid, total, fav_id in pets:
                if len(rows) >= BOARD_SIZE:
                    break
                p = await asyncio.to_thread(pet_registry.get_pet, guild.id, pid)
                if p is None:
                    continue          # a pet removed before forget_pet existed
                i = len(rows) + 1
                owner = guild.get_member(p.owner_id)
                who = owner.display_name if owner else "?"
                if fav_id:
                    fav = guild.get_member(int(fav_id))
                    crown = f" · 👑 {fav.display_name if fav else 'someone who left'}"
                else:
                    crown = " · 👑 *up for grabs*"
                rows.append(f"`{i}.` **{p.name}** *({who}'s)* — {total} 🍖{crown}")
            embed.add_field(
                name="Most treats · 👑 favourite human",
                value="\n".join(rows) or "Nobody yet.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Most treats",
                value="Nobody has fed anything yet. Get on with it.",
                inline=False,
            )

        if feeders:
            rows = []
            for i, (uid, total) in enumerate(feeders, 1):
                m = guild.get_member(uid)
                rows.append(f"`{i}.` {m.display_name if m else 'someone who left'} — {total} 🍖")
            embed.add_field(name="Most generous", value="\n".join(rows), inline=False)

        embed.set_footer(text=f"{DAILY_TREATS} treats each per day · resets at midnight")
        await interaction.response.send_message(embed=embed)
        self._after_post()

    async def _pet_card(
        self, guild: discord.Guild, record: pet_registry.Pet, *,
        large: bool, with_stats: bool = True,
    ) -> tuple[discord.Embed, discord.File | None]:
        """One pet's public card: who they are, and optionally how they're doing.

        Two callers, differing in two ways. `large` gives the photo the full
        width instead of the corner. `with_stats` decides whether the feeding
        record is on it at all — About is a record of a pet, Show everyone is an
        introduction to one, and a league table of who fed them most is not part
        of an introduction.

        Built once rather than twice so the two cards can't drift into
        disagreeing about the same animal.
        """
        owner = guild.get_member(record.owner_id)

        embed = discord.Embed(
            title=f"🐾 {record.name}",
            description=f"{owner.display_name if owner else 'someone who left'}'s",
            colour=EMBED_COLOUR,
        )

        if with_stats:
            # The ledger is only read when something on the card needs it.
            stats = await asyncio.to_thread(pet_stats, guild.id, record.pet_id)
            by: dict[str, Any] = stats.get("by", {})

            embed.add_field(name="Treats eaten", value=str(stats.get("total", 0)))

            fav_id = _favourite_id(by)
            if fav_id:
                fav = guild.get_member(int(fav_id))
                embed.add_field(
                    name="Favourite human",
                    value=f"{fav.display_name if fav else 'someone who left'} ({by[fav_id]})",
                )
            else:
                embed.add_field(name="Favourite human", value="nobody yet")

            days = _days_since(stats.get("last_fed") or record.added)
            embed.add_field(
                name="Last fed",
                value="never" if days is None else "today" if days == 0 else f"{days} days ago",
            )

            ranked = sorted(by.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:5]
            if ranked:
                rows = []
                for uid, count in ranked:
                    m = guild.get_member(int(uid))
                    rows.append(f"**{count}** — {m.display_name if m else 'someone who left'}")
                embed.add_field(name="Fed by", value="\n".join(rows), inline=False)

        pet_profile.apply(embed, record)

        # With the stats gone, a pet whose owner never filled anything in would
        # be a name and a photo and nothing else — which reads as broken rather
        # than as empty. Say which it is.
        if not with_stats and not record.has_profile:
            embed.add_field(
                name="No bio yet",
                value=(
                    "Their owner can add one from 🐾 **Manage my pets** → ✏️ Edit bio."
                ),
                inline=False,
            )

        file = self._thumb(record)
        if file is not None:
            if large:
                embed.set_image(url="attachment://pet.png")
            else:
                embed.set_thumbnail(url="attachment://pet.png")
        return embed, file

    async def send_about(self, interaction: discord.Interaction, pet_id: str) -> None:
        if interaction.guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        record = await self._resolve(interaction.guild, pet_id)
        if record is None:
            return await self._deny(interaction, "❌ That pet isn't registered any more.")

        embed, file = await self._pet_card(interaction.guild, record, large=False)

        view = discord.ui.View(timeout=None)
        # No edit button. This card is public and shows anybody's pet, so the
        # edit sat in front of fifteen people who couldn't use it and one mod
        # who shouldn't. Editing lives in 🐾 Manage my pets, which only ever
        # lists your own.
        view.add_item(DexButton(0))

        await interaction.response.send_message(
            embed=embed, view=view, file=file or discord.utils.MISSING
        )
        self._after_post()

    async def post_bio(self, interaction: discord.Interaction, pet_id: str) -> None:
        """Put the dex entry you're looking at into the channel, for everyone.

        The dex has to be ephemeral — it marks which pets *you* have fed, and one
        shared message cannot show a different tick to each reader — so a pet you
        want to show somebody is otherwise stuck behind your own private message.
        This is the way out: the same card, public, with the photo given the full
        width rather than the corner.

        The ephemeral dex is left open behind it, so showing one pet doesn't cost
        you the page you were on.
        """
        guild = interaction.guild
        if guild is None:
            return
        if self._too_fast(interaction.user.id):
            return await self._deny(interaction, "⏳ Slow down.")

        record = await asyncio.to_thread(pet_registry.get_pet, guild.id, pet_id)
        if record is None:
            return await self._deny(interaction, "❌ That pet isn't registered any more.")

        # Bio only. This card is an introduction to a pet, not its feeding
        # record — the treat counts and the league table of who fed them most
        # live on the About card and the board, where they're the point.
        embed, file = await self._pet_card(
            guild, record, large=True, with_stats=False
        )
        # Says who put it there, since it arrives in the channel unprompted and
        # is otherwise a card that nobody appears to have asked for.
        embed.set_footer(text=f"shown by {interaction.user.display_name}")

        view = discord.ui.View(timeout=None)
        view.add_item(DexButton(0))

        # A new message rather than an edit: this interaction came from the
        # ephemeral dex, and answering it publicly is what puts the card in the
        # channel while leaving the dex where it was.
        await interaction.response.send_message(
            embed=embed, view=view, file=file or discord.utils.MISSING
        )
        self._after_post()

    # ──────────────────────────────────────────────────────────
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"⏳ Slow down. Try again in {error.retry_after:.1f}s.", ephemeral=True
                )
            return
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(f"🚫 {error}", ephemeral=True)
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    try:
        bot.add_dynamic_items(AboutButton, DexButton)
    except Exception:
        # A reload re-registers the same templates; the first registration stands.
        log.debug("[pets] dynamic items already registered", exc_info=True)

    cog = PetCare(bot)
    await bot.add_cog(cog)

    # Answers clicks on a panel this process did not post — see FoodBowlView.
    # An empty shell is enough; the custom_ids are what the dispatch matches on.
    bot.add_view(FoodBowlView(cog, None, []))
