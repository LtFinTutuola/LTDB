"""
src/agents/warehouse_search_agent/nodes/sql_filter_node.py
----------------------------------------------------------
Compatibility alias for retrieve_node.
"""
from sqlalchemy.orm import Session
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState
from src.agents.warehouse_search_agent.nodes.retrieve_node import retrieve_node


async def sql_filter_node(state: WarehouseSearchGraphState, db: Session) -> dict:
    """Delegates to retrieve_node."""
    return await retrieve_node(state, db)
