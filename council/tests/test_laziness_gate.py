#!/usr/bin/env python3
"""Durable production-driving integration regression for laziness_gate.py.

Drives the REAL module in-process (rule 3): imports it, monkeypatches STATE_ROOT to a
temp dir, and calls the real main()/collect_evidence_commands -- NOT a reimplementation.
It exercises the module's logic in-process; the hook subprocess/host carriage is a
separate test layer, not exercised here. Every driving case asserts rc==0 (a crash ->
rc=1 -> the case fails).

Covers the 2026-07-22 redesign + every council-flagged case:
  Fix 1  blind -> fail-open + observable note, for ALL THREE blind cases (no session_id,
         no file, read error via a directory-as-evidence-file so read_text raises OSError);
         the note is asserted in all three.
  Fix 2a recency: a stale probe (outside the last EVIDENCE_RECENCY_EVENTS) does NOT clear;
         a recent one does.
  Fix 2d split: the always-deny trigger and the recency-gated compute-marker trigger.
  Bug   Edit that REMOVES an always-deny caveat is ALLOWED (new_string-only scan).

Self-contained, no API, gitignored. Run: python3 council/tests/test_laziness_gate.py
"""
import io, json, sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import laziness_gate as lg  # noqa: E402

# The real main() checks Council/DISABLED; if it exists every case would allow.
if (ROOT / "DISABLED").exists():
    print("ABORT: Council/DISABLED exists -- gate is disabled, test would be meaningless.")
    sys.exit(2)

N = lg.EVIDENCE_RECENCY_EVENTS
assert N == 30, f"test assumes EVIDENCE_RECENCY_EVENTS==30, found {N}"

NONEXEMPT = "/home/user/myproject/foo.py"   # contains no exempt substring
# DERIVED from where the gate actually lives, for the same reason the gate derives it: a
# literal "/professional/council/" here passed on WSL2 and would have gone on passing after
# the 2026-08-02 move while the real exemption was broken -- the test would have been
# asserting against a path the hook no longer recognises, and agreeing with itself.
EXEMPT = str(Path(lg.__file__).resolve().parent / "foo.py")


def write_evidence(state_root, sid, events):
    sd = Path(state_root) / sid
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "evidence.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")


def drive_main(payload, state_root):
    lg.STATE_ROOT = Path(state_root)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = lg.main()
    except Exception as e:               # a crash is a failure, surfaced via rc!=0
        rc = 1
        err.write(f"EXCEPTION: {e!r}")
    finally:
        sys.stdin = old_stdin
    return rc, out.getvalue(), err.getvalue()


def is_deny(stdout):
    if not stdout.strip():
        return False
    obj = json.loads(stdout)
    return obj.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def W(content, path=NONEXEMPT):
    return {"tool_name": "Write", "tool_input": {"content": content, "file_path": path},
            "session_id": "sess"}


def E(new, old, path=NONEXEMPT):
    return {"tool_name": "Edit",
            "tool_input": {"new_string": new, "old_string": old, "file_path": path},
            "session_id": "sess"}


NOTE = "could not read this session's evidence"   # the fail-open observable note prefix
cases = []


def add(label, cond, detail=""):
    cases.append((label, bool(cond), detail))


