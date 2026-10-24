"""One run: settle chats → annotate the owner's messages → fold in the owner's edits → rewrite → render me.md.

Judgment (what is a preference, how to merge them) is always the LLM's. Code only does plumbing: cursors,
deduplication, diffs, ordering by priority and the word cap."""

from __future__ import annotations

import difflib
import json
import time
from pathlib import Path

from .config import Config
from .llm import LLM, LLMStopped, obj
from .store import Store, now
from .transcripts import Message, discover_chats, read_messages

STR = {"type": "string"}
SECTIONS = {
    "communication": "Communication",
    "writing": "Writing",
    "code_and_prs": "Code and pull requests",
    "working_with_ai": "Working with AI",
    "other": "Other",
}
SOURCE_RANK = {"owner": 3, "observed": 2, "imported": 1}
HEADER = ("# How I want you to work\n\n"
          "_Maintained by who-am-ai from my own messages. My edits to this file win over anything inferred._\n")

ANNOTATE_SYSTEM = """\
You read messages the owner wrote to an AI assistant (Claude Code), in order. Find the PREFERENCES they reveal: \
how they want the AI to work, communicate, write, review, code and handle their requests — durable things they would \
want every assistant to know without being told again.

- Short reactions often reveal a preference ("shorten this", "no, in English", "stop doing X", "don't paraphrase \
my request"). Use the CONTEXT (the end of the reply they react to) to understand what they mean. The context is \
never a preference by itself.
- A request specific to the task at hand ("run the tests", "open the PR") is not a preference, unless it reveals \
a recurring way they want things done.
- The bar is high. Every line you return can end up in the instructions of ALL their future sessions, in other \
repos and other kinds of work. Before you return one, ask: would it change what an assistant does in an \
unrelated task next month, and is it what they would want there? If it only makes sense inside this ticket, \
document, repo or conversation (a marker format for one comment, the scope of one runbook, a detail of one \
page), it is not a preference: return nothing for it. When a narrow request reveals a general principle, return \
the general principle, not the detail; when you are unsure, return nothing.
- Text they pasted (marked as omitted) is not their preference.
- `preference`: one imperative line addressed to the assistant, in English (e.g. "Never echo or paraphrase my \
request in the deliverable."). `quote`: their literal words, in the original language. `topic`: one of \
communication, writing, code_and_prs, working_with_ai, other.
- Return an empty list when there is nothing durable."""

ANNOTATE_SCHEMA = obj({"observations": {"type": "array", "items": obj({
    "preference": STR, "quote": STR, "topic": {"type": "string", "enum": list(SECTIONS)},
})}})

REWRITE_SYSTEM = """\
You maintain the owner's profile: the short list of instructions every AI assistant loads before working with \
them. You receive the CURRENT ITEMS (with how strongly they are supported) and NEW OBSERVATIONS. Return the \
complete new item list.

- Merge duplicates and near-duplicates into one item. When observations conflict, the most recent one wins.
- Observations of kind `owner` come from the owner editing the profile themselves: they have the final word and \
are never overridden or dropped. An owner observation starting with "Do not" means they removed that line: drop \
or rewrite the items it refers to accordingly.
- Each item is ONE rule: one imperative sentence of at most ~20 words, specific and actionable, in English. \
Split compound items into separate rules; merge only items that say the same thing. Short items matter: the \
profile has a word cap, and long items push other rules out.
- Add a short `example` (before → after) only when the rule alone is likely to be misread; otherwise null.
- `keeps`: the id of the current item this one continues (e.g. "I12"), or null if it is new.
- `supported_by`: the ids of the NEW observations behind it (e.g. ["O45"]); [] if none.
- Admission bar: every item is loaded into every future session, in any repo and any kind of work, and each \
one costs attention for everything else. Include an item only when it is general (it changes what an assistant \
does across unrelated tasks) AND it is durable (they state it as a standing rule, or it recurs across chats). \
An observation tied to one ticket, document, repo or conversation stays out, even if worded as a rule; keep \
only the general principle behind it, if there is one. When in doubt, leave it out: a missing rule costs one \
reminder, a wrong rule misleads every session.
- Current items already passed this bar: keep them. Drop or generalize a current item only when it is clearly \
tied to one ticket, document, repo or conversation, and never when its source is `owner`. Lack of repetition \
is not a reason to drop a current item. Every current item you drop must be named in `summary`.
- Do not invent preferences. Keep every current item that is still valid, even if not mentioned now.
- `summary`: one sentence on what changed."""

