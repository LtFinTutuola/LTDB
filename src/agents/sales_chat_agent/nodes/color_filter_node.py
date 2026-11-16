"""
src/agents/sales_chat_agent/nodes/color_filter_node.py
------------------------------------------------------
Node 4: Evaluates color compatibility strictly against the colors of the physical Article sold
in each RECONCILED ExcelSale row.
"""
import json
from typing import Dict, List, Any

from src.agents.llm_client import LLMClient
from src.agents.sales_chat_agent.state import (
    SalesChatGraphState,
    SalesColorFilterResponse,
)
from src.core.logger import get_logger

logger = get_logger()

from src.utils.llm_models import SALES_CHAT_COLOR_FILTER_MODEL as _MODEL
from src.utils.prompts import SALES_CHAT_COLOR_FILTER_SYSTEM_PROMPT as _SYSTEM_PROMPT


def _strip_embedding(cand: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in cand.items() if k != "embedding"}


async def color_filter_node(state: SalesChatGraphState) -> dict:
    """
    Filter ranked RECONCILED sales by the actual color of the sold physical Article.
    """
    candidates = state.ranked_reconciled
    plan = state.query_plan

    if not candidates or not plan or plan.is_pure_chitchat:
        return {"filtered_reconciled": []}

    filtered_text = (plan.semantic_query or "").strip()
    has_any_colors = any(bool(c.get("available_colors")) for c in candidates)

    if not filtered_text or not has_any_colors:
        cleaned = [_strip_embedding(c) for c in candidates]
        logger.log_agent(
            "sales_color_filter_node",
            "node_exit",
            "ok",
            output={"mode": "passthrough_no_color_context", "count": len(cleaned)},
        )
        return {"filtered_reconciled": cleaned}

    payload = [
        {
            "sale_id": c["sale_id"],
            "article_name": c.get("article_name", ""),
            "available_colors": c.get("available_colors", []),
        }
        for c in candidates
    ]

    user_prompt = (
        f"Testo ricerca (`filtered_text`): \"{filtered_text}\"\n\n"
        f"Articoli venduti candidati e relativi colori:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Verifica la compatibilità cromatica per ciascuna vendita."
    )

    try:
        client = LLMClient()
        raw_json = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=user_prompt,
            pipeline_stage="sales_color_filter",
            response_schema=SalesColorFilterResponse,
            response_mime_type="application/json",
            temperature=0.0,
        )
        color_res = SalesColorFilterResponse.model_validate_json(raw_json)

        if not color_res.has_color_constraint:
            cleaned = [_strip_embedding(c) for c in candidates]
            return {"filtered_reconciled": cleaned}

        selection_map: Dict[str, List[str]] = {
            sel.sale_id: [col.lower() for col in sel.selected_colors if col]
            for sel in color_res.selections
        }

        matched: List[Dict[str, Any]] = []
        for c in candidates:
            s_id = c["sale_id"]
            if selection_map.get(s_id):
                matched.append(_strip_embedding(c))

        logger.log_agent(
            "sales_color_filter_node",
            "node_exit",
            "ok",
            output={"has_color_constraint": True, "matched_count": len(matched)},
        )
        return {"filtered_reconciled": matched}

    except Exception as exc:
        logger.log_agent("sales_color_filter_node", "color_llm_error", "err", exc=exc)
        return {"filtered_reconciled": [_strip_embedding(c) for c in candidates]}
