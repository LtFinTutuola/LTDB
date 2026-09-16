from __future__ import annotations
from pydantic import BaseModel, Field, model_validator
from typing import List, Literal, Optional, Any, Dict

from src.schemas.types import NormalizedIdentifier, NormalizedStringList

class ExtractionRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to the PDF file on the server filesystem")
    brand_id: str = Field(..., description="Brand ID for the shipment document (must exist in the database)")

class DdtItemSchema(BaseModel):
    supplier_code: NormalizedIdentifier
    description: str
    quantity: int

class CategoryRefSchema(BaseModel):
    id: str
    description: str

class EnrichedItemSchema(BaseModel):
    """Full enriched item schema returned by the DataIngestionAgent."""
    vendor_code: Optional[NormalizedIdentifier] = None
    normalized_vendor_code: Optional[str] = None
    barcode: Optional[str] = None
    quantity: Optional[int] = None
    category: Optional[CategoryRefSchema] = None
    sub_category: Optional[CategoryRefSchema] = None
    sex: Optional[str] = None
    materials: Optional[NormalizedStringList] = None
    dimensions: Optional[str] = None
    colors: Optional[NormalizedStringList] = None
    article_name: Optional[str] = None
    product_short_description: Optional[str] = None
    product_extended_description: Optional[str] = None
    tags: Optional[NormalizedStringList] = None
    sources: Optional[List[str]] = None
    warnings: Optional[List[str]] = None
    blueprint_group_id: Optional[str] = None

class DdtExtractionResponse(BaseModel):
    items: List[DdtItemSchema]

class JobStatusResponse(BaseModel):
    status: str
    data: Optional[dict] = None

class BlueprintDefinitionSchema(BaseModel):
    """Blueprint definition returned by ArticleBlueprintsAgent."""
    id: str
    is_new: bool
    category: Optional[Any] = None
    sub_category: Optional[Any] = None
    article_name: Optional[str] = None
    description: Optional[str] = None
    extended_description: Optional[str] = None
    tags: Optional[NormalizedStringList] = None
    materials: Optional[NormalizedStringList] = None
    dimensions: Optional[str] = None

class ExtractedItemSchema(BaseModel):
    """Item schema returned by DataExtractionAgent."""
    item_id: str
    vendor_code: Optional[NormalizedIdentifier] = None
    barcode: Optional[str] = None
    quantity: Optional[int] = None
    colors: Optional[NormalizedStringList] = None
    description: Optional[str] = None
    article_name: Optional[str] = None
    article_description: Optional[str] = None

class BlueprintItemSchema(BaseModel):
    """Item schema returned by ArticleBlueprintsAgent linking to a blueprint."""
    item_id: str
    vendor_code: Optional[NormalizedIdentifier] = None
    barcode: Optional[str] = None
    quantity: Optional[int] = None
    colors: Optional[NormalizedStringList] = None
    article_blueprint_id: str

class BipartiteIngestionResponse(BaseModel):
    items: List[BlueprintItemSchema]
    blueprints: List[BlueprintDefinitionSchema]
    warnings: Optional[List[str]] = None

class SingleItemIngestionRequest(BaseModel):
    brand_id: str = Field(..., description="Brand ID for the item")
    vendor_code: NormalizedIdentifier = Field(..., description="Vendor code or model")
    article_name: Optional[str] = Field(default=None, description="Article name or description")
    barcode: Optional[str] = Field(default=None, description="Barcode or EAN")
    quantity: Optional[int] = Field(default=1, description="Item quantity")
    colors: NormalizedStringList = Field(..., description="Article colors hint for web search")


# ---------------------------------------------------------------------------
# Human-in-the-Loop Staging Revision Schemas
# ---------------------------------------------------------------------------

class CreateBlueprintPayload(BaseModel):
    """Payload for the create_blueprint operation with strict validation."""
    article_name: str
    description: str
    extended_description: str
    category: CategoryRefSchema
    tags: NormalizedStringList
    materials: NormalizedStringList
    sub_category: Optional[CategoryRefSchema] = None
    dimensions: Optional[str] = None

    @model_validator(mode="after")
    def validate_non_empty_lists(self) -> "CreateBlueprintPayload":
        errors = []
        if not self.tags:
            errors.append("'tags' must be a non-empty list")
        if not self.materials:
            errors.append("'materials' must be a non-empty list")
        if errors:
            raise ValueError("; ".join(errors))
        return self


class RevisionOperation(BaseModel):
    """A single revision operation to apply to staged data."""
    op: Literal[
        "update_item", "update_blueprint", "reassign_item",
        "create_blueprint", "merge_blueprints", "split_blueprint", "delete_item"
    ]
    # Conditionally required fields (validated in service layer based on op type):
    item_id: Optional[str] = None
    item_ids: Optional[List[str]] = None
    blueprint_id: Optional[str] = None
    fields: Optional[dict] = None
    target_blueprint_id: Optional[str] = None
    blueprint: Optional[CreateBlueprintPayload] = None
    source_blueprint_ids: Optional[List[str]] = None
    source_blueprint_id: Optional[str] = None
    blueprint_overrides: Optional[dict] = None


class StagingRevisionRequest(BaseModel):
    """Request body for PUT /api/v1/ingestion/staging/{job_id}."""
    operations: List[RevisionOperation]


class StagingRevisionResponse(BaseModel):
    """Response body for PUT /api/v1/ingestion/staging/{job_id}."""
    status: str
    data: dict

class PhotoRetryRequest(BaseModel):
    """Request body for POST /api/v1/ingestion/photo-retry/{job_id}."""
    item_id: str = Field(..., description="ID dell'item per cui ritentare la ricerca foto")
    model_ok: bool = Field(..., description="True se il modello trovato è corretto")
    color_ok: bool = Field(..., description="True se il colore trovato è corretto")
    user_feedback: Optional[str] = Field(default=None, description="Feedback testuale dell'utente")
    new_url: Optional[str] = Field(default=None, description="Short-circuit: URL diretto fornito dall'utente. Bypassa l'agente.")


# ---------------------------------------------------------------------------
# Heuristic Deduction Schemas
# ---------------------------------------------------------------------------

class HeuristicDeductionRequest(BaseModel):
    """Request body for POST /api/v1/brands/{brand_id}/heuristic."""
    file_path: str = Field(..., description="Absolute path to a sample PDF for pattern discovery")

class HeuristicExampleSchema(BaseModel):
    """A single before/after transformation example."""
    raw: str
    normalized: str

class HeuristicProposalResponse(BaseModel):
    """Proposed heuristic data returned by the deduction agent."""
    textual_explanation: str
    grouped_items: Dict[str, List[Dict[str, Any]]]

class HeuristicJobStatusResponse(BaseModel):
    """Response body for GET /api/v1/brands/{brand_id}/heuristic/{job_id}."""
    status: str
    data: Optional[dict] = None
