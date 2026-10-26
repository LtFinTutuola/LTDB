from __future__ import annotations
from pydantic import BaseModel, Field, model_validator, field_validator
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

    @field_validator("vendor_code", mode="before")
    @classmethod
    def clean_vendor_code_whitespace(cls, v: Any) -> Any:
        if isinstance(v, str):
            return "".join(v.split()).upper()
        return v

    @field_validator("barcode", mode="before")
    @classmethod
    def clean_barcode_whitespace(cls, v: Any) -> Any:
        if isinstance(v, str):
            cleaned = "".join(v.split())
            return cleaned or None
        return v


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


# ---------------------------------------------------------------------------
# Blueprint & Heuristic Revision Schemas
# ---------------------------------------------------------------------------

class BlueprintArticleGroupSchema(BaseModel):
    """Represents a homogeneous group of physical articles under a blueprint (grouped by supplier_code, colors, ean)."""
    group_key: str
    article_ids: List[str]
    supplier_code: str
    ean: Optional[str] = None
    colors: List[str] = Field(default_factory=list)
    quantity: int = 0
    available_stock: int = 0
    photo_id: Optional[str] = None
    photo_url: Optional[str] = None


class BlueprintRevisionContextResponse(BaseModel):
    """Context returned when opening the visual split/revision workspace for a blueprint."""
    blueprint_id: str
    article_name: str
    description: str
    extended_description: str
    brand_id: str
    brand_name: str
    normalized_vendor_code: Optional[str] = None
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    sub_category_name: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    materials: List[str] = Field(default_factory=list)
    dimensions: Optional[str] = None
    current_heuristic_id: Optional[str] = None
    current_heuristic_explanation: Optional[str] = None
    article_groups: List[BlueprintArticleGroupSchema] = Field(default_factory=list)


class PartitionGroupInput(BaseModel):
    """A bucket defined by the user in the visual Bucket Selector."""
    group_id: str
    label: Optional[str] = None
    is_original: bool = False
    article_ids: List[str] = Field(default_factory=list)
    supplier_codes: List[str] = Field(default_factory=list)
    user_hint: Optional[str] = None


class RuleDeductionRequest(BaseModel):
    """Request to deduce or refine the brand heuristic from visual partitions and optional user feedback."""
    blueprint_id: str
    job_id: Optional[str] = None
    partitions: List[PartitionGroupInput]
    user_correction: Optional[str] = None


class RuleDeductionResponse(BaseModel):
    """Response with plain-language explanation, examples, and impact summary (never exposes raw regex)."""
    job_id: str
    textual_explanation: str
    examples: List[HeuristicExampleSchema] = Field(default_factory=list)
    estimated_splits_count: int = 0
    estimated_merges_count: int = 0
    aligned_blueprints_count: int = 0


class ReconciliationPlanUpdateRequest(BaseModel):
    """Inline edit request for proposed blueprints in the 'Before & After' Staging review."""
    target_blueprint_id: str
    article_name: Optional[str] = None
    description: Optional[str] = None
    extended_description: Optional[str] = None
    category: Optional[Dict[str, str]] = None
    sub_category: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None
    materials: Optional[List[str]] = None
    dimensions: Optional[str] = None
    excluded: Optional[bool] = None


class BlueprintRevisionPhotoRetryRequest(BaseModel):
    """Request body for photo URL set or AI retry in the blueprint revision Before/After staging area."""
    target_blueprint_id: str = Field(..., description="Target blueprint ID in the After column")
    color: Optional[str] = Field(default=None, description="Optional color variant for the photo")
    user_feedback: Optional[str] = Field(default=None, description="Optional user feedback to guide AI retry")
    new_url: Optional[str] = Field(default=None, description="Direct external image URL provided by user")


class ManualSplitPrepareRequest(BaseModel):
    """Request to prepare the Staging Area 'Before & After' directly from manual partitions (skipping rule deduction)."""
    blueprint_id: str = Field(..., description="Primary blueprint ID being manually revised/split")
    source_blueprint_ids: Optional[List[str]] = Field(default=None, description="Optional list of blueprint IDs when resolving duplicate blueprints")
    partitions: List[PartitionGroupInput] = Field(..., min_length=1)
    job_id: Optional[str] = Field(default=None, description="Existing staging job ID if re-preparing")


class WarehouseAnomalyItemSchema(BaseModel):
    """Represents a single anomalous blueprint (or group of duplicate blueprints) detected in the warehouse."""
    anomaly_id: str
    anomaly_type: str = Field(..., description="'SPLIT_REQUIRED' or 'DUPLICATE_MODEL'")
    blueprint_id: str
    related_blueprint_ids: List[str] = Field(default_factory=list)
    article_name: str
    brand_id: str
    brand_name: str
    normalized_vendor_code: Optional[str] = None
    distinct_normalized_codes: List[str] = Field(default_factory=list)
    supplier_codes: List[str] = Field(default_factory=list)
    total_physical_units: int = 0
    photo_url: Optional[str] = None
    reason: str = Field(default="Anomalia di classificazione")


class WarehouseAnomaliesResponse(BaseModel):
    """Response body for GET /api/v1/brands/blueprint-revision/anomalies."""
    total_count: int = 0
    anomalies: List[WarehouseAnomalyItemSchema] = Field(default_factory=list)


class CloseAnomalyResponse(BaseModel):
    """Response body for POST /api/v1/brands/blueprint-revision/anomalies/{blueprint_id}/close."""
    success: bool
    message: str
    remaining_anomalies: int
    blueprint_id: str


