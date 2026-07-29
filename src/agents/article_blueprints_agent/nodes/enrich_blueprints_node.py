import asyncio
import json
from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()

_SYSTEM_PROMPT = (
    "Sei un Agente AI di arricchimento catalogo PIM per un brand moda e lusso. "
    "Il tuo compito è analizzare un articolo (nome e descrizione) e la gerarchia categorie del brand "
    "e restituire ESCLUSIVAMENTE un JSON con i seguenti campi:\n"
    "- category: (stringa, nome esatto della macro-categoria appropriata tra quelle fornite)\n"
    "- sub_category: (stringa o null, nome esatto della sotto-categoria tra quelle fornite)\n"
    "- extended_description: (stringa, descrizione marketing estesa e dettagliata di almeno 3-4 frasi in italiano)\n"
    "- tags: (array di stringhe, fino a 10 parole chiave SEO rilevanti per la ricerca semantica)\n"
    "- materials: (array di stringhe, materiali principali dell'articolo. INCLUDI un materiale SOLO se è esplicitamente menzionato nel nome o nella descrizione dell'articolo forniti. Se non sono menzionati materiali, restituisci un array vuoto [])\n"
    "Non inventare categorie non presenti nell'elenco fornito."
)

_MODEL = "gemini-3.1-flash-lite"


async def _enrich_single_blueprint(client: LLMClient, bp: dict, categories: dict) -> dict:
    bp_copy = dict(bp)
    name = bp.get("article_name", "")
    desc = bp.get("description", "")

    cat_lines = []
    for cat_name, cat_obj in categories.items():
        if isinstance(cat_obj, dict):
            sub_cats = list(cat_obj.get("sub_categories", {}).keys()) if isinstance(cat_obj.get("sub_categories"), dict) else []
            cat_lines.append(f"- Macro: '{cat_name}' | Sottocategorie: {sub_cats}")
        elif hasattr(cat_obj, "sub_categories"):
            sub_cats = list(cat_obj.sub_categories.keys()) if isinstance(cat_obj.sub_categories, dict) else []
            cat_lines.append(f"- Macro: '{cat_name}' | Sottocategorie: {sub_cats}")
        else:
            cat_lines.append(f"- Macro: '{cat_name}'")
    cat_prompt = "\n".join(cat_lines)

    prompt = (
        f"Articolo:\nNome: {name}\nDescrizione: {desc}\n\n"
        f"Gerarchia Categorie Brand:\n{cat_prompt}\n\n"
        f"Mappa la categoria corretta e genera descrizione estesa, tag e materiali in formato JSON."
    )

    fallback_cat = list(categories.keys())[0] if categories else "Generico"

    try:
        raw_res = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 6 - Blueprint Enrichment",
            response_mime_type="application/json",
        )
        parsed = json.loads(raw_res)
        bp_copy["category"] = parsed.get("category", fallback_cat)
        bp_copy["sub_category"] = parsed.get("sub_category")
        bp_copy["extended_description"] = parsed.get("extended_description", f"{name} - {desc}")
        bp_copy["tags"] = parsed.get("tags", [name.lower()])
        bp_copy["materials"] = parsed.get("materials", [])
    except Exception as exc:
        print(f"[enrich_blueprints_node] Warning: Enrichment failed ({exc}). Using fallback.")
        bp_copy["category"] = fallback_cat
        bp_copy["sub_category"] = None
        bp_copy["extended_description"] = f"{name} - {desc}"
        bp_copy["tags"] = [name.lower()] if name else []
        bp_copy["materials"] = []

    return bp_copy


async def enrich_blueprints_node(state: BlueprintsGraphState) -> dict:
    """
    For each new blueprint, perform web searches (temp=0.0) based on the synthesized article_name and description
    to enrich it with an extended_description.
    """
    logger.log_agent("enrich_blueprints_node", "node_entry", "ok", 
                     new_blueprints_count=len(state.new_blueprints))
    print(f"[enrich_blueprints_node] Enriching {len(state.new_blueprints)} new blueprint(s) via web search...")
    client = LLMClient()
    tasks = [_enrich_single_blueprint(client, bp, state.categories) for bp in state.new_blueprints]
    enriched = await asyncio.gather(*tasks)
    warnings = [] # Placeholder logic
    if warnings:
        for w in warnings:
            print(w)

    print("[enrich_blueprints_node] Enrichment complete.")
    result = {"new_blueprints": enriched, "warnings": warnings}
    logger.log_agent("enrich_blueprints_node", "node_exit", "ok", output=result)
    return result
