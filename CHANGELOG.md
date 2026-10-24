# Changelog

## v1.1.0

- Runs on Windows and Linux as well as macOS. `whoamai install` schedules the daily run with a Task Scheduler task (Windows) or a systemd user timer (Linux), both catching up after sleep.
- The Claude desktop app's session records are found in their per-platform folder.
- Text files (the profile, `CLAUDE.md`, settings) are always read and written as UTF-8, and the console output too, so non-ASCII text no longer breaks on Windows.
- `whoamai install` and `uninstall` keep the line endings of `~/.claude/CLAUDE.md`, and a failed install leaves nothing behind that looks scheduled.

## v1.0.0

First release.

- Reads your messages in Claude Code, notes the preferences they reveal and keeps a short profile (`~/.claude/me.md`) that Claude Code loads in every conversation.
- Your edits to the profile win over anything inferred; every change is recorded and explained (`whoamai log`).
- A word cap, a monthly spend cap and a daily scheduled run (macOS LaunchAgent).
