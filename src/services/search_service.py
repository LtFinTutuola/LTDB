"""
src/services/search_service.py
-------------------------------
Hybrid warehouse search service for the LTDB catalog.

Execution pipeline:
  1. Snapshot cache lookup (normalized query + in_stock_only) -> re-sort & paginate without LLM.
  2. Tier 1 short-circuit lookup on exact EAN, supplier_code, or normalized_vendor_code (zero LLM calls).
  3. WarehouseSearchAgent execution:
     - Structured JSON query plan via LLM (brand, category, color, keywords) with 24h plan cache.
     - Deterministic relational filtering and whole-word keyword AND matching.
     - Two-phase color filter (memo cache + dictionary + lightweight LLM).
     - Strict zero-results policy on mismatch (NO FALLBACK relaxation).
  4. Snapshot persistence, multi-criteria sorting, server-side pagination, and 200-result capping.
"""
from __future__ import annotations
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from src.models.pim import ArticleBlueprint
from src.repositories.pim_repo import pim_repo
from src.agents.warehouse_search_agent import WarehouseSearchAgent
from src.core.cache import search_snapshot_cache
from src.core.logger import get_logger
from src.schemas.catalog import InterpretedPlanSummary
from src.services.catalog_query_service import (
    build_variant_rows_for_blueprints,
    sort_variant_rows,
    paginate_rows,
    DEFAULT_PAGE_SIZE,
    MAX_SEARCH_RESULTS_CAP,
)
from src.utils.text_normalize import fold_text

logger = get_logger()


def _format_search_message(
    query: str,
    count: int,
    truncated: bool = False,
    total_blueprints: int = 0,
) -> str:
    if count == 0:
        return f"Nessun articolo trovato per la ricerca «{query}»."
    trunc_info = f" (mostrati i primi {MAX_SEARCH_RESULTS_CAP} risultati più rilevanti)" if truncated else ""
    if count == 1:
        return f"Ho trovato 1 articolo corrispondente alla ricerca «{query}»{trunc_info}."
    return f"Ho trovato {count} articoli corrispondenti alla ricerca «{query}»{trunc_info}."


