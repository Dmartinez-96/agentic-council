#!/usr/bin/env python3
"""Bootstrap installer for the agentic-council Claude Code integration.

Installs the council scripts to an install location of your choosing,
templates and writes the Claude Code settings.json hook block, and
installs the /council slash command. By default the council scripts
land at ~/.local/share/agentic-council/ and Claude Code config is
under ~/.claude/.

Usage:
    python3 install.py                 # install with defaults
    python3 install.py --council-root /custom/path
    python3 install.py --dry-run       # print actions, do not write
    python3 install.py --force         # overwrite existing files

Prerequisites the script verifies:
    Python 3.10+         (the council scripts use 3.10+ type syntax)
    codex CLI on PATH    (authenticated; gpt-5.6-sol model accessible)
    OPENROUTER_API_KEY   (gemini + deepseek voting members AND the layer-2
                          inspectors kimi/glm/grok all route through OpenRouter)
    bubblewrap on PATH   (Linux only; warning if missing -- gates the exec sandbox)

A council member must never be able to mutate state. An agentic CLI used as a
council member is a member that can WRITE: an agentic gemini CLI, run as a
read-only critic, rewrote six council files plus settings.json in a single
review. So members hold no write access: gemini/deepseek and the inspectors are
stateless API calls, codex is a read-only sandboxed subprocess, and each has only
harness-mediated read-only tools. Do not "improve" this installer by probing an
agentic CLI.

Files this installer touches:
    <council_root>/                    council scripts (copied from
                                       the repo's council/ directory)
    ~/.claude/settings.json            hook block merged into existing
    ~/.claude/commands/council.md      slash command installed
    ~/.claude/state/                   directory created if missing
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
COUNCIL_SRC_DIR = REPO_ROOT / "council"
HOOKS_TEMPLATE = REPO_ROOT / "claude-code" / "settings.hooks.template.json"
COMMAND_TEMPLATE = REPO_ROOT / "claude-code" / "commands" / "council.template.md"

DEFAULT_COUNCIL_ROOT = Path.home() / ".local" / "share" / "agentic-council"
CLAUDE_HOME = Path.home() / ".claude"
SETTINGS_PATH = CLAUDE_HOME / "settings.json"
COMMANDS_DIR = CLAUDE_HOME / "commands"
STATE_DIR = CLAUDE_HOME / "state"

COUNCIL_FILES = [
    "consult_council.py",
    "council_leader.py",
    "council_advisor.py",
    "council_dialogue.py",
    "council_outcome.py",
    "council_audit_writes.py",
    "council_shadow_audit.py",
    "laziness_gate.py",
    "stop_audit.py",
    "session_start_probe.py",
    "session_start_directive.py",
    "evidence_logger.py",
    "council_system_prompt.md",
    "council_dialogue_prompt.md",
    "council_layer2_prompt.md",
]
EXECUTABLE_FILES = [f for f in COUNCIL_FILES if f.endswith(".py")]


class Reporter:
    def __init__(self, dry_run: bool = False, quiet: bool = False) -> None:
        self.dry_run = dry_run
        self.quiet = quiet
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg: str) -> None:
        if not self.quiet:
            print(msg)

    def step(self, msg: str) -> None:
        if not self.quiet:
            print(f"[step] {msg}")

    def ok(self, msg: str) -> None:
        if not self.quiet:
            print(f"[ ok ] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[warn] {msg}", file=sys.stderr)

    def err(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"[err ] {msg}", file=sys.stderr)


def council_module(rep: Reporter):
    """Import the council wrapper so the installer reads ITS config.

    Returns the module, or None (having reported an error) if it cannot be
    imported. Verified present on the wrapper: CODEX_MODEL, CODEX_REASONING,
    GEMINI_API_MODEL, GEMINI_API_URL.
    """
    sys.path.insert(0, str(COUNCIL_SRC_DIR))
    try:
        import consult_council as cc
        return cc
    except Exception as e:  # noqa: BLE001
        rep.err(f"could not import the council wrapper at {COUNCIL_SRC_DIR}: {e}")
        return None


def check_python(rep: Reporter) -> bool:
    v = sys.version_info
    if (v.major, v.minor) < (3, 10):
        rep.err(f"Python 3.10+ required; found {v.major}.{v.minor}.{v.micro}")
        return False
    rep.ok(f"Python version OK: {v.major}.{v.minor}.{v.micro}")
    return True


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def check_codex(rep: Reporter) -> bool:
    path = which("codex")
    if not path:
        rep.err("codex CLI not found on PATH. Install it per your "
                "vendor's official instructions before re-running.")
        return False
    rep.ok(f"codex CLI on PATH: {path}")
    try:
        proc = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=10
        )
        rep.ok(f"codex --version: {proc.stdout.strip() or proc.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        rep.warn(f"codex --version failed: {e}")
    return True


def check_openrouter_key(rep: Reporter) -> bool:
    """The gemini and deepseek voting members AND the layer-2 inspector tier
    (kimi/glm/grok) all run through OpenRouter, so OPENROUTER_API_KEY is required.
    Without it only codex remains -- a single voting member, which cannot reach the
    BLOCK quorum and leaves layer 2 (half the council) dark.

    Presence test only. NEVER interpolate the value: a `${KEY:-...}` style expansion
    in a shell, or an f-string here, prints the secret.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        rep.err("OPENROUTER_API_KEY not set. The gemini and deepseek voting members "
                "and the layer-2 inspectors (kimi/glm/grok) all route through "
                "OpenRouter, so without this key only codex remains. Export it in the "
                "environment Claude Code itself launches under -- the council runs as "
                "a child process and inherits that env.")
        return False
    rep.ok("OPENROUTER_API_KEY present (value not printed): gemini + deepseek + the "
           "layer-2 inspector tier are enabled. Layer 2 is ON BY DEFAULT; "
           "`touch <council_root>/NO_SHADOW` disables it.")
    return True


