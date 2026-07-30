import asyncio
import json
from typing import Optional
from pydantic import BaseModel
from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Canonical output schema — enforced via Gemini responseSchema.
# All numeric fields are optional: if a dimension is not applicable or not
# mentioned in the source data, the model returns null.
# ---------------------------------------------------------------------------

class Dimensions(BaseModel):
    width_cm:  Optional[float] = None
    height_cm: Optional[float] = None
    depth_cm:  Optional[float] = None


class BlueprintSynthesisOutput(BaseModel):
    article_name: str
    description:  str
    dimensions:   Optional[Dimensions] = None


# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Sei un Agente di PIM (Product Information Management). "
    "Il tuo compito è analizzare le varianti di un gruppo di articoli simili e sintetizzare un unico nome commerciale ufficiale (article_name) "
    "e un'unica descrizione standardizzata (description) che rappresenti l'intero gruppo di articoli.\n"
    "REGOLE OBBLIGATORIE:\n"
    "1. Il nome articolo (article_name) NON deve contenere riferimenti a colori specifici. "
    "Usa solo il nome commerciale del modello e, se rilevante per distinguere il prodotto, la dimensione.\n"
    "2. La descrizione deve essere basata ESCLUSIVAMENTE sulle informazioni fornite nelle varianti in input. "
    "Non inventare materiali o caratteristiche non presenti nei dati forniti.\n"
    "3. Se le varianti fornite riportano materiali contraddittori tra loro, ometti completamente i materiali dalla descrizione.\n"
    "4. Estrai le dimensioni fisiche principali dell'articolo nel campo 'dimensions' (width_cm, height_cm, depth_cm). "
    "Usa i valori in centimetri. Se le dimensioni non sono presenti o non sono applicabili all'articolo, imposta il campo su null. "
    "La 'description' DEVE essere priva di qualsiasi riferimento numerico dimensionale.\n"
    "Restituisci ESCLUSIVAMENTE un oggetto JSON conforme allo schema fornito."
)

_MODEL = "gemini-3.1-flash-lite"


async def _synthesize_single_blueprint(client: LLMClient, bp: dict) -> dict:
    bp_copy = dict(bp)
    items = bp.get("cluster_items", [])
    if not items:
        bp_copy["article_name"] = "Articolo Sconosciuto"
        bp_copy["description"] = "Descrizione non disponibile"
        bp_copy["dimensions"] = None
        return bp_copy

    variations = []
    for it in items:
        variations.append(
            f"- Codice: {it.get('vendor_code', '')} | Nome: {it.get('article_name', '')} | Desc: {it.get('article_description', '')}"
        )
    variations_text = "\n".join(variations)

    prompt = f"Sintetizza un nome e descrizione unificati per questo gruppo di varianti articolo:\n\n{variations_text}"

    fallback_name = items[0].get("article_name") or items[0].get("description") or "Articolo"
    fallback_desc = items[0].get("article_description") or items[0].get("description") or "Descrizione articolo"

    try:
        raw_res = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 6 - Blueprint Synthesis",
            response_schema=BlueprintSynthesisOutput,
        )
        parsed = BlueprintSynthesisOutput.model_validate_json(raw_res)
        bp_copy["article_name"] = parsed.article_name
        bp_copy["description"] = parsed.description
        # Serialize Dimensions to a compact JSON string, or None if absent
        bp_copy["dimensions"] = (
            parsed.dimensions.model_dump_json(exclude_none=True)
            if parsed.dimensions else None
        )
    except Exception as exc:
        print(f"[synthesize_blueprints_node] Warning: Synthesis failed ({exc}). Using fallback.")
        bp_copy["article_name"] = fallback_name
        bp_copy["description"] = fallback_desc
        bp_copy["dimensions"] = None

    return bp_copy


async def synthesize_blueprints_node(state: BlueprintsGraphState) -> dict:
    """
    For each new blueprint, invoke LLM to synthesize article_name, description and
    dimensions based on the characteristics of its clustered items.
    The output schema is enforced via Gemini responseSchema (BlueprintSynthesisOutput),
    guaranteeing a canonical {width_cm, height_cm, depth_cm} structure every time.
    """
    logger.log_agent("synthesize_blueprints_node", "node_entry", "ok",
                     new_blueprints_count=len(state.new_blueprints))
    print(f"[synthesize_blueprints_node] Synthesizing {len(state.new_blueprints)} new blueprint(s)...")
    client = LLMClient()
    tasks = [_synthesize_single_blueprint(client, bp) for bp in state.new_blueprints]
    updated_blueprints = await asyncio.gather(*tasks)
    print("[synthesize_blueprints_node] Synthesis complete.")
    result = {"new_blueprints": list(updated_blueprints)}
    logger.log_agent("synthesize_blueprints_node", "node_exit", "ok", output=result)
    return result

