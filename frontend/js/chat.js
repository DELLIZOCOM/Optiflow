/* ============================================================================
   OptiFlow AI — Chat UI controller
   ============================================================================ */

// ── DOM refs ────────────────────────────────────────────────────────────────

const chatArea       = document.getElementById('chatArea');
const chatInner      = document.getElementById('chatInner');
const input          = document.getElementById('questionInput');
const sendBtn        = document.getElementById('sendBtn');
const sendBtnLabel   = document.getElementById('sendBtnLabel');
const sessionPill    = document.getElementById('sessionPill');
const sessionLabel   = document.getElementById('sessionPillLabel');

const sidebar        = document.getElementById('sidebar');
const sessionListEl  = document.getElementById('sessionList');
const sessionSearch  = document.getElementById('sessionSearch');
const sessionCountEl = document.getElementById('sessionCount');
const activeTitleEl  = document.getElementById('activeChatTitle');

// ── State ───────────────────────────────────────────────────────────────────
//
// The server is the source of truth for conversation history. We only keep a
// handful of UI preferences in sessionStorage:
//   * agent_session_id        — which session is currently loaded in the UI
//   * optiflow_answer_mode    — last-used response format (text | chart)
//   * optiflow_sidebar_state  — open | collapsed (desktop only)

const SESSION_KEY        = 'agent_session_id';
const MODE_KEY           = 'optiflow_answer_mode';
const SIDEBAR_KEY        = 'optiflow_sidebar_state';

let busy             = false;
let currentSessionId = sessionStorage.getItem(SESSION_KEY) || null;
let _activeAbort     = null;
let answerMode       = (sessionStorage.getItem(MODE_KEY) === 'chart') ? 'chart' : 'text';
let _chartSeq        = 0;
const _chartInstances = new Map();   // canvasId -> Chart instance (for cleanup)

let _sessionCache   = [];             // last-known list from GET /sessions
let _searchQuery    = '';
let _loadingSession = false;          // true while switching sessions
let _transcriptRequestSeq = 0;        // ignore stale log fetches on rapid clicks

const SAMPLE_QUESTIONS = [
  'How many projects are currently active?',
  'Revenue last 30 days, by customer',
  'Top 5 pending invoices by amount',
  'Show me overdue payments this month',
];

// ── Utilities ───────────────────────────────────────────────────────────────

function escHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtClock() {
  const d = new Date();
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function scrollBottom(smooth) {
  if (smooth) {
    chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: 'smooth' });
  } else {
    chatArea.scrollTop = chatArea.scrollHeight;
  }
}

// Scroll *inside* a trace panel's body so streaming text stays visible
// without pushing the rest of the page around. Only scrolls if the user
// is already near the bottom — if they've scrolled up to read earlier
// steps, leave their position alone.
function scrollTraceBottom(panel) {
  if (!panel) return;
  const body = panel.querySelector('.trace-body');
  if (!body) return;
  const distanceFromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
  if (distanceFromBottom < 60) {
    body.scrollTop = body.scrollHeight;
  }
}

function setStatus(state, label) {
  if (!sessionPill) return;
  sessionPill.classList.remove('status-idle', 'status-running', 'status-error');
  sessionPill.classList.add('status-' + state);
  if (sessionLabel) sessionLabel.textContent = label;
}

function setBusy(isBusy) {
  busy = isBusy;
  input.disabled = false; // keep enabled — Esc still works
  if (isBusy) {
    sendBtn.classList.add('stop');
    sendBtnLabel.textContent = 'Stop';
    sendBtn.setAttribute('aria-label', 'Stop');
  } else {
    sendBtn.classList.remove('stop');
    sendBtnLabel.textContent = 'Send';
    sendBtn.setAttribute('aria-label', 'Send');
  }
  updateSendEnabled();
}

function updateSendEnabled() {
  if (busy) {
    sendBtn.disabled = false;
  } else {
    sendBtn.disabled = input.value.trim().length === 0;
  }
}

// ── Auto-growing textarea ───────────────────────────────────────────────────

function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
}

input.addEventListener('input', function() {
  autoGrow();
  updateSendEnabled();
});

input.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!busy) sendFromInput();
  } else if (e.key === 'Escape' && busy) {
    e.preventDefault();
    abortActive();
  }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && busy) {
    abortActive();
  }
});

// ── Persistence ─────────────────────────────────────────────────────────────
//
// The server persists all conversation history (display-log + LLM messages).
// These shims exist so the rest of this file doesn't need to care — later
// we may want client-side optimistic caching for offline mode, but for now
// the network path is the truth and these are no-ops.

function _saveMessage(_entry) { /* server-side; intentional no-op */ }

function _clearLocalMirror() {
  // Reserved for future client-side cache. No-op today.
}

// ── Message rendering ───────────────────────────────────────────────────────

function renderUserMessage(text, opts) {
  const msg = document.createElement('div');
  msg.className = 'msg msg-user';

  const t = document.createElement('div');
  t.className = 'msg-text';
  t.textContent = text;
  msg.appendChild(t);

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.textContent = (opts && opts.ts) || fmtClock();
  msg.appendChild(meta);

  chatInner.appendChild(msg);
  scrollBottom();
  return msg;
}

