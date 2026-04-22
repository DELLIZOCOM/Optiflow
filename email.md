# Email Integration — Architecture Plan

**Status:** design, MVP scope
**Author context:** continues OptiFlow AI (DB-agent over MSSQL/PostgreSQL/MySQL) with a new email source
**Goal:** let the agent answer questions that require reading the user's company email, company-agnostic, starting with **Outlook (Microsoft 365)** and adding Gmail later.

---

## 1. Design stance: FTS-only for MVP, embeddings optional later

We deliberately ship **without vector embeddings** in the first cut. The reasoning:

| Concern | Why FTS wins for email |
| --- | --- |
| **Exact entity recall** | Emails are dense with IDs, names, PO numbers, invoice numbers, project codes. BM25 on tokenized text matches `#8892` exactly; cosine similarity on dense vectors will happily return `#8891` as "close enough". |
| **Debuggability** | An FTS query is a string. You can paste it into `sqlite3` and see what matched and why. Vector retrieval is a black box the first time it returns the wrong thread. |
| **Cost & latency** | No embedding compute on ingest, no vector index to rebuild, no GPU path, no Voyage API bill. Search is a single `SELECT … MATCH … ORDER BY bm25(...)` against an SQLite table. |
| **Operational footprint** | Zero new services. `sqlite3` ships with Python. The whole email cache is one file on disk. |
| **Agent synergy** | Modern LLMs are *already* good at rewriting a natural-language question into keyword variants. We push synonym expansion to the model, which is where it belongs. The retrieval layer stays dumb-but-fast. |

**Embeddings are not cancelled — they're deferred.** The schema reserves a nullable `embedding BLOB` column and the ingestion pipeline has a post-insert hook. If we observe real recall failures in production (the agent missing emails that a human would have found) we layer in `fastembed` (local, free, ~30MB model) and switch `search_emails` to hybrid (BM25 score × cosine rank fusion). That's a 1-day change, not a re-architecture. We do **not** build it speculatively.

---

## 2. Non-goals (MVP)

Being explicit about what this cut does *not* do, so scope stays honest:

- **No sending.** Agent is strictly read-only. `Mail.Read` scope only.
- **No attachment text extraction.** We index attachment filenames + MIME types + sizes so the agent can say "this email has a PDF called `Q3_forecast.pdf`", but the body of the PDF is not searchable. PDF/DOCX/XLSX extraction is Phase 2.
- **No OCR** on image attachments. Ever, probably.
- **No embeddings.** See §1.
- **No inline calendar / meeting parsing.** Treat `.ics` attachments as opaque.
- **No Gmail.** Phase 3. Adapter-shaped so it drops in later without changing tools.
- **No webhooks.** Poll via Graph `$delta`. Webhooks require a public HTTPS endpoint and signed payload verification — too much ops for MVP.
- **No admin / tenant-wide consent.** Per-user delegated OAuth only. Admin-consent for larger orgs is Phase 3.
- **No rule-based auto-summarization.** Agent summarizes on demand, not eagerly.

---

## 3. Architecture overview

```
┌────────────┐   OAuth    ┌───────────────┐   Graph API    ┌─────────────────┐
│  Browser   │──────────▶│   Setup UI    │──────────────▶│  Microsoft 365  │
│ (setup pg) │            │ /setup/email  │                │     (user       │
└────────────┘            └───────┬───────┘                │    mailbox)     │
                                  │ tokens                 └────────┬────────┘
                                  ▼                                 │
                          ┌─────────────────┐    delta pull         │
                          │ OutlookSource   │◀──────────────────────┘
                          │ (DataSource)    │
                          └────────┬────────┘
                                   │ insert/update
                                   ▼
                          ┌─────────────────┐
                          │ email.db        │   SQLite + FTS5
                          │  emails         │   (BM25, porter tokenizer)
                          │  emails_fts     │
                          │  mailboxes      │
                          │  sync_state     │
                          └────────┬────────┘
                                   │
                                   ▼
                          ┌─────────────────┐
                          │ search_emails   │   Agent tools
                          │ get_email       │   (registered at
                          │ list_mailboxes  │    startup)
                          │ get_thread      │
                          └────────┬────────┘
                                   │
                                   ▼
                          ┌─────────────────┐
                          │ Orchestrator    │   Existing ReAct loop;
                          │ (unchanged)     │   email tools appear
                          └─────────────────┘   alongside DB tools
```

