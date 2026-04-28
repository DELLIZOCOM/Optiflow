// ==========================================================================
// OptiFlow AI — Email management page
//
// Talks to the same endpoints as the setup wizard's email step.
// Supports three providers; only one can be active at a time:
//   GET    /setup/email/status                       (provider-aware payload)
//   POST   /setup/email/outlook/test  | /setup/email/outlook  | DELETE /setup/email/outlook
//   POST   /setup/email/imap/test     | /setup/email/imap     | DELETE /setup/email/imap
// ==========================================================================

let _lastStatus     = null;
let _pollTimer      = null;
let _activityTimer  = null;
let _reconfigMode   = false;
let _emailProvider  = null;     // 'outlook' | 'godaddy' | 'generic'
let _pendingRemoveEmail = null; // mailbox email queued in the remove modal

// ── Status helpers (mirrors setup.js) ─────────────────────────────────────
function setStatus(id, type, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'status show ' + type;
  el.textContent = msg;
}
function clearStatus(id) {
  const el = document.getElementById(id);
  if (!el) return;
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

// ── Bootstrap ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  refreshStatus();
  // Wire up provider picker
  document.querySelectorAll('#email-provider-picker .provider-card').forEach(card => {
    card.addEventListener('click', () => selectProvider(card.dataset.provider));
  });
  // Poll every 30s while the tab is visible
  _pollTimer = setInterval(() => {
    if (!document.hidden) refreshStatus({ silent: true });
  }, 30000);
  // Refresh activity feed every 60s — its own cadence so the feed stays fresh
  // without making the status endpoint twice as chatty.
  _activityTimer = setInterval(() => {
    if (!document.hidden && _lastStatus && _lastStatus.configured) {
      loadRecentMessages({ silent: true });
    }
  }, 60000);
});

window.addEventListener('beforeunload', () => {
  if (_pollTimer)     clearInterval(_pollTimer);
  if (_activityTimer) clearInterval(_activityTimer);
});

// ── Status fetch + render ─────────────────────────────────────────────────
async function refreshStatus({ silent } = {}) {
  if (!silent) _setTopStatus('off', 'Loading status…');
  try {
    const res  = await fetch('/setup/email/status');
    const data = await res.json();
    _lastStatus = data;
    _render(data);
  } catch (e) {
    _setTopStatus('error', 'Could not reach server: ' + e.message);
  }
}

function _setTopStatus(state, label) {
  document.getElementById('status-dot').className = 'status-dot ' + state;
  document.getElementById('status-label').textContent = label;
}

