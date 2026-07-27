"""
nodes/free_form_node.py
------------------------
Stage 3c: Generate the free-form descriptive fields and semantic tags.

Uses the raw web search output (not the parsed XML version) to maximise
the context available for generating rich descriptions.
"""
import json
from typing import List

from pydantic import BaseModel, Field

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import ItemState

_SYSTEM_PROMPT = (
    "You are an AI describing assistant. Read the provided raw web data and generate "
    "comprehensive, free-form fields.\n"
    "CRITICAL RULES:\n"
    "1. DO NOT include the SKU or VendorCode inside the 'article_name'.\n"
    "2. Populate 'tags' with up to 10 semantic keywords describing the item to enhance downstream search.\n"
    "3. All output data values MUST be written in Italian."
)

_MODEL = "gemini-3.1-flash-lite"


class FreeFormFieldsSheet(BaseModel):
    article_name: str = Field(
        description="Official name of the article/product. DO NOT include the SKU or VendorCode in this string."
    )
    product_short_description: str = Field(
        description="Concise, technical, and precise description to help an ERP operator identify the product. NO commercial fluff."
    )
    product_extended_description: str = Field(
        description="Extended and comprehensive description including all details; aimed at downstream semantic search."
    )
    tags: List[str] = Field(
        description="List of up to 10 descriptive tags (e.g., color, style, material details, usage) to enhance semantic search."
    )


async def free_form_node(state: ItemState) -> dict:
    """
    Generate free-form descriptions and SEO tags for a single product.

    Returns:
        Partial ItemState update with article_name, product_short_description,
        product_extended_description, and tags.
        On failure, returns empty strings/lists with a warning.
    """
    # Use raw web text for maximum context; fall back to parsed if raw is empty
    context = state.web_search_raw if state.web_search_raw else state.web_search_parsed

    prompt = (
        f"Generate the free-form description fields and tags based on this raw data.\n\n"
        f"--- RAW WEB DATA ---\n{context}\n"
    )

    client = LLMClient()

    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3c - Free Form Completion",
            response_schema=FreeFormFieldsSheet,
        )
        parsed = json.loads(raw_response)
        print(f"[free_form_node] Generated name: '{parsed.get('article_name')}'.")
        return {
            "article_name": parsed.get("article_name", ""),
            "product_short_description": parsed.get("product_short_description", ""),
            "product_extended_description": parsed.get("product_extended_description", ""),
            "tags": parsed.get("tags", []),
        }
    except Exception as exc:
        warning = f"[free_form_node] Free-form completion failed ({exc})."
        print(warning)
        return {
            "article_name": "",
            "product_short_description": "",
            "product_extended_description": "",
            "tags": [],
            "warnings": [warning],
        }
