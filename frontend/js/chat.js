// ── State ─────────────────────────────────────────────────────────────────────

const chatArea = document.getElementById('chatArea');
const input    = document.getElementById('questionInput');
const sendBtn  = document.getElementById('sendBtn');

let busy            = false;
let traceCounter    = 0;
let currentSessionId = sessionStorage.getItem('agent_session_id') || null;


// ── Utilities ─────────────────────────────────────────────────────────────────

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setDisabled(disabled) {
  busy = disabled;
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
  panel.innerHTML = `
    <div class="trace-header" id="${id}-header">
      <span class="trace-dots"><span></span><span></span><span></span></span>
      <span class="trace-title" id="${id}-title">Agent working\u2026</span>
    </div>
    <div class="trace-body" id="${id}-body"></div>`;
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
  const step = document.createElement('div');
  step.className = 'trace-step trace-thinking';
  step.innerHTML =
    `<span class="trace-icon">\uD83E\uDDE0</span>` +
    `<span class="trace-text">${escHtml(content)}</span>`;
  body.appendChild(step);
  scrollBottom();
}

function appendToolCallStep(panel, tool, toolInput) {
  const body = panel.querySelector('.trace-body');
  const step = document.createElement('div');
  step.className = 'trace-step trace-tool';

  let icon  = '\uD83D\uDD27';   // 🔧
  let label = `<strong>${escHtml(tool)}</strong>`;
  let extra = '';

  if (tool === 'execute_sql') {
    icon  = '\u26A1';           // ⚡
    label = '<strong>execute_sql</strong>';
    if (toolInput && toolInput.sql) {
      extra = `<pre class="trace-sql">${escHtml(toolInput.sql)}</pre>`;
    }
  } else if (tool === 'get_table_schema' && toolInput && toolInput.tables) {
    const names = Array.isArray(toolInput.tables) ? toolInput.tables.join(', ') : toolInput.tables;
    label = `<strong>get_table_schema</strong> <span class="trace-tool-args">(${escHtml(names)})</span>`;
  } else if (tool === 'list_tables' && toolInput && toolInput.filter) {
    label = `<strong>list_tables</strong> <span class="trace-tool-args">filter: ${escHtml(toolInput.filter)}</span>`;
  } else if (tool === 'get_business_context' && toolInput && toolInput.topic) {
    label = `<strong>get_business_context</strong> <span class="trace-tool-args">${escHtml(toolInput.topic)}</span>`;
  }

  step.innerHTML =
    `<div class="trace-tool-header">` +
      `<span class="trace-icon">${icon}</span>` +
      `<span class="trace-text">${label}</span>` +
    `</div>${extra}`;

  body.appendChild(step);
  scrollBottom();
  return step;
}

function appendToolResult(stepEl, summary, isError) {
  if (!stepEl) return;
  const result = document.createElement('div');
  result.className = 'trace-result' + (isError ? ' trace-result-error' : '');
  result.innerHTML =
    `<span class="trace-arrow">\u2192</span> ${escHtml(summary || '')}`;
  stepEl.appendChild(result);
  scrollBottom();
}

function collapseTrace(panel, stepCount) {
  const id     = panel.id;
  const header = panel.querySelector('.trace-header');
  const body   = panel.querySelector('.trace-body');

  panel.classList.add('trace-done');

  const label = stepCount > 0 ? `Agent trace \u00b7 ${stepCount} steps` : 'Agent trace';
  header.className = 'trace-header trace-header-done';
  header.innerHTML =
    `<span class="trace-done-icon">\u2713</span>` +
    `<span class="trace-title">${label}</span>` +
    `<button class="trace-toggle" onclick="toggleTrace('${id}')">&#9660; Show</button>`;

  body.style.display  = 'none';
  panel.dataset.open  = 'false';
}

function toggleTrace(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  const body = panel.querySelector('.trace-body');
  const btn  = panel.querySelector('.trace-toggle');
  const open = panel.dataset.open === 'true';

  if (open) {
    body.style.display = 'none';
    btn.innerHTML      = '&#9660; Show';
    panel.dataset.open = 'false';
  } else {
    body.style.display = '';
    btn.innerHTML      = '&#9650; Hide';
    panel.dataset.open = 'true';
    scrollBottom();
  }
}