function renderAiMessage(text, metaBadges, opts) {
  const msg = document.createElement('div');
  msg.className = 'msg msg-ai';

  const t = document.createElement('div');
  t.className = 'msg-text';
  t.innerHTML = marked.parse(text || '');
  msg.appendChild(t);

  const meta = document.createElement('div');
  meta.className = 'msg-meta';

  const ts = document.createElement('span');
  ts.textContent = (opts && opts.ts) || fmtClock();
  meta.appendChild(ts);

  if (metaBadges && metaBadges.length) {
    for (const b of metaBadges) {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = b;
      meta.appendChild(badge);
    }
  }

  // Copy button
  const copyBtn = document.createElement('button');
  copyBtn.className = 'msg-action';
  copyBtn.title = 'Copy answer';
  copyBtn.textContent = 'Copy';
  copyBtn.addEventListener('click', function() {
    navigator.clipboard.writeText(text || '').then(function() {
      copyBtn.textContent = 'Copied';
      setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1400);
    }).catch(function() {});
  });
  meta.appendChild(copyBtn);

  msg.appendChild(meta);
  chatInner.appendChild(msg);
  scrollBottom();
  return msg;
}

function renderErrorMessage(text, retryFn) {
  const msg = document.createElement('div');
  msg.className = 'msg-error';

  const icon = document.createElement('span');
  icon.className = 'err-icon';
  icon.textContent = '\u26A0';
  msg.appendChild(icon);

  const body = document.createElement('div');
  body.className = 'err-body';

  const txt = document.createElement('div');
  txt.textContent = text || 'Something went wrong.';
  body.appendChild(txt);

  if (retryFn) {
    const btn = document.createElement('button');
    btn.textContent = 'Retry';
    btn.addEventListener('click', retryFn);
    body.appendChild(btn);
  }

  msg.appendChild(body);
  chatInner.appendChild(msg);
  scrollBottom();
  return msg;
}

// ── Empty state ─────────────────────────────────────────────────────────────

function renderEmptyState() {
  const wrap = document.createElement('div');
  wrap.className = 'empty-state';
  wrap.innerHTML =
    '<h2>Your autonomous data analyst</h2>' +
    '<p>Ask a question in plain English \u2014 OptiFlow plans the query, ' +
    'runs it against your database, and explains the result.</p>' +
    '<div class="empty-chips" id="emptyChips"></div>';
  chatInner.appendChild(wrap);

  const chipWrap = wrap.querySelector('#emptyChips');
  for (const q of SAMPLE_QUESTIONS) {
    const btn = document.createElement('button');
    btn.className = 'empty-chip';
    btn.textContent = q;
    btn.addEventListener('click', function() {
      input.value = q;
      autoGrow();
      updateSendEnabled();
      input.focus();
      sendFromInput();
    });
    chipWrap.appendChild(btn);
  }
}

function clearEmptyState() {
  const emptyEl = chatInner.querySelector('.empty-state');
  if (emptyEl) emptyEl.remove();
}

// ── Trace panel ─────────────────────────────────────────────────────────────

let _traceSeq = 0;

function createTracePanel() {
  const id = 'trace-' + (_traceSeq++);
  const panel = document.createElement('div');
  panel.className = 'trace-panel';
  panel.id = id;
  panel.innerHTML =
    '<div class="trace-header">' +
      '<span class="trace-dots"><span></span><span></span><span></span></span>' +
      '<span class="trace-title">Starting\u2026</span>' +
    '</div>' +
    '<div class="trace-body"></div>';
  chatInner.appendChild(panel);
  scrollBottom();
  return panel;
}

function updateTraceStatus(panel, message) {
  const el = panel.querySelector('.trace-title');
  if (el) el.textContent = message;
}

function appendThinkingStep(panel, content, opts) {
  const body = panel.querySelector('.trace-body');
  if (!body) return null;
  const step = document.createElement('div');
  step.className = 'trace-step trace-thinking';
  if (opts && opts.placeholder) step.classList.add('trace-thinking-placeholder');
  if (opts && opts.live)        step.classList.add('trace-thinking-live');

  const icon = document.createElement('span');
  icon.className = 'trace-icon';
  icon.textContent = '\uD83E\uDDE0';

  const text = document.createElement('span');
  text.className = 'trace-text';
  text.textContent = content || '';

  step.appendChild(icon);
  step.appendChild(text);
  body.appendChild(step);
  scrollTraceBottom(panel);
  return text; // caller can append text
}

function appendToolCallStep(panel, tool, toolInput) {
  const body = panel.querySelector('.trace-body');
  if (!body) return null;

  const step = document.createElement('div');
  step.className = 'trace-step trace-tool';

  let iconText  = '\uD83D\uDD27';
  let labelText = tool;
  let sqlText   = null;

  if (tool === 'list_tables') {
    iconText  = '\uD83D\uDCCB';
    labelText = 'Orienting to database\u2026';
  } else if (tool === 'get_table_schema') {
    iconText = '\uD83D\uDCC4';
    const names = toolInput && toolInput.tables
      ? (Array.isArray(toolInput.tables) ? toolInput.tables : [toolInput.tables]).join(', ')
      : '';
    labelText = names ? 'Schema: ' + names : 'Getting table schema\u2026';
  } else if (tool === 'execute_sql') {
    iconText  = '\u26A1';
    labelText = (toolInput && toolInput.explanation) ? toolInput.explanation : 'Running query\u2026';
    sqlText   = (toolInput && toolInput.sql) ? toolInput.sql : null;
  } else if (tool === 'get_relationships') {
    iconText  = '\uD83D\uDD17';
    labelText = 'Getting table relationships\u2026';
  } else if (tool === 'get_business_context') {
    iconText  = '\uD83D\uDCD6';
    labelText = (toolInput && toolInput.topic)
      ? 'Business context: ' + toolInput.topic
      : 'Looking up business context\u2026';
  }

  const header = document.createElement('div');
  header.className = 'trace-tool-header';

  const icon = document.createElement('span');
  icon.className = 'trace-icon';
  icon.textContent = iconText;

  const label = document.createElement('span');
  label.className = 'trace-text';
  label.textContent = labelText;

  header.appendChild(icon);
  header.appendChild(label);
  step.appendChild(header);

  if (sqlText) {
    const details = document.createElement('details');
    details.className = 'trace-sql-details';
    const summary = document.createElement('summary');
    summary.className = 'trace-sql-summary';
    summary.textContent = 'View SQL';
    const pre = document.createElement('pre');
    pre.className = 'trace-sql';
    pre.textContent = sqlText;
    details.appendChild(summary);
    details.appendChild(pre);
    step.appendChild(details);
  }

  body.appendChild(step);
  scrollTraceBottom(panel);
  return step;
}

