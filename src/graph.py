from langgraph.graph import StateGraph, START, END
from .state import AgentState
from .nodes import (
    ingestion_node,
    orchestrator_triage_node,
    worker_extraction_node,
    schema_validation_node,
    logic_validation_node,
    human_in_loop_staging_node
)

def build_graph():
    # Initialize the graph
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("ingestion", ingestion_node)
    workflow.add_node("triage", orchestrator_triage_node)
    workflow.add_node("extraction", worker_extraction_node)
    workflow.add_node("schema_validation", schema_validation_node)
    workflow.add_node("logic_validation", logic_validation_node)
    workflow.add_node("human_staging", human_in_loop_staging_node)
    
    # Define conditional edges
    def route_after_schema(state: AgentState) -> str:
        if state.get("schema_valid"):
            return "logic_validation"
        # In a real app, you might want to retry extraction up to a limit
        return "human_staging"
        
    def route_after_logic(state: AgentState) -> str:
        # Both valid and invalid logic route to staging,
        # but the staging payload reflects the error for manual review.
        return "human_staging"

    # Add edges
    workflow.add_edge(START, "ingestion")
    workflow.add_edge("ingestion", "triage")
    workflow.add_edge("triage", "extraction")
    workflow.add_edge("extraction", "schema_validation")
    
    workflow.add_conditional_edges(
        "schema_validation",
        route_after_schema,
        {
            "logic_validation": "logic_validation",
            "human_staging": "human_staging"
        }
    )
    
    workflow.add_conditional_edges(
        "logic_validation",
        route_after_logic,
        {
            "human_staging": "human_staging"
        }
    )
    
    workflow.add_edge("human_staging", END)
    
    # Compile the graph
    app = workflow.compile()
    return app
