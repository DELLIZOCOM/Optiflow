"""Shared Jinja2 templates instance pointing at the frontend/ directory."""
import os
from fastapi.templating import Jinja2Templates

_FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
templates = Jinja2Templates(directory=_FRONTEND)
