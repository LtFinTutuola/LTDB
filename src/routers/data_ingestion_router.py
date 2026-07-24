from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.data_ingestion import ExtractionRequest, JobStatusResponse, StagingConfirmationRequest
from src.services import data_ingestion_service

router = APIRouter(prefix="/api/v1/ingestion", tags=["Data Ingestion"])

@router.post("/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_data(request: ExtractionRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Accepts a file path, checks if it exists and is a PDF, and triggers the extraction in the background.
    """
    try:
        job_id = data_ingestion_service.accept_job(db, request.file_path)
        background_tasks.add_task(
            data_ingestion_service.process_and_stage_pdf,
            job_id,
            request.file_path,
            request.brand,
        )
        return {
            "job_id": job_id,
            "message": "Starting to process the file"
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )

@router.get("/extract/{job_id}", response_model=JobStatusResponse)
def get_extracted_data(job_id: str, db: Session = Depends(get_db)):
    """
    Retrieves the extracted staging data and status.
    """
    try:
        return data_ingestion_service.get_job(db, job_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )

@router.post("/confirm", status_code=status.HTTP_200_OK)
def confirm_extraction(request: StagingConfirmationRequest, db: Session = Depends(get_db)):
    """
    Confirms the extraction and persists to database (PIM and WMS).
    """
    try:
        data_ingestion_service.check_job_status(db, request.job_id)
        data_ingestion_service.confirm_and_persist_staging(db, request)
        return {"status": "success", "message": "Data successfully ingested"}
    except HTTPException as e:
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )
