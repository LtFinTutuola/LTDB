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
from src.core.logger import get_logger
from src.utils.llm_models import PHOTO_SEARCH_GROUNDED_MODEL as _MODEL
from src.utils.prompts import PHOTO_GROUNDED_SEARCH_SYSTEM_PROMPT

logger = get_logger()

_RETRY_TEMPERATURES = (0.2, 0.5, 0.8)

_GROUNDING_REDIRECT_PREFIX = "https://vertexaisearch.cloud.google.com/"
_URL_PATTERN = re.compile(r'https?://[^\s\)\]\"\'>]+')


def _has_grounding_evidence(client: LLMClient, extracted_urls: list[str]) -> bool:
    if extracted_urls:
        return True
    get_logs = getattr(client, "get_logs", None)
    logs = get_logs() if callable(get_logs) else None
    if isinstance(logs, list) and logs:
        last = logs[-1]
        if isinstance(last, dict):
            q_usage = last.get("query_usage")
            if isinstance(q_usage, int) and q_usage > 0:
                return True
            if last.get("grounding_detected") is True:
                return True
    return False


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

    search_prompt = (
        f"Esegui una ricerca su internet e trova una foto reale del prodotto usando questa query:\n"
        f"{state.current_query}\n\n"
        f"Riferimenti verifica -> Vendor Code: {state.vendor_code} | Colore richiesto: {state.color}"
    )
    barcode = (state.barcode or "").strip()
    has_distinct_barcode = bool(barcode and barcode != state.vendor_code)
    barcode_prompt = (
        f"Esegui una ricerca su internet e trova una foto reale del prodotto usando il codice a barre (EAN):\n"
        f"{state.brand_name} {barcode} {state.color}\n\n"
        f"Riferimenti verifica -> Barcode EAN: {barcode} | Articolo: {state.article_name} | Colore richiesto: {state.color}"
        if has_distinct_barcode
        else None
    )

    if state.mode == "retry":
        stage_name = "Stage 5 - Photo Retry Grounded"
    elif state.mode == "catalog":
        stage_name = "Catalog Photo Search - Grounded"
    elif state.mode == "revision":
        stage_name = "Blueprint Revision - Photo Search Grounded"
    else:
        stage_name = "Stage 5 - Photo Search Grounded"

    candidate_url: str | None = None
    for attempt_idx, temp in enumerate(_RETRY_TEMPERATURES, start=1):
        use_barcode = attempt_idx == 2 and barcode_prompt is not None
        prompt_to_send = barcode_prompt if use_barcode else search_prompt
        if attempt_idx > 1 and not use_barcode:
            prompt_to_send = (
                f"{search_prompt}\n\n"
                f"NOTA: Nel tentativo precedente non hai eseguito la ricerca web. "
                f"Devi OBBLIGATORIAMENTE usare il tool Google Search prima di rispondere."
            )

        raw_res, extracted_urls = await llm.call_with_grounding(
            model_name=_MODEL,
            system_prompt=PHOTO_GROUNDED_SEARCH_SYSTEM_PROMPT,
            prompt=prompt_to_send,
            pipeline_stage=stage_name,
            max_output_tokens=4096,
            temperature=temp,
            thinking_level="LOW",
        )

        raw_res_stripped = (raw_res or "").strip()
        candidate_url = None if raw_res_stripped.upper() == "NULL" else extract_best_url(extracted_urls, raw_res_stripped)

        if candidate_url is not None:
            break

        has_grounding = _has_grounding_evidence(llm, extracted_urls)
        if has_grounding:
            if attempt_idx == 1 and barcode_prompt is not None:
                logger.log_agent(
                    "grounded_search_node",
                    "not_found_retry_with_barcode",
                    "warning",
                    vendor_code=state.vendor_code,
                    barcode=barcode,
                    attempt=attempt_idx,
                    temperature=temp,
                )
                continue
            break

        logger.log_agent(
            "grounded_search_node",
            "grounding_missing_retry",
            "warning",
            vendor_code=state.vendor_code,
            attempt=attempt_idx,
            temperature=temp,
        )

    return {
        "candidate_url": candidate_url,
    }
