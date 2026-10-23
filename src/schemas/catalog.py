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
# Search and catalog listings
# ---------------------------------------------------------------------------

class CatalogSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Search query string")
    in_stock_only: bool = False
    page: int = Field(1, ge=1, description="Page number (1-based)")
    page_size: int = Field(50, ge=1, le=100, description="Items per page")
    sort_by: str = Field("date", description="Sort field: 'date', 'category', 'brand', 'relevance'")
    sort_dir: str = Field("desc", description="Sort direction: 'asc', 'desc'")


class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Search query string")
    in_stock_only: bool = False
    page: int = Field(1, ge=1, description="Page number (1-based)")
    page_size: int = Field(50, ge=1, le=100, description="Items per page")
    sort_by: str = Field("date", description="Sort field: 'date', 'category', 'brand', 'relevance'")
    sort_dir: str = Field("desc", description="Sort direction: 'asc', 'desc'")


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

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

class CatalogItemsResponse(BaseModel):
    results: List[SearchResultItem]
    total_blueprints: int
    total_items: Optional[int] = None
    page: Optional[int] = None
    limit: Optional[int] = None
    total_pages: Optional[int] = None


class InterpretedPlanSummary(BaseModel):
    brand: Optional[str] = None
    category: Optional[str] = None
    color: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


class CatalogSearchResponse(BaseModel):
    message: str
    results: List[SearchResultItem]
    total_items: int = 0
    total_blueprints: int = 0
    page: int = 1
    limit: int = 50
    total_pages: int = 1
    interpreted_plan: Optional[InterpretedPlanSummary] = None
    truncated: bool = False


class SemanticSearchResponse(BaseModel):
    message: str
    results: List[SearchResultItem]
    total_items: Optional[int] = None
    total_blueprints: Optional[int] = None
    page: Optional[int] = None
    limit: Optional[int] = None
    total_pages: Optional[int] = None
    interpreted_plan: Optional[InterpretedPlanSummary] = None
    truncated: Optional[bool] = False


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
    new_color: Optional[str] = None
    colors: Optional[List[str]] = None
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