function appendToolResult(stepEl, summary, isError) {
  if (!stepEl) return;
  const result = document.createElement('div');
  result.className = 'trace-result' + (isError ? ' trace-result-error' : '');
  const arrow = document.createElement('span');
  arrow.className = 'trace-arrow';
  arrow.textContent = '\u2192 ';
  result.appendChild(arrow);
  result.appendChild(document.createTextNode(summary || ''));
  stepEl.appendChild(result);
  const panel = stepEl.closest('.trace-panel');
  scrollTraceBottom(panel);
}

function collapseTrace(panel, stepCount) {
  const header = panel.querySelector('.trace-header');
  const body   = panel.querySelector('.trace-body');
  if (!header || !body) return;

  panel.classList.add('trace-done');
  const id    = panel.id;
  const label = stepCount > 0
    ? 'Agent trace \u00b7 ' + stepCount + ' step' + (stepCount === 1 ? '' : 's')
    : 'Agent trace';

  header.className = 'trace-header trace-header-done';
  header.innerHTML = '';

  const tick = document.createElement('span');
  tick.className = 'trace-done-icon';
  tick.textContent = '\u2713';

  const title = document.createElement('span');
  title.className = 'trace-title';
  title.textContent = label;

  const btn = document.createElement('button');
  btn.className = 'trace-toggle';
  btn.textContent = 'Show';
  btn.onclick = function() { toggleTrace(id, btn); };

  header.appendChild(tick);
  header.appendChild(title);
  header.appendChild(btn);

  body.style.display = 'none';
  panel.dataset.open = 'false';
}

function toggleTrace(id, btn) {
  const panel = document.getElementById(id);
  if (!panel) return;
  const body = panel.querySelector('.trace-body');
  if (!body) return;

  if (panel.dataset.open === 'true') {
    body.style.display = 'none';
    if (btn) btn.textContent = 'Show';
    panel.dataset.open = 'false';
  } else {
    body.style.display = 'flex';
    if (btn) btn.textContent = 'Hide';
    panel.dataset.open = 'true';
  }
}

// ── Rate-limit notice ───────────────────────────────────────────────────────

let _rlTimer = null;
let _rlEl    = null;

function showRateLimitCountdown(seconds, onDone) {
  _cancelRlCountdown();
  _rlEl = document.createElement('div');
  _rlEl.className = 'rl-notice';

  const label = document.createElement('span');
  label.textContent = 'Rate limit reached. Auto-retry in';

  const countEl = document.createElement('span');
  countEl.className = 'rl-countdown';
  countEl.textContent = seconds + 's';

  _rlEl.appendChild(label);
  _rlEl.appendChild(countEl);
  chatInner.appendChild(_rlEl);
  scrollBottom();

  let remaining = seconds;
  _rlTimer = setInterval(function() {
    remaining--;
    countEl.textContent = remaining + 's';
    if (remaining <= 0) {
      _cancelRlCountdown();
      onDone();
    }
  }, 1000);
}

function _cancelRlCountdown() {
  if (_rlTimer) { clearInterval(_rlTimer); _rlTimer = null; }
  if (_rlEl)    { _rlEl.remove();           _rlEl = null;    }
}

// ── Inline rate-limit notice (inside the trace panel) ──────────────────────

function showInlineRateLimit(panel, opts) {
  const body = panel.querySelector('.trace-body');
  if (!body) return null;

  const wrap = document.createElement('div');
  wrap.className = 'trace-rl';

  const icon = document.createElement('span');
  icon.className = 'trace-rl-icon';
  icon.textContent = '\u23F3';

  const textCol = document.createElement('div');
  textCol.className = 'trace-rl-body';

  const head = document.createElement('div');
  head.className = 'trace-rl-head';
  head.textContent = 'API rate limit reached';

  const sub = document.createElement('div');
  sub.className = 'trace-rl-sub';
  const a = opts.attempt || 1;
  const m = opts.maxAttempts || 3;
  sub.textContent = 'Waiting to retry automatically \u2014 attempt ' + a + ' of ' + m;

  const countdown = document.createElement('div');
  countdown.className = 'trace-rl-count';
  countdown.textContent = (opts.waitSeconds || 0) + 's';

  textCol.appendChild(head);
  textCol.appendChild(sub);

  wrap.appendChild(icon);
  wrap.appendChild(textCol);
  wrap.appendChild(countdown);
  body.appendChild(wrap);
  scrollBottom();

  return { wrap: wrap, countdown: countdown, total: opts.waitSeconds || 0 };
}

