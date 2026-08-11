#!/usr/bin/env python3
"""Battery for the agent-agnostic rules stack. Run from Council/:
    python3 council/tests/test_rules_stack.py

EXITS NON-ZERO ON ANY FAILURE. No API calls.

WHAT IT PINS:
  RESOLUTION  -- base always; model overlay keyed on the EXACT slug; role overlay keyed
                 on tier; absent files yield "" rather than an error or a borrowed file.
  NO FAMILY FALLBACK -- a sibling slug with no overlay of its own gets NOTHING. This is
                 the load-bearing one: family fallback is the misattribution bug the
                 split exists to remove, and its absence must be asserted rather than
                 assumed, because "no file exists yet" and "fallback is disabled" look
                 identical until a sibling file is planted. The fixture plants one.
  CONTAINMENT -- a slug from roster.json is untrusted input. Traversal must resolve to
                 None, not to a file outside the overlay root.

WHY A PLANTED FIXTURE RATHER THAN THE AMBIENT TREE: observing that glm has no overlay
today proves nothing about fallback, since no family file exists to fall back TO. The
sibling case constructs the condition and runs production code over it.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def member(model: str, tier: str = "voting") -> cc.Member:
    return cc.Member(name="probe", tier=tier, transport="openrouter",
                     model=model, fallback_model=None, capabilities=())


def containment_checks() -> None:
    print("CONTAINMENT: a roster-supplied slug is untrusted input")
    for bad in ("../../etc/passwd", "../secrets", "../../.ssh/id_rsa"):
        check(f"traversal denied: {bad!r}", cc._overlay_path("models", bad) is None)
    check("empty key denied", cc._overlay_path("models", "") is None)

    ok = cc._overlay_path("models", "z-ai/glm-5.2")
    check("a legitimate nested slug is ALLOWED (depth is not escape)",
          ok is not None and ok.name == "glm-5.2.md" and ok.parent.name == "z-ai",
          f"got {ok}")


PROBE_MODEL = "claude-opus-5"


def resolution_checks() -> None:
    print("\nRESOLUTION against the real installed files")
    base, overlay = cc.resolve_rules(member("z-ai/glm-5.2"))
    check("base is non-empty for any member", len(base) > 1000, f"{len(base)} B")
    check("base is the ground-rules file",
          "Ground rules" in base and "universal layer" in base)
    check("base carries NO dated incident narration",
          "2026-07-" not in base,
          "a date in base means memoir leaked across the partition")
    check("a model with no overlay file gets an EMPTY overlay", overlay == "",
          f"got {len(overlay)} B")

    base2, overlay2 = cc.resolve_rules(member(PROBE_MODEL))
    check("a model WITH an overlay file gets it", PROBE_MODEL in overlay2,
          f"{len(overlay2)} B")
    check("base is identical regardless of member", base2 == base)

    _, vote_overlay = cc.resolve_rules(member(PROBE_MODEL, tier="voting"))
    _, lead_overlay = cc.resolve_rules(member(PROBE_MODEL, tier=cc.LEADER))
    check("tier=leader picks up the leader role overlay",
          "LEAD WORKER" in lead_overlay, f"{len(lead_overlay)} B")
    check("tier=voting does NOT (no voting role file exists)",
          "LEAD WORKER" not in vote_overlay)
    check("the leader overlay is strictly larger than the voting one",
          len(lead_overlay) > len(vote_overlay))


def no_family_fallback_check() -> None:
    """Plant a sibling family file and assert it is NOT served. Constructs the
    condition rather than observing its absence."""
    print("\nNO FAMILY FALLBACK (planted fixture, in a TEMP overlay root)")
    # The fixture rebinds OVERLAY_ROOT to a tempdir rather than writing into the live
    # one. An earlier version planted files under the real overlays/ and rmtree'd them
    # in a finally: an interrupt would have left a served _family.md in production, and
    # if "probevendor" ever became a real vendor the cleanup would delete real rules.
    # Same discipline missing_base_check already uses for GROUND_RULES_PATH.
    tmp = Path(tempfile.mkdtemp(prefix="rules_stack_"))
    real_root = cc.OVERLAY_ROOT
    cc.OVERLAY_ROOT = tmp
    fam_dir = tmp / "models" / "probevendor"
    fam_dir.mkdir(parents=True, exist_ok=True)
    planted = fam_dir / "_family.md"
    try:
        planted.write_text("FAMILY LEVEL CONTENT DO NOT SERVE\n", encoding="utf-8")
        (fam_dir / "sibling-a.md").write_text("SIBLING A OWN CONTENT\n",
                                              encoding="utf-8")

        _, own = cc.resolve_rules(member("probevendor/sibling-a"))
        check("a model WITH its own overlay gets its own", "SIBLING A OWN" in own)

        _, other = cc.resolve_rules(member("probevendor/sibling-b"))
        check("a sibling with NO overlay gets NOTHING, not the family file",
              other == "", f"got {other[:60]!r}")
        check("the family file was reachable, so this is not a vacuous pass",
              planted.exists() and "FAMILY LEVEL" in planted.read_text())
    finally:
        cc.OVERLAY_ROOT = real_root
        shutil.rmtree(tmp, ignore_errors=True)


def missing_base_check() -> None:
    """Base absent must degrade to "" rather than raising: a missing rules file should
    not take the whole council down."""
    print("\nDEGRADATION")
    real = cc.GROUND_RULES_PATH
    cc.GROUND_RULES_PATH = Path(tempfile.mkdtemp()) / "nope.md"
    try:
        base, _ = cc.resolve_rules(member("z-ai/glm-5.2"))
        check("a missing base yields \"\" and does not raise", base == "")
    except Exception as e:  # noqa: BLE001
        check("a missing base yields \"\" and does not raise", False, repr(e))
    finally:
        cc.GROUND_RULES_PATH = real


def rules_are_configured() -> list[str]:
    """Names of the ambient files resolution_checks() needs, or [] if all are present.

    THE BASE AND THE OVERLAYS ARE OPERATOR-CREATED BY DESIGN, not shipped: the installer
    prints where the starter template is and deliberately refuses to write the file, because
    copying one project's accrued failure history into another's tree would present it as
    that user's own. So on a fresh install these are ABSENT, and that is a correct install
    rather than a broken one -- which makes their absence a SKIP condition, not a failure.
    FOUND BY RUNNING THIS SUITE FROM THE PACKAGE rather than by reading it: at
    2026-08-11T05:49:38Z a package copy exited 1 with `FAILURES: 5`, on a tree where all
    three paths below were absent, i.e. an install configured exactly as documented."""
    # FILES, NOT DIRECTORIES, AND RESOLVED THROUGH THE ENGINE'S OWN _overlay_path SO THIS CANNOT
    # DRIFT FROM WHAT resolution_checks() ACTUALLY READS. An earlier version of this guard tested
    # `(OVERLAY_ROOT / "models").is_dir()`, which is the wrong question: an operator who created
    # the two directories and left them EMPTY would satisfy it, un-skip the group, and get three
    # failures for having followed the instructions. A precondition that can be satisfied without
    # supplying what the checks need is worse than none, because it converts a clean skip into a
    # false failure.
    needed = [cc.GROUND_RULES_PATH,
              cc._overlay_path("models", PROBE_MODEL),
              cc._overlay_path("roles", cc.LEADER)]
    return [str(p) for p in needed if p is None or not Path(p).exists()]


def main() -> int:
    containment_checks()
    # THE SKIP IS ANNOUNCED, AT LENGTH, AND THAT IS THE WHOLE DESIGN. A suite that quietly
    # ran three groups instead of four would exit 0 and be indistinguishable from one that
    # ran all four -- the exact silence this project exists to remove. Everything else here
    # is portable: containment is pure logic, and the other two groups build their own
    # fixtures in a tempdir rather than reading the ambient tree.
    missing = rules_are_configured()
    if missing:
        print("\nRESOLUTION -- SKIPPED, NOT PASSED. These ambient files are absent:")
        for m in missing:
            print(f"    {m}")
        print("  They are operator-created by design; see the installer's ground-rules"
              "\n  notice and starter-prompts/ground-rules.md.template. Create them and"
              "\n  re-run to exercise base/overlay resolution. The other three groups ran.")
    else:
        resolution_checks()
    no_family_fallback_check()
    missing_base_check()
    print(f"\nFAILURES: {len(FAILURES)}" + (f" -> {FAILURES}" if FAILURES else ""))
    # EXIT 77 IS THE RUNNER'S RUNTIME-SKIP CONVENTION, and using it rather than exiting 0 is
    # the point: a partial run that exits 0 is counted as a full pass, so the runner's summary
    # would read "all passed, 0 skipped" on an install where a whole group never executed.
    # A FAILURE OUTRANKS A SKIP, always. Returning 77 while something actually failed would let
    # a real regression hide behind an unconfigured tree -- the skip would swallow it, and the
    # runner does not look at failures in a suite it has marked skipped.
    # The reason line is printed LAST because the runner takes the final stdout line as the
    # skip reason.
    if FAILURES:
        return 1
    if missing:
        print(f"SKIPPED: resolution group not run, {len(missing)} operator-created "
              f"file(s) absent (first: {missing[0]})")
        return 77
    return 0


sys.exit(main())