function _render(s) {
  const connectPanel = document.getElementById('panel-connect');
  const dashPanel    = document.getElementById('panel-dashboard');
  const syncBtn      = document.getElementById('btn-sync-now');
  const cadenceEl    = document.getElementById('status-cadence');

  if (!s.configured || _reconfigMode) {
    connectPanel.style.display = 'block';
    dashPanel.style.display    = 'none';
    document.getElementById('btn-cancel-reconfig').style.display = _reconfigMode ? 'inline-flex' : 'none';
    if (_reconfigMode) {
      _setTopStatus('pending', 'Reconfiguring email — pick a provider and enter credentials.');
    } else {
      _setTopStatus('off', 'Not connected. Pick a provider to start.');
    }
    if (syncBtn)   syncBtn.style.display   = 'none';
    if (cadenceEl) cadenceEl.textContent   = '';
    return;
  }

  connectPanel.style.display = 'none';
  dashPanel.style.display    = 'block';

  const provider = s.provider || 'outlook';   // 'outlook' | 'imap'
  const mb = s.mailboxes || {total:0, active:0, with_errors:0, initial_synced:0};
  const total = mb.total|0, active = mb.active|0, errs = mb.with_errors|0, synced = mb.initial_synced|0;

  // Top status dot
  if (!s.live) {
    _setTopStatus('pending', 'Configured, starting up…');
  } else if (errs > 0) {
    _setTopStatus('error', `Live — ${errs} mailbox${errs === 1 ? '' : 'es'} with errors`);
  } else if (total === 0) {
    _setTopStatus('pending', 'Live — discovering mailboxes…');
  } else if (synced < total) {
    _setTopStatus('pending', `Live — syncing (${synced}/${total} mailboxes complete)`);
  } else {
    _setTopStatus('live', `Live — ${total} mailbox${total === 1 ? '' : 'es'} indexed`);
  }

  // Cadence label — answers "when will the new emails be added?"
  if (cadenceEl) {
    cadenceEl.textContent = (provider === 'imap')
      ? '· auto-syncs every 5 min'
      : '· auto-syncs every 10 min';
  }
  // Sync-now is wired for IMAP today (Outlook gets a no-op note from server)
  if (syncBtn) {
    syncBtn.style.display = (provider === 'imap') ? 'inline-flex' : 'none';
  }

  // Dashboard header
  const providerLabel = (provider === 'imap')
    ? `${_imapProviderLabel(s.imap_provider)} · IMAP ${s.host || ''}${s.port ? ':' + s.port : ''}${s.use_ssl === false ? ' (plain)' : ''}`
    : 'Microsoft 365 / Outlook (Microsoft Graph)';
  document.getElementById('dash-title').textContent    = s.display_name || (provider === 'imap' ? 'Company Email (IMAP)' : 'Outlook');
  document.getElementById('dash-tenant').textContent   =
    s.tenant_id ? (provider === 'imap' ? `Server: ${s.tenant_id}` : `Tenant ID: ${s.tenant_id}`) : '';
  document.getElementById('dash-provider').textContent = providerLabel;

  // Stats
  document.getElementById('stat-mailboxes').textContent     = total.toLocaleString();
  document.getElementById('stat-mailboxes-sub').textContent =
    total > 0 ? `${active} active · ${errs} with errors` : '';
  document.getElementById('stat-messages').textContent      = (s.messages_total || 0).toLocaleString();
  document.getElementById('stat-synced').textContent        = total > 0 ? `${synced}/${total}` : '0';
  document.getElementById('stat-last-sync').textContent     = _fmtTime(s.last_sync_at);

  // ── Mailbox management table (provider-aware) ──────────────────────────
  _renderMailboxTable(provider, s);

  // ── Recent activity feed (provider-agnostic) ───────────────────────────
  document.getElementById('activity-section').style.display = 'block';
  loadRecentMessages({ silent: true });

  // Errors (kept as a separate banner above the table for fast triage)
  const errSection = document.getElementById('errors-section');
  const errList    = document.getElementById('error-list');
  if (s.errors && s.errors.length) {
    errSection.style.display = 'block';
    errList.innerHTML = s.errors.map(e => `
      <div class="error-row">
        <div class="error-mailbox">${_esc(e.mailbox || 'unknown')}</div>
        <div class="error-msg">${_esc(e.error || 'unknown error')}</div>
      </div>
    `).join('');
  } else {
    errSection.style.display = 'none';
    errList.innerHTML = '';
  }
}

// ── Mailbox table render ──────────────────────────────────────────────────
function _renderMailboxTable(provider, s) {
  const section = document.getElementById('mailbox-table-section');
  const table   = document.getElementById('mailbox-table');
  const hint    = document.getElementById('mailbox-table-hint');
  const addBtn  = document.getElementById('btn-toggle-add-mailbox');

  const rows = Array.isArray(s.mailbox_details) ? s.mailbox_details : [];
  if (!rows.length) {
    section.style.display = 'none';
    table.innerHTML = '';
    if (addBtn) addBtn.style.display = (provider === 'imap') ? 'inline-flex' : 'none';
    return;
  }

  section.style.display = 'block';
  if (addBtn) addBtn.style.display = (provider === 'imap') ? 'inline-flex' : 'none';
  hint.textContent = (provider === 'imap')
    ? 'Sync now triggers an immediate poll. Remove stops polling and (optionally) wipes the mailbox cache.'
    : 'Outlook syncs via Microsoft Graph delta automatically; per-mailbox controls are read-only here.';

  const head = `
    <div class="mb-row mb-head">
      <div>Mailbox</div>
      <div>Status</div>
      <div>Messages</div>
      <div>Last sync</div>
      <div></div>
    </div>`;

  const body = rows.map(r => {
    const email   = r.account_email || '';
    const display = r.display_name && r.display_name !== email ? r.display_name : '';
    const folder  = r.folder || 'INBOX';
    const errMsg  = r.last_error || '';
    const status  = _mbStatusBadge(r);
    const synced  = r.initial_synced
      ? `${(r.message_count|0).toLocaleString()}`
      : `${(r.message_count|0).toLocaleString()} <span class="attach-pin">syncing…</span>`;
    const last    = _fmtTime(r.last_sync_at);
    const meta    = errMsg
      ? `<span style="color:var(--danger);">${_esc(errMsg)}</span>`
      : `folder: <code>${_esc(folder)}</code>`;
    const rowCls  = errMsg ? 'mb-row is-error'
                  : (r.status === 'disabled' ? 'mb-row is-disabled' : 'mb-row');

    const actions = (provider === 'imap')
      ? `
        <button class="btn btn-ghost btn-small"
                onclick="syncNow('${_jsAttr(r.id)}')"
                title="Trigger an immediate sync">Sync</button>
        <button class="btn btn-ghost btn-small"
                onclick="openRemoveMailbox('${_jsAttr(email)}')"
                title="Stop polling this mailbox">Remove</button>`
      : `<span class="status-cadence">read-only</span>`;

    return `
      <div class="${rowCls}">
        <div class="mb-cell-email">
          <span class="mb-email-addr">${_esc(email)}</span>
          ${display ? `<span class="mb-display-name">${_esc(display)}</span>` : ''}
          <span class="mb-cell-meta">${meta}</span>
        </div>
        <div class="mb-cell-status ${status.cls}">
          <span class="dot"></span>${_esc(status.label)}
        </div>
        <div class="mb-cell-count">${synced}</div>
        <div class="mb-cell-sync">${_esc(last)}</div>
        <div class="mb-cell-actions">${actions}</div>
      </div>`;
  }).join('');

  table.innerHTML = head + body;
}

