"""
src/agents/sales_chat_agent/nodes/semantic_rank_node.py
-------------------------------------------------------
Node 3: Performs vector similarity ranking over RECONCILED sales candidates.
Unlike WarehouseSearchAgent, this node NEVER relaxes date or sale constraints to the full catalog:
if 0 sold items pass the similarity threshold, it returns an empty list so the synthesis node
can accurately report that no matching reconciled item was sold in the requested period.
"""
from typing import Dict, List, Any

from src.agents.llm_client import LLMClient
from src.agents.sales_chat_agent.state import SalesChatGraphState
from src.utils.vector_math import cosine_similarity
from src.core.logger import get_logger

logger = get_logger()

_HIGH_SIMILARITY_THRESHOLD = 0.65
_LOW_SIMILARITY_THRESHOLD = 0.50
_MAX_CANDIDATES = 30


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


async def semantic_rank_node(state: SalesChatGraphState) -> dict:
    """
    Filter and rank RECONCILED sales candidates by cosine similarity when requires_semantic_search is True.
    """
    plan = state.query_plan
    candidates = state.reconciled_candidates

    if not plan or plan.is_pure_chitchat or not candidates:
        logger.log_agent(
            "sales_semantic_rank_node",
            "node_passthrough_empty",
            "ok",
            candidates_count=len(candidates),
        )
        return {"ranked_reconciled": []}

    requires_semantic = bool(plan.requires_semantic_search)
    if not requires_semantic:
        ranked = candidates[:_MAX_CANDIDATES]
        logger.log_agent(
            "sales_semantic_rank_node",
            "node_exit",
            "ok",
            output={"regime": "static_only", "ranked_count": len(ranked)},
        )
        return {"ranked_reconciled": ranked}

    has_static_filters = bool(plan.brand_name or plan.category_name or plan.excel_columns)
    threshold = _HIGH_SIMILARITY_THRESHOLD if has_static_filters else _LOW_SIMILARITY_THRESHOLD
    semantic_text = (plan.semantic_query or plan.standalone_query or state.raw_query).strip()

    logger.log_agent(
        "sales_semantic_rank_node",
        "node_entry",
        "ok",
        candidates_count=len(candidates),
        semantic_text=semantic_text,
        threshold=threshold,
    )

    try:
        client = LLMClient()
        query_emb = await client.generate_embedding(semantic_text)
        ranked = _score_and_filter(candidates, query_emb, threshold)
        # If high threshold yielded 0 on a small reconciled set, check if any pass the 0.50 threshold
        # within the SAME strictly filtered date/category sales set (never relaxing SQL filters!)
        if not ranked and threshold > _LOW_SIMILARITY_THRESHOLD:
            ranked = _score_and_filter(candidates, query_emb, _LOW_SIMILARITY_THRESHOLD)
    except Exception as exc:
        logger.log_agent("sales_semantic_rank_node", "embedding_error", "err", exc=exc)
        ranked = candidates[:_MAX_CANDIDATES]

    logger.log_agent(
        "sales_semantic_rank_node",
        "node_exit",
        "ok",
        output={"regime": "semantic_strict_sales", "ranked_count": len(ranked)},
    )
    return {"ranked_reconciled": ranked}
