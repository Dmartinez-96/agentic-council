#!/usr/bin/env python3
"""Leader/member family overlap (0w item 1) and the shipped default leader (item 2).

THE PROPERTY THAT MATTERS AND WHY IT IS NOT NAME EQUALITY: `_council_review` does not
exclude the seated leader, so a leader can sit on the panel reviewing its own writes.
Catching that by SEAT NAME finds `codex` reviewing `codex` and misses a `claude` leader
among members running `anthropic/...` under any other seat name. Section C drives exactly
that case, because it is the one a name check cannot pass.

WHAT WOULD FALSIFY THIS SUITE: an overlap reported for a genuinely mixed bench (false
alarm, which would train the user to ignore the banner), an overlap MISSED across
transports (the whole point), an UNDETERMINED family silently rendered as "no overlap", or
a default leader appearing on a roster that was rejected rather than absent.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import consult_council as cc  # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


M = cc.Member


def roster_leader(roster_path: str | None) -> dict:
    """Ask a FRESH engine process what leader it resolves for a given roster path.

    A subprocess, not an import: the roster is bound at MODULE IMPORT time, so re-reading
    it in this process would measure the roster this suite happened to start with.
    """
    env = dict(os.environ)
    if roster_path is not None:
        env["COUNCIL_ROSTER_PATH"] = roster_path
    out = subprocess.run(
        [sys.executable, str(ROOT / "consult_council.py"), "--print-roster"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    return json.loads(out.stdout)


def main():
    print("\n-- A. model_family resolves every shipped transport --")
    cases = [("openrouter", "anthropic/claude-opus-5", "anthropic"),
             ("openrouter", "tencent/hy3", "tencent"),
             ("openrouter", "MOONSHOTAI/Kimi-K3", "moonshotai"),   # case-folded
             ("openrouter", "no-slash", None),                     # malformed, not empty
             ("openrouter", "", None),
             ("codex_subprocess", "gpt-5.6-sol", "openai"),
             ("claude_subprocess", "claude-opus-5", "anthropic"),
             ("gemini_rest", "gemini-3.5-flash", "google"),
             ("deepseek_https", "deepseek-v4-pro", "deepseek"),
             ("transport_added_without_updating_the_map", "x", None)]
    for t, m, exp in cases:
        check(f"{t} / {m!r} -> {exp!r}", cc.model_family(t, m) == exp,
              repr(cc.model_family(t, m)))

    print("\n-- B. direct transports agree with their OpenRouter twins --")
    # The map is only trustworthy if a direct seat and the same model routed through
    # OpenRouter land in ONE family. That is the entire basis for deriving it.
    pairs = [("codex_subprocess", "gpt-5.6-sol", cc.CODEX_OPENROUTER_FALLBACK),
             ("claude_subprocess", "claude-opus-5", cc.CLAUDE_OPENROUTER_FALLBACK),
             ("gemini_rest", "g", cc.GEMINI_OPENROUTER_MODEL),
             ("deepseek_https", "d", cc.DEEPSEEK_OPENROUTER_MODEL)]
    for t, native, slug in pairs:
        check(f"{t} matches {slug}",
              cc.model_family(t, native) == cc.model_family("openrouter", slug))

    print("\n-- C. THE CASE NAME-MATCHING CANNOT CATCH --")
    bench = (M("claudia", "voting", "openrouter", "anthropic/claude-opus-5"),
             M("grok", "voting", "openrouter", "x-ai/grok-4.5"))
    o = cc.leader_family_overlap(M("claude", "voting", "claude_subprocess",
                                   "claude-opus-5"), bench)
    check("claude leader vs an anthropic/ member under a DIFFERENT name",
          o["overlaps"] and o["voting"] == ["claudia"], str(o["voting"]))
    check("no seat name is shared", "claude" not in [m.name for m in bench])

    print("\n-- D. voting and inspector are NOT conflated --")
    bench2 = (M("codex", "voting", "codex_subprocess", "gpt-5.6-sol"),
              M("oai_watch", "inspector", "openrouter", "openai/gpt-5.6-sol"))
    o2 = cc.leader_family_overlap(M("codex", "voting", "codex_subprocess",
                                    "gpt-5.6-sol"), bench2)
    check("voting overlap listed separately", o2["voting"] == ["codex"], str(o2))
    check("inspector overlap listed separately", o2["inspector"] == ["oai_watch"])

    print("\n-- E. a genuinely mixed bench raises NOTHING (no false alarm) --")
    o3 = cc.leader_family_overlap(M("claude", "voting", "claude_subprocess",
                                    "claude-opus-5"), cc.DEFAULT_REGISTRY)
    check("claude leader vs the shipped bench: no overlap", not o3["overlaps"], str(o3))
    check("no undetermined seats in the shipped bench", o3["undetermined"] == [],
          str(o3["undetermined"]))
    check("banner stays silent", cc.format_family_overlap_banner(o3) is None)

    print("\n-- F. UNDETERMINED is never rendered as 'fine' --")
    bench4 = (M("mystery", "voting", "openrouter", "noslug"),)
    o4 = cc.leader_family_overlap(M("claude", "voting", "claude_subprocess",
                                    "claude-opus-5"), bench4)
    check("unresolvable seat reported as undetermined", o4["undetermined"] == ["mystery"])
    check("overlaps stays False (nothing MATCHED)", o4["overlaps"] is False)
    b4 = cc.format_family_overlap_banner(o4)
    check("banner still fires for an undetermined seat", b4 is not None)
    check("banner refuses to call it clean", b4 and "UNDETERMINED" in b4)

    print("\n-- G. no leader -> nothing to report --")
    # PASSING None AS `leader` DOES NOT TEST THIS. None is the SENTINEL meaning "use the
    # live LEADER_MEMBER" (`leader if leader is not None else LEADER_MEMBER`), so under
    # any roster with a leader it returns a dict and the check fails for a reason that has
    # nothing to do with the property. A first draft did exactly that; caught in
    # logs/2026-08-06/20260806T131659Z-547a7e3b.json (both layers).
    # The no-leader state has to be the MODULE's state, so set it.
    _saved = cc.LEADER_MEMBER
    try:
        cc.LEADER_MEMBER = None
        check("overlap is None when NO leader is seated",
              cc.leader_family_overlap(None, cc.DEFAULT_REGISTRY) is None)
    finally:
        cc.LEADER_MEMBER = _saved
    check("live LEADER_MEMBER restored", cc.LEADER_MEMBER is _saved)
    check("banner is None for a None overlap",
          cc.format_family_overlap_banner(None) is None)

    print("\n-- H. DEFAULT LEADER: absent vs omitted vs rejected --")
    tmp = Path(tempfile.mkdtemp(prefix="leader-default-"))
    r = roster_leader(str(tmp / "nope.json"))
    check("ABSENT roster -> claude leads", r["leader"].get("name") == "claude",
          str(r["leader"].get("name")))
    check("...via claude_subprocess",
          r["leader"].get("transport") == "claude_subprocess")
    check("...and no roster errors", not r.get("errors"))
    check("...with no family overlap against the shipped bench",
          not (r.get("leader_family_overlap") or {}).get("overlaps"))

    p2 = tmp / "noleader.json"
    p2.write_text(json.dumps({"members": [
        {"name": "gemini", "tier": "voting", "transport": "openrouter",
         "model": "google/gemini-3.6-flash"}]}))
    r2 = roster_leader(str(p2))
    check("roster WITHOUT a leader key -> no leader (respects the user)",
          r2["leader"].get("name") == "claude_code", str(r2["leader"].get("name")))
    check("...and it is not reported as an error", not r2.get("errors"))

    p3 = tmp / "bad.json"
    p3.write_text(json.dumps({"members": [
        {"name": "x", "tier": "bogus", "transport": "openrouter", "model": "a/b"}]}))
    r3 = roster_leader(str(p3))
    check("REJECTED roster -> still no leader", r3["leader"].get("name") == "claude_code")
    check("...and ROSTER_ERRORS distinguishes it", bool(r3.get("errors")))
    check("...overlap field is None with no leader",
          r3.get("leader_family_overlap") is None)

    n = len(checks)
    bad = [c for c in checks if not c[1]]
    print(f"\n{n - len(bad)}/{n} checks passed")
    if bad:
        print("FAILED:")
        for name, _, detail in bad:
            print(f"  - {name} {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
