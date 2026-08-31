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
an optional argument and both callers use it — see `channel` on `/purge` in
`moderation.py`.

**Calling `.callback` skips everything the tree would have run first** — the
command's own `@app_commands.check` decorators *and* a cog-wide
`interaction_check` (which is why `moderation.py`'s role gate has to be
re-stated here). So the panel does two things about it. It re-runs the
command's decorator checks itself before calling (`_checks_pass`), so a button
can never be a wider door than typing the command. And each `Action` names a
gate for *visibility*, off the same predicates the cogs use, so a drawer shows
you what you can use and nothing else — anything above your gate is not greyed
out or annotated, it is simply not built. Gates written inline in a command's
body (most of Mittens' are) still enforce themselves, in the command's own
words, which is why some buttons can still say no.

**One public message, and everything behind it is private.** Twenty-five
components is Discord's cap per message, and there are more commands than that,
so the panel is a door rather than a wall: one Open button, and what it gives
you is an ephemeral screen only you can see and drive. That also buys back the
dropdowns — a channel or member select on a *shared* message would show every
mod whatever the last person picked, but on your own ephemeral copy it is yours.

**A screen asks one question.** A drawer is buttons; pressing one opens *its*
picks and nothing else — Timeout asks who and for how long, Purge asks which
channel and then how many. When the last required answer lands and there is
nothing optional left to weigh, the dropdown carries straight on into the
command (`PickScreen.ready`), so the common case is press, choose, done. Picks
are cleared on the way in, because a value remembered from the last button is a
value you can no longer see.

**The explaining happens one screen down.** The public message is a title, a
line and the door. The home screen is drawer buttons and nothing else. A
drawer's embed lists that drawer's buttons, one line each (`Action.blurb`),
because by then you have narrowed thirty commands to five, and the labels alone
won't separate "Test paper" from "Post the paper". Five lines you asked for is
a legend; thirty you didn't is a wall.

Channel-locked commands: `/event`, the admin `/birthday` commands and the
`avatar_*` commands each insist on the admin-commands channel, and a button
cannot move the interaction somewhere else — the interaction happens where the
panel is. Rather than park those buttons, each of those three cogs now accepts
this channel as well as its own, through one named constant. Widening a staff
command from one staff channel to two is not a permission change.

The interaction token behind an ephemeral message dies after 15 minutes, so a
screen left open that long stops answering and Open starts a fresh one — the
right failure, since a stale staff screen is worth less than nothing.

**A button that isn't wired up yet stays.** `Action.parked` builds the button
and answers the press with a line saying why nothing happened — for a command
that exists but whose other end isn't there yet. Greying it out reads as "not
for you", and deleting it loses the note.

**What keeps itself up to date and what doesn't.** The panel re-posts itself on
boot and keeps itself at the bottom of the channel (the `pet_care` pattern,
including the self-trigger guard that cog shipped a bug over once). 📖 Slash
Commands is read off the live tree minus whatever already has a button, so a
newly added command appears there with no edit here. Buttons do *not* auto-
appear: supplying a command's arguments is a decision rather than something
derivable. Add an `Action` to `_sections()` and it appears — with a `blurb`, or
it borrows the command's own description until you write one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from common import storage

log = logging.getLogger("cozy.admin_panel")

# Where the panel lives: the staff channel that already collects the output of
# half its own buttons — the morning-news test post, the reset test post and the
# status ideas all land here.
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


# ── the gates, as data ────────────────────────────────────────────────────────
#
# Not a ladder. Mittens' cogs don't share one — `moderation.py` wants two role
# *names*, `pet_care.py` wants Manage Server, `mittens_say.py` wants
# Administrator, the news wants two role *ids* — so these are named gates that
# mirror what each command already checks, and an `Action` names the one its own
# command uses. Nothing here invents a new definition of "allowed".

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

    Imported inside the call so a broken `moderation.py` costs its four buttons
    rather than the whole panel, and returns False in that case rather than
    falling back to something broader: a widening on the way out of an error is
    exactly the kind that never gets noticed.
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
    and the owner. Someone passing none of them would be handed an empty panel,
    which reads as a bug rather than as a refusal — so they are turned away here.
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
    the command it stands for — `/mittensay` is Administrator-only by decorator,
    and Manage Server is not Administrator.

    A check that raises is a refusal (that is how `pet_care`'s `admin_only` and
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


# ── the registry ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pick:
    """Something a command needs that a dropdown supplies.

    `kind` is which dropdown ("channel", "member", "minutes", "avatar") and
    `param` is what that value is called in the command's signature — they
    differ often enough to be worth separating: the same member dropdown feeds
    `user=` to `/timeout` and `member=` to `/birthday remove`.

    `cast` bridges the rest of the gap. A dropdown can only hand back a string
    or a Discord object, and commands want other things — `/timeout` wants
    "10m", `/avatar_set` wants an `app_commands.Choice`. Converting here keeps
    that knowledge next to the pick rather than inside every modal.
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

    `blurb` is the line the drawer prints next to the button. It is written here
    rather than read off the command's own description because a button is not
    always the whole command — "Test daily" and "Test weekly" are one command
    with its argument baked in — and because a description written for the
    slash-command picker is written for whoever is typing it. Left out, the
    drawer falls back to the live command description, so a new action still
    says something.
    """
    label: str
    emoji: str
    gate: str
    command: str
    blurb: str = ""
    style: discord.ButtonStyle = BUTTON_STYLE
    confirm: Optional[str] = None
    picks: tuple[Pick, ...] = ()
    modal: Optional[str] = None      # key into _MODALS
    kwargs: dict[str, Any] = field(default_factory=dict)
    parked: str = ""                 # built and visible, but says this instead of running


@dataclass(frozen=True)
class Section:
    """A drawer: a heading and the buttons in it.

    No dropdowns, and no drawers inside drawers. Mittens has thirty commands,
    not four hundred: one level is enough to find anything, and a second one
    would mean navigating rather than working.
    """
    label: str
    emoji: str
    actions: tuple[Action, ...]
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


def _avatar_choice(key: str) -> app_commands.Choice[str]:
    """`/avatar_set` takes a Choice, so the dropdown's string becomes one."""
    return app_commands.Choice(name=key, value=key)


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


class SayModal(_Modal):
    """What Mittens should say. The channel came from the dropdown behind this."""

    text = discord.ui.TextInput(
        label="What should Mittens say?",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        body = str(self.text.value).strip()
        if not body:
            await _fail(interaction, "Type something for him to say. 😾")
            return
        await _call(interaction, self.action.command, text=body, **self.gathered)


class ShameModal(_Modal):
    """A message link, and optionally what to say about it."""

    link = discord.ui.TextInput(
        label="Message link",
        placeholder="https://discord.com/channels/…",
        required=True,
    )
    taunt = discord.ui.TextInput(
        label="Footer (optional — he'll pick one otherwise)",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        taunt = str(self.taunt.value).strip() or None
        await _call(
            interaction,
            self.action.command,
            link=str(self.link.value).strip(),
            taunt=taunt,
            **self.gathered,
        )


_MODALS: dict[str, type[_Modal]] = {
    "purge": PurgeModal,
    "say": SayModal,
    "shame": ShameModal,
}


def _modal_title(action: Action, gathered: dict[str, Any]) -> str:
    """Name the target in the title, so the modal is its own confirmation."""
    for value in gathered.values():
        if isinstance(value, (discord.TextChannel, discord.Thread)):
            return f"{action.label} in #{value.name}"
        if isinstance(value, discord.Member):
            return f"{action.label}: {value.display_name}"
    return action.label


# ── the dropdowns ─────────────────────────────────────────────────────────────

async def _picked(item: discord.ui.Item, interaction: discord.Interaction) -> None:
    """Redraw the pick screen, or move on if that was the last answer needed.

    Moving on by itself is the whole point of asking one button's questions on
    one screen: pick the member for a timeout, pick the length, and it fires. An
    action with an *optional* pick can't do that — nothing tells us you are done
    leaving it blank — so those keep a Continue button and this only redraws.
    """
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
    """Mittens' faces, read off `seasonal_avatar` rather than copied.

    Imported inside the function so a broken `seasonal_avatar` costs this one
    dropdown instead of the whole panel.
    """
    try:
        from cogs.seasonal_avatar import DEFAULT_KEY, OCCASIONS  # noqa: PLC0415
    except Exception:
        log.debug("[panel] could not read the avatar list", exc_info=True)
        return []
    options = [
        discord.SelectOption(
            label="auto", value="auto", description="Back to following the calendar"
        ),
        discord.SelectOption(label=DEFAULT_KEY, value=DEFAULT_KEY),
    ]
    options += [discord.SelectOption(label=key, value=key) for key, _ in OCCASIONS]
    return options[:25]


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
    """A fixed list of strings — currently only the avatar keys."""

    def __init__(self, session: Session, kind: str, row: int) -> None:
        options = _avatar_options() if kind == "avatar" else []
        super().__init__(
            placeholder="Picture" if kind == "avatar" else kind.title(),
            options=options or [discord.SelectOption(label="nothing to pick", value="-")],
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
    """How long. Values come back as strings; the int is what the `cast` on the
    Pick turns into whatever the command wants."""

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
    came from home. Abandoning a half-made choice should not also lose the five
    buttons you were choosing between.
    """

    def __init__(self, session: Session, row: int, section: Optional[Section] = None) -> None:
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
    """A button that stands for a command.

    Pressing one starts that command's own flow — its picks, then its
    are-you-sure, then its modal — and each step spends exactly one interaction,
    because a callback must be handed an unanswered one. Where there is nothing
    to ask, this click *is* the command's click and it replies in its own voice.

    The picks are cleared on the way in. A dropdown only appears after a press,
    so a remembered value would be an invisible one — press Purge, pick
    #general, go back, press Say and watch Mittens talk somewhere you can no
    longer see you chose.
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
        if self.action.picks:
            view = PickScreen(self.session, self.action, self.section)
            await interaction.response.edit_message(embed=view.embed(), view=view)
            return
        await _advance(self.session, interaction, self.action, self.section)


class PickScreen(Screen):
    """One button's questions, on their own screen.

    Everything the action needs and nothing it doesn't. `ready()` is the
    auto-advance rule: when every required pick is answered and there is nothing
    optional left to decide, the last dropdown carries straight on into the
    command. An optional pick means only you know when you're done, so those get
    a Continue button instead.
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
        lines = [self.action.blurb] if self.action.blurb else []
        lines.append(
            f"Choose {need}, then Continue — {spare} is optional."
            if spare else f"Choose {need}."
        )
        return discord.Embed(
            title=f"{self.action.emoji} {self.action.label}",
            description="\n\n".join(lines),
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
    """A drawer: its buttons, then Back. Nothing else."""

    def __init__(self, session: Session, section: Section) -> None:
        super().__init__(session)
        self.section = section

        allowed = session.panel.visible_actions(section, session.member)
        for index, action in enumerate(allowed):
            self.add_item(ActionButton(session, action, index // 5, section))
        self.add_item(BackButton(session, len(allowed) // 5))

    def embed(self) -> discord.Embed:
        """Title, then a line per button.

        This is the *only* screen that explains anything, and that is the point:
        by the time you are here you have already said which five things you are
        choosing between. Only what you can press is described — the list is
        built off `visible_actions`, the same call that builds the buttons, so a
        drawer never explains something that isn't there.
        """
        lines = [
            f"{action.emoji} **{action.label}** — {self._describe(action)}"
            for action in self.session.panel.visible_actions(self.section, self.session.member)
        ]
        return discord.Embed(
            title=f"{self.section.emoji} {self.section.label}",
            description="\n".join(lines) or None,
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
    def __init__(self, session: Session, section: Section, row: int) -> None:
        super().__init__(
            label=section.label, emoji=section.emoji, style=BUTTON_STYLE, row=row
        )
        self.session = session
        self.section = section

    async def callback(self, interaction: discord.Interaction) -> None:
        # The session survives navigation — a fresh one per screen would lose the
        # channel you picked on the way in.
        view = SectionScreen(self.session, self.section)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class HomeScreen(Screen):
    """The drawers. Nothing but buttons — their labels say what they are."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._sections = [
            s for s in session.panel.sections
            if session.panel.section_visible(s, session.member)
        ]
        for index, section in enumerate(self._sections):
            self.add_item(SectionButton(session, section, index // 5))
        # It sits where the drawers end and the reference material begins, which
        # is what it is — the last thing you *do* is above it.
        self.add_item(CatalogueButton(session, len(self._sections) // 5))

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=PANEL_TITLE,
            description="Pick a drawer. He is watching, and he is judging.",
            colour=COLOUR,
        )


# ── everything without a button ───────────────────────────────────────────────

def _walk(bot: commands.Bot, guild: Optional[discord.Guild]) -> list[app_commands.Command]:
    """Every slash command in the tree, groups flattened, both scopes merged.

    Deduped by qualified name, because a command copied into the guild scope by
    `bot.py` is in both.
    """

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
    introspected, and this list can't be filtered down to what you personally can
    run. It is a list of what exists. The commands still refuse in their own
    words when they aren't for you.
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
                label="Open the panel",
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
        # otherwise no way to tell whether the click reached the bot at all — an
        # exception here is silent, and a click that never arrives looks
        # identical from Discord's side. One line settles which.
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
_AVATAR = Pick("avatar", "pick", cast=_avatar_choice)

_DAILY = app_commands.Choice(name="daily", value="daily")
_WEEKLY = app_commands.Choice(name="weekly", value="weekly")


def _sections() -> tuple[Section, ...]:
    """The registry. Adding a button is adding a line here.

    Each `gate` mirrors what its command already checks rather than being chosen
    afresh, so the panel cannot quietly widen access — and where a command has a
    gate of its own beyond that (a channel, a role by name, the Fresh Meat rule)
    it still enforces it, and says no in its own words.
    """
    return (
        Section("Moderation", "🧹", (
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
            Action("Wall of Shame", "📸", "mod", "shame",
                   blurb="Send a message link to the evidence locker. He'll write the caption if you don't.",
                   modal="shame"),
        )),
        Section("Pets", "🐾", (
            Action("Repost pet panel", "🍖", "admin", "petpanel",
                   blurb="Put the food bowl back at the bottom of the pet channel — after a purge, mostly."),
            Action("Preview the nudge", "👀", "admin", "petnudge",
                   blurb="Tonight's hungry-pet post, shown to you alone. Roll it as many times as you like.",
                   kwargs={"post": False}),
            Action("Post the nudge", "📣", "admin", "petnudge",
                   blurb="Send tonight's hungry-pet post to the pet channel for real.",
                   style=discord.ButtonStyle.danger,
                   kwargs={"post": True},
                   confirm="Posts tonight's hungry-pet complaint in the pet channel, "
                           "where everyone reads it. The 9pm post still happens on its own."),
            Action("Top up my treats", "🍬", "owner", "pettreats",
                   blurb="Refill your own allowance without waiting for midnight. Yours only."),
        )),
        Section("Morning News", "📰", (
            Action("Test paper", "🧪", "staff", "test_morning_news",
                   blurb="Build a paper from the test pool and drop it in this channel."),
            Action("Post the paper", "📣", "staff", "repost_morning_news",
                   blurb="Build today's paper and post it in the live news channel for everyone.",
                   style=discord.ButtonStyle.danger,
                   confirm="Posts to the live morning-news channel where everyone reads it, "
                           "and marks today as done so the 8am post doesn't repeat it."),
        )),
        Section("Birthdays", "🎂", (
            Action("Everyone's", "📋", "admin", "birthday check",
                   blurb="Every birthday saved, in date order. Posted here for everyone in the channel."),
            Action("Today's", "🎉", "admin", "birthday today",
                   blurb="Who is having one today, if anyone."),
            Action("Remove one", "✂️", "admin", "birthday remove",
                   blurb="Cross a member out of the birthday list.",
                   style=discord.ButtonStyle.danger, picks=(_MEMBER,),
                   confirm="Deletes that member's birthday. They'd have to set it again themselves."),
        )),
        Section("Resets", "⏰", (
            Action("Next reset", "🗓️", "staff", "resets next",
                   blurb="When the next daily and weekly resets land, in UTC and local time."),
            Action("Countdown", "⌛", "staff", "resets countdown",
                   blurb="Just the next one, and how long you've got."),
            Action("Test daily", "☀️", "staff", "resets test",
                   blurb="Post a random daily-reset message in this channel to see how it reads.",
                   kwargs={"kind": _DAILY}),
            Action("Test weekly", "🌙", "staff", "resets test",
                   blurb="The same, for the weekly one.",
                   kwargs={"kind": _WEEKLY}),
            Action("Set the channel", "📍", "admin", "resets set_channel",
                   blurb="Move the real reset announcements somewhere else.",
                   picks=(_CHANNEL,)),
        )),
        Section("Mittens himself", "😼", (
            Action("Say something", "💬", "admin", "mittensay",
                   blurb="Put words in his mouth, in any channel. He will not thank you.",
                   picks=(_CHANNEL,), modal="say"),
            Action("Rotate status", "🔄", "staff", "status_now",
                   blurb="Give him a new status line right now instead of waiting for the rotation."),
            Action("Status ideas", "💡", "staff", "status_ideas",
                   blurb="Read the last month of chat and suggest new status lines, posted here."),
            Action("Pin the avatar", "📌", "admin", "avatar_set",
                   blurb="Hold his picture on one occasion, or hand it back to the calendar with 'auto'.",
                   picks=(_AVATAR,)),
            Action("Sync the avatar", "🖼️", "admin", "avatar_sync",
                   blurb="Check today's date and put the right picture up now.",
                   kwargs={"force": False}),
            Action("Avatar schedule", "📅", "staff", "avatar_schedule",
                   blurb="Which picture is up, why, and what's coming next."),
        )),
        Section("Doors & menus", "🚪", (
            Action("Role menus", "🎭", "admin", "setup_roles",
                   blurb="Post the pronoun, server, ping-role and interest menus in get-roles, fresh.",
                   confirm="Posts a new set of role menus in get-roles. The old ones stay "
                           "where they are and keep working — delete them by hand."),
            Action("Landing gate", "🚪", "admin", "setup_gate",
                   blurb="Rebuild the ✅ gate message new arrivals react to.",
                   confirm="Posts a new gate message in the landing zone. Anyone still "
                           "looking at the old one will need the new one instead."),
        )),
        Section("Events", "🎉", (
            Action("New event", "➕", "staff", "event",
                   blurb="The event form: scheduled event, forum thread, and the announcement. "
                         "Attach a cover image by typing /event instead."),
        )),
    )


SECTIONS: tuple[Section, ...] = _sections()

# Layout is data, so the layout rules are checked at import — a drawer that
# can't fit its own Back button is a mistake to find on deploy, not when a mod
# opens it three days later.
for _s in SECTIONS:
    # A drawer is its buttons and a Back, five to a row, five rows.
    assert len(_s.actions) // 5 <= 4, f"{_s.label}: Back has no row"
    assert (len(_s.actions) + 4) // 5 <= 5, f"{_s.label}: too many buttons for five rows"
    for _a in _s.actions:
        assert _a.gate in _GATES, f"{_a.label}: no such gate {_a.gate!r}"
        assert _a.modal is None or _a.modal in _MODALS, f"{_a.label}: no modal {_a.modal!r}"
        # A pick screen is one select per pick, then Continue and Back below.
        _rows = len(_a.picks) + 1
        assert _rows <= 5, f"{_a.label}: {len(_a.picks)} picks needs {_rows} rows"
        for _p in _a.picks:
            assert _p.kind in _KIND_WORD, f"{_a.label}: no dropdown for {_p.kind!r}"
    # The legend goes in the embed description, which Discord caps at 4096. Six
    # short lines is nowhere near it; this catches the day someone writes an
    # essay in a blurb, at import rather than when a mod opens the drawer.
    _legend = sum(len(_a.label) + len(_a.blurb) + 8 for _a in _s.actions) + 80
    assert _legend <= 4096, f"{_s.label}: legend is {_legend} chars, Discord allows 4096"
assert len(SECTIONS) <= 24, "home screen holds 24 drawers plus Slash Commands"


# ── the cog ───────────────────────────────────────────────────────────────────

class AdminPanel(commands.Cog):
    """Keeps one panel at the bottom of the staff channel and runs what it offers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sections = SECTIONS
        self._panel: Optional[discord.Message] = None
        # Set when the panel could not delete its predecessors, so the symptom
        # (a channel filling with panels) is explained on the panel itself
        # rather than only in a log nobody is tailing.
        self._warning: Optional[str] = None
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

    def section_visible(self, section: Section, member: discord.Member) -> bool:
        """Anything in here you can press? An empty drawer is worse than a
        missing one — it reads as a bug rather than as a permission."""
        return bool(self.visible_actions(section, member))

    def covered_commands(self) -> frozenset[str]:
        return frozenset(a.command for s in self.sections for a in s.actions)

    def _channel(self) -> Optional[discord.TextChannel]:
        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        return channel if isinstance(channel, discord.TextChannel) else None

    # ── drawing ───────────────────────────────────────────────────────────────

    def _build(self, guild: discord.Guild) -> tuple[discord.Embed, PanelView]:
        """A title, a line, and the door. Nothing else.

        Everything behind the door describes itself, one screen at a time, so
        summarising eight drawers here would only put a wall in front of the
        button somebody came to press.
        """
        description = "Every staff command, without remembering what it's called."
        if self._warning:
            description = f"{self._warning}\n\n{description}"
        embed = discord.Embed(
            title=PANEL_TITLE,
            description=description,
            colour=COLOUR_DANGER if self._warning else COLOUR,
        )
        embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
        embed.set_footer(text="Staff only. He checks. 🐾")
        return embed, PanelView()

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
        channel = self._channel()
        if channel is None:
            log.warning("[panel] staff channel %s is not reachable", PANEL_CHANNEL_ID)
            return
        await self._place(channel, force=True, sweep=True)

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        """Move the panel down for anything that lands above it — except a panel.

        Half of what arrives in this channel is the bot's own: the test paper,
        the reset tests, the status ideas. Those have to push the panel down like
        anything else, so this ignores our *panels* rather than our messages —
        see `_is_our_panel` for why ignoring only the panel is both necessary and
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
        await self.repost(channel)
        await interaction.followup.send(f"Panel reposted in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(OpenButton)
    await bot.add_cog(AdminPanel(bot))