function updateInlineRateLimit(banner, remaining) {
  if (!banner || !banner.countdown) return;
  banner.countdown.textContent = remaining + 's';
  if (banner.wrap && banner.total > 0) {
    const pct = Math.max(0, Math.min(100, (remaining / banner.total) * 100));
    banner.wrap.style.setProperty('--rl-progress', pct + '%');
  }
}

function clearInlineRateLimit(banner) {
  if (banner && banner.wrap) banner.wrap.remove();
}

// ── Answer-mode toggle (Text / Chart) ───────────────────────────────────────

function setMode(mode) {
  if (mode !== 'text' && mode !== 'chart') return;
  answerMode = mode;
  try { sessionStorage.setItem(MODE_KEY, mode); } catch (_) {}
  _applyModeUI();
}

function _applyModeUI() {
  const t = document.getElementById('modeText');
  const c = document.getElementById('modeChart');
  if (!t || !c) return;
  if (answerMode === 'chart') {
    c.classList.add('active');
    c.setAttribute('aria-selected', 'true');
    t.classList.remove('active');
    t.setAttribute('aria-selected', 'false');
  } else {
    t.classList.add('active');
    t.setAttribute('aria-selected', 'true');
    c.classList.remove('active');
    c.setAttribute('aria-selected', 'false');
  }
  const ph = (answerMode === 'chart')
    ? 'Ask a question — the answer will be visualised…'
    : 'Ask anything about your data…';
  if (input) input.setAttribute('placeholder', ph);
}

// ── Chart rendering ─────────────────────────────────────────────────────────

// Deterministic palette — plenty of contrast, accessible-ish.
const _CHART_PALETTE = [
  '#2563eb', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
  '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1',
];

function _paletteAt(i) {
  return _CHART_PALETTE[i % _CHART_PALETTE.length];
}

function _paletteFor(n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(_paletteAt(i));
  return out;
}

// Build a card DOM shell for either a chart or a table fallback.
function _buildChartCard(spec) {
  const card = document.createElement('div');
  card.className = 'chart-card';

  if (spec && spec.title) {
    const h = document.createElement('div');
    h.className = 'chart-title';
    h.textContent = spec.title;
    card.appendChild(h);
  }

  if (spec && spec.explanation) {
    const p = document.createElement('div');
    p.className = 'chart-explanation';
    p.textContent = spec.explanation;
    card.appendChild(p);
  }

  return card;
}

function _renderTableCard(spec) {
  const card = _buildChartCard(spec);
  const wrap = document.createElement('div');
  wrap.className = 'chart-table-wrap';

  const table = document.createElement('table');
  table.className = 'chart-table';

  const rows = Array.isArray(spec.rows) ? spec.rows : [];
  const cols = (spec.x ? [spec.x] : []).concat(
    Array.isArray(spec.y) ? spec.y : (spec.y ? [spec.y] : [])
  );

  // If no columns declared, fall back to whatever keys the first row has.
  const columns = cols.length ? cols : (rows[0] ? Object.keys(rows[0]) : []);

  const thead = document.createElement('thead');
  const trh = document.createElement('tr');
  for (const c of columns) {
    const th = document.createElement('th');
    th.textContent = c;
    trh.appendChild(th);
  }
  thead.appendChild(trh);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    for (const c of columns) {
      const td = document.createElement('td');
      const v = r[c];
      td.textContent = (v === null || v === undefined) ? '' : String(v);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  wrap.appendChild(table);
  card.appendChild(wrap);
  return card;
}

function _chartJsConfig(spec) {
  const rows = Array.isArray(spec.rows) ? spec.rows : [];
  const xKey = spec.x;
  const yKeys = Array.isArray(spec.y) ? spec.y : (spec.y ? [spec.y] : []);
  const labels = rows.map(function(r) {
    const v = r[xKey];
    return (v === null || v === undefined) ? '' : String(v);
  });

  const isPieLike = spec.type === 'pie' || spec.type === 'doughnut';
  const isAreaLike = spec.type === 'area';
  const chartType = isAreaLike ? 'line' : (spec.type === 'table' ? 'bar' : spec.type);

  let datasets;
  if (isPieLike) {
    // Pie/doughnut: use the FIRST y-column only.
    const k = yKeys[0];
    const data = rows.map(function(r) { return Number(r[k]) || 0; });
    datasets = [{
      label:           k,
      data:            data,
      backgroundColor: _paletteFor(data.length),
      borderWidth:     1,
    }];
  } else {
    datasets = yKeys.map(function(k, i) {
      const color = _paletteAt(i);
      const data = rows.map(function(r) {
        const v = r[k];
        return (v === null || v === undefined) ? null : Number(v);
      });
      const ds = {
        label:           k,
        data:            data,
        backgroundColor: isAreaLike ? color + '33' : color,
        borderColor:     color,
        borderWidth:     2,
      };
      if (isAreaLike) {
        ds.fill    = true;
        ds.tension = 0.3;
      } else if (chartType === 'line') {
        ds.fill    = false;
        ds.tension = 0.25;
      }
      return ds;
    });
  }

  const opts = {
    responsive:          true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display:  yKeys.length > 1 || isPieLike,
        position: 'bottom',
      },
      tooltip: { mode: 'index', intersect: false },
    },
  };
  if (!isPieLike) {
    opts.scales = {
      x: { ticks: { autoSkip: true, maxRotation: 0 } },
      y: { beginAtZero: true },
    };
  }

  return { type: chartType, data: { labels: labels, datasets: datasets }, options: opts };
}

