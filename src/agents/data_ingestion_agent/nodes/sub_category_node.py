"""
nodes/sub_category_node.py
---------------------------
Stage 3b2: Map a product to its sub-category within the chosen macro-category.

SHORT-CIRCUIT: if macro_category is None/invalid, or only one sub-category
exists, we skip the LLM call. Otherwise a dynamic schema is built from the
available sub-categories of the resolved macro-category.
"""
import json
from typing import Optional

from pydantic import Field, create_model

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import ItemState

_SYSTEM_PROMPT = (
    "You are an AI mapping assistant. Read the provided product details "
    "and select strictly one of the allowed sub-categories."
)

_MODEL = "gemini-3.1-flash-lite"


async def sub_category_node(state: ItemState) -> dict:
    """
    Assign a sub-category to the item based on the resolved macro-category.

    Returns:
        Partial ItemState update: {"sub_category": <value_or_None>}
    """
    macro = state.macro_category

    # --- Short-circuit: no valid macro ---
    if not macro or macro not in state.categories:
        print(f"[sub_category_node] Short-circuit: invalid macro_category '{macro}'.")
        return {"sub_category": None}

    available_subs_dict: dict[str, str] = state.categories[macro].sub_categories
    available_subs = list(available_subs_dict.keys())

    # --- Short-circuit: single or zero sub-categories ---
    if len(available_subs) <= 1:
        chosen = available_subs[0] if available_subs else None
        print(f"[sub_category_node] Short-circuit: only option is '{chosen}'.")
        return {"sub_category": chosen}

    # --- Build dynamic schema ---
    sub_descriptions = [f"- {s}: {available_subs_dict[s]}" for s in available_subs]
    sub_options_str = "\n".join(sub_descriptions)

    SubCategorySchema = create_model(
        "SubCategorySchema",
        sub_category=(
            str,
            Field(description=f"Select the most appropriate sub-category. Allowed values:\n{sub_options_str}"),
        ),
    )

    context = state.web_search_parsed if state.web_search_parsed else state.web_search_raw
    prompt = (
        f"Select the appropriate sub-category for the following product "
        f"under macro-category '{macro}'.\n\n"
        f"--- PRODUCT DETAILS ---\n{context}\n\n"
        f"--- ALLOWED SUB-CATEGORIES ---\n{sub_options_str}\n"
    )

    client = LLMClient()

    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3b2 - Sub-Category",
            response_schema=SubCategorySchema,
        )
        parsed = json.loads(raw_response)
        sub_category: Optional[str] = parsed.get("sub_category")
        print(f"[sub_category_node] Mapped to: '{sub_category}'.")
        return {"sub_category": sub_category}
    except Exception as exc:
        warning = f"[sub_category_node] Mapping failed ({exc}), defaulting to None."
        print(warning)
        return {"sub_category": None, "warnings": [warning]}
