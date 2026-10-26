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

import base64
import copy
import re
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.agents.article_blueprints_agent import ArticleBlueprintsAgent
from src.agents.article_blueprints_agent.nodes.color_photo_search_node import (
    _extract_best_url,
    _validate_photo_url,
    _SYSTEM_PROMPT_QUERY_GEN,
    _SYSTEM_PROMPT_SEARCH,
)
from src.agents.base import AgentException
from src.agents.codes_deduction_agent import CodesDeductionAgent
from src.agents.llm_client import LLMClient
from src.core.logger import get_logger
from src.models.pim import ArticleBlueprint, ArticlePhoto, Brand, BrandHeuristic, Category
from src.models.staging import JobStatus, JobType, StagingArea
from src.models.wms import Article, ArticleStatus
from src.repositories import staging_repo
from src.repositories.pim_repo import category_repo, heuristic_repo, pim_repo
from src.schemas.data_ingestion import (
    BlueprintArticleGroupSchema,
    BlueprintRevisionContextResponse,
    BlueprintRevisionPhotoRetryRequest,
    HeuristicExampleSchema,
    ManualSplitPrepareRequest,
    PartitionGroupInput,
    ReconciliationPlanUpdateRequest,
    RuleDeductionRequest,
    RuleDeductionResponse,
    WarehouseAnomaliesResponse,
    WarehouseAnomalyItemSchema,
    CloseAnomalyResponse,
)
from src.services import photo_service
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