function renderChartCard(spec, parent) {
  if (!spec || typeof spec !== 'object') return;
  if (!window.Chart) {
    // Chart.js failed to load — fall back to table.
    parent.appendChild(_renderTableCard(spec));
    return;
  }

  if (spec.type === 'table') {
    parent.appendChild(_renderTableCard(spec));
    return;
  }

  const card = _buildChartCard(spec);
  const wrap = document.createElement('div');
  wrap.className = 'chart-canvas-wrap';
  const canvas = document.createElement('canvas');
  const cid = 'chart-' + (_chartSeq++);
  canvas.id = cid;
  wrap.appendChild(canvas);
  card.appendChild(wrap);
  parent.appendChild(card);

  try {
    const cfg = _chartJsConfig(spec);
    const inst = new window.Chart(canvas.getContext('2d'), cfg);
    _chartInstances.set(cid, inst);
  } catch (err) {
    // Bail out to table on any Chart.js error — never break the chat.
    try { canvas.remove(); } catch (_) {}
    card.appendChild(_renderTableCard(spec).querySelector('.chart-table-wrap'));
  }
}

function _destroyAllCharts() {
  for (const inst of _chartInstances.values()) {
    try { inst.destroy(); } catch (_) {}
  }
  _chartInstances.clear();
}

// ── SSE reader ──────────────────────────────────────────────────────────────

async function _readSSE(url, body, signal, onEvent) {
  const res = await fetch(url, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
    body:    JSON.stringify(body),
    signal:  signal,
  });

  if (!res.ok) throw new Error('HTTP ' + res.status);
  if (!res.body) throw new Error('No response body');

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep last partial line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') return;
        try { onEvent(JSON.parse(raw)); } catch (_) {}
      }
    }
  } finally {
    try { reader.cancel(); } catch (_) {}
  }
}

// ── Abort helper ────────────────────────────────────────────────────────────

function abortActive() {
  if (_activeAbort) {
    try { _activeAbort.abort(); } catch (_) {}
    _activeAbort = null;
  }
}

function sendOrStop() {
  if (busy) abortActive();
  else      sendFromInput();
}

function sendFromInput() {
  const q = input.value.trim();
  if (!q) return;
  sendQuestion(q);
}

// ── Main send flow ──────────────────────────────────────────────────────────

