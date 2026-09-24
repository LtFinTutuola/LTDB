"""
src/routers/frontend_router.py
-------------------------------
Serves the single-page application HTML shell.

GET / injects the brand list into the Jinja2 template so the dropdown
is immediately populated without a separate API call on page load.
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.pim import Brand

router = APIRouter(tags=["Frontend"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/", include_in_schema=False)
def index(request: Request, db: Session = Depends(get_db)):
    """
    Serve the SPA index page.
    Injects window.__LTDB_BRANDS__ with the current brand list
    so the import forms can populate their dropdowns immediately.
    """
    brands = db.query(Brand).order_by(Brand.name).all()
    brands_json = json.dumps([{"id": b.id, "name": b.name} for b in brands])
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"brands_json": brands_json},
    )
