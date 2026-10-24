import uvicorn
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from src.core.config import settings
from src.core.logger import get_logger
from src.routers.data_ingestion_router import router as data_ingestion_router
from src.routers.heuristic_router import router as heuristic_router
from src.routers.catalog_router import router as catalog_router
from src.routers.frontend_router import router as frontend_router
from src.routers.excel_synchronization_router import router as excel_synchronization_router
from src.routers.sales_router import router as sales_router
from src.routers.settings_router import router as settings_router

# Initialize the logger at startup
logger = get_logger()

from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.log_execution("main", "startup", "ok", message="Starting up application services...")
    yield
    # Shutdown
    logger.log_execution("main", "shutdown", "ok", message="Shutting down application services...")

app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# Enable CORS for mobile devices, LAN IPs, and web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files (JS, CSS)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# API routers — must be registered before the catch-all frontend route
app.include_router(data_ingestion_router)
app.include_router(heuristic_router)
app.include_router(catalog_router)
app.include_router(excel_synchronization_router)
app.include_router(sales_router)
app.include_router(settings_router)


# Frontend SPA (catch-all GET /)
app.include_router(frontend_router)

if __name__ == "__main__":
    uvicorn.run("src.main:app", host=settings.API_HOST, port=settings.API_PORT, reload=True)
