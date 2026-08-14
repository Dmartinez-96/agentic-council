#!/usr/bin/env python3
"""Battery for the verdict-recovery ladder. Run from Council/:
    python3 council/tests/test_verdict_ladder.py          # unit checks only, no API calls
    python3 council/tests/test_verdict_ladder.py --live   # adds the live ladder checks

EXITS NON-ZERO ON ANY FAILURE. An earlier version of this file only printed
"expect" beside "got" with no comparison and always exited 0 -- a harness that
cannot fail, which would have been cited as green to declare the ladder done.

TWO LAYERS, deliberately separated:
  UNIT  -- the MODULE-LEVEL PRIMITIVES the ladder's classifier is built from:
           VERDICT_RE, NO_POSITION_RE and parse_verdict. _classify itself is a
           closure inside reformat_unparseable and cannot be imported, and
           re-implementing it here would test my copy rather than the engine's. So
           this layer does NOT exercise _classify's branches directly; it pins the
           behaviour a regression in those branches would have to break.
  LIVE  -- the real reformat_unparseable over real historical member responses.

WHAT THE LIVE ROWS DO AND DO NOT ASSERT. A recovery at an EARLIER stage than
expected is not a failure -- if the primary commits at stage 2, that is the ladder
working and stage 3 is correctly not reached. So the live rows assert an ACCEPTABLE
SET of outcomes, and separately report which stage fired, rather than pinning one.
What they do assert strictly: no seat may end with a verdict it did not produce, a
stage-3 verdict must carry verdict_model, and a seat with no fallback must record
fallback_unavailable rather than borrow another model.
"""
import asyncio
import glob
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc  # noqa: E402

REFUSAL = "你好，我无法给到相关内容。"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def unit_checks() -> None:
    """Assert the module-level primitives the ladder's classifier is built from."""
    print("UNIT: classifier primitives (deterministic, no API calls)")
    check("VERDICT_RE matches a bare first-line verdict",
          bool(cc.VERDICT_RE.search("VERDICT: PASS")))
    check("VERDICT_RE does NOT match an inline mention",
          not cc.VERDICT_RE.search("I would say VERDICT: PASS is right"))
    check("NO_POSITION_RE matches VERDICT: NONE",
          bool(cc.NO_POSITION_RE.search("VERDICT: NONE")))
    check("NO_POSITION_RE does not match a real verdict",
          not cc.NO_POSITION_RE.search("VERDICT: WARN"))
    check("parse_verdict rejects a non-first-line verdict",
          cc.parse_verdict("some preamble\nVERDICT: PASS") == "UNPARSEABLE",
          f"got {cc.parse_verdict('some preamble' + chr(10) + 'VERDICT: PASS')}")
    check("parse_verdict rejects conflicting verdicts",
          cc.parse_verdict("VERDICT: PASS\nVERDICT: WARN") == "UNPARSEABLE")
    check("parse_verdict accepts a clean first line",
          cc.parse_verdict("VERDICT: BLOCK\nREASONS:\n- x") == "BLOCK")
    # The ambiguity rule the ladder depends on: NONE first, then a real verdict.
    amb = "VERDICT: NONE\nVERDICT: PASS"
    check("ambiguous text: NO_POSITION_RE and VERDICT_RE BOTH fire",
          bool(cc.NO_POSITION_RE.search(amb)) and bool(cc.VERDICT_RE.search(amb)),
          "this is what makes the ladder discard rather than guess")


def recovery_provenance_checks() -> None:
    """Force the historical stage-2 shape and prove its source reaches the JSON log."""
    print("\nUNIT: recovered-verdict provenance (deterministic, no API calls)")

    async def exercise() -> dict:
        replies = iter(({"text": "VERDICT: NONE"}, {"text": "VERDICT: BLOCK"}))

        async def fake_runner(prompt, system_prompt, cwd):
            return next(replies)

        saved_runner = cc.MEMBER_RUNNERS.get("codex")
        cc.MEMBER_RUNNERS["codex"] = fake_runner
        record = {"role": "codex", "text": "<tool_call>read</tool_call>",
                  "stderr": "", "verdict": "UNPARSEABLE"}
        try:
            await cc.reformat_unparseable([record], Path(tempfile.mkdtemp()))
        finally:
            if saved_runner is None:
                cc.MEMBER_RUNNERS.pop("codex", None)
            else:
                cc.MEMBER_RUNNERS["codex"] = saved_runner
        return record

    record = asyncio.run(exercise())
    check("stage 1 NONE advances to stage 2 BLOCK",
          record.get("verdict") == "BLOCK" and record.get("verdict_stage") == 2)
    check("original response remains the member text",
          record.get("text") == "<tool_call>read</tool_call>")
    check("stage-2 response is retained as verdict provenance",
          record.get("recovered_verdict_text") == "VERDICT: BLOCK")

    saved_logs = cc.LOGS_ROOT
    try:
        cc.LOGS_ROOT = Path(tempfile.mkdtemp())
        written = cc.write_log("posttool", "Edit", "/tmp/provenance.py", "pitch",
                               [record], "BLOCK")
        logged = json.loads(written.read_text())["members"][0]
        check("recovered verdict text reaches the JSON log",
              logged.get("recovered_verdict_text") == "VERDICT: BLOCK")
        check("logged original text still differs from its recovered verdict",
              "BLOCK" not in logged.get("text", ""))
    finally:
        cc.LOGS_ROOT = saved_logs


