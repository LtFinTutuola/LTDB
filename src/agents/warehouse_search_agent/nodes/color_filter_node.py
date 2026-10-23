"""
src/agents/warehouse_search_agent/nodes/color_filter_node.py
------------------------------------------------------------
Node 3: Two-phase color filtering with:
  1. 24-hour decision memo cache for (target_color, candidate_color) pairs.
  2. Inclusive deterministic color dictionary evaluation.
  3. Lightweight LLM call on remaining unique colors returning 0-based indices.
  4. Strict zero-results policy on mismatch / LLM failure (no fallback passthrough).
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional

from src.agents.llm_client import LLMClient
from src.agents.warehouse_search_agent.state import (
    WarehouseSearchGraphState,
    ColorIndexResponse,
)
from src.core.cache import color_decision_cache
from src.core.logger import get_logger
from src.utils.color_matcher import filter_colors_by_text
from src.utils.text_normalize import fold_text

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT = """Sei un assistente AI specializzato nella selezione delle varianti colore di articoli moda/pelletteria in magazzino.
Il tuo compito è identificare quali dei colori a catalogo forniti sono associabili alla richiesta cromatica dell'utente.
Devi adottare una POLICY INCLUSIVA:
- Includi tonalità chiare o scure (es. per "blu": "dark navy", "harbor blue", "avio", "celeste", "denim").
- Includi sfumature affini (es. per "marrone": "cuoio", "cognac", "tan", "tabacco", "cioccolato", "dark brown").
- Includi denominazioni commerciali o in lingua inglese.
Restituisci unicamente un array di interi contenente gli indici a base 0 dei colori associabili."""


async def color_filter_node(state: WarehouseSearchGraphState) -> dict:
    """
    Filter candidate blueprints by requested color variant.
    """
    candidates = state.retrieved_blueprints or state.ranked_candidates or state.sql_candidates
    plan = state.query_plan
    target_color = fold_text(plan.color) if plan and plan.color else None

    logger.log_agent(
        "color_filter_node",
        "node_entry",
        "ok",
        candidates_count=len(candidates),
        target_color=target_color,
    )

    if not candidates:
        return {"selected_blueprints": [], "color_degraded": False}

    # Case A: No color specified in search plan -> passthrough all blueprints
    if not target_color:
        selected = [
            {
                "id": c["id"],
                "allowed_colors": None,  # None means all colors are allowed
                "relevance_score": c.get("relevance_score", 0.0),
                "similarity_score": c.get("similarity_score", 0.0),
                "blueprint": c.get("blueprint"),
            }
            for c in candidates
        ]
        return {"selected_blueprints": selected, "color_degraded": False}

    # Case B: Specific color requested
    distinct_colors: List[str] = sorted(
        list(
            {
                str(c).strip().lower()
                for bp in candidates
                for c in bp.get("available_colors", [])
                if c and str(c).strip()
            }
        )
    )

    if not distinct_colors:
        logger.log_agent("color_filter_node", "no_colors_available", "ok")
        return {"selected_blueprints": [], "color_degraded": False}

    decision_map: Dict[str, bool] = {}
    undecided_colors: List[str] = []

    # 1. Check in-memory 24h cache for each (target_color, candidate_color)
    for c in distinct_colors:
        cache_key = f"{target_color}::{c}"
        cached = color_decision_cache.get(cache_key)
        if cached is not None:
            decision_map[c] = bool(cached)
        else:
            undecided_colors.append(c)

    # 2. Consult deterministic inclusive color dictionary for undecided colors
    from src.utils.color_matcher import detect_color_intent

    still_undecided: List[str] = []
    for c in undecided_colors:
        matched, colors_list = filter_colors_by_text(target_color, [c])
        if matched and c in colors_list:
            decision_map[c] = True
            color_decision_cache.set(f"{target_color}::{c}", True)
        else:
            has_cand_intent, _, _ = detect_color_intent(c)
            if has_cand_intent:
                # Known distinct color family that does not match target family
                decision_map[c] = False
                color_decision_cache.set(f"{target_color}::{c}", False)
            else:
                # Unknown commercial shade or unindexed color: defer to LLM
                still_undecided.append(c)

    # 3. For any remaining unknown colors, call LLM with index-based prompt
    color_degraded = False
    if still_undecided:
        numbered_list = "\n".join([f"[{i}] {c}" for i, c in enumerate(still_undecided)])
        prompt = (
            f"Quali di questi colori a catalogo sono associabili al colore «{target_color}»?\n"
            f"Sii INCLUSIVO (considera tonalità chiare/scure, sfumature affini e denominazioni commerciali/inglesi compatibili).\n\n"
            f"Colori a catalogo:\n{numbered_list}"
        )

        try:
            client = LLMClient()
            raw_json = await client.call(
                model_name=_MODEL,
                system_prompt=_SYSTEM_PROMPT,
                prompt=prompt,
                pipeline_stage="search_color_filter",
                response_schema=ColorIndexResponse,
                response_mime_type="application/json",
                temperature=0.0,
            )
            index_res = ColorIndexResponse.model_validate_json(raw_json)
            valid_indices = {idx for idx in index_res.indices if 0 <= idx < len(still_undecided)}

            for i, c in enumerate(still_undecided):
                is_match = (i in valid_indices)
                decision_map[c] = is_match
                color_decision_cache.set(f"{target_color}::{c}", is_match)

        except Exception as exc:
            logger.log_agent("color_filter_node", "color_llm_error", "err", exc=exc, target_color=target_color)
            color_degraded = True
            for c in still_undecided:
                decision_map[c] = False

    # 4. Filter candidate blueprints to only those with at least one matching color
    selected_blueprints: List[Dict[str, Any]] = []
    for bp in candidates:
        bp_colors = [str(c).strip().lower() for c in bp.get("available_colors", []) if c]
        matched_colors = [c for c in bp_colors if decision_map.get(c) is True]
        if matched_colors:
            selected_blueprints.append(
                {
                    "id": bp["id"],
                    "allowed_colors": matched_colors,
                    "relevance_score": bp.get("relevance_score", 0.0),
                    "similarity_score": bp.get("similarity_score", 0.0),
                    "blueprint": bp.get("blueprint"),
                }
            )

    result = {
        "selected_blueprints": selected_blueprints,
        "color_degraded": color_degraded,
    }
    logger.log_agent(
        "color_filter_node",
        "node_exit",
        "ok",
        selected_count=len(selected_blueprints),
    )
    return result
