import pdfplumber

from src.agents.base import AgentException
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.core.logger import get_logger

logger = get_logger()

async def ingestion_node(state: ExtractionGraphState) -> dict:
    """
    Extract raw text from the PDF file referenced in state.file_path.
    """
    pdf_path = state.file_path
    logger.log_agent("ingestion_node", "node_entry", "ok", file_path=pdf_path)
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
        result = {"raw_text": full_text}
        logger.log_agent("ingestion_node", "node_exit", "ok", output=result)
        return result

    except AgentException:
        raise
    except Exception as exc:
        raise AgentException(
            message=f"Failed to read PDF '{pdf_path}': {exc}",
            output=None,
        ) from exc
