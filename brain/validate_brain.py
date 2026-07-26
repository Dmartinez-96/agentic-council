#!/usr/bin/env python3
"""Validate a council BRAIN vault: a ledger of observations, not a knowledge base.

WHAT THIS IS FOR. Prose documents go stale silently -- a number written into a
paragraph is correct when written and stays there long after it stops being true.
A brain note does not restate a fact; it stores the CHECK that produces the fact,
and this validator re-runs those checks.

WHAT IT IS NOT FOR. It holds FACTS, not REASONING. Design rationale, research
syntheses, and lessons are neither observations nor attributed statements, and
forcing them into this schema would launder opinion as fact -- which is the exact
failure the schema exists to prevent. Keep those in prose; let the prose LINK to
notes instead of restating their numbers.

HONEST STANDING OF THE PREMISE (do not let this harden): the motivation is that a
2,000+ line append-mostly handoff propagated stale numbers twice in one session.
Whether atomic checkable notes PREVENT that is UNTESTED. This design has not been
compared against any alternative. It is a hypothesis being tried, not a fix known
to work.

DESIGN RESOLVED BY THE FULL 12-MEMBER COUNCIL, thread 20260726T023302Z-5a39b4,
consensus PASS across 6 rounds. Points that survived attack, each of which changed
the design:
  - The CLAIM IS RENDERED from the check, never authored. An authored claim beside
    a check re-opens opinion-laundering, because nothing forces correspondence.
  - A note CANNOT ASSERT ITS OWN FRESHNESS. There is no `verified` field. The
    validator owns a separate attestation ledger; verification is a property of the
    ledger, not a claim in the note.
  - An attestation is keyed by a hash of the COMPLETE NORMALIZED CHECK. Hashing
    only the command would let an edit to `expect`, `exit_status`, or a path inherit
    a prior PASS -- a stale verification wearing a fresh badge.
  - A FAILING CHECK IS NEVER "FALSE". The world may have changed, the check may
    have rotted, or it may be transient; those are indistinguishable here and only
    the first is a fact change. Failures report NEEDS_ADJUDICATION, for a human.
  - AN UNRUN CHECK IS NEVER A PASS. Budget exhaustion reports NOT_RUN.

WHAT THIS DOES NOT FIX, stated plainly because the schema can look like more than
it is: it does NOT close the void-check hole. A check whose output is the same
whether the fact is true or false will pass, and be attested, and look authoritative.
The `falsifier` field is a REQUIRED AUTHORING STEP that forces the author to write
down what a false fact would print -- it makes the omission visible to a human. The
validator cannot check it. Two specific void shapes ARE caught mechanically: a
self-echoing `expect` (rejected outright), and a command whose error text happens to
contain `expect` (a matching exit status is REQUIRED). The design review also accepted
a named-stream mitigation; implementation found it IMPOSSIBLE -- the sandbox returns
combined stdout+stderr and no caller can separate them -- so it was dropped rather
than shipped as a field the code ignores. See the note above CHECK_KINDS.

SAFETY. `check_kind: command` executes text from note files. It runs ONLY under
`--run-commands` (default OFF) and ONLY through the engine's bubblewrap sandbox
(network off, env cleared, tmpfs, rlimits, process-group kill, over a scrubbed
ephemeral copy; fail-closed when bwrap is absent). The AUTHORED form is an argv
array which is shell-quoted before it reaches the sandbox -- this removes shell
metacharacter surface at the AUTHORING layer, but the sandbox interface is `sh -c`,
so this is NOT an argv-only execution path and must not be described as one.
THE REAL BOUNDARY IS AUTHORSHIP: a vault is authored by you or your harness, never
by a council member. Importing someone else's vault means agreeing to run their
commands.

Run:  python3 brain/validate_brain.py <vault-dir> [--root DIR] [--run-commands]
                                      [--budget-seconds N] [--json]
"""

import argparse
import hashlib
import json
import re
import shlex
import sys
import time
from datetime import date
from pathlib import Path

CHECK_KINDS = ("file", "url", "command")
LIST_FIELDS = ("supersedes", "tags")

