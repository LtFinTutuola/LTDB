from typing import List, Optional
from pydantic import BaseModel
from src.schemas.base import BaseSchemaWithAudit, BaseSchema

class BrandCreate(BaseModel):
    name: str

class BrandResponse(BaseSchemaWithAudit):
    name: str

class CategoryCreate(BaseModel):
    parent_id: Optional[str] = None
    name: str
    description: str

class CategoryResponse(BaseSchemaWithAudit):
    parent_id: Optional[str]
    name: str
    description: str

class ArticleBlueprintCreate(BaseModel):
    brand_id: str
    category_id: Optional[str] = None
    supplier_code: str
    ean: Optional[str] = None
    description: str
    extended_description: str
    tags: List[str]
    colors: Optional[List[str]] = None
    materials: Optional[List[str]] = None

class ArticleBlueprintResponse(BaseSchemaWithAudit):
    brand_id: str
    category_id: Optional[str]
    supplier_code: str
    ean: Optional[str]
    description: str
    extended_description: str
    tags: List[str]
    colors: Optional[List[str]]
    materials: Optional[List[str]]
