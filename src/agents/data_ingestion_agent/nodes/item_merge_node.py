"""
nodes/item_merge_node.py
-------------------------
Fan-in node: assembles the final enriched item dictionary.

This node runs once per item at the end of each parallel branch and
returns a single-element list. Because GraphState.enriched_items uses
operator.add as its LangGraph reducer, these lists are concatenated at
the fan-in point — producing the final complete list of enriched items.
"""
from src.agents.data_ingestion_agent.state import ItemState


async def item_merge_node(state: ItemState) -> dict:
    """
    Merge all per-stage results into a single enriched item dict.

    Returns:
        {"enriched_items": [<merged_item>], "warnings": <accumulated_warnings>}
    """
    raw_item = state.item

    merged_item = {
        # --- Original fields from Stage 2 extraction ---
        "VendorCode": raw_item.get("VendorCode"),
        "Barcode": raw_item.get("Barcode"),
        "Quantity": raw_item.get("Quantity"),

        # --- Stage 3b1 / 3b2: Category mapping ---
        "category": state.macro_category,
        "sub_category": state.sub_category,

        # --- Stage 3b3: Fixed fields ---
        "sex": state.sex,
        "materials": state.materials,
        "colors": state.colors,

        # --- Stage 3c: Free-form fields ---
        "article_name": state.article_name,
        "product_short_description": state.product_short_description,
        "product_extended_description": state.product_extended_description,
        "tags": state.tags,

        # --- Stage 3a: Web search metadata ---
        "sources": state.web_search_urls,

        # --- Per-item warnings (non-fatal issues during enrichment) ---
        "warnings": state.warnings,
    }

    print(f"[item_merge_node] Merged item: VendorCode='{merged_item['VendorCode']}'.")
    return {"enriched_items": [merged_item]}
