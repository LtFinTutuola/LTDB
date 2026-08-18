import os
import uuid
from typing import Dict, Any, Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_extraction_agent import DataExtractionAgent
from src.agents.single_item_extraction_agent import SingleItemExtractionAgent
from src.agents.article_blueprints_agent import ArticleBlueprintsAgent
from src.repositories.pim_repo import pim_repo, category_repo
from src.schemas.data_ingestion import StagingConfirmationRequest, EnrichedItemSchema, SingleItemIngestionRequest
from src.services.pim_service import get_or_create_product
from src.services.wms_service import register_inbound_movement
from src.repositories import staging_repo
from src.models.staging import JobStatus
from src.core.logger import get_logger

logger = get_logger()

class InvalidFileFormatException(ValueError):
    """Raised when an unsupported or invalid file format is provided."""
    pass

def accept_job(db: Session, file_path: str) -> str:
    """
    Validates file, checks if a job exists for the given file path in ACCEPTED status.
    If yes, raises HTTPException. Otherwise creates a new job and returns the job_id.
    """
    logger.log_execution("data_ingestion_service", "accept_job_start", "ok", file_path=file_path)
    if not os.path.exists(file_path):
        logger.log_execution("data_ingestion_service", "accept_job_failed", "err", reason="File not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found on the server filesystem")
        
    if not file_path.lower().endswith(".pdf"):
        logger.log_execution("data_ingestion_service", "accept_job_failed", "err", reason="Invalid file format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file format. Only PDF files (.pdf) are supported.")

    existing_job = staging_repo.get_job_by_status_and_path(db, status=JobStatus.ACCEPTED.value, file_path=file_path)
    if existing_job:
        logger.log_execution("data_ingestion_service", "accept_job_failed", "err", reason="Job already exists")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A job for this file is already in progress")
        
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db, job_id=job_id, file_path=file_path)
    logger.log_execution("data_ingestion_service", "accept_job_success", "ok", job_id=job_id)
    return job_id