function _mbStatusBadge(r) {
  if (r.status === 'disabled') return { cls: 'disabled', label: 'Disabled' };
  if (r.last_error)            return { cls: 'error',    label: 'Error'    };
  if (!r.initial_synced)       return { cls: 'pending',  label: 'Syncing'  };
  return { cls: 'ok', label: 'Active' };
}

function _imapProviderLabel(p) {
  if (p === 'godaddy') return 'GoDaddy Workspace';
  if (p === 'generic') return 'Generic IMAP';
  return p ? String(p) : 'IMAP';
}

function _fmtTime(ts) {
  if (!ts) return 'never';
  const now   = Date.now() / 1000;
  const delta = Math.max(0, now - Number(ts));
  if (delta < 60)    return 'just now';
  if (delta < 3600)  return `${Math.floor(delta/60)} min ago`;
  if (delta < 86400) return `${Math.floor(delta/3600)} h ago`;
  try {
    return new Date(Number(ts) * 1000).toLocaleString();
  } catch { return '—'; }
}

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escAttr(s) {
  return _esc(s);
}

// ── Provider picker ───────────────────────────────────────────────────────
function selectProvider(provider) {
  _emailProvider = provider;

  document.querySelectorAll('#email-provider-picker .provider-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.provider === provider);
    const radio = card.querySelector('input[type="radio"]');
    if (radio) radio.checked = (card.dataset.provider === provider);
  });

  document.getElementById('email-common-fields').style.display = 'block';
  document.getElementById('email-outlook-form').style.display = (provider === 'outlook') ? 'block' : 'none';
  document.getElementById('email-imap-form').style.display    = (provider === 'godaddy' || provider === 'generic') ? 'block' : 'none';
  document.getElementById('imap-godaddy-hint').style.display  = (provider === 'godaddy') ? 'block' : 'none';
  document.getElementById('imap-generic-hint').style.display  = (provider === 'generic') ? 'block' : 'none';

  const hostEl = document.getElementById('inp-imap-host');
  const portEl = document.getElementById('inp-imap-port');
  const sslEl  = document.getElementById('inp-imap-ssl');
  if (provider === 'godaddy') {
    hostEl.value = 'imap.secureserver.net';
    hostEl.readOnly = true;
    portEl.value = 993;
    sslEl.value = 'ssl';
  } else if (provider === 'generic') {
    if (hostEl.readOnly) hostEl.value = '';
    hostEl.readOnly = false;
    if (!portEl.value) portEl.value = 993;
  }

  if (provider === 'godaddy' || provider === 'generic') {
    const wrap = document.getElementById('mailbox-rows');
    if (wrap.children.length === 0) addMailboxRow();
  }

  document.getElementById('btn-email-test').disabled = false;
  document.getElementById('btn-email-save').disabled = false;

  clearStatus('email-form-status');
}

