import operator
from typing import Annotated, List, Optional
from pydantic import BaseModel, Field


def reduce_list(left: List[dict], right: List[dict]) -> List[dict]:
    """Reducer for list of dictionaries. Allows overwrite if _overwrite flag is present."""
    if right and isinstance(right, list) and len(right) > 0 and right[0].get("_overwrite"):
        for item in right:
            item.pop("_overwrite", None)
        return right
    return left + right


class ExtractionGraphState(BaseModel):
    """Shared state for DataExtractionAgent pipeline."""
    file_path: str = Field(..., description="Absolute path to the PDF file to process.")
    brand: str = Field(..., description="Brand name for the shipment document.")
    raw_text: str = Field(default="", description="Raw text extracted from PDF.")
    cleaned_text: str = Field(default="", description="Cleaned text after LLM noise removal.")
    extracted_items: Annotated[List[dict], reduce_list] = Field(
        default_factory=list, description="Extracted and web-search grounded items."
    )
    warnings: Annotated[List[str], operator.add] = Field(
        default_factory=list, description="Non-fatal warnings."
    )
    brand_code_heuristic: str = Field(..., description="The brand's heuristic regex to validate vendor codes")
    validation_passed: bool = Field(default=False)
    extraction_retries: int = Field(default=0)
    extraction_errors: Annotated[List[str], operator.add] = Field(
        default_factory=list, description="Errors from the validation node fed back to the extraction node."
    )
