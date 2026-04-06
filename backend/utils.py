"""Shared utilities: JSON serialization helper."""
import json
from decimal import Decimal
from datetime import datetime, date
from fastapi.responses import Response


def json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return str(obj)
    raise TypeError(f"Not serializable: {type(obj).__name__}")


def safe_json(data) -> Response:
    return Response(content=json.dumps(data, default=json_default), media_type="application/json")
