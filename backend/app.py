"""OptiFlow AI — application entry point."""

import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.routes import setup, query
from backend.services.pipeline import startup_permission_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="OptiFlow AI")

_FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")

app.include_router(setup.router)
app.include_router(query.router)


@app.on_event("startup")
async def _startup():
    await startup_permission_check()
