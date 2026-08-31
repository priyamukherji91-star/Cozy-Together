# cogs/admin_panel.py
# -*- coding: utf-8 -*-
"""The staff panel: one message, one button, every staff command behind it.

Why this exists: the bot has around thirty slash commands, two thirds of them
gated to staff, and `/help` deliberately doesn't list those — it is written for
members. So the staff half of the bot had no index at all, and nobody remembers
twenty command names, which channel each one insists on, or which of them takes
a `kind:` choice.

Five things shape everything below.

**A bot cannot invoke a slash command on a user's behalf.** There is no API for
it. So every button reaches the *same* callback the slash command reaches —
`_call()` finds the command in the tree and calls `cmd.callback(cmd.binding, …)`
— rather than reimplementing what it does. A reimplementation is a second copy
that drifts, and the moment it drifts the panel is lying about what the bot
does. Where a button needed behaviour a command hadn't got, the *command* grew
it and both callers use it: `channel` on `/purge`, `private` on `/status_ideas`,
`/birthday add`, `/setup_panels`, the whole hand-added avatar store.

**Calling `.callback` skips everything the tree would have run first** — the
command's own `@app_commands.check` decorators *and* a cog-wide
`interaction_check` (which is why `moderation.py`'s role gate has to be
re-stated here). So the panel does two things about it. It re-runs the
command's decorator checks itself before calling (`_checks_pass`), so a button
can never be a wider door than typing the command. And each `Action` names a
gate for *visibility*, off the same predicates the cogs use, so a drawer shows
you what you can use and nothing else. Gates written inline in a command's body
(most of Mittens' are) still enforce themselves, in the command's own words,
which is why some buttons can still say no.

**One public message, and everything behind it is private.** Twenty-five
components is Discord's cap per message, and there are more commands than that,
so the panel is a door rather than a wall: one Open button, and what it gives
you is an ephemeral screen only you can see and drive. That also buys back the
dropdowns — a channel or member select on a *shared* message would show every
mod whatever the last person picked, but on your own ephemeral copy it is yours.

**A screen asks one question.** A drawer is buttons; pressing one opens *its*
picks and nothing else. When the last required answer lands and there is nothing
optional left to weigh, the dropdown carries straight on into the command
(`PickScreen.ready`), so the common case is press, choose, done. Picks are
cleared on the way in, because a value remembered from the last button is a
value you can no longer see.

**The explaining happens one screen down.** The public message is a title, a
line and the door. The home screen is buttons and nothing else. A drawer's
embed lists that drawer's buttons, one line each (`Action.blurb`), because by
then you have narrowed thirty commands to five and the labels alone won't
separate "Generate a paper" from "Post today's paper".

Three things a button cannot do, and what happens instead:

*Channel-locked commands.* `/event`, the admin `/birthday` commands and the
`avatar_*` commands each insist on the admin-commands channel, and an
interaction happens where the panel is. Rather than park those buttons, each of
those cogs now accepts this channel as well as its own, through one named
constant. Widening a staff command from one staff channel to two is not a
permission change.

*Uploads.* A Discord modal holds text inputs and nothing else, so "add an
avatar" asks for the name and dates in a modal and then watches the channel for
the picture — the same shape `pet_care` uses for photo swaps, because staff have
met it there. `seasonal_avatar` owns that wait; the panel only starts it.

*Two commands, one job.* The landing gate and the get-roles menus live in two
cogs because they are two channels, but reposting one without the other is how
a landing zone ends up pointing at menus that are gone. That became one command
(`/setup_panels`) rather than a button that fires two, because a button gets one
interaction and the first callback spends it.

**What keeps itself up to date, and what tells you when it hasn't.** 📖 Slash
Commands is read off the live tree minus whatever already has a button, so a
newly added command appears there with no edit here. Every drawer line falls
back to the live command description when an `Action` has no blurb. And on boot
`_audit()` walks every button, checks the command behind it still exists and
still takes the arguments the button supplies, and says so on the panel itself
if it doesn't — a renamed parameter is otherwise a button that fails only when
somebody presses it. Buttons do *not* auto-appear: supplying a command's
arguments is a decision. Add an `Action` to `_sections()` and it appears.

**A button that isn't wired up yet stays.** `Action.parked` builds the button
and answers the press with a line saying why nothing happened. Greying it out
reads as "not for you", and deleting it loses the note.

The interaction token behind an ephemeral message dies after 15 minutes, so a
screen left open that long stops answering and Open starts a fresh one — the
right failure, since a stale staff screen is worth less than nothing.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from common import lines as lines_registry
from common import storage

log = logging.getLogger("cozy.admin_panel")

# Where the panel lives: the staff channel that already collects the output of
# half its own buttons — the morning-news test post and the status ideas both
# land here.
PANEL_CHANNEL_ID = 1426295618934149212

# Who the panel is for. The same pair `events.py`, `morning_news.py` and
# `ffxiv_resets.py` check for their own staff gates, re-stated rather than
# imported so that one cog failing to load can't take the panel's door with it.
STAFF_ROLE_IDS: frozenset[int] = frozenset({1426194314337189949, 1425977436859797595})

# The narrowest gate: one account, by id. Matches `pet_care.OWNER_USER_ID`.
OWNER_USER_ID = 1130859582407847977

PANEL_PATH = storage.DATA_DIR / "admin_panel.json"

PANEL_TITLE = "😾 Mittens' Panel"

# Every title the panel has ever had. This is what the sweeper matches on when
# it clears old panels out of the channel, so a title it doesn't recognise is a
# message nothing will ever tidy up. Add names here when the title changes;
# never remove one — an old name costs nothing and dropping it strands whatever
# is still sitting in the channel wearing it.
PANEL_TITLES: frozenset[str] = frozenset({PANEL_TITLE})
STRAY_SCAN = 100
STRAY_DELETE_CAP = 40

# How long the channel has to go quiet before the panel moves to the bottom. A
# burst of conversation has to cost exactly one repost; the floor underneath is
# the circuit breaker in case anything ever makes the panel react to its own
# arrival, which produces a new panel every few seconds, forever — pet_care
# shipped that bug once, and this is deliberately not derived from the debounce.
REPOST_DEBOUNCE = 3.0
MIN_PLACE_INTERVAL = 8.0

# How long a private screen stays live. Under Discord's own 15-minute limit on
# an interaction token, so a stale one goes grey rather than throwing
# "This interaction failed" at you.
SESSION_TIMEOUT = 12 * 60

BUTTON_STYLE = discord.ButtonStyle.primary

# Mittens' pink, the same one the pet panel uses, so his two panels read as one
# bot. Red stays reserved for the buttons that delete or publish things.
COLOUR = discord.Colour(0xE0708A)
COLOUR_DANGER = discord.Colour.from_str("#ED4245")

DENY = "You don't have paws for that. 🐾"

# The lines editor: how many lines to a page, and how much of a long one the
# list shows before it is cut.
LINES_PER_PAGE = 10
LINES_PREVIEW = 180


# ── the gates, as data ────────────────────────────────────────────────────────
#
# Not a ladder. Mittens' cogs don't share one — `moderation.py` wants two role
# *names*, `pet_care.py` wants Manage Server, the news wants two role *ids* — so
# these are named gates that mirror what each command already checks, and an
# `Action` names the one its own command uses. Nothing here invents a new
# definition of "allowed".

def _is_owner(member: discord.abc.User) -> bool:
    return member.id == OWNER_USER_ID


def _is_admin(member: discord.Member) -> bool:
    """Manage Server or Administrator — `pet_care._is_admin`, and birthday's."""
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


def _is_staff(member: discord.Member) -> bool:
    """The two staff roles the news and event commands check for, plus admins."""
    return bool(STAFF_ROLE_IDS & {r.id for r in member.roles}) or _is_admin(member)


def _has_mod_power(member: discord.Member) -> bool:
    """`moderation.py`'s own gate — Mama Cat or Ghoul, and not blocked.

    Imported inside the call so a broken `moderation.py` costs its three buttons
    rather than the whole panel, and returns False in that case rather than
    falling back to something broader: a widening on the way out of an error is
    exactly the kind nobody notices.
    """
    try:
        from cogs.moderation import has_mittens_power, is_blocked  # noqa: PLC0415
    except Exception:
        log.debug("[panel] moderation.py is not importable", exc_info=True)
        return False
    return has_mittens_power(member) and not is_blocked(member)


_GATES: dict[str, Callable[[discord.Member], bool]] = {
    "owner": _is_owner,
    "admin": _is_admin,
    "staff": _is_staff,
    "mod": _has_mod_power,
}


def _passes(gate: str, member: discord.Member) -> bool:
    return _GATES[gate](member)


def may_open(member: discord.Member) -> bool:
    """Anyone who can press anything can open the door.

    In practice: the two staff roles, admins, whoever holds Mama Cat or Ghoul,
    and the owner. This is also what `pet_care.panel_staff_only` and `help.py`
    ask, so "staff" has one definition and it lives here.
    """
    return any(gate(member) for gate in _GATES.values())