The agent loop does not change. Email tools register into the existing `ToolRegistry` and the `OutlookSource` registers into `SourceRegistry` alongside database sources. The agent discovers both kinds of sources the same way.

---

## 4. Where code lives

```
app/
├── sources/
│   └── email/
│       ├── __init__.py                # already a stub
│       ├── base.py                    # EmailSource protocol (extends DataSource)
│       ├── store.py                   # SQLite + FTS5 wrapper (EmailStore)
│       ├── outlook/
│       │   ├── __init__.py
│       │   ├── source.py              # OutlookSource(DataSource)
│       │   ├── auth.py                # MSAL token acquire/refresh
│       │   ├── ingest.py              # initial sync + delta + backfill
│       │   └── mapper.py              # Graph JSON -> row dict
│       └── gmail/                     # Phase 3 (empty for now)
├── tools/
│   └── email.py                       # search_emails, get_email, list_mailboxes, get_thread
├── routes/
│   └── email.py                       # OAuth callback, setup helpers, sync status
└── config.py                          # EMAIL_DB_PATH, OUTLOOK_CLIENT_ID, scopes
data/
├── cache/
│   ├── sessions.db                    # existing
│   └── email.db                       # NEW - FTS-backed mail cache
└── config/
    └── email/
        └── outlook/
            └── <user_id>.json         # Fernet-encrypted OAuth tokens
```

**Why a separate `email.db`, not inside `sessions.db`:** sessions are short-lived (30d TTL, bounded to 1000 entries), email is long-lived and can be large (50k+ rows). Different retention, different vacuum strategies, different backup priorities. Mixing them invites accidental wipes.

---

## 5. Data model

### 5.1 `emails` — the canonical table

```sql
CREATE TABLE emails (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT    NOT NULL,           -- which OptiFlow user owns this mailbox
    account_email      TEXT    NOT NULL,           -- the mailbox address (e.g. alice@company.com)

    -- Identity (provider-specific)
    provider           TEXT    NOT NULL,           -- 'outlook' | 'gmail' (future)
    provider_msg_id    TEXT    NOT NULL,           -- Graph id or Gmail msg id
    internet_msg_id    TEXT,                       -- RFC 5322 Message-ID (dedupe across providers)
    conversation_id    TEXT,                       -- thread id

    -- Headers
    subject            TEXT,
    from_name          TEXT,
    from_email         TEXT,
    to_emails          TEXT,                       -- JSON array of strings
    cc_emails          TEXT,
    bcc_emails         TEXT,

    -- Body
    body_text          TEXT,                       -- plain-text rendering (for FTS)
    body_html_hash     TEXT,                       -- sha256 of HTML body; full HTML fetched on demand
    has_attachments    INTEGER NOT NULL DEFAULT 0, -- boolean
    attachment_names   TEXT,                       -- JSON array of filenames

    -- Folder / state
    folder             TEXT,                       -- 'inbox' | 'sent' | 'archive' | '<custom>'
    is_read            INTEGER NOT NULL DEFAULT 0,
    importance         TEXT,                       -- 'low' | 'normal' | 'high'

    -- Timestamps (all stored as UTC unix epoch, seconds)
    sent_at            REAL NOT NULL,
    received_at        REAL NOT NULL,
    ingested_at        REAL NOT NULL,

    -- Hook for future embeddings layer
    embedding          BLOB,                       -- NULL until fastembed phase
    embedding_model    TEXT,                       -- model id; NULL for now

    UNIQUE(user_id, provider, provider_msg_id)
);

CREATE INDEX idx_emails_user_received ON emails(user_id, received_at DESC);
CREATE INDEX idx_emails_conversation  ON emails(user_id, conversation_id);
CREATE INDEX idx_emails_from          ON emails(user_id, from_email);
CREATE INDEX idx_emails_internet_id   ON emails(internet_msg_id);
```

### 5.2 `emails_fts` — the FTS5 virtual table

```sql
CREATE VIRTUAL TABLE emails_fts USING fts5(
    subject,
    from_name,
    from_email,
    to_emails,
    body_text,
    attachment_names,
    content='emails',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
```

