"""
src/agents/warehouse_search_agent/nodes/retrieve_node.py
--------------------------------------------------------
Node 2: Deterministic retrieval executing:
  1. Relational filters on Brand and Category (with subcategory hierarchy expansion).
  2. Stock filtering if in_stock_only=True.
  3. Strict Python keyword AND matching at whole-word boundaries with diacritics folding
     and Italian singular/plural variance.
  4. Deterministic lexical relevance scoring.
"""
from __future__ import annotations
from typing import List, Dict, Any, Set, Optional
from collections import defaultdict
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from src.models.pim import ArticleBlueprint, Brand, Category
from src.models.wms import Article, ArticleStatus
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState
from src.utils.text_normalize import fold_text, contains_whole_word
from src.core.logger import get_logger

logger = get_logger()


async def retrieve_node(state: WarehouseSearchGraphState, db: Session) -> dict:
    """
    Retrieve candidate blueprints matching the structured search query plan.
    """
    plan = state.query_plan
    brand_name = plan.brand if plan else None
    category_name = plan.category if plan else None
    keywords = plan.keywords if plan else []

    logger.log_agent(
        "retrieve_node",
        "node_entry",
        "ok",
        brand=brand_name,
        category=category_name,
        keywords=keywords,
        in_stock_only=state.in_stock_only,
    )

    query = (
        db.query(ArticleBlueprint)
        .options(
            joinedload(ArticleBlueprint.brand),
            joinedload(ArticleBlueprint.category),
        )
    )

    # 1. Hard Brand Filter
    if brand_name and brand_name.strip():
        clean_brand = brand_name.strip()
        brand_obj = db.query(Brand).filter(func.lower(Brand.name) == clean_brand.lower()).first()
        if brand_obj:
            query = query.filter(ArticleBlueprint.brand_id == brand_obj.id)
        else:
            # Fallback to folded comparison across brands
            all_brands = db.query(Brand).all()
            matched_b = next((b for b in all_brands if fold_text(b.name) == fold_text(clean_brand)), None)
            if matched_b:
                query = query.filter(ArticleBlueprint.brand_id == matched_b.id)
            else:
                logger.log_agent("retrieve_node", "brand_not_found", "ok", brand=brand_name)
                return {"retrieved_blueprints": [], "sql_candidates": [], "ranked_candidates": []}

    # 2. Hard Category Filter (Macro + Subcategories)
    if category_name and category_name.strip():
        clean_cat = category_name.strip()
        matched_cats = (
            db.query(Category)
            .filter(func.lower(Category.name) == clean_cat.lower())
            .all()
        )
        if not matched_cats:
            all_cats = db.query(Category).all()
            clean_folded = fold_text(clean_cat)
            matched_cats = [c for c in all_cats if fold_text(c.name) == clean_folded]

        if not matched_cats:
            logger.log_agent("retrieve_node", "category_not_found", "ok", category=category_name)
            return {"retrieved_blueprints": [], "sql_candidates": [], "ranked_candidates": []}

        target_cat_ids: Set[str] = set()
        for cat in matched_cats:
            target_cat_ids.add(str(cat.id))
            if cat.parent_id is None:
                # Expand macro category to all child subcategories
                children = db.query(Category).filter(Category.parent_id == cat.id).all()
                for ch in children:
                    target_cat_ids.add(str(ch.id))

        query = query.filter(ArticleBlueprint.category_id.in_(list(target_cat_ids)))

    # Execute relational retrieval without artificial limit
    blueprints: List[ArticleBlueprint] = query.all()
    if not blueprints:
        logger.log_agent("retrieve_node", "no_blueprints_after_sql", "ok")
        return {"retrieved_blueprints": [], "sql_candidates": [], "ranked_candidates": []}

    # 3. Batch load all related articles for candidate blueprints
    bp_ids = [bp.id for bp in blueprints]
    all_articles = db.query(Article).filter(Article.article_blueprint_id.in_(bp_ids)).all()
    articles_by_bp: Dict[str, List[Article]] = defaultdict(list)
    for a in all_articles:
        articles_by_bp[str(a.article_blueprint_id)].append(a)

    surviving_candidates: List[Dict[str, Any]] = []

    for bp in blueprints:
        bp_id_str = str(bp.id)
        articles = articles_by_bp.get(bp_id_str, [])

        # Filter by stock if requested
        if state.in_stock_only:
            eligible_articles = [a for a in articles if a.status == ArticleStatus.AVAILABLE]
            if not eligible_articles:
                continue
        else:
            eligible_articles = articles

        # Collect distinct available colors for this blueprint
        available_colors: Set[str] = set()
        for a in eligible_articles:
            for c in (a.colors or []):
                if c and str(c).strip():
                    available_colors.add(str(c).strip().lower())

        # 4. Keyword AND Filtering at whole-word level
        if keywords:
            # Prepare searchable combined text
            combined_tokens = [
                bp.article_name or "",
                bp.description or "",
                bp.extended_description or "",
                bp.normalized_vendor_code or "",
            ]
            if bp.tags:
                combined_tokens.extend([str(t) for t in bp.tags])
            if bp.materials:
                combined_tokens.extend([str(m) for m in bp.materials])
            for a in articles:
                if a.supplier_code:
                    combined_tokens.append(a.supplier_code)
                if a.ean:
                    combined_tokens.append(a.ean)

            combined_text = " ".join(combined_tokens)

            # ALL keywords must match (AND condition)
            kw_match = True
            for kw in keywords:
                if not contains_whole_word(kw, combined_text):
                    kw_match = False
                    break

            if not kw_match:
                continue

        # 5. Deterministic Relevance Scoring
        score = 0.0
        if keywords:
            name_folded = fold_text(bp.article_name or "")
            desc_folded = fold_text(bp.description or "")
            ext_desc_folded = fold_text(bp.extended_description or "")
            code_folded = fold_text(bp.normalized_vendor_code or "")
            tags_folded = [fold_text(str(t)) for t in (bp.tags or [])]
            mat_folded = [fold_text(str(m)) for m in (bp.materials or [])]

            all_in_name = True
            for kw in keywords:
                kw_folded = fold_text(kw)
                if code_folded and kw_folded in code_folded:
                    score += 12.0 if kw_folded == code_folded else 8.0

                if contains_whole_word(kw_folded, name_folded):
                    score += 10.0
                else:
                    all_in_name = False

                if any(contains_whole_word(kw_folded, t) for t in tags_folded):
                    score += 5.0
                if any(contains_whole_word(kw_folded, m) for m in mat_folded):
                    score += 5.0
                if contains_whole_word(kw_folded, desc_folded):
                    score += 3.0
                if contains_whole_word(kw_folded, ext_desc_folded):
                    score += 1.0

            if len(keywords) > 1 and all_in_name:
                score += 5.0

            kw_phrase = " ".join([fold_text(k) for k in keywords])
            if kw_phrase and kw_phrase in name_folded:
                score += 3.0

        surviving_candidates.append(
            {
                "id": bp_id_str,
                "article_name": bp.article_name,
                "available_colors": sorted(list(available_colors)),
                "relevance_score": score,
                "similarity_score": score,  # alias for backward compat
                "blueprint": bp,
            }
        )

    # Sort candidates by relevance score descending
    surviving_candidates.sort(key=lambda c: c["relevance_score"], reverse=True)

    result = {
        "retrieved_blueprints": surviving_candidates,
        "sql_candidates": surviving_candidates,
        "ranked_candidates": surviving_candidates,
    }

    logger.log_agent(
        "retrieve_node",
        "node_exit",
        "ok",
        surviving_count=len(surviving_candidates),
    )
    return result
