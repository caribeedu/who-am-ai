# Design

> Learns how its owner wants AI assistants to work, from the owner's own messages, and keeps a short profile that the assistant loads in every conversation. The goal: **let your AI know how you like things done, without telling it again.**

## What it does

When a conversation settles, `whoamai run` reads only the owner's new messages, notes the preferences they reveal, and rewrites the profile file (`me.md`), which the assistant loads in every conversation.

## Supported today

The design is independent of the assistant and of the platform; the current implementation supports one of each.

| | Supported | How |
|---|---|---|
| **Assistant** | Claude Code | Reads its transcripts (`~/.claude/projects`, and the desktop app's session records for chat titles). The profile is `~/.claude/me.md`, imported by `~/.claude/CLAUDE.md` with `@~/.claude/me.md`. |
| **Platform** | macOS | A LaunchAgent runs `whoamai run` once a day; the API key is stored in the Keychain. On other systems you can run `whoamai run` yourself, and the key is stored with whatever backend `keyring` provides. |

![How who-am-ai works](../assets/how-it-works.svg)

## Pieces

| Piece | What | Judgment? |
|---|---|---|
| **Settle** | A transcript file is processed when it has new complete lines and has not changed for `settle_minutes` (default 5). The cursor is a byte offset per file. | No (plumbing) |
| **Read** | Owner messages only. Dropped: compaction summaries, sub-agent (sidechain) lines, meta lines, task notifications, interruptions and system reminders. Pasted blocks (`<pasted_content>`) are replaced by a marker, because pasted text is not the owner's preference. Messages are deduplicated by id across files (resuming a chat copies its history into a new file). | No |
| **Read mode** | `mine`: owner messages only. `mine+agent` (default): each owner message also carries the tail of the assistant reply it reacts to, as **context only**. Assistant text never becomes a preference. | No |
| **Annotate** | One LLM call per settled chunk. Output: observations, each with the owner's literal quote, a one-line preference and a topic. Task-specific requests are not preferences. | **LLM** |
| **Owner edits** | Before rewriting, the current `me.md` is diffed against the last version this tool wrote. Lines the owner added or changed become `owner` observations; removed lines become `owner` "do not" observations. Owner observations have the highest authority and are never overridden by inferences. | Diff: no. Meaning: the rewrite LLM. |
| **Rewrite** | One LLM call per run, only when there are new observations. Input: the current profile items (with support and source) and the new observations. Output: the full new item list, each item citing the observations behind it. Consolidates, deduplicates, the most recent preference wins, and the owner wins over everything. | **LLM** |
| **Render** | Code renders the items as Markdown, by section, one imperative line each (with a short before/after example only where a rule alone fails). It enforces the word cap (`max_words`, default 600) by priority: owner items, then by support (how many observations, across how many chats), then by recency. Items that do not fit stay in the database and come back if they gain support. | No |
| **Changelog and history** | Every rewrite stores the rendered version and what was added, removed or changed, with a reason. `whoamai log` shows it. | No |

## Runtime

- `whoamai run` is a one-shot command scheduled (on macOS, by a LaunchAgent) once a day at `run_at` (default 12:00). If the machine sleeps at that time, the run starts on wake; if it is off, that day is skipped and the next run reads everything since the last one. There is no daemon, no Docker and no web interface.
- State lives in `~/.who-am-ai/` (SQLite and `config.json`).
- Claude is used through the Anthropic API. The key lives in the system keychain (macOS Keychain today).
- A monthly spend cap (`cap_usd`, default 10) stops runs from calling the LLM, and says so in `whoamai log` and `whoamai status`. **There is no fallback without an LLM.**

## CLI

| Command | What |
|---|---|
| `whoamai run` | Process settled chats; rewrite the profile if needed. |
| `whoamai status` | Cursors, last run, spend this month, cap, settings. |
| `whoamai log [-n N]` | Profile changelog. |
| `whoamai config` | Show settings. |
| `whoamai config time 12:00` | Change the daily run time (updates the schedule). |
| `whoamai config cap 10` | Monthly spend cap in USD. |
| `whoamai config read-mode mine\|mine+agent` | What is read. |
| `whoamai config exclude "<title prefix>"` / `include` | Chats left out, by title prefix. |
| `whoamai config models <annotate> <rewrite>` | Models per step. |
| `whoamai config max-words 600` | Profile size cap. |
| `whoamai config settle 5m` | How long a chat must be idle before it is processed. |
| `whoamai install` / `uninstall` | The schedule (LaunchAgent) and the `@~/.claude/me.md` import line in `~/.claude/CLAUDE.md`. |
| `whoamai key` | Store the Anthropic API key in the Keychain. |
| `whoamai import <file.json>` | One-time import of preferences from elsewhere: a JSON list of `{preference, quote, chat, ts, kind?}`. |
