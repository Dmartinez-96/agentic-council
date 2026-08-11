#!/usr/bin/env python3
"""ABORT and STEER. Drives the REAL run_leader_turn; only call_leader is injected."""
import sys, asyncio, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc, council_leader as cl

n = 0
def ck(c, label):
    global n
    assert c, f"FAILED: {label}"
    n += 1
    print(f"  [ok] {label}")

L = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)

def run(interrupt, replies, slow=0.0, max_rounds=4):
    wd = Path(tempfile.mkdtemp()); it = iter(replies); seen = []
    async def call(l, p, w):
        seen.append(p)
        if slow:
            await asyncio.sleep(slow)
        return {"ok": True, "text": next(it, "done"), "error": "", "transport": "t",
                "model_used": "m"}
    rec = asyncio.run(cl.run_leader_turn(L, "T", wd, ground_rules="", max_rounds=max_rounds,
                                         nonce_fn=lambda: "N", call_leader=call,
                                         interrupt=interrupt))
    return rec, seen

def after(k, sig):
    """An interrupt that fires on the k-th poll and never again."""
    c = {"n": 0}
    def f():
        c["n"] += 1
        return sig if c["n"] == k else None
    return f

print("=== A. ABORT ===")
rec, seen = run(lambda: (cl.INTERRUPT_ABORT, ""), ["never"])
ck(len(seen) == 0, "an ABORT at the first boundary issues NO model call at all")
ck(rec.interrupted and "between rounds" in rec.stop_reason, "and is recorded on the record")
t0 = time.monotonic()
rec2, _ = run(after(3, (cl.INTERRUPT_ABORT, "")), ["x"], slow=2.0)
dt = time.monotonic() - t0
ck(dt < 1.5, f"an ABORT mid-call does NOT wait for it ({dt:.2f}s against a 2.0s call)")
ck(rec2.interrupted and "abandoned" in rec2.stop_reason,
   "and says the in-flight response was abandoned rather than pretending it stopped")

print("=== B. STEER is NOT an abort -- the bug a layer-2 inspector caught ===")
rec3, seen3 = run(after(3, (cl.INTERRUPT_STEER, "use approach Y")), ["ok", "ok", "ok"], slow=2.0)
ck(not rec3.interrupted, "a STEER during an in-flight call does NOT abort the turn")
ck(rec3.steered, "it is recorded as a steer")
ck(any("use approach Y" in p for p in seen3[1:]),
   "and the operator's words reach a LATER prompt -- buffered, since the call was in flight")
ck(any(cl.ZERO_WRITE_REPROMPT in p for p in seen3[1:]),
   "a harness re-prompt in the same round does NOT overwrite the operator's message")

print("=== C. no interrupt channel is exactly the old behaviour ===")
rec4, seen4 = run(None, ["done"])
ck(not rec4.interrupted and not rec4.steered, "no interrupt -> neither flag set")
ck(len(seen4) >= 1, "and the turn runs normally")
rec5, _ = run(lambda: None, ["done"])
ck(not rec5.interrupted, "an interrupt that never fires changes nothing")



print("=== D. T2: aborting a RUNNING command ===")

def exec_turn(interrupt, exec_impl, replies):
    wd = Path(tempfile.mkdtemp()); it = iter(replies); seen = []
    ab, ae = cl._actions_sentinels("N")
    async def call(l, p, w):
        seen.append(p)
        return {"ok": True, "text": next(it, "done"), "error": "", "transport": "t",
                "model_used": "m"}
    rec = asyncio.run(cl.run_leader_turn(L, "T", wd, ground_rules="", max_rounds=4,
                                         nonce_fn=lambda: "N", call_leader=call,
                                         run_exec=exec_impl, interrupt=interrupt,
                                         apply_write=apply_write_spy))
    return rec, seen

ab, ae = cl._actions_sentinels("N")
ENV = f"{ab}\nEXEC: sleep 999\nWRITE: after.py\n" + cl._write_sentinels("N")[0] + \
      "\nx\n" + cl._write_sentinels("N")[1] + f"\n{ae}"

