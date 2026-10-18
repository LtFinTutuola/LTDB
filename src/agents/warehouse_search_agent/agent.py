"""
src/agents/warehouse_search_agent/agent.py
------------------------------------------
LangGraph state machine for the WarehouseSearchAgent.
Orchestrates:
  1. query_plan_node     (LLM structured JSON query plan)
  2. sql_filter_node     (Deterministic SQLAlchemy candidate retrieval via pim_repo)
  3. semantic_rank_node  (Dual-regime cosine similarity + zero-results fallback relaxation)
  4. color_filter_node   (Dedicated LLM color variant selection per blueprint)
"""
from functools import partial
from sqlalchemy.orm import Session
from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState
from src.agents.warehouse_search_agent.nodes.query_plan_node import query_plan_node
from src.agents.warehouse_search_agent.nodes.sql_filter_node import sql_filter_node
from src.agents.warehouse_search_agent.nodes.semantic_rank_node import semantic_rank_node
from src.agents.warehouse_search_agent.nodes.color_filter_node import color_filter_node


def _build_graph(db: Session):
    """Construct and compile the WarehouseSearchAgent LangGraph state machine."""
    graph = StateGraph(WarehouseSearchGraphState)

    graph.add_node("query_plan_node", query_plan_node)
    graph.add_node("sql_filter_node", partial(sql_filter_node, db=db))
    graph.add_node("semantic_rank_node", partial(semantic_rank_node, db=db))
    graph.add_node("color_filter_node", color_filter_node)

    graph.add_edge(START, "query_plan_node")
    graph.add_edge("query_plan_node", "sql_filter_node")
    graph.add_edge("sql_filter_node", "semantic_rank_node")
    graph.add_edge("semantic_rank_node", "color_filter_node")
    graph.add_edge("color_filter_node", END)

    return graph.compile()


class WarehouseSearchAgent(BaseAgent):
    """
    Hybrid RAG search agent for the LTDB warehouse catalog.
    Converts a natural language query into a structured SQL query plan,
    applies dual-regime semantic similarity ranking with zero-results relaxation,
    and filters color variants via a dedicated LLM pass.
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
            fallback_triggered = final_state.get("fallback_triggered", False)
        else:
            selected_blueprints = getattr(final_state, "selected_blueprints", [])
            query_plan = getattr(final_state, "query_plan", None)
            fallback_triggered = getattr(final_state, "fallback_triggered", False)

        plan_dict = query_plan.model_dump() if hasattr(query_plan, "model_dump") else query_plan

        return {
            "selected_blueprints": selected_blueprints,
            "query_plan": plan_dict,
            "fallback_triggered": fallback_triggered,
        }
