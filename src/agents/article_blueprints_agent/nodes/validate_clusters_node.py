"""
validate_clusters_node.py
-------------------------
Validates each candidate cluster produced by cluster_unmatched_node using an
LLM-as-a-Judge approach. For every cluster, the LLM decides whether all items
share the same structural blueprint identity or should be split into sub-groups.

When the LLM proposes a split, an embedding centroid cross-check is executed as a
soft guard: if sub-group centroids are still above articles_similarity_threshold, a
warning is appended to state.warnings, but the LLM's split decision is always
respected and preserved.

All cluster validation calls are dispatched concurrently via asyncio.gather.
"""
import asyncio
import json
import uuid
import numpy as np
from typing import List, Optional

from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.db_match_node import cosine_similarity
from src.core.logger import get_logger

logger = get_logger()

_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT = (
    "Sei un AI Data Steward specializzato in Master Data Management per il retail. "
    "Il tuo compito è validare raggruppamenti di articoli (cluster) per definire gli 'Article Blueprint'.\n\n"
    "**DEFINIZIONE DI ARTICLE BLUEPRINT:**\n"
    "Un 'Article Blueprint' rappresenta l'identità strutturale, funzionale e dimensionale di un prodotto. "
    "È il modello astratto di base.\n"
    "- Varianti puramente estetiche o di finitura (es. colore, pattern della stampa, piccole variazioni "
    "di materiale esterno) appartengono allo STESSO Blueprint.\n"
    "- Variazioni strutturali (es. taglie fisiche, capacità in litri, diametri, versioni 'Cabin' vs 'Large', "
    "genere Uomo/Donna) modificano l'identità del prodotto e richiedono Blueprint DIFFERENTI.\n\n"
    "Restituisci ESCLUSIVAMENTE un JSON strutturato come una lista di liste (array di array), "
    "dove ogni lista interna rappresenta un Blueprint valido contenente gli elementi ad esso associati."
)


def _compute_centroid(embeddings: List[List[float]]) -> List[float]:
    """Return the element-wise mean of a list of embedding vectors."""
    arr = np.array(embeddings, dtype=float)
    return arr.mean(axis=0).tolist()


def _build_cluster_prompt(cluster_items: List[dict]) -> str:
    """Serialise cluster items into the prompt payload (name + description only)."""
    lines = []
    for i, item in enumerate(cluster_items, start=1):
        name = item.get("article_name", "")
        desc = item.get("article_description", "") or item.get("description", "")
        lines.append(f"{i}. Nome: {name} | Descrizione: {desc}")
    items_text = "\n".join(lines)
    return (
        f"**TASK:**\n"
        f"Analizza il seguente gruppo di articoli candidati:\n"
        f"{items_text}\n\n"
        f"Se tutti gli articoli condividono la stessa identità strutturale, restituisci un unico cluster. "
        f"Se individui articoli che divergono nei requisiti strutturali del Blueprint, splitta il gruppo "
        f"in array separati.\n"
        f"Restituisci ESCLUSIVAMENTE un JSON strutturato come una lista di liste (array di array), "
        f"dove ogni lista interna rappresenta un Blueprint valido contenente gli elementi ad esso associati."
    )


def _check_centroid_similarity(
    sub_groups: List[List[dict]],
    threshold: float,
) -> Optional[str]:
    """
    Soft guard: compute pairwise centroid similarities between sub-groups.

    Returns a warning message string if any pair of sub-group centroids exceeds
    the threshold, else returns None.
    """
    centroids = []
    for group in sub_groups:
        valid_embs = [item["embedding"] for item in group if item.get("embedding")]
        if valid_embs:
            centroids.append(_compute_centroid(valid_embs))
        else:
            centroids.append(None)

    for i in range(len(centroids)):
        for j in range(i + 1, len(centroids)):
            ci, cj = centroids[i], centroids[j]
            if ci is not None and cj is not None:
                sim = cosine_similarity(ci, cj)
                if sim >= threshold:
                    return (
                        f"[validate_clusters_node] Soft-check warning: LLM proposed a split into "
                        f"{len(sub_groups)} sub-groups, but sub-group centroids {i} and {j} have "
                        f"cosine similarity {sim:.4f} >= threshold {threshold}. "
                        f"Split accepted as-is (potential LLM hallucination flagged)."
                    )
    return None


