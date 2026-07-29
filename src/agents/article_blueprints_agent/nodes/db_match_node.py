import numpy as np
from typing import List
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.core.logger import get_logger

logger = get_logger()


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    vec1 = np.array(v1)
    vec2 = np.array(v2)
    if np.linalg.norm(vec1) == 0 or np.linalg.norm(vec2) == 0:
        return 0.0
    return float(np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2)))


def db_match_node(state: BlueprintsGraphState) -> dict:
    """
    Match items against DB embeddings matrix using db_similarity_threshold.
    Records existing blueprints in output_blueprints without duplicates.
    """
    logger.log_agent("db_match_node", "node_entry", "ok", 
                     item_count=len(state.items), db_matrix_size=len(state.db_embeddings_matrix), threshold=state.db_similarity_threshold)
    matched: list[dict] = []
    unmatched: list[dict] = []
    output_blueprints: list[dict] = list(state.output_blueprints)
    seen_bp_ids = {bp["id"] for bp in output_blueprints}

    threshold = state.db_similarity_threshold
    matrix = state.db_embeddings_matrix

    print(f"[db_match_node] Comparing {len(state.items)} items against {len(matrix)} DB blueprints (threshold: {threshold})...")

    for item in state.items:
        item_emb = item.get("embedding")
        best_sim = -1.0
        best_match_id = None

        if item_emb and matrix:
            for db_entry in matrix:
                db_emb = db_entry.get("embedding")
                if db_emb:
                    sim = cosine_similarity(item_emb, db_emb)
                    if sim > best_sim:
                        best_sim = sim
                        best_match_id = db_entry.get("id")

        if best_sim >= threshold and best_match_id:
            item_copy = dict(item)
            item_copy["article_blueprint_id"] = best_match_id
            matched.append(item_copy)
            if best_match_id not in seen_bp_ids:
                output_blueprints.append({"id": best_match_id, "is_new": False})
                seen_bp_ids.add(best_match_id)
        else:
            unmatched.append(dict(item))

    print(f"[db_match_node] Matched: {len(matched)}, Unmatched: {len(unmatched)}.")
    result = {
        "matched_items": matched,
        "unmatched_items": unmatched,
        "output_blueprints": output_blueprints,
    }
    logger.log_agent("db_match_node", "node_exit", "ok", output=result)
    return result
