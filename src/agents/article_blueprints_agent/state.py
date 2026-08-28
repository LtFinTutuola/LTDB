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
