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
    article_name: str = "Unknown"
    description: str
    extended_description: str
    tags: List[str]
    materials: Optional[List[str]] = None
    dimensions: Optional[str] = None

class ArticleBlueprintResponse(BaseSchemaWithAudit):
    brand_id: str
    category_id: Optional[str]
    article_name: str
    description: str
    extended_description: str
    tags: List[str]
    materials: Optional[List[str]]
    dimensions: Optional[str]
