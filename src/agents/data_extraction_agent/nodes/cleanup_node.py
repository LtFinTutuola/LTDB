from src.agents.llm_client import LLMClient
from src.agents.data_extraction_agent.state import ExtractionGraphState
from src.core.logger import get_logger
from src.utils.llm_models import DATA_EXTRACTION_CLEANUP_MODEL as _MODEL
from src.utils.prompts import DATA_EXTRACTION_CLEANUP_SYSTEM_PROMPT

logger = get_logger()


async def cleanup_node(state: ExtractionGraphState) -> dict:
    """
    Invoke the LLM to clean the raw PDF text.
    """
    logger.log_agent("cleanup_node", "node_entry", "ok", raw_text=state.raw_text)
    print("[cleanup_node] Cleaning raw text via LLM...")
    client = LLMClient()
    prompt = f"Filtra il seguente documento mantenendo solo intestazioni di colonna e righe articolo:\n\n<RAW_DDT>\n{state.raw_text}\n</RAW_DDT>"

    try:
        cleaned = await client.call(
            model_name=_MODEL,
            system_prompt=DATA_EXTRACTION_CLEANUP_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 1 - Raw Text Cleanup",
            response_mime_type="text/plain",
            temperature=0.0,
        )
        print("[cleanup_node] Cleanup complete.")
        result = {"cleaned_text": cleaned}
        logger.log_agent("cleanup_node", "node_exit", "ok", output=result)
        return result
    except Exception as exc:
        warning = f"[cleanup_node] LLM cleanup failed ({exc}), falling back to raw text."
        print(warning)
        result = {"cleaned_text": state.raw_text, "warnings": [warning]}
        logger.log_agent("cleanup_node", "node_exit", "err", exc=exc, output=result)
        return result
