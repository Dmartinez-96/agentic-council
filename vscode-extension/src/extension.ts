// Workers' Council -- VS Code panel for the council engine (consult_council.py).
//
// The extension is a THIN SHELL: it renders controls, writes roster.json, and
// spawns the Python engine. Roster validation lives in the engine (the
// --print-roster flag is the read path), and API keys are read by the engine
// from its process environment -- this extension neither reads, stores, nor
// transmits them.

import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';

interface EngineResult {
  code: number;
  stdout: string;
  stderr: string;
}

function councilRoot(): string | undefined {
  const cfg = vscode.workspace.getConfiguration('council');
  const configured = cfg.get<string>('rootPath', '');
  if (configured) {
    return fs.existsSync(path.join(configured, 'consult_council.py'))
      ? configured
      : undefined;
  }
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const base = folder.uri.fsPath;
    // Both subdirectory spellings checked: filesystems here are case-sensitive.
    // The parent ('..') covers the layout where the opened folder sits INSIDE
    // the council root -- e.g. this extension's own directory.
    for (const sub of ['', 'council', 'Council', '..']) {
      const dir = sub ? path.join(base, sub) : base;
      if (fs.existsSync(path.join(dir, 'consult_council.py'))) {
        return dir;
      }
    }
  }
  return undefined;
}

// Reports WHY the root was not found, matching what councilRoot() actually did. When
// council.rootPath is set, councilRoot() checks ONLY that path and returns -- it never
// falls back to the workspace scan -- so claiming a workspace search there would send the
// user looking in the wrong place.
function reportRootNotFound(): void {
  const configured = vscode.workspace
    .getConfiguration('council')
    .get<string>('rootPath', '');
  vscode.window.showErrorMessage(
    configured
      ? "Workers' Council: council.rootPath is set to " +
          `"${configured}", but no consult_council.py was found there. ` +
          'Fix or clear that setting (clearing it enables the workspace search).'
      : "Workers' Council: consult_council.py not found in any workspace " +
          'folder (searched each folder, its council/ and Council/ ' +
          'subdirectories, and its parent directory). Set council.rootPath ' +
          'to point at it directly.',
  );
}

function runEngine(
  root: string,
  args: string[],
  stdin?: string,
): Promise<EngineResult> {
  const python = vscode.workspace
    .getConfiguration('council')
    .get<string>('pythonPath', 'python3');
  return new Promise((resolve) => {
    const child = cp.execFile(
      python,
      [path.join(root, 'consult_council.py'), ...args],
      { cwd: root, maxBuffer: 16 * 1024 * 1024, timeout: 15 * 60 * 1000 },
      (error, stdout, stderr) => {
        // The engine exits 1/2 for WARN/BLOCK verdicts, which execFile
        // surfaces as an error object; a numeric exit code is a verdict,
        // not a failure.
        let code: number;
        if (!error) {
          code = 0;
        } else {
          const c = (error as cp.ExecFileException).code;
          code = typeof c === 'number' ? c : -1;
        }
        resolve({ code, stdout: stdout ?? '', stderr: stderr ?? '' });
      },
    );
    child.stdin?.write(stdin ?? '');
    child.stdin?.end();
  });
}

// How long to wait before treating a launched GUI as "up". The GUI is a long-lived
// window, so there is no success exit code to wait for -- the only observable failure is
// the process dying immediately (a missing PySide6, no display, a syntax error). So we
// watch for an early exit and report it; surviving the window is EVIDENCE the process
// started, not proof the window rendered.
const GUI_LAUNCH_GRACE_MS = 3000;

