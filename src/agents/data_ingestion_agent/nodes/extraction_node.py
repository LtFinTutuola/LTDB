"""
nodes/extraction_node.py
-------------------------
Stage 2: Extract the structured product array from the cleaned text.

Uses the LLM with JSON output mode to normalise the shipment rows into
a list of dicts with keys: VendorCode, Barcode, Description, Color, Quantity.

Raises AgentException (fatal) if:
  - The LLM call fails.
  - The response cannot be parsed as JSON.
  - The resulting list is empty.
"""
import json

from src.agents.base import AgentException
from src.agents.llm_client import LLMClient
from src.agents.data_ingestion_agent.state import GraphState

_SYSTEM_PROMPT = (
    "Sei un Agente di Document Intelligence per un sistema ERP. "
    "Riceverai in input un testo pulito contenente solo gli articoli di una bolla di spedizione B2B. "
    "Il tuo compito è analizzare semanticamente questi dati "
    "e restituire ESCLUSIVAMENTE un array JSON di oggetti normalizzati con le seguenti chiavi: "
    "VendorCode (es. modello/codice prodotto), Barcode (se presente), Description (descrizione del prodotto), "
    "Color (codice/colore), Quantity (leggendo i dati relativi alle quantità o confezioni associate all'articolo)."
)

_MODEL = "gemini-3.1-flash-lite"


async def extraction_node(state: GraphState) -> dict:
    """
    Extract a structured list of products from the cleaned text.

    Returns:
        Partial state update: {"base_items": <list_of_dicts>}

    Raises:
        AgentException: On LLM failure, JSON parse error, or empty result.
    """
    print("[extraction_node] Extracting product JSON from cleaned text...")
    client = LLMClient()
    prompt = f"Analizza e mappa questo testo in JSON:\n\n{state.cleaned_text}"

    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 2 - JSON Extraction",
            response_mime_type="application/json",
        )
    except Exception as exc:
        raise AgentException(
            message=f"[extraction_node] LLM call failed: {exc}",
            output=None,
        ) from exc

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise AgentException(
            message=f"[extraction_node] Failed to parse LLM JSON response: {exc}",
            output={"raw_response": raw_response},
        ) from exc

    if not isinstance(parsed, list) or len(parsed) == 0:
        raise AgentException(
            message="[extraction_node] LLM returned an empty or non-list result.",
            output={"parsed": parsed},
        )

    print(f"[extraction_node] Extracted {len(parsed)} product(s).")
    return {"base_items": parsed}
