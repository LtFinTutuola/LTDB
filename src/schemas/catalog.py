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
# Brand
# ---------------------------------------------------------------------------

class BrandListItem(BaseModel):
    id: str
    name: str


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Natural language search query")


class SearchResultItem(BaseModel):
    blueprint_id: str
    article_name: str
    brand_name: str
    category_name: Optional[str] = None
    description: str
    photo_id: Optional[str] = None
    colors: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    materials: List[str] = Field(default_factory=list)
    stock: int = 0


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


class CatalogUpdateResponse(BaseModel):
    status: str
    blueprint_id: str
    updated_fields: List[str]
