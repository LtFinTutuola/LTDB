"""
nodes/blueprint_grouping_node.py
--------------------------------
Stage 4: Group items into blueprints using semantic embeddings.

Runs in the main graph after all parallel item extraction sub-graphs finish.
Buckets items by macro_category and uses cosine similarity on embeddings
to group identical base products, assigning a blueprint_group_id.
"""
import uuid
import asyncio
import numpy as np
from typing import List, Dict

from src.agents.llm_client import LLMClient, _load_gemini_config
from src.agents.data_ingestion_agent.state import GraphState


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    vec1 = np.array(v1)
    vec2 = np.array(v2)
    if np.linalg.norm(vec1) == 0 or np.linalg.norm(vec2) == 0:
        return 0.0
    return float(np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2)))


async def _embed_item(client: LLMClient, item: dict) -> dict:
    """Fetch embedding for a single item and attach it."""
    name = item.get("article_name", "")
    short_desc = item.get("product_short_description", "")
    text_to_embed = f"{name} {short_desc}".strip()
    
    if text_to_embed:
        try:
            embedding = await client.generate_embedding(text_to_embed)
            item["embedding"] = embedding
        except Exception as e:
            print(f"[blueprint_grouping_node] Warning: Failed to embed '{text_to_embed}': {e}")
            item["embedding"] = None
    else:
        item["embedding"] = None
    return item


async def blueprint_grouping_node(state: GraphState) -> dict:
    """
    Groups enriched items by calculating semantic similarity of their embeddings.
    """
    enriched_items = list(state.enriched_items)
    if not enriched_items:
        return {"enriched_items": []}

    # 1. Fetch embeddings in parallel
    client = LLMClient()
    embed_tasks = [_embed_item(client, item) for item in enriched_items]
    enriched_items = await asyncio.gather(*embed_tasks)

    # 2. Get similarity threshold from config
    config = _load_gemini_config()
    threshold = config.get("articles_similarity_threshold", 0.9)

    # 3. Bucket items by macro_category
    buckets: Dict[str, List[dict]] = {}
    for item in enriched_items:
        # category is stored as a CategoryNode dict or string depending on stage,
        # but in enriched_items it's a string (from ItemState.macro_category).
        cat = item.get("category")
        cat_name = cat if isinstance(cat, str) else str(cat)
        buckets.setdefault(cat_name, []).append(item)

    # 4. Group items within each bucket
    for cat_name, bucket_items in buckets.items():
        # A list of groups. Each group is a list of items.
        groups: List[List[dict]] = []

        for item in bucket_items:
            placed = False
            item_emb = item.get("embedding")
            
            if item_emb:
                # Try to place in an existing group by checking similarity with the group's first item
                for group in groups:
                    rep_item = group[0]
                    rep_emb = rep_item.get("embedding")
                    if rep_emb:
                        sim = cosine_similarity(item_emb, rep_emb)
                        if sim >= threshold:
                            group.append(item)
                            placed = True
                            break
            
            if not placed:
                groups.append([item])

        # 5. Assign a blueprint_group_id to each group and remove embedding vector from output
        for group in groups:
            group_id = str(uuid.uuid4())
            for item in group:
                item["blueprint_group_id"] = group_id
                item.pop("embedding", None)

    print(f"[blueprint_grouping_node] Grouped {len(enriched_items)} items into {len({item['blueprint_group_id'] for item in enriched_items})} blueprints using threshold {threshold}.")
    
    new_items = [dict(item, _overwrite=True) for item in enriched_items]
    return {"enriched_items": new_items}