REWRITE_SCHEMA = obj({
    "items": {"type": "array", "items": obj({
        "section": {"type": "string", "enum": list(SECTIONS)},
        "text": STR,
        "example": {"anyOf": [STR, {"type": "null"}]},
        "keeps": {"anyOf": [STR, {"type": "null"}]},
        "supported_by": {"type": "array", "items": STR},
    })},
    "summary": STR,
})


class Runner:
    def __init__(self, cfg: Config, store: Store, llm: LLM | None = None):
        self.cfg = cfg
        self.store = store
        self.llm = llm or LLM(store, float(cfg["cap_usd"]))

    # ---------------------------------------------------------------- run

    def run(self, now_ts: float | None = None) -> dict:
        stats = {"chunks": 0, "observations": 0, "owner_edits": 0, "rewrote": False, "stopped": None}
        try:
            stats["owner_edits"] = self.fold_owner_edits()
            for path, title in self.settled_files(now_ts if now_ts is not None else time.time()):
                c, o = self.annotate_file(path, title)
                stats["chunks"] += c
                stats["observations"] += o
            if self.store.pending_observations():
                self.rewrite()
                stats["rewrote"] = True
        except LLMStopped as exc:
            stats["stopped"] = str(exc)
        self.store.log_run(json.dumps(stats))
        return stats

    # ---------------------------------------------------------------- reading

    def settled_files(self, now_ts: float) -> list[tuple[Path, str | None]]:
        """Transcript files with new complete lines that have not changed for `settle_minutes`."""
        projects = self.cfg.path("projects_root")
        chats = discover_chats(projects, self.cfg.path("app_sessions_root"))
        title_of = {stem: c["title"] for c in chats for stem in c["files"]}
        excluded = [x.strip().lower() for x in self.cfg["excluded_chats"] if x.strip()]
        settle = float(self.cfg["settle_minutes"]) * 60
        out = []
        for path in sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime) if projects.is_dir() else []:
            title = title_of.get(path.stem)
            if title and any(title.strip().lower().startswith(x) for x in excluded):
                continue
            st = path.stat()
            row = self.store.one("SELECT offset FROM files WHERE stem = ?", (path.stem,))
            offset = row["offset"] if row else 0
            if st.st_size > offset and now_ts - st.st_mtime >= settle:
                out.append((path, title))
        return out

    def annotate_file(self, path: Path, title: str | None) -> tuple[int, int]:
        row = self.store.one("SELECT offset FROM files WHERE stem = ?", (path.stem,))
        offset = row["offset"] if row else 0
        messages, end = read_messages(path, offset)
        # When reading from the middle of a file, the reply the first new message reacts to may be earlier.
        context_source = read_messages(path, 0)[0] if offset and self.cfg["read_mode"] == "mine+agent" else messages
        chunks = self._chunks(messages, context_source)
        chunks_done = observations = 0
        for chunk in chunks:
            text = "\n\n".join(chunk["lines"])
            out = self.llm.structured("annotate", self.cfg["models"]["annotate"], ANNOTATE_SYSTEM,
                                      f"CHAT: {title or path.stem}\n\n{text}", ANNOTATE_SCHEMA)
            for o in out["observations"]:
                self.store.add_observation("observed", o["preference"], o["quote"], o["topic"], title or path.stem,
                                           chunk["ts"])
                observations += 1
            for mid in chunk["ids"]:
                self.store.x("INSERT OR IGNORE INTO seen (message_id) VALUES (?)", (mid,))
            chunks_done += 1
        # The cursor only moves after every chunk of the file was annotated (an LLM failure retries the file).
        self.store.x("""INSERT INTO files (stem, path, offset, mtime) VALUES (?, ?, ?, ?)
                        ON CONFLICT(stem) DO UPDATE SET offset = excluded.offset, mtime = excluded.mtime""",
                     (path.stem, str(path), end, path.stat().st_mtime))
        return chunks_done, observations

    def _chunks(self, messages: list[Message], context_source: list[Message]) -> list[dict]:
        seen = {r["message_id"] for r in self.store.q("SELECT message_id FROM seen")}
        reply_before: dict[str, Message] = {}
        last_reply = None
        for m in context_source:
            if m.role == "assistant":
                last_reply = m
            else:
                if last_reply is not None:
                    reply_before[m.id] = last_reply
                last_reply = None
        limit, keep = int(self.cfg["chunk_chars"]), int(self.cfg["context_chars"])
        chunks, current, size = [], {"lines": [], "ids": [], "ts": None}, 0
        for m in messages:
            if m.role != "user" or m.id in seen:
                continue
            line = f"[{m.at:%Y-%m-%d %H:%M}] OWNER: {m.text}"
            reply = reply_before.get(m.id) if self.cfg["read_mode"] == "mine+agent" else None
            if reply is not None:
                tail = reply.text if len(reply.text) <= keep else "…" + reply.text[-keep:]
                line = f"CONTEXT (end of the assistant reply they react to): {tail}\n{line}"
            if current["lines"] and size + len(line) > limit:
                chunks.append(current)
                current, size = {"lines": [], "ids": [], "ts": None}, 0
            current["lines"].append(line)
            current["ids"].append(m.id)
            current["ts"] = m.at.isoformat(timespec="seconds")
            size += len(line)
        if current["lines"]:
            chunks.append(current)
        return chunks

    # ---------------------------------------------------------------- owner edits

    def fold_owner_edits(self) -> int:
        """Lines the owner added or changed in me.md become owner observations; removed lines become
        "Do not ..." owner observations. Plain diff (plumbing); what they mean is left to the rewrite."""
        path = self.cfg.path("profile_path")
        last = self.store.last_version()
        if last is None or not path.exists():
            return 0
        current = path.read_text(encoding="utf-8")
        if current == last:
            return 0
        old, new = _lines(last), _lines(current)
        added, removed = [], []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
            # A changed line counts as "removed the old one" plus "wrote the new one"; the rewrite decides
            # what that means (the owner's newest words win).
            if tag in ("replace", "insert"):
                added.extend(new[j1:j2])
            if tag in ("replace", "delete"):
                removed.extend(old[i1:i2])
        ts = now()
        for line in added:
            self.store.add_observation("owner", line, line, None, "me.md (edited by the owner)", ts)
        for line in removed:
            self.store.add_observation("owner", f"Do not: {line}", line, None, "me.md (edited by the owner)", ts)
        # The edited file becomes the baseline, so the same edit is not read twice if the rewrite fails.
        self.store.x("INSERT INTO versions (written_at, content) VALUES (?, ?)", (ts, current))
        return len(added) + len(removed)

    # ---------------------------------------------------------------- rewrite

    def rewrite(self) -> None:
        items = self.store.active_items()
        pending = self.store.pending_observations()
        obs_by_id = {r["id"]: r for r in self.store.q("SELECT * FROM observations")}
        lines = ["CURRENT ITEMS"] + [
            f"I{it['id']} ({it['section']}; source {it['source']}; {it['support']} observations in {it['chats']} chats; "
            f"last {it['last_seen'][:10]}): {it['text']}" + (f" | example: {it['example']}" if it["example"] else "")
            for it in items] + ["", "NEW OBSERVATIONS"] + [
            f"O{o['id']} ({o['kind']}; {o['chat'] or '?'}; {o['ts'][:10]}): {o['preference']}"
            + (f' | quote: "{o["quote"]}"' if o["quote"] and o["quote"] != o["preference"] else "")
            for o in pending]
        out = self.llm.structured("rewrite", self.cfg["models"]["rewrite"], REWRITE_SYSTEM, "\n".join(lines),
                                  REWRITE_SCHEMA)
        old = {f"I{it['id']}": it for it in items}
        new_items = []
        for it in out["items"]:
            prev = old.get(it["keeps"] or "")
            ids = set(prev["observation_ids"]) if prev else set()
            ids |= {int(s[1:]) for s in it["supported_by"] if s.startswith("O") and s[1:].isdigit()
                    and int(s[1:]) in obs_by_id}
            if not ids:
                continue  # an item nothing supports is an invention
            new_items.append({**it, "observation_ids": sorted(ids), "prev": prev})
        # The owner has the final word: an owner observation nobody kept becomes its own item.
        covered = {i for it in new_items for i in it["observation_ids"]}
        for o in pending:
            if o["kind"] == "owner" and o["id"] not in covered and not o["preference"].startswith("Do not:"):
                new_items.append({"section": o["topic"] or "other", "text": o["preference"], "example": None,
                                  "keeps": None, "observation_ids": [o["id"]], "prev": None})
        self._save(items, new_items, out["summary"], obs_by_id)
        self.store.x("UPDATE observations SET consumed = 1 WHERE consumed = 0")
        self.render()

    def _save(self, old_items: list[dict], new_items: list[dict], summary: str, obs_by_id: dict) -> None:
        kept = {it["prev"]["id"] for it in new_items if it["prev"]}
        added = [it["text"] for it in new_items if not it["prev"]]
        changed = [{"from": it["prev"]["text"], "to": it["text"]} for it in new_items
                   if it["prev"] and it["prev"]["text"] != it["text"]]
        removed = [it["text"] for it in old_items if it["id"] not in kept]
        self.store.x("UPDATE items SET active = 0")
        for it in new_items:
            obs = [obs_by_id[i] for i in it["observation_ids"]]
            source = max((o["kind"] for o in obs), key=lambda k: SOURCE_RANK.get(k, 0))
            self.store.x("""INSERT INTO items (section, text, example, source, observation_ids, support, chats, last_seen)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (it["section"], it["text"], it["example"], source, json.dumps(it["observation_ids"]),
                          len(obs), len({o["chat"] for o in obs}), max(o["ts"] for o in obs)))
        self.store.x("INSERT INTO changelog (ts, summary, added, removed, changed) VALUES (?, ?, ?, ?, ?)",
                     (now(), summary, json.dumps(added), json.dumps(removed), json.dumps(changed)))

    # ---------------------------------------------------------------- render

    def render(self) -> str:
        """me.md from the active items, by section, under the word cap. Priority when it does not all fit: what
        the owner wrote, then what is supported across more observations and chats, then the most recent."""
        items = self.store.active_items()
        items.sort(key=lambda it: (SOURCE_RANK.get(it["source"], 0), it["support"], it["chats"], it["last_seen"]),
                   reverse=True)
        budget = int(self.cfg["max_words"]) - len(HEADER.split())
        chosen, used = [], 0
        for it in items:
            words = len(it["text"].split()) + len((it["example"] or "").split())
            if used + words <= budget:
                chosen.append(it)
                used += words
        blocks = [HEADER.rstrip()]
        for key, title in SECTIONS.items():
            section = [it for it in chosen if it["section"] == key]
            if not section:
                continue
            bullets = [f"- {it['text']}" + (f"\n  - Example: {it['example']}" if it["example"] else "") for it in section]
            blocks.append(f"## {title}\n\n" + "\n".join(bullets))
        content = "\n\n".join(blocks) + "\n"
        path = self.cfg.path("profile_path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.store.x("INSERT INTO versions (written_at, content) VALUES (?, ?)", (now(), content))
        return content


def _lines(text: str) -> list[str]:
    """Profile lines that carry content (bullets and examples), without the markers."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("_Maintained by"):
            continue
        content = line.lstrip("-* ").removeprefix("Example: ").strip()
        if content:  # an emptied bullet ("- ") is a deletion, not a new line
            out.append(content)
    return out
