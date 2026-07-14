#!/usr/bin/env python3
"""SessionStart hook: run environment probes and land them in evidence.

Fires when a Claude Code session begins. Runs a fixed list of cheap
environment probes (hardware, disk, memory, python, etc.) and appends
each as a synthetic Bash event to the per-session evidence file at
~/.claude/state/<session_id>/evidence.jsonl. The council's rule 11
("caveat phrases require probe in evidence") can then credit hardware
or environment claims that were probed at session start.

Per the local plugin-dev hook-development SKILL.md, SessionStart hooks
execute when a Claude Code session begins, and the input payload contains
common hook fields such as session_id, transcript_path, cwd,
permission_mode, and hook_event_name.

The script exits 0 in all paths. Probe failures (command not found,
non-zero exit, timeout) are still logged as Bash events with the
appropriate exit_code or error in the stderr_tail so the council can
see that the probe was attempted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "state"
MAX_TAIL_LINES = 50
MAX_TAIL_BYTES = 4096
PER_PROBE_TIMEOUT_S = 10

PROBES: list[tuple[str, list[str]]] = [
    ("uname -a", ["uname", "-a"]),
    ("hostname", ["hostname"]),
    ("nproc", ["nproc"]),
    ("free -h", ["free", "-h"]),
    ("df -h", ["df", "-h"]),
    ("nvidia-smi", ["nvidia-smi"]),
    ("which python3", ["which", "python3"]),
    ("python3 --version", ["python3", "--version"]),
    ("which node", ["which", "node"]),
    ("node --version", ["node", "--version"]),
    ("git --version", ["git", "--version"]),
]


def truncate_tail(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    if len(lines) > MAX_TAIL_LINES:
        lines = lines[-MAX_TAIL_LINES:]
        text = "[head truncated]\n" + "".join(lines)
    else:
        text = "".join(lines)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > MAX_TAIL_BYTES:
        tail_bytes = encoded[-MAX_TAIL_BYTES:]
        text = "[truncated]" + tail_bytes.decode("utf-8", errors="replace")
    return text


def run_probe(display: str, argv: list[str]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "tool": "Bash",
        "at": now,
        "command": display,
        "description": "SessionStart probe (auto)",
    }
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PER_PROBE_TIMEOUT_S,
        )
        event["exit_code"] = proc.returncode
        event["stdout_tail"] = truncate_tail(proc.stdout)
        event["stderr_tail"] = truncate_tail(proc.stderr)
        event["interrupted"] = False
    except FileNotFoundError:
        event["exit_code"] = 127
        event["stdout_tail"] = ""
        event["stderr_tail"] = f"command not found: {argv[0]}"
        event["interrupted"] = False
    except subprocess.TimeoutExpired:
        event["exit_code"] = None
        event["stdout_tail"] = ""
        event["stderr_tail"] = f"timeout after {PER_PROBE_TIMEOUT_S}s"
        event["interrupted"] = True
    except Exception as exc:
        event["exit_code"] = None
        event["stdout_tail"] = ""
        event["stderr_tail"] = f"probe error: {exc}"
        event["interrupted"] = False
    return event


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"session-start-probe: invalid JSON on stdin: {e}", file=sys.stderr)
        return 0

    session_id = payload.get("session_id", "")
    if not session_id:
        return 0

    state_dir = STATE_ROOT / session_id
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"session-start-probe: mkdir failed: {e}", file=sys.stderr)
        return 0

    evidence_file = state_dir / "evidence.jsonl"
    try:
        with open(evidence_file, "a", encoding="utf-8") as f:
            for display, argv in PROBES:
                event = run_probe(display, argv)
                f.write(json.dumps(event, default=str) + "\n")
    except OSError as e:
        print(f"session-start-probe: write failed: {e}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
