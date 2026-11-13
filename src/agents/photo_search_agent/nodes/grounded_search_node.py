"""
src/agents/photo_search_agent/nodes/grounded_search_node.py
-----------------------------------------------------------
Ricerca web grounded 1-shot con Google Search per `PhotoSearchAgent`,
ripristinata fedelmente alla logica e al prompt strict originali (HEAD).
"""
from __future__ import annotations

import re
from typing import Optional

from src.agents.llm_client import LLMClient
from src.agents.photo_search_agent.state import PhotoSearchItemState

_MODEL = "gemini-3.1-flash-lite"

_GROUNDING_REDIRECT_PREFIX = "https://vertexaisearch.cloud.google.com/"
_URL_PATTERN = re.compile(r'https?://[^\s\)\]\"\'>]+')

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


def extract_best_url(extracted_urls: list, raw_text: str) -> str | None:
    """
    Selects the best photo URL from the grounding response.
    Prefers a direct image URL parsed from the model's text output over the
    grounding redirect URLs, which are short-lived and cannot be downloaded later.
    """
    for match in _URL_PATTERN.finditer(raw_text or ""):
        url = match.group(0).rstrip(".")
        if not url.startswith(_GROUNDING_REDIRECT_PREFIX):
            lower = url.lower()
            if any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", "imwidth", "image", "/media", "/product", "/article")):
                return url

    for url in extracted_urls or []:
        if not url.startswith(_GROUNDING_REDIRECT_PREFIX):
            return url
    return extracted_urls[0] if extracted_urls else None


async def grounded_image_search_node(state: PhotoSearchItemState, client: Optional[LLMClient] = None) -> dict:
    llm = client or LLMClient()

    search_prompt = f"Esegui una ricerca su internet e trova una foto reale del prodotto usando questa query:\n{state.current_query}"

    if state.mode == "retry":
        stage_name = "Stage 5 - Photo Retry Grounded"
    elif state.mode == "catalog":
        stage_name = "Catalog Photo Search - Grounded"
    elif state.mode == "revision":
        stage_name = "Blueprint Revision - Photo Search Grounded"
    else:
        stage_name = "Stage 5 - Photo Search Grounded"

    raw_res, extracted_urls = await llm.call_with_grounding(
        model_name=_MODEL,
        system_prompt=_SYSTEM_PROMPT_SEARCH,
        prompt=search_prompt,
        pipeline_stage=stage_name,
        temperature=0.2,
    )

    raw_res_stripped = (raw_res or "").strip()
    candidate_url = None if raw_res_stripped.upper() == "NULL" else extract_best_url(extracted_urls, raw_res_stripped)

    return {
        "candidate_url": candidate_url,
    }
