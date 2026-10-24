"""Settings, stored as JSON in the tool's home (`~/.who-am-ai` unless WHO_AM_AI_HOME says otherwise)."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

SONNET = "claude-sonnet-5-5"


def _app_sessions_root() -> str:
    """Where the Claude desktop app keeps its session records (they carry the chat titles)."""
    if sys.platform == "darwin":
        return "~/Library/Application Support/Claude/claude-code-sessions"
    if sys.platform.startswith("win"):
        return str(Path(os.environ.get("APPDATA") or "~/AppData/Roaming") / "Claude" / "claude-code-sessions")
    return str(Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config") / "Claude" / "claude-code-sessions")


DEFAULTS: dict[str, Any] = {
    "run_at": "12:00",            # daily time (HH:MM) the scheduler runs `whoamai run`; asleep then = runs on wake
    "settle_minutes": 5,          # a chat is processed after this long without new lines
    "cap_usd": 10.0,              # monthly LLM spend cap; at the cap the run stops (no fallback)
    "read_mode": "mine+agent",    # "mine" | "mine+agent" (the reply each message reacts to, as context only)
    "excluded_chats": [],         # chat title prefixes left out
    "models": {"annotate": SONNET, "rewrite": SONNET},
    "max_words": 600,             # profile size cap
    "context_chars": 1500,        # how much of the end of the assistant reply goes as context
    "chunk_chars": 60000,         # owner text per annotate call
    "profile_path": "~/.claude/me.md",
    "claude_md": "~/.claude/CLAUDE.md",
    "projects_root": "~/.claude/projects",
    "app_sessions_root": _app_sessions_root(),
}
READ_MODES = ("mine", "mine+agent")


def home() -> Path:
    return Path(os.environ.get("WHO_AM_AI_HOME", "~/.who-am-ai")).expanduser()


class Config:
    def __init__(self, root: Path | None = None):
        self.root = root or home()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "config.json"
        self.data = copy.deepcopy(DEFAULTS)
        if self.file.exists():
            stored = json.loads(self.file.read_text(encoding="utf-8"))
            for k, v in stored.items():
                if k not in self.data:
                    continue  # a setting that no longer exists
                self.data[k] = {**self.data[k], **v} if isinstance(self.data.get(k), dict) and isinstance(v, dict) else v

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting: {key}")
        self.data[key] = value
        self.file.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")

    def path(self, key: str) -> Path:
        return Path(os.path.expanduser(self.data[key]))
