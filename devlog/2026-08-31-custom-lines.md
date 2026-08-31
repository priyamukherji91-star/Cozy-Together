---
title: Editable line pools for Mittens' random lines
tags: Developments, Done
---
**Summary**
Every set of lines Mittens picks from at random is now editable from the panel. 11 pools, ~190 lines.

**Pools**
• Discord statuses (97)
• Daily reset announcements (31) / weekly (6)
• Timeout announcements (9)
• Pet feeding (12), playing (12), hungry-pet nudges (12), multi-feed (7), new favourite (3)
• Wall of shame footers (25)
• Paper filler when there's no menace photo (3)

**Location**
Administration → Mittens the Menace → Mittens custom lines

**Actions**
• Read, add, edit or delete individual lines
• Arrows page through the long pools
• Reset restores that pool's coded defaults

**Notes**
• Edits go live immediately, stored on the volume, survive deploys
• Placeholders are validated on save — `{pet}`, `{user}`, `{duration}` etc. A line missing a required one, or inventing an unknown one, is rejected with the reason
• Defaults are never overwritten, so Reset is always available
