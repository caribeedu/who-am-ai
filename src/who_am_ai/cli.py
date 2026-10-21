"""whoamai: the command line. `run` is what the LaunchAgent calls; everything else is for the owner."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import READ_MODES, Config
from .pipeline import Runner
from .store import Store, now

AGENT = Path.home() / "Library/LaunchAgents/local.who-am-ai.plist"
IMPORT_LINE = "@~/.claude/me.md"


def _open(cfg: Config) -> Store:
    return Store(cfg.root / "state.db")


def parse_interval(value: str) -> int:
    """'15m', '1h', '90' (minutes) → minutes."""
    m = re.fullmatch(r"\s*(\d+)\s*([mh]?)\s*", value.lower())
    if not m:
        raise SystemExit(f"invalid interval: {value!r} (use e.g. 15m, 1h)")
    minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if minutes < 5:
        raise SystemExit("the interval must be at least 5 minutes")
    return minutes


def parse_time(value: str) -> str:
    """'9:30', '09:30' → '09:30'."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SystemExit(f"invalid time: {value!r} (use HH:MM, e.g. 12:00)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def cmd_run(cfg: Config, args) -> None:
    stats = Runner(cfg, _open(cfg)).run()
    print(json.dumps(stats))
    if stats["stopped"]:
        sys.exit(1)


def cmd_status(cfg: Config, args) -> None:
    s = _open(cfg)
    last = s.one("SELECT ts, note FROM runs ORDER BY id DESC LIMIT 1")
    print(f"last run:      {last['ts'] + ' ' + last['note'] if last else 'never'}")
    print(f"spend (month): US${s.month_spend():.2f} of US${float(cfg['cap_usd']):.2f}")
    print(f"observations:  {s.one('SELECT count(*) AS n FROM observations')['n']}"
          f" ({s.one('SELECT count(*) AS n FROM observations WHERE consumed = 0')['n']} pending)")
    print(f"profile items: {s.one('SELECT count(*) AS n FROM items WHERE active = 1')['n']}")
    print(f"files tracked: {s.one('SELECT count(*) AS n FROM files')['n']}")
    print(f"scheduled:     {'yes, daily at ' + cfg['run_at'] if AGENT.exists() else 'no (whoamai install)'}")
    print(f"read mode:     {cfg['read_mode']}")


def cmd_log(cfg: Config, args) -> None:
    for r in _open(cfg).q("SELECT * FROM changelog ORDER BY id DESC LIMIT ?", (args.n,)):
        print(f"\n{r['ts']}  {r['summary']}")
        for t in json.loads(r["added"]):
            print(f"  + {t}")
        for c in json.loads(r["changed"]):
            print(f"  ~ {c['from']}\n    → {c['to']}")
        for t in json.loads(r["removed"]):
            print(f"  - {t}")


def cmd_config(cfg: Config, args) -> None:
    key, values = args.key, args.values
    if key is None:
        print(json.dumps(cfg.data, indent=2))
        return
    if key == "time":
        cfg.set("run_at", parse_time(values[0]))
        if AGENT.exists():
            install_agent(cfg)
    elif key == "cap":
        cfg.set("cap_usd", float(values[0]))
    elif key == "read-mode":
        if values[0] not in READ_MODES:
            raise SystemExit(f"read-mode must be one of {', '.join(READ_MODES)}")
        cfg.set("read_mode", values[0])
    elif key == "exclude":
        cfg.set("excluded_chats", sorted(set(cfg["excluded_chats"]) | {" ".join(values)}))
    elif key == "include":
        cfg.set("excluded_chats", [x for x in cfg["excluded_chats"] if x != " ".join(values)])
    elif key == "models":
        cfg.set("models", {"annotate": values[0], "rewrite": values[1] if len(values) > 1 else values[0]})
    elif key == "max-words":
        cfg.set("max_words", int(values[0]))
    elif key == "settle":
        cfg.set("settle_minutes", parse_interval(values[0]))
    else:
        raise SystemExit(f"unknown setting: {key}")
    print("saved")


