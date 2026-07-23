import os
from fastapi import APIRouter, Depends, HTTPException, Response, status, BackgroundTasks
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.data_ingestion import ExtractionRequest, DdtExtractionResponse, StagingConfirmationRequest, JobStatusResponse
from src.services import data_ingestion_service

router = APIRouter(prefix="/api/v1/ingestion", tags=["Data Ingestion"])

@router.post("/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_data(request: ExtractionRequest, response: Response, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Accepts a file path, checks if it exists and is a PDF, and triggers the extraction in the background.
    """
    if not os.path.exists(request.file_path):
        status_code = status.HTTP_404_NOT_FOUND
        job_id = ""
        message = "File not found on the server filesystem"
    elif not data_ingestion_service.validate_pdf_file(request.file_path):
        status_code = status.HTTP_400_BAD_REQUEST
        job_id = ""
        message = "Invalid file format. Only PDF files (.pdf) are supported."
    else:
        job_id = data_ingestion_service.accept_job(db, request.file_path)
        if job_id:
            background_tasks.add_task(data_ingestion_service.process_and_stage_pdf, job_id, request.file_path)
            status_code = status.HTTP_202_ACCEPTED
            message = "Starting to process the file"
        else:
            status_code = status.HTTP_409_CONFLICT
            job_id = ""
            message = "A job for this file is already in progress"

    response.status_code = status_code
    return {
        "status_code": status_code,
        "job_id": job_id,
        "message": message
    }
    

@router.get("/extract/{job_id}", response_model=JobStatusResponse)
def get_extracted_data(job_id: str, db: Session = Depends(get_db)):
    """
    Retrieves the extracted staging data and status.
    """
    job_info = data_ingestion_service.get_job(db, job_id)
    if not job_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        
    return job_info

@router.post("/confirm", status_code=status.HTTP_200_OK)
def confirm_extraction(request: StagingConfirmationRequest, db: Session = Depends(get_db)):
    """
    Confirms the extraction and persists to database (PIM and WMS).
    """
    try:
        data_ingestion_service.confirm_and_persist_staging(db, request)
        return {"status": "success", "message": "Data successfully ingested"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
