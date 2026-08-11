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
    python3 install.py --uninstall     # remove the council (keeps roster.json + logs)

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
    # consult_council imports this at module scope, so an install that omits it raises
    # ModuleNotFoundError on every fire -- it is a hard dependency, not an optional extra.
    "council_events.py",
    # Terminal renderer for the progress stream. Standalone (nothing imports it), so an
    # install without it loses the live view but still fires normally.
    "council_watch.py",
    # The standalone GUI. council_gui.py is the only file importing PySide6, and nothing
    # imports IT, so a user who never opens the GUI needs no extra dependency and the
    # engine's zero-PyPI-dependency install is preserved.
    "council_gui.py",
    "council_gui_engine.py",
    "council_leader_run.py",
    "council_leader.py",
    # council_leader_run imports this at module scope for multi-turn conversations, so an
    # install that omits it raises ModuleNotFoundError on every leader turn -- a hard
    # dependency, exactly like council_events.py above.
    "council_session.py",
    "council_advisor.py",
    "council_dialogue.py",
    "council_outcome.py",
    "council_audit_writes.py",
    "council_shadow_audit.py",
    "laziness_gate.py",
    # PreToolUse advisory on Bash. The settings template wires a hook to this path, so an
    # install that omits it points a hook at a missing file.
    "scripted_write_guard.py",
    # THE DETERMINISTIC PreToolUse GATE, and the pre-landing seat it calls. Added 2026-08-10.
    # THEY INSTALL TOGETHER because the dependency is one-directional and was checked, not
    # assumed. `grep -n "^\s*import doorman\|doorman\.review" tier0_gate.py` returned two
    # lines -- an `import doorman` and a `doorman.review(payload)` call -- while searching the
    # hook registries for "doorman" returned zero occurrences. So doorman.py is NOT a hook:
    # the gate calls it. A gate installed without it has an unreachable seat, and a doorman
    # installed alone is code nothing invokes.
    # ONLY THE GATE NEEDS ITS MODE BIT, and the distinction is worth keeping straight:
    # EXECUTABLE_FILES below chmods every `.py` here to 0755, but that bit is load-bearing only
    # for a script the template EXECS. The template invokes `hook_env.sh <path>` and hook_env
    # ends in `exec "$@"`, so a non-executable gate never runs at all -- measured 2026-08-10
    # against a `-rw-rw-r--` file: `hook_env.sh ./doorman.py` -> rc=126, and the same command
    # with `python3` inserted -> rc=0. WHAT THAT ESTABLISHES is that the gate's checks do not
    # execute; what the harness then does with a hook that failed to exec is NOT established
    # here and is deliberately not asserted. Either way the protection is absent while the
    # configuration still looks correct, which is why the mode bit belongs to the installer
    # rather than to whoever remembers to set it.
    # doorman.py is IMPORTED, never exec'd, so its own bit is inert; it gets one only because
    # this list does not special-case extensions.
    "tier0_gate.py",
    "doorman.py",
    "stop_audit.py",
    "session_start_probe.py",
    "session_start_directive.py",
    "evidence_logger.py",
    # THE CODEX-LED LIFECYCLE. Added 2026-08-10; without these an install cannot run a
    # Codex-led session at all. codex_hook.py is the Codex CLI's
    # SessionStart/PreToolUse/PostToolUse/Stop handler, and its line 32 is a module-scope
    # `import brain_index`, so brain_index.py is a hard dependency exactly like
    # council_events.py above -- omitting it raises ModuleNotFoundError on every invocation.
    "codex_hook.py",
    "brain_index.py",
    # BOTH PROFILES, and the asymmetry is why neither is optional. hook_env.sh points
    # COUNCIL_ROSTER_PATH at `roster.<harness>-led.json`, so a missing file is a missing
    # profile -- and the two harnesses answer that very differently.
    # MEASURED 2026-08-10 against an absent path: consult_council's loader returns its built-in
    # DEFAULT_REGISTRY (its own comment names the case, "No roster file at all: a fresh
    # install"), so a CLAUDE-led council still fires. codex_hook does not: _profile_error()
    # returned "Codex council profile unreadable: [Errno 2] No such file or directory", and
    # codex_hook.py:794-796 reads `profile_error = _profile_error()` / `if profile_error:` /
    # `return emit_pre_deny(...)` -- so every apply_patch is denied.
    # An install missing these therefore leaves Claude-led working and Codex-led unable to edit
    # anything, which is the worse half of the pair to discover from a blocked session.
    "roster.claude-led.json",
    "roster.codex-led.json",
    # Every hook is invoked THROUGH this wrapper (see the hooks template), so an install
    # that omits it leaves every configured hook pointing at a file that is not there.
    # It loads OPENROUTER_API_KEY when the launching environment did not carry it, which
    # is the difference between a full council and one silently reduced to the seats that
    # need no key.
    "hook_env.sh",
    "council_system_prompt.md",
    "council_dialogue_prompt.md",
    "council_layer2_prompt.md",
    # The leader's harness guide. council_leader reads it OPTIONALLY, so omitting it produces
    # no error -- it seats a leader that never learns the harness is its only write path, and
    # does so silently. That silence is why it belongs in this list rather than being left to
    # the operator.
    "leader_harness_skill.md",
]
# NOT `.py`-ONLY, and the widening landed WITH hook_env.sh rather than after it. Hooks are
# invoked as programs, so anything in this list that a hook execs needs its mode bit; a
# `.py`-only filter would have installed hook_env.sh without one and every configured hook
# would then have failed at exec, since all seven are invoked THROUGH it. Extensions are
# listed rather than inferred, so adding a file with a new extension stays a decision made
# here instead of an accident.
EXECUTABLE_FILES = [f for f in COUNCIL_FILES if f.endswith((".py", ".sh"))]


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
        rep.err("codex CLI not found on PATH. Install it "
                "(`npm install -g @openai/codex` [Node 16+], `brew install --cask "
                "codex`, or the installer at github.com/openai/codex), then "
                "authenticate: run `codex` and Sign in with ChatGPT, or "
                "`printenv OPENAI_API_KEY | codex login --with-api-key`. Re-run "
                "install once codex is ready.")
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


