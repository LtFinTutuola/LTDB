import asyncio
import json
from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()

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
    "Restituisci ESCLUSIVAMENTE un oggetto JSON con le chiavi: article_name, description."
)

_MODEL = "gemini-3.1-flash-lite"


async def _synthesize_single_blueprint(client: LLMClient, bp: dict) -> dict:
    bp_copy = dict(bp)
    items = bp.get("cluster_items", [])
    if not items:
        bp_copy["article_name"] = "Articolo Sconosciuto"
        bp_copy["description"] = "Descrizione non disponibile"
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
            pipeline_stage="Stage 5 - Blueprint Synthesis",
            response_mime_type="application/json",
        )
        parsed = json.loads(raw_res)
        bp_copy["article_name"] = parsed.get("article_name", fallback_name)
        bp_copy["description"] = parsed.get("description", fallback_desc)
    except Exception as exc:
        print(f"[synthesize_blueprints_node] Warning: Synthesis failed ({exc}). Using fallback.")
        bp_copy["article_name"] = fallback_name
        bp_copy["description"] = fallback_desc

    return bp_copy


async def synthesize_blueprints_node(state: BlueprintsGraphState) -> dict:
    """
    For each new blueprint, invoke LLM to synthesize article_name, description, tags, materials
    based on the characteristics of its clustered items.
    """
    logger.log_agent("synthesize_blueprints_node", "node_entry", "ok", 
                     new_blueprints_count=len(state.new_blueprints))
    print(f"[synthesize_blueprints_node] Synthesizing {len(state.new_blueprints)} new blueprint(s)...")
    client = LLMClient()
    tasks = [_synthesize_single_blueprint(client, bp) for bp in state.new_blueprints]
    updated_blueprints = await asyncio.gather(*tasks)
    print("[synthesize_blueprints_node] Synthesis complete.")
    result = {"new_blueprints": updated_blueprints}
    logger.log_agent("synthesize_blueprints_node", "node_exit", "ok", output=result)
    return result
