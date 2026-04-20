// ── State ──────────────────────────────────────────────────────────────────
let connectionData  = null;   // {server, database, user, password}
let schemaData      = null;   // {db_name, server, tables: [...]}
let _draftGenerated = false;  // prevent double-generation on back/forward
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
  if (!btn.dataset.label) btn.dataset.label = btn.textContent;
  btn.disabled = loading;
  btn.innerHTML = loading
    ? `<span class="spinner"></span>${label || 'Loading…'}`
    : btn.dataset.label;
}

// ── Step 1: Anthropic API Key ──────────────────────────────────────────────
function _updateAiNextBtn() {
  const key = document.getElementById('inp-ai-key').value.trim();
  document.getElementById('btn-next-1').disabled = !key;
}

document.addEventListener('DOMContentLoaded', () => {
  const keyEl = document.getElementById('inp-ai-key');
  if (keyEl) {
    keyEl.addEventListener('input', _updateAiNextBtn);
    keyEl.addEventListener('keydown', e => {
      if (e.key === 'Enter' && keyEl.value.trim()) testAiKey();
    });
  }
});

async function testAiKey() {
  const api_key = document.getElementById('inp-ai-key').value.trim();
  const model   = document.getElementById('inp-ai-model').value.trim();

  if (!api_key) {
    setStatus('ai-test-status', 'error', 'Please enter an API key first.');
    return;
  }

  setLoading('btn-test-ai', true, 'Testing…');
  setStatus('ai-test-status', 'info', 'Sending a minimal test request to Anthropic…');

  try {
    const res  = await fetch('/setup/test-ai-provider', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ provider: 'anthropic', api_key, model, custom_endpoint: '' }),
    });
    const data = await res.json();
    if (data.success) {
      setStatus('ai-test-status', 'success', 'API key works. You can continue.');
      document.getElementById('btn-next-1').disabled = false;
    } else {
      setStatus('ai-test-status', 'error', data.error || 'Test failed.');
    }
  } catch (err) {
    setStatus('ai-test-status', 'error', 'Network error: ' + err.message);
  }

  setLoading('btn-test-ai', false);
}

async function saveAiConfig() {
  const api_key = document.getElementById('inp-ai-key').value.trim();
  const model   = document.getElementById('inp-ai-model').value.trim();

  if (!api_key) {
    setStatus('ai-test-status', 'error', 'Please enter an API key.');
    return;
  }

  setLoading('btn-next-1', true, 'Saving…');

  try {
    const res  = await fetch('/setup/save-ai-config', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({
        provider: 'anthropic',
        api_key, model,
        custom_endpoint: '',
        local_enabled:   false,
        local_endpoint:  'http://localhost:11434',
        local_model:     'qwen3:8b',
      }),
    });
    const data = await res.json();
    if (data.success) {
      goTo(2);
    } else {
      setStatus('ai-test-status', 'error', data.error || 'Save failed.');
    }
  } catch (err) {
    setStatus('ai-test-status', 'error', 'Network error: ' + err.message);
  }

  setLoading('btn-next-1', false);
}

// ── Step 2: Database Connection ────────────────────────────────────────────
async function testConnection() {
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
      setStatus('conn-status', 'success', data.message || 'Connection successful.');
      connectionData = {server, database, user, password};
      await checkPermissions(server, database, user, password);
    } else {
      setStatus('conn-status', 'error', data.error);
      hidePerm();
    }
  } catch (err) {
    setStatus('conn-status', 'error', 'Network error: ' + err.message);
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

    const sqlTxt = readonlySql(database);

    if (!data.success) {
      _setPerm('unknown', 'Could not verify permissions.', 'Make sure this is a read-only user.');
      btn.disabled = false;
      btn.textContent = 'Continue →';
      btn.className = 'btn btn-primary';
      return;
    }

    const level = data.access_level;

    if (level === 'readonly') {
      _setPerm('readonly', 'Read-only access confirmed.', 'This user can only read data.');
      document.getElementById('perm-sql').className = 'sql-helper';
      btn.disabled = false;
      btn.textContent = 'Continue →';
      btn.className = 'btn btn-primary';

    } else if (level === 'warning') {
      _setPerm('warning', 'Write permissions detected.',
        data.message + ' We strongly recommend a read-only user.');
      document.getElementById('perm-sql').className = 'sql-helper show';
      document.getElementById('perm-sql-text').textContent = sqlTxt;
      btn.disabled = false;
      btn.textContent = 'Continue anyway';
      btn.className = 'btn btn-warn';

    } else if (level === 'blocked') {
      _setPerm('blocked', 'Admin / owner privileges detected.',
        data.message + ' Please create a read-only user before continuing.');
      document.getElementById('perm-sql').className = 'sql-helper show';
      document.getElementById('perm-sql-text').textContent = sqlTxt;
      btn.disabled = true;
      btn.textContent = 'Use a read-only user to continue';
      btn.className = 'btn btn-primary';

    } else {
      _setPerm('unknown', 'Could not verify permissions.', 'Make sure this is a read-only user.');
      btn.disabled = false;
      btn.textContent = 'Continue →';
      btn.className = 'btn btn-primary';
    }

  } catch (err) {
    _setPerm('unknown', 'Could not verify permissions.', 'Make sure this is a read-only user.');
    btn.disabled = false;
    btn.textContent = 'Continue →';
    btn.className = 'btn btn-primary';
  }
}

