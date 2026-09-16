import asyncio
import re
from typing import List

from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()
_MODEL = "gemini-3.1-flash-lite"
MAX_CONCURRENT_SEARCHES = 10

_GROUNDING_REDIRECT_PREFIX = "https://vertexaisearch.cloud.google.com/"
_URL_PATTERN = re.compile(r'https?://[^\s\)\]\"\'>]+')

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


async def _search_photo_for_item(client: LLMClient, item: dict, brand_name: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        colors = item.get("colors", [])
        color_str = colors[0] if colors else "unknown"
        
        # Check context
        photo_ctx = item.get("_photo_search_ctx", [])
        
        prompt = (
            f"Cerca un'immagine del prodotto {brand_name} {item.get('article_name', '')} "
            f"nel colore {color_str}.\n"
            f"Rispondi ESCLUSIVAMENTE con l'URL diretto dell'immagine più rappresentativa del prodotto, "
            f"senza testo aggiuntivo, senza markdown, senza spiegazioni. "
            f"Restituisci solo l'URL grezzo, ad esempio: https://example.com/image.jpg"
        )
        
        if photo_ctx:
            # We are in retry
            context_str = "\n".join(f"{msg.get('role')}: {msg.get('content')}" for msg in photo_ctx)
            prompt = f"Previous context:\n{context_str}\n\n{prompt}"
            
        try:
            raw_res, extracted_urls = await client.call_with_grounding(
                model_name=_MODEL,
                system_prompt=(
                    "Sei un assistente specializzato nella ricerca di foto di prodotti. "
                    "Rispondi sempre e solo con l'URL grezzo dell'immagine, senza aggiungere altro testo."
                ),
                prompt=prompt,
                pipeline_stage="Stage 5 - Photo Search",
                temperature=0.2,
            )
            
            photo_url = _extract_best_url(extracted_urls, raw_res)
            
            new_ctx = list(photo_ctx)
            new_ctx.append({"role": "user", "content": prompt})
            new_ctx.append({"role": "model", "content": raw_res})
            
            return {
                "item_id": item.get("item_id"),
                "photo_url": photo_url,
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
    
    # gather all items that need photo
    items_to_process = []
    
    # 1. New blueprints items
    for bp in state.new_blueprints:
        for item in bp.get("cluster_items", []):
            items_to_process.append(item)
            
    # 2. Photo only items
    for item in state.photo_only_items:
        items_to_process.append(item)
        
    tasks = [
        _search_photo_for_item(client, item, state.brand_name, sem)
        for item in items_to_process
    ]
    
    proposals = await asyncio.gather(*tasks)
    
    result = {
        "photo_proposals": [p for p in proposals if p is not None]
    }
    
    logger.log_agent("color_photo_search_node", "node_exit", "ok", proposals_count=len(result["photo_proposals"]))
    return result
