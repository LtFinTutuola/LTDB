from typing import List, Optional
from pydantic import BaseModel, Field


class PhotoSearchItemState(BaseModel):
    """
    Stato LangGraph per la ricerca fotografica 1-shot di una tupla (Blueprint, color)
    all'interno di `PhotoSearchAgent`.
    """

    item_id: str = ""
    brand_name: str = ""
    vendor_code: str = ""
    color: str = "unknown"
    article_name: str = ""
    description: str = ""
    user_feedback: Optional[str] = None
    previous_ctx: List[dict] = Field(default_factory=list)
    mode: str = "ingestion"  # "ingestion" | "retry" | "catalog" | "revision"

    # Stato interno dell'esecuzione lineare 1-shot
    current_query: str = ""
    candidate_url: Optional[str] = None
    validation_passed: bool = False
    working_ctx: List[dict] = Field(default_factory=list)

    # Output finale
    proposal: dict = Field(default_factory=dict)
