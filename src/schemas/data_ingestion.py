from pydantic import BaseModel, Field
from typing import List, Optional

class ExtractionRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to the PDF file on the server filesystem")
    brand: str = Field(..., description="Brand name for the shipment document (must exist in the database)")

class DdtItemSchema(BaseModel):
    supplier_code: str
    description: str
    quantity: int

class EnrichedItemSchema(BaseModel):
    """Full enriched item schema returned by the DataIngestionAgent."""
    VendorCode: Optional[str] = None
    Barcode: Optional[str] = None
    Quantity: Optional[int] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    sex: Optional[str] = None
    materials: Optional[List[str]] = None
    colors: Optional[List[str]] = None
    product_name: Optional[str] = None
    product_short_description: Optional[str] = None
    product_extended_description: Optional[str] = None
    tags: Optional[List[str]] = None
    sources: Optional[List[str]] = None
    warnings: Optional[List[str]] = None

class DdtExtractionResponse(BaseModel):
    items: List[DdtItemSchema]

class JobStatusResponse(BaseModel):
    status: str
    data: Optional[dict] = None

class StagingConfirmationRequest(BaseModel):
    job_id: str
    items: Optional[List[DdtItemSchema]] = None
