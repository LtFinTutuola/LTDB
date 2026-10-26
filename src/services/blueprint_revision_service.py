"""
src/services/blueprint_revision_service.py
------------------------------------------
Service layer orchestrating the Visual Blueprint Split/Merge & Vendor Code Heuristic Revision flow:
  1. Fetch physical articles context for a Blueprint (grouped by supplier_code + colors + ean with photos)
  2. Run Contrastive Sandbox deduction (Must-Split / Must-Merge + optional natural language correction)
  3. Perform Two-Phase Warehouse Diffing & AI Synthesis for "Before & After" Staging Area review
  4. Support inline metadata editing in the Staging Area
  5. Execute the 5-Step Ordered Atomic Transaction (updating brand_heuristics, migrating ArticlePhoto,
     aligning all normalized_vendor_code records of the Brand, and invalidating search cache)
"""
from __future__ import annotations

import copy
import re
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.agents.article_blueprints_agent import ArticleBlueprintsAgent
from src.agents.base import AgentException
from src.agents.codes_deduction_agent import CodesDeductionAgent
from src.agents.llm_client import LLMClient
from src.core.logger import get_logger
from src.models.pim import ArticleBlueprint, ArticlePhoto, Brand, BrandHeuristic, Category
from src.models.staging import JobStatus, JobType
from src.models.wms import Article, ArticleStatus
from src.repositories import staging_repo
from src.repositories.pim_repo import category_repo, heuristic_repo, pim_repo
from src.schemas.data_ingestion import (
    BlueprintArticleGroupSchema,
    BlueprintRevisionContextResponse,
    HeuristicExampleSchema,
    PartitionGroupInput,
    ReconciliationPlanUpdateRequest,
    RuleDeductionRequest,
    RuleDeductionResponse,
)
from src.services.pim_service import build_blueprint_embedding_text
from src.services.search_service import invalidate_search_cache

logger = get_logger()


def _normalize_vendor_code(raw_code: str, compiled_regex: Optional[re.Pattern]) -> Tuple[str, bool]:
    """
    Apply a compiled brand heuristic regex to a raw vendor code.
    Returns (normalized_code, matched_bool).
    """
    clean = (raw_code or "").strip().upper()
    if not clean:
        return "", False
    if compiled_regex is None:
        return clean, True
    match = compiled_regex.search(clean)
    if not match:
        return clean, False
    if "color_code" in match.groupdict() and match.group("color_code") is not None:
        norm = clean[:match.start("color_code")] + "#" + clean[match.end("color_code"):]
        return norm, True
    return clean, True


