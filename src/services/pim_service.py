from sqlalchemy.orm import Session
from src.repositories.pim_repo import pim_repo
from src.schemas.pim import ArticleBlueprintCreate, BrandCreate
from src.repositories.base import BaseRepository
from src.models.pim import Brand

brand_repo = BaseRepository[Brand, BrandCreate, BrandCreate](Brand)

def get_or_create_product(db: Session, supplier_code: str, description: str, commit_changes: bool = True) -> str:
    """
    Checks if an article blueprint exists by supplier code.
    If not, creates a new one with a dummy brand and returns the ID.
    """
    existing_product = pim_repo.get_by_supplier_code(db, supplier_code=supplier_code)
    if existing_product:
        return str(existing_product.id)
        
    dummy_brand_name = "DUMMY_BRAND"
    existing_brand = db.query(Brand).filter(Brand.name == dummy_brand_name).first()
    if existing_brand:
        brand_id = str(existing_brand.id)
    else:
        brand_in = BrandCreate(name=dummy_brand_name)
        new_brand = brand_repo.create(db, obj_in=brand_in, commit_changes=False)
        brand_id = str(new_brand.id)
    
    # Create new product
    blueprint_in = ArticleBlueprintCreate(
        brand_id=brand_id,
        category_id=None,
        supplier_code=supplier_code,
        ean=None,
        description=description,
        extended_description=description,
        tags=[],
        colors=[],
        materials=[]
    )
    new_product = pim_repo.create(db, obj_in=blueprint_in, commit_changes=commit_changes)
    return str(new_product.id)
