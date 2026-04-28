# Email Integration — Architecture Plan (Admin-Consent / Org-Wide)

**Status:** design + initial scaffold landed
**Scope:** OptiFlow AI gains an **organization-wide** email source, starting with **Outlook (Microsoft 365)**. Gmail (Google Workspace) follows the same shape.
**Deployment model:** OptiFlow runs inside a company. The company's Microsoft 365 admin registers an Azure AD app, grants **application-level `Mail.Read`** with admin consent, and hands the credentials to OptiFlow. No per-user OAuth prompts. All mailboxes in the tenant are searchable by the agent.

---

## 1. Design stance: FTS-only for MVP, embeddings optional later

We deliberately ship **without vector embeddings** in the first cut.

| Concern | Why FTS wins for email |
| --- | --- |
| **Exact entity recall** | Emails are dense with IDs, names, PO numbers, invoice numbers, project codes. BM25 on tokenized text matches `#8892` exactly; cosine similarity on dense vectors will happily return `#8891` as "close enough". |
| **Debuggability** | An FTS query is a string. You can paste it into `sqlite3` and see what matched and why. Vector retrieval is a black box the first time it returns the wrong thread. |
| **Cost & latency** | No embedding compute on ingest, no vector index to rebuild, no GPU path, no Voyage API bill. Search is one `SELECT … MATCH … ORDER BY bm25(...)` against an SQLite table. |
| **Operational footprint** | Zero new services. `sqlite3` ships with Python. The whole email cache is one file on disk. |
| **Agent synergy** | Modern LLMs are already good at rewriting a natural-language question into keyword variants. We push synonym expansion to the model, where it belongs. Retrieval stays dumb-but-fast. |

**Embeddings are deferred, not cancelled.** The schema reserves a nullable `embedding BLOB` column and the ingest pipeline has a post-insert hook. If we observe recall failures in practice, we layer in local `fastembed` and switch `search_emails` to hybrid BM25 × cosine with reciprocal-rank fusion. That's a 1-day change, not a re-architecture.

---

## 2. Non-goals (MVP)

- **No sending.** Agent is strictly read-only. `Mail.Read` application permission only.
- **No attachment text extraction.** Index filenames + MIME types + sizes; PDF/DOCX/XLSX body extraction is Phase 2.
- **No OCR** on image attachments. Ever, probably.
- **No embeddings.** See §1.
- **No inline calendar parsing.** `.ics` attachments are opaque.
- **No Gmail adapter yet.** Phase 3, same schema/tools shape.
- **No webhooks.** Poll via Graph `$delta`. Webhooks require a public HTTPS endpoint with signed payload verification.
- **No per-user OAuth flow.** This build is admin-consent only — one Azure AD app per tenant, read access to every mailbox.
- **No per-mailbox access policy inside OptiFlow.** If the admin consents, every OptiFlow user in that org can ask questions against every mailbox. Layering role-based redaction on top is Phase 4.
- **No rule-based auto-summarization.** Agent summarizes on demand.

---

## 3. Architecture overview

```
Azure AD tenant                      OptiFlow server
─────────────────                    ────────────────
┌──────────────────┐                 ┌──────────────────────────────┐
│  Admin registers │  tenant_id,     │ Setup UI  /setup/email       │
│  app + grants    │  client_id,     │   ↓                          │
│  Mail.Read       │  client_secret  │ EmailCredentials             │
│  (application)   │ ──────────────▶ │   (Fernet-encrypted, disk)   │
└──────────────────┘                 └──────────────┬───────────────┘
                                                    │ MSAL ConfidentialClientApplication
                                                    │ grant=client_credentials
                                                    │ scope=https://graph.microsoft.com/.default
                                                    ▼
                    Microsoft Graph (app-only)
                    ───────────────────────────
                    GET /users                                  → discover mailboxes
                    GET /users/{id}/messages?$top=100           → initial 30-day sync
                    GET /users/{id}/messages/delta              → incremental sync
                                                    ▲
                                                    │
                                                    ▼
                                    ┌──────────────────────────────┐
                                    │ OutlookSource                │
                                    │  - enumerate mailboxes       │
                                    │  - per-mailbox ingest loop   │
                                    └──────────────┬───────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │ email.db  (SQLite + FTS5)    │
                                    │   mailboxes                  │
                                    │   emails                     │
                                    │   emails_fts (BM25)          │
                                    │   sync_state                 │
                                    └──────────────┬───────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │ Agent tools                  │
                                    │   list_mailboxes             │
                                    │   search_emails              │
                                    │   get_email                  │
                                    │   get_email_thread           │
                                    └──────────────┬───────────────┘
                                                   │
                                                   ▼
                                    Existing ReAct orchestrator
                                    (no changes; tools discovered
                                     via ToolRegistry)
```

