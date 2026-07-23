import uuid
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.agents.langgraph_engine import mock_extract_ddt_data
from src.schemas.data_ingestion import StagingConfirmationRequest
from src.services.pim_service import get_or_create_product
from src.services.wms_service import register_inbound_movement
from src.repositories import staging_repo
from src.models.staging import JobStatus

class InvalidFileFormatException(ValueError):
    """Raised when an unsupported or invalid file format is provided."""
    pass

def validate_pdf_file(file_path: str) -> bool:
    """Verifies that the target file is a PDF."""
    return file_path.lower().endswith(".pdf")
    
def accept_job(db: Session, file_path: str) -> Optional[str]:
    """
    Checks if a job exists for the given file path in ACCEPTED status.
    If yes, returns None. Otherwise creates a new job and returns the job_id.
    """
    existing_job = staging_repo.get_job_by_status_and_path(db, status=JobStatus.ACCEPTED.value, file_path=file_path)
    if existing_job:
        return None
        
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db, job_id=job_id, file_path=file_path)
    return job_id

async def process_and_stage_pdf(job_id: str, file_path: str) -> None:
    """
    Extracts data using the mock AI agent and updates the staging DB record.
    Runs asynchronously in the background.
    """
    extracted_data = await mock_extract_ddt_data(file_path)
    
    # Add job_id to the data
    extracted_data["job_id"] = job_id
    
    with SessionLocal() as db:
        staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=extracted_data)

def get_job(db: Session, job_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves the job data from the database.
    """
    job = staging_repo.get_job(db, job_id)
    if not job:
        return None
        
    return {
        "status": job.status,
        "data": job.data
    }

def confirm_and_persist_staging(db: Session, staging_data: StagingConfirmationRequest) -> bool:
    """
    Orchestrates the creation of missing products and registration of warehouse movements.
    """
    try:
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
        return True
    except Exception as e:
        db.rollback()
        raise e
