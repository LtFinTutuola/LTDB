import json
import uuid

from src.agents.base import AgentException
from src.agents.llm_client import LLMClient
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.core.logger import get_logger
from src.utils.llm_models import DATA_EXTRACTION_JSON_MODEL as _MODEL
from src.utils.prompts import DATA_EXTRACTION_JSON_SYSTEM_PROMPT

logger = get_logger()


async def extraction_node(state: ExtractionGraphState) -> dict:
    """
    Extract a structured list of products from the cleaned text and inject item_id.
    """
    logger.log_agent("extraction_node", "node_entry", "ok", cleaned_text=state.cleaned_text)
    print("[extraction_node] Extracting product JSON from cleaned text...")
    client = LLMClient()
    prompt = f"Analizza e mappa in JSON il seguente testo:\n\n<CLEANED_DDT>\n{state.cleaned_text}\n</CLEANED_DDT>\n"
    if state.extraction_errors:
        prompt += "\nATTENZIONE: Nel tentativo precedente hai commesso il seguente errore di estrazione:\n"
        for err in state.extraction_errors:
            prompt += f"- {err}\n"
        prompt += "Correggi la mappatura delle colonne. Se hai scambiato due colonne (es. Materiale e SKU), invertile in modo che VendorCode corrisponda al pattern richiesto.\n"
    try:
        raw_response = await client.call(
            model_name=_MODEL,
            system_prompt=DATA_EXTRACTION_JSON_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 2 - JSON Extraction",
            response_mime_type="application/json",
            temperature=0.0,
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
    result = {"extracted_items": parsed}
    logger.log_agent("extraction_node", "node_exit", "ok", output=result)
    return result
