# OptiFlow AI — Internal Hosting Plan (CEO Pilot)

Target: single-host deployment on the internal office network for the CEO and a small circle of testers. Low-ops, reversible, no public exposure.

---

## 1. Scope of this deployment

| Dimension | Decision |
|-----------|----------|
| Users | 1–5 (CEO + internal testers) |
| Network | Office LAN only (no public internet) |
| Authentication | Browser Basic-Auth at the reverse proxy |
| TLS | Self-signed cert on the LAN hostname (Caddy handles this automatically) |
| Data residency | DB stays inside the company; only the AI API call leaves the network |
| Backups | Nightly tarball of `data/` to the office NAS |
| Uptime target | Business hours; unattended restart is fine |

**Out of scope for v1:** multi-user auth, per-user chat history, audit log, mobile app, public hosting.

---

## 2. Host requirements

### Option A — Dedicated Mac mini on the office LAN (recommended)

- Apple silicon, 16 GB RAM, 256 GB SSD is plenty
- Connected to the office router over Ethernet (not Wi-Fi)
- Static LAN IP or DHCP reservation from the router
- macOS user with admin rights for initial install only; OptiFlow runs under a normal user
- Wakes on AC power; "Prevent computer from sleeping" enabled in Energy settings

### Option B — Small Linux VM (Ubuntu 22.04 LTS)

- 2 vCPU / 4 GB RAM / 20 GB disk
- Can run on an existing office Hyper-V / Proxmox / VMware host
- Same Ethernet + static IP requirements

Either option is fine. The Mac mini is simpler if there isn't already an internal VM platform.

---

## 3. Prerequisites to install on the host

| Component | Why | How |
|-----------|-----|-----|
| Python 3.11+ | Runs the backend | `brew install python@3.12` / `apt-get install python3.12 python3.12-venv` |
| Microsoft ODBC Driver 18 | MSSQL connector | See [DOCUMENTATION.md §17](DOCUMENTATION.md) for the exact commands |
| Git | Pull/update code | `brew install git` / `apt-get install git` |
| Caddy 2 | Reverse proxy + TLS + Basic Auth | `brew install caddy` / `apt-get install caddy` |

Nothing else is required — no Docker, no Node, no database server (we connect to the existing internal MSSQL).

---

## 4. One-time install

```bash
# 1. Clone and set up the app
cd /opt   # or ~/Applications on Mac
git clone <internal-repo-url> optiflow-ai
cd optiflow-ai
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Verify startup (Ctrl+C when you see "OptiFlow AI ready")
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. Run the setup wizard once from a browser on the same machine
#    open http://127.0.0.1:8000
#    → pick AI provider, enter Anthropic API key
#    → connect MSSQL (server, database, username, password)
#    → run schema discovery
#    → confirm the auto-generated company.md and save
```

The wizard creates `data/config/*.json`, `data/sources/<name>/`, and `data/knowledge/company.md`. Everything after setup is a read from those files plus the live DB.

---

## 5. Run as a service

The app should auto-start on boot and restart on crash. Choose one:

### macOS — launchd

Create `~/Library/LaunchAgents/com.optiflow.ai.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>com.optiflow.ai</string>
  <key>WorkingDirectory</key> <string>/Users/SHARED/optiflow-ai</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/SHARED/optiflow-ai/.venv/bin/uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>RunAtLoad</key>    <true/>
  <key>KeepAlive</key>    <true/>
  <key>StandardOutPath</key> <string>/Users/SHARED/optiflow-ai/data/logs/stdout.log</string>
  <key>StandardErrorPath</key> <string>/Users/SHARED/optiflow-ai/data/logs/stderr.log</string>
</dict>
</plist>
```

Load it: `launchctl load ~/Library/LaunchAgents/com.optiflow.ai.plist`

### Linux — systemd

Create `/etc/systemd/system/optiflow.service`:

```ini
[Unit]
Description=OptiFlow AI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=optiflow
WorkingDirectory=/opt/optiflow-ai
ExecStart=/opt/optiflow-ai/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
StandardOutput=append:/opt/optiflow-ai/data/logs/stdout.log
StandardError=append:/opt/optiflow-ai/data/logs/stderr.log

[Install]
WantedBy=multi-user.target
```

Enable: `sudo systemctl daemon-reload && sudo systemctl enable --now optiflow`

**Why bind to 127.0.0.1 only:** the app itself has no authentication. All traffic from outside the host must go through the reverse proxy.

---

## 6. Reverse proxy (Caddy) — TLS + Basic Auth

Create `/etc/caddy/Caddyfile` (or `$(brew --prefix)/etc/Caddyfile` on Mac):

```caddy
# Internal hostname — point the office DNS at this machine's LAN IP.
# If you don't run internal DNS, use the machine's hostname.local (mDNS).
optiflow.office.local {
    # Self-signed cert auto-generated for internal hosts
    tls internal

    # Basic auth — generate hash with:  caddy hash-password
    basicauth {
        ceo         JDJhJDE0JDN...<hash>...
        analyst     JDJhJDE0JDVxZ...<hash>...
    }

    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1   # required: streams SSE without buffering
    }
}
```

