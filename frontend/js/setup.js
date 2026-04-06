// ── State ──────────────────────────────────────────────────────────────────
let connectionData  = null;   // {server, database, user, password}
let schemaData      = null;   // {db_name, server, tables: [...]}
let _draftGenerated = false;  // prevent double-generation if user navigates back/forward
const TOTAL_STEPS   = 5;

// ── Step navigation ────────────────────────────────────────────────────────
function goTo(n) {
  for (let i = 1; i <= TOTAL_STEPS; i++) {
    document.getElementById('panel-' + i).classList.toggle('hidden', i !== n);
    const dot = document.getElementById('dot-' + i);
    dot.classList.toggle('active', i === n);
    dot.classList.toggle('done',   i < n);
  }
  if (n === 4 && !_draftGenerated) autoGenerateKnowledge();
  if (n === 5) buildDoneScreen();
  window.scrollTo(0, 0);
}

// ── Status helpers ─────────────────────────────────────────────────────────
function setStatus(id, type, msg) {
  const el = document.getElementById(id);
  el.className = 'status show ' + type;
  el.textContent = msg;
}
function clearStatus(id) {
  const el = document.getElementById(id);
  el.className = 'status';
  el.textContent = '';
}

function setLoading(btnId, loading, label) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.disabled = loading;
  btn.innerHTML = loading
    ? `<span class="spinner"></span>${label || 'Loading…'}`
    : (btn.dataset.label || btn.textContent);
  if (!loading && btn.dataset.label) btn.textContent = btn.dataset.label;
}

function initBtn(btnId) {
  const btn = document.getElementById(btnId);
  if (btn && !btn.dataset.label) btn.dataset.label = btn.textContent;
}

// ── Step 2: Database Connection ─────────────────────────────────────────────
async function testConnection() {
  initBtn('btn-test');
  const server   = document.getElementById('inp-server').value.trim();
  const database = document.getElementById('inp-db').value.trim();
  const user     = document.getElementById('inp-user').value.trim();
  const password = document.getElementById('inp-pass').value.trim();

  if (!server || !database || !user || !password) {
    setStatus('conn-status', 'error', 'All fields are required.');
    return;
  }

  clearStatus('conn-status');
  setLoading('btn-test', true, 'Testing…');

  try {
    const res  = await fetch('/setup/test-connection', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({server, database, user, password}),
    });
    const data = await res.json();
    if (data.success) {
      setStatus('conn-status', 'success', `✓ ${data.message || 'Connection successful.'}`);
      connectionData = {server, database, user, password};
      await checkPermissions(server, database, user, password);
    } else {
      setStatus('conn-status', 'error', '✗ ' + data.error);
      hidePerm();
    }
  } catch (err) {
    setStatus('conn-status', 'error', '✗ Network error: ' + err.message);
    hidePerm();
  }
  setLoading('btn-test', false);
}

function hidePerm() {
  document.getElementById('perm-box').className = 'perm-box';
  document.getElementById('perm-sql').className = 'sql-helper';
}

function toggleSqlHelper() {
  const body = document.getElementById('perm-sql-body');
  const chev = document.getElementById('sql-chevron');
  const open = body.classList.toggle('open');
  chev.textContent = open ? '▲' : '▼';
}

