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

# HOW LONG THIS WRAPPER LETS A FIRE RUN. ONE constant, used BOTH as the subprocess timeout
# and as the orphan age below, because those two are the same fact. They were previously two
# literals, and the old comment on ORPHAN_MIN_AGE_S asserted "900s matches the wrapper
# timeout above" -- exactly the sentence a later edit to one literal silently falsifies.
# Deriving one from the other removes the possibility rather than documenting it.
#
# WHY 1500 AND NOT 900. The engine's per-member cap is PER_CRITIC_TIMEOUT_S = 600
# (consult_council.py:310) and its phases are SEQUENTIAL -- voting round 1, then round 2,
# then the inspector pass -- so a fire behaving exactly as designed can need up to 1800s. A
# 900s wrapper cap therefore killed fires at half their sanctioned budget.
# MEASURED over the full-depth corpus, re-run against logs/ when this line was written.
# floor = the sum over phases of that phase's slowest usable member, which is a LOWER bound
# on a fire because it excludes the doorman, dialogue, prompt assembly and I/O:
#     n = 1533 fires with a rankable floor
#     >600s: 55    >900s: 12    >1200s: 4    >1500s: 0    max floor 1445.8s
# So the 900s cap was demonstrably cutting into real fires, and 1500 clears every floor in
# the corpus. WHAT THAT DOES NOT ESTABLISH: that 1500 is sufficient in general -- the sample
# is right-censored BY the old 900s cap, so fires that would have run longer were killed
# before they could be measured, and the salvage path below exists precisely because a cap
# can still be hit.
#
# 1500 SITS BETWEEN TWO WALLS AND MUST STAY THERE. Below: PER_CRITIC_TIMEOUT_S x 3 = 1800 is
# what the engine may legitimately want. Above: the Claude Code hook `timeout` is 1800
# (claude-code/settings.hooks.template.json), and this wrapper needs headroom UNDER that wall
# to notice its own timeout and still deliver a report -- a cap of 1800 here would be
# cancelled by the harness mid-report and salvage nothing.
FIRE_TIMEOUT_S = 1500

# An orphan younger than this may simply be a fire still running in a concurrent hook --
# concurrency SERIALIZES fires rather than dropping them (measured: 4 concurrent advisors
# all logged, but wall time went 81s -> 326s), so a live sibling is expected and must not
# be reported as a loss. Past FIRE_TIMEOUT_S no live fire of OURS can still be running,
# because subprocess.run would already have raised.
ORPHAN_MIN_AGE_S = FIRE_TIMEOUT_S


def _pending_dir(session_id: str) -> Path:
    """Directory holding this session's in-flight review markers.

    Sessions are kept apart so one session's reconciliation never reports another's live
    fire as a loss -- with parallel sessions the norm on this machine, a shared directory
    would make every concurrent fire look like an orphan to whichever session looked first.
    """
    return EVIDENCE_STATE_ROOT / (session_id or "_no_session") / PENDING_DIRNAME


def write_pending_marker(session_id: str, tool_name: str, target: str,
                         tool_use_id: str = "", cwd: str = "") -> Path | None:
    """Record that a review is STARTING. Returns the marker path, or None if it could not
    be written -- never raises, because failing to instrument a review must not also
    prevent it.

    `cwd` IS FOR NAMING THE SESSION TO A HUMAN. A watcher showing several concurrent fires
    has to label the rows, and a session id is a 36-character hash that identifies nothing to
    a reader. The working directory is what an operator actually recognises. It is recorded
    here rather than derived from `target` because a target path names the FILE being edited,
    whose parent directory is often a subdirectory several levels below the tree the session
    is working in.
    """
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
            # Absent on markers written before this field existed; a reader must treat a
            # missing value as UNKNOWN and fall back to the session id rather than to "".
            "cwd": cwd,
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


def _events_path(marker: Path) -> Path:
    """Sidecar holding the engine's NDJSON progress stream for this fire.

    APPENDED, not Path.with_suffix, for the same reason as _beats_path above: a tool_use_id
    may contain dots and with_suffix would replace from the last one, collapsing two markers
    onto one sidecar.

    IT MUST NOT LOOK LIKE A MARKER. Both readers of this directory glob `*.json` --
    count_inflight() at the module level and orphan_markers() below -- and a name ending
    `.json.events` does not match that pattern, while a real `<id>.json` marker still does.
    Re-checked with fnmatch against both patterns and a positive control when this was added,
    because a third sidecar that DID match would inflate the in-flight count and manufacture
    phantom lost reviews."""
    return Path(str(marker) + ".events")


