from typing import List, Optional, Any
from pydantic import BaseModel, Field


class BlueprintsGraphState(BaseModel):
    """Shared state for ArticleBlueprintsAgent pipeline."""
    # Input fields
    items: List[dict] = Field(default_factory=list, description="Extracted items from DataExtractionAgent.")
    categories: dict = Field(default_factory=dict, description="Brand category hierarchy.")
    db_embeddings_matrix: List[dict] = Field(
        default_factory=list, description="Existing DB blueprint embeddings for the brand."
    )
    db_similarity_threshold: float = Field(default=0.92, description="Cosine similarity threshold for DB matching.")
    articles_similarity_threshold: float = Field(default=0.88, description="Cosine similarity threshold for intra-DDT clustering.")

    # Internal buckets
    matched_items: List[dict] = Field(default_factory=list, description="Items matched to an existing DB blueprint.")
    unmatched_items: List[dict] = Field(default_factory=list, description="Items that did not match DB blueprints.")
    new_blueprints: List[dict] = Field(default_factory=list, description="Newly synthesized blueprint definitions.")

    # Output accumulators
    output_items: List[dict] = Field(default_factory=list, description="Final formatted items list linking to blueprints.")
    output_blueprints: List[dict] = Field(default_factory=list, description="Final list of blueprints (existing and new).")
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings.")
