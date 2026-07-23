from pydantic import BaseModel, Field
from typing import List, Optional

class ExtractionRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to the PDF file on the server filesystem")

class DdtItemSchema(BaseModel):
    supplier_code: str
    description: str
    quantity: int

class DdtExtractionResponse(BaseModel):
    items: List[DdtItemSchema]

class JobStatusResponse(BaseModel):
    status: str
    data: Optional[DdtExtractionResponse] = None

class StagingConfirmationRequest(BaseModel):
    job_id: str
    items: Optional[List[DdtItemSchema]] = None