def check_ground_rules(rep: Reporter, council_root: Path) -> None:
    """Informational. council_ground_rules.md is the BASE rules layer -- the one every
    seat receives, ahead of the evidence. Without it that layer is simply empty: the
    overlay layers still resolve and the council still runs, so this is a missing
    capability rather than a broken install.

    Deliberately does NOT write the file, for the same reason check_standing_rules does
    not. The template's rules were accrued on one project; copying them in silently
    would present another project's failure history as this user's own, which is the
    exact misattribution the base/overlay split exists to remove.
    """
    path = council_root / "council_ground_rules.md"
    if path.exists():
        rep.ok(f"{path} present: every seat receives it as the base rules layer, "
               f"ahead of the evidence and inside the cacheable prefix.")
        return
    rep.info(f"No base rules at {path}. Seats will receive no ground rules; "
             f"everything else still runs.")
    rep.info(f"  A starter you can copy and edit: "
             f"{REPO_ROOT / 'starter-prompts' / 'ground-rules.md.template'}")
    rep.info("  Read it before copying. A rule belongs in the base ONLY if it would be "
             "true and useful for an agent that had never made the mistake -- no agent "
             "names, no dates, no incident narration. Anything else belongs in an "
             "overlay under overlays/models/ or overlays/roles/.")


def check_standing_rules(rep: Reporter) -> None:
    """Informational. The standing-rules channel carries the rules the REVIEWED party
    works under, so a member can cite them by name rather than inferring them. It is a
    DIFFERENT question from the base/overlay layers, which say what binds the READER.

    IT IS OPT-IN. The council delivers this file only when COUNCIL_STANDING_RULES_PATH
    is set. It formerly defaulted to ~/.claude/CLAUDE.md, which made one agent's incident
    log the council's rules layer for every seat whatever model held it -- the exact
    thing the base/overlay split exists to undo. So an unset variable is reported here as
    a channel that is OFF, not as a missing file.

    Deliberately does NOT write the file. Those are the user's own standing instructions
    to their agent; an installer that silently authors them has overstepped.
    """
    env_path = os.environ.get("COUNCIL_STANDING_RULES_PATH")
    if not env_path:
        rep.info("COUNCIL_STANDING_RULES_PATH is not set, so the standing-rules channel "
                 "is OFF and no such file is delivered. This is optional: the base and "
                 "overlay layers are the council's rules layer.")
        rep.info(f"  To turn it on, point it at a file of your own standing "
                 f"instructions. A starter you can copy and edit: "
                 f"{REPO_ROOT / 'starter-prompts' / 'standing-rules.md.template'}")
        rep.info("  Read it before copying: its failure-mode list was observed on one "
                 "project and may not be your agent's failures.")
        return
    path = Path(env_path).expanduser()
    if path.exists():
        rep.ok(f"{path} present and COUNCIL_STANDING_RULES_PATH is set: the council "
               f"will show it to every member after the evidence, and members can cite "
               f"your rules by name (bar item 12).")
        return
    rep.warn(f"COUNCIL_STANDING_RULES_PATH points at {path}, which does not exist. "
             f"The channel is configured but delivers nothing; the council falls back "
             f"to an empty block rather than erroring.")


