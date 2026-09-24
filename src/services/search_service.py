"""
src/services/search_service.py
-------------------------------
Semantic search service for the LTDB catalog.

Supports two search modes selected by an LLM routing step:
  - filter:     SQL query with conditions extracted from the natural language query.
  - similarity: Cosine similarity over stored ArticleBlueprint embedding vectors.

The endpoint returns a flat list of SearchResultItem dicts plus a human-readable
chat message.
"""
from __future__ import annotations
import math
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.pim import ArticleBlueprint, Brand, Category, ArticlePhoto
from src.models.wms import Article, ArticleStatus
from src.repositories.pim_repo import pim_repo
from src.schemas.catalog import SearchInterpretation, SemanticSearchResponse, SearchResultItem
from src.agents.llm_client import LLMClient
from src.core.logger import get_logger

logger = get_logger()

_SEARCH_SYSTEM_PROMPT = """
You are a search query interpreter for a luxury fashion product database (PIM/WMS).
Given a user natural-language query in any language, extract structured search parameters.

Return a JSON object with:
- "mode": use "filter" when the query contains explicit, concrete parameters (brand,
  category, color, availability status, or specific tags/materials);
  use "similarity" for vague, conceptual, or descriptive queries that need semantic matching.
- "brand_name": brand name if explicitly mentioned (e.g. "Gucci", "Prada"), otherwise null.
- "category_name": product category if mentioned (e.g. "borse", "scarpe", "cinture", "bags"), otherwise null.
- "colors": list of color strings if mentioned (e.g. ["rosso", "nero"]), otherwise null.
- "status": one of "Available", "Sold", "Lost" if mentioned, otherwise null.
- "tags": list of attribute keywords if clearly present (e.g. ["pelle", "sera"]), otherwise null.
- "free_text": the full original query text (always populated, used as embedding input).

Examples:
- "borse rosse disponibili" → {"mode":"filter","category_name":"borse","colors":["rosso"],"status":"Available","free_text":"borse rosse disponibili"}
- "articoli in pelle di Gucci" → {"mode":"filter","brand_name":"Gucci","tags":["pelle"],"free_text":"articoli in pelle di Gucci"}
- "qualcosa di elegante per una serata" → {"mode":"similarity","free_text":"qualcosa di elegante per una serata"}
- "show me sold items" → {"mode":"filter","status":"Sold","free_text":"show me sold items"}
"""

_MAX_RESULTS = 20


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_semantic_search(db: Session, query: str) -> dict:
    """
    Interpret a natural language query and return matching catalog results.

    Returns:
        dict matching SemanticSearchResponse schema.
    """
    logger.log_execution("search_service", "search_start", "ok", query=query)

    # Step 1 — LLM routing
    interpretation = await _interpret_query(query)

    # Step 2 — Execute search
    if interpretation.mode == "filter":
        results = _filter_search(db, interpretation)
    else:
        results = await _similarity_search(db, interpretation.free_text or query)

    count = len(results)
    if count == 0:
        message = f"Nessun articolo trovato per la ricerca \u00ab{query}\u00bb."
    elif count == 1:
        message = f"Ho trovato 1 articolo corrispondente alla ricerca \u00ab{query}\u00bb."
    else:
        message = f"Ho trovato {count} articoli corrispondenti alla ricerca \u00ab{query}\u00bb."

    logger.log_execution("search_service", "search_done", "ok",
                         query=query, mode=interpretation.mode, count=count)
    return {"message": message, "results": results}


# ---------------------------------------------------------------------------
# LLM routing
# ---------------------------------------------------------------------------

async def _interpret_query(query: str) -> SearchInterpretation:
    """Call the LLM to classify the query and extract structured filters."""
    try:
        client = LLMClient()
        raw = await client.call(
            model_name="gemini-2.0-flash-lite",
            system_prompt=_SEARCH_SYSTEM_PROMPT,
            prompt=query,
            pipeline_stage="search_interpretation",
            response_schema=SearchInterpretation,
            response_mime_type="application/json",
        )
        return SearchInterpretation.model_validate_json(raw)
    except Exception as exc:
        logger.log_execution("search_service", "interpretation_error", "err", exc=exc)
        # Graceful fallback: treat as similarity search with the raw query
        return SearchInterpretation(mode="similarity", free_text=query)


# ---------------------------------------------------------------------------
# Filter-based search
# ---------------------------------------------------------------------------

