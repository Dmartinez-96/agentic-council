#!/usr/bin/env python3
"""Detect FORWARD REFERENCES: prose that names a code symbol the file does not have.

THE FAILURE THIS TARGETS. A comment or docstring says `foo()` / `BAR` / "the new
`baz` flag" while the code still does the old thing, or while that symbol was just
deleted or renamed. The file then LIES to every later reader. Two failure-mode
notes in the vault record this happening repeatedly; the bench asked for a
CHECKABLE detector to pair with them, because a testimony note about a habit
cannot itself catch a recurrence.

WHAT IT DOES. For one Python file: collect the names the module binds, then pull
backtick-quoted BARE IDENTIFIERS out of comments and docstrings and report the
ones that resolve to nothing.

WHAT COUNTS AS BOUND, stated exactly because the bias direction matters:
  - MODULE-LEVEL def / class / assignment / annotated assignment.
  - imports at ANY depth (a function-local import still makes the name real).
  - builtins and keywords.
  - anything passed via --known.
NESTED defs and classes are deliberately NOT counted, and neither are function
locals. Prose naming them IS reported. That is a FALSE POSITIVE and it is the
direction chosen on purpose: over-reporting is visible and costs a reader ten
seconds, under-reporting is silent and is the entire failure being hunted.

WHAT IT DELIBERATELY DOES NOT DO, because a noisy detector gets ignored:
  - DOTTED names (`mod.attr`, `obj.method`) are not matched at all.
  - Cross-module references are not resolved. A file may legitimately discuss a
    symbol living elsewhere -- `validate_brain.py` naming a function from the
    engine is correct prose, not a forward reference. Use --known for those.
So on a REAL tree this is ADVISORY. Its discriminating use is on constructed
fixtures where the right answer is known, which is what the paired brain note
checks.

FALSIFIER for the tool itself: plant a comment naming a symbol that does not
exist and it must be reported; plant one naming a symbol that DOES exist and it
must not be. BOTH directions are required -- a detector that flags everything
passes the first and is worthless.

EXIT STATUS is three-valued so that a passing status cannot be vacuous:
    0  every requested file was scanned, nothing reported
    1  every requested file was scanned, at least one candidate reported
    2  usage error, OR a file could not be read/parsed and was skipped
A skipped file is status 2 rather than 0 deliberately. An earlier version printed
SKIPPED and still returned 0, so a check asserting exit 0 would have passed on a
fixture that never got scanned -- a void check inside the tool built to catch
exactly that class of mistake.

NOISE, MEASURED, because "advisory" without a number is a dodge. Over the six engine
files (consult_council, council_leader, council_dialogue, council_advisor,
sync_to_package and this file) the default run reports ~140 candidates. Almost all are
the documented false-positive classes: JSON field names, shell words (`cp`, `rm`), dict
keys, CLI subcommands, and plain English that happens to be backticked. So THIS IS NOT
USABLE AS A REPO-WIDE GATE, and it is not wired into one.
--symbolic cuts that to ~34 on the same files by keeping only snake_case and ALL_CAPS
tokens. That is a real improvement and still not a gate: the survivors include many
legitimate CROSS-MODULE names (`cached_tokens` is an OpenRouter response field,
`exit_status` a brain-note schema key), which this tool cannot resolve by design. Treat
--symbolic output as a review list, not a verdict. Re-measure rather than quoting these
two figures -- both move as the files do.

Run:  python3 forward_refs.py <file.py> [...] [--known NAME,NAME] [--symbolic]
"""
import ast
import builtins
import io
import keyword
import re
import sys
import tokenize
from pathlib import Path

# A backticked token that looks like a bare Python identifier, optionally written
# as a call. Anything containing a dot, space, slash or dash is left alone.
_TICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`")


def bound_names(tree: ast.AST) -> set[str]:
    """Names the module binds. See the module docstring for the exact policy."""
    names: set[str] = set()
    # Imports at ANY depth: a function-local import still makes the name real.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    # Definitions and assignments at MODULE LEVEL ONLY. Using ast.walk here would
    # count nested defs as bound and thereby SUPPRESS reports -- under-reporting,
    # which is the silent direction and the opposite of what this tool is for.
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    # ctx=Store, not merely "is a Name". In `obj.attr = 1` the Name
                    # `obj` is a LOAD, and in `a[i] = 1` both `a` and `i` are; adding
                    # them would mark names as bound that the statement does not
                    # bind, suppressing reports -- the silent direction this tool
                    # exists to avoid.
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def prose_spans(src: str, tree: ast.AST) -> list[tuple[int, str]]:
    """(line, text) for every comment and docstring.

    Comments come from `tokenize`, not from a regex for '#'. A regex would treat a
    '#' inside a string literal as the start of a comment and invent prose that is
    not there.
    """
    out: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            out.append((tok.start[0], tok.string))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.append((getattr(node, "lineno", 1), doc))
    return out


def looks_symbolic(sym: str) -> bool:
    """Heuristic: does this token look like a CODE symbol rather than an English word?

    True for snake_case (contains an underscore) and for ALL_CAPS of length > 1.
    Those are the two naming conventions this codebase actually uses for functions,
    fields and constants. A bare lowercase word -- `must`, `say`, `cp`, `delivery` --
    is far more often prose, a dict key, a shell command or a CLI subcommand.
    It is a HEURISTIC and it discards real single-word symbols (`scan`, `main`);
    that is why it is opt-in rather than the default.
    """
    return ("_" in sym) or (sym.isupper() and len(sym) > 1)


def scan(path: Path, known: set[str] | None = None) -> list[tuple[int, str]]:
    """[(line, symbol)] for backticked identifiers the file does not bind.

    Raises OSError / SyntaxError / tokenize.TokenError to the caller rather than
    swallowing them, so "could not scan" can never be mistaken for "found nothing".
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    resolvable = (bound_names(tree) | set(dir(builtins)) | set(keyword.kwlist)
                  | (known or set()))
    found: list[tuple[int, str]] = []
    for line, text in prose_spans(src, tree):
        for sym in _TICKED.findall(text):
            if sym not in resolvable:
                found.append((line, sym))
    return found


def main(argv: list[str]) -> int:
    known: set[str] = set()
    files: list[str] = []
    symbolic_only = False
    i = 0
    while i < len(argv):
        if argv[i] == "--known" and i + 1 < len(argv):
            known |= {s.strip() for s in argv[i + 1].split(",") if s.strip()}
            i += 2
            continue
        if argv[i] == "--symbolic":
            symbolic_only = True
            i += 1
            continue
        files.append(argv[i])
        i += 1
    if not files:
        print("usage: forward_refs.py <file.py> ... "
              "[--known NAME,NAME] [--symbolic]")
        return 2
    total = 0
    skipped = 0
    for f in files:
        try:
            hits = scan(Path(f), known)
        except (OSError, SyntaxError, tokenize.TokenError, ValueError) as e:
            print(f"{f}: NOT SCANNED ({e.__class__.__name__}: {e})")
            skipped += 1
            continue
        if symbolic_only:
            hits = [(line, s) for line, s in hits if looks_symbolic(s)]
        for line, sym in hits:
            print(f"{f}:{line}: prose names `{sym}`, which this file does not bind")
        total += len(hits)
    print(f"forward-reference candidates: {total}; files not scanned: {skipped}")
    if skipped:
        return 2
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
