---
name: Discord thread access
description: Discord commands may be received in a thread before the bot has joined it, so reply paths should ensure thread membership.
---

# Discord Thread Access

**Rule:** Before prefix or slash commands reply in a Discord thread, attempt to join the thread; log archived, locked, forbidden, and HTTP failures clearly.

**Why:** Discord thread membership and send permissions are separate from access to the parent channel, and failures otherwise look like the bot ignored the command.

**How to apply:** Keep the join step in both the prefix command hook and slash interaction check. If joining is forbidden, the guild must grant View Channel and Send Messages in Threads, or add the bot to private threads.