# ── reaching the real commands ────────────────────────────────────────────────

def _find_command(
    bot: commands.Bot, qualified: str, guild: Optional[discord.Guild]
) -> Optional[app_commands.Command]:
    """Look a slash command up by qualified name, e.g. "birthday remove".

    Both scopes are searched because `bot.py` copies the global set into the dev
    guild when GUILD_ID is set and syncs globally when it isn't, so which scope
    holds a command depends on how the bot was started.
    """
    scopes: list[Any] = [guild, None] if guild else [None]
    parts = qualified.split()
    for scope in scopes:
        node: Any = bot.tree.get_command(parts[0], guild=scope)
        for part in parts[1:]:
            if not isinstance(node, app_commands.Group):
                node = None
                break
            node = node.get_command(part)
        if isinstance(node, app_commands.Command):
            return node
    return None


async def _checks_pass(
    cmd: app_commands.Command, interaction: discord.Interaction
) -> Optional[str]:
    """None if the command's own decorators would have let this through.

    The tree runs these before a typed command; calling `.callback` doesn't, so
    the panel runs them itself. Without it a button would be a wider door than
    the command it stands for.

    A check that raises is a refusal (that is how `pet_care`'s gates and
    discord.py's own `has_permissions` say no), and its message is worth more
    than anything invented here, so it comes back as the reason.
    """
    for check in getattr(cmd, "checks", []):
        try:
            result = check(interaction)
            if asyncio.iscoroutine(result):
                result = await result
        except app_commands.AppCommandError as e:
            return str(e) or DENY
        except Exception:
            log.debug("[panel] a check on /%s blew up", cmd.qualified_name, exc_info=True)
            return DENY
        if not result:
            return DENY
    return None


async def _call(interaction: discord.Interaction, qualified: str, **kwargs: Any) -> None:
    """Run a slash command's own callback on this interaction.

    The interaction handed in must be *unanswered* — the callback answers it, in
    its own words, exactly as it would have if you had typed the command. That
    is the whole point, and it is the constraint that shapes every flow here: a
    button that has to ask something first spends its own interaction on the
    asking and passes the fresh one along to this.
    """
    bot: commands.Bot = interaction.client  # type: ignore[assignment]
    cmd = _find_command(bot, qualified, interaction.guild)
    if cmd is None:
        await _fail(interaction, f"`/{qualified}` isn't loaded right now.")
        return

    refusal = await _checks_pass(cmd, interaction)
    if refusal is not None:
        await _fail(interaction, refusal)
        return

    try:
        if cmd.binding is not None:
            await cmd.callback(cmd.binding, interaction, **kwargs)
        else:
            await cmd.callback(interaction, **kwargs)  # type: ignore[call-arg]
    except Exception:
        log.exception("[panel] /%s failed", qualified)
        await _fail(interaction, f"`/{qualified}` went wrong. The log has the details.")


async def _fail(interaction: discord.Interaction, text: str) -> None:
    """Say so, whether or not the interaction has already been answered."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {text}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {text}", ephemeral=True)
    except discord.HTTPException:
        log.debug("[panel] could not deliver an error message", exc_info=True)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── the registry ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pick:
    """Something a command needs that a dropdown supplies.

    `kind` is which dropdown ("channel", "member", "minutes", "avatar",
    "added_avatar") and `param` is what that value is called in the command's
    signature — they differ often enough to be worth separating: the same member
    dropdown feeds `user=` to `/timeout` and `member=` to `/birthday remove`.

    `cast` bridges the rest of the gap. A dropdown can only hand back a string
    or a Discord object, and `/timeout` wants "10m".
    """
    kind: str
    param: str
    required: bool = True
    cast: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class Action:
    """One button, and everything about what pressing it does.

    Declarative rather than a callback, because the steps compose: a button may
    need a dropdown value, *then* a confirmation, *then* a modal, and spelling
    that out as data keeps the order in one place instead of in twenty closures.

    `opens` is the escape hatch, for the two buttons that are not a command at
    all — the lines editor is screens, and adding an avatar has to wait for an
    upload. Everything else is `command`.

    `blurb` is the line the drawer prints next to the button. It is written here
    rather than read off the command's own description because a button is not
    always the whole command, and because a description written for the
    slash-command picker is written for whoever is typing it. Left out, the
    drawer falls back to the live command description, so a new action still
    says something.
    """
    label: str
    emoji: str
    gate: str
    command: str = ""
    opens: str = ""                  # key into _FLOWS, for the non-command buttons
    blurb: str = ""
    style: discord.ButtonStyle = BUTTON_STYLE
    confirm: Optional[str] = None
    picks: tuple[Pick, ...] = ()
    modal: Optional[str] = None      # key into _MODALS
    kwargs: dict[str, Any] = field(default_factory=dict)
    parked: str = ""                 # built and visible, but says this instead of running


@dataclass(frozen=True)
class Section:
    """A drawer: a heading, the buttons in it, and any drawer inside it.

    `subs` is for a group that is one idea with several verbs — his avatar is
    add, remove, pin, sync and schedule, and spreading those five across the
    drawer that also rotates his status says they are unrelated things. One
    level and no further: past that you are navigating rather than working.

    `blurb` is a line above the buttons, for a drawer that needs a word of
    warning rather than a description of each button.
    """
    label: str
    emoji: str
    actions: tuple[Action, ...]
    subs: tuple["Section", ...] = ()
    blurb: str = ""


@dataclass
class Session:
    """One person's trip through the panel.

    Ephemeral and per-click, which is what makes the dropdowns safe: the values
    live here, not on a shared message, so two mods can be halfway through the
    same drawer without touching each other. Keyed by *kind* rather than by
    parameter name, so one channel dropdown serves every button in the drawer.
    """
    panel: "AdminPanel"
    member: discord.Member
    values: dict[str, Any] = field(default_factory=dict)

    def gather(self, action: Action) -> Optional[dict[str, Any]]:
        """Map picked values onto the command's parameters, or None if short."""
        out: dict[str, Any] = dict(action.kwargs)
        for pick in action.picks:
            value = self.values.get(pick.kind)
            if value is None:
                if pick.required:
                    return None
                continue
            out[pick.param] = pick.cast(value) if pick.cast else value
        return out

    def missing(self, action: Action) -> list[str]:
        return [
            _KIND_WORD[p.kind]
            for p in action.picks
            if p.required and self.values.get(p.kind) is None
        ]


_KIND_WORD = {
    "channel": "a channel",
    "member": "a member",
    "minutes": "how long",
    "avatar": "a picture",
    "added_avatar": "which one",
}

# `/timeout` parses its own duration string and takes suffixes this list doesn't
# offer, so these are the offer rather than the rule — type the command for the
# awkward ones.
_MINUTE_CHOICES: tuple[int, ...] = (1, 5, 10, 15, 30, 60, 120, 1440)


def _minute_word(minutes: int) -> str:
    if minutes >= 1440:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes >= 60:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _duration_arg(minutes: int) -> str:
    """What `/timeout` wants: a string it can parse. It reads "m" as minutes."""
    return f"{int(minutes)}m"


# ── modals ────────────────────────────────────────────────────────────────────
#
# Every modal's submit interaction is unanswered, which is what lets it be
# handed straight to `_call`. None of them answer it themselves. `gathered` is
# whatever the dropdowns and the action's static kwargs already produced.

class _Modal(discord.ui.Modal):
    def __init__(self, title: str, action: Action, gathered: dict[str, Any]) -> None:
        super().__init__(title=title[:45], timeout=SESSION_TIMEOUT)
        self.action = action
        self.gathered = gathered

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("[panel] %s failed", type(self).__name__, exc_info=error)
        await _fail(interaction, "That didn't go through.")


class PurgeModal(_Modal):
    """How many. Typing a number *is* the confirmation, which is why Purge has
    no separate are-you-sure screen in front of it."""

    amount = discord.ui.TextInput(
        label="How many messages?", placeholder="1–200", max_length=3, required=True
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.amount.value).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 200:
            await _fail(interaction, "That needs to be a number from 1 to 200.")
            return
        await _call(interaction, self.action.command, amount=int(raw), **self.gathered)


