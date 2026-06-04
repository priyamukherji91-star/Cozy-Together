# 📅 `/event` — Guide

One command to set up an event everywhere at once. You fill in the details **once**
and Mittens creates the native Discord event, a discussion thread, ties them together,
announces it, and reminds people an hour before.

---

## What it does

When you run `/event`, Mittens automatically:

1. **Creates a native Discord Scheduled Event** — shows up in the server's **Events**
   section and renders in each member's own local time.
2. **Opens a forum post** in the events forum for discussion.
3. **Links them together** — the event points at the thread, and the thread shows the
   event (with its cover image).
4. **Announces it in #general** with links to both the event and the thread.
5. **Sends one reminder, 1 hour before** the start — a public nudge plus a DM to
   everyone who clicked **Interested**.

---

## Who can use it, and where

- **Roles:** only members holding one of the **two authorised event roles** (see
  *Admin reference* below for the IDs).
- **Channel:** only in the **admin command channel**. Run it anywhere else and Mittens
  refuses with a pointer to the right channel.

Everyone else interacts with the **results** (the event, the thread, the announcement,
the reminder) — they don't run the command.

---

## Creating an event — step by step

1. Type **`/event`**.
2. *(Optional)* Attach a **cover image** in the single `image` slot.
   - It's genuinely optional. No image → the event simply has no cover (there's no
     default).
   - Only image files are accepted; anything else is rejected with a friendly message.
3. Press **Enter**. A **popup form** opens. Fill in:

   | Field | What to put | Example |
   |---|---|---|
   | **Event name** | The title | `Movie Night` |
   | **Date (YYYY-MM-DD)** | Calendar date | `2026-06-20` |
   | **Start time (AM/PM)** | Time in **AM/PM only** | `7:30pm` or `7pm` |
   | **Description** | What it's about | `Cosy horror double feature` |
   | **Duration (optional)** | Length in minutes; blank = **2 hours** | `90` |

4. **Submit.** Mittens replies **privately to you** (ephemeral) with links to the new
   event and the thread.

> **Time format note:** the time field is **AM/PM only**. 24-hour input like `19:30`
> is rejected — type `7:30pm` instead.

---

## Time zones — how times are handled

- **You type in UK time.** Mittens always reads your typed time as **UK local**
  (`Europe/London`), and it handles GMT/BST automatically. There is no timezone picker.
- **Everyone else sees their own local time.** The native event and the #general
  announcement display per-viewer, so members abroad see the time converted for them.
  You don't need to do anything for that.

---

## The 1-hour reminder

Fires **automatically, 60 minutes before** the event starts:

- **Public post in #general** — no pings, just a visible nudge with both links.
- **A DM to every subscriber** — i.e. everyone who clicked **Interested** on the event.
  - If a member has DMs closed or has blocked the bot, they're **skipped silently**.
  - Want the DM? Just click **Interested** on the event in the Events section.
- **Restart-safe** — even if the bot reboots, the reminder still goes out and won't be
  sent twice.
- **Late-boot protection** — if the bot was offline well past the 1-hour mark, it skips
  the reminder rather than sending a misleading "60 minutes" message.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/event` doesn't appear | Press **Ctrl+R** to refresh Discord; new commands can take a few minutes to show. |
| "Use this in #…" | You're in the wrong channel — run it in the admin command channel. |
| "You don't have paws for that 🐾" | Your account doesn't have one of the two authorised roles. |
| "That start time is in the past" | Times are **UK time** — make sure you're ahead of UK-now. |
| "Couldn't read that time" | Use **AM/PM**, e.g. `7:30pm` (not `19:30`). |
| "That attachment isn't an image" | The `image` slot only takes pictures — leave it empty otherwise. |
| No #general announcement / no reminder | Usually a bot permission gap — see *Admin reference*. |

---

## Admin reference (config)

All hardcoded in `cogs/events.py`:

| Setting | Value |
|---|---|
| Guild | `1425974791516586045` |
| Events forum channel | `1441764350930063400` |
| #general (announcement + public reminder) | `1425974792745648252` |
| Command channel (where `/event` is allowed) | `1429796227192459264` |
| Authorised role IDs | `1425977436859797595`, `1426194314337189949` |
| Reminder offset | 1 hour before start |
| Default duration | 120 minutes |
| Reminder state ledger | `DATA_DIR/events_reminders.json` (Railway volume) |

**Bot permissions required:**

- **Manage Events** (server-wide) — create/edit the scheduled event.
- In the **events forum**: View Channel, **Create Posts** (Send Messages),
  **Send Messages in Threads**.
- In **#general**: View Channel, **Send Messages**, **Embed Links**.

**Notes for maintainers:**

- The scheduled event is an **external** event (a forum channel can't be a native event
  location), so it has an end time (start + duration) and its location is set to the
  thread link.
- The forum post does **not** attach the image as a file — the event link in the post
  already unfurls the cover image, so attaching it again would show it twice.
- Reminders are **not** in-memory timers: a `tasks.loop` re-reads events from Discord
  each minute and records what it has sent in the JSON ledger, so restarts are safe.
