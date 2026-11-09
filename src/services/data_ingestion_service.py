import copy
import os
import re
import uuid
from typing import Dict, Any, Optional, List
from fastapi import HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_extraction_agent import DataExtractionAgent
from src.agents.article_blueprints_agent import ArticleBlueprintsAgent
from src.repositories.pim_repo import pim_repo, category_repo, heuristic_repo
from src.schemas.data_ingestion import EnrichedItemSchema, SingleItemIngestionRequest
from src.services.pim_service import get_or_create_product
from src.services.wms_service import register_inbound_movement
from src.repositories import staging_repo
from src.models.staging import JobStatus, JobType
from src.models.pim import HeuristicMode
from src.core.logger import get_logger
from src.services.search_service import invalidate_search_cache
from src.utils import vendor_code_normalizer as vcn

logger = get_logger()


class InvalidFileFormatException(ValueError):
    """Raised when an unsupported or invalid file format is provided."""
    pass


def _extract_item_colors(item: dict) -> list[str]:
    colors = item.get("colors") or item.get("Color") or []
    if isinstance(colors, str):
        return [colors.strip().lower()] if colors.strip() else []
    if isinstance(colors, list):
        return [str(c).strip().lower() for c in colors if c and str(c).strip()]
    return []


def _serialize_db_blueprint(db: Session, bp) -> dict:
    cat_dict = None
    sub_cat_dict = None
    if bp.category:
        if bp.category.parent_id:
            parent_cat = bp.category.parent
            if not parent_cat:
                from src.models.pim import Category
                parent_cat = db.query(Category).filter_by(id=bp.category.parent_id).first()
            if parent_cat:
                cat_dict = {"id": str(parent_cat.id), "description": parent_cat.name}
                sub_cat_dict = {"id": str(bp.category.id), "description": bp.category.name}
            else:
                cat_dict = {"id": str(bp.category.id), "description": bp.category.name}
                sub_cat_dict = None
        else:
            cat_dict = {"id": str(bp.category.id), "description": bp.category.name}
            sub_cat_dict = None

    return {
        "id": str(bp.id),
        "is_new": False,
        "article_name": bp.article_name,
        "description": bp.description,
        "category": cat_dict,
        "sub_category": sub_cat_dict,
        "extended_description": bp.extended_description,
        "tags": bp.tags,
        "materials": bp.materials,
        "dimensions": bp.dimensions,
    }