async function checkPermissions(server, database, user, password) {
  const box = document.getElementById('perm-box');
  box.className = 'perm-box unknown show';
  document.getElementById('perm-title').textContent = 'Checking permissions…';
  document.getElementById('perm-detail').textContent = '';

  const btn = document.getElementById('btn-next-2');
  btn.disabled = true;

  try {
    const res  = await fetch('/setup/check-permissions', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({server, database, user, password}),
    });
    const data = await res.json();

    if (!data.success) {
      box.className = 'perm-box unknown show';
      document.getElementById('perm-title').textContent = '⚠ Could not verify permissions.';
      document.getElementById('perm-detail').textContent = 'Make sure this is a read-only user.';
      btn.disabled = false;
      btn.textContent = 'Continue →';
      btn.className = 'btn btn-primary';
      return;
    }

    const level  = data.access_level;
    const sqlTxt = readonlySql(database);

    if (level === 'readonly') {
      box.className = 'perm-box readonly show';
      document.getElementById('perm-title').textContent = '✓ Read-only access confirmed.';
      document.getElementById('perm-detail').textContent = 'This user can only read data.';
      document.getElementById('perm-sql').className = 'sql-helper';
      btn.disabled   = false;
      btn.textContent = 'Continue →';
      btn.className  = 'btn btn-primary';

    } else if (level === 'warning') {
      box.className = 'perm-box warning show';
      document.getElementById('perm-title').textContent = '⚠ Write permissions detected.';
      document.getElementById('perm-detail').textContent =
        data.message + ' We strongly recommend creating a read-only user.';
      document.getElementById('perm-sql').className = 'sql-helper show';
      document.getElementById('perm-sql-text').textContent = sqlTxt;
      btn.disabled   = false;
      btn.textContent = 'Continue Anyway (Not Recommended)';
      btn.className  = 'btn btn-warn';

    } else if (level === 'blocked') {
      box.className = 'perm-box blocked show';
      document.getElementById('perm-title').textContent = '✗ Admin/owner privileges detected.';
      document.getElementById('perm-detail').textContent =
        data.message + ' Please create a read-only user before continuing.';
      document.getElementById('perm-sql').className = 'sql-helper show';
      document.getElementById('perm-sql-text').textContent = sqlTxt;
      btn.disabled   = true;
      btn.textContent = 'Cannot Continue — Please Use a Read-Only User';
      btn.className  = 'btn btn-primary';

    } else {
      box.className = 'perm-box unknown show';
      document.getElementById('perm-title').textContent = '⚠ Could not verify permissions.';
      document.getElementById('perm-detail').textContent = 'Make sure this is a read-only user.';
      document.getElementById('perm-sql').className = 'sql-helper';
      btn.disabled   = false;
      btn.textContent = 'Continue →';
      btn.className  = 'btn btn-primary';
    }

  } catch (err) {
    box.className = 'perm-box unknown show';
    document.getElementById('perm-title').textContent = '⚠ Could not verify permissions.';
    document.getElementById('perm-detail').textContent = 'Make sure this is a read-only user.';
    btn.disabled   = false;
    btn.textContent = 'Continue →';
    btn.className  = 'btn btn-primary';
  }
}

function readonlySql(dbName) {
  return `-- Run in SQL Server Management Studio or sqlcmd:
CREATE LOGIN optiflow_reader WITH PASSWORD = 'choose_a_strong_password';
USE [${dbName}];
CREATE USER optiflow_reader FOR LOGIN optiflow_reader;
ALTER ROLE db_datareader ADD MEMBER optiflow_reader;`;
}

document.getElementById('inp-pass').addEventListener('keydown', e => {
  if (e.key === 'Enter') testConnection();
});

// ── Step 3: Schema Discovery ───────────────────────────────────────────────
async function discoverSchema() {
  initBtn('btn-disc');
  clearStatus('disc-status');
  setLoading('btn-disc', true, 'Scanning tables…');
  setStatus('disc-status', 'info', 'Connecting and scanning your database. This may take 30–60 seconds for large databases…');

  try {
    const res  = await fetch('/setup/discover-schema', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(connectionData),
    });
    const data = await res.json();

    if (data.success) {
      schemaData = data;
      setStatus('disc-status', 'success',
        `✓ Discovered ${data.tables.length} tables — credentials and schema saved`);

      renderTableGrid(data.tables);

      document.getElementById('disc-subtitle').textContent =
        `Connected to ${data.db_name} on ${data.server}`;

      document.getElementById('btn-next-3').disabled = false;
      document.getElementById('btn-disc').style.display = 'none';
    } else {
      setStatus('disc-status', 'error', '✗ ' + data.error);
    }
  } catch (err) {
    setStatus('disc-status', 'error', '✗ ' + err.message);
  }
  setLoading('btn-disc', false);
}

