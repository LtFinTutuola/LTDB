from typing import List
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.excel_sales import ExcelSale
from src.schemas.excel_sales import ExcelSaleCreate, ExcelSaleStatus
from src.repositories.base import BaseRepository

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

excel_synchronization_repo = ExcelSaleRepository()
