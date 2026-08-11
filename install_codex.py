#!/usr/bin/env python3
"""Install the additional Codex CLI hooks without disturbing the Claude Code hooks.

Claude Code hooks live in ~/.claude/settings.json (install.py); Codex CLI hooks live in
~/.codex/hooks.json (this script). Both point at one council root, so engine, rosters and logs
are shared while the two registrations never touch each other. This script does not read or
modify ~/.claude/settings.json.

REQUIRED IN THE COUNCIL ROOT, all checked before any registration is written, because a hook
pointing at a missing file fails at exec rather than reporting a problem:
  hook_env.sh, codex_hook.py, brain_index.py, roster.codex-led.json
brain_index.py is there because codex_hook.py imports it at MODULE SCOPE, so a root without it
registers a handler that dies on import. roster.codex-led.json is there because the two
harnesses answer a missing roster differently -- consult_council has an
`if not ROSTER_PATH.exists():` branch returning its built-in DEFAULT_REGISTRY, while
codex_hook's `_profile_error()` returns "Codex council profile unreadable" and pre_tool turns
that into emit_pre_deny. A Codex session without it cannot edit anything.

ACTIVATION IS MANUAL AND I HAVE NOT VERIFIED WHY. Codex's own installer and this project's
notes both say newly installed hook definitions must be inspected and trusted via /hooks before
they take effect. No primary source was consulted here and no trust prompt was observed, so
treat that as received guidance rather than a measured fact: run /hooks, and if the hooks work
without it, this docstring is wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import install as shared

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATE = REPO_ROOT / "codex" / "hooks.template.json"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
HOOKS_PATH = CODEX_HOME / "hooks.json"
STATE_DIR = CODEX_HOME / "state"
CANONICAL_RULES = Path.home() / ".config" / "agentic-council" / "standing-rules.md"
OWNERSHIP_PATH = CANONICAL_RULES.parent / ".link-ownership.json"
INSTRUCTION_PATHS = (Path.home() / ".claude" / "CLAUDE.md", CODEX_HOME / "AGENTS.md")
# Everything codex_hook.py needs at import or first use. Kept beside the module rather than
# inline in main() so the docstring above and the check below cannot drift apart.
REQUIRED_NAMES = ("hook_env.sh", "codex_hook.py", "brain_index.py", "roster.codex-led.json")


def render(council_root: Path) -> dict:
    # THE TEMPLATE'S SCHEMA IS PARTLY VERIFIED. Recorded here because JSON carries no comment and
    # this constant is the only reference to codex/hooks.template.json in the repository.
    # OBSERVED 2026-08-11, by walking both documents into sets of leaf key-paths and diffing the
    # sets rather than reading them side by side: the template and a live, working
    # ~/.codex/hooks.json have IDENTICAL key sets, 23 paths, none unique to either. Field sets
    # differ BY EVENT, identically in both -- Stop carries neither `matcher` nor
    # `additionalContextLimit`; SessionStart, PreToolUse and PostToolUse carry both.
    # AND THAT LIVE REGISTRY DISPATCHES: `codex exec --ephemeral --skip-git-repo-check --sandbox
    # read-only --color never -c model="gpt-5.6-sol" -` printed four `hook: ` lines (SessionStart
    # and Stop, each with a Completed) at rc=0, while the same argv plus `--disable hooks` printed
    # zero, rc=0, still answering.
    # NOT ESTABLISHED: PreToolUse and PostToolUse were NOT observed dispatching -- that run issued
    # no tool calls, and why they did not appear was not probed. No field was varied to check that
    # Codex honours it. And the shape agreement is not independent evidence: the live file and
    # this template may share an origin. No Codex hook-schema document was read.
    return json.loads(TEMPLATE.read_text().replace("{{COUNCIL_ROOT}}", str(council_root)))


def is_ours(handler) -> bool:
    """True for a handler whose command runs SOME codex_hook.py.

    IT MATCHES BY BASENAME, NOT BY ROOT, and that is a real over-match rather than a tidy
    heuristic: a handler pointing at an unrelated /elsewhere/codex_hook.py returns True here and
    would be pruned. Verified, not assumed -- is_ours() on a handler reading
    `/opt/someone-else/wrapper /opt/someone-else/codex_hook.py post-tool` returns True.
    Tokenising still matters (a bare word in an argument cannot match, only a path), but do not
    read prune_hooks as "foreign handlers are safe": it is "handlers naming a differently-rooted
    codex_hook.py are treated as ours".
    """
    if not isinstance(handler, dict):
        return False
    try:
        tokens = shlex.split(str(handler.get("command") or ""))
    except ValueError:
        return False
    return any(os.sep in token and Path(token).name == "codex_hook.py" for token in tokens)


def prune_hooks(hooks: dict) -> tuple[dict, int]:
    """Drop handlers is_ours() claims, keep the rest, and report how many went.

    Structure this version does not understand passes through verbatim rather than being
    normalised, so an unfamiliar config shape survives a re-run instead of being rewritten.
    """
    result, count = {}, 0
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            result[event] = entries
            continue
        kept = []
        for entry in entries:
            handlers = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(handlers, list):
                kept.append(entry)
                continue
            survivors = [handler for handler in handlers if not is_ours(handler)]
            count += len(handlers) - len(survivors)
            if survivors:
                kept.append({**entry, "hooks": survivors})
        if kept:
            result[event] = kept
    return result, count


def atomic_json(path: Path, value: dict) -> None:
    """Temp file plus os.replace: an interrupted run must not leave a truncated hooks.json,
    which would disable every Codex hook at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.partial")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def read_config(rep: shared.Reporter, path: Path) -> tuple[dict | None, dict | None]:
    """(config, hooks) for an existing hooks file, or (None, None) if it cannot be used.

    THREE SHAPES THAT ARE NOT THE SAME THING, and an earlier draft conflated the last two:
      - unparseable JSON            -> refuse; it may hold handlers worth keeping.
      - a non-OBJECT top level      -> refuse. `json.loads("[1,2,3]").get("hooks")` raises
                                       AttributeError (verified), so this crashed rather than
                                       reporting, and a crash mid-install says nothing useful.
      - `hooks` present but not a dict -> refuse. The old `current.get("hooks") or {}` turned a
                                       FALSEY malformed value (`[]`, `0`, `""`) into `{}` and
                                       sailed past the type check below, silently discarding
                                       whatever the operator actually had.
    An ABSENT `hooks` key is the only one of the four that is fine, and it means an empty dict.
    """
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        rep.err(f"existing {path} is not valid JSON: {exc}")
        return None, None
    if not isinstance(config, dict):
        rep.err(f"existing {path} has a non-object top level "
                f"({type(config).__name__}); refusing to touch it")
        return None, None
    hooks = config.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        rep.err(f"existing {path} has a non-object hooks field ({type(hooks).__name__}); "
                f"refusing to replace it")
        return None, None
    return config, hooks


