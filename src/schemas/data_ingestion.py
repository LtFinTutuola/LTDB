from pydantic import BaseModel, Field
from typing import List, Optional, Any

class ExtractionRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to the PDF file on the server filesystem")
    brand_id: str = Field(..., description="Brand ID for the shipment document (must exist in the database)")

class DdtItemSchema(BaseModel):
    supplier_code: str
    description: str
    quantity: int

class CategoryRefSchema(BaseModel):
    id: str
    description: str

class EnrichedItemSchema(BaseModel):
    """Full enriched item schema returned by the DataIngestionAgent."""
    vendor_code: Optional[str] = Field(None, alias="VendorCode")
    barcode: Optional[str] = Field(None, alias="Barcode")
    quantity: Optional[int] = Field(None, alias="Quantity")
    category: Optional[CategoryRefSchema] = None
    sub_category: Optional[CategoryRefSchema] = None
    sex: Optional[str] = None
    materials: Optional[List[str]] = None
    colors: Optional[List[str]] = None
    article_name: Optional[str] = None
    product_short_description: Optional[str] = None
    product_extended_description: Optional[str] = None
    tags: Optional[List[str]] = None
    sources: Optional[List[str]] = None
    warnings: Optional[List[str]] = None
    blueprint_group_id: Optional[str] = None

class DdtExtractionResponse(BaseModel):
    items: List[DdtItemSchema]

class JobStatusResponse(BaseModel):
    status: str
    data: Optional[dict] = None

class StagingConfirmationRequest(BaseModel):
    items: Optional[List[EnrichedItemSchema]] = None

class ExtractedItemSchema(BaseModel):
    """Item schema returned by DataExtractionAgent."""
    item_id: str
    vendor_code: Optional[str] = Field(None, alias="VendorCode")
    barcode: Optional[str] = Field(None, alias="Barcode")
    quantity: Optional[int] = Field(None, alias="Quantity")
    colors: Optional[List[str]] = None
    description: Optional[str] = None
    article_name: Optional[str] = None
    article_description: Optional[str] = None

class BlueprintItemSchema(BaseModel):
    """Item schema returned by ArticleBlueprintsAgent linking to a blueprint."""
    item_id: str
    vendor_code: Optional[str] = Field(None, alias="VendorCode")
    quantity: Optional[int] = Field(None, alias="Quantity")
    colors: Optional[List[str]] = None
    article_blueprint_id: str

class BlueprintDefinitionSchema(BaseModel):
    """Blueprint definition returned by ArticleBlueprintsAgent."""
    id: str
    is_new: bool
    category: Optional[Any] = None
    sub_category: Optional[Any] = None
    article_name: Optional[str] = None
    description: Optional[str] = None
    extended_description: Optional[str] = None
    tags: Optional[List[str]] = None
    materials: Optional[List[str]] = None

class BipartiteIngestionResponse(BaseModel):
    items: List[BlueprintItemSchema]
    blueprints: List[BlueprintDefinitionSchema]
    warnings: Optional[List[str]] = None

