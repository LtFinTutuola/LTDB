"""
src/agents/warehouse_search_agent/nodes/query_plan_node.py
----------------------------------------------------------
Node 1: Translates a user natural language query into a structured SearchQueryPlan JSON
using database schema context and live Brand/Category taxonomy.
"""
from src.agents.llm_client import LLMClient
from src.agents.warehouse_search_agent.state import WarehouseSearchGraphState, SearchQueryPlan
from src.core.logger import get_logger

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT = """Sei un query planner AI per il database magazzino/catalogo di un negozio di pelletteria, valigeria e accessori moda (LTDB).
Lo schema logico del database su cui operi è composto da:
1. `article_blueprints`: rappresenta il modello base dell'articolo, collegato a:
   - `brands` (`brand_name`): marca del prodotto.
   - `categories` (`category_name`): gerarchia a due livelli (Macro-categoria -> Sotto-categoria).
   - Campi descrittivi indicizzati semanticamente tramite embedding: `article_name`, `description`, `extended_description`, `tags`, `materials`.
2. `articles`: rappresenta le singole unità fisiche/varianti in magazzino collegate a un `article_blueprint`, dotate di `colors` (valutati in seguito).

Il tuo compito è convertire la query testuale dell'utente in un piano di ricerca JSON strutturato (`SearchQueryPlan`):
- `brand_name`: se l'utente cita esplicitamente un brand presente nella lista dei Brand a catalogo, riporta il nome esatto del Brand a catalogo; altrimenti `null`.
- `category_name`: se l'utente cita una categoria o sottocategoria presente nella tassonomia fornita (es. "borsa" -> "Borse", "bauletto" -> "Bauletto", "zaino" -> "Zaini da scuola" o relativa sottocategoria), riporta il nome esatto della categoria o sottocategoria; altrimenti `null`.
- `semantic_query`:
  - Estrai SEMPRE la porzione residua della query rimuovendo il nome del brand e della categoria già mappati nei filtri statici (es. da "borsa di Guess con la tracolla" -> "con la tracolla"; da "borse di Guess nere" -> "nere"; da "borsa sportiva adatta a tutti i giorni" -> "sportiva adatta a tutti i giorni").
  - Imposta `null` SOLO se la query conteneva unicamente il brand e/o la categoria (es. "borse di Guess" -> `null`).
- `requires_semantic_search`:
  - Imposta `false` se la query contiene esclusivamente riferimenti al brand, alla categoria e/o a un colore semplice (poiché i colori vengono valutati separatamente su `semantic_query`), senza altri attributi descrittivi, funzionali o di materiale.
  - Imposta `true` in TUTTI gli altri casi: quando la query contiene dettagli funzionali (es. "con la tracolla", "con zip"), qualitativi/descrittivi (es. "sportiva adatta a tutti i giorni", "elegante da sera"), materiali (es. "in pelle", "nylon") o nomi specifici di linea/modello.
"""


def _format_taxonomy_prompt(brands: list[str], categories: dict) -> str:
    brands_str = ", ".join(brands) if brands else "Nessun brand registrato"
    cat_lines: list[str] = []
    for macro_name, macro_data in (categories or {}).items():
        desc = macro_data.get("description", "") if isinstance(macro_data, dict) else ""
        subs = macro_data.get("sub_categories", {}) if isinstance(macro_data, dict) else {}
        sub_names = ", ".join(subs.keys()) if subs else "Nessuna sottocategoria"
        cat_lines.append(f"- Macro-categoria: '{macro_name}' ({desc}) | Sottocategorie: [{sub_names}]")
    cats_str = "\n".join(cat_lines) if cat_lines else "Nessuna categoria registrata"
    return f"Brands disponibili a DB:\n[{brands_str}]\n\nTassonomia Categorie a DB:\n{cats_str}"


async def query_plan_node(state: WarehouseSearchGraphState) -> dict:
    """
    Generate a structured SearchQueryPlan from the natural language query.
    On LLM failure, gracefully falls back to a pure semantic search plan with the raw query.
    """
    logger.log_agent(
        "query_plan_node",
        "node_entry",
        "ok",
        raw_query=state.raw_query,
        brands_count=len(state.brands_catalog),
        categories_count=len(state.categories_catalog),
    )

    taxonomy_context = _format_taxonomy_prompt(state.brands_catalog, state.categories_catalog)
    user_prompt = (
        f"{taxonomy_context}\n\n"
        f"Query Utente: \"{state.raw_query}\"\n\n"
        f"Genera il piano di ricerca strutturato JSON."
    )

    try:
        client = LLMClient()
        raw_json = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=user_prompt,
            pipeline_stage="search_query_plan",
            response_schema=SearchQueryPlan,
            response_mime_type="application/json",
            temperature=0.0,
        )
        plan = SearchQueryPlan.model_validate_json(raw_json)

        # Safety guard: if neither brand nor category was extracted, semantic search MUST be active
        if not plan.brand_name and not plan.category_name:
            plan.requires_semantic_search = True
            if not plan.semantic_query or not plan.semantic_query.strip():
                plan.semantic_query = state.raw_query
        elif plan.requires_semantic_search and (not plan.semantic_query or not plan.semantic_query.strip()):
            plan.semantic_query = state.raw_query

    except Exception as exc:
        logger.log_agent("query_plan_node", "llm_plan_fallback", "err", exc=exc, raw_query=state.raw_query)
        plan = SearchQueryPlan(
            brand_name=None,
            category_name=None,
            requires_semantic_search=True,
            semantic_query=state.raw_query,
        )

    result = {"query_plan": plan}
    logger.log_agent("query_plan_node", "node_exit", "ok", output=plan.model_dump())
    return result
