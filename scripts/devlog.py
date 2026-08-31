#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Post a dev-log entry to the developers' corner forum, as Mittens.

Every change to this bot is supposed to end up as a post in the forum, written
for the people who use the server rather than for whoever wrote the code. Doing
that by hand means it stops happening the first busy week, so it lives here as
one command.

    railway run --service worker python scripts/devlog.py --list-tags
    railway run --service worker python scripts/devlog.py devlog/panel.md
    railway run --service worker python scripts/devlog.py devlog/*.md --dry-run

`railway run` is how it gets a token: `DISCORD_TOKEN` is the running bot's, held
by the service, and there is no copy of it on this machine. Without the token it
says so and stops rather than half-posting.

**A post is a file**, so the writing happens in an editor and the posting is the
boring part:

    ---
    title: Mittens now does the thing
    tags: Developments, Done
    ---
    Two or three short paragraphs, in plain words.

Tags are matched by name against the forum's own list, case-insensitively, so a
tag renamed in Discord fails loudly here instead of posting untagged. `--list-tags`
prints what the forum actually has.

The client connects, posts, and logs out — no cog, no listener, nothing left
running. It deliberately does *not* run inside the bot: a change is posted when
somebody decides it is worth posting, and that decision is not something the bot
can make about its own deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import NamedTuple

import discord

# The developers' corner. A forum channel: posts are threads, tags are its own.
FORUM_CHANNEL_ID = 1497225972800422059

# Discord's cap on a message. A dev-log post that needs more than this is two
# posts, or it is a technical document that belongs somewhere else.
MAX_BODY = 2000
MAX_TITLE = 100


class Post(NamedTuple):
    path: Path
    title: str
    tags: list[str]
    body: str


def parse(path: Path) -> Post:
    """Read one post file. Fails on anything ambiguous rather than guessing."""
    raw = path.read_text(encoding="utf-8").lstrip("﻿")
    if not raw.startswith("---"):
        raise SystemExit(f"{path}: needs a --- front matter block with title and tags")

    _, _, rest = raw.partition("---")
    front, sep, body = rest.partition("---")
    if not sep:
        raise SystemExit(f"{path}: front matter is not closed with ---")

    fields: dict[str, str] = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        if not value:
            raise SystemExit(f"{path}: can't read front-matter line {line!r}")
        fields[key.strip().lower()] = value.strip()

    title = fields.get("title", "").strip()
    if not title:
        raise SystemExit(f"{path}: no title")
    if len(title) > MAX_TITLE:
        raise SystemExit(f"{path}: title is {len(title)} characters, the limit is {MAX_TITLE}")

    tags = [t.strip() for t in fields.get("tags", "").split(",") if t.strip()]
    if not tags:
        raise SystemExit(f"{path}: no tags — see --list-tags")

    text = body.strip()
    if not text:
        raise SystemExit(f"{path}: no body")
    if len(text) > MAX_BODY:
        raise SystemExit(f"{path}: body is {len(text)} characters, the limit is {MAX_BODY}")

    return Post(path, title, tags, text)


def resolve_tags(forum: discord.ForumChannel, wanted: list[str], path: Path) -> list[discord.ForumTag]:
    """Names to the forum's own tags. An unknown name stops everything.

    Posting untagged would be worse than not posting: the forum is read by tag,
    and an untagged post is one nobody filters into view.
    """
    by_name = {tag.name.lower(): tag for tag in forum.available_tags}
    found: list[discord.ForumTag] = []
    for name in wanted:
        tag = by_name.get(name.lower())
        if tag is None:
            raise SystemExit(
                f"{path}: no tag called {name!r} in #{forum.name}. "
                f"It has: {', '.join(sorted(t.name for t in forum.available_tags))}"
            )
        found.append(tag)
    return found


async def run(paths: list[Path], *, dry_run: bool, list_tags: bool) -> int:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print(
            "No DISCORD_TOKEN. Run this through Railway so it borrows the bot's own:\n"
            "  railway run --service worker python scripts/devlog.py …",
            file=sys.stderr,
        )
        return 2

    posts = [parse(p) for p in paths]

    client = discord.Client(intents=discord.Intents.default())
    failures = 0

    @client.event
    async def on_ready() -> None:  # noqa: ANN202
        nonlocal failures
        try:
            forum = client.get_channel(FORUM_CHANNEL_ID) or await client.fetch_channel(
                FORUM_CHANNEL_ID
            )
            if not isinstance(forum, discord.ForumChannel):
                print(f"{FORUM_CHANNEL_ID} is not a forum channel", file=sys.stderr)
                failures += 1
                return

            if list_tags:
                print(f"#{forum.name} tags:")
                for tag in forum.available_tags:
                    print(f"  {tag.name}")
                return

            for post in posts:
                tags = resolve_tags(forum, post.tags, post.path)
                if dry_run:
                    print(f"\n── would post to #{forum.name} ──")
                    print(f"title: {post.title}")
                    print(f"tags : {', '.join(t.name for t in tags)}")
                    print(post.body)
                    continue
                thread = await forum.create_thread(
                    name=post.title,
                    content=post.body,
                    applied_tags=tags,
                    # A dev-log post is an announcement, not a mention machine.
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                print(f"posted: {post.title} → {thread.thread.jump_url}")
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            failures += 1
        except discord.Forbidden:
            print("Mittens can't post in that forum — check his permissions.", file=sys.stderr)
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
        finally:
            await client.close()

    await client.start(token)
    return 1 if failures else 0


def main() -> int:
    # A Windows console defaults to cp1252, and these posts have bullets and em
    # dashes in them: without this a --dry-run dies on its own output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Post a dev-log entry to the developers' corner.")
    ap.add_argument("files", nargs="*", type=Path, help="post files, in the order to post them")
    ap.add_argument("--dry-run", action="store_true", help="print the posts instead of sending them")
    ap.add_argument("--list-tags", action="store_true", help="print the forum's tags and stop")
    args = ap.parse_args()

    if not args.files and not args.list_tags:
        ap.error("give me a post file, or --list-tags")
    return asyncio.run(run(args.files, dry_run=args.dry_run, list_tags=args.list_tags))


if __name__ == "__main__":
    raise SystemExit(main())