def marker_is_live(marker: Path) -> bool:
    """True when this marker could still belong to a RUNNING fire.

    IT LIVES HERE BECAUSE THIS MODULE OWNS THE MARKERS. Two readers need the rule -- the
    statusline and council_watch's --follow -- and a copy in each would be a third definition
    of "live" free to drift from the writers above.

    A MARKER'S PRESENCE IS NOT LIVENESS. A marker is removed when its fire returns by any
    route, so a surviving one is either a fire still running or a fire that died without
    cleaning up, and only evidence beyond existence separates them. Two gates, cheapest
    first:
      AGE, from mtime, which keeps meaning "when this started" because the heartbeat beats
      into a sidecar rather than the marker. Past FIRE_TIMEOUT_S the subprocess.run that owns
      the fire would already have raised, so nothing of ours is still under it.
      THE PID, `os.getpid()` recorded by write_pending_marker above, i.e. this process. If
      that pid is gone the fire is not running, whatever the age says.

    WHAT IT DOES NOT ESTABLISH: that the fire IS running. A pid can be REUSED, and a live
    advisor is not proof its engine still works. The claim is bounded to "could be", which is
    what a progress view needs. The asymmetry is why the pid gate earns its place: reuse can
    only manufacture a false LIVE, and only inside the age window that already gated it,
    whereas dropping the gate lets every marker that outlives its fire read as live for the
    whole of FIRE_TIMEOUT_S.

    ACCEPTS BOTH PID FIELD NAMES: the review marker written above carries
    `started_monotonic_pid`; the tier-0 gate's doorman marker carries `pid`. One rule, two
    writers.
    """
    try:
        if (datetime.now(timezone.utc).timestamp() - marker.stat().st_mtime
                >= FIRE_TIMEOUT_S):
            return False
    except OSError:
        return False
    try:
        rec = json.loads(marker.read_text())
        pid = rec.get("started_monotonic_pid", rec.get("pid"))
    except (OSError, ValueError):
        return True      # unreadable mid-write: the age gate above already passed
    if not isinstance(pid, int):
        return True      # predates the pid field; age is all there is
    return Path(f"/proc/{pid}").exists()


def clear_pending_marker(path: Path | None) -> None:
    """Mark this review COMPLETE by removing its marker and its sidecars. Idempotent
    and never raises.

    THE SIDECARS GO TOO. Beats and events are diagnostic evidence about a review that DIED;
    for one that finished, the log entry is the record and leftover sidecars would just
    accumulate files per successful fire forever. A marker that gets ARCHIVED rather than
    cleared keeps them, which is the case the evidence is actually for.

    CONSTRAINT ON ANY CALLER THAT WANTS THE EVENTS SIDECAR: take it FIRST. On a route where
    the engine produced no log -- a killed fire -- that sidecar is the only surviving record
    of the seat-rounds that did finish, and this function deletes it like any other. Copy or
    rename it before calling, or it is gone."""
    if path is None:
        return
    for p in (path, _beats_path(path), _events_path(path)):
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


# --- SALVAGE: what survives when a fire is killed before it can report -------------
#
# THE LOSS THIS ADDRESSES. When this wrapper's timeout fires, subprocess.run kills the
# engine, and the engine writes its log entry only at the very end -- so the whole review
# goes, including rounds that had already finished. Re-measured against logs/ when this was
# written, for the two fires that reported timing out on 2026-08-11: emit_tty.sh has 0 log
# entries, and member_timing.py has 4, all of which carry a final_verdict and so completed.
# WHAT THAT DOES NOT ESTABLISH: that the kill CAUSED the absence. No cancellation was
# observed; what is established is that a fire reported as timed out left no log behind.
#
# WHAT MAKES RECOVERY POSSIBLE is that the engine streams NDJSON progress records to
# --events-fd as each seat lands, and every member_finished record carries that seat's
# verdict AND its member_text. So the completed seat-rounds are already on disk when the
# kill arrives; nothing new has to be computed, only read.
PARTIAL_STORE = COUNCIL_ROOT / "partials.jsonl"

