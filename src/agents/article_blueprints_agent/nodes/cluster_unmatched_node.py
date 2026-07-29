import uuid
import numpy as np
from typing import List
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.db_match_node import cosine_similarity
from src.core.logger import get_logger

logger = get_logger()


def _compute_centroid(embeddings: List[List[float]]) -> List[float]:
    """Return the element-wise mean of a list of embedding vectors."""
    arr = np.array(embeddings, dtype=float)
    return arr.mean(axis=0).tolist()


def cluster_unmatched_node(state: BlueprintsGraphState) -> dict:
    """
    Group unmatched items among themselves using articles_similarity_threshold.
    Uses centroid-based running means, best-match group assignment, and post-merge pass.
    Assigns a client-side uuid4 as article_blueprint_id to each cluster.
    """
    unmatched = list(state.unmatched_items)
    threshold = state.articles_similarity_threshold

    logger.log_agent("cluster_unmatched_node", "node_entry", "ok", 
                     unmatched_items=unmatched, threshold=threshold)
    print(f"[cluster_unmatched_node] Clustering {len(unmatched)} unmatched item(s) (threshold: {threshold})...")

    # Each group is a dict: {"items": [...], "centroid": [...]}
    groups: List[dict] = []

    for item in unmatched:
        item_emb = item.get("embedding")
        if not item_emb:
            # No embedding: always create its own group
            groups.append({"items": [item], "centroid": None})
            continue

        best_sim = -1.0
        best_idx = -1
        for idx, group in enumerate(groups):
            rep = group["centroid"]
            if rep is None:
                continue
            sim = cosine_similarity(item_emb, rep)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx

        if best_sim >= threshold and best_idx >= 0:
            groups[best_idx]["items"].append(item)
            # Update centroid incrementally
            valid_embs = [i["embedding"] for i in groups[best_idx]["items"] if i.get("embedding")]
            groups[best_idx]["centroid"] = _compute_centroid(valid_embs)
        else:
            groups.append({"items": [item], "centroid": item_emb})

    # Post-merge pass: merge groups whose centroids exceed the threshold
    merged = True
    iterations = 0
    while merged:
        merged = False
        iterations += 1
        new_groups = []
        absorbed = set()
        for i in range(len(groups)):
            if i in absorbed:
                continue
            current = groups[i]
            for j in range(i + 1, len(groups)):
                if j in absorbed:
                    continue
                ci = current["centroid"]
                cj = groups[j]["centroid"]
                if ci is not None and cj is not None:
                    if cosine_similarity(ci, cj) >= threshold:
                        current["items"].extend(groups[j]["items"])
                        valid_embs = [it["embedding"] for it in current["items"] if it.get("embedding")]
                        current["centroid"] = _compute_centroid(valid_embs) if valid_embs else None
                        absorbed.add(j)
                        merged = True
            new_groups.append(current)
        groups = new_groups

    # Assign blueprint UUIDs
    new_blueprints: list[dict] = []
    updated_unmatched: list[dict] = []
    for group in groups:
        cluster_id = str(uuid.uuid4())
        for item in group["items"]:
            item["article_blueprint_id"] = cluster_id
            updated_unmatched.append(item)
        new_blueprints.append({
            "id": cluster_id,
            "is_new": True,
            "cluster_items": group["items"],
        })

    print(f"[cluster_unmatched_node] Created {len(new_blueprints)} new blueprint cluster(s).")
    result = {
        "unmatched_items": updated_unmatched,
        "new_blueprints": new_blueprints,
    }
    logger.log_agent("cluster_unmatched_node", "node_exit", "ok", 
                     output=result, post_merge_iterations=iterations)
    return result

