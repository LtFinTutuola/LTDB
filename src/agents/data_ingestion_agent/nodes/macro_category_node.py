"""
nodes/macro_category_node.py
-----------------------------
Stage 3b1: Map a product to its macro-category.

SHORT-CIRCUIT: if the brand has only one (or zero) macro-categories
available, the assignment is trivial and we skip the LLM call entirely.
Otherwise a dynamic Pydantic schema is built from the available categories
and sent to the LLM for a constrained JSON response.
"""
import json
from typing import Optional

from pydantic import Field, create_model

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import ItemState

_SYSTEM_PROMPT = (
    "You are an AI mapping assistant. Read the provided product details "
    "and select strictly one of the allowed macro-categories."
)

_MODEL = "gemini-3.1-flash-lite"


async def macro_category_node(state: ItemState) -> dict:
    """
    Assign a macro-category to the item.

    Returns:
        Partial ItemState update: {"macro_category": <value_or_None>}
    """
    available_macros = list(state.categories.keys())

    # --- Short-circuit ---
    if len(available_macros) <= 1:
        chosen = available_macros[0] if available_macros else None
        print(f"[macro_category_node] Short-circuit: only option is '{chosen}'.")
        return {"macro_category": chosen}

    # --- Build dynamic schema ---
    macro_descriptions = [
        f"- {m}: {state.categories[m].description}" for m in available_macros
    ]
    macro_options_str = "\n".join(macro_descriptions)

    MacroCategorySchema = create_model(
        "MacroCategorySchema",
        macro_category=(
            str,
            Field(description=f"Select the most appropriate macro-category. Allowed values:\n{macro_options_str}"),
        ),
    )

    context = state.web_search_parsed if state.web_search_parsed else state.web_search_raw
    prompt = (
        f"Select the appropriate macro-category for the following product.\n\n"
        f"--- PRODUCT DETAILS ---\n{context}\n\n"
        f"--- ALLOWED MACRO-CATEGORIES ---\n{macro_options_str}\n"
    )

    client = LLMClient()

    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3b1 - Macro-Category",
            response_schema=MacroCategorySchema,
        )
        parsed = json.loads(raw_response)
        macro_category: Optional[str] = parsed.get("macro_category")
        print(f"[macro_category_node] Mapped to: '{macro_category}'.")
        return {"macro_category": macro_category}
    except Exception as exc:
        warning = f"[macro_category_node] Mapping failed ({exc}), defaulting to None."
        print(warning)
        return {"macro_category": None, "warnings": [warning]}
