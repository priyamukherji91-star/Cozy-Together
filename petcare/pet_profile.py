# petcare/pet_profile.py
# -*- coding: utf-8 -*-
"""The pet profile: its fields, the modal that collects them, and how it renders.

Both halves of the pet system need this — registration and editing — so it sits
beside the register rather than inside either cog.

**One form, five boxes.** A Discord modal takes at most five text inputs, so the
whole bio is one screen rather than a menu of half-forms.

**The name is not in here.** It is the one field with rules of its own — it
can't be empty and it can't collide with another of your pets — and it is
changed once in a pet's life, so it has its own small form rather than spending
one of the five boxes forever.
"""
from __future__ import annotations

import datetime
from typing import Awaitable, Callable, Iterable

import discord

from petcare import pet_registry


def _about_prefill(pet: pet_registry.Pet) -> str:
    """The old two boxes shown as one, so nothing anybody wrote goes missing."""
    parts = [(pet.known_for or "").strip(), (pet.traits or "").strip()]
    return "\n".join(part for part in parts if part)


class Field:
    __slots__ = ("key", "label", "placeholder", "cap", "long", "prefill")

    def __init__(
        self,
        key: str,
        label: str,
        placeholder: str,
        long: bool = False,
        prefill: Callable[[pet_registry.Pet], str] | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.placeholder = placeholder
        self.long = long
        self.prefill = prefill
        self.cap = pet_registry.PROFILE_FIELDS.get(key, pet_registry.MAX_NAME_LENGTH)


# Order here is the order on screen and in the rendered card.
BIO: tuple[Field, ...] = (
    Field("species", "What are they?", "cat, rabbit, hamster…"),
    Field("born", "Born", "a year is fine — 2019"),
    Field("treat", "Favourite treat", "fishcake, cheese, chicken…"),
    Field("toy", "Favourite toy", "a shoelace, the box the toy came in…"),
    Field(
        "known_for",
        "What are they like?",
        "screaming at 4am · smug · clingy · deeply stupid",
        long=True,
        prefill=_about_prefill,
    ),
)

# Registration only ever asks these two, so signing up stays quick — the rest is
# offered straight afterwards and from the edit button.
BASICS: tuple[Field, ...] = BIO[:2]

# Written by an older two-box profile, never offered for editing again. Kept so
# a pet nobody has edited since still shows what its owner wrote.
LEGACY: tuple[Field, ...] = (Field("traits", "Personality", ""),)

ALL_FIELDS: tuple[Field, ...] = BIO + LEGACY

# What each field is called on the card, when it has something in it.
LABELS: dict[str, str] = {
    "species": "Species",
    "born": "Born",
    "treat": "Favourite treat",
    "toy": "Favourite toy",
    "known_for": "What they're like",
    "traits": "Personality",
}

Saver = Callable[[discord.Interaction, dict[str, str], str | None], Awaitable[None]]


class ProfileModal(discord.ui.Modal):
    """The fields, pre-filled with whatever is already saved.

    Pre-filling is what makes this work as both the edit screen and the
    fill-in-the-gaps prompt for pets registered before profiles existed: those
    simply open with every box empty.
    """

    def __init__(
        self,
        fields: Iterable[Field],
        *,
        title: str,
        save: Saver,
        pet: pet_registry.Pet | None = None,
        ask_name: bool = False,
        also_clear: tuple[str, ...] = (),
    ) -> None:
        super().__init__(title=title[:45])
        self._save = save
        self._also_clear = also_clear
        self._inputs: list[tuple[str, discord.ui.TextInput]] = []
        self._name_input: discord.ui.TextInput | None = None

        if ask_name:
            self._name_input = discord.ui.TextInput(
                label="Name",
                placeholder="What are they called?",
                default=pet.name if pet else None,
                max_length=pet_registry.MAX_NAME_LENGTH,
                required=True,
            )
            self.add_item(self._name_input)

        for field in fields:
            if pet is None:
                current = None
            elif field.prefill is not None:
                current = field.prefill(pet) or None
            else:
                current = (getattr(pet, field.key) or None)

            box = discord.ui.TextInput(
                label=field.label,
                placeholder=field.placeholder,
                default=current,
                max_length=field.cap,
                required=False,
                style=discord.TextStyle.paragraph if field.long else discord.TextStyle.short,
            )
            self.add_item(box)
            self._inputs.append((field.key, box))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {key: str(box.value or "") for key, box in self._inputs}
        # Emptied rather than left alone: whatever the merged box now holds is
        # the whole answer, and a leftover `traits` would print underneath it
        # saying half of it twice.
        for key in self._also_clear:
            values[key] = ""
        name = str(self._name_input.value) if self._name_input is not None else None
        await self._save(interaction, values, name)


def bio_modal(save: Saver, pet: pet_registry.Pet | None = None):
    """Everything editable about a pet, on one screen."""
    return ProfileModal(
        BIO, title="Edit bio", save=save, pet=pet, also_clear=("traits",)
    )


def name_modal(save: Saver, pet: pet_registry.Pet | None = None):
    """Just the name — its own form, since it has rules the bio fields don't."""
    return ProfileModal((), title="Rename", save=save, pet=pet, ask_name=True)


def age_from(born: str) -> str | None:
    """"2019" -> "about 7". Anything that isn't a plausible year gets left alone.

    The field is free text on purpose — people write "2019", "spring 2019" and
    "no idea" — so this only decorates the cases it understands and never
    rewrites what somebody typed.
    """
    digits = "".join(ch for ch in born if ch.isdigit())
    if len(digits) != 4:
        return None
    year = int(digits)
    now = datetime.datetime.now(datetime.timezone.utc).year
    if not (1970 <= year <= now):
        return None
    age = now - year
    if age == 0:
        return "born this year"
    return f"about {age}"


def rows(pet: pet_registry.Pet) -> list[tuple[str, str]]:
    """The filled-in fields as (label, value), in screen order. Empty ones vanish.

    Walks ALL_FIELDS rather than BIO so a pet still carrying the old separate
    `traits` keeps showing it until somebody edits them.
    """
    out: list[tuple[str, str]] = []
    for field in ALL_FIELDS:
        value = (getattr(pet, field.key) or "").strip()
        if not value:
            continue
        if field.key == "born":
            age = age_from(value)
            value = f"{value} · {age}" if age else value
        out.append((LABELS[field.key], value))
    return out


def apply(embed: discord.Embed, pet: pet_registry.Pet, *, inline: bool = True) -> discord.Embed:
    """Add the profile to an embed. Longer prose goes full width."""
    for label, value in rows(pet):
        embed.add_field(
            name=label,
            value=value[:1024],
            inline=inline and label not in ("What they're like", "Personality"),
        )
    return embed


def missing(pet: pet_registry.Pet) -> list[str]:
    """Labels of the fields still blank, for nudging without nagging.

    BIO only — there is no point naming a field nobody can fill in any more.
    """
    return [LABELS[f.key] for f in BIO if not (getattr(pet, f.key) or "").strip()]