// ── Rate-limit countdown ──────────────────────────────────────────────────────

let _rlTimer = null;
let _rlEl    = null;

function showRateLimitCountdown(seconds, onDone) {
  _cancelRlCountdown();
  _rlEl = document.createElement('div');
  _rlEl.className = 'rl-notice';
  _rlEl.innerHTML =
    'Rate limit reached. Retrying in\u2026' +
    `<span class="rl-countdown" id="rl-count">${seconds}s</span>`;
  chatArea.appendChild(_rlEl);
  scrollBottom();

  let remaining = seconds;
  _rlTimer = setInterval(() => {
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

async function _readSSE(url, body, onEvent) {
  const res = await fetch(url, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  });

  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer    = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();   // keep any incomplete trailing line

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      if (raw === '[DONE]') return;
      try { onEvent(JSON.parse(raw)); } catch (_) {}
    }
  }
}


// ── Main send flow ────────────────────────────────────────────────────────────

async function sendQuestion(question) {
  if (busy || !question.trim()) return;

  addMessage(question, 'user');
  input.value = '';
  setDisabled(true);

  const panel       = createTracePanel();
  let   stepCount   = 0;
  let   lastStepEl  = null;

  try {
    await _readSSE(
      '/ask',
      { question, session_id: currentSessionId },
      (event) => {
        switch (event.type) {

          case 'status':
            updateTraceStatus(panel, event.message);
            break;

          case 'thinking':
            stepCount++;
            lastStepEl = null;
            appendThinkingStep(panel, event.content);
            break;

          case 'tool_call':
            stepCount++;
            lastStepEl = appendToolCallStep(panel, event.tool, event.input);
            break;

          case 'tool_result':
            appendToolResult(lastStepEl, event.result_summary, event.is_error);
            lastStepEl = null;
            break;

          case 'answer': {
            currentSessionId = event.session_id || null;
            if (currentSessionId) {
              sessionStorage.setItem('agent_session_id', currentSessionId);
            }
            const q = event.queries_executed || 0;
            const n = event.iterations       || 0;
            collapseTrace(panel, stepCount);
            addMessage(
              event.content || 'No answer.',
              'ai',
              `\uD83E\uDD16 Agent \u00b7 ${q} quer${q === 1 ? 'y' : 'ies'} \u00b7 ${n} step${n === 1 ? '' : 's'}`
            );
            setDisabled(false);
            input.focus();
            break;
          }

          case 'error': {
            collapseTrace(panel, stepCount);
            if (event.retry_after) {
              showRateLimitCountdown(event.retry_after, () => sendQuestion(question));
              // keep disabled during countdown — sendQuestion re-enables when done
            } else {
              addMessage(
                event.message || 'Agent encountered an error. Please try again.',
                'ai',
                '\u26A0 Error'
              );
              setDisabled(false);
              input.focus();
            }
            break;
          }
        }
      }
    );

    // Stream ended cleanly without an answer/error event
    // (shouldn't happen in practice, but handle gracefully)
    if (busy) {
      collapseTrace(panel, stepCount);
      setDisabled(false);
      input.focus();
    }

  } catch (err) {
    panel.remove();
    addMessage('Connection error. Please try again.', 'ai');
    setDisabled(false);
    input.focus();
  }
}

function sendFromInput() {
  sendQuestion(input.value.trim());
}


// ── Reset / New Company ───────────────────────────────────────────────────────

async function resetData() {
  if (!confirm('This will delete all connected sources and knowledge, and restart the setup wizard. Continue?')) return;
  try {
    const res = await fetch('/setup/reset', { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    sessionStorage.removeItem(_STORAGE_KEY);
    sessionStorage.removeItem('agent_session_id');
    window.location.href = '/setup';
  } catch (err) {
    alert('Reset failed: ' + err.message);
  }
}


// ── Init ─────────────────────────────────────────────────────────────────────

(function init() {
  // Restore prior messages from this browser tab session
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
    "Hello! I\u2019m your autonomous data analyst.\n\n" +
    "Ask me anything about your data \u2014 I\u2019ll explore the database, " +
    "run the queries, and give you direct answers.",
    'ai'
  );
  input.focus();
})();
