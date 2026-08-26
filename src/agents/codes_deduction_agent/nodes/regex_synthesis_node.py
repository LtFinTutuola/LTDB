"""
regex_synthesis_node.py
-----------------------
Translates the natural language pattern analysis into a Python regex
with a named capture group (?P<model_code>...) for the normalized vendor code.
"""
import json
from pathlib import Path
import yaml

from src.agents.llm_client import LLMClient
from src.agents.codes_deduction_agent.state import CodesDeductionGraphState
from src.core.logger import get_logger

logger = get_logger()


def _get_model() -> str:
    """Read the configurable model from gemini.yaml."""
    gemini_yaml_path = Path("src/agents/gemini.yaml")
    if gemini_yaml_path.exists():
        with open(gemini_yaml_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("codes_deduction_agent_model", "gemini-3.5-flash")
    return "gemini-3.5-flash"


_SYSTEM_PROMPT = (
    "Sei un esperto di espressioni regolari Python. "
    "Riceverai un'analisi strutturale di codici prodotto e devi tradurla in un'espressione regolare Python.\n\n"
    "REGOLE OBBLIGATORIE:\n"
    "1. Il regex DEVE contenere un named capture group chiamato 'model_code' che cattura "
    "l'identificativo del modello/prodotto (senza suffissi colore/taglia).\n"
    "2. Il regex deve matchare l'INTERO codice fornitore (usa ^ e $ per ancorarlo).\n"
    "3. Se ci sono pattern multipli, usa alternation groups (pattern_A|pattern_B), "
    "ma ogni alternation deve comunque estrarre il 'model_code'.\n"
    "4. NON usare lookbehind/lookahead complessi che potrebbero causare backtracking catastrofico.\n"
    "5. Mantieni il regex il più semplice e leggibile possibile.\n\n"
    "Restituisci ESCLUSIVAMENTE un JSON con le seguenti chiavi:\n"
    "- 'regex': l'espressione regolare Python come stringa\n"
    "- 'explanation': spiegazione in linguaggio naturale della regola di codifica, "
    "comprensibile da un operatore non tecnico (es. 'Le prime 10 lettere e cifre identificano il modello. "
    "Dopo il trattino seguono 3 lettere che indicano il colore.')"
)


async def regex_synthesis_node(state: CodesDeductionGraphState) -> dict:
    """
    Synthesize a Python regex from the pattern analysis result.
    """
    logger.log_agent(
        "regex_synthesis_node", "node_entry", "ok",
        iteration=state.iteration,
    )
    print(f"[regex_synthesis_node] Synthesizing regex from pattern analysis...")

    client = LLMClient()
    model = _get_model()

    # Provide a few sample codes for the LLM to test against mentally
    sample_codes = state.vendor_codes[:10]
    samples_text = "\n".join(f"- {code}" for code in sample_codes)

    prompt = (
        f"**Analisi Strutturale:**\n{state.analysis_result}\n\n"
        f"**Codici Esempio per Verifica:**\n{samples_text}\n\n"
        f"Genera il regex Python con il named capture group 'model_code'."
    )

    try:
        raw_res = await client.call(
            model_name=model,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Heuristic Deduction - Regex Synthesis",
            response_mime_type="application/json",
            temperature=0.0,
        )
        parsed = json.loads(raw_res)
        candidate_regex = parsed.get("regex", "")
        explanation = parsed.get("explanation", "")
    except Exception as exc:
        print(f"[regex_synthesis_node] Warning: Synthesis failed ({exc}).")
        candidate_regex = ""
        explanation = ""

    print(f"[regex_synthesis_node] Candidate regex: {candidate_regex}")
    result = {
        "candidate_regex": candidate_regex,
        "output_explanation": explanation,
    }
    logger.log_agent("regex_synthesis_node", "node_exit", "ok", output=result)
    return result
