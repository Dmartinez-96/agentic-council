"""Deterministic regression for council_leader's leaderless handoff panel (driver piece c):
format_turn_record + author_handoff. Uses an INJECTED call_model seam -- no live model calls.

Locks the load-bearing contract:
  - format_turn_record renders the metadata NOTES + the leader's summary labelled ASSERTED.
  - author_handoff excludes the turn's leader from BOTH the default panel AND a caller-supplied
    one (builder != auditor) -- the check uses a leader that IS a roster member, so a missing
    filter would leave it in and fail.
  - the emitted handoff contains the RECORD verbatim (the panel cannot alter/delete it) plus a
    UNION of every panelist's note; a failing panelist is noted, not fatal.

Re-run:  python3 council/tests/test_leader_handoff.py
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


WD = Path("/tmp")


def mk_record(leader="codex", final="LEADER-SAYS-DONE"):
    return cl.TurnRecord(
        leader,
        ({"round": 0, "notes": ("READ a.py: 12 bytes",
                                "WRITE b.py: verdict=PASS applied=True"), "leader_chars": 5},
         {"round": 1, "notes": ("PROBLEM: a dropped write",), "leader_chars": 3}),
        final, "final answer (no actions)")


def member(name, tier, transport, model):
    return cc.Member(name, tier, transport, model, capabilities=cc._DEFAULT_CAPS)


def recording_call(log):
    async def _c(m, prompt, cwd):
        log.append((m.name, prompt))
        return {"ok": True, "text": f"NOTE-FROM-{m.name}", "error": "",
                "transport": "t", "model_used": ""}
    return _c


# ---------------------------------------------------------------------------
# 1. format_turn_record: notes + leader summary labelled ASSERTED
# ---------------------------------------------------------------------------
rt = cl.format_turn_record(mk_record())
check("record shows the metadata notes (read + write verdict + problem)",
      "READ a.py: 12 bytes" in rt and "WRITE b.py: verdict=PASS" in rt
      and "PROBLEM: a dropped write" in rt)
check("record carries the leader name and the ASSERTED/UNVERIFIED label on its summary",
      "codex" in rt and "ASSERTED" in rt and "LEADER-SAYS-DONE" in rt)


# ---------------------------------------------------------------------------
# 2. DEFAULT panel excludes the turn's leader (leader is a roster voting member)
# ---------------------------------------------------------------------------
log = []
res = asyncio.run(cl.author_handoff(mk_record(leader="codex"), WD,
                                    call_model=recording_call(log)))
# DERIVED from the registry, never hardcoded. This assertion used to name five seats
# literally: gemini, deepseek, kimi, glm, grok. That is exactly voting+inspector minus
# codex for the registry HANDOFF 0c.A records -- codex/gemini/deepseek VOTING,
# kimi/glm/grok INSPECTOR -- so it was correct when written and went stale the moment
# the bench grew to 6+6. A hardcoded roster silently turns "the leader is excluded"
# into "the roster has not changed", and the second claim fails first, hiding the first.
expected_panel = [m.name for m in
                  list(cc.voting_members()) + list(cc.inspector_members())
                  if m.name != "codex"]
check(f"default panel EXCLUDES the leader (codex) -> the other "
      f"{len(expected_panel)} members (got {res['panel']})",
      res["panel"] == expected_panel)
check("the excluded leader is never dispatched", "codex" not in [n for n, _ in log])


# ---------------------------------------------------------------------------
# 3. CALLER-SUPPLIED panel also has the leader filtered out
# ---------------------------------------------------------------------------
custom = [member("codex", cc.VOTING, "codex_subprocess", cc.CODEX_MODEL),
          member("grok", cc.INSPECTOR, "openrouter", "x-ai/grok-4.5")]
res = asyncio.run(cl.author_handoff(mk_record(leader="codex"), WD, panel=custom,
                                    call_model=recording_call([])))
check("caller-supplied panel with the leader in it -> leader removed",
      res["panel"] == ["grok"])


# ---------------------------------------------------------------------------
# 4. handoff = VERBATIM record (unalterable) + UNION of panel notes
# ---------------------------------------------------------------------------
dlog = []
res = asyncio.run(cl.author_handoff(mk_record(leader="lead-external"), WD,
                                    panel=[member("grok", cc.INSPECTOR, "openrouter", "x/y"),
                                           member("glm", cc.INSPECTOR, "openrouter", "z/w")],
                                    call_model=recording_call(dlog)))
check("handoff begins with the record VERBATIM (panel cannot alter/delete it)",
      res["handoff"].startswith(res["record"]))
check("handoff carries EVERY panelist's note (union)",
      "NOTE-FROM-grok" in res["handoff"] and "NOTE-FROM-glm" in res["handoff"])
check("DELIVERY: each panelist's prompt carries the record AND the panel instructions "
      "('LEADERLESS panel' is unique to the instructions, absent from the record)",
      len(dlog) == 2
      and all(res["record"] in pr and "LEADERLESS panel" in pr for _, pr in dlog))


# ---------------------------------------------------------------------------
# 5. a failing panelist is noted, not fatal
# ---------------------------------------------------------------------------
async def fail_call(m, prompt, cwd):
    return {"ok": False, "text": "", "error": "boom", "transport": "t", "model_used": ""}


res = asyncio.run(cl.author_handoff(mk_record(leader="lead-external"), WD,
                                    panel=[member("grok", cc.INSPECTOR, "openrouter", "x/y")],
                                    call_model=fail_call))
check("failing panelist -> noted as unavailable, no crash, record still present",
      "unavailable" in res["handoff"] and "boom" in res["handoff"]
      and res["record"] in res["handoff"])


# ---------------------------------------------------------------------------
# 6. panel that filters down to EMPTY (all were the leader) -> no crash
# ---------------------------------------------------------------------------
res = asyncio.run(cl.author_handoff(mk_record(leader="codex"), WD,
                                    panel=[member("codex", cc.VOTING, "codex_subprocess",
                                                  cc.CODEX_MODEL)],
                                    call_model=recording_call([])))
check("panel emptied by the leader filter -> record preserved, no crash",
      res["panel"] == [] and res["record"] in res["handoff"])


print(f"\n=== handoff panel: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
