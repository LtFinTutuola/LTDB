import json
import uuid

from src.agents.base import AgentException
from src.agents.llm_client import LLMClient
from src.agents.data_extraction_agent.state import ExtractionGraphState

_SYSTEM_PROMPT = (
    "Sei un Agente di Document Intelligence per un sistema ERP. "
    "Riceverai in input un testo pulito contenente solo gli articoli di una bolla di spedizione B2B. "
    "Il tuo compito è analizzare semanticamente questi dati "
    "e restituire ESCLUSIVAMENTE un array JSON di oggetti normalizzati con le seguenti chiavi: "
    "VendorCode (es. modello/codice prodotto), Barcode (se presente), Description (descrizione del prodotto), "
    "Color (codice/colore), Quantity (leggendo i dati relativi alle quantità o confezioni associate all'articolo)."
)

_MODEL = "gemini-3.1-flash-lite"


async def extraction_node(state: ExtractionGraphState) -> dict:
    """
    Extract a structured list of products from the cleaned text and inject item_id.
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

    for item in parsed:
        item["item_id"] = str(uuid.uuid4())
        item["vendor_code"] = item.get("vendor_code") or item.get("VendorCode") or ""
        item["barcode"] = item.get("barcode") or item.get("Barcode") or ""
        try:
            item["quantity"] = int(item.get("quantity") or item.get("Quantity") or 0)
        except (ValueError, TypeError):
            item["quantity"] = 0

        c = item.get("Color") or item.get("colors") or []
        if isinstance(c, str):
            item["colors"] = [c] if c.strip() else []
        elif isinstance(c, list):
            item["colors"] = [str(x) for x in c if x]
        else:
            item["colors"] = []
        item["description"] = item.get("description") or item.get("Description") or ""

    if parsed:
        parsed[0]["_overwrite"] = True

    print(f"[extraction_node] Extracted and assigned item_ids to {len(parsed)} product(s).")
    return {"extracted_items": parsed}
