"""The daily schedule for `whoamai run`: a LaunchAgent on macOS, a Task Scheduler task on Windows and a systemd
user timer on Linux. All three run at a fixed time and catch up after sleep, as D2 requires."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config

LABEL = "local.who-am-ai"
TASK = "who-am-ai"
LAUNCH_AGENT = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
SYSTEMD_DIR = Path.home() / ".config/systemd/user"
PLATFORM = "macos" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else "linux"


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(f"{args[0]} not found; run `whoamai run` yourself instead") from None
    if check and r.returncode != 0:
        raise SystemExit(f"{args[0]} refused the schedule: {(r.stderr or r.stdout).strip()}")
    return r


def _command() -> list[str]:
    return [sys.executable, "-m", "who_am_ai.cli", "run"]


def installed() -> bool:
    if PLATFORM == "macos":
        return LAUNCH_AGENT.exists()
    if PLATFORM == "windows":
        return _run(["schtasks", "/Query", "/TN", TASK], check=False).returncode == 0
    return (SYSTEMD_DIR / f"{TASK}.timer").exists()


def install(cfg: Config) -> None:
    {"macos": _install_launchd, "windows": _install_schtasks, "linux": _install_systemd}[PLATFORM](cfg)


def uninstall() -> None:
    {"macos": _uninstall_launchd, "windows": _uninstall_schtasks, "linux": _uninstall_systemd}[PLATFORM]()


# ---------------------------------------------------------------- macOS


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _install_launchd(cfg: Config) -> None:
    uid = _uid()
    if LAUNCH_AGENT.exists():
        _run(["launchctl", "bootout", f"gui/{uid}", str(LAUNCH_AGENT)], check=False)
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": _command(),
        "StartCalendarInterval": {"Hour": int(cfg["run_at"][:2]), "Minute": int(cfg["run_at"][3:])},
        "RunAtLoad": False,
        "StandardOutPath": str(cfg.root / "run.log"),
        "StandardErrorPath": str(cfg.root / "run.log"),
        "EnvironmentVariables": {"WHO_AM_AI_HOME": str(cfg.root)},
    }))
    _run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT)])


def _uninstall_launchd() -> None:
    if LAUNCH_AGENT.exists():
        _run(["launchctl", "bootout", f"gui/{_uid()}", str(LAUNCH_AGENT)], check=False)
        LAUNCH_AGENT.unlink()


# ---------------------------------------------------------------- Windows


def task_xml(cfg: Config) -> str:
    """The Task Scheduler definition. XML is the only way to ask for "run as soon as possible if the time was
    missed" (StartWhenAvailable), which is how a sleeping machine catches up."""
    command = " ".join(f'"{a}"' for a in _command())
    line = f'set "WHO_AM_AI_HOME={cfg.root}" && {command} >> "{cfg.root / "run.log"}" 2>&1'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{cfg["run_at"]}:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>cmd.exe</Command><Arguments>/c "{escape(line)}"</Arguments></Exec>
  </Actions>
</Task>
"""


def _install_schtasks(cfg: Config) -> None:
    xml = cfg.root / "task.xml"
    xml.write_text(task_xml(cfg), encoding="utf-16")
    try:
        _run(["schtasks", "/Create", "/TN", TASK, "/XML", str(xml), "/F"])
    finally:
        xml.unlink(missing_ok=True)


def _uninstall_schtasks() -> None:
    if installed():
        _run(["schtasks", "/Delete", "/TN", TASK, "/F"])


# ---------------------------------------------------------------- Linux


def systemd_units(cfg: Config) -> tuple[str, str]:
    """(service, timer). `Persistent=true` runs a missed activation as soon as the machine is up again."""
    exec_start = " ".join(f'"{a}"' for a in _command())
    log = cfg.root / "run.log"
    service = f"""[Unit]
Description=who-am-ai: learn preferences from your assistant chats

[Service]
Type=oneshot
Environment="WHO_AM_AI_HOME={cfg.root}"
ExecStart={exec_start}
StandardOutput=append:{log}
StandardError=append:{log}
"""
    timer = f"""[Unit]
Description=who-am-ai daily run

[Timer]
OnCalendar=*-*-* {cfg["run_at"]}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def _install_systemd(cfg: Config) -> None:
    service, timer = systemd_units(cfg)
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    (SYSTEMD_DIR / f"{TASK}.service").write_text(service, encoding="utf-8")
    (SYSTEMD_DIR / f"{TASK}.timer").write_text(timer, encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", f"{TASK}.timer"])


def _uninstall_systemd() -> None:
    if installed():
        _run(["systemctl", "--user", "disable", "--now", f"{TASK}.timer"], check=False)
    for suffix in ("timer", "service"):
        (SYSTEMD_DIR / f"{TASK}.{suffix}").unlink(missing_ok=True)
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"], check=False)