// ── Mailbox list editor ───────────────────────────────────────────────────
function addMailboxRow(values = {}) {
  const wrap = document.getElementById('mailbox-rows');
  const row  = document.createElement('div');
  row.className = 'mailbox-row';
  row.innerHTML = `
    <input type="email"    placeholder="user@company.com"   class="mb-email"    autocomplete="off" spellcheck="false" value="${escAttr(values.account_email || '')}">
    <input type="password" placeholder="password / app pw"   class="mb-pass"     autocomplete="new-password"          value="${escAttr(values.password || '')}">
    <input type="text"     placeholder="Display name (opt.)" class="mb-display"  autocomplete="off"                    value="${escAttr(values.display_name || '')}">
    <input type="text"     placeholder="INBOX"               class="mb-folder"   autocomplete="off"                    value="${escAttr(values.folder || 'INBOX')}">
    <button type="button" class="btn-remove-mailbox" title="Remove this mailbox" aria-label="Remove">×</button>
  `;
  row.querySelector('.btn-remove-mailbox').addEventListener('click', () => {
    row.remove();
    if (document.getElementById('mailbox-rows').children.length === 0) addMailboxRow();
  });
  wrap.appendChild(row);
}

function _readMailboxRows() {
  const out = [];
  document.querySelectorAll('#mailbox-rows .mailbox-row').forEach(row => {
    const account_email = row.querySelector('.mb-email').value.trim().toLowerCase();
    const password      = row.querySelector('.mb-pass').value;
    const display_name  = row.querySelector('.mb-display').value.trim();
    const folder        = row.querySelector('.mb-folder').value.trim() || 'INBOX';
    if (!account_email && !password) return;
    out.push({ account_email, password, display_name: display_name || null, folder });
  });
  return out;
}

// ── Field collection / validation ─────────────────────────────────────────
function _commonFields() {
  return {
    tenant_display_name: document.getElementById('inp-email-display').value.trim(),
    backfill_days:       parseInt(document.getElementById('inp-email-backfill').value, 10) || 365,
  };
}

function _outlookFields() {
  return {
    ..._commonFields(),
    tenant_id:     document.getElementById('inp-email-tenant').value.trim(),
    client_id:     document.getElementById('inp-email-client').value.trim(),
    client_secret: document.getElementById('inp-email-secret').value,
  };
}

function _imapFields() {
  const sslVal = document.getElementById('inp-imap-ssl').value;
  return {
    ..._commonFields(),
    provider:  _emailProvider,
    host:      document.getElementById('inp-imap-host').value.trim(),
    port:      parseInt(document.getElementById('inp-imap-port').value, 10) || 993,
    use_ssl:   sslVal === 'ssl',
    mailboxes: _readMailboxRows(),
  };
}

function _validateOutlook(f) {
  if (!f.tenant_display_name)                          return 'Display name is required.';
  if (!f.tenant_id || f.tenant_id.length < 8)          return 'Tenant ID looks too short.';
  if (!f.client_id || f.client_id.length < 8)          return 'Client ID looks too short.';
  if (!f.client_secret || f.client_secret.length < 8)  return 'Client secret is required.';
  return null;
}

function _validateImap(f) {
  if (!f.tenant_display_name)                  return 'Display name is required.';
  if (!f.host || f.host.length < 3)            return 'IMAP host is required.';
  if (!f.port || f.port < 1 || f.port > 65535) return 'IMAP port is invalid.';
  if (!f.mailboxes || !f.mailboxes.length)     return 'Add at least one mailbox.';
  for (const m of f.mailboxes) {
    if (!m.account_email || !/^[^\s@]+@[^\s@]+$/.test(m.account_email)) return `Invalid email: "${m.account_email || ''}"`;
    if (!m.password)                                                    return `Password missing for ${m.account_email}.`;
  }
  return null;
}

