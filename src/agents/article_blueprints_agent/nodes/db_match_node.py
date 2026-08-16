"""
db_match_node.py
----------------
Matches incoming items against the existing DB blueprint embeddings using a
"Retrieve & Validate" two-phase approach.

Phase 1 — Candidate retrieval:
    All DB blueprints with cosine similarity >= (db_similarity_threshold - CANDIDATE_TOLERANCE)
    are collected as candidates for a given item.

Phase 2 — Branch on candidate count:
    0 candidates  → item routed to unmatched_items (forwarded to clustering).
    1 candidate   → direct match, no LLM call required.
    2+ candidates → LLM disambiguation via _disambiguate_via_llm.

When the LLM returns null (no confident match among the candidates), the item is
routed to unmatched_items rather than being discarded.
"""
import asyncio
import json
from typing import List

import numpy as np

from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()


_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT = (
    "Sei un AI Data Steward. Il tuo compito è risolvere un'ambiguità di associazione "
    "(Semantic Crowding) nel database articoli.\n\n"
    "Restituisci ESCLUSIVAMENTE un JSON con la chiave 'selected_blueprint_id' contenente "
    "l'ID del candidato corretto, oppure null se l'articolo non corrisponde ad alcun candidato."
)


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    vec1 = np.array(v1)
    vec2 = np.array(v2)
    if np.linalg.norm(vec1) == 0 or np.linalg.norm(vec2) == 0:
        return 0.0
    return float(np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2)))


async def _disambiguate_via_llm(
    client: LLMClient,
    item: dict,
    candidates: List[dict],
) -> str | None:
    """
    Ask the LLM to pick the correct blueprint among a list of candidates.

    Returns the selected blueprint ID string, or None if the LLM indicates
    that none of the candidates is a confident match.
    """
    name = item.get("article_name", "")
    desc = item.get("article_description", "") or item.get("description", "")
    dimensions = item.get("dimensions")

    candidate_lines = []
    for c in candidates:
        line = f"- ID: {c['id']} | Nome Blueprint: {c.get('article_name', '')} | Descrizione: {c.get('description', '')}"
        if c.get("dimensions"):
            line += f" | Dimensioni: {c.get('dimensions')}"
        candidate_lines.append(line)
    candidates_text = "\n".join(candidate_lines)

    article_details = f"- Nome: {name}\n- Descrizione: {desc}"
    if dimensions:
        article_details += f"\n- Dimensioni: {dimensions}"

    prompt = (
        f"**Articolo in Ingresso:**\n"
        f"{article_details}\n\n"
        f"**Blueprint Candidati dal Database:**\n"
        f"{candidates_text}\n\n"
        f"**TASK:**\n"
        f"Confronta l'articolo in ingresso con i candidati. Identifica se l'articolo corrisponde "
        f"esattamente a uno dei blueprint candidati, prestando estrema attenzione ai dettagli "
        f"strutturali e dimensionali (es. misure, capacità, taglia) se presenti.\n\n"
        f"Restituisci ESCLUSIVAMENTE un JSON con la chiave 'selected_blueprint_id' contenente "
        f"l'ID del candidato corretto. Se l'articolo presenta caratteristiche diverse "
        f"da TUTTI i candidati forniti, restituisci null come valore per questa chiave."
    )

    try:
        raw_res = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 4 - DB Disambiguation",
            response_mime_type="application/json",
            temperature=0.0,
        )
        parsed = json.loads(raw_res)
        return parsed.get("selected_blueprint_id")  # may be None
    except Exception as exc:
        print(f"[db_match_node] Warning: LLM disambiguation failed ({exc}). Routing item to unmatched.")
        return None


