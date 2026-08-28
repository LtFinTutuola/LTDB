import re
from typing import Dict, Any
from src.agents.base import AgentException
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.core.logger import get_logger

logger = get_logger()

async def validation_node(state: ExtractionGraphState) -> Dict[str, Any]:
    """
    Validates that the extracted vendor codes match the brand's heuristic regex.
    If they fail, it feeds errors back to the extraction node for self-correction.
    """
    logger.log_agent("validation_node", "node_entry", "ok")
    extracted_items = state.extracted_items
    
    if not extracted_items:
        logger.log_agent("validation_node", "node_exit", "ok", result="no_items")
        return {"validation_passed": True}
        
    regex_pattern = state.brand_code_heuristic
    if not regex_pattern:
        logger.log_agent("validation_node", "node_exit", "ok", result="no_heuristic")
        return {"validation_passed": True}
        
    compiled_regex = re.compile(regex_pattern)
    
    # Check if the VendorCode in all items passes the regex
    failed_items = []
    for item in extracted_items:
        val = item.get("vendor_code") or item.get("VendorCode") or ""
        val = str(val).strip().upper()
        if not compiled_regex.search(val):
            failed_items.append(val)
            
    if not failed_items:
        logger.log_agent("validation_node", "node_exit", "ok", result="validation_passed")
        return {"validation_passed": True}
        
    # Validation failed for at least one item
    if state.extraction_retries >= 2:
        error_msg = f"Extraction failed regex validation after max retries. Expected pattern: '{regex_pattern}'. Failing codes: {failed_items[:3]}"
        logger.log_agent("validation_node", "max_retries_exceeded", "err", error=error_msg)
        raise AgentException(
            message=error_msg,
            output=None
        )
        
    # Trigger reflection loop
    error_message = (
        f"I codici VendorCode estratti (es. {failed_items[:3]}) NON corrispondono al pattern richiesto: `{regex_pattern}`. "
        "Hai probabilmente scambiato le colonne (es. hai usato il Materiale invece dello SKU)."
    )
    
    logger.log_agent("validation_node", "validation_failed", "warn", retries=state.extraction_retries + 1, error=error_message)
    
    return {
        "validation_passed": False,
        "extraction_retries": state.extraction_retries + 1,
        "extraction_errors": [error_message]
    }
