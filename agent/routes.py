"""
Agent API routes.

POST /agent/ask            — streaming SSE by default (stream=true query param)
POST /agent/ask?stream=false — non-streaming, returns full AgentResponse JSON
GET  /agent/session/{id}   — session status
"""

import dataclasses
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agent.orchestrator import AgentOrchestrator

logger = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: str
    session_id: Optional[str] = None


def create_agent_router(orchestrator: AgentOrchestrator) -> APIRouter:
    """
    Factory: returns a FastAPI router wired to the given orchestrator instance.

    Usage in app.py:
        app.include_router(create_agent_router(orchestrator), prefix="/agent")
    """
    router = APIRouter()

    @router.post("/ask")
    async def ask(req: AskRequest, stream: bool = Query(default=True)):
        question = req.question.strip()
        if not question:
            return JSONResponse({"error": "Please ask a question."}, status_code=400)

        logger.info(
            f"[Agent] question={question!r}  "
            f"session={req.session_id!r}  stream={stream}"
        )

        if not stream:
            # Non-streaming: collect all events, return AgentResponse JSON
            result = await orchestrator.ask(question, req.session_id)
            return JSONResponse(dataclasses.asdict(result))

        # Streaming: Server-Sent Events
        async def event_stream():
            try:
                async for event in orchestrator.ask_stream(
                    question, req.session_id
                ):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:
                logger.exception("[Agent] Unhandled error in SSE stream")
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            finally:
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control":    "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/session/{session_id}")
    async def session_status(session_id: str):
        messages = orchestrator._sessions.get_messages(session_id)
        return JSONResponse({
            "session_id":     session_id,
            "exists":         orchestrator._sessions.exists(session_id),
            "message_count":  len(messages),
            "total_sessions": orchestrator._sessions.session_count(),
        })

    return router