def _uid() -> int:
    """launchd domain of the current user (the scheduler is macOS-only; elsewhere run `whoamai run` yourself)."""
    return os.getuid() if hasattr(os, "getuid") else 0


def install_agent(cfg: Config) -> None:
    uid = _uid()
    if AGENT.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(AGENT)], capture_output=True)
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    AGENT.write_bytes(plistlib.dumps({
        "Label": "local.who-am-ai",
        "ProgramArguments": [sys.executable, "-m", "who_am_ai.cli", "run"],
        "StartCalendarInterval": {"Hour": int(cfg["run_at"][:2]), "Minute": int(cfg["run_at"][3:])},
        "RunAtLoad": False,
        "StandardOutPath": str(cfg.root / "run.log"),
        "StandardErrorPath": str(cfg.root / "run.log"),
        "EnvironmentVariables": {"WHO_AM_AI_HOME": str(cfg.root)},
    }))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl refused the agent: {r.stderr.strip()}")


def cmd_install(cfg: Config, args) -> None:
    install_agent(cfg)
    claude_md = cfg.path("claude_md")
    text = claude_md.read_text() if claude_md.exists() else ""
    if IMPORT_LINE not in text.splitlines():
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(text.rstrip() + ("\n\n" if text.strip() else "") + IMPORT_LINE + "\n")
    print(f"scheduled daily at {cfg['run_at']}; {claude_md} imports {IMPORT_LINE}")


def cmd_uninstall(cfg: Config, args) -> None:
    if AGENT.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}", str(AGENT)], capture_output=True)
        AGENT.unlink()
    claude_md = cfg.path("claude_md")
    if claude_md.exists():
        lines = [line for line in claude_md.read_text().splitlines() if line.strip() != IMPORT_LINE]
        claude_md.write_text("\n".join(lines).rstrip() + "\n")
    print("unscheduled; import line removed (me.md and the history are kept)")


def cmd_import(cfg: Config, args) -> None:
    """One-time import: JSON list of {preference, quote, chat, ts, kind?}. `kind` is "imported" (default) or "owner"
    for preferences the owner stated themselves elsewhere (they keep the owner's authority)."""
    s = _open(cfg)
    items = json.loads(Path(args.file).read_text())
    for it in items:
        kind = it.get("kind") or "imported"
        if kind not in ("imported", "owner"):
            raise SystemExit(f"invalid kind: {kind}")
        s.add_observation(kind, it["preference"], it.get("quote"), it.get("topic"), it.get("chat"), it.get("ts") or now())
    print(f"imported {len(items)} observations (the next run folds them into the profile)")


def cmd_key(cfg: Config, args) -> None:
    """Stores the Anthropic API key in this tool's Keychain item (service "who-am-ai"). Typed without echo."""
    import getpass

    import keyring
    value = getpass.getpass("Anthropic API key (not shown): ").strip()
    if not value:
        raise SystemExit("no key given")
    keyring.set_password("who-am-ai", "anthropic_api_key", value)
    print('saved in the Keychain as service "who-am-ai"')


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="whoamai", description="Keeps a short profile of how you want AI assistants to work.")
    p.add_argument("--version", action="version", version=f"whoamai {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("status", cmd_status), ("install", cmd_install),
                     ("uninstall", cmd_uninstall)):
        sub.add_parser(name).set_defaults(fn=fn)
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=10)
    lg.set_defaults(fn=cmd_log)
    c = sub.add_parser("config", help="show or change settings: time, cap, read-mode, exclude, include, models, max-words, settle")
    c.add_argument("key", nargs="?")
    c.add_argument("values", nargs="*")
    c.set_defaults(fn=cmd_config)
    k = sub.add_parser("key", help="store the Anthropic API key in this tool's own Keychain item")
    k.set_defaults(fn=cmd_key)
    im = sub.add_parser("import")
    im.add_argument("file")
    im.set_defaults(fn=cmd_import)
    args = p.parse_args(argv)
    args.fn(Config(), args)


if __name__ == "__main__":
    main()