async def execute_search(
    db: Session,
    query: str,
    in_stock_only: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "date",
    sort_dir: str = "desc",
) -> dict:
    """
    Execute warehouse catalog search with snapshot caching, multi-criteria sorting,
    and server-side pagination.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return {
            "message": "Query di ricerca vuota.",
            "results": [],
            "total_items": 0,
            "total_blueprints": 0,
            "page": 1,
            "limit": page_size,
            "total_pages": 1,
            "interpreted_plan": None,
            "truncated": False,
        }

    logger.log_execution(
        "search_service",
        "search_start",
        "ok",
        query=clean_query,
        in_stock_only=in_stock_only,
        page=page,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )

    query_folded = fold_text(clean_query)
    snapshot_key = f"{query_folded}::{in_stock_only}"

    # ---------------------------------------------------------------------------
    # Step 1 — Check search snapshot cache
    # ---------------------------------------------------------------------------
    cached_snapshot = search_snapshot_cache.get(snapshot_key, touch=True)
    if cached_snapshot is not None:
        logger.log_execution("search_service", "snapshot_cache_hit", "ok", query=clean_query)
        candidate_entries = cached_snapshot.get("candidates", [])
        interpreted_plan = cached_snapshot.get("interpreted_plan")
        plan_summary = InterpretedPlanSummary.model_validate(interpreted_plan) if interpreted_plan else None
        truncated = cached_snapshot.get("truncated", False)

        if not candidate_entries:
            empty_msg = cached_snapshot.get("message") or f"Nessun articolo trovato per «{clean_query}»."
            return {
                "message": empty_msg,
                "results": [],
                "total_items": 0,
                "total_blueprints": 0,
                "page": 1,
                "limit": page_size,
                "total_pages": 1,
                "interpreted_plan": plan_summary,
                "truncated": False,
            }

        rows = build_variant_rows_for_blueprints(db, candidate_entries, in_stock_only=in_stock_only)

        # Decide effective sort
        has_keywords = bool(plan_summary and plan_summary.keywords)
        effective_sort_by = sort_by
        if sort_by == "date" and has_keywords:
            effective_sort_by = "relevance"
        elif sort_by == "relevance" and not has_keywords:
            effective_sort_by = "date"

        sorted_rows = sort_variant_rows(rows, sort_by=effective_sort_by, sort_dir=sort_dir)
        paginated = paginate_rows(sorted_rows, page=page, page_size=page_size)
        message = _format_search_message(clean_query, len(rows), truncated=truncated, total_blueprints=paginated["total_blueprints"])

        return {
            "message": message,
            "results": paginated["results"],
            "total_items": len(rows),
            "total_blueprints": paginated["total_blueprints"],
            "page": paginated["page"],
            "limit": paginated["limit"],
            "total_pages": paginated["total_pages"],
            "interpreted_plan": plan_summary,
            "truncated": truncated,
        }

    # ---------------------------------------------------------------------------
    # Step 2 — Tier 1 deterministic short-circuit (EAN / codes / wildcard #)
    # ---------------------------------------------------------------------------
    exact_matches = pim_repo.search_by_exact_code(db, clean_query)
    if exact_matches:
        candidate_entries = [
            {
                "id": m["blueprint_id"],
                "allowed_colors": m.get("allowed_colors"),
                "relevance_score": 100.0,
            }
            for m in exact_matches
        ]
        rows = build_variant_rows_for_blueprints(db, candidate_entries, in_stock_only=in_stock_only)
        truncated = False
        if len(rows) > MAX_SEARCH_RESULTS_CAP:
            rows = rows[:MAX_SEARCH_RESULTS_CAP]
            truncated = True

        search_snapshot_cache.set(
            snapshot_key,
            {
                "candidates": candidate_entries,
                "interpreted_plan": None,
                "truncated": truncated,
            },
        )

        sorted_rows = sort_variant_rows(rows, sort_by=sort_by, sort_dir=sort_dir)
        paginated = paginate_rows(sorted_rows, page=page, page_size=page_size)
        message = _format_search_message(clean_query, len(rows), truncated=truncated, total_blueprints=paginated["total_blueprints"])

        logger.log_execution(
            "search_service",
            "search_done",
            "ok",
            query=clean_query,
            mode="exact_code_tier1",
            count=len(rows),
        )
        return {
            "message": message,
            "results": paginated["results"],
            "total_items": len(rows),
            "total_blueprints": paginated["total_blueprints"],
            "page": paginated["page"],
            "limit": paginated["limit"],
            "total_pages": paginated["total_pages"],
            "interpreted_plan": None,
            "truncated": truncated,
        }

    # ---------------------------------------------------------------------------
    # Step 3 — WarehouseSearchAgent (Plan -> Retrieve -> Color Filter)
    # ---------------------------------------------------------------------------
    taxonomy = pim_repo.get_catalog_taxonomy_for_search(db)
    agent = WarehouseSearchAgent(db=db)
    agent_output = await agent.aexecute(
        {
            "query": clean_query,
            "in_stock_only": in_stock_only,
            "brands_catalog": taxonomy["brands"],
            "categories_catalog": taxonomy["categories"],
        }
    )

    selected_blueprints: List[Dict[str, Any]] = agent_output.get("selected_blueprints", [])
    query_plan_dict = agent_output.get("query_plan") or {}
    color_degraded = agent_output.get("color_degraded", False)

    plan_summary = InterpretedPlanSummary(
        brand=query_plan_dict.get("brand"),
        category=query_plan_dict.get("category"),
        color=query_plan_dict.get("color"),
        keywords=query_plan_dict.get("keywords") or [],
    )

    # Empty Results: Strict zero-results policy (NO FALLBACK relaxation)
    if not selected_blueprints:
        details = []
        if plan_summary.brand:
            details.append(f"brand «{plan_summary.brand}»")
        if plan_summary.category:
            details.append(f"categoria «{plan_summary.category}»")
        if plan_summary.color:
            details.append(f"colore «{plan_summary.color}»")
        if plan_summary.keywords:
            details.append(f"termini «{', '.join(plan_summary.keywords)}»")

        interp_str = f" — interpretata come: {', '.join(details)}" if details else ""
        if color_degraded:
            message = f"Nessun articolo trovato per «{clean_query}»{interp_str}. (Nota: il filtro colore non ha potuto verificare la corrispondenza)"
        else:
            message = f"Nessun articolo trovato per la ricerca «{clean_query}»{interp_str}."

        search_snapshot_cache.set(
            snapshot_key,
            {
                "candidates": [],
                "interpreted_plan": plan_summary.model_dump(),
                "truncated": False,
                "message": message,
            },
        )

        logger.log_execution(
            "search_service",
            "search_done_empty",
            "ok",
            query=clean_query,
            interpreted_plan=plan_summary.model_dump(),
        )
        return {
            "message": message,
            "results": [],
            "total_items": 0,
            "total_blueprints": 0,
            "page": 1,
            "limit": page_size,
            "total_pages": 1,
            "interpreted_plan": plan_summary,
            "truncated": False,
        }

    # Step 4 — Build rows, cap at 200, snapshot, sort & paginate
    rows = build_variant_rows_for_blueprints(db, selected_blueprints, in_stock_only=in_stock_only)
    total_matches = len(rows)
    truncated = False

    if total_matches > MAX_SEARCH_RESULTS_CAP:
        # Prioritize top relevance items before capping
        relevance_sorted = sort_variant_rows(rows, sort_by="relevance", sort_dir="desc")
        rows = relevance_sorted[:MAX_SEARCH_RESULTS_CAP]
        truncated = True

    search_snapshot_cache.set(
        snapshot_key,
        {
            "candidates": selected_blueprints,
            "interpreted_plan": plan_summary.model_dump(),
            "truncated": truncated,
        },
    )

    # Effective sort
    has_keywords = bool(plan_summary.keywords)
    effective_sort_by = sort_by
    if sort_by == "date" and has_keywords:
        effective_sort_by = "relevance"
    elif sort_by == "relevance" and not has_keywords:
        effective_sort_by = "date"

    sorted_rows = sort_variant_rows(rows, sort_by=effective_sort_by, sort_dir=sort_dir)
    paginated = paginate_rows(sorted_rows, page=page, page_size=page_size)
    message = _format_search_message(clean_query, total_matches, truncated=truncated, total_blueprints=paginated["total_blueprints"])

    logger.log_execution(
        "search_service",
        "search_done",
        "ok",
        query=clean_query,
        count=len(rows),
        total_matches=total_matches,
        truncated=truncated,
        interpreted_plan=plan_summary.model_dump(),
    )

    return {
        "message": message,
        "results": paginated["results"],
        "total_items": len(rows),
        "total_blueprints": paginated["total_blueprints"],
        "page": paginated["page"],
        "limit": paginated["limit"],
        "total_pages": paginated["total_pages"],
        "interpreted_plan": plan_summary,
        "truncated": truncated,
    }


async def execute_semantic_search(
    db: Session,
    query: str,
    in_stock_only: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "date",
    sort_dir: str = "desc",
) -> dict:
    """Backward compatibility alias for execute_search."""
    return await execute_search(
        db=db,
        query=query,
        in_stock_only=in_stock_only,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
from src.utils.vector_math import cosine_similarity as _cosine_similarity
from src.models.wms import Article, ArticleStatus
from sqlalchemy import func

def _count_available_stock(db: Session, blueprint_id: str) -> int:
    return (
        db.query(func.count(Article.id))
        .filter(
            Article.article_blueprint_id == blueprint_id,
            Article.status == ArticleStatus.AVAILABLE,
        )
        .scalar()
        or 0
    )

