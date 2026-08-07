#!/usr/bin/env python3
"""Council advisor: PostToolUse hook (advisory mode).

Runs the workers' council after a matched tool call (Write, Edit, or
NotebookEdit) completes, and surfaces any concerns to Claude via the
hook's structured output.

A PostToolUse hook cannot block or undo the tool: it fires after the
tool has already run, so the action always stands. (The Claude Code
hooks documentation, mirrored locally in the plugin-dev hook-development
SKILL.md, documents hook exit code 2 as a blocking error whose stderr is
fed back to Claude, but for PostToolUse the tool has already executed.)
This hook therefore surfaces concerns rather than denying them, across
three tiers keyed on the wrapper's return code:
  - PASS  (rc 0): stays silent, nothing surfaces.
  - WARN  (rc 1): emits the wrapper's full output (verdict +
    per-member critiques) as additionalContext so Claude reads it on
    the next model request and can revise, revert, or proceed. A
    discoverable, Claude-initiated dialogue-escalation command is
    appended (Claude runs it only if it wants to discuss the verdict).
  - BLOCK (rc 2, rule-11 caveat-without-probe): routes the same
    output to Claude via stderr (exit 2), asking it to revert the
    just-made change and re-attempt after running the required probe.
    This is a request to Claude, not a denial of the action that
    already executed.

The wrapper runs once per tool call; there is no dialogue or round
counter at this layer. Escalation to a multi-turn dialogue is
Claude-initiated: this hook never auto-creates a thread, it only
surfaces the command Claude can run (council_dialogue.py escalate).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

COUNCIL_ROOT = Path(__file__).resolve().parent
WRAPPER = COUNCIL_ROOT / "consult_council.py"
DIALOGUE = COUNCIL_ROOT / "council_dialogue.py"
EVIDENCE_STATE_ROOT = Path.home() / ".claude" / "state"

# --- Auto-revert on BLOCK ---------------------------------------------------
#
# Enabled deliberately, and only after the measurement described below.
# Read that before you turn this on.
#
# This hook is PostToolUse: by the time it runs the write is already on disk, so
# a BLOCK could only ever REQUEST a revert. That request does halt the turn (exit
# 2 feeds stderr back to Claude), but nothing undid the write, and the one layer
# that can deny a write -- the PreToolUse gate -- is a regex. Measured over the
# July 2026 logs: 57 of 60 BLOCKs contained none of the gate's eight trigger
# phrases, so the gate could not have caught them. (What that shows is only that
# the phrases did not appear; the reason is not established here.)
#
# Enforcing means paying the council's false-positive rate on every BLOCK, so it
# was measured BEFORE being built. A random cohort of 12 BLOCK concerns was
# labelled and adjudicated by the council with the raising member excluded, and
# none of the labels that survived adjudication was a refutation. The cohort,
# its seed and the rulings are on disk in sample_cohorts.jsonl / outcomes.jsonl,
# so this is checkable rather than a number in a comment. Honest limits: it is a
# REASON-level rate on a small sample, and enforcement acts on FIRES. An order of
# magnitude, not a guarantee.
#
# Two rules follow from "not a guarantee", and both are implemented in
# auto_revert() below:
#   1. NEVER DESTROY THE WORK. A wrong BLOCK must cost minutes, not work.
#   2. NEVER DELETE A FILE. A new-file Write has no prior content, so "reverting"
#      it would mean deletion -- categorically more destructive than restoring
#      bytes, and not something a hook should do behind your back.
AUTO_REVERT_ON_BLOCK = True
REVERT_BACKUP_DIR = COUNCIL_ROOT / "reverted"
NO_AUTO_REVERT = COUNCIL_ROOT / "NO_AUTO_REVERT"   # kill switch, checked per call


def auto_revert(tool_input: dict, tool_response: dict) -> str:
    """Restore a file's pre-edit content after a rule-11 BLOCK.

    Returns a line describing exactly what happened, for Claude to read. Never
    raises: this runs inside a PostToolUse hook, and a crash here must not take
    the tool call with it.
    """
    if not AUTO_REVERT_ON_BLOCK:
        return ""
    if NO_AUTO_REVERT.exists():
        return ("AUTO-REVERT DISABLED (NO_AUTO_REVERT present). The write stands; "
                "revert it yourself.")
    if not isinstance(tool_response, dict) or not isinstance(tool_input, dict):
        return ""

    file_path = (tool_input.get("file_path")
                 or tool_response.get("filePath") or "")
    if not file_path:
        return ""
    original = tool_response.get("originalFile")

    # Rule 2. Restore ONLY when the payload actually carries prior bytes.
    #
    # MEASURED (real PostToolUse payloads, Claude Code, 2026-07-14) -- three Writes
    # captured via the DEBUG_PAYLOAD probe above:
    #
    #   brand-new file        -> type="create", originalFile=None, 0 hunks
    #   EXISTING EMPTY file   -> type="create", originalFile=None, 0 hunks
    #   existing non-empty    -> type="update", originalFile=str,  1 hunk
    #
    # In the first two the probed fields MATCH exactly: Claude Code reports a write
    # to an existing empty file as a "create". So none of the fields that could
    # plausibly carry prior-existence (type, originalFile, structuredPatch)
    # separates "this file did not exist" from "this file existed and was empty".
    # The probe compared those fields, not every key in the payload, so the honest
    # claim is "no field I checked distinguishes them", not "no field could".
    # Either way the two want OPPOSITE undo operations -- delete the file, versus
    # truncate it back to empty -- and guessing wrong deletes a file the user made.
    # So decline both, and say so.
    #
    # Note `originalFile` came back None, not "", in all three Write cases (Edit
    # and NotebookEdit were not probed). The `== ""` half of the test below is
    # therefore belt-and-braces, not the branch that fires.
    if not isinstance(original, str) or original == "":
        return (f"AUTO-REVERT DECLINED for {file_path}: the payload carries no "
                f"prior content (originalFile={original!r}). Measured: Claude Code "
                f"reports BOTH a new file AND an existing-but-empty file as "
                f"type='create' with originalFile=None, so undoing this could mean "
                f"deleting a file or truncating one, and no field the probe checked "
                f"(type, originalFile, structuredPatch) tells them apart. This hook "
                f"will not guess and delete. REVERT IT YOURSELF, then run the probe "
                f"rule 11 needs.")

    p = Path(file_path)
    try:
        current = p.read_text()
    except OSError as e:
        return f"AUTO-REVERT FAILED for {file_path}: cannot read it back ({e})."
    if current == original:
        return ""                       # nothing to undo

    # Rule 1: park the rejected content BEFORE overwriting it. If the council is
    # wrong, the work is one `cp` away, not gone.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = REVERT_BACKUP_DIR / f"{stamp}-{uuid.uuid4().hex[:8]}-{p.name}"
    try:
        REVERT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup.write_text(current)
    except OSError as e:
        return (f"AUTO-REVERT ABORTED for {file_path}: could not park the "
                f"rejected content ({e}), so nothing was overwritten. The write "
                f"STANDS. Revert it yourself.")

    # ATOMIC restore. A bare write_text() truncates first and can then die
    # mid-write, leaving a half-file -- i.e. the "safety" feature would be the
    # thing that destroyed the file. Write a sibling temp file, fsync, and
    # os.replace() it into place: replace is atomic on POSIX, so the target is
    # either the old bytes or the restored bytes and never a fragment.
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w") as fh:
            fh.write(original)
            fh.flush()
            os.fsync(fh.fileno())
        # MEASURED on this host, all three:
        #   tempfile.mkstemp()          -> mode 0600
        #   bare os.replace(tmp, target) -> target mode 0755 becomes 0600
        #   shutil.copymode(Path, Path) -> mode 0755 preserved
        # So without this copymode, "restoring" an executable script would
        # silently strip its +x bit and the file would stop running. The safety
        # feature would be the thing that broke the file.
        shutil.copymode(p, tmp_path)
        # Write-elsewhere-then-swap, rather than writing the target in place, so
        # that no reader ever observes a partially-rewritten file. The chain is
        # verified rather than assumed:
        #   strace       -> os.replace(a, b) issues rename("a", "b") = 0
        #                   (os.replace's own docstring does NOT state this)
        #   man 2 rename -> "If newpath already exists, it will be atomically
        #                   replaced"
        #
        # Scope, precisely: that buys ATOMIC REPLACEMENT. It is NOT a durability
        # guarantee. The file's bytes are fsync'd above, but the directory entry
        # is not, and whether that can lose the rename across a power cut is
        # UNVERIFIED here. Durability is not the goal; not clobbering the target
        # is. Do not cite this code as evidence of the former.
        os.replace(tmp_path, p)
        tmp_path = None
    except OSError as e:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return (f"AUTO-REVERT FAILED for {file_path}: could not restore the "
                f"original ({e}). The file was NOT modified by this hook and the "
                f"write STANDS. Your content is also safe at {backup}.")

    return (f"AUTO-REVERTED {file_path} to its pre-edit content. Your rejected "
            f"version was NOT destroyed -- it is at {backup}. If you believe the "
            f"BLOCK is wrong, do not just rewrite the file: escalate to the "
            f"council with evidence, or restore from that path.")

# Tools the council reviews; the matcher in settings.json mirrors this
# set exactly. Each has a dedicated pitch formatter below; any other
# tool that reaches this hook routes through the generic JSON pitch (the
# final return in build_pitch). (MultiEdit is not a current Claude Code
# tool, so it is intentionally absent here and from the matcher.)
SUPPORTED_TOOLS = ("Write", "Edit", "NotebookEdit")

# --- Edit-context injection -------------------------------------------------
#
# Why this exists: without it, a member sees only old_string/new_string and no
# surrounding code. All three council members independently ranked that their
# single biggest defect ("Edit myopia"), and gemini named the mechanism: it
# cannot see whether a symbol is declared elsewhere, so rules 3/13 force a
# defensive WARN. Two of the three members have no tools and cannot go look.
#
# We do NOT search the file for the edited text. An earlier draft did, and the
# council correctly killed it: matching new_string against whole lines fails for
# the commonest edit shape (a partial-line change, e.g. renaming x to count
# inside "for count in items:"), and a text search cannot tell which occurrence
# actually changed when the same text appears elsewhere in the file.
#
# None of that guessing is necessary. The PostToolUse payload already carries
# the answer authoritatively. Captured from a real Edit fire on this machine,
# tool_response has these keys:
#     filePath, oldString, newString, originalFile, structuredPatch,
#     userModified, replaceAll
# where structuredPatch is a list of unified-diff hunks. Each hunk carries a
# 1-indexed newStart / newLines span locating it in the POST-edit file, e.g.
#     [{"oldStart": 1, "oldLines": 3, "newStart": 1, "newLines": 3,
#       "lines": [" alpha", "-beta", "+BETA", " gamma"]}]
# That span covers the changed lines PLUS the differ's surrounding context
# lines, so it is the region the edit touched, not only the lines that differ.
# The location is given to us, not inferred.
#
# A single Edit tool call can produce MORE THAN ONE hunk. Measured directly on
# this machine: one Edit replacing a 10-line block whose interior lines were
# unchanged returned two hunks (newStart=1 newLines=4, and newStart=7
# newLines=4), split around the untouched middle. So "hunk" is not a synonym
# for "edit site", and iterating hunks is required.
#
# CONTEXT_MAX_BLOCK_LINES is a token budget, not a claim about code. It was
# picked from the actual distribution of def/class block sizes across the
# Python files this council reviews (n=135: median 17, p75 38, p90 54, p95 98):
# a cap of 80 lets 94% of blocks be shown whole. Tune it if fires get costly.
# The remaining caps are deliberately conservative guards, chosen to bound the
# hook's cost rather than derived from data; they are tunable, not conventions.
CONTEXT_MAX_BLOCK_LINES = 80
CONTEXT_WINDOW = 10          # fallback: lines shown above/below the edit
CONTEXT_MAX_SITES = 3        # cap hunks shown when one edit touches many places
CONTEXT_MAX_FILE_BYTES = 2_000_000

_PY_BLOCK_RE = re.compile(r"^(\s*)(async\s+def|def|class)\s")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _numbered(lines: list[str], start_idx: int) -> str:
    """Render lines with 1-indexed line numbers. start_idx is 0-indexed."""
    return "\n".join(f"{start_idx + i + 1:5d} | {ln}"
                     for i, ln in enumerate(lines))


def _python_block(lines: list[str], lo: int, hi: int) -> tuple[int, int] | None:
    """0-indexed inclusive span of the def/class enclosing lines[lo:hi+1].

    Returns None when the edit is not inside a def/class (module level).

    Anchor normalisation, and why it is deliberately narrow. codex predicted
    that a hunk landing on a decorator (adding @functools.cache above a def)
    would miss the block; it reproduced, rendering only the decorator, the def
    line and one body line before a blank stopped the paragraph fallback --
    hiding the very function being decorated. So a hunk that OPENS on a
    decorator skips forward to the def it decorates.

    That forward skip must not be generalised. A draft that also skipped blank
    and `#` lines was caught by gemini and reproduced: editing a trailing
    comment inside func1 skipped forward into func2, rendered the WRONG
    function, and -- because the edited line then fell outside the block -- the
    changed line was elided entirely and never shown. Misattributed context is
    worse than none. So blanks anchor BACKWARD (staying inside the block the
    edit is already in), and comments are not skipped at all: an indented
    comment resolves to its enclosing def through the normal indent scan.
    """
    a = lo
    if lines[a].lstrip().startswith("@"):
        j = a
        while j < len(lines) and (lines[j].lstrip().startswith("@")
                                  or not lines[j].strip()):
            j += 1
        if j < len(lines) and _PY_BLOCK_RE.match(lines[j]):
            a = j
    while a > 0 and not lines[a].strip():
        a -= 1
    if a >= len(lines) or not lines[a].strip():
        return None

    if _PY_BLOCK_RE.match(lines[a]):
        def_line = a                      # the hunk opens a def/class itself
    else:
        def_line = None                   # find the def/class enclosing it
        target = _indent(lines[a])
        for i in range(a, -1, -1):
            if _PY_BLOCK_RE.match(lines[i]) and _indent(lines[i]) < target:
                def_line = i
                break
        if def_line is None:
            return None

    base = _indent(lines[def_line])
    start = def_line
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1                        # include the decorators

    end = def_line
    for j in range(def_line + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and _indent(ln) <= base:
            break
        end = j
    # The scan above runs to the next line at or below the block's indent, so
    # it swallows the blank lines that separate this block from the next one.
    # Trim them back, but never past the edit itself.
    while end > hi and not lines[end].strip():
        end -= 1
    if end < hi:            # edit spills past the block; not a clean enclosure
        return None
    return start, end


def _paragraph_block(lines: list[str], lo: int, hi: int) -> tuple[int, int]:
    """Blank-line-delimited span containing lines[lo:hi+1].

    The generic fallback for non-Python files (.md, .json, .yaml, shell),
    where there is no def/class to anchor on. Suggested by deepseek, whose
    point was that language-aware parsing is fragile across the mix of file
    types this council actually reviews.
    """
    start = lo
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = hi
    while end < len(lines) - 1 and lines[end + 1].strip():
        end += 1
    return start, end


def _block_span(lines: list[str], lo: int, hi: int,
                is_python: bool) -> tuple[int, int]:
    """0-indexed inclusive span of the context block enclosing lines[lo:hi+1]."""
    span = _python_block(lines, lo, hi) if is_python else None
    if span is None:
        span = _paragraph_block(lines, lo, hi)
    return span


def _render_span(lines: list[str], start: int, end: int,
                 edits: list[tuple[int, int]]) -> str:
    """Render lines[start:end+1], eliding down to the edits when it is too big.

    `edits` are the (lo, hi) hunk ranges that fall inside this block. When the
    block busts the budget we do NOT render lo..hi as one stretch: two edits at
    opposite ends of a long function would then print the whole function
    between them and silently defeat CONTEXT_MAX_BLOCK_LINES. Instead each edit
    gets its own bounded window and the gaps are elided.
    """
    if end - start + 1 <= CONTEXT_MAX_BLOCK_LINES:
        return _numbered(lines[start:end + 1], start)

    # The hunk spans outrank padding. An earlier draft trimmed the kept set to
    # its first and last halves, which could discard a middle hunk entirely --
    # exactly the lines a reviewer needs. So padding is dropped first, and a
    # hunk span is given up only when the spans themselves, plus the block's
    # mandatory opening line, exceed the budget (handled below by eliding
    # inside the spans rather than around them). Note `must` holds whole hunk
    # spans, which include the differ's unchanged context lines, not only the
    # lines that actually changed.
    must: set[int] = {start}
    for lo, hi in edits:
        must.update(range(max(start, lo), min(end, hi) + 1))

    if len(must) > CONTEXT_MAX_BLOCK_LINES:
        # The hunk spans plus the opening line bust the budget on their own.
        # Nothing to do but elide inside them; keep the head and both ends so
        # the shape of the change stays legible.
        ordered = sorted(must)
        half = max(1, (CONTEXT_MAX_BLOCK_LINES - 1) // 2)
        keep = {start} | set(ordered[:half]) | set(ordered[-half:])
    else:
        # Spend whatever budget is left on the padding nearest to an edit.
        keep = set(must)
        pad: set[int] = set()
        for lo, hi in edits:
            pad.update(range(max(start, lo - CONTEXT_WINDOW),
                             min(end, hi + CONTEXT_WINDOW) + 1))
        dist = lambda i: min(abs(i - lo) if i < lo else (i - hi if i > hi else 0)
                             for lo, hi in edits)
        for i in sorted(pad - keep, key=dist):
            if len(keep) >= CONTEXT_MAX_BLOCK_LINES:
                break
            keep.add(i)

    out: list[str] = []
    prev: int | None = None
    for i in sorted(keep):
        if prev is not None and i > prev + 1:
            out.append(f"      | ... ({i - prev - 1} lines elided) ...")
        out.append(f"{i + 1:5d} | {lines[i]}")
        prev = i
    if prev is not None and prev < end:
        out.append(f"      | ... (block continues to line {end + 1}) ...")
    return "\n".join(out)


def edit_context(file_path: str, tool_response: dict) -> str:
    """Surrounding code for an Edit, located from the payload's structuredPatch.

    Returns '' when no context can be produced. Every failure mode here is a
    silent degradation to no-context (the old behaviour), never an exception:
    this runs inside a PostToolUse hook and must not break the tool call.
    """
    if not isinstance(tool_response, dict):
        return ""
    hunks = tool_response.get("structuredPatch")
    if not isinstance(hunks, list) or not hunks:
        # No patch in the payload (unexpected shape, or a no-op edit). Degrade
        # silently rather than fall back to guessing at the location.
        return ""

    try:
        p = Path(file_path)
        if not p.is_file() or p.stat().st_size > CONTEXT_MAX_FILE_BYTES:
            return ""
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:          # binary
            return ""
        text = raw.decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        # Verified this session: Path(None), Path(123) and Path({...}) each
        # raise TypeError, which an OSError-only guard would not catch. These
        # three types degrade to no-context here; the call site in build_pitch
        # carries a broad guard for anything else, so context can never break
        # the hook.
        return ""

    lines = text.split("\n")
    is_python = file_path.endswith(".py")

    # Resolve every hunk to the span of the block that encloses it.
    regions: list[tuple[int, int, int, int]] = []   # (start, end, lo, hi)
    for h in hunks:
        try:
            start_1 = int(h["newStart"])
            count = int(h.get("newLines", 1))
        except (KeyError, TypeError, ValueError):
            continue
        lo = max(0, start_1 - 1)
        hi = min(len(lines) - 1, lo + max(count, 1) - 1)
        if lo > hi:
            continue
        s, e = _block_span(lines, lo, hi, is_python)
        regions.append((s, e, lo, hi))
    if not regions:
        return ""

    # Merge hunks whose enclosing blocks overlap. Two hunks in the same
    # function, or in a file with no blank lines to split on, resolve to the
    # same span; rendering each separately would print the identical block
    # twice. A merged region keeps ONE copy of the block and carries the list
    # of hunk ranges inside it, so _render_span can protect each of them
    # individually instead of treating lo..hi as one undifferentiated stretch.
    regions.sort()
    merged: list[tuple[int, int, list[tuple[int, int]]]] = []
    for s, e, lo, hi in regions:
        if merged and s <= merged[-1][1]:
            ps, pe, pedits = merged[-1]
            merged[-1] = (ps, max(pe, e), pedits + [(lo, hi)])
        else:
            merged.append((s, e, [(lo, hi)]))

    shown = merged[:CONTEXT_MAX_SITES]
    parts = []
    for k, (s, e, edits) in enumerate(shown, 1):
        label = f"region {k} of {len(merged)}" if len(merged) > 1 else "context"
        spans = ", ".join(f"{lo + 1}-{hi + 1}" for lo, hi in edits)
        parts.append(
            f"--- {label}: diff hunk span(s) {spans} of {file_path} (each "
            f"span is changed lines plus the differ's surrounding context, "
            f"not exclusively changed lines); enclosing block below ---\n"
            + _render_span(lines, s, e, edits))
    if len(merged) > CONTEXT_MAX_SITES:
        parts.append(f"({len(merged)} separate regions were touched; only the "
                     f"first {CONTEXT_MAX_SITES} are shown)")
    return "\n\n".join(parts)


def build_pitch(tool_name: str, tool_input: dict,
                tool_response: dict | None = None) -> str:
    if tool_name == "Write":
        return (
            f"Tool: Write\n"
            f"Target: {tool_input.get('file_path', '')}\n\n"
            f"--- Proposed content begin ---\n"
            f"{tool_input.get('content', '')}\n"
            f"--- Proposed content end ---\n"
        )
    if tool_name == "Edit":
        replace_all = tool_input.get("replace_all", False)
        file_path = tool_input.get("file_path", "")
        pitch = (
            f"Tool: Edit\n"
            f"Target: {file_path}\n"
            f"Replace all: {bool(replace_all)}\n\n"
            f"--- Old string begin ---\n"
            f"{tool_input.get('old_string', '')}\n"
            f"--- Old string end ---\n\n"
            f"--- New string begin ---\n"
            f"{tool_input.get('new_string', '')}\n"
            f"--- New string end ---\n"
        )
        # Broad guard: context is an enrichment, never a correctness
        # dependency. A PostToolUse hook runs after the tool has already
        # succeeded, so nothing here is allowed to turn a good edit into a
        # hook failure. Any exception at all degrades to the old
        # no-context pitch.
        try:
            ctx = edit_context(file_path, tool_response or {})
        except Exception:  # noqa: BLE001
            ctx = ""
        if ctx:
            pitch += (
                "\n--- Surrounding code, from the file as it now stands ---\n"
                "The edit above has already been applied, so this is the "
                "resulting code. Line numbers are 1-indexed and real.\n"
                "This is a LOCAL window: it shows the enclosing block, so you "
                "can judge indentation, local scope, and whether the change "
                "reads correctly where it sits. It does NOT show callers "
                "elsewhere in the file or in other files, and a symbol absent "
                "from this window may still be defined out of view. Do not "
                "infer from its absence here.\n\n"
                f"{ctx}\n"
                "--- End surrounding code ---\n"
            )
        return pitch
    if tool_name == "NotebookEdit":
        # tool_input fields: notebook_path and new_source are required;
        # cell_id, cell_type, and edit_mode are optional (edit_mode
        # defaults to "replace").
        return (
            f"Tool: NotebookEdit\n"
            f"Target: {tool_input.get('notebook_path', '')}\n"
            f"Cell id: {tool_input.get('cell_id', '')}\n"
            f"Cell type: {tool_input.get('cell_type', '')}\n"
            f"Edit mode: {tool_input.get('edit_mode', 'replace')}\n\n"
            f"--- New cell source begin ---\n"
            f"{tool_input.get('new_source', '')}\n"
            f"--- New cell source end ---\n"
        )
    return (
        f"Tool: {tool_name}\n"
        f"Input (json):\n{json.dumps(tool_input, indent=2)}\n"
    )


def target_path_for_log(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Write", "Edit"):
        return tool_input.get("file_path", "")
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path", "")
    # Any other tool carries its full payload through the generic pitch;
    # it gets no dedicated --target-path label here.
    return ""


# --- PENDING-REVIEW MARKERS: making a LOST review distinguishable from a PASS -
#
# THE DEFECT THIS EXISTS FOR, measured 2026-08-06 and not inferred. Comparing every
# successful Write/Edit in two session transcripts against the fires in logs/:
#     Noumad session  250 successful writes -> 162 council fires   (35% lost)
#     Council session 522 successful writes -> 376 council fires   (27% lost)
# `evidence_logger.py` sits on the IDENTICAL PostToolUse matcher and recorded 252 of 252
# writes for the first session, so the hook system fires reliably and it is THIS script
# that goes missing. And because a PASS returns 0 in silence (see below), a review that
# never happened is byte-identical, from the agent's side, to a review that approved.
# That is the project's own rule-12 hazard arriving structurally rather than through a
# scripted write: absence of a verdict reads as approval.
#
# WHAT THIS DOES: write a marker BEFORE the council subprocess starts and delete it after
# the subprocess returns -- by ANY route, including a timeout or a non-zero exit. A marker
# that outlives its process therefore means exactly one thing: THIS PROCESS DIED BETWEEN
# THOSE TWO POINTS, taking an unfinished review with it. Nothing else produces one.
#
# WHY DELETE-ON-EVERY-RETURN AND NOT ONLY ON SUCCESS. The question a marker answers is
# "did the review COMPLETE", not "did it pass". A wrapper that exits 3 completed and said
# so, and its notice already reaches the agent; leaving a marker for it would report a
# second, phantom failure for an event the agent was already told about.
#
# WHY A KILL LEAVES ONE BEHIND, which is the whole mechanism: SIGKILL runs no handlers at
# all, and Python's default SIGTERM disposition terminates without unwinding, so neither
# runs a `finally`. The marker survives precisely in the cases that are otherwise silent.
# It is written with fsync BEFORE the subprocess launches so a kill cannot lose it to
# buffering.
#
# WHAT A MARKER DOES NOT ESTABLISH: why the process died. It proves a review was started
# and did not finish; it names no cause. The leading hypothesis is that the harness kills
# long-running hooks (hook-driven fires show a p99 of 579s and only one above 600s, while
# manual runs reach 1459s), but that is UNCONFIRMED and this instrument is deliberately
# agnostic to it -- it measures the loss, whatever causes it.
PENDING_DIRNAME = "pending-review"
# An orphan younger than this may simply be a fire still running in a concurrent hook --
# concurrency SERIALIZES fires rather than dropping them (measured: 4 concurrent advisors
# all logged, but wall time went 81s -> 326s), so a live sibling is expected and must not
# be reported as a loss. 900s matches the wrapper timeout above: past it, no live fire of
# ours can still be running, because subprocess.run would already have raised.
ORPHAN_MIN_AGE_S = 900


def _pending_dir(session_id: str) -> Path:
    """Directory holding this session's in-flight review markers.

    Sessions are kept apart so one session's reconciliation never reports another's live
    fire as a loss -- with parallel sessions the norm on this machine, a shared directory
    would make every concurrent fire look like an orphan to whichever session looked first.
    """
    return EVIDENCE_STATE_ROOT / (session_id or "_no_session") / PENDING_DIRNAME


def write_pending_marker(session_id: str, tool_name: str, target: str,
                         tool_use_id: str = "") -> Path | None:
    """Record that a review is STARTING. Returns the marker path, or None if it could not
    be written -- never raises, because failing to instrument a review must not also
    prevent it."""
    try:
        d = _pending_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_use_id) or uuid.uuid4().hex
        path = d / f"{name}.json"
        payload = {
            "started": datetime.now(timezone.utc).isoformat(),
            "started_monotonic_pid": os.getpid(),
            "tool_name": tool_name,
            "target_path": target,
            "tool_use_id": tool_use_id,
            "session_id": session_id,
        }
        # fsync before the council starts: a marker lost to a page cache on a killed
        # process would defeat the entire mechanism.
        with open(path, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        return path
    except OSError:
        return None


# ---------------------------------------------------------------- heartbeats
# WHAT A PLAIN MARKER CANNOT TELL YOU: WHEN the advisor died, or what else was running
# when it did. It records a start and then either vanishes or does not. That is enough to
# COUNT losses -- it is what measured ~30% -- but not to diagnose them, because every
# hypothesis about the cause is a hypothesis about a TIME (a ceiling at some duration) or
# about LOAD (concurrent fires). A marker holds neither.
#
# SO EACH FIRE NOW BEATS INTO A SIDECAR WHILE IT WAITS. A stranded marker's last beat is
# the moment the advisor stopped breathing, +/- one interval, and every beat carries the
# number of fires in flight across ALL sessions on this host. A killed review therefore
# leaves behind both its time of death and the load at that instant.
#
# A SIDECAR, NOT THE MARKER ITSELF, AND THE REASON IS LOAD-BEARING: orphan_markers() ages
# a marker by its MTIME. Beating into the marker would refresh that mtime every interval,
# silently redefining "age" from time-since-start to time-since-death and shifting when a
# loss is allowed to surface. The existing detector is validated and measured; this
# instrument is strictly additive and must not perturb it. `*.beats` also falls outside
# that function's `*.json` glob, so it cannot be mistaken for a marker.
#
# THE THREAD IS A DAEMON AND EVERY OPERATION SWALLOWS ITS ERRORS. An instrument that can
# delay or break the hook it measures is worse than no instrument: this hook already sits
# in the path of every edit the agent makes.
BEAT_INTERVAL_S = 5.0


def _beats_path(marker: Path) -> Path:
    """Sidecar for a marker. Built by APPENDING, not Path.with_suffix -- a tool_use_id may
    contain dots (the sanitiser permits them), and with_suffix would replace from the last
    dot and could collide two markers onto one sidecar."""
    return Path(str(marker) + ".beats")


def count_inflight() -> int:
    """Fires in flight across EVERY session on this host, not just ours.

    Host-wide on purpose: the load that could kill a fire is produced by all sessions
    together, and this project routinely runs several at once. Counts `*.json` only, so
    already-reported `.json.reported` markers are excluded."""
    try:
        return sum(1 for _ in EVIDENCE_STATE_ROOT.glob(f"*/{PENDING_DIRNAME}/*.json"))
    except OSError:
        return -1          # unknown, and said so rather than reported as zero


def start_heartbeat(marker: Path | None) -> tuple[threading.Event, threading.Thread] | None:
    """Beat into `marker`'s sidecar until the returned Event is set. None if not started.

    Returns the THREAD as well as the Event because stopping has to be able to JOIN it --
    see stop_heartbeat. Each beat is one JSON line, flushed and fsync'd: a kill must not
    take the last beats with it, which is the same reason the marker itself is fsync'd."""
    if marker is None:
        return None
    stop = threading.Event()
    beats = _beats_path(marker)

    def _loop() -> None:
        started = datetime.now(timezone.utc)
        while True:
            # Checked immediately before the write, not only at the bottom of the loop: a
            # beat that lands after clear_pending_marker has unlinked the sidecar would
            # RECREATE it, leaving a stray file for a review that actually succeeded.
            if stop.is_set():
                return
            try:
                now = datetime.now(timezone.utc)
                rec = {
                    "t": now.isoformat(),
                    "elapsed_s": round((now - started).total_seconds(), 1),
                    "inflight": count_inflight(),
                    "pid": os.getpid(),
                }
                with open(beats, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass       # a failed beat must never end the review
            if stop.wait(BEAT_INTERVAL_S):
                return

    t = threading.Thread(target=_loop, name="council-heartbeat", daemon=True)
    try:
        t.start()
    except RuntimeError:
        return None        # cannot spawn a thread: proceed uninstrumented, never fail
    return stop, t


def stop_heartbeat(handle: tuple[threading.Event, threading.Thread] | None) -> None:
    """Stop beating and WAIT for the beat to actually stop. Idempotent.

    THE JOIN IS THE POINT, and the first version omitted it. Setting the Event without
    joining leaves the thread free to complete one more write AFTER the caller has deleted
    the sidecar, resurrecting a file for a review that succeeded. The thread sleeps on
    stop.wait(), so it wakes as soon as the Event is set rather than finishing its interval;
    the timeout exists only so a beat wedged in fsync on a stuck filesystem cannot hold the
    hook open. It stays a daemon so that a thread outliving that timeout still cannot keep
    the process alive -- which is a statement about PROCESS EXIT, not about the race below.

    THIS NARROWS THE RACE; IT DOES NOT ELIMINATE IT, and saying otherwise would be the
    overclaim the council caught in the previous draft. On the timeout path the thread is
    still alive, and even the top-of-loop stop.is_set() check can pass and then be
    preempted, so one write can still land after the unlink.
    WHY THAT IS TOLERABLE rather than merely admitted: a loss is identified by a MARKER, and
    the readers of this directory were AUDITED rather than assumed -- orphan_markers() below
    and count_inflight() above both glob `*.json`, stop_audit.py reads only through
    orphan_markers(), and the two suites match `*.json`/`*.reported`. (codex_hook.py's
    _pending_dirs() looks adjacent in a grep and is NOT a reader of this directory: it walks
    `<state>/pending`, a different tree of 64-hex snapshot keys.) So a sidecar whose marker
    is gone is litter, not a false loss -- it cannot make a completed review look lost,
    which is the only failure that would matter. If a future reader globs `*` here, that
    reader has to skip `.beats` itself, and this paragraph stops being true."""
    if handle is None:
        return
    stop, thread = handle
    stop.set()
    try:
        thread.join(timeout=2.0)
    except RuntimeError:
        pass


def clear_pending_marker(path: Path | None) -> None:
    """Mark this review COMPLETE by removing its marker and its beat sidecar. Idempotent
    and never raises.

    THE SIDECAR GOES TOO. Beats are diagnostic evidence about a review that DIED; for one
    that finished, the log entry is the record and a leftover sidecar would just accumulate
    a file per successful fire forever. A marker that gets ARCHIVED rather than cleared
    keeps its beats, which is the case the evidence is actually for."""
    if path is None:
        return
    for p in (path, _beats_path(path)):
        try:
            p.unlink()
        except OSError:
            pass


def archive_pending_marker(path: Path | None) -> None:
    """Retire a marker that has been REPORTED, without destroying the evidence of it.

    Renamed rather than deleted: the notice must fire once (a re-report on every later
    fire would be noise the agent learns to skip), but the record of which edits went
    unreviewed is the only durable trace of the loss and later analysis needs it. The
    suffix takes it out of orphan_markers' `*.json` glob, which is what makes it retired.
    """
    if path is None:
        return
    try:
        path.rename(path.with_suffix(".json.reported"))
    except OSError:
        pass


def orphan_markers(session_id: str, min_age_s: float = ORPHAN_MIN_AGE_S) -> list[dict]:
    """Markers old enough that no live fire could still own them: PROVEN lost reviews.

    Age is taken from the file's mtime rather than its `started` field, because a clock
    change would corrupt the second and not the first, and this is the one instrument that
    must not be able to invent a loss.
    """
    out: list[dict] = []
    d = _pending_dir(session_id)
    if not d.is_dir():
        return out
    now = datetime.now(timezone.utc).timestamp()
    try:
        entries = sorted(d.glob("*.json"))
    except OSError:
        return out
    for p in entries:
        try:
            age = now - p.stat().st_mtime
            if age < min_age_s:
                continue
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        rec["age_s"] = round(age)
        rec["marker_path"] = str(p)
        out.append(rec)
    return out


def format_orphan_notice(orphans: list[dict]) -> str:
    """The loud half. A lost review is reported as a LOSS, never as a pass."""
    n = len(orphans)
    lines = [
        f"COUNCIL REVIEWS LOST: {n} review{'' if n == 1 else 's'} in this session STARTED "
        f"AND NEVER FINISHED. The edits below were applied and are UNREVIEWED -- no "
        f"verdict exists for them, which is NOT the same as a passing one.",
    ]
    for o in orphans[:10]:
        lines.append(f"  - {o.get('tool_name')} on {o.get('target_path')} "
                     f"(started {o.get('started')}, {o.get('age_s')}s ago)")
    if n > 10:
        lines.append(f"  ... and {n - 10} more")
    lines.append("Re-review them explicitly, or treat them as unreviewed. This notice "
                 "fires even when the CURRENT review passed, because a silent PASS is "
                 "exactly what a lost review would otherwise look like.")
    return "\n".join(lines)


def emit_warning(text: str) -> int:
    """Exit 0 with structured JSON containing additionalContext."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")
    return 0


def escalation_hint(wrapper_stdout: str, session_id: str,
                    transcript_path: str = "") -> str:
    """Build a discoverable, Claude-initiated dialogue-escalation command
    from the wrapper's `# log: <path>` line. Returns '' if no log path is
    present. This hook does NOT open a thread itself; it only surfaces the
    command Claude can choose to run to discuss the verdict as a
    round-table dialogue.

    The transcript is passed through so an escalated dialogue carries the
    same Recent user directives block the one-shot review had; without it
    the dialogue members lose the standing instructions bar item 12 tells
    them to check against."""
    m = re.search(r"^# log:\s*(\S+)", wrapper_stdout or "", re.MULTILINE)
    if not m:
        return ""
    log_path = m.group(1)
    ev = ""
    if session_id:
        evf = EVIDENCE_STATE_ROOT / session_id / "evidence.jsonl"
        if evf.exists():
            ev = f" --evidence-file {evf}"
    tp = f" --transcript-path {transcript_path}" if transcript_path else ""
    return (
        "--- Optional: to discuss this with the council as a round-table "
        "dialogue (the members can question you; you answer with "
        "evidence), run:\n"
        f"  {DIALOGUE} escalate {log_path}{ev}{tp}\n"
        f"  {DIALOGUE} say <thread-id> \"<your reply>\"   (then resolve when converged)\n"
        "This is Claude-initiated and optional; otherwise revise, revert, "
        "or proceed as usual."
    )


def batch_probe(payload: dict, tool_input: dict) -> None:
    """NON-BLOCKING instrument, GATED on COUNCIL_ROOT/BATCH_PROBE. Records the turn and
    tool identifiers on each edit fire so offline analysis can TEST whether edits group
    into same-turn, same-file batches.

    VERIFIED PRESENT 2026-07-15 (batch_probe.jsonl, real hook payload): the PostToolUse
    payload carries `prompt_id` and `tool_use_id`. Only their PRESENCE is confirmed,
    not their semantics. Working hypothesis, to check once a real multi-edit turn is
    logged: `tool_use_id` is unique per edit call, and `prompt_id` is shared across the
    tool calls of one turn (the batch grouping key). IF that holds, a batch shows as
    >=2 records sharing one prompt_id and file -- detectable with no transcript
    reconstruction and no content-hash guessing (an earlier draft of this probe did
    both; the identifiers were in the payload all along, just unread by the advisor),
    their timestamps spaced ~one review apart because this hook blocks.

    Changes no review decision and does not touch the verdict or the file -- when
    enabled it only appends to batch_probe.jsonl. Every failure is swallowed: a probe
    must never break the review. Motivated by the 2026-07-15 coalescing discussion.
    """
    try:
        if not (COUNCIL_ROOT / "BATCH_PROBE").exists():
            return
        rec = {
            "at": datetime.now(timezone.utc).isoformat(),
            "session_id": payload.get("session_id") or "",
            "file": tool_input.get("file_path") or tool_input.get("notebook_path") or "",
            "prompt_id": payload.get("prompt_id"),      # presumed turn id (grouping unverified)
            "tool_use_id": payload.get("tool_use_id"),  # this edit call's id
        }
        with (COUNCIL_ROOT / "batch_probe.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001  a probe must never break the hook
        pass


def main() -> int:
    # Kill switch: `touch <council dir>/DISABLED` silences this hook;
    # `rm` re-enables it. Checked per call, so it works mid-session.
    if (COUNCIL_ROOT / "DISABLED").exists():
        return 0

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"council-advisor: invalid JSON on stdin: {e}", file=sys.stderr)
        return 1

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    # The Edit branch of build_pitch reads structuredPatch out of this to
    # locate the change; without it, no surrounding code is injected.
    tool_response = payload.get("tool_response", {}) or {}

    # Payload probe, off unless COUNCIL_ROOT/DEBUG_PAYLOAD exists. The hook is
    # the only place a REAL payload can be observed, and auto_revert() turns on
    # what `originalFile` actually contains for a new file vs an existing empty
    # one. Records the SHAPE (types, lengths, key names), never file contents.
    if (COUNCIL_ROOT / "DEBUG_PAYLOAD").exists():
        try:
            orig = tool_response.get("originalFile")
            patch = tool_response.get("structuredPatch")
            shape = {
                "tool": tool_name,
                "file": str(tool_input.get("file_path", "")),
                "tool_response_keys": sorted(tool_response.keys()),
                "originalFile_present": "originalFile" in tool_response,
                "originalFile_type": type(orig).__name__,
                "originalFile_len": len(orig) if isinstance(orig, str) else None,
                # `type` and `structuredPatch` are the fields that might tell a
                # CREATE from an UPDATE. A first pass at this probe recorded only
                # originalFile and concluded the two were indistinguishable --
                # while `type` sat unread in the very same payload.
                "type_value": tool_response.get("type"),
                "userModified": tool_response.get("userModified"),
                "patch_type": type(patch).__name__,
                "patch_len": len(patch) if isinstance(patch, list) else None,
                # KEYS of the first hunk, not the hunk: a hunk carries the
                # changed lines themselves, which is file content.
                "patch_hunk0_keys": (
                    sorted(patch[0].keys())
                    if isinstance(patch, list) and patch
                    and isinstance(patch[0], dict) else None),
            }
            with (COUNCIL_ROOT / "payload_shape.jsonl").open("a") as fh:
                fh.write(json.dumps(shape) + "\n")
        except Exception:  # noqa: BLE001  a probe must never break the hook
            pass

    if tool_name not in SUPPORTED_TOOLS:
        return 0  # matcher should prevent; be defensive

    # Measurement only: no-op unless COUNCIL_ROOT/BATCH_PROBE exists. When enabled it
    # reads the transcript tail and appends to batch_probe.jsonl, but it makes no
    # review decision and does not touch the verdict or the file -- behaviour-neutral.
    batch_probe(payload, tool_input)

    if not WRAPPER.exists():
        print(f"council-advisor: wrapper missing at {WRAPPER}", file=sys.stderr)
        return 1

    pitch = build_pitch(tool_name, tool_input, tool_response)
    target_for_log = target_path_for_log(tool_name, tool_input)
    cwd = payload.get("cwd") or "."
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""

    cmd = [
        sys.executable, str(WRAPPER),
        "--layer", "posttool",
        "--tool-name", tool_name,
        "--target-path", target_for_log,
        # The containment jail for member REQUEST_FILE retrieval. Passed
        # explicitly rather than inherited through the subprocess cwd, so the
        # engine's jail root never depends on an implicit contract.
        "--workdir", cwd,
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
        evidence_file = EVIDENCE_STATE_ROOT / session_id / "evidence.jsonl"
        if evidence_file.exists():
            cmd.extend(["--evidence-file", str(evidence_file)])
    if transcript_path:
        cmd.extend(["--transcript-path", transcript_path])
    # Collected BEFORE this fire's own marker is written, so a fire can never appear in
    # its own orphan report.
    prior_orphans = orphan_markers(session_id)
    marker = write_pending_marker(session_id, tool_name, target_for_log,
                                  payload.get("tool_use_id") or "")
    # Beats start BEFORE the subprocess and stop on every route out, exactly mirroring the
    # marker. If this fire is killed, the sidecar's last line dates the death and records
    # how many fires were in flight at that moment -- the two things a bare marker cannot
    # say and that every hypothesis about the cause needs.
    beat = start_heartbeat(marker)
    try:
        proc = subprocess.run(
            cmd,
            input=pitch,
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        # A timeout COMPLETED the attempt and says so to the agent below, so it is not a
        # silent loss and must not leave a marker behind.
        stop_heartbeat(beat)
        clear_pending_marker(marker)
        return emit_warning(
            f"Council timed out (>900s) reviewing {tool_name} on "
            f"{target_for_log}. The action already proceeded; this notice "
            f"is just to flag that the council did not complete."
        )
    stop_heartbeat(beat)
    clear_pending_marker(marker)

    wrapper_stdout = proc.stdout
    wrapper_stderr = proc.stderr
    rc = proc.returncode

    if rc == 0:
        # Wrapper returned PASS. Silent -- EXCEPT when earlier reviews in this session
        # were lost, which is the one case where silence is the bug rather than the
        # design. A lost review and a passing one are indistinguishable from the agent's
        # side, so the loss is reported on the next fire that finishes, and the markers
        # are archived rather than deleted so the notice fires ONCE and the evidence
        # still survives for later analysis.
        if prior_orphans:
            # EMIT FIRST, ARCHIVE SECOND, AND FLUSH BETWEEN THEM. Archiving is what takes
            # a marker out of orphan_markers, so doing it first means a kill in this
            # window retires the evidence for a notice that was never delivered -- the
            # loss then becomes permanently invisible to both this path and stop_audit.
            # That is strictly worse than no instrument at all. A layer-2 inspector caught
            # this exact inversion here after the council caught its twin in stop_audit.
            rc_out = emit_warning(format_orphan_notice(prior_orphans))
            try:
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
            for o in prior_orphans:
                archive_pending_marker(Path(o["marker_path"]))
            return rc_out
        return 0

    if rc == 1:
        # WARN: surface the wrapper's output to Claude, plus a discoverable
        # (Claude-initiated) escalation command. Nothing is auto-created.
        text_parts = []
        if wrapper_stdout:
            text_parts.append(wrapper_stdout.rstrip("\n"))
        if wrapper_stderr:
            text_parts.append(wrapper_stderr.rstrip("\n"))
        hint = escalation_hint(wrapper_stdout, session_id, transcript_path)
        if hint:
            text_parts.append(hint)
        return emit_warning("\n\n".join(text_parts))

    if rc == 2:
        # BLOCK from the council wrapper signals rule 11 (caveat without
        # probe). Per council_system_prompt.md rule 11, the expected
        # response is to revert the change just made, run the probe the
        # bar item requires, then re-attempt with the probe in evidence.
        # Exit 2 routes this to Claude via stderr: the Claude Code hooks
        # documentation (mirrored locally in the plugin-dev
        # hook-development SKILL.md) documents exit code 2 as a blocking
        # error whose stderr is fed back to Claude.
        # ENFORCE the block. Until now this only ASKED Claude to revert, and the
        # ask was ignorable; the write stayed on disk either way.
        revert_note = ""
        try:
            revert_note = auto_revert(tool_input, tool_response)
        except Exception as e:  # noqa: BLE001
            # A crash in the revert must not swallow the BLOCK itself.
            revert_note = (f"AUTO-REVERT CRASHED ({e!r}). The write STANDS and "
                           f"was NOT undone. Revert it yourself.")

        text_parts = [
            "COUNCIL BLOCK: rule 11 violation (caveat phrase without "
            "probe evidence). Per council_system_prompt.md rule 11, "
            "run the probe the bar requires, then re-attempt with the "
            "probe in the session evidence."
        ]
        if revert_note:
            text_parts.append(revert_note)
        if wrapper_stdout:
            text_parts.append(wrapper_stdout.rstrip("\n"))
        if wrapper_stderr:
            text_parts.append(wrapper_stderr.rstrip("\n"))
        hint = escalation_hint(wrapper_stdout, session_id, transcript_path)
        if hint:
            text_parts.append(hint)
        sys.stderr.write("\n\n".join(text_parts) + "\n")
        return 2

    # Wrapper hit a setup or internal error. Surface as a softer notice.
    return emit_warning(
        f"Council wrapper exited with code {rc} for {tool_name} on "
        f"{target_for_log}. The action already proceeded; the wrapper "
        f"did not complete a clean review.\n\n"
        f"Wrapper stdout:\n{wrapper_stdout}\n\n"
        f"Wrapper stderr:\n{wrapper_stderr}"
    )


if __name__ == "__main__":
    sys.exit(main())
