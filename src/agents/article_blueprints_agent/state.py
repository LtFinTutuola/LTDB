from typing import List
from pydantic import BaseModel, Field


class BlueprintsGraphState(BaseModel):
    """Shared state for ArticleBlueprintsAgent pipeline."""
    # Input fields
    new_blueprints: List[dict] = Field(default_factory=list, description="Newly synthesized blueprint definitions (pre-grouped).")
    brand_name: str = Field(default="", description="The name of the brand for web search context.")
    categories: dict = Field(default_factory=dict, description="Brand category hierarchy.")

    # Output accumulators
    output_items: List[dict] = Field(default_factory=list, description="Final formatted items list linking to blueprints.")
    output_blueprints: List[dict] = Field(default_factory=list, description="Final list of blueprints (existing and new).")
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings.")
    
    # Photo Search
    photo_only_items: List[dict] = Field(default_factory=list, description="Items con blueprint già noto ma senza foto. Solo color_photo_search_node li processa.")
    photo_proposals: List[dict] = Field(default_factory=list, description="Proposte fotografiche per ogni item processato (sia new che photo_only).")
