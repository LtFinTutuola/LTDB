from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.agents.data_extraction_agent.nodes.ingestion_node import ingestion_node
from src.agents.data_extraction_agent.nodes.cleanup_node import cleanup_node
from src.agents.data_extraction_agent.nodes.extraction_node import extraction_node
from src.agents.data_extraction_agent.nodes.web_search_node import web_search_node


def _build_graph() -> StateGraph:
    """Construct and compile the DataExtractionAgent LangGraph state machine."""
    graph = StateGraph(ExtractionGraphState)
    graph.add_node("ingestion_node", ingestion_node)
    graph.add_node("cleanup_node", cleanup_node)
    graph.add_node("extraction_node", extraction_node)
    graph.add_node("web_search_node", web_search_node)

    graph.add_edge(START, "ingestion_node")
    graph.add_edge("ingestion_node", "cleanup_node")
    graph.add_edge("cleanup_node", "extraction_node")
    graph.add_edge("extraction_node", "web_search_node")
    graph.add_edge("web_search_node", END)

    return graph.compile()


class DataExtractionAgent(BaseAgent):
    """
    Agent responsible for reading a DDT PDF, cleaning text, extracting product items,
    injecting item_id, and grounding names via web search.
    """
    def __init__(self):
        super().__init__()

    async def _aexecute(self, input_data: dict) -> dict:
        try:
            initial_state = ExtractionGraphState(
                file_path=input_data["file_path"],
                brand=input_data["brand"],
            )
        except (KeyError, Exception) as exc:
            raise AgentException(
                message=f"Invalid input_data for DataExtractionAgent: {exc}",
                output=None,
            ) from exc

        graph = _build_graph()

        try:
            final_state = await graph.ainvoke(initial_state)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error in DataExtractionAgent: {exc}",
                output=None,
            ) from exc

        items = final_state.get("extracted_items", []) if isinstance(final_state, dict) else getattr(final_state, "extracted_items", [])
        warnings = final_state.get("warnings", []) if isinstance(final_state, dict) else getattr(final_state, "warnings", [])

        return {
            "items": items,
            "warnings": warnings,
        }