def find(pred, label):
    for fn in sorted(glob.glob("logs/2026-07-2*/*.json"), reverse=True):
        try:
            d = json.load(open(fn))
        except (OSError, ValueError):
            continue
        for key in ("members", "shadow"):
            for r in (d.get(key) or []):
                if isinstance(r, dict) and pred(r):
                    return dict(r)
    FAILURES.append(f"fixture not found: {label}")
    print(f"  FAIL fixture not found: {label}  (cannot test this row)")
    return None


async def live_checks() -> None:
    print("\nLIVE: the real reformat_unparseable over real responses")
    cwd = Path(tempfile.mkdtemp(prefix="ladder_test_"))
    rows = []

    # Exclude AMBIGUOUS responses (a NONE alongside a real verdict): the ladder is
    # supposed to DISCARD those, so demanding recovery from one would fail the
    # harness on correct behaviour.
    r1 = find(lambda r: r.get("verdict") == "UNPARSEABLE"
              and cc.VERDICT_RE.findall(r.get("text") or "")
              and not cc.NO_POSITION_RE.search(r.get("text") or "")
              and (r.get("text") or "").strip(), "mis-formatted verdict")
    if r1:
        rows.append(("mis-formatted verdict", r1, {1}, True))

    r2 = find(lambda r: r.get("verdict") == "UNPARSEABLE"
              and (r.get("text") or "").lstrip().startswith("REQUEST_")
              and not cc.VERDICT_RE.findall(r.get("text") or ""),
              "requests-only final response")
    if r2:
        rows.append(("requests-only", r2, {2, 3}, False))

    rows.append(("canned refusal, seat WITH fallback",
                 {"role": "deepseek", "text": REFUSAL, "verdict": "UNPARSEABLE"},
                 {2, 3}, False))
    # A no-fallback seat can still legitimately recover at stage 1 or 2 -- only
    # stage 3 is impossible for it. An earlier version allowed NO stages here, which
    # would have failed the harness on a correct primary recovery.
    rows.append(("canned refusal, seat with NO fallback",
                 {"role": "muse", "text": REFUSAL, "verdict": "UNPARSEABLE"},
                 {1, 2}, False))

    recs = [r for _, r, _, _ in rows]
    await cc.reformat_unparseable(recs, cwd)

    for (label, _, allowed_stages, must_recover), r in zip(rows, recs):
        stage = r.get("verdict_stage")
        recovered = r.get("verdict") in ("PASS", "WARN", "BLOCK")
        flags = {k: v for k, v in r.items()
                 if k.startswith(("reformat_", "commit_", "fallback_", "verdict_"))
                 and v is not None}
        print(f"\n  [{label}] stage={stage} verdict={r.get('verdict')}")
        print(f"      flags: {flags}")
        if must_recover:
            check(f"{label}: recovered a verdict", recovered)
        if recovered:
            check(f"{label}: stage in {allowed_stages or '(none expected)'}",
                  stage in allowed_stages, f"stage={stage}")
        else:
            # Not recovering is acceptable ONLY if the ladder recorded WHY.
            why = {"reformat_ambiguous", "reformat_failed", "commit_declined",
                   "fallback_unavailable", "fallback_no_position", "reformat_error",
                   "commit_error", "fallback_error", "reformat_no_position"}
            check(f"{label}: non-recovery is explained", bool(why & set(flags)),
                  f"flags={sorted(set(flags))}")
        if stage == 3:
            check(f"{label}: stage-3 verdict names its model",
                  bool(r.get("verdict_model")), f"model={r.get('verdict_model')}")
        if label.endswith("NO fallback"):
            # Only assert the skip when the ladder actually REACHED stage 3; if the
            # primary answered earlier, stage 3 was correctly never attempted.
            if not recovered:
                check(f"{label}: records fallback_unavailable",
                      bool(r.get("fallback_unavailable")))
            check(f"{label}: never borrowed another model",
                  r.get("verdict_model") is None)
            check(f"{label}: never reached stage 3", stage != 3)


def main() -> int:
    unit_checks()
    recovery_provenance_checks()
    if "--live" in sys.argv:
        asyncio.run(live_checks())
    else:
        print("\n(skipping live ladder checks; pass --live to run them)")
    print(f"\nFAILURES: {len(FAILURES)}" + (f" -> {FAILURES}" if FAILURES else ""))
    return 1 if FAILURES else 0


sys.exit(main())
