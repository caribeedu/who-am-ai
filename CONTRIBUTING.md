# Contributing

Thanks for your interest in who-am-ai. Before proposing a change, read [docs/decisions.md](docs/decisions.md): the project is deliberately small, and changes that widen its scope will not be merged.

## Principles in short

- Judgment is always the LLM's. Code only does plumbing: cursors, diffs, caps and rendering.
- No fallback without an LLM.
- No approval gates. The user corrects after the fact and has the last word, so the user's edits to `me.md` always win.
- The profile stays short: every item is loaded into every session, so each one must be general and durable.

## Development setup

You need Python 3.13 or 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run python -m pytest -q
```

The tests use a fake LLM and a temporary home, so they need no API key and no network.

## Pull requests

- Keep a change focused, and add or update tests. Bug fixes should come with a regression test.
- If you change a prompt, check the effect on real chats and describe it in the pull request.
- Record a new design decision in [docs/decisions.md](docs/decisions.md).
- Write code, comments and documentation in English.

## Reporting problems

Open an issue with what you did, what you expected and what happened. For security problems, follow [SECURITY.md](SECURITY.md) instead.
