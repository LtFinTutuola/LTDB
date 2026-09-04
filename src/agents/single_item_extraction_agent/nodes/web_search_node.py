import re

from src.agents.llm_client import LLMClient
from src.agents.single_item_extraction_agent.state import SingleItemExtractionState
from src.core.logger import get_logger

logger = get_logger()

_SYSTEM_PROMPT = (
    "Sei un Agente AI specializzato nell'estrazione e standardizzazione anagrafica prodotti ERP. "
    "Il tuo obiettivo è navigare sul web utilizzando i suggerimenti dell'utente per trovare "
    "i dati ufficiali dell'articolo.\n"
    "REGOLE CRITICHE:\n"
    "1. Usa lo strumento di ricerca web di Google per trovare le informazioni. Puoi consultare qualsiasi fonte affidabile (es. e-commerce, rivenditori), non sei limitato al solo sito ufficiale del brand.\n"
    "2. DEVI racchiudere il nome ufficiale del prodotto nel tag esatto <NAME>...</NAME>.\n"
    "3. DEVI racchiudere una descrizione estesa, ottimizzata per SEO, nel tag esatto <DESCRIPTION>...</DESCRIPTION>.\n"
    "4. DEVI racchiudere una breve descrizione, utile per identificare visivamente il prodotto, nel tag esatto <OFFICIAL_SHORT_DESC>...</OFFICIAL_SHORT_DESC>.\n"
    "5. DEVI racchiudere i colori trovati nel tag esatto <OFFICIAL_COLORS>...</OFFICIAL_COLORS> (separati da virgola se più di uno).\n"
    "6. REGOLA SUI COLORI: Mappa o traduci ESCLUSIVAMENTE i colori suggeriti dall'utente nella loro denominazione commerciale ufficiale esatta (es. da 'Yellow' a 'Golden Yellow'). NON elencare altri colori disponibili per l'articolo.\n"
    "7. Se non trovi dati affidabili online, NON allucinare valori, ma restituisci un risultato vuoto per tutti i tag.\n"
    "8. IMPORTANTE: Scrivi l'output in italiano.\n"
    "9. Il nome e la descrizione (sia breve che estesa) devono essere COMPLETAMENTE NEUTRI rispetto al colore specifico.\n"
)

_MODEL = "gemini-3.1-flash-lite"


def _parse_extraction(raw_text: str, fallback_name: str, fallback_desc: str, fallback_colors: list[str]) -> tuple[str, str, str, list[str]]:
    name_match = re.search(r"<NAME>(.*?)</NAME>", raw_text, re.DOTALL | re.IGNORECASE)
    desc_match = re.search(r"<DESCRIPTION>(.*?)</DESCRIPTION>", raw_text, re.DOTALL | re.IGNORECASE)
    short_desc_match = re.search(r"<OFFICIAL_SHORT_DESC>(.*?)</OFFICIAL_SHORT_DESC>", raw_text, re.DOTALL | re.IGNORECASE)
    colors_match = re.search(r"<OFFICIAL_COLORS>(.*?)</OFFICIAL_COLORS>", raw_text, re.DOTALL | re.IGNORECASE)

    name = name_match.group(1).strip() if name_match else fallback_name
    desc = desc_match.group(1).strip() if desc_match else fallback_desc
    short_desc = short_desc_match.group(1).strip() if short_desc_match else fallback_desc
    
    colors = fallback_colors
    if colors_match:
        extracted_colors_str = colors_match.group(1).strip()
        colors = [c.strip() for c in extracted_colors_str.split(",") if c.strip()]
        if not colors:
            colors = fallback_colors

    return name, desc, short_desc, colors


async def web_search_node(state: SingleItemExtractionState) -> dict:
    """
    Perform a web search using user hints to extract the official data.
    """
    logger.log_agent("web_search_node", "node_entry", "ok", vendor_code=state.vendor_code)
    print(f"[web_search_node] Running single-item web search (temp=0.0) for {state.vendor_code}...")
    
    client = LLMClient()
    
    colors_str = ", ".join(state.colors) if state.colors else "Non specificato"
    prompt = (
        f"Effettua una ricerca sul web per trovare i dati di questo specifico articolo.\n\n"
        f"--- DATI DI PARTENZA (SUGGERIMENTI) ---\n"
        f"Brand: {state.brand}\n"
        f"Codice / Modello (VendorCode): {state.vendor_code}\n"
        f"Colori da mappare: {colors_str}\n\n"
        f"--- QUERY SUGGERITA ---\n"
        f'"{state.brand} {state.vendor_code}"\n\n'
        f"Estrai <NAME>, <DESCRIPTION>, <OFFICIAL_SHORT_DESC> e <OFFICIAL_COLORS> rispettando le regole di formato."
    )

    fallback_name = f"{state.brand} {state.vendor_code}".strip()
    fallback_desc = ""
    fallback_colors = state.colors

    item_copy = dict(state.extracted_item) if state.extracted_item else {}
    warnings = []

    try:
        from src.agents.base import AgentException
        
        raw_text, extracted_urls = await client.call_with_grounding(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3 - Web Search Extraction",
            max_output_tokens=512,
            temperature=0.0,
        )
        
        if not extracted_urls:
            raise AgentException(message=f"Codice '{state.vendor_code}' non trovato online. Estrazione bloccata.", output=None)

        name, desc, short_desc, colors = _parse_extraction(raw_text, fallback_name, fallback_desc, fallback_colors)
        
        item_copy["article_name"] = name
        item_copy["article_description"] = desc
        item_copy["description"] = short_desc
        item_copy["colors"] = colors
        
    except Exception as exc:
        warning = f"[web_search_node] Item '{state.vendor_code}': web search failed ({exc}). Using fallback."
        print(warning)
        item_copy["article_name"] = fallback_name
        item_copy["article_description"] = fallback_desc
        item_copy["description"] = fallback_desc
        item_copy["colors"] = fallback_colors
        warnings.append(warning)
        if "AgentException" in str(type(exc)):
            raise exc

    print("[web_search_node] Web search enrichment complete.")
    result = {"extracted_item": item_copy, "warnings": warnings}
    logger.log_agent("web_search_node", "node_exit", "ok", output=result)
    return result
