import uuid
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from src.infrastructure.file_storage import save_staging_file, read_staging_file
from src.agents.langgraph_engine import mock_extract_ddt_data
from src.schemas.data_ingestion import StagingConfirmationRequest
from src.services.pim_service import get_or_create_product
from src.services.wms_service import register_inbound_movement

class InvalidFileFormatException(ValueError):
    """Raised when an unsupported or invalid file format is provided."""
    pass

def validate_pdf_file(file_path: str) -> bool:
    """Verifies that the target file is a PDF."""
    return file_path.lower().endswith(".pdf")
    
    
def process_and_stage_pdf(file_path: str) -> str:
    """
    Extracts data using the mock AI agent and saves it to a staging file.
    Returns the job_id.
    """

    job_id = str(uuid.uuid4())
    extracted_data = mock_extract_ddt_data(file_path)
    
    # Add job_id to the data
    extracted_data["job_id"] = job_id
    
    save_staging_file(job_id, extracted_data)
    return job_id

def retrieve_staged_data(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Reads staging data and deletes the file.
    """
    return read_staging_file(job_id)

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
