"""Shared Jinja2 environment for server-rendered pages."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.web.auth import csrf_input

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Every mutating admin form needs this, so it is a global rather than something
# each route has to remember to put in its context.
templates.env.globals["csrf_input"] = csrf_input
