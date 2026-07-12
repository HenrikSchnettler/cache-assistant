---
description: Show the current session's prompt-cache tier, countdown, and cold re-cache cost.
---

Run the Cache Assistant status readout for the current session and show the user
the output verbatim:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/statusline/cache_status.py"
```

If it reports it cannot locate a transcript, tell the user to run this from the
directory of the session they want to inspect, or to pass `--session <id>`.