def check_standing_rules(rep: Reporter) -> None:
    """Informational. The council READS ~/.claude/CLAUDE.md and shows it to every
    member, so bar item 12 can cite the user's own rules by name. Without the file
    that block is simply empty -- the council still enforces the directives typed
    during a session, so this is a missing capability, not a broken install.

    Deliberately does NOT write the file. CLAUDE.md is the user's own standing
    instructions to their agent; an installer that silently authors those has
    overstepped. Point at the template and let them decide.
    """
    path = CLAUDE_HOME / "CLAUDE.md"
    if path.exists():
        rep.ok(f"{path} present: the council will show it to every member, and "
               f"members can cite your rules by name (bar item 12).")
        return
    rep.info(f"No {path}. The council will still enforce the directives you type "
             f"during a session, but it has no STANDING rules of yours to cite.")
    rep.info(f"  A starter you can copy and edit: "
             f"{REPO_ROOT / 'starter-prompts' / 'CLAUDE.md.template'}")
    rep.info("  Read it before copying: its failure-mode list was observed on one "
             "project and may not be your agent's failures.")


def check_bubblewrap(rep: Reporter) -> None:
    if sys.platform.startswith("linux"):
        if not which("bwrap"):
            rep.warn("bubblewrap (bwrap) not on PATH.")


