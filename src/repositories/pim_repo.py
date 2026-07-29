from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload
from src.models.pim import ArticleBlueprint, Brand, Category
from src.schemas.pim import ArticleBlueprintCreate, ArticleBlueprintResponse  # placeholder for update schema
from src.repositories.base import BaseRepository
from src.core.logger import get_logger

logger = get_logger()

class ArticleBlueprintRepository(BaseRepository[ArticleBlueprint, ArticleBlueprintCreate, ArticleBlueprintCreate]):
    def __init__(self):
        super().__init__(ArticleBlueprint)

    def get_embeddings_by_brand(self, db: Session, brand_id: str) -> list[dict]:
        """
        Retrieve lightweight dictionary list of all blueprints for a brand where embedding is not null.
        """
        blueprints = (
            db.query(ArticleBlueprint.id, ArticleBlueprint.embedding, ArticleBlueprint.category_id)
            .filter(ArticleBlueprint.brand_id == brand_id)
            .filter(ArticleBlueprint.embedding.isnot(None))
            .all()
        )
        result = [
            {"id": str(bp.id), "embedding": bp.embedding, "category_id": str(bp.category_id) if bp.category_id else None}
            for bp in blueprints
        ]
        logger.log_execution("pim_repo", "db_embeddings_fetched", "ok", brand_id=brand_id, count=len(result))
        return result

    def save_embedding(self, db: Session, blueprint_id: str, embedding: list[float], commit_changes: bool = True) -> None:
        """
        Update embedding vector for an existing article blueprint.
        """
        logger.log_execution("pim_repo", "embedding_saved", "ok", blueprint_id=blueprint_id, vector_dim=len(embedding) if embedding else 0)
        bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == blueprint_id).first()
        if bp:
            bp.embedding = embedding
            if commit_changes:
                db.commit()
            else:
                db.flush()



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

        total_subs = sum(len(h["sub_categories"]) for h in hierarchy.values())
        logger.log_execution("category_repo", "category_hierarchy_fetched", "ok", brand_id=brand_id, macro_categories=len(hierarchy), total_sub_categories=total_subs, hierarchy=hierarchy)
        return hierarchy


pim_repo = ArticleBlueprintRepository()
category_repo = CategoryRepository()
