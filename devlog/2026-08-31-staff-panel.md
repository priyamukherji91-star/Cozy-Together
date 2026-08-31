---
title: Admin panel for staff commands
tags: Developments, Done
---
**Summary**
All staff commands are now reachable from one button in the staff channel. Nothing was removed — every command is still typeable.

**Drawers**
• Discord Moderation — purge, timeout, untimeout
• Bot Panels — repost landing zone + get-roles, repost pet panel
• Morning News — test paper, post today's paper
• Birthdays — list, today, add, remove
• Mittens the Menace — rotate status, status ideas, custom lines, avatar
• Pets — treats top-up
• Create an Event — on the home screen
• Slash Commands — live list of everything without a button

**Usage**
• Press **Administration** → private menu, visible only to you
• Buttons ask for what they need (channel / member / duration) then run
• You only see what your roles allow
• Panel stays at the bottom of the channel; `/adminpanel` reposts it

**Notes**
• Permissions unchanged — each button runs the same command with the same checks
• Destructive actions (purge, remove, live paper post) have a confirm step
• Screens expire after ~12 min; press Administration again
