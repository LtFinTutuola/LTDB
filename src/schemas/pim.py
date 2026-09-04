from typing import List, Optional
from pydantic import BaseModel, Field
from src.schemas.base import BaseSchemaWithAudit, BaseSchema

class BrandHeuristicCreate(BaseModel):
    pattern: str
    explanation: Optional[str] = None

class BrandHeuristicResponse(BaseSchemaWithAudit):
    brand_id: str
    pattern: str
    explanation: Optional[str]

class BrandCreate(BaseModel):
    name: str
    heuristics: Optional[List[BrandHeuristicCreate]] = None

class BrandResponse(BaseSchemaWithAudit):
    name: str
    heuristics: List[BrandHeuristicResponse] = Field(default_factory=list)

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
    normalized_vendor_code: Optional[str] = None
    article_name: str = "Unknown"
    description: str
    extended_description: str
    tags: List[str]
    materials: Optional[List[str]] = None
    dimensions: Optional[str] = None

class ArticleBlueprintResponse(BaseSchemaWithAudit):
    brand_id: str
    category_id: Optional[str]
    normalized_vendor_code: Optional[str]
    article_name: str
    description: str
    extended_description: str
    tags: List[str]
    materials: Optional[List[str]]
    dimensions: Optional[str]
