#!/usr/bin/env python3
"""The doorman: a single cheap model that reviews an edit BEFORE it lands.

WHERE IT SITS. Three things stand in front of an edit:
    the gate      deterministic, no model, BLOCKING and not overridable (tier0_gate.py)
    the doorman   one cheap model, advisory, runs only if the gate passed  (this file)
    the council   twelve seats, reviews edits that actually LANDED

WHY BEFORE, NOT AFTER. An edit the doorman turns back never lands, so there is nothing to
review and no council turn is spent on a defect that was already knowable. This is the whole
economy of the thing: the saving comes from edits not landing, NOT from reviews being
skipped. Every edit that lands is still SENT to the council, and that property is what makes
the doorman safe to add; it must not be traded away for speed.
AN EARLIER DRAFT SAID "nothing that reaches disk goes unreviewed", AND THAT IS FALSE HERE.
Roughly a third of this project's council reviews are killed mid-flight, so edits do reach
disk unreviewed. RE-RUN IT RATHER THAN TAKING THIS LINE'S WORD -- `python3
_nogit/loss_analysis.py` prints the per-session loss rate and the DEATH TIMES table it is
drawn from; the figure moves as the corpus grows. That measurement is why the pending-review
markers exist at all. The doorman
adds no new way for that to happen and removes some load from the bench, but it cannot
promise a property the review layer underneath it does not currently have.

ADVISORY MEANS THE LEADER CAN OVERRULE IT, and the mechanism is deliberately plain: submit
the identical edit again. The first submission draws an objection and is turned back; an
identical re-submission is read as "I have considered this and disagree", lands, and is
SENT to the full council. Disagreement is therefore put to twelve seats rather than decided
by one cheap model or by the agent alone -- SENT, not guaranteed reviewed, because roughly a
third of this install's council fires die mid-flight (see `python3 _nogit/loss_analysis.py`).
Saying "reviewed by the council" would assert a delivery this system does not currently
have.

THE COUNCIL IS NOT TOLD WHAT THE DOORMAN SAID. Operator ruling, and the reasoning is sound:
a deliberately weaker advisory seat should not frame how twelve stronger reviewers see a
problem. The cost is that an override is invisible to the bench, which makes the audit log
below an APPEND-ONLY record that the agent was warned and proceeded. THREE EARLIER DRAFTS
OVERCLAIMED THIS AND EACH RETRACTION INTRODUCED THE NEXT, so read the limits rather than the
adjective: it is not the only DURABLE record (save_objections persists the objection,
`attempts` and `overridden_at`); it is not COMPLETE (`old_string` and `proposed` are each
truncated to 2000
characters); and it is not the only place a repeat override is countable, since `attempts`
increments in the store. What this log adds is an entry PER EVENT with its own timestamp,
where the store keeps one mutable row per identity. Whether any OTHER channel in this
install also records overrides has not been audited. That log is
load-bearing for oversight, not diagnostics.

IDENTITY IS A LENGTH-PREFIXED DIGEST of six fields -- tool_name, file_path, old_string,
new_string, content and replace_all. Each is coerced with `str(value or "")` (replace_all
with `str(bool(value))`), encoded `utf-8` with errors="replace", and written as
`<byte-length>:<bytes>`. So identity is exact over those ENCODED BYTES -- not over raw input
bytes, as one draft claimed, and not quite over the coerced strings either, since the
"replace" encode maps unpaired surrogates onto a shared replacement.
WAYS DISTINCT EDITS CAN SHARE AN IDENTITY -- recorded rather than hidden, because this
contract decides overrides, and NOT offered as a closed list: an earlier draft said "TWO
WAYS", which read as exhaustive and undercounted. The `or ""` collapses every falsy value
(None, "", 0, False, []) to the empty string; `str()` merges values that differ only by type,
so 1 and "1" coincide; `str(bool(...))` merges every truthy replace_all; and the lossy encode
collapses differing lone surrogates. All are properties of the ENCODING rather than of any
payload. Whether the
harness can actually produce such payloads is NOT established here -- no probe of live
tool_input types was run -- so this is stated as a property of the encoding, not dismissed
as unreachable. Length prefixes rather than a bare separator, because NUL-delimiting fields that may
themselves contain NUL is not injective: old="a\0"/new="b" and old="a"/new="\0b" collided,
and identity is what decides whether an override is granted.
Normalising whitespace was considered and rejected: normalisation is itself a gaming surface,
and a one-character change genuinely IS a different edit that deserves a fresh look.

THE FILE'S CONTENT IS DELIBERATELY *NOT* PART OF IDENTITY, and the tolerated vector is named
rather than hidden. Including a content hash would mean any unrelated edit to the file
invalidates a pending objection -- and since an agent edits the same file repeatedly,
objections would evaporate constantly, which is a far larger hole than the one it closes.
The vector left open: the agent could deliberately touch the file to... nothing, actually,
since the hash is not consulted. The real tolerated vector is the reverse -- an objection
stays valid even if the surrounding file changed underneath it, so the doorman may be
answering about a context that has moved. That is the lesser error, and it is logged.

THE COUNTER FAILS CLOSED, and the failure is narrower than an earlier draft said. It claimed
a missing or unreadable store gets the edit "turned back", which is wrong twice: a MISSING
store is not a failure at all (it loads as empty history with ok=True and never reaches this
branch), and an UNREADABLE one does not turn anything back by itself -- it resets history to
empty and the edit proceeds into a normal doorman consult, so it is turned back only if the
model objects. What fails closed is exactly one thing: NO OVERRIDE IS EVER GRANTED ON HISTORY
THAT COULD NOT BE READ. The asymmetry decides it: a false
deny costs one extra doorman cycle, while a false free-override lets an edit skip the
doorman entirely -- losing one cheap early check and, worse, the audit record that a
disagreement happened at all. (It does NOT put an unreviewed edit on disk, which an earlier
draft claimed as the justification: the edit still lands and is still sent to the council.
The asymmetry is real but smaller than that.)

UNREACHABLE AND MALFORMED ARE DIFFERENT FAILURES, and conflating them was flagged in design.
  UNREACHABLE (network, no key, timeout) -> fall through: the edit lands and the council
    reviews it exactly as it did before the doorman existed. Degrades to the old system.
  MALFORMED (a reply that is neither OK nor a parseable objection) -> also falls through, but
    is reported separately and the raw reply is logged, because a model that has started
    answering in the wrong shape is a different problem from one that never answered.
Neither ever blocks work, and neither is silent.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path(os.environ.get("COUNCIL_STATE_ROOT", Path.home() / ".claude" / "state"))
ROSTER_PATH = Path(os.environ.get("COUNCIL_ROSTER_PATH",
                                  Path(__file__).resolve().parent / "roster.json"))
EVIDENCE_TAIL = 12          # recent tool results the doorman may reason from
MAX_PITCH_CHARS = 12_000

# THE DOORMAN'S OWN WALL CLOCK, and it is deliberately NOT the council's.
# consult_council's PER_CRITIC_TIMEOUT_S is 600s, which suits a critic the operator is
# willing to wait ten minutes for. The doorman is not that: it runs inside PreToolUse, in
# front of EVERY edit, and the harness gives that hook 120s -- so a 600s bound is not a
# bound at all here, it just means the HARNESS decides how the doorman dies instead of the
# doorman. MEASURED before choosing: six trials on a real 2032-char pitch ran 3.4-4.9s
# (claude CLI) and 3.7-3.9s (OpenRouter). 60s is roughly twelve times the slowest of those.
# WHAT THAT DOES NOT ESTABLISH: that only a stuck process can trip it. Six trials at ONE
# pitch size bound no distribution -- MAX_PITCH_CHARS is 12_000, six times the probed pitch,
# and cold starts and provider queueing were not measured at all. A healthy-but-slow call
# CAN exceed 60s. When it does, the doorman degrades to "unreachable" and the edit proceeds,
# which is the direction every other doorman failure already takes.
# THIS BOUNDS THE SUBPROCESS ROUTE ONLY. The OpenRouter route goes through
# consult_council._openrouter_call_blocking, whose urlopen timeout this module does not
# own and does not override.
DOORMAN_TIMEOUT_S = 60

SYSTEM = """You are the DOORMAN: a fast, single-model check that runs BEFORE an edit lands.

