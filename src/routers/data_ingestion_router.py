import base64
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Body, Response, Request
from sqlalchemy.orm import Session
from src.core.database import get_db
from src.schemas.data_ingestion import ExtractionRequest, JobStatusResponse, SingleItemIngestionRequest, StagingRevisionRequest, StagingRevisionResponse, PhotoRetryRequest
from src.services import data_ingestion_service, staging_revision_service, photo_service, blueprint_revision_service
from src.repositories import staging_repo
from src.core.logger import get_logger

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
        anomalies = blueprint_revision_service.get_warehouse_anomalies(db)
        if anomalies.total_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Impossibile importare nuovi articoli: sono presenti {anomalies.total_count} anomalie a magazzino non risolte. "
                    f"Risolvere o chiudere le anomalie prima di procedere con l'importazione."
                ),
            )
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
async def confirm_extraction(
    job_id: str,
    background_tasks: BackgroundTasks,
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
        anomalies = blueprint_revision_service.get_warehouse_anomalies(db)
        if anomalies.total_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Impossibile confermare l'importazione: sono presenti {anomalies.total_count} anomalie a magazzino non risolte. "
                    f"Risolvere o chiudere le anomalie prima di procedere con l'inserimento a magazzino."
                ),
            )
        data_ingestion_service.check_job_status(db, job_id)
        await data_ingestion_service.confirm_and_persist_staging(db, job_id, background_tasks)
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
        anomalies = blueprint_revision_service.get_warehouse_anomalies(db)
        if anomalies.total_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Impossibile importare l'articolo: sono presenti {anomalies.total_count} anomalie a magazzino non risolte. "
                    f"Risolvere o chiudere le anomalie prima di procedere con l'importazione."
                ),
            )
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


@router.post("/photo-retry/{job_id}", status_code=status.HTTP_200_OK)
async def retry_photo(job_id: str, request: PhotoRetryRequest, db: Session = Depends(get_db)):
    """
    Retries photo search for a specific item, or sets a manual URL.
    """
    try:
        updated_proposal = await photo_service.retry_photo_search(db, job_id, request)
        return {"status": "success", "data": updated_proposal}
    except HTTPException as e:
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )


@router.post("/staging/{job_id}/photo-upload/{item_id}", status_code=status.HTTP_200_OK)
async def upload_staging_photo(
    job_id: str,
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Upload an image for a specific item in the staging area.
    Converts image to data URL and updates photo_proposals in staging job data.
    """
    try:
        job = staging_repo.get_job(db, job_id)
        if not job or not job.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job non trovato")

        content = await request.body()
        content_type = "image/jpeg"
        header_ct = request.headers.get("content-type")
        if header_ct and header_ct.startswith("image/"):
            content_type = header_ct

        if not content:
            raise HTTPException(status_code=400, detail="Nessun file o contenuto immagine inviato")

        b64 = base64.b64encode(content).decode("ascii")
        data_url = f"data:{content_type};base64,{b64}"

        proposals = job.data.get("photo_proposals", [])
        proposal_idx = next((i for i, p in enumerate(proposals) if p.get("item_id") == item_id), None)

        if proposal_idx is not None:
            proposals[proposal_idx]["photo_url"] = data_url
            proposals[proposal_idx]["photo_source"] = "manual_upload"
            target_proposal = proposals[proposal_idx]
        else:
            target_proposal = {
                "item_id": item_id,
                "photo_url": data_url,
                "photo_source": "manual_upload",
            }
            proposals.append(target_proposal)

        job.data["photo_proposals"] = proposals
        staging_repo.update_job_data(db, job_id, job.data)

        return {"status": "success", "data": target_proposal}
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.log_execution("data_ingestion_router", "staging_photo_upload_err", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc) if str(exc).strip() else "unknown server error",
        )


@router.get("/photos/{photo_id}", response_class=Response)
def serve_photo(photo_id: str, db: Session = Depends(get_db)):
    """
    Serves a photo from the database.
    """
    try:
        return photo_service.serve_photo(db, photo_id)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e) if str(e).strip() else "unknown server error"
        )