# Round numbers as the engine emits them. Verified against the four _seat call sites in
# consult_council.py rather than assumed: 7218 voting/1, 7279 voting/2, 7355 inspector/3,
# 7383 inspector/4 -- and pass 2 is gated on `if requesters:` (7377), so round 4 covers only
# the inspectors that asked for tooling, which is why a missing round 4 is normal.
_ROUND_LABELS = {1: "voting r1", 2: "voting r2", 3: "inspectors", 4: "inspectors pass2"}


def _int(value: object) -> int:
    """Best-effort int, never raising.

    ONE DAMAGED RECORD MUST NOT DEFEAT THE SALVAGE. read_events already tolerates a torn
    line; it would be pointless for the reducer to then die on a record whose `round` is a
    string, a dict, or absent. `int(x or 0)` -- the first version of this -- raises on all
    three, so a single malformed event could have cost the entire partial review. 0 is
    returned instead, which lands the record under an unknown round and keeps its verdict.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def read_events(path: Path) -> list[dict]:
    """Parse an events sidecar into records, tolerating a truncated tail.

    A KILLED FIRE'S LAST LINE MAY BE HALF-WRITTEN, which is exactly the case this has to
    survive: the emitter writes whole records but the process can die mid-write, so the final
    line may be incomplete JSON. Each line is parsed independently and unparseable ones are
    SKIPPED rather than aborting the read -- keeping every intact record is the whole point,
    and a parser that gave up at the first bad line would return nothing at all.
    """
    out: list[dict] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue          # truncated tail, or a torn write; skip it, keep the rest
        if isinstance(rec, dict):
            out.append(rec)
    return out


def summarise_partial(records: list[dict]) -> dict:
    """Reduce a fire's event stream to what a partial report needs.

    Returns counts per round, the seats still outstanding, and each finished seat's LATEST
    verdict and text. "Latest" matters: a voting seat appears in round 1 and again in round
    2, and round 2 is the one the council would have aggregated, so a later record for the
    same seat supersedes an earlier one.

    `seat_rounds` KEEPS WHAT `seats` COLLAPSES: {round: {member: record}}, so a caller can
    show round 1 and round 2 side by side instead of only the survivor. `seats` cannot serve
    that -- its latest-wins overwrite is exactly what format_partial needs (the aggregated
    verdict) and exactly what a per-round view must not have. Both are returned because the
    two consumers want opposite things from the same stream, and deriving one from the other
    after the fact is impossible in the collapsing direction.
    """
    expected: dict[str, list[str]] = {"voting": [], "inspector": []}
    started: dict[int, set[str]] = {}
    finished: dict[int, set[str]] = {}
    seats: dict[str, dict] = {}
    seat_rounds: dict[int, dict[str, dict]] = {}
    corrected: list[dict] = []
    for rec in records:
        ev = rec.get("ev")
        if ev == "run_started":
            for key, field in (("voting", "voting"), ("inspector", "inspectors")):
                names = rec.get(field)
                if isinstance(names, list):
                    expected[key] = [str(n) for n in names]
        elif ev == "member_started":
            started.setdefault(_int(rec.get("round")), set()).add(str(rec.get("member")))
        elif ev == "member_finished":
            rnd = _int(rec.get("round"))
            finished.setdefault(rnd, set()).add(str(rec.get("member")))
            entry = {
                "round": rnd,
                "tier": rec.get("tier"),
                "verdict": rec.get("verdict"),
                "text": rec.get("member_text"),
                "duration_s": rec.get("duration_s"),
            }
            seats[str(rec.get("member"))] = entry
            # A COPY, not the same object. `seats` is latest-wins, so for a two-round seat
            # seats[m] would otherwise BE round 2's entry here, and the correction branch's
            # `seats[m]["verdict"] = ...` would mutate the per-round record through the alias.
            # Corrections DO reach seat_rounds -- deliberately, below -- and the copy is what
            # makes that an addressed write to a chosen round rather than an aliasing accident.
            seat_rounds.setdefault(rnd, {})[str(rec.get("member"))] = dict(entry)
        elif ev == "member_corrected":
            # A seat's verdict CHANGED after it was first reported. Carried through so a
            # partial never quotes a verdict the council itself would have superseded.
            corrected.append(rec)
            m = str(rec.get("member"))
            if m in seats:
                seats[m]["verdict"] = rec.get("verdict")
                seats[m]["corrected_from"] = rec.get("was")
            # Apply to the round the correction names, or to the seat's latest round when the
            # record does not say -- a correction with no round would otherwise vanish from
            # the per-round view while showing up in the aggregate, so the two would disagree.
            rnd = _int(rec.get("round"))
            if rnd not in seat_rounds or m not in seat_rounds.get(rnd, {}):
                cand = [r for r, members in seat_rounds.items() if m in members]
                rnd = max(cand) if cand else rnd
            if m in seat_rounds.get(rnd, {}):
                seat_rounds[rnd][m]["verdict"] = rec.get("verdict")
                seat_rounds[rnd][m]["corrected_from"] = rec.get("was")
    return {"expected": expected, "started": started, "finished": finished,
            "seats": seats, "seat_rounds": seat_rounds, "corrected": corrected}


def format_partial(summary: dict, tool_name: str, target: str, elapsed_s: int) -> str:
    """Render the partial review for the agent.

    THREE THINGS THIS MUST NOT DO, each of which would turn a salvage into a hazard.
    (1) It must never read as a PASS. A partial is not a clean bill of health, and the whole
        pending-marker mechanism exists because absence of a verdict reads as approval.
    (2) It must name WHO IS MISSING, not just how many, because which seats are absent is
        the information that tells a reader how much the surviving verdicts are worth.
    (3) It must state the SELECTION BIAS, which is measured rather than supposed: over the
        full-depth corpus (n=1543 voting rounds of each kind), kimi and deepseek together
        were the slowest seat in 79.3% of round 1s and 81.0% of round 2s. The seats that
        survive a timeout are therefore the fast ones, so a partial that looks unanimous may
        simply be missing the seats most likely to dissent.
    """
    exp = summary["expected"]
    fin = summary["finished"]
    seats = summary["seats"]
    lines = [
        f"COUNCIL PARTIAL REVIEW -- the fire was cut off at {elapsed_s}s reviewing "
        f"{tool_name} on {target}. THIS IS NOT A COMPLETE REVIEW AND NOT A PASS.",
        "",
        "COMPLETED SEAT-ROUNDS:",
    ]
    for rnd in (1, 2, 3, 4):
        done = sorted(fin.get(rnd, set()))
        if not done and rnd == 4:
            continue          # pass 2 runs only for inspectors that requested tooling
        total = len(exp["inspector"] if rnd in (3, 4) else exp["voting"])
        lines.append(f"  {_ROUND_LABELS[rnd]:<16} {len(done)}/{total or '?'}"
                     + (f"  ({', '.join(done)})" if done else ""))
    missing_v = [m for m in exp["voting"] if m not in fin.get(2, set())]
    missing_i = [m for m in exp["inspector"] if m not in fin.get(3, set())]
    if missing_v or missing_i:
        lines += ["", "NEVER REPORTED (their round did not finish):"]
        if missing_v:
            lines.append(f"  voting round 2: {', '.join(missing_v)}")
        if missing_i:
            lines.append(f"  inspectors:     {', '.join(missing_i)}")
    lines += [
        "",
        "HOW MUCH THIS IS WORTH: the seats that finish first are systematically the FAST "
        "ones. Measured over the full-depth corpus, kimi and deepseek together were the "
        "slowest voter in 79.3% of round 1s and 81.0% of round 2s (n=1543 each). A partial "
        "that looks unanimous may simply be missing the seats most likely to have "
        "dissented. Treat what follows as evidence, never as a verdict.",
    ]
    if summary["corrected"]:
        lines.append(f"NOTE: {len(summary['corrected'])} seat(s) had a verdict CORRECTED "
                     f"after first reporting; the corrected value is shown.")
    blocked = [m for m, s in seats.items() if str(s.get("verdict")).upper() == "BLOCK"]
    if blocked:
        # WHY THE DOWNGRADE IS EXPLAINED PER SEAT AND NOT ONCE. The first version of this
        # said a BLOCK "never got a round 2" -- which is false whenever the fire died during
        # the INSPECTOR phase, since voting round 2 had already completed by then. Two
        # different situations, two different reasons, so the seat's own round decides which
        # sentence it gets.
        pre_r2 = sorted(m for m in blocked if _int(seats[m].get("round")) < 2)
        post_r2 = sorted(m for m in blocked if _int(seats[m].get("round")) >= 2)
        lines += ["", f"A SEAT SAID BLOCK: {', '.join(sorted(blocked))}. Reported here as a "
                      f"WARN; it does NOT trigger the revert protocol."]
        if pre_r2:
            lines.append(
                f"  {', '.join(pre_r2)}: blocked in round 1, and this fire never reached the "
                f"round 2 where peers confirm a BLOCK or talk it down. The seat has not yet "
                f"been tested against anyone.")
        if post_r2:
            lines.append(
                f"  {', '.join(post_r2)}: blocked in a round its peers DID see, so this one "
                f"is not untested -- it is downgraded only because the panel is incomplete "
                f"and the aggregate verdict was never computed.")
        lines.append("  Either way: read the reasoning below and decide deliberately rather "
                     "than treating the downgrade as a dismissal.")
    if seats:
        lines += ["", "VERDICTS FROM SEATS THAT FINISHED (latest round each reached):"]
        for name in sorted(seats):
            s = seats[name]
            lines.append(f"  {name:<10} {str(s.get('verdict')):<12} "
                         f"({_ROUND_LABELS.get(s.get('round'), '?')}, "
                         f"{s.get('duration_s')}s)")
        for name in sorted(seats):
            body = (seats[name].get("text") or "").strip()
            if body:
                lines += ["", f"## {name} (partial, {seats[name].get('verdict')})", body]
    return "\n".join(lines)


def record_partial(summary: dict, tool_name: str, target: str, session_id: str,
                   elapsed_s: int) -> None:
    """Append a partial fire to its OWN store, never to logs/.

    DELIBERATELY NOT logs/. Every existing consumer -- council_outcome's cohorts, the GUI's
    depth audit, any clean-rate computation -- globs logs/*/*.json and assumes each entry is
    a COMPLETED review. Dropping partials in there would make them count as fires, and a
    consumer that predates the flag would read a missing `partial` key as False, which is
    precisely the launder-a-weaker-look-as-full-strength move this project exists to stop.
    A separate store keeps the corpus meaning what it already meant while making the timeout
    rate computable for the first time. Never raises: salvage must not fail the hook."""
    try:
        rec = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "tool_name": tool_name,
            "target_path": target,
            "elapsed_s": elapsed_s,
            "partial": True,
            "completed": {str(k): sorted(v) for k, v in summary["finished"].items()},
            "expected": summary["expected"],
            "verdicts": {m: s.get("verdict") for m, s in summary["seats"].items()},
        }
        with PARTIAL_STORE.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001  salvage must never break the hook
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
    # PASSED SO THE LOG CAN BE PAIRED WITH THIS EXACT EDIT. The marker is already named after
    # this id; recording it in the log too is what lets a reader join the two without guessing
    # from timestamps, since session + tool + target is identical across every fire aimed at
    # one file. Sent independently of session_id: the two identify different things, and an
    # unusual payload carrying one without the other should still record what it has.
    if payload.get("tool_use_id"):
        cmd.extend(["--tool-use-id", str(payload.get("tool_use_id"))])
    if transcript_path:
        cmd.extend(["--transcript-path", transcript_path])
    # Collected BEFORE this fire's own marker is written, so a fire can never appear in
    # its own orphan report.
    prior_orphans = orphan_markers(session_id)
    marker = write_pending_marker(session_id, tool_name, target_for_log,
                                  payload.get("tool_use_id") or "", cwd)
    # Beats start BEFORE the subprocess and stop on every route out, exactly mirroring the
    # marker. If this fire is killed, the sidecar's last line dates the death and records
    # how many fires were in flight at that moment -- the two things a bare marker cannot
    # say and that every hypothesis about the cause needs.
    # THE EVENTS SIDECAR, opened BEFORE the fire so the engine can stream into it as each
    # seat lands. Without a marker there is nowhere to put it (the pending dir is what could
    # not be written), and salvage degrades to the bare timeout notice rather than failing.
    #
    # A FILE, because the records must OUTLIVE THE PROCESS THAT WROTE THEM. That is the
    # whole requirement here and it is enough on its own: this hook has no live reader, and
    # whatever the engine streamed has to still be on disk after the kill.
    events_file = _events_path(marker) if marker is not None else None
    events_fd = None
    if events_file is not None:
        try:
            events_fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            events_fd = None          # instrumenting a review must never prevent it
    if events_fd is not None:
        cmd.extend(["--events-fd", str(events_fd)])
    beat = start_heartbeat(marker)
    try:
        proc = subprocess.run(
            cmd,
            input=pitch,
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=FIRE_TIMEOUT_S,
            # pass_fds keeps the descriptor open ACROSS the fork and clears FD_CLOEXEC on
            # it, which is what makes the number in --events-fd mean the same thing in the
            # child. MEASURED with a discriminating probe rather than assumed, because the
            # whole salvage rests on it: a child handed this fd number wrote its record and
            # the sidecar grew to 25 bytes WITH pass_fds, and raised OSError errno 9 (EBADF)
            # leaving the sidecar at 0 bytes WITHOUT it.
            # NOT silent, to be exact: the engine checks the fd's access mode up front and
            # prints "--events-fd N unusable ... continuing without progress events" on
            # stderr, then runs the review anyway. So a broken fd costs the salvage, not the
            # review -- but it is announced, and an operator who reads stderr will see it.
            pass_fds=(events_fd,) if events_fd is not None else (),
        )
    except subprocess.TimeoutExpired:
        # A timeout COMPLETED the attempt and says so to the agent below, so it is not a
        # silent loss and must not leave a marker behind.
        # The duration is INTERPOLATED, never spelled out: this sentence used to read
        # ">900s" as a literal, which is the same drift hazard the constant above exists to
        # remove -- a cap change would have left the notice quoting the old number.
        stop_heartbeat(beat)
        # SALVAGE BEFORE CLEARING, because clear_pending_marker deletes this sidecar along
        # with the marker, and on a timed-out fire there may be nothing else.
        # WHAT IS VERIFIED, and only this: an UNTRUNCATED grep for write_log across the
        # engine returns 7 lines -- the definition (6579), five mentions in comments, and
        # exactly ONE executable call (7431), which sits below the four seat-round calls in
        # the same function. (The first version of this check was piped through `head`, so
        # it could not have shown a call site it truncated; that is the pipeline-ate-the-
        # verdict trap, caught by re-running without it.) Grep gives textual position, not
        # an execution trace, so the claim stays narrow: a process killed before reaching
        # that single call writes no logs/ entry.
        # THE CORPUS AGREES, stated carefully because the raw counts invite a misreading:
        # of the two fires that reported timing out on 2026-08-11, emit_tty.sh has 0 log
        # entries, and member_timing.py has 4 -- but all 4 carry a final_verdict, i.e. they
        # are the fires on that file which COMPLETED, not the one that timed out. Neither
        # timed-out fire left an entry. The CAUSE of the absence remains unobserved.
        salvaged = ""
        if events_fd is not None:
            try:
                os.close(events_fd)
            except OSError:
                pass
            events_fd = None
        if events_file is not None:
            summary = summarise_partial(read_events(events_file))
            if summary["seats"]:
                salvaged = format_partial(summary, tool_name, target_for_log,
                                          FIRE_TIMEOUT_S)
                record_partial(summary, tool_name, target_for_log, session_id,
                               FIRE_TIMEOUT_S)
        clear_pending_marker(marker)
        if salvaged:
            return emit_warning(salvaged)
        return emit_warning(
            f"Council timed out (>{FIRE_TIMEOUT_S}s) reviewing {tool_name} on "
            f"{target_for_log}. The action already proceeded; this notice "
            f"is just to flag that the council did not complete. NOTHING WAS SALVAGED: no "
            f"completed seat-round could be RECOVERED, which is not the same as none "
            f"having happened -- the sidecar may have been unwritable, empty, or damaged. "
            f"Either way there is no partial result to report, and this is silence about "
            f"the edit rather than approval of it."
        )
    finally:
        if events_fd is not None:
            try:
                os.close(events_fd)
            except OSError:
                pass
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
