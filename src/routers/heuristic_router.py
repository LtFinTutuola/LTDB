"""
heuristic_router.py
-------------------
Dedicated API endpoints for the heuristic deduction flow.
Completely decoupled from the ingestion pipeline.

Endpoints:
  POST   /api/v1/brands/{brand_id}/heuristic             - Trigger deduction
  GET    /api/v1/brands/{brand_id}/heuristic/{job_id}     - Poll job status
  POST   /api/v1/brands/{brand_id}/heuristic/{job_id}/confirm - Confirm heuristic
"""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.services import heuristic_service
from src.schemas.data_ingestion import (
    HeuristicDeductionRequest,
    HeuristicJobStatusResponse,
)

router = APIRouter(prefix="/api/v1/brands", tags=["Heuristic Deduction"])


@router.post("/{brand_id}/heuristic")
def trigger_heuristic_deduction(
    brand_id: str,
    request: HeuristicDeductionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Trigger heuristic deduction for a brand using a sample DDT."""
    job_id = heuristic_service.accept_heuristic_job(
        db, brand_id=brand_id, file_path=request.file_path
    )
    background_tasks.add_task(
        heuristic_service.process_heuristic,
        job_id=job_id,
        brand_id=brand_id,
        file_path=request.file_path,
    )
    return {"job_id": job_id}


@router.get("/{brand_id}/heuristic/{job_id}", response_model=HeuristicJobStatusResponse)
def get_heuristic_status(
    brand_id: str,
    job_id: str,
    db: Session = Depends(get_db),
):
    """Poll the status and result of a heuristic deduction job."""
    return heuristic_service.get_heuristic_job(db, job_id)


@router.post("/{brand_id}/heuristic/{job_id}/confirm")
def confirm_heuristic(
    brand_id: str,
    job_id: str,
    db: Session = Depends(get_db),
):
    """Confirm a completed heuristic proposal and persist it to the Brand record."""
    heuristic_service.confirm_heuristic(db, brand_id=brand_id, job_id=job_id)
    return {"status": "confirmed", "brand_id": brand_id}
