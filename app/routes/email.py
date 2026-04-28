"""
Email-source setup and status routes.

Outlook (Microsoft 365 / Exchange Online via admin consent + Microsoft Graph):
  POST   /setup/email/outlook/test
  POST   /setup/email/outlook
  DELETE /setup/email/outlook

IMAP (GoDaddy Workspace, Zoho, FastMail, cPanel, on-prem Exchange, etc.):
  POST   /setup/email/imap/test
  POST   /setup/email/imap
  DELETE /setup/email/imap

Shared:
  GET  /setup/email/status      — live status for setup wizard + sidebar
  GET  /setup/email/providers   — preset host/port hints for the UI

At most one provider can be active at a time. Connecting one disconnects the
other. The agent doesn't care which connector filled the database — both
write into the same EmailStore.
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


_MAX_ID_LEN     = 80
_MAX_SECRET_LEN = 512
_MAX_NAME_LEN   = 120
_MAX_PASS_LEN   = 256
_MAX_HOST_LEN   = 255
_MAX_FOLDER_LEN = 80


class OutlookCredsRequest(BaseModel):
    tenant_id:           str = Field(..., min_length=8, max_length=_MAX_ID_LEN)
    client_id:           str = Field(..., min_length=8, max_length=_MAX_ID_LEN)
    client_secret:       str = Field(..., min_length=8, max_length=_MAX_SECRET_LEN)
    tenant_display_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    added_by:            Optional[str] = Field(default=None, max_length=120)
    backfill_days:       int = Field(default=365, ge=30, le=3650)

    @field_validator("tenant_id", "client_id", "tenant_display_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class OutlookDisconnectRequest(BaseModel):
    wipe_cache: bool = False


class IMAPMailboxIn(BaseModel):
    account_email: str = Field(..., min_length=3, max_length=_MAX_NAME_LEN)
    password:      str = Field(..., min_length=1, max_length=_MAX_PASS_LEN)
    display_name:  Optional[str] = Field(default=None, max_length=_MAX_NAME_LEN)
    folder:        str = Field(default="INBOX", min_length=1, max_length=_MAX_FOLDER_LEN)

    @field_validator("account_email", "folder")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class IMAPCredsRequest(BaseModel):
    provider:            str = Field(default="generic", max_length=40)         # 'godaddy' | 'generic'
    tenant_display_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    host:                str = Field(..., min_length=3, max_length=_MAX_HOST_LEN)
    port:                int = Field(default=993, ge=1, le=65535)
    use_ssl:             bool = True
    mailboxes:           list[IMAPMailboxIn] = Field(..., min_length=1, max_length=200)
    added_by:            Optional[str] = Field(default=None, max_length=120)
    backfill_days:       int = Field(default=365, ge=30, le=3650)

    @field_validator("provider", "tenant_display_name", "host")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class IMAPDisconnectRequest(BaseModel):
    wipe_cache: bool = False


class SyncNowRequest(BaseModel):
    """Body for POST /setup/email/sync_now."""
    mailbox_id: Optional[str] = Field(default=None, max_length=120)


class IMAPMailboxAddRequest(IMAPMailboxIn):
    """Adding one mailbox to an existing IMAP config — same shape as a mailbox row."""
    pass


class IMAPMailboxRemoveRequest(BaseModel):
    account_email: str = Field(..., min_length=3, max_length=_MAX_NAME_LEN)
    purge_cache:   bool = False

    @field_validator("account_email")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().lower()


# ── Entity-resolution payloads ──────────────────────────────────────────────

_ENTITY_KINDS = ("customer", "vendor", "employee", "unknown")


class EntityUpsertRequest(BaseModel):
    """
    Create OR update an entity. UNIQUE(kind, canonical_email) means the same
    payload twice is idempotent. The first email in `emails` becomes
    canonical; subsequent ones are aliases.
    """
    kind:         str = Field(default="unknown")
    display_name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LEN)
    emails:       list[str] = Field(..., min_length=1, max_length=20)
    company:      Optional[str] = Field(default=None, max_length=_MAX_NAME_LEN)
    notes:        Optional[str] = Field(default=None, max_length=2000)
    source_pk:    Optional[str] = Field(default=None, max_length=120)
    confidence:   float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v: str) -> str:
        v = (v or "unknown").strip().lower()
        if v not in _ENTITY_KINDS:
            raise ValueError(f"kind must be one of {_ENTITY_KINDS}")
        return v

    @field_validator("emails")
    @classmethod
    def _emails_ok(cls, v: list[str]) -> list[str]:
        cleaned = [str(a).strip().lower() for a in v if str(a).strip()]
        if not cleaned:
            raise ValueError("at least one email address is required")
        return cleaned


class EntityUpdateRequest(BaseModel):
    """Partial update — only fields you set are touched."""
    kind:         Optional[str] = None
    display_name: Optional[str] = Field(default=None, max_length=_MAX_NAME_LEN)
    company:      Optional[str] = Field(default=None, max_length=_MAX_NAME_LEN)
    notes:        Optional[str] = Field(default=None, max_length=2000)
    source_pk:    Optional[str] = Field(default=None, max_length=120)
    confidence:   Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        if v not in _ENTITY_KINDS:
            raise ValueError(f"kind must be one of {_ENTITY_KINDS}")
        return v


def create_email_router(
    *,
    source_registry,
    tool_registry,
    get_or_create_store,   # callable () -> EmailStore
    install_source,        # callable (OutlookSource) -> None
    uninstall_source,      # callable (name: str) -> None
) -> APIRouter:
    """
    Factory so we can wire in the app's registries and lifecycle hooks
    without creating cycles at import time.

    Mounted with no prefix; frontend calls POST /setup/email/outlook etc.
    """
    r = APIRouter()

    _OUTLOOK_SOURCE_NAME = "outlook"
    _IMAP_SOURCE_NAME    = "imap"

    def _existing_outlook_source():
        return source_registry.get(_OUTLOOK_SOURCE_NAME)

    def _existing_imap_source():
        return source_registry.get(_IMAP_SOURCE_NAME)

    async def _disconnect_other(active: str) -> None:
        """When connecting one provider, tear down the other so we never run both."""
        from app.config import delete_outlook_config, delete_imap_config
        if active == "outlook" and _existing_imap_source():
            await uninstall_source(_IMAP_SOURCE_NAME)
            delete_imap_config()
        if active == "imap" and _existing_outlook_source():
            await uninstall_source(_OUTLOOK_SOURCE_NAME)
            delete_outlook_config()

    # ── shared: provider presets for the UI ─────────────────────────────────

    @r.get("/setup/email/providers")
    async def email_providers():
        """Static list of provider presets the wizard renders into a picker."""
        return JSONResponse({
            "providers": [
                {
                    "id":       "outlook",
                    "label":    "Microsoft 365 / Outlook",
                    "kind":     "graph",
                    "needs":    ["tenant_id", "client_id", "client_secret"],
                    "summary":  "For Exchange Online / Microsoft 365 tenants. Uses admin-consent (app-only) Microsoft Graph.",
                },
                {
                    "id":       "godaddy",
                    "label":    "GoDaddy Workspace Email",
                    "kind":     "imap",
                    "host":     "imap.secureserver.net",
                    "port":     993,
                    "use_ssl":  True,
                    "needs":    ["mailboxes"],
                    "summary":  "GoDaddy's standard mailbox product (not the Microsoft 365 plan). IMAP with username/password.",
                },
                {
                    "id":       "generic",
                    "label":    "Generic IMAP (Zoho / FastMail / cPanel / on-prem)",
                    "kind":     "imap",
                    "host":     "",
                    "port":     993,
                    "use_ssl":  True,
                    "needs":    ["host", "port", "use_ssl", "mailboxes"],
                    "summary":  "Any RFC 3501 IMAP server. You supply host + port + per-mailbox credentials.",
                },
            ],
        })

    # ── test-only (no persistence) ───────────────────────────────────────────

    @r.post("/setup/email/outlook/test")
    async def outlook_test(req: OutlookCredsRequest):
        from app.sources.email.outlook.auth import OutlookCredentials
        from app.sources.email.outlook.source import OutlookSource

        probe_store = get_or_create_store()
        probe = OutlookSource(
            name=f"{_OUTLOOK_SOURCE_NAME}-probe",
            tenant_display_name=req.tenant_display_name,
            credentials=OutlookCredentials(
                tenant_id=req.tenant_id,
                client_id=req.client_id,
                client_secret=req.client_secret,
            ),
            store=probe_store,
            backfill_days=req.backfill_days,
        )
        try:
            ok, err = await probe.test_credentials()
        finally:
            await probe.stop()

        if not ok:
            return JSONResponse(
                {"success": False, "error": err or "credential test failed"},
                status_code=400,
            )
        return JSONResponse({"success": True})

    # ── save + start ────────────────────────────────────────────────────────

    @r.post("/setup/email/outlook")
    async def outlook_connect(req: OutlookCredsRequest):
        from app.config import save_outlook_config
        from app.sources.email.outlook.auth import OutlookCredentials
        from app.sources.email.outlook.source import OutlookSource

        # Validate first
        probe_store = get_or_create_store()
        probe = OutlookSource(
            name=f"{_OUTLOOK_SOURCE_NAME}-probe",
            tenant_display_name=req.tenant_display_name,
            credentials=OutlookCredentials(
                tenant_id=req.tenant_id,
                client_id=req.client_id,
                client_secret=req.client_secret,
            ),
            store=probe_store,
            backfill_days=req.backfill_days,
        )
        ok, err = await probe.test_credentials()
        await probe.stop()
        if not ok:
            return JSONResponse(
                {"success": False, "error": err or "credentials rejected by Microsoft Graph"},
                status_code=400,
            )

        # Persist
        save_outlook_config({
            "tenant_id":           req.tenant_id,
            "client_id":           req.client_id,
            "client_secret":       req.client_secret,
            "tenant_display_name": req.tenant_display_name,
            "added_at":            time.time(),
            "added_by":            req.added_by or "",
            "backfill_days":       req.backfill_days,
        })

        # Swap in the live source — tear down IMAP if it was the prior provider
        await _disconnect_other("outlook")
        if _existing_outlook_source():
            await uninstall_source(_OUTLOOK_SOURCE_NAME)

        live_store = get_or_create_store()
        live = OutlookSource(
            name=_OUTLOOK_SOURCE_NAME,
            tenant_display_name=req.tenant_display_name,
            credentials=OutlookCredentials(
                tenant_id=req.tenant_id,
                client_id=req.client_id,
                client_secret=req.client_secret,
            ),
            store=live_store,
            backfill_days=req.backfill_days,
        )
        await install_source(live)
        return JSONResponse({"success": True, "source_name": _OUTLOOK_SOURCE_NAME, "provider": "outlook"})

    # ── disconnect ──────────────────────────────────────────────────────────

    @r.delete("/setup/email/outlook")
    async def outlook_disconnect(req: OutlookDisconnectRequest):
        from app.config import delete_outlook_config

        if _existing_outlook_source():
            await uninstall_source(_OUTLOOK_SOURCE_NAME)
        delete_outlook_config()

        if req.wipe_cache:
            try:
                store = get_or_create_store()
                store.purge_all()
            except Exception:
                logger.exception("Failed to purge email cache on disconnect")
        return JSONResponse({"success": True, "wiped_cache": req.wipe_cache})

    # ══════════════════════════════════════════════════════════════════════════
    # IMAP (GoDaddy / Zoho / FastMail / cPanel / on-prem / generic)
    # ══════════════════════════════════════════════════════════════════════════

    def _build_imap_source(req: IMAPCredsRequest, *, name: str):
        """Construct an IMAPSource from the request — used by both test and live paths."""
        from app.sources.email.imap.client  import IMAPServer
        from app.sources.email.imap.ingest  import IMAPMailboxConfig
        from app.sources.email.imap.source  import IMAPSource

        server = IMAPServer(host=req.host, port=req.port, use_ssl=req.use_ssl)
        mbs = [
            IMAPMailboxConfig(
                account_email=m.account_email.lower(),
                password=m.password,
                display_name=m.display_name,
                folder=m.folder or "INBOX",
            )
            for m in req.mailboxes
        ]
        return IMAPSource(
            name=name,
            tenant_display_name=req.tenant_display_name,
            server=server,
            mailboxes=mbs,
            store=get_or_create_store(),
            provider_label=req.provider or "imap",
            backfill_days=req.backfill_days,
        )

    @r.post("/setup/email/imap/test")
    async def imap_test(req: IMAPCredsRequest):
        """Validate IMAP credentials for every mailbox in the request, no persistence."""
        probe = _build_imap_source(req, name=f"{_IMAP_SOURCE_NAME}-probe")
        try:
            ok, err = await probe.test_credentials()
        finally:
            await probe.stop()
        if not ok:
            return JSONResponse(
                {"success": False, "error": err or "credential test failed"},
                status_code=400,
            )
        return JSONResponse({"success": True, "mailbox_count": len(req.mailboxes)})

    @r.post("/setup/email/imap")
    async def imap_connect(req: IMAPCredsRequest):
        """Validate, persist, and start an IMAP source. Disconnects any Outlook source first."""
        from app.config import save_imap_config

        # Validate first — fail loud if any mailbox is wrong before we touch disk
        probe = _build_imap_source(req, name=f"{_IMAP_SOURCE_NAME}-probe")
        ok, err = await probe.test_credentials()
        await probe.stop()
        if not ok:
            return JSONResponse(
                {"success": False, "error": err or "IMAP login failed"},
                status_code=400,
            )

        # Persist (per-mailbox passwords get Fernet-encrypted)
        save_imap_config({
            "provider":            req.provider or "generic",
            "tenant_display_name": req.tenant_display_name,
            "host":                req.host,
            "port":                req.port,
            "use_ssl":             req.use_ssl,
            "backfill_days":       req.backfill_days,
            "mailboxes": [
                {
                    "account_email": m.account_email,
                    "password":      m.password,
                    "display_name":  m.display_name,
                    "folder":        m.folder,
                }
                for m in req.mailboxes
            ],
            "added_at":            time.time(),
            "added_by":            req.added_by or "",
        })

        # Swap in the live source, tearing down Outlook if it was prior
        await _disconnect_other("imap")
        if _existing_imap_source():
            await uninstall_source(_IMAP_SOURCE_NAME)

        live = _build_imap_source(req, name=_IMAP_SOURCE_NAME)
        await install_source(live)
        return JSONResponse({
            "success":     True,
            "source_name": _IMAP_SOURCE_NAME,
            "provider":    req.provider or "imap",
            "mailbox_count": len(req.mailboxes),
        })

    @r.delete("/setup/email/imap")
    async def imap_disconnect(req: IMAPDisconnectRequest):
        from app.config import delete_imap_config

        if _existing_imap_source():
            await uninstall_source(_IMAP_SOURCE_NAME)
        delete_imap_config()

        if req.wipe_cache:
            try:
                store = get_or_create_store()
                store.purge_all()
            except Exception:
                logger.exception("Failed to purge email cache on disconnect")
        return JSONResponse({"success": True, "wiped_cache": req.wipe_cache})

    # ══════════════════════════════════════════════════════════════════════════
    # Shared status (provider-aware)
    # ══════════════════════════════════════════════════════════════════════════

    def _empty_status(provider: Optional[str] = None) -> dict:
        return {
            "configured":     False,
            "provider":       provider,
            "live":           False,
            "mailboxes":      {"total": 0, "active": 0, "with_errors": 0, "initial_synced": 0},
            "messages_total": 0,
            "last_sync_at":   None,
            "errors":         [],
        }

    @r.get("/setup/email/status")
    async def email_status():
        """
        Returns a uniform status payload regardless of which provider is active.
        UI uses `provider` to decide which connect form to show on disconnect.
        """
        from app.config import load_outlook_config, load_imap_config

        outlook_cfg = load_outlook_config()
        imap_cfg    = load_imap_config()

        # Determine which provider (if any) is configured
        if outlook_cfg and outlook_cfg.get("client_id"):
            provider = "outlook"
            display_name = outlook_cfg.get("tenant_display_name")
            tenant_label = outlook_cfg.get("tenant_id")
            src = _existing_outlook_source()
        elif imap_cfg and imap_cfg.get("host"):
            provider = "imap"
            display_name = imap_cfg.get("tenant_display_name")
            tenant_label = f"{imap_cfg.get('host')}:{imap_cfg.get('port')}"
            src = _existing_imap_source()
        else:
            return JSONResponse(_empty_status())

        store = src.store if src else None
        if store is None:
            payload = _empty_status(provider)
            payload["configured"]   = True
            payload["display_name"] = display_name
            payload["tenant_id"]    = tenant_label
            return JSONResponse(payload)

        mailboxes = store.list_mailboxes(active_only=False)
        total   = len(mailboxes)
        active  = sum(1 for m in mailboxes if m.get("status") == "active")
        errors  = [
            {"mailbox": m.get("account_email"), "error": m.get("last_error")}
            for m in mailboxes if m.get("last_error")
        ]
        initial   = sum(1 for m in mailboxes if m.get("initial_synced"))
        msg_total = sum(int(m.get("message_count") or 0) for m in mailboxes)
        last_sync = max(
            (float(m["last_sync_at"]) for m in mailboxes if m.get("last_sync_at")),
            default=None,
        )

        # IMAP-specific extras for the dashboard
        extras: dict = {}
        if provider == "imap" and imap_cfg:
            extras = {
                "host":          imap_cfg.get("host"),
                "port":          imap_cfg.get("port"),
                "use_ssl":       imap_cfg.get("use_ssl", True),
                "imap_provider": imap_cfg.get("provider"),
                "configured_mailboxes": [
                    {"account_email": m["account_email"], "folder": m["folder"]}
                    for m in (imap_cfg.get("mailboxes") or [])
                ],
            }

        # Per-mailbox detail rows for the management dashboard.
        # Surfaces everything the table needs without a second round-trip.
        mailbox_details = [
            {
                "id":              m.get("id"),
                "account_email":   m.get("account_email"),
                "display_name":    m.get("display_name"),
                "status":          m.get("status"),
                "folder":          m.get("folder"),
                "message_count":   int(m.get("message_count") or 0),
                "last_sync_at":    m.get("last_sync_at"),
                "last_error":      m.get("last_error"),
                "initial_synced":  bool(m.get("initial_synced")),
                "backfill_done":   bool(m.get("backfill_done")),
                "discovered_at":   m.get("discovered_at"),
            }
            for m in mailboxes
        ]

        return JSONResponse({
            "configured":      True,
            "provider":        provider,
            "live":            True,
            "tenant_id":       tenant_label,
            "display_name":    display_name,
            "mailboxes": {
                "total":          total,
                "active":         active,
                "with_errors":    len(errors),
                "initial_synced": initial,
            },
            "mailbox_details": mailbox_details,
            "messages_total":  msg_total,
            "last_sync_at":    last_sync,
            "errors":          errors[:20],
            **extras,
        })

    # ══════════════════════════════════════════════════════════════════════════
    # Management endpoints (sync now / add+remove mailbox / recent activity)
    # ══════════════════════════════════════════════════════════════════════════

    @r.post("/setup/email/sync_now")
    async def email_sync_now(req: SyncNowRequest):
        """
        Trigger an immediate poll cycle without waiting for the 5-minute timer.
        - IMAP: nudges the wake event for one mailbox (by id) or all mailboxes.
        - Outlook: today the delta loop has its own cadence and no manual hook,
          so we report the no-op honestly rather than pretend it fired.
        """
        imap_src = _existing_imap_source()
        if imap_src is not None:
            try:
                fired = imap_src.sync_now(req.mailbox_id)
            except Exception as e:
                logger.exception("[IMAP] sync_now failed")
                return JSONResponse(
                    {"success": False, "error": f"sync_now failed: {e}"},
                    status_code=500,
                )
            return JSONResponse({
                "success":  True,
                "provider": "imap",
                "fired":    fired,
            })

        outlook_src = _existing_outlook_source()
        if outlook_src is not None:
            return JSONResponse({
                "success":  True,
                "provider": "outlook",
                "fired":    0,
                "note":     "Outlook auto-syncs every 10 minutes via Microsoft Graph delta; manual sync is not yet wired.",
            })

        return JSONResponse(
            {"success": False, "error": "no email source is configured"},
            status_code=400,
        )

    @r.post("/setup/email/imap/mailboxes")
    async def imap_add_mailbox(req: IMAPMailboxAddRequest):
        """
        Add one mailbox to the running IMAP source.
        Validates by attempting login + SELECT, then persists into imap.json
        and spawns a poll task for it immediately.
        """
        from app.config import load_imap_config, save_imap_config
        from app.sources.email.imap.client  import IMAPClient, IMAPServer, IMAPAuthError
        from app.sources.email.imap.ingest  import IMAPMailboxConfig

        imap_src = _existing_imap_source()
        cfg = load_imap_config()
        if imap_src is None or not cfg or not cfg.get("host"):
            return JSONResponse(
                {"success": False, "error": "IMAP is not configured. Connect IMAP first."},
                status_code=400,
            )

        account_email = req.account_email.lower()
        existing = [m for m in (cfg.get("mailboxes") or [])
                    if (m.get("account_email") or "").lower() == account_email]
        if existing:
            return JSONResponse(
                {"success": False, "error": f"{account_email} is already configured"},
                status_code=400,
            )

        # Validate this one mailbox against the existing host/port/ssl
        server = IMAPServer(
            host=cfg["host"],
            port=int(cfg.get("port", 993)),
            use_ssl=bool(cfg.get("use_ssl", True)),
        )
        client = IMAPClient(server, account_email, req.password)
        try:
            await client.connect()
            await client.select_folder(req.folder or "INBOX")
        except IMAPAuthError as e:
            return JSONResponse(
                {"success": False, "error": f"login failed: {e}"},
                status_code=400,
            )
        except Exception as e:
            return JSONResponse(
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                status_code=400,
            )
        finally:
            await client.close()

        # Add to the running coordinator
        try:
            mailbox_id = await imap_src.add_mailbox(IMAPMailboxConfig(
                account_email=account_email,
                password=req.password,
                display_name=req.display_name,
                folder=req.folder or "INBOX",
            ))
        except Exception as e:
            logger.exception("[IMAP] add_mailbox failed")
            return JSONResponse(
                {"success": False, "error": f"failed to start poller: {e}"},
                status_code=500,
            )

        # Persist to imap.json so it survives restart
        cfg["mailboxes"] = list(cfg.get("mailboxes") or []) + [{
            "account_email": account_email,
            "password":      req.password,
            "display_name":  req.display_name,
            "folder":        req.folder or "INBOX",
        }]
        save_imap_config(cfg)

        return JSONResponse({
            "success":       True,
            "mailbox_id":    mailbox_id,
            "account_email": account_email,
        })

    @r.delete("/setup/email/imap/mailboxes")
    async def imap_remove_mailbox(req: IMAPMailboxRemoveRequest):
        """
        Stop polling a mailbox. With purge_cache=True, also drops its messages
        and sync state from the local store. Without it, the mailbox is just
        marked disabled — re-adding the same email later will resume.
        """
        from app.config import load_imap_config, save_imap_config

        imap_src = _existing_imap_source()
        cfg = load_imap_config()
        if imap_src is None or not cfg:
            return JSONResponse(
                {"success": False, "error": "IMAP is not configured"},
                status_code=400,
            )

        try:
            ok = await imap_src.remove_mailbox(req.account_email, purge_cache=req.purge_cache)
        except Exception as e:
            logger.exception("[IMAP] remove_mailbox failed")
            return JSONResponse(
                {"success": False, "error": f"failed to stop poller: {e}"},
                status_code=500,
            )

        if not ok:
            return JSONResponse(
                {"success": False, "error": f"{req.account_email} is not a configured mailbox"},
                status_code=404,
            )

        # Persist the pruned mailbox list. If nothing remains, leave the rest
        # of the IMAP config in place so the user can still add a mailbox via
        # this endpoint without re-entering host/port.
        cfg["mailboxes"] = [
            m for m in (cfg.get("mailboxes") or [])
            if (m.get("account_email") or "").lower() != req.account_email
        ]
        save_imap_config(cfg)

        return JSONResponse({
            "success":      True,
            "purged_cache": req.purge_cache,
            "remaining":    len(cfg["mailboxes"]),
        })

    @r.get("/setup/email/recent_messages")
    async def email_recent_messages(limit: int = 20, mailbox_id: Optional[str] = None):
        """
        Newest-first list of recently ingested messages — feeds the activity
        panel on the management dashboard. Provider-agnostic: reads straight
        from EmailStore.
        """
        # Clamp to keep the dashboard response small
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 20

        src = _existing_imap_source() or _existing_outlook_source()
        if src is None:
            return JSONResponse({"messages": [], "configured": False})

        try:
            rows = src.store.recent_emails(limit=limit, mailbox_id=mailbox_id)
        except Exception as e:
            logger.exception("[email] recent_emails failed")
            return JSONResponse(
                {"success": False, "error": f"failed to read recent messages: {e}"},
                status_code=500,
            )

        return JSONResponse({"messages": rows, "configured": True, "count": len(rows)})

    # ══════════════════════════════════════════════════════════════════════════
    # Entity resolution
    #
    # These routes manage the canonical "who is this person/org" registry the
    # agent uses to translate names → email addresses. The store lives inside
    # the EmailStore so it's available the moment any email source comes up,
    # but the entities themselves are independent of any specific provider.
    # ══════════════════════════════════════════════════════════════════════════

    def _entity_store_or_404():
        """
        Returns the live EmailStore (which owns entity tables) if available,
        or a (None, 503-response) tuple if no email source is configured. We
        DON'T call get_or_create_store() here because it would auto-create an
        empty DB; better to surface a clear error.
        """
        src = _existing_imap_source() or _existing_outlook_source()
        if src is None:
            return None, JSONResponse(
                {
                    "success": False,
                    "error": "Connect an email provider before managing entities. "
                             "Entities live alongside indexed mail.",
                },
                status_code=503,
            )
        return src.store, None

    @r.get("/entities")
    async def entities_list(
        kind: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 100,
        offset: int = 0,
    ):
        """List entities, newest-seen first. Optional filters."""
        store, err = _entity_store_or_404()
        if err is not None:
            return err
        if kind and kind.lower() not in _ENTITY_KINDS:
            return JSONResponse(
                {"success": False, "error": f"kind must be one of {_ENTITY_KINDS}"},
                status_code=400,
            )
        try:
            limit  = max(1, min(int(limit),  500))
            offset = max(0, int(offset))
            min_c  = max(0.0, min(float(min_confidence), 1.0))
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "limit/offset/min_confidence must be numeric"},
                status_code=400,
            )
        items = store.list_entities(
            kind=kind.lower() if kind else None,
            min_confidence=min_c,
            limit=limit,
            offset=offset,
        )
        total = store.count_entities(
            kind=kind.lower() if kind else None,
            min_confidence=min_c,
        )
        return JSONResponse({"entities": items, "count": len(items), "total": total})

    @r.get("/entities/{entity_id}")
    async def entities_get(entity_id: int):
        store, err = _entity_store_or_404()
        if err is not None:
            return err
        ent = store.get_entity(entity_id)
        if not ent:
            return JSONResponse(
                {"success": False, "error": f"entity {entity_id} not found"},
                status_code=404,
            )
        return JSONResponse(ent)

    @r.post("/entities")
    async def entities_upsert(req: EntityUpsertRequest):
        """
        Create or update an entity. Idempotent on (kind, canonical_email).
        Use this to confirm an auto-discovered entity (set kind + confidence=1.0)
        or to register a contact manually before the first email arrives.
        """
        store, err = _entity_store_or_404()
        if err is not None:
            return err
        try:
            entity_id = store.upsert_entity(
                kind=req.kind,
                display_name=req.display_name,
                emails=req.emails,
                company=req.company,
                notes=req.notes,
                source="manual",
                source_pk=req.source_pk,
                confidence=req.confidence,
            )
        except Exception as e:
            logger.exception("entities upsert failed")
            return JSONResponse(
                {"success": False, "error": f"upsert failed: {type(e).__name__}: {e}"},
                status_code=500,
            )
        if entity_id is None:
            return JSONResponse(
                {"success": False, "error": "no usable email addresses provided"},
                status_code=400,
            )
        ent = store.get_entity(entity_id) or {}
        return JSONResponse({"success": True, "entity_id": entity_id, "entity": ent})

    @r.patch("/entities/{entity_id}")
    async def entities_update(entity_id: int, req: EntityUpdateRequest):
        """Partial update — promote kind, attach a source_pk, edit notes, etc."""
        store, err = _entity_store_or_404()
        if err is not None:
            return err
        ok = store.update_entity(
            entity_id,
            kind=req.kind,
            display_name=req.display_name,
            company=req.company,
            notes=req.notes,
            source_pk=req.source_pk,
            confidence=req.confidence,
        )
        if not ok:
            return JSONResponse(
                {"success": False, "error": f"entity {entity_id} not found or no fields changed"},
                status_code=404,
            )
        return JSONResponse({"success": True, "entity": store.get_entity(entity_id)})

    @r.delete("/entities/{entity_id}")
    async def entities_delete(entity_id: int):
        store, err = _entity_store_or_404()
        if err is not None:
            return err
        ok = store.delete_entity(entity_id)
        if not ok:
            return JSONResponse(
                {"success": False, "error": f"entity {entity_id} not found"},
                status_code=404,
            )
        return JSONResponse({"success": True})

    @r.post("/entities/discover")
    async def entities_discover(lookback_seconds: int = 86400):
        """
        Trigger a manual auto-discovery pass over the last N seconds of mail.
        Auto-runs after each IMAP sync; this endpoint is for one-shot backfill.
        """
        store, err = _entity_store_or_404()
        if err is not None:
            return err
        try:
            lookback_seconds = max(60, min(int(lookback_seconds), 365 * 86400))
        except (TypeError, ValueError):
            lookback_seconds = 86400
        try:
            n = await store.auto_discover_entities_from_recent(
                lookback_seconds=lookback_seconds
            )
        except Exception as e:
            logger.exception("entities discover failed")
            return JSONResponse(
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                status_code=500,
            )
        return JSONResponse({"success": True, "discovered_or_refreshed": n})

    return r
