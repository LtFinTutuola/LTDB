"""
src/agents/warehouse_search_agent/nodes/sql_filter_node.py
----------------------------------------------------------
Node 2: Converts the structured SearchQueryPlan into a deterministic SQLAlchemy query
via pim_repo and retrieves candidate blueprints.
"""
from sqlalchemy.orm import Session
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState
from src.repositories.pim_repo import pim_repo
from src.core.logger import get_logger

logger = get_logger()


async def sql_filter_node(state: WarehouseSearchGraphState, db: Session) -> dict:
    """
    Execute the relational filter query defined in state.query_plan using pim_repo.
    """
    plan = state.query_plan
    brand_name = plan.brand_name if plan else None
    category_name = plan.category_name if plan else None

    logger.log_agent(
        "sql_filter_node",
        "node_entry",
        "ok",
        brand_name=brand_name,
        category_name=category_name,
        in_stock_only=state.in_stock_only,
    )

    candidates = pim_repo.get_candidate_blueprints_by_sql_plan(
        db=db,
        brand_name=brand_name,
        category_name=category_name,
        in_stock_only=state.in_stock_only,
    )

    result = {"sql_candidates": candidates}
    logger.log_agent(
        "sql_filter_node",
        "node_exit",
        "ok",
        output={"candidate_count": len(candidates), "candidate_ids": [c["id"] for c in candidates]},
    )
    return result
