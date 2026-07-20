from typing import Optional
from sqlalchemy.orm import Session
from src.models.pim import ArticleBlueprint
from src.schemas.pim import ArticleBlueprintCreate, ArticleBlueprintResponse # placeholder for update schema
from src.repositories.base import BaseRepository

class ArticleBlueprintRepository(BaseRepository[ArticleBlueprint, ArticleBlueprintCreate, ArticleBlueprintCreate]):
    def __init__(self):
        super().__init__(ArticleBlueprint)

    def get_by_ean(self, db: Session, ean: str) -> Optional[ArticleBlueprint]:
        return db.query(self.model).filter(self.model.ean == ean).first()

    def get_by_supplier_code(self, db: Session, supplier_code: str) -> Optional[ArticleBlueprint]:
        return db.query(self.model).filter(self.model.supplier_code == supplier_code).first()

pim_repo = ArticleBlueprintRepository()