`content='emails'` makes it a contentless FTS5 table — it stores only the inverted index and pulls the matching columns from `emails` on read. Saves ~30% disk.

Triggers keep FTS in sync:

```sql
CREATE TRIGGER emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES (new.id, new.subject, new.from_name, new.from_email, new.to_emails, new.body_text, new.attachment_names);
END;

CREATE TRIGGER emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES ('delete', old.id, old.subject, old.from_name, old.from_email, old.to_emails, old.body_text, old.attachment_names);
END;

CREATE TRIGGER emails_au AFTER UPDATE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES ('delete', old.id, old.subject, old.from_name, old.from_email, old.to_emails, old.body_text, old.attachment_names);
    INSERT INTO emails_fts(rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES (new.id, new.subject, new.from_name, new.from_email, new.to_emails, new.body_text, new.attachment_names);
END;
```

### 5.3 `mailboxes` and `sync_state`

```sql
CREATE TABLE mailboxes (
    user_id         TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    provider        TEXT NOT NULL,
    display_name    TEXT,
    connected_at    REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',    -- 'active' | 'reauth_required' | 'revoked'
    PRIMARY KEY (user_id, account_email)
);

CREATE TABLE sync_state (
    user_id         TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    delta_link      TEXT,                               -- Graph $deltaLink for incremental sync
    initial_synced  INTEGER NOT NULL DEFAULT 0,         -- boolean; set when 30-day window landed
    backfill_cursor TEXT,                               -- page token for older-than-30d backfill
    backfill_done   INTEGER NOT NULL DEFAULT 0,
    last_sync_at    REAL,
    last_error      TEXT,
    PRIMARY KEY (user_id, account_email)
);
```

---

## 6. Ingestion pipeline

Three phases, all background:

1. **Initial sync (eager, on connect).** On successful OAuth, pull the last **30 days** of inbox + sent items. This is the "hot window" — covers most questions. Page through Graph with `$top=100&$orderby=receivedDateTime desc`. Store `delta_link` from the final response.
2. **Delta sync (recurring).** Every **10 minutes** per connected mailbox, call the saved `delta_link`. Inserts new mail, updates modified mail, deletes removed mail. Cheap because Graph only returns what changed.
3. **Backfill (background, low priority).** After initial sync completes, a separate task walks backward from "30 days ago" in 1000-message pages until the mailbox is exhausted or `backfill_done=1`. Runs at lower rate (1 page per minute) so it doesn't starve delta sync or hit throttling limits.

All three write to the same `emails` table with `INSERT OR REPLACE` keyed by `(user_id, provider, provider_msg_id)` so retries are idempotent.

**Concurrency.** One ingestion task per mailbox (per `(user_id, account_email)`). An `asyncio.Lock` in `OutlookSource` prevents overlap between the delta cron and the backfill cron on the same mailbox. Cross-mailbox runs in parallel.

**Failure handling.**
- 429 (throttle) → respect `Retry-After`, exponential backoff.
- 401 (token expired) → refresh via MSAL; if refresh fails, set `status='reauth_required'` and surface in UI.
- 5xx → retry 3x, then log and wait for next tick.
- Network flake → same as 5xx.

**Ingestion freshness SLO.** New mail appears in search within ~10 min of receipt. Documented, not promised in the UI.

---

## 7. Authentication — Outlook (MSAL)

- **App type:** multi-tenant delegated OAuth. User signs in with their personal or work Microsoft account.
- **Scopes:** `offline_access Mail.Read User.Read` — strictly read-only. No `Mail.Send`, no `Mail.ReadWrite`.
- **Flow:** authorization-code with PKCE. Backend hosts `/setup/email/outlook/callback` which exchanges the code for an access/refresh token.
- **Token storage:** reuse the existing `app/utils/crypto.py` Fernet helper. Store per-user JSON at `data/config/email/outlook/<user_id>.json`:
  ```json
  {
    "account_email": "alice@company.com",
    "access_token":  "gAAAAABk...",
    "refresh_token": "gAAAAABk...",
    "expires_at":    1714800000,
    "home_account_id": "abc.def"
  }
  ```
  Both tokens encrypted. The file itself is `chmod 600`.
