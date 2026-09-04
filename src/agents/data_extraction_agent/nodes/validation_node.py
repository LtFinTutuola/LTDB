import re
from typing import Dict, Any
from src.agents.base import AgentException
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.core.logger import get_logger

logger = get_logger()

async def validation_node(state: ExtractionGraphState) -> Dict[str, Any]:
    """
    Validation is now handled in the service layer using multiple heuristics.
    Always pass validation here to proceed to completion.
    """
    logger.log_agent("validation_node", "node_entry", "ok")
    logger.log_agent("validation_node", "node_exit", "ok", result="validation_passed")
    return {"validation_passed": True}
