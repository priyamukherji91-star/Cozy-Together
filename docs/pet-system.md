# Pets — Guide

Register your own pets with a photo, and feed everyone else's. Each person gets
six treats a day, and every pet keeps track of who has fed it most.

---

## Where the commands work

There are two channels involved, and it matters which is which:

- **Pet photos channel** — where you **register** a pet. That's where the photos
  already are, so that's where registering happens.
- **Bot commands channel** — **everything else**: feeding, the lists and the
  scoreboards.

Run a command in the wrong channel and the bot replies privately with a pointer
to the right one. Nobody else sees it.

---

## Registering a pet

Two ways. Both ask you for a name before anything is saved.

**1. `/pet add`** — in the pet photos channel.

- `name` — what the pet is called.
- `photo` — attach a picture.

**2. Right-click a photo you already posted.**

- Right-click (or long-press on mobile) your message → **Apps** → **Mittens the
  Menace** → **This is my pet**.
- A small form opens asking for the name.
- This only works on **your own** messages, and only on messages with an image
  attached. You can't claim someone else's photo as your pet.
- **Use a message with only one photo in it.** If you posted several at once,
  the bot takes the first one and there's no way to pick a different one. Post
  the pet on its own and register from that.

### The rules

| Limit | Value |
| --- | --- |
| Pets per member | 6 |
| Name length | 24 characters |
| Duplicate names | Not allowed for the same owner |
| Photo size | Under 10 MB |
| Photo type | An actual image — PNG or JPEG |

Two different people **can** both have a pet called Mochi. You just can't have
two of your own.

### The photo becomes the pet's avatar

**The picture you register is the pet's face from then on.** It's the thumbnail
on every `/feed` message, the picture on `/petinfo`, and the pet's half of the
`/shippet` card. So pick one you're happy to see everywhere.

It gets cropped to a **square** and stored at 256×256. The crop aims at the
busiest part of the picture rather than the dead centre, so a tall portrait
keeps the animal's head instead of its chest — but a photo where the pet is
already roughly centred will always come out best.

There's no way to swap the photo later. To change it, `/pet remove` the pet and
register it again with the picture you want.

The photo is copied and stored by the bot, so it keeps working forever. Deleting
your original message in the photos channel does not break it.

---

## Feeding

**`/feed`** — spend one treat on a pet.

- You get **6 treats every day**.
- **One treat per pet, per day, per person.** You can't pour all six into one
  pet.
- Your allowance refills at **midnight**.
- Feeding **your own** pet is allowed, and it counts.

Start typing in the `pet` box and the bot suggests every registered pet, labelled
with whose it is — `Mochi — Sam's` — so two pets with the same name are still
easy to tell apart.

---

## The favourite human

Every pet remembers how many treats it has had from each person.

- Whoever has given a pet the **most** treats is that pet's **favourite human**.
- When the title **changes hands**, the bot says so in the feed message.
- If two people are **tied for the lead, nobody holds the title** — the pet has
  no favourite until someone pulls ahead again.

So it's not first-come-first-served, and it's not permanent. Keep feeding and
you can take it off someone.

---

## Checking on things

All of these go in the bot commands channel.

**`/treats`** — how many of today's six you have left, and which pets you've
already fed today.

**`/pet list`** — every pet registered in the server and whose it is.

**`/petinfo`** — one pet in detail: its photo, how many treats it has eaten in
total, its favourite human, and the top five people who have fed it.

**`/petboard`** — the scoreboard. Two lists:

- **Most treats** — the best-fed pets, each shown with its favourite human.
- **Most generous** — the people who have given out the most treats overall.

---

## The quiet reminder

Once a day, **if** any pets have gone **7 days or more** without being fed, the
bot posts a short list of up to four of them in the **bot commands channel** —
the same place people run `/feed`, so the reminder sits next to the fix.

If nothing has been neglected, it posts nothing at all. Most days, that's what
happens.

---

## Shipping pets

**`/shippet`** — pairs a pet with someone and gives them a compatibility score,
using the same card as the normal `/ship`. Bot commands channel only.

- `pet` — the pet. Required.
- `partner` — **optional.** Leave it empty to ship the pet with *you*. Otherwise
  pick from the list, which contains **both** pets (🐾) and members (👤).

So one command covers all three:

| What you want | What you type |
| --- | --- |
| Your pet and you | `/shippet pet:Mochi` |
| Two pets | `/shippet pet:Mochi partner:🐾 Luna` |
| A pet and a member | `/shippet pet:Mochi partner:👤 Sam` |

The pet's registered photo is used as its half of the card. Pets can't be
pinged, so the mention goes to their owner instead.

Scores are **deterministic per day** — the same pairing gives the same number
all day, in either order, and rerolls at midnight. Same rules as `/ship`.

---

## Removing a pet

**`/pet remove`** — removes one of your own pets. The suggestion list only shows
your pets, so you can't remove someone else's by accident.

Moderators (anyone with **Manage Messages**) can remove any pet, and their
suggestion list shows all of them.

Removing a pet deletes its stored photo too.

---

## What's public and what isn't

Everything posts publicly **except registering a pet**. `/pet add` and the
right-click route both reply only to you, because they're used in the pet photos
channel where the photos themselves are the point — the confirmation shouldn't
sit on top of them.

Errors — wrong channel, out of treats, name already taken — are only ever shown
to you, in every command.

---

## Notes
- Treats and pets are stored on disk and survive restarts and redeploys.
- The allowance rolls over by date, so a restart at midnight can't cause a
  missed or double reset.

---

## Admin reference

| Setting | Value |
| --- | --- |
| Register channel | `1427657614061207724` (pet photos) |
| Commands channel | `1436115021066408016` (bot commands) |
| Treats per day | 6 |
| Pets per owner | 6 |
| Name limit | 24 characters |
| Reset timezone | Europe/Brussels |
| Neglect threshold | 7 days, max 4 pets named |
| Nudge time | 18:30 |

Code lives in `cogs/pets.py`; storage in `petcare/storage.py`. State is written
to `$DATA_DIR` (the Railway volume): `pets.json`, `pet_treats.json`, and one PNG
per pet in `pets/`.
