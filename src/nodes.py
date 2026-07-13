import yaml
import os
import json
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import ValidationError
from .state import AgentState, ExtractedDataModel
from .prompts import ORCHESTRATOR_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT

def ingestion_node(state: AgentState) -> AgentState:
    print("--- INGESTION NODE ---")
    file_path = state.get("file_path")
    file_format = state.get("file_format")
    
    if not file_path or not os.path.exists(file_path):
        state["validation_errors"] = [f"Input file not found: {file_path}"]
        state["schema_valid"] = False
        state["logic_valid"] = False
        return state

    # For simplicity, we just pass the file_path forward.
    # In a real setup, we would upload to Gemini File API or parse Excel here.
    state["document_content"] = file_path
    return state

def orchestrator_triage_node(state: AgentState) -> AgentState:
    print("--- ORCHESTRATOR TRIAGE NODE ---")
    if state.get("validation_errors"): return state
    
    # Initialize the LLM
    api_key = state.get("api_key")
    endpoint_url = state.get("endpoint_url")
    client_options = {"api_endpoint": endpoint_url} if endpoint_url else None
    
    llm = ChatGoogleGenerativeAI(
        model="gemini-1.5-pro", 
        temperature=0,
        api_key=api_key,
        client_options=client_options
    )
    
    # Here we would normally attach the file via the Gemini File API.
    # Since we don't have the file logic hooked up here, we'll mock the classification 
    # or pass a placeholder if we're just setting up the structure.
    
    # Mocked classification for demonstration
    state["classification"] = "Invoice"
    return state

def worker_extraction_node(state: AgentState) -> AgentState:
    print("--- WORKER EXTRACTION NODE ---")
    if state.get("validation_errors"): return state
    
    # Initialize the LLM with structured output
    api_key = state.get("api_key")
    endpoint_url = state.get("endpoint_url")
    client_options = {"api_endpoint": endpoint_url} if endpoint_url else None
    
    llm = ChatGoogleGenerativeAI(
        model="gemini-1.5-pro", 
        temperature=0,
        api_key=api_key,
        client_options=client_options
    )
    structured_llm = llm.with_structured_output(ExtractedDataModel)
    
    # Mocking extraction output since we can't run Gemini without credentials and files
    # In real execution, we'd invoke the structured_llm with the WORKER_SYSTEM_PROMPT
    # and the document.
    mock_response = {
        "document_type": state["classification"],
        "total_items_reported": 10,
        "line_items": [
            {
                "VendorCode": "SKU-123",
                "Description": "TRUNK BLACK M",
                "Quantity": 10,
                "UnitPrice": 15.50,
                "ImplicitCategory": "Underwear"
            }
        ]
    }
    
    state["extracted_data"] = mock_response
    return state

def schema_validation_node(state: AgentState) -> AgentState:
    print("--- SCHEMA VALIDATION NODE ---")
    if state.get("validation_errors"): return state
    
    extracted_data = state.get("extracted_data")
    try:
        # Validate against Pydantic schema
        ExtractedDataModel(**extracted_data)
        state["schema_valid"] = True
    except ValidationError as e:
        state["schema_valid"] = False
        state["validation_errors"] = [str(e)]
        
    return state

def logic_validation_node(state: AgentState) -> AgentState:
    print("--- LOGIC VALIDATION NODE ---")
    if not state.get("schema_valid") or state.get("validation_errors"): return state
    
    extracted_data = state.get("extracted_data")
    total_reported = extracted_data.get("total_items_reported")
    
    calculated_total = sum(item["Quantity"] for item in extracted_data.get("line_items", []))
    
    if total_reported is not None and calculated_total != total_reported:
        state["logic_valid"] = False
        state["validation_errors"] = [f"Logic mismatch: Reported total {total_reported}, calculated sum {calculated_total}"]
    else:
        state["logic_valid"] = True
        
    return state

def human_in_loop_staging_node(state: AgentState) -> AgentState:
    print("--- HUMAN IN LOOP STAGING NODE ---")
    
    # Prepare the final output payload
    final_output = {
        "status": "pending_reconciliation" if not state.get("logic_valid") else "ready",
        "data": state.get("extracted_data"),
        "errors": state.get("validation_errors", [])
    }
    
    state["final_output"] = final_output
    return state