def _resolve_items_with_photos(db: Session, brand_id: str, extracted_items: list, heuristics: list):
    """
    Fast-Path deterministico a due livelli:
      - L0: Match esatto su Article.supplier_code (raw_code).
            Se il brand è in PENDING_COHORT e l'item ha un colore diverso da quelli già noti
            per quel supplier_code, devia al Cold-Path per permettere all'Oracolo la verifica Q2/Q4.
      - L1: Applicazione della maschera posizionale / regex e lookup su ArticleBlueprint.normalized_vendor_code.
    """
    from src.repositories.photo_repo import photo_repo
    from src.repositories.wms_repo import wms_repo
    from src.models.pim import ArticleBlueprint

    resolved_items = []
    unresolved_items = []
    resolved_blueprints = {}
    heuristic_warnings = []
    photo_only_items = []

    def _select_active_heuristic(h_list: list):
        if not h_list:
            return None
        confirmed_or_masked = [
            h for h in h_list
            if getattr(h, "heuristic_confirmed", False)
            or (getattr(h, "heuristic_mode", None) not in (None, HeuristicMode.PENDING_COHORT))
        ]
        pool = confirmed_or_masked if confirmed_or_masked else h_list
        return sorted(pool, key=lambda h: getattr(h, "created_at", None) or 0)[-1] if all(getattr(h, "created_at", None) for h in pool) else pool[-1]

    active_h = _select_active_heuristic(heuristics)
    active_mode = getattr(active_h, "heuristic_mode", None) or HeuristicMode.PENDING_COHORT
    brand_embeddings = heuristic_repo.get_embeddings_by_brand(db, brand_id)
    from src.models.pim import BrandHeuristic

    for item in extracted_items:
        raw_code = item.get("vendor_code") or item.get("VendorCode") or ""
        clean_raw = vcn.clean_code(raw_code)
        sterilized_code = vcn.sterilize_separators(raw_code)
        item_colors = _extract_item_colors(item)

        bp = None
        send_to_cold_path = False
        normalized_code = sterilized_code or clean_raw
        color_masked = False
        matched_heuristic_id = None

        # Pre-calcolo codice normalizzato tramite euristiche del brand o embedding
        # Solo l'euristica ATTIVA: le euristiche storiche non devono mascherare.
        # (normalize_code ignora i pattern regex delle euristiche PENDING_COHORT.)
        if active_h is not None:
            cand = vcn.normalize_code(clean_raw, active_h)
            if cand != clean_raw and cand != sterilized_code:
                normalized_code = cand
                color_masked = True
                matched_heuristic_id = getattr(active_h, "id", None)

        if not color_masked and sterilized_code:
            for emb in brand_embeddings:
                emb_h = db.query(BrandHeuristic).filter_by(id=emb.heuristic_id).first()
                score, root_str, masked_code = vcn.score_embedding_compatibility(sterilized_code, emb, emb_h)
                if score >= 0.80 and masked_code:
                    normalized_code = masked_code
                    color_masked = True
                    matched_heuristic_id = emb.heuristic_id
                    break

        # Gate sulla lunghezza del colore: fuori range degli embedding ⇒ nessuna maschera silenziosa
        if color_masked and sterilized_code and brand_embeddings:
            h_id = matched_heuristic_id or getattr(active_h, "id", None)
            emb_scope = [e for e in brand_embeddings if e.heuristic_id == h_id] or list(brand_embeddings)
            gate_h = active_h if getattr(active_h, "id", None) == h_id else None
            col_len = vcn.color_segment_length(sterilized_code, emb_scope[0], gate_h)
            if not vcn.color_length_in_range(col_len, emb_scope):
                normalized_code = sterilized_code
                color_masked = False
                matched_heuristic_id = None
                item["_color_length_anomaly"] = True

        # --- Stadio 1: Exact Match (WMS Articles & Static Blueprints) ---
        existing_article = None
        if clean_raw:
            existing_article = wms_repo.get_article_by_supplier_code_and_brand(db, brand_id, clean_raw)
            if not existing_article and sterilized_code != clean_raw:
                existing_article = wms_repo.get_article_by_supplier_code_and_brand(db, brand_id, sterilized_code)
            if existing_article:
                known_colors = {str(c).strip().lower() for c in (existing_article.colors or []) if c}
                if (
                    active_mode in (HeuristicMode.COLOR_AGNOSTIC, HeuristicMode.COLOR_MASKED, HeuristicMode.COLOR_MASKED_COMPACT, HeuristicMode.COLOR_MASKED_DELIMITED)
                    or "@" in sterilized_code
                    or not item_colors
                    or not known_colors
                    or bool(set(item_colors) & known_colors)
                ):
                    bp = db.query(ArticleBlueprint).filter(
                        ArticleBlueprint.id == existing_article.article_blueprint_id
                    ).first()
                    if bp and bp.normalized_vendor_code:
                        normalized_code = bp.normalized_vendor_code
                        if "#" in normalized_code:
                            color_masked = True
                else:
                    send_to_cold_path = True

        if bp is None and not send_to_cold_path and normalized_code:
            cand_bp = pim_repo.get_blueprint_by_normalized_code(db, brand_id, normalized_code)
            if cand_bp is not None:
                if not color_masked and active_mode == HeuristicMode.PENDING_COHORT and item_colors and "@" not in sterilized_code:
                    bp_colors = {str(c).strip().lower() for a in (cand_bp.articles or []) for c in (a.colors or []) if c}
                    if bp_colors and not (set(item_colors) & bp_colors):
                        send_to_cold_path = True
                if not send_to_cold_path:
                    bp = cand_bp

        if bp is None and not send_to_cold_path and sterilized_code:
            cand_bp = pim_repo.get_blueprint_by_normalized_code(db, brand_id, sterilized_code)
            if not cand_bp and clean_raw != sterilized_code:
                cand_bp = pim_repo.get_blueprint_by_normalized_code(db, brand_id, clean_raw)
            if cand_bp is not None and not (cand_bp.normalized_vendor_code and "#" in cand_bp.normalized_vendor_code):
                if active_mode == HeuristicMode.PENDING_COHORT and item_colors and "@" not in sterilized_code:
                    bp_colors = {str(c).strip().lower() for a in (cand_bp.articles or []) for c in (a.colors or []) if c}
                    if bp_colors and not (set(item_colors) & bp_colors):
                        send_to_cold_path = True
                if not send_to_cold_path:
                    bp = cand_bp
                    normalized_code = bp.normalized_vendor_code or sterilized_code

        # --- Stadio 2A & 2B: Morphological Routing (CodeEmbedding) + Skeleton Lookup ---
        if bp is None and not send_to_cold_path and normalized_code:
            candidate_skeleton = vcn.compute_skeleton_code(normalized_code)
            is_compact_no_sep = ("@" not in sterilized_code and active_mode in (HeuristicMode.COLOR_MASKED, HeuristicMode.COLOR_MASKED_COMPACT))
            matches = pim_repo.get_by_skeleton_code(db, brand_id, candidate_skeleton, exclude_color_agnostic=is_compact_no_sep)
            if len(matches) == 1:
                bp = matches[0]
            elif len(matches) > 1:
                # Q4 case / split HITL pregresso: invia all'Oracolo (Stadio 4)
                send_to_cold_path = True

        if bp is not None:
            item["article_blueprint_id"] = str(bp.id)
            item["normalized_vendor_code"] = normalized_code

            # --- PHOTO AWARE RESOLUTION ---
            photo_found = None
            if color_masked:
                art = existing_article or wms_repo.get_article_by_supplier_code_and_brand(db, brand_id, raw_code)
                if art and art.colors:
                    if not item_colors:
                        item["colors"] = list(art.colors)
                    photo_found = photo_repo.get_photo_by_blueprint_and_color(db, str(bp.id), art.colors[0])
            if photo_found is None and item_colors:
                photo_found = photo_repo.get_photo_by_blueprint_and_color(db, str(bp.id), item_colors[0])
            if photo_found is None and existing_article and existing_article.colors:
                if not item_colors:
                    item["colors"] = list(existing_article.colors)
                photo_found = photo_repo.get_photo_by_blueprint_and_color(db, str(bp.id), existing_article.colors[0])

            if photo_found:
                item["existing_photo_id"] = str(photo_found.id)
                resolved_items.append(item)
            else:
                item["needs_photo_only"] = True
                photo_only_items.append(item)
                resolved_items.append(item)

            if str(bp.id) not in resolved_blueprints:
                resolved_blueprints[str(bp.id)] = _serialize_db_blueprint(db, bp)
        else:
            item["normalized_vendor_code"] = normalized_code
            unresolved_items.append(item)

    return resolved_items, unresolved_items, resolved_blueprints, heuristic_warnings, photo_only_items


