"""
src/agents/warehouse_search_agent/agent.py
------------------------------------------
LangGraph state machine for the WarehouseSearchAgent.
Orchestrates:
  1. query_plan_node   (LLM structured JSON query plan with cache & deterministic fallback)
  2. retrieve_node     (Deterministic SQL filters + strict keyword AND matching + relevance scoring)
  3. color_filter_node (Two-phase color variant selection with cache & index-based LLM)
"""
from functools import partial
from sqlalchemy.orm import Session
from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState
from src.agents.warehouse_search_agent.nodes.query_plan_node import query_plan_node
from src.agents.warehouse_search_agent.nodes.retrieve_node import retrieve_node
from src.agents.warehouse_search_agent.nodes.color_filter_node import color_filter_node


def _build_graph(db: Session):
    """Construct and compile the WarehouseSearchAgent LangGraph state machine."""
    graph = StateGraph(WarehouseSearchGraphState)

    graph.add_node("query_plan_node", query_plan_node)
    graph.add_node("retrieve_node", partial(retrieve_node, db=db))
    graph.add_node("color_filter_node", color_filter_node)

    graph.add_edge(START, "query_plan_node")
    graph.add_edge("query_plan_node", "retrieve_node")
    graph.add_edge("retrieve_node", "color_filter_node")
    graph.add_edge("color_filter_node", END)

    return graph.compile()


class WarehouseSearchAgent(BaseAgent):
    """
    Search agent for the LTDB warehouse catalog.
    Converts a natural language query into a structured query plan,
    retrieves candidate blueprints deterministically using hard filters and keyword AND matching,
    and resolves color compatibility via a lightweight two-phase color filter.
    """

    def __init__(self, db: Session) -> None:
        super().__init__()
        self.db = db

    async def _aexecute(self, input_data: dict) -> dict:
        try:
            initial_state = WarehouseSearchGraphState(
                raw_query=input_data.get("query", ""),
                in_stock_only=bool(input_data.get("in_stock_only", False)),
                brands_catalog=input_data.get("brands_catalog", []),
                categories_catalog=input_data.get("categories_catalog", {}),
            )
        except Exception as exc:
            raise AgentException(
                message=f"Invalid input_data for WarehouseSearchAgent: {exc}",
                output=None,
            ) from exc

        if not initial_state.raw_query or not initial_state.raw_query.strip():
            raise AgentException(
                message="WarehouseSearchAgent requires a non-empty 'query' string.",
                output=None,
            )

        graph = _build_graph(self.db)

        try:
            final_state = await graph.ainvoke(initial_state)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error in WarehouseSearchAgent: {exc}",
                output=None,
            ) from exc

        if isinstance(final_state, dict):
            selected_blueprints = final_state.get("selected_blueprints", [])
            query_plan = final_state.get("query_plan")
            color_degraded = final_state.get("color_degraded", False)
        else:
            selected_blueprints = getattr(final_state, "selected_blueprints", [])
            query_plan = getattr(final_state, "query_plan", None)
            color_degraded = getattr(final_state, "color_degraded", False)

        plan_dict = query_plan.model_dump() if hasattr(query_plan, "model_dump") else query_plan

        return {
            "selected_blueprints": selected_blueprints,
            "query_plan": plan_dict,
            "color_degraded": color_degraded,
            "fallback_triggered": False,  # Strict: no fallback!
        }
