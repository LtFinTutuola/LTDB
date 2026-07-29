from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.embed_items_node import embed_items_node
from src.agents.article_blueprints_agent.nodes.db_match_node import db_match_node
from src.agents.article_blueprints_agent.nodes.cluster_unmatched_node import cluster_unmatched_node
from src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node import synthesize_blueprints_node
from src.agents.article_blueprints_agent.nodes.enrich_blueprints_node import enrich_blueprints_node
from src.agents.article_blueprints_agent.nodes.format_output_node import format_output_node


def _build_graph() -> StateGraph:
    """Construct and compile the ArticleBlueprintsAgent LangGraph state machine."""
    graph = StateGraph(BlueprintsGraphState)
    graph.add_node("embed_items_node", embed_items_node)
    graph.add_node("db_match_node", db_match_node)
    graph.add_node("cluster_unmatched_node", cluster_unmatched_node)
    graph.add_node("synthesize_blueprints_node", synthesize_blueprints_node)
    graph.add_node("enrich_blueprints_node", enrich_blueprints_node)
    graph.add_node("format_output_node", format_output_node)

    graph.add_edge(START, "embed_items_node")
    graph.add_edge("embed_items_node", "db_match_node")
    graph.add_edge("db_match_node", "cluster_unmatched_node")
    graph.add_edge("cluster_unmatched_node", "synthesize_blueprints_node")
    graph.add_edge("synthesize_blueprints_node", "enrich_blueprints_node")
    graph.add_edge("enrich_blueprints_node", "format_output_node")
    graph.add_edge("format_output_node", END)

    return graph.compile()


class ArticleBlueprintsAgent(BaseAgent):
    """
    Agent responsible for matching items against DB embeddings, clustering unmatched items,
    and synthesizing/enriching new blueprints.
    """
    def __init__(self):
        super().__init__()

    async def _aexecute(self, input_data: dict) -> dict:
        try:
            initial_state = BlueprintsGraphState(
                items=input_data.get("items", []),
                categories=input_data.get("categories", {}),
                db_embeddings_matrix=input_data.get("db_embeddings_matrix", []),
                db_similarity_threshold=input_data.get("db_similarity_threshold", 0.92),
                articles_similarity_threshold=input_data.get("articles_similarity_threshold", 0.88),
            )
        except Exception as exc:
            raise AgentException(
                message=f"Invalid input_data for ArticleBlueprintsAgent: {exc}",
                output=None,
            ) from exc

        graph = _build_graph()

        try:
            final_state = await graph.ainvoke(initial_state)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error in ArticleBlueprintsAgent: {exc}",
                output=None,
            ) from exc

        output_items = final_state.get("output_items", []) if isinstance(final_state, dict) else getattr(final_state, "output_items", [])
        output_blueprints = final_state.get("output_blueprints", []) if isinstance(final_state, dict) else getattr(final_state, "output_blueprints", [])
        warnings = final_state.get("warnings", []) if isinstance(final_state, dict) else getattr(final_state, "warnings", [])

        return {
            "items": output_items,
            "blueprints": output_blueprints,
            "warnings": warnings,
        }
