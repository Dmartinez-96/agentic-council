#!/usr/bin/env python3
"""run_exec_sandbox's interrupt path, against the REAL sandbox on this host.

THE LOAD-BEARING CASE IS THE QUIET ONE. Bounding the select wait to a poll interval is what
makes an abort responsive; the danger is that an empty select then reads as a TIMEOUT and
kills a command that was merely silent. codex named this in the design review before any code
existed, and reasoning is not evidence -- a build that compiles quietly for a minute must
survive. So the first check runs a command that produces NOTHING for many poll intervals and
asserts it finishes normally.

REQUIRES WORKING bubblewrap ON THIS HOST. It drives the real primitive, not a stub, because
the thing under test is the drain loop itself.
"""
# requires: bwrap
# MEASURED, NOT GREPPED (2026-08-06): run on a host with bwrap removed from PATH, this
# suite exits 1 -- it makes four real cc.run_exec_sandbox calls. A prior audit counted
# FIVE bwrap-dependent suites from a substring grep; the real number is two (this one and
# test_leader_resources). test_gui_engine, test_interrupt and test_tooling_e2e all pass
# without bwrap because they stub the sandbox rather than entering it.
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc  # noqa: E402

n = 0


def ck(cond, label):
    global n
    assert cond, f"FAILED: {label}"
    n += 1
    print(f"  [ok] {label}")


WD = Path(__file__).resolve().parent.parent
POLL = cc.EXEC_INTERRUPT_POLL_S

print(f"=== A. a QUIET command survives (poll={POLL}s) ===")
quiet_s = 3.0
t0 = time.monotonic()
text, note, info = cc.run_exec_sandbox(f"sleep {quiet_s}; echo finished",
                                       WD, should_abort=lambda: False)
dt = time.monotonic() - t0
ck(info is not None and info.get("exit_status") == 0,
   f"a command silent for {quiet_s}s ({int(quiet_s / POLL)} poll intervals) exits 0")
ck(not info.get("aborted"), "and is NOT reported aborted")
ck(not info.get("timed_out"),
   "and is NOT reported timed out -- an empty select is not a deadline")
ck("finished" in (text or ""), "its output still arrives after the silence")
ck(dt >= quiet_s, f"it really did run the full {quiet_s}s (took {dt:.1f}s)")

print("=== B. an ABORT stops a running command ===")
t0 = time.monotonic()
fired = {"n": 0}


def abort_after_a_moment():
    fired["n"] += 1
    return fired["n"] > 4          # ~1s in, well inside the 15s wall


text2, note2, info2 = cc.run_exec_sandbox("sleep 60", WD,
                                          should_abort=abort_after_a_moment)
dt2 = time.monotonic() - t0
ck(dt2 < 8, f"a `sleep 60` is stopped in {dt2:.1f}s rather than running to the wall")
ck(info2.get("aborted") is True, "info reports aborted")
ck(not info2.get("timed_out"),
   "and NOT timed out -- the operator stopping it and it running too long are different facts")
ck("ABORTED BY OPERATOR" in note2, "the note says who stopped it")

print("=== C. no predicate is exactly the old behaviour ===")
text3, note3, info3 = cc.run_exec_sandbox("echo plain", WD)
ck(info3.get("exit_status") == 0 and "plain" in (text3 or ""), "a normal command still runs")
ck(info3.get("aborted") is False, "and reports aborted=False rather than omitting the key")

print("=== D. a STEER-shaped signal must never reach this layer as an abort ===")
# should_abort is an ABORT-ONLY PREDICATE by contract; the caller buffers everything else.
# This pins the contract at the boundary: a predicate that keeps returning False cannot stop
# a command however often it is polled.
t0 = time.monotonic()
polls = {"n": 0}


def never_aborts():
    polls["n"] += 1
    return False


text4, note4, info4 = cc.run_exec_sandbox("sleep 2; echo survived", WD,
                                          should_abort=never_aborts)
ck(info4.get("exit_status") == 0 and "survived" in (text4 or ""),
   "a predicate that never returns True never kills the command")
ck(polls["n"] > 1, f"and it WAS polled repeatedly ({polls['n']} times) -- not a void check")

print(f"\nALL {n} CHECKS PASSED")