async def _validate_single_cluster(
    client: LLMClient,
    bp: dict,
    threshold: float,
) -> tuple[list[dict], list[str]]:
    """
    Validate a single candidate cluster against the LLM judge.

    Returns:
        (list[dict], list[str]): a list of confirmed blueprint dicts and a list
        of warning strings to append to state.warnings.
    """
    cluster_items = bp.get("cluster_items", [])
    warnings: list[str] = []

    # Clusters with 0 or 1 item cannot be split — pass through unchanged.
    if len(cluster_items) <= 1:
        return [bp], warnings

    prompt = _build_cluster_prompt(cluster_items)

    try:
        raw_res = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 5 - Cluster Validation",
            response_mime_type="application/json",
            temperature=0.0,
        )
        parsed = json.loads(raw_res)
    except Exception as exc:
        print(f"[validate_clusters_node] Warning: LLM validation failed ({exc}). Keeping cluster unchanged.")
        return [bp], warnings

    # Validate that the output is a non-empty list of lists.
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(sub, list) for sub in parsed)
    ):
        print(
            f"[validate_clusters_node] Warning: unexpected LLM output format. Keeping cluster unchanged. "
            f"Output: {parsed}"
        )
        return [bp], warnings

    # Single sub-group → cluster confirmed, preserve existing UUID.
    if len(parsed) == 1:
        return [bp], warnings

    # Multiple sub-groups → split proposed. Rebuild item dicts by matching on
    # index position within cluster_items (LLM echoes items by their order).
    sub_groups: list[list[dict]] = []
    used_indices: set[int] = set()

    for sub_list in parsed:
        sub_group: list[dict] = []
        for entry in sub_list:
            # The LLM may return the original text entries or integer indices.
            # We match by 1-based index if entry is an integer, otherwise by
            # searching for a name/desc substring match.
            if isinstance(entry, int):
                idx = entry - 1
                if 0 <= idx < len(cluster_items) and idx not in used_indices:
                    sub_group.append(cluster_items[idx])
                    used_indices.add(idx)
            elif isinstance(entry, str):
                for idx, item in enumerate(cluster_items):
                    if idx not in used_indices and (
                        entry in item.get("article_name", "")
                        or entry in item.get("article_description", "")
                        or entry in item.get("description", "")
                    ):
                        sub_group.append(item)
                        used_indices.add(idx)
                        break
            elif isinstance(entry, dict):
                # LLM returned dicts — match by article_name.
                name_key = entry.get("article_name") or entry.get("Nome", "")
                for idx, item in enumerate(cluster_items):
                    if idx not in used_indices and item.get("article_name") == name_key:
                        sub_group.append(item)
                        used_indices.add(idx)
                        break
        if sub_group:
            sub_groups.append(sub_group)

    # Recover any items the LLM omitted (safety net).
    omitted = [item for idx, item in enumerate(cluster_items) if idx not in used_indices]
    if omitted:
        if sub_groups:
            sub_groups[-1].extend(omitted)
        else:
            sub_groups.append(omitted)

    # Soft embedding cross-check.
    warning = _check_centroid_similarity(sub_groups, threshold)
    if warning:
        warnings.append(warning)
        print(warning)
        logger.log_agent("validate_clusters_node", "soft_check_warning", "warning", message=warning)
        # Hard guard: reject the split and keep the original cluster intact
        return [bp], warnings

    # Build confirmed blueprint dicts for each sub-group.
    confirmed_blueprints: list[dict] = []
    for sub_group in sub_groups:
        new_id = str(uuid.uuid4())
        for item in sub_group:
            item["article_blueprint_id"] = new_id
        confirmed_blueprints.append({
            "id": new_id,
            "is_new": True,
            "cluster_items": sub_group,
        })

    return confirmed_blueprints, warnings


async def validate_clusters_node(state: BlueprintsGraphState) -> dict:
    """
    LLM-as-a-Judge cluster validation node.

    Reads new_blueprints from state, validates each cluster via LLM, optionally
    splits clusters, and writes the confirmed blueprints back to new_blueprints.
    All validation calls run concurrently via asyncio.gather.
    """
    logger.log_agent(
        "validate_clusters_node", "node_entry", "ok",
        candidate_clusters=len(state.new_blueprints),
        threshold=state.articles_similarity_threshold,
    )
    print(f"[validate_clusters_node] Validating {len(state.new_blueprints)} candidate cluster(s)...")

    client = LLMClient()
    threshold = state.hallucination_recognition_threshold

    tasks = [
        _validate_single_cluster(client, bp, threshold)
        for bp in state.new_blueprints
    ]
    results = await asyncio.gather(*tasks)

    confirmed_blueprints: list[dict] = []
    all_warnings: list[str] = list(state.warnings)

    for bp_list, w_list in results:
        confirmed_blueprints.extend(bp_list)
        all_warnings.extend(w_list)

    print(
        f"[validate_clusters_node] Validation complete: "
        f"{len(state.new_blueprints)} candidate(s) → {len(confirmed_blueprints)} confirmed blueprint(s)."
    )

    result = {
        "new_blueprints": confirmed_blueprints,
        "warnings": all_warnings,
    }
    logger.log_agent("validate_clusters_node", "node_exit", "ok", output=result)
    return result
