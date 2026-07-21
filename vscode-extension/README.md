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

## Setup

```bash
cd vscode-extension
npm install
npm run compile
```

Then launch via the VS Code Extension Development Host (open this folder,
press F5), or package a `.vsix` with `npx @vscode/vsce package` and install it.

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
