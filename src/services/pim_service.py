from sqlalchemy.orm import Session
from typing import Optional
from src.repositories.pim_repo import pim_repo
from src.schemas.pim import ArticleBlueprintCreate, BrandCreate
from src.repositories.base import BaseRepository
from src.models.pim import Brand, ArticleBlueprint

brand_repo = BaseRepository[Brand, BrandCreate, BrandCreate](Brand)

def get_or_create_product(
    db: Session, 
    brand_id: str,
    supplier_code: str, 
    description: str, 
    extended_description: str = "",
    tags: Optional[list[str]] = None,
    colors: Optional[list[str]] = None,
    materials: Optional[list[str]] = None,
    category_id: Optional[str] = None,
    ean: Optional[str] = None,
    commit_changes: bool = True
) -> str:
    """
    Checks if an article blueprint exists by supplier code.
    If it exists, verifies that non-free-form fields (brand_id, category_id, colors, materials) match.
    If they match, returns the existing product ID.
    If they do not match, or if it doesn't exist, creates a new product record.
    """
    if tags is None:
        tags = []
    
    existing_products = db.query(ArticleBlueprint).filter(ArticleBlueprint.supplier_code == supplier_code).all()
    
    # get_by_supplier_code might just return a single item. If supplier_code is not unique,
    # we should check all matches if possible. Since we only had get_by_supplier_code, we'll
    # query directly to get all matches for this supplier code.
    existing_products = db.query(ArticleBlueprint).filter(ArticleBlueprint.supplier_code == supplier_code).all()
    
    for existing_product in existing_products:
        # Check if non-free-form fields match
        # Handle None vs [] for colors/materials
        ext_colors = existing_product.colors or []
        ext_materials = existing_product.materials or []
        new_colors = colors or []
        new_materials = materials or []
        
        if (existing_product.brand_id == brand_id and
            existing_product.category_id == category_id and
            set(ext_colors) == set(new_colors) and
            set(ext_materials) == set(new_materials)):
            return str(existing_product.id)
            
    # Create new product
    blueprint_in = ArticleBlueprintCreate(
        brand_id=brand_id,
        category_id=category_id,
        supplier_code=supplier_code,
        ean=ean,
        description=description,
        extended_description=extended_description,
        tags=tags,
        colors=colors,
        materials=materials
    )
    new_product = pim_repo.create(db, obj_in=blueprint_in, commit_changes=commit_changes)
    return str(new_product.id)