def merge_hooks(rep: shared.Reporter, council_root: Path) -> bool:
    desired = render(council_root)
    rep.step(f"Merging Codex hook block into {HOOKS_PATH}")
    if rep.dry_run:
        rep.info(json.dumps(desired, indent=2))
        return True
    if HOOKS_PATH.exists():
        config, hooks = read_config(rep, HOOKS_PATH)
        if config is None:
            return False
        backup = HOOKS_PATH.with_name(HOOKS_PATH.name + ".pre-council.bak")
        if not backup.exists():
            shutil.copy2(HOOKS_PATH, backup)
            rep.ok(f"backed up existing hooks to {backup}")
    else:
        config, hooks = {}, {}
    hooks, removed = prune_hooks(hooks)
    if removed:
        rep.info(f"  pruned {removed} handler(s) naming a codex_hook.py")
    for event, entries in desired["hooks"].items():
        # SAME DEFECT AS read_config's, AT A SECOND SITE. `hooks.get(event) or []` discards a
        # FALSEY malformed per-event value -- `0`, `""`, `False` all yield [] and sail past the
        # isinstance check below, so whatever the operator had under that event is replaced with
        # no error. Demonstrated 2026-08-10: of `[] 0 "" False None`, the three middle values are
        # silently dropped while a non-falsey wrong type is caught.
        # read_config was fixed for the top-level `hooks` key and this site was left behind --
        # the fix has to cover every occurrence of the class, not the one that was noticed.
        current_entries = hooks.get(event)
        if current_entries is None:
            current_entries = []
        if not isinstance(current_entries, list):
            rep.err(f"existing hooks[{event}] is not a list "
                    f"({type(current_entries).__name__}); refusing to replace it")
            return False
        hooks[event] = current_entries + entries
    config["hooks"] = hooks
    config.setdefault("description", desired["description"])
    atomic_json(HOOKS_PATH, config)
    rep.ok(f"updated {HOOKS_PATH}")
    return True


