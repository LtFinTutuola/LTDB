import os
import uuid
from typing import Dict, Any, Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_ingestion_agent import DataIngestionAgent
from src.repositories.pim_repo import category_repo
from src.schemas.data_ingestion import StagingConfirmationRequest
from src.services.pim_service import get_or_create_product
from src.services.wms_service import register_inbound_movement
from src.repositories import staging_repo
from src.models.staging import JobStatus

class InvalidFileFormatException(ValueError):
    """Raised when an unsupported or invalid file format is provided."""
    pass

def accept_job(db: Session, file_path: str) -> str:
    """
    Validates file, checks if a job exists for the given file path in ACCEPTED status.
    If yes, raises HTTPException. Otherwise creates a new job and returns the job_id.
    """
    if not os.path.exists(file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found on the server filesystem")
        
    if not file_path.lower().endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file format. Only PDF files (.pdf) are supported.")

    existing_job = staging_repo.get_job_by_status_and_path(db, status=JobStatus.ACCEPTED.value, file_path=file_path)
    if existing_job:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A job for this file is already in progress")
        
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db, job_id=job_id, file_path=file_path)
    return job_id

async def process_and_stage_pdf(job_id: str, file_path: str, brand: str) -> None:
    """
    Fetches the brand hierarchy from the DB, runs the DataIngestionAgent,
    and persists the result to the staging area.

    On AgentException: updates the job to ERROR status with the error payload,
    then re-raises so the FastAPI background worker logs the full stack trace.
    """
    with SessionLocal() as db:
        try:
            # 1. Fetch brand hierarchy
            categories = category_repo.get_brand_hierarchy(db, brand)

            # 2. Build input payload for the agent
            input_data = {
                "file_path": file_path,
                "brand": brand,
                "categories": categories,
                "allowed_sex": ["Uomo", "Donna", "Unisex"],
            }

            # 3. Run the agent
            agent = DataIngestionAgent()
            result = await agent.aexecute(input_data)

            # 4. Attach job_id and persist as COMPLETED
            result["job_id"] = job_id
            staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=result)

        except AgentException as exc:
            # Update DB to ERROR so polling clients can observe the failure,
            # then re-raise for the background worker to log the stack trace.
            staging_repo.update_job(
                db,
                job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "output": exc.output},
            )
            raise

def get_job(db: Session, job_id: str) -> Dict[str, Any]:
    """
    Retrieves the job data from the database.
    """
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        
    return {
        "status": JobStatus(job.status).name,
        "data": job.data
    }

def check_job_status(db: Session, job_id: str) -> None:
    """
    Checks the status of a job.
    Raises exception if not found or not completed.
    """
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job is not yet completed")

def confirm_and_persist_staging(db: Session, staging_data: StagingConfirmationRequest) -> None:
    """
    Orchestrates the creation of missing products and registration of warehouse movements.
    """
    try:
        if not staging_data.items:
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items to confirm")
             
        for item in staging_data.items:
            # 1. Ensure product exists
            blueprint_id = get_or_create_product(
                db=db,
                supplier_code=item.supplier_code,
                description=item.description,
                commit_changes=False
            )
            
            # 2. Register warehouse movement
            register_inbound_movement(
                db=db,
                job_id=staging_data.job_id,
                blueprint_id=blueprint_id,
                quantity=item.quantity,
                commit_changes=False
            )
            
        # Commit at the end of the unit of work
        db.commit()
    finally:
        try:
            staging_repo.delete_job(db, staging_data.job_id)
        except Exception:
            pass
