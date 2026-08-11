#!/usr/bin/env python3
"""Tests for codex_hook.py -- the Codex CLI lifecycle hook (Codex as LEADER).

COVERED: the emit_* protocol shapes, state keying, path containment and symlink refusal,
apply_patch grammar parsing, state-file durability, and roster-profile validation -- pure or
near-pure functions whose failure would be SILENT rather than loud.
NOT COVERED, and not guessed at: prepare_snapshot, restore, archive, reconcile_pending,
run_council, and the pre_tool/post_tool/stop/session_start dispatchers. Those cross turns or
shell out to the council and need fixtures this file does not build. Next suite, not coverage.

SECTION A IS THE HIGHEST-STAKES ONE, by arithmetic. emit_post decides whether a verdict reaches
Codex as a BLOCK, as advisory context, or as nothing. If its branches drift, BLOCK becomes
unreachable and every review degrades to advice -- indistinguishable from a clean run.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# SET BEFORE IMPORT. state_root() reads COUNCIL_STATE_ROOT at call time, but COUNCIL_ROOT is
# module-level, so the import needs a sane environment. Pointing the state root at a temp dir
# keeps every test off the operator's real ~/.codex/state.
_TMP = tempfile.mkdtemp(prefix="codex-hook-test-")
os.environ["COUNCIL_STATE_ROOT"] = _TMP

import codex_hook as h  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def emitted(fn, *args) -> dict | None:
    """Run an emit_* function; return the JSON it wrote, or None if it wrote nothing.

    NONE IS A MEANINGFUL RESULT: emit_post writing nothing is how a clean complete-bench PASS
    is signalled, so a helper that could not tell "wrote nothing" from "wrote something
    unparseable" would pass either way.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    text = buf.getvalue().strip()
    return json.loads(text) if text else None