- **Refresh:** MSAL `acquire_token_silent` handles refresh transparently. We only re-prompt the user when the refresh token is revoked.
- **Registration:** OptiFlow app registered in Azure AD with redirect URI `http://localhost:<port>/setup/email/outlook/callback` for local installs; for hosted deployments, the operator configures their own app ID via `OUTLOOK_CLIENT_ID` in config.

**Per-user vs admin consent.** MVP is per-user (delegated). Admin consent for tenant-wide access is Phase 3 and changes the scope model but not the ingestion or search code.

---

## 8. Agent tools

Registered into the existing `ToolRegistry` at startup. The agent sees them alongside `execute_sql`, `list_tables`, etc.

### 8.1 `list_mailboxes`

```json
{
  "name": "list_mailboxes",
  "description": "List the email accounts the user has connected. Use this when the user asks about what email is available.",
  "input_schema": { "type": "object", "properties": {}, "additionalProperties": false }
}
```

Returns `[{account_email, provider, folder_counts, last_sync_at, status}]` for the current user. Small result, included in system context on every turn so the agent doesn't need to call it unless the user asks explicitly.

### 8.2 `search_emails`

The workhorse. BM25-ranked FTS with structured filters.

```json
{
  "name": "search_emails",
  "description": "Search the user's email for messages matching keywords, optionally filtered by sender, date, folder, or attachment presence. Returns a ranked list of matches. IMPORTANT: think about keyword variants — if the user asks about 'Q3 forecast', try ['Q3 forecast', 'quarterly forecast', 'Q3 revenue']. Use sender= to narrow when the user names a person. Use date_range= for temporal questions. Do NOT invent message ids.",
  "input_schema": {
    "type": "object",
    "properties": {
      "keywords":         { "type": "array", "items": { "type": "string" }, "minItems": 1, "description": "Up to 6 keyword phrases OR'd together. Combine synonyms and abbreviations." },
      "sender":           { "type": "string", "description": "Email address or display-name substring. Optional." },
      "recipient":        { "type": "string", "description": "Email address or display-name substring (to/cc/bcc). Optional." },
      "date_range":       { "type": "string", "description": "e.g. 'last_7_days', 'last_30_days', '2026-01-01..2026-03-31'. Optional." },
      "folder":           { "type": "string", "description": "'inbox' | 'sent' | 'archive' | custom folder name. Optional." },
      "has_attachments":  { "type": "boolean", "description": "Filter to messages with/without attachments. Optional." },
      "limit":            { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 }
    },
    "required": ["keywords"],
    "additionalProperties": false
  }
}
```

**SQL shape:**

```sql
SELECT e.id, e.subject, e.from_name, e.from_email, e.sent_at,
       e.has_attachments, e.folder, e.conversation_id,
       snippet(emails_fts, 4, '<mark>', '</mark>', '…', 12) AS preview,
       bm25(emails_fts) AS score
FROM   emails_fts
JOIN   emails e ON e.id = emails_fts.rowid
WHERE  emails_fts MATCH :fts_query
  AND  e.user_id = :user_id
  AND  (:sender     IS NULL OR e.from_email LIKE :sender OR e.from_name LIKE :sender)
  AND  (:date_from  IS NULL OR e.sent_at >= :date_from)
  AND  (:date_to    IS NULL OR e.sent_at <  :date_to)
  AND  (:folder     IS NULL OR e.folder = :folder)
  AND  (:has_attach IS NULL OR e.has_attachments = :has_attach)
ORDER BY bm25(emails_fts)
LIMIT :limit;
```

`fts_query` is built from the `keywords` array with `OR` between phrases, quoted to preserve multi-word terms: `"Q3 forecast" OR "quarterly forecast" OR "Q3 revenue"`. Raw FTS5 syntax is **never** passed through from the model — we tokenize and quote ourselves.

**`user_id` is always injected** by the tool adapter from session context. The model cannot set or override it. This is the tenant-isolation boundary.

### 8.3 `get_email`

```json
{
  "name": "get_email",
  "description": "Fetch the full text and metadata of a single email by its id. Use this after search_emails when you need the complete body to answer.",
  "input_schema": {
    "type": "object",
    "properties": { "email_id": { "type": "integer" } },
    "required": ["email_id"],
    "additionalProperties": false
  }
}
```

Returns the full row, including `body_text`. Body HTML is not returned — agent reasons over plain text only.

### 8.4 `get_email_thread`

