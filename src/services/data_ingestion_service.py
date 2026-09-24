import copy
import os
import re
import uuid
from typing import Dict, Any, Optional, List
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_extraction_agent import DataExtractionAgent
from src.agents.article_blueprints_agent import ArticleBlueprintsAgent
from src.repositories.pim_repo import pim_repo, category_repo
from src.schemas.data_ingestion import EnrichedItemSchema, SingleItemIngestionRequest
from src.services.pim_service import get_or_create_product
from src.services.wms_service import register_inbound_movement
from src.repositories import staging_repo
from src.models.staging import JobStatus, JobType
from src.core.logger import get_logger

logger = get_logger()

class InvalidFileFormatException(ValueError):
    """Raised when an unsupported or invalid file format is provided."""
    pass

def _resolve_items_with_photos(db: Session, brand_id: str, extracted_items: list, heuristics: list):
    from src.repositories.photo_repo import photo_repo
    from src.repositories.wms_repo import wms_repo
    
    resolved_items = []
    unresolved_items = []
    resolved_blueprints = {}
    heuristic_warnings = []
    photo_only_items = []
    
    for item in extracted_items:
        raw_code = item.get("vendor_code") or item.get("VendorCode") or ""
        raw_code = raw_code.strip().upper()
        
        normalized_code = raw_code
        matched = False
        color_code_extracted = None
        for h in heuristics:
            if not h.pattern:
                continue
                
            compiled_regex = re.compile(h.pattern)
            match = compiled_regex.search(raw_code)
            if match:
                if "color_code" in match.groupdict() and match.group("color_code") is not None:
                    color_code_extracted = match.group("color_code")
                    normalized_code = raw_code[:match.start("color_code")] + "#" + raw_code[match.end("color_code"):]
                matched = True
                break
        
        if not matched:
            heuristic_warnings.append(f"heuristic_break: '{raw_code}' failed regex validation")
        
        bp = pim_repo.get_blueprint_by_normalized_code(db, brand_id, normalized_code)
        if bp:
            item["article_blueprint_id"] = str(bp.id)
            item["normalized_vendor_code"] = normalized_code
            
            # --- PHOTO AWARE RESOLUTION ---
            photo_found = None
            
            # Caso A
            if color_code_extracted:
                existing_article = wms_repo.get_article_by_supplier_code_and_brand(db, brand_id, raw_code)
                if existing_article and existing_article.colors:
                    photo_found = photo_repo.get_photo_by_blueprint_and_color(db, str(bp.id), existing_article.colors[0])
            else:
                # Caso B
                colors = item.get("colors") or item.get("Color") or []
                if isinstance(colors, str):
                    colors = [colors] if colors.strip() else []
                if colors:
                    photo_found = photo_repo.get_photo_by_blueprint_and_color(db, str(bp.id), colors[0])
            
            if photo_found:
                item["existing_photo_id"] = str(photo_found.id)
                resolved_items.append(item)
            else:
                item["needs_photo_only"] = True
                photo_only_items.append(item)
                resolved_items.append(item)

            if str(bp.id) not in resolved_blueprints:
                cat_dict = None
                if bp.category:
                    cat_dict = {"id": str(bp.category.id), "description": bp.category.name}
                    
                resolved_blueprints[str(bp.id)] = {
                    "id": str(bp.id),
                    "is_new": False,
                    "article_name": bp.article_name,
                    "description": bp.description,
                    "category": cat_dict,
                    "sub_category": None,
                    "extended_description": bp.extended_description,
                    "tags": bp.tags,
                    "materials": bp.materials,
                    "dimensions": bp.dimensions
                }
        else:
            item["normalized_vendor_code"] = normalized_code
            unresolved_items.append(item)
            
    return resolved_items, unresolved_items, resolved_blueprints, heuristic_warnings, photo_only_items

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

    existing_job = staging_repo.get_job_by_status_and_path(db, status=JobStatus.ACCEPTED.value, file_path=file_path, job_type=JobType.DDT_IMPORT.value)
    if existing_job:
        logger.log_execution("data_ingestion_service", "accept_job_failed", "err", reason="Job already exists")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A job for this file is already in progress")
        
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db, job_id=job_id, job_type=JobType.DDT_IMPORT.value, file_path=file_path)
    logger.log_execution("data_ingestion_service", "accept_job_success", "ok", job_id=job_id)
    return job_id

