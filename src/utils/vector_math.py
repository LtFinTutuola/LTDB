"""
src/utils/vector_math.py
------------------------
Pure Python vector math utilities (cosine similarity, normalization).
"""
import math
from typing import List


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