```json
{
  "name": "get_email_thread",
  "description": "Fetch all messages in the same conversation/thread as a given email, ordered oldest to newest.",
  "input_schema": {
    "type": "object",
    "properties": { "conversation_id": { "type": "string" } },
    "required": ["conversation_id"],
    "additionalProperties": false
  }
}
```

Common pattern: `search_emails` → pick top hit → `get_email_thread` to see the full back-and-forth.

---

## 9. System-prompt additions

A new section added by `EmailSource.get_system_prompt_section()`:

```
## Email source: alice@company.com (Outlook)
You can read Alice's email via search_emails, get_email, and get_email_thread.

When searching email:
  - Generate 2-6 keyword variants, not just the literal user phrase. Include synonyms,
    abbreviations, and common misspellings (e.g. "PO" vs "purchase order").
  - Use sender= when the user names a person. Match display name OR email.
  - Use date_range= when the user uses temporal words ("last week", "since Monday",
    "in March"). Translate to the standard forms: last_7_days, last_30_days, or
    YYYY-MM-DD..YYYY-MM-DD.
  - Quote invoice numbers, IDs, and codes exactly in keywords — do not normalize.

Do NOT fabricate email content. If search returns nothing, say so plainly and
offer alternative searches.

The user cannot see raw search output — summarize findings with concrete details
(sender, subject, date) so they can identify the message.
```

---

## 10. Setup wizard integration

Add an "Email" tab to the existing Sources setup UI.

- Step 1: "Connect your email" — button launches Microsoft OAuth popup.
- Step 2: callback hits `/setup/email/outlook/callback`, exchanges code, stores encrypted tokens, kicks off initial sync as a background task.
- Step 3: UI polls `/setup/email/status?account=<email>` every 2s and shows: `Syncing last 30 days… 1,247 / ~5,000 messages`.
- Step 4: when initial sync completes, UI flips to "Connected. Background sync every 10 min." and user can close the wizard.
- Backfill continues silently. A small badge in the sidebar shows "Backfilling older mail" until `backfill_done=1`.

A "Disconnect" button in the sources panel revokes the token (best-effort) and deletes `data/config/email/outlook/<user_id>.json`. The user can also choose "Delete all cached mail" which drops their rows from `email.db`.

---

## 11. Security model

| Concern | Mitigation |
| --- | --- |
| Token exfiltration | Tokens Fernet-encrypted at rest. File permissions `0600`. The encryption key (`data/config/.secret`) is local to the install. |
| Prompt injection via email body | The model sees email content as tool output, not as user input. It still can't issue writes — no `Mail.Send` scope exists in our OAuth grant. Worst case is the model getting lied to about facts, which is a content-trust problem, not a privilege-escalation one. |
| Cross-user data leakage | `user_id` injected by the tool layer, not the model. All queries have a mandatory `WHERE user_id = ?`. Code reviews enforce this. |
| Over-scoped permissions | Only `Mail.Read` + `offline_access` + `User.Read` requested. Documented in the consent screen. |
| Graph throttling | Exponential backoff + `Retry-After` respected. Backfill cron runs at 1 page/minute. |
| Refresh token revocation | MSAL detects, we surface `status='reauth_required'`, UI prompts re-auth. No silent failures. |
| Stale tokens at shutdown | Refresh tokens persist; access tokens are re-acquired on next start. No long-lived access tokens kept in memory. |
| Attachment malware | We never open attachments. Filename only. |
| Logging secrets | Request/response logs for Graph strip `Authorization:` headers; token dumps are gated behind a debug-only flag. |

---

## 12. Observability

Per-mailbox metrics written into `sync_state` and surfaced on `/setup/email/status`:

- `last_sync_at`
- `initial_synced` + `backfill_done`
- `message_count` (SELECT COUNT(*) FROM emails WHERE user_id=? AND account_email=?)
- `last_error` (cleared on next success)

Structured logs for each sync tick:

```
[OutlookSource] delta alice@company.com  +12 new  +3 updated  -0 deleted  in 842ms
[OutlookSource] backfill alice@company.com  page 7/?  +1000 messages  in 2.1s
```

Agent tool calls already logged via the existing `[Agent]` path.

---

## 13. Testing strategy

