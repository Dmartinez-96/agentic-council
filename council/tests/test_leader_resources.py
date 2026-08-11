#!/usr/bin/env python3
# requires: bwrap
# MEASURED 2026-08-06: with bwrap removed from PATH this suite scores 7/16 rather than
# failing to start, so the dependency is real but PARTIAL -- the non-sandbox checks still
# pass. It is declared here anyway: a suite that reports 7/16 on a host without bwrap
# teaches a newcomer the project is broken, which is exactly what the skip exists to
# prevent.
"""END-TO-END: does a LEADER TURN actually reach the GPU, and does scratch persist?

THIS IS THE FILE THAT ANSWERS ITEM 1, and it exists because everything else about this work
was a mechanism test. `probe_exec_profiles.py` proves `run_exec_sandbox` can reach the GPU
when handed a profile; that says NOTHING about whether a leader ever gets one. A parameter
with a default can thread three layers and arrive as None while every unit test stays green.
So this drives the REAL `run_leader_turn` -- real `run_leader_actions`, real
`run_exec_sandbox`, real bubblewrap -- with only the MODEL stubbed, because a stubbed model
is the one thing that cannot affect whether a device is reachable.

WHAT IS STUBBED AND WHY IT DOES NOT WEAKEN THE RESULT: `call_leader` returns a canned actions
envelope, and `apply_write` is never exercised. The stub sits ABOVE the code under test, not
inside it -- the entire path from `run_leader_turn` down through bwrap is production code.

WHAT A GREEN RUN DOES NOT ESTABLISH: that a real model would CHOOSE to emit these actions.
That is task-dependent behaviour and this file says nothing about it.

    python3 council/tests/test_leader_resources.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc  # noqa: E402
import council_leader as cl  # noqa: E402

P = []
SKIPPED = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


LEADER = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)
WD = Path(tempfile.mkdtemp(prefix="leadres_wd_"))
(WD / "app.py").write_text("print('hi')\n")
# Bound HERE, not inside the section that uses it: the cleanup loop at the end names both,
# and an exception before the scratch section would have left this unbound and turned
# teardown into a NameError that leaks the workdir.
SCRATCH = Path(tempfile.mkdtemp(prefix="leadres_scratch_"))


def scripted(commands):
    """A model that emits one EXEC per round from `commands`, then a final answer."""
    state = {"i": 0}

    async def call_leader(leader, prompt, cwd):
        i = state["i"]
        state["i"] += 1
        if i >= len(commands):
            return {"ok": True, "text": "done", "error": "", "transport": "stub",
                    "model_used": "stub"}
        nonce = prompt.rsplit("BEGIN ACTIONS ", 1)[1].split(" ", 1)[0]
        return {"ok": True, "error": "", "transport": "stub", "model_used": "stub",
                "text": (f"--- BEGIN ACTIONS {nonce} ---\n"
                         f"EXEC: {commands[i]}\n"
                         f"--- END ACTIONS {nonce} ---\n")}
    return call_leader


def turn(commands, **kw):
    events = []
    rec = asyncio.run(cl.run_leader_turn(
        LEADER, "task", WD, ground_rules="", call_leader=scripted(commands),
        on_event=lambda name, **f: events.append((name, f)), **kw))
    return rec, events


# ---------------------------------------------------------------------------
print("== a turn with NO elevation reaches no GPU (the default a leader gets) ==")
# THE PROBE IS ctypes ON libcuda, NOT `nvidia-smi`, and the difference decides whether this
# control means anything: `nvidia-smi -L || echo NO_GPU` reports a MISSING BINARY exactly the
# way it reports a missing device, so it cannot tell the two apart. This probe attempts the
# driver call itself.
# WHAT THE RUN ACTUALLY SHOWED, replacing what I assumed. I predicted the library would load
# here and cuInit would merely fail. It does not load at all: libcuda.so.1 lives in
# /usr/lib/wsl/lib, and only the GPU profile puts that directory on LD_LIBRARY_PATH, so the
# default sandbox raises OSError from CDLL. Either outcome is the SAME property -- CUDA does
# not come up unelevated -- so that is what the assertion states, and it prints which of the
# two mechanisms occurred rather than pretending to have predicted one.
# WHAT THE FAILURE CODE DOES *NOT* TELL YOU, so the assertion below does not claim it: the
# default profile withholds the device AND imposes RLIMIT_AS=512MB, and a nonzero cuInit
# could come from either (measured earlier this session: 512MB alone yields
# CUDA_ERROR_OUT_OF_MEMORY on a host that DOES have the device). The control asserts only
# that CUDA does not come up unelevated -- which is the property being controlled for.
# ONE LINE, and that is a grammar constraint rather than a style choice: `EXEC:` is parsed
# per-line, so a command containing a newline is TRUNCATED at it. The first version of this
# probe was a multi-line python -c and reached the shell as `python3 -c 'import ctypes`,
# which failed with "Unterminated quoted string" -- a broken probe that would have been easy
# to misread as a GPU result. A missing library surfaces as an OSError traceback with no
# cuInit line, which is the trade for staying on one line. The assertion keys on the ABSENCE
# of "cuInit 0" -- the one string that would mean CUDA came up -- and reports which of the two
# failure mechanisms occurred without asserting either.
CUPROBE = ('python3 -c "import ctypes; '
           'print(\'cuInit\', ctypes.CDLL(\'libcuda.so.1\').cuInit(0))"')
rec, _ev = turn([CUPROBE])
out = "\n".join(r.content for r in rec.results)
check("TurnRecord.results is populated (the field whose absence made the event loop dead), "
      "with exactly one exec result",
      len(rec.results) == 1 and rec.results[0].kind == "exec")
how = ("the driver library did not even load" if "cuInit" not in out
       else "the library loaded but cuInit failed")
check(f"CONTROL: CUDA does not come up unelevated -- {how} ({out.strip()[:50]!r})",
      bool(rec.results) and rec.results[0].ok and "cuInit 0" not in out)

print("== a turn WITH the elevated GPU profile reaches the real device ==")
prof = cc.elevated_exec_profile(gpu=True)
ok, why = cc.exec_profile_preflight(prof)
if not ok:
    # A SKIP MUST NOT LOOK LIKE A PASS. This file's whole claim is that a leader reaches the
    # GPU; if that cannot be tested here, the run is INCONCLUSIVE, not green. Recording a
    # False keeps the exit status honest on a GPU-less host.
    SKIPPED.append("GPU legs")
    print(f"  [skip] GPU legs -- {why}")
    check("GPU legs could not run on this host, so this file did NOT establish its claim",
          False)
else:
    rec, events = turn(["nvidia-smi -L"], profile=prof)
    out = "\n".join(r.content for r in rec.results)
    check(f"a LEADER TURN sees the GPU end to end ({out.strip()[:60]!r})", "GPU 0" in out)
    check("the action succeeded rather than being denied", rec.results[0].ok)
    # The compute stack, not just the NVML query -- cuInit is what RLIMIT_AS broke.
    rec, _ev = turn(['python3 -c \'import ctypes;'
                     'l=ctypes.CDLL("libcuda.so.1");print("cuInit",l.cuInit(0))\''],
                    profile=prof)
    check(f"CUDA initialises inside a leader turn "
          f"({''.join(r.content for r in rec.results).strip()[:30]!r})",
          "cuInit 0" in "".join(r.content for r in rec.results))

print("== ONE scratch directory persists ACROSS ROUNDS of a turn ==")
M = cc.EXEC_SCRATCH_MOUNT
rec, events = turn([f"echo ROUND0_WROTE_THIS > {M}/state.txt",
                    f"cat {M}/state.txt"], scratch=SCRATCH)
joined = "\n".join(r.content for r in rec.results)
check("round 1 reads back what round 0 wrote to scratch (state survives the round boundary)",
      "ROUND0_WROTE_THIS" in joined)
check("and the file is really on the host afterwards, not just inside a sandbox",
      (SCRATCH / "state.txt").is_file())
check("TurnRecord records the scratch path", rec.scratch == str(SCRATCH))
# THE CONTROL that makes the above mean something: without scratch, round 1 finds nothing,
# because /work is a fresh ephemeral copy every single call.
rec2, _ev = turn([f"echo X > {M}/state.txt 2>&1 || echo NO_SCRATCH_MOUNT",
                  f"cat {M}/state.txt 2>&1 || echo NOTHING_TO_READ"])
j2 = "\n".join(r.content for r in rec2.results)
check(f"CONTROL: with no scratch, there is no {M} at all and nothing persists",
      "ROUND0_WROTE_THIS" not in j2 and ("NO_SCRATCH_MOUNT" in j2 or "NOTHING_TO_READ" in j2))
# And the workdir itself is untouched by any of it -- the only path to the repo is a WRITE.
check("the real workdir is unchanged by everything above",
      (WD / "app.py").read_text() == "print('hi')\n")

print("== scratch aimed at the workdir is REFUSED, through the whole leader stack ==")
rec3, _ev = turn(["echo SHOULD_NOT_RUN"], scratch=WD)
n3 = "\n".join(r.note for r in rec3.results)
check(f"a leader turn cannot point scratch at the tree under review ({n3[:70]!r})",
      not rec3.results[0].ok and "DENIED" in n3)

print("== the LIVE event stream carries the turn as it happens ==")
rec4, events = turn(["echo LIVE_A", "echo LIVE_B"])
names = [n for n, _f in events]
check(f"rounds, text and actions all streamed ({names})",
      names.count("leader_round") >= 2 and names.count("leader_text") >= 2
      and names.count("leader_action") == 2)
# ORDER is the property that makes it a transcript rather than a dump, and it is checked
# PER ROUND rather than once. `names.index(...)` would compare only the FIRST occurrence of
# each name, so a later round emitting its action before its text would sail through.
# Pairing is by the event's own `round` field, not by position: the LAST round emits a
# leader_text (the final answer) and no action at all, so any length- or zip-based pairing is
# off by one on the happy path -- which is exactly how the previous version of this check
# went red.
text_at = {f["round"]: i for i, (n, f) in enumerate(events) if n == "leader_text"}
act_at = {}
for i, (n, f) in enumerate(events):
    if n == "leader_action":
        act_at.setdefault(f["round"], i)
check(f"EVERY round with an action emitted its text FIRST (text {text_at}, actions {act_at})",
      act_at and all(r in text_at and text_at[r] < a for r, a in act_at.items()))
act = [f for n, f in events if n == "leader_action"][0]
check(f"an action event carries kind/target/ok/note ({act})",
      act.get("action") == "exec" and act.get("ok") is True and act.get("note"))
check("action events carry the metadata NOTE, never the retrieved CONTENT",
      all("content" not in f for n, f in events if n == "leader_action"))
# A dead consumer must not be able to kill the turn.
boom_rec = asyncio.run(cl.run_leader_turn(
    LEADER, "t", WD, ground_rules="", call_leader=scripted(["echo STILL_RAN"]),
    on_event=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("consumer died"))))
check("a callback that RAISES does not fail the turn",
      "STILL_RAN" in "".join(r.content for r in boom_rec.results))

for d in (WD, SCRATCH):
    shutil.rmtree(d, ignore_errors=True)
print()
if SKIPPED:
    print(f"SKIPPED: {SKIPPED}")
print(f"=== leader resources end-to-end: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
