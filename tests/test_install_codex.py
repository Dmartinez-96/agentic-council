#!/usr/bin/env python3
"""Tests for install_codex.py -- the Codex-led installer.

WHY THIS FILE SHIPS, when the project's other suites do not. install_codex.py MERGES INTO AN
OPERATOR'S EXISTING ~/.codex/hooks.json. Every other component here either reads, or writes a
file it alone owns; this one takes a config it did not create, rewrites it, and hands it back.
The failure that matters is not a crash -- it is a merge that silently discards handlers the
operator had. An installer with that reach and no falsifier is the shape this project exists to
prevent, so the falsifier travels with it.

COVERED: render's placeholder substitution, is_ours' documented over-match, prune_hooks'
pass-through of structure it does not model, read_config's four input shapes, merge_hooks'
per-event type refusal and idempotency, and atomic_json's residue.
NOT COVERED, and not guessed at: link_standing_rules, restore_owned_links and uninstall, which
touch ~/.config and symlink ownership; and main(), which shells out to install.py. Those need
fixtures this file does not build. Next suite, not coverage.

SECTION A IS A GUARD ON THE SUITE ITSELF, not on the installer, and it HALTS rather than merely
reporting. Sections E and F write to HOOKS_PATH, which install_codex resolves ONCE at import from
$CODEX_HOME; the other sections are pure or write to a temp path of their own. If that
redirection failed, a check that only recorded a FAIL would still let E and F run -- so the
damage would already be done to the operator's real ~/.codex/hooks.json by the time the summary
line printed it. Reporting is not enough when the reported condition is the one that makes the
rest of the run destructive, so section A RETURNS 2 and nothing after it executes.
TO EXERCISE THAT PATH, edit the `os.environ["CODEX_HOME"]` ASSIGNMENT below in a COPY of this
file. Setting CODEX_HOME in the environment will NOT do it -- that assignment overwrites the
environment before the import.

SECTIONS E AND F ARE THE HIGH-STAKES ONES, because they cover a defect the code itself records
having shipped: a FALSEY malformed value (`[]`, `0`, `""`, `False`) coerced to an empty container
and then silently replacing whatever the operator had. It lives at TWO sites -- read_config's
top-level `hooks` key, and merge_hooks' per-event value -- and the comment at the second site
says in as many words that the first was fixed while this one was left behind. Both are exercised
here with each falsey value individually, because those four inputs are one bug and a single `[]`
case would not have caught the class.

Run: python3 tests/test_install_codex.py     (exit 0 = all passed)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# SET BEFORE IMPORT, AND THIS ORDERING IS LOAD-BEARING RATHER THAN TIDY. install_codex resolves
# CODEX_HOME, HOOKS_PATH and STATE_DIR at MODULE SCOPE, so an assignment after the import would
# have no effect on them and every test would then operate on the real ~/.codex. Section A
# proves the redirection actually took, rather than trusting this comment.
_TMP = Path(tempfile.mkdtemp(prefix="install-codex-test-"))
os.environ["CODEX_HOME"] = str(_TMP / "codex-home")

import install_codex as ic  # noqa: E402

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


def quiet_reporter():
    """A Reporter whose info/step/ok are suppressed and whose err/warn are captured.

    `rep.errors` is the assertion surface: a refusal that reports nothing is indistinguishable
    from one that reported clearly, and the difference is what an operator sees when the install
    stops."""
    import install as shared
    return shared.Reporter(dry_run=False, quiet=True)


def run_quiet(fn, *args, **kwargs):
    """Call fn with stderr swallowed; return its value. Reporter.err prints to stderr
    unconditionally, and this suite deliberately triggers many refusals."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        return fn(*args, **kwargs)


def write_hooks(value) -> None:
    """Put a raw value at HOOKS_PATH. Takes ANY object, including invalid shapes, because the
    inputs under test are the ones json.dump would refuse to make idiomatic."""
    ic.HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ic.HOOKS_PATH.write_text(value if isinstance(value, str) else json.dumps(value))


def clear_hooks() -> None:
    for p in (ic.HOOKS_PATH,
              ic.HOOKS_PATH.with_name(ic.HOOKS_PATH.name + ".pre-council.bak")):
        if p.exists():
            p.unlink()


FOREIGN = {"type": "command", "command": "/opt/other/tool.sh --flag"}
OURS = {"type": "command", "command": "/somewhere/hook_env.sh /somewhere/codex_hook.py pre-tool"}


