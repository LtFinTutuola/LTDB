from sqlalchemy.orm import Session
from typing import Optional
from src.repositories.pim_repo import pim_repo
from src.schemas.pim import ArticleBlueprintCreate, BrandCreate
from src.repositories.base import BaseRepository
from src.models.pim import Brand, Category, ArticleBlueprint
from src.core.logger import get_logger

logger = get_logger()

brand_repo = BaseRepository[Brand, BrandCreate, BrandCreate](Brand)


def build_embedding_text(
    brand_name: Optional[str] = None,
    category_name: Optional[str] = None,
    sub_category_name: Optional[str] = None,
    article_name: Optional[str] = None,
    description: Optional[str] = None,
    extended_description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    materials: Optional[list[str]] = None,
) -> str:
    """
    Build the canonical text representation of an ArticleBlueprint for embedding generation.
    Colors are intentionally excluded as they are handled at the Article variant level
    by the dedicated color_filter_node in WarehouseSearchAgent.
    """
    parts: list[str] = []
    if brand_name:
        parts.append(f"Brand: {brand_name}")
    if category_name:
        parts.append(f"Categoria: {category_name}")
    if sub_category_name:
        parts.append(f"Sottocategoria: {sub_category_name}")
    if article_name:
        parts.append(f"Articolo: {article_name}")
    if description:
        parts.append(f"Descrizione: {description}")
    if extended_description:
        parts.append(f"Dettagli: {extended_description}")
    if tags:
        clean_tags = [str(t).strip() for t in tags if str(t).strip()]
        if clean_tags:
            parts.append(f"Tags: {', '.join(clean_tags)}")
    if materials:
        clean_materials = [str(m).strip() for m in materials if str(m).strip()]
        if clean_materials:
            parts.append(f"Materiali: {', '.join(clean_materials)}")
    return " | ".join(parts).strip()


def build_blueprint_embedding_text(db: Session, bp: ArticleBlueprint) -> str:
    """
    Resolve Brand and Category hierarchy for an ArticleBlueprint ORM object
    and return its canonical embedding text.
    """
    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first() if bp.brand_id else None
    category = db.query(Category).filter(Category.id == bp.category_id).first() if bp.category_id else None

    main_cat_name: Optional[str] = None
    sub_cat_name: Optional[str] = None
    if category:
        if category.parent_id:
            sub_cat_name = category.name
            parent_cat = db.query(Category).filter(Category.id == category.parent_id).first()
            main_cat_name = parent_cat.name if parent_cat else None
        else:
            main_cat_name = category.name

    return build_embedding_text(
        brand_name=brand.name if brand else None,
        category_name=main_cat_name,
        sub_category_name=sub_cat_name,
        article_name=bp.article_name,
        description=bp.description,
        extended_description=bp.extended_description,
        tags=list(bp.tags or []),
        materials=list(bp.materials or []),
    )


def get_or_create_product(
    db: Session, 
    brand_id: str,
    description: str, 
    article_name: str = "Unknown",
    extended_description: str = "",
    tags: Optional[list[str]] = None,
    materials: Optional[list[str]] = None,
    dimensions: Optional[str] = None,
    category_id: Optional[str] = None,
    normalized_vendor_code: Optional[str] = None,
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
        normalized_vendor_code=normalized_vendor_code,
        article_name=article_name,
        description=description,
        extended_description=extended_description,
        tags=tags,
        materials=materials,
        dimensions=dimensions
    )
    new_product = pim_repo.create(db, obj_in=blueprint_in, commit_changes=commit_changes)
    logger.log_execution("pim_service", "blueprint_created", "ok", 
                         blueprint_id=str(new_product.id), 
                         payload=blueprint_in.model_dump())
    return str(new_product.id)