The agent loop is untouched. `OutlookSource` registers into `SourceRegistry` alongside DB sources. Email tools register into `ToolRegistry`.

---

## 4. Where code lives

```
app/
├── sources/
│   └── email/
│       ├── base.py                    # EmailSource protocol
│       ├── store.py                   # SQLite + FTS5 wrapper (EmailStore)
│       ├── outlook/
│       │   ├── __init__.py
│       │   ├── source.py              # OutlookSource(DataSource)
│       │   ├── auth.py                # MSAL ConfidentialClientApplication (client credentials)
│       │   ├── graph.py               # Thin async Graph client (httpx)
│       │   ├── ingest.py              # Discover mailboxes + initial + delta + backfill
│       │   └── mapper.py              # Graph JSON → row dict
│       └── gmail/                     # Phase 3 (empty)
├── tools/
│   └── email.py                       # search_emails, get_email, list_mailboxes, get_email_thread
├── routes/
│   └── email.py                       # /setup/email/outlook/*, /status
└── config.py                          # EMAIL_DB_PATH + load/save email credentials
data/
├── cache/
│   ├── sessions.db                    # existing
│   └── email.db                       # NEW
└── config/
    └── email/
        └── outlook.json               # Fernet-encrypted { tenant_id, client_id, client_secret }
```

**Why a separate `email.db`:** sessions are short-lived (30d TTL, ≤1000 entries). Email is long-lived and can be large (millions of rows across an org). Different retention, different vacuum strategy, different backup priority.

---

## 5. Data model

### 5.1 `mailboxes` — one row per discovered mailbox in the tenant

```sql
CREATE TABLE mailboxes (
    id              TEXT PRIMARY KEY,           -- Graph user id (GUID)
    account_email   TEXT NOT NULL UNIQUE,       -- e.g. alice@company.com
    display_name    TEXT,
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'disabled' | 'not_licensed'
    last_sync_at    REAL,
    discovered_at   REAL NOT NULL
);
CREATE INDEX idx_mailboxes_status ON mailboxes(status);
```

### 5.2 `emails` — the canonical table

```sql
CREATE TABLE emails (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id         TEXT    NOT NULL REFERENCES mailboxes(id),
    account_email      TEXT    NOT NULL,

    provider           TEXT    NOT NULL,           -- 'outlook' | 'gmail' (future)
    provider_msg_id    TEXT    NOT NULL,           -- Graph message id
    internet_msg_id    TEXT,                       -- RFC 5322 Message-ID (cross-provider dedupe)
    conversation_id    TEXT,                       -- thread id

    subject            TEXT,
    from_name          TEXT,
    from_email         TEXT,
    to_emails          TEXT,                       -- JSON array
    cc_emails          TEXT,
    bcc_emails         TEXT,

    body_text          TEXT,                       -- plain-text for FTS
    body_html_hash     TEXT,                       -- sha256; full HTML fetched on demand
    has_attachments    INTEGER NOT NULL DEFAULT 0,
    attachment_names   TEXT,                       -- JSON array of filenames

    folder             TEXT,                       -- 'inbox' | 'sent' | 'archive' | custom
    is_read            INTEGER NOT NULL DEFAULT 0,
    importance         TEXT,                       -- 'low' | 'normal' | 'high'

    sent_at            REAL NOT NULL,              -- UTC unix seconds
    received_at        REAL NOT NULL,
    ingested_at        REAL NOT NULL,

    embedding          BLOB,                       -- reserved for Phase 4
    embedding_model    TEXT,

    UNIQUE(mailbox_id, provider_msg_id)
);
CREATE INDEX idx_emails_mailbox_recv   ON emails(mailbox_id, received_at DESC);
CREATE INDEX idx_emails_conversation   ON emails(mailbox_id, conversation_id);
CREATE INDEX idx_emails_from           ON emails(from_email);
CREATE INDEX idx_emails_internet_id    ON emails(internet_msg_id);
```