// ── Test / Save ───────────────────────────────────────────────────────────
async function testEmail() {
  if (!_emailProvider) { setStatus('email-form-status', 'error', 'Pick a provider above first.'); return; }

  let body, url, infoMsg;
  if (_emailProvider === 'outlook') {
    body = _outlookFields();
    const err = _validateOutlook(body);
    if (err) { setStatus('email-form-status', 'error', err); return; }
    url = '/setup/email/outlook/test';
    infoMsg = 'Asking Microsoft Graph for a token and listing a mailbox…';
  } else {
    body = _imapFields();
    const err = _validateImap(body);
    if (err) { setStatus('email-form-status', 'error', err); return; }
    url = '/setup/email/imap/test';
    infoMsg = `Logging in to ${body.host}:${body.port} for each mailbox…`;
  }

  clearStatus('email-form-status');
  setLoading('btn-email-test', true, 'Testing…');
  setStatus('email-form-status', 'info', infoMsg);

  try {
    const res  = await fetch(url, {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.success) {
      const extra = (data.mailbox_count != null) ? ` (${data.mailbox_count} mailbox(es))` : '';
      setStatus('email-form-status', 'success', 'Credentials accepted' + extra + '.');
    } else {
      setStatus('email-form-status', 'error', data.error || 'Test failed.');
    }
  } catch (e) {
    setStatus('email-form-status', 'error', 'Network error: ' + e.message);
  }

  setLoading('btn-email-test', false);
}

async function saveEmail() {
  if (!_emailProvider) { setStatus('email-form-status', 'error', 'Pick a provider above first.'); return; }

  let body, url;
  if (_emailProvider === 'outlook') {
    body = _outlookFields();
    const err = _validateOutlook(body);
    if (err) { setStatus('email-form-status', 'error', err); return; }
    url = '/setup/email/outlook';
  } else {
    body = _imapFields();
    const err = _validateImap(body);
    if (err) { setStatus('email-form-status', 'error', err); return; }
    url = '/setup/email/imap';
  }

  clearStatus('email-form-status');
  setLoading('btn-email-save', true, 'Connecting…');

  try {
    const res  = await fetch(url, {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.success) {
      setStatus('email-form-status', 'success', 'Connected. Ingestion running in the background.');
      _reconfigMode = false;
      // Clear secrets out of the DOM
      const sec = document.getElementById('inp-email-secret');
      if (sec) sec.value = '';
      document.querySelectorAll('#mailbox-rows .mb-pass').forEach(i => i.value = '');
      await refreshStatus();
    } else {
      setStatus('email-form-status', 'error', data.error || 'Save failed.');
    }
  } catch (e) {
    setStatus('email-form-status', 'error', 'Network error: ' + e.message);
  }

  setLoading('btn-email-save', false);
}

// ── Reconfigure ───────────────────────────────────────────────────────────
function openReconfigure() {
  _reconfigMode = true;
  // Preselect the matching provider so the UI looks like a normal connect form
  const s = _lastStatus || {};
  let prov = null;
  if (s.provider === 'outlook') prov = 'outlook';
  else if (s.provider === 'imap') prov = (s.imap_provider === 'godaddy') ? 'godaddy' : 'generic';

  if (prov) selectProvider(prov);

  // Pre-fill what we can; secrets / passwords are never returned by the server
  if (s.display_name) {
    const d = document.getElementById('inp-email-display');
    if (d) d.value = s.display_name;
  }
  if (s.provider === 'outlook' && s.tenant_id) {
    const t = document.getElementById('inp-email-tenant');
    if (t) t.value = s.tenant_id;
  }
  if (s.provider === 'imap') {
    const h = document.getElementById('inp-imap-host');
    const p = document.getElementById('inp-imap-port');
    const e = document.getElementById('inp-imap-ssl');
    if (h && s.host) h.value = s.host;
    if (p && s.port) p.value = s.port;
    if (e) e.value = (s.use_ssl === false) ? 'plain' : 'ssl';
    // Pre-fill known mailbox rows (no passwords — user must re-enter)
    const wrap = document.getElementById('mailbox-rows');
    wrap.innerHTML = '';
    (s.configured_mailboxes || []).forEach(m => addMailboxRow({
      account_email: m.account_email,
      folder:        m.folder,
    }));
    if (wrap.children.length === 0) addMailboxRow();
  }

  _render(s);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function cancelReconfigure() {
  _reconfigMode = false;
  clearStatus('email-form-status');
  _render(_lastStatus || {});
}

// ── Disconnect modal ──────────────────────────────────────────────────────
function openDisconnect() {
  clearStatus('disconnect-status');
  document.getElementById('chk-wipe-cache').checked = false;
  const provider = (_lastStatus && _lastStatus.provider) || 'email';
  const label = provider === 'outlook' ? 'Outlook' : (provider === 'imap' ? 'IMAP email' : 'email');
  document.getElementById('modal-disconnect-title').textContent = `Disconnect ${label}?`;
  document.getElementById('modal-disconnect').style.display = 'flex';
}

function closeDisconnect(evt) {
  if (evt && evt.target && evt.target.id !== 'modal-disconnect' && evt.type === 'click') return;
  document.getElementById('modal-disconnect').style.display = 'none';
}

// ── Sync now (top-bar + per-row) ──────────────────────────────────────────
async function syncNow(mailboxId) {
  const btnId = 'btn-sync-now';
  const isTopButton = !mailboxId;
  if (isTopButton) setLoading(btnId, true, 'Syncing…');

  try {
    const res  = await fetch('/setup/email/sync_now', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ mailbox_id: mailboxId || null }),
    });
    const data = await res.json();
    if (data.success) {
      // Give the poller a couple of seconds to finish a cycle, then refresh.
      setTimeout(() => refreshStatus({ silent: true }), 2500);
      setTimeout(() => loadRecentMessages({ silent: true }), 4000);
    } else {
      alert(data.error || 'Sync failed.');
    }
  } catch (e) {
    alert('Network error: ' + e.message);
  }
  if (isTopButton) setLoading(btnId, false);
}

