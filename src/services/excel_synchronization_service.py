import threading
from typing import List, Dict, Any, Optional
from datetime import date
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.logger import get_logger
from src.core.database import SessionLocal
from src.repositories.excel_synchronization_repo import excel_synchronization_repo
from src.repositories.wms_repo import wms_repo
from src.schemas.excel_sales import ExcelSaleCreate, ExcelSaleStatus
from src.schemas.wms import StockUpdate
from src.models.wms import ArticleStatus, MovementReason

try:
    import openpyxl
except ImportError:
    openpyxl = None

logger = get_logger()

# ---------------------------------------------------------------------------
# Ingestion Logic
# ---------------------------------------------------------------------------

def ingest_daily_sales(db: Session, file_path: str, target_date: date) -> List[str]:
    """
    Ingests Excel sales data for the given date.
    Uses 'Stream-until-Empty' strategy starting from `db_count + 1` up to row 698.
    """
    if not openpyxl:
        logger.error("openpyxl is not installed.")
        raise ImportError("openpyxl is required to ingest excel files")

    # 1. Count existing records
    db_count = excel_synchronization_repo.count_by_date(db, target_date)
    min_row = db_count + 1
    
    # 2. Open workbook in read_only mode
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheet = wb.active
    except Exception as e:
        logger.log_execution("excel_synchronization_service", "ingest_open_error", "err", exc=e)
        raise e

    # 3. Determine max_col based on settings
    code_col_idx = None
    max_col_to_read = 12
    if settings.EXCEL_CODE_COLUMN:
        try:
            code_col_idx = ord(settings.EXCEL_CODE_COLUMN.upper()) - ord('A')
            max_col_to_read = max(12, code_col_idx + 1)
        except TypeError:
            logger.error(f"Invalid EXCEL_CODE_COLUMN: {settings.EXCEL_CODE_COLUMN}")

    # 4. Read only new rows
    sales_to_insert = []
    for row_idx, row in enumerate(sheet.iter_rows(min_row=min_row, max_row=698, min_col=1, max_col=max_col_to_read), start=min_row):
        # A row is empty if all cells in A to I are None
        if all(cell.value is None for cell in row[0:9]):
            break
            
        status = ExcelSaleStatus.ORPHAN
        starting_price_val = None
        selling_price_val = None
        found_category_col = None
        
        # Parse starting price (Column A, index 0)
        try:
            val_a = row[0].value
            if val_a is not None:
                starting_price_val = float(val_a)
        except (ValueError, TypeError):
            status = ExcelSaleStatus.UNPROCESSABLE
            
        # Parse selling price (Columns B-I, index 1-8)
        for col_idx in range(1, 9):
            cell = row[col_idx]
            if cell.value is not None:
                found_category_col = chr(ord('A') + col_idx) # Convert index to letter, e.g., 1 -> 'B'
                try:
                    selling_price_val = float(cell.value)
                except (ValueError, TypeError):
                    status = ExcelSaleStatus.UNPROCESSABLE
                break
                
        # Parse is_exchange (Column L, index 11)
        is_exchange = False
        val_l = row[11].value
        if val_l is not None and str(val_l).strip().upper() == "X":
            is_exchange = True

        # Parse raw_article_code
        raw_article_code = None
        if code_col_idx is not None and code_col_idx < len(row):
            val_code = row[code_col_idx].value
            if val_code is not None:
                raw_article_code = str(val_code).strip()

        sale_in = ExcelSaleCreate(
            date=target_date,
            excel_row_index=row_idx,
            excel_file_column=found_category_col,
            status=status,
            starting_price=starting_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
            selling_price=selling_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
            is_exchange=is_exchange,
            raw_article_code=raw_article_code
        )
        sales_to_insert.append(sale_in)
        
    wb.close()
    
    # 5. Bulk insert
    if sales_to_insert:
        created_records = excel_synchronization_repo.bulk_create(db, sales_to_insert)
        logger.log_execution(
            "excel_synchronization_service", 
            "ingest_daily_sales", 
            "ok", 
            target_date=str(target_date),
            records_inserted=len(created_records),
            min_row_checked=min_row
        )
        return [str(r.id) for r in created_records]
    
    return []


# ---------------------------------------------------------------------------
# Reconciliation Logic
# ---------------------------------------------------------------------------

from sqlalchemy import func
from src.models.pim import ArticlePhoto

def find_candidates(db: Session, search_code: str, is_exchange: bool) -> List[Dict[str, Any]]:
    """
    Finds reconciliation candidates for a given search code (vendor code or EAN) and transaction type.
    Groups results by color and returns the oldest (FIFO) article for each color variant.
    """
    articles = wms_repo.get_reconciliation_candidates(db, search_code, is_exchange)
    
    # Group by color string
    grouped = {}
    inventory_counts = {}
    
    for a in articles:
        colors_list = [c.lower() for c in (a.colors or [])]
        color_key = ", ".join(colors_list) if colors_list else "unknown"
        
        if color_key not in grouped:
            grouped[color_key] = a # FIFO relies on query order_by created_at asc
            inventory_counts[color_key] = 1
        else:
            inventory_counts[color_key] += 1
            
    results = []
    for color_key, article in grouped.items():
        colors_list = [c.lower() for c in (article.colors or [])]
        photo = None
        if colors_list:
            photo = db.query(ArticlePhoto).filter(
                ArticlePhoto.article_blueprint_id == article.article_blueprint_id,
                func.lower(ArticlePhoto.canonical_color_name).in_(colors_list)
            ).first()
        
        if not photo:
            photo = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == article.article_blueprint_id).first()
            
        photo_url = f"/api/v1/ingestion/photos/{photo.id}" if photo else None

        blueprint = article.blueprint
        results.append({
            "article_id": str(article.id),
            "article_name": blueprint.article_name if blueprint else None,
            "article_description": blueprint.description if blueprint else None,
            "colors": article.colors,
            "inventory": inventory_counts[color_key],
            "photo_serving_endpoint_url": photo_url
        })
    
    return results

