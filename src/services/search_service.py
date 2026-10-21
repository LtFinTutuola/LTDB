"""
src/services/search_service.py
-------------------------------
Hybrid RAG search service for the LTDB warehouse catalog.

Execution pipeline:
  1. Short-circuit lookup on exact EAN, supplier_code, or normalized_vendor_code (zero LLM calls).
  2. WarehouseSearchAgent execution:
     - Structured JSON query plan via LLM (brand_name, category_name, requires_semantic_search, semantic_query)
     - Deterministic SQLAlchemy candidate retrieval with category hierarchy expansion
     - Dual-regime cosine similarity ranking (0.65 primary / 0.50 zero-results fallback)
     - Dedicated LLM color variant filtering per candidate blueprint
  3. Variant card assembly respecting `allowed_colors` and `in_stock_only`.
"""
from __future__ import annotations
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.pim import ArticleBlueprint, Brand, Category, ArticlePhoto
from src.models.wms import Article, ArticleStatus
from src.repositories.pim_repo import pim_repo
from src.agents.warehouse_search_agent import WarehouseSearchAgent
from src.agents.warehouse_search_agent.nodes.semantic_rank_node import cosine_similarity as _cosine_similarity
from src.core.logger import get_logger

logger = get_logger()

_MAX_RESULTS = 20


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_semantic_search(db: Session, query: str, in_stock_only: bool = False) -> dict:
    """
    Execute the hybrid warehouse search pipeline for a user query.

    Args:
        db:            SQLAlchemy database session.
        query:         User search string (code or natural language query).
        in_stock_only: When True, excludes out-of-stock / sold variants.

    Returns:
        dict matching SemanticSearchResponse schema: {"message": str, "results": list[dict]}.
    """
    logger.log_execution(
        "search_service",
        "search_start",
        "ok",
        query=query,
        in_stock_only=in_stock_only,
    )

    # Step 1 — Deterministic short-circuit on EAN / supplier_code / normalized_vendor_code
    exact_matches = pim_repo.search_by_exact_code(db, query)
    if exact_matches:
        results: list[dict] = []
        for match in exact_matches:
            bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == match["blueprint_id"]).first()
            if not bp:
                continue
            variant_items = _build_variant_items(
                db=db,
                bp=bp,
                allowed_colors=match.get("allowed_colors"),
                in_stock_only=in_stock_only,
            )
            results.extend(variant_items)
            if len(results) >= _MAX_RESULTS:
                results = results[:_MAX_RESULTS]
                break

        message = _format_search_message(query, len(results))
        logger.log_execution(
            "search_service",
            "search_done",
            "ok",
            query=query,
            mode="exact_code_short_circuit",
            count=len(results),
        )
        return {"message": message, "results": results}

    # Step 2 — Run WarehouseSearchAgent (Query Plan -> SQL Filter -> Semantic Rank -> Color Filter)
    taxonomy = pim_repo.get_catalog_taxonomy_for_search(db)
    agent = WarehouseSearchAgent(db=db)
    agent_output = await agent.aexecute(
        {
            "query": query,
            "in_stock_only": in_stock_only,
            "brands_catalog": taxonomy["brands"],
            "categories_catalog": taxonomy["categories"],
        }
    )

    selected_blueprints = agent_output.get("selected_blueprints", [])
    fallback_triggered = agent_output.get("fallback_triggered", False)

    # Step 3 — Assemble SearchResultItem cards per blueprint & allowed color variants
    results = []
    for bp_entry in selected_blueprints:
        bp_id = bp_entry.get("id")
        if not bp_id:
            continue
        bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == bp_id).first()
        if not bp:
            continue
        variant_items = _build_variant_items(
            db=db,
            bp=bp,
            allowed_colors=bp_entry.get("allowed_colors"),
            in_stock_only=in_stock_only,
        )
        results.extend(variant_items)
        if len(results) >= _MAX_RESULTS:
            results = results[:_MAX_RESULTS]
            break

    message = _format_search_message(query, len(results))
    logger.log_execution(
        "search_service",
        "search_done",
        "ok",
        query=query,
        mode="hybrid_fallback" if fallback_triggered else "hybrid_agent",
        count=len(results),
        query_plan=agent_output.get("query_plan"),
    )
    return {"message": message, "results": results}


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _format_search_message(query: str, count: int) -> str:
    if count == 0:
        return f"Nessun articolo trovato per la ricerca \u00ab{query}\u00bb."
    if count == 1:
        return f"Ho trovato 1 articolo corrispondente alla ricerca \u00ab{query}\u00bb."
    return f"Ho trovato {count} articoli corrispondenti alla ricerca \u00ab{query}\u00bb."


