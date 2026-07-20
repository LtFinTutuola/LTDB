from sqlalchemy.orm import Session
from src.models.sales import Sale, SaleLine
from src.schemas.sales import SaleCreate
from src.repositories.base import BaseRepository

class SaleRepository(BaseRepository[Sale, SaleCreate, SaleCreate]):
    def __init__(self):
        super().__init__(Sale)

    def create_transaction(self, db: Session, sale_in: SaleCreate) -> Sale:
        """
        Persists a transactional sale using a session rollback block.
        """
        try:
            # Create master sale record
            db_sale = Sale(
                seller_id=sale_in.seller_id,
                total_paid=sale_in.total_paid,
                payment_method=sale_in.payment_method
            )
            db.add(db_sale)
            db.flush() # get id

            # Create sale lines
            for line_in in sale_in.lines:
                db_line = SaleLine(
                    sale_id=db_sale.id,
                    article_id=line_in.article_id,
                    quantity=line_in.quantity,
                    unit_price=line_in.unit_price
                )
                db.add(db_line)
            
            db.commit()
            db.refresh(db_sale)
            return db_sale
        except Exception as e:
            db.rollback()
            raise e

sales_repo = SaleRepository()
