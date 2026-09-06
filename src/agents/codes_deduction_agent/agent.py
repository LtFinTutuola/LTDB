from langgraph.graph import StateGraph, START, END

from src.agents.base import BaseAgent, AgentException
from src.agents.codes_deduction_agent.state import CodesDeductionGraphState
from src.agents.codes_deduction_agent.nodes.pattern_analysis_node import pattern_analysis_node
from src.agents.codes_deduction_agent.nodes.regex_synthesis_node import regex_synthesis_node
from src.agents.codes_deduction_agent.nodes.validation_node import validation_node


def _should_retry(state: CodesDeductionGraphState) -> str:
    """Conditional edge: decide whether to retry or finish."""
    if state.success:
        return "end"
    if state.iteration >= state.max_iterations:
        return "end"
    return "retry"


def _build_graph() -> StateGraph:
    """Construct and compile the CodesDeductionAgent LangGraph state machine."""
    graph = StateGraph(CodesDeductionGraphState)
    graph.add_node("pattern_analysis_node", pattern_analysis_node)
    graph.add_node("regex_synthesis_node", regex_synthesis_node)
    graph.add_node("validation_node", validation_node)

    graph.add_edge(START, "pattern_analysis_node")
    graph.add_edge("pattern_analysis_node", "regex_synthesis_node")
    graph.add_edge("regex_synthesis_node", "validation_node")

    # Conditional routing from validation: retry or finish
    graph.add_conditional_edges(
        "validation_node",
        _should_retry,
        {
            "retry": "pattern_analysis_node",
            "end": END,
        }
    )

    return graph.compile()


class CodesDeductionAgent(BaseAgent):
    """
    Agent responsible for discovering brand-specific vendor code encoding patterns.
    Analyzes a corpus of vendor codes, synthesizes a regex with a model_code
    named capture group, and validates it with a retry loop (max 3 iterations).
    """
    def __init__(self):
        super().__init__()

    async def _aexecute(self, input_data: dict) -> dict:
        try:
            initial_state = CodesDeductionGraphState(
                vendor_codes=input_data.get("vendor_codes", []),
                brand_name=input_data.get("brand_name", ""),
                previous_explanation=input_data.get("previous_explanation"),
                max_iterations=input_data.get("max_iterations", 3),
            )
        except Exception as exc:
            raise AgentException(
                message=f"Invalid input_data for CodesDeductionAgent: {exc}",
                output=None,
            ) from exc

        if not initial_state.vendor_codes:
            raise AgentException(
                message="CodesDeductionAgent requires a non-empty 'vendor_codes' list.",
                output=None,
            )

        graph = _build_graph()

        try:
            final_state = await graph.ainvoke(initial_state)
        except AgentException:
            raise
        except Exception as exc:
            raise AgentException(
                message=f"Unexpected graph execution error in CodesDeductionAgent: {exc}",
                output=None,
            ) from exc

        # Extract output from final state
        if isinstance(final_state, dict):
            success = final_state.get("success", False)
            output_regex = final_state.get("output_regex", "")
            output_explanation = final_state.get("output_explanation", "")
            output_examples = final_state.get("output_examples", [])
            error_message = final_state.get("error_message", "")
        else:
            success = getattr(final_state, "success", False)
            output_regex = getattr(final_state, "output_regex", "")
            output_explanation = getattr(final_state, "output_explanation", "")
            output_examples = getattr(final_state, "output_examples", [])
            error_message = getattr(final_state, "error_message", "")

        if not success:
            raise AgentException(
                message=f"CodesDeductionAgent failed to deduce a valid heuristic: {error_message}",
                output={
                    "error": error_message,
                    "last_regex_attempt": output_regex,
                },
            )

        return {
            "regex": output_regex,
            "explanation": output_explanation,
            "examples": output_examples,
        }
