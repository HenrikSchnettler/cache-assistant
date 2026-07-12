---
name: install-statusline
description: Add the Cache Assistant prompt-cache tier + live countdown row to the user's Claude Code status line, non-destructively merging with any status line they already have.
---

# Install the Cache Assistant status line

Use this skill when the user asks to install / set up / add the Cache Assistant
status line (the cache tier + countdown row).

It runs `install_statusline.py`, which:

- **Preserves any existing status line.** If the user already has a
  `statusLine` command, the installer wraps it: the original command runs first
  and Cache Assistant's row is appended below it. The original is also saved to
  `<settings-dir>/cache-assistant/original-statusline.json` for `--restore`.
- If there is no existing status line, it points `statusLine.command` straight
  at Cache Assistant's script.
- Sets `statusLine.refreshInterval` to `1` so the countdown ticks once per
  second, and preserves unrelated fields like `padding`.
- Backs up `settings.json` before writing.

## Steps

1. **Preview** the change first (no files written):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/install-statusline/install_statusline.py" --print
   ```

   This prints the detected mode (fresh install vs. merge), the resolved paths,
   and the exact `statusLine` block that would be written. Show the user.

2. **Apply** it:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/install-statusline/install_statusline.py"
   ```

3. Tell the user to **restart Claude Code** (or trigger any interaction) for the
   status line to take effect, and that the row looks like
   `⚡ cache 1h · 57:12 left` (green when healthy, yellow near expiry, red when
   the window has expired).

## Options

- Target a non-default settings file: `--settings /path/to/settings.json`
  (e.g. a project's `.claude/settings.json`).
- Undo: `--restore` puts back the status line that was there before install.

## Notes

- The status line is what feeds the model/effort guard hook its "current model
  and effort" reading, so installing it also makes that guard fully effective.
- Re-running the installer is safe and idempotent — it will not double-wrap.
