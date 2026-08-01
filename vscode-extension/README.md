# Workers' Council -- VS Code extension

A panel for the council engine (`consult_council.py`). The extension is a thin
shell: it renders the roster editor and consult box, writes `roster.json`, and
spawns the Python engine. Roster validation lives in the engine (its
`--print-roster` flag is the read path), and API keys are read by the engine
from its process environment -- the extension neither reads, stores, nor
displays them.

## Status

v0.1. `npm run compile` is clean against `@types/vscode` pinned to exactly
1.85.0 (matching the `engines.vscode` floor), and `media/main.js` passes
`node --check`. NOT yet exercised inside a running VS Code instance -- the
first F5 launch is still owed, and webview behavior (message wiring, rendering)
is untested until then.

## What it does

- **Roster editor** -- assign each member to a layer (voting / inspector) with
  radio buttons, pick its transport/billing route (subscription codex CLI,
  common OpenRouter key, or direct vendor key where available), set OpenRouter
  model slugs and fallbacks, and add extra OpenRouter-routed members. Saving
  writes `<council root>/roster.json` and immediately shows the ENGINE's
  verdict on it: a rejected roster surfaces the engine's own error list (the
  engine keeps running on its built-in default until the file is fixed), and
  your in-progress edits stay on screen. `roster.json` is global to the
  council install -- every session's fires use it.
- **Consult** -- put a pitch before the council (`--layer reasoning`) and read
  the full output: per-member verdicts and the non-voting layer-2 inspection.
- **Launch GUI** -- open the standalone operator cockpit (tabs: Config, Run,
  Leader, Brain, Metrics) in its own window, from the panel button or the
  command palette (`Council: Launch GUI`). The extension only starts the
  process; the window is independent and outlives the extension host. See
  "The GUI" below for its one extra requirement.

## Setup

```bash
cd vscode-extension
npm install
npm run compile
```

Then launch via the VS Code Extension Development Host (open this folder,
press F5), or package a `.vsix` with `npx @vscode/vsce package` and install it.

## The GUI

The standalone cockpit (`council_gui.py`) carries the council's only third-party
**Python** dependency: **PySide6**. Checked by an AST scan of the direct imports
of every `*.py` under the council root, excluding virtualenvs and `_nogit/`:
across those files PySide6 was the sole non-stdlib, non-local import.
(`validate_brain` also appears, but it is a local module in the sibling `brain/`
directory, imported lazily and degrading to `None` if absent.) The scan is Python
imports only, so it says nothing about the extension's npm devDependencies or
about external executables such as `bwrap` (optional, for the exec-sandbox tool).

**If a `.venv-gui` exists in the council root, no configuration is needed.** The
extension picks the GUI's interpreter in this order:

1. `council.pythonPath`, if you explicitly set it -- an explicit choice always wins;
2. `<council root>/.venv-gui/bin/python3`, if that virtualenv exists;
3. otherwise the default `python3`.

Step 2 matters because the extension host's `python3` *can fail to* import
PySide6: VS Code does not necessarily inherit a login shell's environment. On the
development machine an interactive shell resolved `python3` to a venv carrying
PySide6 while a non-interactive one resolved it to a different interpreter
without it, because `~/.bashrc` returns early for non-interactive shells. Which
interpreter a given VS Code install inherits was not measured -- hence preferring
a venv that sits inside the project, where the intent is unambiguous.

If the GUI cannot start, the extension says so rather than failing silently: a
missing PySide6 is named explicitly and offers both fixes (install into the
current interpreter, or open the `council.pythonPath` setting). Note the reverse
is weaker -- the host reports failures it can observe, meaning a spawn error or
an early non-zero exit. A GUI that starts and then crashes later is not
reported, because by then the window is yours to watch.

## Settings

- `council.rootPath` -- directory containing `consult_council.py`. When empty,
  each workspace folder is searched, then its `council/` and `Council/`
  subdirectories (both spellings: case-sensitive filesystems), then its parent
  directory (covers a workspace opened inside the council root, like this
  extension folder).
- `council.pythonPath` -- Python interpreter for the engine (default
  `python3`).

## Keys

The engine reads `OPENROUTER_API_KEY` (and any direct-vendor keys) from its
environment, which the spawned process inherits from VS Code. Launch VS Code
from a shell that has them exported. The extension itself never touches key
material.
