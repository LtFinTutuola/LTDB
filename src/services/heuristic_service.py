"""
heuristic_service.py
--------------------
Service layer orchestrating the heuristic deduction flow:
  - Accept and manage heuristic deduction jobs
  - Run DataExtractionAgent → CodesDeductionAgent pipeline
  - Confirm and persist heuristic to Brand record
  - Handle recalculation triggered by heuristic breaks
"""
import os
import uuid
from typing import Dict, Any, Optional, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_extraction_agent import DataExtractionAgent
from src.agents.codes_deduction_agent import CodesDeductionAgent
from src.models.pim import Brand, ArticleBlueprint
from src.models.wms import Article
from src.models.staging import JobStatus, JobType
from src.repositories import staging_repo
from src.core.logger import get_logger

logger = get_logger()


def accept_heuristic_job(db: Session, brand_id: str, file_path: str) -> str:
    """
    Validates brand and file, creates a staging job for heuristic deduction.
    Returns the job_id.
    """
    logger.log_execution("heuristic_service", "accept_heuristic_job_start", "ok",
                         brand_id=brand_id, file_path=file_path)

    # Validate brand exists
    brand = db.query(Brand).filter(Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand ID '{brand_id}' not found in the database."
        )

    # Validate file exists and is PDF
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found on the server filesystem"
        )
    if not file_path.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only PDF files (.pdf) are supported."
        )

    # Check for existing in-progress heuristic job for this brand
    existing_job = staging_repo.get_job_by_status_and_path(
        db, status=JobStatus.ACCEPTED.value, file_path=file_path,
        job_type=JobType.HEURISTIC_DEDUCTION.value
    )
    if existing_job:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A heuristic deduction job for this file is already in progress."
        )

    job_id = str(uuid.uuid4())
    staging_repo.create_job(
        db, job_id=job_id,
        job_type=JobType.HEURISTIC_DEDUCTION.value,
        file_path=file_path
    )
    logger.log_execution("heuristic_service", "accept_heuristic_job_success", "ok", job_id=job_id)
    return job_id


async def process_heuristic(job_id: str, brand_id: str, file_path: str) -> None:
    """
    Background task: runs DataExtractionAgent → CodesDeductionAgent pipeline.
    Stages the result (regex, explanation, examples) without persisting to Brand.
    """
    with SessionLocal() as db:
        try:
            logger.log_execution("heuristic_service", "process_heuristic_start", "ok",
                                 job_id=job_id, brand_id=brand_id)

            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{brand_id}' not found in the database.")
            brand_name = brand_obj.name

            # Stage 1: Extract items from sample DDT
            extraction_agent = DataExtractionAgent()
            logger.log_execution("heuristic_service", "extraction_agent_dispatched", "ok")
            extraction_res = await extraction_agent.aexecute({
                "file_path": file_path,
                "brand": brand_name,
            })
            extracted_items = extraction_res["items"]

            # Extract vendor codes from items
            vendor_codes = _extract_vendor_codes(extracted_items)
            if not vendor_codes:
                raise AgentException(
                    message="No vendor codes found in the sample document. Cannot deduce heuristic.",
                    output={"items_count": len(extracted_items)},
                )

            # Stage 2: Run CodesDeductionAgent
            deduction_agent = CodesDeductionAgent()
            logger.log_execution("heuristic_service", "deduction_agent_dispatched", "ok",
                                 vendor_codes_count=len(vendor_codes))

            deduction_res = await deduction_agent.aexecute({
                "vendor_codes": vendor_codes,
                "brand_name": brand_name,
                "previous_explanation": brand_obj.brand_code_explanation,
            })

            # Stage result
            result = {
                "regex": deduction_res["regex"],
                "explanation": deduction_res["explanation"],
                "examples": deduction_res["examples"],
                "brand_id": brand_id,
                "vendor_codes_analyzed": len(vendor_codes),
            }
            staging_repo.update_job(
                db, job_id=job_id,
                status=JobStatus.COMPLETED.value,
                data=result
            )
            logger.log_execution("heuristic_service", "process_heuristic_success", "ok",
                                 job_id=job_id)

        except AgentException as exc:
            logger.log_execution("heuristic_service", "agent_exception", "err",
                                 job_id=job_id, exc=exc, output=exc.output)
            staging_repo.update_job(
                db, job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "output": exc.output},
            )
            raise


