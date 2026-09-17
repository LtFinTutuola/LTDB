import asyncio
import re
import httpx
from typing import List

from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()
_MODEL = "gemini-3.1-flash-lite"
MAX_CONCURRENT_SEARCHES = 10

_SYSTEM_PROMPT_QUERY_GEN = (
    "Sei un esperto SEO e specialista nell'estrazione di immagini di prodotti e-commerce tramite motori di ricerca. "
    "Il tuo unico compito è generare la stringa di ricerca perfetta per trovare la foto di un prodotto, partendo dai suoi dati anagrafici.\n\n"
    "REGOLE TASSATIVE:\n"
    "1. Analizza i dettagli forniti e seleziona SOLO i termini essenziali per identificare il prodotto univocamente.\n"
    "2. DEVI SEMPRE INCLUDERE il Vendor Code (codice articolo) fornito, in quanto è l'identificativo primario per la ricerca.\n"
    "3. Seleziona il brand, il nome articolo e il colore di riferimento. Ignora descrizioni lunghe o concetti astratti.\n"
    "4. Aggiungi keyword di contesto come 'packshot', 'product shot' o 'white background' per favorire risultati da e-commerce puliti.\n"
    "5. Non usare alcun saluto, spiegazione o testo introduttivo.\n"
    "6. Non formattare la risposta in markdown (niente grassetto, niente virgolette o backticks).\n"
    "7. La risposta deve contenere ESCLUSIVAMENTE la stringa di ricerca grezza, pronta per essere immessa su Google."
)

_SYSTEM_PROMPT_SEARCH = (
    "Sei un agente autonomo specializzato nella ricerca di immagini (packshot/product shot) di articoli di moda e lusso su e-commerce. "
    "Riceverai in input una query di ricerca ottimizzata. Il tuo unico scopo è eseguire una ricerca web tramite i tuoi tool e restituire l'URL "
    "di un'immagine ad alta risoluzione che rispetti rigorosamente determinati vincoli visivi.\n\n"
    "REGOLE TASSATIVE DI COMPORTAMENTO:\n"
    "1. Usa la tua funzionalità di web search per trovare le immagini. NON inventare MAI URL basandoti sulla tua conoscenza interna.\n"
    "2. Rispondi ESCLUSIVAMENTE con l'URL grezzo dell'immagine trovata (es. https://sito.com/foto.jpg), senza testo extra, markdown o saluti.\n"
    "3. Se non trovi nessun risultato reale che rispetti le regole sottostanti, rispondi ESATTAMENTE con la parola: NULL.\n\n"
    "VINCOLI VISIVI OBBLIGATORI (L'IMMAGINE SARÀ SCARTATA SE NON LI RISPETTA):\n"
    "- Sfondo Neutro: L'immagine deve avere uno sfondo bianco, grigio chiaro o comunque neutro (stile e-commerce/still-life).\n"
    "- Soggetto Singolo: Deve essere presente SOLO l'articolo cercato. Non ammettere bundle o composizioni miste.\n"
    "- Nessun Modello/Indossato: Sono ASSOLUTAMENTE VIETATE foto con modelli umani, manichini, parti del corpo (es. mani che tengono una borsa), o foto in stile editoriale/streetwear.\n"
    "- Affidabilità: Privilegia immagini provenienti dal sito ufficiale del brand o da e-commerce riconosciuti (Farfetch, Zalando, Giglio, Luisaviaroma, ecc.). Evita marketplace dubbi."
)

_GROUNDING_REDIRECT_PREFIX = "https://vertexaisearch.cloud.google.com/"
_URL_PATTERN = re.compile(r'https?://[^\s\)\]\"\'>]+')


async def _validate_photo_url(url: str) -> bool:
    """Validates that a URL is reachable and points to an actual image.
    Returns False for any hallucinated, dead, or non-image URL.
    """
    if not url or url.upper() == "NULL":
        return False
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=4.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as res:
                if res.status_code >= 400:
                    return False
                ct = res.headers.get("content-type", "")
                return ct.startswith("image/")
    except Exception:
        return False

