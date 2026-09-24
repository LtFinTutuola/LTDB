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

# Initialize the logger at startup
logger = get_logger()

app = FastAPI(title=settings.PROJECT_NAME)

# Static files (JS, CSS)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# API routers — must be registered before the catch-all frontend route
app.include_router(data_ingestion_router)
app.include_router(heuristic_router)
app.include_router(catalog_router)

# Frontend SPA (catch-all GET /)
app.include_router(frontend_router)

if __name__ == "__main__":
    uvicorn.run("src.main:app", host=settings.API_HOST, port=settings.API_PORT, reload=True)
