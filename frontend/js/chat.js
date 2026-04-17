// ── State ─────────────────────────────────────────────────────────────────────

const chatArea = document.getElementById('chatArea');
const input    = document.getElementById('questionInput');
const sendBtn  = document.getElementById('sendBtn');

let busy             = false;
let traceCounter     = 0;
let currentSessionId = sessionStorage.getItem('agent_session_id') || null;
let _activeAbort     = null;   // AbortController for the in-flight SSE request


// ── Utilities ─────────────────────────────────────────────────────────────────

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setDisabled(disabled) {
  busy             = disabled;
  sendBtn.disabled = disabled;
  input.disabled   = disabled;
}

function scrollBottom() {
  chatArea.scrollTop = chatArea.scrollHeight;
}


// ── Chat messages ─────────────────────────────────────────────────────────────

const _STORAGE_KEY = 'optiflow_chat_history_v2';

function _saveMessage(text, type, meta) {
  try {
    const h = JSON.parse(sessionStorage.getItem(_STORAGE_KEY) || '[]');
    h.push({ text, type, meta: meta || null });
    if (h.length > 200) h.splice(0, h.length - 200);
    sessionStorage.setItem(_STORAGE_KEY, JSON.stringify(h));
  } catch (_) {}
}

function addMessage(text, type, meta) {
  const msg = document.createElement('div');
  msg.className = 'msg msg-' + type;

  const t = document.createElement('div');
  t.className = 'msg-text';
  if (type === 'ai') t.innerHTML = marked.parse(text || '');
  else               t.textContent = text;
  msg.appendChild(t);

  if (meta) {
    const m = document.createElement('div');
    m.className = 'msg-meta';
    m.textContent = meta;
    msg.appendChild(m);
  }

  chatArea.appendChild(msg);
  scrollBottom();
  _saveMessage(text, type, meta);
  return msg;
}


// ── Trace panel ───────────────────────────────────────────────────────────────

function createTracePanel() {
  const id    = 'trace-' + (traceCounter++);
  const panel = document.createElement('div');
  panel.className = 'trace-panel';
  panel.id        = id;
  panel.innerHTML =
    '<div class="trace-header">' +
      '<span class="trace-dots"><span></span><span></span><span></span></span>' +
      '<span class="trace-title">Agent working\u2026</span>' +
    '</div>' +
    '<div class="trace-body"></div>';
  chatArea.appendChild(panel);
  scrollBottom();
  return panel;
}

function updateTraceStatus(panel, message) {
  const el = panel.querySelector('.trace-title');
  if (el) el.textContent = message;
}

function appendThinkingStep(panel, content) {
  const body = panel.querySelector('.trace-body');
  if (!body) return;
  const step = document.createElement('div');
  step.className = 'trace-step trace-thinking';
  const icon = document.createElement('span');
  icon.className = 'trace-icon';
  icon.textContent = '\uD83E\uDDE0';
  const text = document.createElement('span');
  text.className = 'trace-text';
  text.textContent = content;
  step.appendChild(icon);
  step.appendChild(text);
  body.appendChild(step);
  scrollBottom();
}

