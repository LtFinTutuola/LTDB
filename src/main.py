import uvicorn
from fastapi import FastAPI
from src.core.config import settings
from src.routers.data_ingestion_router import router as data_ingestion_router

app = FastAPI(title=settings.PROJECT_NAME)

app.include_router(data_ingestion_router)

if __name__ == "__main__":
    uvicorn.run("src.main:app", host=settings.API_HOST, port=settings.API_PORT, reload=True)
