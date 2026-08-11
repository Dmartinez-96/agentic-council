#!/usr/bin/env python3
"""Write reconciliation + the CLAIMS block. ASSERTS rather than prints -- the probes beside
this file print booleans and exit 0 either way, so they illustrate; this one fails."""
import sys, asyncio, tempfile, pathlib, hashlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import consult_council as cc, council_leader as cl

L = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)
N = "NONCE"
ab, ae = cl._actions_sentinels(N); cb, ce = cl._write_sentinels(N)
kb, ke = cl._claims_sentinels(N)
W = lambda p, b: f"WRITE: {p}\n{cb}\n{b}\n{ce}"
n = 0
def ck(cond, label):
    global n
    assert cond, f"FAILED: {label}"
    n += 1
    print(f"  [ok] {label}")

def turn(replies, review=None, wd=None):
    wd = wd or pathlib.Path(tempfile.mkdtemp(prefix="wd-"))
    it = iter(replies)
    async def call(l, p, w):
        # A DEFAULT, not next(it): the INTENT re-prompt costs one extra round on any turn that
        # does not declare intent, so a fixed reply list now under-runs. The filler ends the
        # turn without declaring anything, which is exactly the shape being tested.
        return {"ok": True, "text": next(it, "Nothing further."), "error": "",
                "transport": "t", "model_used": "m"}
    rev = review or (lambda *a, **k: (0, "VERDICT: PASS\nok", ""))
    return asyncio.run(cl.run_leader_turn(L, "T", wd, ground_rules="", max_rounds=4,
                                          nonce_fn=lambda: N, call_leader=call, review=rev)), wd

block_no = lambda pitch, target, workdir, **k: (
    (2, "VERDICT: BLOCK\nno", "") if "no.py" in target else (0, "VERDICT: PASS\nok", ""))

print("=== A. capture: target + sha256 reach TurnRecord ===")
rec, wd = turn([f"{ab}\n{W('good.py','body')}\n{W('no.py','x')}\n{W('../esc.py','y')}\n{ae}",
                "done"], block_no)
ws = {r.arg: r for r in rec.results if r.kind == "write"}
ck(ws['good.py'].target.endswith("good.py") and ws['good.py'].target.startswith(str(wd)),
   "applied write carries the RESOLVED absolute target")
ck(ws['good.py'].sha256 == hashlib.sha256(b"body").hexdigest(),
   "applied write hash matches the bytes requested")
ck(hashlib.sha256((wd/"good.py").read_bytes()).hexdigest() == ws['good.py'].sha256,
   "and matches the file actually on disk")
ck(ws['no.py'].sha256 == "", "a BLOCKED write is hashed as ''  (nothing reached disk)")
ck(ws['../esc.py'].target == "../esc.py",
   "a jail denial falls back to the raw path, never a silent None")

print("=== B. reconcile_writes ===")
ck(rec.writes["requested"] == ("good.py", "no.py", "../esc.py"), "requested lists every WRITE")
ck([u["path"] for u in rec.writes["unapplied"]] == ["no.py", "../esc.py"], "unapplied lists the rest")
ck("verdict=BLOCK" in rec.writes["unapplied"][0]["note"], "unapplied carries WHY")
ck(rec.writes["altered"] == (), "an intact applied write is not flagged altered")
rec2, wd2 = turn([f"{ab}\n{W('a.py','v1')}\n{W('a.py','v2')}\n{ae}", "done"])
ck(rec2.writes["altered"] == (), "rewriting the SAME path twice is not a false positive")
(wd/"good.py").write_text("TAMPERED")
alt = cl.reconcile_writes(rec.results)["altered"]
ck(len(alt) == 1 and alt[0]["expected"] != alt[0]["found"], "out-of-band tampering IS detected")
(wd/"good.py").unlink()
ck(cl.reconcile_writes(rec.results)["altered"][0]["found"] == "", "deletion is detected as absent")

print("=== C. parse_claims ===")
body = f"prose\n{kb}\nCLAIMED: a.py\nnot a claim line\nCLAIMED: a.py\nCLAIMED: b.py\n{ke}\n"
ck(cl.parse_claims(body, N) == ["a.py", "b.py"], "parses, ignores prose, deduplicates")
ck(cl.parse_claims(body, "OTHER") is None,
   "a STALE NONCE cannot replay a claims block (None: no block for THIS nonce exists)")
ck(cl.parse_claims("no block here", N) is None, "an ABSENT block yields None ('said nothing')")
ck(cl.parse_claims(f"{kb}\n{ke}", N) == [], "an EMPTY block yields [] ('declares nothing')")
ck(cl.parse_claims("no block here", N) != cl.parse_claims(f"{kb}\n{ke}", N),
   "absent and empty are DISTINGUISHABLE -- without this a leader cannot retract")