def probe_codex_model(rep: Reporter) -> bool:
    """Probe codex on the SAME model the council will actually use.

    The model is read from the wrapper (cc.CODEX_MODEL), never restated here.
    An installer that hardcodes its own copy of the model name drifts from the
    thing it installs, and then green-lights a model the council never runs --
    which is how this file came to be probing gpt-5.5 long after the council
    had moved to the gpt-5.6 family.
    """
    cc = council_module(rep)
    if cc is None:
        return False
    model, effort = cc.CODEX_MODEL, cc.CODEX_REASONING
    rep.step(f"Probing codex with model={model}, reasoning={effort}...")
    cmd = [
        "codex", "exec",
        "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--color", "never",
        "-c", f'model="{model}"',
        "-c", f'model_reasoning_effort="{effort}"',
        "Reply with exactly the text VERDICT: PASS and nothing else.",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        rep.err(f"codex probe timed out (model={model})")
        return False
    if proc.returncode != 0:
        rep.err(f"codex probe failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()[:400]}")
        return False
    # Check STDOUT, never stderr. Measured on codex-cli 0.144.1, both branches:
    #   gpt-5.6-sol           -> rc=0, stdout == "VERDICT: PASS"
    #   gpt-5.6-nonexistent   -> rc=1, stdout == ""   (API 400, model rejected)
    # so stdout is a clean signal. STDERR IS NOT: codex echoes the prompt there,
    # and this prompt contains the literal words "VERDICT: PASS". Measured: the
    # string is present in stderr even on the REJECTED run. The rc check above
    # would still catch that particular case -- but a sentinel search against
    # stderr is matching our own prompt text, so it proves nothing on its own,
    # and must never be the thing a caller relies on.
    if "VERDICT: PASS" not in proc.stdout:
        rep.warn(f"codex exited 0 but stdout did not contain `VERDICT: PASS`: "
                 f"{proc.stdout.strip()[:300]}")
        return False
    rep.ok(f"codex probe succeeded with model={model}")
    return True


def probe_openrouter(rep: Reporter) -> bool:
    """Probe OpenRouter with the configured key. gemini, deepseek, and the layer-2
    inspectors all route through it, so this verifies the key actually AUTHENTICATES,
    not merely that it is set.

    Uses GET /api/v1/key, which requires auth and returns the key's own metadata.
    Measured 2026-07-21: a valid key -> HTTP 200, an invalid one -> HTTP 401. (The
    /models endpoint, by contrast, returns 200 with NO key, so it cannot verify
    credentials.) No completion is spent.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        rep.err("OPENROUTER_API_KEY not set; cannot probe.")
        return False
    rep.step("Probing OpenRouter (GET /api/v1/key)...")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        if e.code == 401:
            rep.err("OpenRouter rejected OPENROUTER_API_KEY (HTTP 401). Check the key.")
        else:
            rep.err(f"OpenRouter probe failed: HTTP {e.code}.")
        return False
    except Exception as e:  # noqa: BLE001
        rep.err(f"OpenRouter probe failed: {e}")
        return False
    if code != 200:
        rep.warn(f"OpenRouter /key returned HTTP {code}, expected 200.")
        return False
    rep.ok("OpenRouter probe succeeded (OPENROUTER_API_KEY authenticates).")
    return True


def copy_council_scripts(rep: Reporter, council_root: Path,
                        force: bool) -> bool:
    rep.step(f"Installing council scripts to {council_root}/")
    if rep.dry_run:
        for name in COUNCIL_FILES:
            rep.info(f"  would copy: {COUNCIL_SRC_DIR / name} -> "
                     f"{council_root / name}")
        return True
    council_root.mkdir(parents=True, exist_ok=True)
    for name in COUNCIL_FILES:
        src = COUNCIL_SRC_DIR / name
        dst = council_root / name
        if not src.exists():
            rep.err(f"missing source file: {src}")
            return False
        if dst.exists() and not force:
            rep.warn(f"{dst} exists; pass --force to overwrite. Skipping.")
            continue
        shutil.copy2(src, dst)
        if name in EXECUTABLE_FILES:
            dst.chmod(0o755)
        rep.ok(f"installed {dst}")
    return True


def render_hooks_template(council_root: Path) -> dict:
    text = HOOKS_TEMPLATE.read_text()
    rendered = text.replace("{{COUNCIL_ROOT}}", str(council_root))
    return json.loads(rendered)


def merge_settings(rep: Reporter, council_root: Path, force: bool) -> bool:
    rep.step(f"Merging hook block into {SETTINGS_PATH}")
    hooks_to_add = render_hooks_template(council_root)
    if rep.dry_run:
        rep.info("  would merge the following hooks into "
                 f"{SETTINGS_PATH}:")
        rep.info(json.dumps(hooks_to_add, indent=2))
        return True
    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        try:
            current = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            rep.err(f"existing {SETTINGS_PATH} is not valid JSON: {e}")
            return False
        backup = SETTINGS_PATH.with_suffix(".json.pre-council.bak")
        if not backup.exists() or force:
            shutil.copy2(SETTINGS_PATH, backup)
            rep.ok(f"backed up existing settings.json to {backup}")
    else:
        current = {}
    existing_hooks = current.get("hooks") or {}
    if not isinstance(existing_hooks, dict):
        rep.err(f"existing {SETTINGS_PATH} has a non-dict `hooks` key. "
                "Refusing to merge.")
        return False
    # IDEMPOTENT re-install. The previous version did a bare `.extend()`, so
    # running the installer twice appended a SECOND copy of every council hook
    # and the council then fired twice on every single edit -- double the calls,
    # double the cost, two verdicts per write. So: drop any entry that points at
    # one of OUR scripts, then add the fresh set. Entries belonging to hooks the
    # user installed themselves are matched by nothing here and are preserved.
    # The settings schema nests: hooks[event] is a list of ENTRIES, each with a
    # "matcher" and its own "hooks" list of HANDLERS (verified against a real
    # settings.json and against our own template).
    #
    # Prune at the HANDLER level, not the entry level. An entry can legitimately
    # mix our handlers with someone else's under one matcher -- a real one seen
    # in the wild bundles council_advisor.py, evidence_logger.py and a THIRD
    # party's hook in a single PostToolUse entry. Dropping whole entries would
    # have deleted that third-party hook without a word.
    #
    # Matching is on the EXACT basename, so that a previous install at a
    # DIFFERENT council_root is still recognised and replaced.
    #
    # It must be exact. A suffix test (`cmd.endswith(name)`) looks equivalent and
    # is not: "claude_council_advisor.py".endswith("council_advisor.py") is True,
    # so a suffix test silently deletes a DIFFERENT tool's hook -- and the real
    # settings.json this was written against contains exactly that handler.
    #
    # The honest residual limit: a hook of the user's whose script is named
    # exactly e.g. `stop_audit.py` would still be pruned. Not detected.
    council_scripts = {f for f in COUNCIL_FILES if f.endswith(".py")}

    def is_council_handler(h: dict) -> bool:
        # A token is ours when it is PATH-SHAPED and its basename is exactly one
        # of our scripts. Both halves are load-bearing:
        #
        #   every token, not just the first -- a hook may be written as
        #     `python3 /opt/council/laziness_gate.py`, where token 0 is "python3".
        #     Matching only token 0 fails to recognise our own hook and appends a
        #     duplicate on every reinstall, which is the bug this exists to stop.
        #
        #   path-shaped (contains a separator), not any bare token -- otherwise
        #     someone else's `lint --exclude stop_audit.py` matches on an
        #     ARGUMENT and we silently delete their hook.
        #
        # shlex, not .split(), so a quoted path containing spaces stays one token.
        cmd = str(h.get("command") or "")
        try:
            tokens = shlex.split(cmd)
        except ValueError:              # unbalanced quotes: not ours, leave it
            return False
        return any(os.sep in tok and Path(tok).name in council_scripts
                   for tok in tokens)

    for event, entries in hooks_to_add.items():
        cur = existing_hooks.get(event) or []
        if not isinstance(cur, list):
            rep.err(f"existing {SETTINGS_PATH} has a non-list "
                    f"hooks[{event}]. Refusing to merge.")
            return False
        kept, pruned = [], 0
        for entry in cur:
            handlers = entry.get("hooks")
            if not isinstance(handlers, list):
                kept.append(entry)                  # shape we do not own; leave it
                continue
            survivors = [h for h in handlers if not is_council_handler(h)]
            pruned += len(handlers) - len(survivors)
            if survivors:                           # foreign handlers remain -> keep
                kept.append({**entry, "hooks": survivors})
            # an entry whose handlers were ALL ours is dropped entirely
        if pruned:
            rep.info(f"  hooks[{event}]: pruned {pruned} stale council "
                     f"handler(s); all non-council handlers preserved")
        existing_hooks[event] = kept + entries
    current["hooks"] = existing_hooks
    SETTINGS_PATH.write_text(json.dumps(current, indent=2) + "\n")
    rep.ok(f"updated {SETTINGS_PATH}")
    return True


def install_command(rep: Reporter, council_root: Path, force: bool) -> bool:
    rep.step(f"Installing /council slash command at "
             f"{COMMANDS_DIR}/council.md")
    template_text = COMMAND_TEMPLATE.read_text()
    rendered = template_text.replace("{{COUNCIL_ROOT}}", str(council_root))
    if rep.dry_run:
        rep.info(f"  would write {COMMANDS_DIR}/council.md "
                 f"({len(rendered)} bytes)")
        return True
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    target = COMMANDS_DIR / "council.md"
    if target.exists() and not force:
        rep.warn(f"{target} exists; pass --force to overwrite. Skipping.")
        return True
    target.write_text(rendered)
    rep.ok(f"installed {target}")
    return True


def ensure_state_dir(rep: Reporter) -> None:
    if rep.dry_run:
        rep.info(f"  would create {STATE_DIR}/ if missing")
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rep.ok(f"state directory ready: {STATE_DIR}/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--council-root", type=Path, default=None,
                        help=f"Where to install the council scripts. "
                             f"Default: {DEFAULT_COUNCIL_ROOT}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions but do not write any files.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files.")
    parser.add_argument("--skip-probes", action="store_true",
                        help="Skip the codex / gemini live probes.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress informational output.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rep = Reporter(dry_run=args.dry_run, quiet=args.quiet)
    council_root = (args.council_root or DEFAULT_COUNCIL_ROOT).expanduser()
    rep.info("agentic-council installer")
    rep.info(f"  repo root:    {REPO_ROOT}")
    rep.info(f"  council root: {council_root}")
    rep.info(f"  dry-run:      {args.dry_run}")
    rep.info(f"  force:        {args.force}")
    rep.info("")

    ok = True
    if not check_python(rep):
        ok = False
    if not check_codex(rep):
        ok = False
    if not check_openrouter_key(rep):
        ok = False
    check_standing_rules(rep)
    check_bubblewrap(rep)

    if not ok:
        rep.err("prerequisite checks failed. Address the errors above "
                "and re-run.")
        return 2

    if not args.skip_probes:
        if not probe_codex_model(rep):
            rep.err("codex probe failed (the model it tried is named in the "
                    "step line above). Check your codex CLI version and your "
                    "subscription tier -- a model can be rejected for the "
                    "account type rather than for the CLI version. Verified "
                    "working combination: codex-cli 0.144.1 + gpt-5.6-sol. "
                    "Pass --skip-probes to bypass.")
            return 2
        if not probe_openrouter(rep):
            rep.err("OpenRouter probe failed (see above). Check OPENROUTER_API_KEY. "
                    "Pass --skip-probes to bypass.")
            return 2

    if not copy_council_scripts(rep, council_root, args.force):
        return 2
    if not merge_settings(rep, council_root, args.force):
        return 2
    if not install_command(rep, council_root, args.force):
        return 2
    ensure_state_dir(rep)

    rep.info("")
    rep.info("Install complete.")
    rep.info("")
    rep.info("Next steps:")
    rep.info("  1. Restart Claude Code so the new hooks are loaded "
             "(hook config is read at session start).")
    rep.info("  2. Edit the system prompt to match your workflow: "
             f"{council_root / 'council_system_prompt.md'}")
    rep.info("  3. Optionally tune the rule-11 trigger phrase list in "
             f"{council_root / 'laziness_gate.py'} and "
             f"{council_root / 'stop_audit.py'} for false positives "
             "specific to your domain.")
    rep.info("  4. On your next Claude Code session, the SessionStart "
             "probe will run automatically and land hardware/disk/"
             "python info in the per-session evidence file at "
             "~/.claude/state/<session_id>/evidence.jsonl.")
    rep.info("")
    if rep.warnings:
        rep.info(f"{len(rep.warnings)} warning(s) emitted; review above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
