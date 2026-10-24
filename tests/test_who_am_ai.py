import json
import os
import time
from pathlib import Path

import pytest

from who_am_ai import cli, scheduler
from who_am_ai.config import Config
from who_am_ai.llm import LLMStopped
from who_am_ai.pipeline import Runner, _lines
from who_am_ai.store import Store
from who_am_ai.transcripts import read_messages


class FakeLLM:
    """Records calls; `handlers[step]` answers them."""

    def __init__(self, store, cap=10.0):
        self.store, self.cap, self.calls, self.handlers = store, cap, [], {}

    def structured(self, step, model, system, user, schema, max_tokens=16000):
        if self.store.month_spend() >= self.cap:
            raise LLMStopped("monthly cap reached")
        self.calls.append((step, user))
        self.store.x("INSERT INTO llm_calls (ts, step, model, cost_usd, ok) VALUES (datetime('now'), ?, ?, 0.01, 1)",
                     (step, model))
        return self.handlers[step](user)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WHO_AM_AI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(scheduler, "LAUNCH_AGENT", tmp_path / "LaunchAgents" / "local.who-am-ai.plist")
    monkeypatch.setattr(scheduler, "SYSTEMD_DIR", tmp_path / "systemd")
    cfg = Config(tmp_path / "home")
    for key, sub in (("projects_root", "projects"), ("app_sessions_root", "app"), ("profile_path", "claude/me.md"),
                     ("claude_md", "claude/CLAUDE.md")):
        cfg.set(key, str(tmp_path / sub))
    (tmp_path / "projects" / "-repo").mkdir(parents=True)
    store = Store(cfg.root / "state.db")
    llm = FakeLLM(store)
    return cfg, store, llm, tmp_path


def fake_scheduler_tools(monkeypatch, platform, returncode=0):
    """Pretend to be `platform` and record the commands (launchctl, schtasks, systemctl) instead of running them."""
    calls = []
    monkeypatch.setattr(scheduler, "PLATFORM", platform)
    monkeypatch.setattr(scheduler.subprocess, "run", lambda args, **kw: calls.append(args) or
                        type("R", (), {"returncode": returncode, "stdout": "", "stderr": ""})())
    return calls


def line(kind, uid, text=None, content=None, ts="2026-10-01T10:00:00Z", **extra):
    msg = {"content": content if content is not None else text}
    if kind == "assistant":
        msg = {"id": uid, "content": [{"type": "text", "text": text}]}
    return {"type": kind, "uuid": uid, "timestamp": ts, "message": msg, **extra}


def write(path: Path, *lines, age_s=3600):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    t = time.time() - age_s
    os.utime(path, (t, t))


def annotate_all(user):
    """Every OWNER line about English becomes a preference."""
    obs = []
    for chunk in user.split("OWNER: ")[1:]:
        said = chunk.split("\n")[0]
        if "english" in said.lower():
            obs.append({"preference": "Write everything in English.", "quote": said, "topic": "writing"})
    return {"observations": obs}


def rewrite_merge(user):
    """Keeps every current item and adds one item per new observation."""
    items = []
    for row in user.split("NEW OBSERVATIONS")[0].splitlines():
        if row.startswith("I"):
            iid, text = row.split(" ")[0], row.split("): ", 1)[1].split(" | example")[0]
            items.append({"section": "writing", "text": text, "example": None, "keeps": iid, "supported_by": []})
    for row in user.split("NEW OBSERVATIONS")[1].splitlines():
        if row.startswith("O"):
            oid, text = row.split(" ")[0], row.split("): ", 1)[1].split(" | quote")[0]
            items.append({"section": "writing", "text": text, "example": None, "keeps": None, "supported_by": [oid]})
    return {"items": items, "summary": "merged"}


def test_reader_keeps_only_the_owners_own_words(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p,
          line("user", "u1", "keep it short <system-reminder>noise</system-reminder>"),
          line("user", "u2", content=[{"type": "tool_result", "content": "tool output"}]),
          line("user", "u3", "This session is being continued...", isCompactSummary=True),
          line("user", "u4", "<task-notification>done</task-notification>"),
          line("user", "u5", "sidechain", isSidechain=True),
          line("user", "u6", 'look: <pasted_content id="1">someone else\'s text</pasted_content id="1"> no adverbs'),
          line("assistant", "a1", "Here it is, as you asked."))
    msgs, end = read_messages(p)
    assert [(m.role, m.text) for m in msgs] == [
        ("user", "keep it short"), ("user", "look: [pasted content omitted] no adverbs"),
        ("assistant", "Here it is, as you asked.")]
    assert end == p.stat().st_size


