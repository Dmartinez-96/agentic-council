// Webview logic for the Workers' Council panel. Talks to the extension host
// via postMessage; the host talks to the Python engine. Roster validation
// happens in the engine -- this file only renders what the engine reports.
(function () {
  const vscode = acquireVsCodeApi();

  const TIERS = ['voting', 'inspector'];
  // Transports selectable per member. The engine enforces that vendor-bespoke
  // transports are only usable under their canonical names; the UI mirrors
  // that constraint instead of offering rosters the engine would reject.
  const TRANSPORTS_BY_NAME = {
    codex: [
      { id: 'codex_subprocess', label: 'subscription (codex CLI)' },
      { id: 'openrouter', label: 'OpenRouter key' },
    ],
    gemini: [
      { id: 'openrouter', label: 'OpenRouter key' },
      { id: 'gemini_rest', label: 'direct vendor key (Gemini API)' },
    ],
    deepseek: [
      { id: 'openrouter', label: 'OpenRouter key' },
      { id: 'deepseek_https', label: 'direct vendor key (DeepSeek API)' },
    ],
  };
  const DEFAULT_TRANSPORTS = [{ id: 'openrouter', label: 'OpenRouter key' }];

  let members = [];
  // Leader state: null = the Claude Code harness leads (no council-native leader);
  // otherwise { name, transport, model, fallback_model }. Lives in roster.json's top-level
  // "leader" key and is saved alongside the members by the Save button.
  let leader = null;

  // The direct-vendor leader transports are gated by the engine to a canonical name and a
  // pinned model; the UI mirrors that (name fixed, model disabled) so it never offers a
  // leader the engine would reject. openrouter is the any-model path.
  const LEADER_CANONICAL = {
    codex_subprocess: 'codex',
    gemini_rest: 'gemini',
    deepseek_https: 'deepseek',
  };

  const rosterStatus = document.getElementById('roster-status');
  const editor = document.getElementById('roster-editor');
  const problems = document.getElementById('roster-problems');
  const verdictOut = document.getElementById('verdict-output');
  const consultStatus = document.getElementById('consult-status');
  const runBtn = document.getElementById('run-consult');
  const leaderTransport = document.getElementById('leader-transport');
  const leaderName = document.getElementById('leader-name');
  const leaderModel = document.getElementById('leader-model');
  const leaderFallback = document.getElementById('leader-fallback');
  const markerFast = document.getElementById('marker-FAST');
  const markerDisabled = document.getElementById('marker-DISABLED');
  const markersStatus = document.getElementById('markers-status');

  function transportsFor(name) {
    return TRANSPORTS_BY_NAME[name] || DEFAULT_TRANSPORTS;
  }

  // --- Leader configuration (roster.json top-level "leader" key) ---------------
  function renderLeader() {
    const t = leader ? leader.transport : '';
    leaderTransport.value = t;
    const canonical = LEADER_CANONICAL[t];
    // name: fixed to the canonical for a direct-vendor transport; free for openrouter;
    // irrelevant (disabled) for the harness default.
    leaderName.value = canonical || (leader ? leader.name || '' : '');
    leaderName.disabled = t === '' || Boolean(canonical);
    // model: only the openrouter transport takes a user-supplied slug.
    leaderModel.value = t === 'openrouter' && leader ? leader.model || '' : '';
    leaderModel.disabled = t !== 'openrouter';
    leaderModel.title = leaderModel.disabled
      ? 'Only OpenRouter takes a model slug; direct-vendor leaders are pinned by the engine.'
      : 'Any OpenRouter provider/model slug (e.g. deepseek/deepseek-v4-pro)';
    // fallback: openrouter and the codex subscription route read a fallback.
    const takesFb = t === 'openrouter' || t === 'codex_subprocess';
    leaderFallback.value = takesFb && leader ? leader.fallback_model || '' : '';
    leaderFallback.disabled = !takesFb;
  }

  function updateLeaderFromInputs() {
    const t = leaderTransport.value;
    if (!t) {
      leader = null;
      return;
    }
    const canonical = LEADER_CANONICAL[t];
    leader = {
      name: canonical || leaderName.value.trim(),
      transport: t,
      model: t === 'openrouter' ? leaderModel.value.trim() : '',
      fallback_model:
        t === 'openrouter' || t === 'codex_subprocess'
          ? leaderFallback.value.trim() || null
          : null,
    };
  }

  // The roster.json shape the engine validates: omit "leader" entirely when none is
  // configured (the harness leads), and only write a fallback for the transports that read
  // one -- mirroring toRosterJson()'s member rules so the engine does not reject the save.
  function leaderToJson() {
    if (!leader || !leader.transport) {
      return undefined;
    }
    const rec = { name: leader.name, transport: leader.transport };
    if (leader.transport === 'openrouter') {
      rec.model = leader.model || '';
    }
    if (
      (leader.transport === 'openrouter' || leader.transport === 'codex_subprocess') &&
      leader.fallback_model
    ) {
      rec.fallback_model = leader.fallback_model;
    }
    return rec;
  }

  function render() {
    editor.textContent = '';
    const table = document.createElement('table');
    const head = document.createElement('tr');
    for (const h of ['member', 'layer', 'transport / billing',
                     'model (OpenRouter routes)', 'fallback model', '']) {
      const th = document.createElement('th');
      th.textContent = h;
      head.appendChild(th);
    }
    table.appendChild(head);

    members.forEach(function (m, idx) {
      const tr = document.createElement('tr');

      const nameTd = document.createElement('td');
      nameTd.textContent = m.name;
      tr.appendChild(nameTd);

      const tierTd = document.createElement('td');
      for (const tier of TIERS) {
        const label = document.createElement('label');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'tier-' + idx;
        radio.checked = m.tier === tier;
        radio.addEventListener('change', function () { m.tier = tier; });
        label.className = 'tier-choice';
        label.appendChild(radio);
        label.appendChild(document.createTextNode(' ' + tier));
        tierTd.appendChild(label);
      }
      tr.appendChild(tierTd);

      const trTd = document.createElement('td');
      const select = document.createElement('select');
      for (const t of transportsFor(m.name)) {
        const opt = document.createElement('option');
        opt.value = t.id;
        opt.textContent = t.label;
        opt.selected = m.transport === t.id;
        select.appendChild(opt);
      }
      select.addEventListener('change', function () {
        m.transport = select.value;
        render();
      });
      trTd.appendChild(select);
      tr.appendChild(trTd);

      const modelTd = document.createElement('td');
      const modelInput = document.createElement('input');
      modelInput.type = 'text';
      modelInput.value = m.model || '';
      modelInput.disabled = m.transport !== 'openrouter';
      modelInput.title = modelInput.disabled
        ? 'Direct-vendor transports read their model from the engine constants.'
        : 'OpenRouter model slug, e.g. deepseek/deepseek-v4-pro';
      modelInput.addEventListener('input', function () {
        m.model = modelInput.value;
      });
      modelTd.appendChild(modelInput);
      tr.appendChild(modelTd);

      const fbTd = document.createElement('td');
      const fbInput = document.createElement('input');
      fbInput.type = 'text';
      fbInput.value = m.fallback_model || '';
      fbInput.disabled = !(m.transport === 'openrouter'
                           || m.transport === 'codex_subprocess');
      fbInput.addEventListener('input', function () {
        m.fallback_model = fbInput.value;
      });
      fbTd.appendChild(fbInput);
      tr.appendChild(fbTd);

      const rmTd = document.createElement('td');
      if (!TRANSPORTS_BY_NAME[m.name]) {
        const rm = document.createElement('button');
        rm.textContent = 'remove';
        rm.addEventListener('click', function () {
          members.splice(idx, 1);
          render();
        });
        rmTd.appendChild(rm);
      }
      tr.appendChild(rmTd);

      table.appendChild(tr);
    });
    editor.appendChild(table);

    const addRow = document.createElement('div');
    addRow.className = 'row';
    const addName = document.createElement('input');
    addName.type = 'text';
    addName.placeholder = 'new member name';
    const addModel = document.createElement('input');
    addModel.type = 'text';
    addModel.placeholder = 'OpenRouter model slug';
    const addBtn = document.createElement('button');
    addBtn.textContent = 'Add OpenRouter member';
    addBtn.addEventListener('click', function () {
      const name = addName.value.trim();
      const model = addModel.value.trim();
      if (!name || !model) {
        return;
      }
      members.push({
        name: name,
        tier: 'inspector',
        transport: 'openrouter',
        model: model,
        fallback_model: null,
      });
      render();
    });
    addRow.appendChild(addName);
    addRow.appendChild(addModel);
    addRow.appendChild(addBtn);
    editor.appendChild(addRow);
  }

  function showProblems(errors, warnings) {
    problems.textContent = '';
    (errors || []).forEach(function (e) {
      const div = document.createElement('div');
      div.className = 'error';
      div.textContent = 'rejected: ' + e;
      problems.appendChild(div);
    });
    (warnings || []).forEach(function (w) {
      const div = document.createElement('div');
      div.className = 'warning';
      div.textContent = 'warning: ' + w;
      problems.appendChild(div);
    });
  }

  function toRosterJson() {
    return members.map(function (m) {
      const rec = { name: m.name, tier: m.tier, transport: m.transport };
      if (m.transport === 'openrouter') {
        rec.model = m.model || '';
      }
      // Only the transports that read a fallback get one written: a stale
      // value left over from a transport switch would otherwise be saved and
      // rejected by the engine (fallback_model is an error on gemini_rest /
      // deepseek_https).
      const takesFallback = m.transport === 'openrouter'
        || m.transport === 'codex_subprocess';
      if (takesFallback && m.fallback_model) {
        rec.fallback_model = m.fallback_model;
      }
      return rec;
    });
  }

  document.getElementById('save-roster').addEventListener('click', function () {
    updateLeaderFromInputs();
    vscode.postMessage({
      type: 'saveRoster',
      members: toRosterJson(),
      leader: leaderToJson(),
    });
  });

  leaderTransport.addEventListener('change', function () {
    updateLeaderFromInputs();
    renderLeader();
  });
  [leaderName, leaderModel, leaderFallback].forEach(function (el) {
    el.addEventListener('input', updateLeaderFromInputs);
  });

  [markerFast, markerDisabled].forEach(function (cb) {
    cb.addEventListener('change', function () {
      vscode.postMessage({
        type: 'setMarker',
        name: cb.id.slice('marker-'.length),
        on: cb.checked,
      });
    });
  });
  document.getElementById('reset-roster').addEventListener('click', function () {
    vscode.postMessage({ type: 'resetRoster' });
  });
  document.getElementById('reload-roster').addEventListener('click', function () {
    vscode.postMessage({ type: 'loadRoster' });
  });
  runBtn.addEventListener('click', function () {
    const pitch = document.getElementById('pitch').value;
    if (!pitch.trim()) {
      return;
    }
    vscode.postMessage({ type: 'consult', pitch: pitch });
  });

  // The GUI is a separate window owned by the host, so there is no reply to render here.
  // The host notifies on failures it can SEE: a spawn error, or a non-zero exit within
  // its grace window (missing PySide6, wrong interpreter). It does NOT notify on a clean
  // exit, nor on a crash after that window -- those are silent by design, since by then
  // the window is the user's to watch. Guarded because the button lives in host-rendered
  // HTML: if a stale build ships without it, a null here would kill the whole script.
  const launchGuiBtn = document.getElementById('launch-gui');
  if (launchGuiBtn) {
    launchGuiBtn.addEventListener('click', function () {
      vscode.postMessage({ type: 'launchGui' });
    });
  }

  window.addEventListener('message', function (event) {
    const msg = event.data;
    if (msg.type === 'roster') {
      if (!msg.ok) {
        rosterStatus.textContent = 'engine error reading roster';
        showProblems([msg.stderr], []);
        return;
      }
      let data;
      try {
        data = JSON.parse(msg.raw);
      } catch (e) {
        rosterStatus.textContent = 'engine returned unparseable roster JSON';
        showProblems([String(e)], []);
        return;
      }
      const note = msg.saved ? ' (saved)' : msg.reset ? ' (reset)' : '';
      rosterStatus.textContent = 'active source: ' + data.source + note;
      showProblems(data.errors, data.warnings);
      // On a rejected SAVE the engine reports the built-in default it fell
      // back to; keep the user's editing state on screen so their work is
      // not lost -- the error list above tells them what to fix.
      if (!(msg.saved && data.errors && data.errors.length)) {
        members = data.members;
        // The leader lives in the top-level "leader" key. A CONFIGURED leader has a
        // transport; the default harness leader is reported as {name, note} with no
        // transport -> treat that as "no council-native leader" (null).
        leader = data.leader && data.leader.transport ? {
          name: data.leader.name,
          transport: data.leader.transport,
          model: data.leader.model || '',
          fallback_model: data.leader.fallback_model || null,
        } : null;
      }
      render();
      renderLeader();
    } else if (msg.type === 'consultStarted') {
      consultStatus.textContent = 'council deliberating...';
      runBtn.disabled = true;
    } else if (msg.type === 'verdict') {
      runBtn.disabled = false;
      consultStatus.textContent = '';
      verdictOut.textContent = msg.stdout
        + (msg.stderr ? '\n--- stderr ---\n' + msg.stderr : '');
      verdictOut.className = msg.stdout.indexOf('VERDICT: PASS') === 0 ? 'pass'
        : msg.stdout.indexOf('VERDICT: BLOCK') === 0 ? 'block' : 'warn';
    } else if (msg.type === 'markers') {
      markerFast.checked = Boolean(msg.state && msg.state.FAST);
      markerDisabled.checked = Boolean(msg.state && msg.state.DISABLED);
    } else if (msg.type === 'markerError') {
      markersStatus.textContent = msg.stderr || '';
    }
  });

  vscode.postMessage({ type: 'loadRoster' });
  vscode.postMessage({ type: 'loadMarkers' });
})();
