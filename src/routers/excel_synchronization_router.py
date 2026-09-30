from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import date
import time

from src.core.database import get_db
from src.core.config import settings
from src.services import excel_synchronization_service
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
        inserted_ids = excel_synchronization_service.ingest_daily_sales(db, path_to_use, date_to_use)
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


@router.get("/pending-sales", status_code=status.HTTP_200_OK)
def get_pending_sales(
    target_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    """
    Returns a list of all orphan and unprocessable Excel sales records for the specified date.
    If no date is specified, it returns the records for the current date.
    """
    try:
        sales = excel_synchronization_service.get_pending_sales(db, target_date)
        return {"status": "success", "data": sales}
    except Exception as e:
        logger.log_execution("excel_synchronization_router", "get_pending_sales", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily-sales", status_code=status.HTTP_200_OK)
def get_daily_sales(
    target_date: date,
    db: Session = Depends(get_db)
):
    """
    Returns a list of all Excel sales records for the specified date.
    """
    try:
        sales = excel_synchronization_service.get_daily_sales(db, target_date)
        return {"status": "success", "data": sales}
    except Exception as e:
        logger.log_execution("excel_synchronization_router", "get_daily_sales", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reconciliation-candidates", status_code=status.HTTP_200_OK)
def get_reconciliation_candidates(
    search_code: str,
    is_exchange: bool = False,
    db: Session = Depends(get_db)
):
    """
    Gets reconciliation candidates grouped by color (FIFO order).
    """
    try:
        candidates = excel_synchronization_service.find_candidates(db, search_code, is_exchange)
        if not candidates:
            raise HTTPException(status_code=404, detail="No reconciliation candidates found")
        return {"status": "success", "data": candidates}
    except HTTPException:
        raise
    except Exception as e:
        logger.log_execution("excel_synchronization_router", "get_reconciliation_candidates", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))

class ReconcileRequest(BaseModel):
    article_id: str

@router.post("/reconcile/{sale_id}", status_code=status.HTTP_200_OK)
def reconcile_sale(
    sale_id: str,
    request: ReconcileRequest,
    db: Session = Depends(get_db)
):
    """
    Reconciles an ExcelSale with a physical WMS Article (dual-write transaction).
    """
    try:
        result = excel_synchronization_service.reconcile_sale(db, sale_id, request.article_id)
        return result
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        db.rollback()
        logger.log_execution("excel_synchronization_router", "reconcile_sale", "err", exc=e)
        raise HTTPException(status_code=500, detail=str(e))