def _build_heuristic_context(heuristics: list) -> dict:
    if not heuristics:
        return {"mode": HeuristicMode.PENDING_COHORT, "rules": [], "negative_constraints": []}
    confirmed_or_masked = [
        h for h in heuristics
        if getattr(h, "heuristic_confirmed", False)
        or (getattr(h, "heuristic_mode", None) not in (None, HeuristicMode.PENDING_COHORT))
    ]
    pool = confirmed_or_masked if confirmed_or_masked else heuristics
    h = sorted(pool, key=lambda x: getattr(x, "created_at", None) or 0)[-1] if all(getattr(x, "created_at", None) for x in pool) else pool[-1]
    meta = getattr(h, "heuristic_metadata", None) or {}
    return {
        "mode": getattr(h, "heuristic_mode", None) or HeuristicMode.PENDING_COHORT,
        "rules": list(meta.get("rules") or []),
        "negative_constraints": list(meta.get("negative_constraints") or []),
    }


def _enrich_reconciliations_with_photos(db: Session, reconciliations: list[dict]) -> list[dict]:
    if not reconciliations:
        return []
    from src.repositories.photo_repo import photo_repo
    from src.models.wms import Article
    for rec in reconciliations:
        bps_to_check = list(rec.get("matched_db_blueprints") or []) + list(rec.get("affected_db_blueprints") or [])
        for bp in bps_to_check:
            bp_id = bp.get("id")
            if bp_id:
                photos = photo_repo.get_all_photos_by_blueprint(db, str(bp_id))
                if photos:
                    bp["photo_url"] = f"/api/v1/ingestion/photos/{photos[0].id}"
            for v in bp.get("variants") or []:
                cols = v.get("colors") or []
                if bp_id and cols:
                    ph = photo_repo.get_photo_by_blueprint_and_color(db, str(bp_id), str(cols[0]))
                    if ph:
                        v["photo_url"] = f"/api/v1/ingestion/photos/{ph.id}"
                        if not bp.get("photo_url"):
                            bp["photo_url"] = v["photo_url"]

            # Assicura che raw_vendor_code sia popolato con il codice grezzo non mascherato
            if not bp.get("raw_vendor_code"):
                raw = None
                for v in bp.get("variants") or []:
                    if v.get("supplier_code"):
                        raw = v["supplier_code"]
                        break
                if not raw and bp_id:
                    art = db.query(Article.supplier_code).filter(
                        Article.article_blueprint_id == str(bp_id),
                        Article.supplier_code.isnot(None)
                    ).first()
                    if art:
                        raw = art[0]
                bp["raw_vendor_code"] = raw or bp.get("normalized_vendor_code")
    return reconciliations


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


def _clean_staged_item(it: dict) -> dict:
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


async def _stage_extracted_items(
    db: Session,
    job_id: str,
    brand_id: str,
    brand_name: str,
    categories: dict,
    heuristics: list,
    extracted_items: list,
    extraction_warnings: list,
) -> dict:
    resolved_items, unresolved_items, resolved_blueprints, heuristic_warnings, photo_only_items = _resolve_items_with_photos(
        db, brand_id, extracted_items, heuristics
    )

    clusters = {}
    for item in unresolved_items:
        n_code = item.get("normalized_vendor_code") or item.get("vendor_code") or ""
        if n_code not in clusters:
            clusters[n_code] = {
                "id": str(uuid.uuid4()),
                "is_new": True,
                "cluster_items": [],
            }
        clusters[n_code]["cluster_items"].append(item)

    new_blueprints_input = list(clusters.values())
    logger.log_execution(
        "data_ingestion_service", "deterministic_resolution", "ok",
        resolved=len(resolved_items), unresolved=len(unresolved_items),
        new_clusters=len(new_blueprints_input),
    )

    if new_blueprints_input or photo_only_items:
        brand_cohort = pim_repo.get_brand_cohort(db, brand_id) if hasattr(pim_repo, "get_brand_cohort") else []
        heuristic_ctx = _build_heuristic_context(heuristics)
        blueprints_agent = ArticleBlueprintsAgent()
        logger.log_execution(
            "data_ingestion_service", "blueprints_agent_dispatched", "ok",
            input_summary={"clusters_count": len(new_blueprints_input), "photo_only_count": len(photo_only_items)},
        )
        blueprints_res = await blueprints_agent.aexecute({
            "new_blueprints": new_blueprints_input,
            "photo_only_items": photo_only_items,
            "categories": categories,
            "brand_id": brand_id,
            "brand_name": brand_name,
            "resolved_blueprints": resolved_blueprints,
            "brand_cohort": brand_cohort,
            "brand_heuristic": heuristic_ctx,
        })
        agent_items = blueprints_res["items"]
        agent_blueprints = blueprints_res["blueprints"]
        agent_photo_proposals = blueprints_res.get("photo_proposals", [])
        agent_warnings = blueprints_res.get("warnings", [])
        heuristic_reconciliations = _enrich_reconciliations_with_photos(
            db, blueprints_res.get("heuristic_reconciliations", [])
        )
        logger.log_execution("data_ingestion_service", "blueprints_agent_completed", "ok")
    else:
        agent_items = []
        agent_blueprints = []
        agent_photo_proposals = []
        agent_warnings = []
        heuristic_reconciliations = []

    output_items = [_clean_staged_item(it) for it in resolved_items] + agent_items
    seen_bp_ids = set()
    output_blueprints = []
    for bp in list(resolved_blueprints.values()) + agent_blueprints:
        bp_id = bp.get("id")
        if bp_id and bp_id not in seen_bp_ids:
            output_blueprints.append(bp)
            seen_bp_ids.add(bp_id)

    photo_proposals_db = []
    for it in resolved_items:
        if it.get("existing_photo_id"):
            colors = _extract_item_colors(it)
            photo_proposals_db.append({
                "item_id": it.get("item_id"),
                "photo_url": f"/api/v1/ingestion/photos/{it['existing_photo_id']}",
                "photo_source": "db_existing",
                "canonical_color_candidate": colors[0] if colors else "unknown",
                "retry_count": 0,
                "_photo_search_ctx": [],
            })
    all_photo_proposals = photo_proposals_db + agent_photo_proposals

    # Enrich incoming items in heuristic reconciliations with photo proposals
    photo_prop_map = {
        p.get("item_id"): p.get("photo_url") or p.get("url")
        for p in all_photo_proposals
        if p.get("item_id") and (p.get("photo_url") or p.get("url"))
    }
    for rec in heuristic_reconciliations:
        for it in rec.get("incoming_items", []) or []:
            it_id = it.get("item_id")
            if it_id in photo_prop_map and not it.get("photo_url"):
                it["photo_url"] = photo_prop_map[it_id]

    # Fail-fast category validation and resolution for new blueprints
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

    return {
        "items": output_items,
        "blueprints": output_blueprints,
        "photo_proposals": all_photo_proposals,
        "heuristic_reconciliations": heuristic_reconciliations,
        "warnings": extraction_warnings + heuristic_warnings + agent_warnings,
        "job_id": job_id,
        "brand_id": brand_id,
    }


