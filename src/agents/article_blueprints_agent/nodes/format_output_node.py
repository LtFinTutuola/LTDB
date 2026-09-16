from typing import List
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()


def _clean_item(item: dict) -> dict:
    colors = item.get("colors") or item.get("Color") or []
    if isinstance(colors, str):
        colors = [colors] if colors.strip() else []
    elif isinstance(colors, list):
        colors = [str(c) for c in colors if c]
    else:
        colors = []

    try:
        qty = int(item.get("quantity") or item.get("Quantity") or 0)
    except (ValueError, TypeError):
        qty = 0

    return {
        "item_id": item.get("item_id", ""),
        "vendor_code": item.get("vendor_code") or item.get("VendorCode") or "",
        "normalized_vendor_code": item.get("normalized_vendor_code") or "",
        "barcode": item.get("barcode") or item.get("Barcode") or "",
        "quantity": qty,
        "colors": colors,
        "article_blueprint_id": item.get("article_blueprint_id", ""),
    }


def _clean_blueprint(bp: dict) -> dict:
    cleaned = {
        "id": bp.get("id", ""),
        "is_new": bp.get("is_new", True),
        "article_name": bp.get("article_name"),
        "description": bp.get("description"),
    }
    if cleaned["is_new"]:
        cleaned["category"] = bp.get("category")
        cleaned["sub_category"] = bp.get("sub_category")
        cleaned["extended_description"] = bp.get("extended_description")
        cleaned["tags"] = bp.get("tags", [])
        cleaned["materials"] = bp.get("materials", [])
        cleaned["dimensions"] = bp.get("dimensions")
    return cleaned


def format_output_node(state: BlueprintsGraphState) -> dict:
    """
    Format final bipartite items and blueprints lists, stripping internal fields.
    """
    logger.log_agent("format_output_node", "node_entry", "ok", 
                     new_blueprints_count=len(state.new_blueprints))
    
    clean_items = []
    seen_bp_ids = set()
    clean_blueprints = []

    for bp in list(state.output_blueprints) + list(state.new_blueprints):
        bp_id = bp.get("id")
        
        # Extract items from cluster and link them to the new blueprint
        for it in bp.get("cluster_items", []):
            it["article_blueprint_id"] = bp_id
            clean_items.append(_clean_item(it))

        if bp_id and bp_id not in seen_bp_ids:
            clean_blueprints.append(_clean_blueprint(bp))
            seen_bp_ids.add(bp_id)

    print(f"[format_output_node] Formatted {len(clean_items)} items and {len(clean_blueprints)} blueprints.")
    result = {
        "output_items": clean_items,
        "output_blueprints": clean_blueprints,
        "photo_proposals": state.photo_proposals,
    }
    logger.log_agent("format_output_node", "node_exit", "ok", output=result)
    return result