def test_settled_chat_is_annotated_once_with_the_reply_as_context(env):
    cfg, store, llm, tmp = env
    llm.handlers.update(annotate=annotate_all, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl",
          line("user", "u1", "write the doc"),
          line("assistant", "a1", "Done. Wrote it in Spanish as you asked."),
          line("user", "u2", "no, english please"))
    write(tmp / "projects/-repo/s2.jsonl", line("user", "u9", "too recent"), age_s=10)  # not settled
    stats = Runner(cfg, store, llm).run()
    assert stats["chunks"] == 1 and stats["observations"] == 1 and stats["rewrote"]
    sent = llm.calls[0][1]
    assert "CONTEXT (end of the assistant reply they react to): Done. Wrote it in Spanish" in sent
    assert "too recent" not in sent
    me = cfg.path("profile_path").read_text()
    assert "- Write everything in English." in me
    # Second run: nothing new to read, nothing to rewrite.
    llm.calls.clear()
    assert Runner(cfg, store, llm).run()["chunks"] == 0 and llm.calls == []
    # A resumed chat copies its history (same ids) into a new file: those messages are not annotated again.
    write(tmp / "projects/-repo/s3.jsonl", line("user", "u1", "write the doc"), line("user", "u2", "no, english please"),
          line("user", "u3", "and keep it short"))
    Runner(cfg, store, llm).run()
    assert "write the doc" not in llm.calls[0][1] and "keep it short" in llm.calls[0][1]


def test_read_mode_mine_sends_no_assistant_text(env):
    cfg, store, llm, tmp = env
    cfg.set("read_mode", "mine")
    llm.handlers.update(annotate=lambda u: {"observations": []}, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl", line("assistant", "a1", "SECRET REPLY"), line("user", "u1", "shorter"))
    Runner(cfg, store, llm).run()
    assert "SECRET REPLY" not in llm.calls[0][1] and "CONTEXT" not in llm.calls[0][1]


def test_excluded_chat_titles_are_skipped(env):
    cfg, store, llm, tmp = env
    cfg.set("excluded_chats", ["Private"])
    llm.handlers.update(annotate=lambda u: {"observations": []}, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "hello"), {"type": "custom-title", "customTitle": "Private stuff"})
    assert Runner(cfg, store, llm).run()["chunks"] == 0


def test_owner_edits_win_and_are_never_dropped(env):
    cfg, store, llm, tmp = env
    llm.handlers.update(annotate=annotate_all, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "in english"))
    Runner(cfg, store, llm).run()
    me = cfg.path("profile_path")
    edited = me.read_text().replace("- Write everything in English.", "- Write code in English; talk to me in Spanish.")
    me.write_text(edited + "- Never add task numbers to code comments.\n")
    llm.handlers["rewrite"] = lambda u: {"items": [], "summary": "dropped everything"}  # a bad rewrite
    stats = Runner(cfg, store, llm).run()
    assert stats["owner_edits"] == 3  # changed line = removed old + wrote new; plus one new line
    owner = [r["preference"] for r in store.q("SELECT preference FROM observations WHERE kind = 'owner'")]
    assert "Write code in English; talk to me in Spanish." in owner and "Never add task numbers to code comments." in owner
    final = me.read_text()
    assert "Never add task numbers to code comments." in final, "the owner's line survives a rewrite that drops it"
    assert "Write code in English; talk to me in Spanish." in final


def test_removed_line_becomes_a_do_not_observation(env):
    cfg, store, llm, tmp = env
    llm.handlers.update(annotate=annotate_all, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "in english"))
    Runner(cfg, store, llm).run()
    me = cfg.path("profile_path")
    me.write_text(me.read_text().replace("- Write everything in English.\n", ""))
    seen = []
    llm.handlers["rewrite"] = lambda u: (seen.append(u), {"items": [], "summary": "removed"})[1]
    Runner(cfg, store, llm).run()
    assert "Do not: Write everything in English." in seen[0]