def check_bubblewrap(rep: Reporter) -> None:
    if sys.platform.startswith("linux"):
        if not which("bwrap"):
            rep.warn("bubblewrap (bwrap) not on PATH.")


def check_pyside6(rep: Reporter, council_root: Path) -> None:
    """Report whether the standalone GUI can run. OPTIONAL: among the council files that
    get installed, council_gui.py is the only one importing PySide6 -- the engine, hooks
    and CLI never do -- so a missing GUI dependency must never fail an install. (This
    installer imports it too, in the probe below, but install.py is not a council runtime
    file and its import is guarded.)

    Which interpreter to check is a CHOICE, not a sweep: if
    `<council root>/.venv-gui/bin/python3` exists it is the one that matters and the
    ambient interpreter is irrelevant, so this reports on the venv and stops. Only when
    there is no venv does it fall back to the interpreter running install.py.
    KNOWN DIVERGENCE, stated because a reader would otherwise assume symmetry: the VS
    Code extension's resolution is `council.pythonPath` (when explicitly set) -> the venv
    -> `python3`. This check has no access to that setting, so it starts at the venv. If
    a user sets `council.pythonPath` to a THIRD interpreter, the extension will launch
    with that one while this check reports on the venv, and they can disagree.
    """
    venv_python = council_root / ".venv-gui" / "bin" / "python3"
    if venv_python.is_file():
        try:
            probe = subprocess.run([str(venv_python), "-c", "import PySide6"],
                                   capture_output=True, text=True)
        except OSError as e:
            rep.warn(f"GUI: could not run {venv_python} ({e}).")
            return
        if probe.returncode == 0:
            rep.ok(f"GUI: PySide6 available in {venv_python}")
            return
        last = probe.stderr.strip().splitlines()[-1:] or ["no output"]
        rep.warn(f"GUI: {venv_python} exists but cannot import PySide6 "
                 f"({last[0]}). Recreate it or install PySide6 into it.")
        return
    try:
        # __import__ rather than a plain `import PySide6`: it genuinely imports (so an
        # installed-but-broken package is caught, which importlib.util.find_spec would
        # miss) while binding no unused name for linters to flag.
        __import__("PySide6")
    # Deliberately broad: this check is advisory, and a third-party package that raises
    # something other than ImportError at import time must not abort an install.
    except Exception:  # noqa: BLE001
        rep.warn(
            "GUI: PySide6 not importable, so `council_gui.py` will not start. "
            "This is OPTIONAL -- the engine, hooks and CLI do not need it. "
            "To enable the GUI, either create a dedicated venv "
            f"(python3 -m venv {council_root / '.venv-gui'} && "
            f"{council_root / '.venv-gui' / 'bin' / 'pip'} install PySide6), "
            "which the VS Code extension then finds automatically, or install "
            "PySide6 into the interpreter you will launch the GUI with.")
        return
    rep.ok("GUI: PySide6 available in the current interpreter")


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


COUNCIL_SCRIPTS = frozenset(f for f in COUNCIL_FILES if f.endswith(".py"))