# THERE IS NO `stream` FIELD, and the reason matters. The design review accepted
# "named stream + exit status" as a mitigation for checks that match error text
# instead of a real result. On implementation the first half turned out to be
# impossible: `run_exec_sandbox` returns "(combined stdout+stderr, note)" -- the
# streams are MERGED inside the sandbox and no caller can separate them. A `stream`
# field would therefore be a guarantee the code cannot keep, so it is rejected
# rather than accepted-and-ignored. What survives of that mitigation is the REQUIRED
# exit status. Write command checks to be unambiguous in COMBINED output.

# Status vocabulary. NOT_RUN and NEEDS_ADJUDICATION are deliberately distinct from
# FAIL: only a human decides whether a failing check means the fact changed or the
# check rotted.
OK, NEEDS_ADJ, NOT_RUN, INVALID, SUPERSEDED = (
    "PASS", "NEEDS_ADJUDICATION", "NOT_RUN", "INVALID", "SUPERSEDED")


def _engine():
    """Import the council engine for its hardened fetch/exec primitives.

    Returns the module or None. The brain deliberately does NOT reimplement URL
    validation or sandboxing: `fetch_web_url` carries the exact-host allowlist, the
    per-hop redirect revalidation and the DNS-rebinding IP pin, and
    `run_exec_sandbox` carries the bubblewrap containment. Reimplementing either
    here would mean a second, weaker copy of a security boundary.
    """
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "council", here.parent):
        if (cand / "consult_council.py").exists():
            sys.path.insert(0, str(cand))
            try:
                import consult_council  # noqa: PLC0415
                return consult_council
            except Exception:  # noqa: BLE001 -- a broken engine must not crash validation
                return None
    return None


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------