def accept_single_item_job(db: Session, request: SingleItemIngestionRequest) -> str:
    """
    Creates a new staging job for a single item ingestion.
    """
    job_id = str(uuid.uuid4())
    logger.log_execution("data_ingestion_service", "accept_single_item_job_start", "ok", job_id=job_id, brand_id=request.brand_id)
    staging_repo.create_job(db, job_id=job_id, file_path="single-item-ingestion")
    logger.log_execution("data_ingestion_service", "accept_single_item_job_success", "ok", job_id=job_id)
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
            logger.log_execution("data_ingestion_service", "process_and_stage_start", "ok", job_id=job_id, file_path=file_path, brand_id=brand_id)
            from src.models.pim import Brand
            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{brand_id}' not found in the database.")
            brand_name = brand_obj.name
            logger.log_execution("data_ingestion_service", "brand_resolved", "ok", brand_id=brand_id, brand_name=brand_name)

            # 1. Fetch brand hierarchy and DB embeddings
            categories = category_repo.get_brand_hierarchy(db, brand_id)
            logger.log_execution("data_ingestion_service", "categories_fetched", "ok", macro_category_count=len(categories))
            db_embeddings_matrix = pim_repo.get_embeddings_by_brand(db, brand_id)
            logger.log_execution("data_ingestion_service", "db_embeddings_fetched", "ok", db_embeddings_count=len(db_embeddings_matrix))

            # 2. Run DataExtractionAgent
            extraction_agent = DataExtractionAgent()
            logger.log_execution("data_ingestion_service", "extraction_agent_dispatched", "ok", input_summary={"file_path": file_path, "brand": brand_name})
            extraction_res = await extraction_agent.aexecute({
                "file_path": file_path,
                "brand": brand_name,
            })
            extracted_items = extraction_res["items"]
            logger.log_execution("data_ingestion_service", "extraction_agent_completed", "ok")

            # 3. Run ArticleBlueprintsAgent
            from pathlib import Path
            import yaml
            
            gemini_yaml_path = Path("src/agents/gemini.yaml")
            if gemini_yaml_path.exists():
                with open(gemini_yaml_path, "r") as f:
                    gemini_cfg = yaml.safe_load(f) or {}
            else:
                gemini_cfg = {}

            blueprints_agent = ArticleBlueprintsAgent()
            logger.log_execution("data_ingestion_service", "blueprints_agent_dispatched", "ok", input_summary={"items_count": len(extracted_items)})
            blueprints_res = await blueprints_agent.aexecute({
                "items": extracted_items,
                "categories": categories,
                "db_embeddings_matrix": db_embeddings_matrix,
                "db_similarity_threshold": gemini_cfg.get("db_similarity_threshold", 0.92),
                "articles_similarity_threshold": gemini_cfg.get("articles_similarity_threshold", 0.88),
                "hallucination_recognition_threshold": gemini_cfg.get("hallucination_recognition_threshold", 0.95),
                "candidate_tolerance": gemini_cfg.get("candidate_tolerance", 0.04),
            })
            output_items = blueprints_res["items"]
            output_blueprints = blueprints_res["blueprints"]
            logger.log_execution("data_ingestion_service", "blueprints_agent_completed", "ok")

            # 4. Fail-fast category validation and resolution for new blueprints
            from sqlalchemy import func
            from src.models.pim import Category

            for bp in output_blueprints:
                if bp.get("is_new"):
                    for field, label in [("category", "category"), ("sub_category", "sub-category")]:
                        cat_name = bp.get(field)
                        if cat_name:
                            if isinstance(cat_name, dict):
                                continue
                            cat = db.query(Category).filter(
                                func.lower(Category.name) == str(cat_name).lower()
                            ).first()
                            if cat:
                                logger.log_execution("data_ingestion_service", "category_resolution", "ok", blueprint_id=bp.get("id"), category_attempted=cat_name, result="matched")
                                bp[field] = {"id": str(cat.id), "description": cat.name}
                            else:
                                if field == "category":
                                    logger.log_execution("data_ingestion_service", "category_resolution", "err", blueprint_id=bp.get("id"), category_attempted=cat_name, result="not_found")
                                    raise AgentException(
                                        message=f"category '{cat_name}' does not exist, extraction aborted",
                                        output=None,
                                    )
                                else:
                                    logger.log_execution("data_ingestion_service", "category_resolution", "ok", blueprint_id=bp.get("id"), category_attempted=cat_name, result="fallback_null")
                                    bp[field] = None

            # 5. Build bipartite response and persist as COMPLETED
            result = {
                "items": output_items,
                "blueprints": output_blueprints,
                "warnings": extraction_res.get("warnings", []) + blueprints_res.get("warnings", []),
                "job_id": job_id,
                "brand_id": brand_id,
            }
            staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=result)
            logger.log_execution("data_ingestion_service", "process_and_stage_success", "ok", job_id=job_id)

        except AgentException as exc:
            # Update DB to ERROR so polling clients can observe the failure,
            # then re-raise for the background worker to log the stack trace.
            logger.log_execution("data_ingestion_service", "agent_exception", "err", job_id=job_id, exc=exc, output=exc.output)
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
    logger.log_execution("data_ingestion_service", "confirm_staging_start", "ok", job_id=job_id)
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Determine items and blueprints: use request if present, otherwise fallback to stored job data
    raw_items = None
    raw_blueprints = []

    if staging_data and (staging_data.items is not None or staging_data.blueprints is not None):
        raw_items = [item.model_dump(by_alias=True) if hasattr(item, "model_dump") else item for item in (staging_data.items or [])]
        if staging_data.blueprints is not None:
            raw_blueprints = [bp.model_dump() if hasattr(bp, "model_dump") else bp for bp in staging_data.blueprints]
    elif job.data and "items" in job.data:
        raw_items = job.data.get("items") or []
        raw_blueprints = job.data.get("blueprints") or []

    if not raw_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items to confirm")

    logger.log_execution("data_ingestion_service", "bipartite_merge", "ok", item_count=len(raw_items), blueprint_count=len(raw_blueprints))

    # Bipartite merging: if blueprints are present, join each item with its blueprint definition
    bp_map = {}
    if raw_blueprints:
        for bp in raw_blueprints:
            bp_dict = bp.model_dump() if hasattr(bp, "model_dump") else dict(bp)
            bp_id = str(bp_dict.get("id", ""))
            if bp_id:
                bp_map[bp_id] = bp_dict

        merged_items = []
        for raw_it in raw_items:
            it_dict = raw_it.model_dump(by_alias=True) if hasattr(raw_it, "model_dump") else dict(raw_it)
            bp_id = str(it_dict.get("article_blueprint_id") or it_dict.get("blueprint_group_id") or "")
            if bp_id in bp_map:
                bp = bp_map[bp_id]
                it_dict["category"] = it_dict.get("category") or bp.get("category")
                it_dict["sub_category"] = it_dict.get("sub_category") or bp.get("sub_category")
                it_dict["article_name"] = it_dict.get("article_name") or bp.get("article_name")
                it_dict["product_short_description"] = it_dict.get("product_short_description") or bp.get("description")
                it_dict["product_extended_description"] = it_dict.get("product_extended_description") or bp.get("extended_description")
                it_dict["tags"] = it_dict.get("tags") or bp.get("tags")
                it_dict["materials"] = it_dict.get("materials") or bp.get("materials")
                it_dict["dimensions"] = it_dict.get("dimensions") or bp.get("dimensions")
                it_dict["blueprint_group_id"] = bp_id
            merged_items.append(it_dict)
        items = [EnrichedItemSchema.model_validate(item) for item in merged_items]
    else:
        items = [EnrichedItemSchema.model_validate(item) for item in raw_items]

    brand_id = job.data.get("brand_id") if job.data else None
    if not brand_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Staged job data is missing brand_id")

    resolved_blueprints: Dict[str, str] = {}

    for item in items:
        bp_group_key = item.blueprint_group_id or f"{item.article_name}|{item.product_short_description}"
        is_new = True
        if bp_group_key in bp_map:
            is_new = bp_map[bp_group_key].get("is_new", True)

        if not is_new:
            # Existing DB blueprint: skip category validation and product creation
            blueprint_id = bp_group_key
            if bp_group_key not in resolved_blueprints:
                resolved_blueprints[bp_group_key] = blueprint_id
                logger.log_execution("data_ingestion_service", "blueprint_resolved", "ok", blueprint_key=bp_group_key, blueprint_id=blueprint_id, resolution_status="existing_db")
        else:
            # New blueprint: enforce category validation and create product
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

            if bp_group_key in resolved_blueprints:
                blueprint_id = resolved_blueprints[bp_group_key]
                logger.log_execution("data_ingestion_service", "blueprint_resolved", "ok", blueprint_key=bp_group_key, blueprint_id=blueprint_id, resolution_status="existing_cluster")
            else:
                blueprint_id = get_or_create_product(
                    db=db,
                    brand_id=brand_id,
                    description=item.product_short_description or "Unknown Product",
                    article_name=item.article_name or item.product_short_description or "Unknown Product",
                    extended_description=item.product_extended_description or "",
                    tags=item.tags,
                    materials=item.materials,
                    dimensions=item.dimensions,
                    category_id=category_id,
                    commit_changes=False
                )
                resolved_blueprints[bp_group_key] = blueprint_id
                logger.log_execution("data_ingestion_service", "blueprint_resolved", "ok", blueprint_key=bp_group_key, blueprint_id=blueprint_id, resolution_status="new")

                # Check if newly created blueprint lacks an embedding; if so, generate and save synchronously
                bp_obj = pim_repo.get(db, blueprint_id)
                if bp_obj and bp_obj.embedding is None:
                    text_to_embed = f"{item.article_name or ''} {item.product_short_description or ''}".strip()
                    if text_to_embed:
                        try:
                            from src.agents.llm_client import LLMClient
                            emb = LLMClient().generate_embedding_sync(text_to_embed)
                            pim_repo.save_embedding(db, blueprint_id, emb, commit_changes=False)
                        except Exception as exc:
                            print(f"[confirm_and_persist_staging] Warning: Failed to generate embedding for blueprint {blueprint_id}: {exc}")

        # 2. Register warehouse movement
        if item.quantity and item.quantity > 0:
            logger.log_execution("data_ingestion_service", "wms_registration", "ok", blueprint_id=blueprint_id, quantity=item.quantity)
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

    # Ensure all ingestion changes (blueprints, articles, movements) are committed FIRST
    db.commit()
    logger.log_execution("data_ingestion_service", "confirm_staging_commit", "ok", job_id=job_id)

    # Delete job from staging area in a separate, isolated transaction
    try:
        staging_repo.delete_job(db, job_id)
    except Exception as exc:
        print(f"[confirm_and_persist_staging] Warning: Failed to clean up staging job {job_id}: {exc}")

