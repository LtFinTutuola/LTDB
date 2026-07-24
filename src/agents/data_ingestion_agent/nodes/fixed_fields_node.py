"""
nodes/fixed_fields_node.py
---------------------------
Stage 3b3: Extract sex, materials, and colors using a strict Pydantic schema.

The allowed sex values come directly from state.allowed_sex so the agent
remains configurable without any code changes.
"""
import json
from typing import List

from pydantic import BaseModel, Field

from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import ItemState

_SYSTEM_PROMPT = (
    "You are an AI mapping assistant. Read the provided product details and map them "
    "strictly to the allowed JSON schema values.\n"
    "CRITICAL RULES:\n"
    "1. 'sex' MUST be populated using strictly one of the values provided in the Allowed lists. "
    "Extract 'materials' directly from the text.\n"
    "2. All output data values MUST be written in Italian.\n"
    "3. You must return arrays for 'materials' and 'colors' instead of single string values."
)

_MODEL = "gemini-3.1-flash-lite"


class MappedFieldsSheet(BaseModel):
    sex: str = Field(
        description="Target gender for the product. MUST be exactly one of the values provided in the prompt's Allowed Sex list."
    )
    materials: List[str] = Field(
        description="List of materials of the product. Extract the materials directly from the provided text."
    )
    colors: List[str] = Field(
        description="List of colors of the product inferred from codes or web images."
    )


async def fixed_fields_node(state: ItemState) -> dict:
    """
    Extract sex, materials, and colors for a single product item.

    Returns:
        Partial ItemState update: {"sex": ..., "materials": [...], "colors": [...]}
        On failure, returns empty/None values with a warning.
    """
    context = state.web_search_parsed if state.web_search_parsed else state.web_search_raw
    allowed_sex_str = ", ".join(state.allowed_sex)

    prompt = (
        f"Map the following data to the required JSON schema.\n\n"
        f"--- PRODUCT DETAILS ---\n{context}\n\n"
        f"--- ALLOWED MAPPING VALUES ---\n"
        f"Allowed Sex: {allowed_sex_str}\n"
    )

    client = LLMClient()

    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3b3 - Fixed Fields",
            response_schema=MappedFieldsSheet,
        )
        parsed = json.loads(raw_response)
        print(f"[fixed_fields_node] sex='{parsed.get('sex')}', materials={parsed.get('materials')}, colors={parsed.get('colors')}")
        return {
            "sex": parsed.get("sex"),
            "materials": parsed.get("materials", []),
            "colors": parsed.get("colors", []),
        }
    except Exception as exc:
        warning = f"[fixed_fields_node] Field mapping failed ({exc})."
        print(warning)
        return {"sex": None, "materials": [], "colors": [], "warnings": [warning]}