// The GUI's interpreter, which is NOT always the engine's. Resolution order:
//   1. council.pythonPath when the user actually set it (inspect() distinguishes an
//      explicit value from the packaged default, so an explicit choice always wins);
//   2. <root>/.venv-gui/bin/python3 when that venv exists -- a GUI venv living inside
//      the council root is unambiguously meant for this, and preferring it means the
//      GUI works with no configuration even though the extension host's `python3`
//      usually cannot import PySide6;
//   3. the configured default (`python3`).
function guiPython(root: string): string {
  const cfg = vscode.workspace.getConfiguration('council');
  const inspected = cfg.inspect<string>('pythonPath');
  const explicit =
    inspected?.workspaceFolderValue ??
    inspected?.workspaceValue ??
    inspected?.globalValue;
  if (explicit) {
    return explicit;
  }
  const venvPython = path.join(root, '.venv-gui', 'bin', 'python3');
  if (fs.existsSync(venvPython)) {
    return venvPython;
  }
  return cfg.get<string>('pythonPath', 'python3');
}

function launchGui(root: string): void {
  const python = guiPython(root);
  const script = path.join(root, 'council_gui.py');
  if (!fs.existsSync(script)) {
    vscode.window.showErrorMessage(
      `Workers' Council: council_gui.py not found in ${root}.`,
    );
    return;
  }
  let child: cp.ChildProcess;
  try {
    // detached + ignored stdin so the GUI outlives this extension host; stderr is kept
    // so an immediate failure has something to report.
    child = cp.spawn(python, [script], {
      cwd: root,
      detached: true,
      stdio: ['ignore', 'ignore', 'pipe'],
    });
  } catch (e) {
    vscode.window.showErrorMessage(
      `Workers' Council: could not start ${python}: ${String(e)}`,
    );
    return;
  }

  let stderr = '';
  child.stderr?.on('data', (d: Buffer) => {
    stderr += d.toString();
  });
  child.on('error', (e) => {
    vscode.window.showErrorMessage(
      `Workers' Council: could not start ${python}: ${e.message}`,
    );
  });

  const settled = setTimeout(() => {
    child.removeAllListeners('exit');
    child.unref();
    vscode.window.setStatusBarMessage("Workers' Council: GUI launched", 4000);
  }, GUI_LAUNCH_GRACE_MS);

  child.on('exit', (code) => {
    clearTimeout(settled);
    if (code === 0) {
      return; // the user closed it, or it chose to exit cleanly
    }
    // The one failure worth naming precisely, because it is the expected one on a fresh
    // install: the GUI's only third-party dependency is absent.
    if (/ModuleNotFoundError.*PySide6|No module named ['"]?PySide6/.test(stderr)) {
      // Two genuinely different fixes, and which one is right depends on the user's
      // setup: the interpreter VS Code inherits is often NOT the shell's. If PySide6
      // lives in a virtualenv, pointing council.pythonPath at that venv's python is
      // correct and installing into the inherited interpreter is wasted effort.
      vscode.window
        .showErrorMessage(
          `Workers' Council: the GUI needs PySide6, which ${python} cannot import. ` +
            'Note the extension host may not use the same interpreter as your shell.',
          'Install into this interpreter',
          'Set council.pythonPath',
        )
        .then((choice) => {
          if (choice === 'Install into this interpreter') {
            const term = vscode.window.createTerminal("Workers' Council");
            term.show();
            // pythonPath is user-configurable and may contain spaces, so it is
            // single-quoted with embedded quotes escaped POSIX-style ('\'').
            const q = `'${python.replace(/'/g, `'\\''`)}'`;
            term.sendText(`${q} -m pip install PySide6`);
          } else if (choice === 'Set council.pythonPath') {
            // The command id is not verified in this session, so failure is handled
            // rather than assumed away: if it does not resolve, name the setting so
            // the user can still act.
            void Promise.resolve(
              vscode.commands.executeCommand(
                'workbench.action.openSettings',
                'council.pythonPath',
              ),
            ).then(undefined, () => {
              vscode.window.showInformationMessage(
                "Workers' Council: set the setting `council.pythonPath` to an " +
                  'interpreter that can import PySide6.',
              );
            });
          }
        });
      return;
    }
    const detail = stderr.trim().split('\n').slice(-4).join('\n');
    vscode.window.showErrorMessage(
      `Workers' Council: the GUI exited immediately (code ${code}).` +
        (detail ? ` Last output:\n${detail}` : ''),
    );
  });
}

function getNonce(): string {
  let text = '';
  const chars =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
}

// The ONLY marker files the panel may toggle -- a whitelist, so the webview can never ask
// the host to create or delete an arbitrary path. FAST = all members at low reasoning
// effort; DISABLED = silence the council. Each is read per-fire by the engine (FAST_PATH /
// COUNCIL_ROOT/"DISABLED"), so a change takes effect on the NEXT fire (no restart), and both
// live in the council root, so they are GLOBAL to the install (every session's next fire).
const MARKERS = ['FAST', 'DISABLED'] as const;

function reportMarkers(panel: vscode.WebviewPanel, root: string): void {
  const state: Record<string, boolean> = {};
  for (const name of MARKERS) {
    state[name] = fs.existsSync(path.join(root, name));
  }
  panel.webview.postMessage({ type: 'markers', state });
}

function panelHtml(webview: vscode.Webview, extensionUri: vscode.Uri): string {
  const nonce = getNonce();
  const scriptUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'main.js'),
  );
  const styleUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'main.css'),
  );
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${styleUri}" rel="stylesheet">
<title>Workers' Council</title>
</head>
<body>
<h1>Workers' Council</h1>
<section id="gui-section">
  <h2>Standalone GUI</h2>
  <p class="hint">Opens the full operator cockpit (Config, Run, Leader, Brain, Metrics) in
    its own window. It needs PySide6 on the interpreter set by <code>council.pythonPath</code>,
    which is NOT necessarily the one your shell uses.</p>
  <div class="row">
    <button id="launch-gui">Launch GUI</button>
  </div>
</section>
<section id="roster-section">
  <h2>Roster</h2>
  <p id="roster-status"></p>
  <div id="roster-editor"></div>
  <div class="row">
    <button id="save-roster">Save roster.json</button>
    <button id="reset-roster">Reset to built-in default</button>
    <button id="reload-roster">Reload</button>
  </div>
  <div id="roster-problems"></div>
</section>
<section id="leader-section">
  <h2>Leader (tool-using actor)</h2>
  <p class="hint">The leader takes tool-using turns and is the ONLY role that can write
    files (each write goes through the council wall). "Claude Code harness" is the default:
    no council-native leader. Saved with the roster (top-level <code>leader</code> key).</p>
  <div class="row">
    <label>transport
      <select id="leader-transport">
        <option value="">Claude Code harness (default -- no council-native leader)</option>
        <option value="openrouter">OpenRouter (any model can lead)</option>
        <option value="codex_subprocess">codex -- subscription CLI</option>
        <option value="gemini_rest">gemini -- direct vendor key</option>
        <option value="deepseek_https">deepseek -- direct vendor key</option>
      </select>
    </label>
    <label>name <input id="leader-name" type="text" placeholder="e.g. claude-opus"></label>
    <label>model <input id="leader-model" type="text" placeholder="OpenRouter model slug"></label>
    <label>fallback <input id="leader-fallback" type="text" placeholder="optional fallback"></label>
  </div>
</section>
<section id="markers-section">
  <h2>Switches (take effect on the next fire -- global to all sessions)</h2>
  <label><input type="checkbox" id="marker-FAST"> FAST -- all members run at low reasoning effort</label>
  <label><input type="checkbox" id="marker-DISABLED"> DISABLED -- silence the council's auto-review hook</label>
  <p id="markers-status"></p>
</section>
<section id="consult-section">
  <h2>Consult</h2>
  <textarea id="pitch" rows="8"
    placeholder="Describe the decision / design / diff to put before the council..."></textarea>
  <div class="row">
    <button id="run-consult">Run council (reasoning layer)</button>
    <span id="consult-status"></span>
  </div>
  <pre id="verdict-output"></pre>
</section>
<script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand('council.launchGui', () => {
      const root = councilRoot();
      if (!root) {
        reportRootNotFound();
        return;
      }
      launchGui(root);
    }),
  );
  context.subscriptions.push(
    vscode.commands.registerCommand('council.openPanel', () => {
      const root = councilRoot();
      if (!root) {
        reportRootNotFound();
        return;
      }
      const panel = vscode.window.createWebviewPanel(
        'councilPanel',
        "Workers' Council",
        vscode.ViewColumn.One,
        {
          enableScripts: true,
          retainContextWhenHidden: true,
          localResourceRoots: [
            vscode.Uri.joinPath(context.extensionUri, 'media'),
          ],
        },
      );
      panel.webview.html = panelHtml(panel.webview, context.extensionUri);
      panel.webview.onDidReceiveMessage(
        async (msg: {
          type?: string;
          members?: unknown;
          leader?: unknown;
          pitch?: unknown;
          name?: unknown;
          on?: unknown;
        }) => {
          switch (msg?.type) {
            case 'loadRoster': {
              const res = await runEngine(root, ['--print-roster']);
              panel.webview.postMessage({
                type: 'roster',
                ok: res.code === 0,
                raw: res.stdout,
                stderr: res.stderr,
              });
              break;
            }
            case 'saveRoster': {
              // The leader lives in roster.json's top-level "leader" key; write it only
              // when one is configured (omitting it means the Claude Code harness leads,
              // the engine default). The engine validates both on the --print-roster below.
              const roster: { members: unknown; leader?: unknown } = {
                members: msg.members,
              };
              if (msg.leader) {
                roster.leader = msg.leader;
              }
              try {
                fs.writeFileSync(
                  path.join(root, 'roster.json'),
                  JSON.stringify(roster, null, 2) + '\n',
                );
              } catch (e) {
                panel.webview.postMessage({
                  type: 'roster',
                  ok: false,
                  raw: '',
                  stderr: `could not write roster.json: ${String(e)}`,
                });
                break;
              }
              const res = await runEngine(root, ['--print-roster']);
              panel.webview.postMessage({
                type: 'roster',
                ok: res.code === 0,
                saved: true,
                raw: res.stdout,
                stderr: res.stderr,
              });
              break;
            }
            case 'resetRoster': {
              const rosterPath = path.join(root, 'roster.json');
              try {
                if (fs.existsSync(rosterPath)) {
                  fs.unlinkSync(rosterPath);
                }
              } catch (e) {
                panel.webview.postMessage({
                  type: 'roster',
                  ok: false,
                  raw: '',
                  stderr: `could not delete roster.json: ${String(e)}`,
                });
                break;
              }
              const res = await runEngine(root, ['--print-roster']);
              panel.webview.postMessage({
                type: 'roster',
                ok: res.code === 0,
                reset: true,
                raw: res.stdout,
                stderr: res.stderr,
              });
              break;
            }
            case 'consult': {
              panel.webview.postMessage({ type: 'consultStarted' });
              const res = await runEngine(
                root,
                ['--layer', 'reasoning'],
                String(msg.pitch ?? ''),
              );
              panel.webview.postMessage({
                type: 'verdict',
                code: res.code,
                stdout: res.stdout,
                stderr: res.stderr,
              });
              break;
            }
            case 'launchGui': {
              launchGui(root);
              break;
            }
            case 'loadMarkers': {
              reportMarkers(panel, root);
              break;
            }
            case 'setMarker': {
              // Whitelisted names only (MARKERS) -- the webview can never make the host
              // touch/delete an arbitrary path. `on` creates the empty marker file; else
              // it is removed if present. The engine reads it on its next fire.
              const name = String(msg.name ?? '');
              if ((MARKERS as readonly string[]).includes(name)) {
                const markerPath = path.join(root, name);
                try {
                  if (msg.on) {
                    fs.writeFileSync(markerPath, '');
                  } else if (fs.existsSync(markerPath)) {
                    fs.unlinkSync(markerPath);
                  }
                } catch (e) {
                  panel.webview.postMessage({
                    type: 'markerError',
                    stderr: `could not update ${name}: ${String(e)}`,
                  });
                }
              }
              reportMarkers(panel, root);
              break;
            }
          }
        },
        undefined,
        context.subscriptions,
      );
    }),
  );
}

export function deactivate(): void {}
