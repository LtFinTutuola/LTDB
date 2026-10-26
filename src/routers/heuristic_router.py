"""
heuristic_router.py
-------------------
Dedicated API endpoints for the heuristic deduction flow.
Completely decoupled from the ingestion pipeline.

Endpoints:
  POST   /api/v1/brands/{brand_id}/heuristic             - Trigger deduction
    GET    /api/v1/brands/heuristic/{job_id}              - Poll job status
    POST   /api/v1/brands/heuristic/{job_id}/confirm      - Confirm heuristic
"""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.services import heuristic_service, blueprint_revision_service
from src.schemas.data_ingestion import (
    HeuristicDeductionRequest,
    HeuristicJobStatusResponse,
    BlueprintRevisionContextResponse,
    RuleDeductionRequest,
    RuleDeductionResponse,
    ReconciliationPlanUpdateRequest,
)

router = APIRouter(prefix="/api/v1/brands", tags=["Heuristic Deduction"])


@router.post("/{brand_id}/heuristic")
def trigger_heuristic_deduction(
    brand_id: str,
    request: HeuristicDeductionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Trigger heuristic deduction for a brand using a sample DDT."""
    job_id = heuristic_service.accept_heuristic_job(
        db, brand_id=brand_id, file_path=request.file_path
    )
    background_tasks.add_task(
        heuristic_service.process_heuristic,
        job_id=job_id,
        brand_id=brand_id,
        file_path=request.file_path,
    )
    return {"job_id": job_id}


@router.get("/heuristic/{job_id}", response_model=HeuristicJobStatusResponse)
def get_heuristic_status(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Poll the status and result of a heuristic deduction job."""
    return heuristic_service.get_heuristic_job(db, job_id)


@router.post("/heuristic/{job_id}/confirm")
def confirm_heuristic(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Confirm a completed heuristic proposal and persist it to the Brand record."""
    heuristic_service.confirm_heuristic(db, job_id=job_id)
    return {"status": "success", "message": "Heuristic confirmed and saved"}


# --- Blueprint Revision & Retroactive Reconciliation Endpoints ---


@router.get(
    "/blueprint-revision/{blueprint_id}/context",
    response_model=BlueprintRevisionContextResponse,
)
def get_blueprint_revision_context(
    blueprint_id: str,
    db: Session = Depends(get_db),
):
    """Return physical article groups and metadata for a blueprint to initiate visual separation."""
    return blueprint_revision_service.get_blueprint_revision_context(db, blueprint_id)


@router.post(
    "/blueprint-revision/deduce-rule",
    response_model=RuleDeductionResponse,
)
async def deduce_blueprint_revision_rule(
    request: RuleDeductionRequest,
    db: Session = Depends(get_db),
):
    """Deduce or refine the vendor code normalization rule from visual partitions and optional user feedback."""
    return await blueprint_revision_service.deduce_and_preview_rule(db, request)


@router.post("/blueprint-revision/{job_id}/prepare-staging")
async def prepare_blueprint_revision_staging(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Synthesize new split blueprint metadata and build the full Before & After reconciliation plan."""
    return await blueprint_revision_service.prepare_staging_plan(db, job_id)


@router.get("/blueprint-revision/{job_id}")
def get_blueprint_revision_job(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Retrieve the sanitized heuristic revision staging job (without raw regex exposure)."""
    return blueprint_revision_service.get_sanitized_revision_job(db, job_id)


@router.put("/blueprint-revision/{job_id}/plan")
def update_blueprint_revision_plan(
    job_id: str,
    request: ReconciliationPlanUpdateRequest,
    db: Session = Depends(get_db),
):
    """Apply inline edits to the proposed Before & After reconciliation plan in Staging Area."""
    return blueprint_revision_service.update_reconciliation_plan(db, job_id, request)


@router.post("/blueprint-revision/{job_id}/commit")
def commit_blueprint_revision_plan(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Atomically execute the 5-step database reconciliation and update brand_heuristics."""
    return blueprint_revision_service.commit_reconciliation_plan(db, job_id)

