from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Body
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.data_ingestion import ExtractionRequest, JobStatusResponse, SingleItemIngestionRequest, StagingRevisionRequest, StagingRevisionResponse
from src.services import data_ingestion_service, staging_revision_service
from src.core.logger import get_logger
import time

logger = get_logger()

router = APIRouter(prefix="/api/v1/ingestion", tags=["Data Ingestion"])

@router.post("/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_data(request: ExtractionRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Accepts a file path, checks if it exists and is a PDF, and triggers the extraction in the background.
    """
    logger.log_execution("data_ingestion_router", "request_received", "ok", 
                         path="/api/v1/ingestion/extract", method="POST",
                         request_body=request.model_dump())
    start_time = time.time()
    try:
        job_id = data_ingestion_service.accept_job(db, request.file_path)
        background_tasks.add_task(
            data_ingestion_service.process_and_stage_pdf,
            job_id,
            request.file_path,
            request.brand_id,
        )
        response_body = {
            "job_id": job_id,
            "message": "Starting to process the file"
        }
        latency_ms = int((time.time() - start_time) * 1000)
        logger.log_execution("data_ingestion_router", "response_dispatched", "ok",
                             path="/api/v1/ingestion/extract", status_code=status.HTTP_202_ACCEPTED,
                             job_id=job_id, latency_ms=latency_ms, response_body=response_body)
        return response_body
    except HTTPException as e:
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=e.status_code, detail=e.detail)
        raise e
    except Exception as e:
        db.rollback()
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )

@router.get("/extract/{job_id}", response_model=JobStatusResponse)
def get_extracted_data(job_id: str, db: Session = Depends(get_db)):
    """
    Retrieves the extracted staging data and status.
    """
    logger.log_execution("data_ingestion_router", "request_received", "ok", 
                         path=f"/api/v1/ingestion/extract/{job_id}", method="GET",
                         job_id=job_id)
    start_time = time.time()
    try:
        result = data_ingestion_service.get_job(db, job_id)
        latency_ms = int((time.time() - start_time) * 1000)
        logger.log_execution("data_ingestion_router", "response_dispatched", "ok",
                             path=f"/api/v1/ingestion/extract/{job_id}", status_code=status.HTTP_200_OK,
                             job_id=job_id, latency_ms=latency_ms, response_body=result)
        return result
    except HTTPException as e:
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=e.status_code, detail=e.detail)
        raise e
    except Exception as e:
        db.rollback()
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )

@router.put("/staging/{job_id}", response_model=StagingRevisionResponse, status_code=status.HTTP_200_OK)
def revise_staging(
    job_id: str,
    request: StagingRevisionRequest,
    db: Session = Depends(get_db)
):
    """
    Revises the staged extraction data before confirmation (human-in-the-loop).
    """
    logger.log_execution("data_ingestion_router", "request_received", "ok",
                         path=f"/api/v1/ingestion/staging/{job_id}", method="PUT",
                         job_id=job_id, request_body=request.model_dump())
    start_time = time.time()
    try:
        updated_data = staging_revision_service.revise_staging_data(db, job_id, request.operations)
        response_body = {
            "status": "success",
            "data": updated_data
        }
        latency_ms = int((time.time() - start_time) * 1000)
        logger.log_execution("data_ingestion_router", "response_dispatched", "ok",
                             path=f"/api/v1/ingestion/staging/{job_id}", status_code=status.HTTP_200_OK,
                             job_id=job_id, latency_ms=latency_ms)
        return response_body
    except HTTPException as e:
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=e.status_code, detail=e.detail)
        raise e
    except Exception as e:
        db.rollback()
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )

@router.post("/confirm/{job_id}", status_code=status.HTTP_200_OK)
def confirm_extraction(
    job_id: str,
    db: Session = Depends(get_db)
):
    """
    Confirms the extraction and persists to database (PIM and WMS).
    Always reads from the staging area data.
    """
    logger.log_execution("data_ingestion_router", "request_received", "ok", 
                         path=f"/api/v1/ingestion/confirm/{job_id}", method="POST",
                         job_id=job_id)
    start_time = time.time()
    try:
        data_ingestion_service.check_job_status(db, job_id)
        data_ingestion_service.confirm_and_persist_staging(db, job_id)
        response_body = {"status": "success", "message": "Data successfully ingested"}
        latency_ms = int((time.time() - start_time) * 1000)
        logger.log_execution("data_ingestion_router", "response_dispatched", "ok",
                             path=f"/api/v1/ingestion/confirm/{job_id}", status_code=status.HTTP_200_OK,
                             job_id=job_id, latency_ms=latency_ms, response_body=response_body)
        return response_body
    except HTTPException as e:
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=e.status_code, detail=e.detail)
        raise e
    except Exception as e:
        db.rollback()
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )

@router.post("/single-item", status_code=status.HTTP_202_ACCEPTED)
async def process_single_item(
    request: SingleItemIngestionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Directly processes a single item ingestion through the staging area asynchronously.
    """
    logger.log_execution("data_ingestion_router", "request_received", "ok", 
                         path="/api/v1/ingestion/single-item", method="POST",
                         request_body=request.model_dump())
    start_time = time.time()
    try:
        job_id = data_ingestion_service.accept_single_item_job(db, request)
        background_tasks.add_task(
            data_ingestion_service.process_and_stage_single_item,
            job_id,
            request
        )
        response_body = {
            "job_id": job_id,
            "message": "Starting to process the single item"
        }
        latency_ms = int((time.time() - start_time) * 1000)
        logger.log_execution("data_ingestion_router", "response_dispatched", "ok",
                             path="/api/v1/ingestion/single-item", status_code=status.HTTP_202_ACCEPTED,
                             latency_ms=latency_ms, response_body=response_body)
        return response_body
    except HTTPException as e:
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=e.status_code, detail=e.detail)
        raise e
    except Exception as e:
        db.rollback()
        logger.log_execution("data_ingestion_router", "exception_caught", "err",
                             exc=e, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )
