"""
src/agents/data_ingestion_agent/state.py
-----------------------------------------
Pydantic state models for the DataIngestionAgent LangGraph graph.

Two top-level state classes:
  - GraphState: the main pipeline state, carried from START to END.
  - ItemState:  the per-item enrichment state, one instance per Send() call.

A helper model CategoryNode encapsulates the brand hierarchy structure
expected by the agent so the categories field is fully typed.

LangGraph natively supports Pydantic v2 BaseModel as state via
StateGraph(state_schema=GraphState). The Annotated[List[dict], operator.add]
on `enriched_items` tells LangGraph to accumulate results from all parallel
item branches (Map-Reduce fan-in) instead of replacing the list.
"""
import operator
from typing import Annotated, List, Optional

from pydantic import BaseModel, Field


class CategoryNode(BaseModel):
    """Represents a single macro-category with its description and sub-categories."""
    description: str
    sub_categories: dict[str, str] = Field(
        default_factory=dict,
        description="Maps sub-category name to its description string."
    )


class GraphState(BaseModel):
    """
    Shared state for the entire DataIngestionAgent pipeline.

    Input fields are populated once at graph invocation by DataIngestionAgent.aexecute.
    Pipeline fields are progressively filled by each node.
    """

    # --- Input fields (injected by the service via aexecute) ---
    file_path: str = Field(..., description="Absolute path to the PDF file to process.")
    brand: str = Field(..., description="Brand name for the shipment document.")
    categories: dict[str, CategoryNode] = Field(
        ..., description="Brand hierarchy: macro-category name → CategoryNode."
    )
    allowed_sex: List[str] = Field(
        ..., description="Allowed sex values for fixed-field mapping (e.g. ['Uomo', 'Donna', 'Unisex'])."
    )

    # --- Pipeline output fields (filled progressively by nodes) ---
    raw_text: str = Field(default="", description="Raw text extracted from the PDF by pdfplumber.")
    cleaned_text: str = Field(default="", description="Cleaned text after LLM noise removal (Stage 1).")
    base_items: List[dict] = Field(default_factory=list, description="Initial product array from Stage 2 extraction.")

    # Annotated with operator.add so LangGraph accumulates results from all parallel
    # item branches instead of overwriting. Each item_merge_node call returns a
    # single-element list; they are concatenated at fan-in.
    enriched_items: Annotated[List[dict], operator.add] = Field(
        default_factory=list, description="Final enriched items after Stage 3 processing."
    )
    warnings: Annotated[List[str], operator.add] = Field(
        default_factory=list, description="Non-fatal per-item warnings."
    )


class ItemState(BaseModel):
    """
    State for a single item's parallel enrichment sub-graph.
    One instance is created per base_item via the Send() API.
    """

    # --- Forwarded read-only context from GraphState ---
    item: dict = Field(..., description="The raw base_item dict being enriched.")
    brand: str = Field(..., description="Forwarded brand name.")
    categories: dict[str, CategoryNode] = Field(..., description="Forwarded brand hierarchy.")
    allowed_sex: List[str] = Field(..., description="Forwarded allowed sex values.")

    # --- Stage 3a: Web Search ---
    web_search_raw: str = Field(default="", description="Raw LLM response from web search.")
    web_search_parsed: str = Field(default="", description="Parsed XML sections from web search output.")
    web_search_urls: List[str] = Field(default_factory=list, description="Grounding URLs from web search.")

    # --- Stage 3b1: Macro-Category ---
    macro_category: Optional[str] = Field(default=None, description="Mapped macro-category name.")

    # --- Stage 3b2: Sub-Category ---
    sub_category: Optional[str] = Field(default=None, description="Mapped sub-category name.")

    # --- Stage 3b3: Fixed Fields ---
    sex: Optional[str] = Field(default=None, description="Mapped sex field.")
    materials: List[str] = Field(default_factory=list, description="Extracted materials list.")
    colors: List[str] = Field(default_factory=list, description="Extracted/inferred colors list.")

    # --- Stage 3c: Free-Form ---
    product_name: str = Field(default="", description="Official product name (no SKU).")
    product_short_description: str = Field(default="", description="Concise ERP-friendly description.")
    product_extended_description: str = Field(default="", description="Comprehensive description for semantic search.")
    tags: List[str] = Field(default_factory=list, description="Up to 10 semantic search tags.")

    # --- Non-fatal warnings accumulated during this item's processing ---
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings for this item.")

    # --- Output to parent graph ---
    enriched_items: List[dict] = Field(
        default_factory=list, 
        description="The final merged item wrapped in a list, so it can be accumulated by the parent GraphState."
    )
