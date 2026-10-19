"""
src/agents/sales_chat_agent/nodes/sql_and_stats_node.py
-------------------------------------------------------
Node 2: Executes deterministic queries over ExcelSale:
  1. Statistical and comparative aggregations (target period vs general_avg, dow_avg, and custom baseline)
  2. Candidate retrieval separating RECONCILED sales (linked to ArticleBlueprint) from ORPHAN sales.
"""
from datetime import datetime, date
from typing import List, Optional
from sqlalchemy.orm import Session

from src.agents.sales_chat_agent.state import SalesChatGraphState
from src.repositories.excel_synchronization_repo import excel_synchronization_repo
from src.services import excel_synchronization_service
from src.core.logger import get_logger

logger = get_logger()


def _parse_date_safe(date_str: Optional[str], fallback_str: str) -> date:
    for candidate in (date_str, fallback_str):
        if candidate:
            try:
                return datetime.strptime(candidate.strip(), "%Y-%m-%d").date()
            except ValueError:
                continue
    return date.today()


def _parse_optional_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


async def sql_and_stats_node(state: SalesChatGraphState, db: Session) -> dict:
    """
    Execute SQL filtering on ExcelSale and compute statistical comparisons.
    """
    plan = state.query_plan
    if not plan or plan.is_pure_chitchat:
        logger.log_agent("sql_and_stats_node", "node_skip_chitchat", "ok")
        return {
            "stats_comparison": None,
            "daily_pnl_summary": None,
            "reconciled_candidates": [],
            "other_reconciled_in_columns": [],
            "orphan_summary": {},
        }

    start_dt = _parse_date_safe(plan.start_date, state.context_date)
    end_dt = _parse_date_safe(plan.end_date, state.context_date)
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    base_start_dt = _parse_optional_date(plan.baseline_start_date)
    base_end_dt = _parse_optional_date(plan.baseline_end_date)

    # Map Excel column headers (e.g. ["CAR"], ["B"]) to integer indices (0..7)
    header_to_idx = {
        str(col["header"]).strip().upper(): int(col["index"])
        for col in state.columns_schema
    }
    col_indices: List[int] = []
    for h in (plan.excel_columns or []):
        h_clean = str(h).strip().upper()
        if h_clean in header_to_idx:
            col_indices.append(header_to_idx[h_clean])

    excel_col_filter = sorted(list(set(col_indices))) if col_indices else None

    logger.log_agent(
        "sql_and_stats_node",
        "node_entry",
        "ok",
        start_date=str(start_dt),
        end_date=str(end_dt),
        excel_col_filter=excel_col_filter,
        brand_name=plan.brand_name,
        category_name=plan.category_name,
    )

    # 1. Fetch RECONCILED candidates, other RECONCILED in columns, and ORPHAN summary
    candidates_data = excel_synchronization_repo.get_sales_candidates_by_plan(
        db=db,
        start_date=start_dt,
        end_date=end_dt,
        excel_column_indices=excel_col_filter,
        brand_name=plan.brand_name,
        category_name=plan.category_name,
    )

    # 2. Compute statistical comparison (always computed so the synthesis node has exact counts & averages)
    stats_comparison = excel_synchronization_repo.compute_sales_comparison_stats(
        db=db,
        start_date=start_dt,
        end_date=end_dt,
        excel_column_indices=excel_col_filter,
        baseline_start_date=base_start_dt,
        baseline_end_date=base_end_dt,
    )

    # 3. If single day and no specific column filter, also attach P&L summary from get_daily_aggregations
    daily_pnl_summary = None
    if start_dt == end_dt and not excel_col_filter:
        try:
            agg = excel_synchronization_service.get_daily_aggregations(db, start_dt)
            daily_pnl_summary = agg.get("pnl_summary")
        except Exception as exc:
            logger.log_agent("sql_and_stats_node", "pnl_summary_warning", "err", exc=exc)

    result = {
        "stats_comparison": stats_comparison,
        "daily_pnl_summary": daily_pnl_summary,
        "reconciled_candidates": candidates_data["reconciled_candidates"],
        "other_reconciled_in_columns": candidates_data.get("other_reconciled_in_columns", []),
        "orphan_summary": candidates_data["orphan_summary"],
    }

    logger.log_agent(
        "sql_and_stats_node",
        "node_exit",
        "ok",
        output={
            "reconciled_count": len(candidates_data["reconciled_candidates"]),
            "other_reconciled_in_columns_count": len(candidates_data.get("other_reconciled_in_columns", [])),
            "orphan_total": candidates_data["orphan_summary"].get("total_orphan_sales", 0),
            "target_total_sales": stats_comparison["target_period"]["total_sales"],
        },
    )
    return result
