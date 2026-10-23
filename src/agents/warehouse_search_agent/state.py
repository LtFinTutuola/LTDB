"""
src/agents/warehouse_search_agent/state.py
------------------------------------------
Shared state and structured LLM output schemas for WarehouseSearchAgent.
"""
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, model_validator


class SearchQueryPlan(BaseModel):
    """
    Structured output schema produced by query_plan_node.
    Extracts brand, category, single target color, and residual keyword tokens.
    """
    brand: Optional[str] = Field(
        default=None,
        description="Exact brand name from the database catalog if explicitly mentioned in the query, else null.",
    )
    category: Optional[str] = Field(
        default=None,
        description="Exact macro-category or sub-category name from the database taxonomy if mentioned, else null.",
    )
    color: Optional[str] = Field(
        default=None,
        description="Single base color name extracted from the search query (e.g. 'marrone', 'nero', 'blu'), else null.",
    )
    keywords: List[str] = Field(
        default_factory=list,
        description="Key model, line, or descriptive terms extracted for fast lexical matching (normalized to singular, lowercase, no accents).",
    )

    # Backward compatibility attributes & aliases
    requires_semantic_search: bool = Field(default=False)
    semantic_query: Optional[str] = Field(default=None)
    target_colors: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _remap_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "brand_name" in data and "brand" not in data:
                data["brand"] = data.get("brand_name")
            if "category_name" in data and "category" not in data:
                data["category"] = data.get("category_name")
            if "target_colors" in data and not data.get("color") and data.get("target_colors"):
                data["color"] = data["target_colors"][0]
            if "color" in data and data.get("color") and not data.get("target_colors"):
                data["target_colors"] = [data["color"]]
        return data

    @property
    def brand_name(self) -> Optional[str]:
        return self.brand

    @brand_name.setter
    def brand_name(self, value: Optional[str]) -> None:
        self.brand = value

    @property
    def category_name(self) -> Optional[str]:
        return self.category

    @category_name.setter
    def category_name(self, value: Optional[str]) -> None:
        self.category = value


class ColorIndexResponse(BaseModel):
    """
    Structured output schema for phase-2 color candidate filtering.
    Returns 0-based indices of matching colors from the candidate list.
    """
    indices: List[int] = Field(
        default_factory=list,
        description="0-based integer indices of colors from the provided candidate list that match the target color.",
    )


# Kept for backward compatibility with existing tests
class BlueprintColorSelection(BaseModel):
    blueprint_id: str = Field(..., description="The ID of the candidate ArticleBlueprint.")
    selected_colors: List[str] = Field(
        default_factory=list,
        description="List of matching color strings from the blueprint's available_colors.",
    )


class ColorFilterResponse(BaseModel):
    has_color_constraint: bool = Field(default=False)
    selections: List[BlueprintColorSelection] = Field(default_factory=list)


class WarehouseSearchGraphState(BaseModel):
    """Shared state for the WarehouseSearchAgent LangGraph pipeline."""

    # Input fields
    raw_query: str = Field(..., description="Original natural language search query from the user.")
    in_stock_only: bool = False
    brands_catalog: List[str] = Field(default_factory=list)
    categories_catalog: Dict[str, Any] = Field(default_factory=dict)

    # Internal pipeline state
    query_plan: Optional[SearchQueryPlan] = None
    retrieved_blueprints: List[Dict[str, Any]] = Field(default_factory=list)
    selected_blueprints: List[Dict[str, Any]] = Field(default_factory=list)
    color_degraded: bool = False

    # Backward compatibility fields
    sql_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    ranked_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    fallback_triggered: bool = False
