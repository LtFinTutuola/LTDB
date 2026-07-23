from typing import Dict, Any

def mock_extract_ddt_data(pdf_path: str) -> Dict[str, Any]:
    """
    Mocks the extraction of DDT data from a PDF file.
    Returns a dictionary compliant with DdtExtractionResponse schema.
    """
    return {
        "items": [
            {
                "supplier_code": "SUP-001",
                "description": "Widget A",
                "quantity": 100
            },
            {
                "supplier_code": "SUP-002",
                "description": "Widget B",
                "quantity": 50
            }
        ]
    }