def get_blueprint_revision_context(
    db: Session,
    blueprint_id: str,
    related_blueprint_ids: Optional[List[str]] = None,
) -> BlueprintRevisionContextResponse:
    """
    Fetch full blueprint metadata and all physical articles (grouped by supplier_code + colors + ean)
    to populate the visual Bucket Selector screen.
    If related_blueprint_ids are provided (e.g. duplicate model anomaly), loads articles across all of them.
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

    all_bp_ids = [str(bp.id)]
    if related_blueprint_ids:
        for rid in related_blueprint_ids:
            if rid and str(rid) not in all_bp_ids:
                all_bp_ids.append(str(rid))

    bp_articles = db.query(Article).filter(Article.article_blueprint_id.in_(all_bp_ids)).all()
    bp_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id.in_(all_bp_ids)).all()

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


def get_warehouse_anomalies(
    db: Session,
    brand_id: Optional[str] = None,
) -> WarehouseAnomaliesResponse:
    """
    Retrieve warehouse anomalies specifically emerged during the manual blueprint revision phase
    for the brand(s) of reference, stored in the StagingArea under job_type='warehouse_anomalies'.

    Blueprints that no longer exist (e.g. already split, merged, or deleted) are dynamically pruned.
    """
    query = db.query(StagingArea).filter(StagingArea.job_type == JobType.WAREHOUSE_ANOMALIES.value)
    if brand_id:
        query = query.filter(StagingArea.file_path == str(brand_id))
    jobs = query.all()
    if not jobs:
        return WarehouseAnomaliesResponse(total_count=0, anomalies=[])

    all_anomalies: List[WarehouseAnomalyItemSchema] = []

    for job in jobs:
        data = job.data or {}
        raw_list = data.get("anomalies") or []
        if not raw_list:
            continue

        bp_ids_to_check = set()
        for item in raw_list:
            if item.get("blueprint_id"):
                bp_ids_to_check.add(str(item["blueprint_id"]))
            for r_id in (item.get("related_blueprint_ids") or []):
                bp_ids_to_check.add(str(r_id))

        existing_bps = (
            db.query(ArticleBlueprint)
            .filter(ArticleBlueprint.id.in_(list(bp_ids_to_check)))
            .all()
            if bp_ids_to_check
            else []
        )
        existing_bp_map = {str(b.id): b for b in existing_bps}

        pruned_list: List[WarehouseAnomalyItemSchema] = []
        for item in raw_list:
            if item.get("anomaly_type") != "SPLIT_REQUIRED":
                continue
            primary_id = str(item.get("blueprint_id") or "")
            if not primary_id or primary_id not in existing_bp_map:
                continue

            primary_bp = existing_bp_map[primary_id]
            item["related_blueprint_ids"] = []
            item["article_name"] = primary_bp.article_name
            pruned_list.append(WarehouseAnomalyItemSchema(**item))

        if len(pruned_list) != len(raw_list):
            if pruned_list:
                data["anomalies"] = [a.model_dump() for a in pruned_list]
                staging_repo.update_job_data(db, job.id, data=data)
            else:
                staging_repo.delete_job(db, job.id)

        all_anomalies.extend(pruned_list)

    return WarehouseAnomaliesResponse(
        total_count=len(all_anomalies),
        anomalies=all_anomalies,
    )


def _build_brand_anomalies_from_diff(
    db: Session,
    brand_id: str,
    brand_name: str,
    diff_summary: dict,
) -> List[WarehouseAnomalyItemSchema]:
    """
    Constructs the list of WarehouseAnomalyItemSchema for anomalies that emerged
    during the review of all articles of the brand of reference.
    Only SPLIT_REQUIRED anomalies are included (duplicate blueprints are ignored).
    """
    splits_raw = diff_summary.get("warehouse_splits_raw") or []
    if not splits_raw:
        return []

    all_bp_ids: set[str] = {str(s["source_blueprint_id"]) for s in splits_raw}
    bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.id.in_(list(all_bp_ids))).all() if all_bp_ids else []
    bp_map = {str(b.id): b for b in bps}

    arts = db.query(Article).filter(Article.article_blueprint_id.in_(list(all_bp_ids))).all() if all_bp_ids else []
    arts_by_bp: Dict[str, List[Article]] = defaultdict(list)
    for a in arts:
        arts_by_bp[str(a.article_blueprint_id)].append(a)

    photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id.in_(list(all_bp_ids))).all() if all_bp_ids else []
    photos_by_bp: Dict[str, List[ArticlePhoto]] = defaultdict(list)
    for p in photos:
        photos_by_bp[str(p.article_blueprint_id)].append(p)

    anomalies: List[WarehouseAnomalyItemSchema] = []

    # Splits discovered in brand of reference
    for s in splits_raw:
        bp_id = str(s["source_blueprint_id"])
        bp = bp_map.get(bp_id)
        if not bp:
            continue
        bp_arts = arts_by_bp.get(bp_id, [])
        bp_photos = photos_by_bp.get(bp_id, [])
        photo_url = f"/api/v1/ingestion/photos/{bp_photos[0].id}" if bp_photos else None
        sup_codes = sorted({(a.supplier_code or "").strip().upper() for a in bp_arts if a.supplier_code})
        distinct_norms = sorted(list(s.get("clusters", {}).keys()))
        anomalies.append(
            WarehouseAnomalyItemSchema(
                anomaly_id=f"split_{bp_id}",
                anomaly_type="SPLIT_REQUIRED",
                blueprint_id=bp_id,
                related_blueprint_ids=[],
                article_name=bp.article_name,
                brand_id=brand_id,
                brand_name=brand_name,
                normalized_vendor_code=bp.normalized_vendor_code,
                distinct_normalized_codes=distinct_norms,
                supplier_codes=sup_codes,
                total_physical_units=len(bp_arts),
                photo_url=photo_url,
                reason=f"Contiene {len(distinct_norms)} modelli diversi accorpati ({', '.join(distinct_norms)})",
            )
        )

    return anomalies


def _remove_anomaly_from_staging(db: Session, brand_id: str, blueprint_id: str) -> None:
    """Removes a resolved/closed blueprint anomaly from StagingArea for the brand."""
    anom_job_id = f"anomalies_brand_{brand_id}"
    anom_job = staging_repo.get_job(db, anom_job_id)
    if not anom_job:
        anom_job = db.query(StagingArea).filter(
            StagingArea.job_type == JobType.WAREHOUSE_ANOMALIES.value,
            StagingArea.file_path == str(brand_id),
        ).first()

    if anom_job and anom_job.data:
        anom_data = anom_job.data
        current_anoms = anom_data.get("anomalies") or []
        bp_str = str(blueprint_id)
        remaining = [
            item for item in current_anoms
            if str(item.get("blueprint_id")) != bp_str
            and bp_str not in [str(r) for r in (item.get("related_blueprint_ids") or [])]
        ]
        if remaining:
            anom_data["anomalies"] = remaining
            staging_repo.update_job_data(db, anom_job.id, anom_data)
        else:
            staging_repo.delete_job(db, anom_job.id)


def close_warehouse_anomaly(db: Session, blueprint_id: str) -> CloseAnomalyResponse:
    """
    Manually closes an anomaly for a blueprint, but strictly validates that all physical
    articles' vendor codes conform to the active BrandHeuristic and produce a single normalized root.
    If any vendor code violates the heuristic syntax or generates a divergent root, raises HTTP 400.
    """
    bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == blueprint_id).first()
    if not bp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{blueprint_id}' non trovato.",
        )

    articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
    supplier_codes = sorted({(a.supplier_code or "").strip().upper() for a in articles if a.supplier_code})

    if not supplier_codes:
        # Blueprint without physical supplier codes: can be closed safely
        _remove_anomaly_from_staging(db, str(bp.brand_id), str(bp.id))
        db.commit()
        remaining = get_warehouse_anomalies(db).total_count
        return CloseAnomalyResponse(
            success=True,
            message="Anomalia chiusa con successo: la scheda non presenta articoli fisici con codici fornitore da validare.",
            remaining_anomalies=remaining,
            blueprint_id=blueprint_id,
        )

    matched_heuristic = heuristic_repo.find_matching_heuristic(db, str(bp.brand_id), supplier_codes)
    if not matched_heuristic or not matched_heuristic.pattern:
        brand_name = bp.brand.name if bp.brand else str(bp.brand_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nessuna euristica attiva configurata per il brand '{brand_name}'.",
        )

    try:
        compiled_regex = re.compile(matched_heuristic.pattern)
    except re.error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Errore nella compilazione dell'euristica attiva del brand: {e}",
        )

    syntax_errors: List[str] = []
    roots_by_code: Dict[str, str] = {}
    codes_by_root: Dict[str, List[str]] = defaultdict(list)

    for code in supplier_codes:
        norm_root, matched = _normalize_vendor_code(code, compiled_regex)
        if not matched:
            syntax_errors.append(code)
        else:
            roots_by_code[code] = norm_root
            codes_by_root[norm_root].append(code)

    if syntax_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Impossibile chiudere l'anomalia: i seguenti codici fornitore non rispettano la sintassi dell'euristica attiva: "
                f"{', '.join(syntax_errors)}. Correggi i codici o l'euristica prima di chiudere l'anomalia."
            ),
        )

    if len(codes_by_root) > 1:
        # Multiple distinct normalized roots: blueprint merges disparate models!
        target_root = (
            bp.normalized_vendor_code
            if bp.normalized_vendor_code in codes_by_root
            else max(codes_by_root.keys(), key=lambda r: len(codes_by_root[r]))
        )
        violating_codes = [c for c, r in roots_by_code.items() if r != target_root]
        roots_desc = [f"'{r}' ({len(cds)} articoli: {', '.join(cds)})" for r, cds in codes_by_root.items()]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Impossibile chiudere l'anomalia: la scheda articolo aggrega modelli differenti che generano radici distinte: "
                f"{'; '.join(roots_desc)}. Codici non conformi rispetto al modello principale: {', '.join(violating_codes)}. "
                f"È necessario procedere con la divisione manuale della scheda."
            ),
        )

    # All codes strictly conform to the heuristic and produce a single normalized root!
    single_root = next(iter(codes_by_root.keys()))
    if bp.normalized_vendor_code != single_root:
        bp.normalized_vendor_code = single_root

    _remove_anomaly_from_staging(db, str(bp.brand_id), str(bp.id))
    db.commit()

    remaining = get_warehouse_anomalies(db).total_count
    return CloseAnomalyResponse(
        success=True,
        message="Anomalia chiusa con successo: tutti i codici fornitore risultano conformi all'euristica attiva del brand.",
        remaining_anomalies=remaining,
        blueprint_id=blueprint_id,
    )




# ---------------------------------------------------------------------------
# Phase 2: Contrastive Rule Deduction & Fast Warehouse Diffing
# ---------------------------------------------------------------------------

def _build_grounded_code_breakdowns(
    compiled_regex: Optional[re.Pattern],
    partitions: List[dict],
    bp_articles: List[Article],
) -> List[dict]:
    """
    Deterministically breaks down each distinct supplier code from the selected Blueprint's partitions
    into model root vs color variant substring, along with the article's color and assigned partition label.
    """
    breakdowns = []
    seen_codes = set()
    art_map = {str(a.id): a for a in bp_articles}

    for part in partitions:
        label = part.get("label") or "Modello"

        part_codes: List[Tuple[str, List[str]]] = []
        for aid in part.get("article_ids", []):
            art = art_map.get(aid)
            if art and art.supplier_code:
                c = art.supplier_code.strip().upper()
                if c and not any(x[0] == c for x in part_codes):
                    part_codes.append((c, list(art.colors or [])))
        for sc in part.get("supplier_codes", []):
            c = (sc or "").strip().upper()
            if c and not any(x[0] == c for x in part_codes):
                part_codes.append((c, []))

        for code, colors in part_codes:
            if code in seen_codes:
                continue
            seen_codes.add(code)

            model_root = code
            color_variant = ""
            position_info = ""
            if compiled_regex:
                match = compiled_regex.search(code)
                if match and "color_code" in match.groupdict() and match.group("color_code"):
                    c_start = match.start("color_code")
                    c_end = match.end("color_code")
                    color_variant = code[c_start:c_end]

                    if c_start == 0:
                        position_info = f"i primi {c_end} caratteri"
                        model_root = code[c_end:].lstrip("-_/ ")
                    elif c_end == len(code):
                        prefix_len = len(code) - c_start
                        position_info = f"gli ultimi {prefix_len} caratteri"
                        model_root = code[:c_start].rstrip("-_/ ")
                    else:
                        position_info = f"i caratteri centrali ({c_start + 1}-{c_end})"
                        model_root = (code[:c_start] + code[c_end:]).strip("-_/ ")

            breakdowns.append({
                "code": code,
                "model_root": model_root or code,
                "color_variant": color_variant,
                "position_info": position_info,
                "partition_label": label,
                "is_original": bool(part.get("is_original")),
                "colors": colors,
            })

    return breakdowns


def _generate_deterministic_grounded_explanation(brand_name: str, code_breakdowns: List[dict]) -> str:
    """Deterministic, non-technical explanation with concrete examples from the blueprint."""
    if not code_breakdowns:
        return f"Per il brand {brand_name}, i codici fornitore vengono interpretati isolando la radice del modello dalle varianti colore."

    primary = code_breakdowns[0]
    others = [b for b in code_breakdowns[1:] if b["model_root"] != primary["model_root"]]

    ex_text = f"Nel codice {primary['code']}"
    if primary["position_info"] and primary["color_variant"]:
        ex_text += f", {primary['position_info']} ({primary['color_variant']}) indicano la variante colore, mentre i restanti caratteri ({primary['model_root']}) identificano la linea del modello."
    else:
        ex_text += f", la parte principale ({primary['model_root']}) identifica il modello."

    if others:
        other_ex = others[0]
        ex_text += f" Di conseguenza, il codice {other_ex['code']} (modello {other_ex['model_root']}) viene separato su una scheda distinta, trattandosi di un modello differente."
    else:
        ex_text += " In questo modo, gli articoli che condividono la stessa radice modello vengono raggruppati come varianti colore della medesima scheda."

    return f"Per i prodotti del brand {brand_name}, la regola scompone il codice distinguendo la linea del modello dalla variante colore. {ex_text}"


async def _generate_user_friendly_rule_explanation(
    brand_name: str,
    code_breakdowns: List[dict],
    base_explanation: str,
    user_correction: Optional[str] = None,
) -> str:
    """
    Generates a natural, non-technical explanation of the brand heuristic grounded in
    concrete examples from the selected Blueprint being split.
    Uses LLMClient with a dedicated non-technical prompt, falling back deterministically if unavailable.
    """
    if not code_breakdowns:
        return base_explanation or f"I codici del brand {brand_name} vengono normalizzati per identificare la linea di prodotto."

    examples_bullets = []
    for b in code_breakdowns:
        col_str = f" (variante colore: '{b['color_variant']}' - {', '.join(b['colors']) or 'colore'})" if b["color_variant"] else ""
        pos_str = f" [{b['position_info']}]" if b["position_info"] else ""
        examples_bullets.append(
            f"- Codice '{b['code']}': radice linea/modello = '{b['model_root']}'{col_str}{pos_str} -> assegnato alla scheda '{b['partition_label']}'"
        )
    examples_ctx = "\n".join(examples_bullets)

    system_prompt = (
        "Sei un assistente per la gestione di magazzino in un negozio al dettaglio. "
        "Devi spiegare a un commesso/operatore (utente NON TECNICO) come il sistema riconosce e divide i codici di questo brand.\n\n"
        "REGOLE ASSOLUTE:\n"
        "1. TONO: Semplice, discorsivo, rassicurante e chiaro per un operatore di negozio.\n"
        "2. NIENTE GERGO TECNICO: È VIETATO usare termini come 'regex', 'espressione regolare', 'pattern', "
        "'gruppo di cattura', 'alphanumeric', 'token', 'match', 'backslash'. Parla solo di 'codice', 'caratteri', "
        "'parte iniziale/finale', 'trattino', 'linea/modello' e 'colore'.\n"
        "3. ESEMPI CONCRETI OBBLIGATORI: Devi citare esplicitamente i codici fornitore degli esempi reali forniti "
        "(es. 'Nel codice ABCDE-123 i primi 5 caratteri indicano la linea (ABCDE) e gli ultimi 3 indicano il colore (123)').\n"
        "4. Mostra chiaramente perché i codici con radice diversa formano schede distinte e quelli con la stessa radice formano varianti dello stesso modello.\n"
        "5. Lunghezza: Massimo 3-4 frasi chiare ed esaustive."
    )

    user_prompt = (
        f"Brand: {brand_name}\n"
        f"Esempi reali della scheda che l'utente sta dividendo:\n{examples_ctx}\n\n"
    )
    if user_correction:
        user_prompt += f"L'utente ha precedentemente fornito questo chiarimento/correzione:\n\"{user_correction}\"\n\n"
    user_prompt += "Genera la spiegazione in italiano spiegando la regola con gli esempi pratici citati."

    try:
        client = LLMClient()
        resp = await client.call(
            model_name="gemini-3.1-flash-lite",
            system_prompt=system_prompt,
            prompt=user_prompt,
            pipeline_stage="Blueprint Revision - Grounded User Explanation",
            temperature=0.2,
        )
        cleaned = resp.strip() if resp else ""
        if cleaned:
            return cleaned
    except Exception as exc:
        logger.log_execution("blueprint_revision_service", "grounded_explanation_llm_failed", "warn", exc=str(exc))

    return _generate_deterministic_grounded_explanation(brand_name, code_breakdowns)


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

    # Generate user-friendly rule explanation grounded in concrete examples from the selected Blueprint
    code_breakdowns = _build_grounded_code_breakdowns(
        compiled_regex=compiled_new,
        partitions=normalized_partitions_data,
        bp_articles=bp_articles,
    )
    grounded_explanation = await _generate_user_friendly_rule_explanation(
        brand_name=brand.name,
        code_breakdowns=code_breakdowns,
        base_explanation=textual_explanation,
        user_correction=request.user_correction,
    )
    if grounded_explanation:
        textual_explanation = grounded_explanation

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
    Runs AI synthesis from scratch for all blueprints resulting from the user's manual split
    and builds the Before & After reconciliation plan in the Staging Area ONLY for the split blueprint.
    Other warehouse anomalies are left untouched in the database to be resolved manually one by one.
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
    data["skip_rule_update"] = False

    categories_hierarchy = category_repo.get_brand_hierarchy(db, brand_id)

    # Build Target Split Operation ONLY for the blueprint manually split by the operator
    source_bp_id = data["trigger_context"]["source_blueprint_id"]
    source_bp_ids = data["trigger_context"].get("source_blueprint_ids") or [source_bp_id]
    partitions = data["trigger_context"]["partitions"]
    target_split_plan = await _build_split_operation_plan(
        db=db,
        brand_id=brand_id,
        brand_name=brand_name,
        source_bp_id=source_bp_id,
        source_blueprint_ids=source_bp_ids,
        partitions=partitions,
        compiled_regex=compiled_regex,
        categories_hierarchy=categories_hierarchy,
    )

    # Aligned blueprints list (single-model non-anomalous blueprints whose normalized code is updated)
    aligned_list = data.get("diff_preview", {}).get("aligned_blueprints_raw", [])

    data["phase"] = "READY_FOR_REVIEW"
    data["reconciliation_plan"] = {
        "target_split": target_split_plan,
        "warehouse_splits": [],
        "warehouse_merges": [],
        "aligned_blueprints": aligned_list,
    }

    staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=data)
    logger.log_execution("blueprint_revision_service", "prepare_staging_plan_success", "ok", job_id=job_id)

    return get_sanitized_revision_job(db, job_id)


async def prepare_manual_split_staging_plan(
    db: Session,
    request: ManualSplitPrepareRequest,
) -> dict:
    """
    Prepare the Staging Area 'Before & After' directly from the operator's manual partitions
    without recalculating the vendor code heuristic (used when resolving warehouse anomalies one-by-one).
    All resulting blueprints are recreated from scratch via AI while preserving user-selected photos.
    """
    logger.log_execution(
        "blueprint_revision_service",
        "prepare_manual_split_start",
        "ok",
        blueprint_id=request.blueprint_id,
        partitions_count=len(request.partitions),
    )

    bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == request.blueprint_id).first()
    if not bp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{request.blueprint_id}' non trovato.",
        )

    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
    brand_name = brand.name if brand else "Unknown"
    brand_id_str = str(bp.brand_id)

    all_source_ids = [str(bp.id)]
    if request.source_blueprint_ids:
        for sid in request.source_blueprint_ids:
            if sid and str(sid) not in all_source_ids:
                all_source_ids.append(str(sid))

    active_partitions = [
        p.model_dump()
        for p in request.partitions
        if p.article_ids or p.supplier_codes
    ]

    if len(all_source_ids) == 1 and len(active_partitions) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Per dividere la scheda è necessario separare gli articoli in almeno due modelli distinti.",
        )
    if not active_partitions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inserire almeno un gruppo di articoli valido.",
        )

    bp_articles = db.query(Article).filter(Article.article_blueprint_id.in_(all_source_ids)).all()
    sample_codes = [a.supplier_code for a in bp_articles if a.supplier_code]
    matched_heuristic = heuristic_repo.find_matching_heuristic(db, brand_id_str, sample_codes)

    compiled_regex: Optional[re.Pattern] = None
    if matched_heuristic and matched_heuristic.pattern:
        try:
            compiled_regex = re.compile(matched_heuristic.pattern)
        except re.error:
            compiled_regex = None

    categories_hierarchy = category_repo.get_brand_hierarchy(db, brand_id_str)

    target_split_plan = await _build_split_operation_plan(
        db=db,
        brand_id=brand_id_str,
        brand_name=brand_name,
        source_bp_id=str(bp.id),
        source_blueprint_ids=all_source_ids,
        partitions=active_partitions,
        compiled_regex=compiled_regex,
        categories_hierarchy=categories_hierarchy,
    )

    job_data = {
        "brand_id": brand_id_str,
        "brand_name": brand_name,
        "heuristic_id": str(matched_heuristic.id) if matched_heuristic else None,
        "skip_rule_update": True,
        "phase": "READY_FOR_REVIEW",
        "trigger_context": {
            "source_blueprint_id": str(bp.id),
            "source_blueprint_ids": all_source_ids,
            "partitions": active_partitions,
        },
        "heuristic_state": {
            "textual_explanation": (
                matched_heuristic.explanation
                if matched_heuristic and matched_heuristic.explanation
                else f"Correzione manuale scheda {brand_name} secondo la regola attiva del brand."
            ),
            "user_confirmed_rule": True,
        },
        "reconciliation_plan": {
            "target_split": target_split_plan,
            "warehouse_splits": [],
            "warehouse_merges": [],
            "aligned_blueprints": [],
        },
    }

    job_id = request.job_id
    existing_job = staging_repo.get_job(db, job_id) if job_id else None
    if existing_job:
        staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=job_data)
    else:
        job_id = str(uuid.uuid4())
        staging_repo.create_job(db, job_id=job_id, job_type=JobType.HEURISTIC_REVISION.value)
        staging_repo.update_job(db, job_id=job_id, status=JobStatus.COMPLETED.value, data=job_data)

    logger.log_execution("blueprint_revision_service", "prepare_manual_split_success", "ok", job_id=job_id)
    return get_sanitized_revision_job(db, job_id)


async def _build_split_operation_plan(
    db: Session,
    brand_id: str,
    brand_name: str,
    source_bp_id: str,
    partitions: List[dict],
    compiled_regex: Optional[re.Pattern],
    categories_hierarchy: dict,
    source_blueprint_ids: Optional[List[str]] = None,
) -> dict:
    """
    Build a detailed Before / After representation for a Blueprint being split (or duplicate blueprints unified/split).
    Every resulting blueprint in the 'After' column is recreated from scratch via ArticleBlueprintsAgent
    (without inheriting old descriptions/tags/materials from source_bp), while preserving the existing
    ArticlePhotos associated with each partition's articles.
    """
    all_source_ids = list(dict.fromkeys(([source_bp_id] + (source_blueprint_ids or []))))
    source_bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.id.in_(all_source_ids)).all()
    if not source_bps:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Blueprint '{source_bp_id}' non trovato.")

    source_bp = next((b for b in source_bps if str(b.id) == source_bp_id), source_bps[0])

    bp_articles = db.query(Article).filter(Article.article_blueprint_id.in_(all_source_ids)).all()
    bp_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id.in_(all_source_ids)).all()
    art_map = {str(a.id): a for a in bp_articles}

    cat_dict, sub_cat_dict = _resolve_category_dicts(db, source_bp)
    before_groups = [g.model_dump() for g in _build_article_groups_for_blueprint(bp_articles, bp_photos)]

    before_names = " / ".join(dict.fromkeys(b.article_name for b in source_bps if b.article_name))
    before_card = {
        "blueprint_id": str(source_bp.id),
        "source_blueprint_ids": all_source_ids,
        "article_name": before_names or source_bp.article_name,
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

    # ALL partitions are treated as brand-new blueprints to be synthesized from scratch by ArticleBlueprintsAgent
    new_clusters_for_agent = []
    cluster_by_temp_id: Dict[str, dict] = {}
    ordered_temp_ids: List[str] = []

    for idx, part in enumerate(partitions):
        part_arts = [art_map[aid] for aid in part.get("article_ids", []) if aid in art_map]
        if not part_arts and part.get("supplier_codes"):
            target_scs = {s.upper() for s in part["supplier_codes"]}
            part_arts = [a for a in bp_articles if (a.supplier_code or "").upper() in target_scs]

        if not part_arts:
            continue

        # Preserve existing photos from source blueprint(s) matching the colors of these articles
        part_groups = [g.model_dump() for g in _build_article_groups_for_blueprint(part_arts, bp_photos)]
        first_sc = next((a.supplier_code for a in part_arts if a.supplier_code), "")
        norm_code, _ = _normalize_vendor_code(first_sc, compiled_regex) if first_sc else (source_bp.normalized_vendor_code or "", True)

        temp_id = f"new_bp_{uuid.uuid4().hex[:10]}"
        user_hint = (part.get("user_hint") or "").strip()
        clean_search_seed = user_hint if user_hint else f"{brand_name} {first_sc}".strip()

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
                "article_name": clean_search_seed,
                "description": clean_search_seed,
                "article_description": clean_search_seed,
            })

        cluster_obj = {
            "id": temp_id,
            "is_new": True,
            "cluster_items": cluster_items,
        }
        new_clusters_for_agent.append(cluster_obj)
        ordered_temp_ids.append(temp_id)
        cluster_by_temp_id[temp_id] = {
            "temp_id": temp_id,
            "norm_code": norm_code,
            "first_sc": first_sc,
            "part_arts": part_arts,
            "part_groups": part_groups,
            "user_hint": user_hint,
            "fallback_label": clean_search_seed,
        }

    # Invoke ArticleBlueprintsAgent to synthesize all resulting blueprints from scratch
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
            returned_bps = agent_res.get("blueprints", [])
            for i, abp in enumerate(returned_bps):
                if abp.get("id"):
                    synthesized_by_id[abp["id"]] = abp
                elif abp.get("vendor_code"):
                    vc_upper = str(abp["vendor_code"]).strip().upper()
                    for tid, ci in cluster_by_temp_id.items():
                        if str(ci.get("first_sc") or "").strip().upper() == vc_upper:
                            synthesized_by_id[tid] = abp
                            break
                elif i < len(ordered_temp_ids):
                    synthesized_by_id[ordered_temp_ids[i]] = abp
        except Exception as exc:
            logger.log_execution("blueprint_revision_service", "agent_synthesis_fallback", "warn", exc=str(exc))

    after_blueprints: List[dict] = []
    for temp_id in ordered_temp_ids:
        c_info = cluster_by_temp_id[temp_id]
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

        fresh_name = synth.get("article_name") or c_info["user_hint"] or c_info["fallback_label"]
        fresh_desc = synth.get("description") or f"Articolo {brand_name} modello {c_info['first_sc']}".strip()
        fresh_ext_desc = synth.get("extended_description") or f"{fresh_name} - {fresh_desc}"
        default_tags = [t for t in [brand_name.lower(), c_info["first_sc"].lower()] if t] or ["articolo"]
        synth_tags = synth.get("tags") or default_tags
        synth_materials = synth.get("materials") if synth.get("materials") is not None else []

        after_blueprints.append({
            "id": temp_id,
            "is_new": True,
            "excluded": False,
            "article_name": fresh_name,
            "description": fresh_desc,
            "extended_description": fresh_ext_desc,
            "normalized_vendor_code": c_info["norm_code"],
            "category": resolved_cat,
            "sub_category": resolved_sub,
            "tags": synth_tags if synth_tags else ["articolo"],
            "materials": synth_materials,
            "dimensions": synth.get("dimensions"),
            "article_ids": [str(a.id) for a in c_info["part_arts"]],
            "article_groups": c_info["part_groups"],
        })

    return {
        "source_blueprint_id": str(source_bp.id),
        "source_blueprint_ids": all_source_ids,
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
            cat_desc = request.category["description"].strip().lower()
            cat_obj = db.query(Category).filter(
                (func.lower(Category.name) == cat_desc)
                | (func.lower(Category.description) == cat_desc)
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
                sub_desc = request.sub_category["description"].strip().lower()
                sub_obj = db.query(Category).filter(
                    (func.lower(Category.name) == sub_desc)
                    | (func.lower(Category.description) == sub_desc)
                ).first()
            if not sub_obj:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sottocategoria non trovata.")
            target_bp_dict["sub_category"] = {"id": str(sub_obj.id), "description": sub_obj.name}

    if request.excluded is not None:
        target_bp_dict["excluded"] = request.excluded

    staging_repo.update_job_data(db, job_id=job_id, data=data)
    return get_sanitized_revision_job(db, job_id)


def _find_card_in_plan(plan: dict, target_id: str) -> Optional[dict]:
    """Helper to locate a target blueprint card in the 'After' section of a reconciliation plan."""
    target_split = plan.get("target_split") or {}
    for bp_card in target_split.get("after", []):
        if bp_card.get("id") == target_id:
            return bp_card

    for ws in plan.get("warehouse_splits", []):
        for bp_card in ws.get("after", []):
            if bp_card.get("id") == target_id:
                return bp_card

    for wm in plan.get("warehouse_merges", []):
        after_c = wm.get("after") or {}
        if after_c.get("id") == target_id or wm.get("merge_id") == target_id:
            return after_c

    return None


def upload_staged_blueprint_photo(
    db: Session,
    job_id: str,
    target_blueprint_id: str,
    color: Optional[str],
    content: bytes,
    content_type: str = "image/jpeg",
) -> dict:
    """
    Upload an image for a specific Blueprint card and color variant in the revision Before/After staging plan.
    Saves the data URL in staged_photos_by_color and updates the matching article groups in job data.
    """
    job = staging_repo.get_job(db, job_id)
    if not job or not job.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job non trovato.")

    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nessun contenuto immagine inviato.")

    b64 = base64.b64encode(content).decode("ascii")
    data_url = f"data:{content_type};base64,{b64}"

    data = copy.deepcopy(job.data)
    plan = data.get("reconciliation_plan")
    if not plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Piano di riconciliazione non trovato.")

    card = _find_card_in_plan(plan, target_blueprint_id)
    if not card:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Scheda '{target_blueprint_id}' non trovata nel piano.")

    norm_color = (color or "").strip().lower()
    if not norm_color:
        art_groups = card.get("article_groups") or []
        for g in art_groups:
            c_list = g.get("colors") or []
            if c_list:
                norm_color = str(c_list[0]).strip().lower()
                break

    if "staged_photos_by_color" not in card or not isinstance(card["staged_photos_by_color"], dict):
        card["staged_photos_by_color"] = {}
    card["staged_photos_by_color"][norm_color] = data_url

    art_groups = card.get("article_groups") or []
    for g in art_groups:
        c_list = [str(c).strip().lower() for c in (g.get("colors") or [])]
        if not norm_color or norm_color in c_list or len(art_groups) == 1:
            g["photo_url"] = data_url

    staging_repo.update_job_data(db, job_id=job_id, data=data)

    return {
        "status": "success",
        "target_blueprint_id": target_blueprint_id,
        "color": norm_color,
        "photo_url": data_url,
    }


async def retry_or_set_staged_blueprint_photo(
    db: Session,
    job_id: str,
    request: BlueprintRevisionPhotoRetryRequest,
) -> dict:
    """
    Set direct image URL or run Gemini Grounded Photo Search for a target blueprint card in the revision staging plan.
    """
    job = staging_repo.get_job(db, job_id)
    if not job or not job.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job non trovato.")

    data = copy.deepcopy(job.data)
    plan = data.get("reconciliation_plan")
    if not plan:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Piano di riconciliazione non trovato.")

    target_id = request.target_blueprint_id
    card = _find_card_in_plan(plan, target_id)
    if not card:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Scheda '{target_id}' non trovata nel piano.")

    norm_color = (request.color or "").strip().lower()
    if not norm_color:
        art_groups = card.get("article_groups") or []
        for g in art_groups:
            c_list = g.get("colors") or []
            if c_list:
                norm_color = str(c_list[0]).strip().lower()
                break

    target_photo_url = None

    if request.new_url:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=6.0, follow_redirects=True) as client:
                async with client.stream("GET", request.new_url) as res:
                    if res.status_code >= 400:
                        raise HTTPException(status_code=422, detail="URL immagine non raggiungibile (status >= 400).")
                    ct = res.headers.get("content-type", "")
                    if not ct.startswith("image/"):
                        raise HTTPException(status_code=422, detail="L'URL fornito non punta a un'immagine valida.")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=422, detail=f"Impossibile raggiungere l'URL indicato: {exc}")

        target_photo_url = request.new_url
    else:
        brand_name = data.get("brand_name", "")
        article_name = card.get("article_name", "")
        vendor_code = ""
        art_groups = card.get("article_groups") or []
        if art_groups:
            vendor_code = art_groups[0].get("supplier_code", "")

        bp_info = (
            f"Brand: {brand_name}\n"
            f"Article Name: {article_name}\n"
            f"Vendor Code: {vendor_code}\n"
            f"Color: {norm_color}\n"
            f"Description: {card.get('description', '')}"
        )

        if request.user_feedback:
            prompt_content = f"L'utente ha fornito questo feedback:\n'{request.user_feedback}'\n\nUsa queste informazioni sull'articolo per generare una query di ricerca:\n{bp_info}"
        else:
            prompt_content = f"Genera una query di ricerca Google mirata per trovare l'immagine del prodotto:\n{bp_info}"

        client = LLMClient()
        generated_query = await client.call(
            model_name="gemini-3.1-flash-lite",
            system_prompt=_SYSTEM_PROMPT_QUERY_GEN,
            prompt=prompt_content,
            pipeline_stage="Blueprint Revision - Photo Search Query Gen",
            temperature=0.3,
        )
        generated_query = (generated_query or "").strip()

        search_prompt = f"Esegui una ricerca su internet e trova una foto reale del prodotto usando questa query:\n{generated_query}"
        raw_res, extracted_urls = await client.call_with_grounding(
            model_name="gemini-3.1-flash-lite",
            system_prompt=_SYSTEM_PROMPT_SEARCH,
            prompt=search_prompt,
            pipeline_stage="Blueprint Revision - Photo Search Grounded",
            temperature=0.2,
        )

        raw_res_stripped = (raw_res or "").strip()
        candidate_url = None if raw_res_stripped.upper() == "NULL" else _extract_best_url(extracted_urls, raw_res_stripped)
        if candidate_url:
            is_valid = await _validate_photo_url(candidate_url)
            if not is_valid:
                candidate_url = None

        if not candidate_url:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Nessuna foto valida trovata sul web per questo articolo. Prova a inserire un URL o a caricare una foto manualmente."
            )
        target_photo_url = candidate_url

    if "staged_photos_by_color" not in card or not isinstance(card["staged_photos_by_color"], dict):
        card["staged_photos_by_color"] = {}
    card["staged_photos_by_color"][norm_color] = target_photo_url

    art_groups = card.get("article_groups") or []
    for g in art_groups:
        c_list = [str(c).strip().lower() for c in (g.get("colors") or [])]
        if not norm_color or norm_color in c_list or len(art_groups) == 1:
            g["photo_url"] = target_photo_url

    staging_repo.update_job_data(db, job_id=job_id, data=data)

    return {
        "status": "success",
        "target_blueprint_id": target_id,
        "color": norm_color,
        "photo_url": target_photo_url,
    }


# ---------------------------------------------------------------------------
# Phase 5: 5-Step Ordered Atomic Database Commit
# ---------------------------------------------------------------------------

def commit_reconciliation_plan(db: Session, job_id: str) -> dict:
    """
    Executes the Atomic Database Commit for the revised Blueprint:
      Step 1: Align normalized_vendor_code on clean single-model Blueprints of the Brand (when updating heuristic).
      Step 2: Recreate all split Blueprints from scratch (new UUIDs, fresh embeddings, reassigned Articles,
              preserved existing ArticlePhotos + user-selected staged_photos_by_color overrides)
              and delete the old faulty Blueprint(s).
      Step 3: Update BrandHeuristic in brand_heuristics (unless skip_rule_update is True),
              delete staging job, commit, and invalidate search cache.
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
    skip_rule_update = bool(data.get("skip_rule_update"))
    heuristic_id = data.get("heuristic_id")
    h_state = data.get("heuristic_state") or {}
    new_regex = h_state.get("new_regex", "")
    textual_explanation = h_state.get("textual_explanation", "")

    splits_created = 0
    merges_executed = 0
    codes_aligned = 0

    try:
        all_split_ops = [plan.get("target_split")]
        source_bp_ids_Being_replaced: set[str] = set()
        for sp_op in all_split_ops:
            if not sp_op or sp_op.get("excluded"):
                continue
            s_ids = sp_op.get("source_blueprint_ids") or ([sp_op["source_blueprint_id"]] if sp_op.get("source_blueprint_id") else [])
            for sid in s_ids:
                if sid:
                    source_bp_ids_Being_replaced.add(str(sid))

        # Free normalized_vendor_code on source blueprints being replaced so new blueprints never collide
        if source_bp_ids_Being_replaced:
            bps_being_replaced = (
                db.query(ArticleBlueprint)
                .filter(ArticleBlueprint.id.in_(list(source_bp_ids_Being_replaced)))
                .all()
            )
            for s_bp in bps_being_replaced:
                s_bp.normalized_vendor_code = f"__del_{uuid.uuid4().hex[:12]}__"
            db.flush()

        # -------------------------------------------------------------------
        # STEP 1: Alignment of normalized_vendor_code on Clean Single-Model Blueprints
        # -------------------------------------------------------------------
        desired_norm_by_bp_id: Dict[str, str] = {}
        if not skip_rule_update:
            for al in plan.get("aligned_blueprints", []):
                bid = al["blueprint_id"]
                if bid not in source_bp_ids_Being_replaced and al.get("new_normalized_code"):
                    desired_norm_by_bp_id[bid] = al["new_normalized_code"]

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

        existing_brand_bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.brand_id == brand_id).all()
        occupied_norms = {
            bp_o.normalized_vendor_code
            for bp_o in existing_brand_bps
            if bp_o.normalized_vendor_code
            and not bp_o.normalized_vendor_code.startswith("__tmp_")
            and not bp_o.normalized_vendor_code.startswith("__del_")
        }

        for bp_obj in bps_to_align:
            target_norm = desired_norm_by_bp_id[str(bp_obj.id)]
            if target_norm not in occupied_norms:
                bp_obj.normalized_vendor_code = target_norm
                occupied_norms.add(target_norm)
                codes_aligned += 1
            else:
                bp_obj.normalized_vendor_code = f"{target_norm}_{str(bp_obj.id)[:4]}"
                occupied_norms.add(bp_obj.normalized_vendor_code)
        if bps_to_align:
            db.flush()

        # -------------------------------------------------------------------
        # STEP 2: Recreate All Split Blueprints From Scratch & Preserve User Photos
        # -------------------------------------------------------------------
        for sp_op in all_split_ops:
            if not sp_op or sp_op.get("excluded"):
                continue

            s_ids = sp_op.get("source_blueprint_ids") or ([sp_op["source_blueprint_id"]] if sp_op.get("source_blueprint_id") else [])
            source_bps = db.query(ArticleBlueprint).filter(ArticleBlueprint.id.in_(s_ids)).all() if s_ids else []
            if not source_bps:
                continue

            primary_source_bp = source_bps[0]
            source_photos = db.query(ArticlePhoto).filter(
                ArticlePhoto.article_blueprint_id.in_([str(b.id) for b in source_bps])
            ).all()
            photos_by_color: Dict[str, ArticlePhoto] = {}
            for p in source_photos:
                c_key = (p.canonical_color_name or "").strip().lower()
                if c_key and c_key not in photos_by_color:
                    photos_by_color[c_key] = p

            created_new_bps: List[ArticleBlueprint] = []

            for after_c in sp_op.get("after", []):
                if after_c.get("excluded"):
                    continue

                cat_id = _extract_target_category_id(after_c, fallback_category_id=primary_source_bp.category_id)
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
                    article_name=after_c.get("article_name") or primary_source_bp.article_name,
                    description=after_c.get("description") or primary_source_bp.description,
                    extended_description=after_c.get("extended_description") or primary_source_bp.extended_description,
                    dimensions=after_c.get("dimensions"),
                    tags=after_c.get("tags") or ["articolo"],
                    materials=after_c.get("materials") if after_c.get("materials") is not None else [],
                )
                db.add(new_bp)
                db.flush()
                created_new_bps.append(new_bp)

                # Generate embedding for the newly created blueprint
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

                staged_new_photos = after_c.get("staged_photos_by_color") or {}

                # Preserve existing ArticlePhoto records from source blueprint(s) for each moved color
                for mc in moved_colors:
                    src_photo = photos_by_color.get(mc)
                    if not src_photo and source_photos and len(photos_by_color) == 1:
                        src_photo = source_photos[0]
                    if src_photo:
                        cloned_photo = ArticlePhoto(
                            id=str(uuid.uuid4()),
                            article_blueprint_id=new_bp.id,
                            canonical_color_name=src_photo.canonical_color_name,
                            photo_data=src_photo.photo_data,
                        )
                        db.add(cloned_photo)
                db.flush()

                # Apply any user-selected photo overrides from the Staging Area
                for c_name, photo_data_or_url in staged_new_photos.items():
                    if photo_data_or_url:
                        photo_service.download_and_save_photo(
                            db,
                            blueprint_id=str(new_bp.id),
                            color_name=c_name,
                            photo_url=photo_data_or_url,
                            commit_changes=False,
                            force_update=True,
                        )

                splits_created += 1
                db.flush()

            # Delete the old replaced blueprint(s) after ensuring all physical articles have been reassigned
            if created_new_bps:
                fallback_new_bp_id = created_new_bps[0].id
                remaining_arts = (
                    db.query(Article)
                    .filter(Article.article_blueprint_id.in_([str(b.id) for b in source_bps]))
                    .all()
                )
                for ra in remaining_arts:
                    ra.article_blueprint_id = fallback_new_bp_id
                db.flush()

                for old_p in source_photos:
                    db.delete(old_p)
                db.flush()

                for s_bp in source_bps:
                    db.delete(s_bp)
                db.flush()

        # -------------------------------------------------------------------
        # STEP 3: Update BrandHeuristic (if deduced in this flow) & Commit
        # -------------------------------------------------------------------
        if not skip_rule_update and new_regex:
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

        # -------------------------------------------------------------------
        # STEP 4: Persist or Prune Anomalies for the Brand of Reference
        # -------------------------------------------------------------------
        anom_job_id = f"anomalies_brand_{brand_id}"
        if not skip_rule_update:
            # Full brand review occurred during regex recalculation: record ONLY anomalies from this brand
            brand_anomalies = _build_brand_anomalies_from_diff(
                db=db,
                brand_id=str(brand_id),
                brand_name=data.get("brand_name", ""),
                diff_summary=data.get("diff_preview") or {},
            )
            existing_anom_job = staging_repo.get_job(db, anom_job_id)
            anom_payload = {
                "brand_id": str(brand_id),
                "brand_name": data.get("brand_name", ""),
                "anomalies": [a.model_dump() for a in brand_anomalies],
            }
            if brand_anomalies:
                if existing_anom_job:
                    existing_anom_job.data = anom_payload
                    staging_repo.flag_modified(existing_anom_job, "data")
                else:
                    new_anom_job = StagingArea(
                        id=anom_job_id,
                        job_type=JobType.WAREHOUSE_ANOMALIES.value,
                        file_path=str(brand_id),
                        data=anom_payload,
                        status=JobStatus.ACCEPTED.value,
                    )
                    db.add(new_anom_job)
            else:
                if existing_anom_job:
                    db.delete(existing_anom_job)
        else:
            # Manual resolution of an individual blueprint anomaly: prune the resolved blueprint(s)
            existing_anom_job = staging_repo.get_job(db, anom_job_id)
            if existing_anom_job and existing_anom_job.data:
                anom_data = existing_anom_job.data
                current_anoms = anom_data.get("anomalies") or []
                resolved_ids = set(source_bp_ids_Being_replaced)
                remaining_list = [
                    item for item in current_anoms
                    if str(item.get("blueprint_id")) not in resolved_ids
                    and not any(str(r) in resolved_ids for r in (item.get("related_blueprint_ids") or []))
                ]
                if remaining_list:
                    anom_data["anomalies"] = remaining_list
                    existing_anom_job.data = anom_data
                    staging_repo.flag_modified(existing_anom_job, "data")
                else:
                    db.delete(existing_anom_job)

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

    remaining_anomalies = 0
    try:
        remaining_anomalies = get_warehouse_anomalies(db).total_count
    except Exception:
        pass

    return {
        "status": "success",
        "splits_created": splits_created,
        "merges_executed": merges_executed,
        "codes_aligned": codes_aligned,
        "remaining_anomalies": remaining_anomalies,
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
