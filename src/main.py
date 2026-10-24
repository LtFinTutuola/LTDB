import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from src.core.config import settings
from src.core.logger import get_logger
from src.core.auth import get_current_user, require_admin, bootstrap_auth, LoginRedirect
from src.core.access_log_middleware import AccessLogMiddleware
from src.core.database import SessionLocal

from src.routers.auth_router import router as auth_router
from src.routers.users_router import router as users_router
from src.routers.data_ingestion_router import router as data_ingestion_router
from src.routers.heuristic_router import router as heuristic_router
from src.routers.catalog_router import router as catalog_router
from src.routers.excel_synchronization_router import (
    edge_router as excel_synchronization_edge_router,
    router as excel_synchronization_router,
)
from src.routers.sales_router import router as sales_router
from src.routers.settings_router import router as settings_router
from src.routers.frontend_router import router as frontend_router

# Initialize the logger at startup
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.log_execution("main", "startup", "ok", message="Starting up application services...")
    db = SessionLocal()
    try:
        bootstrap_auth(db)
    except Exception as exc:
        logger.log_execution("main", "bootstrap_auth_error", "err", exc=exc)
    finally:
        db.close()
    yield
    # Shutdown
    logger.log_execution("main", "shutdown", "ok", message="Shutting down application services...")


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# Access log middleware
app.add_middleware(AccessLogMiddleware)

# Enable CORS for mobile devices, LAN IPs, and web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(LoginRedirect)
async def login_redirect_handler(request: Request, exc: LoginRedirect):
    """
    Catches LoginRedirect exceptions from unauthenticated HTML page requests
    and redirects the user to /login with a 303 status code.
    """
    next_path = request.url.path
    response = RedirectResponse(url=f"/login?next={next_path}", status_code=303)
    response.delete_cookie(key=settings.AUTH_COOKIE_NAME, path="/")
    return response


# Static files (JS, CSS)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Public Auth router (login, logout, me, change-password)
app.include_router(auth_router)

# Public Edge synchronization router for cash register script (agent-config, push-batch)
app.include_router(excel_synchronization_edge_router)

# Protected API routers — require valid authenticated user
auth_dependencies = [Depends(get_current_user)]
app.include_router(data_ingestion_router, dependencies=auth_dependencies)
app.include_router(heuristic_router, dependencies=auth_dependencies)
app.include_router(catalog_router, dependencies=auth_dependencies)
app.include_router(excel_synchronization_router, dependencies=auth_dependencies)
app.include_router(sales_router, dependencies=auth_dependencies)

# Admin-only API routers
app.include_router(settings_router, dependencies=[Depends(require_admin)])
app.include_router(users_router)

# Frontend SPA and HTML templates (contains per-route auth dependencies)
app.include_router(frontend_router)

if __name__ == "__main__":
    uvicorn.run("src.main:app", host=settings.API_HOST, port=settings.API_PORT, reload=True)
