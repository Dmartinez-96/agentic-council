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
import os
import shlex
import sys
import time
from pathlib import Path

CHECK_KINDS = ("file", "url", "command")
LIST_FIELDS = ("supersedes", "tags")

# THERE IS NO `stream` FIELD, and the reason matters. The design review accepted
# "named stream + exit status" as a mitigation for checks that match error text
# instead of a real result. On implementation the first half turned out to be
# impossible: `run_exec_sandbox` MERGES stdout and stderr inside the sandbox
# (stderr=subprocess.STDOUT) and hands back ONE combined stream, so no caller can
# separate them -- this is a property of the capture, not of the return shape. A `stream`
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
    res = engine.run_exec_sandbox(shlex.join(argv), root)
    # ARITY-TOLERANT ON PURPOSE, not sloppiness. _engine() dynamically imports whichever
    # consult_council.py sits beside this file: an engine predating the structural-info
    # change returns (text, note); a current one returns (text, note, info). Indexing
    # reads both. A strict 3-unpack would raise ValueError on the older one, and this
    # function's contract is that it NEVER raises -- an exception here would look like a
    # missing result rather than a failed check.
    out, note = res[0], res[1]
    info = res[2] if len(res) > 2 else None
    if out is None:
        return NOT_RUN, f"sandbox refused: {note}"
    # TIMEOUT IS TESTED FIRST, ahead of `expect`. A wall-timeout SIGKILLs the process
    # group, so `out` holds only PARTIAL output and the exit status is -9 (the signal),
    # not the command's own. Neither is a verdict about the fact. This is therefore
    # NOT_RUN and deliberately NOT NEEDS_ADJUDICATION: the latter tells a human the fact
    # may have changed, when all that actually happened is the check ran out of wall
    # clock. Testing `expect` first would have reported "expect ABSENT" for output that
    # was merely cut off mid-run.
    if info is not None and info.get("timed_out"):
        return NOT_RUN, f"check did not complete: {note}"
    if expect not in out:
        return NEEDS_ADJ, f"expect ABSENT ({note})"
    want = f.get("exit_status")
    if want is None:
        return OK, note
    if info is None:
        # Engine too old to report a status structurally. FAIL CLOSED: an exit status was
        # REQUIRED by the schema and this cannot establish it, so it is not a PASS.
        return NEEDS_ADJ, f"exit status required but engine returned no info: {note!r}"
    got = info.get("exit_status")
    if got is None:
        return NEEDS_ADJ, f"exit status required but absent from engine info: {info!r}"
    if int(got) != int(want):
        return NEEDS_ADJ, f"exit status {got}, wanted {want}"
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

    # ENUMERATION AND ITS ERROR DETECTION ARE ONE TRAVERSAL, deliberately.
    # This used to be `vault.rglob("*.md")`, which SILENTLY OMITS the contents of a
    # directory it cannot read -- no exception, and no per-note parse error either,
    # because the note is never reached to be parsed (measured: a note under a
    # chmod-000 subdirectory simply vanished from the listing). Orphan pruning below
    # would then delete its still-valid attestation.
    # A SECOND, SEPARATE os.walk probe was the first fix and was not good enough: if a
    # directory's accessibility changed between the two scans, the probe could report
    # clean while the enumeration had already missed notes. Walking ONCE and recording
    # errors from that same pass removes the window entirely.
    # dirnames is pruned IN PLACE so os.walk never descends into a dot-directory.
    # Opening a vault in Obsidian creates `.obsidian/` inside it (measured 2026-07-26:
    # app.json, appearance.json, core-plugins.json, workspace.json), and a community
    # plugin shipping a README.md there would otherwise be parsed as a NOTE, fail the
    # schema, and make the whole run exit non-zero. Also covers .git/, .trash/,
    # .stfolder/ -- and, because they are never entered, an unreadable dot-directory
    # cannot raise an error that would needlessly disable pruning.
    walk_errors: list[str] = []
    md_paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
            vault, onerror=lambda e: walk_errors.append(
                f"{e.__class__.__name__}: {getattr(e, 'filename', '?')}")):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        md_paths.extend(Path(dirpath) / fn for fn in filenames if fn.endswith(".md"))

    notes = []
    for p in sorted(md_paths):
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
    # Ids CLAIMED by some note in this vault, for the end-of-run orphan prune below.
    # Collected from every note that yielded an id, INCLUDING schema-invalid ones: an
    # invalid note still OWNS its id (its own entry is popped separately just below),
    # and counting it stops a different note's entry from looking orphaned.
    claimed_ids: set[str] = set()
    # Notes whose FILE could not be read at all. parse_note reports those with an
    # "unreadable:" error and EMPTY fields, so they claim no id and are indistinguish-
    # able by id from a deliberately id-less note. Any one of them disables pruning for
    # this run. Keyed on the error TEXT, deliberately not on f_id()'s "<no id: ...>"
    # display string, which is presentation and must not become an interface.
    unreadable: list[str] = []

    def _now():
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _record_non_pass(note_id, spec, status_, detail_):
        """Record that the MOST RECENT run of this check did not pass.

        Keeps last_pass when the spec is unchanged -- the check really did pass then,
        and erasing that loses the only record of when. What changes is last_run and
        last_status, which is exactly what a consumer needs to tell "passed on its last
        run" from "passed at some point and has not been re-run since". A spec that no
        longer matches loses the attestation outright, as it always did.
        """
        prior = ledger.get(note_id)
        if not prior or prior.get("spec_sha256") != spec:
            ledger.pop(note_id, None)
            return
        entry = dict(prior)
        entry.update(last_run=_now(), last_status=status_, detail=detail_)
        ledger[note_id] = entry

    for n in notes:
        f, rec = n["fields"], {"file": str(n["path"]), "id": f_id(n)}
        if f.get("id"):
            claimed_ids.add(f["id"])
        if n["errors"]:
            # An INVALID note must LOSE any attestation it had. Without this a note
            # that passed, was then edited into an invalid state, would keep a PASS
            # entry in the ledger -- a verification for a check that no longer
            # parses, which is the stale-attestation failure this ledger exists to
            # prevent.
            if any(str(e).startswith("unreadable:") for e in n["errors"]):
                unreadable.append(n["path"].name)
            if f.get("id"):
                ledger.pop(f["id"], None)
            rec.update(status=INVALID, detail="; ".join(n["errors"]))
            results.append(rec)
            continue
        if f["type"] == "testimony":
            # A testimony note has NO check, so it must hold no attestation at all.
            # This branch used to return before the ledger was touched, so a note that
            # passed as `checkable` and was later rewritten as testimony kept its PASS
            # entry indefinitely (measured, not predicted).
            ledger.pop(f["id"], None)
            rec.update(status="TESTIMONY",
                       detail=f"{f['attributed_to']} on {f['date']} "
                              "(NOT citable as ground truth)")
            results.append(rec)
            continue
        if f.get("superseded_by"):
            # spec_hash covers neither `type` nor `superseded_by`, so a superseded
            # note's entry keeps MATCHING its own spec and would read as current to
            # any consumer testing the hash. Measured: it survived and read as
            # attested. Pop it here; a consumer should ALSO re-read superseded_by from
            # the note's frontmatter rather than trusting the ledger alone.
            ledger.pop(f["id"], None)
            rec.update(status=SUPERSEDED, detail=f"superseded by {f['superseded_by']}")
            results.append(rec)
            continue

        h = spec_hash(f)
        if args.budget_seconds is not None and (
                time.monotonic() - started) >= args.budget_seconds:
            # An unrun check must never read as verified. Recording the NOT_RUN is what
            # stops a prior PASS from standing in for a run that never happened.
            rec.update(status=NOT_RUN, detail="total budget exhausted", claim=rendered_claim(f))
            _record_non_pass(f["id"], h, NOT_RUN, "total budget exhausted")
            results.append(rec)
            continue

        status, detail = run_check(f, root, engine, args.run_commands)
        ran += 1
        rec.update(status=status, detail=detail, claim=rendered_claim(f))

        # THERE IS NO `expires` FIELD, and this is where it used to live. It was
        # REMOVED on the author's instruction -- nothing used it, and the only
        # behaviour it ever produced was a bug: it set rec["status"] to
        # NEEDS_ADJUDICATION while the ledger write was guarded on the untouched local
        # `status`, so an expired note was REPORTED as failing and ATTESTED as passing
        # in the same pass. A note carrying `expires:` today is simply an unrecognised
        # key, ignored like any other, exactly as the flat schema treats anything it
        # does not define.
        #
        # The ledger write is keyed on rec["status"] rather than the raw `status`. With
        # `expires` gone the two are always equal, so this is not currently load-
        # bearing -- it is kept because rec["status"] is what the validator REPORTS,
        # and the ledger agreeing with the report is the property that broke last time.
        if rec["status"] == OK:
            now = _now()
            ledger[f["id"]] = {"spec_sha256": h, "last_pass": now, "last_run": now,
                               "last_status": OK, "detail": detail}
        else:
            _record_non_pass(f["id"], h, rec["status"], rec["detail"])
        results.append(rec)

    # END-OF-RUN ORPHAN PRUNE. An id no note claims is unreachable from the vault and
    # can only ever do harm: a future note reusing that id with an identical check spec
    # would inherit a PASS recorded before it existed. Two ways one appears, both
    # measured -- a note's `id:` line is removed (nothing left to pop), or its id is
    # changed (the new id is written and the old one stays).
    # IT RUNS ONLY AFTER THE FULL ENUMERATION, AND ONLY IF THAT ENUMERATION WAS
    # COMPLETE -- the guard below is `unreadable or walk_errors`, covering BOTH ways a
    # note can fail to claim its id: a file that could not be READ (per-note
    # "unreadable:" parse error) and a file that was never ENUMERATED (a directory
    # os.walk could not descend into). Either one alone disables pruning, because such
    # a note claims no id and pruning would delete its still-valid attestation. Both
    # verified by chmod-000 fixtures. The ledger is written once, after this, so a
    # partial scan cannot half-apply a delete.
    # `walk_errors` comes from the SINGLE enumeration traversal above -- deliberately
    # not from a second walk here, which would reopen the window it exists to close.
    pruned: list[dict] = []
    prune_skipped = ""
    if unreadable or walk_errors:
        why = []
        if unreadable:
            why.append(f"{len(unreadable)} note(s) unreadable "
                       f"({', '.join(sorted(unreadable)[:5])})")
        if walk_errors:
            why.append(f"{len(walk_errors)} directory enumeration error(s) "
                       f"({'; '.join(sorted(walk_errors)[:3])})")
        prune_skipped = (
            "orphan pruning SKIPPED: " + "; ".join(why) + ". A note that is unreadable "
            "or was never enumerated claims no id, so pruning now could delete a "
            "still-valid attestation.")
    else:
        for stale in sorted(set(ledger) - claimed_ids):
            pruned.append({"id": stale,
                           "last_pass": ledger[stale].get("last_pass", "?"),
                           "detail": ledger[stale].get("detail", "")})
            ledger.pop(stale, None)

    problems = integrity(notes)
    # Every ledger mutation this run made -- writes, pops and prunes alike -- exists
    # only in memory until this succeeds, so the report below must not present prunes
    # as deletions that happened unless they did.
    # WRITE-THEN-REPLACE, not a direct write. Path.write_text opens with mode='w',
    # which TRUNCATES on open: an I/O failure partway through would leave the ledger
    # truncated or half-written, destroying every attestation in it rather than
    # preserving the previous state. Writing a temp file and renaming means the old
    # ledger survives intact on failure and the new one appears whole -- rename(2) is
    # atomic WITHIN A SINGLE FILESYSTEM, which holds here because the temp sits in the
    # same directory as its target. The temp is invisible to the note scanner because
    # that scanner keeps only names ending in .md and this one does not -- NOT because
    # it starts with a dot; the scanner's dot test prunes DIRECTORY names, never
    # filenames.
    # The pid in the name keeps concurrent validators from clobbering one another's
    # pending temp, and stops the failure path below from unlinking a temp belonging to
    # a different run. It does NOT make concurrent runs safe overall: two validators
    # racing on the same vault still resolve last-writer-wins on the ledger itself.
    # NOTHING RULES THAT OUT OF SCOPE -- it is an unaddressed limitation, not a
    # decided non-goal. An earlier version of this comment asserted otherwise.
    ledger_written = True
    tmp_path = ledger_path.with_name(f"{ledger_path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
        tmp_path.replace(ledger_path)
    except OSError as e:
        ledger_written = False
        problems.append(f"could not write attestation ledger: {e}")
        try:
            tmp_path.unlink()
        except OSError:
            pass

    if args.json:
        print(json.dumps({"results": results, "integrity": problems,
                          "pruned": pruned, "prune_skipped": prune_skipped,
                          "ledger_written": ledger_written,
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
        # A deletion is never silent: name what was evicted and when it last passed,
        # so a human can tell an intended rename from an accident.
        if pruned:
            verb = ("PRUNED" if ledger_written else
                    "WOULD HAVE PRUNED (ledger write FAILED -- nothing was removed)")
            print(f"\n{verb} {len(pruned)} orphan attestation(s) "
                  f"(no note claims these ids):")
            for e in pruned:
                print(f"  - {e['id']}  last_pass={e['last_pass']}")
        if prune_skipped:
            print(f"\n{prune_skipped}")
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
