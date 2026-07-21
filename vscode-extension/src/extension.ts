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
    vscode.commands.registerCommand('council.openPanel', () => {
      const root = councilRoot();
      if (!root) {
        vscode.window.showErrorMessage(
          "Workers' Council: consult_council.py not found via council.rootPath " +
            'or any workspace folder (searched each folder, its council/ and ' +
            'Council/ subdirectories, and its parent directory).',
        );
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