function appendToolCallStep(panel, tool, toolInput) {
  const body = panel.querySelector('.trace-body');
  if (!body) return null;

  const step = document.createElement('div');
  step.className = 'trace-step trace-tool';

  let iconText  = '\uD83D\uDD27'; // 🔧
  let labelText = tool;
  let sqlText   = null;

  if (tool === 'list_tables') {
    iconText  = '\uD83D\uDCCB'; // 📋
    labelText = 'Orienting to database\u2026';
  } else if (tool === 'get_table_schema') {
    iconText = '\uD83D\uDCC4'; // 📄
    const names = toolInput && toolInput.tables
      ? (Array.isArray(toolInput.tables) ? toolInput.tables : [toolInput.tables]).join(', ')
      : '';
    labelText = names ? 'Schema: ' + names : 'Getting table schema\u2026';
  } else if (tool === 'execute_sql') {
    iconText  = '\u26A1'; // ⚡
    labelText = (toolInput && toolInput.explanation) ? toolInput.explanation : 'Running query\u2026';
    sqlText   = (toolInput && toolInput.sql) ? toolInput.sql : null;
  } else if (tool === 'get_relationships') {
    iconText  = '\uD83D\uDD17'; // 🔗
    labelText = 'Getting table relationships\u2026';
  } else if (tool === 'get_business_context') {
    iconText  = '\uD83D\uDCD6'; // 📖
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
  scrollBottom();
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
  scrollBottom();
}

function collapseTrace(panel, stepCount) {
  const header = panel.querySelector('.trace-header');
  const body   = panel.querySelector('.trace-body');
  if (!header || !body) return;

  panel.classList.add('trace-done');
  const id    = panel.id;
  const label = stepCount > 0 ? 'Agent trace \u00b7 ' + stepCount + ' steps' : 'Agent trace';

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
  btn.innerHTML = '&#9660; Show';
  btn.onclick = function() { toggleTrace(id); };

  header.appendChild(tick);
  header.appendChild(title);
  header.appendChild(btn);

  body.style.display = 'none';
  panel.dataset.open = 'false';
}

function toggleTrace(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  const body = panel.querySelector('.trace-body');
  const btn  = panel.querySelector('.trace-toggle');
  if (!body || !btn) return;

  if (panel.dataset.open === 'true') {
    body.style.display = 'none';
    btn.innerHTML      = '&#9660; Show';
    panel.dataset.open = 'false';
  } else {
    body.style.display = 'flex';
    btn.innerHTML      = '&#9650; Hide';
    panel.dataset.open = 'true';
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}


// ── Rate-limit countdown ──────────────────────────────────────────────────────

let _rlTimer = null;
let _rlEl    = null;

function showRateLimitCountdown(seconds, onDone) {
  _cancelRlCountdown();
  _rlEl = document.createElement('div');
  _rlEl.className = 'rl-notice';
  const countEl = document.createElement('span');
  countEl.className = 'rl-countdown';
  countEl.id = 'rl-count';
  countEl.textContent = seconds + 's';
  _rlEl.textContent = 'Rate limit reached. Retrying in\u2026 ';
  _rlEl.appendChild(countEl);
  chatArea.appendChild(_rlEl);
  scrollBottom();

  let remaining = seconds;
  _rlTimer = setInterval(function() {
    remaining--;
    const el = document.getElementById('rl-count');
    if (el) el.textContent = remaining + 's';
    if (remaining <= 0) { _cancelRlCountdown(); onDone(); }
  }, 1000);
}

function _cancelRlCountdown() {
  if (_rlTimer) { clearInterval(_rlTimer); _rlTimer = null; }
  if (_rlEl)    { _rlEl.remove(); _rlEl = null; }
}


// ── SSE reader ────────────────────────────────────────────────────────────────

async function _readSSE(url, body, signal, onEvent) {
  const res = await fetch(url, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
    signal:  signal,
  });

  if (!res.ok) throw new Error('HTTP ' + res.status);

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let   buffer  = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') return;
        try { onEvent(JSON.parse(raw)); } catch (_) {}
      }
    }
  } finally {
    reader.cancel().catch(function() {});
  }
}


// ── Main send flow ────────────────────────────────────────────────────────────

