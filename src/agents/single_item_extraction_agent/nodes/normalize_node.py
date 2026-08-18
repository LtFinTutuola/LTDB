import uuid

from src.agents.base import AgentException
from src.agents.single_item_extraction_agent.state import SingleItemExtractionState
from src.core.logger import get_logger

logger = get_logger()

async def normalize_node(state: SingleItemExtractionState) -> dict:
    """
    Initializes the extracted_item dictionary with structural basics.
    Note: It purposefully DOES NOT inject description and colors, 
    as they are hints to be processed by the next node.
    """
    logger.log_agent("normalize_node", "node_entry", "ok", vendor_code=state.vendor_code)
    print(f"[normalize_node] Normalizing single item {state.vendor_code}...")

    item = {
        "item_id": str(uuid.uuid4()),
        "vendor_code": state.vendor_code,
        "barcode": state.barcode or "",
        "quantity": state.quantity,
    }

    print(f"[normalize_node] Initialized single item: {item['item_id']}.")
    
    result = {"extracted_item": item}
    logger.log_agent("normalize_node", "node_exit", "ok", output=result)
    
    return result
