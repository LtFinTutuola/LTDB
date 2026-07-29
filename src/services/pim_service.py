from sqlalchemy.orm import Session
from typing import Optional
from src.repositories.pim_repo import pim_repo
from src.schemas.pim import ArticleBlueprintCreate, BrandCreate
from src.repositories.base import BaseRepository
from src.models.pim import Brand, ArticleBlueprint
from src.core.logger import get_logger

logger = get_logger()

brand_repo = BaseRepository[Brand, BrandCreate, BrandCreate](Brand)

def get_or_create_product(
    db: Session, 
    brand_id: str,
    description: str, 
    article_name: str = "Unknown",
    extended_description: str = "",
    tags: Optional[list[str]] = None,
    materials: Optional[list[str]] = None,
    category_id: Optional[str] = None,
    commit_changes: bool = True
) -> str:
    """
    Creates a new product blueprint record.
    (Deduplication recognition system will be updated in a further development).
    """
    if tags is None:
        tags = []
            
    # Create new product
    blueprint_in = ArticleBlueprintCreate(
        brand_id=brand_id,
        category_id=category_id,
        article_name=article_name,
        description=description,
        extended_description=extended_description,
        tags=tags,
        materials=materials
    )
    new_product = pim_repo.create(db, obj_in=blueprint_in, commit_changes=commit_changes)
    logger.log_execution("pim_service", "blueprint_created", "ok", 
                         blueprint_id=str(new_product.id), 
                         payload=blueprint_in.model_dump())
    return str(new_product.id)
