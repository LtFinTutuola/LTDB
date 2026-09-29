from typing import List
from datetime import date
from sqlalchemy.orm import Session
from src.core.logger import get_logger

try:
    import openpyxl
except ImportError:
    openpyxl = None

from src.repositories.excel_sales_repo import excel_sales_repo
from src.schemas.excel_sales import ExcelSaleCreate, ExcelSaleStatus

logger = get_logger()

def ingest_daily_sales(db: Session, file_path: str, target_date: date) -> List[str]:
    """
    Ingests Excel sales data for the given date.
    Uses 'Stream-until-Empty' strategy starting from `db_count + 1` up to row 698.
    """
    if not openpyxl:
        logger.error("openpyxl is not installed.")
        raise ImportError("openpyxl is required to ingest excel files")

    # 1. Count existing records
    db_count = excel_sales_repo.count_by_date(db, target_date)
    min_row = db_count + 1
    
    # 2. Open workbook in read_only mode
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheet = wb.active
    except Exception as e:
        logger.error(f"Failed to open excel file {file_path}: {e}")
        raise e

    # 3. Read only new rows
    sales_to_insert = []
    
    # We read from min_row to 698 (since headers are at 699, data ends at 698 max)
    # We read up to column 12 (L)
    for row_idx, row in enumerate(sheet.iter_rows(min_row=min_row, max_row=698, min_col=1, max_col=12), start=min_row):
        # A row is empty if all cells in A to I are None
        if all(cell.value is None for cell in row[0:9]):
            # Reached empty rows, we are done
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

        sale_in = ExcelSaleCreate(
            date=target_date,
            excel_row_index=row_idx,
            excel_file_column=found_category_col,
            status=status,
            starting_price=starting_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
            selling_price=selling_price_val if status != ExcelSaleStatus.UNPROCESSABLE else None,
            is_exchange=is_exchange
        )
        sales_to_insert.append(sale_in)
        
    wb.close()
    
    # 5. Bulk insert
    if sales_to_insert:
        created_records = excel_sales_repo.bulk_create(db, sales_to_insert)
        logger.log_execution(
            "excel_ingestion_service", 
            "ingest_daily_sales", 
            "ok", 
            target_date=str(target_date),
            records_inserted=len(created_records),
            min_row_checked=min_row
        )
        return [str(r.id) for r in created_records]
    
    return []
