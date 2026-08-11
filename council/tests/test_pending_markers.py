#!/usr/bin/env python3
"""Pending-review markers: does a LOST review become distinguishable from a PASS?

WHAT WOULD FALSIFY THIS SUITE: a marker that survives a normal return (false alarm), a
marker that does NOT survive a kill (the mechanism is inert), or an orphan reported while
a sibling fire could still be running (false alarm under the concurrency that is normal
on this machine).

The kill test is the load-bearing one and it runs the REAL council_advisor.main() in a
real subprocess, then SIGKILLs it mid-fire. Rebuilding that in-process would not exercise
the property under test, which is precisely what happens when no handler gets to run.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import council_advisor as ca  # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def main():
    # Redirect the state root so this suite never touches a real session's markers.
    tmp = Path(tempfile.mkdtemp(prefix="pending-markers-"))
    ca.EVIDENCE_STATE_ROOT = tmp
    SESS = "unit-session"

    print("\n-- A. write / clear round trip --")
    m = ca.write_pending_marker(SESS, "Write", "/x/y.py", "tu-1")
    check("marker written", m is not None and m.exists(), str(m))
    rec = json.loads(m.read_text())
    check("marker records tool + target",
          rec["tool_name"] == "Write" and rec["target_path"] == "/x/y.py")
    ca.clear_pending_marker(m)
    check("cleared marker is gone", not m.exists())
    ca.clear_pending_marker(m)  # idempotent
    check("clearing twice does not raise", True)

    print("\n-- B. a FRESH marker is not an orphan (a sibling fire may be live) --")
    m2 = ca.write_pending_marker(SESS, "Edit", "/x/fresh.py", "tu-2")
    check("fresh marker not reported", ca.orphan_markers(SESS) == [])
    check("...but it does exist on disk", m2.exists())

    print("\n-- C. an OLD marker IS an orphan --")
    old = time.time() - (ca.ORPHAN_MIN_AGE_S + 60)
    os.utime(m2, (old, old))
    orphans = ca.orphan_markers(SESS)
    check("aged marker reported as orphan", len(orphans) == 1, f"n={len(orphans)}")
    check("orphan carries its target",
          orphans and orphans[0]["target_path"] == "/x/fresh.py")
    check("orphan carries an age", orphans and orphans[0]["age_s"] >= ca.ORPHAN_MIN_AGE_S)

    print("\n-- D. the notice says LOST, not passed --")
    text = ca.format_orphan_notice(orphans)
    check("notice names the file", "/x/fresh.py" in text)
    check("notice says reviews were lost", "LOST" in text.upper())
    check("notice denies it is a pass", "not the same as a passing" in text.lower()
          or "NOT the same as a passing" in text)

    print("\n-- E. archiving retires it exactly once --")
    ca.archive_pending_marker(Path(orphans[0]["marker_path"]))
    check("archived marker no longer an orphan", ca.orphan_markers(SESS) == [])
    kept = list((tmp / SESS / ca.PENDING_DIRNAME).glob("*.reported"))
    check("evidence of the loss is KEPT, not deleted", len(kept) == 1, str(kept))

    print("\n-- F. sessions are isolated (parallel sessions are normal here) --")
    a = ca.write_pending_marker("sess-A", "Write", "/a.py", "ta")
    old2 = time.time() - (ca.ORPHAN_MIN_AGE_S + 60)
    os.utime(a, (old2, old2))
    check("session B does not see session A's orphan", ca.orphan_markers("sess-B") == [])
    check("session A does see its own", len(ca.orphan_markers("sess-A")) == 1)

    print("\n-- G. LOAD-BEARING: a KILLED advisor leaves its marker behind --")
    state = Path(tempfile.mkdtemp(prefix="pending-kill-"))
    target = state / "victim.py"
    target.write_text("def f():\n    return 1\n")
    payload = {
        "session_id": "kill-session", "transcript_path": "", "cwd": str(state),
        "tool_name": "Write", "tool_use_id": "kill-1", "prompt_id": "p",
        "tool_input": {"file_path": str(target), "content": "def f():\n    return 2\n"},
        "tool_response": {"filePath": str(target),
                          "originalFile": "def f():\n    return 1\n"},
    }
    env = dict(os.environ, HOME=str(state))
    # Real advisor, real council subprocess -- then killed while it runs.
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "council_advisor.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True)
    proc.stdin.write(json.dumps(payload))
    proc.stdin.close()
    pending = state / ".claude" / "state" / "kill-session" / ca.PENDING_DIRNAME
    appeared = False
    for _ in range(120):                      # up to ~12s for the marker to land
        if pending.is_dir() and list(pending.glob("*.json")):
            appeared = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    check("marker appears BEFORE the council finishes", appeared,
          f"dir={pending} exists={pending.is_dir()}")
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    survivors = list(pending.glob("*.json")) if pending.is_dir() else []
    check("marker SURVIVES a SIGKILL", len(survivors) == 1,
          f"n={len(survivors)}")
    if survivors:
        r = json.loads(survivors[0].read_text())
        check("surviving marker names the unreviewed edit",
              r.get("target_path") == str(target))

    n = len(checks)
    bad = [c for c in checks if not c[1]]
    print(f"\n{n - len(bad)}/{n} checks passed")
    if bad:
        print("FAILED:")
        for name, _, detail in bad:
            print(f"  - {name} {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
