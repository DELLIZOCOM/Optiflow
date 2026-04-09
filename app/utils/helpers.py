"""Shared utilities: JSON serialization, common helpers."""

import json
from datetime import date, datetime
from decimal import Decimal

from fastapi.responses import Response


def json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return str(obj)
    raise TypeError(f"Not serializable: {type(obj).__name__}")


def safe_json(data) -> Response:
    """Return a FastAPI Response with JSON-serialized data, handling Decimal and datetime."""
    return Response(
        content=json.dumps(data, default=json_default),
        media_type="application/json",
    )


def sanitize_name(name: str) -> str:
    """Convert a string to a safe identifier (lowercase, underscores)."""
    import re
    return re.sub(r"[^\w]", "_", name.lower()).strip("_") or "source"