def accept_single_item_job(db: Session, request: SingleItemIngestionRequest) -> str:
    """
    Creates a new staging job for a single item ingestion.
    """
    job_id = str(uuid.uuid4())
    logger.log_execution("data_ingestion_service", "accept_single_item_job_start", "ok", job_id=job_id, brand_id=request.brand_id)
    staging_repo.create_job(db, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
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

            # 1. Fetch brand hierarchy
            categories = category_repo.get_brand_hierarchy(db, brand_id)
            logger.log_execution("data_ingestion_service", "categories_fetched", "ok", macro_category_count=len(categories))

            # 2. Check Heuristic Pre-Requisites (Hard block for DDT)
            heuristics = brand_obj.heuristics
            if not heuristics:
                raise AgentException("Nessuna regola di normalizzazione trovata per questo Brand. Importa prima un singolo articolo manualmente per permettere al sistema di imparare la codifica del Brand.")

            # 3. Run DataExtractionAgent
            extraction_agent = DataExtractionAgent()
            logger.log_execution("data_ingestion_service", "extraction_agent_dispatched", "ok", input_summary={"file_path": file_path, "brand": brand_name})
            extraction_res = await extraction_agent.aexecute({
                "file_path": file_path,
                "brand": brand_name,
            })
            extracted_items = extraction_res["items"]
            logger.log_execution("data_ingestion_service", "extraction_agent_completed", "ok")

            # 4. Deterministic Pre-Resolution
            heuristics = brand_obj.heuristics
            
            resolved_items, unresolved_items, resolved_blueprints, heuristic_warnings, photo_only_items = _resolve_items_with_photos(
                db, brand_id, extracted_items, heuristics
            )
                    
            # Group unresolved_items into new_blueprints
            clusters = {}
            for item in unresolved_items:
                n_code = item.get("normalized_vendor_code") or item.get("vendor_code") or ""
                if n_code not in clusters:
                    clusters[n_code] = {
                        "id": str(uuid.uuid4()),
                        "is_new": True,
                        "cluster_items": []
                    }
                clusters[n_code]["cluster_items"].append(item)
                
            new_blueprints_input = list(clusters.values())
            logger.log_execution("data_ingestion_service", "deterministic_resolution", "ok", 
                                 resolved=len(resolved_items), unresolved=len(unresolved_items),
                                 new_clusters=len(new_blueprints_input))

            # 4. Run ArticleBlueprintsAgent ONLY IF there are unresolved items or photo_only_items
            if new_blueprints_input or photo_only_items:
                blueprints_agent = ArticleBlueprintsAgent()
                logger.log_execution("data_ingestion_service", "blueprints_agent_dispatched", "ok", input_summary={"clusters_count": len(new_blueprints_input), "photo_only_count": len(photo_only_items)})
                blueprints_res = await blueprints_agent.aexecute({
                    "new_blueprints": new_blueprints_input,
                    "photo_only_items": photo_only_items,
                    "categories": categories,
                    "brand_name": brand_name,
                    "resolved_blueprints": resolved_blueprints,
                })
                agent_items = blueprints_res["items"]
                agent_blueprints = blueprints_res["blueprints"]
                agent_photo_proposals = blueprints_res.get("photo_proposals", [])
                agent_warnings = blueprints_res.get("warnings", [])
                logger.log_execution("data_ingestion_service", "blueprints_agent_completed", "ok")
            else:
                agent_items = []
                agent_blueprints = []
                agent_photo_proposals = []
                agent_warnings = []
                
            # Merge outputs
            def _clean_item(it: dict) -> dict:
                colors = it.get("colors") or it.get("Color") or []
                if isinstance(colors, str):
                    colors = [colors] if colors.strip() else []
                elif isinstance(colors, list):
                    colors = [str(c) for c in colors if c]
                else:
                    colors = []
                try:
                    qty = int(it.get("quantity") or it.get("Quantity") or 0)
                except (ValueError, TypeError):
                    qty = 0
                return {
                    "item_id": it.get("item_id", ""),
                    "vendor_code": it.get("vendor_code") or it.get("VendorCode") or "",
                    "normalized_vendor_code": it.get("normalized_vendor_code") or "",
                    "barcode": it.get("barcode") or it.get("Barcode") or "",
                    "quantity": qty,
                    "colors": colors,
                    "article_blueprint_id": it.get("article_blueprint_id", ""),
                }
            
            output_items = [_clean_item(it) for it in resolved_items] + agent_items
            output_blueprints = list(resolved_blueprints.values()) + agent_blueprints

            # Combine photo_proposals (DB ones + Agent ones)
            photo_proposals_db = []
            for it in resolved_items:
                if it.get("existing_photo_id"):
                    colors = it.get("colors") or it.get("Color") or []
                    if isinstance(colors, str):
                        colors = [colors] if colors.strip() else []
                    
                    photo_proposals_db.append({
                        "item_id": it.get("item_id"),
                        "photo_url": f"/api/v1/ingestion/photos/{it['existing_photo_id']}",
                        "photo_source": "db_existing",
                        "canonical_color_candidate": colors[0].lower() if colors else "unknown",
                        "retry_count": 0,
                        "_photo_search_ctx": []
                    })
            all_photo_proposals = photo_proposals_db + agent_photo_proposals

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
                "photo_proposals": all_photo_proposals,
                "warnings": extraction_res.get("warnings", []) + heuristic_warnings + agent_warnings,
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
    Enriches existing DB blueprints (is_new=False) with article_name and description if missing.
    """
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        
    data = copy.deepcopy(job.data) if job.data else None
    if data and "blueprints" in data and isinstance(data["blueprints"], list):
        from src.models.pim import ArticleBlueprint
        missing_bp_ids = [
            bp["id"] for bp in data["blueprints"]
            if isinstance(bp, dict) and not bp.get("is_new", True) and (bp.get("article_name") is None or bp.get("description") is None) and bp.get("id")
        ]
        if missing_bp_ids:
            db_bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.id.in_(missing_bp_ids)).all()
            bp_map = {str(b.id): b for b in db_bps}
            for bp in data["blueprints"]:
                if isinstance(bp, dict) and not bp.get("is_new", True) and bp.get("id") in bp_map:
                    db_bp = bp_map[bp["id"]]
                    if bp.get("article_name") is None:
                        bp["article_name"] = db_bp.article_name
                    if bp.get("description") is None:
                        bp["description"] = db_bp.description

    return {
        "status": JobStatus(job.status).name,
        "data": data
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

from fastapi import BackgroundTasks

async def confirm_and_persist_staging(
    db: Session, 
    job_id: str,
    background_tasks: BackgroundTasks,
) -> None:
    """
    Orchestrates the creation of missing products and registration of warehouse movements.
    Always reads from the staging area data (edited via the revision endpoint).
    """
    logger.log_execution("data_ingestion_service", "confirm_staging_start", "ok", job_id=job_id)
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Always read from stored staging data
    raw_items = job.data.get("items") or [] if job.data else []
    raw_blueprints = job.data.get("blueprints") or [] if job.data else []

    if not raw_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items to confirm")

    logger.log_execution("data_ingestion_service", "bipartite_merge", "ok", item_count=len(raw_items), blueprint_count=len(raw_blueprints))

    # Build blueprint map indexed by ID
    bp_map = {}
    if raw_blueprints:
        for bp in raw_blueprints:
            bp_dict = bp.model_dump() if hasattr(bp, "model_dump") else dict(bp)
            bp_id = str(bp_dict.get("id", ""))
            if bp_id:
                bp_map[bp_id] = bp_dict

    # Bipartite merging: join each item with its blueprint definition via article_blueprint_id
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

    brand_id = job.data.get("brand_id") if job.data else None
    if not brand_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Staged job data is missing brand_id")

    # Check for heuristic breaks and deduce new regex if needed BEFORE saving to DB
    warnings = job.data.get("warnings") or [] if job.data else []
    heuristic_breaks = [w for w in warnings if isinstance(w, str) and w.startswith("heuristic_break:")]
    
    if heuristic_breaks:
        from src.models.pim import Brand
        logger.log_execution("data_ingestion_service", "heuristic_break_detected", "warn", job_id=job_id, breaks_count=len(heuristic_breaks))
        brand = db.query(Brand).filter(Brand.id == brand_id).first()
        if brand:
            brand.heuristic_confirmed = False
            db.commit()
            
            # Extract breaking codes
            breaking_codes = []
            for w in heuristic_breaks:
                parts = w.split("'")
                if len(parts) >= 3:
                    breaking_codes.append(parts[1])

            if job.job_type == JobType.SINGLE_ITEM_IMPORT.value and len(breaking_codes) == 1:
                # Dispatch single-shot deduction for single item synchronously
                raw_code = breaking_codes[0]
                
                article_name = ""
                for item in items:
                    if item.vendor_code == raw_code or item.normalized_vendor_code == raw_code:
                        article_name = item.article_name or ""
                        break
                
                from src.services.heuristic_service import process_single_shot_deduction
                new_regex = await process_single_shot_deduction(
                    brand_id=brand_id,
                    raw_vendor_code=raw_code,
                    official_name=article_name
                )
                logger.log_execution("data_ingestion_service", "single_shot_task_dispatched", "ok", brand_id=brand_id)

                # In-memory retroactive normalization for the current staging payload
                if new_regex:
                    try:
                        compiled_regex = re.compile(new_regex)
                        for item in items:
                            if item.vendor_code in breaking_codes:
                                match = compiled_regex.search(item.vendor_code)
                                if match and "color_code" in match.groupdict() and match.group("color_code") is not None:
                                    item.normalized_vendor_code = item.vendor_code[:match.start("color_code")] + "#" + item.vendor_code[match.end("color_code"):]
                    except Exception as e:
                        print(f"Failed to apply regex {new_regex}: {e}")
            else:
                # For DDT imports, we might still dispatch recalculation to background, but for now we won't block
                # Get historical codes
                from src.models.wms import Article
                from src.models.pim import ArticleBlueprint
                historical_articles = db.query(Article.supplier_code).join(ArticleBlueprint, Article.article_blueprint_id == ArticleBlueprint.id).filter(ArticleBlueprint.brand_id == brand_id).all()
                historical_codes = [a.supplier_code for a in historical_articles if a.supplier_code]
                
                corpus = list(set(historical_codes + breaking_codes))
                
                # Dispatch background recalculation
                from src.services.heuristic_service import process_recalculation
                background_tasks.add_task(
                    process_recalculation,
                    brand_id=brand_id,
                    vendor_codes_corpus=corpus,
                    previous_explanation="" # Empty explanation string as fallback
                )
                logger.log_execution("data_ingestion_service", "recalculation_task_dispatched", "ok", brand_id=brand_id)

    resolved_blueprints: Dict[str, str] = {}
    item_photo_context = {}

    for item, raw_it_dict in zip(items, merged_items):
        bp_group_key = item.blueprint_group_id or item.article_name or f"{item.product_short_description}"
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
                    normalized_vendor_code=item.normalized_vendor_code,
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
                            
        # Map item_id to blueprint and color for photo download
        if raw_it_dict.get("item_id"):
            colors = item.colors or []
            color_name = colors[0].lower() if colors else "unknown"
            item_photo_context[raw_it_dict["item_id"]] = {"blueprint_id": blueprint_id, "color_name": color_name}

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

    # 3. Download and save photos from photo_proposals
    photo_proposals = job.data.get("photo_proposals") or []
    if photo_proposals:
        from src.services.photo_service import download_and_save_photo
        for proposal in photo_proposals:
            p_source = proposal.get("photo_source")
            if p_source == "db_existing":
                continue
                
            it_id = proposal.get("item_id")
            photo_url = proposal.get("photo_url")
            
            if not photo_url or not it_id or it_id not in item_photo_context:
                continue
                
            bp_id = item_photo_context[it_id]["blueprint_id"]
            color_name = item_photo_context[it_id]["color_name"]
            
            try:
                download_and_save_photo(db, bp_id, color_name, photo_url, commit_changes=False)
            except Exception as exc:
                logger.log_execution("data_ingestion_service", "photo_download_failed", "error",
                                     photo_url=photo_url, exc=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Impossibile scaricare la foto per blueprint '{bp_id}' (colore: {color_name}): {exc}"
                )
        db.commit()



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

            # 1. Fetch brand hierarchy
            categories = category_repo.get_brand_hierarchy(db, request.brand_id)

            # 2. Map request directly to item
            extracted_items = [{
                "item_id": str(uuid.uuid4()),
                "vendor_code": request.vendor_code,
                "VendorCode": request.vendor_code,
                "barcode": request.barcode,
                "quantity": request.quantity,
                "colors": request.colors,
                "article_name": request.article_name
            }]
            extraction_res = {"warnings": []}
            
            # 3. Deterministic Pre-Resolution
            heuristics = brand_obj.heuristics
            
            resolved_items, unresolved_items, resolved_blueprints, heuristic_warnings, photo_only_items = _resolve_items_with_photos(
                db, request.brand_id, extracted_items, heuristics
            )
            # Group unresolved_items into new_blueprints
            clusters = {}
            for item in unresolved_items:
                n_code = item.get("normalized_vendor_code") or item.get("vendor_code") or ""
                if n_code not in clusters:
                    clusters[n_code] = {
                        "id": str(uuid.uuid4()),
                        "is_new": True,
                        "cluster_items": []
                    }
                clusters[n_code]["cluster_items"].append(item)
                
            new_blueprints_input = list(clusters.values())

            # 4. Run ArticleBlueprintsAgent ONLY IF there are unresolved items or photo_only_items
            if new_blueprints_input or photo_only_items:
                blueprints_agent = ArticleBlueprintsAgent()
                logger.log_execution("data_ingestion_service", "blueprints_agent_single_item", "ok")
                blueprints_res = await blueprints_agent.aexecute({
                    "new_blueprints": new_blueprints_input,
                    "photo_only_items": photo_only_items,
                    "categories": categories,
                    "brand_name": brand_name,
                    "resolved_blueprints": resolved_blueprints,
                })
                agent_items = blueprints_res["items"]
                agent_blueprints = blueprints_res["blueprints"]
                agent_photo_proposals = blueprints_res.get("photo_proposals", [])
                agent_warnings = blueprints_res.get("warnings", [])
            else:
                agent_items = []
                agent_blueprints = []
                agent_photo_proposals = []
                agent_warnings = []

            # Merge outputs
            def _clean_item(it: dict) -> dict:
                colors = it.get("colors") or it.get("Color") or []
                if isinstance(colors, str):
                    colors = [colors] if colors.strip() else []
                elif isinstance(colors, list):
                    colors = [str(c) for c in colors if c]
                else:
                    colors = []
                try:
                    qty = int(it.get("quantity") or it.get("Quantity") or 0)
                except (ValueError, TypeError):
                    qty = 0
                return {
                    "item_id": it.get("item_id", ""),
                    "vendor_code": it.get("vendor_code") or it.get("VendorCode") or "",
                    "normalized_vendor_code": it.get("normalized_vendor_code") or "",
                    "barcode": it.get("barcode") or it.get("Barcode") or "",
                    "quantity": qty,
                    "colors": colors,
                    "article_blueprint_id": it.get("article_blueprint_id", ""),
                }
            
            output_items = [_clean_item(it) for it in resolved_items] + agent_items
            output_blueprints = list(resolved_blueprints.values()) + agent_blueprints

            # Combine photo_proposals (DB ones + Agent ones)
            photo_proposals_db = []
            for it in resolved_items:
                if it.get("existing_photo_id"):
                    colors = it.get("colors") or it.get("Color") or []
                    if isinstance(colors, str):
                        colors = [colors] if colors.strip() else []
                    
                    photo_proposals_db.append({
                        "item_id": it.get("item_id"),
                        "photo_url": f"/api/v1/ingestion/photos/{it['existing_photo_id']}",
                        "photo_source": "db_existing",
                        "canonical_color_candidate": colors[0].lower() if colors else "unknown",
                        "retry_count": 0,
                        "_photo_search_ctx": []
                    })
            all_photo_proposals = photo_proposals_db + agent_photo_proposals

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
                "photo_proposals": all_photo_proposals,
                "warnings": extraction_res.get("warnings", []) + heuristic_warnings + agent_warnings,
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
