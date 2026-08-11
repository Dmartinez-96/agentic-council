"""Deterministic regression for council_leader.run_leader_turn (driver piece b3).

Drives the whole turn loop with INJECTED seams -- a scripted call_leader, a controllable
nonce_fn, and fake read/fetch/apply_write -- no live model or council calls. The checks are
built to FALSIFY the three fixes the design review demanded, not merely to pass:
  - ACCUMULATION across >2 rounds: round-1 content must still be in the round-3 prompt
    (a carry-only-previous bug would drop it -> the check fails).
  - FRESH per-round nonce / REPLAY: nonces differ per round; a later round that echoes an
    EARLIER round's (now-stale) envelope must NOT re-execute (nonce scoping rejects it).
  - CUMULATIVE caps across rounds: a shared budget bounds BOTH the write count and (a
    non-write path) the read count over the whole turn.
  - OVERFLOW is refused whole AND the rejection is delivered into the NEXT round's prompt.
  - RE-INJECTION, leader-call failure, exfil context, round cap.

Re-run:  python3 council/tests/test_leader_turn.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc
import council_leader as cl

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


WD = Path("/tmp")   # no real IO -- all primitives are injected seams
LEADER = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)


def env_for(nonce, *lines):
    ab, ae = cl._actions_sentinels(nonce)
    return ab + "\n" + "\n".join(lines) + "\n" + ae


def write_lines(nonce, path, body):
    cb, ce = cl._write_sentinels(nonce)
    return [f"WRITE: {path}", cb, body, ce]


def make_leader(responses, prompts):
    idx = {"i": 0}

    async def _call(leader, prompt, cwd):
        prompts.append(prompt)
        text = responses[min(idx["i"], len(responses) - 1)]
        idx["i"] += 1
        return {"ok": True, "text": text, "error": "", "transport": "test", "model_used": "m"}
    return _call


def seq_nonce(names):
    """Fixed nonces, then DISTINCT synthetic ones once the list runs out.

    The list used to be exhaustive and `next(it)` raised when a turn took one more round than
    the test author expected. The INTENT re-prompt now costs exactly that extra round on any
    turn that declares no intent, so every fixed list here was one short.
    THE EXTRAS ARE DISTINCT, NOT A REPEAT OF THE LAST: a fresh per-round nonce is what stops
    an envelope echoed from an earlier round replaying, and a test helper that quietly reused
    one would be testing a weaker harness than the one that ships.
    """
    it = iter(names)
    extra = {"n": 0}

    def nxt():
        try:
            return next(it)
        except StopIteration:
            extra["n"] += 1
            return f"XTRA{extra['n']:03d}"
    return nxt


def zw_prompts(prompts):
    """Prompts EXCLUDING those issued by the INTENT re-prompt.

    THIS FILE TESTS THE ZERO-WRITE MECHANISM, and a raw prompt count is a PROXY for it. A
    second, unrelated re-prompt (INTENT, added for multi-turn conversation) costs one extra
    round on any turn that declares no intent, which moved every total here by one while the
    mechanism under test was unchanged -- verified: `reprompted` and the delivered-notice
    counts were all still correct. Counting the mechanism instead of the total is the fix;
    bumping the expected numbers would have made the tests pass while leaving them measuring
    something they do not mean.
    """
    return [p for p in prompts if cl.INTENT_REPROMPT not in p]


def turn(task, **kw):
    return asyncio.run(cl.run_leader_turn(LEADER, task, WD, **kw))


# ---------------------------------------------------------------------------
# 1. ACCUMULATION over 3 rounds + RE-INJECTION (distinct nonces per round)
# ---------------------------------------------------------------------------
prompts = []
reads = []


def read_ab(wd, p):
    reads.append(p)
    return ({"a.py": "CONTENT-A", "b.py": "CONTENT-B"}.get(p, "?"), f"{len(p)} bytes")


# FOUR nonces, not three, and the reason is the zero-write re-prompt: this turn READs twice
# and never WRITEs, so its round-2 "All done." is sent back once before it is accepted. The
# 4th round is that re-prompt's reply (make_leader repeats its last response). A 3-nonce
# sequence raised StopIteration here, which is how the new mechanism announced itself.
rec = turn("TASK-XYZ", ground_rules="RULES-ABC", max_rounds=5,
           nonce_fn=seq_nonce(["N0", "N1", "N2", "N3"]),
           call_leader=make_leader([env_for("N0", "READ: a.py"),
                                    env_for("N1", "READ: b.py"),
                                    "All done."], prompts),
           read=read_ab)
check("four rounds (two READ, a re-prompted end, then the final answer)",
      len([r for r in rec.rounds if not any("no INTENT" in n for n in r["notes"])]) == 4
      and rec.final_text == "All done.")
check("each round's READ executed once, in order", reads == ["a.py", "b.py"])
check("ACCUMULATION: round-3 prompt carries BOTH round-1 and round-2 content "
      "(not just the previous round)",
      "CONTENT-A" in prompts[2] and "CONTENT-B" in prompts[2])
check("RE-INJECTION: every prompt carries ground rules + task",
      all("RULES-ABC" in p and "TASK-XYZ" in p for p in prompts))
check("each prompt carries THAT round's distinct nonce",
      "N0" in prompts[0] and "N1" in prompts[1] and "N2" in prompts[2])


# ---------------------------------------------------------------------------
# 2. REPLAY: a later round echoing an EARLIER round's stale envelope is ignored
# ---------------------------------------------------------------------------
rreads = []
stale = env_for("P0", "READ: a.py")     # built with round-0's nonce
# THREE nonces for the same reason section 1 needs four: the replayed envelope does not parse
# under P1, so round 1 lands on the no-actions path having never WRITTEN, and the zero-write
# re-prompt spends one more round before the reply is accepted as final.
rec = turn("T", max_rounds=5, nonce_fn=seq_nonce(["P0", "P1", "P2"]),
           call_leader=make_leader([stale, stale], []),   # round 1 REPLAYS the P0 envelope
           read=lambda wd, p: (rreads.append(p) or ("x", "1 byte")))
check("replay guard: the stale-nonce envelope in round 2 does NOT re-execute the read",
      rreads == ["a.py"])
check("replay guard: the unmatched envelope is treated as a (final) non-action response",
      rec.final_text == stale and "final answer" in rec.stop_reason)


# ---------------------------------------------------------------------------
# 3. leader call failure stops the turn
# ---------------------------------------------------------------------------
async def failing(leader, prompt, cwd):
    return {"ok": False, "text": "", "error": "boom", "transport": "t", "model_used": ""}


rec = turn("T", nonce_fn=lambda: "X", call_leader=failing)
check("failed leader call -> stop, no final answer, error surfaced",
      "leader call failed" in rec.stop_reason and rec.final_text == "" and "boom" in rec.stop_reason)


# ---------------------------------------------------------------------------
# 4. OVERFLOW refused whole AND the rejection reaches the NEXT round's prompt
# ---------------------------------------------------------------------------
oprompts = []
oreads = []
over_env = env_for("OV", *[f"READ: f{i}.py" for i in range(cl.LEADER_MAX_ACTIONS_PER_TURN + 3)])
rec = turn("T", max_rounds=5, nonce_fn=lambda: "OV",
           call_leader=make_leader([over_env, "done"], oprompts),
           read=lambda wd, p: (oreads.append(p) or ("x", "1 byte")))
check("overflow: NO read executed (truncated set refused whole)", oreads == [])
check("overflow: the rejection is DELIVERED into the next round's prompt",
      len(oprompts) >= 2 and "REJECTED and none ran" in oprompts[1])


# ---------------------------------------------------------------------------
# 5. CUMULATIVE WRITE cap across rounds (shared budget)
# ---------------------------------------------------------------------------
applies = []


def fake_apply(leader, path, content, wd, *, session_id="", transcript_path="", review=None):
    applies.append(path)
    return {"applied": True, "verdict": "PASS", "target": path, "review": "ok"}


six = env_for("W0", *sum((write_lines("W0", f"f{i}.py", "b") for i in range(6)), []))
turn("T", max_rounds=2, nonce_fn=lambda: "W0",
     call_leader=make_leader([six, six], []), apply_write=fake_apply)
check("cumulative WRITE cap: 6 writes/round x 2 rounds -> only LEADER_MAX_WRITES_PER_TURN applied",
      len(applies) == cl.LEADER_MAX_WRITES_PER_TURN)


# ---------------------------------------------------------------------------
# 6. CUMULATIVE READ-count cap across rounds (a NON-write path)
# ---------------------------------------------------------------------------
rcount = {"n": 0}


def counting_read(wd, p):
    rcount["n"] += 1
    return ("x", "1 byte")


full = env_for("R0", *[f"READ: f{i}.py" for i in range(cl.LEADER_MAX_ACTIONS_PER_TURN)])
rec = turn("T", max_rounds=2, nonce_fn=lambda: "R0",
           call_leader=make_leader([full, full], []), read=counting_read)
check("cumulative READ cap: reads granted across rounds stop at the turn read cap",
      rcount["n"] == cl.LEADER_TURN_TOOL_CAPS["read"][0])
check("cumulative READ cap: over-cap reads are EXPLICITLY denied (note recorded, not skipped)",
      any("read cap" in n for r in rec.rounds for n in r["notes"]))


# ---------------------------------------------------------------------------
# 7. EXFIL context forwarded to fetch spans ground_rules + prior_handoff + task
# ---------------------------------------------------------------------------
seen = {}


def fake_fetch(url, ctx):
    seen["ctx"] = ctx
    return ("PAGE", "status 200")


turn("TASK-Q", ground_rules="RULES-Z", prior_handoff="HANDOFF-Y", nonce_fn=lambda: "F0",
     call_leader=make_leader([env_for("F0", "FETCH: https://docs.python.org/3/"), "done"], []),
     fetch=fake_fetch)
check("exfil_context spans ground_rules + handoff + task (not just task)",
      all(s in seen.get("ctx", "") for s in ("RULES-Z", "HANDOFF-Y", "TASK-Q")))


# ---------------------------------------------------------------------------
# 7b. EXFIL context also folds in PRIOR tool-result content (READ then FETCH)
# ---------------------------------------------------------------------------
ectx = {}


def fetch_capture(url, ctx):
    ectx["ctx"] = ctx
    return ("PAGE", "status 200")


turn("T", max_rounds=3, nonce_fn=seq_nonce(["E0", "E1", "E2"]),
     call_leader=make_leader([env_for("E0", "READ: s.py"),
                              env_for("E1", "FETCH: https://docs.python.org/3/"),
                              "done"], []),
     read=lambda wd, p: ("SECRET-READ-BODY-42", "18 bytes"),
     fetch=fetch_capture)
check("exfil_context also includes PRIOR tool-result content (a round-1 READ reaches a "
      "round-2 FETCH's anti-exfil comparison)",
      "SECRET-READ-BODY-42" in ectx.get("ctx", ""))


# ---------------------------------------------------------------------------
# 8. round cap: a non-terminating leader stops at max_rounds
# ---------------------------------------------------------------------------
rec = turn("T", max_rounds=3, nonce_fn=lambda: "L0",
           call_leader=make_leader([env_for("L0", "READ: loop.py")], []),
           read=lambda wd, p: ("x", "1 byte"))
check("round cap: a non-terminating leader stops at max_rounds",
      len(rec.rounds) == 3 and "round cap" in rec.stop_reason and rec.final_text == "")


# ---------------------------------------------------------------------------
# 9. THE ZERO-WRITE RE-PROMPT. A turn that ends with no envelope having never emitted a
#    WRITE is sent back exactly once. Every check below is built to FALSIFY a specific way
#    the mechanism could be wrong -- not merely to observe that it fired.
# ---------------------------------------------------------------------------
zprompts = []
rec = turn("T", max_rounds=5, nonce_fn=seq_nonce(["Z0", "Z1"]),
           call_leader=make_leader(["I cannot write; my sandbox is read-only."], zprompts))
check("no WRITE all turn -> the notice is DELIVERED into the next round's prompt",
      len(zw_prompts(zprompts)) == 2 and cl.ZERO_WRITE_REPROMPT in zprompts[1])
check("the notice is absent from the FIRST prompt (it is not injected unconditionally)",
      cl.ZERO_WRITE_REPROMPT not in zprompts[0])
check("re-prompt is recorded machine-readably AND in the stop_reason",
      rec.reprompted is True and "re-prompt" in rec.stop_reason)
check("the re-prompted round is in the record with its NOTICE note",
      any("no WRITE" in n for r in rec.rounds for n in r["notes"])
      and any("no WRITE" in n for n in rec.rounds[0]["notes"]))

# FIRES ONCE, NOT PER ROUND: the leader refuses again, and the turn must end rather than
# loop. A missing `reprompted` guard would spend every remaining round here.
z2 = []
rec = turn("T", max_rounds=6, nonce_fn=seq_nonce(["Y0", "Y1", "Y2", "Y3"]),
           call_leader=make_leader(["still no."], z2))
check("fires at most ONCE per turn (a second no-action reply ends the turn)",
      len(zw_prompts(z2)) == 2 and rec.final_text == "still no.")
check("the notice is not repeated in any later prompt",
      sum(1 for p in z2 if cl.ZERO_WRITE_REPROMPT in p) == 1)

# A TURN THAT DID WRITE IS LEFT ALONE -- the trigger is the absence of a WRITE, so a
# condition inverted here would re-prompt every leader that finished its work.
w = []
wenv = env_for("W1", *write_lines("W1", "done.py", "x"))
rec = turn("T", max_rounds=5, nonce_fn=seq_nonce(["W1", "W2"]),
           call_leader=make_leader([wenv, "finished."], w), apply_write=fake_apply)
check("a turn that DID emit a WRITE is not re-prompted",
      rec.reprompted is False and rec.final_text == "finished." and len(zw_prompts(w)) == 2
      and not any(cl.ZERO_WRITE_REPROMPT in p for p in w))

# NEVER EXTENDS max_rounds: on the last available round there is nothing to spend, so the
# reply is accepted as final. WHAT THE GUARD ACTUALLY PREVENTS, measured by running a copy of
# council_leader with `i < max_rounds - 1` removed rather than reasoned about: the prompt
# count does NOT change (there is no second iteration in range(1) to reach). What changes is
# that the turn sets reprompted, continues, falls out of the loop and returns
# stop_reason='hit round cap (1) without a final answer' with final_text='' -- it DISCARDS the
# leader's answer. That is why this check asserts final_text, not just the count.
one = []
rec = turn("T", max_rounds=1, nonce_fn=seq_nonce(["Q0"]),
           call_leader=make_leader(["cannot."], one))
check("does NOT fire on the last round (max_rounds is never extended)",
      len(one) == 1 and rec.reprompted is False and rec.final_text == "cannot.")

# AN OVERFLOW-REFUSED ENVELOPE'S WRITES DO NOT COUNT. None of them ran, so the turn still
# has never put a write through the harness and the re-prompt is still owed. This is the
# check that fails if `wrote` is set before the overflow branch.
ov = []
ov_env = env_for("V0", *sum((write_lines("V0", f"g{i}.py", "b")
                             for i in range(cl.LEADER_MAX_ACTIONS_PER_TURN + 2)), []))
rec = turn("T", max_rounds=5, nonce_fn=seq_nonce(["V0", "V1", "V2"]),
           call_leader=make_leader([ov_env, "giving up."], ov), apply_write=fake_apply)
check("a WRITE refused by OVERFLOW does not count as having used the write path",
      rec.reprompted is True and any(cl.ZERO_WRITE_REPROMPT in p for p in ov))

print(f"\n=== run_leader_turn: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