ck(cl.parse_claims(f"{kb}\nCLAIMED: x.py\n", N) is None, "an UNCLOSED block yields None")
# THE TYPO-RETRACTION HAZARD (codex, in dialogue): unrecognized lines are skipped, so a block
# of nothing but `CLAIM:` would parse to [] and silently withdraw real claims.
_p = []
ck(cl.parse_claims(f"{kb}\nCLAIM: a.py\n{ke}", N, _p) is None,
   "a junk-only block is MALFORMED, not empty -- a typo cannot retract")
ck(_p and "no CLAIMED: line parsed" in _p[0], "and the malformed block is REPORTED, not silent")
ck(cl.parse_claims(f"{kb}\n\n   \n{ke}", N) == [],
   "a genuinely BLANK block still retracts -- the deliberate case survives the guard")
ck(cl.parse_claims(f"{kb}\nprose\nCLAIMED: a.py\n{ke}", N) == ["a.py"],
   "junk ALONGSIDE a valid line is still ignored, as documented")

print("=== D. verify_claims: the four states ===")
rec3, wd3 = turn([f"{ab}\n{W('good.py','ok')}\n{W('no.py','x')}\n{ae}",
                  f"Done.\n{kb}\nCLAIMED: good.py\nCLAIMED: no.py\nCLAIMED: ghost.py\n{ke}"],
                 block_no)
st = {c["claim"]: c["status"] for c in rec3.claims}
ck(st["good.py"] == cl.CLAIM_VERIFIED, "applied + intact -> VERIFIED")
ck(st["no.py"] == cl.CLAIM_CONTRADICTED, "requested but BLOCKED -> CONTRADICTED")
ck(st["ghost.py"] == cl.CLAIM_UNSUBSTANTIATED, "never requested -> UNSUBSTANTIATED")
(wd3/"good.py").write_text("changed")
ck(cl.verify_claims(["good.py"], rec3.results, wd3)[0]["status"] == cl.CLAIM_ALTERED,
   "applied then changed underneath -> ALTERED")
ck(cl.verify_claims([str(wd3/"good.py")], rec3.results, wd3)[0]["status"] == cl.CLAIM_ALTERED,
   "an ABSOLUTE path inside the workdir matches the same write")
ck(cl.verify_claims(["/etc/passwd"], rec3.results, wd3)[0]["status"] == cl.CLAIM_UNSUBSTANTIATED,
   "a path OUTSIDE the workdir can never match a write")

print("=== E. wiring: it arrives on the record, and absence is not verification ===")
ck(len(rec3.claims) == 3, "claims arrive on TurnRecord from run_leader_turn")
rec4, _ = turn([f"{ab}\n{W('z.py','ok')}\n{ae}", "Done, no claims block."])
ck(rec4.claims == () and rec4.writes["applied"], "absent block -> (), NOT a verified claim")
ck(kb in cl._action_grammar_instructions(N), "the leader is TOLD the claims grammar")
ck("--- BEGIN ACTIONS " + N in cl._action_grammar_instructions(N),
   "and the actions grammar still survives beside it")



print("=== F. trace capture + discrepancy gate (item 2) ===")
import consult_council as _cc
ERRLINE = "ERROR codex_core::tools::router: error=patch rejected: writing is blocked"
big = "banner\n" + ("prompt echo line\n" * 5000) + ERRLINE + "\n"
sl = _cc.leader_trace_slice(big)
ck(ERRLINE in sl, "a tool error at the END of a huge prompt echo survives the slice")
ck(len(sl.encode()) <= _cc.LEADER_TRACE_HEAD_BYTES + _cc.LEADER_TRACE_TAIL_BYTES,
   "the slice never exceeds its cap (marker charged against the budget)")
ck(ERRLINE not in big.encode()[:_cc.LEADER_TRACE_HEAD_BYTES
                              + _cc.LEADER_TRACE_TAIL_BYTES].decode("utf-8", "replace"),
   "and a HEAD-weighted slice would have lost it -- the design choice, controlled")
ck(_cc.leader_trace_slice("short") == "short", "short stderr passes through untouched")

async def _tcall(l, p, w):
    return {"ok": True, "text": "done", "error": "", "transport": "t",
            "model_used": "m", "trace": "STDERR-SENTINEL"}
_wd = pathlib.Path(tempfile.mkdtemp(prefix="wd-"))
_rec = asyncio.run(cl.run_leader_turn(L, "T", _wd, ground_rules="", max_rounds=2,
                                      nonce_fn=lambda: N, call_leader=_tcall))
ck(any("STDERR-SENTINEL" in t["trace"] for t in _rec.traces),
   "the trace is kept on a SUCCESSFUL call (ok=True), which is the whole gap")

