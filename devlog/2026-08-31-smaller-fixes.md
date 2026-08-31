---
title: Command changes bundled with the panel
tags: Developments, Done
---
**Summary**
Smaller command-level changes made while building the panel.

**Changes**
• `/birthday add` — new. Set a birthday for another member (admin, admin-commands channel)
• `/setup_panels` — new. Reposts the landing-zone gate and the get-roles menus in one go, clearing the previous set instead of stacking
• `/purge` — optional `channel` argument; still defaults to the channel you're in
• `/status_ideas` — optional `private` argument; returns the ideas to you only, in a copy-pasteable block
• `/pettreats` — opened from owner-only to the panel's staff roles. Still refills your own allowance only
• `/event`, the admin `/birthday` commands and `/avatar_*` — now accepted in the staff panel channel as well as admin-commands

**Notes**
• Panel self-audit on boot: every button is checked against the command behind it, and a mismatch is reported on the panel rather than failing when pressed
• No permission gates were widened — the panel re-runs each command's own checks before calling it
