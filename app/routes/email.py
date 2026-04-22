"""
Email-source setup and status routes.

POST /setup/email/outlook/test     — validate admin-consent credentials without saving
POST /setup/email/outlook          — save credentials and kick off discovery+ingestion
DELETE /setup/email/outlook        — disconnect; optionally wipe the cache
GET  /setup/email/status           — live status for the setup wizard + sidebar
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

    def _existing_outlook_source():
        return source_registry.get(_OUTLOOK_SOURCE_NAME)

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

        # Swap in the live source
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
        return JSONResponse({"success": True, "source_name": _OUTLOOK_SOURCE_NAME})

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

    # ── status ──────────────────────────────────────────────────────────────

    @r.get("/setup/email/status")
    async def outlook_status():
        from app.config import load_outlook_config

        cfg = load_outlook_config()
        configured = bool(cfg and cfg.get("client_id"))
        src = _existing_outlook_source()
        store = src.store if src else None

        if not configured or store is None:
            return JSONResponse({
                "configured":     configured,
                "live":           False,
                "mailboxes":      {"total": 0, "active": 0, "with_errors": 0, "initial_synced": 0},
                "messages_total": 0,
                "last_sync_at":   None,
                "errors":         [],
            })

        mailboxes = store.list_mailboxes(active_only=False)
        total = len(mailboxes)
        active = sum(1 for m in mailboxes if m.get("status") == "active")
        errors = [
            {"mailbox": m.get("account_email"), "error": m.get("last_error")}
            for m in mailboxes if m.get("last_error")
        ]
        initial = sum(1 for m in mailboxes if m.get("initial_synced"))
        msg_total = sum(int(m.get("message_count") or 0) for m in mailboxes)
        last_sync = max(
            (float(m["last_sync_at"]) for m in mailboxes if m.get("last_sync_at")),
            default=None,
        )
        return JSONResponse({
            "configured":     True,
            "live":           True,
            "tenant_id":      cfg.get("tenant_id"),
            "display_name":   cfg.get("tenant_display_name"),
            "mailboxes": {
                "total":          total,
                "active":         active,
                "with_errors":    len(errors),
                "initial_synced": initial,
            },
            "messages_total": msg_total,
            "last_sync_at":   last_sync,
            "errors":         errors[:20],
        })

    return r