async function sendQuestion(question) {
  question = (question || '').trim();
  if (!question) return;

  // Cancel any in-flight request before starting a new one
  if (_activeAbort) {
    _activeAbort.abort();
    _activeAbort = null;
  }

  addMessage(question, 'user');
  input.value = '';
  setDisabled(true);

  const ctrl     = new AbortController();
  _activeAbort   = ctrl;

  const panel       = createTracePanel();
  let stepCount     = 0;
  let lastStepEl    = null;
  let answered      = false;   // true once we receive 'answer' or 'error'
  let hasThinking   = false;   // true once at least one thinking event arrives

  function unlock() {
    setDisabled(false);
    input.focus();
  }

  try {
    await _readSSE(
      '/ask',
      { question: question, session_id: currentSessionId },
      ctrl.signal,
      function(event) {
        switch (event.type) {

          case 'status':
            updateTraceStatus(panel, event.message);
            break;

          case 'thinking':
            hasThinking = true;
            stepCount++;
            lastStepEl = null;
            appendThinkingStep(panel, event.content);
            break;

          case 'tool_call':
            // If the model called a tool without writing a thinking block first,
            // inject a generic fallback so the trace panel is never empty.
            if (!hasThinking) {
              hasThinking = true;
              stepCount++;
              appendThinkingStep(panel, 'Analyzing the question\u2026');
            }
            stepCount++;
            lastStepEl = appendToolCallStep(panel, event.tool, event.input);
            break;

          case 'tool_result':
            appendToolResult(lastStepEl, event.result_summary, event.is_error);
            lastStepEl = null;
            break;

          case 'answer': {
            answered = true;
            currentSessionId = event.session_id || currentSessionId;
            if (event.session_id) {
              sessionStorage.setItem('agent_session_id', event.session_id);
            }
            const q = event.queries_executed || 0;
            const n = event.iterations       || 0;
            collapseTrace(panel, stepCount);
            addMessage(
              event.content || 'No answer.',
              'ai',
              '\uD83E\uDD16 Agent \u00b7 ' + q + ' quer' + (q === 1 ? 'y' : 'ies') +
              ' \u00b7 ' + n + ' step' + (n === 1 ? '' : 's')
            );
            break;
          }

          case 'error': {
            answered = true;
            collapseTrace(panel, stepCount);
            if (event.retry_after) {
              showRateLimitCountdown(
                event.retry_after,
                function() { sendQuestion(question); }
              );
            } else {
              addMessage(
                event.message || 'Agent encountered an error. Please try again.',
                'ai',
                '\u26A0 Error'
              );
            }
            break;
          }
        }
      }
    );

  } catch (err) {
    // AbortError = we cancelled it intentionally (new question, clear chat, etc.)
    // Don't show an error message for that.
    if (err.name !== 'AbortError' && !answered) {
      collapseTrace(panel, stepCount);
      addMessage('Connection error. Please try again.', 'ai');
    }

  } finally {
    // Always clean up — runs after normal return, abort, or error
    if (_activeAbort === ctrl) _activeAbort = null;

    // Rate-limit path: onDone callback handles re-send, keep disabled
    const isRetrying = answered && document.getElementById('rl-count');
    if (!isRetrying) {
      unlock();
    }
  }
}

function sendFromInput() {
  sendQuestion(input.value.trim());
}


// ── Clear Chat ────────────────────────────────────────────────────────────────

async function clearChat() {
  // Cancel any in-flight stream
  if (_activeAbort) { _activeAbort.abort(); _activeAbort = null; }

  const sid = currentSessionId;
  if (sid) {
    try { await fetch('/session/' + sid, { method: 'DELETE' }); } catch (_) {}
  }
  sessionStorage.removeItem(_STORAGE_KEY);
  sessionStorage.removeItem('agent_session_id');
  currentSessionId = null;
  chatArea.innerHTML = '';
  setDisabled(false);
  addMessage('Chat cleared. Ask me anything about your data.', 'ai');
  input.focus();
}


// ── Reset / New Company ───────────────────────────────────────────────────────

async function resetData() {
  if (!confirm(
    'This will remove all connected sources, schemas, and business context.\n\n' +
    'Your AI provider settings will be kept.\n\nContinue?'
  )) return;

  const btn = document.getElementById('resetBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Resetting\u2026'; }

  if (_activeAbort) { _activeAbort.abort(); _activeAbort = null; }

  try {
    const res = await fetch('/setup/reset', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (!data.success) throw new Error((data.errors && data.errors[0]) || 'Reset failed');
    sessionStorage.removeItem(_STORAGE_KEY);
    sessionStorage.removeItem('agent_session_id');
    window.location.href = '/setup';
  } catch (err) {
    alert('Reset failed: ' + err.message);
    if (btn) { btn.disabled = false; btn.innerHTML = '&#8635; New Company'; }
  }
}


// ── Init ─────────────────────────────────────────────────────────────────────

(function init() {
  try {
    const history = JSON.parse(sessionStorage.getItem(_STORAGE_KEY) || '[]');
    if (history.length > 0) {
      for (const entry of history) {
        const msg = document.createElement('div');
        msg.className = 'msg msg-' + entry.type;
        const t = document.createElement('div');
        t.className = 'msg-text';
        if (entry.type === 'ai') t.innerHTML = marked.parse(entry.text || '');
        else                     t.textContent = entry.text;
        msg.appendChild(t);
        if (entry.meta) {
          const m = document.createElement('div');
          m.className = 'msg-meta';
          m.textContent = entry.meta;
          msg.appendChild(m);
        }
        chatArea.appendChild(msg);
      }
      scrollBottom();
      return;
    }
  } catch (_) {}

  addMessage(
    'Hello! I\u2019m your autonomous data analyst.\n\n' +
    'Ask me anything about your data \u2014 I\u2019ll explore the database, ' +
    'run the queries, and give you direct answers.',
    'ai'
  );
  input.focus();
}());