function renderTableGrid(tables) {
  const grid = document.getElementById('tables-grid');
  grid.innerHTML = '';
  tables.forEach(t => {
    const card = document.createElement('div');
    card.className = 'table-card';
    card.innerHTML = `
      <div class="table-card-name">${escHtml(t.name)}</div>
      <div class="table-card-meta">${t.row_count.toLocaleString()} rows</div>
      <div class="table-card-meta">${t.columns.length} columns</div>
    `;
    grid.appendChild(card);
  });
  grid.style.display = 'grid';
}

// ── Step 5: Done ───────────────────────────────────────────────────────────
function buildDoneScreen() {
  const tables    = schemaData ? schemaData.tables : [];
  const totalRows = tables.reduce((s, t) => s + t.row_count, 0);
  const dbName    = schemaData ? schemaData.db_name : '—';

  document.getElementById('done-subtitle').textContent =
    `Connected to ${dbName}. OptiFlow is ready to answer your questions.`;

  const grid = document.getElementById('done-grid');
  grid.innerHTML = `
    <div class="done-stat">
      <div class="done-stat-val">${tables.length}</div>
      <div class="done-stat-lbl">Tables discovered</div>
    </div>
    <div class="done-stat">
      <div class="done-stat-val">${totalRows.toLocaleString()}</div>
      <div class="done-stat-lbl">Total rows</div>
    </div>
  `;
}

// ── Step 1: AI Provider ─────────────────────────────────────────────────────
const _DEFAULT_MODELS = {
  anthropic: 'claude-sonnet-4-20250514',
  openai:    'gpt-4o',
  custom:    '',
};

function onProviderChange() {
  const provider = document.getElementById('inp-ai-provider').value;
  const modelEl  = document.getElementById('inp-ai-model');
  const epGroup  = document.getElementById('ai-endpoint-group');
  if (!modelEl.value || Object.values(_DEFAULT_MODELS).includes(modelEl.value)) {
    modelEl.value = _DEFAULT_MODELS[provider] || '';
  }
  epGroup.style.display = provider === 'custom' ? '' : 'none';
  _updateAiNextBtn();
}

function onLocalToggle() {
  const enabled = document.getElementById('chk-local').checked;
  document.getElementById('local-fields').style.display = enabled ? '' : 'none';
}

function _updateAiNextBtn() {
  const key = document.getElementById('inp-ai-key').value.trim();
  document.getElementById('btn-next-1').disabled = !key;
}

document.addEventListener('DOMContentLoaded', function() {
  const keyEl = document.getElementById('inp-ai-key');
  if (keyEl) keyEl.addEventListener('input', _updateAiNextBtn);
  const modelEl = document.getElementById('inp-ai-model');
  if (modelEl && !modelEl.value) modelEl.value = _DEFAULT_MODELS.anthropic;
});

async function testAiKey() {
  const provider  = document.getElementById('inp-ai-provider').value;
  const api_key   = document.getElementById('inp-ai-key').value.trim();
  const model     = document.getElementById('inp-ai-model').value.trim();
  const endpoint  = document.getElementById('inp-ai-endpoint').value.trim();

  if (!api_key) {
    setStatus('ai-test-status', 'error', '✗ Please enter an API key first.');
    return;
  }

  const btn = document.getElementById('btn-test-ai');
  btn.disabled = true;
  btn.textContent = 'Testing…';
  clearStatus('ai-test-status');
  setStatus('ai-test-status', 'info', 'Sending test request…');

  try {
    const res  = await fetch('/setup/test-ai-provider', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ provider, api_key, model, custom_endpoint: endpoint }),
    });
    const data = await res.json();
    if (data.success) {
      setStatus('ai-test-status', 'success', '✓ API key works! You can continue.');
      document.getElementById('btn-next-1').disabled = false;
    } else {
      setStatus('ai-test-status', 'error', '✗ ' + (data.error || 'Test failed.'));
    }
  } catch (err) {
    setStatus('ai-test-status', 'error', '✗ Network error: ' + err.message);
  }

  btn.disabled = false;
  btn.textContent = 'Test API Key';
}