rec5, wd5 = turn([f"{ab}\n{W('no.py','x')}\n{ae}", "done"], block_no)
ck(cl.turn_has_discrepancy(rec5), "an unapplied write IS a discrepancy (persist the trace)")
rec6, _ = turn([f"{ab}\n{W('fine.py','x')}\n{ae}", "done"])
ck(cl.turn_has_discrepancy(rec6) == (), "a clean turn is NOT a discrepancy (do not persist)")
rec7, wd7 = turn([f"{ab}\n{W('g.py','x')}\n{ae}",
                  f"done\n{kb}\nCLAIMED: nowhere.py\n{ke}"])
ck(all(c["status"] == cl.CLAIM_UNSUBSTANTIATED for c in rec7.claims)
   and cl.turn_has_discrepancy(rec7) == (),
   "UNSUBSTANTIATED alone does NOT trip the gate (it is the weak state)")



print("=== G. codex multi-message capture (item 2: the B3 root cause) ===")
_EV = ('{"type":"thread.started","thread_id":"t"}\n'
       '{"type":"turn.started"}\n'
       '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":'
       '"--- BEGIN ACTIONS NX ---\\nWRITE: hello.py\\n--- BEGIN CONTENT NX ---\\n'
       'print(1)\\n--- END CONTENT NX ---\\n--- END ACTIONS NX ---"}}\n'
       '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":'
       '"Created hello.py with the requested contents."}}\n'
       '{"type":"turn.completed","usage":{}}\n')
_m = _cc.codex_agent_messages(_EV)
ck(len(_m) == 2, "both agent messages are recovered from the JSONL stream")
ck("WRITE: hello.py" in _m[0] and "WRITE" not in _m[1],
   "the envelope is in message 1 and the summary in message 2 -- the B3 shape")
ck(_cc.codex_agent_messages('garbage\n{"type":"turn.started"}\n{"nope"\n') == [],
   "non-JSON and non-message events are skipped, never raised")
ck(_cc.codex_agent_messages(
     '{"type":"item.completed","item":{"type":"reasoning","text":"internal"}}') == [],
   "a non-agent_message item is not treated as leader output")

# the turn must now find the envelope even though the LAST message has none
async def _mcall(l, p, w):
    return {"ok": True, "text": _m[1], "error": "", "transport": "codex_subprocess",
            "model_used": "m", "trace": "", "messages": tuple(
                x.replace("NX", N) for x in _m)}
_wd2 = pathlib.Path(tempfile.mkdtemp(prefix="wd-"))
_r = asyncio.run(cl.run_leader_turn(L, "T", _wd2, ground_rules="", max_rounds=3,
                                    nonce_fn=lambda: N, call_leader=_mcall,
                                    review=lambda *a, **k: (0, "VERDICT: PASS\nok", "")))
# THE COUNTERFACTUAL, without which the check above proves only "it works", not "the fix
# is what makes it work": parsing ONLY the last message -- the old behaviour -- finds nothing.
ck(len(cl.parse_leader_actions(_m[1].replace("NX", N), N).actions) == 0,
   "parsing ONLY the last message finds NO actions -- the old behaviour, reproduced")
ck(len(cl.parse_leader_actions("\n".join(x.replace("NX", N) for x in _m), N).actions) == 1,
   "parsing ALL messages finds the WRITE -- the difference the fix makes")
ck((_wd2 / "hello.py").exists(),
   "THE WRITE LANDS from a non-final message (before the fix this was lost)")
ck(_r.reprompted is False,
   "and the zero-write re-prompt does NOT fire -- it was firing on a harness defect")
ck(_r.writes["applied"] and _r.writes["unapplied"] == (),
   "the turn reconciles cleanly instead of looking like a false completion claim")

# codex_cmd: --json is leader-only, because codex_cmd is shared with the member path
_mc = _cc.codex_cmd(pathlib.Path("/tmp/x"))
_lc = _cc.codex_cmd(pathlib.Path("/tmp/x"), json_events=True)
ck("--json" not in _mc, "the MEMBER codex command does not request JSONL")
ck("--json" in _lc, "the LEADER codex command does")
ck(_mc[-1] == "-" and _lc[-1] == "-", "the stdin sentinel stays last in both")



print("=== H. INTENT block (multi-turn: the field a record cannot reconstruct) ===")
_ib, _ie = cl._intent_sentinels(N)
_good = f"Done.\n{_ib}\nDECIDED: approach X not Y, Y needs a GPU\nNEXT: wire the loader\n{_ie}"
_pi = cl.parse_intent(_good, N)
ck(_pi and _pi["decided"].startswith("approach X"), "a valid block parses DECIDED")
ck(_pi["next"] == "wire the loader" and _pi["open"] == "", "NEXT captured; absent OPEN is empty")
ck(cl.parse_intent(_good, "OTHER") is None, "a STALE NONCE cannot replay an intent block")
ck(cl.parse_intent(f"{_ib}\nDECIDED:\nNEXT: x\n{_ie}", N) is None,
   "an EMPTY DECIDED is rejected -- the void check that would pass an empty promise")
