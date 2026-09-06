from typing import List, Optional
from pydantic import BaseModel, Field


class CodesDeductionGraphState(BaseModel):
    """Shared state for CodesDeductionAgent pipeline."""

    # Input fields
    vendor_codes: List[str] = Field(
        default_factory=list,
        description="Corpus of raw vendor codes to analyze for pattern discovery."
    )
    brand_name: str = Field(
        default="",
        description="Brand name for contextual prompting."
    )
    previous_explanation: Optional[str] = Field(
        default=None,
        description="Existing heuristic explanation for recalculation context."
    )

    # Internal state (populated by nodes)
    analysis_result: str = Field(
        default="",
        description="Natural language pattern description from the LLM."
    )
    candidate_regex: str = Field(
        default="",
        description="Proposed regex string from the synthesis node."
    )
    validation_failures: List[str] = Field(
        default_factory=list,
        description="Vendor codes that failed regex validation (used for retry context)."
    )
    iteration: int = Field(
        default=0,
        description="Current retry iteration count (max 3)."
    )
    max_iterations: int = Field(
        default=3,
        description="Maximum retry iterations before reporting failure."
    )

    # Output fields
    success: bool = Field(
        default=False,
        description="Whether the deduction succeeded."
    )
    output_regex: str = Field(
        default="",
        description="Final confirmed regex pattern."
    )
    output_explanation: str = Field(
        default="",
        description="Final plain-text explanation of the encoding rule."
    )
    output_examples: List[dict] = Field(
        default_factory=list,
        description="Before/after transformation examples."
    )
    error_message: str = Field(
        default="",
        description="Error message if deduction failed after max retries."
    )
