#!/usr/bin/env python3
"""The CONVERSATION LOOP through the REAL council_leader_run.main().

WHY main() AND NOT A REBUILT PATH: the thing under test is the wiring -- that the runner
starts/continues a conversation, uses the conversation scratch, carries prior context INTO
run_leader_turn, and persists the turn afterwards. Re-implementing that in a test would prove
nothing about the runner (rule 3). So main() runs for real, and only TWO seams are injected:
  - cc.active_leader, because the seam must not depend on WHICH leader this install happens
    to have configured, and editing roster.json to control that would change EVERY session's
    fires globally (it is GLOBAL to the install).
    THIS REASON WAS RESTATED 2026-08-06: it used to read "this install has no
    council-native leader", which is now false twice over -- roster.json seats codex, and
    DEFAULT_LEADER means even an ABSENT roster yields claude rather than None. The seam is
    right for a reason that does not depend on the current roster at all.
  - cc._call_leader and cl.author_handoff, so the loop is exercised without model spend.
Everything between those seams -- argparse, the conversation branch, scratch selection,
prior-context assembly, persist_turn -- is production code.
"""
import sys, re, tempfile, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import consult_council as cc
import council_leader as cl
import council_session as cs
import council_leader_run as run

n = 0
def ck(cond, label):
    global n
    assert cond, f"FAILED: {label}"
    n += 1
    print(f"  [ok] {label}")

TMP = Path(tempfile.mkdtemp(prefix="convroot-"))
cs.CONVERSATIONS_ROOT = TMP
WD = Path(tempfile.mkdtemp(prefix="wd-"))
(WD / "README.md").write_text("a project\n")

LEADER = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)
cc.active_leader = lambda: LEADER

prompts = []
NONCE = "PB0001"

def make_call(replies):
    """Replies are templates; `{nonce}` is filled from THE PROMPT.

    run_leader_turn mints a FRESH RANDOM nonce per round (secrets.token_hex) and main() does
    not expose a nonce_fn seam, so a hard-coded nonce in a reply simply does not match and the
    block is correctly ignored -- which is what a first version of this test did, and it
    looked exactly like the capture being broken. Reading the nonce out of the prompt is also
    what a real leader does.
    """
    it = iter(replies)
    async def _call(leader, prompt, cwd):
        prompts.append(prompt)
        m = re.search(r"--- BEGIN ACTIONS ([0-9a-f]+) ---", prompt)
        nonce = m.group(1) if m else "NONONCE"
        tpl = next(it, "Nothing further.")
        return {"ok": True, "text": tpl.replace("{nonce}", nonce), "error": "",
                "transport": "t", "model_used": "m", "trace": "", "messages": ()}
    return _call

async def _fake_panel(record, workdir, **kw):
    return {"handoff": f"HANDOFF for a turn that ended: {record.stop_reason}",
            "record": "", "panel": [], "panelist_notes": []}
cl.author_handoff = _fake_panel

def turn(task, conversation, replies):
    prompts.clear()
    cc._call_leader = make_call(replies)
    sys.argv = ["council_leader_run.py", "--task", task, "--workdir", str(WD),
                "--mode", "plan-only", "--max-rounds", "4", "--conversation", conversation]
    rc = run.main()
    return rc

ib, ie = "--- BEGIN INTENT {nonce} ---", "--- END INTENT {nonce} ---"

print("=== A. turn 1 starts the conversation and persists ===")
rc = turn("first task", "c1", [f"Done.\n{ib}\nDECIDED: use approach X\n{ie}"])
ck(rc == 0, "the runner exits 0")
ck(cs.turn_numbers("c1") == [1], "turn 1 is persisted and complete")
ck((cs.conversation_dir("c1") / "turns" / "0001" / "handoff.md").read_text().startswith(
    "HANDOFF for a turn"), "the panel's handoff is stored with the turn")
st = json.loads((cs.conversation_dir("c1") / "turns" / "0001" / "state.json").read_text())
ck(st["intent"]["decided"] == "use approach X", "the turn's DECIDED reaches state.json")
ck(cs.scratch_dir("c1").is_dir(), "the conversation scratch exists")

print("=== B. turn 2 CARRIES turn 1's context into the prompt ===")
rc = turn("second task", "c1", [f"Done again.\n{ib}\nDECIDED: also use Y\n{ie}"])
ck(rc == 0, "the runner exits 0 on the second turn")
ck(cs.turn_numbers("c1") == [1, 2], "turn 2 is persisted alongside turn 1")
first_prompt = prompts[0]
ck("HANDOFF FROM THE PRIOR TURN" in first_prompt,
   "turn 2's prompt carries the prior handoff -- prior_handoff is no longer a dead parameter")
ck("use approach X" in first_prompt,
   "and turn 1's DECIDED reaches turn 2 VERBATIM -- the whole point of the ledger")
ck("turn 1: first task" in first_prompt,
   "and the derived spine names turn 1 and its task")

print("=== C. the ledger accumulates and marks supersession ===")
turn("third task", "c1", [f"Done.\n{ib}\nDECIDED: back to X\nSUPERSEDES: turn 2 -- Y was slower\n{ie}"])
led = cs.decided_ledger("c1")
ck([e["decided"] for e in led] == ["use approach X", "also use Y", "back to X"],
   "three decisions carried verbatim, oldest first")
ck(led[1]["superseded_by"] and led[1]["superseded_by"][0]["turn"] == 3,
   "turn 2's decision is MARKED superseded by turn 3")
ck(led[1]["decided"] == "also use Y", "and retained, not deleted")

print("=== D. no --conversation is the old one-shot behaviour ===")
prompts.clear()
cc._call_leader = make_call(["Done."])
sys.argv = ["council_leader_run.py", "--task", "solo", "--workdir", str(WD),
            "--mode", "plan-only", "--max-rounds", "3"]
ck(run.main() == 0, "a turn with no --conversation still runs")
ck("HANDOFF FROM THE PRIOR TURN" not in prompts[0],
   "and carries NO prior context -- conversations are opt-in, never inferred")
ck(sorted(p.name for p in TMP.iterdir()) == ["c1"],
   "and creates no conversation directory")

print(f"\nALL {n} CHECKS PASSED")
