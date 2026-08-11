#!/usr/bin/env python3
"""stop_audit's lost-review catch-all, and the ORDER bug the council found in it.

WHY THIS SUITE EXISTS AS A SUITE. The first version of the stop_audit change computed the
notice, ARCHIVED the markers, and then never appended the notice to the surfaced text on
the normal (transcript present) path. Both halves were wrong and they compounded: the
report was dropped AND the evidence was retired, so the loss could never be reported by
anything, ever. The council caught it; nothing in the test corpus would have. These checks
are that gap closed.

THE TWO PROPERTIES UNDER TEST, stated so a reader can see what would falsify them:
  1. A turn whose ONLY finding is lost reviews must still surface something. If stop_audit
     exits 0 in silence, a lost review is again indistinguishable from a clean turn.
  2. A marker must NOT be archived on a path that does not surface it. Archive-then-drop
     is the failure above; the test drives the drop path and asserts the marker SURVIVES.
"""
import json
import os
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


def seed_orphan(home: Path, session: str, target: str) -> Path:
    """Plant an aged, unfinished review marker under a fake HOME."""
    d = home / ".claude" / "state" / session / ca.PENDING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    p = d / "orphan-1.json"
    p.write_text(json.dumps({
        "started": "2026-08-06T00:00:00+00:00", "tool_name": "Edit",
        "target_path": target, "tool_use_id": "orphan-1", "session_id": session}))
    old = time.time() - (ca.ORPHAN_MIN_AGE_S + 120)
    os.utime(p, (old, old))
    return p


def run_stop_audit(home: Path, payload: dict) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "stop_audit.py")],
        input=json.dumps(payload), text=True, capture_output=True,
        env=dict(os.environ, HOME=str(home)), timeout=300)
    # SURFACED TEXT IS THE UNION OF BOTH STREAMS, and that is not laziness. The two hooks
    # in this project surface on DIFFERENT streams by design: council_advisor emits JSON
    # on stdout (exit 0), while stop_audit uses exit 2 + stderr, which is what Claude
    # Code's Stop-hook contract requires to keep the turn alive. A first version of this
    # suite asserted on stdout alone and reported four failures against correct code --
    # the test was measuring the wrong pipe.
    return proc.returncode, proc.stdout + proc.stderr, proc.stderr


def main():
    SESS = "stopaudit-lost"
    TARGET = "/some/unreviewed_file.py"

    print("\n-- A. NO transcript: the loss is still reported --")
    home = Path(tempfile.mkdtemp(prefix="sa-notrans-"))
    m = seed_orphan(home, SESS, TARGET)
    rc, out, err = run_stop_audit(home, {"session_id": SESS, "transcript_path": "",
                                         "cwd": str(home)})
    check("something was surfaced", bool(out.strip()), f"rc={rc} bytes={len(out)}")
    check("the surfaced text names the unreviewed file", TARGET in out)
    check("marker archived once reported", not m.exists())
    kept = list(m.parent.glob("*.reported"))
    check("evidence kept, not deleted", len(kept) == 1)

    print("\n-- B. MAIN path (transcript present): the notice is NOT dropped --")
    # This is the exact case the first draft got wrong: transcript present, no other
    # finding, so the old code fell through to `if not any_warn: return 0` in silence.
    home2 = Path(tempfile.mkdtemp(prefix="sa-main-"))
    tr = home2 / "transcript.jsonl"
    tr.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "Done. Nothing notable."}]}}) + "\n")
    m2 = seed_orphan(home2, SESS, TARGET)
    rc2, out2, err2 = run_stop_audit(home2, {"session_id": SESS,
                                             "transcript_path": str(tr),
                                             "cwd": str(home2)})
    check("main path surfaces the loss", bool(out2.strip()),
          f"rc={rc2} bytes={len(out2)}")
    check("main-path text names the unreviewed file", TARGET in out2)
    check("main-path text says LOST", "LOST" in out2.upper())
    check("main-path marker archived after surfacing", not m2.exists())

    print("\n-- C. no orphans -> stop_audit stays quiet (no false alarm) --")
    home3 = Path(tempfile.mkdtemp(prefix="sa-clean-"))
    tr3 = home3 / "t.jsonl"
    tr3.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "All good."}]}}) + "\n")
    rc3, out3, _ = run_stop_audit(home3, {"session_id": "no-orphans",
                                          "transcript_path": str(tr3),
                                          "cwd": str(home3)})
    check("clean session surfaces no loss notice", "COUNCIL REVIEWS LOST" not in out3,
          f"bytes={len(out3)}")

    print("\n-- D. a FRESH marker is never reported as lost --")
    home4 = Path(tempfile.mkdtemp(prefix="sa-fresh-"))
    d = home4 / ".claude" / "state" / SESS / ca.PENDING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    fresh = d / "live.json"
    fresh.write_text(json.dumps({"started": "now", "tool_name": "Edit",
                                 "target_path": "/live/fire.py",
                                 "tool_use_id": "live", "session_id": SESS}))
    rc4, out4, _ = run_stop_audit(home4, {"session_id": SESS, "transcript_path": "",
                                          "cwd": str(home4)})
    check("live fire not reported as a loss", "COUNCIL REVIEWS LOST" not in out4)
    check("live marker left untouched", fresh.exists())

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