ck(cl.parse_intent(f"{_ib}\nNEXT: x\n{_ie}", N) is None, "a block with no DECIDED is rejected")
ck(cl.parse_intent("no block at all", N) is None, "absence yields None, never an empty intent")
ck(cl.parse_intent(f"{_ib}\nDECIDED: d\n", N) is None, "an UNCLOSED block yields nothing")
_sup = cl.parse_intent(f"{_ib}\nDECIDED: d\nSUPERSEDES: turn 2 -- reversed\n"
                       f"SUPERSEDES: turn 3 -- also\n{_ie}", N)
ck(_sup["supersedes"] == ["turn 2 -- reversed", "turn 3 -- also"],
   "SUPERSEDES is repeatable and ordered -- no silent eviction of decisions")
ck(cl.parse_intent(f"{_ib}\nDECIDED:\nDECIDED: real one\n{_ie}", N)["decided"] == "real one",
   "first NON-EMPTY wins, exactly as the code does it (a blank first line is falsy)")

def turn_capturing(replies):
    """turn() returns (record, workdir); these checks need the PROMPTS, so capture them."""
    wd = pathlib.Path(tempfile.mkdtemp(prefix="wd-"))
    it, prompts = iter(replies), []
    async def call(l, p, w):
        prompts.append(p)
        return {"ok": True, "text": next(it, "Nothing further."), "error": "",
                "transport": "t", "model_used": "m"}
    rec = asyncio.run(cl.run_leader_turn(L, "T", wd, ground_rules="", max_rounds=5,
                                         nonce_fn=lambda: N, call_leader=call,
                                         review=lambda *a, **k: (0, "VERDICT: PASS\nok", "")))
    return rec, prompts, wd

_r, _, _ = turn_capturing([f"{ab}\n{W('a.py','x')}\n{ae}", _good])
ck(_r.intent and _r.intent["decided"], "intent arrives on TurnRecord from run_leader_turn")
_r2, _seen2, _ = turn_capturing([f"{ab}\n{W('a.py','x')}\n{ae}", "Done, no block.",
                                 "Still nothing."])
ck(_r2.intent is None, "no block -> TurnRecord.intent is None ('none declared')")
ck(sum(1 for p in _seen2 if cl.INTENT_REPROMPT in p) == 1, "the intent re-prompt fires ONCE")
_r3, _, _wd3 = turn_capturing([f"{ab}\n{W('a.py','x')}\n{ae}",
                               f"Done.\n{kb}\nCLAIMED: a.py\n{ke}",
                               f"Nothing more.\n{_ib}\nDECIDED: keep a.py\n{_ie}"])
ck([c["claim"] for c in _r3.claims] == ["a.py"] and _r3.intent["decided"] == "keep a.py",
   "CLAIMS declared BEFORE the intent re-prompt are NOT discarded by it")
ck(_r2.stop_reason.startswith("final answer"),
   "a missing intent does NOT fail the turn -- form is not a liveness risk")
# THE THREE THINGS codex RAISED IN DIALOGUE. Two were real defects; the third was my own
# test being wrong. All are pinned here so they cannot come back.
_r4, _, _ = turn_capturing([f"I cannot write.\n{_ib}\nDECIDED: use X\n{_ie}\n"
                            f"{kb}\nCLAIMED: a.py\n{ke}", "Nothing more."])
ck(_r4.intent and _r4.intent["decided"] == "use X" and [c["claim"] for c in _r4.claims] == ["a.py"],
   "declarations in the answer that TRIGGERS the zero-write re-prompt are NOT discarded")
_r5, _, _ = turn_capturing([f"no write\n{_ib}\nDECIDED: FIRST\n{_ie}",
                            f"still none\n{_ib}\nDECIDED: SECOND\n{_ie}"])
ck(_r5.intent["decided"] == "SECOND",
   "across TWO candidate-final answers the LAST intent wins, as documented")
_r6, _, _ = turn_capturing([f"{ab}\n{W('a.py','x')}\n{ae}", f"one\n{kb}\nCLAIMED: a.py\n{ke}",
                            f"retract\n{kb}\n{ke}\n{_ib}\nDECIDED: withdrew it\n{_ie}"])
ck(_r6.claims == (),
   "an EMPTY claims block RETRACTS an earlier one -- impossible before absent/empty split")

_g = cl._action_grammar_instructions(N)
ck(_ib in _g and "DECIDED:" in _g, "the leader is TOLD the intent grammar")
ck(all(x in _g for x in (ab, cb, kb)), "and actions/content/claims grammars all survive")

print(f"\nALL {n} CHECKS PASSED")