async function testOllama() {
  const endpoint = document.getElementById('inp-ollama-url').value.trim();
  const btn = document.getElementById('btn-test-ollama');
  btn.disabled = true;
  btn.textContent = 'Testing…';
  clearStatus('ollama-test-status');
  setStatus('ollama-test-status', 'info', 'Connecting to Ollama…');

  try {
    const res  = await fetch('/setup/test-ollama', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ endpoint }),
    });
    const data = await res.json();
    if (data.success) {
      const modelList = data.models && data.models.length
        ? ' Models: ' + data.models.slice(0, 4).join(', ')
        : '';
      setStatus('ollama-test-status', 'success', '✓ Ollama is reachable.' + modelList);
    } else {
      setStatus('ollama-test-status', 'error', '✗ ' + (data.error || 'Could not connect.'));
    }
  } catch (err) {
    setStatus('ollama-test-status', 'error', '✗ Network error: ' + err.message);
  }

  btn.disabled = false;
  btn.textContent = 'Test Connection';
}

async function saveAiConfig() {
  const provider        = document.getElementById('inp-ai-provider').value;
  const api_key         = document.getElementById('inp-ai-key').value.trim();
  const model           = document.getElementById('inp-ai-model').value.trim();
  const custom_endpoint = document.getElementById('inp-ai-endpoint').value.trim();
  const local_enabled   = document.getElementById('chk-local').checked;
  const local_endpoint  = document.getElementById('inp-ollama-url').value.trim();
  const local_model     = document.getElementById('inp-ollama-model').value.trim();

  if (!api_key) {
    setStatus('ai-test-status', 'error', '✗ Please enter an API key.');
    return;
  }

  const btn = document.getElementById('btn-next-1');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const res  = await fetch('/setup/save-ai-config', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ provider, api_key, model, custom_endpoint,
                                local_enabled, local_endpoint, local_model }),
    });
    const data = await res.json();
    if (data.success) {
      goTo(2);
    } else {
      setStatus('ai-test-status', 'error', '✗ ' + (data.error || 'Save failed.'));
      btn.disabled = false;
      btn.textContent = 'Save & Continue →';
    }
  } catch (err) {
    setStatus('ai-test-status', 'error', '✗ Network error: ' + err.message);
    btn.disabled = false;
    btn.textContent = 'Save & Continue →';
  }
}

// ── Step 4: Company Knowledge ──────────────────────────────────────────────

function _showTeachError(msg) {
  const el = document.getElementById('teach-error');
  el.textContent = '✗ ' + msg;
  el.style.display = 'block';
}

function switchTeachTab(tab) {
  const textarea = document.getElementById('inp-company-md');
  const preview  = document.getElementById('md-preview');
  const tabEdit  = document.getElementById('tab-edit');
  const tabPrev  = document.getElementById('tab-preview');

  if (tab === 'preview') {
    preview.innerHTML = _mdToHtml(textarea.value);
    textarea.style.display = 'none';
    preview.style.display  = 'block';
    tabEdit.style.color = '#aaa'; tabEdit.style.borderBottomColor = 'transparent';
    tabPrev.style.color = '#4c6ef5'; tabPrev.style.borderBottomColor = '#4c6ef5';
  } else {
    textarea.style.display = 'block';
    preview.style.display  = 'none';
    tabEdit.style.color = '#4c6ef5'; tabEdit.style.borderBottomColor = '#4c6ef5';
    tabPrev.style.color = '#aaa'; tabPrev.style.borderBottomColor = 'transparent';
  }
}

