"""
Agent chat routes.

POST /ask              — streaming SSE (default) or full JSON (stream=false)
GET  /session/{id}     — session status
"""

import asyncio
import dataclasses
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

_MAX_TITLE_CHARS = 120

logger = logging.getLogger(__name__)

router = APIRouter()


_MAX_QUESTION_CHARS = 4000
_MAX_SESSION_ID_CHARS = 128


class AskRequest(BaseModel):
    question:   str = Field(..., min_length=1, max_length=_MAX_QUESTION_CHARS)
    session_id: Optional[str] = Field(default=None, max_length=_MAX_SESSION_ID_CHARS)
    # When true, the agent is asked to produce chart spec(s) alongside a
    # short text explanation. When false (default), it answers with text.
    visualise:  bool = False

    @field_validator("question")
    @classmethod
    def _strip_question(cls, v: str) -> str:
        return v.strip()


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=_MAX_TITLE_CHARS)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return v.strip()


def create_agent_router(orchestrator) -> APIRouter:
    """
    Factory: returns a FastAPI router wired to the given orchestrator instance.
    Mount at app root (no prefix) — frontend calls POST /ask.
    """
    r = APIRouter()

    @r.post("/ask")
    async def ask(req: AskRequest, stream: bool = Query(default=True)):
        question = req.question.strip()
        if not question:
            return JSONResponse({"error": "Please ask a question."}, status_code=400)

        logger.info(
            f"[Agent] question={question!r}  "
            f"session={req.session_id!r}  stream={stream}  "
            f"visualise={req.visualise}"
        )

        if not stream:
            result = await orchestrator.ask(question, req.session_id)
            return JSONResponse(dataclasses.asdict(result))

        async def event_stream():
            # Heartbeat comment every 15s keeps proxies (and some browsers)
            # from timing out an idle SSE connection while the agent is
            # still thinking or waiting on a rate-limit countdown.
            queue: asyncio.Queue = asyncio.Queue()
            DONE = object()

            async def producer():
                try:
                    async for event in orchestrator.ask_stream(
                        question, req.session_id, visualise=req.visualise
                    ):
                        await queue.put(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("[Agent] Unhandled error in SSE stream")
                    await queue.put({
                        "type": "error",
                        "message": "The agent encountered an unexpected error. Please try again.",
                        "detail": type(exc).__name__,
                    })
                finally:
                    await queue.put(DONE)

            prod_task = asyncio.create_task(producer())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # SSE comment — ignored by EventSource but keeps the
                        # connection warm through intermediaries.
                        yield ": keepalive\n\n"
                        continue
                    if item is DONE:
                        break
                    yield f"data: {json.dumps(item)}\n\n"
            except asyncio.CancelledError:
                prod_task.cancel()
                raise
            finally:
                if not prod_task.done():
                    prod_task.cancel()
                    try:
                        await prod_task
                    except (asyncio.CancelledError, Exception):
                        pass
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @r.get("/sessions")
    async def sessions_list(limit: int = Query(default=200, ge=1, le=1000)):
        """
        List recent sessions newest-first for the sidebar. Lightweight shape:
        no full messages, no full display log — just title, preview, counts,
        and timestamps. Safe to call on every page load.
        """
        rows = orchestrator._sessions.list_sessions(limit=limit)
        return JSONResponse({"sessions": rows, "count": len(rows)})

    @r.get("/session/{session_id}")
    async def session_status(session_id: str):
        store = orchestrator._sessions
        messages = store.get_messages(session_id)
        return JSONResponse({
            "session_id":     session_id,
            "exists":         store.exists(session_id),
            "title":          store.get_title(session_id),
            "message_count":  len(messages),
            "total_sessions": store.session_count(),
        })

    @r.get("/session/{session_id}/log")
    async def session_log(session_id: str):
        """
        Return the UI-facing transcript for this session — user/ai turns with
        timestamps, meta badges, and any chart specs. This is what the frontend
        replays when the user clicks a session in the sidebar.
        """
        store = orchestrator._sessions
        if not store.exists(session_id):
            return JSONResponse(
                {"error": "Session not found or expired.", "session_id": session_id},
                status_code=404,
            )
        return JSONResponse({
            "session_id": session_id,
            "title":      store.get_title(session_id),
            "entries":    store.get_display_log(session_id),
        })

    @r.patch("/session/{session_id}")
    async def session_rename(session_id: str, req: RenameRequest):
        """Rename a session (title shown in the sidebar)."""
        ok = orchestrator._sessions.rename(session_id, req.title)
        if not ok:
            return JSONResponse(
                {"error": "Session not found.", "session_id": session_id},
                status_code=404,
            )
        return JSONResponse({
            "success":    True,
            "session_id": session_id,
            "title":      orchestrator._sessions.get_title(session_id),
        })

    @r.delete("/session/{session_id}")
    async def session_clear(session_id: str):
        """Delete a session permanently (removes it from the sidebar)."""
        orchestrator._sessions.destroy(session_id)
        return JSONResponse({"success": True, "session_id": session_id})

    return r