with tempfile.TemporaryDirectory() as d:
    recent_py = [{"tool": "Read", "file_path": f"f{i}.py"} for i in range(20)] + \
                [{"tool": "Bash", "command": "python3 check.py"}]
    stale_py = [{"tool": "Bash", "command": "python3 old.py"}] + \
               [{"tool": "Read", "file_path": f"g{i}.py"} for i in range(34)]  # py at -35
    recent_pip = [{"tool": "Bash", "command": "pip install torch"}]

    # --- Fix 2d: always-deny trigger denied even with a recent python3 ---
    write_evidence(d, "sess", recent_py)
    rc, so, se = drive_main(W("this is not feasible right now"), d)
    add("always-deny trigger + recent python3 -> DENY", rc == 0 and is_deny(so), so[:70])

    # --- Fix 2d: recency-gated trigger cleared by a RECENT pip ---
    write_evidence(d, "sess", recent_pip)
    rc, so, se = drive_main(W("that would require a torch build"), d)
    add("recency-gated trigger + recent pip -> ALLOW (backed)", rc == 0 and not is_deny(so), so[:70])

    # --- recency-gated trigger with NO probe -> DENY ---
    write_evidence(d, "sess", [{"tool": "Read", "file_path": "x.py"}])
    rc, so, se = drive_main(W("that would require a rewrite"), d)
    add("recency-gated trigger + no probe -> DENY", rc == 0 and is_deny(so), so[:70])

    # --- Fix 2a recency: recent python3 clears the compute-marker trigger ---
    write_evidence(d, "sess", recent_py)
    rc, so, se = drive_main(W("compute required for this"), d)
    add("compute-marker trigger + RECENT python3 -> ALLOW", rc == 0 and not is_deny(so), so[:70])

    # --- Fix 2a recency: STALE python3 (event -35) does NOT clear ---
    write_evidence(d, "sess", stale_py)
    rc, so, se = drive_main(W("compute required here"), d)
    add("compute-marker trigger + STALE python3 (-35) -> DENY (recency)", rc == 0 and is_deny(so), so[:70])

    # --- old_string bug fix: Edit REMOVING an always-deny caveat is ALLOWED ---
    write_evidence(d, "sess", recent_py)
    rc, so, se = drive_main(E("this is now handled", "this is out of scope"), d)
    add("Edit REMOVING an always-deny caveat (new clean) -> ALLOW", rc == 0 and not is_deny(so), so[:70])

    # --- Edit ADDING an always-deny caveat (in new_string) -> DENY ---
    rc, so, se = drive_main(E("this is out of scope", "this is fine"), d)
    add("Edit ADDING an always-deny caveat (in new_string) -> DENY", rc == 0 and is_deny(so), so[:70])

    # --- Fix 1: blind (no session_id) + trigger -> ALLOW + note ---
    p = W("compute required"); p["session_id"] = ""
    rc, so, se = drive_main(p, d)
    add("blind (no session_id) + trigger -> ALLOW + note", rc == 0 and not is_deny(so) and NOTE in se, se[:55])

    # --- Fix 1: blind (no evidence file) + trigger -> ALLOW + note ---
    p = W("compute required"); p["session_id"] = "no_such_session"
    rc, so, se = drive_main(p, d)
    add("blind (no evidence file) + trigger -> ALLOW + note", rc == 0 and not is_deny(so) and NOTE in se, se[:55])

    # --- Fix 1: blind (read error) -> ALLOW + note. Make evidence.jsonl a DIRECTORY so
    #     read_text raises IsADirectoryError (an OSError). Deterministic, no mock. ---
    (Path(d) / "readerr" / "evidence.jsonl").mkdir(parents=True, exist_ok=True)  # a dir
    p = W("compute required"); p["session_id"] = "readerr"
    rc, so, se = drive_main(p, d)
    add("blind (read error: dir-as-file) + trigger -> ALLOW + note",
        rc == 0 and not is_deny(so) and NOTE in se and "unreadable" in se, se[:70])

    # --- exempt path -> ALLOW even with an always-deny trigger ---
    write_evidence(d, "sess", recent_py)
    rc, so, se = drive_main(W("out of scope", path=EXEMPT), d)
    add("exempt council path -> ALLOW", rc == 0 and not is_deny(so), so[:40])

    # --- no trigger -> ALLOW ---
    rc, so, se = drive_main(W("all good here"), d)
    add("no trigger -> ALLOW", rc == 0 and not is_deny(so), so[:40])

    # --- collect_evidence_commands tuple/blind shape (unit) ---
    lg.STATE_ROOT = Path(d)
    c, br = lg.collect_evidence_commands("")
    add("collect(\"\") -> (\"\", reason)", c == "" and isinstance(br, str), (c, br))
    c, br = lg.collect_evidence_commands("no_such_session")
    add("collect(missing file) -> (\"\", reason)", c == "" and "no evidence file" in (br or ""), br)
    write_evidence(d, "sess", recent_py)
    c, br = lg.collect_evidence_commands("sess")
    add("collect(real) -> (cmds, None)", br is None and "python3 check.py" in c, br)

ok = True
for label, cond, detail in cases:
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}   :: {detail}")
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'} ({len(cases)} checks)")
sys.exit(0 if ok else 1)
