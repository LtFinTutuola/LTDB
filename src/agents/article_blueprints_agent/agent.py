from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node import synthesize_blueprints_node
from src.agents.article_blueprints_agent.nodes.enrich_blueprints_node import enrich_blueprints_node
from src.agents.article_blueprints_agent.nodes.format_output_node import format_output_node
from src.agents.article_blueprints_agent.nodes.web_search_blueprints_node import web_search_blueprints_node
from src.agents.article_blueprints_agent.nodes.color_photo_search_node import color_photo_search_node


def _route_after_start(state: BlueprintsGraphState) -> str:
    if state.new_blueprints:
        return "web_search_blueprints_node"
    return "color_photo_search_node"


def _build_graph() -> StateGraph:
    """Construct and compile the ArticleBlueprintsAgent LangGraph state machine."""
    graph = StateGraph(BlueprintsGraphState)
    graph.add_node("web_search_blueprints_node", web_search_blueprints_node)
    graph.add_node("synthesize_blueprints_node", synthesize_blueprints_node)
    graph.add_node("enrich_blueprints_node", enrich_blueprints_node)
    graph.add_node("color_photo_search_node", color_photo_search_node)
    graph.add_node("format_output_node", format_output_node)

    graph.add_conditional_edges(START, _route_after_start, {
        "web_search_blueprints_node": "web_search_blueprints_node",
        "color_photo_search_node": "color_photo_search_node",
    })
    
    graph.add_edge("web_search_blueprints_node", "synthesize_blueprints_node")
    graph.add_edge("synthesize_blueprints_node", "enrich_blueprints_node")
    graph.add_edge("enrich_blueprints_node", "color_photo_search_node")
    
    graph.add_edge("color_photo_search_node", "format_output_node")
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
                new_blueprints=input_data.get("new_blueprints", []),
                photo_only_items=input_data.get("photo_only_items", []),
                categories=input_data.get("categories", {}),
                brand_name=input_data.get("brand_name", ""),
                resolved_blueprints=input_data.get("resolved_blueprints", {}),
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
        photo_proposals = final_state.get("photo_proposals", []) if isinstance(final_state, dict) else getattr(final_state, "photo_proposals", [])
        warnings = final_state.get("warnings", []) if isinstance(final_state, dict) else getattr(final_state, "warnings", [])

        return {
            "items": output_items,
            "blueprints": output_blueprints,
            "photo_proposals": photo_proposals,
            "warnings": warnings,
        }
