from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from datetime import date
import time

from src.core.database import get_db
from src.core.config import settings
from src.services.excel_ingestion_service import ingest_daily_sales
from src.core.logger import get_logger

logger = get_logger()

router = APIRouter(prefix="/api/v1/excel-synchronization", tags=["Excel Synchronization"])

@router.post("/poll-rows", status_code=status.HTTP_200_OK)
def poll_rows(
    file_path: Optional[str] = Body(None),
    target_date: Optional[date] = Body(None),
    db: Session = Depends(get_db)
):
    """
    Manually triggers Excel ingestion for testing purposes, bypassing the polling loop.
    """
    path_to_use = file_path or settings.EXCEL_FILE_PATH
    date_to_use = target_date or date.today()
    
    if not path_to_use:
        raise HTTPException(status_code=400, detail="No excel file path configured or provided")
        
    start_time = time.time()
    try:
        inserted_ids = ingest_daily_sales(db, path_to_use, date_to_use)
        latency_ms = int((time.time() - start_time) * 1000)
        logger.log_execution("excel_synchronization_router", "poll_rows", "ok", 
                             path="/api/v1/excel-synchronization/poll-rows", latency_ms=latency_ms,
                             inserted_count=len(inserted_ids))
        return {
            "status": "success", 
            "message": f"Processed {len(inserted_ids)} new rows from Excel",
            "inserted_ids": inserted_ids
        }
    except Exception as e:
        db.rollback()
        logger.log_execution("excel_synchronization_router", "poll_rows", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))