def main() -> int:
    council_root = Path("/opt/council-root")

    print("\n-- A. PRECONDITION: this suite is not editing the real ~/.codex --")
    # DISCRIMINATING BY CONSTRUCTION: if the env var were ignored, HOOKS_PATH would resolve
    # under the operator's home and these checks would fail.
    # AND IT HALTS, WHICH IS THE WHOLE POINT. A check that only records a FAIL would let sections
    # E and F run anyway, and those WRITE to HOOKS_PATH -- so the operator's real config would
    # already be overwritten by the time the summary line reported the problem. Reporting is not
    # enough when the reported condition is the one that makes the rest of the run destructive.
    redirected = (str(_TMP) in str(ic.HOOKS_PATH)
                  and not str(ic.HOOKS_PATH).startswith(str(Path.home() / ".codex"))
                  and str(_TMP) in str(ic.STATE_DIR))
    check("CODEX_HOME redirection reached the module's constants",
          str(_TMP) in str(ic.HOOKS_PATH),
          str(ic.HOOKS_PATH))
    check("and HOOKS_PATH is not under the real home",
          not str(ic.HOOKS_PATH).startswith(str(Path.home() / ".codex")),
          str(ic.HOOKS_PATH))
    check("STATE_DIR is redirected too", str(_TMP) in str(ic.STATE_DIR))
    if not redirected:
        print("\nABORTING BEFORE ANY WRITE: $CODEX_HOME did not redirect install_codex's "
              f"module-scope constants (HOOKS_PATH={ic.HOOKS_PATH}). Sections E and F write "
              "to that path and would have hit the real Codex config.")
        return 2

    print("\n-- B. render(): the template becomes a usable config --")
    rendered = ic.render(council_root)
    blob = json.dumps(rendered)
    check("every {{COUNCIL_ROOT}} placeholder is substituted", "{{" not in blob)
    check("and the substitution used the given root", str(council_root) in blob)
    check("all four lifecycle events are present",
          sorted(rendered["hooks"]) == ["PostToolUse", "PreToolUse", "SessionStart", "Stop"],
          sorted(rendered["hooks"]))
    check("every handler invokes python3 on codex_hook.py",
          all("codex_hook.py" in h["command"] and "python3" in h["command"]
              for entries in rendered["hooks"].values()
              for e in entries for h in e["hooks"]))

    print("\n-- C. is_ours(): what the pruner will claim --")
    check("a handler naming codex_hook.py by path is ours", ic.is_ours(OURS))
    check("an unrelated handler is not", not ic.is_ours(FOREIGN))
    # PINNING A KNOWN OVER-MATCH, NOT ENDORSING IT. The docstring says a foreign-rooted
    # codex_hook.py is treated as ours; if that ever changes, this check must be the thing that
    # notices, because the behaviour is a pruning decision on someone else's config.
    check("a DIFFERENTLY-ROOTED codex_hook.py is also claimed (documented over-match)",
          ic.is_ours({"command": "/opt/someone-else/wrapper /opt/someone-else/codex_hook.py x"}))
    check("a bare word cannot match -- only a path can",
          not ic.is_ours({"command": "echo codex_hook.py"}))
    check("a non-dict handler is not ours", not ic.is_ours(["codex_hook.py"]))
    check("a handler with no command is not ours", not ic.is_ours({"type": "command"}))
    # shlex.split raises on an unbalanced quote; is_ours must answer False rather than propagate,
    # because a config it cannot tokenise is one it must not claim.
    check("an untokenisable command is not ours, and does not raise",
          not ic.is_ours({"command": "cmd 'unbalanced"}))

    print("\n-- D. prune_hooks(): remove ours, keep everything else --")
    pruned, removed = ic.prune_hooks({"PreToolUse": [{"matcher": ".*", "hooks": [OURS, FOREIGN]}]})
    check("one handler was pruned", removed == 1, f"removed={removed}")
    check("the foreign handler survived",
          pruned["PreToolUse"][0]["hooks"] == [FOREIGN])
    check("and the entry's other keys survived with it",
          pruned["PreToolUse"][0]["matcher"] == ".*")
    emptied, n = ic.prune_hooks({"Stop": [{"hooks": [OURS]}]})
    check("an event left with no handlers is dropped entirely",
          emptied == {} and n == 1, f"{emptied} removed={n}")
    # STRUCTURE THIS VERSION DOES NOT MODEL MUST SURVIVE A RE-RUN. Normalising it would rewrite
    # an operator's unfamiliar config into this version's idea of one.
    odd = {"PreToolUse": "not-a-list", "Weird": [{"no_hooks_key": 1}]}
    passthrough, n2 = ic.prune_hooks(odd)
    check("unmodelled structure passes through verbatim and untouched",
          passthrough == odd and n2 == 0, str(passthrough))

    print("\n-- E. read_config(): four input shapes, three of them refusals --")
    rep = quiet_reporter()
    write_hooks("{not json")
    check("unparseable JSON is refused",
          run_quiet(ic.read_config, rep, ic.HOOKS_PATH) == (None, None))
    check("and the refusal is REPORTED, not silent", len(rep.errors) == 1, str(rep.errors[-1:]))

    rep = quiet_reporter()
    write_hooks([1, 2, 3])
    check("a non-object top level is refused rather than crashing",
          run_quiet(ic.read_config, rep, ic.HOOKS_PATH) == (None, None))
    check("and it says so", any("non-object top level" in e for e in rep.errors))

    # THE DEFECT THE CODE'S OWN COMMENT RECORDS, ONE INPUT AT A TIME. `current.get("hooks") or {}`
    # turned each of these into {} and then sailed past an isinstance check, discarding the
    # operator's config with no error.
    for bad in ([], 0, "", False):
        rep = quiet_reporter()
        write_hooks({"hooks": bad})
        got = run_quiet(ic.read_config, rep, ic.HOOKS_PATH)
        check(f"a FALSEY non-dict hooks value ({bad!r}) is refused, not coerced to empty",
              got == (None, None) and rep.errors, f"got={got}")

    rep = quiet_reporter()
    write_hooks({"hooks": {"Stop": []}})
    check("a well-formed config is accepted",
          run_quiet(ic.read_config, rep, ic.HOOKS_PATH) == ({"hooks": {"Stop": []}}, {"Stop": []}))
    rep = quiet_reporter()
    write_hooks({"description": "no hooks key here"})
    cfg, hooks = run_quiet(ic.read_config, rep, ic.HOOKS_PATH)
    check("an ABSENT hooks key is the one shape that is fine, and means empty",
          hooks == {} and cfg is not None and not rep.errors)

    print("\n-- F. merge_hooks(): the second falsey site, refusal, and idempotency --")
    # THE SITE THAT WAS LEFT BEHIND when read_config was fixed. Same class, different key.
    for bad in (0, "", False):
        clear_hooks()
        write_hooks({"hooks": {"Stop": bad}})
        rep = quiet_reporter()
        before = ic.HOOKS_PATH.read_text()
        ok = run_quiet(ic.merge_hooks, rep, council_root)
        check(f"a falsey per-event value ({bad!r}) is refused, not replaced",
              ok is False and rep.errors, f"returned={ok}")
        check(f"and the operator's file is UNCHANGED after refusing ({bad!r})",
              ic.HOOKS_PATH.read_text() == before)

    clear_hooks()
    write_hooks({"hooks": {"Stop": None}})
    rep = quiet_reporter()
    check("a per-event None is treated as absent, not as a type error",
          run_quiet(ic.merge_hooks, rep, council_root) is True and not rep.errors)

    clear_hooks()
    write_hooks({"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [FOREIGN]}]}})
    rep = quiet_reporter()
    run_quiet(ic.merge_hooks, rep, council_root)
    after_first = json.loads(ic.HOOKS_PATH.read_text())
    foreign_kept = [h for e in after_first["hooks"]["PreToolUse"] for h in e["hooks"]
                    if h == FOREIGN]
    check("a foreign handler SURVIVES the merge", foreign_kept == [FOREIGN])
    check("a backup of the pre-existing file was written",
          ic.HOOKS_PATH.with_name(ic.HOOKS_PATH.name + ".pre-council.bak").exists())

    # IDEMPOTENCY IS THE PROPERTY AN INSTALLER IS RE-RUN FOR. Without prune-then-add, a second
    # run doubles every handler and Codex fires the council twice per tool call.
    rep = quiet_reporter()
    run_quiet(ic.merge_hooks, rep, council_root)
    after_second = json.loads(ic.HOOKS_PATH.read_text())
    check("re-running is IDEMPOTENT -- no handler is duplicated",
          after_second == after_first, "second run differed")
    ours_count = sum(1 for entries in after_second["hooks"].values()
                     for e in entries for h in e["hooks"] if ic.is_ours(h))
    check("exactly one of our handlers per event survives a re-run",
          ours_count == 4, f"ours={ours_count}")

    print("\n-- G. atomic_json(): no residue, and a parent that may not exist --")
    target = _TMP / "made" / "up" / "path" / "out.json"
    ic.atomic_json(target, {"a": 1})
    check("it creates the parent directory", target.exists())
    check("and writes valid JSON", json.loads(target.read_text()) == {"a": 1})
    check("leaving no .partial file behind",
          not list(target.parent.glob("*.partial")),
          str(list(target.parent.glob("*.partial"))))

    print("\n-- H. the pre-flight names the installer refuses to run without --")
    check("REQUIRED_NAMES covers codex_hook.py's module-scope import",
          "brain_index.py" in ic.REQUIRED_NAMES, str(ic.REQUIRED_NAMES))
    check("and the roster profile, whose absence makes codex_hook deny every edit",
          "roster.codex-led.json" in ic.REQUIRED_NAMES)

    total = PASS + FAIL
    print(f"\n{PASS}/{total} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
