"""
src/schemas/catalog.py
-----------------------
Pydantic schemas for the Catalog router endpoints:
  - Brand listing (for dropdown population)
  - Category hierarchy listing  
  - Semantic search request / response
  - Catalog patch (direct ArticleBlueprint update)
  - SearchInterpretation (internal, used by search_service)
"""
from __future__ import annotations
from typing import List, Literal, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Brand & Category CRUD Schemas
# ---------------------------------------------------------------------------

class BrandListItem(BaseModel):
    id: str
    name: str


class BrandDetailItem(BaseModel):
    id: str
    name: str
    category_ids: List[str] = Field(default_factory=list)
    category_names: List[str] = Field(default_factory=list)
    blueprint_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BrandCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    category_ids: List[str] = Field(default_factory=list)


class BrandUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    category_ids: Optional[List[str]] = None


class CategoryDetailItem(BaseModel):
    id: str
    name: str
    description: str
    parent_id: Optional[str] = None
    parent_name: Optional[str] = None
    subcategories_count: int = 0
    brand_ids: List[str] = Field(default_factory=list)
    brand_names: List[str] = Field(default_factory=list)
    blueprint_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CategoryCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field("", min_length=0)
    parent_id: Optional[str] = None
    brand_ids: Optional[List[str]] = Field(default_factory=list)


class CategoryUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None
    parent_id: Optional[str] = None
    brand_ids: Optional[List[str]] = None



# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Natural language search query")
    in_stock_only: bool = False


from datetime import date

class SourceDocument(BaseModel):
    delivery_note_number: str
    supplier_name: str
    document_date: date

class SearchResultItem(BaseModel):
    blueprint_id: str
    article_name: str
    brand_name: str
    brand_id: Optional[str] = None
    category_name: Optional[str] = None
    sub_category_name: Optional[str] = None
    description: Optional[str] = None
    extended_description: Optional[str] = None
    vendor_code: Optional[str] = None
    barcode: Optional[str] = None
    photo_id: Optional[str] = None
    colors: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    materials: List[str] = Field(default_factory=list)
    stock: int = 0

class CatalogItemsResponse(BaseModel):
    results: List[SearchResultItem]
    total_blueprints: int


class SemanticSearchResponse(BaseModel):
    message: str
    results: List[SearchResultItem]


class SearchInterpretation(BaseModel):
    """Internal schema for LLM query interpretation. Used as response_schema for structured output."""
    mode: Literal["filter", "similarity"]
    brand_name: Optional[str] = None
    category_name: Optional[str] = None
    colors: Optional[List[str]] = None
    status: Optional[str] = None   # "Available" | "Sold" | "Lost"
    tags: Optional[List[str]] = None
    free_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Catalog patch (PATCH /catalog/{blueprint_id})
# ---------------------------------------------------------------------------

class CatalogUpdateRequest(BaseModel):
    article_name: Optional[str] = None
    description: Optional[str] = None
    extended_description: Optional[str] = None
    tags: Optional[List[str]] = None
    materials: Optional[List[str]] = None
    dimensions: Optional[str] = None
    vendor_code: Optional[str] = None
    barcode: Optional[str] = None
    color: Optional[str] = None
    quantity: Optional[int] = None
    category_name: Optional[str] = None
    sub_category_name: Optional[str] = None


class CatalogUpdateResponse(BaseModel):
    status: str
    blueprint_id: str
    updated_fields: List[str]


class CatalogDeleteResponse(BaseModel):
    blueprint_id: str
    deleted_blueprint: bool
    deleted_articles_count: int

