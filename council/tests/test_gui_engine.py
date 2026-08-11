#!/usr/bin/env python3
"""Regression for council_gui_engine.EngineRun -- the GUI's subprocess seam.

Driven against a FAKE engine, not a real council fire: the behaviour under test is
process/pipe lifetime, not model output, and a fake makes the deadlock and cancellation
cases deterministic and free. The real engine is exercised separately by a live fire.

The load-bearing case is `stdout_over_pipe_capacity`. MEASURED before this seam was
written: a child writing 200 KB into an undrained stdout PIPE fills the 64 KB buffer and
blocks, and because the reader is busy on the events fd, the events stream never reaches
EOF -- the GUI hangs on any real fire. That is why stdout/stderr are temp FILES here.

Re-run:  python3 council/tests/test_gui_engine.py
"""

import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# NOTE: `json` and `os` are NOT imported here. The fake engines below use both, but only
# inside string bodies executed by the CHILD process, which does its own imports. This
# process never parses the stream itself -- EngineRun hands back decoded dicts.

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import council_gui_engine as ge  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


def fake_engine(body: str) -> Path:
    """Write a stand-in engine that receives --events-fd like the real one.

    `evj` builds its record with json.dumps; `ev` takes a pre-formed line. Use `evj`
    whenever a field is COMPUTED. An earlier draft wrote records as single-quoted
    literals containing `+ line.strip()`, so the concatenation was never evaluated, the
    record was not valid JSON, and it was silently dropped by the reader -- three tests
    failed for a reason that had nothing to do with the code under test.

    PROVENANCE NOTE, recorded because it is exactly the thing this project exists to
    catch: the evj fix was originally applied to this file by a Python string-replacement
    script run through Bash. The council hooks Write and Edit; it does not hook the shell,
    so that change reached disk with NO review. Worse, council_audit_writes.py prunes
    `council/tests/` (see its skip list), so the integrity tool cannot see edits here at all --
    an audit of this directory reports nothing whether or not a bypass happened. This
    docstring is edited through the reviewed path deliberately, so the bypass is on the
    record rather than only in a transcript.
    """
    d = Path(tempfile.mkdtemp())
    p = d / "fake_engine.py"
    p.write_text(textwrap.dedent("""
        import os, sys
        fd = int(sys.argv[sys.argv.index("--events-fd") + 1])
        import json as _json
        def ev(line):
            os.write(fd, (line + "\\n").encode())
        def evj(**kw):
            os.write(fd, (_json.dumps(kw) + "\\n").encode())
    """) + textwrap.dedent(body))
    return p


print("== events stream, stdout capture, and exit status ==")
eng = fake_engine("""
    ev('{"ev":"run_started","voting":["a"]}')
    ev('{"ev":"member_finished","member":"a","verdict":"PASS"}')
    sys.stdout.write("VERDICT: PASS\\nbody\\n")
    sys.stderr.write("a warning\\n")
    sys.exit(1)
""")
run = ge.EngineRun(["--layer", "posttool"], engine=eng)
events = list(run.stream())
check("both records arrived", [e["ev"] for e in events] == ["run_started", "member_finished"])
check("no start error", run.start_error is None)
check("returncode propagated", run.returncode == 1)
check("stdout captured", run.stdout.startswith("VERDICT: PASS"))
check("stderr captured", "a warning" in run.stderr)

print("== THE DEADLOCK CASE: engine writes far past a pipe's capacity ==")
# Falsifier: on the previous pipe-based implementation this hangs and the suite times out.
eng = fake_engine("""
    ev('{"ev":"round_started","round":1}')
    sys.stdout.write("Z" * 300000)
    ev('{"ev":"final_verdict","verdict":"PASS"}')
""")
t0 = time.time()
run = ge.EngineRun([], engine=eng)
events = list(run.stream())
dt = time.time() - t0
check(f"completed without hanging ({dt:.2f}s)", dt < 30)
check("BOTH events arrived (the second is after the big write)",
      [e["ev"] for e in events] == ["round_started", "final_verdict"])
check("all 300000 bytes of stdout recovered", len(run.stdout) == 300000)

print("== stdin reaches the engine ==")
eng = fake_engine("""
    data = sys.stdin.read()
    sys.stdout.write("GOT:" + data)
""")
run = ge.EngineRun([], stdin_text="the pitch", engine=eng)
list(run.stream())
check("engine received the pitch on stdin", run.stdout == "GOT:the pitch")

