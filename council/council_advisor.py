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
import uuid
from datetime import datetime, timezone
from pathlib import Path

COUNCIL_ROOT = Path(__file__).resolve().parent
WRAPPER = COUNCIL_ROOT / "consult_council.py"
DIALOGUE = COUNCIL_ROOT / "council_dialogue.py"
EVIDENCE_STATE_ROOT = Path.home() / ".claude" / "state"

# --- Auto-revert on BLOCK ---------------------------------------------------
#
# Enabled at Dakotah's explicit instruction ("let's go ahead and do the auto
# revert idea"), after the measurement he asked for first.
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
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
        evidence_file = EVIDENCE_STATE_ROOT / session_id / "evidence.jsonl"
        if evidence_file.exists():
            cmd.extend(["--evidence-file", str(evidence_file)])
    if transcript_path:
        cmd.extend(["--transcript-path", transcript_path])
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
        return emit_warning(
            f"Council timed out (>900s) reviewing {tool_name} on "
            f"{target_for_log}. The action already proceeded; this notice "
            f"is just to flag that the council did not complete."
        )

    wrapper_stdout = proc.stdout
    wrapper_stderr = proc.stderr
    rc = proc.returncode

    if rc == 0:
        # Wrapper returned PASS. Stay silent; nothing surfaces to Claude.
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