async def _process_item(
    client: LLMClient,
    item: dict,
    matrix: List[dict],
    threshold: float,
    candidate_threshold: float,
) -> tuple[dict | None, dict | None]:
    """
    Process a single item through the Retrieve & Validate pipeline.

    Returns:
        (matched_item, unmatched_item): exactly one of the two is non-None.
    """
    item_emb = item.get("embedding")

    if not item_emb or not matrix:
        return None, dict(item)

    # Phase 1 — collect candidates above the relaxed threshold.
    candidates: List[dict] = []
    for db_entry in matrix:
        db_emb = db_entry.get("embedding")
        if db_emb:
            sim = cosine_similarity(item_emb, db_emb)
            if sim >= candidate_threshold:
                candidates.append({**db_entry, "_sim": sim})

    logger.log_agent(
        "db_match_node",
        "db_similarity_computed",
        "ok",
        item_id=item.get("item_id"),
        candidate_count=len(candidates),
        candidate_threshold=candidate_threshold,
        strict_threshold=threshold,
    )

    # Phase 2 — branch on candidate count.
    if len(candidates) == 0:
        # No candidates at all → unmatched.
        return None, dict(item)

    # 1+ candidates — check if the top candidate already clears the strict threshold alone.
    candidates.sort(key=lambda c: c["_sim"], reverse=True)
    top = candidates[0]

    # If only one candidate crosses the strict threshold, no ambiguity.
    strict_candidates = [c for c in candidates if c["_sim"] >= threshold]
    if len(strict_candidates) == 1:
        item_copy = dict(item)
        item_copy["article_blueprint_id"] = strict_candidates[0]["id"]
        return item_copy, None

    # Genuine ambiguity (2+ candidates above strict threshold, or top below strict
    # but multiple near-matches, or exactly 1 candidate below strict) → LLM disambiguation.
    ambiguous = strict_candidates if strict_candidates else candidates
    selected_id = await _disambiguate_via_llm(client, item, ambiguous)

    if selected_id is not None:
        item_copy = dict(item)
        item_copy["article_blueprint_id"] = selected_id
        return item_copy, None
    else:
        # LLM returned null
        if len(candidates) == 1:
            # If the LLM explicitly rejected the only candidate we had, route to unmatched.
            return None, dict(item)

        # fallback to the most similar candidate
        top_fallback = ambiguous[0]
        logger.log_agent(
            "db_match_node", "unresolved_ambiguity", "warning",
            item_id=item.get("item_id"),
            fallback_candidate_id=top_fallback["id"]
        )
        print(f"[db_match_node] Warning: Unresolved ambiguity for item {item.get('item_id')}. Falling back to {top_fallback['id']}.")
        item_copy = dict(item)
        item_copy["article_blueprint_id"] = top_fallback["id"]
        return item_copy, None


async def db_match_node(state: BlueprintsGraphState) -> dict:
    """
    Retrieve & Validate DB matching node.

    Matches items against DB embeddings using a relaxed candidate threshold,
    then resolves ambiguity via LLM disambiguation for crowded semantic spaces.
    Items with no confident match are forwarded to unmatched_items for clustering.
    """
    threshold = state.db_similarity_threshold
    candidate_threshold = threshold - state.candidate_tolerance
    matrix = state.db_embeddings_matrix

    logger.log_agent(
        "db_match_node", "node_entry", "ok",
        item_count=len(state.items),
        db_matrix_size=len(matrix),
        strict_threshold=threshold,
        candidate_threshold=candidate_threshold,
    )
    print(
        f"[db_match_node] Matching {len(state.items)} item(s) against {len(matrix)} DB blueprints "
        f"(strict: {threshold}, candidate band: {candidate_threshold})..."
    )

    client = LLMClient()
    tasks = [
        _process_item(client, item, matrix, threshold, candidate_threshold)
        for item in state.items
    ]
    results = await asyncio.gather(*tasks)

    matched: list[dict] = []
    unmatched: list[dict] = []
    output_blueprints: list[dict] = list(state.output_blueprints)
    seen_bp_ids = {bp["id"] for bp in output_blueprints}

    for matched_item, unmatched_item in results:
        if matched_item is not None:
            matched.append(matched_item)
            bp_id = matched_item["article_blueprint_id"]
            if bp_id not in seen_bp_ids:
                output_blueprints.append({"id": bp_id, "is_new": False})
                seen_bp_ids.add(bp_id)
        else:
            unmatched.append(unmatched_item)

    print(f"[db_match_node] Matched: {len(matched)}, Unmatched: {len(unmatched)}.")
    result = {
        "matched_items": matched,
        "unmatched_items": unmatched,
        "output_blueprints": output_blueprints,
    }
    logger.log_agent("db_match_node", "node_exit", "ok", output=result)
    return result
