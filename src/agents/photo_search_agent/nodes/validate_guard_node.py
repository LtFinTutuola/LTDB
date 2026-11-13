"""
src/agents/photo_search_agent/nodes/validate_guard_node.py
----------------------------------------------------------
Validazione HTTP & Content-Type (Livello 1 originale di HEAD) per `PhotoSearchAgent`.
"""
from __future__ import annotations

import httpx
from src.agents.photo_search_agent.state import PhotoSearchItemState
from src.core.logger import get_logger

logger = get_logger()


async def _validate_photo_url(url: str) -> bool:
    """Validates that a URL is reachable and points to an actual image.
    Returns False for any hallucinated, dead, or non-image URL.
    """
    if not url or url.upper() == "NULL":
        return False
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=4.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as res:
                if res.status_code >= 400:
                    return False
                ct = res.headers.get("content-type", "")
                return ct.startswith("image/")
    except Exception:
        return False


async def validate_candidate_node(state: PhotoSearchItemState) -> dict:
    candidate = state.candidate_url
    if not candidate:
        return {
            "validation_passed": False,
            "candidate_url": None,
        }

    is_valid = await _validate_photo_url(candidate)
    if not is_valid:
        logger.log_agent(
            "photo_search_agent",
            "url_guard_rejected",
            "warn",
            rejected_url=candidate,
            item_id=state.item_id,
        )
        return {
            "validation_passed": False,
            "candidate_url": None,
        }

    return {
        "validation_passed": True,
        "candidate_url": candidate,
    }
