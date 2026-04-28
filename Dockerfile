# =============================================================================
# OptiFlow AI — single-container image (API + static frontend served together)
#
# Goal: portable, identical-behavior runtime across macOS / Windows / Linux for
# testing. NOT production-hardened (no TLS, no secrets manager, single replica).
# =============================================================================
FROM python:3.12-slim-bookworm

# Don't write .pyc files, force unbuffered stdio so `docker logs` is real-time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# -----------------------------------------------------------------------------
# Microsoft ODBC Driver 18 for SQL Server.
# OptiFlow's MSSQL connector falls back from Driver 18 → Driver 17, so we ship
# 18 (the recommended one). Debian 12 (Bookworm) is what python:3.12-slim is
# built on, so we use the matching MS repo. ACCEPT_EULA is required to install.
# -----------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        ca-certificates \
        apt-transport-https \
        gcc \
        g++ \
        unixodbc-dev \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# -----------------------------------------------------------------------------
# Install Python deps before copying source so code edits don't bust this layer.
# -----------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install -r requirements.txt

# -----------------------------------------------------------------------------
# App code. `data/` is intentionally NOT copied — it's bind-mounted from the
# host at runtime so the user's encrypted creds, schema cache, and email DB
# persist across container restarts.
# -----------------------------------------------------------------------------
COPY app/      ./app/
COPY frontend/ ./frontend/

# Run as a non-root user so a misbehaving container can't trivially trash the
# host volume. The /app dir already belongs to root from the COPY above; we
# just need ownership of the runtime data dir, which Docker will create when
# the volume is mounted.
RUN useradd --create-home --shell /bin/bash --uid 1001 optiflow \
 && mkdir -p /app/data \
 && chown -R optiflow:optiflow /app
USER optiflow

EXPOSE 8000

# Cheap liveness probe against a route that doesn't require a configured AI key.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/setup/status', timeout=3).status==200 else sys.exit(1)" || exit 1

# Single worker — OptiFlow's coordinators (IMAP poll loop, Outlook delta loop)
# hold per-process state. Don't multi-worker without redesigning that.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
