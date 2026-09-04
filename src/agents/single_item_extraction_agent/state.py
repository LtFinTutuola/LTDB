import operator
from typing import Annotated, List, Optional
from pydantic import BaseModel, Field

class SingleItemExtractionState(BaseModel):
    # Mandatory Inputs
    brand: str = Field(..., description="Brand name.")
    vendor_code: str = Field(..., description="Vendor code or model.")
    colors: List[str] = Field(..., description="User provided color hints.")
    
    # Optional Inputs
    barcode: Optional[str] = Field(default=None, description="Optional barcode.")
    quantity: int = Field(default=1, description="Quantity.")
    
    # Outputs
    extracted_item: Optional[dict] = Field(default=None, description="The normalized and enriched item.")
    warnings: Annotated[List[str], operator.add] = Field(default_factory=list, description="Non-fatal warnings.")