def _filter_search(db: Session, interp: SearchInterpretation) -> list[dict]:
    """Build a dynamic SQL query from structured filter parameters."""
    query = db.query(ArticleBlueprint).join(Brand, ArticleBlueprint.brand_id == Brand.id)

    if interp.brand_name:
        query = query.filter(Brand.name.ilike(f"%{interp.brand_name}%"))

    if interp.category_name:
        query = query.join(Category, ArticleBlueprint.category_id == Category.id).filter(
            Category.name.ilike(f"%{interp.category_name}%")
        )

    blueprints: list[ArticleBlueprint] = query.limit(200).all()

    results: list[dict] = []
    for bp in blueprints:
        # --- Tag filter (Python-side, JSON field) ---
        if interp.tags:
            bp_tags = [t.lower() for t in (bp.tags or [])]
            if not any(t.lower() in bp_tags for t in interp.tags):
                continue

        variant_items = _build_variant_items(db, bp, interp)
        results.extend(variant_items)
        if len(results) >= _MAX_RESULTS:
            results = results[:_MAX_RESULTS]
            break

    return results


# ---------------------------------------------------------------------------
# Similarity-based search
# ---------------------------------------------------------------------------

async def _similarity_search(db: Session, free_text: str) -> list[dict]:
    """Generate a query embedding and rank blueprints by cosine similarity."""
    all_embeddings = pim_repo.get_all_embeddings(db)

    if not all_embeddings:
        logger.log_execution("search_service", "similarity_no_embeddings", "ok")
        return []

    try:
        client = LLMClient()
        query_embedding: list[float] = await client.generate_embedding(free_text)
    except Exception as exc:
        logger.log_execution("search_service", "embedding_generation_error", "err", exc=exc)
        return []

    # Score all blueprints
    scored: list[tuple[float, str]] = []
    for bp_data in all_embeddings:
        stored = bp_data.get("embedding")
        if not stored:
            continue
        score = _cosine_similarity(query_embedding, stored)
        scored.append((score, bp_data["id"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_ids = [bp_id for _, bp_id in scored[:_MAX_RESULTS]]

    results: list[dict] = []
    for bp_id in top_ids:
        bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == bp_id).first()
        if not bp:
            continue
        variant_items = _build_variant_items(db, bp)
        results.extend(variant_items)
        if len(results) >= _MAX_RESULTS:
            results = results[:_MAX_RESULTS]
            break

    return results


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _count_available_stock(db: Session, blueprint_id: str) -> int:
    """Count Article records with status AVAILABLE for a given blueprint."""
    from src.models.wms import Article, ArticleStatus
    return db.query(func.count(Article.id)).filter(
        Article.article_blueprint_id == blueprint_id,
        Article.status == ArticleStatus.AVAILABLE,
    ).scalar() or 0

def _build_variant_items(db: Session, bp: ArticleBlueprint, interp: Optional[SearchInterpretation] = None) -> list[dict]:
    """Assemble SearchResultItem dicts from an ArticleBlueprint, one per color variant."""
    from src.models.wms import Article, ArticleStatus
    
    all_articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
    
    colors_dict = {}
    for a in all_articles:
        color_keys = [c.lower() for c in (a.colors or [])]
        if not color_keys:
            color_keys = ["unknown"]
        for c in color_keys:
            if c not in colors_dict:
                colors_dict[c] = []
            colors_dict[c].append(a)
    
    if not colors_dict:
        colors_dict["unknown"] = []
        
    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
    category = db.query(Category).filter(Category.id == bp.category_id).first() if bp.category_id else None

    results = []
    for color, articles in colors_dict.items():
        filtered = articles
        if interp:
            if interp.colors:
                if not any(color == c.lower() for c in interp.colors):
                    continue
            if interp.status:
                filtered = [a for a in filtered if a.status.value == interp.status]
                if interp.status and not filtered and articles:
                    continue
                    
        stock = len([a for a in filtered if a.status == ArticleStatus.AVAILABLE])
        
        photo = db.query(ArticlePhoto).filter(
            ArticlePhoto.article_blueprint_id == bp.id,
            ArticlePhoto.canonical_color_name == color
        ).first()
        if not photo:
            photo = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp.id).first()
            
        name = f"{bp.article_name} - {color.capitalize()}" if color and color != "unknown" else bp.article_name
        
        results.append({
            "blueprint_id": bp.id,
            "article_name": name,
            "brand_name": brand.name if brand else "Unknown",
            "category_name": category.name if category else None,
            "description": bp.description,
            "photo_id": photo.id if photo else None,
            "colors": [color] if color != "unknown" else [],
            "tags": list(bp.tags or []),
            "materials": list(bp.materials or []),
            "stock": stock,
        })
    return results