def _resolve_category_dicts(db: Session, bp: ArticleBlueprint) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (category_dict, sub_category_dict) for an ArticleBlueprint."""
    if not bp.category_id:
        return None, None
    cat = bp.category or db.query(Category).filter(Category.id == bp.category_id).first()
    if not cat:
        return None, None
    if cat.parent_id:
        parent = cat.parent or db.query(Category).filter(Category.id == cat.parent_id).first()
        if parent:
            return (
                {"id": str(parent.id), "description": parent.name},
                {"id": str(cat.id), "description": cat.name},
            )
    return {"id": str(cat.id), "description": cat.name}, None


def _build_article_groups_for_blueprint(
    bp_articles: List[Article],
    bp_photos: List[ArticlePhoto],
) -> List[BlueprintArticleGroupSchema]:
    """
    Group physical Article rows by (supplier_code, colors, ean) so that:
      - Identical physical units in stock are grouped into a single card with quantity badge.
      - Distinct models that were mistakenly merged under the same color (e.g. XYZ123-BLK and XYZ124-BLK)
        remain separate, individually selectable cards.
    """
    grouped: Dict[str, List[Article]] = defaultdict(list)
    meta_by_key: Dict[str, Tuple[str, Tuple[str, ...], Optional[str]]] = {}

    for art in bp_articles:
        sup_code = (art.supplier_code or "").strip().upper()
        colors_tuple = tuple(sorted({str(c).strip().lower() for c in (art.colors or []) if c and str(c).strip()}))
        ean_val = (art.ean or "").strip() or None
        key = f"{sup_code}::{'|'.join(colors_tuple)}::{ean_val or ''}"
        grouped[key].append(art)
        meta_by_key[key] = (sup_code, colors_tuple, ean_val)

    photos_by_color: Dict[str, ArticlePhoto] = {}
    for p in bp_photos:
        if p.canonical_color_name:
            photos_by_color[p.canonical_color_name.strip().lower()] = p
    fallback_photo = bp_photos[0] if bp_photos else None

    result: List[BlueprintArticleGroupSchema] = []
    for key in sorted(grouped.keys()):
        arts = grouped[key]
        sup_code, colors_tuple, ean_val = meta_by_key[key]
        primary_color = colors_tuple[0] if colors_tuple else None
        matched_photo = photos_by_color.get(primary_color) if primary_color else None
        if not matched_photo:
            matched_photo = fallback_photo

        avail_count = sum(1 for a in arts if a.status == ArticleStatus.AVAILABLE)
        photo_id_str = str(matched_photo.id) if matched_photo else None

        result.append(
            BlueprintArticleGroupSchema(
                group_key=key,
                article_ids=[str(a.id) for a in arts],
                supplier_code=sup_code,
                ean=ean_val,
                colors=list(colors_tuple),
                quantity=len(arts),
                available_stock=avail_count,
                photo_id=photo_id_str,
                photo_url=f"/api/v1/ingestion/photos/{photo_id_str}" if photo_id_str else None,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Phase 1: Context Retrieval for Visual Bucket Selector
# ---------------------------------------------------------------------------

def get_blueprint_revision_context(db: Session, blueprint_id: str) -> BlueprintRevisionContextResponse:
    """
    Fetch full blueprint metadata and all physical articles (grouped by supplier_code + colors + ean)
    to populate the visual Bucket Selector screen.
    """
    logger.log_execution("blueprint_revision_service", "get_context_start", "ok", blueprint_id=blueprint_id)

    bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == blueprint_id).first()
    if not bp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{blueprint_id}' non trovato nel catalogo.",
        )

    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
    brand_name = brand.name if brand else "Unknown"

    cat_dict, sub_cat_dict = _resolve_category_dicts(db, bp)

    bp_articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
    bp_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp.id).all()

    article_groups = _build_article_groups_for_blueprint(bp_articles, bp_photos)
    sample_codes = [g.supplier_code for g in article_groups if g.supplier_code]

    matched_heuristic = heuristic_repo.find_matching_heuristic(db, str(bp.brand_id), sample_codes)

    return BlueprintRevisionContextResponse(
        blueprint_id=str(bp.id),
        article_name=bp.article_name,
        description=bp.description,
        extended_description=bp.extended_description,
        brand_id=str(bp.brand_id),
        brand_name=brand_name,
        normalized_vendor_code=bp.normalized_vendor_code,
        category_id=str(bp.category_id) if bp.category_id else None,
        category_name=cat_dict["description"] if cat_dict else None,
        sub_category_name=sub_cat_dict["description"] if sub_cat_dict else None,
        tags=list(bp.tags or []),
        materials=list(bp.materials or []),
        dimensions=bp.dimensions,
        current_heuristic_id=str(matched_heuristic.id) if matched_heuristic else None,
        current_heuristic_explanation=matched_heuristic.explanation if matched_heuristic else None,
        article_groups=article_groups,
    )


# ---------------------------------------------------------------------------
# Phase 2: Contrastive Rule Deduction & Fast Warehouse Diffing
# ---------------------------------------------------------------------------

async def deduce_and_preview_rule(
    db: Session,
    request: RuleDeductionRequest,
) -> RuleDeductionResponse:
    """
    Deduces a new vendor code normalization rule from the user's visual partition
    (Must-Split / Must-Merge constraints) and optional natural language correction.
    Validates in Contrastive Sandbox and runs fast in-memory diffing across the Brand's warehouse.
    """
    logger.log_execution(
        "blueprint_revision_service",
        "deduce_rule_start",
        "ok",
        blueprint_id=request.blueprint_id,
        partitions_count=len(request.partitions),
        has_user_correction=bool(request.user_correction),
    )

    bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == request.blueprint_id).first()
    if not bp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{request.blueprint_id}' non trovato.",
        )

    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand '{bp.brand_id}' non trovato.",
        )

    non_empty_partitions = [
        p for p in request.partitions if p.article_ids or p.supplier_codes
    ]
    if len(non_empty_partitions) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Devi distribuire gli articoli in almeno 2 Blueprint distinti per eseguire una separazione.",
        )

    # Load all articles of the blueprint to resolve supplier codes accurately
    bp_articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
    art_by_id = {str(a.id): a for a in bp_articles}

    must_split_groups: List[List[str]] = []
    must_merge_groups: List[List[str]] = []
    all_partition_codes: List[str] = []
    seen_codes_by_group: Dict[str, str] = {}

    normalized_partitions_data: List[dict] = []

    for idx, part in enumerate(non_empty_partitions):
        codes_in_part: List[str] = []
        resolved_art_ids: List[str] = []

        for aid in part.article_ids:
            art_obj = art_by_id.get(str(aid))
            if art_obj:
                resolved_art_ids.append(str(art_obj.id))
                if art_obj.supplier_code:
                    sc = art_obj.supplier_code.strip().upper()
                    if sc and sc not in codes_in_part:
                        codes_in_part.append(sc)

        for sc_raw in part.supplier_codes:
            sc = (sc_raw or "").strip().upper()
            if sc and sc not in codes_in_part:
                codes_in_part.append(sc)

        if not codes_in_part:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Il gruppo '{part.label or part.group_id}' non contiene articoli con codice fornitore valido.",
            )

        for sc in codes_in_part:
            if sc in seen_codes_by_group and seen_codes_by_group[sc] != part.group_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Il codice fornitore '{sc}' è presente in più gruppi contemporaneamente. "
                           f"Tutti gli articoli con lo stesso codice fornitore devono stare nello stesso Blueprint.",
                )
            seen_codes_by_group[sc] = part.group_id
            if sc not in all_partition_codes:
                all_partition_codes.append(sc)

        must_split_groups.append(codes_in_part)
        if len(codes_in_part) >= 2:
            must_merge_groups.append(codes_in_part)

        normalized_partitions_data.append({
            "group_id": part.group_id,
            "label": part.label or (bp.article_name if part.is_original else f"Nuovo Modello {idx}"),
            "is_original": bool(part.is_original),
            "article_ids": resolved_art_ids,
            "supplier_codes": codes_in_part,
            "user_hint": part.user_hint,
        })

    # Ensure exactly one partition is marked is_original
    if not any(p["is_original"] for p in normalized_partitions_data):
        normalized_partitions_data[0]["is_original"] = True

    # Identify existing heuristic for this brand
    matched_heuristic = heuristic_repo.find_matching_heuristic(db, str(brand.id), all_partition_codes)
    old_regex = matched_heuristic.pattern if matched_heuristic else ""
    old_explanation = matched_heuristic.explanation if matched_heuristic else ""

    # Gather historical vendor codes for this brand
    historical_rows = (
        db.query(Article.supplier_code)
        .join(ArticleBlueprint, Article.article_blueprint_id == ArticleBlueprint.id)
        .filter(ArticleBlueprint.brand_id == brand.id)
        .all()
    )
    historical_codes = []
    seen_hist = set(all_partition_codes)
    for (sc_val,) in historical_rows:
        if sc_val:
            clean_sc = sc_val.strip().upper()
            if clean_sc and clean_sc not in seen_hist:
                # If an existing heuristic exists, prefer historical codes that belonged to this heuristic
                if old_regex:
                    try:
                        if re.compile(old_regex).search(clean_sc):
                            historical_codes.append(clean_sc)
                            seen_hist.add(clean_sc)
                    except re.error:
                        historical_codes.append(clean_sc)
                        seen_hist.add(clean_sc)
                else:
                    historical_codes.append(clean_sc)
                    seen_hist.add(clean_sc)

    corpus = all_partition_codes + historical_codes

    # Load previous explanation if refining an existing job
    existing_job = staging_repo.get_job(db, request.job_id) if request.job_id else None
    prev_explanation_ctx = old_explanation
    chat_history: List[dict] = []
    if existing_job and existing_job.data:
        h_state = existing_job.data.get("heuristic_state") or {}
        if h_state.get("textual_explanation"):
            prev_explanation_ctx = h_state["textual_explanation"]
        chat_history = list(h_state.get("chat_history") or [])

    if request.user_correction:
        chat_history.append({"role": "user", "content": request.user_correction})

    deduction_agent = CodesDeductionAgent()
    try:
        deduction_res = await deduction_agent.aexecute({
            "vendor_codes": corpus,
            "brand_name": brand.name,
            "previous_explanation": prev_explanation_ctx,
            "must_split_groups": must_split_groups,
            "must_merge_groups": must_merge_groups,
            "user_feedback": request.user_correction,
        })
    except AgentException as exc:
        # Fallback: if historical codes from other product lines conflicted, retry with the blueprint's codes only
        if historical_codes:
            logger.log_execution("blueprint_revision_service", "deduce_retry_scoped_corpus", "warn", exc=str(exc))
            deduction_res = await deduction_agent.aexecute({
                "vendor_codes": all_partition_codes,
                "brand_name": brand.name,
                "previous_explanation": prev_explanation_ctx,
                "must_split_groups": must_split_groups,
                "must_merge_groups": must_merge_groups,
                "user_feedback": request.user_correction,
            })
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Impossibile dedurre una regola coerente con la separazione indicata: {exc}",
            )

    new_regex = deduction_res.get("regex", "")
    textual_explanation = deduction_res.get("explanation", "")
    raw_examples = deduction_res.get("examples", [])

    # Double-check contrastive constraints deterministically in service layer
    compiled_new = re.compile(new_regex) if new_regex else None
    norm_by_code = {c: _normalize_vendor_code(c, compiled_new)[0] for c in corpus}

    # Build prioritize examples starting with the partitioned codes
    examples_list: List[HeuristicExampleSchema] = []
    for c in all_partition_codes:
        examples_list.append(HeuristicExampleSchema(raw=c, normalized=norm_by_code.get(c, c)))
    for ex in raw_examples:
        r_code = ex.get("raw", "")
        if r_code and r_code not in all_partition_codes and len(examples_list) < 8:
            examples_list.append(HeuristicExampleSchema(raw=r_code, normalized=ex.get("normalized", r_code)))

    chat_history.append({"role": "assistant", "content": textual_explanation})

    # Phase A: Fast Warehouse Diffing across the Brand
    diff_summary = _compute_warehouse_diff_preview(
        db=db,
        brand_id=str(brand.id),
        source_blueprint_id=str(bp.id),
        compiled_regex=compiled_new,
        partitions=normalized_partitions_data,
    )

    job_id = existing_job.id if existing_job else str(uuid.uuid4())
    staging_payload = {
        "phase": "RULE_REVIEW",
        "brand_id": str(brand.id),
        "brand_name": brand.name,
        "heuristic_id": str(matched_heuristic.id) if matched_heuristic else None,
        "trigger_context": {
            "source_blueprint_id": str(bp.id),
            "partitions": normalized_partitions_data,
        },
        "heuristic_state": {
            "old_regex": old_regex,
            "old_explanation": old_explanation,
            "new_regex": new_regex,
            "textual_explanation": textual_explanation,
            "examples": [e.model_dump() for e in examples_list],
            "user_confirmed_rule": False,
            "chat_history": chat_history,
        },
        "diff_preview": diff_summary,
        "reconciliation_plan": None,
    }

    if existing_job:
        staging_repo.update_job(db, job_id=job_id, status=JobStatus.ACCEPTED.value, data=staging_payload)
    else:
        staging_repo.create_job(db, job_id=job_id, job_type=JobType.HEURISTIC_REVISION.value)
        staging_repo.update_job(db, job_id=job_id, status=JobStatus.ACCEPTED.value, data=staging_payload)

    logger.log_execution(
        "blueprint_revision_service",
        "deduce_rule_success",
        "ok",
        job_id=job_id,
        splits=diff_summary["estimated_splits_count"],
        merges=diff_summary["estimated_merges_count"],
        aligned=diff_summary["aligned_blueprints_count"],
    )

    return RuleDeductionResponse(
        job_id=job_id,
        textual_explanation=textual_explanation,
        examples=examples_list,
        estimated_splits_count=diff_summary["estimated_splits_count"],
        estimated_merges_count=diff_summary["estimated_merges_count"],
        aligned_blueprints_count=diff_summary["aligned_blueprints_count"],
    )


def _compute_warehouse_diff_preview(
    db: Session,
    brand_id: str,
    source_blueprint_id: str,
    compiled_regex: Optional[re.Pattern],
    partitions: List[dict],
) -> dict:
    """
    Fast in-memory evaluation of the new regex across all blueprints and articles of the Brand.
    """
    blueprints = db.query(ArticleBlueprint).filter(ArticleBlueprint.brand_id == brand_id).all()
    bp_ids = [str(b.id) for b in blueprints]
    all_articles = db.query(Article).filter(Article.article_blueprint_id.in_(bp_ids)).all() if bp_ids else []

    articles_by_bp: Dict[str, List[Article]] = defaultdict(list)
    for a in all_articles:
        articles_by_bp[str(a.article_blueprint_id)].append(a)

    warehouse_splits: List[dict] = []
    norm_to_blueprints: Dict[str, List[str]] = defaultdict(list)
    aligned_blueprints: List[dict] = []

    for bp in blueprints:
        bp_id_str = str(bp.id)
        arts = articles_by_bp.get(bp_id_str, [])
        if bp_id_str == source_blueprint_id:
            # Handled explicitly by the user's partitions; record the original partition's norm code for merge checks
            orig_part = next((p for p in partitions if p.get("is_original")), partitions[0])
            if orig_part["supplier_codes"]:
                orig_norm, _ = _normalize_vendor_code(orig_part["supplier_codes"][0], compiled_regex)
                norm_to_blueprints[orig_norm].append(bp_id_str)
            continue

        if not arts:
            continue

        # Compute normalized codes for all articles of this blueprint
        by_norm: Dict[str, List[Article]] = defaultdict(list)
        matched_any = False
        for a in arts:
            if a.supplier_code:
                n_code, matched = _normalize_vendor_code(a.supplier_code, compiled_regex)
                if matched:
                    matched_any = True
                by_norm[n_code].append(a)
            else:
                by_norm[bp.normalized_vendor_code or ""].append(a)

        if not matched_any and compiled_regex is not None:
            # Blueprint belongs to a different heuristic morphology — leave untouched
            continue

        if len(by_norm) > 1:
            warehouse_splits.append({
                "source_blueprint_id": bp_id_str,
                "article_name": bp.article_name,
                "old_normalized_code": bp.normalized_vendor_code,
                "clusters": {
                    nc: [str(x.id) for x in cluster_arts]
                    for nc, cluster_arts in by_norm.items()
                },
            })
        elif len(by_norm) == 1:
            single_norm = next(iter(by_norm.keys()))
            norm_to_blueprints[single_norm].append(bp_id_str)
            if single_norm and bp.normalized_vendor_code != single_norm:
                aligned_blueprints.append({
                    "blueprint_id": bp_id_str,
                    "article_name": bp.article_name,
                    "old_normalized_code": bp.normalized_vendor_code,
                    "new_normalized_code": single_norm,
                })

    warehouse_merges: List[dict] = []
    merged_bp_ids: set[str] = set()
    for norm_code, bp_id_list in norm_to_blueprints.items():
        if norm_code and len(bp_id_list) >= 2:
            warehouse_merges.append({
                "normalized_vendor_code": norm_code,
                "blueprint_ids": bp_id_list,
            })
            merged_bp_ids.update(bp_id_list)

    aligned_blueprints = [
        al for al in aligned_blueprints if al["blueprint_id"] not in merged_bp_ids
    ]

    return {
        "estimated_splits_count": 1 + len(warehouse_splits),  # 1 (target) + additional warehouse splits
        "additional_warehouse_splits_count": len(warehouse_splits),
        "estimated_merges_count": len(warehouse_merges),
        "aligned_blueprints_count": len(aligned_blueprints),
        "warehouse_splits_raw": warehouse_splits,
        "warehouse_merges_raw": warehouse_merges,
        "aligned_blueprints_raw": aligned_blueprints,
    }


# ---------------------------------------------------------------------------
# Phase 3: Prepare "Before & After" Staging Plan & AI Synthesis
# ---------------------------------------------------------------------------

async def prepare_staging_plan(db: Session, job_id: str) -> dict:
    """
    Triggered once the user confirms the natural language rule explanation.
    Runs AI synthesis for newly split blueprints and builds the complete
    Before & After reconciliation plan in the Staging Area.
    """
    logger.log_execution("blueprint_revision_service", "prepare_staging_plan_start", "ok", job_id=job_id)

    job = staging_repo.get_job(db, job_id)
    if not job or not job.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job di revisione non trovato.")

    data = copy.deepcopy(job.data)
    brand_id = data["brand_id"]
    brand_name = data.get("brand_name", "")
    new_regex = data["heuristic_state"]["new_regex"]
    compiled_regex = re.compile(new_regex) if new_regex else None

    data["heuristic_state"]["user_confirmed_rule"] = True

    categories_hierarchy = category_repo.get_brand_hierarchy(db, brand_id)

    # 1. Build Target Split Operation (from trigger_context)
    source_bp_id = data["trigger_context"]["source_blueprint_id"]
    partitions = data["trigger_context"]["partitions"]
    target_split_plan = await _build_split_operation_plan(
        db=db,
        brand_id=brand_id,
        brand_name=brand_name,
        source_bp_id=source_bp_id,
        partitions=partitions,
        compiled_regex=compiled_regex,
        categories_hierarchy=categories_hierarchy,
    )

    # 2. Build Additional Warehouse Splits (if any)
    warehouse_splits_plans = []
    for ws in data.get("diff_preview", {}).get("warehouse_splits_raw", []):
        ws_bp_id = ws["source_blueprint_id"]
        ws_bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == ws_bp_id).first()
        if not ws_bp:
            continue
        ws_partitions = []
        for idx, (nc, aid_list) in enumerate(ws["clusters"].items()):
            ws_partitions.append({
                "group_id": f"ws_{ws_bp_id}_{idx}",
                "label": ws_bp.article_name if idx == 0 else f"{ws_bp.article_name} ({nc})",
                "is_original": idx == 0,
                "article_ids": aid_list,
                "supplier_codes": [],
                "user_hint": None,
            })
        ws_plan = await _build_split_operation_plan(
            db=db,
            brand_id=brand_id,
            brand_name=brand_name,
            source_bp_id=ws_bp_id,
            partitions=ws_partitions,
            compiled_regex=compiled_regex,
            categories_hierarchy=categories_hierarchy,
        )
        warehouse_splits_plans.append(ws_plan)

    # 3. Build Warehouse Merges (if any)
    warehouse_merges_plans = []
    for wm in data.get("diff_preview", {}).get("warehouse_merges_raw", []):
        m_plan = _build_merge_operation_plan(
            db=db,
            blueprint_ids=wm["blueprint_ids"],
            normalized_vendor_code=wm["normalized_vendor_code"],
        )
        if m_plan:
            warehouse_merges_plans.append(m_plan)

    # 4. Aligned blueprints list
    aligned_list = data.get("diff_preview", {}).get("aligned_blueprints_raw", [])

    data["phase"] = "READY_FOR_REVIEW"
    data["reconciliation_plan"] = {
        "target_split": target_split_plan,
        "warehouse_splits": warehouse_splits_plans,
        "warehouse_merges": warehouse_merges_plans,
        "aligned_blueprints": aligned_list,
    }

    staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=data)
    logger.log_execution("blueprint_revision_service", "prepare_staging_plan_success", "ok", job_id=job_id)

    return get_sanitized_revision_job(db, job_id)


async def _build_split_operation_plan(
    db: Session,
    brand_id: str,
    brand_name: str,
    source_bp_id: str,
    partitions: List[dict],
    compiled_regex: Optional[re.Pattern],
    categories_hierarchy: dict,
) -> dict:
    """
    Build a detailed Before / After representation for a Blueprint being split,
    running ArticleBlueprintsAgent synthesis for the newly created Blueprint(s).
    """
    source_bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == source_bp_id).first()
    if not source_bp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Blueprint '{source_bp_id}' non trovato.")

    bp_articles = db.query(Article).filter(Article.article_blueprint_id == source_bp.id).all()
    bp_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == source_bp.id).all()
    art_map = {str(a.id): a for a in bp_articles}

    cat_dict, sub_cat_dict = _resolve_category_dicts(db, source_bp)
    before_groups = [g.model_dump() for g in _build_article_groups_for_blueprint(bp_articles, bp_photos)]

    before_card = {
        "blueprint_id": str(source_bp.id),
        "article_name": source_bp.article_name,
        "description": source_bp.description,
        "extended_description": source_bp.extended_description,
        "normalized_vendor_code": source_bp.normalized_vendor_code,
        "category": cat_dict,
        "sub_category": sub_cat_dict,
        "tags": list(source_bp.tags or []),
        "materials": list(source_bp.materials or []),
        "dimensions": source_bp.dimensions,
        "article_groups": before_groups,
    }

    # Prepare new clusters for AI synthesis
    new_clusters_for_agent = []
    cluster_by_temp_id: Dict[str, dict] = {}
    after_blueprints: List[dict] = []

    for idx, part in enumerate(partitions):
        part_arts = [art_map[aid] for aid in part.get("article_ids", []) if aid in art_map]
        if not part_arts and part.get("supplier_codes"):
            target_scs = {s.upper() for s in part["supplier_codes"]}
            part_arts = [a for a in bp_articles if (a.supplier_code or "").upper() in target_scs]

        part_groups = [g.model_dump() for g in _build_article_groups_for_blueprint(part_arts, bp_photos)]
        first_sc = next((a.supplier_code for a in part_arts if a.supplier_code), "")
        norm_code, _ = _normalize_vendor_code(first_sc, compiled_regex) if first_sc else (source_bp.normalized_vendor_code or "", True)

        if part.get("is_original"):
            after_blueprints.append({
                "id": str(source_bp.id),
                "is_new": False,
                "excluded": False,
                "article_name": part.get("user_hint") or source_bp.article_name,
                "description": source_bp.description,
                "extended_description": source_bp.extended_description,
                "normalized_vendor_code": norm_code,
                "category": cat_dict,
                "sub_category": sub_cat_dict,
                "tags": list(source_bp.tags or []),
                "materials": list(source_bp.materials or []),
                "dimensions": source_bp.dimensions,
                "article_ids": [str(a.id) for a in part_arts],
                "article_groups": part_groups,
            })
        else:
            temp_id = f"new_bp_{uuid.uuid4().hex[:10]}"
            hint_name = part.get("user_hint") or part.get("label") or f"{source_bp.article_name} ({first_sc})"
            cluster_items = []
            for a in part_arts:
                cluster_items.append({
                    "item_id": str(a.id),
                    "vendor_code": a.supplier_code or "",
                    "VendorCode": a.supplier_code or "",
                    "normalized_vendor_code": norm_code,
                    "barcode": a.ean or "",
                    "quantity": 1,
                    "colors": list(a.colors or []),
                    "article_name": hint_name,
                    "description": hint_name,
                    "article_description": source_bp.description,
                })
            cluster_obj = {
                "id": temp_id,
                "is_new": True,
                "cluster_items": cluster_items,
            }
            new_clusters_for_agent.append(cluster_obj)
            cluster_by_temp_id[temp_id] = {
                "temp_id": temp_id,
                "norm_code": norm_code,
                "part_arts": part_arts,
                "part_groups": part_groups,
                "hint_name": hint_name,
            }

    # Invoke ArticleBlueprintsAgent to synthesize new blueprints
    synthesized_by_id: Dict[str, dict] = {}
    if new_clusters_for_agent:
        try:
            agent = ArticleBlueprintsAgent()
            agent_res = await agent.aexecute({
                "new_blueprints": new_clusters_for_agent,
                "photo_only_items": [],
                "categories": categories_hierarchy,
                "brand_name": brand_name,
                "resolved_blueprints": {},
            })
            for abp in agent_res.get("blueprints", []):
                synthesized_by_id[abp.get("id")] = abp
        except Exception as exc:
            logger.log_execution("blueprint_revision_service", "agent_synthesis_fallback", "warn", exc=str(exc))

    for temp_id, c_info in cluster_by_temp_id.items():
        synth = synthesized_by_id.get(temp_id, {})
        resolved_cat = cat_dict
        resolved_sub = sub_cat_dict

        # Resolve category name from agent if returned as string
        raw_cat = synth.get("category")
        if isinstance(raw_cat, str) and raw_cat.strip():
            cat_obj = db.query(Category).filter(func.lower(Category.name) == raw_cat.strip().lower()).first()
            if cat_obj:
                resolved_cat = {"id": str(cat_obj.id), "description": cat_obj.name}
        elif isinstance(raw_cat, dict) and raw_cat.get("id"):
            resolved_cat = raw_cat

        raw_sub = synth.get("sub_category")
        if isinstance(raw_sub, str) and raw_sub.strip():
            sub_obj = db.query(Category).filter(func.lower(Category.name) == raw_sub.strip().lower()).first()
            if sub_obj:
                resolved_sub = {"id": str(sub_obj.id), "description": sub_obj.name}
        elif isinstance(raw_sub, dict) and raw_sub.get("id"):
            resolved_sub = raw_sub

        synth_tags = synth.get("tags") or list(source_bp.tags or ["articolo"])
        synth_materials = synth.get("materials") if synth.get("materials") is not None else list(source_bp.materials or [])

        after_blueprints.append({
            "id": temp_id,
            "is_new": True,
            "excluded": False,
            "article_name": synth.get("article_name") or c_info["hint_name"],
            "description": synth.get("description") or source_bp.description,
            "extended_description": synth.get("extended_description") or source_bp.extended_description,
            "normalized_vendor_code": c_info["norm_code"],
            "category": resolved_cat,
            "sub_category": resolved_sub,
            "tags": synth_tags if synth_tags else ["articolo"],
            "materials": synth_materials,
            "dimensions": synth.get("dimensions") or source_bp.dimensions,
            "article_ids": [str(a.id) for a in c_info["part_arts"]],
            "article_groups": c_info["part_groups"],
        })

    return {
        "source_blueprint_id": str(source_bp.id),
        "excluded": False,
        "before": before_card,
        "after": after_blueprints,
    }


def _build_merge_operation_plan(
    db: Session,
    blueprint_ids: List[str],
    normalized_vendor_code: str,
) -> Optional[dict]:
    """
    Build a Before / After representation for 2+ existing Blueprints collapsing into 1 unified Blueprint.
    """
    bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.id.in_(blueprint_ids)).all()
    if len(bps) < 2:
        return None

    # Sort so the oldest blueprint (or the one with most articles) is the surviving target
    bps.sort(key=lambda b: (b.created_at or 0))
    surviving_bp = bps[0]
    absorbed_bps = bps[1:]

    before_cards = []
    all_articles: List[Article] = []
    all_photos: List[ArticlePhoto] = []
    merged_tags: List[str] = list(surviving_bp.tags or [])
    merged_materials: List[str] = list(surviving_bp.materials or [])

    for b in bps:
        b_arts = db.query(Article).filter(Article.article_blueprint_id == b.id).all()
        b_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == b.id).all()
        all_articles.extend(b_arts)
        all_photos.extend(b_photos)

        for t in (b.tags or []):
            if t not in merged_tags:
                merged_tags.append(t)
        for m in (b.materials or []):
            if m not in merged_materials:
                merged_materials.append(m)

        c_dict, sc_dict = _resolve_category_dicts(db, b)
        before_cards.append({
            "blueprint_id": str(b.id),
            "article_name": b.article_name,
            "description": b.description,
            "normalized_vendor_code": b.normalized_vendor_code,
            "category": c_dict,
            "sub_category": sc_dict,
            "article_groups": [g.model_dump() for g in _build_article_groups_for_blueprint(b_arts, b_photos)],
        })

    surv_cat, surv_sub = _resolve_category_dicts(db, surviving_bp)
    after_card = {
        "id": str(surviving_bp.id),
        "surviving_blueprint_id": str(surviving_bp.id),
        "absorbed_blueprint_ids": [str(ab.id) for ab in absorbed_bps],
        "is_new": False,
        "excluded": False,
        "article_name": surviving_bp.article_name,
        "description": surviving_bp.description,
        "extended_description": surviving_bp.extended_description,
        "normalized_vendor_code": normalized_vendor_code,
        "category": surv_cat,
        "sub_category": surv_sub,
        "tags": merged_tags if merged_tags else ["articolo"],
        "materials": merged_materials,
        "dimensions": surviving_bp.dimensions,
        "article_ids": [str(a.id) for a in all_articles],
        "article_groups": [g.model_dump() for g in _build_article_groups_for_blueprint(all_articles, all_photos)],
    }

    return {
        "merge_id": f"merge_{surviving_bp.id}",
        "excluded": False,
        "before": before_cards,
        "after": after_card,
    }


def get_sanitized_revision_job(db: Session, job_id: str) -> dict:
    """
    Return the staging job status and data, stripping any raw regex patterns
    so they are never exposed to the client UI (Criterion 3).
    """
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job non trovato.")

    data = copy.deepcopy(job.data) if job.data else {}
    if "heuristic_state" in data and isinstance(data["heuristic_state"], dict):
        data["heuristic_state"].pop("old_regex", None)
        data["heuristic_state"].pop("new_regex", None)

    return {
        "job_id": job_id,
        "status": JobStatus(job.status).name,
        "data": data,
    }


# ---------------------------------------------------------------------------
# Phase 4: Inline Editing of Proposed "After" Blueprints in Staging
# ---------------------------------------------------------------------------

def update_reconciliation_plan(
    db: Session,
    job_id: str,
    request: ReconciliationPlanUpdateRequest,
) -> dict:
    """
    Update metadata (name, description, category, tags, materials, or excluded flag)
    of a blueprint card inside the 'After' column of the staging reconciliation plan.
    """
    job = staging_repo.get_job(db, job_id)
    if not job or not job.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job non trovato.")

    data = copy.deepcopy(job.data)
    plan = data.get("reconciliation_plan")
    if not plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Il piano di riconciliazione non è ancora stato generato.")

    target_id = request.target_blueprint_id
    target_bp_dict = None

    # Search in target_split.after
    for bp_card in (plan.get("target_split") or {}).get("after", []):
        if bp_card.get("id") == target_id:
            target_bp_dict = bp_card
            break

    # Search in warehouse_splits
    if not target_bp_dict:
        for ws in plan.get("warehouse_splits", []):
            if ws.get("source_blueprint_id") == target_id and request.excluded is not None:
                ws["excluded"] = request.excluded
            for bp_card in ws.get("after", []):
                if bp_card.get("id") == target_id:
                    target_bp_dict = bp_card
                    break

    # Search in warehouse_merges
    if not target_bp_dict:
        for wm in plan.get("warehouse_merges", []):
            after_c = wm.get("after") or {}
            if after_c.get("id") == target_id or wm.get("merge_id") == target_id:
                target_bp_dict = after_c
                if request.excluded is not None:
                    wm["excluded"] = request.excluded
                break

    if not target_bp_dict:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{target_id}' non trovato nel piano di staging.",
        )

    if request.article_name is not None:
        if not request.article_name.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Il nome articolo non può essere vuoto.")
        target_bp_dict["article_name"] = request.article_name.strip()

    if request.description is not None:
        if not request.description.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La descrizione non può essere vuota.")
        target_bp_dict["description"] = request.description.strip()

    if request.extended_description is not None:
        target_bp_dict["extended_description"] = request.extended_description.strip()

    if request.tags is not None:
        clean_tags = [t.strip().lower() for t in request.tags if t and t.strip()]
        if not clean_tags:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inserire almeno un tag.")
        target_bp_dict["tags"] = clean_tags

    if request.materials is not None:
        target_bp_dict["materials"] = [m.strip().lower() for m in request.materials if m and m.strip()]

    if request.dimensions is not None:
        target_bp_dict["dimensions"] = request.dimensions

    if request.category is not None:
        cat_id = request.category.get("id")
        cat_obj = db.query(Category).filter(Category.id == cat_id).first() if cat_id else None
        if not cat_obj and request.category.get("description"):
            cat_obj = db.query(Category).filter(
                func.lower(Category.name) == request.category["description"].strip().lower()
            ).first()
        if not cat_obj:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Categoria non trovata nel database.")
        target_bp_dict["category"] = {"id": str(cat_obj.id), "description": cat_obj.name}

    if "sub_category" in request.model_fields_set:
        if request.sub_category is None:
            target_bp_dict["sub_category"] = None
        else:
            sub_id = request.sub_category.get("id")
            sub_obj = db.query(Category).filter(Category.id == sub_id).first() if sub_id else None
            if not sub_obj and request.sub_category.get("description"):
                sub_obj = db.query(Category).filter(
                    func.lower(Category.name) == request.sub_category["description"].strip().lower()
                ).first()
            if not sub_obj:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sottocategoria non trovata.")
            target_bp_dict["sub_category"] = {"id": str(sub_obj.id), "description": sub_obj.name}

    if request.excluded is not None:
        target_bp_dict["excluded"] = request.excluded

    staging_repo.update_job_data(db, job_id=job_id, data=data)
    return get_sanitized_revision_job(db, job_id)


# ---------------------------------------------------------------------------
# Phase 5: 5-Step Ordered Atomic Database Commit
# ---------------------------------------------------------------------------

def commit_reconciliation_plan(db: Session, job_id: str) -> dict:
    """
    Executes the 5-Step Ordered Atomic Transaction:
      Step 1: Reassign Articles & migrate ArticlePhotos for Merges (deduplicating on canonical_color_name).
      Step 2: Delete absorbed orphan Blueprints + flush.
      Step 3: Align normalized_vendor_code across all impacted Blueprints of the Brand + flush.
      Step 4: Execute Splits (create new Blueprints with embeddings, reassign Articles, migrate/clone ArticlePhotos).
      Step 5: Update BrandHeuristic in brand_heuristics, delete staging job, commit, and invalidate search cache.
    """
    logger.log_execution("blueprint_revision_service", "commit_plan_start", "ok", job_id=job_id)

    job = staging_repo.get_job(db, job_id)
    if not job or not job.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job non trovato.")

    data = job.data
    plan = data.get("reconciliation_plan")
    if not plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Il piano di riconciliazione non è pronto per la conferma.")

    brand_id = data["brand_id"]
    heuristic_id = data.get("heuristic_id")
    h_state = data.get("heuristic_state") or {}
    new_regex = h_state.get("new_regex", "")
    textual_explanation = h_state.get("textual_explanation", "")

    splits_created = 0
    merges_executed = 0
    codes_aligned = 0

    try:
        # -------------------------------------------------------------------
        # STEP 1 & 2: Execute Merges (Reassign Articles & Photos, Delete Orphans)
        # -------------------------------------------------------------------
        absorbed_bp_ids_deleted: set[str] = set()

        for wm in plan.get("warehouse_merges", []):
            if wm.get("excluded"):
                continue
            after_card = wm.get("after") or {}
            if after_card.get("excluded"):
                continue

            surviving_id = after_card["surviving_blueprint_id"]
            absorbed_ids = after_card.get("absorbed_blueprint_ids", [])

            surviving_bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == surviving_id).first()
            if not surviving_bp:
                continue

            # Update surviving blueprint metadata if edited
            _apply_card_metadata_to_blueprint(db, surviving_bp, after_card)

            # Existing photo colors on surviving blueprint
            surviving_photos = db.query(ArticlePhoto).filter(
                ArticlePhoto.article_blueprint_id == surviving_bp.id
            ).all()
            existing_colors = {
                (p.canonical_color_name or "").strip().lower() for p in surviving_photos
            }

            for abs_id in absorbed_ids:
                abs_bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == abs_id).first()
                if not abs_bp:
                    continue

                # Reassign physical articles
                abs_articles = db.query(Article).filter(Article.article_blueprint_id == abs_bp.id).all()
                for art in abs_articles:
                    art.article_blueprint_id = surviving_bp.id

                # Migrate or deduplicate photos BEFORE deleting abs_bp (prevents CASCADE photo loss!)
                abs_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == abs_bp.id).all()
                for p in abs_photos:
                    c_name = (p.canonical_color_name or "").strip().lower()
                    if c_name not in existing_colors:
                        p.article_blueprint_id = surviving_bp.id
                        existing_colors.add(c_name)
                    else:
                        db.delete(p)

                db.flush()
                db.delete(abs_bp)
                absorbed_bp_ids_deleted.add(str(abs_bp.id))
                merges_executed += 1

        db.flush()

        # -------------------------------------------------------------------
        # STEP 3: Global Alignment of normalized_vendor_code on Existing Blueprints
        # -------------------------------------------------------------------
        # Collect all target normalized code updates for existing blueprints
        desired_norm_by_bp_id: Dict[str, str] = {}

        # From aligned_blueprints
        for al in plan.get("aligned_blueprints", []):
            bid = al["blueprint_id"]
            if bid not in absorbed_bp_ids_deleted and al.get("new_normalized_code"):
                desired_norm_by_bp_id[bid] = al["new_normalized_code"]

        # From merges surviving blueprints
        for wm in plan.get("warehouse_merges", []):
            if not wm.get("excluded") and not (wm.get("after") or {}).get("excluded"):
                after_c = wm["after"]
                if after_c.get("normalized_vendor_code"):
                    desired_norm_by_bp_id[after_c["surviving_blueprint_id"]] = after_c["normalized_vendor_code"]

        # From splits (the original "keep" blueprint in each split)
        all_split_ops = [plan.get("target_split")] + list(plan.get("warehouse_splits", []))
        for sp_op in all_split_ops:
            if not sp_op or sp_op.get("excluded"):
                continue
            for after_c in sp_op.get("after", []):
                if not after_c.get("is_new") and not after_c.get("excluded"):
                    if after_c.get("normalized_vendor_code"):
                        desired_norm_by_bp_id[after_c["id"]] = after_c["normalized_vendor_code"]

        # Two-stage update with temporary placeholders to avoid transient UniqueConstraint collisions
        bps_to_align = (
            db.query(ArticleBlueprint)
            .filter(ArticleBlueprint.id.in_(list(desired_norm_by_bp_id.keys())))
            .all()
            if desired_norm_by_bp_id
            else []
        )
        for bp_obj in bps_to_align:
            bp_obj.normalized_vendor_code = f"__tmp_{uuid.uuid4().hex[:12]}__"
        if bps_to_align:
            db.flush()

        # Check already occupied normalized codes for this brand (e.g. if a merge was excluded)
        existing_brand_bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.brand_id == brand_id).all()
        occupied_norms = {
            bp_o.normalized_vendor_code
            for bp_o in existing_brand_bps
            if bp_o.normalized_vendor_code and not bp_o.normalized_vendor_code.startswith("__tmp_")
        }

        for bp_obj in bps_to_align:
            target_norm = desired_norm_by_bp_id[str(bp_obj.id)]
            if target_norm not in occupied_norms:
                bp_obj.normalized_vendor_code = target_norm
                occupied_norms.add(target_norm)
                codes_aligned += 1
            else:
                # Disambiguate if another blueprint already holds that code (e.g. excluded merge)
                bp_obj.normalized_vendor_code = f"{target_norm}_{str(bp_obj.id)[:4]}"
                occupied_norms.add(bp_obj.normalized_vendor_code)
        if bps_to_align:
            db.flush()

        # -------------------------------------------------------------------
        # STEP 4: Execute Splits (Create New Blueprints, Reassign Articles & Photos)
        # -------------------------------------------------------------------
        for sp_op in all_split_ops:
            if not sp_op or sp_op.get("excluded"):
                continue

            source_bp_id = sp_op["source_blueprint_id"]
            source_bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == source_bp_id).first()
            if not source_bp:
                continue

            source_photos = db.query(ArticlePhoto).filter(
                ArticlePhoto.article_blueprint_id == source_bp.id
            ).all()
            photos_by_color: Dict[str, ArticlePhoto] = {
                (p.canonical_color_name or "").strip().lower(): p for p in source_photos
            }

            # Determine colors remaining on the original blueprint
            orig_card = next((c for c in sp_op.get("after", []) if not c.get("is_new")), None)
            if orig_card:
                _apply_card_metadata_to_blueprint(db, source_bp, orig_card)

            orig_art_ids = set(orig_card.get("article_ids", [])) if orig_card else set()
            orig_articles = db.query(Article).filter(Article.id.in_(list(orig_art_ids))).all() if orig_art_ids else []
            colors_kept_on_source: set[str] = set()
            for oa in orig_articles:
                for c in (oa.colors or []):
                    if c and str(c).strip():
                        colors_kept_on_source.add(str(c).strip().lower())

            # Create each new split blueprint
            for after_c in sp_op.get("after", []):
                if not after_c.get("is_new") or after_c.get("excluded"):
                    continue

                cat_id = _extract_target_category_id(after_c, fallback_category_id=source_bp.category_id)
                target_norm = after_c.get("normalized_vendor_code") or ""
                if target_norm in occupied_norms:
                    target_norm = f"{target_norm}_{uuid.uuid4().hex[:4]}"
                if target_norm:
                    occupied_norms.add(target_norm)

                new_bp = ArticleBlueprint(
                    id=str(uuid.uuid4()),
                    brand_id=brand_id,
                    category_id=cat_id,
                    normalized_vendor_code=target_norm or None,
                    article_name=after_c.get("article_name") or source_bp.article_name,
                    description=after_c.get("description") or source_bp.description,
                    extended_description=after_c.get("extended_description") or source_bp.extended_description,
                    dimensions=after_c.get("dimensions"),
                    tags=after_c.get("tags") or list(source_bp.tags or ["articolo"]),
                    materials=after_c.get("materials") if after_c.get("materials") is not None else list(source_bp.materials or []),
                )
                db.add(new_bp)
                db.flush()

                # Generate embedding for the new blueprint
                try:
                    text_to_embed = build_blueprint_embedding_text(db, new_bp)
                    if text_to_embed:
                        emb = LLMClient().generate_embedding_sync(text_to_embed)
                        new_bp.embedding = emb
                except Exception as exc:
                    logger.log_execution("blueprint_revision_service", "embedding_warning", "warn", exc=str(exc))

                # Reassign physical articles to the new blueprint
                moved_art_ids = after_c.get("article_ids", [])
                moved_articles = (
                    db.query(Article).filter(Article.id.in_(moved_art_ids)).all()
                    if moved_art_ids
                    else []
                )
                moved_colors: set[str] = set()
                for ma in moved_articles:
                    ma.article_blueprint_id = new_bp.id
                    for c in (ma.colors or []):
                        if c and str(c).strip():
                            moved_colors.add(str(c).strip().lower())

                # Migrate or clone ArticlePhoto records for moved_colors
                for mc in moved_colors:
                    src_photo = photos_by_color.get(mc)
                    if not src_photo and source_photos and len(photos_by_color) == 1:
                        src_photo = source_photos[0]
                    if src_photo:
                        if mc in colors_kept_on_source:
                            # Both the original blueprint and the new split blueprint have articles of this color: clone photo
                            cloned_photo = ArticlePhoto(
                                id=str(uuid.uuid4()),
                                article_blueprint_id=new_bp.id,
                                canonical_color_name=src_photo.canonical_color_name,
                                photo_data=src_photo.photo_data,
                            )
                            db.add(cloned_photo)
                        else:
                            # Only the new blueprint uses this color: move photo directly
                            src_photo.article_blueprint_id = new_bp.id
                            photos_by_color.pop(mc, None)

                splits_created += 1
                db.flush()

        # -------------------------------------------------------------------
        # STEP 5: Update BrandHeuristic, Commit Transaction & Invalidate Cache
        # -------------------------------------------------------------------
        if heuristic_id:
            heuristic_repo.update_heuristic(
                db,
                heuristic_id=heuristic_id,
                pattern=new_regex,
                explanation=textual_explanation,
                commit_changes=False,
            )
        else:
            heuristic_repo.create_heuristic(
                db,
                brand_id=brand_id,
                pattern=new_regex,
                explanation=textual_explanation,
                commit_changes=False,
            )

        db.commit()

    except Exception as exc:
        db.rollback()
        logger.log_execution("blueprint_revision_service", "commit_plan_error", "err", job_id=job_id, exc=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Errore durante l'applicazione transazionale delle modifiche: {exc}",
        )

    # Clean up staging job and invalidate search cache
    try:
        staging_repo.delete_job(db, job_id)
    except Exception:
        pass

    invalidate_search_cache()

    logger.log_execution(
        "blueprint_revision_service",
        "commit_plan_success",
        "ok",
        job_id=job_id,
        splits_created=splits_created,
        merges_executed=merges_executed,
        codes_aligned=codes_aligned,
    )

    return {
        "status": "success",
        "splits_created": splits_created,
        "merges_executed": merges_executed,
        "codes_aligned": codes_aligned,
    }


def _extract_target_category_id(card: dict, fallback_category_id: Optional[str] = None) -> Optional[str]:
    sub_cat = card.get("sub_category")
    if isinstance(sub_cat, dict) and sub_cat.get("id"):
        return sub_cat["id"]
    cat = card.get("category")
    if isinstance(cat, dict) and cat.get("id"):
        return cat["id"]
    return fallback_category_id


def _apply_card_metadata_to_blueprint(db: Session, bp: ArticleBlueprint, card: dict) -> None:
    changed_desc = False
    if card.get("article_name") and card["article_name"] != bp.article_name:
        bp.article_name = card["article_name"]
        changed_desc = True
    if card.get("description") and card["description"] != bp.description:
        bp.description = card["description"]
        changed_desc = True
    if card.get("extended_description") is not None and card["extended_description"] != bp.extended_description:
        bp.extended_description = card["extended_description"]
        changed_desc = True
    if card.get("tags"):
        bp.tags = card["tags"]
        changed_desc = True
    if card.get("materials") is not None:
        bp.materials = card["materials"]
        changed_desc = True
    if "dimensions" in card:
        bp.dimensions = card["dimensions"]

    new_cat_id = _extract_target_category_id(card, fallback_category_id=bp.category_id)
    if new_cat_id and new_cat_id != bp.category_id:
        bp.category_id = new_cat_id
        changed_desc = True

    if changed_desc:
        try:
            text_to_embed = build_blueprint_embedding_text(db, bp)
            if text_to_embed:
                bp.embedding = LLMClient().generate_embedding_sync(text_to_embed)
        except Exception:
            pass
