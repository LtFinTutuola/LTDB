"""
edges/main_router.py
---------------------
Fan-out router: launches one parallel item enrichment sub-graph per
base_item found in GraphState.base_items using LangGraph's Send API.

This is the Map step in the Map-Reduce pattern. Each Send creates an
independent ItemState, forwarding the read-only context fields (brand,
categories, allowed_sex) alongside the individual product item.
"""
from typing import List

from langgraph.types import Send

from src.agents.data_ingestion_agent.state import GraphState, ItemState


def fan_out_router(state: GraphState) -> List[Send]:
    """
    Convert each base_item into a parallel Send command targeting the
    item enrichment sub-graph entry point ('web_search_node').

    Args:
        state: The current GraphState after extraction_node completes.

    Returns:
        A list of Send objects — one per product item.
    """
    sends: List[Send] = []
    for item in state.base_items:
        payload = {
            "item": item,
            "brand": state.brand,
            "categories": state.categories,
            "allowed_sex": state.allowed_sex,
        }
        sends.append(Send("run_item_subgraph", payload))
    return sends