def get_heuristic_job(db: Session, job_id: str) -> Dict[str, Any]:
    """Retrieves the heuristic deduction job data."""
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found"
        )
    return {
        "status": JobStatus(job.status).name,
        "data": job.data
    }


def confirm_heuristic(db: Session, brand_id: str, job_id: str) -> None:
    """
    Confirms a completed heuristic deduction job.
    Persists regex, explanation, and heuristic_confirmed=True to the Brand record.
    Deletes the staging job.
    """
    logger.log_execution("heuristic_service", "confirm_heuristic_start", "ok",
                         brand_id=brand_id, job_id=job_id)

    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found"
        )
    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job is not yet completed. Cannot confirm."
        )

    data = job.data or {}
    regex = data.get("regex")
    explanation = data.get("explanation")

    if not regex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Staged heuristic data is missing the regex pattern."
        )

    # Update Brand record
    brand = db.query(Brand).filter(Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand ID '{brand_id}' not found in the database."
        )

    brand.brand_code_heuristic = regex
    brand.brand_code_explanation = explanation
    brand.heuristic_confirmed = True
    db.commit()

    # Clean up staging job
    try:
        staging_repo.delete_job(db, job_id)
    except Exception as exc:
        print(f"[heuristic_service] Warning: Failed to clean up staging job {job_id}: {exc}")

    logger.log_execution("heuristic_service", "confirm_heuristic_success", "ok",
                         brand_id=brand_id, job_id=job_id)


async def process_recalculation(brand_id: str, vendor_codes_corpus: List[str], previous_explanation: Optional[str]) -> None:
    """
    Background task: recalculates a brand's heuristic using the full historical
    vendor code corpus plus new breaking codes.
    Stages the result for operator confirmation.
    """
    with SessionLocal() as db:
        try:
            logger.log_execution("heuristic_service", "process_recalculation_start", "ok",
                                 brand_id=brand_id, corpus_size=len(vendor_codes_corpus))

            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{brand_id}' not found for recalculation.")

            # Create staging job for the recalculation
            job_id = str(uuid.uuid4())
            staging_repo.create_job(
                db, job_id=job_id,
                job_type=JobType.HEURISTIC_DEDUCTION.value,
            )

            deduction_agent = CodesDeductionAgent()
            deduction_res = await deduction_agent.aexecute({
                "vendor_codes": vendor_codes_corpus,
                "brand_name": brand_obj.name,
                "previous_explanation": previous_explanation,
            })

            result = {
                "regex": deduction_res["regex"],
                "explanation": deduction_res["explanation"],
                "examples": deduction_res["examples"],
                "brand_id": brand_id,
                "vendor_codes_analyzed": len(vendor_codes_corpus),
                "is_recalculation": True,
            }
            staging_repo.update_job(
                db, job_id=job_id,
                status=JobStatus.COMPLETED.value,
                data=result
            )
            logger.log_execution("heuristic_service", "process_recalculation_success", "ok",
                                 brand_id=brand_id, job_id=job_id)

        except AgentException as exc:
            logger.log_execution("heuristic_service", "recalculation_exception", "err",
                                 brand_id=brand_id, exc=exc)
            if 'job_id' in dir():
                staging_repo.update_job(
                    db, job_id=job_id,
                    status=JobStatus.ERROR.value,
                    data={"error": str(exc), "output": exc.output},
                )
            raise


def _extract_vendor_codes(items: list) -> List[str]:
    """Extract and deduplicate vendor codes from extracted items."""
    codes = []
    seen = set()
    for item in items:
        code = item.get("vendor_code") or item.get("VendorCode") or ""
        code = code.strip().upper()
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes
