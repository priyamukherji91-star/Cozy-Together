<!-- Paste each block below as its own Discord message, in order. -->
<!-- Every block is under 2000 characters. -->

--- MESSAGE 1 ---

# 🐾 Pets are here

Register your own pets with a photo, feed everyone else's, and ship them with each other.

Everyone gets **6 treats a day**. Every pet remembers who has fed it most — and that person becomes its **favourite human**.

## Registering — in <#1427657614061207724>

Two ways, and both ask you for a name first:

- **`/pet add`** — fill in `name`, attach a `photo`.
- **Right-click a photo you already posted** → **Apps** → **Mittens the Menace** → **This is my pet** → type the name.

The right-click route only works on **your own** messages, and only on messages with an image on them. Nobody can claim your pet as theirs.

⚠️ **Use a post with only ONE photo in it.** If your message has several, the bot grabs the first one and you can't pick a different one.

**The rules:**
- 6 pets each
- Names up to 24 characters
- No two of your own pets with the same name (two *different* people can both have a Mochi)
- Photos under 10 MB, and an actual image

## 🖼️ The photo becomes the pet's avatar

**The picture you register is that pet's face from then on** — the thumbnail on every `/feed`, the picture on `/petinfo`, and the pet's half of the `/shippet` card. Pick one you're happy to see everywhere.

It's cropped to a **square**, aiming at the busiest part rather than the dead centre, so tall portraits keep the head instead of the chest. A photo where the pet is already roughly centred comes out best.

To change it later: `/pet remove` and register again. Your photo is copied and stored by the bot, so deleting your original message won't break anything.

--- MESSAGE 2 ---

## Feeding — in <#1436115021066408016>

**`/feed`** spends one of your treats on a pet.

- **6 treats a day**, refilled at **midnight**
- **One treat per pet per day** — you can't dump all six into one animal
- Feeding **your own** pet is allowed, and it counts

Start typing in the `pet` box and it'll suggest every registered pet with whose it is, like `Mochi — Sam's`, so same-named pets are still easy to tell apart.

## 👑 The favourite human

Whoever has given a pet the **most** treats is its **favourite human**. When the title changes hands, the bot announces it right there in the feed message.

If two people are **tied**, **nobody** holds the title until someone pulls ahead.

So it's not first-come-first-served, and it's not permanent. Keep feeding and you can take it off someone.

--- MESSAGE 3 ---

## 💞 Shipping pets — in <#1436115021066408016>

**`/shippet`** pairs a pet with someone and scores them, on the same card `/ship` uses.

- **`pet`** — the pet. Required.
- **`partner`** — *optional.* Leave it empty to ship the pet with **you**. Otherwise pick from the list, which has **both** pets 🐾 and members 👤 in it.

One command, three things:

- `/shippet pet:Mochi` → **your pet and you**
- `/shippet pet:Mochi partner:🐾 Luna` → **two pets**
- `/shippet pet:Mochi partner:👤 Sam` → **a pet and a member**

The pet's registered photo is its half of the card. Pets can't be pinged, so the ping goes to their owner instead.

Scores are **locked in for the day** — same pairing, same number, either order — and reroll at midnight. Exactly like `/ship`.

--- MESSAGE 4 ---

## Checking on things

All of these go in <#1436115021066408016>:

- **`/treats`** — how many of today's six you have left, and who you've already fed
- **`/pet list`** — every pet in the server and whose it is
- **`/petinfo`** — one pet in detail: total treats, its favourite human, and the top 5 people who've fed it
- **`/petboard`** — the scoreboard: best-fed pets each shown with their favourite human, plus a **most generous** list of the top feeders overall

## Removing

**`/pet remove`** takes one of yours off the list — the suggestions only show your own pets, so you can't remove someone else's by accident. Mods can remove any of them.

--- MESSAGE 5 ---

## Who sees what

Everything is public **except registering** — **`/pet add`** and the right-click route reply **only to you**, so your confirmation doesn't land on top of the photos in <#1427657614061207724>.

Errors are always private too.

## A few last things

- Once a day, *if* any pets have gone **7+ days** without food, the bot posts a short list in <#1436115021066408016>. If everyone's been fed, it says nothing at all.
- Everything is saved to disk, so treats and pets survive restarts.

**Quick start:** go to <#1427657614061207724>, post a photo of your pet, right-click it → **Apps** → **This is my pet**. Then head to <#1436115021066408016>, `/feed` something, and `/shippet` your pet with a friend. 🐾
