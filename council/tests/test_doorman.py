#!/usr/bin/env python3
"""Tests for doorman.py -- the pre-landing advisory seat.

WHAT MATTERS MOST HERE, and why these are the tests rather than a broader sweep: the doorman
grants overrides that the council never hears about. The council is deliberately
never told what it said, so the audit log is the append-only evidence that the agent was
warned and proceeded. Every failure path does announce itself on stderr -- _log reports a
failed audit write, the override path flags an unrecorded override, tier0_gate prints
"doorman: not consulted" -- so none of this is silent; an earlier draft of this paragraph
said "silent and unreviewable" and that was written before those notices existed. But stderr
scrolls away and the log does not, which is why sections D, E and G exist: each was a way
for an override to happen without one.

  C  fail-closed: a store whose history cannot be trusted -- section C exercises invalid
     JSON, section B also covers a genuinely unreadable one -- must never grant a free
     override, not even when a valid prior objection was demonstrably in it beforehand
  D  the override is granted ONLY by a genuine identical re-submission
  E  a corrupt store entry must neither raise NOR buy an override. A raise would reach
     tier0_gate's except -- announced, but the check is still skipped for that submission.
     Worse, an earlier version coerced a malformed entry into a valid-looking one and
     granted the override, so a damaged store could MANUFACTURE overrides. A malformed
     entry is now ignored and the doorman is consulted afresh. The line is drawn at the
     OBJECTION TEXT, not at tidiness: an entry carrying a real objection whose `attempts`
     counter is unparseable still records a genuine prior and DOES override, while an entry
     with a valid-looking counter but no objection text (e1b) does NOT.
  F  response parsing: an objection must never be read as an OK
  G  an objection that cannot be persisted must NOT be issued, or the identical retry is
     denied again and the leader is wedged with no legal move
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import doorman as dm  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}   [{detail}]")


class FakeSeat:
    """Stands in for the model. Records what it was asked and replies as scripted."""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls = 0

    def __call__(self, role, models, prompt, cache_prefix=""):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"text": self.reply, "model_used": models[0], "returncode": 0}


def install_seat(reply, monkey_seat=True):
    """Point doorman at a scripted model and a configured seat."""
    import consult_council as cc
    fake = FakeSeat(reply)
    cc._openrouter_call_blocking = fake
    if monkey_seat:
        dm.seat = lambda: {"name": "doorman", "transport": "openrouter",
                           "model": "test/model"}
    return fake


def edit_payload(sess: str, path: str, old: str = "a", new: str = "b") -> dict:
    return {"tool_name": "Edit", "session_id": sess,
            "tool_input": {"file_path": path, "old_string": old, "new_string": new}}


def main() -> int:
    # Captured BEFORE anything is mutated -- STATE_ROOT especially, since the next line
    # overwrites it and a restore captured afterwards would restore the temp path.
    import consult_council as cc
    _real_openrouter = cc._openrouter_call_blocking
    _real_state_root = dm.STATE_ROOT
    real_seat = dm.seat

    tmp = Path(tempfile.mkdtemp(prefix="doorman-"))
    dm.STATE_ROOT = tmp / "state"

    print("\n-- A. identity is exact bytes --")
    p1 = {"file_path": "/f", "old_string": "a", "new_string": "b"}
    check("identical inputs give identical identity",
          dm.edit_identity("Edit", p1) == dm.edit_identity("Edit", dict(p1)))
    check("a one-space change is a DIFFERENT edit",
          dm.edit_identity("Edit", p1)
          != dm.edit_identity("Edit", {**p1, "new_string": "b "}))
    check("tool name participates",
          dm.edit_identity("Edit", p1) != dm.edit_identity("Write", p1))
    check("replace_all participates",
          dm.edit_identity("Edit", p1)
          != dm.edit_identity("Edit", {**p1, "replace_all": True}))
    check("content participates (Write path)",
          dm.edit_identity("Write", {"file_path": "/f", "content": "x"})
          != dm.edit_identity("Write", {"file_path": "/f", "content": "y"}))
    # FIELD-BOUNDARY COLLISION. With a bare NUL separator the encoding is not injective when
    # a field can itself contain NUL: these two DIFFERENT edits hashed identically, and
    # identity is what decides whether an override is granted. Length-prefixing fixed it.
    check("a NUL inside a field cannot forge another edit's identity",
          dm.edit_identity("Edit", {"file_path": "/f", "old_string": "a\x00",
                                    "new_string": "b"})
          != dm.edit_identity("Edit", {"file_path": "/f", "old_string": "a",
                                       "new_string": "\x00b"}))

    # NOT titled "atomically": success, a clean round-trip and the absence of a leftover
    # .tmp would ALL pass for a plain non-atomic write, so these assertions cannot support
    # that word. Atomicity rests on reading tmp+os.replace in save_objections; what is
    # tested here is the round-trip and that no temp residue is left behind.
    print("\n-- B. the objection store round-trips and leaves no residue --")
    check("save reports success", dm.save_objections("b1", {"k": {"objection": "o"}}))
    got, ok = dm.load_objections("b1")
    check("round-trips", ok and got == {"k": {"objection": "o"}}, str(got))
    check("no .tmp left behind",
          not dm._store_path("b1").with_suffix(".json.tmp").exists())
    dm._store_path("b2").parent.mkdir(parents=True, exist_ok=True)
    dm._store_path("b2").write_text("{ not json")
    got, ok = dm.load_objections("b2")
    check("readable-but-invalid JSON reports ok=False (never a silent empty)",
          got == {} and not ok)
    # A GENUINELY UNREADABLE STORE is a different failure from invalid JSON, and only the
    # latter was covered. A directory where the file belongs makes read_text raise OSError.
    dm._store_path("b3").parent.mkdir(parents=True, exist_ok=True)
    dm._store_path("b3").mkdir()
    got, ok = dm.load_objections("b3")
    check("an unreadable store also reports ok=False", got == {} and not ok, str(got))

    print("\n-- C. FAIL CLOSED: an invalid store grants no override --")
    dm._store_path("c1").parent.mkdir(parents=True, exist_ok=True)
    dm._store_path("c1").write_text("{ corrupt")
    fake = install_seat("OBJECTION: made up claim")
    deny, meta = dm.review(edit_payload("c1", "/x"))
    check("with an invalid-JSON store the edit is treated as a FIRST attempt",
          deny is not None and meta["status"] == "objection", str(meta))
    check("the model was actually consulted", fake.calls == 1)

    # THE DISCRIMINATING CASE, and the check above does NOT cover it: an empty store also
    # yields an objection, so that assertion passes whether or not corruption is handled
    # fail-closed. This one can only pass if it is. A prior objection genuinely EXISTS and
    # would grant an override -- then the file is corrupted. Fail-open would read "no record
    # I can trust, wave it through"; fail-closed must still refuse the override.
    ident2 = dm.edit_identity("Edit", {"file_path": "/x2", "old_string": "a",
                                       "new_string": "b"})
    dm.save_objections("c2", {ident2: {"objection": "prior", "attempts": 1}})
    got, ok = dm.load_objections("c2")
    check("precondition: that prior WOULD grant an override", ok and ident2 in got)
    dm._store_path("c2").write_text("{ corrupt")
    fake2 = install_seat("OBJECTION: still objectionable")
    deny2, meta2 = dm.review(edit_payload("c2", "/x2"))
    check("a corrupted store does NOT convert a prior objection into a free override",
          deny2 is not None and meta2["status"] != "override", str(meta2))
    check("and the model was re-consulted rather than trusted from a lost record",
          fake2.calls == 1)

    print("\n-- D. the override needs a genuine identical re-submission --")
    fake = install_seat("OBJECTION: unsupported causal claim")
    deny1, m1 = dm.review(edit_payload("d1", "/y"))
    check("first submission is turned back", deny1 is not None and m1["status"] == "objection")
    # KEYED BY THE RIGHT IDENTITY, not merely "a file exists". The earlier version reused a
    # stale `ident` from section C and asserted only that the store file was present, which
    # would pass even if the objection had been filed under the wrong key -- the one thing
    # that would break the override.
    ident_y = dm.edit_identity("Edit", {"file_path": "/y", "old_string": "a",
                                        "new_string": "b"})
    stored, ok_d = dm.load_objections("d1")
    check("the objection is stored under THIS edit's identity",
          ok_d and ident_y in stored, f"keys={list(stored)}")
    check("and carries the objection text",
          stored.get(ident_y, {}).get("objection", "").startswith("unsupported"),
          str(stored.get(ident_y)))
    calls_after_first = fake.calls
    deny2, m2 = dm.review(edit_payload("d1", "/y"))
    check("the identical re-submission is allowed", deny2 is None, str(m2))
    check("and is recorded as an override", m2["status"] == "override", str(m2))
    # Measured as a DELTA. A cumulative `calls == 1` would also hold if the first submission
    # had somehow made none and the retry made one.
    check("the model was NOT consulted again", fake.calls == calls_after_first,
          f"before={calls_after_first} after={fake.calls}")
    recs = [json.loads(ln) for ln in
            (dm.STATE_ROOT / "d1" / "doorman-audit.jsonl").read_text().splitlines() if ln]
    ovr = [r for r in recs if r.get("event") == "override"]
    # ASSERT THE VALUES, not the presence of key names -- `'"proposed"' in text` would pass
    # for a null or wrong value, which is the whole thing this record exists to preserve.
    check("an override record exists", len(ovr) == 1, f"{len(ovr)} override records")
    check("the audit record reconstructs the edit, not just its hash",
          ovr and ovr[0].get("proposed") == "b" and ovr[0].get("old_string") == "a"
          and ovr[0].get("file") == "/y", str(ovr[:1]))
    deny3, m3 = dm.review(edit_payload("d1", "/y", new="DIFFERENT"))
    check("a DIFFERENT edit does not inherit the override",
          deny3 is not None, str(m3))

    print("\n-- E. a corrupt store ENTRY must not raise --")
    dm._store_path("e1").parent.mkdir(parents=True, exist_ok=True)
    key = dm.edit_identity("Edit", {"file_path": "/z", "old_string": "a",
                                    "new_string": "b"})
    dm._store_path("e1").write_text(json.dumps({key: "a bare string, not a dict"}))
    # The seat OBJECTS here on purpose. With "OK" the edit would be allowed either way, so
    # the assertion could not tell "ignored the corrupt entry and re-checked" from
    # "honoured it as an override" -- both end in deny=None. An objection discriminates.
    fake_e = install_seat("OBJECTION: fresh look")
    try:
        deny, meta = dm.review(edit_payload("e1", "/z"))
        raised = False
    except Exception as e:  # noqa: BLE001
        raised, deny, meta = True, None, {"err": repr(e)}
    check("non-dict entry does not raise", not raised, str(meta))
    check("a MALFORMED entry does NOT buy an override -- corrupt history must not "
          "manufacture one", deny is not None and meta.get("status") == "objection",
          str(meta))
    check("and the doorman was consulted afresh", fake_e.calls == 1, f"{fake_e.calls}")

    # A DICT CARRYING NO OBJECTION. This is the case the guard regressed to accepting when
    # it tested only isinstance(prior, dict): {"attempts": 1} is a perfectly good dict and
    # bought an override on the strength of being one. Without this check, that regression
    # would pass every other assertion in this section.
    dm._store_path("e1b").parent.mkdir(parents=True, exist_ok=True)
    dm._store_path("e1b").write_text(json.dumps({key: {"attempts": 1}}))
    fake_e1b = install_seat("OBJECTION: fresh look")
    deny, meta = dm.review(edit_payload("e1b", "/z"))
    check("a dict with NO objection text does not buy an override",
          deny is not None and meta.get("status") == "objection", str(meta))
    check("and the doorman was consulted afresh", fake_e1b.calls == 1, f"{fake_e1b.calls}")
    # An empty/whitespace objection is the same class and must also be refused.
    dm._store_path("e1c").parent.mkdir(parents=True, exist_ok=True)
    dm._store_path("e1c").write_text(json.dumps({key: {"objection": "   ",
                                                      "attempts": 1}}))
    install_seat("OBJECTION: fresh look")
    deny, meta = dm.review(edit_payload("e1c", "/z"))
    check("a blank objection string does not buy an override",
          deny is not None and meta.get("status") == "objection", str(meta))

    dm._store_path("e2").parent.mkdir(parents=True, exist_ok=True)
    dm._store_path("e2").write_text(json.dumps({key: {"objection": "o",
                                                     "attempts": "garbage"}}))
    fake_e2 = install_seat("OBJECTION: should not be asked")
    try:
        deny, meta = dm.review(edit_payload("e2", "/z"))
        raised = False
    except Exception as e:  # noqa: BLE001
        raised, deny, meta = True, None, {"err": repr(e)}
    check("non-numeric attempts does not raise", not raised, str(meta))
    # A WELL-FORMED entry with a junk counter is still a genuine prior objection, so it DOES
    # earn the override -- and asserting the status (not merely "no exception") is what
    # separates "handled correctly" from "swallowed the error and did something arbitrary".
    check("a well-formed prior with a junk counter still overrides",
          deny is None and meta.get("status") == "override", str(meta))
    check("attempts falls back to a sane number", meta.get("attempts") == 2,
          str(meta.get("attempts")))
    check("and the model was NOT consulted for an override", fake_e2.calls == 0,
          f"{fake_e2.calls}")

    print("\n-- F. response parsing never reads an objection as an OK --")
    cfg = {"name": "d", "model": "m"}
    for reply, want in (("OK", "ok"),
                        ("OK.", "ok"),
                        ("OBJECTION: x", "objection"),
                        ("OKAY, but OBJECTION: this is unsupported", "objection"),
                        ("I think maybe it's fine?", "malformed"),
                        ("OBJECTION:", "malformed")):
        install_seat(reply)
        _, status = dm.ask(cfg, "pitch")
        check(f"{reply[:34]!r} -> {want}", status == want, f"got {status}")
    install_seat(RuntimeError("network down"))
    _, status = dm.ask(cfg, "pitch")
    check("an exception is 'unreachable', distinct from malformed", status == "unreachable")
    install_seat("")
    _, status = dm.ask(cfg, "pitch")
    check("an empty reply is 'unreachable'", status == "unreachable")

    print("\n-- G. an objection that cannot be recorded is NOT issued --")
    install_seat("OBJECTION: something")
    saved = dm.save_objections
    dm.save_objections = lambda *a, **k: False          # simulate a failed write
    try:
        deny, meta = dm.review(edit_payload("g1", "/w"))
    finally:
        dm.save_objections = saved
    check("the edit is allowed through rather than wedged",
          deny is None, str(meta))
    check("and the reason is recorded as unrecorded-objection",
          meta.get("status") == "objection_unrecorded", str(meta))

    print("\n-- H. degradation paths --")
    install_seat(RuntimeError("boom"))
    deny, meta = dm.review(edit_payload("h1", "/v"))
    check("unreachable model lets the edit land", deny is None
          and meta["status"] == "unreachable", str(meta))
    install_seat("some nonsense reply")
    deny, meta = dm.review(edit_payload("h2", "/v"))
    check("malformed reply lets the edit land", deny is None
          and meta["status"] == "malformed", str(meta))
    dm.seat = lambda: None
    deny, meta = dm.review(edit_payload("h3", "/v"))
    check("no configured seat means the doorman is silently absent",
          deny is None and meta["status"] == "skipped", str(meta))
    dm.seat = real_seat

    print("\n-- I. review() tolerates junk payloads --")
    install_seat("OK")
    # review() promises it never raises, so the junk has to include the shapes that actually
    # reached a .get on a non-dict: a TRUTHY non-dict tool_input (which `or {}` waves
    # through), and a payload that is not a mapping at all.
    for bad in ({}, {"tool_name": "Edit"}, {"tool_input": None},
                {"tool_name": None, "tool_input": {}},
                {"tool_name": "Edit", "tool_input": "not a dict"},
                {"tool_name": "Edit", "tool_input": ["also", "not"]},
                {"tool_name": "Edit", "tool_input": 7},
                ["not", "a", "payload"], "a bare string", None):
        try:
            dm.review(bad)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            print("       raised:", repr(e))
        check(f"payload {str(bad)[:34]} handled", ok)

    # ---- P2. OUTPUT ELISION KEEPS BOTH ENDS AND STAYS INSIDE ITS BUDGET ----
    # Standing rule 8: charge the marker against the budget. Slicing head+tail to the full
    # cap and THEN adding the marker overshoots by the marker's length, and just past the cap
    # it returns MORE than it was given. Both directions are asserted, and the boundary region
    # is swept rather than sampled -- an off-by-the-marker bug lives exactly there.
    print("\n-- P2. _elide_middle: both ends survive, budget is never exceeded --")
    over = [n for n in range(dm._OUT_BUDGET - 10, dm._OUT_BUDGET + 200)
            if len(dm._elide_middle("x" * n)) > dm._OUT_BUDGET]
    check("never exceeds _OUT_BUDGET across the boundary", over == [],
          f"lengths that overshot: {over[:5]}")
    grew = [n for n in range(dm._OUT_BUDGET - 10, dm._OUT_BUDGET + 200)
            if len(dm._elide_middle("x" * n)) > n]
    check("never returns MORE than it was given", grew == [],
          f"lengths that grew: {grew[:5]}")
    _big = "HEAD" + ("m" * 3000) + "TAIL"
    _r = dm._elide_middle(_big)
    check("the HEAD survives elision", _r.startswith("HEAD"))
    check("the TAIL survives elision", _r.endswith("TAIL"),
          "a head-only slice would drop the answer to a `grep -A` probe")
    check("short text is returned untouched",
          dm._elide_middle("short") == "short")

    # ---- Q. TRANSPORT DISPATCH ----
    # Every check above this point ran against a seat stubbed as transport="openrouter",
    # because until the subscription route existed that was the only route there was. The
    # word "transport" appeared NOWHERE in doorman.py: the roster key was decorative and
    # every call went to OpenRouter regardless of what the seat said. These checks cover the
    # dispatch that now reads it -- without them the CLI branch ships untested.
    print("\n-- Q. transport dispatch: the roster key that used to be decorative --")
    import subprocess as _rsp

    class FakeProc:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    class FakeSubprocess:
        """Stands in for the `subprocess` MODULE as doorman sees it.

        Stubbed at the subprocess boundary and NOT at _ask_claude_cli, deliberately: the
        thing under test is the argv construction, the env scrub and the dispatch in ask(),
        and stubbing the helper that contains them would only prove a stub was callable.
        SubprocessError/TimeoutExpired are re-exported because _ask_claude_cli's except
        clause resolves them through this same module reference.
        """

        SubprocessError = _rsp.SubprocessError
        TimeoutExpired = _rsp.TimeoutExpired

        def __init__(self, rc=0, out="OK", raises=None):
            self.rc, self.out, self.raises = rc, out, raises
            self.calls = []

        def run(self, argv, **kw):
            self.calls.append({"argv": argv, "kw": kw})
            if self.raises:
                raise self.raises
            return FakeProc(self.rc, self.out)

    CLI_SEAT = {"name": "doorman", "transport": "claude_subprocess",
                "model": "claude-sonnet-5", "fallback_model": "or/sonnet"}

    # CAPTURED IMMEDIATELY BEFORE THE FIRST MUTATION, not in the block at the top of main():
    # nothing above section Q touches dm.subprocess, so this is the earliest point where the
    # captured value is guaranteed to be the real module, and keeping the capture next to the
    # mutation is what stops the two from drifting apart.
    _real_subprocess = dm.subprocess

    # SEED SENTINELS SO THE SCRUB CHECK CAN ACTUALLY FAIL. Without this the assertion below
    # passes whether _ask_claude_cli pops these keys or not, because a test runner's
    # environment normally has neither -- `env = dict(os.environ)` then simply never contains
    # them and a DELETED pop loop looks identical to a working one. The scrub is the
    # load-bearing claim of the whole subscription route (an inherited key bills the API and
    # defeats the reason for the route existing), so its only test must be discriminating.
    _seeded = {"ANTHROPIC_API_KEY": "sentinel-key-must-not-reach-the-child",
               "ANTHROPIC_AUTH_TOKEN": "sentinel-token-must-not-reach-the-child"}
    _prior_env = {k: os.environ.get(k) for k in _seeded}
    os.environ.update(_seeded)

    orf = install_seat("OK", monkey_seat=False)
    fs = FakeSubprocess(0, "OK")
    dm.subprocess = fs
    obj, status = dm.ask(CLI_SEAT, "a pitch")
    check("claude_subprocess route returns the model's verdict",
          status == "ok" and obj is None, f"status={status!r}")
    check("and OpenRouter is NOT touched on the happy path -- the billing claim",
          orf.calls == 0, f"openrouter calls={orf.calls}")
    # GUARDED. Every check below reads fs.calls[0]; if the dispatch never reached run() an
    # unguarded index would crash the suite with an IndexError instead of reporting which
    # check failed, and this suite's whole pattern is that a failure is REPORTED.
    _call = fs.calls[0] if fs.calls else {"argv": [], "kw": {}}
    check("the CLI was actually invoked", bool(fs.calls),
          "everything below reads this call")
    argv = _call["argv"]
    check("argv carries the seat's CLI slug after --model",
          "--model" in argv and argv[argv.index("--model") + 1] == "claude-sonnet-5",
          f"argv={argv}")
    check("the seat is given no tools",
          "--tools" in argv and argv[argv.index("--tools") + 1] == "")
    check("the prompt goes on STDIN, never argv",
          _call["kw"].get("input", "").endswith("a pitch")
          and not any("a pitch" in str(x) for x in argv))
    check("the call is BOUNDED by the doorman's own timeout",
          _call["kw"].get("timeout") == dm.DOORMAN_TIMEOUT_S)
    _env = _call["kw"].get("env") or {}
    check("the seeded sentinels really were in the parent env (precondition)",
          all(os.environ.get(k) == v for k, v in _seeded.items()),
          "if this fails the scrub check below proves nothing")
    check("CLAUDE_DROP_ENV names are scrubbed from the child env",
          all(k not in _env for k in cc.CLAUDE_DROP_ENV),
          f"scrubbed={list(cc.CLAUDE_DROP_ENV)}")
    check("and the child env is otherwise populated, so absence means REMOVED not empty",
          len(_env) > 1, f"child env has {len(_env)} keys")
    for _k, _v in _prior_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

    orf2 = install_seat("OBJECTION: raised by the fallback", monkey_seat=False)
    dm.subprocess = FakeSubprocess(1, "")
    _, status3 = dm.ask(CLI_SEAT, "a pitch")
    check("a non-zero CLI rc FALLS BACK to OpenRouter rather than dropping the check",
          status3 == "objection" and orf2.calls == 1, f"status={status3!r}")

    orf3 = install_seat("OK", monkey_seat=False)
    dm.subprocess = FakeSubprocess(1, "")
    check("a failing CLI with NO fallback is 'unreachable', never a silent 'ok'",
          dm.ask(dict(CLI_SEAT, fallback_model=""), "p")[1] == "unreachable"
          and orf3.calls == 0)

    dm.subprocess = FakeSubprocess(raises=_rsp.TimeoutExpired("claude", 60))
    check("a CLI timeout degrades to unreachable instead of raising through the hook",
          dm.ask(dict(CLI_SEAT, fallback_model=""), "p")[1] == "unreachable")

    orf5 = install_seat("OK", monkey_seat=False)
    dm.subprocess = FakeSubprocess(0, "OK")
    check("an unknown transport is unreachable and bills NOTHING",
          dm.ask(dict(CLI_SEAT, transport="telepathy"), "p")[1] == "unreachable"
          and orf5.calls == 0, "must not silently become OpenRouter")

    orf6 = install_seat("OK", monkey_seat=False)
    dm.subprocess = FakeSubprocess(0, "SHOULD NOT BE REACHED")
    check("BACK-COMPAT: a seat with no transport key still goes to OpenRouter",
          dm.ask({"name": "d", "model": "test/model"}, "p")[1] == "ok" and orf6.calls == 1)

    # RESTORE THE GLOBALS THIS SUITE MUTATED. Not a live hazard under run_tests.py, which
    # spawns each suite as its own process (run_tests.py:183) -- but it is one for anyone
    # importing this module or chaining suites in-process, and a stubbed
    # _openrouter_call_blocking left on a real module is a booby trap for whatever runs next.
    # Placed here rather than in a finally deliberately: if a check raises, the traceback and
    # a poisoned module are both wanted, since the run is already invalid and hiding the
    # mutation would make the failure harder to read.
    import consult_council as cc
    cc._openrouter_call_blocking = _real_openrouter
    dm.seat = real_seat
    dm.STATE_ROOT = _real_state_root
    # dm.subprocess is restored for the same reason as the three above, and it is the one
    # whose absence would be least visible: a doorman left holding a FakeSubprocess would
    # not error for an in-process caller, it would silently return the fake's scripted
    # verdict forever.
    dm.subprocess = _real_subprocess

    total = PASS + FAIL
    print(f"\n{PASS}/{total} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
