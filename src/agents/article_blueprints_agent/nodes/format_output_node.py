from src.agents.article_blueprints_agent.state import BlueprintsGraphState


def _clean_item(item: dict) -> dict:
    colors = item.get("colors") or item.get("Color") or []
    if isinstance(colors, str):
        colors = [colors] if colors.strip() else []
    elif isinstance(colors, list):
        colors = [str(c) for c in colors if c]
    else:
        colors = []

    return {
        "item_id": item.get("item_id", ""),
        "vendor_code": item.get("vendor_code") or item.get("VendorCode") or "",
        "quantity": item.get("quantity") or item.get("Quantity") or 0,
        "colors": colors,
        "article_blueprint_id": item.get("article_blueprint_id", ""),
    }


def _clean_blueprint(bp: dict) -> dict:
    cleaned = {
        "id": bp.get("id", ""),
        "is_new": bp.get("is_new", True),
    }
    if cleaned["is_new"]:
        cleaned["category"] = bp.get("category")
        cleaned["sub_category"] = bp.get("sub_category")
        cleaned["article_name"] = bp.get("article_name")
        cleaned["description"] = bp.get("description")
        cleaned["extended_description"] = bp.get("extended_description")
        cleaned["tags"] = bp.get("tags", [])
        cleaned["materials"] = bp.get("materials", [])
    return cleaned


def format_output_node(state: BlueprintsGraphState) -> dict:
    """
    Format final bipartite items and blueprints lists, stripping internal fields.
    """
    all_raw_items = list(state.matched_items) + list(state.unmatched_items)
    clean_items = [_clean_item(it) for it in all_raw_items]

    seen_bp_ids = set()
    clean_blueprints = []

    for bp in list(state.output_blueprints) + list(state.new_blueprints):
        bp_id = bp.get("id")
        if bp_id and bp_id not in seen_bp_ids:
            clean_blueprints.append(_clean_blueprint(bp))
            seen_bp_ids.add(bp_id)

    print(f"[format_output_node] Formatted {len(clean_items)} items and {len(clean_blueprints)} blueprints.")
    return {
        "output_items": clean_items,
        "output_blueprints": clean_blueprints,
    }