def _count_available_stock(db: Session, blueprint_id: str) -> int:
    """Count Article records with status AVAILABLE for a given blueprint."""
    return (
        db.query(func.count(Article.id))
        .filter(
            Article.article_blueprint_id == blueprint_id,
            Article.status == ArticleStatus.AVAILABLE,
        )
        .scalar()
        or 0
    )


def _build_variant_items(
    db: Session,
    bp: ArticleBlueprint,
    allowed_colors: Optional[list[str]] = None,
    in_stock_only: bool = False,
) -> list[dict]:
    """
    Assemble SearchResultItem dicts from an ArticleBlueprint, one per color variant,
    filtered by `allowed_colors` (if provided) and `in_stock_only`.
    """
    all_articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()

    colors_dict: dict[str, list[Article]] = {}
    for a in all_articles:
        color_keys = [str(c).strip().lower() for c in (a.colors or []) if c and str(c).strip()]
        if not color_keys:
            color_keys = ["unknown"]
        for c in color_keys:
            if c not in colors_dict:
                colors_dict[c] = []
            colors_dict[c].append(a)

    if not colors_dict:
        colors_dict["unknown"] = []

    allowed_colors_set = (
        {str(c).strip().lower() for c in allowed_colors if c and str(c).strip()}
        if allowed_colors is not None
        else None
    )

    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
    category = db.query(Category).filter(Category.id == bp.category_id).first() if bp.category_id else None
    main_category = None
    sub_category = None
    if category:
        if category.parent_id:
            sub_category = category
            main_category = db.query(Category).filter(Category.id == category.parent_id).first()
        else:
            main_category = category

    results: list[dict] = []
    for color, articles in colors_dict.items():
        if allowed_colors_set is not None and color.lower() not in allowed_colors_set:
            continue

        stock = len([a for a in articles if a.status == ArticleStatus.AVAILABLE])
        if in_stock_only and stock <= 0:
            continue

        photo = (
            db.query(ArticlePhoto)
            .filter(
                ArticlePhoto.article_blueprint_id == bp.id,
                ArticlePhoto.canonical_color_name == color,
            )
            .first()
        )
        if not photo:
            photo = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp.id).first()

        name = f"{bp.article_name} - {color.capitalize()}" if color and color != "unknown" else bp.article_name
        vendor_code = next((a.supplier_code for a in articles if a.supplier_code), None)
        barcode = next((a.ean for a in articles if a.ean), None)

        results.append(
            {
                "blueprint_id": str(bp.id),
                "article_name": name,
                "brand_name": brand.name if brand else "Unknown",
                "brand_id": str(brand.id) if brand else None,
                "category_name": main_category.name if main_category else None,
                "sub_category_name": sub_category.name if sub_category else None,
                "description": bp.description,
                "extended_description": bp.extended_description,
                "vendor_code": vendor_code,
                "barcode": barcode,
                "photo_id": str(photo.id) if photo else None,
                "colors": [color] if color != "unknown" else [],
                "tags": list(bp.tags or []),
                "materials": list(bp.materials or []),
                "stock": stock,
            }
        )
    return results
