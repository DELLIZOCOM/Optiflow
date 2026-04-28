# OptiFlow AI — Docker setup (testing on another PC)

This is a **one-shot, testing-only** guide for getting OptiFlow running on a
different machine via Docker. It is **not production hardening** — there's
no TLS, no secrets manager, no horizontal scaling. The point is: clone the
repo on another Windows / macOS / Linux machine, run two commands, get the
app on `http://localhost:8000`.

---

## 0. Why a single container?

OptiFlow's FastAPI server already serves **both** the JSON API and the
static frontend (HTML/JS/CSS) out of the same process — there is no separate
build step for the UI, no nginx, no React bundle. Splitting "frontend" and
"backend" into two containers would just add network plumbing, break SSE
buffering defaults, and double the things that can fail. For a testing
transfer, **one container is the right answer**.

The compose file is structured so adding more services later (e.g. a
separate Postgres, a Redis, a reverse proxy) is straightforward, but you
don't need that to test.

---

## 1. Install Docker Desktop on Windows

1. Download from <https://www.docker.com/products/docker-desktop/>.
2. Run the installer, accept defaults — let it enable **WSL 2** when it
   prompts. (You don't need to install a Linux distro yourself; Docker
   Desktop manages a tiny one internally.)
3. Reboot when it asks.
4. Launch **Docker Desktop**. Wait until the whale icon in the system tray
   shows a steady green "Engine running."
5. Open **PowerShell** (or **Windows Terminal**) and verify:

   ```powershell
   docker version
   docker compose version
   ```

   Both should print version strings. If `docker compose` errors out, you
   have an old standalone `docker-compose`; upgrade Docker Desktop.

> **Apple Silicon Mac note** — Docker Desktop installs the same way; the
> Microsoft ODBC repo we use serves both `amd64` and `arm64`, so the build
> works native on M-series chips with no emulation.

---

## 2. Get the code onto the target machine

Either clone:

```powershell
git clone https://github.com/DELLIZOCOM/Optiflow.git optiflow
cd optiflow
```

…or copy the entire `optiflow-ai` directory from a USB drive / network share.
Just make sure the structure looks like:

```
optiflow\
  app\
  frontend\
  data\                  <- might be empty or missing on a fresh transfer
  Dockerfile
  docker-compose.yml
  .dockerignore
  requirements.txt
```

### What to copy (or NOT copy) inside `data/`

`data/` is bind-mounted into the container, so anything you put there
becomes the running app's state.

| Path | Copy from old machine? | Why |
|---|---|---|
| `data/config/.secret` | **Yes**, if you want to keep saved encrypted credentials | This is the Fernet key; without it, the encrypted passwords in the configs are unreadable |
| `data/config/app.json` | Yes, if you want to keep your AI API key | Encrypted with `.secret` above |
| `data/config/sources/*.json` | Yes, for saved DB connections | Encrypted with `.secret` |
| `data/config/email/*.json` | Yes, for saved Outlook/IMAP creds | Encrypted with `.secret` |
| `data/cache/email.db*` | Optional | Re-built on next sync; copying just saves you the wait |
| `data/cache/sessions.db` | Optional | Just chat history — you can lose it without harm |
| `data/sources/<name>/` | Optional | Schema snapshots; will rebuild on next discovery |
| `data/knowledge/company.md` | Yes, if you've curated business context | This is hand-edited content, not regenerable |
| `data/logs/*.jsonl` | Skip | Audit logs — local to the old machine |

If you don't bring the `.secret` file, **all encrypted credentials will fail
to decrypt** and you'll need to re-run the setup wizard. That's the safest
path on a fresh machine anyway.

---

## 3. First run

From the project root:

```powershell
docker compose up -d --build
```

This will:

1. Build the image `optiflow-ai:local` from the `Dockerfile` (3–6 minutes
   the first time; the slow steps are the Microsoft ODBC repo install and
   the Python wheels).
2. Start the container in the background.
3. Bind-mount `./data` into `/app/data` inside the container.
4. Expose port `8000` on your host.

Verify it's up:

```powershell
docker compose ps
```

Look for `STATUS = Up X seconds (healthy)` (the healthcheck takes ~20
seconds to flip from "starting" to "healthy"). Then open your browser to:

```
http://localhost:8000
```

If you have nothing configured yet, the root route will redirect to
`http://localhost:8000/setup` — this is intentional, do the wizard.

---

## 4. Daily operations

```powershell
# tail logs (Ctrl+C to detach — does NOT stop the container)
docker compose logs -f optiflow

# stop the container, keep the data
docker compose down

# start it again later
docker compose up -d

# rebuild the image after editing app/ or frontend/ code
docker compose up -d --build

# open a shell inside the running container (debugging)
docker compose exec optiflow bash

# nuke EVERYTHING and start over (image, container, volumes, your data)
docker compose down
Remove-Item -Recurse -Force data\cache, data\config\sources, data\config\email, data\sources, data\knowledge -ErrorAction SilentlyContinue
docker compose up -d --build
```

> **Reset without rebuild** — if you just want to wipe state but not the
> image, skip `--build`. The image will be reused; your `data/` directory
> on the host is the only thing that gets touched.

---

