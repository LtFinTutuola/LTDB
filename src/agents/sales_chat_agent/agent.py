"""
src/agents/sales_chat_agent/agent.py
------------------------------------
LangGraph state machine for SalesConversationalAgent.
Orchestrates:
  1. query_plan_node        (Contextual LLM structured JSON query plan with chat_history + columns.yaml + taxonomy)
  2. sql_and_stats_node     (Deterministic ExcelSale filtering + statistical comparison tool)
  3. semantic_rank_node     (Strict cosine similarity over RECONCILED sold items)
  4. color_filter_node      (Color verification over sold physical Article colors)
  5. answer_synthesis_node  (Conversational response generation in Italian)
"""
from functools import partial
from sqlalchemy.orm import Session
from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.sales_chat_agent.state import SalesChatGraphState
from src.agents.sales_chat_agent.nodes.query_plan_node import query_plan_node
from src.agents.sales_chat_agent.nodes.sql_and_stats_node import sql_and_stats_node
from src.agents.sales_chat_agent.nodes.semantic_rank_node import semantic_rank_node
from src.agents.sales_chat_agent.nodes.color_filter_node import color_filter_node
from src.agents.sales_chat_agent.nodes.answer_synthesis_node import answer_synthesis_node


def _build_graph(db: Session):
    """Construct and compile the SalesConversationalAgent LangGraph state machine."""
    graph = StateGraph(SalesChatGraphState)

    graph.add_node("query_plan_node", query_plan_node)
    graph.add_node("sql_and_stats_node", partial(sql_and_stats_node, db=db))
    graph.add_node("semantic_rank_node", semantic_rank_node)
    graph.add_node("color_filter_node", color_filter_node)
    graph.add_node("answer_synthesis_node", answer_synthesis_node)

    graph.add_edge(START, "query_plan_node")
    graph.add_edge("query_plan_node", "sql_and_stats_node")
    graph.add_edge("sql_and_stats_node", "semantic_rank_node")
    graph.add_edge("semantic_rank_node", "color_filter_node")
    graph.add_edge("color_filter_node", "answer_synthesis_node")
    graph.add_edge("answer_synthesis_node", END)

    return graph.compile()


class SalesConversationalAgent(BaseAgent):
    """
    Conversational hybrid RAG agent for the LTDB sales page.
    Answers questions about daily/historical sales, sold articles (both ORPHAN via columns.yaml
    and RECONCILED via PIM/WMS catalog + embeddings + colors), and statistical comparisons.
    """

    def __init__(self, db: Session) -> None:
        super().__init__()
        self.db = db

    async def _aexecute(self, input_data: dict) -> dict:
        try:
            initial_state = SalesChatGraphState(
                raw_query=input_data.get("query", ""),
                context_date=input_data.get("context_date", ""),
                chat_history=input_data.get("chat_history", []),
                columns_schema=input_data.get("columns_schema", []),
                brands_catalog=input_data.get("brands_catalog", []),
                categories_catalog=input_data.get("categories_catalog", {}),
            )
        except Exception as exc:
            raise AgentException(
                message=f"Invalid input_data for SalesConversationalAgent: {exc}",
                output=None,
            ) from exc

        if not initial_state.raw_query or not initial_state.raw_query.strip():
            raise AgentException(
                message="SalesConversationalAgent requires a non-empty 'query' string.",
                output=None,
            )

        graph = _build_graph(self.db)

        try:
            final_state = await graph.ainvoke(initial_state)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error in SalesConversationalAgent: {exc}",
                output=None,
            ) from exc

        if isinstance(final_state, dict):
            reply = final_state.get("reply", "")
            query_plan = final_state.get("query_plan")
            stats_comparison = final_state.get("stats_comparison")
            filtered_reconciled = final_state.get("filtered_reconciled", [])
        else:
            reply = getattr(final_state, "reply", "")
            query_plan = getattr(final_state, "query_plan", None)
            stats_comparison = getattr(final_state, "stats_comparison", None)
            filtered_reconciled = getattr(final_state, "filtered_reconciled", [])

        plan_dict = query_plan.model_dump() if hasattr(query_plan, "model_dump") else query_plan

        return {
            "reply": reply,
            "query_plan": plan_dict,
            "stats_comparison": stats_comparison,
            "filtered_reconciled": filtered_reconciled,
        }