def _extract_best_url(extracted_urls: list, raw_text: str) -> str | None:
    """
    Selects the best photo URL from the grounding response.
    Prefers a direct image URL parsed from the model's text output over the
    grounding redirect URLs, which are short-lived and cannot be downloaded later.
    """
    # 1. Try to find a direct image URL from the free text (highest priority)
    for match in _URL_PATTERN.finditer(raw_text):
        url = match.group(0).rstrip('.')
        # Accept direct links that look like images and are NOT grounding redirects
        if not url.startswith(_GROUNDING_REDIRECT_PREFIX):
            lower = url.lower()
            if any(ext in lower for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', 'imwidth', 'image', '/media', '/product', '/article')):
                return url

    # 2. Fallback to grounding extracted_urls (will likely fail at download time, but kept as last resort)
    for url in extracted_urls:
        if not url.startswith(_GROUNDING_REDIRECT_PREFIX):
            return url
    return extracted_urls[0] if extracted_urls else None


async def _search_photo_for_item(client: LLMClient, item: dict, blueprint: dict, brand_name: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        colors = item.get("colors", [])
        color_str = colors[0] if colors else "unknown"
        
        photo_ctx = item.get("_photo_search_ctx", [])
        
        # --- Stage 1: Query Generation (No Grounding) ---
        bp_info = (
            f"Brand: {brand_name}\n"
            f"Article Name: {blueprint.get('article_name', item.get('article_name', ''))}\n"
            f"Vendor Code: {item.get('vendor_code', '')}\n"
            f"Color: {color_str}\n"
            f"Description: {blueprint.get('description', '')}"
        )
        
        query_prompt = f"Genera la query di ricerca ottimizzata per trovare la foto di questo prodotto. Devi assolutamente includere il Vendor Code nella stringa finale.\n\nDati Articolo:\n{bp_info}"
        
        try:
            generated_query = await client.call(
                model_name=_MODEL,
                system_prompt=_SYSTEM_PROMPT_QUERY_GEN,
                prompt=query_prompt,
                pipeline_stage="Stage 5 - Photo Query Gen",
                temperature=0.1
            )
            generated_query = generated_query.strip()
            
            # --- Stage 2: Grounded Search ---
            search_prompt = f"Esegui una ricerca su internet e trova una foto reale del prodotto usando questa query:\n{generated_query}"
            
            raw_res, extracted_urls = await client.call_with_grounding(
                model_name=_MODEL,
                system_prompt=_SYSTEM_PROMPT_SEARCH,
                prompt=search_prompt,
                pipeline_stage="Stage 5 - Photo Search Grounded",
                temperature=0.2,
            )
            
            raw_res_stripped = raw_res.strip()
            
            # URL Guard: extract best candidate then validate it
            candidate_url = None if raw_res_stripped.upper() == "NULL" else _extract_best_url(extracted_urls, raw_res_stripped)
            if candidate_url:
                is_valid = await _validate_photo_url(candidate_url)
                if not is_valid:
                    logger.log_agent("color_photo_search_node", "url_guard_rejected", "warn",
                                     rejected_url=candidate_url, item_id=item.get("item_id"))
                    candidate_url = None
            
            new_ctx = list(photo_ctx)
            new_ctx.append({"role": "user", "content": query_prompt})
            new_ctx.append({"role": "model", "content": generated_query})
            
            return {
                "item_id": item.get("item_id"),
                "photo_url": candidate_url,
                "photo_source": "web_search",
                "canonical_color_candidate": color_str.lower(),
                "retry_count": len(photo_ctx) // 2,
                "_photo_search_ctx": new_ctx
            }
        except Exception as exc:
            logger.log_agent("color_photo_search_node", "search_failed", "warn", exc=str(exc))
            return {
                "item_id": item.get("item_id"),
                "photo_url": None,
                "photo_source": "web_search",
                "canonical_color_candidate": color_str.lower(),
                "retry_count": 0,
                "_photo_search_ctx": photo_ctx
            }

async def color_photo_search_node(state: BlueprintsGraphState) -> dict:
    logger.log_agent("color_photo_search_node", "node_entry", "ok", 
                     new_blueprints_count=len(state.new_blueprints),
                     photo_only_items_count=len(state.photo_only_items))
    
    client = LLMClient()
    sem = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
    
    # Build a blueprint lookup map
    bp_map = {}
    for bp in state.new_blueprints:
        bp_map[bp.get("id")] = bp
    for bp_id, bp in state.resolved_blueprints.items():
        bp_map[bp_id] = bp
        
    items_to_process = []
    
    # 1. New blueprints items
    for bp in state.new_blueprints:
        for item in bp.get("cluster_items", []):
            items_to_process.append((item, bp))
            
    # 2. Photo only items
    for item in state.photo_only_items:
        bp_id = item.get("article_blueprint_id")
        bp = bp_map.get(bp_id, {})
        items_to_process.append((item, bp))
        
    tasks = [
        _search_photo_for_item(client, item, bp, state.brand_name, sem)
        for item, bp in items_to_process
    ]
    
    proposals = await asyncio.gather(*tasks)
    
    result = {
        "photo_proposals": [p for p in proposals if p is not None]
    }
    
    logger.log_agent("color_photo_search_node", "node_exit", "ok", proposals_count=len(result["photo_proposals"]))
    return result