def test_word_cap_keeps_owner_then_most_supported(env):
    cfg, store, llm, tmp = env
    cfg.set("max_words", 35)  # the header takes 23
    runner = Runner(cfg, store, llm)
    o1 = store.add_observation("observed", "a", None, None, "c1", "2026-10-01T00:00:00")
    o2 = store.add_observation("owner", "b", None, None, "me.md", "2026-10-01T00:00:00")
    for text, source, support in (("one-off rule said once here", "observed", 1),
                                  ("repeated rule across many chats here", "observed", 5),
                                  ("owner rule written by me here", "owner", 1)):
        store.x("""INSERT INTO items (section, text, source, observation_ids, support, chats, last_seen)
                   VALUES ('writing', ?, ?, ?, ?, ?, '2026-10-01')""", (text, source, json.dumps([o1, o2]), support, support))
    me = runner.render()
    assert "owner rule written by me here" in me and "repeated rule across many chats here" in me
    assert "one-off rule said once here" not in me, "the one-off is the first to leave when over the cap"
    assert store.one("SELECT active FROM items WHERE text LIKE 'one-off%'")["active"] == 1, "it stays in the database"


def test_cap_stops_the_run_without_moving_cursors(env):
    cfg, store, llm, tmp = env
    llm.cap = 0.0
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "in english"))
    stats = Runner(cfg, store, llm).run()
    assert stats["stopped"] and store.one("SELECT count(*) AS n FROM files")["n"] == 0


def test_changelog_records_added_changed_removed(env):
    cfg, store, llm, tmp = env
    llm.handlers.update(annotate=annotate_all, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "in english"))
    Runner(cfg, store, llm).run()
    row = store.one("SELECT * FROM changelog ORDER BY id DESC LIMIT 1")
    assert json.loads(row["added"]) == ["Write everything in English."]


def test_cli_config(env, capsys, monkeypatch):
    cfg, store, llm, tmp = env
    calls = fake_scheduler_tools(monkeypatch, "macos")
    scheduler.LAUNCH_AGENT.parent.mkdir(parents=True)
    scheduler.LAUNCH_AGENT.write_bytes(b"")  # scheduled: changing the time rewrites the agent
    cli.main(["config", "cap", "7.5"])
    cli.main(["config", "read-mode", "mine"])
    cli.main(["config", "time", "9:30"])
    c = Config()
    assert c["cap_usd"] == 7.5 and c["read_mode"] == "mine" and c["run_at"] == "09:30"
    assert scheduler.plistlib.loads(scheduler.LAUNCH_AGENT.read_bytes())["StartCalendarInterval"] == {"Hour": 9, "Minute": 30}
    assert all(str(scheduler.LAUNCH_AGENT) in a[-1] for a in calls), "launchctl only ever sees the test's own agent file"
    with pytest.raises(SystemExit):
        cli.main(["config", "read-mode", "everything"])
    with pytest.raises(SystemExit):
        cli.parse_interval("2m")
    for bad in (["time", "25:00"], ["time", "noon"], ["interval", "1h"]):
        with pytest.raises(SystemExit):
            cli.main(["config", *bad])


def test_profile_lines_ignore_headers():
    assert _lines("# How\n\n_Maintained by x_\n\n## Writing\n- Do X.\n  - Example: a → b\n") == ["Do X.", "a → b"]


def test_emptying_a_bullet_is_a_deletion_not_a_new_empty_rule(env):
    """Erasing a line's text and leaving "- " behind is a deletion, not a new empty rule."""
    cfg, store, llm, tmp = env
    llm.handlers.update(annotate=annotate_all, rewrite=rewrite_merge)
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "in english"))
    Runner(cfg, store, llm).run()
    me = cfg.path("profile_path")
    me.write_text(me.read_text().replace("- Write everything in English.", "- "))
    seen = []
    llm.handlers["rewrite"] = lambda u: (seen.append(u), {"items": [], "summary": "removed"})[1]
    Runner(cfg, store, llm).run()
    owner = [r["preference"] for r in store.q("SELECT preference FROM observations WHERE kind = 'owner'")]
    assert owner == ["Do not: Write everything in English."]
    assert "- \n" not in me.read_text() and "Write everything in English" not in me.read_text()


