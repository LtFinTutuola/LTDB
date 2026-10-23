"""
src/agents/warehouse_search_agent/nodes/semantic_rank_node.py
-------------------------------------------------------------
Node 3: Performs vector similarity ranking over candidate blueprints with a two-regime threshold:
  - Primary regime (when static SQL filters are active and yield candidates):
    Uses `semantic_query` (Option A) and `_HIGH_SIMILARITY_THRESHOLD` (0.65).
  - Fallback regime (when static SQL filters return 0 candidates, or 0 candidates pass the high
    threshold, or no static filters were active):
    Relaxes SQL filters to the full catalog, embeds `raw_query`, and filters with
    `_LOW_SIMILARITY_THRESHOLD` (0.50).
"""
import math
from typing import Dict, List, Any
from sqlalchemy.orm import Session

from src.agents.llm_client import LLMClient
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState
from src.repositories.pim_repo import pim_repo
from src.core.logger import get_logger

logger = get_logger()

_HIGH_SIMILARITY_THRESHOLD = 0.65
_LOW_SIMILARITY_THRESHOLD = 0.50
_MAX_CANDIDATES = 20


from src.utils.vector_math import cosine_similarity


def _score_and_filter(
    candidates: List[Dict[str, Any]],
    query_embedding: List[float],
    threshold: float,
) -> List[Dict[str, Any]]:
    scored: List[tuple[float, Dict[str, Any]]] = []
    for cand in candidates:
        stored_emb = cand.get("embedding")
        if not stored_emb:
            continue
        score = cosine_similarity(query_embedding, stored_emb)
        if score >= threshold:
            cand_with_score = dict(cand)
            cand_with_score["similarity_score"] = round(score, 4)
            scored.append((score, cand_with_score))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:_MAX_CANDIDATES]]


async def semantic_rank_node(state: WarehouseSearchGraphState, db: Session) -> dict:
    """
    Execute semantic similarity ranking with automatic fallback relaxation on zero results.
    """
    plan = state.query_plan
    has_static_filters = bool(plan and (plan.brand_name or plan.category_name))
    requires_semantic = bool(plan.requires_semantic_search) if plan else True
    semantic_text = (plan.semantic_query if plan and plan.semantic_query else state.raw_query).strip()

    logger.log_agent(
        "semantic_rank_node",
        "node_entry",
        "ok",
        sql_candidates_count=len(state.sql_candidates),
        has_static_filters=has_static_filters,
        requires_semantic=requires_semantic,
        semantic_text=semantic_text,
    )

    # Case 1: Pure static filter query that returned >= 1 SQL candidates
    if not requires_semantic and len(state.sql_candidates) > 0:
        ranked = state.sql_candidates[:_MAX_CANDIDATES]
        result = {"ranked_candidates": ranked, "fallback_triggered": False}
        logger.log_agent(
            "semantic_rank_node",
            "node_exit",
            "ok",
            output={
                "regime": "static_only",
                "ranked_count": len(ranked),
                "fallback_triggered": False,
            },
        )
        return result

    client = LLMClient()

    # Case 2: Primary regime — SQL candidates exist and semantic search is required
    if len(state.sql_candidates) > 0 and requires_semantic:
        threshold = _HIGH_SIMILARITY_THRESHOLD if has_static_filters else _LOW_SIMILARITY_THRESHOLD
        try:
            query_emb = await client.generate_embedding(semantic_text)
            ranked = _score_and_filter(state.sql_candidates, query_emb, threshold)
        except Exception as exc:
            logger.log_agent("semantic_rank_node", "primary_embedding_error", "err", exc=exc)
            # Graceful degradation: if static filters were active, keep SQL candidates; else empty
            ranked = state.sql_candidates[:_MAX_CANDIDATES] if has_static_filters else []
            result = {"ranked_candidates": ranked, "fallback_triggered": False}
            logger.log_agent(
                "semantic_rank_node",
                "node_exit",
                "ok",
                output={"regime": "embedding_error_degraded", "ranked_count": len(ranked)},
            )
            return result

        if len(ranked) > 0 or not has_static_filters:
            result = {"ranked_candidates": ranked, "fallback_triggered": False}
            logger.log_agent(
                "semantic_rank_node",
                "node_exit",
                "ok",
                output={
                    "regime": "primary_semantic",
                    "threshold": threshold,
                    "ranked_count": len(ranked),
                    "fallback_triggered": False,
                },
            )
            return result

    # Case 3: Fallback regime — SQL filters yielded 0 candidates OR 0 passed _HIGH_SIMILARITY_THRESHOLD.
    # Preserve brand filter in fallback if user explicitly searched for a brand!
    brand_fallback = plan.brand_name if plan and plan.brand_name else None
    fallback_candidates = []
    if brand_fallback:
        fallback_candidates = pim_repo.get_candidate_blueprints_by_sql_plan(
            db=db,
            brand_name=brand_fallback,
            category_name=None,
            in_stock_only=state.in_stock_only,
        )

    if not fallback_candidates:
        fallback_candidates = pim_repo.get_candidate_blueprints_by_sql_plan(
            db=db,
            brand_name=None,
            category_name=None,
            in_stock_only=state.in_stock_only,
        )

    if not fallback_candidates:
        result = {"ranked_candidates": [], "fallback_triggered": True}
        logger.log_agent(
            "semantic_rank_node",
            "node_exit",
            "ok",
            output={"regime": "fallback_empty_catalog", "ranked_count": 0, "fallback_triggered": True},
        )
        return result

    try:
        fallback_emb = await client.generate_embedding(state.raw_query)
        ranked_fallback = _score_and_filter(
            fallback_candidates,
            fallback_emb,
            _LOW_SIMILARITY_THRESHOLD,
        )
    except Exception as exc:
        logger.log_agent("semantic_rank_node", "fallback_embedding_error", "err", exc=exc)
        ranked_fallback = []

    result = {"ranked_candidates": ranked_fallback, "fallback_triggered": True}
    logger.log_agent(
        "semantic_rank_node",
        "node_exit",
        "ok",
        output={
            "regime": "fallback_low_threshold",
            "threshold": _LOW_SIMILARITY_THRESHOLD,
            "ranked_count": len(ranked_fallback),
            "fallback_triggered": True,
        },
    )
    return result
