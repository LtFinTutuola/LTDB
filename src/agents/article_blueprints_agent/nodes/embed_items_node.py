import asyncio
from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.state import BlueprintsGraphState


async def _embed_single_item(client: LLMClient, item: dict) -> dict:
    name = item.get("article_name") or ""
    desc = item.get("article_description") or item.get("description") or ""
    text_to_embed = f"{name} {desc}".strip()

    item_copy = dict(item)
    if text_to_embed:
        try:
            emb = await client.generate_embedding(text_to_embed)
            item_copy["embedding"] = emb
        except Exception as exc:
            print(f"[embed_items_node] Warning: Failed to embed '{text_to_embed}': {exc}")
            item_copy["embedding"] = None
    else:
        item_copy["embedding"] = None
    return item_copy


async def embed_items_node(state: BlueprintsGraphState) -> dict:
    """
    Batch generate embeddings for all items in parallel using asyncio.gather.
    """
    print(f"[embed_items_node] Generating embeddings for {len(state.items)} item(s)...")
    client = LLMClient()
    tasks = [_embed_single_item(client, item) for item in state.items]
    embedded_items = await asyncio.gather(*tasks)
    print("[embed_items_node] Embeddings generated.")
    return {"items": embedded_items}
