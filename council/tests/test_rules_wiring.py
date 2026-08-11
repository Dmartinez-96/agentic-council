#!/usr/bin/env python3
"""Battery for the WIRING of the agent-agnostic rules stack. Run from Council/:
    python3 council/tests/test_rules_wiring.py

EXITS NON-ZERO ON ANY FAILURE. No API calls: the transport is stubbed, so this is free.

test_rules_stack.py pins RESOLUTION -- that the right files are found. This pins
DELIVERY -- that what was resolved actually reaches a member's prompt, on the correct
SIDE of the evidence, on every route including the fallback. The two are different
claims: resolve_rules returning the right text proves nothing about whether any caller
passes it on, and a wiring that dropped it entirely would leave test_rules_stack green.

WHAT IT PINS:
  PREFIX      -- the base sits in the LEADING span, contiguous from byte 0, and is the
                 cache_prefix the transport is handed. Asserted on the prompt the
                 transport actually receives, not on run_member's return value.
  SIDE        -- base BEFORE the evidence, overlay AFTER it. The de-anchoring ruling is
                 the reason the split exists; a wiring that put the overlay first would
                 satisfy every other check here.
  FALLBACK    -- the codex fallback route rebuilds its own prefix, so it is the one
                 route that can silently lose the ground rules. Driven by forcing the
                 primary to ERROR.
  MISATTRIBUTION -- a seat whose fallback slug resolves to a DIFFERENT model overlay
                 gets the MODEL layer withheld and the ROLE layer kept. Planted, because
                 "no overlay exists" and "the guard fired" look identical otherwise.
  CAP         -- _fit_to_cap charges header+footer before slicing, and does NOT charge
                 the truncation note against the FIT TEST (a body that fits needs no
                 marker; charging it up front shrinks every block's usable budget).
  GATE        -- STANDING_RULES_CONFIGURED follows the env var, checked in a SUBPROCESS
                 because it is computed at import.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio  # noqa: E402
import consult_council as cc  # noqa: E402

FAILURES: list[str] = []

BASE_SENTINEL = "GROUND-RULES-SENTINEL-b7f2"
MODEL_SENTINEL = "MODEL-OVERLAY-SENTINEL-4c1a"
ROLE_SENTINEL = "ROLE-OVERLAY-SENTINEL-9e33"
LEAD_SENTINEL = "LEAD-ROLE-OVERLAY-SENTINEL-7b18"
FB_SENTINEL = "FALLBACK-MODEL-OVERLAY-SENTINEL-2d70"
EVIDENCE = "## Evidence\nEVIDENCE-SENTINEL-51aa\n"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def member(model: str, tier: str = "voting", transport: str = "openrouter",
           fallback: str | None = None) -> cc.Member:
    return cc.Member(name="probe", tier=tier, transport=transport,
                     model=model, fallback_model=fallback, capabilities=())


class _Fixture:
    """Temp GROUND_RULES_PATH + OVERLAY_ROOT, so nothing is written into the live tree
    and the assertions do not depend on which overlays happen to exist today."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rules_wiring_"))
        self.real_base = cc.GROUND_RULES_PATH
        self.real_root = cc.OVERLAY_ROOT

    def __enter__(self) -> "_Fixture":
        base = self.tmp / "council_ground_rules.md"
        base.write_text(f"# Ground rules\n\n{BASE_SENTINEL}\n", encoding="utf-8")
        cc.GROUND_RULES_PATH = base
        cc.OVERLAY_ROOT = self.tmp / "overlays"
        (cc.OVERLAY_ROOT / "models" / "probevendor").mkdir(parents=True)
        (cc.OVERLAY_ROOT / "roles").mkdir(parents=True)
        (cc.OVERLAY_ROOT / "models" / "probevendor" / "has-overlay.md").write_text(
            MODEL_SENTINEL + "\n", encoding="utf-8")
        (cc.OVERLAY_ROOT / "models" / "probevendor" / "other-overlay.md").write_text(
            FB_SENTINEL + "\n", encoding="utf-8")
        (cc.OVERLAY_ROOT / "roles" / "voting.md").write_text(
            ROLE_SENTINEL + "\n", encoding="utf-8")
        (cc.OVERLAY_ROOT / "roles" / f"{cc.LEADER}.md").write_text(
            LEAD_SENTINEL + "\n", encoding="utf-8")
        return self

    def __exit__(self, *a) -> bool:
        cc.GROUND_RULES_PATH = self.real_base
        cc.OVERLAY_ROOT = self.real_root
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def capture(m: cc.Member, *, codex_errors: bool = False) -> dict:
    """Drive the REAL run_member and return what the transport was HANDED.

    The stub replaces _openrouter_call_blocking -- the far end of the chain -- so the
    captured prompt is the one run_member/build_prompt actually produced, and the
    captured cache_prefix is the one run_openrouter actually chose. Asserting on
    run_member's return value instead would prove nothing about either.
    """
    seen: dict = {}

    def _stub(role, models, prompt, cache_prefix="") -> dict:
        seen["prompt"] = prompt
        seen["cache_prefix"] = cache_prefix
        seen["models"] = list(models)
        return {"role": role, "verdict": "PASS", "text": "VERDICT: PASS", "model_used": models[0]}

    real = cc._openrouter_call_blocking
    real_codex = cc.run_codex
    cc._openrouter_call_blocking = _stub
    if codex_errors:
        async def _err(*a, **k):
            return {"role": "probe", "verdict": "ERROR", "text": "",
                    "stderr": "usage limit"}
        cc.run_codex = _err
    try:
        asyncio.run(cc.run_member(m, "PITCH-SENTINEL", "SYSTEM-PROMPT-SENTINEL",
                                  Path(tempfile.gettempdir()),
                                  evidence_block=EVIDENCE))
    finally:
        cc._openrouter_call_blocking = real
        cc.run_codex = real_codex
    return seen


