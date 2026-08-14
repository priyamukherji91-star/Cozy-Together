# Pets — Guide

Register your own pets with a photo, and feed everyone else's. Each person gets
**ten treats and three plays a day**, and every pet keeps track of who has fed
it most.

Everything lives in <#1537820996390883440>, the pet channel.

---

## The panel

The pet system is **one message**, and the bot keeps it as the **last message in
the pet channel** so you never have to scroll back for it. Talk in the channel
and it moves down to meet you.

The panel shows, for the whole server:

- **Hungry right now** — how many pets nobody has fed today.
- **Resets in** — a live countdown to midnight, in your own timezone.
- **Fed here today** — treats handed out across the server today.
- **Still waiting** — up to six cards for the pets nobody has fed today, each
  with a coloured hunger bar: 🟩 fine, 🟧 peckish, 🟥 starving.

Six buttons under it:

| Button | What it does |
| --- | --- |
| 🍖 **Feed pets** | Opens **your** private feeding list. |
| 🧸 **Play** | Play with one pet. Three a day. |
| 🍬 **Treat bag** | What you have left today, and who you've already fed. |
| 🏆 **Board** | The scoreboard. Posts publicly. |
| 📖 **Pet dex** | Every pet, one per page, with a jump menu and an index. |
| 🐾 **Manage my pets** | Your own pets only — edit, rename, remove. |

Everything except the board and the feed posts is **private to you**.

---

## Feeding

Press **🍖 Feed pets** and you get a list only you can see.

1. **Tick** everyone you want to feed — up to your whole day's allowance at once.
2. **Close the menu.** Discord tells the bot nothing until you do, so the ticks
   only appear in the list once the menu is shut.
3. Press **🍖 Feed the N ticked.**

The list is drawn **hungriest first** and only shows pets you can still feed
today. Ones you've already fed are named underneath it instead.

### The rules

- **10 treats a day, each.** They come back at **midnight** (Europe/Brussels).
- **One treat per pet, per day, per person.** You can't pour all ten into one pet.
- **Your allowance is yours.** A pet somebody else fed an hour ago is still
  yours to feed.
- Feeding **your own** pet is allowed, and it counts.

### What they get

A pet with a **favourite treat** written on its profile **always** gets that
one — the post says *their favourite* when it happens. Everyone else gets one of
ten standard treats at random.

Feeding one pet posts their photo and a line from Mittens. Feeding several posts
them as a **photo grid** with their names on it, plus one line for the lot.

---

## Playing

Press **🧸 Play** and pick one pet. **Three plays a day**, one per pet — plays
are deliberately scarcer than treats, so choosing matters.

A pet with a **favourite toy** on its profile always gets that. Playing is
tracked separately from feeding and **does not** count toward the favourite
human race — that stays about food.

---

## The favourite human

Every pet remembers how many **treats** it has had from each person.

- Whoever has given a pet the **most** is that pet's **👑 favourite human**.
- When the title **changes hands**, the bot announces it in the feed post.
- If two people are **tied for the lead, nobody holds the title** — the pet has
  no favourite until someone pulls ahead again.

Not first-come-first-served, and not permanent. Keep feeding and you can take it
off someone.

---

## Registering a pet

Two ways. Both ask for a name, and offer **species** and **year born** while
they're at it — leave those blank if you like, they're editable forever after.

**1. `/pet add`** — in the pet channel.

- `name` — what they're called.
- `photo` — attach a picture.

**2. Right-click a photo you already posted.**

- Right-click (or long-press on mobile) your message → **Apps** → **Mittens the
  Menace** → **This is my pet**.
- Works in **<#1427657614061207724>** as well as the pet channel — the claim has
  to reach where the photos actually are.
- Only on **your own** messages, and only on messages with an image attached.
- **Use a message with only one photo in it.** If you posted several at once,
  the bot takes the first one and there's no way to pick a different one.

Straight after registering you get an **✨ Add what they're like** button, which
opens the full bio form. You can skip it and do it later from 🐾 Manage my pets.

### The rules

| Limit | Value |
| --- | --- |
| Pets per member | 6 |
| Name length | 24 characters |
| Duplicate names | Not allowed for the same owner |
| Photo size | Under 32 MB |
| Photo type | An actual image — PNG or JPEG |

Two different people **can** both have a pet called Mochi. You just can't have
two of your own.

### The photo becomes the pet's face

**The picture you register is the pet's face from then on** — the thumbnail on
every feed post, its tile in the grid, its picture in the dex, and its half of
the `/shippet` card.

