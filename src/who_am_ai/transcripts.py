"""Claude Code transcripts: which chats exist (with their titles) and the messages in them.

A chat in the desktop app has a fixed id and may span several
session files (resuming copies the history, with the same message ids, into a new file); terminal chats are
the chain of files that start with the same message."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REMINDERS = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_PASTED = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.DOTALL)
_SKIP_PREFIXES = ("<task-notification>", "[Request interrupted", "<command-", "<local-command-")


@dataclass
class Message:
    id: str
    role: str        # "user" (the owner) | "assistant"
    text: str
    at: datetime


def _ts(value: str | None) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)


def clean_owner_text(text: str) -> str:
    """The owner's own words only: no harness reminders, and pasted material replaced by a marker (what they
    pasted — logs, documents, other people's messages — is not their preference)."""
    text = _REMINDERS.sub("", text)
    text = _PASTED.sub("[pasted content omitted]", text)
    return text.strip()


def read_messages(path: Path, offset: int = 0) -> tuple[list[Message], int]:
    """Complete lines from `offset`. Returns the messages and the new offset (only up to the last newline)."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    last = data.rfind(b"\n")
    if last < 0:
        return [], offset
    data = data[: last + 1]
    out: list[Message] = []
    by_reply: dict[str, Message] = {}
    for raw in data.splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = line.get("type")
        if kind not in ("user", "assistant") or line.get("isSidechain") or line.get("isMeta") \
                or line.get("isCompactSummary"):
            continue  # compaction summaries are written by the AI but stored as user lines
        content = (line.get("message") or {}).get("content")
        at = _ts(line.get("timestamp"))
        if kind == "user":
            if isinstance(content, str):
                text = content
            else:  # tool results are user lines too; only text blocks are the owner's words
                text = "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
            text = clean_owner_text(text)
            if text and not text.startswith(_SKIP_PREFIXES):
                out.append(Message(line.get("uuid") or f"{path.stem}:{len(out)}", "user", text, at))
            continue
        parts = [b["text"].strip() for b in content or [] if b.get("type") == "text" and b.get("text")]
        if not parts:
            continue
        mid = (line.get("message") or {}).get("id") or line.get("uuid")
        if mid in by_reply:  # blocks of one reply arrive in separate lines
            by_reply[mid].text += "\n" + "\n".join(parts)
        else:
            m = Message(mid, "assistant", "\n".join(parts), at)
            by_reply[mid] = m
            out.append(m)
    return out, offset + len(data)


def discover_chats(projects: Path, app_root: Path) -> list[dict]:
    """[{id, title, files: [stems]}] for every chat (desktop-app registry first, terminal chains otherwise)."""
    files = {p.stem: p for p in projects.glob("*/*.jsonl")} if projects.is_dir() else {}
    chats: dict[str, dict] = {}
    covered: set[str] = set()
    if app_root.is_dir():
        for f in app_root.glob("*/*/*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            stems = [s for s in [d.get("cliSessionId"), *(d.get("priorCliSessionIds") or [])] if s and s in files]
            if stems and d.get("sessionId"):
                chats[d["sessionId"]] = {"id": d["sessionId"], "title": d.get("title") or "(untitled)", "files": stems}
                covered |= set(stems)
    chains: dict[str, list[tuple[float, str, str | None]]] = {}
    for stem, path in files.items():
        if stem in covered:
            continue
        first, title = None, None
        try:
            with open(path, "rb") as f:
                for raw in f:
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") == "custom-title":
                        title = d.get("customTitle") or title
                    elif d.get("type") in ("user", "assistant") and first is None:
                        first = d.get("uuid")
            chains.setdefault(first or stem, []).append((path.stat().st_mtime, stem, title))
        except OSError:
            continue
    for members in chains.values():
        members.sort()
        title = next((t for _, _, t in reversed(members) if t), None) or "(untitled)"
        chats[f"cli-{members[0][1]}"] = {"id": f"cli-{members[0][1]}", "title": title, "files": [s for _, s, _ in members]}
    return list(chats.values())