def prefix_and_side_checks() -> None:
    print("PREFIX + SIDE: what the transport was handed")
    seen = capture(member("probevendor/has-overlay"))
    prompt, prefix = seen["prompt"], seen["cache_prefix"]

    check("the base reaches the prompt at all", BASE_SENTINEL in prompt)
    check("the model overlay reaches the prompt", MODEL_SENTINEL in prompt)
    check("the role overlay reaches the prompt", ROLE_SENTINEL in prompt)

    check("the prompt still starts with the system prompt",
          prompt.startswith("SYSTEM-PROMPT-SENTINEL"))
    check("the base is INSIDE the cache_prefix (step 2: the prefix now covers it)",
          BASE_SENTINEL in prefix, f"prefix {len(prefix)} B")
    check("the cache_prefix is a genuine LEADING span of the prompt",
          prompt.startswith(prefix))
    check("the overlay is NOT in the cache_prefix (it is per-seat, after the evidence)",
          MODEL_SENTINEL not in prefix)

    i_base, i_ev = prompt.index(BASE_SENTINEL), prompt.index(EVIDENCE.strip()[:20])
    i_model, i_role = prompt.index(MODEL_SENTINEL), prompt.index(ROLE_SENTINEL)
    check("BASE lands BEFORE the evidence", i_base < i_ev, f"{i_base} < {i_ev}")
    check("the MODEL overlay lands AFTER the evidence", i_model > i_ev,
          f"{i_model} > {i_ev}")
    check("the ROLE overlay lands AFTER the evidence", i_role > i_ev)
    check("the overlay header names this seat's own model",
          "probevendor/has-overlay" in prompt)

    seen2 = capture(member("probevendor/no-overlay-here"))
    p2 = seen2["prompt"]
    check("a model with no overlay file still gets the base", BASE_SENTINEL in p2)
    check("a model with no overlay file gets no model overlay",
          MODEL_SENTINEL not in p2)
    check("...but still gets its ROLE overlay", ROLE_SENTINEL in p2)


def fallback_route_checks() -> None:
    """The codex fallback rebuilds its own prefix from base_prompt, so it is the one
    route that can lose the ground rules while every other test stays green."""
    print("\nFALLBACK ROUTE: the rebuilt prefix")
    m = cc.Member(name="probe", tier="voting", transport="codex_subprocess",
                  model="probevendor/has-overlay",
                  fallback_model="probevendor/has-overlay", capabilities=())
    seen = capture(m, codex_errors=True)
    check("the fallback route actually fired", bool(seen), f"models={seen.get('models')}")
    check("the rebuilt fallback prompt CARRIES the ground rules",
          BASE_SENTINEL in seen.get("prompt", ""))
    check("the fallback cache_prefix covers the base too",
          BASE_SENTINEL in seen.get("cache_prefix", ""))


def misattribution_checks() -> None:
    """A fallback slug may serve the prompt instead of member.model. Planting a
    DIFFERENT overlay on the fallback constructs the condition; observing that no
    fallback overlay exists would prove nothing."""
    print("\nMISATTRIBUTION GUARD: a fallback with a different overlay")
    m = member("probevendor/has-overlay", fallback="probevendor/other-overlay")
    p = capture(m)["prompt"]
    check("the PRIMARY model's overlay is WITHHELD when the fallback differs",
          MODEL_SENTINEL not in p)
    check("the FALLBACK model's overlay is not substituted either",
          FB_SENTINEL not in p)
    check("the ROLE layer survives the withholding", ROLE_SENTINEL in p)
    check("the base is unaffected", BASE_SENTINEL in p)

    same = member("probevendor/has-overlay", fallback="probevendor/has-overlay")
    p_same = capture(same)["prompt"]
    check("a fallback resolving to the SAME overlay keeps it (guard is not blanket)",
          MODEL_SENTINEL in p_same)


