# Decisions

## Why this project exists

who-am-ai is a leaner alternative to [Pythia](https://github.com/caribeedu/pythia), and grew out of measuring it. Pythia extracted claims from many sources (chats, pull requests, Slack, Confluence), kept them consistent over time and served them on demand. It was costly to maintain, and in testing it did not beat a plain text search over the raw sources on direct questions (see its [measurement results](https://github.com/caribeedu/pythia/blob/main/docs/measuring-value.md)). What clearly helped, at almost no cost, was something much smaller: the user's own preferences about how an assistant should work, learned from their own messages and delivered in every conversation.

who-am-ai does only that. It trades breadth for a result that can be verified: the user repeats themselves less, and can read, edit and veto everything the tool believes about them.

## Decisions

| # | Decision |
|---|---|
| D1 | **Scope.** A small tool with one job. Out of scope: integrations with other sources, backfills, time travel, a knowledge graph, verdicts, narratives, usage rules and a raw store of conversations. |
| D2 | **Execution.** A one-shot command (`whoamai run`), scheduled once a day (by the platform's own scheduler: a macOS LaunchAgent, a Windows Task Scheduler task or a Linux systemd user timer) at a fixed time (`whoamai config time HH:MM`, default 12:00). No daemon, no Docker, no web. A fixed time runs on wake if the machine slept through it, and is skipped if the machine was off. Nothing is lost, because each run reads everything since the last one and the assistant keeps conversations for a while (Claude Code keeps them for at least 30 days). An interval timer would not work, since the scheduler (launchd) restarts it at every boot. Every platform's scheduler must therefore support "run at a fixed time, and as soon as possible if that time was missed". A preference stated today reaches the profile the next day. |
| D3 | **Corrections.** The user edits `me.md` anywhere. Before each rewrite the tool diffs the file against the last version it wrote; the user's changes become `owner` observations with the highest authority, never overridden by inferences. Every version is kept. |
| D4 | **What is read.** All conversations of the supported assistant (today, Claude Code). An exclusion list by title prefix starts empty. Only the user's messages are read, so assistant text about other data never becomes a preference. |
| D5 | **Profile shape.** `me.md` has up to ~600 words, in sections by topic, one imperative line per item, with a before/after example only where a rule alone fails. Over the cap, priority is: what the user wrote, then what they repeated, then the most recent. One-off observations leave the file first but stay in the ledger. |
| D6 | **Admission bar.** A preference enters the profile only when it is general (it changes what an assistant does in unrelated tasks) and durable (a standing rule, or it recurs across chats). Details tied to one ticket, document, repository or conversation stay out; only the general principle behind them may enter. Current items are dropped only when clearly tied to one context, never for lack of repetition, and every drop is named in the changelog. Each item loads into every session, so one-off details snowball; but applying the strict bar to current items also dropped general rules, so they get the softer rule. |
| D7 | **Visibility.** A changelog of every rewrite, with what was added, removed or changed and why, read with `whoamai log`. |
| D8 | **Models.** Sonnet for annotating and for rewriting, configurable per step. A typical user spends a few dollars per month. |
| D9 | **Settings.** Run time, monthly cap, read mode (`mine` or `mine+agent`), exclusions, models, size cap and settle time are changeable from the CLI. |
| D10 | **Language.** Everything the tool writes is in English. Quotes keep the language of the original message. |
| D11 | **Rules inherited from the design philosophy.** Judgment is always the LLM's (code only does plumbing: cursors, diffs, caps, rendering). No fallback without an LLM. No approval gates: the user corrects after the fact and has the last word. |