def test_install_on_windows_creates_a_task_that_catches_up(env, monkeypatch):
    cfg, store, llm, tmp = env
    monkeypatch.setattr(scheduler, "PLATFORM", "windows")
    seen = []

    def run(args, **kw):
        if "/XML" in args:  # the definition only exists while schtasks reads it
            seen.append(Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16"))
        seen.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(scheduler.subprocess, "run", run)
    cfg.set("run_at", "09:30")
    scheduler.install(cfg)
    xml, create = seen
    assert create[:4] == ["schtasks", "/Create", "/TN", "who-am-ai"]
    assert "<StartBoundary>2026-01-01T09:30:00</StartBoundary>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "who_am_ai.cli" in xml and "run.log" in xml and str(cfg.root) in xml
    assert not (cfg.root / "task.xml").exists()


def test_install_on_linux_writes_a_persistent_user_timer(env, monkeypatch):
    cfg, store, llm, tmp = env
    calls = fake_scheduler_tools(monkeypatch, "linux")
    cfg.set("run_at", "07:05")
    assert not scheduler.installed()
    scheduler.install(cfg)
    timer = (scheduler.SYSTEMD_DIR / "who-am-ai.timer").read_text(encoding="utf-8")
    service = (scheduler.SYSTEMD_DIR / "who-am-ai.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 07:05:00" in timer and "Persistent=true" in timer
    assert "who_am_ai.cli" in service and f"WHO_AM_AI_HOME={cfg.root}" in service
    assert calls.index(["systemctl", "--user", "enable", "who-am-ai.timer"]) \
        < calls.index(["systemctl", "--user", "restart", "who-am-ai.timer"]), "restart applies a new run time"
    assert scheduler.installed()
    scheduler.uninstall()
    assert not scheduler.installed() and not (scheduler.SYSTEMD_DIR / "who-am-ai.service").exists()


def test_scheduler_failure_is_reported(env, monkeypatch):
    cfg, store, llm, tmp = env
    fake_scheduler_tools(monkeypatch, "linux", returncode=1)
    with pytest.raises(SystemExit, match="refused the schedule"):
        scheduler.install(cfg)
    assert not scheduler.installed(), "a failed install must not leave files that look scheduled"

    def missing(args, **kw):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(scheduler.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="run `whoamai run` yourself"):
        scheduler.install(cfg)
    assert not scheduler.installed() and not list(scheduler.SYSTEMD_DIR.iterdir())


def test_status_and_uninstall_survive_a_missing_scheduler_tool(env, monkeypatch):
    cfg, store, llm, tmp = env
    monkeypatch.setattr(scheduler, "PLATFORM", "windows")

    def missing(args, **kw):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(scheduler.subprocess, "run", missing)
    assert not scheduler.installed()
    scheduler.uninstall()


def test_systemd_units_escape_specifiers_and_variables(env, monkeypatch):
    cfg, store, llm, tmp = env
    cfg.root = tmp / "100%" / "a$b"
    monkeypatch.setattr(scheduler.sys, "executable", "/opt/p$q/100%/python")
    service, _ = scheduler.systemd_units(cfg)
    lines = dict(l.split("=", 1) for l in service.splitlines() if "=" in l)
    assert lines["ExecStart"].startswith('"/opt/p$$q/100%%/python"')
    assert "100%%" in lines["Environment"] and "100%%" in lines["StandardOutput"]
    assert "a$b" in lines["Environment"], "$ is only expanded in ExecStart"


def test_install_keeps_the_line_endings_of_claude_md(env, monkeypatch):
    cfg, store, llm, tmp = env
    fake_scheduler_tools(monkeypatch, "linux")
    claude_md = cfg.path("claude_md")
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    claude_md.write_bytes(b"# Notes\r\n- Be brief.\r\n")
    cli.main(["install"])
    assert claude_md.read_bytes() == b"# Notes\r\n- Be brief.\r\n\r\n@~/.claude/me.md\r\n"
    cli.main(["uninstall"])
    assert claude_md.read_bytes() == b"# Notes\r\n- Be brief.\r\n"


def test_cli_install_and_uninstall_manage_the_import_line(env, monkeypatch):
    cfg, store, llm, tmp = env
    fake_scheduler_tools(monkeypatch, "linux")
    claude_md = cfg.path("claude_md")
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    claude_md.write_text("# Notas\n- Responda em português.\n", encoding="utf-8")
    cli.main(["install"])
    cli.main(["install"])
    assert claude_md.read_text(encoding="utf-8").splitlines().count(cli.IMPORT_LINE) == 1
    cli.main(["uninstall"])
    assert claude_md.read_text(encoding="utf-8") == "# Notas\n- Responda em português.\n"


def test_profile_keeps_non_ascii_text_in_utf8(env):
    """Windows defaults text files to a legacy code page; the profile must always be UTF-8."""
    cfg, store, llm, tmp = env
    llm.handlers.update(annotate=annotate_all,
                        rewrite=lambda u: {"items": [{"section": "writing", "text": "Escreva 'ação' sem emojis.",
                                                      "example": None, "keeps": None, "supported_by": ["O1"]}],
                                           "summary": "added"})
    write(tmp / "projects/-repo/s1.jsonl", line("user", "u1", "in english"))
    Runner(cfg, store, llm).run()
    assert "Escreva 'ação' sem emojis." in cfg.path("profile_path").read_bytes().decode("utf-8")