def leader_parity_checks() -> None:
    """The leader path used to default ground_rules to "", so a caller that forgot the
    argument seated the one mutating role with no rules and nothing said so. Driven
    through the REAL run_leader_turn with only the model call stubbed, because a
    resolver that returns the right text proves nothing about the prompt."""
    print("\nLEADER PARITY: run_leader_turn's default")
    import council_leader as cl

    seen: dict = {}

    async def _call(leader, prompt, cwd) -> dict:
        seen["prompt"] = prompt
        return {"ok": True, "text": "nothing to do"}

    lead = cc.Member(name="lead", tier=cc.LEADER, transport="openrouter",
                     model="probevendor/has-overlay", fallback_model=None,
                     capabilities=())
    wd = Path(tempfile.gettempdir())
    asyncio.run(cl.run_leader_turn(lead, "TASK-SENTINEL", wd, call_leader=_call))
    p = seen.get("prompt", "")
    check("the DEFAULT (no ground_rules argument) delivers the base to the leader",
          BASE_SENTINEL in p)
    check("...and the leader's own model overlay", MODEL_SENTINEL in p)
    check("...and the LEAD role overlay, which no member seat can reach",
          LEAD_SENTINEL in p)
    check("the voting role overlay is NOT delivered to the leader",
          ROLE_SENTINEL not in p)

    seen.clear()
    asyncio.run(cl.run_leader_turn(lead, "TASK-SENTINEL", wd, call_leader=_call,
                                   ground_rules=""))
    check('an explicit ground_rules="" still means DELIBERATELY none',
          BASE_SENTINEL not in seen.get("prompt", ""))

    seen.clear()
    asyncio.run(cl.run_leader_turn(lead, "TASK-SENTINEL", wd, call_leader=_call,
                                   ground_rules="CALLER-SUPPLIED-RULES"))
    check("an explicit string is still used verbatim",
          "CALLER-SUPPLIED-RULES" in seen.get("prompt", ""))


def cap_checks() -> None:
    print("\nCAP: _fit_to_cap arithmetic")
    note = "\n\n[trunc]"
    check("text that fits is returned UNCHANGED",
          cc._fit_to_cap("abc", 100, 10, note) == "abc")
    check("the note is NOT charged against the FIT TEST",
          cc._fit_to_cap("x" * 90, 100, 10, note) == "x" * 90,
          "a body that exactly fills the budget needs no marker")
    out = cc._fit_to_cap("y" * 200, 100, 10, note)
    check("over-cap text is truncated and marked", out.endswith(note) and len(out) < 200)
    check("header+footer are charged BEFORE slicing (block stays within cap)",
          len(out.encode()) + 10 <= 100, f"{len(out.encode())} + 10")
    check("a degenerate cap yields \"\" rather than a negative slice",
          cc._fit_to_cap("z" * 50, 5, 10, note) == "")

    print("\nCAP: the standing-rules file at the raised cap")
    tmp = Path(tempfile.mkdtemp(prefix="rules_cap_"))
    try:
        small = tmp / "small.md"
        small.write_text("RULE-A\n" * 100, encoding="utf-8")
        blk = cc.format_standing_rules(small)
        check("a file under the cap is delivered WHOLE (no truncation marker)",
              "[... truncated" not in blk and blk.count("RULE-A") == 100)

        big = tmp / "big.md"
        big.write_text("RULE-B\n" * 20000, encoding="utf-8")
        blk2 = cc.format_standing_rules(big)
        check("a file over the cap IS marked truncated", "[... truncated" in blk2)
        check("and the whole block still fits the cap",
              len(blk2.encode()) <= cc.STANDING_RULES_MAX_BYTES,
              f"{len(blk2.encode())} <= {cc.STANDING_RULES_MAX_BYTES}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def exfil_corpus_check() -> None:
    print("\nEXFIL CORPUS: overlays are local text, so the brake must cover them")
    ctx = cc.build_exfil_context("E", "D", "P", rules_overlay_block=MODEL_SENTINEL)
    check("rules_overlay_block reaches the corpus", MODEL_SENTINEL in ctx)
    check("omitting it changes nothing else",
          cc.build_exfil_context("E", "D", "P") == "E\nD\nP")


def gate_check() -> None:
    """STANDING_RULES_CONFIGURED is computed at import, so the only honest check runs a
    fresh interpreter under each env. Reading the module's own already-imported value
    would report this session's env twice and pass either way -- a void check."""
    print("\nGATE: STANDING_RULES_CONFIGURED follows the env var")
    root = str(Path(__file__).resolve().parent.parent)
    code = ("import sys; sys.path.insert(0, %r); import consult_council as cc; "
            "print(cc.STANDING_RULES_CONFIGURED)" % root)
    for value, expected in ((None, "False"), (str(Path(__file__)), "True")):
        env = dict(os.environ)
        env.pop("COUNCIL_STANDING_RULES_PATH", None)
        if value:
            env["COUNCIL_STANDING_RULES_PATH"] = value
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, cwd=root)
        got = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        check(f"env {'set' if value else 'unset'} -> {expected}", got == expected,
              f"got {got!r} rc={out.returncode} stderr={out.stderr.strip()[-200:]}")


def main() -> int:
    with _Fixture():
        prefix_and_side_checks()
        fallback_route_checks()
        misattribution_checks()
        leader_parity_checks()
        exfil_corpus_check()
    cap_checks()
    gate_check()
    print(f"\nFAILURES: {len(FAILURES)}" + (f" -> {FAILURES}" if FAILURES else ""))
    return 1 if FAILURES else 0


sys.exit(main())