async function sendQuestion(question) {
  question = (question || '').trim();
  if (!question) return;

  abortActive();
  clearEmptyState();

  // User bubble
  const userTs = fmtClock();
  renderUserMessage(question, { ts: userTs });
  _saveMessage({ kind: 'user', text: question, ts: userTs });

  input.value = '';
  autoGrow();
  setBusy(true);
  setStatus('running', 'Thinking…');

  const ctrl  = new AbortController();
  _activeAbort = ctrl;

  // ── Always show the trace panel immediately ─────────────────────────────
  const panel = createTracePanel();

  // Seed with a placeholder so the user always sees *something* while
  // waiting for the first token. Replaced once thinking_start arrives.
  let stepCount   = 0;
  let liveTextEl  = null;     // active streaming text span (or null)
  let pendingSeed = appendThinkingStep(panel, 'Preparing the agent…', {
    placeholder: true, live: false,
  });

  let lastToolStepEl = null;
  let answered       = false;
  let erroredMessage = null;
  let rlBanner       = null;   // inline rate-limit countdown banner, if shown
  const chartSpecs   = [];     // collected chart events for this turn
  const wantVisualise = (answerMode === 'chart');

  function ensureLive() {
    // Convert the placeholder into a live block on first real chunk,
    // OR create a brand-new live block.
    if (liveTextEl) return liveTextEl;

    if (pendingSeed) {
      // upgrade placeholder into live block
      const parent = pendingSeed.parentElement;
      if (parent) {
        parent.classList.remove('trace-thinking-placeholder');
        parent.classList.add('trace-thinking-live');
      }
      pendingSeed.textContent = '';
      liveTextEl   = pendingSeed;
      pendingSeed  = null;
      return liveTextEl;
    }

    stepCount++;
    liveTextEl = appendThinkingStep(panel, '', { live: true });
    return liveTextEl;
  }

  function closeLive() {
    if (liveTextEl) {
      const parent = liveTextEl.parentElement;
      if (parent) parent.classList.remove('trace-thinking-live');
      liveTextEl = null;
    }
  }

  function removePlaceholder() {
    if (pendingSeed) {
      const parent = pendingSeed.parentElement;
      if (parent) parent.remove();
      pendingSeed = null;
    }
  }

  try {
    await _readSSE(
      '/ask',
      { question: question, session_id: currentSessionId, visualise: wantVisualise },
      ctrl.signal,
      function(event) {
        switch (event.type) {

          case 'status':
            updateTraceStatus(panel, event.message || 'Working…');
            break;

          case 'thinking_start':
            removePlaceholder();
            closeLive();
            stepCount++;
            liveTextEl = appendThinkingStep(panel, '', { live: true });
            break;

          case 'thinking_delta': {
            const el = ensureLive();
            if (el && event.delta) {
              el.textContent += event.delta;
              scrollTraceBottom(panel);
            }
            break;
          }

          case 'thinking_end':
            closeLive();
            break;

          case 'thinking':
            // Legacy buffered event — only use if nothing else streamed
            if (liveTextEl || pendingSeed === null) break;
            removePlaceholder();
            stepCount++;
            appendThinkingStep(panel, event.content || '');
            break;

          case 'tool_call':
            closeLive();
            removePlaceholder();
            stepCount++;
            lastToolStepEl = appendToolCallStep(panel, event.tool, event.input);
            break;

          case 'rate_limit_wait':
            closeLive();
            removePlaceholder();
            setStatus('running', 'Waiting for API');
            updateTraceStatus(panel, 'API rate limit — waiting to retry…');
            rlBanner = showInlineRateLimit(panel, {
              waitSeconds: event.wait_seconds || 30,
              attempt:     event.attempt || 1,
              maxAttempts: event.max_attempts || 3,
            });
            break;

          case 'rate_limit_tick':
            if (rlBanner) updateInlineRateLimit(rlBanner, event.remaining || 0);
            break;

          case 'rate_limit_resume':
            if (rlBanner) { clearInlineRateLimit(rlBanner); rlBanner = null; }
            updateTraceStatus(panel, 'Resuming\u2026');
            setStatus('running', 'Thinking…');
            break;

          case 'tool_result':
            appendToolResult(lastToolStepEl, event.result_summary, event.is_error);
            lastToolStepEl = null;
            break;

          case 'chart': {
            // Buffer chart specs — render them inside the AI message card
            // once the final `answer` event arrives. This keeps charts
            // visually attached to the text they belong to.
            if (event.spec && typeof event.spec === 'object') {
              chartSpecs.push(event.spec);
            }
            break;
          }

          case 'answer': {
            answered = true;
            closeLive();
            removePlaceholder();

            currentSessionId = event.session_id || currentSessionId;
            if (event.session_id) {
              sessionStorage.setItem(SESSION_KEY, event.session_id);
            }

            const q = event.queries_executed || 0;
            const n = event.iterations       || 0;
            const badges = [
              q + ' quer' + (q === 1 ? 'y' : 'ies'),
              n + ' step'  + (n === 1 ? ''  : 's'),
            ];
            if (chartSpecs.length > 0) {
              badges.push(chartSpecs.length + ' chart' + (chartSpecs.length === 1 ? '' : 's'));
            }

            collapseTrace(panel, stepCount);

            const aiTs = fmtClock();
            const aiMsg = renderAiMessage(event.content || 'No answer.', badges, { ts: aiTs });

            // Render charts inside the AI message, before the meta row.
            if (chartSpecs.length > 0 && aiMsg) {
              const metaEl = aiMsg.querySelector('.msg-meta');
              for (const spec of chartSpecs) {
                const host = document.createElement('div');
                host.className = 'chart-host';
                if (metaEl) aiMsg.insertBefore(host, metaEl);
                else        aiMsg.appendChild(host);
                renderChartCard(spec, host);
              }
              scrollBottom();
            }

            // Refresh the sidebar so the active session moves to the top
            // with its new title/preview. Fire-and-forget — don't block
            // the answer rendering on it.
            loadSessionList();

            _saveMessage({
              kind: 'ai', text: event.content || '',
              ts: aiTs, badges: badges,
              charts: chartSpecs.length > 0 ? chartSpecs.slice() : undefined,
            });
            break;
          }

          case 'error': {
            answered = true;
            erroredMessage = event.message || 'Agent encountered an error.';
            closeLive();
            removePlaceholder();
            collapseTrace(panel, stepCount);

            if (event.retry_after) {
              showRateLimitCountdown(event.retry_after, function() {
                sendQuestion(question);
              });
            } else {
              renderErrorMessage(erroredMessage, function() {
                sendQuestion(question);
              });
              _saveMessage({ kind: 'error', text: erroredMessage, ts: fmtClock() });
            }
            break;
          }
        }
      }
    );

  } catch (err) {
    if (err && err.name === 'AbortError') {
      closeLive();
      removePlaceholder();
      if (!answered) {
        collapseTrace(panel, stepCount);
        renderErrorMessage('Stopped.');
      }
    } else if (!answered) {
      closeLive();
      removePlaceholder();
      collapseTrace(panel, stepCount);
      const txt = 'Connection error. Please try again.';
      renderErrorMessage(txt, function() { sendQuestion(question); });
      _saveMessage({ kind: 'error', text: txt, ts: fmtClock() });
    }

  } finally {
    if (_activeAbort === ctrl) _activeAbort = null;

    const isRetrying = answered && !!document.querySelector('.rl-countdown');
    if (!isRetrying) {
      setBusy(false);
      setStatus(erroredMessage ? 'error' : 'idle', erroredMessage ? 'Error' : 'Ready');
      input.focus();
    }
  }
}

// ── Sidebar: session list ───────────────────────────────────────────────────

