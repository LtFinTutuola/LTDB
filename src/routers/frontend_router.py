"""
src/routers/frontend_router.py
-------------------------------
Serves the HTML shell templates for the LTDB application.
Enforces authentication on all views and role restrictions on /settings.
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.core.database import get_db
from src.core.config import settings
from src.core.security import decode_access_token
from src.core.auth import get_current_user, require_admin
from src.models.pim import Brand
from src.models.users import User

router = APIRouter(tags=["Frontend"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(
    directory=str(_TEMPLATES_DIR),
    context_processors=[lambda request: {"current_user": getattr(request.state, "user", None)}],
)


@router.get("/login", include_in_schema=False)
def login_page(request: Request, db: Session = Depends(get_db)):
    """
    Renders the login page, or redirects to /vendite if a valid session exists.
    """
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if token:
        try:
            payload = decode_access_token(token)
            user = db.query(User).filter(User.id == payload.get("sub")).first()
            if user and user.is_active and user.token_version == payload.get("tv"):
                return RedirectResponse(url="/vendite", status_code=303)
        except Exception:
            pass

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={},
    )


@router.get("/", include_in_schema=False)
def index_redirect():
    """
    Start page of the application: redirects root / to /vendite.
    """
    return RedirectResponse(url="/vendite", status_code=307)


@router.get("/magazzino", include_in_schema=False)
def magazzino_page(
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Serve the Magazzino / catalog page.
    Injects window.__LTDB_BRANDS__ with the current brand list.
    """
    brands = db.query(Brand).order_by(func.lower(Brand.name)).all()
    brands_json = json.dumps([{"id": b.id, "name": b.name} for b in brands])

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"brands_json": brands_json, "active_page": "magazzino"},
    )


@router.get("/vendite", include_in_schema=False)
def vendite_page(
    request: Request,
    _user=Depends(get_current_user),
):
    """
    Serve the Sales Reconciliation tabular page.
    """
    return templates.TemplateResponse(
        request=request,
        name="vendite.html",
        context={"active_page": "vendite"},
    )


@router.get("/brands", include_in_schema=False)
def brands_page(
    request: Request,
    _user=Depends(get_current_user),
):
    """
    Serve the Brand management page.
    """
    return templates.TemplateResponse(
        request=request,
        name="brands.html",
        context={"active_page": "brands"},
    )


@router.get("/categories", include_in_schema=False)
def categories_page(
    request: Request,
    _user=Depends(get_current_user),
):
    """
    Serve the Category hierarchy management page.
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
def settings_page(
    request: Request,
    _admin=Depends(require_admin),
):
    """
    Serve the Settings & User Management configuration page (Admin only).
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