Reload: `sudo systemctl reload caddy` / `brew services restart caddy`.

**Why `flush_interval -1`:** SSE streams break if any proxy buffers responses. This tells Caddy to forward bytes immediately — required for the live thinking stream.

The CEO opens `https://optiflow.office.local`, accepts the internal cert once, and logs in with their Basic-Auth credentials.

---

## 7. Secrets management

The Anthropic API key is the only real secret.

- **Stored at rest:** `data/config/app.json` (encrypted with `data/config/.secret`)
- **Never in git:** `data/` is already in `.gitignore` — verify before first push
- **Rotation:** rotate quarterly or whenever someone leaves. Re-enter in the setup wizard; old key is overwritten.

MSSQL credentials: stored encrypted in `data/config/sources/<name>.json`. Same rotation rules.

---

## 8. Networking checklist

- [ ] App bound to `127.0.0.1:8000` only (not `0.0.0.0`)
- [ ] Caddy on `:443` accepts traffic from the office LAN subnet only (firewall rule on the host, or office router ACL)
- [ ] DNS: office DNS has `optiflow.office.local → 10.x.x.x` (or document the `hostname.local` mDNS name)
- [ ] Outbound TLS to `api.anthropic.com:443` is allowed by the corporate firewall
- [ ] Outbound access to the MSSQL host:port from the Mac/VM is allowed

---

## 9. Backups

Everything we care about lives under `data/`. Nightly job:

```bash
# /usr/local/bin/optiflow-backup.sh
DATE=$(date +%Y%m%d)
cd /opt/optiflow-ai
tar czf /mnt/nas/optiflow/optiflow-$DATE.tgz data/
find /mnt/nas/optiflow -name 'optiflow-*.tgz' -mtime +30 -delete
```

Schedule with cron (`0 2 * * *`) or `launchd`. 30-day retention is enough for pilot.

**Restore:** stop the service, extract the tarball over `data/`, start the service. The app rehydrates sources and schemas on startup.

---

## 10. Monitoring during the pilot

No Prometheus/Grafana needed for a pilot. Two things are enough:

1. **Log tail on the host**
   ```bash
   tail -f data/logs/stderr.log
   ```
   Watch for `[AIClient] 429` (rate limit) and `[Agent] end_turn with empty answer` (the explicit-error path).

2. **Weekly review** of `data/logs/ai_calls.jsonl` to see how much API spend the pilot generated (the file records one row per call with token counts, added recently).

---

## 11. CEO onboarding (the 5-minute version)

Hand the CEO this short doc:

> **OptiFlow AI — How to use**
>
> 1. Open `https://optiflow.office.local` in Chrome or Safari
> 2. Accept the one-time "internal certificate" warning (safe — it's our office server)
> 3. Log in with the username/password sent over Slack
> 4. Type a question in plain English and press Enter
>    - Examples: *"revenue last 30 days"*, *"top 5 pending invoices"*, *"compare quotations this month vs last month"*
> 5. If something looks wrong, click **Retry** or ping #optiflow-pilot on Slack
>
> The agent only reads the database — it cannot change any data.

---

## 12. Rollout sequence

| Day | Action |
|-----|--------|
| D0  | Install host, set up Caddy, run setup wizard, verify a known-answer question |
| D1  | 2 internal testers use it for a day. Collect feedback. |
| D2  | Fix any obvious issues. Add/refine `company.md` entries based on tester questions. |
| D3  | Give CEO access. Sit with them for the first 10 minutes. |
| D4–D14 | Daily log review; weekly `company.md` refinement based on questions that went wrong. |
| D15 | Pilot retro — decide to expand, refine, or wind down. |

---

## 13. Kill switch

If something goes badly wrong during the pilot:

```bash
# macOS
launchctl unload ~/Library/LaunchAgents/com.optiflow.ai.plist

# Linux
sudo systemctl stop optiflow
```

That's it. The CEO's browser shows a connection error, no data is touched. To roll back fully, remove the Caddy route and restore the last `data/` tarball.

---

## 14. Cost envelope (for budgeting)

Rough monthly cost for a CEO pilot:

| Line item | Est. |
|-----------|------|
| Anthropic API (~200 questions/month × avg 6 tool-iterations × ~8k input / 1.5k output tokens, Sonnet pricing) | $15–$40 |
| Hardware (amortized Mac mini, if new) | $30–$50 |
| Electricity | $2 |
| **Total** | **$50–$100 / month** |

Actual usage will vary with question complexity and retries. The `ai_calls.jsonl` log gives the precise number after week 1.

---

## 15. Known limitations the CEO should be told about

1. **Read-only.** The agent cannot write, update, or delete anything. If they ask it to, it will refuse.
2. **One question at a time.** No multi-tab parallelism per user yet.
3. **English only.** No localization pass has been done.
4. **Does not remember across sessions.** Closing the browser clears chat history (intentional — keeps data lightweight).
5. **Rate limits are real.** During peak usage the Anthropic API may throttle; the UI shows a live retry countdown.
6. **Schema freshness.** If a new table is added to the DB, run **Reset & Discover Schema** from the setup wizard — the app does not auto-refresh.

---

*Last updated: 2026-04-17*