function _fmtRelative(tsSec) {
  if (!tsSec) return '';
  const now = Date.now() / 1000;
  const d = Math.max(0, now - tsSec);
  if (d < 60)       return 'just now';
  if (d < 3600)    return Math.floor(d / 60) + 'm ago';
  if (d < 86400)   return Math.floor(d / 3600) + 'h ago';
  if (d < 7 * 86400) return Math.floor(d / 86400) + 'd ago';
  const date = new Date(tsSec * 1000);
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function _groupLabel(tsSec) {
  if (!tsSec) return 'Earlier';
  const now = new Date();
  const d   = new Date(tsSec * 1000);
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return 'Today';
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  const diffDays = Math.floor((now - d) / 86400000);
  if (diffDays < 7)  return 'Previous 7 days';
  if (diffDays < 30) return 'Previous 30 days';
  return 'Older';
}

async function loadSessionList() {
  try {
    const res = await fetch('/sessions');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _sessionCache = Array.isArray(data.sessions) ? data.sessions : [];
  } catch (_) {
    _sessionCache = [];
  }
  renderSessionList();
}

function renderSessionList() {
  if (!sessionListEl) return;
  sessionListEl.innerHTML = '';

  const q = _searchQuery.trim().toLowerCase();
  const visible = q
    ? _sessionCache.filter(function(s) {
        return (s.title || '').toLowerCase().includes(q)
            || (s.preview || '').toLowerCase().includes(q);
      })
    : _sessionCache;

  if (sessionCountEl) {
    const n = _sessionCache.length;
    sessionCountEl.textContent = n + ' conversation' + (n === 1 ? '' : 's');
  }

  if (visible.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'session-list-empty';
    empty.textContent = q
      ? 'No conversations match "' + q + '"'
      : 'No conversations yet. Ask your first question to start.';
    sessionListEl.appendChild(empty);
    return;
  }

  let lastGroup = null;
  for (const s of visible) {
    const group = _groupLabel(s.updated_at);
    if (group !== lastGroup) {
      const lab = document.createElement('div');
      lab.className = 'session-group-label';
      lab.textContent = group;
      sessionListEl.appendChild(lab);
      lastGroup = group;
    }
    sessionListEl.appendChild(_buildSessionItem(s));
  }
}

function _buildSessionItem(s) {
  const item = document.createElement('div');
  item.className = 'session-item';
  if (s.session_id === currentSessionId) item.classList.add('active');
  item.setAttribute('role', 'button');
  item.tabIndex = 0;
  item.dataset.sid = s.session_id;
  item.title = s.title || 'Untitled';

  const body = document.createElement('div');
  body.className = 'si-body';

  const title = document.createElement('div');
  title.className = 'si-title';
  title.textContent = s.title || 'Untitled';
  body.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'si-meta';
  const turns = s.turn_count || 0;
  meta.textContent = _fmtRelative(s.updated_at)
    + (turns ? ' \u00b7 ' + turns + ' question' + (turns === 1 ? '' : 's') : '');
  body.appendChild(meta);

  item.appendChild(body);

  const actions = document.createElement('div');
  actions.className = 'si-actions';

  const renameBtn = document.createElement('button');
  renameBtn.className = 'si-action-btn';
  renameBtn.title = 'Rename';
  renameBtn.setAttribute('aria-label', 'Rename conversation');
  renameBtn.textContent = '\u270E';
  renameBtn.addEventListener('click', function(ev) {
    ev.stopPropagation();
    _promptRename(s.session_id, s.title || '');
  });
  actions.appendChild(renameBtn);

  const delBtn = document.createElement('button');
  delBtn.className = 'si-action-btn danger';
  delBtn.title = 'Delete';
  delBtn.setAttribute('aria-label', 'Delete conversation');
  delBtn.textContent = '\u2715';
  delBtn.addEventListener('click', function(ev) {
    ev.stopPropagation();
    _confirmDelete(s.session_id, s.title || '');
  });
  actions.appendChild(delBtn);

  item.appendChild(actions);

  const activate = function() { switchToSession(s.session_id); };
  item.addEventListener('click', activate);
  item.addEventListener('keydown', function(ev) {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      activate();
    }
  });
  return item;
}

function _promptRename(sid, currentTitle) {
  const next = window.prompt('Rename conversation', currentTitle || 'Untitled');
  if (next === null) return;
  const title = next.trim();
  if (!title || title === currentTitle) return;
  fetch('/session/' + encodeURIComponent(sid), {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ title: title }),
  }).then(function(res) {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }).then(function() {
    // Optimistic local update
    for (const s of _sessionCache) {
      if (s.session_id === sid) { s.title = title; break; }
    }
    if (sid === currentSessionId && activeTitleEl) {
      activeTitleEl.textContent = title;
    }
    renderSessionList();
  }).catch(function() {
    alert('Rename failed. Please try again.');
  });
}

function _confirmDelete(sid, title) {
  if (!confirm('Delete "' + (title || 'this conversation') + '"? This cannot be undone.')) return;
  fetch('/session/' + encodeURIComponent(sid), { method: 'DELETE' })
    .then(function(res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      _sessionCache = _sessionCache.filter(function(s) { return s.session_id !== sid; });
      if (sid === currentSessionId) {
        // Active session was deleted — drop into "new chat" state.
        newChat({ skipListReload: true });
      }
      renderSessionList();
    })
    .catch(function() {
      alert('Delete failed. Please try again.');
    });
}

// ── Switch / load / new ─────────────────────────────────────────────────────

async function switchToSession(sid) {
  if (!sid || sid === currentSessionId) { closeSidebarOnMobile(); return; }
  if (_loadingSession) return;
  _loadingSession = true;

  abortActive();
  _destroyAllCharts();
  chatInner.innerHTML = '';
  setBusy(false);
  setStatus('running', 'Loading…');

  const seq = ++_transcriptRequestSeq;

  try {
    const res = await fetch('/session/' + encodeURIComponent(sid) + '/log');
    if (seq !== _transcriptRequestSeq) return; // superseded by a newer click
    if (!res.ok) {
      if (res.status === 404) {
        // Session expired or was purged — drop it from the cache and reset.
        _sessionCache = _sessionCache.filter(function(s) { return s.session_id !== sid; });
        renderSessionList();
        newChat({ skipListReload: true });
        return;
      }
      throw new Error('HTTP ' + res.status);
    }
    const data = await res.json();

    currentSessionId = sid;
    sessionStorage.setItem(SESSION_KEY, sid);
    _renderTranscript(Array.isArray(data.entries) ? data.entries : []);
    _setActiveTitle(data.title || 'Conversation');
    renderSessionList();   // refresh "active" state
    closeSidebarOnMobile();
  } catch (err) {
    renderErrorMessage('Could not load that conversation.');
  } finally {
    setStatus('idle', 'Ready');
    _loadingSession = false;
  }
}