function _setPerm(level, title, detail) {
  document.getElementById('perm-box').className = 'perm-box ' + level + ' show';
  document.getElementById('perm-title').textContent = title;
  document.getElementById('perm-detail').textContent = detail;
}

function readonlySql(dbName) {
  return `-- Run in SQL Server Management Studio or sqlcmd:
CREATE LOGIN optiflow_reader WITH PASSWORD = 'choose_a_strong_password';
USE [${dbName}];
CREATE USER optiflow_reader FOR LOGIN optiflow_reader;
ALTER ROLE db_datareader ADD MEMBER optiflow_reader;`;
}

document.addEventListener('DOMContentLoaded', () => {
  const passEl = document.getElementById('inp-pass');
  if (passEl) passEl.addEventListener('keydown', e => {
    if (e.key === 'Enter') testConnection();
  });
});

// ── Step 3: Schema Discovery ───────────────────────────────────────────────
async function discoverSchema() {
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
        `Discovered ${data.tables.length} tables — credentials and schema saved.`);

      renderTableGrid(data.tables);

      document.getElementById('disc-subtitle').textContent =
        `Connected to ${data.db_name} on ${data.server}.`;

      document.getElementById('btn-next-3').disabled = false;
      document.getElementById('btn-disc').style.display = 'none';
    } else {
      setStatus('disc-status', 'error', data.error);
    }
  } catch (err) {
    setStatus('disc-status', 'error', err.message);
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
      <div class="table-card-meta">${t.row_count.toLocaleString()} rows · ${t.columns.length} columns</div>
    `;
    grid.appendChild(card);
  });
  grid.style.display = 'grid';
}

// ── Step 4: Company Knowledge ──────────────────────────────────────────────
function _showTeachError(msg) {
  setStatus('teach-error', 'error', msg);
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
    tabEdit.classList.remove('active');
    tabPrev.classList.add('active');
  } else {
    textarea.style.display = 'block';
    preview.style.display  = 'none';
    tabEdit.classList.add('active');
    tabPrev.classList.remove('active');
  }
}

function _mdToHtml(text) {
  let h = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  h = h.replace(/^### (.+)$/mg, '<h3>$1</h3>');
  h = h.replace(/^## (.+)$/mg,  '<h2>$1</h2>');
  h = h.replace(/^# (.+)$/mg,   '<h1>$1</h1>');
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/^- (.+)$/mg, '<li>$1</li>');
  h = h.replace(/(<li>.*?<\/li>\n?)+/gs, '<ul>$&</ul>');
  h = h.replace(/\n\n+/g, '</p><p>');
  h = h.replace(/\n/g, '<br>');
  return '<p>' + h + '</p>';
}

async function autoGenerateKnowledge() {
  if (!schemaData) {
    document.getElementById('teach-editor-section').style.display = 'block';
    document.getElementById('btn-teach-save').style.display = 'inline-flex';
    _draftGenerated = true;
    return;
  }

  _draftGenerated = true;
  document.getElementById('teach-loading').style.display = 'block';
  document.getElementById('teach-editor-section').style.display = 'none';
  clearStatus('teach-error');

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
    document.getElementById('btn-teach-save').style.display = 'inline-flex';
    return;
  }

  document.getElementById('inp-company-md').value = draft;
  document.getElementById('teach-loading').style.display = 'none';
  document.getElementById('teach-editor-section').style.display = 'block';
  document.getElementById('btn-teach-save').style.display = 'inline-flex';
}

async function saveCompanyKnowledge() {
  const content = document.getElementById('inp-company-md').value.trim();
  if (!content) { goTo(5); return; }

  setLoading('btn-teach-save', true, 'Saving…');

  try {
    const res  = await fetch('/setup/save-company-knowledge', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ content, followup_answers: [] }),
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

  setLoading('btn-teach-save', false);
}

// ── Step 5: Done ───────────────────────────────────────────────────────────
function buildDoneScreen() {
  const tables    = schemaData ? schemaData.tables : [];
  const totalRows = tables.reduce((s, t) => s + t.row_count, 0);
  const dbName    = schemaData ? schemaData.db_name : '—';

  document.getElementById('done-subtitle').textContent =
    `Connected to ${dbName}. OptiFlow is ready to answer your questions.`;

  document.getElementById('done-grid').innerHTML = `
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

// ── Utility ────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