## 5. Connecting to a database that lives outside Docker

Three common scenarios:

**A) SQL Server is on the same Windows machine as Docker Desktop.**

- Use `host.docker.internal` instead of `localhost` / `127.0.0.1` in the
  setup wizard. The compose file already pre-wires this hostname.
- If SQL Server only listens on `localhost`, enable TCP/IP in
  *SQL Server Configuration Manager* → *SQL Server Network Configuration*
  → *Protocols for MSSQLSERVER*, and restart the service.
- Windows Firewall: allow inbound TCP 1433 for the Docker subnet (usually
  `172.17.0.0/16` or whatever Docker Desktop assigned).

**B) SQL Server is on the same LAN (e.g. `192.168.1.198`).**

- Just use the LAN IP. Docker's default bridge network NATs out to the
  host's network, so this works without any extra setup.

**C) SQL Server is in another Docker container.**

- Add it as a second service in `docker-compose.yml` and reference it by
  service name. Out of scope for this guide.

---

## 6. Connecting an email mailbox from inside the container

Same rules as the database:

- **Outlook (Microsoft Graph)** — outbound HTTPS to
  `https://login.microsoftonline.com` and `https://graph.microsoft.com`.
  Works through the container's default outbound NAT, no special setup.
- **IMAP (GoDaddy / Zoho / FastMail / cPanel / on-prem)** — outbound TCP
  to your IMAP host on 993. Same default-bridge NAT, just works.

---

## 7. Troubleshooting

**Build fails on `msodbcsql18` install.**

The Microsoft repo briefly returns 503s during heavy traffic. Re-run
`docker compose up -d --build`. If it persists, comment the Microsoft repo
lines in the `Dockerfile` and use Driver 17 instead — OptiFlow's connector
falls back to it.

**Healthcheck stays "starting" / "unhealthy".**

```powershell
docker compose logs optiflow
```

Most common: the AI key isn't set yet. The healthcheck only needs
`/setup/status` to return 200 — it does, even with nothing configured. If
you see import errors or `pyodbc.OperationalError`, tell me what they say.

**`docker compose up` says port 8000 is already in use.**

Change the host port in `docker-compose.yml`:

```yaml
ports:
  - "8080:8000"   # host 8080 → container 8000
```

Then visit `http://localhost:8080`.

**The agent says "Login failed" but the wizard's Test Connection passed.**

This was a real bug fixed on 2026-04-28 (commit `fc8ee6a`) — make sure
you're running with the latest code. If it still happens, share the
container logs.

**Charts don't render in the browser.**

The frontend pulls Chart.js + marked.js from public CDNs (`cdnjs` and
`jsdelivr`). The container itself doesn't need network for this, but the
*browser* does. Check the browser console for blocked CDN requests if your
test machine is on a locked-down network.

**On Windows, file changes inside `./data` are slow.**

Docker Desktop on Windows uses WSL2. If `./data` lives on a Windows
filesystem path (`C:\...`) bind-mounted into a Linux container, file I/O
is slower than a native Linux mount. For testing this is fine; if you
notice it, move the project under your WSL2 home directory
(`\\wsl$\Ubuntu\home\<you>\optiflow`) and bind-mount from there.

---

## 8. What's intentionally not done

| Concern | Status |
|---|---|
| HTTPS / TLS termination | Not configured. Run behind a reverse proxy (nginx, Caddy, Traefik) if you put this on a real network. |
| Multi-worker uvicorn | Not configured. The IMAP / Outlook coordinators hold per-process state and would double-poll under multi-worker. |
| Secrets-manager integration | Not configured. Encrypted creds live in `data/config/`, encrypted by `data/config/.secret`. |
| Health-check exposed externally | Internal only. Container's own probe; not surfaced to the LAN. |
| Image push to a registry | Not configured. The image stays local (`optiflow-ai:local`). For another machine: copy the repo and build there, or `docker save \| ssh \| docker load`. |
| Resource limits (cpu, memory) | Not set. Add `deploy.resources` if your test box is constrained. |

For a real production deployment, see `HOSTING.md` (separate, more involved
guide). This file is **only** for "make it run on another PC for testing."

---

## 9. Transferring the running setup to another machine

If you've already configured everything on machine A and want to take it to
machine B:

```powershell
# On machine A:
docker compose down                         # flushes pending writes to data/
# (no need to export images; we'll rebuild on B from the same source)
# Then copy the entire project directory (including data/) to machine B.

# On machine B (after Docker Desktop is installed):
cd path\to\copied\optiflow
docker compose up -d --build
```

Because the image is built from source and `./data` is bind-mounted, your
encrypted creds and the email DB just travel with the directory. If you
forgot to copy `data/config/.secret`, the Fernet key won't match and you'll
need to re-enter credentials in the setup wizard — not a disaster.

---

## 10. Quick reference — one screen of commands

```powershell
docker compose up -d --build         # first run / after code change
docker compose ps                    # is it healthy?
docker compose logs -f optiflow      # live logs
docker compose exec optiflow bash    # shell inside the container
docker compose restart optiflow      # restart without rebuild
docker compose down                  # stop, keep data
docker compose down --rmi local      # stop + remove the built image
```

URL: <http://localhost:8000>