### 5.3 `emails_fts` — FTS5 virtual table

```sql
CREATE VIRTUAL TABLE emails_fts USING fts5(
    subject, from_name, from_email, to_emails, body_text, attachment_names,
    content='emails', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
```

Plus the three standard insert/update/delete triggers to keep FTS in sync with `emails`.

### 5.4 `sync_state` — per-mailbox ingestion cursor

```sql
CREATE TABLE sync_state (
    mailbox_id      TEXT PRIMARY KEY REFERENCES mailboxes(id),
    delta_link      TEXT,                               -- Graph $deltaLink
    initial_synced  INTEGER NOT NULL DEFAULT 0,
    backfill_cursor TEXT,
    backfill_done   INTEGER NOT NULL DEFAULT 0,
    last_sync_at    REAL,
    last_error      TEXT
);
```

---

## 6. Ingestion pipeline

Admin-consent unlocks a different workflow from per-user OAuth: OptiFlow can enumerate all tenant mailboxes and ingest each one in parallel.

**On connect (one-time):**
1. Admin supplies `{tenant_id, client_id, client_secret}` via setup UI.
2. Backend validates by acquiring a token and calling `GET /v1.0/users?$top=1`.
3. Credentials encrypted (Fernet), written to `data/config/email/outlook.json`.
4. Mailbox discovery task enqueues: `GET /users?$select=id,mail,displayName,accountEnabled&$top=999` (paginated). Inserts into `mailboxes`.

**Per-mailbox, ongoing:**
1. **Initial sync (eager).** Pull last **30 days** of inbox + sent. Page `GET /users/{id}/messages?$top=100&$orderby=receivedDateTime desc` until older than cutoff. Store `@odata.deltaLink` from the final page → `sync_state.delta_link`. Mark `initial_synced=1`.
2. **Delta sync (every 10 min).** Hit the saved `delta_link`. Graph returns new + updated + deleted since last cursor. Update `delta_link` with the new one. Cheap.
3. **Backfill (background, 1 page/min).** Walk backward from "30 days ago" in pages of 1000. Stops at configurable horizon (default: 1 year). Runs slower than delta so it never starves real-time ingestion.

**Concurrency model.**
- One `asyncio.Task` per mailbox for delta sync (started/stopped as mailboxes come online / are disabled).
- A shared `asyncio.Semaphore(6)` across the whole tenant caps Graph concurrency so we never trip `429`.
- Backfill runs on a single low-priority worker iterating mailboxes round-robin.

**Error handling.**
- 429 → respect `Retry-After`, exponential backoff.
- 401 → refresh token via MSAL silent acquire; if the secret rotated, mark tenant `reauth_required` and surface on `/setup/email/status`.
- 5xx / network → retry 3×, then defer to next tick.
- Per-mailbox `last_error` stored so the UI can show which mailboxes are unhealthy.

**Freshness SLO.** New mail appears in search within ~10 min of receipt.

---

## 7. Authentication — Outlook (admin consent, app-only)

