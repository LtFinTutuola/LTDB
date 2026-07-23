from pydantic import BaseModel, Field
from typing import List

class ExtractionRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to the PDF file on the server filesystem")

class DdtItemSchema(BaseModel):
    supplier_code: str
    description: str
    quantity: int

class DdtExtractionResponse(BaseModel):
    items: List[DdtItemSchema]

class StagingConfirmationRequest(BaseModel):
    job_id: str
    items: List[DdtItemSchema]