def is_council_handler(h) -> bool:
    """True when a hook handler's command contains a path-shaped token whose basename
    is EXACTLY one of our council scripts.

    ``h`` is a settings.json handler dict; a non-dict (malformed settings) is never
    ours -- preserved, not crashed on. The command is tokenised with ``shlex`` (so a
    quoted path with spaces stays one token), and a token counts as ours only when it
    is PATH-SHAPED (contains a separator) AND its basename is exactly a council script:

      - EVERY token is checked, not just the first, because a hook may be written
        ``python3 /opt/council/laziness_gate.py`` (token 0 is "python3").
      - PATH-SHAPED, not any bare token, so a bare ``stop_audit.py`` appearing as an
        argument (``lint --exclude stop_audit.py``) does NOT match.

    RESIDUAL, stated honestly: because every path-shaped token is checked, the match is
    NOT limited to the script the handler invokes -- a path-shaped ARGUMENT with a
    council basename (e.g. ``lint --config /tmp/stop_audit.py``) also returns True, and
    that foreign hook would be pruned. A suffix test would be worse still
    (``"claude_council_advisor.py".endswith("council_advisor.py")`` is True). Exact
    basename on path-shaped tokens is the rule the installer uses; it is not
    collision-proof. Both ``merge_settings`` (prune-on-reinstall) and
    ``prune_council_from_hooks`` inherit this limit.
    """
    if not isinstance(h, dict):
        return False
    cmd = str(h.get("command") or "")
    try:
        tokens = shlex.split(cmd)
    except ValueError:                       # unbalanced quotes: not ours, leave it
        return False
    return any(os.sep in tok and Path(tok).name in COUNCIL_SCRIPTS
               for tok in tokens)


def prune_council_from_hooks(existing_hooks: dict) -> tuple[dict, int]:
    """Remove council handlers from EVERY hook event; return (new_hooks, pruned_count).

    Entries whose handlers were all council are dropped, and an event left with no
    entries is omitted. Foreign handlers -- and any unrecognised shape -- are preserved
    (subject to the exact-basename residual in ``is_council_handler``). Used by the
    uninstaller; ``merge_settings`` prunes the same way before re-adding the fresh set.
    """
    new_hooks: dict = {}
    pruned = 0
    for event, entries in existing_hooks.items():
        if not isinstance(entries, list):
            new_hooks[event] = entries            # shape we do not own; leave it
            continue
        kept = []
        for entry in entries:
            handlers = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(handlers, list):
                kept.append(entry)                 # shape we do not own; leave it
                continue
            survivors = [h for h in handlers if not is_council_handler(h)]
            pruned += len(handlers) - len(survivors)
            if survivors:
                kept.append({**entry, "hooks": survivors})
            # an entry whose handlers were ALL ours is dropped entirely
        if kept:
            new_hooks[event] = kept
        # an event left with no entries is dropped
    return new_hooks, pruned


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
    # double the cost, two verdicts per write. So: drop any HANDLER that points at
    # one of OUR scripts, then add the fresh set. Hooks the user installed
    # themselves are preserved -- unless a handler's command collides on an exact
    # council-script basename (the residual documented on is_council_handler).
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
    # Matching is on the EXACT basename (see the module-level is_council_handler),
    # so a previous install at a DIFFERENT council_root is still recognised and
    # replaced. That function owns the exact-basename rule and its collision residual.
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
                     f"handler(s); handlers not matched as council preserved")
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


