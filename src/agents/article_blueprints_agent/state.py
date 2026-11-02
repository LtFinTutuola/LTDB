from typing import List, Optional
from pydantic import BaseModel, Field


class BlueprintsGraphState(BaseModel):
    """Shared state for ArticleBlueprintsAgent pipeline."""
    # Input fields
    new_blueprints: List[dict] = Field(default_factory=list, description="Newly synthesized blueprint definitions (pre-grouped).")
    brand_id: Optional[str] = Field(default=None, description="Brand UUID for oracle & heuristic induction.")
    brand_name: str = Field(default="", description="The name of the brand for web search context.")
    categories: dict = Field(default_factory=dict, description="Brand category hierarchy.")
    resolved_blueprints: dict = Field(default_factory=dict, description="Existing blueprints fetched from DB.")
    brand_cohort: List[dict] = Field(default_factory=list, description="Existing DB blueprints cohort for the brand.")
    brand_heuristic: dict = Field(default_factory=dict, description="Current brand heuristic context (mode, rules, negative_constraints).")

    # Output accumulators
    output_items: List[dict] = Field(default_factory=list, description="Final formatted items list linking to blueprints.")
    output_blueprints: List[dict] = Field(default_factory=list, description="Final list of blueprints (existing and new).")
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings.")
    heuristic_reconciliations: List[dict] = Field(default_factory=list, description="Heuristic induction & HITL reconciliation proposals.")
    
    # Photo Search
    photo_only_items: List[dict] = Field(default_factory=list, description="Items con blueprint già noto ma senza foto. Solo color_photo_search_node li processa.")
    photo_proposals: List[dict] = Field(default_factory=list, description="Proposte fotografiche per ogni item processato (sia new che photo_only).")
