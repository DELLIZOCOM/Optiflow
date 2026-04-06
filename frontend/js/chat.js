const chatArea = document.getElementById('chatArea');
const input    = document.getElementById('questionInput');
const sendBtn  = document.getElementById('sendBtn');
let busy = false;

const pendingQueries = {};
let agentCardCounter = 0;

// ── Rate-limit countdown ─────────────────────────────────────────────────────

let _rlTimer = null;
let _rlEl    = null;

function showRateLimitCountdown(seconds, onDone) {
  _cancelRlCountdown();
  _rlEl = document.createElement('div');
  _rlEl.className = 'rl-notice';
  _rlEl.innerHTML =
    'Processing your request. Please wait\u2026' +
    `<span class="rl-countdown" id="rl-count">${seconds}s</span>`;
  chatArea.appendChild(_rlEl);
  chatArea.scrollTop = chatArea.scrollHeight;

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

// ── Helpers ──────────────────────────────────────────────────────────────────

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setDisabled(disabled) {
  busy = disabled;
  sendBtn.disabled = disabled;
  input.disabled = disabled;
}

function showLoading() {
  const el = document.createElement('div');
  el.className = 'loading';
  el.id = 'loader';
  el.innerHTML = '<span></span><span></span><span></span>';
  chatArea.appendChild(el);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function hideLoading() {
  const el = document.getElementById('loader');
  if (el) el.remove();
}

// ── Session storage for conversation history ─────────────────────────────────

const _STORAGE_KEY = 'optiflow_chat_history';

function _saveMessage(text, type, meta) {
  try {
    const history = JSON.parse(sessionStorage.getItem(_STORAGE_KEY) || '[]');
    history.push({ text, type, meta: meta || null });
    if (history.length > 200) history.splice(0, history.length - 200);
    sessionStorage.setItem(_STORAGE_KEY, JSON.stringify(history));
  } catch (_) {}
}

function _restoreHistory() {
  try { return JSON.parse(sessionStorage.getItem(_STORAGE_KEY) || '[]'); }
  catch (_) { return []; }
}

// ── Messages ─────────────────────────────────────────────────────────────────

function addMessage(text, type, meta) {
  const msg = document.createElement('div');
  msg.className = 'msg msg-' + type;
  const t = document.createElement('div');
  t.className = 'msg-text';
  if (type === 'ai') { t.innerHTML = marked.parse(text || ''); }
  else               { t.textContent = text; }
  msg.appendChild(t);
  if (meta) {
    const m = document.createElement('div');
    m.className = 'msg-meta';
    m.textContent = meta;
    msg.appendChild(m);
  }
  chatArea.appendChild(msg);
  chatArea.scrollTop = chatArea.scrollHeight;
  _saveMessage(text, type, meta);
  return msg;
}

const CONF_LABEL = {
  high: 'High confidence', medium: 'Medium confidence',
  low: 'Low confidence', none: 'Unknown'
};

function tagsHtml(tables) {
  return (tables || []).map(t => `<span class="table-tag">${escHtml(t)}</span>`).join('');
}

// ── Agent cards ──────────────────────────────────────────────────────────────

function addAgentCard(question, data) {
  const cardId = 'agent-card-' + (agentCardCounter++);
  pendingQueries[cardId] = {
    agent_type: 'single', question,
    sql: data.sql, tables_used: data.tables_used || [],
    from_cache: data.from_cache || data.from_approved_log || false,
  };

  const conf      = data.confidence || 'none';
  const confLabel = CONF_LABEL[conf] || conf;
  const warningsHtml = (data.warnings || [])
    .map(w => `<div class="agent-warning">\u26a0 ${escHtml(w)}</div>`).join('');

  const card = document.createElement('div');
  card.className = 'msg-agent-preview';
  card.id = cardId;
  card.innerHTML = `
    <div class="agent-header">\uD83E\uDD16 Agent Mode \u2014 Review before running</div>
    <div class="agent-explanation">${escHtml(data.explanation)}</div>
    <div class="agent-meta-row">
      <div style="display:flex;flex-wrap:wrap;gap:6px;">${tagsHtml(data.tables_used)}</div>
      <span class="conf-badge conf-${escHtml(conf)}">${escHtml(confLabel)}</span>
    </div>
    <pre class="sql-block">${escHtml(data.sql)}</pre>
    ${warningsHtml ? `<div class="agent-warnings">${warningsHtml}</div>` : ''}
    <div class="agent-actions" id="${cardId}-actions">
      <button class="btn-approve" onclick="approveQuery('${cardId}')">\u2713 Approve &amp; Run</button>
      <button class="btn-reject"  onclick="rejectQuery('${cardId}')">\u2717 Reject</button>
    </div>
    <div class="msg-meta">\uD83E\uDD16 Agent \u00b7 ${data.time_ms}ms</div>
  `;
  chatArea.appendChild(card);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function addChainCard(question, data) {
  const cardId = 'agent-card-' + (agentCardCounter++);
  pendingQueries[cardId] = {
    agent_type: 'chain', question,
    steps: data.steps || [], summary_prompt: data.summary_prompt || '',
    entity_label: '', from_cache: data.from_cache || false,
  };

  const conf      = data.confidence || 'none';
  const confLabel = CONF_LABEL[conf] || conf;
  const n         = (data.steps || []).length;
  const stepsHtml = (data.steps || []).map(s => `
    <div class="chain-step">
      <div class="chain-step-header">
        <div class="chain-step-num">${s.step}</div>
        <div class="chain-step-label">${escHtml(s.explanation)}</div>
      </div>
      <pre class="sql-block" style="margin-bottom:6px;">${escHtml(s.sql)}</pre>
      <div class="step-tables">${tagsHtml(s.tables)}</div>
    </div>
  `).join('');
  const warningsHtml = (data.warnings || [])
    .map(w => `<div class="agent-warning">\u26a0 ${escHtml(w)}</div>`).join('');

  const card = document.createElement('div');
  card.className = 'msg-chain-preview';
  card.id = cardId;
  card.innerHTML = `
    <div class="chain-header">\uD83D\uDD17 Chain Query \u2014 ${n}-step investigation</div>
    <div class="agent-explanation" style="margin-bottom:12px;">
      This answer requires ${n} queries run in sequence. Review all steps below.
    </div>
    ${stepsHtml}
    ${warningsHtml ? `<div class="agent-warnings">${warningsHtml}</div>` : ''}
    <div class="agent-meta-row" style="margin-bottom:10px;">
      <span class="conf-badge conf-${escHtml(conf)}">${escHtml(confLabel)}</span>
    </div>
    <div class="agent-actions" id="${cardId}-actions">
      <button class="btn-approve" onclick="approveChain('${cardId}')">\u2713 Approve &amp; Run All</button>
      <button class="btn-reject"  onclick="rejectQuery('${cardId}')">\u2717 Reject</button>
    </div>
    <div class="msg-meta">\uD83D\uDD17 Chain \u00b7 ${n} steps \u00b7 ${data.time_ms}ms</div>
  `;
  chatArea.appendChild(card);
  chatArea.scrollTop = chatArea.scrollTop;
}

function addDeepDiveCard(question, data) {
  const cardId = 'agent-card-' + (agentCardCounter++);
  pendingQueries[cardId] = {
    agent_type: 'deep_dive', question,
    steps: data.steps || [], summary_prompt: data.summary_prompt || '',
    entity_label: data.entity_label || '', from_cache: data.from_cache || false,
  };

  const entityIcon = data.entity_type === 'project' ? '\uD83D\uDCCB' : '\uD83C\uDFE2';
  const n          = (data.steps || []).length;
  const stepsHtml  = (data.steps || []).map(s => `
    <div class="deepdive-step">
      <div class="deepdive-step-num">${s.step}</div>
      <div class="deepdive-step-body">
        <div class="deepdive-step-label">${escHtml(s.explanation)}</div>
        <div class="step-tables">${tagsHtml(s.tables)}</div>
      </div>
    </div>
  `).join('');

  const card = document.createElement('div');
  card.className = 'msg-deepdive-preview';
  card.id = cardId;
  card.innerHTML = `
    <div class="deepdive-header">${entityIcon} Deep Dive \u2014 ${escHtml(data.entity_type || '')}</div>
    <div class="deepdive-entity-label">${escHtml(data.entity_label)}</div>
    ${stepsHtml}
    <div class="agent-actions" id="${cardId}-actions" style="margin-top:14px;">
      <button class="btn-approve" onclick="approveChain('${cardId}')">\u2713 Run Deep Dive</button>
      <button class="btn-reject"  onclick="rejectQuery('${cardId}')">\u2717 Cancel</button>
    </div>
    <div class="msg-meta">\uD83D\uDD0D Deep Dive \u00b7 ${n} queries \u00b7 ${data.time_ms}ms</div>
  `;
  chatArea.appendChild(card);
  chatArea.scrollTop = chatArea.scrollHeight;
}

// ── Approve / reject ─────────────────────────────────────────────────────────

async function approveQuery(cardId) {
  const pending = pendingQueries[cardId];
  if (!pending) return;

  const isRlRetry = pending._rlRetry || false;
  pending._rlRetry = false;

  setDisabled(true);
  const actionsEl = document.getElementById(cardId + '-actions');
  if (actionsEl) {
    actionsEl.innerHTML = '<div class="agent-running"><span></span><span></span><span></span> Running query\u2026</div>';
  }

  try {
    const res  = await fetch('/approve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pending),
    });
    const data = await res.json();

    if (data.rate_limited) {
      if (isRlRetry) {
        if (actionsEl) {
          actionsEl.innerHTML =
            `<div style="color:#c92a2a;font-size:13px;margin-bottom:10px;">
               Service busy. Your query is saved. Try again in a minute.
             </div>
             <button class="btn btn-secondary" onclick="approveQuery('${escHtml(cardId)}')">Try Again</button>
             <button class="btn btn-secondary" onclick="rejectQuery('${escHtml(cardId)}')">Cancel</button>`;
        }
      } else {
        pending._rlRetry = true;
        const secs = data.retry_after || 60;
        if (actionsEl) {
          actionsEl.innerHTML =
            `<div class="rl-notice" style="text-align:left;padding:10px 14px;">
               Processing your request. Please wait\u2026
               <span class="rl-countdown" id="${escHtml(cardId)}-rl-count">${secs}s</span>
             </div>`;
        }
        let remaining = secs;
        const iv = setInterval(() => {
          remaining--;
          const el = document.getElementById(cardId + '-rl-count');
          if (el) el.textContent = remaining + 's';
          if (remaining <= 0) { clearInterval(iv); approveQuery(cardId); }
        }, 1000);
      }
      setDisabled(false);
      return;
    }

    const card = document.getElementById(cardId);
    if (card) card.remove();
    const rowWord = data.rows_returned === 1 ? 'row' : 'rows';
    addMessage(data.answer || 'No results.', 'ai',
      `\uD83E\uDD16 Agent \u00b7 ${data.rows_returned} ${rowWord} \u00b7 ${data.time_ms}ms`);
  } catch (err) {
    const card = document.getElementById(cardId);
    if (card) card.remove();
    addMessage('Something went wrong executing the query. Please try again.', 'ai', '\uD83E\uDD16 Agent');
  }

  delete pendingQueries[cardId];
  setDisabled(false);
  input.focus();
}

async function approveChain(cardId) {
  const pending = pendingQueries[cardId];
  if (!pending) return;

  const isRlRetry  = pending._rlRetry || false;
  pending._rlRetry = false;

  setDisabled(true);

  const steps       = pending.steps || [];
  const isDeepDive  = pending.agent_type === 'deep_dive';
  const icon        = isDeepDive ? '\uD83D\uDD0D' : '\uD83D\uDD17';
  const progressMsgs = [
    ...steps.map((s, i) => `Running step ${i + 1} of ${steps.length}: ${s.explanation}\u2026`),
    'Analysing combined results\u2026',
  ];
  let msgIdx = 0;

  const actionsEl = document.getElementById(cardId + '-actions');
  if (actionsEl) {
    actionsEl.innerHTML = `
      <div class="agent-running">
        <span></span><span></span><span></span>
        <span id="${cardId}-progress-text" style="background:none;border-radius:0;width:auto;height:auto;animation:none;">
          ${escHtml(progressMsgs[0])}
        </span>
      </div>`;
  }

  const progressInterval = setInterval(() => {
    msgIdx = (msgIdx + 1) % progressMsgs.length;
    const el = document.getElementById(cardId + '-progress-text');
    if (el) el.textContent = progressMsgs[msgIdx];
  }, 3000);

  try {
    const res  = await fetch('/approve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pending),
    });
    const data = await res.json();
    clearInterval(progressInterval);

    if (data.rate_limited) {
      if (isRlRetry) {
        if (actionsEl) {
          actionsEl.innerHTML =
            `<div style="color:#c92a2a;font-size:13px;margin-bottom:10px;">
               Service busy. Your query is saved. Try again in a minute.
             </div>
             <button class="btn btn-secondary" onclick="approveChain('${escHtml(cardId)}')">Try Again</button>
             <button class="btn btn-secondary" onclick="rejectQuery('${escHtml(cardId)}')">Cancel</button>`;
        }
      } else {
        pending._rlRetry = true;
        const secs = data.retry_after || 60;
        if (actionsEl) {
          actionsEl.innerHTML =
            `<div class="rl-notice" style="text-align:left;padding:10px 14px;">
               Processing your request. Please wait\u2026
               <span class="rl-countdown" id="${escHtml(cardId)}-rl-count">${secs}s</span>
             </div>`;
        }
        let remaining = secs;
        const iv = setInterval(() => {
          remaining--;
          const el = document.getElementById(cardId + '-rl-count');
          if (el) el.textContent = remaining + 's';
          if (remaining <= 0) { clearInterval(iv); approveChain(cardId); }
        }, 1000);
      }
      setDisabled(false);
      return;
    }

    const card = document.getElementById(cardId);
    if (card) card.remove();
    const totalRows = data.total_rows ?? 0;
    const rowWord   = totalRows === 1 ? 'row' : 'rows';
    addMessage(data.answer || 'No results.', 'ai',
      `${icon} ${isDeepDive ? 'Deep Dive' : 'Chain'} \u00b7 ${totalRows} ${rowWord} \u00b7 ${data.time_ms}ms`);
  } catch (err) {
    clearInterval(progressInterval);
    const card = document.getElementById(cardId);
    if (card) card.remove();
    addMessage('Something went wrong executing the queries. Please try again.', 'ai',
      `${icon} ${isDeepDive ? 'Deep Dive' : 'Chain'}`);
  }

  delete pendingQueries[cardId];
  setDisabled(false);
  input.focus();
}

async function rejectQuery(cardId) {
  const pending = pendingQueries[cardId];
  try {
    await fetch('/reject', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: pending?.question || '',
        sql: pending?.sql || (pending?.steps?.[0]?.sql || ''),
      }),
    });
  } catch (_) {}

  const card = document.getElementById(cardId);
  if (card) card.remove();
  addMessage(
    'Query rejected.\n\nTry rephrasing your question \u2014 for example:\n' +
    '- Be more specific about the time period or customer name\n' +
    '- Ask about a single metric at a time\n' +
    '- Use exact names if you know them',
    'ai', '\uD83E\uDD16 Agent'
  );

  delete pendingQueries[cardId];
  setDisabled(false);
  input.focus();
}

