"""
src/agents/warehouse_search_agent/nodes/color_filter_node.py
------------------------------------------------------------
Node 4: Evaluates color compatibility via a dedicated LLM call.
Receives the filtered query text (same text used for embedding) and the available colors
for each selected ArticleBlueprint (grouped by blueprint), returning the appropriate colors.
"""
import json
from typing import Dict, List, Any

from src.agents.llm_client import LLMClient
from src.agents.warehouse_search_agent.state import (
    WarehouseSearchGraphState,
    ColorFilterResponse,
)
from src.core.logger import get_logger

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT = """Sei un assistente AI specializzato nella selezione delle varianti colore di articoli moda/pelletteria in magazzino.
Ti vengono forniti:
1. Il testo della ricerca dell'utente (`filtered_text`).
2. La lista degli articoli candidati selezionati, ciascuno con il proprio `blueprint_id`, `article_name` e la lista `available_colors` (i nomi dei colori effettivamente salvati nel database per quell'articolo, spesso in inglese o con nomi commerciali come "black", "astral red", "bone town", "dark navy", "harbor blue").

Il tuo compito è restituire un JSON conforme allo schema `ColorFilterResponse`:
- `has_color_constraint`:
  - Imposta `true` SE E SOLO SE `filtered_text` contiene un riferimento esplicito o implicito a un colore o tonalità (es. "nera", "rosso", "blu", "chiara", "scura", "beige", "verde", "black").
  - Imposta `false` se `filtered_text` NON menziona alcun colore (es. "con la tracolla", "sportiva adatta a tutti i giorni", "in pelle").
- `selections`:
  - Se `has_color_constraint` è `false`, restituisci per ogni articolo tutti i suoi `available_colors`.
  - Se `has_color_constraint` è `true`, per ogni articolo seleziona in `selected_colors` SOLO i colori presenti in `available_colors` che sono semanticamente e cromaticamente compatibili con il colore richiesto (es. se il testo chiede "rossa" e `available_colors` contiene `["black", "astral red"]`, restituisci `["astral red"]`; se chiede "blu" e `available_colors` ha `["dark navy", "harbor blue", "black"]`, restituisci `["dark navy", "harbor blue"]`).
  - Se per un dato articolo nessuno dei suoi `available_colors` è compatibile con il colore richiesto, restituisci `selected_colors: []` per quel `blueprint_id`.
"""


def _passthrough_all_colors(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": c["id"],
            "allowed_colors": None,  # None means all color variants of this blueprint are allowed
            "similarity_score": c.get("similarity_score"),
        }
        for c in candidates
    ]


async def color_filter_node(state: WarehouseSearchGraphState) -> dict:
    """
    Call the LLM to filter each candidate blueprint's color variants based on the filtered search text.
    """
    candidates = state.ranked_candidates
    plan = state.query_plan
    has_static_filters = bool(plan and (plan.brand_name or plan.category_name))
    if has_static_filters and not state.fallback_triggered:
        filtered_text = (plan.semantic_query or "").strip()
    else:
        filtered_text = ((plan.semantic_query if plan and plan.semantic_query else state.raw_query) or "").strip()

    logger.log_agent(
        "color_filter_node",
        "node_entry",
        "ok",
        candidates_count=len(candidates),
        filtered_text=filtered_text,
    )

    if not candidates:
        result = {"selected_blueprints": []}
        logger.log_agent("color_filter_node", "node_exit", "ok", output=result)
        return result

    # If there is no filtered text or no candidate has any colors stored, keep all variants
    has_any_colors = any(bool(c.get("available_colors")) for c in candidates)
    if not filtered_text or not has_any_colors:
        selected = _passthrough_all_colors(candidates)
        result = {"selected_blueprints": selected}
        logger.log_agent(
            "color_filter_node",
            "node_exit",
            "ok",
            output={"mode": "passthrough_no_color_context", "selected_count": len(selected)},
        )
        return result

    grouped_payload = [
        {
            "blueprint_id": c["id"],
            "article_name": c.get("article_name", ""),
            "available_colors": c.get("available_colors", []),
        }
        for c in candidates
    ]

    user_prompt = (
        f"Testo ricerca filtrato (`filtered_text`): \"{filtered_text}\"\n\n"
        f"Articoli candidati e colori disponibili a database:\n"
        f"{json.dumps(grouped_payload, ensure_ascii=False, indent=2)}\n\n"
        f"Seleziona i colori appropriati per ciascun articolo."
    )

    try:
        client = LLMClient()
        raw_json = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=user_prompt,
            pipeline_stage="search_color_filter",
            response_schema=ColorFilterResponse,
            response_mime_type="application/json",
            temperature=0.0,
        )
        color_res = ColorFilterResponse.model_validate_json(raw_json)

        if not color_res.has_color_constraint:
            selected = _passthrough_all_colors(candidates)
            result = {"selected_blueprints": selected}
            logger.log_agent(
                "color_filter_node",
                "node_exit",
                "ok",
                output={"has_color_constraint": False, "selected_count": len(selected)},
            )
            return result

        selection_map: Dict[str, List[str]] = {
            sel.blueprint_id: [c.lower() for c in sel.selected_colors if c]
            for sel in color_res.selections
        }

        filtered_blueprints: List[Dict[str, Any]] = []
        for c in candidates:
            bp_id = c["id"]
            chosen_colors = selection_map.get(bp_id, [])
            if chosen_colors:
                filtered_blueprints.append(
                    {
                        "id": bp_id,
                        "allowed_colors": chosen_colors,
                        "similarity_score": c.get("similarity_score"),
                    }
                )

        # Anti-empty safety: if color constraint was detected but 0 blueprints matched the color,
        # keep only blueprints if none matched? No, if none matched the requested color,
        # fallback to showing all variants of the top ranked candidates so we don't return 0 results unnecessarily,
        # UNLESS the user explicitly wants only matching colors when at least one matches.
        if not filtered_blueprints:
            selected = _passthrough_all_colors(candidates)
        else:
            selected = filtered_blueprints

    except Exception as exc:
        logger.log_agent("color_filter_node", "color_llm_error", "err", exc=exc)
        selected = _passthrough_all_colors(candidates)

    result = {"selected_blueprints": selected}
    logger.log_agent(
        "color_filter_node",
        "node_exit",
        "ok",
        output={"selected_count": len(selected), "selected_blueprints": selected},
    )
    return result