def get_pending_sales(db: Session, target_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """
    Returns a list of orphan/unprocessable records for the specified date (defaults to today).
    """
    date_to_use = target_date or date.today()
    records = excel_synchronization_repo.get_pending_records(db, target_date=date_to_use)
    
    results = []
    for r in records:
        results.append({
            "id": str(r.id),
            "date": str(r.date),
            "excel_row_index": r.excel_row_index,
            "excel_file_column": r.excel_file_column,
            "status": r.status.value,
            "starting_price": float(r.starting_price) if r.starting_price is not None else None,
            "selling_price": float(r.selling_price) if r.selling_price is not None else None,
            "is_exchange": r.is_exchange,
            "raw_article_code": r.raw_article_code
        })
    return results


def _get_or_create_reason(db: Session, code: str, sign: int) -> str:
    reason = db.query(MovementReason).filter(MovementReason.code == code).first()
    if not reason:
        reason = MovementReason(code=code, sign=sign)
        db.add(reason)
        db.flush()
    return str(reason.id)

def reconcile_sale(db: Session, excel_sale_id: str, article_id: str) -> Dict[str, Any]:
    """
    Executes the dual-write atomic transaction to reconcile an ExcelSale with a physical WMS Article.
    """
    # 1. Fetch and validate the ExcelSale
    excel_sale = excel_synchronization_repo.get(db, excel_sale_id)
    if not excel_sale:
        raise ValueError("ExcelSale record not found.")
        
    if excel_sale.status == ExcelSaleStatus.RECONCILED:
        return {"status": "ok", "message": "Already reconciled."}
        
    if excel_sale.status == ExcelSaleStatus.UNPROCESSABLE:
        raise ValueError("Cannot reconcile an unprocessable record.")

    # 2. Fetch and validate the Article
    article = wms_repo.get(db, article_id)
    if not article:
        raise ValueError("Article not found.")
        
    # Check if the article's status is compatible with the transaction
    if not excel_sale.is_exchange and article.status != ArticleStatus.AVAILABLE:
        raise ValueError("Article is not AVAILABLE for sale.")
    if excel_sale.is_exchange and article.status != ArticleStatus.SOLD:
        raise ValueError("Article is not SOLD, cannot process exchange return.")

    # 3. Dual-Write Transaction
    try:
        if not excel_sale.is_exchange:
            # Sale
            article.status = ArticleStatus.SOLD
            reason_id = _get_or_create_reason(db, "EXCEL_SALE", -1)
        else:
            # Return
            article.status = ArticleStatus.AVAILABLE
            reason_id = _get_or_create_reason(db, "EXCEL_RETURN", 1)
            
        movement_in = StockUpdate(
            article_id=str(article.id),
            reason_id=reason_id,
            notes=f"Reconciled from ExcelSale {excel_sale.id}"
        )
        wms_repo.add_movement(db, movement_in, commit_changes=False)
        
        excel_sale.article_id = str(article.id)
        excel_sale.status = ExcelSaleStatus.RECONCILED
        
        # Commit the atomic transaction
        db.commit()
        
        logger.log_execution("excel_synchronization_service", "reconcile_sale", "ok",
                             excel_sale_id=excel_sale_id, article_id=article_id)
        return {"status": "ok", "message": "Successfully reconciled."}
        
    except Exception as e:
        logger.log_execution("excel_synchronization_service", "reconcile_sale", "err", exc=e)
        raise e

# ---------------------------------------------------------------------------
# Polling Daemon Logic
# ---------------------------------------------------------------------------

_polling_thread = None
_stop_event = threading.Event()

def _polling_loop(stop_event: threading.Event):
    logger.log_execution("excel_synchronization_service", "startup", "ok", message=f"Starting Excel polling loop. Interval: {settings.EXCEL_POLLING_INTERVAL}s")
    
    while not stop_event.is_set():
        if settings.EXCEL_FILE_PATH:
            db = SessionLocal()
            try:
                target_date = date.today()
                inserted_ids = ingest_daily_sales(db, settings.EXCEL_FILE_PATH, target_date)
                if inserted_ids:
                    logger.log_execution("excel_synchronization_service", "ingested", "ok", message=f"Polled Excel and ingested {len(inserted_ids)} new sales rows.")
            except Exception as e:
                logger.log_execution("excel_synchronization_service", "error", "error", exc=e)
            finally:
                db.close()
        
        stop_event.wait(timeout=settings.EXCEL_POLLING_INTERVAL)
        
    logger.log_execution("excel_synchronization_service", "shutdown", "ok", message="Excel polling loop stopped.")

def start_polling():
    global _polling_thread
    if _polling_thread is not None and _polling_thread.is_alive():
        logger.log_execution("excel_synchronization_service", "start_polling", "warning", message="Polling thread is already running.")
        return
        
    _stop_event.clear()
    _polling_thread = threading.Thread(target=_polling_loop, args=(_stop_event,), daemon=True)
    _polling_thread.start()

def stop_polling():
    global _polling_thread
    if _polling_thread is not None:
        _stop_event.set()
        _polling_thread.join(timeout=5.0)
        _polling_thread = None