def uninstall(rep: Reporter, council_root: Path) -> int:
    """Remove the council's hooks, /council command, and installed scripts; keep the rest.

    Removes: the council hook handlers from ~/.claude/settings.json (other handlers
    kept, subject to is_council_handler's exact-basename residual); the /council slash
    command (~/.claude/commands/council.md); and the installed council scripts (the
    COUNCIL_FILES) under council_root. Preserves: roster.json, logs/, reverted/,
    threads/, marker files and any other data under council_root, plus every
    non-council setting in settings.json. Removing hooks takes effect live via Claude
    Code's settings file-watcher -- no restart. Existing settings.json .pre-*.bak
    backups are left in place. Re-running the installer REINSTALLS; only this removes.
    Returns a process exit code (0 ok; 2 on an unreadable settings.json). Honours dry-run.
    """
    rep.info("agentic-council uninstaller")
    rep.info(f"  council root: {council_root}")
    rep.info(f"  dry-run:      {rep.dry_run}")
    rep.info("")

    # 1. settings.json: prune council hook handlers, preserve everything else.
    if SETTINGS_PATH.exists():
        try:
            current = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            rep.err(f"{SETTINGS_PATH} is not valid JSON: {e}. Remove the council hook "
                    "entries by hand.")
            return 2
        hooks = current.get("hooks")
        if isinstance(hooks, dict):
            pruned_hooks, n = prune_council_from_hooks(hooks)
            if not n:
                rep.info(f"no council hook handlers found in {SETTINGS_PATH}")
            elif rep.dry_run:
                rep.info(f"  would remove {n} council hook handler(s) from "
                         f"{SETTINGS_PATH}; handlers not matched as council preserved")
            else:
                backup = SETTINGS_PATH.with_suffix(".json.pre-uninstall.bak")
                shutil.copy2(SETTINGS_PATH, backup)
                rep.ok(f"backed up settings.json to {backup}")
                current["hooks"] = pruned_hooks
                SETTINGS_PATH.write_text(json.dumps(current, indent=2) + "\n")
                rep.ok(f"removed {n} council hook handler(s); handlers not matched as council preserved")
        else:
            rep.info(f"{SETTINGS_PATH} has no hooks dict to prune")
    else:
        rep.info(f"no {SETTINGS_PATH}; nothing to unhook")

    # 2. the /council slash command.
    cmd_file = COMMANDS_DIR / "council.md"
    if not cmd_file.exists():
        rep.info(f"no slash command at {cmd_file}")
    elif rep.dry_run:
        rep.info(f"  would remove {cmd_file}")
    else:
        cmd_file.unlink()
        rep.ok(f"removed {cmd_file}")

    # 3. the installed council scripts (leave roster.json, logs/, markers, user data).
    removed = 0
    for name in COUNCIL_FILES:
        f = council_root / name
        if not f.exists():
            continue
        if rep.dry_run:
            rep.info(f"  would remove {f}")
        else:
            f.unlink()
            removed += 1
    if not rep.dry_run:
        rep.ok(f"removed {removed} council script(s) from {council_root}")

    rep.info("")
    if rep.dry_run:
        rep.info("Dry run: nothing was changed.")
    else:
        rep.info("Uninstall complete. Removing hooks takes effect live (the settings "
                 "file-watcher picks it up) -- no restart needed.")
        rep.info(f"  Left in place, yours to delete if you want: user data under "
                 f"{council_root} (roster.json, logs/, markers) and any settings.json "
                 f".pre-*.bak backups.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--council-root", type=Path, default=None,
                        help=f"Where to install the council scripts. "
                             f"Default: {DEFAULT_COUNCIL_ROOT}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions but do not write any files.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files.")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove the council's hooks, the /council command, and "
                             "the installed council scripts, then exit. Re-running "
                             "the installer REINSTALLS.")
    parser.add_argument("--skip-probes", action="store_true",
                        help="Skip the codex / gemini live probes.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress informational output.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rep = Reporter(dry_run=args.dry_run, quiet=args.quiet)
    council_root = (args.council_root or DEFAULT_COUNCIL_ROOT).expanduser()
    if args.uninstall:
        return uninstall(rep, council_root)
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
    check_ground_rules(rep, council_root)
    check_standing_rules(rep)
    check_bubblewrap(rep)
    check_pyside6(rep, council_root)

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
    rep.info("Activation:")
    rep.info("  - The council is LIVE now -- no restart. Claude Code's settings "
             "file-watcher picks up the new hooks, so the PreToolUse gate and the "
             "PostToolUse council fire on your next Write/Edit/NotebookEdit, and the "
             "Stop audit runs when a response ends.")
    rep.info("  - The one piece that waits for a session boundary is the SessionStart "
             "probe (it seeds hardware/disk/python evidence). If Claude Code was "
             "already running, start a new session or /resume to run it; if you "
             "installed before launching Claude Code, it runs from session one.")
    rep.info("  - Confirm it works: make a trivial edit and watch for the council's "
             "verdict (PASS / WARN / BLOCK). The /council command is installed; use "
             "it in a new session.")
    rep.info("")
    rep.info("Next steps:")
    rep.info("  1. Edit the quality bar to match your workflow: "
             f"{council_root / 'council_system_prompt.md'}")
    rep.info("  2. Optionally tune the trigger-phrase list in "
             f"{council_root / 'laziness_gate.py'} AND "
             f"{council_root / 'stop_audit.py'} (edit both) for false positives "
             "specific to your domain.")
    rep.info("  3. Install the BASE rules layer: copy "
             f"{REPO_ROOT / 'starter-prompts' / 'ground-rules.md.template'} to "
             f"{council_root / 'council_ground_rules.md'}. Read it before copying. "
             "Without it, seats receive no ground rules.")
    rep.info("  4. Optionally turn on the standing-rules channel for the rules the "
             "REVIEWED party works under: set COUNCIL_STANDING_RULES_PATH to a file of "
             f"your own instructions ({REPO_ROOT / 'starter-prompts' / 'standing-rules.md.template'} "
             "is a starter). It is OFF unless that variable is set.")
    rep.info("")
    if rep.warnings:
        rep.info(f"{len(rep.warnings)} warning(s) emitted; review above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