- **Unit.** `mapper.py` (Graph JSON → row) tested against recorded fixtures. FTS query builder tested with keyword edge cases (quotes, apostrophes, non-ASCII, empty strings).
- **Integration.** A `FakeOutlookClient` that serves canned Graph pages from disk. Drives the full ingest → FTS → search loop without hitting Microsoft.
- **E2E (manual).** A "recorded mailbox" in a dev Microsoft account with ~500 known emails. We have a test script that asks 20 questions and asserts the top-ranked hit matches a ground-truth file.
- **Load.** Synthetic mailbox generator with 50k rows. Measures ingest throughput and p95 search latency. Target: <100ms p95 for search, ingest at ≥500 msgs/sec.

---

## 14. Rollout phases

| Phase | Scope | Duration estimate |
| --- | --- | --- |
| **MVP (this doc)** | Outlook only. FTS5-only search. 30-day eager + delta + backfill. Read-only. Per-user OAuth. Metadata-only attachments. | 1 sprint |
| **Phase 2** | Attachment text extraction (PDF/DOCX/XLSX → `body_text` append). Still no embeddings. | 3–5 days |
| **Phase 3** | Gmail adapter reusing the same schema and tools. Admin-consent for Outlook. | 1 sprint |
| **Phase 4 (conditional)** | Local `fastembed` embeddings layered behind a feature flag. Hybrid search = BM25 × cosine with reciprocal-rank fusion. **Only built if we observe recall problems in real use.** | 1–2 days |
| **Phase 5 (speculative)** | Webhook-based push sync (replaces delta polling) for sub-minute freshness. OCR on image attachments. Calendar integration. | unscoped |

---

## 15. Open questions (to resolve before code)

These don't block the plan but should be agreed:

1. **Multi-tenant story for hosted OptiFlow.** Do we expect to run one OptiFlow server per company (current model, each company registers their own Outlook app), or one shared server with per-user mailboxes across companies? Affects how `OUTLOOK_CLIENT_ID` is configured — env var (one-per-deploy) or DB column (per-tenant).
2. **Backfill limit.** Do we backfill forever, or cap at (say) 2 years? Older mail is rarely queried and uses disk. Default proposal: backfill 1 year, then stop. Operator override via config.
3. **Multiple mailboxes per user.** Does one OptiFlow user connect multiple Outlook accounts (personal + work)? Schema supports it (`(user_id, account_email)` composite key), tool returns all by default. Confirming this is desired.
4. **Retention.** Do we auto-expire email cache rows the way we do sessions (30d)? Proposal: **no** — email is reference data, not session data. Deletion is user-initiated via "Disconnect + wipe cache".
5. **Scope upgrade path.** If we later add "draft a reply" features, we need `Mail.ReadWrite` or `Mail.Send`. Re-consent is required. Treat that as a separate product decision, not creep.

---

## 16. What this plan deliberately does not prescribe

- **LLM keyword-expansion prompt wording.** Handled in `app/agent/prompts.py` once real queries start showing up. Tuning this is cheap and iterative.
- **UI for showing email results in chat.** Reuse the existing answer-rendering path — the agent writes a text summary with subject/date/sender cited, same as it cites query results today. No new card type until we see friction.
- **Cross-source joins.** "Show me invoices from the DB that match emails mentioning Q3." Possible in principle (agent can call both tools in sequence) but no special machinery for it in MVP.

---

## 17. Definition of done (MVP)

- [ ] `OutlookSource` implements `DataSource`; registered into `SourceRegistry` at startup.
- [ ] `search_emails`, `get_email`, `list_mailboxes`, `get_email_thread` registered into `ToolRegistry`.
- [ ] Setup wizard "Email" tab: connect, initial-sync progress, disconnect.
- [ ] Delta sync every 10 minutes, backfill in background, both resilient to 429 / token expiry.
- [ ] `email.db` isolated from `sessions.db`. FTS5 triggers keep index in sync.
- [ ] Tool calls enforce `user_id` at the adapter layer; agent cannot leak across users.
- [ ] Fernet-encrypted per-user token files with `0600` perms.
- [ ] Integration tests green against `FakeOutlookClient`.
- [ ] One page in `DOCUMENTATION.md` covering "How email search works" for operators.

No embeddings. No vectors. No promises about semantic similarity. Just fast, exact, debuggable email search that composes with the rest of the agent.