// ── Inline add-mailbox (IMAP only) ────────────────────────────────────────
function toggleInlineAddMailbox() {
  const wrap = document.getElementById('inline-add-mailbox');
  const open = wrap.style.display === 'block';
  wrap.style.display = open ? 'none' : 'block';
  if (!open) {
    document.getElementById('add-mb-email').value   = '';
    document.getElementById('add-mb-pass').value    = '';
    document.getElementById('add-mb-display').value = '';
    document.getElementById('add-mb-folder').value  = 'INBOX';
    clearStatus('add-mb-status');
    setTimeout(() => document.getElementById('add-mb-email').focus(), 50);
  }
}

async function submitInlineAddMailbox() {
  const email   = document.getElementById('add-mb-email').value.trim().toLowerCase();
  const pass    = document.getElementById('add-mb-pass').value;
  const display = document.getElementById('add-mb-display').value.trim();
  const folder  = document.getElementById('add-mb-folder').value.trim() || 'INBOX';

  if (!email || !/^[^\s@]+@[^\s@]+$/.test(email)) {
    setStatus('add-mb-status', 'error', 'Enter a valid email address.');
    return;
  }
  if (!pass) {
    setStatus('add-mb-status', 'error', 'Password is required.');
    return;
  }

  setLoading('btn-confirm-add-mb', true, 'Adding…');
  setStatus('add-mb-status', 'info', 'Logging in to verify the mailbox…');

  try {
    const res  = await fetch('/setup/email/imap/mailboxes', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({
        account_email: email,
        password:      pass,
        display_name:  display || null,
        folder:        folder,
      }),
    });
    const data = await res.json();
    if (data.success) {
      setStatus('add-mb-status', 'success', `Added ${email}. Initial sync starting now.`);
      // Clear the password before the form closes so it's not lurking in the DOM
      document.getElementById('add-mb-pass').value = '';
      setTimeout(() => {
        toggleInlineAddMailbox();
        refreshStatus({ silent: true });
      }, 800);
    } else {
      setStatus('add-mb-status', 'error', data.error || 'Add failed.');
    }
  } catch (e) {
    setStatus('add-mb-status', 'error', 'Network error: ' + e.message);
  }
  setLoading('btn-confirm-add-mb', false);
}

// ── Remove-mailbox modal ──────────────────────────────────────────────────
function openRemoveMailbox(email) {
  _pendingRemoveEmail = email;
  document.getElementById('rm-mb-email').textContent  = email;
  document.getElementById('chk-purge-mb').checked     = false;
  clearStatus('rm-mb-status');
  document.getElementById('modal-remove-mailbox').style.display = 'flex';
}

function closeRemoveMailbox(evt) {
  if (evt && evt.target && evt.target.id !== 'modal-remove-mailbox' && evt.type === 'click') return;
  document.getElementById('modal-remove-mailbox').style.display = 'none';
  _pendingRemoveEmail = null;
}