seen_write = {"n": 0}
def exec_aborted(cmd, workdir, **kw):
    """Stands in for run_exec_sandbox reporting an operator abort."""
    sa = kw.get("should_abort")
    fired = bool(sa and sa())          # the turn must pass an abort-only predicate
    return ("partial output", "exit -9 (ABORTED BY OPERATOR, group killed)",
            {"exit_status": -9, "timed_out": False, "aborted": fired})

def apply_write_spy(*a, **k):
    seen_write["n"] += 1
    return {"applied": True, "verdict": "PASS", "target": "/tmp/after.py"}

# CASE 1: the steer arrives at the ROUND BOUNDARY, so it IS delivered (round 0's prompt
# carries it) and the abort follows during the action. Delivered means delivered.
seq = [(cl.INTERRUPT_STEER, "try the other approach"), (cl.INTERRUPT_ABORT, "")]
it2 = iter(seq)
rec6, seen6 = exec_turn(lambda: next(it2, None), exec_aborted, [ENV, "done"])
ck(rec6.interrupted, "an aborted EXEC ends the turn")
ck("during an action" in rec6.stop_reason, "and the stop_reason says it was during an action")
ck(seen_write["n"] == 0,
   "the WRITE that FOLLOWED the aborted EXEC never ran -- the batch stops")
ck(any(r.kind == "aborted" for r in rec6.results),
   "and the abort sentinel is on the record, so the stop is structural not inferred")
ck(rec6.steered and any("try the other approach" in p for p in seen6),
   "a boundary STEER reached that round's prompt, so it counts as delivered")
ck(not any("NOT DELIVERED" in n for r in rec6.rounds for n in r["notes"]),
   "and is NOT labelled undelivered, because it was delivered")

# CASE 2: the steer arrives DURING the action and the same batch aborts, so no further prompt
# is ever assembled. The channel already consumed it, so it must survive in the RECORD.
def exec_steer_then_abort(cmd, workdir, **kw):
    sa = kw.get("should_abort")
    if not sa:
        return ("out", "exit 0", {"exit_status": 0, "timed_out": False, "aborted": False})
    sa()                      # first poll: a STEER, buffered, must NOT abort
    fired = bool(sa())        # second poll: the ABORT
    return ("partial", "exit -9 (ABORTED BY OPERATOR, group killed)",
            {"exit_status": -9, "timed_out": False, "aborted": fired})

it4 = iter([None, (cl.INTERRUPT_STEER, "switch to plan B"), (cl.INTERRUPT_ABORT, "")])
rec8, seen8 = exec_turn(lambda: next(it4, None), exec_steer_then_abort, [ENV, "done"])
ck(rec8.interrupted, "the second poll's ABORT stops the batch")
notes8 = [n for r in rec8.rounds for n in r["notes"]]
ck(any("switch to plan B" in n for n in notes8),
   "a STEER consumed DURING the action survives in the ROUND RECORD, not silently dropped")
ck(any("NOT DELIVERED" in n for n in notes8),
   "and is labelled undelivered, since the abort meant no further prompt was assembled")
ck(not rec8.steered,
   "`steered` stays False -- its definition is 'reached a prompt', and this one did not")

def exec_clean(cmd, workdir, **kw):
    sa = kw.get("should_abort")
    if sa:
        sa()                           # a steer here must NOT abort
    return ("out", "exit 0", {"exit_status": 0, "timed_out": False, "aborted": False})

it3 = iter([(cl.INTERRUPT_STEER, "keep going but use Y")])
rec7, seen7 = exec_turn(lambda: next(it3, None), exec_clean, [ENV, "done"])
ck(not rec7.interrupted, "a STEER during a running command does NOT abort it")
ck(rec7.steered and any("keep going but use Y" in p for p in seen7),
   "and reaches a prompt instead -- the boundary poll delivers it to THAT round, not a later "
   "one, which is why this checks every prompt rather than seen[1:]")

print(f"\nALL {n} CHECKS PASSED")