def raises(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except Exception:
        return True
    return False


def patch(*body: str) -> str:
    return "\n".join(("*** Begin Patch",) + body + ("*** End Patch",))


def main() -> int:
    tmp = Path(_TMP)
    cwd = Path(tempfile.mkdtemp(prefix="codex-hook-cwd-"))

    # --- A. protocol shapes: the hook's contract with Codex. Drift here does not raise.
    print("\n=== A. protocol shapes ===")
    deny = emitted(h.emit_pre_deny, "because reasons")
    hso = (deny or {}).get("hookSpecificOutput", {})
    check("pre deny names the PreToolUse event", hso.get("hookEventName") == "PreToolUse")
    check("pre deny decides deny", hso.get("permissionDecision") == "deny")
    check("pre deny carries the reason", hso.get("permissionDecisionReason") == "because reasons")
    check("pre deny also sets systemMessage", (deny or {}).get("systemMessage") == "because reasons")

    ctx = emitted(h.emit_pre_context, "just so you know")
    hso = (ctx or {}).get("hookSpecificOutput", {})
    check("pre context decides ALLOW, not deny", hso.get("permissionDecision") == "allow")
    check("pre context carries additionalContext",
          hso.get("additionalContext") == "just so you know")
    # An advisory must not carry a field a stricter reader could take for a refusal.
    check("pre context has no permissionDecisionReason", "permissionDecisionReason" not in hso)

    out = emitted(h.emit_post, {"rc": 2, "text": "BLOCK: undo it", "bench_complete": True})
    check("rc=2 blocks", (out or {}).get("decision") == "block", str(out)[:70])
    check("rc=2 block carries the verdict text", (out or {}).get("reason") == "BLOCK: undo it")

    out = emitted(h.emit_post, {"rc": 1, "text": "WARN: careful", "bench_complete": True})
    check("rc=1 is advisory context, NOT a block",
          "decision" not in (out or {}) and
          (out or {}).get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse")

    out = emitted(h.emit_post, {"rc": 0, "text": "PASS", "bench_complete": True})
    check("rc=0 with a COMPLETE bench emits NOTHING", out is None, repr(out)[:60])

    # A PASS ON AN INCOMPLETE BENCH IS NOT A PASS: half the seats returning nothing and all of
    # them agreeing look identical in the verdict line. Only bench_complete separates them.
    out = emitted(h.emit_post, {"rc": 0, "text": "PASS", "bench_complete": False})
    check("rc=0 with an INCOMPLETE bench still surfaces context", out is not None, str(out)[:70])

    # FAIL-CLOSED ON THE UNEXPECTED. A wrapper that died in a new way must not read as PASS.
    out = emitted(h.emit_post, {"rc": 99, "text": "exploded", "bench_complete": True})
    check("an unrecognised rc BLOCKS rather than passing", (out or {}).get("decision") == "block")
    out = emitted(h.emit_post, {"text": "no rc at all", "bench_complete": True})
    check("a MISSING rc blocks", (out or {}).get("decision") == "block", str(out)[:60])
    out = emitted(h.emit_post, {"rc": 0, "state_error": "corrupt", "text": "x",
                                "bench_complete": True})
    check("a state_error blocks even on rc=0", (out or {}).get("decision") == "block")
    out = emitted(h.emit_post, {"rc": 2, "bench_complete": True})
    check("a blocking result with NO text still says something",
          bool((out or {}).get("reason")), str(out)[:70])

    # --- B. state keying: a partial key would collide across turns, sharing one snapshot dir.
    print("\n=== B. state keying ===")
    full = {"session_id": "s", "turn_id": "t", "tool_use_id": "u"}
    k1 = h.state_key(full)
    check("state_key is a sha256 hex digest",
          len(k1) == 64 and all(c in "0123456789abcdef" for c in k1))
    check("state_key is stable for the same payload", h.state_key(dict(full)) == k1)
    check("changing turn_id changes the key", h.state_key({**full, "turn_id": "other"}) != k1)
    for missing in ("session_id", "turn_id", "tool_use_id"):
        partial = {k: v for k, v in full.items() if k != missing}
        check(f"a payload missing {missing} RAISES rather than keying on the rest",
              raises(h.state_key, partial))
    # Without a separator, ("ab","c") and ("a","bc") would collide.
    check("field boundaries are not ambiguous",
          h.state_key({"session_id": "ab", "turn_id": "c", "tool_use_id": "u"}) !=
          h.state_key({"session_id": "a", "turn_id": "bc", "tool_use_id": "u"}))

    # --- C. containment: the state dir holds the snapshots used to RESTORE after a BLOCK, so
    # a patch able to write there could destroy the evidence of its own damage.
    print("\n=== C. path containment ===")
    check("_under is true for a real child", h._under(tmp, tmp / "a" / "b"))
    check("_under is false for the sibling-prefix trap",
          not h._under(tmp, Path(str(tmp) + "_evil")))
    check("_under is false for a parent", not h._under(tmp / "a", tmp))
    check("a patch may not target the state root itself",
          raises(h._target_path, str(h.state_root()), cwd))
    check("a patch may not target anything under the state root",
          raises(h._target_path, str(h.state_root() / "sessions" / "x"), cwd))
    check("an ordinary path resolves fine",
          h._target_path("notes.md", cwd) == str((cwd / "notes.md").absolute()))
    check("a relative path is resolved against the payload cwd",
          h._target_path("notes.md", cwd).startswith(str(cwd)))
    check("an empty path raises", raises(h._target_path, "", cwd))
    check("a NUL-containing path raises", raises(h._target_path, "a\0b", cwd))
    # `..` must be normalised BEFORE the containment test, or a path that lexically looks like
    # it points elsewhere can still resolve inside the state root.
    escape = str(h.state_root() / ".." / "agentic-council" / "sessions")
    check("a `..` path that still resolves INSIDE the state root is refused",
          raises(h._target_path, escape, cwd), escape[-46:])

    link_dir = Path(tempfile.mkdtemp(prefix="codex-hook-link-"))
    real = link_dir / "real"
    real.mkdir()
    link = link_dir / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        check("symlink tests could run", False, f"symlink unsupported here: {exc}")
    else:
        check("a symlink component is refused",
              raises(h._reject_symlink_components, link / "file.json"))
        check("the equivalent real path is accepted",
              not raises(h._reject_symlink_components, real / "file.json"))

    # --- D. apply_patch grammar. A MISREAD patch is worse than a rejected one: the snapshot
    # then covers the wrong files and the restore-after-BLOCK protects nothing.
    print("\n=== D. apply_patch grammar ===")
    a = h.analyze_patch(patch("*** Add File: new.txt", "+hello"), cwd)
    check("Add File yields one operation", len(a["operations"]) == 1)
    check("Add File records the kind", a["operations"][0]["kind"] == "add")
    check("added lines are captured without the + marker", a["operations"][0]["added"] == ["hello"])
    check("the target is absolute", Path(a["targets"][0]["path"]).is_absolute())

    a = h.analyze_patch(patch("*** Update File: a.txt", "*** Move to: b.txt", "+x"), cwd)
    check("Move to is recorded as a destination",
          a["operations"][0]["destination"] == str((cwd / "b.txt").absolute()))
    check("a move registers BOTH paths as targets", len(a["targets"]) == 2)
    # Real paths must not reach the council: the rewritten patch is what gets pitched.
    check("the rewritten patch replaces real paths with synthetic names",
          "a.txt" not in a["rewritten"] and "target-0000" in a["rewritten"])
    check("the rewritten patch keeps its boundaries",
          a["rewritten"].startswith("*** Begin Patch") and
          a["rewritten"].rstrip().endswith("*** End Patch"))

    check("missing Begin/End boundaries raise", raises(h.analyze_patch, "*** Add File: x", cwd))
    check("an empty patch raises", raises(h.analyze_patch, patch(), cwd))
    check("an unknown *** marker raises",
          raises(h.analyze_patch, patch("*** Frobnicate File: x"), cwd))
    check("a remote Environment ID patch is refused",
          raises(h.analyze_patch, patch("*** Environment ID: abc", "*** Add File: x", "+y"), cwd))
    check("the same target twice in one patch raises",
          raises(h.analyze_patch, patch("*** Add File: dup.txt", "+a",
                                        "*** Add File: dup.txt", "+b"), cwd))
    check("a move onto itself raises",
          raises(h.analyze_patch, patch("*** Update File: same.txt",
                                        "*** Move to: same.txt", "+x"), cwd))
    check("two Move to markers in one operation raise",
          raises(h.analyze_patch, patch("*** Update File: a.txt", "*** Move to: b.txt",
                                        "*** Move to: c.txt", "+x"), cwd))
    check("a patch targeting the state root raises",
          raises(h.analyze_patch,
                 patch(f"*** Add File: {h.state_root() / 'sneak.json'}", "+x"), cwd))
    a = h.analyze_patch(patch("*** Delete File: gone.txt"), cwd)
    check("Delete File needs no added lines", a["operations"][0]["kind"] == "delete")

    # --- E. durability: a corrupt manifest loading as {} would discard a pending review and
    # look clean doing it.
    print("\n=== E. state file durability ===")
    sf = tmp / "sub" / "state.json"
    h.atomic_json(sf, {"a": 1})
    check("atomic_json round-trips", h.read_json(sf) == {"a": 1})
    check("the file is written 0600", stat.S_IMODE(os.lstat(sf).st_mode) == 0o600,
          oct(stat.S_IMODE(os.lstat(sf).st_mode)))
    check("no .partial temp files are left behind", not list(sf.parent.glob(".partial-*")))
    h.atomic_bytes(sf, b"{ this is not json")
    check("a CORRUPT state file raises rather than loading as {}", raises(h.read_json, sf))
    # AN EMPTY FILE IS A DISTINCT CORRUPTION CASE -- how it got that way is NOT established
    # here, and an earlier version of this comment called it "the interrupted-write case",
    # which the council refuted from this module's own code: atomic_bytes writes a .partial-*
    # temp and then os.replace, so an interrupted write leaves an orphan temp or the PRIOR
    # contents, not an empty target. What matters is the code path, not the cause: the read
    # loop collects no chunks at all, so a guard written as
    # `json.loads(joined) if joined else {}` would swallow it while the malformed-bytes test
    # above still passed. Found by mutation-testing this suite -- that exact mutation survived
    # until this check existed.
    h.atomic_bytes(sf, b"")
    check("an EMPTY state file raises rather than loading as {}", raises(h.read_json, sf))
    h.atomic_bytes(sf, b"   \n")
    check("a whitespace-only state file raises", raises(h.read_json, sf))
    h.atomic_bytes(sf, b"[1, 2, 3]")
    check("a JSON ARRAY raises rather than being treated as a mapping", raises(h.read_json, sf))
    h.atomic_json(sf, {"a": 1})
    check("the size limit is enforced", raises(h.read_json, sf, 2))
    check("a missing state file raises", raises(h.read_json, tmp / "nope.json"))

    # --- F. roster validation: pre_tool DENIES an apply_patch when this returns a message.
    print("\n=== F. codex profile validation ===")
    saved = os.environ.get("COUNCIL_ROSTER_PATH")
    try:
        os.environ.pop("COUNCIL_ROSTER_PATH", None)
        check("an unset roster path is an error", h._profile_error() is not None)

        os.environ["COUNCIL_ROSTER_PATH"] = str(tmp / "absent.json")
        check("an unreadable roster is an error", h._profile_error() is not None)

        bad = tmp / "bad.json"
        h.atomic_json(bad, {"members": []})
        os.environ["COUNCIL_ROSTER_PATH"] = str(bad)
        check("a roster with no leader is an error", h._profile_error() is not None)

        # If the shipped roster does NOT validate, every apply_patch in a codex session is
        # being denied right now. Loud rather than silent, and still worth a test.
        real_roster = ROOT / "roster.codex-led.json"
        os.environ["COUNCIL_ROSTER_PATH"] = str(real_roster)
        err = h._profile_error()
        check("the shipped roster.codex-led.json VALIDATES", err is None, str(err)[:110])

        value = json.loads(real_roster.read_text())
        value["leader"]["name"] = "claude"
        wrong = tmp / "wrong-leader.json"
        h.atomic_json(wrong, value)
        os.environ["COUNCIL_ROSTER_PATH"] = str(wrong)
        check("a non-codex leader is rejected", h._profile_error() is not None)

        value = json.loads(real_roster.read_text())
        value["members"] = [m for m in value["members"] if m["name"] != "grok"]
        short = tmp / "short-bench.json"
        h.atomic_json(short, value)
        os.environ["COUNCIL_ROSTER_PATH"] = str(short)
        check("a bench missing one voter is rejected", h._profile_error() is not None)
    finally:
        if saved is None:
            os.environ.pop("COUNCIL_ROSTER_PATH", None)
        else:
            os.environ["COUNCIL_ROSTER_PATH"] = saved

    # --- G. exemption scope: an over-broad exemption silently disables the laziness check.
    print("\n=== G. exemption scope ===")
    check("the council tree itself is exempt", h._is_exempt(str(ROOT / "consult_council.py")))
    check("the package tree is exempt",
          h._is_exempt(str(ROOT.parent / "agentic-council" / "council" / "x.py")))
    check("an unrelated project is NOT exempt",
          not h._is_exempt(str(Path.home() / "Documents" / "SomeProject" / "main.py")))
    check("the sibling-prefix trap does not grant exemption",
          not h._is_exempt(str(ROOT.parent / "Council-evil" / "x.py")),
          str(ROOT.parent / "Council-evil" / "x.py"))
    check("a memory tree is exempt", h._is_exempt("/home/u/.claude/projects/p/memory/note.md"))

    total = PASS + FAIL
    print(f"\n{PASS}/{total} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