You are NOT the council. You do not judge style, design, or whether the change is a good
idea. You look for four specific defects, all of which are about a claim outrunning its
evidence. Everything else is out of scope and must be allowed through.

  1. MECHANISM WITHOUT EVIDENCE. The text asserts a causal mechanism ("X because Y", "this
     fixes Z", "the cause was W"). Does any surfaced command output actually demonstrate the
     TRANSITION, or only an end state? Flag a causal claim that no observation supports.

  2. NON-DISCRIMINATING PROBE. The text cites a command or output as evidence for a claim.
     Ask: what would that command have printed if the claim were FALSE? If the answer is
     "the same thing", the probe proves nothing and the citation is empty.

  3. CLAIM WRITTEN FROM RECALL. A count, tally, enumeration, date, line number or "N of M"
     stated as fact with no measurement visible in the session. Precise numbers are the
     usual tell. "Several" is fine; "six of six" needs a source.

  4. PROSE DESCRIBING CODE THAT IS NOT IN THIS EDIT. A comment or docstring asserting
     behaviour the hunk does not implement, or describing a function, parameter or file that
     this edit does not create. The comment must be true of the code as it will stand AFTER
     this edit alone.

REPLY IN EXACTLY ONE OF TWO FORMS AND NOTHING ELSE:

OK

or

OBJECTION: <one short paragraph naming which of the four, quoting the exact offending text,
and saying what would resolve it>

Be sparing. You are in the path of every edit, and a false objection costs the author a
round-trip. If it is arguable, reply OK and let the council decide."""


def _log(session_id: str, record: dict) -> bool:
    """Append one audit record; returns False if it could not be written.

    THIS LOG IS NOT DIAGNOSTICS. Because the council is deliberately never told what the
    doorman said, this file is an APPEND-ONLY record that the agent was warned and
    proceeded anyway. A silently dropped write therefore erases this per-event record of an
    override -- so a failure here is announced, and the caller escalates for the records
    that matter. It is also fsync'd: an override notice lost to a page cache when the
    process dies would defeat the same purpose."""
    try:
        d = STATE_ROOT / (session_id or "_no_session")
        d.mkdir(parents=True, exist_ok=True)
        record["t"] = datetime.now(timezone.utc).isoformat()
        with open(d / "doorman-audit.jsonl", "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError as e:
        print(f"doorman: AUDIT WRITE FAILED ({e}); this event is unrecorded: "
              f"{str(record.get('event'))}", file=sys.stderr)
        return False


def seat() -> dict | None:
    """The doorman's roster entry, or None if this install has not configured one.

    A per-leader-profile default: Sonnet under a claude-led roster, Terra under codex-led,
    and in the general build whatever the operator picks -- it need not share the leader's
    family. It is excluded from leader_family_overlap for a reason that has nothing to do
    with family: the doorman DOES NOT VOTE, and overlap matters because a leader sharing a
    family with VOTING members produces correlated judgement on the verdict."""
    try:
        data = json.loads(ROSTER_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None          # valid JSON of the wrong shape must not raise on .get
    d = data.get("doorman")
    return d if isinstance(d, dict) and d.get("model") else None


def edit_identity(tool_name: str, tool_input: dict) -> str:
    """Identity over the LENGTH-PREFIXED ENCODING of six coerced fields -- not "exact bytes",
    which this docstring claimed while the module docstring said otherwise. See the module
    docstring for the exact encoding, why nothing is normalised, and why the FILE's content
    is excluded (distinct from the `content` FIELD, which is included)."""
    h = hashlib.sha256()
    # EVERY field is coerced, tool_name included. It was the one left raw, so a payload with
    # a null tool_name raised AttributeError here. tier0_gate catches anything out of
    # review() and ANNOUNCES it ("doorman: not consulted"), so the bypass would not have been
    # silent -- but it would still have skipped the check on every such payload, and a
    # coercion is cheaper than relying on an outer handler to report the damage.
    #
    # LENGTH-PREFIXED, NOT NUL-DELIMITED. A bare NUL separator does not make the encoding
    # injective when the fields themselves may contain NUL: old="a\0" with new="b" produces
    # the same byte stream as old="a" with new="\0b", so two DIFFERENT edits shared one
    # identity -- and identity is what decides whether an override is granted. Prefixing each
    # field with its byte length makes the encoding unambiguous regardless of content.
    for part in (str(tool_name or ""),
                 str(tool_input.get("file_path") or ""),
                 str(tool_input.get("old_string") or ""),
                 str(tool_input.get("new_string") or ""),
                 str(tool_input.get("content") or ""),
                 str(bool(tool_input.get("replace_all")))):
        raw = part.encode("utf-8", "replace")
        h.update(str(len(raw)).encode("ascii"))
        h.update(b":")
        h.update(raw)
    return h.hexdigest()


def _store_path(session_id: str) -> Path:
    return STATE_ROOT / (session_id or "_no_session") / "doorman-objections.json"


def load_objections(session_id: str) -> tuple[dict, bool]:
    """Returns (store, ok). ok=False means it exists but could not be read.

    THE CALLER MUST FAIL CLOSED ON ok=False. An unreadable store means prior objections are
    unknown, and treating unknown as "no objection recorded" would turn every lost store into
    a free, silent override."""
    p = _store_path(session_id)
    if not p.exists():
        return {}, True
    try:
        data = json.loads(p.read_text())
        return (data, True) if isinstance(data, dict) else ({}, False)
    except (OSError, ValueError):
        return {}, False


def save_objections(session_id: str, store: dict) -> bool:
    """Returns True if the store was persisted.

    THE RETURN VALUE IS LOAD-BEARING AND THE FIRST VERSION DISCARDED IT. The override works
    by recognising a re-submitted edit, which requires the objection to still be on disk. If
    persistence fails and the objection is issued anyway, the identical re-submission finds
    no record, is objected to again, and the leader is WEDGED -- denied in a loop with no
    legal move, which is precisely what an advisory check must never do. The caller must not
    deny on an objection it could not record."""
    try:
        p = _store_path(session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=2))
        os.replace(tmp, p)
        return True
    except OSError as e:
        print(f"doorman: could not record the objection ({e}).", file=sys.stderr)
        return False


def recent_evidence(session_id: str, limit: int = EVIDENCE_TAIL) -> str:
    """The tail of this session's tool log. Checks 1 and 2 are ABOUT whether a claim is
    supported by observation, so the doorman cannot perform them without seeing what was
    actually observed."""
    p = STATE_ROOT / (session_id or "_no_session") / "evidence.jsonl"
    try:
        lines = p.read_text(errors="replace").splitlines()[-limit:]
    except OSError:
        return ""
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue         # a JSON line of the wrong shape must not raise on .get
        out.append("- " + _render_evidence(rec))
    return "\n".join(out)


# WHAT IDENTIFIES A RECORD, and WHAT IT PRODUCED. Ordered most-specific first; the first
# present key wins for the header, and every present output key is shown.
# KEY-DRIVEN RATHER THAN AN if-CHAIN ON TOOL NAME, and that is the whole point: the previous
# version branched on `command` then `file_path` and returned a bare tool name for anything
# else. A real WebFetch record carries {url, prompt, output_tail} and NONE of those, so it
# rendered as the literal string "WebFetch " -- url, prompt and fetched content all dropped.
# WebSearch rendered as "WebSearch ". That is exactly the "it cannot see WebFetch results"
# report from another session, and it was this function, not the evidence logger: the logger
# records WebFetch and WebSearch correctly and always did.
# A new tool now degrades to "toolname + whatever identifying/output keys it happens to
# carry" instead of silently rendering as nothing.
# TWO PROVENANCES, KEPT APART -- an earlier version of this comment merged them and claimed
# the whole list came from the live log, which the council refuted.
# WHAT REACHES THE PITCH: `tool` (always, as the header), `exit_code` when present (via its
# own `rc=` branch, not via either tuple), ONE key from _IDENT_KEYS -- first match wins, the
# loop breaks -- and EVERY present key from _OUTPUT_KEYS.
# `description` is consequently unreachable for Bash in this corpus: all 2983 Bash records
# here carry a non-empty `command`, which wins first. Counted per record, not inferred from a
# union of keys -- the union only shows a key appeared SOMEWHERE. It stays in the tuple for a
# record that might carry a description and no command.
# EVERYTHING ELSE IS DROPPED, and that is stated as a RULE rather than as a list of dropped
# keys: a list would need updating whenever the logger gains a field, and a stale list reads
# as authoritative. If a key is not `tool`, not `exit_code`, and not in either tuple below,
# it does not reach the pitch.
# Two omissions ARE deliberate and worth naming: `prompt` (WebFetch) and `questions`
# (AskUserQuestion) name what was ASKED, while the answer/output keys carry what came BACK,
# and it is the answer that constitutes evidence.
# OBSERVED IN THIS INSTALL'S evidence.jsonl: {Bash: command/exit_code/stdout_tail/
# stderr_tail}, {Read: file_path/output_tail}, {Edit,Write: file_path},
# {WebFetch: url/output_tail}, {WebSearch: query/output_tail},
# {AskUserQuestion: answers}.
# READ FROM evidence_logger.py SOURCE, never yet seen in a log here: {Grep,Glob:
# args/output_tail} -- the block sets exactly `event["args"]` and `event["output_tail"]`.
# Covered on the strength of the source, not of a sighting.
# `answers` EARNS ITS PLACE. It carries the operator's actual decisions, and council members
# have repeatedly said they could not verify an attributed ruling from their seat. Rendering
# it is what lets "the operator chose X" be checked rather than taken on trust.
_IDENT_KEYS = ("command", "url", "query", "pattern", "args", "file_path", "description")
_OUTPUT_KEYS = ("stdout_tail", "output_tail", "stderr_tail", "answers")


def _render_evidence(rec: dict) -> str:
    """One evidence record, rendered so the doorman can actually reason from it.

    THE KEYS HERE ARE THE LOGGER'S, READ OFF THE LIVE FILE RATHER THAN ASSUMED. The previous
    version of this line read `tool_name` and `summary`. The evidence logger writes NEITHER,
    so every record collapsed to the literal string "? " and the doorman's entire evidence
    window was blank -- in this fire and in every fire it had ever run.
    THAT IS NOT A COSMETIC BUG. Two of the doorman's four checks are evidence-based
    ("does any surfaced command output demonstrate the TRANSITION?", "a count stated with no
    measurement visible in the session"), so both were structurally unanswerable: the honest
    answer was always "no measurement is visible", for every claim, forever. It surfaced by
    objecting three times running to figures that HAD just been measured, each time citing
    blank evidence -- a false positive that was really this function reporting its own
    blindness. An advisory seat that cannot see evidence does not fail loudly; it fails by
    objecting to everything precise, which reads like rigour.
    VERIFIED against ~/.claude/state/<session>/evidence.jsonl: a Bash record carries
    {at, command, description, exit_code, interrupted, stderr_tail, stdout_tail, tool}; a
    Read record carries {at, file_path, limit, offset, output_tail, tool}.
    `tool_name` is still accepted as a fallback so a future logger that emits it is not
    silently dropped again -- the failure above was silent for exactly that reason.
    Slices are per-record so twelve records stay well inside MAX_PITCH_CHARS; build_pitch
    truncates from the FRONT, so an oversized block would drop the NEWEST records, which are
    the ones a reviewer most needs.
    """
    tool = str(rec.get("tool") or rec.get("tool_name") or "?")
    head = tool
    if rec.get("exit_code") is not None:
        head += f" rc={rec.get('exit_code')}"
    for key in _IDENT_KEYS:
        value = rec.get(key)
        if value:
            head += (f" $ {str(value)[:200]}" if key == "command"
                     else f" {key}={str(value)[:200]}")
            break
    parts = [head]
    for key in _OUTPUT_KEYS:
        value = rec.get(key)
        if value:
            parts.append(f"    {key}: {_elide_middle(str(value))}")
    return "\n".join(parts)


# HOW MUCH OF ONE OUTPUT SURVIVES, and it is deliberately NOT a head slice.
# The line replaced here was `str(value)[:500]`. What it sliced was already elided text: the
# evidence logger's truncate_tail "keeps the head and the tail of long text, eliding the
# middle", on the stated grounds that neither end of a long result is safe to lose. A head
# slice of that discards the tail the logger preserved, so for any output longer than the cap
# the reviewer saw an opening and no conclusion. That matters most for exactly the evidence
# this seat keeps ASKING for -- a `grep -A` of a function, where the answer usually sits below
# the docstring. A reviewer shown half a probe and asked whether it supports a claim will say
# no, correctly, about the half it was given.
_OUT_BUDGET = 1100
_OUT_HEAD = 500


_ELISION = "\n    [... {} chars elided ...]\n    "


def _elide_middle(text: str) -> str:
    """Keep the head and the tail of `text`, eliding the middle, WITHIN _OUT_BUDGET total.

    THE MARKER IS CHARGED AGAINST THE BUDGET, which is standing rule 8. Slicing head+tail to
    the full budget and THEN adding the marker would exceed the cap by the marker's length,
    and for an input only slightly over the cap it would return something LONGER THAN THE
    INPUT -- the inversion that rule names.
    The marker's own length varies with the digit count it reports, so the reservation is
    computed from an UPPER BOUND -- the digits of len(text), which cannot be fewer than the
    digits of the elided count -- rather than from the answer it is about to produce.
    """
    if len(text) <= _OUT_BUDGET:
        return text
    reserve = len(_ELISION.format(len(text)))
    tail = max(0, _OUT_BUDGET - _OUT_HEAD - reserve)
    elided = len(text) - _OUT_HEAD - tail
    return text[:_OUT_HEAD] + _ELISION.format(elided) + (text[-tail:] if tail else "")


def build_pitch(tool_name: str, tool_input: dict, evidence: str) -> str:
    # COERCED, because review() promises it never raises and a payload is not obliged to
    # carry strings. A truthy non-string new_string/content (an int, a list) survives `or ""`
    # and then raises on the slice below -- the same truth-check-instead-of-type-check
    # mistake that `or {}` made with tool_input.
    body = (tool_input.get("new_string") if tool_name == "Edit"
            else tool_input.get("content"))
    body = body if isinstance(body, str) else ("" if body is None else str(body))
    parts = [
        f"TOOL: {tool_name}",
        f"TARGET: {tool_input.get('file_path', '?')}",
        "",
        "--- PROPOSED TEXT (this is what would be written) ---",
        body[:MAX_PITCH_CHARS],
        "--- END PROPOSED TEXT ---",
    ]
    if evidence:
        parts += ["", "--- WHAT WAS ACTUALLY OBSERVED THIS SESSION (most recent) ---",
                  evidence[:MAX_PITCH_CHARS], "--- END OBSERVATIONS ---"]
    return "\n".join(parts)


def _ask_openrouter(seat_cfg: dict, models: list[str], pitch: str) -> str:
    """One doorman turn over OpenRouter -- the API-billed route. "" on any failure."""
    if not models:
        return ""
    try:
        import consult_council as cc
    except Exception:  # noqa: BLE001
        return ""
    # THE RESULT ACCESS IS INSIDE THE HANDLER, not after it. `_openrouter_call_blocking` is a
    # private helper whose return shape this module does not own; an earlier version called
    # res.get() outside the try, so a non-dict result would raise straight through review()
    # and out of the hook -- while review()'s docstring promised it never raises. Its shape
    # returned a dict carrying text / model_used / returncode when probed this session
    # (anthropic/claude-sonnet-5 -> text='PONG', returncode=0). That is ONE observation of a
    # private helper, not a contract it owes us, which is exactly why the isinstance guard
    # below stands rather than trusting the shape.
    try:
        res = cc._openrouter_call_blocking(
            seat_cfg.get("name", "doorman"), models, f"{SYSTEM}\n\n{pitch}")
        return (res.get("text") or "").strip() if isinstance(res, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def _ask_claude_cli(model: str, pitch: str) -> str:
    """One doorman turn over the local `claude` CLI -- the SUBSCRIPTION route. "" on failure.

    WHY A LOCAL SUBPROCESS CALL rather than consult_council.run_claude: run_claude is async,
    and it hard-codes CLAUDE_MODEL through claude_cmd() -- it accepts NO model parameter, so
    a Sonnet doorman cannot be expressed through it at all.
    VERIFIED, not recalled: `claude -p --model claude-sonnet-5 --safe-mode --tools ""` with
    the prompt on stdin returned `PONG` at rc=0, and a deliberately bogus slug returned rc=1
    with "There's an issue with the selected model" -- so the flag is honoured rather than
    quietly ignored, which is the only thing that makes this route mean anything.

    The prompt goes on STDIN, never argv, because a pitch runs to MAX_PITCH_CHARS. `--tools
    ""` is passed so the seat has no tools -- it judges the hunk placed in front of it and
    has no business reading the tree. CLAUDE_DROP_ENV is scrubbed from the child env so the
    CLI's own login is what serves.

    WHAT THIS DOES NOT ESTABLISH -- and the scope here is INHERITED FROM THE PRIMARY SOURCE
    rather than invented: consult_council.py, at the comment above CLAUDE_MODEL, measured that
    scrubbing the variable makes the "ANTHROPIC_API_KEY ... takes precedence over your
    claude.ai login" warning disappear, and then states plainly that "No billing record was
    inspected. `total_cost_usd` is reported on BOTH paths ... nothing here establishes how a
    run was CHARGED, and no UI or doc may claim it does."
      - So this route selects an AUTH SOURCE. Whether it changes what is CHARGED is NOT
        established -- not by this module, and not by anything measured for it. An earlier
        draft of this docstring asserted that an inherited ANTHROPIC_API_KEY "would bill the
        API"; that is precisely the claim the source above forbids, and the council caught it.
      - THE FALLBACK IS AN OPENROUTER CALL REGARDLESS. Any empty CLI result routes to
        _ask_openrouter on fallback_model, so "subscription route" describes the PRIMARY path
        only.
      - Nor does it establish that the reply came from Sonnet: the bogus-slug rejection shows
        the slug is real and permitted to this account, not which weights answered.
      - Nor that the child session is genuinely toolless. The probes exercised `--model`, not
        tool availability; `--tools ""` matches consult_council's own CLAUDE_LEADER_TOOL_GUARD,
        which is the REASON for passing it, not evidence of its effect.
    """
    if not model:
        return ""
    try:
        import consult_council as cc
    except Exception:  # noqa: BLE001
        return ""
    env = dict(os.environ)
    for key in cc.CLAUDE_DROP_ENV:
        env.pop(key, None)
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, *cc.CLAUDE_RUNTIME_GUARD, "--tools", ""],
            input=pitch, text=True, capture_output=True,
            timeout=DOORMAN_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""                       # includes TimeoutExpired
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def ask(seat_cfg: dict, pitch: str) -> tuple[str | None, str]:
    """Returns (objection_text_or_None, status).

    status is one of: "ok" (no objection), "objection", "unreachable", "malformed".
    These are kept distinct on purpose -- see the module docstring.

    THE SEAT'S `transport` IS READ HERE, and until this function was written it was NOT:
    grep found the word zero times in this file, and every doorman call went to OpenRouter
    no matter what the roster said. A config key that nothing reads is worse than no key,
    because it advertises a choice the operator does not actually have.
    """
    transport = seat_cfg.get("transport") or "openrouter"
    model = seat_cfg.get("model") or ""
    fallback = seat_cfg.get("fallback_model") or ""
    if transport == "claude_subprocess":
        text = _ask_claude_cli(model, f"{SYSTEM}\n\n{pitch}")
        if not text and fallback:
            # SAME SHAPE AND SAME REASON AS THE COUNCIL'S SUBSCRIPTION SEATS: a usage cap, an
            # auth failure or a timeout must not silently drop the check. Note the two slugs
            # live in DIFFERENT NAMESPACES -- the primary is a CLI slug (`claude-sonnet-5`)
            # and the fallback an OpenRouter one (`anthropic/...`) -- so the roster carries
            # both and neither is derived from the other.
            text = _ask_openrouter(seat_cfg, [fallback], pitch)
    elif transport == "openrouter":
        text = _ask_openrouter(seat_cfg, [m for m in (model, fallback) if m], pitch)
    else:
        # DEGRADE LOUDLY. An unknown transport must not silently fall back to OpenRouter --
        # that would bill a route the operator did not choose -- and must not block the edit,
        # because the doorman is advisory in every other failure mode too.
        print(f"doorman: unknown transport {transport!r} in the roster's doorman entry; "
              f"NO pre-landing check ran for this edit.", file=sys.stderr)
        return None, "unreachable"
    if not text:
        return None, "unreachable"

    # PARSE STRICTLY. `startswith("OK")` also accepted "OKAY, but here is a problem..." and
    # any reply that opened with OK and raised an objection later -- turning a real objection
    # into a silent pass, which is the worst direction for this to fail in. An OK must be the
    # entire reply; an objection is recognised only by the literal marker.
    upper = text.upper()
    if "OBJECTION:" in upper:
        idx = upper.index("OBJECTION:")
        body = text[idx + len("OBJECTION:"):].strip()
        return (body, "objection") if body else (text[:400], "malformed")
    if upper.strip() in ("OK", "OK."):
        return None, "ok"
    return text[:400], "malformed"


def review(payload: dict) -> tuple[str | None, dict]:
    """Decide whether this edit should be turned back. Returns (deny_reason_or_None, meta).

    Never raises: the doorman sits in the path of every edit, so any internal failure must
    degrade to letting the edit through rather than blocking work."""
    meta: dict = {"status": "skipped"}
    if not isinstance(payload, dict):
        return None, meta
    tool_name = payload.get("tool_name", "")
    # `or {}` catches None and {} but NOT a truthy non-dict -- a payload carrying
    # tool_input="something" would sail past it and raise on the first .get, which
    # review() promises never to do. Type-check rather than truth-check.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    # session_id was the one field left uncoerced, and it is used to BUILD PATHS
    # (STATE_ROOT / session_id). A truthy non-string -- say 123 -- raises TypeError there,
    # which review() promises never to do. PROBED rather than assumed, after an earlier
    # draft here asserted "the harness sends string UUIDs" with no source: all 124 session_id
    # values recorded in this install's pending-review markers are `str`. That is 124
    # observations from one harness, not a contract, so the promise stays unqualified and so
    # does the guard.
    session_id = payload.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = str(session_id) if session_id is not None else ""
    cfg = seat()
    if cfg is None:
        return None, meta                       # no doorman configured: silently absent

    ident = edit_identity(tool_name, tool_input)
    store, ok = load_objections(session_id)
    if not ok:
        # FAIL CLOSED: unknown history is treated as no history, so this is a FIRST attempt.
        print("doorman: objection store unreadable; treating this as a first submission "
              "(an override cannot be granted on unknown history).", file=sys.stderr)
        _log(session_id, {"event": "store_unreadable"})
        store = {}

    prior = store.get(ident)
    if prior:
        # SANITISE FIRST, DECIDE SECOND. An override should follow only from a usable record
        # that this edit was objected to before. Two earlier versions were too permissive:
        # one coerced a non-dict entry into {"objection": str(entry)} and granted the
        # override anyway; the next accepted any dict, so {"attempts": 1} -- carrying no
        # objection at all -- also counted. Either way a corrupted store could MANUFACTURE
        # overrides. Unusable history is now treated as no history: consult the doorman
        # afresh.
        # WHAT THIS DOES AND DOES NOT ESTABLISH. It checks the RECORD is usable, not that the
        # doorman genuinely objected -- a fabricated but well-formed entry would still pass,
        # and nothing here can distinguish one, since this process is what writes the store.
        # THE COST OF BEING WRONG IS BOUNDED, and an earlier comment overstated it as "an
        # unreviewed edit": it is not. A wrongly-granted override bypasses only the DOORMAN.
        # The edit still lands and is still sent to the twelve-seat council, exactly as it
        # would have without a doorman at all. What is lost is one cheap early check and the
        # audit record of a disagreement -- worth closing, but not a hole in review itself.
        if not (isinstance(prior, dict)
                and isinstance(prior.get("objection"), str)
                and prior.get("objection", "").strip()):
            _log(session_id, {"event": "malformed_entry_ignored", "identity": ident})
            print("doorman: an objection record without a usable objection was ignored "
                  "rather than honoured as an override; this edit is being checked again "
                  "from scratch.", file=sys.stderr)
            prior = None
    if prior:
        # THE OVERRIDE. The identical edit has been objected to and is being re-submitted,
        # which the operator ruled means "considered and disagreed". It lands, and the
        # council -- which is NOT told any of this -- reviews it on its own terms.
        # `attempts` is coerced defensively: int("garbage") raises, and
        # an earlier `or 1` guard covered only falsy values. Caught here, the override
        # proceeds to the recording step -- whose own failure is a separate case, announced
        # there. Uncaught, the raise would reach tier0_gate's except, which ANNOUNCES the
        # bypass but still skips the check for that submission.
        try:
            attempts = int(prior.get("attempts", 1)) + 1
        except (TypeError, ValueError):
            attempts = 2
        meta = {"status": "override", "identity": ident,
                "prior_objection": prior.get("objection", ""),
                "attempts": attempts}
        store[ident] = {**prior, "attempts": meta["attempts"],
                        "overridden_at": datetime.now(timezone.utc).isoformat()}
        save_objections(session_id, store)
        # THE RECORD CARRIES AN EXCERPT OF THE EDIT, NOT JUST ITS HASH. An identity digest
        # proves two submissions matched but tells an auditor nothing about WHAT was waved
        # through, and the council is never told any of this. WHEN THE WRITE SUCCEEDS this
        # log holds a timestamped event; `wrote` below carries whether it did, because
        # claiming the record exists regardless would be the same absence-reads-as-approval
        # error this file is full of retractions for. `old_string` and `proposed` are each
        # truncated to 2000 characters -- the other fields are stored whole -- so a large
        # edit is identifiable from this record but not necessarily reconstructable.
        body = (tool_input.get("new_string") if tool_name == "Edit"
                else tool_input.get("content")) or ""
        wrote = _log(session_id, {
            "event": "override", **meta,
            "tool": tool_name,
            "file": tool_input.get("file_path", ""),
            "old_string": str(tool_input.get("old_string") or "")[:2000],
            "proposed": str(body)[:2000],
        })
        note = ("" if wrote else
                " *** AND THE AUDIT RECORD COULD NOT BE WRITTEN. The objection store may "
                "still hold overridden_at/attempts for this identity, but the per-event "
                "trace is missing -- surface this to the operator by hand. ***")
        print(f"doorman: OVERRIDDEN by re-submission -- "
              f"{tool_input.get('file_path', '?')}. Prior objection stands unanswered: "
              f"{str(prior.get('objection', ''))[:200]}{note}", file=sys.stderr)
        return None, meta

    pitch = build_pitch(tool_name, tool_input, recent_evidence(session_id))
    objection, status = ask(cfg, pitch)
    meta = {"status": status, "identity": ident}
    if status == "unreachable":
        _log(session_id, {"event": "unreachable", "file": tool_input.get("file_path", "")})
        print("doorman: unreachable; edit proceeds and goes to the council as usual.",
              file=sys.stderr)
        return None, meta
    if status == "malformed":
        _log(session_id, {"event": "malformed", "raw": objection,
                          "file": tool_input.get("file_path", "")})
        print("doorman: reply was neither OK nor a parseable objection; edit proceeds. "
              "Raw reply logged.", file=sys.stderr)
        return None, meta
    if status == "ok":
        _log(session_id, {"event": "ok", "file": tool_input.get("file_path", "")})
        return None, meta

    store[ident] = {"objection": objection, "attempts": 1,
                    "file": tool_input.get("file_path", ""),
                    "first_seen": datetime.now(timezone.utc).isoformat()}
    if not save_objections(session_id, store):
        # AN OBJECTION THAT CANNOT BE RECORDED MUST NOT BE ISSUED. The override is granted by
        # recognising the re-submitted edit, so an unrecorded objection would deny the first
        # attempt and then deny the identical retry too -- an advisory check turned into an
        # inescapable block. Fall through instead: the edit lands and goes to the council,
        # which is the pre-doorman behaviour and the correct degradation.
        meta["status"] = "objection_unrecorded"
        _log(session_id, {"event": "objection_unrecorded", "identity": ident,
                          "objection": objection,
                          "file": tool_input.get("file_path", "")})
        print("doorman: objection NOT issued because it could not be recorded -- issuing it "
              "would be un-overridable. The edit proceeds to the council. The objection "
              f"was: {str(objection)[:200]}", file=sys.stderr)
        return None, meta
    _log(session_id, {"event": "objection", "identity": ident,
                      "objection": objection, "file": tool_input.get("file_path", "")})
    return (
        f"DOORMAN OBJECTION (advisory -- one cheap model, not the council):\n\n{objection}\n\n"
        f"Revise the edit, or SUBMIT THE IDENTICAL EDIT AGAIN to overrule this and let the "
        f"council decide. An override is recorded.\n\n"
        f"NOTE: identical means BYTE-IDENTICAL in the edit itself -- tool, file_path, "
        f"old_string, new_string, content, replace_all. Nothing is normalised, so a changed "
        f"space or line ending is a different edit. A REWORDED resubmission is therefore a "
        f"NEW edit, checked from scratch: it does NOT override, and no disagreement is "
        f"recorded anywhere."
    ), meta


if __name__ == "__main__":
    # Standalone use is for testing the seat end-to-end; in production tier0_gate calls
    # review() directly so the free deterministic checks run first and no model call is
    # spent on an edit that was going to be denied anyway.
    try:
        pl = json.load(sys.stdin)
    except (ValueError, json.JSONDecodeError):
        sys.exit(0)
    deny, m = review(pl if isinstance(pl, dict) else {})
    print(json.dumps({"deny": deny, "meta": m}, default=str))
