from typing import List, Dict, Any, Optional
from datetime import date
from pathlib import Path
import yaml
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.excel_sales import ExcelSale
from src.models.pim import ArticleBlueprint, Brand, Category
from src.schemas.excel_sales import ExcelSaleCreate, ExcelSaleStatus
from src.repositories.base import BaseRepository
from src.core.logger import get_logger

logger = get_logger()


def _load_columns_map() -> Dict[int, Dict[str, Any]]:
    cols_path = Path(__file__).parent.parent / "columns.yaml"
    with open(cols_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        int(col["index"]): {
            "index": int(col["index"]),
            "header": str(col["header"]),
            "description": str(col["description"]),
        }
        for col in data.get("columns", [])
    }


class ExcelSaleRepository(BaseRepository[ExcelSale, ExcelSaleCreate, ExcelSaleCreate]):
    def __init__(self):
        super().__init__(ExcelSale)

    def count_by_date(self, db: Session, target_date: date) -> int:
        """Counts total records for a specific date (orphans, reconciled, unprocessable)."""
        return db.query(func.count(self.model.id)).filter(self.model.date == target_date).scalar() or 0

    def bulk_create(self, db: Session, sales_in: List[ExcelSaleCreate], commit_changes: bool = True) -> List[ExcelSale]:
        """Bulk inserts ExcelSale records."""
        if not sales_in:
            return []
            
        db_sales = []
        for sale_in in sales_in:
            db_sale = self.model(**sale_in.model_dump())
            db.add(db_sale)
            db_sales.append(db_sale)
            
        if commit_changes:
            db.commit()
            for db_sale in db_sales:
                db.refresh(db_sale)
        else:
            db.flush()
            
        return db_sales

    def get_pending_records(self, db: Session, target_date: date = None) -> List[ExcelSale]:
        """Returns records that need attention (ORPHAN or UNPROCESSABLE) for a specific date."""
        query = db.query(self.model).filter(
            self.model.status.in_([ExcelSaleStatus.ORPHAN, ExcelSaleStatus.UNPROCESSABLE])
        )
        if target_date:
            query = query.filter(self.model.date == target_date)
        return query.order_by(self.model.excel_row_index.asc()).all()

    def get_all_by_date(self, db: Session, target_date: date) -> List[ExcelSale]:
        """Returns all records for a specific date, ordered by row index."""
        return db.query(self.model).filter(self.model.date == target_date).order_by(self.model.excel_row_index.asc()).all()

    def find_historical_pattern_date(
        self,
        db: Session,
        target_date: date,
        rows: List[Any],
        min_matches: int = 10
    ) -> Optional[date]:
        """
        Checks if incoming rows match >= min_matches identical records on a single
        historical date (ExcelSale.date != target_date).
        If detected, returns that historical date. Otherwise returns None.
        """
        if not rows or len(rows) < min_matches:
            return None

        from collections import Counter
        row_indices = [r.excel_row_index for r in rows]
        if not row_indices:
            return None

        historical_records = (
            db.query(self.model)
            .filter(
                self.model.date != target_date,
                self.model.excel_row_index.in_(row_indices)
            )
            .all()
        )
        if not historical_records:
            return None

        hist_by_date_and_row = {}
        for r in historical_records:
            hist_by_date_and_row[(r.date, r.excel_row_index)] = r

        matches_per_date = Counter()
        for incoming in rows:
            for (hist_date, row_idx), hist_record in hist_by_date_and_row.items():
                if row_idx != incoming.excel_row_index:
                    continue

                inc_start = round(float(incoming.starting_price), 2) if incoming.starting_price is not None else None
                hist_start = round(float(hist_record.starting_price), 2) if hist_record.starting_price is not None else None
                if inc_start != hist_start:
                    continue

                inc_sell = round(float(incoming.selling_price), 2) if incoming.selling_price is not None else None
                hist_sell = round(float(hist_record.selling_price), 2) if hist_record.selling_price is not None else None
                if inc_sell != hist_sell:
                    continue

                if incoming.excel_file_column != hist_record.excel_file_column:
                    continue

                if incoming.is_exchange != hist_record.is_exchange:
                    continue

                inc_code = (incoming.raw_article_code or "").strip() or None
                hist_code = (hist_record.raw_article_code or "").strip() or None
                if inc_code != hist_code:
                    continue

                matches_per_date[hist_date] += 1
                if matches_per_date[hist_date] >= min_matches:
                    return hist_date

        return None

    def get_sales_candidates_by_plan(
        self,
        db: Session,
        start_date: date,
        end_date: date,
        excel_column_indices: Optional[List[int]] = None,
        brand_name: Optional[str] = None,
        category_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retrieves ExcelSale rows for [start_date, end_date] (excluding UNPROCESSABLE),
        separating:
          - `reconciled_candidates`: RECONCILED sales matching all active SQL filters (column, brand, category).
          - `other_reconciled_in_columns`: RECONCILED sales in the target Excel column(s) that were excluded
            because their linked PIM article belongs to a different category/brand or is no longer in the catalog.
          - `orphan_summary`: ORPHAN sales in the target Excel column(s), summarized by column and by date.
        """
        columns_map = _load_columns_map()

        # Resolve optional Brand filter
        target_brand_ids: Optional[set[str]] = None
        if brand_name and brand_name.strip():
            clean_brand = brand_name.strip()
            exact_brand = db.query(Brand).filter(func.lower(Brand.name) == clean_brand.lower()).first()
            if exact_brand:
                target_brand_ids = {str(exact_brand.id)}
            else:
                matched_brands = db.query(Brand).filter(Brand.name.ilike(f"%{clean_brand}%")).all()
                target_brand_ids = {str(b.id) for b in matched_brands}

        # Resolve optional Category filter (with macro-category -> child sub-categories expansion)
        target_cat_ids: Optional[set[str]] = None
        if category_name and category_name.strip():
            clean_cat = category_name.strip()
            matched_cats = db.query(Category).filter(func.lower(Category.name) == clean_cat.lower()).all()
            if not matched_cats:
                matched_cats = db.query(Category).filter(Category.name.ilike(f"%{clean_cat}%")).all()
            target_cat_ids = set()
            for cat in matched_cats:
                target_cat_ids.add(str(cat.id))
                if cat.parent_id is None:
                    child_cats = db.query(Category).filter(Category.parent_id == cat.id).all()
                    for child in child_cats:
                        target_cat_ids.add(str(child.id))

        has_pim_filter = (target_brand_ids is not None) or (target_cat_ids is not None)

        query = db.query(ExcelSale).filter(
            ExcelSale.date >= start_date,
            ExcelSale.date <= end_date,
            ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
            ExcelSale.selling_price.isnot(None),
        )
        # If no PIM brand/category filter is active, we can filter directly by Excel column in SQL.
        # If a PIM filter IS active, we fetch all window rows so we can also find RECONCILED items
        # matching the requested PIM category/brand even if recorded in a different column, while still
        # restricting ORPHAN and column stats to excel_column_indices.
        if excel_column_indices and not has_pim_filter:
            query = query.filter(ExcelSale.excel_file_column.in_(excel_column_indices))

        sales_rows: List[ExcelSale] = query.order_by(ExcelSale.date.asc(), ExcelSale.excel_row_index.asc()).all()

        reconciled_candidates: List[Dict[str, Any]] = []
        other_reconciled_in_columns: List[Dict[str, Any]] = []
        orphan_by_col: Dict[int, Dict[str, Any]] = {}
        orphan_dates_by_col: Dict[int, Dict[str, Dict[str, Any]]] = {}
        total_orphan_sales = 0
        total_orphan_exchanges = 0
        total_orphan_revenue = 0.0
        total_reconciled_sales = 0
        total_sales_in_scope = 0

        for row in sales_rows:
            col_idx = row.excel_file_column if row.excel_file_column is not None else -1
            in_target_columns = (not excel_column_indices) or (col_idx in excel_column_indices)
            col_info = columns_map.get(col_idx, {"header": str(col_idx), "description": "non specificato"})
            sell_price = float(row.selling_price or 0.0)
            start_price = float(row.starting_price) if row.starting_price is not None else None
            date_iso = row.date.isoformat()

            if in_target_columns:
                total_sales_in_scope += 1

            if row.status == ExcelSaleStatus.RECONCILED:
                if in_target_columns:
                    total_reconciled_sales += 1

                art = row.article if row.article_id else None
                bp: Optional[ArticleBlueprint] = art.blueprint if art else None

                if not art or not bp:
                    if in_target_columns:
                        other_reconciled_in_columns.append(
                            {
                                "id": str(row.id),
                                "sale_id": str(row.id),
                                "date": date_iso,
                                "excel_file_column": col_idx,
                                "excel_column_header": col_info["header"],
                                "excel_column_description": col_info["description"],
                                "starting_price": start_price,
                                "selling_price": sell_price,
                                "is_exchange": bool(row.is_exchange),
                                "raw_article_code": row.raw_article_code,
                                "article_id": str(row.article_id) if row.article_id else None,
                                "article_name": None,
                                "brand_name": None,
                                "category_name": None,
                                "sub_category_name": None,
                                "mismatch_reason": (
                                    "Vendita marcata come RECONCILED, ma l'articolo associato "
                                    "non è più presente nell'anagrafica di magazzino."
                                ),
                            }
                        )
                    continue

                brand_obj = db.query(Brand).filter(Brand.id == bp.brand_id).first()
                cat_obj = db.query(Category).filter(Category.id == bp.category_id).first() if bp.category_id else None
                main_cat_name: Optional[str] = None
                sub_cat_name: Optional[str] = None
                if cat_obj:
                    if cat_obj.parent_id:
                        sub_cat_name = cat_obj.name
                        parent_cat = db.query(Category).filter(Category.id == cat_obj.parent_id).first()
                        main_cat_name = parent_cat.name if parent_cat else None
                    else:
                        main_cat_name = cat_obj.name

                brand_label = brand_obj.name if brand_obj else "Unknown"
                article_colors = sorted(
                    list({str(c).strip().lower() for c in (art.colors or []) if c and str(c).strip()})
                )

                brand_ok = (target_brand_ids is None) or (str(bp.brand_id) in target_brand_ids)
                cat_ok = (target_cat_ids is None) or (
                    bp.category_id is not None and str(bp.category_id) in target_cat_ids
                )

                # Determine whether this reconciled row matches the query filters:
                # - If PIM brand/category filters are active, it must pass brand_ok and cat_ok.
                #   (Also if excel_column_indices is specifically 'Sam/AT' [index 1] without brand_name,
                #    require in_target_columns so we don't pull non-Samsonite items from other columns).
                if has_pim_filter:
                    col_constraint_ok = in_target_columns or (
                        target_cat_ids is not None and excel_column_indices != [1]
                    )
                    is_match = brand_ok and cat_ok and col_constraint_ok
                else:
                    is_match = in_target_columns

                if is_match:
                    reconciled_candidates.append(
                        {
                            "id": str(row.id),
                            "sale_id": str(row.id),
                            "blueprint_id": str(bp.id),
                            "article_id": str(art.id),
                            "date": date_iso,
                            "excel_file_column": col_idx,
                            "excel_column_header": col_info["header"],
                            "excel_column_description": col_info["description"],
                            "starting_price": start_price,
                            "selling_price": sell_price,
                            "is_exchange": bool(row.is_exchange),
                            "raw_article_code": row.raw_article_code,
                            "article_name": bp.article_name,
                            "brand_name": brand_label,
                            "category_name": main_cat_name,
                            "sub_category_name": sub_cat_name,
                            "description": bp.description,
                            "extended_description": bp.extended_description,
                            "tags": list(bp.tags or []),
                            "materials": list(bp.materials or []),
                            "embedding": bp.embedding,
                            "available_colors": article_colors,
                        }
                    )
                elif in_target_columns:
                    reasons: List[str] = []
                    if not cat_ok and category_name:
                        actual_cat = (
                            f"{main_cat_name} ({sub_cat_name})"
                            if main_cat_name and sub_cat_name
                            else (main_cat_name or sub_cat_name or "Non categorizzato")
                        )
                        reasons.append(
                            f"categoria effettiva '{actual_cat}' diversa dalla categoria cercata '{category_name}'"
                        )
                    if not brand_ok and brand_name:
                        reasons.append(
                            f"brand effettivo '{brand_label}' diverso dal brand cercato '{brand_name}'"
                        )
                    reason_str = (
                        f"Registrata nella colonna '{col_info['header']}', ma " + " e ".join(reasons)
                        if reasons
                        else f"Registrata nella colonna '{col_info['header']}', ma non corrispondente ai filtri richiesti"
                    )
                    other_reconciled_in_columns.append(
                        {
                            "id": str(row.id),
                            "sale_id": str(row.id),
                            "blueprint_id": str(bp.id),
                            "article_id": str(art.id),
                            "date": date_iso,
                            "excel_file_column": col_idx,
                            "excel_column_header": col_info["header"],
                            "excel_column_description": col_info["description"],
                            "starting_price": start_price,
                            "selling_price": sell_price,
                            "is_exchange": bool(row.is_exchange),
                            "raw_article_code": row.raw_article_code,
                            "article_name": bp.article_name,
                            "brand_name": brand_label,
                            "category_name": main_cat_name,
                            "sub_category_name": sub_cat_name,
                            "available_colors": article_colors,
                            "mismatch_reason": reason_str,
                        }
                    )
            else:
                if not in_target_columns:
                    continue
                total_orphan_sales += 1
                if row.is_exchange:
                    total_orphan_exchanges += 1
                total_orphan_revenue += sell_price
                bucket = orphan_by_col.setdefault(
                    col_idx,
                    {
                        "col_index": col_idx,
                        "header": col_info["header"],
                        "description": col_info["description"],
                        "count": 0,
                        "exchange_count": 0,
                        "total_revenue": 0.0,
                        "raw_codes": [],
                        "sales_by_date": [],
                    },
                )
                bucket["count"] += 1
                if row.is_exchange:
                    bucket["exchange_count"] += 1
                bucket["total_revenue"] = round(bucket["total_revenue"] + sell_price, 2)
                if row.raw_article_code and len(bucket["raw_codes"]) < 10:
                    bucket["raw_codes"].append(row.raw_article_code)

                col_dates = orphan_dates_by_col.setdefault(col_idx, {})
                date_entry = col_dates.setdefault(
                    date_iso,
                    {"date": date_iso, "count": 0, "exchange_count": 0, "total_revenue": 0.0},
                )
                date_entry["count"] += 1
                if row.is_exchange:
                    date_entry["exchange_count"] += 1
                date_entry["total_revenue"] = round(date_entry["total_revenue"] + sell_price, 2)

        for c_idx, bucket in orphan_by_col.items():
            col_dates = orphan_dates_by_col.get(c_idx, {})
            bucket["sales_by_date"] = [col_dates[d] for d in sorted(col_dates.keys())]

        orphan_summary = {
            "total_orphan_sales": total_orphan_sales,
            "total_orphan_exchanges": total_orphan_exchanges,
            "total_orphan_revenue": round(total_orphan_revenue, 2),
            "by_column": [orphan_by_col[k] for k in sorted(orphan_by_col.keys())],
        }

        logger.log_execution(
            "excel_synchronization_repo",
            "sales_candidates_fetched",
            "ok",
            start_date=str(start_date),
            end_date=str(end_date),
            reconciled_matched=len(reconciled_candidates),
            other_reconciled_in_columns=len(other_reconciled_in_columns),
            total_reconciled=total_reconciled_sales,
            total_orphan=total_orphan_sales,
        )
        return {
            "reconciled_candidates": reconciled_candidates,
            "other_reconciled_in_columns": other_reconciled_in_columns,
            "orphan_summary": orphan_summary,
            "total_reconciled_in_scope": total_reconciled_sales,
            "total_sales_in_scope": total_sales_in_scope,
        }

    def compute_sales_comparison_stats(
        self,
        db: Session,
        start_date: date,
        end_date: date,
        excel_column_indices: Optional[List[int]] = None,
        baseline_start_date: Optional[date] = None,
        baseline_end_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """
        Deterministic statistical and comparative tool over ExcelSale.
        Computes target period metrics (count, exchanges, revenue, list price, discount, per-column breakdown)
        and compares them against:
          1. Historical general daily average (general_avg)
          2. Historical Day-of-Week daily average (dow_avg, when start_date == end_date)
          3. Optional custom baseline period [baseline_start_date, baseline_end_date] (custom_baseline)
        Works seamlessly on both ORPHAN and RECONCILED sales, globally or filtered by specific Excel columns.
        """
        columns_map = _load_columns_map()

        def _query_window_rows(d_from: date, d_to: date) -> List[ExcelSale]:
            q = db.query(ExcelSale).filter(
                ExcelSale.date >= d_from,
                ExcelSale.date <= d_to,
                ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
                ExcelSale.selling_price.isnot(None),
            )
            if excel_column_indices:
                q = q.filter(ExcelSale.excel_file_column.in_(excel_column_indices))
            return q.all()

        target_rows = _query_window_rows(start_date, end_date)
        calendar_days = max(1, (end_date - start_date).days + 1)

        total_sales = len(target_rows)
        exchange_count = sum(1 for r in target_rows if r.is_exchange)
        reconciled_count = sum(1 for r in target_rows if r.status == ExcelSaleStatus.RECONCILED)
        orphan_count = total_sales - reconciled_count
        total_revenue = round(sum(float(r.selling_price or 0.0) for r in target_rows), 2)
        total_list_price = round(sum(float(r.starting_price or 0.0) for r in target_rows), 2)
        total_discount = round(total_list_price - total_revenue, 2)
        avg_price = round(total_revenue / total_sales, 2) if total_sales > 0 else 0.0
        target_daily_sales = round(total_sales / calendar_days, 2)
        target_daily_revenue = round(total_revenue / calendar_days, 2)

        # Breakdown by column (0..7 or filtered subset)
        cols_to_report = sorted(excel_column_indices) if excel_column_indices else sorted(columns_map.keys())
        by_column: List[Dict[str, Any]] = []
        for c_idx in cols_to_report:
            c_info = columns_map.get(c_idx, {"header": str(c_idx), "description": ""})
            c_rows = [r for r in target_rows if r.excel_file_column == c_idx]
            c_sales = len(c_rows)
            c_exch = sum(1 for r in c_rows if r.is_exchange)
            c_rec = sum(1 for r in c_rows if r.status == ExcelSaleStatus.RECONCILED)
            c_rev = round(sum(float(r.selling_price or 0.0) for r in c_rows), 2)
            c_pl = round(sum(float(r.starting_price or 0.0) for r in c_rows), 2)
            by_column.append(
                {
                    "col_index": c_idx,
                    "header": c_info["header"],
                    "description": c_info["description"],
                    "total_sales": c_sales,
                    "exchange_count": c_exch,
                    "reconciled_count": c_rec,
                    "orphan_count": c_sales - c_rec,
                    "total_revenue": c_rev,
                    "total_list_price": c_pl,
                    "discount": round(c_pl - c_rev, 2),
                    "avg_price": round(c_rev / c_sales, 2) if c_sales > 0 else 0.0,
                }
            )

        # 1. Historical General Average (across all store active days in DB)
        general_days = (
            db.query(func.count(func.distinct(ExcelSale.date)))
            .filter(
                ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
                ExcelSale.selling_price.isnot(None),
            )
            .scalar()
            or 1
        )
        gen_q = db.query(
            func.count(ExcelSale.selling_price).label("cnt"),
            func.sum(ExcelSale.selling_price).label("rev"),
        ).filter(
            ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
            ExcelSale.selling_price.isnot(None),
        )
        if excel_column_indices:
            gen_q = gen_q.filter(ExcelSale.excel_file_column.in_(excel_column_indices))
        gen_row = gen_q.one()
        gen_avg_sales = round(float(gen_row.cnt or 0) / general_days, 2)
        gen_avg_rev = round(float(gen_row.rev or 0.0) / general_days, 2)

        def _build_diff(target_val: float, base_val: float) -> Dict[str, Any]:
            diff = round(target_val - base_val, 2)
            diff_pct = round((diff / base_val) * 100.0, 1) if base_val > 0 else None
            return {"diff": diff, "diff_pct": diff_pct}

        general_comparison = {
            "historical_active_days": general_days,
            "daily_avg_sales": gen_avg_sales,
            "daily_avg_revenue": gen_avg_rev,
            "sales_comparison": _build_diff(target_daily_sales, gen_avg_sales),
            "revenue_comparison": _build_diff(target_daily_revenue, gen_avg_rev),
        }

        # 2. Historical Day-of-Week Average (when querying a single date)
        dow_comparison: Optional[Dict[str, Any]] = None
        if start_date == end_date:
            target_dow = start_date.strftime("%w")
            dow_days = (
                db.query(func.count(func.distinct(ExcelSale.date)))
                .filter(
                    ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
                    ExcelSale.selling_price.isnot(None),
                    func.strftime("%w", ExcelSale.date) == target_dow,
                )
                .scalar()
                or 1
            )
            dow_q = db.query(
                func.count(ExcelSale.selling_price).label("cnt"),
                func.sum(ExcelSale.selling_price).label("rev"),
            ).filter(
                ExcelSale.status != ExcelSaleStatus.UNPROCESSABLE,
                ExcelSale.selling_price.isnot(None),
                func.strftime("%w", ExcelSale.date) == target_dow,
            )
            if excel_column_indices:
                dow_q = dow_q.filter(ExcelSale.excel_file_column.in_(excel_column_indices))
            dow_row = dow_q.one()
            dow_avg_sales = round(float(dow_row.cnt or 0) / dow_days, 2)
            dow_avg_rev = round(float(dow_row.rev or 0.0) / dow_days, 2)
            dow_comparison = {
                "dow_index": int(target_dow),
                "historical_dow_days": dow_days,
                "daily_avg_sales": dow_avg_sales,
                "daily_avg_revenue": dow_avg_rev,
                "sales_comparison": _build_diff( float(total_sales), dow_avg_sales),
                "revenue_comparison": _build_diff(total_revenue, dow_avg_rev),
            }

        # 3. Optional Custom Baseline Period Comparison
        custom_comparison: Optional[Dict[str, Any]] = None
        if baseline_start_date and baseline_end_date and baseline_start_date <= baseline_end_date:
            base_rows = _query_window_rows(baseline_start_date, baseline_end_date)
            base_active_days = len({r.date for r in base_rows}) or max(
                1, (baseline_end_date - baseline_start_date).days + 1
            )
            base_total_sales = len(base_rows)
            base_total_rev = round(sum(float(r.selling_price or 0.0) for r in base_rows), 2)
            base_daily_sales = round(base_total_sales / base_active_days, 2)
            base_daily_rev = round(base_total_rev / base_active_days, 2)
            custom_comparison = {
                "baseline_start_date": baseline_start_date.isoformat(),
                "baseline_end_date": baseline_end_date.isoformat(),
                "baseline_active_days": base_active_days,
                "total_sales": base_total_sales,
                "total_revenue": base_total_rev,
                "daily_avg_sales": base_daily_sales,
                "daily_avg_revenue": base_daily_rev,
                "daily_sales_comparison": _build_diff(target_daily_sales, base_daily_sales),
                "daily_revenue_comparison": _build_diff(target_daily_revenue, base_daily_rev),
            }

        return {
            "target_period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "calendar_days": calendar_days,
                "filtered_columns": [
                    columns_map[idx]["header"] for idx in excel_column_indices if idx in columns_map
                ]
                if excel_column_indices
                else "ALL",
                "total_sales": total_sales,
                "exchange_count": exchange_count,
                "reconciled_count": reconciled_count,
                "orphan_count": orphan_count,
                "total_revenue": total_revenue,
                "total_list_price": total_list_price,
                "total_discount": total_discount,
                "avg_price": avg_price,
                "daily_avg_sales": target_daily_sales,
                "daily_avg_revenue": target_daily_revenue,
                "by_column": by_column,
            },
            "general_historical_comparison": general_comparison,
            "dow_historical_comparison": dow_comparison,
            "custom_period_comparison": custom_comparison,
        }


excel_synchronization_repo = ExcelSaleRepository()