def parse_note(path):
    """Parse one note into (fields, body, errors).

    The schema is deliberately FLAT -- no nested mappings -- so this parser needs no
    YAML dependency and cannot disagree with a real YAML parser about indentation.
    Anything it cannot parse is an error, never a guess.
    """
    errors = []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {}, "", [f"unreadable: {e.__class__.__name__}"]
    if not raw.startswith("---\n"):
        return {}, raw, ["missing YAML frontmatter (file must start with '---')"]
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw, ["frontmatter not terminated by a '---' line"]
    head, body = raw[4:end], raw[end + 5:]
    fields = {}
    for lineno, line in enumerate(head.split("\n"), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            errors.append(f"line {lineno}: not a 'key: value' pair")
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key in LIST_FIELDS:
            val = val.strip("[]")
            fields[key] = [v.strip().strip("'\"") for v in val.split(",") if v.strip()]
        else:
            fields[key] = val.strip("'\"")
    return fields, body, errors


def validate_fields(f):
    """Schema validation. Returns a list of errors; empty means structurally valid."""
    errs = []
    t = f.get("type")
    if t not in ("checkable", "testimony"):
        return [f"type must be 'checkable' or 'testimony', got {t!r}"]
    if not f.get("id"):
        errs.append("missing required field: id")

    if t == "testimony":
        for k in ("statement", "attributed_to", "date"):
            if not f.get(k):
                errs.append(f"missing required field for testimony: {k}")
        # A testimony note that claims to be ground truth defeats the whole point of
        # having two types, so the field is pinned rather than merely defaulted.
        if f.get("citable_as_ground_truth", "false").lower() != "false":
            errs.append("testimony must set citable_as_ground_truth: false")
        return errs

    kind = f.get("check_kind")
    if kind not in CHECK_KINDS:
        errs.append(f"check_kind must be one of {CHECK_KINDS}, got {kind!r}")
    # `expect` may be EMPTY for a command check, and only for a command check: a
    # linter or test suite whose fact is "this exits 0" has no natural substring, and
    # forcing one would make authors invent a token to satisfy the schema. When it is
    # empty the exit status carries ALL the discriminating power -- which is why
    # exit_status is required below. file and url checks have no exit status to lean
    # on, so an empty expect there would match everything and establish nothing.
    if "expect" not in f:
        errs.append("missing required field: expect")
    elif not f["expect"] and kind != "command":
        errs.append(f"expect may only be empty for a command check (which has an "
                    f"exit_status to discriminate on); an empty expect for a "
                    f"{kind} check matches everything")
    if not f.get("falsifier"):
        errs.append("missing required field: falsifier (what this check prints if "
                    "the fact is FALSE -- if you cannot state it, the check "
                    "establishes nothing)")
    if kind == "file" and not f.get("check_path"):
        errs.append("check_kind: file requires check_path")
    if kind == "url" and not f.get("check_url"):
        errs.append("check_kind: url requires check_url")
    if kind == "command":
        if not f.get("check_argv"):
            errs.append("check_kind: command requires check_argv (a JSON array, "
                        "NOT a shell string)")
        else:
            try:
                argv = json.loads(f["check_argv"])
                if not isinstance(argv, list) or not argv or not all(
                        isinstance(a, str) for a in argv):
                    errs.append("check_argv must be a non-empty JSON array of strings")
            except ValueError:
                errs.append("check_argv is not valid JSON")
        if "stream" in f:
            errs.append("there is no 'stream' field: the sandbox returns COMBINED "
                        "stdout+stderr and no caller can separate them, so a stream "
                        "selector would be a guarantee this cannot keep. Write the "
                        "check to be unambiguous in combined output")
        # REQUIRED, not optional: with streams merged, the exit status is the only
        # remaining structural signal separating "the command reported the fact" from
        # "the command failed and its error text happened to contain expect".
        if "exit_status" not in f:
            errs.append("check_kind: command requires exit_status")
        else:
            try:
                int(f["exit_status"])
            except ValueError:
                errs.append("exit_status must be an integer")

    # SELF-ECHO GUARD. A check whose expected substring appears in the check itself
    # can match the command being echoed back rather than any real result. This is
    # not hypothetical: this project recorded a member echoing the prompt into
    # stderr, so a sentinel grep matched the author's own words and "passed" on a
    # run that had failed.
    expect = f.get("expect", "")
    haystack = " ".join(str(f.get(k, "")) for k in
                        ("check_argv", "check_url", "check_path"))
    if expect and expect in haystack:
        errs.append("expect appears inside the check itself (self-echo): this check "
                    "can match its own text and cannot establish the fact")
    return errs


def rendered_claim(f):
    """The claim, GENERATED from the check. Never authored, never read from a field."""
    when = "when last run"
    if f["check_kind"] == "file":
        return (f"reading {f['check_path']} {when} produced content containing "
                f"{f['expect']!r}")
    if f["check_kind"] == "url":
        return (f"fetching {f['check_url']} {when} produced a body containing "
                f"{f['expect']!r}")
    argv = json.loads(f["check_argv"])
    return (f"running {shlex.join(argv)} {when} produced combined output containing "
            f"{f['expect']!r} and exit status {f['exit_status']}")


def spec_hash(f):
    """Hash the COMPLETE normalized check.

    Every field that changes what the check MEANS is included. Hashing a subset
    (the command alone, say) would let an edit to `expect` or `exit_status` inherit
    the previous PASS -- a stale verification presented as current.
    """
    parts = [f.get(k, "") for k in
             ("check_kind", "check_path", "check_url", "check_argv",
              "expect", "exit_status", "falsifier")]
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()


# --------------------------------------------------------------------------
# running checks
# --------------------------------------------------------------------------

_EXIT_RE = re.compile(r"\bexit\s+(-?\d+)\b")


def run_check(f, root, engine, allow_commands):
    """Execute one check. Returns (status, detail).

    Never raises: any failure is a verdict, because an exception here would look
    like a missing result rather than a failed check.
    """
    kind = f["check_kind"]
    expect = f["expect"]

    if kind == "file":
        p = (root / f["check_path"]).resolve()
        try:
            # Containment is a PATH test, not a string-prefix test. An earlier version
            # used str(p).startswith(str(root.resolve())), which accepts a SIBLING whose
            # name merely extends the root's. Measured end to end 2026-07-26: with a
            # temp root <tmp>/root, a check_path of ../root-evil/secret.md resolved
            # outside that root and this function returned PASS on it, so the ledger
            # would have attested a file the vault does not own. is_relative_to
            # compares path COMPONENTS, so it rejects that; build_sandbox_copy already
            # used it, and this path is now consistent with it.
            if not p.is_relative_to(root.resolve()):
                return NEEDS_ADJ, "check_path escapes --root"
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return NEEDS_ADJ, f"unreadable ({e.__class__.__name__})"
        return (OK, "expect present") if expect in text else (
            NEEDS_ADJ, "expect ABSENT from file")

    if kind == "url":
        if engine is None:
            return NOT_RUN, "engine unavailable; url checks need fetch_web_url"
        body, note = engine.fetch_web_url(f["check_url"])
        if body is None:
            # Includes allowlist denials: v1 is coupled to the engine's fixed host
            # allowlist by design, and a denial is not a fact change.
            return NEEDS_ADJ, f"fetch denied/failed: {note}"
        return (OK, note) if expect in body else (NEEDS_ADJ, f"expect ABSENT ({note})")

    if not allow_commands:
        return NOT_RUN, "command checks disabled (pass --run-commands to enable)"
    if engine is None:
        return NOT_RUN, "engine unavailable; command checks need run_exec_sandbox"
    argv = json.loads(f["check_argv"])
    out, note = engine.run_exec_sandbox(shlex.join(argv), root)
    if out is None:
        return NOT_RUN, f"sandbox refused: {note}"
    if expect not in out:
        return NEEDS_ADJ, f"expect ABSENT ({note})"
    want = f.get("exit_status")
    if want is None:
        return OK, note
    # BRITTLE COUPLING, stated rather than hidden: run_exec_sandbox returns
    # (text, note) and puts the exit code in the human-readable note as "exit N".
    # There is no structural return for it, so this parses that string. If the note
    # format changes this stops matching -- and it then FAILS CLOSED rather than
    # silently treating an unknown status as a pass. A structural return is a
    # recorded engine follow-up.
    m = _EXIT_RE.search(note or "")
    if not m:
        return NEEDS_ADJ, f"exit status required but unreadable from note: {note!r}"
    if int(m.group(1)) != int(want):
        return NEEDS_ADJ, f"exit status {m.group(1)}, wanted {want}"
    return OK, note


# --------------------------------------------------------------------------
# integrity across the vault
# --------------------------------------------------------------------------

def integrity(notes):
    """Cross-note checks. A link graph that lies is worse than no link graph."""
    problems = []
    by_id = {}
    for n in notes:
        i = n["fields"].get("id")
        if not i:
            continue
        by_id.setdefault(i, []).append(n["path"].name)
    for i, files in by_id.items():
        if len(files) > 1:
            problems.append(f"duplicate id {i!r} in: {', '.join(sorted(files))}")

    live = {n["fields"]["id"]: n for n in notes
            if n["fields"].get("id") and not n["fields"].get("superseded_by")}
    for n in notes:
        f = n["fields"]
        sb = f.get("superseded_by")
        if sb:
            if sb not in by_id:
                problems.append(f"{f.get('id')}: superseded_by {sb!r} does not exist")
            else:
                # Supersession must be reciprocal or the graph can claim a note is
                # replaced by something that never claimed to replace it.
                target = next((m for m in notes if m["fields"].get("id") == sb), None)
                if target and f.get("id") not in target["fields"].get("supersedes", []):
                    problems.append(
                        f"non-reciprocal supersession: {f.get('id')} says it is "
                        f"superseded by {sb}, but {sb} does not list it in supersedes")
        for s in f.get("supersedes", []):
            if s not in by_id:
                problems.append(f"{f.get('id')}: supersedes {s!r} which does not exist")

    # Cycle detection over superseded_by.
    for start in list(live) + [n["fields"].get("id") for n in notes]:
        seen, cur = set(), start
        while cur:
            if cur in seen:
                problems.append(f"supersession cycle involving {cur!r}")
                break
            seen.add(cur)
            nxt = next((m["fields"].get("superseded_by") for m in notes
                        if m["fields"].get("id") == cur), None)
            cur = nxt
    return sorted(set(problems))


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="validate a council brain vault")
    ap.add_argument("vault", help="directory of .md notes")
    ap.add_argument("--root", default=".",
                    help="repo root that file/command checks resolve against")
    ap.add_argument("--run-commands", action="store_true",
                    help="ENABLE command checks (executes note-authored argv in the "
                         "engine sandbox; OFF by default)")
    ap.add_argument("--budget-seconds", type=float, default=None,
                    help="optional TOTAL wall-clock cap across all checks. No "
                         "default: per-check time is already bounded by the engine "
                         "sandbox, and a total cap is a number only you can justify")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"ERROR: not a directory: {vault}", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    engine = _engine()

    notes = []
    for p in sorted(vault.rglob("*.md")):
        # Skip anything under a DOT-DIRECTORY. Opening a vault in Obsidian creates
        # `.obsidian/` inside it (measured 2026-07-26: app.json, appearance.json,
        # core-plugins.json, workspace.json), and a community plugin shipping a
        # README.md or docs there would otherwise be parsed as a NOTE, fail the schema,
        # and make the whole run exit non-zero. Also covers .git/, .trash/, .stfolder/.
        if any(part.startswith(".") for part in p.relative_to(vault).parts[:-1]):
            continue
        if p.name.startswith("_") or p.name == "README.md":
            continue
        fields, body, errs = parse_note(p)
        if not errs:
            errs = validate_fields(fields)
        notes.append({"path": p, "fields": fields, "body": body, "errors": errs})

    ledger_path = vault / ".attestations.json"
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, ValueError):
        ledger = {}

    started = time.monotonic()
    results, ran = [], 0
    for n in notes:
        f, rec = n["fields"], {"file": str(n["path"]), "id": f_id(n)}
        if n["errors"]:
            # An INVALID note must LOSE any attestation it had. Without this a note
            # that passed, was then edited into an invalid state, would keep a PASS
            # entry in the ledger -- a verification for a check that no longer
            # parses, which is the stale-attestation failure this ledger exists to
            # prevent.
            if f.get("id"):
                ledger.pop(f["id"], None)
            rec.update(status=INVALID, detail="; ".join(n["errors"]))
            results.append(rec)
            continue
        if f["type"] == "testimony":
            rec.update(status="TESTIMONY",
                       detail=f"{f['attributed_to']} on {f['date']} "
                              "(NOT citable as ground truth)")
            results.append(rec)
            continue
        if f.get("superseded_by"):
            rec.update(status=SUPERSEDED, detail=f"superseded by {f['superseded_by']}")
            results.append(rec)
            continue

        h = spec_hash(f)
        if args.budget_seconds is not None and (
                time.monotonic() - started) >= args.budget_seconds:
            # An unrun check must never read as verified.
            rec.update(status=NOT_RUN, detail="total budget exhausted", claim=rendered_claim(f))
            results.append(rec)
            continue

        status, detail = run_check(f, root, engine, args.run_commands)
        ran += 1
        rec.update(status=status, detail=detail, claim=rendered_claim(f))

        expires = f.get("expires")
        if expires and status == OK:
            try:
                if date.fromisoformat(expires) < date.today():
                    rec["status"] = NEEDS_ADJ
                    rec["detail"] = f"check passes but note expired on {expires}"
            except ValueError:
                rec["detail"] += f" (unparseable expires: {expires!r})"

        if status == OK:
            ledger[f["id"]] = {"spec_sha256": h, "last_pass": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "detail": detail}
        elif ledger.get(f["id"], {}).get("spec_sha256") != h:
            # The check changed since its last PASS, so the old attestation cannot
            # speak for it.
            ledger.pop(f["id"], None)
        results.append(rec)

    problems = integrity(notes)
    try:
        ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        problems.append(f"could not write attestation ledger: {e}")

    if args.json:
        print(json.dumps({"results": results, "integrity": problems,
                          "ran": ran, "elapsed_s": round(time.monotonic() - started, 2)},
                         indent=2))
    else:
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"vault: {vault}   notes: {len(notes)}   checks run: {ran}   "
              f"elapsed: {time.monotonic() - started:.1f}s")
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for r in results:
            if r["status"] != OK:
                print(f"\n[{r['status']}] {r['id']}  ({Path(r['file']).name})")
                print(f"    {r['detail']}")
        if problems:
            print("\nINTEGRITY PROBLEMS:")
            for p in problems:
                print(f"  - {p}")
        print("\nA failing check is NOT a false fact: the world may have changed, the "
              "check may have\nrotted, or it may be transient. NEEDS_ADJUDICATION is "
              "for a human to resolve.")
        if engine is None:
            print("NOTE: council engine not importable; url/command checks were NOT run.")

    bad = sum(1 for r in results if r["status"] in (INVALID,)) + len(problems)
    return 1 if bad else 0


def f_id(n):
    return n["fields"].get("id") or f"<no id: {n['path'].name}>"


if __name__ == "__main__":
    sys.exit(main())
