# petcare/pet_treats.py
# -*- coding: utf-8 -*-
"""What a pet gets handed — food or a toy — and what it's called.

The rule is deliberately plain: **a pet with a favourite treat written on its
profile always gets that; anything else gets one of the standard treats at
random.** No hand to manage, no scarcity, no way to earn more — the daily
allowance (`cogs/pet_care.py`'s DAILY_TREATS) is the only limit, and picking the
pet is still the only decision.
"""
from __future__ import annotations

import random
from typing import Iterable

# The standard treats, handed out when a pet has no favourite of its own.
# Singular so they read correctly in "A {treat} for {pet}, then."
STAPLES: tuple[str, ...] = (
    "jerky",
    "cheese",
    "sardine",
    "biscuit",
    "chicken bite",
    "milk drop",
    "fish bite",
    "meat chunk",
    "crunchy stick",
    "egg bite",
)

# Known treats get the right picture. Anything a member invents falls through to
# a stable pick from FALLBACK, so the same word always looks the same.
EMOJI: dict[str, str] = {
    "jerky": "🥓",
    "cheese": "🧀",
    "sardine": "🐟",
    "biscuit": "🍪",
    "chicken bite": "🍗",
    "milk drop": "🥛",
    "fish bite": "🐠",
    "meat chunk": "🥩",
    "crunchy stick": "🥨",
    "egg bite": "🥚",
    "fishcake": "🍥",
    "chicken": "🍗",
    "fish": "🐟",
    "milk": "🥛",
    "egg": "🥚",
    "honey": "🍯",
    "carrot": "🥕",
    "apple": "🍎",
    "banana": "🍌",
    "bone": "🦴",
    "shrimp": "🍤",
    "steak": "🥩",
    "tuna": "🐟",
    "salmon": "🍣",
    "cake": "🧁",
    "seed": "🌾",
    "lettuce": "🥬",
    "popoto": "🥔",
    "meat": "🥩",
}

# The same idea for playing: a pet with a favourite toy on its profile always
# gets that one, anything else gets one of these. Singular, for the same reason.
TOYS: tuple[str, ...] = (
    "ball",
    "feather wand",
    "cardboard box",
    "squeaky mouse",
    "bit of string",
    "paper ball",
    "rope knot",
    "bottle cap",
    "laser dot",
    "old sock",
)

TOY_EMOJI: dict[str, str] = {
    "ball": "🥎",
    "feather wand": "🪶",
    "cardboard box": "📦",
    "squeaky mouse": "🐭",
    "bit of string": "🧵",
    "paper ball": "📄",
    "rope knot": "🪢",
    "bottle cap": "🍾",
    "laser dot": "🔴",
    "old sock": "🧦",
    "string": "🧵",
    "box": "📦",
    "mouse": "🐭",
    "feather": "🪶",
    "rope": "🪢",
    "stick": "🥢",
    "sock": "🧦",
    "laser": "🔴",
    "wand": "🪄",
    "bell": "🔔",
    "blanket": "🧶",
    "wool": "🧶",
    "shoelace": "👟",
    "toy": "🧸",
}

FALLBACK: tuple[str, ...] = ("🍖", "🥩", "🍤", "🥚", "🍯", "🧁", "🍡", "🥨")
TOY_FALLBACK: tuple[str, ...] = ("🧸", "🪀", "🧶", "🎾", "🪁", "🔔")

MAX_LABEL = 32


def normalise(name: str) -> str:
    """Collapse to the comparable form. Empty means "no favourite set"."""
    return " ".join(str(name or "").split()).casefold()[:MAX_LABEL]


def emoji_for(treat: str) -> str:
    key = normalise(treat)
    if key in EMOJI:
        return EMOJI[key]
    for word, glyph in EMOJI.items():
        if word in key:                      # "dried fish" -> 🐟
            return glyph
    if not key:
        return "🍖"
    # Stable across processes, unlike hash() which is salted per run.
    return FALLBACK[sum(ord(c) for c in key) % len(FALLBACK)]


def label(treat: str) -> str:
    key = normalise(treat)
    return f"{emoji_for(key)} {key}" if key else "🍖 treat"


def pick(favourite: str, rng: random.Random | None = None) -> tuple[str, bool]:
    """(what they get, was it their own favourite).

    A pet with a favourite always gets it — the point of writing one down is
    that it's what they eat, not that it might be.
    """
    want = normalise(favourite)
    if want:
        return want, True
    return (rng or random).choice(STAPLES), False


def toy_emoji_for(toy: str) -> str:
    key = normalise(toy)
    if key in TOY_EMOJI:
        return TOY_EMOJI[key]
    for word, glyph in TOY_EMOJI.items():
        if word in key:                      # "a ratty old shoelace" -> 👟
            return glyph
    if not key:
        return "🧸"
    return TOY_FALLBACK[sum(ord(c) for c in key) % len(TOY_FALLBACK)]


def toy_label(toy: str) -> str:
    key = normalise(toy)
    return f"{toy_emoji_for(key)} {key}" if key else "🧸 toy"


def pick_toy(favourite: str, rng: random.Random | None = None) -> tuple[str, bool]:
    """(what they get to play with, was it their own favourite). Mirrors `pick`."""
    want = normalise(favourite)
    if want:
        return want, True
    return (rng or random).choice(TOYS), False


def tally(treats: Iterable[str]) -> list[tuple[str, int]]:
    """Grouped for display: [("fishcake", 2), ("cheese", 1)], commonest first."""
    counts: dict[str, int] = {}
    for treat in treats:
        counts[treat] = counts.get(treat, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
