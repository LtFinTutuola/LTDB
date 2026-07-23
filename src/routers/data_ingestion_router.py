import os
from fastapi import APIRouter, Depends, HTTPException, Response, status, BackgroundTasks
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.data_ingestion import ExtractionRequest, DdtExtractionResponse, StagingConfirmationRequest
from src.services import data_ingestion_service

router = APIRouter(prefix="/api/v1/ingestion", tags=["Data Ingestion"])

@router.post("/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_data(request: ExtractionRequest, response: Response, background_tasks: BackgroundTasks):
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
        job_id = data_ingestion_service.process_and_stage_pdf(request.file_path)
        if job_id:
            status_code = status.HTTP_202_ACCEPTED
            message = "Starting to process the file"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            job_id = ""
            message = "Failed to process the file"

    response.status_code = status_code
    return {
        "status_code": status_code,
        "job_id": job_id,
        "message": message
    }
    

@router.get("/extract/{job_id}", response_model=DdtExtractionResponse)
def get_extracted_data(job_id: str):
    """
    Retrieves the extracted staging data and deletes the temporary file.
    """
    data = data_ingestion_service.retrieve_staged_data(job_id)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or already confirmed")
        
    return data

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