**Azure AD app registration (one-time, done by customer's M365 admin):**

1. Register a new app in Entra ID → Azure AD → App registrations.
2. Add **application permission**: `Microsoft Graph → Mail.Read` (and `User.Read.All` for mailbox discovery).
3. Click **"Grant admin consent for <tenant>"**. This is the bit that makes it org-wide.
4. Create a client secret. Copy the value (shown once).
5. Paste `tenant_id`, `client_id`, `client_secret` into OptiFlow's setup UI.

**Token flow (automatic, every ~1 hour):**

```python
import msal

app = msal.ConfidentialClientApplication(
    client_id=CLIENT_ID,
    client_credential=CLIENT_SECRET,
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
)
result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
access_token = result["access_token"]   # good for ~1h
```

No refresh token, no redirect URI, no user interaction. MSAL caches tokens in-memory; we re-acquire on expiry.

**Credential storage.** JSON at `data/config/email/outlook.json`:

```json
{
  "tenant_id":     "12345678-aaaa-bbbb-cccc-...",
  "client_id":     "abcdef01-...",
  "client_secret": "gAAAAABk...",        // Fernet-encrypted
  "added_at":      1714800000,
  "added_by":      "admin@company.com"
}
```

File permission `0600`. Key (Fernet) is the same `data/config/.secret` used for AI API keys.

**Minimum scopes we request.** `Mail.Read` (app), `User.Read.All` (app), and optionally `Mail.ReadBasic.All` as a fallback. Nothing else. Admin can review the exact list during consent.

---

## 8. Agent tools

Registered into `ToolRegistry` at startup when email is configured.

### 8.1 `list_mailboxes`

```json
{
  "name": "list_mailboxes",
  "description": "List the mailboxes OptiFlow has indexed. Use when the user asks who's included or what email accounts are available.",
  "input_schema": { "type": "object", "properties": {}, "additionalProperties": false }
}
```

Returns `[{account_email, display_name, status, last_sync_at, message_count}]` for every active mailbox in the tenant.

### 8.2 `search_emails`

```json
{
  "name": "search_emails",
  "description": "Search all indexed company email for messages matching keywords, optionally filtered by mailbox, sender, date, folder, or attachment presence. Returns a BM25-ranked list. IMPORTANT: generate 2-6 keyword variants including synonyms and abbreviations. Quote IDs/invoice numbers exactly. Do NOT invent message ids.",
  "input_schema": {
    "type": "object",
    "properties": {
      "keywords":         { "type": "array", "items": { "type": "string" }, "minItems": 1 },
      "mailbox":          { "type": "string", "description": "Limit to a specific mailbox address. Optional." },
      "sender":           { "type": "string" },
      "recipient":        { "type": "string" },
      "date_range":       { "type": "string", "description": "'last_7_days' | 'last_30_days' | 'YYYY-MM-DD..YYYY-MM-DD'" },
      "folder":           { "type": "string" },
      "has_attachments":  { "type": "boolean" },
      "limit":            { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 }
    },
    "required": ["keywords"],
    "additionalProperties": false
  }
}
```

**SQL shape:**

```sql
SELECT e.id, e.mailbox_id, e.account_email, e.subject, e.from_name, e.from_email,
       e.sent_at, e.has_attachments, e.folder, e.conversation_id,
       snippet(emails_fts, 4, '<mark>', '</mark>', '…', 12) AS preview,
       bm25(emails_fts) AS score
FROM   emails_fts
JOIN   emails e ON e.id = emails_fts.rowid
WHERE  emails_fts MATCH :fts_query
  AND  (:mailbox    IS NULL OR e.account_email = :mailbox)
  AND  (:sender     IS NULL OR e.from_email LIKE :sender OR e.from_name LIKE :sender)
  AND  (:date_from  IS NULL OR e.sent_at >= :date_from)
  AND  (:date_to    IS NULL OR e.sent_at <  :date_to)
  AND  (:folder     IS NULL OR e.folder = :folder)
  AND  (:has_attach IS NULL OR e.has_attachments = :has_attach)
ORDER BY bm25(emails_fts)
LIMIT :limit;
```

`fts_query` is built server-side by quoting each keyword phrase and joining with ` OR `. Raw FTS5 syntax is never passed through from the model.

### 8.3 `get_email`

Fetch the full row for a single email by its internal id. Returns everything except HTML body.

### 8.4 `get_email_thread`

Fetch all messages in the same `conversation_id`, oldest to newest, scoped to the same mailbox.

---

## 9. System-prompt additions

```
## Email source: Contoso Corp (Outlook, admin-consented, N mailboxes indexed)
You can read company email via search_emails, get_email, get_email_thread, list_mailboxes.

When searching email:
  - Generate 2-6 keyword variants, not just the literal user phrase.
  - Use mailbox= to scope when the user names a specific person or role inbox.
  - Use sender= when the user names an external party.
  - Translate temporal words to date_range: last_7_days, last_30_days, YYYY-MM-DD..YYYY-MM-DD.
  - Quote invoice numbers, PO numbers, and IDs exactly. Do NOT normalize.

Do NOT fabricate email content. If search returns nothing, say so plainly and
suggest alternative searches. Summarize findings with concrete details (sender,
subject, date) so the user can identify the message.
```

---

## 10. Setup wizard integration

New "Email (Microsoft 365)" card in the Sources wizard.

- **Step 1 — "Before you start":** a short checklist for the admin, with a link to the official Microsoft docs. Tells them exactly what app permissions to grant and how to create a secret.
- **Step 2 — "Enter credentials":** three fields — Tenant ID, Client ID, Client Secret. Plus a display-name override ("Contoso Corp").
- **Step 3 — "Test & connect":** backend validates (acquires a token + lists 1 user). On success, saves encrypted credentials and triggers mailbox discovery.
- **Step 4 — "Indexing":** shows live progress: `Discovered 142 mailboxes · Indexed 38 · 2,914 / ~~~ messages`. Polls `/setup/email/status` every 2s.

A "Disconnect email" action wipes `data/config/email/outlook.json` AND optionally drops every row from `email.db` (confirm modal).

---

## 11. Security model

| Concern | Mitigation |
| --- | --- |
| Client secret exfiltration | Fernet-encrypted at rest, file perm `0600`. Secret never logged — logs redact `client_secret` / `Authorization` / `access_token`. |
| Over-scoped permissions | Only `Mail.Read` + `User.Read.All` (app) requested. Admin reviews exact scope list during consent. |
| Secret rotation | Admin rotates in Azure; updates secret in OptiFlow UI. Old secret stays valid until Azure revokes it, so rotation is non-disruptive. |
| Prompt injection via email body | Tool output flows as tool-result, not user input. Agent still cannot send mail (no `Mail.Send` permission). Worst case: the model is lied to about facts — a content-trust issue, not a privilege escalation. |
| Cross-tenant data leakage | There's only ever one tenant configured at a time in a given OptiFlow install. No concept of cross-tenant queries; enforced structurally. |
| Graph throttling | Global `Semaphore(6)`. `Retry-After` honored. Backfill runs at 1 page/min. |
| Disabled / off-boarded mailboxes | Discovery updates `status` to `disabled`. Delta sync skips disabled mailboxes. Existing indexed messages are retained (compliance) until admin requests a purge. |
| Attachment malware | We never open attachments. Filename + MIME type + size only. |
| Logging secrets | Global log filter strips `Authorization:` headers and keys matching `/^client_secret$/i`. Debug-mode token dumps are gated behind `OPTIFLOW_DEBUG_AUTH=1`. |
| Compliance / legal hold | Admin can purge a mailbox from the cache via the setup UI; Graph source of truth is untouched. OptiFlow is a secondary index, not a record of truth. |

---

## 12. Observability

Per-mailbox rows in `sync_state` + aggregate endpoint `/setup/email/status` returning:

```json
{
  "configured":        true,
  "tenant_id":         "12345678-...",
  "mailboxes_total":   142,
  "mailboxes_active":  138,
  "mailboxes_with_errors": 2,
  "initial_synced":    136,
  "messages_total":    2_914_402,
  "last_sync_at":      1714799820,
  "errors":            [{ "mailbox": "old@company.com", "error": "ResourceNotFound" }]
}
```

Structured logs per sync tick:

```
[Outlook] discover tenant=contoso +3 new -1 disabled  142 total
[Outlook] delta alice@contoso.com +12 new +3 updated -0 deleted  842ms
[Outlook] backfill bob@contoso.com page 7/?  +1000 messages  2.1s
```

---

## 13. Testing strategy

- **Unit.** `mapper.py` against recorded Graph fixtures. FTS query builder against edge cases.
- **Integration.** `FakeGraphClient` that serves canned pages from disk. Drives full discovery → ingest → FTS → search loop without hitting Microsoft.
- **E2E (manual).** A dev Microsoft 365 tenant with ~5 seeded mailboxes. Ground-truth Q&A file of 20 questions asserting top-ranked hit ids.
- **Load.** Synthetic generator with 200 mailboxes × 5k messages each. Targets: <100ms p95 search, ≥500 msgs/sec ingest, full tenant initial sync in <30 min on a modest box.

---

## 14. Rollout phases

| Phase | Scope | Estimate |
| --- | --- | --- |
| **MVP (this doc + scaffold landed)** | Outlook admin-consent. FTS5-only. 30-day eager + delta + backfill. All mailboxes in tenant. Read-only. | 1 sprint |
| **Phase 2** | Attachment text extraction (PDF/DOCX/XLSX → `body_text`). | 3–5 days |
| **Phase 3** | Gmail (Google Workspace) admin-consent adapter, same schema/tools. | 1 sprint |
| **Phase 4** | Optional `fastembed` embeddings layered behind a feature flag. Hybrid BM25 × cosine. **Only if recall shows real gaps.** | 1–2 days |
| **Phase 5** | Role-based redaction (who-can-see-which-mailbox within the org). Webhook push sync. OCR. | unscoped |

---

## 15. Open questions

1. **Backfill horizon.** Default cap 1 year. Operator override via `OPTIFLOW_EMAIL_BACKFILL_DAYS`. OK?
2. **Shared mailboxes & resource mailboxes.** Include by default, or require explicit opt-in? Default proposal: include (they're often the most valuable — `support@`, `orders@`).
3. **Per-OptiFlow-user access policy.** MVP: every OptiFlow user can query every mailbox the admin consented to. Phase 5 adds per-user redaction. Confirm this is acceptable.
4. **Retention.** Email cache rows are NOT auto-expired (unlike sessions). Admin triggers purge explicitly. OK?
5. **Legal discoverability.** The cache may be subject to discovery separately from Microsoft. Make the cache path a single directory so ops can snapshot/wipe easily. Done.

---

## 16. Explicitly not prescribed here

- **LLM keyword-expansion prompt wording.** Tuned in `app/agent/prompts.py` once real queries come in.
- **UI for email results.** Reuse existing chat answer rendering — agent writes a text summary citing subject/date/sender.
- **Cross-source joins.** Possible via agent composing multiple tool calls; no special machinery.

---

## 17. Definition of done (MVP)

- [ ] Admin-consent app-only auth with MSAL; client secret Fernet-encrypted.
- [ ] Mailbox discovery enumerates tenant on connect + periodically.
- [ ] Per-mailbox initial + delta + backfill ingestion, resilient to 429/401/5xx.
- [ ] `email.db` isolated from `sessions.db`; FTS5 triggers keep index in sync.
- [ ] `list_mailboxes`, `search_emails`, `get_email`, `get_email_thread` registered.
- [ ] Setup wizard card: credentials → test → indexing progress → disconnect.
- [ ] Log filter strips `Authorization:` / `client_secret` from all output.
- [ ] Integration tests green against `FakeGraphClient`.
- [ ] `DOCUMENTATION.md` section "Email search" written for operators.

No per-user OAuth. No embeddings. No vectors. Fast, exact, debuggable company-wide email search.
