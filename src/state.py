from typing import TypedDict, List, Optional, Any, Dict
from pydantic import BaseModel, Field

# -------------------------------------------------------------------
# Pydantic Models for LLM Structured Output and Validation
# -------------------------------------------------------------------

class LineItemModel(BaseModel):
    VendorCode: str = Field(description="The unique SKU or vendor code for the item.")
    Description: str = Field(description="The raw text description of the product.")
    Quantity: int = Field(description="The final quantity of the item, accounting for any dropped/storned items.")
    UnitPrice: Optional[float] = Field(default=None, description="The unit price of the item, if applicable.")
    ImplicitCategory: Optional[str] = Field(default=None, description="A first-pass categorization deduced from the text (e.g., identifying 'TRUNK' as underwear).")

class ExtractedDataModel(BaseModel):
    document_type: str = Field(description="The classified type of the document (e.g., 'Invoice', 'Delivery Note').")
    total_items_reported: Optional[int] = Field(default=None, description="The 'Total Items' printed at the bottom of the document, if available.")
    line_items: List[LineItemModel] = Field(description="The extracted product lines.")

# -------------------------------------------------------------------
# LangGraph State Definition
# -------------------------------------------------------------------

class AgentState(TypedDict):
    # API configuration
    api_key: str
    endpoint_url: str
    
    # Input config
    file_path: str
    file_format: str
    
    # Processed document content
    # For PDFs, this might be a base64 encoded string or file URI.
    # For Excel, this could be a pandas dataframe or markdown string.
    document_content: Any 
    
    # Orchestrator routing classification
    classification: str 
    
    # Worker extraction output
    extracted_data: Optional[Dict]  # Stored as dict for state, validated via Pydantic
    
    # Validation flags and errors
    schema_valid: bool
    logic_valid: bool
    validation_errors: List[str]
    
    # Final output payload
    final_output: Optional[Dict]
