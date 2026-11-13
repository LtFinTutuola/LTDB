"""
src/agents/photo_search_agent/agent.py
--------------------------------------
Agente autonomo LangGraph (`PhotoSearchAgent`) per la ricerca fotografica sul web.
Mantiene la separazione architetturale da `ArticleBlueprintsAgent` eseguendo al suo interno
il flusso lineare 1-shot originale (Query Gen -> Grounded Search -> HTTP Image Guard)
per ciascuna tupla `(Blueprint, color)`.
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional
from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent
from src.agents.llm_client import LLMClient
from src.agents.photo_search_agent.state import PhotoSearchItemState
from src.agents.photo_search_agent.nodes.query_gen_node import generate_query_node
from src.agents.photo_search_agent.nodes.grounded_search_node import grounded_image_search_node
from src.agents.photo_search_agent.nodes.validate_guard_node import validate_candidate_node
from src.core.logger import get_logger

logger = get_logger()
MAX_CONCURRENT_SEARCHES = 10


def _finalize_proposal_node(state: PhotoSearchItemState) -> dict:
    proposal = {
        "item_id": state.item_id,
        "photo_url": state.candidate_url if state.validation_passed else None,
        "photo_source": "web_search",
        "canonical_color_candidate": (state.color or "unknown").lower(),
        "retry_count": len(state.previous_ctx) // 2,
        "_photo_search_ctx": list(state.working_ctx),
        "search_query": state.current_query or None,
    }
    return {"proposal": proposal}


def _build_item_graph(client: Optional[LLMClient] = None):
    graph = StateGraph(PhotoSearchItemState)

    async def _qgen(s: PhotoSearchItemState) -> dict:
        return await generate_query_node(s, client=client)

    async def _gsearch(s: PhotoSearchItemState) -> dict:
        return await grounded_image_search_node(s, client=client)

    graph.add_node("generate_query_node", _qgen)
    graph.add_node("grounded_image_search_node", _gsearch)
    graph.add_node("validate_candidate_node", validate_candidate_node)
    graph.add_node("finalize_proposal_node", _finalize_proposal_node)

    graph.add_edge(START, "generate_query_node")
    graph.add_edge("generate_query_node", "grounded_image_search_node")
    graph.add_edge("grounded_image_search_node", "validate_candidate_node")
    graph.add_edge("validate_candidate_node", "finalize_proposal_node")
    graph.add_edge("finalize_proposal_node", END)

    return graph.compile()


class PhotoSearchAgent(BaseAgent):
    """
    Agente autonomo per la ricerca fotografica di articoli e varianti cromatiche.
    Accetta in `input_data`:
      - Modalità Batch (Ingestion): `items_with_blueprints` (lista di dict `{item, blueprint}`), `brand_name`.
      - Modalità Singola (Retry / Catalogo / Revisione): campi diretti di `PhotoSearchItemState`.
    """

    def __init__(self, llm_client: Optional[LLMClient] = None):
        super().__init__()
        self._llm_client = llm_client

    async def search_single_item(
        self,
        item_input: dict[str, Any],
        sem: Optional[asyncio.Semaphore] = None,
    ) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            color_str = str(item_input.get("color") or "unknown")
            previous_ctx = list(item_input.get("previous_ctx") or item_input.get("_photo_search_ctx") or [])
            mode = str(item_input.get("mode") or "")
            if not mode:
                if previous_ctx:
                    mode = "retry"
                elif item_input.get("user_feedback"):
                    mode = "catalog"
                else:
                    mode = "ingestion"

            try:
                initial_state = PhotoSearchItemState(
                    item_id=str(item_input.get("item_id") or ""),
                    brand_name=str(item_input.get("brand_name") or ""),
                    vendor_code=str(item_input.get("vendor_code") or ""),
                    color=color_str,
                    article_name=str(item_input.get("article_name") or ""),
                    description=str(item_input.get("description") or ""),
                    user_feedback=item_input.get("user_feedback"),
                    previous_ctx=previous_ctx,
                    mode=mode,
                )
                compiled = _build_item_graph(client=self._llm_client)
                final_state = await compiled.ainvoke(initial_state)
                proposal = (
                    final_state.get("proposal", {})
                    if isinstance(final_state, dict)
                    else getattr(final_state, "proposal", {})
                )
                return proposal
            except Exception as exc:
                logger.log_agent("photo_search_agent", "search_failed", "warn", exc=str(exc))
                return {
                    "item_id": item_input.get("item_id"),
                    "photo_url": None,
                    "photo_source": "web_search",
                    "canonical_color_candidate": color_str.lower(),
                    "retry_count": 0,
                    "_photo_search_ctx": previous_ctx,
                    "search_query": None,
                }

        if sem is not None:
            async with sem:
                return await _run()
        return await _run()

    async def _aexecute(self, input_data: dict) -> dict:
        client = self._llm_client or LLMClient()
        self._llm_client = client

        # Modalità Batch (usata in data_ingestion_service)
        if "items_with_blueprints" in input_data:
            entries: List[dict] = input_data.get("items_with_blueprints") or []
            brand_name = input_data.get("brand_name", "")

            sem = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
            tasks = []
            for entry in entries:
                item = entry.get("item") or {}
                bp = entry.get("blueprint") or {}
                colors = item.get("colors") or []
                if isinstance(colors, str):
                    colors = [colors] if colors.strip() else []
                color_str = str(colors[0]) if colors else "unknown"

                single_payload = {
                    "item_id": item.get("item_id", ""),
                    "brand_name": brand_name,
                    "vendor_code": item.get("vendor_code") or item.get("VendorCode") or "",
                    "color": color_str,
                    "article_name": bp.get("article_name") or item.get("article_name") or "",
                    "description": bp.get("description") or item.get("description") or "",
                    "previous_ctx": item.get("_photo_search_ctx") or [],
                    "mode": entry.get("mode") or "ingestion",
                }
                tasks.append(self.search_single_item(single_payload, sem=sem))

            proposals = await asyncio.gather(*tasks)
            return {
                "photo_proposals": [p for p in proposals if p is not None],
            }

        # Modalità Singola (usata in retry_photo_search, search_and_set_catalog_photo, blueprint_revision)
        proposal = await self.search_single_item(input_data)
        return {
            "proposal": proposal,
            "photo_proposals": [proposal],
        }
