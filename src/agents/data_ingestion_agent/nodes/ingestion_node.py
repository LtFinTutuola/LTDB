"""
nodes/ingestion_node.py
------------------------
Stage 0: Extract raw text from the input PDF using pdfplumber.

This is the only node that performs I/O on the local filesystem.
On any failure (file unreadable, zero pages extracted) it raises
AgentException immediately, aborting the pipeline.
"""
import pdfplumber

from src.agents.base import AgentException
from src.agents.data_ingestion_agent.state import GraphState


async def ingestion_node(state: GraphState) -> dict:
    """
    Extract raw text from the PDF file referenced in state.file_path.

    Returns:
        Partial state update: {"raw_text": <extracted_text>}

    Raises:
        AgentException: If pdfplumber fails or no text is extracted.
    """
    pdf_path = state.file_path
    print(f"[ingestion_node] Extracting text from: {pdf_path}")

    try:
        pages: list[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)

        if not pages:
            raise AgentException(
                message=f"PDF '{pdf_path}' contains no extractable text.",
                output=None,
            )

        full_text = "\n--- PAGINA ---\n".join(pages)
        print(f"[ingestion_node] Extracted {len(pages)} page(s).")
        return {"raw_text": full_text}

    except AgentException:
        raise
    except Exception as exc:
        raise AgentException(
            message=f"Failed to read PDF '{pdf_path}': {exc}",
            output=None,
        ) from exc