def _same_bytes(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def link_standing_rules(rep: shared.Reporter) -> bool:
    """Point both harnesses' instruction files at one neutral canonical path.

    OPT-IN ONLY, because it replaces files the operator wrote with symlinks. It REFUSES on
    divergence rather than picking a winner: if CLAUDE.md and AGENTS.md differ, merging them is
    not an installer's call. Each replacement is recorded BEFORE the link is created, so
    --uninstall can tell a link it made from one that was already there.
    """
    source = INSTRUCTION_PATHS[0]
    if not source.exists() and not CANONICAL_RULES.exists():
        rep.err(f"cannot initialize canonical rules: neither {source} nor "
                f"{CANONICAL_RULES} exists")
        return False
    seed = CANONICAL_RULES.read_bytes() if CANONICAL_RULES.exists() else source.read_bytes()
    for path in INSTRUCTION_PATHS:
        if path.is_symlink():
            if path.resolve() == CANONICAL_RULES.resolve():
                continue
            rep.err(f"refusing non-canonical instruction symlink {path}; "
                    f"resolve it explicitly first")
            return False
        if path.exists() and not _same_bytes(path, seed):
            rep.err(f"refusing divergent instruction file {path}; merge it explicitly first")
            return False
    already_linked = all(
        path.is_symlink() and path.resolve() == CANONICAL_RULES.resolve()
        for path in INSTRUCTION_PATHS
    )
    if already_linked and OWNERSHIP_PATH.exists():
        rep.ok("standing-rule links already point to the canonical path; ownership preserved")
        return True
    if OWNERSHIP_PATH.exists():
        rep.err("standing-rule ownership records a partial migration; run "
                "install_codex.py --uninstall to restore it before retrying")
        return False
    if rep.dry_run:
        rep.info(f"  would canonicalize standing rules at {CANONICAL_RULES}")
        return True
    CANONICAL_RULES.parent.mkdir(parents=True, exist_ok=True)
    if not CANONICAL_RULES.exists():
        CANONICAL_RULES.write_bytes(seed)
    if CANONICAL_RULES.read_bytes() != seed:
        rep.err("canonical standing rules changed during migration")
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ownership = {"canonical": str(CANONICAL_RULES), "links": {}}
    for path in INSTRUCTION_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() and path.resolve() == CANONICAL_RULES.resolve():
            continue
        backup = None
        if path.exists():
            backup = path.with_name(path.name + f".pre-council-{stamp}.bak")
            shutil.copy2(path, backup)
        # Recorded BEFORE the file is touched: a crash between unlink and symlink_to leaves the
        # path absent, and only a "pending" record separates that from a path that never
        # existed -- the difference between restoring a backup and removing a link.
        ownership["links"][str(path)] = {
            "backup": str(backup) if backup else None,
            "state": "pending",
        }
        try:
            atomic_json(OWNERSHIP_PATH, ownership)
            if path.exists():
                path.unlink()
            path.symlink_to(CANONICAL_RULES)
            ownership["links"][str(path)]["state"] = "linked"
            atomic_json(OWNERSHIP_PATH, ownership)
        except OSError as exc:
            rep.err(f"could not link {path}: {exc}; ownership record retained for recovery")
            return False
        rep.ok(f"linked {path} -> {CANONICAL_RULES}")
    return True


def restore_owned_links(rep: shared.Reporter) -> bool:
    """Undo only the links this installer made, per the ownership record.

    Anything it no longer owns is reported and skipped: overwriting a file the operator has
    since replaced would destroy their work in the name of tidying up ours.
    """
    if not OWNERSHIP_PATH.exists():
        return True
    try:
        ownership = json.loads(OWNERSHIP_PATH.read_text())
    except (OSError, ValueError) as exc:
        rep.err(f"could not read standing-rule ownership record: {exc}")
        return False
    links = ownership.get("links") if isinstance(ownership, dict) else None
    if not isinstance(links, dict):
        rep.err("standing-rule ownership record has no links object")
        return False
    restored_all = True
    for raw, record in links.items():
        path = Path(raw)
        if not isinstance(record, dict) or "backup" not in record:
            rep.err(f"cannot restore {path}: ownership entry lacks a backup field; "
                    "leaving the Council-owned link intact")
            restored_all = False
            continue
        state = record.get("state", "linked")
        if state not in {"pending", "linked"}:
            rep.err(f"cannot restore {path}: ownership entry has invalid state {state!r}")
            restored_all = False
            continue
        owned_link = path.is_symlink() and path.resolve() == CANONICAL_RULES.resolve()
        pending_absent = state == "pending" and not os.path.lexists(path)
        if not owned_link and not pending_absent:
            rep.warn(f"leaving {path}: it is no longer the Council-owned link")
            if state == "pending":
                restored_all = False
            continue
        backup = record["backup"]
        if backup is not None and (not isinstance(backup, str) or not Path(backup).is_file()):
            rep.err(f"cannot restore {path}: recorded backup is missing or invalid "
                    f"({backup!r}); leaving the Council-owned link intact")
            restored_all = False
            continue
        if rep.dry_run:
            rep.info(f"  would restore owned instruction path {path}")
            continue
        if backup is None:
            if owned_link:
                path.unlink()
                rep.ok(f"removed Council-owned link {path}")
            continue
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".restore-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copy2(backup, tmp)
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            rep.err(f"cannot restore {path} from {backup}: {exc}; leaving the "
                    "Council-owned link intact")
            restored_all = False
            continue
        rep.ok(f"restored {path}")
    if not rep.dry_run and restored_all:
        OWNERSHIP_PATH.unlink()
    return restored_all


def uninstall(rep: shared.Reporter) -> int:
    removed_handlers = 0
    if HOOKS_PATH.exists():
        config, hooks = read_config(rep, HOOKS_PATH)
        if config is None:
            return 2
        pruned, count = prune_hooks(hooks)
        if rep.dry_run:
            rep.info(f"  would remove {count} handler(s) naming a codex_hook.py")
        elif count:
            backup = HOOKS_PATH.with_name(HOOKS_PATH.name + ".pre-uninstall.bak")
            shutil.copy2(HOOKS_PATH, backup)
            config["hooks"] = pruned
            atomic_json(HOOKS_PATH, config)
            removed_handlers = count
            rep.ok(f"removed {count} handler(s) naming a codex_hook.py")
    if not restore_owned_links(rep):
        # The prefix is not cosmetic: whether hooks came out before the link failure changes
        # what the operator has left to fix.
        if rep.dry_run:
            rep.err("dry run found one or more standing-rule links whose recorded "
                    "backup cannot be restored; no files were changed")
        else:
            prefix = ("Codex hooks were removed, but" if removed_handlers else
                      "No Codex Council handler was removed in this run, and")
            rep.err(prefix + " one or more standing-rule links could not be restored; "
                    "ownership record retained for recovery")
        return 2
    rep.info("Shared council scripts, profiles, logs, snapshots and canonical rules were "
             "preserved. install.py --uninstall handles the Claude Code side.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the additional Codex CLI hooks without disturbing "
                    "the Claude Code hooks.")
    parser.add_argument("--council-root", type=Path, default=shared.DEFAULT_COUNCIL_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-copy", action="store_true",
                        help="Point hooks at an existing live council root without copying")
    parser.add_argument("--link-standing-rules", action="store_true",
                        help="Opt in to reversible CLAUDE.md/AGENTS.md neutral-path links")
    parser.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rep = shared.Reporter(dry_run=args.dry_run)
    root = args.council_root.expanduser().absolute()
    if args.uninstall:
        return uninstall(rep)
    # Required here and not in install.py: the codex-led roster seats claude as a VOTING member
    # over claude_subprocess, so without the CLI that seat cannot run. Checked before any write.
    claude_path = shutil.which("claude")
    if claude_path is None:
        rep.err("claude CLI is required for the Claude voting seat in Codex-led sessions")
        return 2
    rep.ok(f"claude CLI on PATH: {claude_path}")
    if not args.skip_copy:
        if not shared.copy_council_scripts(rep, root, args.force):
            return 2
    # No brain-validator step, and the absence is deliberate: an earlier draft called
    # shared.copy_brain_validator(), which install.py does not define. Measured 2026-08-10 with
    # a pointer this comment cannot stale, because it greps the OTHER file for a DEFINITION:
    # `grep -n '^def copy_' install.py` returns exactly one line, copy_council_scripts. So that
    # call would have raised AttributeError on any run without --skip-copy.
    # WHAT install.py DOES AND DOES NOT DO WITH THE BRAIN, since "no brain handling at all"
    # would be wrong: brain_index.py is a member of its COUNCIL_FILES list, which is the
    # mechanism by which it ships -- the rationale recorded beside it there is codex_hook.py's
    # module-scope `import brain_index`. What install.py has NO step for is
    # brain/validate_brain.py or brain/templates: `grep -n validate_brain install.py` returns
    # nothing. Adding that belongs there, where both harnesses would get it.
    missing = [str(root / name) for name in REQUIRED_NAMES if not (root / name).is_file()]
    if missing:
        rep.err("Codex integration files missing: " + ", ".join(missing)
                + " -- run install.py first, or drop --skip-copy")
        return 2
    rep.ok("council root has " + ", ".join(REQUIRED_NAMES))
    if not merge_hooks(rep, root):
        return 2
    if args.link_standing_rules and not link_standing_rules(rep):
        return 2
    if not rep.dry_run:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    rep.info("Codex hooks written. Per Codex's documented flow they may not be active until "
             "you open /hooks, inspect and trust them, then start a fresh session; that "
             "requirement is received guidance, not something this installer verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