It's cropped to a **square** and stored at 256×256. The crop aims at the busiest
part of the picture rather than the dead centre, so a tall portrait keeps the
animal's head instead of its chest.

There's no way to swap the photo later. To change it, remove the pet and
register it again. The photo is copied and stored by the bot, so deleting your
original message doesn't break it.

---

## The profile

One form, five boxes, all optional:

| Field | What it does |
| --- | --- |
| **What are they?** | Species. If the server has an emoji with that name, it becomes the pet's icon on the panel. |
| **Born** | A year is enough — "2019" renders as *2019 · about 7*. |
| **Favourite treat** | **They will always be fed this.** |
| **Favourite toy** | **They will always be played with this.** |
| **What are they like?** | Free text, several lines if you want. |

Edit it from 🐾 **Manage my pets** → ✏️ **Edit bio**. **Only the owner** can edit
a bio or rename a pet — not mods.

---

## The pet dex

📖 **Pet dex** is the catalogue. One pet per page, **numbered by registration
date** so the numbers never shift when somebody new signs up.

- **◀ ▶** to walk, **Jump to…** to skip, **📋 All pets** for the whole index.
- ✅ means **you have fed that pet at least once**; ▫️ means you haven't. The
  footer counts how many of them you've caught.

The dex only reads — you can't feed from it. Feeding is the feeding list, and
one thing doing two jobs is how you end up bypassing your own treat count.

---

## The board

🏆 **Board** posts publicly. Two lists:

- **Most treats** — the best-fed pets, each with its 👑 favourite human.
- **Most generous** — the people who have given out the most treats overall.

---

## Managing your pets

🐾 **Manage my pets** lists **only your own**. Pick one and you get:

- ✏️ **Edit bio** — the five-box form.
- 🏷️ **Rename** — its own small form, since a name can't be blank or clash.
- 🗑️ **Remove** — behind a confirmation. Deletes the photo **and** the treat
  history.

`/pet remove` still exists as a command, and mods (**Manage Messages**) can use
it on anyone's pet — taking a photo down is moderation. `/pet list` shows every
pet in the server, privately.

---

## Shipping pets

**`/shippet`** — pairs a pet with someone and gives them a compatibility score,
using the same card as `/ship`. Unchanged by the revamp, and **only works in
<#1436115021066408016>** — it is deliberately kept out of the pet channel, which
belongs to the panel. Run it anywhere else and it refuses privately.

- `pet` — the pet. Required.
- `partner` — **optional.** Leave it empty to ship the pet with *you*. Otherwise
  pick from the list, which contains **both** pets (🐾) and members (👤).

Scores are **deterministic per day** — the same pairing gives the same number
all day, in either order, and rerolls at midnight.

---

## Notes

- Treats, plays, pets and profiles are stored on disk and survive restarts and
  redeploys.
- The allowance rolls over **by date**, not on a timer, so a restart at midnight
  can't cause a missed or double reset.
- There is no background task and no daily nudge. The panel says who is hungry,
  and it's always the last thing in the channel.
- The panel moves at most once every few seconds no matter how busy the channel
  gets, so a conversation costs one repost rather than one per message.

---

## Admin reference

| Setting | Value |
| --- | --- |
| Pet channel (panel, feeding, `/pet add`) | `1537820996390883440` |
| Pet photos channel (right-click claim only) | `1427657614061207724` |
| Treats per day | 10 |
| Plays per day | 3 |
| Pets per owner | 6 |
| Name limit | 24 characters |
| Reset timezone | Europe/Brussels |

**`/petpanel`** — admins only (Administrator or Manage Server). Posts a fresh
panel and sweeps up any strays. Needed after purging the channel, since the
panel the bot is holding on to no longer exists.

**`/pettreats`** — **owner only** (`1130859582407847977`). Hands today's treats
and plays back and clears the list of who you've fed, so the whole thing can be
tested without waiting for midnight. It only ever touches the caller's own
record — there is no version of it that hands somebody else treats.

Code lives in `cogs/pet_care.py`; the register in `petcare/pet_registry.py`, the
profile form in `petcare/pet_profile.py`, the treat and toy tables in
`petcare/pet_treats.py`, and atomic JSON plus photo cropping in
`petcare/storage.py`. State is written to `$DATA_DIR` (the Railway volume):
`pets.json`, `pet_treats.json`, `pet_panel.json`, and one PNG per pet in
`pets/`.