async function confirmRemoveMailbox() {
  if (!_pendingRemoveEmail) return;
  const purge = document.getElementById('chk-purge-mb').checked;
  setLoading('btn-confirm-rm-mb', true, 'Removing…');
  clearStatus('rm-mb-status');

  try {
    const res  = await fetch('/setup/email/imap/mailboxes', {
      method:  'DELETE',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({
        account_email: _pendingRemoveEmail,
        purge_cache:   purge,
      }),
    });
    const data = await res.json();
    if (data.success) {
      closeRemoveMailbox();
      await refreshStatus({ silent: true });
    } else {
      setStatus('rm-mb-status', 'error', data.error || 'Remove failed.');
    }
  } catch (e) {
    setStatus('rm-mb-status', 'error', 'Network error: ' + e.message);
  }
  setLoading('btn-confirm-rm-mb', false);
}

// ── Recent activity feed ──────────────────────────────────────────────────
async function loadRecentMessages({ silent } = {}) {
  const list = document.getElementById('activity-list');
  const meta = document.getElementById('activity-meta');
  if (!list) return;

  if (!silent && !list.innerHTML) {
    list.innerHTML = '<div class="activity-empty">Loading…</div>';
  }

  try {
    const res  = await fetch('/setup/email/recent_messages?limit=20');
    const data = await res.json();
    const msgs = Array.isArray(data.messages) ? data.messages : [];

    if (!msgs.length) {
      list.innerHTML = '<div class="activity-empty">No messages indexed yet — initial sync may still be running.</div>';
      meta.textContent = '';
      return;
    }

    list.innerHTML = msgs.map(m => {
      const fromName  = m.from_name && m.from_name !== m.from_email ? m.from_name : '';
      const subject   = (m.subject || '(no subject)').trim() || '(no subject)';
      const preview   = m.preview || '';
      const att       = m.has_attachments ? '<span class="attach-pin" title="Has attachments">📎</span>' : '';
      const mailboxL  = m.account_email
        ? `${_esc(m.account_email)}${m.folder ? ' / ' + _esc(m.folder) : ''}`
        : '';
      return `
        <div class="activity-row">
          <div class="activity-from">
            ${fromName ? `<span class="from-name">${_esc(fromName)}</span>` : ''}
            <span class="from-email">${_esc(m.from_email || '')}</span>
          </div>
          <div class="activity-body">
            <div class="activity-subject">${_esc(subject)}${att}</div>
            <div class="activity-snip">${_esc(preview)}</div>
            ${mailboxL ? `<div class="activity-mailbox">${mailboxL}</div>` : ''}
          </div>
          <div class="activity-time">${_esc(_fmtTime(m.received_at))}</div>
        </div>`;
    }).join('');

    meta.textContent = `Showing ${msgs.length} most recent`;
  } catch (e) {
    if (!silent) list.innerHTML = `<div class="activity-empty">Could not load activity: ${_esc(e.message)}</div>`;
  }
}

// String-attr helper for inline onclick handlers — keeps quotes/HTML safe
function _jsAttr(s) {
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

async function confirmDisconnect() {
  const wipe = document.getElementById('chk-wipe-cache').checked;
  setLoading('btn-confirm-disconnect', true, 'Disconnecting…');
  clearStatus('disconnect-status');

  // Pick the right delete endpoint based on the active provider
  const provider = (_lastStatus && _lastStatus.provider) || 'outlook';
  const url = provider === 'imap' ? '/setup/email/imap' : '/setup/email/outlook';

  try {
    const res = await fetch(url, {
      method:  'DELETE',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ wipe_cache: wipe }),
    });
    const data = await res.json();
    if (data.success) {
      document.getElementById('modal-disconnect').style.display = 'none';
      _reconfigMode = false;
      _emailProvider = null;
      // Reset the connect form so the next interaction starts fresh
      document.querySelectorAll('#email-provider-picker .provider-card').forEach(c => c.classList.remove('selected'));
      document.querySelectorAll('#email-provider-picker input[type="radio"]').forEach(r => r.checked = false);
      document.getElementById('email-common-fields').style.display = 'none';
      document.getElementById('email-outlook-form').style.display  = 'none';
      document.getElementById('email-imap-form').style.display     = 'none';
      document.getElementById('btn-email-test').disabled = true;
      document.getElementById('btn-email-save').disabled = true;
      await refreshStatus();
    } else {
      setStatus('disconnect-status', 'error', data.error || 'Disconnect failed.');
    }
  } catch (e) {
    setStatus('disconnect-status', 'error', 'Network error: ' + e.message);
  }

  setLoading('btn-confirm-disconnect', false);
}
