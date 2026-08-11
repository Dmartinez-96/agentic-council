#!/usr/bin/env python3
"""Regression for council_events.py -- the live-progress NDJSON channel.

Runs against the PRODUCTION module. Every group exists because the property was either
broken in an earlier draft or asserted without a check; comments say which, so a future
reader can tell a real guard from decoration.

Sizing matters in this file and is stated rather than assumed: the pipe holds 65536 bytes
on this host, and council_events.PENDING_MAX is 1 MiB. A backlog test must therefore
exceed the PIPE without exceeding PENDING_MAX, or the emitter starts DROPPING and the
"resume" it is meant to prove never happens. An earlier draft got this wrong (400 x ~3 KB
= ~1.2 MB, past PENDING_MAX) and could not have passed.

Re-run:  python3 council/tests/test_events.py
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
import council_events as ce  # noqa: E402

_RE = r"(?mi)^\s*(REQUEST_FILE|REQUEST_URL|REQUEST_EXEC)\s*:.*$"
RED = lambda s: re.sub(_RE, lambda m: f"{m.group(1)}: <redacted>", s)  # noqa: E731
SECRET = "REQUEST_EXEC: cat ~/.ssh/id_rsa"
PIPE_CAP = 65536

FAILS = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


def nonblock(fd):
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)


class Reader:
    """Drains a NON-BLOCKING fd, retaining a trailing PARTIAL line across reads.

    Two traps this exists for, both hit by earlier drafts: a BLOCKING read here hangs
    forever while the write end is open, and a non-blocking read can stop mid-record, so
    splitting on newline and parsing every fragment crashes on the split one.
    """

    def __init__(self, fd):
        self.fd = fd
        self.buf = ""

    def take(self):
        while True:
            try:
                chunk = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.buf += chunk.decode()
        *complete, self.buf = self.buf.split("\n")
        return [json.loads(l) for l in complete if l.strip()]


def pipe_pair():
    r, w = os.pipe()
    nonblock(r)
    return r, w, ce.emitter_from_fd(w, RED)


print("== inert emitter ==")
e = ce.emitter_from_fd(None, RED)
check("no fd -> emit False", e.emit("x") is False)
check("no fd -> nothing counted", e.count == 0)
check("no fd -> not active", e.active is False)

print("== basic records ==")
r, w, e = pipe_pair()
e.emit("member_started", member="codex", tier="voting", round=1)
e.emit("member_finished", member="codex", verdict="PASS", duration_s=12.1)
recs = Reader(r).take()
check("two records", len(recs) == 2)
check("ev preserved", [x["ev"] for x in recs] == ["member_started", "member_finished"])
check("fields round-trip", recs[1]["verdict"] == "PASS" and recs[1]["duration_s"] == 12.1)
check("count tracks acceptance", e.count == 2)
os.close(r); os.close(w)

print("== redaction reaches nested containers, dict KEYS, and str() renderings ==")


class Sneaky:
    def __str__(self):
        return SECRET


r, w, e = pipe_pair()
e.emit("round_finished", verdicts={"grok": SECRET}, notes=[SECRET, {"deep": SECRET}],
       obj=Sneaky(), keyed={SECRET: "v"})
blob = json.dumps(Reader(r).take())
check("dict value redacted", "id_rsa" not in blob)
check("list element redacted", "id_rsa" not in blob)
check("dict KEY redacted", "id_rsa" not in blob)
check("str()-rendered object redacted", "id_rsa" not in blob)
check("label kept (redaction, not deletion)", "REQUEST_EXEC" in blob)
os.close(r); os.close(w)

print("== a raising redactor is contained, not propagated ==")
r, w = os.pipe()
boom = lambda s: (_ for _ in ()).throw(RuntimeError("redactor exploded"))  # noqa: E731
e = ce.EventEmitter(w, boom)
raised = False
try:
    ok = e.emit("note", text="x")
except Exception:
    raised, ok = True, None
check("emit did not raise", raised is False)
check("returned False", ok is False)
check("reason recorded, not a silent death", "redactor exploded" in (e.disabled_reason or ""))
os.close(r); os.close(w)

print("== writability probe discriminates (os.fstat does NOT -- measured) ==")
d = tempfile.mkdtemp(); p = os.path.join(d, "f"); open(p, "w").close()
ro, wo = os.open(p, os.O_RDONLY), os.open(p, os.O_WRONLY)
check("read-only -> inert with a reason", ce.emitter_from_fd(ro, RED).disabled_reason is not None)
check("write-only -> active", ce.emitter_from_fd(wo, RED).active is True)
check("never-opened fd -> inert with a reason", ce.emitter_from_fd(9999, RED).disabled_reason is not None)
os.close(ro); os.close(wo)

print("== THE SAFETY PROPERTY: a stalled consumer must not block the fire ==")
# Falsifier: against a blocking implementation this loop never returns and the suite hangs.
r, w = os.pipe()
e = ce.emitter_from_fd(w, RED)
t0 = time.time()
for i in range(4000):
    e.emit("member_finished", member=f"m{i}", text="y" * 3000)
dt = time.time() - t0
check(f"returned promptly with nobody reading ({dt:.2f}s)", dt < 10)
check("overflow was counted, not silent", e.dropped > 0)
check("lifetime counter tracks it too", e.dropped_total >= e.dropped)
os.close(r); os.close(w)

print("== backlog forms, stream stays valid, and RESUMES on the next emit ==")
# SIZING: 40 x ~3 KB = ~124 KB. Past the 64 KB pipe (so a backlog is guaranteed) and far
# under PENDING_MAX (so nothing is dropped and resume is what is actually under test).
r, w, e = pipe_pair()
for i in range(40):
    e.emit("member_finished", member=f"m{i}", text="z" * 3000)
check("a real backlog formed (else this proves nothing)", len(e._pending) > 0)
check("nothing dropped at this size (resume, not overflow, is under test)", e.dropped == 0)
rd = Reader(r)
first = rd.take()
check(f"first drain parsed cleanly ({len(first)} records)", len(first) > 0)
pending_before = len(e._pending)
e.emit("note", text="after-drain")
seen = list(first) + rd.take()
check("backlog shrank after the next emit", len(e._pending) < pending_before)
# MEASURED, and it corrected the expectation this test was first written with: 40 records
# are 122,160 bytes and the pipe accepted only 49,843, leaving 72,347 pending -- a backlog
# LARGER than the 65,536-byte pipe. One resume emit therefore CANNOT flush it, and
# "after-drain" legitimately sits at the end of the queue. That is the emitter working, not
# failing, so the test drains in a bounded loop and asserts eventual delivery instead.
for _ in range(100):
    if not e._pending:
        break
    seen += rd.take()
    # THE DRAIN MUST BE PUMPED. _drain() runs only inside emit(), so a loop that merely
    # reads the pipe never advances _pending -- the loop would spin and both checks below
    # would fail. Reading frees pipe space; the emit is what pushes the backlog into it.
    e.emit("note", text="pump")
seen += rd.take()
check("backlog fully drained", not e._pending)
check("the resumed record eventually arrived",
      any(x.get("text") == "after-drain" for x in seen))
# Count the records UNDER TEST, not the total: the pump emits above are test scaffolding
# and inflate a bare len(). All 40 originals must survive the backlog in order.
members = [x for x in seen if x.get("ev") == "member_finished"]
check(f"all 40 original records survived the backlog ({len(members)})", len(members) == 40)
check("and in the order they were emitted",
      [x["member"] for x in members] == [f"m{i}" for i in range(40)])
os.close(r); os.close(w)

print("== loss is REPORTED to the consumer once the backlog clears ==")
r, w, e = pipe_pair()
for i in range(4000):                      # force genuine overflow
    e.emit("member_finished", member=f"m{i}", text="y" * 3000)
check("drops occurred", e.dropped > 0)
rd = Reader(r)
for _ in range(200):                       # bounded: drain until the backlog empties
    rd.take()
    if not e._pending:
        break
    e.emit("note", text="tick")
check("backlog eventually cleared", not e._pending)
lifetime_before = e.dropped_total
e.emit("note", text="post-clear")
tail = rd.take()
check("a dropped-accounting record reached the consumer",
      any(x.get("ev") == "dropped" and x.get("n", 0) > 0 for x in tail))
# THE DEFINING PROPERTY, and the reason two counters exist: confessing the arrears RESETS
# `dropped` to 0, so a run summary reading it would report "nothing lost" after real loss.
# `dropped_total` must survive that reset. Asserting it before the confession (where the
# two are trivially equal) would not discriminate.
check("arrears were reset by the confession", e.dropped == 0)
check("lifetime total SURVIVED the reset", e.dropped_total == lifetime_before > 0)
os.close(r); os.close(w)

print("== field bounding (rule 8: marker charged against the budget) ==")
r, w, e = pipe_pair()
e.emit("note", text="q" * 100_000)
rec = Reader(r).take()[0]
check("field truncated to exactly the cap", len(rec["text"]) == ce.FIELD_MAX)
check("truncation is visible, not silent", rec["text"].endswith("...[truncated]"))
os.close(r); os.close(w)

print("== records arrive BEFORE the writer exits (the entire purpose) ==")
prog = (f"import sys,time; sys.path.insert(0,{ROOT!r})\n"
        "import council_events as ce\n"
        "e=ce.emitter_from_fd(1, lambda s:s)\n"
        "for i in range(3):\n"
        "    e.emit('member_finished', member='m%d'%i); time.sleep(0.4)\n")
t0 = time.time()
pr = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE, text=True)
arrivals = [round(time.time() - t0, 2) for _ in pr.stdout]
pr.wait()
print(f"     arrival times: {arrivals}")
check("3 records", len(arrivals) == 3)
check("spread over time, not clustered at exit",
      len(arrivals) == 3 and arrivals[-1] - arrivals[0] > 0.5)

print()
print(f"FAILURES: {FAILS if FAILS else 'none'}")
sys.exit(1 if FAILS else 0)
