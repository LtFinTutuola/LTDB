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
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

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
    brands = db.query(Brand).order_by(func.lower(Brand.name)).all()
    brands_json = json.dumps([{"id": b.id, "name": b.name} for b in brands])

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"brands_json": brands_json, "active_page": "dashboard"},
    )


@router.get("/vendite", include_in_schema=False)

def vendite_page(request: Request):
    """
    Serve the Sales Reconciliation tabular page.
    """
    return templates.TemplateResponse(
        request=request,
        name="vendite.html",
        context={"active_page": "vendite"},
    )


@router.get("/brands", include_in_schema=False)
def brands_page(request: Request):
    """
    Serve the Brand management CRUD page.
    """
    return templates.TemplateResponse(
        request=request,
        name="brands.html",
        context={"active_page": "brands"},
    )


@router.get("/categories", include_in_schema=False)
def categories_page(request: Request):
    """
    Serve the Category hierarchy management CRUD page.
    """
    return templates.TemplateResponse(
        request=request,
        name="categories.html",
        context={"active_page": "categories"},
    )


@router.get("/categorie", include_in_schema=False)
def categorie_redirect():
    """
    Redirect /categorie to /categories.
    """
    return RedirectResponse(url="/categories", status_code=307)


@router.get("/settings", include_in_schema=False)
def settings_page(request: Request):
    """
    Serve the Settings configuration page.
    """
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"active_page": "settings"},
    )


@router.get("/impostazioni", include_in_schema=False)
def impostazioni_redirect():
    """
    Redirect /impostazioni to /settings.
    """
    return RedirectResponse(url="/settings", status_code=307)


