import os
import uuid
from typing import Dict, Any, Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_ingestion_agent import DataIngestionAgent
from src.repositories.pim_repo import category_repo
from src.schemas.data_ingestion import StagingConfirmationRequest, EnrichedItemSchema
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

async def process_and_stage_pdf(job_id: str, file_path: str, brand_id: str) -> None:
    """
    Fetches the brand hierarchy from the DB using brand_id, retrieves the brand name,
    runs the DataIngestionAgent, and persists the result to the staging area.

    On AgentException: updates the job to ERROR status with the error payload,
    then re-raises so the FastAPI background worker logs the full stack trace.
    """
    with SessionLocal() as db:
        try:
            from src.models.pim import Brand
            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{brand_id}' not found in the database.")
            brand_name = brand_obj.name

            # 1. Fetch brand hierarchy
            categories = category_repo.get_brand_hierarchy(db, brand_id)

            # 2. Build input payload for the agent
            input_data = {
                "file_path": file_path,
                "brand": brand_name,
                "categories": categories,
                "allowed_sex": ["Uomo", "Donna", "Unisex"],
            }

            # 3. Run the agent
            agent = DataIngestionAgent()
            result = await agent.aexecute(input_data)

            # 3.5 Post-process categories
            from sqlalchemy import func
            from src.models.pim import Category
            if "items" in result:
                for item in result["items"]:
                    item["id"] = str(uuid.uuid4())
                    for field in ["category", "sub_category"]:
                        if item.get(field):
                            cat_name = item[field]
                            cat = db.query(Category).filter(func.lower(Category.name) == cat_name.lower()).first()
                            if cat:
                                item[field] = {"id": str(cat.id), "description": cat.name}
                            else:
                                item[field] = None

            # 4. Attach job_id and persist as COMPLETED
            result["job_id"] = job_id
            result["brand_id"] = brand_id
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

def confirm_and_persist_staging(
    db: Session, 
    job_id: str, 
    staging_data: Optional[StagingConfirmationRequest] = None
) -> None:
    """
    Orchestrates the creation of missing products and registration of warehouse movements.
    """
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Determine items list: use request items if present, otherwise fallback to stored job data
    items = None
    if staging_data and staging_data.items is not None:
        items = staging_data.items
    elif job.data and "items" in job.data:
        raw_items = job.data.get("items") or []
        items = [EnrichedItemSchema.model_validate(item) for item in raw_items]

    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items to confirm")

    brand_id = job.data.get("brand_id") if job.data else None
    if not brand_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Staged job data is missing brand_id")

    for item in items:
        # Resolve category_id based on sub_category or category objects
        category_id = None
        if item.sub_category and item.sub_category.id:
            category_id = item.sub_category.id
        elif item.category and item.category.id:
            category_id = item.category.id

        if not category_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item is missing a valid category. Confirmation failed."
            )

        # 1. Ensure product exists
        blueprint_id = get_or_create_product(
            db=db,
            brand_id=brand_id,
            description=item.product_short_description or "Unknown Product",
            article_name=item.article_name or item.product_short_description or "Unknown Product",
            extended_description=item.product_extended_description or "",
            tags=item.tags,
            materials=item.materials,
            category_id=category_id,
            commit_changes=False
        )
        
        # 2. Register warehouse movement
        if item.quantity and item.quantity > 0:
            register_inbound_movement(
                db=db,
                job_id=job_id,
                blueprint_id=blueprint_id,
                quantity=item.quantity,
                supplier_code=item.vendor_code,
                ean=item.barcode,
                colors=item.colors,
                commit_changes=False
            )
        
    # Delete job from staging area only upon successful processing
    try:
        staging_repo.delete_job(db, job_id)
    except Exception:
        pass
    # Ensure changes are committed if delete_job didn't commit
    db.commit()