// ── Main send flow ────────────────────────────────────────────────────────────

async function sendQuestion(question, _isRetry = false) {
  if (busy || !question.trim()) return;

  if (!_isRetry) { addMessage(question, 'user'); input.value = ''; }
  setDisabled(true);
  if (!_isRetry) showLoading();

  try {
    const res  = await fetch('/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    hideLoading();

    if (data.rate_limited) {
      if (_isRetry) {
        _cancelRlCountdown();
        addMessage('Service busy. Your query is saved. Try again in a minute.', 'ai', '\u23f3 Rate limited');
        setDisabled(false);
      } else {
        showRateLimitCountdown(data.retry_after || 60, () => sendQuestion(question, true));
      }
      return;
    }

    if (data.mode === 'deep_dive') {
      if (!(data.steps || []).length) {
        addMessage(data.warnings?.[0] || "I couldn't identify the entity to look up.",
          'ai', '\uD83D\uDD0D Deep Dive \u00b7 ' + data.time_ms + 'ms');
        setDisabled(false);
      } else {
        addDeepDiveCard(question, data);
      }
    } else if (data.mode === 'chain') {
      if (!(data.steps || []).length) {
        addMessage(data.warnings?.[0] || "I couldn't build a query for that question.",
          'ai', '\uD83D\uDD17 Chain \u00b7 ' + data.time_ms + 'ms');
        setDisabled(false);
      } else {
        addChainCard(question, data);
      }
    } else if (data.mode === 'agent') {
      if (!data.sql) {
        addMessage(data.explanation || "I couldn't find an answer to that question in this database.",
          'ai', '\uD83E\uDD16 Agent \u00b7 ' + data.time_ms + 'ms');
        setDisabled(false);
      } else {
        addAgentCard(question, data);
      }
    } else {
      const intent = data.intent ? data.intent + ' \u00b7 ' : '';
      addMessage(data.answer, 'ai', `\u26a1 Template \u00b7 ${intent}${data.time_ms}ms`);
      setDisabled(false);
    }
  } catch (err) {
    hideLoading();
    _cancelRlCountdown();
    addMessage('Something went wrong. Please try again.', 'ai');
    setDisabled(false);
  }

  input.focus();
}

function sendFromInput() {
  sendQuestion(input.value);
}

// ── Init: restore history or show static greeting ────────────────────────────

(function init() {
  const history = _restoreHistory();
  if (history.length > 0) {
    for (const entry of history) {
      const msg = document.createElement('div');
      msg.className = 'msg msg-' + entry.type;
      const t = document.createElement('div');
      t.className = 'msg-text';
      if (entry.type === 'ai') { t.innerHTML = marked.parse(entry.text || ''); }
      else { t.textContent = entry.text; }
      msg.appendChild(t);
      if (entry.meta) {
        const m = document.createElement('div');
        m.className = 'msg-meta';
        m.textContent = entry.meta;
        msg.appendChild(m);
      }
      chatArea.appendChild(msg);
    }
    chatArea.scrollTop = chatArea.scrollHeight;
  } else {
    addMessage(
      'Hello! I\'m ready to help you explore your database.\n\n' +
      'Ask me anything \u2014 I\'ll generate the SQL, show it to you for review, then run it and explain the results.',
      'ai'
    );
  }
  input.focus();
})();
