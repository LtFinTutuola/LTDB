"""
src/agents/data_ingestion_agent/agent.py
-----------------------------------------
DataIngestionAgent: the main LangGraph orchestrator for the DDT extraction pipeline.

Graph topology
--------------
Main graph (sequential):
  START
    → ingestion_node      (extract raw text from PDF)
    → cleanup_node        (LLM: strip noise)
    → extraction_node     (LLM: produce base_items JSON)
    → fan_out_router      (conditional edge: Send() one sub-graph per item)
    → [parallel item enrichment sub-graphs converge via operator.add]
    → END

Per-item sub-graph (parallel, one per base_item via Send):
  web_search_node
    → [should_run_macro_category conditional edge]
    → macro_category_node (or skip)
    → [should_run_sub_category conditional edge]
    → sub_category_node   (or skip)
    → fixed_fields_node
    → free_form_node
    → item_merge_node     (returns enriched_items: [merged_item])
"""
from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.data_ingestion_agent.state import GraphState, ItemState

# --- Nodes ---
from src.agents.data_ingestion_agent.nodes.ingestion_node import ingestion_node
from src.agents.data_ingestion_agent.nodes.cleanup_node import cleanup_node
from src.agents.data_ingestion_agent.nodes.extraction_node import extraction_node
from src.agents.data_ingestion_agent.nodes.web_search_node import web_search_node
from src.agents.data_ingestion_agent.nodes.macro_category_node import macro_category_node
from src.agents.data_ingestion_agent.nodes.sub_category_node import sub_category_node
from src.agents.data_ingestion_agent.nodes.fixed_fields_node import fixed_fields_node
from src.agents.data_ingestion_agent.nodes.free_form_node import free_form_node
from src.agents.data_ingestion_agent.nodes.item_merge_node import item_merge_node

# --- Edges ---
from src.agents.data_ingestion_agent.edges.main_router import fan_out_router
from src.agents.data_ingestion_agent.edges.short_circuits import (
    should_run_macro_category,
    should_run_sub_category,
)


def _build_graph() -> StateGraph:
    """Construct and compile the LangGraph state machine."""
    # ------------------------------------------------------------------ #
    # Item enrichment sub-graph (ItemState)                               #
    # ------------------------------------------------------------------ #
    item_graph = StateGraph(ItemState)
    item_graph.add_node("web_search_node", web_search_node)
    item_graph.add_node("macro_category_node", macro_category_node)
    item_graph.add_node("sub_category_node", sub_category_node)
    item_graph.add_node("fixed_fields_node", fixed_fields_node)
    item_graph.add_node("free_form_node", free_form_node)
    item_graph.add_node("item_merge_node", item_merge_node)

    item_graph.add_edge(START, "web_search_node")
    item_graph.add_conditional_edges(
        "web_search_node",
        should_run_macro_category,
        {
            "macro_category_node": "macro_category_node",
            "sub_category_node": "sub_category_node",
        },
    )
    item_graph.add_conditional_edges(
        "macro_category_node",
        should_run_sub_category,
        {
            "sub_category_node": "sub_category_node",
            "fixed_fields_node": "fixed_fields_node",
        },
    )
    item_graph.add_edge("sub_category_node", "fixed_fields_node")
    item_graph.add_edge("fixed_fields_node", "free_form_node")
    item_graph.add_edge("free_form_node", "item_merge_node")
    item_graph.add_edge("item_merge_node", END)
    
    item_subgraph = item_graph.compile()

    async def run_item_subgraph(payload: dict) -> dict:
        """Wrapper to invoke the item subgraph and filter the output.
        This prevents the subgraph from trying to concurrently update non-reducer
        keys (like 'brand') in the parent GraphState.
        """
        result = await item_subgraph.ainvoke(payload)
        # Debug why enriched_items might be empty
        enriched = result.get("enriched_items", [])
        print(f"[run_item_subgraph] Got {len(enriched)} enriched items. Result keys: {list(result.keys())}")
        return {
            "enriched_items": enriched,
            "warnings": result.get("warnings", []),
        }

    # ------------------------------------------------------------------ #
    # Main pipeline graph (GraphState)                                     #
    # ------------------------------------------------------------------ #
    main_graph = StateGraph(GraphState)

    main_graph.add_node("ingestion_node", ingestion_node)
    main_graph.add_node("cleanup_node", cleanup_node)
    main_graph.add_node("extraction_node", extraction_node)
    
    # Add the wrapper node in the main graph
    main_graph.add_node("run_item_subgraph", run_item_subgraph)

    # ------------------------------------------------------------------ #
    # Edges — main pipeline                                               #
    # ------------------------------------------------------------------ #
    main_graph.add_edge(START, "ingestion_node")
    main_graph.add_edge("ingestion_node", "cleanup_node")
    main_graph.add_edge("cleanup_node", "extraction_node")

    # Fan-out: one Send() per base_item → launches run_item_subgraph
    main_graph.add_conditional_edges("extraction_node", fan_out_router, ["run_item_subgraph"])
    
    # Fan-in automatically occurs as the output of each run_item_subgraph
    # updates the GraphState. Since run_item_subgraph returns
    # `enriched_items` (a list of 1), GraphState's operator.add reducer accumulates them.
    main_graph.add_edge("run_item_subgraph", END)

    return main_graph.compile()


class DataIngestionAgent(BaseAgent):
    """
    LangGraph-backed agent for DDT (shipment document) data extraction and enrichment.

    Expects input_data with keys:
      - file_path   (str)  : absolute path to the PDF file.
      - brand       (str)  : brand name for the shipment.
      - categories  (dict) : brand hierarchy from the DB (macro → CategoryNode structure).
      - allowed_sex (list) : allowed sex values for fixed-field mapping.
    """

    async def aexecute(self, input_data: dict) -> dict:
        """
        Run the full extraction and enrichment pipeline.

        Args:
            input_data: Dict with keys file_path, brand, categories, allowed_sex.

        Returns:
            {"items": [...], "warnings": [...]}

        Raises:
            AgentException: If a fatal error occurs (e.g. PDF unreadable, empty extraction).
        """
        # Validate and initialise state — Pydantic raises ValidationError for
        # missing/wrong-type fields, which we convert to AgentException.
        try:
            initial_state = GraphState(
                file_path=input_data["file_path"],
                brand=input_data["brand"],
                categories=input_data["categories"],
                allowed_sex=input_data.get("allowed_sex", ["Uomo", "Donna", "Unisex"]),
            )
        except (KeyError, Exception) as exc:
            raise AgentException(
                message=f"Invalid input_data for DataIngestionAgent: {exc}",
                output=None,
            ) from exc

        graph = _build_graph()

        try:
            final_state: GraphState = await graph.ainvoke(initial_state)
        except AgentException:
            # Allow AgentExceptions from nodes to propagate directly
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error: {exc}",
                output=None,
            ) from exc

        print(f"DEBUG FINAL STATE KEYS: {final_state.keys()}")
        print(f"DEBUG ENRICHED ITEMS: {final_state.get('enriched_items')}")

        return {
            "items": final_state.get("enriched_items", []),
            "warnings": final_state.get("warnings", []),
        }
