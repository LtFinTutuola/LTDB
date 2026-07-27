"""
nodes/marketing_copy_node.py
----------------------------
Stage 5: Generate the creative marketing copy (extended description and tags) per group.

Runs in the main graph after blueprint_grouping_node.
Makes a single LLM call per unique blueprint_group_id to save tokens and ensure
consistency across identical variants.
"""
import json
from typing import List

from pydantic import BaseModel, Field

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import GraphState

_SYSTEM_PROMPT = (
    "You are an AI marketing assistant. Read the provided product details and generate "
    "a comprehensive extended description and semantic tags.\n"
    "CRITICAL RULES:\n"
    "1. The 'product_extended_description' should be a rich, SEO-friendly description.\n"
    "2. Populate 'tags' with up to 10 semantic keywords describing the item to enhance downstream search.\n"
    "3. All output data values MUST be written in Italian."
)

_MODEL = "gemini-3.1-flash-lite"


class MarketingCopySheet(BaseModel):
    product_extended_description: str = Field(
        description="Extended and comprehensive description including all details; aimed at downstream semantic search."
    )
    tags: List[str] = Field(
        description="List of up to 10 descriptive tags (e.g., style, material details, usage) to enhance semantic search."
    )


async def marketing_copy_node(state: GraphState) -> dict:
    """
    Generate extended description and tags once per blueprint_group_id and apply to all items in that group.
    """
    enriched_items = list(state.enriched_items)
    if not enriched_items:
        return {}

    # Group items by blueprint_group_id
    groups = {}
    for item in enriched_items:
        gid = item.get("blueprint_group_id")
        if gid:
            groups.setdefault(gid, []).append(item)

    client = LLMClient()

    for gid, items in groups.items():
        # Pick the first item as representative for the group
        rep = items[0]
        
        # Build prompt using representative item's data
        context = {
            "name": rep.get("article_name"),
            "short_desc": rep.get("product_short_description"),
            "category": rep.get("category"),
            "materials": rep.get("materials"),
            "colors": rep.get("colors")
        }
        
        prompt = (
            f"Generate the extended description and tags based on these core product details.\n\n"
            f"--- CORE DETAILS ---\n{json.dumps(context, indent=2, ensure_ascii=False)}\n"
        )
        
        try:
            raw_response = await client.call(
                model_name=_MODEL,
                system_prompt=_SYSTEM_PROMPT,
                prompt=prompt,
                pipeline_stage="Stage 5 - Marketing Copy Generation",
                response_schema=MarketingCopySheet,
                # Using default temperature (e.g. 0.7) for creative generation
            )
            parsed = json.loads(raw_response)
            
            ext_desc = parsed.get("product_extended_description", "")
            tags = parsed.get("tags", [])
            
            # Apply to all items in the group
            for item in items:
                item["product_extended_description"] = ext_desc
                item["tags"] = tags
                
            print(f"[marketing_copy_node] Generated marketing copy for group {gid}.")
            
        except Exception as exc:
            warning = f"[marketing_copy_node] Marketing copy generation failed for group {gid}: {exc}"
            print(warning)
            for item in items:
                item["product_extended_description"] = ""
                item["tags"] = []
                item.setdefault("warnings", []).append(warning)

    new_items = [dict(item, _overwrite=True) for item in enriched_items]
    return {"enriched_items": new_items}
