from typing import Annotated, List, Any
from pydantic import BeforeValidator

def to_upper_and_strip(val: Any) -> Any:
    if isinstance(val, str):
        return val.strip().upper()
    return val

def to_lower_and_dedup_list(val: Any) -> Any:
    if isinstance(val, list):
        # Strip and lower each item if it's a string, ignore empty strings
        processed = [
            item.strip().lower() for item in val 
            if isinstance(item, str) and item.strip()
        ]
        # Order-preserving deduplication
        return list(dict.fromkeys(processed))
    return val

NormalizedIdentifier = Annotated[str, BeforeValidator(to_upper_and_strip)]
NormalizedStringList = Annotated[List[str], BeforeValidator(to_lower_and_dedup_list)]