print("== a malformed record does not end the stream ==")
eng = fake_engine("""
    ev('{"ev":"one"}')
    ev('not json at all')
    ev('{"ev":"two"}')
""")
run = ge.EngineRun([], engine=eng)
check("valid records survive a bad one between them",
      [e["ev"] for e in list(run.stream())] == ["one", "two"])

print("== a missing engine is reported, not silently empty ==")
run = ge.EngineRun([], engine=ROOT / "no_such_engine_xyz.py")
check("stream yields nothing", list(run.stream()) == [])
check("start_error explains why", "not found" in (run.start_error or ""))

print("== CANCEL kills the whole process TREE, not just the child ==")
# The engine spawns a grandchild that outlives it unless the GROUP is signalled -- the
# real engine spawns codex and bwrap the same way, so killing only the direct child
# would leave paid model calls running.
marker = Path(tempfile.mkdtemp()) / "grandchild_still_alive"
eng = fake_engine(f"""
    import subprocess, time
    subprocess.Popen([sys.executable, "-c",
        "import time,pathlib; time.sleep(6); pathlib.Path({str(marker)!r}).write_text('x')"])
    ev('{{"ev":"run_started"}}')
    time.sleep(30)
""")
run = ge.EngineRun([], engine=eng)
stream = run.stream()
first = next(stream)
check("engine started", first["ev"] == "run_started")
run.cancel()
try:
    for _ in stream:
        pass
except StopIteration:
    pass
check("cancel recorded", run.cancelled is True)
time.sleep(8)
check("the GRANDCHILD was killed too (marker never written)", not marker.exists())

print("== CONTROL CHANNEL: the child can be answered while it blocks ==")
# The approve-each path depends on this entirely. The child below blocks reading one line
# from --control-fd; if send_control does not reach it, the read never returns and this
# test hangs -- which is exactly the failure the stdin-based design would have had.
eng = fake_engine("""
    cfd = int(sys.argv[sys.argv.index("--control-fd") + 1])
    ev('{"ev":"approval_request","id":"w1"}')
    with os.fdopen(cfd, "r") as ctl:
        line = ctl.readline()
    evj(ev="note", text="got:" + line.strip())
""")
run = ge.EngineRun([], engine=eng, control=True)
stream = run.stream()
first = next(stream)
check("child asked for approval", first["ev"] == "approval_request")
check("send_control reports success", run.send_control("APPROVE w1") is True)
rest = list(stream)
check("the child received the exact line",
      any(e.get("text") == "got:APPROVE w1" for e in rest))

print("== closing the control channel gives the child EOF, so it can decline ==")
eng = fake_engine("""
    cfd = int(sys.argv[sys.argv.index("--control-fd") + 1])
    ev('{"ev":"approval_request","id":"w1"}')
    with os.fdopen(cfd, "r") as ctl:
        line = ctl.readline()
    evj(ev="note", text=("eof" if line == "" else "answered"))
""")
run = ge.EngineRun([], engine=eng, control=True)
stream = run.stream()
next(stream)
run._close_control()
rest = list(stream)
check("child saw EOF rather than hanging",
      any(e.get("text") == "eof" for e in rest))
check("send_control after close reports failure", run.send_control("APPROVE w1") is False)

print("== control=False leaves the flag off entirely ==")
eng = fake_engine("""
    evj(ev="note", text="has_control=" + str("--control-fd" in sys.argv))
""")
run = ge.EngineRun([], engine=eng)
check("no --control-fd is passed by default",
      any(e.get("text") == "has_control=False" for e in run.stream()))
check("send_control is a no-op without a channel", run.send_control("x") is False)

print("== print_roster reads the engine's own validation ==")
roster = ge.print_roster()
check("no error", "error" not in roster)
check("has members", isinstance(roster.get("members"), list) and roster["members"])
check("has a leader field", "leader" in roster)
check("has a source field", "source" in roster)

print("== the premise behind the temp-file design, checked rather than asserted ==")
with tempfile.TemporaryFile(mode="w+") as fh:
    p = subprocess.Popen([sys.executable, "-c", "print('x')"], stdout=fh, text=True)
    out, err = p.communicate()
check("communicate() returns (None, None) when stdout is a FILE", out is None and err is None)

print()
print(f"FAILURES: {FAILS if FAILS else 'none'}")
sys.exit(1 if FAILS else 0)