function _mdToHtml(text) {
  let h = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  h = h.replace(/^### (.+)$/mg, '<h3 style="font-size:14px;font-weight:700;color:#1a1a2e;margin:18px 0 5px">$1</h3>');
  h = h.replace(/^## (.+)$/mg,  '<h2 style="font-size:16px;font-weight:700;color:#1a1a2e;margin:24px 0 8px;padding-top:8px;border-top:1px solid #eee">$1</h2>');
  h = h.replace(/^# (.+)$/mg,   '<h1 style="font-size:20px;font-weight:700;color:#1a1a2e;margin:0 0 18px">$1</h1>');
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/`([^`]+)`/g, '<code style="background:#f0f0f0;padding:1px 5px;border-radius:3px;font-family:monospace;font-size:12px">$1</code>');
  h = h.replace(/^- (.+)$/mg, '<li style="margin:3px 0 3px 20px;list-style:disc">$1</li>');
  h = h.replace(/(<li[^>]*>.*?<\/li>\n?)+/gs, '<ul style="margin:6px 0">$&</ul>');
  h = h.replace(/\n\n+/g, '</p><p style="margin:6px 0">');
  h = h.replace(/\n/g, '<br>');
  return '<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif"><p style="margin:6px 0">' + h + '</p></div>';
}

function _renderFollowup(questions) {
  if (!questions || questions.length === 0) return;
  const section = document.getElementById('followup-section');
  const container = document.getElementById('followup-questions');
  container.innerHTML = '';

  questions.forEach(q => {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-bottom:18px;';
    wrap.innerHTML = `
      <label style="display:block;font-size:13px;font-weight:500;color:#333;margin-bottom:6px;">${escHtml(q.question)}</label>
      <textarea
        class="followup-answer"
        data-question="${escHtml(q.question)}"
        rows="2"
        placeholder="${escHtml(q.placeholder || '')}"
        style="width:100%;padding:10px 12px;border:1px solid #d0d0d0;border-radius:8px;
               font-size:13px;line-height:1.5;outline:none;resize:vertical;
               font-family:-apple-system,BlinkMacSystemFont,sans-serif;color:#1a1a2e;"
      ></textarea>`;
    container.appendChild(wrap);
  });
  section.style.display = 'block';
}

async function autoGenerateKnowledge() {
  if (!schemaData) {
    document.getElementById('teach-editor-section').style.display = 'block';
    document.getElementById('btn-teach-save').style.display = 'inline-block';
    _draftGenerated = true;
    return;
  }

  _draftGenerated = true;
  document.getElementById('teach-loading').style.display = 'block';
  document.getElementById('teach-editor-section').style.display = 'none';
  document.getElementById('teach-error').style.display = 'none';

  let draft = '';
  try {
    const res  = await fetch('/setup/generate-company-draft', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ db_name: schemaData.db_name }),
    });
    const data = await res.json();
    if (data.success) {
      draft = data.content;
    } else if (data.retry_after) {
      await new Promise(r => setTimeout(r, (data.retry_after + 2) * 1000));
      const res2  = await fetch('/setup/generate-company-draft', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({ db_name: schemaData.db_name }),
      });
      const data2 = await res2.json();
      if (data2.success) draft = data2.content;
      else throw new Error(data2.error || 'Generation failed after rate limit retry.');
    } else {
      throw new Error(data.error || 'Generation failed.');
    }
  } catch (err) {
    document.getElementById('teach-loading').style.display = 'none';
    _showTeachError('Draft generation failed: ' + err.message + ' — you can type your knowledge manually below.');
    document.getElementById('inp-company-md').value = '';
    document.getElementById('teach-editor-section').style.display = 'block';
    document.getElementById('btn-teach-save').style.display = 'inline-block';
    return;
  }

  document.getElementById('inp-company-md').value = draft;
  document.getElementById('teach-loading').style.display = 'none';
  document.getElementById('teach-editor-section').style.display = 'block';
  document.getElementById('btn-teach-save').style.display = 'inline-block';

  try {
    const res  = await fetch('/setup/company-followup', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ draft }),
    });
    const data = await res.json();
    if (data.success && data.questions && data.questions.length > 0) {
      _renderFollowup(data.questions);
    }
  } catch (_) { /* follow-up is optional */ }
}

async function saveCompanyKnowledge() {
  const content = document.getElementById('inp-company-md').value.trim();
  if (!content) { goTo(5); return; }

  const followup_answers = [];
  document.querySelectorAll('.followup-answer').forEach(el => {
    const answer = el.value.trim();
    if (answer) {
      followup_answers.push({ question: el.getAttribute('data-question'), answer });
    }
  });

  const btn = document.getElementById('btn-teach-save');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const res  = await fetch('/setup/save-company-knowledge', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ content, followup_answers }),
    });
    const data = await res.json();
    if (data.success) {
      goTo(5);
    } else {
      _showTeachError(data.error || 'Save failed.');
    }
  } catch (err) {
    _showTeachError('Network error: ' + err.message);
  }

  btn.disabled = false;
  btn.textContent = 'Save & Continue →';
}

// ── Utility ────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
