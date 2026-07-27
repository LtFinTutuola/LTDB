from typing import List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func
from src.models.wms import Article, ArticleMovement, ArticlePrice
from src.schemas.wms import ArticleCreate, StockUpdate
from src.repositories.base import BaseRepository

class ArticleRepository(BaseRepository[Article, ArticleCreate, ArticleCreate]):
    def __init__(self):
        super().__init__(Article)

    def get_by_ean(self, db: Session, ean: str) -> List[Article]:
        return db.query(self.model).filter(self.model.ean == ean).all()

    def get_by_supplier_code(self, db: Session, supplier_code: str) -> List[Article]:
        return db.query(self.model).filter(self.model.supplier_code == supplier_code).all()

    def get_current_stock(self, db: Session, article_id: str) -> int:
        """
        Calculates the aggregated physical stock by reading the append-only ledger.
        """
        result = (
            db.query(func.sum(ArticleMovement.reason.property.mapper.class_.sign)) # Needs explicit join
            .join(ArticleMovement.reason)
            .filter(ArticleMovement.article_id == article_id)
            .scalar()
        )
        return result or 0

    def add_movement(self, db: Session, movement_in: StockUpdate, commit_changes: bool = True) -> ArticleMovement:
        """
        Adds a new movement to the append-only ledger.
        """
        db_movement = ArticleMovement(
            article_id=movement_in.article_id,
            reason_id=movement_in.reason_id,
            notes=movement_in.notes
        )
        db.add(db_movement)
        if commit_changes:
            db.commit()
            db.refresh(db_movement)
        else:
            db.flush()
        return db_movement

wms_repo = ArticleRepository()
