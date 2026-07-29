import asyncio
import re

from src.agents.llm_client import LLMClient
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.core.logger import get_logger

logger = get_logger()

_SYSTEM_PROMPT = (
    "Sei un Agente AI specializzato nell'estrazione e standardizzazione anagrafica prodotti ERP. "
    "Il tuo obiettivo è navigare sul web utilizzando il brand e il codice articolo per trovare "
    "il nome commerciale ufficiale e una descrizione accurata e deterministica dell'articolo.\n"
    "REGOLE CRITICHE:\n"
    "1. Usa lo strumento di ricerca web di Google per cercare i dati ufficiali del brand.\n"
    "2. DEVI racchiudere il nome ufficiale del prodotto nel tag esatto <NAME>...</NAME>.\n"
    "3. DEVI racchiudere la descrizione nel tag esatto <DESCRIPTION>...</DESCRIPTION>.\n"
    "4. Se non trovi dati online, usa la descrizione e il codice forniti nel DDT in modo pulito all'interno dei tag.\n"
    "5. IMPORTANTE: Scrivi l'output in italiano.\n"
    "6. Il nome e la descrizione devono essere COMPLETAMENTE NEUTRI rispetto al colore specifico. "
    "Non menzionare MAI colori nella NAME o nella DESCRIPTION.\n"
    "7. La taglia o le dimensioni DEVONO essere incluse nella DESCRIPTION se differenziano i prodotti "
    "(presta particolare attenzione nel caso in cui l'articolo che stai analizzando è una valigia,\n"
    " in questo caso dovrai includere nella descrizione se si tratta di un bagaglio a mano, o di un modello da stiva medio o grande)\n"
    "8. Descrivi le caratteristiche generali del modello, non di una variante cromatica specifica.\n"
    "9. I materiali devono essere menzionati SOLO se sono esplicitamente indicati nella fonte web trovata. "
    "Non dedurre, assumere o inventare materiali non citati esplicitamente nella pagina web."
)

_MODEL = "gemini-3.1-flash-lite"


def _parse_name_and_desc(raw_text: str, fallback_name: str, fallback_desc: str) -> tuple[str, str]:
    name_match = re.search(r"<NAME>(.*?)</NAME>", raw_text, re.DOTALL | re.IGNORECASE)
    desc_match = re.search(r"<DESCRIPTION>(.*?)</DESCRIPTION>", raw_text, re.DOTALL | re.IGNORECASE)

    name = name_match.group(1).strip() if name_match else fallback_name
    desc = desc_match.group(1).strip() if desc_match else fallback_desc
    return name, desc


async def _enrich_single_item(item: dict, brand: str, client: LLMClient) -> tuple[dict, list[str]]:
    vendor_code = item.get("vendor_code") or item.get("VendorCode") or ""
    description = item.get("description") or item.get("Description") or ""

    prompt = (
        f"Trova il nome ufficiale e la descrizione del prodotto nel catalogo o e-commerce del brand.\n\n"
        f"--- DATI DI PARTENZA ---\n"
        f"Brand: {brand}\n"
        f"Codice / Modello (VendorCode): {vendor_code}\n"
        f"Descrizione DDT: {description}\n\n"
        f"--- QUERY SUGGERITA ---\n"
        f'"{brand} {vendor_code}" OR "{brand} {description}"\n\n'
        f"Analizza i risultati web e restituisci il nome ufficiale dell'articolo all'interno di un tag <NAME>...</NAME> "
        f"e una descrizione chiara e accurata all'interno di un tag <DESCRIPTION>...</DESCRIPTION>."
    )

    fallback_name = description if description else f"{brand} {vendor_code}".strip()
    fallback_desc = description

    try:
        raw_text, _ = await client.call_with_grounding(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 3 - Web Search Extraction",
            max_output_tokens=512,
            temperature=0.0,
        )
        name, desc = _parse_name_and_desc(raw_text, fallback_name, fallback_desc)
        item_copy = dict(item)
        item_copy["vendor_code"] = vendor_code
        item_copy["quantity"] = item.get("quantity") or item.get("Quantity") or 0
        item_copy["article_name"] = name
        item_copy["article_description"] = desc
        return item_copy, []
    except Exception as exc:
        warning = f"[web_search_node] Item '{vendor_code}': web search failed ({exc}). Using fallback."
        print(warning)
        item_copy = dict(item)
        item_copy["vendor_code"] = vendor_code
        item_copy["quantity"] = item.get("quantity") or item.get("Quantity") or 0
        item_copy["article_name"] = fallback_name
        item_copy["article_description"] = fallback_desc
        return item_copy, [warning]


async def web_search_node(state: ExtractionGraphState) -> dict:
    """
    Perform concurrent web searches (temperature=0.0) for all extracted items to attach canonical article_name and article_description.
    """
    logger.log_agent("web_search_node", "node_entry", "ok", 
                     extracted_items=[{k: v for k, v in item.items() if k in ["vendor_code", "description"]} for item in state.extracted_items])
    print(f"[web_search_node] Running web search (temp=0.0) for {len(state.extracted_items)} item(s)...")
    client = LLMClient()
    tasks = [
        _enrich_single_item(item, state.brand, client)
        for item in state.extracted_items
    ]
    results = await asyncio.gather(*tasks)

    updated_items: list[dict] = []
    warnings: list[str] = []
    for item_copy, item_warnings in results:
        updated_items.append(item_copy)
        warnings.extend(item_warnings)

    if updated_items:
        updated_items[0]["_overwrite"] = True

    print("[web_search_node] Web search enrichment complete.")
    result = {"extracted_items": updated_items, "warnings": warnings}
    logger.log_agent("web_search_node", "node_exit", "ok", output=result)
    return result