function _renderTranscript(entries) {
  if (!entries || entries.length === 0) {
    renderEmptyState();
    return;
  }
  for (const e of entries) {
    const tsLabel = _entryTsLabel(e);
    if (e.role === 'user') {
      renderUserMessage(e.text || '', { ts: tsLabel });
    } else if (e.role === 'ai') {
      const aiMsg = renderAiMessage(e.text || '', e.badges || [], { ts: tsLabel });
      if (aiMsg && Array.isArray(e.charts) && e.charts.length > 0) {
        const metaEl = aiMsg.querySelector('.msg-meta');
        for (const spec of e.charts) {
          const host = document.createElement('div');
          host.className = 'chart-host';
          if (metaEl) aiMsg.insertBefore(host, metaEl);
          else        aiMsg.appendChild(host);
          renderChartCard(spec, host);
        }
      }
    }
  }
  scrollBottom();
}

function _entryTsLabel(entry) {
  const raw = entry && entry.ts;
  if (typeof raw !== 'number' || !isFinite(raw)) return fmtClock();
  return new Date(raw * 1000).toLocaleTimeString(
    [], { hour: '2-digit', minute: '2-digit' }
  );
}

function _setActiveTitle(t) {
  if (!activeTitleEl) return;
  activeTitleEl.textContent = t && t.trim() ? t : 'Database Intelligence';
}

function newChat(opts) {
  opts = opts || {};
  abortActive();
  _destroyAllCharts();
  sessionStorage.removeItem(SESSION_KEY);
  currentSessionId = null;
  chatInner.innerHTML = '';
  renderEmptyState();
  _setActiveTitle('');  // falls back to "Database Intelligence"
  setBusy(false);
  setStatus('idle', 'Ready');
  if (!opts.skipListReload) renderSessionList();
  closeSidebarOnMobile();
  input.focus();
}

// Back-compat alias for any callers that still reference clearChat
async function clearChat() { newChat(); }

// ── Sidebar visibility ──────────────────────────────────────────────────────

function _isMobileLayout() {
  return window.matchMedia('(max-width: 860px)').matches;
}

function toggleSidebar() {
  if (!sidebar) return;
  if (_isMobileLayout()) {
    const isOpen = document.body.classList.toggle('sidebar-open');
    sidebar.classList.toggle('collapsed', !isOpen);
  } else {
    const collapsed = sidebar.classList.toggle('collapsed');
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    try { sessionStorage.setItem(SIDEBAR_KEY, collapsed ? 'collapsed' : 'open'); } catch (_) {}
  }
}

function closeSidebar() {
  if (!sidebar) return;
  document.body.classList.remove('sidebar-open');
  if (_isMobileLayout()) sidebar.classList.add('collapsed');
}

function closeSidebarOnMobile() {
  if (_isMobileLayout()) closeSidebar();
}

function _applyInitialSidebarState() {
  if (!sidebar) return;
  if (_isMobileLayout()) {
    sidebar.classList.add('collapsed');
    document.body.classList.remove('sidebar-open');
    return;
  }
  const pref = sessionStorage.getItem(SIDEBAR_KEY);
  const collapsed = pref === 'collapsed';
  sidebar.classList.toggle('collapsed', collapsed);
  document.body.classList.toggle('sidebar-collapsed', collapsed);
}

// ── Reset / New company ─────────────────────────────────────────────────────

async function resetData() {
  if (!confirm(
    'This will remove all connected sources, schemas, and business context.\n\n' +
    'Your AI provider settings will be kept.\n\nContinue?'
  )) return;

  const btn = document.getElementById('resetBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span>\u2026</span><span class="label">Resetting</span>'; }

  abortActive();

  try {
    const res = await fetch('/setup/reset', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (!data.success) throw new Error((data.errors && data.errors[0]) || 'Reset failed');
    sessionStorage.removeItem(SESSION_KEY);
    window.location.href = '/setup';
  } catch (err) {
    alert('Reset failed: ' + err.message);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span>\u21BB</span><span class="label">Reset</span>';
    }
  }
}

// ── Init ────────────────────────────────────────────────────────────────────

(async function init() {
  _applyInitialSidebarState();
  _applyModeUI();

  if (sessionSearch) {
    sessionSearch.addEventListener('input', function(e) {
      _searchQuery = e.target.value || '';
      renderSessionList();
    });
  }

  // Load the session list first so the sidebar populates immediately,
  // then load the active session's transcript (if any) or show empty state.
  await loadSessionList();

  if (currentSessionId) {
    // Verify the stored session still exists; if not, fall back to empty.
    const inList = _sessionCache.some(function(s) { return s.session_id === currentSessionId; });
    if (inList) {
      await switchToSession(currentSessionId);
    } else {
      currentSessionId = null;
      sessionStorage.removeItem(SESSION_KEY);
      renderEmptyState();
      _setActiveTitle('');  // falls back to "Database Intelligence"
    }
  } else {
    renderEmptyState();
    _setActiveTitle('');  // falls back to "Database Intelligence"
  }

  autoGrow();
  updateSendEnabled();
  setStatus('idle', 'Ready');
  input.focus();
})();