async def process_and_stage_pdf(job_id: str, file_path: str, brand_id: str) -> None:
    """
    Fetches the brand hierarchy from the DB using brand_id, retrieves the brand name,
    runs the DataExtractionAgent + ArticleBlueprintsAgent pipeline, and persists the result to the staging area.
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

            categories = category_repo.get_brand_hierarchy(db, brand_id)
            logger.log_execution("data_ingestion_service", "categories_fetched", "ok", macro_category_count=len(categories))

            extraction_agent = DataExtractionAgent()
            logger.log_execution("data_ingestion_service", "extraction_agent_dispatched", "ok", input_summary={"file_path": file_path, "brand": brand_name})
            extraction_res = await extraction_agent.aexecute({
                "file_path": file_path,
                "brand": brand_name,
            })
            extracted_items = extraction_res["items"]
            logger.log_execution("data_ingestion_service", "extraction_agent_completed", "ok")

            result = await _stage_extracted_items(
                db=db,
                job_id=job_id,
                brand_id=brand_id,
                brand_name=brand_name,
                categories=categories,
                heuristics=list(brand_obj.heuristics or []),
                extracted_items=extracted_items,
                extraction_warnings=extraction_res.get("warnings", []),
            )
            staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=result)
            logger.log_execution("data_ingestion_service", "process_and_stage_success", "ok", job_id=job_id)

        except AgentException as exc:
            logger.log_execution("data_ingestion_service", "agent_exception", "err", job_id=job_id, exc=exc, output=exc.output)
            db.rollback()
            staging_repo.update_job(
                db,
                job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "output": exc.output},
            )
            raise
        except Exception as exc:
            logger.log_execution("data_ingestion_service", "process_and_stage_failure", "err", exc=exc, job_id=job_id)
            db.rollback()
            staging_repo.update_job(
                db,
                job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "stage": "agent_execution"},
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
        from src.models.pim import ArticleBlueprint, Category
        missing_bp_ids = [
            bp["id"] for bp in data["blueprints"]
            if isinstance(bp, dict) and not bp.get("is_new", True) and (
                bp.get("article_name") is None
                or bp.get("description") is None
                or bp.get("category") is None
                or bp.get("sub_category") is None
            ) and bp.get("id")
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
                    if (bp.get("category") is None or bp.get("sub_category") is None) and db_bp.category:
                        if db_bp.category.parent_id:
                            parent_cat = db_bp.category.parent or db.query(Category).filter_by(id=db_bp.category.parent_id).first()
                            if parent_cat:
                                bp["category"] = {"id": str(parent_cat.id), "description": parent_cat.name}
                                bp["sub_category"] = {"id": str(db_bp.category.id), "description": db_bp.category.name}
                            elif bp.get("category") is None:
                                bp["category"] = {"id": str(db_bp.category.id), "description": db_bp.category.name}
                        elif bp.get("category") is None:
                            bp["category"] = {"id": str(db_bp.category.id), "description": db_bp.category.name}

    return {
        "status": JobStatus(job.status).name,
        "data": data,
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


def _apply_reconciliations_and_realign(
    db: Session,
    brand_id: str,
    reconciliations: list[dict],
    items: list[EnrichedItemSchema],
) -> None:
    """
    Applica atomicamente gli aggiornamenti all'euristica del brand (NOT_REQUIRED / ACCEPTED / REJECTED)
    e riallinea retroattivamente i codici normalizzati dei blueprint esistenti nel DB,
    eseguendo anche gli eventuali split e merge retroattivi approvati dall'utente (HITL).
    """
    from src.models.pim import ArticleBlueprint, ArticlePhoto
    from sqlalchemy.orm import selectinload

    active_h = heuristic_repo.get_active_heuristic(db, brand_id)
    meta = copy.deepcopy(getattr(active_h, "heuristic_metadata", None) or {})
    rules: list[dict] = list(meta.get("rules") or [])
    negatives: list[list[str]] = list(meta.get("negative_constraints") or [])
    mode: str = getattr(active_h, "heuristic_mode", None) or HeuristicMode.PENDING_COHORT
    confirmed: bool = bool(getattr(active_h, "heuristic_confirmed", False))
    explanation: Optional[str] = getattr(active_h, "explanation", None)
    pattern: str = getattr(active_h, "pattern", "") or ""
    active_color_segment_index: Optional[int] = getattr(active_h, "color_segment_index", None)
    root_len: Optional[int] = meta.get("root_length")
    changed = False
    rules_changed = False

    accepted_splits: list[dict] = []
    rejected_split_bp_ids: set[str] = set()
    accepted_merges: list[dict] = []
    rejected_merges: list[dict] = []

    for rec in reconciliations or []:
        st = rec.get("status") or "NOT_REQUIRED"
        kind = rec.get("kind") or ""

        if st == "REJECTED":
            for pair in rec.get("negative_pairs") or []:
                if pair not in negatives and pair[::-1] not in negatives:
                    negatives.append(pair)
                    changed = True
            src_split_id = rec.get("split_source_blueprint_id") or rec.get("source_blueprint_id")
            if kind == "RETROACTIVE_SPLIT" and src_split_id:
                rejected_split_bp_ids.add(str(src_split_id))
            elif kind == "RETROACTIVE_MERGE":
                rejected_merges.append(rec)
            continue

        if st not in ("NOT_REQUIRED", "ACCEPTED", "CONFIRMED"):
            continue

        if kind == "RETROACTIVE_SPLIT" and st in ("ACCEPTED", "CONFIRMED"):
            accepted_splits.append(rec)
            continue

        if kind == "RETROACTIVE_MERGE" and st in ("ACCEPTED", "CONFIRMED"):
            accepted_merges.append(rec)
            continue

        # Se l'utente ha confermato esplicitamente una riconciliazione, rimuovi eventuali vincoli
        # negativi pregressi tra i codici dell'articolo in ingresso e il blueprint target confermato.
        if st in ("ACCEPTED", "CONFIRMED") and rec.get("merge_target_blueprint_id"):
            target_bp_id = str(rec["merge_target_blueprint_id"])
            inc_codes = {
                vcn.sterilize_separators(it.get("vendor_code"))
                for it in (rec.get("incoming_items") or [])
                if it.get("vendor_code")
            }
            cand_codes: set[str] = set()
            for mb in (rec.get("matched_db_blueprints") or []) + (rec.get("affected_db_blueprints") or []):
                if str(mb.get("id")) == target_bp_id:
                    for v in mb.get("variants") or []:
                        if v.get("supplier_code"):
                            cand_codes.add(vcn.sterilize_separators(v["supplier_code"]))
            if inc_codes and cand_codes:
                new_negatives = []
                for pair in negatives:
                    if len(pair) == 2 and (
                        (pair[0] in inc_codes and pair[1] in cand_codes)
                        or (pair[1] in inc_codes and pair[0] in cand_codes)
                    ):
                        changed = True
                        continue
                    new_negatives.append(pair)
                negatives = new_negatives

        for pair in rec.get("negative_pairs") or []:
            if pair not in negatives and pair[::-1] not in negatives:
                negatives.append(pair)
                changed = True

        color_seg_idx = rec.get("color_segment_index")
        if color_seg_idx is not None:
            active_color_segment_index = color_seg_idx
            changed = True

        if rec.get("mode_update"):
            mode = rec["mode_update"]
            confirmed = mode in HeuristicMode.MASKED_MODES or mode == HeuristicMode.COLOR_AGNOSTIC
            changed = True

        if rec.get("rule_update"):
            rule = rec["rule_update"]
            rules = vcn.merge_rule(rules, rule)
            if rule.get("type") == "DELIMITED" or rec.get("mode_update") == HeuristicMode.COLOR_MASKED_DELIMITED:
                mode = HeuristicMode.COLOR_MASKED_DELIMITED
                if rule.get("color_segment_index") is not None:
                    active_color_segment_index = rule.get("color_segment_index")
            elif rule.get("type") in ("FIXED", "ELASTIC") or rec.get("mode_update") == HeuristicMode.COLOR_MASKED_COMPACT:
                mode = HeuristicMode.COLOR_MASKED_COMPACT
                if rule.get("root_length") is not None:
                    root_len = rule["root_length"]
            elif mode not in (HeuristicMode.COLOR_MASKED_DELIMITED, HeuristicMode.COLOR_MASKED_COMPACT):
                mode = HeuristicMode.COLOR_MASKED
            confirmed = True
            pattern = vcn.rule_to_regex(rule) or pattern
            prop = rec.get("proposed_heuristic_update") or {}
            explanation = prop.get("explanation") or (
                "Le posizioni variabili del codice identificano il colore; il resto identifica il modello."
            )
            changed = True
            rules_changed = True

    h_meta = {"rules": rules, "negative_constraints": negatives}
    if root_len is not None:
        h_meta["root_length"] = root_len

    # Se il brand non ha ancora un'euristica in DB, inizializza in modalità PENDING_COHORT (Lazy Induction N=1)
    if active_h is None:
        active_h = heuristic_repo.create_heuristic(
            db,
            brand_id=brand_id,
            pattern=pattern,
            explanation=explanation or "In attesa di osservare almeno due varianti colore dello stesso modello.",
            commit_changes=False,
            heuristic_mode=mode,
            heuristic_confirmed=confirmed,
            color_segment_index=active_color_segment_index,
            heuristic_metadata=h_meta,
        )
    elif changed:
        heuristic_repo.update_heuristic(
            db,
            heuristic_id=str(active_h.id),
            pattern=pattern,
            explanation=explanation,
            commit_changes=False,
            heuristic_mode=mode,
            heuristic_confirmed=confirmed,
            color_segment_index=active_color_segment_index,
            heuristic_metadata=h_meta,
        )

    if not rules_changed and not accepted_splits and not accepted_merges:
        return

    # Carica tutti i blueprint esistenti del brand con articoli e foto per il riallineamento retroattivo
    db_blueprints = (
        db.query(ArticleBlueprint)
        .options(
            selectinload(ArticleBlueprint.articles),
            selectinload(ArticleBlueprint.photos),
        )
        .filter(ArticleBlueprint.brand_id == brand_id)
        .all()
    )
    bp_by_id: dict[str, ArticleBlueprint] = {str(bp.id): bp for bp in db_blueprints}
    orig_norm_by_id: dict[str, Optional[str]] = {str(bp.id): bp.normalized_vendor_code for bp in db_blueprints}

    # Fase 0: Imposta codici normalizzati temporanei su tutti i blueprint del brand per evitare
    # collisioni intermedie su uq_brand_normalized_vendor_code durante split, merge e riallineamento
    if db_blueprints:
        for bp in db_blueprints:
            bp.normalized_vendor_code = f"__TMP__{bp.id}"
        db.flush()

    # Fase A: Esecuzione degli Split Retroattivi approvati dall'utente (RETROACTIVE_SPLIT == ACCEPTED)
    split_new_bps: list[tuple[ArticleBlueprint, str]] = []
    for sp_rec in accepted_splits:
        src_id = str(sp_rec.get("split_source_blueprint_id") or sp_rec.get("source_blueprint_id") or "")
        source_bp = bp_by_id.get(src_id)
        split_art_ids = {str(aid) for aid in (sp_rec.get("split_article_ids") or []) if aid}
        split_norm = sp_rec.get("split_normalized_code")
        if not source_bp or not split_art_ids or not split_norm:
            continue

        arts_to_move = [a for a in list(source_bp.articles or []) if str(a.id) in split_art_ids]
        if not arts_to_move:
            continue

        new_bp = ArticleBlueprint(
            brand_id=source_bp.brand_id,
            category_id=source_bp.category_id,
            normalized_vendor_code=f"__TMP_SPLIT__{uuid.uuid4()}",
            article_name=source_bp.article_name,
            description=source_bp.description,
            extended_description=source_bp.extended_description,
            dimensions=copy.deepcopy(source_bp.dimensions),
            tags=copy.deepcopy(source_bp.tags),
            materials=copy.deepcopy(source_bp.materials),
            embedding=source_bp.embedding,
        )
        db.add(new_bp)
        db.flush()

        moved_colors = {
            str(c).strip().lower()
            for a in arts_to_move
            for c in (a.colors or [])
            if c and str(c).strip()
        }
        rem_colors = {
            str(c).strip().lower()
            for a in (source_bp.articles or [])
            if str(a.id) not in split_art_ids
            for c in (a.colors or [])
            if c and str(c).strip()
        }

        for a in arts_to_move:
            a.article_blueprint_id = new_bp.id
            a.blueprint = new_bp

        for ph in list(source_bp.photos or []):
            c_name = (ph.canonical_color_name or "").strip().lower()
            if c_name in moved_colors and c_name not in rem_colors:
                ph.article_blueprint_id = new_bp.id
                ph.blueprint = new_bp
            elif c_name in moved_colors and c_name in rem_colors:
                cloned_ph = ArticlePhoto(
                    article_blueprint_id=new_bp.id,
                    canonical_color_name=ph.canonical_color_name,
                    photo_path=ph.photo_path,
                    original_url=ph.original_url,
                )
                db.add(cloned_ph)

        db.flush()
        split_new_bps.append((new_bp, split_norm))
        logger.log_execution(
            "data_ingestion_service",
            "retroactive_split_executed",
            "ok",
            source_blueprint_id=src_id,
            new_blueprint_id=str(new_bp.id),
            split_normalized_code=split_norm,
            moved_article_ids=sorted(split_art_ids),
        )

    # Fase B: Esecuzione dei Merge Retroattivi approvati dall'utente (RETROACTIVE_MERGE == ACCEPTED)
    deleted_bp_ids: set[str] = set()
    for mg_rec in accepted_merges:
        primary_id = str(mg_rec.get("merge_target_blueprint_id") or "")
        sec_id = str(mg_rec.get("merge_secondary_blueprint_id") or mg_rec.get("secondary_blueprint_id") or "")
        primary_bp = bp_by_id.get(primary_id)
        sec_bp = bp_by_id.get(sec_id)
        if not primary_bp or not sec_bp or primary_id == sec_id or sec_id in deleted_bp_ids:
            continue

        for a in list(sec_bp.articles or []):
            a.article_blueprint_id = primary_bp.id
            a.blueprint = primary_bp

        existing_photo_colors = {
            (p.canonical_color_name or "").strip().lower()
            for p in (primary_bp.photos or [])
            if p.canonical_color_name
        }
        for ph in list(sec_bp.photos or []):
            c_name = (ph.canonical_color_name or "").strip().lower()
            if c_name and c_name not in existing_photo_colors:
                ph.article_blueprint_id = primary_bp.id
                ph.blueprint = primary_bp
                existing_photo_colors.add(c_name)
            else:
                db.delete(ph)

        for item in items:
            if item.blueprint_group_id == sec_id:
                item.blueprint_group_id = primary_id

        db.flush()
        db.delete(sec_bp)
        deleted_bp_ids.add(sec_id)
        db.flush()
        logger.log_execution(
            "data_ingestion_service",
            "retroactive_merge_executed",
            "ok",
            primary_blueprint_id=primary_id,
            secondary_blueprint_id=sec_id,
        )

    # Fase C: Assegnazione finale dei codici normalizzati su tutti i blueprint superstiti e nuovi da split
    occupied_final: set[str] = set()

    for new_bp, split_norm in split_new_bps:
        cand_norm = split_norm
        if cand_norm in occupied_final:
            cand_norm = f"{split_norm}__{str(new_bp.id)[:6]}"
        new_bp.normalized_vendor_code = cand_norm
        new_bp.skeleton_vendor_code = vcn.compute_skeleton_code(cand_norm)
        if active_h:
            new_bp.heuristic_id = str(active_h.id)
        occupied_final.add(cand_norm)

    for bp in db_blueprints:
        bp_id_str = str(bp.id)
        if bp_id_str in deleted_bp_ids:
            continue

        orig_norm = orig_norm_by_id.get(bp_id_str)
        if bp_id_str in rejected_split_bp_ids:
            # L'utente ha rifiutato lo split retroattivo di questo blueprint: resta intatto con il suo codice originale
            target_norm = orig_norm if (orig_norm and orig_norm not in occupied_final) else (orig_norm or f"__KEEP__{bp.id}")
            if target_norm in occupied_final:
                target_norm = f"{target_norm}__{bp_id_str[:6]}"
            bp.normalized_vendor_code = target_norm
            bp.skeleton_vendor_code = vcn.compute_skeleton_code(target_norm)
            occupied_final.add(target_norm)
            continue

        var_codes = [
            vcn.sterilize_separators(a.supplier_code)
            for a in (bp.articles or [])
            if a.supplier_code and str(a.article_blueprint_id) == bp_id_str
        ]
        incoming_bp_codes = [
            vcn.sterilize_separators(it.vendor_code)
            for it in (items or [])
            if getattr(it, "vendor_code", None) and str(getattr(it, "blueprint_group_id", "")) == bp_id_str
        ]
        all_bp_codes = var_codes + [c for c in incoming_bp_codes if c and c not in var_codes]
        if not all_bp_codes:
            fallback = orig_norm if (orig_norm and orig_norm not in occupied_final) else f"__EMPTY__{bp.id}"
            bp.normalized_vendor_code = fallback
            bp.skeleton_vendor_code = vcn.compute_skeleton_code(fallback)
            occupied_final.add(fallback)
            continue

        # Privilegia i codici effettivamente mascherati dalle regole attive rispetto al fallback storico
        matched_norms = sorted({
            nc for c in all_bp_codes
            if (nc := vcn.apply_rules(c, rules)) is not None
        })
        if matched_norms:
            target_norm = matched_norms[0]
        else:
            target_norm = orig_norm or all_bp_codes[0]
        if target_norm in occupied_final:
            # Collisione (es. merge retroattivo rifiutato dall'utente o vincolo negativo):
            # preserva il codice originario non collidente o il codice fornitore grezzo
            fallback = orig_norm if (orig_norm and orig_norm not in occupied_final) else var_codes[0]
            if fallback in occupied_final:
                fallback = f"{var_codes[0]}__{bp_id_str[:6]}"
            bp.normalized_vendor_code = fallback
            bp.skeleton_vendor_code = vcn.compute_skeleton_code(fallback)
            occupied_final.add(fallback)
            logger.log_execution(
                "data_ingestion_service",
                "retroactive_realign_kept_distinct_code",
                "ok",
                blueprint_id=bp_id_str,
                target_norm=target_norm,
                assigned_norm=fallback,
            )
        else:
            bp.normalized_vendor_code = target_norm
            bp.skeleton_vendor_code = vcn.compute_skeleton_code(target_norm)
            occupied_final.add(target_norm)
        if active_h:
            bp.heuristic_id = str(active_h.id)

    db.flush()

    if active_h:
        heuristic_repo.recalculate_embeddings_for_heuristic(db, brand_id, str(active_h.id), commit_changes=False)

    # Aggiorna in-memory i codici normalizzati degli items in conferma
    surviving_bp_ids = {str(bp.id) for bp in db_blueprints if str(bp.id) not in deleted_bp_ids} | {
        str(nb.id) for nb, _ in split_new_bps
    }
    for item in items:
        if item.vendor_code:
            raw_clean = vcn.clean_code(item.vendor_code)
            masked = vcn.apply_rules(raw_clean, rules)
            if masked:
                # Se l'item sta creando un NUOVO blueprint ma il codice mascherato è già occupato
                # da un blueprint esistente (es. perché l'utente ha rifiutato l'unione), usa il codice grezzo
                if item.blueprint_group_id not in surviving_bp_ids and masked in occupied_final:
                    item.normalized_vendor_code = raw_clean
                else:
                    item.normalized_vendor_code = masked


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

    reconciliations = (job.data.get("heuristic_reconciliations") or []) if job.data else []
    pending_hitl = [
        r for r in reconciliations
        if r.get("requires_confirmation") and r.get("status") == "PENDING_USER_CONFIRMATION"
    ]
    if pending_hitl:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sono presenti proposte di modifica dell'euristica del brand in attesa di conferma manuale.",
        )

    raw_items = job.data.get("items") or [] if job.data else []
    raw_blueprints = job.data.get("blueprints") or [] if job.data else []

    if not raw_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items to confirm")

    logger.log_execution("data_ingestion_service", "bipartite_merge", "ok", item_count=len(raw_items), blueprint_count=len(raw_blueprints))

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

    brand_id = job.data.get("brand_id") if job.data else None
    if not brand_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Staged job data is missing brand_id")

    # Apply heuristic updates and retroactive blueprint code alignment BEFORE creating new blueprints
    _apply_reconciliations_and_realign(db, brand_id, reconciliations, items)

    resolved_blueprints: Dict[str, str] = {}
    item_photo_context = {}

    for item, raw_it_dict in zip(items, merged_items):
        bp_group_key = item.blueprint_group_id or item.article_name or f"{item.product_short_description}"
        is_new = True
        if bp_group_key in bp_map:
            is_new = bp_map[bp_group_key].get("is_new", True)

        if not is_new:
            blueprint_id = bp_group_key
            if bp_group_key not in resolved_blueprints:
                resolved_blueprints[bp_group_key] = blueprint_id
                logger.log_execution("data_ingestion_service", "blueprint_resolved", "ok", blueprint_key=bp_group_key, blueprint_id=blueprint_id, resolution_status="existing_db")
        else:
            category_id = None
            if item.sub_category and item.sub_category.id:
                category_id = item.sub_category.id
            elif item.category and item.category.id:
                category_id = item.category.id

            if not category_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item is missing a valid category. Confirmation failed.",
                )

            if bp_group_key in resolved_blueprints:
                blueprint_id = resolved_blueprints[bp_group_key]
                logger.log_execution("data_ingestion_service", "blueprint_resolved", "ok", blueprint_key=bp_group_key, blueprint_id=blueprint_id, resolution_status="existing_cluster")
            else:
                matched_h_id = raw_it_dict.get("matched_heuristic_id")
                active_h = heuristic_repo.get_active_heuristic(db, brand_id)
                if not matched_h_id and active_h and (active_h.heuristic_confirmed or active_h.heuristic_mode in HeuristicMode.MASKED_MODES | {HeuristicMode.COLOR_AGNOSTIC}):
                    matched_h_id = str(active_h.id)
                skel_code = vcn.compute_skeleton_code(item.normalized_vendor_code)
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
                    heuristic_id=matched_h_id,
                    skeleton_vendor_code=skel_code,
                    commit_changes=False,
                )
                if matched_h_id:
                    heuristic_repo.recalculate_embeddings_for_heuristic(db, brand_id, matched_h_id, commit_changes=False)
                resolved_blueprints[bp_group_key] = blueprint_id
                logger.log_execution("data_ingestion_service", "blueprint_resolved", "ok", blueprint_key=bp_group_key, blueprint_id=blueprint_id, resolution_status="new")

                bp_obj = pim_repo.get(db, blueprint_id)
                if bp_obj and bp_obj.embedding is None:
                    from src.services.pim_service import build_blueprint_embedding_text
                    text_to_embed = build_blueprint_embedding_text(db, bp_obj)
                    if text_to_embed:
                        try:
                            from src.agents.llm_client import LLMClient
                            emb = LLMClient().generate_embedding_sync(text_to_embed)
                            pim_repo.save_embedding(db, blueprint_id, emb, commit_changes=False)
                        except Exception as exc:
                            print(f"[confirm_and_persist_staging] Warning: Failed to generate embedding for blueprint {blueprint_id}: {exc}")

        if raw_it_dict.get("item_id"):
            colors = item.colors or []
            color_name = colors[0].lower() if colors else "unknown"
            item_photo_context[raw_it_dict["item_id"]] = {"blueprint_id": blueprint_id, "color_name": color_name}

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
                commit_changes=False,
            )

    db.flush()
    active_h = heuristic_repo.get_active_heuristic(db, brand_id)
    if active_h and active_h.heuristic_mode in HeuristicMode.MASKED_MODES:
        heuristic_repo.recalculate_embeddings_for_heuristic(db, brand_id, str(active_h.id), commit_changes=False)

    db.commit()
    logger.log_execution("data_ingestion_service", "confirm_staging_commit", "ok", job_id=job_id)

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
                logger.log_execution("data_ingestion_service", "photo_download_failed", "error", photo_url=photo_url, exc=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Impossibile scaricare la foto per blueprint '{bp_id}' (colore: {color_name}): {exc}",
                )
        db.commit()

    invalidate_search_cache()

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

            categories = category_repo.get_brand_hierarchy(db, request.brand_id)

            extracted_items = [{
                "item_id": str(uuid.uuid4()),
                "vendor_code": request.vendor_code,
                "VendorCode": request.vendor_code,
                "barcode": request.barcode,
                "quantity": request.quantity,
                "colors": request.colors,
                "article_name": request.article_name,
            }]

            # --- Stadio 3: Guardrail Anti-Typo Interattivo (solo SINGLE_ITEM_IMPORT) ---
            if not getattr(request, "typo_warning_confirmed", False):
                from src.models.pim import ArticleBlueprint
                resolved_items, unresolved_items, _, _, _ = _resolve_items_with_photos(
                    db, request.brand_id, extracted_items, list(brand_obj.heuristics or [])
                )
                if unresolved_items:
                    brand_embeddings = heuristic_repo.get_embeddings_by_brand(db, request.brand_id)
                    from sqlalchemy.orm import selectinload
                    brand_blueprints = (
                        db.query(ArticleBlueprint)
                        .options(selectinload(ArticleBlueprint.articles))
                        .filter(ArticleBlueprint.brand_id == request.brand_id)
                        .all()
                    )
                    typo_info = vcn.detect_typo_anomaly(request.vendor_code, brand_embeddings, brand_blueprints)
                    if typo_info:
                        staging_repo.update_job(
                            db,
                            job_id=job_id,
                            status=JobStatus.TYPO_WARNING.value,
                            data=typo_info,
                        )
                        logger.log_execution("data_ingestion_service", "typo_warning_triggered", "ok", job_id=job_id, typo_info=typo_info)
                        return

            result = await _stage_extracted_items(
                db=db,
                job_id=job_id,
                brand_id=request.brand_id,
                brand_name=brand_name,
                categories=categories,
                heuristics=list(brand_obj.heuristics or []),
                extracted_items=extracted_items,
                extraction_warnings=[],
            )
            staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=result)
            logger.log_execution("data_ingestion_service", "process_and_stage_single_item_success", "ok", job_id=job_id)

        except AgentException as exc:
            logger.log_execution("data_ingestion_service", "process_and_stage_single_item_exception", "err", job_id=job_id, exc=exc, output=exc.output)
            db.rollback()
            staging_repo.update_job(
                db,
                job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "output": exc.output},
            )
            raise
        except Exception as exc:
            logger.log_execution("data_ingestion_service", "process_and_stage_single_item_failure", "err", exc=exc, job_id=job_id)
            db.rollback()
            staging_repo.update_job(
                db,
                job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "stage": "agent_execution"},
            )
            raise
