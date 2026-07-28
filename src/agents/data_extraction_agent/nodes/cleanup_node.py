from src.agents.llm_client import LLMClient
from src.agents.data_extraction_agent.state import ExtractionGraphState

_SYSTEM_PROMPT = (
    "Sei un assistente specializzato nell'estrazione dati. "
    "Il tuo compito è pulire il testo grezzo di una bolla di spedizione B2B. "
    "Rimuovi intestazioni, note legali, totali, dati bancari o qualsiasi altro dato "
    "che non riguardi strettamente gli articoli/prodotti spediti. "
    "Restituisci SOLO il testo relativo alle righe degli articoli, includendo anche "
    "le intestazioni delle colonne, senza alterare i dati in esse contenuti."
)

_MODEL = "gemini-3.1-flash-lite"


async def cleanup_node(state: ExtractionGraphState) -> dict:
    """
    Invoke the LLM to clean the raw PDF text.
    """
    print("[cleanup_node] Cleaning raw text via LLM...")
    client = LLMClient()
    prompt = f"Pulisci questo testo mantenendo solo le righe degli articoli:\n\n{state.raw_text}"

    try:
        cleaned = await client.call(
            model_name=_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Stage 1 - Raw Text Cleanup",
            response_mime_type="text/plain",
        )
        print("[cleanup_node] Cleanup complete.")
        return {"cleaned_text": cleaned}
    except Exception as exc:
        warning = f"[cleanup_node] LLM cleanup failed ({exc}), falling back to raw text."
        print(warning)
        return {"cleaned_text": state.raw_text, "warnings": [warning]}
