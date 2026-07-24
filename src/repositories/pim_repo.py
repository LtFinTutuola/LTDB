from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload
from src.models.pim import ArticleBlueprint, Brand, Category
from src.schemas.pim import ArticleBlueprintCreate, ArticleBlueprintResponse  # placeholder for update schema
from src.repositories.base import BaseRepository


class ArticleBlueprintRepository(BaseRepository[ArticleBlueprint, ArticleBlueprintCreate, ArticleBlueprintCreate]):
    def __init__(self):
        super().__init__(ArticleBlueprint)

    def get_by_ean(self, db: Session, ean: str) -> Optional[ArticleBlueprint]:
        return db.query(self.model).filter(self.model.ean == ean).first()

    def get_by_supplier_code(self, db: Session, supplier_code: str) -> Optional[ArticleBlueprint]:
        return db.query(self.model).filter(self.model.supplier_code == supplier_code).first()


class CategoryRepository:
    """
    Repository for querying the brand-category hierarchy.

    Returns the structured dict expected by DataIngestionAgent:
        {
            "<MacroCategoryName>": {
                "description": "...",
                "sub_categories": {"<SubCategoryName>": "...", ...}
            },
            ...
        }
    """

    def get_brand_hierarchy(self, db: Session, brand_id: str) -> dict:
        """
        Build the complete category hierarchy for a given brand.

        Args:
            db:         SQLAlchemy session.
            brand_id:   The brand ID string from the extraction request.

        Returns:
            Nested dict mapping macro-category names → CategoryNode-compatible dicts.

        Raises:
            HTTPException(404): If the brand is not found in the database.
        """
        brand: Optional[Brand] = (
            db.query(Brand)
            .options(
                joinedload(Brand.categories).joinedload(Category.subcategories)
            )
            .filter(Brand.id == brand_id)
            .first()
        )
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Brand ID '{brand_id}' not found in the database. "
                       "Please ensure the brand hierarchy has been seeded."
            )

        hierarchy: dict = {}
        # brand.categories contains the macro-level categories linked via BrandCategory
        for macro_cat in brand.categories:
            # Only process top-level categories (parent_id is None)
            if macro_cat.parent_id is not None:
                continue

            # Query sub-categories directly to avoid relying on the lazy-loaded
            # adjacency-list relationship (which has a known remote_side config issue)
            subs = (
                db.query(Category)
                .filter(Category.parent_id == macro_cat.id)
                .all()
            )
            sub_categories: dict[str, str] = {
                sub.name: sub.description for sub in subs
            }

            hierarchy[macro_cat.name] = {
                "description": macro_cat.description,
                "sub_categories": sub_categories,
            }

        return hierarchy


pim_repo = ArticleBlueprintRepository()
category_repo = CategoryRepository()
