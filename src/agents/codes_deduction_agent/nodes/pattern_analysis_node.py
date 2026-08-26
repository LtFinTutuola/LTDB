"""
pattern_analysis_node.py
------------------------
Analyzes a set of vendor codes to identify structural encoding patterns.
Instructs the LLM to describe the encoding scheme in natural language.
On retries, includes previously failed codes as additional context.
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
    "Sei un AI Data Steward specializzato in analisi strutturale dei codici prodotto. "
    "Il tuo compito è analizzare un insieme di codici fornitore (vendor codes) per un brand "
    "e identificare lo schema di codifica utilizzato.\n\n"
    "Devi determinare:\n"
    "1. Quale parte del codice rappresenta l'identificativo del modello (model code)\n"
    "2. Quale parte rappresenta varianti (colore, taglia, ecc.)\n"
    "3. Quali delimitatori separano le parti\n"
    "4. Se esistono pattern multipli per lo stesso brand\n\n"
    "Restituisci ESCLUSIVAMENTE un JSON con le seguenti chiavi:\n"
    "- 'pattern_description': descrizione strutturale dettagliata dello schema di codifica\n"
    "- 'model_code_position': dove si trova il codice modello (es. 'before the hyphen', 'first N characters')\n"
    "- 'variant_parts': cosa rappresentano le parti variabili\n"
    "- 'multiple_formats': boolean, se ci sono formati multipli"
)


async def pattern_analysis_node(state: CodesDeductionGraphState) -> dict:
    """
    Analyze vendor codes to identify encoding patterns.
    On retries, includes failure context from previous validation attempts.
    """
    iteration = state.iteration
    logger.log_agent(
        "pattern_analysis_node", "node_entry", "ok",
        iteration=iteration,
        vendor_codes_count=len(state.vendor_codes),
        has_failures=bool(state.validation_failures),
    )
    print(f"[pattern_analysis_node] Analyzing {len(state.vendor_codes)} vendor codes (iteration {iteration + 1})...")

    client = LLMClient()
    model = _get_model()

    # Build sample list (limit to 50 for prompt efficiency)
    sample_codes = state.vendor_codes[:50]
    codes_text = "\n".join(f"- {code}" for code in sample_codes)
    if len(state.vendor_codes) > 50:
        codes_text += f"\n... e altri {len(state.vendor_codes) - 50} codici"

    prompt = (
        f"**Brand:** {state.brand_name}\n\n"
        f"**Codici Fornitore da Analizzare:**\n{codes_text}\n\n"
    )

    # Add context from previous explanation if recalculating
    if state.previous_explanation and iteration == 0:
        prompt += (
            f"**NOTA:** Questo è un ricalcolo. La regola precedente era:\n"
            f"'{state.previous_explanation}'\n"
            f"La regola deve essere aggiornata per coprire anche i nuovi codici.\n\n"
        )

    # Add failure context on retries
    if state.validation_failures:
        failures_text = "\n".join(f"- {code}" for code in state.validation_failures)
        prompt += (
            f"**ATTENZIONE:** Il tentativo precedente ha prodotto un regex che NON ha matchato "
            f"i seguenti codici:\n{failures_text}\n\n"
            f"Riesamina questi codici e aggiorna la tua analisi per includerli.\n"
        )

    prompt += "Analizza tutti i codici e identifica lo schema di codifica."

    try:
        raw_res = await client.call(
            model_name=model,
            system_prompt=_SYSTEM_PROMPT,
            prompt=prompt,
            pipeline_stage="Heuristic Deduction - Pattern Analysis",
            response_mime_type="application/json",
            temperature=0.0,
        )
        parsed = json.loads(raw_res)
        analysis = json.dumps(parsed, ensure_ascii=False)
    except Exception as exc:
        print(f"[pattern_analysis_node] Warning: Analysis failed ({exc}). Using raw response.")
        analysis = str(raw_res) if 'raw_res' in dir() else f"Analysis failed: {exc}"

    print(f"[pattern_analysis_node] Analysis complete.")
    result = {
        "analysis_result": analysis,
        "iteration": iteration + 1,
    }
    logger.log_agent("pattern_analysis_node", "node_exit", "ok", output=result)
    return result