class BirthdayModal(_Modal):
    """The date. The member came from the dropdown behind this."""

    date = discord.ui.TextInput(
        label="Their birthday (DD/MM)", placeholder="05/09", max_length=5, required=True
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Not validated here: `/birthday add` parses it with the same
        # `_valid_ddmm` that `/birthday set` uses, and says no in its own words.
        # A second opinion in here is a second thing to keep in step.
        await _call(
            interaction, self.action.command, date=str(self.date.value).strip(), **self.gathered
        )


_MODALS: dict[str, type[_Modal]] = {
    "purge": PurgeModal,
    "birthday": BirthdayModal,
}


def _modal_title(action: Action, gathered: dict[str, Any]) -> str:
    """Name the target in the title, so the modal is its own confirmation."""
    for value in gathered.values():
        if isinstance(value, (discord.TextChannel, discord.Thread)):
            return f"{action.label} in #{value.name}"
        if isinstance(value, discord.Member):
            return f"{action.label}: {value.display_name}"
    return action.label


class AvatarAddModal(discord.ui.Modal, title="Add an avatar"):
    """Name it and date it; the picture comes next.

    Everything here is checked *before* the upload is asked for — being told the
    window is taken should not cost you a photo. The checks are
    `seasonal_avatar`'s own, so the modal cannot disagree with the store.
    """

    name = discord.ui.TextInput(
        label="Name it", placeholder="anniversary", max_length=24, required=True
    )
    start = discord.ui.TextInput(
        label="First day up (DD/MM/YYYY)", placeholder="24/12/2026", required=True
    )
    end = discord.ui.TextInput(
        label="Last day up (DD/MM/YYYY)", placeholder="26/12/2026", required=True
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            from cogs.seasonal_avatar import (  # noqa: PLC0415
                AvatarError, check_key, check_window, parse_day,
            )
        except Exception:
            await _fail(interaction, "The avatar cog isn't loaded right now.")
            return

        try:
            key = check_key(str(self.name.value))
            first = parse_day(str(self.start.value))
            last = parse_day(str(self.end.value))
            check_window(first, last, ignore=key)
        except AvatarError as exc:
            await _fail(interaction, str(exc))
            return

        cog = interaction.client.get_cog("SeasonalAvatar")
        if cog is None:
            await _fail(interaction, "The avatar cog isn't loaded right now.")
            return
        # It answers this interaction itself and then waits for the picture.
        await cog.await_custom_upload(interaction, key, first, last)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("[panel] avatar modal failed", exc_info=error)
        await _fail(interaction, "That didn't go through.")


# ── the dropdowns ─────────────────────────────────────────────────────────────

async def _picked(item: discord.ui.Item, interaction: discord.Interaction) -> None:
    """Redraw the pick screen, or move on if that was the last answer needed."""
    view = item.view
    if isinstance(view, PickScreen) and view.ready():
        await _advance(view.session, interaction, view.action, view.section)
        return
    await interaction.response.edit_message(view=view)


async def _advance(
    session: Session,
    interaction: discord.Interaction,
    action: Action,
    section: Optional[Section] = None,
) -> None:
    """Everything the picks were for: the are-you-sure, or the command itself."""
    if action.confirm:
        view = ConfirmScreen(session, action, section)
        await interaction.response.edit_message(embed=view.embed(), view=view)
        return
    await _run(session, interaction, action)


async def _run(session: Session, interaction: discord.Interaction, action: Action) -> None:
    """The last step: hand the command its interaction, or open its modal."""
    gathered = session.gather(action)
    if gathered is None:
        await _fail(interaction, f"Choose {' and '.join(session.missing(action))} first.")
        return
    if action.modal:
        await interaction.response.send_modal(
            _MODALS[action.modal](_modal_title(action, gathered), action, gathered)
        )
        return
    await _call(interaction, action.command, **gathered)


def _avatar_options() -> list[discord.SelectOption]:
    """Every face he has, read off `seasonal_avatar` rather than copied.

    Imported inside the function so a broken `seasonal_avatar` costs this one
    dropdown instead of the whole panel.
    """
    try:
        from cogs.seasonal_avatar import known_keys  # noqa: PLC0415
    except Exception:
        log.debug("[panel] could not read the avatar list", exc_info=True)
        return []
    options = [
        discord.SelectOption(
            label="auto", value="auto", description="Back to following the calendar"
        )
    ]
    options += [discord.SelectOption(label=key, value=key) for key in known_keys()]
    return options[:25]


def _added_avatar_options() -> list[discord.SelectOption]:
    """Only the hand-added ones — the built-ins are rules and cannot be removed."""
    try:
        from cogs.seasonal_avatar import custom_windows  # noqa: PLC0415
    except Exception:
        log.debug("[panel] could not read the added avatars", exc_info=True)
        return []
    return [
        discord.SelectOption(
            label=key, value=key, description=f"{s:%d %b %Y} → {e:%d %b %Y}"[:100]
        )
        for key, s, e in custom_windows()
    ][:25]


class ChannelPick(discord.ui.ChannelSelect):
    def __init__(self, session: Session, row: int) -> None:
        super().__init__(
            placeholder="Channel",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            row=row,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen = self.values[0]
        # A ChannelSelect hands back a partial object; the commands behind these
        # buttons want the real thing.
        resolved = interaction.guild.get_channel(chosen.id) if interaction.guild else None
        if not isinstance(resolved, discord.TextChannel):
            await _fail(interaction, "I can't reach that channel.")
            return
        self.session.values["channel"] = resolved
        self.placeholder = f"#{resolved.name}"
        await _picked(self, interaction)


class MemberPick(discord.ui.UserSelect):
    def __init__(self, session: Session, row: int) -> None:
        super().__init__(placeholder="Member", row=row)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen = self.values[0]
        # A UserSelect can hand back a plain User for someone who has left. Every
        # command behind these buttons wants a Member, so it is resolved here and
        # refused now rather than blowing up downstream.
        member = interaction.guild.get_member(chosen.id) if interaction.guild else None
        if member is None:
            await _fail(interaction, f"{chosen} isn't in the server any more.")
            return
        self.session.values["member"] = member
        self.placeholder = member.display_name
        await _picked(self, interaction)


class ListPick(discord.ui.Select):
    """A fixed list of strings — the avatar keys, in both directions."""

    def __init__(self, session: Session, kind: str, row: int) -> None:
        options = _avatar_options() if kind == "avatar" else _added_avatar_options()
        placeholder = "Which picture" if kind == "avatar" else "Which added one"
        empty = (
            "no pictures found" if kind == "avatar" else "nothing has been added yet"
        )
        super().__init__(
            placeholder=placeholder,
            options=options or [discord.SelectOption(label=empty, value="-")],
            disabled=not options,
            row=row,
        )
        self.session = session
        self.kind = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        self.session.values[self.kind] = self.values[0]
        self.placeholder = self.values[0]
        await _picked(self, interaction)


class MinutePick(discord.ui.Select):
    """How long. Values come back as strings; the Pick's `cast` turns the int
    into whatever the command wants."""

    def __init__(self, session: Session, row: int) -> None:
        super().__init__(
            placeholder="How long",
            options=[
                discord.SelectOption(label=_minute_word(m), value=str(m))
                for m in _MINUTE_CHOICES
            ],
            row=row,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        minutes = int(self.values[0])
        self.session.values["minutes"] = minutes
        self.placeholder = _minute_word(minutes)
        await _picked(self, interaction)


# ── the private screens ───────────────────────────────────────────────────────

class Screen(discord.ui.View):
    """Base for every ephemeral screen. Locks itself to whoever opened it.

    The message is ephemeral, so nobody else can see the buttons in the first
    place — the check is for the day that stops being true rather than for today.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(timeout=SESSION_TIMEOUT)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.member.id:
            return True
        await _fail(interaction, "That isn't your panel.")
        return False

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("[panel] %s failed", type(item).__name__, exc_info=error)
        await _fail(interaction, "That didn't go through.")


class BackButton(discord.ui.Button):
    """One step back, not all the way out.

    A pick screen came from a drawer, so Back returns to that drawer; a drawer
    came from home, or from the drawer above it. Abandoning a half-made choice
    should not also lose the buttons you were choosing between.
    """

    def __init__(
        self, session: Session, row: int, section: Optional[Section] = None
    ) -> None:
        super().__init__(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)
        self.session = session
        self.section = section

    async def callback(self, interaction: discord.Interaction) -> None:
        view: Screen = (
            SectionScreen(self.session, self.section)
            if self.section is not None
            else HomeScreen(self.session)
        )
        await interaction.response.edit_message(embed=view.embed(), view=view)


class ActionButton(discord.ui.Button):
    """A button that stands for a command, or for one of the two flows.

    Pressing one starts that command's own flow — its picks, then its
    are-you-sure, then its modal — and each step spends exactly one interaction,
    because a callback must be handed an unanswered one. Where there is nothing
    to ask, this click *is* the command's click and it replies in its own voice.

    The picks are cleared on the way in. A dropdown only appears after a press,
    so a remembered value would be an invisible one.
    """

    def __init__(
        self, session: Session, action: Action, row: int, section: Optional[Section] = None
    ) -> None:
        super().__init__(label=action.label, emoji=action.emoji, style=action.style, row=row)
        self.session = session
        self.action = action
        self.section = section

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action.parked:
            # Kept in place on purpose: the button is right, the thing on the
            # other end of it isn't ready. Saying so beats grey (which reads as
            # "not for you") and beats deleting it (which loses the note).
            await interaction.response.send_message(
                f"⏸️ {self.action.parked}", ephemeral=True
            )
            return
        self.session.values.clear()
        if self.action.opens:
            await _FLOWS[self.action.opens](self.session, interaction, self.section)
            return
        if self.action.picks:
            view = PickScreen(self.session, self.action, self.section)
            await interaction.response.edit_message(embed=view.embed(), view=view)
            return
        await _advance(self.session, interaction, self.action, self.section)


class PickScreen(Screen):
    """One button's questions, on their own screen.

    `ready()` is the auto-advance rule: when every required pick is answered and
    there is nothing optional left to decide, the last dropdown carries straight
    on into the command. An optional pick means only you know when you're done,
    so those get a Continue button instead.
    """

    def __init__(
        self, session: Session, action: Action, section: Optional[Section] = None
    ) -> None:
        super().__init__(session)
        self.action = action
        self.section = section

        row = 0
        for pick in action.picks:
            if pick.kind == "channel":
                self.add_item(ChannelPick(session, row))
            elif pick.kind == "member":
                self.add_item(MemberPick(session, row))
            elif pick.kind == "minutes":
                self.add_item(MinutePick(session, row))
            else:
                self.add_item(ListPick(session, pick.kind, row))
            row += 1

        if self._optional:
            self.add_item(ContinueButton(session, action, section, row))
        self.add_item(BackButton(session, row, section))

    @property
    def _optional(self) -> bool:
        return any(not p.required for p in self.action.picks)

    def ready(self) -> bool:
        """True when the picks can carry themselves into the command."""
        return not self._optional and not self.session.missing(self.action)

    def embed(self) -> discord.Embed:
        need = " and ".join(
            _KIND_WORD[p.kind] for p in self.action.picks if p.required
        )
        spare = " and ".join(
            _KIND_WORD[p.kind] for p in self.action.picks if not p.required
        )
        body = [self.action.blurb] if self.action.blurb else []
        body.append(
            f"Choose {need}, then Continue — {spare} is optional."
            if spare else f"Choose {need}."
        )
        return discord.Embed(
            title=f"{self.action.emoji} {self.action.label}",
            description="\n\n".join(body),
            colour=COLOUR,
        )


class ContinueButton(discord.ui.Button):
    """Only on the screens that can't know when you've finished picking."""

    def __init__(
        self, session: Session, action: Action, section: Optional[Section], row: int
    ) -> None:
        super().__init__(label="Continue", emoji="➡️", style=discord.ButtonStyle.primary, row=row)
        self.session = session
        self.action = action
        self.section = section

    async def callback(self, interaction: discord.Interaction) -> None:
        missing = self.session.missing(self.action)
        if missing:
            await _fail(interaction, f"Choose {' and '.join(missing)} first.")
            return
        await _advance(self.session, interaction, self.action, self.section)


class ConfirmScreen(Screen):
    """The one gate in front of the actions that are hard to take back."""

    def __init__(
        self, session: Session, action: Action, section: Optional[Section] = None
    ) -> None:
        super().__init__(session)
        self.action = action
        self.section = section
        # Confirming spends the interaction on the command, so this screen can't
        # also grey itself out — one interaction, one response. The flag is what
        # stops a second press re-running something slow and irreversible that
        # gave no visible sign of having started.
        self._spent = False

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"{self.action.emoji} {self.action.label}",
            description=self.action.confirm or "",
            colour=COLOUR_DANGER,
        )

    @discord.ui.button(label="Do it", emoji="✅", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if self._spent:
            await _fail(interaction, "Already done — press Back and start again to repeat it.")
            return
        self._spent = True
        await _run(self.session, interaction, self.action)

    @discord.ui.button(label="Never mind", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def nope(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        view: Screen = (
            SectionScreen(self.session, self.section)
            if self.section is not None
            else HomeScreen(self.session)
        )
        await interaction.response.edit_message(embed=view.embed(), view=view)


class SectionScreen(Screen):
    """A drawer: its sub-drawers, its buttons, then Back. Nothing else."""

    def __init__(
        self, session: Session, section: Section, origin: Optional[Section] = None
    ) -> None:
        super().__init__(session)
        self.section = section
        self.origin = origin

        subs = session.panel.visible_subs(section, session.member)
        allowed = session.panel.visible_actions(section, session.member)
        # Sub-drawers first: they are the bigger step, and putting them under the
        # buttons hides them below the fold.
        for index, sub in enumerate(subs):
            self.add_item(SectionButton(session, sub, index // 5, section))
        offset = len(subs)
        for index, action in enumerate(allowed):
            self.add_item(ActionButton(session, action, (offset + index) // 5, section))
        self.add_item(BackButton(session, (offset + len(allowed)) // 5, origin))

    def embed(self) -> discord.Embed:
        """Title, the drawer's own warning if it has one, then a line per button.

        This is the *only* screen that explains anything, and that is the point:
        by the time you are here you have already said which handful of things
        you are choosing between. Only what you can press is described — the
        list is built off `visible_actions`, the same call that builds the
        buttons, so a drawer never explains something that isn't there.
        """
        body: list[str] = []
        if self.section.blurb:
            body.append(self.section.blurb)
            body.append("")
        for sub in self.session.panel.visible_subs(self.section, self.session.member):
            body.append(
                f"{sub.emoji} **{sub.label}** — "
                f"{sub.blurb or f'{len(sub.actions)} more inside.'}"
            )
        for action in self.session.panel.visible_actions(self.section, self.session.member):
            body.append(f"{action.emoji} **{action.label}** — {self._describe(action)}")
        return discord.Embed(
            title=f"{self.section.emoji} {self.section.label}",
            description="\n".join(body)[:4000] or None,
            colour=COLOUR,
        )

    def _describe(self, action: Action) -> str:
        """The action's own line, or the live command's description behind it.

        The fallback is there so an `Action` added without a blurb still says
        something rather than printing an em dash and nothing, and it reads off
        the tree rather than a copy, for the same reason the buttons call the
        real callback.
        """
        if action.blurb:
            return action.blurb
        if not action.command:
            return "Opens its own screen."
        cmd = _find_command(
            self.session.panel.bot, action.command, self.session.member.guild
        )
        text = (cmd.description or "").strip() if cmd else ""
        return text or f"Runs /{action.command}."


class CatalogueButton(discord.ui.Button):
    def __init__(self, session: Session, row: int) -> None:
        super().__init__(label="Slash Commands", emoji="📖", style=BUTTON_STYLE, row=row)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            embed=await _catalogue_embed(interaction, self.session.panel), ephemeral=True
        )


class SectionButton(discord.ui.Button):
    """Opens a drawer. `origin` is set when the drawer is inside another one, so
    Back from it lands where you came from rather than at home.

    Not `parent`: `discord.ui.Item.parent` is a read-only property from 2.6
    onwards, and assigning it raises.
    """

    def __init__(
        self, session: Session, section: Section, row: int, origin: Optional[Section] = None
    ) -> None:
        super().__init__(
            label=section.label, emoji=section.emoji, style=BUTTON_STYLE, row=row
        )
        self.session = session
        self.section = section
        self.origin = origin

    async def callback(self, interaction: discord.Interaction) -> None:
        # The session survives navigation — a fresh one per screen would lose the
        # channel you picked on the way in.
        view = SectionScreen(self.session, self.section, self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class HomeScreen(Screen):
    """The drawers, the one loose button, and the catalogue. Labels only.

    Creating an event sits out here rather than in a drawer of its own: a drawer
    holding one button is a click that asks a question with one answer, and this
    is the thing staff press most.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._sections = [
            s for s in session.panel.sections
            if session.panel.section_visible(s, session.member)
        ]
        count = 0
        for section in self._sections:
            self.add_item(SectionButton(session, section, count // 5))
            count += 1
        for action in session.panel.visible_home_actions(session.member):
            self.add_item(ActionButton(session, action, count // 5))
            count += 1
        self.add_item(CatalogueButton(session, count // 5))

    def embed(self) -> discord.Embed:
        return discord.Embed(title=PANEL_TITLE, colour=COLOUR)


# ── his own lines ─────────────────────────────────────────────────────────────
#
# A small editor for `common/lines.py`: the pools he picks his wording from,
# with the coded version as the default and edits kept on the volume, so they
# survive a deploy.
#
# Editing is one line at a time rather than one big text box. A modal input caps
# at 4000 characters and the status pool alone is over 3000, so "paste the whole
# thing back" cannot be the only way in — and one line at a time is also what
# makes a typo cost one line rather than ninety-seven.

def _line_pages(key: str) -> list[list[tuple[int, str]]]:
    """The pool cut into pages of (index, line), index counted from the whole."""
    rows = list(enumerate(lines_registry.pool(key)))
    return [rows[i:i + LINES_PER_PAGE] for i in range(0, len(rows), LINES_PER_PAGE)] or [[]]


class PoolSelect(discord.ui.Select):
    def __init__(self, session: Session, origin: Optional[Section], row: int) -> None:
        options = [
            discord.SelectOption(
                label=_shorten(pool.label, 100),
                value=pool.key,
                description=_shorten(
                    f"{len(lines_registry.pool(pool.key))} lines · {pool.where}", 100
                ),
                emoji="✏️" if lines_registry.is_overridden(pool.key) else None,
            )
            for pool in lines_registry.pools()[:25]
        ]
        super().__init__(
            placeholder="Which lines?",
            options=options or [discord.SelectOption(label="nothing registered", value="-")],
            disabled=not options,
            row=row,
        )
        self.session = session
        self.origin = origin

    async def callback(self, interaction: discord.Interaction) -> None:
        view = PoolScreen(self.session, self.values[0], origin=self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class LinesScreen(Screen):
    """Every pool, with the edited ones marked."""

    def __init__(self, session: Session, origin: Optional[Section] = None) -> None:
        super().__init__(session)
        self.origin = origin
        self.add_item(PoolSelect(session, origin, 0))
        self.add_item(BackButton(session, 1, origin))

    def embed(self) -> discord.Embed:
        rows = []
        for pool in lines_registry.pools():
            mark = " ✏️ *edited*" if lines_registry.is_overridden(pool.key) else ""
            rows.append(
                f"**{pool.label}** — {len(lines_registry.pool(pool.key))} lines · "
                f"{pool.where}{mark}"
            )
        return discord.Embed(
            title="✒️ Mittens' custom lines",
            description=(
                "What he says, and where. Pick a set to read it, change it, or add "
                "to it — edits are live immediately and survive a redeploy.\n"
                "**Everything he picks at random is in this list.** Fixed labels and "
                "one-off messages are still in the code.\n\n"
                + "\n".join(rows)
            )[:4000],
            colour=COLOUR,
        )


class PoolScreen(Screen):
    """One pool: the lines, and what you can do to them."""

    def __init__(
        self, session: Session, key: str, page: int = 0, origin: Optional[Section] = None
    ) -> None:
        super().__init__(session)
        self.key = key
        self.origin = origin
        self.pages = _line_pages(key)
        self.page = max(0, min(page, len(self.pages) - 1))

        row = 0
        if len(self.pages) > 1:
            self.add_item(
                PageButton(session, key, self.page - 1, "◀", self.page == 0, row, origin)
            )
            self.add_item(
                PageButton(
                    session, key, self.page + 1, "▶",
                    self.page >= len(self.pages) - 1, row, origin,
                )
            )
            row = 1
        self.add_item(LineActionButton(session, key, self.page, "add", "Add a line", "➕", row, origin))
        self.add_item(LineActionButton(session, key, self.page, "edit", "Edit a line", "✏️", row, origin))
        self.add_item(LineActionButton(session, key, self.page, "remove", "Remove a line", "🗑️", row, origin))
        if lines_registry.is_overridden(key):
            self.add_item(ResetLinesButton(session, key, row, origin))
        self.add_item(BackToListButton(session, origin, row + 1))

    def embed(self) -> discord.Embed:
        pool = lines_registry.get_pool(self.key)
        title = pool.label if pool else self.key
        rows = self.pages[self.page]
        body = "\n".join(
            f"**{index + 1}.** {_shorten(line, LINES_PREVIEW)}" for index, line in rows
        ) or "*Nothing here yet.*"

        total = sum(len(page) for page in self.pages)
        header = [f"{pool.where}." if pool and pool.where else ""]
        if pool:
            header.append(pool.hint())
        header.append(
            "✏️ Edited — Reset puts the original lines back."
            if lines_registry.is_overridden(self.key)
            else "These are the lines as written in the code."
        )

        embed = discord.Embed(
            title=f"✒️ {title}",
            description=("\n".join(x for x in header if x) + f"\n\n{body}")[:4000],
            colour=COLOUR,
        )
        embed.set_footer(
            text=f"{total} line{'s' if total != 1 else ''}"
            + (f" · page {self.page + 1} of {len(self.pages)}" if len(self.pages) > 1 else "")
        )
        return embed


class PageButton(discord.ui.Button):
    """◀ ▶ through a long pool.

    Greyed at the ends rather than wrapping: a list that jumps from the last
    page to the first looks like it lost your place.
    """

    def __init__(
        self, session: Session, key: str, page: int, glyph: str, disabled: bool,
        row: int, origin: Optional[Section],
    ) -> None:
        super().__init__(
            label=glyph, style=discord.ButtonStyle.secondary, disabled=disabled, row=row
        )
        self.session, self.key, self.page, self.origin = session, key, page, origin

    async def callback(self, interaction: discord.Interaction) -> None:
        view = PoolScreen(self.session, self.key, self.page, origin=self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class LineActionButton(discord.ui.Button):
    """Add opens a modal; edit and remove need a line picked first."""

    def __init__(
        self, session: Session, key: str, page: int, mode: str, label: str, emoji: str,
        row: int, origin: Optional[Section],
    ) -> None:
        super().__init__(
            label=label, emoji=emoji,
            style=discord.ButtonStyle.danger if mode == "remove" else BUTTON_STYLE,
            row=row,
        )
        self.session, self.key, self.page, self.mode = session, key, page, mode
        self.origin = origin

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.mode == "add":
            await interaction.response.send_modal(
                LineModal(self.session, self.key, self.page, origin=self.origin)
            )
            return
        view = LineTargetScreen(self.session, self.key, self.page, self.mode, self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class LineTargetSelect(discord.ui.Select):
    def __init__(
        self, session: Session, key: str, page: int, mode: str, row: int,
        origin: Optional[Section],
    ) -> None:
        rows = _line_pages(key)[page]
        super().__init__(
            placeholder="Which line?",
            options=[
                discord.SelectOption(
                    label=f"{index + 1}. {_shorten(line, 92)}"[:100], value=str(index)
                )
                for index, line in rows
            ] or [discord.SelectOption(label="nothing on this page", value="-")],
            disabled=not rows,
            row=row,
        )
        self.session, self.key, self.page, self.mode = session, key, page, mode
        self.origin = origin

    async def callback(self, interaction: discord.Interaction) -> None:
        index = int(self.values[0])
        current = lines_registry.pool(self.key)
        if index >= len(current):
            await _fail(interaction, "That line is gone — someone else changed this set.")
            return

        if self.mode == "edit":
            await interaction.response.send_modal(
                LineModal(
                    self.session, self.key, self.page,
                    index=index, current=current[index], origin=self.origin,
                )
            )
            return

        removed = current.pop(index)
        try:
            lines_registry.save(self.key, current)
        except lines_registry.LineError as exc:
            await _fail(interaction, str(exc))
            return
        view = PoolScreen(self.session, self.key, self.page, origin=self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)
        await interaction.followup.send(f"Removed: *{_shorten(removed, 200)}*", ephemeral=True)


class LineTargetScreen(Screen):
    def __init__(
        self, session: Session, key: str, page: int, mode: str, origin: Optional[Section]
    ) -> None:
        super().__init__(session)
        self.key, self.page, self.mode, self.origin = key, page, mode, origin
        self.add_item(LineTargetSelect(session, key, page, mode, 0, origin))
        self.add_item(BackToPoolButton(session, key, page, 1, origin))

    def embed(self) -> discord.Embed:
        pool = lines_registry.get_pool(self.key)
        verb = "Edit" if self.mode == "edit" else "Remove"
        return discord.Embed(
            title=f"✒️ {verb} — {pool.label if pool else self.key}",
            description=(
                f"Pick the line to {verb.lower()}. Only this page's lines are listed; "
                "Back, then the arrows, for the rest."
            ),
            colour=COLOUR_DANGER if self.mode == "remove" else COLOUR,
        )


class BackToPoolButton(discord.ui.Button):
    def __init__(
        self, session: Session, key: str, page: int, row: int, origin: Optional[Section]
    ) -> None:
        super().__init__(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)
        self.session, self.key, self.page, self.origin = session, key, page, origin

    async def callback(self, interaction: discord.Interaction) -> None:
        view = PoolScreen(self.session, self.key, self.page, origin=self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class BackToListButton(discord.ui.Button):
    """Out of one pool and back to the list of them."""

    def __init__(self, session: Session, origin: Optional[Section], row: int) -> None:
        super().__init__(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)
        self.session, self.origin = session, origin

    async def callback(self, interaction: discord.Interaction) -> None:
        view = LinesScreen(self.session, self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class LineModal(discord.ui.Modal):
    """Add or edit one line. `index` None means add.

    The placeholder rules are enforced by `lines.check` rather than re-stated
    here, so the modal cannot drift from what the registry will accept — and a
    rejection comes back with the line still in your hands, not swallowed.
    """

    def __init__(
        self,
        session: Session,
        key: str,
        page: int,
        *,
        index: Optional[int] = None,
        current: str = "",
        origin: Optional[Section] = None,
    ) -> None:
        pool = lines_registry.get_pool(key)
        super().__init__(
            title=("Edit line" if index is not None else "New line")[:45],
            timeout=SESSION_TIMEOUT,
        )
        self.session, self.key, self.page, self.index = session, key, page, index
        self.origin = origin
        self.text = discord.ui.TextInput(
            label=(pool.label if pool else "Line")[:45],
            style=discord.TextStyle.paragraph,
            default=current or None,
            max_length=lines_registry.MAX_LINE,
            placeholder=(pool.hint().replace("`", "")[:100] if pool else None),
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rows = lines_registry.pool(self.key)
        text = str(self.text)
        try:
            if self.index is None:
                rows.append(text)
            else:
                if self.index >= len(rows):
                    raise lines_registry.LineError("That line is gone — the set changed underneath.")
                rows[self.index] = text
            lines_registry.save(self.key, rows)
        except lines_registry.LineError as exc:
            # The rejection has to say what was wrong with *this* line, and it
            # has to leave the screen alone so nothing looks half-saved.
            await _fail(interaction, str(exc))
            return

        view = PoolScreen(self.session, self.key, self.page, origin=self.origin)
        try:
            await interaction.response.edit_message(embed=view.embed(), view=view)
        except discord.HTTPException:
            await interaction.response.send_message(
                "Saved. Reopen the set to see it.", ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("[panel] line modal failed", exc_info=error)
        await _fail(interaction, "That didn't save.")


class ResetLinesButton(discord.ui.Button):
    def __init__(
        self, session: Session, key: str, row: int, origin: Optional[Section]
    ) -> None:
        super().__init__(label="Reset", emoji="↩️", style=discord.ButtonStyle.danger, row=row)
        self.session, self.key, self.origin = session, key, origin

    async def callback(self, interaction: discord.Interaction) -> None:
        view = ResetLinesScreen(self.session, self.key, self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class ResetLinesScreen(Screen):
    def __init__(self, session: Session, key: str, origin: Optional[Section]) -> None:
        super().__init__(session)
        self.key, self.origin = key, origin
        self.add_item(BackToPoolButton(session, key, 0, 1, origin))

    def embed(self) -> discord.Embed:
        pool = lines_registry.get_pool(self.key)
        return discord.Embed(
            title=f"↩️ Reset — {pool.label if pool else self.key}",
            description=(
                "Throws away your version and goes back to the "
                f"{len(pool.default) if pool else 0} lines written in the code. "
                "There is no copy of what you typed."
            ),
            colour=COLOUR_DANGER,
        )

    @discord.ui.button(label="Reset it", emoji="↩️", style=discord.ButtonStyle.danger, row=0)
    async def go(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        try:
            lines_registry.reset(self.key)
        except lines_registry.LineError as exc:
            await _fail(interaction, str(exc))
            return
        view = PoolScreen(self.session, self.key, origin=self.origin)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ── the two buttons that aren't commands ──────────────────────────────────────

async def _flow_lines(
    session: Session, interaction: discord.Interaction, section: Optional[Section]
) -> None:
    view = LinesScreen(session, section)
    await interaction.response.edit_message(embed=view.embed(), view=view)


async def _flow_avatar_add(
    session: Session, interaction: discord.Interaction, section: Optional[Section]
) -> None:
    await interaction.response.send_modal(AvatarAddModal())


_FLOWS: dict[str, Callable[..., Any]] = {
    "lines": _flow_lines,
    "avatar_add": _flow_avatar_add,
}


# ── everything without a button ───────────────────────────────────────────────

def _walk(bot: commands.Bot, guild: Optional[discord.Guild]) -> list[app_commands.Command]:
    """Every slash command in the tree, groups flattened, both scopes merged."""

    def expand(node: Any) -> list[app_commands.Command]:
        if isinstance(node, app_commands.Group):
            out: list[app_commands.Command] = []
            for child in node.commands:
                out.extend(expand(child))
            return out
        return [node] if isinstance(node, app_commands.Command) else []

    found: dict[str, app_commands.Command] = {}
    for scope in ([guild, None] if guild else [None]):
        for node in bot.tree.get_commands(guild=scope):
            for cmd in expand(node):
                found.setdefault(cmd.qualified_name, cmd)
    return list(found.values())


async def _catalogue_embed(
    interaction: discord.Interaction, panel: "AdminPanel"
) -> discord.Embed:
    """Everything with no button, discovered from the tree.

    This is the half of "the panel keeps up on its own" that genuinely does: add
    a command anywhere in the bot and it appears here, with no edit to this file.

    Most of Mittens' gates are inline `if` checks in the command body rather than
    decorators, so — exactly as `help.py` notes about itself — they can't be
    introspected, and this list can't be narrowed to what you personally can run.
    It is a list of what exists. The commands still refuse in their own words.
    """
    covered = panel.covered_commands()
    bot: commands.Bot = interaction.client  # type: ignore[assignment]
    listed: list[str] = []
    for cmd in _walk(bot, interaction.guild):
        if cmd.qualified_name in covered:
            continue
        refusal = await _checks_pass(cmd, interaction)
        if refusal is not None:
            continue
        listed.append(f"`/{cmd.qualified_name}` — {cmd.description or '—'}")
    listed.sort()

    embed = discord.Embed(
        title="📖 Everything without a button",
        description="Type these. `/help` is the members' version of this list.",
        colour=COLOUR,
    )
    if not listed:
        embed.add_field(name="​", value="Nothing — every command has a button.")
        return embed

    # Discord caps a field at 1024 characters, so this pours into as many fields
    # as it takes rather than truncating and quietly hiding commands.
    chunk: list[str] = []
    size = 0
    for line in listed:
        if size + len(line) + 1 > 1000 and chunk:
            embed.add_field(name="​", value="\n".join(chunk), inline=False)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        embed.add_field(name="​", value="\n".join(chunk), inline=False)
    return embed


# ── the public door ───────────────────────────────────────────────────────────

class OpenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"cozypanel:open",
):
    """The only public control.

    Dynamic rather than a plain button because the panel sitting in the channel
    across a deploy is clickable before `_boot` has replaced it, and a dynamic
    item answers that click instead of leaving it hanging.
    """

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Administration",
                emoji="😾",
                style=discord.ButtonStyle.primary,
                custom_id="cozypanel:open",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001, ARG003
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        # Logged on the way in, before anything can fail. This is the only
        # control in the channel, and when it "didn't respond in time" there is
        # otherwise no way to tell whether the click reached the bot at all.
        log.info("[panel] open pressed by %s (%s)", interaction.user, interaction.user.id)
        try:
            member = interaction.user
            if not isinstance(member, discord.Member):
                await _fail(interaction, "This only works in the server.")
                return
            if not may_open(member):
                await _fail(interaction, DENY)
                return
            panel: Optional[AdminPanel] = interaction.client.get_cog("AdminPanel")  # type: ignore[assignment]
            if panel is None:
                await _fail(interaction, "The panel isn't loaded right now.")
                return
            view = HomeScreen(Session(panel=panel, member=member))
            await interaction.response.send_message(
                embed=view.embed(), view=view, ephemeral=True
            )
        except Exception:
            log.exception("[panel] Open failed")
            await _fail(interaction, "Opening the panel went wrong. The log has the details.")


class PanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(OpenButton())

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        # discord.py's default logs and leaves the click unanswered, which
        # surfaces as "didn't respond in time" and tells nobody anything.
        log.exception("[panel] %s on the public panel failed", type(item).__name__, exc_info=error)
        await _fail(interaction, "That didn't go through. The log has the details.")


# ── panel bookkeeping ─────────────────────────────────────────────────────────

def _remember_panel(guild_id: int, channel_id: int, message_id: int) -> None:
    data = storage.load_json(PANEL_PATH, default={})
    if not isinstance(data, dict):
        data = {}
    data[str(guild_id)] = {"channel_id": channel_id, "message_id": message_id}
    storage.save_json(PANEL_PATH, data)


def _recall_panel(guild_id: int) -> Optional[tuple[int, int]]:
    data = storage.load_json(PANEL_PATH, default={})
    rec = data.get(str(guild_id)) if isinstance(data, dict) else None
    if not isinstance(rec, dict):
        return None
    try:
        return int(rec["channel_id"]), int(rec["message_id"])
    except (KeyError, TypeError, ValueError):
        return None


# ── what each button is ───────────────────────────────────────────────────────

_CHANNEL = Pick("channel", "channel")
_MEMBER = Pick("member", "member")
_USER = Pick("member", "user")
_TIMEOUT_LENGTH = Pick("minutes", "duration", cast=_duration_arg)
_AVATAR = Pick("avatar", "pick")
_ADDED_AVATAR = Pick("added_avatar", "name")


def _sections() -> tuple[Section, ...]:
    """The registry. Adding a button is adding a line here.

    Each `gate` mirrors what its command already checks rather than being chosen
    afresh, so the panel cannot quietly widen access — and where a command has a
    gate of its own beyond that (a channel, a role by name) it still enforces it,
    and says no in its own words.
    """
    return (
        Section("Discord Moderation", "🧹", (
            Action("Purge", "🧽", "mod", "purge",
                   blurb="Delete the last N messages in a channel. Up to 200, and nothing over 14 days old.",
                   style=discord.ButtonStyle.danger,
                   picks=(_CHANNEL,), modal="purge"),
            Action("Timeout", "⏳", "mod", "timeout",
                   blurb="Mute a member everywhere for as long as you say. Wears off on its own.",
                   style=discord.ButtonStyle.danger,
                   picks=(_USER, _TIMEOUT_LENGTH)),
            Action("Untimeout", "🕊️", "mod", "untimeout",
                   blurb="Let a member out early.",
                   picks=(_USER,)),
        )),
        Section("Bot Panels", "🚪", (
            Action("Repost Landing Zone and Get Roles Panels", "🚪", "admin", "setup_panels",
                   blurb="Both doors at once: a fresh ✅ gate in the landing zone and a fresh "
                         "set of role menus in get-roles. The old ones are cleared.",
                   confirm="Posts a new gate and a new set of role menus, each in its own "
                           "channel, and deletes the ones they replace. Anyone mid-click on "
                           "an old menu will need the new one."),
            Action("Repost pet panel", "🍖", "admin", "petpanel",
                   blurb="Put the food bowl back at the bottom of the pet channel — after a purge, mostly."),
        )),
        Section("Morning News", "📰",
            (
                Action("Generate a paper (testing purpose)", "🧪", "staff", "test_morning_news",
                       blurb="Build a paper from the test pool and drop it in this channel."),
                Action("Post today's Paper", "📣", "staff", "repost_morning_news",
                       blurb="Build today's paper and post it in the live news channel for everyone.",
                       style=discord.ButtonStyle.danger,
                       confirm="Posts to the live morning-news channel where everyone reads it, "
                               "and marks today as done so the 8am post doesn't repeat it.\n\n"
                               "**Every paper costs real money to write.** The 8am one happens "
                               "on its own — press this only when that one didn't."),
            ),
            blurb="⚠️ Every paper is written by an AI and **costs a few cents each time**. "
                  "The live one posts itself at 8am — these are for when something went "
                  "wrong, not for fun.",
        ),
        Section("Birthdays", "🎂", (
            Action("Birthday List", "📋", "admin", "birthday check",
                   blurb="Every birthday saved, in date order. Posted here for everyone in the channel."),
            Action("Today's", "🎈", "admin", "birthday today",
                   blurb="Who is having one today, if anyone."),
            Action("Add Birthday", "➕", "admin", "birthday add",
                   blurb="Write one down for somebody — a correction, or for whoever never set theirs.",
                   picks=(_MEMBER,), modal="birthday"),
            Action("Remove Birthday", "✂️", "admin", "birthday remove",
                   blurb="Cross a member out of the birthday list.",
                   style=discord.ButtonStyle.danger, picks=(_MEMBER,),
                   confirm="Deletes that member's birthday. They'd have to set it again themselves."),
        )),
        Section("Mittens the Menace", "😼", (
            Action("Rotate Mitten's Discord status", "🔄", "staff", "status_now",
                   blurb="Give him a new status line right now instead of waiting for the rotation."),
            Action("Generate Discord Status Ideas", "💡", "staff", "status_ideas",
                   blurb="Read the last month of chat and suggest new status lines — to you only, "
                         "in a block you can copy straight into the lines editor.",
                   kwargs={"private": True}),
            Action("Mittens custom lines", "✒️", "admin", opens="lines",
                   blurb="Everything he says at random — statuses, resets, timeouts, pet chatter. "
                         "Read them, change them, add your own, put the originals back."),
        ), subs=(
            Section("Avatar", "🖼️", (
                Action("Avatar schedule", "📅", "staff", "avatar_schedule",
                       blurb="Which picture is up, why, and what's coming next."),
                Action("Add an avatar", "📷", "admin", opens="avatar_add",
                       blurb="Name it, give it a start and end date, then post the picture here. "
                             "He'll refuse dates that clash with something already booked."),
                Action("Remove an avatar", "🗑️", "admin", "avatar_remove",
                       blurb="Take a hand-added picture back out. The built-in occasions stay.",
                       style=discord.ButtonStyle.danger, picks=(_ADDED_AVATAR,)),
                Action("Sync the avatar", "🔃", "admin", "avatar_sync",
                       blurb="Check today's date and put the right picture up now.",
                       kwargs={"force": False}),
                Action("Manually change avatar", "📌", "admin", "avatar_set",
                       blurb="Hold his picture on one of them, or hand it back to the calendar with 'auto'.",
                       picks=(_AVATAR,)),
            ), blurb="His face: what's up now, what's next, and adding your own."),
        )),
        Section("Pets", "🐾", (
            Action("Top up my treats", "🍬", "staff", "pettreats",
                   blurb="Refill your own allowance without waiting for midnight. Yours only — "
                         "there is no version of this that hands somebody else treats."),
        )),
    )


def _home_actions() -> tuple[Action, ...]:
    """Buttons on the home screen itself, for a job with no drawer around it."""
    return (
        Action("Create an Event", "🎉", "staff", "event",
               blurb="The event form: scheduled event, forum thread, and the announcement."),
    )


SECTIONS: tuple[Section, ...] = _sections()
HOME_ACTIONS: tuple[Action, ...] = _home_actions()


def _walk_sections(sections: tuple[Section, ...]) -> list[Section]:
    """Every drawer, sub-drawers included — the budget applies to all of them."""
    found: list[Section] = []
    for section in sections:
        found.append(section)
        found.extend(_walk_sections(section.subs))
    return found


def _all_actions() -> list[Action]:
    found = list(HOME_ACTIONS)
    for section in _walk_sections(SECTIONS):
        found.extend(section.actions)
    return found


# Layout is data, so the layout rules are checked at import — a drawer that
# can't fit its own Back button is a mistake to find on deploy, not when a mod
# opens it three days later.
for _s in _walk_sections(SECTIONS):
    _items = len(_s.subs) + len(_s.actions)
    assert _items // 5 <= 4, f"{_s.label}: Back has no row"
    assert (_items + 4) // 5 <= 5, f"{_s.label}: too many buttons for five rows"
    # The legend goes in the embed description, which Discord caps at 4096.
    _legend = sum(len(_a.label) + len(_a.blurb) + 8 for _a in _s.actions) + len(_s.blurb) + 80
    assert _legend <= 4096, f"{_s.label}: legend is {_legend} chars, Discord allows 4096"
for _a in _all_actions():
    assert _a.gate in _GATES, f"{_a.label}: no such gate {_a.gate!r}"
    assert _a.command or _a.opens or _a.parked, f"{_a.label}: does nothing"
    assert not (_a.command and _a.opens), f"{_a.label}: is both a command and a flow"
    assert _a.opens in _FLOWS or not _a.opens, f"{_a.label}: no flow {_a.opens!r}"
    assert _a.modal is None or _a.modal in _MODALS, f"{_a.label}: no modal {_a.modal!r}"
    # A pick screen is one select per pick, then Continue and Back below.
    assert len(_a.picks) + 1 <= 5, f"{_a.label}: {len(_a.picks)} picks needs too many rows"
    for _p in _a.picks:
        assert _p.kind in _KIND_WORD, f"{_a.label}: no dropdown for {_p.kind!r}"
assert len(SECTIONS) + len(HOME_ACTIONS) <= 24, "home holds 24 buttons plus Slash Commands"


# ── the cog ───────────────────────────────────────────────────────────────────

class AdminPanel(commands.Cog):
    """Keeps one panel at the bottom of the staff channel and runs what it offers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sections = SECTIONS
        self.home_actions = HOME_ACTIONS
        self._panel: Optional[discord.Message] = None
        # Set when the panel could not delete its predecessors, or when a button
        # no longer matches the command behind it, so the symptom is explained on
        # the panel itself rather than only in a log nobody is tailing.
        self._warning: Optional[str] = None
        self._broken: list[str] = []
        self._repost: Optional[asyncio.Task] = None
        self._last_place = 0.0
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        # asyncio.create_task rather than bot.loop.create_task: `bot.loop` is
        # only set once the client has logged in, and a cog can be loaded before
        # that. This is always called from inside the loop, so it is safe.
        asyncio.create_task(self._boot())

    def cog_unload(self) -> None:
        if self._repost and not self._repost.done():
            self._repost.cancel()

    # ── who sees what ─────────────────────────────────────────────────────────

    def visible_actions(self, section: Section, member: discord.Member) -> list[Action]:
        return [a for a in section.actions if _passes(a.gate, member)]

    def visible_subs(self, section: Section, member: discord.Member) -> list[Section]:
        return [s for s in section.subs if self.section_visible(s, member)]

    def visible_home_actions(self, member: discord.Member) -> list[Action]:
        return [a for a in self.home_actions if _passes(a.gate, member)]

    def section_visible(self, section: Section, member: discord.Member) -> bool:
        """Anything in here you can press? An empty drawer is worse than a
        missing one — it reads as a bug rather than as a permission."""
        return bool(self.visible_actions(section, member)) or any(
            self.section_visible(sub, member) for sub in section.subs
        )

    def covered_commands(self) -> frozenset[str]:
        return frozenset(a.command for a in _all_actions() if a.command)

    def _channel(self) -> Optional[discord.TextChannel]:
        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        return channel if isinstance(channel, discord.TextChannel) else None

    # ── does every button still fit its command? ──────────────────────────────

    def _audit(self) -> list[str]:
        """Check each button against the command it stands for.

        The panel supplies arguments by name, so a renamed or removed parameter
        turns a button into something that only fails when somebody presses it,
        with a traceback in the log and a shrug on the screen. This runs at boot
        and says so on the panel instead. It reads the live tree, so it is
        checking what the bot is actually running rather than what this file
        assumes.
        """
        problems: list[str] = []
        for action in _all_actions():
            if not action.command:
                continue
            cmd = _find_command(self.bot, action.command, None)
            if cmd is None:
                problems.append(f"{action.label}: /{action.command} isn't loaded")
                continue
            try:
                params = set(inspect.signature(cmd.callback).parameters)
            except (TypeError, ValueError):
                continue
            supplied = set(action.kwargs) | {p.param for p in action.picks}
            unknown = supplied - params
            if unknown:
                problems.append(
                    f"{action.label}: /{action.command} has no "
                    + ", ".join(sorted(unknown))
                )
        return problems

    # ── drawing ───────────────────────────────────────────────────────────────

    def _build(self, guild: discord.Guild) -> tuple[discord.Embed, PanelView]:
        """A title, a line, and the door. Nothing else.

        Everything behind the door describes itself, one screen at a time, so
        summarising the drawers here would only put a wall in front of the
        button somebody came to press.
        """
        notes = [n for n in (self._warning, self._broken_note()) if n]
        description = "Every staff command, without remembering what it's called."
        if notes:
            description = "\n".join(notes) + "\n\n" + description
        embed = discord.Embed(
            title=PANEL_TITLE,
            description=description,
            colour=COLOUR_DANGER if notes else COLOUR,
        )
        embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
        embed.set_footer(text="Staff only. He checks. 🐾")
        return embed, PanelView()

    def _broken_note(self) -> Optional[str]:
        if not self._broken:
            return None
        listed = "; ".join(self._broken[:3])
        more = f" (+{len(self._broken) - 3} more)" if len(self._broken) > 3 else ""
        return f"⚠️ {len(self._broken)} button(s) no longer match their command — {listed}{more}"

    # ── placing it ────────────────────────────────────────────────────────────

    def _is_our_panel(self, message: discord.Message) -> bool:
        """One of ours, by title — never by id.

        By id is not enough, and that distinction is the whole self-trigger
        guard: the gateway can deliver MESSAGE_CREATE for a panel before
        `channel.send` has returned and `self._panel` has been reassigned, so a
        panel would see its own arrival as somebody else's message and schedule
        another move. That is a new panel every few seconds, forever. Matching
        on the title catches it whichever copy of the bot posted it.
        """
        if self.bot.user is None or message.author.id != self.bot.user.id:
            return False
        return bool(message.embeds) and (message.embeds[0].title or "") in PANEL_TITLES

    async def _clear_strays(self, channel: discord.TextChannel, keep_id: int) -> int:
        """Delete any other panel of ours still sitting in the channel.

        Belt and braces for the duplicate-panel bug: whatever leaves a stray
        behind — a crash between send and delete, two copies of the bot during
        an overlapping deploy — the next placement tidies it away instead of
        stacking. Returns how many it could not remove, which is what the
        warning line on the panel is made of.
        """
        removed = 0
        blocked = 0
        try:
            async for message in channel.history(limit=STRAY_SCAN):
                if removed >= STRAY_DELETE_CAP:
                    break
                if message.id == keep_id or not self._is_our_panel(message):
                    continue
                try:
                    await message.delete()
                    removed += 1
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    blocked += 1
        except Exception:
            log.debug("[panel] stray sweep failed", exc_info=True)
        if removed:
            log.info("[panel] cleared %d stray panel(s)", removed)
        return blocked

    async def _place(
        self, channel: discord.TextChannel, *, force: bool = False, sweep: bool = False
    ) -> None:
        """Post a fresh panel at the bottom and clear the old one above it.

        `force` skips the rate floor — for `/adminpanel`, where a human has asked
        for it directly and waiting the floor out would just look broken.

        `sweep` also rakes the channel for older panels. That is a hundred-
        message history fetch, so it only runs where an abnormal state is
        expected: at boot, and when somebody asks for a panel by hand.
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
                        log.debug("[panel] could not fetch the stored panel", exc_info=True)
                        old = None

            try:
                embed, view = self._build(channel.guild)
                fresh = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                log.warning("[panel] cannot post in #%s", channel.name)
                return
            except Exception:
                log.exception("[panel] could not post the panel")
                return

            self._panel = fresh
            log.info("[panel] panel %s placed in #%s", fresh.id, channel.name)
            await asyncio.to_thread(_remember_panel, channel.guild.id, channel.id, fresh.id)

            # Last, so a failure above never leaves the channel with no panel.
            blocked = 0
            if old is not None and old.id != fresh.id:
                try:
                    await old.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    blocked += 1
                except Exception:
                    log.debug("[panel] could not delete the old panel", exc_info=True)

            if sweep:
                blocked += await self._clear_strays(channel, fresh.id)

            was = self._warning
            self._warning = (
                f"⚠️ I can't delete my old panels here — I need **Manage Messages** "
                f"in this channel. {blocked} stuck right now."
            ) if blocked else None
            if self._warning != was:
                try:
                    embed, view = self._build(channel.guild)
                    await fresh.edit(embed=embed, view=view)
                except Exception:
                    log.debug("[panel] could not annotate the panel", exc_info=True)

    async def repost(self, channel: discord.TextChannel) -> None:
        """Force a fresh panel."""
        self._panel = None           # whatever it pointed at may have been purged
        if self._repost and not self._repost.done():
            self._repost.cancel()    # don't let a queued move double up behind us
        await self._place(channel, force=True, sweep=True)

    async def _needs_moving(self, channel: discord.TextChannel) -> bool:
        if self._panel is None:
            return True
        try:
            async for message in channel.history(limit=1):
                return message.id != self._panel.id
        except Exception:
            log.debug("[panel] could not read the channel tail", exc_info=True)
            return False
        return True

    async def _repost_soon(self) -> None:
        """Debounced: a burst of conversation costs exactly one repost.

        The rate floor is waited out here rather than enforced by dropping the
        move. Skipping it outright means anything happening within
        MIN_PLACE_INTERVAL of a placement loses its move entirely and leaves the
        panel stranded above the newest post until somebody else speaks.
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
            await self._place(channel)

    def _schedule_repost(self) -> None:
        if self._repost and not self._repost.done():
            self._repost.cancel()
        self._repost = asyncio.create_task(self._repost_soon())

    async def _boot(self) -> None:
        await self.bot.wait_until_ready()
        self._broken = self._audit()
        for problem in self._broken:
            log.warning("[panel] %s", problem)
        channel = self._channel()
        if channel is None:
            log.warning("[panel] staff channel %s is not reachable", PANEL_CHANNEL_ID)
            return
        await self._place(channel, force=True, sweep=True)

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        """Move the panel down for anything that lands above it — except a panel.

        Half of what arrives in this channel is the bot's own: the test paper,
        the status ideas. Those have to push the panel down like anything else,
        so this ignores our *panels* rather than our messages — see
        `_is_our_panel` for why ignoring only the panel is both necessary and
        sufficient.
        """
        if message.guild is None or message.channel.id != PANEL_CHANNEL_ID:
            return
        if self._is_our_panel(message):
            return
        self._schedule_repost()

    # ── the one command, for putting the panel back ───────────────────────────

    @app_commands.command(
        name="adminpanel", description="Post the staff panel again 😾"
    )
    async def adminpanel_cmd(self, interaction: discord.Interaction) -> None:
        """Manual repost. Needed after purging the channel, since the message the
        cog is holding no longer exists and nothing replaces it until somebody
        talks."""
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        if not _is_admin(interaction.user):
            return await interaction.response.send_message(DENY, ephemeral=True)

        channel = self._channel()
        if channel is None:
            return await interaction.response.send_message(
                "❌ I can't see the staff channel.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        self._broken = self._audit()
        await self.repost(channel)
        await interaction.followup.send(f"Panel reposted in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(OpenButton)
    await bot.add_cog(AdminPanel(bot))
