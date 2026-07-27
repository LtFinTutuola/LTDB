"""
nodes/core_identity_node.py
------------------------
Stage 3c: Generate the deterministic core identity fields.

Uses the raw web search output to generate the article name and short description
with temperature=0.0 to guarantee predictability for downstream clustering.
"""
import json

from pydantic import BaseModel, Field

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import ItemState

_SYSTEM_PROMPT = (
    "You are an AI describing assistant. Read the provided raw web data and generate "
    "a highly standardized core identity for this product.\n"
    "CRITICAL RULES:\n"
    "1. DO NOT include the SKU, VendorCode, sizes, or colors inside the 'article_name'. It must be the canonical base model name.\n"
    "2. All output data values MUST be written in Italian.\n"
    "3. Be extremely concise and deterministic."
)

_MODEL = "gemini-3.1-flash-lite"


class CoreIdentitySheet(BaseModel):
    article_name: str = Field(
        description="Official, canonical name of the article/product. DO NOT include the SKU, VendorCode, sizes, or colors in this string."
    )
    product_short_description: str = Field(
        description="Concise, technical, and precise description to help an ERP operator identify the base product model. NO commercial fluff."
    )


async def core_identity_node(state: ItemState) -> dict:
    """
    Generate deterministic core descriptions for a single product.

    Returns:
        Partial ItemState update with article_name and product_short_description.
    """
    context = state.web_search_raw if state.web_search_raw else state.web_search_parsed

    prompt = (
        f"Generate the core identity fields based on this raw data.\n\n"
        f"--- RAW WEB DATA ---\n{context}\n"
    )

    client = LLMClient()

    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3c - Core Identity Generation",
            response_schema=CoreIdentitySheet,
            temperature=0.0,
        )
        parsed = json.loads(raw_response)
        print(f"[core_identity_node] Generated canonical name: '{parsed.get('article_name')}'.")
        return {
            "article_name": parsed.get("article_name", ""),
            "product_short_description": parsed.get("product_short_description", ""),
        }
    except Exception as exc:
        warning = f"[core_identity_node] Core identity generation failed ({exc})."
        print(warning)
        return {
            "article_name": "",
            "product_short_description": "",
            "warnings": [warning],
        }
