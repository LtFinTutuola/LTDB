from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.single_item_extraction_agent.state import SingleItemExtractionState
from src.agents.single_item_extraction_agent.nodes.normalize_node import normalize_node
from src.agents.single_item_extraction_agent.nodes.web_search_node import web_search_node

def _build_graph() -> StateGraph:
    """Construct and compile the SingleItemExtractionAgent LangGraph state machine."""
    graph = StateGraph(SingleItemExtractionState)
    graph.add_node("normalize_node", normalize_node)
    graph.add_node("web_search_node", web_search_node)

    graph.add_edge(START, "normalize_node")
    graph.add_edge("normalize_node", "web_search_node")
    graph.add_edge("web_search_node", END)

    return graph.compile()


class SingleItemExtractionAgent(BaseAgent):
    """
    Agent responsible for extracting and enriching a single item from user-provided hints.
    Overrides user hints with official data from the web.
    """
    def __init__(self):
        super().__init__()

    async def _aexecute(self, input_data: dict) -> dict:
        try:
            initial_state = SingleItemExtractionState(
                brand=input_data["brand"],
                vendor_code=input_data["vendor_code"],
                description=input_data["description"],
                colors=input_data["colors"],
                barcode=input_data.get("barcode"),
                quantity=input_data.get("quantity", 1)
            )
            graph = _build_graph()
        except (KeyError, Exception) as exc:
            raise AgentException(
                message=f"Invalid input_data for SingleItemExtractionAgent: {exc}",
                output=None,
            ) from exc

        try:
            final_state = await graph.ainvoke(initial_state)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error in SingleItemExtractionAgent: {exc}",
                output=None,
            ) from exc

        extracted_item = final_state.get("extracted_item") if isinstance(final_state, dict) else getattr(final_state, "extracted_item", None)
        warnings = final_state.get("warnings", []) if isinstance(final_state, dict) else getattr(final_state, "warnings", [])

        return {
            "items": [extracted_item] if extracted_item else [],
            "warnings": warnings,
        }