async def process_and_stage_single_item(job_id: str, request: SingleItemIngestionRequest) -> None:
    """
    Processes a single item ingestion synchronously by running the agents and persisting the result in staging area.
    """
    with SessionLocal() as db:
        try:
            logger.log_execution("data_ingestion_service", "process_and_stage_single_item_start", "ok", job_id=job_id, brand_id=request.brand_id)
            from src.models.pim import Brand
            brand_obj = db.query(Brand).filter(Brand.id == request.brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{request.brand_id}' not found in the database.")
            brand_name = brand_obj.name
            logger.log_execution("data_ingestion_service", "brand_resolved", "ok", brand_id=request.brand_id, brand_name=brand_name)

            # 1. Fetch brand hierarchy and DB embeddings
            categories = category_repo.get_brand_hierarchy(db, request.brand_id)
            db_embeddings_matrix = pim_repo.get_embeddings_by_brand(db, request.brand_id)

            # 2. Run SingleItemExtractionAgent
            extraction_agent = SingleItemExtractionAgent()
            logger.log_execution("data_ingestion_service", "extraction_agent_single_item", "ok", vendor_code=request.vendor_code)
            
            extraction_res = await extraction_agent.aexecute({
                "brand": brand_name,
                "vendor_code": request.vendor_code,
                "description": request.description,
                "barcode": request.barcode,
                "quantity": request.quantity,
                "colors": request.colors
            })
            extracted_items = extraction_res["items"]
            
            # 3. Run ArticleBlueprintsAgent
            from pathlib import Path
            import yaml
            
            gemini_yaml_path = Path("src/agents/gemini.yaml")
            if gemini_yaml_path.exists():
                with open(gemini_yaml_path, "r") as f:
                    gemini_cfg = yaml.safe_load(f) or {}
            else:
                gemini_cfg = {}

            blueprints_agent = ArticleBlueprintsAgent()
            logger.log_execution("data_ingestion_service", "blueprints_agent_single_item", "ok")
            blueprints_res = await blueprints_agent.aexecute({
                "items": extracted_items,
                "categories": categories,
                "db_embeddings_matrix": db_embeddings_matrix,
                "db_similarity_threshold": gemini_cfg.get("db_similarity_threshold", 0.92),
                "articles_similarity_threshold": gemini_cfg.get("articles_similarity_threshold", 0.88),
                "hallucination_recognition_threshold": gemini_cfg.get("hallucination_recognition_threshold", 0.95),
                "candidate_tolerance": gemini_cfg.get("candidate_tolerance", 0.04),
            })
            
            output_items = blueprints_res["items"]
            output_blueprints = blueprints_res["blueprints"]

            # 4. Fail-fast category validation for new blueprints
            from sqlalchemy import func
            from src.models.pim import Category

            for bp in output_blueprints:
                if bp.get("is_new"):
                    for field, label in [("category", "category"), ("sub_category", "sub-category")]:
                        cat_name = bp.get(field)
                        if cat_name:
                            if isinstance(cat_name, dict):
                                continue
                            cat = db.query(Category).filter(
                                func.lower(Category.name) == str(cat_name).lower()
                            ).first()
                            if cat:
                                bp[field] = {"id": str(cat.id), "description": cat.name}
                            else:
                                if field == "category":
                                    raise AgentException(
                                        message=f"category '{cat_name}' does not exist, extraction aborted",
                                        output=None,
                                    )
                                else:
                                    bp[field] = None

            result = {
                "items": output_items,
                "blueprints": output_blueprints,
                "warnings": extraction_res.get("warnings", []) + blueprints_res.get("warnings", []),
                "job_id": job_id,
                "brand_id": request.brand_id,
            }
            staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=result)
            logger.log_execution("data_ingestion_service", "process_and_stage_single_item_success", "ok", job_id=job_id)

        except AgentException as exc:
            logger.log_execution("data_ingestion_service", "process_and_stage_single_item_exception", "err", job_id=job_id, exc=exc, output=exc.output)
            staging_repo.update_job(
                db,
                job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "output": exc.output},
            )
            raise
